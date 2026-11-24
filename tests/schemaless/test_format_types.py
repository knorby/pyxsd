"""Opt-in JSON format conversions preserve the authoritative native value."""

from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from uuid import UUID

import pytest

from pyxsd import xsd_data_types as xsd
from pyxsd.schemaless import from_data, from_json
from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.values import XSD_NS


@pytest.mark.parametrize(
    ("format_name", "native", "expected", "canonical"),
    [
        (
            "uuid",
            "123E4567-E89B-12D3-A456-426614174000",
            UUID("123e4567-e89b-12d3-a456-426614174000"),
            "123e4567-e89b-12d3-a456-426614174000",
        ),
        ("ipv4", "192.0.2.1", IPv4Address("192.0.2.1"), "192.0.2.1"),
        ("ipv6", "2001:0DB8:0:0:0:0:0:1", IPv6Address("2001:db8::1"), "2001:db8::1"),
    ],
)
@pytest.mark.parametrize("format_assertion", [False, True])
def test_address_formats_are_opt_in(format_name, native, expected, canonical, format_assertion):
    schema = {"format": format_name}
    default = from_data(native, json_schema=schema, format_assertion=format_assertion)
    doc = from_json(
        f'"{native}"',
        json_schema=schema,
        format_assertion=format_assertion,
        format_typing=True,
    )

    assert doc.is_valid and default.is_valid
    assert type(default.value_at("", typed=True)) is str
    assert default.to_xdm() == (XdmAtomic.string(native),)
    typed = doc.value_at("", typed=True)
    assert type(typed) is type(expected)
    assert typed == expected
    assert doc.to_xdm() == (XdmAtomic.string(canonical),)
    assert doc.value_at("") == doc.to_data() == native
    assert doc.to_json() == f'"{native}"'


@pytest.mark.parametrize(
    ("schema", "native", "datatype", "expected"),
    [
        ({"format": "int32"}, 1.0, xsd.Int, 1),
        ({"format": "int32"}, -(2**31), xsd.Int, -(2**31)),
        ({"format": "int32"}, 2**31 - 1, xsd.Int, 2**31 - 1),
        ({"format": "int64"}, Decimal("1E+3"), xsd.Long, 1000),
        ({"format": "int64"}, float(2**63 - 1024), xsd.Long, 2**63 - 1024),
        ({"format": "int64"}, "-9223372036854775808", xsd.Long, -(2**63)),
        ({"format": "int64"}, "0009223372036854775807", xsd.Long, 2**63 - 1),
        ({"format": "float"}, Decimal("16777217"), xsd.Float, 16777216),
        ({"format": "float"}, 2**128 - 2**103 - 1, xsd.Float, 2**128 - 2**104),
        ({"format": "float"}, -(2**128 - 2**103 - 1), xsd.Float, -(2**128 - 2**104)),
        ({"format": "double"}, Decimal("0.125"), xsd.Double, 0.125),
        ({"format": "byte"}, "YQ==", xsd.Base64Binary, "YQ=="),
        ({"contentEncoding": "base64"}, "YQ==", xsd.Base64Binary, "YQ=="),
    ],
)
def test_openapi_types_keep_native_storage(schema, native, datatype, expected):
    default = from_data(native, json_schema=schema)
    doc = from_data(native, json_schema=schema, format_typing=True, format_assertion=True)

    assert doc.is_valid
    typed = doc.value_at("", typed=True)
    assert type(typed) is datatype
    assert typed == expected
    atomic = doc.to_xdm()[0]
    assert isinstance(atomic, XdmAtomic)
    assert atomic.type_name == f"{{{XSD_NS}}}{datatype.name}"
    assert type(atomic.value) is datatype
    assert atomic.value == typed
    assert type(doc.to_data()) is type(default.value_at("", typed=True)) is type(native)
    assert doc.to_data() == native
    assert doc.to_json() == default.to_json()


@pytest.mark.parametrize(
    ("format_name", "native"),
    [
        ("uuid", "{123e4567-e89b-12d3-a456-426614174000}"),
        ("ipv4", "192.000.2.1"),
        ("ipv6", "fe80::1%eth0"),
        ("int32", 2**31),
        ("int32", 1.5),
        ("int64", 2**63),
        ("int64", " 1"),
        ("int64", "+1"),
        ("int64", "1.0"),
        ("int64", "1e0"),
        ("int64", "\N{MINUS SIGN}1"),
        ("float", 2**128 - 2**103),
        ("double", Decimal("1e309")),
        ("byte", "Y Q=="),
        ("byte", "YR=="),
    ],
)
def test_invalid_format_values_keep_native_type_without_assertion(format_name, native):
    schema = {"format": format_name}
    doc = from_data(native, json_schema=schema, format_typing=True)
    strict = from_data(native, json_schema=schema, format_typing=True, format_assertion=True)

    assert doc.is_valid
    assert not strict.is_valid
    for fallback in (doc, strict):
        assert type(fallback.value_at("", typed=True)) is type(native)
        assert fallback.value_at("", typed=True) == native
        assert fallback.profile().at("").interpretation == "native"


@pytest.mark.parametrize(
    ("format_name", "native"),
    [
        ("uuid", 1),
        ("int32", "1"),
        ("int32", True),
        ("int64", False),
        ("float", "1.5"),
        ("double", "1.5"),
        ("byte", 1),
        ("json-pointer", "/a~1b"),
        ("regex", "[a-z]"),
    ],
)
def test_inapplicable_or_untyped_formats_keep_the_native_kind(format_name, native):
    doc = from_data(
        native, json_schema={"format": format_name}, format_assertion=True, format_typing=True
    )
    assert doc.is_valid
    assert type(doc.value_at("", typed=True)) is type(native)
    assert doc.value_at("", typed=True) == native
    assert doc.profile().at("").interpretation == "native"


@pytest.mark.parametrize("native", ["YR==", "Y Q==", 1])
def test_content_encoding_never_asserts_or_types_inapplicable_values(native):
    doc = from_data(
        native,
        json_schema={"contentEncoding": "base64"},
        format_assertion=True,
        format_typing=True,
    )
    assert doc.is_valid
    assert type(doc.value_at("", typed=True)) is type(native)
    assert doc.value_at("", typed=True) == native
    assert doc.profile().at("").interpretation == "native"


def test_resolved_adapter_formats_the_checked_occurrence_for_projection():
    from pyxsd.schemaless.format_types import schema_format_adapter

    native = float(2**63 - 1024)
    adapter = schema_format_adapter([{"format": "int64"}], native, lambda: repr(native))
    assert adapter is not None
    assert adapter.format(adapter.parse(repr(native))) == "9223372036854774784"
    assert not adapter.test("9223372036854775808")
