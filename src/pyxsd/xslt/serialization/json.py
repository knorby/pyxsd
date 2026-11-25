"""The JSON and adaptive output methods from Serialization 3.1.

Neither method performs sequence normalization: JSON serializes exactly one
item (or the JSON token ``null`` for the empty sequence) and adaptive serializes
each item with the method's item separator.  Both build on the XML serializer
for nodes and on the character-map/normalization layer for strings, so the
output methods never fork the escaping pipeline.
"""

from __future__ import annotations

import codecs
import math
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmItem, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xdm.values import _FAMILIES, _local_type_name
from pyxsd.xslt.errors import XsltSerializationError
from pyxsd.xslt.runtime.numbering import DecimalFormat, format_number
from pyxsd.xslt.serialization.characters import (
    CharacterMap,
    apply_mapped,
    character_map_index,
    normalize_text,
)
from pyxsd.xslt.serialization.parameters import SerializationParameters
from pyxsd.xslt.serialization.text import serialize_text
from pyxsd.xslt.serialization.xml import (
    attribute_value,
    normalize_sequence,
    serialize_markup,
)

__all__ = ["serialize_adaptive", "serialize_json"]

FN_NS = "http://www.w3.org/2005/xpath-functions"
MATH_NS = "http://www.w3.org/2005/xpath-functions/math"
MAP_NS = "http://www.w3.org/2005/xpath-functions/map"
ARRAY_NS = "http://www.w3.org/2005/xpath-functions/array"
XSD_NS = "http://www.w3.org/2001/XMLSchema"

_CONVENTIONAL_PREFIXES = {
    FN_NS: "fn",
    MATH_NS: "math",
    MAP_NS: "map",
    ARRAY_NS: "array",
    XSD_NS: "xs",
}

#: The Serialization 3.1 adaptive double picture with the default decimal
#: format properties.
_ADAPTIVE_DOUBLE_PICTURE = "0.0##########################e0"
_ADAPTIVE_FORMATS: dict[str | None, DecimalFormat] = {None: DecimalFormat()}

_STRING_LOCALS = frozenset({"string", "untypedAtomic", "anyURI"})
_QNAME_LOCALS = frozenset({"QName", "NOTATION"})
_JSON_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "/": "\\/",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _error(
    message: str, code: str, *, cause: BaseException | None = None
) -> XsltSerializationError:
    return XsltSerializationError(message, code=code, cause=cause)


def _family(item: XdmAtomic) -> str:
    return _FAMILIES.get(_local_type_name(item.type_name), "other")


# ---------------------------------------------------------------------------
# JSON output method


def _json_escape(value: str, codec: str) -> str:
    pieces: list[str] = []
    for character in value:
        escaped = _JSON_ESCAPES.get(character)
        if escaped is not None:
            pieces.append(escaped)
            continue
        code = ord(character)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            pieces.append(f"\\u{code:04X}")
            continue
        try:
            character.encode(codec)
        except UnicodeEncodeError:
            pieces.append(_json_code_point(code))
            continue
        pieces.append(character)
    return "".join(pieces)


def _json_code_point(code: int) -> str:
    """Escape one code point, using a surrogate pair outside the BMP."""
    if code > 0xFFFF:
        offset = code - 0x10000
        return f"\\u{0xD800 + (offset >> 10):04X}\\u{0xDC00 + (offset & 0x3FF):04X}"
    return f"\\u{code:04X}"


def _json_string(
    value: str,
    parameters: SerializationParameters,
    characters: CharacterMap,
    codec: str,
) -> str:
    form = parameters.normalization_form
    escaped = apply_mapped(
        value,
        characters,
        lambda run: _json_escape(normalize_text(run, form), codec),
    )
    return f'"{escaped}"'


def _json_number(item: XdmAtomic) -> str:
    # xs:float and xs:double are the numeric types whose value space includes
    # non-finite values; xs:decimal cannot reach this branch with one.
    value = item.value
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise _error("the JSON output method cannot represent a non-finite number", "SERE0020")
    return item.lexical


def _json_node(
    node: XdmNode, parameters: SerializationParameters, characters: CharacterMap, codec: str
) -> str:
    method = parameters.json_node_output_method
    if method == "html":
        root = node if node.kind is NodeKind.ELEMENT else node.root_element
        if root is not None and root.namespace_uri == "http://www.w3.org/1999/xhtml":
            # An XHTML-namespace node keeps its namespace declaration; the HTML
            # method alone would drop it (Serialization 3.1 section 6).
            method = "xhtml"
    delegated = replace(parameters, method=method, omit_xml_declaration=True)
    if method == "text":
        text = serialize_text((node,), delegated)
    else:
        text = serialize_markup(normalize_sequence((node,)), delegated, method=method)
    return _json_string(text, parameters, characters, codec)


def _json_sequence(
    sequence: Sequence[XdmItem],
    parameters: SerializationParameters,
    characters: CharacterMap,
    codec: str,
) -> str:
    if not sequence:
        return "null"
    if len(sequence) > 1:
        raise _error(
            "the JSON output method cannot serialize a sequence of more than one item",
            "SERE0023",
        )
    return _json_item(sequence[0], parameters, characters, codec)


def _json_entry_name(
    key: XdmAtomic, parameters: SerializationParameters, characters: CharacterMap, codec: str
) -> str:
    return _json_string(key.lexical, parameters, characters, codec)


def _json_object(
    mapping: XdmMap, parameters: SerializationParameters, characters: CharacterMap, codec: str
) -> str:
    members: list[str] = []
    seen: set[str] = set()
    for key, value in mapping.entries:
        name = key.lexical
        if name in seen and not parameters.allow_duplicate_names:
            raise _error(
                f"the JSON output method found the duplicate object name {name!r}", "SERE0022"
            )
        seen.add(name)
        members.append(
            f"{_json_entry_name(key, parameters, characters, codec)}:"
            f"{_json_sequence(value, parameters, characters, codec)}"
        )
    return "{" + ",".join(members) + "}"


def _json_item(
    item: XdmItem,
    parameters: SerializationParameters,
    characters: CharacterMap,
    codec: str,
) -> str:
    if isinstance(item, XdmMap):
        return _json_object(item, parameters, characters, codec)
    if isinstance(item, XdmArray):
        return (
            "["
            + ",".join(
                _json_sequence(member, parameters, characters, codec) for member in item.members
            )
            + "]"
        )
    if isinstance(item, XdmNode):
        return _json_node(item, parameters, characters, codec)
    if isinstance(item, XdmAtomic):
        family = _family(item)
        if family == "numeric":
            return _json_number(item)
        if family == "boolean":
            return "true" if bool(item.value) else "false"
        return _json_string(item.lexical, parameters, characters, codec)
    raise _error(
        f"the JSON output method cannot serialize a {type(item).__name__} item", "SERE0021"
    )


def serialize_json(sequence: Sequence[XdmItem], parameters: SerializationParameters) -> str:
    """Return the JSON-method serialization of *sequence*."""
    codec = codecs.lookup(parameters.encoding).name
    characters = character_map_index(parameters.character_maps)
    return _json_sequence(sequence, parameters, characters, codec)


# ---------------------------------------------------------------------------
# Adaptive output method


def _adaptive_quoted(
    value: str, parameters: SerializationParameters, characters: CharacterMap
) -> str:
    form = parameters.normalization_form
    quoted = apply_mapped(
        value,
        characters,
        lambda run: normalize_text(run, form).replace('"', '""'),
    )
    return f'"{quoted}"'


def _uri_qualified_name(value: Any) -> str:
    if getattr(value, "_resolved_", False) and isinstance(getattr(value, "_local_", None), str):
        return f"Q{{{value._uri_ or ''}}}{value._local_}"
    namespace = getattr(value, "namespace", None)
    local_name = getattr(value, "local_name", None)
    if isinstance(local_name, str):
        return f"Q{{{namespace or ''}}}{local_name}"
    lexical = str(value)
    return lexical if ":" in lexical else f"Q{{}}{lexical}"


def _adaptive_double(item: XdmAtomic) -> str:
    number = float(item.value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "INF" if number > 0 else "-INF"
    return format_number(
        item,
        _ADAPTIVE_DOUBLE_PICTURE,
        None,
        _ADAPTIVE_FORMATS,
        {},
    )


def _adaptive_atomic(
    item: XdmAtomic, parameters: SerializationParameters, characters: CharacterMap
) -> str:
    local = _local_type_name(item.type_name)
    family = _family(item)
    if family == "boolean":
        return "true()" if bool(item.value) else "false()"
    if local in _STRING_LOCALS:
        return _adaptive_quoted(item.lexical, parameters, characters)
    if local == "double":
        return _adaptive_double(item)
    if family == "numeric" and local != "float":
        return item.lexical
    if local in _QNAME_LOCALS:
        return _uri_qualified_name(item.value)
    return f"xs:{local}(" + _adaptive_quoted(item.lexical, parameters, characters) + ")"


def _adaptive_member(
    sequence: Sequence[XdmItem],
    parameters: SerializationParameters,
    characters: CharacterMap,
    codec: str,
) -> str:
    """Serialize an array member or map value, parenthesized when it is not one item."""
    if len(sequence) == 1:
        return _adaptive_item(sequence[0], parameters, characters, codec)
    rendered = ",".join(_adaptive_item(item, parameters, characters, codec) for item in sequence)
    return f"({rendered})"


def _adaptive_array(
    array: XdmArray,
    parameters: SerializationParameters,
    characters: CharacterMap,
    codec: str,
) -> str:
    return (
        "["
        + ",".join(
            _adaptive_member(member, parameters, characters, codec) for member in array.members
        )
        + "]"
    )


def _adaptive_map(
    mapping: XdmMap,
    parameters: SerializationParameters,
    characters: CharacterMap,
    codec: str,
) -> str:
    members = ",".join(
        f"{_adaptive_atomic(key, parameters, characters)}:"
        f"{_adaptive_member(value, parameters, characters, codec)}"
        for key, value in mapping.entries
    )
    return f"map{{{members}}}"


def _function_name(item: XdmFunction) -> str:
    minimum, maximum = item.arity
    arity = maximum if maximum is not None else minimum
    name = item.name
    if not name:
        return f"(anonymous-function)#{arity}"
    if name.startswith("{"):
        uri, _, local = name[1:].partition("}")
        prefix = _CONVENTIONAL_PREFIXES.get(uri)
        if prefix is None:
            return f"Q{{{uri}}}{local}#{arity}"
        return f"{prefix}:{local}#{arity}"
    if ":" in name:
        return f"{name}#{arity}"
    return f"Q{{}}{name}#{arity}"


def _adaptive_standalone(node: XdmNode, parameters: SerializationParameters, codec: str) -> str:
    characters = character_map_index(parameters.character_maps)
    if node.kind is NodeKind.ATTRIBUTE:
        local = node.local_name or node.name or ""
        name = f"{node.prefix}:{local}" if node.prefix else local
        value = attribute_value(node.string_value, parameters, codec=codec, characters=characters)
        return f'{name}="{value}"'
    prefix = node.local_name or ""
    uri = attribute_value(
        node.string_value, parameters, codec=codec, characters=character_map_index(())
    )
    if prefix:
        return f'xmlns:{prefix}="{uri}"'
    return f'xmlns="{uri}"'


def _adaptive_node(node: XdmNode, parameters: SerializationParameters, codec: str) -> str:
    if node.kind in (NodeKind.ATTRIBUTE, NodeKind.NAMESPACE):
        return _adaptive_standalone(node, parameters, codec)
    if node.kind is NodeKind.COMMENT:
        try:
            node.string_value.encode(codec)
        except UnicodeEncodeError:
            escaped = "".join(
                character if _encodable_character(character, codec) else f"&#x{ord(character):X};"
                for character in node.string_value
            )
            return f"<!--{escaped}-->"
    # Every serialization parameter except byte-order-mark reaches the XML
    # method, including omit-xml-declaration and indent.
    delegated = replace(parameters, method="xml", byte_order_mark=False)
    return serialize_markup(normalize_sequence((node,)), delegated, method="xml")


def _encodable_character(character: str, codec: str) -> bool:
    try:
        character.encode(codec)
    except UnicodeEncodeError:
        return False
    return True


def _adaptive_item(
    item: XdmItem,
    parameters: SerializationParameters,
    characters: CharacterMap,
    codec: str,
) -> str:
    if isinstance(item, XdmMap):
        return _adaptive_map(item, parameters, characters, codec)
    if isinstance(item, XdmArray):
        return _adaptive_array(item, parameters, characters, codec)
    if isinstance(item, XdmNode):
        return _adaptive_node(item, parameters, codec)
    if isinstance(item, XdmAtomic):
        return _adaptive_atomic(item, parameters, characters)
    if isinstance(item, XdmFunction):
        return _function_name(item)
    raise _error(
        f"the adaptive output method cannot serialize a {type(item).__name__} item", "SENR0001"
    )


def serialize_adaptive(sequence: Sequence[XdmItem], parameters: SerializationParameters) -> str:
    """Return the adaptive-method serialization of *sequence*."""
    codec = codecs.lookup(parameters.encoding).name
    characters = character_map_index(parameters.character_maps)
    separator = "\n" if parameters.item_separator is None else parameters.item_separator
    return separator.join(_adaptive_item(item, parameters, characters, codec) for item in sequence)
