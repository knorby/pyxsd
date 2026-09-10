"""Structured validation reporting.

While an XML instance is bound to the classes generated from a schema,
recoverable problems (missing attributes, wrong element order, facet
violations) are collected into a :class:`ValidationReport` instead of
being printed immediately. The CLI prints the report to standard error
after the run; library users inspect the ``report`` attribute of the
:class:`pyxsd.parser.PyXSD` object.
"""

import enum
from dataclasses import dataclass


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
    """

    severity: IssueSeverity
    code: str
    message: str
    element: str | None = None

    def format(self) -> str:
        """Render the issue as one line, as shown by the CLI."""
        prefix = f"{self.severity.value}: [{self.code}]"
        if self.element is not None:
            return f"{prefix} {self.element}: {self.message}"
        return f"{prefix} {self.message}"


class ValidationReport:
    """An ordered collection of validation issues.

    Reports are owned by a :class:`~pyxsd.parser.PyXSD` run and filled
    in by the binding machinery as it walks the instance document.
    """

    def __init__(self) -> None:
        self._issues: list[ValidationIssue] = []

    def add_error(self, message: str, *, code: str, element: str | None = None) -> None:
        """Record a fatal-severity issue."""
        self._issues.append(ValidationIssue(IssueSeverity.ERROR, code, message, element))

    def add_warning(self, message: str, *, code: str, element: str | None = None) -> None:
        """Record a recoverable-severity issue."""
        self._issues.append(ValidationIssue(IssueSeverity.WARNING, code, message, element))

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

    def __iter__(self):
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
