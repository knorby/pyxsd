"""Tests for Draft 2020-12 JSON Schema generation of every data format.

Every exported fixture must validate against the schema generated for its
root; negative fixtures pin the shapes the generated schema rejects; and
the ``x-pyxsd-validation`` annotations disclose the XSD rules JSON Schema
cannot carry. The schemas are checked with the test-only ``jsonschema``
validator; the Pydantic projection remains a separate contract tested
elsewhere.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import replace
from io import StringIO

import pytest
from jsonschema import Draft202012Validator
from tests.test_data_formats import FIXTURES

import pyxsd
from pyxsd.data_conversion.json_schema import build_json_schema
from pyxsd.exceptions import PyXSDError, PyXSDWarning

#: Each shared fixture's global root name.
FIXTURE_ROOTS = {
    "simple": "simple",
    "attribute": "attributed",
    "repeated": "dates",
    "ordered": "section",
    "mixed": "para",
    "namespaced": "root",
    "nillable": "nillable",
    "recursive": "node",
}

EMPTY_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType/></xs:element>
</xs:schema>"""

EMPTY_NILLABLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true"><xs:complexType/></xs:element>
</xs:schema>"""

EMPTY_PLUS_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:attribute name="id" type="xs:string"/>
  </xs:complexType></xs:element>
</xs:schema>"""

EMPTY_PLUS_NILLABLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true"><xs:complexType>
    <xs:attribute name="id" type="xs:string" use="required"/>
  </xs:complexType></xs:element>
</xs:schema>"""

SIMPLE_PLUS_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:simpleContent>
      <xs:extension base="xs:string">
        <xs:attribute name="id" type="xs:string" use="required"/>
      </xs:extension>
    </xs:simpleContent>
  </xs:complexType></xs:element>
</xs:schema>"""

SIMPLE_PLUS_NILLABLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true"><xs:complexType>
    <xs:simpleContent>
      <xs:extension base="xs:string">
        <xs:attribute name="id" type="xs:string"/>
      </xs:extension>
    </xs:simpleContent>
  </xs:complexType></xs:element>
</xs:schema>"""

LIST_BOUNDS_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:sequence>
      <xs:element name="c" type="xs:int" minOccurs="2" maxOccurs="3"/>
    </xs:sequence>
  </xs:complexType></xs:element>
</xs:schema>"""

LIST_PLUS_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:sequence>
      <xs:element name="c" type="xs:int" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="a" type="xs:string"/>
  </xs:complexType></xs:element>
</xs:schema>"""

LIST_PLUS_NILLABLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true"><xs:complexType>
    <xs:sequence>
      <xs:element name="c" type="xs:int" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="a" type="xs:string"/>
  </xs:complexType></xs:element>
</xs:schema>"""

RECORD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:all>
      <xs:element name="req" type="xs:int"/>
      <xs:element name="opt" type="xs:string" minOccurs="0"/>
    </xs:all>
    <xs:attribute name="id" type="xs:string"/>
  </xs:complexType></xs:element>
</xs:schema>"""

RECORD_NILLABLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true"><xs:complexType>
    <xs:all>
      <xs:element name="req" type="xs:int"/>
    </xs:all>
    <xs:attribute name="id" type="xs:string"/>
  </xs:complexType></xs:element>
</xs:schema>"""

SEQUENCE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:sequence>
      <xs:element name="head" type="xs:string"/>
      <xs:element name="p" type="xs:string" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string"/>
  </xs:complexType></xs:element>
</xs:schema>"""

BOOLEAN_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:boolean"/>
</xs:schema>"""

INTEGER_BOUNDS_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="bounded"/>
  <xs:simpleType name="bounded">
    <xs:restriction base="xs:int">
      <xs:minInclusive value="2"/>
      <xs:maxExclusive value="10"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>"""

DECIMAL_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:decimal"/>
</xs:schema>"""

DOUBLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:double"/>
</xs:schema>"""

QNAME_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:t="urn:example"
            targetNamespace="urn:example">
  <xs:element name="root" type="xs:QName"/>
</xs:schema>"""

XSD_LIST_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:simpleType><xs:list itemType="xs:int"/></xs:simpleType>
  </xs:element>
</xs:schema>"""

ENUM_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="coded"/>
  <xs:simpleType name="coded">
    <xs:restriction base="xs:string">
      <xs:enumeration value="a"/>
      <xs:enumeration value="b"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>"""

SAFE_PATTERN_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="coded"/>
  <xs:simpleType name="coded">
    <xs:restriction base="xs:string">
      <xs:pattern value="[abc]+"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>"""

UNSAFE_PATTERN_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="coded"/>
  <xs:simpleType name="coded">
    <xs:restriction base="xs:string">
      <xs:pattern value="\\w+"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>"""

LENGTH_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="sized"/>
  <xs:simpleType name="sized">
    <xs:restriction base="xs:string">
      <xs:minLength value="2"/>
      <xs:maxLength value="4"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>"""

FRACTION_DIGITS_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="money"/>
  <xs:simpleType name="money">
    <xs:restriction base="xs:decimal">
      <xs:fractionDigits value="2"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>"""

UNION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="ut"/>
  <xs:simpleType name="ut">
    <xs:union memberTypes="xs:int xs:string"/>
  </xs:simpleType>
</xs:schema>"""

MULTIPLE_ROOTS_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="alpha" type="xs:int"/>
  <xs:element name="beta" type="xs:string"/>
</xs:schema>"""

NO_GLOBAL_ROOTS_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="unused">
    <xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>
  </xs:complexType>
</xs:schema>"""

SUBSTITUTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="head" type="xs:string"/>
  <xs:element name="member" type="xs:int" substitutionGroup="head"/>
  <xs:element name="wrap"><xs:complexType>
    <xs:sequence>
      <xs:element ref="head" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType></xs:element>
</xs:schema>"""

ABSTRACT_HEAD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="head" type="xs:string" abstract="true"/>
  <xs:element name="member" type="xs:int" substitutionGroup="head"/>
</xs:schema>"""

WILDCARD_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:simpleContent>
      <xs:extension base="xs:string">
        <xs:anyAttribute processContents="lax"/>
      </xs:extension>
    </xs:simpleContent>
  </xs:complexType></xs:element>
</xs:schema>"""

WILDCARD_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:sequence>
      <xs:element name="c" type="xs:string"/>
      <xs:any namespace="##other" processContents="lax" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType></xs:element>
</xs:schema>"""

NESTED_XSITYPE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="Base">
    <xs:sequence>
      <xs:element name="a" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="Ext">
    <xs:complexContent>
      <xs:extension base="Base">
        <xs:sequence>
          <xs:element name="b" type="xs:int"/>
        </xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="root"><xs:complexType>
    <xs:sequence>
      <xs:element name="value" type="Base"/>
    </xs:sequence>
  </xs:complexType></xs:element>
</xs:schema>"""

XSITYPE_SIMPLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:string"/>
  <xs:simpleType name="code">
    <xs:restriction base="xs:string">
      <xs:enumeration value="a"/>
      <xs:enumeration value="b"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>"""

CTA_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="st">
    <xs:alternative test="@k='i'" type="it"/>
  </xs:element>
  <xs:complexType name="st">
    <xs:simpleContent><xs:extension base="xs:string">
      <xs:attribute name="k" type="xs:string"/>
    </xs:extension></xs:simpleContent>
  </xs:complexType>
  <xs:complexType name="it">
    <xs:simpleContent><xs:extension base="xs:int">
      <xs:attribute name="k" type="xs:string"/>
    </xs:extension></xs:simpleContent>
  </xs:complexType>
</xs:schema>"""

IDENTITY_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>
    </xs:complexType>
    <xs:key name="k"><xs:selector xpath="c"/><xs:field xpath="."/></xs:key>
  </xs:element>
</xs:schema>"""

ASSERTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>
      <xs:assert test="true()"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ANONYMOUS_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="w" type="t1"/>
    <xs:element name="v" type="t2"/>
  </xs:sequence></xs:complexType></xs:element>
  <xs:complexType name="t1"><xs:sequence>
    <xs:element name="c"><xs:complexType><xs:sequence>
      <xs:element name="a" type="xs:int"/>
    </xs:sequence></xs:complexType></xs:element>
  </xs:sequence></xs:complexType>
  <xs:complexType name="t2"><xs:sequence>
    <xs:element name="c"><xs:complexType><xs:sequence>
      <xs:element name="b" type="xs:string"/>
    </xs:sequence></xs:complexType></xs:element>
  </xs:sequence></xs:complexType>
</xs:schema>"""

COLLIDING_NAMES_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="one" type="a-b"/>
  <xs:element name="two" type="a_b"/>
  <xs:simpleType name="a-b">
    <xs:restriction base="xs:int"/>
  </xs:simpleType>
  <xs:simpleType name="a_b">
    <xs:restriction base="xs:string"/>
  </xs:simpleType>
</xs:schema>"""

AMBIGUOUS_LOCAL_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
            xmlns:a="urn:a" xmlns:b="urn:b">
  <xs:import namespace="urn:a"/>
  <xs:import namespace="urn:b"/>
</xs:schema>"""


def _compile(schema_source: str) -> pyxsd.Schema:
    return pyxsd.Schema.compile(StringIO(schema_source))


def _compiled(name: str) -> tuple[pyxsd.Schema, object]:
    schema_source, instance_source = FIXTURES[name]
    schema = _compile(schema_source)
    return schema, schema.parse(StringIO(instance_source))


def _generated(schema: pyxsd.Schema, element: str | None = None) -> dict:
    generated = schema.json_schema(format="pyxsd-json", element=element)
    Draft202012Validator.check_schema(generated)
    return generated


def _errors(schema: pyxsd.Schema, element: str | None, data: object) -> list:
    return list(Draft202012Validator(_generated(schema, element)).iter_errors(data))


def _export(schema: pyxsd.Schema, instance: str) -> object:
    return schema.parse(StringIO(instance)).to_data(format="pyxsd-json")


def _unvalidated(schema: pyxsd.Schema, element: str | None = None) -> list:
    return _generated(schema, element)["x-pyxsd-validation"]["unvalidated"]


# ---------------------------------------------------------------------------
# Envelope and shared fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_every_shared_fixture_export_validates(name):
    schema, document = _compiled(name)

    exported = document.to_data(format="pyxsd-json")
    generated = _generated(schema, FIXTURE_ROOTS[name])

    assert list(Draft202012Validator(generated).iter_errors(exported)) == []


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_shared_fixture_envelope(name):
    schema, _document = _compiled(name)

    generated = _generated(schema, FIXTURE_ROOTS[name])

    assert generated["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert generated["x-pyxsd-format"] == "pyxsd-json"
    assert generated["x-pyxsd-profile-version"] == 1
    assert generated["x-pyxsd-validation"]["level"] == "structural"
    assert generated["x-pyxsd-validation"]["unvalidated"] == sorted(
        generated["x-pyxsd-validation"]["unvalidated"]
    )


def test_a_fully_describable_schema_has_no_unvalidated_rules():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:all><xs:element name="a" type="xs:boolean"/></xs:all>
  </xs:complexType></xs:element>
</xs:schema>""")

    assert _unvalidated(schema) == []


def test_generation_is_deterministic():
    schema, _document = _compiled("recursive")

    first = schema.json_schema(format="pyxsd-json", element="node")
    second = schema.json_schema(format="pyxsd-json", element="node")

    assert first == second


def test_the_builder_function_and_the_schema_method_agree():
    schema, _document = _compiled("mixed")

    assert build_json_schema(schema, element="para") == schema.json_schema(
        format="pyxsd-json", element="para"
    )


def test_a_fresh_compile_generates_the_same_schema():
    source, _instance = FIXTURES["namespaced"]

    first = _compile(source).json_schema(format="pyxsd-json", element="root")
    second = _compile(source).json_schema(format="pyxsd-json", element="root")

    assert first == second


# ---------------------------------------------------------------------------
# Options and formats
# ---------------------------------------------------------------------------


def test_hybrid_rejects_the_native_only_options():
    schema = _compile(SIMPLE_PLUS_SCHEMA)

    with pytest.raises(ValueError, match="native-only"):
        schema.json_schema(format="pyxsd-json", element="root", typed=False)
    with pytest.raises(ValueError, match="native-only"):
        schema.json_schema(format="pyxsd-json", element="root", always_list=True)


def test_unknown_format_and_fidelity_raise_value_error():
    schema = _compile(SIMPLE_PLUS_SCHEMA)

    with pytest.raises(ValueError, match="unknown data format"):
        schema.json_schema(format="nope", element="root")
    with pytest.raises(ValueError, match="unknown fidelity"):
        schema.json_schema(format="pyxsd-json", element="root", fidelity="loose")


def test_conventional_formats_reject_native_only_options():
    schema = _compile(SIMPLE_PLUS_SCHEMA)

    for format in ("pyxsd-json", "parker", "badgerfish", "jsonml"):
        with pytest.raises(ValueError, match="native-only"):
            schema.json_schema(format=format, element="root", typed=False)
        with pytest.raises(ValueError, match="native-only"):
            schema.json_schema(format=format, element="root", always_list=True)


@pytest.mark.parametrize("format", ("pyxsd", "parker", "badgerfish", "jsonml"))
def test_every_format_fidelity_modes_generate_the_same_schema(format):
    schema = _compile(RECORD_SCHEMA)

    strict = schema.json_schema(format=format, element="root", fidelity="strict")
    best_effort = schema.json_schema(format=format, element="root", fidelity="best-effort")

    assert strict == best_effort


def test_hybrid_fidelity_modes_generate_the_same_schema():
    schema = _compile(RECORD_SCHEMA)

    strict = schema.json_schema(format="pyxsd-json", element="root", fidelity="strict")
    best_effort = schema.json_schema(format="pyxsd-json", element="root", fidelity="best-effort")

    assert strict == best_effort


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------


def test_empty_layout_is_the_empty_string():
    schema = _compile(EMPTY_SCHEMA)

    assert not _errors(schema, "root", {"root": ""})
    assert _errors(schema, "root", {"root": "text"})
    assert _errors(schema, "root", {"root": {}})


def test_empty_nillable_layout_accepts_null():
    schema = _compile(EMPTY_NILLABLE_SCHEMA)

    assert not _errors(schema, "root", {"root": None})
    assert not _errors(schema, "root", {"root": ""})


def test_empty_plus_accepts_an_attribute_only_object():
    schema = _compile(EMPTY_PLUS_SCHEMA)

    assert not _errors(schema, "root", {"root": {}})
    assert not _errors(schema, "root", {"root": {"@id": "x"}})
    assert _errors(schema, "root", {"root": {"@unknown": "x"}})
    assert _errors(schema, "root", {"root": ""})


def test_empty_plus_nil_uses_the_content_marker():
    schema = _compile(EMPTY_PLUS_NILLABLE_SCHEMA)

    assert not _errors(schema, "root", {"root": {"@id": "x", "#content": None}})
    assert _errors(schema, "root", {"root": {"#content": None}})


def test_simple_plus_requires_content():
    schema = _compile(SIMPLE_PLUS_SCHEMA)

    assert not _errors(schema, "root", {"root": {"@id": "x", "#content": "text"}})
    assert _errors(schema, "root", {"root": {"@id": "x"}})
    assert _errors(schema, "root", {"root": {"@id": "x", "#content": None}})


def test_simple_plus_nil_replaces_content_with_null():
    schema = _compile(SIMPLE_PLUS_NILLABLE_SCHEMA)

    assert not _errors(schema, "root", {"root": {"#content": None}})
    assert not _errors(schema, "root", {"root": {"@id": "x", "#content": "v"}})


def test_list_layout_applies_occurrence_bounds():
    schema = _compile(LIST_BOUNDS_SCHEMA)

    assert not _errors(schema, "root", {"root": [1, 2]})
    assert not _errors(schema, "root", {"root": [1, 2, 3]})
    assert _errors(schema, "root", {"root": [1]})
    assert _errors(schema, "root", {"root": [1, 2, 3, 4]})
    assert _errors(schema, "root", {"root": {"c": [1, 2]}})


def test_list_plus_always_carries_the_child_array():
    schema = _compile(LIST_PLUS_SCHEMA)

    assert not _errors(schema, "root", {"root": {"c": []}})
    assert not _errors(schema, "root", {"root": {"@a": "x", "c": [1, 2]}})
    assert _errors(schema, "root", {"root": {"@a": "x"}})
    assert _errors(schema, "root", {"root": {"c": 1}})
    assert _errors(schema, "root", {"root": [1]})


def test_list_plus_nil_omits_the_child_array():
    schema = _compile(LIST_PLUS_NILLABLE_SCHEMA)

    assert not _errors(schema, "root", {"root": {"@a": "x", "#content": None}})
    assert _errors(schema, "root", {"root": {"@a": "x", "#content": None, "c": []}})


def test_record_properties_and_requirements():
    schema = _compile(RECORD_SCHEMA)

    assert not _errors(schema, "root", {"root": {"req": 1}})
    assert not _errors(schema, "root", {"root": {"@id": "x", "req": 1, "opt": "v"}})
    assert _errors(schema, "root", {"root": {"opt": "v"}})
    assert _errors(schema, "root", {"root": {"req": 1, "unknown": "v"}})
    assert _errors(schema, "root", {"root": {"req": "one"}})


def test_record_nil_uses_the_content_marker():
    schema = _compile(RECORD_NILLABLE_SCHEMA)

    assert not _errors(schema, "root", {"root": {"@id": "x", "#content": None}})
    assert _errors(schema, "root", {"root": {"#content": None, "req": 1}})


def test_sequence_allows_attribute_maps_and_child_maps():
    schema = _compile(SEQUENCE_SCHEMA)

    exported = _export(schema, '<root id="i"><head>h</head><p>a</p><p>b</p></root>')
    assert not _errors(schema, "root", exported)
    assert not _errors(schema, "root", {"root": [{"head": "h"}, {"p": "a"}, {"p": "b"}]})
    assert _errors(schema, "root", {"root": [{"head": "h"}, {"unknown": "x"}]})
    assert _errors(schema, "root", {"root": {"head": "h"}})
    assert _errors(schema, "root", {"root": [{"@id": 1}]})


def test_sequence_discloses_particle_order():
    schema = _compile(SEQUENCE_SCHEMA)

    assert _unvalidated(schema) == ["xsd-particle-order"]


def test_mixed_allows_interleaved_text_strings():
    schema, document = _compiled("mixed")

    exported = document.to_data(format="pyxsd-json")
    assert not _errors(schema, "para", exported)
    assert not _errors(schema, "para", {"para": ["a ", {"i": "b"}, " c"]})
    assert _errors(schema, "para", {"para": [{"i": "b"}, 5]})


def test_mixed_discloses_mixed_order():
    schema, _document = _compiled("mixed")

    assert _unvalidated(schema, "para") == ["xsd-mixed-order"]


def test_xsi_schema_location_attribute_is_accepted():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:attribute name="a" type="xs:string"/>
  </xs:complexType></xs:element>
</xs:schema>""")

    exported = _export(
        schema,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:schemaLocation="urn:x x.xsd" a="v"/>',
    )

    assert not _errors(schema, "root", exported)


# ---------------------------------------------------------------------------
# Scalar families
# ---------------------------------------------------------------------------


def test_boolean_scalar():
    schema = _compile(BOOLEAN_SCHEMA)

    assert not _errors(schema, "root", {"root": True})
    assert _errors(schema, "root", {"root": "true"})
    assert _errors(schema, "root", {"root": 1})


def test_integer_scalar_with_bounds():
    schema = _compile(INTEGER_BOUNDS_SCHEMA)

    assert not _errors(schema, "root", {"root": 2})
    assert not _errors(schema, "root", {"root": 9})
    assert _errors(schema, "root", {"root": 1})
    assert _errors(schema, "root", {"root": 10})
    assert _errors(schema, "root", {"root": "2"})


def test_decimal_is_a_fixed_point_string():
    schema = _compile(DECIMAL_SCHEMA)

    assert not _errors(schema, "root", {"root": "1.20"})
    assert not _errors(schema, "root", {"root": "-0"})
    assert _errors(schema, "root", {"root": 1.2})
    assert _errors(schema, "root", {"root": "1.2E3"})
    assert _errors(schema, "root", {"root": ".5"})


def test_double_accepts_finite_numbers_and_non_finite_markers():
    schema = _compile(DOUBLE_SCHEMA)

    assert not _errors(schema, "root", {"root": 1.5})
    assert not _errors(schema, "root", {"root": "INF"})
    assert not _errors(schema, "root", {"root": "-INF"})
    assert not _errors(schema, "root", {"root": "NaN"})
    assert _errors(schema, "root", {"root": "inf"})
    assert _errors(schema, "root", {"root": True})


def test_qname_is_an_eqname_string():
    schema = _compile(QNAME_SCHEMA)

    exported = _export(schema, '<root xmlns="urn:example" xmlns:t="urn:example">t:local</root>')
    assert exported == {"Q{urn:example}root": "Q{urn:example}local"}
    assert not _errors(schema, None, exported)
    assert _errors(schema, None, {"Q{urn:example}root": "t:local"})


def test_xsd_list_is_an_array_of_typed_items():
    schema = _compile(XSD_LIST_SCHEMA)

    assert not _errors(schema, "root", {"root": [1, 2, 3]})
    assert not _errors(schema, "root", {"root": []})
    assert _errors(schema, "root", {"root": "1 2"})
    assert _errors(schema, "root", {"root": [1, "2"]})


def test_enumeration_restriction_becomes_an_enum():
    schema = _compile(ENUM_SCHEMA)

    assert not _errors(schema, "root", {"root": "a"})
    assert not _errors(schema, "root", {"root": "b"})
    assert _errors(schema, "root", {"root": "c"})


def test_ecmascript_safe_patterns_are_emitted():
    schema = _compile(SAFE_PATTERN_SCHEMA)

    assert not _errors(schema, "root", {"root": "cab"})
    assert _errors(schema, "root", {"root": "d"})
    assert _unvalidated(schema) == []


def test_unsafe_patterns_are_disclosed_instead_of_translated():
    schema = _compile(UNSAFE_PATTERN_SCHEMA)

    assert not _errors(schema, "root", {"root": "anything"})
    assert _unvalidated(schema) == ["xsd-regex-dialect"]


def test_digit_shorthand_patterns_are_disclosed():
    # XSD ``\d`` is Unicode Nd; ECMAScript ``\d`` is ASCII-only, so the
    # shorthand is not a tested equivalent even though Python accepts it.
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="coded"/>
  <xs:simpleType name="coded">
    <xs:restriction base="xs:string">
      <xs:pattern value="\\d{2}"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": "34"})
    # Disclosed, not emitted: a string the shorthand would reject must
    # pass, proving no tested-equivalent pattern was installed.
    assert not _errors(schema, "r", {"r": "ab"})
    assert _unvalidated(schema) == ["xsd-regex-dialect"]


def test_complement_shorthand_patterns_are_disclosed():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="coded"/>
  <xs:simpleType name="coded">
    <xs:restriction base="xs:string">
      <xs:pattern value="\\D{2}"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": "ab"})
    assert _unvalidated(schema) == ["xsd-regex-dialect"]


def test_length_facets_are_emitted():
    schema = _compile(LENGTH_SCHEMA)

    assert not _errors(schema, "root", {"root": "ab"})
    assert not _errors(schema, "root", {"root": "abcd"})
    assert _errors(schema, "root", {"root": "a"})
    assert _errors(schema, "root", {"root": "abcde"})


def test_inexpressible_numeric_facets_are_disclosed():
    schema = _compile(FRACTION_DIGITS_SCHEMA)

    assert not _errors(schema, "root", {"root": "1.234"})
    assert _unvalidated(schema) == ["xsd-facet"]


def test_union_values_validate_as_their_members():
    schema = _compile(UNION_SCHEMA)

    assert not _errors(schema, "root", {"root": "42"})
    assert not _errors(schema, "root", {"root": "hello"})
    assert not _errors(schema, "root", {"root": 42})
    document = schema.parse(StringIO("<root>42</root>"))
    assert document.to_data(format="pyxsd-json") == {"root": 42}
    restored = schema.from_data(document.to_data(format="pyxsd-json"))
    restored.require_valid()
    assert isinstance(restored.root.memberValue, int)
    assert restored.root.memberValue == 42


# ---------------------------------------------------------------------------
# Root alternatives and selection
# ---------------------------------------------------------------------------


def test_multiple_globals_use_anyof_roots():
    schema = _compile(MULTIPLE_ROOTS_SCHEMA)

    assert not _errors(schema, None, {"alpha": 1})
    assert not _errors(schema, None, {"beta": "x"})
    assert _errors(schema, None, {"alpha": "one"})
    assert _errors(schema, None, {"gamma": 1})
    assert _errors(schema, None, {"alpha": 1, "beta": "x"})


def test_selected_root_keeps_only_its_alternatives():
    schema = _compile(MULTIPLE_ROOTS_SCHEMA)

    assert not _errors(schema, "alpha", {"alpha": 1})
    assert _errors(schema, "alpha", {"beta": "x"})


def test_clark_and_eqname_root_selection():
    schema = _compile(QNAME_SCHEMA)

    assert not _errors(schema, "{urn:example}root", {"Q{urn:example}root": "Q{urn:example}x"})
    assert not _errors(schema, "Q{urn:example}root", {"Q{urn:example}root": "Q{urn:example}x"})


def test_an_ambiguous_local_root_selection_raises(tmp_path):
    (tmp_path / "b.xsd").write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
            targetNamespace="urn:b">
  <xs:element name="root" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
            targetNamespace="urn:a">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:element name="root" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    schema = pyxsd.Schema.compile(str(main))

    with pytest.raises(PyXSDError, match="name-ambiguous"):
        schema.json_schema(format="pyxsd-json", element="root")
    assert not _errors(schema, "Q{urn:a}root", {"Q{urn:a}root": "x"})


def test_an_invalid_local_root_selection_raises():
    schema = _compile(SIMPLE_PLUS_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.json_schema(format="pyxsd-json", element="1bad")


def test_zero_global_roots_reject_every_document():
    schema = _compile(NO_GLOBAL_ROOTS_SCHEMA)

    generated = _generated(schema, None)
    assert generated["not"] == {}
    assert _errors(schema, None, {"anything": 1})
    assert _errors(schema, None, [1])


def test_an_unknown_element_selection_uses_the_general_shape():
    schema = _compile(SIMPLE_PLUS_SCHEMA)

    assert not _errors(schema, "missing", {"missing": ["text"]})
    assert not _errors(schema, "missing", {"missing": [{"@a": "x"}]})
    assert _errors(schema, "missing", {"missing": 5})


def test_substitution_member_roots_are_valid_alternatives():
    schema = _compile(SUBSTITUTION_SCHEMA)

    assert not _errors(schema, "head", {"head": "h"})
    assert not _errors(schema, "head", {"member": 7})


def test_an_abstract_head_lists_only_its_members():
    schema = _compile(ABSTRACT_HEAD_SCHEMA)

    generated = _generated(schema, None)
    assert not _errors(schema, None, {"member": 7})
    assert _errors(schema, None, {"head": "h"})
    assert list(generated["properties"]) == ["member"]


# ---------------------------------------------------------------------------
# Substitutions, wildcards, and xsi:type
# ---------------------------------------------------------------------------


def test_substitution_children_validate_with_their_own_names():
    schema = _compile(SUBSTITUTION_SCHEMA)

    exported = _export(schema, "<wrap><head>a</head><member>3</member></wrap>")

    assert not _errors(schema, "wrap", exported)
    assert _errors(schema, "wrap", {"wrap": [{"head": "a"}, {"member": "three"}]})


def test_wildcard_attributes_allow_additional_attribute_keys():
    schema = _compile(WILDCARD_ATTRIBUTE_SCHEMA)

    assert not _errors(schema, "root", {"root": {"@custom": "c", "#content": "v"}})
    assert not _errors(schema, "root", {"root": {"#content": "v"}})
    assert _errors(schema, "root", {"root": {"@custom": 5, "#content": "v"}})
    assert _unvalidated(schema) == ["xsd-wildcard-namespace"]


def test_wildcard_children_accept_generic_singleton_maps():
    schema = _compile(WILDCARD_CHILD_SCHEMA)

    assert not _errors(schema, "root", {"root": [{"c": "x"}]})
    assert not _errors(schema, "root", {"root": [{"c": "x"}, {"Q{urn:other}w": ["t"]}]})
    assert _errors(schema, "root", {"root": [{"c": "x", "extra": 1}]})
    assert _errors(schema, "root", {"root": [[{"c": "x"}]]})
    assert _unvalidated(schema) == ["xsd-particle-order", "xsd-wildcard-namespace"]


def test_xsi_type_uses_the_actual_type_layout_with_a_marker():
    schema = _compile(NESTED_XSITYPE_SCHEMA)

    declared = _export(schema, "<root><value><a>x</a></value></root>")
    extended = _export(
        schema,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<value xsi:type="Ext"><a>x</a><b>2</b></value></root>',
    )

    assert not _errors(schema, "root", declared)
    assert not _errors(schema, "root", extended)
    assert extended["root"][0]["value"][0] == {"@xsi:type": "Ext"}
    assert _errors(schema, "root", {"root": [{"value": [{"@xsi:type": "Unknown"}, {"a": "x"}]}]})
    assert _errors(schema, "root", {"root": [{"value": {"@xsi:type": "Ext"}}]})


def test_xsi_type_on_a_simple_root_selects_simple_plus():
    schema = _compile(XSITYPE_SIMPLE_SCHEMA)

    exported = _export(
        schema,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="code">a</root>',
    )

    assert exported == {"root": {"@xsi:type": "code", "#content": "a"}}
    assert not _errors(schema, "root", exported)
    assert not _errors(schema, "root", {"root": "plain"})
    assert _errors(schema, "root", {"root": {"@xsi:type": "unknown", "#content": "a"}})


def test_conditional_type_alternatives_validate_without_a_marker():
    schema = _compile(CTA_SCHEMA)

    as_int = _export(schema, '<root k="i">42</root>')
    as_string = _export(schema, '<root k="s">text</root>')

    assert as_int == {"root": {"@k": "i", "#content": 42}}
    assert not _errors(schema, "root", as_int)
    assert not _errors(schema, "root", as_string)
    assert _errors(schema, "root", {"root": {"@k": "i"}})
    assert "xsd-conditional-type" in _unvalidated(schema)


# ---------------------------------------------------------------------------
# Definitions, recursion, and annotations
# ---------------------------------------------------------------------------


def test_recursive_types_use_defs_and_refs():
    schema, document = _compiled("recursive")

    generated = _generated(schema, "node")
    exported = document.to_data(format="pyxsd-json")

    assert "$defs" in generated
    assert not _errors(schema, "node", exported)
    deeper = schema.parse(
        StringIO('<node name="a"><node name="b"><node name="c"/></node></node>')
    ).to_data(format="pyxsd-json")
    assert not _errors(schema, "node", deeper)


def test_anonymous_types_get_their_own_definitions():
    schema = _compile(ANONYMOUS_SCHEMA)

    exported = _export(schema, "<r><w><c><a>1</a></c></w><v><c><b>x</b></c></v></r>")
    generated = _generated(schema, "r")

    assert not _errors(schema, "r", exported)
    assert len(generated["$defs"]) >= 5
    assert _errors(schema, "r", {"r": [{"w": [{"c": [{"a": "one"}]}]}]})


def test_colliding_type_names_get_unique_definition_keys():
    schema = _compile(COLLIDING_NAMES_SCHEMA)

    generated = _generated(schema, None)

    defs = generated["$defs"]
    assert len(defs) == 2
    assert len(set(defs)) == 2
    annotations = sorted(value.get("x-pyxsd-qname") for value in defs.values())
    assert annotations == ["a-b", "a_b"]
    assert not _errors(schema, None, {"one": 1})
    assert not _errors(schema, None, {"two": "x"})


def test_identity_constraints_are_disclosed():
    schema = _compile(IDENTITY_SCHEMA)

    assert "xsd-identity-constraints" in _unvalidated(schema)


def test_assertions_are_disclosed():
    schema = _compile(ASSERTION_SCHEMA)

    assert "xsd-assertions" in _unvalidated(schema)


def test_definitions_carry_type_and_layout_annotations():
    schema = _compile(RECORD_SCHEMA)

    generated = _generated(schema, "root")
    body = next(iter(generated["$defs"].values()))

    assert body["x-pyxsd-layout"] == "record"
    assert "x-pyxsd-qname" in body


# ---------------------------------------------------------------------------
# Undetermined declarations and general shapes
# ---------------------------------------------------------------------------


def test_untyped_roots_use_the_general_shape():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r"/>
</xs:schema>""")

    exported = _export(schema, "<r>hello <b>bold</b> tail</r>")
    assert not _errors(schema, "r", exported)
    assert not _errors(schema, "r", {"r": ["hello ", {"b": ["bold"]}, " tail"]})
    assert not _errors(schema, "r", {"r": {"@xsi:type": "Q{urn:x}t", "#content": "v"}})
    assert _errors(schema, "r", {"r": 5})
    assert _errors(schema, "r", {"r": ["text", 5]})


def test_untyped_nillable_roots_accept_the_nil_marker_item():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" nillable="true"/>
</xs:schema>""")

    exported = _export(
        schema,
        '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>',
    )
    assert exported == {"r": [{"#content": None}]}
    assert not _errors(schema, "r", exported)


def test_clark_selection_of_an_unknown_root_uses_the_general_shape():
    schema = _compile(SIMPLE_PLUS_SCHEMA)

    assert not _errors(schema, "{urn:x}missing", {"Q{urn:x}missing": ["text"]})
    assert _errors(schema, "{urn:x}missing", {"Q{urn:x}missing": 5})


# ---------------------------------------------------------------------------
# Nil in ordered layouts
# ---------------------------------------------------------------------------


def test_nillable_sequence_accepts_the_nil_marker_item():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" nillable="true"><xs:complexType>
    <xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>
  </xs:complexType></xs:element>
</xs:schema>""")

    exported = _export(
        schema,
        '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>',
    )
    assert exported == {"r": [{"#content": None}]}
    assert not _errors(schema, "r", exported)
    assert _errors(schema, "r", {"r": [{"#content": "x"}]})


def test_non_nillable_sequence_rejects_the_nil_marker_item():
    schema = _compile(SEQUENCE_SCHEMA)

    assert _errors(schema, "root", {"root": [{"id": "i"}, {"#content": None}]})


# ---------------------------------------------------------------------------
# Attribute declaration states
# ---------------------------------------------------------------------------


def test_prohibited_attributes_are_excluded_and_closed():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="derived"/>
  <xs:complexType name="base">
    <xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>
    <xs:attribute name="a" type="xs:int"/>
    <xs:attribute name="b" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="derived">
    <xs:complexContent><xs:restriction base="base">
      <xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>
      <xs:attribute name="a" use="prohibited"/>
      <xs:attribute name="b" type="xs:string"/>
    </xs:restriction></xs:complexContent>
  </xs:complexType>
</xs:schema>""")

    exported = _export(schema, '<r b="x"><c>v</c></r>')
    assert exported == {"r": [{"@b": "x"}, {"c": "v"}]}
    assert not _errors(schema, "r", exported)
    assert _errors(schema, "r", {"r": [{"@a": 1}, {"c": "v"}]})


# ---------------------------------------------------------------------------
# Union members and list scalars
# ---------------------------------------------------------------------------


def test_union_members_use_boolean_and_expanded_qname_contracts():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="ut"/>
  <xs:simpleType name="ut">
    <xs:union memberTypes="xs:boolean xs:QName"/>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": "true"})
    assert not _errors(schema, "r", {"r": "false"})
    assert not _errors(schema, "r", {"r": "Q{urn:value}local"})
    assert _errors(schema, "r", {"r": "pfx:local"})
    assert not _errors(schema, "r", {"r": True})
    assert _errors(schema, "r", {"r": 42})


def test_builtin_list_types_are_string_arrays():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="xs:NMTOKENS"/>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": ["a", "b"]})
    assert not _errors(schema, "r", {"r": []})
    assert _errors(schema, "r", {"r": "a b"})
    assert _errors(schema, "r", {"r": [1]})


def test_list_length_facets_apply_to_the_array():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="sized"/>
  <xs:simpleType name="sized">
    <xs:restriction>
      <xs:simpleType><xs:list itemType="xs:string"/></xs:simpleType>
      <xs:length value="2"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": ["a", "b"]})
    assert _errors(schema, "r", {"r": ["a"]})
    assert _errors(schema, "r", {"r": ["a", "b", "c"]})


def test_list_pattern_facets_are_disclosed():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="coded"/>
  <xs:simpleType name="coded">
    <xs:restriction>
      <xs:simpleType><xs:list itemType="xs:string"/></xs:simpleType>
      <xs:pattern value="\\w+"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": ["a", "b"]})
    assert _unvalidated(schema) == ["xsd-facet"]


# ---------------------------------------------------------------------------
# Facet expressibility
# ---------------------------------------------------------------------------


def test_integer_enumerations_become_enums():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="sized"/>
  <xs:simpleType name="sized">
    <xs:restriction base="xs:int">
      <xs:enumeration value="1"/>
      <xs:enumeration value="2"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": 1})
    assert not _errors(schema, "r", {"r": 2})
    assert _errors(schema, "r", {"r": 3})
    assert _errors(schema, "r", {"r": "1"})
    assert _unvalidated(schema) == []


def test_integer_patterns_are_disclosed():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="coded"/>
  <xs:simpleType name="coded">
    <xs:restriction base="xs:int">
      <xs:pattern value="[0-9]+"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": 42})
    assert _unvalidated(schema) == ["xsd-facet"]


def test_decimal_whitespace_restrictions_stay_clean():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="money"/>
  <xs:simpleType name="money">
    <xs:restriction base="xs:decimal">
      <xs:whiteSpace value="collapse"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": "1.20"})
    assert _unvalidated(schema) == []


def test_double_bounds_apply_to_the_number_branch():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="big"/>
  <xs:simpleType name="big">
    <xs:restriction base="xs:double">
      <xs:minInclusive value="0.5"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": 0.5})
    assert not _errors(schema, "r", {"r": "INF"})
    assert _errors(schema, "r", {"r": 0.4})


def test_double_patterns_are_disclosed():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="coded"/>
  <xs:simpleType name="coded">
    <xs:restriction base="xs:double">
      <xs:pattern value="[0-9]+\\.[0-9]"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": 1.5})
    assert _unvalidated(schema) == ["xsd-facet"]


def test_non_finite_bounds_are_disclosed():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="big"/>
  <xs:simpleType name="big">
    <xs:restriction base="xs:double">
      <xs:minInclusive value="INF"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": 1.5})
    assert not _errors(schema, "r", {"r": "INF"})
    assert _unvalidated(schema) == ["xsd-facet"]


def test_exact_length_facet_sets_both_bounds():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="sized"/>
  <xs:simpleType name="sized">
    <xs:restriction base="xs:string">
      <xs:length value="3"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": "abc"})
    assert _errors(schema, "r", {"r": "ab"})
    assert _errors(schema, "r", {"r": "abcd"})


def test_qname_enumerations_are_disclosed():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="qt"/>
  <xs:simpleType name="qt">
    <xs:restriction base="xs:QName">
      <xs:enumeration value="a:b"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert _unvalidated(schema) == ["xsd-facet"]


def test_boolean_enumerations_become_exact_enums():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="flag"/>
  <xs:simpleType name="flag">
    <xs:restriction base="xs:boolean">
      <xs:enumeration value="false"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    exported = _export(schema, "<r>0</r>")
    assert exported == {"r": False}
    assert not _errors(schema, "r", exported)
    assert _errors(schema, "r", {"r": True})
    assert _unvalidated(schema) == []


def test_boolean_patterns_are_disclosed():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="flag"/>
  <xs:simpleType name="flag">
    <xs:restriction base="xs:boolean">
      <xs:pattern value="true|false"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": True})
    assert _errors(schema, "r", {"r": "true"})
    assert _unvalidated(schema) == ["xsd-facet"]


def test_decimal_enumerations_use_fixed_point_spellings():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="money"/>
  <xs:simpleType name="money">
    <xs:restriction base="xs:decimal">
      <xs:enumeration value="1.20"/>
      <xs:enumeration value="2.50"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    exported = _export(schema, "<r>1.20</r>")
    assert exported == {"r": "1.20"}
    assert not _errors(schema, "r", exported)
    assert not _errors(schema, "r", {"r": "2.50"})
    assert _errors(schema, "r", {"r": "1.2"})
    assert _errors(schema, "r", {"r": "3.00"})
    assert _unvalidated(schema) == []


def test_integer_enumerations_with_patterns_keep_both():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="coded"/>
  <xs:simpleType name="coded">
    <xs:restriction base="xs:int">
      <xs:enumeration value="1"/>
      <xs:enumeration value="2"/>
      <xs:pattern value="[0-9]+"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": 1})
    assert _errors(schema, "r", {"r": 3})
    assert _errors(schema, "r", {"r": "1"})
    assert _unvalidated(schema) == ["xsd-facet"]


def test_out_of_family_enumeration_values_are_disclosed():
    # The facet parser types enumeration values with the restriction's
    # base, so a value outside the family cannot occur through a schema.
    # Swapping one in pins the fallback: disclose rather than emit.
    boolean_schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="flag"/>
  <xs:simpleType name="flag">
    <xs:restriction base="xs:boolean">
      <xs:enumeration value="false"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")
    integer_schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="sized"/>
  <xs:simpleType name="sized">
    <xs:restriction base="xs:int">
      <xs:enumeration value="1"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")
    decimal_schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="money"/>
  <xs:simpleType name="money">
    <xs:restriction base="xs:decimal">
      <xs:enumeration value="1.20"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")
    for schema, type_name in (
        (boolean_schema, "flag"),
        (integer_schema, "sized"),
        (decimal_schema, "money"),
    ):
        type_class = schema.classes[type_name]
        type_class._facetConstraints_ = replace(
            type_class._facetConstraints_, enumeration_values=(object(),)
        )
        assert _unvalidated(schema) == ["xsd-facet"], type_name


def test_multiple_pattern_steps_are_disclosed():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="sub"/>
  <xs:simpleType name="base">
    <xs:restriction base="xs:string">
      <xs:pattern value="x+"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="sub">
    <xs:restriction base="base">
      <xs:pattern value="y?"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": "x"})
    assert _unvalidated(schema) == ["xsd-regex-dialect"]


# ---------------------------------------------------------------------------
# Conditional alternatives
# ---------------------------------------------------------------------------


def test_an_alternative_naming_the_declared_type_adds_no_branch():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="st">
    <xs:alternative test="@k='x'" type="st"/>
  </xs:element>
  <xs:complexType name="st">
    <xs:simpleContent><xs:extension base="xs:string">
      <xs:attribute name="k" type="xs:string"/>
    </xs:extension></xs:simpleContent>
  </xs:complexType>
</xs:schema>""")

    generated = _generated(schema, "r")

    assert "anyOf" not in generated
    assert not _errors(schema, "r", {"r": {"@k": "x", "#content": "t"}})
    assert not _errors(schema, "r", {"r": {"#content": "t"}})


def test_an_unresolved_alternative_type_is_skipped():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="xs:string">
    <xs:alternative test="@k='x'" type="missing:type"/>
  </xs:element>
</xs:schema>""")

    assert not _errors(schema, "r", {"r": "text"})


# ---------------------------------------------------------------------------
# Translation-shape contracts
# ---------------------------------------------------------------------------


def test_pattern_translation_requires_the_anchored_shape():
    from types import SimpleNamespace

    from pyxsd.data_conversion.json_schema import _pattern_keyword

    plain = SimpleNamespace(patterns=((re.compile("plain"),),))
    assert _pattern_keyword(plain) is None
    no_steps = SimpleNamespace(patterns=())
    assert _pattern_keyword(no_steps) is None
    empty_step = SimpleNamespace(patterns=((),))
    assert _pattern_keyword(empty_step) is None


def test_an_element_without_a_model_stays_open():
    from pyxsd.data_conversion.json_schema import _SchemaBuilder

    class Bare:
        pass

    assert _SchemaBuilder._element_wildcard(Bare) is True


# ---------------------------------------------------------------------------
# Native, Parker, BadgerFish, and JsonML projections
# ---------------------------------------------------------------------------

NATIVE_OPTIONS: tuple[dict[str, object], ...] = (
    {},
    {"typed": False},
    {"always_list": True},
    {"typed": False, "always_list": True},
)

CONVENTIONAL_FORMATS = ("pyxsd", "parker", "badgerfish", "jsonml")

NATIVE_REPEAT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="c" type="xs:int" minOccurs="0" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

NATIVE_LIST_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r">
    <xs:simpleType><xs:list itemType="xs:int"/></xs:simpleType>
  </xs:element>
</xs:schema>"""

EMPTY_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="e"><xs:complexType/></xs:element>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

NILLABLE_EMPTY_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="e" nillable="true"><xs:complexType/></xs:element>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

NILLABLE_EMPTY_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" nillable="true"><xs:complexType/></xs:element>
</xs:schema>"""

SIMPLE_NILLABLE_LEAF_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="xs:string" nillable="true"/>
</xs:schema>"""

LEXICAL_QNAME_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="xs:QName"/>
</xs:schema>"""

NATIVE_REQUIRED_ATTR_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:simpleContent>
    <xs:extension base="xs:string">
      <xs:attribute name="id" type="xs:string" use="required"/>
    </xs:extension>
  </xs:simpleContent></xs:complexType></xs:element>
</xs:schema>"""

MIXED_NO_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r"><xs:complexType mixed="true"/></xs:element>
</xs:schema>"""


def _export_quiet(document: pyxsd.Document, fmt: str, **options: object) -> object:
    """Export a fixture, silencing the exporter's documented loss warnings."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PyXSDWarning)
        return document.to_data(format=fmt, **options)  # type: ignore[arg-type]


def _format_generated(
    schema: pyxsd.Schema, fmt: str, element: str | None = None, **options: object
) -> dict:
    generated = schema.json_schema(format=fmt, element=element, **options)  # type: ignore[arg-type]
    Draft202012Validator.check_schema(generated)
    return generated


def _format_errors(
    schema: pyxsd.Schema, fmt: str, element: str | None, data: object, **options: object
) -> list:
    return list(
        Draft202012Validator(_format_generated(schema, fmt, element, **options)).iter_errors(data)
    )


def _format_unvalidated(
    schema: pyxsd.Schema, fmt: str, element: str | None = None, **options: object
) -> list:
    return _format_generated(schema, fmt, element, **options)["x-pyxsd-validation"]["unvalidated"]


# ---------------------------------------------------------------------------
# Conventional fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("options", NATIVE_OPTIONS)
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_native_fixture_exports_validate(name, options):
    schema, document = _compiled(name)

    exported = _export_quiet(document, "pyxsd", **options)
    generated = _format_generated(schema, "pyxsd", FIXTURE_ROOTS[name], **options)

    assert list(Draft202012Validator(generated).iter_errors(exported)) == []


@pytest.mark.parametrize("fmt", ("parker", "badgerfish", "jsonml"))
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_conventional_fixture_exports_validate(name, fmt):
    schema, document = _compiled(name)

    exported = _export_quiet(document, fmt)
    generated = _format_generated(schema, fmt, FIXTURE_ROOTS[name])

    assert list(Draft202012Validator(generated).iter_errors(exported)) == []


@pytest.mark.parametrize("fmt", CONVENTIONAL_FORMATS)
def test_conventional_envelope(fmt):
    schema = _compile(MULTIPLE_ROOTS_SCHEMA if fmt in ("badgerfish", "jsonml") else EMPTY_SCHEMA)
    element = None if fmt in ("badgerfish", "jsonml") else "root"

    generated = _format_generated(schema, fmt, element)

    assert generated["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert generated["x-pyxsd-format"] == fmt
    assert "x-pyxsd-profile-version" not in generated
    assert generated["x-pyxsd-validation"]["level"] == "structural"
    assert generated["x-pyxsd-validation"]["unvalidated"] == sorted(
        generated["x-pyxsd-validation"]["unvalidated"]
    )


@pytest.mark.parametrize("fmt", CONVENTIONAL_FORMATS)
def test_conventional_generation_is_deterministic(fmt):
    source, _instance = FIXTURES["recursive"]

    first = _compile(source).json_schema(format=fmt, element="node")
    second = _compile(source).json_schema(format=fmt, element="node")

    assert first == second


@pytest.mark.parametrize("fmt", CONVENTIONAL_FORMATS)
def test_a_fresh_conventional_compile_generates_the_same_schema(fmt):
    source, _instance = FIXTURES["recursive"]

    assert _compile(source).json_schema(format=fmt, element="node") == _compile(source).json_schema(
        format=fmt, element="node"
    )


@pytest.mark.parametrize("fmt", CONVENTIONAL_FORMATS)
def test_conventional_formats_reject_invalid_selections(fmt):
    schema = _compile(SIMPLE_PLUS_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.json_schema(format=fmt, element="1bad")


@pytest.mark.parametrize("fmt", ("pyxsd", "parker"))
def test_absorbed_formats_require_one_root_selection(fmt):
    schema = _compile(MULTIPLE_ROOTS_SCHEMA)

    with pytest.raises(PyXSDError, match="root-ambiguous"):
        schema.json_schema(format=fmt)
    if fmt == "pyxsd":
        assert not _format_errors(schema, fmt, "alpha", {"$": 1})
    else:
        assert not _format_errors(schema, fmt, "alpha", 1)


def test_absorbed_formats_reject_a_schema_without_global_roots():
    schema = _compile(NO_GLOBAL_ROOTS_SCHEMA)

    with pytest.raises(PyXSDError, match="root-ambiguous"):
        schema.json_schema(format="pyxsd")
    with pytest.raises(PyXSDError, match="root-ambiguous"):
        schema.json_schema(format="parker")


@pytest.mark.parametrize("fmt", ("badgerfish", "jsonml"))
def test_root_carrying_formats_describe_every_global_root(fmt):
    schema = _compile(MULTIPLE_ROOTS_SCHEMA)

    generated = _format_generated(schema, fmt, None)

    assert "anyOf" in generated


@pytest.mark.parametrize("fmt", ("badgerfish", "jsonml"))
def test_root_carrying_formats_reject_a_schema_without_global_roots(fmt):
    schema = _compile(NO_GLOBAL_ROOTS_SCHEMA)

    generated = _format_generated(schema, fmt, None)

    assert generated["not"] == {}


@pytest.mark.parametrize("fmt", CONVENTIONAL_FORMATS)
def test_unknown_selections_use_the_general_shape(fmt):
    schema = _compile(SIMPLE_PLUS_SCHEMA)

    if fmt == "pyxsd":
        assert not _format_errors(schema, fmt, "missing", {"$": "text"})
        assert not _format_errors(schema, fmt, "missing", "text")
        assert _format_errors(schema, fmt, "missing", 5)
    elif fmt == "parker":
        assert not _format_errors(schema, fmt, "missing", "text")
        assert _format_errors(schema, fmt, "missing", 5)
    elif fmt == "badgerfish":
        assert not _format_errors(schema, fmt, "missing", {"anything": {"a": 1}})
        assert _format_errors(schema, fmt, "missing", 5)
    else:
        assert not _format_errors(schema, fmt, "missing", ["anything", "text"])
        assert _format_errors(schema, fmt, "missing", "not-an-array")


@pytest.mark.parametrize("fmt", CONVENTIONAL_FORMATS)
def test_substitution_member_selections_are_valid_alternatives(fmt):
    schema = _compile(SUBSTITUTION_SCHEMA)

    for element, data in (
        ("head", "h"),
        ("member", 7),
    ):
        if fmt == "pyxsd":
            expected = {"$": data} if not isinstance(data, int) else data
        elif fmt == "parker":
            expected = data
        elif fmt == "badgerfish":
            expected = {element: data}
        else:
            expected = [element, data]
        assert not _format_errors(schema, fmt, element, expected)


# ---------------------------------------------------------------------------
# Native format specifics
# ---------------------------------------------------------------------------


def test_native_root_absorbs_and_wraps_primitive_roots():
    schema, document = _compiled("simple")

    exported = _export_quiet(document, "pyxsd")

    assert exported == {"$": 42}
    assert not _format_errors(schema, "pyxsd", "simple", {"$": 42})
    assert _format_errors(schema, "pyxsd", "simple", {"simple": 42})
    assert _format_errors(schema, "pyxsd", "simple", {"$": None})
    assert _format_errors(schema, "pyxsd", "simple", {"$": True})


def test_native_simple_content_in_an_object_keeps_the_raw_text():
    schema = _compile(NATIVE_REQUIRED_ATTR_SCHEMA)

    exported = _export_quiet(
        _compile(SIMPLE_PLUS_SCHEMA).parse(StringIO('<root id="x">42</root>')), "pyxsd"
    )

    assert exported == {"@id": "x", "$": "42"}
    assert not _format_errors(schema, "pyxsd", "root", {"@id": "x", "$": "42"})
    assert _format_errors(schema, "pyxsd", "root", {"@id": "x", "$": 42})
    assert _format_errors(schema, "pyxsd", "root", {"$": "42"})


def test_native_typed_false_uses_lexical_strings():
    schema = _compile(NATIVE_LIST_ROOT_SCHEMA)

    assert not _format_errors(schema, "pyxsd", "r", {"$": "1 2 3"}, typed=False)
    assert _format_errors(schema, "pyxsd", "r", {"$": [1, 2, 3]}, typed=False)
    assert _format_errors(schema, "pyxsd", "r", {"$": 42}, typed=False)
    assert not _format_errors(schema, "pyxsd", "r", {"$": [1, 2, 3]})


def test_native_repeatable_children_allow_one_value_or_an_array():
    schema = _compile(NATIVE_REPEAT_SCHEMA)

    assert not _format_errors(schema, "pyxsd", "root", {"c": 1})
    assert not _format_errors(schema, "pyxsd", "root", {"c": [1, 2]})
    assert _format_errors(schema, "pyxsd", "root", {"c": "one"})
    assert _format_errors(schema, "pyxsd", "root", {"c": [1, "two"]})


def test_native_always_list_wraps_every_child():
    schema = _compile(NATIVE_REPEAT_SCHEMA)

    assert not _format_errors(schema, "pyxsd", "root", {"c": [1]}, always_list=True)
    assert not _format_errors(schema, "pyxsd", "root", {"c": [1, 2]}, always_list=True)
    assert _format_errors(schema, "pyxsd", "root", {"c": 1}, always_list=True)


def test_native_null_covers_empty_and_nil_states():
    non_nillable = _compile(EMPTY_CHILD_SCHEMA)
    nillable = _compile(NILLABLE_EMPTY_CHILD_SCHEMA)

    assert not _format_errors(non_nillable, "pyxsd", "root", {"e": None})
    assert "native-null-ambiguity" not in _format_unvalidated(non_nillable, "pyxsd", "root")

    assert not _format_errors(nillable, "pyxsd", "root", {"e": None})
    assert "native-null-ambiguity" in _format_unvalidated(nillable, "pyxsd", "root")

    # A nilled element that keeps its marker exports the attribute object.
    document = nillable.parse(
        StringIO(
            '<root><e xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/></root>'
        )
    )
    assert _export_quiet(document, "pyxsd") == {"e": {"@xsi:nil": "true"}}
    assert not _format_errors(nillable, "pyxsd", "root", {"e": {"@xsi:nil": "true"}})
    assert _format_errors(nillable, "pyxsd", "root", {"e": {"@xsi:nil": True}})
    assert _format_errors(nillable, "pyxsd", "root", {"e": {"@xsi:nil": "false"}})


def test_native_nillable_simple_leaves_allow_null_with_the_annotation():
    schema = _compile(SIMPLE_NILLABLE_LEAF_SCHEMA)

    assert not _format_errors(schema, "pyxsd", "r", {"$": "text"})
    assert not _format_errors(schema, "pyxsd", "r", {"$": None})
    assert "native-null-ambiguity" in _format_unvalidated(schema, "pyxsd", "r")


def test_native_typed_false_keeps_null_for_nil_only():
    schema = _compile(SIMPLE_NILLABLE_LEAF_SCHEMA)

    assert not _format_errors(schema, "pyxsd", "r", {"$": ""}, typed=False)
    assert not _format_errors(schema, "pyxsd", "r", {"$": None}, typed=False)
    assert "native-null-ambiguity" not in _format_unvalidated(schema, "pyxsd", "r", typed=False)


def test_native_xsi_type_marker_and_target_content():
    schema = _compile(NESTED_XSITYPE_SCHEMA)

    exported = _export_quiet(
        schema.parse(
            StringIO(
                '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                '<value xsi:type="Ext"><a>x</a><b>2</b></value></root>'
            )
        ),
        "pyxsd",
    )

    assert exported == {"value": {"@xsi:type": "Ext", "a": "x", "b": 2}}
    assert not _format_errors(schema, "pyxsd", "root", exported)
    assert _format_errors(schema, "pyxsd", "root", {"value": {"@xsi:type": "Ext", "zzz": 1}})
    assert _format_errors(
        schema, "pyxsd", "root", {"value": {"@xsi:type": "Ext", "a": "x", "b": "two"}}
    )


def test_native_clark_child_names():
    schema, document = _compiled("namespaced")

    exported = _export_quiet(document, "pyxsd")

    assert exported == {"@attr": "a", "{urn:example}child": "v"}
    assert not _format_errors(schema, "pyxsd", "root", exported)
    assert _format_errors(schema, "pyxsd", "root", {"@attr": "a", "child": "v"})


def test_native_mixed_text_and_order_disclosures():
    schema = _compile(MULTIPLE_ROOTS_SCHEMA)
    ordered, _document = _compiled("ordered")
    mixed, _mixed_document = _compiled("mixed")

    assert _format_unvalidated(schema, "pyxsd", "alpha") == []
    assert "xsd-particle-order" in _format_unvalidated(ordered, "pyxsd", "section")
    assert "xsd-mixed-order" in _format_unvalidated(mixed, "pyxsd", "para")


def test_native_wildcard_attributes_stay_open():
    schema = _compile(WILDCARD_ATTRIBUTE_SCHEMA)

    assert not _format_errors(schema, "pyxsd", "root", {"@custom": "c", "$": "v"})
    assert _format_errors(schema, "pyxsd", "root", {"@custom": 5, "$": "v"})
    assert "xsd-wildcard-namespace" in _format_unvalidated(schema, "pyxsd", "root")


def test_native_wildcard_children_stay_open():
    schema = _compile(WILDCARD_CHILD_SCHEMA)

    assert not _format_errors(schema, "pyxsd", "root", {"c": "x", "{urn:other}w": "t"})
    assert "xsd-wildcard-namespace" in _format_unvalidated(schema, "pyxsd", "root")


def test_native_qname_scalars_are_lexical_strings():
    schema = _compile(LEXICAL_QNAME_SCHEMA)

    generated = _format_generated(schema, "pyxsd", "r")

    assert not _format_errors(schema, "pyxsd", "r", {"$": "t:local"})
    assert not _format_errors(schema, "pyxsd", "r", {"$": "local"})
    assert _format_errors(schema, "pyxsd", "r", {"$": 5})
    wrap = next(branch for branch in generated["anyOf"] if branch.get("properties"))
    scalar = wrap["properties"]["$"]
    assert scalar["x-pyxsd-type"] == "QName"
    assert scalar["x-pyxsd-namespace-context"] is True


def test_native_conditional_alternative_types_contribute_leaf_scalars():
    schema = _compile(CTA_SCHEMA)

    exported_int = _export_quiet(schema.parse(StringIO('<root k="i">42</root>')), "pyxsd")

    assert exported_int == {"@k": "i", "$": "42"}
    assert not _format_errors(schema, "pyxsd", "root", exported_int)
    # Both branches can export bare leaves because ``k`` is optional, so
    # the leaf union carries each alternative's scalar.
    assert not _format_errors(schema, "pyxsd", "root", {"$": 42})
    assert not _format_errors(schema, "pyxsd", "root", {"$": "text"})
    assert _format_errors(schema, "pyxsd", "root", {"$": True})
    assert _format_errors(schema, "pyxsd", "root", {"@k": "i", "$": 42})
    assert "xsd-conditional-type" in _format_unvalidated(schema, "pyxsd", "root")


def test_native_recursive_types_use_defs_and_refs():
    schema, document = _compiled("recursive")

    generated = _format_generated(schema, "pyxsd", "node")
    exported = _export_quiet(document, "pyxsd")

    assert "$defs" in generated
    assert not _format_errors(schema, "pyxsd", "node", exported)
    deeper = schema.parse(StringIO('<node name="a"><node name="b"><node name="c"/></node></node>'))
    assert not _format_errors(schema, "pyxsd", "node", _export_quiet(deeper, "pyxsd"))


def test_native_identity_and_assertion_disclosures():
    assert "xsd-identity-constraints" in _format_unvalidated(
        _compile(IDENTITY_SCHEMA), "pyxsd", "root"
    )
    assert "xsd-assertions" in _format_unvalidated(_compile(ASSERTION_SCHEMA), "pyxsd", "root")


def test_native_undetermined_root_uses_the_general_shape():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r"/>
</xs:schema>""")

    exported = _export_quiet(schema.parse(StringIO("<r>hello <b>bold</b> tail</r>")), "pyxsd")

    assert not _format_errors(schema, "pyxsd", "r", exported)
    assert not _format_errors(schema, "pyxsd", "r", {"$": None})
    assert not _format_errors(schema, "pyxsd", "r", {"$": "text"})
    assert _format_errors(schema, "pyxsd", "r", 5)
    assert "xsd-wildcard-namespace" in _format_unvalidated(schema, "pyxsd", "r")


# ---------------------------------------------------------------------------
# Parker specifics
# ---------------------------------------------------------------------------


def test_parker_absorbs_the_root_and_keeps_scalars_bare():
    schema, document = _compiled("simple")

    exported = _export_quiet(document, "parker")

    assert exported == 42
    assert not _format_errors(schema, "parker", "simple", 42)
    assert _format_errors(schema, "parker", "simple", {"$": 42})
    assert _format_errors(schema, "parker", "simple", "42")


def test_parker_empty_and_nil_stay_distinct():
    empty = _compile(EMPTY_CHILD_SCHEMA)
    nillable = _compile(NILLABLE_EMPTY_CHILD_SCHEMA)
    nillable_root = _compile(NILLABLE_EMPTY_ROOT_SCHEMA)
    simple_nillable = _compile(SIMPLE_NILLABLE_LEAF_SCHEMA)

    assert not _format_errors(empty, "parker", "root", {"e": {}})
    assert _format_errors(empty, "parker", "root", {"e": None})

    assert not _format_errors(nillable, "parker", "root", {"e": {}})
    assert not _format_errors(nillable, "parker", "root", {"e": None})
    # An empty element-only root stays {} where nil is null.
    assert not _format_errors(nillable_root, "parker", "r", {})
    assert not _format_errors(nillable_root, "parker", "r", None)
    # Simple content never becomes {}; nil is null alone.
    assert not _format_errors(simple_nillable, "parker", "r", None)
    assert _format_errors(simple_nillable, "parker", "r", {})


def test_parker_drops_attributes():
    schema = _compile(SIMPLE_PLUS_SCHEMA)

    assert _format_errors(schema, "parker", "root", {"@id": "x", "$": "text"})
    assert _format_errors(schema, "parker", "root", {"#content": "text"})


def test_parker_local_names_and_actual_repeat_arrays():
    schema = _compile(NATIVE_REPEAT_SCHEMA)
    namespaced, _document = _compiled("namespaced")

    assert not _format_errors(schema, "parker", "root", {"c": 1})
    assert not _format_errors(schema, "parker", "root", {"c": [1, 2]})
    assert _format_errors(schema, "parker", "root", {"c": [1, "two"]})
    exported = _export_quiet(
        namespaced.parse(StringIO('<root xmlns="urn:example" attr="a"><child>v</child></root>')),
        "parker",
    )
    assert exported == {"child": "v"}
    assert not _format_errors(namespaced, "parker", "root", exported)


def test_parker_grouped_content_discloses_order():
    ordered, _document = _compiled("ordered")
    mixed, _document = _compiled("mixed")

    assert "xsd-particle-order" in _format_unvalidated(ordered, "parker", "section")
    assert "xsd-mixed-order" in _format_unvalidated(mixed, "parker", "para")


def test_parker_qname_namespace_loss_is_disclosed():
    schema = _compile(LEXICAL_QNAME_SCHEMA)

    generated = _format_generated(schema, "parker", "r")

    assert not _format_errors(schema, "parker", "r", "t:local")
    assert _format_errors(schema, "parker", "r", "Q{urn:x}local")
    assert "namespace-lost" in generated["x-pyxsd-validation"]["unvalidated"]


def test_parker_conditional_types_contribute_scalars():
    schema = _compile(CTA_SCHEMA)

    assert not _format_errors(schema, "parker", "root", 42)
    assert not _format_errors(schema, "parker", "root", "text")
    assert "xsd-conditional-type" in _format_unvalidated(schema, "parker", "root")


def test_parker_wildcard_children_stay_open():
    schema = _compile(WILDCARD_CHILD_SCHEMA)

    assert not _format_errors(schema, "parker", "root", {"c": "x"})
    assert not _format_errors(schema, "parker", "root", {"c": "x", "w": "t"})
    assert "xsd-wildcard-namespace" in _format_unvalidated(schema, "parker", "root")


def test_parker_colliding_local_names_stay_valid(tmp_path):
    (tmp_path / "a.xsd").write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
            targetNamespace="urn:a">
  <xs:element name="c" type="xs:int"/>
</xs:schema>""",
        encoding="utf-8",
    )
    (tmp_path / "b.xsd").write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
            targetNamespace="urn:b">
  <xs:element name="c" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
            xmlns:a="urn:a" xmlns:b="urn:b">
  <xs:import namespace="urn:a" schemaLocation="a.xsd"/>
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:element name="root"><xs:complexType><xs:choice>
    <xs:element ref="a:c"/>
    <xs:element ref="b:c"/>
  </xs:choice></xs:complexType></xs:element>
</xs:schema>""",
        encoding="utf-8",
    )
    schema = pyxsd.Schema.compile(str(main))

    exported = _export_quiet(
        schema.parse(StringIO('<root xmlns:a="urn:a"><a:c>5</a:c></root>')), "parker"
    )

    assert exported == {"c": 5}
    assert not _format_errors(schema, "parker", "root", exported)
    assert not _format_errors(schema, "parker", "root", {"c": "text"})
    assert _format_errors(schema, "parker", "root", {"c": True})


def test_parker_recursive_types_use_defs_and_refs():
    schema, document = _compiled("recursive")

    generated = _format_generated(schema, "parker", "node")

    assert "$defs" in generated
    assert not _format_errors(schema, "parker", "node", _export_quiet(document, "parker"))


# ---------------------------------------------------------------------------
# BadgerFish specifics
# ---------------------------------------------------------------------------


def test_badgerfish_root_object_and_lexical_names():
    schema, document = _compiled("namespaced")

    exported = _export_quiet(document, "badgerfish")

    assert exported == {
        "ns0:root": {"@xmlns": {"ns0": "urn:example"}, "@attr": "a", "ns0:child": "v"}
    }
    assert not _format_errors(schema, "badgerfish", "root", exported)
    assert _format_errors(schema, "badgerfish", "root", {"root": exported["ns0:root"]})
    assert _format_errors(
        schema, "badgerfish", "root", {"ns0:root": {**exported["ns0:root"], "@xmlns": {"ns0": 5}}}
    )


def test_badgerfish_bare_scalar_roots_and_the_dollar_wrap():
    simple, simple_document = _compiled("simple")
    namespaced = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example" targetNamespace="urn:example">
  <xs:element name="r" type="xs:int"/>
</xs:schema>""")

    assert _export_quiet(simple_document, "badgerfish") == {"simple": 42}
    assert not _format_errors(simple, "badgerfish", "simple", {"simple": 42})
    # The wrapped "$" keeps the typed scalar, never its lexical spelling.
    assert _format_errors(simple, "badgerfish", "simple", {"simple": {"$": "42"}})

    exported = _export_quiet(
        namespaced.parse(StringIO('<r xmlns="urn:example">7</r>')), "badgerfish"
    )
    assert exported == {"ns0:r": {"@xmlns": {"ns0": "urn:example"}, "$": 7}}
    assert not _format_errors(namespaced, "badgerfish", "r", exported)


def test_badgerfish_nil_marker_is_a_boolean():
    schema, document = _compiled("nillable")

    exported = _export_quiet(document, "badgerfish")

    assert exported == {
        "nillable": {
            "@xmlns": {"xsi": "http://www.w3.org/2001/XMLSchema-instance"},
            "@xsi:nil": True,
        }
    }
    assert not _format_errors(schema, "badgerfish", "nillable", exported)
    assert _format_errors(
        schema,
        "badgerfish",
        "nillable",
        {
            "nillable": {
                "@xsi:nil": "true",
                "@xmlns": {"xsi": "http://www.w3.org/2001/XMLSchema-instance"},
            }
        },
    )
    non_nillable = _compile(SIMPLE_PLUS_SCHEMA)
    assert _format_errors(non_nillable, "badgerfish", "root", {"root": {"@xsi:nil": True}})


def test_badgerfish_schema_repeatable_children_are_arrays():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="many" type="xs:int" minOccurs="0" maxOccurs="unbounded"/>
    <xs:element name="one" type="xs:int" minOccurs="0"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>""")

    exported = _export_quiet(
        schema.parse(StringIO("<root><many>1</many><one>2</one></root>")), "badgerfish"
    )

    assert exported == {"root": {"many": [1], "one": 2}}
    assert not _format_errors(schema, "badgerfish", "root", exported)
    assert _format_errors(schema, "badgerfish", "root", {"root": {"many": 1, "one": 2}})
    assert _format_errors(schema, "badgerfish", "root", {"root": {"many": [1], "one": [2]}})


def test_badgerfish_typed_dollar_and_attributes():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:simpleContent>
    <xs:extension base="xs:int">
      <xs:attribute name="a" type="xs:string" use="required"/>
    </xs:extension>
  </xs:simpleContent></xs:complexType></xs:element>
</xs:schema>""")

    exported = _export_quiet(schema.parse(StringIO('<root a="x">42</root>')), "badgerfish")

    assert exported == {"root": {"@a": "x", "$": 42}}
    assert not _format_errors(schema, "badgerfish", "root", exported)
    assert _format_errors(schema, "badgerfish", "root", {"root": {"@a": "x", "$": "42"}})
    assert _format_errors(schema, "badgerfish", "root", {"root": {"$": 42}})


def test_badgerfish_xsi_type_marker_and_target_content():
    schema = _compile(NESTED_XSITYPE_SCHEMA)

    exported = _export_quiet(
        schema.parse(
            StringIO(
                '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                '<value xsi:type="Ext"><a>x</a><b>2</b></value></root>'
            )
        ),
        "badgerfish",
    )

    assert exported == {
        "root": {
            "@xmlns": {"xsi": "http://www.w3.org/2001/XMLSchema-instance"},
            "value": {"@xsi:type": "Ext", "a": "x", "b": 2},
        }
    }
    assert not _format_errors(schema, "badgerfish", "root", exported)
    assert _format_errors(
        schema, "badgerfish", "root", {"root": {"value": {"@xsi:type": "Ext", "zzz": 1}}}
    )


def test_badgerfish_grouped_content_discloses_order():
    ordered, _document = _compiled("ordered")

    assert "xsd-particle-order" in _format_unvalidated(ordered, "badgerfish", "section")
    assert "xsd-wildcard-namespace" in _format_unvalidated(
        _compile(WILDCARD_CHILD_SCHEMA), "badgerfish", "root"
    )


def test_badgerfish_recursive_types_use_defs_and_refs():
    schema, document = _compiled("recursive")

    generated = _format_generated(schema, "badgerfish", "node")

    assert "$defs" in generated
    assert not _format_errors(schema, "badgerfish", "node", _export_quiet(document, "badgerfish"))


# ---------------------------------------------------------------------------
# JsonML specifics
# ---------------------------------------------------------------------------


def test_jsonml_tag_constants_reject_wrong_roots():
    schema, document = _compiled("ordered")

    exported = _export_quiet(document, "jsonml")

    assert not _format_errors(schema, "jsonml", "section", exported)
    assert _format_errors(schema, "jsonml", "section", ["wrong", {"head": "h"}])


def test_jsonml_empty_elements_stop_at_the_attribute_object():
    schema = _compile(EMPTY_SCHEMA)

    assert not _format_errors(schema, "jsonml", "root", ["root"])
    assert not _format_errors(schema, "jsonml", "root", ["root", {}])
    assert _format_errors(schema, "jsonml", "root", ["root", "text"])
    assert _format_errors(schema, "jsonml", "root", ["root", {}, "more"])


def test_jsonml_simple_content_requires_the_scalar_item():
    schema = _compile(BOOLEAN_SCHEMA)

    assert not _format_errors(schema, "jsonml", "root", ["root", True])
    assert _format_errors(schema, "jsonml", "root", ["root"])
    assert _format_errors(schema, "jsonml", "root", ["root", "true"])


def test_jsonml_attribute_object_carries_typed_values():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:simpleContent>
    <xs:extension base="xs:int">
      <xs:attribute name="a" type="xs:string" use="required"/>
    </xs:extension>
  </xs:simpleContent></xs:complexType></xs:element>
</xs:schema>""")

    exported = _export_quiet(schema.parse(StringIO('<root a="x">42</root>')), "jsonml")

    assert exported == ["root", {"a": "x"}, 42]
    assert not _format_errors(schema, "jsonml", "root", exported)
    assert _format_errors(schema, "jsonml", "root", ["root", {"a": 5}, 42])
    assert _format_errors(schema, "jsonml", "root", ["root"])
    assert _format_errors(schema, "jsonml", "root", ["root", {"a": "x"}, 42, 42])


def test_jsonml_root_namespace_declarations_are_prefix_items():
    schema, document = _compiled("namespaced")

    exported = _export_quiet(document, "jsonml")

    assert exported == [
        "ns0:root",
        {"xmlns:ns0": "urn:example", "attr": "a"},
        ["ns0:child", "v"],
    ]
    assert not _format_errors(schema, "jsonml", "root", exported)
    # Nested attribute objects are closed against namespace declarations.
    assert _format_errors(
        schema,
        "jsonml",
        "root",
        ["ns0:root", {"attr": "a"}, ["ns0:child", {"xmlns:ns0": "urn:example"}, "v"]],
    )
    assert _format_errors(schema, "jsonml", "root", ["root", {"attr": "a"}, ["ns0:child", "v"]])


def test_jsonml_nil_uses_a_null_content_item():
    schema, document = _compiled("nillable")

    exported = _export_quiet(document, "jsonml")

    assert exported == ["nillable", None]
    assert not _format_errors(schema, "jsonml", "nillable", exported)
    non_nillable = _compile(SIMPLE_PLUS_SCHEMA)
    assert _format_errors(non_nillable, "jsonml", "root", ["root", {"id": "x"}, None])


def test_jsonml_mixed_content_keeps_text_positions_without_disclosure():
    schema, document = _compiled("mixed")

    exported = _export_quiet(document, "jsonml")

    assert exported == ["para", {"id": "p1"}, "This is a ", ["i", "fine"], " mess!"]
    assert not _format_errors(schema, "jsonml", "para", exported)
    assert _format_errors(schema, "jsonml", "para", ["para", {"id": "p1"}, 5])
    assert "xsd-mixed-order" not in _format_unvalidated(schema, "jsonml", "para")
    assert "xsd-particle-order" not in _format_unvalidated(schema, "jsonml", "para")


def test_jsonml_element_only_content_rejects_text_items():
    schema, _document = _compiled("ordered")

    assert _format_errors(schema, "jsonml", "section", ["section", "text"])


def test_jsonml_recursive_types_use_defs_and_refs():
    schema, document = _compiled("recursive")

    generated = _format_generated(schema, "jsonml", "node")

    assert "$defs" in generated
    assert not _format_errors(schema, "jsonml", "node", _export_quiet(document, "jsonml"))


def test_jsonml_wildcard_children_stay_open():
    schema = _compile(WILDCARD_CHILD_SCHEMA)

    assert not _format_errors(schema, "jsonml", "root", ["root", ["c", "x"], ["w", "t"]])
    assert "xsd-wildcard-namespace" in _format_unvalidated(schema, "jsonml", "root")


# ---------------------------------------------------------------------------
# Undetermined roots and wildcards in the conventional formats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ("parker", "badgerfish", "jsonml"))
def test_conventional_undetermined_roots_use_the_general_shape(fmt):
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r"/>
</xs:schema>""")

    exported = _export_quiet(schema.parse(StringIO("<r>hello <b>bold</b> tail</r>")), fmt)

    assert not _format_errors(schema, fmt, "r", exported)
    assert "xsd-wildcard-namespace" in _format_unvalidated(schema, fmt, "r")


def test_badgerfish_undetermined_root_accepts_objects_and_strings():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r"/>
</xs:schema>""")

    assert not _format_errors(schema, "badgerfish", "r", {"r": "text"})
    assert not _format_errors(schema, "badgerfish", "r", {"r": {"a": {"b": "c"}}})
    assert _format_errors(schema, "badgerfish", "r", {"r": 5})


def test_jsonml_undetermined_roots_stay_arrays():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r"/>
</xs:schema>""")

    exported = _export_quiet(schema.parse(StringIO("<r>hello <b>bold</b> tail</r>")), "jsonml")

    # The untyped binding records the leading character data as an empty
    # piece; the schema accepts whatever strings the exporter emits.
    assert exported[0] == "r"
    assert not _format_errors(schema, "jsonml", "r", exported)
    assert not _format_errors(schema, "jsonml", "r", ["r"])
    assert _format_errors(schema, "jsonml", "r", "not-an-array")


def test_jsonml_undetermined_nillable_roots_accept_the_null_item():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" nillable="true"/>
</xs:schema>""")

    exported = _export_quiet(
        schema.parse(
            StringIO('<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>')
        ),
        "jsonml",
    )

    assert exported == ["r", None]
    assert not _format_errors(schema, "jsonml", "r", exported)


def test_jsonml_undetermined_children_stay_arrays():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="u"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>""")

    exported = _export_quiet(schema.parse(StringIO("<root><u>text</u></root>")), "jsonml")

    # The untyped child keeps the array shape whatever text it recorded.
    assert exported[0] == "root"
    assert isinstance(exported[1], list)
    assert not _format_errors(schema, "jsonml", "root", exported)


def test_absorbed_formats_accept_a_single_global_root_without_element():
    schema = _compile(EMPTY_SCHEMA)

    for fmt in ("pyxsd", "parker"):
        assert not _format_errors(schema, fmt, None, {} if fmt == "parker" else {"$": None})


def test_native_prohibited_attributes_are_excluded_and_closed():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="derived"/>
  <xs:complexType name="base">
    <xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>
    <xs:attribute name="a" type="xs:int"/>
    <xs:attribute name="b" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="derived">
    <xs:complexContent><xs:restriction base="base">
      <xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>
      <xs:attribute name="a" use="prohibited"/>
      <xs:attribute name="b" type="xs:string"/>
    </xs:restriction></xs:complexContent>
  </xs:complexType>
</xs:schema>""")

    exported = _export_quiet(schema.parse(StringIO('<r b="x"><c>v</c></r>')), "pyxsd")

    assert exported == {"@b": "x", "c": "v"}
    assert not _format_errors(schema, "pyxsd", "r", exported)
    assert _format_errors(schema, "pyxsd", "r", {"@a": 1, "c": "v"})


def test_native_conditional_cardinality_merges_to_repeatable():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="wide">
    <xs:alternative test="@k='t'" type="tight"/>
  </xs:element>
  <xs:complexType name="wide"><xs:sequence>
    <xs:element name="c" type="xs:int" minOccurs="0" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType>
  <xs:complexType name="tight"><xs:complexContent><xs:restriction base="wide">
    <xs:sequence><xs:element name="c" type="xs:int" minOccurs="0" maxOccurs="1"/></xs:sequence>
  </xs:restriction></xs:complexContent></xs:complexType>
</xs:schema>""")

    exported = _export_quiet(schema.parse(StringIO("<r><c>1</c><c>2</c></r>")), "pyxsd")

    assert exported == {"c": [1, 2]}
    assert not _format_errors(schema, "pyxsd", "r", exported)
    assert not _format_errors(schema, "pyxsd", "r", {"c": 1})


def test_xml_namespace_attributes_keep_their_prefix():
    schema = _compile(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r"><xs:complexType>
    <xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>
    <xs:attribute ref="xml:lang"/>
  </xs:complexType></xs:element>
</xs:schema>"""
    )

    exported = _export_quiet(schema.parse(StringIO('<r xml:lang="en"><c>v</c></r>')), "badgerfish")

    assert exported == {"r": {"@xml:lang": "en", "c": "v"}}
    assert not _format_errors(schema, "badgerfish", "r", exported)
    assert not _format_errors(schema, "jsonml", "r", ["r", {"xml:lang": "en"}, ["c", "v"]])
    assert _format_errors(schema, "jsonml", "r", ["r", {"xml:lang": 5}, ["c", "v"]])


def test_jsonml_wildcard_attributes_stay_open():
    schema = _compile(WILDCARD_ATTRIBUTE_SCHEMA)

    exported = _export_quiet(schema.parse(StringIO('<root custom="c">v</root>')), "jsonml")

    assert exported == ["root", {"custom": "c"}, "v"]
    assert not _format_errors(schema, "jsonml", "root", exported)
    assert _format_errors(schema, "jsonml", "root", ["root", {"custom": 5}, "v"])
    assert "xsd-wildcard-namespace" in _format_unvalidated(schema, "jsonml", "root")


# ---------------------------------------------------------------------------
# Conditional-type namespaces shared by export and JSON Schema
# ---------------------------------------------------------------------------


NILLABLE_REQUIRED_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="e" nillable="true"><xs:complexType><xs:sequence>
      <xs:element name="c" type="xs:string"/>
    </xs:sequence></xs:complexType></xs:element>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

REQUIRED_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="e"><xs:complexType><xs:sequence>
      <xs:element name="c" type="xs:string"/>
    </xs:sequence></xs:complexType></xs:element>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""


def _write_two_namespace_schema(tmp_path, declared_uri, extension_uri):
    """A schema whose declared branch and CTA branch use two namespaces.

    ``Base`` holds one optional child in *declared_uri*; the conditional
    ``Other`` type extends it with a child in *extension_uri*, so the full
    schema closure always knows both namespaces while an instance may show
    only one.
    """
    (tmp_path / "a.xsd").write_text(
        f"""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="{declared_uri}">
  <xs:element name="a" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    (tmp_path / "z.xsd").write_text(
        f"""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="{extension_uri}">
  <xs:element name="z" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        f"""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
            xmlns:da="{declared_uri}" xmlns:ez="{extension_uri}">
  <xs:import namespace="{declared_uri}" schemaLocation="a.xsd"/>
  <xs:import namespace="{extension_uri}" schemaLocation="z.xsd"/>
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="v" type="Base">
      <xs:alternative test="@k='o'" type="Other"/>
    </xs:element>
  </xs:sequence></xs:complexType></xs:element>
  <xs:complexType name="Base"><xs:sequence>
    <xs:element ref="da:a" minOccurs="0"/>
  </xs:sequence></xs:complexType>
  <xs:complexType name="Other"><xs:complexContent><xs:extension base="Base"><xs:sequence>
    <xs:element ref="ez:z" minOccurs="0"/>
  </xs:sequence></xs:extension></xs:complexContent></xs:complexType>
</xs:schema>""",
        encoding="utf-8",
    )
    return str(main)


def test_cta_namespaces_agree_between_export_and_schema(tmp_path):
    main = _write_two_namespace_schema(tmp_path, "urn:aaa", "urn:zzz")
    schema = pyxsd.Schema.compile(main)

    cta = schema.parse(StringIO('<root><v k="o"><z:z xmlns:z="urn:zzz">v</z:z></v></root>'))
    exported = _export_quiet(cta, "badgerfish")
    assert exported == {"root": {"@xmlns": {"ns1": "urn:zzz"}, "v": {"ns1:z": "v"}}}
    assert not _format_errors(schema, "badgerfish", "root", exported)

    jsonml_exported = _export_quiet(cta, "jsonml")
    assert jsonml_exported == ["root", {"xmlns:ns1": "urn:zzz"}, ["v", ["ns1:z", "v"]]]
    assert not _format_errors(schema, "jsonml", "root", jsonml_exported)

    declared = schema.parse(StringIO('<root><v k="b"><a:a xmlns:a="urn:aaa">x</a:a></v></root>'))
    declared_exported = _export_quiet(declared, "badgerfish")
    assert declared_exported == {"root": {"@xmlns": {"ns0": "urn:aaa"}, "v": {"ns0:a": "x"}}}
    assert not _format_errors(schema, "badgerfish", "root", declared_exported)
    assert not _format_errors(schema, "jsonml", "root", _export_quiet(declared, "jsonml"))


def test_cta_namespaces_agree_for_explicit_type_markers(tmp_path):
    main = _write_two_namespace_schema(tmp_path, "urn:aaa", "urn:zzz")
    schema = pyxsd.Schema.compile(main)

    marked = schema.parse(
        StringIO(
            '<root><v xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="Other">'
            '<z:z xmlns:z="urn:zzz">v</z:z></v></root>'
        )
    )
    exported = _export_quiet(marked, "badgerfish")

    assert exported == {
        "root": {
            "@xmlns": {
                "ns1": "urn:zzz",
                "xsi": "http://www.w3.org/2001/XMLSchema-instance",
            },
            "v": {"@xsi:type": "Other", "ns1:z": "v"},
        }
    }
    assert not _format_errors(schema, "badgerfish", "root", exported)
    assert not _format_errors(schema, "jsonml", "root", _export_quiet(marked, "jsonml"))


def test_declared_branch_prefixes_use_the_full_schema_closure(tmp_path):
    main = _write_two_namespace_schema(tmp_path, "urn:zzz", "urn:aaa")
    schema = pyxsd.Schema.compile(main)

    declared = schema.parse(StringIO('<root><v k="b"><a:a xmlns:a="urn:zzz">x</a:a></v></root>'))
    exported = _export_quiet(declared, "badgerfish")

    # The schema-seeded closure knows urn:aaa sorts first, so this
    # instance's only namespace is still ns1; the old observed-instance
    # seed would have called it ns0 and disagreed with the schema.
    assert exported == {"root": {"@xmlns": {"ns1": "urn:zzz"}, "v": {"ns1:a": "x"}}}
    assert not _format_errors(schema, "badgerfish", "root", exported)
    assert not _format_errors(schema, "jsonml", "root", _export_quiet(declared, "jsonml"))

    cta = schema.parse(StringIO('<root><v k="o"><z:z xmlns:z="urn:aaa">v</z:z></v></root>'))
    assert _export_quiet(cta, "badgerfish") == {
        "root": {"@xmlns": {"ns0": "urn:aaa"}, "v": {"ns0:z": "v"}}
    }
    assert not _format_errors(schema, "badgerfish", "root", _export_quiet(cta, "badgerfish"))


# ---------------------------------------------------------------------------
# Nillable parents with required children
# ---------------------------------------------------------------------------


def test_native_nillable_required_children_are_not_unconditional():
    schema = _compile(NILLABLE_REQUIRED_CHILD_SCHEMA)

    document = schema.parse(
        StringIO(
            '<root><e xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/></root>'
        )
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", PyXSDWarning)
        exported = document.to_data(format="pyxsd", fidelity="strict")

    assert exported == {"e": {"@xsi:nil": "true"}}
    assert not _format_errors(schema, "pyxsd", "root", exported)
    # The nil marker keeps its constraint and typed child content still
    # rejects wrong scalar shapes.
    assert _format_errors(schema, "pyxsd", "root", {"e": {"@xsi:nil": "false"}})
    assert _format_errors(schema, "pyxsd", "root", {"e": {"@xsi:nil": "true", "c": 5}})
    assert _format_errors(schema, "pyxsd", "root", {"e": {"c": 5}})


def test_native_required_children_stay_required_without_nillability():
    schema = _compile(REQUIRED_CHILD_SCHEMA)

    assert not _format_errors(schema, "pyxsd", "root", {"e": {"c": "v"}})
    assert _format_errors(schema, "pyxsd", "root", {"e": {}})


def test_badgerfish_nillable_required_children_are_not_unconditional():
    schema = _compile(NILLABLE_REQUIRED_CHILD_SCHEMA)

    document = schema.parse(
        StringIO(
            '<root><e xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/></root>'
        )
    )
    exported = _export_quiet(document, "badgerfish")

    assert exported == {
        "root": {
            "@xmlns": {"xsi": "http://www.w3.org/2001/XMLSchema-instance"},
            "e": {"@xsi:nil": True},
        }
    }
    assert not _format_errors(schema, "badgerfish", "root", exported)
    assert _format_errors(schema, "badgerfish", "root", {"root": {"e": {"@xsi:nil": True, "c": 5}}})
    assert _format_errors(schema, "badgerfish", "root", {"root": {"e": {"c": 5}}})


def test_badgerfish_required_children_stay_required_without_nillability():
    schema = _compile(REQUIRED_CHILD_SCHEMA)

    assert not _format_errors(schema, "badgerfish", "root", {"root": {"e": {"c": "v"}}})
    assert _format_errors(schema, "badgerfish", "root", {"root": {"e": {}}})


def test_native_undetermined_nillable_roots_disclose_the_ambiguity():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" nillable="true"/>
</xs:schema>""")

    assert not _format_errors(schema, "pyxsd", "r", {"$": None})
    assert not _format_errors(schema, "pyxsd", "r", {"$": "text"})
    assert "native-null-ambiguity" in _format_unvalidated(schema, "pyxsd", "r")


def test_badgerfish_undetermined_children_use_the_general_shape():
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="u"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>""")

    exported = _export_quiet(schema.parse(StringIO("<root><u>x<b>c</b></u></root>")), "badgerfish")

    assert not _format_errors(schema, "badgerfish", "root", exported)
    assert _format_errors(schema, "badgerfish", "root", {"root": {"u": 5}})


@pytest.mark.parametrize("fmt", ("badgerfish", "jsonml"))
def test_root_carrying_formats_walk_substitution_members_once(fmt):
    schema = _compile(SUBSTITUTION_SCHEMA)

    generated = _format_generated(schema, fmt, None)

    assert not _format_errors(
        schema, fmt, None, {"head": "h"} if fmt == "badgerfish" else ["head", "h"]
    )
    assert not _format_errors(
        schema, fmt, None, {"member": 7} if fmt == "badgerfish" else ["member", 7]
    )
    assert generated["x-pyxsd-format"] == fmt


@pytest.mark.parametrize("fmt", ("pyxsd", "parker", "badgerfish", "jsonml"))
def test_conventional_formats_skip_unresolved_alternatives(fmt):
    schema = _compile("""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="xs:string">
    <xs:alternative test="@k='x'" type="missing:type"/>
  </xs:element>
</xs:schema>""")

    assert _format_generated(schema, fmt, "r")["x-pyxsd-format"] == fmt
