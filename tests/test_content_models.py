"""Content-model legality: ``xs:all`` compositor and particle occurrence checks.

Schema-phase checks behind the ``all-rule`` code (all compositor legality,
duplicate/conflicting element particles under ``all``) and the compositor
occurrence checks on ``sequence``/``choice``/``group`` particles. Corpus
shapes are condensed from the XSTS model-groups clusters (mgA/mgB/mgC/mgO/
mgP/mgQ/mgR), the SUN MGroup ``all`` cases and the Saxon ``All`` suite.
"""

import io

import pytest

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD
from pyxsd.validation import IssueSeverity

XSD_HEAD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
XSD_TAIL = "</xs:schema>"


def schema_codes(report) -> set[str]:
    return {issue.code for issue in report.for_phase("schema")}


def all_rule_issues(report) -> list:
    return [
        issue
        for issue in report.for_phase("schema")
        if issue.code == "all-rule" and issue.severity is IssueSeverity.ERROR
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


class TestAllGroupLegality:
    def test_all_max_occurs_gt_one_is_invalid(self, parse):
        # mgAb: all with maxOccurs > 1
        report = parse(
            "<xs:complexType name='t'><xs:all maxOccurs='2'>"
            "<xs:element name='e'/></xs:all></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_all_min_occurs_gt_one_is_invalid(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:all minOccurs='2'>"
            "<xs:element name='e'/></xs:all></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_child_of_all_with_max_occurs_gt_one_is_valid(self, parse):
        # Brief listed the element-child maxOccurs>1 shape under the XSD 1.0
        # all rules ("see 1.1 note"); XSD 1.1 relaxes it — Saxon all001/all003
        # pin element particles of an all with maxOccurs>1 as VALID under the
        # xsd11 profile, so the 1.0 rule is deliberately not enforced. The
        # particlesEa carrier shape (a group reference repeating an all) is
        # still invalid — see TestGroupReferenceLegality.
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='e' maxOccurs='2'/></xs:all>"
            "</xs:complexType>"
        )
        assert "all-rule" not in schema_codes(report)

    def test_duplicate_element_names_in_all_is_invalid(self, parse):
        # mgR001: same name, different types, under all
        report = parse(
            "<xs:complexType name='foo'><xs:all>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:element name='e1' type='bar'/></xs:all></xs:complexType>"
            "<xs:complexType name='bar'><xs:sequence>"
            "<xs:choice><xs:choice><xs:element name='e1' type='xs:string'/>"
            "</xs:choice></xs:choice></xs:sequence></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_duplicate_identical_declarations_in_all_is_invalid(self, parse):
        # mgQ: same name, same type, twice under all — UPA violation
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:element name='e1' type='xs:string'/></xs:all>"
            "</xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_all_name_attribute_is_invalid(self, parse):
        # mgA013: all carries a name attribute
        report = parse(
            "<xs:complexType name='t'><xs:all name='foo'>"
            "<xs:element name='e'/></xs:all></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_all_nested_in_sequence_is_invalid(self, parse):
        # mgA019: all cannot be nested under a compositor
        report = parse(
            "<xs:complexType name='t'><xs:sequence><xs:all>"
            "<xs:element name='e'/></xs:all></xs:sequence></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_all_nested_in_choice_is_invalid(self, parse):
        # mgA018
        report = parse(
            "<xs:complexType name='t'><xs:choice><xs:all>"
            "<xs:element name='e'/></xs:all></xs:choice></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_all_simple_type_child(self, parse):
        # mgB008: child node other than annotation or element
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:simpleType name='foo'><xs:restriction base='xs:integer'/>"
            "</xs:simpleType></xs:all></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_sequence_child_of_all_is_invalid(self, parse):
        # SUN MGroup particles00103m1: the particles of all must be element
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:sequence><xs:element name='e'/></xs:sequence>"
            "</xs:all></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_group_definition_child_of_all_is_invalid(self, parse):
        # mgB009: a group *definition* is not a particle of all
        report = parse(
            "<xs:complexType name='t'><xs:all><xs:group name='foo'/></xs:all></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_group_ref_to_sequence_group_inside_all_is_invalid(self, parse):
        # Saxon all008.n: a group reference in all must name an all group
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='a'/>"
            "<xs:group ref='allgroup'/></xs:all></xs:complexType>"
            "<xs:group name='allgroup'><xs:sequence>"
            "<xs:element name='b'/></xs:sequence></xs:group>"
        )
        assert "all-rule" in schema_codes(report)

    def test_group_ref_with_relaxed_occurrence_inside_all_is_invalid(self, parse):
        # Saxon all009.n: the reference itself must have minOccurs=maxOccurs=1
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='a'/>"
            "<xs:group ref='allgroup' minOccurs='0'/></xs:all></xs:complexType>"
            "<xs:group name='allgroup'><xs:all>"
            "<xs:element name='b'/></xs:all></xs:group>"
        )
        assert "all-rule" in schema_codes(report)

    def test_group_ref_to_all_group_inside_all_is_valid(self, parse):
        # Saxon all007: a group reference to an all group is legal (1.1)
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='a'/>"
            "<xs:group ref='allgroup'/></xs:all></xs:complexType>"
            "<xs:group name='allgroup'><xs:all>"
            "<xs:element name='b'/></xs:all></xs:group>"
        )
        assert "all-rule" not in schema_codes(report)

    def test_duplicate_conflict_is_reported_once_per_name(self, parse):
        # mgQ with three copies of one name: each conflicting name is
        # reported once, not once per pair.
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:element name='e1' type='xs:string'/></xs:all>"
            "</xs:complexType>"
        )
        assert len(all_rule_issues(report)) == 1

    def test_wildcard_overlap_in_all_is_invalid(self, parse):
        # Saxon all243.n: two overlapping wildcards under all
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:any namespace='http://one.uri/ http://two.uri/'/>"
            "<xs:any namespace='http://two.uri/ http://three.uri/'/>"
            "</xs:all></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_disjoint_wildcards_in_all(self, parse):
        # Saxon all005: two disjoint-namespace wildcards under all (1.1)
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:any namespace='http://a.ns/'/>"
            "<xs:any namespace='http://b.ns/'/>"
            "</xs:all></xs:complexType>"
        )
        assert "all-rule" not in schema_codes(report)

    def test_substitution_group_overlap_in_all_is_invalid(self, parse):
        # Saxon all241.n: one element in the substitution group of another
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='o' type='xs:integer'/>"
            "<xs:element name='p' type='xs:boolean' substitutionGroup='o'/>"
            "</xs:all></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    # -- shapes the corpus pins as VALID (guards against over-reporting) --

    def test_empty_all_is_valid(self, parse):
        # mgB001: an all with no particles is a legal (empty) content model
        report = parse("<xs:complexType name='t'><xs:all/></xs:complexType>")
        assert "all-rule" not in schema_codes(report)

    def test_all_with_min_zero_max_one_is_valid(self, parse):
        # mgO030 / mgAa003
        report = parse(
            "<xs:complexType name='t'><xs:all minOccurs='0' maxOccurs='1'>"
            "<xs:element name='e'/></xs:all></xs:complexType>"
        )
        assert "all-rule" not in schema_codes(report)

    def test_emptiable_all_under_complex_type_is_valid(self, parse):
        # mgO001/mgO018: valid@1.1 — an emptiable all (min=max=0) is a
        # legal complex type content model
        report = parse(
            "<xs:complexType name='t'><xs:all minOccurs='0' maxOccurs='0'>"
            "<xs:element name='e'/></xs:all></xs:complexType>"
        )
        assert "all-rule" not in schema_codes(report)

    def test_emptiable_all_inside_group_is_invalid(self, parse):
        # mgO019 (unversioned invalid): the emptiable form is not accepted
        # inside a group definition
        report = parse(
            "<xs:group name='g'><xs:all maxOccurs='0' minOccurs='0'>"
            "<xs:element name='e'/></xs:all></xs:group>"
        )
        assert "all-rule" in schema_codes(report)

    def test_all_max_occurs_zero_with_default_min_is_invalid(self, parse):
        # mgC009: minOccurs defaults to 1, so max=0 is contradictory
        report = parse(
            "<xs:complexType name='t'><xs:all maxOccurs='0'>"
            "<xs:element name='e'/></xs:all></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_relaxed_element_occurrences_in_all_are_valid(self, parse):
        # Saxon all001: under XSD 1.1 element particles of an all may
        # carry relaxed minOccurs/maxOccurs.
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='a' minOccurs='0' maxOccurs='5'/>"
            "<xs:element name='b' minOccurs='2' maxOccurs='unbounded'/>"
            "</xs:all></xs:complexType>"
        )
        assert "all-rule" not in schema_codes(report)

    def test_all_under_extension_is_valid(self, parse):
        # mgO028 / Saxon all301: an all as an extension suffix is legal
        report = parse(
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='e' minOccurs='0' maxOccurs='2'/>"
            "</xs:all></xs:extension></xs:complexContent></xs:complexType>"
            "<xs:complexType name='b'/>"
        )
        assert "all-rule" not in schema_codes(report)


class TestDuplicateDetectionBeyondAll:
    def test_different_type_duplicates_in_sequence_is_invalid(self, parse):
        # mgR002: EDC applies to any compositor, not just all
        report = parse(
            "<xs:complexType name='t'><xs:sequence>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:element name='e1' type='xs:integer'/></xs:sequence>"
            "</xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_different_type_duplicates_in_nested_compositors_is_invalid(self, parse):
        # mgR006: the conflict is transitive through nested compositors
        report = parse(
            "<xs:complexType name='t'><xs:sequence>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:choice><xs:element name='e1' type='xs:integer'/></xs:choice>"
            "</xs:sequence></xs:complexType>"
        )
        assert "all-rule" in schema_codes(report)

    def test_identical_duplicates_in_sequence_are_valid(self, parse):
        # mgQ002: identical declarations under sequence are deterministic
        report = parse(
            "<xs:complexType name='t'><xs:sequence>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:element name='e1' type='xs:string'/></xs:sequence>"
            "</xs:complexType>"
        )
        assert "all-rule" not in schema_codes(report)


class TestCompositorOccurrenceLegality:
    def test_sequence_min_occurs_gt_max_occurs_is_invalid(self, parse):
        # mgG027
        report = parse(
            "<xs:complexType name='t'><xs:sequence minOccurs='2' maxOccurs='1'>"
            "<xs:element name='e'/></xs:sequence></xs:complexType>"
        )
        assert "declaration-attribute" in schema_codes(report)

    def test_sequence_min_occurs_lexical_is_invalid(self, parse):
        # mgEa001 / mgEa003
        report = parse(
            "<xs:complexType name='t'><xs:sequence minOccurs='*'>"
            "<xs:element name='e'/></xs:sequence></xs:complexType>"
        )
        assert "invalid-occurs" in schema_codes(report)

    def test_sequence_max_occurs_lexical_is_invalid(self, parse):
        # mgEb006
        report = parse(
            "<xs:complexType name='t'><xs:sequence maxOccurs='a'>"
            "<xs:element name='e'/></xs:sequence></xs:complexType>"
        )
        assert "invalid-occurs" in schema_codes(report)

    def test_sequence_min_occurs_one_max_occurs_zero_is_invalid(self, parse):
        # mgG028
        report = parse(
            "<xs:complexType name='t'><xs:sequence minOccurs='1' maxOccurs='0'>"
            "<xs:element name='e'/></xs:sequence></xs:complexType>"
        )
        assert "declaration-attribute" in schema_codes(report)

    def test_choice_min_occurs_gt_max_occurs_is_invalid(self, parse):
        # mgJ027
        report = parse(
            "<xs:complexType name='t'><xs:choice minOccurs='2' maxOccurs='1'>"
            "<xs:element name='e'/></xs:choice></xs:complexType>"
        )
        assert "declaration-attribute" in schema_codes(report)

    def test_choice_min_occurs_lexical_is_invalid(self, parse):
        # mgHa001
        report = parse(
            "<xs:complexType name='t'><xs:choice minOccurs=''>"
            "<xs:element name='e'/></xs:choice></xs:complexType>"
        )
        assert "invalid-occurs" in schema_codes(report)

    def test_sequence_relaxed_occurrences_are_valid(self, parse):
        # mgG026: maxOccurs=unbounded is legal on a sequence
        report = parse(
            "<xs:complexType name='t'><xs:sequence maxOccurs='unbounded'>"
            "<xs:element name='e'/></xs:sequence></xs:complexType>"
        )
        assert "invalid-occurs" not in schema_codes(report)
        assert "declaration-attribute" not in schema_codes(report)


class TestGroupReferenceLegality:
    def test_group_ref_min_occurs_gt_max_occurs_is_invalid(self, parse):
        # groupN022: parent is complexType: minOccurs=2, maxOccurs=1
        report = parse(
            "<xs:complexType name='t'><xs:group ref='g' minOccurs='2' maxOccurs='1'/>"
            "</xs:complexType>"
            "<xs:group name='g'><xs:sequence><xs:element name='e'/></xs:sequence></xs:group>"
        )
        assert "declaration-attribute" in schema_codes(report)

    def test_group_ref_min_occurs_one_max_occurs_zero_is_invalid(self, parse):
        # groupL023 family
        report = parse(
            "<xs:complexType name='t'><xs:group ref='g' minOccurs='1' maxOccurs='0'/>"
            "</xs:complexType>"
            "<xs:group name='g'><xs:sequence><xs:element name='e'/></xs:sequence></xs:group>"
        )
        assert "declaration-attribute" in schema_codes(report)

    def test_group_ref_has_name(self, parse):
        # groupC004: name is only allowed at top level
        report = parse(
            "<xs:complexType name='t'><xs:group ref='g' name='nope'/>"
            "</xs:complexType>"
            "<xs:group name='g'><xs:sequence><xs:element name='e'/></xs:sequence></xs:group>"
        )
        assert "declaration-attribute" in schema_codes(report)

    def test_group_ref_occurrence_lexical_is_invalid(self, parse):
        # groupA-family lexical garbage on a reference site
        report = parse(
            "<xs:complexType name='t'><xs:group ref='g' minOccurs='?'/>"
            "</xs:complexType>"
            "<xs:group name='g'><xs:sequence><xs:element name='e'/></xs:sequence></xs:group>"
        )
        assert "invalid-occurs" in schema_codes(report)

    def test_group_ref_to_all_group_with_max_occurs_two_is_invalid(self, parse):
        # particlesEa025: a particle carrying an all term must have
        # minOccurs=maxOccurs=1
        report = parse(
            "<xs:complexType name='t'><xs:group ref='g' minOccurs='1' maxOccurs='2'/>"
            "</xs:complexType>"
            "<xs:group name='g'><xs:all><xs:element name='e'/></xs:all></xs:group>"
        )
        assert "all-rule" in schema_codes(report)

    def test_group_ref_to_all_group_at_type_root_is_valid(self, parse):
        # mgO005: a group reference whose content is an all may sit at the
        # root of a complexType with minOccurs=maxOccurs=1
        report = parse(
            "<xs:complexType name='t'><xs:group ref='g' minOccurs='1' maxOccurs='1'/>"
            "</xs:complexType>"
            "<xs:group name='g'><xs:all><xs:element name='e'/></xs:all></xs:group>"
        )
        assert "all-rule" not in schema_codes(report)

    def test_optional_group_ref_to_all_group_at_type_root_is_valid(self, parse):
        # particlesEa022: minOccurs=0 is legal on a reference carrying an
        # all term at the root of a complex type
        report = parse(
            "<xs:complexType name='t'><xs:group ref='g' minOccurs='0'/>"
            "</xs:complexType>"
            "<xs:group name='g'><xs:all><xs:element name='e'/></xs:all></xs:group>"
        )
        assert "all-rule" not in schema_codes(report)

    def test_group_with_all_referenced_from_sequence_is_invalid(self, parse):
        # mgA020: all can only be the root of a content model — reaching it
        # through a group reference inside a sequence is illegal
        report = parse(
            "<xs:complexType name='t'><xs:sequence>"
            "<xs:element name='a'/>"
            "<xs:group ref='g'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:group name='g'><xs:all><xs:element name='b'/></xs:all></xs:group>"
        )
        assert "all-rule" in schema_codes(report)

    def test_group_definition_name_must_be_ncname(self, parse):
        # groupA010/groupA012: name='1' and name='a:b'
        report = parse(
            "<xs:group name='1'><xs:sequence><xs:element name='e'/></xs:sequence></xs:group>"
        )
        assert "declaration-attribute" in schema_codes(report)

    def test_occurrence_attribute_on_global_group_is_invalid(self, parse):
        # groupD001: parent is schema can't have minOccurs
        report = parse(
            "<xs:group name='g' minOccurs='1'>"
            "<xs:sequence><xs:element name='e'/></xs:sequence></xs:group>"
        )
        assert "declaration-attribute" in schema_codes(report)

    def test_group_ref_under_any_is_invalid(self, parse):
        # groupO026: a group reference cannot appear inside xs:any
        report = parse(
            "<xs:group name='g'><xs:sequence>"
            "<xs:any namespace='##any'><xs:group ref='h'/></xs:any>"
            "</xs:sequence></xs:group>"
            "<xs:group name='h'><xs:sequence><xs:element name='e'/></xs:sequence></xs:group>"
        )
        assert "misplaced-declaration" in schema_codes(report)


class TestPointlessParticles:
    def test_empty_choice_under_optional_group_ref_is_invalid(self, parse):
        # particlesHa008 (verbatim shape, condensed)
        report = parse(
            "<xs:group name='P'><xs:sequence>"
            "<xs:group ref='x:Q' minOccurs='0'/></xs:sequence></xs:group>"
            "<xs:group name='Q'><xs:choice minOccurs='0'/></xs:group>"
        )
        assert "pointless-particle" in schema_codes(report)

    def test_emptiable_choice_is_fine_when_not_optional(self, parse):
        # valid control: the same group without minOccurs=0 must stay clean
        report = parse(
            "<xs:group name='P'><xs:sequence>"
            "<xs:group ref='x:Q'/></xs:sequence></xs:group>"
            "<xs:group name='Q'><xs:choice minOccurs='0'/></xs:group>"
        )
        assert "pointless-particle" not in schema_codes(report)

    def test_choice_with_vacuous_child_in_optional_group_ref_is_valid(self, parse):
        # groupL007: a choice (minOccurs=0) whose child is a group
        # reference with maxOccurs=0 can still be eliminated without
        # changing the language, but MS pins the schema VALID — only a
        # compositor with *no* particle children is pointless.
        report = parse(
            "<xs:group name='A'><xs:sequence>"
            "<xs:choice minOccurs='0'>"
            "<xs:group ref='B' minOccurs='0' maxOccurs='0'/>"
            "</xs:choice></xs:sequence></xs:group>"
            "<xs:group name='B'><xs:choice>"
            "<xs:element name='b1'/><xs:element name='b2'/>"
            "</xs:choice></xs:group>"
            "<xs:element name='elem'><xs:complexType>"
            "<xs:group ref='A' minOccurs='0'/>"
            "</xs:complexType></xs:element>"
        )
        assert "pointless-particle" not in schema_codes(report)

    def test_compositor_with_optional_children_in_optional_group_ref_is_valid(self, parse):
        # ECMA-376 shape: every child optional makes the compositor
        # emptiable, but it can still match content — eliminating it
        # would change the language, so it is not pointless.
        report = parse(
            "<xs:group name='g'><xs:sequence>"
            "<xs:element name='a' minOccurs='0'/>"
            "<xs:element name='b' minOccurs='0'/>"
            "</xs:sequence></xs:group>"
            "<xs:element name='doc'><xs:complexType>"
            "<xs:group ref='g' minOccurs='0'/>"
            "</xs:complexType></xs:element>"
        )
        assert "pointless-particle" not in schema_codes(report)

    def test_empty_choice_with_default_min_occurs_is_not_reported(self, parse):
        # an empty choice with minOccurs=1 is unsatisfiable rather than
        # eliminable — not the pointless-particle shape
        report = parse(
            "<xs:group name='P'><xs:sequence>"
            "<xs:group ref='x:Q' minOccurs='0'/></xs:sequence></xs:group>"
            "<xs:group name='Q'><xs:choice/></xs:group>"
        )
        assert "pointless-particle" not in schema_codes(report)
