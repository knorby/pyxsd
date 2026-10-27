"""MCP preview submission shares the durable job and artifact lifecycle."""

import base64
import hashlib

import pytest

pytest.importorskip("mcp")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_server import connected, create_workspace, structured, upload


def test_stdio_preview_uses_shared_jobs_and_retained_artifacts(tmp_path):
    async def scenario():
        async with connected(
            tmp_path,
            execution_backend="trusted-local",
            enabled_groups={"workspaces", "execution"},
        ) as (client, services):
            workspace = await create_workspace(client)
            await upload(client, workspace)
            arguments = {
                "workspace_id": workspace,
                "document": {"path": "document.xml"},
                "source_schema": {"source": {"path": "schema.xsd"}},
                "transform": {
                    "kind": "python",
                    "source": "def transform(document, context):\n    return None\n",
                },
                "idempotency_key": "sdk-preview",
            }
            submitted = await client.call_tool("pyxsd_preview_transform", arguments)
            assert submitted.is_error is False, submitted
            identifier = structured(submitted)["data"]["id"]
            with anyio.fail_after(10):
                while True:
                    status = await client.call_tool("pyxsd_job_status", {"job_id": identifier})
                    result = structured(status)["data"]
                    if result["state"] in {"succeeded", "failed", "cancelled"}:
                        break
                    await anyio.sleep(0.01)
            assert result["state"] == "succeeded", result
            record = result["result"]
            assert record["valid"] is True
            assert services.previews.jobs is services.jobs
            artifact = await client.call_tool(
                "pyxsd_read_artifact", {"artifact_id": record["candidate"]["id"]}
            )
            content = base64.b64decode(structured(artifact)["data"]["data"])
            assert hashlib.sha256(content).hexdigest() == record["candidate"]["digest"]
            replay = await client.call_tool("pyxsd_preview_transform", arguments)
            assert structured(replay)["data"]["id"] == identifier
            cancelled = await client.call_tool("pyxsd_cancel_job", {"job_id": identifier})
            assert structured(cancelled)["data"]["state"] == "succeeded"

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
                    "idempotency_key": "disabled",
                },
            )
            assert refused.is_error is True
            assert structured(refused)["data"]["fault"]["code"] == "execution_unavailable"

    anyio.run(scenario)
