"""Root-preserving, plan-directed RELAX NG JSON profile export and import.

Only compiled routes select layouts. Missing routes retain the lexical ordered
tree; wildcard routes retain their compiled scalar contracts without pretending
that an open child name is a record field. Import is the strict inverse: it
mirrors each documented envelope, never infers a layout from observed content,
and binds parsed tag claims to constructed positions as assertions only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal, cast
from xml.etree import ElementTree as ET

from pyxsd.data_conversion.contracts import (
    Fidelity,
    FormatName,
    JsonValue,
    json_pointer,
    raise_conversion,
    validate_fidelity,
    validate_format,
)
from pyxsd.data_conversion.names import eqname_to_expanded, expanded_to_eqname
from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import XML_NS
from pyxsd.relaxng.contracts import ScalarContract, scalar_contract
from pyxsd.relaxng.datatypes import DatatypeContext
from pyxsd.relaxng.document import _whole_element_scalar_value
from pyxsd.relaxng.json_values import (
    ScalarTagClaim,
    ScalarTagLocation,
    parse_scalar_tag,
    scalar_to_relaxng_json,
)
from pyxsd.relaxng.names import (
    AnyName,
    ExceptName,
    Name,
    NameChoice,
    NameClass,
    NsName,
)
from pyxsd.relaxng.patterns import (
    Choice,
    Data,
    DataExcept,
    Group,
    Interleave,
    List,
    OneOrMore,
    Value,
)
from pyxsd.relaxng.projection import ContentContract, ProjectionPlan, RouteContract
from pyxsd.relaxng.tree import RelaxNGElement, _effective_base

__all__ = ["ImportedTree", "data_to_relaxng_tree", "export_profile", "import_profile"]

_Path = tuple[str | int, ...]
_Layout = Literal["empty", "scalar", "repeated", "record", "sequence", "mixed"]
_XMLNS = "{http://www.w3.org/2000/xmlns/}"
_XSD_DATATYPES = "http://www.w3.org/2001/XMLSchema-datatypes"
#: Atomic XSD types whose decoded value is an array of tokens.
_LIST_VALUED_ATOMICS = frozenset({"IDREFS", "ENTITIES", "NMTOKENS"})
#: Sentinel distinguishing an absent member from an explicit JSON null.
_MISSING = object()


def _reject_nonfinite_data(value: JsonValue, path: tuple[str | int, ...]) -> None:
    """Apply the JSON finite-number boundary to direct Python input too."""
    if isinstance(value, float) and not math.isfinite(value):
        raise_conversion(
            "json-invalid", json_pointer(path), "non-finite numbers require INF/-INF/NaN strings"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite_data(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite_data(item, (*path, index))


def _repointed(exc: PyXSDError, path: str) -> PyXSDError:
    """Rebuild a nested conversion error with the importer's JSON path.

    Every shared helper used here raises with an empty pointer; this function is
    only sound while that invariant holds.
    """
    text = str(exc)
    code, _, rest = text.partition(" at ")
    detail = rest.partition("': ")[2]
    return PyXSDError(f"{code} at {path!r}: {detail}")


def _split(name: str) -> tuple[str | None, str]:
    if name.startswith("{"):
        uri, local = name[1:].split("}", 1)
        return uri or None, local
    return None, name


def _select_expanded_root(plan: ProjectionPlan, element: str) -> str:
    """Resolve one explicit root selector against the start name class.

    Name classes distinguish namespaces only by their authored constants. A
    fresh representative detects infinitely many matches, without enumerating
    an open name class or consulting any instance.
    """
    if element.startswith(("{", "Q{")):
        return eqname_to_expanded(
            expanded_to_eqname(element) if element.startswith("{") else element
        )
    local = eqname_to_expanded(element)
    namespaces: set[str | None] = {None}
    pending = [plan.routes[key].name_class for key in plan.roots]
    while pending:
        name_class = pending.pop()
        if isinstance(name_class, (Name, NsName)):
            namespaces.add(name_class.uri)
        elif isinstance(name_class, NameChoice):
            pending.extend(name_class.alternatives)
        elif isinstance(name_class, ExceptName):
            pending.extend((name_class.base, name_class.excluded))
    fresh = "urn:pyxsd:profile:unmentioned"
    while fresh in namespaces:
        fresh += "_"
    matches = {
        uri
        for uri in (*namespaces, fresh)
        if any(plan.routes[key].name_class.contains(uri, local) for key in plan.roots)
    }
    if fresh in matches or len(matches) > 1:
        raise_conversion("root-ambiguous", "", f"ambiguous root name {element!r}")
    if not matches:
        raise_conversion("root-unresolved", "", f"unknown root name {element!r}")
    uri = matches.pop()
    return f"{{{uri}}}{local}" if uri else local


def _root_route(root: RelaxNGElement, plan: ProjectionPlan, element: str | None) -> RouteContract:
    actual = root._name_
    expanded_to_eqname(actual)
    roots = [plan.routes[key] for key in plan.roots]
    if element is not None:
        selected = _select_expanded_root(plan, element)
        if selected != actual:
            raise_conversion("root-mismatch", "", f"root {actual!r} does not match {element!r}")
    uri, local = _split(actual)
    for route in roots:
        if route.name_class.contains(uri, local):
            return route
    raise_conversion("root-unresolved", "", f"root {actual!r} is outside the start name class")


def _child_model(
    model: ContentContract, memo: dict[int, ContentContract | None]
) -> ContentContract | None:
    """Erase attribute-only content once per DAG node, preserving shared operands."""
    key = id(model)
    if key in memo:
        return memo[key]
    if model.kind in ("attribute", "empty"):
        result = None
    else:
        operands = tuple(
            child for part in model.operands if (child := _child_model(part, memo)) is not None
        )
        if model.kind in ("group", "interleave") and len(operands) == 1:
            result = operands[0]
        elif model.operands and not operands:
            result = None
        else:
            # Keep choice/repeat even with one child operand: erasing an empty
            # alternative must not turn an ordered model into a different layout.
            result = ContentContract(model.kind, operands)
    memo[key] = result
    return result


def _layout(route: RouteContract | None, generic: bool) -> _Layout:
    if route is None:
        return "mixed"
    if route.content.scalar_contracts:
        return "scalar"
    if generic or route.generic_reason or any(w.role == "child" for w in route.wildcards):
        return "mixed"
    if route.content.mixed:
        return "mixed"
    model = _child_model(route.content.model, {})
    if model is None:
        return "empty"
    if len(route.children) == 1 and next(iter(route.children.values())).cardinality.repeatable:
        return "repeated"
    if model.kind == "interleave" and all(
        not field.cardinality.repeatable for field in route.children.values()
    ):
        return "record"
    return "sequence"


def _scalar(value: Any, path: _Path) -> JsonValue:
    if value is None:
        raise_conversion(
            "profile-scalar-invalid", json_pointer(path), "null is not a profile scalar"
        )
    if isinstance(value, (tuple, list)):
        return [_scalar(item, (*path, index)) for index, item in enumerate(value)]
    try:
        return scalar_to_relaxng_json(value, format="pyxsd-json")
    except PyXSDError as error:
        raise_conversion("profile-scalar-invalid", json_pointer(path), str(error))


def _attributes(
    node: RelaxNGElement, route: RouteContract | None, path: _Path
) -> dict[str, JsonValue]:
    names = list(route.attributes) if route is not None else []
    names.extend(sorted(set(node.attributes) - set(names)))
    result: dict[str, JsonValue] = {}
    for name in names:
        if name == "xmlns" or name.startswith(_XMLNS) or name not in node.attributes:
            continue
        key = "@" + expanded_to_eqname(name)
        field = route.attributes.get(name) if route is not None else None
        value = (
            node.attribute_value(name)
            if field is not None and field.scalar_contracts
            else node.attribute_lexical(name)
        )
        result[key] = _scalar(value, (*path, key))
    return result


def _child_route(
    route: RouteContract | None, child: RelaxNGElement, plan: ProjectionPlan
) -> tuple[RouteContract | None, bool]:
    if route is not None:
        field = route.children.get(child._name_)
        if field is not None:
            return plan.routes.get(field.child_route_id or ""), False
        uri, local = _split(child._name_)
        for wildcard in route.wildcards:
            if wildcard.role == "child" and wildcard.name_class.contains(uri, local):
                return plan.routes.get(wildcard.child_route_id or ""), True
    return None, True


def _export(
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    *,
    generic: bool = False,
) -> JsonValue:
    layout = _layout(route, generic)
    attribute_path = (*path, 0) if layout in ("sequence", "mixed") else path
    attributes = _attributes(node, route, attribute_path)
    has_attributes = route is None or bool(
        route.attributes or any(w.role == "attribute" for w in route.wildcards)
    )
    if layout in ("empty", "scalar"):
        if node.children:
            raise_conversion("profile-container-invalid", json_pointer(path), "unexpected children")
        if layout == "empty":
            text = node.text_value()
            if text and text.strip():
                raise_conversion("profile-container-invalid", json_pointer(path), "unexpected text")
            return attributes if has_attributes else ""
        wrapped = has_attributes or generic or bool(route and route.generic_reason)
        value = _scalar(
            _whole_element_scalar_value(node),
            (*path, "#content") if wrapped else path,
        )
        if wrapped:
            return {**attributes, "#content": value}
        return value

    if layout != "mixed" and any(
        text and text.strip()
        for text in (node.text_value(), *(child.tail_value() for child in node.children))
    ):
        raise_conversion("profile-container-invalid", json_pointer(path), "unexpected text")

    if layout == "record":
        result = dict(attributes)
        for child in node.children:
            key = expanded_to_eqname(child._name_)
            if key in result:
                raise_conversion(
                    "profile-container-invalid", json_pointer((*path, key)), "duplicate child"
                )
            child_route, child_generic = _child_route(route, child, plan)
            result[key] = _export(child, child_route, plan, (*path, key), generic=child_generic)
        return result

    if layout == "repeated":
        assert route is not None
        name = next(iter(route.children))
        key = expanded_to_eqname(name)
        items: list[JsonValue] = []
        item_path = (*path, key) if has_attributes else path
        for child in node.children:
            expanded_to_eqname(child._name_)
            if child._name_ != name:
                raise_conversion(
                    "profile-container-invalid", json_pointer(item_path), "unexpected child"
                )
            child_route, child_generic = _child_route(route, child, plan)
            items.append(
                _export(child, child_route, plan, (*item_path, len(items)), generic=child_generic)
            )
        return {**attributes, key: items} if has_attributes else items

    sequence: list[JsonValue] = []
    if attributes:
        sequence.append(dict(sorted(attributes.items())))
    if layout == "mixed" and node.text_value() is not None:
        sequence.append(node.text_value())
    for child in node.children:
        key = expanded_to_eqname(child._name_)
        child_route, child_generic = _child_route(route, child, plan)
        sequence.append(
            {
                key: _export(
                    child, child_route, plan, (*path, len(sequence), key), generic=child_generic
                )
            }
        )
        if layout == "mixed" and child.tail_value() is not None:
            sequence.append(child.tail_value())
    return sequence


def export_profile(
    root: RelaxNGElement, plan: ProjectionPlan, *, element: str | None = None
) -> dict[str, JsonValue]:
    """Export a current typed tree using only its compiled route contracts.

    ``element`` narrows the start name class, not a global declaration. Local
    selectors must be unambiguous across the grammar, including open classes.
    Public dispatch owns the document-level validity and freshness gate.
    """
    route = _root_route(root, plan, element)
    key = expanded_to_eqname(root._name_)
    return {key: _export(root, route, plan, (key,))}


# ---------------------------------------------------------------------------
# Deterministic import
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportedTree:
    """A temporary Clark-named ElementTree with its per-node contexts.

    ``contexts`` is keyed by ``id()`` of each constructed element only; the
    parse adapts every context onto the corresponding lexical node frame, so
    ET identity is never retained and the context is not resource authority.
    ``claims`` are parsed wire assertions with their import-side locations.
    """

    root: ET.Element
    contexts: Mapping[int, DatatypeContext]
    claims: tuple[ScalarTagClaim, ...]


def _atomic_qname(contract: ScalarContract) -> bool:
    return (
        contract.kind == "atomic"
        and contract.type_name in ("QName", "NOTATION")
        and contract.library_uri == _XSD_DATATYPES
    )


def _contract_qname(contract: ScalarContract, plan: ProjectionPlan) -> bool:
    """Whether a scalar contract reads a QName, including a list of QNames."""
    if contract.kind != "list":
        return _atomic_qname(contract)
    pending = [plan.encoding_patterns[contract.pattern_identity]]
    seen: set[int] = set()
    while pending:
        pattern = pending.pop()
        if id(pattern) in seen:
            continue
        seen.add(id(pattern))
        if isinstance(pattern, (Data, DataExcept, Value)):
            if _contract_qname(scalar_contract(pattern), plan):
                return True
        elif isinstance(pattern, (Choice, Group, Interleave)):
            pending.extend((pattern.first, pattern.second))
        elif isinstance(pattern, (List, OneOrMore)):
            pending.append(pattern.pattern)
    return False


def _list_valued(contract: ScalarContract) -> bool:
    """Whether this contract's decoded JSON value is an array of tokens."""
    return contract.kind == "list" or (
        contract.kind == "atomic"
        and contract.library_uri == _XSD_DATATYPES
        and contract.type_name in _LIST_VALUED_ATOMICS
    )


def _position_scalar_contracts(
    model: ContentContract,
    name: str | None,
) -> tuple[ScalarContract, ...]:
    """Potential contracts at one position, without flattening field roles.

    ``None`` selects leading text, including scalar alternatives in structural
    routes. Attributes retain the name/contract correlation of original leaves,
    which merged wildcard views erase. These are not surviving candidates.
    """
    pending = [model]
    seen: set[int] = set()
    contracts: dict[ScalarContract, None] = {}
    while pending:
        part = pending.pop()
        if id(part) in seen:
            continue
        seen.add(id(part))
        if part.kind == ("scalar" if name is None else "attribute"):
            if name is None or cast("NameClass", part.name_class).contains(*_split(name)):
                contracts.update(dict.fromkeys(part.scalar_contracts))
        else:
            pending.extend(part.operands)
    return tuple(contracts)


class _ImportState:
    """One import run's contexts, claims, and deterministic generated prefixes."""

    __slots__ = ("claims", "contexts", "counter", "prefixes")

    def __init__(self) -> None:
        self.claims: list[ScalarTagClaim] = []
        self.contexts: dict[int, DatatypeContext] = {}
        self.counter = 0
        self.prefixes: dict[str, str] = {}

    def prefix_for(self, uri: str, frame: dict[str, str]) -> str:
        """Bind a deterministic generated prefix for *uri* in *frame*."""
        prefix = self.prefixes.get(uri)
        if prefix is not None and prefix not in frame:
            frame[prefix] = uri
            return prefix
        prefix = f"ns{self.counter}"
        self.counter += 1
        while prefix in frame:
            prefix = f"ns{self.counter}"
            self.counter += 1
        self.prefixes[uri] = prefix
        frame[prefix] = uri
        return prefix

    def tree(self, root: ET.Element) -> ImportedTree:
        return ImportedTree(root, MappingProxyType(self.contexts), tuple(self.claims))


def _parse_node_tags(raws: list[tuple[Any, _Path]]) -> dict[int, ScalarTagClaim]:
    """Structurally parse every candidate tag before any tree is built."""
    tags: dict[int, ScalarTagClaim] = {}
    for raw, path in raws:
        if isinstance(raw, dict) and "#value" in raw:
            tags[id(raw)] = parse_scalar_tag(raw, json_pointer(path))
    return tags


def _claim_node_context(
    claims: list[ScalarTagClaim], path: _Path
) -> tuple[dict[str, str], str | None]:
    """The one node frame and base URI every tag on a node asserts."""
    first = claims[0].context
    for claim in claims[1:]:
        other = claim.context
        if other.base_uri != first.base_uri or dict(other.namespaces) != dict(first.namespaces):
            raise_conversion(
                "namespace-conflict",
                json_pointer(path),
                "scalar tags on one element declare different datatype contexts",
            )
    return dict(first.namespaces), first.base_uri


def _eqname_or_string(
    text: str,
    contracts: tuple[ScalarContract, ...],
    frame: dict[str, str],
    state: _ImportState,
    frozen: bool,
    path: _Path,
    plan: ProjectionPlan,
) -> str:
    """Translate an EQName string when every contract reads a QName.

    A list-of-QName contract receives one token per call.  The default binding
    is a representable prefix (the lexical local name), and the implicit ``xml``
    prefix never needs a new assertion.  Only a tag-frozen frame with no
    representable binding is a conflict.
    """
    if (
        not text.startswith("Q{")
        or not contracts
        or not all(_contract_qname(c, plan) for c in contracts)
    ):
        return text
    try:
        expanded = eqname_to_expanded(text)
    except PyXSDError:
        return text
    uri, local = _split(expanded)
    if not uri:
        return local
    if uri == XML_NS:
        return f"xml:{local}"
    for prefix, bound in frame.items():
        if prefix and bound == uri:
            return f"{prefix}:{local}"
    if frame.get("") == uri:
        return local
    if frozen:
        raise_conversion(
            "namespace-conflict",
            json_pointer(path),
            f"no prefix for namespace {uri!r} can join the tag-scoped context",
        )
    return f"{state.prefix_for(uri, frame)}:{local}"


def _lexical_scalar(
    raw: Any,
    *,
    contracts: tuple[ScalarContract, ...],
    frame: dict[str, str],
    state: _ImportState,
    tags: Mapping[int, ScalarTagClaim],
    frozen: bool,
    path: _Path,
    plan: ProjectionPlan,
) -> str:
    """Rebuild one scalar's lexical text without choosing a datatype."""
    claim = tags.get(id(raw))
    if claim is not None:
        return claim.lexical
    if raw is None:
        raise_conversion(
            "scalar-type-invalid", json_pointer(path), "bare null is not a RELAX NG scalar value"
        )
    if isinstance(raw, dict):
        raise_conversion(
            "container-invalid",
            json_pointer(path),
            "a scalar slot requires a scalar, not an object",
        )
    if isinstance(raw, list):
        return " ".join(
            _lexical_scalar(
                item,
                contracts=contracts,
                frame=frame,
                state=state,
                tags=tags,
                frozen=frozen,
                path=(*path, index),
                plan=plan,
            )
            for index, item in enumerate(raw)
        )
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        return str(raw)
    return _eqname_or_string(raw, contracts, frame, state, frozen, path, plan)


def _append_text(element: ET.Element, text: str) -> None:
    """Place character data as leading text or the last child's tail."""
    children = list(element)
    if children:
        last = children[-1]
        last.tail = (last.tail or "") + text
    else:
        element.text = (element.text or "") + text


def _expand_key(key: str, path: _Path) -> str:
    """Expand an EQName or bare local key, repointing name failures."""
    try:
        return eqname_to_expanded(key)
    except PyXSDError as error:
        raise _repointed(error, json_pointer(path)) from None


def _attribute_key(key: str, path: _Path) -> str:
    """Expand one ``@``-prefixed profile or native attribute key."""
    expanded = _expand_key(key[1:], path)
    if expanded == "xmlns" or expanded.startswith(_XMLNS):
        raise_conversion(
            "name-invalid",
            json_pointer(path),
            "namespace declarations are not importable attribute data",
        )
    return expanded


def _open_class(name_class: NameClass) -> bool:
    if isinstance(name_class, (AnyName, NsName)):
        return True
    if isinstance(name_class, NameChoice):
        return any(_open_class(alternative) for alternative in name_class.alternatives)
    if isinstance(name_class, ExceptName):
        return _open_class(name_class.base)
    return False


def _finite_class_names(name_class: NameClass) -> list[str]:
    """Expanded names a closed class can mention."""
    if isinstance(name_class, Name):
        return [f"{{{name_class.uri}}}{name_class.local}" if name_class.uri else name_class.local]
    if isinstance(name_class, NameChoice):
        return [name for part in name_class.alternatives for name in _finite_class_names(part)]
    if isinstance(name_class, ExceptName):
        return _finite_class_names(name_class.base) + _finite_class_names(name_class.excluded)
    return []


def _unique_admitted_root(plan: ProjectionPlan) -> str:
    """The single expanded name a root-absorbed format can reconstruct."""
    names: set[str] = set()
    for key in plan.roots:
        name_class = plan.routes[key].name_class
        if _open_class(name_class):
            names.clear()
            break
        names.update(
            name for name in _finite_class_names(name_class) if name_class.contains(*_split(name))
        )
    if len(names) != 1:
        raise_conversion(
            "root-ambiguous",
            "",
            "a root-absorbed format needs an explicit element when the start "
            f"pattern admits {len(names)} named roots",
        )
    return names.pop()


def _plan_route(plan: ProjectionPlan, name: str) -> RouteContract | None:
    """The root route admitting *name*, or ``None`` for a lexical escape."""
    uri, local = _split(name)
    for key in plan.roots:
        route = plan.routes[key]
        if route.name_class.contains(uri, local):
            return route
    return None


def _child_target(
    plan: ProjectionPlan, route: RouteContract | None, expanded: str
) -> tuple[RouteContract | None, bool]:
    """The child's route and generic flag for one expanded name."""
    if route is not None:
        field = route.children.get(expanded)
        if field is not None:
            return plan.routes.get(field.child_route_id or ""), False
        uri, local = _split(expanded)
        for wildcard in route.wildcards:
            if wildcard.role == "child" and wildcard.name_class.contains(uri, local):
                return plan.routes.get(wildcard.child_route_id or ""), True
    return None, True


class _ProfileImporter:
    """The strict inverse of :func:`export_profile` for one plan."""

    def __init__(self, plan: ProjectionPlan, root_frame: dict[str, str]) -> None:
        self.plan = plan
        self.root_frame = root_frame
        self.state = _ImportState()

    def build(self, data: Any, element: str | None) -> ET.Element:
        if not isinstance(data, dict):
            raise_conversion(
                "container-invalid",
                "",
                f"the pyxsd-json root must be a single-entry object, not {type(data).__name__}",
            )
        if len(data) != 1:
            raise_conversion(
                "root-ambiguous",
                "",
                f"the pyxsd-json root object must hold exactly one entry, not {len(data)}",
            )
        key, value = next(iter(data.items()))
        name = eqname_to_expanded(key)
        if element is not None:
            selected = _select_expanded_root(self.plan, element)
            if selected != name:
                raise_conversion(
                    "root-mismatch",
                    "",
                    f"the carried root {name!r} does not match the selected root {selected!r}",
                )
        route = _plan_route(self.plan, name)
        if route is None:
            raise_conversion(
                "root-unresolved", "", f"root {name!r} is outside the start name class"
            )
        root = self._element(name, value, route, dict(self.root_frame), None, (key,), ())
        return root

    # ------------------------------------------------------------------
    # Scalars, claims, and node contexts
    # ------------------------------------------------------------------

    def _apply_scalars(
        self,
        element: ET.Element,
        attr_pairs: list[tuple[str, Any, _Path]],
        content_pair: tuple[Any, _Path] | None,
        route: RouteContract | None,
        frame: dict[str, str],
        parent_base: str | None,
        path: _Path,
        element_path: tuple[int, ...],
    ) -> str | None:
        """Apply attributes and simple content, binding every parsed claim.

        In-band ``xml:base`` participates in the effective base only after the
        attribute store is complete.  A claim's asserted context replaces the
        inherited frame and base; no other metadata influences them.
        """
        candidates = [(raw, error_path) for _, raw, error_path in attr_pairs]
        if content_pair is not None:
            candidates.append(content_pair)
        tags = _parse_node_tags(candidates)
        frozen = bool(tags)
        base = parent_base
        if tags:
            claim_frame, claim_base = _claim_node_context(list(tags.values()), path)
            frame.clear()
            frame.update(claim_frame)
            base = claim_base
        seen: dict[str, _Path] = {}
        for expanded, raw, error_path in attr_pairs:
            if expanded in seen:
                raise_conversion(
                    "json-duplicate-key",
                    json_pointer(error_path),
                    f"the attributes at {json_pointer(seen[expanded])} and "
                    f"{json_pointer(error_path)} denote the expanded name {expanded!r}",
                )
            seen[expanded] = error_path
            self._bind_claim(tags, raw, element_path, attribute=expanded)
            contracts = _position_scalar_contracts(route.content.model, expanded) if route else ()
            element.attrib[expanded] = _lexical_scalar(
                raw,
                contracts=contracts,
                frame=frame,
                state=self.state,
                tags=tags,
                frozen=frozen,
                path=error_path,
                plan=self.plan,
            )
        if not tags:
            base = _effective_base(parent_base, element)
        if content_pair is not None:
            raw, error_path = content_pair
            self._bind_claim(tags, raw, element_path, content_index=0)
            contracts = _position_scalar_contracts(route.content.model, None) if route else ()
            element.text = _lexical_scalar(
                raw,
                contracts=contracts,
                frame=frame,
                state=self.state,
                tags=tags,
                frozen=frozen,
                path=error_path,
                plan=self.plan,
            )
        return base

    def _bind_claim(
        self,
        tags: Mapping[int, ScalarTagClaim],
        raw: Any,
        element_path: tuple[int, ...],
        *,
        attribute: str | None = None,
        content_index: int | None = None,
    ) -> None:
        claim = tags.get(id(raw))
        if claim is not None:
            self.state.claims.append(
                replace(
                    claim,
                    location=ScalarTagLocation(
                        element_path, attribute=attribute, content_index=content_index
                    ),
                )
            )

    def _child_target(
        self, route: RouteContract | None, expanded: str
    ) -> tuple[RouteContract | None, bool]:
        """The child's route and generic flag, mirroring export exactly."""
        return _child_target(self.plan, route, expanded)

    # ------------------------------------------------------------------
    # Element decoding
    # ------------------------------------------------------------------

    def _element(
        self,
        name: str,
        raw: Any,
        route: RouteContract | None,
        parent_frame: dict[str, str],
        parent_base: str | None,
        path: _Path,
        element_path: tuple[int, ...],
        *,
        generic: bool = False,
    ) -> ET.Element:
        layout = _layout(route, generic)
        has_attributes = route is None or bool(
            route.attributes or any(w.role == "attribute" for w in route.wildcards)
        )
        element = ET.Element(name)
        frame = dict(parent_frame)
        base = parent_base

        if layout == "empty":
            if has_attributes:
                if not isinstance(raw, dict):
                    raise_conversion(
                        "container-invalid",
                        json_pointer(path),
                        f"empty content with attributes for {name!r} requires an attribute object",
                    )
                base = self._apply_scalars(
                    element,
                    self._attribute_entries(raw, path),
                    None,
                    route,
                    frame,
                    parent_base,
                    path,
                    element_path,
                )
            elif raw != "":
                raise_conversion(
                    "container-invalid",
                    json_pointer(path),
                    f'empty content for {name!r} is the empty string "", not {type(raw).__name__}',
                )
        elif layout == "scalar":
            wrapped = has_attributes or generic or bool(route and route.generic_reason)
            if wrapped:
                if not isinstance(raw, dict):
                    raise_conversion(
                        "container-invalid",
                        json_pointer(path),
                        f"simple content with attributes for {name!r} requires an object",
                    )
                attr_pairs: list[tuple[str, Any, _Path]] = []
                content_pair: tuple[Any, _Path] | None = None
                for key, item in raw.items():
                    if key.startswith("@"):
                        attr_pairs.append((_attribute_key(key, (*path, key)), item, (*path, key)))
                    elif key == "#content":
                        content_pair = (item, (*path, key))
                    else:
                        raise_conversion(
                            "container-invalid",
                            json_pointer((*path, key)),
                            f"unexpected member {key!r} in simple content",
                        )
                if content_pair is None:
                    raise_conversion(
                        "container-invalid",
                        json_pointer(path),
                        f"simple content for {name!r} requires a #content member",
                    )
                base = self._apply_scalars(
                    element, attr_pairs, content_pair, route, frame, parent_base, path, element_path
                )
            else:
                base = self._apply_scalars(
                    element,
                    [],
                    (raw, path),
                    route,
                    frame,
                    parent_base,
                    path,
                    element_path,
                )
        elif layout == "record":
            if not isinstance(raw, dict):
                raise_conversion(
                    "container-invalid",
                    json_pointer(path),
                    f"record content for {name!r} requires an object",
                )
            attr_pairs = []
            children: list[tuple[str, Any, _Path]] = []
            seen: dict[str, str] = {}
            for key, item in raw.items():
                if key.startswith("@"):
                    attr_pairs.append((_attribute_key(key, (*path, key)), item, (*path, key)))
                    continue
                expanded = _expand_key(key, (*path, key))
                if expanded in seen:
                    raise_conversion(
                        "json-duplicate-key",
                        json_pointer((*path, key)),
                        f"the members {seen[expanded]!r} and {key!r} denote the expanded "
                        f"name {expanded!r}",
                    )
                seen[expanded] = key
                children.append((expanded, item, (*path, key)))
            base = self._apply_scalars(
                element, attr_pairs, None, route, frame, parent_base, path, element_path
            )
            assert route is not None
            by_name = {
                expanded: (expanded, item, child_path) for expanded, item, child_path in children
            }
            ordered = [by_name[name] for name in route.children if name in by_name]
            ordered.extend(entry for entry in children if entry[0] not in route.children)
            children = ordered
            for expanded, item, child_path in children:
                child_route, child_generic = self._child_target(route, expanded)
                element.append(
                    self._element(
                        expanded,
                        item,
                        child_route,
                        frame,
                        base,
                        child_path,
                        (*element_path, len(element)),
                        generic=child_generic,
                    )
                )
        elif layout == "repeated":
            assert route is not None
            child_name = next(iter(route.children))
            key = expanded_to_eqname(child_name)
            if has_attributes:
                if not isinstance(raw, dict):
                    raise_conversion(
                        "container-invalid",
                        json_pointer(path),
                        f"repeated content with attributes for {name!r} requires an object",
                    )
                attr_pairs = []
                items: Any = _MISSING
                for member, item in raw.items():
                    if member.startswith("@"):
                        attr_pairs.append(
                            (_attribute_key(member, (*path, member)), item, (*path, member))
                        )
                    elif member == key:
                        items = item
                    else:
                        raise_conversion(
                            "container-invalid",
                            json_pointer((*path, member)),
                            f"unexpected member {member!r} in repeated content",
                        )
                if items is _MISSING:
                    raise_conversion(
                        "container-invalid",
                        json_pointer((*path, key)),
                        f"repeated content for {name!r} requires the member {key!r}",
                    )
                if not isinstance(items, list):
                    raise_conversion(
                        "container-invalid",
                        json_pointer((*path, key)),
                        f"the repeated member {key!r} must be an array",
                    )
                item_path = (*path, key)
                base = self._apply_scalars(
                    element, attr_pairs, None, route, frame, parent_base, path, element_path
                )
            else:
                if not isinstance(raw, list):
                    raise_conversion(
                        "container-invalid",
                        json_pointer(path),
                        f"repeated content for {name!r} requires an array",
                    )
                items = raw
                item_path = path
            child_route, child_generic = self._child_target(route, child_name)
            for index, item in enumerate(items):
                element.append(
                    self._element(
                        child_name,
                        item,
                        child_route,
                        frame,
                        base,
                        (*item_path, index),
                        (*element_path, len(element)),
                        generic=child_generic,
                    )
                )
        else:
            if not isinstance(raw, list):
                raise_conversion(
                    "container-invalid",
                    json_pointer(path),
                    f"ordered content for {name!r} requires an array",
                )
            leading_attributes = self._leading_attribute_maps(raw, path)
            base = self._apply_scalars(
                element,
                [
                    (expanded, item, (*path, map_index, key))
                    for map_index, key, expanded, item in leading_attributes
                ],
                None,
                route,
                frame,
                parent_base,
                path,
                element_path,
            )
            for index in range(len(leading_attributes), len(raw)):
                item = raw[index]
                item_path = (*path, index)
                if isinstance(item, str):
                    if layout == "sequence" and not item.strip():
                        continue
                    _append_text(element, item)
                    continue
                if isinstance(item, dict):
                    if item and all(key.startswith("@") for key in item):
                        raise_conversion(
                            "container-invalid",
                            json_pointer(item_path),
                            "ordered content attributes must precede every content item",
                        )
                    if len(item) != 1:
                        raise_conversion(
                            "container-invalid",
                            json_pointer(item_path),
                            "ordered content items must be strings or singleton child maps",
                        )
                    key, child_raw = next(iter(item.items()))
                    expanded = _expand_key(key, (*item_path, key))
                    child_route, child_generic = self._child_target(route, expanded)
                    element.append(
                        self._element(
                            expanded,
                            child_raw,
                            child_route,
                            frame,
                            base,
                            (*item_path, key),
                            (*element_path, len(element)),
                            generic=child_generic,
                        )
                    )
                    continue
                raise_conversion(
                    "container-invalid",
                    json_pointer(item_path),
                    "ordered content items must be strings or singleton child maps",
                )

        self.state.contexts[id(element)] = DatatypeContext(base, dict(frame))
        return element

    def _attribute_entries(self, raw: dict[str, Any], path: _Path) -> list[tuple[str, Any, _Path]]:
        entries: list[tuple[str, Any, _Path]] = []
        for key, item in raw.items():
            if not key.startswith("@"):
                raise_conversion(
                    "container-invalid",
                    json_pointer((*path, key)),
                    f"empty content cannot carry the member {key!r}",
                )
            entries.append((_attribute_key(key, (*path, key)), item, (*path, key)))
        return entries

    def _leading_attribute_maps(
        self, raw: list[Any], path: _Path
    ) -> list[tuple[int, str, str, Any]]:
        """Consume attribute maps that precede all ordered content."""
        entries: list[tuple[int, str, str, Any]] = []
        seen: dict[str, str] = {}
        for index, item in enumerate(raw):
            if not (isinstance(item, dict) and item and all(k.startswith("@") for k in item)):
                break
            map_seen: dict[str, str] = {}
            for key, value in item.items():
                expanded = _attribute_key(key, (*path, index, key))
                if expanded in map_seen:
                    raise_conversion(
                        "json-duplicate-key",
                        json_pointer((*path, index, key)),
                        f"the members {map_seen[expanded]!r} and {key!r} denote "
                        f"the expanded name {expanded!r}",
                    )
                map_seen[expanded] = key
                if expanded in seen:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, index, key)),
                        f"the attribute {expanded!r} is declared in more than one attribute map",
                    )
                seen[expanded] = key
                entries.append((index, key, expanded, value))
        return entries


def import_profile(
    plan: ProjectionPlan,
    data: JsonValue,
    *,
    element: str | None,
    root_frame: dict[str, str],
) -> ImportedTree:
    """Build the profile inverse of ``data`` under one compiled plan."""
    importer = _ProfileImporter(plan, root_frame)
    return importer.state.tree(importer.build(data, element))


def data_to_relaxng_tree(
    schema: Any,
    data: JsonValue,
    *,
    format: FormatName = "pyxsd-json",
    element: str | None = None,
    namespaces: Mapping[str, str] | None = None,
    fidelity: Fidelity = "strict",
) -> ImportedTree:
    """Convert JSON-compatible *data* into a RELAX NG element tree.

    Format and fidelity names are validated and non-finite numbers rejected
    before any work. The builders mirror each exporter's documented shape and
    compiled layout in deterministic order: no sample-shape inference,
    permutation search, XML defaults, ``xsi`` markers, or synthesized
    ``xml:base``. Seeded *namespaces* scope the root and are shadowed by
    in-band declarations and tag contexts exactly as XML scopes are.
    """
    requested_format = validate_format(format)
    requested_fidelity = validate_fidelity(fidelity)
    _reject_nonfinite_data(data, ())
    plan = schema.projection
    root_frame = dict(namespaces) if namespaces else {}
    if requested_format == "pyxsd-json":
        return import_profile(plan, data, element=element, root_frame=root_frame)
    from pyxsd.relaxng.xml_conversion import (
        import_badgerfish,
        import_jsonml,
        import_native,
        import_parker,
    )

    if requested_format == "pyxsd":
        return import_native(
            plan, data, element=element, root_frame=root_frame, fidelity=requested_fidelity
        )
    if requested_format == "parker":
        return import_parker(
            plan, data, element=element, root_frame=root_frame, fidelity=requested_fidelity
        )
    if requested_format == "badgerfish":
        return import_badgerfish(
            plan, data, element=element, root_frame=root_frame, fidelity=requested_fidelity
        )
    return import_jsonml(
        plan, data, element=element, root_frame=root_frame, fidelity=requested_fidelity
    )
