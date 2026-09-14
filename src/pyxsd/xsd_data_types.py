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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar, Self

__all__ = [
    "ENTITIES",
    "ENTITY",
    "ID",
    "IDREF",
    "IDREFS",
    "NMTOKEN",
    "NMTOKENS",
    "NOTATION",
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
    # can distinguish the 46 built-ins from intermediate helper classes
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
            if base is list:
                return list.__new__(cls)
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

# Ambient namespace bindings for ``xs:QName`` values. QName construction
# takes only the lexical value, so the in-scope prefix -> URI map reaches
# it through a ContextVar set by the parser around instance binding.
# ``None`` means no context: QName falls back to plain lexical comparison.
_QNAME_CONTEXT: ContextVar[Mapping[str, str] | None] = ContextVar(
    "pyxsd_qname_context", default=None
)


@contextmanager
def whitespace_mode(mode: str) -> Iterator[None]:
    """Set the ambient whitespace handling for datatype construction."""
    token = _WHITESPACE_MODE.set(mode)
    try:
        yield
    finally:
        _WHITESPACE_MODE.reset(token)


@contextmanager
def qname_context(bindings: Mapping[str, str] | None) -> Iterator[None]:
    """Set the ambient prefix bindings used to resolve ``xs:QName`` values.

    ``bindings`` maps prefixes to namespace URIs; the empty string maps
    the default namespace. ``None`` disables resolution, so QName values
    compare by lexical form (the legacy behaviour).
    """
    token = _QNAME_CONTEXT.set(bindings)
    try:
        yield
    finally:
        _QNAME_CONTEXT.reset(token)


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
    """``xs:QName``: optionally prefixed name (``prefix:local``).

    Resolution against the ambient :func:`qname_context` supplies the
    value's namespace URI, so two prefixes bound to one URI compare
    equal. Without a context the value is purely lexical.
    """

    name = "QName"
    _pattern = re.compile(rf"({_NCNAME}:)?{_NCNAME}")

    # Assigned by ``__new__`` from the ambient QName context.
    _resolved_: bool
    _uri_: str | None
    _local_: str

    def __new__(cls, val: str) -> Self:
        instance: Self = super().__new__(cls, val)
        text = str(instance)
        if ":" in text:
            prefix, local = text.split(":", 1)
        else:
            prefix, local = "", text
        bindings = _QNAME_CONTEXT.get()
        instance._resolved_ = bindings is not None
        instance._uri_ = bindings.get(prefix) if bindings is not None else None
        instance._local_ = local
        return instance


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


class NOTATION(String):
    """``xs:NOTATION``: a reference to a notation declaration.

    XSD 1.0 forbids using ``xs:NOTATION`` directly as an element or
    attribute type (an ``xs:notation`` declaration must be used
    instead), but schemas still write it; resolving the name keeps
    those schemas loadable and treats the value space as strings.
    """

    name = "NOTATION"


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
# XSD 1.1 allows year 0000 (1 BCE, optionally written -0000) for the
# temporal types.  Extended years still may not carry redundant leading
# zeros, so five or more digits must start with a non-zero digit.
_YEAR = r"-?(?:[0-9]{4}|[1-9][0-9]{4,})"
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


_FLOAT_LEXICAL = re.compile(
    r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|[-+]?INF|NaN"
)


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


class XsdList(TypeList):
    """A schema-declared ``xs:list`` type: a list of item-typed values.

    The class generated for a ``<simpleType><list itemType="..."/>``
    declaration derives from this base (alongside ``SchemaBase``); the
    resolved item declaration is recorded as the class's ``itemType``
    attribute. Construction parses the lexical form -- whitespace
    separated tokens, per the ``collapse`` whitespace facet fixed for
    list types -- converting each token through the item type, so an
    invalid item raises ``TypeError``/``ValueError`` exactly like any
    other datatype.
    """

    #: The item type class; the class generated for a list simple type
    #: overrides this with the resolved item declaration.
    itemType: ClassVar[type] = AnySimpleType

    def __new__(cls, value: Any = "", *args: Any, **kwargs: Any) -> Self:
        instance = super().__new__(cls)
        if value is None:
            value = ""
        if isinstance(value, str):
            text = _ws_collapse(value)
            tokens: list[Any] = text.split(" ") if text else []
        elif isinstance(value, (list, tuple)):
            tokens = list(value)
        else:
            tokens = [value]
        instance.extend(cls.itemType(token) for token in tokens)
        return instance

    def __init__(self, value: Any = "", *args: Any, **kwargs: Any) -> None:
        """Values are built in ``__new__``; keep ``list.__init__`` inert."""
        pass

    @property
    def tokens(self) -> list[str]:
        """The items of the list as strings, for facet length checks."""
        return [str(item) for item in self]


# ---------------------------------------------------------------------------
# XSD value-space comparison
# ---------------------------------------------------------------------------

_TEMPORAL_DATETIME = re.compile(
    r"^(-?[0-9]{4,})-([0-9]{2})-([0-9]{2})T"
    r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})?$"
)
_TEMPORAL_DATE = re.compile(r"^(-?[0-9]{4,})-([0-9]{2})-([0-9]{2})(Z|[+-][0-9]{2}:[0-9]{2})?$")
_TEMPORAL_TIME = re.compile(
    r"^([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})?$"
)


def _offset_seconds(token: str | None) -> int:
    """Timezone offset in seconds; an absent timezone compares as UTC."""
    if token is None or token == "Z":
        return 0
    sign = 1 if token[0] == "+" else -1
    hours, minutes = token[1:].split(":")
    return sign * (int(hours) * 3600 + int(minutes) * 60)


def _fraction_microseconds(digits: str | None) -> int:
    """The fractional-second part of a lexical, padded or truncated to microseconds."""
    if not digits:
        return 0
    return int((digits + "000000")[:6])


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Proleptic-Gregorian day number (1970-01-01 is day 0).

    Unlike :mod:`datetime` this supports XSD's extended years, including
    year 0000 and negative (BCE) years.
    """
    adjusted = year - (1 if month <= 2 else 0)
    era = adjusted // 400
    year_of_era = adjusted - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146097 + day_of_era - 719468


def _datetime_key(text: str) -> int | tuple[str, str]:
    """A value-space key for ``xs:dateTime``: microseconds on a UTC timeline.

    The key is uniform for zoned and unzoned values (an absent timezone
    compares as UTC), preserves fractional seconds, and handles the
    end-of-day spelling ``24:00:00``.
    """
    match = _TEMPORAL_DATETIME.match(text)
    if match is None:
        return ("lex", text)
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    micros = _fraction_microseconds(match.group(7))
    offset = _offset_seconds(match.group(8))
    days = _days_from_civil(year, month, day)
    extra_day = 0
    if hour == 24:  # 24:00:00 is the end of the day
        hour, extra_day = 0, 1
    seconds = (days + extra_day) * 86400 + hour * 3600 + minute * 60 + second
    return seconds * 1_000_000 + micros - offset * 1_000_000


def _time_key(text: str) -> int | tuple[str, str]:
    """A value-space key for ``xs:time``: microseconds since midnight UTC."""
    match = _TEMPORAL_TIME.match(text)
    if match is None:
        return ("lex", text)
    hour, minute, second = (int(match.group(i)) for i in range(1, 4))
    micros = _fraction_microseconds(match.group(4))
    offset = _offset_seconds(match.group(5))
    seconds = 0 if hour == 24 else hour * 3600 + minute * 60 + second
    return seconds * 1_000_000 + micros - offset * 1_000_000


def _date_key(text: str) -> int | tuple[str, str]:
    """A value-space key for ``xs:date``: microseconds on a UTC timeline."""
    match = _TEMPORAL_DATE.match(text)
    if match is None:
        return ("lex", text)
    year, month, day = (int(match.group(i)) for i in range(1, 4))
    offset = _offset_seconds(match.group(4))
    days = _days_from_civil(year, month, day)
    return days * 86400 * 1_000_000 - offset * 1_000_000


_TEMPORAL_GYEAR = re.compile(r"^(-?[0-9]{4,})(Z|[+-][0-9]{2}:[0-9]{2})?$")
_TEMPORAL_GYEARMONTH = re.compile(r"^(-?[0-9]{4,})-([0-9]{2})(Z|[+-][0-9]{2}:[0-9]{2})?$")
_TEMPORAL_GMONTH = re.compile(r"^--([0-9]{2})(Z|[+-][0-9]{2}:[0-9]{2})?$")
_TEMPORAL_GMONTHDAY = re.compile(r"^--([0-9]{2})-([0-9]{2})(Z|[+-][0-9]{2}:[0-9]{2})?$")
_TEMPORAL_GDAY = re.compile(r"^---([0-9]{2})(Z|[+-][0-9]{2}:[0-9]{2})?$")


def _gyear_key(text: str) -> int | tuple[str, str]:
    """A value-space key for ``xs:gYear`` anchored at January 1st."""
    match = _TEMPORAL_GYEAR.match(text)
    if match is None:
        return ("lex", text)
    year = int(match.group(1))
    offset = _offset_seconds(match.group(2))
    return _days_from_civil(year, 1, 1) * 86400 * 1_000_000 - offset * 1_000_000


def _gyearmonth_key(text: str) -> int | tuple[str, str]:
    """A value-space key for ``xs:gYearMonth`` anchored at the month start."""
    match = _TEMPORAL_GYEARMONTH.match(text)
    if match is None:
        return ("lex", text)
    year, month = int(match.group(1)), int(match.group(2))
    offset = _offset_seconds(match.group(3))
    return _days_from_civil(year, month, 1) * 86400 * 1_000_000 - offset * 1_000_000


def _gmonth_key(text: str) -> tuple:
    """A value-space key for ``xs:gMonth``: ``(month, offset)``."""
    match = _TEMPORAL_GMONTH.match(text)
    if match is None:
        return ("lex", text)
    return (int(match.group(1)), _offset_seconds(match.group(2)))


def _gmonthday_key(text: str) -> tuple:
    """A value-space key for ``xs:gMonthDay``: ``(month, day, offset)``."""
    match = _TEMPORAL_GMONTHDAY.match(text)
    if match is None:
        return ("lex", text)
    return (
        int(match.group(1)),
        int(match.group(2)),
        _offset_seconds(match.group(3)),
    )


def _gday_key(text: str) -> tuple:
    """A value-space key for ``xs:gDay``: ``(day, offset)``."""
    match = _TEMPORAL_GDAY.match(text)
    if match is None:
        return ("lex", text)
    return (int(match.group(1)), _offset_seconds(match.group(2)))


def _duration_key(text: str) -> tuple:
    """A value-space key for ``xs:duration``: ``(sign, months, seconds)``.

    XSD compares durations by months and seconds independently, so
    ``P1Y`` equals ``P12M`` and ``P1D`` equals ``PT24H`` while ``P1M``
    and ``P30D`` stay distinct (their equality is calendar-dependent).
    """
    match = _DURATION_PARTS.fullmatch(text)
    if match is None:
        return (0, 0, 0.0)

    def number(part: str | None) -> float:
        if not part:
            return 0.0
        return float(re.sub(r"[^0-9.]", "", part))

    years, months, days, hours, minutes, seconds = (number(group) for group in match.groups())
    total_months = int(years) * 12 + int(months)
    total_seconds = int(days) * 86400 + int(hours) * 3600 + int(minutes) * 60 + seconds
    sign = -1 if text.startswith("-") else 1
    return (sign, total_months, total_seconds)


def _has_timezone(text: str) -> bool:
    """Whether a temporal lexical form carries an explicit timezone."""
    return text.endswith("Z") or bool(re.search(r"[+-][0-9]{2}:[0-9]{2}$", text))


def _temporal_equality_key(kind: str, key: int | tuple[str, str], text: str) -> tuple[str, Any]:
    """Wraps a timeline key with timezone presence for XSD equality.

    An unzoned and a zoned value denote different XSD values even when
    their timelines agree, so equality comparisons (enumeration, fixed
    checks, identity constraints) must distinguish them.  Ordering uses
    the bare timeline :func:`facets.order_key` produces instead.
    """
    if isinstance(key, tuple):
        return (kind, key)
    return (kind, (key, _has_timezone(text)))


def xsd_value_key(value: Any) -> tuple:
    """A comparison key implementing XSD value-space equality.

    Lexical spellings that denote one XSD value compare equal: hex case
    (``FF``/``ff``), base64 whitespace, list whitespace, numeric values
    that differ only in scale (``1.0``/``1.00``), duration values that
    differ only in unit choice (``P1Y``/``P12M``), and timezone offsets
    that name the same instant. Types without a specialised key fall
    back to their lexical form.
    """
    if isinstance(value, HexBinary):
        return ("hexBinary", bytes.fromhex(str(value)))
    if isinstance(value, Base64Binary):
        return ("base64Binary", base64.b64decode(_ws_remove(str(value))))
    if isinstance(value, _ListString):
        return (value.name, tuple(value.tokens))
    if isinstance(value, XsdList):
        return ("list", tuple(xsd_value_key(item) for item in value))
    if isinstance(value, bool):
        return ("boolean", int(value))
    if isinstance(value, Duration):
        return ("duration", _duration_key(str(value)))
    if isinstance(value, decimal.Decimal):
        return ("decimal", decimal.Decimal(str(value)))
    if isinstance(value, int):
        return ("integer", int(value))
    if isinstance(value, float):
        return ("float", "NaN" if math.isnan(value) else float(value))
    if isinstance(value, DateTime):
        return _temporal_equality_key("dateTime", _datetime_key(str(value)), str(value))
    if isinstance(value, Date):
        return _temporal_equality_key("date", _date_key(str(value)), str(value))
    if isinstance(value, Time):
        return _temporal_equality_key("time", _time_key(str(value)), str(value))
    if isinstance(value, GYear):
        return _temporal_equality_key("gYear", _gyear_key(str(value)), str(value))
    if isinstance(value, GYearMonth):
        return _temporal_equality_key("gYearMonth", _gyearmonth_key(str(value)), str(value))
    if isinstance(value, GMonthDay):
        return _temporal_equality_key("gMonthDay", _gmonthday_key(str(value)), str(value))
    if isinstance(value, GMonth):
        return _temporal_equality_key("gMonth", _gmonth_key(str(value)), str(value))
    if isinstance(value, GDay):
        return _temporal_equality_key("gDay", _gday_key(str(value)), str(value))
    if isinstance(value, QName) and getattr(value, "_resolved_", False):
        return ("QName", (value._uri_, value._local_))
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
