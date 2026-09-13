"""Regression tests for the Area B crash-elimination burndown.

Each test class corresponds to one crash signature from
``docs/superpowers/2026-09-13-xsts-failure-burndown.md``; schema and
instance snippets are reduced from the XSTS cases that triggered the
crash. A former crash must become either a structured ``PyXSD`` report
issue or a correct verdict -- never an uncaught non-``pyxsd`` exception.
"""

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
