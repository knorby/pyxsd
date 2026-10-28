"""MCP previews return retained outputs without a publication path."""

import base64
import hashlib

import pytest

pytest.importorskip("mcp")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_server import connected, create_workspace, structured, upload


def test_stdio_preview_returns_retained_artifacts(tmp_path):
    async def scenario():
        async with connected(
            tmp_path,
            execution_backend="trusted-local",
            enabled_groups={"workspaces", "execution"},
        ) as (client, services):
            workspace = await create_workspace(client)
            await upload(client, workspace)
            submitted = await client.call_tool(
                "pyxsd_preview_transform",
                {
                    "workspace_id": workspace,
                    "document": {"path": "document.xml"},
                    "source_schema": {"source": {"path": "schema.xsd"}},
                    "transform": {
                        "kind": "python",
                        "source": "def transform(document, context):\n    return None\n",
                    },
                },
            )
            assert submitted.is_error is False, submitted
            record = structured(submitted)["data"]
            assert record["valid"] is True
            artifact = await client.call_tool(
                "pyxsd_read_artifact", {"artifact_id": record["candidate"]["id"]}
            )
            content = base64.b64decode(structured(artifact)["data"]["data"])
            assert hashlib.sha256(content).hexdigest() == record["candidate"]["digest"]
            assert (await client.list_tools()).tools
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "pyxsd_commit_transform" not in names
            assert (await services.outputs.get(record["candidate"]["id"])).kind == (
                "preview-candidate"
            )

    anyio.run(scenario)


def test_disabled_preview_tool_returns_a_typed_refusal(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups={"workspaces", "execution"}) as (
            client,
            _services,
        ):
            workspace = await create_workspace(client)
            await upload(client, workspace)
            refused = await client.call_tool(
                "pyxsd_preview_transform",
                {
                    "workspace_id": workspace,
                    "document": {"path": "document.xml"},
                    "source_schema": {"source": {"path": "schema.xsd"}},
                    "transform": {"kind": "python", "source": "raise RuntimeError('must not run')"},
                },
            )
            assert refused.is_error is True
            assert structured(refused)["data"]["fault"]["code"] == "execution_unavailable"

    anyio.run(scenario)
