"""XSD facet constraints and their enforcement.

Facets are parsed by the element-representative layer into attributes on
the containing :class:`~pyxsd.element_representatives.simple_type.SimpleType`
(``length``, ``minLength``, ``maxLength``, ``patterns``,
``enumerations``, ``whiteSpace``, the four bound facets, and the two
digit facets).  This module turns that raw data into an immutable
:class:`FacetConstraints` value attached to the generated type class and
checks bound values against it.

The split mirrors XSD's own model:

- **Schema-time** work — merging a restriction's facets with its base's,
  compiling patterns, and parsing bound/enumeration literals — happens
  in :func:`build_constraints` and reports problems through the caller.
- **Instance-time** work is :meth:`FacetConstraints.check`, which raises
  the same ``TypeError``/``ValueError`` the built-in datatypes raise for
  invalid lexical values.  The existing binding paths turn that into a
  ``value``-coded validation issue, so facets flow through the report
  exactly like lexical validation does.

Patterns execute on the C ``re`` engine after translation by
``elementpath``; translation and compilation happen once, at class
creation.
"""

from __future__ import annotations

import base64
import decimal
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from elementpath import translate_pattern
from elementpath.exceptions import ElementPathError
from elementpath.regex import RegexError

from pyxsd.xsd_data_types import (
    Base64Binary,
    Boolean,
    Date,
    DateTime,
    Duration,
    GDay,
    GMonth,
    GMonthDay,
    GYear,
    GYearMonth,
    HexBinary,
    QName,
    Time,
    XsdList,
    _date_key,
    _datetime_key,
    _duration_key,
    _gday_key,
    _gmonth_key,
    _gmonthday_key,
    _gyear_key,
    _gyearmonth_key,
    _ListString,
    _time_key,
    _ws_collapse,
    _ws_remove,
    _ws_replace,
    xsd_comparable_key,
)

#: The pattern translation mode.  The suite runs the XSD 1.1 profile and the
#: 1.1 grammar accepts everything 1.0 does.
PATTERN_XSD_VERSION = "1.1"


def whitespace_transform(kind: str, text: str) -> str:
    """Apply an XSD ``whiteSpace`` facet value to a lexical form."""
    if kind == "replace":
        return _ws_replace(text)
    if kind == "collapse":
        return _ws_collapse(text)
    return text


def order_key(value: Any) -> Any:
    """A comparable key implementing XSD value ordering.

    Values of the same primitive type produce keys of one Python type, so
    ``minInclusive``/``maxInclusive`` checks compare like with like.  The
    temporal keys are the same ones value-space equality uses; they are
    exact for whole-second values and an approximation where XSD ordering
    is itself partial (duration, mixed timezone offsets).
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, decimal.Decimal):
        return decimal.Decimal(str(value))
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, Duration):
        return _duration_key(str(value))
    if isinstance(value, DateTime):
        return _datetime_key(str(value))
    if isinstance(value, Date):
        return _date_key(str(value))
    if isinstance(value, Time):
        return _time_key(str(value))
    if isinstance(value, GYear):
        return _gyear_key(str(value))
    if isinstance(value, GYearMonth):
        return _gyearmonth_key(str(value))
    if isinstance(value, GMonthDay):
        return _gmonthday_key(str(value))
    if isinstance(value, GMonth):
        return _gmonth_key(str(value))
    if isinstance(value, GDay):
        return _gday_key(str(value))
    return str(value)


def decimal_digits(value: Any) -> tuple[int, int]:
    """Return ``(totalDigits, fractionDigits)`` for a decimal value.

    Counts the canonical form: significant digits for ``totalDigits``
    (including trailing zeros contributed by a positive exponent) and the
    digits after the point for ``fractionDigits``.
    """
    try:
        number = decimal.Decimal(str(value)).normalize()
    except (decimal.InvalidOperation, ValueError) as exc:
        raise TypeError(f"cannot count digits of {value!r}") from exc
    sign, digits, exponent = number.as_tuple()
    del sign
    if not isinstance(exponent, int):
        # NaN or infinity: no meaningful digit counts.
        return (len(digits), 0)
    digit_list = list(digits)
    while len(digit_list) > 1 and digit_list[0] == 0:
        digit_list.pop(0)
    total = len(digit_list)
    if exponent > 0:
        total += exponent
    fraction = max(0, -exponent)
    return total, fraction


def _value_length(value: Any) -> int:
    """The XSD ``length`` of a value: characters, octets, or list items."""
    if isinstance(value, HexBinary):
        return len(bytes.fromhex(str(value)))
    if isinstance(value, Base64Binary):
        return len(base64.b64decode(_ws_remove(str(value))))
    if isinstance(value, _ListString):
        return len(value.tokens)
    if isinstance(value, XsdList):
        return len(value)
    return len(str(value))


def _list_items(value: Any) -> list[str] | None:
    """The items of a list-typed value, or ``None`` when it is not a list."""
    if isinstance(value, _ListString):
        return value.tokens
    if isinstance(value, XsdList):
        return [str(item) for item in value]
    tokens = getattr(value, "tokens", None)
    if isinstance(tokens, list):
        return [str(token) for token in tokens]
    return None


def _parse_bound(base: Callable[[str], Any], text: str) -> Any:
    """Parse a bound facet literal with the base type and key it for ordering."""
    return order_key(base(text))


def _parse_enumeration(base: Callable[[str], Any], text: str) -> Any:
    """Parse an enumeration literal with the base type and key it for equality.

    The type tag is dropped because the instance value is a generated
    subclass, not the base datatype; membership is a value-space question.
    """
    return xsd_comparable_key(base(text))


@dataclass(frozen=True)
class FacetConstraints:
    """The effective facets of one generated simple type.

    ``None`` means the facet is absent.  Patterns are compiled and
    anchored for full-match semantics.  Bounds and enumerations are
    stored as value-space keys so comparison is by value, not spelling.
    """

    length: int | None = None
    min_length: int | None = None
    max_length: int | None = None
    #: One entry per restriction step; patterns within a step are a
    #: disjunction (XSD groups sibling pattern facets with OR), while the
    #: steps themselves are a conjunction.
    patterns: tuple[tuple[re.Pattern[str], ...], ...] = ()
    enumerations: tuple[tuple[Any, ...], ...] = ()
    white_space: str | None = None
    min_inclusive: Any | None = None
    min_exclusive: Any | None = None
    max_inclusive: Any | None = None
    max_exclusive: Any | None = None
    total_digits: int | None = None
    fraction_digits: int | None = None

    @property
    def is_empty(self) -> bool:
        """Whether this constraint set would enforce nothing."""
        return (
            self.length is None
            and self.min_length is None
            and self.max_length is None
            and not self.patterns
            and not self.enumerations
            and self.white_space is None
            and self.min_inclusive is None
            and self.min_exclusive is None
            and self.max_inclusive is None
            and self.max_exclusive is None
            and self.total_digits is None
            and self.fraction_digits is None
        )

    def check(self, value: Any, lexical: str | None = None) -> None:
        """Raise ``TypeError`` when *value* violates any facet.

        ``lexical`` is the whitespace-processed lexical form the value was
        built from, when the caller has it.  Pattern is a lexical-space
        facet, so it must see that text rather than the Python rendering
        of the value (``False``, ``1e-05``, ... are not XSD spellings).
        """
        items = _list_items(value)
        self._check_lengths(value, items)
        self._check_bounds(value)
        self._check_digits(value)
        self._check_patterns(value, lexical)
        self._check_enumerations(value)

    def _check_lengths(self, value: Any, items: list[str] | None) -> None:
        if self.length is None and self.min_length is None and self.max_length is None:
            return
        actual = len(items) if items is not None else _value_length(value)
        if self.length is not None and actual != self.length:
            raise TypeError(
                f"facet 'length' violated: {value!r} has {actual} items, expected {self.length}"
            )
        if self.min_length is not None and actual < self.min_length:
            raise TypeError(
                f"facet 'minLength' violated: {value!r} has {actual} items, "
                f"minimum {self.min_length}"
            )
        if self.max_length is not None and actual > self.max_length:
            raise TypeError(
                f"facet 'maxLength' violated: {value!r} has {actual} items, "
                f"maximum {self.max_length}"
            )

    def _check_bounds(self, value: Any) -> None:
        if (
            self.min_inclusive is None
            and self.min_exclusive is None
            and self.max_inclusive is None
            and self.max_exclusive is None
        ):
            return
        actual = order_key(value)
        try:
            if self.min_inclusive is not None and actual < self.min_inclusive:
                raise TypeError(
                    f"facet 'minInclusive' violated: {value!r} is below {self.min_inclusive!r}"
                )
            if self.min_exclusive is not None and actual <= self.min_exclusive:
                raise TypeError(
                    f"facet 'minExclusive' violated: {value!r} is not above {self.min_exclusive!r}"
                )
            if self.max_inclusive is not None and actual > self.max_inclusive:
                raise TypeError(
                    f"facet 'maxInclusive' violated: {value!r} is above {self.max_inclusive!r}"
                )
            if self.max_exclusive is not None and actual >= self.max_exclusive:
                raise TypeError(
                    f"facet 'maxExclusive' violated: {value!r} is not below {self.max_exclusive!r}"
                )
        except TypeError as exc:
            if "violated" in str(exc):
                raise
            raise TypeError(f"facet bound comparison failed for {value!r}: {exc}") from exc

    def _check_digits(self, value: Any) -> None:
        if self.total_digits is None and self.fraction_digits is None:
            return
        total, fraction = decimal_digits(value)
        if self.total_digits is not None and total > self.total_digits:
            raise TypeError(
                f"facet 'totalDigits' violated: {value!r} has {total} digits, "
                f"maximum {self.total_digits}"
            )
        if self.fraction_digits is not None and fraction > self.fraction_digits:
            raise TypeError(
                f"facet 'fractionDigits' violated: {value!r} has {fraction} "
                f"fraction digits, maximum {self.fraction_digits}"
            )

    def _check_patterns(self, value: Any, lexical: str | None) -> None:
        if not self.patterns:
            return
        # Pattern is matched against the whole lexical representation of
        # the value (for a list type, the whitespace-separated list text,
        # not each item).  Patterns declared together in one restriction
        # step are alternatives; a value need only match one of them.
        subject = lexical if lexical is not None else str(value)
        for level in self.patterns:
            if not any(pattern.match(subject) is not None for pattern in level):
                raise TypeError(
                    f"facet 'pattern' violated: {subject!r} does not match the required pattern"
                )

    def _check_enumerations(self, value: Any) -> None:
        if not self.enumerations:
            return
        if xsd_comparable_key(value) not in self.enumerations:
            raise TypeError(
                f"facet 'enumeration' violated: {value!r} is not one of the allowed values"
            )


@dataclass(frozen=True)
class FacetBuildResult:
    """The constraints built for one type plus any schema-level problems.

    ``errors`` are single-facet legality problems (reported with the
    ``facet`` code); ``conflicts`` are combination problems — mutually
    exclusive facets, an empty value space — reported with the
    ``facet-conflict`` code.
    """

    constraints: FacetConstraints
    errors: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


# --- applicability ---------------------------------------------------------

_LENGTH_FACETS = ("length", "minLength", "maxLength")
_BOUND_FACETS = ("minInclusive", "maxInclusive", "minExclusive", "maxExclusive")
_DIGIT_FACETS = ("totalDigits", "fractionDigits")


#: Types that are Python ``str`` subclasses for storage but are not
#: derived from ``xs:string``, so the length family does not apply.
_NON_STRING_DERIVED = (
    HexBinary,
    Base64Binary,
    Boolean,
    Duration,
    DateTime,
    Date,
    Time,
    GYear,
    GYearMonth,
    GMonth,
    GMonthDay,
    GDay,
)


def _is_string_like(base: type) -> bool:
    return issubclass(base, str) and not issubclass(base, _NON_STRING_DERIVED)


def _is_binary(base: type) -> bool:
    return issubclass(base, (HexBinary, Base64Binary))


def _is_list(base: type) -> bool:
    return issubclass(base, (_ListString, XsdList))


def _base_min_length(base: type | None) -> int | None:
    """A fixed ``minLength`` on a built-in *base*, if it has one.

    Every XSD list type fixes ``minLength`` to 1: an empty list is not a
    legal value, so a restriction can neither lower the minimum nor set a
    maximum below it.
    """
    if base is not None and _is_list(base):
        return 1
    return None


def _is_numeric_or_temporal(base: type) -> bool:
    return issubclass(base, (int, float, decimal.Decimal, Duration, DateTime, Date, Time))


def _is_decimal(base: type) -> bool:
    # ``totalDigits`` applies to every type derived from decimal, which
    # includes the whole integer family (pyxsd models those as ``int``
    # subclasses) but not float/double or boolean.
    if issubclass(base, (Boolean, bool)):
        return False
    return issubclass(base, (decimal.Decimal, int))


def _is_integer(base: type) -> bool:
    """Whether *base* is derived from ``xs:integer``.

    ``xs:boolean`` is a Python ``bool``/``int`` subclass but is not an
    integer-derived XSD type.
    """
    if issubclass(base, (Boolean, bool)):
        return False
    return issubclass(base, int)


#: Inclusive bounds fixed by the integer-derived built-ins whose value
#: space is encoded in ``__new__`` rather than a ``_min``/``_max`` class
#: attribute.
_INTEGER_FIXED_BOUNDS: dict[str, tuple[int | None, int | None]] = {
    "positiveInteger": (1, None),
    "nonNegativeInteger": (0, None),
    "negativeInteger": (None, -1),
    "nonPositiveInteger": (None, 0),
}


def _base_fixed_bounds(base: type | None) -> tuple[Any | None, Any | None, Any | None, Any | None]:
    """The bounds a built-in *base* fixes on its value space.

    Returns ``(min_inclusive, min_exclusive, max_inclusive, max_exclusive)``.
    Only the integer-derived built-ins fix a bound; every other built-in
    datatype is unbounded.
    """
    if base is None:
        return (None, None, None, None)
    low = getattr(base, "_min", None)
    high = getattr(base, "_max", None)
    if low is not None or high is not None:
        return (low, None, high, None)
    for klass in getattr(base, "__mro__", (base,)):
        name = klass.__dict__.get("name") or klass.__name__
        if name in _INTEGER_FIXED_BOUNDS:
            low, high = _INTEGER_FIXED_BOUNDS[name]
            return (low, None, high, None)
    return (None, None, None, None)


def _effective_lower(inclusive: Any | None, exclusive: Any | None) -> tuple[Any, bool] | None:
    """The tighter lower bound as ``(value, is_exclusive)``, or ``None``."""
    if inclusive is None and exclusive is None:
        return None
    if inclusive is None:
        return (exclusive, True)
    if exclusive is None:
        return (inclusive, False)
    try:
        if exclusive > inclusive:
            return (exclusive, True)
    except TypeError:
        return (exclusive, True)
    # Equal bounds: the exclusive one is stricter.
    if exclusive == inclusive:
        return (inclusive, True)
    return (inclusive, False)


def _effective_upper(inclusive: Any | None, exclusive: Any | None) -> tuple[Any, bool] | None:
    """The tighter upper bound as ``(value, is_exclusive)``, or ``None``."""
    if inclusive is None and exclusive is None:
        return None
    if inclusive is None:
        return (exclusive, True)
    if exclusive is None:
        return (inclusive, False)
    try:
        if exclusive < inclusive:
            return (exclusive, True)
    except TypeError:
        return (exclusive, True)
    # Equal bounds: the exclusive one is stricter.
    if exclusive == inclusive:
        return (inclusive, True)
    return (inclusive, False)


def _is_qname_like(base: type) -> bool:
    """Whether *base* is QName or derives from it.

    XSD 1.1 deprecates the length family on QName, and the test suite
    encodes the XSD 1.0 ruling that every QName value satisfies those
    facets.  Schema legality is still checked, but enforcement is
    vacuous.
    """
    name = getattr(base, "name", "")
    return name == "QName" or issubclass(base, QName)


#: whiteSpace values ordered from least to most restrictive; a restriction
#: step may only move to an equal or more restrictive value.
_WHITESPACE_RANK = {"preserve": 0, "replace": 1, "collapse": 2}


def _fixed_white_space(base: type) -> str:
    """The whiteSpace value a base type fixes.

    ``xs:string`` fixes ``preserve`` and ``xs:normalizedString`` fixes
    ``replace``; every other built-in (and every type derived from one)
    fixes ``collapse``.
    """
    name = getattr(base, "name", "")
    if name == "string":
        return "preserve"
    if name == "normalizedString":
        return "replace"
    return "collapse"


def _facet_applicable(facet: str, base: type) -> bool:
    """Whether *facet* may be declared on a type derived from *base*.

    This is a practical reading of the XSD applicability tables aimed at
    catching clear misuse (a length facet on a number, a digit facet on a
    string); it deliberately does not chase every edge of the temporal
    and union tables.
    """
    if facet in ("pattern", "enumeration"):
        return True
    if facet == "whiteSpace":
        # whiteSpace is applicable to every simple type; the value is
        # checked against the base's fixed value separately.
        return True
    if facet in _LENGTH_FACETS:
        return _is_string_like(base) or _is_binary(base) or _is_list(base)
    if facet in _BOUND_FACETS:
        return _is_numeric_or_temporal(base) or issubclass(base, str)
    if facet in _DIGIT_FACETS:
        return _is_decimal(base)
    return True


def _facet_non_negative_int(text: str | None, name: str, errors: list[str]) -> int | None:
    """Parse a ``nonNegativeInteger`` facet value, recording bad values."""
    if text is None:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        errors.append(f"facet {name!r} value {text!r} is not a non-negative integer")
        return None
    if value < 0:
        errors.append(f"facet {name!r} value {text!r} is not a non-negative integer")
        return None
    return value


def _facet_positive_int(text: str | None, name: str, errors: list[str]) -> int | None:
    """Parse a ``positiveInteger`` facet value, recording bad values."""
    if text is None:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        errors.append(f"facet {name!r} value {text!r} is not a positive integer")
        return None
    if value < 1:
        errors.append(f"facet {name!r} value {text!r} is not a positive integer")
        return None
    return value


_XML11_NAME_START_CONTENTS = (
    ":A-Z_a-z"
    "\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u02ff"
    "\u0370-\u037d\u037f-\u1fff\u200c-\u200d\u2070-\u218f"
    "\u2c00-\u2fef\u3001-\ud7ff\uf900-\ufdcf\ufdf0-\ufffd"
    "\U00010000-\U000effff"
)
_XML11_NAME_CHAR_CONTENTS = _XML11_NAME_START_CONTENTS + "0-9.\u00b7\u0300-\u036f\u203f-\u2040-"
_XML11_NAME_START_CLASS = f"[{_XML11_NAME_START_CONTENTS}]"
_XML11_NAME_CHAR_CLASS = f"[{_XML11_NAME_CHAR_CONTENTS}]"


def _xml11_name_classes(text: str) -> str:
    """Spell out the ``\\i`` and ``\\c`` escapes as XML 1.1 name classes.

    elementpath translates those escapes from an XML 1.0 table that stops
    before the astral name characters, so the full ranges are substituted
    here.  Outside a character class the replacement is the class itself;
    inside one its contents are spliced in so the surrounding brackets stay
    balanced.
    """
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "\\":
                out.append("\\\\")
                i += 2
                continue
            if nxt in "ic":
                name_class = _XML11_NAME_START_CLASS if nxt == "i" else _XML11_NAME_CHAR_CLASS
                out.append(name_class if depth == 0 else name_class[1:-1])
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


def _compile_pattern(text: str) -> re.Pattern[str]:
    """Translate and compile one XSD pattern.

    Raises :class:`ValueError` when the pattern is not legal XSD, so the
    caller can report it as a schema problem.
    """
    try:
        translated = translate_pattern(
            _xml11_name_classes(text),
            xsd_version=PATTERN_XSD_VERSION,
            anchors=False,
            back_references=False,
            lazy_quantifiers=False,
        )
    except (ElementPathError, RegexError, re.error) as exc:
        raise ValueError(f"illegal XSD pattern {text!r}: {exc}") from exc
    try:
        return re.compile(translated)
    except re.error as exc:
        raise ValueError(f"illegal XSD pattern {text!r}: {exc}") from exc


def build_constraints(
    source: Any,
    base: type | None,
    parent: FacetConstraints | None,
    *,
    base_factory: Callable[[str], Any],
) -> FacetBuildResult:
    """Build the effective constraints for one simple type.

    ``source`` is the SimpleType element representative (its facet
    attributes are read directly); ``base`` is the resolved base class
    used to type-check facet literals; ``parent`` carries the constraints
    inherited from a derived-from type.  Problems are returned as strings
    rather than raised, because a malformed facet should be a schema
    error while the rest of the schema still compiles.
    """
    errors: list[str] = []
    conflicts: list[str] = []
    parent = parent or FacetConstraints()
    allowed: set[str] = set()
    if base is not None:
        base_name = getattr(base, "name", base.__name__)
        for facet in _LENGTH_FACETS + _BOUND_FACETS + _DIGIT_FACETS + ("whiteSpace",):
            if getattr(source, facet, None) is None:
                continue
            if _facet_applicable(facet, base):
                allowed.add(facet)
            else:
                errors.append(f"facet {facet!r} is not applicable to base type {base_name!r}")
    else:
        allowed.update(_LENGTH_FACETS + _BOUND_FACETS + _DIGIT_FACETS + ("whiteSpace",))

    def facet_value(name: str) -> str | None:
        """The child's declared facet value, applicable or not."""
        value = getattr(source, name, None)
        return None if value is None else str(value)

    def declared(name: str) -> str | None:
        """The child's facet value when it is applicable, else ``None``."""
        if name not in allowed:
            return None
        return facet_value(name)

    # Facet values are validated even when the facet is not applicable to
    # the base type: an illegal value is a schema error in its own right.
    length = _facet_non_negative_int(facet_value("length"), "length", errors)
    min_length = _facet_non_negative_int(facet_value("minLength"), "minLength", errors)
    max_length = _facet_non_negative_int(facet_value("maxLength"), "maxLength", errors)
    if length is not None and max_length is not None:
        errors.append("cannot specify both 'length' and 'maxLength'")
    if length is not None and min_length is not None:
        # XSD 1.1 (Datatypes, "length and minLength or maxLength") allows
        # length alongside minLength only when an ancestor fixes the same
        # minLength without length; list types carry such a fixed
        # minLength, so a list restriction may combine them.
        inherited_min = parent.length is None and parent.min_length == min_length
        list_base = base is not None and _is_list(base)
        if not (min_length <= length and (inherited_min or list_base)):
            errors.append("cannot specify both 'length' and 'minLength'")
    if min_length is not None and max_length is not None and min_length > max_length:
        errors.append(f"maxLength {max_length} is less than minLength {min_length}")
    base_min_length = _base_min_length(base)
    if base_min_length is not None:
        # A list base fixes minLength to 1, so a restriction cannot lower
        # the minimum or cap the maximum below it.
        for facet, value in (
            ("length", length),
            ("minLength", min_length),
            ("maxLength", max_length),
        ):
            if value is not None and value < base_min_length:
                conflicts.append(
                    f"facet {facet!r} value {value} is less than the base type's "
                    f"minimum {base_min_length}"
                )
    if "length" not in allowed:
        length = None
    if "minLength" not in allowed:
        min_length = None
    if "maxLength" not in allowed:
        max_length = None
    if base is not None and _is_qname_like(base):
        # The combination checks above still apply, but QName values
        # satisfy the length family vacuously (see ``_is_qname_like``).
        length = None
        min_length = None
        max_length = None

    raw_patterns = tuple(getattr(source, "patterns", ()) or ())
    own_level: list[re.Pattern[str]] = []
    for pattern_text in raw_patterns:
        try:
            own_level.append(_compile_pattern(str(pattern_text)))
        except ValueError as exc:
            errors.append(str(exc))
    # Sibling patterns in this restriction step are alternatives
    # (OR); the level they form is combined with the inherited levels
    # as a conjunction (AND).
    pattern_levels = parent.patterns
    if own_level:
        pattern_levels = (*parent.patterns, tuple(own_level))

    white_space = declared("whiteSpace")
    base_fixed = _fixed_white_space(base) if base is not None else None
    if white_space is not None:
        # A restriction step may only keep or tighten whiteSpace.
        inherited = parent.white_space if parent.white_space is not None else base_fixed
        if (
            white_space in _WHITESPACE_RANK
            and inherited is not None
            and _WHITESPACE_RANK[white_space] < _WHITESPACE_RANK[inherited]
        ):
            base_name = getattr(base, "name", None) or (
                base.__name__ if base is not None else "unknown"
            )
            errors.append(
                f"facet 'whiteSpace' value {white_space!r} is not allowed for "
                f"base type {base_name!r} (fixed to {inherited!r})"
            )
            white_space = inherited
    else:
        white_space = parent.white_space
    if white_space is None and base is not None:
        # Every simple type has an effective whiteSpace: the built-in
        # default when no facet narrows it.  This must be applied before
        # the value-space facets are checked.
        white_space = base_fixed
    if white_space is not None and white_space not in ("preserve", "replace", "collapse"):
        errors.append(f"illegal whiteSpace facet value {white_space!r}")
        white_space = parent.white_space

    enumerations: tuple[tuple[Any, ...], ...] = parent.enumerations
    raw_enumerations = tuple(getattr(source, "enumerations", ()) or ())
    if raw_enumerations:
        parsed: list[tuple[Any, ...]] = []
        for raw_value in raw_enumerations:
            try:
                parsed.append(_parse_enumeration(base_factory, str(raw_value)))
            except (TypeError, ValueError) as exc:
                errors.append(
                    f"enumeration value {raw_value!r} is not valid for its base type: {exc}"
                )
        if parsed:
            own = tuple(parsed)
            enumerations = (
                tuple(value for value in own if value in enumerations)
                if parent.enumerations
                else own
            )

    def parse_bound(name: str) -> Any | None:
        text = facet_value(name)
        if text is None:
            return None
        try:
            value = _parse_bound(base_factory, text)
        except (TypeError, ValueError) as exc:
            errors.append(f"{name} value {text!r} is not valid for its base type: {exc}")
            return None
        # A bound on an inapplicable facet is still parsed (so its value is
        # validated) but never enforced.
        return value if name in allowed else None

    # Each bound facet tightens the inherited one (restriction can only
    # narrow a value space).
    min_inclusive = _tighten_min(parse_bound("minInclusive"), parent.min_inclusive)
    min_exclusive = _tighten_min(parse_bound("minExclusive"), parent.min_exclusive)
    max_inclusive = _tighten_max(parse_bound("maxInclusive"), parent.max_inclusive)
    max_exclusive = _tighten_max(parse_bound("maxExclusive"), parent.max_exclusive)

    # The two lower bounds and the two upper bounds are mutually exclusive
    # within one restriction step.
    if facet_value("maxInclusive") is not None and facet_value("maxExclusive") is not None:
        conflicts.append("cannot specify both 'maxInclusive' and 'maxExclusive'")
    if facet_value("minInclusive") is not None and facet_value("minExclusive") is not None:
        conflicts.append("cannot specify both 'minInclusive' and 'minExclusive'")

    # Fold in the bounds the built-in base itself fixes, then require the
    # effective interval to be non-empty: a lower bound above the upper
    # one, or equal bounds with an exclusive side.
    base_min_inclusive, base_min_exclusive, base_max_inclusive, base_max_exclusive = (
        _base_fixed_bounds(base)
    )
    lower = _effective_lower(
        _tighten_min(min_inclusive, base_min_inclusive),
        _tighten_min(min_exclusive, base_min_exclusive),
    )
    upper = _effective_upper(
        _tighten_max(max_inclusive, base_max_inclusive),
        _tighten_max(max_exclusive, base_max_exclusive),
    )
    if lower is not None and upper is not None:
        lower_value, lower_is_exclusive = lower
        upper_value, upper_is_exclusive = upper
        try:
            empty = lower_value > upper_value or (
                lower_value == upper_value and (lower_is_exclusive or upper_is_exclusive)
            )
        except TypeError:
            empty = False
        if empty:
            conflicts.append("the declared bounds leave no value in the base type's value space")

    total_digits_value = _facet_positive_int(facet_value("totalDigits"), "totalDigits", errors)
    fraction_digits_value = _facet_non_negative_int(
        facet_value("fractionDigits"), "fractionDigits", errors
    )
    if (
        fraction_digits_value is not None
        and fraction_digits_value != 0
        and base is not None
        and _is_integer(base)
    ):
        # ``fractionDigits`` is fixed to 0 on every integer-derived type:
        # the base's value space has no fractional part, so a restriction
        # may restate the fixed value but not change it.
        base_label = getattr(base, "name", None) or base.__name__
        errors.append(
            f"facet 'fractionDigits' value {fraction_digits_value!r} is not allowed "
            f"for base type {base_label!r} (fixed to 0 on integer types)"
        )
    total_digits = _min_optional(
        total_digits_value if "totalDigits" in allowed else None,
        parent.total_digits,
    )
    fraction_digits = _min_optional(
        fraction_digits_value if "fractionDigits" in allowed else None,
        parent.fraction_digits,
    )

    # Effective length semantics: an explicit length pins min and max and
    # overrides inherited length facets; otherwise min/max tighten the
    # inherited values.
    effective_min = parent.min_length
    effective_max = parent.max_length
    if min_length is not None:
        effective_min = min_length if effective_min is None else max(effective_min, min_length)
    if max_length is not None:
        effective_max = max_length if effective_max is None else min(effective_max, max_length)
    if length is not None:
        effective_min = length
        effective_max = length

    if effective_min is not None and effective_max is not None and effective_min > effective_max:
        errors.append(
            f"inconsistent length facets: minLength {effective_min} exceeds maxLength "
            f"{effective_max}"
        )

    if total_digits is not None and fraction_digits is not None and fraction_digits > total_digits:
        errors.append(f"fractionDigits {fraction_digits} exceeds totalDigits {total_digits}")

    constraints = FacetConstraints(
        length=length if length is not None else parent.length,
        min_length=effective_min,
        max_length=effective_max,
        patterns=pattern_levels,
        enumerations=enumerations,
        white_space=white_space,
        min_inclusive=min_inclusive,
        min_exclusive=min_exclusive,
        max_inclusive=max_inclusive,
        max_exclusive=max_exclusive,
        total_digits=total_digits,
        fraction_digits=fraction_digits,
    )
    return FacetBuildResult(
        constraints=constraints, errors=tuple(errors), conflicts=tuple(conflicts)
    )


def _tighten_min(own: Any | None, inherited: Any | None) -> Any | None:
    if own is None:
        return inherited
    if inherited is None:
        return own
    try:
        return own if own > inherited else inherited
    except TypeError:
        return own


def _tighten_max(own: Any | None, inherited: Any | None) -> Any | None:
    if own is None:
        return inherited
    if inherited is None:
        return own
    try:
        return own if own < inherited else inherited
    except TypeError:
        return own


def _min_optional(own: int | None, inherited: int | None) -> int | None:
    if own is None:
        return inherited
    if inherited is None:
        return own
    return min(own, inherited)
