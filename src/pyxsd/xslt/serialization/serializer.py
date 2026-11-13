"""Serialization on XDM sequences, plus encoding and destination writing.

Producing the string never touches the filesystem or network.  Encoding and
the byte-order mark are applied only by :meth:`Serializer.write`, which writes
to a binary stream or an explicitly supplied path.

The output method is chosen here and nowhere else: ``_METHODS`` is the one
method-support source, so parameter validation and delivery agree on exactly
which methods exist.
"""

from __future__ import annotations

import codecs
from collections.abc import Iterable, Mapping
from os import PathLike
from typing import Any

from pyxsd.xdm import XdmItem
from pyxsd.xslt.errors import XsltSerializationError
from pyxsd.xslt.serialization.json import serialize_adaptive, serialize_json
from pyxsd.xslt.serialization.parameters import SerializationParameters, method_local_name
from pyxsd.xslt.serialization.text import serialize_text
from pyxsd.xslt.serialization.xml import normalize_sequence, serialize_markup

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
    ) -> str:
        """Serialize *sequence* and return the unencoded string."""
        resolved = cls._parameters(parameters)
        items = tuple(sequence)
        if resolved.method == "json":
            return serialize_json(items, resolved)
        if resolved.method == "adaptive":
            return serialize_adaptive(items, resolved)
        if resolved.method == "text":
            return serialize_text(items, resolved)
        return serialize_markup(normalize_sequence(items), resolved, method=resolved.method)

    @classmethod
    def write(
        cls,
        sequence: Iterable[XdmItem],
        parameters: SerializationParameters | Mapping[str, Any] | None,
        destination: Any,
    ) -> None:
        """Encode and write one serialized sequence to a binary destination."""
        resolved = cls._parameters(parameters)
        data = cls.encode(cls.serialize(sequence, resolved), resolved)
        write = getattr(destination, "write", None)
        if callable(write):
            write(data)
            return
        if isinstance(destination, (str, bytes, PathLike)):
            with open(destination, "wb") as stream:
                stream.write(data)
            return
        raise TypeError("destination must provide write() or be a filesystem path")

    @classmethod
    def encode(
        cls,
        value: str,
        parameters: SerializationParameters | Mapping[str, Any] | None = None,
    ) -> bytes:
        """Encode one serialized string with its effective output parameters."""
        return cls._encode(value, cls._parameters(parameters))

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
