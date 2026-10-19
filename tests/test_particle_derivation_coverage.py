"""Coverage-rounding tests for ``pyxsd.particle_derivation``.

The public predicate is exercised by ``tests/test_particle_derivation.py``;
this module pins the internal helper contracts the full suite does not
reach: declaration-resolution fallbacks, the EDC walk, the effective
total range, the wildcard probe/coverage helpers, the form-aware
namespace resolution and the alignment budgets. Stand-ins mirror the
style of the existing particle-derivation tests.
"""

import xml.etree.ElementTree as ET

import pyxsd.particle_derivation as pd
from pyxsd.content_model import Particle
from pyxsd.particle_derivation import (
    _call_or_none,
    _effective_total_range,
    _expanded_name,
    _fixed_values_agree,
    _has_type_alternatives,
    _local_declaration_is_qualified,
    _name_text,
    _namespace_names_covered,
    _nssubset_violations,
    _over_all_violations,
    _pair_violations,
    _safe_resolve,
    _same_or_substitution_member,
    _type_clause_violation,
    _type_of,
    _unordered_units,
    _unordered_violations,
    _ur_type_admits,
    _walk_particles,
    _wildcard_admission_violations,
    _wildcard_covered,
    _wildcard_particles,
    _wildcard_probes,
    _wildcard_split_coverable,
    _wildcards_overlap,
    contains_occurs,
    derived_wildcard_edc_violations,
    element_namespace,
    is_valid_particle_restriction,
    wildcard_subset,
)
from pyxsd.schema_base import SchemaBase
from pyxsd.wildcards import DISALLOWED_DEFINED, WildcardSpec
from pyxsd.xsd_data_types import AnySimpleType, AnyType, String

XSD_NS = "http://www.w3.org/2001/XMLSchema"
ANY = WildcardSpec(namespace="##any")
LOCAL = WildcardSpec(namespace="##local")


def no_resolver(_particle):
    return None


class _Decl:
    """A stand-in element declaration for the helper-level tests."""

    def __init__(self, name=None, namespace=None, type_=None, alternatives=False, block=None):
        self.name = name
        self._namespace = namespace
        self._type = type_
        self._block = block
        self.referredElement = None
        self.identities = []
        self.xsdElement = _alternative_root() if alternatives else None

    def getNamespace(self):
        return self._namespace

    def getType(self):
        return self._type

    def getBlock(self):
        return self._block


class _TypedDecl(_Decl):
    def isGlobalDeclaration(self):
        return True


def _alternative_root():
    root = ET.Element(f"{{{XSD_NS}}}complexType")
    ET.SubElement(root, f"{{{XSD_NS}}}alternative")
    return root


def _elt(name, decl, min_occurs=1, max_occurs=1):
    return Particle(
        "element",
        min_occurs=min_occurs,
        max_occurs=max_occurs,
        name=name,
        descriptor=decl,
    )


class _Restricted:
    _derivation_ = "restriction"


class _Unrelated:
    pass


class _RaisingType:
    def getType(self):
        raise ValueError("no type")


class _RaisingBlock:
    def getBlock(self):
        raise ValueError("no block")


def _decl_resolver(particle):
    return particle.descriptor


def _edc_base(decl):
    return Particle("sequence", children=[_elt("e", decl)])


def _edc_derived(spec):
    return Particle("all", children=[Particle("any", spec=spec)])


class TestEdcWalk:
    """``derived_wildcard_edc_violations``'s guard and resolution paths."""

    def test_missing_inputs_returns_empty(self):
        derived = _edc_derived(ANY)
        assert derived_wildcard_edc_violations(None, derived, _decl_resolver, lambda _n: None) == []
        assert (
            derived_wildcard_edc_violations(_edc_base(_Decl("e")), None, _decl_resolver, None) == []
        )
        # ``global_lookup`` absent is also a skip.
        assert derived_wildcard_edc_violations(_edc_base(_Decl("e")), derived, _decl_resolver) == []

    def test_unnamed_base_declaration_is_skipped(self):
        base = _edc_base(_Decl(name=None))
        assert (
            derived_wildcard_edc_violations(base, _edc_derived(ANY), _decl_resolver, _lookup) == []
        )

    def test_unnamed_derived_declaration_is_skipped(self):
        derived = Particle(
            "all",
            children=[_elt("d", _Decl(name=None)), Particle("any", spec=ANY)],
        )
        # The unnamed derived element contributes no name; the wildcard's
        # global lookup misses, so nothing is reported.
        assert (
            derived_wildcard_edc_violations(_edc_base(_Decl("e")), derived, _decl_resolver, _lookup)
            == []
        )

    def test_wildcard_excluding_the_name_is_skipped(self):
        base = _edc_base(_Decl(name="e", namespace="urn:e"))
        derived = _edc_derived(WildcardSpec(namespace="##local"))
        assert derived_wildcard_edc_violations(base, derived, _decl_resolver, _lookup) == []

    def test_global_declaration_with_alternatives_is_skipped(self):
        base = _edc_base(_Decl(name="e", namespace="urn:e"))
        derived = _edc_derived(ANY)
        assert (
            derived_wildcard_edc_violations(
                base, derived, _decl_resolver, lambda _n: _Decl("g", alternatives=True)
            )
            == []
        )

    def test_global_declaration_without_type_is_skipped(self):
        base = _edc_base(_Decl(name="e", namespace="urn:e"))
        derived = _edc_derived(ANY)
        assert (
            derived_wildcard_edc_violations(base, derived, _decl_resolver, lambda _n: _Decl("g"))
            == []
        )

    def test_local_declaration_with_alternatives_is_skipped(self):
        base = _edc_base(_Decl(name="e", namespace="urn:e", alternatives=True))
        derived = _edc_derived(ANY)
        assert (
            derived_wildcard_edc_violations(
                base, derived, _decl_resolver, lambda _n: _Decl("g", type_=_Unrelated)
            )
            == []
        )

    def test_local_declaration_without_type_is_skipped(self):
        base = _edc_base(_Decl(name="e", namespace="urn:e", type_=None))
        derived = _edc_derived(ANY)
        assert (
            derived_wildcard_edc_violations(
                base, derived, _decl_resolver, lambda _n: _Decl("g", type_=_Unrelated)
            )
            == []
        )

    def test_type_conflict_is_reported(self):
        base = _edc_base(_Decl(name="e", namespace="urn:e", type_=_Restricted))
        derived = _edc_derived(ANY)
        reasons = derived_wildcard_edc_violations(
            base, derived, _decl_resolver, lambda _n: _Decl("g", type_=_Unrelated)
        )
        assert reasons and "cos-element-consistent" in reasons[0]


def _lookup(_name):
    return None


class TestResolutionHelpers:
    """The small resolution/formatting helpers' fallback branches."""

    def test_safe_resolve_without_resolver(self):
        assert _safe_resolve(None, Particle("element", name="e")) is None

    def test_safe_resolve_swallows_resolver_errors(self):
        def boom(_particle):
            raise RuntimeError("resolver failed")

        assert _safe_resolve(boom, Particle("element", name="e")) is None

    def test_walk_particles_yields_every_particle(self):
        tree = Particle(
            "sequence",
            children=[Particle("element", name="a"), Particle("element", name="b")],
        )
        assert [particle.name for particle in _walk_particles(tree)] == [None, "a", "b"]

    def test_has_type_alternatives_true_and_false(self):
        assert _has_type_alternatives(_Decl("g", alternatives=True)) is True
        assert _has_type_alternatives(_Decl("g")) is False

    def test_has_type_alternatives_follows_referred_element(self):
        referred = _Decl("g", alternatives=True)
        declaration = _Decl("h")
        declaration.referredElement = referred
        assert _has_type_alternatives(declaration) is True

    def test_expanded_name_none(self):
        assert _expanded_name(None) is None

    def test_expanded_name_follows_referred_element(self):
        referred = _Decl("e", namespace="urn:e")
        declaration = _Decl("other")
        declaration.referredElement = referred
        assert _expanded_name(declaration) == ("urn:e", "e")

    def test_expanded_name_without_local_part(self):
        assert _expanded_name(_Decl(name="")) is None
        assert _expanded_name(_Decl(name=None)) is None

    def test_type_of_swallows_gettype_errors(self):
        assert _type_of(_RaisingType()) is None
        assert _type_of(object()) is None

    def test_call_or_none_for_missing_and_raising_getters(self):
        assert _call_or_none(object(), "getFixed") is None
        assert _call_or_none(_RaisingBlock(), "getBlock") is None

    def test_wildcard_subset_skips_unresolvable_base_token(self):
        derived = WildcardSpec(namespace="##any")
        base = WildcardSpec(namespace="##any", not_qname=frozenset({"prefix:x"}))
        assert wildcard_subset(derived, base) is True

    def test_name_text_of_unresolved_declaration(self):
        assert _name_text(_RaisingBlock()) == "(unresolved)"


class TestOccurrenceAndRange:
    """``contains_occurs`` and ``_effective_total_range``."""

    def test_effective_range_of_a_leaf(self):
        assert _effective_total_range(Particle("element", 2, 3)) == (2, 3)

    def test_effective_range_propagates_unbounded_member(self):
        tree = Particle("sequence", children=[Particle("element", 1, None)])
        assert _effective_total_range(tree) == (1, None)

    def test_effective_range_vacuous_compositor(self):
        assert _effective_total_range(Particle("sequence", children=[])) == (0, 0)

    def test_effective_range_unbounded_compositor_with_content(self):
        tree = Particle("sequence", 1, None, [Particle("element", 1, 2)])
        assert _effective_total_range(tree) == (1, None)

    def test_effective_range_choice_takes_widest_maximum(self):
        tree = Particle(
            "choice",
            children=[Particle("element", 1, 2), Particle("element", 2, 5)],
        )
        assert _effective_total_range(tree) == (1, 5)

    def test_contains_occurs_boundaries(self):
        assert contains_occurs(1, 1, 1, 1)
        assert not contains_occurs(0, 1, 1, 1)
        assert contains_occurs(5, None, 0, None)


class TestOverAllTransition:
    """The wildcard-over-all / choice-over-all cell."""

    def test_covered_wildcard_over_optional_all_is_valid(self):
        base = Particle("all", children=[Particle("any", min_occurs=0, spec=ANY)])
        derived = Particle("any", spec=LOCAL)
        assert is_valid_particle_restriction(base, derived, no_resolver) == []

    def test_wildcard_covered_rejects_missing_spec(self):
        base = Particle("all", children=[Particle("any", spec=ANY)])
        assert _wildcard_covered(Particle("any", spec=None), base) is False

    def test_over_all_of_non_wildcard_is_forbidden(self):
        base = Particle("all", children=[Particle("element", name="e", min_occurs=0)])
        derived = Particle("any", spec=ANY)
        reasons = _over_all_violations(base, derived, no_resolver, set(), None)
        assert reasons and "Forbidden" in reasons[0]

    def test_wildcard_particles_collects_nested_wildcards(self):
        tree = Particle(
            "sequence",
            children=[
                Particle("element", name="e"),
                Particle("choice", children=[Particle("any", spec=ANY)]),
            ],
        )
        assert _wildcard_particles(tree) == [tree.children[1].children[0]]

    def test_wildcard_particles_of_a_wildcard(self):
        wildcard = Particle("any", spec=ANY)
        assert _wildcard_particles(wildcard) == [wildcard]


class TestNullResolutions:
    """The predicates that give up when a pair cannot be resolved."""

    def test_is_valid_particle_restriction_with_none_pair(self):
        element = Particle("element", name="e")
        assert is_valid_particle_restriction(None, element, no_resolver) == []
        assert is_valid_particle_restriction(element, None, no_resolver) == []

    def test_nssubset_without_specs(self):
        base = Particle("any", spec=None)
        derived = Particle("any", spec=None)
        assert _nssubset_violations(base, derived) == []

    def test_wildcard_admission_for_non_wildcard_base(self):
        base = Particle("element", name="e")
        derived = Particle("element", name="e")
        assert _wildcard_admission_violations(base, derived, no_resolver) == []

    def test_wildcard_admission_without_spec(self):
        base = Particle("any", spec=None)
        derived = Particle("element", name="e")
        assert _wildcard_admission_violations(base, derived, no_resolver) == []


class TestSubstitutionAndTypeClause:
    """The substitution walk and the type-subsumption clause."""

    def test_substitution_walk_exhausts_the_depth_cap(self):
        chain = [_Decl(f"h{index}") for index in range(17)]
        heads = {chain[index]: chain[index + 1] for index in range(16)}.get
        base_decl = _Decl("target")
        derived_decl = chain[0]
        assert _same_or_substitution_member(base_decl, derived_decl, heads) is False

    def test_ur_type_admits_simple_but_not_complex(self):
        class _Simple:
            _contentKind_ = "simple"

        class _Complex:
            _contentKind_ = "complex"

        assert _ur_type_admits(AnySimpleType, _Simple) is True
        assert _ur_type_admits(AnySimpleType, _Complex) is False
        assert _ur_type_admits(AnyType, _Unrelated) is True
        assert _ur_type_admits(SchemaBase, _Unrelated) is True

    def test_type_clause_skips_unrelated_non_datatype_classes(self):
        assert _type_clause_violation(_Restricted, _Unrelated) is not None
        assert _type_clause_violation(None, _Unrelated) is None
        assert _type_clause_violation(_Restricted, None) is None


class _Plain:
    """A type that is not an XSD datatype."""


class _UnionStyleValue(String):
    """A stand-in union class whose value comes through ``__new__``."""

    def __init__(self, value):
        raise TypeError("the value is taken through __new__")


class _BrokenValue(String):
    """A datatype whose construction fails with a non-TypeError."""

    def __new__(cls, value):
        raise ValueError("cannot build")


class TestFixedValuesAgree:
    """``_fixed_values_agree``'s fallback comparison branches."""

    def test_lexical_equality_short_circuits(self):
        assert _fixed_values_agree("same", _Restricted, "same", _Unrelated) is True

    def test_non_datatype_classes_fall_back_to_inequality(self):
        assert _fixed_values_agree("a", _Plain, "b", _Plain) is False

    def test_union_style_class_compares_typed_values(self):
        assert _fixed_values_agree("1", _UnionStyleValue, "2", _UnionStyleValue) is False

    def test_construction_failure_is_not_equal(self):
        assert _fixed_values_agree("a", _BrokenValue, "b", _BrokenValue) is False


class TestChoiceAndUnorderedMatchers:
    """The choice/unordered matchers' empty and budgeted shapes."""

    def test_empty_base_choice_reports_no_members(self):
        base = Particle("choice", children=[])
        derived = Particle(
            "choice",
            children=[Particle("element", name="e1"), Particle("element", name="e2")],
        )
        reasons = is_valid_particle_restriction(base, derived, no_resolver)
        assert reasons and "no members" in reasons[0]

    def test_unordered_units_of_a_choice(self):
        first = Particle("element", name="a")
        second = Particle("element", name="b")
        units = _unordered_units(Particle("choice", children=[first, second]))
        assert units == [first, second]

    def test_unordered_units_fan_out_sequence_choice_members(self):
        inner = Particle(
            "choice",
            children=[Particle("element", name="a"), Particle("element", name="b")],
        )
        units = _unordered_units(Particle("sequence", children=[inner]))
        assert [unit.name for unit in units] == ["a", "b"]

    def test_unordered_matcher_skips_when_budget_exhausted(self, monkeypatch):
        monkeypatch.setattr(pd, "_UNORDERED_NODE_BUDGET", 0)
        base = Particle("all", children=[Particle("element", name="a")])
        derived = Particle("sequence", children=[Particle("element", name="a")])
        assert (
            _unordered_violations(base, derived, no_resolver, set(), False, None, "Recurse", False)
            == []
        )

    def test_unordered_accounting_reports_combined_overflow(self):
        base = Particle(
            "all",
            children=[Particle("element", name="a"), Particle("element", name="a")],
        )
        derived = Particle(
            "sequence",
            children=[
                Particle("element", name="a"),
                Particle("element", name="a"),
                Particle("element", name="a"),
            ],
        )
        reasons = _unordered_violations(
            base, derived, no_resolver, set(), False, None, "Recurse", False
        )
        assert reasons and "combined occurrence range" in reasons[0]


class TestWildcardHelpers:
    """The wildcard-probe and coverage helpers."""

    def test_wildcard_probes_skips_unresolvable_tokens(self):
        spec = WildcardSpec(not_qname=frozenset({"##defined", "prefix:x"}))
        probes = _wildcard_probes([spec], None)
        assert None in probes

    def test_split_coverable_without_spec(self):
        base = Particle("all", children=[Particle("element", name="e")])
        assert _wildcard_split_coverable(Particle("any", spec=None), base) is False

    def test_split_coverable_without_overlapping_wildcard(self):
        base = Particle("all", children=[Particle("element", name="e")])
        assert _wildcard_split_coverable(Particle("any", spec=ANY), base) is False

    def test_names_covered_when_spec_excludes_every_local(self):
        spec = WildcardSpec(namespace="##any", not_qname=frozenset({"{urn:a}"}))
        assert _namespace_names_covered(spec, [], "urn:a", None) is True

    def test_names_not_covered_when_defined_keyword_not_inherited(self):
        spec = WildcardSpec(namespace="##any")
        candidate = WildcardSpec(namespace="##any", not_qname=frozenset({DISALLOWED_DEFINED}))
        assert _namespace_names_covered(spec, [candidate], "urn:a", None) is False

    def test_wildcards_overlap_with_explicit_target(self):
        assert _wildcards_overlap(ANY, ANY, "urn:t") is True

    def test_split_wildcard_accounting_uses_containment_probe(self):
        # The split unit's namespace constraint is contained in the base
        # wildcard under the unit's own target, but not under the pair's
        # default probe, so the unit is split (no direct base member) and
        # its occurrence minimum is attributed to the containing member.
        unit = Particle(
            "any",
            spec=WildcardSpec(namespace="##targetNamespace", target_namespace="urn:u"),
        )
        member = Particle("any", spec=WildcardSpec(namespace="##targetNamespace"))
        base = Particle("all", children=[member])
        reasons = _unordered_violations(
            base, unit, no_resolver, set(), False, None, "Recurse", False
        )
        assert reasons and "combined occurrence range" in reasons[0]


class TestPairGuard:
    """``_pair_violations``'s identity-cycle guard."""

    def test_already_visited_pair_is_skipped(self):
        base = Particle("element", name="e")
        derived = Particle("element", name="e")
        visited = {(id(base), id(derived))}
        assert _pair_violations(base, derived, no_resolver, visited) == []


class TestFormAwareNamespaces:
    """``element_namespace`` and its qualification helpers."""

    def test_element_namespace_follows_referred_element(self):
        referred = _Decl("e", namespace="urn:e")
        declaration = _Decl("other")
        declaration.referredElement = referred
        assert element_namespace(declaration) == "urn:e"

    def test_element_namespace_explicit_target_namespace_wins(self):
        declaration = _Decl("e")
        declaration.xsdElement = ET.Element(
            f"{{{XSD_NS}}}element", {"targetNamespace": "urn:explicit"}
        )
        assert element_namespace(declaration) == "urn:explicit"

    def test_element_namespace_without_getter(self):
        class _NoNamespace:
            name = "e"

        assert element_namespace(_NoNamespace()) is None

    def test_local_declaration_without_schema_is_qualified(self):
        class _NoSchema:
            xsdElement = None

            def isGlobalDeclaration(self):
                return False

            def getNamespace(self):
                return None

            def getSchema(self):
                raise AttributeError("no schema")

        assert _local_declaration_is_qualified(_NoSchema()) is True

    def test_local_declaration_with_none_schema_is_qualified(self):
        class _NoneSchema:
            xsdElement = None

            def isGlobalDeclaration(self):
                return False

            def getNamespace(self):
                return None

            def getSchema(self):
                return None

        assert _local_declaration_is_qualified(_NoneSchema()) is True

    def test_local_declaration_without_form_default_getter_is_qualified(self):
        class _PlainSchema:
            formDefaultOverrides = None

        class _Declaration:
            xsdElement = None

            def isGlobalDeclaration(self):
                return False

            def getNamespace(self):
                return None

            def getSchema(self):
                return _PlainSchema()

        assert _local_declaration_is_qualified(_Declaration()) is True
        assert element_namespace(_Declaration()) is None
