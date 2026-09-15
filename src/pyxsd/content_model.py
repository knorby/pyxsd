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

import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from pyxsd.namespaces import namespace_of
from pyxsd.wildcards import WildcardSpec, wildcard_spec

_UNBOUNDED_THRESHOLD = 99999


@dataclass
class Particle:
    """One node of a compiled content model.

    ``kind`` is ``sequence``, ``choice``, ``all``, ``element`` or
    ``any``. ``max_occurs`` of ``None`` means unbounded. An ``any``
    particle carries the wildcard's :class:`WildcardSpec` in ``spec``.
    An ``element`` particle carries the declaration that position uses
    in ``descriptor``, so binding validates each occurrence through the
    particle that consumed it instead of re-deriving a declaration by
    name.
    """

    kind: str
    min_occurs: int = 1
    max_occurs: int | None = 1
    children: list[Particle] = field(default_factory=list)
    name: str | None = None
    spec: WildcardSpec | None = None
    descriptor: Any = None
    #: True for the ``sequence`` wrapper the compiler emits around a
    #: group reference's compositor. The reference is transparent in the
    #: component model — the compositor takes the reference site's
    #: occurrence — so derivation checks fold the two ranges together
    #: instead of treating the wrapper as a literal sequence.
    synthetic: bool = False

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
_CONTENT_LEAVES = ("Sequence", "Choice", "All", "Element", "Group", "Any")


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


def compile_own_content(type_er: Any, py_xsd: Any = None) -> Particle | None:
    """Compiles a complex type's own explicit particle tree.

    Unlike :func:`compile_content_model` this never composes the base
    type's model in: it is the type's own content model exactly as
    written (for an extension, its suffix). Returns ``None`` when the
    shape cannot be represented or the type has no content at all.
    """
    content = _content_children(type_er)
    own = _group_particles(_compile_items(content, type_er, frozenset(), py_xsd))
    if own is None and content:
        return None
    return own


def compile_content_model(type_er: Any, py_xsd: Any = None) -> Particle | None:
    """Compiles a complex type's own particle tree, composed with its base.

    Returns ``None`` when the shape cannot be represented; callers then
    keep their legacy flat checks. Genuinely empty types compile to an
    empty model that accepts no child elements, so stray children are
    reported.

    An extension whose suffix is an ``all`` and whose base's effective
    model is also an ``all`` composes into a single ``all`` (XSD 1.1
    §3.4.2.3.3 clause 4.2.3.2): the base's particles lead and the
    suffix supplies the occurrence. Every other extension is
    ``sequence[base, own]``.
    """
    own = compile_own_content(type_er, py_xsd)
    if own is None and _content_children(type_er):
        # Some particle could not be represented; fall back to legacy.
        return None
    derivation, base_model = _base_model(type_er, py_xsd)
    if derivation == "restriction":
        # A restriction replaces the base's particle tree entirely.
        return own if own is not None else _empty_model()
    if derivation == "extension" and base_model is not None:
        if own is None:
            return base_model
        composition = all_extension_composition(base_model, own)
        if composition is not None:
            return composition
        return Particle("sequence", 1, 1, [base_model, own])
    if own is not None:
        return own
    if not _content_children(type_er) and not getattr(type_er, "superClassNames", None):
        return _empty_model()
    return None


def all_term(particle: Particle) -> Particle | None:
    """The ``all`` term of a particle, seeing through a group reference.

    A group reference compiles to a synthetic sequence wrapper; the
    reference is transparent in the component model, so the referenced
    ``all`` group is the term. Returns ``None`` when the particle's
    term is not an ``all``.
    """
    if particle.kind == "all":
        return particle
    if particle.synthetic and len(particle.children) == 1 and particle.children[0].kind == "all":
        return particle.children[0]
    return None


def all_extension_composition(base_model: Particle, own: Particle) -> Particle | None:
    """Composes an all-over-all extension into one ``all`` particle.

    XSD 1.1 §3.4.2.3.3 clause 4.2.3.2: the composed ``all``'s particles
    are the base all's followed by the extension all's, and its
    occurrence is the extension particle's own ``minOccurs`` (with a
    group-reference wrapper's occurrence folded in). ``None`` when
    either side's term is not an ``all``.
    """
    base_term = all_term(base_model)
    if base_term is None:
        return None
    occurrence = all_extension_occurrence(own)
    if occurrence is None:
        return None
    own_term, minimum = occurrence
    return Particle("all", minimum, 1, [*base_term.children, *own_term.children])


def all_extension_occurrence(particle: Particle) -> tuple[Particle, int] | None:
    """The ``all`` term of an extension suffix and its ``minOccurs``.

    For a direct ``all`` the occurrence is the particle's own; for a
    group-reference wrapper the reference's occurrence multiplies the
    referenced ``all`` group's. ``None`` when the term is not an
    ``all``.
    """
    if particle.kind == "all":
        return particle, particle.min_occurs
    if particle.synthetic and len(particle.children) == 1 and particle.children[0].kind == "all":
        inner = particle.children[0]
        return inner, particle.min_occurs * inner.min_occurs
    return None


def all_members(model: Particle) -> list[Particle]:
    """The member particles of an ``all`` term, group references flattened.

    A group reference inside an ``all`` is transparent (all007): the
    referenced ``all`` group's members are members of the enclosing
    ``all``. Nested ``all`` terms (from group references) are spliced in
    at any depth; every other particle is a member as written.
    """
    if model.synthetic and len(model.children) == 1 and model.children[0].kind == "all":
        return all_members(model.children[0])
    members: list[Particle] = []
    for child in model.children:
        term = all_term(child)
        if term is not None:
            members.extend(all_members(term))
        else:
            members.append(child)
    return members


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
    if className == "Any":
        return _compile_any(item)
    if className in ("Sequence", "Choice", "All"):
        minimum, maximum = _occurrence(getattr(item, "tagAttributes", {}) or {})
        children = _compile_items(_content_children(item), owner, visited, py_xsd)
        if className == "All":
            children = _flatten_all_group_members(children)
        return Particle(className.lower(), minimum, maximum, children)
    return None


def _flatten_all_group_members(children: list[Particle]) -> list[Particle]:
    """Splices a group reference's ``all`` members into the enclosing all.

    A group reference inside an ``all`` must name an ``all`` group and
    carry ``minOccurs=maxOccurs=1`` (the ``all`` rule), so the reference
    is transparent: its group's members are members of the enclosing
    ``all`` and match in any order (all007). A wrapper that does not
    name an ``all`` (a malformed schema) or repeats is left in place for
    the checks that report it.
    """
    flattened: list[Particle] = []
    for child in children:
        if (
            child.synthetic
            and child.min_occurs == 1
            and child.max_occurs == 1
            and len(child.children) == 1
            and child.children[0].kind == "all"
        ):
            flattened.extend(child.children[0].children)
        else:
            flattened.append(child)
    return flattened


def _compile_any(item: Any) -> Particle:
    """Compiles an ``xs:any`` wildcard into an ``any`` particle.

    The wildcard's namespace constraint rides along in ``spec``, along
    with the target namespace of the document that declared it (so
    ``##targetNamespace``/``##other`` keep their source meaning when a
    derived type inherits the wildcard).
    """
    attributes = getattr(item, "tagAttributes", {}) or {}
    minimum, maximum = _occurrence(attributes)
    return Particle(
        "any",
        minimum,
        maximum,
        [],
        None,
        wildcard_spec(
            attributes,
            is_attribute=False,
            target_namespace=item.getNamespace(),
        ),
    )


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
    # Carry the resolved declaration: a reference site delegates its
    # constraints to the referred declaration, and that declaration is
    # the one whose type and ``fixed`` value validate the occurrence.
    descriptor = target if target is not None else item
    return Particle("element", minimum, maximum, [], name, None, descriptor=descriptor)


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
    # The reference repeats the whole group as a unit. The wrapper is
    # marked synthetic: the reference is not a real sequence particle,
    # it stands in for the referenced compositor at the reference
    # site's occurrence (derivation checks fold the ranges together).
    return Particle("sequence", minimum, maximum, [inner], synthetic=True)


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
    """The first particle that must occur at least once, if any."""
    if model is None:
        return None
    if model.is_element() or model.kind == "any":
        if model.min_occurs > 0:
            return model.name or "wildcard content"
        return None
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


class ChildMatch(NamedTuple):
    """One consumed instance node and the particle that admitted it.

    ``position`` is the node's position in the list passed to
    :func:`match_content_associations`; ``particle`` is the element or
    wildcard particle that consumed it. Binding uses these records
    instead of searching the declarations again, so a child admitted by
    a specific wildcard is validated and bound through *that* wildcard.
    """

    position: int
    particle: Particle


class _MatchContext(NamedTuple):
    """Immutable per-call matching context.

    Threading one object keeps the recursive matcher signatures short;
    ``namespace_checked`` distinguishes strict namespace matching from
    the legacy rule that every undeclared name is wildcard content.
    Wildcard precedence is positional: a wildcard particle may consume
    a declared name when the wildcard is the particle active at that
    point (so a repeated declaration can flow into a later element
    particle), and declared particles consume their names where they
    appear in the model.
    """

    member_head_map: dict[str, str]
    name_of: Any
    target_namespace: str | None
    namespace_checked: bool


def _accepts(node_name: str, particle: Particle, ctx: _MatchContext) -> bool:
    if particle.kind == "any":
        if not ctx.namespace_checked or particle.spec is None:
            return True
        return particle.spec.allows(namespace_of(node_name), ctx.target_namespace)
    head = ctx.member_head_map.get(node_name, node_name)
    return particle.name == head


def _match_all(
    particle: Particle,
    nodes: list[Any],
    position: int,
    ctx: _MatchContext,
) -> tuple[int | None, list[ChildMatch]]:
    """Greedily match an ``xs:all`` particle.

    Returns ``(end, matches)``: the position after the last consumed
    node (``None`` when a minimum occurrence bound is unmet) and the
    ordered association records for the nodes consumed.
    """
    counts = [0] * len(particle.children)
    matches: list[ChildMatch] = []
    index = position
    while index < len(nodes):
        node_name = ctx.name_of(nodes[index])
        chosen = None
        for i, member in enumerate(particle.children):
            limit = member.max_occurs
            if limit is not None and counts[i] >= limit:
                continue
            if _accepts(node_name, member, ctx):
                chosen = i
                break
        if chosen is None:
            break
        counts[chosen] += 1
        matches.append(ChildMatch(index, particle.children[chosen]))
        index += 1
    for i, member in enumerate(particle.children):
        if counts[i] < member.min_occurs:
            return None, []
    return index, matches


# The matcher returns sets of end positions and memoizes by particle and
# start position. Real-world schemas (for example WordprocessingML's
# ``CT_Body``) nest unbounded compositors deeply enough that an unmemoized
# backtracking search over a document with dozens of children explodes
# combinatorially. Because a match consumes a contiguous run of nodes,
# only the end position matters, which makes the result a pure function
# of ``(particle, position)`` and safe to cache per call.


def _ends_one(
    particle: Particle,
    nodes: list[Any],
    position: int,
    ctx: _MatchContext,
    memo: dict[Any, frozenset[int]],
    depth: int,
) -> frozenset[int]:
    """End positions after matching exactly one occurrence of *particle*."""
    if depth > 32:
        return frozenset()
    key = (id(particle), position)
    cached = memo.get(key)
    if cached is not None:
        return cached

    if particle.is_element() or particle.kind == "any":
        if position < len(nodes) and _accepts(ctx.name_of(nodes[position]), particle, ctx):
            result = frozenset({position + 1})
        else:
            result = frozenset()
    elif particle.kind == "sequence":
        current: set[int] = {position}
        for child in particle.children:
            advanced: set[int] = set()
            for start in current:
                advanced |= _ends_repeated(child, nodes, start, ctx, memo, depth + 1)
            current = advanced
            if not current:
                break
        result = frozenset(current)
    elif particle.kind == "choice":
        ends: set[int] = set()
        for branch in particle.children:
            ends |= _ends_repeated(branch, nodes, position, ctx, memo, depth + 1)
        result = frozenset(ends)
    elif particle.kind == "all":
        end, _ = _match_all(particle, nodes, position, ctx)
        result = frozenset() if end is None else frozenset({end})
    else:
        result = frozenset()

    memo[key] = result
    return result


def _ends_repeated(
    particle: Particle,
    nodes: list[Any],
    position: int,
    ctx: _MatchContext,
    memo: dict[Any, frozenset[int]],
    depth: int,
) -> frozenset[int]:
    """End positions for a particle repeated between its occurrence bounds."""
    key = ("rep", id(particle), position)
    cached = memo.get(key)
    if cached is not None:
        return cached

    results: set[int] = set()
    if particle.min_occurs == 0:
        results.add(position)
    frontier: set[int] = {position}
    count = 0
    maximum = particle.max_occurs
    if maximum is None:
        maximum = len(nodes) + 1
    while frontier and count < maximum:
        count += 1
        advanced: set[int] = set()
        for start in frontier:
            advanced |= _ends_one(particle, nodes, start, ctx, memo, depth)
        if count >= particle.min_occurs:
            results |= advanced
        if advanced == frontier and count >= particle.min_occurs:
            # A zero-width particle has reached its fixed point; further
            # repetitions cannot reach a new position.
            break
        frontier = advanced

    result = frozenset(results)
    memo[key] = result
    return result


def _trace_one(
    particle: Particle,
    nodes: list[Any],
    start: int,
    target: int,
    ctx: _MatchContext,
    memo: dict[Any, frozenset[int]],
    out: list[ChildMatch],
) -> None:
    """Records associations for one occurrence of ``particle``.

    Precondition: ``target`` is in the particle's one-occurrence end set
    from ``start``. The walker uses the memoized end sets to choose a
    feasible path, so it reconstructs a real match rather than a greedy
    approximation.
    """
    if particle.is_element() or particle.kind == "any":
        out.append(ChildMatch(start, particle))
        return
    if particle.kind == "all":
        end, matches = _match_all(particle, nodes, start, ctx)
        if end == target:
            out.extend(matches)
        return
    if particle.kind == "choice":
        for branch in particle.children:
            if target in _ends_repeated(branch, nodes, start, ctx, memo, 0):
                _trace_repeated(branch, nodes, start, target, ctx, memo, out)
                return
        return
    if particle.kind == "sequence":
        children = particle.children
        if not children or start == target:
            return
        # Backward feasibility: can[i] holds the positions from which
        # children[i:] can reach the target. Then walk forward choosing
        # a feasible split point for each child. ``target`` itself is a
        # valid position: a zero-width (optional) suffix may leave the
        # remaining children to consume nothing, so excluding it would
        # starve the feasibility sets and drop real associations.
        can: list[set[int]] = [set() for _ in range(len(children) + 1)]
        can[-1] = {target}
        for i in range(len(children) - 1, -1, -1):
            for position in range(start, target + 1):
                if _ends_repeated(children[i], nodes, position, ctx, memo, 0) & can[i + 1]:
                    can[i].add(position)
        position = start
        for index, child in enumerate(children):
            ends = [
                end
                for end in sorted(_ends_repeated(child, nodes, position, ctx, memo, 0))
                if end in can[index + 1]
            ]
            if not ends:
                return
            end = ends[0]
            _trace_repeated(child, nodes, position, end, ctx, memo, out)
            position = end
        return


def _trace_repeated(
    particle: Particle,
    nodes: list[Any],
    start: int,
    target: int,
    ctx: _MatchContext,
    memo: dict[Any, frozenset[int]],
    out: list[ChildMatch],
) -> None:
    """Records associations for a bounded repetition of ``particle``.

    Precondition: ``target`` is in the particle's repeated end set from
    ``start``. A breadth-first search over ``(position, steps)`` finds a
    chain of one-occurrence steps satisfying the occurrence bounds.
    """
    if start == target and particle.min_occurs == 0:
        return
    maximum = particle.max_occurs
    if maximum is None:
        maximum = len(nodes) + 1
    start_state = (start, 0)
    queue: deque[tuple[int, int]] = deque([start_state])
    seen = {start_state}
    previous: dict[tuple[int, int], tuple[int, int] | None] = {start_state: None}
    found: tuple[int, int] | None = None
    while queue:
        position, steps = queue.popleft()
        if steps >= particle.min_occurs and position == target:
            found = (position, steps)
            break
        if steps >= maximum:
            continue
        for end in sorted(_ends_one(particle, nodes, position, ctx, memo, 0)):
            state = (end, steps + 1)
            if state in seen:
                continue
            if end == position and steps + 1 > particle.min_occurs:
                # A zero-width occurrence beyond the minimum cannot
                # make progress; stop the chain from looping forever.
                continue
            seen.add(state)
            previous[state] = (position, steps)
            queue.append(state)
    if found is None:
        return
    chain: list[tuple[int, int]] = []
    cursor: tuple[int, int] | None = found
    while cursor is not None:
        chain.append(cursor)
        cursor = previous[cursor]
    chain.reverse()
    for (before, _), (after, _) in itertools.pairwise(chain):
        _trace_one(particle, nodes, before, after, ctx, memo, out)


def match_content_associations(
    model: Particle,
    nodes: list[Any],
    member_head_map: dict[str, str] | None = None,
    name_of: Any = _name_of,
    target_namespace: str | None = None,
    namespace_checked: bool = False,
) -> tuple[bool, list[Any], list[ChildMatch]]:
    """Matches children and reports which particle admitted each node.

    ``name_of`` extracts the name an instance node is matched by: the
    local name in legacy mode (the default) or the expanded tag in
    strict namespace mode. ``namespace_checked`` turns on namespace
    checking for wildcard particles (strict namespace mode); in legacy
    mode a wildcard absorbs any name no declared particle claims.

    Returns ``(complete, leftover, associations)``: ``complete`` is True
    when the whole model is satisfied and consumes every node;
    ``leftover`` is the unconsumed tail of the best partial match;
    ``associations`` maps each consumed node index to the element or
    wildcard particle that admitted it.
    """
    member_head_map = member_head_map or {}
    ctx = _MatchContext(
        member_head_map,
        name_of,
        target_namespace,
        namespace_checked,
    )
    memo: dict[Any, frozenset[int]] = {}
    ends = _ends_repeated(model, nodes, 0, ctx, memo, 0)
    complete = len(nodes) in ends
    target = len(nodes) if complete else max(ends, default=0)
    out: list[ChildMatch] = []
    if complete or target > 0 or model.min_occurs == 0:
        _trace_repeated(model, nodes, 0, target, ctx, memo, out)
    out.sort(key=lambda match: match.position)
    if complete:
        return True, [], out
    return False, list(nodes[target:]), out


def match_content(
    model: Particle,
    nodes: list[Any],
    member_head_map: dict[str, str] | None = None,
    name_of: Any = _name_of,
    target_namespace: str | None = None,
    namespace_checked: bool = False,
) -> tuple[bool, list[Any]]:
    """Matches child elements against a compiled content model.

    Returns ``(complete, leftover)``; see
    :func:`match_content_associations` for the association records that
    binding consumes.
    """
    complete, leftover, _ = match_content_associations(
        model,
        nodes,
        member_head_map,
        name_of,
        target_namespace,
        namespace_checked,
    )
    return complete, leftover
