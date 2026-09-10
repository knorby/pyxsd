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
import re
from typing import ClassVar

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

    @classmethod
    def _unvalidated(cls):
        """Returns a bare instance of the type without lexical validation.

        Used for ``xsi:nil`` elements: a nillable element may carry no
        content at all, so no lexical form is available to validate.
        The instance is built through the nearest immutable base type's
        ``__new__`` (str/int/Decimal/float), which bypasses the
        validating ``__new__`` each datatype class defines.
        """
        for base in cls.__mro__:
            if base is str:
                return str.__new__(cls)
            if base is int:
                return int.__new__(cls)
            if base is float:
                return float.__new__(cls)
            if base is decimal.Decimal:
                return decimal.Decimal.__new__(cls)
        return object.__new__(cls)


# ---------------------------------------------------------------------------
# String and string-derived types
# ---------------------------------------------------------------------------

# A letter or underscore, but not a digit (the start of an XML Name).
_LETTER = r"[^\W\d]"
_NAME_CHAR = r"[\w.\-]"
_NCNAME = rf"{_LETTER}{_NAME_CHAR}*"


class String(str, XsdDataType):
    """The ``xs:string`` type: any character data."""

    name = "string"


class NormalizedString(String):
    """``xs:normalizedString``: no carriage returns, tabs, or newlines."""

    name = "normalizedString"

    def __new__(cls, val):
        text = str(val)
        if "\n" in text or "\r" in text or "\t" in text:
            raise TypeError(f"Not a valid normalizedString: {text!r}")
        return super().__new__(cls, text)


class Token(NormalizedString):
    """``xs:token``: like normalizedString; collapse happens later."""

    name = "token"


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

    def __new__(cls, val):
        text = " ".join(str(val).split())
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
    _pattern = re.compile(rf"(?:{_LETTER}|:)[\w.\-:]*")


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
    _pattern = re.compile(r"[\w.\-:]+")


class AnyURI(_PatternString):
    """``xs:anyURI``: a URI reference. Whitespace is not allowed."""

    name = "anyURI"
    _pattern = re.compile(r"\S*")


class QName(_PatternString):
    """``xs:QName``: optionally prefixed name (``prefix:local``)."""

    name = "QName"
    _pattern = re.compile(rf"({_NCNAME}:)?{_NCNAME}")


class _ListString(String):
    """Base for the list types: whitespace-separated tokens.

    Subclasses set ``_token_pattern`` for a single token.
    """

    _token_pattern: ClassVar[re.Pattern[str]]

    def __new__(cls, val):
        text = str(val)
        for token in text.split():
            if cls._token_pattern.fullmatch(token) is None:
                raise TypeError(f"Not a valid {cls.name}: {text!r}")
        return super().__new__(cls, text)

    @property
    def tokens(self):
        """The individual tokens of the list as a plain ``list`` of strings."""
        return str(self).split()


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

    def __new__(cls, val):
        text = "".join(str(val).split())
        try:
            base64.b64decode(text, validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise TypeError(f"Not a valid base64Binary: {text!r}") from None
        return super().__new__(cls, text)


class HexBinary(_PatternString):
    """``xs:hexBinary``: hex-encoded binary data, in pairs of digits."""

    name = "hexBinary"
    _pattern = re.compile(r"([0-9a-fA-F]{2})*")


# ---------------------------------------------------------------------------
# Temporal types (lexical validation)
# ---------------------------------------------------------------------------

_TIMEZONE = r"(?:Z|[+-]\d{2}:\d{2})?"
_YEAR = r"-?\d{4,}"
_MONTH = r"(?:0[1-9]|1[0-2])"
_DAY = r"(?:0[1-9]|[12]\d|3[01])"
_HOUR = r"(?:[01]\d|2[0-3])"
_MINUTE = r"(?:[0-5]\d)"
_SECOND = r"(?:[0-5]\d(?:\.\d+)?)"


class DateTime(_PatternString):
    """``xs:dateTime``: e.g. ``2006-08-30T14:30:00`` (optional timezone)."""

    name = "dateTime"
    _pattern = re.compile(rf"{_YEAR}-{_MONTH}-{_DAY}T{_HOUR}:{_MINUTE}:{_SECOND}{_TIMEZONE}")


class Date(_PatternString):
    """``xs:date``: e.g. ``2006-08-30`` (optional timezone)."""

    name = "date"
    _pattern = re.compile(rf"{_YEAR}-{_MONTH}-{_DAY}{_TIMEZONE}")


class Time(_PatternString):
    """``xs:time``: e.g. ``14:30:00`` (optional timezone)."""

    name = "time"
    _pattern = re.compile(rf"{_HOUR}:{_MINUTE}:{_SECOND}{_TIMEZONE}")


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

    def __new__(cls, val):
        text = str(val)
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

    def __new__(cls, val):
        if isinstance(val, str):
            # Integer derives from token: collapse whitespace first.
            collapsed = " ".join(val.split())
            if _INT_LEXICAL.fullmatch(collapsed) is None:
                raise TypeError(f"Not a valid integer: {val!r}")
            return super().__new__(cls, collapsed)
        return super().__new__(cls, val)


class _BoundedInteger(Integer):
    """Base for range-restricted integer types (long/short/byte/unsigned)."""

    _min: ClassVar[int]
    _max: ClassVar[int]

    def __new__(cls, val):
        obj = super().__new__(cls, val)
        if not (cls._min <= obj <= cls._max):
            raise TypeError(
                f"Not a valid {cls.name}: {val!r} (must be between {cls._min} and {cls._max})"
            )
        return obj


class PositiveInteger(Integer):
    """``xs:positiveInteger``: integers greater than zero."""

    name = "positiveInteger"

    def __new__(cls, val):
        if int(val) <= 0:
            raise TypeError(f"Not a valid PositiveInteger: {val!r}")
        return super().__new__(cls, val)


class NonNegativeInteger(Integer):
    """``xs:nonNegativeInteger``: integers greater than or equal to zero."""

    name = "nonNegativeInteger"

    def __new__(cls, val):
        if int(val) < 0:
            raise TypeError(f"Not a valid NonNegativeInteger: {val!r}")
        return super().__new__(cls, val)


class NegativeInteger(Integer):
    """``xs:negativeInteger``: integers less than zero."""

    name = "negativeInteger"

    def __new__(cls, val):
        if int(val) >= 0:
            raise TypeError(f"Not a valid NegativeInteger: {val!r}")
        return super().__new__(cls, val)


class NonPositiveInteger(Integer):
    """``xs:nonPositiveInteger``: integers less than or equal to zero."""

    name = "nonPositiveInteger"

    def __new__(cls, val):
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

    def __new__(cls, val):
        if isinstance(val, str):
            collapsed = " ".join(val.split())
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

    def __new__(cls, val):
        if isinstance(val, str):
            collapsed = " ".join(val.split())
            if _FLOAT_LEXICAL.fullmatch(collapsed) is None:
                raise TypeError(f"Not a valid double: {val!r}")
            return super().__new__(cls, collapsed)
        return super().__new__(cls, val)


class Float(Double):
    """``xs:float``: 32-bit floating point; same lexical space as double."""

    name = "float"


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

    def __new__(cls, val):
        if isinstance(val, str):
            collapsed = " ".join(val.split())
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

    def __str__(self):
        """Returns 'true' or 'false', depending on the value.

        Use for xml and xsd files.
        """
        if self.val == 1:
            return "true"
        return "false"

    def __repr__(self):
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
