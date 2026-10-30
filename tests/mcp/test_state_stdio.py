"""The real transport respects launch directories and configured sharing."""

import sys
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("mcp")
import anyio
from mcp.client.stdio import stdio_client

from mcp import ClientSession, StdioServerParameters


@asynccontextmanager
async def client_at(project, *args):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "pyxsd.mcp", *args], cwd=str(project)
    )
    async with stdio_client(parameters) as streams, ClientSession(*streams) as client:
        with anyio.fail_after(30):
            await client.initialize()
            await client.list_tools()
            await client.list_resources()
        yield client


async def call(client, name, arguments):
    with anyio.fail_after(30):
        response = await client.call_tool("pyxsd_" + name, arguments)
    assert not response.is_error, response
    return response.structured_content


async def invalid_report(client):
    workspace = (await call(client, "manage_workspace", {"action": "create"}))["data"]["id"]
    await call(
        client,
        "put_files",
        {
            "workspace_id": workspace,
            "files": [
                {
                    "path": "a.xsd",
                    "text": '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="root" type="xs:int"/></xs:schema>',
                },
                {"path": "a.xml", "text": "<root>bad</root>"},
            ],
        },
    )
    result = await call(
        client,
        "validate_document",
        {
            "workspace_id": workspace,
            "document": {"path": "a.xml"},
            "schema_ref": {"source": {"path": "a.xsd"}},
        },
    )
    assert result["data"]["valid"] is False
    return result["artifacts"][0]["id"]


@pytest.mark.parametrize("shared", [False, True])
def test_real_stdio_project_isolation_and_configured_sharing(tmp_path, shared):
    async def scenario():
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        args = ["--state-dir", str(tmp_path / "shared")] if shared else []
        async with client_at(a, *args) as first:
            async with client_at(b, *args) as second:
                caps = await call(first, "capabilities", {})
                assert caps["data"]["project_root"] == str(a)
                assert not (a / ".pyxsd").exists()
                assert not (b / ".pyxsd").exists()
                assert not (tmp_path / "shared").exists()
                identifier = await invalid_report(first)
                listed = await call(second, "list_artifacts", {})
                assert bool(listed["data"]["artifacts"]) is shared
            # The other server closing cannot invalidate this server's store.
            assert (await call(first, "read_artifact", {"artifact_id": identifier, "length": 16}))[
                "data"
            ]["length"] == 16
        async with client_at(a, *args) as restarted:
            listed = await call(restarted, "list_artifacts", {})
            assert [item["id"] for item in listed["data"]["artifacts"]] == [identifier]
        assert not (b / ".pyxsd").exists()

    anyio.run(scenario)


def test_no_persist_real_stdio_never_creates_project_state(tmp_path):
    async def scenario():
        async with client_at(tmp_path, "--no-persist") as client:
            await invalid_report(client)
            assert not (tmp_path / ".pyxsd").exists()
        assert not (tmp_path / ".pyxsd").exists()

    anyio.run(scenario)
