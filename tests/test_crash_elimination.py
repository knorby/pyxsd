"""Regression tests for the Area B crash-elimination burndown.

Each test class corresponds to one crash signature from
``docs/superpowers/2026-09-13-xsts-failure-burndown.md``; schema and
instance snippets are reduced from the XSTS cases that triggered the
crash. A former crash must become either a structured ``PyXSD`` report
issue or a correct verdict -- never an uncaught non-``pyxsd`` exception.
"""

import io
from pathlib import Path

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD
from pyxsd.xsd_data_types import AnySimpleType

XSTS_CORPUS = Path(__file__).parent / "xsts" / "corpus"


def _parse(tmp_path, schema_text, instance_text, mode=ParseModes.STRICT):
    """Writes an inline schema/instance pair and returns the parser."""
    schema = tmp_path / "schema.xsd"
    instance = tmp_path / "instance.xml"
    schema.write_text(schema_text)
    instance.write_text(instance_text)
    return PyXSD(
        str(instance),
        str(schema),
        xmlFileOutput=False,
        transformOutputName=None,
        mode=mode,
    )


def _parse_schema(tmp_path, schema_text, monkeypatch):
    """Loads an inline schema without an instance document.

    Mirrors the XSTS driver's schema-only mode: the schema phase still
    runs, but the instance phase is stubbed out so a schema that
    declares no root elements can be inspected.
    """
    schema = tmp_path / "schema.xsd"
    schema.write_text(schema_text)
    monkeypatch.setattr(PyXSD, "parseXML", lambda self: None)
    return PyXSD(
        io.StringIO("<x/>"),
        str(schema),
        xmlFileOutput=False,
        transformOutputName=None,
    )


class TestUntypedAttributeDefaultsToAnySimpleType:
    """``Attribute.getType()`` raised ``TypeError`` for an attribute with
    neither a ``type`` attribute nor an inline ``simpleType`` child
    (the Assert/* instance failures, ctA045, ctB039, ctB053)."""

    def test_any_value_on_untyped_attribute(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t"><xs:attribute name="note"/></xs:complexType>'
            '<xs:element name="t" type="t"/></xs:schema>',
            '<t note="hello"/>',
        )
        assert parser.report.has_errors is False
        root = parser.parseXML()
        assert str(root.note) == "hello"

    def test_required_untyped_attribute(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t">'
            '<xs:attribute name="x" use="required"/>'
            "</xs:complexType>"
            '<xs:element name="t" type="t"/></xs:schema>',
            '<t x="42"/>',
        )
        assert parser.report.has_errors is False
        root = parser.parseXML()
        assert str(root.x) == "42"

    def test_getType_is_anySimpleType(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t"><xs:attribute name="note"/></xs:complexType>'
            '<xs:element name="t" type="t"/></xs:schema>',
            '<t note="x"/>',
        )
        assert parser.classes["t"].note.getType() is AnySimpleType


LIST_SCHEMA = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:simpleType name="version-token">'
    '<xs:restriction base="xs:token">'
    '<xs:enumeration value="1.0"/>'
    '<xs:enumeration value="1.1"/>'
    "</xs:restriction>"
    "</xs:simpleType>"
    '<xs:simpleType name="version-info">'
    '<xs:list itemType="version-token"/>'
    "</xs:simpleType>"
    '<xs:simpleType name="two-versions">'
    '<xs:restriction base="version-info"><xs:maxLength value="2"/></xs:restriction>'
    "</xs:simpleType>"
    '<xs:complexType name="t">'
    '<xs:attribute name="version" type="version-info"/>'
    '<xs:attribute name="pair" type="two-versions"/>'
    "</xs:complexType>"
    '<xs:element name="t" type="t"/>'
    "</xs:schema>"
)


class TestListTypedAttributeBinding:
    """``Attribute.__set__`` tested the owning instance instead of the
    value and raised for list-typed attributes (the introspection
    testSet -- ``xsts.xsd``'s ``version-info`` -- and attD004)."""

    def test_list_attribute_binds_items(self, tmp_path):
        parser = _parse(tmp_path, LIST_SCHEMA, '<t version="1.0 1.1"/>')
        assert parser.report.has_errors is False
        root = parser.parseXML()
        assert [str(item) for item in root.version] == ["1.0", "1.1"]

    def test_list_attribute_single_token(self, tmp_path):
        parser = _parse(tmp_path, LIST_SCHEMA, '<t version="1.1"/>')
        assert parser.report.has_errors is False

    def test_list_item_type_validates(self, tmp_path):
        parser = _parse(tmp_path, LIST_SCHEMA, '<t version="9.9"/>')
        assert parser.report.has_errors is True
        codes = [issue.code for issue in parser.report.issues]
        assert "invalid-attribute" in codes

    def test_list_length_facet_counts_items(self, tmp_path):
        parser = _parse(tmp_path, LIST_SCHEMA, '<t pair="1.0 1.1 1.0"/>')
        assert parser.report.has_errors is True
        codes = [issue.code for issue in parser.report.issues]
        assert "invalid-attribute" in codes

    def test_xsts_metaschema_version_list_does_not_crash(self):
        parser = PyXSD(
            str(XSTS_CORPUS / "saxonMeta" / "All.testSet"),
            str(XSTS_CORPUS / "common" / "xsts.xsd"),
            xmlFileOutput=False,
            transformOutputName=None,
            mode=ParseModes.NAMESPACED,
        )
        root = parser.parseXML()
        assert [str(item) for item in root.version] == ["1.1"]


class TestAttributeRefResolution:
    """``_resolveAttributeRef`` assumed the referred declaration always
    carried a ``type`` (addB007/addB109/addB187, attE002) and the strict
    candidate scan included local declarations."""

    def test_ref_to_untyped_global_attribute(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:attribute name="att2"/>'
            '<xs:complexType name="t"><xs:attribute ref="att2"/></xs:complexType>'
            '<xs:element name="t" type="t"/></xs:schema>',
            '<t att2="anything"/>',
        )
        assert parser.report.has_errors is False
        assert str(parser.parseXML().att2) == "anything"

    def test_ref_to_local_declaration_is_unresolved(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:t" xmlns:t="urn:t" elementFormDefault="qualified">'
            '<xs:complexType name="other">'
            '<xs:attribute name="att1" type="xs:string"/>'
            "</xs:complexType>"
            '<xs:complexType name="t"><xs:attribute ref="t:att1"/></xs:complexType>'
            '<xs:element name="t" type="t"/></xs:schema>',
            '<t:t xmlns:t="urn:t"/>',
            mode=ParseModes.NAMESPACED,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "unknown-attributeRef" in codes


class TestRestrictionBase:
    """``Restriction`` read ``tagAttributes['base']`` unconditionally
    and crashed on a restriction with an inline ``simpleType`` instead
    (stZ073b, addB014, addB064)."""

    def test_inline_restriction_type(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="test" type="st.unionType"/>'
            '<xs:simpleType name="st.unionType">'
            "<xs:restriction><xs:simpleType>"
            '<xs:union memberTypes="xs:string xs:integer"/>'
            "</xs:simpleType>"
            '<xs:enumeration value="a"/>'
            "</xs:restriction>"
            "</xs:simpleType>"
            "</xs:schema>",
            "<test>a</test>",
        )
        assert parser.report.has_errors is False

    def test_missing_base_reports(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="test" type="Bad"/>'
            '<xs:simpleType name="Bad"><xs:restriction>'
            '<xs:enumeration value="a"/>'
            "</xs:restriction></xs:simpleType>"
            "</xs:schema>",
            "<test>a</test>",
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "restriction-base" in codes


class TestAnnotationContent:
    """``documentation`` may contain arbitrary XML; an unqualified child
    tag crashed tag parsing (annotB004, annotB005)."""

    def test_foreign_documentation_child(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="foo"><xs:complexType><xs:all>'
            "<xs:annotation><xs:documentation><Documentation/>"
            "</xs:documentation></xs:annotation>"
            "</xs:all></xs:complexType></xs:element></xs:schema>",
            "<foo/>",
        )
        assert parser.report.has_errors is False

    def test_documentation_text_still_sets_doc(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t"><xs:annotation>'
            "<xs:documentation>hello docs</xs:documentation>"
            "</xs:annotation></xs:complexType>"
            '<xs:element name="t" type="t"/></xs:schema>',
            "<t/>",
        )
        assert parser.report.has_errors is False
        assert parser.classes["t"].__doc__ == "hello docs"


class TestOccursValues:
    """``getMinOccurs``/``getMaxOccurs`` ran ``int()`` on invalid
    lexical values (elemJ006, elemJ008)."""

    def test_empty_max_occurs_reports(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="foo" type="bar"/>'
            '<xs:complexType name="bar"><xs:sequence>'
            '<xs:element name="name" maxOccurs=""/>'
            "</xs:sequence></xs:complexType></xs:schema>",
            "<foo/>",
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "invalid-occurs" in codes

    def test_wrong_case_unbounded_reports(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="foo" type="bar"/>'
            '<xs:complexType name="bar"><xs:sequence>'
            '<xs:element name="name" maxOccurs="Unbounded"/>'
            "</xs:sequence></xs:complexType></xs:schema>",
            "<foo/>",
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "invalid-occurs" in codes

    def test_valid_occurs_still_work(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="foo" type="bar"/>'
            '<xs:complexType name="bar"><xs:sequence>'
            '<xs:element name="name" minOccurs="0" maxOccurs="unbounded"/>'
            "</xs:sequence></xs:complexType></xs:schema>",
            "<foo><name/><name/></foo>",
        )
        assert parser.report.has_errors is False


class TestUnnamedDeclarations:
    """Declarations without a usable name crashed class-name
    capitalization (attQ005, attC004, ctA044)."""

    def test_global_attribute_without_name(self, tmp_path, monkeypatch):
        parser = _parse_schema(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:attribute/></xs:schema>',
            monkeypatch,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "declaration-name" in codes

    def test_local_attribute_with_empty_name(self, tmp_path, monkeypatch):
        parser = _parse_schema(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="attRef"><xs:attribute name=""/>'
            "</xs:complexType>"
            '<xs:element name="doc" type="attRef"/></xs:schema>',
            monkeypatch,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "declaration-name" in codes

    def test_complex_type_with_empty_name(self, tmp_path, monkeypatch):
        parser = _parse_schema(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name=""><xs:sequence/>'
            "</xs:complexType></xs:schema>",
            monkeypatch,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "declaration-name" in codes


class TestIdentityConstraintChildren:
    """An illegal child inside an identity constraint crashed on
    ``parent.elements`` (s2_2_4si01)."""

    def test_element_child_reports(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root"><xs:complexType><xs:sequence>'
            '<xs:element name="hi">'
            '<xs:key name="k"><xs:selector xpath="."/>'
            '<xs:field xpath="@a"/></xs:key>'
            '<xs:keyref name="r" refer="k"><xs:element name="a"/></xs:keyref>'
            "</xs:element>"
            "</xs:sequence></xs:complexType></xs:element></xs:schema>",
            '<root><hi a="1"/></root>',
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "unexpected-identity-child" in codes

    def test_selector_and_field_still_work(self, tmp_path):
        parser = _parse(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root"><xs:complexType><xs:sequence>'
            '<xs:element name="hi" maxOccurs="unbounded">'
            '<xs:complexType><xs:attribute name="a" type="xs:string"/>'
            "</xs:complexType>"
            '<xs:key name="k"><xs:selector xpath="."/>'
            '<xs:field xpath="@a"/></xs:key>'
            "</xs:element>"
            "</xs:sequence></xs:complexType></xs:element></xs:schema>",
            '<root><hi a="1"/><hi a="2"/></root>',
        )
        assert parser.report.has_errors is False


class TestCircularDerivation:
    """A type deriving from itself recursed until ``RecursionError``
    (addB101, elemM003)."""

    def test_self_restriction_reports(self, tmp_path, monkeypatch):
        parser = _parse_schema(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:simpleType name="foo"><xs:restriction base="foo">'
            '<xs:pattern value="[0-9]{5}"/></xs:restriction></xs:simpleType>'
            "</xs:schema>",
            monkeypatch,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "circular-derivation" in codes

    def test_self_extension_reports(self, tmp_path, monkeypatch):
        parser = _parse_schema(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="sAddress"><xs:complexContent>'
            '<xs:extension base="sAddress"><xs:sequence>'
            '<xs:element name="country" type="xs:string"/>'
            "</xs:sequence></xs:extension></xs:complexContent></xs:complexType>"
            "</xs:schema>",
            monkeypatch,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "circular-derivation" in codes

    def test_indirect_cycle_reports(self, tmp_path, monkeypatch):
        parser = _parse_schema(
            tmp_path,
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:simpleType name="a"><xs:restriction base="b">'
            '<xs:enumeration value="x"/></xs:restriction></xs:simpleType>'
            '<xs:simpleType name="b"><xs:restriction base="a">'
            '<xs:enumeration value="x"/></xs:restriction></xs:simpleType>'
            "</xs:schema>",
            monkeypatch,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "circular-derivation" in codes
