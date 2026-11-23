"""A native JSON document: strict storage, JSON Pointer reads, serialization.

A :class:`JsonDocument` owns one value in the native JSON data model. It is
deliberately not an XML :class:`~pyxsd.document.Document`: objects stay
mappings, arrays stay lists, numbers stay :class:`int` or :class:`Decimal`, and
nothing is projected into XML until an explicit mapping asks for it.

The document owns a private copy of its value. :meth:`~JsonDocument.to_data`
and :meth:`~JsonDocument.value_at` return copies for containers and the
model's immutable scalars otherwise, so a caller cannot change authoritative
storage through a read.

A document-local :class:`~pyxsd.schemaless.types.TypePolicy` interprets scalar
leaves for explicit typed reads and XDM conversion. Native storage, JSON
spelling, and the untyped data export never change because of a policy.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import IO, TYPE_CHECKING, Any, Literal, overload

from pyxsd.exceptions import ValidationError
from pyxsd.locations import DataLocation
from pyxsd.schemaless.errors import JsonParseError, ScalarTypeError
from pyxsd.schemaless.json_values import (
    JsonLimits,
    NativeJsonValue,
    copy_native_data,
    decode_json,
    encode_json,
    resolve_pointer,
)
from pyxsd.schemaless.json_xdm import escape_pointer_segment, json_to_xdm
from pyxsd.schemaless.types import _PARSE_ERRORS, ScalarAdapter, TypePolicy, TypeRef
from pyxsd.validation import ValidationReport
from pyxsd.xdm.values import XdmAtomic, XdmSequence
from pyxsd.xsd_data_types import qname_context

if TYPE_CHECKING:
    from pyxsd.schemaless.profile import DocumentProfile, ProfileLimits

_JsonSource = str | bytes | bytearray | memoryview | IO[str] | IO[bytes]
_ScalarValue = bool | int | float | Decimal | str
_POINTER_BAD_ESCAPE = re.compile(r"~(?:[^01]|$)")


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


def _pointer_syntax(pointer: str) -> str:
    """Return why *pointer* is not RFC 6901 syntax, or the empty string."""
    if pointer and not pointer.startswith("/"):
        return "must be empty or start with '/'"
    if any(_POINTER_BAD_ESCAPE.search(raw) for raw in pointer.split("/")[1:]):
        return "contains an invalid escape"
    return ""


def _scalar_text(value: _ScalarValue) -> str:
    """Return the lossless text of a native scalar for adapter parsing."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        # JSON's exponent spelling is not an XSD decimal lexical form, and
        # "f" keeps the exact value without a float round trip.
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    return value


def _typed_scalar(source: str, pointer: str, adapter: ScalarAdapter, value: _ScalarValue) -> Any:
    """Parse one native scalar with *adapter*, naming its pointer on failure."""
    try:
        # JSON has no in-scope namespace bindings for a QName lexical value.
        with qname_context({}):
            return adapter.parse(_scalar_text(value))
    except _PARSE_ERRORS as exc:
        location = DataLocation(source, pointer)
        raise ScalarTypeError(
            f"the JSON value at pointer {pointer!r} is not valid for the selected "
            f"scalar type: {exc}",
            location=location,
        ) from exc


class JsonDocument:
    """One native JSON value with an initially empty validation report.

    ``source`` is the recorded input name (``"<memory>"`` for anonymous text,
    bytes, streams, and host data), ``report`` is the document's report, and
    ``is_valid`` is false once that report holds an error. Schema evaluation in
    a later layer fills the report; parsing itself either succeeds completely
    or raises. ``types`` is the immutable scalar policy; the default is
    :meth:`~pyxsd.schemaless.types.TypePolicy.native`, and :meth:`pin_type`
    replaces it for this document only.
    """

    _value: NativeJsonValue
    _types: TypePolicy
    _report: ValidationReport
    source: str

    def __init__(
        self,
        value: object,
        *,
        source: str = "<memory>",
        types: TypePolicy | None = None,
    ) -> None:
        """Build a document from host data copied into native storage."""
        self._value = copy_native_data(value)
        self.source = source
        self._types = TypePolicy.native() if types is None else types
        self._report = ValidationReport()

    @classmethod
    def _from_owned(
        cls, value: NativeJsonValue, *, source: str, types: TypePolicy | None
    ) -> JsonDocument:
        """Wrap a value the caller has already copied and validated."""
        document = cls.__new__(cls)
        document._value = value
        document.source = source
        document._types = TypePolicy.native() if types is None else types
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

    @property
    def types(self) -> TypePolicy:
        """The document's immutable scalar policy."""
        return self._types

    def require_valid(self) -> None:
        """Raise :class:`pyxsd.ValidationError` if the report has errors."""
        if self._report.has_errors:
            raise ValidationError(
                f"the document is not valid: {self._report.error_count} error(s); "
                "inspect ValidationError.report",
                self._report,
            )

    @overload
    def value_at(self, pointer: str, typed: Literal[False] = False) -> NativeJsonValue: ...

    @overload
    def value_at(self, pointer: str, typed: bool) -> Any: ...

    def value_at(self, pointer: str, typed: bool = False) -> Any:
        """Return the value addressed by an RFC 6901 JSON Pointer.

        The empty pointer addresses the root. Scalars from the native model are
        immutable and returned directly; objects and arrays are deep-copied so
        the caller cannot mutate authoritative storage.

        ``typed=True`` returns an independent view whose scalar leaves follow
        the document's policy. Objects stay mappings, arrays stay lists, null
        stays ``None``, and map keys are never interpreted. A scalar that the
        policy cannot parse raises :class:`ScalarTypeError` with the failing
        pointer; the native value itself remains unchanged.
        """
        selected = resolve_pointer(self._value, pointer)
        if typed:
            return self._typed_tree(selected, pointer)
        if isinstance(selected, (dict, list)):
            return copy_native_data(selected)
        return selected

    def to_data(self) -> NativeJsonValue:
        """Return a deep copy of the native value.

        Parsed integral numbers remain :class:`int`, other parsed numbers
        remain :class:`Decimal`, and a float remains a float only when host
        data supplied one. No number passes through float merely to be copied.
        The type policy affects only :meth:`value_at` and :meth:`to_xdm`.
        """
        return copy_native_data(self._value)

    def to_json(self, indent: int | str | None = None) -> str:
        """Serialize the native value as JSON text.

        ``indent`` follows :func:`json.dumps`: ``None`` emits compact
        containers and a non-negative integer or string supplies one level of
        indentation. Numbers use their exact native spelling, never a float
        round trip or a quoted string. A type policy never changes this text.
        """
        return encode_json(self._value, indent=indent)

    def to_xdm(self) -> XdmSequence:
        """Return this document's native value as an XDM 3.1 sequence.

        Objects become maps with ``xs:string`` keys, arrays become arrays whose
        members keep their nesting, and null becomes the empty sequence. A
        scalar uses the policy's adapter: an explicit pointer pin, a
        conservative inferred string, or the exact native atomic kind. A pin
        that rejects its value raises :class:`ScalarTypeError` with the
        pointer. Nothing is wrapped in an XML document.
        """
        return json_to_xdm(self._value, converter=self._convert_scalar)

    def profile(self, limits: ProfileLimits | None = None) -> DocumentProfile:
        """Return a bounded, immutable profile of the current native value.

        Rows use exact RFC 6901 pointers: one ``"value"`` row for every
        address and one additional ``"array-items"`` row at each array's
        pointer for aggregate member evidence. Scalar candidates are evaluated
        as evidence only: the profile never changes the native value, the
        scalar policy, or any later XDM interpretation. A native document has
        no revision counter, so the snapshot records ``revision_id=None`` and
        ``revision_dirty=False``.
        """
        from pyxsd.schemaless.profile import _json_profile

        return _json_profile(self, limits)

    def test_type(self, pointer: str, type_ref: TypeRef) -> ValidationReport:
        """Report whether the JSON scalar at *pointer* accepts a proposed type.

        Testing never installs a pin. A malformed or absent pointer, a
        non-scalar value, and an unknown type each report one error and leave
        the policy unchanged. A scalar that rejects the type also leaves the
        policy unchanged.
        """
        report, _ = self._check_type(pointer, type_ref)
        return report

    def pin_type(self, pointer: str, type_ref: TypeRef) -> ValidationReport:
        """Install a pointer pin even when the current value rejects the type.

        A malformed or absent pointer, a non-scalar value, and an unknown type
        report an error without changing the policy. A scalar that rejects the
        type still receives the pin, so later typed reads and XDM conversion
        raise :class:`ScalarTypeError`. Installing a pin changes neither native
        storage nor the JSON spelling.
        """
        report, adapter = self._check_type(pointer, type_ref)
        if adapter is not None:
            self._types = self._types.with_pin(pointer, adapter)
        return report

    def _check_type(
        self, pointer: str, type_ref: TypeRef
    ) -> tuple[ValidationReport, ScalarAdapter | None]:
        """Report the proposed type and return its adapter for a valid scalar."""
        report = ValidationReport(diagnostics_complete=True)
        try:
            value = resolve_pointer(self._value, pointer)
        except TypeError as exc:
            report.add_error(str(exc), code="invalid-json-pointer")
            return report, None
        except ValueError as exc:
            reason = _pointer_syntax(pointer)
            code = "invalid-json-pointer" if reason else "unknown-json-pointer"
            report.add_error(
                f"invalid JSON Pointer {pointer!r}: {reason}" if reason else str(exc),
                code=code,
            )
            return report, None
        if isinstance(value, (dict, list)):
            report.add_error(
                f"the JSON Pointer {pointer!r} addresses a container, not a scalar",
                code="non-scalar-json-value",
                location=DataLocation(self.source, pointer),
                location_precision="exact",
            )
            return report, None
        if value is None:
            report.add_error(
                f"the JSON Pointer {pointer!r} addresses null, which has no scalar value",
                code="non-scalar-json-value",
                location=DataLocation(self.source, pointer),
                location_precision="exact",
            )
            return report, None
        try:
            adapter = TypePolicy.resolve(type_ref)
        except ValueError as exc:
            report.add_error(str(exc), code="unknown-scalar-type")
            return report, None
        try:
            _typed_scalar(self.source, pointer, adapter, value)
        except ScalarTypeError as exc:
            report.add_error(
                str(exc),
                code="scalar-type-invalid",
                location=exc.location,
                location_precision="exact",
            )
        return report, adapter

    def _asserted_adapter(self, pointer: str, value: object) -> ScalarAdapter | None:
        """Return sound schema-asserted guidance for this scalar, if any.

        An evaluated JSON Schema is a later layer. Until it supplies an
        occurrence-proven adapter, the document asserts no type and the policy
        falls through to inference and the native kind.
        """
        return None

    def _adapter_at(self, pointer: str, value: _ScalarValue) -> ScalarAdapter | None:
        """Resolve the effective adapter for one native JSON scalar."""
        return self._resolution_at(pointer, value)[0]

    def _interpretation_at(self, pointer: str, value: _ScalarValue) -> str:
        """Name the policy source that interprets one native JSON scalar."""
        return self._resolution_at(pointer, value)[1]

    def _resolution_at(self, pointer: str, value: _ScalarValue) -> tuple[ScalarAdapter | None, str]:
        """Resolve one native scalar and its current interpretation source."""
        asserted = self._asserted_adapter(pointer, value)
        return self._types._json_adapter_source(pointer, value, asserted=asserted)

    def _typed_tree(self, value: NativeJsonValue, pointer: str) -> Any:
        """Build an independent typed view of *value* at *pointer*."""
        if isinstance(value, dict):
            return {
                key: self._typed_tree(member, f"{pointer}/{escape_pointer_segment(key)}")
                for key, member in value.items()
            }
        if isinstance(value, list):
            return [
                self._typed_tree(member, f"{pointer}/{index}") for index, member in enumerate(value)
            ]
        if value is None:
            return None
        adapter = self._adapter_at(pointer, value)
        if adapter is None:
            return value
        return _typed_scalar(self.source, pointer, adapter, value)

    def _convert_scalar(self, pointer: str, value: _ScalarValue) -> XdmAtomic | None:
        """Convert one native scalar through the effective policy adapter."""
        adapter = self._adapter_at(pointer, value)
        if adapter is None:
            return None
        return adapter.to_xdm(_typed_scalar(self.source, pointer, adapter, value))


def from_data(
    value: object,
    *,
    limits: JsonLimits | None = None,
    types: TypePolicy | None = None,
) -> JsonDocument:
    """Copy host data into a new native JSON document.

    Mappings with string keys, lists, tuples, strings, booleans, integers,
    finite floats and ``Decimal`` values, and ``None`` are accepted. Tuples
    become lists, and the input is copied so later changes to *value* cannot
    affect the document. Values outside the native model raise
    :class:`~pyxsd.schemaless.errors.JsonParseError`; structural budgets raise
    :class:`~pyxsd.schemaless.errors.JsonLimitError`.

    *types* is the document's scalar policy for typed reads and XDM
    conversion; the default is
    :meth:`~pyxsd.schemaless.types.TypePolicy.native`, which preserves every
    native scalar kind.
    """
    data = copy_native_data(value, limits=limits)
    return JsonDocument._from_owned(data, source="<memory>", types=types)


def from_json(
    source: _JsonSource,
    *,
    limits: JsonLimits | None = None,
    types: TypePolicy | None = None,
) -> JsonDocument:
    """Decode JSON text, bytes, or a stream into a new native JSON document.

    Integral number tokens become :class:`int` and other number tokens become
    :class:`Decimal`; no number passes through float. Malformed text, duplicate
    object keys, non-standard numeric constants, and non-UTF-8 bytes raise
    :class:`~pyxsd.schemaless.errors.JsonParseError` named for the source:
    a named stream keeps its ``name`` and every anonymous source is
    ``"<memory>"``. An exceeded structural budget raises
    :class:`~pyxsd.schemaless.errors.JsonLimitError`.

    *types* is the document's scalar policy for typed reads and XDM
    conversion; the default is
    :meth:`~pyxsd.schemaless.types.TypePolicy.native`, which preserves every
    native scalar kind. Parsed JSON text never changes because of it.
    """
    source_name = _source_name(source)
    try:
        value = decode_json(source, limits=limits)
    except JsonParseError as error:
        raise _source_error(source_name, error) from error
    return JsonDocument._from_owned(value, source=source_name, types=types)


__all__ = [
    "JsonDocument",
    "from_data",
    "from_json",
]
