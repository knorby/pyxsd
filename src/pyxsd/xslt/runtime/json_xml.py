"""Read and write the XSLT 3.0 XML representation of JSON over XDM."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import cast

from pyxsd import xsd_data_types as _xsd
from pyxsd.namespaces import XSD_NS
from pyxsd.xdm import (
    XdmArray,
    XdmAtomic,
    XdmFunction,
    XdmItem,
    XdmMap,
    XdmSequence,
    XdmSequenceType,
)
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.values import _coerce_sequence, canonical_float_string, is_xsd_subtype
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.serialization.json_lexical import (
    JsonNumber,
    JsonObject,
    JsonValue,
    decode_json_escapes,
    escape_json_string,
    parse_json_text,
    render_json_string,
)

__all__ = [
    "JsonParseOptions",
    "json_to_xml",
    "parse_json_options",
    "parse_json_xdm",
    "xml_to_json",
]

_FN_NS = "http://www.w3.org/2005/xpath-functions"
_XML_WHITESPACE = " \t\r\n"
_INDENT = XdmAtomic.string("indent")
_INDENT_TYPE = XdmSequenceType("xs:boolean", "one")
_BOOLEAN_OPTION_TYPE = XdmSequenceType("xs:boolean", "one")
_STRING_OPTION_TYPE = XdmSequenceType("xs:string", "one")

_PARSE_JSON_OPTIONS = ("liberal", "duplicates", "escape", "fallback")
_JSON_TO_XML_OPTIONS = (*_PARSE_JSON_OPTIONS, "validate")
_PARSE_JSON_DUPLICATES = frozenset({"use-first", "use-last", "reject"})
_JSON_TO_XML_DUPLICATES = frozenset({"retain", "use-first", "reject"})


@dataclass(frozen=True, slots=True)
class JsonParseOptions:
    """The converted F&O 3.1 JSON parse options for one call."""

    liberal: bool = False
    duplicates: str = "use-first"
    escape: bool = False
    fallback: XdmFunction | None = None
    validate: bool = False


@dataclass(frozen=True, slots=True)
class _XmlKey:
    """One rendered map key and whether it carries backslash escapes."""

    text: str
    escaped: bool


def parse_json_options(options: XdmMap | None, *, kind: str) -> JsonParseOptions:
    """Convert an F&O options map for ``parse-json`` or ``json-to-xml``.

    Option names may be any atomic value that compares equal to the required
    string.  Values use the function conversion rules: an ``xs:boolean`` or
    ``xs:string`` option accepts an ``xs:untypedAtomic`` spelling but not a
    different primitive type, and the ``fallback`` option must be a
    single-argument function item.  Unknown entries are ignored.

    Conversion failures raise ``XPTY0004``; a converted value outside the
    permitted set raises ``FOJS0005``, as does ``fallback`` together with
    ``escape=true()``.  ``json-to-xml`` additionally rejects
    ``duplicates=retain`` with ``validate=true()``.

    ``parse-json`` and ``json-doc`` default ``escape`` to false.  The F&O 3.1
    option table says true, but its own examples and the pinned QT3 corpus
    assume false; W3C bug 30287 records that internal conflict.  The corpus
    behavior is the one this profile preserves.
    """
    names = _JSON_TO_XML_OPTIONS if kind == "json-to-xml" else _PARSE_JSON_OPTIONS
    supplied: dict[str, XdmSequence] = {}
    for key, value in () if options is None else options.entries:
        for name in names:
            if key.same_key(XdmAtomic.string(name)):
                supplied[name] = value
                break

    liberal = _boolean_option(supplied.get("liberal"), "liberal")
    escape = _boolean_option(supplied.get("escape"), "escape")
    validate = (
        _boolean_option(supplied.get("validate"), "validate") if kind == "json-to-xml" else False
    )
    duplicates: str | None = None
    if "duplicates" in supplied:
        duplicates = _string_option(supplied["duplicates"], "duplicates")

    if duplicates is None:
        duplicates = ("reject" if validate else "retain") if kind == "json-to-xml" else "use-first"
    elif duplicates not in (
        _JSON_TO_XML_DUPLICATES if kind == "json-to-xml" else _PARSE_JSON_DUPLICATES
    ):
        raise XsltDynamicError(
            f"the duplicates option value {duplicates!r} is not permitted",
            code="FOJS0005",
        )

    fallback: XdmFunction | None = None
    if "fallback" in supplied:
        fallback = _fallback_option(supplied["fallback"])
    if fallback is not None and escape:
        raise XsltDynamicError(
            "the fallback option cannot be combined with escape=true()",
            code="FOJS0005",
        )
    if kind == "json-to-xml" and validate and duplicates == "retain":
        raise XsltDynamicError(
            "duplicates=retain is incompatible with validate=true()",
            code="FOJS0005",
        )
    return JsonParseOptions(
        liberal=liberal,
        duplicates=duplicates,
        escape=escape,
        fallback=fallback,
        validate=validate,
    )


def parse_json_xdm(
    text: str,
    *,
    liberal: bool = False,
    duplicates: str = "use-first",
    escape: bool = False,
    fallback: Callable[[str], str] | None = None,
) -> XdmItem | None:
    """Parse JSON text into one XDM value under the F&O 3.1 mapping.

    ``null`` produces the empty sequence, both as the whole input and as a
    member of an array or object.  Empty text is malformed JSON and raises
    ``FOJS0001``; the calling bridge handles an empty XDM input sequence
    before this function is reached.  Duplicate keys under
    ``duplicates=reject`` raise ``FOJS0003``.
    """
    tree = _parse_json(text, liberal=liberal)
    return _json_tree_to_xdm(
        tree,
        duplicates=duplicates,
        escape=escape,
        fallback=fallback,
    )


def json_to_xml(
    text: str,
    *,
    liberal: bool,
    validate: bool,
    escape: bool,
    fallback: Callable[[str], str] | None,
    duplicates: str,
    base_uri: str | None,
) -> XdmDocument:
    """Return the F&O 3.1 XML representation of one JSON text.

    The document node carries *base_uri* as the static base URI of the call.
    ``validate=true()`` is explicitly unsupported and raises ``FOJS0004``
    after the input has been shown to be valid JSON; no annotations are
    fabricated.  Malformed JSON raises ``FOJS0001`` and duplicate keys under
    ``duplicates=reject`` raise ``FOJS0003``.
    """
    tree = _parse_json(text, liberal=liberal)
    if validate:
        raise XsltDynamicError(
            "schema-validated json-to-xml output is not supported",
            code="FOJS0004",
        )
    body = _json_xml_element(
        tree,
        key=None,
        escape=escape,
        fallback=fallback,
        duplicates=duplicates,
        root=True,
    )
    document = XdmDocument.parse(
        body.encode("utf-8"),
        source_name="<json-to-xml>",
        base_uri=base_uri,
    )
    return document


def _parse_json(text: str, *, liberal: bool) -> JsonValue:
    try:
        return parse_json_text(text, liberal=liberal)
    except ValueError as exc:
        raise XsltDynamicError(
            "the JSON document is not valid JSON",
            code="FOJS0001",
            cause=exc,
        ) from exc


def _json_xml_element(
    value: JsonValue,
    *,
    key: _XmlKey | None,
    escape: bool,
    fallback: Callable[[str], str] | None,
    duplicates: str,
    root: bool = False,
) -> str:
    """Serialize one parsed JSON value as a §17.4.2 element."""
    if isinstance(value, JsonObject):
        children = "".join(
            _json_xml_element(
                member,
                key=member_key,
                escape=escape,
                fallback=fallback,
                duplicates=duplicates,
            )
            for member_key, member in _json_members(
                value, escape=escape, fallback=fallback, duplicates=duplicates
            )
        )
        return _json_xml_container("map", children, key=key, root=root)
    if isinstance(value, list):
        children = "".join(
            _json_xml_element(
                member,
                key=None,
                escape=escape,
                fallback=fallback,
                duplicates=duplicates,
            )
            for member in value
        )
        return _json_xml_container("array", children, key=key, root=root)
    if isinstance(value, str):
        rendered = render_json_string(value, escape=escape, fallback=fallback)
        escaped = escape and "\\" in rendered
        namespace = _root_namespace(root)
        return (
            f"<string{namespace}{_xml_attributes(key, escaped=escaped)}>"
            f"{_xml_text(rendered)}</string>"
        )
    if isinstance(value, JsonNumber):
        return (
            f"<number{_root_namespace(root)}{_xml_attributes(key)}>"
            f"{_xml_text(value.lexical)}</number>"
        )
    if isinstance(value, bool):
        lexical = "true" if value else "false"
        return f"<boolean{_root_namespace(root)}{_xml_attributes(key)}>{lexical}</boolean>"
    if value is None:
        return f"<null{_root_namespace(root)}{_xml_attributes(key)}/>"
    raise TypeError(f"{type(value).__name__} is not a JSON value")  # pragma: no cover


def _json_members(
    value: JsonObject,
    *,
    escape: bool,
    fallback: Callable[[str], str] | None,
    duplicates: str,
) -> Iterator[tuple[_XmlKey, JsonValue]]:
    """Render object keys and apply the json-to-xml duplicate policy."""
    kept: dict[str, tuple[_XmlKey, JsonValue]] = {}
    for raw_key, member in value.members:
        rendered = render_json_string(raw_key, escape=escape, fallback=fallback)
        key = _XmlKey(rendered, escape and "\\" in rendered)
        if duplicates == "retain":
            yield key, member
            continue
        if rendered in kept:
            if duplicates == "reject":
                raise XsltDynamicError(
                    f"duplicate map key {rendered!r}",
                    code="FOJS0003",
                )
            continue
        kept[rendered] = (key, member)
    yield from kept.values()


def _json_xml_container(
    name: str,
    children: str,
    *,
    key: _XmlKey | None,
    root: bool,
) -> str:
    namespace = _root_namespace(root)
    if not children:
        return f"<{name}{namespace}{_xml_attributes(key)}/>"
    return f"<{name}{namespace}{_xml_attributes(key)}>{children}</{name}>"


def _root_namespace(root: bool) -> str:
    """The default namespace declaration on the document's only child."""
    return f' xmlns="{_FN_NS}"' if root else ""


def _xml_attributes(key: _XmlKey | None, *, escaped: bool = False) -> str:
    if key is None:
        return "" if not escaped else ' escaped="true"'
    pieces = [f' key="{_xml_attribute(key.text)}"']
    if key.escaped:
        pieces.append(' escaped-key="true"')
    if escaped:
        pieces.append(' escaped="true"')
    return "".join(pieces)


def _xml_text(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#xD;")
    )


def _xml_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\t", "&#x9;")
        .replace("\n", "&#xA;")
        .replace("\r", "&#xD;")
    )


def _json_tree_to_xdm(
    value: JsonValue,
    *,
    duplicates: str,
    escape: bool,
    fallback: Callable[[str], str] | None,
) -> XdmItem | None:
    """Convert one parsed JSON value into its parse-json XDM result."""
    if value is None:
        return None
    if isinstance(value, bool):
        return XdmAtomic.boolean(value)
    if isinstance(value, JsonNumber):
        return XdmAtomic.double(float(value.lexical))
    if isinstance(value, str):
        return XdmAtomic.string(render_json_string(value, escape=escape, fallback=fallback))
    if isinstance(value, list):
        return XdmArray(
            _json_sequence_to_xdm(
                member,
                duplicates=duplicates,
                escape=escape,
                fallback=fallback,
            )
            for member in value
        )
    if isinstance(value, JsonObject):
        entries: list[tuple[XdmAtomic, XdmSequence]] = []
        positions: dict[str, int] = {}
        for raw_key, member in value.members:
            rendered = render_json_string(raw_key, escape=escape, fallback=fallback)
            converted = _json_sequence_to_xdm(
                member,
                duplicates=duplicates,
                escape=escape,
                fallback=fallback,
            )
            if rendered in positions:
                if duplicates == "reject":
                    raise XsltDynamicError(
                        f"duplicate map key {rendered!r}",
                        code="FOJS0003",
                    )
                if duplicates == "use-first":
                    continue
                entries[positions[rendered]] = (XdmAtomic.string(rendered), converted)
            else:
                positions[rendered] = len(entries)
                entries.append((XdmAtomic.string(rendered), converted))
        return XdmMap(entries)
    raise TypeError(f"{type(value).__name__} is not a JSON value")  # pragma: no cover


def _json_sequence_to_xdm(
    value: JsonValue,
    *,
    duplicates: str,
    escape: bool,
    fallback: Callable[[str], str] | None,
) -> XdmSequence:
    item = _json_tree_to_xdm(
        value,
        duplicates=duplicates,
        escape=escape,
        fallback=fallback,
    )
    return () if item is None else (item,)


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


def _boolean_option(value: XdmSequence | None, name: str) -> bool:
    if value is None:
        return False
    try:
        (converted,) = _coerce_sequence(tuple(_option_atoms(value)), _BOOLEAN_OPTION_TYPE)
    except TypeError as exc:
        raise XsltDynamicError(
            f"the {name} option must be convertible to xs:boolean",
            code="XPTY0004",
        ) from exc
    return bool(cast(XdmAtomic, converted).value)


def _string_option(value: XdmSequence, name: str) -> str:
    try:
        (converted,) = _coerce_sequence(tuple(_option_atoms(value)), _STRING_OPTION_TYPE)
    except TypeError as exc:
        raise XsltDynamicError(
            f"the {name} option must be convertible to xs:string",
            code="XPTY0004",
        ) from exc
    return cast(XdmAtomic, converted).lexical


def _fallback_option(value: XdmSequence) -> XdmFunction:
    if len(value) != 1 or not isinstance(value[0], XdmFunction):
        raise XsltDynamicError(
            "the fallback option must be a function item",
            code="XPTY0004",
        )
    function = value[0]
    if function.arity != (1, 1):
        raise XsltDynamicError(
            "the fallback option function must have arity 1",
            code="XPTY0004",
        )
    if not _fallback_accepts_string(function):
        raise XsltDynamicError(
            "the fallback option function must accept an xs:string argument",
            code="XPTY0004",
        )
    return function


def _fallback_accepts_string(function: XdmFunction) -> bool:
    """Whether the fallback's declared parameter accepts a single string.

    Function parameter types are contravariant: the parameter must be
    ``item()`` or a supertype of ``xs:string`` (``xs:anyType``,
    ``xs:anySimpleType``, ``xs:anyAtomicType``, or ``xs:string`` itself).
    ``map(*)``, a node type, a non-string atomic, or a string *subtype* does
    not accept every string.  Type identity keeps its namespace: a schema type
    whose local name happens to be ``string`` or ``anyType`` is not the XSD
    type.  Declarations built from a static context carry Clark names; the
    conventional ``xs`` prefix is the XSD namespace for a signature assembled
    without one.  The shared XSD subtype relation decides the hierarchy, so a
    new supertype spelling does not need a hand-maintained list.  When no
    declaration is available the compatibility check stays at invocation.
    """
    declared = function.declared_types or function.signature
    if declared is None or not declared.parameters:
        return True
    type_name = declared.parameters[0].type_name
    if type_name == "item()":
        return True
    namespace, local = _type_identity(type_name)
    return namespace == XSD_NS and is_xsd_subtype("string", local)


def _type_identity(type_name: str) -> tuple[str | None, str]:
    """Split one declared type name into its namespace and local name."""
    if type_name.startswith("{"):
        namespace, _, local = type_name[1:].partition("}")
        return namespace, local
    prefix, separator, local = type_name.partition(":")
    if separator and prefix == "xs":
        return XSD_NS, local
    return None, type_name


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
        allowed.add("key")
        if map_member is None or kind != "map":
            allowed.add("escaped-key")
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
    # A map member named fn:map has only a key; its attributes are validated
    # by _write_value. Do not interpret a disallowed escape flag here.
    preescaped = child.name != f"{{{_FN_NS}}}map" and _attribute_flag(child, "escaped-key")
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
