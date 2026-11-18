"""Lexical-tree adapters for the conventional RELAX NG data formats.

Native ``pyxsd`` keeps one grouped object per element: expanded element names
are keys, attributes use ``@``, and character data uses ``$``.  Parker absorbs
the root and groups children by local name.  BadgerFish retains the root and
uses deterministic lexical prefixes.  JsonML writes an ordered positional
array, so it remains available for invalid, generic, and dirty trees.

Layouts and routes come from the compiled projection plan through the same
private helpers the ``pyxsd-json`` profile uses.  Scalar conversion uses the
shared RELAX NG scalar layer; XSD declarations, layouts, binders, nil state,
and ``xsi`` markers are never consulted.  Namespace declaration attributes are
the only attributes excluded from ordinary data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from copy import copy
from dataclasses import dataclass, field
from itertools import product
from typing import TYPE_CHECKING, Any, NoReturn, cast
from xml.etree import ElementTree as ET

from pyxsd.data_conversion.contracts import (
    Fidelity,
    FormatName,
    JsonValue,
    json_pointer,
    raise_conversion,
    warn_conversion,
)
from pyxsd.data_conversion.values import scalar_to_json
from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import XML_NS
from pyxsd.relaxng.contracts import ScalarContract, scalar_contract
from pyxsd.relaxng.datatypes import DatatypeContext
from pyxsd.relaxng.document import _whole_element_scalar_value
from pyxsd.relaxng.evidence import EvidenceGraph, EvidenceOccurrence, build_binding_evidence
from pyxsd.relaxng.json_profile import _child_route, _layout, _root_route
from pyxsd.relaxng.json_values import scalar_to_relaxng_json
from pyxsd.relaxng.names import ExceptName, Name, NameChoice, NameClass, NsName
from pyxsd.relaxng.patterns import (
    Choice,
    Data,
    DataExcept,
    Element,
    Group,
    Interleave,
    List,
    OneOrMore,
    Value,
)
from pyxsd.relaxng.projection import ContentContract, ProjectionPlan, RouteContract
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.validator import validate_element
from pyxsd.relaxng.values import AlternativeValue, ListInterpretation
from pyxsd.xsd_data_types import QName

if TYPE_CHECKING:
    from pyxsd.relaxng.conversion import ExportMode
    from pyxsd.relaxng.document import RelaxNGDocument

__all__ = ["export_badgerfish", "export_jsonml", "export_native", "export_parker"]

_Path = tuple[str | int, ...]

_TEXT_KEY = "$"
_XMLNS_KEY = "@xmlns"
_XMLNS = "{http://www.w3.org/2000/xmlns/}"
_WILDCARD_URI_SEED = "__pyxsd_relaxng_wildcard__"


# ---------------------------------------------------------------------------
# Shared route, name, and diagnostic helpers
# ---------------------------------------------------------------------------


def _split(name: str) -> tuple[str | None, str]:
    if name.startswith("{"):
        uri, local = name[1:].split("}", 1)
        return uri or None, local
    return None, name


def _local_name(name: str) -> str:
    return _split(name)[1]


def _data_attributes(node: RelaxNGElement) -> list[tuple[str, str]]:
    """Observed attributes without XML namespace declarations."""
    return [
        (name, value)
        for name, value in node.attributes.items()
        if name != "xmlns" and not name.startswith(_XMLNS)
    ]


def _root_or_none(document: RelaxNGDocument, *, require: bool) -> RouteContract | None:
    """Resolve the root route, or keep a lexical fallback for dirty trees."""
    try:
        return _root_route(document.root, document.schema.projection, None)
    except PyXSDError:
        if require:
            raise
        return None


def _node_layout(
    route: RouteContract | None,
    child: RelaxNGElement,
    plan: ProjectionPlan,
) -> tuple[RouteContract | None, bool]:
    """Resolve one child's route and whether its layout is generic."""
    return _child_route(route, child, plan)


def _typed_available(document: RelaxNGDocument) -> bool:
    """Whether schema-directed scalar values describe the current tree."""
    return document.typed and document.interpretations_current and not document.report.has_errors


def _loss(
    code: str,
    path: _Path,
    message: str,
    fidelity: Fidelity,
) -> None:
    if fidelity == "strict":
        raise_conversion(code, json_pointer(path), message)
    warn_conversion(code, json_pointer(path), message)


def _container_error(path: _Path, message: str) -> NoReturn:
    raise_conversion("container-invalid", json_pointer(path), message)


def _has_non_whitespace_content(node: RelaxNGElement) -> bool:
    text = node.text_value()
    if text is not None and text.strip():
        return True
    return any(
        tail is not None and tail.strip()
        for tail in (child.tail_value() for child in node.children)
    )


def _mixed_text(node: RelaxNGElement) -> str:
    pieces: list[str] = []
    text = node.text_value()
    if text:
        pieces.append(text)
    for child in node.children:
        tail = child.tail_value()
        if tail:
            pieces.append(tail)
    return "".join(pieces)


def _parent_name(node: RelaxNGElement) -> str:
    parent = node.parent
    return _local_name(parent._name_) if parent is not None else ""


def _check_grouped_order(
    route: RouteContract | None,
    children: tuple[RelaxNGElement, ...],
    path: _Path,
    fidelity: Fidelity,
) -> None:
    """Reject grouped child order that a schema order cannot reconstruct."""
    if not children:
        return
    observed = [child._name_ for child in children]
    seen: set[str] = set()
    first_order: list[str] = []
    previous: str | None = None
    for name in observed:
        if name != previous and name in seen:
            _loss(
                "order-ambiguous",
                path,
                "grouped export cannot retain the interleaving of child "
                f"{name!r} in element {_parent_name(children[0])!r}",
                fidelity,
            )
            return
        if name not in seen:
            seen.add(name)
            first_order.append(name)
        previous = name

    plan_order = list(route.children) if route is not None else []
    known = [name for name in plan_order if name in seen]
    extras = [name for name in first_order if name not in set(plan_order)]
    if first_order != known + extras:
        _loss(
            "order-ambiguous",
            path,
            "grouped export cannot reconstruct the child order of element "
            f"{_parent_name(children[0])!r}",
            fidelity,
        )


def _check_mixed_order(
    route: RouteContract | None,
    node: RelaxNGElement,
    path: _Path,
    fidelity: Fidelity,
) -> None:
    children = node.children
    if len({child._name_ for child in children}) > 1:
        _loss(
            "order-ambiguous",
            path,
            f"grouped export cannot retain the child order of mixed element {node._name_!r}",
            fidelity,
        )
    else:
        _check_grouped_order(route, children, path, fidelity)


# ---------------------------------------------------------------------------
# Scalar helpers
# ---------------------------------------------------------------------------


def _needs_lexical_fallback(value: Any) -> bool:
    """Whether the scalar layer should keep the original lexical text.

    Wrappers carry their own original lexical content and are tagged by the
    native JSON-safe layer, so they are representable.  Binary values have no
    JSON representation; conventional formats use the lexical characters.
    """
    if isinstance(value, (AlternativeValue, ListInterpretation)):
        return False
    if isinstance(value, (bytes, bytearray)):
        return True
    if isinstance(value, (tuple, list)):
        return any(_needs_lexical_fallback(item) for item in value)
    return False


def _native_json_scalar(value: Any, lexical: str | None, path: _Path) -> JsonValue:
    if value is None:
        return None
    if _needs_lexical_fallback(value):
        return cast(JsonValue, lexical)
    try:
        return scalar_to_relaxng_json(value, format="pyxsd")
    except PyXSDError as error:
        raise_conversion("scalar-type-invalid", json_pointer(path), str(error))


def _qname_candidates(value: AlternativeValue | ListInterpretation) -> bool:
    """Whether any surviving interpretation is an XSD QName or NOTATION."""
    if isinstance(value, AlternativeValue):
        candidates = tuple(value.candidates) + tuple(
            candidate
            for interpretation in value.interpretations
            for group in interpretation.token_candidates
            for candidate in group
        )
    else:
        candidates = tuple(candidate for group in value.token_candidates for candidate in group)
    return any(
        candidate.contract.kind == "atomic"
        and candidate.contract.type_name in ("QName", "NOTATION")
        for candidate in candidates
    )


def _lexical_qname_prefixes(value: AlternativeValue | ListInterpretation) -> list[tuple[str, str]]:
    """Original lexical prefixes and their scoped namespace URIs."""
    if not _qname_candidates(value):
        return []
    tokens: list[str] = []
    if isinstance(value, AlternativeValue):
        if any(_atomic_qname(c.contract) for c in value.candidates):
            tokens.append(value.lexical.strip())
        interpretations = value.interpretations
    else:
        interpretations = (value,)
    for interpretation in interpretations:
        for span, candidates in zip(
            interpretation.token_spans, interpretation.token_candidates, strict=True
        ):
            if any(_atomic_qname(c.contract) for c in candidates):
                tokens.append(span.text)
    prefixes: list[tuple[str, str]] = []
    for token in tokens:
        prefix = token.split(":", 1)[0] if ":" in token else ""
        uri = value.context.namespaces.get(prefix)
        if uri:
            prefixes.append((prefix, uri))
    return prefixes


def _declare_wrapper_qnames(
    value: AlternativeValue | ListInterpretation,
    *,
    names: _LexicalNames,
    path: _Path,
) -> None:
    for prefix, uri in _lexical_qname_prefixes(value):
        names.declare(prefix, uri, path)


def _parker_wrapper_qnames(
    value: AlternativeValue | ListInterpretation,
    *,
    fidelity: Fidelity,
    path: _Path,
) -> None:
    for prefix, _uri in _lexical_qname_prefixes(value):
        label = "default namespace" if prefix == "" else f"prefix {prefix!r}"
        _loss(
            "namespace-lost",
            path,
            f"Parker cannot carry the namespace of QName {label}",
            fidelity,
        )


def _conventional_scalar(
    value: Any,
    lexical: str | None,
    *,
    format: FormatName,
    fidelity: Fidelity,
    path: _Path,
    names: _LexicalNames | None = None,
) -> JsonValue:
    """Convert one conventional-format scalar without inventing metadata."""
    if isinstance(value, AlternativeValue):
        if len(value.candidates) == 1 and value.candidates[0].decodable:
            value = value.candidates[0].decoded
        else:
            if format == "parker":
                _parker_wrapper_qnames(value, fidelity=fidelity, path=path)
            elif names is not None:
                _declare_wrapper_qnames(value, names=names, path=path)
            return value.lexical
    elif isinstance(value, ListInterpretation):
        if format == "parker":
            _parker_wrapper_qnames(value, fidelity=fidelity, path=path)
        elif names is not None:
            _declare_wrapper_qnames(value, names=names, path=path)
        return value.lexical
    if isinstance(value, (tuple, list)):
        if any(_needs_lexical_fallback(item) for item in value):
            return cast(JsonValue, lexical)
        return [
            _conventional_scalar(
                item,
                lexical,
                format=format,
                fidelity=fidelity,
                path=(*path, index),
                names=names,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, QName):
        if format == "parker":
            return _parker_qname(value, fidelity=fidelity, path=path)
        return _lexical_qname(value, path=path, names=cast(_LexicalNames, names))
    if isinstance(value, (bytes, bytearray)):
        return cast(JsonValue, lexical)
    try:
        return scalar_to_json(value, format=format)
    except PyXSDError as error:
        raise_conversion("scalar-type-invalid", json_pointer(path), str(error))


def _parker_qname(value: QName, *, fidelity: Fidelity, path: _Path) -> str:
    try:
        expanded = value.to_expanded_name(strict=True)
    except ValueError:
        _loss(
            "namespace-lost",
            path,
            f"Parker cannot carry the namespace of xs:QName {str(value)!r}",
            fidelity,
        )
        return str(value)
    if expanded.startswith("{"):
        _loss(
            "namespace-lost",
            path,
            f"Parker cannot carry the namespace of xs:QName {str(value)!r}",
            fidelity,
        )
        return str(value)
    return expanded


def _lexical_qname(value: QName, *, path: _Path, names: _LexicalNames) -> str:
    try:
        expanded = value.to_expanded_name(strict=True)
    except ValueError as error:
        raise_conversion("namespace-unresolved", json_pointer(path), str(error))
    if not expanded.startswith("{"):
        return expanded
    return names.lexical(expanded)


class _LexicalNames:
    """Shared generated prefixes, with authored bindings scoped to each node."""

    __slots__ = ("_exact", "_frames", "_prefixes", "_reserved", "_used")

    def __init__(self, known_names: list[str], reserved: set[str] | None = None) -> None:
        self._reserved = set(reserved or ())
        self._prefixes: dict[str, str] = {}
        self._used: dict[str, str] = {}
        self._frames: dict[RelaxNGElement, dict[str, str]] = {}
        self._exact: dict[str, str] = {}
        index = 0
        for uri in sorted({uri for name in known_names if (uri := _split(name)[0])}):
            while f"ns{index}" in self._reserved:
                index += 1
            self._prefixes[uri] = f"ns{index}"
            index += 1

    def at(self, node: RelaxNGElement) -> _LexicalNames:
        scoped = object.__new__(_LexicalNames)
        scoped._prefixes, scoped._used = self._prefixes, self._used
        scoped._reserved, scoped._frames = self._reserved, self._frames
        scoped._exact = dict(self._frames.get(node, {}))
        return scoped

    def lexical(self, expanded_name: str, *, attribute: bool = False) -> str:
        uri, local = _split(expanded_name)
        if not uri:
            return local
        if uri == XML_NS:
            return f"xml:{local}"
        prefix = self._prefixes.get(uri)
        if prefix is None:
            prefix = "ns_" + uri.encode("utf-8").hex()
            while prefix in self._reserved:
                prefix += "_"
            self._prefixes[uri] = prefix
        self._used[prefix] = uri
        return f"{prefix}:{local}"

    def declare(self, prefix: str, uri: str, path: _Path) -> None:
        """Keep an original lexical prefix binding for a surviving QName."""
        if prefix == "xml":
            if uri != XML_NS:
                raise_conversion(
                    "namespace-conflict",
                    json_pointer(path),
                    "the implicit xml prefix cannot be rebound",
                )
            return
        existing = self._exact.get(prefix)
        if existing is not None and existing != uri:
            raise_conversion(
                "namespace-conflict",
                json_pointer(path),
                f"QName prefix {prefix!r} denotes both {existing!r} and {uri!r}",
            )
        generated = self._used.get(prefix)
        if generated is not None and generated != uri:
            raise_conversion(
                "namespace-conflict",
                json_pointer(path),
                f"QName prefix {prefix!r} collides with generated namespace {generated!r}",
            )
        self._exact[prefix] = uri

    def declarations(self) -> dict[str, str]:
        declarations = dict(self._used)
        for prefix, uri in self._exact.items():
            existing = declarations.get(prefix)
            if existing is not None and existing != uri:
                raise_conversion(
                    "namespace-conflict",
                    "",
                    f"QName prefix {prefix!r} collides with generated namespace {existing!r}",
                )
            declarations[prefix] = uri
        return dict(sorted(declarations.items()))


def _wrapper_bindings(value: Any) -> dict[str, str]:
    if isinstance(value, (AlternativeValue, ListInterpretation)):
        return dict(_lexical_qname_prefixes(value))
    if isinstance(value, (tuple, list)):
        return {p: uri for item in value for p, uri in _wrapper_bindings(item).items()}
    return {}


def _namespace_names(
    document: RelaxNGDocument, route: RouteContract | None, plan: ProjectionPlan, *, lexical: bool
) -> _LexicalNames:
    """Reserve authored prefixes before allocating any generated name.

    Lexical escape exports retain authoritative frames, not stale evidence.
    Typed exports need authored bindings only where wrapper text survives.
    """
    nodes = [document.root]
    reserved: set[str] = set()
    while nodes:
        node = nodes.pop()
        reserved.update(node.namespaces or {})
        nodes.extend(node.children)
    names = _LexicalNames(_known_names(plan), reserved)
    pending = [(document.root, route, False, "")]
    while pending:
        node, node_route, generic, inherited_default = pending.pop()
        frame = dict(node.namespaces or {})
        if lexical:
            bindings = {p: uri for p, uri in frame.items() if p != "xml"}
        else:
            bindings = {}
            for name, _ in _data_attributes(node):
                bindings.update(_wrapper_bindings(node.attribute_value(name)))
            if _layout(node_route, generic) == "scalar" or not node.children:
                bindings.update(_wrapper_bindings(_whole_element_scalar_value(node)))
        # A default binding belongs only on its owning element. A lexical
        # unnamespaced descendant also needs the ordinary XML default reset.
        if inherited_default and "" not in bindings:
            bindings[""] = frame.get("", "")
        if bindings.get("") and not _split(node._name_)[0]:
            raise_conversion(
                "namespace-conflict",
                "",
                "a default QName namespace cannot qualify an unnamespaced element",
            )
        names._frames[node] = bindings
        current_default = bindings.get("", inherited_default)
        for child in node.children:
            child_route, child_generic = _child_route(node_route, child, plan)
            pending.append((child, child_route, child_generic, current_default))
    return names


def _name_class_names(name_class: NameClass) -> list[str]:
    """Finite expanded names mentioned by a compiled name class."""
    if isinstance(name_class, Name):
        return [f"{{{name_class.uri}}}{name_class.local}" if name_class.uri else name_class.local]
    if isinstance(name_class, NameChoice):
        names: list[str] = []
        for alternative in name_class.alternatives:
            names.extend(_name_class_names(alternative))
        return names
    if isinstance(name_class, ExceptName):
        return _name_class_names(name_class.base) + _name_class_names(name_class.excluded)
    if isinstance(name_class, NsName) and name_class.uri:
        return [f"{{{name_class.uri}}}{_WILDCARD_URI_SEED}"]
    # AnyName, an absent NsName URI, and any other open class mention no
    # finite expanded name.
    return []


def _known_names(plan: ProjectionPlan) -> list[str]:
    """Compiled element and attribute names used to seed deterministic prefixes.

    The ``xml`` prefix is implicit and never allocated, so its namespace does
    not consume a deterministic generated prefix.
    """
    names: set[str] = set()
    for route in plan.routes.values():
        names.update(_name_class_names(route.name_class))
        if route.expanded_name is not None:
            names.add(route.expanded_name)
        names.update(route.attributes)
        names.update(route.children)
        for wildcard in route.wildcards:
            names.update(_name_class_names(wildcard.name_class))
    return sorted(name for name in names if _split(name)[0] != XML_NS)


def _typed_scalar(node: RelaxNGElement) -> Any:
    """Return the schema-directed scalar value at one scalar position."""
    return _whole_element_scalar_value(node)


def _typed_attribute(node: RelaxNGElement, name: str, lexical: str, *, typed: bool) -> Any:
    if not typed:
        return lexical
    value = node.attribute_value(name)
    return lexical if value is None else value


def _iter_qname_values(value: Any) -> list[QName]:
    """Collect unique QName values from a typed scalar, including a list."""
    found: list[QName] = []
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, QName):
            found.append(item)
        elif isinstance(item, (tuple, list)):
            pending.extend(item)
    return found


def _child_paths(
    node: RelaxNGElement, path: _Path, always_list: bool
) -> Iterator[tuple[RelaxNGElement, _Path]]:
    """Address native child values in their actual grouped representation."""
    counts = Counter(child._name_ for child in node.children)
    indexes: Counter[str] = Counter()
    for child in node.children:
        name = child._name_
        child_path = (*path, name)
        if always_list or counts[name] > 1:
            child_path = (*child_path, indexes[name])
        indexes[name] += 1
        yield child, child_path


_Counts = tuple[int, ...]
_Sequence = tuple[str, ...]


@dataclass
class _OrderAnalysis:
    """One bounded, parse-local dynamic program; never enumerate derivations."""

    names: tuple[str, ...]
    path: _Path
    remaining: int = 100_000
    sequences: dict[tuple[int, _Counts], set[_Sequence]] = field(default_factory=dict)
    matches: dict[tuple[int, _Sequence], bool] = field(default_factory=dict)

    def step(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise_conversion(
                "order-ambiguous",
                json_pointer(self.path),
                "native child-order reconstruction exceeded its work budget",
            )

    def splits(self, counts: _Counts) -> Iterator[tuple[_Counts, _Counts]]:
        for left in product(*(range(count + 1) for count in counts)):
            self.step()
            yield left, tuple(total - part for total, part in zip(counts, left, strict=True))


def _order_parts(
    parts: tuple[ContentContract, ...],
    counts: _Counts,
    analysis: _OrderAnalysis,
    *,
    unordered: bool,
) -> set[_Sequence]:
    if len(parts) == 1:
        return _model_order(parts[0], counts, analysis)
    result: set[_Sequence] = set()
    for left, right in analysis.splits(counts):
        first = _model_order(parts[0], left, analysis)
        if not first:
            continue
        rest = _order_parts(parts[1:], right, analysis, unordered=unordered)
        for a in first:
            for b in rest:
                result.add(_interleave_sequence(a, b) if unordered else a + b)
                if len(result) == 2:
                    return result
    return result


def _interleave_sequence(first: _Sequence, second: _Sequence) -> _Sequence:
    """Canonical shuffle, preserving each operand's group/repeat precedence.

    Choosing the least available head is associative and commutative across
    the disjoint name classes required of compiled interleave operands. Sorting
    concatenated blocks instead would retain the arbitrary binary association.
    """
    merged: list[str] = []
    left = right = 0
    while left < len(first) and right < len(second):
        if first[left] < second[right]:
            merged.append(first[left])
            left += 1
        else:
            merged.append(second[right])
            right += 1
    return (*merged, *first[left:], *second[right:])


def _model_order(
    model: ContentContract,
    counts: _Counts,
    analysis: _OrderAnalysis,
) -> set[_Sequence]:
    """At most two distinct reconstructions for these exact observed counts.

    Empty repeat iterations contribute no additional sequences. Nonempty
    iterations strictly decrease the remaining counts, including nullable
    operands; shared model/count states are evaluated only once.
    """
    key = id(model), counts
    if key in analysis.sequences:
        return analysis.sequences[key]
    analysis.step()
    result: set[_Sequence] = set()
    if model.kind == "child":
        if sum(counts) == 1:
            name = analysis.names[counts.index(1)]
            if cast(NameClass, model.name_class).contains(*_split(name)):
                result.add((name,))
    elif model.kind in ("attribute", "empty", "scalar"):
        if not any(counts):
            result.add(())
    elif model.kind == "choice":
        for part in model.operands:
            result.update(_model_order(part, counts, analysis))
            if len(result) >= 2:
                break
    elif model.kind in ("group", "interleave"):
        result = _order_parts(
            model.operands, counts, analysis, unordered=model.kind == "interleave"
        )
    elif model.kind == "repeat":
        result = _repeat_orders(model, counts, analysis)
    # notAllowed has no accepted child sequence.
    result = set(sorted(result)[:2])
    analysis.sequences[key] = result
    return result


def _repeat_orders(
    model: ContentContract,
    counts: _Counts,
    analysis: _OrderAnalysis,
) -> set[_Sequence]:
    # Lexicographic subcounts precede any vector obtained by adding a
    # nonempty iteration. Compute that table bottom-up, not by child recursion.
    for target, _ in analysis.splits(counts):
        key = id(model), target
        if key in analysis.sequences:
            continue
        if not any(target):
            result = _model_order(model.operands[0], target, analysis)
        else:
            result = set()
            for left, right in analysis.splits(target):
                if not any(left):
                    continue
                first = _model_order(model.operands[0], left, analysis)
                rest = analysis.sequences[id(model), right] if any(right) else {()}
                result.update(a + b for a in first for b in rest)
                if len(result) >= 2:
                    break
        analysis.sequences[key] = set(sorted(result)[:2])
    return analysis.sequences[id(model), counts]


def _match_parts(
    parts: tuple[ContentContract, ...],
    observed: _Sequence,
    analysis: _OrderAnalysis,
    *,
    unordered: bool,
) -> bool:
    if len(parts) == 1:
        return _order_matches(parts[0], observed, analysis)
    partitions = (
        (
            (
                tuple(name for name, keep in zip(observed, mask, strict=True) if keep),
                tuple(name for name, keep in zip(observed, mask, strict=True) if not keep),
            )
            for mask in product((False, True), repeat=len(observed))
        )
        if unordered
        else ((observed[:index], observed[index:]) for index in range(len(observed) + 1))
    )
    for first, rest in partitions:
        analysis.step()
        if _order_matches(parts[0], first, analysis) and _match_parts(
            parts[1:], rest, analysis, unordered=unordered
        ):
            return True
    return False


def _order_matches(
    model: ContentContract,
    observed: _Sequence,
    analysis: _OrderAnalysis,
) -> bool:
    """Check the authored sequence, not just its multiset, including interleave."""
    key = id(model), observed
    if key in analysis.matches:
        return analysis.matches[key]
    analysis.step()
    if model.kind == "child":
        result = len(observed) == 1 and cast(NameClass, model.name_class).contains(
            *_split(observed[0])
        )
    elif model.kind in ("attribute", "empty", "scalar"):
        result = not observed
    elif model.kind == "choice":
        result = any(_order_matches(part, observed, analysis) for part in model.operands)
    elif model.kind in ("group", "interleave"):
        result = _match_parts(
            model.operands, observed, analysis, unordered=model.kind == "interleave"
        )
    elif model.kind == "repeat":
        reachable = {0}
        for end in range(1, len(observed) + 1):
            for start in range(end):
                analysis.step()
                if start in reachable and _order_matches(
                    model.operands[0], observed[start:end], analysis
                ):
                    reachable.add(end)
                    break
        result = (
            len(observed) in reachable
            if observed
            else _order_matches(model.operands[0], (), analysis)
        )
    else:
        result = False
    analysis.matches[key] = result
    return result


def _native_order(
    route: RouteContract | None, children: tuple[RelaxNGElement, ...], path: _Path
) -> None:
    if route is None:
        if children:
            raise_conversion("order-ambiguous", json_pointer(path), "no compiled child order")
        return
    observed = tuple(child._name_ for child in children)
    counts = Counter(observed)
    analysis = _OrderAnalysis(tuple(sorted(counts)), path)
    sequences = _model_order(
        route.content.model, tuple(counts[n] for n in analysis.names), analysis
    )
    if len(sequences) == 1 and (
        observed in sequences or _order_matches(route.content.model, observed, analysis)
    ):
        return
    seen: Counter[str] = Counter()
    previous: str | None = None
    for name in observed:
        if name != previous and seen[name]:
            path = (*path, name, seen[name])
            break
        seen[name] += 1
        previous = name
    raise_conversion(
        "order-ambiguous",
        json_pointer(path),
        "grouped properties do not determine one accepted compiled child order",
    )


def _atomic_qname(contract: ScalarContract) -> bool:
    return (
        contract.kind == "atomic"
        and contract.type_name in ("QName", "NOTATION")
        and contract.library_uri == "http://www.w3.org/2001/XMLSchema-datatypes"
    )


def _contract_qname(contract: ScalarContract, plan: ProjectionPlan) -> bool:
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


def _lexical_bindings(node: RelaxNGElement, lexical: str) -> dict[str, str]:
    """Only bindings used by lexical QName tokens, including the default."""
    frame = node.namespaces or {}
    prefixes = {token.split(":", 1)[0] if ":" in token else "" for token in lexical.split()}
    return {
        prefix: frame[prefix]
        for prefix in sorted(prefixes)
        if frame.get(prefix) and prefix != "xml"
    }


def _native_lexical_context(
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    fidelity: Fidelity,
    audit: _LexicalQNameAudit,
) -> None:
    if route is None:
        return
    slots: list[tuple[str, tuple[ScalarContract, ...], _Path, str | None]] = [
        (
            node.text_value() or "",
            _position_scalar_contracts(route.content.model, None),
            _native_scalar_path(node, path),
            None,
        )
    ]
    for name, lexical in _data_attributes(node):
        contracts = _position_scalar_contracts(route.content.model, name)
        slots.append((lexical, contracts, (*path, "@" + name), name))
    for lexical, contracts, pointer, slot_name in slots:
        if not _lexical_bindings(node, lexical) or not any(
            _contract_qname(contract, plan) for contract in contracts
        ):
            continue
        message = audit.loss(node, slot_name, lexical, contracts, plan)
        if message is not None:
            _loss(
                "namespace-lost",
                pointer,
                message,
                fidelity,
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
            if name is None or cast(NameClass, part.name_class).contains(*_split(name)):
                contracts.update(dict.fromkeys(part.scalar_contracts))
        else:
            pending.extend(part.operands)
    return tuple(contracts)


@dataclass
class _LexicalQNameAudit:
    """Accepting whole-document witnesses, mapped back to exact caller nodes."""

    document: RelaxNGDocument
    losses: set[tuple[RelaxNGElement, str | None]] | None = None
    unavailable: str | None = None

    def _load(self) -> None:
        self.losses = set()
        try:
            if _typed_available(self.document):
                view = self.document
            else:
                # Revalidation already clones, preserves lexical frames/DTD
                # evidence and instance limits, and enters the schema parse
                # guard. Change only a disposable request, never the caller.
                request = copy(self.document)
                request.typed = True
                request._capture = "none"
                request._xdm_requested = False
                view = request.revalidate()
        except (PyXSDError, RuntimeError) as error:
            self.unavailable = f"whole-document QName audit failed: {error}"
            return
        if view.report.has_errors:
            self.unavailable = "current tree has no accepting whole-document derivation"
            return
        graph = view.binding_evidence
        if graph is None:
            self.unavailable = "whole-document accepting evidence is unavailable"
            return
        originals: dict[RelaxNGElement, RelaxNGElement] = {}
        pending = [(self.document.root, view.root)]
        while pending:
            original, audited = pending.pop()
            originals[audited] = original
            pending.extend(zip(original.children, audited.children, strict=True))
        for edge in graph.retained_edges:
            for witness in edge.witnesses:
                if not _atomic_qname(scalar_contract(witness.pattern)) or not _lexical_bindings(
                    witness.occurrence.element, witness.lexical
                ):
                    continue
                site = witness.occurrence
                if site.kind == "token":
                    site = cast(EvidenceOccurrence, site.parent)
                name = site.name if site.kind == "attribute" else None
                self.losses.add((originals[site.element], name))

    def loss(
        self,
        node: RelaxNGElement,
        name: str | None,
        lexical: str,
        contracts: tuple[ScalarContract, ...],
        plan: ProjectionPlan,
    ) -> str | None:
        if self.losses is None:
            self._load()
        if self.unavailable is not None:
            # Without an accepting document, local matching can only establish
            # potential loss, never a surviving interpretation. Retain its
            # name/token precision and make the unavailable proof explicit.
            if _potential_qname_loss(self.document, node, lexical, contracts, plan):
                return f"native lexical values may lose QName context; {self.unavailable}"
            return None
        if (node, name) in cast(set[tuple[RelaxNGElement, str | None]], self.losses):
            return "native lexical values cannot carry QName context"
        return None


def _potential_qname_loss(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    lexical: str,
    contracts: tuple[ScalarContract, ...],
    plan: ProjectionPlan,
) -> bool:
    """Bound local potential loss only when whole-document evidence is unavailable.

    This cannot prove branch survival. A positive result must be reported with
    the whole-document audit failure, never as an accepting QName candidate.
    """
    for contract in contracts:
        if not _contract_qname(contract, plan):
            continue
        source = ET.Element("scalar")
        source.text = lexical
        scalar = RelaxNGElement.from_etree(source, base_uri=node.base_uri)
        scalar.set_namespace_bindings(node.namespaces or {})
        pattern = Element(Name(None, "scalar"), plan.encoding_patterns[contract.pattern_identity])
        limits = document.schema.limits
        if not validate_element(scalar, pattern, limits=limits).valid:
            continue
        proof = build_binding_evidence(
            scalar,
            pattern,
            datatype_context=DatatypeContext(node.base_uri, node.namespaces or {}),
            limits=limits,
        )
        for edge in proof.retained_edges:
            for witness in edge.witnesses:
                if _atomic_qname(scalar_contract(witness.pattern)) and _lexical_bindings(
                    node, witness.lexical
                ):
                    return True
    return False


# ---------------------------------------------------------------------------
# Native pyxsd
# ---------------------------------------------------------------------------


def _native_scalar_path(node: RelaxNGElement, path: _Path) -> _Path:
    """A leaf child is the value itself; roots and objects own a '$' member."""
    return (
        (*path, _TEXT_KEY)
        if node.parent is None or node.children or _data_attributes(node)
        else path
    )


def _native_scalar_evidence(document: RelaxNGDocument, node: RelaxNGElement) -> bool:
    """Distinguish accepted empty scalars from skipped/insignificant text."""
    graph = cast(EvidenceGraph, document.binding_evidence)
    return any(
        site.element is node
        and site.kind == "text"
        and site.content_index == 0
        and graph.scalar_witnesses_at(site)
        for site in graph.occurrences
    )


def export_native(
    document: RelaxNGDocument,
    *,
    fidelity: Fidelity,
    typed: bool,
    always_list: bool,
    mode: ExportMode,
) -> dict[str, Any]:
    """Export the root under the grouped native structure."""
    plan = document.schema.projection
    route = _root_or_none(document, require=typed)
    root = document.root
    _check_native_strict(
        document,
        root,
        route,
        plan,
        (),
        typed=typed,
        mode=mode,
        fidelity=fidelity,
        always_list=always_list,
        audit=_LexicalQNameAudit(document),
    )
    layout = _layout(route, False)
    if root.children or _data_attributes(root):
        return _native_object(
            document,
            root,
            route,
            plan,
            (),
            typed=typed,
            always_list=always_list,
            mode=mode,
            generic=False,
        )
    return {_TEXT_KEY: _native_leaf(document, root, (), layout=layout, typed=typed, mode=mode)}


def _check_native_strict(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    *,
    typed: bool,
    mode: ExportMode,
    audit: _LexicalQNameAudit,
    generic: bool = False,
    fidelity: Fidelity = "strict",
    always_list: bool = False,
) -> None:
    """Reject native grouping losses before any representation is built."""
    layout = _layout(route, generic)
    children = node.children
    if children:
        text = node.text_value()
        tails = [child.tail_value() or "" for child in children]
        if (layout == "mixed" and (text or any(tails)) and fidelity == "strict") or (
            not typed and layout != "mixed" and any(tails)
        ):
            _loss(
                "mixed-content",
                path,
                "native export groups character data under '$' and loses "
                "its position among child elements",
                fidelity,
            )

    if fidelity == "strict":
        _native_order(route, children, path)
    if typed and fidelity == "strict":
        _check_native_qnames(node.scalar_value(), _native_scalar_path(node, path), mode=mode)
        for name, _lexical in _data_attributes(node):
            _check_native_qnames(node.attribute_value(name), (*path, "@" + name), mode=mode)
    elif not typed:
        _native_lexical_context(node, route, plan, path, fidelity, audit)

    for child, child_path in _child_paths(node, path, always_list):
        child_route, child_generic = _node_layout(route, child, plan)
        _check_native_strict(
            document,
            child,
            child_route,
            plan,
            child_path,
            typed=typed,
            mode=mode,
            generic=child_generic,
            fidelity=fidelity,
            always_list=always_list,
            audit=audit,
        )


def _check_native_qnames(value: Any, path: _Path, *, mode: ExportMode) -> None:
    if mode == "raw" or isinstance(value, (AlternativeValue, ListInterpretation)):
        return
    for qname in _iter_qname_values(value):
        try:
            expanded = qname.to_expanded_name(strict=True)
        except ValueError:
            expanded = "{unresolved}"
        if expanded.startswith("{"):
            raise_conversion(
                "namespace-lost",
                json_pointer(path),
                f"native export cannot carry the namespace of xs:QName {str(qname)!r}",
            )


def _native_leaf(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    path: _Path,
    *,
    layout: str,
    typed: bool,
    mode: ExportMode,
) -> Any:
    text = node.text_value()
    if not typed:
        return "" if text is None else text
    if text is None and layout not in ("scalar", "mixed"):
        return None
    value = _typed_scalar(node)
    if mode == "raw":
        return value
    return _native_json_scalar(value, text, path)


def _native_object(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    *,
    typed: bool,
    always_list: bool,
    mode: ExportMode,
    generic: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, lexical in _data_attributes(node):
        value = _typed_attribute(node, name, lexical, typed=typed)
        if mode == "raw" or not typed:
            result["@" + name] = value
        else:
            result["@" + name] = _native_json_scalar(value, lexical, (*path, "@" + name))

    groups: dict[str, list[Any]] = {}
    for child, child_path in _child_paths(node, path, always_list):
        child_route, child_generic = _node_layout(route, child, plan)
        child_layout = _layout(child_route, child_generic)
        if child.children or _data_attributes(child):
            exported = _native_object(
                document,
                child,
                child_route,
                plan,
                child_path,
                typed=typed,
                always_list=always_list,
                mode=mode,
                generic=child_generic,
            )
        else:
            exported = _native_leaf(
                document,
                child,
                child_path,
                layout=child_layout,
                typed=typed,
                mode=mode,
            )
        groups.setdefault(child._name_, []).append(exported)
    for name, values in groups.items():
        result[name] = values if always_list else (values[0] if len(values) == 1 else values)

    layout = _layout(route, generic)
    if layout == "scalar" or (
        typed and not node.children and _native_scalar_evidence(document, node)
    ):
        result[_TEXT_KEY] = _native_leaf(
            document, node, (*path, _TEXT_KEY), layout=layout, typed=typed, mode=mode
        )
    elif layout == "mixed":
        pieces: list[str] = []
        text = node.text_value()
        if text is not None:
            pieces.append(text)
        for child in node.children:
            tail = child.tail_value()
            if tail is not None:
                pieces.append(tail)
        if pieces:
            result[_TEXT_KEY] = "".join(pieces)
    else:
        text = node.text_value()
        if text is not None and text.strip():
            result[_TEXT_KEY] = text
    return result


# ---------------------------------------------------------------------------
# Parker
# ---------------------------------------------------------------------------


def export_parker(document: RelaxNGDocument, *, fidelity: Fidelity) -> JsonValue:
    """Export the root under Parker's root-absorbed grouped structure."""
    plan = document.schema.projection
    route = _root_route(document.root, plan, None)
    return _parker_node(document, document.root, route, plan, (), fidelity=fidelity)


def _parker_node(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    *,
    fidelity: Fidelity,
    generic: bool = False,
) -> JsonValue:
    children = node.children
    layout = _layout(route, generic)

    for child in children:
        name = child._name_
        if name.startswith("{"):
            _loss(
                "namespace-lost",
                (*path, _local_name(name)),
                f"Parker cannot carry the namespace of element {name!r}",
                fidelity,
            )
    _parker_name_losses(node, children, path, fidelity)

    if _data_attributes(node):
        _loss(
            "attributes-lost",
            path,
            f"Parker cannot carry the attributes of element {node._name_!r}",
            fidelity,
        )

    if layout == "empty":
        if children or _has_non_whitespace_content(node):
            _container_error(
                path,
                f"empty content for {node._name_!r} has no container "
                "for observed text or child elements",
            )
        return {}

    if layout == "scalar":
        if children:
            _container_error(
                path,
                f"simple content for {node._name_!r} cannot carry child elements",
            )
        return _parker_text(document, node, path, fidelity=fidelity)

    if layout == "mixed":
        if not children:
            text = node.text_value()
            if text is None or text == "":
                return {}
            return _parker_text(document, node, path, fidelity=fidelity)
        text = _mixed_text(node)
        if text:
            _loss(
                "mixed-content",
                path,
                f"Parker cannot retain the text positions of element {node._name_!r}",
                fidelity,
            )
        _check_mixed_order(route, node, path, fidelity)
        result: dict[str, JsonValue] = {}
        if text:
            result[_TEXT_KEY] = text
        result.update(_parker_children(document, node, route, plan, path, fidelity=fidelity))
        return result

    if _has_non_whitespace_content(node):
        _container_error(
            path,
            f"element-only content {node._name_!r} cannot carry non-whitespace text",
        )
    if layout == "sequence":
        _check_grouped_order(route, children, path, fidelity)
    return _parker_children(document, node, route, plan, path, fidelity=fidelity)


def _parker_name_losses(
    node: RelaxNGElement,
    children: tuple[RelaxNGElement, ...],
    path: _Path,
    fidelity: Fidelity,
) -> None:
    observed: dict[str, set[str]] = {}
    for child in children:
        observed.setdefault(_local_name(child._name_), set()).add(child._name_)
    for local, names in observed.items():
        if len(names) > 1:
            _loss(
                "name-ambiguous",
                (*path, local),
                f"Parker cannot reconstruct the local child name {local!r} from {sorted(names)!r}",
                fidelity,
            )


def _parker_text(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    path: _Path,
    *,
    fidelity: Fidelity,
) -> JsonValue:
    value = _typed_scalar(node) if _typed_available(document) else None
    lexical = node.text_value()
    if value is None:
        return "" if lexical is None else lexical
    return _conventional_scalar(
        value,
        lexical,
        format="parker",
        fidelity=fidelity,
        path=path,
    )


def _parker_children(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    *,
    fidelity: Fidelity,
) -> dict[str, JsonValue]:
    groups: dict[str, list[RelaxNGElement]] = {}
    for child in node.children:
        groups.setdefault(_local_name(child._name_), []).append(child)

    plan_order: list[str] = []
    if route is not None:
        for name in route.children:
            local = _local_name(name)
            if local not in plan_order:
                plan_order.append(local)
    keys = [key for key in plan_order if key in groups]
    keys.extend(key for key in groups if key not in plan_order)

    result: dict[str, JsonValue] = {}
    for key in keys:
        group = groups[key]
        values: list[JsonValue] = []
        for index, child in enumerate(group):
            child_path = (*path, key, index) if len(group) > 1 else (*path, key)
            child_route, child_generic = _node_layout(route, child, plan)
            values.append(
                _parker_node(
                    document,
                    child,
                    child_route,
                    plan,
                    child_path,
                    fidelity=fidelity,
                    generic=child_generic,
                )
            )
        result[key] = values if len(values) > 1 else values[0]
    return result


# ---------------------------------------------------------------------------
# BadgerFish
# ---------------------------------------------------------------------------


def export_badgerfish(document: RelaxNGDocument, *, fidelity: Fidelity) -> JsonValue:
    """Export a root-retained BadgerFish object with deterministic prefixes."""
    plan = document.schema.projection
    route = _root_route(document.root, plan, None)
    names = _namespace_names(document, route, plan, lexical=not _typed_available(document))
    root_name = names.lexical(document.root._name_)
    value = _badgerfish_node(
        document,
        document.root,
        route,
        plan,
        (root_name,),
        fidelity=fidelity,
        names=names,
    )
    declarations = names.declarations()
    if declarations:
        declaration_values: dict[str, JsonValue] = dict(declarations)
        if isinstance(value, dict):
            declaration_values.update(cast(dict[str, JsonValue], value.get(_XMLNS_KEY, {})))
            value = {key: item for key, item in value.items() if key != _XMLNS_KEY}
            value = {_XMLNS_KEY: declaration_values, **value}
        else:
            value = {_XMLNS_KEY: declaration_values, _TEXT_KEY: value}
    return {root_name: value}


def _badgerfish_node(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    *,
    fidelity: Fidelity,
    names: _LexicalNames,
    generic: bool = False,
) -> JsonValue:
    scoped = names.at(node)
    value = _badgerfish_value(
        document, node, route, plan, path, fidelity=fidelity, names=scoped, generic=generic
    )
    if scoped._exact:
        attrs: dict[str, JsonValue] = {_XMLNS_KEY: dict(scoped._exact)}
        if isinstance(value, dict):
            return {**attrs, **value}
        return {**attrs, _TEXT_KEY: value}
    return value


def _badgerfish_value(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    *,
    fidelity: Fidelity,
    names: _LexicalNames,
    generic: bool = False,
) -> JsonValue:
    children = node.children
    layout = _layout(route, generic)
    attrs = _badgerfish_attributes(document, node, route, path, names=names)

    if layout == "empty":
        if children or _has_non_whitespace_content(node):
            _container_error(
                path,
                f"empty content for {node._name_!r} has no container "
                "for observed text or child elements",
            )
        return attrs

    if layout == "scalar":
        if children:
            _container_error(
                path,
                f"simple content for {node._name_!r} cannot carry child elements",
            )
        value = _badgerfish_text(document, node, path, names=names)
        if attrs:
            attrs[_TEXT_KEY] = value
            return attrs
        return value

    if layout == "mixed":
        if not children:
            text = node.text_value()
            if text is None or text == "":
                return attrs
            value = _badgerfish_text(document, node, path, names=names)
            if attrs:
                attrs[_TEXT_KEY] = value
                return attrs
            return value
        text = _mixed_text(node)
        if text:
            _loss(
                "mixed-content",
                path,
                f"BadgerFish cannot retain the text positions of element {node._name_!r}",
                fidelity,
            )
        _check_mixed_order(route, node, path, fidelity)
        result = attrs
        if text:
            result[_TEXT_KEY] = text
        result.update(
            _badgerfish_children(document, node, route, plan, path, fidelity=fidelity, names=names)
        )
        return result

    if _has_non_whitespace_content(node):
        _container_error(
            path,
            f"element-only content {node._name_!r} cannot carry non-whitespace text",
        )
    if layout == "sequence":
        _check_grouped_order(route, children, path, fidelity)
    result = attrs
    result.update(
        _badgerfish_children(document, node, route, plan, path, fidelity=fidelity, names=names)
    )
    return result


def _badgerfish_text(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    path: _Path,
    *,
    names: _LexicalNames,
) -> JsonValue:
    value = _typed_scalar(node) if _typed_available(document) else None
    lexical = node.text_value()
    if value is None:
        return "" if lexical is None else lexical
    return _conventional_scalar(
        value,
        lexical,
        format="badgerfish",
        fidelity="strict",
        path=path,
        names=names,
    )


def _badgerfish_attributes(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    route: RouteContract | None,
    path: _Path,
    *,
    names: _LexicalNames,
) -> dict[str, JsonValue]:
    raw = dict(_data_attributes(node))
    names_order: list[str] = []
    if route is not None:
        names_order.extend(name for name in route.attributes if name in raw)
    names_order.extend(sorted(name for name in raw if name not in names_order))

    typed = _typed_available(document)
    result: dict[str, JsonValue] = {}
    for name in names_order:
        lexical = raw[name]
        value = _typed_attribute(node, name, lexical, typed=typed)
        key = names.lexical(name, attribute=True)
        result["@" + key] = _conventional_scalar(
            value,
            lexical,
            format="badgerfish",
            fidelity="strict",
            path=(*path, "@" + key),
            names=names,
        )
    return result


def _badgerfish_children(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    *,
    fidelity: Fidelity,
    names: _LexicalNames,
) -> dict[str, JsonValue]:
    groups: dict[str, list[RelaxNGElement]] = {}
    for child in node.children:
        groups.setdefault(child._name_, []).append(child)

    plan_order = list(route.children) if route is not None else []
    repeatable = (
        {name: field.cardinality.repeatable for name, field in route.children.items()}
        if route is not None
        else {}
    )
    plan_set = set(plan_order)
    ordered = [name for name in plan_order if name in groups]
    ordered.extend(name for name in groups if name not in plan_set)

    result: dict[str, JsonValue] = {}
    for name in ordered:
        group = groups[name]
        lexical = names.lexical(name)
        is_array = repeatable.get(name, False) or len(group) > 1
        values: list[JsonValue] = []
        for index, child in enumerate(group):
            child_path = (*path, lexical, index) if is_array else (*path, lexical)
            child_route, child_generic = _node_layout(route, child, plan)
            values.append(
                _badgerfish_node(
                    document,
                    child,
                    child_route,
                    plan,
                    child_path,
                    fidelity=fidelity,
                    names=names,
                    generic=child_generic,
                )
            )
        result[lexical] = values if is_array else values[0]
    return result


# ---------------------------------------------------------------------------
# JsonML
# ---------------------------------------------------------------------------


def export_jsonml(document: RelaxNGDocument, *, fidelity: Fidelity) -> JsonValue:
    """Export the root as an ordered JsonML array, including dirty trees."""
    del fidelity
    plan = document.schema.projection
    route = _root_or_none(document, require=False)
    typed = _typed_available(document)
    names = _namespace_names(document, route, plan, lexical=not typed)
    _reserve_jsonml_names(document.root, route, plan, names, typed=typed)
    declarations = names.declarations()
    result = _jsonml_node(
        document,
        document.root,
        route,
        plan,
        (),
        names=names,
        typed=typed,
        root_declarations=bool(declarations),
    )
    if declarations:
        attrs: dict[str, JsonValue] = {
            ("xmlns" if prefix == "" else f"xmlns:{prefix}"): uri
            for prefix, uri in declarations.items()
        }
        if len(result) > 1 and isinstance(result[1], dict):
            attrs.update(result[1])
            result[1] = attrs
        else:
            result.insert(1, attrs)
    return result


def _reserve_jsonml_names(
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    names: _LexicalNames,
    *,
    typed: bool,
    generic: bool = False,
) -> None:
    """Reserve every lexical name before root declarations shift positions."""
    names = names.at(node)
    names.lexical(node._name_)
    layout = _layout(route, generic)
    for name, _lexical in _data_attributes(node):
        names.lexical(name, attribute=True)
        if typed:
            _reserve_value_names(node.attribute_value(name), names)
    if typed and layout == "scalar":
        _reserve_value_names(_whole_element_scalar_value(node), names)
    for child in node.children:
        child_route, child_generic = _node_layout(route, child, plan)
        _reserve_jsonml_names(child, child_route, plan, names, typed=typed, generic=child_generic)


def _reserve_value_names(value: Any, names: _LexicalNames) -> None:
    if isinstance(value, (AlternativeValue, ListInterpretation)):
        _declare_wrapper_qnames(value, names=names, path=())
    elif isinstance(value, (tuple, list)):
        for item in value:
            _reserve_value_names(item, names)
    elif isinstance(value, QName):
        try:
            expanded = value.to_expanded_name(strict=True)
        except ValueError:
            return
        if expanded.startswith("{"):
            names.lexical(expanded)


def _jsonml_node(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    *,
    names: _LexicalNames,
    typed: bool,
    root_declarations: bool = False,
    generic: bool = False,
) -> list[JsonValue]:
    names = names.at(node)
    tag = names.lexical(node._name_)
    result: list[JsonValue] = [tag]
    attrs = _jsonml_attributes(document, node, route, path, names=names, typed=typed)
    attrs = {
        **{("xmlns" if p == "" else f"xmlns:{p}"): uri for p, uri in names._exact.items()},
        **attrs,
    }
    if attrs:
        result.append(attrs)
    shift = 1 if root_declarations and not attrs and not path else 0
    layout = _layout(route, generic)

    if typed and layout == "scalar":
        value = _whole_element_scalar_value(node)
        result.append(
            _conventional_scalar(
                value,
                node.text_value(),
                format="jsonml",
                fidelity="strict",
                path=(*path, len(result) + shift),
                names=names,
            )
        )
        return result

    text = node.text_value()
    if text is not None:
        result.append(text)
    for child in node.children:
        child_route, child_generic = _node_layout(route, child, plan)
        result.append(
            _jsonml_node(
                document,
                child,
                child_route,
                plan,
                (*path, len(result) + shift),
                names=names,
                typed=typed,
                generic=child_generic,
            )
        )
        tail = child.tail_value()
        if tail is not None:
            result.append(tail)
    return result


def _jsonml_attributes(
    document: RelaxNGDocument,
    node: RelaxNGElement,
    route: RouteContract | None,
    path: _Path,
    *,
    names: _LexicalNames,
    typed: bool,
) -> dict[str, JsonValue]:
    raw = dict(_data_attributes(node))
    names_order: list[str] = []
    if route is not None:
        names_order.extend(name for name in route.attributes if name in raw)
    names_order.extend(sorted(name for name in raw if name not in names_order))

    result: dict[str, JsonValue] = {}
    for name in names_order:
        lexical = raw[name]
        value = _typed_attribute(node, name, lexical, typed=typed)
        key = names.lexical(name, attribute=True)
        result[key] = _conventional_scalar(
            value,
            lexical,
            format="jsonml",
            fidelity="strict",
            path=(*path, 1, key),
            names=names,
        )
    return result
