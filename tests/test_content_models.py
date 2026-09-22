"""Content-model legality: ``xs:all`` compositor and particle occurrence checks.

Schema-phase checks behind the ``all-rule`` code (all compositor legality,
duplicate/conflicting element particles under ``all``) and the compositor
occurrence checks on ``sequence``/``choice``/``group`` particles. Corpus
shapes are condensed from the XSTS model-groups clusters (mgA/mgB/mgC/mgO/
mgP/mgQ/mgR), the SUN MGroup ``all`` cases and the Saxon ``All`` suite.
"""

import pytest

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema
from pyxsd.validation import IssueSeverity
from pyxsd.wildcards import (
    WildcardSpec,
    effective_attribute_wildcard,
    intersect_wildcard_specs,
    union_wildcard_specs,
    wildcard_spec,
)

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
def parse(tmp_path):
    """Compile a schema fragment (wrapped in an ``xs:schema`` root) and
    return its schema-phase report.
    """
    schema_path = tmp_path / "schema.xsd"

    def _parse(schema_string: str, xsd_version: str = "1.1"):
        schema_path.write_text(XSD_HEAD + schema_string + XSD_TAIL, encoding="utf-8")
        return Schema.compile(
            str(schema_path), mode=ParseModes.NAMESPACED, xsd_version=xsd_version
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

    def test_child_of_all_max_occurs_gt_one_invalid_in_10(self, parse):
        # The same shape is a 1.0 violation: each element particle of an
        # all is capped at a single occurrence.
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='e' maxOccurs='2'/></xs:all>"
            "</xs:complexType>",
            "1.0",
        )
        assert "all-rule" in schema_codes(report)

    def test_child_of_all_min_occurs_gt_one_invalid_in_10(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='e' minOccurs='2'/></xs:all>"
            "</xs:complexType>",
            "1.0",
        )
        assert "all-rule" in schema_codes(report)

    def test_child_of_all_unit_occurs_valid_in_10(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='e' minOccurs='0' maxOccurs='1'/></xs:all>"
            "</xs:complexType>",
            "1.0",
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


class TestExtensionStructure:
    """Extension derivation structure (cos-ct-extends / cos-particle-extend).

    The explicit content of an extension is appended to the base type's
    effective content model. ``all`` may extend only ``all`` (XSD 1.1's
    relaxation; the 1.0 forbidden shapes stay forbidden merely as
    ``particle-restriction``), ``all``-extends-``all`` requires the two
    ``minOccurs`` to match, and the composed ``all`` must be
    unambiguous (Saxon all301-314, particlesFb002).
    """

    def test_all_extends_sequence_is_invalid(self, parse):
        # Saxon all309/all312: all cannot extend a sequence base
        report = parse(
            "<xs:complexType name='b'><xs:sequence>"
            "<xs:element name='a'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='d'/></xs:all>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" in schema_codes(report)

    def test_singleton_all_extends_sequence_is_invalid(self, parse):
        # all312: even a singleton all is not a sequence
        report = parse(
            "<xs:complexType name='b'><xs:sequence>"
            "<xs:element name='a'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='d' minOccurs='0' maxOccurs='2'/>"
            "</xs:all></xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" in schema_codes(report)

    def test_sequence_extends_all_is_invalid(self, parse):
        # Saxon all310: a sequence suffix cannot extend an all base
        report = parse(
            "<xs:complexType name='b'><xs:all>"
            "<xs:element name='a'/></xs:all></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:sequence>"
            "<xs:element name='d'/></xs:sequence>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" in schema_codes(report)

    def test_singleton_sequence_extends_singleton_all_is_invalid(self, parse):
        # all311: the singleton shapes are still invalid
        report = parse(
            "<xs:complexType name='b'><xs:all>"
            "<xs:element name='a'/></xs:all></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:sequence>"
            "<xs:element name='d' minOccurs='0' maxOccurs='2'/>"
            "</xs:sequence></xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" in schema_codes(report)

    def test_all_extends_choice_is_invalid(self, parse):
        # particlesFb002: an all suffix cannot extend a choice base
        report = parse(
            "<xs:complexType name='b'><xs:choice>"
            "<xs:element name='c1'/><xs:element name='c2'/>"
            "</xs:choice></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='a1'/><xs:element name='a2'/>"
            "</xs:all></xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" in schema_codes(report)

    def test_all_extends_all_min_occurs_mismatch_is_invalid(self, parse):
        # Saxon all313: both are all groups but the minOccurs differs
        report = parse(
            "<xs:complexType name='b'><xs:all>"
            "<xs:element name='child1'/></xs:all></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all minOccurs='0'>"
            "<xs:element name='child2'/></xs:all>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        issues = [
            issue for issue in report.for_phase("schema") if issue.code == "particle-restriction"
        ]
        assert issues
        assert any("minOccurs" in issue.message for issue in issues)

    def test_all_extends_all_min_occurs_equal_is_valid(self, parse):
        # Saxon all314: both minOccurs=0 is the 1.1 relaxation
        report = parse(
            "<xs:complexType name='b'><xs:all minOccurs='0'>"
            "<xs:element name='child1'/></xs:all></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all minOccurs='0'>"
            "<xs:element name='child2'/></xs:all>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" not in schema_codes(report)

    def test_all_extends_all_overlapping_elements_is_invalid(self, parse):
        # Saxon all302: the composed all has two particles named 'c'
        report = parse(
            "<xs:complexType name='b'><xs:all>"
            "<xs:element name='a'/><xs:element name='c'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='e'/><xs:element name='c'/>"
            "</xs:all></xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" in schema_codes(report)

    def test_all_extends_all_overlapping_wildcards_is_invalid(self, parse):
        # Saxon all305: the base admits http://one.com/ and the
        # extension excludes only http://two.com/, so the two composed
        # wildcards overlap
        report = parse(
            "<xs:complexType name='b'><xs:all>"
            "<xs:element name='a'/>"
            "<xs:any namespace='http://one.com/' processContents='skip'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='e'/>"
            "<xs:any notNamespace='http://two.com/' processContents='skip'/>"
            "</xs:all></xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" in schema_codes(report)

    def test_all_extends_all_disjoint_wildcards_is_valid(self, parse):
        # Saxon all304: disjoint namespace constraints compose cleanly
        report = parse(
            "<xs:complexType name='b'><xs:all>"
            "<xs:element name='a'/>"
            "<xs:any namespace='http://one.com/' processContents='skip'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='e'/>"
            "<xs:any namespace='http://two.com/' processContents='skip'/>"
            "</xs:all></xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" not in schema_codes(report)

    def test_all_extends_all_clean_shapes_are_valid(self, parse):
        # Saxon all301/all306/all307: the composed all is unambiguous
        report = parse(
            "<xs:complexType name='b' mixed='true'><xs:all>"
            "<xs:element name='a' minOccurs='0' maxOccurs='5'/>"
            "<xs:element name='b' minOccurs='0' maxOccurs='5'/>"
            "</xs:all></xs:complexType>"
            "<xs:complexType name='t' mixed='true'><xs:complexContent>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='d' minOccurs='0' maxOccurs='1'/>"
            "<xs:element name='e' minOccurs='0' maxOccurs='4'/>"
            "</xs:all></xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" not in schema_codes(report)

    def test_all_extends_empty_mixed_all_base_is_invalid(self, parse):
        # Saxon all308 (bug 6202): an empty mixed all base cannot be
        # extended by an all
        report = parse(
            "<xs:complexType name='b' mixed='true'><xs:all/></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent mixed='true'>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='d' minOccurs='0' maxOccurs='2'/>"
            "</xs:all></xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" in schema_codes(report)

    def test_all_extends_empty_all_base_is_valid(self, parse):
        # mgO007: a non-mixed empty all base makes the suffix the model
        report = parse(
            "<xs:complexType name='b'><xs:all minOccurs='1' maxOccurs='1'/>"
            "</xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='e1'/></xs:all>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" not in schema_codes(report)

    def test_all_extends_empty_sequence_base_is_valid(self, parse):
        # mgO028: an empty sequence base has empty content, so the all
        # suffix becomes the effective model
        report = parse(
            "<xs:complexType name='b'><xs:sequence/></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:all minOccurs='0' maxOccurs='1'>"
            "<xs:element name='e1'/></xs:all>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" not in schema_codes(report)

    def test_all_extends_all_group_ref_suffix_is_valid(self, parse):
        # mgO035/mgZ003: the extension suffix is a group reference to an
        # all group over an empty all base
        report = parse(
            "<xs:complexType name='b'><xs:all minOccurs='1' maxOccurs='1'/>"
            "</xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:group ref='g'/>"
            "</xs:extension></xs:complexContent></xs:complexType>"
            "<xs:group name='g'><xs:all><xs:element name='e1'/></xs:all></xs:group>"
        )
        assert "particle-restriction" not in schema_codes(report)

    def test_empty_suffix_over_all_base_is_valid(self, parse):
        # an empty sequence suffix is empty explicit content: the base
        # particle is retained, so there is no structure error
        report = parse(
            "<xs:complexType name='b'><xs:all>"
            "<xs:element name='a'/></xs:all></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:sequence/>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" not in schema_codes(report)

    def test_all_extends_all_through_two_steps_is_valid(self, parse):
        # a extends all, b extends a by an all: the chain composes
        report = parse(
            "<xs:complexType name='a'><xs:all>"
            "<xs:element name='a1'/></xs:all></xs:complexType>"
            "<xs:complexType name='b'><xs:complexContent>"
            "<xs:extension base='a'><xs:all>"
            "<xs:element name='b1'/></xs:all>"
            "</xs:extension></xs:complexContent></xs:complexType>"
            "<xs:complexType name='c'><xs:complexContent>"
            "<xs:extension base='b'><xs:all>"
            "<xs:element name='c1'/></xs:all>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" not in schema_codes(report)

    def test_sequence_extension_of_sequence_base_is_valid(self, parse):
        # the ordinary non-all shape is not reported
        report = parse(
            "<xs:complexType name='b'><xs:sequence>"
            "<xs:element name='a'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='b'><xs:sequence>"
            "<xs:element name='d'/></xs:sequence>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" not in schema_codes(report)


class TestExtensionOfBuiltinAnyType:
    """``xs:anyType`` bases in an extension suffix check.

    ``xs:anyType``'s effective content type is mixed with a sequence
    particle, so an ``all`` suffix over it is the same all-in-sequence
    violation as any sequence base; a ``sequence`` suffix is not
    reported by this check (the 1.1 mixed-variety mismatch is a
    different rule).
    """

    def test_all_extends_any_type_is_invalid(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='xs:anyType'><xs:all>"
            "<xs:element name='e'/></xs:all></xs:extension>"
            "</xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" in schema_codes(report)

    def test_sequence_extends_any_type_is_not_reported(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='xs:anyType'><xs:sequence>"
            "<xs:element name='e'/></xs:sequence></xs:extension>"
            "</xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" not in schema_codes(report)

    def test_attribute_only_extends_any_type_is_valid(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='xs:anyType'>"
            "<xs:attribute name='x' type='xs:string'/></xs:extension>"
            "</xs:complexContent></xs:complexType>"
        )
        assert "particle-restriction" not in schema_codes(report)

    def test_all_extends_any_type_is_invalid_in_legacy_mode(self, tmp_path):
        # legacy namespace mode resolves no prefixes, so the builtin is
        # recognised by the conventional xs:/xsd: spelling
        schema_path = tmp_path / "schema.xsd"
        schema_path.write_text(
            "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='xs:anyType'><xs:all><xs:element name='e'/>"
            "</xs:all></xs:extension></xs:complexContent></xs:complexType>"
            "<xs:element name='r' type='t'/></xs:schema>",
            encoding="utf-8",
        )
        report = Schema.compile(str(schema_path), mode=ParseModes.STRICT).report
        assert "particle-restriction" in schema_codes(report)


class TestWildcardNamespaceConstraintGrammar:
    """Schema-phase ``namespace`` grammar on ``xs:any``/``xs:anyAttribute``.

    Only ``##any`` or ``##other`` (alone), or a whitespace-separated list
    whose tokens are URI references, ``##local`` or
    ``##targetNamespace``, are legal. Corpus shapes: MS wildC/wildF
    (``xs:any``), wildK/wildN (``xs:anyAttribute``).
    """

    def any_schema(self, namespace):
        return (
            "<xs:complexType name='t'><xs:sequence>"
            f"<xs:any namespace='{namespace}'/>"
            "</xs:sequence></xs:complexType>"
        )

    def attribute_schema(self, namespace):
        return (
            f"<xs:complexType name='t'><xs:anyAttribute namespace='{namespace}'/></xs:complexType>"
        )

    @pytest.mark.parametrize(
        "namespace, source",
        [
            ("##target", "wildC035"),
            ("##all", "wildC036"),
            ("##any ##other", "wildC049"),
            ("##any ##local", "wildC050"),
            ("##any ##targetNameSpace", "wildC051"),
            ("##other ##local", "wildC052"),
            ("##other ##targetNamespace", "wildC053"),
            ("##any ##other ##local", "wildC055"),
            ("##any ##other ##targetNamespace", "wildC056"),
            ("##any ##local ##targetNamespace", "wildC057"),
            ("##any ##other ##local ##targetNamespace", "wildC058"),
            ("##any http://www.w3.org/1999/xhtml", "wildC066"),
            ("##other http://www.w3.org/1999/xhtml", "wildC067"),
            ("##anyAttribute", "wildN001"),
            ("##anyAttribute ##other ##local ##targetNamespace", "wildN015"),
        ],
    )
    def test_illegal_namespace_token_on_any(self, parse, namespace, source):
        report = parse(self.any_schema(namespace))
        assert "wildcard-invalid" in schema_codes(report), source

    @pytest.mark.parametrize(
        "namespace, source",
        [
            ("##anyAttribute", "wildK002/wildN001"),
            ("##target", "wildK006"),
            ("##all", "wildK007"),
            ("##anyAttribute ##other", "wildK020/wildN006"),
            ("##anyAttribute ##local", "wildK021/wildN007"),
            ("##anyAttribute ##targetNamespace", "wildK022/wildN008"),
            ("##other ##local", "wildK023/wildN009"),
            ("##other ##targetNamespace", "wildK024/wildN010"),
            ("##anyAttribute ##local ##targetNamespace", "wildK028/wildN014"),
            ("##anyAttribute ##other ##local ##targetNamespace", "wildK029/wildN015"),
            ("##anyAttribute http://foobar", "wildN016"),
            ("##other http://foobar", "wildN018/wildK038"),
        ],
    )
    def test_illegal_namespace_token_on_any_attribute(self, parse, namespace, source):
        report = parse(self.attribute_schema(namespace))
        assert "wildcard-invalid" in schema_codes(report), source

    @pytest.mark.parametrize(
        "namespace, source",
        [
            ("##any", "wildC031"),
            ("##other", "wildC033"),
            ("##local", "wildC032"),
            ("##targetNamespace", "wildC034"),
            ("##local ##targetNamespace", "wildC054"),
            ("http://a.example/ http://b.example/", "wildC060"),
            ("#any", "wildC037: a single '#' is a URI reference"),
            ("#local", "wildC038"),
            ("#other", "wildC039"),
            ("#targetNamespace", "wildC040"),
            ("any", "wildC043: plain words are relative URI references"),
            ("local", "wildC044"),
            ("target", "wildC047"),
            ("2", "brief: numeric token is a URI reference"),
            ("a:b", "brief ruling: a QName-like token is a legal URI reference"),
            ("", "wildZ010: empty namespace is not a schema error"),
            ("x http://a.example/", "wildN019/mixed list"),
            ("##local ##targetNamespace http://foobar", "wildN020 list"),
        ],
    )
    def test_legal_namespace_token_on_any(self, parse, namespace, source):
        report = parse(self.any_schema(namespace))
        assert "wildcard-invalid" not in schema_codes(report), source

    def test_legal_namespace_token_on_any_attribute(self, parse):
        report = parse(self.attribute_schema("a:b"))
        assert "wildcard-invalid" not in schema_codes(report)

    def test_legal_namespace_list_on_any_attribute(self, parse):
        report = parse(self.attribute_schema("##local http://foobar"))
        assert "wildcard-invalid" not in schema_codes(report)


class TestWildcardProcessContents:
    """Schema-phase ``processContents`` grammar (MS wildD/wildL)."""

    def any_schema(self, value):
        return (
            "<xs:complexType name='t'><xs:sequence>"
            f"<xs:any processContents='{value}'/>"
            "</xs:sequence></xs:complexType>"
        )

    def attribute_schema(self, value):
        return (
            "<xs:complexType name='t'>"
            f"<xs:anyAttribute processContents='{value}'/>"
            "</xs:complexType>"
        )

    @pytest.mark.parametrize(
        "value, source",
        [
            ("", "wildD071"),
            ("lax skip", "wildD075"),
            ("lax strict", "wildD076"),
            ("skip strict", "wildD077"),
            ("lax skip strict", "wildD078"),
            ("all", "wildD079"),
        ],
    )
    def test_illegal_process_contents_on_any(self, parse, value, source):
        report = parse(self.any_schema(value))
        assert "wildcard-invalid" in schema_codes(report), source

    @pytest.mark.parametrize(
        "value, source",
        [
            ("", "wildL001"),
            ("lax skip", "wildL005"),
            ("lax strict", "wildL006"),
            ("skip strict", "wildL007"),
            ("lax skip strict", "wildL008"),
            ("all", "wildL009"),
        ],
    )
    def test_illegal_process_contents_on_any_attribute(self, parse, value, source):
        report = parse(self.attribute_schema(value))
        assert "wildcard-invalid" in schema_codes(report), source

    @pytest.mark.parametrize("value", ["skip", "lax", "strict"])
    def test_legal_process_contents_on_any(self, parse, value):
        report = parse(self.any_schema(value))
        assert "wildcard-invalid" not in schema_codes(report)

    @pytest.mark.parametrize("value", ["skip", "lax", "strict"])
    def test_legal_process_contents_on_any_attribute(self, parse, value):
        report = parse(self.attribute_schema(value))
        assert "wildcard-invalid" not in schema_codes(report)

    def test_absent_process_contents_defaults_to_strict(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:sequence><xs:any/></xs:sequence></xs:complexType>"
        )
        assert "wildcard-invalid" not in schema_codes(report)

    def test_wildcard_spec_still_defaults_malformed_to_strict(self):
        # ``wildcard_spec`` keeps its defensive default (``strict``) so a
        # malformed value can never silently disable validation; the new
        # schema check inspects the raw attribute instead.
        spec = wildcard_spec({"processContents": "all"})
        assert spec.process_contents == "strict"


class TestAnyOccurrenceLegality:
    """``xs:any`` is a particle and takes ``minOccurs``/``maxOccurs``.

    Corpus shapes: MS wildB (lexical garbage and range violations), and
    particlesOa004/Oa008 (``minOccurs`` > ``maxOccurs``).
    """

    def any_schema(self, attributes):
        return (
            "<xs:complexType name='t'><xs:sequence>"
            f"<xs:any {attributes}/>"
            "</xs:sequence></xs:complexType>"
        )

    @pytest.mark.parametrize(
        "attributes, source",
        [
            ("maxOccurs=''", "wildB014"),
            ("maxOccurs='-1'", "wildB015"),
            ("maxOccurs='Unbounded'", "wildB016"),
            ("minOccurs='unbounded'", "wildB020"),
            ("minOccurs=''", "wildB022"),
            ("minOccurs='-1'", "wildB023"),
            ("minOccurs='Unbounded'", "wildB024"),
            ("minOccurs='unbounded' maxOccurs='unbounded'", "wildB028"),
        ],
    )
    def test_lexically_illegal_occurs_on_any(self, parse, attributes, source):
        report = parse(self.any_schema(attributes))
        assert "invalid-occurs" in schema_codes(report), source

    def test_min_occurs_greater_than_max_occurs_on_any(self, parse):
        # wildB027
        report = parse(self.any_schema("minOccurs='2' maxOccurs='1'"))
        assert "declaration-attribute" in schema_codes(report)

    def test_valid_occurs_on_any(self, parse):
        report = parse(self.any_schema("minOccurs='0' maxOccurs='unbounded'"))
        assert "invalid-occurs" not in schema_codes(report)
        assert "declaration-attribute" not in schema_codes(report)


class TestAnyAttributeOccurrenceLegality:
    """``xs:anyAttribute`` takes no ``minOccurs``/``maxOccurs`` (wildQ)."""

    def attribute_schema(self, attributes):
        return f"<xs:complexType name='t'><xs:anyAttribute {attributes}/></xs:complexType>"

    @pytest.mark.parametrize(
        "attributes, source",
        [
            ("minOccurs='2'", "wildQ002"),
            ("maxOccurs='2'", "wildQ003"),
            ("minOccurs='2' maxOccurs='unbounded'", "wildQ004"),
        ],
    )
    def test_occurrence_attribute_on_any_attribute(self, parse, attributes, source):
        report = parse(self.attribute_schema(attributes))
        assert "wildcard-invalid" in schema_codes(report), source

    def test_plain_any_attribute_has_no_occurrence(self, parse):
        report = parse(self.attribute_schema(""))
        assert "wildcard-invalid" not in schema_codes(report)


class TestWildcardAnnotationCardinality:
    """A wildcard carries at most one annotation (wildE002/wildM002, SUN)."""

    ANNOTATION = "<xs:annotation><xs:documentation>x</xs:documentation></xs:annotation>"

    def test_two_annotations_on_any(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:sequence><xs:any>"
            f"{self.ANNOTATION}{self.ANNOTATION}"
            "</xs:any></xs:sequence></xs:complexType>"
        )
        assert "declaration-duplicate" in schema_codes(report)

    def test_two_annotations_on_any_attribute(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:anyAttribute>"
            f"{self.ANNOTATION}{self.ANNOTATION}"
            "</xs:anyAttribute></xs:complexType>"
        )
        assert "declaration-duplicate" in schema_codes(report)

    def test_one_annotation_is_valid(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:sequence><xs:any>"
            f"{self.ANNOTATION}"
            "</xs:any></xs:sequence></xs:complexType>"
        )
        assert "declaration-duplicate" not in schema_codes(report)


class TestWildcardForeignAttributes:
    """Unqualified XML attributes on a wildcard are restricted (wildI).

    ``wildI001`` pins the qualified foreign attribute ``a:b="c"``
    *valid*: attributes in a non-schema namespace are foreign and never
    reported. ``wildI002``/``wildI003`` add an unqualified attribute and
    are invalid.
    """

    def sequence(self, any_attributes):
        return (
            "<xs:complexType name='t'><xs:sequence>"
            f"<xs:any {any_attributes}/>"
            "</xs:sequence></xs:complexType>"
        )

    def test_unknown_unqualified_attribute_on_any(self, parse):
        # wildI003
        report = parse(self.sequence("id='bar' namespace='##other' foo='bar'"))
        assert "invalid-attribute" in schema_codes(report)

    def test_unqualified_attribute_beside_qualified_foreign(self, parse):
        # wildI002
        report = parse(
            self.sequence(
                "id='bar' namespace='##other' processContents='lax' "
                "maxOccurs='2' minOccurs='1' a:b='c' b='c' xmlns:a='http://foo'"
            )
        )
        assert "invalid-attribute" in schema_codes(report)

    def test_qualified_foreign_attribute_is_valid(self, parse):
        # wildI001
        report = parse(
            self.sequence(
                "id='bar' namespace='##other' processContents='lax' "
                "maxOccurs='2' minOccurs='1' a:b='c' xmlns:a='http://foo'"
            )
        )
        assert "invalid-attribute" not in schema_codes(report)

    def test_unknown_unqualified_attribute_on_any_attribute(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:anyAttribute namespace='##other' foo='bar'/>"
            "</xs:complexType>"
        )
        assert "invalid-attribute" in schema_codes(report)

    def test_qualified_foreign_attribute_on_any_attribute_is_valid(self, parse):
        report = parse(
            "<xs:complexType name='t'>"
            "<xs:anyAttribute namespace='##other' a:b='c' xmlns:a='http://foo'/>"
            "</xs:complexType>"
        )
        assert "invalid-attribute" not in schema_codes(report)


class TestWildcardNonDeterminism:
    """UPA for element wildcards in one ``sequence``/``choice`` (wildI009-014).

    In a ``choice`` every alternative is live at once, so overlapping
    wildcards are ambiguous. In a ``sequence`` the earlier wildcard
    creates the ambiguity only when it can match again
    (``maxOccurs`` > 1) or be skipped (``minOccurs`` = 0) while every
    particle between the two is emptiable; ``wildI011``/``wildI012`` pin
    the single-occurrence and second-repeats shapes *valid*.
    """

    def choice_schema(self, children, choice_attrs=""):
        return (
            "<xs:complexType name='t'><xs:choice"
            + (f" {choice_attrs}" if choice_attrs else "")
            + ">"
            + children
            + "</xs:choice></xs:complexType>"
        )

    def sequence_schema(self, children, sequence_attrs=""):
        return (
            "<xs:complexType name='t'><xs:sequence"
            + (f" {sequence_attrs}" if sequence_attrs else "")
            + ">"
            + children
            + "</xs:sequence></xs:complexType>"
        )

    def test_overlapping_wildcards_in_choice(self, parse):
        # wildI009: ##other and A overlap in a repeating choice
        report = parse(
            self.choice_schema(
                "<xs:any namespace='##other' processContents='lax'/><xs:any namespace='A'/>",
                "maxOccurs='10'",
            )
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_identical_wildcards_in_choice(self, parse):
        # wildI010: two identical wildcards in a choice are still ambiguous
        report = parse(
            self.choice_schema(
                "<xs:any namespace='A' processContents='lax'/><xs:any namespace='A'/>",
                "maxOccurs='10'",
            )
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_repeatable_wildcard_before_overlapping_sequence_member(self, parse):
        # wildI013: ##other maxOccurs=2 followed by A
        report = parse(
            self.sequence_schema(
                "<xs:any namespace='##other' maxOccurs='2' processContents='lax'/>"
                "<xs:any namespace='A' processContents='lax'/>",
                "maxOccurs='10'",
            )
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_repeatable_wildcard_between_overlapping_sequence_members(self, parse):
        # wildI014: A, ##other maxOccurs=2, A
        report = parse(
            self.sequence_schema(
                "<xs:any namespace='A' processContents='lax'/>"
                "<xs:any namespace='##other' maxOccurs='2' processContents='lax'/>"
                "<xs:any namespace='A' processContents='lax'/>",
                "maxOccurs='10'",
            )
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_optional_wildcard_before_overlapping_sequence_member(self, parse):
        # a skippable first wildcard and the next can both match at the start
        report = parse(
            self.sequence_schema("<xs:any namespace='A' minOccurs='0'/><xs:any namespace='A'/>")
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_single_occurrence_wildcards_in_sequence_are_deterministic(self, parse):
        # wildI011
        report = parse(
            self.sequence_schema(
                "<xs:any namespace='##other' processContents='lax'/>"
                "<xs:any namespace='A' processContents='lax'/>"
            )
        )
        assert "wildcard-invalid" not in schema_codes(report)

    def test_second_wildcard_repeating_in_sequence_is_deterministic(self, parse):
        # wildI012
        report = parse(
            self.sequence_schema(
                "<xs:any namespace='##other' processContents='lax'/>"
                "<xs:any namespace='A' maxOccurs='2' processContents='lax'/>"
            )
        )
        assert "wildcard-invalid" not in schema_codes(report)

    def test_identical_single_occurrence_wildcards_in_sequence_are_valid(self, parse):
        report = parse(self.sequence_schema("<xs:any namespace='A'/><xs:any namespace='A'/>"))
        assert "wildcard-invalid" not in schema_codes(report)

    def test_disjoint_wildcards_in_choice_are_valid(self, parse):
        report = parse(
            self.choice_schema(
                "<xs:any namespace='http://a.example/'/><xs:any namespace='http://b.example/'/>"
            )
        )
        assert "wildcard-invalid" not in schema_codes(report)

    def test_disjoint_wildcards_in_sequence_are_valid(self, parse):
        report = parse(
            self.sequence_schema(
                "<xs:any namespace='http://a.example/' maxOccurs='unbounded'/>"
                "<xs:any namespace='http://b.example/'/>"
            )
        )
        assert "wildcard-invalid" not in schema_codes(report)

    def test_invalid_namespace_token_does_not_drive_overlap_noise(self, parse):
        # rule 1 already reports the malformed token; the UPA sweep skips it
        report = parse(self.choice_schema("<xs:any namespace='##bogus'/><xs:any namespace='A'/>"))
        issues = [issue for issue in report.for_phase("schema") if issue.code == "wildcard-invalid"]
        assert len(issues) == 1

    def test_unreferenced_group_content_is_not_reported(self, parse):
        # addB194: UPA applies to a complex type's content model, not to an
        # orphan group definition, so an ambiguous sequence nothing
        # references stays valid
        report = parse(
            "<xs:group name='g'><xs:sequence>"
            "<xs:any namespace='##other' maxOccurs='2'/>"
            "<xs:any namespace='##other'/>"
            "</xs:sequence></xs:group>"
        )
        assert "wildcard-invalid" not in schema_codes(report)

    def test_referenced_group_content_is_reported(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:group ref='g'/></xs:complexType>"
            "<xs:group name='g'><xs:sequence>"
            "<xs:any namespace='##other' maxOccurs='2'/>"
            "<xs:any namespace='A'/>"
            "</xs:sequence></xs:group>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_transitively_unreferenced_group_content_is_not_reported(self, parse):
        # group reference is not transitive: 'inner' is named by a ref
        # site, but that site lives in an orphan group, so nothing in a
        # complex type declaration's model reaches 'inner' - valid (I1)
        report = parse(
            "<xs:group name='outer'><xs:sequence>"
            "<xs:group ref='inner'/>"
            "</xs:sequence></xs:group>"
            "<xs:group name='inner'><xs:sequence>"
            "<xs:any namespace='##other' maxOccurs='2'/>"
            "<xs:any namespace='A'/>"
            "</xs:sequence></xs:group>"
        )
        assert "wildcard-invalid" not in schema_codes(report)

    def test_transitively_referenced_group_content_is_reported(self, parse):
        # a type reaches 'inner' through 'outer', so UPA applies
        report = parse(
            "<xs:complexType name='t'><xs:group ref='outer'/></xs:complexType>"
            "<xs:group name='outer'><xs:sequence>"
            "<xs:group ref='inner'/>"
            "</xs:sequence></xs:group>"
            "<xs:group name='inner'><xs:sequence>"
            "<xs:any namespace='##other' maxOccurs='2'/>"
            "<xs:any namespace='A'/>"
            "</xs:sequence></xs:group>"
        )
        assert "wildcard-invalid" in schema_codes(report)


class TestAttributeWildcardAlgebra:
    """Rule 8: the pure intersection/union helpers for attribute wildcards.

    Restriction intersects the base and own constraints; extension unions
    them. Intersection takes the weaker ``processContents`` (skip < lax <
    strict), union the stronger, because both wildcards apply.
    """

    TARGET = "http://t.example/"

    def attr(self, namespace, process="strict", target=None):
        spec = WildcardSpec(namespace=namespace, process_contents=process, is_attribute=True)
        if target is not None:
            return WildcardSpec(
                namespace=namespace,
                process_contents=process,
                is_attribute=True,
                target_namespace=target,
            )
        return spec

    def admits(self, spec, uri):
        return spec.allows(uri, self.TARGET)

    def test_intersection_with_any_keeps_the_other_constraint(self):
        result = intersect_wildcard_specs(self.attr("##any"), self.attr("foo bar"), self.TARGET)
        assert result.namespace == "foo bar"
        assert self.admits(result, "foo") and not self.admits(result, "baz")

    def test_intersection_of_enumerations(self):
        result = intersect_wildcard_specs(self.attr("foo bar"), self.attr("bar baz"), self.TARGET)
        assert result.namespace == "bar"
        assert self.admits(result, "bar") and not self.admits(result, "foo")

    def test_intersection_of_other_and_enumeration_drops_the_target(self):
        result = intersect_wildcard_specs(
            self.attr("##other"), self.attr(f"{self.TARGET} foo"), self.TARGET
        )
        assert result.namespace == "foo"
        assert not self.admits(result, self.TARGET)
        assert not self.admits(result, None)

    def test_intersection_of_two_other_constraints(self):
        result = intersect_wildcard_specs(self.attr("##other"), self.attr("##other"), self.TARGET)
        assert result.namespace == "##other"
        assert self.admits(result, "foo") and not self.admits(result, self.TARGET)

    def test_intersection_of_other_constraints_with_distinct_targets(self):
        result = intersect_wildcard_specs(
            self.attr("##other", target="http://a.example/"),
            self.attr("##other", target="http://b.example/"),
            self.TARGET,
        )
        assert self.admits(result, "http://c.example/")
        assert not self.admits(result, "http://a.example/")
        assert not self.admits(result, "http://b.example/")

    def test_intersection_keeps_local_only_when_both_admit_it(self):
        result = intersect_wildcard_specs(
            self.attr("##local foo"), self.attr("##local bar"), self.TARGET
        )
        assert self.admits(result, None)
        result = intersect_wildcard_specs(self.attr("##local foo"), self.attr("foo"), self.TARGET)
        assert not self.admits(result, None)
        assert self.admits(result, "foo")

    def test_intersection_of_computed_other_local_keeps_absent(self):
        # A computed ``##other … ##local`` base (an extension union)
        # admits the absent namespace, so intersecting it with another
        # local-admitting set keeps that admission.
        base = self.attr("##other ##local", target=self.TARGET)
        result = intersect_wildcard_specs(base, self.attr("##local foo bar"), self.TARGET)
        assert result.namespace == "bar foo ##local"
        assert self.admits(result, None)
        assert self.admits(result, "foo")
        assert not self.admits(result, self.TARGET)

    def test_intersection_of_two_computed_other_locals_keeps_absent(self):
        base = self.attr("##other ##local", target=self.TARGET)
        own = self.attr(f"##other {self.TARGET} ##local", target=self.TARGET)
        result = intersect_wildcard_specs(base, own, self.TARGET)
        assert self.admits(result, None)
        assert self.admits(result, "other")
        assert not self.admits(result, self.TARGET)

    def test_intersection_of_computed_other_local_with_non_local_set_drops_absent(self):
        # Only one side admits the absent namespace: the intersection
        # must not.
        base = self.attr("##other ##local", target=self.TARGET)
        result = intersect_wildcard_specs(base, self.attr("foo bar"), self.TARGET)
        assert not self.admits(result, None)
        assert self.admits(result, "foo")

    def test_intersection_takes_the_weaker_process_contents(self):
        result = intersect_wildcard_specs(
            self.attr("##any", process="strict"),
            self.attr("##any", process="lax"),
            self.TARGET,
        )
        assert result.process_contents == "lax"
        result = intersect_wildcard_specs(
            self.attr("##any", process="lax"),
            self.attr("##any", process="skip"),
            self.TARGET,
        )
        assert result.process_contents == "skip"

    def test_union_with_any_is_any(self):
        result = union_wildcard_specs(self.attr("foo"), self.attr("##any"), self.TARGET)
        assert result.namespace == "##any"
        assert self.admits(result, "whatever")

    def test_union_of_enumerations(self):
        result = union_wildcard_specs(self.attr("foo"), self.attr("bar"), self.TARGET)
        assert self.admits(result, "foo") and self.admits(result, "bar")
        assert not self.admits(result, "baz")

    def test_union_of_other_and_enumeration(self):
        base = self.attr("##other", target="http://a.example/")
        result = union_wildcard_specs(base, self.attr("B"), self.TARGET)
        assert self.admits(result, "B")
        assert not self.admits(result, "http://a.example/")
        assert not self.admits(result, None)

    def test_union_of_other_and_enumeration_can_widen_to_any(self):
        # the set side admits the only namespace the ##other excludes and
        # the absent namespace, so the union is everything
        base = self.attr("##other", target=self.TARGET)
        result = union_wildcard_specs(base, self.attr(f"##local {self.TARGET} foo"), self.TARGET)
        assert result.namespace == "##any"
        assert self.admits(result, self.TARGET)
        assert self.admits(result, "bar")
        assert self.admits(result, None)

    def test_union_of_other_and_enumeration_keeps_absent_excluded(self):
        # even when the exclusions cancel, a union of constraints that both
        # exclude the absent namespace still excludes it
        base = self.attr("##other", target=self.TARGET)
        result = union_wildcard_specs(base, self.attr(f"{self.TARGET} foo"), self.TARGET)
        assert self.admits(result, self.TARGET)
        assert self.admits(result, "bar")
        assert not self.admits(result, None)

    def test_union_takes_the_stronger_process_contents(self):
        result = union_wildcard_specs(
            self.attr("##any", process="skip"),
            self.attr("##any", process="strict"),
            self.TARGET,
        )
        assert result.process_contents == "strict"

    def test_effective_wildcard_of_no_specs_is_none(self):
        assert effective_attribute_wildcard([], self.TARGET) is None

    def test_effective_wildcard_of_one_spec_is_that_spec(self):
        spec = self.attr("foo")
        result = effective_attribute_wildcard([spec], self.TARGET)
        assert result == spec

    def test_effective_wildcard_intersects_all_specs(self):
        result = effective_attribute_wildcard(
            [
                self.attr("##other", process="lax"),
                self.attr(f"{self.TARGET} foo", process="strict"),
                self.attr("foo bar", process="strict"),
            ],
            self.TARGET,
        )
        assert result.namespace == "foo"
        assert result.process_contents == "lax"


class TestAttributeWildcardAlgebraWithExclusions:
    """XSD 1.1 §3.10.6.3/§3.10.6.4: the set algebra carries exclusions.

    The corpus shapes are wild014-018, wild023-026, wild043-046 and
    wild058-060/wild083: intersection unions the exclusion sets, union
    intersects them with the wildcard-aware allowance rules.
    """

    TARGET = "http://t.example/"

    def spec(
        self,
        namespace="##any",
        *,
        not_namespace=(),
        not_qname=(),
        target=None,
        process="strict",
    ):
        return WildcardSpec(
            namespace=namespace,
            process_contents=process,
            is_attribute=True,
            target_namespace=self.TARGET if target is None else target,
            not_namespace=frozenset(not_namespace),
            not_qname=frozenset(not_qname),
        )

    def allows(self, spec, name, **kwargs):
        return spec.allows_name(name, self.TARGET, **kwargs)

    # -- intersection --------------------------------------------------------

    def test_intersection_unions_the_not_namespace_sets(self):
        # wild017: B excludes abel/cain, R excludes cain/abel/adam
        base = self.spec(not_namespace=("http://abel.com/", "http://cain.com/"))
        own = self.spec(not_namespace=("http://cain.com/", "http://abel.com/", "http://adam.com/"))
        result = intersect_wildcard_specs(base, own, self.TARGET)
        assert not self.allows(result, "{http://adam.com/}a")
        assert not self.allows(result, "{http://abel.com/}a")
        assert not self.allows(result, "{http://cain.com/}a")
        assert self.allows(result, "{http://eve.com/}a")

    def test_intersection_subtracts_not_namespace_from_a_list(self):
        # wild025: "adam eve" ∩ not("eve abel") = adam only
        listed = self.spec("http://adam.com/ http://eve.com/")
        excluded = self.spec(not_namespace=("http://eve.com/", "http://abel.com/"))
        result = intersect_wildcard_specs(listed, excluded, self.TARGET)
        assert self.allows(result, "{http://adam.com/}a")
        assert not self.allows(result, "{http://eve.com/}a")

    def test_intersection_of_not_namespace_keeps_absent_excluded(self):
        # wild023: not(##local) ∩ not(eve) still rejects unqualified names
        base = self.spec(not_namespace=("##local",))
        own = self.spec(not_namespace=("http://eve.com/",))
        result = intersect_wildcard_specs(base, own, self.TARGET)
        assert not self.allows(result, "a")
        assert not self.allows(result, "{http://eve.com/}a")
        assert self.allows(result, "{http://adam.com/}a")

    def test_intersection_unions_the_exact_not_qname_names(self):
        # wild043: {a b c} ∩ {c d e} = {a b c d e}
        base = self.spec(not_qname=("{urn:x}a", "{urn:x}b", "{urn:x}c"))
        own = self.spec(not_qname=("{urn:x}c", "{urn:x}d", "{urn:x}e"))
        result = intersect_wildcard_specs(base, own, self.TARGET)
        for local in ("a", "b", "c", "d", "e"):
            assert not self.allows(result, f"{{urn:x}}{local}")
        assert self.allows(result, "{urn:x}f")

    def test_intersection_keeps_defined_when_either_side_has_it(self):
        # wild058/059: g has ##defined jang, h has ##defined
        result = intersect_wildcard_specs(
            self.spec(not_qname=("##defined", "jang")),
            self.spec(not_qname=("##defined",)),
        )
        assert not self.allows(result, "jang")
        assert not self.allows(result, "zang", defined={"zang"})
        assert self.allows(result, "zing", defined={"zang"})

    # -- union ---------------------------------------------------------------

    def test_union_intersects_the_not_namespace_sets(self):
        # wild014: not(cain) union not(abel cain) = not(cain)
        base = self.spec(not_namespace=("http://cain.com/",))
        own = self.spec(not_namespace=("http://abel.com/", "http://cain.com/"))
        result = union_wildcard_specs(base, own, self.TARGET)
        assert not self.allows(result, "{http://cain.com/}a")
        assert self.allows(result, "{http://abel.com/}a")

    def test_union_subtracts_the_list_side_from_a_not_constraint(self):
        # wild015/wild016: "abel adam" union not("abel cain") = not(cain)
        listed = self.spec("http://abel.com/ http://adam.com/")
        excluded = self.spec(not_namespace=("http://abel.com/", "http://cain.com/"))
        result = union_wildcard_specs(listed, excluded, self.TARGET)
        assert self.allows(result, "{http://eve.com/}a")
        assert self.allows(result, "{http://adam.com/}a")
        assert not self.allows(result, "{http://cain.com/}a")

    def test_union_drops_names_the_other_side_allows(self):
        # wild060/wild083: a name survives only when the other wildcard
        # does not allow it
        base = self.spec(not_qname=("##defined", "no-surprise"))
        own = self.spec(not_qname=("surprise", "no-surprise"))
        result = union_wildcard_specs(base, own, self.TARGET)
        assert not self.allows(result, "no-surprise")
        assert self.allows(result, "surprise")

    def test_union_keeps_a_name_the_other_namespace_rejects(self):
        # wild046: the list side is ##local, so an XML-namespace exclusion
        # of the not side survives
        base = self.spec(namespace="##local", not_qname=("a", "b", "c"))
        own = self.spec(
            not_namespace=("http://www.w3.org/1999/XSL/Transform",),
            not_qname=("c", "d", "e", "{http://www.w3.org/XML/1998/namespace}lang"),
        )
        result = union_wildcard_specs(base, own, self.TARGET)
        assert not self.allows(result, "c")
        assert not self.allows(result, "{http://www.w3.org/XML/1998/namespace}lang")
        assert self.allows(result, "a")
        assert self.allows(result, "d")

    def test_union_drops_defined_unless_both_sides_carry_it(self):
        # wild083's spec note: ##defined is dropped when only one side has it
        base = self.spec(not_qname=("##defined", "no-surprise"))
        own = self.spec(not_qname=("surprise", "no-surprise"))
        result = union_wildcard_specs(base, own, self.TARGET)
        assert self.allows(result, "zang", defined={"zang"})

    def test_effective_wildcard_of_attribute_groups_carries_exclusions(self):
        # wild025/026: two attribute groups intersect
        groups = [
            self.spec("http://adam.com/ http://eve.com/"),
            self.spec(not_namespace=("http://eve.com/", "http://abel.com/")),
        ]
        result = effective_attribute_wildcard(groups, self.TARGET)
        assert self.allows(result, "{http://adam.com/}a")
        assert not self.allows(result, "{http://eve.com/}a")


class TestAttributeWildcardRestriction:
    """Rule 8 schema wiring: a restriction must narrow the base wildcard.

    The derived type's effective attribute wildcard (its own and its
    attribute groups' constraints, intersected) must be a subset of the
    base's effective wildcard, and must not weaken ``processContents``.
    """

    def restriction(self, base_wildcard, derived_wildcard):
        return (
            "<xs:complexType name='b'><xs:sequence/>"
            f"{base_wildcard}</xs:complexType>"
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:restriction base='b'><xs:sequence/>"
            f"{derived_wildcard}</xs:restriction>"
            "</xs:complexContent></xs:complexType>"
        )

    def test_restriction_widening_the_namespace_is_invalid(self, parse):
        report = parse(
            self.restriction(
                "<xs:anyAttribute namespace='foo'/>",
                "<xs:anyAttribute namespace='foo bar'/>",
            )
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_restriction_weakening_process_contents_is_invalid(self, parse):
        report = parse(
            self.restriction(
                "<xs:anyAttribute namespace='##any' processContents='strict'/>",
                "<xs:anyAttribute namespace='##any' processContents='lax'/>",
            )
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_restriction_narrowing_the_namespace_is_valid(self, parse):
        report = parse(
            self.restriction(
                "<xs:anyAttribute namespace='##any' processContents='lax'/>",
                "<xs:anyAttribute namespace='foo' processContents='strict'/>",
            )
        )
        assert "wildcard-invalid" not in schema_codes(report)

    def test_restriction_without_own_wildcard_is_valid(self, parse):
        report = parse(
            self.restriction(
                "<xs:anyAttribute namespace='##any'/>",
                "",
            )
        )
        assert "wildcard-invalid" not in schema_codes(report)

    def test_restriction_over_unresolved_base_is_skipped(self, parse):
        report = parse(
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:restriction base='missing'><xs:sequence/>"
            "<xs:anyAttribute namespace='##any'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert "wildcard-invalid" not in schema_codes(report)


class TestWildcardDeclarationPredicates:
    """The pure grammar predicates behind the wildcard declaration check."""

    def test_unknown_keyword_token(self):
        from pyxsd.wildcards import invalid_namespace_constraint

        assert invalid_namespace_constraint("##target") == "##target"
        assert invalid_namespace_constraint("foo ##all") == "##all"
        assert invalid_namespace_constraint("##ANY") == "##ANY"

    def test_legal_constraints(self):
        from pyxsd.wildcards import invalid_namespace_constraint

        for legal in (
            None,
            "",
            "##any",
            "##other",
            "##local",
            "##targetNamespace",
            "##local ##targetNamespace",
            "##any ##other",  # reported below only for ##any mixed? no: token ##any is unknown here
            "foo",
            "#any",
            "a:b",
            "foo ##local",
        ):
            if legal == "##any ##other":
                continue
            assert invalid_namespace_constraint(legal) is None, legal

    def test_process_contents_predicate(self):
        from pyxsd.wildcards import invalid_process_contents

        assert invalid_process_contents(None) is None
        assert invalid_process_contents("skip") is None
        assert invalid_process_contents(" lax ") is None
        assert invalid_process_contents("") == ""
        assert invalid_process_contents("lax skip") == "lax skip"

    def test_declaration_problems_reports_each_class(self):
        from pyxsd.wildcards import wildcard_declaration_problems

        problems = wildcard_declaration_problems(
            {
                "namespace": "##bogus",
                "processContents": "all",
                "minOccurs": "2",
                "foo": "bar",
            },
            is_attribute=True,
        )
        codes = [code for code, _ in problems]
        assert codes == [
            "wildcard-invalid",  # namespace
            "wildcard-invalid",  # processContents
            "wildcard-invalid",  # occurrence attribute
            "invalid-attribute",  # unknown unqualified attribute
        ]

    def test_declaration_problems_ignores_foreign_attributes(self):
        from pyxsd.wildcards import wildcard_declaration_problems

        assert (
            wildcard_declaration_problems({"{http://foo}b": "c", "id": "bar"}, is_attribute=False)
            == []
        )

    def test_declaration_problems_allows_11_wildcard_attributes(self):
        from pyxsd.wildcards import wildcard_declaration_problems

        assert (
            wildcard_declaration_problems(
                {"notNamespace": "foo", "notQName": "bar"}, is_attribute=False
            )
            == []
        )


NESTED_CHOICES_SCHEMA = (
    XSD_HEAD
    + """
  <xs:group name="G1">
    <xs:sequence>
      <xs:choice>
        <xs:element name="id" type="xs:integer"/>
        <xs:element name="id_str" type="xs:string"/>
      </xs:choice>
      <xs:choice>
        <xs:element name="name" type="xs:string"/>
        <xs:element name="type" type="xs:string"/>
      </xs:choice>
    </xs:sequence>
  </xs:group>
  <xs:element name="a">
    <xs:complexType>
      <xs:group ref="G1"/>
    </xs:complexType>
  </xs:element>
"""
    + XSD_TAIL
)

NESTED_SEQUENCES_SCHEMA = (
    XSD_HEAD
    + """
  <xs:group name="G1">
    <xs:sequence>
      <xs:sequence>
        <xs:element name="date" type="xs:date"/>
        <xs:element name="marked" type="xs:boolean"/>
      </xs:sequence>
      <xs:sequence>
        <xs:element name="num" type="xs:int"/>
      </xs:sequence>
    </xs:sequence>
  </xs:group>
  <xs:element name="a">
    <xs:complexType>
      <xs:group ref="G1"/>
    </xs:complexType>
  </xs:element>
"""
    + XSD_TAIL
)

FLAT_CHOICE_GROUP_SCHEMA = (
    XSD_HEAD
    + """
  <xs:group name="G2">
    <xs:choice>
      <xs:element name="a" type="xs:string"/>
      <xs:element name="b" type="xs:string"/>
    </xs:choice>
  </xs:group>
  <xs:element name="r">
    <xs:complexType>
      <xs:group ref="G2"/>
    </xs:complexType>
  </xs:element>
"""
    + XSD_TAIL
)


def _instance_errors(doc):
    return [
        issue for issue in doc.report.for_phase("instance") if issue.severity is IssueSeverity.ERROR
    ]


def _instance_parser(schema, instance, tmp_path):
    (tmp_path / "schema.xsd").write_text(schema, encoding="utf-8")
    (tmp_path / "instance.xml").write_text(instance, encoding="utf-8")
    return Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.STRICT).parse(
        str(tmp_path / "instance.xml")
    )


class TestGroupDefinitionCompositor:
    """A group definition's content model is its own direct compositor.

    Every nested compositor registers on the group, so the first
    registered entry is an inner one; the definition must record the
    compositor child written in the schema (MGroup particles00303m1 /
    particles00304m1).
    """

    def test_sequence_of_choices_accepts_the_second_choice(self, tmp_path):
        doc = _instance_parser(
            NESTED_CHOICES_SCHEMA,
            "<a><id_str>12345678900987654321J.ABC</id_str><type>#QQQQ</type></a>",
            tmp_path,
        )
        assert _instance_errors(doc) == []
        assert [child._name_ for child in doc.root._children_] == [
            "id_str",
            "type",
        ]

    def test_sequence_of_choices_requires_the_second_choice(self, tmp_path):
        doc = _instance_parser(
            NESTED_CHOICES_SCHEMA,
            "<a><id_str>12345678900987654321J.ABC</id_str></a>",
            tmp_path,
        )
        assert _instance_errors(doc)

    def test_sequence_of_sequences_accepts_all_members(self, tmp_path):
        doc = _instance_parser(
            NESTED_SEQUENCES_SCHEMA,
            "<a><date>2002-04-25</date><marked>true</marked><num>123</num></a>",
            tmp_path,
        )
        assert _instance_errors(doc) == []
        assert [child._name_ for child in doc.root._children_] == [
            "date",
            "marked",
            "num",
        ]

    def test_sequence_of_sequences_requires_the_second_sequence(self, tmp_path):
        doc = _instance_parser(
            NESTED_SEQUENCES_SCHEMA,
            "<a><date>2002-04-25</date><marked>true</marked></a>",
            tmp_path,
        )
        assert _instance_errors(doc)

    def test_flat_choice_group_still_selects_either_branch(self, tmp_path):
        valid = _instance_parser(FLAT_CHOICE_GROUP_SCHEMA, "<r><b>x</b></r>", tmp_path)
        assert _instance_errors(valid) == []
        assert [child._name_ for child in valid.root._children_] == ["b"]
        invalid = _instance_parser(FLAT_CHOICE_GROUP_SCHEMA, "<r><c>x</c></r>", tmp_path)
        assert _instance_errors(invalid)


GROUP_RESTRICTION_BODY = (
    "<xs:complexType name='A'><xs:choice minOccurs='0' maxOccurs='4'>"
    "<xs:group ref='x'/><xs:group ref='y'/></xs:choice></xs:complexType>"
    "<xs:group name='x'><xs:sequence>"
    "<xs:element name='x1'/><xs:element name='x2'/></xs:sequence></xs:group>"
    "<xs:group name='y'><xs:choice>"
    "<xs:element name='y1'/><xs:element name='y2'/></xs:choice></xs:group>"
    "<xs:group name='G'><xs:choice>"
    "<xs:group ref='x'/><xs:group ref='y'/></xs:choice></xs:group>"
)


def _empty_group_restriction(mixed: bool) -> str:
    mixed_attr = " mixed='true'" if mixed else ""
    return (
        f"<xs:complexType name='elem'{mixed_attr}><xs:complexContent>"
        "<xs:restriction base='A'>"
        "<xs:group ref='G' minOccurs='0' maxOccurs='0'/>"
        "</xs:restriction></xs:complexContent></xs:complexType>"
    )


class TestMixedContentRestriction:
    """A mixed content type cannot restrict an element-only base type.

    ``Derivation Valid (Restriction, Complex)`` clause 2.4.1 admits only
    an element-only derived type over an element-only/mixed base, or a
    mixed derived type over a mixed base. groupH007v (mixed, whose
    ``maxOccurs=0`` reference makes its effective content a synthetic
    empty sequence) is invalid; its non-mixed twin groupH008v is valid.
    """

    def test_mixed_restriction_of_element_only_base_is_reported(self, parse):
        report = parse(GROUP_RESTRICTION_BODY + _empty_group_restriction(mixed=True))
        assert "particle-restriction" in schema_codes(report)

    def test_element_only_restriction_of_element_only_base_is_valid(self, parse):
        report = parse(GROUP_RESTRICTION_BODY + _empty_group_restriction(mixed=False))
        assert "particle-restriction" not in schema_codes(report)

    def test_mixed_restriction_of_mixed_base_is_valid(self, parse):
        body = GROUP_RESTRICTION_BODY.replace(
            "<xs:complexType name='A'>", "<xs:complexType name='A' mixed='true'>"
        )
        report = parse(body + _empty_group_restriction(mixed=True))
        assert "particle-restriction" not in schema_codes(report)

    def test_element_only_restriction_over_an_optional_reference_is_valid(self, parse):
        derived = (
            "<xs:complexType name='elem'><xs:complexContent>"
            "<xs:restriction base='A'><xs:group ref='G' minOccurs='1'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        report = parse(GROUP_RESTRICTION_BODY + derived)
        assert "particle-restriction" not in schema_codes(report)


class TestAttributeUseDerivation:
    """Attribute-use derivation on complex type restriction/extension.

    The bounded slice of *Derivation Valid (Restriction, Complex)* and
    *Derivation Valid (Extension)* that the corpus pins: a required
    base attribute use must stay required in a restriction (XSD 1.0
    §3.4.6 clause 3; particlesZ030_d), and an extension that redeclares
    a base use must not change its fixed value (XSD 1.1 §3.4.6.2
    clause 1.2; particlesZ026a).
    """

    def test_restriction_required_use_made_optional_is_invalid(self, parse):
        # particlesZ030_d: the derived a(optional) cannot restrict the
        # base a(required)
        report = parse(
            "<xs:complexType name='cs'><xs:simpleContent>"
            "<xs:extension base='xs:string'>"
            "<xs:attribute name='a' use='required'/>"
            "<xs:attribute name='b' use='optional'/>"
            "</xs:extension></xs:simpleContent></xs:complexType>"
            "<xs:complexType name='csD'><xs:simpleContent>"
            "<xs:restriction base='cs'>"
            "<xs:simpleType><xs:restriction base='xs:string'/></xs:simpleType>"
            "<xs:attribute name='a' use='optional'/>"
            "</xs:restriction></xs:simpleContent></xs:complexType>"
        )
        assert "attribute-restriction" in schema_codes(report)

    def test_restriction_dropped_required_use_is_valid(self, parse):
        # Saxon assert011: omitting the base's required attribute from a
        # restriction is accepted by the corpus and the oracle, so only
        # a *redeclared* weaker use is reported
        report = parse(
            "<xs:complexType name='cs'><xs:sequence/>"
            "<xs:attribute name='a' use='required'/></xs:complexType>"
            "<xs:complexType name='csD'><xs:complexContent>"
            "<xs:restriction base='cs'><xs:sequence/></xs:restriction>"
            "</xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" not in schema_codes(report)

    def test_restriction_required_use_kept_is_valid(self, parse):
        report = parse(
            "<xs:complexType name='cs'><xs:sequence/>"
            "<xs:attribute name='a' use='required'/></xs:complexType>"
            "<xs:complexType name='csD'><xs:complexContent>"
            "<xs:restriction base='cs'><xs:sequence/>"
            "<xs:attribute name='a' use='required'/></xs:restriction>"
            "</xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" not in schema_codes(report)

    def test_restriction_optional_use_made_required_is_valid(self, parse):
        # strengthening an optional base use is a legal restriction
        report = parse(
            "<xs:complexType name='cs'><xs:sequence/>"
            "<xs:attribute name='a' use='optional'/></xs:complexType>"
            "<xs:complexType name='csD'><xs:complexContent>"
            "<xs:restriction base='cs'><xs:sequence/>"
            "<xs:attribute name='a' use='required'/></xs:restriction>"
            "</xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" not in schema_codes(report)

    def test_restriction_different_fixed_value_is_invalid(self, parse):
        # attZ008_e: the derived fixed must equal the base's fixed
        report = parse(
            "<xs:complexType name='Person'><xs:sequence/>"
            "<xs:attribute name='att' type='xs:string' fixed='not_fixed'/>"
            "</xs:complexType>"
            "<xs:complexType name='Someone'><xs:complexContent>"
            "<xs:restriction base='Person'><xs:sequence/>"
            "<xs:attribute name='att' type='xs:string' fixed='fixed'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" in schema_codes(report)

    def test_restriction_fixed_replaced_by_default_is_invalid(self, parse):
        # attZ008_f: a fixed base use cannot become a defaulted one
        report = parse(
            "<xs:complexType name='Person'><xs:sequence/>"
            "<xs:attribute name='att' type='xs:string' fixed='fixed'/>"
            "</xs:complexType>"
            "<xs:complexType name='Someone'><xs:complexContent>"
            "<xs:restriction base='Person'><xs:sequence/>"
            "<xs:attribute name='att' type='xs:string' default='fixed'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" in schema_codes(report)

    def test_restriction_fixed_dropped_is_invalid(self, parse):
        # attZ008_h: a fixed base use must be reproduced with the same fixed
        report = parse(
            "<xs:complexType name='Person'><xs:sequence/>"
            "<xs:attribute name='att' type='xs:string' fixed='fixed'/>"
            "</xs:complexType>"
            "<xs:complexType name='Someone'><xs:complexContent>"
            "<xs:restriction base='Person'><xs:sequence/>"
            "<xs:attribute name='att' type='xs:string'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" in schema_codes(report)

    def test_restriction_matching_fixed_value_is_accepted(self, parse):
        # attZ008_a/g
        report = parse(
            "<xs:complexType name='Person'><xs:sequence/>"
            "<xs:attribute name='att' type='xs:string' fixed='fixed'/>"
            "</xs:complexType>"
            "<xs:complexType name='Someone'><xs:complexContent>"
            "<xs:restriction base='Person'><xs:sequence/>"
            "<xs:attribute name='att' type='xs:string' fixed='fixed'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" not in schema_codes(report)

    def test_restriction_base_default_to_derived_fixed_is_accepted(self, parse):
        # attZ008_b/d: a base default may be replaced by a fixed
        for base_value in ("fixed", "not_fixed"):
            report = parse(
                "<xs:complexType name='Person'><xs:sequence/>"
                f"<xs:attribute name='att' type='xs:string' default='{base_value}'/>"
                "</xs:complexType>"
                "<xs:complexType name='Someone'><xs:complexContent>"
                "<xs:restriction base='Person'><xs:sequence/>"
                "<xs:attribute name='att' type='xs:string' fixed='fixed'/>"
                "</xs:restriction></xs:complexContent></xs:complexType>"
            )
            assert "attribute-restriction" not in schema_codes(report)

    def test_restriction_respaced_list_fixed_is_accepted(self, parse):
        # addB183: a list type's fixed value compares by value, so extra
        # whitespace is not a different fixed value
        report = parse(
            "<xs:simpleType name='intList'><xs:list itemType='xs:int'/></xs:simpleType>"
            "<xs:complexType name='base1'><xs:sequence/>"
            "<xs:attribute name='Att' type='intList' fixed='1   2        3'/>"
            "</xs:complexType>"
            "<xs:complexType name='derived1'><xs:complexContent>"
            "<xs:restriction base='base1'><xs:sequence/>"
            "<xs:attribute name='Att' type='intList' fixed='1  2   3'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" not in schema_codes(report)

    def test_extension_different_fixed_value_is_invalid(self, parse):
        # particlesZ026a: ManagedItemType redeclares StatementAssembly
        # with a different fixed value than its base's use
        report = parse(
            "<xs:complexType name='ItemType'><xs:sequence/>"
            "<xs:attribute name='StatementAssembly' type='xs:string' fixed='one'/>"
            "</xs:complexType>"
            "<xs:complexType name='ManagedItemType'><xs:complexContent>"
            "<xs:extension base='ItemType'>"
            "<xs:attribute name='StatementAssembly' type='xs:string' fixed='two'/>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" in schema_codes(report)

    def test_extension_matching_fixed_value_is_accepted(self, parse):
        report = parse(
            "<xs:complexType name='ItemType'><xs:sequence/>"
            "<xs:attribute name='StatementAssembly' type='xs:string' fixed='one'/>"
            "</xs:complexType>"
            "<xs:complexType name='ManagedItemType'><xs:complexContent>"
            "<xs:extension base='ItemType'>"
            "<xs:attribute name='StatementAssembly' type='xs:string' fixed='one'/>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" not in schema_codes(report)

    def test_extension_inherited_use_is_valid(self, parse):
        # not redeclaring the base use keeps it identical
        report = parse(
            "<xs:complexType name='ItemType'><xs:sequence/>"
            "<xs:attribute name='a' type='xs:string' fixed='one'/>"
            "</xs:complexType>"
            "<xs:complexType name='ManagedItemType'><xs:complexContent>"
            "<xs:extension base='ItemType'>"
            "<xs:attribute name='b' type='xs:string'/>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        assert "attribute-restriction" not in schema_codes(report)


class TestOpenContentModelMerge:
    """The open-content particle merge and its §3.4.4.3 attribution."""

    @staticmethod
    def _element(name, minimum=1, maximum=1):
        from pyxsd.content_model import Particle

        return Particle("element", minimum, maximum, [], name)

    @staticmethod
    def _nodes(*tags):
        import xml.etree.ElementTree as ET

        return [ET.Element(tag) for tag in tags]

    def test_interleave_admits_wildcard_around_and_between(self):
        from pyxsd.content_model import (
            Particle,
            expanded_name_of,
            match_content,
            merge_open_content,
        )
        from pyxsd.open_content import OpenContent

        model = Particle("sequence", 1, 1, [self._element("a"), self._element("b")])
        spec = WildcardSpec(namespace="urn:open", process_contents="lax")
        merged = merge_open_content(model, OpenContent("interleave", spec))
        for tags in (
            ("a", "{urn:open}c", "b"),
            ("{urn:open}c", "a", "b"),
            ("a", "b", "{urn:open}c"),
            ("a", "b"),
        ):
            complete, _leftover = match_content(
                merged,
                self._nodes(*tags),
                name_of=expanded_name_of,
                namespace_checked=True,
            )
            assert complete, tags

    def test_interleave_still_enforces_declared_order(self):
        from pyxsd.content_model import (
            Particle,
            expanded_name_of,
            match_content,
            merge_open_content,
        )
        from pyxsd.open_content import OpenContent

        model = Particle("sequence", 1, 1, [self._element("a"), self._element("b")])
        spec = WildcardSpec(namespace="urn:open", process_contents="lax")
        merged = merge_open_content(model, OpenContent("interleave", spec))
        complete, leftover = match_content(
            merged,
            self._nodes("b", "a"),
            name_of=expanded_name_of,
            namespace_checked=True,
        )
        assert not complete
        assert [expanded_name_of(node) for node in leftover] == ["b", "a"]

    def test_suffix_admits_trailing_only(self):
        from pyxsd.content_model import (
            Particle,
            expanded_name_of,
            match_content,
            merge_open_content,
        )
        from pyxsd.open_content import OpenContent

        model = Particle("sequence", 1, 1, [self._element("a"), self._element("b")])
        spec = WildcardSpec(namespace="urn:open", process_contents="lax")
        merged = merge_open_content(model, OpenContent("suffix", spec))
        complete, _ = match_content(
            merged,
            self._nodes("a", "b", "{urn:open}c"),
            name_of=expanded_name_of,
            namespace_checked=True,
        )
        assert complete
        complete, _ = match_content(
            merged,
            self._nodes("{urn:open}c", "a", "b"),
            name_of=expanded_name_of,
            namespace_checked=True,
        )
        assert not complete

    def test_interleave_forces_declared_attribution(self):
        # The open wildcard must not absorb a node the declared particle
        # can still consume: both ``i`` children bind to the declared
        # particle (Saxon open025 clause 3.3).
        from pyxsd.content_model import (
            Particle,
            expanded_name_of,
            match_content_with_open_content,
            merge_open_content,
        )
        from pyxsd.open_content import OpenContent

        declared = Particle(
            "sequence",
            1,
            1,
            [self._element("i", 1, None), self._element("d", 0, None)],
        )
        spec = WildcardSpec(namespace="##local", process_contents="skip")
        merged = merge_open_content(declared, OpenContent("interleave", spec))
        complete, _leftover, matches = match_content_with_open_content(
            merged,
            declared,
            self._nodes("i", "i", "j"),
            name_of=expanded_name_of,
            namespace_checked=True,
        )
        assert complete
        by_position = {match.position: match.particle for match in matches}
        assert by_position[0].kind == "element"
        assert by_position[1].kind == "element"
        assert by_position[2].kind == "any" and by_position[2].open_content
