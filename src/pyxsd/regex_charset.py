"""XSD regular-expression character-class semantics for Python ``re``.

elementpath translates an XSD pattern to a Python regex but leaves the
single-character shorthands ``\\w``, ``\\W``, ``\\s`` and ``\\S`` for
Python to interpret.  Python's definitions differ from XML Schema's
(XSD 1.1 Appendix G.4.2):

* ``\\w`` is every character *except* those in the punctuation, separator
  and other categories, so combining marks (``M*``) and symbols (``S*``)
  are word characters — including on the astral planes — while ``_``
  (``Pc``) is not;
* ``\\s`` is exactly ``[#x20\\t\\n\\r]``.

This module rewrites the *translated* pattern (the last pyxsd-controlled
step before ``re.compile``), expanding each shorthand into an explicit
class computed from :mod:`unicodedata` over the whole Unicode range.  It
also rejects a category/block escape with an empty block name (``\\p{Is}``),
which does not match the ``IsBlock`` production and so is not a legal XSD
regular expression.
"""

from __future__ import annotations

import unicodedata
from functools import cache

_MAX_UNICODE = 0x10FFFF

#: The XSD ``\\s`` set: space, tab, line feed and carriage return.
_SPACE_CODEPOINTS = (0x20, 0x09, 0x0A, 0x0D)


def _scan_ranges(keep) -> list[tuple[int, int]]:
    """Inclusive codepoint ranges for which *keep* is true."""
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    last = 0
    for codepoint in range(_MAX_UNICODE + 1):
        if keep(chr(codepoint)):
            if start is None:
                start = codepoint
            last = codepoint
        elif start is not None:
            ranges.append((start, last))
            start = None
    if start is not None:
        ranges.append((start, last))
    return ranges


def _complement(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The complement of *ranges* over ``0..0x10FFFF``."""
    out: list[tuple[int, int]] = []
    next_cp = 0
    for low, high in sorted(ranges):
        if low > next_cp:
            out.append((next_cp, low - 1))
        next_cp = high + 1
    if next_cp <= _MAX_UNICODE:
        out.append((next_cp, _MAX_UNICODE))
    return out


def _is_word(codepoint: str) -> bool:
    return unicodedata.category(codepoint)[0] not in ("P", "Z", "C")


@cache
def _class_contents(name: str) -> str:
    """A Python class body (no brackets) for one XSD shorthand class."""
    if name in ("word", "nonword"):
        word = _scan_ranges(_is_word)
        ranges = word if name == "word" else _complement(word)
    else:
        space = [(cp, cp) for cp in _SPACE_CODEPOINTS]
        ranges = space if name == "space" else _complement(space)
    return "".join(_escape_range(low, high) for low, high in ranges)


def _escape_range(low: int, high: int) -> str:
    if low == high:
        return _escape(low)
    return f"{_escape(low)}-{_escape(high)}"


def _escape(codepoint: int) -> str:
    if codepoint < 0x100:
        return f"\\x{codepoint:02x}"
    if codepoint < 0x10000:
        return f"\\u{codepoint:04x}"
    return f"\\U{codepoint:08x}"


#: Translated-pattern shorthand -> (outside-class class, inside-class body).
_INSIDE = {
    "w": lambda: _class_contents("word"),
    "W": lambda: _class_contents("nonword"),
    "s": lambda: _class_contents("space"),
    "S": lambda: _class_contents("nonspace"),
}
_OUTSIDE_NAME = {"w": "word", "W": "nonword", "s": "space", "S": "nonspace"}


def rewrite_xsd_shorthands(pattern: str) -> str:
    """Expand ``\\w``/``\\W``/``\\s``/``\\S`` to explicit XSD classes.

    *pattern* is an already-translated Python regex (elementpath leaves these
    shorthands untouched).  Inside a character class the contents are spliced
    in so the surrounding brackets stay balanced; outside, a bracketed class
    is emitted.  Escaped backslashes and expanded ``\\p{...}`` classes are
    copied verbatim.
    """
    out: list[str] = []
    depth = 0
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        if char == "\\" and i + 1 < length:
            nxt = pattern[i + 1]
            if nxt == "\\":
                out.append("\\\\")
                i += 2
                continue
            if nxt in _INSIDE:
                if depth:
                    out.append(_INSIDE[nxt]())
                else:
                    out.append(f"[{_class_contents(_OUTSIDE_NAME[nxt])}]")
                i += 2
                continue
            out.append(char)
            out.append(nxt)
            i += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(depth - 1, 0)
        out.append(char)
        i += 1
    return "".join(out)


class RegexEncodingError(ValueError):
    """A pattern element that is not a legal XSD regular expression."""


def reject_malformed_escapes(pattern: str) -> None:
    """Reject a ``\\p{}``/``\\P{}`` escape with an empty block name.

    ``Is`` with no following block-name characters does not match the XSD
    ``IsBlock`` production, so ``\\p{Is}`` is not a regular expression and
    the schema is in error (XSD 1.1 Appendix G.4.2.3).  elementpath would
    otherwise accept it and treat it as "all characters".
    """
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        if char == "\\" and i + 1 < length:
            if pattern[i + 1] in ("p", "P") and i + 2 < length and pattern[i + 2] == "{":
                end = pattern.find("}", i + 3)
                if end == -1:
                    i += 2
                    continue
                if pattern[i + 3 : end] == "Is":
                    raise RegexEncodingError(
                        f"illegal XSD pattern {pattern!r}: empty block name in "
                        f"\\{pattern[i + 1]}{{Is}}"
                    )
                i = end + 1
                continue
            i += 2
            continue
        i += 1
