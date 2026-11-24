"""RFC 3339 assertions are deterministic with the plain JSON Schema extra."""

import pytest
from jsonschema import Draft202012Validator

from pyxsd.schemaless import from_data
from pyxsd.schemaless.formats import (
    assertion_checker,
    is_rfc3339_date,
    is_rfc3339_date_time,
    is_rfc3339_time,
)


def test_assertion_checker_preserves_other_draft_formats_without_mutating_the_draft():
    translated = {"date", "date-time", "time"}
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


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="2026 is not a Gregorian leap year")
def test_rfc3339_checker_owns_date_time_and_time_boundaries():
    assert is_rfc3339_date("2026-11-23")
    assert is_rfc3339_date_time("2026-02-29T00:00:00Z")
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
    assert asserted.report.issues[0].location.schema_pointer == "/format"
    assert annotated.to_data() == asserted.to_data() == value
    assert from_data(42, json_schema=schema, format_assertion=True).is_valid
