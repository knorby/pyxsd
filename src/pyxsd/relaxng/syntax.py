"""The RELAX NG 1.0 XML full-syntax reader.

The reader is deliberately nonvalidating.  It records the authored grammar
tree exactly enough for ordered simplification and composition to decide
legality: grammar scopes with parent links, ``start``/``define``/``combine``
components, unresolved reference placeholders, name classes, datatype-library
and namespace context, annotations, structural findings, and a source origin
for every construct.  Lexical QName/NCName errors, relative or fragment-bearing
datatype-library URIs, duplicate ``except`` clauses, and element children of
``name``/``value`` are structural findings too.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal, cast
from xml.etree import ElementTree as ET

from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import XML_NS, local_name, namespace_of
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
from pyxsd.relaxng.resources import LoadedResource, resolve_xml_base
from pyxsd.relaxng.uris import is_uri_reference, xlink_escape
from pyxsd.relaxng.whitespace import is_xml_whitespace, trim_xml_whitespace
from pyxsd.resource_policy import ResourcePolicyError

__all__ = [
    "RELAX_NG_NAMESPACE",
    "Annotation",
    "FullAnyName",
    "FullAttribute",
    "FullChoice",
    "FullData",
    "FullDefine",
    "FullDiv",
    "FullElement",
    "FullEmpty",
    "FullExcept",
    "FullGrammar",
    "FullGroup",
    "FullInterleave",
    "FullList",
    "FullMixed",
    "FullName",
    "FullNameChoice",
    "FullNameClass",
    "FullNameExcept",
    "FullNode",
    "FullNotAllowed",
    "FullNsName",
    "FullOneOrMore",
    "FullOptional",
    "FullParam",
    "FullPattern",
    "FullSchema",
    "FullStart",
    "FullText",
    "FullUnknown",
    "FullValue",
    "FullZeroOrMore",
    "GrammarScope",
    "ReferencePlaceholder",
    "SchemaFinding",
    "read_full_syntax",
]

#: The RELAX NG 1.0 structure namespace.
RELAX_NG_NAMESPACE = "http://relaxng.org/ns/structure/1.0"

_XML_BASE = f"{{{XML_NS}}}base"

_PATTERN_TAGS = frozenset(
    {
        "attribute",
        "choice",
        "data",
        "div",
        "element",
        "empty",
        "externalRef",
        "grammar",
        "group",
        "interleave",
        "list",
        "mixed",
        "notAllowed",
        "oneOrMore",
        "optional",
        "parentRef",
        "ref",
        "text",
        "value",
        "zeroOrMore",
    }
)

_NAME_CLASS_TAGS = frozenset({"anyName", "choice", "name", "nsName"})

#: RELAX NG elements whose character children are their string value.
_STRING_CONTENT_TAGS = frozenset({"name", "param", "value"})

_KNOWN_ATTRIBUTES: dict[str, frozenset[str]] = {
    "attribute": frozenset({"name"}),
    "choice": frozenset(),
    "data": frozenset({"type"}),
    "define": frozenset({"combine", "name"}),
    "div": frozenset(),
    "element": frozenset({"name"}),
    "empty": frozenset(),
    "externalRef": frozenset({"href"}),
    "grammar": frozenset(),
    "group": frozenset(),
    "include": frozenset({"href"}),
    "interleave": frozenset(),
    "list": frozenset(),
    "mixed": frozenset(),
    "name": frozenset(),
    "notAllowed": frozenset(),
    "nsName": frozenset(),
    "oneOrMore": frozenset(),
    "optional": frozenset(),
    "param": frozenset({"name"}),
    "parentRef": frozenset({"name"}),
    "ref": frozenset({"name"}),
    "start": frozenset({"combine"}),
    "text": frozenset(),
    "value": frozenset({"type"}),
    "zeroOrMore": frozenset(),
    "anyName": frozenset(),
    "except": frozenset(),
}

#: Attributes allowed on every RELAX NG element.
_COMMON_ATTRIBUTES = frozenset({"datatypeLibrary", "ns"})

# ---------------------------------------------------------------------------
# XML 1.0 lexical names and datatype-library URIs
# ---------------------------------------------------------------------------
#
# RELAX NG's ``name`` attributes are XML QNames and its ``ref``/``define``
# names are NCNames.  The 2001 specification predates XML 1.0 Fifth Edition,
# so the Fourth Edition character classes below apply: a combining mark such
# as U+0E35 is a name character but cannot start a name.  The tables encode
# productions [85]-[89] as inclusive hexadecimal ranges.

_BASE_CHAR = (
    "0041-005A 0061-007A 00C0-00D6 00D8-00F6 00F8-00FF 0100-0131 0134-013E 0141-0148 "
    "014A-017E 0180-01C3 01CD-01F0 01F4-01F5 01FA-0217 0250-02A8 02BB-02C1 0386 0388-038A "
    "038C 038E-03A1 03A3-03CE 03D0-03D6 03DA 03DC 03DE 03E0 03E2-03F3 0401-040C 040E-044F "
    "0451-045C 045E-0481 0490-04C4 04C7-04C8 04CB-04CC 04D0-04EB 04EE-04F5 04F8-04F9 "
    "0531-0556 0559 0561-0586 05D0-05EA 05F0-05F2 0621-063A 0641-064A 0671-06B7 06BA-06BE "
    "06C0-06CE 06D0-06D3 06D5 06E5-06E6 0905-0939 093D 0958-0961 0985-098C 098F-0990 "
    "0993-09A8 09AA-09B0 09B2 09B6-09B9 09DC-09DD 09DF-09E1 09F0-09F1 0A05-0A0A 0A0F-0A10 "
    "0A13-0A28 0A2A-0A30 0A32-0A33 0A35-0A36 0A38-0A39 0A59-0A5C 0A5E 0A72-0A74 0A85-0A8B "
    "0A8D 0A8F-0A91 0A93-0AA8 0AAA-0AB0 0AB2-0AB3 0AB5-0AB9 0ABD 0AE0 0B05-0B0C 0B0F-0B10 "
    "0B13-0B28 0B2A-0B30 0B32-0B33 0B36-0B39 0B3D 0B5C-0B5D 0B5F-0B61 0B85-0B8A 0B8E-0B90 "
    "0B92-0B95 0B99-0B9A 0B9C 0B9E-0B9F 0BA3-0BA4 0BA8-0BAA 0BAE-0BB5 0BB7-0BB9 0C05-0C0C "
    "0C0E-0C10 0C12-0C28 0C2A-0C33 0C35-0C39 0C60-0C61 0C85-0C8C 0C8E-0C90 0C92-0CA8 "
    "0CAA-0CB3 0CB5-0CB9 0CDE 0CE0-0CE1 0D05-0D0C 0D0E-0D10 0D12-0D28 0D2A-0D39 0D60-0D61 "
    "0E01-0E2E 0E30 0E32-0E33 0E40-0E45 0E81-0E82 0E84 0E87-0E88 0E8A 0E8D 0E94-0E97 "
    "0E99-0E9F 0EA1-0EA3 0EA5 0EA7 0EAA-0EAB 0EAD-0EAE 0EB0 0EB2-0EB3 0EBD 0EC0-0EC4 "
    "0F40-0F47 0F49-0F69 10A0-10C5 10D0-10F6 1100 1102-1103 1105-1107 1109 110B-110C "
    "110E-1112 113C 113E 1140 114C 114E 1150 1154-1155 1159 115F-1161 1163 1165 1167 1169 "
    "116D-116E 1172-1173 1175 119E 11A8 11AB 11AE-11AF 11B7-11B8 11BA 11BC-11C2 11EB 11F0 "
    "11F9 1E00-1E9B 1EA0-1EF9 1F00-1F15 1F18-1F1D 1F20-1F45 1F48-1F4D 1F50-1F57 1F59 1F5B "
    "1F5D 1F5F-1F7D 1F80-1FB4 1FB6-1FBC 1FBE 1FC2-1FC4 1FC6-1FCC 1FD0-1FD3 1FD6-1FDB "
    "1FE0-1FEC 1FF2-1FF4 1FF6-1FFC 2126 212A-212B 212E 2180-2182 3041-3094 30A1-30FA "
    "3105-312C AC00-D7A3 "
)

_IDEOGRAPHIC = "3007 3021-3029 4E00-9FA5"

_DIGIT = (
    "0030-0039 0660-0669 06F0-06F9 0966-096F 09E6-09EF 0A66-0A6F 0AE6-0AEF 0B66-0B6F "
    "0BE7-0BEF 0C66-0C6F 0CE6-0CEF 0D66-0D6F 0E50-0E59 0ED0-0ED9 0F20-0F29 "
)

_COMBINING_CHAR = (
    "0300-0345 0360-0361 0483-0486 0591-05A1 05A3-05B9 05BB-05BD 05BF 05C1-05C2 05C4 "
    "064B-0652 0670 06D6-06DC 06DD-06DF 06E0-06E4 06E7-06E8 06EA-06ED 0901-0903 093C "
    "093E-094C 094D 0951-0954 0962-0963 0981-0983 09BC 09BE 09BF 09C0-09C4 09C7-09C8 "
    "09CB-09CD 09D7 09E2-09E3 0A02 0A3C 0A3E 0A3F 0A40-0A42 0A47-0A48 0A4B-0A4D 0A70-0A71 "
    "0A81-0A83 0ABC 0ABE-0AC5 0AC7-0AC9 0ACB-0ACD 0B01-0B03 0B3C 0B3E-0B43 0B47-0B48 "
    "0B4B-0B4D 0B56-0B57 0B82-0B83 0BBE-0BC2 0BC6-0BC8 0BCA-0BCD 0BD7 0C01-0C03 0C3E-0C44 "
    "0C46-0C48 0C4A-0C4D 0C55-0C56 0C82-0C83 0CBE-0CC4 0CC6-0CC8 0CCA-0CCD 0CD5-0CD6 "
    "0D02-0D03 0D3E-0D43 0D46-0D48 0D4A-0D4D 0D57 0E31 0E34-0E3A 0E47-0E4E 0EB1 0EB4-0EB9 "
    "0EBB-0EBC 0EC8-0ECD 0F18-0F19 0F35 0F37 0F39 0F3E 0F3F 0F71-0F84 0F86-0F8B 0F90-0F95 "
    "0F97 0F99-0FAD 0FB1-0FB7 0FB9 20D0-20DC 20E1 302A-302F 3099 309A "
)

_EXTENDER = "00B7 02D0 02D1 0387 0640 0E46 0EC6 3005 3031-3035 309D-309E 30FC-30FE"


def _merge_ranges(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _parse_ranges(text: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for item in text.split():
        start, _, end = item.partition("-")
        ranges.append((int(start, 16), int(end or start, 16)))
    return _merge_ranges(ranges)


def _in_ranges(code: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    index = bisect_right(ranges, (code, 0x10FFFF)) - 1
    return index >= 0 and code <= ranges[index][1]


_NCNAME_START = _parse_ranges(f"{_BASE_CHAR} {_IDEOGRAPHIC} 005F")
_NCNAME_CHAR = _parse_ranges(
    f"{_BASE_CHAR} {_IDEOGRAPHIC} {_DIGIT} {_COMBINING_CHAR} {_EXTENDER} 002D 002E 005F"
)

_URI_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")


def _is_ncname(value: str) -> bool:
    """Whether *value* is an NCName in XML 1.0 Fourth Edition terms."""
    if not value or not _in_ranges(ord(value[0]), _NCNAME_START):
        return False
    return all(_in_ranges(ord(character), _NCNAME_CHAR) for character in value[1:])


def _split_qname(value: str) -> tuple[str | None, str] | None:
    """Split a lexical QName, or return ``None`` when it is not one."""
    if ":" not in value:
        return (None, value) if _is_ncname(value) else None
    if value.count(":") != 1:
        return None
    prefix, local = value.split(":", 1)
    if not _is_ncname(prefix) or not _is_ncname(local):
        return None
    return prefix, local


def _is_datatype_library_uri(value: str) -> bool:
    """Whether *value* is an absolute RFC 2396 URI reference sans fragment.

    The RELAX NG XML syntax makes ``datatypeLibrary`` an absolute URI
    reference: a relative reference or a fragment identifier is a schema
    error.  An empty value names the built-in library and is accepted.  The
    caller passes the XLink-escaped value, because RELAX NG section 4.3
    transforms the attribute before use.
    """
    if not value:
        return True
    if "#" in value:
        return False
    scheme = _URI_SCHEME.match(value)
    if scheme is None or scheme.end() == len(value):
        return False
    return is_uri_reference(value)


@dataclass(frozen=True)
class SchemaFinding:
    """A structural finding the reader can establish without simplification."""

    code: str
    message: str
    location: IssueLocation
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class Annotation:
    """One recorded foreign element or foreign attribute.

    RELAX NG 1.0 has no RELAX NG-namespace annotation production; only a
    foreign element or attribute is an annotation.  Its bytes are retained
    raw, and nothing inside it influences the grammar tree.  ``element`` is
    the authored subtree for element annotations and is absent for attribute
    annotations.
    """

    kind: Literal["element", "attribute"]
    name: str
    namespace: str | None
    value: str | None
    origins: OriginTable
    element: ET.Element | None = field(default=None, compare=False, hash=False, repr=False)


class FullNode:
    """Base marker for one authored node in the full XML syntax."""


class FullPattern(FullNode):
    """Base marker for one authored pattern."""


class FullNameClass(FullNode):
    """Base marker for one authored name class."""


@dataclass(frozen=True)
class FullEmpty(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullNotAllowed(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullText(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullElement(FullPattern):
    name_class: FullNameClass | None
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullAttribute(FullPattern):
    name_class: FullNameClass | None
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullGroup(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullInterleave(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullChoice(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullOptional(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullZeroOrMore(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullOneOrMore(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullList(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullMixed(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullDiv(FullPattern):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullParam(FullNode):
    name: str | None
    value: str
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullExcept(FullNode):
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullValue(FullPattern):
    """An authored ``value`` with its inherited datatype and QName context."""

    datatype_library: str
    type_name: str | None
    lexical: str
    base_uri: str | None
    namespace_uri: str | None
    namespaces: tuple[tuple[str, str], ...]
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullData(FullPattern):
    datatype_library: str
    type_name: str | None
    base_uri: str | None
    namespaces: tuple[tuple[str, str], ...]
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()

    @property
    def parameters(self) -> tuple[FullParam, ...]:
        """The authored datatype parameters, in document order."""
        return tuple(child for child in self.children if isinstance(child, FullParam))

    @property
    def except_pattern(self) -> FullExcept | None:
        """The authored ``except`` wrapper, when one is present."""
        return next((child for child in self.children if isinstance(child, FullExcept)), None)


@dataclass(frozen=True)
class FullUnknown(FullPattern):
    """An unknown RELAX NG element, preserved with its raw subtree."""

    tag: str
    attributes: tuple[tuple[str, str], ...]
    text: str | None
    children: tuple[FullUnknown, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullStart(FullNode):
    combine: str | None
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullDefine(FullNode):
    name: str | None
    combine: str | None
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullName(FullNameClass):
    lexical: str
    uri: str | None
    local: str
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullAnyName(FullNameClass):
    except_class: FullNameClass | None
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullNsName(FullNameClass):
    uri: str | None
    except_class: FullNameClass | None
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullNameChoice(FullNameClass):
    alternatives: tuple[FullNameClass, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullNameExcept(FullNameClass):
    alternatives: tuple[FullNameClass, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class ReferencePlaceholder(FullPattern):
    """An unresolved ``ref``, ``parentRef``, ``externalRef``, or ``include``.

    ``scope`` is the grammar scope that contains the reference, or ``None``
    for a reference outside any grammar.  ``base`` is the effective base of
    an ``externalRef`` or ``include`` and already includes any ``xml:base``.
    ``ns_attribute`` is an authored ``ns`` attribute on the reference itself;
    ``externalRef`` transfers it to a referenced pattern that has none.
    """

    kind: Literal["ref", "parentRef", "externalRef", "include"]
    name: str | None
    href: str | None
    base: str | None
    ns_attribute: str | None
    scope: GrammarScope | None
    children: tuple[FullNode, ...]
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class FullGrammar(FullPattern):
    scope: GrammarScope
    origins: OriginTable
    annotations: tuple[Annotation, ...] = ()


class GrammarScope:
    """One authored grammar's components.

    The reader builds a scope once, then only exposes it for reading.  The
    scope object itself is the stable parent-grammar identity that nested
    scopes and reference placeholders retain.
    """

    __slots__ = ("_annotations", "_children", "_origins", "_parent")

    def __init__(
        self,
        *,
        parent: GrammarScope | None,
        origins: OriginTable,
        annotations: tuple[Annotation, ...],
    ) -> None:
        self._parent = parent
        self._children: list[FullNode] = []
        self._origins = origins
        self._annotations = annotations

    def _append(self, node: FullNode) -> None:
        self._children.append(node)

    @property
    def parent(self) -> GrammarScope | None:
        """The enclosing grammar scope, or ``None`` for a top-level grammar."""
        return self._parent

    @property
    def children(self) -> tuple[FullNode, ...]:
        """Every grammar component in document order."""
        return tuple(self._children)

    @property
    def starts(self) -> tuple[FullStart, ...]:
        """The directly authored ``start`` components."""
        return tuple(node for node in self._children if isinstance(node, FullStart))

    @property
    def definitions(self) -> tuple[FullDefine, ...]:
        """The directly authored ``define`` components."""
        return tuple(node for node in self._children if isinstance(node, FullDefine))

    @property
    def includes(self) -> tuple[ReferencePlaceholder, ...]:
        """The directly authored ``include`` components."""
        return tuple(
            node
            for node in self._children
            if isinstance(node, ReferencePlaceholder) and node.kind == "include"
        )

    @property
    def divs(self) -> tuple[FullDiv, ...]:
        """The directly authored ``div`` grouping components."""
        return tuple(node for node in self._children if isinstance(node, FullDiv))

    @property
    def extras(self) -> tuple[FullNode, ...]:
        """Known constructs in a position the grammar content model excludes."""
        return tuple(
            node
            for node in self._children
            if not isinstance(node, (FullStart, FullDefine, FullDiv))
            and not (isinstance(node, ReferencePlaceholder) and node.kind == "include")
        )

    @property
    def origins(self) -> OriginTable:
        """The origin of the authored grammar element."""
        return self._origins

    @property
    def annotations(self) -> tuple[Annotation, ...]:
        """Annotations authored on the grammar element."""
        return self._annotations

    def __repr__(self) -> str:
        return (
            f"GrammarScope(starts={len(self.starts)}, "
            f"definitions={len(self.definitions)}, "
            f"includes={len(self.includes)}, divs={len(self.divs)})"
        )


@dataclass(frozen=True)
class FullSchema:
    """One resource read as a full-syntax schema.

    ``start`` is the authored top-level pattern.  ``grammars`` lists every
    inline grammar scope in preorder, including the top-level grammar when
    the resource root is one.
    """

    resource: LoadedResource
    start: FullPattern
    grammars: tuple[GrammarScope, ...]
    findings: tuple[SchemaFinding, ...]


def read_full_syntax(resource: LoadedResource) -> FullSchema:
    """Read *resource* into the unresolved full-syntax grammar tree.

    The reader never retrieves a referenced resource and never decides
    grammar legality.  Unknown RELAX NG elements and attributes, unbound
    prefixes, missing required attributes, invalid ``xml:base`` values, and
    non-RELAX NG roots are reported as findings; annotation content is opaque.
    """
    return _Reader(resource).read()


@dataclass(frozen=True)
class _Visit:
    """One element awaiting construction by the reader's explicit stack."""

    kind: Literal["node", "grammar", "name_class", "unknown"]
    element: ET.Element
    location: IssueLocation
    scope: GrammarScope | None
    base: str
    library: str
    namespace: str
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True)
class _Pending:
    """One element's child visits and the finish step that builds its node."""

    finish: Callable[[list[FullNode]], FullNode]
    visits: list[_Visit]


@dataclass(frozen=True)
class _Finish:
    """Marker that a pending element's child results are complete."""

    finish: Callable[[list[FullNode]], FullNode]
    start: int


def _split_tag(tag: str) -> tuple[str | None, str]:
    return namespace_of(tag), local_name(tag)


def _namespace(uri: str | None) -> str | None:
    return uri or None


class _Reader:
    def __init__(self, resource: LoadedResource) -> None:
        self.resource = resource
        self.source_id = resource.canonical_id
        self.findings: list[SchemaFinding] = []
        self.grammars: list[GrammarScope] = []

    def read(self) -> FullSchema:
        root = self.resource.root
        location = IssueLocation(self.source_id, (PathStep(root.tag, 1),))
        namespace, tag = _split_tag(root.tag)
        if namespace != RELAX_NG_NAMESPACE:
            self._finding(
                "relaxng-root-not-relaxng",
                f"the schema root {root.tag!r} is not a RELAX NG element",
                location,
            )
            start = cast(
                FullPattern,
                self._build_one(
                    _Visit(
                        "unknown",
                        root,
                        location,
                        None,
                        self.source_id,
                        "",
                        "",
                        self._annotations(root, location),
                    )
                ),
            )
        elif tag not in _PATTERN_TAGS:
            self._finding(
                "relaxng-root-not-pattern",
                f"the RELAX NG {tag!r} element is not a schema pattern",
                location,
            )
            start = cast(
                FullPattern,
                self._build_one(
                    _Visit(
                        "unknown",
                        root,
                        location,
                        None,
                        self.source_id,
                        "",
                        "",
                        self._annotations(root, location),
                    )
                ),
            )
        else:
            start = cast(
                FullPattern,
                self._build_one(
                    _Visit(
                        "node",
                        root,
                        location,
                        None,
                        self.source_id,
                        "",
                        "",
                    )
                ),
            )
        return FullSchema(
            resource=self.resource,
            start=start,
            grammars=tuple(self.grammars),
            findings=tuple(self.findings),
        )

    def _finding(self, code: str, message: str, location: IssueLocation) -> None:
        self.findings.append(SchemaFinding(code, message, location))

    def _origins(self, element: ET.Element, location: IssueLocation) -> OriginTable:
        origins = {SourceOrigin(self.source_id, location)}
        for attribute in sorted(element.attrib):
            origins.add(SourceOrigin(self.source_id, replace(location, attribute=attribute)))
        return OriginTable(frozenset(origins))

    def _attribute_origin(self, location: IssueLocation, attribute: str) -> OriginTable:
        return OriginTable(
            frozenset({SourceOrigin(self.source_id, replace(location, attribute=attribute))})
        )

    def _child_locations(
        self, element: ET.Element, location: IssueLocation
    ) -> list[tuple[ET.Element, IssueLocation]]:
        counts: dict[str, int] = {}
        result: list[tuple[ET.Element, IssueLocation]] = []
        for child in element:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            result.append(
                (
                    child,
                    IssueLocation(
                        self.source_id,
                        (*location.path, PathStep(child.tag, counts[child.tag])),
                    ),
                )
            )
        return result

    def _context(
        self,
        element: ET.Element,
        location: IssueLocation,
        base: str,
        library: str,
        namespace: str,
    ) -> tuple[str, str, str]:
        raw_base = element.get(_XML_BASE)
        if raw_base is not None:
            try:
                base = resolve_xml_base(base, xlink_escape(raw_base))
            except ResourcePolicyError as exc:
                self._finding(
                    "relaxng-invalid-base",
                    str(exc),
                    replace(location, attribute=_XML_BASE),
                )
        if "ns" in element.attrib:
            namespace = element.attrib["ns"]
        if "datatypeLibrary" in element.attrib:
            library = xlink_escape(element.attrib["datatypeLibrary"])
        return base, library, namespace

    def _annotations(self, element: ET.Element, location: IssueLocation) -> tuple[Annotation, ...]:
        annotations: list[Annotation] = []
        for child, child_location in self._child_locations(element, location):
            namespace, tag = _split_tag(child.tag)
            if namespace != RELAX_NG_NAMESPACE:
                annotations.append(
                    Annotation(
                        kind="element",
                        name=tag,
                        namespace=namespace,
                        value=None,
                        origins=self._origins(child, child_location),
                        element=child,
                    )
                )
        for attribute in sorted(element.attrib):
            namespace, name = _split_tag(attribute)
            if namespace is not None and namespace != RELAX_NG_NAMESPACE:
                annotations.append(
                    Annotation(
                        kind="attribute",
                        name=name,
                        namespace=namespace,
                        value=element.attrib[attribute],
                        origins=self._attribute_origin(location, attribute),
                    )
                )
        return tuple(annotations)

    def _check_attributes(self, element: ET.Element, tag: str, location: IssueLocation) -> None:
        allowed = _KNOWN_ATTRIBUTES.get(tag, frozenset())
        library = element.get("datatypeLibrary")
        if library is not None:
            normalized = xlink_escape(library)
            if not _is_datatype_library_uri(normalized):
                self._finding(
                    "relaxng-invalid-datatype-library",
                    f"the datatypeLibrary value {library!r} is not an absolute URI reference",
                    replace(location, attribute="datatypeLibrary"),
                )
        for attribute in sorted(element.attrib):
            namespace, name = _split_tag(attribute)
            if namespace is not None and namespace != RELAX_NG_NAMESPACE:
                continue
            if namespace is None and (name in _COMMON_ATTRIBUTES or name in allowed):
                continue
            self._finding(
                "relaxng-unknown-attribute",
                f"the RELAX NG {tag!r} element has no {name!r} attribute",
                replace(location, attribute=attribute),
            )

    def _check_character_data(self, element: ET.Element, location: IssueLocation, tag: str) -> None:
        """Reject non-whitespace character children outside string content.

        RELAX NG 1.0 section 3 allows only XML whitespace (#x20, #x9, #xD,
        #xA) as character children of every element other than ``value``,
        ``param``, and ``name``.  Foreign element subtrees stay opaque; only
        text and tails that belong to this element are inspected.
        """
        if is_xml_whitespace(element.text or "") and all(
            is_xml_whitespace(child.tail or "") for child in element
        ):
            return
        self._finding(
            "relaxng-invalid-character-content",
            f"the RELAX NG {tag!r} element may contain only XML whitespace character data",
            location,
        )

    def _check_ncname(
        self,
        value: str | None,
        location: IssueLocation,
        tag: str,
        attribute: str,
    ) -> None:
        """Validate an NCName-valued attribute after section 4.2 trimming."""
        if value is None:
            return
        text = trim_xml_whitespace(value)
        if not text:
            return
        if not _is_ncname(text):
            self._finding(
                "relaxng-invalid-name",
                f"the RELAX NG {tag} {attribute} {text!r} is not an NCName",
                replace(location, attribute=attribute),
            )

    def _check_reference_name(self, name: str | None, location: IssueLocation, tag: str) -> None:
        """Validate a ``ref``, ``parentRef``, or ``define`` NCName."""
        self._check_ncname(name, location, tag, "name")

    def _child_specs(
        self, element: ET.Element, location: IssueLocation
    ) -> list[tuple[ET.Element, IssueLocation]]:
        return [
            (child, child_location)
            for child, child_location in self._child_locations(element, location)
            if _split_tag(child.tag)[0] == RELAX_NG_NAMESPACE
        ]

    def _build(self, visits: list[_Visit]) -> tuple[FullNode, ...]:
        """Construct visits without recursion, preserving authored order."""
        results: list[FullNode] = []
        stack: list[_Visit | _Finish] = list(reversed(visits))
        while stack:
            item = stack.pop()
            if isinstance(item, _Finish):
                children = results[item.start :]
                del results[item.start :]
                results.append(item.finish(children))
                continue
            pending = self._plan(item)
            stack.append(_Finish(pending.finish, len(results)))
            stack.extend(reversed(pending.visits))
        return tuple(results)

    def _build_one(self, visit: _Visit) -> FullNode:
        return self._build([visit])[0]

    def _plan(self, visit: _Visit) -> _Pending:
        """Apply shared element checks, then plan one element's construction."""
        namespace, tag = _split_tag(visit.element.tag)
        if namespace == RELAX_NG_NAMESPACE and tag not in _STRING_CONTENT_TAGS:
            self._check_character_data(visit.element, visit.location, tag)
        if visit.kind == "node":
            return self._plan_node(visit)
        if visit.kind == "grammar":
            return self._plan_grammar_component(visit)
        if visit.kind == "name_class":
            return self._plan_name_class(visit)
        return self._plan_unknown(visit)

    def _plan_node(self, visit: _Visit) -> _Pending:
        tag = _split_tag(visit.element.tag)[1]
        if tag == "start":
            return self._plan_start(visit)
        if tag == "define":
            return self._plan_define(visit)
        if tag == "include":
            return self._plan_include(visit)
        if tag in {"name", "anyName", "nsName"}:
            return self._plan_name_class(visit)
        if tag == "param":
            return self._plan_param(visit)
        if tag == "except":
            return self._plan_except(visit)
        return self._plan_pattern(visit)

    def _node_visits(
        self,
        specs: list[tuple[ET.Element, IssueLocation]],
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> list[_Visit]:
        return [
            _Visit("node", child, child_location, scope, base, library, namespace)
            for child, child_location in specs
        ]

    def _grammar_visits(
        self,
        specs: list[tuple[ET.Element, IssueLocation]],
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> list[_Visit]:
        return [
            _Visit("grammar", child, child_location, scope, base, library, namespace)
            for child, child_location in specs
        ]

    def _plan_pattern(self, visit: _Visit) -> _Pending:
        element, location = visit.element, visit.location
        _, tag = _split_tag(element.tag)
        base, library, namespace = self._context(
            element, location, visit.base, visit.library, visit.namespace
        )
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, tag, location)
        specs = self._child_specs(element, location)

        if tag == "grammar":
            scope = GrammarScope(
                parent=visit.scope,
                origins=origins,
                annotations=annotations,
            )
            self.grammars.append(scope)

            def finish_grammar(children: list[FullNode]) -> FullNode:
                for child in children:
                    scope._append(child)
                return FullGrammar(scope, origins, annotations)

            return _Pending(
                finish_grammar,
                self._grammar_visits(specs, scope, base, library, namespace),
            )
        if tag in {"element", "attribute"}:
            return self._plan_element_pattern(
                element,
                location,
                specs,
                visit.scope,
                base,
                library,
                namespace,
                annotations,
                origins,
                attribute=tag == "attribute",
            )

        constructors: dict[
            str,
            Callable[
                [tuple[FullNode, ...], OriginTable, tuple[Annotation, ...]],
                FullPattern,
            ],
        ] = {
            "group": FullGroup,
            "interleave": FullInterleave,
            "choice": FullChoice,
            "optional": FullOptional,
            "zeroOrMore": FullZeroOrMore,
            "oneOrMore": FullOneOrMore,
            "list": FullList,
            "mixed": FullMixed,
            "div": FullDiv,
            "empty": FullEmpty,
            "text": FullText,
            "notAllowed": FullNotAllowed,
        }
        if tag in constructors:
            constructor = constructors[tag]

            def finish_container(children: list[FullNode]) -> FullNode:
                return constructor(tuple(children), origins, annotations)

            return _Pending(
                finish_container,
                self._node_visits(specs, visit.scope, base, library, namespace),
            )
        if tag == "value":
            if any(_split_tag(child.tag)[0] != RELAX_NG_NAMESPACE for child in element):
                self._finding(
                    "relaxng-invalid-value-content",
                    "a RELAX NG value element must contain only its lexical value",
                    location,
                )
            type_name = element.get("type")
            self._check_ncname(type_name, location, "value", "type")
            namespaces = tuple(sorted(self.resource.namespaces.bindings_for(element).items()))

            def finish_value(children: list[FullNode]) -> FullNode:
                return FullValue(
                    datatype_library=library,
                    type_name=type_name,
                    lexical=element.text or "",
                    base_uri=base,
                    namespace_uri=_namespace(namespace),
                    namespaces=namespaces,
                    children=tuple(children),
                    origins=origins,
                    annotations=annotations,
                )

            return _Pending(
                finish_value,
                self._node_visits(specs, visit.scope, base, library, namespace),
            )
        if tag == "data":
            type_name = element.get("type")
            if type_name is None:
                self._finding(
                    "relaxng-missing-attribute",
                    "a RELAX NG data element needs a type attribute",
                    replace(location, attribute="type"),
                )
            else:
                self._check_ncname(type_name, location, "data", "type")
            namespaces = tuple(sorted(self.resource.namespaces.bindings_for(element).items()))

            def finish_data(children: list[FullNode]) -> FullNode:
                return FullData(
                    datatype_library=library,
                    type_name=type_name,
                    base_uri=base,
                    namespaces=namespaces,
                    children=tuple(children),
                    origins=origins,
                    annotations=annotations,
                )

            return _Pending(
                finish_data,
                self._node_visits(specs, visit.scope, base, library, namespace),
            )
        if tag in {"ref", "parentRef"}:
            name = element.get("name")
            if name is None:
                self._finding(
                    "relaxng-missing-attribute",
                    f"a RELAX NG {tag} element needs a name attribute",
                    replace(location, attribute="name"),
                )
            self._check_reference_name(name, location, tag)
            reference_scope = visit.scope

            def finish_reference(children: list[FullNode]) -> FullNode:
                return ReferencePlaceholder(
                    kind=cast(Literal["ref", "parentRef"], tag),
                    name=name,
                    href=None,
                    base=None,
                    ns_attribute=element.get("ns"),
                    scope=reference_scope,
                    children=tuple(children),
                    origins=origins,
                    annotations=annotations,
                )

            return _Pending(
                finish_reference,
                self._node_visits(specs, reference_scope, base, library, namespace),
            )
        if tag == "externalRef":
            href = self._href(element, location)
            external_scope = visit.scope

            def finish_external_ref(children: list[FullNode]) -> FullNode:
                return ReferencePlaceholder(
                    kind="externalRef",
                    name=None,
                    href=href,
                    base=base,
                    ns_attribute=element.get("ns"),
                    scope=external_scope,
                    children=tuple(children),
                    origins=origins,
                    annotations=annotations,
                )

            return _Pending(
                finish_external_ref,
                self._node_visits(specs, external_scope, base, library, namespace),
            )
        self._finding(
            "relaxng-unknown-element",
            f"unknown RELAX NG element {tag!r}",
            location,
        )
        return self._plan_unknown(
            _Visit(
                "unknown",
                element,
                location,
                visit.scope,
                base,
                library,
                namespace,
                annotations,
            )
        )

    def _href(self, element: ET.Element, location: IssueLocation) -> str | None:
        """Return the XLink-escaped ``href`` value, reporting a missing one."""
        raw = element.get("href")
        if raw is None:
            self._finding(
                "relaxng-missing-attribute",
                f"a RELAX NG {_split_tag(element.tag)[1]} element needs an href attribute",
                replace(location, attribute="href"),
            )
            return None
        href = xlink_escape(raw)
        if not is_uri_reference(href):
            self._finding(
                "relaxng-invalid-href",
                f"the RELAX NG href value {raw!r} is not a URI reference",
                replace(location, attribute="href"),
            )
        return href

    def _plan_element_pattern(
        self,
        element: ET.Element,
        location: IssueLocation,
        specs: list[tuple[ET.Element, IssueLocation]],
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
        annotations: tuple[Annotation, ...],
        origins: OriginTable,
        *,
        attribute: bool,
    ) -> _Pending:
        name_attribute = element.get("name")
        has_name_class_child = False
        if name_attribute is not None:
            name_class: FullNameClass | None = self._qname_name(
                name_attribute,
                element,
                location,
                namespace,
                attribute_mode=attribute,
                location_attribute="name",
            )
            visits = self._node_visits(specs, scope, base, library, namespace)
        else:
            name_class = None
            visits = []
            # Leading unknown RELAX NG extension elements stay opaque and do
            # not displace the name class, matching the reader's existing
            # annotation-compatibility rule.  The name class must precede the
            # first known pattern, so <empty/><name> is a late name class.
            first_known = next(
                (
                    index
                    for index, (child, _) in enumerate(specs)
                    if _split_tag(child.tag)[1] in _KNOWN_ATTRIBUTES
                ),
                None,
            )
            if first_known is not None and self._starts_name_class(specs[first_known][0]):
                child, child_location = specs[first_known]
                visits.append(
                    _Visit(
                        "name_class",
                        child,
                        child_location,
                        scope,
                        base,
                        library,
                        namespace,
                    )
                )
                has_name_class_child = True
                specs = [*specs[:first_known], *specs[first_known + 1 :]]
            else:
                self._finding(
                    "relaxng-missing-name-class",
                    f"a RELAX NG {'attribute' if attribute else 'element'} "
                    "element needs a name or name-class child",
                    location,
                )
            visits.extend(self._node_visits(specs, scope, base, library, namespace))

        def finish_element(children: list[FullNode]) -> FullNode:
            child_name_class: FullNameClass | None
            if has_name_class_child:
                child_name_class = cast(FullNameClass, children[0])
                patterns = tuple(children[1:])
            else:
                child_name_class = name_class
                patterns = tuple(children)
            if attribute:
                return FullAttribute(child_name_class, patterns, origins, annotations)
            return FullElement(child_name_class, patterns, origins, annotations)

        return _Pending(finish_element, visits)

    def _starts_name_class(self, element: ET.Element) -> bool:
        _, tag = _split_tag(element.tag)
        if tag in {"name", "anyName", "nsName"}:
            return True
        if tag != "choice":
            return False
        children = [child for child in element if _split_tag(child.tag)[0] == RELAX_NG_NAMESPACE]
        return bool(children) and all(
            _split_tag(child.tag)[1] in _NAME_CLASS_TAGS for child in children
        )

    def _qname_name(
        self,
        lexical: str,
        element: ET.Element,
        location: IssueLocation,
        namespace: str,
        *,
        attribute_mode: bool,
        location_attribute: str | None,
    ) -> FullName:
        text = trim_xml_whitespace(lexical)
        parts = _split_qname(text)
        if parts is None:
            self._finding(
                "relaxng-invalid-name",
                f"the RELAX NG name {text!r} is not a QName",
                replace(location, attribute=location_attribute)
                if location_attribute is not None
                else location,
            )
            prefix, local = text.split(":", 1) if ":" in text else (None, text)
        else:
            prefix, local = parts
        if prefix is not None:
            if prefix == "xml":
                uri: str | None = XML_NS
            else:
                uri = self.resource.namespaces.bindings_for(element).get(prefix) or None
                if uri is None:
                    self._finding(
                        "relaxng-unbound-prefix",
                        f"the namespace prefix {prefix!r} is not bound in scope",
                        replace(location, attribute=location_attribute)
                        if location_attribute is not None
                        else location,
                    )
        else:
            uri = None if attribute_mode and "ns" not in element.attrib else _namespace(namespace)
        return FullName(
            lexical=lexical,
            uri=uri,
            local=trim_xml_whitespace(local),
            origins=self._origins(element, location),
            annotations=self._annotations(element, location),
        )

    def _plan_name_class(self, visit: _Visit) -> _Pending:
        element, location = visit.element, visit.location
        _, tag = _split_tag(element.tag)
        namespace = element.attrib.get("ns", visit.namespace)
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, tag, location)

        if tag == "name":
            if len(element):
                self._finding(
                    "relaxng-invalid-name-content",
                    "a RELAX NG name element must contain only a QName",
                    location,
                )

            def finish_name(children: list[FullNode]) -> FullNode:
                del children
                return self._qname_name(
                    element.text or "",
                    element,
                    location,
                    namespace,
                    attribute_mode=False,
                    location_attribute=None,
                )

            return _Pending(finish_name, [])
        if tag in {"anyName", "nsName"}:
            excepts = [
                (child, child_location)
                for child, child_location in self._child_specs(element, location)
                if _split_tag(child.tag)[1] == "except"
            ]
            for _, duplicate_location in excepts[1:]:
                self._finding(
                    "relaxng-duplicate-except",
                    f"a RELAX NG {tag} element allows at most one except",
                    duplicate_location,
                )
            except_pending: _Pending | None = None
            visits: list[_Visit] = []
            if excepts:
                except_element, except_location = excepts[0]
                except_pending = self._plan_name_except(except_element, except_location, namespace)
                visits = except_pending.visits

            def finish_wildcard(children: list[FullNode]) -> FullNode:
                except_class: FullNameClass | None = None
                if except_pending is not None:
                    except_class = cast(FullNameClass, except_pending.finish(children))
                if tag == "anyName":
                    return FullAnyName(except_class, origins, annotations)
                return FullNsName(_namespace(namespace), except_class, origins, annotations)

            return _Pending(finish_wildcard, visits)
        visits = self._plan_name_class_alternatives(element, location, namespace)

        def finish_choice(children: list[FullNode]) -> FullNode:
            return FullNameChoice(
                tuple(cast(FullNameClass, child) for child in children),
                origins,
                annotations,
            )

        return _Pending(finish_choice, visits)

    def _plan_name_class_alternatives(
        self,
        element: ET.Element,
        location: IssueLocation,
        namespace: str,
    ) -> list[_Visit]:
        visits: list[_Visit] = []
        for child, child_location in self._child_specs(element, location):
            _, tag = _split_tag(child.tag)
            if tag not in _NAME_CLASS_TAGS:
                if tag == "except":
                    continue
                self._finding(
                    "relaxng-unknown-element",
                    f"unknown RELAX NG name-class element {tag!r}",
                    child_location,
                )
                continue
            visits.append(
                _Visit(
                    "name_class",
                    child,
                    child_location,
                    None,
                    "",
                    "",
                    namespace,
                )
            )
        return visits

    def _plan_name_except(
        self,
        element: ET.Element,
        location: IssueLocation,
        namespace: str,
    ) -> _Pending:
        self._check_attributes(element, "except", location)
        except_namespace = element.attrib.get("ns", namespace)
        visits = self._plan_name_class_alternatives(element, location, except_namespace)
        origins = self._origins(element, location)
        annotations = self._annotations(element, location)

        def finish_except(children: list[FullNode]) -> FullNode:
            return FullNameExcept(
                tuple(cast(FullNameClass, child) for child in children),
                origins,
                annotations,
            )

        return _Pending(finish_except, visits)

    def _plan_param(self, visit: _Visit) -> _Pending:
        element, location = visit.element, visit.location
        base, library, namespace = self._context(
            element, location, visit.base, visit.library, visit.namespace
        )
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, "param", location)
        name = element.get("name")
        if name is None:
            self._finding(
                "relaxng-missing-attribute",
                "a RELAX NG param element needs a name attribute",
                replace(location, attribute="name"),
            )
        else:
            self._check_ncname(name, location, "param", "name")

        def finish_param(children: list[FullNode]) -> FullNode:
            return FullParam(
                name=name,
                value=element.text or "",
                children=tuple(children),
                origins=origins,
                annotations=annotations,
            )

        return _Pending(
            finish_param,
            self._node_visits(
                self._child_specs(element, location),
                None,
                base,
                library,
                namespace,
            ),
        )

    def _plan_except(self, visit: _Visit) -> _Pending:
        element, location = visit.element, visit.location
        base, library, namespace = self._context(
            element, location, visit.base, visit.library, visit.namespace
        )
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, "except", location)

        def finish_except(children: list[FullNode]) -> FullNode:
            return FullExcept(tuple(children), origins, annotations)

        return _Pending(
            finish_except,
            self._node_visits(
                self._child_specs(element, location),
                visit.scope,
                base,
                library,
                namespace,
            ),
        )

    def _plan_start(self, visit: _Visit) -> _Pending:
        element, location = visit.element, visit.location
        base, library, namespace = self._context(
            element, location, visit.base, visit.library, visit.namespace
        )
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, "start", location)
        combine = element.get("combine")

        def finish_start(children: list[FullNode]) -> FullNode:
            return FullStart(combine, tuple(children), origins, annotations)

        return _Pending(
            finish_start,
            self._node_visits(
                self._child_specs(element, location),
                visit.scope,
                base,
                library,
                namespace,
            ),
        )

    def _plan_define(self, visit: _Visit) -> _Pending:
        element, location = visit.element, visit.location
        base, library, namespace = self._context(
            element, location, visit.base, visit.library, visit.namespace
        )
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, "define", location)
        name = element.get("name")
        if name is None:
            self._finding(
                "relaxng-missing-attribute",
                "a RELAX NG define element needs a name attribute",
                replace(location, attribute="name"),
            )
        self._check_reference_name(name, location, "define")
        combine = element.get("combine")

        def finish_define(children: list[FullNode]) -> FullNode:
            return FullDefine(name, combine, tuple(children), origins, annotations)

        return _Pending(
            finish_define,
            self._node_visits(
                self._child_specs(element, location),
                visit.scope,
                base,
                library,
                namespace,
            ),
        )

    def _plan_include(self, visit: _Visit) -> _Pending:
        element, location = visit.element, visit.location
        base, library, namespace = self._context(
            element, location, visit.base, visit.library, visit.namespace
        )
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, "include", location)
        href = self._href(element, location)
        scope = visit.scope

        def finish_include(children: list[FullNode]) -> FullNode:
            return ReferencePlaceholder(
                kind="include",
                name=None,
                href=href,
                base=base,
                ns_attribute=element.get("ns"),
                scope=scope,
                children=tuple(children),
                origins=origins,
                annotations=annotations,
            )

        return _Pending(
            finish_include,
            self._grammar_visits(
                self._child_specs(element, location),
                scope,
                base,
                library,
                namespace,
            ),
        )

    def _plan_grammar_component(self, visit: _Visit) -> _Pending:
        tag = _split_tag(visit.element.tag)[1]
        if tag == "start":
            return self._plan_start(visit)
        if tag == "define":
            return self._plan_define(visit)
        if tag == "include":
            return self._plan_include(visit)
        if tag == "div":
            return self._plan_grammar_div(visit)
        return self._plan_node(visit)

    def _plan_grammar_div(self, visit: _Visit) -> _Pending:
        element, location = visit.element, visit.location
        base, library, namespace = self._context(
            element, location, visit.base, visit.library, visit.namespace
        )
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, "div", location)

        def finish_div(children: list[FullNode]) -> FullNode:
            return FullDiv(tuple(children), origins, annotations)

        return _Pending(
            finish_div,
            self._grammar_visits(
                self._child_specs(element, location),
                visit.scope,
                base,
                library,
                namespace,
            ),
        )

    def _plan_unknown(self, visit: _Visit) -> _Pending:
        element, location = visit.element, visit.location
        visits = [
            _Visit(
                "unknown",
                child,
                child_location,
                None,
                "",
                "",
                "",
            )
            for child, child_location in self._child_locations(element, location)
        ]

        def finish_unknown(children: list[FullNode]) -> FullNode:
            return FullUnknown(
                tag=element.tag,
                attributes=tuple(sorted(element.attrib.items())),
                text=element.text,
                children=tuple(cast(FullUnknown, child) for child in children),
                origins=self._origins(element, location),
                annotations=visit.annotations,
            )

        return _Pending(finish_unknown, visits)
