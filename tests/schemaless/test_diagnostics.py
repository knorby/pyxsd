"""Behavior of JSON data locations in shared diagnostics."""

import dataclasses
import json

import pytest

from pyxsd.locations import (
    DataLocation,
    IssueLocation,
    PathStep,
    SourceTextSpan,
)
from pyxsd.validation import IssueSeverity, ValidationIssue, ValidationReport


def test_data_location_formats_pointer_without_fake_xml_path():
    location = DataLocation("in.json", "/a/0", schema_pointer="/items/type")
    issue = ValidationIssue(IssueSeverity.ERROR, "json-schema", "invalid", location=location)
    assert issue.location.path == ()
    assert "/a/0" in issue.format()
    report = ValidationReport()
    report.add_error("invalid", code="json-schema", location=location)
    payload = report.to_dict()["issues"][0]["location"]
    assert payload["pointer"] == "/a/0"
    assert payload["schema_pointer"] == "/items/type"


def test_data_location_has_no_xml_path_attribute_or_mutable_state():
    location = DataLocation("in.json", "/a")
    assert location.path == ()
    assert location.attribute is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        location.pointer = "/b"  # type: ignore[misc]


def test_data_location_format_uses_a_source_and_pointer_fragment():
    location = DataLocation("in.json", "/a/0")
    issue = ValidationIssue(IssueSeverity.ERROR, "json-schema", "invalid", location=location)
    assert issue.format() == "error: [json-schema] in.json#/a/0: invalid"

    anonymous = DataLocation(None, "/a")
    anonymous_issue = ValidationIssue(
        IssueSeverity.WARNING, "json-schema", "suspect", location=anonymous
    )
    assert anonymous_issue.format() == "warning: [json-schema] #/a: suspect"


def test_data_location_root_pointer_formats_as_the_whole_document():
    issue = ValidationIssue(
        IssueSeverity.ERROR,
        "json-schema",
        "invalid",
        element="root",
        location=DataLocation("in.json", ""),
    )
    assert issue.format() == "error: [json-schema] in.json#: root: invalid"


def test_data_location_pointer_evidence_wins_over_a_span():
    location = DataLocation("in.json", "/a", span=SourceTextSpan(0, 1, 1, 1, 1, 2))
    issue = ValidationIssue(IssueSeverity.ERROR, "json-schema", "invalid", location=location)
    assert issue.format() == "error: [json-schema] in.json#/a: invalid"


def test_data_location_dict_retains_pointer_fields_and_span():
    location = DataLocation(
        "in.json", "/a", schema_pointer="/properties/a/type", span=SourceTextSpan(0, 1, 1, 1, 1, 2)
    )
    report = ValidationReport()
    report.add_error("invalid", code="json-schema", location=location)
    payload = report.to_dict()["issues"][0]["location"]
    assert payload == {
        "source": "in.json",
        "path": [],
        "attribute": None,
        "pointer": "/a",
        "schema_pointer": "/properties/a/type",
        "span": {
            "start": 0,
            "end": 1,
            "line": 1,
            "column": 1,
            "end_line": 1,
            "end_column": 2,
        },
    }


def test_data_location_without_schema_pointer_retains_a_null_field():
    report = ValidationReport()
    report.add_error("invalid", code="json-schema", location=DataLocation("in.json", ""))
    payload = report.to_dict()["issues"][0]["location"]
    assert payload["pointer"] == ""
    assert payload["schema_pointer"] is None
    assert "span" not in payload

    decoded = json.loads(report.to_json())
    assert decoded["issues"][0]["location"]["pointer"] == ""


def test_xml_location_diagnostic_shape_is_unchanged():
    location = IssueLocation("in.xml", (PathStep("r", 1),))
    issue = ValidationIssue(IssueSeverity.ERROR, "order", "invalid", location=location)
    assert issue.format() == "error: [order] invalid"

    report = ValidationReport()
    report.add_error("invalid", code="order", location=location)
    payload = report.to_dict()["issues"][0]["location"]
    assert payload == {"source": "in.xml", "path": [{"name": "r", "index": 1}], "attribute": None}
    assert "pointer" not in payload
    assert "schema_pointer" not in payload


def test_data_location_accepts_an_issue_location_where_exact_identity_does_not_matter():
    location: IssueLocation = DataLocation("in.json", "/a")
    assert isinstance(location, IssueLocation)
    assert location == DataLocation("in.json", "/a")
