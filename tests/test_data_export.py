"""Tests for native, ``pyxsd-json``, and conventional JSON export.

The shared fixtures from ``test_data_formats`` pin every layout, namespace
form, nil state, and scalar family. Native tests keep the raw ``to_dict``
contract distinct from the JSON-safe ``to_data`` contract. Parker,
BadgerFish, and JsonML tests pin their grouped or positional structures,
namespace declarations, and every strict or best-effort loss.
"""

from __future__ import annotations

import json
import math
import warnings
from io import StringIO

import pytest
from tests.test_data_formats import FIXTURES

import pyxsd
from pyxsd.binding import BindingPolicy
from pyxsd.data_conversion.contracts import json_pointer
from pyxsd.data_conversion.export import bound_to_data
from pyxsd.exceptions import PyXSDError, PyXSDWarning
from pyxsd.xsd_data_types import IDREFS, Integer, QName
from pyxsd.xsi import XSI_NAMESPACE

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

NATIVE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" type="xs:int" maxOccurs="unbounded"/>
        <xs:element name="note" type="xs:string"/>
      </xs:sequence>
      <xs:attribute name="a" type="xs:int"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
NATIVE_INSTANCE = '<root a="1"><item>1</item><item>2</item><note>hi</note></root>'

EMPTY_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType/>
  </xs:element>
</xs:schema>"""

EMPTY_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:attribute name="id" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SIMPLE_CONTENT_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:attribute name="id" type="xs:string"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SIMPLE_CONTENT_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:anyAttribute processContents="lax"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SIMPLE_CONTENT_NILABLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:attribute name="id" type="xs:string"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

REPEATED_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="dates">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SIMPLE_CONTENT_DECLARED_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:attribute name="id" type="xs:string"/>
          <xs:anyAttribute processContents="lax"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SIMPLE_CONTENT_INT_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:attribute name="id" type="xs:int"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

REPEATED_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="dates">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="source" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

REPEATED_NILABLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="dates" nillable="true">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

REPEATED_NILABLE_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="dates" nillable="true">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="source" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

RECORD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:all>
        <xs:element name="a" type="xs:string"/>
        <xs:element name="b" type="xs:string" minOccurs="0" nillable="true"/>
      </xs:all>
      <xs:attribute name="id" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

REPEATED_ATTRIBUTED_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" maxOccurs="unbounded">
          <xs:complexType>
            <xs:simpleContent>
              <xs:extension base="xs:string">
                <xs:attribute name="id" type="xs:int"/>
              </xs:extension>
            </xs:simpleContent>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

RECORD_ATTRIBUTE_ORDER_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:all>
        <xs:element name="a" type="xs:string"/>
      </xs:all>
      <xs:attribute name="z" type="xs:string"/>
      <xs:attribute name="a" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SEQUENCE_ATTRIBUTE_ORDER_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
      </xs:sequence>
      <xs:attribute name="z" type="xs:string"/>
      <xs:attribute name="a" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SEQUENCE_OPTIONAL_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string" minOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SEQUENCE_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
        <xs:element name="b" type="xs:string"/>
      </xs:sequence>
      <xs:attribute name="id" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SEQUENCE_NILABLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
      </xs:sequence>
      <xs:attribute name="id" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SEQUENCE_REPEATED_CHOICE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:choice maxOccurs="unbounded">
        <xs:element name="a" type="xs:int"/>
        <xs:element name="b" type="xs:string"/>
      </xs:choice>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SEQUENCE_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
  <xs:element name="known" type="xs:string"/>
</xs:schema>"""

MIXED_NILABLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true">
    <xs:complexType mixed="true">
      <xs:sequence>
        <xs:element name="i" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="id" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

MIXED_NO_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType mixed="true">
      <xs:sequence>
        <xs:element name="i" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SUBSTITUTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="item" type="xs:string"/>
  <xs:element name="special" type="xs:string" substitutionGroup="item"/>
  <xs:element name="extra" type="xs:string" substitutionGroup="special"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="item" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ABSTRACT_SUBSTITUTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="item" abstract="true" type="xs:string"/>
  <xs:element name="special" type="xs:string" substitutionGroup="item"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="item" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

NAMESPACED_SUBSTITUTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:element name="item" type="xs:string"/>
  <xs:element name="special" type="xs:string" substitutionGroup="t:item"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="t:item" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

XSITYPE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:complexType name="Base">
    <xs:sequence>
      <xs:element name="a" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="Ext">
    <xs:complexContent>
      <xs:extension base="t:Base">
        <xs:sequence>
          <xs:element name="b" type="xs:int"/>
        </xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="root" type="t:Base"/>
</xs:schema>"""
XSITYPE_INSTANCE = (
    '<t:root xmlns:t="urn:example" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:type="t:Ext"><t:a>x</t:a><t:b>2</t:b></t:root>'
)

XSITYPE_EQUAL_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:complexType name="Base">
    <xs:sequence>
      <xs:element name="a" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="t:Base"/>
</xs:schema>"""

XSITYPE_SIMPLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:anySimpleType"/>
</xs:schema>"""
XSITYPE_SIMPLE_INSTANCE = (
    '<root xmlns:xs="http://www.w3.org/2001/XMLSchema" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:type="xs:int">5</root>'
)

XSITYPE_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="value" type="xs:anySimpleType"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

XSITYPE_LIST_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:complexType name="ListType">
    <xs:sequence>
      <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="xs:anyType"/>
</xs:schema>"""

XSITYPE_RECORD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:complexType name="RecordType">
    <xs:all>
      <xs:element name="a" type="xs:string"/>
    </xs:all>
  </xs:complexType>
  <xs:element name="root" type="xs:anyType"/>
</xs:schema>"""

UNNAMESPACED_XSITYPE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="T">
    <xs:sequence>
      <xs:element name="a" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="xs:anyType"/>
</xs:schema>"""

WILDCARD_GENERIC_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

EMPTY_XSITYPE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:complexType name="EmptyType"/>
  <xs:element name="root" type="xs:anyType"/>
</xs:schema>"""

NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified"
           attributeFormDefault="qualified">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="child" type="xs:string"/>
      </xs:sequence>
      <xs:attribute name="attr" type="xs:int"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

NAMESPACED_LEGACY_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="child" type="xs:string"/>
      </xs:sequence>
      <xs:attribute name="attr" type="xs:int"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

QNAME_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:QName"/>
</xs:schema>"""

DOUBLE_LIST_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="values">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="v" type="xs:double" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

DOUBLE_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:double"/>
</xs:schema>"""

DECIMAL_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:decimal"/>
</xs:schema>"""

IDREFS_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:IDREFS"/>
</xs:schema>"""

EMPTY_OR_NIL_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="empty">
          <xs:complexType/>
        </xs:element>
        <xs:element name="maybe" type="xs:string" nillable="true" minOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

NILABLE_EMPTY_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="empty" nillable="true">
          <xs:complexType/>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

NILABLE_COMPLEX_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true">
    <xs:complexType>
      <xs:attribute name="id" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

NAMESPACED_SIMPLE_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:element name="root" type="xs:string"/>
</xs:schema>"""

NAMESPACED_NILABLE_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:element name="root" type="xs:string" nillable="true"/>
</xs:schema>"""

PARKER_LOCAL_COLLISION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" type="xs:string"/>
        <xs:element name="item" type="xs:string" form="unqualified"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

QNAME_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="ref" type="xs:QName"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

QNAME_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:attribute name="ref" type="xs:QName"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

QNAME_LIST_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:simpleType name="QL">
    <xs:list itemType="xs:QName"/>
  </xs:simpleType>
  <xs:element name="root" type="t:QL"/>
</xs:schema>"""

WILDCARD_NAMESPACED_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ORDERED_SCHEMA_FOR_PARKER = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="section">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="head" type="xs:string"/>
        <xs:element name="p" type="xs:string" maxOccurs="2"/>
        <xs:element name="note" type="xs:string" minOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ANYTYPE_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:anyType"/>
</xs:schema>"""

BOOLEAN_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:boolean"/>
</xs:schema>"""

MIXED_TWO_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType mixed="true">
      <xs:sequence>
        <xs:element name="i" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
        <xs:element name="b" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

NESTED_XSITYPE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:complexType name="Base">
    <xs:sequence>
      <xs:element name="a" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="Ext">
    <xs:complexContent>
      <xs:extension base="t:Base">
        <xs:sequence>
          <xs:element name="b" type="xs:int"/>
        </xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="value" type="t:Base"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

#: Hex spellings PrefixAllocator derives for namespace URIs outside the schema.
HEX_EXAMPLE = "ns_75726e3a6578616d706c65"
HEX_WILD = "ns_75726e3a77696c64"
HEX_OTHER = "ns_75726e3a6f74686572"


def _compile(schema_source: str, instance_source: str, *, mode=None) -> pyxsd.Document:
    if mode is None:
        schema = pyxsd.Schema.compile(StringIO(schema_source))
    else:
        schema = pyxsd.Schema.compile(StringIO(schema_source), mode=mode)
    return schema.parse(StringIO(instance_source))


def _compile_fixture(name: str) -> pyxsd.Document:
    schema_source, instance_source = FIXTURES[name]
    return _compile(schema_source, instance_source)


# ---------------------------------------------------------------------------
# Shared fixture matrix
# ---------------------------------------------------------------------------

SHARED_EXPECTED = {
    "simple": {"simple": 42},
    "attribute": {"attributed": {"@id": "a1", "#content": "text"}},
    "repeated": {"dates": ["2026-01-01", "2026-02-01"]},
    "ordered": {
        "section": [
            {"head": "Introduction"},
            {"p": "First"},
            {"p": "Second"},
            {"note": "End"},
        ]
    },
    "mixed": {
        "para": [{"@id": "p1"}, "This is a ", {"i": "fine"}, " mess!"],
    },
    "namespaced": {
        "Q{urn:example}root": [
            {"@attr": "a"},
            {"Q{urn:example}child": "v"},
        ]
    },
    "nillable": {"nillable": None},
    "recursive": {
        "node": {
            "@name": "a",
            "node": [
                {
                    "@name": "b",
                    "node": [{"@name": "c", "node": []}],
                }
            ],
        }
    },
}


@pytest.mark.parametrize("name", sorted(SHARED_EXPECTED))
def test_pyxsd_json_exports_each_shared_fixture(name):
    document = _compile_fixture(name)
    expected = SHARED_EXPECTED[name]

    data = document.to_data(format="pyxsd-json")

    assert data == expected
    assert json.loads(document.to_json(format="pyxsd-json")) == expected


@pytest.mark.parametrize("name", sorted(SHARED_EXPECTED))
def test_pyxsd_json_export_is_deterministic(name):
    document = _compile_fixture(name)

    assert document.to_data(format="pyxsd-json") == document.to_data(format="pyxsd-json")


# ---------------------------------------------------------------------------
# pyxsd-json layouts
# ---------------------------------------------------------------------------


def test_empty_layout_uses_an_empty_string():
    document = _compile(EMPTY_SCHEMA, "<root/>")

    assert document.to_data(format="pyxsd-json") == {"root": ""}


def test_empty_layout_rejects_observed_attributes():
    document = _compile(EMPTY_SCHEMA, "<root/>")
    document.root._attribs_ = {"a": "x"}

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_empty_layout_rejects_observed_children():
    document = _compile(
        EMPTY_SCHEMA,
        "<root><x/></root>",
        mode=BindingPolicy(undeclared_content="generic"),
    )

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_empty_plus_layout_uses_an_attribute_object():
    assert _compile(EMPTY_ATTRIBUTE_SCHEMA, "<root/>").to_data(format="pyxsd-json") == {"root": {}}
    assert _compile(EMPTY_ATTRIBUTE_SCHEMA, '<root id="x"/>').to_data(format="pyxsd-json") == {
        "root": {"@id": "x"}
    }


def test_empty_plus_nil_uses_the_content_marker():
    document = _compile(
        EMPTY_ATTRIBUTE_SCHEMA.replace('name="root"', 'name="root" nillable="true"'),
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true" id="x"/>',
    )

    assert document.to_data(format="pyxsd-json") == {"root": {"@id": "x", "#content": None}}


def test_empty_plus_rejects_observed_children():
    document = _compile(EMPTY_ATTRIBUTE_SCHEMA, "<root/>")
    document.root._children_.append(type("Other", (), {"_name_": "other"})())

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_simple_plus_layout_uses_content_and_attributes():
    document = _compile(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, '<root id="r">text</root>')

    assert document.to_data(format="pyxsd-json") == {"root": {"@id": "r", "#content": "text"}}


def test_simple_plus_absent_attributes_are_omitted():
    document = _compile(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, "<root>text</root>")

    assert document.to_data(format="pyxsd-json") == {"root": {"#content": "text"}}


def test_simple_plus_nil_uses_the_content_marker():
    document = _compile(
        SIMPLE_CONTENT_NILABLE_SCHEMA,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true" id="r"/>',
    )

    assert document.to_data(format="pyxsd-json") == {"root": {"@id": "r", "#content": None}}


def test_simple_plus_wildcard_attribute_uses_its_lexical_value():
    document = _compile(SIMPLE_CONTENT_WILDCARD_SCHEMA, '<root custom="c">v</root>')

    assert document.to_data(format="pyxsd-json") == {"root": {"@custom": "c", "#content": "v"}}


def test_simple_plus_rejects_observed_children():
    document = _compile(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, "<root>text</root>")
    document.root._children_.append(type("Other", (), {"_name_": "other"})())

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_absent_declared_attribute_with_a_wildcard_attribute():
    document = _compile(SIMPLE_CONTENT_DECLARED_WILDCARD_SCHEMA, '<root custom="c">v</root>')

    assert document.to_data(format="pyxsd-json") == {"root": {"@custom": "c", "#content": "v"}}


def test_declared_and_wildcard_attributes_keep_declaration_order():
    document = _compile(SIMPLE_CONTENT_DECLARED_WILDCARD_SCHEMA, '<root custom="c" id="x">v</root>')

    assert document.to_data(format="pyxsd-json") == {
        "root": {"@id": "x", "@custom": "c", "#content": "v"}
    }


def test_declared_attribute_without_a_typed_value_uses_the_raw_lexical_form():
    document = _compile(SIMPLE_CONTENT_INT_ATTRIBUTE_SCHEMA, '<root id="oops">v</root>')
    del document.root.__dict__["id"]

    assert document.to_data(format="pyxsd-json") == {"root": {"@id": "oops", "#content": "v"}}


def test_simple_layout_rejects_observed_attributes():
    document = _compile(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="root" type="xs:int"/></xs:schema>',
        '<root a="x">5</root>',
    )

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_simple_layout_rejects_observed_children():
    document = _compile(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="root" type="xs:int"/></xs:schema>',
        "<root>5</root>",
    )
    document.root._children_.append(type("Other", (), {"_name_": "other"})())

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_list_layout_covers_zero_one_and_many_occurrences():
    assert _compile(REPEATED_SCHEMA, "<dates/>").to_data(format="pyxsd-json") == {"dates": []}
    assert _compile(REPEATED_SCHEMA, "<dates><date>2026-01-01</date></dates>").to_data(
        format="pyxsd-json"
    ) == {"dates": ["2026-01-01"]}


def test_list_layout_rejects_observed_attributes():
    document = _compile(REPEATED_SCHEMA, "<dates/>")
    document.root._attribs_ = {"a": "x"}

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_list_layout_rejects_non_whitespace_text():
    document = _compile(REPEATED_SCHEMA, "<dates>oops<date>2026-01-01</date></dates>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_list_layout_rejects_non_whitespace_child_tails():
    document = _compile(REPEATED_SCHEMA, "<dates><date>2026-01-01</date>oops</dates>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_list_nil_is_null():
    document = _compile(
        REPEATED_NILABLE_SCHEMA,
        '<dates xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>',
    )

    assert document.to_data(format="pyxsd-json") == {"dates": None}


def test_list_layout_rejects_a_different_child_name():
    document = _compile(REPEATED_SCHEMA, "<dates><date>2026-01-01</date></dates>")
    document.root._children_[0]._name_ = "other"

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_list_children_keep_their_attributes():
    document = _compile(
        REPEATED_ATTRIBUTED_CHILD_SCHEMA,
        '<root><item id="1">A</item><item id="2">B</item></root>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "root": [
            {"@id": 1, "#content": "A"},
            {"@id": 2, "#content": "B"},
        ]
    }


def test_list_plus_always_emits_the_child_array():
    document = _compile(
        REPEATED_ATTRIBUTE_SCHEMA,
        '<dates source="known"><date>2026-01-01</date></dates>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "dates": {"@source": "known", "date": ["2026-01-01"]}
    }


def test_list_plus_empty_child_array_and_absent_attribute():
    document = _compile(REPEATED_ATTRIBUTE_SCHEMA, '<dates source="known"/>')

    assert document.to_data(format="pyxsd-json") == {"dates": {"@source": "known", "date": []}}

    without_source = _compile(REPEATED_ATTRIBUTE_SCHEMA, "<dates/>")
    assert without_source.to_data(format="pyxsd-json") == {"dates": {"date": []}}


def test_list_plus_nil_omits_the_child_array():
    document = _compile(
        REPEATED_NILABLE_ATTRIBUTE_SCHEMA,
        '<dates xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:nil="true" source="known"/>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "dates": {"@source": "known", "#content": None}
    }


def test_list_plus_rejects_non_whitespace_text():
    document = _compile(
        REPEATED_ATTRIBUTE_SCHEMA,
        '<dates source="known">oops<date>2026-01-01</date></dates>',
    )

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_list_plus_rejects_non_whitespace_child_tails():
    document = _compile(
        REPEATED_ATTRIBUTE_SCHEMA,
        '<dates source="known"><date>2026-01-01</date>oops</dates>',
    )

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_record_uses_schema_order_and_omits_absent_children():
    document = _compile(RECORD_SCHEMA, '<root id="r"><b>B</b><a>A</a></root>')

    assert document.to_data(format="pyxsd-json") == {"root": {"@id": "r", "a": "A", "b": "B"}}


def test_record_distinguishes_absent_empty_and_nil_children():
    absent = _compile(RECORD_SCHEMA, "<root><a>A</a></root>")
    assert absent.to_data(format="pyxsd-json") == {"root": {"a": "A"}}

    empty = _compile(RECORD_SCHEMA, "<root><a>A</a><b/></root>")
    assert empty.to_data(format="pyxsd-json") == {"root": {"a": "A", "b": ""}}

    nilled = _compile(
        RECORD_SCHEMA,
        '<root><a>A</a><b xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:nil="true"/></root>',
    )
    assert nilled.to_data(format="pyxsd-json") == {"root": {"a": "A", "b": None}}


def test_record_attributes_follow_declaration_order():
    document = _compile(RECORD_ATTRIBUTE_ORDER_SCHEMA, '<root z="z" a="a"><a>A</a></root>')

    data = document.to_data(format="pyxsd-json")

    assert list(data["root"]) == ["@z", "@a", "a"]


def test_record_repeated_child_is_a_container_error():
    document = _compile(RECORD_SCHEMA, "<root><a>A</a><a>B</a></root>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_record_unknown_child_is_a_container_error():
    document = _compile(RECORD_SCHEMA, "<root><a>A</a></root>")
    document.root._children_[0]._name_ = "unknown"

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_record_rejects_non_whitespace_text():
    document = _compile(RECORD_SCHEMA, "<root>oops<a>A</a></root>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_record_rejects_non_whitespace_child_tails():
    document = _compile(RECORD_SCHEMA, "<root><a>A</a>oops</root>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_record_nil_uses_the_content_marker():
    document = _compile(
        RECORD_SCHEMA.replace('name="root"', 'name="root" nillable="true"'),
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>',
    )

    assert document.to_data(format="pyxsd-json") == {"root": {"#content": None}}


def test_sequence_preserves_document_order_and_attribute_maps():
    document = _compile(SEQUENCE_ATTRIBUTE_SCHEMA, '<root id="r"><b>B</b><a>A</a></root>')

    assert document.to_data(format="pyxsd-json") == {"root": [{"@id": "r"}, {"b": "B"}, {"a": "A"}]}


def test_sequence_attribute_maps_sort_by_expanded_name():
    document = _compile(SEQUENCE_ATTRIBUTE_ORDER_SCHEMA, '<root z="z" a="a"><a>A</a></root>')

    data = document.to_data(format="pyxsd-json")

    assert list(data["root"][0]) == ["@a", "@z"]


def test_sequence_covers_zero_occurrences_and_interleaved_repeats():
    empty = _compile(SEQUENCE_OPTIONAL_SCHEMA, "<root/>")
    assert empty.to_data(format="pyxsd-json") == {"root": []}

    document = _compile(
        SEQUENCE_REPEATED_CHOICE_SCHEMA,
        "<root><a>1</a><b>x</b><a>2</a></root>",
    )
    assert document.to_data(format="pyxsd-json") == {"root": [{"a": 1}, {"b": "x"}, {"a": 2}]}


def test_sequence_nil_uses_a_content_marker_after_attributes():
    document = _compile(
        SEQUENCE_NILABLE_SCHEMA,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true" id="r"/>',
    )

    assert document.to_data(format="pyxsd-json") == {"root": [{"@id": "r"}, {"#content": None}]}


def test_sequence_wildcard_child_uses_its_actual_name():
    document = _compile(SEQUENCE_WILDCARD_SCHEMA, "<root><known>v</known></root>")

    assert document.to_data(format="pyxsd-json") == {"root": [{"known": "v"}]}


def test_sequence_rejects_non_whitespace_child_tails():
    document = _compile(SEQUENCE_OPTIONAL_SCHEMA, "<root><a>A</a>oops</root>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_sequence_rejects_non_whitespace_text():
    document = _compile(SEQUENCE_OPTIONAL_SCHEMA, "<root>oops</root>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


def test_mixed_retains_leading_text_and_every_tail():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root>a<i>x</i>b<i>y</i>c</root>")

    assert document.to_data(format="pyxsd-json") == {
        "root": ["a", {"i": "x"}, "b", {"i": "y"}, "c"]
    }


def test_mixed_child_without_leading_text_or_tail():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root><i>x</i></root>")

    assert document.to_data(format="pyxsd-json") == {"root": [{"i": "x"}]}


def test_plain_string_character_data_is_accepted():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root><i>x</i></root>")
    document.root._value_ = "lead"

    assert document.to_data(format="pyxsd-json") == {"root": ["lead", {"i": "x"}]}


def test_mixed_nil_uses_a_content_marker_after_attributes():
    document = _compile(
        MIXED_NILABLE_SCHEMA,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true" id="p1"/>',
    )

    assert document.to_data(format="pyxsd-json") == {"root": [{"@id": "p1"}, {"#content": None}]}


def test_mixed_rejects_nil_with_children():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root><i>x</i></root>")
    document.root._nil_ = True

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="pyxsd-json")


# ---------------------------------------------------------------------------
# Names, namespaces, substitution, and xsi:type
# ---------------------------------------------------------------------------


def test_namespaced_fixture_uses_eqname_keys_for_elements_and_attributes():
    document = _compile(
        NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
        '<t:root xmlns:t="urn:example" t:attr="5"><t:child>v</t:child></t:root>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": [
            {"@Q{urn:example}attr": 5},
            {"Q{urn:example}child": "v"},
        ]
    }


def test_legacy_namespace_mode_reconstructs_eqnames_from_the_schema():
    document = _compile(
        NAMESPACED_LEGACY_SCHEMA,
        '<t:root xmlns:t="urn:example" attr="5"><t:child>v</t:child></t:root>',
        mode=BindingPolicy(namespaces="legacy"),
    )

    assert document.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": [
            {"@attr": 5},
            {"Q{urn:example}child": "v"},
        ]
    }


def test_xsi_schema_location_uses_the_xsi_eqname():
    document = _compile(
        SEQUENCE_ATTRIBUTE_SCHEMA,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="urn:x s.xsd"><a>A</a><b>B</b></root>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "root": [
            {
                "@Q{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation": "urn:x s.xsd"
            },
            {"a": "A"},
            {"b": "B"},
        ]
    }


def test_unknown_wildcard_child_uses_its_local_name():
    document = _compile(WILDCARD_GENERIC_SCHEMA, "<root><other>v</other></root>")

    assert document.to_data(format="pyxsd-json") == {"root": [{"other": ["v"]}]}


def test_qname_content_becomes_an_eqname():
    document = _compile(QNAME_ROOT_SCHEMA, '<root xmlns:p="urn:example">p:v</root>')

    assert document.to_data(format="pyxsd-json") == {"root": "Q{urn:example}v"}


def test_substitution_members_keep_their_actual_names_and_order():
    document = _compile(
        SUBSTITUTION_SCHEMA,
        "<root><item>1</item><special>2</special><extra>3</extra></root>",
    )

    assert document.to_data(format="pyxsd-json") == {
        "root": [{"item": "1"}, {"special": "2"}, {"extra": "3"}]
    }


def test_an_abstract_substitution_head_with_one_member_is_a_list():
    document = _compile(ABSTRACT_SUBSTITUTION_SCHEMA, "<root><special>2</special></root>")

    assert document.to_data(format="pyxsd-json") == {"root": ["2"]}


def test_namespaced_substitution_members_use_eqnames():
    document = _compile(
        NAMESPACED_SUBSTITUTION_SCHEMA,
        '<t:root xmlns:t="urn:example"><t:special>2</t:special></t:root>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": [{"Q{urn:example}special": "2"}]
    }


def test_xsi_type_marks_the_actual_type_and_uses_its_layout():
    document = _compile(XSITYPE_SCHEMA, XSITYPE_INSTANCE)

    assert document.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": [
            {"@xsi:type": "Q{urn:example}Ext"},
            {"Q{urn:example}a": "x"},
            {"Q{urn:example}b": 2},
        ]
    }


def test_xsi_type_equal_to_the_declared_type_is_omitted():
    document = _compile(
        XSITYPE_EQUAL_SCHEMA,
        '<t:root xmlns:t="urn:example" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:type="t:Base"><t:a>x</t:a></t:root>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": [{"Q{urn:example}a": "x"}]
    }


def test_xsi_type_on_a_simple_root_selects_simple_plus():
    document = _compile(XSITYPE_SIMPLE_SCHEMA, XSITYPE_SIMPLE_INSTANCE)

    assert document.to_data(format="pyxsd-json") == {
        "root": {
            "@xsi:type": "Q{http://www.w3.org/2001/XMLSchema}int",
            "#content": 5,
        }
    }


def test_xsi_type_on_a_child_is_kept_in_its_child_map():
    document = _compile(
        XSITYPE_CHILD_SCHEMA,
        '<root><value xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:type="xs:int">5</value></root>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "root": [
            {
                "value": {
                    "@xsi:type": "Q{http://www.w3.org/2001/XMLSchema}int",
                    "#content": 5,
                }
            }
        ]
    }


def test_xsi_type_promotes_a_list_to_list_plus():
    document = _compile(
        XSITYPE_LIST_SCHEMA,
        '<t:root xmlns:t="urn:example" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:type="t:ListType"><t:date>2026-01-01</t:date></t:root>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": {
            "@xsi:type": "Q{urn:example}ListType",
            "Q{urn:example}date": ["2026-01-01"],
        }
    }


def test_xsi_type_keeps_a_record_a_record():
    document = _compile(
        XSITYPE_RECORD_SCHEMA,
        '<t:root xmlns:t="urn:example" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:type="t:RecordType"><t:a>A</t:a></t:root>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": {
            "@xsi:type": "Q{urn:example}RecordType",
            "Q{urn:example}a": "A",
        }
    }


def test_xsi_type_for_an_unnamespaced_generated_type_uses_the_local_eqname():
    document = _compile(
        UNNAMESPACED_XSITYPE_SCHEMA,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="T"><a>A</a></root>',
    )

    assert document.to_data(format="pyxsd-json") == {"root": [{"@xsi:type": "T"}, {"a": "A"}]}


def test_xsi_type_marker_with_a_wildcard_attribute_on_a_builtin_type():
    document = _compile(
        XSITYPE_SIMPLE_SCHEMA,
        '<root xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:type="xs:int" xsi:noNamespaceSchemaLocation="urn:x s.xsd">5</root>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "root": {
            "@xsi:type": "Q{http://www.w3.org/2001/XMLSchema}int",
            "@Q{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation": "urn:x s.xsd",
            "#content": 5,
        }
    }


def test_xsi_type_promotes_an_empty_type_to_empty_plus():
    document = _compile(
        EMPTY_XSITYPE_SCHEMA,
        '<t:root xmlns:t="urn:example" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:type="t:EmptyType"/>',
    )

    assert document.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": {"@xsi:type": "Q{urn:example}EmptyType"}
    }


# ---------------------------------------------------------------------------
# Parker
# ---------------------------------------------------------------------------


def _warning_texts(record) -> list[str]:
    return [str(item.message) for item in record if isinstance(item.message, PyXSDWarning)]


def _qname_node(text: str) -> QName:
    """A QName outside any qname context, shaped as one bound simple node."""
    node = QName(text)
    node._name_ = "root"
    node._attribs_ = {}
    node._children_ = []
    node._nil_ = None
    return node


def _compile_root_ambiguity(tmp_path) -> pyxsd.Document:
    (tmp_path / "b.xsd").write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:b="urn:b"
           targetNamespace="urn:b"
           elementFormDefault="qualified">
  <xs:element name="root" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:a="urn:a"
           xmlns:b="urn:b"
           targetNamespace="urn:a"
           elementFormDefault="qualified">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:element name="root" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    schema = pyxsd.Schema.compile(str(main))
    return schema.parse(StringIO('<a:root xmlns:a="urn:a">x</a:root>'))


def _compile_abstract_root(tmp_path) -> pyxsd.Document:
    (tmp_path / "b.xsd").write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:b="urn:b"
           targetNamespace="urn:b"
           elementFormDefault="qualified">
  <xs:element name="root" type="xs:string" abstract="true"/>
</xs:schema>""",
        encoding="utf-8",
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:a="urn:a"
           xmlns:b="urn:b"
           targetNamespace="urn:a"
           elementFormDefault="qualified">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:element name="root" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    schema = pyxsd.Schema.compile(str(main))
    return schema.parse(StringIO('<a:root xmlns:a="urn:a">x</a:root>'))


def _compile_wildcard_ambiguity(tmp_path) -> pyxsd.Document:
    (tmp_path / "b.xsd").write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:b="urn:b"
           targetNamespace="urn:b"
           elementFormDefault="qualified">
  <xs:element name="item" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:a="urn:a"
           xmlns:b="urn:b"
           targetNamespace="urn:a"
           elementFormDefault="qualified">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:element name="item" type="xs:string"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>""",
        encoding="utf-8",
    )
    schema = pyxsd.Schema.compile(str(main))
    return schema.parse(
        StringIO('<a:root xmlns:a="urn:a" xmlns:b="urn:b"><b:item>x</b:item></a:root>')
    )


def _compile_optional_namespace(tmp_path) -> pyxsd.Document:
    (tmp_path / "a.xsd").write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:a="urn:a"
           targetNamespace="urn:a"
           elementFormDefault="qualified">
  <xs:element name="first" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:a="urn:a"
           xmlns:b="urn:b"
           targetNamespace="urn:b"
           elementFormDefault="qualified">
  <xs:import namespace="urn:a" schemaLocation="a.xsd"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="a:first" minOccurs="0"/>
        <xs:element name="second" type="xs:string" minOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>""",
        encoding="utf-8",
    )
    schema = pyxsd.Schema.compile(str(main))
    return schema.parse(StringIO('<b:root xmlns:b="urn:b"><b:second>v</b:second></b:root>'))


def test_parker_absorbs_the_root_and_converts_scalar_families():
    assert _compile_fixture("simple").to_data(format="parker") == 42
    assert json.loads(_compile_fixture("simple").to_json(format="parker")) == 42

    assert _compile(DECIMAL_ROOT_SCHEMA, "<root>1.20</root>").to_data(format="parker") == "1.20"
    assert _compile(DOUBLE_ROOT_SCHEMA, "<root>1.5</root>").to_data(format="parker") == 1.5
    assert _compile(IDREFS_ROOT_SCHEMA, "<root>a b</root>").to_data(format="parker") == [
        "a",
        "b",
    ]
    assert _compile(DOUBLE_LIST_SCHEMA, "<values><v>INF</v><v>-INF</v><v>NaN</v></values>").to_data(
        format="parker"
    ) == {"v": ["INF", "-INF", "NaN"]}


def test_parker_text_only_element_is_a_scalar():
    document = _compile_fixture("attribute")

    with pytest.warns(PyXSDWarning, match="attributes-lost"):
        assert document.to_data(format="parker") == "text"


def test_parker_empty_content_is_an_object_and_nil_is_null():
    assert _compile(EMPTY_SCHEMA, "<root/>").to_data(format="parker") == {}
    assert _compile(EMPTY_ATTRIBUTE_SCHEMA, "<root/>").to_data(format="parker") == {}
    assert _compile(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, "<root/>").to_data(format="parker") == ""
    assert _compile_fixture("nillable").to_data(format="parker", fidelity="strict") is None


def test_parker_nil_element_only_content_is_null():
    document = _compile(
        REPEATED_NILABLE_SCHEMA,
        '<dates xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>',
    )

    assert document.to_data(format="parker", fidelity="strict") is None


def test_parker_empty_mixed_content_is_an_object():
    assert (
        _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root/>").to_data(format="parker", fidelity="strict")
        == {}
    )


def test_parker_empty_sequence_is_an_object():
    assert (
        _compile(SEQUENCE_OPTIONAL_SCHEMA, "<root/>").to_data(format="parker", fidelity="strict")
        == {}
    )


@pytest.mark.parametrize(
    ("format", "expected"),
    [
        ("parker", "x"),
        ("badgerfish", {"root": "x"}),
        ("jsonml", ["root", "x"]),
    ],
)
def test_conventional_formats_export_text_only_any_type(format, expected):
    document = _compile(ANYTYPE_ROOT_SCHEMA, "<root>x</root>")

    assert document.to_data(format=format, fidelity="strict") == expected


@pytest.mark.parametrize(
    ("format", "expected"),
    [
        ("parker", True),
        ("badgerfish", {"root": True}),
        ("jsonml", ["root", True]),
    ],
)
def test_conventional_formats_export_boolean_scalars(format, expected):
    document = _compile(BOOLEAN_ROOT_SCHEMA, "<root>true</root>")

    assert document.to_data(format=format, fidelity="strict") == expected


def test_parker_repeated_children_are_arrays_only_when_repeated():
    assert _compile_fixture("repeated").to_data(format="parker", fidelity="strict") == {
        "date": ["2026-01-01", "2026-02-01"]
    }
    assert _compile(REPEATED_SCHEMA, "<dates><date>2026-01-01</date></dates>").to_data(
        format="parker", fidelity="strict"
    ) == {"date": "2026-01-01"}
    assert _compile(REPEATED_SCHEMA, "<dates/>").to_data(format="parker", fidelity="strict") == {}


def test_parker_actual_repeats_of_a_nonrepeatable_child_use_an_array():
    document = _compile(SEQUENCE_OPTIONAL_SCHEMA, "<root><a>A</a></root>")
    document.root._children_.append(document.root._children_[0])

    assert document.to_data(format="parker", fidelity="strict") == {"a": ["A", "A"]}


def test_parker_ordered_unique_children_keep_schema_order():
    assert _compile_fixture("ordered").to_data(format="parker", fidelity="strict") == {
        "head": "Introduction",
        "p": ["First", "Second"],
        "note": "End",
    }
    assert _compile(
        ORDERED_SCHEMA_FOR_PARKER, "<section><head>H</head><p>P</p><note>N</note></section>"
    ).to_data(format="parker", fidelity="strict") == {
        "head": "H",
        "p": "P",
        "note": "N",
    }


def test_parker_record_ignores_instance_order():
    document = _compile(RECORD_SCHEMA, "<root><b>B</b><a>A</a></root>")

    assert document.to_data(format="parker", fidelity="strict") == {"a": "A", "b": "B"}


def test_parker_strict_rejects_attributes_without_dropping_them():
    document = _compile_fixture("attribute")

    with pytest.raises(PyXSDError, match="attributes-lost") as caught:
        document.to_data(format="parker", fidelity="strict")

    assert json_pointer([]) in str(caught.value)


def test_parker_best_effort_omits_attributes_and_warns():
    document = _compile_fixture("attribute")

    with pytest.warns(PyXSDWarning, match="attributes-lost"):
        assert document.to_data(format="parker") == "text"


def test_parker_reports_nested_attribute_loss_with_its_pointer():
    document = _compile(
        REPEATED_ATTRIBUTED_CHILD_SCHEMA,
        '<root><item id="1">A</item></root>',
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert document.to_data(format="parker") == {"item": "A"}

    texts = _warning_texts(caught)
    assert any("attributes-lost" in text and "at '/item'" in text for text in texts)

    with pytest.raises(PyXSDError, match="attributes-lost") as caught_error:
        document.to_data(format="parker", fidelity="strict")
    assert "/item" in str(caught_error.value)


def test_parker_treats_an_explicit_actual_type_as_an_attribute_loss():
    document = _compile(XSITYPE_SCHEMA, XSITYPE_INSTANCE)

    with pytest.raises(PyXSDError, match="attributes-lost"):
        document.to_data(format="parker", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="attributes-lost"):
        assert document.to_data(format="parker") == {"a": "x", "b": 2}


def test_parker_strict_rejects_mixed_content_positions():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root>a<i>x</i>b</root>")

    with pytest.raises(PyXSDError, match="mixed-content"):
        document.to_data(format="parker", fidelity="strict")


def test_parker_best_effort_concatenates_text_under_dollar():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root>a<i>x</i>b<i>y</i>c</root>")

    with pytest.warns(PyXSDWarning, match="mixed-content"):
        assert document.to_data(format="parker") == {"$": "abc", "i": ["x", "y"]}


def test_parker_best_effort_keeps_mixed_attributes_as_a_warning():
    document = _compile_fixture("mixed")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert document.to_data(format="parker") == {"$": "This is a  mess!", "i": "fine"}

    texts = _warning_texts(caught)
    assert any("attributes-lost" in text for text in texts)
    assert any("mixed-content" in text for text in texts)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("<root>text</root>", "text"),
        ("<root><i>x</i></root>", {"i": "x"}),
    ],
)
def test_parker_text_only_and_child_only_mixed_content_is_representable(source, expected):
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, source)

    assert document.to_data(format="parker", fidelity="strict") == expected


def test_parker_mixed_distinct_child_names_are_an_order_loss():
    document = _compile(MIXED_TWO_CHILD_SCHEMA, "<root><i>x</i><b>y</b></root>")

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        document.to_data(format="parker", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        assert document.to_data(format="parker") == {"i": "x", "b": "y"}


def test_parker_mixed_one_repeated_child_name_keeps_its_order():
    document = _compile(MIXED_TWO_CHILD_SCHEMA, "<root><i>x</i><i>y</i></root>")

    assert document.to_data(format="parker", fidelity="strict") == {"i": ["x", "y"]}


def test_parker_strict_rejects_interleaved_children():
    document = _compile(
        SEQUENCE_REPEATED_CHOICE_SCHEMA,
        "<root><a>1</a><b>x</b><a>2</a></root>",
    )

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        document.to_data(format="parker", fidelity="strict")


def test_parker_best_effort_groups_interleaved_children():
    document = _compile(
        SEQUENCE_REPEATED_CHOICE_SCHEMA,
        "<root><a>1</a><b>x</b><a>2</a></root>",
    )

    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        assert document.to_data(format="parker") == {"a": [1, 2], "b": "x"}


def test_parker_strict_rejects_order_the_schema_cannot_reconstruct():
    document = _compile(SEQUENCE_ATTRIBUTE_SCHEMA, "<root><b>B</b><a>A</a></root>")

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        document.to_data(format="parker", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        assert document.to_data(format="parker") == {"a": "A", "b": "B"}


def test_parker_reconstructs_a_unique_namespaced_local_name():
    document = _compile(
        NAMESPACED_LEGACY_SCHEMA,
        '<t:root xmlns:t="urn:example"><t:child>v</t:child></t:root>',
    )

    assert document.to_data(format="parker", fidelity="strict") == {"child": "v"}


def test_parker_strict_rejects_a_local_name_collision():
    document = _compile(
        PARKER_LOCAL_COLLISION_SCHEMA,
        '<t:root xmlns:t="urn:example"><t:item>A</t:item><item>B</item></t:root>',
    )

    with pytest.raises(PyXSDError, match="name-ambiguous") as caught:
        document.to_data(format="parker", fidelity="strict")

    assert "/item" in str(caught.value)


def test_parker_best_effort_merges_a_local_name_collision():
    document = _compile(
        PARKER_LOCAL_COLLISION_SCHEMA,
        '<t:root xmlns:t="urn:example"><t:item>A</t:item><item>B</item></t:root>',
    )

    with pytest.warns(PyXSDWarning, match="name-ambiguous"):
        assert document.to_data(format="parker") == {"item": ["A", "B"]}


def test_parker_strict_rejects_an_ambiguous_root(tmp_path):
    document = _compile_root_ambiguity(tmp_path)

    with pytest.raises(PyXSDError, match="root-ambiguous") as caught:
        document.to_data(format="parker", fidelity="strict")

    assert json_pointer([]) in str(caught.value)


def test_parker_best_effort_warns_for_an_ambiguous_root(tmp_path):
    document = _compile_root_ambiguity(tmp_path)

    with pytest.warns(PyXSDWarning, match="root-ambiguous"):
        assert document.to_data(format="parker") == "x"


def test_parker_ignores_an_abstract_global_with_the_same_local_name(tmp_path):
    document = _compile_abstract_root(tmp_path)

    assert document.to_data(format="parker", fidelity="strict") == "x"


def test_parker_strict_rejects_a_wildcard_local_name_collision(tmp_path):
    document = _compile_wildcard_ambiguity(tmp_path)

    with pytest.raises(PyXSDError, match="name-ambiguous") as caught:
        document.to_data(format="parker", fidelity="strict")

    assert "/item" in str(caught.value)


def test_conventional_prefixes_follow_the_schema_not_the_instance(tmp_path):
    document = _compile_optional_namespace(tmp_path)

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "ns1:root": {"@xmlns": {"ns1": "urn:b"}, "ns1:second": "v"}
    }
    assert document.to_data(format="jsonml") == [
        "ns1:root",
        {"xmlns:ns1": "urn:b"},
        ["ns1:second", "v"],
    ]


def test_parker_strict_rejects_a_namespace_dependent_qname_value():
    document = _compile(QNAME_ROOT_SCHEMA, '<root xmlns:p="urn:example">p:v</root>')

    with pytest.raises(PyXSDError, match="namespace-lost"):
        document.to_data(format="parker", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="namespace-lost"):
        assert document.to_data(format="parker") == "p:v"


def test_parker_qname_loss_reports_the_child_pointer():
    document = _compile(QNAME_CHILD_SCHEMA, '<root xmlns:p="urn:example"><ref>p:v</ref></root>')

    with pytest.raises(PyXSDError, match="namespace-lost") as caught:
        document.to_data(format="parker", fidelity="strict")
    assert "/ref" in str(caught.value)

    with pytest.warns(PyXSDWarning, match="namespace-lost"):
        assert document.to_data(format="parker") == {"ref": "p:v"}


def test_parker_unqualified_qname_values_need_no_namespace():
    document = _compile(QNAME_ROOT_SCHEMA, "<root>v</root>")

    assert document.to_data(format="parker", fidelity="strict") == "v"


def test_parker_unresolved_qname_values_are_a_namespace_loss():
    node = _qname_node("p:v")

    with pytest.raises(PyXSDError, match="namespace-lost"):
        bound_to_data(node, format="parker", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="namespace-lost"):
        assert bound_to_data(node, format="parker") == "p:v"


def test_parker_qname_lists_warn_for_each_namespace_dependent_item():
    document = _compile(
        QNAME_LIST_SCHEMA,
        '<t:root xmlns:t="urn:example" xmlns:p="urn:other">p:a p:b</t:root>',
    )

    with pytest.raises(PyXSDError, match="namespace-lost"):
        document.to_data(format="parker", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="namespace-lost"):
        assert document.to_data(format="parker") == ["p:a", "p:b"]


def test_parker_uses_a_wildcard_runtime_local_name():
    document = _compile(WILDCARD_GENERIC_SCHEMA, "<root><other>v</other></root>")

    assert document.to_data(format="parker", fidelity="strict") == {"other": "v"}


@pytest.mark.parametrize(
    ("schema", "source", "mode"),
    [
        (SEQUENCE_OPTIONAL_SCHEMA, "<root>oops</root>", None),
        (EMPTY_SCHEMA, "<root><x/></root>", BindingPolicy(undeclared_content="generic")),
    ],
)
def test_parker_rejects_content_its_layout_cannot_carry(schema, source, mode):
    document = _compile(schema, source, mode=mode)

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="parker")


def test_parker_rejects_a_simple_element_with_children():
    document = _compile(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, "<root>text</root>")
    document.root._children_.append(type("Other", (), {"_name_": "other"})())

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="parker")


def test_parker_rejects_element_only_list_text():
    document = _compile(REPEATED_SCHEMA, "<dates>oops<date>2026-01-01</date></dates>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="parker")


def test_parker_rejects_nil_with_children():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root><i>x</i></root>")
    document.root._nil_ = True

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="parker")


def test_parker_export_is_deterministic_and_json_round_trips():
    document = _compile_fixture("ordered")

    assert document.to_data(format="parker") == document.to_data(format="parker")
    assert json.loads(document.to_json(format="parker")) == document.to_data(format="parker")


# ---------------------------------------------------------------------------
# BadgerFish
# ---------------------------------------------------------------------------

SHARED_BADGERFISH = {
    "simple": {"simple": 42},
    "attribute": {"attributed": {"@id": "a1", "$": "text"}},
    "repeated": {"dates": {"date": ["2026-01-01", "2026-02-01"]}},
    "ordered": {"section": {"head": "Introduction", "p": ["First", "Second"], "note": "End"}},
    "namespaced": {
        "ns0:root": {
            "@xmlns": {"ns0": "urn:example"},
            "@attr": "a",
            "ns0:child": "v",
        }
    },
    "nillable": {"nillable": {"@xmlns": {"xsi": XSI_NAMESPACE}, "@xsi:nil": True}},
    "recursive": {
        "node": {
            "@name": "a",
            "node": [{"@name": "b", "node": [{"@name": "c"}]}],
        }
    },
}


@pytest.mark.parametrize("name", sorted(SHARED_BADGERFISH))
def test_badgerfish_exports_each_shared_fixture_in_strict_mode(name):
    document = _compile_fixture(name)
    expected = SHARED_BADGERFISH[name]

    assert document.to_data(format="badgerfish", fidelity="strict") == expected
    assert json.loads(document.to_json(format="badgerfish")) == expected


def test_badgerfish_root_object_keeps_schema_property_order():
    document = _compile_fixture("namespaced")

    data = document.to_data(format="badgerfish", fidelity="strict")

    assert list(data["ns0:root"]) == ["@xmlns", "@attr", "ns0:child"]


def test_badgerfish_mixed_content_is_a_strict_loss():
    document = _compile_fixture("mixed")

    with pytest.raises(PyXSDError, match="mixed-content") as caught:
        document.to_data(format="badgerfish", fidelity="strict")

    assert json_pointer(["para"]) in str(caught.value)


def test_badgerfish_best_effort_groups_mixed_text_and_children():
    document = _compile_fixture("mixed")

    with pytest.warns(PyXSDWarning, match="mixed-content"):
        assert document.to_data(format="badgerfish") == {
            "para": {"@id": "p1", "$": "This is a  mess!", "i": ["fine"]}
        }


def test_badgerfish_text_only_mixed_content_is_a_scalar():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root>text</root>")

    assert document.to_data(format="badgerfish", fidelity="strict") == {"root": "text"}


def test_badgerfish_text_only_mixed_content_keeps_attributes():
    document = _compile(MIXED_NILABLE_SCHEMA, '<root id="p1">text</root>')

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "root": {"@id": "p1", "$": "text"}
    }


def test_badgerfish_empty_mixed_content_is_an_object():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root/>")

    assert document.to_data(format="badgerfish", fidelity="strict") == {"root": {}}


def test_badgerfish_empty_sequence_is_an_object():
    document = _compile(SEQUENCE_OPTIONAL_SCHEMA, "<root/>")

    assert document.to_data(format="badgerfish", fidelity="strict") == {"root": {}}


def test_badgerfish_children_only_mixed_content_groups_repeats():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root><i>x</i><i>y</i></root>")

    assert document.to_data(format="badgerfish", fidelity="strict") == {"root": {"i": ["x", "y"]}}


def test_badgerfish_mixed_distinct_child_names_are_an_order_loss():
    document = _compile(MIXED_TWO_CHILD_SCHEMA, "<root><i>x</i><b>y</b></root>")

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        document.to_data(format="badgerfish", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        assert document.to_data(format="badgerfish") == {"root": {"i": ["x"], "b": ["y"]}}


def test_badgerfish_mixed_one_repeated_child_name_keeps_its_order():
    document = _compile(MIXED_TWO_CHILD_SCHEMA, "<root><i>x</i><i>y</i></root>")

    assert document.to_data(format="badgerfish", fidelity="strict") == {"root": {"i": ["x", "y"]}}


def test_badgerfish_strict_rejects_interleaved_children():
    document = _compile(
        SEQUENCE_REPEATED_CHOICE_SCHEMA,
        "<root><a>1</a><b>x</b><a>2</a></root>",
    )

    with pytest.raises(PyXSDError, match="order-ambiguous") as caught:
        document.to_data(format="badgerfish", fidelity="strict")

    assert json_pointer(["root"]) in str(caught.value)


def test_badgerfish_best_effort_groups_interleaved_children():
    document = _compile(
        SEQUENCE_REPEATED_CHOICE_SCHEMA,
        "<root><a>1</a><b>x</b><a>2</a></root>",
    )

    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        assert document.to_data(format="badgerfish") == {"root": {"a": [1, 2], "b": ["x"]}}


def test_badgerfish_strict_rejects_order_that_schema_cannot_reconstruct():
    document = _compile(SEQUENCE_ATTRIBUTE_SCHEMA, "<root><b>B</b><a>A</a></root>")

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        document.to_data(format="badgerfish", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        assert document.to_data(format="badgerfish") == {"root": {"a": "A", "b": "B"}}


def test_badgerfish_record_layout_ignores_instance_order():
    document = _compile(RECORD_SCHEMA, '<root id="r"><b>B</b><a>A</a></root>')

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "root": {"@id": "r", "a": "A", "b": "B"}
    }


def test_badgerfish_repeatable_child_is_an_array_even_once():
    assert _compile(REPEATED_SCHEMA, "<dates><date>2026-01-01</date></dates>").to_data(
        format="badgerfish", fidelity="strict"
    ) == {"dates": {"date": ["2026-01-01"]}}
    assert _compile(REPEATED_SCHEMA, "<dates/>").to_data(
        format="badgerfish", fidelity="strict"
    ) == {"dates": {}}
    assert _compile(REPEATED_ATTRIBUTE_SCHEMA, '<dates source="known"/>').to_data(
        format="badgerfish", fidelity="strict"
    ) == {"dates": {"@source": "known"}}


def test_badgerfish_nonrepeatable_actual_repeat_still_uses_an_array():
    document = _compile(SEQUENCE_OPTIONAL_SCHEMA, "<root><a>A</a></root>")
    document.root._children_.append(document.root._children_[0])

    assert document.to_data(format="badgerfish", fidelity="strict") == {"root": {"a": ["A", "A"]}}


def test_badgerfish_list_layout_rejects_a_different_child_name():
    document = _compile(REPEATED_SCHEMA, "<dates><date>2026-01-01</date></dates>")
    document.root._children_[0]._name_ = "other"

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="badgerfish")


def test_badgerfish_keeps_attributes_on_repeated_children():
    document = _compile(
        REPEATED_ATTRIBUTED_CHILD_SCHEMA,
        '<root><item id="1">A</item></root>',
    )

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "root": {"item": [{"@id": 1, "$": "A"}]}
    }


def test_badgerfish_empty_and_nil_are_distinct():
    assert _compile(EMPTY_SCHEMA, "<root/>").to_data(format="badgerfish", fidelity="strict") == {
        "root": {}
    }
    assert _compile(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, "<root/>").to_data(
        format="badgerfish", fidelity="strict"
    ) == {"root": ""}
    assert _compile(EMPTY_ATTRIBUTE_SCHEMA, '<root id="x"/>').to_data(
        format="badgerfish", fidelity="strict"
    ) == {"root": {"@id": "x"}}


@pytest.mark.parametrize(
    ("format", "expected"),
    [
        ("parker", None),
        (
            "badgerfish",
            {
                "ns0:root": {
                    "@xmlns": {"ns0": "urn:example", "xsi": XSI_NAMESPACE},
                    "@xsi:nil": True,
                }
            },
        ),
        (
            "jsonml",
            ["ns0:root", {"xmlns:ns0": "urn:example"}, None],
        ),
    ],
)
def test_conventional_formats_keep_a_namespaced_nil_root_distinct(format, expected):
    document = _compile(
        NAMESPACED_NILABLE_ROOT_SCHEMA,
        '<t:root xmlns:t="urn:example" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>',
    )

    assert document.to_data(format=format, fidelity="strict") == expected


def test_badgerfish_nil_keeps_attributes_and_never_adds_text_or_children():
    document = _compile(
        SIMPLE_CONTENT_NILABLE_SCHEMA,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true" id="r"/>',
    )

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "root": {
            "@xmlns": {"xsi": XSI_NAMESPACE},
            "@xsi:nil": True,
            "@id": "r",
        }
    }


def test_badgerfish_nil_list_omits_children():
    document = _compile(
        REPEATED_NILABLE_ATTRIBUTE_SCHEMA,
        '<dates xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:nil="true" source="known"/>',
    )

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "dates": {
            "@xmlns": {"xsi": XSI_NAMESPACE},
            "@xsi:nil": True,
            "@source": "known",
        }
    }


def test_badgerfish_namespaced_attribute_uses_a_lexical_qname():
    document = _compile(
        NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
        '<t:root xmlns:t="urn:example" t:attr="5"><t:child>v</t:child></t:root>',
    )

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "ns0:root": {
            "@xmlns": {"ns0": "urn:example"},
            "@ns0:attr": 5,
            "ns0:child": "v",
        }
    }


def test_badgerfish_namespaced_simple_root_uses_dollar_for_its_declaration():
    document = _compile(
        NAMESPACED_SIMPLE_ROOT_SCHEMA,
        '<t:root xmlns:t="urn:example">x</t:root>',
    )

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "ns0:root": {"@xmlns": {"ns0": "urn:example"}, "$": "x"}
    }


def test_badgerfish_xml_prefix_is_implicit():
    document = _compile(
        SIMPLE_CONTENT_DECLARED_WILDCARD_SCHEMA,
        '<root xml:lang="en">v</root>',
    )

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "root": {"@xml:lang": "en", "$": "v"}
    }


def test_badgerfish_xsi_schema_location_uses_the_xsi_prefix():
    document = _compile(
        SEQUENCE_ATTRIBUTE_SCHEMA,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="urn:x s.xsd"><a>A</a><b>B</b></root>',
    )

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "root": {
            "@xmlns": {"xsi": XSI_NAMESPACE},
            "@xsi:noNamespaceSchemaLocation": "urn:x s.xsd",
            "a": "A",
            "b": "B",
        }
    }


def test_badgerfish_qname_content_allocates_a_declaration():
    document = _compile(QNAME_ROOT_SCHEMA, '<root xmlns:p="urn:example">p:v</root>')

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "root": {
            "@xmlns": {HEX_EXAMPLE: "urn:example"},
            "$": f"{HEX_EXAMPLE}:v",
        }
    }


def test_badgerfish_qname_child_and_attribute_keep_their_context():
    child = _compile(
        QNAME_CHILD_SCHEMA,
        '<root xmlns:p="urn:example"><ref>p:v</ref></root>',
    )
    assert child.to_data(format="badgerfish", fidelity="strict") == {
        "root": {
            "@xmlns": {HEX_EXAMPLE: "urn:example"},
            "ref": f"{HEX_EXAMPLE}:v",
        }
    }

    attribute = _compile(
        QNAME_ATTRIBUTE_SCHEMA,
        '<root xmlns:p="urn:example" ref="p:v"/>',
    )
    assert attribute.to_data(format="badgerfish", fidelity="strict") == {
        "root": {
            "@xmlns": {HEX_EXAMPLE: "urn:example"},
            "@ref": f"{HEX_EXAMPLE}:v",
        }
    }


def test_badgerfish_qname_lists_allocate_once_per_namespace():
    document = _compile(
        QNAME_LIST_SCHEMA,
        '<t:root xmlns:t="urn:example" xmlns:p="urn:other">p:a p:b</t:root>',
    )

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "ns0:root": {
            "@xmlns": {
                "ns0": "urn:example",
                HEX_OTHER: "urn:other",
            },
            "$": [f"{HEX_OTHER}:a", f"{HEX_OTHER}:b"],
        }
    }


def test_badgerfish_unqualified_qname_values_need_no_declaration():
    document = _compile(QNAME_ROOT_SCHEMA, "<root>v</root>")

    assert document.to_data(format="badgerfish", fidelity="strict") == {"root": "v"}


@pytest.mark.parametrize("fidelity", ["strict", "best-effort"])
def test_badgerfish_rejects_an_unresolved_qname(fidelity):
    node = _qname_node("p:v")

    with pytest.raises(PyXSDError, match="namespace-unresolved"):
        bound_to_data(node, format="badgerfish", fidelity=fidelity)


def test_badgerfish_xsi_type_uses_a_lexical_type_name():
    document = _compile(XSITYPE_SCHEMA, XSITYPE_INSTANCE)

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "ns0:root": {
            "@xmlns": {"ns0": "urn:example", "xsi": XSI_NAMESPACE},
            "@xsi:type": "ns0:Ext",
            "ns0:a": "x",
            "ns0:b": 2,
        }
    }


def test_badgerfish_nested_xsi_type_uses_the_root_declarations():
    document = _compile(
        NESTED_XSITYPE_SCHEMA,
        '<t:root xmlns:t="urn:example" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<t:value xsi:type="t:Ext"><t:a>x</t:a><t:b>2</t:b></t:value>'
        "</t:root>",
    )

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "ns0:root": {
            "@xmlns": {"ns0": "urn:example", "xsi": XSI_NAMESPACE},
            "ns0:value": {
                "@xsi:type": "ns0:Ext",
                "ns0:a": "x",
                "ns0:b": 2,
            },
        }
    }


def test_badgerfish_wildcard_runtime_names_use_their_actual_names():
    local = _compile(WILDCARD_GENERIC_SCHEMA, "<root><other>v</other></root>")
    assert local.to_data(format="badgerfish", fidelity="strict") == {"root": {"other": "v"}}

    namespaced = _compile(
        WILDCARD_NAMESPACED_SCHEMA,
        '<root xmlns:o="urn:wild"><o:other>v</o:other></root>',
    )
    assert namespaced.to_data(format="badgerfish", fidelity="strict") == {
        "root": {
            "@xmlns": {HEX_WILD: "urn:wild"},
            f"{HEX_WILD}:other": "v",
        }
    }


def test_badgerfish_scalar_families_use_the_fixed_contract():
    assert _compile(IDREFS_ROOT_SCHEMA, "<root>a b</root>").to_data(
        format="badgerfish", fidelity="strict"
    ) == {"root": ["a", "b"]}
    assert _compile(DECIMAL_ROOT_SCHEMA, "<root>1.20</root>").to_data(
        format="badgerfish", fidelity="strict"
    ) == {"root": "1.20"}
    assert _compile(DOUBLE_ROOT_SCHEMA, "<root>1.5</root>").to_data(
        format="badgerfish", fidelity="strict"
    ) == {"root": 1.5}
    assert _compile(DOUBLE_LIST_SCHEMA, "<values><v>INF</v><v>-INF</v><v>NaN</v></values>").to_data(
        format="badgerfish", fidelity="strict"
    ) == {"values": {"v": ["INF", "-INF", "NaN"]}}


def test_badgerfish_rejects_element_only_text():
    document = _compile(SEQUENCE_OPTIONAL_SCHEMA, "<root>oops</root>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="badgerfish")


def test_badgerfish_rejects_element_only_list_text():
    document = _compile(REPEATED_SCHEMA, "<dates>oops<date>2026-01-01</date></dates>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="badgerfish")


def test_badgerfish_rejects_children_in_empty_content():
    document = _compile(
        EMPTY_SCHEMA,
        "<root><x/></root>",
        mode=BindingPolicy(undeclared_content="generic"),
    )

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="badgerfish")


def test_badgerfish_keeps_a_wildcard_attribute_value():
    document = _compile(SIMPLE_CONTENT_WILDCARD_SCHEMA, '<root custom="c">v</root>')

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "root": {"@custom": "c", "$": "v"}
    }


def test_badgerfish_uses_a_raw_attribute_when_no_typed_value_is_available():
    document = _compile(SIMPLE_CONTENT_INT_ATTRIBUTE_SCHEMA, '<root id="oops">v</root>')
    del document.root.__dict__["id"]

    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "root": {"@id": "oops", "$": "v"}
    }


def test_badgerfish_rejects_simple_content_with_children():
    document = _compile(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, "<root>text</root>")
    document.root._children_.append(type("Other", (), {"_name_": "other"})())

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="badgerfish")


def test_badgerfish_rejects_nil_with_content():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root><i>x</i></root>")
    document.root._nil_ = True

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="badgerfish")


def test_badgerfish_export_is_deterministic_and_json_round_trips():
    document = _compile_fixture("recursive")

    assert document.to_data(format="badgerfish") == document.to_data(format="badgerfish")
    assert json.loads(document.to_json(format="badgerfish")) == document.to_data(
        format="badgerfish"
    )


# ---------------------------------------------------------------------------
# JsonML
# ---------------------------------------------------------------------------


def test_jsonml_exports_mixed_content_positionally():
    document = _compile_fixture("mixed")

    assert document.to_data(format="jsonml") == [
        "para",
        {"id": "p1"},
        "This is a ",
        ["i", "fine"],
        " mess!",
    ]
    assert json.loads(document.to_json(format="jsonml")) == document.to_data(format="jsonml")


def test_jsonml_shared_fixtures():
    assert _compile_fixture("simple").to_data(format="jsonml") == ["simple", 42]
    assert _compile_fixture("attribute").to_data(format="jsonml") == [
        "attributed",
        {"id": "a1"},
        "text",
    ]
    assert _compile_fixture("repeated").to_data(format="jsonml") == [
        "dates",
        ["date", "2026-01-01"],
        ["date", "2026-02-01"],
    ]
    assert _compile_fixture("ordered").to_data(format="jsonml") == [
        "section",
        ["head", "Introduction"],
        ["p", "First"],
        ["p", "Second"],
        ["note", "End"],
    ]
    assert _compile_fixture("namespaced").to_data(format="jsonml") == [
        "ns0:root",
        {"xmlns:ns0": "urn:example", "attr": "a"},
        ["ns0:child", "v"],
    ]
    assert _compile_fixture("nillable").to_data(format="jsonml") == ["nillable", None]
    assert _compile_fixture("recursive").to_data(format="jsonml") == [
        "node",
        {"name": "a"},
        ["node", {"name": "b"}, ["node", {"name": "c"}]],
    ]


def test_jsonml_omits_an_empty_attribute_object():
    assert _compile(EMPTY_SCHEMA, "<root/>").to_data(format="jsonml") == ["root"]
    assert _compile(EMPTY_ATTRIBUTE_SCHEMA, "<root/>").to_data(format="jsonml") == ["root"]
    assert _compile(EMPTY_ATTRIBUTE_SCHEMA, '<root id="x"/>').to_data(format="jsonml") == [
        "root",
        {"id": "x"},
    ]


def test_jsonml_children_only_and_text_only_mixed_content():
    children = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root><i>x</i></root>")
    assert children.to_data(format="jsonml") == ["root", ["i", "x"]]

    multiple = _compile(MIXED_TWO_CHILD_SCHEMA, "<root><i>x</i><b>y</b></root>")
    assert multiple.to_data(format="jsonml") == ["root", ["i", "x"], ["b", "y"]]

    text = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root>text</root>")
    assert text.to_data(format="jsonml") == ["root", "text"]


def test_jsonml_distinguishes_empty_simple_and_nil_content():
    assert _compile(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, "<root/>").to_data(format="jsonml") == [
        "root",
        "",
    ]
    assert _compile(EMPTY_SCHEMA, "<root/>").to_data(format="jsonml") == ["root"]
    assert _compile_fixture("nillable").to_data(format="jsonml") == ["nillable", None]


def test_jsonml_nil_keeps_attributes_but_adds_only_null():
    document = _compile(
        SIMPLE_CONTENT_NILABLE_SCHEMA,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true" id="r"/>',
    )

    assert document.to_data(format="jsonml") == ["root", {"id": "r"}, None]


def test_jsonml_namespaced_simple_root_declares_its_prefix():
    document = _compile(
        NAMESPACED_SIMPLE_ROOT_SCHEMA,
        '<t:root xmlns:t="urn:example">x</t:root>',
    )

    assert document.to_data(format="jsonml") == [
        "ns0:root",
        {"xmlns:ns0": "urn:example"},
        "x",
    ]


def test_jsonml_namespaced_qualified_attribute_uses_a_lexical_name():
    document = _compile(
        NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
        '<t:root xmlns:t="urn:example" t:attr="5"><t:child>v</t:child></t:root>',
    )

    assert document.to_data(format="jsonml") == [
        "ns0:root",
        {"xmlns:ns0": "urn:example", "ns0:attr": 5},
        ["ns0:child", "v"],
    ]


def test_jsonml_xml_prefix_is_implicit():
    document = _compile(
        SIMPLE_CONTENT_DECLARED_WILDCARD_SCHEMA,
        '<root xml:lang="en">v</root>',
    )

    assert document.to_data(format="jsonml") == ["root", {"xml:lang": "en"}, "v"]


def test_jsonml_keeps_a_wildcard_attribute_value():
    document = _compile(SIMPLE_CONTENT_WILDCARD_SCHEMA, '<root custom="c">v</root>')

    assert document.to_data(format="jsonml") == ["root", {"custom": "c"}, "v"]


def test_jsonml_uses_a_raw_attribute_when_no_typed_value_is_available():
    document = _compile(SIMPLE_CONTENT_INT_ATTRIBUTE_SCHEMA, '<root id="oops">v</root>')
    del document.root.__dict__["id"]

    assert document.to_data(format="jsonml") == ["root", {"id": "oops"}, "v"]


def test_jsonml_xsi_schema_location_uses_the_xsi_prefix():
    document = _compile(
        SEQUENCE_ATTRIBUTE_SCHEMA,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="urn:x s.xsd"><a>A</a><b>B</b></root>',
    )

    assert document.to_data(format="jsonml") == [
        "root",
        {"xmlns:xsi": XSI_NAMESPACE, "xsi:noNamespaceSchemaLocation": "urn:x s.xsd"},
        ["a", "A"],
        ["b", "B"],
    ]


def test_jsonml_qname_content_allocates_a_declaration():
    document = _compile(QNAME_ROOT_SCHEMA, '<root xmlns:p="urn:example">p:v</root>')

    assert document.to_data(format="jsonml") == [
        "root",
        {"xmlns:" + HEX_EXAMPLE: "urn:example"},
        f"{HEX_EXAMPLE}:v",
    ]


def test_jsonml_qname_attribute_allocates_a_declaration():
    document = _compile(
        QNAME_ATTRIBUTE_SCHEMA,
        '<root xmlns:p="urn:example" ref="p:v"/>',
    )

    assert document.to_data(format="jsonml") == [
        "root",
        {
            "xmlns:" + HEX_EXAMPLE: "urn:example",
            "ref": f"{HEX_EXAMPLE}:v",
        },
    ]


def test_jsonml_qname_lists_are_content_items():
    document = _compile(
        QNAME_LIST_SCHEMA,
        '<t:root xmlns:t="urn:example" xmlns:p="urn:other">p:a p:b</t:root>',
    )

    assert document.to_data(format="jsonml") == [
        "ns0:root",
        {
            "xmlns:ns0": "urn:example",
            "xmlns:" + HEX_OTHER: "urn:other",
        },
        [f"{HEX_OTHER}:a", f"{HEX_OTHER}:b"],
    ]


@pytest.mark.parametrize("fidelity", ["strict", "best-effort"])
def test_jsonml_rejects_an_unresolved_qname(fidelity):
    node = _qname_node("p:v")

    with pytest.raises(PyXSDError, match="namespace-unresolved"):
        bound_to_data(node, format="jsonml", fidelity=fidelity)


def test_jsonml_xsi_type_is_an_attribute():
    document = _compile(XSITYPE_SCHEMA, XSITYPE_INSTANCE)

    data = document.to_data(format="jsonml")

    assert data == [
        "ns0:root",
        {
            "xmlns:ns0": "urn:example",
            "xmlns:xsi": XSI_NAMESPACE,
            "xsi:type": "ns0:Ext",
        },
        ["ns0:a", "x"],
        ["ns0:b", 2],
    ]
    assert list(data[1]) == ["xmlns:ns0", "xmlns:xsi", "xsi:type"]


def test_jsonml_nested_xsi_type_uses_the_root_declarations():
    document = _compile(
        NESTED_XSITYPE_SCHEMA,
        '<t:root xmlns:t="urn:example" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<t:value xsi:type="t:Ext"><t:a>x</t:a><t:b>2</t:b></t:value>'
        "</t:root>",
    )

    assert document.to_data(format="jsonml") == [
        "ns0:root",
        {"xmlns:ns0": "urn:example", "xmlns:xsi": XSI_NAMESPACE},
        [
            "ns0:value",
            {"xsi:type": "ns0:Ext"},
            ["ns0:a", "x"],
            ["ns0:b", 2],
        ],
    ]


def test_jsonml_wildcard_runtime_names_use_their_actual_names():
    local = _compile(WILDCARD_GENERIC_SCHEMA, "<root><other>v</other></root>")
    assert local.to_data(format="jsonml") == ["root", ["other", "v"]]

    namespaced = _compile(
        WILDCARD_NAMESPACED_SCHEMA,
        '<root xmlns:o="urn:wild"><o:other>v</o:other></root>',
    )
    assert namespaced.to_data(format="jsonml") == [
        "root",
        {"xmlns:" + HEX_WILD: "urn:wild"},
        [f"{HEX_WILD}:other", "v"],
    ]


def test_jsonml_scalar_families_use_the_fixed_contract():
    assert _compile(IDREFS_ROOT_SCHEMA, "<root>a b</root>").to_data(format="jsonml") == [
        "root",
        ["a", "b"],
    ]
    assert _compile(DECIMAL_ROOT_SCHEMA, "<root>1.20</root>").to_data(format="jsonml") == [
        "root",
        "1.20",
    ]
    assert _compile(DOUBLE_ROOT_SCHEMA, "<root>1.5</root>").to_data(format="jsonml") == [
        "root",
        1.5,
    ]
    assert _compile(DOUBLE_LIST_SCHEMA, "<values><v>INF</v><v>-INF</v><v>NaN</v></values>").to_data(
        format="jsonml"
    ) == [
        "values",
        ["v", "INF"],
        ["v", "-INF"],
        ["v", "NaN"],
    ]


def test_jsonml_rejects_element_only_text():
    document = _compile(SEQUENCE_OPTIONAL_SCHEMA, "<root>oops</root>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="jsonml")


def test_jsonml_rejects_element_only_list_text():
    document = _compile(REPEATED_SCHEMA, "<dates>oops<date>2026-01-01</date></dates>")

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="jsonml")


def test_jsonml_rejects_children_in_empty_content():
    document = _compile(
        EMPTY_SCHEMA,
        "<root><x/></root>",
        mode=BindingPolicy(undeclared_content="generic"),
    )

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="jsonml")


def test_jsonml_rejects_simple_content_with_children():
    document = _compile(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, "<root>text</root>")
    document.root._children_.append(type("Other", (), {"_name_": "other"})())

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="jsonml")


def test_jsonml_rejects_nil_with_content():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root><i>x</i></root>")
    document.root._nil_ = True

    with pytest.raises(PyXSDError, match="container-invalid"):
        document.to_data(format="jsonml")


def test_jsonml_strict_and_best_effort_are_identical_and_silent():
    document = _compile_fixture("mixed")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        strict = document.to_data(format="jsonml", fidelity="strict")
        best_effort = document.to_data(format="jsonml")

    assert strict == best_effort
    assert _warning_texts(caught) == []


def test_jsonml_export_is_deterministic():
    document = _compile_fixture("recursive")

    assert document.to_data(format="jsonml") == document.to_data(format="jsonml")


# ---------------------------------------------------------------------------
# Native compatibility
# ---------------------------------------------------------------------------


def test_native_to_dict_keeps_raw_typed_values():
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)

    raw = document.to_dict()

    assert raw["item"] == [1, 2]
    assert isinstance(raw["item"][0], Integer)
    assert isinstance(raw["@a"], Integer)
    assert raw["note"] == "hi"


def test_native_to_data_is_json_safe_and_matches_the_raw_shape():
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)

    data = document.to_data()

    assert data == {"@a": 1, "item": [1, 2], "note": "hi"}
    assert type(data["@a"]) is int
    assert type(data["item"][0]) is int
    assert document.to_data(format="pyxsd") == data
    assert json.loads(document.to_json()) == data


def test_native_typed_false_keeps_lexical_values():
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)

    assert document.to_data(typed=False) == {
        "@a": "1",
        "item": ["1", "2"],
        "note": "hi",
    }
    assert json.loads(document.to_json(typed=False)) == document.to_data(typed=False)


def test_native_always_list_wraps_single_children():
    document = _compile(NATIVE_SCHEMA, '<root a="1"><item>1</item><note>hi</note></root>')

    assert document.to_data(always_list=True)["item"] == [1]
    assert json.loads(document.to_json(always_list=True))["item"] == [1]


def test_native_decimal_and_date_values_keep_existing_json_strings():
    decimal_document = _compile(DECIMAL_ROOT_SCHEMA, "<root>1.20</root>")
    assert decimal_document.to_dict()["$"].__class__.__name__ == "Decimal"
    assert decimal_document.to_data() == {"$": "1.20"}
    assert decimal_document.to_json() == '{"$": "1.20"}'

    date_document = _compile_fixture("repeated")
    assert json.loads(date_document.to_json()) == {"date": ["2026-01-01", "2026-02-01"]}


def test_native_non_finite_floats_become_xsd_strings():
    document = _compile(
        DOUBLE_LIST_SCHEMA,
        "<values><v>INF</v><v>-INF</v><v>NaN</v></values>",
    )

    raw = document.to_dict()
    assert math.isinf(raw["v"][0])
    assert math.isnan(raw["v"][2])

    assert document.to_data() == {"v": ["INF", "-INF", "NaN"]}
    text = document.to_json()
    assert "Infinity" not in text
    assert "NaN" in text
    assert json.loads(text) == {"v": ["INF", "-INF", "NaN"]}

    root_document = _compile(DOUBLE_ROOT_SCHEMA, "<root>INF</root>")
    assert root_document.to_data() == {"$": "INF"}
    assert root_document.to_json() == '{"$": "INF"}'


def test_native_list_values_are_arrays_in_typed_mode():
    document = _compile(IDREFS_ROOT_SCHEMA, "<root>a b</root>")

    assert isinstance(document.to_dict()["$"], IDREFS)
    assert document.to_data() == {"$": ["a", "b"]}
    assert document.to_json() == '{"$": ["a", "b"]}'
    assert document.to_data(typed=False) == {"$": "a b"}


def test_pyxsd_json_scalar_families_use_the_fixed_contract():
    assert _compile(IDREFS_ROOT_SCHEMA, "<root>a b</root>").to_data(format="pyxsd-json") == {
        "root": ["a", "b"]
    }
    assert _compile(DECIMAL_ROOT_SCHEMA, "<root>1.20</root>").to_data(format="pyxsd-json") == {
        "root": "1.20"
    }
    assert _compile(DOUBLE_ROOT_SCHEMA, "<root>1.5</root>").to_data(format="pyxsd-json") == {
        "root": 1.5
    }


def test_pyxsd_json_non_finite_values_are_strings():
    document = _compile(
        DOUBLE_LIST_SCHEMA,
        "<values><v>INF</v><v>-INF</v><v>NaN</v></values>",
    )

    assert document.to_data(format="pyxsd-json") == {"values": ["INF", "-INF", "NaN"]}
    assert json.loads(document.to_json(format="pyxsd-json")) == {"values": ["INF", "-INF", "NaN"]}


# ---------------------------------------------------------------------------
# Native strict and best-effort fidelity
# ---------------------------------------------------------------------------


def test_native_best_effort_keeps_the_empty_null_shape_without_warnings():
    document = _compile(EMPTY_OR_NIL_SCHEMA, "<root><empty/></root>")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert document.to_data() == {"empty": None}

    assert caught == []


def test_native_strict_reports_empty_nil_ambiguity():
    document = _compile(EMPTY_OR_NIL_SCHEMA, "<root><empty/></root>")

    with pytest.raises(PyXSDError, match="empty-nil-ambiguous") as caught:
        document.to_data(fidelity="strict")

    assert json_pointer(["empty"]) in str(caught.value)


def test_native_strict_accepts_an_explicit_nil_marker():
    document = _compile(
        NILABLE_EMPTY_SCHEMA,
        '<root><empty xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:nil="true"/></root>',
    )

    assert document.to_data(fidelity="strict") == {"empty": {"@xsi:nil": "true"}}


def test_native_strict_accepts_an_xsi_type_only_empty_element():
    document = _compile(
        EMPTY_XSITYPE_SCHEMA,
        '<t:root xmlns:t="urn:example" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:type="t:EmptyType"/>',
    )

    assert document.to_data(fidelity="strict") == {"@xsi:type": "t:EmptyType"}


def test_native_strict_rejects_a_nil_state_without_its_marker():
    document = _compile(NILABLE_COMPLEX_ATTRIBUTE_SCHEMA, '<root id="x"/>')
    document.root._nil_ = True

    with pytest.raises(PyXSDError, match="empty-nil-ambiguous"):
        document.to_data(fidelity="strict")


def test_native_strict_rejects_mixed_content_positions():
    document = _compile_fixture("mixed")

    with pytest.raises(PyXSDError, match="mixed-content"):
        document.to_data(fidelity="strict")


def test_native_strict_accepts_mixed_children_without_character_data():
    document = _compile(MIXED_NO_ATTRIBUTE_SCHEMA, "<root><i>x</i></root>")

    assert document.to_data(fidelity="strict") == {"i": "x"}


def test_native_best_effort_mixed_content_keeps_the_legacy_shape():
    document = _compile_fixture("mixed")

    assert document.to_data() == {
        "@id": "p1",
        "i": "fine",
        "$": "This is a  mess!",
    }


def test_native_strict_rejects_interleaved_repeated_children():
    document = _compile(
        SEQUENCE_REPEATED_CHOICE_SCHEMA,
        "<root><a>1</a><b>x</b><a>2</a></root>",
    )

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        document.to_data(fidelity="strict")


def test_native_strict_accepts_contiguous_repeats():
    document = _compile_fixture("ordered")

    assert document.to_data(fidelity="strict") == {
        "head": "Introduction",
        "p": ["First", "Second"],
        "note": "End",
    }


def test_native_strict_rejects_namespace_loss_in_legacy_mode():
    document = _compile(
        NAMESPACED_LEGACY_SCHEMA,
        '<t:root xmlns:t="urn:example" attr="5"><t:child>v</t:child></t:root>',
        mode=BindingPolicy(namespaces="legacy"),
    )

    with pytest.raises(PyXSDError, match="namespace-lost"):
        document.to_data(fidelity="strict")


# ---------------------------------------------------------------------------
# Argument and root errors
# ---------------------------------------------------------------------------


def test_bound_to_data_rejects_unknown_formats_and_fidelity():
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)

    with pytest.raises(ValueError, match="unknown data format"):
        bound_to_data(document.root, format="xml")
    with pytest.raises(ValueError, match="unknown fidelity mode"):
        bound_to_data(document.root, fidelity="lossy")


@pytest.mark.parametrize("name", ["parker", "badgerfish", "jsonml"])
def test_bound_to_data_dispatches_every_conventional_format(name):
    document = _compile_fixture("ordered")

    assert bound_to_data(document.root, format=name) == document.to_data(format=name)


@pytest.mark.parametrize("name", ["parker", "badgerfish", "jsonml"])
def test_conventional_formats_reject_native_only_options(name):
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)

    with pytest.raises(ValueError, match="native-only"):
        document.to_data(format=name, typed=False)
    with pytest.raises(ValueError, match="native-only"):
        document.to_data(format=name, always_list=True)
    with pytest.raises(ValueError, match="native-only"):
        document.to_json(format=name, typed=False)


def test_pyxsd_json_rejects_native_only_options():
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)

    with pytest.raises(ValueError, match="native-only"):
        document.to_data(format="pyxsd-json", typed=False)
    with pytest.raises(ValueError, match="native-only"):
        document.to_data(format="pyxsd-json", always_list=True)
    with pytest.raises(ValueError, match="native-only"):
        document.to_json(format="pyxsd-json", typed=False)


def test_export_without_a_bound_root_raises():
    document = pyxsd.Schema.compile(StringIO(NATIVE_SCHEMA)).parse(StringIO("<memo/>"))
    assert document.root is None

    with pytest.raises(PyXSDError, match="no root"):
        document.to_data()
    with pytest.raises(PyXSDError, match="no root"):
        document.to_json()
    with pytest.raises(PyXSDError, match="no root"):
        bound_to_data(None)


def test_bound_to_data_accepts_a_bound_root_directly():
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)

    assert bound_to_data(document.root) == document.to_data()
    assert bound_to_data(document.root, format="pyxsd-json") == document.to_data(
        format="pyxsd-json"
    )


def test_strict_native_argument_checks_run_before_root_checks():
    with pytest.raises(ValueError, match="unknown data format"):
        bound_to_data(None, format="xml")


def test_pyxsd_warning_is_never_emitted_by_best_effort_export():
    document = _compile_fixture("mixed")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        document.to_data(format="pyxsd-json")

    assert not any(isinstance(item.message, PyXSDWarning) for item in caught)


def test_export_module_exposes_the_documented_signature():
    import inspect

    signature = inspect.signature(bound_to_data)
    assert list(signature.parameters) == [
        "root",
        "format",
        "fidelity",
        "typed",
        "always_list",
    ]
    assert signature.parameters["format"].default == "pyxsd"
    assert signature.parameters["fidelity"].default == "best-effort"
    assert signature.parameters["typed"].default is True
    assert signature.parameters["always_list"].default is False


def test_document_to_json_forwards_indentation():
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)

    text = document.to_json(indent=2)

    assert "\n" in text
    assert json.loads(text) == document.to_data()


def test_export_cli_is_not_changed_here():
    # Task 3 adds no CLI surface; ToDict JSON output remains the existing path.
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)
    assert json.loads(document.to_json()) == document.to_dict()
