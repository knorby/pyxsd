"""Root-preserving, plan-directed RELAX NG JSON profile export.

Only compiled routes select layouts. Missing routes retain the lexical ordered
tree; wildcard routes retain their compiled scalar contracts without pretending
that an open child name is a record field.
"""

from __future__ import annotations

from typing import Any, Literal

from pyxsd.data_conversion.contracts import JsonValue, json_pointer, raise_conversion
from pyxsd.data_conversion.names import eqname_to_expanded, expanded_to_eqname
from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng.json_values import scalar_to_relaxng_json
from pyxsd.relaxng.names import ExceptName, Name, NameChoice, NsName
from pyxsd.relaxng.projection import ContentContract, ProjectionPlan, RouteContract
from pyxsd.relaxng.tree import RelaxNGElement

__all__ = ["export_profile"]

_Path = tuple[str | int, ...]
_Layout = Literal["empty", "scalar", "repeated", "record", "sequence", "mixed"]
_XMLNS = "{http://www.w3.org/2000/xmlns/}"


def _split(name: str) -> tuple[str | None, str]:
    if name.startswith("{"):
        uri, local = name[1:].split("}", 1)
        return uri or None, local
    return None, name


def _root_route(root: RelaxNGElement, plan: ProjectionPlan, element: str | None) -> RouteContract:
    actual = root._name_
    expanded_to_eqname(actual)
    roots = [plan.routes[key] for key in plan.roots]
    if element is not None:
        if element.startswith(("{", "Q{")):
            selected = eqname_to_expanded(
                expanded_to_eqname(element) if element.startswith("{") else element
            )
        else:
            local = eqname_to_expanded(element)
            # Name classes distinguish namespaces only by their authored
            # constants. A fresh representative detects infinitely many matches,
            # without enumerating an open name class or consulting this instance.
            namespaces: set[str | None] = {None}
            pending = [route.name_class for route in roots]
            while pending:
                name_class = pending.pop()
                if isinstance(name_class, (Name, NsName)):
                    namespaces.add(name_class.uri)
                elif isinstance(name_class, NameChoice):
                    pending.extend(name_class.alternatives)
                elif isinstance(name_class, ExceptName):
                    pending.extend((name_class.base, name_class.excluded))
            fresh = "urn:pyxsd:profile:unmentioned"
            while fresh in namespaces:
                fresh += "_"
            matches = {
                uri
                for uri in (*namespaces, fresh)
                if any(route.name_class.contains(uri, local) for route in roots)
            }
            if fresh in matches or len(matches) > 1:
                raise_conversion("root-ambiguous", "", f"ambiguous root name {element!r}")
            if not matches:
                raise_conversion("root-unresolved", "", f"unknown root name {element!r}")
            uri = matches.pop()
            selected = f"{{{uri}}}{local}" if uri else local
        if selected != actual:
            raise_conversion("root-mismatch", "", f"root {actual!r} does not match {element!r}")
    uri, local = _split(actual)
    for route in roots:
        if route.name_class.contains(uri, local):
            return route
    raise_conversion("root-unresolved", "", f"root {actual!r} is outside the start name class")


def _child_model(model: ContentContract) -> ContentContract | None:
    """Erase attribute-only subexpressions, retaining child ordering operators."""
    if model.kind in ("attribute", "empty"):
        return None
    operands = tuple(child for part in model.operands if (child := _child_model(part)) is not None)
    if model.kind in ("group", "interleave") and len(operands) == 1:
        return operands[0]
    if model.operands and not operands:
        return None
    # Keep choice/repeat even with one child operand: erasing an empty
    # alternative must not turn an ordered model into a different layout.
    return ContentContract(model.kind, operands)


def _layout(route: RouteContract | None, generic: bool) -> _Layout:
    if route is None:
        return "mixed"
    if route.content.scalar_contracts:
        return "scalar"
    if generic or route.generic_reason or any(w.role == "child" for w in route.wildcards):
        return "mixed"
    if route.content.mixed:
        return "mixed"
    model = _child_model(route.content.model)
    if model is None:
        return "empty"
    if len(route.children) == 1 and next(iter(route.children.values())).cardinality.repeatable:
        return "repeated"
    if (
        model.kind == "interleave"
        and not route.wildcards
        and all(not field.cardinality.repeatable for field in route.children.values())
    ):
        return "record"
    return "sequence"


def _scalar(value: Any, path: _Path) -> JsonValue:
    if value is None:
        raise_conversion(
            "profile-scalar-invalid", json_pointer(path), "null is not a profile scalar"
        )
    if isinstance(value, (tuple, list)):
        return [_scalar(item, (*path, index)) for index, item in enumerate(value)]
    try:
        return scalar_to_relaxng_json(value, format="pyxsd-json")
    except PyXSDError as error:
        raise_conversion("profile-scalar-invalid", json_pointer(path), str(error))


def _attributes(
    node: RelaxNGElement, route: RouteContract | None, path: _Path
) -> dict[str, JsonValue]:
    names = list(route.attributes) if route is not None else []
    names.extend(sorted(set(node.attributes) - set(names)))
    result: dict[str, JsonValue] = {}
    for name in names:
        if name == "xmlns" or name.startswith(_XMLNS) or name not in node.attributes:
            continue
        key = "@" + expanded_to_eqname(name)
        field = route.attributes.get(name) if route is not None else None
        value = (
            node.attribute_value(name)
            if field is not None and field.scalar_contracts
            else node.attribute_lexical(name)
        )
        result[key] = _scalar(value, (*path, key))
    return result


def _child_route(
    route: RouteContract | None, child: RelaxNGElement, plan: ProjectionPlan
) -> tuple[RouteContract | None, bool]:
    if route is not None:
        field = route.children.get(child._name_)
        if field is not None:
            return plan.routes.get(field.child_route_id or ""), False
        uri, local = _split(child._name_)
        for wildcard in route.wildcards:
            if wildcard.role == "child" and wildcard.name_class.contains(uri, local):
                return plan.routes.get(wildcard.child_route_id or ""), True
    return None, True


def _export(
    node: RelaxNGElement,
    route: RouteContract | None,
    plan: ProjectionPlan,
    path: _Path,
    *,
    generic: bool = False,
) -> JsonValue:
    layout = _layout(route, generic)
    attribute_path = (*path, 0) if layout in ("sequence", "mixed") else path
    attributes = _attributes(node, route, attribute_path)
    has_attributes = route is None or bool(
        route.attributes or any(w.role == "attribute" for w in route.wildcards)
    )
    if layout in ("empty", "scalar"):
        if node.children:
            raise_conversion("profile-container-invalid", json_pointer(path), "unexpected children")
        if layout == "empty":
            text = node.text_value()
            if text and text.strip():
                raise_conversion("profile-container-invalid", json_pointer(path), "unexpected text")
            return attributes if has_attributes else ""
        wrapped = has_attributes or generic or bool(route and route.generic_reason)
        value = _scalar(
            node.scalar_value() if node.text_value() else "",
            (*path, "#content") if wrapped else path,
        )
        if wrapped:
            return {**attributes, "#content": value}
        return value

    if layout != "mixed" and any(
        text and text.strip()
        for text in (node.text_value(), *(child.tail_value() for child in node.children))
    ):
        raise_conversion("profile-container-invalid", json_pointer(path), "unexpected text")

    if layout == "record":
        result = dict(attributes)
        for child in node.children:
            key = expanded_to_eqname(child._name_)
            if key in result:
                raise_conversion(
                    "profile-container-invalid", json_pointer((*path, key)), "duplicate child"
                )
            child_route, child_generic = _child_route(route, child, plan)
            result[key] = _export(child, child_route, plan, (*path, key), generic=child_generic)
        return result

    if layout == "repeated":
        assert route is not None
        name = next(iter(route.children))
        key = expanded_to_eqname(name)
        items: list[JsonValue] = []
        item_path = (*path, key) if has_attributes else path
        for child in node.children:
            expanded_to_eqname(child._name_)
            if child._name_ != name:
                raise_conversion(
                    "profile-container-invalid", json_pointer(item_path), "unexpected child"
                )
            child_route, child_generic = _child_route(route, child, plan)
            items.append(
                _export(child, child_route, plan, (*item_path, len(items)), generic=child_generic)
            )
        return {**attributes, key: items} if has_attributes else items

    sequence: list[JsonValue] = []
    if attributes:
        sequence.append(dict(sorted(attributes.items())))
    if layout == "mixed" and node.text_value() is not None:
        sequence.append(node.text_value())
    for child in node.children:
        key = expanded_to_eqname(child._name_)
        child_route, child_generic = _child_route(route, child, plan)
        sequence.append(
            {
                key: _export(
                    child, child_route, plan, (*path, len(sequence), key), generic=child_generic
                )
            }
        )
        if layout == "mixed" and child.tail_value() is not None:
            sequence.append(child.tail_value())
    return sequence


def export_profile(
    root: RelaxNGElement, plan: ProjectionPlan, *, element: str | None = None
) -> dict[str, JsonValue]:
    """Export a current typed tree using only its compiled route contracts.

    ``element`` narrows the start name class, not a global declaration. Local
    selectors must be unambiguous across the grammar, including open classes.
    Public dispatch owns the document-level validity and freshness gate.
    """
    route = _root_route(root, plan, element)
    key = expanded_to_eqname(root._name_)
    return {key: _export(root, route, plan, (key,))}
