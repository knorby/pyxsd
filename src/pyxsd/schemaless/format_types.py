"""Resolve applied JSON Schema annotations for typed views and profiles."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from math import copysign, isinf
from typing import Any
from uuid import UUID

from pyxsd.schemaless.formats import (
    is_base64,
    is_double,
    is_float,
    is_int32,
    is_int64,
    is_ipv4,
    is_ipv6,
    is_uuid,
)
from pyxsd.schemaless.types import _BUILTINS, ScalarAdapter


@dataclass(frozen=True, slots=True)
class _Format:
    candidate: str | None
    adapter: ScalarAdapter | None = None


def _checked(
    adapter: ScalarAdapter,
    check: Callable[[object], bool],
    read: Callable[[str], object] = str,
) -> ScalarAdapter:
    def parse(lexical: str) -> Any:
        if not check(read(lexical)):
            raise ValueError(f"invalid JSON format value: {lexical!r}")
        return adapter.parse(lexical)

    return replace(adapter, parse=parse)


def _integer_adapter(name: str) -> ScalarAdapter:
    adapter = _BUILTINS[name]
    return replace(adapter, parse=lambda text: adapter.parse(str(int(Decimal(text)))))


def _binary32_adapter() -> ScalarAdapter:
    adapter = _BUILTINS["float"]

    def parse(lexical: str) -> Any:
        value = adapter.parse(lexical)
        if isinf(value):
            # The exact number passed the finite check. Rounding to binary64
            # first can land on the binary32 overflow midpoint.
            return adapter.parse(str(copysign(float.fromhex("0x1.fffffep127"), value)))
        return value

    return replace(adapter, parse=parse)


_BASE64 = _Format("xs:base64Binary", _checked(_BUILTINS["base64Binary"], is_base64))
_TEXT_FORMATS = {
    "uuid": _Format(None, _checked(ScalarAdapter(UUID, str), is_uuid)),
    "ipv4": _Format(None, _checked(ScalarAdapter(IPv4Address, str), is_ipv4)),
    "ipv6": _Format(None, _checked(ScalarAdapter(IPv6Address, str), is_ipv6)),
    "int64": _Format("xs:long", _checked(_BUILTINS["long"], is_int64)),
    "byte": _BASE64,
    "date": _Format("xs:date"),
    "date-time": _Format("xs:dateTime"),
    "time": _Format("xs:time"),
    "uri": _Format("xs:anyURI"),
}
_NUMBER_FORMATS = {
    "int32": _Format("xs:int", _checked(_integer_adapter("int"), is_int32, Decimal)),
    "int64": _Format("xs:long", _checked(_integer_adapter("long"), is_int64, Decimal)),
    "float": _Format("xs:float", _checked(_binary32_adapter(), is_float, Decimal)),
    "double": _Format("xs:double", _checked(_BUILTINS["double"], is_double, Decimal)),
}


def _format_hints(schema: Mapping[str, Any], value: object) -> Iterable[_Format]:
    name = schema.get("format", "")
    if isinstance(value, str):
        hint = _TEXT_FORMATS.get(name)
        if hint is not None:
            yield hint
        if schema.get("contentEncoding") == "base64":
            yield _BASE64
    elif not isinstance(value, bool) and isinstance(value, (int, Decimal, float)):
        hint = _NUMBER_FORMATS.get(name)
        if hint is not None:
            yield hint


def format_annotation_types(schema: Mapping[str, Any], value: object) -> Iterable[str]:
    """Suggest XSD lexical tests for annotations applicable to the native kind."""
    for hint in _format_hints(schema, value):
        if hint.candidate is not None:
            yield hint.candidate


def schema_format_adapter(
    schemas: Iterable[Mapping[str, Any]], value: object, lexical: Callable[[], str]
) -> ScalarAdapter | None:
    """Return the unique specific adapter that accepts this occurrence, if any."""
    adapters = dict.fromkeys(
        hint.adapter
        for schema in schemas
        for hint in _format_hints(schema, value)
        if hint.adapter is not None
    )
    if not adapters:
        return None
    text = lexical()
    # The shortest round-trip spelling of a host double need not represent
    # its exact integer value. Use the native number for the format check and
    # conversion, just as assertion mode checks the original host double.
    exact = str(Decimal.from_float(value)) if isinstance(value, float) else text
    accepted = [adapter for adapter in adapters if adapter.test(exact)]
    if len(accepted) != 1:
        return None
    adapter = accepted[0]
    if exact == text:
        return adapter
    return replace(
        adapter, parse=lambda spelling: adapter.parse(exact if spelling == text else spelling)
    )
