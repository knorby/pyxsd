"""Tests for native and schema-driven ``pyxsd-json`` document export.

The shared fixtures from ``test_data_formats`` pin every layout, namespace
form, nil state, and scalar family. Native tests keep the raw ``to_dict``
contract distinct from the JSON-safe ``to_data`` contract.
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
from pyxsd.xsd_data_types import IDREFS, Integer

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
def test_bound_to_data_rejects_formats_this_task_does_not_implement(name):
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)

    with pytest.raises(ValueError, match="not implemented"):
        bound_to_data(document.root, format=name)


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
