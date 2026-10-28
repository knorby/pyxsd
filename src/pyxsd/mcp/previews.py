"""Preview outputs retain exact candidate bytes and validate outside execution."""

import difflib
import hashlib
import json
import secrets
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue

from .captures import CaptureFile, InputCapture
from .contracts import ArtifactRef, FileRef, Nonempty, SchemaRef, WireModel
from .errors import ToolError
from .execution import TrustedLocalExecution
from .inputs import SessionInputs
from .output_store import OutputRef, OutputStore
from .safe_io import SafeDirectory
from .transforms import PipelineTransformSpec, TransformSpec, normalize_transform, resolve_sources
from .worker_protocol import Operation, ParseOperation, StructuralDiffOperation
from .workers import WorkerPool, capture_to_wire


def _artifact(reference: OutputRef) -> ArtifactRef:
    return ArtifactRef(
        id=reference.id,
        path=str(reference.path),
        media_type=reference.media_type,
        size=reference.size,
        digest=reference.digest,
        created_at=reference.created_at,
        kind=reference.kind,
    )


class PreviewTransformRequest(WireModel):
    workspace_id: Nonempty
    document: FileRef
    source_schema: SchemaRef
    target_schema: SchemaRef | None = None
    transform: TransformSpec


class PreviewCheckpoint(WireModel):
    candidate: ArtifactRef
    report: ArtifactRef
    valid: bool
    well_formed: bool


class PreviewRecord(WireModel):
    id: Nonempty
    workspace_id: Nonempty
    generation: int
    input_digest: str
    schema_digest: str
    code_digest: str
    candidate: ArtifactRef
    report: ArtifactRef
    diff: ArtifactRef
    structural_diff: ArtifactRef
    valid: bool
    well_formed: bool
    warnings: list[str]
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
        inputs: SessionInputs,
        pool: WorkerPool,
        outputs: OutputStore,
        *,
        backend: str = "disabled",
        registry: dict[str, str] | None = None,
    ) -> None:
        self.inputs, self.pool, self.outputs = inputs, pool, outputs
        self.limits = inputs.limits
        self.backend = backend
        self.registry = dict(registry or {})
        self.staging = outputs.storage.scratch / "preview-staging"
        self.staging.mkdir(mode=0o700, exist_ok=True)
        self.execution = TrustedLocalExecution(self.staging, self.limits)

    async def preview(self, workspace_id: str, request: PreviewTransformRequest) -> PreviewRecord:
        if self.backend != "trusted-local":
            raise ToolError("execution_unavailable", "No supported execution backend configured")
        spec = normalize_transform(request.transform, self.limits)
        sources = (
            resolve_sources(spec, self.registry, self.limits)
            if isinstance(spec, PipelineTransformSpec)
            else {}
        )
        async with self.inputs.capture(workspace_id) as capture:
            operation = PreviewOperation(
                request_id=secrets.token_hex(16),
                capture=capture_to_wire(capture),
                document=request.document,
                source_schema=request.source_schema,
                target_schema=request.target_schema or request.source_schema,
                transform=spec,
                sources=sources,
            )
            return await self._execute(
                operation,
                capture,
                deadline=time.monotonic() + self.limits.transform_seconds,
            )

    async def _put(self, content: bytes, media: str, *, name: str, kind: str) -> ArtifactRef:
        return _artifact(
            await self.outputs.put_bytes(content, name=name, media_type=media, kind=kind)
        )

    async def _execute(
        self, operation: PreviewOperation, capture: InputCapture, *, deadline: float
    ) -> PreviewRecord:
        # Preserve inputs before user code runs. Local execution has launching-user
        # privileges; it must not supply the schema bytes used for the final verdict.
        with SafeDirectory(capture.root) as source:
            originals = {
                item.path: source.read(item.path, max_bytes=self.limits.xml_file_bytes).content
                for item in capture.files
            }
        if any(
            hashlib.sha256(originals[item.path]).hexdigest() != item.digest
            for item in capture.files
        ):
            raise ToolError("source_changed", "Preview input differs from its manifest")
        original = originals[operation.document.path]
        with tempfile.TemporaryDirectory(prefix="preview-", dir=self.staging) as directory:
            root = Path(directory)
            self._materialize(root, originals)
            isolated = replace(capture, root=root)
            candidate = await self.execution.run(
                isolated,
                operation.source_schema,
                operation.document,
                operation.transform,
                registry=operation.sources,
                deadline=deadline,
            )
        valid, well_formed, report = await self._validate(
            operation, capture, originals, candidate.content, operation.target_schema, deadline
        )
        structural: JsonValue = (
            await self._compare(operation, capture, originals, candidate.content, deadline)
            if well_formed
            else {"available": False, "reason": "Candidate is not well-formed XML"}
        )
        checkpoints = []
        for index, content in enumerate(candidate.checkpoint_candidates):
            checkpoint_valid, checkpoint_formed, checkpoint_report = await self._validate(
                operation, capture, originals, content, operation.source_schema, deadline
            )
            checkpoints.append(
                PreviewCheckpoint(
                    candidate=await self._put(
                        content,
                        "application/xml",
                        name=f"checkpoint-{index}.xml",
                        kind="preview-checkpoint",
                    ),
                    report=await self._put(
                        json.dumps(checkpoint_report, separators=(",", ":")).encode(),
                        "application/json",
                        name=f"checkpoint-{index}-report.json",
                        kind="preview-checkpoint-report",
                    ),
                    valid=checkpoint_valid,
                    well_formed=checkpoint_formed,
                )
            )
        return await self._record(
            operation,
            capture,
            original,
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
        capture: InputCapture,
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
                capture,
                root=root,
                files=(
                    *capture.files,
                    CaptureFile(name, hashlib.sha256(content).hexdigest(), len(content)),
                ),
            )
            reply = await self.pool.run(
                StructuralDiffOperation(
                    request_id=secrets.token_hex(16),
                    capture=capture_to_wire(comparison),
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
        capture: InputCapture,
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
            validation_capture = replace(
                capture,
                root=root,
                files=(
                    *capture.files,
                    CaptureFile(candidate_path, digest, len(content)),
                ),
            )
            validation = await self.pool.run(
                ParseOperation(
                    request_id=secrets.token_hex(16),
                    capture=capture_to_wire(validation_capture),
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
        capture: InputCapture,
        original: bytes,
        content: bytes,
        valid: bool,
        well_formed: bool,
        report: JsonValue,
        checkpoints: tuple[PreviewCheckpoint, ...],
        structural: JsonValue,
    ) -> PreviewRecord:
        candidate_ref = await self._put(
            content, "application/xml", name="candidate.xml", kind="preview-candidate"
        )
        report_ref = await self._put(
            json.dumps(report, separators=(",", ":")).encode(),
            "application/json",
            name="report.json",
            kind="preview-report",
        )
        diff = "".join(
            difflib.unified_diff(
                original.decode("utf-8", errors="replace").splitlines(keepends=True),
                content.decode("utf-8", errors="replace").splitlines(keepends=True),
                fromfile="original",
                tofile="candidate",
            )
        ).encode()
        diff_ref = await self._put(diff, "text/x-diff", name="candidate.diff", kind="preview-diff")
        structural_ref = await self._put(
            json.dumps(structural, separators=(",", ":")).encode(),
            "application/json",
            name="structural.json",
            kind="preview-structural",
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
        return PreviewRecord(
            id=operation.request_id,
            workspace_id=capture.workspace_id,
            generation=capture.generation,
            input_digest=hashlib.sha256(original).hexdigest(),
            schema_digest=hashlib.sha256(
                json.dumps(
                    {
                        "files": [(item.path, item.digest) for item in capture.files],
                        "schema": operation.target_schema.model_dump(mode="json"),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            code_digest=hashlib.sha256(code.encode()).hexdigest(),
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
        )

    @staticmethod
    def _materialize(root: Path, files: dict[str, bytes]) -> None:
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
