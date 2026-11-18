"""Standalone Pydantic models for finite RELAX NG projection routes.

Models are built from the compiled :class:`~pyxsd.relaxng.projection.RouteContract`
graph before any instance XML exists.  A class belongs to one route, not to one
document path, so recursive routes reuse finite model classes and repeated
resolution returns the same class object.

Pydantic record input is an unordered data projection.  Every generated class
retains its compiled ``RouteContract`` and a truthful layout value as class
metadata through ``__relaxng_route__``, ``__relaxng_route_id__``,
``__relaxng_layout__``, ``__relaxng_scope__``, ``__relaxng_start__``,
``__relaxng_element__``, and ``__relaxng_input_order__``.  The model JSON
Schema repeats the route ID, layout, scope, and unordered-input statement.
Task 3 and Task 4 use that retained route, not field order or insertion order,
to reconstruct canonical lexical XML.  Root, ``$``, and attribute scalar
annotations come from :mod:`pyxsd.relaxng.pydantic_values`: unique decodable
contracts use ordinary Python values, while ambiguous or nondecodable
positions use codec-checked alternatives and scoped datatype context.  A
nonempty content contract still requires a supplied value, while an
empty-capable scalar uses ``None`` for real empty content and never invents an
empty lexical string.  Standalone structural validation reconstructs one
deterministic lexical tree from the retained compiled layout and requires a
native element pattern for the selected route to accept it; Pydantic field
checks alone never certify joint route validity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import GenericAlias, MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast
from xml.etree import ElementTree as ET

from pydantic import ValidationInfo
from pyxsd.exceptions import PyXSDError
from pyxsd.integrations import IntegrationError
from pyxsd.integrations.pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    _safe_name,
    clean_field_schema,
    create_model,
    model_validator,
)
from pyxsd.namespaces import clark, local_name
from pyxsd.relaxng.contracts import ScalarContract, scalar_contract
from pyxsd.relaxng.datatypes import DatatypeContext, UnsupportedDatatypeOperation
from pyxsd.relaxng.diagnostics import report_mismatches
from pyxsd.relaxng.json_values import parse_scalar_tag
from pyxsd.relaxng.names import ExceptName, Name, NameChoice, NameClass, NsName
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Data,
    DataExcept,
    Element,
    Group,
    Interleave,
    OneOrMore,
    Pattern,
    Ref,
    Text,
    Value,
)
from pyxsd.relaxng.patterns import (
    List as PatternList,
)
from pyxsd.relaxng.projection import (
    Cardinality,
    ContentContract,
    ProjectionPlan,
    RouteContract,
)
from pyxsd.relaxng.pydantic_values import (
    ScalarCodec,
    context_from_mapping,
    model_dump_json_with_scalar_wrappers,
    model_dump_with_scalar_wrappers,
    relaxng_scalar_annotation,
)
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.tree import NodeContext, RelaxNGElement
from pyxsd.relaxng.validator import validate_element_recovering
from pyxsd.relaxng.values import AlternativeValue, ListInterpretation, encode_scalar
from pyxsd.validation import ValidationReport
from pyxsd.xsd_data_types import QName

if TYPE_CHECKING:
    from pyxsd.relaxng.json_profile import ImportedTree

__all__ = ["RelaxNGModelSet", "route_to_lexical", "validate_route_data"]

#: Validator names start with an underscore, which ``_safe_name`` never emits.
_CHILD_PRESENCE_VALIDATOR = "__pyxsd_relaxng_child_presence__"
_ROUTE_VALIDATION_VALIDATOR = "__pyxsd_relaxng_route_validation__"

_SCALAR_PATTERNS = (Text, Data, DataExcept, Value, PatternList)

#: One JSON Schema description of a schema-static scalar contract.
ContractMetadata = dict[str, Any]


def _contract_metadata(contract: ScalarContract) -> ContractMetadata:
    """Render one contract deterministically for field metadata.

    The complete public contract is retained, not just a display name, so the
    scalar phase can replace annotations without consulting path or evidence
    state that these models deliberately do not retain.
    """
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
    contracts: tuple[ScalarContract, ...],
    cardinality: Cardinality,
    *,
    child_route_id: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "x-pyxsd-contracts": [_contract_metadata(contract) for contract in contracts],
        "x-pyxsd-cardinality": {
            "minimum": cardinality.minimum,
            "maximum": cardinality.maximum,
        },
    }
    if child_route_id is not None:
        metadata["x-pyxsd-child-route"] = child_route_id
    return metadata


def _route_metadata(plan: ProjectionPlan, route: RouteContract, layout: str) -> dict[str, Any]:
    start = route.route_id in plan.roots
    if start:
        validation = (
            "unordered projected data for this start route; Task 3/4 reconstruct "
            "canonical lexical XML from the retained compiled route; use "
            "RelaxNGDocument validation for XML validity"
        )
    else:
        validation = (
            "unordered local route content only; ancestor and sibling constraints "
            "are not validated; Task 3/4 reconstruct canonical lexical XML from the "
            "retained compiled route; use RelaxNGDocument validation for XML validity"
        )
    return {
        "x-pyxsd-route": route.route_id,
        "x-pyxsd-layout": layout,
        "x-pyxsd-element": route.expanded_name,
        "x-pyxsd-scope": "start" if start else "local",
        "x-pyxsd-input-order": "unordered",
        "x-pyxsd-validation": validation,
    }


def _route_label(route: RouteContract) -> str:
    name = route.expanded_name if route.expanded_name is not None else "an open-name element"
    return f"{name} ({route.route_id})"


def _child_content(model: ContentContract) -> ContentContract | None:
    """Erase attribute-only content and collapse one-operand ordering nodes."""
    results: dict[int, ContentContract | None] = {}
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
        if node.kind in ("attribute", "empty"):
            results[id(node)] = None
            continue
        operands = tuple(
            part for operand in node.operands if (part := results[id(operand)]) is not None
        )
        if node.kind in ("group", "interleave") and len(operands) == 1:
            result = operands[0]
        elif node.operands and not operands:
            result = None
        else:
            result = ContentContract(node.kind, operands)
        results[id(node)] = result
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


def _flat_interleave(model: ContentContract) -> bool:
    """Whether a content tree is only flat singular child structure.

    Singular optional choices (``empty`` or one child) and nested interleaves
    remain order-immaterial.  Groups and repeats introduce order or multiplicity
    and therefore are not flat.
    """
    if model.kind in ("child", "empty"):
        return True
    if model.kind == "choice":
        return all(_flat_interleave(operand) for operand in model.operands)
    if model.kind != "interleave":
        return False
    return all(_flat_interleave(operand) for operand in model.operands)


def _route_layout(route: RouteContract) -> str:
    """Describe the retained child layout without reading instance content.

    ``record`` is an interleave whose operands are only flat singular child
    elements, whose relative order is immaterial.  ``interleaved`` is an
    interleave containing a repeatable child or nested ordered/choice
    structure, where canonical reconstruction still needs the compiled order.
    ``ordered`` is a group/sequence.  Input through Pydantic is always
    unordered; this value only records what the retained route requires.
    """
    if _content_cannot_match(route.content.model):
        return "generic"
    if route.content.scalar_contracts:
        return "scalar"
    if route.generic_reason is not None or route.wildcards or route.content.mixed:
        return "generic"
    model = _child_content(route.content.model)
    if model is None:
        return "empty"
    children = tuple(route.children.values())
    if len(children) == 1 and children[0].cardinality.repeatable:
        return "repeated"
    if model.kind == "choice":
        return "choice"
    if model.kind == "repeat":
        return "repeated"
    if model.kind == "interleave":
        if _flat_interleave(model) and not any(field.cardinality.repeatable for field in children):
            return "record"
        return "interleaved"
    return "ordered"


def _content_may_be_empty(model: ContentContract) -> bool:
    """Whether the compiled content admits no scalar occurrence.

    Attributes never contribute element character content.  A text scalar is
    nullable even though atomic and list scalars are not; this is the scalar
    presence distinction Task 1 must preserve as ``None`` rather than ``""``.
    """
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


def _scalar_annotation(
    contracts: tuple[ScalarContract, ...],
    *,
    codec: ScalarCodec,
    may_be_empty: bool,
) -> Any:
    return relaxng_scalar_annotation(
        contracts,
        codec=codec,
        may_be_empty=may_be_empty,
    )


def _root_scalar_annotation(
    contracts: tuple[ScalarContract, ...],
    *,
    codec: ScalarCodec,
    may_be_empty: bool,
) -> Any:
    return relaxng_scalar_annotation(
        contracts,
        codec=codec,
        may_be_empty=may_be_empty,
    )


def _attach_route(
    model: type[BaseModel], plan: ProjectionPlan, route: RouteContract, layout: str
) -> None:
    """Attach route identity and retained layout to the generated class."""
    scope = "start" if route.route_id in plan.roots else "local"
    target = cast(Any, model)
    target.__relaxng_route__ = route
    target.__relaxng_route_id__ = route.route_id
    target.__relaxng_layout__ = layout
    target.__relaxng_scope__ = scope
    target.__relaxng_start__ = scope == "start"
    target.__relaxng_element__ = route.expanded_name
    target.__relaxng_input_order__ = "unordered"
    target.model_dump = model_dump_with_scalar_wrappers
    target.model_dump_json = model_dump_json_with_scalar_wrappers


def _model_stem(route: RouteContract) -> str:
    """Return a deterministic class stem without a grammar or route digest."""
    local = "open_name" if route.expanded_name is None else route.expanded_name.rsplit("}", 1)[-1]
    return "RelaxNGModel_" + re.sub(r"[^0-9A-Za-z_]", "_", local)


def _model_names(plan: ProjectionPlan) -> dict[str, str]:
    used: set[str] = set()
    names: dict[str, str] = {}
    for route in plan.routes.values():
        base = _model_stem(route)
        name = base
        index = 2
        while name in used:
            name = f"{base}_{index}"
            index += 1
        used.add(name)
        names[route.route_id] = name
    return names


def _parse_name(value: object, *, what: str) -> tuple[str | None, str]:
    """Parse a local, Clark, or EQName spelling into an expanded component.

    Prefix-form EQNames need a namespace context that this API does not accept,
    so they are rejected rather than resolved against process state.
    """
    if not isinstance(value, str) or not value:
        raise IntegrationError(f"{what} must be a nonempty element name")
    if value.startswith(("Q{", "{")):
        body = value[2:] if value.startswith("Q{") else value[1:]
        uri, separator, local = body.partition("}")
        if not separator or not local or "{" in local or "}" in local:
            raise IntegrationError(f"{what} {value!r}: malformed Clark or EQName")
        return uri or None, local
    if ":" in value:
        raise IntegrationError(f"{what} {value!r}: prefix-form EQName requires a namespace context")
    if "}" in value:
        raise IntegrationError(f"{what} {value!r}: malformed element name")
    return None, value


def _is_none(value: Any) -> bool:
    """Field exclusion predicate for omitted optional attributes/children."""
    return value is None


def _field_info(
    alias: str,
    *,
    metadata: dict[str, Any],
    cardinality: Cardinality,
    omit_none: bool = False,
) -> Any:
    repeatable = cardinality.repeatable
    options: dict[str, Any] = {
        "alias": alias,
        "json_schema_extra": metadata,
    }
    if omit_none:
        options["exclude_if"] = _is_none
    if repeatable:
        options["min_length"] = cardinality.minimum
        options["max_length"] = cardinality.maximum
    if cardinality.minimum > 0:
        options["default"] = ...
    elif repeatable:
        options["default_factory"] = list
    else:
        options["default"] = None
    return Field(**options)


def _check_route_supported(route: RouteContract) -> None:
    """Reject route shapes a flat Pydantic record cannot represent."""
    if route.generic_reason is not None:
        raise IntegrationError(
            f"RELAX NG route {_route_label(route)} is generic: "
            f"{route.generic_reason}; Pydantic fields cannot represent this "
            "content without losing required XML information"
        )
    if _content_cannot_match(route.content.model):
        raise IntegrationError(
            f"RELAX NG route {_route_label(route)} cannot match any document: "
            "its content is notAllowed"
        )
    if route.content.mixed:
        raise IntegrationError(
            f"RELAX NG route {_route_label(route)} has mixed content; Pydantic "
            "record fields cannot preserve text and child order"
        )
    if route.wildcards:
        roles = " and ".join(sorted({wildcard.role for wildcard in route.wildcards}))
        raise IntegrationError(
            f"RELAX NG route {_route_label(route)} has {roles} wildcard content; "
            "arbitrary XML names cannot be represented as fixed Pydantic fields "
            "without losing required XML structure"
        )


def _pattern_operands(node: Pattern) -> tuple[Pattern, ...]:
    if isinstance(node, Ref):
        return (node.target,)
    if isinstance(node, (Choice, Group, Interleave)):
        return (node.first, node.second)
    if isinstance(node, (Element, Attribute, OneOrMore, PatternList)):
        return (node.pattern,)
    if isinstance(node, DataExcept):
        return (node.excluded,)
    return ()


def _encoding_patterns(route: RouteContract) -> dict[str, Pattern]:
    """Index the scalar encoding patterns reachable from one route.

    The plan's registry already rejects conflicting encoders for one contract
    identity, so any matching reachable node is the authoritative encoder.
    """
    patterns: dict[str, Pattern] = {}
    seen: set[int] = set()
    pending: list[Pattern] = list(route.patterns)
    while pending:
        node = pending.pop()
        key = id(node)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(node, _SCALAR_PATTERNS):
            contract = scalar_contract(node)
            patterns.setdefault(contract.pattern_identity, node)
        pending.extend(_pattern_operands(node))
    return patterns


def _route_witness_name(route: RouteContract) -> str:
    """Choose a deterministic concrete name admitted by an open name class.

    Open-name routes have no single element spelling.  Local standalone
    validation still needs an expanded name for the fabricated route element,
    so this witness is internal and is never exposed as a document root.
    """
    mentioned_namespaces: set[str | None] = {None}
    mentioned_locals: set[str] = set()
    pending: list[NameClass] = [route.name_class]
    while pending:
        name_class = pending.pop()
        if isinstance(name_class, (Name, NsName)):
            mentioned_namespaces.add(name_class.uri)
            if isinstance(name_class, Name):
                mentioned_locals.add(name_class.local)
        elif isinstance(name_class, NameChoice):
            pending.extend(reversed(name_class.alternatives))
        elif isinstance(name_class, ExceptName):
            pending.extend((name_class.base, name_class.excluded))
    namespaces = [
        *sorted({str(uri) for uri in mentioned_namespaces if uri is not None}),
        "urn:pyxsd:relaxng:route-witness",
    ]
    fresh_local = "pyxsd-route"
    while fresh_local in mentioned_locals:
        fresh_local += "_"
    locals_ = [*sorted(mentioned_locals), fresh_local]
    for uri in (None, *namespaces):
        for local in locals_:
            if route.name_class.contains(uri, local):
                return clark(uri, local)
    raise IntegrationError(
        f"RELAX NG route {_route_label(route)} has no deterministic witness element name"
    )


def _model_route(model_class: type[BaseModel]) -> RouteContract:
    route = getattr(model_class, "__relaxng_route__", None)
    if not isinstance(route, RouteContract):
        raise IntegrationError(f"{model_class.__name__} is not a generated RELAX NG route model")
    return route


def _model_values(route: RouteContract, model: BaseModel) -> dict[str, Any]:
    """Return one generated model's canonical field-alias values."""
    model_class = type(model)
    model_route = getattr(model_class, "__relaxng_route__", None)
    if not isinstance(model_route, RouteContract):
        raise IntegrationError(f"{model_class.__name__} is not a generated RELAX NG route model")
    if model_route.route_id != route.route_id:
        raise IntegrationError(
            f"model route {model_route.route_id} does not match requested route {route.route_id}"
        )
    if getattr(model_class, "__pydantic_root_model__", False):
        return {"$": cast(Any, model).root}
    values: dict[str, Any] = {}
    for name, field_info in model_class.model_fields.items():
        alias = field_info.alias or name
        values[alias] = getattr(model, name)
    return values


@dataclass
class _FieldState:
    """One route field's unread model values in canonical alias order."""

    alias: str
    role: Literal["scalar", "attribute", "child"]
    values: tuple[Any, ...]
    used: int = 0

    @property
    def remaining(self) -> int:
        return len(self.values) - self.used


def _route_states(route: RouteContract, data: Mapping[str, Any]) -> dict[str, _FieldState]:
    """Map canonical aliases to the model values this route can emit."""
    states: dict[str, _FieldState] = {}
    expected: set[str] = set()
    if route.content.scalar_contracts:
        expected.add("$")
        value = data.get("$")
        states["$"] = _FieldState("$", "scalar", () if value is None else (value,))
    for name in route.attributes:
        alias = "@" + name
        expected.add(alias)
        value = data.get(alias)
        states[alias] = _FieldState(alias, "attribute", () if value is None else (value,))
    for name, field in route.children.items():
        expected.add(name)
        value = data.get(name)
        if value is None:
            values: tuple[Any, ...] = ()
        elif field.cardinality.repeatable:
            if not isinstance(value, (list, tuple)):
                raise ValueError(
                    f"RELAX NG route {_route_label(route)} field {name!r}: "
                    "a repeated child requires a list"
                )
            values = tuple(value)
        else:
            values = (value,)
        states[name] = _FieldState(name, "child", values)
    unknown = sorted(set(data) - expected)
    if unknown:
        raise ValueError(f"RELAX NG route {_route_label(route)}: unknown field(s) {unknown!r}")
    return states


def _value_contexts(value: Any) -> tuple[DatatypeContext, ...]:
    """Collect scoped datatype contexts carried by one normalized value."""
    result: list[DatatypeContext] = []
    pending = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, (AlternativeValue, ListInterpretation)):
            result.append(item.context)
            continue
        if isinstance(item, QName):
            if item._resolved_:
                if item._prefix_:
                    if item._uri_:
                        result.append(DatatypeContext(None, {item._prefix_: item._uri_}))
                elif item._uri_:
                    result.append(DatatypeContext(None, {"": item._uri_}))
            continue
        if isinstance(item, Mapping):
            if "#value" in item:
                try:
                    claim = parse_scalar_tag(dict(item), "")
                except PyXSDError as error:
                    raise ValueError(str(error)) from error
                result.append(claim.context)
            continue
        if isinstance(item, (list, tuple)):
            if id(item) in seen:
                continue
            seen.add(id(item))
            pending.extend(reversed(item))
    return tuple(result)


def _merge_contexts(
    inherited: DatatypeContext,
    contexts: tuple[DatatypeContext, ...],
    *,
    path: tuple[str, ...],
) -> DatatypeContext:
    """Merge scoped value contexts into the element's inherited frame."""
    namespaces = dict(inherited.namespaces)
    base_uri = inherited.base_uri
    for context in contexts:
        if context.base_uri is not None:
            if base_uri is None:
                base_uri = context.base_uri
            elif base_uri != context.base_uri:
                raise ValueError(
                    f"RELAX NG route path {'/'.join(path) or '/'}: scalar contexts "
                    f"declare conflicting base URIs {base_uri!r} and {context.base_uri!r}"
                )
        for prefix, uri in context.namespaces.items():
            existing = namespaces.get(prefix)
            if existing is not None and existing != uri:
                raise ValueError(
                    f"RELAX NG route path {'/'.join(path) or '/'}: scalar contexts "
                    f"declare conflicting namespace bindings for {prefix!r}"
                )
            namespaces[prefix] = uri
    return DatatypeContext(base_uri, namespaces)


def _encode_scalar_value(
    value: Any,
    contracts: tuple[ScalarContract, ...],
    context: DatatypeContext,
    patterns: Mapping[str, Pattern],
    *,
    path: tuple[str, ...],
    normalized: bool,
) -> str:
    """Encode one field value through explicit scalar encoders.

    Model values have already been normalized: a decoded string is re-encoded
    through its unique contract so decoder/encoder pairs retain their value
    space.  A bare mapping value is independent input, where a string is
    lexical evidence and never reverse-engineered into a datatype choice.
    """
    location = "/".join(path) or "/"
    if isinstance(value, (AlternativeValue, ListInterpretation)):
        return value.lexical
    if isinstance(value, Mapping) and "#value" in value:
        try:
            claim = parse_scalar_tag(dict(value), "")
        except PyXSDError as error:
            raise ValueError(f"RELAX NG route path {location}: {error}") from error
        return claim.lexical
    if isinstance(value, str) and (
        not normalized or len(contracts) != 1 or contracts[0].kind == "text"
    ):
        return value
    if len(contracts) != 1:
        raise ValueError(
            f"RELAX NG route path {location}: an ambiguous scalar position requires "
            "a lexical string, an exact #value tag, or an AlternativeValue snapshot; "
            f"got {type(value).__name__}"
        )
    contract = contracts[0]
    pattern = patterns.get(contract.pattern_identity)
    if pattern is None:
        raise IntegrationError(
            f"RELAX NG route path {location}: no compiled encoding pattern for "
            f"scalar contract {contract.pattern_identity!r}"
        )
    try:
        return encode_scalar(contract, value, context, pattern=pattern)
    except (TypeError, ValueError, UnsupportedDatatypeOperation) as error:
        raise ValueError(f"RELAX NG route path {location}: {error}") from error


class _Renderer:
    """One deterministic route tree assembled from persisted field values."""

    def __init__(
        self,
        route: RouteContract,
        states: dict[str, _FieldState],
        context: DatatypeContext,
        *,
        normalized: bool,
    ) -> None:
        self.route = route
        self.states = states
        self.context = context
        self.normalized = normalized
        self.contexts: dict[int, DatatypeContext] = {}
        self.encoding: dict[str, dict[str, Pattern]] = {}
        self.aliases: dict[int, frozenset[str]] = {}

    def render(self) -> tuple[ET.Element, dict[int, DatatypeContext]]:
        name = self.route.expanded_name or _route_witness_name(self.route)
        root = self._element(name, self.route, self.states, self.context, (name,))
        return root, self.contexts

    def _patterns(self, route: RouteContract) -> dict[str, Pattern]:
        cached = self.encoding.get(route.route_id)
        if cached is None:
            cached = _encoding_patterns(route)
            self.encoding[route.route_id] = cached
        return cached

    def _content_aliases(self, node: ContentContract) -> frozenset[str]:
        cached = self.aliases.get(id(node))
        if cached is not None:
            return cached
        result: set[str] = set()
        if node.kind == "scalar":
            result.add("$")
        elif node.kind in ("attribute", "child"):
            prefix = "@" if node.kind == "attribute" else ""
            result.update(
                prefix + reference.expanded_name
                for reference in node.fields
                if reference.expanded_name is not None
            )
        else:
            for operand in node.operands:
                result.update(self._content_aliases(operand))
        frozen = frozenset(result)
        self.aliases[id(node)] = frozen
        return frozen

    def _remaining(self, node: ContentContract) -> int:
        return sum(
            self.states[alias].remaining
            for alias in self._content_aliases(node)
            if alias in self.states
        )

    def _has_remaining(self, node: ContentContract) -> bool:
        return self._remaining(node) > 0

    def _element(
        self,
        name: str,
        route: RouteContract,
        states: dict[str, _FieldState],
        inherited: DatatypeContext,
        path: tuple[str, ...],
    ) -> ET.Element:
        scalar_states = [states["$"]] if "$" in states else []
        scalar_states.extend(
            states["@" + attribute] for attribute in route.attributes if "@" + attribute in states
        )
        value_contexts: list[DatatypeContext] = []
        for state in scalar_states:
            for value in state.values:
                value_contexts.extend(_value_contexts(value))
        context = _merge_contexts(inherited, tuple(value_contexts), path=path)
        element = ET.Element(name)
        self._emit(route.content.model, element, states, context, route, path, iteration=False)
        leftovers = sorted(alias for alias, state in states.items() if state.remaining > 0)
        if leftovers:
            raise ValueError(
                f"RELAX NG route {_route_label(route)} path {'/'.join(path)}: "
                f"field(s) {leftovers!r} do not belong to one compiled alternative"
            )
        self.contexts[id(element)] = context
        return element

    def _emit(
        self,
        node: ContentContract,
        element: ET.Element,
        states: dict[str, _FieldState],
        context: DatatypeContext,
        route: RouteContract,
        path: tuple[str, ...],
        *,
        iteration: bool,
    ) -> None:
        kind = node.kind
        if kind == "empty" or kind == "notAllowed":
            return
        if kind == "attribute":
            patterns = self._patterns(route)
            for reference in node.fields:
                alias = "@" + cast(str, reference.expanded_name)
                state = states[alias]
                assert state.remaining > 0
                lexical = _encode_scalar_value(
                    state.values[state.used],
                    node.scalar_contracts,
                    context,
                    patterns,
                    path=(*path, alias),
                    normalized=self.normalized,
                )
                element.set(cast(str, reference.expanded_name), lexical)
                state.used += 1
            return
        if kind == "child":
            for reference in node.fields:
                alias = cast(str, reference.expanded_name)
                state = states[alias]
                if state.remaining == 0:
                    continue
                value = state.values[state.used]
                state.used += 1
                element.append(
                    self._child(
                        alias,
                        reference.child_route_id,
                        value,
                        context,
                        (*path, alias),
                    )
                )
            return
        if kind == "scalar":
            patterns = self._patterns(route)
            state = states["$"]
            if state.remaining == 0:
                return
            element.text = _encode_scalar_value(
                state.values[state.used],
                node.scalar_contracts,
                context,
                patterns,
                path=(*path, "$"),
                normalized=self.normalized,
            )
            state.used += 1
            return
        if kind in ("group", "interleave"):
            for operand in node.operands:
                self._emit(operand, element, states, context, route, path, iteration=iteration)
            return
        if kind == "repeat":
            body = node.operands[0]
            while self._has_remaining(body):
                before = self._remaining(body)
                self._emit(body, element, states, context, route, path, iteration=True)
                assert self._remaining(body) < before
            return
        if kind == "choice":
            present = {
                alias
                for alias in self._content_aliases(node)
                if alias in states and states[alias].remaining
            }
            if iteration:
                chosen: ContentContract | None = next(
                    (operand for operand in node.operands if self._has_remaining(operand)),
                    node.operands[0],
                )
            else:
                chosen = next(
                    (
                        operand
                        for operand in node.operands
                        if present <= self._content_aliases(operand)
                    ),
                    None,
                )
                if chosen is None:
                    raise ValueError(
                        f"RELAX NG route {_route_label(route)} path {'/'.join(path)}: "
                        f"field(s) {sorted(present)!r} belong to different compiled "
                        "alternatives"
                    )
            assert chosen is not None
            self._emit(chosen, element, states, context, route, path, iteration=iteration)
            return
        raise IntegrationError(
            f"RELAX NG route {_route_label(route)} path {'/'.join(path)}: "
            f"unknown content contract {kind!r}"
        )

    def _child(
        self,
        name: str,
        child_route_id: str | None,
        value: Any,
        inherited: DatatypeContext,
        path: tuple[str, ...],
    ) -> ET.Element:
        if not isinstance(value, BaseModel):
            raise IntegrationError(
                f"RELAX NG route path {'/'.join(path)}: nested child data must be "
                "a generated RELAX NG route model"
            )
        child_route = _model_route(type(value))
        if child_route_id is not None and child_route.route_id != child_route_id:
            raise IntegrationError(
                f"RELAX NG route path {'/'.join(path)}: child route "
                f"{child_route.route_id} does not match field route {child_route_id}"
            )
        states = _route_states(child_route, _model_values(child_route, value))
        renderer = _Renderer(child_route, states, inherited, normalized=True)
        element = renderer._element(name, child_route, states, inherited, path)
        self.contexts.update(renderer.contexts)
        return element


def _route_tree(
    route: RouteContract,
    data: Mapping[str, Any],
    context: object | None,
    *,
    normalized: bool,
) -> tuple[ET.Element, dict[int, DatatypeContext]]:
    """Build one complete deterministic element tree for *route*."""
    inherited = context_from_mapping(context)
    states = _route_states(route, data)
    return _Renderer(route, states, inherited, normalized=normalized).render()


def _relaxng_root(tree: ImportedTree) -> RelaxNGElement:
    """Adapt one constructed tree and its identity-keyed contexts."""
    root = RelaxNGElement.from_etree(tree.root)
    pending: list[tuple[ET.Element, RelaxNGElement]] = [(tree.root, root)]
    while pending:
        source, node = pending.pop()
        context = tree.contexts[id(source)]
        current = node.revision.context_for(node)
        node.revision._replace_context(
            node,
            NodeContext(context.namespaces, context.base_uri, current.source),
        )
        pending.extend(zip(list(source), node.children, strict=True))
    return root


def _native_path(mismatch: Any) -> str:
    parts: list[str] = []
    node = mismatch.element
    while node is not None:
        parts.append(node._name_ or "")
        node = node.parent
    parts.reverse()
    if mismatch.event == "attribute" and mismatch.name is not None:
        parts[-1] = f"{parts[-1]}/@{mismatch.name}"
    elif mismatch.event in ("text", "weak_text"):
        parts[-1] = f"{parts[-1]}/text()"
    return "/" + "/".join(parts)


def _validate_route_tree(
    route: RouteContract,
    tree: tuple[ET.Element, dict[int, DatatypeContext]],
) -> None:
    """Require at least one route assignment to accept the produced tree."""
    if not route.patterns:
        raise IntegrationError(
            f"RELAX NG route {_route_label(route)} retains no compiled element patterns"
        )
    root = _relaxng_root(_imported_tree(*tree))
    first: Any = None
    for pattern in route.patterns:
        outcome = validate_element_recovering(root, pattern)
        if outcome.valid:
            return
        if first is None:
            first = outcome
    report = ValidationReport(diagnostics_complete=False)
    report_mismatches(first, report)
    issue = report.errors[0]
    mismatch = first.mismatches[0]
    raise ValueError(
        f"RELAX NG route {_route_label(route)} at {_native_path(mismatch)}: "
        f"{issue.code}: {issue.message}"
    )


def _imported_tree(root: ET.Element, contexts: dict[int, DatatypeContext]) -> ImportedTree:
    from pyxsd.relaxng.json_profile import ImportedTree

    return ImportedTree(root, MappingProxyType(dict(contexts)), ())


def validate_route_data(
    route: RouteContract | type[BaseModel],
    data: Mapping[str, Any] | BaseModel,
    *,
    context: object | None = None,
) -> Mapping[str, Any]:
    """Validate independent data against one route's compiled alternatives.

    A :class:`~pyxsd.relaxng.projection.RouteContract` accepts canonical
    field-alias data whose nested children are generated route models.  A
    generated route model class also accepts raw mapping input and validates it
    through Pydantic first, including every nested child.  Validation builds
    one deterministic lexical tree from the compiled layout and requires at
    least one retained native element pattern to accept it.  The returned
    mapping uses canonical aliases and never mutates *data* or *context*.
    """
    model_class: type[BaseModel] | None = None
    if isinstance(route, type) and issubclass(route, BaseModel):
        model_class = route
        route = _model_route(route)
    if not isinstance(route, RouteContract):
        raise TypeError("route must be a RouteContract or a generated RELAX NG model class")
    _check_route_supported(route)
    payload: Mapping[str, Any] | BaseModel = data
    if model_class is not None and not isinstance(payload, model_class):
        payload = model_class.model_validate(payload, context=context)
    if isinstance(payload, BaseModel):
        values = _model_values(route, payload)
        normalized = True
    elif isinstance(payload, Mapping):
        values = dict(payload)
        normalized = False
    else:
        raise ValueError(f"RELAX NG route {_route_label(route)}: data must be a mapping")
    tree = _route_tree(route, values, context, normalized=normalized)
    _validate_route_tree(route, tree)
    return values


def route_to_lexical(
    route: RouteContract,
    model: BaseModel | Mapping[str, Any],
    *,
    context: object | None = None,
) -> ImportedTree:
    """Build one deterministic lexical route tree from validated model data.

    Every constructed element carries its complete scoped datatype context.
    Order follows the retained compiled content model; repeated values repeat,
    optional absences are omitted, and singular values appear once.  Open-name
    routes have no concrete element spelling and raise ``IntegrationError``
    rather than fabricating a global root.
    """
    if not isinstance(route, RouteContract):
        raise TypeError("route must be a RouteContract")
    if isinstance(model, BaseModel):
        values = _model_values(route, model)
        normalized = True
    elif isinstance(model, Mapping):
        values = dict(model)
        normalized = False
    else:
        raise ValueError(f"RELAX NG route {_route_label(route)}: model data must be a mapping")
    _check_route_supported(route)
    if route.expanded_name is None:
        raise IntegrationError(
            f"RELAX NG route {_route_label(route)} has an open name class; it has "
            "no concrete element name for lexical reconstruction"
        )
    root, contexts = _route_tree(route, values, context, normalized=normalized)
    return _imported_tree(root, contexts)


def _route_after_validator(route: RouteContract) -> Any:
    """Build the model-level native route validator for one retained route."""

    def validate_route(value: BaseModel, info: ValidationInfo) -> BaseModel:
        validate_route_data(route, value, context=getattr(info, "context", None))
        return value

    return validate_route


class RelaxNGModelSet:
    """A compiled-RELAX-NG-schema-scoped registry of route Pydantic models.

    The registry is built from :attr:`RelaxNGSchema.projection` only.  It never
    reads instance XML, pattern capture, or binding evidence, so models exist
    before the first document is parsed and cannot inherit instance-specific
    route choices.
    """

    def __init__(self, schema: RelaxNGSchema) -> None:
        if not isinstance(schema, RelaxNGSchema):
            raise TypeError("RelaxNGModelSet requires a RelaxNGSchema")
        schema.require_valid()
        self.schema = schema
        self._plan = schema.projection
        self._codec = ScalarCodec(schema, self._plan, schema._grammar_fingerprint)
        self._names = _model_names(self._plan)
        self._models: dict[str, type[BaseModel]] = {}

    def model_for(self, *, element: str, path: tuple[str, ...] = ()) -> type[BaseModel]:
        """Return the one cached model class for an admitted route.

        ``element`` accepts a local name, a Clark name (``{uri}local``), or an
        EQName (``Q{uri}local``).  Each ``path`` step is an exact expanded child
        name, or one uniquely matching child wildcard.  Unknown and ambiguous
        resolutions fail with :class:`~pyxsd.integrations.IntegrationError`.
        """
        route = self._resolve(element=element, path=path)
        return self._model(route)

    def _resolve(self, *, element: str, path: tuple[str, ...]) -> RouteContract:
        if not isinstance(path, tuple) or any(
            not isinstance(step, str) or not step for step in path
        ):
            raise IntegrationError("model route path must be a tuple of nonempty element names")
        uri, local = _parse_name(element, what="element")
        route = self._start_route(element, uri, local)
        for index in range(len(path)):
            route = self._child_route(route, element=element, path=path[: index + 1])
        return route

    def _start_route(self, element: str, uri: str | None, local: str) -> RouteContract:
        matches = [
            route_id
            for route_id in self._plan.roots
            if self._plan.routes[route_id].name_class.contains(uri, local)
        ]
        if not matches and uri is None:
            # A local spelling directly matches any start name class that
            # admits (None, local), including an AnyName wildcard.  If no class
            # admits it, a uniquely named start route in a namespace may still
            # be selected by local name.
            matches = [
                route_id
                for route_id in self._plan.roots
                if (expanded := self._plan.routes[route_id].expanded_name) is not None
                and local_name(expanded) == local
            ]
        if not matches:
            raise IntegrationError(f"RELAX NG start route {element!r}: no admitted start route")
        if len(matches) > 1:
            raise IntegrationError(
                f"RELAX NG start route {element!r}: ambiguous; use a Clark or "
                "EQName spelling with the namespace"
            )
        return self._plan.routes[matches[0]]

    def _child_route(
        self, route: RouteContract, *, element: str, path: tuple[str, ...]
    ) -> RouteContract:
        step = path[-1]
        uri, local = _parse_name(step, what="path step")
        key = clark(uri, local)
        field = route.children.get(key)
        if field is not None:
            assert field.child_route_id is not None
            return self._plan.routes[field.child_route_id]
        for wildcard in route.wildcards:
            if wildcard.role == "child" and wildcard.name_class.contains(uri, local):
                assert wildcard.child_route_id is not None
                return self._plan.routes[wildcard.child_route_id]
        raise IntegrationError(
            f"RELAX NG route {(element, *path)}: child {step!r} is not admitted by "
            f"{_route_label(route)}"
        )

    def _check_route(self, route: RouteContract) -> None:
        _check_route_supported(route)

    def _reachable(self, route: RouteContract) -> list[str]:
        pending = [route]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current.route_id in seen:
                continue
            seen.add(current.route_id)
            self._check_route(current)
            for name in sorted(current.children):
                child = current.children[name]
                assert child.child_route_id is not None
                pending.append(self._plan.routes[child.child_route_id])
        return [route_id for route_id in self._plan.routes if route_id in seen]

    def _model(self, route: RouteContract) -> type[BaseModel]:
        cached = self._models.get(route.route_id)
        if cached is not None:
            return cached
        reachable = self._reachable(route)
        new = {
            route_id: self._build(self._plan.routes[route_id])
            for route_id in reachable
            if route_id not in self._models
        }
        namespace = {model.__name__: model for model in self._models.values()}
        namespace.update({model.__name__: model for model in new.values()})
        for model in new.values():
            model.model_rebuild(_types_namespace=namespace, raise_errors=True)
        self._models.update(new)
        return self._models[route.route_id]

    def _build(self, route: RouteContract) -> type[BaseModel]:
        if route.content.scalar_contracts and not route.attributes and not route.children:
            return self._root_model(route)
        return self._record_model(route)

    def _root_model(self, route: RouteContract) -> type[BaseModel]:
        layout = _route_layout(route)
        may_be_empty = _content_may_be_empty(route.content.model)
        metadata = _route_metadata(self._plan, route, layout)
        metadata["x-pyxsd-contracts"] = [
            _contract_metadata(contract) for contract in route.content.scalar_contracts
        ]
        metadata["x-pyxsd-cardinality"] = {
            "minimum": 0 if may_be_empty else 1,
            "maximum": 1,
        }
        annotation = _root_scalar_annotation(
            route.content.scalar_contracts,
            codec=self._codec,
            may_be_empty=may_be_empty,
        )
        root_base: Any = RootModel
        root_validators: dict[str, Any] = {
            _ROUTE_VALIDATION_VALIDATOR: model_validator(mode="after")(
                _route_after_validator(route)
            )
        }
        model = create_model(
            self._names[route.route_id],
            __base__=root_base[annotation],
            __config__=ConfigDict(json_schema_extra=metadata),
            __validators__=root_validators,
            __module__=__name__,
        )
        _attach_route(model, self._plan, route, layout)
        return model

    def _record_model(self, route: RouteContract) -> type[BaseModel]:
        layout = _route_layout(route)
        may_be_empty = _content_may_be_empty(route.content.model)
        route_metadata = _route_metadata(self._plan, route, layout)
        child_aliases = set(route.children)
        field_extras: dict[str, dict[str, Any]] = {}
        child_fields: list[tuple[str, str, bool]] = []
        fields: dict[str, Any] = {}
        used: set[str] = {"xml_contract", _CHILD_PRESENCE_VALIDATOR, _ROUTE_VALIDATION_VALIDATOR}

        def add(
            alias: str,
            annotation: Any,
            *,
            cardinality: Cardinality,
            contracts: tuple[ScalarContract, ...],
            child_route_id: str | None = None,
        ) -> None:
            field_name = _safe_name(alias, used | (child_aliases - {alias}))
            used.add(field_name)
            field_metadata = _field_metadata(
                contracts,
                cardinality,
                child_route_id=child_route_id,
            )
            field_extras[alias] = field_metadata
            if child_route_id is not None:
                child_fields.append((alias, field_name, cardinality.repeatable))
            fields[field_name] = (
                annotation,
                _field_info(
                    alias,
                    metadata=field_metadata,
                    cardinality=cardinality,
                    omit_none=(
                        cardinality.minimum == 0 and not cardinality.repeatable and alias != "$"
                    ),
                ),
            )

        if route.content.scalar_contracts:
            add(
                "$",
                _scalar_annotation(
                    route.content.scalar_contracts,
                    codec=self._codec,
                    may_be_empty=may_be_empty,
                ),
                cardinality=Cardinality(0 if may_be_empty else 1, 1),
                contracts=route.content.scalar_contracts,
            )
        for name in sorted(route.attributes):
            field = route.attributes[name]
            add(
                "@" + field.expanded_name,
                _scalar_annotation(
                    field.scalar_contracts,
                    codec=self._codec,
                    may_be_empty=False,
                ),
                cardinality=field.cardinality,
                contracts=field.scalar_contracts,
            )
        for name in sorted(route.children):
            field = route.children[name]
            assert field.child_route_id is not None
            child_name = self._names[field.child_route_id]
            annotation: Any = (
                GenericAlias(list, child_name) if field.cardinality.repeatable else child_name
            )
            add(
                field.expanded_name,
                annotation,
                cardinality=field.cardinality,
                contracts=field.scalar_contracts,
                child_route_id=field.child_route_id,
            )

        def model_schema(description: dict[str, Any]) -> None:
            description.update(route_metadata)
            properties = description.get("properties", {})
            for alias, extra in field_extras.items():
                properties[alias].update(extra)
            for property_schema in properties.values():
                clean_field_schema(property_schema)

        def validate_children(value: Any) -> Any:
            if isinstance(value, dict):
                for alias, field_name, repeatable in child_fields:
                    for key in (alias, field_name):
                        if key not in value:
                            continue
                        item = value[key]
                        if item is None:
                            raise ValueError(
                                f"child content must be omitted, not None, when absent: {alias!r}"
                            )
                        if (
                            repeatable
                            and isinstance(item, list)
                            and any(entry is None for entry in item)
                        ):
                            raise ValueError(f"child content items must not be None: {alias!r}")
            return value

        validators: dict[str, Any] = {
            _CHILD_PRESENCE_VALIDATOR: model_validator(mode="before")(validate_children),
            _ROUTE_VALIDATION_VALIDATOR: model_validator(mode="after")(
                _route_after_validator(route)
            ),
        }
        model = create_model(
            self._names[route.route_id],
            __config__=ConfigDict(
                extra="forbid",
                strict=True,
                validate_by_name=True,
                serialize_by_alias=True,
                json_schema_extra=model_schema,
            ),
            __validators__=validators,
            __module__=__name__,
            **fields,
        )
        _attach_route(model, self._plan, route, layout)
        return model
