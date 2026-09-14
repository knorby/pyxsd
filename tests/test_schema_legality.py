"""Schema-legality child-grammar checks.

The parser reports illegal, duplicated and misordered child elements of
declarations that carry a table-driven child grammar. These tests pin the
public issue codes and the base mechanism that later tables build on.
"""

import io

import pytest

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD


def _schema_codes(report) -> set[str]:
    return {issue.code for issue in report.for_phase("schema")}


@pytest.fixture
def parse_schema(tmp_path, monkeypatch):
    """Parse a schema without an instance and return the report.

    Mirrors ``tests/xsts/drivers.py::_schema_only_call``: the instance
    phase is stubbed out so a schema declaring no root element can still
    be inspected.
    """
    monkeypatch.setattr(PyXSD, "parseXML", lambda self: None)
    schema_path = tmp_path / "schema.xsd"

    def _parse(schema_string: str):
        schema_path.write_text(schema_string, encoding="utf-8")
        return PyXSD(
            io.StringIO("<pyxsd-schema-probe/>"),
            str(schema_path),
            xmlFileOutput=False,
            mode=ParseModes.NAMESPACED,
        ).report

    return _parse


class TestChildGrammarInfra:
    def test_unknown_child_reports_declaration_child(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'><xsd:complexType name='t'>"
            "<xsd:bogus/></xsd:complexType></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_duplicate_single_child_reports_declaration_duplicate(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'><xsd:complexType name='t'>"
            "<xsd:simpleContent><xsd:extension base='xsd:string'/></xsd:simpleContent>"
            "<xsd:simpleContent><xsd:extension base='xsd:string'/></xsd:simpleContent>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_annotation_not_first_reports_declaration_order(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'><xsd:complexType name='t'>"
            "<xsd:simpleContent><xsd:extension base='xsd:string'/></xsd:simpleContent>"
            "<xsd:annotation><xsd:documentation/></xsd:annotation>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-order" in _schema_codes(report)

    def test_permissive_body_does_not_report_annotation_order(self, parse_schema):
        """Arbitrary foreign XML inside an ``appinfo``/``documentation``
        body must not emit a grammar-order error: those bodies are
        permissive, not schema components.
        """
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema' xmlns:f='urn:foreign'>"
            "<xsd:annotation>"
            "<xsd:appinfo><f:annotation/></xsd:appinfo>"
            "<xsd:documentation><f:annotation/></xsd:documentation>"
            "</xsd:annotation></xsd:schema>"
        )
        assert "declaration-order" not in _schema_codes(report)

    def test_foreign_annotation_is_not_a_schema_child(self, parse_schema):
        """A foreign-namespaced element whose local name is ``annotation``
        is not an XSD annotation: it is an illegal child of a complexType,
        but must not drive the annotation-order check.
        """
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema' xmlns:f='urn:foreign'>"
            "<xsd:complexType name='t'>"
            "<xsd:simpleContent><xsd:extension base='xsd:string'/></xsd:simpleContent>"
            "<f:annotation/>"
            "</xsd:complexType></xsd:schema>"
        )
        codes = _schema_codes(report)
        assert "declaration-child" in codes
        assert "declaration-order" not in codes

    def test_untabulated_container_reports_annotation_order(self, parse_schema):
        """A container with no grammar table yet (``sequence``) still
        enforces annotation-first: the check must not be gated on the
        presence of a table.
        """
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:sequence>"
            "<xsd:element name='a' type='xsd:string'/>"
            "<xsd:annotation><xsd:documentation/></xsd:annotation>"
            "</xsd:sequence></xsd:complexType></xsd:schema>"
        )
        assert "declaration-order" in _schema_codes(report)


class TestContainerGrammar:
    def test_notation_non_annotation_child_reports_declaration_child(self, parse_schema):
        """notatF002: ``all`` is not a legal child of ``notation``."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='jpeg' public='image/jpeg' system='viewer.exe'>"
            "<xsd:all/>"
            "</xsd:notation></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_notation_foreign_child_reports_declaration_child(self, parse_schema):
        """notatG002: arbitrary foreign XML is not legal notation content."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='jpeg' public='image/jpeg' system='viewer.exe'>"
            "<a><b/></a>"
            "</xsd:notation></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_notation_allows_annotation_child(self, parse_schema):
        """notatF004: an annotation is the one legal notation child."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='foo' public='jpeg' system='system.exe'>"
            "<xsd:annotation/>"
            "</xsd:notation></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_notation_duplicate_annotation_reports_declaration_duplicate(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='foo' public='jpeg' system='system.exe'>"
            "<xsd:annotation/><xsd:annotation/>"
            "</xsd:notation></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_nested_annotation_reports_declaration_child(self, parse_schema):
        """annotB001: an annotation may not contain another annotation."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation>"
            "<xsd:annotation><xsd:appinfo>Application Information</xsd:appinfo></xsd:annotation>"
            "<xsd:appinfo>Application Information</xsd:appinfo>"
            "<xsd:documentation>Documentation</xsd:documentation>"
            "</xsd:annotation></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_annotation_allows_appinfo_and_documentation(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation>"
            "<xsd:appinfo>Application Information</xsd:appinfo>"
            "<xsd:documentation>Documentation</xsd:documentation>"
            "</xsd:annotation></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_schema_unknown_child_reports_declaration_child(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'><xsd:bogus/></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_schema_allows_repeated_annotations(self, parse_schema):
        """A schema may carry more than one top-level annotation (each
        declaration may be preceded by one)."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation/><xsd:annotation/>"
            "<xsd:element name='root' type='xsd:string'/>"
            "</xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_schema_allows_trailing_annotation(self, parse_schema):
        """A top-level annotation may also follow the declarations."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root' type='xsd:string'/>"
            "<xsd:annotation/>"
            "</xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_schema_annotation_first_is_clean(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation><xsd:documentation/></xsd:annotation>"
            "<xsd:element name='root' type='xsd:string'/>"
            "</xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)


class TestTypeDefinitionGrammar:
    def test_simple_type_direct_facet_reports_declaration_child(self, parse_schema):
        """A facet is not legal directly inside ``simpleType``; it belongs
        in a ``restriction`` (msData stF facets-outside-restriction)."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:length value='3'/></xsd:simpleType>"
            "</xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_simple_type_restriction_then_list_reports_declaration_order(self, parse_schema):
        """stB019: a simpleType cannot combine restriction and list."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'>"
            "<xsd:restriction base='xsd:string'/><xsd:list itemType='xsd:string'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "declaration-order" in _schema_codes(report)

    def test_simple_type_duplicate_restriction_reports_declaration_duplicate(self, parse_schema):
        """stB004: at most one derivation child of a simpleType."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'>"
            "<xsd:restriction base='xsd:string'/><xsd:restriction base='xsd:string'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_list_duplicate_simple_type_reports_declaration_duplicate(self, parse_schema):
        """stD004: a list may declare at most one inline item type."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:list>"
            "<xsd:simpleType><xsd:restriction base='xsd:string'/></xsd:simpleType>"
            "<xsd:simpleType><xsd:restriction base='xsd:string'/></xsd:simpleType>"
            "</xsd:list></xsd:simpleType></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_union_multiple_simple_types_is_clean(self, parse_schema):
        """A union may declare several inline member types (stE014); the
        list-style max-one rule must not apply to ``union``."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:union>"
            "<xsd:simpleType><xsd:restriction base='xsd:string'/></xsd:simpleType>"
            "<xsd:simpleType><xsd:restriction base='xsd:int'/></xsd:simpleType>"
            "</xsd:union></xsd:simpleType></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_list_facet_child_reports_declaration_child(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'>"
            "<xsd:list itemType='xsd:string'><xsd:length value='3'/></xsd:list>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_restriction_unknown_facet_reports_declaration_child(self, parse_schema):
        """stF007: ``duration`` is not a constraining facet (msData facet
        set), so a restriction may not carry it."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:string'>"
            "<xsd:duration value='P1D'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_extension_facet_child_reports_declaration_child(self, parse_schema):
        """ctE005: an extension may not carry a constraining facet."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:complexContent>"
            "<xsd:extension base='xsd:anyType'><xsd:length value='3'/></xsd:extension>"
            "</xsd:complexContent></xsd:complexType></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_restriction_two_particles_reports_declaration_duplicate(self, parse_schema):
        """ctG027/ctB063: a restriction holds a single particle; ``choice``
        and ``group`` are two distinct tags in the same slot."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:complexContent>"
            "<xsd:restriction base='xsd:anyType'><xsd:choice/><xsd:group ref='g'/>"
            "</xsd:restriction></xsd:complexContent></xsd:complexType></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_extension_two_particles_reports_declaration_duplicate(self, parse_schema):
        """ctH003: an extension holds a single particle; two ``group``
        references repeat the one particle tag."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:complexContent>"
            "<xsd:extension base='xsd:anyType'>"
            "<xsd:group ref='g'/><xsd:group ref='h'/>"
            "</xsd:extension></xsd:complexContent></xsd:complexType></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_complex_type_simple_and_complex_content_reports_declaration_duplicate(
        self, parse_schema
    ):
        """ctB006/ctB019: the two content kinds are mutually exclusive even
        though each appears only once."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'>"
            "<xsd:simpleContent/><xsd:complexContent/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_complex_type_choice_and_group_reports_declaration_duplicate(self, parse_schema):
        """ctB063: a complexType holds a single particle; ``choice`` and
        ``group`` are distinct tags in the particle slot."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:choice/><xsd:group ref='g'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_complex_content_restriction_and_extension_reports_declaration_duplicate(
        self, parse_schema
    ):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:complexContent>"
            "<xsd:restriction base='xsd:anyType'/><xsd:extension base='xsd:anyType'/>"
            "</xsd:complexContent></xsd:complexType></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_restriction_valid_particle_with_attributes_is_clean(self, parse_schema):
        """ctG007: a particle followed by attributes is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:complexContent>"
            "<xsd:restriction base='xsd:anyType'>"
            "<xsd:choice/><xsd:attribute name='a'/><xsd:anyAttribute/>"
            "</xsd:restriction></xsd:complexContent></xsd:complexType></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_extension_valid_group_with_any_attribute_is_clean(self, parse_schema):
        """ctH001/ctH082: a particle plus attributes is legal in an extension."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:complexContent>"
            "<xsd:extension base='xsd:anyType'>"
            "<xsd:group ref='g'/><xsd:attribute name='a'/><xsd:anyAttribute/>"
            "</xsd:extension></xsd:complexContent></xsd:complexType></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_restriction_allows_open_content(self, parse_schema):
        """saxonData Open/open015: an XSD 1.1 restriction may carry an
        ``openContent`` before its particle."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:complexContent>"
            "<xsd:restriction base='xsd:anyType'>"
            "<xsd:openContent mode='suffix'><xsd:any namespace='##any'/></xsd:openContent>"
            "<xsd:sequence/>"
            "</xsd:restriction></xsd:complexContent></xsd:complexType></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_simple_type_restriction_assertion_facet_is_allowed(self, parse_schema):
        """XSD 1.1 ``assertion`` is a constraining facet of a restriction
        (ibmData assertion tests); it must not be reported as illegal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:string'>"
            "<xsd:assertion test='true()'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "declaration-child" not in _schema_codes(report)


class TestDeclarationContainerGrammar:
    def test_attribute_attribute_child_reports_declaration_child(self, parse_schema):
        """attP001: an attribute declaration cannot contain another
        attribute."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='att'>"
            "<xsd:attribute name='att1' type='xsd:string'/>"
            "</xsd:attribute></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_attribute_element_child_reports_declaration_child(self, parse_schema):
        """attP002: an element is not a legal attribute child."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='att'><xsd:element name='elem'/>"
            "</xsd:attribute></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_attribute_complex_type_child_reports_declaration_child(self, parse_schema):
        """attQ006: a complexType is not a legal attribute child."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='att'><xsd:complexType name='foo'/>"
            "</xsd:attribute></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_attribute_simple_type_is_clean(self, parse_schema):
        """The one legal type child of an attribute is an inline
        simpleType."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='att'>"
            "<xsd:simpleType><xsd:restriction base='xsd:string'/></xsd:simpleType>"
            "</xsd:attribute></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_element_attribute_child_reports_declaration_child(self, parse_schema):
        """attQ002: an attribute declaration is not a legal element child."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='e'><xsd:attribute name='att'/>"
            "</xsd:element></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_element_notation_child_reports_declaration_child(self, parse_schema):
        """notatF023: a notation is not a legal element child."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='e'>"
            "<xsd:notation name='n' public='p' system='s'/>"
            "</xsd:element></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_element_group_child_reports_declaration_child(self, parse_schema):
        """groupO024: a group reference is not a legal element child."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:group name='A'><xsd:sequence/></xsd:group>"
            "<xsd:element name='e'><xsd:group ref='A'/></xsd:element>"
            "</xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_element_two_inline_types_reports_declaration_duplicate(self, parse_schema):
        """An element may carry at most one inline type (simpleType or
        complexType)."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='e'>"
            "<xsd:simpleType><xsd:restriction base='xsd:string'/></xsd:simpleType>"
            "<xsd:complexType/>"
            "</xsd:element></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_element_inline_type_and_identity_constraint_is_clean(self, parse_schema):
        """A valid element combines an inline type with identity
        constraints."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='e'>"
            "<xsd:complexType><xsd:sequence><xsd:element name='a'/></xsd:sequence>"
            "</xsd:complexType>"
            "<xsd:key name='k'><xsd:selector xpath='a'/><xsd:field xpath='.'/></xsd:key>"
            "</xsd:element></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_group_duplicate_particle_reports_declaration_duplicate(self, parse_schema):
        """addB083: a global group holds exactly one particle; two
        ``choice`` children repeat it."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:group name='G'><xsd:choice/><xsd:choice/></xsd:group>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_group_distinct_particles_reports_declaration_duplicate(self, parse_schema):
        """A global group cannot combine two different particles."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:group name='G'><xsd:choice/><xsd:sequence/></xsd:group>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_group_attribute_child_reports_declaration_child(self, parse_schema):
        """groupO013: an attribute is not a legal group child."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:group name='G'><xsd:sequence/><xsd:attribute name='a'/>"
            "</xsd:group></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_group_single_particle_is_clean(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:group name='G'><xsd:annotation/><xsd:sequence>"
            "<xsd:element name='a'/></xsd:sequence></xsd:group></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_attribute_group_group_child_reports_declaration_child(self, parse_schema):
        """groupO025: a group reference is not a legal attributeGroup
        child."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:group name='foo'><xsd:sequence/></xsd:group>"
            "<xsd:attributeGroup name='ag'><xsd:group ref='foo'/>"
            "<xsd:attribute name='att'/></xsd:attributeGroup></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_attribute_group_notation_child_reports_declaration_child(self, parse_schema):
        """notatF013: a notation is not a legal attributeGroup child."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attributeGroup name='ag'>"
            "<xsd:notation name='n' public='p' system='s'/>"
            "</xsd:attributeGroup></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_attribute_group_attributes_and_wildcard_are_clean(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attributeGroup name='inner'><xsd:attribute name='b'/>"
            "</xsd:attributeGroup>"
            "<xsd:attributeGroup name='ag'><xsd:annotation/>"
            "<xsd:attribute name='a'/><xsd:attributeGroup ref='inner'/>"
            "<xsd:anyAttribute/></xsd:attributeGroup></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)
