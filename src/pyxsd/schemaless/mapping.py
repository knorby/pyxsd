"""Explicit element-first projection from native JSON into XML.

A :class:`~pyxsd.schemaless.json_document.JsonDocument` never turns its native
value into XML on its own. :meth:`JsonDocument.to_xml` asks for one explicit
:class:`XmlMapping`, which names the root element and may rename projected
occurrences by JSON Pointer. Element-first is the conservative default: object
properties become child elements, arrays become a wrapper element holding
repeated ``item`` children, and no property becomes an attribute without an
explicit rule.

The projection is one-way. It reads authoritative native storage, builds a
fresh :class:`~pyxsd.schemaless.document.SchemalessDocument` with no source
bytes, and never writes edits back to the JSON document. JSON Pointer pins are
a separate addressing language and are not carried into the XML tree. Fatal
mapping problems raise :class:`~pyxsd.schemaless.errors.XmlMappingError` with
structured diagnostics; information the XML shape cannot retain is reported as
a non-fatal warning on the derived document's report.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, NoReturn
from xml.etree import ElementTree as ET
from xml.parsers import expat

from pyxsd._lexical import NodeContext
from pyxsd.locations import DataLocation
from pyxsd.namespaces import XSI_NS
from pyxsd.schemaless.document import SchemalessDocument
from pyxsd.schemaless.errors import XmlMappingError
from pyxsd.schemaless.json_values import NativeJsonValue, encode_json
from pyxsd.schemaless.json_xdm import escape_pointer_segment
from pyxsd.schemaless.nodes import NodeMapping
from pyxsd.schemaless.tree import SchemalessElement
from pyxsd.schemaless.types import TypePolicy
from pyxsd.validation import ValidationReport

if TYPE_CHECKING:
    from pyxsd.schemaless.nodes import _CompiledNodeMapping

_XSI_NIL = f"{{{XSI_NS}}}nil"
_XMLNS_NS = "http://www.w3.org/2000/xmlns/"
_POINTER_BAD_ESCAPE = re.compile(r"~(?:[^01]|$)")
_OccurrencePath = tuple[tuple[str, int], ...]

#: ASCII characters XLink 1.0 section 5.4 percent-escapes. A namespace name is
#: a literal identifier, never an escaped ``xs:anyURI`` value, so these cannot
#: appear in one; legal non-ASCII IRI characters remain allowed.
_NS_FORBIDDEN_ASCII = frozenset(
    [chr(code) for code in range(0x20)]
    + [" ", "<", ">", '"', "{", "}", "|", "\\", "^", "`", "\x7f"]
)


@dataclass(frozen=True, slots=True)
class MappingRule:
    """One explicit override for an occurrence addressed by JSON Pointer.

    ``name`` is a local XML name or a Clark expanded name. It replaces the
    default element name derived from an object member key (or ``item`` for an
    array member, or the mapping's root name at the empty pointer). Only
    explicit mapping names may be expanded: object keys are data and must be
    local names. Later mapping policies add node kinds, namespaces, array
    wrapping, null handling, and scalar type pins; element-first projection
    only renames.
    """

    name: str | None = None

    def __post_init__(self) -> None:
        if self.name is not None and not isinstance(self.name, str):
            raise TypeError("a mapping rule name must be a string or None")


class XmlMapping:
    """A root name plus per-pointer element-first overrides.

    ``rules`` maps RFC 6901 JSON Pointers to :class:`MappingRule` values. A
    pointer that does not occur in a particular document is simply policy for a
    possible occurrence; it is not an error, so mapping factories can describe
    optional schema properties. ``default="elements"`` is the only projection
    policy in this release: object properties and array members are elements.
    A supplied rule name is checked when its occurrence is projected, because
    only then is its XML path known.
    """

    __slots__ = ("_default", "_root_name", "_rules")

    def __init__(
        self,
        root_name: str,
        *,
        default: str = "elements",
        rules: Mapping[str, MappingRule] | None = None,
    ) -> None:
        if not isinstance(root_name, str):
            raise TypeError("root_name must be a string")
        if default != "elements":
            raise ValueError(
                f"unsupported XmlMapping default {default!r}; only 'elements' is available"
            )
        root_path = _format_path(((root_name, 1),))
        _check_xml_name(root_name, "", root_path, None)

        checked: dict[str, MappingRule] = {}
        if rules is not None and not isinstance(rules, Mapping):
            raise TypeError("mapping rules must be a mapping from JSON Pointers to rules")
        for pointer, rule in (rules or {}).items():
            if not isinstance(pointer, str):
                raise TypeError("mapping rule pointers must be strings")
            problem = _pointer_problem(pointer)
            if problem:
                _fail(
                    "invalid-json-pointer",
                    f"invalid JSON Pointer {pointer!r}: {problem}",
                    pointer,
                    root_path,
                    None,
                )
            if not isinstance(rule, MappingRule):
                raise TypeError("mapping rule values must be MappingRule instances")
            checked[pointer] = rule

        self._root_name = root_name
        self._default = "elements"
        self._rules: Mapping[str, MappingRule] = MappingProxyType(checked)

    @property
    def root_name(self) -> str:
        """The required element name of the projected document."""
        return self._root_name

    @property
    def default(self) -> str:
        """The projection policy; only ``"elements"`` is currently available."""
        return self._default

    @property
    def rules(self) -> Mapping[str, MappingRule]:
        """An immutable view of the JSON Pointer to rule policy."""
        return self._rules

    @classmethod
    def elements(
        cls, root_name: str, *, rules: Mapping[str, MappingRule] | None = None
    ) -> XmlMapping:
        """Return the conservative element-first mapping for *root_name*."""
        return cls(root_name, default="elements", rules=rules)


def _pointer_problem(pointer: str) -> str:
    """Return why *pointer* is not RFC 6901 syntax, or the empty string."""
    if pointer and not pointer.startswith("/"):
        return "must be empty or start with '/'"
    if any(_POINTER_BAD_ESCAPE.search(raw) for raw in pointer.split("/")[1:]):
        return "contains an invalid escape"
    return ""


def _format_path(path: _OccurrencePath) -> str:
    """Render an occurrence path for a mapping diagnostic."""
    return "".join(f"/{name}[{index}]" for name, index in path)


def _split_name(name: str) -> tuple[str, str]:
    """Split a Clark expanded name into its namespace URI and local part."""
    if name.startswith("{"):
        uri, separator, local = name[1:].partition("}")
        if not separator:
            raise ValueError("expanded name is missing its closing '}'")
        return uri, local
    return "", name


def _is_xml_ncname(name: str) -> bool:
    """Return whether Expat accepts *name* as exactly one element name.

    Expat is the project's XML parser, so its name grammar is authoritative.
    The probe compares the received start-element name with the input, which
    rejects names that merely rearrange the synthesized document instead of
    naming its only element. Invalid UTF-8 encodings (for example an unpaired
    surrogate) are rejected by the same path.
    """
    if not name or ":" in name:
        return False
    seen: list[str] = []

    def start(received: str, attributes: dict[str, str]) -> None:
        seen.append(received)

    parser = expat.ParserCreate()
    parser.StartElementHandler = start
    try:
        parser.Parse(f"<{name}/>".encode(), True)
    except (expat.ExpatError, UnicodeEncodeError):
        return False
    return seen == [name]


def _is_xml_char(code: int) -> bool:
    """Whether *code* is an XML 1.0 ``Char``."""
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def _is_namespace_name(uri: str) -> bool:
    """Whether *uri* can be used literally as an element namespace name.

    A namespace name is compared literally, so ASCII characters that a URI
    reference must percent-encode cannot appear in it. Non-ASCII IRI
    characters are allowed when they are XML characters. Structural URI
    validation reuses the project's XSD ``anyURI`` discipline.
    """
    for character in uri:
        code = ord(character)
        if code <= 0x7F:
            if character in _NS_FORBIDDEN_ASCII:
                return False
        elif not _is_xml_char(code):
            return False
    from pyxsd.relaxng.uris import is_uri_reference

    return is_uri_reference(uri)


def _check_xml_name(name: str, pointer: str, xml_path: str, source: str | None) -> None:
    """Reject an invalid explicit local or Clark expanded name; never repair it."""
    try:
        uri, local = _split_name(name)
    except ValueError:
        _fail(
            "invalid-xml-name",
            f"the XML name {name!r} is not valid",
            pointer,
            xml_path,
            source,
        )
    if uri and (uri == _XMLNS_NS or not _is_namespace_name(uri)):
        _fail(
            "invalid-xml-name",
            f"the XML name {name!r} has an unusable namespace name {uri!r}",
            pointer,
            xml_path,
            source,
        )
    if not _is_xml_ncname(local):
        _fail(
            "invalid-xml-name",
            f"the XML name {name!r} is not valid",
            pointer,
            xml_path,
            source,
        )


def _check_local_name(name: str, pointer: str, xml_path: str, source: str | None) -> None:
    """Reject a data-derived object key that is not a local XML name.

    Only explicit mapping names may be Clark expanded names. A JSON object key
    is data, not namespace policy, so `{urn:x}id` is an invalid key rather than
    a namespaced element.
    """
    if not _is_xml_ncname(name):
        _fail(
            "invalid-xml-name",
            f"the object member name {name!r} is not a valid local XML name",
            pointer,
            xml_path,
            source,
        )


def _check_xml_text(text: str, pointer: str, path: _OccurrencePath, source: str) -> None:
    """Reject a JSON string XML 1.0 cannot represent as character data."""
    for character in text:
        code = ord(character)
        if not _is_xml_char(code):
            marker = f"U+{code:04X}"
            _fail(
                "invalid-xml-text",
                f"the JSON string at pointer {pointer!r} contains {marker}, "
                "which XML 1.0 cannot represent",
                pointer,
                _format_path(path),
                source,
                character=marker,
            )


def _fail(
    code: str,
    message: str,
    pointer: str,
    xml_path: str,
    source: str | None,
    *,
    character: str | None = None,
) -> NoReturn:
    """Raise one fatal mapping diagnostic with both addresses."""
    details: dict[str, object] = {"json_pointer": pointer, "xml_path": xml_path}
    if character is not None:
        details["character"] = character
    report = ValidationReport(diagnostics_complete=True)
    report.add_error(
        message,
        code=code,
        location=DataLocation(source, pointer),
        location_precision="exact",
        details=details,
    )
    raise XmlMappingError(report)


class _Projector:
    """One immutable mapping applied to one native JSON value."""

    __slots__ = ("_mapping", "_report", "_source", "_uses_xsi")

    def __init__(self, mapping: XmlMapping, source: str, report: ValidationReport) -> None:
        self._mapping = mapping
        self._source = source
        self._report = report
        self._uses_xsi = False

    def build(self, value: NativeJsonValue) -> SchemalessElement:
        rule = self._mapping.rules.get("")
        root_name = (
            rule.name if rule is not None and rule.name is not None else self._mapping.root_name
        )
        path: _OccurrencePath = ((root_name, 1),)
        _check_xml_name(root_name, "", _format_path(path), self._source)
        root = ET.Element(root_name)
        self._fill(root, value, "", path)
        element = SchemalessElement.from_etree(root)
        if self._uses_xsi:
            element.revision._replace_context(element, NodeContext({"xsi": XSI_NS}, None, None))
        return element

    def _fill(
        self, element: ET.Element, value: NativeJsonValue, pointer: str, path: _OccurrencePath
    ) -> None:
        if isinstance(value, dict):
            if not value:
                self._warn_empty("empty-object", "object", pointer, path)
                return
            counts: dict[str, int] = {}
            for key, member in value.items():
                child_pointer = f"{pointer}/{escape_pointer_segment(key)}"
                child_name, child_path = self._child_name(key, child_pointer, path, counts)
                self._fill(ET.SubElement(element, child_name), member, child_pointer, child_path)
            return
        if isinstance(value, list):
            if not value:
                self._warn_empty("empty-array", "array", pointer, path)
                return
            counts = {}
            for index, member in enumerate(value):
                child_pointer = f"{pointer}/{index}"
                child_name, child_path = self._child_name("item", child_pointer, path, counts)
                self._fill(ET.SubElement(element, child_name), member, child_pointer, child_path)
            return
        if value is None:
            element.set(_XSI_NIL, "true")
            self._uses_xsi = True
        elif isinstance(value, str):
            _check_xml_text(value, pointer, path, self._source)
            element.text = value
        else:
            element.text = encode_json(value)

    def _child_name(
        self,
        default: str,
        pointer: str,
        parent_path: _OccurrencePath,
        counts: dict[str, int],
    ) -> tuple[str, _OccurrencePath]:
        rule = self._mapping.rules.get(pointer)
        name = default
        explicit = False
        if rule is not None and rule.name is not None:
            name = rule.name
            explicit = True
        index = counts.get(name, 0) + 1
        path: _OccurrencePath = (*parent_path, (name, index))
        if explicit:
            _check_xml_name(name, pointer, _format_path(path), self._source)
        else:
            _check_local_name(name, pointer, _format_path(path), self._source)
        counts[name] = index
        return name, path

    def _warn_empty(self, code: str, kind: str, pointer: str, path: _OccurrencePath) -> None:
        self._report.add_warning(
            f"an empty JSON {kind} projects to an empty XML element, so the "
            "container kind is not recoverable",
            code=code,
            location=DataLocation(self._source, pointer),
            location_precision="exact",
            details={
                "json_pointer": pointer,
                "xml_path": _format_path(path),
                "json_kind": kind,
            },
        )


def _project_document(
    value: NativeJsonValue,
    mapping: XmlMapping,
    *,
    source: str,
    node_mapping: NodeMapping | None,
) -> SchemalessDocument:
    """Build one fresh XML document from a native value and its mapping."""
    if node_mapping is not None and not isinstance(node_mapping, NodeMapping):
        raise TypeError("node_mapping must be a NodeMapping or None")
    report = ValidationReport(diagnostics_complete=True)
    root = _Projector(mapping, source, report).build(value)
    compiled: _CompiledNodeMapping | None = None
    if node_mapping is not None:
        compiled = node_mapping._compiled
        if compiled.rules:
            root = compiled.apply(root)
        if compiled.pins:
            root.revision._set_types(TypePolicy.lexical()._with_default_pins(compiled.pins))
    return SchemalessDocument._from_caller_tree(root, report, node_mapping=compiled)


__all__ = ["MappingRule", "XmlMapping"]
