"""The RELAX NG 1.0 XML full-syntax reader.

The reader is deliberately nonvalidating.  It records the authored grammar
tree exactly enough for ordered simplification and composition to decide
legality: grammar scopes with parent links, ``start``/``define``/``combine``
components, unresolved reference placeholders, name classes, datatype-library
and namespace context, annotations, structural findings, and a source origin
for every construct.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal, cast
from xml.etree import ElementTree as ET

from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import XML_NS, local_name, namespace_of
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
from pyxsd.relaxng.resources import LoadedResource, resolve_xml_base
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
    "annotation": frozenset(),
}

#: Attributes allowed on every RELAX NG element.
_COMMON_ATTRIBUTES = frozenset({"datatypeLibrary", "ns"})


@dataclass(frozen=True)
class SchemaFinding:
    """A structural finding the reader can establish without simplification."""

    code: str
    message: str
    location: IssueLocation
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class Annotation:
    """One recorded foreign or ``annotation`` element/attribute.

    Annotation bytes are retained raw; nothing in the annotation influences
    the grammar tree.  ``element`` is the authored subtree for element
    annotations and is absent for attribute annotations.
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
            start: FullPattern = self._unknown(root, location)
        elif tag == "annotation":
            self._finding(
                "relaxng-root-not-pattern",
                "a RELAX NG annotation is not a schema pattern",
                location,
            )
            start = self._unknown(root, location)
        elif tag not in _PATTERN_TAGS:
            self._finding(
                "relaxng-root-not-pattern",
                f"the RELAX NG {tag!r} element is not a schema pattern",
                location,
            )
            start = self._unknown(root, location)
        else:
            start = self._pattern(
                root,
                location,
                scope=None,
                base=self.source_id,
                library="",
                namespace="",
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
                base = resolve_xml_base(base, raw_base)
            except ResourcePolicyError as exc:
                self._finding(
                    "relaxng-invalid-base",
                    str(exc),
                    replace(location, attribute=_XML_BASE),
                )
        if "ns" in element.attrib:
            namespace = element.attrib["ns"]
        if "datatypeLibrary" in element.attrib:
            library = element.attrib["datatypeLibrary"]
        return base, library, namespace

    def _annotations(self, element: ET.Element, location: IssueLocation) -> tuple[Annotation, ...]:
        annotations: list[Annotation] = []
        for child, child_location in self._child_locations(element, location):
            namespace, tag = _split_tag(child.tag)
            if namespace != RELAX_NG_NAMESPACE or tag == "annotation":
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

    def _child_specs(
        self, element: ET.Element, location: IssueLocation
    ) -> list[tuple[ET.Element, IssueLocation]]:
        return [
            (child, child_location)
            for child, child_location in self._child_locations(element, location)
            if _split_tag(child.tag)[0] == RELAX_NG_NAMESPACE
            and _split_tag(child.tag)[1] != "annotation"
        ]

    def _children(
        self,
        element: ET.Element,
        location: IssueLocation,
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> tuple[FullNode, ...]:
        return tuple(
            self._node(child, child_location, scope, base, library, namespace)
            for child, child_location in self._child_specs(element, location)
        )

    def _node(
        self,
        element: ET.Element,
        location: IssueLocation,
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> FullNode:
        _, tag = _split_tag(element.tag)
        if tag == "start":
            return self._start(element, location, scope, base, library, namespace)
        if tag == "define":
            return self._define(element, location, scope, base, library, namespace)
        if tag == "include":
            return self._include(element, location, scope, base, library, namespace)
        if tag in {"name", "anyName", "nsName"}:
            return self._name_class(element, location, namespace)
        if tag == "param":
            return self._param(element, location, base, library, namespace)
        if tag == "except":
            return self._except(element, location, scope, base, library, namespace)
        return self._pattern(element, location, scope, base, library, namespace)

    def _pattern(
        self,
        element: ET.Element,
        location: IssueLocation,
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> FullPattern:
        _, tag = _split_tag(element.tag)
        base, library, namespace = self._context(element, location, base, library, namespace)
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, tag, location)

        if tag == "grammar":
            return self._grammar(
                element,
                location,
                scope,
                base,
                library,
                namespace,
                annotations,
                origins,
            )
        if tag == "element":
            return self._element_pattern(
                element,
                location,
                scope,
                base,
                library,
                namespace,
                annotations,
                origins,
                attribute=False,
            )
        if tag == "attribute":
            return self._element_pattern(
                element,
                location,
                scope,
                base,
                library,
                namespace,
                annotations,
                origins,
                attribute=True,
            )
        if tag in {"group", "interleave", "choice", "optional", "zeroOrMore", "oneOrMore"}:
            children = self._children(element, location, scope, base, library, namespace)
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
            }
            return constructors[tag](children, origins, annotations)
        if tag == "list":
            return FullList(
                self._children(element, location, scope, base, library, namespace),
                origins,
                annotations,
            )
        if tag == "mixed":
            return FullMixed(
                self._children(element, location, scope, base, library, namespace),
                origins,
                annotations,
            )
        if tag == "div":
            return FullDiv(
                self._children(element, location, scope, base, library, namespace),
                origins,
                annotations,
            )
        if tag == "empty":
            return FullEmpty(
                self._children(element, location, scope, base, library, namespace),
                origins,
                annotations,
            )
        if tag == "text":
            return FullText(
                self._children(element, location, scope, base, library, namespace),
                origins,
                annotations,
            )
        if tag == "notAllowed":
            return FullNotAllowed(
                self._children(element, location, scope, base, library, namespace),
                origins,
                annotations,
            )
        if tag == "value":
            namespaces = tuple(sorted(self.resource.namespaces.bindings_for(element).items()))
            return FullValue(
                datatype_library=library,
                type_name=element.get("type"),
                lexical=element.text or "",
                base_uri=base,
                namespace_uri=_namespace(namespace),
                namespaces=namespaces,
                children=self._children(element, location, scope, base, library, namespace),
                origins=origins,
                annotations=annotations,
            )
        if tag == "data":
            type_name = element.get("type")
            if type_name is None:
                self._finding(
                    "relaxng-missing-attribute",
                    "a RELAX NG data element needs a type attribute",
                    replace(location, attribute="type"),
                )
            return FullData(
                datatype_library=library,
                type_name=type_name,
                base_uri=base,
                namespaces=tuple(sorted(self.resource.namespaces.bindings_for(element).items())),
                children=self._children(element, location, scope, base, library, namespace),
                origins=origins,
                annotations=annotations,
            )
        if tag in {"ref", "parentRef"}:
            name = element.get("name")
            if name is None:
                self._finding(
                    "relaxng-missing-attribute",
                    f"a RELAX NG {tag} element needs a name attribute",
                    replace(location, attribute="name"),
                )
            return ReferencePlaceholder(
                kind=cast(Literal["ref", "parentRef"], tag),
                name=name,
                href=None,
                base=None,
                ns_attribute=element.get("ns"),
                scope=scope,
                children=self._children(element, location, scope, base, library, namespace),
                origins=origins,
                annotations=annotations,
            )
        if tag == "externalRef":
            href = element.get("href")
            if href is None:
                self._finding(
                    "relaxng-missing-attribute",
                    "a RELAX NG externalRef element needs an href attribute",
                    replace(location, attribute="href"),
                )
            return ReferencePlaceholder(
                kind="externalRef",
                name=None,
                href=href,
                base=base,
                ns_attribute=element.get("ns"),
                scope=scope,
                children=self._children(element, location, scope, base, library, namespace),
                origins=origins,
                annotations=annotations,
            )
        self._finding(
            "relaxng-unknown-element",
            f"unknown RELAX NG element {tag!r}",
            location,
        )
        return self._unknown(element, location)

    def _element_pattern(
        self,
        element: ET.Element,
        location: IssueLocation,
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
        annotations: tuple[Annotation, ...],
        origins: OriginTable,
        *,
        attribute: bool,
    ) -> FullPattern:
        specs = self._child_specs(element, location)
        name_attribute = element.get("name")
        if name_attribute is not None:
            name_class: FullNameClass | None = self._qname_name(
                name_attribute,
                element,
                location,
                namespace,
                attribute_mode=attribute,
                location_attribute="name",
            )
        else:
            name_class = None
            for index, (child, child_location) in enumerate(specs):
                if self._starts_name_class(child):
                    name_class = self._name_class(child, child_location, namespace)
                    specs = [*specs[:index], *specs[index + 1 :]]
                    break
            else:
                self._finding(
                    "relaxng-missing-name-class",
                    f"a RELAX NG {'attribute' if attribute else 'element'} "
                    "element needs a name or name-class child",
                    location,
                )
        children = tuple(
            self._node(child, child_location, scope, base, library, namespace)
            for child, child_location in specs
        )
        if attribute:
            return FullAttribute(name_class, children, origins, annotations)
        return FullElement(name_class, children, origins, annotations)

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
        text = lexical.strip()
        if ":" in text:
            prefix, local = text.split(":", 1)
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
            local = text
            uri = None if attribute_mode and "ns" not in element.attrib else _namespace(namespace)
        return FullName(
            lexical=lexical,
            uri=uri,
            local=local.strip(),
            origins=self._origins(element, location),
            annotations=self._annotations(element, location),
        )

    def _name_class(
        self,
        element: ET.Element,
        location: IssueLocation,
        namespace: str,
    ) -> FullNameClass:
        _, tag = _split_tag(element.tag)
        if "ns" in element.attrib:
            namespace = element.attrib["ns"]
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, tag, location)

        if tag == "name":
            return self._qname_name(
                element.text or "",
                element,
                location,
                namespace,
                attribute_mode=False,
                location_attribute=None,
            )
        if tag == "anyName":
            return FullAnyName(
                self._name_except(element, location, namespace),
                origins,
                annotations,
            )
        if tag == "nsName":
            return FullNsName(
                _namespace(namespace),
                self._name_except(element, location, namespace),
                origins,
                annotations,
            )
        alternatives = self._name_class_alternatives(element, location, namespace)
        return FullNameChoice(alternatives, origins, annotations)

    def _name_class_alternatives(
        self,
        element: ET.Element,
        location: IssueLocation,
        namespace: str,
    ) -> tuple[FullNameClass, ...]:
        alternatives: list[FullNameClass] = []
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
            alternatives.append(self._name_class(child, child_location, namespace))
        return tuple(alternatives)

    def _name_except(
        self,
        element: ET.Element,
        location: IssueLocation,
        namespace: str,
    ) -> FullNameClass | None:
        for child, child_location in self._child_specs(element, location):
            _, tag = _split_tag(child.tag)
            if tag != "except":
                continue
            self._check_attributes(child, "except", child_location)
            except_namespace = child.attrib.get("ns", namespace)
            alternatives = self._name_class_alternatives(child, child_location, except_namespace)
            return FullNameExcept(
                alternatives,
                self._origins(child, child_location),
                self._annotations(child, child_location),
            )
        return None

    def _param(
        self,
        element: ET.Element,
        location: IssueLocation,
        base: str,
        library: str,
        namespace: str,
    ) -> FullParam:
        base, library, namespace = self._context(element, location, base, library, namespace)
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
        return FullParam(
            name=name,
            value=element.text or "",
            children=self._children(element, location, None, base, library, namespace),
            origins=origins,
            annotations=annotations,
        )

    def _except(
        self,
        element: ET.Element,
        location: IssueLocation,
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> FullExcept:
        base, library, namespace = self._context(element, location, base, library, namespace)
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, "except", location)
        return FullExcept(
            self._children(element, location, scope, base, library, namespace),
            origins,
            annotations,
        )

    def _start(
        self,
        element: ET.Element,
        location: IssueLocation,
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> FullStart:
        base, library, namespace = self._context(element, location, base, library, namespace)
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, "start", location)
        return FullStart(
            combine=element.get("combine"),
            children=self._children(element, location, scope, base, library, namespace),
            origins=origins,
            annotations=annotations,
        )

    def _define(
        self,
        element: ET.Element,
        location: IssueLocation,
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> FullDefine:
        base, library, namespace = self._context(element, location, base, library, namespace)
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
        return FullDefine(
            name=name,
            combine=element.get("combine"),
            children=self._children(element, location, scope, base, library, namespace),
            origins=origins,
            annotations=annotations,
        )

    def _include(
        self,
        element: ET.Element,
        location: IssueLocation,
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> ReferencePlaceholder:
        base, library, namespace = self._context(element, location, base, library, namespace)
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, "include", location)
        href = element.get("href")
        if href is None:
            self._finding(
                "relaxng-missing-attribute",
                "a RELAX NG include element needs an href attribute",
                replace(location, attribute="href"),
            )
        children = tuple(
            self._grammar_component(child, child_location, scope, base, library, namespace)
            for child, child_location in self._child_specs(element, location)
        )
        return ReferencePlaceholder(
            kind="include",
            name=None,
            href=href,
            base=base,
            ns_attribute=element.get("ns"),
            scope=scope,
            children=children,
            origins=origins,
            annotations=annotations,
        )

    def _grammar(
        self,
        element: ET.Element,
        location: IssueLocation,
        parent_scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
        annotations: tuple[Annotation, ...],
        origins: OriginTable,
    ) -> FullGrammar:
        scope = GrammarScope(
            parent=parent_scope,
            origins=origins,
            annotations=annotations,
        )
        self.grammars.append(scope)
        for child, child_location in self._child_specs(element, location):
            scope._append(
                self._grammar_component(child, child_location, scope, base, library, namespace)
            )
        return FullGrammar(scope, origins, annotations)

    def _grammar_component(
        self,
        element: ET.Element,
        location: IssueLocation,
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> FullNode:
        _, tag = _split_tag(element.tag)
        if tag == "start":
            return self._start(element, location, scope, base, library, namespace)
        if tag == "define":
            return self._define(element, location, scope, base, library, namespace)
        if tag == "include":
            return self._include(element, location, scope, base, library, namespace)
        if tag == "div":
            return self._grammar_div(element, location, scope, base, library, namespace)
        return self._node(element, location, scope, base, library, namespace)

    def _grammar_div(
        self,
        element: ET.Element,
        location: IssueLocation,
        scope: GrammarScope | None,
        base: str,
        library: str,
        namespace: str,
    ) -> FullDiv:
        base, library, namespace = self._context(element, location, base, library, namespace)
        annotations = self._annotations(element, location)
        origins = self._origins(element, location)
        self._check_attributes(element, "div", location)
        children = tuple(
            self._grammar_component(child, child_location, scope, base, library, namespace)
            for child, child_location in self._child_specs(element, location)
        )
        return FullDiv(children, origins, annotations)

    def _unknown(self, element: ET.Element, location: IssueLocation) -> FullUnknown:
        children = tuple(
            self._unknown(child, child_location)
            for child, child_location in self._child_locations(element, location)
        )
        return FullUnknown(
            tag=element.tag,
            attributes=tuple(sorted(element.attrib.items())),
            text=element.text,
            children=children,
            origins=self._origins(element, location),
        )
