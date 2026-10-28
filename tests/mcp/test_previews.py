"""Preview verdicts come from clean workers parsing retained candidate bytes."""

import hashlib
import json
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_workers import XSD

from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.safe_io import AdmittedFile
from pyxsd.mcp.state import State
from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal


@asynccontextmanager
async def harness(tmp_path, *, backend="trusted-local", transport="stdio"):
    from pyxsd.mcp.artifacts import ArtifactStore
    from pyxsd.mcp.previews import PreviewService
    from pyxsd.mcp.snapshot_store import SnapshotStore
    from pyxsd.mcp.workers import WorkerPool

    limits = Limits(workers=1)
    with State(tmp_path / "state") as state:
        workspaces = WorkspaceStore(state, limits)
        principal = stdio_principal()
        workspace = await workspaces.create(principal)
        snapshots = SnapshotStore(workspaces)
        artifacts = ArtifactStore(workspaces)
        pool = WorkerPool(limits)
        previews = PreviewService(snapshots, pool, artifacts, backend=backend, transport=transport)
        await snapshots.put_bytes(
            principal,
            workspace.id,
            [
                AdmittedFile("schema.xsd", XSD),
                AdmittedFile("document.xml", b"<root><item>bad</item></root>"),
            ],
            expected_revision=None,
        )
        try:
            yield previews, principal, workspace.id
        finally:
            await previews.jobs.aclose()
            await pool.aclose()


async def preview(service, principal, workspace, source):
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
        idempotency_key="preview",
    )
    ref = await service.submit(principal, request)
    with anyio.fail_after(10):
        while True:
            result = await service.jobs.status(principal, ref.id)
            if result.state in {"succeeded", "failed", "cancelled"}:
                assert result.state == "succeeded", result
                return await service.get(principal, ref.id)
            await anyio.sleep(0.01)


def test_repair_retains_exact_candidate_original_report_and_serialization_diff(tmp_path):
    source = """from io import StringIO
def transform(document, context):
    return document.schema.parse(StringIO('<root><item>7</item></root>'))
"""

    async def scenario():
        async with harness(tmp_path) as (service, principal, workspace):
            record = await preview(service, principal, workspace, source)
            assert record.valid is True and record.well_formed is True
            assert (
                record.input_digest == hashlib.sha256(b"<root><item>bad</item></root>").hexdigest()
            )
            assert record.code_digest == hashlib.sha256(source.encode()).hexdigest()
            content = await service.artifacts.read(
                principal, record.candidate.id, offset=0, length=8192
            )
            assert b">7<" in content
            assert hashlib.sha256(content).hexdigest() == record.candidate.digest
            original = await service.artifacts.read(
                principal, record.original.id, offset=0, length=8192
            )
            assert original == b"<root><item>bad</item></root>"
            diff = await service.artifacts.read(principal, record.diff.id, offset=0, length=8192)
            assert b"bad" in diff and b"7" in diff
            structural = json.loads(
                await service.artifacts.read(
                    principal, record.structural_diff.id, offset=0, length=8192
                )
            )
            assert structural["available"] is True
            assert structural["identity"] == "positional-expanded-name"
            assert structural["total"] >= 1
            changed = next(
                change
                for change in structural["changes"]
                if change["path"][-1] == {"name": "item", "index": 1}
            )
            assert changed["kind"] == "changed"
            assert changed["before_digest"] != changed["after_digest"]
            assert "comments" in " ".join(record.warnings)
            assert record.publication_state == "available"
            assert record.expires_at <= min(
                ref.expires_at
                for ref in (record.original, record.candidate, record.report, record.diff)
            )

    anyio.run(scenario)


def test_hostile_transform_cannot_monkeypatch_clean_validator(tmp_path):
    source = """from pyxsd.document import Document
def transform(document, context):
    Document.to_string = lambda self: '<root><item>not-an-int</item></root>'
    Document.require_valid = lambda self: None
    return document
"""

    async def scenario():
        async with harness(tmp_path) as (service, principal, workspace):
            record = await preview(service, principal, workspace, source)
            assert record.valid is False and record.well_formed is True
            report = await service.artifacts.read(
                principal, record.report.id, offset=0, length=8192
            )
            assert b'"error":0' not in report
            candidate = await service.artifacts.read(
                principal, record.candidate.id, offset=0, length=8192
            )
            assert b"not-an-int" in candidate

    anyio.run(scenario)


@pytest.mark.parametrize(
    "backend,transport",
    [("disabled", "stdio"), ("trusted-local", "streamable-http")],
)
def test_preview_never_falls_back_to_local_execution(tmp_path, backend, transport):
    from pyxsd.mcp.previews import PreviewTransformRequest

    async def scenario():
        async with harness(tmp_path, backend=backend, transport=transport) as (
            service,
            principal,
            workspace,
        ):
            with pytest.raises(ToolError, match="execution_unavailable"):
                await service.submit(
                    principal,
                    PreviewTransformRequest(
                        workspace_id=workspace,
                        document=FileRef(path="document.xml"),
                        source_schema=SchemaRef(source=FileRef(path="schema.xsd")),
                        transform={
                            "kind": "python",
                            "source": "def transform(document, context): pass",
                        },
                        idempotency_key="refuse",
                    ),
                )

    anyio.run(scenario)


def test_worker_modified_input_paths_are_never_reused_for_clean_validation(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_bytes(b"keep")
    source = f"""from pathlib import Path
def transform(document, context):
    path = document.schema.resource_policy.snapshot.root / 'schema.xsd'
    path.unlink()
    path.symlink_to({str(marker)!r})
    return document
"""

    async def scenario():
        async with harness(tmp_path) as (service, principal, workspace):
            record = await preview(service, principal, workspace, source)
            assert record.valid is False
            assert marker.read_bytes() == b"keep"

    anyio.run(scenario)


def test_checkpoint_verdict_ignores_executed_validator_and_retains_exact_bytes(tmp_path):
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
        async with harness(tmp_path) as (service, principal, workspace):
            record = await preview(
                service,
                principal,
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
            content = await service.artifacts.read(
                principal, checkpoint.candidate.id, offset=0, length=8192
            )
            assert content == b"<root><item>bad-checkpoint</item></root>"
            assert hashlib.sha256(content).hexdigest() == checkpoint.candidate.digest

    anyio.run(scenario)


def test_malformed_candidate_stays_inspectable_but_not_publishable(tmp_path):
    source = """from pyxsd.document import Document
def transform(document, context):
    Document.to_string = lambda self: '<root>'
    return document
"""

    async def scenario():
        async with harness(tmp_path) as (service, principal, workspace):
            record = await preview(service, principal, workspace, source)
            assert not record.valid and not record.well_formed
            content = await service.artifacts.read(
                principal, record.candidate.id, offset=0, length=8192
            )
            assert content == b"<root>"
            report = await service.artifacts.read(
                principal, record.report.id, offset=0, length=8192
            )
            assert json.loads(report)["fault"]["code"] == "invalid_xml"

    anyio.run(scenario)


def test_cross_schema_preview_uses_target_schema_and_rejects_mixed_revisions(tmp_path):
    from pyxsd.mcp.previews import PreviewTransformRequest

    async def scenario():
        async with harness(tmp_path) as (service, principal, workspace):
            current = await service.snapshots.get(principal, workspace)
            await service.snapshots.put_bytes(
                principal,
                workspace,
                [AdmittedFile("target.xsd", XSD.replace(b"xs:int", b"xs:string"))],
                expected_revision=current.revision,
            )
            request = PreviewTransformRequest(
                workspace_id=workspace,
                document=FileRef(path="document.xml"),
                source_schema=SchemaRef(source=FileRef(path="schema.xsd")),
                target_schema=SchemaRef(source=FileRef(path="target.xsd")),
                transform={"kind": "python", "source": "def transform(document, context): pass"},
                idempotency_key="migration",
            )
            ref = await service.submit(principal, request)
            with anyio.fail_after(10):
                while (await service.jobs.status(principal, ref.id)).state == "running" or (
                    await service.jobs.status(principal, ref.id)
                ).state == "queued":
                    await anyio.sleep(0.01)
            assert (await service.get(principal, ref.id)).valid
            stale = request.model_copy(
                update={
                    "document": FileRef(path="document.xml", revision="one"),
                    "target_schema": SchemaRef(source=FileRef(path="target.xsd", revision="two")),
                }
            )
            with pytest.raises(ToolError, match="stale_reference"):
                await service.submit(principal, stale)

    anyio.run(scenario)


def test_unavailable_and_expired_preview_results(tmp_path):
    from pyxsd.mcp.previews import PreviewTransformRequest

    async def scenario():
        async with harness(tmp_path) as (service, principal, workspace):
            request = PreviewTransformRequest(
                workspace_id=workspace,
                document=FileRef(path="document.xml"),
                source_schema=SchemaRef(source=FileRef(path="schema.xsd")),
                transform={
                    "kind": "python",
                    "source": "def transform(document, context): raise ValueError()",
                },
                idempotency_key="failed",
            )
            ref = await service.submit(principal, request)
            with anyio.fail_after(10):
                while (await service.jobs.status(principal, ref.id)).state not in {
                    "failed",
                    "succeeded",
                }:
                    await anyio.sleep(0.01)
            with pytest.raises(ToolError, match="preview_unavailable"):
                await service.get(principal, ref.id)
            record = await preview(
                service, principal, workspace, "def transform(document, context): pass"
            )
            expired = record.model_copy(update={"expires_at": 0})
            await service.snapshots.state.run(
                lambda db: db.execute(
                    "UPDATE jobs SET result=? WHERE id=?", (expired.model_dump_json(), record.id)
                ).close()
            )
            with pytest.raises(ToolError, match="preview_expired"):
                await service.get(principal, record.id)

    anyio.run(scenario)


def test_preview_refuses_changed_inputs_and_clean_worker_operational_faults(tmp_path, monkeypatch):
    import time

    from pyxsd.mcp.contracts import ToolFault
    from pyxsd.mcp.previews import PreviewOperation
    from pyxsd.mcp.worker_protocol import Operation, WorkerReply
    from pyxsd.mcp.workers import snapshot_to_wire

    async def scenario():
        async with harness(tmp_path) as (service, principal, workspace):
            snapshot = await service.snapshots.get(principal, workspace)
            operation = PreviewOperation(
                request_id="direct",
                owner_id=principal.owner_id,
                snapshot=snapshot_to_wire(snapshot),
                document=FileRef(path="document.xml"),
                source_schema=SchemaRef(source=FileRef(path="schema.xsd")),
                target_schema=SchemaRef(source=FileRef(path="schema.xsd")),
                transform={"kind": "python", "source": "def transform(document, context): pass"},
                sources={},
            )
            with pytest.raises(ToolError, match="invalid_argument"):
                await service.run(
                    Operation(
                        request_id="wrong",
                        owner_id=principal.owner_id,
                        snapshot=operation.snapshot,
                    ),
                    deadline=time.monotonic() + 10,
                )
            (snapshot.root / "document.xml").write_bytes(b"changed")
            with pytest.raises(ToolError, match="source_changed"):
                await service.run(operation, deadline=time.monotonic() + 10)

            async def failed(op, *, deadline):
                return WorkerReply(
                    request_id=op.request_id,
                    role="clean",
                    status="error",
                    fault=ToolFault(code="worker_died", message="No verdict"),
                )

            monkeypatch.setattr(service.pool, "run", failed)
            originals = {"schema.xsd": XSD, "document.xml": b"<root/>"}
            with pytest.raises(ToolError, match="worker_died"):
                await service._validate(
                    operation,
                    snapshot,
                    originals,
                    b"<root/>",
                    operation.target_schema,
                    time.monotonic() + 10,
                )
            with pytest.raises(ToolError, match="worker_died"):
                await service._compare(
                    operation, snapshot, originals, b"<root/>", time.monotonic() + 10
                )

    anyio.run(scenario)
