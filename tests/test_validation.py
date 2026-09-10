"""Unit tests for the validation reporting machinery and new enums."""

import pytest

from pyxsd.compositors import Compositor
from pyxsd.exceptions import PyXSDError, PyXSDWarning
from pyxsd.validation import (
    IssueSeverity,
    ValidationIssue,
    ValidationReport,
)


class TestIssueSeverity:
    def test_values(self):
        assert IssueSeverity.ERROR.value == "error"
        assert IssueSeverity.WARNING.value == "warning"


class TestValidationIssue:
    def test_format_without_element(self):
        issue = ValidationIssue(IssueSeverity.ERROR, "order", "expected 'quantity' elsewhere")
        assert issue.format() == "error: [order] expected 'quantity' elsewhere"

    def test_format_with_element(self):
        issue = ValidationIssue(
            IssueSeverity.WARNING, "schema", "bad minOccurs", element="itemType"
        )
        assert issue.format() == "warning: [schema] itemType: bad minOccurs"

    def test_issues_are_immutable(self):
        import dataclasses

        issue = ValidationIssue(IssueSeverity.ERROR, "order", "msg")
        with pytest.raises(dataclasses.FrozenInstanceError):
            issue.code = "other"


class TestValidationReport:
    def test_starts_empty(self):
        report = ValidationReport()
        assert len(report) == 0
        assert not report
        assert not report.has_errors
        assert report.issues == []

    def test_add_error(self):
        report = ValidationReport()
        report.add_error("boom", code="order", element="itemType")
        assert len(report) == 1
        assert report.has_errors
        issue = report.issues[0]
        assert issue.severity is IssueSeverity.ERROR
        assert issue.code == "order"
        assert issue.element == "itemType"

    def test_add_warning(self):
        report = ValidationReport()
        report.add_warning("careful", code="schema")
        assert report
        assert not report.has_errors
        assert report.warnings and not report.errors

    def test_errors_and_warnings_partition(self):
        report = ValidationReport()
        report.add_error("one", code="a")
        report.add_warning("two", code="b")
        report.add_error("three", code="c")
        assert [i.message for i in report.errors] == ["one", "three"]
        assert [i.message for i in report.warnings] == ["two"]
        assert len(report) == 3

    def test_iteration_yields_issues_in_order(self):
        report = ValidationReport()
        report.add_error("first", code="a")
        report.add_warning("second", code="b")
        assert [i.message for i in report] == ["first", "second"]

    def test_str_renders_header_and_lines(self):
        report = ValidationReport()
        report.add_error("boom", code="order", element="itemType")
        report.add_warning("careful", code="schema")
        rendered = str(report)
        assert rendered.splitlines()[0] == "pyxsd: 1 error, 1 warning"
        assert "  error: [order] itemType: boom" in rendered
        assert "  warning: [schema] careful" in rendered

    def test_str_pluralization(self):
        report = ValidationReport()
        report.add_error("a", code="x")
        report.add_error("b", code="y")
        assert str(report).splitlines()[0] == "pyxsd: 2 errors"


class TestExceptions:
    def test_hierarchy(self):
        assert issubclass(PyXSDError, Exception)
        assert issubclass(PyXSDWarning, UserWarning)


class TestCompositor:
    def test_members_compare_equal_to_lexical_names(self):
        assert Compositor.SEQUENCE == "sequence"
        assert Compositor.CHOICE == "choice"
        assert Compositor.ALL == "all"

    def test_lookup_from_tag_type(self):
        assert Compositor("sequence") is Compositor.SEQUENCE
        with pytest.raises(ValueError):
            Compositor("bogus")


class TestReportIntegration:
    def test_valid_fixtures_produce_empty_reports(self, tmp_path):
        from conftest import ALL_FIXTURES, run_parser

        for fixture in ALL_FIXTURES:
            parser = run_parser(fixture)
            assert not parser.report, fixture

    def test_invalid_attribute_value_is_reported(self):
        """An attribute failing its type conversion lands on the report."""
        from conftest import run_parser

        parser = run_parser("primitives")
        root = parser.parseXML()
        descriptor = vars(type(root))["active"]
        descriptor.__set__(root, "maybe")  # not a valid boolean lexical form
        codes = [i.code for i in parser.report]
        assert "invalid-attribute" in codes
