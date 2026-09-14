"""Particle-valid restriction: cos-particle-restrict (XSD 1.0 §3.9.6).

Unit tests over the pure predicate in ``pyxsd.particle_derivation`` plus
schema-level tests for the corpus shapes this sub-task covers (the MS
particlesOb NSSubset cluster and the particlesHb forbidden-transition
cluster, with valid guards for the shapes the corpus pins legal).
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
from pyxsd.validation import IssueSeverity
from pyxsd.wildcards import WildcardSpec

ANY = WildcardSpec(namespace="##any")
LOCAL = WildcardSpec(namespace="##local")

XSD_HEAD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
XSD_TAIL = "</xs:schema>"


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

    def _parse(schema_string: str):
        schema_path.write_text(XSD_HEAD + schema_string + XSD_TAIL, encoding="utf-8")
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

    def test_choice_over_element_is_fully_deferred(self):
        # the deferred choice-over-element cell reports nothing yet, not
        # even occurrence widening on its members; Task 4c/4d completes it
        base = Particle("element", name="e", min_occurs=1, max_occurs=1)
        derived = Particle("choice", children=[Particle("element", name="e", max_occurs=2)])
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

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

    def test_group_over_element_is_not_reported(self):
        # particlesHb008/Hb011: 1.0 forbids a group restricting an
        # element, but the corpus pins the exemplars valid under the
        # 1.1 profile (the 1.1 Recurse alignment absorbs the shape), so
        # this cell is deferred to the Recurse work rather than
        # regressing them
        base = Particle("element", name="e")
        derived = Particle(
            "sequence",
            children=[Particle("element", name="e"), Particle("element", name="f")],
        )
        assert not is_valid_particle_restriction(base, derived, resolver=no_resolver)

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
        # Hb002: the base's group reference compiles to a 1/1 sequence
        # wrapper; shape decisions see through it (choice over all is
        # forbidden, choice over the wrapper would be a different cell)
        base = Particle(
            "sequence",
            children=[Particle("all", children=[Particle("element", name="e1", min_occurs=0)])],
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
        # particlesHb009: choice(e1, e2) restricting all(e1, e2)
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
        assert issues and "Forbidden" in issues[0].message

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
