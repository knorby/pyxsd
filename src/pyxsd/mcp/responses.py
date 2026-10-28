"""Bound structured content and its text fallback as one result payload."""

import json

from pydantic import JsonValue

from .contracts import ArtifactRef, ToolEnvelope
from .errors import ToolError
from .output_store import OutputStore


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


async def render_result(envelope: ToolEnvelope, outputs: OutputStore) -> dict[str, JsonValue]:
    """Return SDK content fields, retaining oversized envelopes exactly once."""
    body = _json(envelope.model_dump(mode="json"))
    budget = outputs.limits.result_bytes
    if len(body) <= budget:
        result = _pack(envelope, body.decode("utf-8"))
        if len(_json(result)) <= budget:
            return result
    if len(body) > outputs.limits.output_bytes:
        raise ToolError("quota_exceeded", "Result exceeds the artifact output limit")

    placeholder = ArtifactRef(
        id="0" * 32,
        path="pending/result.json",
        media_type="application/json",
        size=len(body),
        digest="0" * 64,
        created_at=-1.7976931348623157e308,
        kind="result",
    )
    preview = _summary(envelope, len(body), placeholder)
    if len(_json(_pack(preview, _json(preview.model_dump(mode="json")).decode()))) + 64 > budget:
        raise ToolError("response_limit", "Response budget cannot contain an artifact reference")

    reference = await outputs.put_bytes(
        body, name="result.json", media_type="application/json", kind="result"
    )
    ref = ArtifactRef(
        id=reference.id,
        path=str(reference.path),
        media_type=reference.media_type,
        size=reference.size,
        digest=reference.digest,
        created_at=reference.created_at,
        kind=reference.kind,
    )
    summary = _summary(envelope, len(body), ref)
    return _pack(summary, _json(summary.model_dump(mode="json")).decode())
