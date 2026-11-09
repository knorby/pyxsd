"""Contract tests for JSON data-conversion formats, scalars, and names.

The schema/document fixtures below are shared with the later export, import,
and JSON Schema tasks. They cover simple, attribute, repeated, ordered,
mixed, namespaced, nillable, and recursive content.
"""

from __future__ import annotations

import decimal
import json
import math
import re
from io import StringIO
from typing import get_args

import pytest

import pyxsd
from pyxsd.data_conversion.contracts import (
    Fidelity,
    FormatName,
    JsonValue,
    json_pointer,
    raise_conversion,
    validate_fidelity,
    validate_format,
    warn_conversion,
)
from pyxsd.data_conversion.names import (
    PrefixAllocator,
    eqname_to_expanded,
    expanded_to_eqname,
)
from pyxsd.data_conversion.values import scalar_to_json, scalar_to_lexical
from pyxsd.exceptions import PyXSDError, PyXSDWarning
from pyxsd.xsd_data_types import (
    IDREFS,
    Boolean,
    Date,
    Decimal,
    Double,
    Float,
    Integer,
    QName,
    String,
    TypeList,
    XsdDataType,
    XsdList,
    qname_context,
)

SIMPLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="simple" type="xs:int"/>
</xs:schema>"""
SIMPLE_INSTANCE = "<simple>42</simple>"

ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="attributed">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:attribute name="id" type="xs:string" use="required"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
ATTRIBUTE_INSTANCE = '<attributed id="a1">text</attributed>'

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
REPEATED_INSTANCE = "<dates><date>2026-01-01</date><date>2026-02-01</date></dates>"

ORDERED_SCHEMA = """\
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
ORDERED_INSTANCE = (
    "<section><head>Introduction</head><p>First</p><p>Second</p><note>End</note></section>"
)

MIXED_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="para">
    <xs:complexType mixed="true">
      <xs:sequence>
        <xs:element name="i" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="id" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
MIXED_INSTANCE = '<para id="p1">This is a <i>fine</i> mess!</para>'

NAMESPACED_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="child" type="xs:string"/>
      </xs:sequence>
      <xs:attribute name="attr" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
NAMESPACED_INSTANCE = '<root xmlns="urn:example" attr="a"><child>v</child></root>'

NILLABLE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="nillable" type="xs:string" nillable="true"/>
</xs:schema>"""
NILLABLE_INSTANCE = (
    '<nillable xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>'
)

RECURSIVE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="node">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="node" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="name" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
RECURSIVE_INSTANCE = '<node name="a"><node name="b"><node name="c"/></node></node>'

FIXTURES: dict[str, tuple[str, str]] = {
    "simple": (SIMPLE_SCHEMA, SIMPLE_INSTANCE),
    "attribute": (ATTRIBUTE_SCHEMA, ATTRIBUTE_INSTANCE),
    "repeated": (REPEATED_SCHEMA, REPEATED_INSTANCE),
    "ordered": (ORDERED_SCHEMA, ORDERED_INSTANCE),
    "mixed": (MIXED_SCHEMA, MIXED_INSTANCE),
    "namespaced": (NAMESPACED_SCHEMA, NAMESPACED_INSTANCE),
    "nillable": (NILLABLE_SCHEMA, NILLABLE_INSTANCE),
    "recursive": (RECURSIVE_SCHEMA, RECURSIVE_INSTANCE),
}

DIAGNOSTIC_CODES = (
    "json-invalid",
    "json-duplicate-key",
    "root-ambiguous",
    "root-mismatch",
    "name-invalid",
    "name-ambiguous",
    "namespace-unresolved",
    "container-invalid",
    "scalar-type-invalid",
    "marker-conflict",
    "order-ambiguous",
    "mixed-content",
    "attributes-lost",
    "namespace-lost",
    "empty-nil-ambiguous",
    "schema-shape-unsupported",
)


class IntList(XsdList):
    """A test-only XSD list over ``xs:integer``."""

    itemType = Integer


class Opaque(XsdDataType):
    """A datatype outside the built-in scalar families."""


def _compile_fixture(name: str) -> pyxsd.Document:
    schema_source, instance_source = FIXTURES[name]
    schema = pyxsd.Schema.compile(StringIO(schema_source))
    return schema.parse(StringIO(instance_source))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_shared_fixture_compiles_and_validates(name):
    document = _compile_fixture(name)
    assert document.root is not None
    assert not document.report.errors


def test_scalar_conversion_reads_bound_xsd_instances():
    assert scalar_to_json(_compile_fixture("simple").root, format="pyxsd-json") == 42
    dates = _compile_fixture("repeated").root.date
    assert scalar_to_json(dates[0], format="pyxsd-json") == "2026-01-01"


# ---------------------------------------------------------------------------
# Format and fidelity contracts
# ---------------------------------------------------------------------------


def test_format_names_match_the_profile_contract():
    assert get_args(FormatName) == (
        "pyxsd",
        "pyxsd-json",
        "parker",
        "badgerfish",
        "jsonml",
    )


def test_fidelity_names_match_the_profile_contract():
    assert get_args(Fidelity) == ("strict", "best-effort")


@pytest.mark.parametrize("name", get_args(FormatName))
def test_validate_format_accepts_every_documented_name(name):
    assert validate_format(name) == name


@pytest.mark.parametrize("name", ["", "PYXSD", "xml", "pyxsd-json2", "badgerFish"])
def test_validate_format_rejects_unknown_names(name):
    with pytest.raises(ValueError) as caught:
        validate_format(name)
    assert str(caught.value) == f"unknown data format: {name!r}"


@pytest.mark.parametrize("fidelity", get_args(Fidelity))
def test_validate_fidelity_accepts_every_documented_mode(fidelity):
    assert validate_fidelity(fidelity) == fidelity


@pytest.mark.parametrize("fidelity", ["", "STRICT", "lossy", "best_effort"])
def test_validate_fidelity_rejects_unknown_modes(fidelity):
    with pytest.raises(ValueError) as caught:
        validate_fidelity(fidelity)
    assert str(caught.value) == f"unknown fidelity mode: {fidelity!r}"


def test_json_value_alias_accepts_only_json_safe_shapes():
    value: JsonValue = {"root": [None, True, 1, 2.5, "text", {"@a": []}]}
    assert json.loads(json.dumps(value)) == value


# ---------------------------------------------------------------------------
# JSON Pointer diagnostics
# ---------------------------------------------------------------------------


def test_json_pointer_escapes_tokens_per_rfc_6901():
    assert json_pointer(["a~", "b/c", 0]) == "/a~0/b~1c/0"
    assert json_pointer([]) == ""
    assert json_pointer(iter(["only"])) == "/only"


@pytest.mark.parametrize("code", DIAGNOSTIC_CODES)
def test_raise_conversion_reports_code_path_and_detail(code):
    with pytest.raises(PyXSDError) as caught:
        raise_conversion(code, "/root/item", "detail")
    assert str(caught.value) == f"{code} at '/root/item': detail"


@pytest.mark.parametrize("code", DIAGNOSTIC_CODES)
def test_warn_conversion_reports_code_path_and_detail(code):
    with pytest.warns(PyXSDWarning) as caught:
        warn_conversion(code, "/root/item", "detail")
    assert len(caught) == 1
    assert str(caught[0].message) == f"{code} at '/root/item': detail"


def test_diagnostics_escape_a_lexical_name_in_the_pointer_path():
    path = json_pointer(["lex/name~with", 0])
    assert path == "/lex~1name~0with/0"
    with pytest.raises(PyXSDError, match=re.escape(path)) as caught:
        raise_conversion("name-invalid", path, "invalid lexical name 'lex/name~with'")
    assert "invalid lexical name 'lex/name~with'" in str(caught.value)

    with pytest.warns(PyXSDWarning, match=re.escape(path)) as warnings_seen:
        warn_conversion("order-ambiguous", path, "grouped order lost")
    assert "grouped order lost" in str(warnings_seen[0].message)


# ---------------------------------------------------------------------------
# Scalar export
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (False, False),
        (Boolean("true"), True),
        (Boolean("false"), False),
        (Integer("0"), 0),
        (Integer("-42"), -42),
        (Integer("9007199254740993"), 9007199254740993),
        (5, 5),
        (Decimal("1.20"), "1.20"),
        (Decimal("-0"), "-0"),
        (Decimal("1000.00"), "1000.00"),
        (Decimal("0.000"), "0.000"),
        (Double("1.5"), 1.5),
        (Double("INF"), "INF"),
        (Double("-INF"), "-INF"),
        (Double("NaN"), "NaN"),
        (Float("INF"), "INF"),
        (Date("2026-01-01"), "2026-01-01"),
        ("text", "text"),
    ],
)
def test_scalar_to_json_uses_the_fixed_scalar_contract(value, expected):
    result = scalar_to_json(value, format="pyxsd-json")
    assert result == expected
    assert type(result) is type(expected)


def test_scalar_to_json_preserves_negative_zero():
    decimal_zero = scalar_to_json(Decimal("-0.0"), format="pyxsd-json")
    assert decimal_zero == "-0.0"
    assert isinstance(decimal_zero, str)

    float_zero = scalar_to_json(Double("-0"), format="pyxsd-json")
    assert float_zero == 0.0
    assert math.copysign(1.0, float_zero) == -1.0


@pytest.mark.parametrize("name", get_args(FormatName))
def test_scalar_to_json_spells_non_finite_values_in_xsd_form(name):
    assert scalar_to_json(Double("INF"), format=name) == "INF"
    assert scalar_to_json(Double("-INF"), format=name) == "-INF"
    assert scalar_to_json(Double("NaN"), format=name) == "NaN"


def test_scalar_to_json_native_typed_false_uses_the_lexical_form():
    assert scalar_to_json(Decimal("1.20"), format="pyxsd", typed=False) == "1.20"
    assert scalar_to_json(Integer("42"), format="pyxsd", typed=False) == "42"
    assert scalar_to_json(Boolean("true"), format="pyxsd", typed=False) == "true"
    assert scalar_to_json(Double("INF"), format="pyxsd", typed=False) == "INF"
    assert scalar_to_json("text", format="pyxsd", typed=False) == "text"


def test_scalar_to_json_native_typed_false_rejects_non_scalars():
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_json(1, format="pyxsd", typed=False)


def test_scalar_to_json_qname_uses_eqnames_only_for_pyxsd_json():
    with qname_context({"p": "urn:example", "": "urn:default"}):
        prefixed = QName("p:root")
        defaulted = QName("root")
        unbound = QName("z:root")

    assert scalar_to_json(prefixed, format="pyxsd-json") == "Q{urn:example}root"
    assert scalar_to_json(defaulted, format="pyxsd-json") == "Q{urn:default}root"
    assert scalar_to_json(prefixed, format="pyxsd") == "p:root"
    assert scalar_to_json(prefixed, format="badgerfish") == "p:root"
    assert scalar_to_json(prefixed, format="parker") == "p:root"
    assert scalar_to_json(prefixed, format="jsonml") == "p:root"
    assert scalar_to_json(prefixed, format="pyxsd", typed=False) == "p:root"

    with pytest.raises(PyXSDError, match="namespace-unresolved"):
        scalar_to_json(unbound, format="pyxsd-json")


def test_scalar_to_json_rejects_unresolved_eqname_conversion():
    with pytest.raises(PyXSDError, match="namespace-unresolved"):
        scalar_to_json(QName("p:root"), format="pyxsd-json")


def test_scalar_to_json_emits_lists_as_arrays_even_when_short():
    assert scalar_to_json(IntList(""), format="pyxsd-json") == []
    assert scalar_to_json(IntList("7"), format="pyxsd-json") == [7]
    assert scalar_to_json(IntList("1 2"), format="pyxsd-json") == [1, 2]
    assert scalar_to_json(IntList("1 2"), format="pyxsd") == [1, 2]
    assert scalar_to_json(IntList("1 2"), format="pyxsd", typed=False) == "1 2"


def test_scalar_to_json_native_list_strings_are_arrays_in_typed_mode():
    assert scalar_to_json(IDREFS("a b"), format="pyxsd-json") == ["a", "b"]
    assert scalar_to_json(IDREFS("a"), format="pyxsd-json") == ["a"]
    assert scalar_to_json(IDREFS("a b"), format="pyxsd", typed=False) == "a b"
    assert scalar_to_json(TypeList([Integer("1"), Integer("2")]), format="pyxsd-json") == [1, 2]


def test_scalar_to_json_rejects_values_outside_the_json_contract():
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_json(object(), format="pyxsd-json")


# ---------------------------------------------------------------------------
# Scalar import
# ---------------------------------------------------------------------------


def test_scalar_to_lexical_converts_booleans_before_integers():
    assert scalar_to_lexical(True, datatype=Boolean, format="pyxsd-json") == "true"
    assert scalar_to_lexical(False, datatype=Boolean, format="pyxsd-json") == "false"
    assert scalar_to_lexical(Boolean("true"), datatype=Boolean, format="pyxsd") == "true"
    assert scalar_to_lexical(Boolean("false"), datatype=Boolean, format="pyxsd") == "false"

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_lexical(1, datatype=Boolean, format="pyxsd-json")


def test_scalar_to_lexical_converts_arbitrary_precision_integers():
    assert scalar_to_lexical(42, datatype=Integer, format="pyxsd-json") == "42"
    assert scalar_to_lexical(-42, datatype=Integer, format="pyxsd-json") == "-42"
    assert (
        scalar_to_lexical(9007199254740993, datatype=Integer, format="pyxsd-json")
        == "9007199254740993"
    )
    assert scalar_to_lexical(Integer("7"), datatype=Integer, format="pyxsd") == "7"
    assert scalar_to_lexical("7", datatype=Integer, format="pyxsd") == "7"


def test_scalar_to_lexical_rejects_booleans_and_non_integers_as_integers():
    for value in (True, Boolean("true"), 1.0, "1"):
        with pytest.raises(PyXSDError, match="scalar-type-invalid"):
            scalar_to_lexical(value, datatype=Integer, format="pyxsd-json")


def test_scalar_to_lexical_passes_decimal_strings_through():
    assert scalar_to_lexical("1.20", datatype=Decimal, format="pyxsd-json") == "1.20"
    assert scalar_to_lexical("-0", datatype=Decimal, format="pyxsd-json") == "-0"
    assert scalar_to_lexical("1e3", datatype=Decimal, format="pyxsd-json") == "1e3"
    assert scalar_to_lexical("1.20", datatype=Decimal, format="pyxsd") == "1.20"
    assert scalar_to_lexical(decimal.Decimal("1E+3"), datatype=Decimal, format="pyxsd") == "1000"
    assert scalar_to_lexical(7, datatype=Decimal, format="pyxsd") == "7"


def test_scalar_to_lexical_rejects_wrong_decimal_types():
    for value in (1, 1.5):
        with pytest.raises(PyXSDError, match="scalar-type-invalid"):
            scalar_to_lexical(value, datatype=Decimal, format="pyxsd-json")
    for value in (True, 1.5):
        with pytest.raises(PyXSDError, match="scalar-type-invalid"):
            scalar_to_lexical(value, datatype=Decimal, format="pyxsd")


def test_scalar_to_lexical_converts_finite_and_non_finite_floats():
    assert scalar_to_lexical(1.5, datatype=Double, format="pyxsd-json") == "1.5"
    assert scalar_to_lexical(1.5, datatype=Float, format="pyxsd-json") == "1.5"
    assert scalar_to_lexical(1, datatype=Double, format="pyxsd-json") == "1.0"
    assert scalar_to_lexical(10**20, datatype=Double, format="pyxsd-json") == "1e+20"
    assert scalar_to_lexical(-0.0, datatype=Double, format="pyxsd-json") == "-0.0"
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(PyXSDError, match="scalar-type-invalid"):
            scalar_to_lexical(value, datatype=Double, format="pyxsd-json")
    assert scalar_to_lexical("INF", datatype=Double, format="pyxsd-json") == "INF"
    assert scalar_to_lexical("-INF", datatype=Double, format="pyxsd-json") == "-INF"
    assert scalar_to_lexical("NaN", datatype=Double, format="pyxsd-json") == "NaN"
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_lexical(Double("INF"), datatype=Double, format="pyxsd")


def test_scalar_to_lexical_rejects_wrong_float_types():
    for value in (True, Boolean("true"), "1.5", []):
        with pytest.raises(PyXSDError, match="scalar-type-invalid"):
            scalar_to_lexical(value, datatype=Double, format="pyxsd-json")
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_lexical(10**400, datatype=Double, format="pyxsd-json")


def test_scalar_to_lexical_joins_xsd_list_arrays():
    assert scalar_to_lexical([1, 2], datatype=IntList, format="pyxsd-json") == "1 2"
    assert scalar_to_lexical([7], datatype=IntList, format="pyxsd-json") == "7"
    assert scalar_to_lexical([], datatype=IntList, format="pyxsd-json") == ""
    assert scalar_to_lexical([1, 2], datatype=IntList, format="pyxsd") == "1 2"
    assert scalar_to_lexical("1 2", datatype=IntList, format="pyxsd") == "1 2"
    assert scalar_to_lexical(["a", "b"], datatype=IDREFS, format="pyxsd-json") == "a b"
    assert scalar_to_lexical(["a"], datatype=IDREFS, format="pyxsd-json") == "a"
    assert scalar_to_lexical([], datatype=IDREFS, format="pyxsd-json") == ""
    assert scalar_to_lexical(["a", "b"], datatype=IDREFS, format="pyxsd") == "a b"
    assert scalar_to_lexical(["a"], datatype=TypeList, format="pyxsd-json") == "a"


def test_scalar_to_lexical_rejects_wrong_list_containers_and_items():
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_lexical("1 2", datatype=IntList, format="pyxsd-json")
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_lexical(1, datatype=IntList, format="pyxsd-json")
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_lexical([1], datatype=IDREFS, format="pyxsd-json")
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_lexical(["x"], datatype=IntList, format="pyxsd-json")
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_lexical([True], datatype=IntList, format="pyxsd-json")


def test_scalar_to_lexical_handles_string_based_datatypes():
    assert scalar_to_lexical("hello", datatype=String, format="pyxsd-json") == "hello"
    assert scalar_to_lexical("p:root", datatype=QName, format="pyxsd-json") == "p:root"
    assert scalar_to_lexical("2026-01-01", datatype=Date, format="pyxsd-json") == "2026-01-01"
    assert scalar_to_lexical("x", datatype=Opaque, format="pyxsd-json") == "x"

    for datatype in (String, QName, Date, Opaque):
        with pytest.raises(PyXSDError, match="scalar-type-invalid"):
            scalar_to_lexical(1, datatype=datatype, format="pyxsd-json")


def test_scalar_to_lexical_native_accepts_lexical_strings():
    assert scalar_to_lexical("true", datatype=Boolean, format="pyxsd") == "true"
    assert scalar_to_lexical("42", datatype=Integer, format="pyxsd") == "42"
    assert scalar_to_lexical("1.20", datatype=Decimal, format="pyxsd") == "1.20"
    assert scalar_to_lexical("INF", datatype=Double, format="pyxsd") == "INF"
    assert scalar_to_lexical("1 2", datatype=IntList, format="pyxsd") == "1 2"
    assert scalar_to_lexical("a b", datatype=IDREFS, format="pyxsd") == "a b"


def test_scalar_to_lexical_typed_false_requires_lexical_strings():
    assert scalar_to_lexical("1.20", datatype=Decimal, format="pyxsd-json", typed=False) == "1.20"
    assert scalar_to_lexical("true", datatype=Boolean, format="pyxsd-json", typed=False) == "true"
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_lexical(Boolean("true"), datatype=Boolean, format="pyxsd", typed=False)


# ---------------------------------------------------------------------------
# Names, EQNames, and prefix allocation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expanded", "eqname"),
    [
        ("{urn:example}root", "Q{urn:example}root"),
        ("root", "root"),
        ("{}root", "root"),
    ],
)
def test_expanded_to_eqname(expanded, eqname):
    assert expanded_to_eqname(expanded) == eqname


@pytest.mark.parametrize(
    ("eqname", "expanded"),
    [
        ("Q{urn:example}root", "{urn:example}root"),
        ("root", "root"),
        ("Q{}root", "root"),
    ],
)
def test_eqname_to_expanded(eqname, expanded):
    assert eqname_to_expanded(eqname) == expanded


@pytest.mark.parametrize(
    "name",
    [
        "",
        "{",
        "{urn:example",
        "{urn:example}",
        "{}",
        "{urn:example}1root",
        "{urn:example}bad name",
        "{urn:example}root/x",
        "{urn:example}root~x",
        "p:root",
        "a}b",
    ],
)
def test_expanded_to_eqname_rejects_invalid_names(name):
    with pytest.raises(PyXSDError, match="name-invalid"):
        expanded_to_eqname(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Q{",
        "Q{urn:example",
        "Q{urn:example}",
        "Q{}",
        "Q{urn:example}1root",
        "Q{urn:example}bad name",
        "Q{urn:example}root/x",
        "Q{urn:example}root~x",
        "p:root",
        "{urn:example}root",
    ],
)
def test_eqname_to_expanded_rejects_invalid_names(name):
    with pytest.raises(PyXSDError, match="name-invalid"):
        eqname_to_expanded(name)


def test_prefix_allocator_allocates_known_names_in_sorted_uri_order():
    allocator = PrefixAllocator(["{urn:z}last", "{urn:a}first", "plain", "{urn:z}other"])

    assert allocator.lexical("{urn:z}last") == "ns1:last"
    assert allocator.lexical("{urn:a}first") == "ns0:first"
    assert allocator.lexical("plain") == "plain"
    assert allocator.lexical("{urn:z}attribute", attribute=True) == "ns1:attribute"
    assert allocator.frame() == {"ns0": "urn:a", "ns1": "urn:z"}


def test_prefix_allocator_frame_reports_only_used_prefixes():
    allocator = PrefixAllocator(["{urn:unused}x", "{urn:used}y"])

    assert allocator.frame() == {}
    assert allocator.lexical("{urn:used}y") == "ns1:y"
    assert allocator.frame() == {"ns1": "urn:used"}


def test_prefix_allocator_wildcard_prefixes_are_uri_based_and_stable():
    first = PrefixAllocator(["{urn:known}root"])
    unrelated = PrefixAllocator()

    assert first.lexical("{urn:known}root") == "ns0:root"
    wildcard = first.lexical("{urn:wild}child")
    assert wildcard == unrelated.lexical("{urn:wild}child")
    prefix, _local = wildcard.split(":")
    assert prefix.startswith("ns_")
    assert wildcard == f"{prefix}:child"
    assert first.lexical("{urn:wild}other") == f"{prefix}:other"

    other = first.lexical("{urn:other}child")
    assert other != wildcard
    assert first.frame() == {
        "ns0": "urn:known",
        prefix: "urn:wild",
        other.split(":")[0]: "urn:other",
    }


def test_prefix_allocator_rejects_invalid_names():
    with pytest.raises(PyXSDError, match="name-invalid"):
        PrefixAllocator(["{urn:broken"])

    allocator = PrefixAllocator()
    with pytest.raises(PyXSDError, match="name-invalid"):
        allocator.lexical("{urn:broken")
