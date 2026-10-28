"""Preview outputs retain exact bytes and validate outside execution."""

import hashlib
import json
import time
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_workers import XSD

from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, InlineFile, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.inputs import SessionInputs
from pyxsd.mcp.output_store import OutputStore
from pyxsd.mcp.previews import PreviewService
from pyxsd.mcp.storage import Storage
from pyxsd.mcp.workers import WorkerPool


@asynccontextmanager
async def harness(tmp_path, *, backend="trusted-local", limits=None):
    limits = limits or Limits(workers=1)
    with Storage(tmp_path / "state", persist=False) as storage:
        outputs = OutputStore(storage, limits)
        inputs = SessionInputs(storage.scratch, limits)
        pool = WorkerPool(limits)
        previews = PreviewService(inputs, pool, outputs, backend=backend)
        workspace = await inputs.create()
        await inputs.put_files(
            workspace.id,
            [
                InlineFile(path="schema.xsd", text=XSD.decode()),
                InlineFile(path="document.xml", text="<root><item>bad</item></root>"),
            ],
        )
        try:
            yield previews, workspace.id
        finally:
            await pool.aclose()
            outputs.close()


async def preview(service, workspace, source):
    from pyxsd.mcp.previews import PreviewTransformRequest
    from pyxsd.mcp.transforms import normalize_transform

    request = PreviewTransformRequest(
        workspace_id=workspace,
        document=FileRef(path="document.xml"),
        source_schema=SchemaRef(source=FileRef(path="schema.xsd")),
        transform=normalize_transform(
            {"kind": "python", "source": source} if isinstance(source, str) else source,
            service.limits,
        ),
    )
    return await service.preview(workspace, request)


def test_repair_retains_exact_candidate_report_and_serialization_diff(tmp_path):
    source = """from io import StringIO
def transform(document, context):
    return document.schema.parse(StringIO('<root><item>7</item></root>'))
"""

    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            record = await preview(service, workspace, source)
            assert record.valid is True and record.well_formed is True
            assert record.generation == 1
            assert (
                record.input_digest == hashlib.sha256(b"<root><item>bad</item></root>").hexdigest()
            )
            assert record.code_digest == hashlib.sha256(source.encode()).hexdigest()
            content = await service.outputs.read(record.candidate.id, offset=0, length=8192)
            assert b">7<" in content
            assert hashlib.sha256(content).hexdigest() == record.candidate.digest
            assert record.candidate.kind == "preview-candidate"
            diff = await service.outputs.read(record.diff.id, offset=0, length=8192)
            assert b"bad" in diff and b"7" in diff
            structural = json.loads(
                await service.outputs.read(record.structural_diff.id, offset=0, length=8192)
            )
            assert structural["available"] is True
            assert structural["identity"] == "positional-expanded-name"
            changed = next(
                change
                for change in structural["changes"]
                if change["path"][-1] == {"name": "item", "index": 1}
            )
            assert changed["kind"] == "changed"
            assert "comments" in " ".join(record.warnings)

    anyio.run(scenario)


def test_hostile_transform_cannot_monkeypatch_clean_validator(tmp_path):
    source = """from pyxsd.document import Document
def transform(document, context):
    Document.to_string = lambda self: '<root><item>not-an-int</item></root>'
    Document.require_valid = lambda self: None
    return document
"""

    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            record = await preview(service, workspace, source)
            assert record.valid is False and record.well_formed is True
            report = await service.outputs.read(record.report.id, offset=0, length=8192)
            assert b'"error":0' not in report
            candidate = await service.outputs.read(record.candidate.id, offset=0, length=8192)
            assert b"not-an-int" in candidate

    anyio.run(scenario)


def test_preview_never_falls_back_to_local_execution(tmp_path):
    from pyxsd.mcp.previews import PreviewTransformRequest

    async def scenario():
        async with harness(tmp_path, backend="disabled") as (service, workspace):
            with pytest.raises(ToolError, match="execution_unavailable"):
                await service.preview(
                    workspace,
                    PreviewTransformRequest(
                        workspace_id=workspace,
                        document=FileRef(path="document.xml"),
                        source_schema=SchemaRef(source=FileRef(path="schema.xsd")),
                        transform={
                            "kind": "python",
                            "source": "def transform(document, context): pass",
                        },
                    ),
                )

    anyio.run(scenario)


def test_worker_modified_input_paths_are_never_reused_for_clean_validation(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_bytes(b"keep")
    source = f"""from pathlib import Path
def transform(document, context):
    path = document.schema.resource_policy.capture.root / 'schema.xsd'
    path.unlink()
    path.symlink_to({str(marker)!r})
    return document
"""

    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            record = await preview(service, workspace, source)
            assert record.valid is False
            assert marker.read_bytes() == b"keep"

    anyio.run(scenario)


def test_checkpoint_verdict_ignores_executed_validator_and_retains_bytes(tmp_path):
    first = """from pyxsd.document import Document
def transform(document, context):
    Document.to_string = lambda self: '<root><item>bad-checkpoint</item></root>'
    Document.revalidate = lambda self: self
    return document
"""
    final = """from pyxsd.document import Document
def transform(document, context):
    Document.to_string = lambda self: '<root><item>42</item></root>'
    return document
"""

    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            record = await preview(
                service,
                workspace,
                {
                    "kind": "pipeline",
                    "steps": [{"name": "first", "checkpoint": True}, {"name": "final"}],
                    "definitions": {"first": first, "final": final},
                },
            )
            assert record.valid is True
            assert len(record.checkpoints) == 1
            checkpoint = record.checkpoints[0]
            assert checkpoint.valid is False
            assert checkpoint.well_formed is True
            content = await service.outputs.read(checkpoint.candidate.id, offset=0, length=8192)
            assert content == b"<root><item>bad-checkpoint</item></root>"

    anyio.run(scenario)


def test_malformed_candidate_stays_inspectable(tmp_path):
    source = """from pyxsd.document import Document
def transform(document, context):
    Document.to_string = lambda self: '<root>'
    return document
"""

    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            record = await preview(service, workspace, source)
            assert not record.valid and not record.well_formed
            content = await service.outputs.read(record.candidate.id, offset=0, length=8192)
            assert content == b"<root>"
            report = await service.outputs.read(record.report.id, offset=0, length=8192)
            assert json.loads(report)["fault"]["code"] == "invalid_xml"
            structural = json.loads(
                await service.outputs.read(record.structural_diff.id, offset=0, length=8192)
            )
            assert structural["available"] is False

    anyio.run(scenario)


def test_cross_schema_preview_uses_target_schema(tmp_path):
    from pyxsd.mcp.previews import PreviewTransformRequest

    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            await service.inputs.put_files(
                workspace,
                [InlineFile(path="target.xsd", text=XSD.replace(b"xs:int", b"xs:string").decode())],
            )
            request = PreviewTransformRequest(
                workspace_id=workspace,
                document=FileRef(path="document.xml"),
                source_schema=SchemaRef(source=FileRef(path="schema.xsd")),
                target_schema=SchemaRef(source=FileRef(path="target.xsd")),
                transform={"kind": "python", "source": "def transform(document, context): pass"},
            )
            record = await service.preview(workspace, request)
            assert record.valid
            assert record.generation == 2

    anyio.run(scenario)


def test_transform_failure_is_typed_and_inputs_are_released(tmp_path):
    from pyxsd.mcp.previews import PreviewTransformRequest

    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            request = PreviewTransformRequest(
                workspace_id=workspace,
                document=FileRef(path="document.xml"),
                source_schema=SchemaRef(source=FileRef(path="schema.xsd")),
                transform={
                    "kind": "python",
                    "source": "def transform(document, context): raise ValueError()",
                },
            )
            with pytest.raises(ToolError, match="transform_failed"):
                await service.preview(workspace, request)
            assert list((service.staging).iterdir()) == []

    anyio.run(scenario)


def test_worker_faults_surface_before_a_preview_record(tmp_path, monkeypatch):
    from pyxsd.mcp.contracts import ToolFault
    from pyxsd.mcp.worker_protocol import WorkerReply

    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            async with service.inputs.capture(workspace) as capture:
                operation = await _operation(service, capture)
                (capture.root / "document.xml").write_bytes(b"changed")
                with pytest.raises(ToolError, match="source_changed"):
                    await service._execute(operation, capture, deadline=time.monotonic() + 10)

            async def failed(op, *, deadline):
                return WorkerReply(
                    request_id=op.request_id,
                    role="clean",
                    status="error",
                    fault=ToolFault(code="worker_died", message="No verdict"),
                )

            monkeypatch.setattr(service.pool, "run", failed)
            with pytest.raises(ToolError, match="worker_died"):
                await preview(service, workspace, "def transform(document, context): pass")

    anyio.run(scenario)


async def _operation(service, capture):
    from pyxsd.mcp.previews import PreviewOperation
    from pyxsd.mcp.transforms import normalize_transform
    from pyxsd.mcp.workers import capture_to_wire

    return PreviewOperation(
        request_id="direct",
        capture=capture_to_wire(capture),
        document=FileRef(path="document.xml"),
        source_schema=SchemaRef(source=FileRef(path="schema.xsd")),
        target_schema=SchemaRef(source=FileRef(path="schema.xsd")),
        transform=normalize_transform(
            {"kind": "python", "source": "def transform(document, context): pass"},
            service.limits,
        ),
        sources={},
    )


def test_structural_diff_worker_failure_is_typed(tmp_path, monkeypatch):
    from pyxsd.mcp.contracts import ToolFault
    from pyxsd.mcp.worker_protocol import StructuralDiffOperation, WorkerReply

    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            original = service.pool.run

            async def fail_compare(operation, *, deadline):
                if isinstance(operation, StructuralDiffOperation):
                    return WorkerReply(
                        request_id=operation.request_id,
                        role="clean",
                        status="error",
                        fault=ToolFault(code="worker_died", message="No diff"),
                    )
                return await original(operation, deadline=deadline)

            monkeypatch.setattr(service.pool, "run", fail_compare)
            with pytest.raises(ToolError, match="worker_died"):
                await preview(service, workspace, "def transform(document, context): pass")

    anyio.run(scenario)
