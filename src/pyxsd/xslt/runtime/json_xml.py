"""Read the XSLT 3.0 XML representation of JSON directly from XDM."""

from __future__ import annotations

import math
from types import MappingProxyType

from pyxsd import xsd_data_types as _xsd
from pyxsd.xdm import XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xslt.errors import XsltDynamicError

__all__ = ["xml_to_json"]

_FN_NS = "http://www.w3.org/2005/xpath-functions"
_XML_WHITESPACE = " \t\r\n"
_STRING_ESCAPES = MappingProxyType(
    {
        **{code: f"\\u{code:04X}" for code in range(0x20)},
        **{code: f"\\u{code:04X}" for code in range(0x7F, 0xA0)},
        ord('"'): r"\"",
        ord("\\"): r"\\",
        ord("\b"): r"\b",
        ord("\f"): r"\f",
        ord("\n"): r"\n",
        ord("\r"): r"\r",
        ord("\t"): r"\t",
    }
)


def xml_to_json(input_node: XdmNode | None, options: XdmMap | None = None) -> str | None:
    """Write compact JSON from a node in the XSLT 3.0 function vocabulary.

    ``None`` represents an empty input sequence. A document must have one
    element child and no text children. Invalid structure or scalar content
    raises :class:`~pyxsd.xslt.errors.XsltDynamicError` (``FOJS0006``).
    Output is compact regardless of the supplied options.
    """
    if input_node is None:
        return None
    if input_node.kind is NodeKind.DOCUMENT:
        elements = [child for child in input_node.children if child.kind is NodeKind.ELEMENT]
        if len(elements) != 1 or any(child.kind is NodeKind.TEXT for child in input_node.children):
            raise _invalid_xml("the document must contain one element and no text nodes")
        input_node = elements[0]
    tokens: list[str] = []
    _write_value(input_node, tokens)
    return "".join(tokens)


def _invalid_xml(reason: str) -> XsltDynamicError:
    return XsltDynamicError(f"Invalid XML representation of JSON: {reason}", code="FOJS0006")


def _quote(value: str) -> str:
    return '"' + value.translate(_STRING_ESCAPES) + '"'


def _write_value(node: XdmNode, tokens: list[str]) -> None:
    if node.kind is not NodeKind.ELEMENT or node.namespace_uri != _FN_NS:
        raise _invalid_xml("expected an element in the XPath functions namespace")

    kind = node.local_name
    if kind == "array":
        _write_container(node, tokens, is_map=False)
    elif kind == "map":
        _write_container(node, tokens, is_map=True)
    elif kind in ("null", "string", "boolean", "number"):
        tokens.append(_scalar_token(node, kind))
    else:
        raise _invalid_xml(f"unexpected element {node.name!r}")


def _write_container(node: XdmNode, tokens: list[str], *, is_map: bool) -> None:
    opening, closing = ("{", "}") if is_map else ("[", "]")
    if any(
        child.kind is NodeKind.TEXT and child.string_value.strip(_XML_WHITESPACE)
        for child in node.children
    ):
        raise _invalid_xml(f"{node.local_name} cannot contain non-whitespace text")

    tokens.append(opening)
    first = True
    for child in node.children:
        if child.kind is not NodeKind.ELEMENT:
            continue
        if not first:
            tokens.append(",")
        first = False
        key = child.attribute("key")
        if is_map:
            if key is None:
                raise _invalid_xml("every map member must have a key attribute")
            tokens.extend((_quote(key.string_value), ":"))
        elif key is not None:
            raise _invalid_xml("an array member cannot have a key attribute")
        _write_value(child, tokens)
    tokens.append(closing)


def _scalar_token(node: XdmNode, kind: str) -> str:
    if any(child.kind is NodeKind.ELEMENT for child in node.children):
        raise _invalid_xml(f"{kind} cannot contain child elements")
    text = node.string_value
    if kind == "string":
        return _quote(text)
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
    return str(number).removesuffix(".0")
