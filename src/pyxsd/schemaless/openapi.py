"""Translate OpenAPI XML Objects into the shared JSON-to-XML mapping.

The adapter stays at selected Schema Object scope. It reads the ``xml``
annotation on that schema and the schemas it names directly through
``properties``, ``items``, and ``prefixItems``. Reference resolution,
applicator reconciliation, and ordering remain the evaluator's job, so no
second schema reader is introduced here.

OAS 3.2 defaults are honored: an ``$ref``, ``$dynamicRef``, or array schema
defaults to ``nodeType: "none"`` and every other schema to ``"element"``.
Names are inferred from a JSON property (or the property that owns array
items), while a root or component name comes from ``root_name`` or
``xml.name``. An array item without property context, ``xml.name``, or an
explicit rule cannot be named and fails. CDATA becomes ordinary text because
XDM has no distinct CDATA node. Deprecated ``attribute`` and ``wrapped`` fields
remain compatible when no ``nodeType`` is present.

When instance descent crosses XML metadata that cannot be resolved yet
(``$ref``, ``$dynamicRef``, ``patternProperties``, or an applicator branch),
projection reports ``openapi-metadata-unresolved`` and continues with the
metadata it can apply.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn, cast

from pyxsd.locations import DataLocation
from pyxsd.namespaces import clark
from pyxsd.relaxng.uris import is_uri_reference
from pyxsd.schemaless.errors import XmlMappingError
from pyxsd.schemaless.json_schema import SchemaGuidance
from pyxsd.schemaless.json_values import NativeJsonValue, copy_native_data
from pyxsd.schemaless.json_xdm import escape_pointer_segment
from pyxsd.schemaless.mapping import MappingRule
from pyxsd.validation import ValidationReport

if TYPE_CHECKING:
    from referencing.jsonschema import SchemaRegistry

    from pyxsd.resource_policy import ResourcePolicy

_XmlSchema = Mapping[str, Any] | bool
_NODE_TYPES = frozenset({"element", "attribute", "text", "cdata", "none"})
_ABSOLUTE_IRI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_APPLICATOR_BRANCHES = (
    "allOf",
    "anyOf",
    "oneOf",
    "then",
    "else",
    "dependentSchemas",
    "unevaluatedProperties",
    "unevaluatedItems",
)
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
    [str, NativeJsonValue, MappingRule | None, bool],
    tuple[MappingRule | None, tuple[OpenApiMetadataWarning, ...]],
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


def _fail(code: str, message: str, schema_pointer: str, **details: object) -> NoReturn:
    """Raise one fatal construction diagnostic addressed by schema pointer."""
    report = ValidationReport(diagnostics_complete=True)
    report.add_error(
        message,
        code=code,
        location=DataLocation(None, schema_pointer),
        location_precision="exact",
        details={"schema_pointer": schema_pointer, **details},
    )
    raise XmlMappingError(report)


def _fail_unnamed(pointer: str, schema_pointer: str) -> NoReturn:
    """Reject an array item with no property context and no explicit name."""
    report = ValidationReport(diagnostics_complete=True)
    report.add_error(
        "OpenAPI array items need xml.name or an explicit name",
        code="unnamed-openapi-array-item",
        location=DataLocation(None, pointer),
        location_precision="exact",
        details={"json_pointer": pointer, "schema_pointer": schema_pointer},
    )
    raise XmlMappingError(report)


def _declares_array(schema: Mapping[str, Any]) -> bool:
    """Whether a schema declares the array type, including a nullable union."""
    declared = schema.get("type")
    if declared == "array":
        return True
    return isinstance(declared, list) and "array" in declared


def _check_namespace(namespace: str, pointer: str) -> None:
    """Require OAS 3.2's non-relative, literal namespace IRI."""
    literal = not any(ord(char) <= 0x7F and char in _NS_FORBIDDEN_ASCII for char in namespace)
    if not _ABSOLUTE_IRI.match(namespace) or not literal or not is_uri_reference(namespace):
        _fail(
            "invalid-openapi-namespace",
            f"an OpenAPI XML namespace must be a non-relative IRI; got {namespace!r}",
            pointer,
        )


def _read_xml(schema: Mapping[str, Any], pointer: str) -> _XmlObject:
    """Validate one XML Object and return its fixed fields."""
    raw = schema.get("xml", _MISSING)
    if raw is _MISSING:
        return _EMPTY_XML
    xml_pointer = f"{pointer}/xml"
    if not isinstance(raw, Mapping):
        _fail(
            "invalid-openapi-xml-object",
            "the OpenAPI xml field must be an object",
            xml_pointer,
        )
    node_type = raw.get("nodeType")
    if node_type is not None and (not isinstance(node_type, str) or node_type not in _NODE_TYPES):
        _fail(
            "invalid-openapi-xml-object",
            f"the OpenAPI nodeType {node_type!r} is not one of element, attribute, "
            "text, cdata, or none",
            f"{xml_pointer}/nodeType",
        )
    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        _fail(
            "invalid-openapi-xml-object",
            "the OpenAPI XML name must be a string",
            f"{xml_pointer}/name",
        )
    namespace = raw.get("namespace")
    if namespace is not None:
        if not isinstance(namespace, str):
            _fail(
                "invalid-openapi-xml-object",
                "the OpenAPI XML namespace must be a string",
                f"{xml_pointer}/namespace",
            )
        _check_namespace(namespace, f"{xml_pointer}/namespace")
    prefix = raw.get("prefix")
    if prefix is not None and not isinstance(prefix, str):
        _fail(
            "invalid-openapi-xml-object",
            "the OpenAPI XML prefix must be a string",
            f"{xml_pointer}/prefix",
        )
    attribute = raw.get("attribute")
    if attribute is not None and not isinstance(attribute, bool):
        _fail(
            "invalid-openapi-xml-object",
            "the OpenAPI XML attribute field must be a boolean",
            f"{xml_pointer}/attribute",
        )
    wrapped = raw.get("wrapped")
    if wrapped is not None and not isinstance(wrapped, bool):
        _fail(
            "invalid-openapi-xml-object",
            "the OpenAPI XML wrapped field must be a boolean",
            f"{xml_pointer}/wrapped",
        )
    if node_type is not None and (attribute is not None or wrapped is not None):
        _fail(
            "invalid-openapi-xml-object",
            "the OpenAPI XML nodeType cannot be combined with the deprecated "
            "attribute or wrapped fields",
            xml_pointer,
        )
    if wrapped is not None and not _declares_array(schema):
        _fail(
            "invalid-openapi-xml-object",
            "the OpenAPI XML wrapped field applies only to an array schema",
            f"{xml_pointer}/wrapped",
        )
    return _XmlObject(node_type, name, namespace, prefix, attribute, wrapped)


def _node_type(schema: Mapping[str, Any], xml: _XmlObject) -> str:
    """Resolve the XML node kind, applying OAS 3.2 and legacy defaults."""
    if xml.node_type is not None:
        return xml.node_type
    if xml.legacy_attribute is True:
        return "attribute"
    if xml.legacy_wrapped is True:
        return "element"
    if "$ref" in schema or "$dynamicRef" in schema or _declares_array(schema):
        return "none"
    return "element"


def _merge(base: MappingRule, override: MappingRule | None) -> MappingRule:
    """Layer explicit user policy over metadata, field by field.

    An explicit ``none`` or ``text`` kind replaces the metadata rule: those
    kinds cannot carry the naming, binding, array, or value fields that an
    element or attribute rule may hold.
    """
    if override is None:
        return base
    if override.kind in ("none", "text"):
        return override
    return MappingRule(
        name=override.name if override.name is not None else base.name,
        kind=override.kind if override.kind is not None else base.kind,
        namespace=override.namespace if override.namespace is not None else base.namespace,
        prefix=override.prefix if override.prefix is not None else base.prefix,
        array=override.array if override.array is not None else base.array,
        item_name=override.item_name if override.item_name is not None else base.item_name,
        null=override.null if override.null is not None else base.null,
        type=override.type if override.type is not None else base.type,
        order=override.order if override.order is not None else base.order,
    )


def _pointer_tokens(pointer: str) -> tuple[str, ...]:
    """Decode RFC 6901 tokens from one of the projector's JSON Pointers."""
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/"))


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


def _has_xml(schema: _XmlSchema) -> bool:
    """Whether *schema* or any schema it can reach carries an XML Object."""
    if isinstance(schema, bool):
        return False
    return "xml" in schema or any(_has_xml(child) for child, _ in _schema_children(schema, ""))


def _local_target(root: _XmlSchema, ref: str) -> tuple[_XmlSchema, str] | None:
    """Resolve a local JSON Pointer reference, without resolving anchors."""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    pointer = ref[1:]
    value: object = root
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            return None  # pragma: no cover - an unresolvable pointer cannot validate an instance
    if isinstance(value, (Mapping, bool)):
        return value, pointer
    return None  # pragma: no cover - a reference to a non-schema cannot validate an instance


def _anchor_target(root: _XmlSchema, ref: str) -> tuple[_XmlSchema, str] | None:
    """Find a local ``$dynamicAnchor`` or ``$anchor`` target for warning purposes."""
    if not isinstance(ref, str) or not ref.startswith("#") or ref.startswith("#/"):
        return None
    anchor = ref[1:]
    if not anchor:
        return root, ""
    stack: list[tuple[_XmlSchema, str]] = [(root, "")]
    while stack:
        schema, pointer = stack.pop()
        if isinstance(schema, bool):
            continue  # pragma: no cover - booleans carry no anchors
        if anchor in (schema.get("$dynamicAnchor"), schema.get("$anchor")):
            return schema, pointer
        stack.extend(_schema_children(schema, pointer))
    return None  # pragma: no cover - an unresolved anchor cannot validate an instance


def _applicator_children(
    schema: Mapping[str, Any], pointer: str
) -> Iterator[tuple[_XmlSchema, str, str]]:
    """Yield branches whose annotations this adapter does not reconcile yet."""
    for keyword in _APPLICATOR_BRANCHES:
        if keyword not in schema:
            continue
        children = schema[keyword]
        if keyword == "dependentSchemas":
            for name, child in children.items():
                yield (
                    cast(_XmlSchema, child),
                    f"{pointer}/{keyword}/{escape_pointer_segment(name)}",
                    keyword,
                )
        elif keyword in ("allOf", "anyOf", "oneOf"):
            for index, child in enumerate(children):
                yield cast(_XmlSchema, child), f"{pointer}/{keyword}/{index}", keyword
        else:
            yield cast(_XmlSchema, children), f"{pointer}/{keyword}", keyword


class _OpenApiMapping:
    """Compiled metadata lookup for one selected OpenAPI Schema Object."""

    def __init__(self, schema: _XmlSchema) -> None:
        self._schema = schema
        self._xml: dict[int, _XmlObject] = {}
        self._walk(schema, "")
        self._root_prefix = None if isinstance(schema, bool) else self._xml_for(schema).prefix

    def _walk(self, schema: _XmlSchema, pointer: str) -> None:
        if isinstance(schema, bool):
            return
        self._xml[id(schema)] = _read_xml(schema, pointer)
        for child, child_pointer in _schema_children(schema, pointer):
            self._walk(child, child_pointer)

    def _xml_for(self, schema: Mapping[str, Any]) -> _XmlObject:
        return self._xml.get(id(schema), _EMPTY_XML)

    def rule_at(
        self,
        pointer: str,
        document_value: NativeJsonValue,
        override: MappingRule | None,
        item_name_explicit: bool,
    ) -> tuple[MappingRule | None, tuple[OpenApiMetadataWarning, ...]]:
        """Return the metadata rule at an instance pointer layered under *override*."""
        warnings: list[OpenApiMetadataWarning] = []
        if not pointer:
            if not isinstance(self._schema, bool):
                warnings.extend(self._unresolved_at(self._schema, "", None, document_value))
            base = MappingRule(prefix=self._root_prefix)
            return _merge(base, override), tuple(warnings)
        located = self._locate(pointer, document_value, warnings)
        if located is None:
            return override, tuple(warnings)
        schema, inferred, schema_pointer = located
        merged = _merge(self._rule_for(schema, inferred), override)
        if item_name_explicit and (override is None or override.name is None):
            merged = replace(merged, name=None)
        if (
            merged.kind in ("element", "attribute")
            and merged.name is None
            and not item_name_explicit
        ):
            _fail_unnamed(pointer, schema_pointer)
        return merged, tuple(warnings)

    def _unresolved_at(
        self,
        schema: Mapping[str, Any],
        schema_pointer: str,
        token: str | None,
        value: NativeJsonValue,
    ) -> tuple[OpenApiMetadataWarning, ...]:
        """Report XML metadata hidden behind constructs Task 19 will resolve."""
        pointers: list[str] = []
        constructs: list[str] = []
        for keyword in ("$ref", "$dynamicRef"):
            if keyword not in schema:
                continue
            ref = schema[keyword]
            target = _local_target(self._schema, ref) or _anchor_target(self._schema, ref)
            if target is None or _has_xml(target[0]):
                pointers.append(target[1] if target is not None else f"{schema_pointer}/{keyword}")
                constructs.append(keyword)
        if isinstance(value, Mapping) and token is not None:
            patterns = schema.get("patternProperties")
            if isinstance(patterns, Mapping):
                for pattern, child in patterns.items():
                    if re.search(pattern, token) is not None and _has_xml(child):
                        pointers.append(
                            f"{schema_pointer}/patternProperties/{escape_pointer_segment(pattern)}"
                        )
                        constructs.append("patternProperties")
        for child, child_pointer, keyword in _applicator_children(schema, schema_pointer):
            if _has_xml(child):
                pointers.append(child_pointer)
                constructs.append(keyword)
        if not pointers:
            return ()
        message = (
            "OpenAPI XML metadata at "
            + ", ".join(dict.fromkeys(pointers))
            + " was not applied because "
            + ", ".join(dict.fromkeys(constructs))
            + " is not resolved"
        )
        return (
            OpenApiMetadataWarning(
                message,
                tuple(dict.fromkeys(pointers)),
                tuple(dict.fromkeys(constructs)),
            ),
        )

    def _locate(
        self, pointer: str, document_value: NativeJsonValue, warnings: list[OpenApiMetadataWarning]
    ) -> tuple[_XmlSchema, str | None, str] | None:
        """Follow object members and array items by the actual native value kind."""
        schema = self._schema
        schema_pointer = ""
        inferred: str | None = None
        value = document_value
        for token in _pointer_tokens(pointer):
            if isinstance(schema, bool):
                return None
            warnings.extend(self._unresolved_at(schema, schema_pointer, token, value))
            located = _member_schema(schema, schema_pointer, token, value)
            if located is None:
                return None
            schema, schema_pointer, is_item, value = located
            if not is_item:
                inferred = token
        if not isinstance(schema, bool):
            warnings.extend(self._unresolved_at(schema, schema_pointer, None, value))
        return schema, inferred, schema_pointer

    def _rule_for(self, schema: _XmlSchema, inferred: str | None) -> MappingRule:
        if isinstance(schema, bool):
            node_type = "element"
            xml = _EMPTY_XML
        else:
            xml = self._xml_for(schema)
            node_type = _node_type(schema, xml)
        if node_type in ("text", "cdata"):
            return MappingRule(kind="text")
        if node_type == "none":
            return MappingRule(kind="none")
        name = xml.name if xml.name is not None else inferred
        if node_type == "attribute":
            return MappingRule(name, kind="attribute", namespace=xml.namespace, prefix=xml.prefix)
        wrapped = not isinstance(schema, bool) and _declares_array(schema)
        return MappingRule(
            name,
            kind="element",
            namespace=xml.namespace,
            prefix=xml.prefix,
            array="wrapped" if wrapped else None,
        )


def _member_schema(
    schema: Mapping[str, Any],
    schema_pointer: str,
    token: str,
    value: NativeJsonValue,
) -> tuple[_XmlSchema, str, bool, NativeJsonValue] | None:
    """Follow one pointer token according to the native container that owns it."""
    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, Mapping) and token in properties:
            pointer = f"{schema_pointer}/properties/{escape_pointer_segment(token)}"
            return cast(_XmlSchema, properties[token]), pointer, False, value[token]
        if "additionalProperties" in schema:
            return (
                cast(_XmlSchema, schema["additionalProperties"]),
                f"{schema_pointer}/additionalProperties",
                False,
                value[token],
            )
        return None
    if isinstance(value, list):
        index = int(token)
        prefix = schema.get("prefixItems")
        if isinstance(prefix, list) and index < len(prefix):
            return (
                cast(_XmlSchema, prefix[index]),
                f"{schema_pointer}/prefixItems/{index}",
                True,
                value[index],
            )
        if "items" in schema:
            return cast(_XmlSchema, schema["items"]), f"{schema_pointer}/items", True, value[index]
        return {}, f"{schema_pointer}/items", True, value[index]
    return None  # pragma: no cover - a present pointer cannot descend below a scalar


def compile_openapi_mapping(
    schema_object: object,
    *,
    root_name: str | None,
    dialect: str | None,
    base_uri: str | None,
    reference_registry: SchemaRegistry | None,
    resource_policy: ResourcePolicy | None,
) -> tuple[SchemaGuidance, str, RuleCallable]:
    """Compile validation guidance plus the metadata rule lookup for a schema."""
    if dialect is not None:
        _fail(
            "unsupported-openapi-dialect",
            f"custom OpenAPI dialect {dialect!r} is not supported",
            "",
        )
    if not isinstance(schema_object, (Mapping, bool)):
        raise TypeError("the OpenAPI schema_object must be a loaded mapping or boolean schema")
    copied = cast(_XmlSchema, copy_native_data(schema_object))
    guidance = SchemaGuidance.compile(
        copied,
        base_uri=base_uri,
        reference_registry=reference_registry,
        resource_policy=resource_policy,
    )
    if isinstance(copied, bool):
        root_xml = _EMPTY_XML
    else:
        root_xml = _read_xml(copied, "")
        if _node_type(copied, root_xml) == "attribute":
            _fail(
                "invalid-openapi-xml-object",
                "an OpenAPI root schema cannot describe an attribute",
                "/xml",
            )
    selected = root_name if root_name is not None else root_xml.name
    if selected is None:
        _fail(
            "unnamed-openapi-schema",
            "an inline OpenAPI schema needs root_name or xml.name to name the root element",
            "",
        )
    if not isinstance(selected, str):
        raise TypeError("root_name must be a string")
    if root_xml.namespace is not None:
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
    return guidance, selected, _OpenApiMapping(copied).rule_at


__all__ = ["compile_openapi_mapping"]
