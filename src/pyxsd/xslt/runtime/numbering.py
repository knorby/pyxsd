"""XSLT numbering, decimal formats, and number-picture formatting.

The module implements the normative algorithms behind ``xsl:number`` and
``fn:format-number``.  Number-picture analysis is a dedicated grammar: passive
characters, active characters, sub-pictures, grouping positions, and minimum
sizes are derived here rather than delegated to a host formatting library.
Decimal formats are validated at compile time against XSLT 3.0 section 5.4.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, NoReturn

from elementpath.helpers import is_ncname

from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xslt.errors import XsltDynamicError, XsltStaticError

__all__ = [
    "DEFAULT_DECIMAL_FORMAT",
    "DecimalFormat",
    "decimal_format_attributes",
    "effective_decimal_formats",
    "format_number",
    "format_place_marker",
    "place_marker",
    "validate_decimal_formats",
]

#: XSLT 3.0 section 5.4 defaults for every decimal format.
DEFAULT_DECIMAL_FORMAT: Mapping[str, str] = {
    "decimal-separator": ".",
    "grouping-separator": ",",
    "percent": "%",
    "per-mille": "\u2030",
    "zero-digit": "0",
    "digit": "#",
    "pattern-separator": ";",
    "exponent-separator": "e",
    "infinity": "Infinity",
    "NaN": "NaN",
    "minus-sign": "-",
}

_SINGLE_CHARACTER_ATTRIBUTES = {
    "decimal-separator": "decimal_separator",
    "grouping-separator": "grouping_separator",
    "percent": "percent",
    "per-mille": "per_mille",
    "zero-digit": "zero_digit",
    "digit": "digit",
    "pattern-separator": "pattern_separator",
    "exponent-separator": "exponent_separator",
    "minus-sign": "minus_sign",
}
_DISTINCT_ATTRIBUTES = (
    "decimal-separator",
    "grouping-separator",
    "percent",
    "per-mille",
    "zero-digit",
    "digit",
    "pattern-separator",
    "exponent-separator",
)
_SCALE_WORDS = (
    "thousand",
    "million",
    "billion",
    "trillion",
    "quadrillion",
    "quintillion",
)
_ROMAN_VALUES = (
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)
_ORDINAL_WORDS = {
    "zero": "zeroth",
    "one": "first",
    "two": "second",
    "three": "third",
    "four": "fourth",
    "five": "fifth",
    "six": "sixth",
    "seven": "seventh",
    "eight": "eighth",
    "nine": "ninth",
    "ten": "tenth",
    "eleven": "eleventh",
    "twelve": "twelfth",
    "thirteen": "thirteenth",
    "fourteen": "fourteenth",
    "fifteen": "fifteenth",
    "sixteen": "sixteenth",
    "seventeen": "seventeenth",
    "eighteen": "eighteenth",
    "nineteen": "nineteenth",
    "twenty": "twentieth",
    "thirty": "thirtieth",
    "forty": "fortieth",
    "fifty": "fiftieth",
    "sixty": "sixtieth",
    "seventy": "seventieth",
    "eighty": "eightieth",
    "ninety": "ninetieth",
    "hundred": "hundredth",
    "thousand": "thousandth",
    "million": "millionth",
    "billion": "billionth",
    "trillion": "trillionth",
    "quadrillion": "quadrillionth",
    "quintillion": "quintillionth",
}


@dataclass(frozen=True, slots=True)
class DecimalFormat:
    """One effective XSLT decimal format."""

    decimal_separator: str = "."
    grouping_separator: str = ","
    percent: str = "%"
    per_mille: str = "\u2030"
    zero_digit: str = "0"
    digit: str = "#"
    pattern_separator: str = ";"
    exponent_separator: str = "e"
    infinity: str = "Infinity"
    nan: str = "NaN"
    minus_sign: str = "-"

    @classmethod
    def from_attributes(
        cls,
        attributes: Mapping[str, str],
        *,
        name: str | None = None,
        module: str | None = None,
        location: Any = None,
    ) -> DecimalFormat:
        values = dict(DEFAULT_DECIMAL_FORMAT)
        for key, value in attributes.items():
            if key in values:
                values[key] = value
        for key in _SINGLE_CHARACTER_ATTRIBUTES:
            value = values[key]
            if len(value) != 1:
                raise XsltStaticError(
                    f"the decimal format attribute {key!r} must be a single character",
                    code="XTSE0020",
                    uri=module,
                    location=location,
                )
        zero_digit = values["zero-digit"]
        try:
            digit_value = unicodedata.digit(zero_digit)
        except (TypeError, ValueError):
            digit_value = None
        if digit_value != 0:
            raise XsltStaticError(
                "the zero-digit attribute must be a digit with numeric value zero",
                code="XTSE1295",
                uri=module,
                location=location,
            )
        distinct = [values[key] for key in _DISTINCT_ATTRIBUTES]
        if len(set(distinct)) != len(distinct):
            raise XsltStaticError(
                "decimal format picture characters must have distinct values",
                code="XTSE1300",
                uri=module,
                location=location,
            )
        return cls(
            decimal_separator=values["decimal-separator"],
            grouping_separator=values["grouping-separator"],
            percent=values["percent"],
            per_mille=values["per-mille"],
            zero_digit=zero_digit,
            digit=values["digit"],
            pattern_separator=values["pattern-separator"],
            exponent_separator=values["exponent-separator"],
            infinity=values["infinity"],
            nan=values["NaN"],
            minus_sign=values["minus-sign"],
        )

    @property
    def digits(self) -> str:
        return "".join(chr(ord(self.zero_digit) + index) for index in range(10))

    @property
    def active_characters(self) -> frozenset[str]:
        return frozenset(
            (self.decimal_separator, self.grouping_separator, self.digit, *self.digits)
        )


def validate_decimal_formats(definitions: Iterable[Any]) -> None:
    """Validate every declared decimal format in one package.

    ``definitions`` are the package constructs; only their ``name``,
    ``attributes``, ``module``, and ``location`` are consulted.
    """
    for definition in definitions:
        DecimalFormat.from_attributes(
            definition.attributes,
            name=definition.name,
            module=definition.module,
            location=definition.location,
        )


def effective_decimal_formats(
    definitions: Mapping[str | None, Mapping[str, str]],
) -> dict[str | None, DecimalFormat]:
    """Return every effective decimal format, applying per-format defaults."""
    formats: dict[str | None, DecimalFormat] = {
        None: DecimalFormat.from_attributes(definitions.get(None, {})),
    }
    for name, attributes in definitions.items():
        if name is None:
            continue
        formats[name] = DecimalFormat.from_attributes(attributes)
    return formats


def decimal_format_attributes(format_: DecimalFormat) -> dict[str, str]:
    """Return the elementpath-facing attribute mapping for one decimal format."""
    return {
        "decimal-separator": format_.decimal_separator,
        "grouping-separator": format_.grouping_separator,
        "percent": format_.percent,
        "per-mille": format_.per_mille,
        "zero-digit": format_.zero_digit,
        "digit": format_.digit,
        "pattern-separator": format_.pattern_separator,
        "exponent-separator": format_.exponent_separator,
        "infinity": format_.infinity,
        "NaN": format_.nan,
        "minus-sign": format_.minus_sign,
    }


# ---------------------------------------------------------------------------
# xsl:number place markers


def place_marker(
    node: XdmNode,
    level: str,
    matches_count: Callable[[XdmNode], bool],
    matches_from: Callable[[XdmNode], bool] | None,
) -> tuple[int, ...]:
    """Compute the normative place marker for *node* at *level*."""
    if not isinstance(node, XdmNode):
        raise TypeError("xsl:number requires a node")
    if level not in ("single", "multiple", "any"):
        raise ValueError(f"unknown numbering level {level!r}")
    root = node.root
    from_match = matches_from or (lambda candidate: candidate is root)
    ancestors_or_self = (node, *node.ancestors)
    if level == "single":
        counted = next(
            (candidate for candidate in ancestors_or_self if matches_count(candidate)),
            None,
        )
        from_node = next(
            (candidate for candidate in ancestors_or_self if from_match(candidate)),
            None,
        )
        if counted is None or from_node is None:
            return ()
        if from_node not in (counted, *counted.ancestors):
            return ()
        return (1 + _preceding_siblings(counted, matches_count),)
    if level == "multiple":
        counted_nodes = [candidate for candidate in ancestors_or_self if matches_count(candidate)]
        from_node = next(
            (candidate for candidate in ancestors_or_self if from_match(candidate)),
            None,
        )
        if from_node is None:
            return ()
        selected = [
            candidate
            for candidate in counted_nodes
            if from_node in (candidate, *candidate.ancestors)
        ]
        selected.reverse()
        return tuple(1 + _preceding_siblings(candidate, matches_count) for candidate in selected)
    candidates = _document_order_candidates(node)
    counted_nodes = [candidate for candidate in candidates if matches_count(candidate)]
    from_node = next(
        (candidate for candidate in reversed(candidates) if from_match(candidate)),
        None,
    )
    if from_node is None:
        return ()
    selected = [
        candidate
        for candidate in counted_nodes
        if candidate is from_node or candidate.document_order > from_node.document_order
    ]
    return (len(selected),) if selected else ()


def _preceding_siblings(node: XdmNode, matches_count: Callable[[XdmNode], bool]) -> int:
    parent = node.parent
    if parent is None:
        return 0
    count = 0
    for sibling in parent.children:
        if sibling is node:
            break
        if matches_count(sibling):
            count += 1
    return count


def _document_order_candidates(node: XdmNode) -> list[XdmNode]:
    """All nodes at or before *node* that the ``preceding`` axis can reach.

    Attributes and namespace nodes are not part of the preceding axis; the
    selected node itself is added when it is one of those kinds because the
    normative union includes ``ancestor-or-self``.
    """
    candidates = [
        candidate
        for candidate in _tree_nodes(node.root)
        if candidate.document_order <= node.document_order
        and candidate.kind not in (NodeKind.ATTRIBUTE, NodeKind.NAMESPACE)
    ]
    if node.kind in (NodeKind.ATTRIBUTE, NodeKind.NAMESPACE):
        candidates.append(node)
    return candidates


def _tree_nodes(node: XdmNode) -> Iterable[XdmNode]:
    yield node
    for child in node.children:
        yield from _tree_nodes(child)


# ---------------------------------------------------------------------------
# xsl:number formatting


@dataclass(frozen=True, slots=True)
class _FormatToken:
    text: str
    is_format: bool


def format_place_marker(
    numbers: Sequence[int],
    format_: str,
    *,
    lang: str = "",
    ordinal: bool = False,
    letter_value: str = "",
    grouping_separator: str | None = None,
    grouping_size: str | None = None,
) -> str:
    """Format one place marker using XSLT 3.0 section 12.4."""
    if not all(isinstance(number, int) for number in numbers):
        raise ValueError("place markers contain integers")
    normalized_letter_value = letter_value.strip()
    if normalized_letter_value not in ("", "alphabetic", "traditional"):
        raise XsltDynamicError(
            f"the letter-value {letter_value!r} is not alphabetic or traditional",
            code="XTDE0030",
        )
    if not numbers:
        return ""
    tokens = _format_tokens(format_)
    format_tokens = [token for token in tokens if token.is_format]
    if not format_tokens:
        prefix = format_
        suffix = format_
        format_tokens = [_FormatToken("1", True)]
        separators = ["."]
    else:
        first = tokens.index(format_tokens[0])
        last = len(tokens) - 1 - tokens[::-1].index(format_tokens[-1])
        prefix = tokens[first - 1].text if first > 0 and not tokens[first - 1].is_format else ""
        suffix = (
            tokens[last + 1].text
            if last + 1 < len(tokens) and not tokens[last + 1].is_format
            else ""
        )
        first_format_index = next(index for index, token in enumerate(tokens) if token.is_format)
        separators = []
        for index, token in enumerate(tokens):
            if not token.is_format:
                continue
            if index == first_format_index:
                separators.append(".")
                continue
            previous = tokens[index - 1]
            separators.append(previous.text if not previous.is_format else ".")
    group_size = _grouping_size(grouping_size)
    parts: list[str] = []
    for index, number in enumerate(numbers):
        token = format_tokens[min(index, len(format_tokens) - 1)]
        if index:
            parts.append(separators[min(index, len(separators) - 1)])
        parts.append(
            _format_token(
                number,
                token.text,
                lang=lang,
                ordinal=ordinal,
                letter_value=normalized_letter_value,
                grouping_separator=grouping_separator,
                grouping_size=group_size,
            )
        )
    return prefix + "".join(parts) + suffix


def _format_tokens(source: str) -> list[_FormatToken]:
    tokens: list[_FormatToken] = []
    index = 0
    while index < len(source):
        alphanumeric = _is_alphanumeric(source[index])
        start = index
        while index < len(source) and _is_alphanumeric(source[index]) == alphanumeric:
            index += 1
        tokens.append(_FormatToken(source[start:index], alphanumeric))
    return tokens


def _is_alphanumeric(character: str) -> bool:
    return unicodedata.category(character)[0] in ("L", "N")


def _grouping_size(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return 0


def _format_token(
    number: int,
    token: str,
    *,
    lang: str,
    ordinal: bool,
    letter_value: str,
    grouping_separator: str | None,
    grouping_size: int | None,
) -> str:
    if number < 0:
        return "-" + _format_token(
            -number,
            token,
            lang=lang,
            ordinal=ordinal,
            letter_value=letter_value,
            grouping_separator=grouping_separator,
            grouping_size=grouping_size,
        )
    decimal = _decimal_token(token)
    if decimal is not None:
        zero_digit, width = decimal
        result = _decimal_number(number, zero_digit, width)
        if grouping_separator is not None and grouping_size is not None and grouping_size > 0:
            result = _group_digits(result, grouping_separator, grouping_size)
        if ordinal:
            result += _ordinal_suffix(number)
        return result
    if token in ("A", "a"):
        if letter_value == "traditional":
            roman = _roman(number)
            if roman is None:
                return str(number)
            return roman if token == "A" else roman.lower()
        alphabetic = _alphabetic(number, token)
        if alphabetic is None:
            return str(number)
        return alphabetic
    if token in ("I", "i"):
        if letter_value == "alphabetic":
            alphabetic = _alphabetic(number, token)
            if alphabetic is None:
                return str(number)
            return alphabetic
        roman = _roman(number)
        if roman is None:
            return str(number)
        return roman if token == "I" else roman.lower()
    if token in ("w", "W", "Ww"):
        words = _english_words(number, ordinal=ordinal)
        if words is None:
            return str(number)
        if token == "W":
            return words.upper()
        if token == "Ww":
            return words.title()
        return words
    return str(number)


def _decimal_token(token: str) -> tuple[str, int] | None:
    try:
        last = unicodedata.digit(token[-1])
    except (TypeError, ValueError):
        return None
    if last != 1:
        return None
    for character in token[:-1]:
        try:
            value = unicodedata.digit(character)
        except (TypeError, ValueError):
            return None
        if value != 0:
            return None
    zero_digit = chr(ord(token[-1]) - 1)
    return zero_digit, len(token)


def _decimal_number(number: int, zero_digit: str, width: int) -> str:
    digits = "".join(chr(ord(zero_digit) + index) for index in range(10))
    text = str(number)
    if len(text) < width:
        text = "0" * (width - len(text)) + text
    return "".join(digits[int(character)] for character in text)


def _group_digits(text: str, separator: str, size: int) -> str:
    groups: list[str] = []
    while len(text) > size:
        groups.append(text[-size:])
        text = text[:-size]
    groups.append(text)
    return separator.join(reversed(groups))


def _alphabetic(number: int, token: str) -> str | None:
    if number < 1:
        return None
    base = ord(token)
    characters: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        characters.append(chr(base + remainder))
    return "".join(reversed(characters))


def _roman(number: int) -> str | None:
    if number < 1 or number > 3999:
        return None
    result: list[str] = []
    remainder = number
    for value, numeral in _ROMAN_VALUES:
        while remainder >= value:
            result.append(numeral)
            remainder -= value
    return "".join(result)


def _english_words(number: int, *, ordinal: bool) -> str | None:
    if number == 0:
        return "zeroth" if ordinal else "zero"
    groups: list[int] = []
    remainder = number
    while remainder:
        groups.append(remainder % 1000)
        remainder //= 1000
    if len(groups) > len(_SCALE_WORDS) + 1:
        return None
    words: list[str] = []
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group:
            continue
        words.extend(_english_group(group))
        if index:
            words.append(_SCALE_WORDS[index - 1])
    result = " ".join(words)
    if ordinal:
        result = _ordinal_phrase(result)
    return result


def _english_group(number: int) -> list[str]:
    words: list[str] = []
    hundreds, remainder = divmod(number, 100)
    if hundreds:
        words.extend((_ONES[hundreds], "hundred"))
    if remainder >= 20:
        tens, ones = divmod(remainder, 10)
        word = _TENS[tens]
        if ones:
            word += "-" + _ONES[ones]
        words.append(word)
    elif remainder:
        words.append(_ONES[remainder])
    return words


_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _ordinal_phrase(phrase: str) -> str:
    if "-" in phrase:
        head, _, tail = phrase.rpartition("-")
        return f"{head}-{_ORDINAL_WORDS.get(tail, tail)}"
    head, _, tail = phrase.rpartition(" ")
    ordinal = _ORDINAL_WORDS.get(tail, tail)
    return f"{head} {ordinal}" if head else ordinal


def _ordinal_suffix(number: int) -> str:
    remainder = number % 100
    if 11 <= remainder <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")


# ---------------------------------------------------------------------------
# fn:format-number


@dataclass(frozen=True, slots=True)
class _SubPicture:
    prefix: str
    suffix: str
    integer_grouping_positions: tuple[int, ...]
    integer_grouping_step: int | None
    fractional_grouping_positions: tuple[int, ...]
    minimum_integer_size: int
    minimum_fractional_size: int
    maximum_fractional_size: int
    has_decimal_separator: bool
    scaling_factor: int
    minimum_exponent_size: int
    percent: bool
    per_mille: bool


def format_number(
    value: object,
    picture: str,
    format_name: str | None,
    formats: Mapping[str | None, DecimalFormat],
    namespaces: Mapping[str, str],
) -> str:
    """Format *value* with the XSLT/FO number-picture grammar."""
    if not isinstance(picture, str):
        raise XsltDynamicError("the number picture must be a string", code="XPTY0004")
    decimal_format = _resolve_decimal_format(format_name, formats, namespaces)
    number = _numeric_value(value)
    if isinstance(number, float) and math.isnan(number):
        return decimal_format.nan
    negative = _is_negative(number)
    sub_pictures = picture.split(decimal_format.pattern_separator)
    if len(sub_pictures) > 2:
        raise XsltDynamicError("the number picture has too many sub-pictures", code="FODF1310")
    positive = _analyze_subpicture(sub_pictures[0], decimal_format)
    if len(sub_pictures) == 2:
        negative_picture = _analyze_subpicture(sub_pictures[1], decimal_format)
    else:
        negative_picture = replace(positive, prefix=decimal_format.minus_sign + positive.prefix)
    sub_picture = negative_picture if negative else positive
    if isinstance(number, float) and math.isinf(number):
        return sub_picture.prefix + decimal_format.infinity + sub_picture.suffix
    decimal_number = _as_decimal(number)
    if sub_picture.percent:
        decimal_number = _scale_decimal(decimal_number, 100)
    elif sub_picture.per_mille:
        decimal_number = _scale_decimal(decimal_number, 1000)
    exponent = 0
    if sub_picture.minimum_exponent_size:
        decimal_number, exponent = _exponential_mantissa(
            decimal_number,
            sub_picture.scaling_factor,
        )
    rounded = _round_decimal(decimal_number, sub_picture.maximum_fractional_size)
    text = (
        ""
        if rounded.is_zero() and sub_picture.minimum_integer_size == 0
        else _fixed_decimal(rounded.copy_abs())
    )
    integer, _, fraction = text.partition(".")
    if len(integer) < sub_picture.minimum_integer_size:
        integer = "0" * (sub_picture.minimum_integer_size - len(integer)) + integer
    if len(fraction) < sub_picture.minimum_fractional_size:
        fraction += "0" * (sub_picture.minimum_fractional_size - len(fraction))
    if sub_picture.minimum_integer_size == 0 and integer == "0" and fraction:
        integer = ""
    integer = _apply_integer_grouping(integer, sub_picture, decimal_format)
    fraction = _apply_fractional_grouping(fraction, sub_picture, decimal_format)
    digits = _translate_digits(integer + fraction, decimal_format)
    translated_integer = digits[: len(integer)]
    translated_fraction = digits[len(integer) :]
    result = translated_integer
    if translated_fraction:
        result += decimal_format.decimal_separator + translated_fraction
    if sub_picture.minimum_exponent_size:
        result += decimal_format.exponent_separator
        if exponent < 0:
            result += decimal_format.minus_sign
            exponent = -exponent
        exponent_text = str(exponent).rjust(sub_picture.minimum_exponent_size, "0")
        result += _translate_digits(exponent_text, decimal_format)
    return sub_picture.prefix + result + sub_picture.suffix


def _scale_decimal(value: Decimal, factor: int) -> Decimal:
    """Multiply by 100/1000 without the default 28-digit context rounding."""
    digits = len(value.as_tuple().digits)
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    with localcontext() as context:
        context.prec = max(50, digits + abs(exponent) + len(str(factor)) + 10)
        context.Emax = max(999999, abs(exponent) + 10)
        context.Emin = -context.Emax
        return value * factor


def _round_decimal(value: Decimal, maximum_fractional_size: int) -> Decimal:
    """Round *value* half-to-even without the default 28-digit context limit."""
    digits = len(value.as_tuple().digits)
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    with localcontext() as context:
        context.prec = max(50, digits + abs(exponent) + maximum_fractional_size + 10)
        context.Emax = max(999999, abs(exponent) + maximum_fractional_size + 10)
        context.Emin = -context.Emax
        quantum = Decimal(1).scaleb(-maximum_fractional_size)
        return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


def _exponential_mantissa(value: Decimal, scaling_factor: int) -> tuple[Decimal, int]:
    """Return the mantissa and integer exponent for an exponent picture."""
    if value.is_zero():
        return Decimal(0), 0
    sign = -1 if value.is_signed() else 1
    magnitude = value.copy_abs()
    exponent = magnitude.adjusted() - scaling_factor + 1
    digits = len(magnitude.as_tuple().digits)
    with localcontext() as context:
        context.prec = max(50, digits + abs(exponent) + 10)
        context.Emax = max(999999, abs(exponent) + 10)
        context.Emin = -context.Emax
        mantissa = magnitude.scaleb(-exponent)
    return mantissa.copy_sign(sign), exponent


def _resolve_decimal_format(
    name: str | None,
    formats: Mapping[str | None, DecimalFormat],
    namespaces: Mapping[str, str],
) -> DecimalFormat:
    if name is None or not name.strip():
        return formats.get(None, DecimalFormat())
    lexical = name.strip()
    if lexical.startswith("Q{"):
        close = lexical.find("}")
        if close < 0:
            raise XsltDynamicError(f"invalid decimal format name {name!r}", code="FODF1280")
        uri = lexical[2:close]
        local = lexical[close + 1 :]
        if not is_ncname(local):
            raise XsltDynamicError(f"invalid decimal format name {name!r}", code="FODF1280")
        expanded = f"{{{uri}}}{local}" if uri else local
    else:
        prefix, separator, local = lexical.partition(":")
        if separator:
            if not is_ncname(prefix) or not is_ncname(local):
                raise XsltDynamicError(f"invalid decimal format name {name!r}", code="FODF1280")
            bound_uri = namespaces.get(prefix)
            if bound_uri is None:
                raise XsltDynamicError(
                    f"the decimal format prefix {prefix!r} is not bound",
                    code="FODF1280",
                )
            expanded = f"{{{bound_uri}}}{local}"
        else:
            if not is_ncname(lexical):
                raise XsltDynamicError(f"invalid decimal format name {name!r}", code="FODF1280")
            expanded = lexical
    decimal_format = formats.get(expanded)
    if decimal_format is None:
        raise XsltDynamicError(
            f"the decimal format {name!r} is not declared",
            code="FODF1280",
        )
    return decimal_format


def _numeric_value(value: object) -> int | float | Decimal:
    if value is None:
        return float("nan")
    if isinstance(value, XdmNode):
        value = value.typed_value[0]
    if isinstance(value, XdmAtomic):
        local = (
            value.type_name.partition("}")[2]
            if value.type_name.startswith("{")
            else value.type_name
        )
        if local == "untypedAtomic":
            try:
                return float(value.lexical)
            except ValueError as exc:
                raise XsltDynamicError(
                    "an untyped atomic value is not numeric",
                    code="XPTY0004",
                ) from exc
        if local not in {
            "decimal",
            "double",
            "float",
            "integer",
            "long",
            "int",
            "short",
            "byte",
            "nonNegativeInteger",
            "nonPositiveInteger",
            "negativeInteger",
            "positiveInteger",
            "unsignedLong",
            "unsignedInt",
            "unsignedShort",
            "unsignedByte",
        }:
            raise XsltDynamicError("format-number requires a numeric value", code="XPTY0004")
        value = value.value
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise XsltDynamicError("format-number requires a numeric value", code="XPTY0004")
    return value


def _is_negative(value: int | float | Decimal) -> bool:
    if isinstance(value, Decimal):
        return value.is_signed()
    if isinstance(value, float):
        return math.copysign(1.0, value) < 0
    return value < 0


def _as_decimal(value: int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def _analyze_subpicture(picture: str, decimal_format: DecimalFormat) -> _SubPicture:
    active = decimal_format.active_characters
    decimal_separator = decimal_format.decimal_separator
    grouping_separator = decimal_format.grouping_separator
    exponent_separator = decimal_format.exponent_separator
    if picture.count(decimal_separator) > 1:
        _invalid_picture("more than one decimal separator")
    if decimal_format.percent in picture and decimal_format.per_mille in picture:
        _invalid_picture("both percent and per-mille signs")
    if picture.count(decimal_format.percent) + picture.count(decimal_format.per_mille) > 1:
        _invalid_picture("more than one percent or per-mille sign")
    exponent_index: int | None = None
    for index, character in enumerate(picture):
        if character != exponent_separator or index == 0 or index + 1 >= len(picture):
            continue
        if picture[index - 1] in active and picture[index + 1] in active:
            if exponent_index is not None:
                _invalid_picture("more than one exponent separator")
            exponent_index = index
    effective_active = active | ({exponent_separator} if exponent_index is not None else set())
    if exponent_index is not None and (
        decimal_format.percent in picture or decimal_format.per_mille in picture
    ):
        _invalid_picture("an exponent cannot be combined with percent or per-mille")
    if (
        decimal_separator + grouping_separator in picture
        or grouping_separator + decimal_separator in picture
    ):
        _invalid_picture("a grouping separator is adjacent to the decimal separator")
    if picture.startswith(grouping_separator) or picture.endswith(grouping_separator):
        _invalid_picture("a grouping separator must have digits on both sides")
    first_active = next(
        (index for index, character in enumerate(picture) if character in effective_active), None
    )
    last_active = next(
        (
            index
            for index, character in enumerate(reversed(picture))
            if character in effective_active
        ),
        None,
    )
    if first_active is None or last_active is None:
        _invalid_picture("a sub-picture requires a digit or optional digit")
    last_active = len(picture) - 1 - last_active
    for index in range(first_active + 1, last_active):
        if picture[index] not in effective_active:
            _invalid_picture("a passive character separates active characters")
    mantissa = picture if exponent_index is None else picture[:exponent_index]
    if not any(
        character in decimal_format.digits or character == decimal_format.digit
        for character in mantissa
    ):
        _invalid_picture("a sub-picture requires a digit or optional digit")
    exponent_part = "" if exponent_index is None else picture[exponent_index + 1 :]
    minimum_exponent_size = 0
    if exponent_index is not None:
        digits_seen = sum(1 for character in exponent_part if character in decimal_format.digits)
        if any(
            character in effective_active and character not in decimal_format.digits
            for character in exponent_part
        ):
            _invalid_picture("an exponent may contain only digits and a passive suffix")
        minimum_exponent_size = digits_seen
    decimal_index = mantissa.find(decimal_separator)
    if decimal_index < 0:
        integer_part = mantissa
        fractional_part = ""
        has_decimal = False
    else:
        integer_part = mantissa[:decimal_index]
        fractional_part = mantissa[decimal_index + 1 :]
        has_decimal = True
    _validate_digit_families(
        integer_part + fractional_part + exponent_part,
        decimal_format,
    )
    _validate_digit_order(integer_part, fractional_part, decimal_format)
    integer_grouping = _integer_grouping_positions(integer_part, decimal_format)
    integer_step = _regular_step(integer_grouping)
    fractional_grouping = _fractional_grouping_positions(fractional_part, decimal_format)
    minimum_integer = sum(1 for character in integer_part if character in decimal_format.digits)
    minimum_fractional = sum(
        1 for character in fractional_part if character in decimal_format.digits
    )
    maximum_fractional = sum(
        1
        for character in fractional_part
        if character in decimal_format.digits or character == decimal_format.digit
    )
    if minimum_integer == 0 and maximum_fractional == 0:
        if exponent_index is not None:
            minimum_fractional = 1
            maximum_fractional = 1
        else:
            minimum_integer = 1
    if exponent_index is not None and minimum_integer == 0 and decimal_format.digit in integer_part:
        minimum_integer = 1
    if minimum_integer == 0 and minimum_fractional == 0:
        minimum_fractional = 1
    scaling_factor = sum(1 for character in integer_part if character in decimal_format.digits)
    prefix = picture[:first_active]
    suffix = picture[last_active + 1 :]
    return _SubPicture(
        prefix=prefix,
        suffix=suffix,
        integer_grouping_positions=integer_grouping,
        integer_grouping_step=integer_step,
        fractional_grouping_positions=fractional_grouping,
        minimum_integer_size=minimum_integer,
        minimum_fractional_size=minimum_fractional,
        maximum_fractional_size=maximum_fractional,
        has_decimal_separator=has_decimal,
        scaling_factor=scaling_factor,
        minimum_exponent_size=minimum_exponent_size,
        percent=decimal_format.percent in picture,
        per_mille=decimal_format.per_mille in picture,
    )


def _invalid_picture(message: str) -> NoReturn:
    raise XsltDynamicError(f"invalid number picture: {message}", code="FODF1310")


def _validate_digit_families(picture: str, decimal_format: DecimalFormat) -> None:
    for character in picture:
        try:
            unicodedata.digit(character)
        except (TypeError, ValueError):
            continue
        if character not in decimal_format.digits:
            _invalid_picture("digits from more than one digit family")


def _validate_digit_order(
    integer_part: str,
    fractional_part: str,
    decimal_format: DecimalFormat,
) -> None:
    seen_mandatory = False
    for character in integer_part:
        if character in decimal_format.digits:
            seen_mandatory = True
        elif character == decimal_format.digit and seen_mandatory:
            _invalid_picture("an optional digit follows a mandatory digit in the integer part")
    seen_optional = False
    for character in fractional_part:
        if character == decimal_format.digit:
            seen_optional = True
        elif character in decimal_format.digits and seen_optional:
            _invalid_picture("a mandatory digit follows an optional digit in the fractional part")


def _integer_grouping_positions(
    integer_part: str,
    decimal_format: DecimalFormat,
) -> tuple[int, ...]:
    positions: list[int] = []
    for index, character in enumerate(integer_part):
        if character != decimal_format.grouping_separator:
            continue
        positions.append(
            sum(
                1
                for following in integer_part[index + 1 :]
                if following in decimal_format.digits or following == decimal_format.digit
            )
        )
    return tuple(sorted(positions))


def _regular_step(positions: tuple[int, ...]) -> int | None:
    if not positions or positions[0] <= 0:
        return None
    step = positions[0]
    if all(position == step * (index + 1) for index, position in enumerate(positions)):
        return step
    return None


def _fractional_grouping_positions(
    fractional_part: str,
    decimal_format: DecimalFormat,
) -> tuple[int, ...]:
    positions: list[int] = []
    for index, character in enumerate(fractional_part):
        if character != decimal_format.grouping_separator:
            continue
        positions.append(
            sum(
                1
                for preceding in fractional_part[:index]
                if preceding in decimal_format.digits or preceding == decimal_format.digit
            )
        )
    return tuple(positions)


def _fixed_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _apply_integer_grouping(
    integer: str,
    picture: _SubPicture,
    decimal_format: DecimalFormat,
) -> str:
    positions = picture.integer_grouping_positions
    if picture.integer_grouping_step is not None:
        step = picture.integer_grouping_step
        positions = tuple(range(step, len(integer) + 1, step))
    insertions = {len(integer) - position for position in positions if 0 < position < len(integer)}
    if not insertions:
        return integer
    result: list[str] = []
    for index, character in enumerate(integer):
        if index in insertions:
            result.append(decimal_format.grouping_separator)
        result.append(character)
    return "".join(result)


def _apply_fractional_grouping(
    fraction: str,
    picture: _SubPicture,
    decimal_format: DecimalFormat,
) -> str:
    insertions = {
        position
        for position in picture.fractional_grouping_positions
        if 0 < position < len(fraction)
    }
    if not insertions:
        return fraction
    result: list[str] = []
    for index, character in enumerate(fraction):
        if index in insertions:
            result.append(decimal_format.grouping_separator)
        result.append(character)
    return "".join(result)


def _translate_digits(text: str, decimal_format: DecimalFormat) -> str:
    return "".join(
        decimal_format.digits[int(character)] if character in "0123456789" else character
        for character in text
    )
