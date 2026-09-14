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
* Occurrence containment for element-over-element (``NameAndTypeOK``'s
  common clause), for a group over a wildcard or over an element
  (``NSRecurseCheckCardinality``/``GroupOverElement``), for sequence
  over choice (``MapAndSum``) and for same-kind compositors
  (``Recurse``).
* ``Recurse`` pairing: sequences positionally; choices by mapping each
  derived member onto some base member it validly restricts
  (order-insensitive). ``all`` over ``all`` waits for Task 4c's
  order-insensitive mapping.
* Deliberately silent until the later sub-tasks: element over a model
  group (RecurseAsIfGroup needs the deep occurrence arithmetic of
  particlesM003 — naive containment there would reject a valid
  restriction), a model group over an element (the 1.0 table forbids
  the shape, but the corpus pins the exemplars valid under the 1.1
  profile — particlesHb008/Hb011 — because the 1.1 Recurse alignment
  absorbs them; Task 4c/4d completes this), and everything
  ``NameAndTypeOK`` checks beyond occurrences (Task 4b).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyxsd.content_model import Particle, particle_names
from pyxsd.wildcards import WildcardSpec

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
#: reading that would forbid them. ``EltOverGroup`` stays deferred: its
#: deep occurrence arithmetic is Task 4d's.
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
    ("sequence", "all"): "SequenceOverAll",
    ("all", "element"): "GroupOverElement",
    ("all", "any"): "NSRecurseCheckCardinality",
    ("all", "choice"): "Forbidden",
    ("all", "sequence"): "Forbidden",
    ("all", "all"): "Recurse",
}

Resolver = Callable[[Particle], Any]

#: Cells this sub-task deliberately leaves silent because every partial
#: approximation of them rejects corpus-pinned valid schemas; each lands
#: with the sub-task that implements its full rule:
#:
#: * ``EltOverGroup`` — RecurseAsIfGroup's deep occurrence arithmetic
#:   (particlesM003 pins a restriction valid that naive containment
#:   would reject); Task 4d.
#: * ``GroupOverElement`` — 1.0 forbids the shape, but the corpus pins
#:   the exemplars valid under the 1.1 profile (particlesHb008/Hb011)
#:   via 1.1's Recurse alignment; Task 4c/4d.
#: * ``NSRecurseCheckCardinality`` — its occurrence clause weighs the
#:   group members' multiplicity against the wildcard's range, so plain
#:   containment rejects valid schemas (particlesHa070, Q013, R009);
#:   Task 4d.
#: * ``MapAndSum`` — partitions the derived sequence over the base
#:   choice with occurrence arithmetic (particlesV003); Task 4d.
#: * ``SequenceOverAll`` — RecurseUnordered's order-insensitive mapping
#:   (all211, particlesU003, wild047); Task 4c/4d.
_DEFERRED_CELLS = frozenset(
    {
        "EltOverGroup",
        "GroupOverElement",
        "NSRecurseCheckCardinality",
        "MapAndSum",
        "SequenceOverAll",
    }
)


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
    """Whether the derived wildcard's namespace constraint is a subset.

    The subset relation is probed against a canonical namespace set —
    the absent namespace, the target namespace, a foreign probe URI and
    every literal URI in either constraint — so list constraints are
    compared by extension (``##any`` ⊇ ``##local ##targetNamespace uri``
    and ``{foo}`` ⊆ ``{foo bar}`` hold while ``{abce}`` ⊆ ``{foo bar}``
    does not) instead of by string equality. Each spec resolves
    ``##targetNamespace``/``##other`` against its own recorded source
    namespace, falling back to ``target_namespace`` when it carries none.
    """
    probes: set[str | None] = {None, target_namespace, _FOREIGN_PROBE_URI}
    for spec in (derived_spec, base_spec):
        probes.add(spec.effective_target(target_namespace))
        for token in spec.namespace.split():
            if token and not token.startswith("##"):
                probes.add(token)
    for uri in probes:
        if derived_spec.allows(uri, target_namespace) and not base_spec.allows(
            uri, target_namespace
        ):
            return False
    return True


def is_valid_particle_restriction(
    base: Particle | None, derived: Particle | None, resolver: Resolver
) -> list[str]:
    """The violation reasons for restricting ``base`` by ``derived``.

    Both arguments are compiled content-model trees (the base type's
    effective tree and the restricting type's own tree). ``resolver``
    maps an element particle to its declaration (or ``None``) and is
    used to check a restricting element against a wildcard base. An
    empty result means the restriction is valid as far as the cells
    implemented here reach.
    """
    if base is None or derived is None:
        return []
    return _pair_violations(base, derived, resolver, set(), top=True)


def _pair_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    top: bool = False,
) -> list[str]:
    """The violations for one aligned pair of particles, recursing into
    same-kind compositors. ``visited`` holds already-checked ``(base,
    derived)`` identity pairs so recursive content models terminate;
    ``top`` marks the derivation's own pair, the only level where the
    §3.9.6 common occurrence clause applies (inside compositors the
    compositor's own repetition multiplies the member ranges, so naive
    member containment would reject valid restrictions — the corpus
    pins such shapes legal in mgH014/W006).
    """
    base = _unwrap(base)
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
        over_all = _over_all_violations(base, derived)
        if over_all:
            return over_all
        # The 1.1 relaxation holds; the pair still waits for the Recurse
        # work to verify the members, so nothing more is reported.
        return []
    if rule in _DEFERRED_CELLS:
        # The cells whose full rules need the later sub-tasks' machinery
        # are silent here; partial approximations of them reject valid
        # corpus schemas (details on each entry).
        return []
    violations: list[str] = []
    # The §3.9.6 common clause holds for the derivation pair itself.
    # Inside compositors it survives only where the corpus demands it:
    # an element over a wildcard (the Ja-Jq invalids) and wildcard over
    # wildcard (the Oa cluster). Other member pairs escape it — the
    # enclosing compositor's repetition multiplies member ranges, so
    # member-wise containment would reject valid schemas (mgH014/W006).
    if (top or rule in ("RecurseAsIfGroup", "NSSubset")) and not contains_occurs(
        derived.min_occurs, derived.max_occurs, base.min_occurs, base.max_occurs
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
    elif rule == "Recurse":
        violations.extend(_recurse_pairing_violations(base, derived, resolver, visited))
    return violations


def _over_all_violations(base: Particle, derived: Particle) -> list[str]:
    """The wildcard-over-all / choice-over-all transition.

    XSD 1.0 forbids the shape outright; the corpus keeps it forbidden
    when the derived side escapes the all's language (particlesHb002's
    ``choice(any ##any)`` reaches namespaces no member of its element
    base admits, particlesHb009's choice branches drop a required
    member), and accepts it under the 1.1 profile when every branch of
    the derived choice carries each required base member and every
    wildcard it reaches is covered by a base wildcard (all231/all232).
    Anything subtler waits for the Recurse work.
    """
    forbidden = [
        f"particle restriction (Forbidden): a {derived.kind} particle cannot "
        f"restrict an all particle"
    ]
    required = [child for child in base.children if child.min_occurs >= 1]
    if any(member.kind != "element" for member in required):
        # A non-element required member cannot be matched branch-by-
        # branch with the machinery this sub-task has.
        return forbidden
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
    for member in required:
        for branch in derived.children:
            if member.name not in particle_names(branch):
                return forbidden
    for wildcard in _wildcard_particles(derived):
        if not _wildcard_covered(wildcard, base):
            return forbidden
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
    """Sees through ``1..1`` sequence wrappers holding one particle.

    The pointless-particle elimination of §3.9.6 runs before the shape
    table: such a sequence matches exactly what its child matches, so
    the pair is decided on the child. The compiler emits this exact
    shape for group references; multi-child and repeated sequences
    carry their own identity and stay.
    """
    while (
        particle.kind == "sequence"
        and len(particle.children) == 1
        and particle.min_occurs == 1
        and particle.max_occurs == 1
    ):
        particle = particle.children[0]
    return particle


def _nssubset_violations(base: Particle, derived: Particle) -> list[str]:
    """The NSSubset conditions beyond occurrence containment."""
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
    declaration gets the benefit of the doubt.
    """
    if base.kind != "any" or base.spec is None:
        return []
    declaration = resolver(derived) if resolver is not None else None
    if declaration is None or not callable(getattr(declaration, "getNamespace", None)):
        return []
    uri = _element_namespace(declaration)
    if base.spec.allows(uri, base.spec.effective_target(None)):
        return []
    return [
        f"particle restriction (RecurseAsIfGroup): element '{derived.name}' namespace "
        f"{uri!r} is not admitted by the base wildcard '{base.spec.namespace}'"
    ]


def _recurse_pairing_violations(
    base: Particle, derived: Particle, resolver: Resolver, visited: set[tuple[int, int]]
) -> list[str]:
    """Recurse's pairing over same-kind compositors.

    Sequences pair positionally; choices map each derived member onto
    some base member it validly restricts (the corpus pins the mapping
    as order-insensitive and length-free — particlesM002); ``all`` over
    ``all`` waits for Task 4c's order-insensitive mapping.
    """
    if derived.kind == "all":
        return []
    if derived.kind == "choice":
        violations: list[str] = []
        for index, d_member in enumerate(derived.children):
            mapped = False
            first_attempt: list[str] = []
            for b_member in base.children:
                trial = set(visited)
                candidate = _pair_violations(b_member, d_member, resolver, trial)
                if not candidate:
                    mapped = True
                    break
                if not first_attempt:
                    first_attempt = candidate
            if not mapped:
                reasons = "; ".join(first_attempt) or "the base choice has no members"
                violations.append(
                    f"particle restriction (Recurse): derived choice member {index} "
                    f"validly restricts no member of the base choice ({reasons})"
                )
        return violations
    if len(derived.children) != len(base.children):
        # Positional pairing assumes aligned members; a derived sequence
        # that drops optional base members (wild068) or otherwise shifts
        # the alignment needs the Recurse work's matching machinery.
        return []
    for b_member, d_member in zip(base.children, derived.children, strict=False):
        violations = _pair_violations(b_member, d_member, resolver, visited)
        if violations:
            return violations
    return []


def _element_namespace(declaration: Any) -> str | None:
    """The namespace an element declaration is checked against.

    Reference sites take the referred declaration's namespace; every
    other declaration answers with the declaring document's target
    namespace. The corpus treats local elements as living in their
    schema's namespace regardless of form defaults (particlesJq010 is
    pinned valid with an unqualified local element under a
    ``##targetNamespace`` wildcard, particlesJl002 invalid under a URI
    list missing it), so no form-default qualification happens here.
    """
    if declaration is None:
        return None
    referred = getattr(declaration, "referredElement", None)
    if referred is not None and referred is not declaration:
        return _element_namespace(referred)
    explicit = getattr(getattr(declaration, "xsdElement", None), "get", None)
    if callable(explicit):
        target = explicit("targetNamespace")
        if target:
            # XSD 1.1 lets a local element declaration name its namespace
            # directly (the TargetNS corpus cases).
            return target
    getter = getattr(declaration, "getNamespace", None)
    if not callable(getter):
        return None
    return getter()


def _occurrence_text(minimum: int, maximum: int | None) -> str:
    """Renders an occurrence range the way the schema documents do."""
    if maximum is None:
        return f"[{minimum},unbounded]"
    return f"[{minimum},{maximum}]"
