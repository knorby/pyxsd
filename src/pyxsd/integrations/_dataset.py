"""Private Arrow dataset orchestration; public records have no Arrow imports."""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from pyxsd import __version__
from pyxsd.batch import BatchParseError, DocumentSource, ParseOutcome
from pyxsd.schema import Schema

from . import IntegrationError
from .dataset import DatasetExportError, DatasetResult

if TYPE_CHECKING:
    from .arrow import RecordProjection

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


def _write_part(
    projection: RecordProjection,
    outcome: ParseOutcome,
    ordinal: int,
    staging: Path,
    schema: Any,
    *,
    selector: str | None,
    namespaces: dict[str, str] | None,
    batch_size: int,
    errors: Literal["raise", "report"],
) -> dict[str, Any]:
    assert outcome.document is not None
    outcome.document.require_valid()
    relative = f"parts/part-{ordinal:06d}.parquet"
    path = staging / relative
    rows = 0
    failure = None
    batches = projection.batches(
        outcome.document, selector=selector, namespaces=namespaces, batch_size=batch_size
    )
    try:
        with pq.ParquetWriter(str(path), schema, version="2.6") as writer:
            while True:
                try:
                    batch = next(batches)
                except StopIteration:
                    break
                except IntegrationError as exc:
                    if errors == "raise":
                        raise DatasetExportError(
                            str(exc), source_id=outcome.source.id, stage="projection"
                        ) from exc
                    failure = {"kind": "projection", "message": str(exc)}
                    break
                augmented = pa.RecordBatch.from_arrays(
                    [
                        *batch.columns,
                        pa.array([outcome.source.id] * batch.num_rows, type=pa.string()),
                        pa.array(range(rows + 1, rows + batch.num_rows + 1), type=pa.int64()),
                    ],
                    schema=schema,
                )
                writer.write_batch(augmented)
                rows += batch.num_rows
    finally:
        close = getattr(batches, "close", None)
        if close is not None:
            close()
    if failure is not None:
        path.unlink()
        return entry_snapshot(outcome, ordinal, "projection_error", 0, None, failure=failure)
    return entry_snapshot(outcome, ordinal, "written" if rows else "empty", rows, relative)


def _metadata_snapshot(metadata: dict[bytes, bytes] | None) -> list[dict[str, str]]:
    # Base64 preserves arbitrary byte metadata without coercing live objects.
    return [
        {
            "key_base64": base64.b64encode(k).decode("ascii"),
            "value_base64": base64.b64encode(v).decode("ascii"),
        }
        for k, v in (metadata or {}).items()
    ]


def _manifest(
    projection: RecordProjection,
    schema: Schema,
    result: DatasetResult,
    entries: list[dict[str, Any]],
    selector: str | None,
    namespaces: dict[str, str] | None,
) -> dict[str, Any]:
    return {
        "format_version": 1,
        "status": result.status,
        "versions": {"pyxsd": __version__, "pyarrow": pa.__version__},
        "xsd_version": str(schema._host.processor_version),
        "parse_policy": asdict(schema.mode),
        "projection": {
            "row_route": json.loads(projection.schema.metadata[b"pyxsd:declaration"]),
            "schema_metadata": _metadata_snapshot(projection.schema.metadata),
            "fields": [
                {"name": f.name, "metadata": _metadata_snapshot(f.metadata)}
                for f in projection.schema
            ],
        },
        "selector": selector,
        "namespaces": namespaces,
        "schema_file": "_schema.parquet",
        "counts": {
            "inputs": result.inputs,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "rows": result.rows,
        },
        "entries": entries,
    }


def write_dataset(
    projection: RecordProjection,
    schema: Schema,
    sources: Iterable[DocumentSource],
    destination: Path,
    *,
    selector: str | None,
    namespaces: dict[str, str] | None,
    batch_size: int,
    errors: Literal["raise", "report"],
) -> DatasetResult:
    """Stream one private part per successful source and publish completed metadata."""
    if errors not in ("raise", "report"):
        raise IntegrationError("errors must be 'raise' or 'report'")
    if type(batch_size) is not int or batch_size <= 0:
        raise IntegrationError("batch_size must be a positive integer")
    fixed = augmented_schema(projection.schema)
    schema.require_valid()
    if os.path.lexists(destination):
        raise DatasetExportError("destination already exists", stage="configuration")
    staging = None
    source_id = None
    stage = "staging"
    outcomes = schema.iter_parse(sources, errors=errors)
    entries = []
    try:
        staging = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.pyxsd-", dir=destination.parent)
        )
        (staging / "parts").mkdir()
        ordinal = 0
        while True:
            stage = "parse"
            source_id = None
            try:
                outcome = next(outcomes)
            except StopIteration:
                break
            except BatchParseError as exc:
                raise DatasetExportError(
                    str(exc), source_id=exc.outcome.source.id, stage=exc.outcome.status
                ) from exc
            ordinal += 1
            source_id = outcome.source.id
            stage = "part"
            if outcome.status != "valid":
                entries.append(entry_snapshot(outcome, ordinal, outcome.status, 0, None))
            else:
                entries.append(
                    _write_part(
                        projection,
                        outcome,
                        ordinal,
                        staging,
                        fixed,
                        selector=selector,
                        namespaces=namespaces,
                        batch_size=batch_size,
                        errors=errors,
                    )
                )
            del outcome
        source_id = None
        succeeded = sum(e["part"] is not None for e in entries)
        failed = len(entries) - succeeded
        status: Literal["complete", "partial", "failed"] = (
            "complete" if not failed else "partial" if succeeded else "failed"
        )
        result = DatasetResult(
            destination,
            status,
            len(entries),
            succeeded,
            failed,
            sum(e["rows"] for e in entries),
            destination / "manifest.json",
        )
        stage = "schema"
        with pq.ParquetWriter(str(staging / "_schema.parquet"), fixed, version="2.6"):
            pass
        stage = "manifest"
        with (staging / "manifest.json").open("w", encoding="utf-8") as file:
            json.dump(
                _manifest(projection, schema, result, entries, selector, namespaces),
                file,
                ensure_ascii=True,
                allow_nan=False,
                indent=2,
            )
            file.write("\n")
        stage = "publish"
        if os.path.lexists(destination):
            raise FileExistsError("destination already exists")
        os.rename(staging, destination)
        staging = None
        return result
    except DatasetExportError:
        raise
    except Exception as exc:
        raise DatasetExportError(str(exc), source_id=source_id, stage=stage) from exc
    finally:
        close = getattr(outcomes, "close", None)
        if close is not None:
            close()
        if staging is not None:
            shutil.rmtree(staging)
