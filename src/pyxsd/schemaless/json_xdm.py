"""Native JSON values as XDM 3.1 maps, arrays, atomics, and sequences.

JSON objects become :class:`~pyxsd.xdm.values.XdmMap` items with ``xs:string``
keys, JSON arrays become :class:`~pyxsd.xdm.values.XdmArray` items whose members
are XDM sequences, and JSON null becomes the empty sequence. Conversion is
native rather than an XML projection: map and array items are not XML nodes.

Numbers keep their exact native identity. A parsed integral number is
``xs:integer``, a parsed non-integral :class:`~decimal.Decimal` is
``xs:decimal`` with no float round trip, and a finite host ``float`` is
``xs:double``. Booleans stay ``xs:boolean`` even though Python's ``bool`` is an
``int`` subtype.

A nested JSON array or null is one array member holding an array item or the
empty sequence. Host-list flattening in :func:`~pyxsd.xdm.values.as_xdm` is
never applied to a native list directly; every container is converted first.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import TypeVar

from pyxsd.schemaless.errors import JsonParseError
from pyxsd.schemaless.json_values import NativeJsonValue
from pyxsd.xdm.values import XdmArray, XdmAtomic, XdmMap, XdmSequence, as_xdm
from pyxsd.xsd_data_types import integer_to_decimal

#: One native JSON scalar, as passed to the internal converter hook.
_ScalarValue = bool | int | float | Decimal | str

#: The internal scalar hook's result contract: an XDM value (or host value
#: convertible by :func:`~pyxsd.xdm.values.as_xdm`), or ``None`` for the
#: document's native mapping. The hook is not a public typing surface.
_ScalarConverter = Callable[[str, _ScalarValue], object]
_T = TypeVar("_T")


def _scalar_text(value: _ScalarValue) -> str:
    """Return the lexical text used by explicit pins and profiles."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return integer_to_decimal(value)
    if isinstance(value, Decimal):
        # XSD decimal has no exponent notation.
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    return value


def _schema_scalar_text(value: _ScalarValue) -> str:
    """Keep compact decimal exponents for the schema-specific adapters."""
    return str(value) if isinstance(value, Decimal) else _scalar_text(value)


def json_to_xdm(
    value: NativeJsonValue, *, converter: _ScalarConverter | None = None
) -> XdmSequence:
    """Convert one native JSON value to an XDM sequence.

    Objects and arrays become one map or array item, scalars become one atomic
    item, and null becomes ``()``. ``converter(pointer, value)`` is an internal
    scalar hook: it receives each non-null scalar's RFC 6901 pointer and native
    value, and may return a replacement XDM value or ``None`` to keep the
    native mapping. Null stays the empty sequence regardless of the hook, and
    map keys are always ``xs:string``. Map entry order follows deterministic
    host iteration; JSON objects have no semantic order. Cyclic containers
    outside the native model raise :class:`JsonParseError`.
    """

    def scalar(pointer: str, member: _ScalarValue | None) -> XdmSequence:
        if member is None:
            return ()
        native = _atomic(member)
        if converter is not None:
            converted = converter(pointer, member)
            if converted is not None:
                return as_xdm(converted)
        return (native,)

    return _fold_json(
        value,
        "",
        scalar=scalar,
        object_value=lambda members: (
            XdmMap((XdmAtomic.string(key), member) for key, member in members.items()),
        ),
        array_value=lambda members: (XdmArray(members),),
    )


def _fold_json(
    value: NativeJsonValue,
    pointer: str,
    *,
    scalar: Callable[[str, _ScalarValue | None], _T],
    object_value: Callable[[dict[str, _T]], _T],
    array_value: Callable[[list[_T]], _T],
) -> _T:
    """Build a view bottom-up without recursion or full pointers per frame."""
    stack: list[
        tuple[
            Iterator[tuple[str | int, NativeJsonValue]],
            dict[str, _T] | list[_T],
            str | int,
            int,
        ]
    ] = []
    active: set[int] = set()
    segments = [pointer]
    key: str | int = 0  # The root has no parent; this key is never used.

    def store(member: _T, at: str | int, target: dict[str, _T] | list[_T]) -> None:
        if isinstance(target, dict):
            target[str(at)] = member
        else:
            target.append(member)

    while True:
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in active:
                raise JsonParseError("the value contains a cyclic container")
            active.add(identity)
            if isinstance(value, dict):
                stack.append((iter(value.items()), {}, key, identity))
            else:
                stack.append((enumerate(value), [], key, identity))
        else:
            member = scalar("/".join(segments), value)
            if not stack:
                return member
            store(member, key, stack[-1][1])
            segments.pop()

        while True:
            children, target, parent_key, identity = stack[-1]
            child = next(children, None)
            if child is not None:
                key, value = child
                if isinstance(target, dict):
                    if not isinstance(key, str):
                        raise TypeError(
                            f"a JSON object key must be a string, not {type(key).__name__}"
                        )
                    segments.append(escape_pointer_segment(key))
                else:
                    segments.append(str(key))
                break

            member = object_value(target) if isinstance(target, dict) else array_value(target)
            stack.pop()
            active.remove(identity)
            if not stack:
                return member
            store(member, parent_key, stack[-1][1])
            segments.pop()


def escape_pointer_segment(segment: str) -> str:
    """Escape one RFC 6901 pointer segment."""
    return segment.replace("~", "~0").replace("/", "~1")


def _atomic(value: NativeJsonValue) -> XdmAtomic:
    """Map one native scalar to its exact XDM atomic type."""
    if isinstance(value, bool):
        return XdmAtomic.boolean(value)
    if isinstance(value, int):
        return XdmAtomic.integer(value)
    if isinstance(value, Decimal):
        return XdmAtomic.decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("non-finite floats are not valid JSON")
        return XdmAtomic.double(value)
    if isinstance(value, str):
        return XdmAtomic.string(value)
    raise TypeError(f"{type(value).__name__} is not a native JSON scalar")


__all__ = ["escape_pointer_segment", "json_to_xdm"]
