"""Structured validation reporting.

While an XML instance is bound to the classes generated from a schema,
recoverable problems (missing attributes, wrong element order, facet
violations) are collected into a :class:`ValidationReport` instead of
being printed immediately. The CLI prints the report to standard error
after the run; library users inspect the ``report`` of the
:class:`~pyxsd.document.Document` (a merged view of the owning
schema's compilation issues and the document's own binding issues)
or :attr:`~pyxsd.schema.Schema.report` for the schema phase alone.
"""

from __future__ import annotations

import enum
import json
import logging
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from pyxsd.locations import ConstraintInfo, IssueLocation, LocationPrecision, PathStep

if TYPE_CHECKING:
    from pyxsd.schema_base import SchemaBase
    from pyxsd.schema_context import SchemaContext

logger = logging.getLogger("pyxsd")


class CompileContextProtocol(Protocol):
    """The slice of the compilation context the schema checks read.

    Annotation-only structural view of
    :class:`~pyxsd.schema_composition.CompositionContext`, which
    satisfies it without inheriting from it: the declaration and
    derivation checks in :mod:`pyxsd.schema_checks` accept this
    protocol, so they depend on the attributes they actually touch
    rather than on the concrete pipeline context.

    Members (beyond the four the brief names) are exactly the
    attributes the checks functions read.
    """

    report: ValidationReport
    mode: Any
    classes: dict[str, type[SchemaBase]]
    schema_context: SchemaContext
    namespace_context: Any
    directive_ids: dict[str, Any]
    composed_schema_roots: dict[int, Any]
    composed_schema_namespaces: dict[int, str | None]
    composed_element_ids: set[int]
    composed_target_namespaces: set[str]
    resolved_imports: set[str]


class IssueSeverity(enum.Enum):
    """How serious a validation issue is."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class ValidationIssue:
    """A single problem found while validating a document.

    - ``severity``: the :class:`IssueSeverity` of the issue.

    - ``code``: a short, stable, machine-readable tag describing the
      kind of problem (for example ``"missing-attribute"`` or
      ``"order"``). Codes are documented in the validation guide and
      are safe to match on in tooling.

    - ``message``: a human-readable, single-line description.

    - ``element``: the name of the element or type the issue was found
      in, when known.

    - ``phase``: the pipeline stage that produced the issue, either
      ``"schema"`` (reading the XSD, composing included schemas, building
      the generated classes) or ``"instance"`` (binding the XML document).
      Tooling and the conformance runner use this to check each stage
      independently.
    """

    severity: IssueSeverity
    code: str
    message: str
    element: str | None = None
    phase: str = "instance"
    location: IssueLocation | None = field(default=None, kw_only=True)
    constraint: ConstraintInfo | None = field(default=None, kw_only=True)
    details: dict[str, object] | None = field(default=None, kw_only=True)
    location_precision: LocationPrecision = field(default="unknown", kw_only=True)

    def format(self) -> str:
        """Render the issue as one line, as shown by the CLI."""
        prefix = f"{self.severity.value}: [{self.code}]"
        if self.element is not None:
            return f"{prefix} {self.element}: {self.message}"
        return f"{prefix} {self.message}"


class ValidationReport:
    """An ordered collection of validation issues.

    A schema owns the report of its compilation; each document parsed
    from it gets a fresh instance report, and the document's merged
    report presents both phases in order. The binding machinery fills
    the reports in as it walks the schema and instance documents.
    """

    def __init__(self, *, max_issues: int | None = None) -> None:
        if max_issues is not None and (type(max_issues) is not int or max_issues < 0):
            raise ValueError("max_issues must be a non-negative integer or None")
        self._max_issues = max_issues
        self._issues: list[ValidationIssue] = []
        self._error_count = 0
        self._warning_count = 0
        #: The phase new issues are attributed to unless one is passed
        #: explicitly. The parser flips this between schema compilation
        #: and instance binding.
        self.phase: str = "instance"

    def add_error(
        self,
        message: str,
        *,
        code: str,
        element: str | None = None,
        phase: str | None = None,
        location: IssueLocation | None = None,
        constraint: ConstraintInfo | None = None,
        details: dict[str, object] | None = None,
        location_precision: LocationPrecision = "unknown",
    ) -> None:
        """Record a fatal-severity issue."""
        self._error_count += 1
        self._retain(
            ValidationIssue(
                IssueSeverity.ERROR,
                code,
                message,
                element,
                phase or self.phase,
                location=location,
                constraint=constraint,
                details=details,
                location_precision=location_precision,
            )
        )

    def add_warning(
        self,
        message: str,
        *,
        code: str,
        element: str | None = None,
        phase: str | None = None,
        location: IssueLocation | None = None,
        constraint: ConstraintInfo | None = None,
        details: dict[str, object] | None = None,
        location_precision: LocationPrecision = "unknown",
    ) -> None:
        """Record a recoverable-severity issue."""
        self._warning_count += 1
        self._retain(
            ValidationIssue(
                IssueSeverity.WARNING,
                code,
                message,
                element,
                phase or self.phase,
                location=location,
                constraint=constraint,
                details=details,
                location_precision=location_precision,
            )
        )

    def _retain(self, issue: ValidationIssue) -> None:
        if self._max_issues is None or len(self._issues) < self._max_issues:
            self._issues.append(issue)

    def for_phase(self, phase: str) -> list[ValidationIssue]:
        """Only the issues attributed to *phase* (``"schema"``/``"instance"``)."""
        return [i for i in self._issues if i.phase == phase]

    def extend(self, other: ValidationReport) -> None:
        """Appends every issue from *other* to this report.

        Used to merge the phase reports of one run into a single
        collection: :meth:`pyxsd.schema.Schema.parse` merges the
        schema-phase and instance-phase issues into the document's
        report, and a caller adopting the findings of a
        :meth:`pyxsd.document.Document.revalidate` run merges that
        document's report the same way. The issue objects are shared,
        not copied; the other report is left unchanged.
        """
        # Snapshot both before extending: self-extension is supported too.
        issues = tuple(other._issues)
        errors, warnings = other.error_count, other.warning_count
        for issue in issues:
            self._retain(issue)
        self._error_count += errors
        self._warning_count += warnings

    @property
    def error_count(self) -> int:
        """All errors, including those whose details exceeded the budget."""
        return self._error_count

    @property
    def warning_count(self) -> int:
        """All warnings, including those whose details exceeded the budget."""
        return self._warning_count

    @property
    def suppressed_count(self) -> int:
        """Findings whose details were not retained (including merged reports)."""
        return self.error_count + self.warning_count - len(self._issues)

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe details and complete counts; decimals remain strings.

        Unsupported detail values raise TypeError rather than falling back to a
        potentially lossy or executable object's string representation.
        """
        return {
            "issues": [
                {
                    "severity": issue.severity.value,
                    "code": issue.code,
                    "message": issue.message,
                    "element": issue.element,
                    "phase": issue.phase,
                    "location": _diagnostic_value(issue.location),
                    "constraint": _diagnostic_value(issue.constraint),
                    "details": _diagnostic_value(issue.details),
                    "location_precision": issue.location_precision,
                }
                for issue in self._issues
            ],
            "counts": {"error": self.error_count, "warning": self.warning_count},
            "suppressed_count": self.suppressed_count,
        }

    def to_json(self) -> str:
        """Serialize :meth:`to_dict` deterministically without optional packages."""
        return json.dumps(self.to_dict(), ensure_ascii=True, allow_nan=False)

    @property
    def issues(self) -> list[ValidationIssue]:
        """All recorded issues, in the order they were found."""
        return list(self._issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        """Only the error-severity issues."""
        return [i for i in self._issues if i.severity is IssueSeverity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """Only the warning-severity issues."""
        return [i for i in self._issues if i.severity is IssueSeverity.WARNING]

    @property
    def has_errors(self) -> bool:
        """Whether any error-severity issue was recorded."""
        return self.error_count > 0

    def __len__(self) -> int:
        return len(self._issues)

    def __bool__(self) -> bool:
        return self.error_count + self.warning_count > 0

    def __iter__(self) -> Iterator[ValidationIssue]:
        return iter(self._issues)

    def __str__(self) -> str:
        """A multi-line rendering of the report for CLI display."""
        n_errors = self.error_count
        n_warnings = self.warning_count
        parts = [f"pyxsd: {n_errors} error{'' if n_errors == 1 else 's'}"]
        if n_warnings:
            parts.append(f"{n_warnings} warning{'' if n_warnings == 1 else 's'}")
        header = ", ".join(parts)
        lines = [header]
        lines.extend(f"  {issue.format()}" for issue in self._issues)
        return "\n".join(lines)


def _diagnostic_value(value: object) -> object:
    """Encode only explicitly supported evidence, never arbitrary dataclasses."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > 2**53 - 1 else value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, PathStep):
        return {"name": value.name, "index": value.index}
    if isinstance(value, IssueLocation):
        return {
            "source": value.source,
            "path": [_diagnostic_value(step) for step in value.path],
            "attribute": value.attribute,
        }
    if isinstance(value, ConstraintInfo):
        return {
            "kind": value.kind,
            "expected": _diagnostic_value(value.expected),
            "actual": _diagnostic_value(value.actual),
        }
    if isinstance(value, (tuple, list)):
        return [_diagnostic_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _diagnostic_value(item) for key, item in value.items()}
    raise TypeError(f"unsupported diagnostic value: {type(value).__name__}")


def report_or_log(
    report: ValidationReport | None,
    severity: IssueSeverity,
    *,
    code: str,
    message: str,
    element: str | None = None,
    phase: str | None = None,
) -> None:
    """Records an issue on *report*, or logs it when *report* is ``None``.

    The shared sink for validation diagnostics that may run with or
    without an owning report attached (hand-written overlay classes,
    representatives built in isolation). With a report, *severity*
    selects :meth:`ValidationReport.add_error` /
    :meth:`ValidationReport.add_warning`; without one, the issue is
    logged on the ``pyxsd`` logger at the matching level instead.

    - ``report``: the report to record on, or ``None`` to log.
    - ``severity``: the :class:`IssueSeverity` of the issue.
    - ``code``: the machine-readable issue code.
    - ``message``: the human-readable description.
    - ``element``: the element or type the issue was found in, when known.
    - ``phase``: the pipeline stage to attribute the issue to; ``None``
      keeps the report's own default phase.
    """
    if report is None:
        logger.log(
            logging.ERROR if severity is IssueSeverity.ERROR else logging.WARNING,
            "%s[%s] %s",
            element or "pyxsd",
            code,
            message,
        )
        return
    if severity is IssueSeverity.ERROR:
        report.add_error(message, code=code, element=element, phase=phase)
    else:
        report.add_warning(message, code=code, element=element, phase=phase)
