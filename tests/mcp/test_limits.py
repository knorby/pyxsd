"""Limits fail closed at their configured boundaries without historical quotas."""

from typing import get_args

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import INVALID, XSD, harness, upload

from pyxsd.mcp.config import Limits, Settings
from pyxsd.mcp.contracts import FileRef, InlineFile, Page, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.inputs import SessionInputs
from pyxsd.mcp.services.exploration import ValidateDocumentRequest


def test_only_disabled_and_trusted_local_execution_are_configurable():
    assert get_args(Settings.model_fields["execution_backend"].annotation) == (
        "disabled",
        "trusted-local",
    )
    for removed in (
        "worker_memory_bytes",
        "worker_pids",
        "artifact_entries",
        "snapshot_revisions",
        "managed_bytes",
    ):
        assert removed not in Limits.model_fields


def test_session_input_count_entry_and_byte_boundaries(tmp_path):
    async def scenario():
        inputs = SessionInputs(tmp_path / "scratch", Limits(workspaces=1, workspace_bytes=3))
        workspace = await inputs.create()
        with pytest.raises(ToolError, match="quota_exceeded"):
            await inputs.create()
        await inputs.put_files(workspace.id, [])
        with pytest.raises(ToolError, match="quota_exceeded"):
            await inputs.put_files(workspace.id, [InlineFile(path="a.xml", text="<a/>")])
        await inputs.release(workspace.id)

        entries = SessionInputs(tmp_path / "entries", Limits(input_entries=2))
        workspace = await entries.create()
        with pytest.raises(ToolError, match="quota_exceeded"):
            await entries.put_files(workspace.id, [InlineFile(path="a/b/c.xml", text="")])
        await entries.release(workspace.id)

    anyio.run(scenario)


def test_report_limits_bound_page_size_issue_collection_and_suppression(tmp_path):
    async def scenario():
        limits = Limits(workers=1, page_items=1, collected_issues=2)
        async with harness(tmp_path, limits) as (
            service,
            inputs,
            _pool,
            workspace,
        ):
            await upload(inputs, workspace, {"schema.xsd": XSD, "invalid.xml": INVALID})
            result = await service.validate_document(
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
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="invalid.xml"),
                        schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                        page=Page(limit=2),
                    ),
                )

    anyio.run(scenario)
