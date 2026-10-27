"""Read-only references, immutable artifacts and optional workflow prompts."""

import json
from typing import TYPE_CHECKING

from .services.exploration import _EXPLANATIONS
from .tools import capability_data

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from .server import Services


def register_resources(server: "MCPServer", services: "Services") -> None:
    @server.resource(
        "pyxsd://issue-codes", name="Issue-code reference", mime_type="application/json"
    )
    def issue_codes() -> str:
        return json.dumps(_EXPLANATIONS, ensure_ascii=False, separators=(",", ":"))

    @server.resource(
        "pyxsd://capabilities", name="Effective capabilities", mime_type="application/json"
    )
    def capabilities() -> str:
        return json.dumps(capability_data(services), ensure_ascii=False, separators=(",", ":"))

    @server.resource(
        "pyxsd://artifacts/{artifact_id}",
        name="Immutable artifact",
        mime_type="application/octet-stream",
    )
    async def artifact(artifact_id: str) -> bytes:
        async with services.artifacts.lease(services.principal, artifact_id) as reader:
            chunks = []
            for offset in range(0, reader.ref.size, services.settings.limits.result_bytes):
                chunks.append(
                    await reader.read(
                        offset=offset,
                        length=min(services.settings.limits.result_bytes, reader.ref.size - offset),
                    )
                )
            return b"".join(chunks)

    @server.prompt(name="pyxsd_diagnose_xml", description="Diagnose an XML document against an XSD")
    def diagnose_xml(schema_ref: str, document_ref: str) -> str:
        return (
            f"Validate {document_ref} against {schema_ref}. Inspect exact issue locations "
            "and constraint evidence, preserve repeated-node identity, and distinguish "
            "malformed XML from recoverable validation findings. Revalidate after edits."
        )

    @server.prompt(
        name="pyxsd_author_pipeline", description="Author a bounded XML transform pipeline"
    )
    def author_pipeline(goal: str) -> str:
        return (
            f"Design a minimal XML transformation pipeline for: {goal}. Use registered "
            "transforms or explicit reviewed code, preview the candidate, inspect warnings "
            "and independently revalidate before any publication step."
        )

    @server.prompt(name="pyxsd_evaluate_migration", description="Evaluate an XSD migration")
    def evaluate_schema_migration(old_schema: str, new_schema: str) -> str:
        return (
            f"Compare {old_schema} with {new_schema}. Separate structural compatibility "
            "risks from observed corpus regressions, and validate any transformed candidates "
            "against the target schema. Do not claim universal compatibility from a sample."
        )
