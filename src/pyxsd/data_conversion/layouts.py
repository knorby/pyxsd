"""Schema-driven layout planning for the ``pyxsd-json`` profile.

The compiled effective XSD type selects one of the profile's layouts; an
observed document never does. Child occurrence metadata folds compositor
repetition so export, import, and JSON Schema can describe stable JSON keys
and arrays from the schema alone.

Substitution-group members count as possible child names because an
instance may use a member wherever its head is declared. ``xs:all`` and a
single-occurrence ``xs:choice`` of order-insignificant branches are the
profile's order-insignificant models; ``record`` additionally requires
every possible child maximum at most one. An ordered ``xs:sequence``
remains ``sequence`` even when its child names are unique.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pyxsd.data_conversion.contracts import raise_conversion
from pyxsd.xsd_data_types import AnyType, XsdDataType


class Layout(Enum):
    """The ``pyxsd-json`` profile's schema-selected layouts."""

    EMPTY = "empty"
    EMPTY_PLUS = "empty-plus"
    SIMPLE = "simple"
    SIMPLE_PLUS = "simple-plus"
    LIST = "list"
    LIST_PLUS = "list-plus"
    RECORD = "record"
    SEQUENCE = "sequence"
    MIXED = "mixed"


@dataclass(frozen=True)
class ChildPlan:
    """One possible child expanded name and its computed occurrences."""

    name: str
    declaration: Any
    min_occurs: int
    max_occurs: int | None


@dataclass(frozen=True)
class LayoutPlan:
    """The selected layout and the element model it must represent.

    ``has_attributes`` reports ordinary attribute uses; ``has_wildcards``
    reports an element wildcard, open content, or an effective attribute
    wildcard. A plus layout can also arise from ``explicit_type`` alone.
    """

    layout: Layout
    children: tuple[ChildPlan, ...]
    has_attributes: bool
    has_wildcards: bool


@dataclass(frozen=True)
class _Occurrence:
    """One child name's contribution within a particle tree."""

    declaration: Any
    min_occurs: int
    max_occurs: int | None


@dataclass(frozen=True)
class _Facts:
    """Occurrence facts for one particle subtree."""

    children: dict[str, _Occurrence]
    wildcard: bool = False


def plan_layout(xsd_type: Any, *, explicit_type: bool = False) -> LayoutPlan:
    """Return the profile layout for one compiled effective XSD type.

    ``xsd_type`` is the generated class (or built-in datatype class) that
    governs an element. ``explicit_type`` marks a binding that needs an
    ``@xsi:type`` marker, which selects the corresponding plus layout just
    as an ordinary attribute would.

    Raises ``ValueError`` for an argument that is not a compiled XSD type
    class and ``PyXSDError`` when an element-only type has no compiled
    content model.
    """
    if not isinstance(xsd_type, type) or not (
        issubclass(xsd_type, XsdDataType) or _is_schema_base(xsd_type)
    ):
        raise ValueError("plan_layout() requires a compiled XSD type class")

    content_kind = getattr(xsd_type, "_contentKind_", None)
    has_attributes = _has_attributes(xsd_type)
    attribute_wildcard = getattr(xsd_type, "effectiveAttributeWildcard_", None) is not None
    plus = has_attributes or attribute_wildcard or explicit_type

    if content_kind == "simple":
        layout = Layout.SIMPLE_PLUS if plus else Layout.SIMPLE
        return LayoutPlan(layout, (), has_attributes, attribute_wildcard)

    if content_kind == "complex" and _is_simple_content(xsd_type):
        layout = Layout.SIMPLE_PLUS if plus else Layout.SIMPLE
        return LayoutPlan(layout, (), has_attributes, attribute_wildcard)

    model = _compiled_model(xsd_type)
    if content_kind == "complex" and not getattr(xsd_type, "_elementOnly_", True):
        facts = _model_facts(model) if model is not None else _Facts({}, wildcard=True)
        return LayoutPlan(
            Layout.MIXED,
            _child_plans(facts),
            has_attributes,
            facts.wildcard or attribute_wildcard,
        )

    if content_kind != "complex":
        # An unstamped SchemaBase/AnyType class admits mixed content and
        # arbitrary child names; every other datatype class is scalar.
        if _is_schema_base(xsd_type) or issubclass(xsd_type, AnyType):
            return LayoutPlan(Layout.MIXED, (), has_attributes, True)
        layout = Layout.SIMPLE_PLUS if plus else Layout.SIMPLE
        return LayoutPlan(layout, (), has_attributes, attribute_wildcard)

    if model is None:
        raise_conversion(
            "schema-shape-unsupported",
            "",
            f"element-only type {getattr(xsd_type, 'name', xsd_type)!r} "
            "has no compiled content model",
        )

    facts = _model_facts(model)
    children = _child_plans(facts)
    wildcards = facts.wildcard or attribute_wildcard

    if facts.wildcard:
        layout = Layout.SEQUENCE
    elif not children:
        layout = Layout.EMPTY_PLUS if plus else Layout.EMPTY
    elif (
        not attribute_wildcard
        and _is_order_insignificant(model)
        and all(child.max_occurs is not None and child.max_occurs <= 1 for child in children)
    ):
        layout = Layout.RECORD
    elif len(children) == 1 and (children[0].max_occurs is None or children[0].max_occurs > 1):
        layout = Layout.LIST_PLUS if plus else Layout.LIST
    else:
        layout = Layout.SEQUENCE
    return LayoutPlan(layout, children, has_attributes, wildcards)


def _is_simple_content(xsd_type: type) -> bool:
    return getattr(xsd_type, "_simpleContentType_", None) is not None


def _is_schema_base(xsd_type: type) -> bool:
    """Whether *xsd_type* derives from the runtime schema-class base.

    The check is structural because importing ``pyxsd.schema_base`` at
    module load time re-enters the element-representative import cycle;
    every generated complex class and ``SchemaBase`` itself carries the
    base in its MRO.
    """
    return any(
        klass.__name__ == "SchemaBase" and klass.__module__ == "pyxsd.schema_base"
        for klass in xsd_type.__mro__
    )


def _compiled_model(xsd_type: type) -> Any:
    """The class's own effective particle tree, never an inherited stamp."""
    model = xsd_type.__dict__.get("_instanceContentModel_")
    if model is None:
        model = xsd_type.__dict__.get("_contentModel_")
    return model


def _has_attributes(xsd_type: type) -> bool:
    """Whether the effective type has an ordinary (non-prohibited) use."""
    descriptors: dict[str, Any] = {}
    for klass in xsd_type.__mro__:
        for key in klass.__dict__.get("_attributeNames_", ()):
            descriptors.setdefault(key, klass.__dict__.get(key))
    return any(descriptor.getUse() != "prohibited" for descriptor in descriptors.values())


# ---------------------------------------------------------------------------
# Particle occurrence folding
# ---------------------------------------------------------------------------


def _model_facts(particle: Any) -> _Facts:
    """Fold one particle subtree into per-child occurrence facts."""
    if particle.kind == "element":
        return _element_facts(particle)
    if particle.kind == "any":
        return _Facts({}, wildcard=_wildcard_possible(particle.max_occurs))
    if particle.max_occurs == 0:
        return _Facts({})
    parts = [_model_facts(child) for child in particle.children]
    combined = _choice_facts(parts) if particle.kind == "choice" else _sequence_facts(parts)
    return _scale_facts(combined, particle.min_occurs, particle.max_occurs)


def _element_facts(particle: Any) -> _Facts:
    if particle.max_occurs == 0:
        return _Facts({})
    names = _possible_element_names(particle.descriptor)
    if not names:
        return _Facts({})
    minimum = particle.min_occurs if len(names) == 1 else 0
    return _Facts(
        {
            name: _Occurrence(declaration, minimum, particle.max_occurs)
            for name, declaration in names
        }
    )


def _wildcard_possible(max_occurs: int | None) -> bool:
    return max_occurs is None or max_occurs > 0


def _choice_facts(parts: list[_Facts]) -> _Facts:
    """Combine alternatives: one branch occurs per choice occurrence."""
    names: dict[str, None] = {}
    for part in parts:
        for name in part.children:
            names.setdefault(name, None)
    children: dict[str, _Occurrence] = {}
    for name in names:
        declaration = next(
            part.children[name].declaration for part in parts if name in part.children
        )
        minimum = min(
            part.children[name].min_occurs if name in part.children else 0 for part in parts
        )
        maximum = _max_occurrence([part.children.get(name) for part in parts])
        children[name] = _Occurrence(declaration, minimum, maximum)
    return _Facts(children, any(part.wildcard for part in parts))


def _max_occurrence(occurrences: list[_Occurrence | None]) -> int | None:
    """The largest branch occurrence, or ``None`` when any is unbounded."""
    maximum = 0
    for occurrence in occurrences:
        if occurrence is None:
            continue
        if occurrence.max_occurs is None:
            return None
        maximum = max(maximum, occurrence.max_occurs)
    return maximum


def _sequence_facts(parts: list[_Facts]) -> _Facts:
    """Combine co-occurring particles: their occurrences add up."""
    children: dict[str, _Occurrence] = {}
    for part in parts:
        for name, occurrence in part.children.items():
            existing = children.get(name)
            if existing is None:
                children[name] = occurrence
                continue
            children[name] = _Occurrence(
                existing.declaration,
                existing.min_occurs + occurrence.min_occurs,
                _sum_max(existing.max_occurs, occurrence.max_occurs),
            )
    return _Facts(children, any(part.wildcard for part in parts))


def _sum_max(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    return left + right


def _scale_facts(facts: _Facts, minimum: int, maximum: int | None) -> _Facts:
    """Apply a compositor's own occurrence to every child fact."""
    return _Facts(
        {
            name: _Occurrence(
                occurrence.declaration,
                occurrence.min_occurs * minimum,
                None
                if occurrence.max_occurs is None or maximum is None
                else occurrence.max_occurs * maximum,
            )
            for name, occurrence in facts.children.items()
        },
        facts.wildcard,
    )


def _child_plans(facts: _Facts) -> tuple[ChildPlan, ...]:
    return tuple(
        ChildPlan(
            name,
            occurrence.declaration,
            occurrence.min_occurs,
            occurrence.max_occurs,
        )
        for name, occurrence in facts.children.items()
    )


# ---------------------------------------------------------------------------
# Order and substitution analysis
# ---------------------------------------------------------------------------


def _is_order_insignificant(particle: Any) -> bool:
    """Whether the model's language is order-insignificant for JSON objects."""
    term = particle
    while term.synthetic and len(term.children) == 1:
        term = term.children[0]
    if term.kind in ("all", "element"):
        return True
    if term.kind != "choice" or term.max_occurs != 1:
        return False
    return all(_is_order_insignificant(child) for child in term.children)


def _possible_element_names(declaration: Any) -> tuple[tuple[str, Any], ...]:
    """A declaration's instance name plus its transitive members' names.

    Only a global declaration can head a substitution group. An abstract
    declaration cannot appear itself but may still head concrete members.
    A blocked declared head admits no members; a block on an intermediate
    member does not stop the transitive closure, matching the binder.
    """
    names: list[tuple[str, Any]] = []
    seen_names: set[str] = set()
    seen_declarations: set[int] = set()
    members_blocked = _substitution_blocked(declaration)
    pending = [declaration]
    while pending:
        current = pending.pop()
        if id(current) in seen_declarations:
            continue
        seen_declarations.add(id(current))
        name = current.instanceName()
        if name is not None and not _is_abstract(current) and name not in seen_names:
            seen_names.add(name)
            names.append((name, current))
        if members_blocked or not _is_substitution_head(current):
            continue
        pending.extend(reversed(_substitution_members(current)))
    return tuple(names)


def _is_substitution_head(declaration: Any) -> bool:
    is_global = getattr(declaration, "isGlobalDeclaration", None)
    return bool(is_global()) if callable(is_global) else False


def _is_abstract(declaration: Any) -> bool:
    checker = getattr(declaration, "isAbstract", None)
    return bool(checker()) if callable(checker) else False


def _substitution_blocked(declaration: Any) -> bool:
    block = declaration.getBlock()
    return bool(block) and ("substitution" in block.split() or block == "#all")


def _substitution_members(declaration: Any) -> list[Any]:
    schema = declaration.getSchema()
    groups = getattr(schema, "substitutionGroups", None) or {}
    keys: list[Any] = []
    for candidate in (declaration, getattr(declaration, "referredElement", None)):
        if candidate is None:
            continue
        keys.append(getattr(candidate, "expandedName", None))
        keys.append(getattr(candidate, "name", None))
    members: list[Any] = []
    for key in dict.fromkeys(keys):
        members.extend(groups.get(key, ()))
    return members
