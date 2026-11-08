"""Export a bound document tree to JSON data contracts.

The native ``pyxsd`` format keeps the existing ``bound_to_dict`` structure
and adds a JSON-safe scalar layer. The ``pyxsd-json`` profile traverses the
bound tree directly: the compiled effective type and its layout plan decide
the shape, never the observed number of children. Attributes use ``@``,
simple content in an object layout uses ``#content``, and names use the
profile's ``Q{uri}local`` EQName spelling.
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
)
from pyxsd.data_conversion.layouts import Layout, LayoutPlan, plan_layout
from pyxsd.data_conversion.names import expanded_to_eqname
from pyxsd.data_conversion.values import scalar_to_json
from pyxsd.dict_export import bound_to_dict
from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import XSD_NS
from pyxsd.xsd_data_types import XsdDataType
from pyxsd.xsi import XSI_NAMESPACE, XSI_NIL, XSI_TYPE

#: Key carrying simple content inside an object layout.
CONTENT_KEY = "#content"

#: Reserved attribute key for an explicit actual type.
TYPE_KEY = "@xsi:type"

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
    layout and fixed profile shape. ``typed`` and ``always_list`` are
    native-only options; another format rejects a non-default value with
    ``ValueError`` before conversion.

    A missing root raises :class:`~pyxsd.exceptions.PyXSDError`. Conversion
    failures carry a stable reason code and a JSON Pointer path.
    """
    requested_format = validate_format(format)
    requested_fidelity = validate_fidelity(fidelity)

    if requested_format == "pyxsd-json":
        if typed is not True:
            raise ValueError("typed is a native-only data-export option")
        if always_list is not False:
            raise ValueError("always_list is a native-only data-export option")
    elif requested_format != "pyxsd":
        raise ValueError(f"data format {requested_format!r} is not implemented")

    if root is None:
        raise PyXSDError("the document has no root to export")

    if requested_format == "pyxsd":
        return _export_native(
            root,
            fidelity=requested_fidelity,
            typed=typed,
            always_list=always_list,
        )
    return _export_hybrid(root)


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


def _explicit_type_eqname(node: Any) -> str | None:
    """The marker for an actual type the declared type does not determine."""
    attributes = getattr(node, "_attribs_", None) or {}
    raw = attributes.get("xsi:type", attributes.get(XSI_TYPE))
    if raw is None:
        return None
    descriptor = getattr(node, "_descriptor_", None)
    getter = getattr(descriptor, "getType", None)
    declared = getter() if callable(getter) else None
    if declared is type(node):
        return None
    return _type_eqname(type(node))


def _type_eqname(datatype: type) -> str:
    name = getattr(datatype, "name", None) or datatype.__name__
    namespace = getattr(datatype, "_targetNamespace_", None)
    if (
        not namespace
        and issubclass(datatype, XsdDataType)
        and datatype.__module__ == "pyxsd.xsd_data_types"
    ):
        namespace = XSD_NS
    expanded = f"{{{namespace}}}{name}" if namespace else name
    return expanded_to_eqname(expanded)


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
