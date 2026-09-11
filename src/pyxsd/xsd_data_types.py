"""Built-in data types from XML Schema (XSD 1.0).

This module implements the full lattice of XSD built-in datatypes as
Python classes.  Each class validates the *lexical form* of a value
(what appears in the XML document) at construction time and raises
``TypeError`` for invalid values.

Design notes:

- Subclasses of Python's immutable builtins (``int``, ``float``,
  ``str``, ``decimal.Decimal``) do their construction and validation
  in ``__new__`` rather than ``__init__``, per the standard pattern
  for subclassing immutable types.
- String-based types preserve the original lexical form exactly, so
  instance documents round-trip without reformatting.  Whitespace
  facets (``collapse`` and friends) are not applied here; that is the
  job of the value-semantics layer.
- Validation is regex-based and intentionally pragmatic: patterns
  enforce structure (digit counts, separators, ranges like month
  ``01``-``12``) but do not perform full calendar arithmetic (e.g.
  ``2006-02-30`` passes the ``date`` pattern).  Binary, numeric, and
  boolean types are fully validated.
- ``xs:anyType`` is represented permissively (any text) until the
  wildcard content-model work; see the XSD support table in the
  documentation.

Every concrete class carries a ``name`` attribute holding its XSD
name; the element representative machinery builds its lookup table
from those names.
"""

import base64
import binascii
import decimal
import math
import re
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from typing import Any, ClassVar, Self

__all__ = [
    "ENTITIES",
    "ENTITY",
    "ID",
    "IDREF",
    "IDREFS",
    "NMTOKEN",
    "NMTOKENS",
    "AnySimpleType",
    "AnyType",
    "AnyURI",
    "Base64Binary",
    "Boolean",
    "Byte",
    "Date",
    "DateTime",
    "Decimal",
    "Double",
    "Duration",
    "Float",
    "GDay",
    "GMonth",
    "GMonthDay",
    "GYear",
    "GYearMonth",
    "HexBinary",
    "Int",
    "Integer",
    "Language",
    "Long",
    "NCName",
    "Name",
    "NegativeInteger",
    "NonNegativeInteger",
    "NonPositiveInteger",
    "NormalizedString",
    "PositiveInteger",
    "QName",
    "Short",
    "String",
    "Time",
    "Token",
    "TypeList",
    "UnsignedByte",
    "UnsignedInt",
    "UnsignedLong",
    "UnsignedShort",
    "XsdDataType",
]


class XsdDataType:
    """Common base class for all of the XSD data type classes."""

    # The true XSD spelling of the type (e.g. "string", "base64Binary").
    # Declared on subclasses only, so that ``"name" in klass.__dict__``
    # can distinguish the 45 built-ins from intermediate helper classes
    # that merely inherit a name.
    name: ClassVar[str]

    @classmethod
    def _unvalidated(cls) -> Any:
        """Returns a bare instance of the type without lexical validation.

        Used for ``xsi:nil`` elements: a nillable element may carry no
        content at all, so no lexical form is available to validate.
        The instance is built through the nearest immutable base type's
        ``__new__`` (str/int/Decimal/float), which bypasses the
        validating ``__new__`` each datatype class defines.
        """
        for base in cls.__mro__:
            if base is str:
                # Deliberately dynamic: construct the concrete subclass
                # through the immutable base's __new__.
                return str.__new__(cls)  # type: ignore[type-var]
            if base is int:
                return int.__new__(cls)  # type: ignore[type-var]
            if base is float:
                return float.__new__(cls)  # type: ignore[type-var]
            if base is decimal.Decimal:
                return decimal.Decimal.__new__(cls)  # type: ignore[type-var]
        return object.__new__(cls)


# ---------------------------------------------------------------------------
# String and string-derived types
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# XSD whitespace processing and XML name primitives
# ---------------------------------------------------------------------------

# XSD's definition of whitespace is exactly these four characters; Python's
# str.split()/strip() also fold NBSP and other Unicode spaces, which must be
# treated as ordinary characters.
_XSD_WHITESPACE = " \t\n\r"
_WS_RUN = re.compile(r"[ \t\n\r]+")
_WS_ANY = re.compile(r"[ \t\n\r]")
# Compatibility mode folds any Unicode whitespace (NBSP and friends), the
# way Python's own str.split()/strip() do.
_COMPAT_WS_RUN = re.compile(r"\s+")
_COMPAT_WS_ANY = re.compile(r"\s")

# Ambient whitespace handling for datatype construction. The datatype
# ``__new__`` signatures take only the lexical value, so the parse mode
# reaches them through a ContextVar set by the parser around instance
# binding. "xsd" is the default; "compat" additionally folds Unicode
# whitespace.
_WHITESPACE_MODE: ContextVar[str] = ContextVar("pyxsd_whitespace_mode", default="xsd")


@contextmanager
def whitespace_mode(mode: str) -> Iterator[None]:
    """Set the ambient whitespace handling for datatype construction."""
    token = _WHITESPACE_MODE.set(mode)
    try:
        yield
    finally:
        _WHITESPACE_MODE.reset(token)


def _ws_replace(text: str) -> str:
    """The XSD ``replace`` facet: tab/newline/CR become spaces.

    In ``compat`` mode, other Unicode whitespace also becomes a space.
    """
    if _WHITESPACE_MODE.get() == "compat":
        return _COMPAT_WS_ANY.sub(" ", text)
    return text.translate({0x09: 0x20, 0x0A: 0x20, 0x0D: 0x20})


def _ws_collapse(text: str) -> str:
    """The XSD ``collapse`` facet: trim and squeeze runs to one space.

    In ``compat`` mode, runs of any Unicode whitespace are collapsed.
    """
    if _WHITESPACE_MODE.get() == "compat":
        return _COMPAT_WS_RUN.sub(" ", text).strip(" ")
    return _WS_RUN.sub(" ", text).strip(" ")


def _ws_remove(text: str) -> str:
    """Remove whitespace entirely (used by base64Binary).

    In ``compat`` mode, removes any Unicode whitespace.
    """
    if _WHITESPACE_MODE.get() == "compat":
        return _COMPAT_WS_ANY.sub("", text)
    return _WS_ANY.sub("", text)


# XML 1.0 NameStartChar / NameChar ranges (5th edition). Using explicit
# ranges avoids ``\\w`` (which accepts e.g. superscript two) and the
# "not a digit" approximation.
_NAME_START = (
    "A-Z_a-z"
    "\\u00c0-\\u00d6\\u00d8-\\u00f6\\u00f8-\\u02ff"
    "\\u0370-\\u037d\\u037f-\\u1fff\\u200c-\\u200d\\u2070-\\u218f"
    "\\u2c00-\\u2fef\\u3001-\\ud7ff\\uf900-\\ufdcf\\ufdf0-\\ufffd"
    "\\U00010000-\\U000effff"
)
_NAME_CHAR = _NAME_START + "\\-.0-9\\u00b7\\u0300-\\u036f\\u203f-\\u2040"

# A name character that is not a NameStartChar (used for the start rule).
_LETTER = rf"[{_NAME_START}]"
_NAME_CHAR_CLASS = rf"[{_NAME_CHAR}]"
_NCNAME = rf"{_LETTER}{_NAME_CHAR_CLASS}*"


class String(str, XsdDataType):
    """The ``xs:string`` type: any character data."""

    name = "string"


class NormalizedString(String):
    """``xs:normalizedString``: tabs/newlines/CRs are replaced by spaces.

    This is the XSD ``replace`` whitespace facet, not a validity
    constraint, so such characters are folded rather than rejected.
    """

    name = "normalizedString"

    def __new__(cls, val: str) -> Self:
        return super().__new__(cls, _ws_replace(str(val)))


class Token(NormalizedString):
    """``xs:token``: ``collapsed`` whitespace, per its XSD facet."""

    name = "token"

    def __new__(cls, val: str) -> Self:
        return String.__new__(cls, _ws_collapse(str(val)))


class _PatternString(String):
    """Base for string types validated against a lexical pattern.

    Per XSD, these types derive from ``token``, so the lexical form is
    whitespace-collapsed (leading/trailing whitespace removed, internal
    runs squeezed to single spaces) before validation, and the
    collapsed form is stored.  Subclasses set ``_pattern`` (a compiled
    regular expression the entire collapsed form must match) and
    ``name``.
    """

    _pattern: ClassVar[re.Pattern[str]]

    def __new__(cls, val: str) -> Self:
        text = _ws_collapse(str(val))
        if cls._pattern.fullmatch(text) is None:
            raise TypeError(f"Not a valid {cls.name}: {text!r}")
        return super().__new__(cls, text)


class Language(_PatternString):
    """``xs:language``: an RFC 1766 language code such as ``en-US``."""

    name = "language"
    _pattern = re.compile(r"[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8})*")


class Name(_PatternString):
    """``xs:Name``: an XML name (letters, digits, ``.``, ``-``, ``_``, ``:``)."""

    name = "Name"
    _pattern = re.compile(rf"(?:{_LETTER}|:)[{_NAME_CHAR}:]*")


class NCName(_PatternString):
    """``xs:NCName``: a name without colons (XML namespace-compatible)."""

    name = "NCName"
    _pattern = re.compile(_NCNAME)


class ID(NCName):
    """``xs:ID``: a unique identifier, lexically an NCName."""

    name = "ID"


class IDREF(NCName):
    """``xs:IDREF``: a reference to an ID, lexically an NCName."""

    name = "IDREF"


class ENTITY(NCName):
    """``xs:ENTITY``: an unparsed entity name, lexically an NCName."""

    name = "ENTITY"


class NMTOKEN(_PatternString):
    """``xs:NMTOKEN``: a single name token (may start with a digit or colon)."""

    name = "NMTOKEN"
    _pattern = re.compile(rf"[{_NAME_CHAR}:]+")


class AnyURI(Token):
    """``xs:anyURI``: a URI reference (collapsed, otherwise unconstrained).

    XSD's lexical space permits spaces and other characters, mapping them
    through URI escaping, so there is no ``\\S``-style pattern to enforce.
    """

    name = "anyURI"


class QName(_PatternString):
    """``xs:QName``: optionally prefixed name (``prefix:local``)."""

    name = "QName"
    _pattern = re.compile(rf"({_NCNAME}:)?{_NCNAME}")


class _ListString(String):
    """Base for the list types: whitespace-separated tokens.

    Subclasses set ``_token_pattern`` for a single token.
    """

    _token_pattern: ClassVar[re.Pattern[str]]

    def __new__(cls, val: str) -> Self:
        text = _ws_collapse(str(val))
        tokens = text.split(" ") if text else []
        if not tokens:
            # XSD list types require at least one item (minLength 1).
            raise TypeError(f"Not a valid {cls.name}: a list needs at least one item")
        for token in tokens:
            if cls._token_pattern.fullmatch(token) is None:
                raise TypeError(f"Not a valid {cls.name}: {text!r}")
        return super().__new__(cls, text)

    @property
    def tokens(self) -> list[str]:
        """The individual tokens of the list as a plain ``list`` of strings."""
        text = str(self)
        return text.split(" ") if text else []


class IDREFS(_ListString):
    """``xs:IDREFS``: whitespace-separated ID references."""

    name = "IDREFS"
    _token_pattern = NCName._pattern


class ENTITIES(_ListString):
    """``xs:ENTITIES``: whitespace-separated entity names."""

    name = "ENTITIES"
    _token_pattern = NCName._pattern


class NMTOKENS(_ListString):
    """``xs:NMTOKENS``: whitespace-separated name tokens."""

    name = "NMTOKENS"
    _token_pattern = NMTOKEN._pattern


class AnySimpleType(String):
    """``xs:anySimpleType``: any simple value, no constraints."""

    name = "anySimpleType"


class AnyType(String):
    """``xs:anyType``: any content.

    Represented permissively as unconstrained text until wildcard
    content models are handled; see the XSD support table.
    """

    name = "anyType"


# ---------------------------------------------------------------------------
# Binary types
# ---------------------------------------------------------------------------


class Base64Binary(String):
    """``xs:base64Binary``: base64-encoded binary data.

    Whitespace is allowed anywhere in the lexical form (per XSD) and
    ignored; the remaining characters must decode cleanly.
    """

    name = "base64Binary"

    def __new__(cls, val: str) -> Self:
        text = _ws_remove(str(val))
        try:
            decoded = base64.b64decode(text, validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise TypeError(f"Not a valid base64Binary: {text!r}") from None
        # XSD requires the unused bits of the final quantum to be zero;
        # Python's decoder tolerates non-zero pad bits, so compare against
        # the canonical encoding of the decoded bytes.
        if base64.b64encode(decoded).decode("ascii") != text:
            raise TypeError(f"Not a valid base64Binary: {text!r}")
        return super().__new__(cls, text)


class HexBinary(_PatternString):
    """``xs:hexBinary``: hex-encoded binary data, in pairs of digits."""

    name = "hexBinary"
    _pattern = re.compile(r"([0-9a-fA-F]{2})*")


# ---------------------------------------------------------------------------
# Temporal types (lexical validation)
# ---------------------------------------------------------------------------

_TIMEZONE = r"(?:Z|[+-](?:0[0-9]|1[0-3]):[0-5][0-9]|[+-]14:00)?"
# No leading zeros in an extended year, and 0000 is not a legal year.
_YEAR = r"-?(?!0000(?:-|T|Z|[+-]|$))(?:[0-9]{4}|[1-9][0-9]{4,})"
_MONTH = r"(?:0[1-9]|1[0-2])"
_DAY = r"(?:0[1-9]|[12][0-9]|3[01])"
_HOUR = r"(?:[01][0-9]|2[0-3])"
_MINUTE = r"(?:[0-5][0-9])"
_SECOND = r"(?:[0-5][0-9](?:\.[0-9]+)?)"
# 24:00:00 is the legal end-of-day spelling (fraction, if any, must be zero).
_TIME_BODY = rf"(?:{_HOUR}:{_MINUTE}:{_SECOND}|24:00:00(?:\.0+)?)"


class DateTime(_PatternString):
    """``xs:dateTime``: e.g. ``2006-08-30T14:30:00`` (optional timezone)."""

    name = "dateTime"
    _pattern = re.compile(rf"{_YEAR}-{_MONTH}-{_DAY}T{_TIME_BODY}{_TIMEZONE}")


class Date(_PatternString):
    """``xs:date``: e.g. ``2006-08-30`` (optional timezone)."""

    name = "date"
    _pattern = re.compile(rf"{_YEAR}-{_MONTH}-{_DAY}{_TIMEZONE}")


class Time(_PatternString):
    """``xs:time``: e.g. ``14:30:00`` (optional timezone)."""

    name = "time"
    _pattern = re.compile(rf"{_TIME_BODY}{_TIMEZONE}")


class GYear(_PatternString):
    """``xs:gYear``: a calendar year, e.g. ``2006``."""

    name = "gYear"
    _pattern = re.compile(rf"{_YEAR}{_TIMEZONE}")


class GYearMonth(_PatternString):
    """``xs:gYearMonth``: e.g. ``2006-08``."""

    name = "gYearMonth"
    _pattern = re.compile(rf"{_YEAR}-{_MONTH}{_TIMEZONE}")


class GMonth(_PatternString):
    """``xs:gMonth``: e.g. ``--08``."""

    name = "gMonth"
    _pattern = re.compile(rf"--{_MONTH}{_TIMEZONE}")


class GMonthDay(_PatternString):
    """``xs:gMonthDay``: e.g. ``--08-30``."""

    name = "gMonthDay"
    _pattern = re.compile(rf"--{_MONTH}-{_DAY}{_TIMEZONE}")


class GDay(_PatternString):
    """``xs:gDay``: e.g. ``---30``."""

    name = "gDay"
    _pattern = re.compile(rf"---{_DAY}{_TIMEZONE}")


_DURATION_PARTS = re.compile(
    r"-?P(?=\d|T\d)(\d+Y)?(\d+M)?(\d+D)?"
    r"(?:T(?=\d)(\d+H)?(\d+M)?(\d+(?:\.\d+)?S)?)?"
)


class Duration(String):
    """``xs:duration``: e.g. ``P1Y2M3DT4H5M6S``."""

    name = "duration"

    def __new__(cls, val: str) -> Self:
        text = _ws_collapse(str(val))
        if _DURATION_PARTS.fullmatch(text) is None:
            raise TypeError(f"Not a valid Duration: {text!r}")
        return super().__new__(cls, text)


# ---------------------------------------------------------------------------
# Numeric types
# ---------------------------------------------------------------------------

_INT_LEXICAL = re.compile(r"[-+]?[0-9]+")


class Integer(int, XsdDataType):
    """``xs:integer``: any integer, lexically optional sign + digits."""

    name = "integer"

    def __new__(cls, val: str | int) -> Self:
        if isinstance(val, str):
            # Integer derives from token: collapse whitespace first.
            collapsed = _ws_collapse(val)
            if _INT_LEXICAL.fullmatch(collapsed) is None:
                raise TypeError(f"Not a valid integer: {val!r}")
            return super().__new__(cls, collapsed)
        return super().__new__(cls, val)


class _BoundedInteger(Integer):
    """Base for range-restricted integer types (long/short/byte/unsigned)."""

    _min: ClassVar[int]
    _max: ClassVar[int]

    def __new__(cls, val: str) -> Self:
        obj = super().__new__(cls, val)
        if not (cls._min <= obj <= cls._max):
            raise TypeError(
                f"Not a valid {cls.name}: {val!r} (must be between {cls._min} and {cls._max})"
            )
        return obj


class PositiveInteger(Integer):
    """``xs:positiveInteger``: integers greater than zero."""

    name = "positiveInteger"

    def __new__(cls, val: str) -> Self:
        if int(val) <= 0:
            raise TypeError(f"Not a valid PositiveInteger: {val!r}")
        return super().__new__(cls, val)


class NonNegativeInteger(Integer):
    """``xs:nonNegativeInteger``: integers greater than or equal to zero."""

    name = "nonNegativeInteger"

    def __new__(cls, val: str) -> Self:
        if int(val) < 0:
            raise TypeError(f"Not a valid NonNegativeInteger: {val!r}")
        return super().__new__(cls, val)


class NegativeInteger(Integer):
    """``xs:negativeInteger``: integers less than zero."""

    name = "negativeInteger"

    def __new__(cls, val: str) -> Self:
        if int(val) >= 0:
            raise TypeError(f"Not a valid NegativeInteger: {val!r}")
        return super().__new__(cls, val)


class NonPositiveInteger(Integer):
    """``xs:nonPositiveInteger``: integers less than or equal to zero."""

    name = "nonPositiveInteger"

    def __new__(cls, val: str) -> Self:
        if int(val) > 0:
            raise TypeError(f"Not a valid NonPositiveInteger: {val!r}")
        return super().__new__(cls, val)


class Long(_BoundedInteger):
    """``xs:long``: 64-bit signed integer."""

    name = "long"
    _min = -(2**63)
    _max = 2**63 - 1


class Int(_BoundedInteger):
    """``xs:int``: 32-bit signed integer."""

    name = "int"
    _min = -(2**31)
    _max = 2**31 - 1


class Short(_BoundedInteger):
    """``xs:short``: 16-bit signed integer."""

    name = "short"
    _min = -(2**15)
    _max = 2**15 - 1


class Byte(_BoundedInteger):
    """``xs:byte``: 8-bit signed integer."""

    name = "byte"
    _min = -(2**7)
    _max = 2**7 - 1


class UnsignedLong(_BoundedInteger):
    """``xs:unsignedLong``: 64-bit unsigned integer."""

    name = "unsignedLong"
    _min = 0
    _max = 2**64 - 1


class UnsignedInt(_BoundedInteger):
    """``xs:unsignedInt``: 32-bit unsigned integer."""

    name = "unsignedInt"
    _min = 0
    _max = 2**32 - 1


class UnsignedShort(_BoundedInteger):
    """``xs:unsignedShort``: 16-bit unsigned integer."""

    name = "unsignedShort"
    _min = 0
    _max = 2**16 - 1


class UnsignedByte(_BoundedInteger):
    """``xs:unsignedByte``: 8-bit unsigned integer."""

    name = "unsignedByte"
    _min = 0
    _max = 2**8 - 1


_DECIMAL_LEXICAL = re.compile(r"[-+]?([0-9]+(\.[0-9]*)?|\.[0-9]+)")


class Decimal(decimal.Decimal, XsdDataType):
    """``xs:decimal``: arbitrary-precision decimal (no exponent notation)."""

    name = "decimal"

    def __new__(cls, val: str) -> Self:
        if isinstance(val, str):
            collapsed = _ws_collapse(val)
            if _DECIMAL_LEXICAL.fullmatch(collapsed) is None:
                raise TypeError(f"Not a valid decimal: {val!r}")
            return super().__new__(cls, collapsed)
        return super().__new__(cls, val)


_FLOAT_LEXICAL = re.compile(r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|-?INF|NaN")


class Double(float, XsdDataType):
    """``xs:double``: 64-bit floating point (INF/-INF/NaN allowed)."""

    name = "double"

    def __new__(cls, val: str) -> Self:
        if isinstance(val, str):
            collapsed = _ws_collapse(val)
            if _FLOAT_LEXICAL.fullmatch(collapsed) is None:
                raise TypeError(f"Not a valid double: {val!r}")
            return super().__new__(cls, collapsed)
        return super().__new__(cls, val)


class Float(Double):
    """``xs:float``: 32-bit floating point; same lexical space as double.

    Python floats are binary64, so the parsed value is rounded to the
    nearest IEEE binary32 value (overflow becomes an infinity, underflow
    becomes zero) to match the XSD value space.
    """

    name = "float"

    def __new__(cls, val: str) -> Self:
        obj = super().__new__(cls, val)
        number = float(obj)
        try:
            rounded = struct.unpack(">f", struct.pack(">f", number))[0]
        except (OverflowError, struct.error):
            rounded = math.inf if number > 0 else -math.inf
        return float.__new__(cls, rounded)


# ---------------------------------------------------------------------------
# Boolean
# ---------------------------------------------------------------------------


class Boolean(Integer):
    """``xs:boolean``.

    Accepts the four XSD lexical forms (``true``/``false``/``1``/``0``)
    plus Python ``bool``/``int`` 0 or 1.  In Python, booleans subclass
    ``int``, so this class does too: ``__str__`` produces the XML
    lexical form ("true" / "false") and ``__repr__`` the Python form.
    """

    name = "boolean"

    # The 0/1 numeric form stored for ``.val`` access.
    val: int

    def __new__(cls, val: str | bool | int) -> Self:
        if isinstance(val, str):
            collapsed = _ws_collapse(val)
            if collapsed in ("true", "1"):
                numeric = 1
            elif collapsed in ("false", "0"):
                numeric = 0
            else:
                raise TypeError(f"Invalid Boolean value {val!r}")
        else:
            numeric = int(val)
            if numeric not in (0, 1):
                raise TypeError(f"Invalid Boolean value {val!r}")
        obj = super().__new__(cls, numeric)
        obj.val = numeric
        return obj

    def __str__(self) -> str:
        """Returns 'true' or 'false', depending on the value.

        Use for xml and xsd files.
        """
        if self.val == 1:
            return "true"
        return "false"

    def __repr__(self) -> str:
        """Returns 'True' or 'False', depending on the value.

        Use for Python.
        """
        if self.val == 1:
            return "True"
        return "False"


# ---------------------------------------------------------------------------
# Legacy container
# ---------------------------------------------------------------------------


class TypeList(list, XsdDataType):
    """A plain ``list``; kept for backwards compatibility.

    XSD list types proper (IDREFS, ENTITIES, NMTOKENS) are the
    ``_ListString`` subclasses above.
    """

    name = "List"


# ---------------------------------------------------------------------------
# XSD value-space comparison
# ---------------------------------------------------------------------------

_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})$")


def _split_timezone(text: str) -> tuple[int | None, str]:
    """Returns ``(offset_seconds, lexical_without_timezone)``.

    ``None`` means the lexical form carries no timezone, so the value is
    not comparable across offsets and callers fall back to the lexical
    form.
    """
    if text.endswith("Z"):
        return 0, text[:-1]
    match = _OFFSET.search(text)
    if match is None:
        return None, text
    sign = 1 if match.group(1) == "+" else -1
    seconds = sign * (int(match.group(2)) * 3600 + int(match.group(3)) * 60)
    return seconds, text[: match.start()]


def _datetime_key(text: str) -> Any:
    offset, core = _split_timezone(text)
    if offset is None:
        return ("lex", text)
    end_of_day = "T24:00:00" in core
    if end_of_day:
        core = core.replace("T24:00:00", "T00:00:00")
    try:
        moment = _datetime.fromisoformat(core)
    except ValueError:
        return ("lex", text)
    if end_of_day:
        moment += _timedelta(days=1)
    return moment - _timedelta(seconds=offset)


def _time_key(text: str) -> Any:
    offset, core = _split_timezone(text)
    if offset is None:
        return ("lex", text)
    end_of_day = core.startswith("24:00:00")
    if end_of_day:
        core = "00:00:00" + core[len("24:00:00") :]
    try:
        parsed = _datetime.strptime(core, "%H:%M:%S" if "." not in core else "%H:%M:%S.%f")
    except ValueError:
        return ("lex", text)
    seconds = parsed.hour * 3600 + parsed.minute * 60 + parsed.second
    if end_of_day:
        seconds += 24 * 3600
    return seconds - offset


def _date_key(text: str) -> Any:
    offset, core = _split_timezone(text)
    if offset is None:
        return ("lex", text)
    match = re.match(r"^(-?\d{4,})-(\d{2})-(\d{2})$", core)
    if match is None:
        return ("lex", text)
    try:
        day = _date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return ("lex", text)
    return _datetime(day.year, day.month, day.day) - _timedelta(seconds=offset)


def xsd_value_key(value: Any) -> tuple:
    """A comparison key implementing XSD value-space equality.

    Lexical spellings that denote one XSD value compare equal: hex case
    (``FF``/``ff``), base64 whitespace, list whitespace, and timezone
    offsets that name the same instant. Types without a specialised key
    fall back to their lexical form.
    """
    if isinstance(value, HexBinary):
        return ("hexBinary", bytes.fromhex(str(value)))
    if isinstance(value, Base64Binary):
        return ("base64Binary", base64.b64decode(_ws_remove(str(value))))
    if isinstance(value, _ListString):
        return (value.name, tuple(value.tokens))
    if isinstance(value, DateTime):
        return ("dateTime", _datetime_key(str(value)))
    if isinstance(value, Date):
        return ("date", _date_key(str(value)))
    if isinstance(value, Time):
        return ("time", _time_key(str(value)))
    return (getattr(value, "name", type(value).__name__), str(value))


def xsd_comparable_key(value: Any) -> Any:
    """The type-independent XSD value used to compare two values.

    Identity constraints compare field values across declarations whose
    types may differ in name but share a value space (e.g. ``xs:ID`` and
    ``xs:string``, or two string-derived token types), so the type tag
    from :func:`xsd_value_key` is dropped here.
    """
    key = xsd_value_key(value)
    if isinstance(key, tuple) and len(key) == 2:
        return key[1]
    return key
