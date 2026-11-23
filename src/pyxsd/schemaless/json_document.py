"""A native JSON document: strict storage, JSON Pointer reads, serialization.

A :class:`JsonDocument` owns one value in the native JSON data model. It is
deliberately not an XML :class:`~pyxsd.document.Document`: objects stay
mappings, arrays stay lists, numbers stay :class:`int` or :class:`Decimal`, and
nothing is projected into XML until an explicit mapping asks for it.

The document owns a private copy of its value. :meth:`~JsonDocument.to_data`
and :meth:`~JsonDocument.value_at` return copies for containers and the
model's immutable scalars otherwise, so a caller cannot change authoritative
storage through a read.
"""

from __future__ import annotations

from typing import IO

from pyxsd.exceptions import ValidationError
from pyxsd.schemaless.errors import JsonParseError
from pyxsd.schemaless.json_values import (
    JsonLimits,
    NativeJsonValue,
    copy_native_data,
    decode_json,
    encode_json,
    resolve_pointer,
)
from pyxsd.schemaless.json_xdm import json_to_xdm
from pyxsd.validation import ValidationReport
from pyxsd.xdm.values import XdmSequence

_JsonSource = str | bytes | bytearray | memoryview | IO[str] | IO[bytes]


def _source_name(source: object) -> str:
    """Name a JSON source without treating JSON text as a path."""
    name = getattr(source, "name", None)
    return name if isinstance(name, str) else "<memory>"


def _source_error(source_name: str, error: JsonParseError) -> JsonParseError:
    """Name the failing source while keeping the decoder's position evidence."""
    return JsonParseError(
        f"{source_name}: {error}",
        line=error.line,
        column=error.column,
        position=error.position,
    )


class JsonDocument:
    """One native JSON value with an initially empty validation report.

    ``source`` is the recorded input name (``"<memory>"`` for anonymous text,
    bytes, streams, and host data), ``report`` is the document's report, and
    ``is_valid`` is false once that report holds an error. Schema evaluation in
    a later layer fills the report; parsing itself either succeeds completely
    or raises.
    """

    _value: NativeJsonValue
    _report: ValidationReport
    source: str

    def __init__(self, value: object, *, source: str = "<memory>") -> None:
        """Build a document from host data copied into native storage."""
        self._value = copy_native_data(value)
        self.source = source
        self._report = ValidationReport()

    @classmethod
    def _from_owned(cls, value: NativeJsonValue, *, source: str) -> JsonDocument:
        """Wrap a value the caller has already copied and validated."""
        document = cls.__new__(cls)
        document._value = value
        document.source = source
        document._report = ValidationReport()
        return document

    @property
    def report(self) -> ValidationReport:
        """The document's report, empty until a later layer evaluates a schema."""
        return self._report

    @property
    def is_valid(self) -> bool:
        """Whether the report holds no error-severity issues."""
        return not self._report.has_errors

    def require_valid(self) -> None:
        """Raise :class:`pyxsd.ValidationError` if the report has errors."""
        if self._report.has_errors:
            raise ValidationError(
                f"the document is not valid: {self._report.error_count} error(s); "
                "inspect ValidationError.report",
                self._report,
            )

    def value_at(self, pointer: str) -> NativeJsonValue:
        """Return the value addressed by an RFC 6901 JSON Pointer.

        The empty pointer addresses the root. Scalars from the native model are
        immutable and returned directly; objects and arrays are deep-copied so
        the caller cannot mutate authoritative storage.
        """
        selected = resolve_pointer(self._value, pointer)
        if isinstance(selected, (dict, list)):
            return copy_native_data(selected)
        return selected

    def to_data(self) -> NativeJsonValue:
        """Return a deep copy of the native value.

        Parsed integral numbers remain :class:`int`, other parsed numbers
        remain :class:`Decimal`, and a float remains a float only when host
        data supplied one. No number passes through float merely to be copied.
        """
        return copy_native_data(self._value)

    def to_json(self, indent: int | str | None = None) -> str:
        """Serialize the native value as JSON text.

        ``indent`` follows :func:`json.dumps`: ``None`` emits compact
        containers and a non-negative integer or string supplies one level of
        indentation. Numbers use their exact native spelling, never a float
        round trip or a quoted string.
        """
        return encode_json(self._value, indent=indent)

    def to_xdm(self) -> XdmSequence:
        """Return this document's native value as an XDM 3.1 sequence.

        Objects become maps with ``xs:string`` keys, arrays become arrays whose
        members keep their nesting, scalars become atomics with their exact
        native type, and null becomes the empty sequence. A root null therefore
        supplies no context item; nothing is wrapped in an XML document.
        """
        return json_to_xdm(self._value)


def from_data(value: object, *, limits: JsonLimits | None = None) -> JsonDocument:
    """Copy host data into a new native JSON document.

    Mappings with string keys, lists, tuples, strings, booleans, integers,
    finite floats and ``Decimal`` values, and ``None`` are accepted. Tuples
    become lists, and the input is copied so later changes to *value* cannot
    affect the document. Values outside the native model raise
    :class:`~pyxsd.schemaless.errors.JsonParseError`; structural budgets raise
    :class:`~pyxsd.schemaless.errors.JsonLimitError`.
    """
    data = copy_native_data(value, limits=limits)
    return JsonDocument._from_owned(data, source="<memory>")


def from_json(source: _JsonSource, *, limits: JsonLimits | None = None) -> JsonDocument:
    """Decode JSON text, bytes, or a stream into a new native JSON document.

    Integral number tokens become :class:`int` and other number tokens become
    :class:`Decimal`; no number passes through float. Malformed text, duplicate
    object keys, non-standard numeric constants, and non-UTF-8 bytes raise
    :class:`~pyxsd.schemaless.errors.JsonParseError` named for the source:
    a named stream keeps its ``name`` and every anonymous source is
    ``"<memory>"``. An exceeded structural budget raises
    :class:`~pyxsd.schemaless.errors.JsonLimitError`.
    """
    source_name = _source_name(source)
    try:
        value = decode_json(source, limits=limits)
    except JsonParseError as error:
        raise _source_error(source_name, error) from error
    return JsonDocument._from_owned(value, source=source_name)


__all__ = [
    "JsonDocument",
    "from_data",
    "from_json",
]
