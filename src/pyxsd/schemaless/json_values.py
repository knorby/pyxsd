"""Strict, dependency-free native JSON values for schemaless documents.

Native JSON is the JSON data model itself: objects are mappings with string
keys, arrays are lists, and numbers are Python ``int`` or ``Decimal`` when
they came from JSON text. Parsing never routes a number through ``float``, so
arbitrary precision survives a round trip. Text decoding rejects duplicate
object keys and the non-standard ``NaN``/``Infinity`` constants, and host data
is rejected rather than stringified when it falls outside the model.

These rules are separate from the schema-guided JSON codecs and from XPath's
``parse-json`` mapping, which intentionally produces ``xs:double`` numbers.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import IO, Any, TypeAlias

from pyxsd.schemaless.errors import JsonLimitError, JsonParseError
from pyxsd.xsd_data_types import _integer_from_digits, integer_to_decimal

#: A value in the native JSON data model.
#:
#: Distinct from the schema-guided
#: :data:`pyxsd.data_conversion.contracts.JsonValue`, which excludes
#: ``Decimal`` and includes only JSON-serializable host values.
NativeJsonValue: TypeAlias = (
    bool
    | int
    | float
    | Decimal
    | str
    | list["NativeJsonValue"]
    | dict[str, "NativeJsonValue"]
    | None
)

_STREAM_CHUNK = 65536


@dataclass(frozen=True)
class JsonLimits:
    """Optional budgets for one native JSON input.

    Every structural bound is inclusive and positive: ``max_bytes`` counts the
    input bytes as supplied (UTF-8 bytes for text, including a leading BOM),
    ``max_depth`` counts nesting with the root at depth one, and ``max_values``
    counts every value (containers and scalars) but not object member names.
    ``max_diagnostics`` is a non-negative retention budget for diagnostics that
    later JSON Schema evaluation may attach to the document. ``None`` leaves
    that budget unset.
    """

    max_bytes: int | None = None
    max_depth: int | None = None
    max_values: int | None = None
    max_diagnostics: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_depth", "max_values"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or None")
        diagnostics = self.max_diagnostics
        if diagnostics is not None and (
            isinstance(diagnostics, bool) or not isinstance(diagnostics, int) or diagnostics < 0
        ):
            raise ValueError("max_diagnostics must be a non-negative integer or None")


class _ValueBudget:
    """Count values and depth for one accepted native value."""

    __slots__ = ("_max_depth", "_max_values", "count")

    def __init__(self, limits: JsonLimits) -> None:
        self._max_depth = limits.max_depth
        self._max_values = limits.max_values
        self.count = 0

    def accept(self, depth: int) -> None:
        self.count += 1
        if self._max_values is not None and self.count > self._max_values:
            raise JsonLimitError(f"the JSON input exceeds max_values={self._max_values}")
        if self._max_depth is not None and depth > self._max_depth:
            raise JsonLimitError(f"the JSON input exceeds max_depth={self._max_depth}")


def _reject_constant(name: str) -> NativeJsonValue:
    """Reject JSON's non-standard ``NaN``/``Infinity`` extensions."""
    raise JsonParseError(f"the non-standard numeric constant {name} is not valid JSON")


def _reject_duplicate_keys(pairs: list[tuple[str, NativeJsonValue]]) -> dict[str, NativeJsonValue]:
    """Build an object, refusing a repeated member name."""
    result: dict[str, NativeJsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise JsonParseError(f"the object declares the duplicate key {key!r} more than once")
        result[key] = value
    return result


def _parse_integer(token: str) -> int:
    """Decode an integer token already checked by the JSON scanner."""
    if len(token) <= 9:
        return int(token)
    if token.startswith("-"):
        return -_integer_from_digits(token[1:])
    return _integer_from_digits(token)


def _parse_decimal(token: str) -> Decimal:
    try:
        number = Decimal(token)
    except InvalidOperation as exc:
        raise JsonParseError("a JSON number exceeds the native decimal range") from exc
    if not number.is_finite():
        # With the caller's InvalidOperation trap disabled, Decimal returns NaN.
        raise JsonParseError("a JSON number exceeds the native decimal range")
    return number


_DECODER = json.JSONDecoder(
    parse_float=_parse_decimal,
    parse_int=_parse_integer,
    parse_constant=_reject_constant,
    object_pairs_hook=_reject_duplicate_keys,
)


def decode_json(
    source: str | bytes | bytearray | memoryview | IO[str] | IO[bytes],
    *,
    limits: JsonLimits | None = None,
) -> NativeJsonValue:
    """Decode JSON text, UTF-8 bytes, or a readable stream into native data.

    Integral number tokens become :class:`int` and every other number token
    becomes :class:`Decimal`; no value passes through ``float``. A leading
    UTF-8 BOM is accepted and removed. Duplicate object keys, non-standard
    numeric constants, malformed text, and non-UTF-8 bytes raise
    :class:`~pyxsd.schemaless.errors.JsonParseError`; an exceeded byte, depth,
    or value budget raises :class:`~pyxsd.schemaless.errors.JsonLimitError`.
    Stream bytes are counted under ``max_bytes`` as they are read.
    """
    number_limits = limits or JsonLimits()
    text = _read_source(source, number_limits)
    try:
        value: NativeJsonValue = _DECODER.decode(text)
    except json.JSONDecodeError as exc:
        raise JsonParseError(
            f"{exc.msg}: line {exc.lineno} column {exc.colno} (char {exc.pos})",
            line=exc.lineno,
            column=exc.colno,
            position=exc.pos,
        ) from exc
    except RecursionError as exc:
        raise JsonLimitError("the JSON input exceeds the host parser recursion limit") from exc
    _enforce_value_limits(value, number_limits)
    return value


def _read_source(
    source: str | bytes | bytearray | memoryview | IO[str] | IO[bytes],
    limits: JsonLimits,
) -> str:
    """Materialize *source* as decoded text under the byte budget."""
    if isinstance(source, str):
        _check_byte_budget(len(_utf8_bytes(source)), limits.max_bytes)
        return source.removeprefix("\ufeff")
    if isinstance(source, (bytes, bytearray, memoryview)):
        raw = bytes(source)
        _check_byte_budget(len(raw), limits.max_bytes)
        return _decode_utf8(raw)
    read = getattr(source, "read", None)
    if read is None or not callable(read):
        raise TypeError(
            "JSON source must be str, bytes, bytearray, memoryview, or a readable stream, "
            f"not {type(source).__name__}"
        )
    return _read_stream(read, limits.max_bytes)


def _read_stream(read: Any, max_bytes: int | None) -> str:
    """Read a stream once, rejecting mixed chunk types and enforcing bytes."""
    text_parts: list[str] = []
    byte_parts = bytearray()
    mode: str | None = None
    total = 0
    while True:
        chunk = read(_STREAM_CHUNK)
        if isinstance(chunk, str):
            if chunk == "":
                break
            if mode == "bytes":
                raise JsonParseError("the JSON source stream mixes text and bytes chunks")
            mode = "text"
            total += len(_utf8_bytes(chunk))
            _check_byte_budget(total, max_bytes)
            text_parts.append(chunk)
        elif isinstance(chunk, (bytes, bytearray, memoryview)):
            if len(chunk) == 0:
                break
            if mode == "text":
                raise JsonParseError("the JSON source stream mixes text and bytes chunks")
            mode = "bytes"
            data = bytes(chunk)
            total += len(data)
            _check_byte_budget(total, max_bytes)
            byte_parts.extend(data)
        else:
            raise JsonParseError(
                f"the JSON source stream returned an unsupported chunk type: {type(chunk).__name__}"
            )
    if mode == "text":
        return "".join(text_parts).removeprefix("\ufeff")
    return _decode_utf8(bytes(byte_parts))


def _check_byte_budget(size: int, max_bytes: int | None) -> None:
    if max_bytes is not None and size > max_bytes:
        raise JsonLimitError(f"the JSON input exceeds max_bytes={max_bytes}")


def _utf8_bytes(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise JsonParseError(
            f"the JSON source is not valid UTF-8: {exc}", position=exc.start
        ) from exc


def _decode_utf8(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise JsonParseError(
            f"the JSON source is not valid UTF-8: {exc}", position=exc.start
        ) from exc


def _enforce_value_limits(value: NativeJsonValue, limits: JsonLimits) -> None:
    """Walk parsed values iteratively instead of relying on recursion."""
    if limits.max_depth is None and limits.max_values is None:
        return
    budget = _ValueBudget(limits)
    budget.accept(1)
    stack: list[tuple[NativeJsonValue, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children: Iterable[NativeJsonValue] = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        for child in children:
            budget.accept(depth + 1)
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))


def _unsupported(value: object) -> JsonParseError:
    return JsonParseError(f"unsupported native JSON value: {type(value).__name__}")


def _copy_value(value: Any, depth: int, budget: _ValueBudget) -> NativeJsonValue:
    """Copy one value's scalar or container shell, charging the budget."""
    if isinstance(value, Mapping):
        budget.accept(depth)
        return {}
    if isinstance(value, (list, tuple)):
        budget.accept(depth)
        return []
    if value is None or isinstance(value, bool):
        budget.accept(depth)
        return value
    if isinstance(value, int):
        budget.accept(depth)
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JsonParseError("non-finite floats are not valid JSON")
        budget.accept(depth)
        return float(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise JsonParseError("non-finite decimals are not valid JSON")
        budget.accept(depth)
        return value
    if isinstance(value, str):
        budget.accept(depth)
        return value
    raise _unsupported(value)


def copy_native_data(value: object, *, limits: JsonLimits | None = None) -> NativeJsonValue:
    """Copy host data into the native JSON model under optional budgets.

    Mappings with string keys, lists, tuples, strings, booleans, integers,
    finite floats and ``Decimal`` values, and ``None`` are accepted. Tuples
    become lists and every container is copied, so later changes to *value*
    cannot affect the result. Booleans stay booleans rather than integers;
    non-string object keys, non-finite numbers, cyclic containers, and every
    other host object raise
    :class:`~pyxsd.schemaless.errors.JsonParseError`. Only the depth and value
    budgets apply: host data has no input bytes for ``max_bytes``.

    A shared container reachable by two separate paths is copied once per
    path. Only a container that is its own ancestor is a cycle, so the active
    copy path is tracked by identity and released when its subtree ends.
    """
    number_limits = limits or JsonLimits()
    budget = _ValueBudget(number_limits)
    root = _copy_value(value, 1, budget)
    if not isinstance(root, (dict, list)):
        return root
    active = {id(value)}
    stack: list[
        tuple[
            Any,
            dict[str, NativeJsonValue] | list[NativeJsonValue],
            int,
            Iterator[tuple[Any, Any]],
        ]
    ] = [(value, root, 1, _container_items(value))]
    while stack:
        source, target, depth, items = stack[-1]
        try:
            key, child = next(items)
        except StopIteration:
            stack.pop()
            active.discard(id(source))
            continue
        if isinstance(target, dict):
            if not isinstance(key, str):
                raise JsonParseError(
                    f"a JSON object key must be a string, not {type(key).__name__}"
                )
            copied = _copy_value(child, depth + 1, budget)
            target[key] = copied
        else:
            copied = _copy_value(child, depth + 1, budget)
            target.append(copied)
        if isinstance(copied, (dict, list)):
            if id(child) in active:
                raise JsonParseError("the value contains a cyclic container")
            active.add(id(child))
            stack.append((child, copied, depth + 1, _container_items(child)))
    return root


def _container_items(source: Any) -> Iterator[tuple[Any, Any]]:
    """Yield a container's ``(key, value)`` or ``(index, value)`` items."""
    if isinstance(source, Mapping):
        return iter(source.items())
    return iter(enumerate(source))


def encode_json(value: NativeJsonValue, *, indent: int | str | None = None) -> str:
    """Serialize native data as JSON text without falling back to ``str``.

    Strings and object keys use the standard JSON encoder's ASCII escaping,
    which keeps the result UTF-8 encodable even for an unpaired surrogate.
    Finite floats use Python's shortest round-trip spelling, and finite
    :class:`Decimal` values emit their exact exponent and trailing-zero
    spelling. ``None`` emits compact single-line containers; an integer
    supplies spaces and a string supplies JSON whitespace for one level of
    indentation. Non-whitespace indents, cyclic containers and values outside
    the native model raise :class:`~pyxsd.schemaless.errors.JsonParseError`.
    """
    unit = _indent_unit(indent)
    parts: list[str] = []
    active: set[int | None] = set()
    stack: list[tuple[int | None, Iterator[str | tuple[object]]]] = [(None, iter(((value,),)))]
    while stack:
        identity, tokens = stack[-1]
        token = next(tokens, None)
        if token is None:
            stack.pop()
            active.discard(identity)
        elif isinstance(token, str):
            parts.append(token)
        else:
            (node,) = token
            if isinstance(node, (list, dict)):
                identity = id(node)
                if identity in active:
                    raise JsonParseError("the value contains a cyclic container")
                active.add(identity)
                stack.append((identity, _container_tokens(node, unit, len(stack) - 1)))
            else:
                parts.append(_encode_scalar(node))
    return "".join(parts)


def _indent_unit(indent: int | str | None) -> str | None:
    if isinstance(indent, str) and indent.strip(" \t\r\n"):
        raise JsonParseError("a string indent must contain only JSON whitespace")
    return " " * indent if isinstance(indent, int) else indent


def _encode_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return integer_to_decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JsonParseError("non-finite floats are not valid JSON")
        return json.dumps(value, allow_nan=False)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise JsonParseError("non-finite decimals are not valid JSON")
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise _unsupported(value)


def _container_tokens(
    value: dict[Any, Any] | list[Any], unit: str | None, level: int
) -> Iterator[str | tuple[object]]:
    """Yield literal syntax or a wrapped child for the encoder to visit."""
    if isinstance(value, dict):
        yield "{"
        items: Iterable[tuple[object, object]] = value.items()
        closing = "}"
    else:
        yield "["
        items = enumerate(value)
        closing = "]"
    for index, (key, child) in enumerate(items):
        if index:
            yield ", " if unit is None else ","
        if unit is not None:
            yield "\n" + unit * (level + 1)
        if isinstance(value, dict):
            if not isinstance(key, str):
                raise JsonParseError(
                    f"a JSON object key must be a string, not {type(key).__name__}"
                )
            yield json.dumps(key) + ": "
        yield (child,)
    if value and unit is not None:
        yield "\n" + unit * level
    yield closing


def resolve_pointer(value: NativeJsonValue, pointer: str) -> NativeJsonValue:
    """Resolve an RFC 6901 JSON Pointer against native *value*.

    The empty pointer names the root. ``~1`` and ``~0`` are the only escapes,
    array indices are canonical zero-based decimals without leading zeros, and
    ``-`` is not a readable index. Returns the stored value (containers are not
    copied), or raises ``ValueError`` for an absent or ambiguous address and
    ``TypeError`` for a non-string pointer.
    """
    if not isinstance(pointer, str):
        raise TypeError("a JSON Pointer must be a string")
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError(f"the JSON Pointer {pointer!r} must be empty or start with '/'")
    current: NativeJsonValue = value
    for raw in pointer.split("/")[1:]:
        token = _pointer_token(raw, pointer)
        if isinstance(current, dict):
            if token not in current:
                raise ValueError(f"the JSON Pointer {pointer!r} does not address a present member")
            current = current[token]
        elif isinstance(current, list):
            current = current[_array_index(token, pointer, len(current))]
        else:
            raise ValueError(f"the JSON Pointer {pointer!r} traverses a scalar value")
    return current


def _pointer_token(raw: str, pointer: str) -> str:
    if "~" not in raw:
        return raw
    token: list[str] = []
    index = 0
    while index < len(raw):
        character = raw[index]
        if character != "~":
            token.append(character)
            index += 1
            continue
        if index + 1 >= len(raw) or raw[index + 1] not in "01":
            raise ValueError(f"the JSON Pointer {pointer!r} contains an invalid escape")
        token.append("~" if raw[index + 1] == "0" else "/")
        index += 2
    return "".join(token)


def _array_index(token: str, pointer: str, length: int) -> int:
    if token == "-":
        raise ValueError(f"the JSON Pointer {pointer!r} names the append position '-'")
    if not token.isascii() or not token.isdigit():
        raise ValueError(f"the JSON Pointer {pointer!r} does not contain a canonical array index")
    if len(token) > 1 and token[0] == "0":
        raise ValueError(f"the JSON Pointer {pointer!r} uses a leading zero in an array index")
    index = int(token)
    if index >= length:
        raise ValueError(f"the JSON Pointer {pointer!r} names an out-of-range array index")
    return index


__all__ = [
    "JsonLimits",
    "NativeJsonValue",
    "copy_native_data",
    "decode_json",
    "encode_json",
    "resolve_pointer",
]
