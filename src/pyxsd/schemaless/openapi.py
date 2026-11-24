"""Translate applied OpenAPI XML annotations into the shared XML projection."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn, cast

from pyxsd.locations import DataLocation
from pyxsd.namespaces import clark
from pyxsd.relaxng.uris import is_uri_reference
from pyxsd.schemaless.errors import XmlMappingError
from pyxsd.schemaless.json_schema import _DRAFT, SchemaGuidance
from pyxsd.schemaless.json_values import NativeJsonValue, _pointer_token, resolve_pointer
from pyxsd.schemaless.json_xdm import escape_pointer_segment
from pyxsd.schemaless.mapping import MappingRule, _check_local_name
from pyxsd.validation import ValidationReport

if TYPE_CHECKING:
    from referencing.jsonschema import SchemaRegistry

    from pyxsd.resource_policy import ResourcePolicy
    from pyxsd.schemaless.json_schema import SchemaOccurrence, _AppliedSchema

_XmlSchema = Mapping[str, Any] | bool
_NODE_TYPES = frozenset({"element", "attribute", "text", "cdata", "none"})
_ABSOLUTE_IRI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_OAS_DIALECTS = frozenset(
    {
        "https://spec.openapis.org/oas/3.1/dialect/base",
        "https://spec.openapis.org/oas/3.1/dialect/2024-10-25",
        "https://spec.openapis.org/oas/3.1/dialect/2024-11-10",
        "https://spec.openapis.org/oas/3.2/dialect/2025-09-17",
        "https://spec.openapis.org/oas/3.2/dialect/2026-02-26",
    }
)
_ROOT_APPLICATORS = (
    "$ref",
    "$dynamicRef",
    "allOf",
    "anyOf",
    "oneOf",
    "then",
    "else",
    "dependentSchemas",
)
_UNNAMED_ROOT = "an inline OpenAPI schema needs root_name or xml.name to name the root element"
_SINGLE_SCHEMAS = (
    "additionalProperties",
    "items",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_SCHEMA_SEQUENCES = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "dependentSchemas")

#: ASCII characters that cannot appear literally in a namespace name because a
#: URI reference must percent-encode them. Mirrors the shared mapping policy.
_NS_FORBIDDEN_ASCII = frozenset(
    [chr(code) for code in range(0x20)]
    + [" ", "<", ">", '"', "{", "}", "|", "\\", "^", "`", "\x7f"]
)


@dataclass(frozen=True, slots=True)
class OpenApiMetadataWarning:
    """XML metadata that projection could not consult at one occurrence."""

    message: str
    schema_pointers: tuple[str, ...]
    constructs: tuple[str, ...]


RuleCallable = Callable[
    [str, "_AppliedSchema", NativeJsonValue, MappingRule | None, str | None, str, str],
    tuple[MappingRule, tuple[OpenApiMetadataWarning, ...]],
]


@dataclass(frozen=True, slots=True)
class _XmlObject:
    """The validated fixed fields of one OpenAPI XML Object."""

    node_type: str | None = None
    name: str | None = None
    namespace: str | None = None
    prefix: str | None = None
    legacy_attribute: bool | None = None
    legacy_wrapped: bool | None = None


_EMPTY_XML = _XmlObject()
_MISSING = object()


def _fail(
    code: str,
    message: str,
    schema_pointer: str,
    *,
    occurrence: SchemaOccurrence | None = None,
    source: str | None = None,
) -> NoReturn:
    """Raise a diagnostic at the XML Object field and, when known, its instance."""
    details: dict[str, object] = {"schema_pointer": schema_pointer}
    location = DataLocation(source, schema_pointer)
    if occurrence is not None:
        location = DataLocation(source, occurrence.instance_pointer, schema_pointer=schema_pointer)
        details.update(
            json_pointer=occurrence.instance_pointer, schema_source=occurrence._schema_source
        )
    report = ValidationReport(diagnostics_complete=True)
    report.add_error(
        message,
        code=code,
        location=location,
        location_precision="exact",
        details=details,
    )
    raise XmlMappingError(report)


def _declares_array(schema: Mapping[str, Any]) -> bool:
    """Whether a schema declares the array type, including a nullable union."""
    declared = schema.get("type")
    if declared == "array":
        return True
    return isinstance(declared, list) and "array" in declared


def _valid_namespace(namespace: str) -> bool:
    """OAS requires a non-relative, literal namespace IRI."""
    literal = not any(ord(char) <= 0x7F and char in _NS_FORBIDDEN_ASCII for char in namespace)
    return bool(_ABSOLUTE_IRI.match(namespace) and literal and is_uri_reference(namespace))


def _read_xml(
    schema: Mapping[str, Any],
    pointer: str,
    *,
    occurrence: SchemaOccurrence | None = None,
    source: str | None = None,
) -> _XmlObject:
    """Validate one XML Object and return its fixed fields."""
    raw = schema.get("xml", _MISSING)
    if raw is _MISSING:
        return _EMPTY_XML
    xml_pointer = f"{pointer}/xml"

    def invalid(
        message: str, field: str = "", *, code: str = "invalid-openapi-xml-object"
    ) -> NoReturn:
        _fail(code, message, xml_pointer + field, occurrence=occurrence, source=source)

    if not isinstance(raw, Mapping):
        invalid("the OpenAPI xml field must be an object")
    node_type = raw.get("nodeType")
    if node_type is not None and (not isinstance(node_type, str) or node_type not in _NODE_TYPES):
        invalid(
            f"the OpenAPI nodeType {node_type!r} is not one of element, attribute, "
            "text, cdata, or none",
            "/nodeType",
        )
    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        invalid("the OpenAPI XML name must be a string", "/name")
    namespace = raw.get("namespace")
    if namespace is not None:
        if not isinstance(namespace, str):
            invalid("the OpenAPI XML namespace must be a string", "/namespace")
        if not _valid_namespace(namespace):
            invalid(
                f"an OpenAPI XML namespace must be a non-relative IRI; got {namespace!r}",
                "/namespace",
                code="invalid-openapi-namespace",
            )
    prefix = raw.get("prefix")
    if prefix is not None and not isinstance(prefix, str):
        invalid("the OpenAPI XML prefix must be a string", "/prefix")
    attribute = raw.get("attribute")
    if attribute is not None and not isinstance(attribute, bool):
        invalid("the OpenAPI XML attribute field must be a boolean", "/attribute")
    wrapped = raw.get("wrapped")
    if wrapped is not None and not isinstance(wrapped, bool):
        invalid("the OpenAPI XML wrapped field must be a boolean", "/wrapped")
    if node_type is not None and (attribute is not None or wrapped is not None):
        invalid(
            "the OpenAPI XML nodeType cannot be combined with the deprecated "
            "attribute or wrapped fields"
        )
    if wrapped is not None and not _declares_array(schema):
        invalid("the OpenAPI XML wrapped field applies only to an array schema", "/wrapped")
    return _XmlObject(node_type, name, namespace, prefix, attribute, wrapped)


def _explicit_kind(xml: _XmlObject, *, transparent: bool) -> str | None:
    """Normalize legacy fields and CDATA before checking compatibility."""
    if xml.node_type is not None:
        return "text" if xml.node_type == "cdata" else xml.node_type
    if xml.legacy_attribute:
        return "attribute"
    if xml.legacy_wrapped is not None:
        return "element" if xml.legacy_wrapped else "none"
    if xml.legacy_attribute is False:
        return "none" if transparent else "element"
    return None


def _schema_children(schema: Mapping[str, Any], pointer: str) -> Iterator[tuple[_XmlSchema, str]]:
    """Yield direct subschemas whose XML Objects this adapter can own."""
    for keyword in _SINGLE_SCHEMAS:
        if keyword in schema:
            yield cast(_XmlSchema, schema[keyword]), f"{pointer}/{keyword}"
    for keyword in _SCHEMA_SEQUENCES:
        children = schema.get(keyword)
        if isinstance(children, list):
            for index, child in enumerate(children):
                yield cast(_XmlSchema, child), f"{pointer}/{keyword}/{index}"
    for keyword in _SCHEMA_MAPS:
        children = schema.get(keyword)
        if isinstance(children, Mapping):
            for name, child in children.items():
                yield cast(_XmlSchema, child), f"{pointer}/{keyword}/{escape_pointer_segment(name)}"


def _validate_xml_tree(schema: _XmlSchema, pointer: str = "") -> None:
    """Check authored XML Objects, without trying to apply any subschema."""
    if not isinstance(schema, bool):
        _read_xml(schema, pointer)
        for child, child_pointer in _schema_children(schema, pointer):
            _validate_xml_tree(child, child_pointer)


def _may_have_xml(schema: _XmlSchema) -> bool:
    """Include unknown ref targets when checking unsupported applicators."""
    return not isinstance(schema, bool) and (
        any(key in schema for key in ("xml", "$ref", "$dynamicRef"))
        or any(_may_have_xml(child) for child, _ in _schema_children(schema, ""))
    )


@dataclass(frozen=True, slots=True)
class _Annotation:
    occurrence: SchemaOccurrence
    xml: _XmlObject
    kind: str | None
    transparent: bool


def _annotations(occurrences: tuple[SchemaOccurrence, ...], source: str) -> Iterator[_Annotation]:
    for occurrence in occurrences:
        schema = occurrence.schema
        if not isinstance(schema, bool):
            xml = _read_xml(schema, occurrence.schema_pointer, occurrence=occurrence, source=source)
            transparent = _declares_array(schema) or "$ref" in schema or "$dynamicRef" in schema
            yield _Annotation(
                occurrence, xml, _explicit_kind(xml, transparent=transparent), transparent
            )


def _pick(
    field: str,
    candidates: Iterator[tuple[str | None, SchemaOccurrence]],
    pointer: str,
    source: str,
) -> str | None:
    """Merge one annotation field without privileging a successful branch."""
    choices = [(value, occurrence) for value, occurrence in candidates if value is not None]
    values = tuple(dict.fromkeys(value for value, _ in choices))
    if len(values) > 1:
        origins = tuple(
            dict.fromkeys((item.schema_pointer, item._schema_source) for _, item in choices)
        )
        report = ValidationReport(diagnostics_complete=True)
        report.add_error(
            f"ambiguous OpenAPI XML {field}; supply an explicit rule at this pointer",
            code="ambiguous-openapi-mapping",
            location=DataLocation(source, pointer),
            location_precision="exact",
            details={
                "json_pointer": pointer,
                "field": field,
                "values": values,
                "schema_pointers": tuple(path for path, _ in origins),
                "schema_sources": tuple(uri for _, uri in origins),
            },
        )
        raise XmlMappingError(report)
    return values[0] if values else None


def _name_parts(name: str | None) -> tuple[str | None, str | None]:
    """Allow compatible Clark and separate name/namespace annotations to merge."""
    if name is not None and name.startswith("{") and "}" in name:
        uri, _, local = name[1:].partition("}")
        return local, uri
    return name, None


def _inferred_item_name(
    pointer: str, document: NativeJsonValue, xml_path: str, source: str
) -> str | None:
    """Use the nearest owning object key, not an array index, as the item name."""
    ancestor = pointer
    while ancestor:
        parent, _, raw = ancestor.rpartition("/")
        if isinstance(resolve_pointer(document, parent), dict):
            name = _pointer_token(raw, ancestor)
            _check_local_name(name, pointer, xml_path, source)
            return name
        ancestor = parent
    return None


def _warnings_at(applied: _AppliedSchema, pointer: str) -> tuple[OpenApiMetadataWarning, ...]:
    incomplete, unevaluated = applied.metadata_gaps_at(pointer)
    warnings = []
    if incomplete is not None:
        warnings.append(
            OpenApiMetadataWarning(
                "OpenAPI XML metadata could not be resolved from every applicable schema",
                (incomplete,),
                ("applicators",),
            )
        )
    for keyword, item in unevaluated:
        if _may_have_xml(item.schema):
            warnings.append(
                OpenApiMetadataWarning(
                    f"OpenAPI XML metadata from {keyword} cannot be resolved",
                    (item.schema_pointer,),
                    (keyword,),
                )
            )
    return tuple(warnings)


@dataclass(frozen=True, slots=True)
class _OpenApiMapping:
    """Reconcile only the facts the shared evaluator applies at this pointer."""

    root_name: str | None
    root_xml: _XmlObject

    def rule_at(
        self,
        pointer: str,
        applied: _AppliedSchema,
        document: NativeJsonValue,
        override: MappingRule | None,
        item_name: str | None,
        source: str,
        xml_path: str,
    ) -> tuple[MappingRule, tuple[OpenApiMetadataWarning, ...]]:
        occurrences = applied.occurrences_at(pointer)
        warnings = _warnings_at(applied, pointer)
        override = override or MappingRule()
        if override.kind in ("none", "text"):
            return override, warnings

        annotations = tuple(_annotations(occurrences, source))
        kind: str | None
        if override.kind is not None:
            kind = override.kind
        elif override.array is not None:
            # The caller selected array layout, so don't reconcile schema wrapping.
            kind = "element"
        else:
            kind = _pick("kind", ((a.kind, a.occurrence) for a in annotations), pointer, source)
        if not pointer:
            # The document always has a root element, even for an array or nodeType none.
            if kind == "attribute" and override.kind is None:
                origin = next(a.occurrence for a in annotations if a.kind == "attribute")
                _fail(
                    "invalid-openapi-xml-object",
                    "an OpenAPI root cannot be an attribute",
                    f"{origin.schema_pointer}/xml",
                    occurrence=origin,
                    source=source,
                )
            kind = "element" if override.kind is None else override.kind
        elif kind is None:
            # Defaults are not assertions. In particular a plain reference must
            # not suppress the XML Object on the schema it applies.
            has_named_metadata = any(
                not a.transparent
                and (
                    a.xml.name is not None
                    or a.xml.namespace is not None
                    or a.xml.prefix is not None
                )
                for a in annotations
            )
            kind = (
                "none"
                if not has_named_metadata and any(a.transparent for a in annotations)
                else "element"
            )
        if kind in ("none", "text"):
            return replace(override, kind=kind), warnings

        # Names on an annotation that explicitly produces no named node are ignored.
        named = tuple(
            a
            for a in annotations
            if not pointer
            or a.kind in ("element", "attribute")
            or (a.kind is None and not a.transparent)
        )
        names = tuple((_name_parts(a.xml.name), a) for a in named)
        explicit_name, explicit_uri = _name_parts(override.name)
        root_name, root_uri = _name_parts(self.root_name if not pointer else None)
        name = explicit_name if override.name is not None else root_name
        if name is None and item_name is None:
            name = _pick(
                "name", ((local, a.occurrence) for (local, _), a in names), pointer, source
            )
        namespace = override.namespace if override.namespace is not None else explicit_uri
        if namespace is None and override.name is None:
            _, namespace = _name_parts(item_name)
        if namespace is None:
            namespace = _pick(
                "namespace",
                (
                    (uri, a.occurrence)
                    for (_, embedded), a in names
                    for uri in (a.xml.namespace, embedded)
                ),
                pointer,
                source,
            )
        prefix = override.prefix
        if prefix is None:
            prefix = _pick("prefix", ((a.xml.prefix, a.occurrence) for a in named), pointer, source)
        if not pointer:
            if name is None:
                name = self.root_xml.name
            if name is None:
                _fail("unnamed-openapi-schema", _UNNAMED_ROOT, "", source=source)
            if not occurrences:
                namespace = self.root_xml.namespace if namespace is None else namespace
                prefix = self.root_xml.prefix if prefix is None else prefix
            if root_uri is not None and override.name is None and override.namespace is None:
                name = clark(root_uri, name)
        elif name is None and item_name is None:
            parent = pointer.rpartition("/")[0]
            if isinstance(resolve_pointer(document, parent), list) and (
                occurrences or applied.occurrences_at(parent)
            ):
                name = _inferred_item_name(pointer, document, xml_path, source)
                if name is None:
                    schema = (occurrences or applied.occurrences_at(parent))[0]
                    report = ValidationReport(diagnostics_complete=True)
                    report.add_error(
                        "OpenAPI array items need xml.name or an explicit name",
                        code="unnamed-openapi-array-item",
                        location=DataLocation(
                            source, pointer, schema_pointer=schema.schema_pointer
                        ),
                        location_precision="exact",
                        details={"json_pointer": pointer, "schema_pointer": schema.schema_pointer},
                    )
                    raise XmlMappingError(report)

        # Preserve caller policy (including a Clark override name's spelling).
        return replace(
            override,
            name=name if override.name is None else override.name,
            kind=kind,
            namespace=namespace,
            prefix=prefix,
        ), warnings


def compile_openapi_mapping(
    schema_object: object,
    *,
    root_name: str | None,
    root_override: MappingRule | None,
    dialect: str | None,
    base_uri: str | None,
    reference_registry: SchemaRegistry | None,
    resource_policy: ResourcePolicy | None,
) -> tuple[SchemaGuidance, str | None, RuleCallable]:
    """Compile validation guidance plus the metadata rule lookup for a schema."""
    if dialect is not None and dialect.removesuffix("#") not in _OAS_DIALECTS | {_DRAFT}:
        _fail(
            "unsupported-openapi-dialect",
            f"custom OpenAPI dialect {dialect!r} is not supported",
            "",
        )
    if not isinstance(schema_object, (Mapping, bool)):
        raise TypeError("the OpenAPI schema_object must be a loaded mapping or boolean schema")
    guidance = SchemaGuidance.compile(
        schema_object,
        base_uri=base_uri,
        reference_registry=reference_registry,
        resource_policy=resource_policy,
        _openapi_dialects=_OAS_DIALECTS,
    )
    if root_name is not None and not isinstance(root_name, str):
        raise TypeError("root_name must be a string")
    if isinstance(schema_object, bool):
        root_xml = _EMPTY_XML
    else:
        _validate_xml_tree(schema_object)
        root_xml = _read_xml(schema_object, "")
        if _explicit_kind(root_xml, transparent=False) == "attribute" and (
            root_override is None or (root_override.kind is None and root_override.array is None)
        ):
            _fail(
                "invalid-openapi-xml-object",
                "an OpenAPI root schema cannot describe an attribute",
                "/xml",
            )
    selected = root_override.name if root_override is not None else None
    if selected is None:
        selected = root_name if root_name is not None else root_xml.name
    if selected is None:
        if isinstance(schema_object, bool) or not any(
            key in schema_object for key in _ROOT_APPLICATORS
        ):
            _fail("unnamed-openapi-schema", _UNNAMED_ROOT, "")
    elif root_xml.namespace is not None and (
        root_override is None or (root_override.namespace is None and root_override.name is None)
    ):
        if selected.startswith("{"):
            uri, separator, _ = selected[1:].partition("}")
            if not separator or uri != root_xml.namespace:
                _fail(
                    "invalid-openapi-xml-object",
                    "the OpenAPI root name and xml.namespace specify different namespaces",
                    "/xml/namespace",
                )
        else:
            selected = clark(root_xml.namespace, selected)
    return guidance, selected, _OpenApiMapping(root_name, root_xml).rule_at


__all__ = ["compile_openapi_mapping"]
