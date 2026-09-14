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


class TestAttributeDeclarationLegality:
    """Semantic attribute-declaration legality.

    Mirrors the W3C ``msData/attribute`` family (attKa/Kb/Kc, attF, attO):
    ``default``/``fixed`` consistency, ``use`` legality, global-only
    attributes, ref conflicts and default/fixed value checking.
    """

    def test_attribute_default_and_fixed_reports_declaration_attribute(self, parse_schema):
        """attKa001: default and fixed must not both be present."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='ga' type='xsd:integer' fixed='abc' default='abc'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_group_default_and_fixed_reports_declaration_attribute(self, parse_schema):
        """attKb001: same constraint for an attributeGroup member."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attributeGroup name='ag'>"
            "<xsd:attribute name='aga' default='abc' fixed='abc'/>"
            "</xsd:attributeGroup></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_in_complex_type_default_and_fixed_reports(self, parse_schema):
        """attKc001: same constraint for a complexType attribute use."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' default='abc' fixed='abc'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_use_prohibited_with_default_reports(self, parse_schema):
        """attKb005/attKc005: a prohibited use cannot also declare a default."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' use='prohibited' default='abc'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_use_prohibited_with_fixed_reports(self, parse_schema):
        """attKb009/attKc009: XSD 1.1 forbids a prohibited use with fixed."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' use='prohibited' fixed='abc'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_required_with_default_reports(self, parse_schema):
        """attKb004/attKc004: use must be optional when default is present."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' use='required' default='abc'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_bad_use_value_reports(self, parse_schema):
        """attF007: ``use`` must be optional, required or prohibited."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' use='foo'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_global_attribute_use_reports(self, parse_schema):
        """attO013/attO019: a global attribute must not carry ``use``."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='ga' use='required'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_global_attribute_form_reports(self, parse_schema):
        """attA001: a global attribute must not carry ``form``."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='ga' form='unqualified'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_default_invalid_for_type_reports(self, parse_schema):
        """attO003: default='abc' is not a valid xsd:integer."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' type='xsd:integer' default='abc'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_fixed_invalid_for_type_reports(self, parse_schema):
        """attO002: fixed='abc' is not a valid xsd:integer."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' type='xsd:integer' fixed='abc'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_ref_with_type_reports(self, parse_schema):
        """attKc013: a ref use must not also carry ``type``."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='ga' type='xsd:string'/>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute ref='ga' type='xsd:string'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_type_and_simple_type_reports(self, parse_schema):
        """attKc014: ``type`` and an inline simpleType are mutually exclusive."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' type='xsd:string'>"
            "<xsd:simpleType><xsd:restriction base='xsd:string'/></xsd:simpleType>"
            "</xsd:attribute></xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_invalid_name_reports(self, parse_schema):
        """attC009: a name containing two colons is not an NCName."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='a:b:b' type='xsd:string'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_invalid_id_reports(self, parse_schema):
        """attB006/notatA005: ``id`` must be a valid NCName."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='foo' type='xsd:string' id='0'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_non_simple_type_reports(self, parse_schema):
        """attD002: an attribute's type must be a simple type."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'/>"
            "<xsd:attribute name='bar' type='ct'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_global_attribute_default_is_clean(self, parse_schema):
        """attKa002: a global attribute may carry a default."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='ga' default='abc'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_local_attribute_optional_default_is_clean(self, parse_schema):
        """attKb003: use='optional' with default is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' use='optional' default='abc'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_attribute_valid_integer_default_is_clean(self, parse_schema):
        """attO006: a lexically valid integer default is accepted."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' type='xsd:integer' fixed=' 123 '/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_attribute_required_with_fixed_is_clean(self, parse_schema):
        """use='required' with a fixed value is legal (only prohibited conflicts)."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='ct'>"
            "<xsd:attribute name='ca' type='xsd:string' use='required' fixed='1.1'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_attribute_ref_without_conflicts_is_clean(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='ga' type='xsd:string'/>"
            "<xsd:complexType name='ct'><xsd:attribute ref='ga' use='required'/>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_attribute_fixed_invalid_for_user_simple_type_reports(self, parse_schema):
        """attP006: fixed='' must be valid for the named enumeration type."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'"
            " targetNamespace='urn:test' xmlns:t='urn:test'>"
            "<xsd:simpleType name='mySimpleType'><xsd:restriction base='xsd:int'>"
            "<xsd:enumeration value='1'/><xsd:enumeration value='2'/>"
            "</xsd:restriction></xsd:simpleType>"
            "<xsd:complexType name='ct'><xsd:attribute name='att'"
            " type='t:mySimpleType' fixed=''/></xsd:complexType>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_default_valid_for_user_simple_type_is_clean(self, parse_schema):
        """A default within the named enumeration type's value space is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'"
            " targetNamespace='urn:test' xmlns:t='urn:test'>"
            "<xsd:simpleType name='mySimpleType'><xsd:restriction base='xsd:int'>"
            "<xsd:enumeration value='1'/><xsd:enumeration value='2'/>"
            "</xsd:restriction></xsd:simpleType>"
            "<xsd:complexType name='ct'><xsd:attribute name='att'"
            " type='t:mySimpleType' default='2'/></xsd:complexType>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_attribute_group_standalone_default_invalid_reports(self, parse_schema):
        """A default in an unreferenced global attributeGroup is still validated."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attributeGroup name='ag'>"
            "<xsd:attribute name='a' type='xsd:boolean' default='Yes'/>"
            "</xsd:attributeGroup></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_attribute_group_standalone_default_valid_is_clean(self, parse_schema):
        """A valid default in an unreferenced global attributeGroup is clean."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attributeGroup name='ag'>"
            "<xsd:attribute name='a' type='xsd:boolean' default='true'/>"
            "</xsd:attributeGroup></xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)


class TestElementDeclarationLegality:
    """Semantic element-declaration legality (elemC/elemF/elemJ, schZ)."""

    def test_element_bad_final_token_reports(self, parse_schema):
        """elemF009: ``foo`` is not a legal final token."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' final='foo'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_final_substitution_token_reports(self, parse_schema):
        """elemF004: ``substitution`` is not legal in an element ``final``."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' final='substitution'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_final_all_with_tokens_reports(self, parse_schema):
        """elemF014: ``#all`` cannot be combined with other tokens."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' final='#all extension restriction'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_bad_block_token_reports(self, parse_schema):
        """elemC009: ``foo`` is not a legal block token."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' block='foo'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_block_all_with_tokens_reports(self, parse_schema):
        """elemC014: ``#all`` cannot be combined with other tokens."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' block='#all extension restriction substitution'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_min_occurs_greater_than_max_reports(self, parse_schema):
        """elemJ019: minOccurs must not exceed maxOccurs."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' type='bar'/>"
            "<xsd:complexType name='bar'><xsd:sequence>"
            "<xsd:element name='name' minOccurs='2' maxOccurs='1'/>"
            "</xsd:sequence></xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_local_element_abstract_reports(self, parse_schema):
        """schZ001: a local element must not carry ``abstract``."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root'><xsd:complexType><xsd:all>"
            "<xsd:element name='noAbstract' type='xsd:string' abstract='true'/>"
            "</xsd:all></xsd:complexType></xsd:element></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_local_element_final_reports(self, parse_schema):
        """schZ002: a local element must not carry ``final``."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root'><xsd:complexType><xsd:all>"
            "<xsd:element name='noFinal' type='xsd:string' final='restriction'/>"
            "</xsd:all></xsd:complexType></xsd:element></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_local_element_substitution_group_reports(self, parse_schema):
        """schZ003: a local element must not carry ``substitutionGroup``."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root'><xsd:complexType><xsd:all>"
            "<xsd:element name='noSub' type='xsd:string' substitutionGroup='elt'/>"
            "</xsd:all></xsd:complexType></xsd:element></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_final_all_is_clean(self, parse_schema):
        """elemF001: final='#all' is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' final='#all'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_element_final_extension_restriction_is_clean(self, parse_schema):
        """elemF005: final='extension restriction' is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' final='extension restriction'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_element_block_all_tokens_is_clean(self, parse_schema):
        """elemC: block may list extension, restriction and substitution."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' block='extension restriction substitution'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_element_occurs_min_le_max_is_clean(self, parse_schema):
        """elemJ018: minOccurs='1' maxOccurs='2' is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' type='bar'/>"
            "<xsd:complexType name='bar'><xsd:sequence>"
            "<xsd:element name='name' minOccurs='1' maxOccurs='2'/>"
            "</xsd:sequence></xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_global_element_abstract_is_clean(self, parse_schema):
        """``abstract`` is reserved for global element declarations."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' abstract='true'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_element_default_invalid_for_boolean_reports(self, parse_schema):
        """valueConstraint00401m2: default 'Yes' is not a valid boolean."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='E' type='xsd:boolean' default='Yes'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_fixed_invalid_for_float_reports(self, parse_schema):
        """valueConstraint00601m2: fixed '1.0F-2' is not a valid float."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root' type='xsd:float' fixed='1.0F-2'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_default_and_fixed_together_reports(self, parse_schema):
        """valueConstraint00301m3: default and fixed must not both be present."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root' type='xsd:string' default='0' fixed='0'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_default_invalid_for_restricted_simple_type_reports(self, parse_schema):
        """valueConstraint00401m8: the value must match the user type's facets."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='E' type='answer' default='false'/>"
            "<xsd:simpleType name='answer'><xsd:restriction base='xsd:boolean'>"
            "<xsd:pattern value='true'/></xsd:restriction></xsd:simpleType>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_default_valid_for_restricted_simple_type_is_clean(self, parse_schema):
        """valueConstraint00401m7: 'true' matches the user type's facets."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='E' type='answer' default='true'/>"
            "<xsd:simpleType name='answer'><xsd:restriction base='xsd:boolean'>"
            "<xsd:pattern value='true'/></xsd:restriction></xsd:simpleType>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_element_default_invalid_for_simple_content_complex_type_reports(self, parse_schema):
        """valueConstraint00401m6: simple-content base is boolean, so 'Yes' fails."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='E' type='answer' default='Yes'/>"
            "<xsd:complexType name='answer'><xsd:simpleContent>"
            "<xsd:extension base='xsd:boolean'>"
            "<xsd:attribute name='certainty'/>"
            "</xsd:extension></xsd:simpleContent></xsd:complexType>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_default_valid_for_simple_content_complex_type_is_clean(self, parse_schema):
        """valueConstraint00401m5: 'true' is valid for the boolean content type."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='E' type='answer' default='true'/>"
            "<xsd:complexType name='answer'><xsd:simpleContent>"
            "<xsd:extension base='xsd:boolean'>"
            "<xsd:attribute name='certainty'/>"
            "</xsd:extension></xsd:simpleContent></xsd:complexType>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_element_default_for_any_type_is_clean(self, parse_schema):
        """valueConstraint00401m3/m4: the ur-type accepts any lexical value."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='E' default='alpha'/>"
            "<xsd:element name='F' type='xsd:anyType' default='alpha'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_element_type_with_inline_complex_type_reports(self, parse_schema):
        """typeDef00501m2: an element may not carry both type and an inline type."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root' type='Type'>"
            "<xsd:complexType><xsd:sequence>"
            "<xsd:element name='Local' minOccurs='0'/>"
            "</xsd:sequence></xsd:complexType></xsd:element>"
            "<xsd:complexType name='Type'><xsd:sequence>"
            "<xsd:element name='Local' minOccurs='0'/>"
            "</xsd:sequence></xsd:complexType></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_type_with_inline_simple_type_reports(self, parse_schema):
        """typeDef00502m2: an element may not carry both type and a simpleType."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root' type='Type'>"
            "<xsd:simpleType><xsd:restriction base='xsd:boolean'>"
            "<xsd:pattern value='false'/>"
            "</xsd:restriction></xsd:simpleType></xsd:element>"
            "<xsd:simpleType name='Type'><xsd:restriction base='xsd:boolean'>"
            "<xsd:pattern value='false'/></xsd:restriction></xsd:simpleType>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_element_type_with_inline_type_only_inline_is_clean(self, parse_schema):
        """typeDef00501m1: an inline type alone (no type attribute) is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root'>"
            "<xsd:complexType><xsd:sequence>"
            "<xsd:element name='Local' minOccurs='0'/>"
            "</xsd:sequence></xsd:complexType></xsd:element>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)


class TestSubstitutionGroupLegality:
    """Schema-side substitution-group constraints (final/exclusions/cycles)."""

    def test_member_extension_blocked_by_head_final_reports(self, parse_schema):
        """substGrpExcl00202m2: head final='extension' forbids the extension member."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='Head' type='HeadType' final='extension'/>"
            "<xsd:complexType name='HeadType'><xsd:sequence>"
            "<xsd:element name='Ear'/></xsd:sequence></xsd:complexType>"
            "<xsd:element name='Member3' substitutionGroup='Head'>"
            "<xsd:complexType><xsd:complexContent>"
            "<xsd:extension base='HeadType'><xsd:sequence>"
            "<xsd:element name='Nose'/></xsd:sequence></xsd:extension>"
            "</xsd:complexContent></xsd:complexType></xsd:element>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_member_extension_without_head_final_is_clean(self, parse_schema):
        """substGrpExcl00202m1: without a head final the extension member is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='Head' type='HeadType'/>"
            "<xsd:complexType name='HeadType'><xsd:sequence>"
            "<xsd:element name='Ear'/></xsd:sequence></xsd:complexType>"
            "<xsd:element name='Member3' substitutionGroup='Head'>"
            "<xsd:complexType><xsd:complexContent>"
            "<xsd:extension base='HeadType'><xsd:sequence>"
            "<xsd:element name='Nose'/></xsd:sequence></xsd:extension>"
            "</xsd:complexContent></xsd:complexType></xsd:element>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_member_restriction_blocked_by_block_is_schema_valid(self, parse_schema):
        """disallowedSubst00501m2: block (not final) leaves the schema valid."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='Head' type='xsd:string' block='restriction'/>"
            "<xsd:simpleType name='derivedFromString'>"
            "<xsd:restriction base='xsd:string'/></xsd:simpleType>"
            "<xsd:element name='Member1' type='derivedFromString' substitutionGroup='Head'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_cyclic_substitution_group_reports(self, parse_schema):
        """xsd009.e: a substitution-group cycle is an invalid schema."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='foo' substitutionGroup='bar'/>"
            "<xsd:element name='bar' substitutionGroup='foo'/>"
            "</xsd:schema>"
        )
        assert "circular-substitution-group" in _schema_codes(report)


class TestNotationDeclarationLegality:
    """Semantic notation-declaration legality (notatA/notatB)."""

    def test_notation_without_public_or_system_reports(self, parse_schema):
        """notatB001: a notation needs a public or system identifier."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='foo'/></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_notation_invalid_name_reports(self, parse_schema):
        """notatB008: a notation name must be an NCName."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='foo:bar' public='image/jpeg' system='viewer.exe'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_notation_invalid_id_reports(self, parse_schema):
        """notatA005: ``id`` must be a valid NCName."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation id='25' name='jpeg' public='image/jpeg' system='viewer.exe'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_notation_duplicate_id_reports_declaration_duplicate(self, parse_schema):
        """notatA007: ``id`` values must be unique within the schema."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation id='foo25' name='jpeg' public='image/jpeg'/>"
            "<xsd:notation id='foo25' name='jpeg2' public='image/jpeg'/>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_notation_with_public_is_clean(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='jpeg' public='image/jpeg'/>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_notation_name_and_id_ok(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation id='foo25' name='jpeg' system='viewer.exe'/>"
            "</xsd:schema>"
        )
        assert not {"declaration-attribute", "declaration-duplicate"} & _schema_codes(report)


class TestAnnotationDeclarationLegality:
    """Semantic annotation-declaration legality (annotF)."""

    def test_documentation_invalid_xml_lang_reports(self, parse_schema):
        """annotF001/annotF003: ``xml:lang`` must be a valid language."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation><xsd:documentation xml:lang=''>"
            "</xsd:documentation></xsd:annotation></xsd:schema>"
        )
        assert "declaration-attribute" in _schema_codes(report)

    def test_documentation_empty_source_ok(self, parse_schema):
        """annotB003: an empty ``source`` is a valid anyURI."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation><xsd:documentation source=''/></xsd:annotation></xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)

    def test_documentation_valid_xml_lang_is_clean(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation><xsd:documentation xml:lang='en-US'/></xsd:annotation>"
            "</xsd:schema>"
        )
        assert "declaration-attribute" not in _schema_codes(report)


class TestDuplicateComponentNames:
    """Duplicate names within one schema symbol space (XSD 1.0 §2.5).

    A symbol space holds one name per global component kind: simple and
    complex types share one, while elements, attributes, groups and
    attribute groups each have their own. Within a single element
    declaration the ``key``/``keyref``/``unique`` names share one too.
    Local declarations are scoped to their containing complex type and
    must not be compared across types.
    """

    def test_duplicate_complex_type_reports_declaration_duplicate(self, parse_schema):
        """ctI001: two complex types of one name in one symbol space."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'/>"
            "<xsd:complexType name='t'/>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_simple_and_complex_type_share_a_symbol_space(self, parse_schema):
        """ctI002: simple and complex type definitions share one space."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:string'/></xsd:simpleType>"
            "<xsd:complexType name='t'/>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_duplicate_global_element_reports_declaration_duplicate(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='e' type='xsd:string'/>"
            "<xsd:element name='e' type='xsd:string'/>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_duplicate_global_attribute_reports_declaration_duplicate(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='a' type='xsd:string'/>"
            "<xsd:attribute name='a' type='xsd:string'/>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_duplicate_group_reports_declaration_duplicate(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:group name='g'><xsd:sequence/></xsd:group>"
            "<xsd:group name='g'><xsd:sequence/></xsd:group>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_duplicate_attribute_group_reports_declaration_duplicate(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attributeGroup name='ag'/>"
            "<xsd:attributeGroup name='ag'/>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_duplicate_identity_constraint_reports_declaration_duplicate(self, parse_schema):
        """Two key constraints with one name on the same element declaration."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root'>"
            "<xsd:key name='k'><xsd:selector xpath='a'/><xsd:field xpath='.'/></xsd:key>"
            "<xsd:key name='k'><xsd:selector xpath='b'/><xsd:field xpath='.'/></xsd:key>"
            "</xsd:element></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_distinct_identity_constraints_across_kinds_are_clean(self, parse_schema):
        """A key, a unique and a keyref may share one element; only their
        names matter, and distinct names are legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root'>"
            "<xsd:key name='KEY'><xsd:selector xpath='a'/><xsd:field xpath='.'/></xsd:key>"
            "<xsd:unique name='UNIQ'><xsd:selector xpath='b'/><xsd:field xpath='.'/></xsd:unique>"
            "<xsd:keyref name='REF' refer='KEY'>"
            "<xsd:selector xpath='c'/><xsd:field xpath='.'/></xsd:keyref>"
            "</xsd:element></xsd:schema>"
        )
        assert "declaration-duplicate" not in _schema_codes(report)

    def test_element_and_type_share_one_name(self, parse_schema):
        """Different symbol spaces: an element and a type may share a name."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='t' type='xsd:string'/>"
            "<xsd:complexType name='t'/>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" not in _schema_codes(report)

    def test_attribute_and_type_same_name_is_clean(self, parse_schema):
        """ctI003: an attribute and a complex type may share a name."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:attribute name='fooType' type='xsd:string'/>"
            "<xsd:complexType name='fooType'/>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" not in _schema_codes(report)

    def test_local_element_names_may_repeat_across_types(self, parse_schema):
        """name00301m1: local element declarations are scoped to their
        containing complex type, so the same local name in two types is
        not a duplicate."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t1'><xsd:sequence>"
            "<xsd:element name='local' type='xsd:string'/></xsd:sequence></xsd:complexType>"
            "<xsd:complexType name='t2'><xsd:sequence>"
            "<xsd:element name='local' type='xsd:string'/></xsd:sequence></xsd:complexType>"
            "</xsd:schema>"
        )
        assert "declaration-duplicate" not in _schema_codes(report)


class TestCompositorSingleOccurrenceGrammar:
    """``all``/``sequence``/``choice`` allow one annotation each."""

    def test_duplicate_annotation_in_all_reports_declaration_duplicate(self, parse_schema):
        """annotB004: an ``all`` may carry at most one annotation."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root'><xsd:complexType><xsd:all>"
            "<xsd:annotation><xsd:documentation/></xsd:annotation>"
            "<xsd:annotation><xsd:documentation/></xsd:annotation>"
            "</xsd:all></xsd:complexType></xsd:element></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_duplicate_annotation_in_sequence_reports_declaration_duplicate(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:sequence>"
            "<xsd:annotation/><xsd:annotation/>"
            "</xsd:sequence></xsd:complexType></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_repeated_element_children_in_all_are_allowed(self, parse_schema):
        """The max-one table must not cap repeated element children."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root'><xsd:complexType><xsd:all>"
            "<xsd:element name='a' type='xsd:string'/>"
            "<xsd:element name='b' type='xsd:string'/>"
            "</xsd:all></xsd:complexType></xsd:element></xsd:schema>"
        )
        assert "declaration-duplicate" not in _schema_codes(report)

    def test_repeated_particles_in_choice_are_allowed(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='t'><xsd:choice>"
            "<xsd:sequence><xsd:element name='a'/></xsd:sequence>"
            "<xsd:sequence><xsd:element name='b'/></xsd:sequence>"
            "</xsd:choice></xsd:complexType></xsd:schema>"
        )
        assert "declaration-duplicate" not in _schema_codes(report)


class TestFacetLegality:
    """Schema-phase legality of constraining facets on built-in bases.

    Mirrors the W3C ``msData/datatypes/Facets`` family: a facet must be
    applicable to its base (``fractionDigits`` is fixed to 0 on every
    integer-derived type), and the declared facet values must describe a
    consistent, non-empty value space.
    """

    def test_fraction_digits_on_bounded_integer_reports_facet(self, parse_schema):
        """msData byte_fractionDigits004: fractionDigits is 0 on integers."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:byte'>"
            "<xsd:fractionDigits value='1'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet" in _schema_codes(report)

    def test_fraction_digits_on_integer_reports_facet(self, parse_schema):
        """msData integer_fractionDigits004: the unbounded integer too."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:integer'>"
            "<xsd:fractionDigits value='1'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet" in _schema_codes(report)

    def test_fraction_digits_five_with_total_digits_reports_facet(self, parse_schema):
        """msData byte_fractionDigits007: the digit facets stay conflicting
        when fractionDigits is fixed to 0."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:byte'>"
            "<xsd:fractionDigits value='5'/><xsd:totalDigits value='5'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet" in _schema_codes(report)

    def test_fraction_digits_zero_on_integer_is_clean(self, parse_schema):
        """msData byte_fractionDigits003: the fixed value 0 is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:byte'>"
            "<xsd:fractionDigits value='0'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet" not in _schema_codes(report)

    def test_total_digits_on_integer_is_clean(self, parse_schema):
        """totalDigits is applicable to the integer family, unlike
        fractionDigits, so splitting the two must not reject it."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:byte'>"
            "<xsd:totalDigits value='3'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet" not in _schema_codes(report)

    def test_max_inclusive_and_max_exclusive_reports_facet_conflict(self, parse_schema):
        """msData byte_maxInclusive005: the two upper bounds are exclusive."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:byte'>"
            "<xsd:maxInclusive value='5'/><xsd:maxExclusive value='5'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" in _schema_codes(report)

    def test_min_inclusive_and_min_exclusive_reports_facet_conflict(self, parse_schema):
        """The two lower bounds are mutually exclusive as well."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:byte'>"
            "<xsd:minInclusive value='5'/><xsd:minExclusive value='5'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" in _schema_codes(report)

    def test_min_above_max_reports_facet_conflict(self, parse_schema):
        """msData integer_minInclusive003: an empty interval is illegal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:integer'>"
            "<xsd:minInclusive value='7'/><xsd:maxInclusive value='1'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" in _schema_codes(report)

    def test_equal_inclusive_bounds_are_clean(self, parse_schema):
        """A single-point interval (minInclusive == maxInclusive) is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:integer'>"
            "<xsd:minInclusive value='5'/><xsd:maxInclusive value='5'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" not in _schema_codes(report)

    def test_positive_integer_max_exclusive_at_base_minimum_reports(self, parse_schema):
        """msData positiveInteger_maxExclusive001: ``< 1`` is empty because
        the base type's value space starts at 1."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:positiveInteger'>"
            "<xsd:maxExclusive value='1'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" in _schema_codes(report)

    def test_positive_integer_max_inclusive_at_base_minimum_is_clean(self, parse_schema):
        """``<= 1`` still holds the single value 1."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:positiveInteger'>"
            "<xsd:maxInclusive value='1'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" not in _schema_codes(report)

    def test_list_min_length_below_one_reports_facet_conflict(self, parse_schema):
        """msData NMTOKENS_minLength001: a list always has at least one item."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:NMTOKENS'>"
            "<xsd:minLength value='0'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" in _schema_codes(report)

    def test_list_max_length_zero_reports_facet_conflict(self, parse_schema):
        """msData NMTOKENS_maxLength001: ``maxLength 0`` cannot hold one
        item."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:NMTOKENS'>"
            "<xsd:maxLength value='0'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" in _schema_codes(report)

    def test_list_min_length_below_one_with_max_length_reports(self, parse_schema):
        """msData NMTOKENS_minLength004: the impossible minimum is still a
        conflict when a maximum is also declared."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='t'><xsd:restriction base='xsd:NMTOKENS'>"
            "<xsd:minLength value='0'/><xsd:maxLength value='2'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" in _schema_codes(report)

    def test_generic_list_zero_length_is_legal(self, parse_schema):
        """A generic list is zero or more items, so length 0 is legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='L'><xsd:list itemType='xsd:int'/></xsd:simpleType>"
            "<xsd:simpleType name='t'><xsd:restriction base='L'>"
            "<xsd:length value='0'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" not in _schema_codes(report)

    def test_generic_list_zero_minimum_is_legal(self, parse_schema):
        """A generic list's fixed minimum stays 0, not 1."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='L'><xsd:list itemType='xsd:int'/></xsd:simpleType>"
            "<xsd:simpleType name='t'><xsd:restriction base='L'>"
            "<xsd:minLength value='0'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" not in _schema_codes(report)

    def test_generic_list_zero_maximum_is_legal(self, parse_schema):
        """A generic list's maximum may be 0 (the empty list)."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='L'><xsd:list itemType='xsd:int'/></xsd:simpleType>"
            "<xsd:simpleType name='t'><xsd:restriction base='L'>"
            "<xsd:maxLength value='0'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" not in _schema_codes(report)

    def test_list_derived_from_nmtokens_still_fixes_min_length(self, parse_schema):
        """A user list that restricts NMTOKENS keeps the fixed minimum."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='L'><xsd:restriction base='xsd:NMTOKENS'>"
            "<xsd:maxLength value='4'/></xsd:restriction></xsd:simpleType>"
            "<xsd:simpleType name='t'><xsd:restriction base='L'>"
            "<xsd:minLength value='0'/>"
            "</xsd:restriction></xsd:simpleType></xsd:schema>"
        )
        assert "facet-conflict" in _schema_codes(report)


class TestSimpleTypeAtomicity:
    """A list's item type and a union's member types must be simple types.

    XSD 1.1 relaxes the XSD 1.0 rule in both directions and the msData
    stJ/stK corpus pins the relaxed behaviour: a list item type may be a
    union all of whose members are atomic (stJ002 is valid), and a union
    member may be a list (stK004 is valid) or another union (whose
    members flatten). A list item type that is itself a list stays
    illegal, as does a union member that is a complex type
    (`atomic-required`).
    """

    def test_list_item_type_restriction_of_atomic_is_clean(self, parse_schema):
        """msData stJ001: an item type that restricts an atomic is atomic."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='myType'><xsd:restriction base='xsd:integer'/>"
            "</xsd:simpleType>"
            "<xsd:simpleType name='fooType'><xsd:list itemType='myType'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" not in _schema_codes(report)

    def test_list_item_type_union_of_atomics_is_clean(self, parse_schema):
        """msData stJ002: a union of atomic members is a legal item type."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='myType'><xsd:union>"
            "<xsd:simpleType><xsd:restriction base='xsd:integer'/></xsd:simpleType>"
            "<xsd:simpleType><xsd:restriction base='xsd:NMTOKEN'/></xsd:simpleType>"
            "</xsd:union></xsd:simpleType>"
            "<xsd:simpleType name='fooType'><xsd:list itemType='myType'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" not in _schema_codes(report)

    def test_list_item_type_union_of_nested_unions_is_clean(self, parse_schema):
        """A union item type is legal when no list appears in its
        transitive membership, even through nested unions."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='V'>"
            "<xsd:union memberTypes='xsd:int xsd:string'/></xsd:simpleType>"
            "<xsd:simpleType name='U'>"
            "<xsd:union memberTypes='V'/></xsd:simpleType>"
            "<xsd:simpleType name='L'>"
            "<xsd:list itemType='U'/></xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" not in _schema_codes(report)

    def test_list_item_type_union_with_transitive_list_reports(self, parse_schema):
        """A union item type is illegal when a list appears anywhere in
        its transitive membership, even inside a nested union."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='L0'><xsd:list itemType='xsd:int'/></xsd:simpleType>"
            "<xsd:simpleType name='V'>"
            "<xsd:union memberTypes='xsd:int L0'/></xsd:simpleType>"
            "<xsd:simpleType name='U'>"
            "<xsd:union memberTypes='V'/></xsd:simpleType>"
            "<xsd:simpleType name='L'>"
            "<xsd:list itemType='U'/></xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" in _schema_codes(report)

    def test_list_item_type_referring_to_list_reports(self, parse_schema):
        """msData stJ019: a list cannot be the item type of a list."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='myType'><xsd:list>"
            "<xsd:simpleType><xsd:restriction base='xsd:integer'/></xsd:simpleType>"
            "</xsd:list></xsd:simpleType>"
            "<xsd:simpleType name='fooType'><xsd:list itemType='myType'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" in _schema_codes(report)

    def test_list_item_type_referring_to_builtin_list_reports(self, parse_schema):
        """A built-in list type (IDREFS) is not atomic either."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='fooType'><xsd:list itemType='xsd:IDREFS'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" in _schema_codes(report)

    def test_list_item_type_referring_to_complex_type_reports(self, parse_schema):
        """msData stJ003: a complex type is not an atomic simple type."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='myType'><xsd:simpleContent>"
            "<xsd:extension base='xsd:integer'/></xsd:simpleContent></xsd:complexType>"
            "<xsd:simpleType name='fooType'><xsd:list itemType='myType'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" in _schema_codes(report)

    def test_list_inline_item_type_that_is_a_list_reports(self, parse_schema):
        """An inline item type is subject to the same atomicity rule."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='fooType'><xsd:list>"
            "<xsd:simpleType><xsd:list itemType='xsd:int'/></xsd:simpleType>"
            "</xsd:list></xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" in _schema_codes(report)

    def test_union_member_restriction_of_atomic_is_clean(self, parse_schema):
        """msData stK001: atomic member types are legal."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='myType_1'><xsd:restriction base='xsd:integer'/>"
            "</xsd:simpleType>"
            "<xsd:simpleType name='myType_2'><xsd:restriction base='xsd:duration'/>"
            "</xsd:simpleType>"
            "<xsd:simpleType name='fooType'>"
            "<xsd:union memberTypes='myType_1 myType_2'/></xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" not in _schema_codes(report)

    def test_union_member_referring_to_list_is_clean(self, parse_schema):
        """msData stK004: a list is a legal union member."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='myType'><xsd:list>"
            "<xsd:simpleType><xsd:restriction base='xsd:integer'/></xsd:simpleType>"
            "</xsd:list></xsd:simpleType>"
            "<xsd:simpleType name='fooType'><xsd:union memberTypes='myType'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" not in _schema_codes(report)

    def test_union_member_referring_to_union_is_clean(self, parse_schema):
        """A union-of-union is legal; its member set is flattened."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='myType_1'><xsd:restriction base='xsd:integer'/>"
            "</xsd:simpleType>"
            "<xsd:simpleType name='myType_2'>"
            "<xsd:union memberTypes='myType_1 xsd:duration'/></xsd:simpleType>"
            "<xsd:simpleType name='fooType'><xsd:union memberTypes='myType_2'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" not in _schema_codes(report)

    def test_union_member_unknown_type_reports(self, parse_schema):
        """msData stK002: a member naming an undefined type is reported."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='myType_1'><xsd:restriction base='xsd:integer'/>"
            "</xsd:simpleType>"
            "<xsd:simpleType name='myType_2'>"
            "<xsd:union memberTypes='myType_1 xsd:timeDuration'/></xsd:simpleType>"
            "<xsd:simpleType name='fooType'><xsd:union memberTypes='myType_2'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "unknown-type" in _schema_codes(report)

    def test_union_member_referring_to_complex_type_reports(self, parse_schema):
        """msData stK003: a complex type cannot be a union member."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:complexType name='myType'><xsd:simpleContent>"
            "<xsd:extension base='xsd:integer'/></xsd:simpleContent></xsd:complexType>"
            "<xsd:simpleType name='fooType'><xsd:union memberTypes='myType'/>"
            "</xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" in _schema_codes(report)

    def test_union_inline_member_that_is_a_union_is_clean(self, parse_schema):
        """An inline union member is legal under the same flattening rule."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='fooType'><xsd:union>"
            "<xsd:simpleType><xsd:union memberTypes='xsd:int'/></xsd:simpleType>"
            "</xsd:union></xsd:simpleType></xsd:schema>"
        )
        assert "atomic-required" not in _schema_codes(report)
