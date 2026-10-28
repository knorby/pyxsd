"""Cross-layer quotas fail closed at their exact configured boundaries."""

from typing import get_args

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import INVALID, XSD, harness, upload

from pyxsd.mcp.artifacts import ArtifactStore
from pyxsd.mcp.config import Limits, Settings
from pyxsd.mcp.contracts import FileRef, Page, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.safe_io import AdmittedFile
from pyxsd.mcp.services.exploration import ValidateDocumentRequest
from pyxsd.mcp.snapshot_store import SnapshotStore
from pyxsd.mcp.state import State
from pyxsd.mcp.workspaces import Principal, WorkspaceStore, stdio_principal


def test_only_disabled_and_trusted_local_execution_are_configurable():
    assert get_args(Settings.model_fields["execution_backend"].annotation) == (
        "disabled",
        "trusted-local",
    )
    assert "worker_memory_bytes" not in Limits.model_fields
    assert "worker_pids" not in Limits.model_fields


def test_snapshot_revision_and_entry_limits_are_atomic(tmp_path):
    async def scenario():
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits(snapshot_revisions=1))
            principal = stdio_principal()
            workspace = await workspaces.create(principal)
            store = SnapshotStore(workspaces)
            first = await store.put_bytes(
                principal,
                workspace.id,
                [AdmittedFile("one.xml", b"<r/>")],
                expected_revision=None,
            )
            with pytest.raises(ToolError, match="quota_exceeded"):
                await store.put_bytes(
                    principal,
                    workspace.id,
                    [AdmittedFile("two.xml", b"<r/>")],
                    expected_revision=first.revision,
                )
            assert (
                await state.run(
                    lambda db: db.execute(
                        "SELECT count(*) FROM snapshots WHERE workspace=?", (workspace.id,)
                    ).fetchone()[0]
                )
                == 1
            )

        with State(tmp_path / "other") as state:
            workspaces = WorkspaceStore(state, Limits(snapshot_entries=2))
            principal = stdio_principal()
            workspace = await workspaces.create(principal)
            store = SnapshotStore(workspaces)
            with pytest.raises(ToolError, match="quota_exceeded"):
                await store.put_bytes(
                    principal,
                    workspace.id,
                    [AdmittedFile("a/b/c.xml", b"")],
                    expected_revision=None,
                )

    anyio.run(scenario)


def test_workspace_count_and_aggregate_byte_boundaries(tmp_path):
    async def scenario():
        with State(tmp_path / "state") as state:
            principal = Principal("alice", frozenset({"pyxsd:read", "pyxsd:upload"}))
            limited = WorkspaceStore(state, Limits(workspaces=1))
            first = await limited.create(principal)
            with pytest.raises(ToolError, match="quota_exceeded"):
                await limited.create(principal)

        with State(tmp_path / "aggregate") as state:
            principal = Principal("alice", frozenset({"pyxsd:read", "pyxsd:upload"}))
            workspaces = WorkspaceStore(
                state, Limits(workspaces=2, workspace_bytes=10, managed_bytes=10)
            )
            first = await workspaces.create(principal)
            second = await workspaces.create(principal)
            async with workspaces.reserve(principal, first.id, 3) as reservation:
                await reservation.commit(size=3)
            async with workspaces.reserve(principal, second.id, 2) as reservation:
                await reservation.commit(size=2)
            with pytest.raises(ToolError, match="quota_exceeded"):
                async with workspaces.reserve(principal, second.id, 6):
                    pytest.fail("aggregate managed-byte budget was exceeded")
            async with workspaces.reserve(principal, second.id, 5, kind="artifacts") as reservation:
                await reservation.commit(size=5)

    anyio.run(scenario)


def test_report_limits_bound_page_size_issue_collection_and_suppression(tmp_path):
    async def scenario():
        limits = Limits(workers=1, page_items=1, collected_issues=2)
        async with harness(tmp_path, limits) as (
            service,
            snapshots,
            _pool,
            principal,
            workspace,
        ):
            await upload(snapshots, workspace, {"schema.xsd": XSD, "invalid.xml": INVALID})
            result = await service.validate_document(
                principal,
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="invalid.xml"),
                    schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                    page=Page(limit=1),
                ),
            )
            assert result.page.returned == 1
            assert result.page.total == 4
            assert result.page.truncated is True
            report = result.data["report"]
            assert len(report["issues"]) == 1
            assert report["suppressed_count"] == 2
            assert report["counts"] == {"error": 4, "warning": 0}
            with pytest.raises(ToolError, match="invalid_argument"):
                await service.validate_document(
                    principal,
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="invalid.xml"),
                        schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                        page=Page(limit=2),
                    ),
                )

    anyio.run(scenario)


def test_artifact_entry_limit_is_enforced_before_retaining_bytes(tmp_path):
    async def chunks(content):
        yield content

    async def scenario():
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits(artifact_entries=1))
            workspace = await workspaces.create(stdio_principal())
            store = ArtifactStore(workspaces)
            await store.put(
                stdio_principal(),
                workspace.id,
                chunks(b"one"),
                media_type="text/plain",
                limit=3,
                ttl=30,
            )
            with pytest.raises(ToolError, match="quota_exceeded"):
                await store.put(
                    stdio_principal(),
                    workspace.id,
                    chunks(b"two"),
                    media_type="text/plain",
                    limit=3,
                    ttl=30,
                )
            assert (
                await state.run(
                    lambda db: db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
                )
                == 1
            )

    anyio.run(scenario)
