"""Small real-schema fixtures shared by the optional-integration tests."""

from io import StringIO

import pyxsd


def compile_schema(body, *, namespace=None):
    ns = (
        f' targetNamespace="{namespace}" xmlns:t="{namespace}" elementFormDefault="qualified"'
        if namespace
        else ""
    )
    return pyxsd.compile(
        StringIO(f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"{ns}>{body}</xs:schema>'),
        mode=pyxsd.ParseModes.NAMESPACED,
    )


def parse(schema, xml):
    document = schema.parse(StringIO(xml))
    document.require_valid()
    return document


RECORD_SCHEMA = """
<xs:simpleType name="Money"><xs:restriction base="xs:decimal">
  <xs:totalDigits value="4"/><xs:fractionDigits value="2"/>
</xs:restriction></xs:simpleType>
<xs:element name="root"><xs:complexType><xs:sequence>
  <xs:element name="count" type="xs:int"/>
  <xs:element name="item" type="xs:int" minOccurs="0" maxOccurs="unbounded" nillable="true"/>
  <xs:element name="flag" type="xs:boolean" minOccurs="0"/>
  <xs:element name="amount" minOccurs="0"><xs:complexType><xs:simpleContent>
    <xs:extension base="Money"><xs:attribute name="currency" type="xs:string" default="USD"/>
    </xs:extension></xs:simpleContent></xs:complexType></xs:element>
  <xs:element name="missing" minOccurs="0" nillable="true"><xs:complexType>
    <xs:attribute name="reason" type="xs:string" use="required"/>
  </xs:complexType></xs:element>
</xs:sequence><xs:attribute name="version" type="xs:int" default="9"/>
</xs:complexType></xs:element>
"""
