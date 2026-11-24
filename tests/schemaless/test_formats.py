"""Format assertions are deterministic with the plain JSON Schema extra."""

import sys

import pytest
from jsonschema import Draft202012Validator

from pyxsd.locations import DataLocation
from pyxsd.schemaless import formats, from_data
from pyxsd.schemaless.formats import (
    assertion_checker,
    is_rfc3339_date,
    is_rfc3339_date_time,
    is_rfc3339_time,
)


def test_assertion_checker_preserves_other_draft_formats_without_mutating_the_draft():
    translated = {"date", "date-time", "time", "uuid", "ipv4", "ipv6", "json-pointer", "regex"}
    draft = Draft202012Validator.FORMAT_CHECKER
    original = draft.checkers.copy()
    checker = assertion_checker()
    assert checker is not draft
    assert set(checker.checkers) == original.keys() | translated
    assert {name: checker.checkers[name] for name in original.keys() - translated} == {
        name: original[name] for name in original.keys() - translated
    }
    assert draft.checkers == original
    assert not checker.conforms("not-an-address", "email")


def test_format_assertion_rejects_invalid_rfc3339_without_optional_validator():
    schema = {"type": "string", "format": "date-time"}
    invalid = from_data("not-a-date", json_schema=schema, format_assertion=True)
    assert not invalid.is_valid

    valid = from_data("2026-11-23t12:34:56.789z", json_schema=schema, format_assertion=True)
    assert valid.is_valid


def test_rfc3339_checker_owns_date_time_and_time_boundaries():
    assert is_rfc3339_date("2026-11-23")
    assert is_rfc3339_date_time("2024-02-29T00:00:00Z")
    assert is_rfc3339_date_time("2026-11-23t12:34:60z")
    assert is_rfc3339_time("12:34:56-00:00")

    assert not is_rfc3339_date("2027-02-29")
    assert not is_rfc3339_date_time("2026-11-23 12:34:56Z")
    assert not is_rfc3339_date_time("2026-11-23T24:00:00Z")
    assert not is_rfc3339_date_time("2026-11-23T12:34:61Z")
    assert not is_rfc3339_time("12:34:56")


def test_assertion_checker_is_process_stable_and_overrides_translated_formats():
    first = assertion_checker()
    second = assertion_checker()
    assert first is second
    assert first.checkers["date-time"][0] is is_rfc3339_date_time
    assert first.checkers["date"][0] is is_rfc3339_date
    assert first.checkers["time"][0] is is_rfc3339_time
    assert first.checkers["uuid"][0] is formats.is_uuid
    assert first.checkers["ipv4"][0] is formats.is_ipv4
    assert first.checkers["ipv6"][0] is formats.is_ipv6
    assert first.checkers["json-pointer"][0] is formats.is_json_pointer
    assert first.checkers["regex"][0] is formats.is_regex


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("2026-11-23", True),
        ("0000-02-29", True),
        ("9999-12-31", True),
        ("2000-02-29", True),
        ("1900-02-29", False),
        ("2027-02-29", False),
        ("2026-04-31", False),
        ("2026-11-00", False),
        ("2026-13-01", False),
        ("2026-00-01", False),
        ("02026-11-23", False),
        ("2026-1-23", False),
        ("2026-11-23Z", False),
        ("2026-11-23\n", False),
        ("\u0662\u0660\u0662\u0666-11-23", False),
        (None, True),
    ],
)
def test_rfc3339_full_date(value, valid):
    assert is_rfc3339_date(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("2024-02-29T00:00:00Z", True),
        ("2026-11-23t12:34:60z", True),
        ("2026-11-23T12:34:56.0-00:00", True),
        ("2026-02-29T00:00:00Z", False),
        ("2026-11-23 12:34:56Z", False),
        ("2026-11-23T24:00:00Z", False),
        ("2026-11-23T12:34:61Z", False),
        ("2026-11-23T12:34:56", False),
        ("", False),
        (42, True),
    ],
)
def test_rfc3339_date_time(value, valid):
    assert is_rfc3339_date_time(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("00:00:00Z", True),
        ("23:59:59.0123456789+23:59", True),
        ("12:34:56-00:00", True),
        ("12:34:60z", True),
        ("12:34:56", False),
        ("12:34Z", False),
        ("24:00:00Z", False),
        ("12:60:56Z", False),
        ("12:34:61Z", False),
        ("12:34:56.Z", False),
        ("12:34:56,1Z", False),
        ("12:34:56.\u0661Z", False),
        ("12:34:56+24:00", False),
        ("12:34:56-00:60", False),
        ("12:34:56+0000", False),
        ("12:34:56Z\n", False),
        (["12:34:56"], True),
    ],
)
def test_rfc3339_full_time(value, valid):
    assert is_rfc3339_time(value) is valid


@pytest.mark.parametrize(
    ("name", "value"),
    [("date", "2026-02-29"), ("date-time", "2026-11-23T12:34:56"), ("time", "12:34:56")],
)
def test_format_assertion_is_opt_in_for_all_rfc3339_formats(name, value):
    schema = {"format": name}
    annotated = from_data(value, json_schema=schema)
    asserted = from_data(value, json_schema=schema, format_assertion=True)
    assert annotated.is_valid
    assert not asserted.is_valid
    location = asserted.report.issues[0].location
    assert isinstance(location, DataLocation)
    assert location.schema_pointer == "/format"
    assert annotated.to_data() == asserted.to_data() == value
    assert from_data(42, json_schema=schema, format_assertion=True).is_valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("f81d4fae-7dec-11d0-a765-00a0c91e6bf6", True),
        ("F81D4FAE-7DEC-11D0-A765-00A0C91E6BF6", True),
        ("urn:uuid:f81d4fae-7dec-11d0-a765-00a0c91e6bf6", False),
        ("{f81d4fae-7dec-11d0-a765-00a0c91e6bf6}", False),
        ("f81d4fae7dec11d0a76500a0c91e6bf6", False),
        ("f81d4fa-e7dec-11d0-a765-00a0c91e6bf6", False),
        ("g81d4fae-7dec-11d0-a765-00a0c91e6bf6", False),
        ("+81d4fae-7dec-11d0-a765-00a0c91e6bf6", False),
        ("\u066181d4fae-7dec-11d0-a765-00a0c91e6bf6", False),
        (None, True),
    ],
)
def test_uuid_plain_hyphenated_spelling(value, valid):
    assert formats.is_uuid(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("192.0.2.1", True),
        ("192.168.001.1", False),
        ("127.1", False),
        ("999.1.1.1", False),
        (42, True),
    ],
)
def test_ipv4_dotted_decimal_spelling(value, valid):
    assert formats.is_ipv4(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("2001:db8::1", True),
        ("::ffff:192.0.2.1", True),
        ("fe80::1%eth0", False),
        ("2001:db8:::1", False),
        ([], True),
    ],
)
def test_ipv6_spelling_without_scope_id(value, valid):
    assert formats.is_ipv6(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("", True),
        ("/", True),
        ("/a~0b/~1c", True),
        ("/a/00", True),
        ("a", False),
        ("/a~2b", False),
        ("/a~", False),
        ({}, True),
    ],
)
def test_json_pointer_checks_token_syntax_not_array_indexes(value, valid):
    assert formats.is_json_pointer(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("^[a-z]+$", True),
        ("(?P<name>x)", True),
        ("[", False),
        (r"\p{L}", False),
        pytest.param("x{18446744073709551616}", False, id="repetition-overflow"),
        pytest.param("x{" + "9" * 5000 + "}", False, id="repetition-digit-limit"),
        pytest.param(
            "(" * sys.getrecursionlimit() + "x" + ")" * sys.getrecursionlimit(),
            False,
            id="excessive-nesting",
        ),
        (False, True),
    ],
)
def test_regex_uses_python_re_syntax(value, valid):
    assert formats.is_regex(value) is valid


def test_json_pointer_assertion_is_opt_in():
    schema = {"format": "json-pointer"}
    assert from_data("/a~2b", json_schema=schema).is_valid

    invalid = from_data("/a~2b", json_schema=schema, format_assertion=True)
    assert not invalid.is_valid
    assert isinstance(invalid.report.issues[0].location, DataLocation)
    assert invalid.report.issues[0].location.schema_pointer == "/format"
    assert from_data("/a/00", json_schema=schema, format_assertion=True).is_valid
