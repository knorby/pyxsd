"""Read the XSLT 3.0 XML representation of JSON directly from XDM."""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import cast

from pyxsd import xsd_data_types as _xsd
from pyxsd.xdm import XdmArray, XdmAtomic, XdmItem, XdmMap, XdmSequence, XdmSequenceType
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xdm.values import _coerce_sequence, canonical_float_string
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.serialization.json_lexical import decode_json_escapes, escape_json_string

__all__ = ["xml_to_json"]

_FN_NS = "http://www.w3.org/2005/xpath-functions"
_XML_WHITESPACE = " \t\r\n"
_INDENT = XdmAtomic.string("indent")
_INDENT_TYPE = XdmSequenceType("xs:boolean", "one")


def xml_to_json(input_node: XdmNode | None, options: XdmMap | None = None) -> str | None:
    """Write JSON from a node in the XPath functions vocabulary.

    ``None`` represents an empty input sequence. A document must have one
    element child and no text children. Invalid structure or scalar content
    raises :class:`~pyxsd.xslt.errors.XsltDynamicError` (``FOJS0006``);
    malformed pre-escaped strings and keys raise ``FOJS0007``.
    The boolean ``indent`` option enables two-space indentation. Failure to
    convert its value to an ``xs:boolean`` raises ``XPTY0004``. Other options
    are ignored.
    """
    if input_node is None:
        return None
    if input_node.kind is NodeKind.DOCUMENT:
        elements = [child for child in input_node.children if child.kind is NodeKind.ELEMENT]
        if len(elements) != 1 or any(child.kind is NodeKind.TEXT for child in input_node.children):
            raise _invalid_xml("the document must contain one element and no text nodes")
        input_node = elements[0]
    tokens: list[str] = []
    with _xsd.whitespace_mode("xsd"):
        depth = 0 if _indent_enabled(options) else None
        _write_value(input_node, tokens, depth=depth, map_member=None)
    return "".join(tokens)


def _invalid_xml(reason: str) -> XsltDynamicError:
    return XsltDynamicError(f"Invalid XML representation of JSON: {reason}", code="FOJS0006")


def _option_atoms(sequence: XdmSequence) -> Iterator[XdmItem]:
    for item in sequence:
        if isinstance(item, XdmNode):
            yield from item.typed_value
        elif isinstance(item, XdmArray):
            for member in item.members:
                yield from _option_atoms(member)
        else:
            yield item


def _indent_enabled(options: XdmMap | None) -> bool:
    if options is None or not options.contains(_INDENT):
        return False

    try:
        (value,) = _coerce_sequence(tuple(_option_atoms(options.get(_INDENT))), _INDENT_TYPE)
    except TypeError as exc:
        raise XsltDynamicError("indent must be convertible to xs:boolean", code="XPTY0004") from exc
    return bool(cast(XdmAtomic, value).value)


def _attribute_flag(node: XdmNode, name: str) -> bool:
    attribute = node.attribute(name)
    if attribute is None:
        return False
    try:
        return bool(_xsd.Boolean(attribute.string_value))
    except TypeError as exc:
        raise _invalid_xml(f"the {name} attribute must have an xs:boolean value") from exc


def _escape(value: str, *, preescaped: bool) -> str:
    try:
        return escape_json_string(value, preescaped=preescaped)
    except ValueError as exc:
        raise XsltDynamicError(str(exc), code="FOJS0007") from exc


def _write_value(
    node: XdmNode, tokens: list[str], *, depth: int | None, map_member: bool | None
) -> None:
    if node.kind is not NodeKind.ELEMENT or node.namespace_uri != _FN_NS:
        raise _invalid_xml("expected an element in the XPath functions namespace")

    kind = node.local_name
    allowed = {"escaped"} if kind == "string" else set()
    if map_member is not False:
        # Root key metadata is permitted and ignored; a map consumes its member's key.
        allowed.update(("key", "escaped-key"))
    for attribute in node.attributes:
        if attribute.namespace_uri in (None, _FN_NS) and attribute.name not in allowed:
            raise _invalid_xml(f"unexpected attribute {attribute.name!r} on {node.name!r}")

    if kind == "array":
        _write_container(node, tokens, depth=depth, is_map=False, unique_raw_keys=False)
    elif kind == "map":
        _write_container(node, tokens, depth=depth, is_map=True, unique_raw_keys=map_member is True)
    elif kind in ("null", "string", "boolean", "number"):
        tokens.append(_scalar_token(node, kind))
    else:
        raise _invalid_xml(f"unexpected element {node.name!r}")


def _write_container(
    node: XdmNode,
    tokens: list[str],
    *,
    depth: int | None,
    is_map: bool,
    unique_raw_keys: bool,
) -> None:
    opening, closing = ("{", "}") if is_map else ("[", "]")
    tokens.append(opening)
    child_depth = None if depth is None else depth + 1
    seen: set[str] = set()
    # The schema also constrains raw key spellings in a map directly inside a map.
    raw_keys: set[str] | None = set() if unique_raw_keys else None
    first = True
    for child in node.children:
        if child.kind is not NodeKind.ELEMENT:
            if child.kind is NodeKind.TEXT and child.string_value.strip(_XML_WHITESPACE):
                raise _invalid_xml(f"{node.local_name} cannot contain non-whitespace text")
            continue
        if not first:
            tokens.append(",")
        first = False
        if child_depth is not None:
            tokens.append("\n" + "  " * child_depth)
        if is_map:
            key = _map_key(child, seen, raw_keys)
            tokens.extend((f'"{key}"', ": " if depth is not None else ":"))
        _write_value(child, tokens, depth=child_depth, map_member=is_map)
    if depth is not None and not first:
        tokens.append("\n" + "  " * depth)
    tokens.append(closing)


def _map_key(child: XdmNode, seen: set[str], raw_keys: set[str] | None) -> str:
    attribute = child.attribute("key")
    if attribute is None:
        raise _invalid_xml("every map member must have a key attribute")
    key = attribute.string_value
    preescaped = _attribute_flag(child, "escaped-key")
    escaped = _escape(key, preescaped=preescaped)
    normalized = decode_json_escapes(escaped) if preescaped else key
    if normalized in seen:
        raise _invalid_xml(f"duplicate map key {normalized!r}")
    seen.add(normalized)
    if raw_keys is not None:
        if key in raw_keys:
            raise _invalid_xml(f"duplicate raw map key {key!r}")
        raw_keys.add(key)
    return escaped


def _scalar_token(node: XdmNode, kind: str) -> str:
    if any(child.kind is NodeKind.ELEMENT for child in node.children):
        raise _invalid_xml(f"{kind} cannot contain child elements")
    text = node.string_value
    if kind == "string":
        escaped = _escape(text, preescaped=_attribute_flag(node, "escaped"))
        return f'"{escaped}"'
    if kind == "null":
        if text:
            raise _invalid_xml("null cannot contain text")
        return "null"
    if kind == "boolean":
        try:
            return str(_xsd.Boolean(text))
        except TypeError as exc:
            raise _invalid_xml("expected an xs:boolean value") from exc

    try:
        number = _xsd.Double(text)
    except TypeError as exc:
        raise _invalid_xml("expected an xs:double value") from exc
    if not math.isfinite(number):
        raise _invalid_xml("expected a finite number")
    return canonical_float_string(number, single=False)
