"""Failures that prevent a schemaless document from being built."""

from __future__ import annotations

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation
from pyxsd.validation import ValidationReport


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
