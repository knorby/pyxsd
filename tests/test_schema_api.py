"""Tests for the pyxsd.Schema object API."""

from io import StringIO

import pytest

import pyxsd

SCHEMA = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="note" type="Note"/>
  <xs:complexType name="Note">
    <xs:sequence>
      <xs:element name="to" type="xs:string"/>
      <xs:element name="from" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>
"""

BROKEN = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="a">
    <xs:complexType>
      <xs:complexContent>
        <xs:extension base="NoSuchType"/>
      </xs:complexContent>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

NOT_A_SCHEMA = "<root/>"


def test_compile_returns_schema_with_classes():
    schema = pyxsd.Schema.compile(StringIO(SCHEMA))
    assert "Note" in schema.classes
    assert "note" in schema.classes or schema.classes.get("schema") is not None


def test_compile_collects_schema_phase_report_not_raise():
    schema = pyxsd.Schema.compile(StringIO(BROKEN))
    assert schema.report.has_errors


def test_require_valid_raises_validation_error():
    schema = pyxsd.Schema.compile(StringIO(BROKEN))
    with pytest.raises(pyxsd.ValidationError) as excinfo:
        schema.require_valid()
    assert excinfo.value.report is schema.report


def test_require_valid_passes_clean_schema():
    schema = pyxsd.Schema.compile(StringIO(SCHEMA))
    schema.require_valid()  # must not raise


def test_compile_fatal_inputs_raise_pyxsd_error():
    with pytest.raises(pyxsd.PyXSDError):
        pyxsd.Schema.compile(StringIO(NOT_A_SCHEMA))
    with pytest.raises(pyxsd.PyXSDError):
        pyxsd.Schema.compile("/no/such/file/anywhere.xsd")


def test_target_namespace_exposed():
    ns_schema = SCHEMA.replace(
        'xmlns:xs="http://www.w3.org/2001/XMLSchema">',
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:t">',
    )
    schema = pyxsd.Schema.compile(StringIO(ns_schema))
    assert schema.target_namespace == "urn:t"


HINT_DOC = (
    '<?xml version="1.0"?>'
    '<note xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:noNamespaceSchemaLocation="note.xsd">'
    "<to>you</to><count>1</count></note>"
)


def test_package_parse_resolves_hint(tmp_path):
    (tmp_path / "note.xsd").write_text(SCHEMA)
    (tmp_path / "note.xml").write_text(HINT_DOC)
    doc = pyxsd.parse(tmp_path / "note.xml")
    assert doc.root is not None


def test_package_parse_without_schema_raises():
    with pytest.raises(pyxsd.PyXSDError):
        pyxsd.parse(StringIO("<x/>"))


def test_package_parse_missing_file_raises_pyxsd_error():
    with pytest.raises(pyxsd.PyXSDError, match="could not be read"):
        pyxsd.parse("/no/such/file/anywhere.xml")


def test_package_parse_malformed_xml_raises_pyxsd_error(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<root><child></root>")
    with pytest.raises(pyxsd.PyXSDError, match="could not be read"):
        pyxsd.parse(bad)


def test_package_compile_alias():
    schema = pyxsd.compile(StringIO(SCHEMA))
    assert isinstance(schema, pyxsd.Schema)
