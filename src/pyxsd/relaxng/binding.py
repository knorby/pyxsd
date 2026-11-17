"""Schema-static projection classes and live views over the lexical tree.

Compilation fixes every route identity and every public field.  This module
turns that finite plan into element subclasses whose descriptors read the one
authoritative :class:`~pyxsd.relaxng.tree.RelaxNGElement` tree.  Classes never
enter the lexical tree's state: they add ``__slots__ = ()`` and descriptors
whose values come from the current children and from the revision's typed
snapshot.  Routing a node to a route uses planner-owned child-route references
only; instance attributes and values never choose a class layout.

Generic routes keep the complete generic XML interface.  A generic descendant
or sibling never changes a stable parent's descriptors, and open name classes
remain expanded-name views rather than invented Python aliases.
"""

from __future__ import annotations

import keyword
import threading
from collections.abc import Iterator, Sequence
from typing import Any, overload
from unicodedata import normalize

from pyxsd.namespaces import local_name, namespace_of
from pyxsd.relaxng.evidence import EvidenceGraph
from pyxsd.relaxng.projection import (
    FieldContract,
    ProjectionPlan,
    RouteContract,
    WildcardContract,
)
from pyxsd.relaxng.tree import RelaxNGElement

__all__ = [
    "GENERATED_MODULE",
    "WILDCARD_CHILDREN",
    "AttributeDescriptor",
    "ChildDescriptor",
    "ChildSequence",
    "ProjectionClassFactory",
    "WildcardChildren",
    "bind_projection",
    "projection_class_factory",
]

#: Synthetic module identity for deterministic generated-class names.  These
#: classes are runtime view types, not importable declarations; stubs emit the
#: matching source instead of importing this path.
GENERATED_MODULE = "pyxsd.relaxng.generated"


class _WildcardChildrenKey:
    """The alias-independent lookup key for the ordered wildcard-child view."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "WILDCARD_CHILDREN"


WILDCARD_CHILDREN = _WildcardChildrenKey()


class AttributeDescriptor:
    """One attribute field, exposing its schema-static typed interpretation.

    A missing attribute and an attribute whose accepted derivation has no
    scalar interpretation both read as ``None``, matching
    :meth:`RelaxNGElement.attribute_value`.  The value is produced by the
    revision's typed snapshot, so a stale revision raises before returning old
    evidence.
    """

    __slots__ = ("contract",)

    def __init__(self, contract: FieldContract) -> None:
        self.contract = contract

    @property
    def expanded_name(self) -> str:
        """The exact expanded attribute name this descriptor reads."""
        return self.contract.expanded_name

    def __get__(self, instance: RelaxNGElement | None, owner: type[Any] | None = None) -> Any:
        del owner
        if instance is None:
            return self
        return instance.attribute_value(self.contract.expanded_name)


class ChildSequence(Sequence[RelaxNGElement]):
    """A live ordered view of one expanded-name child field.

    Reads always consult the authoritative child list, so a mutation through
    the tree is visible without rebuilding this view.  Task 4 extends this
    seam with insertion, replacement, and deletion; Task 2 needs reads only.
    """

    __slots__ = ("_expanded_name", "_node")

    def __init__(self, node: RelaxNGElement, expanded_name: str) -> None:
        self._node = node
        self._expanded_name = expanded_name

    @property
    def expanded_name(self) -> str:
        """The exact expanded child name selected by this view."""
        return self._expanded_name

    def _matching(self) -> tuple[RelaxNGElement, ...]:
        return tuple(
            child for child in self._node._children_ if child._name_ == self._expanded_name
        )

    def __len__(self) -> int:
        return len(self._matching())

    @overload
    def __getitem__(self, index: int) -> RelaxNGElement: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[RelaxNGElement, ...]: ...

    def __getitem__(self, index: int | slice) -> RelaxNGElement | tuple[RelaxNGElement, ...]:
        return self._matching()[index]

    def __iter__(self) -> Iterator[RelaxNGElement]:
        return iter(self._matching())

    def __repr__(self) -> str:
        return f"ChildSequence(expanded_name={self._expanded_name!r}, count={len(self)})"


class WildcardChildren(Sequence[RelaxNGElement]):
    """The ordered children covered by the route's open child name classes.

    Named fields are partitioned away from this view at compilation, so a
    wildcard never fabricates a fixed descriptor and never duplicates a named
    child.  Reads are live and ordered.
    """

    __slots__ = ("_contracts", "_node")

    def __init__(self, node: RelaxNGElement, contracts: tuple[WildcardContract, ...]) -> None:
        self._node = node
        self._contracts = contracts

    def _matching(self) -> tuple[RelaxNGElement, ...]:
        if not self._contracts:
            return ()
        result = []
        for child in self._node._children_:
            uri = namespace_of(child._name_)
            local = local_name(child._name_)
            if any(contract.name_class.contains(uri, local) for contract in self._contracts):
                result.append(child)
        return tuple(result)

    def __len__(self) -> int:
        return len(self._matching())

    @overload
    def __getitem__(self, index: int) -> RelaxNGElement: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[RelaxNGElement, ...]: ...

    def __getitem__(self, index: int | slice) -> RelaxNGElement | tuple[RelaxNGElement, ...]:
        return self._matching()[index]

    def __iter__(self) -> Iterator[RelaxNGElement]:
        return iter(self._matching())

    def __repr__(self) -> str:
        return f"WildcardChildren(count={len(self)})"


class ChildDescriptor:
    """One named child field at the planner's cardinality.

    A singular field returns its first lexical node or ``None``.  A repeatable
    field always returns a :class:`ChildSequence`, including when empty.  Nodes
    are the existing lexical objects: descriptor reads never copy XML.
    """

    __slots__ = ("contract",)

    def __init__(self, contract: FieldContract) -> None:
        self.contract = contract

    @property
    def expanded_name(self) -> str:
        """The exact expanded child name this descriptor reads."""
        return self.contract.expanded_name

    def __get__(self, instance: RelaxNGElement | None, owner: type[Any] | None = None) -> Any:
        del owner
        if instance is None:
            return self
        if self.contract.cardinality.repeatable:
            return ChildSequence(instance, self.contract.expanded_name)
        for child in instance._children_:
            if child._name_ == self.contract.expanded_name:
                return child
        return None


def _class_base_name(route: RouteContract) -> str:
    """Return the deterministic class-name stem for one stable route."""
    if route.expanded_name is None:
        suffix = route.route_id.rsplit(":", 1)[-1][:8]
        return f"Element_{suffix}"
    local = normalize("NFKC", route.expanded_name.rsplit("}", 1)[-1])
    escaped = "".join(
        character if ("_" + character).isidentifier() else f"_x{ord(character):x}_"
        for character in local
    )
    if not escaped.isidentifier() or keyword.iskeyword(escaped) or escaped.startswith("_"):
        escaped = "Element_" + escaped
    return escaped[:1].upper() + escaped[1:]


def _class_names(plan: ProjectionPlan) -> dict[str, str]:
    """Assign unique deterministic names in plan route order."""
    used = {"RelaxNGElement"}
    names: dict[str, str] = {}
    for route in plan.routes.values():
        if route.generic_reason is not None:
            continue
        base = _class_base_name(route)
        name = base
        index = 2
        while name in used:
            name = f"{base}_{index}"
            index += 1
        used.add(name)
        names[route.route_id] = name
    return names


class ProjectionClassFactory:
    """Build and cache one generated class per stable route in a plan.

    Class identity is fixed per factory and therefore per compiled schema.
    Separate schemas never share mutable class objects even when their plans
    are equivalent; their deterministic module and qualified names agree.
    Generic routes resolve to the generic lexical class.
    """

    __slots__ = ("__weakref__", "_classes", "_lock", "_names", "_plan")

    def __init__(self, plan: ProjectionPlan) -> None:
        if not isinstance(plan, ProjectionPlan):
            raise TypeError("ProjectionClassFactory requires a ProjectionPlan")
        self._plan = plan
        self._classes: dict[str, type[RelaxNGElement]] = {}
        self._lock = threading.Lock()
        self._names = _class_names(plan)

    @property
    def plan(self) -> ProjectionPlan:
        """The immutable plan this factory belongs to."""
        return self._plan

    def class_for(self, route_id: str) -> type[RelaxNGElement]:
        """Return the cached class for *route_id*.

        An unknown route ID is an error.  A route with a semantic generic
        reason resolves to :class:`RelaxNGElement`; no empty generated shell
        pretends that contract is stable.
        """
        route = self._plan.routes.get(route_id)
        if route is None:
            raise KeyError(route_id)
        if route.generic_reason is not None:
            return RelaxNGElement
        with self._lock:
            cached = self._classes.get(route_id)
            if cached is not None:
                return cached
            generated = self._build(route, self._names[route_id])
            self._classes[route_id] = generated
            return generated

    def _build(self, route: RouteContract, name: str) -> type[RelaxNGElement]:
        wildcards = tuple(contract for contract in route.wildcards if contract.role == "child")

        def lookup(instance: RelaxNGElement, key: str | object) -> ChildSequence | WildcardChildren:
            if key is WILDCARD_CHILDREN:
                return WildcardChildren(instance, wildcards)
            if isinstance(key, str):
                return ChildSequence(instance, key)
            raise TypeError(
                "projection element lookup requires an expanded name or WILDCARD_CHILDREN"
            )

        namespace: dict[str, Any] = {
            "__slots__": (),
            "__module__": GENERATED_MODULE,
            "__qualname__": name,
            "__doc__": (
                "Schema-static RELAX NG projection for "
                f"{route.expanded_name or 'an open name'}; route {route.route_id}."
            ),
            "__getitem__": lookup,
            "_relaxng_plan_fingerprint_": self._plan.grammar_fingerprint,
            "_relaxng_route_": route,
            "_relaxng_route_id_": route.route_id,
            "_relaxng_wildcards_": wildcards,
        }
        for contract in route.attributes.values():
            namespace[contract.alias] = AttributeDescriptor(contract)
        for contract in route.children.values():
            namespace[contract.alias] = ChildDescriptor(contract)
        namespace["_relaxng_fields_"] = tuple(
            namespace[contract.alias]
            for contract in (*route.attributes.values(), *route.children.values())
        )
        return type(name, (RelaxNGElement,), namespace)


_LOCK = threading.Lock()
_FACTORY_ATTRIBUTE = "_relaxng_projection_factory"


def projection_class_factory(plan: ProjectionPlan) -> ProjectionClassFactory:
    """Return the one factory attached to *plan*.

    The plan retains its factory, so any live plan object preserves class
    identity even when no caller holds the factory directly.  Equivalent
    separately compiled plans remain separate factories, preserving
    per-schema class identity.
    """
    with _LOCK:
        factory = getattr(plan, _FACTORY_ATTRIBUTE, None)
        if factory is None:
            factory = ProjectionClassFactory(plan)
            object.__setattr__(plan, _FACTORY_ATTRIBUTE, factory)
        return factory


def _root_route(plan: ProjectionPlan, expanded: str) -> str | None:
    """Resolve a root expanded name through the plan's partitioned name views."""
    uri = namespace_of(expanded)
    local = local_name(expanded)
    matches = [
        route_id for route_id in plan.roots if plan.routes[route_id].name_class.contains(uri, local)
    ]
    return matches[0] if len(matches) == 1 else None


def _child_route(plan: ProjectionPlan, route: RouteContract, expanded: str) -> str | None:
    """Resolve one child through exact fields before the disjoint wildcard."""
    field = route.children.get(expanded)
    if field is not None:
        return field.child_route_id
    uri = namespace_of(expanded)
    local = local_name(expanded)
    for wildcard in route.wildcards:
        if (
            wildcard.role == "child"
            and wildcard.child_route_id is not None
            and wildcard.name_class.contains(uri, local)
        ):
            return wildcard.child_route_id
    return None


def _require_owned_evidence(root: RelaxNGElement, evidence: EvidenceGraph) -> None:
    """Require complete typed evidence for the supplied root and revision."""
    documents = [occurrence for occurrence in evidence.occurrences if occurrence.kind == "document"]
    if len(documents) != 1 or documents[0].element is not root:
        raise ValueError("projection binding requires typed evidence owned by the supplied root")
    if any(occurrence.element.revision is not root.revision for occurrence in evidence.occurrences):
        raise ValueError(
            "projection binding requires typed evidence from the root's lexical revision"
        )


def bind_projection(
    root: RelaxNGElement,
    plan: ProjectionPlan,
    evidence: EvidenceGraph,
    *,
    factory: ProjectionClassFactory | None = None,
) -> RelaxNGElement:
    """Assign plan-fixed projection classes to *root* and its descendants.

    Route selection follows only planner-owned root name views and
    child-route references from each parent route.  A node with no unique
    stable route stays generic; the walk still resolves descendants of a
    generic route through its recorded child views, so a stable child can be
    projected under a generic parent.  The lexical nodes themselves are
    unchanged, and no instance value chooses a class.
    """
    if not isinstance(root, RelaxNGElement):
        raise TypeError("bind_projection requires a RelaxNGElement root")
    if not isinstance(plan, ProjectionPlan):
        raise TypeError("bind_projection requires a ProjectionPlan")
    if not isinstance(evidence, EvidenceGraph):
        raise TypeError("bind_projection requires an EvidenceGraph")
    if factory is None:
        factory = projection_class_factory(plan)
    elif factory.plan is not plan:
        raise ValueError("the projection class factory belongs to a different plan")
    _require_owned_evidence(root, evidence)
    stack: list[tuple[RelaxNGElement, str | None]] = [(root, _root_route(plan, root._name_))]
    while stack:
        node, route_id = stack.pop()
        route = plan.routes[route_id] if route_id is not None else None
        target = RelaxNGElement if route_id is None else factory.class_for(route_id)
        if node.__class__ is not target:
            node.__class__ = target
        if route is None:
            for child in node._children_:
                stack.append((child, None))
            continue
        for child in node._children_:
            stack.append((child, _child_route(plan, route, child._name_)))
    return root
