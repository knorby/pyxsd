"""Advisory stylesheet inspection behavior.

The canonical W3C stylesheet schemas are non-normative inspection input.
These tests pin the advisory contract: resource failures are explicit, raw
schema/instance findings stay in a separate report, and no normative
``XTSE*`` result is produced by inspection.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import pytest

from pyxsd.document import Document
from pyxsd.namespaces import parse_with_namespaces
from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.schema import Schema
from pyxsd.validation import IssueSeverity, ValidationIssue, ValidationReport
from pyxsd.xslt import inspection as inspection_module
from pyxsd.xslt.errors import XsltInspectionError
from pyxsd.xslt.inspection import (
    InspectionDiagnostic,
    StylesheetInspection,
    _capability,
    _classify_issue,
    _diagnostics,
    inspect_stylesheet,
)

FIXTURES = Path(__file__).parent / "fixtures" / "inspection"
RESOURCES = Path(inspection_module.__file__).parent / "resources"

MINI_STYLESHEET_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="http://www.w3.org/1999/XSL/Transform"
           elementFormDefault="qualified">
  <xs:element name="stylesheet">
    <xs:complexType mixed="true">
      <xs:sequence>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
      <xs:anyAttribute processContents="lax"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def _remote(source: str | None) -> bool:
    return source is not None and urlparse(source).scheme in {"http", "https"}


class PackagePolicy:
    """An offline policy that records reads and counts rejected network access."""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.network_requests = 0
        self.denied: set[str] = set()

    def new_report(self) -> ValidationReport:
        return ValidationReport()

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        if _remote(source) or _remote(base):
            self.network_requests += 1
            raise ResourcePolicyError("network access is not allowed")
        path = Path(source)
        if not path.is_absolute():
            if base is None:
                raise ResourcePolicyError(f"relative resource without a base: {source}")
            path = Path(base).parent / path
        path = path.resolve()
        self.reads.append(str(path))
        if path.name in self.denied:
            raise ResourcePolicyError(f"resource denied: {path.name}")
        return ResourceData(str(path), path.read_bytes())

    def parse(self, data: ResourceData, context: Any) -> ET.Element:
        return parse_with_namespaces(io.BytesIO(data.content), context)


class BudgetPolicy(PackagePolicy):
    """A policy whose reports retain no issue details."""

    def new_report(self) -> ValidationReport:
        return ValidationReport(max_issues=0)


@pytest.fixture
def package_policy() -> PackagePolicy:
    return PackagePolicy()


@pytest.fixture
def mini_schema() -> Schema:
    return Schema.compile(io.StringIO(MINI_STYLESHEET_SCHEMA))


def _use_mini_schema(monkeypatch: pytest.MonkeyPatch, schema: Schema) -> None:
    def compile_mini(language: str, *, resource_policy: Any = None) -> Schema:
        assert language in {"2.0", "3.0"}
        return schema

    monkeypatch.setattr(inspection_module, "_compile_stylesheet_schema", compile_mini)


def test_public_xslt_package_exports_the_inspection_surface():
    from pyxsd import xslt

    assert xslt.inspect_stylesheet is inspect_stylesheet
    assert xslt.StylesheetInspection is StylesheetInspection
    assert xslt.InspectionDiagnostic is InspectionDiagnostic
    assert xslt.XsltInspectionError is XsltInspectionError


def test_inspection_is_advisory():
    result = inspect_stylesheet(FIXTURES / "literal-result.xsl")
    assert result.capability in {"bound", "unsupported"}
    if result.capability == "bound":
        assert result.document is not None
        assert result.document.root is not None
    assert not hasattr(result, "xslt_static_errors")


def test_schema_resources_are_offline(package_policy):
    inspect_stylesheet(FIXTURES / "package.xsl", resource_policy=package_policy)
    assert package_policy.network_requests == 0


def test_packaged_schema_resources_are_read_through_the_policy(package_policy):
    result = inspect_stylesheet(FIXTURES / "literal-result.xsl", resource_policy=package_policy)
    assert result.capability == "unsupported"
    assert package_policy.reads
    assert any("schema-for-xslt30.xsd" in path for path in package_policy.reads)
    assert all(Path(path).is_file() for path in package_policy.reads)


def test_inspection_reports_unsupported_without_a_bound_document():
    result = inspect_stylesheet(FIXTURES / "literal-result.xsl")
    assert result.language == "3.0"
    assert result.capability == "unsupported"
    assert result.document is None
    assert result.report.has_errors
    assert result.diagnostics
    assert result.report is not None
    assert not hasattr(result, "xslt_static_errors")
    assert not any(issue.code.upper().startswith("XTSE") for issue in result.report.issues)
    assert not any(diagnostic.code.upper().startswith("XTSE") for diagnostic in result.diagnostics)
    assert result.diagnostics[0].format().startswith("error: [unknown-elementRef] ")


def test_inspection_classifies_the_known_xslt30_findings():
    result = inspect_stylesheet(FIXTURES / "literal-result.xsl")
    assert [(item.code, item.classification) for item in result.diagnostics] == [
        ("unknown-elementRef", "pyxsd-profile-gap"),
        ("attribute-restriction", "pyxsd-validation-defect"),
        ("attribute-restriction", "pyxsd-validation-defect"),
        ("wildcard-invalid", "pyxsd-validation-defect"),
        ("wildcard-invalid", "pyxsd-validation-defect"),
        ("wildcard-invalid", "pyxsd-validation-defect"),
    ]
    assert result.report.error_count == 6
    assert all(item.rationale for item in result.diagnostics)


def test_inspection_classifies_the_known_xslt20_findings():
    result = inspect_stylesheet(FIXTURES / "literal-result.xsl", language="2.0")
    assert result.language == "2.0"
    assert result.capability == "unsupported"
    assert result.document is None
    assert [(item.code, item.classification) for item in result.diagnostics] == [
        ("unknown-elementRef", "pyxsd-profile-gap"),
        ("attribute-restriction", "pyxsd-validation-defect"),
        ("wildcard-invalid", "pyxsd-validation-defect"),
        ("wildcard-invalid", "pyxsd-validation-defect"),
    ]
    assert result.report.error_count == 4


@pytest.mark.parametrize("language", ["3.1", "1.0", "", "XSLT 3.0"])
def test_unknown_language_is_rejected(language: str):
    with pytest.raises(ValueError, match="language"):
        inspect_stylesheet(FIXTURES / "literal-result.xsl", language=language)


def test_missing_stylesheet_is_an_inspection_error(tmp_path: Path):
    with pytest.raises(XsltInspectionError, match="inspection"):
        inspect_stylesheet(tmp_path / "missing.xsl")


def test_missing_packaged_schema_is_an_inspection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(inspection_module, "_resources_root", lambda: tmp_path)
    with pytest.raises(XsltInspectionError, match="inspection"):
        inspect_stylesheet(FIXTURES / "literal-result.xsl")


def test_malformed_stylesheet_is_an_inspection_error(tmp_path: Path):
    source = tmp_path / "broken.xsl"
    source.write_text("<xsl:stylesheet", encoding="utf-8")
    with pytest.raises(XsltInspectionError, match=r"broken\.xsl"):
        inspect_stylesheet(source)


def test_malformed_stream_uses_a_memory_identifier():
    with pytest.raises(XsltInspectionError, match="<memory>"):
        inspect_stylesheet(io.StringIO("<xsl:stylesheet"))


def test_resource_policy_denial_is_an_inspection_error(package_policy: PackagePolicy):
    package_policy.denied.add("literal-result.xsl")
    with pytest.raises(XsltInspectionError, match="denied"):
        inspect_stylesheet(FIXTURES / "literal-result.xsl", resource_policy=package_policy)


def test_bound_inspection_returns_a_separate_document_and_report(
    monkeypatch: pytest.MonkeyPatch, mini_schema: Schema
):
    _use_mini_schema(monkeypatch, mini_schema)
    result = inspect_stylesheet(FIXTURES / "literal-result.xsl")
    assert result.capability == "bound"
    assert result.document is not None
    assert result.document.root is not None
    assert result.document.is_valid
    assert not result.report.has_errors
    assert result.report is not result.document.report
    assert result.report.issues == []
    assert result.diagnostics == ()


def test_missing_instance_root_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mini_schema: Schema
):
    _use_mini_schema(monkeypatch, mini_schema)
    source = tmp_path / "wrong-root.xsl"
    source.write_text("<other/>", encoding="utf-8")
    result = inspect_stylesheet(source)
    assert result.capability == "unsupported"
    assert result.document is None
    assert result.report.has_errors
    assert any(diagnostic.code == "unknown-root" for diagnostic in result.diagnostics)


def test_missing_root_without_a_report_error_is_still_unsupported(mini_schema: Schema):
    document = Document(mini_schema, None, None, ValidationReport())
    assert _capability(ValidationReport(), document) == "unsupported"


def test_budgeted_report_adds_a_truncation_diagnostic():
    result = inspect_stylesheet(FIXTURES / "literal-result.xsl", resource_policy=BudgetPolicy())
    assert result.capability == "unsupported"
    assert result.report.issues == []
    assert [item.code for item in result.diagnostics] == ["inspection-report-truncated"]


def test_inspection_does_not_mutate_the_stylesheet_or_packaged_resources():
    source = FIXTURES / "literal-result.xsl"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    resource_before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(RESOURCES.glob("*.xsd"))
    }
    inspect_stylesheet(source)
    inspect_stylesheet(source, language="2.0")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(RESOURCES.glob("*.xsd"))
    } == resource_before


@pytest.mark.parametrize(
    ("issue", "classification"),
    [
        (
            ValidationIssue(
                IssueSeverity.ERROR,
                "unknown-elementRef",
                "element reference 'xs:schema' in type 'import-schema|complexType' "
                "could not be resolved",
                "import-schema|complexType",
                "schema",
            ),
            "pyxsd-profile-gap",
        ),
        (
            ValidationIssue(
                IssueSeverity.ERROR,
                "attribute-restriction",
                "attribute-use restriction: attribute 'version' of type "
                "'transform-element-base-type' is not admitted by the attribute "
                "wildcard of its base type 'element-only-versioned-element-type'",
                phase="schema",
            ),
            "pyxsd-validation-defect",
        ),
        (
            ValidationIssue(
                IssueSeverity.ERROR,
                "wildcard-invalid",
                "restriction of type 'text-element-base-type' declares an attribute "
                "wildcard but its base type has none",
                phase="schema",
            ),
            "pyxsd-validation-defect",
        ),
        (
            ValidationIssue(
                IssueSeverity.WARNING,
                "some-schema-warning",
                "an unclassified schema warning",
                phase="schema",
            ),
            "unclassified-schema-finding",
        ),
        (
            ValidationIssue(
                IssueSeverity.ERROR,
                "unknown-root",
                "the document root matched no declaration",
                phase="instance",
            ),
            "stylesheet-finding",
        ),
    ],
)
def test_issue_classification(issue: ValidationIssue, classification: str):
    diagnostic = _classify_issue(issue)
    assert diagnostic.code == issue.code
    assert diagnostic.message == issue.message
    assert diagnostic.severity == issue.severity.value
    assert diagnostic.phase == issue.phase
    assert diagnostic.classification == classification
    assert diagnostic.rationale


def test_diagnostics_summarize_a_partially_retained_report():
    report = ValidationReport(max_issues=1)
    report.add_error("first", code="first")
    report.add_error("second", code="second")
    diagnostics = _diagnostics(report, Document(Schema.__new__(Schema), None, None, report))
    assert [item.code for item in diagnostics] == [
        "first",
        "inspection-report-truncated",
        "inspection-no-root",
    ]


def test_inspection_and_diagnostic_invariants(mini_schema: Schema):
    with pytest.raises(ValueError, match="capability"):
        StylesheetInspection(
            capability="maybe",  # type: ignore[arg-type]
            language="3.0",
            document=None,
            report=ValidationReport(),
            diagnostics=(),
        )
    with pytest.raises(ValueError, match="bound"):
        StylesheetInspection(
            capability="bound",
            language="3.0",
            document=None,
            report=ValidationReport(),
            diagnostics=(),
        )
    document = Document(mini_schema, None, None, ValidationReport())
    with pytest.raises(ValueError, match="unsupported"):
        StylesheetInspection(
            capability="unsupported",
            language="3.0",
            document=document,
            report=ValidationReport(),
            diagnostics=(),
        )
    result = StylesheetInspection(
        capability="unsupported",
        language="3.0",
        document=None,
        report=ValidationReport(),
        diagnostics=[],  # type: ignore[arg-type]
    )
    assert result.diagnostics == ()
    for field, value in (
        ("code", ""),
        ("classification", "made-up"),
        ("severity", "fatal"),
        ("phase", "elsewhere"),
    ):
        values = {
            "code": "unknown-root",
            "message": "message",
            "classification": "stylesheet-finding",
            "rationale": "rationale",
            "severity": "error",
            "phase": "schema",
        }
        values[field] = value
        with pytest.raises(ValueError, match=field):
            InspectionDiagnostic(**values)  # type: ignore[arg-type]
