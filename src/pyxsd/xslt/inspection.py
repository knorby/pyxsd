"""Advisory, non-normative XSLT stylesheet inspection.

The inspection front end compiles a packaged W3C stylesheet schema with the
current pyxsd XSD compiler and binds one stylesheet module against it.  It is a
structural view only:

* the canonical stylesheet schemas are inspection input, never the definition
  of XSLT validity;
* findings are kept in a separate :class:`~pyxsd.validation.ValidationReport`
  and classified diagnostics, never translated into normative ``XTSE*`` errors;
* the stylesheet source tree and packaged resources are not modified, and no
  XSD defaults are installed into any future compiler input;
* an unsupported profile reports its limitations honestly instead of hiding
  findings or weakening validation.

All packaged schema locations are local.  Inspection performs no runtime
network retrieval.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import as_file
from importlib.resources import files as resource_files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import IO, Literal

from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError
from pyxsd.resource_policy import ResourcePolicy, new_validation_report, source_identifier
from pyxsd.schema import Schema
from pyxsd.validation import IssueSeverity, ValidationIssue, ValidationReport
from pyxsd.xslt.errors import XsltInspectionError

#: The two inspection outcomes.  ``bound`` means a faithful binding was
#: produced; ``unsupported`` means the current XSD compiler could not bind the
#: profile without errors.
StylesheetCapability = Literal["bound", "unsupported"]

#: Why an advisory finding does not imply an invalid stylesheet.
DiagnosticClassification = Literal[
    "pyxsd-profile-gap",
    "pyxsd-validation-defect",
    "stylesheet-finding",
    "unclassified-schema-finding",
]

CAPABILITIES: frozenset[str] = frozenset({"bound", "unsupported"})
CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        "pyxsd-profile-gap",
        "pyxsd-validation-defect",
        "stylesheet-finding",
        "unclassified-schema-finding",
    }
)
SEVERITIES: frozenset[str] = frozenset({"error", "warning"})
PHASES: frozenset[str] = frozenset({"schema", "instance", "inspection"})


@dataclass(frozen=True)
class InspectionDiagnostic:
    """One classified, advisory inspection finding.

    ``code`` is the underlying pyxsd/XSD diagnostic code.  It is never a
    normative XSLT error code.
    """

    code: str
    message: str
    classification: DiagnosticClassification
    rationale: str
    severity: str = "error"
    phase: str = "schema"
    element: str | None = None

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("code must not be empty")
        if self.classification not in CLASSIFICATIONS:
            raise ValueError(f"unknown classification: {self.classification!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity!r}")
        if self.phase not in PHASES:
            raise ValueError(f"unknown phase: {self.phase!r}")

    def format(self) -> str:
        """Render the diagnostic as one advisory line."""
        return f"{self.severity}: [{self.code}] {self.message}"


@dataclass(frozen=True)
class StylesheetInspection:
    """The result of advisory stylesheet inspection.

    ``capability`` describes the inspection profile, not stylesheet validity.
    A ``bound`` result carries a :class:`~pyxsd.document.Document`; an
    ``unsupported`` result carries no document and explains itself through
    ``report`` and ``diagnostics``.
    """

    capability: StylesheetCapability
    language: str
    document: Document | None
    report: ValidationReport
    diagnostics: tuple[InspectionDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES:
            raise ValueError(f"unknown capability: {self.capability!r}")
        if self.capability == "bound" and self.document is None:
            raise ValueError("a bound inspection requires a bound document")
        if self.capability == "unsupported" and self.document is not None:
            raise ValueError("an unsupported inspection cannot carry a bound document")
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


@dataclass(frozen=True)
class _LanguageProfile:
    language: str
    schema_file: str
    xsd_version: Literal["1.0", "1.1"]


_PROFILES: Mapping[str, _LanguageProfile] = {
    "2.0": _LanguageProfile("2.0", "schema-for-xslt20.xsd", "1.0"),
    "3.0": _LanguageProfile("3.0", "schema-for-xslt30.xsd", "1.1"),
}


def _profile(language: str) -> _LanguageProfile:
    try:
        return _PROFILES[language]
    except KeyError:
        supported = ", ".join(sorted(_PROFILES))
        raise ValueError(
            f"unsupported stylesheet language {language!r}; expected one of {supported}"
        ) from None


def _resources_root() -> Traversable:
    """The packaged stylesheet-schema resource directory."""
    return resource_files("pyxsd.xslt") / "resources"


@contextmanager
def _schema_resource(filename: str) -> Iterator[Path]:
    """Yield a filesystem path for one packaged schema resource."""
    with as_file(_resources_root() / filename) as path:
        yield Path(path)


def _compile_stylesheet_schema(
    language: str, *, resource_policy: ResourcePolicy | None = None
) -> Schema:
    """Compile the packaged stylesheet schema for *language*.

    The caller policy, when present, governs every read, including these local
    packaged resources, exactly as it governs an ordinary pyxsd compilation.
    """
    profile = _profile(language)
    with _schema_resource(profile.schema_file) as schema_path:
        return Schema.compile(
            schema_path,
            xsd_version=profile.xsd_version,
            resource_policy=resource_policy,
        )


def _classify_issue(issue: ValidationIssue) -> InspectionDiagnostic:
    """Classify one raw schema or instance finding as advisory."""
    classification: DiagnosticClassification
    rationale: str
    if issue.phase != "schema":
        classification = "stylesheet-finding"
        rationale = (
            "the stylesheet instance did not bind cleanly; this is an "
            "advisory structural finding only"
        )
    elif issue.code == "unknown-elementRef" and "xs:schema" in issue.message:
        classification = "pyxsd-profile-gap"
        rationale = (
            "pyxsd does not compose the schema-for-schemas global element "
            "declarations when the XML Schema namespace is imported; the "
            "XSLT schema's reference to xs:schema therefore cannot resolve. "
            "The official schema is not at fault."
        )
    elif issue.code == "attribute-restriction" and (
        "not admitted by the attribute wildcard" in issue.message
    ):
        classification = "pyxsd-validation-defect"
        rationale = (
            "the official schema reproduces this attribute use through its "
            "extension chain, but the current pyxsd attribute-restriction "
            "check does not inherit base uses through a restriction. The "
            "finding contradicts the schema."
        )
    elif issue.code == "wildcard-invalid" and (
        "declares an attribute wildcard but its base type has none" in issue.message
    ):
        classification = "pyxsd-validation-defect"
        rationale = (
            "the official schema inherits this attribute wildcard from "
            "generic-element-type through an extension chain, but the "
            "current pyxsd wildcard-restriction check reads only the direct "
            "base type's own wildcard. The finding contradicts the schema."
        )
    else:
        classification = "unclassified-schema-finding"
        rationale = (
            "the current probe has not classified this schema finding; it "
            "remains visible and blocks a bound capability"
        )
    return InspectionDiagnostic(
        code=issue.code,
        message=issue.message,
        classification=classification,
        rationale=rationale,
        severity=issue.severity.value,
        phase=issue.phase,
        element=issue.element,
    )


def _diagnostics(report: ValidationReport, document: Document) -> tuple[InspectionDiagnostic, ...]:
    """Classify retained findings and account for suppressed or missing ones."""
    diagnostics = [_classify_issue(issue) for issue in report.issues]
    retained_errors = sum(1 for issue in report.issues if issue.severity is IssueSeverity.ERROR)
    if report.error_count > retained_errors:
        diagnostics.append(
            InspectionDiagnostic(
                code="inspection-report-truncated",
                message=(
                    f"{report.error_count - retained_errors} inspection error(s) "
                    "were suppressed by the report budget"
                ),
                classification="unclassified-schema-finding",
                rationale=(
                    "the resource policy's report budget removed issue details; "
                    "the error count still makes the inspection unsupported"
                ),
                phase="inspection",
            )
        )
    if document.root is None and not any(item.code == "unknown-root" for item in diagnostics):
        diagnostics.append(
            InspectionDiagnostic(
                code="inspection-no-root",
                message="the stylesheet did not bind to a root element",
                classification="stylesheet-finding",
                rationale=(
                    "the stylesheet root matched no declaration in the "
                    "advisory schema, so no bound document can be produced"
                ),
                phase="instance",
            )
        )
    return tuple(diagnostics)


def _capability(report: ValidationReport, document: Document) -> StylesheetCapability:
    """Bound only when both the schema and the stylesheet bind without errors."""
    if report.has_errors or document.root is None:
        return "unsupported"
    return "bound"


def inspect_stylesheet(
    source: str | os.PathLike[str] | IO[str],
    *,
    language: str = "3.0",
    resource_policy: ResourcePolicy | None = None,
) -> StylesheetInspection:
    """Bind one stylesheet module as an advisory, non-normative structural view.

    ``source`` is a path or readable stream.  ``language`` selects the
    packaged XSLT 2.0 or 3.0 schema (default ``"3.0"``).  A supplied
    ``resource_policy`` governs every read, including the packaged local
    schemas; a policy that denies them makes inspection fail.

    Resource and input failures raise :class:`XsltInspectionError`.  Schema
    and binding findings never raise and never become normative ``XTSE*``
    errors; they are reported on the returned inspection together with a
    classified diagnostic for each finding.
    """
    profile = _profile(language)
    try:
        schema = _compile_stylesheet_schema(profile.language, resource_policy=resource_policy)
        document = schema.parse(source)
    except (PyXSDError, OSError) as exc:
        label = source_identifier(source) or "<memory>"
        raise XsltInspectionError(f"stylesheet inspection failed for {label}: {exc}") from exc

    report = new_validation_report(resource_policy)
    report.extend(document.report)
    capability = _capability(report, document)
    return StylesheetInspection(
        capability=capability,
        language=profile.language,
        document=document if capability == "bound" else None,
        report=report,
        diagnostics=_diagnostics(report, document),
    )
