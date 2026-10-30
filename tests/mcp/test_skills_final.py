"""Final SEP-2640 contract, independent of the former vendor wire shape."""

import hashlib
import json
from pathlib import Path

import anyio
import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import RequestParams
from tests.mcp.test_skill_extension import connected

from mcp import types
from pydantic import TypeAdapter
from pyxsd.skills import export_skill, list_skills


class Lookup(RequestParams):
    uri: str


def test_final_discovery_and_atomic_manifests(tmp_path):
    async def scenario():
        async with connected(tmp_path, modern=True) as (client, _, discovery):
            assert discovery.capabilities.resources is not None
            assert discovery.capabilities.extensions["io.modelcontextprotocol/skills"] == {}
            response = await client.send_request(
                types.Request(method="skills/list", params=RequestParams()), TypeAdapter(dict)
            )
            assert response["resultType"] == "complete"
            assert response["ttlMs"] >= 0
            assert response["cacheScope"] == "public"
            assert len(response["skills"]) == len(list_skills())
            for entry in response["skills"]:
                name = entry["frontmatter"]["name"]
                exported = export_skill(name, tmp_path)
                assert entry["uri"] == f"pyxsd://skills/{name}/SKILL.md"
                header = (exported / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
                assert entry["frontmatter"] == dict(
                    line.split(": ", 1) for line in header.strip().splitlines()
                )
                assert len(entry["resources"]) == len(list(exported.rglob("*.md")))
                for resource in entry["resources"]:
                    assert set(resource) == {"uri", "size", "digest"}
                    read = await client.read_resource(resource["uri"])
                    raw = read.contents[0].text.encode("utf-8")
                    assert resource["digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
                    assert resource["size"] == len(raw)
                    relative = resource["uri"].removeprefix(f"pyxsd://skills/{name}/")
                    assert raw == (exported / relative).read_bytes()
                fetched = await client.send_request(
                    types.Request(method="skills/get", params=Lookup(uri=entry["uri"])),
                    TypeAdapter(dict),
                )
                assert fetched["skill"] == entry
                assert fetched["resultType"] == "complete"
                assert fetched["cacheScope"] == "public"
                assert fetched["ttlMs"] == response["ttlMs"]

    anyio.run(scenario)


@pytest.mark.parametrize(
    "uri",
    [
        "pyxsd-development",
        "pyxsd://skills/../SKILL.md",
        "pyxsd://skills/pyxsd-development/references/constraints.md",
    ],
)
def test_final_lookup_rejects_non_skill_uris(tmp_path, uri):
    async def scenario():
        async with connected(tmp_path, modern=True) as (client, _, _discovery):
            with pytest.raises(MCPError) as caught:
                await client.send_request(
                    types.Request(method="skills/get", params=Lookup(uri=uri)), TypeAdapter(dict)
                )
            assert caught.value.code == -32602

    anyio.run(scenario)


def test_compatibility_fixture_pins_final_source():
    fixture = json.loads((Path(__file__).parent / "fixtures/skills-extension.json").read_text())
    assert fixture["identifier"] == "io.modelcontextprotocol/skills"
    assert fixture["specification"].endswith("specification/stable/skills.mdx")


def test_final_single_page_rejects_unknown_cursor(tmp_path):
    from pyxsd.mcp.skill_extension import SkillsListParams

    async def scenario():
        async with connected(tmp_path, modern=True) as (client, _services, _discovered):
            with pytest.raises(MCPError) as caught:
                await client.send_request(
                    types.Request(method="skills/list", params=SkillsListParams(cursor="missing")),
                    TypeAdapter(dict),
                )
            assert caught.value.code == -32602

    anyio.run(scenario)


def test_skill_file_reader_rejects_noncanonical_directory():
    from pyxsd.mcp.skill_extension import _skill_files

    with pytest.raises(MCPError, match="Unknown skill") as caught:
        _skill_files("references")
    assert caught.value.code == -32602
