"""XSD 1.1 wildcard exclusions: ``notNamespace`` / ``notQName`` (schema phase).

The XSD 1.1 wildcard allows an alternative namespace constraint
(``notNamespace`` instead of ``namespace``) and excludes individual
expanded names (``notQName``, including the ``##defined`` and
``##definedSibling`` keywords). Corpus shapes are condensed from the
Saxon ``Wild`` suite (wild007/008, wild017-022, wild031-039, wild047-060,
wild069, wild075-080) and the IBM S3_10_1 / S3_10_6 schema cases.

This is the schema-phase half of the feature (data model, declaration
grammar and consistency, derivation subsetting, the static side of the
tighter EDC); instance-phase binding (``##defined`` /
``##definedSibling`` admission and the dynamic EDC) is a separate task.
"""

import io

import pytest

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD
from pyxsd.particle_derivation import wildcard_subset
from pyxsd.validation import IssueSeverity
from pyxsd.wildcards import (
    WildcardSpec,
    wildcard_declaration_problems,
    wildcard_spec,
)

XML_NS = "http://www.w3.org/XML/1998/namespace"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

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

    Mirrors ``tests/test_content_models.py``: the instance phase is
    stubbed out so a schema declaring no root element can still be
    inspected.
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


@pytest.fixture
def validate(tmp_path):
    """Parse an inline instance against an inline schema and return the parser."""
    schema_path = tmp_path / "schema.xsd"
    instance_path = tmp_path / "instance.xml"

    def _validate(schema_string: str, instance_string: str) -> PyXSD:
        schema_path.write_text(schema_string, encoding="utf-8")
        instance_path.write_text(instance_string, encoding="utf-8")
        return PyXSD(
            str(instance_path),
            str(schema_path),
            xmlFileOutput=False,
            mode=ParseModes.NAMESPACED,
        )

    return _validate


def instance_codes(parser) -> set[str]:
    return {issue.code for issue in parser.report.for_phase("instance")}


# ---------------------------------------------------------------------------
# Rule 1: the data model
# ---------------------------------------------------------------------------


class TestWildcardSpecExclusions:
    """``not_namespace`` / ``not_qname`` on the pure spec value object."""

    def test_literal_uri_exclusion(self):
        spec = WildcardSpec(not_namespace=frozenset({"http://devil.com/"}))
        assert not spec.allows_name("{http://devil.com/}eve", None)
        assert spec.allows_name("{http://genesis.com/}cain", None)

    def test_absent_namespace_admitted_unless_local_excluded(self):
        # Not excluding ##local leaves the absent namespace admitted
        # (wild003's posture: everything else is excluded, the
        # unqualified name still passes).
        spec = WildcardSpec(not_namespace=frozenset({"http://devil.com/"}))
        assert spec.allows_name("cain", None)
        local = WildcardSpec(not_namespace=frozenset({"##local"}))
        assert not local.allows_name("cain", None)
        assert local.allows_name("{http://devil.com/}eve", None)

    def test_target_namespace_exclusion_resolves_against_the_declaring_document(self):
        spec = WildcardSpec(
            not_namespace=frozenset({"##targetNamespace"}), target_namespace="urn:t"
        )
        assert not spec.allows_name("{urn:t}a", None)
        assert spec.allows_name("{urn:o}a", None)

    def test_target_namespace_without_a_target_means_absent(self):
        spec = WildcardSpec(not_namespace=frozenset({"##targetNamespace"}))
        assert not spec.allows_name("a", None)
        assert spec.allows_name("{urn:o}a", None)

    def test_exact_qname_exclusion(self):
        spec = WildcardSpec(not_qname=frozenset({"{urn:x}a"}))
        assert not spec.allows_name("{urn:x}a", None)
        assert spec.allows_name("{urn:x}b", None)
        assert spec.allows_name("{urn:y}a", None)

    def test_bare_clark_entry_excludes_every_local_in_its_namespace(self):
        spec = WildcardSpec(not_qname=frozenset({"{urn:x}"}))
        assert not spec.allows_name("{urn:x}anything", None)
        assert spec.allows_name("{urn:y}anything", None)

    def test_exclusion_follows_the_namespace_constraint(self):
        # A name the wildcard does not admit is not "allowed" even when
        # notQName does not exclude it.
        spec = WildcardSpec(namespace="##local")
        assert spec.allows_name("a", None)
        assert not spec.allows_name("{urn:x}a", None)

    def test_not_namespace_replaces_the_namespace_constraint(self):
        # XSD 1.1 maps notNamespace to the "not" variety: the
        # namespace attribute is not consulted (the combination is a
        # schema error, so only the continuation reading matters).
        spec = WildcardSpec(namespace="##local", not_namespace=frozenset({"urn:x"}))
        assert spec.allows_name("{urn:y}a", None)
        assert not spec.allows_name("{urn:x}a", None)
        assert spec.allows_name("a", None)

    def test_defined_marker_applies_only_with_a_supplied_set(self):
        spec = WildcardSpec(not_qname=frozenset({"##defined"}))
        assert spec.allows_name("{urn:x}a", None)
        assert not spec.allows_name("{urn:x}a", None, defined={"{urn:x}a"})
        assert spec.allows_name("{urn:x}b", None, defined={"{urn:x}a"})

    def test_defined_sibling_marker_applies_only_with_a_supplied_set(self):
        spec = WildcardSpec(not_qname=frozenset({"##definedSibling"}))
        assert spec.allows_name("{urn:x}a", None)
        assert not spec.allows_name("{urn:x}a", None, siblings={"{urn:x}a"})


class TestWildcardSpecParsing:
    """``wildcard_spec`` reads the 1.1 attributes and expands QNames."""

    def test_not_namespace_tokens_are_split_and_kept_raw(self):
        spec = wildcard_spec({"notNamespace": " ##targetNamespace ##local "})
        assert spec.not_namespace == frozenset({"##targetNamespace", "##local"})
        assert spec.not_qname == frozenset()

    def test_not_qname_expansion_is_injected(self):
        def resolve(token):
            return {"x:a": "{urn:x}a", "xml:space": f"{{{XML_NS}}}space"}[token]

        spec = wildcard_spec({"notQName": "x:a xml:space ##defined"}, resolve_qname=resolve)
        assert spec.not_qname == frozenset({"{urn:x}a", f"{{{XML_NS}}}space", "##defined"})

    def test_not_qname_star_form_normalizes_to_the_bare_clark_name(self):
        spec = wildcard_spec({"notQName": "x:*"}, resolve_qname=lambda token: "{urn:x}*")
        assert spec.not_qname == frozenset({"{urn:x}"})
        assert not spec.allows_name("{urn:x}a", None)

    def test_expansion_failure_keeps_the_raw_token(self):
        def resolve(token):
            raise KeyError(token)

        spec = wildcard_spec({"notQName": "xsl:stylesheet"}, resolve_qname=resolve)
        assert spec.not_qname == frozenset({"xsl:stylesheet"})


class TestWildcardDeclarationGrammar:
    """The pure grammar predicates behind the declaration check."""

    def test_namespace_and_not_namespace_conflict(self):
        problems = wildcard_declaration_problems(
            {"namespace": "##any", "notNamespace": "http://a/"}, is_attribute=False
        )
        assert [code for code, _ in problems] == ["wildcard-invalid"]

    def test_not_namespace_marker_tokens_are_illegal(self):
        for value in ("##any", "##other", "##bogus"):
            problems = wildcard_declaration_problems({"notNamespace": value}, is_attribute=False)
            assert [code for code, _ in problems] == ["wildcard-invalid"], value

    def test_not_namespace_uri_and_keyword_tokens_are_legal(self):
        assert (
            wildcard_declaration_problems(
                {"notNamespace": "http://a/ ##local ##targetNamespace"}, is_attribute=False
            )
            == []
        )
        # Duplicates are legal (wild009 spells ##targetNamespace three times).
        assert (
            wildcard_declaration_problems(
                {"notNamespace": " ##targetNamespace ##targetNamespace ##local "},
                is_attribute=True,
            )
            == []
        )

    def test_not_qname_undeclared_prefix_is_illegal(self):
        def resolve(token):
            raise KeyError(token)

        problems = wildcard_declaration_problems(
            {"notQName": "xsl:stylesheet"}, is_attribute=False, resolve_qname=resolve
        )
        assert [code for code, _ in problems] == ["wildcard-invalid"]

    def test_not_qname_malformed_qnames_are_illegal(self):
        for value in ("xml:xml:lang", ":stylesheet"):
            problems = wildcard_declaration_problems({"notQName": value}, is_attribute=False)
            assert [code for code, _ in problems] == ["wildcard-invalid"], value

    def test_not_qname_legal_forms(self):
        def resolve(token):
            return {"x:a": "{urn:x}a", "xml:space": f"{{{XML_NS}}}space"}.get(token, token)

        assert (
            wildcard_declaration_problems(
                {"notQName": "x:a xml:space bar ##defined ##definedSibling"},
                is_attribute=False,
                resolve_qname=resolve,
            )
            == []
        )

    def test_existing_problem_classes_are_unchanged(self):
        problems = wildcard_declaration_problems(
            {"namespace": "##bogus", "processContents": "all"}, is_attribute=True
        )
        assert [code for code, _ in problems] == ["wildcard-invalid", "wildcard-invalid"]


# ---------------------------------------------------------------------------
# Rule 2: schema-phase declaration checks
# ---------------------------------------------------------------------------


class TestWildcardDeclarationSchemaChecks:
    """``wild007``/``wild008`` and ``wild031``-``wild039`` shapes."""

    def test_namespace_and_not_namespace_on_any_attribute(self, parse):
        # wild007 / IBM S3_10_6si01
        report = parse(
            "<xs:element name='eden'><xs:complexType><xs:sequence/>"
            "<xs:anyAttribute namespace='##other' notNamespace=' ##targetNamespace ' "
            "processContents='skip'/></xs:complexType></xs:element>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_namespace_and_not_namespace_on_any(self, parse):
        # wild008 / IBM S3_10_1si01
        report = parse(
            "<xs:element name='eden'><xs:complexType><xs:sequence>"
            "<xs:any namespace='##any' notNamespace=' ##targetNamespace ' "
            "processContents='skip' minOccurs='0'/></xs:sequence>"
            "</xs:complexType></xs:element>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_not_qname_name_outside_the_admitted_namespace_other(self, parse):
        # wild031: ##other does not admit the absent namespace
        report = parse(
            "<xs:complexType name='computer'><xs:all>"
            "<xs:any processContents='lax' namespace='##other' notQName='memory' "
            "minOccurs='0' maxOccurs='unbounded'/></xs:all></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_not_qname_name_outside_namespace_local(self, parse):
        # wild032: ##local does not admit the XML namespace
        report = parse(
            "<xs:complexType name='computer'><xs:sequence/>"
            "<xs:anyAttribute processContents='lax' namespace='##local' "
            "notQName='xml:space'/></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_not_qname_name_in_the_excluded_target_namespace(self, parse):
        # wild033 / IBM S3_10_6si02: ##targetNamespace resolves to the
        # absent namespace here, and CPU is unqualified
        report = parse(
            "<xs:complexType name='computer'><xs:sequence/>"
            "<xs:anyAttribute processContents='lax' "
            "notNamespace='##targetNamespace' notQName='CPU'/></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_not_qname_name_in_a_not_namespace_excluded_namespace(self, parse):
        # wild034
        report = parse(
            "<xs:complexType name='computer'><xs:sequence/>"
            f"<xs:anyAttribute processContents='lax' notNamespace='{XML_NS}' "
            "notQName='xml:space'/></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_not_qname_name_in_the_xsi_excluded_namespace(self, parse):
        # wild035: the xsi:type name lies in the excluded xsi namespace
        report = parse(
            f"<xs:complexType name='computer' xmlns:xsi='{XSI_NS}'><xs:sequence/>"
            f"<xs:anyAttribute processContents='lax' notNamespace='{XSI_NS}' "
            "notQName='xsi:type'/></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_undeclared_prefix_in_not_qname_on_any_attribute(self, parse):
        # wild036
        report = parse(
            "<xs:complexType name='computer'><xs:sequence/>"
            f"<xs:anyAttribute processContents='lax' notNamespace='{XSI_NS}' "
            "notQName='xsl:stylesheet'/></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_undeclared_prefix_in_not_qname_on_any(self, parse):
        # wild037
        report = parse(
            "<xs:complexType name='computer'><xs:sequence>"
            f"<xs:any processContents='lax' notNamespace='{XSI_NS}' "
            "notQName='xsl:stylesheet'/></xs:sequence></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_malformed_qname_with_two_colons(self, parse):
        # wild038
        report = parse(
            "<xs:complexType name='computer'><xs:sequence/>"
            f"<xs:anyAttribute processContents='lax' notNamespace='{XSI_NS}' "
            "notQName='xml:xml:lang'/></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_malformed_qname_with_empty_prefix(self, parse):
        # wild039
        report = parse(
            "<xs:complexType name='computer'><xs:sequence>"
            f"<xs:any processContents='lax' notNamespace='{XSI_NS}' "
            "notQName=':stylesheet'/></xs:sequence></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_not_namespace_duplicates_and_local_stay_valid(self, parse):
        # wild009: three ##targetNamespace plus ##local, duplicated
        report = parse(
            "<xs:element name='eden'><xs:complexType><xs:sequence/>"
            "<xs:anyAttribute notNamespace=' ##targetNamespace ##targetNamespace "
            "##targetNamespace ##local ' processContents='lax'/>"
            "</xs:complexType></xs:element>",
            head="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "targetNamespace='http://eden.com/'>",
        )
        assert not report.has_errors

    def test_ibm_not_qname_list_valid(self, parse):
        # IBM S3_10_1v02: the excluded list namespaces are admitted
        report = parse(
            "<xs:complexType name='t'><xs:sequence>"
            "<xs:any notNamespace='b c' notQName='a:a b a:c' processContents='lax' "
            "maxOccurs='unbounded'/></xs:sequence></xs:complexType>",
            head="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "xmlns:a='a' xmlns:b='b' xmlns:c='c' targetNamespace='a'>",
        )
        assert not report.has_errors

    def test_defined_sibling_markers_are_consistent(self, parse):
        # wild072 shape: markers are legal notQName entries
        report = parse(
            "<xs:complexType name='t'><xs:sequence>"
            "<xs:any notQName='##definedSibling' processContents='lax' "
            "minOccurs='0' maxOccurs='unbounded'/></xs:sequence></xs:complexType>"
        )
        assert not report.has_errors


# ---------------------------------------------------------------------------
# Rule 3: derivation subsetting with exclusions
# ---------------------------------------------------------------------------


class TestWildcardSubsetExclusions:
    """The pure subset predicate now reads both exclusion sets."""

    def test_derived_not_namespace_must_be_superset(self):
        base = WildcardSpec(not_namespace=frozenset({"http://cain.com/"}))
        wider = WildcardSpec(namespace="http://cain.com/")
        assert not wildcard_subset(wider, base)
        narrower = WildcardSpec(not_namespace=frozenset({"http://cain.com/", "http://abel.com/"}))
        assert wildcard_subset(narrower, base)

    def test_namespace_list_widening_against_not_namespace_base(self):
        # wild020-022: the derived list/##any admits a namespace the
        # base's notNamespace excludes
        base = WildcardSpec(
            not_namespace=frozenset({"http://cain.com/", "http://abel.com/", "http://adam.com/"})
        )
        assert not wildcard_subset(WildcardSpec(namespace="http://eve.com/ http://adam.com/"), base)
        assert not wildcard_subset(
            WildcardSpec(not_namespace=frozenset({"http://adam.com/"})), base
        )
        assert not wildcard_subset(WildcardSpec(namespace="##any"), base)

    def test_not_qname_markers_are_inherited(self):
        base = WildcardSpec(not_qname=frozenset({"##defined"}))
        assert not wildcard_subset(WildcardSpec(not_qname=frozenset({"a"})), base)
        assert wildcard_subset(WildcardSpec(not_qname=frozenset({"##defined", "a"})), base)
        sibling = WildcardSpec(not_qname=frozenset({"##definedSibling"}))
        assert not wildcard_subset(WildcardSpec(), sibling)
        assert wildcard_subset(WildcardSpec(not_qname=frozenset({"##definedSibling"})), sibling)

    def test_base_qname_must_stay_excluded(self):
        base = WildcardSpec(not_qname=frozenset({"{urn:x}a"}))
        assert not wildcard_subset(WildcardSpec(), base)
        assert wildcard_subset(WildcardSpec(not_qname=frozenset({"{urn:x}a"})), base)
        # A different local does not cover the base's exclusion.
        assert not wildcard_subset(WildcardSpec(not_qname=frozenset({"{urn:x}b"})), base)

    def test_a_bare_base_namespace_entry_needs_a_bare_derived_entry(self):
        base = WildcardSpec(not_qname=frozenset({"{urn:x}"}))
        assert not wildcard_subset(WildcardSpec(not_qname=frozenset({"{urn:x}a"})), base)
        assert wildcard_subset(WildcardSpec(not_qname=frozenset({"{urn:x}"})), base)

    def test_derived_bare_entry_covers_a_base_exact_entry(self):
        # A bare {urn:x} entry excludes every local in urn:x, so it
        # covers a base's exact {urn:x}a exclusion.
        base = WildcardSpec(not_qname=frozenset({"{urn:x}a"}))
        derived = WildcardSpec(not_qname=frozenset({"{urn:x}"}))
        assert wildcard_subset(derived, base)

    def test_derived_bare_entry_covers_several_base_exact_entries(self):
        base = WildcardSpec(not_qname=frozenset({"{urn:x}a", "{urn:x}b", "{urn:y}c"}))
        derived = WildcardSpec(not_qname=frozenset({"{urn:x}"}))
        assert not wildcard_subset(derived, base)
        ok = WildcardSpec(not_qname=frozenset({"{urn:x}", "{urn:y}c"}))
        assert wildcard_subset(ok, base)

    def test_unadmitted_base_qname_is_vacuous(self):
        # The base's excluded name lies outside the derived's admitted
        # namespaces, so the derived cannot admit it anyway.
        base = WildcardSpec(namespace="##any", not_qname=frozenset({"{urn:x}a"}))
        derived = WildcardSpec(namespace="urn:y")
        assert wildcard_subset(derived, base)

    def test_plain_constraints_are_unaffected(self):
        assert wildcard_subset(WildcardSpec(namespace="##local"), WildcardSpec())
        assert not wildcard_subset(WildcardSpec(), WildcardSpec(namespace="##local"))


class TestAttributeWildcardRestrictionWithExclusions:
    """wild017-022 / wild055-057: restrictions of attribute wildcards."""

    def test_valid_restriction_widens_the_exclusion_set(self, parse):
        # wild017: derived excludes cain, abel and adam
        report = parse(
            "<xs:complexType name='B'><xs:sequence/>"
            "<xs:anyAttribute notNamespace='http://abel.com/ http://cain.com/' "
            "processContents='lax'/></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:sequence/><xs:anyAttribute notNamespace='http://cain.com/ "
            "http://abel.com/ http://adam.com/' processContents='lax'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not report.has_errors

    def test_valid_restriction_of_an_any_base(self, parse):
        # wild018
        report = parse(
            "<xs:complexType name='B'><xs:sequence/>"
            "<xs:anyAttribute namespace='##any' processContents='lax'/></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:sequence/><xs:anyAttribute notNamespace='http://cain.com/ "
            "http://abel.com/ http://adam.com/' processContents='lax'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not report.has_errors

    def test_valid_restriction_to_a_single_uri(self, parse):
        # wild019
        report = parse(
            "<xs:complexType name='B'><xs:sequence/>"
            "<xs:anyAttribute notNamespace='http://cain.com/ http://abel.com/ "
            "http://adam.com/' processContents='lax'/></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:sequence/><xs:anyAttribute namespace='http://eve.com/' "
            "processContents='lax'/></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not report.has_errors

    def test_prefixed_not_qname_exclusion_dropped_in_restriction_is_invalid(self, parse):
        # The base disallows x:a; the derived wildcard drops the
        # exclusion and so admits a name the base disallows
        report = parse(
            "<xs:complexType name='B'><xs:sequence/>"
            "<xs:anyAttribute namespace='##any' notQName='x:a' processContents='skip'/>"
            "</xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:sequence/><xs:anyAttribute namespace='##any' processContents='skip'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>",
            head="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "xmlns:x='http://extra.com/'>",
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_prefixed_not_qname_exclusion_kept_in_restriction_is_valid(self, parse):
        report = parse(
            "<xs:complexType name='B'><xs:sequence/>"
            "<xs:anyAttribute namespace='##any' notQName='x:a' processContents='skip'/>"
            "</xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:sequence/><xs:anyAttribute namespace='##any' notQName='x:a x:b' "
            "processContents='skip'/></xs:restriction></xs:complexContent></xs:complexType>",
            head="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "xmlns:x='http://extra.com/'>",
        )
        assert not report.has_errors

    def test_derived_star_exclusion_covering_a_base_exact_name_is_valid(self, parse):
        # x:* resolves to the bare {http://extra.com/} entry, which
        # excludes every local in the namespace and so covers the base's
        # exact x:a exclusion
        report = parse(
            "<xs:complexType name='B'><xs:sequence/>"
            "<xs:anyAttribute namespace='##any' notQName='x:a' processContents='skip'/>"
            "</xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:sequence/><xs:anyAttribute namespace='##any' notQName='x:*' "
            "processContents='skip'/></xs:restriction></xs:complexContent></xs:complexType>",
            head="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "xmlns:x='http://extra.com/'>",
        )
        assert not report.has_errors

    def test_derived_uri_list_admits_an_excluded_namespace(self, parse):
        # wild020
        report = parse(
            "<xs:complexType name='B'><xs:sequence/>"
            "<xs:anyAttribute notNamespace='http://cain.com/ http://abel.com/ "
            "http://adam.com/' processContents='lax'/></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:sequence/><xs:anyAttribute namespace='http://eve.com/ "
            "http://adam.com/' processContents='lax'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_derived_exclusion_list_is_narrower(self, parse):
        # wild021
        report = parse(
            "<xs:complexType name='B'><xs:sequence/>"
            "<xs:anyAttribute notNamespace='http://cain.com/ http://abel.com/ "
            "http://adam.com/' processContents='lax'/></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:sequence/><xs:anyAttribute notNamespace='http://adam.com/' "
            "processContents='lax'/></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_derived_any_widens(self, parse):
        # wild022
        report = parse(
            "<xs:complexType name='B'><xs:sequence/>"
            "<xs:anyAttribute notNamespace='http://cain.com/ http://abel.com/ "
            "http://adam.com/' processContents='lax'/></xs:complexType>"
            "<xs:complexType name='R'><xs:complexContent><xs:restriction base='B'>"
            "<xs:sequence/><xs:anyAttribute namespace='##any' processContents='lax'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_defined_restriction_twin_is_valid(self, parse):
        # wild055: base excludes ##defined jang xml:space, derived
        # excludes ##defined jang jing (the XML namespace is no longer
        # admitted by ##local, so dropping its exclusion is legal)
        report = parse(
            "<xs:complexType name='zing'><xs:sequence/>"
            "<xs:anyAttribute namespace='##any' "
            "notQName='##defined jang xml:space' processContents='skip'/></xs:complexType>"
            "<xs:complexType name='restrictedZing'><xs:complexContent>"
            "<xs:restriction base='zing'><xs:sequence/>"
            "<xs:anyAttribute namespace='##local' notQName='##defined jang jing' "
            "processContents='skip'/></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not report.has_errors

    def test_defined_marker_dropped_in_restriction_is_invalid(self, parse):
        # wild057: the base disallows ##defined, the derived does not
        report = parse(
            "<xs:complexType name='zing'><xs:sequence/>"
            "<xs:anyAttribute namespace='##any' notQName='##defined' "
            "processContents='skip'/></xs:complexType>"
            "<xs:complexType name='restrictedZing'><xs:complexContent>"
            "<xs:restriction base='zing'><xs:sequence/>"
            "<xs:anyAttribute namespace='##local' notQName='zang zong' "
            "processContents='skip'/></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert "wildcard-invalid" in schema_codes(report)

    def test_defined_restriction_without_marker_is_valid(self, parse):
        # wild056: the base excludes nothing, the derived may exclude
        report = parse(
            "<xs:complexType name='zing'><xs:sequence/>"
            "<xs:anyAttribute namespace='##any' processContents='skip'/></xs:complexType>"
            "<xs:complexType name='restrictedZing'><xs:complexContent>"
            "<xs:restriction base='zing'><xs:sequence/>"
            "<xs:anyAttribute namespace='##local' notQName='##defined jang jing' "
            "processContents='skip'/></xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not report.has_errors

    def test_attribute_group_intersection_with_defined_markers_is_valid(self, parse):
        # wild058/wild059: the effective intersection must not lose the
        # marker when either contributor excludes it
        report = parse(
            "<xs:complexType name='zing'><xs:sequence/>"
            "<xs:attributeGroup ref='g'/><xs:attributeGroup ref='h'/>"
            "</xs:complexType>"
            "<xs:attributeGroup name='g'>"
            "<xs:anyAttribute namespace='##any' notQName='##defined jang' "
            "processContents='skip'/></xs:attributeGroup>"
            "<xs:attributeGroup name='h'>"
            "<xs:anyAttribute namespace='##local' notQName='##defined' "
            "processContents='skip'/></xs:attributeGroup>"
        )
        assert not report.has_errors


class TestElementWildcardRestrictionWithExclusions:
    """wild047-051, wild069: element-wildcard derivation exclusions."""

    def test_derived_wildcard_narrowing_the_exclusions_is_valid(self, parse):
        # wild047: the derived ##local wildcard only excludes more names
        report = parse(
            "<xs:complexType name='computer'><xs:all>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##local' notQName='a b c' minOccurs='0' "
            "maxOccurs='2' processContents='skip'/></xs:all></xs:complexType>"
            "<xs:complexType name='restrictedComputer'><xs:complexContent>"
            "<xs:restriction base='computer'><xs:sequence>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##local' notQName='a b c d e' minOccurs='1' "
            "maxOccurs='1' processContents='skip'/></xs:sequence>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
        assert not report.has_errors

    def test_derived_wildcard_admits_a_name_no_base_wildcard_admits(self, parse):
        # wild048: the derived ##any admits 'c' (excluded by the base's
        # ##local wildcard) and x:c/x:d/x:e (excluded by the base's
        # notNamespace=##local wildcard)
        report = parse(
            "<xs:complexType name='computer'><xs:all>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##local' notQName='a b c' minOccurs='0' "
            "maxOccurs='2' processContents='skip'/>"
            "<xs:any notNamespace='##local' notQName='x:c x:d x:e' minOccurs='0' "
            "maxOccurs='2' processContents='skip'/></xs:all></xs:complexType>"
            "<xs:complexType name='restrictedComputer'><xs:complexContent>"
            "<xs:restriction base='computer'><xs:sequence>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##any' notQName='a b x:c x:d' minOccurs='1' "
            "maxOccurs='1' processContents='skip'/></xs:sequence>"
            "</xs:restriction></xs:complexContent></xs:complexType>",
            head="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "xmlns:x='http://extra.com/'>",
        )
        assert "particle-restriction" in schema_codes(report)

    def test_split_derived_wildcards_reject_an_uncovered_name(self, parse):
        # wild051: x:heres-the-rub is excluded by the base and admitted
        # by the derived notNamespace=##local wildcard
        report = parse(
            "<xs:complexType name='computer'><xs:all>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##any' notQName='a b x:e x:d x:heres-the-rub' "
            "minOccurs='2' maxOccurs='6' processContents='skip'/></xs:all></xs:complexType>"
            "<xs:complexType name='restrictedComputer'><xs:complexContent>"
            "<xs:restriction base='computer'><xs:sequence>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##local' notQName='a b c' minOccurs='1' "
            "maxOccurs='3' processContents='skip'/>"
            "<xs:any notNamespace='##local' notQName='x:c x:d x:e' minOccurs='1' "
            "maxOccurs='3' processContents='skip'/></xs:sequence>"
            "</xs:restriction></xs:complexContent></xs:complexType>",
            head="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "xmlns:x='http://extra.com/'>",
        )
        assert "particle-restriction" in schema_codes(report)

    def test_split_derived_wildcards_covering_every_name_is_valid(self, parse):
        # wild049: derived ##any excludes everything either base
        # wildcard excludes (and drops the XML-namespace name the base
        # does not admit either)
        report = parse(
            "<xs:complexType name='computer'><xs:all>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##local' notQName='a b c' minOccurs='0' "
            "maxOccurs='2' processContents='skip'/>"
            "<xs:any notNamespace='##local' notQName='x:c x:d x:e' minOccurs='0' "
            "maxOccurs='2' processContents='skip'/></xs:all></xs:complexType>"
            "<xs:complexType name='restrictedComputer'><xs:complexContent>"
            "<xs:restriction base='computer'><xs:sequence>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##any' notQName='a b c d x:c x:d x:e x:f' "
            "minOccurs='1' maxOccurs='2' processContents='skip'/>"
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>",
            head="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "xmlns:x='http://extra.com/'>",
        )
        assert not report.has_errors

    def test_two_split_wildcards_covering_the_base_are_valid(self, parse):
        # wild050: the pair of derived wildcards covers the base
        report = parse(
            "<xs:complexType name='computer'><xs:all>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##any' notQName='a b x:e x:d' minOccurs='2' "
            "maxOccurs='6' processContents='skip'/></xs:all></xs:complexType>"
            "<xs:complexType name='restrictedComputer'><xs:complexContent>"
            "<xs:restriction base='computer'><xs:sequence>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##local' notQName='a b c' minOccurs='1' "
            "maxOccurs='3' processContents='skip'/>"
            "<xs:any notNamespace='##local' notQName='x:c x:d x:e' minOccurs='1' "
            "maxOccurs='3' processContents='skip'/></xs:sequence>"
            "</xs:restriction></xs:complexContent></xs:complexType>",
            head="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "xmlns:x='http://extra.com/'>",
        )
        assert not report.has_errors

    def test_derived_wildcard_shadowing_a_base_element_is_invalid(self, parse):
        # zang's ##local wildcard admits <e/>, which the base type
        # routes to its local element 'e'; the global element 'e' the
        # wildcard would resolve has an incompatible type
        report = parse(
            "<xs:complexType name='zing'><xs:all>"
            "<xs:element name='e' minOccurs='0' maxOccurs='1'>"
            "<xs:simpleType><xs:union memberTypes='xs:date xs:time'/></xs:simpleType>"
            "</xs:element>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/></xs:all></xs:complexType>"
            "<xs:complexType name='zang'><xs:complexContent>"
            "<xs:restriction base='zing'><xs:all>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/></xs:all>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
            "<xs:element name='doc' type='zang'/>"
            "<xs:element name='e' type='xs:duration'/>"
        )
        assert particle_restriction_issues(report)

    def test_derived_wildcard_admitting_a_fully_excluded_namespace_is_invalid(self, parse):
        # The base's bare {urn} entry (x:* here) excludes every local in
        # the extra namespace; the derived ##any wildcard admits them the
        # base excludes
        report = parse(
            "<xs:complexType name='computer'><xs:all>"
            "<xs:any namespace='##any' notQName='x:*' minOccurs='0' "
            "maxOccurs='unbounded' processContents='skip'/></xs:all></xs:complexType>"
            "<xs:complexType name='restricted'><xs:complexContent>"
            "<xs:restriction base='computer'><xs:all>"
            "<xs:any namespace='##any' minOccurs='0' maxOccurs='unbounded' "
            "processContents='skip'/></xs:all></xs:restriction>"
            "</xs:complexContent></xs:complexType>",
            head="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "xmlns:x='http://extra.com/'>",
        )
        assert "particle-restriction" in schema_codes(report)

    def test_sequence_wildcard_shadowing_a_base_element_stays_valid(self, parse):
        # wild068: the same name clash as wild069 in a sequence, but the
        # base's optional element 'e' sits before 'f', so no instance
        # reaches it while the wildcard consumes the name; the static
        # approximation only decides the unordered (all) shape
        report = parse(
            "<xs:complexType name='zing'><xs:sequence>"
            "<xs:element name='e' minOccurs='0' maxOccurs='1'>"
            "<xs:simpleType><xs:union memberTypes='xs:date xs:time'/></xs:simpleType>"
            "</xs:element>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='zang'><xs:complexContent>"
            "<xs:restriction base='zing'><xs:sequence>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/></xs:sequence>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
            "<xs:element name='doc' type='zang'/>"
            "<xs:element name='e' type='xs:duration'/>"
        )
        assert not report.has_errors

    def test_wildcard_shadowing_a_base_element_with_the_same_type_is_valid(self, parse):
        # Same shape as wild069 but the global element's type is the
        # local declaration's type: nothing conflicts.
        report = parse(
            "<xs:complexType name='zing'><xs:all>"
            "<xs:element name='e' minOccurs='0' maxOccurs='1' type='xs:date'/>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/></xs:all></xs:complexType>"
            "<xs:complexType name='zang'><xs:complexContent>"
            "<xs:restriction base='zing'><xs:all>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/></xs:all>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
            "<xs:element name='doc' type='zang'/>"
            "<xs:element name='e' type='xs:date'/>"
        )
        assert not report.has_errors

    def test_skip_wildcard_shadowing_a_base_element_is_exempt(self, parse):
        # wild080's posture: skip wildcards are outside the EDC rule
        report = parse(
            "<xs:complexType name='zing'><xs:all>"
            "<xs:element name='e' minOccurs='0' maxOccurs='1' type='xs:date'/>"
            "<xs:any namespace='##local' processContents='skip'/></xs:all></xs:complexType>"
            "<xs:complexType name='zang'><xs:complexContent>"
            "<xs:restriction base='zing'><xs:all>"
            "<xs:any namespace='##local' processContents='skip'/></xs:all>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
            "<xs:element name='doc' type='zang'/>"
            "<xs:element name='e' type='xs:duration'/>"
        )
        assert not report.has_errors

    def test_defined_element_declaration_valid_controls(self, parse):
        # wild052/wild054 are direct declarations; make sure the new
        # checks do not reject either form
        report = parse(
            "<xs:complexType name='zing'><xs:all>"
            "<xs:element name='name' type='xs:string'/>"
            "<xs:any namespace='##any' notQName='##defined' processContents='skip'/>"
            "</xs:all></xs:complexType>"
            "<xs:element name='zang'/>"
        )
        assert not report.has_errors


# ---------------------------------------------------------------------------
# Rule 4: static tighter EDC (corpus characterization)
# ---------------------------------------------------------------------------


class TestStaticTighterEDC:
    """The static side of cos-element-consistent (XSD 1.1 §3.8.6.3).

    The corpus pins wild075/wild076/wild077/wild080 *schemas* valid: the
    tighter rule only compares {type table}s, and declarations without
    ``xs:alternative`` have no type table. The invalid static shapes
    (wild078/wild079/wild081) and the equivalence case (wild082) need
    ``xs:alternative`` support; the dynamic form (wild075.n1/wild076.n1,
    the EDCWildcard instances) is the instance-phase task.
    """

    def _schema(self, process_contents: str, local_type: str = "xs:integer") -> str:
        return (
            "<xs:complexType name='zing'><xs:sequence>"
            f"<xs:element name='a' type='{local_type}'/>"
            f"<xs:any namespace='##local' processContents='{process_contents}'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:element name='a' type='xs:date'/>"
        )

    def test_strict_wildcard_schema_stays_valid(self, parse):
        # wild075.xsd: expected valid (only the instance is invalid)
        assert not parse(self._schema("strict")).has_errors

    def test_lax_wildcard_schema_stays_valid(self, parse):
        # wild076.xsd: expected valid
        assert not parse(self._schema("lax")).has_errors

    def test_skip_wildcard_schema_stays_valid(self, parse):
        # wild077.xsd: expected valid
        assert not parse(self._schema("skip")).has_errors

    def test_skip_wildcard_with_a_differing_type_table_stays_valid(self, parse):
        # wild080.xsd: expected valid — a skip wildcard is outside the rule
        report = parse(
            "<xs:complexType name='zing'><xs:sequence>"
            "<xs:element name='a' type='xs:date'/>"
            "<xs:any namespace='##local' processContents='skip'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:element name='a'><xs:alternative type='xs:integer'/></xs:element>"
        )
        assert not report.has_errors

    def test_tighter_edc_is_not_reported_for_a_single_type(self, parse):
        # A wildcard and a same-named element particle in *one* content
        # model with conflicting declarations and no type tables stay
        # valid (the corpus reading; only the type-table presence rule
        # is a static constraint).
        report = parse(
            "<xs:complexType name='zing'><xs:sequence>"
            "<xs:element name='a' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='strict'/>"
            "</xs:sequence></xs:complexType>"
        )
        assert not report.has_errors


# ---------------------------------------------------------------------------
# Rule 5: dynamic tighter EDC (corpus characterization)
# ---------------------------------------------------------------------------

_INSTANCE_XSI = (
    "xmlns:xs='http://www.w3.org/2001/XMLSchema' "
    "xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'"
)


class TestDynamicTighterEDC:
    """Element Locally Valid (Complex Type) clause 5, the instance-phase
    counterpart of cos-element-consistent ("dynamic EDC", bug 5970).

    An element admitted by a strict or lax wildcard whose expanded name
    also matches a declaration in the content model is assessed against
    the wildcard-selected (or ``xsi:type``) governing type; that type
    must be the same as, or validly derived from, the model's declared
    type for the name. ``processContents="skip"`` is exempt (skipped
    items have no governing type definition). The corpus shapes are
    condensed from the Saxon ``Wild`` suite (wild062-064, wild067,
    wild068, wild075-077) and IBM EDCWildcard s3_8_6.
    """

    def _doc_body(self, local_e: str, global_e: str, wildcard: str) -> str:
        return (
            "<xs:complexType name='zing'><xs:sequence>"
            f"<xs:element name='e' type='{local_e}'/>"
            "<xs:element name='f' type='xs:string'/>"
            f"<xs:any namespace='##local' processContents='{wildcard}'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:element name='doc' type='zing'/>"
            f"<xs:element name='e' type='{global_e}'/>"
        )

    def test_wild062_n1_unrelated_governing_type_is_invalid(self, validate):
        # wild062.n1: the second e is admitted by the lax wildcard and
        # governed by the global e (xs:time); the local e is xs:date.
        parser = validate(
            XSD_HEAD + self._doc_body("xs:date", "xs:time", "lax") + XSD_TAIL,
            "<doc><e>2008-11-03</e><f/><e>12:20:02</e></doc>",
        )
        assert parser.report.has_errors
        assert "element-consistent" in instance_codes(parser)

    def test_wild062_n2_governing_xsi_type_is_invalid(self, validate):
        # wild062.n2: xsi:type="xs:time" names the governing type, which
        # is still not derived from the local xs:date.
        parser = validate(
            XSD_HEAD + self._doc_body("xs:date", "xs:time", "lax") + XSD_TAIL,
            f"<doc><e>2008-11-03</e><f/><e {_INSTANCE_XSI} xsi:type='xs:time'>12:20:02</e></doc>",
        )
        assert parser.report.has_errors
        assert "element-consistent" in instance_codes(parser)

    def test_wild062_n3_xsi_type_on_a_wildcard_child_is_invalid(self, validate):
        # wild062.n3: the second f has no global declaration; the
        # instance-specified xs:time is the governing type and is not
        # derived from the local f's xs:string.
        parser = validate(
            XSD_HEAD + self._doc_body("xs:date", "xs:time", "lax") + XSD_TAIL,
            f"<doc><e>2008-11-03</e><f/><f {_INSTANCE_XSI} xsi:type='xs:time'>12:20:02</f></doc>",
        )
        assert parser.report.has_errors
        assert "element-consistent" in instance_codes(parser)

    def test_wild062_v1_undeclared_wildcard_child_is_valid(self, validate):
        # wild062.v1: g is undeclared; a lax wildcard skips it, so there
        # is no governing type definition and clause 5 does not apply.
        parser = validate(
            XSD_HEAD + self._doc_body("xs:date", "xs:time", "lax") + XSD_TAIL,
            "<doc><e>2008-11-03</e><f/><g>12:20:02</g></doc>",
        )
        assert not parser.report.has_errors

    def _integer_body(self, wildcard: str = "lax") -> str:
        return (
            "<xs:complexType name='zing'><xs:sequence>"
            "<xs:element name='e' type='xs:integer'/>"
            "<xs:element name='f' type='xs:integer'/>"
            f"<xs:any namespace='##local' processContents='{wildcard}'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:element name='doc' type='zing'/>"
            "<xs:element name='e' type='xs:positiveInteger'/>"
        )

    def test_wild063_n1_governing_value_is_still_validated(self, validate):
        # wild063.n1: positiveInteger is derived from integer, so the
        # EDC clause passes; the lax wildcard still validates -12 against
        # the global positiveInteger it selects. Pins that the global
        # declaration survives a same-named local one.
        parser = validate(
            XSD_HEAD + self._integer_body() + XSD_TAIL,
            "<doc><e>-12</e><f>42</f><e>-12</e></doc>",
        )
        assert parser.report.has_errors

    def test_wild063_v1_derived_governing_type_is_valid(self, validate):
        # wild063.v1: a positive value is fine for the selected global.
        parser = validate(
            XSD_HEAD + self._integer_body() + XSD_TAIL,
            "<doc><e>-12</e><f>42</f><e>12</e></doc>",
        )
        assert not parser.report.has_errors

    def test_wild063_n2_base_governing_type_is_invalid(self, validate):
        # wild063.n2: xs:decimal (a base of xs:integer) does not satisfy
        # the direction of the clause.
        parser = validate(
            XSD_HEAD + self._integer_body() + XSD_TAIL,
            f"<doc><e>-12</e><f>42</f><f {_INSTANCE_XSI} xsi:type='xs:decimal'>12.5</f></doc>",
        )
        assert parser.report.has_errors
        assert "element-consistent" in instance_codes(parser)

    def test_wild063_v2_derived_xsi_type_is_valid(self, validate):
        # wild063.v2: xs:byte is derived from the local xs:integer.
        parser = validate(
            XSD_HEAD + self._integer_body() + XSD_TAIL,
            f"<doc><e>-12</e><f>42</f><f {_INSTANCE_XSI} xsi:type='xs:byte'>3</f></doc>",
        )
        assert not parser.report.has_errors

    def _substitution_body(self) -> str:
        return (
            XSD_HEAD + "<xs:complexType name='zing'><xs:sequence>"
            "<xs:element name='e' type='xs:integer'/>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:element name='doc' type='zing'/>"
            "<xs:element name='e' type='xs:decimal'/>"
            "<xs:element name='g' substitutionGroup='e' type='xs:byte'/>" + XSD_TAIL
        )

    def test_wild064_n1_base_governing_type_is_invalid(self, validate):
        # wild064.n1: the wildcard-selected global e is xs:decimal, a
        # base of the local xs:integer (93.7 is a valid decimal, so the
        # EDC clause is the deciding rule).
        parser = validate(
            self._substitution_body(),
            "<doc><e>-12</e><f>42</f><e>93.7</e></doc>",
        )
        assert parser.report.has_errors
        assert "element-consistent" in instance_codes(parser)

    def test_wild064_v1_substitution_member_is_valid(self, validate):
        # wild064.v1: g is implicitly contained (it is in e's
        # substitution group) and both its governing and locally
        # declared type are xs:byte.
        parser = validate(
            self._substitution_body(),
            "<doc><e>-12</e><f>42</f><g>6</g></doc>",
        )
        assert not parser.report.has_errors

    def test_wild064_v2_xsi_type_derived_from_the_local_type_is_valid(self, validate):
        # wild064.v2: xs:int is derived from the local xs:integer even
        # though the selected global e is xs:decimal.
        parser = validate(
            self._substitution_body(),
            f"<doc><e>-12</e><f>42</f><e {_INSTANCE_XSI} xsi:type='xs:int'>93</e></doc>",
        )
        assert not parser.report.has_errors

    def test_wild066_v1_union_member_governing_type_is_valid(self, validate):
        # wild066.v1: the governing global e is xs:date, a member of the
        # locally declared union(xs:date, xs:time), so the clause is
        # satisfied (the union analogue of the derived-type direction).
        parser = validate(
            XSD_HEAD + "<xs:complexType name='zing'><xs:sequence>"
            "<xs:element name='e'><xs:simpleType>"
            "<xs:union memberTypes='xs:date xs:time'/>"
            "</xs:simpleType></xs:element>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:element name='doc' type='zing'/>"
            "<xs:element name='e' type='xs:date'/>" + XSD_TAIL,
            "<doc><e>12:12:00</e><f>42</f><e>2008-11-02</e></doc>",
        )
        assert not parser.report.has_errors

    def test_wild067_union_governing_type_is_invalid(self, validate):
        # wild067.n1: xs:duration is not a member of the local union.
        parser = validate(
            XSD_HEAD + "<xs:complexType name='zing'><xs:sequence>"
            "<xs:element name='e'><xs:simpleType>"
            "<xs:union memberTypes='xs:date xs:time'/>"
            "</xs:simpleType></xs:element>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:element name='doc' type='zing'/>"
            "<xs:element name='e' type='xs:duration'/>" + XSD_TAIL,
            "<doc><e>12:12:00</e><f>42</f><e>PT12H</e></doc>",
        )
        assert parser.report.has_errors
        assert "element-consistent" in instance_codes(parser)

    def test_wild068_base_type_particle_is_still_locally_declared(self, validate):
        # wild068.n1: zang's restriction drops the e particle, but the
        # locally declared type recurses to the base type (zing) before
        # it is absent, so the duration governing type is still
        # inconsistent.
        parser = validate(
            XSD_HEAD + "<xs:complexType name='zing'><xs:sequence>"
            "<xs:element name='e' minOccurs='0'><xs:simpleType>"
            "<xs:union memberTypes='xs:date xs:time'/>"
            "</xs:simpleType></xs:element>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:complexType name='zang'><xs:complexContent>"
            "<xs:restriction base='zing'><xs:sequence>"
            "<xs:element name='f' type='xs:integer'/>"
            "<xs:any namespace='##local' processContents='lax'/>"
            "</xs:sequence></xs:restriction>"
            "</xs:complexContent></xs:complexType>"
            "<xs:element name='doc' type='zang'/>"
            "<xs:element name='e' type='xs:duration'/>" + XSD_TAIL,
            "<doc><f>42</f><e>PT12H</e></doc>",
        )
        assert parser.report.has_errors
        assert "element-consistent" in instance_codes(parser)

    def _wild075_body(self, process_contents: str) -> str:
        return (
            XSD_HEAD + "<xs:element name='root' type='zing'/>"
            "<xs:complexType name='zing'><xs:sequence>"
            "<xs:element name='a' type='xs:integer'/>"
            f"<xs:any namespace='##local' processContents='{process_contents}'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:element name='a' type='xs:date'/>" + XSD_TAIL
        )

    def test_wild075_strict_wildcard_governing_type_is_invalid(self, validate):
        # wild075.n1 (also wild076.n1's instance, via the test-set's
        # shared href): the second a is governed by the global a
        # (xs:date) and the local a is xs:integer.
        parser = validate(
            self._wild075_body("strict"),
            "<root><a>23</a><a>2010-10-16</a></root>",
        )
        assert parser.report.has_errors
        assert "element-consistent" in instance_codes(parser)

    def test_wild076_lax_wildcard_governing_type_is_invalid(self, validate):
        # wild076.n1 against wild076.xsd: same shape with a lax wildcard.
        parser = validate(
            self._wild075_body("lax"),
            "<root><a>23</a><a>2010-10-16</a></root>",
        )
        assert parser.report.has_errors
        assert "element-consistent" in instance_codes(parser)

    def test_wild077_skip_wildcard_is_exempt_from_the_edc_clause(self, validate):
        # wild077/wild080: a skip wildcard leaves the second a with no
        # governing type definition, so the clause does not fire.
        parser = validate(
            self._wild075_body("skip"),
            "<root><a>23</a><a>2010-10-16</a></root>",
        )
        assert not parser.report.has_errors

    def _edc_wildcard_body(self) -> str:
        return (
            "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'"
            " targetNamespace='urn:b' xmlns:b='urn:b'"
            " elementFormDefault='qualified'>"
            "<xs:complexType name='t'><xs:sequence>"
            "<xs:element name='x' type='xs:string' minOccurs='0'/>"
            "<xs:any namespace='urn:b' processContents='lax' minOccurs='0'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:element name='x' type='xs:integer'/>"
            "<xs:element name='root' type='b:t'/>"
            "</xs:schema>"
        )

    def test_s3_8_6_v01_governing_integer_against_local_string_is_invalid(self, validate):
        # IBM EDCWildcard s3_8_6v01i. The test metadata's expectation
        # was changed to "invalid" in response to bug #12130 (the
        # directory name "valid" predates that): the lax wildcard
        # selects the global xs:integer x, and the same-named local
        # particle is xs:string. The brief's "v01 valid" is inverted;
        # the corpus wins.
        parser = validate(
            self._edc_wildcard_body(),
            "<root xmlns='urn:b'><x>a</x><x>3</x></root>",
        )
        assert parser.report.has_errors
        assert "element-consistent" in instance_codes(parser)

    def test_s3_8_6_ii01_governing_integer_value_is_invalid(self, validate):
        # s3_8_6ii01i: the same EDC violation, and "v" is not an
        # integer either.
        parser = validate(
            self._edc_wildcard_body(),
            "<root xmlns='urn:b'><x>a</x><x>v</x></root>",
        )
        assert parser.report.has_errors

    def test_governing_type_derived_from_the_local_type_is_valid(self, validate):
        # A valid control: the global x is xs:string, so the same-named
        # local particle's type is not violated by the wildcard-selected
        # declaration.
        parser = validate(
            "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'"
            " targetNamespace='urn:b' xmlns:b='urn:b'"
            " elementFormDefault='qualified'>"
            "<xs:complexType name='t'><xs:sequence>"
            "<xs:element name='x' type='xs:string' minOccurs='0'/>"
            "<xs:any namespace='urn:b' processContents='lax' minOccurs='0'/>"
            "</xs:sequence></xs:complexType>"
            "<xs:element name='x' type='xs:token'/>"
            "<xs:element name='root' type='b:t'/>"
            "</xs:schema>",
            "<root xmlns='urn:b'><x>a</x><x>3</x></root>",
        )
        assert not parser.report.has_errors
