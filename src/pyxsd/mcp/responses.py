"""Bound structured content and its text fallback as one result payload."""

import json
from collections.abc import AsyncIterator

from pydantic import JsonValue

from .artifacts import ArtifactStore
from .contracts import ArtifactRef, ToolEnvelope
from .errors import ToolError
from .workspaces import Principal


def _json(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (ValueError, UnicodeError) as exc:
        raise ToolError("unsupported_value", "Result cannot be encoded as UTF-8 JSON") from exc


def _pack(envelope: ToolEnvelope, text: str) -> dict[str, JsonValue]:
    return {
        "structuredContent": envelope.model_dump(mode="json"),
        "content": [{"type": "text", "text": text}],
    }


def _summary(envelope: ToolEnvelope, size: int, ref: ArtifactRef) -> ToolEnvelope:
    return ToolEnvelope(
        status=envelope.status,
        data={"summary": "Full result is stored in the attached JSON artifact.", "bytes": size},
        artifacts=[ref],
    )


async def render_result(
    principal: Principal,
    workspace_id: str,
    envelope: ToolEnvelope,
    artifacts: ArtifactStore,
) -> dict[str, JsonValue]:
    """Return the SDK content fields, retaining oversized envelopes exactly once.

    Budgeting includes JSON escaping in the text fallback, not merely the data
    string's length. Large diagnostics, provenance and warnings live together in
    the retained envelope rather than being cut into invalid/ambiguous JSON.
    Resource authorization belongs to the operation producing the envelope.
    """
    principal.require("pyxsd:read")
    body = _json(envelope.model_dump(mode="json"))
    budget = artifacts.limits.result_bytes
    if len(body) <= budget:
        result = _pack(envelope, body.decode("utf-8"))
        if len(_json(result)) <= budget:
            return result
    if len(body) > artifacts.limits.output_bytes:
        raise ToolError("quota_exceeded", "Result exceeds the artifact output limit")

    # The concrete store issues 32-character IDs and SHA-256 digests. Reserve
    # the longest finite float spelling plus slack for the expiry timestamp,
    # before retaining anything for a budget too small to hold its reference.
    placeholder = ArtifactRef(
        id="0" * 32,
        digest="0" * 64,
        size=len(body),
        media_type="application/json",
        expires_at=-1.7976931348623157e308,
    )
    preview = _summary(envelope, len(body), placeholder)
    if len(_json(_pack(preview, _json(preview.model_dump(mode="json")).decode()))) + 64 > budget:
        raise ToolError("response_limit", "Response budget cannot contain an artifact reference")

    async def stream() -> AsyncIterator[bytes]:
        yield body

    ref = await artifacts.put(
        principal,
        workspace_id,
        stream(),
        media_type="application/json",
        limit=len(body),
        ttl=artifacts.limits.diagnostic_seconds,
    )
    summary = _summary(envelope, len(body), ref)
    return _pack(summary, _json(summary.model_dump(mode="json")).decode())
