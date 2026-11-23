"""Immutable policies for interpreting scalars without rewriting the source."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, cast

from pyxsd import xsd_data_types as xsd
from pyxsd.xdm.values import _BUILTIN_DATATYPES, _CAST_DATATYPES, XSD_NS, XdmAtomic

_PARSE_ERRORS = (ArithmeticError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class ScalarAdapter:
    """Convert lexical strings to Python values and back.

    The ``parse`` callback rejects invalid input with a ``ValueError``,
    ``TypeError``, or ``ArithmeticError``. ``format`` takes a typed value and
    returns its lexical representation. An optional ``xdm`` callback can
    convert the value to an atomic item; otherwise its formatted string is used.
    """

    parse: Callable[[str], Any]
    format: Callable[[Any], str]
    xdm: Callable[[Any], XdmAtomic] | None = None

    def test(self, lexical: str) -> bool:
        """Return whether this adapter accepts a lexical value."""
        try:
            self.parse(lexical)
        except _PARSE_ERRORS:
            return False
        return True

    def to_xdm(self, value: Any) -> XdmAtomic:
        """Convert a typed value to an XDM atomic item."""
        if self.xdm is not None:
            return self.xdm(value)
        return XdmAtomic.string(self.format(value))


TypeRef = str | ScalarAdapter


def _builtin_adapter(datatype: type[xsd.XsdDataType]) -> ScalarAdapter:
    construct = cast(Callable[[Any], xsd.XsdDataType], datatype)

    def parse(lexical: str) -> Any:
        with xsd.whitespace_mode("xsd"):
            value = construct(lexical)
        if isinstance(value, xsd.QName):
            value.to_expanded_name(strict=True)
        # XSD's Boolean is implemented as an int subclass; expose bool here.
        return bool(value) if datatype is xsd.Boolean else value

    def as_datatype(value: Any) -> xsd.XsdDataType:
        return value if type(value) is datatype else construct(value)

    def to_xdm(value: Any) -> XdmAtomic:
        return XdmAtomic.typed(as_datatype(value), datatype)

    return ScalarAdapter(parse, lambda value: as_datatype(value).lexical(), to_xdm)


_BUILTINS = {
    name: _builtin_adapter(datatype)
    for name, datatype in {**_CAST_DATATYPES, "QName": _BUILTIN_DATATYPES["QName"]}.items()
}
_TYPE_NAMES = {
    spelling: adapter
    for name, adapter in _BUILTINS.items()
    for spelling in (name, f"xs:{name}", f"{{{XSD_NS}}}{name}")
}


@dataclass(frozen=True, slots=True)
class TypePolicy:
    """An immutable scalar mode and ordered set of explicit path or pointer pins."""

    mode: Literal["lexical", "native", "infer"]
    pins: Mapping[str, ScalarAdapter] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pins", MappingProxyType(dict(self.pins)))

    @classmethod
    def lexical(cls) -> TypePolicy:
        """Keep XML scalars lexical unless they are pinned."""
        return cls("lexical")

    @classmethod
    def native(cls) -> TypePolicy:
        """Keep native scalar kinds unless they are pinned."""
        return cls("native")

    @classmethod
    def infer(cls) -> TypePolicy:
        """Conservatively interpret lexical scalars unless they are pinned."""
        return cls("infer")

    @staticmethod
    def resolve(type_ref: TypeRef) -> ScalarAdapter:
        """Resolve an XSD scalar name or a caller-supplied adapter."""
        if isinstance(type_ref, ScalarAdapter):
            return type_ref
        try:
            return _TYPE_NAMES[type_ref]
        except KeyError:
            raise ValueError(f"unknown scalar type {type_ref!r}") from None

    def with_pin(self, path: str, type_ref: TypeRef) -> TypePolicy:
        """Return a new policy with the adapter pinned at *path*."""
        pins = dict(self.pins)
        pins.pop(path, None)
        pins[path] = self.resolve(type_ref)
        return replace(self, pins=pins)

    def _with_default_pins(self, pins: Iterable[tuple[str, ScalarAdapter]]) -> TypePolicy:
        """Return this policy with lower-priority pins beneath its own.

        Node-mapping descriptors compile their ``type=`` entries into
        occurrence pins. Those pins behave exactly like an explicit pin
        supplied at parse time, except that a path already pinned by this
        policy keeps its adapter.
        """
        merged = dict(pins)
        merged.update(self.pins)
        return replace(self, pins=merged)

    def json_adapter(
        self,
        pointer: str,
        value: object,
        *,
        asserted: ScalarAdapter | None = None,
    ) -> ScalarAdapter | None:
        """Resolve the adapter for one native JSON scalar at *pointer*.

        Precedence is an explicit pointer pin, *asserted* guidance from an
        evaluated schema, conservative inference of a string leaf, and then the
        native kind (no adapter). ``asserted`` is the document layer's
        extension point for a schema that proves this occurrence's type;
        inference never reinterprets a non-string native value.
        """
        return self._json_adapter_source(pointer, value, asserted=asserted)[0]

    def _json_adapter_source(
        self,
        pointer: str,
        value: object,
        *,
        asserted: ScalarAdapter | None = None,
    ) -> tuple[ScalarAdapter | None, str]:
        """Resolve one native scalar and name the policy source that decided it.

        The source is ``"pinned"``, ``"schema-guided"``, ``"inferred"``, or
        ``"native"``. Profiles use it as interpretation provenance; resolving a
        source never changes the policy or the native storage.
        """
        pinned = self.pins.get(pointer)
        if pinned is not None:
            return pinned, "pinned"
        if asserted is not None:
            return asserted, "schema-guided"
        if self.mode == "infer" and isinstance(value, str):
            inferred = _infer_adapter((value,))
            if inferred is not None:
                return inferred, "inferred"
        return None, "native"


_LEADING_ZERO = re.compile(r"^[ \t\r\n]*[+-]?0[0-9]")
_NUMERIC_CANDIDATES = ("integer", "decimal", "double")
_OTHER_CANDIDATES = ("date", "dateTime", "time")


def _ranked_candidates() -> tuple[tuple[str, ScalarAdapter], ...]:
    """Return the ranked built-in scalar candidates a profile evaluates.

    The names use the ``xs:`` spelling and the order is the conservative
    inference order: boolean, the numeric ladder, then the calendar types.
    Profiles report evidence only, so this is the single owner of the candidate
    ranking and ``profile`` never keeps a parallel name list.
    """
    return tuple(
        (f"xs:{name}", _BUILTINS[name])
        for name in ("boolean", *_NUMERIC_CANDIDATES, *_OTHER_CANDIDATES)
    )


def _finite_double(lexical: str) -> bool:
    try:
        with xsd.whitespace_mode("xsd"):
            return math.isfinite(xsd.Double(lexical))
    except _PARSE_ERRORS:
        return False


def _infer_adapter(values: Sequence[str]) -> ScalarAdapter | None:
    """Select the first non-string candidate accepted by a nonempty group."""
    if all(value in ("true", "false") for value in values):
        return _BUILTINS["boolean"]
    if not any(_LEADING_ZERO.match(value) for value in values):
        for name in _NUMERIC_CANDIDATES:
            adapter = _BUILTINS[name]
            test = _finite_double if name == "double" else adapter.test
            if all(test(value) for value in values):
                return adapter
    for name in _OTHER_CANDIDATES:
        adapter = _BUILTINS[name]
        if all(adapter.test(value) for value in values):
            return adapter
    return None
