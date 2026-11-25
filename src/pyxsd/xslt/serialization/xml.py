"""The XML, XHTML, and HTML output methods.

Sequence normalization happens before this module runs.  The items it sees are
either text (a Python string produced from text nodes and adjacent atomic
values) or the XDM nodes that survive normalization.  No output path adds a
library marker or consults the filesystem.
"""

from __future__ import annotations

import codecs
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence

from pyxsd.xdm import XdmAtomic, XdmItem
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xslt.errors import XsltSerializationError
from pyxsd.xslt.serialization.characters import (
    CharacterMap,
    apply_mapped,
    character_map_index,
    normalize_text,
)
from pyxsd.xslt.serialization.parameters import SerializationParameters

XML_NS = "http://www.w3.org/XML/1998/namespace"
XHTML_NS = "http://www.w3.org/1999/xhtml"
SVG_NS = "http://www.w3.org/2000/svg"
MATHML_NS = "http://www.w3.org/1998/Math/MathML"
_DEFAULT_MEDIA_TYPES = {"html": "text/html", "xhtml": "text/html"}

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
_XHTML_LEGACY_VOID_ELEMENTS = frozenset({"basefont", "frame", "isindex"})
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

#: Serialization 3.1 Appendix D: URI-valued HTML/XHTML attributes and the
#: elements that declare them as ``%URI``.  ``name`` and ``value`` are URI
#: attributes only on the elements listed here, so a name-only test would
#: escape ordinary attributes.
_URI_ATTRIBUTES: Mapping[str, frozenset[str]] = {
    "action": frozenset({"form"}),
    "archive": frozenset({"object"}),
    "background": frozenset({"body"}),
    "cite": frozenset({"blockquote", "del", "ins", "q"}),
    "classid": frozenset({"object"}),
    "codebase": frozenset({"applet", "object"}),
    "data": frozenset({"object"}),
    "datasrc": frozenset(
        {"button", "div", "input", "object", "select", "span", "table", "textarea"}
    ),
    "for": frozenset({"script"}),
    "formaction": frozenset({"button", "input"}),
    "href": frozenset({"a", "area", "base", "link"}),
    "icon": frozenset({"command"}),
    "longdesc": frozenset({"frame", "iframe", "img"}),
    "manifest": frozenset({"html"}),
    "name": frozenset({"a"}),
    "poster": frozenset({"video"}),
    "profile": frozenset({"head"}),
    "src": frozenset(
        {
            "audio",
            "embed",
            "frame",
            "iframe",
            "img",
            "input",
            "script",
            "source",
            "track",
            "video",
        }
    ),
    "usemap": frozenset({"img", "input", "object"}),
}

_URI_SAFE = frozenset(chr(code) for code in range(0x20, 0x7F))


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


def _is_html5(parameters: SerializationParameters, method: str) -> bool:
    if method not in ("html", "xhtml"):
        return False
    version = parameters.html_version
    if version is None and method == "html":
        version = parameters.version
    if version is None:
        return False
    try:
        return float(version) >= 5.0
    except ValueError:  # pragma: no cover - validated parameters
        return False


def _is_html_element(element: XdmNode, *, method: str, html5: bool = False) -> bool:
    """Whether *element* is serialized as an HTML element by *method*.

    The HTML method treats null-namespace elements and elements in the XHTML
    namespace as HTML elements.  The XHTML method recognizes the XHTML
    namespace, and an ``html-version`` of 5.0 or later additionally recognizes
    null-namespace elements (Serialization 3.1 section 6).
    """
    if method == "html":
        return element.namespace_uri in (None, XHTML_NS)
    if method != "xhtml":
        return False
    if element.namespace_uri == XHTML_NS:
        return True
    return html5 and element.namespace_uri is None


def _is_head(element: XdmNode, *, method: str, html5: bool = False) -> bool:
    return (
        _is_html_element(element, method=method, html5=html5)
        and (element.local_name or "").lower() == "head"
    )


def _is_content_type_meta(node: XdmNode, *, method: str, html5: bool = False) -> bool:
    if node.kind is not NodeKind.ELEMENT or not _is_html_element(node, method=method, html5=html5):
        return False
    if (node.local_name or "").lower() != "meta":
        return False
    return any(
        (attribute.local_name or "").lower() == "http-equiv"
        and attribute.string_value.strip().lower() == "content-type"
        for attribute in node.attributes
    )


def _content_type_meta(
    head: XdmNode,
    scope: dict[str, str],
    parameters: SerializationParameters,
    *,
    method: str,
    codec: str,
    html5: bool = False,
    characters: CharacterMap,
) -> str:
    """The content-type ``meta`` element inserted into *head*."""
    media_type = (
        parameters.media_type if parameters.media_type is not None else _DEFAULT_MEDIA_TYPES[method]
    )
    content = f"{media_type}; charset={parameters.encoding}"
    form = parameters.normalization_form
    content = apply_mapped(
        content,
        characters,
        lambda run: normalize_text(run, form),
    )
    if method == "html":
        escaped = _escape_html_attribute(content, codec=codec)
        return f'<meta http-equiv="Content-Type" content="{escaped}">'
    escaped = _escape_xhtml_attribute(content, codec=codec)
    declarations = (
        f' xmlns="{XHTML_NS}"'
        if not html5 and head.namespace_uri == XHTML_NS and scope.get("") != XHTML_NS
        else ""
    )
    return f'<meta{declarations} http-equiv="Content-Type" content="{escaped}" />'


def _undeclares_prefixes(parameters: SerializationParameters, *, html: bool) -> bool:
    if html or not parameters.undeclare_prefixes:
        return False
    if float(parameters.version) > 1.0:
        return True
    raise _serialization_error(
        "undeclare-prefixes requires an XML version later than 1.0", "SEPM0010"
    )


def _escape_text(value: str, *, html: bool, codec: str) -> str:
    escaped = value.replace("&", "&amp;").replace("<", "&lt;")
    if html:
        escaped = escaped.replace("]]>", "]]&gt;")
    else:
        escaped = escaped.replace(">", "&gt;").replace("\r", "&#xD;")
    return _character_references(escaped, codec)


def _escape_xhtml_text(value: str, *, codec: str) -> str:
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = escaped.replace("\r", "&#xD;")
    return _character_references(_c1_references(escaped), codec)


def _c1_references(value: str) -> str:
    if not any(0x7F <= ord(character) <= 0x9F for character in value):
        return value
    return "".join(
        f"&#x{ord(character):X};" if 0x7F <= ord(character) <= 0x9F else character
        for character in value
    )


def _reject_html_control(value: str, *, label: str) -> None:
    for character in value:
        if 0x7F <= ord(character) <= 0x9F:
            raise _serialization_error(
                f"{label} contains the control character U+{ord(character):04X} "
                "the HTML output method cannot represent",
                "SERE0014",
            )


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
    _reject_html_control(value, label="an attribute value")
    escaped = value.replace("&", "&amp;").replace('"', "&quot;")
    return _character_references(escaped, codec)


def _escape_xhtml_attribute(value: str, *, codec: str) -> str:
    escaped = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&#34;")
        .replace("\t", "&#x9;")
        .replace("\n", "&#xA;")
        .replace("\r", "&#xD;")
    )
    return _character_references(_c1_references(escaped), codec)


def escape_html_uri(value: str) -> str:
    """``fn:escape-html-uri``: percent-encode every character outside 32-126."""
    if all(character in _URI_SAFE for character in value):
        return value
    return "".join(
        character
        if character in _URI_SAFE
        else "".join(f"%{byte:02X}" for byte in character.encode("utf-8"))
        for character in value
    )


def _is_uri_attribute(
    element: XdmNode, attribute: XdmNode, *, method: str, html5: bool = False
) -> bool:
    if not _is_html_element(element, method=method, html5=html5):
        return False
    attribute_name = (attribute.local_name or "").lower()
    element_name = (element.local_name or "").lower()
    return element_name in _URI_ATTRIBUTES.get(attribute_name, frozenset())


def text_value(
    value: str,
    parameters: SerializationParameters,
    *,
    method: str,
    codec: str,
    characters: CharacterMap,
) -> str:
    """Character-map, normalize, then escape one text node value."""
    form = parameters.normalization_form
    if form == "fully-normalized" and value and unicodedata.combining(value[0]):
        raise _serialization_error(
            "a fully-normalized result cannot begin with a combining character",
            "SERE0012",
        )
    if method == "html":
        numeric_c1 = _is_html5(parameters, "html")
        if not numeric_c1:
            _reject_html_control(value, label="text")

        def escape(run: str) -> str:
            escaped = _escape_text(normalize_text(run, form), html=True, codec=codec)
            return _c1_references(escaped) if numeric_c1 else escaped
    elif method == "xhtml":

        def escape(run: str) -> str:
            return _escape_xhtml_text(normalize_text(run, form), codec=codec)

    else:

        def escape(run: str) -> str:
            return _escape_text(normalize_text(run, form), html=False, codec=codec)

    return apply_mapped(value, characters, escape)


def attribute_value(
    value: str,
    parameters: SerializationParameters,
    *,
    method: str = "xml",
    codec: str,
    characters: CharacterMap,
    uri_attribute: bool = False,
) -> str:
    """Character-map, normalize, then escape one attribute value.

    A URI attribute under HTML/XHTML escaping is normalized to NFC and
    percent-escaped by ``fn:escape-html-uri`` before normal output escaping;
    character maps are not applied to it.
    """
    escape: Callable[..., str]
    if method == "html":
        numeric_c1 = _is_html5(parameters, "html")

        def escape(raw: str, *, codec: str) -> str:
            escaped = _escape_html_attribute(raw, codec=codec)
            return _c1_references(escaped) if numeric_c1 else escaped

    elif method == "xhtml":
        escape = _escape_xhtml_attribute
    else:
        escape = _escape_attribute
    if uri_attribute and parameters.escape_uri_attributes:
        return escape(escape_html_uri(normalize_text(value, "NFC")), codec=codec)
    form = parameters.normalization_form
    return apply_mapped(
        value,
        characters,
        lambda run: escape(normalize_text(run, form), codec=codec),
    )


def _cdata_section(value: str, *, codec: str) -> str:
    """One CDATA-section serialization, split around unrepresentable characters.

    Serialization 3.1 section 8.5 lets the serializer end the CDATA section
    around a character the encoding cannot represent and output a character
    reference instead.  The same split keeps ``]]>`` from terminating the
    section early.
    """
    pieces: list[str] = []
    pending: list[str] = []
    for character in value.replace("]]>", "]]]]><![CDATA[>"):
        if not _encodable(character, codec):
            if pending:
                pieces.append(_cdata_piece("".join(pending)))
                pending.clear()
            pieces.append(f"&#x{ord(character):X};")
            continue
        pending.append(character)
    if pending:
        pieces.append(_cdata_piece("".join(pending)))
    return "".join(pieces)


def _cdata_piece(value: str) -> str:
    return "<![CDATA[" + value + "]]>"


def _namespace_sort_key(prefix: str) -> tuple[bool, str]:
    return (prefix != "", prefix)


def _new_prefix(scope: dict[str, str]) -> str:
    index = 0
    while f"ns{index}" in scope:
        index += 1
    return f"ns{index}"


def _declaration_changes(
    scope: dict[str, str], parent_scope: dict[str, str], *, undeclare: bool
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
    if undeclare:
        for prefix in sorted(parent_scope, key=_namespace_sort_key):
            if not prefix or prefix == "xml" or prefix in scope or not parent_scope[prefix]:
                continue
            changes.append((prefix, ""))
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
    parameters: SerializationParameters,
    *,
    method: str,
    codec: str,
    characters: CharacterMap,
    undeclare: bool,
    html5: bool,
    extra_declarations: Mapping[str, str] = {},
    root_declaration: bool = False,
) -> str:
    html = method == "html"
    html_element = _is_html_element(element, method=method, html5=html5)
    html_namespace = element.namespace_uri in (None, XHTML_NS, SVG_NS, MATHML_NS)
    name_html = (html_element or (html5 and html_namespace) or not html5) if html else html5
    name = _element_name(element, scope, html=name_html)
    _require_encodable(name, codec, "an element name")
    attributes: list[str] = []
    attribute_declarations: list[tuple[str, str]] = []
    for attribute in element.attributes:
        attribute_name = _attribute_name(attribute, scope, html=html and not html5)
        _require_encodable(attribute_name, codec, "an attribute name")
        if html5 and attribute.namespace_uri is not None and attribute.prefix:
            attribute_declarations.append((attribute.prefix, attribute.namespace_uri))
        value = attribute_value(
            attribute.string_value,
            parameters,
            method=method,
            codec=codec,
            characters=characters,
            uri_attribute=_is_uri_attribute(element, attribute, method=method, html5=html5),
        )
        if method == "html" and html_element and attribute_name.lower() in _HTML_BOOLEAN_ATTRIBUTES:
            lowered = value.lower()
            if not value or lowered == attribute_name.lower():
                attributes.append(f" {attribute_name}")
                continue
        attributes.append(f' {attribute_name}="{value}"')
    attribute_decls = "".join(
        f' xmlns:{prefix}="{_escape_attribute(uri, codec=codec)}"'
        for prefix, uri in _unique_declarations(attribute_declarations)
        if prefix not in extra_declarations
    )
    extra_decls = (
        "".join(
            f' xmlns:{prefix}="{_escape_attribute(uri, codec=codec)}"'
            for prefix, uri in _unique_declarations(list(extra_declarations.items()))
        )
        if root_declaration
        else ""
    )
    if html5:
        if not html:
            declarations = _html5_declarations(element, scope, parent_scope, codec=codec)
            declarations += attribute_decls
        elif html_element:
            declarations = extra_decls + attribute_decls
        elif html_namespace:
            uri = element.namespace_uri
            declarations = (
                f' xmlns="{_escape_attribute(uri, codec=codec)}"' if uri else ""
            ) + attribute_decls
        else:
            element_declaration = (
                f' xmlns:{element.prefix}="{_escape_attribute(element.namespace_uri or "", codec=codec)}"'
                if (
                    element.prefix
                    and element.namespace_uri
                    and element.prefix not in extra_declarations
                )
                else ""
            )
            declarations = element_declaration + attribute_decls
    elif html:
        declarations = ""
    else:
        declarations = "".join(
            f' xmlns{":" + prefix if prefix else ""}="{_escape_attribute(uri, codec=codec)}"'
            for prefix, uri in _declaration_changes(scope, parent_scope, undeclare=undeclare)
        )
    return f"<{name}{declarations}{''.join(attributes)}"


def _html5_root_prefix_declarations(root: XdmNode) -> dict[str, str]:
    """Prefix declarations HTML 5.0 hoists to its root ``html`` element.

    Foreign-namespace elements keep their lexical prefix in HTML serialization,
    but the declaration belongs to the root HTML element so the foreign element
    itself is serialized as a plain start tag.  SVG, MathML, XHTML, and
    no-namespace elements are serialized by default namespace instead.
    """
    special = {None, XHTML_NS, SVG_NS, MATHML_NS}

    def visit(node: XdmNode, *, main_flow: bool) -> None:
        if node.kind is not NodeKind.ELEMENT:
            return
        html_element = node.namespace_uri in (None, XHTML_NS)
        if node.namespace_uri not in special and node.prefix and node.namespace_uri:
            declarations.setdefault(node.prefix, node.namespace_uri)
        if main_flow and html_element:
            for attribute in node.attributes:
                if attribute.namespace_uri and attribute.prefix:
                    declarations.setdefault(attribute.prefix, attribute.namespace_uri)
        child_main_flow = main_flow and html_element
        for child in node.children:
            visit(child, main_flow=child_main_flow)

    declarations: dict[str, str] = {}
    visit(root, main_flow=True)
    return declarations


def _unique_declarations(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for prefix, uri in pairs:
        seen.setdefault(prefix, uri)
    return list(seen.items())


def _html5_declarations(
    element: XdmNode,
    scope: dict[str, str],
    parent_scope: dict[str, str],
    *,
    codec: str,
) -> str:
    """The single default-namespace declaration HTML 5.0 serialization emits.

    HTML 5.0 serialization drops prefixes: every element keeps its local name,
    and a namespace change is expressed by a default ``xmlns`` declaration on
    that element.  Prefixed declarations are never carried over.
    """
    uri = element.namespace_uri or ""
    parent = parent_scope.get("", "")
    if uri == parent:
        return ""
    return f' xmlns="{_escape_attribute(uri, codec=codec)}"' if uri else ' xmlns=""'


def _scope_for(element: XdmNode, parent_scope: dict[str, str], *, html5: bool) -> dict[str, str]:
    scope = dict(element.in_scope_namespaces)
    if html5:
        scope[""] = element.namespace_uri or ""
    return scope


def _text_node(
    value: str,
    element: XdmNode | None,
    parameters: SerializationParameters,
    *,
    method: str,
    codec: str,
    characters: CharacterMap,
    html5: bool,
) -> str:
    html = method == "html"
    if (
        not html
        and element is not None
        and _matches_any(parameters.cdata_section_elements, element)
    ):
        return _cdata_section(normalize_text(value, parameters.normalization_form), codec=codec)
    if (
        html
        and element is not None
        and _is_html_element(element, method=method, html5=html5)
        and (element.local_name or "").lower() in ("script", "style")
    ):
        form = parameters.normalization_form
        return apply_mapped(
            value,
            characters,
            lambda run: normalize_text(run, form),
        )
    return text_value(value, parameters, method=method, codec=codec, characters=characters)


def _render_node(
    node: XdmNode,
    depth: int,
    parent_scope: dict[str, str],
    parameters: SerializationParameters,
    *,
    method: str,
    codec: str,
    characters: CharacterMap,
    undeclare: bool,
    suppressed: bool,
    html5: bool,
    extra_declarations: Mapping[str, str] = {},
    root_declaration: bool = False,
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
        if html and ">" in data:
            raise _serialization_error(
                "an HTML processing instruction cannot contain '>'",
                "SERE0015",
            )
        return f"<?{target} {data}?>" if data else f"<?{target}?>"
    if node.kind is NodeKind.TEXT:
        if node.disable_output_escaping:
            _require_encodable(node.string_value, codec, "unescaped text")
            return node.string_value
        return _text_node(
            node.string_value,
            node.parent,
            parameters,
            method=method,
            codec=codec,
            characters=characters,
            html5=html5,
        )
    if node.kind is not NodeKind.ELEMENT:
        raise _not_well_formed(f"cannot serialize a {node.kind.value} node in content")
    return _render_element(
        node,
        depth,
        parent_scope,
        parameters,
        method=method,
        codec=codec,
        characters=characters,
        undeclare=undeclare,
        suppressed=suppressed,
        html5=html5,
        extra_declarations=extra_declarations,
        root_declaration=root_declaration,
    )


def _render_element(
    element: XdmNode,
    depth: int,
    parent_scope: dict[str, str],
    parameters: SerializationParameters,
    *,
    method: str,
    codec: str,
    characters: CharacterMap,
    undeclare: bool,
    suppressed: bool,
    html5: bool,
    extra_declarations: Mapping[str, str] = {},
    root_declaration: bool = False,
) -> str:
    html = method == "html"
    html_element = _is_html_element(element, method=method, html5=html5)
    suppress = suppressed or _matches_any(parameters.suppress_indentation, element)
    scope = _scope_for(element, parent_scope, html5=html5)
    start = _start_tag(
        element,
        scope,
        parent_scope,
        parameters,
        method=method,
        codec=codec,
        characters=characters,
        undeclare=undeclare,
        html5=html5,
        extra_declarations=extra_declarations,
        root_declaration=root_declaration,
    )
    children = list(element.children)
    content_type = (
        _content_type_meta(
            element,
            scope,
            parameters,
            method=method,
            codec=codec,
            html5=html5,
            characters=characters,
        )
        if parameters.include_content_type
        and method in ("html", "xhtml")
        and _is_head(element, method=method, html5=html5)
        else None
    )
    if content_type is not None:
        children = [
            child
            for child in children
            if not _is_content_type_meta(child, method=method, html5=html5)
        ]
    if parameters.indent and not suppress:
        children = [
            child
            for child in children
            if not (child.kind is NodeKind.TEXT and not child.string_value.strip())
        ]
    html_namespace = element.namespace_uri in (None, XHTML_NS, SVG_NS, MATHML_NS)
    name_html = (html_element or (html5 and html_namespace) or not html5) if html else html5
    name = _element_name(element, scope, html=name_html)
    if not children and content_type is None:
        if html_element:
            local = (element.local_name or "").lower()
            void = local in _HTML_VOID_ELEMENTS or (
                method == "xhtml" and not html5 and local in _XHTML_LEGACY_VOID_ELEMENTS
            )
            if void:
                return f"{start} />" if method == "xhtml" else f"{start}>"
            return f"{start}></{name}>"
        if html:
            return f"{start}>"
        return f"{start} />" if method == "xhtml" else f"{start}/>"

    def render_child(child: XdmNode) -> str:
        return _render_node(
            child,
            depth + 1,
            scope,
            parameters,
            method=method,
            codec=codec,
            characters=characters,
            undeclare=undeclare,
            suppressed=suppress,
            html5=html5,
            extra_declarations=extra_declarations,
        )

    rendered = [render_child(child) for child in children]
    if content_type is not None:
        insertion = next(
            (index for index, child in enumerate(children) if child.kind is NodeKind.ELEMENT),
            len(children),
        )
        rendered.insert(insertion, content_type)
    if (
        parameters.indent
        and not suppress
        and all(child.kind is not NodeKind.TEXT for child in children)
    ):
        inner = "".join(f"\n{'  ' * (depth + 1)}{piece}" for piece in rendered)
        return f"{start}>{inner}\n{'  ' * depth}</{name}>"
    return f"{start}>{''.join(rendered)}</{name}>"


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


def _html5_doctype(
    root: XdmNode, parameters: SerializationParameters, *, method: str, codec: str
) -> str | None:
    """The HTML 5.0 doctype, when the root is the ``html`` element."""
    html5 = _is_html5(parameters, method)
    if not html5 or not _is_html_element(root, method=method, html5=True):
        return None
    if (root.local_name or "").lower() != "html":
        return None
    scope = _scope_for(root, {"xml": XML_NS}, html5=True)
    name = _element_name(root, scope, html=True)
    _require_encodable(name, codec, "a doctype name")
    if parameters.doctype_system is not None:
        return _doctype(root, parameters, html=False, codec=codec)
    # Bug 20264: doctype-public alone still requests a doctype.
    return f"<!DOCTYPE {name}>"


def _validate_xml_shape(
    items: Sequence[NormalizedItem],
    parameters: SerializationParameters,
    *,
    result_tree: bool,
) -> None:
    elements = [
        item for item in items if isinstance(item, XdmNode) and item.kind is NodeKind.ELEMENT
    ]
    if len(elements) <= 1:
        return
    if result_tree:
        # A final result tree is a document node and may contain several
        # element children (XSLT 3.0 section 25.1).  A standalone or doctype
        # declaration still requires a single document element.
        if parameters.standalone is not None or parameters.doctype_system is not None:
            raise _serialization_error(
                "a standalone or document type declaration requires one document element",
                "SEPM0004",
            )
        return
    raise _serialization_error(
        "the XML serialization has more than one top-level element", "SERE0003"
    )


def normalize_sequence(
    sequence: Iterable[XdmItem], item_separator: str | None = None
) -> tuple[NormalizedItem, ...]:
    """Apply XSLT 3.0 section 5.7.1 sequence normalization.

    *item_separator* replaces the single space normalization inserts between
    adjacent atomic values.
    """
    output: list[NormalizedItem] = []
    pending: list[str] = []
    previous_atomic = False

    def flush() -> None:
        nonlocal previous_atomic
        if pending:
            text = "".join(pending)
            pending.clear()
            if text:
                output.append(text)
        previous_atomic = False

    def visit(item: XdmItem) -> None:
        nonlocal previous_atomic
        if isinstance(item, XdmNode):
            if item.kind is NodeKind.DOCUMENT:
                for child in item.children:
                    visit(child)
            elif item.kind is NodeKind.TEXT:
                if item.disable_output_escaping:
                    flush()
                    output.append(item)
                elif item.string_value:
                    pending.append(item.string_value)
                    previous_atomic = False
            elif item.kind is NodeKind.NAMESPACE:
                raise _not_well_formed("a namespace node cannot appear in a serialized sequence")
            elif item.kind is NodeKind.ATTRIBUTE:
                raise _not_well_formed("an attribute node cannot appear in a serialized sequence")
            else:
                flush()
                output.append(item)
        elif isinstance(item, XdmAtomic):
            if previous_atomic:
                pending.append(" " if item_separator is None else item_separator)
            pending.append(item.lexical)
            previous_atomic = True
        else:
            raise _not_well_formed(
                f"cannot serialize a {type(item).__name__} item in an XML sequence"
            )

    for item in sequence:
        visit(item)
    flush()
    return tuple(output)


def _xml_declaration(parameters: SerializationParameters) -> str:
    standalone = ""
    if parameters.standalone is not None:
        standalone = ' standalone="yes"' if parameters.standalone else ' standalone="no"'
    return f'<?xml version="{parameters.version}" encoding="{parameters.encoding}"{standalone}?>'


def serialize_markup(
    items: Sequence[NormalizedItem],
    parameters: SerializationParameters,
    *,
    method: str,
    result_tree: bool = False,
) -> str:
    """Serialize normalized *items* with the XML, XHTML, or HTML method.

    ``result_tree`` marks items normalized from a delivered document node.  A
    final result tree may contain several element children; a bare sequence
    from ``fn:serialize`` may not (SERE0003).
    """
    html = method == "html"
    undeclare = _undeclares_prefixes(parameters, html=html)
    if not items:
        if result_tree and not html and not parameters.omit_xml_declaration:
            return _xml_declaration(parameters)
        return ""
    codec = codecs.lookup(parameters.encoding).name
    if not html:
        _validate_xml_shape(items, parameters, result_tree=result_tree)
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
    html5_document = (
        _is_html5(parameters, method)
        and root is not None
        and _is_html_element(root, method=method, html5=True)
    )
    root_declarations = (
        _html5_root_prefix_declarations(root)
        if html5_document and html and root is not None
        else {}
    )
    doctype = None
    if root is not None:
        doctype = _html5_doctype(root, parameters, method=method, codec=codec)
        if doctype is None and parameters.doctype_system is not None:
            doctype = _doctype(root, parameters, html=html, codec=codec)
    root_scope: dict[str, str] = {"xml": XML_NS}
    separator = parameters.item_separator
    characters = character_map_index(parameters.character_maps)
    for index, item in enumerate(items):
        if index:
            output.append("\n" if parameters.indent and separator is None else (separator or ""))
        if isinstance(item, str):
            output.append(
                _text_node(
                    item,
                    None,
                    parameters,
                    method=method,
                    codec=codec,
                    characters=characters,
                    html5=html5_document,
                )
            )
            continue
        if item.kind is NodeKind.ELEMENT and doctype is not None:
            output.append(doctype)
            if parameters.indent:
                output.append("\n")
            doctype = None
        output.append(
            _render_node(
                item,
                0,
                root_scope,
                parameters,
                method=method,
                codec=codec,
                characters=characters,
                undeclare=undeclare,
                suppressed=False,
                html5=html5_document,
                extra_declarations=root_declarations,
                root_declaration=item is root,
            )
        )
    return "".join(output)
