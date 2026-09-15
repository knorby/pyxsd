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
  absorbs them; Task 4c/4d completes this).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyxsd.content_model import Particle, particle_names
from pyxsd.derivation import is_validly_derived
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
HeadLookup = Callable[[Any], Any]

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
    # Equivalently: the difference {u | derived admits u} minus
    # {u | base admits u} over this probe set must be empty — the probe
    # set is the union of both constraints' vocabularies, so probing one
    # canonical witness per class of namespace suffices.
    for uri in probes:
        if derived_spec.allows(uri, target_namespace) and not base_spec.allows(
            uri, target_namespace
        ):
            return False
    return True


def is_valid_particle_restriction(
    base: Particle | None,
    derived: Particle | None,
    resolver: Resolver,
    head_lookup: HeadLookup | None = None,
) -> list[str]:
    """The violation reasons for restricting ``base`` by ``derived``.

    Both arguments are compiled content-model trees (the base type's
    effective tree and the restricting type's own tree). ``resolver``
    maps an element particle to its declaration (or ``None``) and is
    used to check a restricting element against a wildcard base and
    for the ``NameAndTypeOK`` declaration clauses. ``head_lookup``
    optionally maps a declaration to the declaration of its
    substitution-group head (or ``None``); without it only exact
    expanded-name equality admits a pair. An empty result means the
    restriction is valid as far as the cells implemented here reach.
    """
    if base is None or derived is None:
        return []
    return _pair_violations(base, derived, resolver, set(), top=True, head_lookup=head_lookup)


def _pair_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    top: bool = False,
    amplified: bool = False,
    head_lookup: HeadLookup | None = None,
    removable: bool = False,
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
    occurrence containment everywhere below it.
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
    elif rule == "Recurse":
        violations.extend(
            _recurse_pairing_violations(base, derived, resolver, visited, amplified, head_lookup)
        )
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
    if reason == "not-derived" and _derived_from_union_member(derived_cls, base_cls):
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


def _derived_from_union_member(derived_cls: type, base_cls: type) -> bool:
    """Whether the derived type is validly derived from a (transitive)
    member type of a union base (Type Derivation OK (Simple), union
    clause).

    A restricting type may also restrict a *member that is itself a
    union* (saxonSimple012: ``sub-chap`` restricts ``dt``, a member of
    ``chap``): such an ancestor is a subtype of the base union exactly
    when its own (flattened) members are all members of the base.
    """
    base_members = getattr(base_cls, "_unionMembers", None)
    if not base_members:
        return False
    for ancestor in derived_cls.__mro__:
        if any(isinstance(b, type) and issubclass(ancestor, b) for b in base_members):
            return True
        ancestor_members = getattr(ancestor, "_unionMembers", None)
        if ancestor_members and all(
            any(isinstance(b, type) and issubclass(member, b) for b in base_members)
            for member in ancestor_members
        ):
            return True
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
    return (_element_namespace(declaration), str(local))


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


def _recurse_pairing_violations(
    base: Particle,
    derived: Particle,
    resolver: Resolver,
    visited: set[tuple[int, int]],
    amplified: bool = False,
    head_lookup: HeadLookup | None = None,
    removable: bool = False,
) -> list[str]:
    """Recurse's pairing over same-kind compositors.

    Sequences pair positionally; choices map each derived member onto
    some base member it validly restricts (the corpus pins the mapping
    as order-insensitive and length-free — particlesM002); ``all`` over
    ``all`` waits for Task 4c's order-insensitive mapping.

    Members inherit ``amplified`` once this compositor's ranges differ
    between base and derived (or an ancestor's did): the multipliers
    then differ and member ranges cannot be compared pairwise.
    """
    if derived.kind == "all":
        return []
    member_amplified = (
        amplified
        or (base.min_occurs, base.max_occurs) != (derived.min_occurs, derived.max_occurs)
        # A vacuous compositor (maxOccurs=0) matches nothing, so its
        # members' ranges are unconstrained (particlesW006).
        or base.max_occurs == 0
        or derived.max_occurs == 0
    )
    if derived.kind == "choice":
        violations: list[str] = []
        for index, d_member in enumerate(derived.children):
            mapped = False
            first_attempt: list[str] = []
            for b_member in base.children:
                trial = set(visited)
                candidate = _pair_violations(
                    b_member,
                    d_member,
                    resolver,
                    trial,
                    amplified=member_amplified,
                    head_lookup=head_lookup,
                    removable=True,
                )
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
        violations = _pair_violations(
            b_member,
            d_member,
            resolver,
            visited,
            amplified=member_amplified,
            head_lookup=head_lookup,
            removable=False,
        )
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
