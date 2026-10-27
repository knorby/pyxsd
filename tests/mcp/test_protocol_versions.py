"""Both negotiated MCP protocol generations expose the same ordinary tool surface."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")
pytest.importorskip("mcp")

import anyio
from tests.mcp.test_skill_extension import IDENTIFIER, connected


def test_legacy_initialize_session_uses_tools_and_resources(tmp_path):
    async def scenario():
        async with connected(tmp_path, modern=False) as (client, _services, initialized):
            assert initialized.protocol_version == "2025-11-25"
            # Legacy capabilities do not carry the modern extensions map, but
            # ordinary resources and tools remain fully available.
            assert initialized.capabilities.extensions is None
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"pyxsd_manage_workspace", "pyxsd_put_files", "pyxsd_validate_document"} <= names
            resources = await client.list_resources()
            uris = {str(resource.uri) for resource in resources.resources}
            assert "pyxsd://issue-codes" in uris
            read = await client.read_resource("pyxsd://issue-codes")
            assert read.contents

    anyio.run(scenario)


def test_modern_discovery_advertises_extensions_and_serves_the_same_tools(tmp_path):
    async def scenario():
        async with connected(tmp_path, modern=True) as (client, _services, discovered):
            assert discovered.supported_versions == ["2026-07-28"]
            assert discovered.capabilities.extensions[IDENTIFIER] == {}
            assert discovered.capabilities.resources is not None
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"pyxsd_manage_workspace", "pyxsd_put_files", "pyxsd_validate_document"} <= names
            resources = await client.list_resources()
            uris = {str(resource.uri) for resource in resources.resources}
            assert "pyxsd://issue-codes" in uris
            skill = await client.read_resource("pyxsd://skills/pyxsd-development/SKILL.md")
            assert "pyxsd development workflow" in skill.contents[0].text

    anyio.run(scenario)
