"""Local-only MCP operations return direct results instead of durable jobs."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio

from pydantic import ValidationError
from pyxsd.mcp.contracts import FileRef, SchemaRef
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

            created = await client.call_tool(
                "pyxsd_manage_workspace", {"action": "create", "idempotency_key": "direct"}
            )
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
                    "expected_revision": None,
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
            committed = await client.call_tool(
                "pyxsd_commit_transform",
                {
                    "preview_id": record["id"],
                    "expected_candidate_digest": record["candidate"]["digest"],
                    "destination": "revised.xml",
                    "idempotency_key": "direct-publish",
                },
            )
            assert committed.structured_content["data"]["destination"] == "revised.xml"

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


def test_preview_handles_are_session_scoped_and_expire(tmp_path):
    from tests.mcp.test_previews import harness, preview

    async def scenario():
        async with harness(tmp_path) as (service, principal, workspace):
            record = await preview(service, principal, workspace, REPAIR)
            assert (await service.get(principal, record.id)).id == record.id

            service._previews[(principal.owner_id, record.id)] = record.model_copy(
                update={"expires_at": 0}
            )
            with pytest.raises(ToolError, match="preview_expired"):
                await service.get(principal, record.id)

            fresh = type(service)(
                service.snapshots,
                service.pool,
                service.artifacts,
                backend="trusted-local",
            )
            with pytest.raises(ToolError, match="preview_unavailable"):
                await fresh.get(principal, record.id)

    anyio.run(scenario)
