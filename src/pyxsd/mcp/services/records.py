"""Bounded Arrow previews computed entirely in clean schema workers."""

import os
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, JsonValue

from ..cache import CompileRequest, SchemaCache
from ..contracts import FileRef, Nonempty, SchemaRef, ToolEnvelope, WireModel
from ..encoding import encode_value
from ..errors import ToolError
from ..execution import capture_candidate
from ..inputs import SessionInputs
from ..output_store import OutputRef, OutputStore
from ..worker_protocol import (
    ExportOperation,
    FieldSourceSpec,
    RecordsOperation,
    WorkerReply,
)
from ..workers import WorkerPool, capture_from_wire, capture_to_wire
from .exploration import ExplorationService


class PreviewRecordsRequest(WireModel):
    workspace_id: Nonempty
    schema_ref: SchemaRef
    document: FileRef
    element: Nonempty
    path: tuple[Nonempty, ...] = ()
    selector: str | None = None
    namespaces: dict[str, str] = Field(default_factory=dict)
    columns: dict[str, FieldSourceSpec] | None = None
    exact_count: bool = True


class ExportRecordsRequest(WireModel):
    workspace_id: Nonempty
    schema_ref: SchemaRef
    documents: tuple[FileRef, ...]
    element: Nonempty
    path: tuple[Nonempty, ...] = ()
    selector: str | None = None
    namespaces: dict[str, str] = Field(default_factory=dict)
    columns: dict[str, FieldSourceSpec] | None = None
    format: Literal["parquet", "dataset"] = "parquet"


def execute_records(
    operation: RecordsOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations.arrow import records
    from pyxsd.integrations.projection import FieldSource

    request = CompileRequest(
        capture=capture_from_wire(operation.capture),
        schema_ref=operation.schema_ref,
        hints=(),
        processor_version=processor_version,
    )
    try:
        with cache.acquire(request) as schema:
            document = schema.parse("/workspace/" + operation.document.path)
            if document.report.error_count or document.root is None:
                raise ToolError("invalid_document", "Record projection requires valid XML")
            columns = (
                None
                if operation.columns is None
                else {
                    name: FieldSource(**source.model_dump())
                    for name, source in operation.columns.items()
                }
            )
            projection = records(
                schema, element=operation.element, path=operation.path, columns=columns
            )
            rows: list[JsonValue] = []
            count = 0
            cap = min(20, cache.limits.preview_rows)
            complete = True
            for batch in projection.batches(
                document,
                selector=operation.selector,
                namespaces=operation.namespaces,
                batch_size=cap,
            ):
                count += batch.num_rows
                for row in batch.to_pylist()[: max(0, cap - len(rows))]:
                    rows.append(encode_value(row))
                if not operation.exact_count and count > cap:
                    complete = False
                    break
            return {
                "rows": rows,
                "row_count": count,
                "count_complete": complete,
                "truncated": count > len(rows),
                "declaration_path": list(operation.path),
                "selector": operation.selector,
            }
    except IntegrationError as exc:
        raise ToolError("unsupported_projection", str(exc)[: cache.limits.message_bytes]) from exc


def execute_export(
    operation: ExportOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    """Write Parquet output inside the coordinator-provided private directory.

    The worker only produces bytes on disk; the coordinator re-reads, hashes and
    retains them, so no verdict or digest here is trusted.
    """
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations.arrow import records
    from pyxsd.integrations.projection import FieldSource

    if not operation.documents:
        raise ToolError("invalid_argument", "Exports require at least one document")
    if operation.format == "parquet" and len(operation.documents) != 1:
        raise ToolError("invalid_argument", "Parquet export accepts exactly one document")
    request = CompileRequest(
        capture=capture_from_wire(operation.capture),
        schema_ref=operation.schema_ref,
        hints=(),
        processor_version=processor_version,
    )
    try:
        with cache.acquire(request) as schema:
            documents = []
            for reference in operation.documents:
                document = schema.parse("/workspace/" + reference.path)
                if document.report.error_count or document.root is None:
                    raise ToolError(
                        "invalid_document",
                        "Exports require valid XML",
                        details={"path": reference.path},
                    )
                documents.append(document)
            columns = (
                None
                if operation.columns is None
                else {
                    name: FieldSource(**source.model_dump())
                    for name, source in operation.columns.items()
                }
            )
            projection = records(
                schema, element=operation.element, path=operation.path, columns=columns
            )
            output = Path(operation.output_directory)
            row_count = 0
            if operation.format == "parquet":
                for batch in projection.batches(
                    documents[0],
                    selector=operation.selector,
                    namespaces=operation.namespaces,
                ):
                    row_count += batch.num_rows
                projection.write_parquet(
                    documents[0],
                    output / "export.parquet",
                    selector=operation.selector,
                    namespaces=operation.namespaces,
                    revalidate=False,
                )
            else:
                from pyxsd.batch import DocumentSource

                row_count = sum(
                    batch.num_rows
                    for document in documents
                    for batch in projection.batches(
                        document,
                        selector=operation.selector,
                        namespaces=operation.namespaces,
                    )
                )
                projection.write_dataset(
                    [
                        DocumentSource(id=ref.path, path="/workspace/" + ref.path)
                        for ref in operation.documents
                    ],
                    output / "export-dataset",
                    selector=operation.selector,
                    namespaces=operation.namespaces,
                    errors="raise",
                )
            return {"format": operation.format, "row_count": row_count}
    except IntegrationError as exc:
        raise ToolError("unsupported_projection", str(exc)[: cache.limits.message_bytes]) from exc


class RecordService(ExplorationService):
    def __init__(
        self,
        inputs: SessionInputs,
        pool: WorkerPool,
        *,
        outputs: OutputStore,
        arrow: bool = True,
    ) -> None:
        super().__init__(inputs, pool, outputs=outputs)
        self.limits = inputs.limits
        self.arrow = arrow
        self.outputs = outputs
        self.staging = outputs.storage.scratch / "record-staging"
        self.staging.mkdir(mode=0o700, exist_ok=True)

    def _require_arrow(self) -> None:
        if not self.arrow:
            raise ToolError("arrow_required", "Arrow projection is not available in this build")

    async def preview_records(self, request: PreviewRecordsRequest) -> ToolEnvelope:
        self._require_arrow()
        async with self._capture(request.workspace_id) as capture:
            operation = RecordsOperation(
                request_id=secrets.token_hex(16),
                capture=capture_to_wire(capture),
                **request.model_dump(exclude={"workspace_id"}),
            )
            reply = await self.pool.run(
                operation, deadline=time.monotonic() + self.pool.limits.compile_query_seconds
            )
            envelope = self._envelope(
                reply,
                result_id=self._result_id(request, capture),
                provenance=self._provenance(capture, request.schema_ref, request.document.path),
            )
            return envelope.model_copy(
                update={
                    "warnings": [
                        "Rows are projected, not silently flattened. XML is materialized before batching."
                    ]
                }
            )

    async def export_records(self, request: ExportRecordsRequest) -> dict[str, JsonValue]:
        self._require_arrow()
        if not request.documents:
            raise ToolError("invalid_argument", "Exports require at least one document")
        if len(request.documents) > self.limits.corpus_files:
            raise ToolError("quota_exceeded", "Export corpus exceeds the operator budget")
        async with self._capture(request.workspace_id) as capture:
            operation = ExportOperation(
                request_id=secrets.token_hex(16),
                capture=capture_to_wire(capture),
                schema_ref=request.schema_ref,
                documents=request.documents,
                element=request.element,
                path=request.path,
                selector=request.selector,
                namespaces=request.namespaces,
                columns=request.columns,
                format=request.format,
            )
            deadline = time.monotonic() + self.limits.batch_seconds
            reply = await self._export(operation, deadline=deadline)
        if reply.status == "error":
            assert reply.fault is not None
            raise ToolError(reply.fault.code, reply.fault.message)
        if not isinstance(reply.result, dict):
            raise ToolError("ipc_protocol", "Export returned no result object")
        return reply.result

    async def _retain(self, data: bytes, media_type: str, *, name: str) -> OutputRef:
        assert self.outputs is not None
        return await self.outputs.put_bytes(
            data, name=name.rsplit("/", 1)[-1], media_type=media_type, kind="export"
        )

    async def _export(self, operation: ExportOperation, *, deadline: float) -> WorkerReply:
        """Run the export, then capture coordinator-owned bytes."""
        with tempfile.TemporaryDirectory(prefix="export-", dir=self.staging) as directory:
            root = Path(directory)
            dispatched = operation.model_copy(update={"output_directory": str(root)})
            reply = await self.pool.run(dispatched, deadline=deadline)
            if reply.status == "error" or not isinstance(reply.result, dict):
                return reply
            artifacts: list[dict[str, JsonValue]] = []
            if operation.format == "parquet":
                directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    data = capture_candidate(
                        directory_fd, self.limits.output_bytes, name="export.parquet"
                    )
                finally:
                    os.close(directory_fd)
                reference = await self._retain(
                    data, "application/vnd.apache.parquet", name="export.parquet"
                )
                artifacts.append(
                    {
                        "path": "export.parquet",
                        "id": reference.id,
                        "digest": reference.digest,
                        "size": reference.size,
                        "media_type": reference.media_type,
                    }
                )
            else:
                remaining = self.limits.output_bytes
                base = root / "export-dataset"
                for path in sorted(base.rglob("*")):
                    if path.is_dir():
                        continue
                    metadata = path.lstat()
                    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                        raise ToolError("unsafe_candidate", "Dataset output must be regular files")
                    if metadata.st_size > remaining:
                        raise ToolError("output_limit", "Dataset exceeds its output budget")
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    try:
                        with os.fdopen(fd, "rb", closefd=False) as stream:
                            data = stream.read(remaining + 1)
                    finally:
                        os.close(fd)
                    if len(data) > remaining:
                        raise ToolError("output_limit", "Dataset exceeds its output budget")
                    remaining -= len(data)
                    media_type = (
                        "application/json"
                        if path.suffix == ".json"
                        else "application/vnd.apache.parquet"
                    )
                    reference = await self._retain(data, media_type, name=path.name)
                    artifacts.append(
                        {
                            "path": (Path("export-dataset") / path.relative_to(base)).as_posix(),
                            "id": reference.id,
                            "digest": reference.digest,
                            "size": reference.size,
                            "media_type": reference.media_type,
                        }
                    )
            result: dict[str, JsonValue] = dict(reply.result)
            result["artifacts"] = cast(JsonValue, artifacts)
            result["manifest"] = cast(JsonValue, list(artifacts))
            result["source_documents"] = [ref.path for ref in operation.documents]
            result["schema_fingerprint"] = self._fingerprint(
                capture_from_wire(operation.capture), operation.schema_ref
            )
            return reply.model_copy(update={"result": result})
