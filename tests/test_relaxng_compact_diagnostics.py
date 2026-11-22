"""Compact-source spans, structured syntax errors, and issue formatting."""

from __future__ import annotations

import dataclasses
import json

import pytest

import pyxsd.relaxng
from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, PathStep, SourceTextSpan
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError
from pyxsd.validation import IssueSeverity, ValidationIssue, ValidationReport

HEAD_SPAN = SourceTextSpan(
    start=0,
    end=6,
    line=1,
    column=1,
    end_line=1,
    end_column=7,
)
SECOND_LINE_SPAN = SourceTextSpan(
    start=20,
    end=25,
    line=2,
    column=4,
    end_line=2,
    end_column=9,
)
COMPACT_PATH = (PathStep("start", 1),)


def test_compact_location_serializes_its_half_open_span() -> None:
    report = ValidationReport()
    report.add_error(
        "unexpected token",
        code="relaxng-compact-token",
        element="start",
        location=IssueLocation("schema.rnc", COMPACT_PATH, span=HEAD_SPAN),
        location_precision="exact",
    )

    payload = json.loads(report.to_json())
    location = payload["issues"][0]["location"]

    assert location == {
        "source": "schema.rnc",
        "path": [{"name": "start", "index": 1}],
        "attribute": None,
        "span": {
            "start": 0,
            "end": 6,
            "line": 1,
            "column": 1,
            "end_line": 1,
            "end_column": 7,
        },
    }
    assert report.to_json() == report.to_json()


def test_legacy_location_json_omits_the_span_key() -> None:
    report = ValidationReport()
    report.add_error(
        "required attribute missing",
        code="missing-attribute",
        location=IssueLocation("instance.xml", COMPACT_PATH),
        location_precision="container",
    )

    location = json.loads(report.to_json())["issues"][0]["location"]

    assert location == {
        "source": "instance.xml",
        "path": [{"name": "start", "index": 1}],
        "attribute": None,
    }
    assert "span" not in location


def test_spanned_issue_formatting_includes_source_line_and_column() -> None:
    issue = ValidationIssue(
        IssueSeverity.ERROR,
        "relaxng-compact-token",
        "unexpected token",
        element="start",
        location=IssueLocation("schema.rnc", COMPACT_PATH, span=SECOND_LINE_SPAN),
    )

    rendered = issue.format()

    assert "schema.rnc:2:4" in rendered
    assert rendered.endswith("start: unexpected token")


def test_span_without_a_source_formats_only_line_and_column() -> None:
    issue = ValidationIssue(
        IssueSeverity.WARNING,
        "relaxng-compact-warning",
        "reserved prefix",
        location=IssueLocation(None, COMPACT_PATH, span=SECOND_LINE_SPAN),
    )

    rendered = issue.format()

    assert "2:4" in rendered
    assert "None" not in rendered


def test_unspanned_issue_formatting_is_unchanged() -> None:
    issue = ValidationIssue(
        IssueSeverity.ERROR,
        "missing-attribute",
        "required attribute missing",
        element="start",
        location=IssueLocation("instance.xml", COMPACT_PATH),
    )

    assert issue.format() == "error: [missing-attribute] start: required attribute missing"


def test_span_evidence_participates_in_location_equality() -> None:
    first = IssueLocation("schema.rnc", COMPACT_PATH, span=SECOND_LINE_SPAN)
    same = IssueLocation(
        "schema.rnc",
        COMPACT_PATH,
        span=SourceTextSpan(
            start=20,
            end=25,
            line=2,
            column=4,
            end_line=2,
            end_column=9,
        ),
    )
    other = IssueLocation("schema.rnc", COMPACT_PATH, span=HEAD_SPAN)

    assert first == same
    assert first != other
    assert hash(first) == hash(same)


def test_source_text_span_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        HEAD_SPAN.start = 1  # type: ignore[misc]


def test_compact_syntax_error_retains_its_evidence() -> None:
    location = IssueLocation("schema.rnc", COMPACT_PATH, span=SECOND_LINE_SPAN)

    error = RelaxNGCompactSyntaxError(
        "unexpected token",
        location=location,
        found="'}'",
        expected=("'|='", "';'", "'&='"),
    )

    assert isinstance(error, PyXSDError)
    assert error.location is location
    assert error.found == "'}'"
    assert error.expected == ("'&='", "';'", "'|='")
    assert str(error).startswith("schema.rnc:2:4: ")
    assert "unexpected token" in str(error)
    assert "found '}'" in str(error)
    assert "expected '&=', ';', '|='" in str(error)


def test_compact_syntax_error_without_evidence_keeps_a_plain_message() -> None:
    error = RelaxNGCompactSyntaxError("unterminated literal")

    assert error.location is None
    assert error.found is None
    assert error.expected == ()
    assert str(error) == "unterminated literal"


def test_compact_syntax_error_location_without_a_span_is_not_fabricated() -> None:
    error = RelaxNGCompactSyntaxError(
        "unbound prefix",
        location=IssueLocation("schema.rnc", COMPACT_PATH),
        found="'missing'",
    )

    assert str(error) == "unbound prefix; found 'missing'"


def test_compact_syntax_error_span_without_a_source_keeps_the_position() -> None:
    error = RelaxNGCompactSyntaxError(
        "unexpected token",
        location=IssueLocation(None, COMPACT_PATH, span=SECOND_LINE_SPAN),
    )

    assert str(error).startswith("2:4: ")


def test_compact_syntax_error_is_exported_from_the_package() -> None:
    assert pyxsd.relaxng.RelaxNGCompactSyntaxError is RelaxNGCompactSyntaxError
