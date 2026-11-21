"""Original-source decoding and address mapping for compact RELAX NG.

The compact syntax specification (OASIS, 2002-11-21, Appendix A.2) applies
five lexical stages before tokenization: character encoding, BOM stripping,
newline normalization, escape interpretation, and tokenization.  This module
owns the first four and keeps every expanded character addressable in the
original source text.  Tokenization itself lives in
:mod:`pyxsd.relaxng.compact.lexer`.

A normalized line break is represented by :data:`NEWLINE`, a marker that is
deliberately distinct from the U+000A character an escape such as ``\\x{A}``
can produce.  Escapes are replaced in a single left-to-right pass, so the
replacement character is never rescanned; there is no odd/even-backslash
rule.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Final

from pyxsd.locations import IssueLocation, SourceTextSpan
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError

__all__ = [
    "NEWLINE",
    "CompactSource",
    "NewlineMarker",
    "is_xml_char",
]

_MAX_XML_CODE_POINT = 0x10FFFF

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class NewlineMarker:
    """The normalized newline produced by Appendix A.2 stage 3."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NEWLINE"


#: The single normalized-newline value.  A marker is not a character.
NEWLINE: Final = NewlineMarker()

Character = str | NewlineMarker


def is_xml_char(character: str) -> bool:
    """Whether the single character *character* is an XML 1.0 ``Char``."""
    code = ord(character)
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= _MAX_XML_CODE_POINT
    )


def _decode(content: bytes, source_id: str) -> str:
    """Decode *content* with the encoding the Appendix A.2 rules select."""
    if content.startswith(b"\xff\xfe"):
        encoding, label = "utf-16-le", "UTF-16LE"
    elif content.startswith(b"\xfe\xff"):
        encoding, label = "utf-16-be", "UTF-16BE"
    else:
        encoding, label = "utf-8", "UTF-8"
    try:
        return content.decode(encoding)
    except UnicodeDecodeError as error:
        raise RelaxNGCompactSyntaxError(
            f"the compact source is not valid {label}: {error.reason} at byte {error.start}"
        ) from error


def _line_starts(text: str) -> tuple[int, ...]:
    """Return the original offset of each line start in *text*."""
    starts = [0]
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\r":
            if index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
            starts.append(index + 1)
        elif character == "\n":
            starts.append(index + 1)
        index += 1
    return tuple(starts)


def _position(line_starts: tuple[int, ...], offset: int) -> tuple[int, int]:
    """Return the one-based line and column of an original *offset*."""
    line = bisect_right(line_starts, offset)
    return line, offset - line_starts[line - 1] + 1


def _span(line_starts: tuple[int, ...], start: int, end: int) -> SourceTextSpan:
    line, column = _position(line_starts, start)
    end_line, end_column = _position(line_starts, end)
    return SourceTextSpan(start, end, line, column, end_line, end_column)


def _escape_open(text: str, index: int) -> bool:
    """Whether a backslash at *index* opens ``\\x+{``."""
    cursor = index + 1
    while cursor < len(text) and text[cursor] == "x":
        cursor += 1
    return cursor > index + 1 and cursor < len(text) and text[cursor] == "{"


def _escape_failure(
    source_id: str,
    line_starts: tuple[int, ...],
    message: str,
    start: int,
    end: int,
) -> RelaxNGCompactSyntaxError:
    return RelaxNGCompactSyntaxError(
        message,
        location=IssueLocation(source_id, (), span=_span(line_starts, start, end)),
    )


def _escape_value(
    text: str,
    index: int,
    *,
    source_id: str,
    line_starts: tuple[int, ...],
) -> tuple[str, int]:
    """Decode the escape sequence at *index* and return its end offset."""
    open_end = index + 1
    while text[open_end] == "x":
        open_end += 1
    digits_start = open_end + 1
    cursor = digits_start
    value = 0
    while cursor < len(text) and text[cursor] in _HEX_DIGITS:
        value = min(value * 16 + int(text[cursor], 16), _MAX_XML_CODE_POINT + 1)
        cursor += 1
    if cursor == digits_start:
        raise _escape_failure(
            source_id,
            line_starts,
            "an escape sequence needs at least one hexadecimal digit",
            index,
            digits_start + 1,
        )
    if cursor == len(text):
        raise _escape_failure(
            source_id,
            line_starts,
            "an escape sequence is missing its closing '}'",
            index,
            cursor,
        )
    if text[cursor] != "}":
        raise _escape_failure(
            source_id,
            line_starts,
            "an escape sequence may contain only hexadecimal digits before its closing '}'",
            index,
            cursor + 1,
        )
    if value > _MAX_XML_CODE_POINT or not is_xml_char(chr(value)):
        raise _escape_failure(
            source_id,
            line_starts,
            "the escape sequence does not denote a legal XML character",
            index,
            cursor + 1,
        )
    return chr(value), cursor + 1


def _expand(
    text: str,
    *,
    source_id: str,
    line_starts: tuple[int, ...],
) -> tuple[tuple[Character, ...], tuple[int, ...], tuple[int, ...]]:
    """Normalize newlines and expand escapes, recording original offsets."""
    characters: list[Character] = []
    starts: list[int] = []
    ends: list[int] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\r":
            end = index + 1
            if end < len(text) and text[end] == "\n":
                end += 1
            characters.append(NEWLINE)
        elif character == "\n":
            end = index + 1
            characters.append(NEWLINE)
        elif character == "\\" and _escape_open(text, index):
            character, end = _escape_value(
                text, index, source_id=source_id, line_starts=line_starts
            )
            characters.append(character)
        else:
            end = index + 1
            characters.append(character)
        starts.append(index)
        ends.append(end)
        index = end
    return tuple(characters), tuple(starts), tuple(ends)


@dataclass(frozen=True)
class CompactSource:
    """One compact source after newline normalization and escape expansion.

    ``text`` is the decoded, BOM-stripped source exactly as authored.
    ``characters`` is the expanded stage-3/4 sequence in which a normalized
    line break is :data:`NEWLINE` and every escaped character is one ordinary
    character.  :meth:`span` addresses ranges in ``characters`` and returns
    the corresponding original-text address; an escaped character's range
    covers its complete ``\\x{...}`` sequence.
    """

    source_id: str
    text: str
    characters: tuple[Character, ...]
    _starts: tuple[int, ...] = field(repr=False)
    _ends: tuple[int, ...] = field(repr=False)
    _line_starts: tuple[int, ...] = field(repr=False)

    @classmethod
    def from_bytes(cls, content: bytes, *, source_id: str) -> CompactSource:
        """Decode *content* and apply the pre-tokenization lexical stages."""
        text = _decode(content, source_id)
        if text.startswith("\ufeff"):
            text = text[1:]
        line_starts = _line_starts(text)
        characters, starts, ends = _expand(text, source_id=source_id, line_starts=line_starts)
        return cls(source_id, text, characters, starts, ends, line_starts)

    def span(self, start: int, end: int) -> SourceTextSpan:
        """Map the expanded half-open range ``[start, end)`` to its origin."""
        count = len(self.characters)
        if not 0 <= start <= end <= count:
            raise ValueError(f"expanded range {start}:{end} is outside 0:{count}")
        if start == end:
            offset = self._starts[start] if start < count else len(self.text)
            origin_end = offset
        else:
            offset = self._starts[start]
            origin_end = self._ends[end - 1]
        return _span(self._line_starts, offset, origin_end)
