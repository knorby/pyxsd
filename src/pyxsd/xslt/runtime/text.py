"""Normative XPath 3.0 regular expressions and ``xsl:analyze-string``.

The XPath regular-expression language is defined by XML Schema regular
expressions with the additions in XPath Functions and Operators 3.0 section
5.6.1.  Python's :mod:`re` is used only as the matching substrate: the source
pattern is first parsed against the XPath grammar, character classes and
Unicode categories are translated through elementpath's XSD character-class
machinery, and only the resulting Python pattern is handed to :mod:`re`.
Python-only constructs (lookaround, inline flags, named groups, conditionals)
are rejected rather than silently accepted.

``analyze_string`` implements the inter-character-position algorithm from XSLT
3.0 section 17.1, including zero-length matches and the rule that a matching
substring may be empty while a non-matching substring may not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, NoReturn

from elementpath.regex import CharacterClass, RegexError, UnicodeSubset, unicode_subset
from elementpath.regex.character_classes import (
    c_shortcut,
    d_shortcut,
    i_shortcut,
    s_shortcut,
    w_shortcut,
)

__all__ = [
    "AnalyzedSubstring",
    "CompiledXPathRegex",
    "XPathRegexError",
    "analyze_string",
    "compile_xpath_regex",
]

#: The only flags defined for XPath regular expressions.
VALID_FLAGS: Final[frozenset[str]] = frozenset("imsxq")

_XPATH_WHITESPACE: Final[frozenset[str]] = frozenset(" \t\r\n")
_SINGLE_ESCAPES: Final[dict[str, str]] = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "|": "|",
    ".": ".",
    "-": "-",
    "^": "^",
    "?": "?",
    "*": "*",
    "+": "+",
    "{": "{",
    "}": "}",
    "(": "(",
    ")": ")",
    "[": "[",
    "]": "]",
    "$": "$",
    "\\": "\\",
}
_QUANTIFIER = re.compile(r"\{(\d+)(?:,(\d*))?\}")
_PYTHON_META: Final[frozenset[str]] = frozenset(".^$?*+{}()[]|\\")


class XPathRegexError(ValueError):
    """One invalid XPath regular expression or flag string.

    ``code`` is the Functions and Operators error code (``FORX0001`` for
    flags, ``FORX0002`` for the pattern).  The XSLT instruction layer maps
    these to its own ``XTDE1145``/``XTDE1140`` codes.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AnalyzedSubstring:
    """One matching or non-matching piece of an analyzed string."""

    matched: bool
    value: str
    groups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.matched, bool):
            raise TypeError("matched must be a boolean")
        if not isinstance(self.value, str):
            raise TypeError("a substring value must be a string")
        if not isinstance(self.groups, tuple) or not all(
            isinstance(group, str) for group in self.groups
        ):
            raise TypeError("captured groups must be a tuple of strings")


@dataclass(frozen=True, slots=True)
class CompiledXPathRegex:
    """One XPath regular expression compiled for repeated position matching."""

    source: str
    flags: str
    pattern: re.Pattern[str] = field(repr=False)
    group_count: int

    def match(self, text: str, position: int) -> re.Match[str] | None:
        """Match at exactly *position*, never by searching forward."""
        return self.pattern.match(text, position)

    @staticmethod
    def groups(match: re.Match[str]) -> tuple[str, ...]:
        """Return the full match and every captured group, using empty strings."""
        return (match.group(0), *(group or "" for group in match.groups()))


def compile_xpath_regex(source: str, flags: str = "") -> CompiledXPathRegex:
    """Parse *source* against the XPath regex grammar and compile it.

    Raises :class:`XPathRegexError` with ``FORX0001`` for an undefined flag and
    ``FORX0002`` for a malformed pattern.
    """
    if not isinstance(source, str):
        raise TypeError("a regular expression source must be a string")
    if not isinstance(flags, str):
        raise TypeError("regular expression flags must be a string")
    for flag in flags:
        if flag not in VALID_FLAGS:
            raise XPathRegexError(
                f"the regular expression flag {flag!r} is not defined",
                code="FORX0001",
            )
    if "q" in flags:
        pattern = re.escape(source)
        group_count = 0
    else:
        translator = _Translator(source, flags)
        pattern = translator.translate()
        group_count = translator.group_count
    try:
        compiled = re.compile(pattern, re.IGNORECASE if "i" in flags else 0)
    except re.error as exc:  # pragma: no cover - the translator emits valid Python
        raise XPathRegexError(
            f"invalid XPath regular expression {source!r}: {exc}",
            code="FORX0002",
        ) from exc
    return CompiledXPathRegex(source, flags, compiled, group_count)


def analyze_string(
    regex: CompiledXPathRegex,
    text: str,
) -> tuple[AnalyzedSubstring, ...]:
    """Partition *text* into matching and non-matching substrings.

    This is the literal algorithm from XSLT 3.0 section 17.1.  A zero-length
    match at a non-final position consumes one character into the following
    non-matching substring so the scan always advances.
    """
    if not isinstance(regex, CompiledXPathRegex):
        raise TypeError("regex must be a CompiledXPathRegex")
    if not isinstance(text, str):
        raise TypeError("the analyzed value must be a string")
    result: list[AnalyzedSubstring] = []
    position = 0
    non_matching: list[str] = []
    while True:
        match = regex.match(text, position)
        if match is not None:
            if non_matching:
                result.append(AnalyzedSubstring(False, "".join(non_matching)))
                non_matching.clear()
            value = match.group(0)
            result.append(AnalyzedSubstring(True, value, regex.groups(match)))
            if value:
                position = match.end()
                continue
            if position == len(text):
                break
            non_matching.append(text[position])
            position += 1
            continue
        if position == len(text):
            if non_matching:
                result.append(AnalyzedSubstring(False, "".join(non_matching)))
            break
        non_matching.append(text[position])
        position += 1
    return tuple(result)


class _Translator:
    """One parse of an XPath regular expression into Python syntax."""

    def __init__(self, source: str, flags: str) -> None:
        self.source = source
        self.flags = flags
        self.ignore_whitespace = "x" in flags
        self.dot_all = "s" in flags
        self.multiline = "m" in flags
        self.output: list[str] = []
        self.index = 0
        self.group_count = 0
        self.open_groups: list[int | None] = []
        self.closed_groups: set[int] = set()
        self.can_quantify = False

    def translate(self) -> str:
        while self.index < len(self.source):
            character = self.source[self.index]
            if self.ignore_whitespace and character in _XPATH_WHITESPACE:
                self.index += 1
                continue
            if character == "\\":
                self._escape()
                continue
            if character == "[":
                self._character_class()
                continue
            if character == ".":
                self.output.append(r"[\s\S]" if self.dot_all else r"[^\r\n]")
                self.index += 1
                self.can_quantify = True
                continue
            if character == "^":
                self._anchor_start()
                continue
            if character == "$":
                self._anchor_end()
                continue
            if character in "?*+":
                self._quantifier(character)
                continue
            if character == "{":
                self._braced_quantifier()
                continue
            if character == "(":
                self._open_group()
                continue
            if character == ")":
                self._close_group()
                continue
            if character == "|":
                self.output.append("|")
                self.index += 1
                self.can_quantify = False
                continue
            if character == "]":
                self._fail("unexpected right square bracket")
            self.output.append(re.escape(character))
            self.index += 1
            self.can_quantify = True
        if self.open_groups:
            self._fail("unterminated subexpression")
        return "".join(self.output)

    # -- atoms -------------------------------------------------------------

    def _escape(self) -> None:
        self.index += 1
        if self.index >= len(self.source):
            self._fail("a backslash must not end the pattern")
        character = self.source[self.index]
        if character in _SINGLE_ESCAPES:
            self.output.append(re.escape(_SINGLE_ESCAPES[character]))
            self.index += 1
            self.can_quantify = True
            return
        if character.isdigit():
            self._back_reference()
            return
        if character in "sSdDiIcCwW":
            self._shortcut(character)
            return
        if character in "pP":
            self._unicode_category(character)
            return
        self._fail(f"the escape sequence \\{character} is not defined")

    def _shortcut(self, character: str) -> None:
        subset = {
            "s": s_shortcut,
            "d": d_shortcut,
            "i": i_shortcut,
            "c": c_shortcut,
            "w": w_shortcut,
        }[character.lower()]()
        negative = character.isupper()
        self.output.append(_python_class(subset, negative=negative))
        self.index += 1
        self.can_quantify = True

    def _unicode_category(self, kind: str) -> None:
        close = self.source.find("}", self.index + 2)
        if close < 0:
            self._fail("a Unicode category escape requires a closing brace")
        name = self.source[self.index + 2 : close]
        if not name:
            self._fail("a Unicode category escape requires a name")
        try:
            subset = unicode_subset(name)
        except RegexError:
            if name.startswith("Is"):
                try:
                    subset = unicode_subset(name[2:])
                except RegexError:
                    self._fail(f"the Unicode category {name!r} is not defined")
            else:
                self._fail(f"the Unicode category {name!r} is not defined")
        self.output.append(_python_class(subset, negative=kind == "P"))
        self.index = close + 1
        self.can_quantify = True

    def _back_reference(self) -> None:
        start = self.index
        while self.index < len(self.source) and self.source[self.index].isdigit():
            candidate = int(self.source[start : self.index + 1])
            if candidate > self.group_count:
                break
            self.index += 1
        if self.index == start:
            self._fail("a back-reference must refer to an existing group")
        number = int(self.source[start : self.index])
        if number < 1 or number > self.group_count:
            self._fail("a back-reference must refer to an existing group")
        if number not in self.closed_groups:
            self._fail("a back-reference must follow its captured subexpression")
        self.output.append(f"\\{number}")
        self.can_quantify = True

    def _character_class(self) -> None:
        character_class, self.index = self._parse_character_class(self.index)
        self.output.append(str(character_class) if len(character_class) else "(?!)")
        self.can_quantify = True

    def _parse_character_class(self, index: int) -> tuple[CharacterClass, int]:
        index += 1
        negative = False
        if index < len(self.source) and self.source[index] == "^":
            negative = True
            index += 1
        content_start = index
        while index < len(self.source):
            character = self.source[index]
            if character == "[":
                self._fail("an unescaped left square bracket is not allowed in a class")
            if character == "\\":
                if index + 1 >= len(self.source):
                    self._fail("a backslash must not end a character class")
                index += 2
                continue
            if character == "]":
                break
            if character == "-" and self.source.startswith("-[", index):
                break
            index += 1
        if index >= len(self.source):
            self._fail("unterminated character class")
        content = self.source[content_start:index]
        try:
            character_class = CharacterClass(content, xsd_version="1.1")
        except (RegexError, IndexError, ValueError) as exc:
            raise XPathRegexError(str(exc), code="FORX0002") from exc
        if negative:
            character_class.complement()
        if self.source.startswith("-[", index):
            subtracted, index = self._parse_character_class(index + 1)
            character_class -= subtracted
            if index >= len(self.source) or self.source[index] != "]":
                self._fail("unterminated character class subtraction")
            return character_class, index + 1
        if self.source[index] != "]":  # pragma: no cover - loop exits only on it
            self._fail("unterminated character class")
        return character_class, index + 1

    def _anchor_start(self) -> None:
        if self.multiline:
            self.output.append(r"(?:(?<=\n)(?=[\s\S])|\A)")
        else:
            self.output.append(r"\A")
        self.index += 1
        self.can_quantify = True

    def _anchor_end(self) -> None:
        if self.multiline:
            self.output.append(r"(?:(?=\n)|\Z)")
        else:
            self.output.append(r"\Z")
        self.index += 1
        self.can_quantify = True

    # -- quantifiers and groups -------------------------------------------

    def _quantifier(self, character: str) -> None:
        if not self.can_quantify:
            self._fail(f"the quantifier {character!r} has no preceding atom")
        self._protect_anchor()
        self.output.append(character)
        self.index += 1
        if self.index < len(self.source) and self.source[self.index] == "?":
            self.output.append("?")
            self.index += 1
        self.can_quantify = False

    def _braced_quantifier(self) -> None:
        if not self.can_quantify:
            self._fail("a braced quantifier has no preceding atom")
        self._protect_anchor()
        match = _QUANTIFIER.match(self.source, self.index)
        if match is None:
            self._fail("a left brace must introduce a valid quantifier")
        minimum = int(match.group(1))
        maximum = match.group(2)
        if maximum not in (None, "") and int(maximum) < minimum:
            self._fail("a quantifier maximum must not be less than its minimum")
        self.output.append(match.group(0))
        self.index = match.end()
        if self.index < len(self.source) and self.source[self.index] == "?":
            self.output.append("?")
            self.index += 1
        self.can_quantify = False

    def _open_group(self) -> None:
        if self.source.startswith("(?:", self.index):
            self.output.append("(?:")
            self.open_groups.append(None)
            self.index += 3
            self.can_quantify = False
            return
        if self.source.startswith("(?", self.index):
            self._fail("only non-capturing (?:...) groups are defined")
        self.group_count += 1
        self.open_groups.append(self.group_count)
        self.output.append("(")
        self.index += 1
        self.can_quantify = False

    def _close_group(self) -> None:
        if not self.open_groups:
            self._fail("unbalanced right parenthesis")
        group = self.open_groups.pop()
        if group is not None:
            self.closed_groups.add(group)
        self.output.append(")")
        self.index += 1
        self.can_quantify = True

    def _protect_anchor(self) -> None:
        """Wrap an anchor before a quantifier, where Python forbids direct repetition."""
        if self.output and self.output[-1] in (
            r"\A",
            r"\Z",
            r"(?:(?<=\n)(?=[\s\S])|\A)",
            r"(?:(?=\n)|\Z)",
        ):
            self.output[-1] = f"(?:{self.output[-1]})"

    def _fail(self, message: str) -> NoReturn:
        raise XPathRegexError(
            f"invalid XPath regular expression {self.source!r}: {message}",
            code="FORX0002",
        )


def _python_class(subset: UnicodeSubset, *, negative: bool) -> str:
    """Return one Python character class for an elementpath Unicode subset."""
    content = str(subset)
    if negative:
        return rf"(?-i:[^{content}])"
    return rf"(?-i:[{content}])"
