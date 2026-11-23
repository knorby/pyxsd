"""Failures that prevent a schemaless document from being built."""

from __future__ import annotations

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.validation import ValidationReport


class JsonParseError(PyXSDError):
    """Malformed, ambiguous, or non-native JSON input or output.

    ``line``, ``column``, and ``position`` carry the decoder's one-based text
    position and zero-based character offset when the failure came from JSON
    text; value-model failures (duplicate keys, non-finite numbers, unsupported
    host objects) leave them ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        position: int | None = None,
    ) -> None:
        super().__init__(message)
        self.line = line
        self.column = column
        self.position = position


class JsonLimitError(ResourcePolicyError):
    """Native JSON input exceeded one of its structural or byte budgets."""


class SchemalessParseError(PyXSDError):
    """Malformed XML with a report, exact location, and open expanded names."""

    def __init__(
        self,
        message: str,
        *,
        location: IssueLocation,
        open_elements: tuple[str, ...],
    ) -> None:
        self.location = location
        self.open_elements = open_elements
        self.report = ValidationReport(diagnostics_complete=True)
        self.report.add_error(
            message, code="xml-malformed", location=location, location_precision="exact"
        )
        super().__init__(self.report.issues[0].format())
