"""Finite, schema-side structural contracts shared by projection adapters.

Routes are sets of reachable element assignments, restricted to a name class.
Their keys are compilation-owned structural ordinals, never instance paths or
evidence occurrences. Reaching a key again closes a recursive edge. Child names
partition each route into exact named views and one remaining wildcard view;
all assignments that can reach a view participate in its contract.

The content model retains choice, ordering, repetition, and scalar correlation.
Cardinality envelopes describe accessors, not independent validation rules.
The native pattern graph remains the authority on validity.
"""

from __future__ import annotations

import hashlib
import keyword
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from unicodedata import normalize

from pyxsd.relaxng.contracts import ScalarContract, scalar_contract
from pyxsd.relaxng.datatypes import UnsupportedDatatypeOperation
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.names import AnyName, ExceptName, Name, NameChoice, NameClass
from pyxsd.relaxng.origins import OriginTable
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Data,
    DataExcept,
    Element,
    Empty,
    Group,
    Interleave,
    List,
    NotAllowed,
    OneOrMore,
    Pattern,
    Ref,
    Text,
    Value,
)
from pyxsd.relaxng.simplify import CompiledGrammar

__all__ = [
    "Cardinality",
    "ContentContract",
    "ElementAssignment",
    "FieldContract",
    "FieldReference",
    "OrderedContentContract",
    "ProjectionOccurrence",
    "ProjectionPlan",
    "RouteContract",
    "WildcardContract",
    "plan_projections",
]

Role = Literal["attribute", "child"]
_SCALARS = (Text, Data, DataExcept, Value, List)

# Descriptors must not shadow the lexical tree's public API. Underscore-prefixed
# names are escaped separately, protecting its internal and Python special names.
_RESERVED = frozenset(
    [
        "revision",
        "parent",
        "children",
        "attributes",
        "namespaces",
        "base_uri",
        "source_location",
        "text_value",
        "tail_value",
        "attribute_lexical",
        "scalar_value",
        "attribute_value",
        "iter_content",
        "set_name",
        "set_text",
        "clear_text",
        "set_tail",
        "clear_tail",
        "set_attribute",
        "delete_attribute",
        "set_namespace_bindings",
        "set_base_uri",
        "append_child",
        "insert_child",
        "remove_child",
        "replace_child",
        "clear_children",
        "to_etree",
        "from_etree",
    ]
)


@dataclass(frozen=True)
class Cardinality:
    """Static count envelope; ``None`` is an unbounded maximum."""

    minimum: int
    maximum: int | None

    @property
    def repeatable(self) -> bool:
        """Whether an accessor must be a sequence, even when currently empty."""
        return self.maximum is None or self.maximum > 1


_ZERO = Cardinality(0, 0)


@dataclass(frozen=True)
class ProjectionOccurrence:
    """A planner-generated key for a finite set of schema assignments.

    Assignment ordinals are local to ``grammar_fingerprint`` and independent
    of document position, depth, lexical values, and source location.
    """

    grammar_fingerprint: str
    assignment_ids: tuple[int, ...]
    name_class: NameClass


@dataclass(frozen=True)
class FieldReference:
    """An exact named or wildcard view used by one content-model leaf."""

    role: Role
    expanded_name: str | None
    name_class: NameClass
    child_route_id: str | None


@dataclass(frozen=True)
class ContentContract:
    """A factored content/constraint expression, without expanding derivations.

    Field leaves reference public views. Their name class is retained separately
    on the leaf, so partitioning a finite name choice does not require choosing
    all its fields. ``assignment_id`` identifies the schema-side leaf: two
    same-name assignments may share a child route without losing their ordering
    or cross-field correlations in this expression.
    """

    kind: str
    operands: tuple[ContentContract, ...] = ()
    fields: tuple[FieldReference, ...] = ()
    scalar_contracts: tuple[ScalarContract, ...] = ()
    name_class: NameClass | None = None
    assignment_id: int | None = None
    generic_reason: str | None = None


@dataclass(frozen=True)
class OrderedContentContract:
    """Original XML-order view and, only for scalar-only content, its contracts."""

    ordered: bool
    mixed: bool
    scalar_contracts: tuple[ScalarContract, ...]
    model: ContentContract


@dataclass(frozen=True)
class FieldContract:
    """One alias with an expanded-name lookup key and schema-static value shape."""

    alias: str
    expanded_name: str
    cardinality: Cardinality
    scalar_contracts: tuple[ScalarContract, ...]
    child_route_id: str | None
    constraints: tuple[ContentContract, ...]


@dataclass(frozen=True)
class WildcardContract:
    """A finite open-name view, never an enumeration of Python descriptors."""

    role: Role
    name_class: NameClass
    cardinality: Cardinality
    scalar_contracts: tuple[ScalarContract, ...]
    child_route_id: str | None
    ordered: bool
    constraints: tuple[ContentContract, ...]


@dataclass(frozen=True)
class ElementAssignment:
    """One schema leaf's name/content correlation inside a merged route."""

    name_class: NameClass
    content: ContentContract


@dataclass(frozen=True)
class RouteContract:
    """All compatible assignments at one schema-static element route.

    ``assignments`` resolves child-model leaf ordinals to their original content
    constraints. Merging accessors must not erase, for example, that the first
    of two same-name children requires ``x`` and the second requires ``y``.
    This table is finite even when its child-route references are recursive.
    """

    route_id: str
    expanded_name: str | None
    name_class: NameClass
    attributes: Mapping[str, FieldContract]
    children: Mapping[str, FieldContract]
    content: OrderedContentContract
    wildcards: tuple[WildcardContract, ...]
    constraints: tuple[ContentContract, ...]
    generic_reason: str | None
    occurrence: ProjectionOccurrence
    origins: OriginTable
    assignments: Mapping[int, ElementAssignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        object.__setattr__(self, "children", MappingProxyType(dict(self.children)))
        object.__setattr__(self, "assignments", MappingProxyType(dict(self.assignments)))


@dataclass(frozen=True)
class ProjectionPlan:
    """Immutable finite route graph; selectors are IDs or unambiguous exact names."""

    roots: tuple[str, ...]
    routes: Mapping[str, RouteContract]
    grammar_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))

    def route_for_occurrence(self, occurrence: ProjectionOccurrence) -> str:
        """Resolve only a planner-generated schema key, never runtime evidence."""
        if not isinstance(occurrence, ProjectionOccurrence):
            raise TypeError("occurrence must be a ProjectionOccurrence")
        route_id = _route_id(occurrence)
        if route_id not in self.routes or self.routes[route_id].occurrence != occurrence:
            raise KeyError(occurrence)
        return route_id

    def resolve_route(self, selector: str) -> RouteContract:
        """Resolve an opaque route ID or a unique exact expanded element name."""
        if selector in self.routes:
            return self.routes[selector]
        matches = [r for r in self.routes.values() if r.expanded_name == selector]
        if not matches:
            raise KeyError(selector)
        if len(matches) != 1:
            raise ValueError(f"ambiguous RELAX NG projection route {selector!r}; use a route ID")
        return matches[0]


def _digest(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _route_id(occurrence: ProjectionOccurrence) -> str:
    return "pyxsd.relaxng.route/1:" + _digest(
        (occurrence.grammar_fingerprint, occurrence.assignment_ids, occurrence.name_class)
    )


def _operands(node: Pattern) -> tuple[Pattern, ...]:
    if isinstance(node, Ref):
        return (node.target,)
    if isinstance(node, (Choice, Group, Interleave)):
        return node.first, node.second
    if isinstance(node, (Element, Attribute, OneOrMore, List)):
        return (node.pattern,)
    if isinstance(node, DataExcept):
        return (node.excluded,)
    return ()


def _content_operands(node: Pattern) -> tuple[Pattern, ...]:
    """Only same-element content dependencies; references end at element leaves."""
    if isinstance(node, (Ref, Choice, Group, Interleave, OneOrMore)):
        return _operands(node)
    return ()


def _postorder(start: Pattern) -> tuple[Pattern, ...]:
    """Iterative DAG order; checked recursion crosses an element boundary."""
    result = []
    seen: set[int] = set()
    pending = [(start, False)]
    while pending:
        node, ready = pending.pop()
        if ready:
            result.append(node)
        elif id(node) not in seen:
            seen.add(id(node))
            pending.append((node, True))
            pending.extend((c, False) for c in reversed(_content_operands(node)))
    return tuple(result)


def _frontier(pattern: Pattern) -> tuple[Pattern, ...]:
    return tuple(n for n in _postorder(pattern) if not _content_operands(n))


def _expanded(name: NameClass) -> str | None:
    if isinstance(name, Name):
        return f"{{{name.uri}}}{name.local}" if name.uri else name.local
    return None


def _overlaps(first: NameClass, second: NameClass) -> bool:
    """Exact names need membership, not the general finite-witness search."""
    if isinstance(first, Name):
        return second.contains(first.uri, first.local)
    if isinstance(second, Name):
        return first.contains(second.uri, second.local)
    return first.overlaps(second)


def _finite_names(name: NameClass) -> tuple[Name, ...] | None:
    if isinstance(name, Name):
        return (name,)
    if isinstance(name, NameChoice):
        result: list[Name] = []
        for part in name.alternatives:
            names = _finite_names(part)
            if names is None:
                return None
            result.extend(names)
        return tuple(result)
    return None


def _union(names: tuple[NameClass, ...]) -> NameClass:
    # All callers have at least one assignment; sorting also canonicalizes
    # wildcard routes built from different structural alternatives.
    unique = tuple(sorted(set(names), key=repr))
    return unique[0] if len(unique) == 1 else NameChoice(unique)


def _partition(declarations: tuple[Element | Attribute, ...]) -> tuple[NameClass, ...]:
    """Exact named views followed by the disjoint open-name remainder."""
    named: set[Name] = set()
    open_names: list[NameClass] = []
    for declaration in declarations:
        finite = _finite_names(declaration.name_class)
        if finite is None:
            open_names.append(declaration.name_class)
        else:
            named.update(finite)
    result: list[NameClass] = sorted(named, key=repr)
    if open_names:
        remainder = _union(tuple(open_names))
        if named:
            remainder = ExceptName(remainder, _union(tuple(named)))
        # Checked open classes have an infinite remainder: exclusions inside
        # nsName contain only exact names; anyName cannot exclude anyName.
        result.append(remainder)
    return tuple(result)


def _envelope(counts: tuple[Cardinality, ...]) -> Cardinality:
    maxima = tuple(c.maximum for c in counts)
    return Cardinality(
        min(c.minimum for c in counts),
        None if None in maxima else max(m for m in maxima if m is not None),
    )


def _bounds(order: tuple[Pattern, ...], role: Role, selector: NameClass) -> Cardinality:
    values: dict[int, Cardinality] = {}
    leaf_type = Element if role == "child" else Attribute
    for node in order:
        if isinstance(node, leaf_type) and _overlaps(node.name_class, selector):
            # A broad name choice contributes at most one named item, but need
            # not contribute this particular name in every valid instance.
            required = not ExceptName(node.name_class, selector).overlaps(AnyName())
            value = Cardinality(int(required), 1)
        elif isinstance(node, Ref):
            value = values[id(node.target)]
        elif isinstance(node, Choice):
            first, second = values[id(node.first)], values[id(node.second)]
            value = first if first == second else _envelope((first, second))
        elif isinstance(node, (Group, Interleave)):
            first, second = values[id(node.first)], values[id(node.second)]
            maximum = (
                None
                if first.maximum is None or second.maximum is None
                else first.maximum + second.maximum
            )
            value = Cardinality(first.minimum + second.minimum, maximum)
        elif isinstance(node, OneOrMore):
            item = values[id(node.pattern)]
            value = Cardinality(item.minimum, None if item.maximum != 0 else 0)
        else:
            value = _ZERO
        values[id(node)] = value
    return values[id(order[-1])]


def _aliases(fields: tuple[tuple[Role, str], ...]) -> dict[tuple[Role, str], str]:
    used = set(_RESERVED)
    result = {}
    for key in sorted(fields):
        # Python source normalizes identifiers to NFKC. Resolve those collisions
        # now so attribute syntax, getattr(), and emitted stubs agree.
        local = normalize("NFKC", key[1].rsplit("}", 1)[-1])
        base = "".join(c if ("_" + c).isidentifier() else f"_x{ord(c):x}_" for c in local)
        if not base.isidentifier() or base.startswith("_") or keyword.iskeyword(base):
            base = "field_" + base
        alias = base
        index = 2
        while alias in used:
            alias = f"{base}_{index}"
            index += 1
        used.add(alias)
        result[key] = alias
    return result


class _Planner:
    def __init__(self, grammar: CompiledGrammar, limits: RelaxNGLimits | None) -> None:
        self.grammar = grammar
        self.budget = BudgetCounter("projection", limits)
        self.nodes: list[Pattern] = []
        self.ordinals: dict[int, int] = {}
        self.scalars: dict[int, ScalarContract] = {}
        self.losses: dict[int, str] = {}
        pending = [grammar.start]
        while pending:
            node = pending.pop()
            if id(node) in self.ordinals:
                continue
            self.ordinals[id(node)] = len(self.nodes)
            self.nodes.append(node)
            pending.extend(reversed(_operands(node)))
            if isinstance(node, _SCALARS):
                try:
                    self.scalars[id(node)] = scalar_contract(node)
                except UnsupportedDatatypeOperation as error:
                    # Phase 1 permits validation-only custom datatypes without
                    # stable type metadata. They still compile, but a typed
                    # projection cannot invent the missing scalar contract.
                    self.losses[id(node)] = str(error)
        self.fingerprint = self._fingerprint()
        self.pending: list[tuple[ProjectionOccurrence, tuple[Element, ...]]] = []
        self.registered: dict[ProjectionOccurrence, str] = {}
        self.routes: dict[str, RouteContract] = {}

    def _fingerprint(self) -> str:
        """Identity of the structural plan, not the datatype registry.

        The schema keeps its separate registry-inclusive Phase 3 fingerprint.
        Dependency content distinguishes authored opaque datatypes whose metadata
        cannot participate in a scalar contract. No object repr, source path, or
        instance context is serialized.
        """
        records = []
        for node in self.nodes:
            name = node.name_class if isinstance(node, (Element, Attribute)) else None
            scalar = self.scalars.get(id(node))
            records.append(
                (
                    type(node).__name__ if not isinstance(node, Ref) else "Ref",
                    name,
                    scalar,
                    self.losses.get(id(node)),
                    tuple(self.ordinals[id(c)] for c in _operands(node)),
                )
            )
        dependencies = sorted({(d.purpose, d.content_sha256) for d in self.grammar.dependencies})
        return "pyxsd.relaxng.projection-grammar/1:" + _digest((records, dependencies))

    def register(self, name: NameClass, assignments: tuple[Element, ...]) -> str:
        unique = {self.ordinals[id(n)]: n for n in assignments if _overlaps(n.name_class, name)}
        indices = tuple(sorted(unique))
        occurrence = ProjectionOccurrence(self.fingerprint, indices, name)
        if occurrence not in self.registered:
            self.budget.consume("max_projection_routes")
            route_id = _route_id(occurrence)
            self.registered[occurrence] = route_id
            self.pending.append((occurrence, tuple(unique[i] for i in indices)))
        return self.registered[occurrence]

    def scalar_views(self, patterns: tuple[Pattern, ...]) -> tuple[ScalarContract, ...]:
        result = {
            self.scalars[id(n)]
            for pattern in patterns
            for n in _frontier(pattern)
            if id(n) in self.scalars
        }
        return tuple(sorted(result, key=lambda c: c.pattern_identity))

    def model(self, pattern: Pattern, references: tuple[FieldReference, ...]) -> ContentContract:
        values: dict[int, ContentContract] = {}
        for node in _postorder(pattern):
            if isinstance(node, Ref):
                values[id(node)] = values[id(node.target)]
                continue
            operands = tuple(values[id(c)] for c in _content_operands(node))
            fields: tuple[FieldReference, ...] = ()
            contracts: tuple[ScalarContract, ...] = ()
            name: NameClass | None = None
            if isinstance(node, (Element, Attribute)):
                role = "child" if isinstance(node, Element) else "attribute"
                fields = tuple(
                    f
                    for f in references
                    if f.role == role and _overlaps(f.name_class, node.name_class)
                )
                name = node.name_class
                if isinstance(node, Attribute):
                    contracts = self.scalar_views((node.pattern,))
                kind = role
            elif isinstance(node, _SCALARS):
                contracts = (self.scalars[id(node)],) if id(node) in self.scalars else ()
                kind = "scalar"
            else:
                kind = {
                    Empty: "empty",
                    NotAllowed: "notAllowed",
                    Choice: "choice",
                    Group: "group",
                    Interleave: "interleave",
                    OneOrMore: "repeat",
                }[type(node)]
            values[id(node)] = ContentContract(
                kind,
                operands,
                fields,
                contracts,
                name,
                self.ordinals[id(node)],
                self.losses.get(id(node)),
            )
        return values[id(pattern)]

    def build(self, occurrence: ProjectionOccurrence, assignments: tuple[Element, ...]) -> None:
        patterns = tuple(n.pattern for n in assignments)
        orders = tuple(_postorder(p) for p in patterns)
        leaves = tuple(n for order in orders for n in order if not _content_operands(n))
        references: list[FieldReference] = []
        slots = []
        kinds: tuple[tuple[Role, type[Attribute] | type[Element]], ...] = (
            ("attribute", Attribute),
            ("child", Element),
        )
        for role, kind in kinds:
            declarations = tuple(n for n in leaves if isinstance(n, kind))
            for selector in _partition(declarations):
                members = tuple(n for n in declarations if _overlaps(n.name_class, selector))
                child_route = (
                    self.register(selector, tuple(n for n in members if isinstance(n, Element)))
                    if role == "child"
                    else None
                )
                reference = FieldReference(role, _expanded(selector), selector, child_route)
                references.append(reference)
                cardinality = _envelope(tuple(_bounds(order, role, selector) for order in orders))
                scalars = self.scalar_views(tuple(n.pattern for n in members))
                # A child's mixed/structured route has no whole-element scalar
                # descriptor. Scalar nodes remain in its ordered content model.
                if role == "child" and any(
                    isinstance(n, Element) for m in members for n in _frontier(m.pattern)
                ):
                    scalars = ()
                slots.append((reference, cardinality, scalars))
        models = tuple(self.model(p, tuple(references)) for p in patterns)
        model = models[0] if len(models) == 1 else ContentContract("choice", models)
        constraints = (
            (model,)
            if model.kind in {"choice", "group", "interleave", "repeat"} or len(model.fields) > 1
            else ()
        )
        aliases = _aliases(
            tuple((f.role, f.expanded_name) for f in references if f.expanded_name is not None)
        )
        attributes: dict[str, FieldContract] = {}
        children: dict[str, FieldContract] = {}
        wildcards = []
        for reference, cardinality, scalars in slots:
            name = reference.expanded_name
            if name is None:
                wildcards.append(
                    WildcardContract(
                        reference.role,
                        reference.name_class,
                        cardinality,
                        scalars,
                        reference.child_route_id,
                        reference.role == "child",
                        constraints,
                    )
                )
            else:
                field = FieldContract(
                    aliases[reference.role, name],
                    name,
                    cardinality,
                    scalars,
                    reference.child_route_id,
                    constraints,
                )
                target = attributes if reference.role == "attribute" else children
                target[name] = field
        has_children = any(isinstance(n, Element) for n in leaves)
        has_text = any(isinstance(n, Text) for n in leaves)
        has_atomic = any(isinstance(n, (Data, DataExcept, Value, List)) for n in leaves)
        losses = sorted(
            {self.losses[id(n)] for n in leaves if id(n) in self.losses}
            | {
                self.losses[id(n)]
                for a in leaves
                if isinstance(a, Attribute)
                for n in _frontier(a.pattern)
                if id(n) in self.losses
            }
        )
        reason = losses[0] if losses else None
        if has_children and has_atomic:
            reason = (
                "whole-element scalar and child-element assignments cannot share "
                "a whole-element scalar descriptor"
            )
        route_id = self.registered[occurrence]
        self.routes[route_id] = RouteContract(
            route_id,
            _expanded(occurrence.name_class),
            occurrence.name_class,
            attributes,
            children,
            OrderedContentContract(
                True,
                has_children and has_text,
                () if has_children else self.scalar_views(patterns),
                model,
            ),
            tuple(wildcards),
            constraints,
            reason,
            occurrence,
            OriginTable().union(*(self.grammar.graph.origins(n) for n in assignments)),
            {
                self.ordinals[id(n)]: ElementAssignment(n.name_class, m)
                for n, m in zip(assignments, models, strict=True)
            },
        )

    def plan(self) -> ProjectionPlan:
        declarations = tuple(n for n in _frontier(self.grammar.start) if isinstance(n, Element))
        roots = tuple(self.register(name, declarations) for name in _partition(declarations))
        index = 0
        while index < len(self.pending):
            occurrence, assignments = self.pending[index]
            self.build(occurrence, assignments)
            index += 1
        return ProjectionPlan(roots, self.routes, self.fingerprint)


def plan_projections(
    grammar: CompiledGrammar, *, limits: RelaxNGLimits | None = None
) -> ProjectionPlan:
    """Plan a checked grammar without reading, validating, or decoding instances.

    Every distinct merged route consumes one ``max_projection_routes`` unit.
    Exhaustion raises at compilation, including for a zero budget. An actual
    representation loss is route-local and recorded on ``generic_reason``.
    """
    return _Planner(grammar, limits).plan()
