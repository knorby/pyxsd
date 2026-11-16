"""RELAX NG datatype protocols and the built-in/XSD 1.0 libraries.

RELAX NG delegates lexical acceptance and value equality to datatype
libraries.  A factory compiles a type name, its parameters, and the
schema-side context into a reusable :class:`Datatype`.  Every operation
receives the context of the string it interprets, so a ``value`` pattern can
compare a schema-side QName against an instance-side QName in different
prefix scopes.

The empty library URI is the RELAX NG built-in library.  It has exactly the
two datatypes of RELAX NG 1.0 §6.2.9: ``string`` accepts every string and
compares by identity, and ``token`` accepts every string and compares after
XML-whitespace normalization.  Parameters are not allowed.

:data:`XSD_LIBRARY` adapts pyxsd's existing XSD built-in classes.  The
adapter follows XSD 1.0 semantics, not the XSD compiler's default 1.1
profile:

* the XSD 1.1-only types ``anyAtomicType``, ``dateTimeStamp``,
  ``dayTimeDuration``, ``yearMonthDuration`` (and ``precisionDecimal``) are
  rejected explicitly rather than accepted as strings;
* lexical year ``0000`` is rejected for ``date``, ``dateTime``, ``gYear``
  and ``gYearMonth``; lexical ``-0001`` is 1 BCE, so 1 BCE is a leap year
  and ``-0001-02-29`` is valid while ``-0004-02-29`` (4 BCE) is not;
* temporal value equality, bounds, and decoding use the XSD 1.0
  astronomical mapping for negative years (lexical ``y < 0`` is
  astronomical ``y + 1``), not the shared classes' XSD 1.1 numbering;
* duration equality uses the six-dimensional XSD 1.0 value space, so
  ``P1Y`` and ``P12M`` are distinct values;
* duration bound facets use the XSD 1.0 §3.2.6.2 four-reference-date partial
  order, and an indeterminate comparison is false for every bound facet, as
  §3.2.6.3 requires;
* ``anyURI`` uses the XSD 1.0 lexical space: the XLink 5.4 escaping
  procedure is applied, and the result must be an RFC 2396 URI reference
  as amended by RFC 2732.  XSD 1.1's "any XML character sequence" rule is
  not used.  Equality remains identity of the lexical URI-reference
  representation; XSD 1.0 defines no anyURI normalization.

Scheme-specific URI syntax is not checked, matching XSD 1.0's conformance
note; the generic URI-reference grammar is.

The XSD adapter rejects the ``whiteSpace`` and ``enumeration`` parameters as
the RELAX NG datatype guidelines require.  Repeated ``pattern`` parameters
are intersected; every other parameter may appear at most once.  Facets are
validated when the datatype is compiled, and an inapplicable facet is a
parameter error.

``allows``, ``equal``, ``decode`` and ``encode`` are independent operations.
``decode`` and ``encode`` are optional pyxsd capabilities, not RELAX NG
conformance: a custom datatype may implement only validation, in which case
the inherited methods raise :class:`UnsupportedDatatypeOperation`.  Value
equality deliberately ignores facet parameters because RELAX NG's
``datatypeEqual`` service has no parameters.

A compiled datatype is frozen, hashable, and value-comparable.  Its ``repr``
is deterministic because the canonical choice order of the pattern IR sorts
leaves by ``repr``.  The schema-side :class:`DatatypeContext` is retained for
later ``value`` matching; for context-insensitive types it does not
participate in datatype equality, while QName and NOTATION datatypes compare
by their schema-side namespace bindings.
"""

from __future__ import annotations

import base64
import decimal
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType, NotImplementedType
from typing import Any, cast

from pyxsd import xsd_data_types as _xsd_types
from pyxsd.exceptions import PyXSDError
from pyxsd.facets import (
    FacetConstraints,
    FacetViolation,
    build_constraints,
    compile_pattern,
    whitespace_transform,
)
from pyxsd.locations import ConstraintInfo
from pyxsd.relaxng.uris import normalize_uri_reference
from pyxsd.xsd_data_types import (
    _DURATION_PARTS,
    _TEMPORAL_DATE,
    _TEMPORAL_DATETIME,
    _TEMPORAL_GDAY,
    _TEMPORAL_GMONTH,
    _TEMPORAL_GMONTHDAY,
    _TEMPORAL_GYEAR,
    _TEMPORAL_GYEARMONTH,
    _TEMPORAL_TIME,
    _YEAR,
    AnyURI,
    Base64Binary,
    Boolean,
    Decimal,
    Double,
    Duration,
    HexBinary,
    Integer,
    QName,
    String,
    Token,
    XsdDataType,
    _days_from_civil,
    _has_timezone,
    _offset_seconds,
    qname_context,
    xsd_value_key,
)

__all__ = [
    "BUILTIN_FINGERPRINT",
    "BUILTIN_LIBRARY",
    "XSD_FINGERPRINT",
    "XSD_LIBRARY",
    "BuiltinDatatypeFactory",
    "Datatype",
    "DatatypeContext",
    "DatatypeError",
    "DatatypeFactory",
    "DatatypeLibraryRegistration",
    "DatatypeLibraryRegistry",
    "DatatypeParameter",
    "DatatypeParameterError",
    "DatatypeRegistrationError",
    "DatatypeSelector",
    "ScalarDatatype",
    "UnsupportedDatatypeError",
    "UnsupportedDatatypeOperation",
    "XsdDatatypeFactory",
    "resolve_datatype_selector",
]

#: The empty URI identifies RELAX NG's built-in datatype library.
BUILTIN_LIBRARY = ""

#: The URI of the XSD datatype library described by the RELAX NG guidelines.
XSD_LIBRARY = "http://www.w3.org/2001/XMLSchema-datatypes"

#: Fixed identities of the built-in libraries.  Bump these when their
#: observable behavior changes so retained candidate identities do not lie.
BUILTIN_FINGERPRINT = "pyxsd.relaxng.builtin/1"
XSD_FINGERPRINT = "pyxsd.relaxng.xsd-1.0/1"

_XSD_SHORTHAND = "xsd:"

#: Every XSD 1.0 built-in simple datatype, in the XSD 1.0 inventory.  The
#: class mapping is built from these names so a later XSD 1.1 class cannot
#: silently join the XSD 1.0 adapter.
_XSD_10_TYPE_NAMES = frozenset(
    {
        "ENTITIES",
        "ENTITY",
        "ID",
        "IDREF",
        "IDREFS",
        "NCName",
        "NMTOKEN",
        "NMTOKENS",
        "NOTATION",
        "Name",
        "QName",
        "anySimpleType",
        "anyURI",
        "base64Binary",
        "boolean",
        "byte",
        "date",
        "dateTime",
        "decimal",
        "double",
        "duration",
        "float",
        "gDay",
        "gMonth",
        "gMonthDay",
        "gYear",
        "gYearMonth",
        "hexBinary",
        "int",
        "integer",
        "language",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "normalizedString",
        "positiveInteger",
        "short",
        "string",
        "time",
        "token",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)

_XSD_11_ONLY_TYPE_NAMES = frozenset(
    {"anyAtomicType", "dateTimeStamp", "dayTimeDuration", "precisionDecimal", "yearMonthDuration"}
)

_XSD_10_TYPES: dict[str, type[XsdDataType]] = {}
for _module_value in list(vars(_xsd_types).values()):
    if (
        isinstance(_module_value, type)
        and _module_value.__module__ == _xsd_types.__name__
        and isinstance(_module_value.__dict__.get("name"), str)
        and _module_value.__dict__["name"] in _XSD_10_TYPE_NAMES
    ):
        _XSD_10_TYPES[_module_value.__dict__["name"]] = _module_value
del _module_value

#: A selector is either a bare built-in type name (or the ``xsd:`` shorthand)
#: or an explicit ``(library_uri, type_name)`` pair.
DatatypeSelector = str | tuple[str, str]

_QNAME_TYPES = frozenset({"QName", "NOTATION"})
#: Types whose lexical year has XSD 1.0-specific numbering and leap-day rules.
_XSD10_YEAR_TYPE_NAMES = frozenset({"date", "dateTime", "gYear", "gYearMonth"})
#: Timezone-bearing temporal types with the XSD 1.0 partial order.
_XSD10_TEMPORAL_TYPE_NAMES = frozenset(
    {"date", "dateTime", "gDay", "gMonth", "gMonthDay", "gYear", "gYearMonth", "time"}
)
_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
_XSD11_YEAR_ZERO = re.compile(r"^-?0{4}(?![0-9])")
_YEAR_PREFIX = re.compile(r"^(-?[0-9]{4,})")
_TEMPORAL_TIMEZONE_SPAN = decimal.Decimal(14 * 3600)
_PROCESS_DEPENDENT_FINGERPRINT = re.compile(r"0x[0-9a-fA-F]+")

_XSD_PARAMETER_NAMES = frozenset(
    {
        "fractionDigits",
        "length",
        "maxExclusive",
        "maxInclusive",
        "maxLength",
        "minExclusive",
        "minInclusive",
        "minLength",
        "pattern",
        "totalDigits",
    }
)
_REJECTED_XSD_PARAMETERS = frozenset({"enumeration", "whiteSpace"})
_BOUND_PARAMETER_NAMES = frozenset({"minInclusive", "maxInclusive", "minExclusive", "maxExclusive"})

#: XSD 1.0 types with an order relation, so the four bound facets apply.
#: Boolean, the string family, and the binary types are unordered.
_ORDERED_TYPE_NAMES = frozenset(
    {
        "byte",
        "date",
        "dateTime",
        "decimal",
        "double",
        "duration",
        "float",
        "gDay",
        "gMonth",
        "gMonthDay",
        "gYear",
        "gYearMonth",
        "int",
        "integer",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "short",
        "time",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)

#: The XSD facets read by :func:`pyxsd.facets.build_constraints` from a
#: SimpleType representative.  A parameter carrier exposes the same names.
_FACET_NAMES = (
    "length",
    "minLength",
    "maxLength",
    "minInclusive",
    "maxInclusive",
    "minExclusive",
    "maxExclusive",
    "totalDigits",
    "fractionDigits",
)


class DatatypeError(PyXSDError):
    """Base class for datatype capability and parameter failures."""


class UnsupportedDatatypeError(DatatypeError):
    """A datatype library or type is outside the adapter's capabilities."""


class DatatypeParameterError(DatatypeError, ValueError):
    """A datatype parameter is unknown, inapplicable, or inconsistent."""


class DatatypeRegistrationError(DatatypeError, ValueError):
    """A datatype-library registration is invalid or would replace an identity."""


class UnsupportedDatatypeOperation(DatatypeError):
    """The datatype has no decoder or encoder for the requested operation."""


class _NamespaceMap(Mapping[str, str]):
    """An immutable, hashable, deterministically ordered namespace map."""

    __slots__ = ("_items", "_mapping")

    def __init__(self, values: Mapping[str, str]) -> None:
        items = tuple(sorted(dict(values).items()))
        self._items = items
        self._mapping = MappingProxyType(dict(items))

    def __getitem__(self, key: str) -> str:
        return self._mapping[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)

    def __eq__(self, other: object) -> bool | NotImplementedType:
        if not isinstance(other, Mapping):
            return NotImplemented
        return dict(self._items) == dict(other)

    def __hash__(self) -> int:
        return hash(self._items)

    def __repr__(self) -> str:
        return repr(dict(self._items))


@dataclass(frozen=True)
class DatatypeContext:
    """The schema- or instance-side context a lexical value is read in.

    ``base_uri`` is retained for datatypes that resolve relative references.
    ``namespaces`` maps in-scope prefixes to namespace URIs; the empty prefix
    is the default namespace.  The mapping is copied into an immutable,
    hashable form at construction.
    """

    base_uri: str | None
    namespaces: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespaces", _NamespaceMap(self.namespaces))


@dataclass(frozen=True)
class DatatypeParameter:
    """One RELAX NG datatype parameter: an XSD facet name and its lexical value."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("a datatype parameter name must be a non-empty string")
        if not isinstance(self.value, str):
            raise ValueError("a datatype parameter value must be a string")


class _Xsd10TemporalValue(str):
    """A validated XSD 1.0 temporal lexical value.

    It is a ``str`` subclass so callers get the authored lexical form back,
    but it deliberately exposes no XSD 1.1 calendar helper methods.  XSD 1.0
    has no year zero: lexical ``-0001`` is 1 BCE, and 1 BCE is a leap year.
    """

    __slots__ = ()

    def lexical(self) -> str:
        return str(self)


def _format_astro_year(year: int) -> str:
    """Format an astronomical year in the XSD lexical four-digit form."""
    if year < 0:
        return f"-{-year:04d}"
    return f"{year:04d}"


def _xsd10_temporal_value(type_name: str, text: str) -> _Xsd10TemporalValue:
    """Validate and wrap one XSD 1.0 date/dateTime/gYear/gYearMonth lexical.

    The shared pyxsd classes implement XSD 1.1 astronomical numbering.  XSD
    1.0 maps lexical ``-0001`` to 1 BCE, which is astronomical year 0, so a
    negative lexical year maps to ``year + 1`` before the shared calendar
    check.  The original lexical form is retained in the returned value.
    """
    text = whitespace_transform("collapse", text)
    if _XSD11_YEAR_ZERO.match(text):
        raise TypeError(
            f"xs:{type_name} lexical {text!r} uses year 0000, which XSD 1.0 does not allow"
        )
    match = _YEAR_PREFIX.match(text)
    if match is None or re.fullmatch(_YEAR, match.group(1)) is None:
        raise TypeError(f"Not a valid xs:{type_name}: {text!r}")
    year_text = match.group(1)
    year = int(year_text)
    astro_year = year + 1 if year < 0 else year
    validation_text = _format_astro_year(astro_year) + text[len(year_text) :]
    try:
        cast(Any, _XSD_10_TYPES[type_name])(validation_text)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Not a valid xs:{type_name}: {text!r}") from exc
    return _Xsd10TemporalValue(text)


def _xsd10_astro_lexical(text: str) -> str:
    """Rewrite a validated XSD 1.0 lexical year to its astronomical year."""
    match = _YEAR_PREFIX.match(text)
    assert match is not None
    year_text = match.group(1)
    year = int(year_text)
    astro_year = year + 1 if year < 0 else year
    return _format_astro_year(astro_year) + text[len(year_text) :]


def _fraction_seconds(digits: str | None) -> decimal.Decimal:
    """An exact Decimal for an XSD fractional-second digit string."""
    return decimal.Decimal(f"0.{digits}") if digits else decimal.Decimal(0)


def _xsd10_temporal_key(type_name: str, text: str) -> decimal.Decimal:
    """An exact Decimal-second timeline key for an XSD 1.0 temporal value.

    The key is normalized to UTC when a timezone is present and remains the
    local timeline when it is absent.  Fractional seconds are kept exactly,
    unlike the shared microsecond keys.
    """
    if type_name in _XSD10_YEAR_TYPE_NAMES:
        text = _xsd10_astro_lexical(text)
    if type_name == "date":
        match = _TEMPORAL_DATE.fullmatch(text)
        assert match is not None
        year, month, day = (int(match.group(index)) for index in range(1, 4))
        offset = _offset_seconds(match.group(4))
        return decimal.Decimal(_days_from_civil(year, month, day) * 86400 - offset)
    if type_name == "dateTime":
        match = _TEMPORAL_DATETIME.fullmatch(text)
        assert match is not None
        year, month, day, hour, minute, second = (int(match.group(index)) for index in range(1, 7))
        fraction = _fraction_seconds(match.group(7))
        offset = _offset_seconds(match.group(8))
        days = _days_from_civil(year, month, day)
        if hour == 24:
            hour = 0
            days += 1
        return (
            decimal.Decimal(days * 86400 + hour * 3600 + minute * 60 + second) + fraction - offset
        )
    if type_name == "gYear":
        match = _TEMPORAL_GYEAR.fullmatch(text)
        assert match is not None
        year = int(match.group(1))
        offset = _offset_seconds(match.group(2))
        return decimal.Decimal(_days_from_civil(year, 1, 1) * 86400 - offset)
    if type_name == "gYearMonth":
        match = _TEMPORAL_GYEARMONTH.fullmatch(text)
        assert match is not None
        year, month = int(match.group(1)), int(match.group(2))
        offset = _offset_seconds(match.group(3))
        return decimal.Decimal(_days_from_civil(year, month, 1) * 86400 - offset)
    if type_name == "gMonth":
        match = _TEMPORAL_GMONTH.fullmatch(text)
        assert match is not None
        month = int(match.group(1))
        offset = _offset_seconds(match.group(2))
        return decimal.Decimal(_days_from_civil(1972, month, 1) * 86400 - offset)
    if type_name == "gMonthDay":
        match = _TEMPORAL_GMONTHDAY.fullmatch(text)
        assert match is not None
        month, day = int(match.group(1)), int(match.group(2))
        offset = _offset_seconds(match.group(3))
        return decimal.Decimal(_days_from_civil(1972, month, day) * 86400 - offset)
    if type_name == "gDay":
        match = _TEMPORAL_GDAY.fullmatch(text)
        assert match is not None
        day = int(match.group(1))
        offset = _offset_seconds(match.group(2))
        return decimal.Decimal((day - 1) * 86400 - offset)
    match = _TEMPORAL_TIME.fullmatch(text)
    assert match is not None
    hour, minute, second = (int(match.group(index)) for index in range(1, 4))
    fraction = _fraction_seconds(match.group(4))
    offset = _offset_seconds(match.group(5))
    if hour == 24:
        hour = 0
    return decimal.Decimal(hour * 3600 + minute * 60 + second) + fraction - offset


def _xsd10_temporal_compare_keys(
    left_key: decimal.Decimal,
    left_zoned: bool,
    right_key: decimal.Decimal,
    right_zoned: bool,
) -> int | None:
    """XSD 1.0 §3.2.7.4 order for two temporal timeline keys."""
    if left_zoned == right_zoned:
        if left_key < right_key:
            return -1
        if left_key > right_key:
            return 1
        return 0
    if left_zoned:
        if left_key < right_key - _TEMPORAL_TIMEZONE_SPAN:
            return -1
        if left_key > right_key + _TEMPORAL_TIMEZONE_SPAN:
            return 1
        return None
    if left_key + _TEMPORAL_TIMEZONE_SPAN < right_key:
        return -1
    if left_key - _TEMPORAL_TIMEZONE_SPAN > right_key:
        return 1
    return None


def _xsd10_temporal_bound_text(type_name: str, text: str) -> str:
    """Validate a temporal bound literal and return its processed lexical."""
    if type_name in _XSD10_YEAR_TYPE_NAMES:
        return str(_xsd10_temporal_value(type_name, text))
    text = whitespace_transform("collapse", text)
    try:
        cast(Any, _XSD_10_TYPES[type_name])(text)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Not a valid xs:{type_name}: {text!r}") from exc
    return text


def _xsd10_temporal_bounds(
    type_name: str, parameters: tuple[DatatypeParameter, ...]
) -> tuple[tuple[str, decimal.Decimal, str, bool], ...]:
    """Parse and conflict-check XSD 1.0 temporal bounds."""
    bounds: dict[str, tuple[decimal.Decimal, str, bool]] = {}
    for parameter in parameters:
        if parameter.name not in _BOUND_PARAMETER_NAMES:
            continue
        try:
            text = _xsd10_temporal_bound_text(type_name, parameter.value)
        except (TypeError, ValueError) as exc:
            raise DatatypeParameterError(
                f"{parameter.name} value {parameter.value!r} is not valid for xs:{type_name}: {exc}"
            ) from exc
        bounds[parameter.name] = (
            _xsd10_temporal_key(type_name, text),
            text,
            _has_timezone(text),
        )
    if "minInclusive" in bounds and "minExclusive" in bounds:
        raise DatatypeParameterError("cannot specify both 'minInclusive' and 'minExclusive'")
    if "maxInclusive" in bounds and "maxExclusive" in bounds:
        raise DatatypeParameterError("cannot specify both 'maxInclusive' and 'maxExclusive'")
    lower = bounds.get("minInclusive") or bounds.get("minExclusive")
    upper = bounds.get("maxInclusive") or bounds.get("maxExclusive")
    if lower is not None and upper is not None:
        comparison = _xsd10_temporal_compare_keys(upper[0], upper[2], lower[0], lower[2])
        exclusive = "minExclusive" in bounds or "maxExclusive" in bounds
        if comparison is None or comparison < 0 or (comparison == 0 and exclusive):
            raise DatatypeParameterError(
                "the declared temporal bounds leave no value in the datatype's value space"
            )
    return tuple((name, key, text, zoned) for name, (key, text, zoned) in sorted(bounds.items()))


@dataclass(frozen=True)
class _Xsd10Duration:
    """The six XSD 1.0 duration coordinates plus the sign."""

    sign: int
    years: int
    months: int
    days: int
    hours: int
    minutes: int
    seconds: decimal.Decimal


#: The four qualified dateTimes XSD 1.0 §3.2.6.2 uses to decide duration order.
_DURATION_REFERENCE_DATES = ((1696, 9), (1697, 2), (1903, 3), (1903, 7))


def _parse_decimal_int(text: str) -> int:
    """Parse an arbitrary-length decimal integer without int-string limits."""
    value = 0
    for start in range(0, len(text), 400):
        chunk = text[start : start + 400]
        value = value * 10 ** len(chunk) + int(chunk)
    return value


def _parse_xsd10_duration(text: str) -> _Xsd10Duration:
    """Parse a duration lexical already validated by the shared Duration class."""
    match = _DURATION_PARTS.fullmatch(text)
    assert match is not None
    years, months, days, hours, minutes, seconds_text = match.groups()
    components = [
        _parse_decimal_int(component[:-1]) if component is not None else 0
        for component in (years, months, days, hours, minutes)
    ]
    seconds = decimal.Decimal(seconds_text[:-1]) if seconds_text is not None else decimal.Decimal(0)
    zero = not any(components) and not seconds
    sign = 1 if zero or not text.startswith("-") else -1
    return _Xsd10Duration(
        sign,
        components[0],
        components[1],
        components[2],
        components[3],
        components[4],
        seconds,
    )


def _xsd10_duration_value(text: str) -> tuple[_Xsd10Duration, str]:
    """Validate an XSD 1.0 duration lexical and return its coordinates."""
    text = whitespace_transform("collapse", text)
    try:
        Duration(text)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Not a valid xs:duration: {text!r}") from exc
    return _parse_xsd10_duration(text), text


def _add_duration_to_reference(year: int, month: int, duration: _Xsd10Duration) -> decimal.Decimal:
    """The XSD 1.0 §E result of adding *duration* to a reference midnight.

    The four reference dateTimes are always the first of a month, so the
    spec's day-pinning step is vacuous.  The result is a Decimal second count
    on the UTC timeline, which is all the duration order needs.
    """
    signed_months = duration.sign * duration.months
    temp_month = month + signed_months
    result_month = (temp_month - 1) % 12 + 1
    result_year = year + duration.sign * duration.years + (temp_month - 1) // 12

    signed_seconds = duration.sign * duration.seconds
    second_carry = math.floor(signed_seconds / 60)
    result_second = signed_seconds - second_carry * 60

    signed_minutes = duration.sign * duration.minutes + second_carry
    result_minute = signed_minutes % 60
    minute_carry = signed_minutes // 60

    signed_hours = duration.sign * duration.hours + minute_carry
    result_hour = signed_hours % 24
    day_carry = signed_hours // 24

    day_number = (
        _days_from_civil(result_year, result_month, 1) + duration.sign * duration.days + day_carry
    )
    return (
        decimal.Decimal(day_number * 86400)
        + result_hour * 3600
        + result_minute * 60
        + result_second
    )


def _xsd10_duration_compare(left: _Xsd10Duration, right: _Xsd10Duration) -> int | None:
    """Compare durations under the XSD 1.0 §3.2.6.2 partial order.

    Returns ``-1`` or ``1`` when every reference-date result is strictly
    ordered, ``0`` for identical duration values, and ``None`` for every
    indeterminate case (including distinct values that add identically to all
    four references, such as ``P1Y`` and ``P12M``).  XSD 1.0 §3.2.6.3 makes
    every indeterminate bound comparison false.
    """
    if left == right:
        return 0
    directions: set[int] = set()
    for year, month in _DURATION_REFERENCE_DATES:
        left_result = _add_duration_to_reference(year, month, left)
        right_result = _add_duration_to_reference(year, month, right)
        if left_result < right_result:
            directions.add(-1)
        elif left_result > right_result:
            directions.add(1)
        else:
            directions.add(0)
    if directions == {-1}:
        return -1
    if directions == {1}:
        return 1
    return None


def _xsd10_duration_bounds(
    parameters: tuple[DatatypeParameter, ...],
) -> tuple[tuple[str, _Xsd10Duration, str], ...]:
    """Parse and conflict-check the partial-order bounds of xs:duration."""
    bounds: dict[str, tuple[_Xsd10Duration, str]] = {}
    for parameter in parameters:
        if parameter.name not in _BOUND_PARAMETER_NAMES:
            continue
        try:
            value, text = _xsd10_duration_value(parameter.value)
        except (TypeError, ValueError) as exc:
            raise DatatypeParameterError(
                f"{parameter.name} value {parameter.value!r} is not valid for xs:duration: {exc}"
            ) from exc
        bounds[parameter.name] = (value, text)
    if "minInclusive" in bounds and "minExclusive" in bounds:
        raise DatatypeParameterError("cannot specify both 'minInclusive' and 'minExclusive'")
    if "maxInclusive" in bounds and "maxExclusive" in bounds:
        raise DatatypeParameterError("cannot specify both 'maxInclusive' and 'maxExclusive'")
    lower = bounds.get("minInclusive") or bounds.get("minExclusive")
    upper = bounds.get("maxInclusive") or bounds.get("maxExclusive")
    if lower is not None and upper is not None:
        # The interval is non-empty only when the upper bound is strictly
        # above the lower bound, or they are the identical duration and both
        # sides are inclusive.  An indeterminate pair can contain no value:
        # lower <= x <= upper would otherwise make the bounds comparable.
        comparison = _xsd10_duration_compare(upper[0], lower[0])
        exclusive = "minExclusive" in bounds or "maxExclusive" in bounds
        if comparison is None or comparison < 0 or (comparison == 0 and exclusive):
            raise DatatypeParameterError(
                "the declared duration bounds leave no value in the partial order"
            )
    return tuple((name, value, text) for name, (value, text) in sorted(bounds.items()))


def _xsd10_anyuri_value(text: str) -> AnyURI:
    """Validate an XSD 1.0 anyURI lexical and return its value object."""
    normalize_uri_reference(text)
    return AnyURI(text)


class Datatype(ABC):
    """The compiled behavior of one RELAX NG datatype.

    Implementations that serve as pattern payloads must also be immutable,
    hashable, value-comparable, and deterministically ``repr``-able, because
    the pattern IR interns and canonically orders them.
    """

    @abstractmethod
    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        """Return whether *lexical* is a legal value in *context*."""

    @abstractmethod
    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        """Return whether two lexical values denote the same datatype value."""

    def decode(self, lexical: str, context: DatatypeContext) -> Any:
        """Decode *lexical* into a Python value, when the datatype supports it."""
        raise UnsupportedDatatypeOperation(f"{type(self).__name__} does not provide a decoder")

    def encode(self, value: Any, context: DatatypeContext) -> str:
        """Encode a Python value as a lexical form, when the datatype supports it."""
        raise UnsupportedDatatypeOperation(f"{type(self).__name__} does not provide an encoder")


class DatatypeFactory(ABC):
    """A datatype library: compile a type name and parameters into a datatype."""

    @abstractmethod
    def create(
        self,
        type_name: str,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        """Compile one datatype or report why it is unavailable."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


@dataclass(frozen=True, repr=False)
class ScalarDatatype(Datatype):
    """A frozen scalar datatype backed by one pyxsd XSD built-in class.

    ``context`` is the schema-side context retained for later ``value``
    matching.  It is excluded from equality for context-insensitive types;
    :attr:`semantic_context` carries the namespace bindings that actually
    affect QName/NOTATION identity.
    """

    library_uri: str
    type_name: str
    xsd_type: type[Any]
    parameters: tuple[DatatypeParameter, ...]
    context: DatatypeContext = field(compare=False)
    #: Derived entirely from ``parameters``; excluded from equality so a
    #: compiled regex object's identity can never affect datatype identity.
    constraints: FacetConstraints = field(compare=False)
    white_space: str
    semantic_context: tuple[tuple[str, str], ...] = field(init=False, repr=False)
    #: XSD 1.0 temporal bounds use the adapter's year numbering, not the
    #: shared facet ordering key.  Derived from ``parameters`` like the other
    #: facets, so it is excluded from datatype identity.
    temporal_bounds: tuple[tuple[str, decimal.Decimal, str, bool], ...] = field(
        default=(), compare=False, repr=False
    )
    #: XSD 1.0 duration bounds use the four-reference-date partial order, not
    #: the shared total-order helper.  Also derived from ``parameters``.
    duration_bounds: tuple[tuple[str, _Xsd10Duration, str], ...] = field(
        default=(), compare=False, repr=False
    )

    def __post_init__(self) -> None:
        semantic = tuple(self.context.namespaces.items()) if self.type_name in _QNAME_TYPES else ()
        object.__setattr__(self, "semantic_context", semantic)

    def __repr__(self) -> str:
        """Represent exactly the fields that participate in equality."""
        return (
            f"ScalarDatatype(library_uri={self.library_uri!r}, "
            f"type_name={self.type_name!r}, xsd_type={self.xsd_type!r}, "
            f"parameters={self.parameters!r}, white_space={self.white_space!r}, "
            f"semantic_context={self.semantic_context!r})"
        )

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        try:
            self._checked_value(lexical, context)
        except (TypeError, ValueError):
            return False
        return True

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        """Compare two values in this datatype's value space.

        Facet parameters do not participate: RELAX NG's ``datatypeEqual``
        service has no parameters.  A lexical form outside the base datatype
        has no value and therefore compares unequal to everything.
        """
        left_value = self._equality_value(left, left_context)
        if left_value is None:
            return False
        right_value = self._equality_value(right, right_context)
        if right_value is None:
            return False
        return self._equality_key(left_value) == self._equality_key(right_value)

    def decode(self, lexical: str, context: DatatypeContext) -> Any:
        """Decode a validated lexical form into its Python value.

        ``hexBinary`` and ``base64Binary`` return ``bytes``; ``boolean``
        returns a Python ``bool``; QName and NOTATION return a
        context-resolved :class:`~pyxsd.xsd_data_types.QName`; the four
        year-bearing temporal types return a validated lexical ``str``
        subclass with a ``lexical()`` method; every other type returns its
        XSD value object (an ``int``, ``Decimal``, ``float``, or ``str``
        subclass).  Facets are enforced, so an out-of-range or
        pattern-violating lexical form raises ``TypeError`` or ``ValueError``
        instead of producing a value outside the datatype.
        """
        _, value = self._checked_value(lexical, context)
        if isinstance(value, Boolean):
            return bool(int(value))
        if isinstance(value, (Base64Binary, HexBinary)):
            return value.to_bytes()
        return value

    def encode(self, value: Any, context: DatatypeContext) -> str:
        """Encode a Python value as a canonical lexical form.

        Expected inputs are ``bytes``-like for binary types, ``bool`` or
        ``int`` for boolean, ``int`` for the integer family, ``Decimal`` or
        ``int`` for decimal, ``int`` or ``float`` for float/double, ``str`` for
        string and temporal types, and ``QName`` or ``str`` for QName and
        NOTATION.  The produced lexical form is validated against the
        datatype's facets.  A QName prefix must be bound in *context*; a
        resolved QName must denote the same namespace there, so no unresolved
        prefix is invented.
        """
        lexical = self._encode_lexical(value, context)
        self._checked_value(lexical, context)
        return lexical

    def _prepare(self, lexical: str, context: DatatypeContext) -> tuple[str, Any]:
        """Apply whitespace processing and build the XSD value object."""
        text = whitespace_transform(self.white_space, lexical)
        return text, self._build_value(text, context)

    def _checked_value(self, lexical: str, context: DatatypeContext) -> tuple[str, Any]:
        text, value = self._prepare(lexical, context)
        self.constraints.check(value, text)
        self._check_temporal_bounds(text)
        self._check_duration_bounds(text)
        return text, value

    def _check_temporal_bounds(self, text: str) -> None:
        """Enforce XSD 1.0 temporal bounds under the timezone partial order."""
        if not self.temporal_bounds:
            return
        actual_key = _xsd10_temporal_key(self.type_name, text)
        actual_zoned = _has_timezone(text)
        for name, bound_key, bound_text, bound_zoned in self.temporal_bounds:
            comparison = _xsd10_temporal_compare_keys(
                actual_key, actual_zoned, bound_key, bound_zoned
            )
            if name == "minInclusive":
                accepted = comparison in (0, 1)
            elif name == "minExclusive":
                accepted = comparison == 1
            elif name == "maxInclusive":
                accepted = comparison in (-1, 0)
            else:
                accepted = comparison == -1
            if not accepted:
                raise FacetViolation(
                    f"facet {name!r} violated: {text!r} is not ordered against "
                    f"the XSD 1.0 temporal bound {bound_text!r}",
                    ConstraintInfo(name, bound_text, text),
                )

    def _check_duration_bounds(self, text: str) -> None:
        """Enforce XSD 1.0 duration bounds under the partial order."""
        if not self.duration_bounds:
            return
        actual = _parse_xsd10_duration(text)
        for name, bound, bound_text in self.duration_bounds:
            comparison = _xsd10_duration_compare(actual, bound)
            if name == "minInclusive":
                accepted = comparison in (0, 1)
            elif name == "minExclusive":
                accepted = comparison == 1
            elif name == "maxInclusive":
                accepted = comparison in (-1, 0)
            else:
                accepted = comparison == -1
            if not accepted:
                raise FacetViolation(
                    f"facet {name!r} violated: {text!r} is not ordered against "
                    f"the XSD 1.0 duration bound {bound_text!r}",
                    ConstraintInfo(name, bound_text, text),
                )

    def _equality_value(self, lexical: str, context: DatatypeContext) -> Any | None:
        """Build a value for equality, ignoring facet parameters.

        RELAX NG's ``datatypeEqual`` service has no parameter argument, so a
        lexical form that fails a local facet restriction still has a value.
        A lexical form outside the base datatype has no value and compares
        unequal to everything.
        """
        try:
            _, value = self._prepare(lexical, context)
        except (TypeError, ValueError):
            return None
        return value

    def _build_value(self, text: str, context: DatatypeContext) -> Any:
        if self.type_name in _XSD10_YEAR_TYPE_NAMES:
            return _xsd10_temporal_value(self.type_name, text)
        if self.type_name == "anyURI":
            return _xsd10_anyuri_value(text)
        if self.type_name in _QNAME_TYPES:
            return self._build_qname(text, context)
        return self.xsd_type(text)

    def _build_qname(self, text: str, context: DatatypeContext) -> QName:
        bindings = _namespace_bindings(context)
        with qname_context(bindings):
            value = QName(text)
        prefix = value._prefix_
        if prefix and not bindings.get(prefix):
            raise TypeError(f"QName prefix {prefix!r} is not bound in the datatype context")
        return value

    def _equality_key(self, value: Any) -> tuple[Any, ...]:
        if self.type_name in _XSD10_TEMPORAL_TYPE_NAMES:
            text = str(value)
            return (
                self.type_name,
                _has_timezone(text),
                _xsd10_temporal_key(self.type_name, text),
            )
        if isinstance(value, QName):
            return ("QName", (value._uri_ or "", value._local_))
        if isinstance(value, Duration):
            return ("duration", _xsd10_duration_key(str(value)))
        return xsd_value_key(value)

    def _encode_lexical(self, value: Any, context: DatatypeContext) -> str:
        if self.type_name in _QNAME_TYPES:
            return self._encode_qname(value, context)
        if issubclass(self.xsd_type, (Base64Binary, HexBinary)):
            return self._encode_binary(value)
        if issubclass(self.xsd_type, Boolean):
            if not isinstance(value, (bool, int)):
                raise TypeError(f"expected a bool for xs:{self.type_name}")
            return self.xsd_type(value).lexical()
        if issubclass(self.xsd_type, Decimal):
            if isinstance(value, (bool, Boolean)):
                raise TypeError(f"expected a decimal for xs:{self.type_name}")
            if isinstance(value, decimal.Decimal):
                text = format(value, "f")
            elif isinstance(value, int):
                text = str(value)
            else:
                raise TypeError(f"expected a decimal for xs:{self.type_name}")
            return self.xsd_type(text).lexical()
        if issubclass(self.xsd_type, Integer):
            if isinstance(value, (bool, Boolean)) or not isinstance(value, int):
                raise TypeError(f"expected an integer for xs:{self.type_name}")
            return self.xsd_type(value).lexical()
        if issubclass(self.xsd_type, Double):
            if isinstance(value, (bool, Boolean)):
                raise TypeError(f"expected a number for xs:{self.type_name}")
            if not isinstance(value, (int, float)):
                raise TypeError(f"expected a number for xs:{self.type_name}")
            return self.xsd_type(cast(Any, value)).lexical()
        if not isinstance(value, str):
            raise TypeError(f"expected a string for xs:{self.type_name}")
        _, built = self._prepare(value, context)
        return str(built)

    def _encode_binary(self, value: Any) -> str:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError(f"expected bytes for xs:{self.type_name}")
        raw = bytes(value)
        if issubclass(self.xsd_type, HexBinary):
            return raw.hex().upper()
        return base64.b64encode(raw).decode("ascii")

    def _encode_qname(self, value: Any, context: DatatypeContext) -> str:
        if isinstance(value, QName):
            lexical = str(value)
            if value._resolved_:
                prefix = value._prefix_
                uri = value._uri_
                if prefix:
                    if not uri:
                        raise ValueError(
                            f"QName {lexical!r} has no namespace for prefix {prefix!r}"
                        )
                    if _namespace_bindings(context).get(prefix) != uri:
                        raise ValueError(
                            f"QName prefix {prefix!r} does not denote {uri!r} "
                            f"in the encoding context"
                        )
                else:
                    default = context.namespaces.get("") or None
                    if (uri or None) != default:
                        raise ValueError(
                            f"QName {lexical!r} denotes {uri!r}, but the encoding "
                            f"context has default namespace {default!r}"
                        )
                return lexical
            self._build_qname(lexical, context)
            return lexical
        if isinstance(value, str):
            self._build_qname(value, context)
            return value
        raise TypeError(f"expected a QName for xs:{self.type_name}")


class BuiltinDatatypeFactory(DatatypeFactory):
    """RELAX NG's built-in datatype library: ``string`` and ``token``."""

    def create(
        self,
        type_name: str,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        if parameters:
            raise DatatypeParameterError(
                "the RELAX NG built-in datatype library accepts no parameters"
            )
        xsd_type: type[Any]
        white_space: str
        if type_name == "string":
            xsd_type, white_space = String, "preserve"
        elif type_name == "token":
            xsd_type, white_space = Token, "collapse"
        else:
            raise UnsupportedDatatypeError(
                f"the RELAX NG built-in datatype library has no datatype {type_name!r}"
            )
        return ScalarDatatype(
            library_uri=BUILTIN_LIBRARY,
            type_name=type_name,
            xsd_type=xsd_type,
            parameters=(),
            context=context,
            constraints=FacetConstraints(),
            white_space=white_space,
        )


class _FacetSource:
    """The facet attributes :func:`build_constraints` reads from a representative."""

    xsdElement = None
    patterns: tuple[str, ...] = ()
    enumerations: tuple[str, ...] = ()
    whiteSpace: str | None = None
    explicitTimezone: str | None = None

    def __init__(self, parameters: tuple[DatatypeParameter, ...]) -> None:
        values = {parameter.name: parameter.value for parameter in parameters}
        for name in _FACET_NAMES:
            setattr(self, name, values.get(name))


def _xsd_white_space(xsd_type: type[Any]) -> str:
    """The XSD fixed ``whiteSpace`` value of a built-in type.

    ``xs:string`` and the unconstrained ``xs:anySimpleType`` preserve their
    lexical form; ``xs:normalizedString`` replaces tab/newline/CR with spaces;
    every other built-in collapses XML whitespace.
    """
    name = xsd_type.__dict__.get("name")
    if name in ("string", "anySimpleType"):
        return "preserve"
    if name == "normalizedString":
        return "replace"
    return "collapse"


def _prepare_xsd_parameters(
    type_name: str, parameters: tuple[DatatypeParameter, ...]
) -> tuple[DatatypeParameter, ...]:
    """Validate parameter names and duplicates, then return a canonical order."""
    seen: set[str] = set()
    for parameter in parameters:
        if not isinstance(parameter, DatatypeParameter):
            raise TypeError("datatype parameters must be DatatypeParameter instances")
        if parameter.name in _REJECTED_XSD_PARAMETERS:
            raise DatatypeParameterError(
                f"the RELAX NG XSD datatype guidelines reject the {parameter.name!r} parameter"
            )
        if parameter.name not in _XSD_PARAMETER_NAMES:
            raise DatatypeParameterError(
                f"unknown XSD datatype parameter {parameter.name!r} for xs:{type_name}"
            )
        if parameter.name in _BOUND_PARAMETER_NAMES and type_name not in _ORDERED_TYPE_NAMES:
            raise DatatypeParameterError(
                f"facet {parameter.name!r} is not applicable to xs:{type_name}"
            )
        if parameter.name != "pattern":
            if parameter.name in seen:
                raise DatatypeParameterError(
                    f"duplicate {parameter.name!r} parameter for xs:{type_name}"
                )
            seen.add(parameter.name)
    return tuple(sorted(parameters, key=lambda item: (item.name, item.value)))


def _unsupported_xsd_type(type_name: str) -> UnsupportedDatatypeError:
    """Return the explicit capability failure for an unavailable XSD type."""
    if type_name in _XSD_11_ONLY_TYPE_NAMES:
        return UnsupportedDatatypeError(
            f"xsd:{type_name} is an XSD 1.1 datatype and is not available "
            f"in the XSD 1.0 datatype library"
        )
    if type_name == "anyType":
        return UnsupportedDatatypeError(
            "xsd:anyType is not a simple datatype in the XSD 1.0 datatype library"
        )
    return UnsupportedDatatypeError(f"unknown XSD datatype {type_name!r}")


class XsdDatatypeFactory(DatatypeFactory):
    """The RELAX NG XSD datatype library with XSD 1.0 semantics."""

    def create(
        self,
        type_name: str,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        xsd_type = _XSD_10_TYPES.get(type_name)
        if xsd_type is None:
            raise _unsupported_xsd_type(type_name)
        prepared = _prepare_xsd_parameters(type_name, parameters)
        temporal = type_name in _XSD10_TEMPORAL_TYPE_NAMES
        duration = type_name == "duration"
        # XSD 1.0 temporal and duration bounds need adapter-level ordering, so
        # they are parsed outside the shared facet builder.
        source_parameters = (
            tuple(
                parameter for parameter in prepared if parameter.name not in _BOUND_PARAMETER_NAMES
            )
            if temporal or duration
            else prepared
        )
        result = build_constraints(
            _FacetSource(source_parameters),
            xsd_type,
            None,
            base_factory=xsd_type,
            xsd_version="1.0",
        )
        problems = (*result.errors, *result.conflicts)
        if problems:
            raise DatatypeParameterError(
                f"invalid parameters for xs:{type_name}: " + "; ".join(problems)
            )
        constraints = result.constraints
        patterns = tuple(
            pattern
            for parameter in prepared
            if parameter.name == "pattern"
            for pattern in (self._compile_pattern(parameter.value),)
        )
        if patterns:
            constraints = replace(constraints, patterns=tuple((pattern,) for pattern in patterns))
        temporal_bounds = _xsd10_temporal_bounds(type_name, prepared) if temporal else ()
        duration_bounds = _xsd10_duration_bounds(prepared) if duration else ()
        return ScalarDatatype(
            library_uri=XSD_LIBRARY,
            type_name=type_name,
            xsd_type=xsd_type,
            parameters=prepared,
            context=context,
            constraints=constraints,
            white_space=_xsd_white_space(xsd_type),
            temporal_bounds=temporal_bounds,
            duration_bounds=duration_bounds,
        )

    @staticmethod
    def _compile_pattern(text: str) -> re.Pattern[str]:
        try:
            return compile_pattern(text, xsd_version="1.0")
        except ValueError as exc:
            raise DatatypeParameterError(str(exc)) from exc


def _namespace_bindings(context: DatatypeContext) -> dict[str, str]:
    """The effective prefix map, including the implicitly bound ``xml`` prefix."""
    bindings = dict(context.namespaces)
    bindings.setdefault("xml", _XML_NAMESPACE)
    return bindings


def _xsd10_duration_key(text: str) -> tuple[int, int, int, int, int, int, decimal.Decimal]:
    """The XSD 1.0 six-dimensional identity key for ``xs:duration``.

    XSD 1.0 treats the year, month, day, hour, minute and second components as
    independent coordinates, so ``P1Y`` and ``P12M`` are distinct values.
    XSD 1.1 changed this; the RELAX NG XSD adapter deliberately keeps 1.0.
    """
    parsed = _parse_xsd10_duration(text)
    return (
        parsed.sign,
        parsed.years,
        parsed.months,
        parsed.days,
        parsed.hours,
        parsed.minutes,
        parsed.seconds,
    )


def resolve_datatype_selector(selector: DatatypeSelector) -> tuple[str, str]:
    """Resolve a selector to ``(library_uri, type_name)``.

    A bare string names a type in the built-in library.  The documented
    ``xsd:`` shorthand names a type in the XSD library; it is a fixed alias,
    never a prefix lookup against the datatype context.  A tuple is an
    explicit ``(library_uri, type_name)`` selector.
    """
    if isinstance(selector, str):
        if selector.startswith(_XSD_SHORTHAND):
            type_name = selector[len(_XSD_SHORTHAND) :]
            if not type_name:
                raise UnsupportedDatatypeError("the 'xsd:' datatype selector requires a type name")
            return (XSD_LIBRARY, type_name)
        if ":" in selector:
            raise UnsupportedDatatypeError(
                f"unsupported datatype selector {selector!r}: use the 'xsd:' "
                f"shorthand or a (library_uri, type_name) tuple"
            )
        return (BUILTIN_LIBRARY, selector)
    if isinstance(selector, tuple) and len(selector) == 2:
        library_uri, type_name = selector
        if not isinstance(library_uri, str) or not isinstance(type_name, str):
            raise TypeError("a datatype selector tuple must contain two strings")
        return (library_uri, type_name)
    raise TypeError("a datatype selector must be a string or a (library_uri, type_name) tuple")


@dataclass(frozen=True)
class DatatypeLibraryRegistration:
    """A custom datatype library factory and its stable configuration identity."""

    factory: DatatypeFactory
    fingerprint: str


class DatatypeLibraryRegistry:
    """The available datatype libraries with fixed built-in/XSD identities."""

    def __init__(self) -> None:
        self._registrations: dict[str, DatatypeLibraryRegistration] = {
            BUILTIN_LIBRARY: DatatypeLibraryRegistration(
                BuiltinDatatypeFactory(), BUILTIN_FINGERPRINT
            ),
            XSD_LIBRARY: DatatypeLibraryRegistration(XsdDatatypeFactory(), XSD_FINGERPRINT),
        }

    def library_uris(self) -> tuple[str, ...]:
        """The registered library URIs in deterministic order."""
        return tuple(sorted(self._registrations))

    def registration(self, uri: str) -> DatatypeLibraryRegistration:
        """Return the registration for *uri* or raise an unsupported failure."""
        try:
            return self._registrations[uri]
        except KeyError:
            raise UnsupportedDatatypeError(
                f"unsupported RELAX NG datatype library {uri!r}"
            ) from None

    def register(self, uri: str, registration: DatatypeLibraryRegistration) -> None:
        """Register a custom datatype library under *uri*.

        The built-in and XSD identities are fixed and cannot be replaced.
        Every custom fingerprint must be stable and process-independent, so a
        blank string or a captured object address is rejected.
        """
        if not isinstance(uri, str):
            raise TypeError("a datatype library URI must be a string")
        if not isinstance(registration, DatatypeLibraryRegistration):
            raise TypeError("expected a DatatypeLibraryRegistration")
        if uri in self._registrations:
            raise DatatypeRegistrationError(f"a datatype library is already registered for {uri!r}")
        _validate_fingerprint(registration.fingerprint)
        if not isinstance(registration.factory, DatatypeFactory) and not callable(
            getattr(registration.factory, "create", None)
        ):
            raise DatatypeRegistrationError(
                "a datatype library factory must provide a create() method"
            )
        self._registrations[uri] = registration

    def resolve(self, selector: DatatypeSelector) -> tuple[str, str]:
        """Resolve a selector without constructing a datatype."""
        return resolve_datatype_selector(selector)

    def create(
        self,
        selector: DatatypeSelector,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        """Compile the selected datatype."""
        library_uri, type_name = self.resolve(selector)
        return self.registration(library_uri).factory.create(type_name, parameters, context)


def _validate_fingerprint(fingerprint: str) -> None:
    """Reject a blank or process-dependent library fingerprint."""
    if not isinstance(fingerprint, str):
        raise DatatypeRegistrationError("a custom datatype library fingerprint must be a string")
    if not fingerprint.strip():
        raise DatatypeRegistrationError("a custom datatype library fingerprint must not be blank")
    if _PROCESS_DEPENDENT_FINGERPRINT.search(fingerprint):
        raise DatatypeRegistrationError(
            "a custom datatype library fingerprint must not depend on a process address"
        )
