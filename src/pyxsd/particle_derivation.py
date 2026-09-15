"""Particle-valid restriction checks (cos-particle-restrict, XSD 1.0 §3.9.6).

Pure predicate over compiled :class:`~pyxsd.content_model.Particle` trees:
``is_valid_particle_restriction(base, derived, resolver)`` returns the list
of violation reasons for deriving ``derived`` by restriction from ``base``
(an empty list means the restriction is valid). The parser's content-model
sweep calls this for every complex type whose derivation is a restriction
with a particle; this module never imports the parser.

``1..1`` sequences holding a single particle are transparent for shape
decisions — the pointless-particle elimination §3.9.6 prescribes before
the table applies (the compiler wraps a group reference's compositor in
exactly such a sequence, and the corpus pins eliminated shapes as legal,
e.g. an element restricting an all).

Rule cells implemented so far (later sub-tasks extend this module):

* ``Forbidden``: a wildcard can never restrict an element declaration or
  a model group; an ``all`` cannot restrict or be restricted by another
  compositor.
* ``NSSubset`` (any over any): namespace-constraint subsetting
  (:func:`wildcard_subset`), processContents non-widening and occurrence
  containment.
* Element over any (RecurseAsIfGroup's base case): occurrence
  containment and the wildcard must admit the element's namespace.
* ``NameAndTypeOK`` (element over element): occurrence containment,
  expanded-name equality or transitive substitution-group membership,
  type subsumption (the derived declaration's type must be the base
  type or validly derived from it with no ``extension`` step),
  ``fixed`` value preservation, ``nillable`` direction, the base's
  disallowed substitutions as a subset of the derived's, and the base
  declaration's identity constraints as a subset of the derived's.
  Every declaration clause is skipped — never an error — when either
  declaration cannot be resolved.
* Occurrence containment for a group over a wildcard or over an element
  (``NSRecurseCheckCardinality``/``GroupOverElement``), for sequence
  over choice (``MapAndSum``) and for same-kind compositors
  (``Recurse``).
* ``Recurse`` pairing: sequences align order-preservingly (derived
  members map onto a subsequence of the base members, in order; base
  members left out must be able to match zero instances); choices map
  each derived member onto a *distinct* base member it validly
  restricts (order-insensitive, injective); ``all`` over ``all`` and a
  sequence over an ``all`` (RecurseUnordered) map order-insensitively
  with the mapped members' occurrence ranges summed per base member,
  every required base member covered, and split coverage for a derived
  wildcard spread over several base wildcards. A choice over an ``all``
  restricts it when every branch restricts the ``all`` on its own.
* ``EltOverGroup`` (RecurseAsIfGroup): an element restricting a
  choice/sequence/all base is wrapped in a singleton group of the
  base's variety at 1..1 and that wrapper is checked with the matching
  Recurse rule — the compositor's range gates the wrapper while the
  element's own range is weighed against the member it maps onto
  (particlesL/K/M, the J wildcard-base residues, W014).
* ``MapAndSum``: a sequence restricting a choice maps every derived
  member onto *some* validly-restricted base member (not injectively,
  not order-preservingly) and checks the sequence's effective total
  range against the choice's range (particlesV).
* ``NSRecurseCheckCardinality``: a group restricting a wildcard admits
  each of its members to the wildcard's namespaces and checks the
  group's effective total range against the wildcard's (particlesQ/R,
  the sequence-of-any J residues).
* Deliberately silent: a model group over an element (the 1.0 table
  forbids the shape, but the corpus pins the exemplars valid under the
  1.1 profile — particlesHb008/Hb011 — where the enclosing
  alignments absorb the members; a per-pair check cannot be stated
  without regressing them).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyxsd.content_model import Particle
from pyxsd.derivation import derived_from_union_member, is_validly_derived
from pyxsd.namespaces import local_name
from pyxsd.wildcards import (
    DISALLOWED_DEFINED,
    DISALLOWED_SIBLING,
    WildcardSpec,
    disallowed_name_parts,
    excluded_locals,
    excludes_all_locals,
)

#: Sentinel namespace probed so ``##other`` and URI lists are compared by
#: extension rather than by spelling: a constraint that admits a
#: namespace neither constraint mentions cannot be detected otherwise.
_FOREIGN_PROBE_URI = "http://pyxsd.invalid/particle-restriction-probe"

#: ``processContents`` permissiveness: a derived wildcard must not be
#: more permissive (weaker validation) than its base.
_PROCESS_SEVERITY = {"skip": 0, "lax": 1, "strict": 2}

#: The shape table of cos-particle-restrict: ``(derived kind, base kind)``
#: to the rule name that decides the pair. ``Forbidden`` marks transitions
#: that are never valid. ``EltOverAny``/``EltOverGroup`` are the corpus's
#: reading of RecurseAsIfGroup — the MS suite pins an element restricting
#: a wildcard or group base as *valid* when the deep derivation holds
#: (particlesJa-Jq, particlesK001, particlesM002/M003), against a literal
#: reading that would forbid them.
_SHAPE_RULES: dict[tuple[str, str], str] = {
    ("element", "element"): "NameAndTypeOK",
    ("element", "any"): "RecurseAsIfGroup",
    ("element", "choice"): "EltOverGroup",
    ("element", "sequence"): "EltOverGroup",
    ("element", "all"): "EltOverGroup",
    ("any", "element"): "Forbidden",
    ("any", "any"): "NSSubset",
    ("any", "choice"): "Forbidden",
    ("any", "sequence"): "Forbidden",
    ("any", "all"): "WildcardOverAll",
    ("choice", "element"): "GroupOverElement",
    ("choice", "any"): "NSRecurseCheckCardinality",
    ("choice", "choice"): "Recurse",
    ("choice", "sequence"): "MapAndSum",
    ("choice", "all"): "ChoiceOverAll",
    ("sequence", "element"): "GroupOverElement",
    ("sequence", "any"): "NSRecurseCheckCardinality",
    ("sequence", "choice"): "MapAndSum",
    ("sequence", "sequence"): "Recurse",
    ("sequence", "all"): "RecurseUnordered",
    ("all", "element"): "GroupOverElement",
    ("all", "any"): "NSRecurseCheckCardinality",
    ("all", "choice"): "Forbidden",
    ("all", "sequence"): "Forbidden",
    ("all", "all"): "Recurse",
}

Resolver = Callable[[Particle], Any]
HeadLookup = Callable[[Any], Any]
MemberLookup = Callable[[Any], list[Any]]

#: Cells this module deliberately leaves silent because every partial
#: approximation of them rejects corpus-pinned valid schemas:
#:
#: * ``GroupOverElement`` — 1.0 forbids the shape, and the corpus pins
#:   the exemplars valid under the 1.1 profile via the 1.1
#:   language-inclusion reading (particlesHb008/Hb011): a derived
#:   choice sits inside a derived sequence and absorbs several base
#:   members at once. The enclosing sequence alignment already accepts
#:   those exemplars; a per-pair check (all branches must restrict the
#:   one base element) would reject Hb008, whose choice(e3, e4) member
#:   spreads over the optional base e3 and e4. The cell therefore stays
#:   silent rather than approximate the absorption.
_DEFERRED_CELLS = frozenset({"GroupOverElement"})


def contains_occurs(
    derived_min: int, derived_max: int | None, base_min: int, base_max: int | None
) -> bool:
    """Whether the derived occurrence range is contained in the base's.

    ``None`` as a maximum means unbounded.
    """
    if derived_min < base_min:
        return False
    if base_max is None:
        return True
    return derived_max is not None and derived_max <= base_max


def wildcard_subset(
    derived_spec: WildcardSpec, base_spec: WildcardSpec, target_namespace: str | None = None
) -> bool:
    """Whether the derived wildcard's constraint is a subset.

    The namespace relation is probed against a canonical namespace set —
    the absent namespace, the target namespace, a foreign probe URI and
    every literal URI in either constraint — so list constraints are
    compared by extension (``##any`` ⊇ ``##local ##targetNamespace uri``
    and ``{foo}`` ⊆ ``{foo bar}`` hold while ``{abce}`` ⊆ ``{foo bar}``
    does not) instead of by string equality. Each spec resolves
    ``##targetNamespace``/``##other`` against its own recorded source
    namespace, falling back to ``target_namespace`` when it carries
    none.

    The XSD 1.1 exclusion sets are part of the relation (§3.10.6.2
    Wildcard Subset): the namespace probes go through
    :meth:`WildcardSpec.admits_namespace`, so a ``notNamespace``
    exclusion counts, and the ``{disallowed names}`` clause then requires
    every QName the base disallows to be disallowed by the derived as
    well (or to lie outside the derived's admitted namespaces) and the
    ``##defined``/``##definedSibling`` keywords to be inherited by the
    derived.
    """
    probes: set[str | None] = {None, target_namespace, _FOREIGN_PROBE_URI}
    for spec in (derived_spec, base_spec):
        probes.add(spec.effective_target(target_namespace))
        for token in (*spec.namespace.split(), *spec.not_namespace):
            if token and not token.startswith("##"):
                probes.add(token)
    # Equivalently: the difference {u | derived admits u} minus
    # {u | base admits u} over this probe set must be empty — the probe
    # set is the union of both constraints' vocabularies, so probing one
    # canonical witness per class of namespace suffices.
    for uri in probes:
        if derived_spec.admits_namespace(uri, target_namespace) and not base_spec.admits_namespace(
            uri, target_namespace
        ):
            return False
    for token in base_spec.not_qname:
        if token in (DISALLOWED_DEFINED, DISALLOWED_SIBLING):
            if token not in derived_spec.not_qname:
                return False
            continue
        match = disallowed_name_parts(token)
        if match is None:
            # A raw token whose prefix could not be resolved (or a stray
            # keyword) cannot be compared; the declaration check reports
            # it and the derivation stays silent rather than raising.
            continue
        uri, local = match
        if not derived_spec.admits_namespace(uri, target_namespace):
            # The base's disallowed QName is outside the derived's
            # admitted namespaces, so the derived cannot admit it.
            continue
        covers_all = excludes_all_locals(derived_spec, uri)
        if local is None:
            # A base bare entry excludes every local in ``uri``; only a
            # derived bare entry covers it.
            if not covers_all:
                return False
        elif not covers_all and local not in excluded_locals(derived_spec, uri):
            # A derived bare entry (all locals) covers an exact base
            # name; an exact derived entry covers only its own name.
            return False
    return True


def derived_wildcard_edc_violations(
    base_model: Particle | None,
    derived_model: Particle | None,
    resolver: Resolver,
    global_lookup: Callable[[str], Any] | None = None,
) -> list[str]:
    """The XSD 1.1 tighter-EDC violations across a restriction.

    Content Type Restricts (Complex Content) clause 2 requires the base
    type's default binding for every instance element to subsume the
    derived type's binding (XSD 1.1 §3.4.6.4). When the derived model
    routes a name to a strict or lax wildcard, the binding resolves to
    the top-level element declaration of that expanded name; when the
    base model declares an element particle with the same name, the
    element is bound to that declaration there. The top-level
    declaration's type must then be validly substitutable as a
    restriction for the base particle's type, or the derivation is
    invalid (wild069 is the pinning case).

    Only names with no element particle of their own in the derived
    model are considered: an element particle takes precedence over a
    wildcard in the derived model just as in the base. Only wildcards
    that are members of an unordered (``all``) group are checked: in an
    order-sensitive model the element particle may be positionally
    unreachable (the corpus pins wild068's sequence shape valid while
    its ``all`` twin wild069 is invalid), and deciding reachability is
    beyond this static approximation. Skip wildcards are exempt
    (wild077/wild080, IBM s3_10_1v07), and declarations carrying
    ``xs:alternative`` type tables are out of scope (Area H). Every
    resolution failure skips the pair rather than reporting, and at
    most one violation is returned.
    """
    if base_model is None or derived_model is None or global_lookup is None:
        return []
    base_declarations: dict[tuple[str | None, str], list[Any]] = {}
    for particle in _walk_particles(base_model):
        if particle.kind != "element":
            continue
        declaration = _safe_resolve(resolver, particle)
        name = _expanded_name(declaration)
        if name is None:
            continue
        base_declarations.setdefault(name, []).append(declaration)
    derived_names: set[tuple[str | None, str]] = set()
    for particle in _walk_particles(derived_model):
        if particle.kind != "element":
            continue
        name = _expanded_name(_safe_resolve(resolver, particle))
        if name is not None:
            derived_names.add(name)
    for particle in _all_member_wildcards(derived_model):
        spec = particle.spec
        if spec is None or spec.process_contents == "skip":
            continue
        target = spec.effective_target(None)
        for name, declarations in base_declarations.items():
            if name in derived_names:
                continue
            global_name = _clark_name(name)
            if not spec.allows_name(global_name, target):
                continue
            global_declaration = global_lookup(global_name)
            if global_declaration is None or _has_type_alternatives(global_declaration):
                continue
            global_type = _type_of(global_declaration)
            if global_type is None:
                continue
            for declaration in declarations:
                if _has_type_alternatives(declaration):
                    continue
                local_type = _type_of(declaration)
                if local_type is None:
                    continue
                if _type_clause_violation(local_type, global_type) is None:
                    continue
                return [
                    "particle restriction (cos-element-consistent): the derived "
                    f"wildcard '{spec.namespace}' admits '{global_name}', which the "
                    f"base content model declares as element '{name[1]}'; the "
                    "top-level declaration the wildcard resolves to has a type that "
                    "is not a valid restriction of the base element's type"
                ]
    return []


def _walk_particles(particle: Particle):
    """Every particle in the tree, this one included."""
    yield particle
    for child in particle.children:
        yield from _walk_particles(child)


def _all_member_wildcards(particle: Particle, in_all: bool = False):
    """Every wildcard member of an ``all`` group, at any nesting depth."""
    if particle.kind == "any" and in_all:
        yield particle
        return
    child_in_all = in_all or particle.kind == "all"
    for child in particle.children:
        yield from _all_member_wildcards(child, child_in_all)


def _safe_resolve(resolver: Resolver, particle: Particle) -> Any:
    if resolver is None:
        return None
    try:
        return resolver(particle)
    except Exception:
        return None


def _clark_name(name: tuple[str | None, str]) -> str:
    namespace, local = name
    return f"{{{namespace}}}{local}" if namespace else local


def _has_type_alternatives(declaration: Any) -> bool:
    """Whether an element declaration carries ``xs:alternative`` children."""
    seen: set[int] = set()
    current = declaration
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        element = getattr(current, "xsdElement", None)
        if element is not None:
            for child in element:
                if local_name(child.tag) == "alternative":
                    return True
        current = getattr(current, "referredElement", None)
    return False


def is_valid_particle_restriction(
    base: Particle | None,
    derived: Particle | None,
    resolver: Resolver,
    head_lookup: HeadLookup | None = None,
    member_lookup: MemberLookup | None = None,
) -> list[str]:
    """The violation reasons for restricting ``base`` by ``derived``.

    Both arguments are compiled content-model trees (the base type's
    effective tree and the restricting type's own tree). ``resolver``
    maps an element particle to its declaration (or ``None``) and is
    used to check a restricting element against a wildcard base and
    for the ``NameAndTypeOK`` declaration clauses. ``head_lookup``
    optionally maps a declaration to the declaration of its
    substitution-group head (or ``None``); without it only exact
    expanded-name equality admits a pair. ``member_lookup`` maps a
    declaration to the declarations that substitute for it (its
    substitution-group members, direct members only); it drives the
    §3.9.6 clause 2.1 head expansion, which turns a substitution-head
    element particle into a choice group before the shape table decides
    (particlesZ027a's choice of members restricting its head). An empty
    result means the restriction is valid as far as the cells
    implemented here reach.
    """
    if base is None or derived is None:
        return []
    base = _eliminate_pointless(_unwrap_base(base))
    derived = _eliminate_pointless(_unwrap(derived))
    if member_lookup is not None:
        base = _expand_substitution_head(base, member_lookup)
        derived = _expand_substitution_head(derived, member_lookup)
    return _pair_violations(base, derived, resolver, set(), top=True, head_lookup=head_lookup)


def _expand_substitution_head(particle: Particle, member_lookup: MemberLookup) -> Particle:
    """The §3.9.6 clause 2.1 substitution-head expansion.

    A particle whose declaration is the substitution-group affiliation
    of one or more other declarations is treated as a choice group
    holding the declaration itself and one particle for each member,
    with the original particle's occurrence (the members are 1..1).
    """
    if particle.kind != "element" or particle.descriptor is None:
        return particle
    members = member_lookup(particle.descriptor)
    if not members:
        return particle
    branches = [particle]
    branches.extend(
        Particle("element", 1, 1, [], member.name, None, descriptor=member) for member in members
    )
    return Particle("choice", particle.min_occurs, particle.max_occurs, branches)


def _eliminate_pointless(particle: Particle) -> Particle:
    """The §3.9.6 clause 2.2 pointless-occurrence elimination.

    A ``1..1`` ``sequence``/``choice``/``all`` whose ``{particles}`` has a
    single member is pointless: it is ignored and replaced by that member
    (1..1 multiplication is the identity, so no occurrence folding is
    needed). The corpus's "Apply Pointless rules at top level" matrix
    (particlesHa121-Ha189) applies the rules to the derivation's own pair
    before the shape table, so this runs at the top of
    :func:`is_valid_particle_restriction`; the inner pairs keep the
    existing alignment treatment (``_unwrap``/``_unwrap_base``), which
    the corpus pins for composite members (groupB003v's group-vs-group
    alignment) and for the absorption reading (particlesHb008/Hb011).
    """
    while (
        particle.kind in ("sequence", "choice", "all")
        and particle.min_occurs == 1
        and particle.max_occurs == 1
        and len(particle.children) == 1
        and not (particle.children[0].kind == "sequence" and particle.children[0].synthetic)
        and not (particle.kind == "all" and particle.children[0].kind == "any")
    ):
        # A group reference's synthetic wrapper is folded by ``_unwrap``
        # when the pair is checked; folding the literal singleton around
        # it here would flatten the group before the alignment matches
        # whole groups (groupB003v, groupH021v). A singleton ``all``
        # holding a wildcard is left for the wildcard-over-all cell,
        # which decides it by coverage (wild080's posture) instead of
        # the bare-wildcard shortcut.
        particle = particle.children[0]
    return particle


def _pair_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    top: bool = False,
    amplified: bool = False,
    head_lookup: HeadLookup | None = None,
    removable: bool = False,
    check_occurs: bool = True,
) -> list[str]:
    """The violations for one aligned pair of particles, recursing into
    same-kind compositors. ``visited`` holds already-checked ``(base,
    derived)`` identity pairs so recursive content models terminate;
    ``top`` marks the derivation's own pair, the only level where the
    §3.9.6 common occurrence clause applies (inside compositors the
    compositor's own repetition multiplies the member ranges, so naive
    member containment would reject valid restrictions — the corpus
    pins such shapes legal in mgH014/W006). ``amplified`` records that
    some enclosing compositor repeats, silencing member-level
    occurrence containment everywhere below it. ``check_occurs``
    silences *all* per-pair occurrence containment — the unordered
    RecurseUnordered matcher passes ``False`` and accounts the mapped
    members' ranges by sum instead (all221/all230).
    """
    base = _unwrap_base(base)
    derived = _unwrap(derived)
    key = (id(base), id(derived))
    if key in visited:
        return []
    visited.add(key)
    rule = _SHAPE_RULES.get((derived.kind, base.kind))
    if rule is None:
        return [
            f"particle restriction: unknown shape — a {derived.kind} particle cannot "
            f"restrict a {base.kind} particle"
        ]
    if rule == "Forbidden":
        return [
            f"particle restriction (Forbidden): a {derived.kind} particle cannot "
            f"restrict a {base.kind} particle"
        ]
    if rule in ("WildcardOverAll", "ChoiceOverAll"):
        over_all = _over_all_violations(base, derived, resolver, visited, head_lookup)
        if over_all:
            return over_all
        # The 1.1 relaxation holds; the branch checks have verified the
        # members, so nothing more is reported.
        return []
    if rule in _DEFERRED_CELLS:
        # The cells whose partial approximations reject valid corpus
        # schemas are silent on inner pairs (details on the entry). On
        # the derivation's own pair the corpus pins the shape forbidden
        # (particlesHa121/Ha123/Ha166), so it is reported there.
        if top:
            return [
                f"particle restriction (Forbidden): a {derived.kind} particle cannot "
                f"restrict a {base.kind} particle"
            ]
        return []
    if rule == "EltOverGroup":
        return _elt_over_group_violations(base, derived, resolver, visited, head_lookup)
    if rule == "MapAndSum":
        return _mapandsum_violations(
            base,
            derived,
            resolver,
            visited,
            _member_amplified(base, derived, amplified),
            head_lookup,
            check_occurs,
        )
    if rule == "NSRecurseCheckCardinality":
        return _nscard_violations(base, derived, resolver, visited, head_lookup)
    violations: list[str] = []
    # The §3.9.6 common clause holds for the derivation pair itself.
    # Inside compositors it survives only where the corpus demands it:
    # an element over a wildcard (the Ja-Jq invalids), wildcard over
    # wildcard (the Oa cluster), and element pairs under compositors
    # whose identical ranges cannot rescue a member (the member range
    # is then the effective range). A compositor whose ranges differ
    # between base and derived multiplies member ranges unevenly, so
    # member-wise containment would reject valid schemas (mgH014/W006
    # posture). A member of a *choice* with maxOccurs=0 can never be
    # chosen, so it removes itself — a valid restriction of anything
    # (mgH014); the corpus keeps the same shape invalid on the
    # derivation's own pair and inside sequences (mgE006 posture).
    containment_applies = (
        top
        or rule in ("RecurseAsIfGroup", "NSSubset")
        or (rule == "NameAndTypeOK" and not amplified)
    )
    if (
        containment_applies
        and check_occurs
        and not (removable and not top and derived.max_occurs == 0)
        and not contains_occurs(
            derived.min_occurs, derived.max_occurs, base.min_occurs, base.max_occurs
        )
    ):
        violations.append(
            f"particle restriction ({rule}): occurrence range "
            f"{_occurrence_text(derived.min_occurs, derived.max_occurs)} is not contained "
            f"in {_occurrence_text(base.min_occurs, base.max_occurs)}"
        )
    if rule == "NSSubset":
        violations.extend(_nssubset_violations(base, derived))
    elif rule == "RecurseAsIfGroup":
        violations.extend(_wildcard_admission_violations(base, derived, resolver))
    elif rule == "NameAndTypeOK":
        violations.extend(_nameandtypeok_violations(base, derived, resolver, head_lookup))
    elif rule in ("Recurse", "RecurseUnordered"):
        violations.extend(
            _recurse_pairing_violations(
                base,
                derived,
                resolver,
                visited,
                amplified,
                head_lookup,
                rule,
                check_occurs,
            )
        )
    return violations


def _occurrence_multiplied(member: Particle, base: Particle) -> Particle:
    """A copy of ``member`` with the enclosing compositor's range folded in.

    A compositor matching ``base.min_occurs .. base.max_occurs`` times
    contributes each member over that whole repetition count: the
    member's effective range is the product of the two ranges. The
    member is returned unchanged when the product is its own range.
    """
    minimum = member.min_occurs * base.min_occurs
    if member.max_occurs is None or base.max_occurs is None:
        maximum = None
    else:
        maximum = member.max_occurs * base.max_occurs
    if minimum == member.min_occurs and maximum == member.max_occurs:
        return member
    return Particle(
        member.kind,
        minimum,
        maximum,
        member.children,
        member.name,
        member.spec,
        member.descriptor,
        member.synthetic,
    )


def _elt_over_group_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    head_lookup: HeadLookup | None,
) -> list[str]:
    """RecurseAsIfGroup: an element restricting a model group.

    The check is hybrid because the corpus pins different readings per
    base compositor. For a **choice** base the element is admitted when
    it validly restricts *one of* the choice's members with the
    choice's own occurrence multiplied into that member's range — the
    language-containment reading under which ``a(0,1)`` restricting
    ``choice(0,1)[a, b]`` is legal because the choice can match zero
    (particlesHa161, Z001 valid@1.1), while ``c1(1,1)`` under
    ``choice(2,3)[c1, c2]`` is not (particlesL001), and ``c1(3,3)``
    exceeds a ``c1(2,2)`` member (L004). A member that is itself a
    compositor is folded first (group references are synthetic
    wrappers), then recursed on with the product.

    A **sequence** or **all** base keeps the §3.9.6 singleton-wrapper
    reading instead: the element wrapped in a group of the base's
    variety at 1..1 must be a valid restriction of the base. That is
    what preserves the order and required-member structure of a
    sequence (particlesM001/M033/M034) and the per-member sums of an
    ``all`` (particlesK004/K006); member multiplication would accept
    M033/M034's invalid shapes. The member checks run unamplified so
    the member's own range is compared directly.
    """
    if base.kind == "choice":
        detail: list[str] = []
        for member in base.children:
            candidate = _occurrence_multiplied(_unwrap_base(member), base)
            trial = set(visited)
            if candidate.kind in ("sequence", "all", "choice"):
                reasons = _elt_over_group_violations(
                    candidate, derived, resolver, trial, head_lookup
                )
            else:
                reasons = _pair_violations(
                    candidate,
                    derived,
                    resolver,
                    trial,
                    amplified=False,
                    head_lookup=head_lookup,
                    check_occurs=True,
                )
            if not reasons:
                return []
            if not detail:
                detail.extend(reasons)
        return [
            f"particle restriction (EltOverGroup): element '{derived.name}' validly "
            f"restricts no member of the base choice with the choice's occurrence "
            f"{_occurrence_text(base.min_occurs, base.max_occurs)} folded in "
            f"({'; '.join(detail)})"
        ]
    if not contains_occurs(1, 1, base.min_occurs, base.max_occurs):
        return [
            f"particle restriction (EltOverGroup): the element's singleton "
            f"{base.kind} wrapper has occurrence range [1,1], which is not contained "
            f"in the base {_occurrence_text(base.min_occurs, base.max_occurs)}"
        ]
    wrapper = Particle(base.kind, 1, 1, [derived])
    if base.kind == "sequence":
        return _sequence_alignment_violations(
            base, wrapper, resolver, visited, False, head_lookup, True
        )
    return _unordered_violations(
        base, wrapper, resolver, visited, False, head_lookup, "EltOverGroup", True
    )


def _maps_onto_a_base_member(
    base: Particle,
    member: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    amplified: bool,
    head_lookup: HeadLookup | None,
    check_occurs: bool,
) -> bool:
    """Whether a derived member validly restricts some base member."""
    for candidate in base.children:
        trial = set(visited)
        reasons = _pair_violations(
            candidate,
            member,
            resolver,
            trial,
            amplified=amplified,
            head_lookup=head_lookup,
            removable=True,
            check_occurs=check_occurs,
        )
        if not reasons:
            return True
    return False


def _mapandsum_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    amplified: bool,
    head_lookup: HeadLookup | None,
    check_occurs: bool,
) -> list[str]:
    """MapAndSum: a sequence restricting a choice group.

    Every member of the derived sequence must validly restrict *some*
    member of the base choice — the mapping is neither injective nor
    order-preserving (particlesV015's ``seq(e3, e2, e1)`` over
    ``choice(e1|e2|e3)``) — and the pair of the sequence's own
    occurrence range times its member count (MapAndSum's second
    clause, not the §3.8.6 effective-total-range rule, which applies
    to compositors in general) must be contained in the base choice's
    range (particlesV001 valid, V002/V003/V005).
    """
    violations: list[str] = []
    count = len(derived.children)
    minimum = derived.min_occurs * count
    maximum = None if derived.max_occurs is None else derived.max_occurs * count
    if not contains_occurs(minimum, maximum, base.min_occurs, base.max_occurs):
        violations.append(
            f"particle restriction (MapAndSum): the derived sequence's effective total "
            f"range {_occurrence_text(minimum, maximum)} (its own "
            f"{_occurrence_text(derived.min_occurs, derived.max_occurs)} times {count} "
            f"members) is not contained in the base choice's "
            f"{_occurrence_text(base.min_occurs, base.max_occurs)}"
        )
    for index, member in enumerate(derived.children):
        if _maps_onto_a_base_member(
            base, member, resolver, visited, amplified, head_lookup, check_occurs
        ):
            continue
        label = f"'{member.name}' " if member.kind == "element" else ""
        violations.append(
            f"particle restriction (MapAndSum): derived sequence member {index} "
            f"{label}validly restricts no member of the base choice"
        )
    return violations


def _nscard_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    head_lookup: HeadLookup | None,
) -> list[str]:
    """NSRecurseCheckCardinality: a group restricting a wildcard.

    Every member of the derived group must validly restrict the
    wildcard as defined by Particle Valid (Restriction) — namespace
    admission for elements, recursion for nested groups — with the
    per-member occurrence clause suspended because the aggregate clause
    accounts for it (particlesQ013's members are 2..2 under a 4..8
    wildcard). The group's effective total range must then be contained
    in the wildcard's range (particlesQ006/Q013, R010/R014). A
    ``maxOccurs=0`` group matches nothing, so nothing is checked (the
    W006 posture).
    """
    if derived.max_occurs == 0:
        return []
    violations: list[str] = []
    minimum, maximum = _effective_total_range(derived)
    if not contains_occurs(minimum, maximum, base.min_occurs, base.max_occurs):
        violations.append(
            f"particle restriction (NSRecurseCheckCardinality): the derived "
            f"{derived.kind}'s effective total range {_occurrence_text(minimum, maximum)} "
            f"is not contained in the base wildcard's "
            f"{_occurrence_text(base.min_occurs, base.max_occurs)}"
        )
    for member in derived.children:
        reasons = _pair_violations(
            base,
            member,
            resolver,
            set(visited),
            amplified=True,
            head_lookup=head_lookup,
            check_occurs=False,
        )
        if reasons:
            label = f"'{member.name}' " if member.kind == "element" else ""
            violations.append(
                f"particle restriction (NSRecurseCheckCardinality): derived member "
                f"{label}does not validly restrict the base wildcard "
                f"({'; '.join(reasons)})"
            )
    return violations


def _effective_total_range(particle: Particle) -> tuple[int, int | None]:
    """The §3.8.6 effective total range of a compositor particle.

    A sequence or ``all`` adds its members' contributions, a choice
    takes the narrowest minimum and the widest maximum; the particle's
    own occurrence multiplies the aggregate. ``None`` means unbounded,
    which any unbounded member forces (and which the particle's own
    unbounded range forces whenever the aggregate can be non-zero).
    """
    if particle.kind not in ("sequence", "all", "choice"):
        return (particle.min_occurs, particle.max_occurs)
    if not particle.children:
        return (0, 0)
    ranges = [_effective_total_range(child) for child in particle.children]
    maxima = [item[1] for item in ranges]
    numeric_maxima: list[int] = [value for value in maxima if value is not None]
    inner_max: int | None
    if len(numeric_maxima) != len(maxima):
        inner_max = None
    elif particle.kind == "choice":
        inner_max = max(numeric_maxima)
    else:
        inner_max = sum(numeric_maxima)
    if particle.kind == "choice":
        inner_min = min(item[0] for item in ranges)
    else:
        inner_min = sum(item[0] for item in ranges)
    minimum = particle.min_occurs * inner_min
    if inner_max is None:
        maximum = None
    elif particle.max_occurs is None:
        maximum = None if inner_max > 0 else 0
    else:
        maximum = particle.max_occurs * inner_max
    return (minimum, maximum)


def _over_all_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    head_lookup: HeadLookup | None,
) -> list[str]:
    """The wildcard-over-all / choice-over-all transition.

    XSD 1.0 forbids the shape outright; the corpus keeps it forbidden
    when the derived side escapes the all's language (particlesHb002's
    ``choice(any ##any)`` reaches namespaces no member of its element
    base admits), and accepts it under the 1.1 profile when it stays
    inside: a bare wildcard is covered when the all has no required
    members and one of its wildcards admits everything the derived
    wildcard admits (all218), and a choice restricts the all when
    *every* branch restricts the all on its own (RecurseUnordered per
    branch — all231/all232 valid, all233's over-wide third branch and
    particlesHb009's required-member-dropping branches invalid).
    """
    forbidden = [
        f"particle restriction (Forbidden): a {derived.kind} particle cannot "
        f"restrict an all particle"
    ]
    required = [child for child in base.children if child.min_occurs >= 1]
    if derived.kind == "any":
        # A bare wildcard is covered only when the all has no required
        # members and one of its wildcards admits everything the
        # derived wildcard admits (all218's sequence over an all of
        # wildcards unwraps to exactly this shape).
        if not required and _wildcard_covered(derived, base):
            return []
        return forbidden
    if derived.kind != "choice":
        return forbidden
    for index, branch in enumerate(derived.children):
        reasons = _unordered_violations(
            base,
            branch,
            resolver,
            set(visited),
            True,
            head_lookup,
            "Recurse",
            False,
            branch=index,
        )
        if reasons:
            return reasons
    return []


def _wildcard_covered(wildcard: Particle, base: Particle) -> bool:
    """Whether some base wildcard admits everything this wildcard does."""
    if wildcard.spec is None:
        return False
    return any(
        other.kind == "any"
        and other.spec is not None
        and wildcard_subset(wildcard.spec, other.spec)
        for other in base.children
    )


def _wildcard_particles(particle: Particle) -> list[Particle]:
    """Every wildcard particle in the tree, this one included."""
    if particle.kind == "any":
        return [particle]
    found: list[Particle] = []
    for child in particle.children:
        found.extend(_wildcard_particles(child))
    return found


def _unwrap(particle: Particle) -> Particle:
    """Sees through the compiler's synthetic group-reference wrappers.

    A group reference is not a particle: its content model is the
    referenced compositor, repeated at the reference site's occurrence.
    The compiler models it as a ``synthetic`` singleton ``sequence``
    wrapper, so the wrapper is replaced by its content with the two
    occurrence ranges folded together (a ``minOccurs=0`` reference to a
    choice yields an optional choice, not a sequence). Literal
    sequences — however few members they hold — are real particles per
    §3.9.6 and keep their identity on the *derived* side
    (particlesL010's nested sequence, groupB003v's group-vs-group
    alignment); the base side also sees through ``1..1`` single-member
    literals (``_unwrap_base``).
    """
    while True:
        if particle.kind == "sequence" and particle.synthetic and len(particle.children) == 1:
            particle = _fold_synthetic(particle)
            continue
        if (
            particle.kind == "sequence"
            and len(particle.children) == 1
            and particle.min_occurs == 1
            and particle.max_occurs == 1
            and particle.children[0].kind not in ("sequence", "choice", "all")
        ):
            # A 1..1 singleton sequence holding a leaf is the pointless
            # case §3.9.6 eliminates before the table; composite
            # children keep the sequence's identity so the alignment can
            # match whole groups (groupB003v).
            particle = particle.children[0]
            continue
        return particle


def _fold_synthetic(particle: Particle) -> Particle:
    """Replaces a synthetic group-ref wrapper with its content.

    The reference site's occurrence folds into the content's own range
    (a ``minOccurs=0`` reference to a choice yields an optional choice).
    """
    inner = particle.children[0]
    if particle.max_occurs is None or inner.max_occurs is None:
        maximum = None
    else:
        maximum = particle.max_occurs * inner.max_occurs
    return Particle(
        inner.kind,
        particle.min_occurs * inner.min_occurs,
        maximum,
        inner.children,
        inner.name,
        inner.spec,
        inner.descriptor,
    )


def _unwrap_base(particle: Particle) -> Particle:
    """The base-side view: synthetic wrappers plus literal singleton groups.

    On the base side the corpus treats a ``1..1`` single-member
    sequence as transparent as well — an ``all`` restricting
    ``sequence[any]`` sees the wildcard itself (particlesHa070/Ha080,
    P002), and a vacuous member is not required to consume the
    sequence's supply (particlesJd010). ``1..1`` multiplication is the
    identity, so no occurrence folding is needed for those literals.
    """
    while True:
        inner = _unwrap(particle)
        if inner is not particle:
            particle = inner
            continue
        if (
            particle.kind == "sequence"
            and len(particle.children) == 1
            and particle.min_occurs == 1
            and particle.max_occurs == 1
        ):
            particle = particle.children[0]
            continue
        return particle


def _nssubset_violations(base: Particle, derived: Particle) -> list[str]:
    """The NSSubset conditions beyond occurrence containment.

    Occurrence containment for the pair is applied by the caller's
    generic clause (gated by ``check_occurs`` there): the
    RecurseUnordered matcher accounts mapped ranges by sum instead.
    """
    base_spec = base.spec
    derived_spec = derived.spec
    if base_spec is None or derived_spec is None:
        return []
    violations: list[str] = []
    if not wildcard_subset(derived_spec, base_spec):
        violations.append(
            f"particle restriction (NSSubset): derived wildcard namespace constraint "
            f"'{derived_spec.namespace}' is not a subset of base '{base_spec.namespace}'"
        )
    derived_severity = _PROCESS_SEVERITY.get(derived_spec.process_contents, 2)
    base_severity = _PROCESS_SEVERITY.get(base_spec.process_contents, 2)
    if derived_severity < base_severity:
        violations.append(
            f"particle restriction (NSSubset): derived wildcard processContents "
            f"'{derived_spec.process_contents}' is weaker than base "
            f"'{base_spec.process_contents}'"
        )
    return violations


def _wildcard_admission_violations(
    base: Particle, derived: Particle, resolver: Resolver
) -> list[str]:
    """RecurseAsIfGroup's base case: an element restricting a wildcard
    must be admitted by it. The element is admitted when its instance
    namespace lies in the wildcard's constraint; an unresolvable
    declaration gets the benefit of the doubt. A vacuous element
    (``maxOccurs=0``) can never occur, so the namespace constraint
    cannot exclude it (particlesJq010).
    """
    if base.kind != "any" or base.spec is None:
        return []
    if derived.max_occurs == 0:
        return []
    declaration = resolver(derived) if resolver is not None else None
    if declaration is None or not callable(getattr(declaration, "getNamespace", None)):
        return []
    uri = element_namespace(declaration)
    if base.spec.allows(uri, base.spec.effective_target(None)):
        return []
    return [
        f"particle restriction (RecurseAsIfGroup): element '{derived.name}' namespace "
        f"{uri!r} is not admitted by the base wildcard '{base.spec.namespace}'"
    ]


def _nameandtypeok_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    head_lookup: HeadLookup | None,
) -> list[str]:
    """NameAndTypeOK's declaration clauses for an element pair.

    Occurrence containment is handled by the caller. Every clause
    compares the two particles' *declarations*; when either cannot be
    resolved the clause — and with it the whole check — is skipped
    (never an error). The corpus pins element substitution groups
    (particlesZ008), restriction-derived type replacement (particlesIj005)
    and dropped ``nillable``/``fixed``/``block`` restrictions as legal.
    """
    base_decl = resolver(base) if resolver is not None else None
    derived_decl = resolver(derived) if resolver is not None else None
    if base_decl is None or derived_decl is None:
        return []
    violations: list[str] = []
    if not _same_or_substitution_member(base_decl, derived_decl, head_lookup):
        violations.append(
            f"particle restriction (NameAndTypeOK): element "
            f"{_name_text(derived_decl)} does not match base element "
            f"{_name_text(base_decl)} (neither the expanded names nor the "
            f"substitution-group heads agree)"
        )
        return violations
    base_cls = _type_of(base_decl)
    derived_cls = _type_of(derived_decl)
    type_reason = _type_clause_violation(base_cls, derived_cls)
    if type_reason is not None:
        violations.append(type_reason)
    base_fixed = _call_or_none(base_decl, "getFixed")
    if base_fixed is not None:
        derived_fixed = _call_or_none(derived_decl, "getFixed")
        if derived_fixed is None or not _fixed_values_agree(
            base_fixed, base_cls, derived_fixed, derived_cls
        ):
            violations.append(
                f"particle restriction (NameAndTypeOK): the base element's fixed value "
                f"{base_fixed!r} must be preserved; the derived element carries "
                + ("no fixed value" if derived_fixed is None else repr(derived_fixed))
            )
    if bool(_call_or_none(derived_decl, "isNillable")) and not _call_or_none(
        base_decl, "isNillable"
    ):
        violations.append(
            "particle restriction (NameAndTypeOK): the derived element is nillable "
            "but the base element is not"
        )
    base_blocked = _disallowed_substitutions(base_decl)
    derived_blocked = _disallowed_substitutions(derived_decl)
    if not base_blocked <= derived_blocked:
        violations.append(
            f"particle restriction (NameAndTypeOK): the base element's disallowed "
            f"substitutions {_sorted_tokens(base_blocked)} must be a subset of the "
            f"derived element's {_sorted_tokens(derived_blocked)}"
        )
    if not _identity_signatures(base_decl) <= _identity_signatures(derived_decl):
        violations.append(
            "particle restriction (NameAndTypeOK): the base element's identity "
            "constraints must be carried by the derived element"
        )
    return violations


def _same_or_substitution_member(
    base_decl: Any, derived_decl: Any, head_lookup: HeadLookup | None
) -> bool:
    """Whether the derived declaration may stand where the base's is.

    Either the expanded names agree, or the derived declaration is a
    (transitive) member of the base declaration's substitution group.
    """
    base_name = _expanded_name(base_decl)
    if base_name == _expanded_name(derived_decl):
        return True
    if head_lookup is None or base_name is None:
        return False
    seen = {id(derived_decl)}
    current = derived_decl
    for _ in range(16):
        # Cycles are reported by the substitution-group build; the depth
        # cap only keeps this walk finite in spite of them.
        current = head_lookup(current)
        if current is None or id(current) in seen:
            return False
        seen.add(id(current))
        if _expanded_name(current) == base_name:
            return True
    return False


def _type_clause_violation(base_cls: Any, derived_cls: Any) -> str | None:
    """The type-subsumption clause.

    The derived declaration's type must be the base declaration's type
    or validly derived from it; a restriction may not go through an
    ``extension`` step (particlesIj008), so ``extension`` is the blocked
    derivation method. Unresolvable types skip the clause. A ur-type
    base (``xs:anyType``/its stand-in, ``xs:anySimpleType`` for simple
    content) admits every type of the matching category
    (particlesIj001/Ik014/stZ067), and a union base admits a derived
    type validly derived from any of its (transitive) member types
    (particlesaddB150/saxonSimple010).
    """
    if base_cls is None or derived_cls is None:
        return None
    if _ur_type_admits(base_cls, derived_cls):
        return None
    reason = is_validly_derived(derived_cls, base_cls, frozenset({"extension"}))
    if reason is None:
        return None
    if reason == "not-derived" and derived_from_union_member(derived_cls, base_cls):
        return None
    if reason == "blocked":
        return (
            "particle restriction (NameAndTypeOK): the derived element's type "
            f"{_class_name(derived_cls)} is derived from the base element's type "
            f"{_class_name(base_cls)} through an extension step, which a "
            "restriction may not use"
        )
    return (
        "particle restriction (NameAndTypeOK): the derived element's type "
        f"{_class_name(derived_cls)} is not the base element's type "
        f"{_class_name(base_cls)} and is not validly derived from it"
    )


def _ur_type_admits(base_cls: type, derived_cls: type) -> bool:
    """Whether a ur-type base admits the derived type outright.

    ``xs:anyType`` (and the ``SchemaBase`` stand-in for absent types)
    admits everything; ``xs:anySimpleType`` admits every simple type.
    """
    from pyxsd.schema_base import SchemaBase
    from pyxsd.xsd_data_types import AnySimpleType, AnyType

    if base_cls is SchemaBase or base_cls is AnyType:
        return True
    if base_cls is AnySimpleType:
        kind = getattr(derived_cls, "_contentKind_", None)
        return kind != "complex"
    return False


def _fixed_values_agree(
    base_fixed: Any, base_cls: Any, derived_fixed: Any, derived_cls: Any
) -> bool:
    """Whether the base and derived fixed values agree in the value
    space.

    Lexical spellings may differ legally (whitespace-collapsed tokens,
    list separators, ``-1`` vs ``       -1`` — particlesaddB183), so
    when both declared types resolve to simple-type classes the values
    are compared as typed values; otherwise the comparison falls back
    to the lexical form.
    """
    if str(base_fixed) == str(derived_fixed):
        return True
    if not (isinstance(base_cls, type) and isinstance(derived_cls, type)):
        return False
    from pyxsd.xsd_data_types import XsdDataType, xsd_value_key

    if not (issubclass(base_cls, XsdDataType) and issubclass(derived_cls, XsdDataType)):
        return False

    def typed_value(cls, lexical):
        try:
            return cls(lexical)
        except TypeError:
            # Union-style classes validate through ``__new__``; their
            # ``__init__`` (inherited from SchemaBase) takes no value.
            return cls.__new__(cls, lexical)

    try:
        return xsd_value_key(typed_value(derived_cls, derived_fixed)) == xsd_value_key(
            typed_value(base_cls, base_fixed)
        )
    except Exception:
        return False


def _expanded_name(declaration: Any) -> tuple[str | None, str] | None:
    """The declaration's ``(namespace, local name)``."""
    if declaration is None:
        return None
    referred = getattr(declaration, "referredElement", None)
    if referred is not None and referred is not declaration:
        return _expanded_name(referred)
    local = getattr(declaration, "name", None)
    if not local:
        return None
    return (element_namespace(declaration), str(local))


def _disallowed_substitutions(declaration: Any) -> frozenset[str]:
    """The declaration's ``block`` tokens, ``#all`` expanded."""
    raw = _call_or_none(declaration, "getBlock")
    tokens = {token for token in str(raw or "").split() if token}
    if "#all" in tokens:
        return frozenset({"substitution", "extension", "restriction"})
    return frozenset(tokens)


def _identity_signatures(declaration: Any) -> frozenset[tuple[Any, ...]]:
    """Comparable signatures of the declaration's identity constraints."""
    identities = getattr(declaration, "identities", None) or []
    signatures: set[tuple[Any, ...]] = set()
    for constraint in identities:
        signatures.add(
            (
                type(constraint).__name__,
                getattr(constraint, "constraintName", None),
                getattr(constraint, "selector", None),
                tuple(getattr(constraint, "fieldPaths", None) or ()),
            )
        )
    return frozenset(signatures)


def _type_of(declaration: Any) -> type | None:
    """The declaration's resolved type class (``None`` when unresolved)."""
    getter = getattr(declaration, "getType", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _call_or_none(declaration: Any, method_name: str) -> Any:
    getter = getattr(declaration, method_name, None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _name_text(declaration: Any) -> str:
    name = _expanded_name(declaration)
    if name is None:
        return "(unresolved)"
    namespace, local = name
    return f"'{local}'" + (f" in {namespace!r}" if namespace else "")


def _class_name(cls: Any) -> str:
    return str(getattr(cls, "name", None) or getattr(cls, "__name__", cls))


def _sorted_tokens(tokens: frozenset[str]) -> str:
    return "{" + ", ".join(sorted(tokens)) + "}" if tokens else "{}"


#: Backtracking node budget for the unordered matcher; past it the pair
#: is left unverified rather than rejected (skip-not-reject posture —
#: content models of this size do not occur in the corpus).
_UNORDERED_NODE_BUDGET = 20000


def _recurse_pairing_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    amplified: bool = False,
    head_lookup: HeadLookup | None = None,
    rule_name: str = "Recurse",
    check_occurs: bool = True,
) -> list[str]:
    """Recurse's pairing over same-kind compositors (and over ``all``).

    Sequences align order-preservingly — each derived member restricts
    a base member at its in-order position, and base members left out
    must be able to match zero instances (particlesW008 valid, W010
    invalid). Choices map each derived member onto a *distinct* base
    member it validly restricts — order-insensitive and injective
    (particlesT002/T005 valid, T008 invalid, and a derived branch may
    not consume another's base branch). An ``all`` base — with an
    ``all`` or a sequence derived from it (RecurseUnordered) — maps
    order-insensitively with per-base-member occurrence sums (all201,
    particlesU003).
    """
    if base.kind == "all":
        return _unordered_violations(
            base, derived, resolver, visited, amplified, head_lookup, rule_name, check_occurs
        )
    member_amplified = _member_amplified(base, derived, amplified)
    if derived.kind == "choice" and base.kind == "choice":
        return _choice_mapping_violations(
            base, derived, resolver, visited, member_amplified, head_lookup, check_occurs
        )
    if derived.kind == "sequence" and base.kind == "sequence":
        return _sequence_alignment_violations(
            base, derived, resolver, visited, member_amplified, head_lookup, check_occurs
        )
    # Remaining shapes (an all or other compositor derived over a
    # sequence/choice base) are forbidden transitions handled by the
    # shape table before this dispatch, or deferred cells.
    return []


def _member_amplified(base: Particle, derived: Particle, amplified: bool) -> bool:
    """Whether this compositor's members cannot be occurrence-compared.

    Members inherit ``amplified`` once this compositor's ranges differ
    between base and derived (or an ancestor's did): the multipliers
    then differ and member ranges cannot be compared pairwise. A
    vacuous compositor (maxOccurs=0) matches nothing, so its members'
    ranges are unconstrained (particlesW006).
    """
    return (
        amplified
        or (base.min_occurs, base.max_occurs) != (derived.min_occurs, derived.max_occurs)
        or base.max_occurs == 0
        or derived.max_occurs == 0
    )


def _choice_branches(choice: Particle) -> list[Particle]:
    """The effective alternative list of a choice group.

    A group reference compiles to a synthetic singleton sequence
    wrapper; once unwrapped, a nested ``choice`` is itself an
    alternative union, so its branches join this choice's branch list
    (§3.9.6's pointless-choice elimination, particlesIb006/Ib007). A
    nested choice carrying its own repetition is a real particle and
    stays a single branch. Singleton wrappers are seen through here on
    both sides (``_unwrap_base``): the branch list is the group's
    language, not its spelled structure.
    """
    branches: list[Particle] = []
    for child in choice.children:
        inner = _unwrap_base(child)
        if inner.kind == "choice" and inner.min_occurs == 1 and inner.max_occurs == 1:
            branches.extend(_choice_branches(inner))
        else:
            branches.append(inner)
    return branches


def _choice_mapping_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    amplified: bool,
    head_lookup: HeadLookup | None,
    check_occurs: bool,
) -> list[str]:
    """RecurseLax: an injective, order-insensitive member mapping.

    Every derived branch must validly restrict some base branch, and
    no two derived branches may consume the same base branch
    (RecurseLax's bijective mapping). Backtracking tries each unused
    base branch per derived branch, so a branch that only fits a base
    branch another one needs forces a different overall mapping.
    Group-ref choices among the derived members contribute their own
    branches to this choice's alternatives (``_choice_branches``).
    """
    used: set[int] = set()
    dead_ends: list[tuple[int, list[str]]] = []
    derived_children = _choice_branches(derived)
    base_children = _choice_branches(base)

    def assign(index: int) -> bool:
        if index == len(derived_children):
            return True
        member = derived_children[index]
        for position, candidate in enumerate(base_children):
            if position in used:
                # Injectivity: no two derived branches may consume the
                # same base branch.
                continue
            trial = set(visited)
            reasons = _pair_violations(
                candidate,
                member,
                resolver,
                trial,
                amplified=amplified,
                head_lookup=head_lookup,
                removable=True,
                check_occurs=check_occurs,
            )
            if not reasons:
                used.add(position)
                if assign(index + 1):
                    return True
                used.discard(position)
            elif not any(failed[0] == index for failed in dead_ends):
                dead_ends.append((index, reasons))
        return False

    if assign(0):
        return []
    if dead_ends:
        index, reasons = min(dead_ends, key=lambda failed: failed[0])
        detail = "; ".join(reasons)
    else:
        index, detail = 0, "the base choice has no members"
    return [
        f"particle restriction (Recurse): derived choice member {index} validly "
        f"restricts no unused member of the base choice ({detail})"
    ]


def _particle_emptiable(particle: Particle) -> bool:
    """§3.9.6 Particle Emptiable: whether the particle can match zero.

    A particle is emptiable when its own ``minOccurs`` is 0 or — for a
    group — when its effective total range's minimum is 0. A
    ``sequence[e(0,1)]`` member is therefore emptiable even though the
    member particle's own ``minOccurs`` is 1 (addB091/Ha002).
    """
    if particle.min_occurs == 0:
        return True
    if particle.kind in ("sequence", "all", "choice"):
        minimum, _maximum = _effective_total_range(particle)
        return minimum == 0
    return False


def _sequence_alignment_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    amplified: bool,
    head_lookup: HeadLookup | None,
    check_occurs: bool,
) -> list[str]:
    """Recurse over sequence:sequence — the order-preserving alignment.

    Each derived member must restrict some base member at or after the
    previous alignment, and every base member the alignment steps over
    must be able to match zero instances (an optional member may be
    dropped — particlesW008 — a required one may not — particlesW010).
    A base member the enclosing compositor can supply more than once
    may serve several consecutive derived members — the 1.1 absorption
    reading, which lets ``seq{a, b}`` restrict a repeated choice of
    the substitution-group heads (particlesZ028) — but the number of
    derived members mapped onto one base member is capped by that
    member's occurrence supply (the product of the compositor's and
    the member's maximum occurrences): the supply bounds how many
    copies exist, and every mapping of a derived member onto the base
    member — the plain advance as much as a reuse — consumes one of
    them. Reordering is never allowed (particlesW007/W013), nor is an
    extra derived member (particlesW012).
    """
    base_children = base.children
    derived_children = derived.children
    memo: dict[tuple[int, int, int], bool] = {}
    dead_ends: list[tuple[int, int, list[str]]] = []

    def supply(position: int) -> int | None:
        """How many copies base_children[position] can contribute.

        The compositor repeats its whole content, so the member's
        effective supply is the product of the two maximums; ``None``
        means unbounded — including the vacuous case: a compositor or
        member with ``maxOccurs=0`` matches nothing, so it has no
        meaningful copy count and its members stay unconstrained (the
        corpus pins such shapes legal in particlesW006/particlesJd005).
        """
        member = base_children[position]
        if base.max_occurs is None or member.max_occurs is None:
            return None
        product = base.max_occurs * member.max_occurs
        return None if product == 0 else product

    def walk(index: int, position: int, used: int) -> bool:
        """Whether derived_children[index:] aligns from position onward.

        ``used`` counts the derived members already served by
        base_children[position]; the alignment is order-preserving, so
        every use of one base member is consecutive on the derived
        side. Mapping derived_children[index] onto the member is use
        number ``used + 1`` and requires ``used < supply`` — that guard
        covers the plain advance exactly as much as a reuse. An
        unbounded supply makes the counter irrelevant, so it is
        normalized out of the memo key.
        """
        exhausted = position >= len(base_children)
        available = None if exhausted else supply(position)
        key = (index, position, 0 if exhausted or available is None else used)
        if key in memo:
            return memo[key]
        if index == len(derived_children):
            # An empty tail matches zero members, so a derived sequence
            # that stopped exactly at the end is aligned when every base
            # member left over is emptiable in the §3.9.6 sense.
            ok = all(_particle_emptiable(member) for member in base_children[position:])
            memo[key] = ok
            return ok
        if exhausted:
            memo[key] = False
            return False
        member = derived_children[index]
        if member.max_occurs == 0:
            # A member that can never match removes itself: it neither
            # consumes a base copy nor constrains the alignment
            # (particlesJd010; the mgH014 posture).
            ok = walk(index + 1, position, used)
            memo[key] = ok
            return ok
        trial = set(visited)
        reasons = _pair_violations(
            base_children[position],
            member,
            resolver,
            trial,
            amplified=amplified,
            head_lookup=head_lookup,
            removable=False,
            check_occurs=check_occurs,
        )
        if reasons:
            dead_ends.append((index, position, reasons))
        elif (available is None or used < available) and (
            # Mapping this derived member onto the base member consumes
            # copy number used + 1 of the member's supply; the guard
            # therefore gates the advance branch (which starts a fresh
            # alignment after the map) and the reuse branch (which
            # leaves the member available for the next derived member)
            # alike.
            walk(index + 1, position + 1, 0) or walk(index + 1, position, used + 1)
        ):
            memo[key] = True
            return True
        if _particle_emptiable(base_children[position]) and walk(index, position + 1, 0):
            memo[key] = True
            return True
        memo[key] = False
        return False

    if walk(0, 0, 0):
        return []
    if dead_ends:
        index, _position, reasons = min(dead_ends, key=lambda end: (end[0], end[1]))
        detail = "; ".join(reasons)
        return [
            f"particle restriction (Recurse): derived sequence member {index} "
            f"validly restricts no in-order member of the base sequence ({detail})"
        ]
    return [
        "particle restriction (Recurse): the derived sequence does not align with "
        "the base sequence — it has a member the base sequence's order cannot place"
    ]


def _unordered_units(particle: Particle) -> list[Particle]:
    """The derived-side units the unordered matcher aligns.

    A sequence over an ``all`` contributes one unit per member; a
    choice member contributes its *branches* — each alternative must
    restrict the all on its own, since an instance uses exactly one
    (all234). Anything else is a single unit.
    """
    if particle.kind in ("sequence", "all"):
        units: list[Particle] = []
        for child in particle.children:
            if child.kind == "choice":
                units.extend(child.children)
            else:
                units.append(child)
        return units
    if particle.kind == "choice":
        return list(particle.children)
    return [particle]


def _unordered_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    amplified: bool,
    head_lookup: HeadLookup | None,
    rule_name: str,
    check_occurs: bool,
    branch: int | None = None,
) -> list[str]:
    """RecurseUnordered: order-insensitive matching over an ``all`` base.

    Each derived unit must validly restrict some base member, and every
    required base member must be restricted by at least one unit — an
    optional (minOccurs=0) base member may be dropped (all204 vs
    all201). Units may share a base member; the shared member's range
    must then contain the *sum* of the units' ranges (all221 valid,
    all223/all224 invalid). Where, and only where, a unit restricts no
    single base member but is a wildcard, it may be *split* across the
    base wildcards its namespace constraint overlaps — provided every
    namespace it admits is admitted by some base wildcard, no overlapped
    base wildcard's processContents is weakened, and the split cannot
    leave a required base member's minimum unreachable (all237 valid;
    all244 invalid because the split can starve the base's minimum).
    """
    where = f"choice branch {branch} " if branch is not None else ""
    prefix = f"particle restriction ({rule_name}): {where}"
    units = _unordered_units(derived)
    base_members = base.children
    candidates: list[list[int]] = []
    for unit in units:
        fits: list[int] = []
        for position, candidate in enumerate(base_members):
            trial = set(visited)
            reasons = _pair_violations(
                candidate,
                unit,
                resolver,
                trial,
                amplified=True,
                head_lookup=head_lookup,
                removable=False,
                check_occurs=False,
            )
            if not reasons:
                fits.append(position)
        candidates.append(fits)
    split_capable = {
        index
        for index, unit in enumerate(units)
        if not candidates[index] and unit.kind == "any" and _wildcard_split_coverable(unit, base)
    }
    budget = _UNORDERED_NODE_BUDGET
    first_accounting: list[str] = []

    def accounting(assignment: list[int | None]) -> list[str]:
        minima: dict[int, int] = {}
        maxima: dict[int, int | None] = {}
        for index, position in enumerate(assignment):
            if position is None:
                # A split wildcard's occurrences may land in any
                # overlapped base wildcard's namespaces, so it adds
                # minimum pressure only where it is fully contained.
                unit = units[index]
                for member_position, member in enumerate(base_members):
                    if _wildcard_contained_in(unit, member):
                        minima[member_position] = minima.get(member_position, 0) + unit.min_occurs
                continue
            unit = units[index]
            minima[position] = minima.get(position, 0) + unit.min_occurs
            current = maxima.get(position, 0)
            maxima[position] = (
                None if current is None or unit.max_occurs is None else current + unit.max_occurs
            )
        for position, total_min in minima.items():
            member = base_members[position]
            if not contains_occurs(
                total_min, maxima.get(position), member.min_occurs, member.max_occurs
            ):
                reasons = [
                    prefix + f"the derived members restricting base member '{member.name}' have "
                    f"combined occurrence range "
                    f"{_occurrence_text(total_min, maxima.get(position))} "
                    f"which is not contained in {_occurrence_text(member.min_occurs, member.max_occurs)}"
                ]
                if not first_accounting:
                    first_accounting.extend(reasons)
                return reasons
        for position, member in enumerate(base_members):
            if member.min_occurs >= 1 and position not in minima and position not in maxima:
                reasons = [
                    prefix + f"base member '{member.name}' requires at least {member.min_occurs} "
                    "occurrence(s) but no derived member restricts it"
                ]
                if not first_accounting:
                    first_accounting.extend(reasons)
                return reasons
        return []

    def assign(index: int, assignment: list[int | None]) -> list[str] | None:
        nonlocal budget
        if index == len(units):
            return accounting(assignment)
        for position in candidates[index]:
            budget -= 1
            if budget < 0:
                # Out of budget: leave the rest unverified (skip, not
                # reject).
                return None
            assignment[index] = position
            reasons = assign(index + 1, assignment)
            if not reasons:
                return None
        if index in split_capable:
            assignment[index] = None
            return assign(index + 1, assignment)
        assignment[index] = None
        if first_accounting:
            return first_accounting
        unit = units[index]
        detail = "; ".join(
            _unordered_probe_reasons(units[index], base_members, resolver, visited, head_lookup)
        )
        label = f"'{unit.name}' " if unit.kind == "element" else ""
        return [
            prefix + f"derived member {index} {label}validly restricts no member of the base all "
            f"({detail})"
        ]

    result = assign(0, [None] * len(units))
    return result or []


def _unordered_probe_reasons(
    unit: Particle,
    base_members: list[Particle],
    resolver: Resolver,
    visited: set[tuple[int, int]],
    head_lookup: HeadLookup | None,
) -> list[str]:
    """Why one derived unit fits no base member (for the message)."""
    for candidate in base_members:
        trial = set(visited)
        reasons = _pair_violations(
            candidate,
            unit,
            resolver,
            trial,
            amplified=True,
            head_lookup=head_lookup,
            removable=False,
            check_occurs=False,
        )
        if reasons:
            return reasons
    return ["the base all has no members"]


def _wildcard_probes(specs: list[WildcardSpec], target: str | None) -> set[str | None]:
    """The canonical probe set over which wildcard coverage is decided."""
    probes: set[str | None] = {None, target, _FOREIGN_PROBE_URI}
    for spec in specs:
        probes.add(spec.effective_target(target))
        for token in (*spec.namespace.split(), *spec.not_namespace):
            if token and not token.startswith("##"):
                probes.add(token)
        for token in spec.not_qname:
            match = disallowed_name_parts(token)
            if match is None:
                continue
            uri, _local = match
            if uri is not None:
                probes.add(uri)
    return probes


def _wildcard_split_coverable(unit: Particle, base: Particle) -> bool:
    """Whether a derived wildcard may be spread over the base wildcards.

    Every namespace the unit admits must be admitted by some base
    wildcard, and no overlapped base wildcard's processContents may be
    weakened (all238). The coverage is name-level (XSD 1.1 §3.10.6.2):
    the name space of a namespace the unit admits is covered when no
    name is excluded by every overlapped base wildcard but admitted by
    the unit (wild048/wild051 reject; wild049/wild050 cover). Without
    this reading the pathologically overlapping all237 could not
    restrict its base at all.
    """
    spec = unit.spec
    if spec is None:
        return False
    target = spec.effective_target(None)
    overlapped = [
        member.spec
        for member in base.children
        if member.kind == "any"
        and member.spec is not None
        and _wildcards_overlap(spec, member.spec, target)
    ]
    if not overlapped:
        return False
    severity = _PROCESS_SEVERITY.get(spec.process_contents, 2)
    for other in overlapped:
        if severity < _PROCESS_SEVERITY.get(other.process_contents, 2):
            return False
    for uri in _wildcard_probes([spec, *overlapped], target):
        if not spec.admits_namespace(uri, target):
            continue
        if not _namespace_names_covered(spec, overlapped, uri, target):
            return False
    return True


def _namespace_names_covered(
    spec: WildcardSpec,
    candidates: list[WildcardSpec],
    uri: str | None,
    target: str | None,
) -> bool:
    """Whether the candidates jointly admit every name the spec admits in ``uri``.

    A name is covered when at least one candidate admits it. Equivalently
    the uncovered names are the intersection of the candidates'
    exclusions: a name admitted by the spec but excluded by *every*
    candidate is uncovered. The finite exact exclusions are intersected;
    the ``##defined``/``##definedSibling`` keywords are name sets the
    schema phase cannot enumerate, so only the structural case is
    decided — when every candidate excludes the keyword's set, the spec
    must exclude it too.
    """
    if excludes_all_locals(spec, uri):
        return True
    admitting = [candidate for candidate in candidates if candidate.admits_namespace(uri, target)]
    if not admitting:
        return False
    finite: set[str] | None = None
    every_defined = True
    every_sibling = True
    for candidate in admitting:
        every_defined = every_defined and DISALLOWED_DEFINED in candidate.not_qname
        every_sibling = every_sibling and DISALLOWED_SIBLING in candidate.not_qname
        if excludes_all_locals(candidate, uri):
            # The candidate admits no name in ``uri``; it contributes
            # nothing to the intersection.
            continue
        names = set(excluded_locals(candidate, uri))
        finite = names if finite is None else (finite & names)
    if finite is None:
        # Every admitting candidate excludes every local in ``uri`` (a
        # bare ``{uri}`` entry): the intersection of their exclusions is
        # the whole name space, so the spec's names there are uncovered
        # unless it too excludes everything (handled above).
        return False
    uncovered = frozenset(finite)
    if uncovered and not uncovered <= excluded_locals(spec, uri):
        return False
    if every_defined and DISALLOWED_DEFINED not in spec.not_qname:
        return False
    return not (every_sibling and DISALLOWED_SIBLING not in spec.not_qname)


def _wildcards_overlap(
    first: WildcardSpec, second: WildcardSpec, target: str | None = None
) -> bool:
    """Whether two wildcard constraints admit a common namespace."""
    if target is None:
        target = first.effective_target(None)
    return any(
        first.admits_namespace(uri, target) and second.admits_namespace(uri, target)
        for uri in _wildcard_probes([first, second], target)
    )


def _wildcard_contained_in(unit: Particle, member: Particle) -> bool:
    """Whether the unit's whole constraint sits in one base member."""
    spec = unit.spec
    other = member.spec
    if spec is None or other is None:
        return False
    target = spec.effective_target(None)
    return wildcard_subset(spec, other, target)


def element_namespace(declaration: Any) -> str | None:
    """The {target namespace} an element declaration is checked against.

    Reference sites take the referred declaration's namespace. A
    global declaration carries its schema's target namespace; a local
    declaration carries it only when qualified — its own ``form``
    attribute, the source document's ``elementFormDefault`` (recorded
    at composition time for spliced components), or the host schema's
    default. An unqualified local element therefore has the absent
    namespace, which is a *different* expanded name from a same-named
    global (particlesL010/L030). An explicit XSD 1.1
    ``targetNamespace`` attribute on the declaration wins (the TargetNS
    corpus cases). Declarations without scope information (test
    stand-ins) keep the historical form-blind reading.
    """
    if declaration is None:
        return None
    referred = getattr(declaration, "referredElement", None)
    if referred is not None and referred is not declaration:
        return element_namespace(referred)
    explicit = getattr(getattr(declaration, "xsdElement", None), "get", None)
    if callable(explicit):
        target = explicit("targetNamespace")
        if target:
            return target
    getter = getattr(declaration, "getNamespace", None)
    if not callable(getter):
        return None
    if not _local_declaration_is_qualified(declaration):
        return None
    return getter()


def _local_declaration_is_qualified(declaration: Any) -> bool:
    """Whether a declaration's expanded name carries its schema namespace.

    A global declaration always does. A local one depends on its own
    ``form`` attribute, then the form defaults of the document the
    component was spliced from, then the host schema's
    ``elementFormDefault``. Declarations without scope information
    (test stand-ins) are treated as global.
    """
    is_global = getattr(declaration, "isGlobalDeclaration", None)
    if not callable(is_global):
        return True
    if is_global():
        return True
    element = getattr(declaration, "xsdElement", None)
    explicit = element.get("form") if element is not None else None
    if explicit is not None:
        return explicit == "qualified"
    try:
        schema = declaration.getSchema()
    except AttributeError:
        return True
    if schema is None:
        return True
    source_defaults = getattr(schema, "formDefaultOverrides", None)
    if source_defaults and element is not None and id(element) in source_defaults:
        element_default, _attribute_default = source_defaults[id(element)]
        return (element_default or "unqualified") == "qualified"
    default_getter = getattr(schema, "getElementFormDefault", None)
    if not callable(default_getter):
        return True
    return default_getter() == "qualified"


def _occurrence_text(minimum: int, maximum: int | None) -> str:
    """Renders an occurrence range the way the schema documents do."""
    if maximum is None:
        return f"[{minimum},unbounded]"
    return f"[{minimum},{maximum}]"
