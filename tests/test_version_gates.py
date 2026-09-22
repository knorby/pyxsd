"""The XSD 1.0 vocabulary gate (``xsd11-construct`` issues)."""

from io import StringIO

import pyxsd

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


def _issues(schema_xml):
    doc = pyxsd.Schema.compile(StringIO(schema_xml), xsd_version="1.0")
    return [i for i in doc.report if i.code == "xsd11-construct"]


def test_assert_rejected_in_1_0_mode():
    schema = f"""<xs:schema {XS}>
      <xs:element name="e">
        <xs:complexType><xs:sequence/>
          <xs:assert test="true()"/>
        </xs:complexType>
      </xs:element>
    </xs:schema>"""
    assert any("assert" in i.message for i in _issues(schema))


def test_opencontent_rejected():
    schema = f"""<xs:schema {XS}>
      <xs:element name="e"><xs:complexType>
        <xs:openContent mode="suffix"><xs:any/></xs:openContent>
        <xs:sequence/>
      </xs:complexType></xs:element>
    </xs:schema>"""
    assert any("openContent" in i.message for i in _issues(schema))


def test_alternative_rejected():
    schema = f"""<xs:schema {XS}>
      <xs:element name="e" type="xs:string">
        <xs:alternative test="true()" type="xs:string"/>
      </xs:element>
    </xs:schema>"""
    assert any("alternative" in i.message for i in _issues(schema))


def test_override_rejected():
    schema = f"""<xs:schema {XS}>
      <xs:override schemaLocation="other.xsd"/>
      <xs:element name="e" type="xs:string"/>
    </xs:schema>"""
    assert any("override" in i.message for i in _issues(schema))


def test_1_1_attributes_rejected():
    schema = f"""<xs:schema {XS}>
      <xs:element name="e" type="xs:string">
        <xs:complexType><xs:anyAttribute notQName="foo"/></xs:complexType>
      </xs:element>
    </xs:schema>"""
    assert any("notQName" in i.message for i in _issues(schema))


def test_1_1_datatypes_rejected_in_1_0_mode():
    schema = f'<xs:schema {XS}><xs:element name="e" type="xs:dateTimeStamp"/></xs:schema>'
    assert any("dateTimeStamp" in i.message for i in _issues(schema))


def test_member_types_are_checked():
    schema = (
        f'<xs:schema {XS}><xs:simpleType name="u"><xs:union '
        f'memberTypes="xs:string xs:yearMonthDuration"/></xs:simpleType></xs:schema>'
    )
    assert any("yearMonthDuration" in i.message for i in _issues(schema))


def test_plain_1_0_schema_has_no_gate_issues():
    schema = f'<xs:schema {XS}><xs:element name="e" type="xs:string"/></xs:schema>'
    assert _issues(schema) == []


def test_gate_is_off_in_1_1_mode():
    schema = f'<xs:schema {XS}><xs:element name="e" type="xs:dateTimeStamp"/></xs:schema>'
    doc = pyxsd.Schema.compile(StringIO(schema))
    assert [i for i in doc.report if i.code == "xsd11-construct"] == []
