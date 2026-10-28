"""MCP previews return retained artifacts directly and publish exact bytes."""

import base64
import hashlib

import pytest

pytest.importorskip("mcp")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_server import connected, create_workspace, structured, upload


def test_stdio_preview_returns_retained_artifacts_and_publishes(tmp_path):
    async def scenario():
        async with connected(
            tmp_path,
            execution_backend="trusted-local",
            enabled_groups={"workspaces", "execution"},
        ) as (client, _services):
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
            }
            submitted = await client.call_tool("pyxsd_preview_transform", arguments)
            assert submitted.is_error is False, submitted
            record = structured(submitted)["data"]
            identifier = record["id"]
            assert record["valid"] is True
            artifact = await client.call_tool(
                "pyxsd_read_artifact", {"artifact_id": record["candidate"]["id"]}
            )
            content = base64.b64decode(structured(artifact)["data"]["data"])
            assert hashlib.sha256(content).hexdigest() == record["candidate"]["digest"]
            commit_arguments = {
                "preview_id": identifier,
                "expected_candidate_digest": record["candidate"]["digest"],
                "destination": "published.xml",
                "idempotency_key": "sdk-commit",
            }
            committed = await client.call_tool("pyxsd_commit_transform", commit_arguments)
            assert committed.is_error is False, committed
            receipt = structured(committed)["data"]
            assert receipt["candidate_digest"] == hashlib.sha256(content).hexdigest()
            retried = await client.call_tool("pyxsd_commit_transform", commit_arguments)
            assert structured(retried)["data"] == receipt
            refused = await client.call_tool(
                "pyxsd_commit_transform", {**commit_arguments, "idempotency_key": "another"}
            )
            assert refused.is_error is True
            assert structured(refused)["data"]["fault"]["code"] == "preview_consumed"

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
