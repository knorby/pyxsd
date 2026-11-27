"""JSON string lexemes without character maps or encoding policy."""

from __future__ import annotations

import json
import re
from typing import cast

_JSON_ESCAPES = {
    **{chr(code): f"\\u{code:04X}" for code in range(0x20)},
    **{chr(code): f"\\u{code:04X}" for code in range(0x7F, 0xA0)},
    '"': r"\"",
    "\\": r"\\",
    "\b": r"\b",
    "\f": r"\f",
    "\n": r"\n",
    "\r": r"\r",
    "\t": r"\t",
}
_SOLIDUS_ESCAPE = r"\/"
_ORDINARY_TRANSLATION = str.maketrans(_JSON_ESCAPES)
_PREESCAPED_TRANSLATION = str.maketrans({**_JSON_ESCAPES, "/": _SOLIDUS_ESCAPE})
_ESCAPE = re.compile(r'\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4})|\\')


def escape_json_string(value: str, *, preescaped: bool) -> str:
    """Escape string content without quotes, preserving validated pre-existing escapes.

    Ordinary content leaves solidus alone. Pre-escaped content also escapes
    raw solidus and raises :class:`ValueError` for a malformed backslash escape.
    """
    if not preescaped:
        return value.translate(_ORDINARY_TRANSLATION)

    pieces: list[str] = []
    end = 0
    for match in _ESCAPE.finditer(value):
        escape = match[0]
        if len(escape) == 1:
            raise ValueError(f"invalid JSON escape at character {match.start() + 1}")
        pieces.append(value[end : match.start()].translate(_PREESCAPED_TRANSLATION))
        pieces.append(escape)
        end = match.end()
    pieces.append(value[end:].translate(_PREESCAPED_TRANSLATION))
    return "".join(pieces)


def decode_json_escapes(escaped: str) -> str:
    """Decode escaped JSON string content, combining adjacent surrogate pairs."""
    return cast(str, json.loads(f'"{escaped}"'))
