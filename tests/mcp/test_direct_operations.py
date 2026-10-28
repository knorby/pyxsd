"""Local-only MCP operations return direct results instead of durable jobs."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio

from pydantic import ValidationError
from pyxsd.mcp.contracts import FileRef, InlineFile, SchemaRef
from pyxsd.mcp.errors import ToolError

OLD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="Root"/>
<xs:complexType name="Root"><xs:sequence>
<xs:element name="item" type="xs:int"/>
</xs:sequence></xs:complexType>
</xs:schema>"""

NEW = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="Root"/>
<xs:complexType name="Root"><xs:sequence>
<xs:element name="item" type="xs:int"/>
<xs:element name="note" type="xs:string" minOccurs="0"/>
</xs:sequence></xs:complexType>
</xs:schema>"""

DOC = "<root><item>1</item></root>"
REPAIR = "def transform(document, context):\n    return document\n"


def test_sdk_exposes_direct_results_and_no_job_tools(tmp_path):
    from tests.mcp.test_server import connected

    async def scenario():
        async with connected(
            tmp_path,
            enabled_groups={"exploration", "workspaces", "execution"},
            execution_backend="trusted-local",
        ) as (client, _services):
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "pyxsd_job_status" not in names
            assert "pyxsd_cancel_job" not in names

            created = await client.call_tool("pyxsd_manage_workspace", {"action": "create"})
            workspace = created.structured_content["data"]["id"]
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {"path": "old.xsd", "text": OLD},
                        {"path": "new.xsd", "text": NEW},
                        {"path": "doc.xml", "text": DOC},
                    ],
                },
            )
            compared = await client.call_tool(
                "pyxsd_compare_schemas",
                {
                    "workspace_id": workspace,
                    "old_schema": {"source": {"path": "old.xsd"}},
                    "new_schema": {"source": {"path": "new.xsd"}},
                    "corpus": [{"path": "doc.xml"}],
                },
            )
            comparison = compared.structured_content["data"]
            assert comparison["structural"]["totals"]["added"] >= 1
            assert comparison["corpus"]["files"][0]["status"] == "valid"
            assert comparison["conclusions"]["corpus_only"] is True
            assert "state" not in comparison
            assert "id" not in comparison

            previewed = await client.call_tool(
                "pyxsd_preview_transform",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "source_schema": {"source": {"path": "old.xsd"}},
                    "transform": {"kind": "python", "source": REPAIR},
                },
            )
            record = previewed.structured_content["data"]
            assert previewed.is_error is False, previewed
            assert "valid" in record, record
            assert record["candidate"]["digest"]
            assert "state" not in record
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "pyxsd_commit_transform" not in names

    anyio.run(scenario)


def test_direct_requests_have_no_job_only_idempotency_field():
    from pyxsd.mcp.previews import PreviewTransformRequest
    from pyxsd.mcp.services.comparison import CompareSchemasRequest
    from pyxsd.mcp.services.records import ExportRecordsRequest

    for model, fields in (
        (CompareSchemasRequest, {"workspace_id": "w"}),
        (ExportRecordsRequest, {"workspace_id": "w", "element": "root"}),
        (
            PreviewTransformRequest,
            {
                "workspace_id": "w",
                "document": FileRef(path="doc.xml"),
                "source_schema": SchemaRef(source=FileRef(path="schema.xsd")),
                "transform": {"kind": "python", "source": REPAIR},
            },
        ),
    ):
        with pytest.raises(ValidationError):
            model(idempotency_key="not-a-job", **fields)


def test_artifacts_survive_session_restart_but_workspaces_do_not(tmp_path):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.inputs import SessionInputs
    from pyxsd.mcp.output_store import OutputStore
    from pyxsd.mcp.storage import Storage

    async def scenario():
        storage = Storage(tmp_path / "state")
        try:
            outputs = OutputStore(storage, Limits())
            inputs = SessionInputs(storage.scratch, Limits())
            workspace = await inputs.create()
            await inputs.put_files(workspace.id, [InlineFile(path="doc.xml", text=DOC)])
            kept = await outputs.put_bytes(
                b"evidence", name="report.json", media_type="application/json", kind="report"
            )
            outputs.close()
        finally:
            storage.close()

        restarted = Storage(tmp_path / "state")
        try:
            outputs = OutputStore(restarted, Limits())
            inputs = SessionInputs(restarted.scratch, Limits())
            assert await inputs.list() == []
            with pytest.raises(ToolError, match="not_found"):
                await inputs.put_files(workspace.id, [])
            assert await outputs.read(kept.id, offset=0, length=64) == b"evidence"
            outputs.close()
        finally:
            restarted.close()

    anyio.run(scenario)
