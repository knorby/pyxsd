"""Ordered RELAX NG 1.0 simplification and grammar composition.

The compiler follows the specification's simplification rules in order.  This
module performs the local prefix of that pipeline: annotation removal,
whitespace normalization, inherited datatype and namespace context, name-class
normalization, ``div`` splicing, child-count normalization, the
``mixed``/``optional``/``zeroOrMore`` rewrites, and the ``notAllowed``/``empty``
reductions.

:class:`GrammarBundle` retains authored scopes, components, references, source
origins, and compilation context. :func:`compose_grammars` expands resources,
allocates definition slots before resolving references, and produces the final
cyclic element graph. Local placeholders never escape that stage.

Every simplified semantic pattern is interned in the caller's
:class:`~pyxsd.relaxng.patterns.PatternPool`; origin tables union beside the
interned key exactly as they do for authored patterns.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal, cast
from xml.etree.ElementTree import ParseError

from pyxsd.locations import IssueLocation
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    DatatypeContext,
    DatatypeLibraryRegistry,
    DatatypeParameter,
    DatatypeParameterError,
    UnsupportedDatatypeError,
)
from pyxsd.relaxng.names import (
    AnyName,
    ExceptName,
    Name,
    NameChoice,
    NameClass,
    NsName,
)
from pyxsd.relaxng.origins import OriginTable
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Data,
    DataExcept,
    Element,
    Empty,
    Group,
    Interleave,
    List,
    NotAllowed,
    OneOrMore,
    Pattern,
    PatternPool,
    Ref,
    Text,
    Value,
    choice,
    group,
    interleave,
    one_or_more,
)
from pyxsd.relaxng.resources import LoadedResource, ResourceDependency, ResourceLoader
from pyxsd.relaxng.syntax import (
    FullAnyName,
    FullAttribute,
    FullChoice,
    FullData,
    FullDefine,
    FullDiv,
    FullElement,
    FullEmpty,
    FullExcept,
    FullGrammar,
    FullGroup,
    FullInterleave,
    FullList,
    FullMixed,
    FullName,
    FullNameChoice,
    FullNameClass,
    FullNameExcept,
    FullNode,
    FullNotAllowed,
    FullNsName,
    FullOneOrMore,
    FullOptional,
    FullParam,
    FullPattern,
    FullSchema,
    FullStart,
    FullText,
    FullUnknown,
    FullValue,
    FullZeroOrMore,
    GrammarScope,
    ReferencePlaceholder,
    SchemaFinding,
    read_full_syntax,
)
from pyxsd.relaxng.whitespace import trim_xml_whitespace
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.validation import ValidationReport

__all__ = [
    "BundleDefine",
    "BundleInclude",
    "BundleScope",
    "BundleStart",
    "CompiledGrammar",
    "Definition",
    "GrammarBundle",
    "LocalGrammar",
    "Simplifier",
    "UnresolvedReference",
    "compose_grammars",
    "simplify_schema",
]

#: XML Namespaces reserves this URI; a RELAX NG attribute name class cannot
#: mention it.  The RELAX NG specification writes the URI without the trailing
#: slash used by some other specifications.
_XMLNS_NAMESPACE = "http://www.w3.org/2000/xmlns"

#: Patterns whose authored ``pattern+`` content is grouped into one pattern.
_GroupableContent = (
    FullElement | FullList | FullMixed | FullOneOrMore | FullOptional | FullZeroOrMore
)


@dataclass(frozen=True)
class BundleStart:
    """One simplified ``start`` component awaiting grammar composition."""

    combine: str | None
    pattern: Pattern
    origins: OriginTable


@dataclass(frozen=True)
class BundleDefine:
    """One simplified ``define`` component awaiting grammar composition."""

    name: str | None
    combine: str | None
    pattern: Pattern
    origins: OriginTable


@dataclass(frozen=True)
class BundleInclude:
    """One unresolved ``include`` with its authored override components."""

    href: str | None
    base: str | None
    ns_attribute: str | None
    components: tuple[BundleStart | BundleDefine, ...]
    origins: OriginTable


class BundleScope:
    """One simplified grammar scope.

    The scope object is the stable identity a local reference points at.
    Components are appended exactly once while the bundle is built and are
    read-only afterwards.
    """

    __slots__ = ("_children", "_key", "_origins", "_parent")

    def __init__(
        self,
        *,
        key: int,
        parent: BundleScope | None,
        origins: OriginTable,
    ) -> None:
        self._key = key
        self._parent = parent
        self._origins = origins
        self._children: list[BundleStart | BundleDefine | BundleInclude] = []

    def _append(self, component: BundleStart | BundleDefine | BundleInclude) -> None:
        self._children.append(component)

    @property
    def key(self) -> int:
        """The scope's preorder index in :attr:`GrammarBundle.scopes`."""
        return self._key

    @property
    def parent(self) -> BundleScope | None:
        """The enclosing simplified grammar scope, if any."""
        return self._parent

    @property
    def origins(self) -> OriginTable:
        """The origin of the authored ``grammar`` element."""
        return self._origins

    @property
    def children(self) -> tuple[BundleStart | BundleDefine | BundleInclude, ...]:
        """Every simplified grammar component in document order."""
        return tuple(self._children)

    @property
    def starts(self) -> tuple[BundleStart, ...]:
        """The simplified ``start`` components."""
        return tuple(child for child in self._children if isinstance(child, BundleStart))

    @property
    def definitions(self) -> tuple[BundleDefine, ...]:
        """The simplified ``define`` components."""
        return tuple(child for child in self._children if isinstance(child, BundleDefine))

    @property
    def includes(self) -> tuple[BundleInclude, ...]:
        """The unresolved ``include`` components."""
        return tuple(child for child in self._children if isinstance(child, BundleInclude))

    def __repr__(self) -> str:
        return (
            f"BundleScope(key={self._key}, starts={len(self.starts)}, "
            f"definitions={len(self.definitions)}, includes={len(self.includes)})"
        )


class LocalGrammar(Ref):
    """An unresolved nested ``grammar`` pattern.

    Composition replaces this atom with the simplified start content of its
    scope.  It is a :class:`~pyxsd.relaxng.patterns.Ref` subclass only so the
    pattern pool treats it as an opaque, already-interned atom instead of
    trying to rebuild it as a normal pattern.
    """

    __slots__ = ("scope",)

    scope: BundleScope

    def __init__(self, scope: BundleScope) -> None:
        super().__init__()
        self.scope = scope

    def __repr__(self) -> str:
        return f"LocalGrammar(scope={self.scope.key})"

    @property
    def origins(self) -> OriginTable:
        """The authored grammar occurrence, independent of pool reductions."""
        return self.scope.origins


class UnresolvedReference(Ref):
    """An authored reference preserved until grammar composition.

    ``scope`` is the simplified scope containing the reference, or ``None``
    for a reference outside any grammar.  It is an opaque
    :class:`~pyxsd.relaxng.patterns.Ref` atom until composition binds the
    matching definition.
    """

    __slots__ = ("base", "href", "kind", "name", "ns_attribute", "origins", "scope")

    kind: Literal["ref", "parentRef", "externalRef"]
    name: str | None
    href: str | None
    base: str | None
    ns_attribute: str | None
    scope: BundleScope | None

    def __init__(
        self,
        kind: Literal["ref", "parentRef", "externalRef"],
        name: str | None,
        href: str | None,
        base: str | None,
        ns_attribute: str | None,
        scope: BundleScope | None,
        origins: OriginTable,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.name = name
        self.href = href
        self.base = base
        self.ns_attribute = ns_attribute
        self.scope = scope
        self.origins = origins

    def __repr__(self) -> str:
        scope_key = None if self.scope is None else self.scope.key
        return (
            f"UnresolvedReference(kind={self.kind!r}, name={self.name!r}, "
            f"href={self.href!r}, scope={scope_key!r})"
        )


@dataclass(frozen=True)
class GrammarBundle:
    """The locally simplified schema, preserving unresolved grammar structure.

    ``start`` is the simplified top-level pattern.  A ``grammar`` root (or a
    nested grammar in pattern content) appears as :class:`LocalGrammar`.
    ``scopes`` lists every inline grammar scope in preorder, so
    :attr:`root_scope` is the outermost scope when the schema root is one.
    """

    start: Pattern
    scopes: tuple[BundleScope, ...]
    report: ValidationReport = field(repr=False, compare=False)
    graph: PatternPool = field(repr=False, compare=False)
    libraries: DatatypeLibraryRegistry = field(repr=False, compare=False)
    resource: LoadedResource = field(repr=False, compare=False)
    findings: tuple[SchemaFinding, ...] = field(repr=False, compare=False)
    references: tuple[UnresolvedReference | LocalGrammar, ...] = field(repr=False, compare=False)

    @property
    def root_scope(self) -> BundleScope | None:
        """The top-level grammar scope, or ``None`` for a bare pattern."""
        if isinstance(self.start, LocalGrammar):
            return self.start.scope
        return None


class Simplifier:
    """Apply RELAX NG's ordered local simplification rules.

    One instance is bound to a report, pattern pool, and datatype registry.
    :meth:`simplify` may be called more than once; grammar scope bookkeeping
    is reset for each schema.
    """

    def __init__(
        self,
        report: ValidationReport,
        pool: PatternPool,
        libraries: DatatypeLibraryRegistry,
    ) -> None:
        self.report = report
        self.pool = pool
        self.libraries = libraries
        self._scopes: dict[GrammarScope, BundleScope] = {}
        self._bundle_scopes: list[BundleScope] = []
        self._references: list[UnresolvedReference | LocalGrammar] = []
        self._patterns: dict[int, Pattern] = {}

    def simplify(self, schema: FullSchema) -> GrammarBundle:
        """Simplify *schema* into a local :class:`GrammarBundle`."""
        self._scopes = {}
        self._bundle_scopes = []
        self._references = []
        self._patterns = {}
        start = self._root_pattern(schema.start)
        return GrammarBundle(
            start,
            tuple(self._bundle_scopes),
            self.report,
            self.pool,
            self.libraries,
            schema.resource,
            schema.findings,
            tuple(self._references),
        )

    # ------------------------------------------------------------------
    # Patterns

    def _root_pattern(self, node: FullPattern) -> Pattern:
        if isinstance(node, FullDiv):
            return self._div_pattern(node)
        return self._pattern(node)

    def _pattern(self, node: FullNode) -> Pattern:
        """Simplify pattern content in postorder without authored call depth.

        Grammar scopes still establish their environment before simplifying
        their components. Only actual pattern operands are scheduled here;
        invalid children remain diagnostics, not accidentally active scopes.
        """
        pending = [(node, False)]
        while pending:
            current, ready = pending.pop()
            if id(current) in self._patterns:
                continue
            if not ready:
                pending.append((current, True))
                children: tuple[FullNode, ...] = ()
                if isinstance(
                    current,
                    _GroupableContent | FullAttribute | FullGroup | FullInterleave | FullChoice,
                ):
                    children = current.children
                pending.extend(
                    (child, False) for child in reversed(tuple(_spliced_patterns(children)))
                )
                continue
            self._patterns[id(current)] = self._pattern_node(current)
        return self._patterns[id(node)]

    def _pattern_node(self, node: FullNode) -> Pattern:
        if isinstance(node, FullElement):
            return self._element(node)
        if isinstance(node, FullAttribute):
            return self._attribute(node)
        if isinstance(node, FullGroup):
            return self._nary(node, group, Empty)
        if isinstance(node, FullInterleave):
            return self._nary(node, interleave, Empty)
        if isinstance(node, FullChoice):
            return self._nary(node, choice, NotAllowed)
        if isinstance(node, FullOptional):
            content = self._grouped_content(node)
            empty = self._intern(Empty(), node.origins)
            return self._intern(choice(content, empty), node.origins)
        if isinstance(node, FullZeroOrMore):
            content = self._grouped_content(node)
            repeated = self._intern(one_or_more(content), node.origins)
            empty = self._intern(Empty(), node.origins)
            return self._intern(choice(repeated, empty), node.origins)
        if isinstance(node, FullOneOrMore):
            content = self._grouped_content(node)
            return self._intern(one_or_more(content), node.origins)
        if isinstance(node, FullList):
            content = self._grouped_content(node)
            if isinstance(content, NotAllowed):
                return self._intern(NotAllowed(), node.origins)
            return self._intern(List(content), node.origins)
        if isinstance(node, FullMixed):
            content = self._grouped_content(node)
            text = self._intern(Text(), node.origins)
            return self._intern(interleave(content, text), node.origins)
        if isinstance(node, FullEmpty):
            self._reject_children(node)
            return self._intern(Empty(), node.origins)
        if isinstance(node, FullNotAllowed):
            self._reject_children(node)
            return self._intern(NotAllowed(), node.origins)
        if isinstance(node, FullText):
            self._reject_children(node)
            return self._intern(Text(), node.origins)
        if isinstance(node, FullValue):
            return self._value(node)
        if isinstance(node, FullData):
            return self._data(node)
        if isinstance(node, FullGrammar):
            return self._grammar(node)
        if isinstance(node, ReferencePlaceholder):
            return self._reference(node)
        if isinstance(node, FullUnknown):
            self._error(
                "relaxng-unsupported-pattern",
                f"the unknown RELAX NG element {node.tag!r} has no simplified pattern",
                node.origins,
            )
            return self._intern(NotAllowed(), node.origins)
        self._unexpected_pattern(node)
        return self._intern(NotAllowed(), _node_origins(node))

    def _element(self, node: FullElement) -> Pattern:
        name_class = self._name_class(node.name_class, attribute=False, origins=node.origins)
        origins = node.origins
        if node.name_class is not None:
            origins = origins.union(_name_class_origins(node.name_class))
        content = self._grouped_content(node)
        return self._intern(Element(name_class, content), origins)

    def _attribute(self, node: FullAttribute) -> Pattern:
        name_class = self._name_class(node.name_class, attribute=True, origins=node.origins)
        origins = node.origins
        if node.name_class is not None:
            origins = origins.union(_name_class_origins(node.name_class))
        parts = self._pattern_children(node.children)
        div_origins = _grouping_origins(node.children)
        content_origins = node.origins.union(div_origins)
        if not parts:
            content = self._intern(Text(), content_origins)
        elif len(parts) == 1:
            content = self._intern(parts[0], div_origins)
        else:
            self._error(
                "relaxng-invalid-attribute-content",
                "a RELAX NG attribute element allows at most one content pattern",
                node.origins,
            )
            content = self._fold(group, parts, content_origins)
        if isinstance(content, NotAllowed):
            return self._intern(NotAllowed(), origins)
        return self._intern(Attribute(name_class, content), origins)

    def _nary(
        self,
        node: FullGroup | FullInterleave | FullChoice,
        constructor: Callable[[Pattern, Pattern], Pattern],
        empty: Callable[[], Pattern],
    ) -> Pattern:
        parts = self._pattern_children(node.children)
        origins = node.origins.union(_grouping_origins(node.children))
        if not parts:
            self._error(
                "relaxng-empty-pattern-content",
                f"a RELAX NG {type(node).__name__} needs at least one pattern",
                node.origins,
            )
            return self._intern(empty(), origins)
        if len(parts) == 1:
            return self._intern(parts[0], origins)
        return self._fold(constructor, parts, origins)

    def _grouped_content(self, node: _GroupableContent) -> Pattern:
        """Return one content pattern, grouping multiple authored children."""
        parts = self._pattern_children(node.children)
        div_origins = _grouping_origins(node.children)
        origins = node.origins.union(div_origins)
        if not parts:
            self._error(
                "relaxng-empty-pattern-content",
                f"a RELAX NG {type(node).__name__} needs a content pattern",
                origins,
            )
            return self._intern(NotAllowed(), origins)
        if len(parts) == 1:
            return self._intern(parts[0], div_origins)
        return self._fold(group, parts, origins)

    def _div_pattern(self, node: FullDiv) -> Pattern:
        parts = self._pattern_children(node.children)
        origins = node.origins.union(_grouping_origins(node.children))
        if not parts:
            self._error(
                "relaxng-empty-pattern-content",
                "a RELAX NG div needs at least one pattern",
                node.origins,
            )
            return self._intern(NotAllowed(), origins)
        if len(parts) == 1:
            return self._intern(parts[0], origins)
        self._error(
            "relaxng-invalid-div-content",
            "a RELAX NG div at pattern level needs exactly one pattern",
            node.origins,
        )
        return self._fold(group, parts, origins)

    def _pattern_children(self, children: tuple[FullNode, ...]) -> list[Pattern]:
        return [self._pattern(child) for child in _spliced_patterns(children)]

    def _fold(
        self,
        constructor: Callable[[Pattern, Pattern], Pattern],
        parts: list[Pattern],
        origins: OriginTable,
    ) -> Pattern:
        result = parts[0]
        for part in parts[1:]:
            result = self._intern(constructor(result, part), origins)
        return result

    def _reject_children(self, node: FullEmpty | FullNotAllowed | FullText) -> None:
        for child in node.children:
            self._unexpected_pattern(child)

    def _unexpected_pattern(self, node: FullNode) -> None:
        self._error(
            "relaxng-unexpected-pattern",
            f"the RELAX NG {type(node).__name__} construct is not a pattern here",
            _node_origins(node),
        )

    def _reference(self, node: ReferencePlaceholder) -> Pattern:
        if node.kind == "include":
            self._unexpected_pattern(node)
            return self._intern(NotAllowed(), node.origins)
        for child in node.children:
            self._unexpected_pattern(child)
        name = _strip_name(node.name)
        if node.kind in {"ref", "parentRef"} and node.name is not None and name is None:
            self._error(
                "relaxng-missing-name",
                f"a RELAX NG {node.kind} element needs a non-blank name",
                node.origins,
            )
        scope = self._scopes.get(node.scope) if node.scope is not None else None
        reference = UnresolvedReference(
            node.kind,
            name,
            node.href,
            node.base,
            node.ns_attribute,
            scope,
            node.origins,
        )
        self._references.append(reference)
        return self._intern(reference, node.origins)

    def _value(self, node: FullValue) -> Pattern:
        for child in node.children:
            self._unexpected_pattern(child)
        type_name = node.type_name
        library = node.datatype_library
        if type_name is None:
            type_name = "token"
            library = BUILTIN_LIBRARY
        else:
            type_name = trim_xml_whitespace(type_name)
            if not type_name:
                self._error(
                    "relaxng-missing-datatype",
                    "a RELAX NG value type must name a datatype",
                    node.origins,
                )
                return self._intern(NotAllowed(), node.origins)
        context = DatatypeContext(node.base_uri, self._value_namespaces(node))
        try:
            datatype = self.libraries.create((library, type_name), (), context)
        except DatatypeParameterError as exc:
            self._error("relaxng-invalid-datatype", str(exc), node.origins)
            return self._intern(NotAllowed(), node.origins)
        except UnsupportedDatatypeError as exc:
            if library != BUILTIN_LIBRARY:
                raise
            self._error("relaxng-unknown-datatype", str(exc), node.origins)
            return self._intern(NotAllowed(), node.origins)
        return self._intern(Value(datatype, node.lexical), node.origins)

    def _value_namespaces(self, node: FullValue) -> dict[str, str]:
        namespaces = dict(node.namespaces)
        if node.namespace_uri is None:
            namespaces.pop("", None)
        else:
            namespaces[""] = node.namespace_uri
        return namespaces

    def _data(self, node: FullData) -> Pattern:
        origins = node.origins
        parameters: list[DatatypeParameter] = []
        excepts: list[FullExcept] = []
        for child in node.children:
            if isinstance(child, FullParam):
                for grandchild in child.children:
                    self._unexpected_pattern(grandchild)
                name = _strip_name(child.name)
                if name is None:
                    self._error(
                        "relaxng-invalid-parameter",
                        "a RELAX NG param element needs a name",
                        child.origins,
                    )
                    return self._intern(NotAllowed(), origins)
                parameters.append(DatatypeParameter(name, child.value))
                origins = origins.union(child.origins)
            elif isinstance(child, FullExcept):
                excepts.append(child)
                origins = origins.union(child.origins)
            else:
                self._unexpected_pattern(child)
        type_name = trim_xml_whitespace(node.type_name) if node.type_name is not None else ""
        if not type_name:
            self._error(
                "relaxng-missing-datatype",
                "a RELAX NG data element needs a type attribute",
                origins,
            )
            return self._intern(NotAllowed(), origins)
        context = DatatypeContext(node.base_uri, dict(node.namespaces))
        try:
            datatype = self.libraries.create(
                (node.datatype_library, type_name), tuple(parameters), context
            )
        except DatatypeParameterError as exc:
            self._error("relaxng-invalid-datatype", str(exc), origins)
            return self._intern(NotAllowed(), origins)
        except UnsupportedDatatypeError as exc:
            if node.datatype_library != BUILTIN_LIBRARY:
                raise
            self._error("relaxng-unknown-datatype", str(exc), origins)
            return self._intern(NotAllowed(), origins)
        if len(excepts) > 1:
            self._error(
                "relaxng-duplicate-except",
                "a RELAX NG data element allows at most one except",
                node.origins,
            )
        excluded_parts: list[Pattern] = []
        for except_node in excepts:
            except_origins = except_node.origins.union(_grouping_origins(except_node.children))
            origins = origins.union(except_origins)
            parts = self._pattern_children(except_node.children)
            if not parts:
                self._error(
                    "relaxng-invalid-except-content",
                    "a RELAX NG except element needs at least one pattern",
                    except_node.origins,
                )
                continue
            if len(parts) == 1:
                excluded_parts.append(self._intern(parts[0], except_origins))
            else:
                excluded_parts.append(self._fold(choice, parts, except_origins))
        if not excluded_parts:
            return self._intern(Data(datatype), origins)
        excluded = (
            excluded_parts[0]
            if len(excluded_parts) == 1
            else self._fold(choice, excluded_parts, origins)
        )
        if isinstance(excluded, NotAllowed):
            return self._intern(Data(datatype), origins)
        return self._intern(DataExcept(datatype, excluded), origins)

    # ------------------------------------------------------------------
    # Grammars and references

    def _grammar(self, node: FullGrammar) -> Pattern:
        scope = self._bundle_scope(node.scope)
        grammar = LocalGrammar(scope)
        self._references.append(grammar)
        return self._intern(grammar, node.origins)

    def _bundle_scope(self, source_scope: GrammarScope) -> BundleScope:
        parent_source = source_scope.parent
        parent = self._scopes.get(parent_source) if parent_source is not None else None
        scope = BundleScope(
            key=len(self._bundle_scopes),
            parent=parent,
            origins=source_scope.origins,
        )
        self._scopes[source_scope] = scope
        self._bundle_scopes.append(scope)
        for component in self._grammar_components(source_scope.children, allow_include=True):
            scope._append(component)
        return scope

    def _grammar_components(
        self, nodes: tuple[FullNode, ...], *, allow_include: bool
    ) -> list[BundleStart | BundleDefine | BundleInclude]:
        components: list[BundleStart | BundleDefine | BundleInclude] = []
        for node in nodes:
            if isinstance(node, FullDiv):
                components.extend(
                    self._grammar_components(node.children, allow_include=allow_include)
                )
                continue
            if isinstance(node, FullStart):
                components.append(self._bundle_start(node))
                continue
            if isinstance(node, FullDefine):
                components.append(self._bundle_define(node))
                continue
            if isinstance(node, ReferencePlaceholder) and node.kind == "include":
                if allow_include:
                    components.append(self._bundle_include(node))
                else:
                    self._error(
                        "relaxng-invalid-include-content",
                        "a RELAX NG include cannot contain another include",
                        node.origins,
                    )
                continue
            code = (
                "relaxng-invalid-grammar-content"
                if allow_include
                else "relaxng-invalid-include-content"
            )
            self._error(
                code,
                f"the RELAX NG {type(node).__name__} construct is not grammar content here",
                _node_origins(node),
            )
        return components

    def _bundle_start(self, node: FullStart) -> BundleStart:
        parts = self._pattern_children(node.children)
        div_origins = _grouping_origins(node.children)
        pattern_origins = node.origins.union(div_origins)
        if not parts:
            self._error(
                "relaxng-invalid-start-content",
                "a RELAX NG start element needs exactly one pattern",
                node.origins,
            )
            pattern = self._intern(NotAllowed(), pattern_origins)
        elif len(parts) == 1:
            pattern = self._intern(parts[0], div_origins)
        else:
            self._error(
                "relaxng-invalid-start-content",
                "a RELAX NG start element needs exactly one pattern",
                node.origins,
            )
            pattern = self._fold(group, parts, pattern_origins)
        return BundleStart(_combine(node.combine), pattern, node.origins)

    def _bundle_define(self, node: FullDefine) -> BundleDefine:
        parts = self._pattern_children(node.children)
        div_origins = _grouping_origins(node.children)
        pattern_origins = node.origins.union(div_origins)
        name = _strip_name(node.name)
        if node.name is not None and name is None:
            self._error(
                "relaxng-missing-name",
                "a RELAX NG define element needs a non-blank name",
                node.origins,
            )
        if not parts:
            self._error(
                "relaxng-invalid-define-content",
                "a RELAX NG define element needs at least one pattern",
                node.origins,
            )
            pattern = self._intern(NotAllowed(), pattern_origins)
        elif len(parts) == 1:
            pattern = self._intern(parts[0], div_origins)
        else:
            pattern = self._fold(group, parts, pattern_origins)
        return BundleDefine(name, _combine(node.combine), pattern, node.origins)

    def _bundle_include(self, node: ReferencePlaceholder) -> BundleInclude:
        components = cast(
            tuple[BundleStart | BundleDefine, ...],
            tuple(self._grammar_components(node.children, allow_include=False)),
        )
        return BundleInclude(
            node.href,
            node.base,
            node.ns_attribute,
            components,
            node.origins,
        )

    # ------------------------------------------------------------------
    # Name classes

    def _name_class(
        self,
        node: FullNameClass | None,
        *,
        attribute: bool,
        origins: OriginTable,
    ) -> NameClass:
        if node is None:
            self._error(
                "relaxng-missing-name-class",
                "a RELAX NG element or attribute needs a name class",
                origins,
            )
            return NameChoice(())
        name_class = self._name_class_node(node, attribute=attribute)
        if attribute:
            self._check_attribute_name_class(name_class, origins)
        return name_class

    def _name_class_node(self, node: FullNameClass, *, attribute: bool) -> NameClass:
        if isinstance(node, FullName):
            return Name(node.uri, node.local)
        if isinstance(node, FullAnyName):
            return self._wildcard_name_class(
                AnyName(), node, node.except_class, attribute=attribute, any_name=True
            )
        if isinstance(node, FullNsName):
            return self._wildcard_name_class(
                NsName(node.uri),
                node,
                node.except_class,
                attribute=attribute,
                any_name=False,
            )
        if isinstance(node, (FullNameChoice, FullNameExcept)):
            return self._name_class_union(node, attribute=attribute)
        self._error(
            "relaxng-empty-name-class",
            f"the RELAX NG {type(node).__name__} name class has no alternatives",
            _node_origins(node),
        )
        return NameChoice(())

    def _wildcard_name_class(
        self,
        base: AnyName | NsName,
        node: FullAnyName | FullNsName,
        except_class: FullNameClass | None,
        *,
        attribute: bool,
        any_name: bool,
    ) -> NameClass:
        if except_class is None:
            return base
        if any_name:
            if _full_name_class_mentions(except_class, (FullAnyName,)):
                self._error(
                    "relaxng-any-name-except",
                    "a RELAX NG anyName except cannot contain anyName",
                    node.origins,
                )
        elif _full_name_class_mentions(except_class, (FullAnyName, FullNsName)):
            self._error(
                "relaxng-ns-name-except",
                "a RELAX NG nsName except cannot contain anyName or nsName",
                node.origins,
            )
        excluded = self._name_class_node(except_class, attribute=attribute)
        if isinstance(excluded, NameChoice) and not excluded.alternatives:
            return base
        return ExceptName(base, excluded)

    def _name_class_union(
        self, node: FullNameChoice | FullNameExcept, *, attribute: bool
    ) -> NameClass:
        if not node.alternatives:
            self._error(
                "relaxng-empty-name-class",
                f"a RELAX NG {type(node).__name__} needs at least one alternative",
                node.origins,
            )
            return NameChoice(())
        alternatives = [
            self._name_class_node(alternative, attribute=attribute)
            for alternative in node.alternatives
        ]
        return _name_choice(alternatives)

    def _check_attribute_name_class(self, name_class: NameClass, origins: OriginTable) -> None:
        for item in _walk_name_classes(name_class):
            if isinstance(item, Name):
                if item.uri is None and item.local == "xmlns":
                    self._error(
                        "relaxng-attribute-name-xmlns",
                        "a RELAX NG attribute name cannot be xmlns without a namespace",
                        origins,
                    )
                elif item.uri == _XMLNS_NAMESPACE:
                    self._error(
                        "relaxng-attribute-name-namespace",
                        "a RELAX NG attribute name cannot use the xmlns namespace",
                        origins,
                    )
            elif isinstance(item, NsName) and item.uri == _XMLNS_NAMESPACE:
                self._error(
                    "relaxng-attribute-name-namespace",
                    "a RELAX NG attribute name cannot use the xmlns namespace",
                    origins,
                )

    # ------------------------------------------------------------------
    # Diagnostics

    def _error(self, code: str, message: str, origins: OriginTable) -> None:
        self.report.add_error(
            message,
            code=code,
            phase="schema",
            location=_location(origins),
        )

    def _intern(self, pattern: Pattern, origins: OriginTable) -> Pattern:
        return self.pool.intern(pattern, origins)


@dataclass(frozen=True)
class Definition:
    """One reachable element definition in the final cyclic graph."""

    name: str
    pattern: Element
    origins: OriginTable


@dataclass(frozen=True)
class CompiledGrammar:
    """A composed graph; all remaining reference edges are bound to elements."""

    start: Pattern
    definitions: Mapping[str, Definition]
    graph: PatternPool
    dependencies: tuple[ResourceDependency, ...]


class _ElementReference(Ref):
    """A final cycle edge with a deterministic, traversal-assigned identity.

    Unlike a bare Ref's debugging repr, this key is independent of addresses.
    The pool can therefore canonicalize choices containing recursive edges
    without inspecting their targets or recursing through a cycle.
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def __repr__(self) -> str:
        return f"ElementRef({self.name!r})"


@dataclass(frozen=True, eq=False)
class _Part:
    name: str | None
    combine: str | None
    pattern: Pattern
    origins: OriginTable
    environment: Mapping[BundleScope, _Scope]
    enclosing: _Scope | None = None
    checks: tuple[UnresolvedReference | LocalGrammar, ...] = ()


class _Slot(Ref):
    """Temporary named edge allocated before any definition is resolved."""

    def __init__(self, key: str, parts: list[_Part]) -> None:
        super().__init__()
        self.key = key
        self.parts = parts
        self.pattern: Pattern | None = None
        self.origins = OriginTable().union(*(part.origins for part in parts))

    def __repr__(self) -> str:
        return f"DefinitionSlot({self.key!r})"


class _Scope:
    def __init__(self, key: int, parent: _Scope | None, origins: OriginTable) -> None:
        self.key = key
        self.parent = parent
        self.origins = origins
        self.parts: list[_Part] = []
        self.slots: dict[str | None, _Slot] | None = None


def _reference_checks(
    references: tuple[UnresolvedReference | LocalGrammar, ...],
    origins: OriginTable,
    scope: BundleScope,
) -> tuple[UnresolvedReference | LocalGrammar, ...]:
    location = _location(origins)
    assert location is not None
    return tuple(
        reference
        for reference in references
        if _reference_scope(reference) is scope
        and any(
            origin.location is not None
            and origin.location.path[: len(location.path)] == location.path
            for origin in reference.origins
        )
    )


def _reference_scope(reference: UnresolvedReference | LocalGrammar) -> BundleScope | None:
    return reference.scope.parent if isinstance(reference, LocalGrammar) else reference.scope


def _pattern_operands(pattern: Pattern) -> tuple[Pattern, ...]:
    if isinstance(pattern, (Choice, Group, Interleave)):
        return pattern.first, pattern.second
    if isinstance(pattern, (Element, Attribute, List, OneOrMore)):
        return (pattern.pattern,)
    if isinstance(pattern, DataExcept):
        return (pattern.excluded,)
    return ()


def _rebuild_pattern(pattern: Pattern, children: tuple[Pattern, ...]) -> Pattern:
    if isinstance(pattern, Choice):
        return choice(*children)
    if isinstance(pattern, Group):
        return group(*children)
    if isinstance(pattern, Interleave):
        return interleave(*children)
    if isinstance(pattern, OneOrMore):
        return one_or_more(children[0])
    if isinstance(pattern, (Element, Attribute, List)):
        if isinstance(pattern, (Attribute, List)) and isinstance(children[0], NotAllowed):
            return NotAllowed()
        if isinstance(pattern, List):
            return List(children[0])
        return type(pattern)(pattern.name_class, children[0])
    if isinstance(pattern, DataExcept):
        if isinstance(children[0], NotAllowed):
            return Data(pattern.datatype)
        return DataExcept(pattern.datatype, children[0])
    return pattern


class _Composer:
    """Scope expansion first, reference resolution second, element graph last.

    The temporary slots never escape. Non-element references are expanded
    with an active-path check (specification 4.19); element boundaries enqueue
    work instead, so legal recursive definitions never recursively expand.
    Undefined references and combine errors are checked before reachability.
    """

    def __init__(self, bundle: GrammarBundle, loader: ResourceLoader) -> None:
        self.report = bundle.report
        self.pool = bundle.graph
        self.libraries = bundle.libraries
        self.loader = loader
        self.scopes: list[_Scope] = []
        self.slots: list[_Slot] = []
        # Keep environments alive for the lifetime of identity-keyed memos,
        # including empty environments for external bare patterns.
        self.environments: list[Mapping[BundleScope, _Scope]] = []
        self.element_work: list[tuple[Element, _ElementReference]] = []
        self.expanded: dict[Pattern, Pattern] = {}
        self.resolved: dict[tuple[Pattern, int], Pattern] = {}
        self.external_parts: dict[tuple[Pattern, int], _Part | None] = {}

    def error(self, code: str, message: str, origins: OriginTable) -> None:
        self.report.add_error(message, code=code, phase="schema", location=_location(origins))

    def prepare(
        self,
        bundle: GrammarBundle,
        chain: tuple[str, ...],
        destination: _Scope | None = None,
        parent: _Scope | None = None,
    ) -> tuple[_Part, list[_Part]]:
        for finding in bundle.findings:
            self.report.add_error(
                finding.message, code=finding.code, phase="schema", location=finding.location
            )
        environment: dict[BundleScope, _Scope] = {}
        self.environments.append(environment)
        for scope in bundle.scopes:
            enclosing = environment[scope.parent] if scope.parent is not None else parent
            if scope is bundle.root_scope and destination is not None:
                state = destination
            else:
                state = _Scope(len(self.scopes), enclosing, scope.origins)
                self.scopes.append(state)
            environment[scope] = state
        # externalRef precedes include overrides in section 4. Its resources
        # must be read even when the containing definition will be overridden.
        # Preparation does not resolve named refs against half-built scopes.
        for reference in bundle.references:
            if isinstance(reference, UnresolvedReference) and reference.kind == "externalRef":
                loaded = self.load(reference, bundle.resource, chain)
                external_root = None
                if loaded is not None:
                    external_bundle, external_chain = loaded
                    enclosing = (
                        environment[reference.scope] if reference.scope is not None else parent
                    )
                    external_root, _ = self.prepare(
                        external_bundle, external_chain, parent=enclosing
                    )
                self.external_parts[(reference, id(environment))] = external_root
        included: list[_Part] = []
        for scope in bundle.scopes:
            state = environment[scope]
            parts = self.components(scope, environment, bundle.resource, chain, bundle.references)
            if scope is bundle.root_scope and destination is not None:
                included = parts
            else:
                state.parts.extend(parts)
        root = _Part(
            None,
            None,
            bundle.start,
            self.pool.origins(bundle.start),
            environment,
            parent,
            tuple(ref for ref in bundle.references if _reference_scope(ref) is None),
        )
        return root, included

    def components(
        self,
        scope: BundleScope,
        environment: Mapping[BundleScope, _Scope],
        resource: LoadedResource,
        chain: tuple[str, ...],
        references: tuple[UnresolvedReference | LocalGrammar, ...],
    ) -> list[_Part]:
        parts: list[_Part] = []
        for component in scope.children:
            if not isinstance(component, BundleInclude):
                name = component.name if isinstance(component, BundleDefine) else None
                if isinstance(component, BundleDefine) and name is None:
                    self.error("relaxng-missing-name", "a define needs a name", component.origins)
                    continue
                parts.append(
                    _Part(
                        name,
                        component.combine,
                        component.pattern,
                        component.origins,
                        environment,
                        checks=_reference_checks(references, component.origins, scope),
                    )
                )
                continue
            loaded = self.load(component, resource, chain)
            if loaded is None:
                continue
            bundle, next_chain = loaded
            if bundle.root_scope is None:
                self.error(
                    "relaxng-include-not-grammar",
                    "include must reference a grammar",
                    component.origins,
                )
                continue
            _, imported = self.prepare(bundle, next_chain, environment[scope])
            overrides: dict[str | None, OriginTable] = {}
            for override in component.components:
                name = override.name if isinstance(override, BundleDefine) else None
                removed = [part for part in imported if part.name == name]
                if not removed:
                    self.error(
                        "relaxng-include-override",
                        f"include overrides absent component {name or 'start'!r}",
                        override.origins,
                    )
                origins = component.origins.union(
                    override.origins, *(part.origins for part in removed)
                )
                overrides[name] = origins
                parts.append(
                    _Part(
                        name,
                        override.combine,
                        override.pattern,
                        origins,
                        environment,
                        checks=_reference_checks(references, override.origins, scope),
                    )
                )
            parts.extend(
                replace(part, origins=part.origins.union(component.origins, scope.origins))
                for part in imported
                if part.name not in overrides
            )
        return parts

    def load(
        self,
        reference: BundleInclude | UnresolvedReference,
        containing: LoadedResource,
        chain: tuple[str, ...],
    ) -> tuple[GrammarBundle, tuple[str, ...]] | None:
        origins = reference.origins
        if reference.href is None:
            self.error("relaxng-missing-href", "a resource reference needs an href", origins)
            return None
        try:
            resource = self.loader.resolve(
                reference.href, containing, reference.base, location=_location(origins)
            )
        except (OSError, ParseError, ResourcePolicyError) as exc:
            self.error("relaxng-resource-read", str(exc), origins)
            return None
        if resource.canonical_id in chain:
            self.error("relaxng-resource-cycle", "include/externalRef resource cycle", origins)
            return None
        namespace = reference.ns_attribute
        if namespace is None:
            namespace = containing.namespace_at(origins)
        resource = resource.with_namespace(namespace)
        bundle = Simplifier(self.report, self.pool, self.libraries).simplify(
            read_full_syntax(resource)
        )
        return bundle, (*chain, resource.canonical_id)

    def setup(self, scope: _Scope) -> dict[str | None, _Slot]:
        if scope.slots is not None:
            return scope.slots
        grouped: dict[str | None, list[_Part]] = {}
        for part in scope.parts:
            grouped.setdefault(part.name, []).append(part)
        scope.slots = {}
        for name, parts in grouped.items():
            slot = _Slot(f"scope{scope.key}:{name or '#start'}", parts)
            scope.slots[name] = slot
            self.slots.append(slot)
        if None not in scope.slots:
            self.error("relaxng-missing-start", "a grammar needs a start", scope.origins)
        return scope.slots

    def resolve(self, pattern: Pattern, part: _Part) -> Pattern:
        pending = [(pattern, False)]
        environment = id(part.environment)
        while pending:
            node, ready = pending.pop()
            if (node, environment) in self.resolved:
                continue
            children = _pattern_operands(node)
            if children and not ready:
                pending.append((node, True))
                pending.extend((child, False) for child in reversed(children))
                continue
            self._resolve_node(node, part)
        return self.resolved[(pattern, environment)]

    def _resolve_node(self, pattern: Pattern, part: _Part) -> Pattern:
        memo_key = (pattern, id(part.environment))
        origins = self.pool.origins(pattern)
        if isinstance(pattern, LocalGrammar):
            local_scope = part.environment[pattern.scope]
            result: Pattern = self.setup(local_scope).get(None, NotAllowed())
        elif isinstance(pattern, UnresolvedReference):
            scope = part.environment[pattern.scope] if pattern.scope is not None else part.enclosing
            if pattern.kind == "externalRef":
                root = self.external_parts[memo_key]
                result = NotAllowed() if root is None else self.resolve_part(root)
            else:
                if pattern.kind == "parentRef":
                    scope = scope.parent if scope is not None else None
                if scope is None:
                    self.error(
                        "relaxng-parent-reference"
                        if pattern.kind == "parentRef"
                        else "relaxng-undefined-reference",
                        f"{pattern.kind} has no enclosing grammar",
                        origins,
                    )
                    result = NotAllowed()
                else:
                    result = (
                        self.setup(scope).get(pattern.name, NotAllowed())
                        if pattern.name is not None
                        else NotAllowed()
                    )
                    if isinstance(result, NotAllowed):
                        self.error(
                            "relaxng-undefined-reference",
                            f"no definition for {pattern.name!r}",
                            origins,
                        )
        else:
            result = _rebuild_pattern(
                pattern,
                tuple(
                    self.resolved[(child, id(part.environment))]
                    for child in _pattern_operands(pattern)
                ),
            )
        result = self.pool.intern(result, origins)
        self.resolved[memo_key] = result
        return result

    def resolve_slot(self, slot: _Slot) -> None:
        parts = slot.parts
        methods = {part.combine for part in parts if part.combine is not None}
        if methods - {"choice", "interleave"}:
            self.error(
                "relaxng-invalid-combine", "combine must be choice or interleave", slot.origins
            )
        if {"choice", "interleave"} <= methods:
            self.error("relaxng-conflicting-combine", "combine methods disagree", slot.origins)
        if sum(part.combine is None for part in parts) > 1:
            code = (
                "relaxng-duplicate-start" if parts[0].name is None else "relaxng-duplicate-define"
            )
            self.error(code, "multiple components without combine", slot.origins)
        constructor = interleave if methods == {"interleave"} else choice
        patterns = [self.resolve_part(part) for part in parts]
        result = patterns[0]
        for pattern in patterns[1:]:
            result = self.pool.intern(constructor(result, pattern), slot.origins)
        slot.pattern = self.pool.intern(result, slot.origins)

    def resolve_part(self, part: _Part) -> Pattern:
        # Section 4.18 precedes notAllowed reductions: even a reference
        # erased by a local smart constructor must resolve.
        for reference in part.checks:
            self.resolve(reference, part)
        return self.resolve(part.pattern, part)

    def dependencies(self, node: _Slot | _Part) -> list[tuple[_Slot | _Part, bool]]:
        """Authored dependencies, retaining edges erased by local reductions."""
        if isinstance(node, _Slot):
            return [(part, False) for part in node.parts]
        result: list[tuple[_Slot | _Part, bool]] = []
        pending = [(self.resolve(node.pattern, node), False)]
        seen: set[tuple[Pattern, bool]] = set()
        while pending:
            pattern, guarded = pending.pop()
            if (pattern, guarded) in seen:
                continue
            seen.add((pattern, guarded))
            if isinstance(pattern, _Slot):
                result.append((pattern, guarded))
            else:
                pending.extend(
                    (child, guarded or isinstance(pattern, Element))
                    for child in _pattern_operands(pattern)
                )
        for reference in node.checks:
            location = _location(reference.origins)
            assert location is not None
            tags = [step.name.rsplit("}", 1)[-1] for step in location.path]
            # Start/define components establish a new expansion entry. A
            # reference beneath an element cannot form an expandable loop.
            boundary = max(
                (index for index, tag in enumerate(tags) if tag in {"start", "define"}),
                default=-1,
            )
            guarded = "element" in tags[boundary + 1 :]
            external = self.external_parts.get((reference, id(node.environment)))
            if external is not None:
                result.append((external, guarded))
            else:
                target = self.resolve(reference, node)
                if isinstance(target, _Slot):
                    result.append((target, guarded))
        return result

    def check_recursion(self, root: _Part) -> bool:
        """Check expandable cycles before notAllowed removes reachable refs."""
        edges: dict[_Slot | _Part, list[tuple[_Slot | _Part, bool]]] = {}
        pending: list[_Slot | _Part] = [root]
        while pending:
            node = pending.pop()
            if node in edges:
                continue
            edges[node] = self.dependencies(node)
            pending.extend(child for child, _ in edges[node])
        colors: dict[_Slot | _Part, int] = {}
        invalid = False
        for entry in edges:
            stack = [(entry, False)]
            while stack:
                node, leaving = stack.pop()
                if leaving:
                    colors[node] = 2
                elif colors.get(node) == 1:
                    self.error(
                        "relaxng-recursive-reference",
                        "expanding a non-element definition produces a loop",
                        node.origins,
                    )
                    invalid = True
                elif colors.get(node, 0) == 0:
                    colors[node] = 1
                    stack.append((node, True))
                    stack.extend((child, False) for child, guarded in edges[node] if not guarded)
        return invalid

    def expand(self, pattern: Pattern) -> Pattern:
        # Expand children before their parent without using Python call depth.
        # Element contents remain separate work items, closing guarded cycles
        # through memoized edges rather than following them here.
        pending = [(pattern, False)]
        while pending:
            node, ready = pending.pop()
            origins = self.pool.origins(node)
            if node in self.expanded:
                self.pool.intern(self.expanded[node], origins)
                continue
            if isinstance(node, Element):
                edge = _ElementReference(f"element{len(self.element_work)}")
                self.element_work.append((node, edge))
                result: Pattern = edge
            else:
                if isinstance(node, _Slot):
                    assert node.pattern is not None
                    children: tuple[Pattern, ...] = (node.pattern,)
                else:
                    children = _pattern_operands(node)
                if children and not ready:
                    pending.append((node, True))
                    pending.extend((child, False) for child in reversed(children))
                    continue
                result = (
                    self.expanded[children[0]]
                    if isinstance(node, _Slot)
                    else _rebuild_pattern(node, tuple(self.expanded[child] for child in children))
                )
            self.expanded[node] = self.pool.intern(result, origins)
        return self.expanded[pattern]

    def compose(self, bundle: GrammarBundle) -> CompiledGrammar:
        root, _ = self.prepare(bundle, (bundle.resource.canonical_id,))
        # setup allocates every slot in a scope before resolving its content.
        # Surviving authored grammar occurrences activate scopes, including
        # unreachable definitions, but not subtrees removed by an override.
        start = self.resolve_part(root)
        index = 0
        while index < len(self.slots):
            self.resolve_slot(self.slots[index])
            index += 1
        start = (
            self.pool.intern(NotAllowed(), self.pool.origins(start))
            if self.check_recursion(root)
            else self.expand(start)
        )
        index = 0
        targets: dict[_ElementReference, Element] = {}
        while index < len(self.element_work):
            pattern, edge = self.element_work[index]
            content = self.expand(pattern.pattern)
            target = self.pool.intern(
                Element(pattern.name_class, content),
                self.pool.origins(pattern).union(self.pool.origins(edge)),
            )
            targets[edge] = cast(Element, target)
            index += 1
        # Reindex only the final closure. The staging pool contains authored
        # placeholders, temporary slots, and unreachable patterns. Indexing
        # nodes already charged to the caller is not new compilation work.
        # Canonicalize in the returned pool before closing write-once edges.
        # Ref is opaque to interning, so this also preserves recursive cycles.
        graph = PatternPool()
        start = graph.intern(start, self.pool.origins(start))
        definitions: dict[str, Definition] = {}
        seen: set[Pattern] = set()
        pending = [start]
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            graph.intern(node, self.pool.origins(node))
            if isinstance(node, _ElementReference):
                target = targets[node]
                origins = self.pool.origins(target).union(self.pool.origins(node))
                target = cast(Element, graph.intern(target, origins))
                node.bind(target)
                definitions[node.name] = Definition(node.name, target, origins)
                pending.append(target)
            else:
                pending.extend(reversed(_pattern_operands(node)))
        return CompiledGrammar(
            start, MappingProxyType(definitions), graph, self.loader.dependencies()
        )


def compose_grammars(bundle: GrammarBundle, loader: ResourceLoader) -> CompiledGrammar:
    """Compose a locally simplified bundle using its report and datatype context.

    Findings join ``bundle.report`` in the schema phase. Resource reads use
    only ``loader``; capability failures and budget exhaustion propagate
    unchanged. The result contains no unresolved authored placeholders.
    """
    return _Composer(bundle, loader).compose(bundle)


def simplify_schema(
    schema: FullSchema,
    *,
    report: ValidationReport | None = None,
    pool: PatternPool | None = None,
    libraries: DatatypeLibraryRegistry | None = None,
) -> GrammarBundle:
    """Simplify *schema* into a :class:`GrammarBundle`.

    The optional report, pool, and datatype registry default to fresh
    instances.  A caller that needs the findings or interned origins supplies
    all three, or uses :class:`Simplifier` directly.
    """
    return Simplifier(
        report if report is not None else ValidationReport(),
        pool if pool is not None else PatternPool(),
        libraries if libraries is not None else DatatypeLibraryRegistry(),
    ).simplify(schema)


def _combine(value: str | None) -> str | None:
    return trim_xml_whitespace(value) if value is not None else None


def _strip_name(value: str | None) -> str | None:
    """Strip an authored ``name`` and treat a blank value as missing."""
    if value is None:
        return None
    return trim_xml_whitespace(value) or None


def _node_origins(node: FullNode) -> OriginTable:
    return node.origins  # type: ignore[attr-defined]


def _location(origins: OriginTable) -> IssueLocation | None:
    locations: list[IssueLocation] = []
    for origin in origins:
        if origin.location is not None:
            locations.append(origin.location)
    if not locations:
        return None
    locations.sort(key=_location_key)
    return locations[0]


def _location_key(location: IssueLocation) -> tuple[str, tuple[tuple[str, int], ...], str]:
    return (
        location.source or "",
        tuple((step.name, step.index) for step in location.path),
        location.attribute or "",
    )


def _name_choice(classes: list[NameClass]) -> NameClass:
    flattened: list[NameClass] = []
    for name_class in classes:
        if isinstance(name_class, NameChoice):
            flattened.extend(name_class.alternatives)
        else:
            flattened.append(name_class)
    unique = dict.fromkeys(flattened)
    ordered = sorted(unique, key=repr)
    if not ordered:
        return NameChoice(())
    if len(ordered) == 1:
        return ordered[0]
    return NameChoice(tuple(ordered))


def _name_class_origins(node: FullNameClass) -> OriginTable:
    origins = _node_origins(node)
    if isinstance(node, (FullNameChoice, FullNameExcept)):
        for alternative in node.alternatives:
            origins = origins.union(_name_class_origins(alternative))
    elif isinstance(node, (FullAnyName, FullNsName)) and node.except_class is not None:
        origins = origins.union(_name_class_origins(node.except_class))
    return origins


def _grouping_origins(children: tuple[FullNode, ...]) -> OriginTable:
    """Return the origins of every ``div`` spliced into a child list.

    ``div`` is transparent, so the pattern it produces belongs to the
    enclosing container; recording the div origins there keeps its authored
    location attached to the rewritten group or choice.
    """
    origins = OriginTable()
    for child in children:
        if isinstance(child, FullDiv):
            origins = origins.union(child.origins, _grouping_origins(child.children))
    return origins


def _spliced_patterns(children: tuple[FullNode, ...]) -> Iterator[FullNode]:
    """Splice transparent divs in authored order without recursive descent."""
    pending = list(reversed(children))
    while pending:
        child = pending.pop()
        if isinstance(child, FullDiv):
            pending.extend(reversed(child.children))
        else:
            yield child


def _full_name_class_mentions(node: FullNameClass, kinds: tuple[type[FullNameClass], ...]) -> bool:
    if isinstance(node, kinds):
        return True
    if isinstance(node, (FullNameChoice, FullNameExcept)):
        return any(
            _full_name_class_mentions(alternative, kinds) for alternative in node.alternatives
        )
    if isinstance(node, (FullAnyName, FullNsName)) and node.except_class is not None:
        return _full_name_class_mentions(node.except_class, kinds)
    return False


def _walk_name_classes(name_class: NameClass) -> Iterator[NameClass]:
    yield name_class
    if isinstance(name_class, NameChoice):
        for alternative in name_class.alternatives:
            yield from _walk_name_classes(alternative)
    elif isinstance(name_class, ExceptName):
        yield from _walk_name_classes(name_class.base)
        yield from _walk_name_classes(name_class.excluded)
