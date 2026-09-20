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
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

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

    def __init__(self) -> None:
        self._issues: list[ValidationIssue] = []
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
    ) -> None:
        """Record a fatal-severity issue."""
        self._issues.append(
            ValidationIssue(IssueSeverity.ERROR, code, message, element, phase or self.phase)
        )

    def add_warning(
        self,
        message: str,
        *,
        code: str,
        element: str | None = None,
        phase: str | None = None,
    ) -> None:
        """Record a recoverable-severity issue."""
        self._issues.append(
            ValidationIssue(IssueSeverity.WARNING, code, message, element, phase or self.phase)
        )

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
        self._issues.extend(other._issues)

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
        return any(i.severity is IssueSeverity.ERROR for i in self._issues)

    def __len__(self) -> int:
        return len(self._issues)

    def __bool__(self) -> bool:
        return bool(self._issues)

    def __iter__(self) -> Iterator[ValidationIssue]:
        return iter(self._issues)

    def __str__(self) -> str:
        """A multi-line rendering of the report for CLI display."""
        n_errors = len(self.errors)
        n_warnings = len(self.warnings)
        parts = [f"pyxsd: {n_errors} error{'' if n_errors == 1 else 's'}"]
        if n_warnings:
            parts.append(f"{n_warnings} warning{'' if n_warnings == 1 else 's'}")
        header = ", ".join(parts)
        lines = [header]
        lines.extend(f"  {issue.format()}" for issue in self._issues)
        return "\n".join(lines)


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
