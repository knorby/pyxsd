"""Bounded reports retain verdicts even when issue details cannot be retained."""

import json
import re
from decimal import Decimal
from io import StringIO

import pytest

import pyxsd
from pyxsd.facets import FacetConstraints
from pyxsd.validation import IssueSeverity, ValidationIssue, ValidationReport


def test_bounded_report_keeps_error_state():
    report = ValidationReport(max_issues=1)
    report.add_warning("first", code="hint")
    report.add_error("second", code="value")
    assert [issue.code for issue in report] == ["hint"]
    assert report.has_errors
    assert report.error_count == 1
    assert report.warning_count == 1
    assert report.suppressed_count == 1
    assert str(report).splitlines()[0] == "pyxsd: 1 error, 1 warning"
    assert report.to_dict()["counts"] == {"error": 1, "warning": 1}
    assert report.to_dict()["suppressed_count"] == 1


def test_zero_detail_budget_does_not_hide_findings():
    report = ValidationReport(max_issues=0)
    assert not report
    report.add_error("bad", code="value", phase="schema")
    report.add_warning("advisory", code="hint")
    assert report
    assert len(report) == 0
    assert report.issues == report.errors == report.warnings == []
    assert report.has_errors
    assert report.suppressed_count == 2
    assert report.to_dict()["counts"] == {"error": 1, "warning": 1}


@pytest.mark.parametrize("budget", [-1, 1.5, True, "2"])
def test_invalid_report_budgets_rejected(budget):
    with pytest.raises(ValueError, match="max_issues"):
        ValidationReport(max_issues=budget)


def test_extending_bounded_reports_counts_each_finding_once():
    source = ValidationReport(max_issues=1)
    source.add_warning("hint", code="hint", phase="schema")
    source.add_error("bad", code="value")
    source.add_error("bad again", code="value")
    target = ValidationReport(max_issues=1)
    target.add_error("compile", code="schema", phase="schema")
    target.extend(source)
    assert target.error_count == 3
    assert target.warning_count == 1
    assert target.suppressed_count == 3
    assert target.for_phase("schema")[0].code == "schema"
    assert source.suppressed_count == 2
    merged = ValidationReport()
    merged.extend(target)
    assert merged.error_count == 3
    assert merged.warning_count == 1
    assert merged.suppressed_count == 3
    assert merged.issues == target.issues


def test_self_extend_uses_a_snapshot():
    report = ValidationReport(max_issues=3)
    report.add_error("bad", code="value")
    report.add_warning("hint", code="hint")
    report.extend(report)
    assert [issue.code for issue in report] == ["value", "hint", "value"]
    assert (report.error_count, report.warning_count, report.suppressed_count) == (2, 2, 1)


def test_unlimited_report_compatibility_and_json():
    issue = ValidationIssue(IssueSeverity.ERROR, "value", "bad", "count", "schema")
    assert issue.format() == "error: [value] count: bad"
    report = ValidationReport()
    report.add_error("bad", code="value", element="count", phase="schema")
    assert report.issues == [issue]
    assert report.suppressed_count == 0
    assert json.loads(report.to_json()) == {
        "issues": [
            {
                "severity": "error",
                "code": "value",
                "message": "bad",
                "element": "count",
                "phase": "schema",
                "location": None,
                "constraint": None,
                "details": None,
                "location_precision": "unknown",
            }
        ],
        "counts": {"error": 1, "warning": 0},
        "suppressed_count": 0,
    }


def test_structured_details_keep_decimal_and_occurrence_identity():
    from pyxsd.locations import ConstraintInfo, IssueLocation, PathStep

    location = IssueLocation("orders.xml", (PathStep("{https://example.org/a}order", 2),), "price")
    report = ValidationReport()
    report.add_error(
        "price too low",
        code="value",
        location=location,
        constraint=ConstraintInfo("minInclusive", Decimal("0.10"), Decimal("0.01")),
        details={"related": [location], "missing": None, "enabled": True},
        location_precision="exact",
    )
    row = json.loads(report.to_json())["issues"][0]
    assert row["location"] == {
        "source": "orders.xml",
        "path": [{"name": "{https://example.org/a}order", "index": 2}],
        "attribute": "price",
    }
    assert row["constraint"] == {"kind": "minInclusive", "expected": "0.10", "actual": "0.01"}
    assert row["location_precision"] == "exact"
    assert row["details"]["related"] == [row["location"]]
    assert row["details"]["enabled"] is True


@pytest.mark.parametrize("value", [object(), {1: "not a JSON key"}])
def test_serialization_refuses_lossy_or_unknown_details(value):
    report = ValidationReport()
    report.add_warning("diagnostic", code="hint", details={"value": value})
    with pytest.raises(TypeError):
        report.to_json()


@pytest.mark.parametrize(
    "value,expected",
    [(float("nan"), "NaN"), (float("inf"), "INF"), (float("-inf"), "-INF")],
)
def test_nonfinite_numeric_evidence_uses_canonical_xsd_strings(value, expected):
    report = ValidationReport()
    report.add_error("nonfinite", code="value", details={"value": value})
    assert json.loads(report.to_json())["issues"][0]["details"]["value"] == expected


def test_nonfinite_facet_evidence_has_deterministic_json():
    schema = pyxsd.compile(
        StringIO(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="r"><xs:simpleType><xs:restriction base="xs:double">'
            '<xs:enumeration value="1"/></xs:restriction></xs:simpleType>'
            "</xs:element></xs:schema>"
        )
    )
    document = schema.parse(StringIO("<r>NaN</r>"))
    issue = json.loads(document.report.to_json())["issues"][0]
    assert issue["constraint"]["actual"] == "NaN"


def test_arbitrarily_large_integer_evidence_does_not_hit_interpreter_digit_limit():
    report = ValidationReport()
    report.add_error("large", code="value", details={"n": 10**5000})
    value = json.loads(report.to_json())["issues"][0]["details"]["n"]
    assert value == "1" + "0" * 5000


def test_serialized_details_preserve_safe_numbers_and_large_integer_precision():
    report = ValidationReport()
    report.add_warning(
        "sample",
        code="hint",
        details={
            "small": 5,
            "large": 9007199254740992,
            "negative": -9007199254740992,
            "fraction": 0.5,
            "text": "caf\u00e9",
            "empty": [],
        },
    )
    assert json.loads(report.to_json())["issues"][0]["details"] == {
        "small": 5,
        "large": "9007199254740992",
        "negative": "-9007199254740992",
        "fraction": 0.5,
        "text": "caf\u00e9",
        "empty": [],
    }


def test_enumeration_constraint_prefers_typed_values_to_comparison_keys():
    constraints = FacetConstraints(enumerations=(("n", 1),), enumeration_values=(Decimal("1.00"),))
    with pytest.raises(TypeError) as raised:
        constraints.check(Decimal("2.00"))
    assert raised.value.constraint.expected == (Decimal("1.00"),)
    assert raised.value.constraint.actual == Decimal("2.00")


@pytest.mark.parametrize(
    "constraints,value,lexical,kind,expected,actual",
    [
        (FacetConstraints(length=2), "abc", None, "length", 2, 3),
        (FacetConstraints(min_length=2), "a", None, "minLength", 2, 1),
        (FacetConstraints(max_length=2), "abc", None, "maxLength", 2, 3),
        (FacetConstraints(min_inclusive=2), 1, None, "minInclusive", 2, 1),
        (FacetConstraints(min_exclusive=2), 2, None, "minExclusive", 2, 2),
        (FacetConstraints(max_inclusive=2), 3, None, "maxInclusive", 2, 3),
        (FacetConstraints(max_exclusive=2), 2, None, "maxExclusive", 2, 2),
        (FacetConstraints(total_digits=2), Decimal("123"), None, "totalDigits", 2, 3),
        (FacetConstraints(fraction_digits=1), Decimal("1.23"), None, "fractionDigits", 1, 2),
        (
            FacetConstraints(patterns=((re.compile("a"), re.compile("b")),)),
            "c",
            "c",
            "pattern",
            ("a", "b"),
            "c",
        ),
        (
            FacetConstraints(enumerations=(("s", "a"),)),
            "b",
            None,
            "enumeration",
            (("s", "a"),),
            "b",
        ),
        (
            FacetConstraints(explicit_timezone="required"),
            "2000-01-01",
            None,
            "explicitTimezone",
            "required",
            "2000-01-01",
        ),
        (
            FacetConstraints(explicit_timezone="prohibited"),
            "2000-01-01Z",
            None,
            "explicitTimezone",
            "prohibited",
            "2000-01-01Z",
        ),
    ],
)
def test_facet_violations_expose_typed_constraint(
    constraints, value, lexical, kind, expected, actual
):
    with pytest.raises(TypeError) as raised:
        constraints.check(value, lexical)
    violation = raised.value
    assert violation.constraint.kind == kind
    assert violation.constraint.expected == expected
    assert violation.constraint.actual == actual
    assert "violated" in str(violation)
