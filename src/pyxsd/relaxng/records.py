"""RELAX NG record column compilation and fixed Arrow schemas.

Rows are compiled from the schema-side route graph before any document exists.
This module deliberately constructs only Arrow schemas in this phase; evidence
backed row extraction is a separate step.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    import pyarrow as pa

from pyxsd.document import Document
from pyxsd.integrations import IntegrationError
from pyxsd.integrations._shape import scalar_kind
from pyxsd.integrations._values import prepare_document
from pyxsd.integrations.projection import FieldSource, field_source_metadata
from pyxsd.namespaces import clark, local_name, namespace_of
from pyxsd.relaxng.contracts import (
    ScalarContract,
    candidate_id,
    list_shape,
    scalar_contract,
)
from pyxsd.relaxng.datatypes import ScalarDatatype, UnsupportedDatatypeOperation
from pyxsd.relaxng.errors import AmbiguousValueError
from pyxsd.relaxng.evidence import CandidateSelection, EvidenceGraph, EvidenceOccurrence
from pyxsd.relaxng.patterns import List as PatternList
from pyxsd.relaxng.projection import (
    Cardinality,
    ContentContract,
    FieldContract,
    ProjectionPlan,
    RouteContract,
)
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.values import AlternativeValue, _list_token_plan
from pyxsd.xsd_data_types import QName

__all__ = [
    "RelaxNGColumnBinding",
    "RelaxNGProjectionPlan",
    "RelaxNGRecordProjection",
    "compile_relaxng_projection",
    "iter_relaxng_rows",
]

_DATASET_EXTRACTION = "RELAX NG dataset writing is not implemented by Task 6; Task 7 owns datasets"
_MAX_DEFAULT_DEPTH = 256
_QNAME_TYPES = frozenset({"QName", "NOTATION"})
_TEMPORAL_TYPES = frozenset({"date", "time", "dateTime", "duration"})


def _pyarrow() -> Any:
    """Import the optional backend only when an Arrow schema is requested."""
    try:
        import pyarrow as pyarrow_module
    except ModuleNotFoundError as exc:
        if exc.name != "pyarrow":
            raise
        raise ImportError("Install pyxsd[arrow] to use Arrow and Parquet projections") from exc
    return pyarrow_module


def _pyarrow_parquet() -> Any:
    """Import the optional Parquet backend with the same extra-specific error."""
    _pyarrow()
    import pyarrow.parquet as pyarrow_parquet

    return pyarrow_parquet


@dataclass(frozen=True)
class RelaxNGColumnBinding:
    """One explicit scalar column bound to a schema-static endpoint."""

    name: str
    source: FieldSource
    occurrence_route: RouteContract
    contracts: tuple[ScalarContract, ...]
    selected_contract: ScalarContract
    nullable: bool
    arrow_shape: Any
    candidate: str = ""
    declaration: tuple[str, ...] = ()
    arrow_fallback: str | None = None

    @property
    def candidate_id(self) -> str:
        """The selected contract's grammar-scoped candidate identifier."""
        return self.candidate


@dataclass(frozen=True)
class RelaxNGProjectionPlan:
    """A finite column or default-record plan, independent of any instance."""

    schema: RelaxNGSchema
    route: RouteContract
    columns: tuple[RelaxNGColumnBinding, ...]
    row_chain: tuple[RouteContract, ...] = ()
    default_fields: tuple[Any, ...] = ()
    mode: Literal["columns", "default"] = "columns"
    row_path: tuple[str, ...] = ()
    grammar_fingerprint: str = ""


@dataclass(frozen=True)
class _RouteStep:
    """One resolved child edge; named and wildcard views share this form."""

    field: FieldContract | None
    cardinality: Cardinality
    scalar_contracts: tuple[ScalarContract, ...]
    child_route_id: str
    expanded_name: str


def _qualify(value: object, what: str) -> tuple[str | None, str]:
    """Parse a local, Clark, or EQName spelling without process prefix state."""
    if not isinstance(value, str) or not value:
        raise IntegrationError(f"{what} must be a nonempty expanded name")
    if value.startswith(("Q{", "{")):
        body = value[2:] if value.startswith("Q{") else value[1:]
        uri, separator, local = body.partition("}")
        if not separator or not local or "{" in local or "}" in local:
            raise IntegrationError(f"{what} {value!r}: malformed Clark or EQName")
        return uri or None, local
    if ":" in value:
        raise IntegrationError(f"{what} {value!r}: prefix-form EQName requires a namespace context")
    if "}" in value:
        raise IntegrationError(f"{what} {value!r}: malformed expanded name")
    return None, value


def _start_route(plan: ProjectionPlan, element: object) -> RouteContract:
    uri, local = _qualify(element, "start element")
    expanded = clark(uri, local)
    roots = [plan.routes[route_id] for route_id in plan.roots]
    exact = [route for route in roots if route.expanded_name == expanded]
    if exact:
        return exact[0]
    wildcard = [
        route
        for route in roots
        if route.expanded_name is None and route.name_class.contains(uri, local)
    ]
    if wildcard:
        return wildcard[0]
    raise IntegrationError(f"start element {element!r} is unknown in this RELAX NG grammar")


def _child_step(plan: ProjectionPlan, route: RouteContract, spelling: str, what: str) -> _RouteStep:
    uri, local = _qualify(spelling, what)
    expanded = clark(uri, local)
    field = route.children.get(expanded)
    if field is not None:
        return _RouteStep(
            field,
            field.cardinality,
            field.scalar_contracts,
            cast(str, field.child_route_id),
            expanded,
        )
    wildcards = [
        wildcard
        for wildcard in route.wildcards
        if wildcard.role == "child" and wildcard.name_class.contains(uri, local)
    ]
    if wildcards:
        wildcard = wildcards[0]
        return _RouteStep(
            None,
            wildcard.cardinality,
            wildcard.scalar_contracts,
            cast(str, wildcard.child_route_id),
            expanded,
        )
    raise IntegrationError(f"{what} {spelling!r} is unknown at this route")


def _resolve_row(
    plan: ProjectionPlan, element: object, path: object
) -> tuple[RouteContract, tuple[RouteContract, ...], tuple[str, ...]]:
    if not isinstance(path, tuple) or not all(isinstance(step, str) and step for step in path):
        raise IntegrationError("row path must be a tuple of nonempty step names")
    route = _start_route(plan, element)
    uri, local = _qualify(element, "start element")
    chain = [route]
    names = [clark(uri, local)]
    for step in path:
        edge = _child_step(plan, route, step, "row path step")
        child = plan.routes[edge.child_route_id]
        chain.append(child)
        names.append(edge.expanded_name)
        route = child
    return route, tuple(chain), tuple(names)


def _source_endpoint(
    plan: ProjectionPlan,
    row_chain: tuple[RouteContract, ...],
    row_names: tuple[str, ...],
    source: FieldSource,
) -> tuple[RouteContract, tuple[str, ...], bool]:
    if source.scope == "root":
        index = 0
    elif source.scope == "ancestor":
        index = len(row_chain) - 1 - source.levels
        if index < 0:
            raise IntegrationError("ancestor source climbs above the configured root")
    else:
        index = len(row_chain) - 1
    chain = list(row_chain[: index + 1])
    names = list(row_names[: index + 1])
    nullable = False
    for step in source.path:
        edge = _child_step(plan, chain[-1], step, "column path step")
        if edge.cardinality.repeatable:
            raise IntegrationError("downward column route is potentially repeated")
        child = plan.routes[edge.child_route_id]
        if any(existing.route_id == child.route_id for existing in chain):
            raise IntegrationError("column path repeats a route already on its chain")
        if edge.cardinality.minimum == 0:
            nullable = True
        chain.append(child)
        names.append(edge.expanded_name)
    return chain[-1], tuple(names), nullable


def _attribute_field(route: RouteContract, spelling: str) -> FieldContract:
    uri, local = _qualify(spelling, "column attribute")
    expanded = clark(uri, local)
    field = route.attributes.get(expanded)
    if field is not None:
        return field
    if uri is None:
        matches = [
            candidate for name, candidate in route.attributes.items() if local_name(name) == local
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise IntegrationError(f"column attribute {spelling!r} is ambiguous by local name")
    raise IntegrationError(f"column attribute {spelling!r} is missing from the endpoint route")


def _distinct(contracts: Iterable[ScalarContract]) -> tuple[ScalarContract, ...]:
    return tuple(sorted(set(contracts), key=lambda contract: contract.pattern_identity))


def _content_may_be_empty(model: ContentContract) -> bool:
    """Whether compiled element content admits no scalar occurrence."""
    results: dict[int, bool] = {}
    pending: list[tuple[ContentContract, bool]] = [(model, False)]
    while pending:
        node, ready = pending.pop()
        if id(node) in results:
            continue
        if not ready:
            pending.append((node, True))
            for operand in node.operands:
                if id(operand) not in results:
                    pending.append((operand, False))
            continue
        if node.kind in ("attribute", "child", "element", "empty"):
            value = True
        elif node.kind == "scalar":
            value = any(contract.kind == "text" for contract in node.scalar_contracts)
        elif node.kind == "notAllowed":
            value = False
        elif node.kind in ("group", "interleave"):
            value = all(results[id(operand)] for operand in node.operands)
        elif node.kind == "choice":
            value = any(results[id(operand)] for operand in node.operands)
        elif node.kind == "repeat":
            value = all(results[id(operand)] for operand in node.operands)
        else:
            value = False
        results[id(node)] = value
    return results[id(model)]


def _content_cannot_match(model: ContentContract) -> bool:
    """Whether the whole content model is impossible (``notAllowed``)."""
    results: dict[int, bool] = {}
    pending: list[tuple[ContentContract, bool]] = [(model, False)]
    while pending:
        node, ready = pending.pop()
        if id(node) in results:
            continue
        if not ready:
            pending.append((node, True))
            for operand in node.operands:
                if id(operand) not in results:
                    pending.append((operand, False))
            continue
        if node.kind == "notAllowed":
            value = True
        elif node.kind in ("empty", "attribute", "child", "element", "scalar"):
            value = False
        elif node.kind in ("group", "interleave"):
            value = any(results[id(operand)] for operand in node.operands)
        elif node.kind in ("choice", "repeat"):
            value = all(results[id(operand)] for operand in node.operands)
        else:
            value = False
        results[id(node)] = value
    return results[id(model)]


def _check_default_route(route: RouteContract) -> None:
    if route.wildcards:
        raise IntegrationError("default route has wildcard content")
    if route.content.mixed:
        raise IntegrationError("default route has mixed content")
    if _content_cannot_match(route.content.model):
        raise IntegrationError("default route content is notAllowed")
    if route.generic_reason is not None:
        raise IntegrationError(f"default route is generic: {route.generic_reason}")


def _check_endpoint(route: RouteContract) -> None:
    if route.children:
        raise IntegrationError("selected endpoint is a struct with child elements")
    _check_default_route(route)


def _single_contract(contracts: Iterable[ScalarContract], description: str) -> ScalarContract:
    distinct = _distinct(contracts)
    if not distinct:
        raise IntegrationError(f"{description} has no static scalar contract")
    if len(distinct) > 1:
        raise IntegrationError(
            f"{description} has {len(distinct)} distinct scalar contracts; "
            "ambiguous; set datatype/candidate_id"
        )
    return distinct[0]


def _contract_document(contract: ScalarContract) -> dict[str, object]:
    return {
        "kind": contract.kind,
        "library_uri": contract.library_uri,
        "type_name": contract.type_name,
        "parameters": [
            {"name": parameter.name, "value": parameter.value} for parameter in contract.parameters
        ],
        "restrictions": [
            {"kind": restriction.kind, "pattern_identity": restriction.pattern_identity}
            for restriction in contract.restrictions
        ],
        "pattern_identity": contract.pattern_identity,
    }


def _field_metadata(
    name: str,
    declaration: tuple[str, ...],
    row_path: tuple[str, ...],
    contract: ScalarContract | None,
    candidate: str | None,
    arrow_shape: Any,
    arrow_fallback: str | None,
    route_id: str,
    source: FieldSource | None = None,
) -> dict[bytes, bytes]:
    metadata = {
        b"pyxsd:name": name.encode(),
        b"pyxsd:declaration": json.dumps(list(declaration)).encode(),
        b"pyxsd:row": json.dumps(list(row_path)).encode(),
        b"pyxsd:type": (
            "struct" if contract is None else contract.type_name or contract.kind
        ).encode(),
        b"pyxsd:arrow": str(arrow_shape).encode(),
        b"pyxsd:column_route": route_id.encode(),
    }
    if contract is not None:
        metadata[b"pyxsd:contract"] = json.dumps(
            _contract_document(contract), sort_keys=True
        ).encode()
    if candidate is not None:
        metadata[b"pyxsd:candidate"] = candidate.encode()
    if arrow_fallback is not None:
        metadata[b"pyxsd:arrow_fallback"] = arrow_fallback.encode()
    if source is not None:
        metadata[b"pyxsd:source"] = json.dumps(field_source_metadata(source)).encode()
    return metadata


def _atomic_arrow(contract: ScalarContract, plan: ProjectionPlan) -> tuple[Any, str | None]:
    pa = _pyarrow()
    pattern = plan.encoding_patterns[contract.pattern_identity]
    datatype = getattr(pattern, "datatype", None)
    if not isinstance(datatype, ScalarDatatype):
        return pa.string(), "custom datatype has no Arrow-compatible XSD backing type"
    try:
        kind = scalar_kind(datatype.xsd_type)
    except (IntegrationError, TypeError) as exc:
        return (
            pa.string(),
            f"unsupported XSD backing type for {contract.type_name or contract.kind!r}: {exc}",
        )
    if kind == "bool":
        return pa.bool_(), None
    if kind == "int":
        return pa.int64(), None
    if kind == "decimal":
        constraints = getattr(datatype, "constraints", None)
        precision = getattr(constraints, "total_digits", None)
        scale = getattr(constraints, "fraction_digits", None)
        if precision is None or scale is None:
            return pa.string(), "unbounded decimal precision or scale; using string"
        if precision > 38:
            return (
                pa.string(),
                f"decimal precision {precision} exceeds Arrow decimal128 precision 38; using string",
            )
        if scale > 18:
            return (
                pa.string(),
                f"decimal scale {scale} exceeds supported Arrow scale 18; using string",
            )
        if not 0 <= scale <= precision:
            return (
                pa.string(),
                f"decimal totalDigits {precision} and fractionDigits {scale} are inconsistent; "
                "using string",
            )
        return pa.decimal128(precision, scale), None
    if kind == "float":
        return pa.float64(), None
    if kind in ("base64", "hex"):
        return pa.binary(), None
    if kind == "list":
        return pa.string(), "datatype-level list values have no static Arrow item type"
    mapping = {
        "qname": pa.string(),
        "str": pa.string(),
    }
    return mapping.get(kind, pa.string()), None


def _contract_arrow(contract: ScalarContract, plan: ProjectionPlan) -> tuple[Any, str | None]:
    pa = _pyarrow()
    if contract.kind == "text":
        return pa.string(), None
    if contract.kind == "atomic":
        return _atomic_arrow(contract, plan)
    pattern = plan.encoding_patterns[contract.pattern_identity]
    shape = list_shape(pattern)
    if shape == "interpretation":
        raise IntegrationError(
            "whole-list contract has no schema-static homogeneous or fixed shape"
        )
    plan_tokens = cast(Any, _list_token_plan(cast(PatternList, pattern)))
    item_types: list[Any] = []
    fallbacks: list[str] = []
    for token in plan_tokens.patterns:
        arrow_shape, fallback = _atomic_arrow(scalar_contract(token), plan)
        item_types.append(arrow_shape)
        if fallback is not None:
            fallbacks.append(fallback)
    if not item_types:
        raise IntegrationError("whole-list contract has no scalar token contracts")
    item_type = item_types[0]
    if any(candidate != item_type for candidate in item_types[1:]):
        raise IntegrationError(
            "whole-list contract has heterogeneous token types with no static Arrow item type"
        )
    disclosure = "; ".join(dict.fromkeys(fallbacks)) or None
    return pa.list_(pa.field("item", item_type, nullable=False)), disclosure


def _is_primitive(route: RouteContract) -> bool:
    return not route.children and not route.attributes


def _default_element_shape(
    route: RouteContract,
    plan: ProjectionPlan,
    grammar_fingerprint: str,
    path: tuple[str, ...],
    row_path: tuple[str, ...],
    stack: tuple[str, ...],
) -> tuple[Any, ScalarContract | None, str | None, bool]:
    pa = _pyarrow()
    if len(path) > _MAX_DEFAULT_DEPTH:
        raise IntegrationError(
            f"default field path {'/'.join(path)} (route {route.route_id}): "
            f"nesting exceeds limit {_MAX_DEFAULT_DEPTH}"
        )
    if route.route_id in stack:
        raise IntegrationError(
            f"default field path {'/'.join(path)} (route {route.route_id}): "
            "route is recursive; Arrow cannot express an infinite struct"
        )
    try:
        _check_default_route(route)
    except IntegrationError as exc:
        raise IntegrationError(
            f"default field path {'/'.join(path)} (route {route.route_id}): {exc}"
        ) from exc
    if _is_primitive(route):
        contract = _single_contract(
            route.content.scalar_contracts,
            f"default field path {'/'.join(path)} (route {route.route_id})",
        )
        arrow_shape, fallback = _contract_arrow(contract, plan)
        return (
            arrow_shape,
            contract,
            fallback,
            _content_may_be_empty(route.content.model),
        )
    fields = _default_record_fields(
        route, plan, grammar_fingerprint, path, row_path, (*stack, route.route_id)
    )
    return pa.struct(fields), None, None, False


def _default_root_fields(
    route: RouteContract,
    plan: ProjectionPlan,
    grammar_fingerprint: str,
    row_path: tuple[str, ...],
) -> tuple[Any, ...]:
    pa = _pyarrow()
    try:
        _check_default_route(route)
    except IntegrationError as exc:
        raise IntegrationError(
            f"default field path {'/'.join(row_path)} (route {route.route_id}): {exc}"
        ) from exc
    if _is_primitive(route):
        contract = _single_contract(
            route.content.scalar_contracts,
            f"default field path {'/'.join(row_path)} (route {route.route_id})",
        )
        arrow_shape, fallback = _contract_arrow(contract, plan)
        return (
            pa.field(
                "value",
                arrow_shape,
                nullable=_content_may_be_empty(route.content.model),
                metadata=_field_metadata(
                    row_path[-1],
                    row_path,
                    row_path,
                    contract,
                    candidate_id(grammar_fingerprint, contract),
                    arrow_shape,
                    fallback,
                    route_id=route.route_id,
                ),
            ),
        )
    fields = _default_record_fields(
        route, plan, grammar_fingerprint, row_path, row_path, (route.route_id,)
    )
    return tuple(fields)


def _default_record_fields(
    route: RouteContract,
    plan: ProjectionPlan,
    grammar_fingerprint: str,
    path: tuple[str, ...],
    row_path: tuple[str, ...],
    stack: tuple[str, ...],
) -> list[Any]:
    pa = _pyarrow()
    fields: list[Any] = []
    for attribute in route.attributes.values():
        contract = _single_contract(
            attribute.scalar_contracts,
            f"default attribute {'/'.join((*path, attribute.expanded_name))} "
            f"(route {route.route_id})",
        )
        arrow_shape, fallback = _contract_arrow(contract, plan)
        fields.append(
            pa.field(
                attribute.alias,
                arrow_shape,
                nullable=attribute.cardinality.minimum == 0,
                metadata=_field_metadata(
                    attribute.expanded_name,
                    (*path, f"@{attribute.expanded_name}"),
                    row_path,
                    contract,
                    candidate_id(grammar_fingerprint, contract),
                    arrow_shape,
                    fallback,
                    route_id=route.route_id,
                ),
            )
        )
    if route.content.scalar_contracts:
        contract = _single_contract(
            route.content.scalar_contracts,
            f"default text path {'/'.join(path)} (route {route.route_id})",
        )
        arrow_shape, fallback = _contract_arrow(contract, plan)
        fields.append(
            pa.field(
                "$",
                arrow_shape,
                nullable=_content_may_be_empty(route.content.model),
                metadata=_field_metadata(
                    "$",
                    (*path, "$"),
                    row_path,
                    contract,
                    candidate_id(grammar_fingerprint, contract),
                    arrow_shape,
                    fallback,
                    route_id=route.route_id,
                ),
            )
        )
    for child in route.children.values():
        child_route = plan.routes[cast(str, child.child_route_id)]
        child_path = (*path, child.expanded_name)
        child_shape, child_contract, child_fallback, child_empty = _default_element_shape(
            child_route, plan, grammar_fingerprint, child_path, row_path, stack
        )
        if child.cardinality.repeatable:
            item_nullable = child_empty if child_contract is not None else False
            data_type = pa.list_(pa.field("item", child_shape, nullable=item_nullable))
            nullable = child.cardinality.minimum == 0
            metadata_shape = data_type
        else:
            data_type = child_shape
            nullable = child.cardinality.minimum == 0 or (
                child_contract is not None and child_empty
            )
            metadata_shape = child_shape
        fields.append(
            pa.field(
                child.alias,
                data_type,
                nullable=nullable,
                metadata=_field_metadata(
                    child.expanded_name,
                    child_path,
                    row_path,
                    child_contract,
                    (
                        None
                        if child_contract is None
                        else candidate_id(grammar_fingerprint, child_contract)
                    ),
                    metadata_shape,
                    child_fallback,
                    route_id=child_route.route_id,
                ),
            )
        )
    return fields


def _select_contract(
    source: FieldSource,
    contracts: tuple[ScalarContract, ...],
    *,
    grammar_fingerprint: str,
    description: str,
) -> ScalarContract:
    if source.datatype is not None:
        library_uri, type_name = source.datatype
        matches = [
            contract
            for contract in contracts
            if contract.library_uri == library_uri and contract.type_name == type_name
        ]
        if not matches:
            raise IntegrationError(
                f"{description}: datatype {(library_uri, type_name)!r} matches no "
                "static scalar contract"
            )
        if len(matches) > 1:
            raise IntegrationError(
                f"{description}: datatype {(library_uri, type_name)!r} matches "
                f"{len(matches)} distinct contracts; set candidate_id"
            )
        return matches[0]
    if source.candidate_id is not None:
        matches = [
            contract
            for contract in contracts
            if candidate_id(grammar_fingerprint, contract) == source.candidate_id
        ]
        if not matches:
            raise IntegrationError(
                f"{description}: candidate_id {source.candidate_id!r} is unknown"
            )
        return matches[0]
    if len(contracts) != 1:
        raise IntegrationError(
            f"{description}: {len(contracts)} distinct scalar contracts; "
            "ambiguous; set datatype/candidate_id"
        )
    return contracts[0]


def compile_relaxng_projection(
    schema: RelaxNGSchema,
    *,
    element: str,
    path: tuple[str, ...],
    columns: Mapping[str, FieldSource] | None,
) -> RelaxNGProjectionPlan:
    """Bind explicit columns or the default record shape before rows exist."""
    if not isinstance(schema, RelaxNGSchema):
        raise IntegrationError("RELAX NG projections require a RelaxNGSchema")
    schema.require_valid()
    plan = schema.projection
    grammar_fingerprint = schema._grammar_fingerprint
    if columns is not None:
        if not isinstance(columns, Mapping) or not columns:
            raise IntegrationError(
                "columns must be a nonempty mapping of output names to FieldSource"
            )
        columns = dict(columns)
        try:
            route, row_chain, row_names = _resolve_row(plan, element, path)
        except IntegrationError as exc:
            name = next(iter(columns))
            raise IntegrationError(
                f"column {name!r} source {columns[name]!r} row route {(element, path)!r}: {exc}"
            ) from exc
        bindings: list[RelaxNGColumnBinding] = []
        for name, source in columns.items():
            try:
                if not isinstance(name, str) or not name:
                    raise IntegrationError("output name must be a nonempty string")
                if not isinstance(source, FieldSource):
                    raise IntegrationError("column source must be a FieldSource")
                current, names, nullable = _source_endpoint(plan, row_chain, row_names, source)
                if source.attribute is not None:
                    attribute = _attribute_field(current, source.attribute)
                    if attribute.cardinality.minimum == 0:
                        nullable = True
                    contracts = _distinct(attribute.scalar_contracts)
                    declaration = (*names, f"@{attribute.expanded_name}")
                else:
                    _check_endpoint(current)
                    contracts = _distinct(current.content.scalar_contracts)
                    if not contracts:
                        raise IntegrationError("selected endpoint has no static scalar contract")
                    nullable = nullable or _content_may_be_empty(current.content.model)
                    declaration = names
                contract = _select_contract(
                    source,
                    contracts,
                    grammar_fingerprint=grammar_fingerprint,
                    description=f"endpoint {'/'.join(declaration)}",
                )
                arrow_shape, fallback = _contract_arrow(contract, plan)
                bindings.append(
                    RelaxNGColumnBinding(
                        name,
                        source,
                        current,
                        contracts,
                        contract,
                        nullable,
                        arrow_shape,
                        candidate_id(grammar_fingerprint, contract),
                        declaration,
                        fallback,
                    )
                )
            except IntegrationError as exc:
                raise IntegrationError(
                    f"column {name!r} source {source!r} on {'/'.join(row_names)}: {exc}"
                ) from exc
        return RelaxNGProjectionPlan(
            schema=schema,
            route=route,
            columns=tuple(bindings),
            row_chain=row_chain,
            default_fields=(),
            mode="columns",
            row_path=row_names,
            grammar_fingerprint=grammar_fingerprint,
        )
    route, row_chain, row_names = _resolve_row(plan, element, path)
    default_fields = _default_root_fields(route, plan, grammar_fingerprint, row_names)
    return RelaxNGProjectionPlan(
        schema=schema,
        route=route,
        columns=(),
        row_chain=row_chain,
        default_fields=default_fields,
        mode="default",
        row_path=row_names,
        grammar_fingerprint=grammar_fingerprint,
    )


def _format_path(path: Iterable[str]) -> str:
    return "/".join(path) or "/"


def _route_label(route: RouteContract) -> str:
    return route.expanded_name or route.route_id


def _node_name(node: Any) -> str:
    return cast(str, node._name_)


def _node_route_id(node: Any) -> str | None:
    """Return the generated projection route of one lexical node, if any."""
    node_type = type(node)
    for attribute in ("_relaxng_route_id_", "__relaxng_route_id__"):
        value = getattr(node_type, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


def _node_matches_route(node: Any, route: RouteContract) -> bool:
    """Route identity is generated; only a generic route falls back to names."""
    actual = _node_route_id(node)
    if actual is not None:
        return actual == route.route_id
    if route.generic_reason is None:
        return False
    name = _node_name(node)
    if route.expanded_name is not None and name != route.expanded_name:
        return False
    return route.name_class.contains(namespace_of(name), local_name(name))


def _text_sites(graph: EvidenceGraph) -> dict[Any, EvidenceOccurrence]:
    result: dict[Any, EvidenceOccurrence] = {}
    for occurrence in graph.occurrences:
        if occurrence.kind == "text" and occurrence.content_index == 0:
            result.setdefault(occurrence.element, occurrence)
    return result


def _attribute_sites(graph: EvidenceGraph) -> dict[tuple[Any, str], EvidenceOccurrence]:
    result: dict[tuple[Any, str], EvidenceOccurrence] = {}
    for occurrence in graph.occurrences:
        if occurrence.kind == "attribute" and occurrence.name is not None:
            result.setdefault((occurrence.element, occurrence.name), occurrence)
    return result


@dataclass
class _RowContext:
    """Operation-local indexes and conversion shapes for one document."""

    plan: RelaxNGProjectionPlan
    document: Any
    graph: EvidenceGraph
    text_sites: dict[Any, EvidenceOccurrence]
    attribute_sites: dict[tuple[Any, str], EvidenceOccurrence]
    arrow_shapes: dict[str, Any]


def _context(plan: RelaxNGProjectionPlan, document: Document) -> _RowContext:
    graph = cast(EvidenceGraph, cast(Any, document).binding_evidence)
    return _RowContext(
        plan,
        document,
        graph,
        _text_sites(graph),
        _attribute_sites(graph),
        {},
    )


def _arrow_shape(context: _RowContext, contract: ScalarContract) -> Any:
    shape = context.arrow_shapes.get(contract.pattern_identity)
    if shape is None:
        shape, _ = _contract_arrow(contract, context.plan.schema.projection)
        context.arrow_shapes[contract.pattern_identity] = shape
    return shape


@dataclass(frozen=True)
class _SelectedValue:
    value: Any
    selection: CandidateSelection | None = None


def _convert_value(
    contract: ScalarContract,
    value: Any,
    arrow_shape: Any,
    label: str,
) -> Any:
    """Convert one selected typed value to the compiled Arrow representation."""
    pa = _pyarrow()
    if contract.kind == "text":
        if not isinstance(value, str):
            raise IntegrationError(
                f"{label}: text scalar decoded to {type(value).__name__}, not a string"
            )
        return str(value)
    type_name = contract.type_name or ""
    if type_name in _QNAME_TYPES:
        if not isinstance(value, QName):
            raise IntegrationError(f"{label}: {type_name} value lacks scoped QName evidence")
        try:
            return value.to_expanded_name(strict=True)
        except ValueError as exc:
            raise IntegrationError(
                f"{label}: {type_name} value {str(value)!r} has no resolvable namespace prefix"
            ) from exc
    if type_name in _TEMPORAL_TYPES:
        lexical = getattr(value, "lexical", None)
        if callable(lexical):
            value = lexical()
        if not isinstance(value, str):
            raise IntegrationError(f"{label}: {type_name} value has no lexical string")
        return str(value)
    if isinstance(value, bool):
        if not pa.types.is_boolean(arrow_shape):
            raise IntegrationError(f"{label}: boolean value cannot fill Arrow {arrow_shape}")
        return value
    if isinstance(value, int):
        if not (
            pa.types.is_integer(arrow_shape)
            or pa.types.is_floating(arrow_shape)
            or pa.types.is_decimal(arrow_shape)
        ):
            raise IntegrationError(f"{label}: integer value cannot fill Arrow {arrow_shape}")
        return value
    if isinstance(value, float):
        if not (pa.types.is_floating(arrow_shape) or pa.types.is_decimal(arrow_shape)):
            raise IntegrationError(f"{label}: float value cannot fill Arrow {arrow_shape}")
        return value
    if isinstance(value, Decimal):
        if pa.types.is_string(arrow_shape):
            return format(value, "f")
        if not pa.types.is_decimal(arrow_shape):
            raise IntegrationError(f"{label}: decimal value cannot fill Arrow {arrow_shape}")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        if not pa.types.is_binary(arrow_shape):
            raise IntegrationError(f"{label}: binary value cannot fill Arrow {arrow_shape}")
        return bytes(value)
    if isinstance(value, str):
        if not pa.types.is_string(arrow_shape):
            raise IntegrationError(f"{label}: string value cannot fill Arrow {arrow_shape}")
        return str(value)
    lexical = getattr(value, "lexical", None)
    if callable(lexical) and pa.types.is_string(arrow_shape):
        return str(lexical())
    raise IntegrationError(
        f"{label}: typed {type(value).__name__} value cannot be represented as Arrow {arrow_shape}"
    )


def _list_item_contracts(
    plan: RelaxNGProjectionPlan, contract: ScalarContract
) -> tuple[str, tuple[ScalarContract, ...]]:
    pattern = cast(PatternList, plan.schema.projection.encoding_patterns[contract.pattern_identity])
    shape = list_shape(pattern)
    token_plan = cast(Any, _list_token_plan(pattern))
    contracts = tuple(scalar_contract(token) for token in token_plan.patterns)
    if shape == "tuple":
        return shape, contracts
    return shape, (contracts[0],)


def _convert_sequence(
    plan: RelaxNGProjectionPlan,
    contract: ScalarContract,
    value: Any,
    arrow_shape: Any,
    label: str,
) -> Any:
    shape, contracts = _list_item_contracts(plan, contract)
    item_arrow = cast(Any, arrow_shape).value_type
    if shape == "tuple":
        return tuple(
            _convert_value(item_contract, item, item_arrow, label)
            for item_contract, item in zip(contracts, value, strict=True)
        )
    return [_convert_value(contracts[0], item, item_arrow, label) for item in value]


def _selected_alternative(
    context: _RowContext,
    value: AlternativeValue,
    contract: ScalarContract,
    occurrence: EvidenceOccurrence,
    arrow_shape: Any,
    label: str,
) -> _SelectedValue:
    identifier = candidate_id(context.plan.grammar_fingerprint, contract)
    candidate = next(
        (item for item in value.candidates if item.candidate_id == identifier),
        None,
    )
    if candidate is None:
        raise IntegrationError(
            f"{label}: selected candidate {identifier!r} did not survive at this scalar site"
        )
    if contract.kind == "text":
        return _SelectedValue(str(value.lexical), CandidateSelection(occurrence, identifier))
    if contract.kind == "list":
        interpretation = value.as_interpretation(identifier)
        try:
            decoded = interpretation.as_python()
        except (AmbiguousValueError, UnsupportedDatatypeOperation, ValueError) as exc:
            raise IntegrationError(f"{label}: selected whole list is not decodable: {exc}") from exc
        converted = _convert_sequence(context.plan, contract, decoded, arrow_shape, label)
        return _SelectedValue(converted, CandidateSelection(occurrence, identifier))
    if not candidate.decodable:
        raise IntegrationError(
            f"{label}: selected candidate {identifier!r} has no datatype decoder"
        )
    decoded = value.as_candidate(identifier)
    return _SelectedValue(
        _convert_value(contract, decoded, arrow_shape, label),
        CandidateSelection(occurrence, identifier),
    )


def _read_selected_scalar(
    context: _RowContext,
    node: Any,
    occurrence: EvidenceOccurrence | None,
    contract: ScalarContract,
    arrow_shape: Any,
    *,
    attribute: str | None,
    label: str,
) -> _SelectedValue:
    """Read only the selected surviving contract at one exact scalar occurrence."""
    typed_value = node.attribute_value(attribute) if attribute is not None else node.scalar_value()
    assert typed_value is not None
    schema_contracts = context.graph.schema_contracts_at(cast(EvidenceOccurrence, occurrence))
    if len(schema_contracts) == 1 and schema_contracts[0] != contract:
        raise IntegrationError(
            f"{label}: current scalar evidence differs from the selected contract"
        )
    if isinstance(typed_value, AlternativeValue):
        return _selected_alternative(
            context,
            typed_value,
            contract,
            cast(EvidenceOccurrence, occurrence),
            arrow_shape,
            label,
        )
    if contract.kind == "list":
        converted = _convert_sequence(context.plan, contract, typed_value, arrow_shape, label)
    else:
        converted = _convert_value(contract, typed_value, arrow_shape, label)
    return _SelectedValue(
        converted,
        CandidateSelection(
            cast(EvidenceOccurrence, occurrence),
            candidate_id(context.plan.grammar_fingerprint, contract),
        ),
    )


def _read_element_value(
    context: _RowContext,
    node: Any,
    contract: ScalarContract,
    *,
    nullable: bool,
    label: str,
) -> _SelectedValue:
    arrow_shape = _arrow_shape(context, contract)
    if node.text_value() is None:
        if contract.kind == "list":
            return _SelectedValue([])
        if nullable:
            return _SelectedValue(None)
        raise IntegrationError(f"{label}: required scalar content is absent")
    occurrence = context.text_sites.get(node)
    return _read_selected_scalar(
        context,
        node,
        occurrence,
        contract,
        arrow_shape,
        attribute=None,
        label=label,
    )


def _read_attribute_value(
    context: _RowContext,
    node: Any,
    field: FieldContract,
    contract: ScalarContract,
    *,
    label: str,
) -> _SelectedValue:
    if node.attribute_lexical(field.expanded_name) is None:
        return _SelectedValue(None)
    occurrence = context.attribute_sites.get((node, field.expanded_name))
    return _read_selected_scalar(
        context,
        node,
        occurrence,
        contract,
        _arrow_shape(context, contract),
        attribute=field.expanded_name,
        label=label,
    )


def _record_selection(
    selections: list[tuple[CandidateSelection, str]],
    selected: _SelectedValue,
    label: str,
) -> None:
    if selected.selection is not None:
        selections.append((selected.selection, label))


def _climb_row(
    context: _RowContext,
    row: Any,
    target: int,
    source: FieldSource,
    label: str,
) -> Any:
    chain = context.plan.row_chain
    node = row
    index = len(chain) - 1
    while index > target:
        parent = node.parent
        if parent is None:
            raise IntegrationError(
                f"{label}: source climbs above the configured root "
                f"{chain[0].expanded_name or chain[0].route_id}"
            )
        index -= 1
        expected = chain[index]
        if not _node_matches_route(parent, expected):
            raise IntegrationError(
                f"{label}: ancestor node does not match row path route {_route_label(expected)}"
            )
        node = parent
    return node


def _column_endpoint(
    context: _RowContext,
    binding: RelaxNGColumnBinding,
    row: Any,
    label: str,
) -> tuple[Any | None, RouteContract]:
    chain = context.plan.row_chain
    source = binding.source
    if source.scope == "root":
        target = 0
    elif source.scope == "ancestor":
        target = len(chain) - 1 - source.levels
    else:
        target = len(chain) - 1
    node = _climb_row(context, row, target, source, label)
    route = chain[target]
    for step in source.path:
        edge = _child_step(context.plan.schema.projection, route, step, "column path step")
        matches = [child for child in node.children if _node_name(child) == edge.expanded_name]
        if not matches:
            return None, route
        node = matches[0]
        route = context.plan.schema.projection.routes[edge.child_route_id]
    return node, route


def _column_value(
    context: _RowContext,
    binding: RelaxNGColumnBinding,
    row: Any,
) -> _SelectedValue:
    label = (
        f"column {binding.name!r} source {binding.source!r} "
        f"route {_route_label(binding.occurrence_route)} "
        f"declaration {_format_path(binding.declaration)}"
    )
    endpoint, route = _column_endpoint(context, binding, row, label)
    if endpoint is None:
        return _SelectedValue(None)
    if binding.source.attribute is not None:
        field = _attribute_field(route, binding.source.attribute)
        return _read_attribute_value(
            context, endpoint, field, binding.selected_contract, label=label
        )
    return _read_element_value(
        context,
        endpoint,
        binding.selected_contract,
        nullable=binding.nullable,
        label=label,
    )


def _default_route_value(
    context: _RowContext,
    node: Any,
    route: RouteContract,
    declaration: tuple[str, ...],
    selections: list[tuple[CandidateSelection, str]],
) -> Any:
    label = f"field {_format_path(declaration)} route {_route_label(route)}"
    if _is_primitive(route):
        contract = _single_contract(route.content.scalar_contracts, label)
        selected = _read_element_value(
            context,
            node,
            contract,
            nullable=_content_may_be_empty(route.content.model),
            label=label,
        )
        _record_selection(selections, selected, label)
        return selected.value
    return _default_record_value(context, node, route, declaration, selections)


def _default_record_value(
    context: _RowContext,
    node: Any,
    route: RouteContract,
    declaration: tuple[str, ...],
    selections: list[tuple[CandidateSelection, str]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for attribute in route.attributes.values():
        label = (
            f"field {_format_path((*declaration, '@' + attribute.expanded_name))} "
            f"route {_route_label(route)}"
        )
        contract = _single_contract(attribute.scalar_contracts, label)
        selected = _read_attribute_value(context, node, attribute, contract, label=label)
        result[attribute.alias] = selected.value
        _record_selection(selections, selected, label)
    if route.content.scalar_contracts:
        label = f"field {_format_path((*declaration, '$'))} route {_route_label(route)}"
        contract = _single_contract(route.content.scalar_contracts, label)
        selected = _read_element_value(
            context,
            node,
            contract,
            nullable=_content_may_be_empty(route.content.model),
            label=label,
        )
        result["$"] = selected.value
        _record_selection(selections, selected, label)
    grouped: dict[str, list[Any]] = {name: [] for name in route.children}
    for child in node.children:
        grouped[_node_name(child)].append(child)
    for field in route.children.values():
        label = (
            f"field {_format_path((*declaration, field.expanded_name))} route {_route_label(route)}"
        )
        children = grouped[field.expanded_name]
        child_route = context.plan.schema.projection.routes[cast(str, field.child_route_id)]
        if field.cardinality.repeatable:
            result[field.alias] = [
                _default_route_value(
                    context,
                    child,
                    child_route,
                    (*declaration, field.expanded_name),
                    selections,
                )
                for child in children
            ]
        elif children:
            result[field.alias] = _default_route_value(
                context,
                children[0],
                child_route,
                (*declaration, field.expanded_name),
                selections,
            )
        else:
            result[field.alias] = None
    return result


def _check_joint(
    context: _RowContext,
    selections: list[tuple[CandidateSelection, str]],
    row_index: int,
    *,
    kind: str,
) -> None:
    unique: dict[tuple[EvidenceOccurrence, str], CandidateSelection] = {}
    labels: dict[tuple[EvidenceOccurrence, str], list[str]] = {}
    for selection, label in selections:
        key = (selection.occurrence, selection.candidate_id)
        unique.setdefault(key, selection)
        labels.setdefault(key, []).append(label)
    if context.graph.compatible(unique.values()):
        return
    names = [name for group in labels.values() for name in group]
    raise IntegrationError(
        f"row {row_index} {kind} {', '.join(names)} conflict on the current accepting evidence"
    )


def _row_nodes(
    plan: RelaxNGProjectionPlan,
    document: Document,
    selector: str | None,
    namespaces: dict[str, str] | None,
) -> Iterator[tuple[int, Any]]:
    if selector is None:
        if len(plan.row_chain) > 1:
            raise IntegrationError(
                f"row route {_format_path(plan.row_path)} is local; provide an explicit selector"
            )
        candidates = [document.root]
        description = "document root"
    else:
        if not isinstance(selector, str) or not selector:
            raise IntegrationError("selector must be a nonempty ElementTree path")
        candidates = document.findall(selector, namespaces=namespaces)
        description = "selector node"
    for index, node in enumerate(candidates, 1):
        if not _node_matches_route(node, plan.route):
            actual = _node_route_id(node) or _node_name(node)
            raise IntegrationError(
                f"row {index} {description} {_node_name(node)!r} route {actual!r} "
                f"does not match configured row route {_route_label(plan.route)}"
            )
        yield index, node


def _iter_rows(
    plan: RelaxNGProjectionPlan,
    document: Document,
    selector: str | None,
    namespaces: dict[str, str] | None,
) -> Iterator[dict[str, Any]]:
    context = _context(plan, document)
    for index, node in _row_nodes(plan, document, selector, namespaces):
        selections: list[tuple[CandidateSelection, str]] = []
        if plan.mode == "columns":
            result: dict[str, Any] = {}
            for binding in plan.columns:
                try:
                    selected = _column_value(context, binding, node)
                except IntegrationError as exc:
                    raise IntegrationError(f"row {index}: {exc}") from exc
                result[binding.name] = selected.value
                _record_selection(
                    selections,
                    selected,
                    f"column {binding.name!r} source {binding.source!r}",
                )
            _check_joint(context, selections, index, kind="selected columns")
        else:
            try:
                value = _default_route_value(context, node, plan.route, plan.row_path, selections)
            except IntegrationError as exc:
                raise IntegrationError(f"row {index}: {exc}") from exc
            result = {"value": value} if _is_primitive(plan.route) else value
            _check_joint(context, selections, index, kind="selected fields")
        yield result


def iter_relaxng_rows(
    plan: RelaxNGProjectionPlan,
    document: Document,
    *,
    selector: str | None = None,
    namespaces: dict[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield Arrow-ready rows from one current, valid, typed document.

    The plan fixes row and column routes; a selector only chooses row nodes
    that already resolve to the compiled row route.  No row is yielded until
    every selected candidate survives one joint accepting derivation.
    """
    if not isinstance(plan, RelaxNGProjectionPlan):
        raise IntegrationError("iter_relaxng_rows requires a RelaxNGProjectionPlan")
    document = prepare_document(plan.schema, document, False)
    yield from _iter_rows(plan, document, selector, namespaces)


def _build_arrow_schema(plan: RelaxNGProjectionPlan) -> pa.Schema:
    pa = _pyarrow()
    if plan.mode == "columns":
        fields = [
            pa.field(
                binding.name,
                binding.arrow_shape,
                nullable=binding.nullable,
                metadata=_field_metadata(
                    binding.name,
                    binding.declaration,
                    plan.row_path,
                    binding.selected_contract,
                    binding.candidate,
                    binding.arrow_shape,
                    binding.arrow_fallback,
                    binding.occurrence_route.route_id,
                    binding.source,
                ),
            )
            for binding in plan.columns
        ]
    else:
        fields = list(plan.default_fields)
    return pa.schema(
        fields,
        metadata={
            b"pyxsd:projection": b"3",
            b"pyxsd:format": b"relaxng",
            b"pyxsd:mode": plan.mode.encode(),
            b"pyxsd:declaration": json.dumps(list(plan.row_path)).encode(),
            b"pyxsd:row": json.dumps(list(plan.row_path)).encode(),
            b"pyxsd:row_route": plan.route.route_id.encode(),
            b"pyxsd:grammar": plan.grammar_fingerprint.encode(),
            b"pyxsd:presence": (
                b"absent optional attributes and empty paths are null; repeated fields are lists"
            ),
            b"pyxsd:exactness": (b"selected contract value; no original lexical provenance"),
        },
    )


class RelaxNGRecordProjection:
    """A fixed RELAX NG Arrow schema over current accepting evidence."""

    def __init__(
        self,
        schema: RelaxNGSchema,
        *,
        element: str,
        path: tuple[str, ...] = (),
        columns: Mapping[str, FieldSource] | None = None,
    ) -> None:
        self._schema = schema
        self._plan = compile_relaxng_projection(schema, element=element, path=path, columns=columns)
        self.schema = _build_arrow_schema(self._plan)

    def batches(
        self,
        document: Document,
        *,
        selector: str | None = None,
        namespaces: dict[str, str] | None = None,
        batch_size: int = 10000,
        revalidate: bool = False,
    ) -> Iterator[Any]:
        """Yield bounded row batches, not XML streaming; keep input stable until closed."""
        if type(batch_size) is not int or batch_size <= 0:
            raise IntegrationError("batch_size must be a positive integer")
        document = prepare_document(self._schema, document, revalidate)
        rows: list[dict[str, Any]] = []
        index = 0
        for index, value in enumerate(self._rows(document, selector, namespaces), 1):
            rows.append(value)
            if len(rows) == batch_size:
                yield self._batch(rows, index)
                rows = []
        if rows:
            yield self._batch(rows, index)

    def _rows(
        self,
        document: Document,
        selector: str | None,
        namespaces: dict[str, str] | None,
    ) -> Iterator[dict[str, Any]]:
        yield from _iter_rows(self._plan, document, selector, namespaces)

    def _batch(self, rows: list[dict[str, Any]], last_row: int) -> Any:
        pa = _pyarrow()
        try:
            return pa.RecordBatch.from_pylist(rows, schema=self.schema)
        except (pa.ArrowException, ValueError, OverflowError) as exc:
            raise IntegrationError(f"Arrow batch ending at row {last_row}: {exc}") from exc

    def table(
        self,
        document: Document,
        *,
        selector: str | None = None,
        namespaces: dict[str, str] | None = None,
        revalidate: bool = False,
    ) -> Any:
        """Materialize the selected rows, including a fully typed empty table."""
        pa = _pyarrow()
        return pa.Table.from_batches(
            list(
                self.batches(
                    document, selector=selector, namespaces=namespaces, revalidate=revalidate
                )
            ),
            schema=self.schema,
        )

    def write_parquet(
        self,
        document: Document,
        destination: Any,
        *,
        selector: str | None = None,
        namespaces: dict[str, str] | None = None,
        batch_size: int = 10000,
        revalidate: bool = False,
    ) -> None:
        """Atomically replace local paths; borrowed sinks may retain partial output."""
        if type(batch_size) is not int or batch_size <= 0:
            raise IntegrationError("batch_size must be a positive integer")
        document = prepare_document(self._schema, document, revalidate)
        pq = _pyarrow_parquet()
        temporary: Path | None = None
        target: Path | None = None
        if isinstance(destination, (str, os.PathLike)):
            target = Path(destination)
            with tempfile.NamedTemporaryFile(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
            ) as file:
                temporary = Path(file.name)
            sink: Any = str(temporary)
        else:
            sink = destination
        try:
            with pq.ParquetWriter(
                sink, self.schema, version="2.6", use_compliant_nested_type=False
            ) as writer:
                for batch in self.batches(
                    document, selector=selector, namespaces=namespaces, batch_size=batch_size
                ):
                    writer.write_batch(batch)
            if target is not None and temporary is not None:
                os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def write_dataset(
        self,
        sources: Iterable[Any],
        destination: str | Any,
        *,
        selector: str | None = None,
        namespaces: dict[str, str] | None = None,
        batch_size: int = 10000,
        errors: Literal["raise", "report"] = "raise",
    ) -> Any:
        del sources, destination, selector, namespaces, batch_size, errors
        raise IntegrationError(_DATASET_EXTRACTION)
