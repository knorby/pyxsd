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
from collections.abc import Callable
from decimal import Decimal

from pyxsd.schemaless.json_values import NativeJsonValue
from pyxsd.xdm.values import XdmArray, XdmAtomic, XdmMap, XdmSequence, as_xdm

#: One native JSON scalar, as passed to the internal converter hook.
_ScalarValue = bool | int | float | Decimal | str

#: The internal scalar hook's result contract: an XDM value (or host value
#: convertible by :func:`~pyxsd.xdm.values.as_xdm`), or ``None`` for the
#: document's native mapping. The hook is not a public typing surface.
_ScalarConverter = Callable[[str, _ScalarValue], object]


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
    host iteration; JSON objects have no semantic order.
    """
    return _to_xdm(value, "", converter)


def _to_xdm(
    value: NativeJsonValue, pointer: str, converter: _ScalarConverter | None
) -> XdmSequence:
    """Convert one value at *pointer* into its member or item sequence."""
    if value is None:
        return ()
    if isinstance(value, dict):
        entries: list[tuple[XdmAtomic, XdmSequence]] = []
        for key, member in value.items():
            if not isinstance(key, str):
                raise TypeError(f"a JSON object key must be a string, not {type(key).__name__}")
            entries.append(
                (XdmAtomic.string(key), _to_xdm(member, f"{pointer}/{_escape(key)}", converter))
            )
        return (XdmMap(entries),)
    if isinstance(value, list):
        return (
            XdmArray(
                _to_xdm(member, f"{pointer}/{index}", converter)
                for index, member in enumerate(value)
            ),
        )
    if converter is not None:
        converted = converter(pointer, value)
        if converted is not None:
            return as_xdm(converted)
    return (_atomic(value),)


def _escape(segment: str) -> str:
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


__all__ = ["json_to_xdm"]
