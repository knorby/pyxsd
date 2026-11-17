"""Immutable RELAX NG grammar inventories, stable IDs, and dependency traversal.

The described object is the *compiled* semantic graph, not the authored syntax.
Composition binds every reference and names the surviving element definitions
with deterministic, composer-generated keys (``element0``, ``element1``, ...);
an authored ``define`` name therefore does not appear in the inventory.  Every
definition component is marked :attr:`RelaxNGComponentDescription.synthetic`
and its origins retain the authored evidence that produced it.

Component IDs cover normalized grammar semantics, never source paths, object
addresses, process hash seeds, or mapping insertion order.  Locations and
origins are evidence beside those identities: relocating a resource or
recompiling an equivalent grammar changes locations, not IDs.  Name classes
use a canonical, alternative-ordered presentation; pattern components use
:func:`pyxsd.relaxng.contracts.pattern_identity`; route components use the
projection planner's route ID directly.  A node on the interior of a legal
reference cycle gets a contextual ID keyed by a definition element's pattern
identity and a structural path, because ``pattern_identity`` is defined only
for a graph root and rejects re-entering an active structural node.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

from pyxsd.locations import IssueLocation
from pyxsd.relaxng.contracts import pattern_identity, scalar_contract
from pyxsd.relaxng.names import AnyName, ExceptName, Name, NameChoice, NameClass, NsName
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
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
from pyxsd.relaxng.resources import ResourceDependency, ResourceReference
from pyxsd.relaxng.simplify import CompiledGrammar

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pyxsd.relaxng.contracts import ScalarContract
    from pyxsd.relaxng.projection import ProjectionPlan, RouteContract
    from pyxsd.relaxng.schema import RelaxNGSchema

__all__ = [
    "RelaxNGComponentDescription",
    "RelaxNGSchemaDescription",
    "describe_component",
    "describe_schema",
    "find_components",
    "related_components",
]

#: Component order for a deterministic inventory listing.  The rank separates
#: the grammar root from its definitions, semantic patterns, and evidence
#: components without inventing XSD declaration categories.
_KIND_RANK = {
    kind: rank
    for rank, kind in enumerate(
        ("grammar", "define", "pattern", "name-class", "datatype", "route", "resource")
    )
}

#: Exact pattern-node kind names used by :attr:`RelaxNGComponentDescription.pattern_kind`.
_PATTERN_KINDS: dict[type[Pattern], str] = {
    Empty: "empty",
    NotAllowed: "notAllowed",
    Text: "text",
    Choice: "choice",
    Group: "group",
    Interleave: "interleave",
    OneOrMore: "oneOrMore",
    Element: "element",
    Attribute: "attribute",
    List: "list",
    Data: "data",
    DataExcept: "dataExcept",
    Value: "value",
}

_LocationOrder = tuple[str, tuple[tuple[str, int], ...], str]
_ReferenceOrder = tuple[str, str, str, str, str, tuple[int, str, tuple[tuple[str, int], ...], str]]


@dataclass(frozen=True)
class RelaxNGComponentDescription:
    """One immutable component of a compiled RELAX NG grammar.

    ``kind`` is ``grammar``, ``define``, ``pattern``, ``name-class``,
    ``datatype``, ``route``, or ``resource``.  ``namespace`` is an
    expanded-name namespace only (never a datatype-library URI).  A
    definition's ``name`` is the composer-generated key and ``synthetic`` is
    true; authored ``define`` names survive only as origins.  ``name_class``
    is the canonical presentation of an element/attribute/route/name-class
    name class.  Datatype components carry their normalized library, type, and
    ordered parameters.  There are deliberately no XSD-only fields: no
    ``global_declaration``, Python binding, occurrence count, or default.
    """

    id: str
    kind: str
    name: str | None
    namespace: str | None
    location: IssueLocation | None
    origins: OriginTable
    owner_id: str | None
    pattern_kind: str | None
    name_class: str | None
    datatype_library: str | None
    datatype_name: str | None
    datatype_parameters: tuple[tuple[str, str], ...]
    synthetic: bool


@dataclass(frozen=True)
class RelaxNGSchemaDescription:
    """A deterministic grammar inventory.

    ``start_ids`` names the compiled start pattern component.  Components are
    sorted by kind rank and then ID; ``dependencies`` is the compilation's
    canonical resource closure verbatim, including every dependency record
    whose content merges into one resource component.
    """

    start_ids: tuple[str, ...]
    components: tuple[RelaxNGComponentDescription, ...]
    dependencies: tuple[ResourceDependency, ...]


def describe_schema(schema: RelaxNGSchema) -> RelaxNGSchemaDescription:
    """Return the immutable grammar inventory, valid schema or not.

    An invalid compilation still exposes its grammar, start pattern,
    definitions, patterns, and resource dependencies.  Projection routes are
    omitted without asking an invalid schema for ``schema.projection``; the
    report and its verdict are never changed.
    """
    return _index(schema).description


def describe_component(schema: RelaxNGSchema, component_id: str) -> RelaxNGComponentDescription:
    """Return one exact component, raising :class:`KeyError` for any other ID."""
    return _index(schema).components[component_id]


def find_components(
    schema: RelaxNGSchema,
    *,
    text: str | None = None,
    kind: str | None = None,
    namespace: str | None = None,
) -> tuple[RelaxNGComponentDescription, ...]:
    """Filter by literal case-insensitive text, component kind, and namespace.

    ``None`` leaves a filter unrestricted; an empty namespace selects
    components without an expanded-name namespace.  Text is matched literally
    over the ID, name, namespace, pattern kind, canonical name class, and
    datatype library/type; no caller expression is evaluated.
    """
    needle = text.casefold() if text is not None else ""
    return tuple(
        component
        for component in _index(schema).description.components
        if (kind is None or component.kind == kind)
        and (namespace is None or (component.namespace or "") == namespace)
        and needle
        in " ".join(
            (
                component.id,
                component.name or "",
                component.namespace or "",
                component.pattern_kind or "",
                component.name_class or "",
                component.datatype_library or "",
                component.datatype_name or "",
            )
        ).casefold()
    )


def related_components(
    schema: RelaxNGSchema,
    component_id: str,
    *,
    direction: Literal["uses", "used_by"],
    depth: int,
) -> tuple[RelaxNGComponentDescription, ...]:
    """Breadth-first component traversal, excluding the seed and duplicates."""
    if direction not in ("uses", "used_by"):
        raise ValueError("direction must be uses or used_by")
    if depth < 0:
        raise ValueError("depth must be non-negative")
    index = _index(schema)
    index.components[component_id]
    graph = index.edges if direction == "uses" else index.reverse
    seen = {component_id}
    frontier = [component_id]
    result: list[RelaxNGComponentDescription] = []
    for _ in range(depth):
        following: list[str] = []
        for parent in frontier:
            for child in graph[parent]:
                if child not in seen:
                    seen.add(child)
                    following.append(child)
                    result.append(index.components[child])
        frontier = following
        if not frontier:
            break
    return tuple(result)


@dataclass(frozen=True)
class _Index:
    """One built inventory plus its private immutable dependency edges."""

    description: RelaxNGSchemaDescription
    components: Mapping[str, RelaxNGComponentDescription]
    edges: Mapping[str, tuple[str, ...]]
    reverse: Mapping[str, tuple[str, ...]]


def _index(schema: RelaxNGSchema) -> _Index:
    """Build the inventory and both traversal directions for one snapshot."""
    grammar = schema.grammar
    builder = _Builder()
    builder.add("grammar", kind="grammar", owner_id=None)
    pattern_ids = _add_patterns(grammar, builder)
    start_id = pattern_ids[id(grammar.start)]
    builder.add_edge("grammar", start_id)
    for name in sorted(grammar.definitions):
        definition = grammar.definitions[name]
        component_id = "define:" + definition.name
        element = definition.pattern
        builder.add(
            component_id,
            kind="define",
            name=definition.name,
            namespace=_exact_name_namespace(element.name_class),
            origins=definition.origins,
            pattern_kind="element",
            name_class=_canonical_name_class(element.name_class),
            synthetic=True,
        )
        builder.add_edge(component_id, pattern_ids[id(element)])
        builder.add_edge("grammar", component_id)
    for resource_id in _add_resources(grammar, builder):
        builder.add_edge("grammar", resource_id)
    if not schema.report.has_errors:
        for route_id in _add_routes(schema.projection, pattern_ids, builder):
            builder.add_edge("grammar", route_id)
    components = tuple(
        sorted(
            builder.records.values(),
            key=lambda component: (_KIND_RANK[component.kind], component.id),
        )
    )
    known = {component.id for component in components}
    edges = {
        component.id: tuple(
            sorted(target for target in builder.edges.get(component.id, ()) if target in known)
        )
        for component in components
    }
    reverse: dict[str, list[str]] = {component.id: [] for component in components}
    for component in components:
        for target in edges[component.id]:
            reverse[target].append(component.id)
    description = RelaxNGSchemaDescription(
        (start_id,),
        components,
        grammar.dependencies,
    )
    return _Index(
        description,
        MappingProxyType({component.id: component for component in components}),
        MappingProxyType(edges),
        MappingProxyType({key: tuple(value) for key, value in reverse.items()}),
    )


class _Builder:
    """Mutable construction state for one immutable inventory."""

    def __init__(self) -> None:
        self.records: dict[str, RelaxNGComponentDescription] = {}
        self.edges: dict[str, set[str]] = {}

    def add(
        self,
        component_id: str,
        *,
        kind: str,
        name: str | None = None,
        namespace: str | None = None,
        origins: OriginTable | None = None,
        owner_id: str | None = "grammar",
        pattern_kind: str | None = None,
        name_class: str | None = None,
        datatype_library: str | None = None,
        datatype_name: str | None = None,
        datatype_parameters: tuple[tuple[str, str], ...] = (),
        synthetic: bool = False,
    ) -> str:
        """Add or merge one component; interning can accumulate origins."""
        effective_origins = origins if origins is not None else OriginTable()
        existing = self.records.get(component_id)
        if existing is None:
            self.records[component_id] = RelaxNGComponentDescription(
                id=component_id,
                kind=kind,
                name=name,
                namespace=namespace,
                location=_primary_location(effective_origins),
                origins=effective_origins,
                owner_id=owner_id,
                pattern_kind=pattern_kind,
                name_class=name_class,
                datatype_library=datatype_library,
                datatype_name=datatype_name,
                datatype_parameters=datatype_parameters,
                synthetic=synthetic,
            )
        else:
            merged = existing.origins.union(effective_origins)
            self.records[component_id] = replace(
                existing,
                origins=merged,
                location=_primary_location(merged),
            )
        self.edges.setdefault(component_id, set())
        return component_id

    def add_edge(self, source: str, target: str) -> None:
        """Record one dependency edge from an already-added component."""
        self.edges[source].add(target)


def _add_patterns(grammar: CompiledGrammar, builder: _Builder) -> dict[int, str]:
    """Describe the whole final graph from the start and every definition.

    Reference cycles are legal.  ``pattern_identity`` is defined for a graph
    root; it rejects a cycle entered from a non-anchor interior node (for
    example the content choice of a recursive element whose back edge targets
    that element).  Such a node receives a contextual ID built from its
    deterministic anchor's ``pattern_identity`` and the deterministic
    structural path discovered from that anchor.  The anchor is a definition
    element or, as a fallback, the start pattern; both are cycle entries for
    which ``pattern_identity`` is defined.
    """
    nodes: dict[int, Pattern] = {}
    order: list[Pattern] = []
    links: dict[int, tuple[tuple[str, Pattern], ...]] = {}
    pending: list[Pattern] = [
        grammar.start,
        *(definition.pattern for definition in grammar.definitions.values()),
    ]
    while pending:
        node = pending.pop()
        key = id(node)
        if key in nodes:
            continue
        nodes[key] = node
        order.append(node)
        children = _pattern_links(node)
        links[key] = children
        pending.extend(child for _, child in children)

    ids: dict[int, str] = {}
    contextual: list[Pattern] = []
    for node in order:
        try:
            ids[id(node)] = "pattern:" + pattern_identity(node)
        except ValueError as error:
            if "outside a bound reference" not in str(error):
                raise
            contextual.append(node)
    if contextual:
        anchors = _anchor_roots(grammar)
        discovered = _discover_paths(links, anchors)
        for node in contextual:
            anchor_id, path = discovered[id(node)]
            digest = sha256(
                json.dumps([anchor_id, *path], ensure_ascii=True).encode("utf-8")
            ).hexdigest()
            ids[id(node)] = "pattern:context:" + digest

    for node in order:
        component_id = ids[id(node)]
        origins = grammar.graph.origins(node)
        name_class = node.name_class if isinstance(node, (Element, Attribute)) else None
        canonical_class = _canonical_name_class(name_class) if name_class is not None else None
        builder.add(
            component_id,
            kind="pattern",
            name=(name_class.local if isinstance(name_class, Name) else None),
            namespace=_exact_name_namespace(name_class),
            origins=origins,
            pattern_kind=_pattern_kind(node),
            name_class=canonical_class,
        )
        if name_class is not None:
            builder.add_edge(component_id, _add_name_class(name_class, origins, builder))
        if isinstance(node, (Data, DataExcept, Value)):
            builder.add_edge(component_id, _add_datatype(node.datatype, builder))
        for _, child in links[id(node)]:
            builder.add_edge(component_id, ids[id(child)])
    return ids


def _pattern_links(pattern: Pattern) -> tuple[tuple[str, Pattern], ...]:
    """Return labelled semantic edges, including a bound reference target."""
    if isinstance(pattern, Ref):
        return (("target", pattern.target),)
    if isinstance(pattern, DataExcept):
        return (("excluded", pattern.excluded),)
    if isinstance(pattern, (Choice, Group, Interleave)):
        return (("first", pattern.first), ("second", pattern.second))
    if isinstance(pattern, (Attribute, Element, List, OneOrMore)):
        return (("content", pattern.pattern),)
    return ()


def _anchor_roots(grammar: CompiledGrammar) -> list[tuple[str, Pattern]]:
    """Return definition anchors first, then the start pattern, deterministically."""
    anchors: list[tuple[str, Pattern]] = [
        (
            "pattern:" + pattern_identity(grammar.definitions[name].pattern),
            grammar.definitions[name].pattern,
        )
        for name in sorted(grammar.definitions)
    ]
    anchors.append(("pattern:" + pattern_identity(grammar.start), grammar.start))
    return anchors


def _discover_paths(
    links: dict[int, tuple[tuple[str, Pattern], ...]],
    roots: list[tuple[str, Pattern]],
) -> dict[int, tuple[str, tuple[str, ...]]]:
    """Record one anchor and structural path for every reachable pattern."""
    discovered: dict[int, tuple[str, tuple[str, ...]]] = {}
    for anchor_id, root in roots:
        stack: list[tuple[Pattern, tuple[str, ...]]] = [(root, ())]
        while stack:
            node, path = stack.pop()
            key = id(node)
            if key in discovered:
                continue
            discovered[key] = (anchor_id, path)
            for label, child in reversed(links[key]):
                stack.append((child, (*path, label)))
    return discovered


def _pattern_kind(pattern: Pattern) -> str:
    """Name one pattern node; the composer's final references are ``ref``."""
    if isinstance(pattern, Ref):
        return "ref"
    return _PATTERN_KINDS[type(pattern)]


def _exact_name_namespace(name_class: NameClass | None) -> str | None:
    """Return a namespace only when the class denotes one exact expanded name."""
    return name_class.uri if isinstance(name_class, Name) else None


def _canonical_name_class(name_class: NameClass) -> str:
    """Return the deterministic, order-independent presentation of a name class."""
    if isinstance(name_class, NameChoice):
        alternatives = sorted(
            _canonical_name_class(alternative) for alternative in name_class.alternatives
        )
        return "choice(" + ", ".join(alternatives) + ")"
    if isinstance(name_class, ExceptName):
        return (
            "except("
            + _canonical_name_class(name_class.base)
            + ", "
            + _canonical_name_class(name_class.excluded)
            + ")"
        )
    if isinstance(name_class, Name):
        return f"name({_quoted(name_class.uri)}, {_quoted(name_class.local)})"
    if isinstance(name_class, AnyName):
        return "anyName"
    return f"nsName({_quoted(cast(NsName, name_class).uri)})"


def _quoted(value: str | None) -> str:
    """Quote a namespace URI or local name, keeping the presentation ASCII."""
    return json.dumps(value, ensure_ascii=True)


def _add_name_class(
    name_class: NameClass,
    origins: OriginTable,
    builder: _Builder,
) -> str:
    """Add a name-class component and components for nested composite classes."""
    text = _canonical_name_class(name_class)
    component_id = "name-class:" + sha256(text.encode("utf-8")).hexdigest()
    if isinstance(name_class, NsName):
        namespace: str | None = name_class.uri
    elif isinstance(name_class, Name):
        namespace = name_class.uri
    else:
        namespace = None
    builder.add(
        component_id,
        kind="name-class",
        name=name_class.local if isinstance(name_class, Name) else None,
        namespace=namespace,
        origins=origins,
        name_class=text,
    )
    if isinstance(name_class, NameChoice):
        nested = [
            alternative
            for alternative in name_class.alternatives
            if isinstance(alternative, (NameChoice, ExceptName))
        ]
    elif isinstance(name_class, ExceptName):
        nested = [
            operand
            for operand in (name_class.base, name_class.excluded)
            if isinstance(operand, (NameChoice, ExceptName))
        ]
    else:
        nested = []
    for operand in nested:
        builder.add_edge(component_id, _add_name_class(operand, origins, builder))
    return component_id


def _add_datatype(datatype: object, builder: _Builder) -> str:
    """Add one datatype component from its stable schema-static description."""
    contract = scalar_contract(Data(datatype))
    component_id = "datatype:" + contract.pattern_identity
    builder.add(
        component_id,
        kind="datatype",
        name=contract.type_name,
        datatype_library=contract.library_uri,
        datatype_name=contract.type_name,
        datatype_parameters=tuple(
            (parameter.name, parameter.value) for parameter in contract.parameters
        ),
    )
    return component_id


def _add_resources(grammar: CompiledGrammar, builder: _Builder) -> list[str]:
    """Add one component per canonical content, preserving every dependency."""
    groups: dict[str, list[ResourceDependency]] = {}
    for dependency in grammar.dependencies:
        component_id = f"resource:{dependency.purpose}:{dependency.content_sha256}"
        groups.setdefault(component_id, []).append(dependency)
    component_ids: list[str] = []
    for component_id in sorted(groups):
        group = sorted(
            groups[component_id],
            key=lambda dependency: (dependency.canonical_id, dependency.source_id),
        )
        references = sorted(
            {reference for dependency in group for reference in dependency.references},
            key=_reference_order,
        )
        first_reference = references[0] if references else None
        builder.add(
            component_id,
            kind="resource",
            name=group[0].canonical_id,
            origins=_resource_origins(first_reference),
        )
        component_ids.append(component_id)
    return component_ids


def _resource_origins(first_reference: ResourceReference | None) -> OriginTable:
    """Locate one resource at its first sorted authored reference."""
    if first_reference is None or first_reference.location is None:
        return OriginTable()
    return OriginTable(
        frozenset({SourceOrigin(first_reference.containing_id, first_reference.location)})
    )


def _reference_order(reference: ResourceReference) -> _ReferenceOrder:
    """Total order for dependency references, including a missing location."""
    return (
        reference.containing_id,
        reference.reference,
        reference.base or "",
        reference.xml_base or "",
        reference.canonical_id,
        _optional_location_order(reference.location),
    )


def _optional_location_order(
    location: IssueLocation | None,
) -> tuple[int, str, tuple[tuple[str, int], ...], str]:
    """Order optional locations; an absent location sorts before any present one."""
    return (0, "", (), "") if location is None else (1, *_location_order(location))


def _location_order(location: IssueLocation) -> _LocationOrder:
    """Deterministic source/path/attribute ordering without object identity."""
    return (
        location.source or "",
        tuple((step.name, step.index) for step in location.path),
        location.attribute or "",
    )


def _primary_location(origins: OriginTable) -> IssueLocation | None:
    """Return the smallest authored origin location, independent of identity."""
    located = [
        (
            origin.source_id,
            _location_order(origin.location),
            origin.location,
        )
        for origin in origins
        if origin.location is not None
    ]
    return min(located, key=lambda item: (item[0], item[1]))[2] if located else None


def _add_routes(
    plan: ProjectionPlan,
    pattern_ids: dict[int, str],
    builder: _Builder,
) -> list[str]:
    """Add route components and their child/scalar dependencies."""
    route_ids: list[str] = []
    for route_id in sorted(plan.routes):
        route = plan.routes[route_id]
        builder.add(
            route_id,
            kind="route",
            name=route.expanded_name,
            namespace=_route_namespace(route),
            origins=route.origins,
            name_class=_canonical_name_class(route.name_class),
        )
        for child_id in _route_children(route):
            builder.add_edge(route_id, child_id)
        for contract in _route_contracts(route):
            encoding = plan.encoding_patterns[contract.pattern_identity]
            builder.add_edge(route_id, pattern_ids[id(encoding)])
        route_ids.append(route_id)
    return route_ids


def _route_namespace(route: RouteContract) -> str | None:
    """Return a route namespace only for an exact expanded element name."""
    return route.name_class.uri if isinstance(route.name_class, Name) else None


def _route_children(route: RouteContract) -> tuple[str, ...]:
    """Return every child route referenced by an accessor or wildcard."""
    children: list[str | None] = [field.child_route_id for field in route.attributes.values()]
    children.extend(field.child_route_id for field in route.children.values())
    children.extend(wildcard.child_route_id for wildcard in route.wildcards)
    return tuple(child for child in children if child is not None)


def _route_contracts(route: RouteContract) -> tuple[ScalarContract, ...]:
    """Collect every distinct scalar contract an accessor can represent."""
    contracts = [
        *route.content.scalar_contracts,
        *(contract for field in route.attributes.values() for contract in field.scalar_contracts),
        *(contract for field in route.children.values() for contract in field.scalar_contracts),
        *(contract for wildcard in route.wildcards for contract in wildcard.scalar_contracts),
    ]
    return tuple(dict.fromkeys(contracts))
