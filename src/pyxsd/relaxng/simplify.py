"""Ordered RELAX NG 1.0 simplification and local grammar rewriting.

The compiler follows the specification's simplification rules in order.  This
module performs the local prefix of that pipeline: annotation removal,
whitespace normalization, inherited datatype and namespace context, name-class
normalization, ``div`` splicing, child-count normalization, the
``mixed``/``optional``/``zeroOrMore`` rewrites, and the ``notAllowed``/``empty``
reductions.

Grammar composition is deliberately unresolved.  :class:`GrammarBundle`
retains every authored grammar scope, start, definition, include, reference,
and origin so that composition can resolve ``ref``, ``parentRef``,
``externalRef``, ``include``, ``combine``, reachability, and legal recursion in
the next stage.  Local placeholders are opaque pattern atoms until then.

Every simplified semantic pattern is interned in the caller's
:class:`~pyxsd.relaxng.patterns.PatternPool`; origin tables union beside the
interned key exactly as they do for authored patterns.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal, cast

from pyxsd.locations import IssueLocation
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    DatatypeContext,
    DatatypeLibraryRegistry,
    DatatypeParameter,
    DatatypeParameterError,
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
    Data,
    DataExcept,
    Element,
    Empty,
    List,
    NotAllowed,
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
)
from pyxsd.validation import ValidationReport

__all__ = [
    "BundleDefine",
    "BundleInclude",
    "BundleScope",
    "BundleStart",
    "GrammarBundle",
    "LocalGrammar",
    "Simplifier",
    "UnresolvedReference",
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


class UnresolvedReference(Ref):
    """An authored reference preserved until grammar composition.

    ``scope`` is the simplified scope containing the reference, or ``None``
    for a reference outside any grammar.  It is an opaque
    :class:`~pyxsd.relaxng.patterns.Ref` atom until composition binds the
    matching definition.
    """

    __slots__ = ("base", "href", "kind", "name", "ns_attribute", "scope")

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
    ) -> None:
        super().__init__()
        self.kind = kind
        self.name = name
        self.href = href
        self.base = base
        self.ns_attribute = ns_attribute
        self.scope = scope

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

    def simplify(self, schema: FullSchema) -> GrammarBundle:
        """Simplify *schema* into a local :class:`GrammarBundle`."""
        self._scopes = {}
        self._bundle_scopes = []
        start = self._root_pattern(schema.start)
        return GrammarBundle(start=start, scopes=tuple(self._bundle_scopes))

    # ------------------------------------------------------------------
    # Patterns

    def _root_pattern(self, node: FullPattern) -> Pattern:
        if isinstance(node, FullDiv):
            return self._div_pattern(node)
        return self._pattern(node)

    def _pattern(self, node: FullNode) -> Pattern:
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
        parts: list[Pattern] = []
        for child in children:
            if isinstance(child, FullDiv):
                parts.extend(self._pattern_children(child.children))
            else:
                parts.append(self._pattern(child))
        return parts

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
        scope = self._scopes.get(node.scope) if node.scope is not None else None
        reference = UnresolvedReference(
            node.kind,
            node.name,
            node.href,
            node.base,
            node.ns_attribute,
            scope,
        )
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
            type_name = type_name.strip()
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
                if child.name is None:
                    self._error(
                        "relaxng-invalid-parameter",
                        "a RELAX NG param element needs a name",
                        child.origins,
                    )
                    return self._intern(NotAllowed(), origins)
                try:
                    parameter = DatatypeParameter(child.name, child.value)
                except ValueError as exc:
                    self._error("relaxng-invalid-parameter", str(exc), child.origins)
                    return self._intern(NotAllowed(), origins)
                parameters.append(parameter)
                origins = origins.union(child.origins)
            elif isinstance(child, FullExcept):
                excepts.append(child)
                origins = origins.union(child.origins)
            else:
                self._unexpected_pattern(child)
        type_name = node.type_name.strip() if node.type_name is not None else ""
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
        return self._intern(LocalGrammar(scope), node.origins)

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
        return BundleDefine(node.name, _combine(node.combine), pattern, node.origins)

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
    return value.strip() if value is not None else None


def _node_origins(node: FullNode) -> OriginTable:
    return cast(OriginTable, getattr(node, "origins", OriginTable()))


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
