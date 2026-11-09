"""Export a bound document tree to JSON data contracts.

The native ``pyxsd`` format keeps the existing ``bound_to_dict`` structure
and adds a JSON-safe scalar layer. The ``pyxsd-json`` profile traverses the
bound tree directly: the compiled effective type and its layout plan decide
the shape, never the observed number of children. Attributes use ``@``,
simple content in an object layout uses ``#content``, and names use the
profile's ``Q{uri}local`` EQName spelling.

Parker absorbs the root and groups children by local name. BadgerFish keeps
the root object, carries attributes under ``@``, text under ``$``, and
namespace declarations under ``@xmlns``. JsonML emits nested arrays in
bound order. BadgerFish and JsonML allocate deterministic lexical prefixes
from one shared :class:`PrefixAllocator`.
"""

from __future__ import annotations

from typing import Any, NoReturn

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
from pyxsd.data_conversion.layouts import Layout, LayoutPlan, known_expanded_names, plan_layout
from pyxsd.data_conversion.names import PrefixAllocator, expanded_to_eqname
from pyxsd.data_conversion.values import scalar_to_json, selected_value
from pyxsd.dict_export import bound_to_dict
from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import XSD_NS
from pyxsd.xsd_data_types import QName, XsdDataType
from pyxsd.xsi import XSI_NAMESPACE, XSI_NIL, XSI_TYPE

#: Key carrying simple content inside an object layout.
CONTENT_KEY = "#content"

#: Reserved attribute key for an explicit actual type.
TYPE_KEY = "@xsi:type"

#: BadgerFish's namespace-declaration object.
XMLNS_KEY = "@xmlns"

#: BadgerFish's direct-text key.
TEXT_KEY = "$"

_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"

_COMMENT_NAME = "_comment_"


def bound_to_data(
    root: Any,
    *,
    format: FormatName = "pyxsd",
    fidelity: Fidelity = "best-effort",
    typed: bool = True,
    always_list: bool = False,
) -> JsonValue:
    """Export the bound tree rooted at *root* under a format contract.

    ``pyxsd`` preserves the existing ``to_dict`` structure and returns
    JSON-safe scalar values. ``pyxsd-json`` uses the schema-selected
    layout and fixed profile shape. ``parker``, ``badgerfish``, and
    ``jsonml`` implement the conventional grouped and positional shapes.
    ``typed`` and ``always_list`` are native-only options; another format
    rejects a non-default value with ``ValueError`` before conversion.

    A missing root raises :class:`~pyxsd.exceptions.PyXSDError`. Conversion
    failures carry a stable reason code and a JSON Pointer path.
    """
    requested_format = validate_format(format)
    requested_fidelity = validate_fidelity(fidelity)

    if requested_format != "pyxsd":
        if typed is not True:
            raise ValueError("typed is a native-only data-export option")
        if always_list is not False:
            raise ValueError("always_list is a native-only data-export option")

    if root is None:
        raise PyXSDError("the document has no root to export")

    if requested_format == "pyxsd":
        return _export_native(
            root,
            fidelity=requested_fidelity,
            typed=typed,
            always_list=always_list,
        )
    if requested_format == "pyxsd-json":
        return _export_hybrid(root)
    if requested_format == "parker":
        return _export_parker(root, fidelity=requested_fidelity)
    if requested_format == "badgerfish":
        return _export_badgerfish(root, fidelity=requested_fidelity)
    return _export_jsonml(root)


# ---------------------------------------------------------------------------
# Native export
# ---------------------------------------------------------------------------


def _export_native(root: Any, *, fidelity: Fidelity, typed: bool, always_list: bool) -> JsonValue:
    """Apply the native structure and its JSON-safe scalar normalization."""
    if fidelity == "strict":
        _check_native_strict(root, ())
    exported = bound_to_dict(root, typed=typed, always_list=always_list)
    return _normalize_native(exported, typed=typed)


def _normalize_native(value: Any, *, typed: bool) -> JsonValue:
    """Convert raw native scalars while leaving the structural contract intact."""
    if isinstance(value, dict):
        return {key: _normalize_native(item, typed=typed) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_native(item, typed=typed) for item in value]
    return scalar_to_json(value, format="pyxsd", typed=typed)


def _check_native_strict(node: Any, path: tuple[str, ...]) -> None:
    """Audit representation losses the raw native dictionary cannot carry."""
    name = _node_name(node)
    descriptor = getattr(node, "_descriptor_", None)
    expected_name = _descriptor_instance_name(descriptor)
    if expected_name and expected_name.startswith("{") and not name.startswith("{"):
        raise_conversion(
            "namespace-lost",
            json_pointer(path),
            f"native export spells the namespaced element {expected_name!r} "
            f"as the local name {name!r}",
        )

    children = _content_children(node)
    attributes = getattr(node, "_attribs_", None) or {}
    _check_native_qnames(node, path)
    for key in attributes:
        declared = _declared_attribute(node, key)
        if declared is not None:
            _check_native_qnames(node.__dict__.get(declared[0]), (*path, "@" + key))
    nilled = _is_nil(node)

    if nilled and not _has_nil_marker(node):
        raise_conversion(
            "empty-nil-ambiguous",
            json_pointer(path),
            "a nilled element without an xsi:nil marker exports as the same "
            "null value as an empty element",
        )
    if (
        not nilled
        and not children
        and not attributes
        and not isinstance(node, XsdDataType)
        and getattr(node, "_value_", None) is None
    ):
        raise_conversion(
            "empty-nil-ambiguous",
            json_pointer(path),
            "an empty element exports as null, which a nilled element also uses",
        )

    if not getattr(node, "_elementOnly_", True) and children:
        text = _node_text(node)
        tails = [getattr(child, "_tail_", None) or "" for child in children]
        if text or any(tails):
            raise_conversion(
                "mixed-content",
                json_pointer(path),
                "native export groups character data under '$' and loses "
                "its position among child elements",
            )

    seen: set[str] = set()
    previous = None
    for child in children:
        child_name = _node_name(child)
        if child_name != previous and child_name in seen:
            raise_conversion(
                "order-ambiguous",
                json_pointer((*path, child_name)),
                "native export groups repeated children and loses their "
                "interleaving with other names",
            )
        seen.add(child_name)
        previous = child_name

    for child in children:
        _check_native_strict(child, (*path, _node_name(child)))


def _has_nil_marker(node: Any) -> bool:
    attributes = getattr(node, "_attribs_", None) or {}
    return "xsi:nil" in attributes or XSI_NIL in attributes


# ---------------------------------------------------------------------------
# pyxsd-json export
# ---------------------------------------------------------------------------


def _export_hybrid(root: Any) -> JsonValue:
    """Export the root under the root-preserving ``pyxsd-json`` profile."""
    root_name = _element_eqname(root)
    return {root_name: _export_node(root, (root_name,))}


def _export_node(node: Any, path: tuple[str | int, ...]) -> JsonValue:
    """Export one bound node under its effective type's layout."""
    children = _content_children(node)
    nilled = _is_nil(node)
    type_name = _explicit_type_eqname(node)
    plan = plan_layout(type(node), explicit_type=type_name is not None)

    if nilled and (children or _node_text(node) or _has_non_whitespace_tail(node)):
        _container_error(
            path,
            "a nilled element may carry attributes but no text or child content",
        )

    layout = plan.layout
    if layout is Layout.EMPTY:
        if _usable_attribs(node) or children or _has_non_whitespace_content(node):
            _container_error(
                path,
                f"empty content for {_element_eqname(node)!r} has no container "
                "for observed attributes, text, or children",
            )
        return None if nilled else ""

    if layout is Layout.EMPTY_PLUS:
        if children or _has_non_whitespace_content(node):
            _container_error(
                path,
                f"empty content for {_element_eqname(node)!r} cannot carry text or child elements",
            )
        result = _attribute_map(node, type_name, sort=False)
        if nilled:
            result[CONTENT_KEY] = None
        return result

    if layout is Layout.SIMPLE:
        if _usable_attribs(node) or children:
            _container_error(
                path,
                f"simple content for {_element_eqname(node)!r} has no container "
                "for observed attributes or child elements",
            )
        return None if nilled else _simple_value(node)

    if layout is Layout.SIMPLE_PLUS:
        if children:
            _container_error(
                path,
                f"simple content for {_element_eqname(node)!r} cannot carry child elements",
            )
        result = _attribute_map(node, type_name, sort=False)
        result[CONTENT_KEY] = None if nilled else _simple_value(node)
        return result

    if layout is Layout.LIST:
        if _usable_attribs(node):
            _container_error(
                path,
                f"list layout for {_element_eqname(node)!r} has no attribute map",
            )
        if _has_non_whitespace_content(node):
            _container_error(
                path,
                f"element-only list {_element_eqname(node)!r} cannot carry non-whitespace text",
            )
        if nilled:
            return None
        return _list_values(node, plan, children, path)

    if layout is Layout.LIST_PLUS:
        if _has_non_whitespace_content(node):
            _container_error(
                path,
                f"element-only list {_element_eqname(node)!r} cannot carry non-whitespace text",
            )
        result = _attribute_map(node, type_name, sort=False)
        if nilled:
            result[CONTENT_KEY] = None
            return result
        child_key = expanded_to_eqname(plan.children[0].name)
        result[child_key] = _list_values(node, plan, children, (*path, child_key))
        return result

    if layout is Layout.RECORD:
        if _has_non_whitespace_content(node):
            _container_error(
                path,
                f"element-only record {_element_eqname(node)!r} cannot carry non-whitespace text",
            )
        result = _attribute_map(node, type_name, sort=False)
        if nilled:
            result[CONTENT_KEY] = None
            return result
        return _record_value(node, plan, children, path, type_name)

    if layout is Layout.SEQUENCE:
        if _has_non_whitespace_content(node):
            _container_error(
                path,
                f"element-only sequence {_element_eqname(node)!r} cannot carry non-whitespace text",
            )
        return _sequence_value(node, children, path, type_name, nilled)

    return _mixed_value(node, children, path, type_name, nilled)


def _record_value(
    node: Any,
    plan: LayoutPlan,
    children: list[Any],
    path: tuple[str | int, ...],
    type_name: str | None,
) -> dict[str, JsonValue]:
    """Export a record object in deterministic schema declaration order."""
    result = _attribute_map(node, type_name, sort=False)
    by_name: dict[str, list[Any]] = {}
    for child in children:
        by_name.setdefault(_element_expanded_name(child), []).append(child)

    for child_plan in plan.children:
        matches = by_name.pop(child_plan.name, [])
        if not matches:
            continue
        if len(matches) > 1:
            _container_error(
                (*path, expanded_to_eqname(child_plan.name)),
                f"record child {child_plan.name!r} occurs more than its schema cardinality allows",
            )
        child = matches[0]
        child_key = _element_eqname(child)
        result[child_key] = _export_node(child, (*path, child_key))

    if by_name:
        unknown = next(iter(by_name))
        _container_error(
            (*path, expanded_to_eqname(unknown)),
            f"record content for {_element_eqname(node)!r} has no property for "
            f"the observed child {unknown!r}",
        )
    return result


def _sequence_value(
    node: Any,
    children: list[Any],
    path: tuple[str | int, ...],
    type_name: str | None,
    nilled: bool,
) -> list[JsonValue]:
    """Export ordered child maps, with a leading sorted attribute map."""
    items: list[JsonValue] = []
    attributes = _attribute_map(node, type_name, sort=True)
    if attributes:
        items.append(attributes)
    if nilled:
        items.append({CONTENT_KEY: None})
        return items
    for child in children:
        child_key = _element_eqname(child)
        index = len(items)
        items.append({child_key: _export_node(child, (*path, index, child_key))})
    return items


def _mixed_value(
    node: Any,
    children: list[Any],
    path: tuple[str | int, ...],
    type_name: str | None,
    nilled: bool,
) -> list[JsonValue]:
    """Export mixed content as text, child maps, and following tails."""
    items: list[JsonValue] = []
    attributes = _attribute_map(node, type_name, sort=True)
    if attributes:
        items.append(attributes)
    if nilled:
        items.append({CONTENT_KEY: None})
        return items

    text = _node_text(node)
    if text:
        items.append(text)
    for child in children:
        child_key = _element_eqname(child)
        index = len(items)
        items.append({child_key: _export_node(child, (*path, index, child_key))})
        tail = getattr(child, "_tail_", None)
        if tail:
            items.append(tail)
    return items


def _list_values(
    node: Any,
    plan: LayoutPlan,
    children: list[Any],
    path: tuple[str | int, ...],
) -> list[JsonValue]:
    """Export one repeatable child name as an array of complete values."""
    expected = plan.children[0].name
    values: list[JsonValue] = []
    for index, child in enumerate(children):
        actual = _element_expanded_name(child)
        if actual != expected:
            _container_error(
                (*path, index),
                f"list content for {_element_eqname(node)!r} expected the child "
                f"{expected!r}, not {actual!r}",
            )
        values.append(_export_node(child, (*path, index)))
    return values


def _simple_value(node: Any) -> JsonValue:
    """Convert an element's simple content under the fixed scalar policy.

    Generated simple-content classes inherit their content datatype, so
    the bound instance itself carries the typed value for every simple
    layout.
    """
    return scalar_to_json(node, format="pyxsd-json")


# ---------------------------------------------------------------------------
# Parker
# ---------------------------------------------------------------------------


def _export_parker(root: Any, *, fidelity: Fidelity) -> JsonValue:
    """Export the root under Parker's root-absorbed grouped structure."""
    return _parker_node(root, (), fidelity=fidelity)


def _parker_node(
    node: Any,
    path: tuple[str | int, ...],
    *,
    fidelity: Fidelity,
) -> JsonValue:
    """Export one bound node without attributes or namespace declarations."""
    children = _content_children(node)
    nilled = _is_nil(node)
    actual_type = _explicit_type(node)
    plan = plan_layout(type(node), explicit_type=actual_type is not None)

    if nilled and (children or _node_text(node) or _has_non_whitespace_tail(node)):
        _container_error(
            path,
            "a nilled element may carry attributes but no text or child content",
        )

    if not path:
        _parker_root_loss(node, path, fidelity)
    _parker_name_losses(node, plan, children, path, fidelity)

    if _usable_attribs(node):
        _loss(
            "attributes-lost",
            path,
            f"Parker cannot carry the attributes of element {_element_eqname(node)!r}",
            fidelity,
        )
    if actual_type is not None:
        _loss(
            "attributes-lost",
            path,
            f"Parker cannot carry the xsi:type marker of element {_element_eqname(node)!r}",
            fidelity,
        )

    layout = plan.layout
    if layout is Layout.EMPTY or layout is Layout.EMPTY_PLUS:
        if children or _has_non_whitespace_content(node):
            _container_error(
                path,
                f"empty content for {_element_eqname(node)!r} has no container "
                "for observed text or child elements",
            )
        return None if nilled else {}

    if layout is Layout.SIMPLE or layout is Layout.SIMPLE_PLUS:
        if children:
            _container_error(
                path,
                f"simple content for {_element_eqname(node)!r} cannot carry child elements",
            )
        if nilled:
            return None
        return _parker_scalar(node, fidelity=fidelity, path=path)

    if nilled:
        return None

    if layout is Layout.LIST or layout is Layout.LIST_PLUS:
        if _has_non_whitespace_content(node):
            _container_error(
                path,
                f"element-only list {_element_eqname(node)!r} cannot carry non-whitespace text",
            )
        return _parker_children(plan, children, path, fidelity=fidelity)

    if layout is Layout.RECORD or layout is Layout.SEQUENCE:
        if _has_non_whitespace_content(node):
            _container_error(
                path,
                f"element-only content {_element_eqname(node)!r} cannot carry non-whitespace text",
            )
        if layout is Layout.SEQUENCE:
            _check_grouped_order(node, plan, children, path, fidelity)
        return _parker_children(plan, children, path, fidelity=fidelity)

    return _parker_mixed(node, plan, children, path, fidelity)


def _parker_mixed(
    node: Any,
    plan: LayoutPlan,
    children: list[Any],
    path: tuple[str | int, ...],
    fidelity: Fidelity,
) -> JsonValue:
    """Export mixed content as grouped children plus concatenated direct text."""
    text = _mixed_text(node, children)
    if not children:
        if not text:
            return {}
        return _parker_text_value(node, text, fidelity=fidelity, path=path)
    if text:
        _loss(
            "mixed-content",
            path,
            f"Parker cannot retain the text positions of element {_element_eqname(node)!r}",
            fidelity,
        )
    if len({_element_expanded_name(child) for child in children}) > 1:
        _loss(
            "order-ambiguous",
            path,
            f"Parker cannot retain the child order of mixed element {_element_eqname(node)!r}",
            fidelity,
        )
    else:
        _check_grouped_order(node, plan, children, path, fidelity)
    result: dict[str, JsonValue] = {}
    if text:
        result[TEXT_KEY] = text
    result.update(_parker_children(plan, children, path, fidelity=fidelity))
    return result


def _parker_text_value(
    node: Any,
    text: str,
    *,
    fidelity: Fidelity,
    path: tuple[str | int, ...],
) -> JsonValue:
    if isinstance(node, XsdDataType):
        return _parker_scalar(node, fidelity=fidelity, path=path)
    return text


def _parker_children(
    plan: LayoutPlan,
    children: list[Any],
    path: tuple[str | int, ...],
    *,
    fidelity: Fidelity,
) -> dict[str, JsonValue]:
    """Group children by local name; actual repeats become arrays."""
    groups: dict[str, list[Any]] = {}
    for child in children:
        groups.setdefault(_local_name(_element_expanded_name(child)), []).append(child)
    keys: list[str] = []
    for child_plan in plan.children:
        key = _local_name(child_plan.name)
        if key in groups and key not in keys:
            keys.append(key)
    keys.extend(key for key in groups if key not in keys)

    result: dict[str, JsonValue] = {}
    for key in keys:
        group = groups[key]
        values: list[JsonValue] = []
        for index, child in enumerate(group):
            child_path = (*path, key, index) if len(group) > 1 else (*path, key)
            values.append(_parker_node(child, child_path, fidelity=fidelity))
        result[key] = values if len(values) > 1 else values[0]
    return result


def _parker_root_loss(node: Any, path: tuple[str | int, ...], fidelity: Fidelity) -> None:
    """Reject a root local name that names several concrete global elements."""
    local = _local_name(_element_expanded_name(node))
    candidates: set[str] = set()
    for element in _global_elements(node):
        if element.isAbstract():
            continue
        expanded = element.expandedInstanceName()
        if _local_name(expanded) == local:
            candidates.add(expanded)
    if len(candidates) > 1:
        _loss(
            "root-ambiguous",
            path,
            f"Parker cannot select among the global elements named {local!r}: "
            f"{sorted(candidates)!r}",
            fidelity,
        )


def _parker_name_losses(
    node: Any,
    plan: LayoutPlan,
    children: list[Any],
    path: tuple[str | int, ...],
    fidelity: Fidelity,
) -> None:
    """Reject a local child name the compiled schema cannot resolve uniquely."""
    plan_names = {child.name for child in plan.children}
    observed: dict[str, set[str]] = {}
    for child in children:
        expanded = _element_expanded_name(child)
        observed.setdefault(_local_name(expanded), set()).add(expanded)

    globals_by_local: dict[str, set[str]] | None = None
    for local, names in observed.items():
        declared = {name for name in plan_names if _local_name(name) == local}
        ambiguous = len(declared) > 1 or (len(declared) == 1 and names != declared)
        relevant = declared | names
        if not declared and len(names) > 1:
            ambiguous = True
        elif not declared and len(names) == 1:
            if globals_by_local is None:
                globals_by_local = {}
                for element in _global_elements(node):
                    expanded = element.expandedInstanceName()
                    globals_by_local.setdefault(_local_name(expanded), set()).add(expanded)
            global_names = globals_by_local.get(local, set())
            relevant |= global_names
            ambiguous = ambiguous or len(global_names) > 1
        if ambiguous:
            _loss(
                "name-ambiguous",
                (*path, local),
                f"Parker cannot reconstruct the local child name {local!r} "
                f"from {sorted(relevant)!r}",
                fidelity,
            )


def _check_native_qnames(value: Any, path: tuple[str | int, ...]) -> None:
    """Native lexical QName values have no namespace declarations in the data."""
    value = selected_value(value)
    if isinstance(value, QName):
        try:
            expanded = value.to_expanded_name(strict=True)
        except ValueError:
            expanded = "{unresolved}"
        if expanded.startswith("{"):
            raise_conversion(
                "namespace-lost",
                json_pointer(path),
                f"native export cannot carry the namespace of xs:QName {str(value)!r}",
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_native_qnames(item, (*path, index))


def _parker_scalar(
    value: Any,
    *,
    fidelity: Fidelity,
    path: tuple[str | int, ...],
) -> JsonValue:
    """Convert a Parker scalar, reporting any QName namespace loss."""
    value = selected_value(value)
    if isinstance(value, QName):
        return _parker_qname(value, fidelity=fidelity, path=path)
    if isinstance(value, list):
        return [
            _parker_scalar(item, fidelity=fidelity, path=(*path, index))
            for index, item in enumerate(value)
        ]
    return scalar_to_json(value, format="parker")


def _parker_qname(
    value: QName,
    *,
    fidelity: Fidelity,
    path: tuple[str | int, ...],
) -> str:
    try:
        expanded = value.to_expanded_name(strict=True)
    except ValueError:
        _loss(
            "namespace-lost",
            path,
            f"Parker cannot carry the namespace of xs:QName {str(value)!r}",
            fidelity,
        )
        return str(value)
    if expanded.startswith("{"):
        _loss(
            "namespace-lost",
            path,
            f"Parker cannot carry the namespace of xs:QName {str(value)!r}",
            fidelity,
        )
        return str(value)
    return expanded


# ---------------------------------------------------------------------------
# BadgerFish
# ---------------------------------------------------------------------------


def _export_badgerfish(root: Any, *, fidelity: Fidelity) -> JsonValue:
    """Export a root-retained BadgerFish object with deterministic prefixes."""
    names = _LexicalNames(_known_names(root))
    root_name = names.lexical(_element_expanded_name(root))
    value = _badgerfish_node(root, (root_name,), fidelity=fidelity, names=names)
    declarations = names.declarations()
    if declarations:
        declaration_values: dict[str, JsonValue] = dict(declarations)
        if isinstance(value, dict):
            value = {XMLNS_KEY: declaration_values, **value}
        else:
            value = {XMLNS_KEY: declaration_values, TEXT_KEY: value}
    return {root_name: value}


def _badgerfish_node(
    node: Any,
    path: tuple[str | int, ...],
    *,
    fidelity: Fidelity,
    names: _LexicalNames,
) -> JsonValue:
    """Export one bound node with attributes, text, and grouped children."""
    children = _content_children(node)
    nilled = _is_nil(node)
    actual_type = _explicit_type(node)
    plan = plan_layout(type(node), explicit_type=actual_type is not None)

    if nilled and (children or _node_text(node) or _has_non_whitespace_tail(node)):
        _container_error(
            path,
            "a nilled element may carry attributes but no text or child content",
        )

    layout = plan.layout
    attrs = _badgerfish_attributes(
        node,
        actual_type,
        nilled=nilled,
        path=path,
        names=names,
    )

    if layout is Layout.EMPTY or layout is Layout.EMPTY_PLUS:
        if children or _has_non_whitespace_content(node):
            _container_error(
                path,
                f"empty content for {_element_eqname(node)!r} has no container "
                "for observed text or child elements",
            )
        return attrs

    if layout is Layout.SIMPLE or layout is Layout.SIMPLE_PLUS:
        if children:
            _container_error(
                path,
                f"simple content for {_element_eqname(node)!r} cannot carry child elements",
            )
        if nilled:
            return attrs
        value = _lexical_scalar(node, format="badgerfish", path=path, names=names)
        if attrs:
            attrs[TEXT_KEY] = value
            return attrs
        return value

    if nilled:
        return attrs

    if layout is Layout.LIST or layout is Layout.LIST_PLUS:
        if _has_non_whitespace_content(node):
            _container_error(
                path,
                f"element-only list {_element_eqname(node)!r} cannot carry non-whitespace text",
            )
        result = attrs
        result.update(
            _badgerfish_list_children(plan, children, path, fidelity=fidelity, names=names)
        )
        return result

    if layout is Layout.RECORD or layout is Layout.SEQUENCE:
        if _has_non_whitespace_content(node):
            _container_error(
                path,
                f"element-only content {_element_eqname(node)!r} cannot carry non-whitespace text",
            )
        if layout is Layout.SEQUENCE:
            _check_grouped_order(node, plan, children, path, fidelity)
        result = attrs
        result.update(_badgerfish_children(plan, children, path, fidelity=fidelity, names=names))
        return result

    return _badgerfish_mixed(node, plan, children, attrs, path, fidelity, names)


def _badgerfish_mixed(
    node: Any,
    plan: LayoutPlan,
    children: list[Any],
    attrs: dict[str, JsonValue],
    path: tuple[str | int, ...],
    fidelity: Fidelity,
    names: _LexicalNames,
) -> JsonValue:
    """Group mixed children and put concatenated direct text under ``$``."""
    text = _mixed_text(node, children)
    if not children:
        if not text:
            return attrs
        value = _lexical_text_value(node, text, format="badgerfish", path=path, names=names)
        if attrs:
            attrs[TEXT_KEY] = value
            return attrs
        return value
    if text:
        _loss(
            "mixed-content",
            path,
            f"BadgerFish cannot retain the text positions of element {_element_eqname(node)!r}",
            fidelity,
        )
    if len({_element_expanded_name(child) for child in children}) > 1:
        _loss(
            "order-ambiguous",
            path,
            f"BadgerFish cannot retain the child order of mixed element {_element_eqname(node)!r}",
            fidelity,
        )
    else:
        _check_grouped_order(node, plan, children, path, fidelity)
    result = attrs
    if text:
        result[TEXT_KEY] = text
    result.update(_badgerfish_children(plan, children, path, fidelity=fidelity, names=names))
    return result


def _badgerfish_attributes(
    node: Any,
    actual_type: type | None,
    *,
    nilled: bool,
    path: tuple[str | int, ...],
    names: _LexicalNames,
) -> dict[str, JsonValue]:
    attrs: dict[str, JsonValue] = {}
    if nilled:
        attrs["@" + names.lexical(f"{{{XSI_NAMESPACE}}}nil", attribute=True)] = True
    if actual_type is not None:
        attrs["@" + names.lexical(f"{{{XSI_NAMESPACE}}}type", attribute=True)] = names.lexical(
            _type_expanded_name(actual_type), attribute=True
        )
    raw_attributes = _usable_attribs(node)
    for key in _attribute_keys(node):
        lexical = names.lexical(_attribute_expanded_name(node, key), attribute=True)
        attrs["@" + lexical] = _formatted_attribute_value(
            node,
            key,
            raw_attributes[key],
            format="badgerfish",
            path=(*path, "@" + lexical),
            names=names,
        )
    return attrs


def _badgerfish_list_children(
    plan: LayoutPlan,
    children: list[Any],
    path: tuple[str | int, ...],
    *,
    fidelity: Fidelity,
    names: _LexicalNames,
) -> dict[str, JsonValue]:
    expected = plan.children[0].name
    lexical = names.lexical(expected)
    values: list[JsonValue] = []
    for index, child in enumerate(children):
        actual = _element_expanded_name(child)
        if actual != expected:
            _container_error(
                (*path, lexical, index),
                f"list content expected the child {expected!r}, not {actual!r}",
            )
        values.append(
            _badgerfish_node(child, (*path, lexical, index), fidelity=fidelity, names=names)
        )
    if not values:
        return {}
    return {lexical: values}


def _badgerfish_children(
    plan: LayoutPlan,
    children: list[Any],
    path: tuple[str | int, ...],
    *,
    fidelity: Fidelity,
    names: _LexicalNames,
) -> dict[str, JsonValue]:
    """Group children by expanded name in schema order, arrays per schema."""
    groups: dict[str, list[Any]] = {}
    for child in children:
        groups.setdefault(_element_expanded_name(child), []).append(child)

    plan_order = [child.name for child in plan.children]
    repeatable = {
        child.name: child.max_occurs is None or child.max_occurs > 1 for child in plan.children
    }
    plan_set = set(plan_order)
    ordered = [name for name in plan_order if name in groups]
    ordered.extend(name for name in groups if name not in plan_set)

    result: dict[str, JsonValue] = {}
    for name in ordered:
        group = groups[name]
        lexical = names.lexical(name)
        is_array = repeatable.get(name, False) or len(group) > 1
        values: list[JsonValue] = []
        for index, child in enumerate(group):
            child_path = (*path, lexical, index) if is_array else (*path, lexical)
            values.append(_badgerfish_node(child, child_path, fidelity=fidelity, names=names))
        result[lexical] = values if is_array else values[0]
    return result


# ---------------------------------------------------------------------------
# JsonML
# ---------------------------------------------------------------------------


def _export_jsonml(root: Any) -> JsonValue:
    """Export the root as a nested JsonML array with root declarations."""
    names = _LexicalNames(_known_names(root))
    _reserve_jsonml_names(root, names)
    declarations = names.declarations()
    result = _jsonml_node(root, (), names=names, root_declarations=bool(declarations))
    if declarations:
        attrs: dict[str, JsonValue] = {
            f"xmlns:{prefix}": uri for prefix, uri in declarations.items()
        }
        if len(result) > 1 and isinstance(result[1], dict):
            attrs.update(result[1])
            result[1] = attrs
        else:
            result.insert(1, attrs)
    return result


def _reserve_jsonml_names(node: Any, names: _LexicalNames) -> None:
    """Reserve every lexical name JsonML will emit before converting content.

    Root-scoped declarations are inserted only after the tree is converted,
    but that attribute object shifts every content index by one when the
    root has no attributes of its own. Reserving the names first makes the
    final array positions known while a diagnostic path is still built.
    Unresolved QNames stay for the export pass, which reports their path.
    """
    names.lexical(_element_expanded_name(node))
    actual_type = _explicit_type(node)
    if actual_type is not None:
        names.lexical(f"{{{XSI_NAMESPACE}}}type", attribute=True)
        names.lexical(_type_expanded_name(actual_type), attribute=True)
    for key in _attribute_keys(node):
        names.lexical(_attribute_expanded_name(node, key), attribute=True)
        declared = _declared_attribute(node, key)
        if declared is not None:
            typed = node.__dict__.get(declared[0])
            if typed is not None:
                _reserve_jsonml_value_names(typed, names)
    if isinstance(node, XsdDataType):
        _reserve_jsonml_value_names(node, names)
    for child in _content_children(node):
        _reserve_jsonml_names(child, names)


def _reserve_jsonml_value_names(value: Any, names: _LexicalNames) -> None:
    """Reserve the namespace of every resolved QName in a scalar value."""
    value = selected_value(value)
    if isinstance(value, QName):
        try:
            expanded = value.to_expanded_name(strict=True)
        except ValueError:
            return
        if expanded.startswith("{"):
            names.lexical(expanded)
    elif isinstance(value, list):
        for item in value:
            _reserve_jsonml_value_names(item, names)


def _jsonml_node(
    node: Any,
    path: tuple[str | int, ...],
    *,
    names: _LexicalNames,
    root_declarations: bool = False,
) -> list[JsonValue]:
    """Export one bound node as a positional JsonML array.

    *root_declarations* says the top-level exporter will insert a namespace
    declaration object after this call. When the node has no attributes of
    its own, that object occupies index 1 and every content item moves one
    position, which the diagnostic paths must reflect.
    """
    children = _content_children(node)
    nilled = _is_nil(node)
    actual_type = _explicit_type(node)
    plan = plan_layout(type(node), explicit_type=actual_type is not None)

    if nilled and (children or _node_text(node) or _has_non_whitespace_tail(node)):
        _container_error(
            path,
            "a nilled element may carry attributes but no text or child content",
        )

    tag = names.lexical(_element_expanded_name(node))
    result: list[JsonValue] = [tag]
    attrs = _jsonml_attributes(
        node,
        actual_type,
        path=(*path, 1),
        names=names,
    )
    if attrs:
        result.append(attrs)
    shift = 1 if root_declarations and not attrs and not path else 0

    if nilled:
        result.append(None)
        return result

    layout = plan.layout
    if layout is Layout.EMPTY or layout is Layout.EMPTY_PLUS:
        if children or _has_non_whitespace_content(node):
            _container_error(
                path,
                f"empty content for {_element_eqname(node)!r} has no container "
                "for observed text or child elements",
            )
        return result

    if layout is Layout.SIMPLE or layout is Layout.SIMPLE_PLUS:
        if children:
            _container_error(
                path,
                f"simple content for {_element_eqname(node)!r} cannot carry child elements",
            )
        result.append(
            _lexical_scalar(
                node,
                format="jsonml",
                path=(*path, len(result) + shift),
                names=names,
            )
        )
        return result

    if layout is Layout.LIST or layout is Layout.LIST_PLUS:
        if _has_non_whitespace_content(node):
            _container_error(
                path,
                f"element-only list {_element_eqname(node)!r} cannot carry non-whitespace text",
            )
        _append_jsonml_children(result, children, path, names, shift=shift)
        return result

    if layout is Layout.RECORD or layout is Layout.SEQUENCE:
        if _has_non_whitespace_content(node):
            _container_error(
                path,
                f"element-only content {_element_eqname(node)!r} cannot carry non-whitespace text",
            )
        _append_jsonml_children(result, children, path, names, shift=shift)
        return result

    text = _node_text(node)
    if text:
        result.append(
            _lexical_text_value(
                node,
                text,
                format="jsonml",
                path=(*path, len(result) + shift),
                names=names,
            )
        )
    _append_jsonml_children(result, children, path, names, with_tails=True, shift=shift)
    return result


def _append_jsonml_children(
    result: list[JsonValue],
    children: list[Any],
    path: tuple[str | int, ...],
    names: _LexicalNames,
    *,
    with_tails: bool = False,
    shift: int = 0,
) -> None:
    for child in children:
        child_path = (*path, len(result) + shift)
        result.append(_jsonml_node(child, child_path, names=names))
        if with_tails:
            tail = getattr(child, "_tail_", None)
            if tail:
                result.append(str(tail))


def _jsonml_attributes(
    node: Any,
    actual_type: type | None,
    *,
    path: tuple[str | int, ...],
    names: _LexicalNames,
) -> dict[str, JsonValue]:
    attrs: dict[str, JsonValue] = {}
    if actual_type is not None:
        attrs[names.lexical(f"{{{XSI_NAMESPACE}}}type", attribute=True)] = names.lexical(
            _type_expanded_name(actual_type), attribute=True
        )
    raw_attributes = _usable_attribs(node)
    for key in _attribute_keys(node):
        lexical = names.lexical(_attribute_expanded_name(node, key), attribute=True)
        attrs[lexical] = _formatted_attribute_value(
            node,
            key,
            raw_attributes[key],
            format="jsonml",
            path=(*path, lexical),
            names=names,
        )
    return attrs


# ---------------------------------------------------------------------------
# Shared conventional-format helpers
# ---------------------------------------------------------------------------


class _LexicalNames:
    """Deterministic lexical QNames and the declarations they require."""

    def __init__(self, known_names: list[str]) -> None:
        self._allocator = PrefixAllocator(known_names)
        self._xsi = False

    def lexical(self, expanded_name: str, *, attribute: bool = False) -> str:
        uri, local = _uri_local(expanded_name)
        if not uri:
            return local
        if uri == XSI_NAMESPACE:
            self._xsi = True
            return f"xsi:{local}"
        if uri == _XML_NAMESPACE:
            return f"xml:{local}"
        return self._allocator.lexical(expanded_name, attribute=attribute)

    def declarations(self) -> dict[str, str]:
        result = dict(self._allocator.frame())
        if self._xsi:
            result["xsi"] = XSI_NAMESPACE
        return dict(sorted(result.items()))


def _known_names(root: Any) -> list[str]:
    """Schema-known element and attribute names used for prefix allocation.

    The names come from the full effective-type closure of the root's
    declaration -- declared, conditional, and ``xsi:type``-selectable
    types alike -- independent of which types an instance actually
    observed, so the JSON Schema projection allocates the same prefixes.
    A root without a declaration contributes only its own name; observed
    wildcard names still receive deterministic URI-derived prefixes
    outside this seed.
    """
    descriptor = getattr(root, "_descriptor_", None)
    if descriptor is None:
        return [_element_expanded_name(root)]
    return known_expanded_names(descriptor.getSchema().compiledSchema, [descriptor])


def _global_elements(node: Any) -> tuple[Any, ...]:
    descriptor = getattr(node, "_descriptor_", None)
    if descriptor is None:
        return ()
    schema = descriptor.getSchema()
    return tuple(schema.elements)


def _uri_local(expanded_name: str) -> tuple[str, str]:
    eqname = expanded_to_eqname(expanded_name)
    if not eqname.startswith("Q{"):
        return "", eqname
    end = eqname.find("}", 2)
    return eqname[2:end], eqname[end + 1 :]


def _local_name(expanded_name: str) -> str:
    return _uri_local(expanded_name)[1]


def _lexical_scalar(
    value: Any,
    *,
    format: FormatName,
    path: tuple[str | int, ...],
    names: _LexicalNames,
) -> JsonValue:
    value = selected_value(value)
    if isinstance(value, QName):
        return _lexical_qname(value, path=path, names=names)
    if isinstance(value, list):
        return [
            _lexical_scalar(item, format=format, path=(*path, index), names=names)
            for index, item in enumerate(value)
        ]
    return scalar_to_json(value, format=format)


def _lexical_qname(
    value: QName,
    *,
    path: tuple[str | int, ...],
    names: _LexicalNames,
) -> str:
    try:
        expanded = value.to_expanded_name(strict=True)
    except ValueError as exc:
        raise_conversion("namespace-unresolved", json_pointer(path), str(exc))
    if not expanded.startswith("{"):
        return expanded
    return names.lexical(expanded)


def _lexical_text_value(
    node: Any,
    text: str,
    *,
    format: FormatName,
    path: tuple[str | int, ...],
    names: _LexicalNames,
) -> JsonValue:
    if isinstance(node, XsdDataType):
        return _lexical_scalar(node, format=format, path=path, names=names)
    return text


def _formatted_attribute_value(
    node: Any,
    key: str,
    raw: str,
    *,
    format: FormatName,
    path: tuple[str | int, ...],
    names: _LexicalNames,
) -> JsonValue:
    declared = _declared_attribute(node, key)
    if declared is not None:
        typed = node.__dict__.get(declared[0])
        if typed is not None:
            return _lexical_scalar(typed, format=format, path=path, names=names)
    return raw


def _check_grouped_order(
    node: Any,
    plan: LayoutPlan,
    children: list[Any],
    path: tuple[str | int, ...],
    fidelity: Fidelity,
) -> None:
    """Reject child order a grouped object cannot reconstruct."""
    if not children:
        return
    observed = [_element_expanded_name(child) for child in children]
    seen: set[str] = set()
    first_order: list[str] = []
    previous: str | None = None
    for name in observed:
        if name != previous and name in seen:
            _loss(
                "order-ambiguous",
                path,
                f"grouped export of element {_element_eqname(node)!r} cannot retain "
                f"the interleaving of child {name!r}",
                fidelity,
            )
            return
        if name not in seen:
            seen.add(name)
            first_order.append(name)
        previous = name

    plan_order = [child.name for child in plan.children]
    known = [name for name in plan_order if name in seen]
    extras = [name for name in first_order if name not in set(plan_order)]
    if first_order != known + extras:
        _loss(
            "order-ambiguous",
            path,
            f"grouped export of element {_element_eqname(node)!r} cannot reconstruct "
            "its child order",
            fidelity,
        )


def _mixed_text(node: Any, children: list[Any]) -> str:
    pieces: list[str] = []
    text = _node_text(node)
    if text:
        pieces.append(text)
    for child in children:
        tail = getattr(child, "_tail_", None)
        if tail:
            pieces.append(str(tail))
    return "".join(pieces)


def _loss(
    code: str,
    path: tuple[str | int, ...],
    message: str,
    fidelity: Fidelity,
) -> None:
    if fidelity == "strict":
        raise_conversion(code, json_pointer(path), message)
    warn_conversion(code, json_pointer(path), message)


# ---------------------------------------------------------------------------
# Names, attributes, and nil
# ---------------------------------------------------------------------------


def _node_name(node: Any) -> str:
    return getattr(node, "_name_", None) or ""


def _node_text(node: Any) -> str:
    """The node's leading character data, joined from the writer's pieces."""
    value = getattr(node, "_value_", None)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "".join(str(piece) for piece in value)
    return str(value)


def _content_children(node: Any) -> list[Any]:
    return [
        child
        for child in (getattr(node, "_children_", None) or [])
        if _node_name(child) != _COMMENT_NAME
    ]


def _is_nil(node: Any) -> bool:
    recorded = getattr(node, "_nil_", None)
    if recorded is not None:
        return bool(recorded)
    attributes = getattr(node, "_attribs_", None) or {}
    value = attributes.get("xsi:nil", attributes.get(XSI_NIL))
    return value is not None and str(value).strip().lower() in ("true", "1")


def _usable_attribs(node: Any) -> dict[str, str]:
    """Actual attributes: no namespace declarations and no reserved XSI state."""
    attributes = getattr(node, "_attribs_", None) or {}
    return {
        key: value
        for key, value in attributes.items()
        if key != "xmlns"
        and not key.startswith("xmlns:")
        and key != "xsi:nil"
        and key != XSI_NIL
        and key != "xsi:type"
        and key != XSI_TYPE
    }


def _descriptor_instance_name(descriptor: Any) -> str | None:
    """The declaration's namespace-aware instance name, in every parse mode."""
    if descriptor is None:
        return None
    return descriptor.expandedInstanceName()


def _element_expanded_name(node: Any) -> str:
    """The instance expanded name, reconstructed when legacy mode dropped it."""
    name = _node_name(node)
    if name.startswith("{"):
        return name
    expected = _descriptor_instance_name(getattr(node, "_descriptor_", None))
    if expected and expected.startswith("{"):
        return expected
    return name


def _element_eqname(node: Any) -> str:
    return expanded_to_eqname(_element_expanded_name(node))


def _declared_attribute(node: Any, key: str) -> tuple[str, Any] | None:
    """The storage key and descriptor for one declared attribute, if any."""
    getter = getattr(node, "descAttributes", None)
    if not callable(getter):
        return None
    for storage, descriptor in getter().items():
        if key == storage or key == getattr(descriptor, "expandedName", None):
            return storage, descriptor
    return None


def _attribute_eqname(node: Any, key: str) -> str:
    if key.startswith("{"):
        return expanded_to_eqname(key)
    if key.startswith("xsi:"):
        return expanded_to_eqname(f"{{{XSI_NAMESPACE}}}{key.split(':', 1)[1]}")
    declared = _declared_attribute(node, key)
    if declared is not None:
        return expanded_to_eqname(declared[1].expandedInstanceName(is_attribute=True))
    return expanded_to_eqname(key)


def _attribute_expanded_name(node: Any, key: str) -> str:
    """The Clark expanded name of one observed attribute."""
    if key.startswith("{"):
        return key
    if key.startswith("xsi:"):
        return f"{{{XSI_NAMESPACE}}}{key.split(':', 1)[1]}"
    declared = _declared_attribute(node, key)
    if declared is not None:
        return declared[1].expandedInstanceName(is_attribute=True)
    return key


def _attribute_value(node: Any, key: str, raw: str) -> JsonValue:
    declared = _declared_attribute(node, key)
    if declared is not None:
        typed = node.__dict__.get(declared[0])
        if typed is not None:
            return scalar_to_json(typed, format="pyxsd-json")
    return raw


def _attribute_keys(node: Any) -> list[str]:
    """Declared attributes in declaration order, then wildcard names sorted."""
    attributes = _usable_attribs(node)
    remaining = list(attributes)
    if not remaining:
        return []
    getter = getattr(node, "descAttributes", None)
    if not callable(getter):
        return sorted(remaining, key=lambda key: _attribute_eqname(node, key))

    ordered: list[str] = []
    for storage, descriptor in getter().items():
        expanded = getattr(descriptor, "expandedName", None)
        local = getattr(descriptor, "name", None)
        for key in list(remaining):
            if key in (storage, expanded, local):
                ordered.append(key)
                remaining.remove(key)
                break
    ordered.extend(sorted(remaining))
    return ordered


def _attribute_map(node: Any, type_name: str | None, *, sort: bool) -> dict[str, JsonValue]:
    """Build an attribute object, with the explicit type marker first."""
    result: dict[str, JsonValue] = {}
    if type_name is not None:
        result[TYPE_KEY] = type_name
    attributes = _usable_attribs(node)
    for key in _attribute_keys(node):
        result["@" + _attribute_eqname(node, key)] = _attribute_value(node, key, attributes[key])
    if not sort:
        return result
    return dict(sorted(result.items()))


def _explicit_type(node: Any) -> type | None:
    """The actual type when the declared type does not determine it."""
    attributes = getattr(node, "_attribs_", None) or {}
    raw = attributes.get("xsi:type", attributes.get(XSI_TYPE))
    if raw is None:
        return None
    descriptor = getattr(node, "_descriptor_", None)
    getter = getattr(descriptor, "getType", None)
    declared = getter() if callable(getter) else None
    if declared is type(node):
        return None
    return type(node)


def _explicit_type_eqname(node: Any) -> str | None:
    """The marker for an actual type the declared type does not determine."""
    datatype = _explicit_type(node)
    return None if datatype is None else _type_eqname(datatype)


def _type_eqname(datatype: type) -> str:
    return expanded_to_eqname(_type_expanded_name(datatype))


def _type_expanded_name(datatype: type) -> str:
    name = getattr(datatype, "name", None) or datatype.__name__
    namespace = getattr(datatype, "_targetNamespace_", None)
    if (
        not namespace
        and issubclass(datatype, XsdDataType)
        and datatype.__module__ == "pyxsd.xsd_data_types"
    ):
        namespace = XSD_NS
    return f"{{{namespace}}}{name}" if namespace else name


def _has_non_whitespace_content(node: Any) -> bool:
    if _node_text(node).strip():
        return True
    return _has_non_whitespace_tail(node)


def _has_non_whitespace_tail(node: Any) -> bool:
    for child in _content_children(node):
        tail = getattr(child, "_tail_", None)
        if tail and str(tail).strip():
            return True
    return False


def _container_error(path: tuple[Any, ...], message: str) -> NoReturn:
    raise_conversion("container-invalid", json_pointer(path), message)
