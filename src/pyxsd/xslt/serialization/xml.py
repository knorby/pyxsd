"""The XML, XHTML, and HTML output methods.

Sequence normalization happens before this module runs.  The items it sees are
either text (a Python string produced from text nodes and adjacent atomic
values) or the XDM nodes that survive normalization.  No output path adds a
library marker or consults the filesystem.
"""

from __future__ import annotations

import codecs
from collections.abc import Sequence

from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xslt.errors import XsltSerializationError
from pyxsd.xslt.serialization.parameters import SerializationParameters

XML_NS = "http://www.w3.org/XML/1998/namespace"

_HTML_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_HTML_BOOLEAN_ATTRIBUTES = frozenset(
    {
        "allowfullscreen",
        "async",
        "autofocus",
        "autoplay",
        "checked",
        "controls",
        "default",
        "defer",
        "disabled",
        "formnovalidate",
        "hidden",
        "inert",
        "ismap",
        "itemscope",
        "loop",
        "multiple",
        "muted",
        "nomodule",
        "novalidate",
        "open",
        "playsinline",
        "readonly",
        "required",
        "reversed",
        "selected",
        "shadowrootclonable",
        "shadowrootdelegatesfocus",
        "shadowrootserializable",
    }
)

NormalizedItem = str | XdmNode


def _not_well_formed(message: str) -> XsltSerializationError:
    return XsltSerializationError(message, code="SENR0001")


def _serialization_error(
    message: str, code: str, *, cause: BaseException | None = None
) -> XsltSerializationError:
    return XsltSerializationError(message, code=code, cause=cause)


def _encodable(value: str, codec: str) -> bool:
    try:
        value.encode(codec)
    except UnicodeEncodeError:
        return False
    return True


def _character_references(value: str, codec: str) -> str:
    if _encodable(value, codec):
        return value
    return "".join(
        character if _encodable(character, codec) else f"&#x{ord(character):X};"
        for character in value
    )


def _require_encodable(value: str, codec: str, label: str) -> None:
    try:
        value.encode(codec)
    except UnicodeEncodeError as error:
        raise _serialization_error(
            f"{label} contains a character not representable in {codec}",
            "SERE0008",
            cause=error,
        ) from error


def _expanded_element_name(token: str) -> str:
    if token.startswith("Q{"):
        return "{" + token[2:]
    return token


def _matches_element(token: str, element: XdmNode) -> bool:
    expanded = _expanded_element_name(token)
    if expanded.startswith("{"):
        return expanded == element.name
    if ":" in token:
        return False
    return element.namespace_uri is None and token == element.local_name


def _matches_any(tokens: tuple[str, ...], element: XdmNode) -> bool:
    return any(_matches_element(token, element) for token in tokens)


def _escape_text(value: str, *, html: bool, codec: str) -> str:
    escaped = value.replace("&", "&amp;").replace("<", "&lt;")
    if html:
        escaped = escaped.replace("]]>", "]]&gt;")
    else:
        escaped = escaped.replace("\r", "&#xD;").replace("]]>", "]]&gt;")
    return _character_references(escaped, codec)


def _escape_attribute(value: str, *, codec: str) -> str:
    escaped = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
        .replace("\t", "&#x9;")
        .replace("\n", "&#xA;")
        .replace("\r", "&#xD;")
    )
    return _character_references(escaped, codec)


def _escape_html_attribute(value: str, *, codec: str) -> str:
    escaped = value.replace("&", "&amp;").replace('"', "&quot;")
    return _character_references(escaped, codec)


def _cdata_section(value: str, *, codec: str) -> str:
    _require_encodable(value, codec, "a CDATA section")
    return "<![CDATA[" + value.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _namespace_sort_key(prefix: str) -> tuple[bool, str]:
    return (prefix != "", prefix)


def _new_prefix(scope: dict[str, str]) -> str:
    index = 0
    while f"ns{index}" in scope:
        index += 1
    return f"ns{index}"


def _declaration_changes(
    scope: dict[str, str], parent_scope: dict[str, str]
) -> list[tuple[str, str]]:
    changes: list[tuple[str, str]] = []
    for prefix in sorted(scope, key=_namespace_sort_key):
        if prefix == "xml":
            continue
        uri = scope[prefix]
        if uri == parent_scope.get(prefix, ""):
            continue
        changes.append((prefix, uri))
    if "" in parent_scope and "" not in scope:
        changes.append(("", ""))
    changes.sort(key=lambda declaration: _namespace_sort_key(declaration[0]))
    return changes


def _element_prefix(element: XdmNode, scope: dict[str, str]) -> str:
    uri = element.namespace_uri
    if not uri:
        return ""
    if uri == XML_NS:
        return "xml"
    if element.prefix and scope.get(element.prefix) == uri:
        return element.prefix
    if scope.get("") == uri:
        return ""
    prefix = _new_prefix(scope)
    scope[prefix] = uri
    return prefix


def _attribute_prefix(attribute: XdmNode, scope: dict[str, str]) -> str:
    uri = attribute.namespace_uri
    if not uri:
        return ""
    if uri == XML_NS:
        return "xml"
    if attribute.prefix and scope.get(attribute.prefix) == uri:
        return attribute.prefix
    prefix = _new_prefix(scope)
    scope[prefix] = uri
    return prefix


def _element_name(element: XdmNode, scope: dict[str, str], *, html: bool) -> str:
    local = element.local_name or element.name or ""
    if html:
        return local
    prefix = _element_prefix(element, scope)
    return f"{prefix}:{local}" if prefix else local


def _attribute_name(attribute: XdmNode, scope: dict[str, str], *, html: bool) -> str:
    local = attribute.local_name or attribute.name or ""
    if html:
        return local
    prefix = _attribute_prefix(attribute, scope)
    return f"{prefix}:{local}" if prefix else local


def _start_tag(
    element: XdmNode,
    scope: dict[str, str],
    parent_scope: dict[str, str],
    *,
    html: bool,
    codec: str,
) -> str:
    name = _element_name(element, scope, html=html)
    _require_encodable(name, codec, "an element name")
    attributes: list[str] = []
    for attribute in element.attributes:
        attribute_name = _attribute_name(attribute, scope, html=html)
        _require_encodable(attribute_name, codec, "an attribute name")
        value = (
            _escape_html_attribute(attribute.string_value, codec=codec)
            if html
            else _escape_attribute(attribute.string_value, codec=codec)
        )
        if html and attribute_name.lower() in _HTML_BOOLEAN_ATTRIBUTES:
            lowered = value.lower()
            if not value or lowered == attribute_name.lower():
                attributes.append(f" {attribute_name}")
                continue
        attributes.append(f' {attribute_name}="{value}"')
    declarations = (
        ""
        if html
        else "".join(
            f' xmlns{":" + prefix if prefix else ""}="{_escape_attribute(uri, codec=codec)}"'
            for prefix, uri in _declaration_changes(scope, parent_scope)
        )
    )
    return f"<{name}{declarations}{''.join(attributes)}"


def _text_node(
    value: str,
    element: XdmNode | None,
    parameters: SerializationParameters,
    *,
    html: bool,
    codec: str,
) -> str:
    if (
        not html
        and element is not None
        and _matches_any(parameters.cdata_section_elements, element)
    ):
        return _cdata_section(value, codec=codec)
    if html and element is not None and (element.local_name or "").lower() in ("script", "style"):
        return value
    return _escape_text(value, html=html, codec=codec)


def _render_node(
    node: XdmNode,
    depth: int,
    parent_scope: dict[str, str],
    parameters: SerializationParameters,
    *,
    method: str,
    codec: str,
) -> str:
    html = method == "html"
    if node.kind is NodeKind.COMMENT:
        _require_encodable(node.string_value, codec, "comment text")
        return f"<!--{node.string_value}-->"
    if node.kind is NodeKind.PROCESSING_INSTRUCTION:
        target = node.target or ""
        data = node.string_value
        _require_encodable(target, codec, "a processing-instruction target")
        _require_encodable(data, codec, "processing-instruction data")
        return f"<?{target} {data}?>" if data else f"<?{target}?>"
    if node.kind is NodeKind.TEXT:
        return _text_node(node.string_value, node.parent, parameters, html=html, codec=codec)
    if node.kind is not NodeKind.ELEMENT:
        raise _not_well_formed(f"cannot serialize a {node.kind.value} node in content")
    return _render_element(node, depth, parent_scope, parameters, method=method, codec=codec)


def _render_element(
    element: XdmNode,
    depth: int,
    parent_scope: dict[str, str],
    parameters: SerializationParameters,
    *,
    method: str,
    codec: str,
) -> str:
    html = method == "html"
    scope = dict(element.in_scope_namespaces)
    start = _start_tag(element, scope, parent_scope, html=html, codec=codec)
    children = list(element.children)
    if parameters.indent:
        children = [
            child
            for child in children
            if not (child.kind is NodeKind.TEXT and not child.string_value.strip())
        ]
    if not children:
        if html:
            name = _element_name(element, scope, html=True)
            if (element.local_name or "").lower() in _HTML_VOID_ELEMENTS:
                return f"{start}>"
            return f"{start}></{name}>"
        name = _element_name(element, scope, html=False)
        return f"{start} />" if method == "xhtml" else f"{start}/>"

    name = _element_name(element, scope, html=html)

    def render_child(child: XdmNode) -> str:
        return _render_node(child, depth + 1, scope, parameters, method=method, codec=codec)

    if parameters.indent and all(child.kind is not NodeKind.TEXT for child in children):
        inner = "".join(f"\n{'  ' * (depth + 1)}{render_child(child)}" for child in children)
        return f"{start}>{inner}\n{'  ' * depth}</{name}>"
    return f"{start}>{''.join(render_child(child) for child in children)}</{name}>"


def _doctype_literal(value: str, *, codec: str) -> str:
    _require_encodable(value, codec, "a doctype identifier")
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace('"', "&quot;") + '"'


def _doctype(root: XdmNode, parameters: SerializationParameters, *, html: bool, codec: str) -> str:
    assert parameters.doctype_system is not None
    scope = dict(root.in_scope_namespaces)
    name = _element_name(root, scope, html=html)
    _require_encodable(name, codec, "a doctype name")
    system = _doctype_literal(parameters.doctype_system, codec=codec)
    if parameters.doctype_public is not None:
        public = _doctype_literal(parameters.doctype_public, codec=codec)
        return f"<!DOCTYPE {name} PUBLIC {public} {system}>"
    return f"<!DOCTYPE {name} SYSTEM {system}>"


def _validate_xml_shape(items: Sequence[NormalizedItem]) -> None:
    elements = [
        item for item in items if isinstance(item, XdmNode) and item.kind is NodeKind.ELEMENT
    ]
    if len(elements) > 1:
        raise _serialization_error(
            "the XML serialization has more than one top-level element", "SERE0003"
        )


def _xml_declaration(parameters: SerializationParameters) -> str:
    standalone = ""
    if parameters.standalone is not None:
        standalone = ' standalone="yes"' if parameters.standalone else ' standalone="no"'
    return f'<?xml version="{parameters.version}" encoding="{parameters.encoding}"{standalone}?>'


def serialize_markup(
    items: Sequence[NormalizedItem], parameters: SerializationParameters, *, method: str
) -> str:
    """Serialize normalized *items* with the XML, XHTML, or HTML method."""
    html = method == "html"
    if not items:
        return ""
    codec = codecs.lookup(parameters.encoding).name
    if not html:
        _validate_xml_shape(items)
    if parameters.indent:
        items = [item for item in items if not (isinstance(item, str) and not item.strip())]

    output: list[str] = []
    if not html and not parameters.omit_xml_declaration:
        output.append(_xml_declaration(parameters))
        if parameters.indent:
            output.append("\n")

    root = next(
        (item for item in items if isinstance(item, XdmNode) and item.kind is NodeKind.ELEMENT),
        None,
    )
    doctype = (
        _doctype(root, parameters, html=html, codec=codec)
        if root is not None and parameters.doctype_system is not None
        else None
    )
    root_scope: dict[str, str] = {"xml": XML_NS}
    separator = parameters.item_separator
    for index, item in enumerate(items):
        if index:
            output.append("\n" if parameters.indent and separator is None else (separator or ""))
        if isinstance(item, str):
            output.append(_text_node(item, None, parameters, html=html, codec=codec))
            continue
        if item.kind is NodeKind.ELEMENT and doctype is not None:
            output.append(doctype)
            if parameters.indent:
                output.append("\n")
            doctype = None
        output.append(_render_node(item, 0, root_scope, parameters, method=method, codec=codec))
    return "".join(output)
