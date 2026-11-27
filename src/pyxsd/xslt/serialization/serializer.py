"""Serialization on XDM sequences, plus encoding and destination writing.

Producing the string never touches the filesystem or network. Encoding and
the byte-order mark are applied during byte-budgeted delivery or writing to
a binary stream or an explicitly supplied path.

The output method is chosen here and nowhere else: ``_METHODS`` is the one
method-support source, so parameter validation and delivery agree on exactly
which methods exist.
"""

from __future__ import annotations

import codecs
import sys
from collections.abc import Iterable, Iterator, Mapping
from os import PathLike
from typing import TYPE_CHECKING, Any

from pyxsd.xdm import XdmItem
from pyxsd.xslt.errors import IMPLEMENTATION_BUDGET_CODE, XsltSerializationError
from pyxsd.xslt.serialization.json import serialize_adaptive, serialize_json
from pyxsd.xslt.serialization.parameters import SerializationParameters, method_local_name
from pyxsd.xslt.serialization.text import serialize_text
from pyxsd.xslt.serialization.xml import normalize_sequence, serialize_markup

if TYPE_CHECKING:
    from pyxsd.xslt.runtime.frames import ExecutionBudget

__all__ = ["Serializer"]

_ENDIAN_CODECS: dict[str, tuple[str, bytes]] = {
    "utf-16": ("utf-16-le", b"\xff\xfe"),
    "utf-16-le": ("utf-16-le", b"\xff\xfe"),
    "utf-16-be": ("utf-16-be", b"\xfe\xff"),
    "utf-32": ("utf-32-le", b"\xff\xfe\x00\x00"),
    "utf-32-le": ("utf-32-le", b"\xff\xfe\x00\x00"),
    "utf-32-be": ("utf-32-be", b"\x00\x00\xfe\xff"),
}


class Serializer:
    """Produce serialized strings and write their encoded bytes."""

    _METHODS = frozenset({"xml", "xhtml", "html", "text", "json", "adaptive"})

    @classmethod
    def supports_method(cls, method: str) -> bool:
        """Whether this serializer implements *method*, lexical or EQName."""
        return method_local_name(method) in cls._METHODS

    @staticmethod
    def _parameters(
        parameters: SerializationParameters | Mapping[str, Any] | None,
    ) -> SerializationParameters:
        if parameters is None:
            return SerializationParameters()
        if isinstance(parameters, SerializationParameters):
            return parameters
        if isinstance(parameters, Mapping):
            return SerializationParameters.from_attributes(parameters)
        raise TypeError("parameters must be SerializationParameters, a mapping, or None")

    @classmethod
    def serialize(
        cls,
        sequence: Iterable[XdmItem],
        parameters: SerializationParameters | Mapping[str, Any] | None = None,
        *,
        result_tree: bool = False,
    ) -> str:
        """Serialize *sequence* and return the unencoded string.

        ``result_tree`` marks a sequence normalized from a delivered document
        node; the XML family then allows the several element children a final
        result tree may contain.

        The serializer converts ``RecursionError`` caught while rendering
        into :class:`~pyxsd.xslt.errors.XsltSerializationError` with code
        ``PYXSD-XSLT-001``. Close to the recursion limit, CPython can fail
        inside its own machinery before a guard runs, and the uncoded
        built-in exception then escapes. That condition was observed during
        transformation on CPython 3.13 under a deeply nested host stack.
        """
        resolved = cls._parameters(parameters)
        items = tuple(sequence)
        try:
            if resolved.method == "json":
                return serialize_json(items, resolved)
            if resolved.method == "adaptive":
                return serialize_adaptive(items, resolved)
            if resolved.method == "text":
                return serialize_text(items, resolved)
            return serialize_markup(
                normalize_sequence(items, resolved.item_separator),
                resolved,
                method=resolved.method,
                result_tree=result_tree,
            )
        except RecursionError as error:
            raise XsltSerializationError(
                "serialization exceeded the Python recursion limit: "
                f"Python recursion limit={sys.getrecursionlimit()}",
                code=IMPLEMENTATION_BUDGET_CODE,
                cause=error,
            ) from error

    @classmethod
    def write(
        cls,
        sequence: Iterable[XdmItem],
        parameters: SerializationParameters | Mapping[str, Any] | None,
        destination: Any,
        *,
        _budget: ExecutionBudget | None = None,
    ) -> None:
        """Encode and write one serialized sequence to a binary destination."""
        resolved = cls._parameters(parameters)
        value = cls.serialize(sequence, resolved)
        chunks: Iterable[bytes]
        if _budget is None or _budget.limits.max_output_bytes is None:
            chunks = (cls.encode(value, resolved),)
        else:
            chunks = cls._budgeted_encoding(value, resolved, _budget)
        write = getattr(destination, "write", None)
        if callable(write):
            for chunk in chunks:
                write(chunk)
            return
        if isinstance(destination, (str, bytes, PathLike)):
            with open(destination, "wb") as stream:
                for chunk in chunks:
                    stream.write(chunk)
            return
        raise TypeError("destination must provide write() or be a filesystem path")

    @classmethod
    def _budgeted_encoding(
        cls, value: str, parameters: SerializationParameters, budget: ExecutionBudget
    ) -> Iterator[bytes]:
        from pyxsd.xslt.runtime.frames import resource_error

        for chunk in cls._encode_chunks(value, parameters):
            allowed, attempted = budget._take_output_chunk(len(chunk))
            if allowed:
                yield chunk[:allowed]
            if allowed != len(chunk):
                raise resource_error(
                    budget.exhaustion("output byte", "max_output_bytes", attempted)
                )

    @classmethod
    def _encode_chunks(cls, value: str, parameters: SerializationParameters) -> Iterator[bytes]:
        codec, byte_order_mark = cls._encoding(parameters)
        if byte_order_mark:
            yield byte_order_mark
        encoder = codecs.getincrementalencoder(codec)()
        size = 4096
        try:
            for start in range(0, len(value), size):
                yield encoder.encode(value[start : start + size])
            yield encoder.encode("", final=True)
        except UnicodeEncodeError as error:
            raise cls._unrepresentable(codec, error) from error

    @classmethod
    def encode(
        cls,
        value: str,
        parameters: SerializationParameters | Mapping[str, Any] | None = None,
    ) -> bytes:
        """Encode one serialized string with its effective output parameters."""
        return cls._encode(value, cls._parameters(parameters))

    @staticmethod
    def _encoding(parameters: SerializationParameters) -> tuple[str, bytes]:
        canonical = codecs.lookup(parameters.encoding).name
        codec = canonical
        byte_order_mark = b""
        if canonical in _ENDIAN_CODECS:
            codec, byte_order_mark = _ENDIAN_CODECS[canonical]
        elif canonical == "utf-8":
            byte_order_mark = b"\xef\xbb\xbf"
        return codec, byte_order_mark if parameters.byte_order_mark else b""

    @staticmethod
    def _unrepresentable(codec: str, error: UnicodeEncodeError) -> XsltSerializationError:
        return XsltSerializationError(
            f"the result contains a character not representable in {codec}",
            code="SERE0008",
            cause=error,
        )

    @classmethod
    def _encode(cls, value: str, parameters: SerializationParameters) -> bytes:
        codec, byte_order_mark = cls._encoding(parameters)
        try:
            return byte_order_mark + value.encode(codec)
        except UnicodeEncodeError as error:
            raise cls._unrepresentable(codec, error) from error
