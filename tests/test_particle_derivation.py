"""Particle-valid restriction: cos-particle-restrict (XSD 1.0 §3.9.6).

Unit tests over the pure predicate in ``pyxsd.particle_derivation`` plus
schema-level tests for the corpus shapes this sub-task covers (the MS
particlesOb NSSubset cluster, the particlesHb forbidden-transition
cluster, and the particlesIa-Ik NameAndTypeOK cluster, with valid guards
for the shapes the corpus pins legal).
"""

import io

import pytest

from pyxsd.binding import ParseModes
from pyxsd.content_model import Particle
from pyxsd.parser import PyXSD
from pyxsd.particle_derivation import (
    contains_occurs,
    is_valid_particle_restriction,
    wildcard_subset,
)
from pyxsd.schema_base import SchemaBase
from pyxsd.validation import IssueSeverity
from pyxsd.wildcards import WildcardSpec

ANY = WildcardSpec(namespace="##any")
LOCAL = WildcardSpec(namespace="##local")

XSD_HEAD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
XSD_TAIL = "</xs:schema>"

#: A schema root carrying a target namespace and the ``x`` prefix (for
#: the form-awareness cases: refs to globals against unqualified locals).
XSD_HEAD_NS = (
    "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
    "xmlns:x='http://xsdtesting' targetNamespace='http://xsdtesting'>"
)


def schema_codes(report) -> set[str]:
    return {issue.code for issue in report.for_phase("schema")}


def particle_restriction_issues(report) -> list:
    return [
        issue
        for issue in report.for_phase("schema")
        if issue.code == "particle-restriction" and issue.severity is IssueSeverity.ERROR
    ]


@pytest.fixture
def parse(tmp_path, monkeypatch):
    """Parse a schema fragment (wrapped in an ``xs:schema`` root) and
    return the report.

    Mirrors ``tests/xsts/drivers.py::_schema_only_call``: the instance
    phase is stubbed out so a schema declaring no root element can
    still be inspected.
    """
    monkeypatch.setattr(PyXSD, "parseXML", lambda self: None)
    schema_path = tmp_path / "schema.xsd"

    def _parse(schema_string: str, head: str = XSD_HEAD):
        schema_path.write_text(head + schema_string + XSD_TAIL, encoding="utf-8")
        return PyXSD(
            io.StringIO("<pyxsd-schema-probe/>"),
            str(schema_path),
            xmlFileOutput=False,
            mode=ParseModes.NAMESPACED,
        ).report

    return _parse


def no_resolver(_particle):
    return None


class _NamespacedDeclaration:
    """A stand-in element declaration in a namespace of its own."""

    def getNamespace(self):
        return "http://xsdtesting"

    def isGlobalDeclaration(self):
        return True


class TestWildcardSubset:
    def test_wildcard_namespace_subset(self):
        assert wildcard_subset(LOCAL, ANY, target_namespace="http://t")

    def test_wildcard_superset_rejected(self):
        assert not wildcard_subset(ANY, LOCAL, target_namespace="http://t")

    def test_uri_list_subset_by_extension(self):
        # particlesOb047/Ob048: {foo} ⊆ {foo bar}; Ob046: {abce} ⊄ {foo bar}
        base = WildcardSpec(namespace="foo bar")
        assert wildcard_subset(WildcardSpec(namespace="foo"), base, target_namespace="http://t")
        assert wildcard_subset(WildcardSpec(namespace="bar"), base, target_namespace="http://t")
        assert not wildcard_subset(
            WildcardSpec(namespace="abce"), base, target_namespace="http://t"
        )

    def test_uri_list_whitespace_is_insensitive(self):
        # particlesOb042: "  foo  bar " vs "     foo  bar " are the same set
        first = WildcardSpec(namespace="  foo  bar ")
        second = WildcardSpec(namespace="     foo  bar ")
        assert wildcard_subset(first, second, target_namespace="http://t")

    def test_other_rejects_local_and_target(self):
        # particlesOb013/Ob014: ##other admits neither ##local nor ##targetNamespace
        other = WildcardSpec(namespace="##other")
        assert not wildcard_subset(LOCAL, other, target_namespace="http://t")
        assert not wildcard_subset(
            WildcardSpec(namespace="##targetNamespace"), other, target_namespace="http://t"
        )

    def test_other_admits_foreign_uri(self):
        # particlesOb015: {foo bar} ⊆ ##other when neither is the target
        other = WildcardSpec(namespace="##other")
        assert wildcard_subset(
            WildcardSpec(namespace="foo bar"), other, target_namespace="http://t"
        )


class TestOccurrenceContainment:
    def test_derived_min_below_base_is_rejected(self):
        assert not contains_occurs(0, 1, 1, 1)

    def test_derived_max_above_base_is_rejected(self):
        assert not contains_occurs(1, 2, 1, 1)

    def test_unbounded_base_admits_any_derived_max(self):
        assert contains_occurs(5, None, 0, None)
        assert contains_occurs(5, 7, 0, None)

    def test_bounded_base_rejects_unbounded_derived(self):
        assert not contains_occurs(0, None, 0, 1)

    def test_equal_ranges_are_contained(self):
        assert contains_occurs(1, 1, 1, 1)
        assert contains_occurs(0, 0, 0, 0)


class TestPredicateCells:
    def test_wildcard_restriction_of_element_is_forbidden(self):
        # particlesHb001: a wildcard can never restrict an element
        # declaration ("Forbidden: drived by restriction any : elt")
        base = Particle("element", name="e")
        derived = Particle("any", spec=ANY)
        assert is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_element_restriction_of_wildcard_with_unknown_declaration_is_valid(self):
        # The brief's table marks elt-over-any forbidden, but the corpus
        # pins such restrictions VALID when the occurrences are contained
        # and the wildcard admits the element (particlesJa001/Jl001) —
        # with no resolvable declaration the admission check gives the
        # benefit of the doubt, so this is a valid pair.
        base = Particle("any", spec=ANY)
        derived = Particle("element", name="e")
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_any_to_model_group_is_forbidden(self):
        base = Particle("sequence", children=[Particle("element", name="e")])
        derived = Particle("any", spec=ANY)
        assert is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_choice_over_element_at_top_level_is_forbidden(self):
        # the corpus pins the top-level choice-over-element shape
        # invalid (particlesHa123): a 1..1 singleton choice would be
        # pointless, but this choice's member widens the occurrence and
        # the table forbids the shape outright
        base = Particle("element", name="e", min_occurs=1, max_occurs=1)
        derived = Particle("choice", children=[Particle("element", name="e", max_occurs=2)])
        assert is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_choice_over_all_is_forbidden(self):
        # particlesHb009: choice(e1, e2) restricting all(e1, e2) — each
        # branch of the choice drops a required member of the all
        base = Particle(
            "all",
            children=[Particle("element", name="e1"), Particle("element", name="e2")],
        )
        derived = Particle(
            "choice",
            children=[
                Particle("element", name="e1"),
                Particle("element", name="e2"),
            ],
        )
        assert is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_choice_over_all_carrying_required_members_is_deferred(self):
        # saxon all232: every branch repeats the all's required member,
        # so the shape is accepted under the 1.1 profile; the pairwise
        # verification waits for the Recurse work
        base = Particle(
            "all",
            children=[
                Particle("element", name="a", min_occurs=0, max_occurs=5),
                Particle("element", name="b", max_occurs=5),
                Particle("element", name="d", min_occurs=0, max_occurs=1),
            ],
        )
        derived = Particle(
            "choice",
            children=[
                Particle(
                    "sequence",
                    children=[
                        Particle("element", name="d"),
                        Particle("element", name="b", min_occurs=3, max_occurs=4),
                    ],
                ),
                Particle(
                    "sequence",
                    children=[
                        Particle("element", name="a"),
                        Particle("element", name="b", min_occurs=3, max_occurs=4),
                    ],
                ),
            ],
        )
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_group_over_element_at_top_level_is_forbidden(self):
        # particlesHa121: a non-pointless group restricting a base
        # element is Forbidden at the derivation's own pair; the corpus
        # pins the exemplars valid only where an *enclosing* Recurse
        # alignment absorbs the members (particlesHb008/Hb011, covered
        # by test_sequence_over_element_inside_choice_maps)
        base = Particle("element", name="e")
        derived = Particle(
            "sequence",
            children=[Particle("element", name="e"), Particle("element", name="f")],
        )
        assert is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_sequence_over_element_inside_choice_maps(self):
        # particlesHb011's shape: the repeated sequence member maps onto
        # the base element under the 1.1 profile
        base = Particle(
            "choice",
            min_occurs=2,
            max_occurs=None,
            children=[
                Particle("element", name="e1", min_occurs=0, max_occurs=10),
                Particle("element", name="e2", min_occurs=0),
                Particle("element", name="e3", min_occurs=0),
            ],
        )
        derived = Particle(
            "choice",
            min_occurs=2,
            max_occurs=None,
            children=[
                Particle(
                    "sequence",
                    min_occurs=1,
                    max_occurs=2,
                    children=[Particle("element", name="e1", max_occurs=2)],
                ),
                Particle("element", name="e2"),
                Particle("element", name="e3", min_occurs=1),
            ],
        )
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_any_to_any_subset_is_valid(self):
        base = Particle("any", spec=ANY)
        derived = Particle("any", spec=LOCAL)
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_occurrence_widening_rejected(self):
        base = Particle("element", name="e", min_occurs=1, max_occurs=1)
        derived = Particle("element", name="e", min_occurs=0, max_occurs=2)
        assert is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_element_over_admitting_wildcard_is_valid(self):
        # particlesJa001/Jl001: an element restricting a wildcard that
        # admits it, with contained occurrences, is legal
        base = Particle("any", spec=ANY, min_occurs=0, max_occurs=1)
        derived = Particle("element", name="e", min_occurs=0, max_occurs=1)
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_element_over_narrower_wildcard_namespace_is_invalid(self):
        # particlesJl002: the element's namespace must be admitted
        base = Particle("any", spec=WildcardSpec(namespace="http://other"))
        derived = Particle("element", name="e")
        resolver = lambda _particle: _NamespacedDeclaration()  # noqa: E731
        assert is_valid_particle_restriction(base, derived, resolver=resolver)

    def test_choice_over_choice_maps_members(self):
        # particlesM002: each derived choice member must find a base
        # member it validly restricts; order is irrelevant
        base = Particle(
            "choice",
            children=[
                Particle("element", name="a", min_occurs=2, max_occurs=2),
                Particle("element", name="b"),
            ],
        )
        derived = Particle("choice", children=[Particle("element", name="b")])
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_choice_over_choice_with_unmappable_member_is_invalid(self):
        base = Particle(
            "choice",
            children=[
                Particle("element", name="a"),
                Particle("any", spec=LOCAL),
            ],
        )
        derived = Particle("choice", children=[Particle("any", spec=ANY)])
        assert is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_sequence_over_sequence_is_positional(self):
        # particlesW: sequence restriction is pairwise in order
        base = Particle(
            "sequence",
            children=[Particle("element", name="a"), Particle("element", name="b")],
        )
        derived = Particle(
            "sequence",
            children=[Particle("element", name="b"), Particle("element", name="a")],
        )
        # name equality is Task 4b's NameAndTypeOK work; occurrence and
        # shape checks are all this sub-task reports, so this stays valid
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_nested_forbidden_shape_is_found(self):
        # particlesHb003: choice(any) restricting choice(choice(e1, e2))
        base = Particle(
            "choice",
            children=[
                Particle(
                    "choice",
                    children=[
                        Particle("element", name="e1", min_occurs=0),
                        Particle("element", name="e2", min_occurs=0),
                    ],
                )
            ],
        )
        derived = Particle("choice", children=[Particle("any", spec=ANY)])
        assert is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_wildcard_process_contents_widening_is_invalid(self):
        base = Particle("any", spec=WildcardSpec(namespace="##any", process_contents="strict"))
        derived = Particle("any", spec=WildcardSpec(namespace="##any", process_contents="lax"))
        assert is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_wildcard_process_contents_narrowing_is_valid(self):
        base = Particle("any", spec=WildcardSpec(namespace="##any", process_contents="lax"))
        derived = Particle("any", spec=WildcardSpec(namespace="##any", process_contents="strict"))
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_single_particle_sequence_is_transparent(self):
        # Hb002: the base's group reference compiles to a synthetic 1/1
        # sequence wrapper; shape decisions see through it (choice over
        # all is forbidden, choice over the wrapper would be a different
        # cell)
        base = Particle(
            "sequence",
            children=[Particle("all", children=[Particle("element", name="e1", min_occurs=0)])],
            synthetic=True,
        )
        derived = Particle("choice", children=[Particle("any", spec=ANY)])
        assert is_valid_particle_restriction(base, derived, resolver=no_resolver)


class TestSchemaParticlesOb:
    def test_nssubset_process_contents_widening_is_invalid(self, parse):
        # particlesOb001: ##any/strict restricted by (absent ##any)/lax
        report = parse(
            "<xs:complexType name='B'><xs:choice>"
            "<xs:any namespace='##any' minOccurs='1' maxOccurs='1'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:choice><xs:any minOccurs='1' maxOccurs='1' processContents='lax'/></xs:choice>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "NSSubset" in issues[0].message

    def test_nssubset_namespace_widening_is_invalid(self, parse):
        # particlesOb019: ##local restricted by the default (absent = ##any)
        report = parse(
            "<xs:complexType name='B'><xs:choice>"
            "<xs:any namespace='##local' minOccurs='1' maxOccurs='1'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:choice><xs:any minOccurs='1' maxOccurs='1'/></xs:choice>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "NSSubset" in issues[0].message

    def test_nssubset_contained_restriction_is_valid(self, parse):
        # particlesOb003: ##other 4..6 inside ##any 1..10 with default
        # strict processContents is a valid restriction
        report = parse(
            "<xs:complexType name='B'><xs:choice>"
            "<xs:any namespace='##any' minOccurs='1' maxOccurs='10'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:choice>"
            "<xs:any namespace='##other' minOccurs='4' maxOccurs='6'/>"
            "</xs:choice>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)


class TestSchemaParticlesHb:
    def test_any_over_element_is_invalid(self, parse):
        # particlesHb001: choice(any) restricting choice(elt)
        report = parse(
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='e1' minOccurs='2' maxOccurs='10'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='base'>"
            "<xs:choice><xs:any minOccurs='3' maxOccurs='9'/></xs:choice>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "Forbidden" in issues[0].message

    def test_choice_over_all_is_invalid(self, parse):
        # particlesHb009: choice(e1, e2) restricting all(e1, e2) — neither
        # branch can occur while satisfying the all's required members
        report = parse(
            "<xs:complexType name='base'><xs:all>"
            "<xs:element name='e1'/><xs:element name='e2'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='base'>"
            "<xs:choice>"
            "<xs:element name='e1'/><xs:element name='e2'/>"
            "</xs:choice>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "Recurse" in issues[0].message

    def test_element_over_all_with_contained_occurrence_is_valid(self, parse):
        # particlesK001: an element restricting an all with contained
        # occurrence is legal (RecurseAsIfGroup); this guards the
        # element-over-group cells against over-forbidding
        report = parse(
            "<xs:complexType name='base'><xs:all>"
            "<xs:element name='a0' minOccurs='0'/>"
            "<xs:element name='a1'/>"
            "<xs:element name='a2' minOccurs='0'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='base'>"
            "<xs:sequence><xs:element name='a1'/></xs:sequence>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_element_over_wildcard_with_contained_occurrence_is_valid(self, parse):
        # particlesJa001: an element restricting a wildcard base is a
        # RecurseAsIfGroup shape, not a forbidden one
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:any namespace='##any' minOccurs='0'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='base'>"
            "<xs:sequence><xs:element name='e1' minOccurs='0'/></xs:sequence>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_choice_over_sequence_is_not_reported(self, parse):
        # particlesHb010: the corpus pins choice-over-sequence VALID
        # against the spec's forbidden cell, so this sub-task defers the
        # cell entirely rather than regress the corpus case
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:element name='e1' minOccurs='0'/>"
            "<xs:element name='e2' minOccurs='0'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='base'>"
            "<xs:choice><xs:element name='e1' minOccurs='0'/></xs:choice>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_occurrence_widening_over_wildcard_base_is_invalid(self, parse):
        # particlesHa-class: base any minOccurs=1 restricted by element
        # minOccurs=0 widens the occurrence range
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:any namespace='##any' minOccurs='1'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='base'>"
            "<xs:sequence><xs:element name='e1' minOccurs='0'/></xs:sequence>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "RecurseAsIfGroup" in issues[0].message


class TestPointlessRulesAtTopLevel:
    """The particlesHa121-Ha189 "Apply Pointless rules at top level" matrix.

    §3.9.6 clause 2.2 eliminates pointless compositors before the shape
    table applies: a 1..1 ``sequence``/``choice``/``all`` holding one
    member is ignored (replaced by that member), so the top-level pair is
    the *eliminated* particles on both sides.
    """

    def test_base_sequence_over_derived_sequence_is_invalid(self, parse):
        # particlesHa121: the singleton base sequence is pointless, so
        # the pair is sequence-over-element — Forbidden in the table
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:element name='e1' type='xs:string'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:element name='e1' type='xs:ENTITY'/>"
            "<xs:element name='e2' type='xs:ENTITY'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "Forbidden" in issues[0].message

    def test_base_element_over_derived_singleton_all_is_valid(self, parse):
        # particlesHa122: the derived singleton all is pointless too, so
        # the pair is element-over-element (NameAndTypeOK, valid)
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:element name='e1' type='xs:string'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'><xs:group ref='grp'/></xs:restriction>"
            "</xs:complexContent></xs:complexType>"
            "<xs:group name='grp'><xs:all>"
            "<xs:element name='e1' type='xs:string'/></xs:all></xs:group>"
        )
        assert not particle_restriction_issues(report)

    def test_optional_singleton_choice_over_element_is_invalid(self, parse):
        # particlesHa123: the 0..1 choice is not pointless (its
        # containing particle is not 1..1); choice-over-element is
        # Forbidden in the table
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:element name='e1' type='xs:string'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'>"
            "<xs:choice minOccurs='0'><xs:element name='e1' type='xs:string'/></xs:choice>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "Forbidden" in issues[0].message

    def test_choice_over_choice_with_widened_bound_is_invalid(self, parse):
        # particlesHa164: both singleton sequences are pointless; the
        # derived choice's 2..3 bound exceeds the base choice's 2..2
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:choice maxOccurs='2'>"
            "<xs:element name='a'/><xs:element name='b'/>"
            "</xs:choice></xs:sequence></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:choice maxOccurs='3'>"
            "<xs:element name='a'/><xs:element name='b'/>"
            "</xs:choice></xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues

    def test_choice_over_choice_with_equal_bound_is_valid(self, parse):
        # particlesHa165: the same shape with matching bounds is valid
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:choice maxOccurs='2'>"
            "<xs:element name='a'/><xs:element name='b'/>"
            "</xs:choice></xs:sequence></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:choice maxOccurs='2'><xs:element name='a'/></xs:choice>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_choice_member_occurrence_widening_is_invalid(self, parse):
        # particlesHa166: after the pointless elimination the derived
        # member a(0..1) cannot restrict the base member a(1..1)
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:choice maxOccurs='2'>"
            "<xs:element name='a' minOccurs='1'/><xs:element name='b'/>"
            "</xs:choice></xs:sequence></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:choice maxOccurs='2'><xs:element name='a' minOccurs='0'/></xs:choice>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues

    def test_sequence_over_choice_with_contained_bound_is_valid(self, parse):
        # particlesHa167: a derived sequence whose total range fits the
        # base choice is MapAndSum-valid
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:choice maxOccurs='2'>"
            "<xs:element name='a' minOccurs='1'/><xs:element name='b'/>"
            "</xs:choice></xs:sequence></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:sequence maxOccurs='2'><xs:element name='a' minOccurs='1'/></xs:sequence>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_sequence_over_choice_with_widened_bound_is_invalid(self, parse):
        # particlesHa168: the sequence's effective range 3..3 exceeds
        # the base choice's 2..2
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:choice maxOccurs='2'>"
            "<xs:element name='a' minOccurs='1'/><xs:element name='b'/>"
            "</xs:choice></xs:sequence></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:sequence maxOccurs='3'><xs:element name='a' minOccurs='1'/></xs:sequence>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues

    def test_sequence_member_occurrence_widening_is_invalid(self, parse):
        # particlesHa169: the derived member a(0..1) is not contained in
        # the base member a(1..1)
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:choice maxOccurs='2'>"
            "<xs:element name='a' minOccurs='1'/><xs:element name='b'/>"
            "</xs:choice></xs:sequence></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:sequence maxOccurs='2'><xs:element name='a' minOccurs='0'/></xs:sequence>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues

    def test_choice_over_all_with_dropped_required_member_is_invalid(self, parse):
        # particlesHa182: the singleton derived sequence is pointless;
        # each choice branch alone drops a required all member
        report = parse(
            "<xs:complexType name='base'><xs:all>"
            "<xs:element name='a'/><xs:element name='b'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:choice><xs:element name='a'/><xs:element name='b'/></xs:choice>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues


# ---------------------------------------------------------------------------
# NameAndTypeOK (cos-particle-restrict case [1]): the element-particle vs
# element-particle cell. Corpus clusters: particlesIa-Ik.
# ---------------------------------------------------------------------------


class _Restricted:
    _derivation_ = "restriction"


class _Extended(_Restricted):
    _derivation_ = "extension"


class _Unrelated:
    pass


class _Constraint:
    """A stand-in identity constraint."""

    def __init__(self, kind="Key", name="k", selector="a", fields=("f",)):
        self.kind = kind
        self.constraintName = name
        self.selector = selector
        self.fieldPaths = list(fields)


class _Declaration:
    """A stand-in element declaration for NameAndTypeOK unit tests."""

    def __init__(
        self,
        name="e",
        namespace=None,
        type_=None,
        fixed=None,
        nillable=False,
        block=None,
        identities=(),
    ):
        self.name = name
        self._namespace = namespace
        self._type = type_
        self._fixed = fixed
        self._nillable = nillable
        self._block = block
        self.identities = list(identities)

    def getNamespace(self):
        return self._namespace

    def getType(self):
        return self._type

    def getFixed(self):
        return self._fixed

    def isNillable(self):
        return self._nillable

    def getBlock(self):
        return self._block


def _resolver_of(base_decl, derived_decl):
    def resolver(particle):
        if particle is None:
            return None
        return base_decl if particle.name == "b" else derived_decl

    return resolver


def _nameandtypeok(base_decl, derived_decl, head_lookup=None):
    base = Particle("element", name="b")
    derived = Particle("element", name="d")
    return is_valid_particle_restriction(
        base, derived, resolver=_resolver_of(base_decl, derived_decl), head_lookup=head_lookup
    )


class TestNameAndTypeOK:
    def test_same_expanded_name_is_valid(self):
        # particlesIb001: B name=foo, R name=foo
        assert not _nameandtypeok(
            _Declaration(name="foo", namespace="http://t"),
            _Declaration(name="foo", namespace="http://t"),
        )

    def test_name_mismatch_is_invalid(self):
        # particlesIb002: B name=foo, R name=bar
        reasons = _nameandtypeok(_Declaration(name="foo"), _Declaration(name="bar"))
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_namespace_mismatch_is_invalid(self):
        # particlesIc002: same local name, different target namespaces
        reasons = _nameandtypeok(
            _Declaration(name="e", namespace="http://foo"),
            _Declaration(name="e", namespace="http://bar"),
        )
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_substitution_group_member_is_valid(self):
        # R's declaration is a direct member of B's substitution group
        base_decl = _Declaration(name="foo")
        derived_decl = _Declaration(name="sub")
        heads = {derived_decl: base_decl}
        assert not _nameandtypeok(base_decl, derived_decl, head_lookup=heads.get)

    def test_transitive_substitution_group_is_valid(self):
        # A ← B ← C: C restricts a particle declaring A
        head_a = _Declaration(name="a")
        head_b = _Declaration(name="b")
        member_c = _Declaration(name="c")
        heads = {member_c: head_b, head_b: head_a}
        assert not _nameandtypeok(head_a, member_c, head_lookup=heads.get)

    def test_outside_substitution_group_is_invalid(self):
        base_decl = _Declaration(name="foo")
        other_head = _Declaration(name="other")
        derived_decl = _Declaration(name="sub")
        heads = {derived_decl: other_head}
        reasons = _nameandtypeok(base_decl, derived_decl, head_lookup=heads.get)
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_same_type_is_valid(self):
        assert not _nameandtypeok(_Declaration(type_=_Restricted), _Declaration(type_=_Restricted))

    def test_restriction_derived_type_is_valid(self):
        # particlesIj005: R's type is a restriction of B's type
        assert not _nameandtypeok(
            _Declaration(type_=_Restricted.__mro__[1]),
            _Declaration(type_=_Restricted),
        )

    def test_extension_derived_type_is_invalid(self):
        # particlesIj008: B type=foo, R type=Z derived by *extension*
        # of foo — extension steps may not appear in a restriction
        base_cls = _Restricted.__mro__[1]
        reasons = _nameandtypeok(_Declaration(type_=base_cls), _Declaration(type_=_Extended))
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_base_any_type_admits_derived(self):
        # particlesIk014: B type=anyType, R type=simpleType 'foo'
        assert not _nameandtypeok(_Declaration(type_=SchemaBase), _Declaration(type_=_Unrelated))

    def test_derived_any_type_is_invalid(self):
        # particlesIj015/Ik015: B carries a real type, R widens to anyType
        reasons = _nameandtypeok(_Declaration(type_=_Restricted), _Declaration(type_=SchemaBase))
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_unresolved_type_skips_the_type_clause(self):
        # base unresolved -> the clause is skipped, never an error
        assert not _nameandtypeok(_Declaration(type_=None), _Declaration(type_=_Unrelated))

    def test_fixed_value_change_is_invalid(self):
        # particlesIf007: B fixed=foo, R fixed=bar
        reasons = _nameandtypeok(_Declaration(fixed="foo"), _Declaration(fixed="bar"))
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_fixed_value_may_not_be_dropped(self):
        # particlesIf009: B fixed=foo, R fixed=absent
        reasons = _nameandtypeok(_Declaration(fixed="foo"), _Declaration())
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_same_fixed_value_is_valid(self):
        # particlesIf005
        assert not _nameandtypeok(_Declaration(fixed="foo"), _Declaration(fixed="foo"))

    def test_nillable_may_not_be_added(self):
        # particlesIa006: B nillable=FALSE, R nillable=TRUE
        reasons = _nameandtypeok(_Declaration(), _Declaration(nillable=True))
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_nillable_may_be_dropped(self):
        # particlesIa007: B nillable=TRUE, R nillable=FALSE is legal
        assert not _nameandtypeok(_Declaration(nillable=True), _Declaration())

    def test_block_superset_of_base_is_invalid(self):
        # particlesIg006: B disallowed=sub ext res, R disallowed=sub
        reasons = _nameandtypeok(
            _Declaration(block="substitution extension restriction"),
            _Declaration(block="substitution"),
        )
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_block_all_on_derived_is_valid(self):
        # particlesIg005: B disallowed=sub ext res, R disallowed=#all
        assert not _nameandtypeok(
            _Declaration(block="substitution extension restriction"),
            _Declaration(block="#all"),
        )

    def test_block_all_on_base_requires_full_subset(self):
        # particlesIg007: B disallowed=#all, R disallowed=sub ext
        reasons = _nameandtypeok(
            _Declaration(block="#all"),
            _Declaration(block="substitution extension"),
        )
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_identity_constraints_must_be_carried(self):
        # the base declaration's identity constraints are a subset of
        # the derived declaration's
        constraint = _Constraint()
        reasons = _nameandtypeok(_Declaration(identities=[constraint]), _Declaration())
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_identity_constraint_superset_is_valid(self):
        constraint = _Constraint()
        assert not _nameandtypeok(
            _Declaration(identities=[constraint]),
            _Declaration(identities=[constraint, _Constraint(name="k2")]),
        )

    def test_member_occurrence_containment_inside_plain_compositor(self):
        # particlesId003: choice(1,1)[e1(1,1), e2(1,1)] restricted by
        # choice(1,1)[e1(0,1), e2(0,1)] — the member range widens
        declaration = _Declaration()
        resolver = lambda _particle: declaration  # noqa: E731
        base = Particle(
            "choice",
            children=[
                Particle("element", name="e1"),
                Particle("element", name="e2"),
            ],
        )
        derived = Particle(
            "choice",
            children=[
                Particle("element", name="e1", min_occurs=0),
                Particle("element", name="e2", min_occurs=0),
            ],
        )
        reasons = is_valid_particle_restriction(base, derived, resolver=resolver)
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_member_removal_inside_plain_compositor_is_valid(self):
        # mgH014: a choice member with maxOccurs=0 can never be chosen,
        # so it removes itself — a valid restriction even when the base
        # member is required
        declaration = _Declaration()
        resolver = lambda _particle: declaration  # noqa: E731
        base = Particle(
            "choice",
            children=[
                Particle("element", name="e1"),
                Particle("element", name="e2"),
            ],
        )
        derived = Particle(
            "choice",
            children=[
                Particle("element", name="e1", min_occurs=0, max_occurs=0),
                Particle("element", name="e2"),
            ],
        )
        assert not is_valid_particle_restriction(base, derived, resolver=resolver)

    def test_removal_on_the_derivation_pair_is_invalid(self):
        # mgE006 posture: on the derivation's own pair (and inside
        # sequences) a maxOccurs=0 member does not exempt the pair from
        # the occurrence clause
        declaration = _Declaration()
        resolver = lambda _particle: declaration  # noqa: E731
        base = Particle("element", name="e1")
        derived = Particle("element", name="e1", min_occurs=0, max_occurs=0)
        assert is_valid_particle_restriction(base, derived, resolver=resolver)

    def test_sequence_member_removal_is_invalid(self):
        # the maxOccurs=0 exemption is a choice-member rule only
        declaration = _Declaration()
        resolver = lambda _particle: declaration  # noqa: E731
        base = Particle("sequence", children=[Particle("element", name="e1")])
        derived = Particle(
            "sequence",
            children=[Particle("element", name="e1", min_occurs=0, max_occurs=0)],
        )
        assert is_valid_particle_restriction(base, derived, resolver=resolver)

    def test_member_occurrence_widening_inside_equally_repeated_compositor_is_invalid(self):
        # With identical compositor ranges the member range is the
        # effective range, so widening it is a real violation
        base = Particle(
            "choice",
            min_occurs=2,
            max_occurs=4,
            children=[Particle("element", name="e", min_occurs=1, max_occurs=3)],
        )
        derived = Particle(
            "choice",
            min_occurs=2,
            max_occurs=4,
            children=[Particle("element", name="e", min_occurs=4, max_occurs=5)],
        )
        reasons = is_valid_particle_restriction(base, derived, resolver=no_resolver)
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_member_occurrence_widening_inside_differing_repeated_compositor_is_skipped(self):
        # When the enclosing compositors' ranges differ, the occurrence
        # multipliers differ and member ranges cannot be compared
        # pairwise; the corpus pins such shapes legal (mgH014/W006
        # posture), so the check stays silent
        base = Particle(
            "choice",
            min_occurs=2,
            max_occurs=4,
            children=[Particle("element", name="e", min_occurs=1, max_occurs=3)],
        )
        derived = Particle(
            "choice",
            min_occurs=2,
            max_occurs=2,
            children=[Particle("element", name="e", min_occurs=4, max_occurs=5)],
        )
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

    def test_unresolvable_declarations_skip_the_declaration_clauses(self):
        base = Particle("element", name="b")
        derived = Particle("element", name="d")
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)


class TestSchemaParticlesI:
    def test_name_mismatch_is_invalid(self, parse):
        # particlesIb002: B name=foo, R name=bar
        report = parse(
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='foo'/><xs:element name='e2'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:element name='bar'/><xs:element name='e2'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "NameAndTypeOK" in issues[0].message

    def test_member_occurrence_widening_is_invalid(self, parse):
        # particlesId003: base members minOccurs=1, restricted members 0
        report = parse(
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='e1' minOccurs='1'/><xs:element name='e2' minOccurs='1'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:element name='e1' minOccurs='0'/><xs:element name='e2' minOccurs='0'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "NameAndTypeOK" in issues[0].message

    def test_extension_type_replacement_is_invalid(self, parse):
        # particlesIj008: R's element widens the type by extension
        report = parse(
            "<xs:complexType name='foo'><xs:choice><xs:element name='f1'/></xs:choice>"
            "</xs:complexType>"
            "<xs:complexType name='bar'><xs:complexContent>"
            "<xs:extension base='foo'><xs:choice><xs:element name='f3'/></xs:choice>"
            "</xs:extension></xs:complexContent></xs:complexType>"
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='c1' type='foo'/><xs:element name='c2'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:element name='c1' type='bar'/><xs:element name='c2'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "NameAndTypeOK" in issues[0].message

    def test_unrelated_list_types_are_invalid(self, parse):
        # particlesIk005/006/007: two independently declared list types
        report = parse(
            "<xs:simpleType name='L1'><xs:list itemType='xs:string'/></xs:simpleType>"
            "<xs:simpleType name='L2'><xs:list itemType='xs:integer'/></xs:simpleType>"
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='c1' type='L1'/><xs:element name='c2'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:element name='c1' type='L2'/><xs:element name='c2'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "NameAndTypeOK" in issues[0].message

    def test_fixed_value_change_is_invalid(self, parse):
        # particlesIf007
        report = parse(
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='e1' type='xs:string' fixed='foo'/>"
            "<xs:element name='e2'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:element name='e1' type='xs:string' fixed='bar'/>"
            "<xs:element name='e2'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "NameAndTypeOK" in issues[0].message

    def test_added_nillable_is_invalid(self, parse):
        # particlesIa006
        report = parse(
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='e1'/><xs:element name='e2'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:element name='e1' nillable='true'/><xs:element name='e2'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "NameAndTypeOK" in issues[0].message

    def test_block_narrowing_is_invalid(self, parse):
        # particlesIg006: the base's disallowed substitutions must be a
        # subset of the derived declaration's
        report = parse(
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='e2' block='substitution extension restriction'/>"
            "<xs:element name='e3'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:element name='e2' block='substitution'/>"
            "<xs:element name='e3'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "NameAndTypeOK" in issues[0].message

    def test_substitution_group_member_is_valid(self, parse):
        # a restriction may swap a base element for a member of its
        # substitution group (the type must still validly derive)
        report = parse(
            "<xs:simpleType name='small'><xs:restriction base='xs:string'>"
            "<xs:maxLength value='3'/></xs:restriction></xs:simpleType>"
            "<xs:element name='head' type='xs:string'/>"
            "<xs:element name='member' type='small' substitutionGroup='head'/>"
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element ref='head'/><xs:element name='e2'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:element ref='member'/><xs:element name='e2'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_equivalent_restriction_is_clean(self, parse):
        # particlesIb001/Ia001/Id004: redeclaring the same members with
        # the same type and contained occurrences is valid
        report = parse(
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='foo' type='xs:string'/><xs:element name='e2'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:element name='foo' type='xs:string'/><xs:element name='e2'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)


def _elt(decl, min_occurs=1, max_occurs=1):
    """An element particle carrying a stand-in declaration."""
    return Particle(
        "element", min_occurs=min_occurs, max_occurs=max_occurs, name=decl.name, descriptor=decl
    )


def _decls_resolver(particle):
    return particle.descriptor


def _wild(namespace, min_occurs=1, max_occurs=1, process_contents="strict"):
    return Particle(
        "any",
        min_occurs=min_occurs,
        max_occurs=max_occurs,
        spec=WildcardSpec(namespace=namespace, process_contents=process_contents),
    )


def _group(kind, *children, min_occurs=1, max_occurs=1):
    return Particle(kind, min_occurs=min_occurs, max_occurs=max_occurs, children=list(children))


class TestRecurseSequenceAlignment:
    """Recurse over sequence:sequence — order-preserving, skippable extras."""

    def test_optional_bases_absent_ok(self):
        # particlesW008: B has (a, b, c), b and c emptiable, R has (a)
        base = _group(
            "sequence",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b"), 0, 1),
            _elt(_Declaration(name="c"), 0, 1),
        )
        derived = _group("sequence", _elt(_Declaration(name="a")))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_required_base_particle_cannot_be_left_out(self):
        # particlesW010: c is NOT emptiable, R has (a, b)
        base = _group(
            "sequence",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
        )
        derived = _group("sequence", _elt(_Declaration(name="a")), _elt(_Declaration(name="b")))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_extra_member_is_invalid(self):
        # particlesW012: R has (a, b, c, d)
        base = _group(
            "sequence",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
            _elt(_Declaration(name="d")),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_reordered_members_are_invalid(self):
        # particlesW007: B has (a, b), R has (b, a) — order matters
        base = _group("sequence", _elt(_Declaration(name="a")), _elt(_Declaration(name="b")))
        derived = _group("sequence", _elt(_Declaration(name="b")), _elt(_Declaration(name="a")))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_leading_optional_member_may_be_dropped(self):
        # wild068: the derived sequence drops an optional *leading* member
        base = _group("sequence", _elt(_Declaration(name="e"), 0, 1), _elt(_Declaration(name="f")))
        derived = _group("sequence", _elt(_Declaration(name="f")))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_absorbing_within_supply_is_valid(self):
        # the base compositor supplies two copies of its member (2 x 1), so
        # two derived members may share it
        base = _group(
            "sequence",
            _elt(_Declaration(name="a"), 0, 1),
            min_occurs=1,
            max_occurs=2,
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="a"), 0, 1),
        )
        assert not is_valid_particle_restriction(base, derived, no_resolver)

    def test_absorbing_beyond_supply_fails(self):
        # the base supplies at most two copies of its member (2 x 1); a
        # derived sequence demanding five may not absorb them all
        base = _group(
            "sequence",
            _elt(_Declaration(name="a"), 0, 1),
            min_occurs=1,
            max_occurs=2,
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="a"), 0, 1),
        )
        reasons = is_valid_particle_restriction(base, derived, no_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_supply_one_vs_two_absorbed_fails(self):
        # boundary: supply 1 (1 x 1) admits exactly one derived member; a
        # second may not map, not even by the plain advance branch. A
        # single-member seq(1,1) base would be unwrapped to its bare
        # element before the alignment runs (the GroupOverElement
        # deferral), so the boundary is pinned on a two-member base
        # where the alignment decides.
        base = _group(
            "sequence",
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="b")),
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="b")),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_supply_one_admits_one(self):
        # the valid side of the supply-1 boundary: one derived member
        # against the single permitted copy
        base = _group(
            "sequence",
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="b")),
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="b")),
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_supply_two_vs_three_absorbed_fails(self):
        # boundary: supply 2 (2 x 1) admits exactly two derived members; a
        # third may not map, not even by the plain advance branch
        base = _group(
            "sequence",
            _elt(_Declaration(name="a"), 0, 1),
            min_occurs=1,
            max_occurs=2,
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="a"), 0, 1),
            _elt(_Declaration(name="a"), 0, 1),
        )
        reasons = is_valid_particle_restriction(base, derived, no_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_equal_length_still_pairs_positionally(self):
        # particlesW011: same members, same order
        base = _group(
            "sequence",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_repeated_base_member_may_serve_two_derived_members(self):
        # particlesZ028 absorption: seq{a, b} restricting a repeated choice of
        # the substitution-group heads — the choice is served twice
        head = _Declaration(name="aba")
        sub_a = _Declaration(name="a")
        sub_b = _Declaration(name="b")
        heads = {sub_a: head, sub_b: head}
        base = _group(
            "sequence",
            _group(
                "sequence",
                _group("choice", _elt(head, 1, 1), _elt(_Declaration(name="abb"), 1, 1)),
                min_occurs=0,
                max_occurs=None,
            ),
            _elt(_Declaration(name="d"), 0, 1),
        )
        derived = _group(
            "sequence",
            _group(
                "sequence",
                _elt(sub_a),
                _elt(sub_b),
                min_occurs=0,
                max_occurs=1,
            ),
            _elt(_Declaration(name="d"), 0, 1),
        )
        assert not is_valid_particle_restriction(
            base, derived, _decls_resolver, head_lookup=heads.get
        )


class TestRecurseChoiceMapping:
    """RecurseLax over choice:choice — injective, order-insensitive."""

    def test_mapping_is_injective(self):
        # two derived branches may not consume the same base branch
        base = _group("choice", _elt(_Declaration(name="a")), _elt(_Declaration(name="b")))
        derived = _group(
            "choice", _elt(_Declaration(name="a"), 0, 1), _elt(_Declaration(name="a"), 0, 1)
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_non_emptiable_branch_may_be_left_out(self):
        # particlesT005: B has (a | b | c), c NOT emptiable, R has (a)
        base = _group(
            "choice",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
        )
        derived = _group("choice", _elt(_Declaration(name="a")))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_reordered_branches_are_valid(self):
        # particlesT002 under the 1.1 profile: order-insensitive
        base = _group("choice", _elt(_Declaration(name="a")), _elt(_Declaration(name="b")))
        derived = _group("choice", _elt(_Declaration(name="b")), _elt(_Declaration(name="a")))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_unmappable_branch_is_invalid(self):
        # particlesT008: R has (a | b | c | d)
        base = _group(
            "choice",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
        )
        derived = _group(
            "choice",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
            _elt(_Declaration(name="d")),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_substitution_group_branches_consume_distinct_base_branches(self):
        # each derived branch restricts a *distinct* base branch
        head = _Declaration(name="a")
        sub1 = _Declaration(name="a1")
        sub2 = _Declaration(name="a2")
        heads = {sub1: head, sub2: head}
        base = _group("choice", _elt(head), _elt(_Declaration(name="b")))
        derived = _group("choice", _elt(sub1), _elt(sub2))
        reasons = is_valid_particle_restriction(
            base, derived, _decls_resolver, head_lookup=heads.get
        )
        assert reasons and "Recurse" in reasons[0]


class TestRecurseUnorderedAllAll:
    """Recurse over all:all — order-insensitive with per-member sums."""

    def test_reordered_and_dropped_optional_is_valid(self):
        # all201: R all{d, b, c} over B all{a(0,5), b, c, d}
        base = _group(
            "all",
            _elt(_Declaration(name="a"), 0, 5),
            _elt(_Declaration(name="b"), 1, 5),
            _elt(_Declaration(name="c"), 2, None),
            _elt(_Declaration(name="d")),
        )
        derived = _group(
            "all",
            _elt(_Declaration(name="d")),
            _elt(_Declaration(name="b"), 3, 4),
            _elt(_Declaration(name="c"), 2, 4),
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_reordered_members_are_valid(self):
        # particlesS002 under 1.1: B has (a, b), R has (b, a)
        base = _group("all", _elt(_Declaration(name="a")), _elt(_Declaration(name="b")))
        derived = _group("all", _elt(_Declaration(name="b")), _elt(_Declaration(name="a")))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_required_base_particle_cannot_be_left_out(self):
        # all204 / particlesS005: a required base member may not be
        # dropped; the derived singleton all is pointless (§3.9.6
        # clause 2.2) so the pair is element-over-all and the
        # RecurseAsIfGroup wrapper reports the dropped member
        base = _group(
            "all",
            _elt(_Declaration(name="a"), 0, 5),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="d")),
        )
        derived = _group("all", _elt(_Declaration(name="b")))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "EltOverGroup" in reasons[0]

    def test_extra_member_is_invalid(self):
        # all205: a derived member with no base counterpart
        base = _group(
            "all",
            _elt(_Declaration(name="a"), 0, 5),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
        )
        derived = _group(
            "all",
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
            _elt(_Declaration(name="f"), 0, 1),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_member_occurrence_widening_is_invalid(self):
        # all202/all203: b(0,4) drops the base minimum; d(1,5) exceeds the base maximum
        base = _group(
            "all",
            _elt(_Declaration(name="a"), 0, 5),
            _elt(_Declaration(name="b"), 1, 5),
            _elt(_Declaration(name="d")),
        )
        derived = _group(
            "all",
            _elt(_Declaration(name="b"), 0, 4),
            _elt(_Declaration(name="d"), 1, 5),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_two_members_may_share_one_base_member(self):
        # all221: A1(6,8) + A2(6,8) both restrict base a(10,20); sums are contained
        head = _Declaration(name="a")
        sub1 = _Declaration(name="A1")
        sub2 = _Declaration(name="A2")
        heads = {sub1: head, sub2: head}
        base = _group(
            "all",
            _elt(head, 10, 20),
            _elt(_Declaration(name="b"), 0, 5),
        )
        derived = _group("all", _elt(sub1, 6, 8), _elt(sub2, 6, 8))
        assert not is_valid_particle_restriction(
            base, derived, _decls_resolver, head_lookup=heads.get
        )

    def test_shared_base_member_sum_minimum_is_checked(self):
        # all223: 3+3 < the base's 10 required occurrences
        head = _Declaration(name="a")
        sub1 = _Declaration(name="A1")
        sub2 = _Declaration(name="A2")
        heads = {sub1: head, sub2: head}
        base = _group(
            "all",
            _elt(head, 10, 20),
            _elt(_Declaration(name="b"), 0, 5),
            _elt(_Declaration(name="c"), 0, None),
            _elt(_Declaration(name="d"), 0, 1),
        )
        derived = _group("all", _elt(sub1, 3, 8), _elt(sub2, 3, 8))
        reasons = is_valid_particle_restriction(
            base, derived, _decls_resolver, head_lookup=heads.get
        )
        assert reasons and "Recurse" in reasons[0]

    def test_shared_base_member_sum_maximum_is_checked(self):
        # all224: 15+15 > the base's 20 maximum
        head = _Declaration(name="a")
        sub1 = _Declaration(name="A1")
        sub2 = _Declaration(name="A2")
        heads = {sub1: head, sub2: head}
        base = _group(
            "all",
            _elt(head, 10, 20),
            _elt(_Declaration(name="b"), 0, 5),
            _elt(_Declaration(name="c"), 0, None),
            _elt(_Declaration(name="d"), 0, 1),
        )
        derived = _group("all", _elt(sub1, 6, 15), _elt(sub2, 6, 15))
        reasons = is_valid_particle_restriction(
            base, derived, _decls_resolver, head_lookup=heads.get
        )
        assert reasons and "Recurse" in reasons[0]


class TestRecurseUnorderedWildcardSums:
    """Wildcard accounting in the unordered mapping (all228-244)."""

    def test_subset_wildcard_is_valid(self):
        # all228: {two} ⊆ {one two}
        base = _group("all", _wild("http://one.uri/ http://two.uri/"))
        derived = _group("all", _wild("http://two.uri/"))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_superset_wildcard_is_invalid(self):
        # all229: the singleton alls are pointless (§3.9.6 clause 2.2),
        # so the pair is wildcard-over-wildcard and the namespace
        # constraint must still narrow
        base = _group("all", _wild("http://two.uri/"))
        derived = _group("all", _wild("http://one.uri/ http://two.uri/"))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "NSSubset" in reasons[0]

    def test_one_base_wildcard_may_cover_two_derived(self):
        # all230: (1,1)+(1,1) ⊆ (1,5)
        base = _group("all", _wild("http://one.uri/ http://two.uri/", 1, 5))
        derived = _group("all", _wild("http://one.uri/", 1, 1), _wild("http://two.uri/", 1, 1))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_covered_wildcards_sum_maximum_is_checked(self):
        # all235: (1,3)+(1,3) ⊄ (1,5)
        base = _group("all", _wild("http://one.uri/ http://two.uri/", 1, 5))
        derived = _group("all", _wild("http://one.uri/", 1, 3), _wild("http://two.uri/", 1, 3))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_covered_wildcards_sum_minimum_is_checked(self):
        # all236: (2,unb)+(2,unb) does not reach the base's minimum 5
        base = _group("all", _wild("http://one.uri/ http://two.uri/", 5, None))
        derived = _group(
            "all",
            _wild("http://one.uri/", 2, None),
            _wild("http://two.uri/", 2, None),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_overlapping_wildcards_split_across_bases(self):
        # all237: three derived wildcards over two overlapping base wildcards
        base = _group(
            "all",
            _wild("http://one.uri/ http://two.uri/", 5, None),
            _wild("http://three.uri/", 0, 2),
        )
        derived = _group(
            "all",
            _wild("http://one.uri/", 3, None),
            _wild("http://two.uri/", 2, 2),
            _wild("http://three.uri/", 2, 2),
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_split_wildcard_may_starve_a_base_minimum(self):
        # all244: the split {two three} wildcard can put zero items in {one two},
        # so the base's required five are not always reachable
        base = _group(
            "all",
            _wild("http://one.uri/ http://two.uri/", 5, None),
            _wild("http://three.uri/", 0, 2),
        )
        derived = _group(
            "all",
            _wild("http://one.uri/", 3, None),
            _wild("http://two.uri/ http://three.uri/", 2, 2),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_split_wildcard_may_not_weaken_process_contents(self):
        # all238: the part of the split wildcard landing in the strict base
        # wildcard may not be lax
        base = _group(
            "all",
            _wild("http://one.uri/ http://two.uri/", 5, None, "strict"),
            _wild("http://three.uri/", 0, 2, "strict"),
        )
        derived = _group(
            "all",
            _wild("http://one.uri/", 3, None, "strict"),
            _wild("http://two.uri/ http://three.uri/", 2, 2, "lax"),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]


class TestRecurseUnorderedSequenceOverAll:
    """RecurseUnordered: a derived sequence restricting an all base."""

    def _base_all(self):
        return _group(
            "all",
            _elt(_Declaration(name="a"), 0, 5),
            _elt(_Declaration(name="b"), 1, 5),
            _elt(_Declaration(name="c"), 2, None),
            _elt(_Declaration(name="d")),
        )

    def test_reordered_sequence_is_valid(self):
        # all211 / particlesU003
        derived = _group(
            "sequence",
            _elt(_Declaration(name="d")),
            _elt(_Declaration(name="b"), 3, 4),
            _elt(_Declaration(name="c"), 2, 4),
        )
        assert not is_valid_particle_restriction(self._base_all(), derived, _decls_resolver)

    def test_duplicate_element_in_sequence_is_valid(self):
        # all216: 'a' appears twice in the derived sequence
        derived = _group(
            "sequence",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="d")),
            _elt(_Declaration(name="b"), 3, 4),
            _elt(_Declaration(name="c"), 2, 4),
            _elt(_Declaration(name="a")),
        )
        assert not is_valid_particle_restriction(self._base_all(), derived, _decls_resolver)

    def test_member_max_widening_is_invalid(self):
        # particlesU001: e2(1,2) over e2(1,1)
        base = _group("all", _elt(_Declaration(name="e1")), _elt(_Declaration(name="e2")))
        derived = _group(
            "sequence", _elt(_Declaration(name="e1")), _elt(_Declaration(name="e2"), 1, 2)
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_member_min_widening_is_invalid(self):
        # particlesU002: e1(2,2) over e1(1,1)
        base = _group("all", _elt(_Declaration(name="e1")), _elt(_Declaration(name="e2")))
        derived = _group(
            "sequence", _elt(_Declaration(name="e1"), 2, 2), _elt(_Declaration(name="e2"))
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_optional_base_absent_ok(self):
        # particlesU004: c is emptiable
        base = _group(
            "all",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c"), 0, 1),
        )
        derived = _group("sequence", _elt(_Declaration(name="b")), _elt(_Declaration(name="a")))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_required_base_particle_cannot_be_left_out(self):
        # particlesU006: c is NOT emptiable
        base = _group(
            "all",
            _elt(_Declaration(name="a")),
            _elt(_Declaration(name="b")),
            _elt(_Declaration(name="c")),
        )
        derived = _group("sequence", _elt(_Declaration(name="a")), _elt(_Declaration(name="b")))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_unknown_member_is_invalid(self):
        # particlesU008: e4 has no base counterpart
        base = _group(
            "all",
            _elt(_Declaration(name="e1")),
            _elt(_Declaration(name="e2"), 0, 1),
            _elt(_Declaration(name="e3")),
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="e4")),
            _elt(_Declaration(name="e2"), 0, 1),
            _elt(_Declaration(name="e3")),
            _elt(_Declaration(name="e1")),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_choice_member_restricts_through_its_branches(self):
        # all234: the choice's branches map individually onto the base members
        base = _group(
            "all",
            _elt(_Declaration(name="a"), 0, 5),
            _elt(_Declaration(name="b"), 1, 5),
            _elt(_Declaration(name="c"), 0, None),
            _elt(_Declaration(name="d"), 0, 1),
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="b"), 1, 3),
            _group(
                "choice",
                _elt(_Declaration(name="d")),
                _elt(_Declaration(name="c")),
                _elt(_Declaration(name="a")),
            ),
            _elt(_Declaration(name="b"), 1, 2),
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)


class TestChoiceOverAllBranches:
    """all:choice — every branch must restrict the all on its own."""

    def test_all_optional_base_with_full_choice_is_valid(self):
        # all231
        base = _group(
            "all",
            _elt(_Declaration(name="a"), 0, 5),
            _elt(_Declaration(name="b"), 0, 5),
            _elt(_Declaration(name="c"), 0, None),
            _elt(_Declaration(name="d"), 0, 1),
        )
        derived = _group(
            "choice",
            _elt(_Declaration(name="d")),
            _elt(_Declaration(name="b"), 3, 4),
            _elt(_Declaration(name="c"), 2, 4),
            _elt(_Declaration(name="a"), 0, 5),
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_sequence_branches_covering_the_required_member_are_valid(self):
        # all232
        base = _group(
            "all",
            _elt(_Declaration(name="a"), 0, 5),
            _elt(_Declaration(name="b"), 1, 5),
            _elt(_Declaration(name="c"), 0, None),
            _elt(_Declaration(name="d"), 0, 1),
        )
        derived = _group(
            "choice",
            _group("sequence", _elt(_Declaration(name="d")), _elt(_Declaration(name="b"), 3, 4)),
            _group("sequence", _elt(_Declaration(name="c")), _elt(_Declaration(name="b"), 3, 4)),
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_branch_exceeding_a_base_maximum_is_invalid(self):
        # all233: the third branch's a(1,8) exceeds a(0,5)
        base = _group(
            "all",
            _elt(_Declaration(name="a"), 0, 5),
            _elt(_Declaration(name="b"), 1, 5),
            _elt(_Declaration(name="c"), 0, None),
            _elt(_Declaration(name="d"), 0, 1),
        )
        derived = _group(
            "choice",
            _group("sequence", _elt(_Declaration(name="d")), _elt(_Declaration(name="b"), 3, 4)),
            _group("sequence", _elt(_Declaration(name="c")), _elt(_Declaration(name="b"), 3, 4)),
            _group(
                "sequence",
                _elt(_Declaration(name="a"), 1, 8),
                _elt(_Declaration(name="b"), 3, 4),
            ),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_branch_missing_a_required_member_is_invalid(self):
        # particlesHb009: neither branch can appear while satisfying the all
        base = _group("all", _elt(_Declaration(name="e1")), _elt(_Declaration(name="e2")))
        derived = _group("choice", _elt(_Declaration(name="e1")), _elt(_Declaration(name="e2")))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons

    def test_wildcard_branch_over_element_all_is_invalid(self):
        # particlesHb002: the branch reaches namespaces no base member admits
        base = _group(
            "all", _elt(_Declaration(name="e1"), 0, 1), _elt(_Declaration(name="e2"), 0, 1)
        )
        derived = _group("choice", _wild("##any"))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons


class TestSchemaRecurseCorpus:
    """Corpus-shaped schema tests for the Recurse family."""

    def test_all_all_required_member_left_out_is_invalid(self, parse):
        # all204/particlesS005 shape: the singleton derived all is
        # pointless, so element-over-all reports the dropped members
        report = parse(
            "<xs:complexType name='base'><xs:all>"
            "<xs:element name='a' minOccurs='0' maxOccurs='5'/>"
            "<xs:element name='b' minOccurs='1' maxOccurs='5'/>"
            "<xs:element name='d' minOccurs='1' maxOccurs='1'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:all>"
            "<xs:element name='b' minOccurs='2' maxOccurs='4'/>"
            "</xs:all></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "EltOverGroup" in issues[0].message

    def test_sequence_restricting_all_reordered_is_valid(self, parse):
        # all211 shape
        report = parse(
            "<xs:complexType name='base'><xs:all>"
            "<xs:element name='a' minOccurs='0' maxOccurs='5'/>"
            "<xs:element name='b' minOccurs='1' maxOccurs='5'/>"
            "<xs:element name='d' minOccurs='1' maxOccurs='1'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:element name='d' minOccurs='1' maxOccurs='1'/>"
            "<xs:element name='b' minOccurs='3' maxOccurs='4'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_sequence_required_member_left_out_is_invalid(self, parse):
        # particlesW010 shape
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:element name='a'/><xs:element name='b'/><xs:element name='c'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:element name='a'/><xs:element name='b'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "Recurse" in issues[0].message

    def test_choice_adding_branch_is_invalid(self, parse):
        # particlesT008 shape
        report = parse(
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='a'/><xs:element name='b'/><xs:element name='c'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:element name='a'/><xs:element name='b'/>"
            "<xs:element name='c'/><xs:element name='d'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "Recurse" in issues[0].message

    def test_choice_branch_exceeding_all_maximum_is_invalid(self, parse):
        # all233 shape
        report = parse(
            "<xs:complexType name='base'><xs:all>"
            "<xs:element name='a' minOccurs='0' maxOccurs='5'/>"
            "<xs:element name='b' minOccurs='1' maxOccurs='5'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:sequence><xs:element name='a' minOccurs='1' maxOccurs='8'/>"
            "<xs:element name='b' minOccurs='3' maxOccurs='4'/></xs:sequence>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "Recurse" in issues[0].message

    def test_equal_all_restriction_is_valid(self, parse):
        # particlesS001 shape
        report = parse(
            "<xs:complexType name='base'><xs:all>"
            "<xs:element name='a' minOccurs='0' maxOccurs='5'/>"
            "<xs:element name='b' minOccurs='1' maxOccurs='5'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:all>"
            "<xs:element name='b' minOccurs='2' maxOccurs='4'/>"
            "<xs:element name='a' minOccurs='0' maxOccurs='1'/>"
            "</xs:all></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)


class TestEltOverGroup:
    """RecurseAsIfGroup: an element restricting a choice/sequence/all base.

    The element is wrapped in a singleton group of the base's variety at
    1..1 and that wrapper is checked with the matching Recurse rule, so
    the base compositor's own occurrence range is compared against the
    wrapper while the member carries its own range (particlesL/K/M).
    """

    def test_element_over_choice_maps_one_member(self):
        # particlesL003/L006: B choice(1,1)[c1(1,2), c2], R c1(1,2)
        base = _group(
            "choice",
            _elt(_Declaration(name="c1"), 1, 2),
            _elt(_Declaration(name="c2")),
        )
        derived = _elt(_Declaration(name="c1"), 1, 2)
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_element_over_choice_below_base_minimum_is_invalid(self):
        # particlesL001: B choice(2,3), R c1(1,1) — the singleton wrapper
        # is 1..1 and cannot sit in [2,3]
        base = _group("choice", _elt(_Declaration(name="c1")), min_occurs=2, max_occurs=3)
        derived = _elt(_Declaration(name="c1"))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "EltOverGroup" in reasons[0]

    def test_element_over_choice_member_max_widening_is_invalid(self):
        # particlesL004/L008: member c1(1,1), R c1(1,2)
        base = _group("choice", _elt(_Declaration(name="c1")))
        derived = _elt(_Declaration(name="c1"), 1, 2)
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "occurrence range" in reasons[0]

    def test_element_over_choice_member_min_widening_is_invalid(self):
        # particlesL005: B choice(1,1)[c1(1,1)], R c1(0,1)
        base = _group("choice", _elt(_Declaration(name="c1")))
        derived = _elt(_Declaration(name="c1"), 0, 1)
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "occurrence range" in reasons[0]

    def test_element_over_all_optional_members_may_go_unmapped(self):
        # particlesK001: all(1,1)[a0(0), a1(1,1), a2(0)], R a1(1,1)
        base = _group(
            "all",
            _elt(_Declaration(name="a0"), 0, 1),
            _elt(_Declaration(name="a1")),
            _elt(_Declaration(name="a2"), 0, 1),
        )
        derived = _elt(_Declaration(name="a1"))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_element_over_all_member_widening_is_invalid(self):
        # particlesK004: R a1(2,2) against the member a1(1,1)
        base = _group(
            "all",
            _elt(_Declaration(name="a0"), 0, 1),
            _elt(_Declaration(name="a1")),
            _elt(_Declaration(name="a2"), 0, 1),
        )
        derived = _elt(_Declaration(name="a1"), 2, 2)
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "EltOverGroup" in reasons[0]

    def test_element_over_sequence_contained_member_is_valid(self):
        # particlesW014's contained frame (without the namespace mismatch)
        base = _group(
            "sequence",
            _elt(_Declaration(name="e1"), max_occurs=3),
            _elt(_Declaration(name="e2"), 0, 3),
            _elt(_Declaration(name="e3"), 0, 3),
        )
        derived = _elt(_Declaration(name="e1"))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_element_over_sequence_unmappable_member_is_invalid(self):
        # particlesW014: the imported ref's name matches no base member
        base = _group(
            "sequence",
            _elt(_Declaration(name="e1"), max_occurs=3),
            _elt(_Declaration(name="e2"), 0, 3),
            _elt(_Declaration(name="e3"), 0, 3),
        )
        derived = _elt(_Declaration(name="imported1"))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons

    def test_element_over_sequence_required_unmapped_member_is_invalid(self):
        # particlesJe012's shape: the base's required member has no
        # derived counterpart to absorb it
        base = _group(
            "sequence",
            _elt(_Declaration(name="e1")),
            _elt(_Declaration(name="e2")),
        )
        derived = _elt(_Declaration(name="e1"))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons

    def test_element_over_sequence_member_widening_is_invalid(self):
        # particlesM001: the base member's range does not contain R's
        base = _group(
            "sequence",
            _elt(_Declaration(name="c1")),
            _elt(_Declaration(name="c2"), 0, 1),
            min_occurs=2,
            max_occurs=3,
        )
        derived = _elt(_Declaration(name="c1"), 1, 3)
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons

    def test_element_over_choice_group_ref_member_matches_literal_member(self):
        # A group reference compiles to a synthetic singleton wrapper.
        # A choice member that is one must fold to the referenced choice
        # with the reference's occurrence, exactly like the literal
        # nested choice: base choice(2,3) over choice(a|b) admits a(2,2).
        literal = _group(
            "choice",
            _group("choice", _elt(_Declaration(name="a")), _elt(_Declaration(name="b"))),
            min_occurs=2,
            max_occurs=3,
        )
        referenced = _group(
            "choice",
            Particle(
                "sequence",
                1,
                1,
                [_group("choice", _elt(_Declaration(name="a")), _elt(_Declaration(name="b")))],
                synthetic=True,
            ),
            min_occurs=2,
            max_occurs=3,
        )
        derived = _elt(_Declaration(name="a"), 2, 2)
        assert not is_valid_particle_restriction(literal, derived, _decls_resolver)
        assert not is_valid_particle_restriction(referenced, derived, _decls_resolver)

    def test_vacuous_element_is_not_admitted_over_choice_or_sequence(self):
        # Vacuity is scoped: a maxOccurs=0 element is exempt from a
        # wildcard's namespace constraint and self-removes inside a
        # sequence alignment, but it still may not widen its way onto a
        # choice or sequence member (mgE006's derivation-pair posture).
        over_choice = _group("choice", _elt(_Declaration(name="e")), _elt(_Declaration(name="f")))
        over_sequence = _group("sequence", _elt(_Declaration(name="e")))
        derived = _elt(_Declaration(name="e"), 0, 0)
        assert is_valid_particle_restriction(over_choice, derived, _decls_resolver)
        assert is_valid_particle_restriction(over_sequence, derived, _decls_resolver)


class TestMapAndSum:
    """MapAndSum: a derived sequence over a base choice."""

    def test_contained_product_range_is_valid(self):
        # particlesV001: R seq(1,3) of three members has effective (3,9)
        base = _group(
            "choice",
            _elt(_Declaration(name="e1"), max_occurs=10),
            _elt(_Declaration(name="e2"), 2, 10),
            _elt(_Declaration(name="e3"), 3, 10),
            max_occurs=10,
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="e1"), max_occurs=10),
            _elt(_Declaration(name="e2"), 2, 10),
            _elt(_Declaration(name="e3"), 3, 10),
            max_occurs=3,
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_product_maximum_exceeding_base_is_invalid(self):
        # particlesV002/V005: R seq(1,99) x 3 members has effective max 297
        base = _group(
            "choice",
            _elt(_Declaration(name="e1"), max_occurs=10),
            _elt(_Declaration(name="e2"), 2, 10),
            _elt(_Declaration(name="e3"), 2, 10),
            max_occurs=99,
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="e1"), max_occurs=10),
            _elt(_Declaration(name="e2"), 2, 10),
            _elt(_Declaration(name="e3"), 2, 10),
            max_occurs=99,
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "MapAndSum" in reasons[0]

    def test_product_minimum_below_base_is_invalid(self):
        # the mirror of particlesV003's valid (4,8) in (3,9): a two-member
        # sequence at 1..1 has effective (2,2), below the base's minimum 3
        base = _group(
            "choice",
            _elt(_Declaration(name="e1")),
            _elt(_Declaration(name="e2")),
            min_occurs=3,
            max_occurs=9,
        )
        derived = _group("sequence", _elt(_Declaration(name="e1")), _elt(_Declaration(name="e2")))
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "MapAndSum" in reasons[0]

    def test_two_member_product_in_base_is_valid(self):
        # particlesV003: effective (4,8) in (3,9)
        base = _group(
            "choice",
            _elt(_Declaration(name="e1")),
            _elt(_Declaration(name="e2")),
            min_occurs=3,
            max_occurs=9,
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="e1")),
            _elt(_Declaration(name="e2")),
            min_occurs=2,
            max_occurs=4,
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_member_without_base_mapping_is_invalid(self):
        # particlesV016: R's extra e4 restricts no member of B
        base = _group(
            "choice",
            _elt(_Declaration(name="e1"), max_occurs=3),
            _elt(_Declaration(name="e2"), 0, 3),
            _elt(_Declaration(name="e3"), 0, 3),
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="e1")),
            _elt(_Declaration(name="e2"), 0, 1),
            _elt(_Declaration(name="e3"), 0, 1),
            _elt(_Declaration(name="e4"), 0, 1),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "MapAndSum" in reasons[0]

    def test_mapping_ignores_member_order(self):
        # particlesV015: R seq(e3, e2, e1) over B choice(e1|e2|e3)
        base = _group(
            "choice",
            _elt(_Declaration(name="e1")),
            _elt(_Declaration(name="e2")),
            _elt(_Declaration(name="e3")),
            min_occurs=0,
            max_occurs=3,
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="e3")),
            _elt(_Declaration(name="e2")),
            _elt(_Declaration(name="e1")),
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_member_type_mismatch_is_invalid(self):
        # particlesV018: e1 with an unrelated type restricts no member
        base = _group(
            "choice",
            _elt(_Declaration(name="e1", type_=_Restricted)),
            _elt(_Declaration(name="e2")),
            max_occurs=4,
        )
        derived = _group(
            "sequence",
            _elt(_Declaration(name="e1", type_=_Unrelated)),
            _elt(_Declaration(name="e2")),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "MapAndSum" in reasons[0]


class TestNSRecurseCheckCardinality:
    """A derived group over a wildcard base."""

    def test_effective_range_contained_is_valid(self):
        # particlesQ013: any(4,8) over seq(1,2)[e1(2,2), e2(2,2)] -> (4,8)
        base = _wild("##any", 4, 8)
        derived = _group(
            "sequence",
            _elt(_Declaration(name="e1"), 2, 2),
            _elt(_Declaration(name="e2"), 2, 2),
            min_occurs=1,
            max_occurs=2,
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_effective_range_maximum_exceeds_wildcard(self):
        # particlesQ006: any(0,3) over seq(0,4)[e1(1,1)] -> max 4
        base = _wild("##any", 0, 3)
        derived = _group("sequence", _elt(_Declaration(name="e1")), min_occurs=0, max_occurs=4)
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "NSRecurseCheckCardinality" in reasons[0]

    def test_choice_effective_minimum_below_wildcard_is_invalid(self):
        # particlesR010: any(4,4) over choice(1,1)[e1(3,3), e2(3,3)] -> min 3
        base = _wild("##any", 4, 4)
        derived = _group(
            "choice",
            _elt(_Declaration(name="e1"), 3, 3),
            _elt(_Declaration(name="e2"), 3, 3),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "NSRecurseCheckCardinality" in reasons[0]

    def test_choice_effective_range_in_wildcard_is_valid(self):
        # particlesR014's valid-side control: choice(2,2) -> (2,2) in (2,2)
        base = _wild("##any", 2, 2)
        derived = _group(
            "choice",
            _elt(_Declaration(name="e1")),
            _elt(_Declaration(name="e2")),
            min_occurs=2,
            max_occurs=2,
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_member_namespace_must_be_admitted(self):
        # particlesQ018/Jh001: ##other admits neither the absent namespace
        # nor the target namespace. The sequence keeps minOccurs=0 so the
        # singleton-wrapper elimination does not reduce it to Elt:Any.
        base = _wild("##other", 0, 1)
        derived = _group("sequence", _elt(_Declaration(name="elem")), min_occurs=0, max_occurs=1)
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "NSRecurseCheckCardinality" in reasons[0]

    def test_member_namespace_admitted_is_valid(self):
        base = _wild("##any", 0, 1)
        derived = _group("sequence", _elt(_Declaration(name="elem")))
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_vacuous_derived_group_is_not_reported(self):
        # a compositor with maxOccurs=0 matches nothing, so its members
        # stay unconstrained (the W006 posture)
        base = _wild("##local", 0, 0)
        derived = _group(
            "sequence",
            _elt(_Declaration(name="elem", namespace="http://t")),
            min_occurs=0,
            max_occurs=0,
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)


class TestGroupRefChoiceFlattening:
    """A derived choice branch that is a group-ref choice union."""

    def _group_ref(self, inner):
        """The synthetic singleton wrapper a group reference compiles to."""
        return Particle("sequence", children=[inner], synthetic=True)

    def test_unmappable_nested_branch_is_invalid(self):
        # particlesIb006: choice wrapping the group-ref choice(foo|bar)
        # against a base choice(foo|test); bar has no base branch
        base = _group("choice", _elt(_Declaration(name="foo")), _elt(_Declaration(name="test")))
        derived = _group(
            "choice",
            self._group_ref(
                _group(
                    "choice",
                    _elt(_Declaration(name="foo")),
                    _elt(_Declaration(name="bar")),
                ),
            ),
        )
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "Recurse" in reasons[0]

    def test_nested_branch_mapping_is_valid(self):
        # particlesIb005: the group-ref branch and bar both map
        base = _group(
            "choice",
            _elt(_Declaration(name="foo"), max_occurs=4),
            _elt(_Declaration(name="bar")),
        )
        derived = _group(
            "choice",
            self._group_ref(_group("choice", _elt(_Declaration(name="foo"), max_occurs=3))),
            _elt(_Declaration(name="bar")),
        )
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)


class _FormSchema:
    """A schema stand-in exposing the form defaults the predicate reads."""

    def __init__(self, element_default="unqualified"):
        self._element_default = element_default

    def getElementFormDefault(self):
        return self._element_default

    def getAttributeFormDefault(self):
        return self._element_default


class _FormDeclaration:
    """A declaration stand-in with global scope and form information."""

    def __init__(self, name, namespace, global_, form=None, schema_default="unqualified"):
        self.name = name
        self._namespace = namespace
        self._global = global_
        self.xsdElement = {} if form is None else {"form": form}
        self._schema = _FormSchema(schema_default)

    def getNamespace(self):
        return self._namespace

    def isGlobalDeclaration(self):
        return self._global

    def getSchema(self):
        return self._schema


def _form_elt(name, namespace, global_, form=None, schema_default="unqualified", **occurs):
    return Particle(
        "element",
        name=name,
        descriptor=_FormDeclaration(
            name, namespace, global_, form=form, schema_default=schema_default
        ),
        **occurs,
    )


class TestFormAwareElementNamespaces:
    """Local unqualified declarations carry the absent namespace.

    The {target namespace} of an unqualified local element declaration
    is absent, so it is a *different* expanded name from a global
    declaration of the same local name in the target namespace
    (particlesL010/L030).
    """

    def test_ref_global_versus_unqualified_local_is_a_name_mismatch(self):
        base = _form_elt("c1", "http://t", True)
        derived = _form_elt("c1", "http://t", False)
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "NameAndTypeOK" in reasons[0]

    def test_qualified_local_matches_ref_global(self):
        base = _form_elt("c1", "http://t", True)
        derived = _form_elt("c1", "http://t", False, schema_default="qualified")
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_explicit_form_attribute_wins(self):
        base = _form_elt("c1", "http://t", True)
        derived = _form_elt("c1", "http://t", False, form="qualified")
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_unqualified_local_is_admitted_by_local_wildcard(self):
        base = Particle("any", min_occurs=0, spec=WildcardSpec(namespace="##local"))
        derived = _form_elt("e1", "http://t", False)
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_vacuous_element_is_admitted_by_any_wildcard(self):
        # particlesJq010: R's element carries maxOccurs=0, so its
        # namespace is unconstrained
        base = Particle(
            "any",
            min_occurs=0,
            spec=WildcardSpec(namespace="##targetNamespace", target_namespace="http://t"),
        )
        derived = _form_elt("e1", "http://t", False, min_occurs=0, max_occurs=0)
        assert not is_valid_particle_restriction(base, derived, _decls_resolver)

    def test_non_vacuous_unqualified_element_is_not_admitted(self):
        base = Particle(
            "any",
            min_occurs=0,
            spec=WildcardSpec(namespace="##targetNamespace", target_namespace="http://t"),
        )
        derived = _form_elt("e1", "http://t", False, min_occurs=0, max_occurs=1)
        reasons = is_valid_particle_restriction(base, derived, _decls_resolver)
        assert reasons and "RecurseAsIfGroup" in reasons[0]


class TestSchemaCombinatorialCorpus:
    """Corpus-shaped schema tests for the 4d rules."""

    def test_element_over_choice_occurrence_widening_is_invalid(self, parse):
        # particlesL001 shape
        report = parse(
            "<xs:complexType name='B'><xs:sequence>"
            "<xs:choice minOccurs='2' maxOccurs='3'>"
            "<xs:element name='c1'/><xs:element name='c2'/></xs:choice>"
            "<xs:choice><xs:element name='d1'/><xs:element name='d2'/></xs:choice>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='B'><xs:sequence>"
            "<xs:element name='c1'/><xs:element name='d1'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "particle restriction" in issues[0].message

    def test_element_over_choice_contained_is_valid(self, parse):
        # particlesL003/L006 shape (valid control)
        report = parse(
            "<xs:complexType name='B'><xs:sequence>"
            "<xs:choice><xs:element name='c1' maxOccurs='2'/>"
            "<xs:element name='c2' maxOccurs='2'/></xs:choice>"
            "<xs:choice><xs:element name='d1' maxOccurs='2'/>"
            "<xs:element name='d2' maxOccurs='2'/></xs:choice>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='B'><xs:sequence>"
            "<xs:element name='c1' maxOccurs='2'/>"
            "<xs:element name='d1' maxOccurs='2'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_element_over_all_member_widening_is_invalid(self, parse):
        # particlesK004 shape
        report = parse(
            "<xs:complexType name='B'><xs:all>"
            "<xs:element name='a0' minOccurs='0'/>"
            "<xs:element name='a1' minOccurs='1' maxOccurs='1'/>"
            "<xs:element name='a2' minOccurs='0'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='B'><xs:sequence>"
            "<xs:element name='a1' minOccurs='2' maxOccurs='2'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "particle restriction" in issues[0].message

    def test_element_over_all_optional_members_is_valid(self, parse):
        # particlesK001 shape (valid control)
        report = parse(
            "<xs:complexType name='B'><xs:all>"
            "<xs:element name='a0' minOccurs='0'/>"
            "<xs:element name='a1' minOccurs='1' maxOccurs='1'/>"
            "<xs:element name='a2' minOccurs='0'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='B'><xs:sequence>"
            "<xs:element name='a1' minOccurs='1' maxOccurs='1'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_mapandsum_product_widening_is_invalid(self, parse):
        # particlesV005 shape
        report = parse(
            "<xs:complexType name='B'><xs:choice minOccurs='0' maxOccurs='2'>"
            "<xs:element name='e1' maxOccurs='3'/>"
            "<xs:element name='e2' maxOccurs='3'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='B'><xs:sequence maxOccurs='2'>"
            "<xs:element name='e1' maxOccurs='2'/>"
            "<xs:element name='e2' maxOccurs='2'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "MapAndSum" in issues[0].message

    def test_mapandsum_contained_product_is_valid(self, parse):
        # particlesV003 shape (valid control)
        report = parse(
            "<xs:complexType name='B'><xs:choice minOccurs='3' maxOccurs='9'>"
            "<xs:element name='e1'/><xs:element name='e2'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='B'><xs:sequence minOccurs='2' maxOccurs='4'>"
            "<xs:element name='e1'/><xs:element name='e2'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_nscard_effective_range_widening_is_invalid(self, parse):
        # particlesQ006 shape
        report = parse(
            "<xs:complexType name='B'><xs:sequence>"
            "<xs:any namespace='##any' minOccurs='0' maxOccurs='3'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='B'><xs:sequence>"
            "<xs:sequence minOccurs='0' maxOccurs='4'>"
            "<xs:element name='e1' minOccurs='1' maxOccurs='1'/>"
            "</xs:sequence></xs:sequence></xs:restriction>"
            "</xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "NSRecurseCheckCardinality" in issues[0].message

    def test_nscard_effective_range_contained_is_valid(self, parse):
        # particlesQ013 shape (valid control)
        report = parse(
            "<xs:complexType name='B'><xs:sequence>"
            "<xs:any namespace='##any' minOccurs='4' maxOccurs='8'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='B'><xs:sequence>"
            "<xs:sequence minOccurs='1' maxOccurs='2'>"
            "<xs:element name='e1' minOccurs='2' maxOccurs='2'/>"
            "<xs:element name='e2' minOccurs='2' maxOccurs='2'/>"
            "</xs:sequence></xs:sequence></xs:restriction>"
            "</xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_group_ref_choice_branch_must_map(self, parse):
        # particlesIb006 shape
        report = parse(
            "<xs:group name='G1'><xs:choice>"
            "<xs:element name='foo'/><xs:element name='bar'/>"
            "</xs:choice></xs:group>"
            "<xs:complexType name='base'><xs:choice>"
            "<xs:element name='foo'/><xs:element name='test'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='testing'><xs:complexContent>"
            "<xs:restriction base='base'><xs:choice>"
            "<xs:group ref='G1'/>"
            "</xs:choice></xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues and "Recurse" in issues[0].message

    def test_ref_global_versus_unqualified_local_is_invalid(self, parse):
        # particlesL010 shape: the ref to the global c1 and the base's
        # unqualified local c1 have different expanded names
        report = parse(
            "<xs:element name='c1'/>"
            "<xs:complexType name='B'><xs:sequence>"
            "<xs:choice><xs:element name='c1'/><xs:element name='c2'/></xs:choice>"
            "<xs:choice><xs:element name='d1'/><xs:element name='d2'/></xs:choice>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='x:B'><xs:sequence>"
            "<xs:element ref='x:c1'/><xs:element name='d1'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>",
            head=XSD_HEAD_NS,
        )
        issues = particle_restriction_issues(report)
        assert issues and "NameAndTypeOK" in issues[0].message

    def test_qualified_local_matches_ref_global(self, parse):
        # the valid side of the form boundary: with qualified locals the
        # ref and the local share the target namespace
        head = XSD_HEAD_NS.replace(
            "targetNamespace='http://xsdtesting'",
            "targetNamespace='http://xsdtesting' elementFormDefault='qualified'",
        )
        report = parse(
            "<xs:element name='c1'/>"
            "<xs:complexType name='B'><xs:choice><xs:element name='c1'/>"
            "<xs:element name='c2'/></xs:choice></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='x:B'><xs:choice>"
            "<xs:element ref='x:c1'/></xs:choice>"
            "</xs:restriction></xs:complexContent></xs:complexType>",
            head=head,
        )
        assert not particle_restriction_issues(report)

    def test_vacuous_element_over_target_namespace_wildcard_is_valid(self, parse):
        # particlesJq010 shape: maxOccurs=0 leaves the namespace free
        report = parse(
            "<xs:complexType name='B'><xs:sequence>"
            "<xs:any namespace='##targetNamespace' minOccurs='0'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='x:B'><xs:sequence>"
            "<xs:element name='e1' minOccurs='0' maxOccurs='0'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>",
            head=XSD_HEAD_NS,
        )
        assert not particle_restriction_issues(report)

    def test_edc_conflict_through_group_ref_is_invalid(self, parse):
        # mgR022 shape: same-name particles with different types, one
        # reached through a group reference, violate EDC
        report = parse(
            "<xs:element name='doc' type='foo'/>"
            "<xs:group name='group'>"
            "<xs:sequence><xs:element name='e1' type='xs:integer'/></xs:sequence>"
            "</xs:group>"
            "<xs:complexType name='foo'><xs:choice>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:group ref='group'/>"
            "</xs:choice></xs:complexType>"
        )
        assert any(issue.code == "all-rule" for issue in report.for_phase("schema"))


class TestComplexContentFromSimpleBase:
    """Deriving complex content from a simple-content base type.

    XSD 1.1 §3.4.6.2 clause 1.4 (extension) and §3.4.6.3 clause 2.2
    (restriction) only admit the shape when both sides carry the same
    simple content (1.4.1/2.2.2.1), both are empty (1.4.2/2.3.2), or the
    derived side is element-only/mixed over an element-only/mixed base
    (1.4.3/2.4.1). A complex-content derivation from a simple-content
    base satisfies none of them; XSD 1.0 admitted the extension shape,
    which is why particlesZ031 is a version split.
    """

    def test_extension_of_simple_content_is_invalid(self, parse):
        # particlesZ031: complexContent extension over simpleContent
        report = parse(
            "<xs:complexType name='Type1'><xs:simpleContent>"
            "<xs:extension base='xs:string'>"
            "<xs:attribute name='Field1' type='xs:string'/>"
            "</xs:extension></xs:simpleContent></xs:complexType>"
            "<xs:complexType name='Type2'><xs:complexContent>"
            "<xs:extension base='Type1'>"
            "<xs:attribute name='Field2' type='xs:string'/>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues

    def test_empty_sequence_restriction_of_simple_content_is_invalid(self, parse):
        # particlesZ039: an empty sequence is empty complex content,
        # which cannot restrict simple content
        report = parse(
            "<xs:complexType name='cTypeBase'><xs:simpleContent>"
            "<xs:extension base='xs:string'>"
            "<xs:attribute name='attr1' type='xs:string'/>"
            "</xs:extension></xs:simpleContent></xs:complexType>"
            "<xs:complexType name='cTypeDerived'><xs:complexContent>"
            "<xs:restriction base='cTypeBase'><xs:sequence/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        issues = particle_restriction_issues(report)
        assert issues

    def test_simple_content_restriction_of_simple_content_is_valid(self, parse):
        # the valid control: the same simple type restricted by facets
        report = parse(
            "<xs:complexType name='B'><xs:simpleContent>"
            "<xs:extension base='xs:string'>"
            "<xs:attribute name='a' type='xs:string'/>"
            "</xs:extension></xs:simpleContent></xs:complexType>"
            "<xs:complexType name='R'><xs:simpleContent>"
            "<xs:restriction base='B'>"
            "<xs:simpleType><xs:restriction base='xs:string'>"
            "<xs:maxLength value='4'/>"
            "</xs:restriction></xs:simpleType>"
            "</xs:restriction></xs:simpleContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)

    def test_complex_content_restriction_of_complex_base_is_valid(self, parse):
        # the other control: explicit complex content over a complex base
        report = parse(
            "<xs:complexType name='B'><xs:sequence>"
            "<xs:element name='e1' minOccurs='0'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent>"
            "<xs:restriction base='B'><xs:sequence>"
            "<xs:element name='e1' minOccurs='0'/></xs:sequence>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)


class TestPointlessRulesTopLevelGuards:
    """Valid corpus shapes the top-level pointless rules must not break."""

    def test_singleton_sequence_around_group_ref_stays_valid(self, parse):
        # groupB003v: the group-vs-group alignment must survive the
        # pointless elimination — the singleton literal sequence around
        # the group reference is not flattened before the alignment
        report = parse(
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:group ref='g1'/><xs:group ref='g2' minOccurs='0'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:group name='g1'><xs:sequence>"
            "<xs:element name='r1'/><xs:element name='r2'/></xs:sequence></xs:group>"
            "<xs:group name='g2'><xs:sequence>"
            "<xs:element name='r3'/><xs:element name='r4'/></xs:sequence></xs:group>"
            "<xs:element name='elem'><xs:complexType><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence>"
            "<xs:group ref='g1'/></xs:sequence>"
            "</xs:restriction></xs:complexContent></xs:complexType></xs:element>"
        )
        assert not particle_restriction_issues(report)

    def test_choice_of_substitution_members_over_head_is_valid(self, parse):
        # particlesZ027a: the head particle expands to a choice of its
        # members (clause 2.1), so the member choice is a RecurseLax match
        report = parse(
            "<xs:element name='head'/>"
            "<xs:element name='m1' substitutionGroup='head'/>"
            "<xs:element name='m2' substitutionGroup='head'/>"
            "<xs:complexType name='base'><xs:sequence>"
            "<xs:element ref='head'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='derived'><xs:complexContent>"
            "<xs:restriction base='base'><xs:sequence><xs:choice>"
            "<xs:element ref='m1'/><xs:element ref='m2'/>"
            "</xs:choice></xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not particle_restriction_issues(report)
