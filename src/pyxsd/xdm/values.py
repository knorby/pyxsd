"""Immutable XDM 3.1 value items.

XDM sequences follow the data model, not Python truthiness:

- a sequence is a tuple of items and the empty sequence is ``()``;
- a Python string is one ``xs:string`` item, never an iterable of characters;
- ``None`` is not an XDM value;
- maps, arrays, and function items are immutable and keep XDM identity
  semantics rather than behaving like Python dictionaries or lists;
- atomic values retain their XSD type identity; and
- function items retain arity, signature, implementation, and closure.

Host Python containers and pyxsd/elementpath datatypes cross the boundary only
through :func:`as_xdm` (and the explicit function-item constructor).  The
module does not import stylesheet execution code.
"""

from __future__ import annotations

import base64
import math
import struct
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any, TypeAlias, cast

from elementpath.datatypes import AnyAtomicType as _ElementPathAtomic

from pyxsd import xsd_data_types as _xsd
from pyxsd.namespaces import XSD_NS

__all__ = [
    "XdmArray",
    "XdmAtomic",
    "XdmFunction",
    "XdmFunctionSignature",
    "XdmItem",
    "XdmMap",
    "XdmSequence",
    "XdmSequenceType",
    "as_xdm",
]


class XdmItem:
    """Base class for one XDM item.

    Concrete items are immutable.  Nodes extend this class in
    :mod:`pyxsd.xdm.nodes`; they are not value items and are never compared by
    value here.
    """

    __slots__ = ()


XdmSequence: TypeAlias = tuple[XdmItem, ...]
"""An XDM sequence: an ordered tuple of items, possibly empty."""


# ---------------------------------------------------------------------------
# XSD type identity and value families

_STRING_LOCALS = frozenset(
    {
        "anyURI",
        "ENTITIES",
        "ENTITY",
        "ID",
        "IDREF",
        "IDREFS",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "NMTOKENS",
        "normalizedString",
        "string",
        "token",
        "untypedAtomic",
    }
)
_NUMERIC_LOCALS = frozenset(
    {
        "byte",
        "decimal",
        "double",
        "float",
        "int",
        "integer",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "short",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)
_TEMPORAL_LOCALS = frozenset(
    {
        "date",
        "dateTime",
        "dateTimeStamp",
        "gDay",
        "gMonth",
        "gMonthDay",
        "gYear",
        "gYearMonth",
        "time",
    }
)
_DURATION_LOCALS = frozenset({"dayTimeDuration", "duration", "yearMonthDuration"})
_BINARY_LOCALS = frozenset({"base64Binary", "hexBinary"})
_QNAME_LOCALS = frozenset({"NOTATION", "QName"})
_LIST_LOCALS = frozenset({"ENTITIES", "IDREFS", "List", "NMTOKENS"})

_FAMILIES: dict[str, str] = {}
_FAMILIES.update((local, "string") for local in _STRING_LOCALS)
_FAMILIES.update((local, "numeric") for local in _NUMERIC_LOCALS)
_FAMILIES.update((local, "temporal") for local in _TEMPORAL_LOCALS)
_FAMILIES.update((local, "duration") for local in _DURATION_LOCALS)
_FAMILIES.update((local, "binary") for local in _BINARY_LOCALS)
_FAMILIES.update((local, "qname") for local in _QNAME_LOCALS)
_FAMILIES["boolean"] = "boolean"

_NUMERIC_PROMOTIONS = {
    "integer": 0,
    "positiveInteger": 0,
    "nonPositiveInteger": 0,
    "negativeInteger": 0,
    "nonNegativeInteger": 0,
    "long": 0,
    "int": 0,
    "short": 0,
    "byte": 0,
    "unsignedLong": 0,
    "unsignedInt": 0,
    "unsignedShort": 0,
    "unsignedByte": 0,
    "decimal": 1,
    "float": 2,
    "double": 3,
}

#: The built-in XSD derivation hierarchy, independent of Python storage
#: inheritance.  ``xsd.Float`` inherits ``xsd.Double`` in Python but is not
#: derived from ``xs:double`` in XSD, and the string-storage families
#: (``Date``, ``QName``, ``Base64Binary``, ...) are unrelated to
#: ``xs:string``.  Only names in this table participate in function
#: conversion subtype substitution.
_XSD_BASE_TYPES: dict[str, str] = {
    "anySimpleType": "anyType",
    "anyAtomicType": "anySimpleType",
    "string": "anyAtomicType",
    "normalizedString": "string",
    "token": "normalizedString",
    "language": "token",
    "Name": "token",
    "NCName": "Name",
    "NMTOKEN": "token",
    "ID": "NCName",
    "IDREF": "NCName",
    "ENTITY": "NCName",
    "boolean": "anyAtomicType",
    "decimal": "anyAtomicType",
    "integer": "decimal",
    "nonPositiveInteger": "integer",
    "negativeInteger": "nonPositiveInteger",
    "long": "integer",
    "int": "long",
    "short": "int",
    "byte": "short",
    "nonNegativeInteger": "integer",
    "unsignedLong": "nonNegativeInteger",
    "unsignedInt": "unsignedLong",
    "unsignedShort": "unsignedInt",
    "unsignedByte": "unsignedShort",
    "positiveInteger": "nonNegativeInteger",
    "float": "anyAtomicType",
    "double": "anyAtomicType",
    "duration": "anyAtomicType",
    "yearMonthDuration": "duration",
    "dayTimeDuration": "duration",
    "dateTime": "anyAtomicType",
    "dateTimeStamp": "dateTime",
    "date": "anyAtomicType",
    "time": "anyAtomicType",
    "gYear": "anyAtomicType",
    "gYearMonth": "anyAtomicType",
    "gMonth": "anyAtomicType",
    "gMonthDay": "anyAtomicType",
    "gDay": "anyAtomicType",
    "hexBinary": "anyAtomicType",
    "base64Binary": "anyAtomicType",
    "anyURI": "anyAtomicType",
    "QName": "anyAtomicType",
    "NOTATION": "anyAtomicType",
    "untypedAtomic": "anyAtomicType",
}


def _builtin_datatypes() -> dict[str, type[_xsd.XsdDataType]]:
    datatypes: dict[str, type[_xsd.XsdDataType]] = {}
    for export in _xsd.__all__:
        datatype = getattr(_xsd, export)
        if (
            isinstance(datatype, type)
            and issubclass(datatype, _xsd.XsdDataType)
            and "name" in datatype.__dict__
        ):
            datatypes[datatype.name] = datatype
    return datatypes


_BUILTIN_DATATYPES = _builtin_datatypes()
_CAST_DATATYPES = {
    name: datatype
    for name, datatype in _BUILTIN_DATATYPES.items()
    if name not in _LIST_LOCALS
    and name
    not in {
        "NOTATION",
        "QName",
        "anySimpleType",
        "anyType",
    }
}


def _datatype_local_name(datatype: type[Any]) -> str:
    name = getattr(datatype, "name", None)
    if not isinstance(name, str) or not name:
        raise TypeError("an XDM atomic datatype must have an XSD type name")
    return name


def _xsd_type_name(datatype: type[Any]) -> str:
    local = _datatype_local_name(datatype)
    if datatype in _BUILTIN_DATATYPES.values():
        return f"{{{XSD_NS}}}{local}"
    return local


def _local_type_name(type_name: str) -> str:
    if type_name.startswith("{"):
        return type_name.partition("}")[2]
    return type_name.rpartition(":")[2]


def _is_elementpath_atomic(value: Any) -> bool:
    return isinstance(value, _ElementPathAtomic) and type(value).__module__.startswith(
        "elementpath."
    )


def _is_xsd_subtype(item_local: str, expected_local: str) -> bool:
    """Whether *item_local* names the same XSD type as or a subtype of *expected_local*."""
    current = item_local
    while True:
        if current == expected_local:
            return True
        parent = _XSD_BASE_TYPES.get(current)
        if parent is None:
            return False
        current = parent


def _is_nan(value: Any) -> bool:
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, Decimal):
        return value.is_nan()
    return False


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _as_float32(value: Any) -> float:
    number = _as_float(value)
    try:
        return struct.unpack(">f", struct.pack(">f", number))[0]
    except OverflowError:
        return math.inf if number > 0 else -math.inf


def _is_infinite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isinf(value)
    if isinstance(value, Decimal):
        return value.is_infinite()
    return False


def _numeric_eq(left_value: Any, left_local: str, right_value: Any, right_local: str) -> bool:
    """XPath numeric ``eq`` using the least common numeric type.

    A decimal/double pair converts to binary64, while a decimal/float pair
    converts to binary32 (the least common type is ``xs:float``).  Two exact
    integer/decimal values stay exact.  XPath promotion is intentionally
    non-transitive across mixed numeric types; ``same_key`` has the separate,
    transitive exact comparison required for map keys.
    """
    if left_local == "double" or right_local == "double":
        return _as_float(left_value) == _as_float(right_value)
    if left_local == "float" or right_local == "float":
        return _as_float32(left_value) == _as_float32(right_value)
    return left_value == right_value


def _numeric_same_key(left_value: Any, right_value: Any) -> bool:
    """F&O 3.1 ``op:same-key`` numeric equality.

    Finite numeric values compare as exact rational numbers with no rounding,
    so a binary64 or binary32 value can differ from the exact decimal it would
    be promoted to by ``eq``.  NaNs and equal-signed infinities are the same
    key; positive and negative zero compare equal.
    """
    left_nan = _is_nan(left_value)
    right_nan = _is_nan(right_value)
    if left_nan or right_nan:
        return left_nan and right_nan
    left_infinite = _is_infinite(left_value)
    right_infinite = _is_infinite(right_value)
    if left_infinite or right_infinite:
        if not (left_infinite and right_infinite):
            return False
        return (_as_float(left_value) > 0) == (_as_float(right_value) > 0)
    return Fraction(left_value) == Fraction(right_value)


def _actual_atomic_datatype(value: Any) -> type[Any] | None:
    """The payload's actual XSD datatype class, if it carries one.

    Python storage inheritance is deliberately not consulted: an
    ``xsd.NMTOKENS``, ``xsd.Date``, ``xsd.QName``, or ``xsd.Float`` value has
    its own XSD datatype identity regardless of its Python base class.
    """
    if isinstance(value, _xsd.XsdDataType):
        return type(value)
    if _is_elementpath_atomic(value):
        return type(value)
    return None


def _validate_atomic_payload(value: Any, type_name: str) -> None:
    """Enforce the public atomic invariant for a value and its type name."""
    if value is None:
        raise TypeError("None is not an XDM atomic value")
    declared_local = _local_type_name(type_name)
    if isinstance(value, _xsd.TypeList) or declared_local in _LIST_LOCALS:
        raise TypeError("XSD list values are not atomic XDM items")
    actual_type = _actual_atomic_datatype(value)
    if actual_type is not None:
        actual_local = _datatype_local_name(actual_type)
        if actual_local in _LIST_LOCALS:
            raise TypeError("XSD list values are not atomic XDM items")
        if actual_local != declared_local:
            raise TypeError("an XDM atomic value must use its actual XSD datatype")
    try:
        hash(value)
    except TypeError as exc:
        raise TypeError("an XDM atomic value must be hashable") from exc


def _qname_key(value: Any) -> tuple[str, str, str]:
    if isinstance(value, _xsd.QName):
        if value._resolved_:
            if value._uri_ or not value._prefix_:
                return ("resolved", value._uri_ or "", value._local_)
            return ("lexical", "", str(value))
        return ("lexical", "", str(value))
    namespace = getattr(value, "namespace", None)
    local_name = getattr(value, "local_name", None)
    if isinstance(local_name, str):
        return ("resolved", namespace if isinstance(namespace, str) else "", local_name)
    return ("lexical", "", str(value))


# ---------------------------------------------------------------------------
# Atomic values


@dataclass(frozen=True, slots=True, eq=False)
class XdmAtomic(XdmItem):
    """One immutable XDM atomic value with its XSD type identity."""

    value: Any
    type_name: str
    datatype: type[Any] | None = None

    def __post_init__(self) -> None:
        _validate_atomic_payload(self.value, self.type_name)

    @classmethod
    def string(cls, value: str) -> XdmAtomic:
        if not isinstance(value, str):
            raise TypeError("xs:string requires a string value")
        return cls(_xsd.String(value), f"{{{XSD_NS}}}string", _xsd.String)

    @classmethod
    def integer(cls, value: int) -> XdmAtomic:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("xs:integer requires an integer value")
        return cls(_xsd.Integer(value), f"{{{XSD_NS}}}integer", _xsd.Integer)

    @classmethod
    def decimal(cls, value: int | Decimal | str) -> XdmAtomic:
        if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
            raise TypeError("xs:decimal requires an int, Decimal, or string value")
        decimal_value = _xsd.Decimal(value)  # type: ignore[arg-type]
        if decimal_value.is_nan() or decimal_value.is_infinite():
            raise TypeError("xs:decimal requires a finite value")
        return cls(decimal_value, f"{{{XSD_NS}}}decimal", _xsd.Decimal)

    @classmethod
    def double(cls, value: int | float) -> XdmAtomic:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("xs:double requires an int or float value")
        return cls(_xsd.Double(value), f"{{{XSD_NS}}}double", _xsd.Double)  # type: ignore[arg-type]

    @classmethod
    def float(cls, value: int | float) -> XdmAtomic:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("xs:float requires an int or float value")
        return cls(_xsd.Float(value), f"{{{XSD_NS}}}float", _xsd.Float)  # type: ignore[arg-type]

    @classmethod
    def boolean(cls, value: bool) -> XdmAtomic:
        if not isinstance(value, bool):
            raise TypeError("xs:boolean requires a bool value")
        return cls(_xsd.Boolean(value), f"{{{XSD_NS}}}boolean", _xsd.Boolean)

    @classmethod
    def untyped_atomic(cls, value: str) -> XdmAtomic:
        if not isinstance(value, str):
            raise TypeError("xs:untypedAtomic requires a string value")
        return cls(str(value), f"{{{XSD_NS}}}untypedAtomic", None)

    @classmethod
    def any_uri(cls, value: str) -> XdmAtomic:
        if not isinstance(value, str):
            raise TypeError("xs:anyURI requires a string value")
        return cls(_xsd.AnyURI(value), f"{{{XSD_NS}}}anyURI", _xsd.AnyURI)

    @classmethod
    def typed(cls, value: Any, datatype: type[Any]) -> XdmAtomic:
        """Wrap a value and its XSD datatype at the atomic invariant boundary.

        List values are not atomic, the payload must carry its actual XSD
        datatype class (Python storage bases are not accepted), and the payload
        must be hashable so it can serve as an XDM map key.  The function-item
        layer never constructs an atomic from a mutable Python container.
        """
        if not isinstance(datatype, type):
            raise TypeError("an XDM atomic datatype must be a type")
        type_name = _xsd_type_name(datatype)
        _validate_atomic_payload(value, type_name)
        actual_type = _actual_atomic_datatype(value)
        if actual_type is None:
            raise TypeError("an XDM atomic value must be an instance of its datatype")
        if actual_type is not datatype:
            raise TypeError("an XDM atomic value must use its actual XSD datatype")
        return cls(value, type_name, datatype)

    @property
    def lexical(self) -> str:
        """The XSD lexical form, including ``INF``/``-INF``/``NaN`` spellings."""
        value = self.value
        if isinstance(value, _xsd.XsdDataType):
            return value.lexical()
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float):
            if math.isnan(value):
                return "NaN"
            if math.isinf(value):
                return "INF" if value > 0 else "-INF"
        return str(value)

    def __str__(self) -> str:
        return self.lexical

    @property
    def _is_numeric(self) -> bool:
        return _FAMILIES.get(_local_type_name(self.type_name)) == "numeric"

    def _equality_key(self) -> tuple[Any, ...]:
        local = _local_type_name(self.type_name)
        family = _FAMILIES.get(local, "other")
        if family == "string":
            return ("string", str(self.value))
        if family == "numeric":
            return ("numeric",)
        if family == "boolean":
            return ("boolean", bool(self.value))
        if family == "qname":
            return ("qname", *_qname_key(self.value))
        if family in {"temporal", "duration", "binary"}:
            if isinstance(self.value, _xsd.XsdDataType):
                return ("xsd", _xsd.xsd_value_key(self.value))
            return ("value", local, self.value)
        return ("other", self.type_name, self.value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, XdmAtomic):
            return False
        if self._is_numeric and other._is_numeric:
            if _is_nan(self.value) or _is_nan(other.value):
                return False
            return _numeric_eq(
                self.value,
                _local_type_name(self.type_name),
                other.value,
                _local_type_name(other.type_name),
            )
        return self._equality_key() == other._equality_key()

    def __hash__(self) -> int:
        if self._is_numeric:
            if _is_nan(self.value):
                return hash(("numeric", "nan"))
            # Binary32 is the coarsest numeric value space used by any ``eq``
            # arm.  Hashing to it keeps the Python invariant that equal values
            # share a hash even though mixed-type XPath ``eq`` is not
            # transitive.
            return hash(("numeric", _as_float32(self.value)))
        return hash(self._equality_key())

    def same_key(self, other: object) -> bool:
        """XDM ``op:same-key`` map-key equality.

        Numeric keys compare by exact mathematical value with no rounding, so
        map keys stay transitive and distinct ``xs:decimal``/``xs:double``
        values are not collapsed.  Strings and their derived types compare by
        value, QNames compare by expanded name, and two NaNs are the same key
        even though ``NaN eq NaN`` is false.
        """
        if not isinstance(other, XdmAtomic):
            return False
        if self._is_numeric and other._is_numeric:
            return _numeric_same_key(self.value, other.value)
        return self._equality_key() == other._equality_key()

    def cast(self, type_name: str) -> XdmAtomic:
        """Cast this value to another built-in XSD atomic type.

        Casting uses the XSD lexical space.  Types that require schema or
        namespace context (for example ``xs:QName``) are rejected rather than
        approximated.
        """
        local = _local_type_name(type_name)
        if local == _local_type_name(self.type_name):
            return self
        if local == "untypedAtomic":
            return XdmAtomic.untyped_atomic(self.lexical)
        datatype = _CAST_DATATYPES.get(local)
        if datatype is None:
            raise TypeError(f"cannot cast to xs:{local}")
        constructor = cast(Callable[[str], Any], datatype)
        try:
            return XdmAtomic.typed(constructor(self.lexical), datatype)
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise TypeError(f"cannot cast {self.lexical!r} to xs:{local}") from exc


# ---------------------------------------------------------------------------
# Maps


_DUPLICATE_POLICIES = frozenset({"combine", "reject", "use-any", "use-first", "use-last"})


@dataclass(frozen=True, slots=True, eq=False, init=False)
class XdmMap(XdmItem):
    """An immutable XDM map with XDM key equality and update semantics."""

    _entries: tuple[tuple[XdmAtomic, XdmSequence], ...]

    def __init__(self, entries: Iterable[tuple[XdmAtomic, Any]] = ()) -> None:
        normalized: list[tuple[XdmAtomic, XdmSequence]] = []
        for key, value in entries:
            if not isinstance(key, XdmAtomic):
                raise TypeError("XDM map keys must be atomic values")
            if any(existing.same_key(key) for existing, _ in normalized):
                raise ValueError("duplicate XDM map key")
            normalized.append((key, as_xdm(value)))
        object.__setattr__(self, "_entries", tuple(normalized))

    @classmethod
    def empty(cls) -> XdmMap:
        return cls(())

    @classmethod
    def merge(cls, *maps: XdmMap, duplicates: str = "use-first") -> XdmMap:
        """Merge maps with the normative ``map:merge`` duplicate policies."""
        if duplicates not in _DUPLICATE_POLICIES:
            raise ValueError(f"unknown duplicate policy: {duplicates!r}")
        entries: list[tuple[XdmAtomic, XdmSequence]] = []
        for mapping in maps:
            if not isinstance(mapping, XdmMap):
                raise TypeError("map:merge arguments must be XDM maps")
            for key, value in mapping.entries:
                index = next(
                    (
                        position
                        for position, (existing, _) in enumerate(entries)
                        if existing.same_key(key)
                    ),
                    None,
                )
                if index is None:
                    entries.append((key, value))
                elif duplicates == "reject":
                    raise ValueError("map:merge cannot combine duplicate keys")
                elif duplicates == "use-last":
                    entries[index] = (key, value)
                elif duplicates == "combine":
                    existing_key, existing_value = entries[index]
                    entries[index] = (existing_key, existing_value + value)
                # use-first and use-any keep the existing entry unchanged.
        return cls(entries)

    @classmethod
    def from_mapping(cls, mapping: Mapping[Any, Any]) -> XdmMap:
        """Explicit host-dictionary conversion; keys must atomize to one atomic."""
        result = cls.empty()
        for key, value in mapping.items():
            keys = as_xdm(key)
            if len(keys) != 1 or not isinstance(keys[0], XdmAtomic):
                raise TypeError("XDM map keys must be single atomic values")
            result = result.put(keys[0], as_xdm(value))
        return result

    @property
    def entries(self) -> tuple[tuple[XdmAtomic, XdmSequence], ...]:
        return self._entries

    @property
    def size(self) -> int:
        return len(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _require_key(key: Any) -> XdmAtomic:
        if not isinstance(key, XdmAtomic):
            raise TypeError("XDM map keys must be atomic values")
        return key

    def get(self, key: XdmAtomic) -> XdmSequence:
        key = self._require_key(key)
        for existing, value in self._entries:
            if existing.same_key(key):
                return value
        return ()

    def contains(self, key: XdmAtomic) -> bool:
        key = self._require_key(key)
        return any(existing.same_key(key) for existing, _ in self._entries)

    def put(self, key: XdmAtomic, value: Any) -> XdmMap:
        key = self._require_key(key)
        sequence = as_xdm(value)
        entries = list(self._entries)
        for index, (existing, _) in enumerate(entries):
            if existing.same_key(key):
                entries[index] = (key, sequence)
                return XdmMap(entries)
        entries.append((key, sequence))
        return XdmMap(entries)

    def remove(self, key: XdmAtomic) -> XdmMap:
        key = self._require_key(key)
        return XdmMap(entry for entry in self._entries if not entry[0].same_key(key))

    def keys(self) -> tuple[XdmAtomic, ...]:
        return tuple(key for key, _ in self._entries)

    def values(self) -> tuple[XdmSequence, ...]:
        return tuple(value for _, value in self._entries)


# ---------------------------------------------------------------------------
# Arrays


@dataclass(frozen=True, slots=True, eq=False, init=False)
class XdmArray(XdmItem):
    """An immutable XDM array whose members are XDM sequences, indexed from one."""

    _members: tuple[XdmSequence, ...]

    def __init__(self, members: Iterable[Any] = ()) -> None:
        if isinstance(members, (str, bytes, bytearray, memoryview)):
            raise TypeError("XDM array members must be a sequence of member values")
        object.__setattr__(self, "_members", tuple(as_xdm(member) for member in members))

    @property
    def members(self) -> tuple[XdmSequence, ...]:
        return self._members

    @property
    def size(self) -> int:
        return len(self._members)

    def __len__(self) -> int:
        return len(self._members)

    def get(self, index: int) -> XdmSequence:
        """Return the one-based member at *index*."""
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("XDM array indices are one-based integers")
        if index < 1 or index > len(self._members):
            raise IndexError(f"FOAY0001: array index {index} is out of range")
        return self._members[index - 1]


# ---------------------------------------------------------------------------
# Function items


_OCCURRENCES = frozenset({"one", "one-or-more", "zero-or-more", "zero-or-one"})


@dataclass(frozen=True, slots=True)
class XdmSequenceType:
    """One XDM sequence type: a type name and an occurrence indicator."""

    type_name: str = "item()"
    occurrence: str = "zero-or-more"

    def __post_init__(self) -> None:
        if not self.type_name:
            raise ValueError("an XDM sequence type requires a type name")
        if self.occurrence not in _OCCURRENCES:
            raise ValueError(f"unknown XDM occurrence indicator: {self.occurrence!r}")


@dataclass(frozen=True, slots=True)
class XdmFunctionSignature:
    """The declared parameter and return sequence types of a function item."""

    parameters: tuple[XdmSequenceType, ...] = ()
    return_type: XdmSequenceType | None = None

    def __post_init__(self) -> None:
        parameters = tuple(self.parameters)
        if any(not isinstance(parameter, XdmSequenceType) for parameter in parameters):
            raise TypeError("function parameters must be XDM sequence types")
        if self.return_type is not None and not isinstance(self.return_type, XdmSequenceType):
            raise TypeError("a function return type must be an XDM sequence type")
        object.__setattr__(self, "parameters", parameters)


def _normalize_arity(arity: int | tuple[int, int | None]) -> tuple[int, int | None]:
    if isinstance(arity, bool):
        raise ValueError("function arity must be a non-negative integer")
    if isinstance(arity, int):
        if arity < 0:
            raise ValueError("function arity must be a non-negative integer")
        return (arity, arity)
    if not isinstance(arity, tuple) or len(arity) != 2:
        raise ValueError("function arity must be an integer or a (min, max) tuple")
    minimum, maximum = arity
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ValueError("function arity minimum must be a non-negative integer")
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < minimum
    ):
        raise ValueError("function arity maximum must be None or at least the minimum")
    return (minimum, maximum)


def _require_items(sequence: XdmSequence, item_type: type[XdmItem], label: str) -> XdmSequence:
    if not all(isinstance(item, item_type) for item in sequence):
        raise TypeError(f"expected {label} items in the function argument")
    return sequence


def _coerce_atomic(item: XdmItem, local: str) -> XdmAtomic:
    if not isinstance(item, XdmAtomic):
        raise TypeError(f"expected xs:{local}, got {type(item).__name__}")
    item_local = _local_type_name(item.type_name)
    if item_local == local:
        return item
    if item_local == "anyURI" and local == "string":
        return XdmAtomic.string(str(item.value))
    item_rank = _NUMERIC_PROMOTIONS.get(item_local)
    target_rank = _NUMERIC_PROMOTIONS.get(local)
    if item_rank is not None and target_rank is not None and item_rank < target_rank:
        return item.cast(local)
    if _is_xsd_subtype(item_local, local):
        return item
    if item_local == "untypedAtomic":
        return item.cast(local)
    raise TypeError(f"cannot coerce xs:{item_local} to xs:{local}")


def _coerce_sequence(sequence: XdmSequence, expected: XdmSequenceType) -> XdmSequence:
    count = len(sequence)
    if expected.occurrence == "one" and count != 1:
        raise TypeError("expected exactly one item for a one-occurrence parameter")
    if expected.occurrence == "zero-or-one" and count > 1:
        raise TypeError("expected at most one item for a zero-or-one parameter")
    if expected.occurrence == "one-or-more" and count == 0:
        raise TypeError("expected at least one item for a one-or-more parameter")
    if not sequence:
        return sequence
    type_name = expected.type_name
    if type_name == "item()":
        return sequence
    if type_name == "map(*)":
        return _require_items(sequence, XdmMap, "map")
    if type_name == "array(*)":
        return _require_items(sequence, XdmArray, "array")
    if type_name == "function(*)":
        return _require_items(sequence, XdmFunction, "function")
    if type_name == "node()":
        raise TypeError("XDM node sequence types are not available in the value layer")
    local = _local_type_name(type_name)
    if local in {"anyAtomicType", "anySimpleType"}:
        return _require_items(sequence, XdmAtomic, "atomic")
    return tuple(_coerce_atomic(item, local) for item in sequence)


@dataclass(frozen=True, slots=True, eq=False, init=False)
class XdmFunction(XdmItem):
    """An immutable XDM function item.

    ``implementation`` is called as ``implementation(arguments, environment,
    closure)`` after arity checking and optional signature coercion.  It must
    return an XDM value or a host container convertible by :func:`as_xdm`.
    The implementation is private to the item and is never exposed as a plain
    Python call surface.
    """

    _implementation: Callable[..., Any] = field(repr=False)
    arity: tuple[int, int | None]
    signature: XdmFunctionSignature | None
    closure: Any
    name: str | None

    def __init__(
        self,
        implementation: Callable[..., Any],
        *,
        arity: int | tuple[int, int | None] | None = None,
        signature: XdmFunctionSignature | None = None,
        closure: Any = None,
        name: str | None = None,
    ) -> None:
        if not callable(implementation):
            raise TypeError("an XDM function implementation must be callable")
        if signature is not None and not isinstance(signature, XdmFunctionSignature):
            raise TypeError("signature must be an XdmFunctionSignature")
        if arity is None:
            if signature is None:
                raise TypeError("an XDM function requires arity or a signature")
            arity = len(signature.parameters)
        normalized = _normalize_arity(arity)
        if signature is not None and normalized != (
            len(signature.parameters),
            len(signature.parameters),
        ):
            raise ValueError("function arity does not match its signature")
        object.__setattr__(self, "_implementation", implementation)
        object.__setattr__(self, "arity", normalized)
        object.__setattr__(self, "signature", signature)
        object.__setattr__(self, "closure", closure)
        object.__setattr__(self, "name", name)

    def invoke(self, arguments: Iterable[Any], environment: Any = None) -> XdmSequence:
        """Invoke with XDM argument sequences and one invocation environment."""
        if isinstance(arguments, (str, bytes, bytearray, memoryview)) or not isinstance(
            arguments, Iterable
        ):
            raise TypeError("function arguments must be an iterable of XDM sequences")
        normalized = tuple(as_xdm(argument) for argument in arguments)
        count = len(normalized)
        minimum, maximum = self.arity
        if count < minimum or (maximum is not None and count > maximum):
            raise TypeError(f"function arity mismatch: expected {minimum}..{maximum}, got {count}")
        if self.signature is None:
            coerced = normalized
        else:
            coerced = tuple(
                _coerce_sequence(argument, parameter)
                for argument, parameter in zip(normalized, self.signature.parameters, strict=True)
            )
        result = as_xdm(self._implementation(coerced, environment, self.closure))
        if self.signature is not None and self.signature.return_type is not None:
            result = _coerce_sequence(result, self.signature.return_type)
        return result


# ---------------------------------------------------------------------------
# The explicit host conversion seam


def _atomic_from_python(value: Any) -> XdmAtomic:
    if isinstance(value, _xsd.TypeList):
        raise TypeError("XSD list values are not atomic XDM items")
    if isinstance(value, _xsd.XsdDataType):
        local = _datatype_local_name(type(value))
        if local in _LIST_LOCALS:
            raise TypeError("XSD list values are not atomic XDM items")
        return XdmAtomic.typed(value, type(value))
    if _is_elementpath_atomic(value):
        local = _datatype_local_name(type(value))
        return XdmAtomic(value, f"{{{XSD_NS}}}{local}", type(value))
    if isinstance(value, str):
        return XdmAtomic.string(value)
    if isinstance(value, bool):
        return XdmAtomic.boolean(value)
    if isinstance(value, int):
        return XdmAtomic.integer(value)
    if isinstance(value, Decimal):
        return XdmAtomic.decimal(value)
    if isinstance(value, float):
        return XdmAtomic.double(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return XdmAtomic.typed(_xsd.Base64Binary(encoded), _xsd.Base64Binary)
    raise TypeError(f"{type(value).__name__} is not an XDM item")


def as_xdm(value: Any) -> XdmSequence:
    """Convert one host value to an XDM sequence at the explicit seam.

    An :class:`XdmItem` becomes a one-item sequence.  A host list, tuple, or
    other iterable is a sequence (nested host sequences flatten, since XDM
    sequences never nest).  A mapping is one :class:`XdmMap` item.  Strings and
    byte strings are single atomic items and are never iterated as characters
    or integers.  ``None`` is rejected because the empty sequence is ``()``.
    """
    if value is None:
        raise TypeError("None is not an XDM value; use () for the empty sequence")
    if isinstance(value, XdmItem):
        return (value,)
    if isinstance(value, _xsd.XsdDataType) or _is_elementpath_atomic(value):
        return (_atomic_from_python(value),)
    if isinstance(value, Mapping):
        return (XdmMap.from_mapping(value),)
    if isinstance(value, (set, frozenset)):
        raise TypeError("an unordered host set cannot be an ordered XDM sequence")
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        return (_atomic_from_python(value),)
    if isinstance(value, Iterable):
        items: list[XdmItem] = []
        for item in value:
            items.extend(as_xdm(item))
        return tuple(items)
    return (_atomic_from_python(value),)
