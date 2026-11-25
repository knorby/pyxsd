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
from collections.abc import Mapping
from typing import IO, TYPE_CHECKING, Any, Literal, overload

from pyxsd.exceptions import ValidationError
from pyxsd.locations import DataLocation
from pyxsd.schemaless.errors import JsonParseError, ScalarTypeError
from pyxsd.schemaless.format_types import format_annotation_types, schema_format_adapter
from pyxsd.schemaless.json_values import (
    JsonLimits,
    NativeJsonValue,
    copy_native_data,
    decode_json,
    encode_json,
    resolve_pointer,
)
from pyxsd.schemaless.json_xdm import (
    _fold_json,
    _scalar_text,
    _ScalarValue,
    _schema_scalar_text,
    json_to_xdm,
)
from pyxsd.schemaless.types import (
    _PARSE_ERRORS,
    ScalarAdapter,
    TypePolicy,
    TypeRef,
    _json_asserted_type,
    _json_schema_adapter,
)
from pyxsd.validation import ValidationReport
from pyxsd.xdm.values import XdmAtomic, XdmSequence
from pyxsd.xsd_data_types import qname_context

if TYPE_CHECKING:
    from referencing.jsonschema import SchemaRegistry

    from pyxsd.resource_policy import ResourcePolicy
    from pyxsd.schemaless.document import SchemalessDocument
    from pyxsd.schemaless.json_schema import SchemaOccurrence, _AppliedSchema
    from pyxsd.schemaless.mapping import XmlMapping
    from pyxsd.schemaless.nodes import NodeMapping
    from pyxsd.schemaless.profile import DocumentProfile, ProfileLimits

_JsonSource = str | bytes | bytearray | memoryview | IO[str] | IO[bytes]
_JsonSchemaInput = bool | Mapping[str, object] | str
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


def _typed_scalar(
    source: str,
    pointer: str,
    adapter: ScalarAdapter,
    value: _ScalarValue,
    *,
    asserted: bool = False,
) -> Any:
    """Parse one native scalar with *adapter*, naming its pointer on failure."""
    try:
        # JSON has no in-scope namespace bindings for a QName lexical value.
        with qname_context({}):
            text = _schema_scalar_text(value) if asserted else _scalar_text(value)
            return adapter.parse(text)
    except _PARSE_ERRORS as exc:
        location = DataLocation(source, pointer)
        raise ScalarTypeError(
            f"the JSON value at pointer {pointer!r} is not valid for the selected "
            f"scalar type: {exc}",
            location=location,
        ) from exc


class JsonDocument:
    """One native JSON value with an optional JSON Schema validation report.

    ``source`` is the recorded input name (``"<memory>"`` for anonymous text,
    bytes, streams, and host data), ``report`` is the document's report, and
    ``is_valid`` is false once that report records an error. A schema-invalid
    instance still supports native reads and XDM conversion. ``types`` is the
    immutable scalar policy; the default is
    :meth:`~pyxsd.schemaless.types.TypePolicy.native`, and :meth:`pin_type`
    replaces it for this document only.
    """

    _value: NativeJsonValue
    _types: TypePolicy
    _report: ValidationReport
    _applied_schema: _AppliedSchema | None = None
    _format_typing: bool = False
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
        """The document's report, empty when built without JSON Schema guidance."""
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

        ``None`` emits compact containers. An integer supplies spaces and a
        string supplies JSON whitespace for one level of indentation. A
        non-whitespace indent raises :class:`JsonParseError`. A type policy
        never changes the exact native scalar spellings.
        """
        return encode_json(self._value, indent=indent)

    def to_xml(
        self, mapping: XmlMapping, *, node_mapping: NodeMapping | None = None
    ) -> SchemalessDocument:
        """Project this native value into a fresh XML document.

        *mapping* names the root element and may rename occurrences by RFC 6901
        JSON Pointer. Element-first projection turns object properties and
        array members into elements, strings into escaped character data,
        numbers and booleans into JSON-compatible lexical text, and null into
        ``xsi:nil="true"``. The source value is never changed and edits to the
        derived XML do not write back.

        The result has no source bytes or source index. Its report is current
        and holds non-fatal warnings for information the XML shape cannot
        retain. Invalid names, unusable expanded namespace names, and string
        characters XML 1.0 cannot represent raise
        :class:`~pyxsd.schemaless.errors.XmlMappingError` instead of being
        repaired, escaped around, or dropped. Object member keys are always
        validated as local names; only explicit mapping names may be expanded.
        JSON Pointer pins stay on this document: the projected tree starts from
        a lexical policy.

        A schema-backed mapping requires a valid instance for its own schema.
        If validation fails, ``XmlMappingError.report`` contains the JSON
        Schema diagnostics. Other mappings do not require schema validation.

        Pass *node_mapping* to add Python mixins to matching projected
        occurrences; descriptor ``type=`` entries become occurrence pins on the
        derived document's policy, just as they do for :func:`parse_xml`.
        """
        from pyxsd.schemaless.mapping import XmlMapping, _project_document

        if not isinstance(mapping, XmlMapping):
            raise TypeError("mapping must be an XmlMapping")
        return _project_document(self, mapping, node_mapping=node_mapping)

    def to_xdm(self) -> XdmSequence:
        """Return this document's native value as an XDM 3.1 sequence.

        Objects become maps with ``xs:string`` keys, arrays become arrays whose
        members keep their nesting, and null becomes the empty sequence. A
        scalar uses an explicit pointer pin, unambiguous schema guidance,
        conservative string inference, or its exact native atomic kind, in
        that order. An invalid instance uses only pins and native kinds. A
        pin that rejects its value raises :class:`ScalarTypeError` with the
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

    def _schema_occurrences(self, pointer: str) -> tuple[SchemaOccurrence, ...]:
        if self._applied_schema is None or not self.is_valid:
            return ()
        return self._applied_schema.occurrences_at(pointer)

    def _schema_candidates(self, pointer: str, value: _ScalarValue) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                name
                for item in self._schema_occurrences(pointer)
                if isinstance(item.schema, dict)
                for name in format_annotation_types(item.schema, value)
            )
        )

    def _asserted_adapter(self, pointer: str, value: _ScalarValue) -> ScalarAdapter | None:
        """Prefer a compatible specific adapter over primitive guidance."""
        schemas = tuple(
            item.schema
            for item in self._schema_occurrences(pointer)
            if isinstance(item.schema, dict)
        )

        def lexical() -> str:
            return _schema_scalar_text(value)

        if self._format_typing:
            adapter = schema_format_adapter(schemas, value, lexical)
            if adapter is not None:
                return adapter
        hints = {
            _json_asserted_type(schema["type"], value) for schema in schemas if "type" in schema
        }
        return _json_schema_adapter(hints, lexical)

    def _resolution_at(self, pointer: str, value: _ScalarValue) -> tuple[ScalarAdapter | None, str]:
        """Resolve one native scalar and its current interpretation source."""
        asserted = (
            self._asserted_adapter(pointer, value)
            if self.is_valid and pointer not in self._types.pins
            else None
        )
        return self._types._json_adapter_source(
            pointer, value, asserted=asserted, allow_inference=self.is_valid
        )

    def _adapt_scalar(
        self, pointer: str, value: _ScalarValue, adapter: ScalarAdapter, interpretation: str
    ) -> Any:
        return _typed_scalar(
            self.source, pointer, adapter, value, asserted=interpretation == "schema-guided"
        )

    def _typed_tree(self, value: NativeJsonValue, pointer: str) -> Any:
        """Build an independent typed view of *value* at *pointer*."""
        return _fold_json(
            value,
            pointer,
            scalar=self._typed_leaf,
            object_value=lambda members: members,
            array_value=lambda members: members,
        )

    def _typed_leaf(self, pointer: str, value: _ScalarValue | None) -> Any:
        if value is None:
            return None
        adapter, interpretation = self._resolution_at(pointer, value)
        if adapter is None:
            return value
        return self._adapt_scalar(pointer, value, adapter, interpretation)

    def _convert_scalar(self, pointer: str, value: _ScalarValue) -> XdmAtomic | None:
        """Convert one native scalar through the effective policy adapter."""
        adapter, interpretation = self._resolution_at(pointer, value)
        if adapter is None:
            return None
        return adapter.to_xdm(self._adapt_scalar(pointer, value, adapter, interpretation))


def from_data(
    value: object,
    *,
    limits: JsonLimits | None = None,
    types: TypePolicy | None = None,
    json_schema: _JsonSchemaInput | None = None,
    base_uri: str | None = None,
    reference_registry: SchemaRegistry | None = None,
    resource_policy: ResourcePolicy | None = None,
    format_assertion: bool = False,
    format_typing: bool = False,
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
    native scalar kind. For the optional schema arguments, see :func:`from_json`.
    """
    data = copy_native_data(value, limits=limits)
    return _build_document(
        data,
        "<memory>",
        types,
        limits,
        json_schema,
        base_uri,
        reference_registry,
        resource_policy,
        format_assertion,
        format_typing,
    )


def from_json(
    source: _JsonSource,
    *,
    limits: JsonLimits | None = None,
    types: TypePolicy | None = None,
    json_schema: _JsonSchemaInput | None = None,
    base_uri: str | None = None,
    reference_registry: SchemaRegistry | None = None,
    resource_policy: ResourcePolicy | None = None,
    format_assertion: bool = False,
    format_typing: bool = False,
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

    *json_schema* accepts a Draft 2020-12 boolean, mapping, or strict JSON
    text. Relative references use the schema's *base_uri*, not the instance
    source. External resources must be registered in *reference_registry*
    or authorized and read by *resource_policy*. Format is an annotation
    unless *format_assertion* is true. Assertion uses deterministic checks for
    ``date``, ``date-time``, and ``time`` plus ``uuid``, ``ipv4``, ``ipv6``,
    ``json-pointer``, ``regex``, ``int32``, ``int64``, ``float``, ``double``,
    and ``byte``; ``regex`` follows Python ``re`` syntax, and unlisted formats
    depend on the installed ``jsonschema`` environment. Invalid schemas or
    unresolved references raise
    :class:`~pyxsd.schemaless.errors.JsonSchemaError`; instance failures are
    recorded in the document's report.

    *format_typing* independently enables supported format and content
    encoding annotations for typed reads and XDM conversion of valid
    instances. ``uuid``, ``ipv4``, and ``ipv6`` return standard-library values
    with canonical ``xs:string`` XDM; ``int32``, ``int64``, ``float``,
    ``double``, and ``byte`` or ``contentEncoding: base64`` reuse their XSD
    SimpleTypes. It never changes validation, native storage, or JSON output,
    and unsupported or rejected values keep their native interpretation.
    """
    source_name = _source_name(source)
    try:
        value = decode_json(source, limits=limits)
    except JsonParseError as error:
        raise _source_error(source_name, error) from error
    return _build_document(
        value,
        source_name,
        types,
        limits,
        json_schema,
        base_uri,
        reference_registry,
        resource_policy,
        format_assertion,
        format_typing,
    )


def _build_document(
    value: NativeJsonValue,
    source: str,
    types: TypePolicy | None,
    limits: JsonLimits | None,
    json_schema: _JsonSchemaInput | None,
    base_uri: str | None,
    reference_registry: SchemaRegistry | None,
    resource_policy: ResourcePolicy | None,
    format_assertion: bool,
    format_typing: bool,
) -> JsonDocument:
    document = JsonDocument._from_owned(value, source=source, types=types)
    document._format_typing = format_typing
    if json_schema is not None:
        from pyxsd.schemaless.json_schema import SchemaGuidance

        guidance = SchemaGuidance.compile(
            json_schema,
            source=source,
            limits=limits,
            base_uri=base_uri,
            reference_registry=reference_registry,
            resource_policy=resource_policy,
            format_assertion=format_assertion,
        )
        document._report, document._applied_schema = guidance._evaluate(value)
    return document


__all__ = [
    "JsonDocument",
    "from_data",
    "from_json",
]
