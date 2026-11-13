"""Serialization on XDM sequences, plus encoding and destination writing.

Producing the string never touches the filesystem or network.  Encoding and
the byte-order mark are applied only by :meth:`Serializer.write`, which writes
to a binary stream or an explicitly supplied path.
"""

from __future__ import annotations

import codecs
from collections.abc import Iterable, Mapping
from os import PathLike
from typing import Any

from pyxsd.xdm import XdmAtomic, XdmItem
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xslt.errors import XsltSerializationError
from pyxsd.xslt.serialization.parameters import SerializationParameters, method_local_name
from pyxsd.xslt.serialization.text import serialize_text
from pyxsd.xslt.serialization.xml import NormalizedItem, serialize_markup

__all__ = ["Serializer"]

_ENDIAN_CODECS: dict[str, tuple[str, bytes]] = {
    "utf-16": ("utf-16-le", b"\xff\xfe"),
    "utf-16-le": ("utf-16-le", b"\xff\xfe"),
    "utf-16-be": ("utf-16-be", b"\xfe\xff"),
    "utf-32": ("utf-32-le", b"\xff\xfe\x00\x00"),
    "utf-32-le": ("utf-32-le", b"\xff\xfe\x00\x00"),
    "utf-32-be": ("utf-32-be", b"\x00\x00\xfe\xff"),
}


def _not_well_formed(message: str) -> XsltSerializationError:
    return XsltSerializationError(message, code="SENR0001")


def _normalize_sequence(sequence: Iterable[XdmItem]) -> tuple[NormalizedItem, ...]:
    """Apply XSLT 3.0 section 5.7.1 sequence normalization."""
    output: list[NormalizedItem] = []
    pending: list[str] = []
    previous_atomic = False

    def flush() -> None:
        nonlocal previous_atomic
        if pending:
            text = "".join(pending)
            pending.clear()
            if text:
                output.append(text)
        previous_atomic = False

    def visit(item: XdmItem) -> None:
        nonlocal previous_atomic
        if isinstance(item, XdmNode):
            if item.kind is NodeKind.DOCUMENT:
                for child in item.children:
                    visit(child)
            elif item.kind is NodeKind.TEXT:
                if item.string_value:
                    pending.append(item.string_value)
                    previous_atomic = False
            elif item.kind is NodeKind.NAMESPACE:
                raise _not_well_formed("a namespace node cannot appear in a serialized sequence")
            elif item.kind is NodeKind.ATTRIBUTE:
                raise _not_well_formed("an attribute node cannot appear in a serialized sequence")
            else:
                flush()
                output.append(item)
        elif isinstance(item, XdmAtomic):
            if previous_atomic:
                pending.append(" ")
            pending.append(item.lexical)
            previous_atomic = True
        else:
            raise _not_well_formed(
                f"cannot serialize a {type(item).__name__} item in an XML sequence"
            )

    for item in sequence:
        visit(item)
    flush()
    return tuple(output)


class Serializer:
    """Produce serialized strings and write their encoded bytes."""

    _METHODS = frozenset({"xml", "xhtml", "html", "text"})

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
    ) -> str:
        """Serialize *sequence* and return the unencoded string."""
        resolved = cls._parameters(parameters)
        items = tuple(sequence)
        if resolved.method == "text":
            return serialize_text(items, resolved)
        return serialize_markup(_normalize_sequence(items), resolved, method=resolved.method)

    @classmethod
    def write(
        cls,
        sequence: Iterable[XdmItem],
        parameters: SerializationParameters | Mapping[str, Any] | None,
        destination: Any,
    ) -> None:
        """Encode and write one serialized sequence to a binary destination."""
        resolved = cls._parameters(parameters)
        data = cls._encode(cls.serialize(sequence, resolved), resolved)
        write = getattr(destination, "write", None)
        if callable(write):
            write(data)
            return
        if isinstance(destination, (str, bytes, PathLike)):
            with open(destination, "wb") as stream:
                stream.write(data)
            return
        raise TypeError("destination must provide write() or be a filesystem path")

    @staticmethod
    def _encode(value: str, parameters: SerializationParameters) -> bytes:
        canonical = codecs.lookup(parameters.encoding).name
        codec = canonical
        byte_order_mark = b""
        if canonical in _ENDIAN_CODECS:
            codec, byte_order_mark = _ENDIAN_CODECS[canonical]
        elif canonical == "utf-8":
            byte_order_mark = b"\xef\xbb\xbf"
        try:
            data = value.encode(codec)
        except UnicodeEncodeError as error:
            raise XsltSerializationError(
                f"the result contains a character not representable in {codec}",
                code="SERE0008",
                cause=error,
            ) from error
        if parameters.byte_order_mark and byte_order_mark:
            return byte_order_mark + data
        return data
