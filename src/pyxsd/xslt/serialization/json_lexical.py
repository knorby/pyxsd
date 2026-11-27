"""JSON string lexemes and the shared JSON text parser.

The serializer-side helpers stay free of character maps and encoding policy.
The parse helpers preserve duplicate member pairs and raw number lexemes so
``parse-json``, ``json-doc``, and ``json-to-xml`` can implement the F&O 3.1
duplicate, escape, and lexical-number rules instead of losing them in a plain
``json.loads`` result.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias, cast

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

#: The short JSON escapes used for the special characters that have one.
_SHORT_ESCAPES = {
    "\b": r"\b",
    "\f": r"\f",
    "\n": r"\n",
    "\r": r"\r",
    "\t": r"\t",
}
_REPLACEMENT_CHARACTER = "\ufffd"


@dataclass(frozen=True, slots=True)
class JsonNumber:
    """One JSON number keeping the lexical form of the input."""

    lexical: str


@dataclass(frozen=True, slots=True)
class JsonObject:
    """One JSON object as ordered member pairs, duplicates included."""

    members: tuple[tuple[str, JsonValue], ...]


JsonValue: TypeAlias = str | bool | JsonNumber | JsonObject | list["JsonValue"] | None


def is_xml_character(character: str) -> bool:
    """Whether *character* is one XML 1.0 ``Char``."""
    code = ord(character)
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def json_escape_sequence(character: str) -> str:
    """The two- or six-character JSON escape for one special character."""
    short = _SHORT_ESCAPES.get(character)
    if short is not None:
        return short
    return f"\\u{ord(character):04X}"


def escape_json_value(value: str) -> str:
    """Escape string content with JSON's short and upper-case ``\\u`` escapes.

    Quotes and solidus are not special under F&O 3.1.  Control characters in
    the ranges ``x00``-``x1F`` and ``x7F``-``x9F``, characters that are not
    XML characters, and the backslash are escaped.
    """
    pieces: list[str] = []
    for character in value:
        if character == "\\":
            pieces.append("\\\\")
        elif character in _SHORT_ESCAPES:
            pieces.append(_SHORT_ESCAPES[character])
        elif (
            ord(character) < 0x20
            or 0x7F <= ord(character) <= 0x9F
            or not is_xml_character(character)
        ):
            pieces.append(json_escape_sequence(character))
        else:
            pieces.append(character)
    return "".join(pieces)


def render_json_string(
    value: str,
    *,
    escape: bool,
    fallback: Callable[[str], str] | None,
) -> str:
    """Render one parsed JSON string under the ``escape``/``fallback`` rules.

    With *escape* the special characters use JSON escape sequences.  Otherwise
    characters valid in XML stay literal and each invalid codepoint is replaced
    by ``fallback``'s result, or by U+FFFD when no fallback is supplied.
    """
    if escape:
        return escape_json_value(value)
    pieces: list[str] = []
    for character in value:
        if is_xml_character(character):
            pieces.append(character)
        elif fallback is None:
            pieces.append(_REPLACEMENT_CHARACTER)
        else:
            pieces.append(fallback(json_escape_sequence(character)))
    return "".join(pieces)


def preescape_non_xml_characters(text: str) -> str:
    """Replace XML-invalid codepoints by their JSON escape sequences.

    ``json-doc`` reads octets that may decode to codepoints XML cannot hold.
    Turning each one into the corresponding escape keeps the JSON grammar
    parseable; the escape is decoded again before the fallback rules apply.
    """
    return "".join(
        character if is_xml_character(character) else json_escape_sequence(character)
        for character in text
    )


def parse_json_text(text: str, *, liberal: bool = False) -> JsonValue:
    """Parse one JSON text, preserving duplicate pairs and number lexemes.

    A leading byte order mark is ignored.  ``NaN`` and the infinities are not
    JSON and are rejected.  A malformed text raises :class:`json.JSONDecodeError`;
    the caller maps it to the function's own error code.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    return cast(
        JsonValue,
        json.loads(
            text,
            object_pairs_hook=lambda pairs: JsonObject(tuple(pairs)),
            parse_float=JsonNumber,
            parse_int=JsonNumber,
            parse_constant=_reject_json_constant,
            strict=not liberal,
        ),
    )


def _reject_json_constant(value: str) -> None:
    """Reject NaN and the infinities outside JSON's number grammar."""
    raise ValueError(f"non-finite JSON number {value!r}")


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
