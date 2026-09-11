"""Compiled content models for complex types.

The parser's element representatives describe ``xs:sequence``,
``xs:choice``, ``xs:all``, group references and element references as
element representatives. The raw ``sequencesOrChoices`` list flattens
nested compositors and loses their document order, so this module
compiles the representative tree from each type's ``processedChildren``
into a small particle tree and matches an instance's child elements
against it. Valid documents nested inside repeated groups are accepted,
and unmatched or trailing children are reported.

The matcher is deliberately conservative: a particle tree is only
compiled for shapes it understands, and callers fall back to the
legacy flat checks when compilation returns ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_UNBOUNDED_THRESHOLD = 99999


@dataclass
class Particle:
    """One node of a compiled content model.

    ``kind`` is ``sequence``, ``choice``, ``all`` or ``element``.
    ``max_occurs`` of ``None`` means unbounded.
    """

    kind: str
    min_occurs: int = 1
    max_occurs: int | None = 1
    children: list[Particle] = field(default_factory=list)
    name: str | None = None

    def is_element(self) -> bool:
        return self.kind == "element"


def _occurrence(attributes: dict[str, Any]) -> tuple[int, int | None]:
    try:
        minimum = int(attributes.get("minOccurs", 1))
    except (TypeError, ValueError):
        minimum = 1
    raw_max = attributes.get("maxOccurs", 1)
    if raw_max == "unbounded":
        return minimum, None
    try:
        maximum: int | None = int(raw_max)
    except (TypeError, ValueError):
        maximum = 1
    if maximum is not None and maximum >= _UNBOUNDED_THRESHOLD:
        maximum = None
    return minimum, maximum


_CONTENT_WRAPPERS = ("ComplexContent", "Extension", "Restriction", "SimpleContent")
_CONTENT_LEAVES = ("Sequence", "Choice", "All", "Element", "Group")


def _content_children(er: Any) -> list[Any]:
    """Returns a type's content-model children in document order.

    Descends the ``complexContent``/``extension``/``restriction``
    wrappers so the extension's own compositor is found, and preserves
    nested-compositor order (unlike ``sequencesOrChoices``).
    """
    children: list[Any] = []
    for child in getattr(er, "processedChildren", None) or []:
        className = type(child).__name__
        if className in _CONTENT_LEAVES:
            children.append(child)
        elif className in _CONTENT_WRAPPERS:
            children.extend(_content_children(child))
    return children


def compile_content_model(type_er: Any, py_xsd: Any = None) -> Particle | None:
    """Compiles a complex type's own particle tree, composed with its base.

    Returns ``None`` when the shape cannot be represented; callers then
    keep their legacy flat checks. Genuinely empty types compile to an
    empty model that accepts no child elements, so stray children are
    reported.
    """
    content = _content_children(type_er)
    own = _group_particles(_compile_items(content, type_er, frozenset(), py_xsd))
    if own is None and content:
        # Some particle could not be represented; fall back to legacy.
        return None
    derivation, base_model = _base_model(type_er, py_xsd)
    if derivation == "restriction":
        # A restriction replaces the base's particle tree entirely.
        return own if own is not None else _empty_model()
    if derivation == "extension" and base_model is not None:
        if own is None:
            return base_model
        return Particle("sequence", 1, 1, [base_model, own])
    if own is not None:
        return own
    if not content and not getattr(type_er, "superClassNames", None):
        return _empty_model()
    return None


def _empty_model() -> Particle:
    """A model that accepts zero child elements."""
    return Particle("sequence", 1, 1, [])


def _base_model(type_er: Any, py_xsd: Any) -> tuple[str | None, Particle | None]:
    """Returns ``(derivation, base_particle_tree)`` for a derived type."""
    base_names = list(getattr(type_er, "superClassNames", []) or [])
    if not base_names:
        return None, None
    from pyxsd.element_representatives.element_representative import ElementRepresentative

    base_class = None
    for base_name in base_names:
        resolved = type_er.resolveSchemaQName(base_name, parser=py_xsd)
        base_class = ElementRepresentative.typeFromName(resolved, py_xsd)
        if base_class is not None:
            break
    if base_class is None:
        return None, None
    return type_er.getDerivation(), getattr(base_class, "_contentModel_", None)


def _compile_items(
    items: list[Any], owner: Any, visited: frozenset[str], py_xsd: Any
) -> list[Particle]:
    particles: list[Particle] = []
    for item in items:
        particle = _compile_item(item, owner, visited, py_xsd)
        if particle is not None:
            particles.append(particle)
    return particles


def _group_particles(particles: list[Particle]) -> Particle | None:
    """Collapses a list of sibling particles into one, if there are any."""
    if not particles:
        return None
    if len(particles) == 1:
        return particles[0]
    return Particle("sequence", 1, 1, particles)


def _compile_item(item: Any, owner: Any, visited: frozenset[str], py_xsd: Any) -> Particle | None:
    className = type(item).__name__
    if className == "Group":
        if getattr(item, "isRefSite", False):
            return _compile_group_ref(item, owner, visited, py_xsd)
        return None
    if className == "Element":
        return _compile_element(item, py_xsd)
    if className in ("Sequence", "Choice", "All"):
        minimum, maximum = _occurrence(getattr(item, "tagAttributes", {}) or {})
        children = _compile_items(_content_children(item), owner, visited, py_xsd)
        return Particle(className.lower(), minimum, maximum, children)
    return None


def _compile_element(item: Any, py_xsd: Any) -> Particle | None:
    # A reference site adopts the referenced global declaration's name
    # and namespace; an unresolved reference falls back to its local name.
    is_ref = getattr(item, "isElementRef", False)
    target = getattr(item, "referredElement", None) if is_ref else item
    if is_ref and target is None:
        # Some reference sites (e.g. inside named groups) are resolved
        # lazily; resolve here so the particle gets the expanded name.
        schema = item.getSchema()
        resolver = getattr(item, "resolveReference", None)
        if resolver is not None and schema is not None:
            target = resolver(item.ref, schema.elements, parser=py_xsd)
    name: str | None = None
    if target is not None:
        instance_name = getattr(target, "instanceName", None)
        if instance_name is not None:
            try:
                name = instance_name(parser=py_xsd)
            except TypeError:
                name = instance_name()
        if not name:
            name = getattr(target, "name", None)
    if not name and is_ref:
        name = str(getattr(item, "ref", "")).split(":")[-1]
    if not name:
        return None
    minimum = item.getMinOccurs()
    maximum = item.getMaxOccurs()
    if maximum >= _UNBOUNDED_THRESHOLD:
        maximum = None
    return Particle("element", minimum, maximum, [], name)


def _compile_group_ref(
    ref_site: Any, owner: Any, visited: frozenset[str], py_xsd: Any
) -> Particle | None:
    ref = str(getattr(ref_site, "ref", ""))
    if not ref:
        return None
    schema = owner.getSchema()
    if schema is None:
        return None
    group = None
    resolver = getattr(ref_site, "resolveReference", None)
    if resolver is not None:
        group = resolver(ref, schema.groups.values(), parser=py_xsd)
    if group is None:
        group = schema.groups.get(ref) or schema.groups.get(ref.split(":")[-1])
    if group is None:
        return None
    group_key = getattr(group, "expandedName", None) or ref.split(":")[-1]
    if group_key in visited:
        return None
    compositor = group.getCompositor()
    if compositor is None:
        return None
    inner = _compile_item(compositor, owner, visited | {group_key}, py_xsd)
    if inner is None:
        return None
    minimum, maximum = _occurrence(getattr(ref_site, "tagAttributes", {}) or {})
    # The reference repeats the whole group as a unit.
    return Particle("sequence", minimum, maximum, [inner])


def particle_names(model: Particle | None) -> set[str]:
    """The declared element names anywhere in a particle tree."""
    if model is None:
        return set()
    names: set[str] = set()
    if model.name:
        names.add(model.name)
    for child in model.children:
        names |= particle_names(child)
    return names


def first_required_name(model: Particle | None) -> str | None:
    """The first element name that must occur at least once, if any."""
    if model is None:
        return None
    if model.is_element():
        return model.name if model.min_occurs > 0 else None
    for child in model.children:
        name = first_required_name(child)
        if name is not None:
            return name
    return None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _name_of(node: Any) -> str:
    return node.tag.split("}")[-1]


def expanded_name_of(node: Any) -> str:
    """Instance-name function for strict namespace mode.

    ElementTree stores a namespaced tag as a Clark name, so the tag
    itself is the expanded name; an unqualified tag is already a plain
    local name.
    """
    return node.tag


def _accepts(node_name: str, particle: Particle, member_head_map: dict[str, str]) -> bool:
    head = member_head_map.get(node_name, node_name)
    return particle.name == head


def _dedup(states: list[tuple[int, list[Any]]]) -> list[tuple[int, list[Any]]]:
    seen: set[int] = set()
    result: list[tuple[int, list[Any]]] = []
    for state in states:
        if state[0] in seen:
            continue
        seen.add(state[0])
        result.append(state)
    return result


def _match_one(
    particle: Particle,
    nodes: list[Any],
    position: int,
    member_head_map: dict[str, str],
    depth: int,
    name_of: Any,
) -> list[tuple[int, list[Any]]]:
    if depth > 32:
        return []
    if particle.is_element():
        if position < len(nodes) and _accepts(name_of(nodes[position]), particle, member_head_map):
            return [(position + 1, [nodes[position]])]
        return []
    if particle.kind == "sequence":
        states: list[tuple[int, list[Any]]] = [(position, [])]
        for child in particle.children:
            advanced: list[tuple[int, list[Any]]] = []
            for start, matched in states:
                for end, more in _match_repeated(
                    child, nodes, start, member_head_map, depth + 1, name_of
                ):
                    advanced.append((end, matched + more))
            states = _dedup(advanced)
            if not states:
                return []
        return states
    if particle.kind == "choice":
        # One occurrence of a choice is one occurrence of one branch.
        results: list[tuple[int, list[Any]]] = []
        for branch in particle.children:
            results.extend(
                _match_repeated(branch, nodes, position, member_head_map, depth + 1, name_of)
            )
        return _dedup(results)
    if particle.kind == "all":
        return _match_all(particle, nodes, position, member_head_map, name_of)
    return []


def _match_all(
    particle: Particle,
    nodes: list[Any],
    position: int,
    member_head_map: dict[str, str],
    name_of: Any,
) -> list[tuple[int, list[Any]]]:
    counts = [0] * len(particle.children)
    matched: list[Any] = []
    index = position
    while index < len(nodes):
        node_name = name_of(nodes[index])
        chosen = None
        for i, member in enumerate(particle.children):
            limit = member.max_occurs
            if limit is not None and counts[i] >= limit:
                continue
            if _accepts(node_name, member, member_head_map):
                chosen = i
                break
        if chosen is None:
            break
        counts[chosen] += 1
        matched.append(nodes[index])
        index += 1
    for i, member in enumerate(particle.children):
        if counts[i] < member.min_occurs:
            return []
    return [(index, matched)]


def _match_repeated(
    particle: Particle,
    nodes: list[Any],
    position: int,
    member_head_map: dict[str, str],
    depth: int,
    name_of: Any,
) -> list[tuple[int, list[Any]]]:
    results: list[tuple[int, list[Any]]] = []
    if particle.min_occurs == 0:
        results.append((position, []))
    frontier: list[tuple[int, list[Any]]] = [(position, [])]
    count = 0
    maximum = particle.max_occurs
    if maximum is None:
        maximum = len(nodes) + 1
    while frontier and count < maximum:
        count += 1
        advanced: list[tuple[int, list[Any]]] = []
        for start, matched in frontier:
            for end, more in _match_one(particle, nodes, start, member_head_map, depth, name_of):
                # A zero-width match is a real occurrence (for example a
                # ``minOccurs="1"`` sequence with no children); keep it so
                # ``min_occurs`` is satisfied. The ``count < maximum``
                # guard keeps the frontier finite.
                advanced.append((end, matched + more))
        advanced = _dedup(advanced)
        if count >= particle.min_occurs:
            results.extend(advanced)
        frontier = advanced
    return _dedup(results)


def match_content(
    model: Particle,
    nodes: list[Any],
    member_head_map: dict[str, str] | None = None,
    name_of: Any = _name_of,
) -> tuple[bool, list[Any]]:
    """Matches child elements against a compiled content model.

    ``name_of`` extracts the name an instance node is matched by:
    the local name in legacy mode (the default) or the expanded tag in
    strict namespace mode.

    Returns ``(complete, leftover)``: ``complete`` is True when the whole
    model is satisfied and consumes every node; ``leftover`` is the
    unconsumed tail of the best partial match.
    """
    member_head_map = member_head_map or {}
    states = _match_repeated(model, nodes, 0, member_head_map, 0, name_of)
    for end, _ in states:
        if end == len(nodes):
            return True, []
    best = max((state[0] for state in states), default=0)
    return False, list(nodes[best:])
