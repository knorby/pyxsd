"""SEP-2133 extension exposing pyxsd skills through the official SDK seam.

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

from .. import __version__
from ..skills import list_skills

IDENTIFIER = "com.knorby.pyxsd/skills"


class SkillsListParams(RequestParams):
    pass


class SkillsGetParams(RequestParams):
    name: str = Field(min_length=1, max_length=64)


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

    def settings(self) -> dict[str, Any]:
        return {"version": __version__, "methods": ["skills/list", "skills/get"]}

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
            _context: ServerRequestContext, _params: SkillsListParams
        ) -> dict[str, Any]:
            return {
                "skills": [
                    {"name": skill.name, "description": skill.description}
                    for skill in list_skills()
                ]
            }

        async def get_handler(
            _context: ServerRequestContext, params: SkillsGetParams
        ) -> dict[str, Any]:
            files = _skill_files(params.name)
            manifest = [
                {
                    "path": path,
                    "uri": f"pyxsd://skills/{params.name}/{path}",
                    "size": len(content.encode("utf-8")),
                    "digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
                for path, content in sorted(files.items())
            ]
            return {"files": files, "manifest": manifest}

        return (
            MethodBinding("skills/list", SkillsListParams, list_handler),
            MethodBinding("skills/get", SkillsGetParams, get_handler),
        )
