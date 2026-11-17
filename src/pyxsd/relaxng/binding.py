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
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast, overload
from unicodedata import normalize

from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import local_name, namespace_of
from pyxsd.relaxng.contracts import ScalarContract, scalar_contract
from pyxsd.relaxng.datatypes import Datatype, DatatypeContext
from pyxsd.relaxng.errors import AmbiguousValueError, StaleInterpretationError
from pyxsd.relaxng.evidence import CandidateSelection, EvidenceGraph, EvidenceOccurrence
from pyxsd.relaxng.patterns import List, Pattern
from pyxsd.relaxng.projection import (
    FieldContract,
    ProjectionPlan,
    RouteContract,
    WildcardContract,
)
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.values import AlternativeValue, encode_scalar

__all__ = [
    "GENERATED_MODULE",
    "WILDCARD_CHILDREN",
    "AttributeDescriptor",
    "ChildDescriptor",
    "ChildSequence",
    "ProjectionClassFactory",
    "ScalarDescriptor",
    "WildcardChildren",
    "bind_projection",
    "projection_class_factory",
]

#: Base module prefix for deterministic generated-class names.  Each plan
#: appends its own projection-fingerprint token, so semantically different plans
#: cannot share a fully-qualified name.  These classes are runtime view types,
#: not importable declarations; stubs emit the matching source instead.
GENERATED_MODULE = "pyxsd.relaxng.generated"


class _WildcardChildrenKey:
    """The alias-independent lookup key for the ordered wildcard-child view."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "WILDCARD_CHILDREN"


WILDCARD_CHILDREN = _WildcardChildrenKey()


class ScalarDescriptor:
    """The route's whole-element typed scalar as a stable public descriptor.

    ``typed_value`` delegates to the inherited :meth:`RelaxNGElement.scalar_value`
    so it keeps the same schema-static wrapper, stale gate, invalid-document
    error, and evidence decoding.  It never stringifies or decodes a value on
    its own.  Assignment belongs to Task 3; this phase exposes a read-only seam.
    """

    __slots__ = ("field_name",)

    def __init__(self, field_name: str) -> None:
        self.field_name = field_name

    def __get__(self, instance: RelaxNGElement | None, owner: type[Any] | None = None) -> Any:
        del owner
        if instance is None:
            return self
        return instance.scalar_value()

    def __set__(self, instance: RelaxNGElement, value: Any) -> None:
        instance.assign(**{self.field_name: value})


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

    def __set__(self, instance: RelaxNGElement, value: Any) -> None:
        instance.assign(**{self.contract.alias: value})


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

    def __set__(self, instance: RelaxNGElement, value: Any) -> None:
        instance.assign(**{self.contract.alias: value})


def _sanitize_identifier(value: str) -> str:
    """Escape every character that cannot occur in a Python identifier."""
    return "".join(
        character if ("_" + character).isidentifier() else f"_x{ord(character):x}_"
        for character in normalize("NFKC", value)
    )


def _safe_class_name(stem: str) -> str:
    """Validate the final capitalized class name, not only its stem."""
    candidate = stem[:1].upper() + stem[1:]
    if candidate.startswith("_"):
        candidate = "Element" + candidate
    if not candidate.isidentifier() or keyword.iskeyword(candidate):
        candidate = "Element_" + candidate
    return candidate


def _class_base_name(route: RouteContract) -> str:
    """Return the deterministic, source-valid class-name stem for one route."""
    if route.expanded_name is None:
        suffix = _sanitize_identifier(route.route_id.rsplit(":", 1)[-1])[:24]
        return _safe_class_name("Element_" + (suffix or "anonymous"))
    local = route.expanded_name.rsplit("}", 1)[-1]
    return _safe_class_name(_sanitize_identifier(local))


def _module_name(plan: ProjectionPlan) -> str:
    """Return a deterministic module identity carrying the plan fingerprint."""
    token = plan.grammar_fingerprint.rsplit(":", 1)[-1]
    return f"{GENERATED_MODULE}.{token}"


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


def _scalar_field_name(used: set[str]) -> str:
    """Choose the stable scalar descriptor name around field/API collisions."""
    name = "typed_value"
    while name in used or hasattr(RelaxNGElement, name):
        name += "_"
    return name


class ProjectionClassFactory:
    """Build and cache one generated class per stable route in a plan.

    Class identity is fixed per factory and therefore per compiled schema.
    Separate schemas own separate class objects even when their plans are
    equivalent.  Equivalent plans intentionally share the contract fully
    qualified name because their projection fingerprint is the same; distinct
    plans append different fingerprint tokens.  Consumers must cache class
    objects, never FQN strings.  Generic routes resolve to the generic lexical
    class.
    """

    __slots__ = ("__weakref__", "_classes", "_lock", "_module", "_names", "_plan")

    def __init__(self, plan: ProjectionPlan) -> None:
        if not isinstance(plan, ProjectionPlan):
            raise TypeError("ProjectionClassFactory requires a ProjectionPlan")
        self._plan = plan
        self._classes: dict[str, type[RelaxNGElement]] = {}
        self._lock = threading.Lock()
        self._module = _module_name(plan)
        self._names = _class_names(plan)

    @property
    def plan(self) -> ProjectionPlan:
        """The immutable plan this factory belongs to."""
        return self._plan

    @property
    def module(self) -> str:
        """The deterministic synthetic module shared by this plan's classes."""
        return self._module

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
            "__module__": self._module,
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
        assignable: dict[str, Any] = {}
        for contract in route.attributes.values():
            attribute_descriptor = AttributeDescriptor(contract)
            namespace[contract.alias] = attribute_descriptor
            assignable[contract.alias] = attribute_descriptor
        for contract in route.children.values():
            child_descriptor = ChildDescriptor(contract)
            namespace[contract.alias] = child_descriptor
            assignable[contract.alias] = child_descriptor
        scalar_field: str | None = None
        if route.content.scalar_contracts:
            scalar_field = _scalar_field_name(set(namespace))
            scalar_descriptor = ScalarDescriptor(scalar_field)
            namespace[scalar_field] = scalar_descriptor
            assignable[scalar_field] = scalar_descriptor
        namespace["_relaxng_fields_"] = tuple(
            namespace[contract.alias]
            for contract in (*route.attributes.values(), *route.children.values())
        )
        namespace["_relaxng_assignable_fields_"] = MappingProxyType(assignable)
        namespace["_relaxng_scalar_field_"] = scalar_field
        namespace["_relaxng_scalar_descriptor_"] = (
            None if scalar_field is None else namespace[scalar_field]
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


def _require_owned_evidence(
    root: RelaxNGElement, plan: ProjectionPlan, evidence: EvidenceGraph
) -> None:
    """Require complete typed evidence for the supplied plan, root, and revision."""
    if evidence.projection_fingerprint != plan.grammar_fingerprint:
        raise ValueError(
            "projection binding requires evidence from the plan's projection fingerprint"
        )
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
    _require_owned_evidence(root, plan, evidence)
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


@dataclass(frozen=True)
class _PreparedAssignment:
    """One fully encoded lexical write, not yet applied to the tree."""

    target: RelaxNGElement
    attribute: str | None
    lexical: str
    site_kind: str
    site_name: str | None

    def apply(self) -> None:
        """Commit the prepared lexical value through the public tree writers."""
        if self.attribute is None:
            self.target.set_text(self.lexical)
        else:
            self.target.set_attribute(self.attribute, self.lexical)


def _position_context(node: RelaxNGElement) -> DatatypeContext:
    """Return the exact datatype context of a lexical position."""
    return DatatypeContext(node.base_uri, dict(node.namespaces or {}))


def _site_occurrence(
    evidence: EvidenceGraph,
    target: RelaxNGElement,
    kind: str,
    name: str | None,
) -> EvidenceOccurrence | None:
    """Return the evidence site for one lexical assignment target."""
    if kind == "attribute":
        return next(
            (
                occurrence
                for occurrence in evidence.occurrences
                if occurrence.element is target
                and occurrence.kind == "attribute"
                and occurrence.name == name
            ),
            None,
        )
    return next(
        (
            occurrence
            for occurrence in evidence.occurrences
            if occurrence.element is target
            and occurrence.kind == "text"
            and occurrence.content_index == 0
        ),
        None,
    )


def _schema_patterns_for(
    evidence: EvidenceGraph,
    target: RelaxNGElement,
    kind: str,
    name: str | None,
) -> tuple[Pattern, ...]:
    """Return site patterns first, then every compiled pattern as a fallback.

    A newly created optional attribute or an absent scalar has no parse
    occurrence.  Its compiled pattern may still be present elsewhere in the
    evidence graph because the pattern table is pre-pruning and grammar-owned.
    The fallback never inspects instance values; it only finds the exact
    schema-static pattern identity already stored in the document's evidence.
    """
    site = _site_occurrence(evidence, target, kind, name)
    local = () if site is None else tuple(evidence._schema_patterns.get(site, ()))
    return (
        *local,
        *(pattern for patterns in evidence._schema_patterns.values() for pattern in patterns),
    )


def _matching_patterns(
    evidence: EvidenceGraph,
    contract: ScalarContract,
    target: RelaxNGElement,
    kind: str,
    name: str | None,
) -> tuple[Pattern, ...]:
    """Return compiled patterns whose scalar contract equals *contract*."""
    matches = []
    for pattern in _schema_patterns_for(evidence, target, kind, name):
        if scalar_contract(pattern) == contract:
            matches.append(pattern)
    return tuple(matches)


def _datatype_for(
    evidence: EvidenceGraph,
    contract: ScalarContract,
    target: RelaxNGElement,
    kind: str,
    name: str | None,
) -> Datatype | None:
    """Return the compiled datatype encoder for one unique atomic contract."""
    matches = _matching_patterns(evidence, contract, target, kind, name)
    if not matches:
        return None
    return cast(Datatype, cast(Any, matches[0]).datatype)


def _validate_snapshot(
    snapshot: AlternativeValue,
    grammar_fingerprint: str,
    context: DatatypeContext,
) -> None:
    """Require an alternative snapshot to describe this document and context.

    Only lexical text and context are reused.  Candidate values, decodability,
    and validation authority are never imported into the edited revision.  An
    exact context comparison prevents a QName or relative value from silently
    changing meaning because the snapshot was resolved under another prefix
    map or base URI.
    """
    if snapshot.grammar_fingerprint != grammar_fingerprint:
        raise ValueError("the alternative snapshot was produced by a different compiled grammar")
    if snapshot.context.base_uri != context.base_uri:
        raise ValueError("the alternative snapshot's base URI does not match this position")
    if dict(snapshot.context.namespaces) != dict(context.namespaces):
        raise ValueError(
            "the alternative snapshot's namespace context does not match this position"
        )


def _unique_contract(
    contracts: tuple[ScalarContract, ...],
    description: str,
) -> ScalarContract:
    """Require exactly one schema-static encoding at a bare-value position."""
    unique = tuple(dict.fromkeys(contracts))
    if not unique:
        raise ValueError(f"{description} has no schema scalar contract to encode")
    if len(unique) > 1:
        raise AmbiguousValueError(
            f"{description} has {len(unique)} schema scalar contracts; assign an "
            "AlternativeValue snapshot or an explicit lexical value instead"
        )
    return unique[0]


def _encode_bare_value(
    contracts: tuple[ScalarContract, ...],
    value: object,
    context: DatatypeContext,
    evidence: EvidenceGraph,
    target: RelaxNGElement,
    kind: str,
    name: str | None,
    description: str,
) -> str:
    """Encode one bare Python value using only its unique compiled contract."""
    contract = _unique_contract(contracts, description)
    if contract.kind == "list":
        list_pattern = next(
            (
                pattern
                for pattern in _matching_patterns(evidence, contract, target, kind, name)
                if isinstance(pattern, List)
            ),
            None,
        )
        return encode_scalar(contract, value, context, list_pattern=list_pattern)
    datatype = (
        _datatype_for(evidence, contract, target, kind, name) if contract.kind == "atomic" else None
    )
    return encode_scalar(contract, value, context, datatype=datatype)


def _prepare_descriptor(
    node: RelaxNGElement,
    descriptor: Any,
    value: object,
    evidence: EvidenceGraph,
    grammar_fingerprint: str,
) -> _PreparedAssignment:
    """Prepare one descriptor write without touching the lexical tree."""
    if isinstance(descriptor, AttributeDescriptor):
        target = node
        attribute = descriptor.contract.expanded_name
        contracts = descriptor.contract.scalar_contracts
        site_kind = "attribute"
        site_name = attribute
        description = f"attribute {attribute!r}"
    elif isinstance(descriptor, ScalarDescriptor):
        target = node
        attribute = None
        route = cast(RouteContract, cast(Any, type(node))._relaxng_route_)
        contracts = route.content.scalar_contracts
        site_kind = "text"
        site_name = None
        description = "the whole-element scalar position"
    elif isinstance(descriptor, ChildDescriptor):
        contract = descriptor.contract
        if contract.cardinality.repeatable:
            raise TypeError(
                f"child field {contract.alias!r} is repeatable; Task 4 owns child "
                "sequence assignment"
            )
        matches = [child for child in node._children_ if child._name_ == contract.expanded_name]
        if len(matches) != 1:
            raise ValueError(
                f"child field {contract.alias!r} requires exactly one existing "
                "child for typed assignment; Task 4 owns insertion and removal"
            )
        target = matches[0]
        attribute = None
        contracts = contract.scalar_contracts
        site_kind = "text"
        site_name = None
        description = f"child field {contract.alias!r}"
    else:  # pragma: no cover - guarded by the generated descriptor map
        raise TypeError(f"unsupported projection descriptor {type(descriptor).__name__}")

    context = _position_context(target)
    if isinstance(value, AlternativeValue):
        _validate_snapshot(value, grammar_fingerprint, context)
        lexical = value.lexical
    else:
        lexical = _encode_bare_value(
            contracts,
            value,
            context,
            evidence,
            target,
            site_kind,
            site_name,
            description,
        )
    return _PreparedAssignment(target, attribute, lexical, site_kind, site_name)


def _same_occurrence(
    occurrence: EvidenceOccurrence, candidates: Iterable[EvidenceOccurrence]
) -> bool:
    """Compare evidence occurrences by identity, never by equal field values."""
    return any(candidate is occurrence for candidate in candidates)


def _assigned_occurrences(
    evidence: EvidenceGraph,
    prepared: tuple[_PreparedAssignment, ...],
) -> tuple[EvidenceOccurrence, ...]:
    """Return each assigned site and its token children, in evidence order."""
    sites: list[EvidenceOccurrence] = []
    for assignment in prepared:
        site = _site_occurrence(
            evidence, assignment.target, assignment.site_kind, assignment.site_name
        )
        if site is not None and not _same_occurrence(site, sites):
            sites.append(site)
    tokens = [
        occurrence
        for occurrence in evidence.occurrences
        if occurrence.kind == "token"
        and occurrence.parent is not None
        and _same_occurrence(occurrence.parent, sites)
    ]
    return (*sites, *tokens)


def _validated_selections(
    selections: Iterable[CandidateSelection],
    evidence: EvidenceGraph,
    prepared: tuple[_PreparedAssignment, ...],
    current: bool,
) -> tuple[CandidateSelection, ...]:
    """Validate selection scope and joint compatibility before mutation."""
    if isinstance(selections, CandidateSelection):
        raise TypeError("selections must be an iterable of CandidateSelection values")
    requested = tuple(selections)
    for selection in requested:
        if not isinstance(selection, CandidateSelection):
            raise TypeError("joint selections must be CandidateSelection values")
    if not requested:
        return requested
    if not current:
        raise StaleInterpretationError(
            "joint selections require the current typed evidence; "
            "revalidate() before assigning selections"
        )
    allowed = _assigned_occurrences(evidence, prepared)
    for selection in requested:
        occurrence = selection.occurrence
        if not _same_occurrence(occurrence, evidence.occurrences):
            raise ValueError("a joint selection does not belong to the current evidence")
        if not _same_occurrence(occurrence, allowed):
            raise ValueError("a joint selection does not belong to an assigned field occurrence")
    if not evidence.compatible(requested):
        raise ValueError(
            "the requested joint selections are not jointly compatible with "
            "one complete accepting derivation"
        )
    return requested


def assign_fields(
    node: RelaxNGElement,
    fields: dict[str, object],
    selections: Iterable[CandidateSelection],
) -> None:
    """Prepare every descriptor write, then commit one atomic transaction.

    All Python shape checks, datatype encodings, snapshot context checks, and
    joint-selection queries complete before the first lexical write.  A failure
    leaves the tree byte-for-byte unchanged and does not dirty the revision.
    A success dirties the whole revision under the existing mutation contract;
    it never certifies document validity or refreshes typed evidence.
    """
    descriptor_map = getattr(type(node), "_relaxng_assignable_fields_", None)
    if descriptor_map is None:
        raise PyXSDError("RELAX NG typed assignment requires a projected typed element")
    if not fields:
        if isinstance(selections, CandidateSelection) or tuple(selections):
            raise ValueError("selections require at least one assigned field")
        return

    descriptors = []
    for key, value in fields.items():
        descriptor = descriptor_map.get(key)
        if descriptor is None:
            raise TypeError(f"unknown assignable RELAX NG projection field {key!r}")
        descriptors.append((descriptor, value))

    snapshot = node.revision._typed_snapshot
    evidence = getattr(snapshot, "_evidence", None)
    grammar_fingerprint = getattr(snapshot, "_fingerprint", None)
    if not isinstance(evidence, EvidenceGraph) or not isinstance(grammar_fingerprint, str):
        raise PyXSDError(
            "RELAX NG typed assignment requires a valid typed parse with binding evidence"
        )
    current = (
        getattr(snapshot, "current", False) is True
        and not node.revision.dirty
        and evidence._grammar_fingerprint == grammar_fingerprint
    )
    prepared = tuple(
        _prepare_descriptor(node, descriptor, value, evidence, grammar_fingerprint)
        for descriptor, value in descriptors
    )
    _validated_selections(selections, evidence, prepared, current)
    for assignment in prepared:
        assignment.apply()
