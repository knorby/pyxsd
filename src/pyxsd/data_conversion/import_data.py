"""Strict JSON parsing and ``pyxsd-json`` ElementTree construction.

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
from pyxsd.xsd_data_types import AnyType, QName, TypeList, XsdList
from pyxsd.xsi import XSI_NIL, XSI_TYPE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pyxsd.schema import Schema

#: Reserved attribute key carrying an explicit actual type (profile v1).
TYPE_KEY = "@xsi:type"

#: Reserved property key for simple content and the nil marker.
CONTENT_KEY = "#content"


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
    generated bindings for EQName values) has been recorded on
    ``schema.namespace_context``, so ``schema.parse(result)`` binds it
    exactly like a parsed document. The conventional formats arrive
    with their own import decoders; an unknown format or fidelity mode
    raises ``ValueError`` before any conversion work.
    """
    requested_format = validate_format(format)
    validate_fidelity(fidelity)
    if requested_format != "pyxsd-json":
        raise_conversion(
            "schema-shape-unsupported",
            "",
            f"{requested_format!r} import is not implemented in this profile version",
        )
    builder = _Builder(schema, dict(namespaces) if namespaces else {})
    return builder.build(data, element)


def _repointed(exc: PyXSDError, path: str) -> PyXSDError:
    """Rebuild a nested conversion error with the importer's JSON path."""
    text = str(exc)
    code, _, rest = text.partition(" at ")
    detail = rest.partition("': ")[2]
    return PyXSDError(f"{code} at {path!r}: {detail}")


_NO_MARKER = object()


class _Builder:
    """One import run: root selection, recursive decode, frame recording."""

    def __init__(self, schema: Schema, root_frame: dict[str, str]) -> None:
        self.schema = schema
        self.root_frame = root_frame
        self._prefixes: dict[str, str] = {}
        self._counter = 0
        self._globals: dict[str, Any] | None = None
        self._attr_cache: dict[type, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Root selection
    # ------------------------------------------------------------------

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

    def _declared_type(self, descriptor: Any) -> type | None:
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

    def _resolve_type(self, expanded: str) -> type | None:
        """Resolve an expanded type name through the compiled components."""
        from pyxsd.element_representatives.element_representative import ElementRepresentative

        return ElementRepresentative.typeFromName(expanded, self.schema, warn=False)

    def _expanded(self, key: str, path: str) -> str:
        """Convert an EQName key to its Clark form, repointing failures."""
        try:
            return eqname_to_expanded(key)
        except PyXSDError as exc:
            raise _repointed(exc, path) from None

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

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------

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
        expanded = self._expanded(key, path)
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
            entries[self._expanded(key, json_pointer((*path, key)))] = (key, raw)
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

    @staticmethod
    def _append_text(element: ET.Element, text: str) -> None:
        """Place character data as leading text or the last child's tail."""
        children = list(element)
        if children:
            last = children[-1]
            last.tail = (last.tail or "") + text
        else:
            element.text = (element.text or "") + text


def _is_attribute_map(item: JsonValue) -> TypeGuard[dict[str, JsonValue]]:
    """Whether an array item is an attribute map (every key is ``@``-keyed)."""
    return isinstance(item, dict) and bool(item) and all(key.startswith("@") for key in item)
