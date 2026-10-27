"""Bounded Arrow previews computed entirely in clean schema workers."""

import os
import secrets
import stat
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, JsonValue

from ..artifacts import ArtifactRef, ArtifactStore
from ..audit import AuditLogger
from ..cache import CompileRequest, SchemaCache
from ..contracts import FileRef, JobRef, Nonempty, SchemaRef, ToolEnvelope, WireModel
from ..encoding import encode_value
from ..errors import ToolError
from ..execution import capture_candidate
from ..jobs import JobManager
from ..pagination import CursorCodec
from ..snapshot_store import SnapshotStore
from ..worker_protocol import (
    ExportOperation,
    FieldSourceSpec,
    Operation,
    RecordsOperation,
    WorkerReply,
)
from ..workers import WorkerPool, snapshot_from_wire, snapshot_to_wire
from ..workspaces import Principal
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
    idempotency_key: Nonempty


def execute_records(
    operation: RecordsOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations.arrow import records
    from pyxsd.integrations.projection import FieldSource

    request = CompileRequest(
        snapshot=snapshot_from_wire(operation.snapshot),
        schema_ref=operation.schema_ref,
        hints=(),
        processor_version=processor_version,
        owner_id=operation.owner_id,
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
        snapshot=snapshot_from_wire(operation.snapshot),
        schema_ref=operation.schema_ref,
        hints=(),
        processor_version=processor_version,
        owner_id=operation.owner_id,
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
        snapshots: SnapshotStore,
        pool: WorkerPool,
        *,
        artifacts: ArtifactStore | None = None,
        cursors: CursorCodec | None = None,
        audit: AuditLogger | None = None,
        arrow: bool = True,
        jobs: JobManager | None = None,
    ) -> None:
        super().__init__(snapshots, pool, artifacts=artifacts, cursors=cursors, audit=audit)
        self.limits = snapshots.limits
        self.arrow = arrow
        self.staging = snapshots.state.root / "record-staging"
        self.staging.mkdir(mode=0o700, exist_ok=True)
        self.jobs = jobs if jobs is not None else JobManager(snapshots.workspaces, self)

    def _require_arrow(self) -> None:
        if not self.arrow:
            raise ToolError("arrow_required", "Arrow projection is not available in this build")

    async def preview_records(
        self, principal: Principal, request: PreviewRecordsRequest
    ) -> ToolEnvelope:
        self._require_arrow()
        revision = self._revision(request.document, request.schema_ref)
        async with self._snapshot(principal, request.workspace_id, revision) as snapshot:
            operation = RecordsOperation(
                request_id=secrets.token_hex(16),
                snapshot=snapshot_to_wire(snapshot),
                owner_id=principal.owner_id,
                **request.model_dump(exclude={"workspace_id"}),
            )
            reply = await self.pool.run(
                operation, deadline=time.monotonic() + self.pool.limits.compile_query_seconds
            )
            envelope = self._envelope(
                principal,
                reply,
                snapshot=snapshot,
                query_digest=self._digest(request),
                provenance=self._provenance(snapshot, request.schema_ref, request.document.path),
            )
            return envelope.model_copy(
                update={
                    "warnings": [
                        "Rows are projected, not silently flattened. XML is materialized before batching."
                    ]
                }
            )

    async def export_records(self, principal: Principal, request: ExportRecordsRequest) -> JobRef:
        principal.require("pyxsd:execute", "pyxsd:read", "pyxsd:upload")
        self._require_arrow()
        if self.artifacts is None:
            raise ToolError("invalid_argument", "Record exports require artifact retention")
        if not request.documents:
            raise ToolError("invalid_argument", "Exports require at least one document")
        if len(request.documents) > self.limits.corpus_files:
            raise ToolError("quota_exceeded", "Export corpus exceeds the operator budget")
        revisions = {ref.revision for ref in request.documents if ref.revision is not None}
        schema_revision = request.schema_ref.source.revision
        if schema_revision is not None:
            revisions.add(schema_revision)
        if len(revisions) > 1:
            raise ToolError("stale_reference", "Export references must share a revision")
        async with self._snapshot(
            principal, request.workspace_id, next(iter(revisions), None)
        ) as snapshot:
            operation = ExportOperation(
                request_id=secrets.token_hex(16),
                snapshot=snapshot_to_wire(snapshot),
                owner_id=principal.owner_id,
                schema_ref=request.schema_ref,
                documents=request.documents,
                element=request.element,
                path=request.path,
                selector=request.selector,
                namespaces=request.namespaces,
                columns=request.columns,
                format=request.format,
            )
            return await self.jobs.submit(
                principal,
                request.workspace_id,
                operation,
                idempotency_key=request.idempotency_key,
            )

    async def _retain(
        self, principal: Principal, workspace_id: str, data: bytes, media_type: str
    ) -> ArtifactRef:
        assert self.artifacts is not None

        async def chunks() -> AsyncIterator[bytes]:
            yield data

        return await self.artifacts.put(
            principal,
            workspace_id,
            chunks(),
            media_type=media_type,
            limit=len(data),
            ttl=max(1, self.limits.job_result_seconds),
        )

    async def run(self, operation: Operation, *, deadline: float) -> WorkerReply:
        """JobExecutor entry: dispatch, then capture coordinator-owned bytes."""
        if not isinstance(operation, ExportOperation):
            raise ToolError("invalid_argument", "Unsupported record operation")
        principal = Principal(operation.owner_id, frozenset({"pyxsd:read", "pyxsd:upload"}))
        workspace_id = operation.snapshot.workspace_id
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
                    principal, workspace_id, data, "application/vnd.apache.parquet"
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
                    reference = await self._retain(principal, workspace_id, data, media_type)
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
                snapshot_from_wire(operation.snapshot), operation.schema_ref
            )
            return reply.model_copy(update={"result": result})
