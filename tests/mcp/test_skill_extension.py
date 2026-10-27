"""Skills extension negotiation and content fidelity contracts."""

import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams

from mcp import ClientSession, types
from pydantic import TypeAdapter
from pyxsd.mcp.config import Settings
from pyxsd.mcp.skill_extension import SkillsGetParams
from pyxsd.skills import export_skill, list_skills

IDENTIFIER = "io.modelcontextprotocol/skills"
_RESULT = TypeAdapter(dict)


@asynccontextmanager
async def connected(tmp_path, *, modern=False, **settings_kwargs):
    from pyxsd.mcp.server import Services, create_server

    settings = Settings(state_dir=tmp_path / "state", **settings_kwargs)
    services = Services(settings)
    server = create_server(settings, services)
    try:
        async with (
            create_client_server_memory_streams() as (client_streams, server_streams),
            ClientSession(*client_streams) as client,
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
            )
            initialized = await client.discover() if modern else await client.initialize()
            try:
                yield client, services, initialized
            finally:
                tasks.cancel_scope.cancel()
    finally:
        await services.aclose()


def test_extension_declared_in_capabilities(tmp_path):
    compatibility = json.loads(
        (Path(__file__).parent / "fixtures" / "skills-extension.json").read_text(encoding="utf-8")
    )

    async def scenario():
        async with connected(tmp_path, modern=True) as (_client, _services, initialized):
            extensions = initialized.capabilities.extensions
            assert compatibility["identifier"] == IDENTIFIER
            assert extensions[IDENTIFIER] == {}
            assert compatibility["discoveryProtocol"] in initialized.supported_versions

    anyio.run(scenario)


def test_skills_list_and_get(tmp_path):
    compatibility = json.loads(
        (Path(__file__).parent / "fixtures" / "skills-extension.json").read_text(encoding="utf-8")
    )

    async def scenario():
        async with connected(tmp_path) as (client, _services, _initialized):
            listed = (
                await client.send_request(types.Request(method="skills/list", params={}), _RESULT)
            )["skills"]
            assert {item["frontmatter"]["name"] for item in listed} == {
                skill.name for skill in list_skills()
            }
            by_name = {
                item["frontmatter"]["name"]: item["frontmatter"]["description"] for item in listed
            }
            for skill in list_skills():
                assert by_name[skill.name] == skill.description

            fetched = await client.send_request(
                types.Request(
                    method="skills/get",
                    params=SkillsGetParams(uri="pyxsd://skills/pyxsd-development/SKILL.md"),
                ),
                _RESULT,
            )
            manifest = fetched["skill"]["resources"]
            assert any(entry["uri"].endswith("/SKILL.md") for entry in manifest)
            assert any(entry["uri"].endswith("/references/occurrences.md") for entry in manifest)
            for entry in manifest:
                assert sorted(entry) == compatibility["manifestFields"]
                resource = await client.read_resource(entry["uri"])
                content = resource.contents[0].text.encode("utf-8")
                assert entry["size"] == len(content)
                assert len(entry["digest"]) == 71
                assert "sha256:" + hashlib.sha256(content).hexdigest() == entry["digest"]

    anyio.run(scenario)


def test_no_extension_client_still_works(tmp_path):
    async def scenario():
        async with connected(tmp_path) as (client, _services, _initialized):
            tools = await client.list_tools()
            assert any(tool.name == "pyxsd_capabilities" for tool in tools.tools)
            listed = (
                await client.send_request(types.Request(method="skills/list", params={}), _RESULT)
            )["skills"]
            assert len(listed) == 3

    anyio.run(scenario)


def test_export_matches_get_bytes(tmp_path):
    destination = tmp_path / "export"
    destination.mkdir()
    export_skill("pyxsd-development", destination)

    async def scenario():
        async with connected(tmp_path) as (client, _services, _initialized):
            fetched = await client.send_request(
                types.Request(
                    method="skills/get",
                    params=SkillsGetParams(uri="pyxsd://skills/pyxsd-development/SKILL.md"),
                ),
                _RESULT,
            )
            for entry in fetched["skill"]["resources"]:
                path = entry["uri"].removeprefix("pyxsd://skills/pyxsd-development/")
                content = (await client.read_resource(entry["uri"])).contents[0].text
                exported = (destination / "pyxsd-development" / path).read_text(encoding="utf-8")
                assert exported == content

    anyio.run(scenario)


def test_skill_resources_match_complete_export_without_extension_negotiation(tmp_path):
    async def scenario():
        async with connected(tmp_path) as (client, _services, _initialized):
            resources = await client.list_resources()
            uris = {str(resource.uri) for resource in resources.resources}
            for skill in list_skills():
                exported = export_skill(skill.name, tmp_path)
                fetched = await client.send_request(
                    types.Request(
                        method="skills/get",
                        params=SkillsGetParams(uri=f"pyxsd://skills/{skill.name}/SKILL.md"),
                    ),
                    _RESULT,
                )
                paths = {
                    path.relative_to(exported).as_posix()
                    for path in exported.rglob("*")
                    if path.is_file()
                }
                prefix = f"pyxsd://skills/{skill.name}/"
                assert {
                    entry["uri"].removeprefix(prefix) for entry in fetched["skill"]["resources"]
                } == paths
                for entry in fetched["skill"]["resources"]:
                    assert entry["uri"] in uris
                    result = await client.read_resource(entry["uri"])
                    assert (
                        result.contents[0].text.encode("utf-8")
                        == (exported / entry["uri"].removeprefix(prefix)).read_bytes()
                    )
            assert (await client.read_resource("pyxsd://issue-codes")).contents

    anyio.run(scenario)


@pytest.mark.parametrize("name", ["missing-skill", "references", ".."])
def test_unknown_skill_is_a_typed_error(tmp_path, name):
    async def scenario():
        async with connected(tmp_path) as (client, _services, _initialized):
            with pytest.raises(MCPError, match=r"(?i)unknown"):
                await client.send_request(
                    types.Request(
                        method="skills/get",
                        params=SkillsGetParams(uri=f"pyxsd://skills/{name}/SKILL.md"),
                    ),
                    _RESULT,
                )

    anyio.run(scenario)


def test_docs_make_no_auto_activation_claim():
    sources = [Path("docs/mcp.md")]
    sources.extend(Path("src/pyxsd/skills").rglob("SKILL.md"))
    for path in sources:
        assert "automatically activat" not in path.read_text(encoding="utf-8").lower(), path


def test_skill_reference_manifest_excludes_directories(tmp_path, monkeypatch):
    from importlib import resources

    from pyxsd.mcp.skill_extension import _skill_files

    skill = tmp_path / "example-skill"
    skill.mkdir()
    markdown = "---\nname: example-skill\ndescription: Example skill\n---\n# Example\n"
    (skill / "SKILL.md").write_text(markdown, encoding="utf-8")
    references = tmp_path / "references"
    references.mkdir()
    (references / "empty-directory").mkdir()
    (references / "example.md").write_text("Reference", encoding="utf-8")
    monkeypatch.setattr(resources, "files", lambda _package: tmp_path)

    assert _skill_files("example-skill") == {
        "SKILL.md": markdown,
        "references/example.md": "Reference",
    }
