"""Bounded JSON frames shared by process dispatchers and clean workers.

Callers supply a strict role-specific wire model and channel identity bindings.
This codec is not authentication or a sandbox; the process pool must choose the
model and bindings, never accept either from a tool argument or worker reply.
"""

import json
from typing import NoReturn, TypeVar

from pydantic import JsonValue

from .contracts import WireModel
from .errors import ToolError

T = TypeVar("T", bound=WireModel)


def _budget(max_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ToolError("invalid_argument", "IPC byte limit must be a positive integer")


def _object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> NoReturn:
    raise ValueError("Nonfinite JSON number")


def encode_frame(message: WireModel, *, max_bytes: int) -> bytes:
    """Serialize one wire model, including its newline in the byte budget."""
    _budget(max_bytes)
    if not isinstance(message, WireModel):
        raise ToolError("ipc_protocol", "IPC requires a wire model")
    try:
        frame = (
            json.dumps(
                message.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise ToolError("ipc_protocol", "IPC message cannot be encoded") from exc
    if len(frame) > max_bytes:
        raise ToolError("ipc_limit", "IPC frame exceeds its byte limit")
    return frame


def decode_frame(
    frame: bytes,
    model: type[T],
    *,
    max_bytes: int,
    expected: dict[str, str],
) -> T:
    """Validate one complete frame against the channel's model and identities.

    Stream readers must bound allocation before calling this function. The byte
    check here is defense in depth, not permission to read an unbounded line.
    Validation errors deliberately omit payloads, paths and generated tracebacks.
    """
    _budget(max_bytes)
    if len(frame) > max_bytes:
        raise ToolError("ipc_limit", "IPC frame exceeds its byte limit")
    try:
        if not frame.endswith(b"\n") or frame.count(b"\n") != 1:
            raise ValueError("Incomplete frame")
        data = json.loads(
            frame.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
        if not isinstance(data, dict) or any(
            key not in data or data[key] != value for key, value in expected.items()
        ):
            raise ValueError("Channel identity mismatch")
        return model.model_validate(data)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ToolError("ipc_protocol", "Invalid IPC frame or channel identity") from exc
