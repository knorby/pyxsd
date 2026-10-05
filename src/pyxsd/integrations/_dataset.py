"""Private Arrow dataset orchestration; public records have no Arrow imports."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from pyxsd.batch import ParseOutcome

from . import IntegrationError

SOURCE_ID = "__pyxsd_source_id"
ROW_INDEX = "__pyxsd_row_index"


def augmented_schema(schema: Any) -> Any:
    """Append reserved non-null provenance fields without changing projection metadata."""
    if {SOURCE_ID, ROW_INDEX}.intersection(schema.names):
        raise IntegrationError("projection uses a reserved dataset provenance column")
    return schema.append(pa.field(SOURCE_ID, pa.string(), nullable=False)).append(
        pa.field(ROW_INDEX, pa.int64(), nullable=False)
    )


def entry_snapshot(
    outcome: ParseOutcome,
    ordinal: int,
    status: str,
    rows: int,
    part: str | None,
    *,
    failure: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Copy only JSON-safe diagnostics, never documents, paths, or live exceptions."""
    if failure is None and outcome.error is not None:
        failure = {"kind": outcome.error.kind, "message": outcome.error.message}
    return {
        "source_id": outcome.source.id,
        "ordinal": ordinal,
        "status": status,
        "rows": rows,
        "part": part,
        "issues": [
            {
                "severity": issue.severity.value,
                "code": issue.code,
                "message": issue.message,
                "element": issue.element,
                "phase": issue.phase,
            }
            for issue in outcome.issues
        ],
        "failure": failure,
    }
