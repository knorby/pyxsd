"""RELAX NG record column compilation and fixed Arrow schemas.

Rows are compiled from the schema-side route graph before any document exists.
This module deliberately constructs only Arrow schemas in this phase; evidence
backed row extraction is a separate step.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast

if TYPE_CHECKING:
    import pyarrow as pa

from pyxsd.document import Document
from pyxsd.integrations import IntegrationError
from pyxsd.integrations._shape import scalar_kind
from pyxsd.integrations.projection import FieldSource, field_source_metadata
from pyxsd.namespaces import clark, local_name
from pyxsd.relaxng.contracts import (
    ScalarContract,
    candidate_id,
    list_shape,
    scalar_contract,
)
from pyxsd.relaxng.datatypes import ScalarDatatype
from pyxsd.relaxng.patterns import List as PatternList
from pyxsd.relaxng.projection import (
    Cardinality,
    ContentContract,
    FieldContract,
    ProjectionPlan,
    RouteContract,
)
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.values import _list_token_plan

__all__ = [
    "RelaxNGColumnBinding",
    "RelaxNGProjectionPlan",
    "RelaxNGRecordProjection",
    "compile_relaxng_projection",
]

_ROW_EXTRACTION = "RELAX NG row extraction is not implemented by Task 5; Task 6 owns rows"


def _pyarrow() -> Any:
    """Import the optional backend only when an Arrow schema is requested."""
    try:
        import pyarrow as pyarrow_module
    except ModuleNotFoundError as exc:
        if exc.name != "pyarrow":
            raise
        raise ImportError("Install pyxsd[arrow] to use Arrow and Parquet projections") from exc
    return pyarrow_module


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
    row_chain: tuple[RouteContract, ...]
    columns: tuple[RelaxNGColumnBinding, ...]
    default_fields: tuple[Any, ...]
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
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise IntegrationError(f"start element {element!r} matches several projection roots")
    wildcard = [
        route
        for route in roots
        if route.expanded_name is None and route.name_class.contains(uri, local)
    ]
    if len(wildcard) == 1:
        return wildcard[0]
    if len(wildcard) > 1:
        raise IntegrationError(f"start element {element!r} matches several wildcard roots")
    raise IntegrationError(f"start element {element!r} is unknown in this RELAX NG grammar")


def _child_step(plan: ProjectionPlan, route: RouteContract, spelling: str, what: str) -> _RouteStep:
    uri, local = _qualify(spelling, what)
    expanded = clark(uri, local)
    field = route.children.get(expanded)
    if field is not None:
        if field.child_route_id is None:
            raise IntegrationError(f"child {expanded!r} has no compiled child route")
        return _RouteStep(
            field,
            field.cardinality,
            field.scalar_contracts,
            field.child_route_id,
            expanded,
        )
    wildcards = [
        wildcard
        for wildcard in route.wildcards
        if wildcard.role == "child" and wildcard.name_class.contains(uri, local)
    ]
    if len(wildcards) == 1:
        wildcard = wildcards[0]
        if wildcard.child_route_id is None:
            raise IntegrationError(f"child {expanded!r} has no compiled child route")
        return _RouteStep(
            None,
            wildcard.cardinality,
            wildcard.scalar_contracts,
            wildcard.child_route_id,
            expanded,
        )
    if len(wildcards) > 1:
        raise IntegrationError(f"{what} {spelling!r} matches several child wildcards")
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
        child = plan.routes.get(edge.child_route_id)
        if child is None:
            raise IntegrationError(f"row path step {step!r} has no compiled route")
        chain.append(child)
        names.append(edge.expanded_name)
        route = child
    return route, tuple(chain), tuple(names)


def _source_endpoint(
    plan: ProjectionPlan,
    row_chain: tuple[RouteContract, ...],
    row_names: tuple[str, ...],
    source: FieldSource,
) -> tuple[RouteContract, tuple[str, ...], bool, _RouteStep | None]:
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
    edge: _RouteStep | None = None
    for step in source.path:
        edge = _child_step(plan, chain[-1], step, "column path step")
        if edge.cardinality.repeatable:
            raise IntegrationError("downward column route is potentially repeated")
        child = plan.routes.get(edge.child_route_id)
        if child is None:
            raise IntegrationError(f"column path step {step!r} has no compiled route")
        if any(existing.route_id == child.route_id for existing in chain):
            raise IntegrationError("column path repeats a route already on its chain")
        if edge.cardinality.minimum == 0:
            nullable = True
        chain.append(child)
        names.append(edge.expanded_name)
    return chain[-1], tuple(names), nullable, edge


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
    pattern = plan.encoding_patterns.get(contract.pattern_identity)
    datatype = getattr(pattern, "datatype", None) if pattern is not None else None
    if not isinstance(datatype, ScalarDatatype):
        return pa.string(), "custom datatype has no Arrow-compatible XSD backing type"
    try:
        kind = scalar_kind(datatype.xsd_type)
    except IntegrationError as exc:
        return pa.string(), f"unsupported XSD backing type: {exc}"
    if kind == "bool":
        return pa.bool_(), None
    if kind == "int":
        return pa.int64(), None
    if kind == "decimal":
        constraints = getattr(datatype, "constraints", None)
        digits = getattr(constraints, "total_digits", None)
        scale = getattr(constraints, "fraction_digits", None)
        if (digits is not None and digits > 38) or (scale is not None and scale > 18):
            return pa.string(), "decimal precision exceeds Arrow decimal128(38, 18)"
        return pa.decimal128(38, 18), None
    if kind == "float":
        return pa.float64(), None
    if kind in ("base64", "hex"):
        return pa.binary(), None
    if kind in ("qname", "str"):
        return pa.string(), None
    if kind == "list":
        return pa.string(), "datatype-level list values have no static Arrow item type"
    return pa.string(), f"unsupported XSD scalar kind {kind!r}; using string"


def _contract_arrow(contract: ScalarContract, plan: ProjectionPlan) -> tuple[Any, str | None]:
    pa = _pyarrow()
    if contract.kind == "text":
        return pa.string(), None
    pattern = plan.encoding_patterns.get(contract.pattern_identity)
    if contract.kind != "list" or pattern is None:
        return _atomic_arrow(contract, plan)
    shape = list_shape(pattern)
    if shape == "interpretation":
        raise IntegrationError(
            "whole-list contract has no schema-static homogeneous or fixed shape"
        )
    plan_tokens = _list_token_plan(cast(PatternList, pattern))
    if plan_tokens is None:
        raise IntegrationError("whole-list contract has no derivable token contract")
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
    if route.route_id in stack:
        raise IntegrationError(
            f"default field path {'/'.join(path)} is recursive; Arrow cannot express "
            "an infinite struct"
        )
    _check_default_route(route)
    if _is_primitive(route):
        contract = _single_contract(
            route.content.scalar_contracts, f"default field path {'/'.join(path)}"
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
    _check_default_route(route)
    if _is_primitive(route):
        contract = _single_contract(
            route.content.scalar_contracts, f"default field path {'/'.join(row_path)}"
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
            f"default attribute {'/'.join((*path, attribute.expanded_name))}",
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
                ),
            )
        )
    if route.content.scalar_contracts:
        contract = _single_contract(
            route.content.scalar_contracts, f"default text path {'/'.join(path)}"
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
                ),
            )
        )
    for child in route.children.values():
        if child.child_route_id is None:
            raise IntegrationError(f"default child {child.expanded_name!r} has no compiled route")
        child_route = plan.routes.get(child.child_route_id)
        if child_route is None:
            raise IntegrationError(f"default child {child.expanded_name!r} has no route")
        child_path = (*path, child.expanded_name)
        child_shape, child_contract, child_fallback, _ = _default_element_shape(
            child_route, plan, grammar_fingerprint, child_path, row_path, stack
        )
        if child.cardinality.repeatable:
            data_type = pa.list_(pa.field("item", child_shape, nullable=False))
            nullable = False
            metadata_shape = data_type
        else:
            data_type = child_shape
            nullable = child.cardinality.minimum == 0
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
        if len(matches) > 1:
            raise IntegrationError(
                f"{description}: candidate_id {source.candidate_id!r} matches several contracts"
            )
        return matches[0]
    if not contracts:
        raise IntegrationError(f"{description} has no static scalar contract")
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
                current, names, nullable, edge = _source_endpoint(
                    plan, row_chain, row_names, source
                )
                if source.attribute is not None:
                    attribute = _attribute_field(current, source.attribute)
                    if attribute.cardinality.minimum == 0:
                        nullable = True
                    contracts = _distinct(attribute.scalar_contracts)
                    declaration = (*names, f"@{attribute.expanded_name}")
                else:
                    _check_endpoint(current)
                    contracts = _distinct(current.content.scalar_contracts)
                    if not contracts and edge is not None:
                        contracts = _distinct(edge.scalar_contracts)
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
            schema,
            route,
            row_chain,
            tuple(bindings),
            (),
            "columns",
            row_names,
            grammar_fingerprint,
        )
    route, row_chain, row_names = _resolve_row(plan, element, path)
    default_fields = _default_root_fields(route, plan, grammar_fingerprint, row_names)
    return RelaxNGProjectionPlan(
        schema,
        route,
        row_chain,
        (),
        default_fields,
        "default",
        row_names,
        grammar_fingerprint,
    )


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
            b"pyxsd:grammar": plan.grammar_fingerprint.encode(),
            b"pyxsd:presence": (
                b"absent optional attributes and empty paths are null; repeated fields are lists"
            ),
            b"pyxsd:exactness": (b"selected contract value; no original lexical provenance"),
        },
    )


class RelaxNGRecordProjection:
    """A fixed RELAX NG Arrow schema; row extraction belongs to a later task."""

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

    def _unimplemented(self) -> NoReturn:
        raise IntegrationError(_ROW_EXTRACTION)

    def batches(
        self,
        document: Document,
        *,
        selector: str | None = None,
        namespaces: dict[str, str] | None = None,
        batch_size: int = 10000,
        revalidate: bool = False,
    ) -> Iterator[Any]:
        del document, selector, namespaces, batch_size, revalidate
        self._unimplemented()

    def _rows(
        self,
        document: Document,
        selector: str | None,
        namespaces: dict[str, str] | None,
    ) -> Iterator[dict[str, Any]]:
        del document, selector, namespaces
        self._unimplemented()

    def table(
        self,
        document: Document,
        *,
        selector: str | None = None,
        namespaces: dict[str, str] | None = None,
        revalidate: bool = False,
    ) -> Any:
        del document, selector, namespaces, revalidate
        self._unimplemented()

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
        del document, destination, selector, namespaces, batch_size, revalidate
        self._unimplemented()

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
        self._unimplemented()
