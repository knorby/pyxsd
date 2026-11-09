"""Strict JSON parsing and schema-guided ElementTree construction.

The importer never builds instance objects itself: it converts JSON data
into a plain :mod:`xml.etree.ElementTree` tree with Clark-expanded
names, lexical XSD scalar text, and recorded namespace frames, and then
hands that tree to :meth:`pyxsd.schema.Schema.parse` so the ordinary
binder owns defaults, ``xsi`` handling, datatype conversion, identity
constraints, and generated-class construction.

Conversion errors (malformed JSON, duplicate keys, wrong containers,
wrong scalar types, conflicting markers, ambiguous roots) raise before
any tree is bound; correctly shaped but lexically invalid content is
passed through and lands on the returned document's validation report.

``pyxsd-json`` uses one layout-selected shape per effective type. The
conventional formats keep their documented grouped or positional
structures: native ``pyxsd`` and Parker absorb the root, BadgerFish keeps
one root object with lexical names and ``@xmlns``, and JsonML nests
arrays in document order.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypeGuard
from xml.etree import ElementTree as ET

from pyxsd.data_conversion.contracts import (
    Fidelity,
    FormatName,
    JsonValue,
    json_pointer,
    raise_conversion,
    validate_fidelity,
    validate_format,
    warn_conversion,
)
from pyxsd.data_conversion.layouts import (
    Layout,
    LayoutPlan,
    _is_schema_base,
    _possible_element_names,
    plan_layout,
)
from pyxsd.data_conversion.names import eqname_to_expanded, expanded_to_eqname
from pyxsd.data_conversion.values import scalar_to_lexical
from pyxsd.exceptions import PyXSDError
from pyxsd.xsd_data_types import (
    AnyType,
    NCName,
    QName,
    TypeList,
    XsdList,
    _ListString,
)
from pyxsd.xsi import XSI_NAMESPACE, XSI_NIL, XSI_TYPE

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pyxsd.schema import Schema

#: Reserved attribute key carrying an explicit actual type (profile v1).
TYPE_KEY = "@xsi:type"

#: Reserved property key for simple content and the nil marker.
CONTENT_KEY = "#content"

#: BadgerFish namespace declarations.
XMLNS_KEY = "@xmlns"

#: BadgerFish and native direct-text key.
TEXT_KEY = "$"

_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
_NIL_ATTR = f"{{{XSI_NAMESPACE}}}nil"
_TYPE_ATTR = f"{{{XSI_NAMESPACE}}}type"
_MISSING = object()

_NIL_LEXICALS = ("true", "1")


def json_to_data(source: str | bytes | bytearray) -> JsonValue:
    """Parse JSON text or UTF-8 bytes into a JSON-compatible value.

    Duplicate object keys and the non-standard ``NaN``/``Infinity``
    constants are rejected as conversion errors, so the returned value
    always round-trips one-to-one with its JSON spelling. Any other
    source type raises ``TypeError``: paths and file objects are
    deliberately outside this interface.
    """
    if not isinstance(source, (str, bytes, bytearray)):
        raise TypeError(
            f"JSON source must be str, bytes, or bytearray, not {type(source).__name__}"
        )
    if isinstance(source, (bytes, bytearray)):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise_conversion("json-invalid", "", f"the JSON source is not valid UTF-8: {exc}")
    try:
        loaded: JsonValue = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ValueError as exc:
        raise_conversion("json-invalid", "", f"malformed JSON: {exc}")
    return loaded


def _reject_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    """``object_pairs_hook`` that refuses an object with a repeated key."""
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise_conversion(
                "json-duplicate-key",
                "",
                f"the object declares the duplicate key '{key}' more than once",
            )
        result[key] = value
    return result


def _reject_constant(name: str) -> JsonValue:
    """``parse_constant`` hook refusing ``NaN``/``Infinity`` tokens."""
    raise_conversion(
        "json-invalid",
        "",
        f"the non-standard numeric constant {name} is not valid JSON",
    )


def data_to_element(
    schema: Schema,
    data: JsonValue,
    *,
    format: FormatName = "pyxsd-json",
    element: str | None = None,
    namespaces: Mapping[str, str] | None = None,
    fidelity: Fidelity = "strict",
) -> ET.Element:
    """Build the Clark-named ElementTree root for *data* under a format.

    The returned tree is complete and self-describing: every element's
    inherited namespace frame (seeded from *namespaces*, extended by
    format declarations or generated bindings) has been recorded on
    ``schema.namespace_context``, so ``schema.parse(result)`` binds it
    exactly like a parsed document. An unknown format or fidelity mode
    raises ``ValueError`` before any conversion work.
    """
    requested_format = validate_format(format)
    requested_fidelity = validate_fidelity(fidelity)
    root_frame = dict(namespaces) if namespaces else {}
    if requested_format == "pyxsd-json":
        return _Builder(schema, root_frame).build(data, element)
    if requested_format == "pyxsd":
        return _GroupedBuilder(schema, root_frame, "pyxsd", requested_fidelity).build(data, element)
    if requested_format == "parker":
        return _GroupedBuilder(schema, root_frame, "parker", requested_fidelity).build(
            data, element
        )
    if requested_format == "badgerfish":
        return _BadgerfishBuilder(schema, root_frame, requested_fidelity).build(data, element)
    return _JsonmlBuilder(schema, root_frame, requested_fidelity).build(data, element)


def _repointed(exc: PyXSDError, path: str) -> PyXSDError:
    """Rebuild a nested conversion error with the importer's JSON path.

    Every helper reused from the contract modules raises with an empty
    pointer; this function is only sound while that invariant holds.
    """
    text = str(exc)
    code, _, rest = text.partition(" at ")
    detail = rest.partition("': ")[2]
    return PyXSDError(f"{code} at {path!r}: {detail}")


def _local_name(expanded: str) -> str:
    """The local part of a Clark name or a bare local name."""
    if expanded.startswith("{"):
        return expanded.split("}", 1)[1]
    return expanded


def _clark(uri: str, local: str) -> str:
    return f"{{{uri}}}{local}" if uri else local


def _valid_ncname(name: str) -> bool:
    return NCName._pattern.fullmatch(name) is not None


_NO_MARKER = object()


class _BuilderBase:
    """State and helpers shared by every import decoder."""

    def __init__(self, schema: Schema, root_frame: dict[str, str]) -> None:
        self.schema = schema
        self.root_frame = root_frame
        self._globals: dict[str, Any] | None = None
        self._attr_cache: dict[type, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Schema tables
    # ------------------------------------------------------------------

    def global_elements(self) -> dict[str, Any]:
        """The schema's global element declarations by expanded name."""
        if self._globals is None:
            table: dict[str, Any] = {}
            # The binder makes the same assumption: every compiled schema
            # generates the "schema" class that owns the root elements.
            for descriptor in self.schema.classes["schema"]()._getElements():
                table[descriptor.expandedInstanceName()] = descriptor
            self._globals = table
        return self._globals

    def _concrete_globals(self) -> list[Any]:
        return [
            descriptor
            for descriptor in self.global_elements().values()
            if not descriptor.isAbstract()
        ]

    @staticmethod
    def _declared_type(descriptor: Any) -> type | None:
        if descriptor is None:
            return None
        getter = getattr(descriptor, "getType", None)
        cls = getter() if callable(getter) else None
        return cls if isinstance(cls, type) else None

    @staticmethod
    def _undetermined(type_class: type) -> bool:
        """Whether the type leaves the content model open (anyType-like)."""
        return getattr(type_class, "_contentKind_", None) is None and (
            issubclass(type_class, AnyType) or _is_schema_base(type_class)
        )

    def _declared_attributes(self, type_class: type) -> dict[str, Any]:
        """Declared attribute descriptors by expanded instance name.

        Walks the MRO collecting each class's own attribute uses, least
        derived first, so a derived class's redeclaration shadows its
        base's -- the same collection rule as
        :meth:`pyxsd.schema_base.SchemaBase.descAttributes`.
        """
        cached = self._attr_cache.get(type_class)
        if cached is not None:
            return cached
        table: dict[str, Any] = {}
        for klass in type_class.__mro__:
            for key in klass.__dict__.get("_attributeNames_", ()):
                if key in table:
                    continue
                descriptor = klass.__dict__[key]
                table[descriptor.expandedInstanceName(is_attribute=True)] = descriptor
        self._attr_cache[type_class] = table
        return table

    def _resolve_type(self, expanded: str) -> type | None:
        """Resolve an expanded type name through the compiled components."""
        from pyxsd.element_representatives.element_representative import ElementRepresentative

        return ElementRepresentative.typeFromName(expanded, self.schema, warn=False)

    # ------------------------------------------------------------------
    # Expanded names and root selection
    # ------------------------------------------------------------------

    def _expanded(self, key: str, path: str) -> str:
        """Convert an EQName key to its Clark form, repointing failures."""
        try:
            return eqname_to_expanded(key)
        except PyXSDError as exc:
            raise _repointed(exc, path) from None

    def _confirm_selection(self, element: str | None, root_name: str, root_key: str) -> None:
        """Check an explicit ``element`` against the carried root name."""
        if element is None or element == root_name:
            return
        if element.startswith("Q{"):
            selected = self._expanded(element, "")
        elif element.startswith("{"):
            try:
                # A Clark spelling is validated through its EQName form.
                selected = eqname_to_expanded(expanded_to_eqname(element))
            except PyXSDError as exc:
                raise _repointed(exc, "") from None
        else:
            matches = [
                descriptor
                for descriptor in self.global_elements().values()
                if descriptor.name == element
            ]
            if len(matches) > 1:
                raise_conversion(
                    "name-ambiguous",
                    json_pointer([root_key]),
                    f"the local root name {element!r} matches more than one "
                    "global element declaration",
                )
            if len(matches) == 1:
                selected = matches[0].expandedInstanceName() or element
            else:
                selected = element
        if selected == root_name:
            return
        head = self.global_elements().get(selected)
        if head is not None and any(
            name == root_name for name, _declaration in _possible_element_names(head)
        ):
            return
        raise_conversion(
            "root-mismatch",
            json_pointer([root_key]),
            f"the carried root {root_name!r} does not match the selected root {selected!r}",
        )

    # ------------------------------------------------------------------
    # Attributes and shared content helpers
    # ------------------------------------------------------------------

    def _set_attribute(
        self,
        element: ET.Element,
        expanded: str,
        raw: JsonValue,
        attr_types: dict[str, Any],
        frame: dict[str, str],
        path: str,
    ) -> None:
        """Set one attribute from an already-expanded name."""
        if expanded in element.attrib:
            raise_conversion(
                "marker-conflict",
                path,
                f"the attribute {expanded!r} is declared in more than one attribute map",
            )
        descriptor = attr_types.get(expanded)
        if descriptor is None:
            element.attrib[expanded] = self._scalar_text(raw, None, frame, path)
            return
        datatype = self._declared_type(descriptor)
        element.attrib[expanded] = self._scalar_text(raw, datatype, frame, path)

    _scalar_text: Callable[[JsonValue, type | None, dict[str, str], str], str]

    @staticmethod
    def _append_text(element: ET.Element, text: str) -> None:
        """Place character data as leading text or the last child's tail."""
        children = list(element)
        if children:
            last = children[-1]
            last.tail = (last.tail or "") + text
        else:
            element.text = (element.text or "") + text


# ---------------------------------------------------------------------------
# ``pyxsd-json`` import
# ---------------------------------------------------------------------------


class _Builder(_BuilderBase):
    """One root-retaining ``pyxsd-json`` run."""

    def __init__(self, schema: Schema, root_frame: dict[str, str]) -> None:
        super().__init__(schema, root_frame)
        self._prefixes: dict[str, str] = {}
        self._counter = 0

    def build(self, data: JsonValue, element: str | None) -> ET.Element:
        """Validate the single-entry root object and build the tree."""
        if not isinstance(data, dict):
            raise_conversion(
                "container-invalid",
                "",
                f"the pyxsd-json root must be a single-entry object, not {type(data).__name__}",
            )
        if len(data) != 1:
            raise_conversion(
                "root-ambiguous",
                "",
                f"the pyxsd-json root object must hold exactly one entry, not {len(data)}",
            )
        root_key, root_value = next(iter(data.items()))
        root_name = self._expanded(root_key, "")
        self._confirm_selection(element, root_name, root_key)
        descriptor = self.global_elements().get(root_name)
        return self._element(root_name, root_value, descriptor, self.root_frame, (root_key,))

    # ------------------------------------------------------------------
    # Element decoding
    # ------------------------------------------------------------------

    def _element(
        self,
        name: str,
        value: JsonValue,
        descriptor: Any,
        parent_frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> ET.Element:
        """Decode one element value under its effective type's layout."""
        frame = dict(parent_frame)
        element = ET.Element(name)
        declared = self._declared_type(descriptor)
        marker = self._find_marker(value)
        type_class: type | None = declared
        if marker is not _NO_MARKER:
            expanded_type, lexical = self._marker_value(marker, frame, path)
            element.attrib[XSI_TYPE] = lexical
            resolved = self._resolve_type(expanded_type)
            if resolved is not None:
                type_class = resolved
        if type_class is None:
            type_class = AnyType
        if self._undetermined(type_class):
            self._decode_generic(element, value, frame, path)
        else:
            plan = plan_layout(type_class, explicit_type=marker is not _NO_MARKER)
            self._decode_layout(element, value, plan, type_class, frame, path)
        self.schema.namespace_context.record(element, frame)
        return element

    # ------------------------------------------------------------------
    # Markers and names
    # ------------------------------------------------------------------

    @staticmethod
    def _find_marker(value: JsonValue) -> Any:
        """The raw ``@xsi:type`` value, wherever the container keeps it.

        Object layouts keep it as a property; ordered arrays keep it in
        a leading attribute map. The scan peeks without consuming: the
        planned layout decides which maps become attributes. Returns
        ``_NO_MARKER`` when absent, so a JSON ``null`` marker is still
        reported as a scalar-type error.
        """
        if isinstance(value, dict):
            if TYPE_KEY in value:
                return value[TYPE_KEY]
            return _NO_MARKER
        if isinstance(value, list):
            for item in value:
                if not _is_attribute_map(item):
                    break
                if TYPE_KEY in item:
                    return item[TYPE_KEY]
        return _NO_MARKER

    def _marker_value(
        self, marker: JsonValue, frame: dict[str, str], path: tuple[str | int, ...]
    ) -> tuple[str, str]:
        """Validate ``@xsi:type`` and translate it to a lexical QName."""
        if not isinstance(marker, str):
            raise_conversion(
                "scalar-type-invalid",
                json_pointer((*path, TYPE_KEY)),
                f"expected an EQName string for {TYPE_KEY}, got {type(marker).__name__}",
            )
        expanded = self._expanded(marker, json_pointer((*path, TYPE_KEY)))
        return expanded, self._eqname_lexical(expanded, frame)

    def _eqname_lexical(self, expanded: str, frame: dict[str, str]) -> str:
        """Spell an expanded name as a lexical QName bound in *frame*."""
        if not expanded.startswith("{"):
            return expanded
        end = expanded.find("}")
        uri, local = expanded[1:end], expanded[end + 1 :]
        return f"{self._prefix_for(uri, frame)}:{local}"

    def _prefix_for(self, uri: str, frame: dict[str, str]) -> str:
        """A deterministic prefix for *uri*, preferring the inherited frame."""
        for prefix, bound in frame.items():
            if prefix and bound == uri:
                return prefix
        existing = self._prefixes.get(uri)
        if existing is not None:
            frame[existing] = uri
            return existing
        prefix = self._next_prefix(frame)
        self._prefixes[uri] = prefix
        frame[prefix] = uri
        return prefix

    def _next_prefix(self, frame: dict[str, str]) -> str:
        """Allocate the next generated prefix absent from *frame*."""
        prefix = f"ns{self._counter}"
        self._counter += 1
        while prefix in frame:
            prefix = f"ns{self._counter}"
            self._counter += 1
        return prefix

    # ------------------------------------------------------------------
    # Scalar conversion
    # ------------------------------------------------------------------

    def _scalar_text(
        self,
        value: JsonValue,
        datatype: type | None,
        frame: dict[str, str],
        path: str,
    ) -> str:
        """Convert one JSON scalar to XSD lexical text for *datatype*."""
        if datatype is None:
            if not isinstance(value, str):
                raise_conversion(
                    "scalar-type-invalid",
                    path,
                    f"expected a JSON string for untyped text, got {type(value).__name__}",
                )
            return value
        try:
            converted = self._translate_qnames(value, datatype, frame, path)
            return scalar_to_lexical(converted, datatype=datatype, format="pyxsd-json")
        except PyXSDError as exc:
            raise _repointed(exc, path) from None

    def _translate_qnames(
        self,
        value: JsonValue,
        datatype: type,
        frame: dict[str, str],
        path: str,
    ) -> JsonValue:
        """Translate EQName QName values to lexical QNames before binding."""
        if issubclass(datatype, (XsdList, TypeList)):
            item_type = datatype.itemType if issubclass(datatype, XsdList) else None
            if item_type is not None and isinstance(value, list):
                return [self._translate_qnames(item, item_type, frame, path) for item in value]
            return value
        if issubclass(datatype, QName) and isinstance(value, str) and value.startswith("Q{"):
            return self._eqname_lexical(self._expanded(value, path), frame)
        return value

    def _apply_attribute(
        self,
        element: ET.Element,
        key: str,
        raw: JsonValue,
        attr_types: dict[str, Any],
        frame: dict[str, str],
        path: str,
    ) -> None:
        """Set one attribute from an ``@``-keyed EQName entry."""
        self._set_attribute(element, self._expanded(key, path), raw, attr_types, frame, path)

    # ------------------------------------------------------------------
    # Layout decoding
    # ------------------------------------------------------------------

    def _decode_layout(
        self,
        element: ET.Element,
        value: JsonValue,
        plan: LayoutPlan,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        """Decode one value under its planned layout."""
        if value is None:
            element.attrib[XSI_NIL] = "true"
            return
        layout = plan.layout
        if layout is Layout.EMPTY:
            self._decode_empty(element, value, path)
        elif layout is Layout.EMPTY_PLUS:
            self._decode_object(element, value, type_class, frame, path)
        elif layout is Layout.SIMPLE:
            self._decode_simple(element, value, type_class, frame, path)
        elif layout is Layout.SIMPLE_PLUS:
            self._decode_simple_plus(element, value, type_class, frame, path)
        elif layout is Layout.LIST:
            self._decode_list(element, value, plan, frame, path)
        elif layout is Layout.LIST_PLUS:
            self._decode_list_plus(element, value, plan, type_class, frame, path)
        elif layout is Layout.RECORD:
            self._decode_record(element, value, plan, type_class, frame, path)
        else:
            self._decode_items(element, value, plan, type_class, frame, path)

    def _decode_empty(
        self, element: ET.Element, value: JsonValue, path: tuple[str | int, ...]
    ) -> None:
        if not isinstance(value, str) or value != "":
            raise_conversion(
                "container-invalid",
                json_pointer(path),
                f'empty content for {element.tag!r} is the empty string "", '
                f"not {type(value).__name__}",
            )

    def _decode_simple(
        self,
        element: ET.Element,
        value: JsonValue,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        if isinstance(value, dict):
            raise_conversion(
                "container-invalid",
                json_pointer(path),
                f"simple content for {element.tag!r} has no attribute container",
            )
        # A simple layout is planned only for types that derive from
        # their content datatype (builtin datatypes, and simple-content
        # complex types extend the datatype), so the class itself is
        # the governing datatype.
        lexical = self._scalar_text(value, type_class, frame, json_pointer(path))
        element.text = lexical or None

    def _decode_simple_plus(
        self,
        element: ET.Element,
        value: JsonValue,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        if not isinstance(value, dict):
            raise_conversion(
                "container-invalid",
                json_pointer(path),
                f"simple content with attributes for {element.tag!r} requires an attribute object",
            )
        attr_types = self._declared_attributes(type_class)
        content_seen = False
        nilled = False
        for key, raw in value.items():
            if key == TYPE_KEY:
                # The marker was resolved before the layout was chosen.
                continue
            if key.startswith("@"):
                self._apply_attribute(
                    element, key[1:], raw, attr_types, frame, json_pointer((*path, key))
                )
                continue
            if key == CONTENT_KEY:
                if raw is None:
                    if content_seen:
                        raise_conversion(
                            "marker-conflict",
                            json_pointer((*path, key)),
                            "a nilled element may carry attributes but no text or child content",
                        )
                    element.attrib[XSI_NIL] = "true"
                    nilled = True
                    continue
                lexical = self._scalar_text(raw, type_class, frame, json_pointer(path))
                element.text = lexical or None
                content_seen = True
                continue
            if nilled:
                raise_conversion(
                    "marker-conflict",
                    json_pointer((*path, key)),
                    "a nilled element may carry attributes but no text or child content",
                )
            self._append_child(element, key, raw, frame, (*path, key))
            content_seen = True

    def _decode_list(
        self,
        element: ET.Element,
        value: JsonValue,
        plan: LayoutPlan,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        if not isinstance(value, list):
            raise_conversion(
                "container-invalid",
                json_pointer(path),
                f"list content for {element.tag!r} requires an array of child values",
            )
        child = plan.children[0]
        for index, item in enumerate(value):
            element.append(
                self._element(child.name, item, child.declaration, frame, (*path, index))
            )

    def _decode_list_plus(
        self,
        element: ET.Element,
        value: JsonValue,
        plan: LayoutPlan,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        if not isinstance(value, dict):
            raise_conversion(
                "container-invalid",
                json_pointer(path),
                f"list content with attributes for {element.tag!r} requires an attribute object",
            )
        attr_types = self._declared_attributes(type_class)
        child = plan.children[0]
        child_key = expanded_to_eqname(child.name)
        items: list[JsonValue] = []
        seen_child = False
        nilled = False
        for key, raw in value.items():
            if key == TYPE_KEY:
                # The marker was resolved before the layout was chosen.
                continue
            if key.startswith("@"):
                self._apply_attribute(
                    element, key[1:], raw, attr_types, frame, json_pointer((*path, key))
                )
                continue
            if key == CONTENT_KEY:
                if raw is not None:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, key)),
                        f"{CONTENT_KEY} in list content must be the null marker",
                    )
                if seen_child:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, key)),
                        "a nilled element may carry attributes but no text or child content",
                    )
                element.attrib[XSI_NIL] = "true"
                nilled = True
                continue
            if key == child_key:
                if not isinstance(raw, list):
                    raise_conversion(
                        "container-invalid",
                        json_pointer((*path, key)),
                        f"the list child property {key!r} must be an array",
                    )
                if nilled:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, key)),
                        "a nilled element may carry attributes but no text or child content",
                    )
                items.extend(raw)
                seen_child = True
                continue
            if nilled:
                raise_conversion(
                    "marker-conflict",
                    json_pointer((*path, key)),
                    "a nilled element may carry attributes but no text or child content",
                )
            self._append_child(element, key, raw, frame, (*path, key))
        if not seen_child and not nilled:
            raise_conversion(
                "container-invalid",
                json_pointer((*path, child_key)),
                f'the list child property {child_key!r} is missing; an empty list uses "[]"',
            )
        for index, item in enumerate(items):
            element.append(
                self._element(child.name, item, child.declaration, frame, (*path, child_key, index))
            )

    def _decode_object(
        self,
        element: ET.Element,
        value: JsonValue,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        """Decode empty-plus content: attributes and the nil marker."""
        if not isinstance(value, dict):
            raise_conversion(
                "container-invalid",
                json_pointer(path),
                f"empty content with attributes for {element.tag!r} requires an attribute object",
            )
        attr_types = self._declared_attributes(type_class)
        content_seen = False
        nilled = False
        for key, raw in value.items():
            if key == TYPE_KEY:
                # The marker was resolved before the layout was chosen.
                continue
            if key.startswith("@"):
                self._apply_attribute(
                    element, key[1:], raw, attr_types, frame, json_pointer((*path, key))
                )
                continue
            if key == CONTENT_KEY:
                if raw is not None:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, key)),
                        f"{CONTENT_KEY} in empty content must be the null marker",
                    )
                if content_seen:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, key)),
                        "a nilled element may carry attributes but no text or child content",
                    )
                element.attrib[XSI_NIL] = "true"
                nilled = True
                continue
            if nilled:
                raise_conversion(
                    "marker-conflict",
                    json_pointer((*path, key)),
                    "a nilled element may carry attributes but no text or child content",
                )
            self._append_child(element, key, raw, frame, (*path, key))
            content_seen = True

    def _decode_record(
        self,
        element: ET.Element,
        value: JsonValue,
        plan: LayoutPlan,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        if not isinstance(value, dict):
            raise_conversion(
                "container-invalid",
                json_pointer(path),
                f"record content for {element.tag!r} requires an object",
            )
        attr_types = self._declared_attributes(type_class)
        entries: dict[str, tuple[str, JsonValue]] = {}
        content_seen = False
        nilled = False
        for key, raw in value.items():
            if key == TYPE_KEY:
                # The marker was resolved before the layout was chosen.
                continue
            if key.startswith("@"):
                self._apply_attribute(
                    element, key[1:], raw, attr_types, frame, json_pointer((*path, key))
                )
                continue
            if key == CONTENT_KEY:
                if raw is not None:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, key)),
                        f"{CONTENT_KEY} in record content must be the null marker",
                    )
                if content_seen:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, key)),
                        "a nilled element may carry attributes but no text or child content",
                    )
                element.attrib[XSI_NIL] = "true"
                nilled = True
                continue
            if nilled:
                raise_conversion(
                    "marker-conflict",
                    json_pointer((*path, key)),
                    "a nilled element may carry attributes but no text or child content",
                )
            expanded = self._expanded(key, json_pointer((*path, key)))
            if expanded in entries:
                raise_conversion(
                    "json-duplicate-key",
                    json_pointer((*path, key)),
                    f"the record declares the expanded name {expanded!r} through "
                    f"more than one key ({entries[expanded][0]!r} and {key!r})",
                )
            entries[expanded] = (key, raw)
            content_seen = True
        for child in plan.children:
            entry = entries.pop(child.name, None)
            if entry is None:
                continue
            key, raw = entry
            element.append(self._element(child.name, raw, child.declaration, frame, (*path, key)))
        for _expanded_name, (key, raw) in entries.items():
            self._append_child(element, key, raw, frame, (*path, key))

    def _decode_items(
        self,
        element: ET.Element,
        value: JsonValue,
        plan: LayoutPlan,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        """Decode sequence and mixed arrays in the supplied order."""
        if not isinstance(value, list):
            raise_conversion(
                "container-invalid",
                json_pointer(path),
                f"ordered content for {element.tag!r} requires an array",
            )
        attr_types = self._declared_attributes(type_class)
        mixed = plan.layout is Layout.MIXED
        content_started = False
        nilled = False
        marker_seen = False
        for index, item in enumerate(value):
            if nilled:
                raise_conversion(
                    "marker-conflict",
                    json_pointer((*path, index)),
                    "no content may follow the null marker",
                )
            if isinstance(item, str):
                if not item or (not mixed and not item.strip()):
                    continue
                self._append_text(element, item)
                content_started = True
                continue
            if not isinstance(item, dict) or not item:
                raise_conversion(
                    "container-invalid",
                    json_pointer((*path, index)),
                    "ordered content items must be strings or singleton child maps",
                )
            if all(key.startswith("@") for key in item):
                if content_started:
                    raise_conversion(
                        "container-invalid",
                        json_pointer((*path, index)),
                        "attribute maps must precede child content",
                    )
                for key, raw in item.items():
                    if key == TYPE_KEY:
                        if marker_seen:
                            raise_conversion(
                                "marker-conflict",
                                json_pointer((*path, index, key)),
                                f"{TYPE_KEY} appears in more than one attribute map",
                            )
                        marker_seen = True
                        continue
                    self._apply_attribute(
                        element,
                        key[1:],
                        raw,
                        attr_types,
                        frame,
                        json_pointer((*path, index, key)),
                    )
                continue
            if len(item) != 1:
                raise_conversion(
                    "container-invalid",
                    json_pointer((*path, index)),
                    "a child map must have exactly one entry",
                )
            key, raw = next(iter(item.items()))
            if key == CONTENT_KEY:
                if raw is not None:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, index, key)),
                        f"{CONTENT_KEY} in ordered content must be the null marker",
                    )
                if content_started:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, index, key)),
                        "the null marker may follow only attribute maps",
                    )
                element.attrib[XSI_NIL] = "true"
                nilled = True
                continue
            self._append_map_child(element, key, raw, plan, frame, (*path, index, key))
            content_started = True

    # ------------------------------------------------------------------
    # Generic (undetermined) decoding
    # ------------------------------------------------------------------

    def _decode_generic(
        self,
        element: ET.Element,
        value: JsonValue,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        """Decode a value whose governing type leaves the model open."""
        if value is None:
            element.attrib[XSI_NIL] = "true"
            return
        if isinstance(value, str):
            element.text = value or None
            return
        if isinstance(value, dict):
            self._decode_generic_object(element, value, frame, path)
            return
        if isinstance(value, list):
            self._decode_generic_items(element, value, frame, path)
            return
        raise_conversion(
            "scalar-type-invalid",
            json_pointer(path),
            f"expected a JSON string for untyped text, got {type(value).__name__}",
        )

    def _decode_generic_object(
        self,
        element: ET.Element,
        value: dict[str, JsonValue],
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        content_seen = False
        nilled = False
        for key, raw in value.items():
            if key == TYPE_KEY:
                # The marker was resolved before the layout was chosen.
                continue
            if key.startswith("@"):
                self._apply_attribute(element, key[1:], raw, {}, frame, json_pointer((*path, key)))
                continue
            if key == CONTENT_KEY:
                if raw is None:
                    if content_seen:
                        raise_conversion(
                            "marker-conflict",
                            json_pointer((*path, key)),
                            "a nilled element may carry attributes but no text or child content",
                        )
                    element.attrib[XSI_NIL] = "true"
                    nilled = True
                    continue
                element.text = (
                    self._scalar_text(raw, None, frame, json_pointer((*path, key))) or None
                )
                content_seen = True
                continue
            if nilled:
                raise_conversion(
                    "marker-conflict",
                    json_pointer((*path, key)),
                    "a nilled element may carry attributes but no text or child content",
                )
            self._append_child(element, key, raw, frame, (*path, key))
            content_seen = True

    def _decode_generic_items(
        self,
        element: ET.Element,
        value: list[JsonValue],
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        content_started = False
        nilled = False
        marker_seen = False
        for index, item in enumerate(value):
            if nilled:
                raise_conversion(
                    "marker-conflict",
                    json_pointer((*path, index)),
                    "no content may follow the null marker",
                )
            if isinstance(item, str):
                if not item:
                    continue
                self._append_text(element, item)
                content_started = True
                continue
            if not isinstance(item, dict) or not item:
                raise_conversion(
                    "container-invalid",
                    json_pointer((*path, index)),
                    "mixed content items must be strings or singleton child maps",
                )
            if all(key.startswith("@") for key in item):
                if content_started:
                    raise_conversion(
                        "container-invalid",
                        json_pointer((*path, index)),
                        "attribute maps must precede child content",
                    )
                for key, raw in item.items():
                    if key == TYPE_KEY:
                        if marker_seen:
                            raise_conversion(
                                "marker-conflict",
                                json_pointer((*path, index, key)),
                                f"{TYPE_KEY} appears in more than one attribute map",
                            )
                        marker_seen = True
                        continue
                    self._apply_attribute(
                        element,
                        key[1:],
                        raw,
                        {},
                        frame,
                        json_pointer((*path, index, key)),
                    )
                continue
            if len(item) != 1:
                raise_conversion(
                    "container-invalid",
                    json_pointer((*path, index)),
                    "a child map must have exactly one entry",
                )
            key, raw = next(iter(item.items()))
            if key == CONTENT_KEY:
                if raw is not None:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, index, key)),
                        f"{CONTENT_KEY} in mixed content must be the null marker",
                    )
                if content_started:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, index, key)),
                        "the null marker may follow only attribute maps",
                    )
                element.attrib[XSI_NIL] = "true"
                nilled = True
                continue
            self._append_child(element, key, raw, frame, (*path, index, key))
            content_started = True

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _append_map_child(
        self,
        element: ET.Element,
        key: str,
        raw: JsonValue,
        plan: LayoutPlan,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        """Append one child-map entry, matching the plan or a global."""
        expanded = self._expanded(key, json_pointer(path))
        declaration = None
        for child in plan.children:
            if child.name == expanded:
                declaration = child.declaration
                break
        else:
            declaration = self.global_elements().get(expanded)
        element.append(self._element(expanded, raw, declaration, frame, path))

    def _append_child(
        self,
        element: ET.Element,
        key: str,
        raw: JsonValue,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        """Build content the compiled model does not name; never drop it."""
        expanded = self._expanded(key, json_pointer(path))
        declaration = self.global_elements().get(expanded)
        element.append(self._element(expanded, raw, declaration, frame, path))


# ---------------------------------------------------------------------------
# Native and Parker import
# ---------------------------------------------------------------------------


class _GroupedBuilder(_BuilderBase):
    """One root-absorbed native or Parker import run."""

    def __init__(
        self,
        schema: Schema,
        root_frame: dict[str, str],
        format: FormatName,
        fidelity: Fidelity,
    ) -> None:
        super().__init__(schema, root_frame)
        self.format = format
        self.fidelity = fidelity

    # ------------------------------------------------------------------
    # Root selection
    # ------------------------------------------------------------------

    def build(self, data: JsonValue, element: str | None) -> ET.Element:
        """Select the absorbed root and build its element tree."""
        root_name, descriptor = self._select_root(element)
        if isinstance(data, dict):
            frame = dict(self.root_frame)
            return self._grouped_element(root_name, data, descriptor, frame, ())
        return self._grouped_element(root_name, data, descriptor, self.root_frame, ())

    def _select_root(self, element: str | None) -> tuple[str, Any]:
        """Resolve a root-absorbed format's single global root."""
        if element is not None:
            if element.startswith("Q{") or element.startswith("{"):
                expanded = self._select_expanded(element)
                return expanded, self.global_elements().get(expanded)
            self._validate_local(element, "")
            matches = [
                descriptor
                for descriptor in self.global_elements().values()
                if descriptor.name == element
            ]
            if len(matches) > 1:
                raise_conversion(
                    "name-ambiguous",
                    "",
                    f"the local root name {element!r} matches more than one "
                    "global element declaration",
                )
            if len(matches) == 1:
                return matches[0].expandedInstanceName() or element, matches[0]
            return element, self.global_elements().get(element)
        candidates = self._concrete_globals()
        if len(candidates) != 1:
            raise_conversion(
                "root-ambiguous",
                "",
                "a root-absorbed format needs an explicit element when the schema "
                f"has {len(candidates)} possible global roots",
            )
        descriptor = candidates[0]
        return descriptor.expandedInstanceName() or descriptor.name, descriptor

    def _select_expanded(self, element: str) -> str:
        """Parse a Clark or EQName root selection."""
        if element.startswith("Q{"):
            return self._expanded(element, "")
        try:
            return eqname_to_expanded(expanded_to_eqname(element))
        except PyXSDError as exc:
            raise _repointed(exc, "") from None

    # ------------------------------------------------------------------
    # Fallbacks and names
    # ------------------------------------------------------------------

    def _fallback(self, code: str, path: str, message: str) -> None:
        """Raise in strict mode, or warn and continue best-effort."""
        if self.fidelity == "strict":
            raise_conversion(code, path, message)
        warn_conversion(code, path, message)

    @staticmethod
    def _validate_local(name: str, path: str) -> None:
        """Require one XML local name (no prefix, no Clark braces)."""
        if not isinstance(name, str) or not _valid_ncname(name):
            raise_conversion("name-invalid", path, f"invalid local name {name!r}")

    def _native_name(self, key: str, path: str) -> str:
        """An element key as native spells it: Clark or local."""
        if key.startswith("{"):
            try:
                expanded_to_eqname(key)
            except PyXSDError as exc:
                raise _repointed(exc, path) from None
            return key
        self._validate_local(key, path)
        return key

    def _native_attribute_name(self, key: str, attr_types: dict[str, Any], path: str) -> str:
        """Resolve a native attribute key to an expanded name."""
        if key.startswith("{"):
            try:
                expanded_to_eqname(key)
            except PyXSDError as exc:
                raise _repointed(exc, path) from None
            return key
        self._validate_local(key, path)
        matches = [expanded for expanded in attr_types if _local_name(expanded) == key]
        if len(matches) > 1:
            raise_conversion(
                "name-ambiguous",
                path,
                f"the local attribute name {key!r} matches more than one declaration",
            )
        return matches[0] if matches else key

    def _native_child_spec(self, key: str, plan: LayoutPlan | None, path: str) -> tuple[str, Any]:
        """Resolve one native child key to its expanded name."""
        expanded = self._native_name(key, path)
        if expanded.startswith("{"):
            declaration = self._plan_declaration(plan, expanded)
            if declaration is None:
                declaration = self.global_elements().get(expanded)
            return expanded, declaration
        declared = (
            [child for child in plan.children if _local_name(child.name) == key]
            if plan is not None
            else []
        )
        if len(declared) > 1:
            raise_conversion(
                "name-ambiguous",
                path,
                f"the local child name {key!r} matches more than one declaration",
            )
        if len(declared) == 1:
            return declared[0].name, declared[0].declaration
        globals_matching = [
            descriptor for descriptor in self._concrete_globals() if descriptor.name == key
        ]
        if len(globals_matching) > 1:
            raise_conversion(
                "name-ambiguous",
                path,
                f"the local child name {key!r} matches more than one global element",
            )
        if len(globals_matching) == 1:
            descriptor = globals_matching[0]
            return descriptor.expandedInstanceName() or key, descriptor
        return key, self.global_elements().get(key)

    def _parker_child_spec(self, key: str, plan: LayoutPlan | None, path: str) -> tuple[str, Any]:
        """Resolve a Parker local child name, applying its documented fallback."""
        self._validate_local(key, path)
        declared = (
            [child for child in plan.children if _local_name(child.name) == key]
            if plan is not None
            else []
        )
        if len(declared) == 1:
            return declared[0].name, declared[0].declaration
        globals_matching = (
            [descriptor for descriptor in self._concrete_globals() if descriptor.name == key]
            if not declared
            else []
        )
        if len(declared) <= 1 and len(globals_matching) == 1:
            descriptor = globals_matching[0]
            return descriptor.expandedInstanceName() or key, descriptor
        self._fallback(
            "name-ambiguous",
            path,
            f"Parker cannot resolve the local child name {key!r} uniquely",
        )
        return key, None

    @staticmethod
    def _plan_declaration(plan: LayoutPlan | None, expanded: str) -> Any:
        if plan is None:
            return None
        for child in plan.children:
            if child.name == expanded:
                return child.declaration
        return None

    # ------------------------------------------------------------------
    # Scalar conversion
    # ------------------------------------------------------------------

    def _scalar_text(
        self,
        value: JsonValue,
        datatype: type | None,
        frame: dict[str, str],
        path: str,
    ) -> str:
        """Convert one JSON scalar under the native or Parker policy."""
        if datatype is None:
            if not isinstance(value, str):
                raise_conversion(
                    "scalar-type-invalid",
                    path,
                    f"expected a JSON string for untyped text, got {type(value).__name__}",
                )
            return value
        try:
            lexical = scalar_to_lexical(value, datatype=datatype, format=self.format)
        except PyXSDError as exc:
            raise _repointed(exc, path) from None
        self._check_qname_tokens(lexical, datatype, frame, path)
        return lexical

    def _check_qname_tokens(
        self,
        lexical: str,
        datatype: type,
        frame: dict[str, str],
        path: str,
    ) -> None:
        """Report a lexical QName value whose prefix the frame cannot resolve."""
        for token in _qname_tokens(datatype, lexical):
            if ":" not in token:
                continue
            prefix = token.split(":", 1)[0]
            if prefix != "xml" and not frame.get(prefix):
                self._fallback(
                    "namespace-unresolved",
                    path,
                    f"the QName prefix {prefix!r} is not bound in scope",
                )

    # ------------------------------------------------------------------
    # Element decoding
    # ------------------------------------------------------------------

    def _grouped_element(
        self,
        name: str,
        value: JsonValue,
        descriptor: Any,
        parent_frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> ET.Element:
        """Decode one native or Parker element value."""
        frame = dict(parent_frame)
        element = ET.Element(name)
        declared = self._declared_type(descriptor)
        type_class: type | None = declared
        explicit = False
        nil_raw: Any = _MISSING
        nil_key: str | None = None

        if isinstance(value, dict) and self.format == "pyxsd":
            type_raw, nil_raw, nil_key = self._native_markers(value, frame, path)
            if type_raw is not _MISSING:
                explicit = True
                self._apply_native_type(element, type_raw, frame, path)
                resolved = self._native_type(type_raw, frame, path)
                if resolved is not None:
                    type_class = resolved

        if type_class is None:
            type_class = AnyType
        undetermined = self._undetermined(type_class)
        plan = None if undetermined else plan_layout(type_class, explicit_type=explicit)

        if value is None:
            if self.format == "parker" or self._native_null_is_nil(
                descriptor, type_class, plan, path
            ):
                element.attrib[XSI_NIL] = "true"
            self.schema.namespace_context.record(element, frame)
            return element

        if isinstance(value, dict):
            nilled = False
            if nil_raw is not _MISSING:
                nilled = self._apply_native_nil(element, nil_raw, path)
            self._decode_grouped_object(
                element,
                value,
                plan,
                type_class,
                frame,
                path,
                generic=undetermined,
                nilled=nilled,
                nil_key=nil_key,
            )
            self.schema.namespace_context.record(element, frame)
            return element

        # A scalar or scalar-list value is direct simple content.
        if undetermined:
            element.text = self._untyped_text(value, path) or None
        elif plan is not None and plan.layout in (Layout.SIMPLE, Layout.SIMPLE_PLUS):
            lexical = self._scalar_text(value, type_class, frame, json_pointer(path))
            element.text = lexical or None
        elif plan is not None and plan.layout is Layout.MIXED:
            if not isinstance(value, str):
                raise_conversion(
                    "scalar-type-invalid",
                    json_pointer(path),
                    f"expected a JSON string for mixed text, got {type(value).__name__}",
                )
            element.text = value or None
        elif isinstance(value, str):
            # Element-only content cannot carry text, but the format can:
            # pass it to the binder for its normal content report.
            element.text = value or None
        else:
            raise_conversion(
                "container-invalid",
                json_pointer(path),
                f"content for {element.tag!r} requires an object",
            )
        self.schema.namespace_context.record(element, frame)
        return element

    def _untyped_text(self, value: JsonValue, path: tuple[str | int, ...]) -> str:
        """Native compatibility text for a value whose type is undetermined."""
        if isinstance(value, str):
            return value
        if self.format == "pyxsd":
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (int, float)):
                return str(value)
        raise_conversion(
            "scalar-type-invalid",
            json_pointer(path),
            f"expected a JSON string for untyped text, got {type(value).__name__}",
        )

    def _decode_grouped_object(
        self,
        element: ET.Element,
        value: dict[str, JsonValue],
        plan: LayoutPlan | None,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
        *,
        generic: bool,
        nilled: bool,
        nil_key: str | None,
    ) -> None:
        """Decode a native or Parker grouped object."""
        attr_types = self._declared_attributes(type_class)
        text_raw: Any = _MISSING
        text_key: str | None = None
        entries: list[tuple[str, JsonValue]] = []

        mixed = generic or (plan is not None and plan.layout is Layout.MIXED)

        for key, raw in value.items():
            if not isinstance(key, str):
                raise_conversion(
                    "container-invalid",
                    json_pointer((*path, key)),
                    f"a grouped property name must be a string, not {type(key).__name__}",
                )
            if key == TYPE_KEY and self.format == "pyxsd":
                continue
            if key == nil_key:
                continue
            if self.format == "pyxsd" and _native_special_attribute(key) is not None:
                continue
            if key.startswith("@"):
                if self.format == "parker":
                    raise_conversion(
                        "name-invalid",
                        json_pointer((*path, key)),
                        f"Parker cannot supply the attribute {key!r}",
                    )
                expanded = self._native_attribute_name(
                    key[1:], attr_types, json_pointer((*path, key))
                )
                self._set_attribute(
                    element, expanded, raw, attr_types, frame, json_pointer((*path, key))
                )
                continue
            if key == TEXT_KEY and (self.format == "pyxsd" or mixed):
                text_raw = raw
                text_key = key
                continue
            entries.append((key, raw))

        if nilled:
            if text_key is not None or entries:
                offender = text_key if text_key is not None else entries[0][0]
                raise_conversion(
                    "marker-conflict",
                    json_pointer((*path, offender)),
                    "a nilled element may carry attributes but no text or child content",
                )
            return

        if text_key is not None:
            self._apply_grouped_text(
                element, text_raw, type_class, plan, generic, frame, path, text_key
            )

        if text_key is not None and entries and mixed:
            self._fallback(
                "mixed-content",
                json_pointer(path),
                f"grouped import of {element.tag!r} cannot retain text positions among child elements",
            )

        groups: dict[str, list[tuple[str, JsonValue]]] = {}
        declarations: dict[str, Any] = {}
        for key, raw in entries:
            if self.format == "pyxsd":
                expanded, declaration = self._native_child_spec(
                    key, plan, json_pointer((*path, key))
                )
            else:
                expanded, declaration = self._parker_child_spec(
                    key, plan, json_pointer((*path, key))
                )
            if expanded in groups:
                raise_conversion(
                    "json-duplicate-key",
                    json_pointer((*path, key)),
                    f"more than one property denotes the expanded name {expanded!r}",
                )
            groups[expanded] = [(key, raw)]
            declarations[expanded] = declaration

        if not groups:
            return

        layout = None if plan is None else plan.layout
        if _grouped_order_ambiguous(layout, type_class, plan, list(groups), generic=generic):
            self._fallback(
                "order-ambiguous",
                json_pointer(path),
                f"grouped properties of {element.tag!r} cannot reconstruct the required child order",
            )
            order = list(groups)
        elif layout in (Layout.RECORD, Layout.LIST, Layout.LIST_PLUS) or (
            layout is Layout.SEQUENCE and plan is not None
        ):
            plan_order = [] if plan is None else [child.name for child in plan.children]
            present = set(groups)
            order = [name for name in plan_order if name in present]
            order.extend(name for name in groups if name not in set(plan_order))
        else:
            order = list(groups)

        for expanded in order:
            for key, raw in groups[expanded]:
                declaration = declarations[expanded]
                self._append_grouped_children(
                    element,
                    expanded,
                    declaration,
                    key,
                    raw,
                    plan,
                    frame,
                    path,
                )

    def _apply_grouped_text(
        self,
        element: ET.Element,
        raw: JsonValue,
        type_class: type,
        plan: LayoutPlan | None,
        generic: bool,
        frame: dict[str, str],
        path: tuple[str | int, ...],
        key: str,
    ) -> None:
        """Apply the grouped ``$`` text entry."""
        text_path = json_pointer((*path, key))
        if raw is None:
            return
        layout = None if plan is None else plan.layout
        if not generic and layout in (Layout.SIMPLE, Layout.SIMPLE_PLUS):
            lexical = self._scalar_text(raw, type_class, frame, text_path)
        else:
            if not isinstance(raw, str):
                raise_conversion(
                    "scalar-type-invalid",
                    text_path,
                    f"expected a JSON string for text, got {type(raw).__name__}",
                )
            lexical = raw
        element.text = lexical or None

    def _append_grouped_children(
        self,
        element: ET.Element,
        expanded: str,
        declaration: Any,
        key: str,
        raw: JsonValue,
        plan: LayoutPlan | None,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        """Expand one grouped property's array into child elements."""
        datatype = self._declared_type(declaration)
        max_occurs = _child_max_occurs(plan, expanded)
        if isinstance(raw, list):
            if datatype is not None and _is_list_datatype(datatype):
                if max_occurs is None or max_occurs > 1:
                    raise_conversion(
                        "container-invalid",
                        json_pointer((*path, key)),
                        f"the array-valued child {key!r} is ambiguous between an XSD list "
                        "value and repeated child elements",
                    )
                value = raw
                if self.format == "pyxsd" and len(raw) == 1 and isinstance(raw[0], list):
                    # ``always_list=True`` wraps a list-valued child in one
                    # outer array; unwrap only that unambiguous shape.
                    value = raw[0]
                element.append(
                    self._grouped_element(expanded, value, declaration, frame, (*path, key))
                )
                return
            for index, item in enumerate(raw):
                element.append(
                    self._grouped_element(expanded, item, declaration, frame, (*path, key, index))
                )
            return
        element.append(self._grouped_element(expanded, raw, declaration, frame, (*path, key)))

    # ------------------------------------------------------------------
    # Native markers and null
    # ------------------------------------------------------------------

    @staticmethod
    def _native_markers(
        value: dict[str, JsonValue],
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> tuple[Any, Any, str | None]:
        """Find ``@xsi:type`` and ``@xsi:nil`` without consuming them."""
        type_raw: Any = _MISSING
        nil_raw: Any = _MISSING
        nil_key: str | None = None
        seen: dict[str, str] = {}
        for key in value:
            if not isinstance(key, str):
                raise_conversion(
                    "container-invalid",
                    json_pointer((*path, key)),
                    f"a grouped property name must be a string, not {type(key).__name__}",
                )
            special = _native_special_attribute(key)
            if special is None:
                continue
            if special in seen:
                raise_conversion(
                    "marker-conflict",
                    json_pointer((*path, key)),
                    f"the {special} marker appears more than once ({seen[special]!r} and {key!r})",
                )
            seen[special] = key
            if special == "type":
                type_raw = value[key]
            else:
                nil_raw = value[key]
                nil_key = key
        return type_raw, nil_raw, nil_key

    @staticmethod
    def _apply_native_type(
        element: ET.Element,
        raw: JsonValue,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        if not isinstance(raw, str):
            raise_conversion(
                "scalar-type-invalid",
                json_pointer((*path, "@xsi:type")),
                f"expected a lexical QName string for @xsi:type, got {type(raw).__name__}",
            )
        element.attrib[XSI_TYPE] = raw

    def _native_type(
        self,
        raw: str,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> type | None:
        """Resolve a native ``xsi:type`` marker when its prefix is bound."""
        expanded = _resolve_lexical_name(
            raw,
            frame,
            json_pointer((*path, "@xsi:type")),
            attribute=False,
        )
        if expanded is None:
            return None
        return self._resolve_type(expanded)

    @staticmethod
    def _apply_native_nil(
        element: ET.Element,
        raw: JsonValue,
        path: tuple[str | int, ...],
    ) -> bool:
        if isinstance(raw, bool):
            lexical = "true" if raw else "false"
        elif isinstance(raw, str):
            lexical = raw
        else:
            raise_conversion(
                "scalar-type-invalid",
                json_pointer((*path, "@xsi:nil")),
                f"expected a boolean for @xsi:nil, got {type(raw).__name__}",
            )
        element.attrib[XSI_NIL] = lexical
        return lexical.strip().lower() in _NIL_LEXICALS

    def _native_null_is_nil(
        self,
        descriptor: Any,
        type_class: type,
        plan: LayoutPlan | None,
        path: tuple[str | int, ...],
    ) -> bool:
        """Resolve legacy native ``null`` under the empty/nil ambiguity rule."""
        nillable = bool(descriptor is not None and descriptor.isNillable())
        empty_possible = _empty_content_possible(descriptor, type_class, plan)
        if nillable and empty_possible:
            self._fallback(
                "empty-nil-ambiguous",
                json_pointer(path),
                "native null can mean either empty content or a nil value",
            )
            return True
        return nillable


# ---------------------------------------------------------------------------
# BadgerFish import
# ---------------------------------------------------------------------------


class _BadgerfishBuilder(_BuilderBase):
    """One root-retaining BadgerFish import run."""

    def __init__(
        self,
        schema: Schema,
        root_frame: dict[str, str],
        fidelity: Fidelity,
    ) -> None:
        super().__init__(schema, root_frame)
        self.fidelity = fidelity

    def _fallback(self, code: str, path: str, message: str) -> None:
        if self.fidelity == "strict":
            raise_conversion(code, path, message)
        warn_conversion(code, path, message)

    def build(self, data: JsonValue, element: str | None) -> ET.Element:
        """Validate the root object and build its element tree."""
        if not isinstance(data, dict):
            raise_conversion(
                "container-invalid",
                "",
                f"the BadgerFish root must be a single-entry object, not {type(data).__name__}",
            )
        if len(data) != 1:
            raise_conversion(
                "root-ambiguous",
                "",
                f"the BadgerFish root object must hold exactly one entry, not {len(data)}",
            )
        root_key, root_value = next(iter(data.items()))
        frame = dict(self.root_frame)
        if isinstance(root_value, dict):
            self._merge_declarations(root_value, frame, (root_key,))
        root_name = self._element_name(root_key, frame, (root_key,))
        self._confirm_selection(element, root_name, root_key)
        descriptor = self.global_elements().get(root_name)
        return self._element(root_name, root_value, descriptor, frame, (root_key,))

    def _merge_declarations(
        self,
        value: dict[str, JsonValue],
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        """Merge one element's ``@xmlns`` object into its inherited frame."""
        declarations = value.get(XMLNS_KEY, _MISSING)
        if declarations is _MISSING:
            return
        if not isinstance(declarations, dict):
            raise_conversion(
                "container-invalid",
                json_pointer((*path, XMLNS_KEY)),
                f"{XMLNS_KEY} must be a prefix-to-URI object, not {type(declarations).__name__}",
            )
        for prefix, uri in declarations.items():
            if not isinstance(prefix, str):
                raise_conversion(
                    "name-invalid",
                    json_pointer((*path, XMLNS_KEY, prefix)),
                    f"invalid namespace prefix {prefix!r}",
                )
            if not isinstance(uri, str):
                raise_conversion(
                    "scalar-type-invalid",
                    json_pointer((*path, XMLNS_KEY, prefix)),
                    f"a namespace URI must be a string, not {type(uri).__name__}",
                )
            if prefix in ("$", "xmlns"):
                key = ""
            else:
                if not _valid_ncname(prefix):
                    raise_conversion(
                        "name-invalid",
                        json_pointer((*path, XMLNS_KEY, prefix)),
                        f"invalid namespace prefix {prefix!r}",
                    )
                key = prefix
            frame[key] = uri

    def _element_name(
        self,
        name: str,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> str:
        expanded = _resolve_lexical_name(name, frame, json_pointer(path), attribute=False)
        if expanded is None:
            self._fallback(
                "namespace-unresolved",
                json_pointer(path),
                f"the namespace prefix in {name!r} is not bound in scope",
            )
            return name
        return expanded

    def _attribute_name(
        self,
        name: str,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> str:
        expanded = _resolve_lexical_name(name, frame, json_pointer(path), attribute=True)
        if expanded is None:
            self._fallback(
                "namespace-unresolved",
                json_pointer(path),
                f"the namespace prefix in {name!r} is not bound in scope",
            )
            return name
        return expanded

    # ------------------------------------------------------------------
    # Scalar conversion
    # ------------------------------------------------------------------

    def _scalar_text(
        self,
        value: JsonValue,
        datatype: type | None,
        frame: dict[str, str],
        path: str,
    ) -> str:
        if datatype is None:
            if not isinstance(value, str):
                raise_conversion(
                    "scalar-type-invalid",
                    path,
                    f"expected a JSON string for untyped text, got {type(value).__name__}",
                )
            return value
        try:
            lexical = scalar_to_lexical(value, datatype=datatype, format="badgerfish")
        except PyXSDError as exc:
            raise _repointed(exc, path) from None
        _check_qname_tokens(lexical, datatype, frame, path, self._fallback)
        return lexical

    # ------------------------------------------------------------------
    # Element decoding
    # ------------------------------------------------------------------

    def _element(
        self,
        name: str,
        value: JsonValue,
        descriptor: Any,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> ET.Element:
        """Decode one BadgerFish element with a prepared namespace frame."""
        element = ET.Element(name)
        declared = self._declared_type(descriptor)
        type_class: type | None = declared
        explicit = False
        nilled = False

        if isinstance(value, dict):
            type_entry: tuple[str, str, JsonValue] | None = None
            nil_entry: tuple[str, JsonValue] | None = None
            attributes: list[tuple[str, str, JsonValue]] = []
            text_raw: Any = _MISSING
            text_key: str | None = None
            entries: list[tuple[str, JsonValue]] = []

            for key, raw in value.items():
                if not isinstance(key, str):
                    raise_conversion(
                        "container-invalid",
                        json_pointer((*path, key)),
                        f"a grouped property name must be a string, not {type(key).__name__}",
                    )
                if key == XMLNS_KEY:
                    continue
                if key.startswith("@"):
                    expanded = self._attribute_name(key[1:], frame, (*path, key))
                    if expanded == _TYPE_ATTR:
                        if type_entry is not None:
                            raise_conversion(
                                "marker-conflict",
                                json_pointer((*path, key)),
                                "the xsi:type marker appears more than once",
                            )
                        type_entry = (expanded, key, raw)
                        continue
                    if expanded == _NIL_ATTR:
                        if nil_entry is not None:
                            raise_conversion(
                                "marker-conflict",
                                json_pointer((*path, key)),
                                "the xsi:nil marker appears more than once",
                            )
                        nil_entry = (key, raw)
                        continue
                    attributes.append((expanded, key, raw))
                    continue
                if key == TEXT_KEY:
                    text_raw = raw
                    text_key = key
                    continue
                entries.append((key, raw))

            if type_entry is not None:
                explicit = True
                _expanded, type_key, raw_type = type_entry
                if not isinstance(raw_type, str):
                    raise_conversion(
                        "scalar-type-invalid",
                        json_pointer((*path, type_key)),
                        f"expected a lexical QName string for {type_key}, "
                        f"got {type(raw_type).__name__}",
                    )
                element.attrib[XSI_TYPE] = raw_type
                resolved_name = _resolve_lexical_name(
                    raw_type, frame, json_pointer((*path, type_key)), attribute=False
                )
                if resolved_name is None:
                    self._fallback(
                        "namespace-unresolved",
                        json_pointer((*path, type_key)),
                        f"the xsi:type prefix in {raw_type!r} is not bound in scope",
                    )
                else:
                    resolved = self._resolve_type(resolved_name)
                    if resolved is not None:
                        type_class = resolved

            if nil_entry is not None:
                nil_key, raw_nil = nil_entry
                if not isinstance(raw_nil, bool):
                    raise_conversion(
                        "scalar-type-invalid",
                        json_pointer((*path, nil_key)),
                        f"expected a JSON boolean for {nil_key}, got {type(raw_nil).__name__}",
                    )
                element.attrib[XSI_NIL] = "true" if raw_nil else "false"
                nilled = raw_nil

            if type_class is None:
                type_class = AnyType
            attr_types = self._declared_attributes(type_class)
            for expanded, key, raw in attributes:
                self._set_attribute(
                    element, expanded, raw, attr_types, frame, json_pointer((*path, key))
                )

            if nilled:
                offender = text_key
                if offender is None and entries:
                    offender = entries[0][0]
                if offender is not None:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, offender)),
                        "a nilled element may carry attributes but no text or child content",
                    )
                self.schema.namespace_context.record(element, frame)
                return element

            if text_key is not None:
                self._apply_text(element, text_raw, type_class, frame, path, text_key)

            undetermined = self._undetermined(type_class)
            plan = None if undetermined else plan_layout(type_class, explicit_type=explicit)
            self._decode_object_content(
                element, entries, plan, type_class, frame, path, undetermined, text_key
            )
            self.schema.namespace_context.record(element, frame)
            return element

        if value is None:
            element.attrib[XSI_NIL] = "true"
        else:
            if type_class is None:
                type_class = AnyType
            undetermined = self._undetermined(type_class)
            plan = None if undetermined else plan_layout(type_class)
            if undetermined:
                if not isinstance(value, str):
                    raise_conversion(
                        "scalar-type-invalid",
                        json_pointer(path),
                        f"expected a JSON string for untyped text, got {type(value).__name__}",
                    )
                element.text = value or None
            elif plan is not None and plan.layout in (Layout.SIMPLE, Layout.SIMPLE_PLUS):
                lexical = self._scalar_text(value, type_class, frame, json_pointer(path))
                element.text = lexical or None
            elif plan is not None and plan.layout is Layout.MIXED:
                if not isinstance(value, str):
                    raise_conversion(
                        "scalar-type-invalid",
                        json_pointer(path),
                        f"expected a JSON string for mixed text, got {type(value).__name__}",
                    )
                element.text = value or None
            elif isinstance(value, str):
                element.text = value or None
            else:
                raise_conversion(
                    "container-invalid",
                    json_pointer(path),
                    f"content for {element.tag!r} requires an object",
                )
        self.schema.namespace_context.record(element, frame)
        return element

    def _apply_text(
        self,
        element: ET.Element,
        raw: JsonValue,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
        key: str,
    ) -> None:
        text_path = json_pointer((*path, key))
        if raw is None:
            return
        undetermined = self._undetermined(type_class)
        plan = None if undetermined else plan_layout(type_class)
        if (
            not undetermined
            and plan is not None
            and plan.layout in (Layout.SIMPLE, Layout.SIMPLE_PLUS)
        ):
            lexical = self._scalar_text(raw, type_class, frame, text_path)
        else:
            if not isinstance(raw, str):
                raise_conversion(
                    "scalar-type-invalid",
                    text_path,
                    f"expected a JSON string for text, got {type(raw).__name__}",
                )
            lexical = raw
        element.text = lexical or None

    def _decode_object_content(
        self,
        element: ET.Element,
        entries: list[tuple[str, JsonValue]],
        plan: LayoutPlan | None,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
        generic: bool,
        text_key: str | None,
    ) -> None:
        groups: dict[str, list[tuple[str, JsonValue]]] = {}
        declarations: dict[str, Any] = {}
        child_frames: dict[str, dict[str, str]] = {}
        for key, raw in entries:
            child_frame = dict(frame)
            if isinstance(raw, dict):
                self._merge_declarations(raw, child_frame, (*path, key))
            expanded = self._element_name(key, child_frame, (*path, key))
            if expanded in groups:
                raise_conversion(
                    "json-duplicate-key",
                    json_pointer((*path, key)),
                    f"more than one property denotes the expanded name {expanded!r}",
                )
            groups[expanded] = [(key, raw)]
            declarations[expanded] = self._plan_declaration(plan, expanded) or (
                self.global_elements().get(expanded)
            )
            child_frames[expanded] = child_frame

        if not groups:
            return

        layout = None if plan is None else plan.layout
        if generic or layout is Layout.MIXED:
            if text_key is not None:
                self._fallback(
                    "mixed-content",
                    json_pointer(path),
                    f"grouped import of {element.tag!r} cannot retain text positions "
                    "among child elements",
                )
            if len(groups) > 1:
                self._fallback(
                    "order-ambiguous",
                    json_pointer(path),
                    f"grouped mixed content of {element.tag!r} cannot retain child order",
                )
            order = list(groups)
        elif layout is not None and _grouped_order_ambiguous(
            layout, type_class, plan, list(groups), generic=False
        ):
            self._fallback(
                "order-ambiguous",
                json_pointer(path),
                f"grouped properties of {element.tag!r} cannot reconstruct the required child order",
            )
            order = list(groups)
        elif layout in (Layout.RECORD, Layout.LIST, Layout.LIST_PLUS, Layout.SEQUENCE):
            plan_order = [] if plan is None else [child.name for child in plan.children]
            present = set(groups)
            order = [name for name in plan_order if name in present]
            order.extend(name for name in groups if name not in set(plan_order))
        else:
            order = list(groups)

        for expanded in order:
            for key, raw in groups[expanded]:
                self._append_children(
                    element,
                    expanded,
                    declarations[expanded],
                    key,
                    raw,
                    plan,
                    child_frames[expanded],
                    path,
                )

    @staticmethod
    def _plan_declaration(plan: LayoutPlan | None, expanded: str) -> Any:
        if plan is None:
            return None
        for child in plan.children:
            if child.name == expanded:
                return child.declaration
        return None

    def _append_children(
        self,
        element: ET.Element,
        expanded: str,
        declaration: Any,
        key: str,
        raw: JsonValue,
        plan: LayoutPlan | None,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        datatype = self._declared_type(declaration)
        max_occurs = _child_max_occurs(plan, expanded)
        if isinstance(raw, list):
            if datatype is not None and _is_list_datatype(datatype):
                if max_occurs is None or max_occurs > 1:
                    raise_conversion(
                        "container-invalid",
                        json_pointer((*path, key)),
                        f"the array-valued child {key!r} is ambiguous between an XSD list "
                        "value and repeated child elements",
                    )
                element.append(self._element(expanded, raw, declaration, frame, (*path, key)))
                return
            for index, item in enumerate(raw):
                element.append(
                    self._element(expanded, item, declaration, frame, (*path, key, index))
                )
            return
        element.append(self._element(expanded, raw, declaration, frame, (*path, key)))


# ---------------------------------------------------------------------------
# JsonML import
# ---------------------------------------------------------------------------


class _JsonmlBuilder(_BuilderBase):
    """One positional JsonML import run."""

    def __init__(
        self,
        schema: Schema,
        root_frame: dict[str, str],
        fidelity: Fidelity,
    ) -> None:
        super().__init__(schema, root_frame)
        self.fidelity = fidelity

    def _fallback(self, code: str, path: str, message: str) -> None:
        if self.fidelity == "strict":
            raise_conversion(code, path, message)
        warn_conversion(code, path, message)

    def build(self, data: JsonValue, element: str | None) -> ET.Element:
        """Validate the root array and build its nested elements."""
        if not isinstance(data, list):
            raise_conversion(
                "container-invalid",
                "",
                f"the JsonML root must be an array, not {type(data).__name__}",
            )
        if not data:
            raise_conversion("root-ambiguous", "", "the JsonML root array has no tag")
        tag_name, frame = self._header(data, self.root_frame, ())
        self._confirm_selection(element, tag_name, str(data[0]))
        descriptor = self.global_elements().get(tag_name)
        return self._element(data, tag_name, descriptor, frame, ())

    def _header(
        self,
        items: list[JsonValue],
        parent_frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> tuple[str, dict[str, str]]:
        """Resolve one array's tag after applying its own declarations."""
        if not items or not isinstance(items[0], str):
            raise_conversion(
                "container-invalid",
                json_pointer(path),
                "a JsonML element array must start with a tag string",
            )
        frame = dict(parent_frame)
        if len(items) > 1 and isinstance(items[1], dict):
            self._merge_declarations(items[1], frame, (*path, 1))
        expanded = _resolve_lexical_name(items[0], frame, json_pointer((*path, 0)), attribute=False)
        if expanded is None:
            self._fallback(
                "namespace-unresolved",
                json_pointer((*path, 0)),
                f"the namespace prefix in {items[0]!r} is not bound in scope",
            )
            expanded = items[0]
        return expanded, frame

    def _merge_declarations(
        self,
        attributes: dict[str, JsonValue],
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> None:
        """Merge ``xmlns`` / ``xmlns:prefix`` attributes into a frame."""
        for key, value in attributes.items():
            if not isinstance(key, str):
                raise_conversion(
                    "name-invalid",
                    json_pointer((*path, key)),
                    f"invalid attribute name {key!r}",
                )
            if key == "xmlns":
                prefix = ""
            elif key.startswith("xmlns:"):
                prefix = key.split(":", 1)[1]
                if not _valid_ncname(prefix):
                    raise_conversion(
                        "name-invalid",
                        json_pointer((*path, key)),
                        f"invalid namespace prefix {prefix!r}",
                    )
            else:
                continue
            if not isinstance(value, str):
                raise_conversion(
                    "scalar-type-invalid",
                    json_pointer((*path, key)),
                    f"a namespace URI must be a string, not {type(value).__name__}",
                )
            frame[prefix] = value

    # ------------------------------------------------------------------
    # Scalar conversion
    # ------------------------------------------------------------------

    def _scalar_text(
        self,
        value: JsonValue,
        datatype: type | None,
        frame: dict[str, str],
        path: str,
    ) -> str:
        if datatype is None:
            if not isinstance(value, str):
                raise_conversion(
                    "scalar-type-invalid",
                    path,
                    f"expected a JSON string for untyped text, got {type(value).__name__}",
                )
            return value
        try:
            lexical = scalar_to_lexical(value, datatype=datatype, format="jsonml")
        except PyXSDError as exc:
            raise _repointed(exc, path) from None
        _check_qname_tokens(lexical, datatype, frame, path, self._fallback)
        return lexical

    # ------------------------------------------------------------------
    # Element decoding
    # ------------------------------------------------------------------

    def _element(
        self,
        items: list[JsonValue],
        name: str,
        descriptor: Any,
        frame: dict[str, str],
        path: tuple[str | int, ...],
    ) -> ET.Element:
        """Decode one JsonML array with a prepared namespace frame."""
        element = ET.Element(name)
        declared = self._declared_type(descriptor)
        type_class: type | None = declared
        explicit = False
        has_attributes = len(items) > 1 and isinstance(items[1], dict)
        content_start = 2 if has_attributes else 1

        if has_attributes:
            attributes = items[1]
            assert isinstance(attributes, dict)
            type_entry: tuple[str, str, JsonValue] | None = None
            ordinary: list[tuple[str, str, JsonValue]] = []
            for key, raw in attributes.items():
                if key == "xmlns" or key.startswith("xmlns:"):
                    continue
                expanded = _resolve_lexical_name(
                    key, frame, json_pointer((*path, 1, key)), attribute=True
                )
                if expanded is None:
                    self._fallback(
                        "namespace-unresolved",
                        json_pointer((*path, 1, key)),
                        f"the namespace prefix in {key!r} is not bound in scope",
                    )
                    expanded = key
                if expanded == _TYPE_ATTR:
                    if type_entry is not None:
                        raise_conversion(
                            "marker-conflict",
                            json_pointer((*path, 1, key)),
                            "the xsi:type marker appears more than once",
                        )
                    type_entry = (expanded, key, raw)
                    continue
                ordinary.append((expanded, key, raw))

            if type_entry is not None:
                explicit = True
                _expanded, type_key, raw_type = type_entry
                if not isinstance(raw_type, str):
                    raise_conversion(
                        "scalar-type-invalid",
                        json_pointer((*path, 1, type_key)),
                        f"expected a lexical QName string for {type_key}, "
                        f"got {type(raw_type).__name__}",
                    )
                element.attrib[XSI_TYPE] = raw_type
                resolved_name = _resolve_lexical_name(
                    raw_type, frame, json_pointer((*path, 1, type_key)), attribute=False
                )
                if resolved_name is None:
                    self._fallback(
                        "namespace-unresolved",
                        json_pointer((*path, 1, type_key)),
                        f"the xsi:type prefix in {raw_type!r} is not bound in scope",
                    )
                else:
                    resolved = self._resolve_type(resolved_name)
                    if resolved is not None:
                        type_class = resolved

            if type_class is None:
                type_class = AnyType
            attr_types = self._declared_attributes(type_class)
            for expanded, key, raw in ordinary:
                self._set_attribute(
                    element,
                    expanded,
                    raw,
                    attr_types,
                    frame,
                    json_pointer((*path, 1, key)),
                )
        elif type_class is None:
            type_class = AnyType

        undetermined = self._undetermined(type_class)
        plan = None if undetermined else plan_layout(type_class, explicit_type=explicit)
        content = items[content_start:]

        if (
            not undetermined
            and plan is not None
            and plan.layout
            in (
                Layout.SIMPLE,
                Layout.SIMPLE_PLUS,
            )
        ):
            self._decode_simple(element, content, type_class, frame, path, content_start)
            self.schema.namespace_context.record(element, frame)
            return element

        self._decode_content(
            element,
            content,
            plan,
            type_class,
            frame,
            path,
            content_start,
            undetermined,
        )
        self.schema.namespace_context.record(element, frame)
        return element

    def _decode_simple(
        self,
        element: ET.Element,
        content: list[JsonValue],
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
        content_start: int,
    ) -> None:
        if not content:
            return
        if len(content) > 1:
            for offset, item in enumerate(content):
                if item is None:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, content_start + offset)),
                        "the null marker must be the only content item",
                    )
            raise_conversion(
                "container-invalid",
                json_pointer((*path, content_start)),
                f"simple content for {element.tag!r} accepts exactly one scalar item",
            )
        item = content[0]
        item_path = (*path, content_start)
        if item is None:
            element.attrib[XSI_NIL] = "true"
            return
        if isinstance(item, dict):
            raise_conversion(
                "container-invalid",
                json_pointer(item_path),
                f"simple content for {element.tag!r} cannot carry an object",
            )
        if isinstance(item, list) and not _is_list_datatype(type_class):
            raise_conversion(
                "container-invalid",
                json_pointer(item_path),
                f"simple content for {element.tag!r} cannot carry a child array",
            )
        lexical = self._scalar_text(item, type_class, frame, json_pointer(item_path))
        element.text = lexical or None

    def _decode_content(
        self,
        element: ET.Element,
        content: list[JsonValue],
        plan: LayoutPlan | None,
        type_class: type,
        frame: dict[str, str],
        path: tuple[str | int, ...],
        content_start: int,
        generic: bool,
    ) -> None:
        layout = None if plan is None else plan.layout
        mixed = generic or layout is Layout.MIXED
        nilled = False
        content_seen = False

        for offset, item in enumerate(content):
            index = content_start + offset
            if nilled:
                raise_conversion(
                    "marker-conflict",
                    json_pointer((*path, index)),
                    "no content may follow the null marker",
                )
            if item is None:
                if content_seen:
                    raise_conversion(
                        "marker-conflict",
                        json_pointer((*path, index)),
                        "the null marker may follow only attributes",
                    )
                element.attrib[XSI_NIL] = "true"
                nilled = True
                continue
            if isinstance(item, str):
                if not item:
                    continue
                if not mixed and not item.strip():
                    continue
                self._append_text(element, item)
                content_seen = True
                continue
            if isinstance(item, list):
                tag_name, child_frame = self._header(item, frame, (*path, index))
                declaration = self._plan_declaration(plan, tag_name) or (
                    self.global_elements().get(tag_name)
                )
                element.append(
                    self._element(item, tag_name, declaration, child_frame, (*path, index))
                )
                content_seen = True
                continue
            if isinstance(item, dict):
                raise_conversion(
                    "container-invalid",
                    json_pointer((*path, index)),
                    "a JsonML content item cannot be an object",
                )
            if mixed:
                raise_conversion(
                    "scalar-type-invalid",
                    json_pointer((*path, index)),
                    f"expected a JSON string or child array, got {type(item).__name__}",
                )
            raise_conversion(
                "container-invalid",
                json_pointer((*path, index)),
                f"element-only content for {element.tag!r} cannot carry "
                f"a {type(item).__name__} item",
            )

    @staticmethod
    def _plan_declaration(plan: LayoutPlan | None, expanded: str) -> Any:
        if plan is None:
            return None
        for child in plan.children:
            if child.name == expanded:
                return child.declaration
        return None


# ---------------------------------------------------------------------------
# Shared conventional-format helpers
# ---------------------------------------------------------------------------


def _child_max_occurs(plan: LayoutPlan | None, expanded: str) -> int | None:
    """The schema maximum occurrence for one grouped child property."""
    if plan is None:
        return 1
    for child in plan.children:
        if child.name == expanded:
            return child.max_occurs
    return 1


def _is_list_datatype(datatype: type) -> bool:
    """Whether the datatype's JSON value is an array of items."""
    return issubclass(datatype, (XsdList, TypeList, _ListString))


def _qname_tokens(datatype: type, lexical: str) -> list[str]:
    """The QName-valued tokens of one lexical scalar, if the datatype has any."""
    if issubclass(datatype, QName):
        return [lexical] if lexical else []
    if issubclass(datatype, XsdList):
        item_type = getattr(datatype, "itemType", None)
        if isinstance(item_type, type) and issubclass(item_type, QName):
            return lexical.split()
    return []


def _check_qname_tokens(
    lexical: str,
    datatype: type,
    frame: dict[str, str],
    path: str,
    fallback: Any,
) -> None:
    """Apply the unresolved-QName fallback to every QName-valued token."""
    for token in _qname_tokens(datatype, lexical):
        if ":" not in token:
            continue
        prefix = token.split(":", 1)[0]
        if prefix != "xml" and not frame.get(prefix):
            fallback(
                "namespace-unresolved",
                path,
                f"the QName prefix {prefix!r} is not bound in scope",
            )


def _resolve_lexical_name(
    name: str,
    frame: dict[str, str],
    path: str,
    *,
    attribute: bool,
) -> str | None:
    """Resolve one lexical QName to a Clark name.

    Returns ``None`` when the name carries a prefix the frame does not
    bind. Malformed syntax raises a conversion ``name-invalid`` error with
    *path*. Unprefixed attributes are never in the default namespace.
    """
    if not isinstance(name, str) or "{" in name or "}" in name:
        raise_conversion("name-invalid", path, f"invalid lexical name {name!r}")
    if name.count(":") > 1:
        raise_conversion("name-invalid", path, f"invalid lexical name {name!r}")
    if ":" in name:
        prefix, local = name.split(":", 1)
        if not _valid_ncname(prefix) or not _valid_ncname(local):
            raise_conversion("name-invalid", path, f"invalid lexical name {name!r}")
        if prefix == "xml":
            return _clark(_XML_NAMESPACE, local)
        uri = frame.get(prefix)
        if not uri:
            return None
        return _clark(uri, local)
    if not _valid_ncname(name):
        raise_conversion("name-invalid", path, f"invalid lexical name {name!r}")
    if attribute:
        return name
    uri = frame.get("", "")
    return _clark(uri, name) if uri else name


def _native_special_attribute(key: str) -> str | None:
    """Classify a native attribute key as the XSI nil or type marker."""
    if not key.startswith("@"):
        return None
    body = key[1:]
    if body in ("xsi:nil", _NIL_ATTR):
        return "nil"
    if body in ("xsi:type", _TYPE_ATTR):
        return "type"
    return None


def _compiled_model(xsd_type: type) -> Any:
    model = xsd_type.__dict__.get("_instanceContentModel_")
    if model is None:
        model = xsd_type.__dict__.get("_contentModel_")
    return model


def _particle_repeatable(particle: Any) -> bool:
    return particle.max_occurs is None or particle.max_occurs > 1


def _particle_names(particle: Any) -> tuple[set[str], bool]:
    """Known element names below one particle and whether it has a wildcard."""
    if particle.kind == "element":
        return {name for name, _declaration in _possible_element_names(particle.descriptor)}, False
    if particle.kind == "any":
        return set(), True
    names: set[str] = set()
    wildcard = False
    for child in particle.children:
        child_names, child_wildcard = _particle_names(child)
        names |= child_names
        wildcard = wildcard or child_wildcard
    return names, wildcard


def _has_ambiguous_interleaving(
    particle: Any,
    present: set[str],
    known: set[str],
) -> bool:
    """Whether grouped properties can interleave inside one repeated particle."""
    names, wildcard = _particle_names(particle)
    matched = len(names & present)
    if wildcard:
        matched += len(present - known)
    if matched >= 2 and _particle_repeatable(particle):
        return True
    return any(_has_ambiguous_interleaving(child, present, known) for child in particle.children)


def _grouped_order_ambiguous(
    layout: Layout | None,
    type_class: type,
    plan: LayoutPlan | None,
    present: list[str],
    *,
    generic: bool,
) -> bool:
    """Whether schema order uniquely reconstructs the grouped properties.

    Grouped objects collapse each expanded child name into one key. A
    model can therefore be ambiguous for two independent reasons: names
    can interleave inside a repeated particle, or one name can occupy
    more than one element particle (``a?, b, a?``). The second case is
    rejected conservatively because the grouped property carries no
    position for the individual values.
    """
    if len(present) <= 1:
        return False
    if generic or layout is Layout.MIXED:
        return True
    if layout is Layout.RECORD:
        return False
    if layout in (Layout.EMPTY, Layout.EMPTY_PLUS, Layout.SIMPLE, Layout.SIMPLE_PLUS):
        return True
    model = _compiled_model(type_class)
    known = set() if plan is None else {child.name for child in plan.children}
    names = set(present)
    return _has_ambiguous_interleaving(model, names, known) or _has_name_at_multiple_positions(
        model, names
    )


def _has_name_at_multiple_positions(model: Any, present: set[str]) -> bool:
    """Whether any present name can occupy more than one element particle."""
    positions: dict[str, int] = {}
    _count_element_positions(model, positions)
    return any(positions.get(name, 0) > 1 for name in present)


def _count_element_positions(particle: Any, positions: dict[str, int]) -> None:
    """Count distinct element-particle positions for each expanded name.

    Repeated occurrences of one particle stay one position: ``a+`` is a
    contiguous block and its grouped array is reconstructible. Two
    separate ``a`` particles are not.
    """
    if particle.max_occurs == 0:
        return
    if particle.kind == "element":
        for name, _declaration in _possible_element_names(particle.descriptor):
            positions[name] = positions.get(name, 0) + 1
        return
    if particle.kind == "any":
        return
    for child in particle.children:
        _count_element_positions(child, positions)


def _empty_content_possible(
    descriptor: Any,
    type_class: type,
    plan: LayoutPlan | None,
) -> bool:
    """Whether the target accepts an element with no attributes or content."""
    if descriptor is not None:
        get_default = getattr(descriptor, "getDefault", None)
        get_fixed = getattr(descriptor, "getFixed", None)
        if callable(get_default) and get_default() is not None:
            return True
        if callable(get_fixed) and get_fixed() is not None:
            return True

    if descriptor is None:
        return True
    if plan is None:
        return True

    layout = plan.layout
    if layout in (Layout.EMPTY, Layout.EMPTY_PLUS) or layout is Layout.MIXED:
        empty_possible = True
    elif layout in (Layout.SIMPLE, Layout.SIMPLE_PLUS):
        try:
            type_class("")
        except (TypeError, ValueError):
            empty_possible = False
        else:
            empty_possible = True
    elif plan.has_wildcards:
        empty_possible = True
    else:
        empty_possible = all(child.min_occurs == 0 for child in plan.children)

    if not empty_possible:
        return False
    for descriptor_attr in _declared_attribute_descriptors(type_class).values():
        if descriptor_attr.getUse() != "required":
            continue
        if descriptor_attr.getDefault() is None and descriptor_attr.getFixed() is None:
            return False
    return True


def _declared_attribute_descriptors(type_class: type) -> dict[str, Any]:
    table: dict[str, Any] = {}
    for klass in type_class.__mro__:
        for key in klass.__dict__.get("_attributeNames_", ()):
            if key in table:
                continue
            table[key] = klass.__dict__[key]
    return table


def _is_attribute_map(item: JsonValue) -> TypeGuard[dict[str, JsonValue]]:
    """Whether an array item is an attribute map (every key is ``@``-keyed)."""
    return isinstance(item, dict) and bool(item) and all(key.startswith("@") for key in item)
