"""Final skills extension (SEP-2640) through the official SDK seam.

Extension methods are additive only: ``skills/list`` and ``skills/get`` never
shadow protocol discovery, and reading skill content never activates a skill.
"""

import hashlib
from typing import Any

from mcp.server.context import ServerRequestContext
from mcp.server.extension import Extension, MethodBinding, ResourceBinding
from mcp.server.mcpserver.resources import TextResource
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, RequestParams

from pydantic import Field

from ..skills import list_skills

IDENTIFIER = "io.modelcontextprotocol/skills"


class SkillsListParams(RequestParams):
    cursor: str | None = None


class SkillsGetParams(RequestParams):
    uri: str = Field(min_length=1)


def _text(resource: Any) -> str:
    return resource.read_bytes().decode("utf-8")


def _skill_files(name: str) -> dict[str, str]:
    from importlib.resources import files

    if name not in {skill.name for skill in list_skills()}:
        raise MCPError(INVALID_PARAMS, f"Unknown skill {name!r}")
    skill = files("pyxsd.skills").joinpath(name)
    collected: dict[str, str] = {"SKILL.md": _text(skill.joinpath("SKILL.md"))}
    references = files("pyxsd.skills").joinpath("references")
    for child in sorted(references.iterdir(), key=lambda item: item.name):
        if child.is_file():
            collected[f"references/{child.name}"] = _text(child)
    return collected


class SkillsExtension(Extension):
    identifier = IDENTIFIER

    def _manifest(self, name: str) -> dict[str, Any]:
        metadata = next(skill for skill in list_skills() if skill.name == name)
        return {
            "uri": f"pyxsd://skills/{name}/SKILL.md",
            # These are all fields in the packaged scalar frontmatter. Tests
            # compare the complete authored header against the wire value.
            "frontmatter": {"name": metadata.name, "description": metadata.description},
            "resources": [
                {
                    "uri": f"pyxsd://skills/{name}/{path}",
                    "size": len(content.encode("utf-8")),
                    "digest": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
                for path, content in sorted(_skill_files(name).items())
            ],
        }

    def resources(self) -> tuple[ResourceBinding, ...]:
        return tuple(
            ResourceBinding(
                TextResource(
                    uri=f"pyxsd://skills/{skill.name}/{path}",
                    name=f"{skill.name}/{path}",
                    mime_type="text/markdown",
                    text=content,
                )
            )
            for skill in list_skills()
            for path, content in sorted(_skill_files(skill.name).items())
        )

    def methods(self) -> tuple[MethodBinding, ...]:
        async def list_handler(
            _context: ServerRequestContext, params: SkillsListParams
        ) -> dict[str, Any]:
            if params.cursor is not None:
                raise MCPError(INVALID_PARAMS, "Unknown skills cursor")
            return {
                "resultType": "complete",
                "ttlMs": 60000,
                "cacheScope": "public",
                "skills": [self._manifest(skill.name) for skill in list_skills()],
            }

        async def get_handler(
            _context: ServerRequestContext, params: SkillsGetParams
        ) -> dict[str, Any]:
            names = {f"pyxsd://skills/{skill.name}/SKILL.md": skill.name for skill in list_skills()}
            if params.uri not in names:
                raise MCPError(INVALID_PARAMS, f"Unknown skill URI {params.uri!r}")
            return {
                "resultType": "complete",
                "ttlMs": 60000,
                "cacheScope": "public",
                "skill": self._manifest(names[params.uri]),
            }

        return (
            MethodBinding("skills/list", SkillsListParams, list_handler),
            MethodBinding("skills/get", SkillsGetParams, get_handler),
        )
