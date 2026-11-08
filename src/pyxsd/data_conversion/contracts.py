"""Shared format contracts and diagnostics for JSON data conversion.

The public format names, fidelity modes, JSON-safe value shape, and diagnostic
helpers live here so export, import, and JSON Schema consume one contract
rather than restating the rules independently.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from typing import Literal, NoReturn, TypeAlias

from pyxsd.exceptions import PyXSDError, PyXSDWarning

#: Every format accepted by the conversion APIs.
FormatName = Literal["pyxsd", "pyxsd-json", "parker", "badgerfish", "jsonml"]

#: Whether a conversion may apply its documented representational fallbacks.
Fidelity = Literal["strict", "best-effort"]

#: A value that survives ``json.dumps()`` without a fallback encoder.
JsonValue: TypeAlias = bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None

_FORMAT_NAMES: tuple[FormatName, ...] = (
    "pyxsd",
    "pyxsd-json",
    "parker",
    "badgerfish",
    "jsonml",
)

_FIDELITY_NAMES: tuple[Fidelity, ...] = ("strict", "best-effort")


def validate_format(format: str) -> FormatName:
    """Return *format* as a validated public format name.

    Raises ``ValueError`` for a name outside the published contract.
    """
    if format not in _FORMAT_NAMES:
        raise ValueError(f"unknown data format: {format!r}")
    return format


def validate_fidelity(fidelity: str) -> Fidelity:
    """Return *fidelity* as a validated fidelity mode.

    Raises ``ValueError`` for a mode outside the published contract.
    """
    if fidelity not in _FIDELITY_NAMES:
        raise ValueError(f"unknown fidelity mode: {fidelity!r}")
    return fidelity


def json_pointer(parts: Iterable[str | int]) -> str:
    """Build an RFC 6901 JSON Pointer from its unescaped path segments.

    ``~`` and ``/`` inside a segment are escaped so the result denotes one
    path, not several, and joins to the same path on every call.
    """
    segments = []
    for part in parts:
        token = str(part).replace("~", "~0").replace("/", "~1")
        segments.append("/" + token)
    return "".join(segments)


def _diagnostic_text(code: str, path: str, message: str) -> str:
    return f"{code} at {path!r}: {message}"


def raise_conversion(code: str, path: str, message: str) -> NoReturn:
    """Raise a conversion ``PyXSDError`` naming its stable reason code.

    *path* is an RFC 6901 JSON Pointer, so callers join their traversal
    segments with :func:`json_pointer`. *message* carries any XML element
    identity or other detail available at the call site.
    """
    raise PyXSDError(_diagnostic_text(code, path, message))


def warn_conversion(code: str, path: str, message: str) -> None:
    """Emit a best-effort conversion warning with the same diagnostic shape."""
    warnings.warn(_diagnostic_text(code, path, message), PyXSDWarning, stacklevel=2)
