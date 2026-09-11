"""Robustness fixes for loading real-world schemas.

Phase A covers two constructs that appear throughout large standard
schemas (ECMA-376 WordprocessingML, for example) and previously crashed
the schema parse:

* ``<xs:attribute ref="..."/>`` reference sites, which have no ``name``
  of their own, and
* an unresolved ``base``/type reference, which used to reach
  ``issubclass(None, ...)`` and ``types.new_class(..., (None, ...))``.
"""

from io import StringIO

from pyxsd.parser import PyXSD

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


def _parse_text(schema, instance="<r/>"):
    """Parses an inline schema and instance from strings."""
    return PyXSD(
        StringIO(instance),
        xsdFile=StringIO(schema),
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
    )


def _codes(parser):
    return [issue.code for issue in parser.report.issues]


class TestAttributeRefs:
    def test_attribute_ref_resolves_and_validates(self):
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:attribute name="val" type="xs:int"/>'
            '<xs:complexType name="T"><xs:attribute ref="val"/></xs:complexType>'
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            '<r val="5"/>',
        )
        assert parser.report.issues == []
        assert parser.schemaRootInstance.val == 5

    def test_attribute_ref_use_required(self):
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:attribute name="val" type="xs:int"/>'
            '<xs:complexType name="T">'
            '<xs:attribute ref="val" use="required"/>'
            "</xs:complexType>"
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            "<r/>",
        )
        assert "missing-attribute" in _codes(parser)

    def test_attribute_ref_inside_attribute_group(self):
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:attribute name="val" type="xs:int"/>'
            '<xs:attributeGroup name="g"><xs:attribute ref="val"/></xs:attributeGroup>'
            '<xs:complexType name="T"><xs:attributeGroup ref="g"/></xs:complexType>'
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            '<r val="5"/>',
        )
        assert parser.report.issues == []
        assert parser.schemaRootInstance.val == 5

    def test_unresolved_attribute_ref_reports_without_crashing(self):
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="T"><xs:attribute ref="nope"/></xs:complexType>'
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            "<r/>",
        )
        assert "unknown-attributeRef" in _codes(parser)


class TestUnknownBaseTypes:
    def test_unresolved_simple_type_base_does_not_crash(self):
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:simpleType name="T"><xs:restriction base="missing"/></xs:simpleType>'
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            "<r>5</r>",
        )
        assert "unknown-type" in _codes(parser)

    def test_unresolved_complex_type_base_does_not_crash(self):
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="T"><xs:complexContent>'
            '<xs:extension base="missing"><xs:sequence/></xs:extension>'
            "</xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            "<r/>",
        )
        assert "unknown-type" in _codes(parser)
