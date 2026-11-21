"""Longest-match tokenization for the RELAX NG compact syntax.

Appendix A.2 stage 5 finds, at each position, the longest initial subsequence
that matches a literal terminal, one of the named lexical terminals, or the
separator production.  A separator is discarded; every other match becomes
one :class:`Token`.  Documentation is a named terminal, so adjacent ``##``
lines group into one token exactly as the specification's continuation
grammar describes.

The lexer owns no module-level state.  One
:class:`~pyxsd.relaxng.compact.source.CompactSource` and one
:class:`~pyxsd.relaxng.limits.BudgetCounter` are supplied by the caller, and
every compilation gets its own instances.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from pyxsd.locations import IssueLocation, SourceTextSpan
from pyxsd.relaxng.compact.source import (
    Character,
    CompactSource,
    NewlineMarker,
    is_xml_char,
)
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError
from pyxsd.relaxng.limits import BudgetCounter
from pyxsd.relaxng.syntax import _NCNAME_CHAR, _NCNAME_START, _in_ranges

__all__ = [
    "KEYWORDS",
    "Token",
    "TokenKind",
    "lex_compact",
]

#: The compact syntax's reserved words, from Appendix A.1 ``keyword``.
KEYWORDS = frozenset(
    {
        "attribute",
        "default",
        "datatypes",
        "div",
        "element",
        "empty",
        "external",
        "grammar",
        "include",
        "inherit",
        "list",
        "mixed",
        "namespace",
        "notAllowed",
        "parent",
        "start",
        "string",
        "text",
        "token",
    }
)

#: Single-character literal terminals.  ``|``, ``&``, and ``>`` have longer
#: forms and are dispatched separately.
_SINGLE_SYMBOLS = frozenset("{}()[],=?*+-~")


class TokenKind(StrEnum):
    """The lexical terminal classes a compact token can belong to."""

    IDENTIFIER = "identifier"
    QUOTED_IDENTIFIER = "quoted-identifier"
    CNAME = "cname"
    NS_NAME = "ns-name"
    LITERAL_SEGMENT = "literal-segment"
    DOCUMENTATION = "documentation"
    KEYWORD = "keyword"
    SYMBOL = "symbol"
    EOF = "eof"


@dataclass(frozen=True)
class Token:
    """One emitted compact token.

    ``value`` is the semantic value: an unquoted NCName for identifiers, the
    NCName without its backslash for quoted identifiers, ``prefix:local`` for
    a CName, the prefix for an nsName, the content for a literal segment and
    documentation token, and the terminal text for keywords and symbols.  A
    token's span always covers its complete original-source extent, including
    any escape sequences or delimiters it contains.
    """

    kind: TokenKind
    value: str
    span: SourceTextSpan


def lex_compact(
    content: bytes,
    *,
    source_id: str,
    budget: BudgetCounter,
) -> tuple[Token, ...]:
    """Tokenize one compact source, charging every non-EOF token to *budget*."""
    source = CompactSource.from_bytes(content, source_id=source_id)
    return _Lexer(source, budget).tokenize()


def _is_name_start(character: Character | None) -> bool:
    return isinstance(character, str) and _in_ranges(ord(character), _NCNAME_START)


def _is_name_character(character: Character | None) -> bool:
    return isinstance(character, str) and _in_ranges(ord(character), _NCNAME_CHAR)


def _is_separator_space(character: Character) -> bool:
    return isinstance(character, NewlineMarker) or character in "\t\n "


def _is_line_character(character: Character | None) -> bool:
    """Whether *character* can appear in a comment or documentation line."""
    return isinstance(character, str) and character != "\n" and is_xml_char(character)


class _Lexer:
    def __init__(self, source: CompactSource, budget: BudgetCounter) -> None:
        self._source = source
        self._budget = budget
        self._characters = source.characters
        self._count = len(self._characters)
        self._tokens: list[Token] = []
        self._index = 0

    def tokenize(self) -> tuple[Token, ...]:
        while self._index < self._count:
            character = self._characters[self._index]
            if _is_separator_space(character):
                self._index += 1
            elif character == "#":
                self._scan_hash()
            elif isinstance(character, str) and character in "\"'":
                self._scan_literal(character)
            elif character == "\\":
                self._scan_quoted_identifier()
            elif _is_name_start(character):
                self._scan_name()
            elif character == ">":
                self._scan_follow_annotation_arrow()
            elif character == "|":
                self._scan_assignment_symbol("|")
            elif character == "&":
                self._scan_assignment_symbol("&")
            elif isinstance(character, str) and character in _SINGLE_SYMBOLS:
                self._emit(TokenKind.SYMBOL, character, self._index, self._index + 1)
                self._index += 1
            else:
                self._fail(
                    "no token matches this character",
                    self._index,
                    self._index + 1,
                    found=repr(character),
                )
        self._tokens.append(Token(TokenKind.EOF, "", self._source.span(self._count, self._count)))
        return tuple(self._tokens)

    # -- separators and documentation ------------------------------------

    def _scan_hash(self) -> None:
        start = self._index
        if self._peek_at(start + 1) == "#":
            value, end = self._scan_documentation(start)
            self._emit(TokenKind.DOCUMENTATION, value, start, end)
            self._index = end
        else:
            self._index = self._comment_end(start)

    def _comment_end(self, start: int) -> int:
        cursor = start + 1
        if not _is_line_character(self._peek_at(cursor)):
            return cursor
        cursor += 1
        while _is_line_character(self._peek_at(cursor)):
            cursor += 1
        return cursor

    def _scan_documentation(self, start: int) -> tuple[str, int]:
        value, cursor = self._scan_documentation_line(start + 2)
        lines = [value]
        while True:
            continuation = self._documentation_continuation(cursor)
            if continuation is None:
                return "\n".join(lines), cursor
            value, cursor = self._scan_documentation_line(continuation)
            lines.append(value)

    def _documentation_continuation(self, cursor: int) -> int | None:
        character = self._peek_at(cursor)
        if character is None:
            return None
        if not isinstance(character, NewlineMarker) and character != "\n":
            return None
        cursor += 1
        while self._peek_at(cursor) in ("\t", " "):
            cursor += 1
        if self._peek_at(cursor) == "#" and self._peek_at(cursor + 1) == "#":
            return cursor + 2
        return None

    def _scan_documentation_line(self, start: int) -> tuple[str, int]:
        cursor = start
        while self._peek_at(cursor) == "#":
            cursor += 1
        character = self._peek_at(cursor)
        if character == " ":
            return self._scan_rest_of_line(cursor + 1)
        if character is None or isinstance(character, NewlineMarker):
            return "", cursor
        if character == "\n" or not is_xml_char(character):
            return "", cursor
        rest, cursor = self._scan_rest_of_line(cursor + 1)
        return f"{character}{rest}", cursor

    def _scan_rest_of_line(self, start: int) -> tuple[str, int]:
        parts: list[str] = []
        cursor = start
        while True:
            character = self._peek_at(cursor)
            if not isinstance(character, str) or character == "\n" or not is_xml_char(character):
                return "".join(parts), cursor
            parts.append(character)
            cursor += 1

    # -- literals ---------------------------------------------------------

    def _scan_literal(self, delimiter: str) -> None:
        start = self._index
        if self._peek_at(start + 1) == delimiter and self._peek_at(start + 2) == delimiter:
            triple = self._scan_triple_literal(start, delimiter)
            if triple is not None:
                value, end = triple
                self._emit(TokenKind.LITERAL_SEGMENT, value, start, end)
                self._index = end
                return
        value, end = self._scan_single_literal(start, delimiter)
        self._emit(TokenKind.LITERAL_SEGMENT, value, start, end)
        self._index = end

    def _scan_single_literal(self, start: int, delimiter: str) -> tuple[str, int]:
        cursor = start + 1
        parts: list[str] = []
        while True:
            character = self._peek_at(cursor)
            if character is None:
                self._fail_literal(start, delimiter, cursor)
            if character == delimiter:
                return "".join(parts), cursor + 1
            if isinstance(character, NewlineMarker):
                self._fail_literal(start, delimiter, cursor + 1)
            if not is_xml_char(character):
                self._fail_literal(start, delimiter, cursor + 1)
            parts.append(character)
            cursor += 1

    def _scan_triple_literal(self, start: int, delimiter: str) -> tuple[str, int] | None:
        cursor = start + 3
        parts: list[str] = []
        while True:
            character = self._peek_at(cursor)
            if character is None:
                return None
            if character == delimiter:
                if (
                    self._peek_at(cursor + 1) == delimiter
                    and self._peek_at(cursor + 2) == delimiter
                ):
                    return "".join(parts), cursor + 3
                if self._peek_at(cursor + 1) == delimiter:
                    parts.append(delimiter * 2)
                    cursor += 2
                else:
                    parts.append(delimiter)
                    cursor += 1
                continue
            if isinstance(character, NewlineMarker):
                parts.append("\n")
            elif is_xml_char(character):
                parts.append(character)
            else:
                return None
            cursor += 1

    # -- names ------------------------------------------------------------

    def _scan_quoted_identifier(self) -> None:
        start = self._index
        if not _is_name_start(self._peek_at(start + 1)):
            self._fail(
                "no token matches this character",
                start,
                start + 1,
                found=repr("\\"),
            )
        end = self._scan_ncname(start + 1)
        self._emit(TokenKind.QUOTED_IDENTIFIER, self._joined(start + 1, end), start, end)
        self._index = end

    def _scan_name(self) -> None:
        start = self._index
        end = self._scan_ncname(start)
        lexical = self._joined(start, end)
        if self._peek_at(end) == ":":
            following = self._peek_at(end + 1)
            if following == "*":
                self._emit(TokenKind.NS_NAME, lexical, start, end + 2)
                self._index = end + 2
                return
            if _is_name_start(following):
                local_end = self._scan_ncname(end + 1)
                self._emit(
                    TokenKind.CNAME,
                    f"{lexical}:{self._joined(end + 1, local_end)}",
                    start,
                    local_end,
                )
                self._index = local_end
                return
        kind = TokenKind.KEYWORD if lexical in KEYWORDS else TokenKind.IDENTIFIER
        self._emit(kind, lexical, start, end)
        self._index = end

    def _scan_ncname(self, start: int) -> int:
        cursor = start
        while _is_name_character(self._peek_at(cursor)):
            cursor += 1
        return cursor

    # -- symbols ----------------------------------------------------------

    def _scan_follow_annotation_arrow(self) -> None:
        start = self._index
        if self._peek_at(start + 1) != ">":
            self._fail(
                "no token matches this character",
                start,
                start + 1,
                found=repr(">"),
            )
        self._emit(TokenKind.SYMBOL, ">>", start, start + 2)
        self._index = start + 2

    def _scan_assignment_symbol(self, symbol: str) -> None:
        start = self._index
        if self._peek_at(start + 1) == "=":
            self._emit(TokenKind.SYMBOL, f"{symbol}=", start, start + 2)
            self._index = start + 2
            return
        self._emit(TokenKind.SYMBOL, symbol, start, start + 1)
        self._index = start + 1

    # -- shared helpers ---------------------------------------------------

    def _peek_at(self, index: int) -> Character | None:
        return self._characters[index] if index < self._count else None

    def _joined(self, start: int, end: int) -> str:
        return "".join(
            "\n" if isinstance(character, NewlineMarker) else character
            for character in self._characters[start:end]
        )

    def _emit(self, kind: TokenKind, value: str, start: int, end: int) -> None:
        self._budget.consume("max_compact_tokens")
        self._tokens.append(Token(kind, value, self._source.span(start, end)))

    def _fail_literal(self, start: int, delimiter: str, end: int) -> NoReturn:
        self._fail(
            "a literal segment is not terminated",
            start,
            end,
            found=repr(delimiter),
        )

    def _fail(
        self,
        message: str,
        start: int,
        end: int,
        *,
        found: str | None = None,
    ) -> NoReturn:
        end = min(end, self._count)
        raise RelaxNGCompactSyntaxError(
            message,
            location=IssueLocation(
                self._source.source_id,
                (),
                span=self._source.span(start, end),
            ),
            found=found,
        )
