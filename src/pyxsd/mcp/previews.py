"""Durable previews retain bytes and validate outside the execution process."""

import difflib
import hashlib
import json
import secrets
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue

from .artifacts import ArtifactStore
from .contracts import ArtifactRef, FileRef, JobRef, Nonempty, SchemaRef, WireModel
from .errors import ToolError
from .execution import TrustedLocalExecution
from .jobs import JobManager
from .safe_io import SafeDirectory
from .snapshot_store import SnapshotStore
from .snapshots import Snapshot, SnapshotFile
from .transforms import PipelineTransformSpec, TransformSpec, normalize_transform, resolve_sources
from .worker_protocol import Operation, ParseOperation, StructuralDiffOperation, WorkerReply
from .workers import WorkerPool, snapshot_from_wire, snapshot_to_wire
from .workspaces import Principal


class PreviewTransformRequest(WireModel):
    workspace_id: Nonempty
    document: FileRef
    source_schema: SchemaRef
    target_schema: SchemaRef | None = None
    transform: TransformSpec
    idempotency_key: Nonempty


class PreviewCheckpoint(WireModel):
    candidate: ArtifactRef
    report: ArtifactRef
    valid: bool
    well_formed: bool


class PreviewRecord(WireModel):
    id: Nonempty
    workspace_id: Nonempty
    revision: Nonempty
    input_digest: str
    schema_digest: str
    code_digest: str
    original: ArtifactRef
    candidate: ArtifactRef
    report: ArtifactRef
    diff: ArtifactRef
    structural_diff: ArtifactRef
    valid: bool
    well_formed: bool
    warnings: list[str]
    expires_at: float
    publication_state: Literal["available"] = "available"
    checkpoints: tuple[PreviewCheckpoint, ...] = ()


class PreviewOperation(Operation):
    kind: Literal["preview"] = "preview"
    role: Literal["execution"] = "execution"
    document: FileRef
    source_schema: SchemaRef
    target_schema: SchemaRef
    transform: TransformSpec
    sources: dict[str, str]


class PreviewService:
    def __init__(
        self,
        snapshots: SnapshotStore,
        pool: WorkerPool,
        artifacts: ArtifactStore,
        *,
        backend: str = "disabled",
        transport: str = "stdio",
        registry: dict[str, str] | None = None,
        jobs: JobManager | None = None,
    ) -> None:
        self.snapshots, self.pool, self.artifacts = snapshots, pool, artifacts
        self.limits = snapshots.limits
        self.backend, self.transport = backend, transport
        self.registry = dict(registry or {})
        self.staging = snapshots.state.root / "preview-staging"
        self.staging.mkdir(mode=0o700, exist_ok=True)
        self.execution = TrustedLocalExecution(self.staging, self.limits)
        self.jobs = JobManager(snapshots.workspaces, self) if jobs is None else jobs

    async def submit(self, principal: Principal, request: PreviewTransformRequest) -> JobRef:
        principal.require("pyxsd:execute", "pyxsd:read", "pyxsd:upload")
        if self.backend != "trusted-local" or self.transport != "stdio":
            raise ToolError("execution_unavailable", "No supported execution backend configured")
        spec = normalize_transform(request.transform, self.limits)
        sources = (
            resolve_sources(spec, self.registry, self.limits)
            if isinstance(spec, PipelineTransformSpec)
            else {}
        )
        refs = [request.document, request.source_schema.source]
        if request.target_schema is not None:
            refs.append(request.target_schema.source)
        revisions = {ref.revision for ref in refs if ref.revision is not None}
        if len(revisions) > 1:
            raise ToolError("stale_reference", "Preview references must share a revision")
        async with self.snapshots.workspaces.lease(principal, request.workspace_id):
            snapshot = await self.snapshots.get(
                principal, request.workspace_id, revision=next(iter(revisions), None)
            )
            operation = PreviewOperation(
                request_id=secrets.token_hex(16),
                owner_id=principal.owner_id,
                snapshot=snapshot_to_wire(snapshot),
                document=request.document,
                source_schema=request.source_schema,
                target_schema=request.target_schema or request.source_schema,
                transform=spec,
                sources=sources,
            )
            return await self.jobs.submit(
                principal, request.workspace_id, operation, idempotency_key=request.idempotency_key
            )

    async def get(self, principal: Principal, preview_id: str) -> PreviewRecord:
        result = await self.jobs.status(principal, preview_id)
        if result.state != "succeeded":
            raise ToolError("preview_unavailable", "Preview has no retained result")
        record = PreviewRecord.model_validate_json(json.dumps(result.result))
        if record.expires_at <= time.time():
            raise ToolError("preview_expired", "Preview has expired")
        return record

    async def _put(
        self, principal: Principal, workspace: str, content: bytes, media: str
    ) -> ArtifactRef:
        async def chunks() -> AsyncIterator[bytes]:
            yield content

        return await self.artifacts.put(
            principal,
            workspace,
            chunks(),
            media_type=media,
            limit=self.limits.output_bytes,
            ttl=min(self.limits.preview_seconds, self.limits.job_result_seconds),
        )

    async def run(self, operation: Operation, *, deadline: float) -> WorkerReply:
        if not isinstance(operation, PreviewOperation):
            raise ToolError("invalid_argument", "Unsupported preview operation")
        principal = Principal(
            operation.owner_id, frozenset({"pyxsd:read", "pyxsd:upload", "pyxsd:execute"})
        )
        snapshot = snapshot_from_wire(operation.snapshot)
        # Preserve inputs before user code runs. Local execution has launching-user
        # privileges; it must not supply the schema bytes used for the final verdict.
        with SafeDirectory(snapshot.root) as source:
            originals = {
                item.path: source.read(item.path, max_bytes=self.limits.xml_file_bytes).content
                for item in snapshot.files
            }
        if any(
            hashlib.sha256(originals[item.path]).hexdigest() != item.digest
            for item in snapshot.files
        ):
            raise ToolError("source_changed", "Preview input differs from its manifest")
        original = originals[operation.document.path]
        original_ref = await self._put(
            principal, snapshot.workspace_id, original, "application/xml"
        )
        with tempfile.TemporaryDirectory(prefix="preview-", dir=self.staging) as directory:
            root = Path(directory)
            self._materialize(root, originals)
            isolated_input = replace(snapshot, root=root)
            candidate = await self.execution.run(
                isolated_input,
                operation.source_schema,
                operation.document,
                operation.transform,
                registry=operation.sources,
                deadline=deadline,
            )
        valid, well_formed, report = await self._validate(
            operation, snapshot, originals, candidate.content, operation.target_schema, deadline
        )
        structural: JsonValue = (
            await self._compare(operation, snapshot, originals, candidate.content, deadline)
            if well_formed
            else {"available": False, "reason": "Candidate is not well-formed XML"}
        )
        checkpoints = []
        for content in candidate.checkpoint_candidates:
            checkpoint_valid, checkpoint_formed, checkpoint_report = await self._validate(
                operation, snapshot, originals, content, operation.source_schema, deadline
            )
            checkpoints.append(
                PreviewCheckpoint(
                    candidate=await self._put(
                        principal, snapshot.workspace_id, content, "application/xml"
                    ),
                    report=await self._put(
                        principal,
                        snapshot.workspace_id,
                        json.dumps(checkpoint_report, separators=(",", ":")).encode(),
                        "application/json",
                    ),
                    valid=checkpoint_valid,
                    well_formed=checkpoint_formed,
                )
            )
        return await self._record(
            operation,
            snapshot,
            principal,
            original,
            original_ref,
            candidate.content,
            valid,
            well_formed,
            report,
            tuple(checkpoints),
            structural,
        )

    async def _compare(
        self,
        operation: PreviewOperation,
        snapshot: Snapshot,
        originals: dict[str, bytes],
        content: bytes,
        deadline: float,
    ) -> JsonValue:
        with tempfile.TemporaryDirectory(prefix="comparison-", dir=self.staging) as directory:
            root = Path(directory)
            self._materialize(root, originals)
            name = "candidate-" + secrets.token_hex(16) + ".xml"
            (root / name).write_bytes(content)
            comparison = replace(
                snapshot,
                root=root,
                files=(
                    *snapshot.files,
                    SnapshotFile(name, hashlib.sha256(content).hexdigest(), len(content)),
                ),
            )
            reply = await self.pool.run(
                StructuralDiffOperation(
                    request_id=secrets.token_hex(16),
                    owner_id=operation.owner_id,
                    snapshot=snapshot_to_wire(comparison),
                    original=operation.document,
                    candidate=FileRef(path=name),
                    limit=self.limits.page_items,
                ),
                deadline=deadline,
            )
        if reply.status == "error":
            assert reply.fault is not None
            raise ToolError(reply.fault.code, reply.fault.message)
        return reply.result

    async def _validate(
        self,
        operation: PreviewOperation,
        snapshot: Snapshot,
        originals: dict[str, bytes],
        content: bytes,
        schema_ref: SchemaRef,
        deadline: float,
    ) -> tuple[bool, bool, JsonValue]:
        # Never reuse a directory the executed code could have changed. Build
        # the validation input only after termination, from retained bytes.
        with tempfile.TemporaryDirectory(prefix="validation-", dir=self.staging) as directory:
            root = Path(directory)
            self._materialize(root, originals)
            candidate_path = "candidate-" + secrets.token_hex(16) + ".xml"
            (root / candidate_path).write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            validation_snapshot = replace(
                snapshot,
                root=root,
                files=(
                    *snapshot.files,
                    SnapshotFile(candidate_path, digest, len(content)),
                ),
            )
            validation = await self.pool.run(
                ParseOperation(
                    request_id=secrets.token_hex(16),
                    snapshot=snapshot_to_wire(validation_snapshot),
                    owner_id=operation.owner_id,
                    schema_ref=schema_ref,
                    document=FileRef(path=candidate_path),
                    issue_limit=self.limits.page_items,
                ),
                deadline=deadline,
            )
        well_formed = validation.status == "ok"
        data = validation.result if isinstance(validation.result, dict) else {}
        report = data.get("report", {})
        valid = (
            well_formed
            and isinstance(report, dict)
            and isinstance(report.get("counts"), dict)
            and cast(dict, report["counts"]).get("error") == 0
        )
        if validation.status == "error":
            assert validation.fault is not None
            if validation.fault.code not in {"invalid_xml", "invalid_schema"}:
                raise ToolError(validation.fault.code, validation.fault.message)
            report = {"fault": validation.fault.model_dump(mode="json")}
        return bool(valid), well_formed, report

    async def _record(
        self,
        operation: PreviewOperation,
        snapshot: Snapshot,
        principal: Principal,
        original: bytes,
        original_ref: ArtifactRef,
        content: bytes,
        valid: bool,
        well_formed: bool,
        report: JsonValue,
        checkpoints: tuple[PreviewCheckpoint, ...],
        structural: JsonValue,
    ) -> WorkerReply:
        candidate_ref = await self._put(
            principal, snapshot.workspace_id, content, "application/xml"
        )
        report_ref = await self._put(
            principal,
            snapshot.workspace_id,
            json.dumps(report, separators=(",", ":")).encode(),
            "application/json",
        )
        diff = "".join(
            difflib.unified_diff(
                original.decode("utf-8", errors="replace").splitlines(keepends=True),
                content.decode("utf-8", errors="replace").splitlines(keepends=True),
                fromfile="original",
                tofile="candidate",
            )
        ).encode()
        diff_ref = await self._put(principal, snapshot.workspace_id, diff, "text/x-diff")
        structural_ref = await self._put(
            principal,
            snapshot.workspace_id,
            json.dumps(structural, separators=(",", ":")).encode(),
            "application/json",
        )
        code = (
            operation.transform.source
            if hasattr(operation.transform, "source")
            else json.dumps(
                {
                    "recipe": operation.transform.model_dump(mode="json"),
                    "sources": operation.sources,
                },
                sort_keys=True,
            )
        )
        record = PreviewRecord(
            id=operation.request_id,
            workspace_id=snapshot.workspace_id,
            revision=snapshot.revision,
            input_digest=hashlib.sha256(original).hexdigest(),
            schema_digest=hashlib.sha256(
                json.dumps(
                    {
                        "files": [(item.path, item.digest) for item in snapshot.files],
                        "schema": operation.target_schema.model_dump(mode="json"),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            code_digest=hashlib.sha256(code.encode()).hexdigest(),
            original=original_ref,
            candidate=candidate_ref,
            report=report_ref,
            diff=diff_ref,
            structural_diff=structural_ref,
            checkpoints=checkpoints,
            valid=bool(valid),
            well_formed=well_formed,
            warnings=[
                "Serialization may alter prefixes, layout, mixed-content presentation and comments; the byte diff includes serialization-only changes."
            ],
            expires_at=min(
                ref.expires_at
                for ref in (
                    original_ref,
                    candidate_ref,
                    report_ref,
                    diff_ref,
                    structural_ref,
                    *(
                        ref
                        for checkpoint in checkpoints
                        for ref in (checkpoint.candidate, checkpoint.report)
                    ),
                )
            ),
        )
        return WorkerReply(
            request_id=operation.request_id,
            role="execution",
            status="ok",
            result=record.model_dump(mode="json"),
        )

    @staticmethod
    def _materialize(root: Path, files: dict[str, bytes]) -> None:
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
