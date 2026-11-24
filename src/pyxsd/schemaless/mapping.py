"""Explicit projection from native JSON into XML.

A :class:`~pyxsd.schemaless.json_document.JsonDocument` never turns its native
value into XML on its own. :meth:`JsonDocument.to_xml` asks for one explicit
:class:`XmlMapping`, which names the root element and applies rules by JSON
Pointer. Element-first is the conservative default. Compact mapping turns
scalar object properties into attributes unless a rule selects another kind.

The projection is one-way. It reads authoritative native storage, builds a
fresh :class:`~pyxsd.schemaless.document.SchemalessDocument` with no source
bytes, and never writes edits back to the JSON document. JSON Pointer pins are
a separate addressing language and are not carried into the XML tree. Fatal
mapping problems raise :class:`~pyxsd.schemaless.errors.XmlMappingError` with
structured diagnostics; information the XML shape cannot retain is reported as
a non-fatal warning on the derived document's report.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, NoReturn
from xml.etree import ElementTree as ET
from xml.parsers import expat

from pyxsd.locations import DataLocation
from pyxsd.namespaces import XML_NS, XSI_NS, NamespaceContext, clark, namespace_of
from pyxsd.schemaless.document import SchemalessDocument
from pyxsd.schemaless.errors import ScalarTypeError, XmlMappingError
from pyxsd.schemaless.json_document import _typed_scalar
from pyxsd.schemaless.json_values import NativeJsonValue, encode_json
from pyxsd.schemaless.json_xdm import escape_pointer_segment
from pyxsd.schemaless.nodes import NodeMapping
from pyxsd.schemaless.tree import SchemalessElement
from pyxsd.schemaless.types import _PARSE_ERRORS, TypePolicy, TypeRef
from pyxsd.validation import ValidationReport

if TYPE_CHECKING:
    from referencing.jsonschema import SchemaRegistry

    from pyxsd.resource_policy import ResourcePolicy
    from pyxsd.schemaless.json_document import JsonDocument, _JsonSchemaInput
    from pyxsd.schemaless.json_schema import SchemaGuidance, _AppliedSchema
    from pyxsd.schemaless.nodes import _CompiledNodeMapping
    from pyxsd.schemaless.openapi import OpenApiMetadataWarning

_XSI_NIL = f"{{{XSI_NS}}}nil"
_XMLNS_NS = "http://www.w3.org/2000/xmlns/"
_POINTER_BAD_ESCAPE = re.compile(r"~(?:[^01]|$)")
_Scalar = bool | int | float | Decimal | str

#: ASCII characters XLink 1.0 section 5.4 percent-escapes. A namespace name is
#: a literal identifier, never an escaped ``xs:anyURI`` value, so these cannot
#: appear in one; legal non-ASCII IRI characters remain allowed.
_NS_FORBIDDEN_ASCII = frozenset(
    [chr(code) for code in range(0x20)]
    + [" ", "<", ">", '"', "{", "}", "|", "\\", "^", "`", "\x7f"]
)


@dataclass(frozen=True, slots=True)
class MappingRule:
    """One explicit override for an occurrence addressed by JSON Pointer.

    ``kind`` overrides the default node kind. ``none`` inlines container
    children and drops scalar values with a warning. ``name`` and ``item_name``
    are local or Clark names; keys used as names must be local. ``namespace``
    qualifies a local ``name``; ``prefix`` supplies a lexical binding.

    Arrays wrap by default. Unwrapped arrays repeat the occurrence name
    unless ``item_name`` supplies a separate name. ``order`` lists object
    keys to emit first. By default null emits ``xsi:nil`` for elements and
    is omitted with a warning for attributes and text. ``type`` formats a
    scalar's lexical value without annotating the XML or changing the JSON.
    """

    name: str | None = None
    _: KW_ONLY
    kind: str | None = None
    namespace: str | None = None
    prefix: str | None = None
    array: str | None = None
    item_name: str | None = None
    null: str | None = None
    type: TypeRef | None = None
    order: Sequence[str] | None = None

    def __post_init__(self) -> None:
        for field_name in ("name", "namespace", "prefix", "item_name"):
            component = getattr(self, field_name)
            if component is not None and not isinstance(component, str):
                raise TypeError(f"a mapping rule {field_name} must be a string or None")
        order = self.order
        if order is None:
            return
        if not isinstance(order, Sequence) or isinstance(order, (str, bytes)):
            raise TypeError("mapping rule order must be a sequence of strings")
        if not all(isinstance(key, str) for key in order):
            raise TypeError("mapping rule order must be a sequence of strings")
        object.__setattr__(self, "order", tuple(order))


_DEFAULT_RULE = MappingRule()


class XmlMapping:
    """A root name plus per-pointer projection rules.

    ``rules`` maps RFC 6901 JSON Pointers to :class:`MappingRule` values. A
    pointer that does not occur in a particular document is simply policy for a
    possible occurrence; it is not an error, so mapping factories can describe
    optional schema properties. Use :meth:`compact` to opt into scalar
    attributes. Rules are checked when their occurrences are projected,
    because only then are their XML paths known.
    """

    __slots__ = ("_default", "_derived", "_guidance", "_root_name", "_rules")

    def __init__(
        self,
        root_name: str,
        *,
        default: str = "elements",
        rules: Mapping[str, MappingRule] | None = None,
    ) -> None:
        if not isinstance(root_name, str):
            raise TypeError("root_name must be a string")
        if default != "elements":
            raise ValueError(
                f"with the direct XmlMapping constructor only 'elements' is available; "
                f"got {default!r}. Use XmlMapping.compact() to opt in to compact projection"
            )
        root_path = f"/{root_name}[1]"
        _check_xml_name(root_name, "", root_path, None)

        checked: dict[str, MappingRule] = {}
        if rules is not None and not isinstance(rules, Mapping):
            raise TypeError("mapping rules must be a mapping from JSON Pointers to rules")
        for pointer, rule in (rules or {}).items():
            if not isinstance(pointer, str):
                raise TypeError("mapping rule pointers must be strings")
            problem = _pointer_problem(pointer)
            if problem:
                _fail(
                    "invalid-json-pointer",
                    f"invalid JSON Pointer {pointer!r}: {problem}",
                    pointer,
                    root_path,
                    None,
                )
            if not isinstance(rule, MappingRule):
                raise TypeError("mapping rule values must be MappingRule instances")
            checked[pointer] = rule

        self._root_name = root_name
        self._default = "elements"
        self._rules: Mapping[str, MappingRule] = MappingProxyType(checked)
        self._guidance: SchemaGuidance | None = None
        self._derived: (
            Callable[
                [str, NativeJsonValue, MappingRule | None, bool],
                tuple[MappingRule | None, tuple[OpenApiMetadataWarning, ...]],
            ]
            | None
        ) = None

    @property
    def root_name(self) -> str:
        """The required element name of the projected document."""
        return self._root_name

    @property
    def default(self) -> str:
        """The projection policy: ``"elements"`` or ``"compact"``."""
        return self._default

    @property
    def rules(self) -> Mapping[str, MappingRule]:
        """An immutable view of the JSON Pointer to rule policy."""
        return self._rules

    @classmethod
    def elements(
        cls, root_name: str, *, rules: Mapping[str, MappingRule] | None = None
    ) -> XmlMapping:
        """Return the conservative element-first mapping for *root_name*."""
        return cls(root_name, default="elements", rules=rules)

    @classmethod
    def compact(
        cls, root_name: str, *, rules: Mapping[str, MappingRule] | None = None
    ) -> XmlMapping:
        """Map scalar object properties to attributes unless overridden."""
        mapping = cls(root_name, rules=rules)
        mapping._default = "compact"
        return mapping

    @classmethod
    def from_json_schema(
        cls,
        schema: _JsonSchemaInput,
        *,
        root_name: str,
        rules: Mapping[str, MappingRule] | None = None,
        base_uri: str | None = None,
        reference_registry: SchemaRegistry | None = None,
        resource_policy: ResourcePolicy | None = None,
    ) -> XmlMapping:
        """Compile Draft 2020-12 guidance for an element-first projection.

        The schema must validate the instance at projection time. Only
        applicable structural hints are considered; explicit rules take
        precedence, and neither schema defaults nor scalar casts are applied.
        """
        from pyxsd.schemaless.json_schema import SchemaGuidance

        mapping = cls(root_name, rules=rules)
        mapping._guidance = SchemaGuidance.compile(
            schema,
            base_uri=base_uri,
            reference_registry=reference_registry,
            resource_policy=resource_policy,
        )
        return mapping

    @classmethod
    def from_openapi(
        cls,
        schema_object: Mapping[str, object] | bool,
        *,
        root_name: str | None = None,
        rules: Mapping[str, MappingRule] | None = None,
        base_uri: str | None = None,
        reference_registry: SchemaRegistry | None = None,
        resource_policy: ResourcePolicy | None = None,
        dialect: str | None = None,
    ) -> XmlMapping:
        """Compile an element-first mapping from an OpenAPI Schema Object.

        XML Objects on the selected schema and the properties, items, and
        prefix items it names directly supply node kinds, names, namespaces,
        and array wrapping. ``root_name`` takes precedence over ``xml.name``
        for the document element. Explicit rules override individual metadata
        fields, and the schema still validates the instance before projection.
        """
        from pyxsd.schemaless.openapi import compile_openapi_mapping

        guidance, selected, rule_at = compile_openapi_mapping(
            schema_object,
            root_name=root_name,
            dialect=dialect,
            base_uri=base_uri,
            reference_registry=reference_registry,
            resource_policy=resource_policy,
        )
        mapping = cls(selected, rules=rules)
        mapping._derived = rule_at
        mapping._guidance = guidance
        return mapping

    def _rule_at(
        self, pointer: str, value: NativeJsonValue, *, item_name_explicit: bool = False
    ) -> tuple[MappingRule, tuple[OpenApiMetadataWarning, ...]]:
        """Return policy and nonfatal metadata warnings for one occurrence."""
        explicit = self._rules.get(pointer)
        if self._derived is None:
            return (_DEFAULT_RULE if explicit is None else explicit), ()
        derived, warnings = self._derived(pointer, value, explicit, item_name_explicit)
        return (_DEFAULT_RULE if derived is None else derived), warnings


def _pointer_problem(pointer: str) -> str:
    """Return why *pointer* is not RFC 6901 syntax, or the empty string."""
    if pointer and not pointer.startswith("/"):
        return "must be empty or start with '/'"
    if any(_POINTER_BAD_ESCAPE.search(raw) for raw in pointer.split("/")[1:]):
        return "contains an invalid escape"
    return ""


def _split_name(name: str) -> tuple[str, str]:
    """Split a Clark expanded name into its namespace URI and local part."""
    if name.startswith("{"):
        uri, separator, local = name[1:].partition("}")
        if not separator:
            raise ValueError("expanded name is missing its closing '}'")
        return uri, local
    return "", name


def _is_xml_ncname(name: str) -> bool:
    """Return whether Expat accepts *name* as exactly one element name.

    Expat is the project's XML parser, so its name grammar is authoritative.
    The probe compares the received start-element name with the input, which
    rejects names that merely rearrange the synthesized document instead of
    naming its only element. Invalid UTF-8 encodings (for example an unpaired
    surrogate) are rejected by the same path.
    """
    if not name or ":" in name:
        return False
    seen: list[str] = []

    def start(received: str, attributes: dict[str, str]) -> None:
        seen.append(received)

    parser = expat.ParserCreate()
    parser.StartElementHandler = start
    try:
        parser.Parse(f"<{name}/>".encode(), True)
    except (expat.ExpatError, UnicodeEncodeError):
        return False
    return seen == [name]


def _is_xml_char(code: int) -> bool:
    """Whether *code* is an XML 1.0 ``Char``."""
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def _is_namespace_name(uri: str) -> bool:
    """Whether *uri* can be used literally as an element namespace name.

    A namespace name is compared literally, so ASCII characters that a URI
    reference must percent-encode cannot appear in it. Non-ASCII IRI
    characters are allowed when they are XML characters. Structural URI
    validation reuses the project's XSD ``anyURI`` discipline.
    """
    for character in uri:
        code = ord(character)
        if code <= 0x7F:
            if character in _NS_FORBIDDEN_ASCII:
                return False
        elif not _is_xml_char(code):
            return False
    from pyxsd.relaxng.uris import is_uri_reference

    return is_uri_reference(uri)


def _check_xml_name(name: str, pointer: str, xml_path: str, source: str | None) -> tuple[str, str]:
    """Reject an invalid explicit local or Clark expanded name; never repair it."""
    try:
        uri, local = _split_name(name)
    except ValueError:
        _fail(
            "invalid-xml-name",
            f"the XML name {name!r} is not valid",
            pointer,
            xml_path,
            source,
        )
    if uri and (uri == _XMLNS_NS or not _is_namespace_name(uri)):
        _fail(
            "invalid-xml-name",
            f"the XML name {name!r} has an unusable namespace name {uri!r}",
            pointer,
            xml_path,
            source,
        )
    if not _is_xml_ncname(local):
        _fail(
            "invalid-xml-name",
            f"the XML name {name!r} is not valid",
            pointer,
            xml_path,
            source,
        )
    return uri, local


def _check_local_name(name: str, pointer: str, xml_path: str, source: str | None) -> None:
    """Reject a data-derived object key that is not a local XML name.

    Only explicit mapping names may be Clark expanded names. A JSON object key
    is data, not namespace policy, so `{urn:x}id` is an invalid key rather than
    a namespaced element.
    """
    if not _is_xml_ncname(name):
        _fail(
            "invalid-xml-name",
            f"the object member name {name!r} is not a valid local XML name",
            pointer,
            xml_path,
            source,
        )


def _check_xml_text(text: str, pointer: str, xml_path: str, source: str) -> None:
    """Reject a JSON string XML 1.0 cannot represent as character data."""
    for character in text:
        code = ord(character)
        if not _is_xml_char(code):
            marker = f"U+{code:04X}"
            _fail(
                "invalid-xml-text",
                f"the JSON string at pointer {pointer!r} contains {marker}, "
                "which XML 1.0 cannot represent",
                pointer,
                xml_path,
                source,
                character=marker,
            )


def _fail(
    code: str,
    message: str,
    pointer: str,
    xml_path: str,
    source: str | None,
    **details: object,
) -> NoReturn:
    """Raise one fatal mapping diagnostic with both addresses."""
    report = ValidationReport(diagnostics_complete=True)
    report.add_error(
        message,
        code=code,
        location=DataLocation(source, pointer),
        location_precision="exact",
        details={"json_pointer": pointer, "xml_path": xml_path, **details},
    )
    raise XmlMappingError(report)


@dataclass(slots=True)
class _Target:
    """An emitted element and the occurrence state shared by inlined children."""

    element: ET.Element
    path: str
    counts: dict[str, int] = field(default_factory=dict)
    origins: dict[str, set[str]] = field(default_factory=dict)

    def next_path(self, name: str, kind: str = "element") -> str:
        if kind == "attribute":
            return f"{self.path}/@{name}"
        return f"{self.path}/{name}[{self.counts.get(name, 0) + 1}]"


@dataclass(frozen=True, slots=True)
class _Name:
    """A candidate XML name plus whether it comes from policy or a JSON key."""

    value: str
    explicit: bool = True
    prefix: str | None = None


_ITEM = _Name("item")


class _Projector:
    """Apply one immutable mapping, tracking XML and JSON addresses separately."""

    def __init__(
        self,
        mapping: XmlMapping,
        source: str,
        report: ValidationReport,
        schema: _AppliedSchema | None,
    ) -> None:
        self._mapping = mapping
        self._source = source
        self._report = report
        self._schema = schema
        self._bindings: dict[ET.Element, dict[str, str]] = {}
        self._uses_xsi = False
        self._document_value: NativeJsonValue = None
        self._reported_metadata: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()

    def build(self, value: NativeJsonValue) -> SchemalessElement:
        self._document_value = value
        rule, warnings = self._mapping._rule_at("", value)
        name, path = self._resolve_name(_Name(self._mapping.root_name), rule, "", None, "element")
        self._check_rule(rule, value, "element" if rule.kind is None else rule.kind, "", path)
        self._emit_metadata_warnings(warnings, "", path)
        if rule.kind not in (None, "element") or rule.array == "unwrapped":
            self._invalid("the root must be a single element", "", path)
        if value is None and rule.null == "omit":
            self._invalid("the root element cannot be omitted", "", path)
        root = ET.Element(name.value)
        self._bind(root, name, "", path)
        self._fill(_Target(root, path), rule, value, "")
        return SchemalessElement.from_etree(root, namespaces=self._namespace_context(root))

    def _namespace_context(self, root: ET.Element) -> NamespaceContext | None:
        if self._uses_xsi:
            self._bindings.setdefault(root, {}).setdefault("xsi", XSI_NS)
        if not self._bindings:
            return None
        context = NamespaceContext()
        stack: list[tuple[ET.Element, dict[str, str]]] = [(root, {})]
        while stack:
            node, inherited = stack.pop()
            frame = {**inherited, **self._bindings.get(node, {})}
            if frame.get("") and not namespace_of(node.tag):
                frame[""] = ""
            context.record(node, frame)
            stack.extend((child, frame) for child in reversed(node))
        return context

    def _fill(
        self, target: _Target, rule: MappingRule, value: NativeJsonValue, pointer: str
    ) -> None:
        if isinstance(value, (dict, list)):
            self._children(target, rule, value, pointer)
        elif value is None:
            self._check_null_shape(pointer, target.path)
            if rule.null == "empty":
                self._warn_null("empty", "element", pointer, target.path)
                target.element.text = ""
            else:
                target.element.set(_XSI_NIL, "true")
                self._uses_xsi = True
        else:
            target.element.text = self._lexical(value, rule, pointer, target.path)

    def _check_null_shape(self, pointer: str, path: str) -> None:
        """Reject incompatible structural hints where the instance has no shape.

        Native non-null values already determine the element-first layout.
        Nullable type assertions can name an underlying shape, but neither
        null alone nor an unconstrained schema asserts one. Scalar types all
        share the same XML structure.
        """
        if self._schema is None or pointer in self._mapping.rules:
            return
        hints: list[set[str]] = []
        pointers: list[str] = []
        sources: list[str | None] = []
        for occurrence in self._schema.occurrences_at(pointer):
            if isinstance(occurrence.schema, bool):
                continue
            declared = occurrence.schema.get("type", ())
            types = (declared,) if isinstance(declared, str) else declared
            shapes = {
                kind if kind in ("object", "array") else "scalar"
                for kind in types
                if kind != "null"
            }
            if shapes:
                hints.append(shapes)
                pointers.append(occurrence.schema_pointer)
                sources.append(occurrence._schema_source)
        if hints and not set.intersection(*hints):
            _fail(
                "ambiguous-schema-mapping",
                "ambiguous JSON Schema shapes for null; supply an explicit rule at this pointer",
                pointer,
                path,
                self._source,
                schema_pointers=tuple(pointers),
                schema_sources=tuple(sources),
                schema_shapes=tuple(tuple(sorted(shapes)) for shapes in hints),
            )

    def _children(
        self,
        target: _Target,
        rule: MappingRule,
        value: dict[str, NativeJsonValue] | list[NativeJsonValue],
        pointer: str,
        item_default: _Name = _ITEM,
    ) -> None:
        if isinstance(value, dict):
            if not value:
                self._warn_empty("object", pointer, target.path)
            # dict.fromkeys keeps each listed key at its first position.
            for key in dict.fromkeys((*(rule.order or ()), *value)):
                if key in value:
                    member_pointer = f"{pointer}/{escape_pointer_segment(key)}"
                    self._emit(
                        target,
                        _Name(key, explicit=False),
                        value[key],
                        member_pointer,
                        member_pointer,
                    )
            return

        item = item_default
        if rule.item_name is not None:
            item_path = target.next_path(rule.item_name)
            uri, local = _check_xml_name(rule.item_name, pointer, item_path, self._source)
            if rule.item_name.startswith("{"):
                item = _Name(clark(uri, local))
            else:
                item = _Name(
                    clark(namespace_of(item_default.value), local), prefix=item_default.prefix
                )
        if not value:
            self._warn_empty("array", pointer, target.path)
        for index, member in enumerate(value):
            self._emit(
                target,
                item,
                member,
                f"{pointer}/{index}",
                pointer,
                item_name_explicit=rule.item_name is not None,
            )

    def _emit(
        self,
        parent: _Target,
        default: _Name,
        value: NativeJsonValue,
        pointer: str,
        origin: str,
        *,
        item_name_explicit: bool = False,
    ) -> None:
        rule, warnings = self._mapping._rule_at(
            pointer, self._document_value, item_name_explicit=item_name_explicit
        )
        kind = rule.kind
        if kind is None:
            compact_property = self._mapping.default == "compact" and not default.explicit
            kind = (
                "attribute"
                if compact_property and not isinstance(value, (dict, list))
                else "element"
            )
        if kind in ("text", "none"):
            path = f"{parent.path}/text()" if kind == "text" else parent.path
        else:
            name, path = self._resolve_name(default, rule, pointer, parent, kind)
        self._check_rule(rule, value, kind, pointer, path)
        self._emit_metadata_warnings(warnings, pointer, path)

        if kind == "none":
            if isinstance(value, (dict, list)):
                self._children(parent, rule, value, pointer)
            else:
                self._warn("unmapped-scalar", "the JSON scalar has no XML node", pointer, path)
            return
        if kind == "text":
            self._text(parent, rule, value, pointer, path)
            return
        if kind == "attribute":
            self._attribute(parent, name, rule, value, pointer, path)
            return
        if rule.array == "unwrapped" and isinstance(value, list):
            self._children(parent, rule, value, pointer, name)
            return
        if value is None and rule.null == "omit":
            self._warn_null("omit", kind, pointer, path)
            return
        child = ET.SubElement(parent.element, name.value)
        parent.counts[name.value] = parent.counts.get(name.value, 0) + 1
        origins = parent.origins.setdefault(name.value, set())
        if origins and origin not in origins:
            self._warn(
                "repeated-element-name",
                "distinct JSON members project to sibling elements with the same name",
                pointer,
                path,
            )
        origins.add(origin)
        self._bind(child, name, pointer, path)
        self._fill(_Target(child, path), rule, value, pointer)

    def _resolve_name(
        self, default: _Name, rule: MappingRule, pointer: str, parent: _Target | None, kind: str
    ) -> tuple[_Name, str]:
        name = default if rule.name is None else _Name(rule.name)
        original = name.value
        expanded = name.explicit and original.startswith("{")
        if not name.explicit:
            assert parent is not None  # Only object properties get names from data.
            _check_local_name(original, pointer, parent.next_path(original, kind), self._source)
        # Normalize the no-namespace Clark spelling before counting or comparing
        # names; the writer emits it identically to the local spelling.
        value = original.removeprefix("{}") if expanded else original
        if rule.namespace and not expanded:
            value = clark(rule.namespace, value)
        path = f"/{value}[1]" if parent is None else parent.next_path(value, kind)
        if name.explicit:
            uri, local = _check_xml_name(original, pointer, path, self._source)
        else:
            uri, local = "", original
        if rule.namespace is not None:
            if expanded and uri != rule.namespace:
                self._invalid("name and namespace specify different namespace names", pointer, path)
            uri = rule.namespace
            if uri and (uri == _XMLNS_NS or not _is_namespace_name(uri)):
                _fail(
                    "invalid-xml-name",
                    f"unusable namespace name {uri!r}",
                    pointer,
                    path,
                    self._source,
                )
        if kind == "attribute" and not uri and local == "xmlns":
            _fail(
                "invalid-xml-name", "xmlns cannot be a data attribute", pointer, path, self._source
            )
        prefix = rule.prefix if rule.prefix is not None else name.prefix
        if prefix is not None:
            if prefix == "xmlns" or (prefix and not _is_xml_ncname(prefix)):
                self._invalid(f"invalid namespace prefix {prefix!r}", pointer, path)
            if not uri or (prefix == "xml") != (uri == XML_NS):
                self._invalid(f"prefix {prefix!r} cannot bind namespace {uri!r}", pointer, path)
            if kind == "attribute" and not prefix:
                self._invalid("an attribute cannot use a default namespace binding", pointer, path)
        return _Name(clark(uri, local), prefix=prefix), path

    def _check_rule(
        self, rule: MappingRule, value: NativeJsonValue, kind: str, pointer: str, path: str
    ) -> None:
        if kind not in ("element", "attribute", "text", "none"):
            self._invalid(f"unsupported node kind {kind!r}", pointer, path)
        if rule.array not in (None, "wrapped", "unwrapped"):
            self._invalid(f"unsupported array policy {rule.array!r}", pointer, path)
        if rule.null not in (None, "omit", "empty", "nil"):
            self._invalid(f"unsupported null policy {rule.null!r}", pointer, path)
        if kind in ("text", "none") and any(
            option is not None for option in (rule.name, rule.namespace, rule.prefix)
        ):
            self._invalid(f"{kind!r} has no XML name or namespace binding", pointer, path)
        if kind == "none" and (
            rule.array == "wrapped" or rule.type is not None or rule.null is not None
        ):
            self._invalid("'none' cannot wrap, format or emit a value of its own", pointer, path)
        if kind in ("attribute", "text"):
            if isinstance(value, (dict, list)):
                self._invalid(f"{kind!r} requires a scalar value", pointer, path)
            if rule.null == "nil":
                self._invalid("only an element can carry xsi:nil", pointer, path)
            if rule.array is not None or rule.item_name is not None:
                self._invalid(f"{kind!r} cannot configure array items", pointer, path)
        if (rule.array is not None or rule.item_name is not None) and not (
            value is None or isinstance(value, list)
        ):
            self._invalid("array and item_name require an array", pointer, path)
        if rule.type is not None and isinstance(value, (dict, list)):
            self._invalid("type requires a scalar value", pointer, path)
        if rule.order is not None:
            if value is not None and not isinstance(value, dict):
                self._invalid("order requires an object", pointer, path)
            if len(set(rule.order)) != len(rule.order):
                self._invalid("order cannot list a property twice", pointer, path)

    def _bind(self, element: ET.Element, name: _Name, pointer: str, path: str) -> None:
        if name.prefix is None:
            return
        uri = namespace_of(name.value)
        assert uri is not None  # _resolve_name requires a namespace for every explicit prefix.
        bindings = self._bindings.setdefault(element, {})
        if bindings.setdefault(name.prefix, uri) != uri:
            self._invalid(
                f"prefix {name.prefix!r} has conflicting bindings on one element", pointer, path
            )

    def _lexical(self, value: _Scalar, rule: MappingRule, pointer: str, path: str) -> str:
        if isinstance(value, str):
            _check_xml_text(value, pointer, path, self._source)
        if rule.type is None:
            return value if isinstance(value, str) else encode_json(value)
        try:
            adapter = TypePolicy.resolve(rule.type)
            lexical = adapter.format(_typed_scalar(self._source, pointer, adapter, value))
            if not isinstance(lexical, str):
                raise TypeError("the scalar formatter did not return a string")
        except (ScalarTypeError, *_PARSE_ERRORS):
            _fail(
                "invalid-mapping-type",
                "the rule could not format the scalar",
                pointer,
                path,
                self._source,
            )
        _check_xml_text(lexical, pointer, path, self._source)
        return lexical

    def _scalar_content(
        self, rule: MappingRule, value: NativeJsonValue, kind: str, pointer: str, path: str
    ) -> str | None:
        if value is None:
            policy = "empty" if rule.null == "empty" else "omit"
            self._warn_null(policy, kind, pointer, path)
            return "" if policy == "empty" else None
        assert not isinstance(value, (dict, list))  # _check_rule requires a scalar.
        return self._lexical(value, rule, pointer, path)

    def _attribute(
        self,
        parent: _Target,
        name: _Name,
        rule: MappingRule,
        value: NativeJsonValue,
        pointer: str,
        path: str,
    ) -> None:
        lexical = self._scalar_content(rule, value, "attribute", pointer, path)
        if lexical is None:
            return
        if name.value in parent.element.attrib:
            _fail(
                "duplicate-attribute",
                "two values map to one attribute",
                pointer,
                path,
                self._source,
            )
        self._bind(parent.element, name, pointer, path)
        parent.element.set(name.value, lexical)

    def _text(
        self, parent: _Target, rule: MappingRule, value: NativeJsonValue, pointer: str, path: str
    ) -> None:
        lexical = self._scalar_content(rule, value, "text", pointer, path)
        if lexical is None:
            return
        node = parent.element
        slot = node[-1].tail if len(node) else node.text
        if slot is not None:
            _fail("ambiguous-text", "adjacent text values would merge", pointer, path, self._source)
        if len(node):
            node[-1].tail = lexical
        else:
            node.text = lexical

    def _invalid(self, message: str, pointer: str, path: str) -> NoReturn:
        _fail("invalid-mapping-rule", message, pointer, path, self._source)

    def _warn_null(self, policy: str, kind: str, pointer: str, path: str) -> None:
        if policy == "empty":
            code = "null-emptied"
            message = f"null projects as empty {kind} content and is not distinguishable from empty content"
        else:
            code = "null-attribute-omitted" if kind == "attribute" else "null-omitted"
            message = f"omitted null {kind} is not distinguishable from a missing value"
        self._warn(code, message, pointer, path)

    def _warn_empty(self, kind: str, pointer: str, path: str) -> None:
        self._warn(
            f"empty-{kind}",
            f"an empty JSON {kind} has no XML children, so the container kind is not recoverable",
            pointer,
            path,
            json_kind=kind,
        )

    def _emit_metadata_warnings(
        self, warnings: tuple[OpenApiMetadataWarning, ...], pointer: str, path: str
    ) -> None:
        for warning in warnings:
            identity = (warning.schema_pointers, warning.constructs)
            if identity in self._reported_metadata:
                continue
            self._reported_metadata.add(identity)
            self._warn(
                "openapi-metadata-unresolved",
                warning.message,
                pointer,
                path,
                schema_pointers=warning.schema_pointers,
                constructs=warning.constructs,
            )

    def _warn(self, code: str, message: str, pointer: str, path: str, **details: object) -> None:
        self._report.add_warning(
            message,
            code=code,
            location=DataLocation(self._source, pointer),
            location_precision="exact",
            details={
                "json_pointer": pointer,
                "xml_path": path,
                **details,
            },
        )


def _project_document(
    document: JsonDocument,
    mapping: XmlMapping,
    *,
    node_mapping: NodeMapping | None,
) -> SchemalessDocument:
    """Build one fresh XML document from a native value and its mapping."""
    if node_mapping is not None and not isinstance(node_mapping, NodeMapping):
        raise TypeError("node_mapping must be a NodeMapping or None")
    applied = None
    if mapping._guidance is not None:
        applied = document._applied_schema
        if applied is None or not mapping._guidance._matches(applied.guidance):
            validation, applied = mapping._guidance._evaluate(
                document._value, source=document.source
            )
            if applied is None:
                raise XmlMappingError(validation)
    report = ValidationReport(diagnostics_complete=True)
    root = _Projector(mapping, document.source, report, applied).build(document._value)
    compiled: _CompiledNodeMapping | None = None
    if node_mapping is not None:
        compiled = node_mapping._compiled
        if compiled.rules:
            root = compiled.apply(root)
        if compiled.pins:
            root.revision._set_types(TypePolicy.lexical()._with_default_pins(compiled.pins))
    return SchemalessDocument._from_caller_tree(root, report, node_mapping=compiled)


__all__ = ["MappingRule", "XmlMapping"]
