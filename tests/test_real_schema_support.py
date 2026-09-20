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
from pathlib import Path

import pytest

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema
from pyxsd.validation import IssueSeverity

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

#: The test suite's own large XSD 1.1 schema, the ``introspection``
#: robustness canary.
CORPUS_XSTS = Path(__file__).parent / "xsts" / "corpus" / "common" / "xsts.xsd"


def _parse_text(schema, instance="<r/>"):
    """Compiles an inline schema and binds an inline instance from strings."""
    return Schema.compile(StringIO(schema)).parse(StringIO(instance))


def _codes(doc):
    return [issue.code for issue in doc.report.issues]


class TestAttributeRefs:
    def test_attribute_ref_resolves_and_validates(self):
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:attribute name="val" type="xs:int"/>'
            '<xs:complexType name="T"><xs:attribute ref="val"/></xs:complexType>'
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            '<r val="5"/>',
        )
        assert doc.report.issues == []
        assert doc.root.val == 5

    def test_attribute_ref_use_required(self):
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:attribute name="val" type="xs:int"/>'
            '<xs:complexType name="T">'
            '<xs:attribute ref="val" use="required"/>'
            "</xs:complexType>"
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            "<r/>",
        )
        assert "missing-attribute" in _codes(doc)

    def test_attribute_ref_inside_attribute_group(self):
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:attribute name="val" type="xs:int"/>'
            '<xs:attributeGroup name="g"><xs:attribute ref="val"/></xs:attributeGroup>'
            '<xs:complexType name="T"><xs:attributeGroup ref="g"/></xs:complexType>'
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            '<r val="5"/>',
        )
        assert doc.report.issues == []
        assert doc.root.val == 5

    def test_unresolved_attribute_ref_reports_without_crashing(self):
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="T"><xs:attribute ref="nope"/></xs:complexType>'
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            "<r/>",
        )
        assert "unknown-attributeRef" in _codes(doc)


@pytest.mark.skipif(not CORPUS_XSTS.is_file(), reason="xsdtests corpus not checked out")
class TestSuiteIntrospectionSchema:
    def test_xsts_schema_compiles_without_schema_errors(self):
        """The suite's own 1.1 schema resolves its XLink references.

        ``xsts.xsd`` imports the XLink namespace with a remote
        ``schemaLocation``; the XLink attribute declarations are built
        in, so ``xlink:type``/``xlink:href`` resolve and the schema has
        no schema-phase errors.
        """
        schema = Schema.compile(str(CORPUS_XSTS), mode=ParseModes.NAMESPACED)
        errors = [
            issue
            for issue in schema.report.for_phase("schema")
            if issue.severity is IssueSeverity.ERROR
        ]
        assert errors == []


class TestUnknownBaseTypes:
    def test_unresolved_simple_type_base_does_not_crash(self):
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:simpleType name="T"><xs:restriction base="missing"/></xs:simpleType>'
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            "<r>5</r>",
        )
        assert "unknown-type" in _codes(doc)

    def test_unresolved_complex_type_base_does_not_crash(self):
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="T"><xs:complexContent>'
            '<xs:extension base="missing"><xs:sequence/></xs:extension>'
            "</xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="T"/>'
            "</xs:schema>",
            "<r/>",
        )
        assert "unknown-type" in _codes(doc)
