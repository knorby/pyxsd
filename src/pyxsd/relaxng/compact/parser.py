"""Recursive-descent reader for the RELAX NG compact syntax.

The reader follows the formal BNF of the OASIS RELAX NG Compact Syntax
specification (21 November 2002), Appendix A.1.  It parses directly into the
existing unresolved full-syntax tree instead of translating the whole schema
into an XML document: environments resolve compact names and datatype
prefixes while the abstract productions build
:class:`~pyxsd.relaxng.syntax.FullSchema` nodes.  Every node keeps the exact
compact source span of the construct that created it and a path in the
equivalent virtual RELAX NG element structure, so the composer's
element-boundary analysis and the existing diagnostics keep working unchanged.

Appendix A annotations are parsed into the existing opaque
:class:`~pyxsd.relaxng.syntax.Annotation` evidence: documentation shorthand,
initial bracket attributes and children, following ``>>`` elements placed as
foreign siblings of the containing construct, and grammar or include-content
annotation elements.  Recoverable named constraints become
``relaxng-compact-*`` findings with exact spans.  One reader instance owns all
of its state, so separate compilations never share an environment, token
stream, grammar scope, or budget observation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Literal, NoReturn, cast
from xml.etree import ElementTree as ET

from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import XML_NS, NamespaceContext, clark
from pyxsd.relaxng.compact.lexer import Token, TokenKind, lex_compact
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError
from pyxsd.relaxng.limits import BudgetCounter
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
from pyxsd.relaxng.resources import LoadedCompactResource, LoadedResource
from pyxsd.relaxng.syntax import (
    RELAX_NG_NAMESPACE,
    Annotation,
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
    FullValue,
    FullZeroOrMore,
    GrammarScope,
    ReferencePlaceholder,
    SchemaFinding,
    _is_datatype_library_uri,
)

__all__ = ["read_compact_syntax"]

#: The datatype library the predeclared ``xsd`` prefix selects.
_XSD_DATATYPES = "http://www.w3.org/2001/XMLSchema-datatypes"

#: The namespace for compact ``##`` documentation elements.
_COMPAT_ANNOTATIONS = "http://relaxng.org/ns/compatibility/annotations/1.0"

#: The reserved XML namespaces prefix namespace.
_XMLNS_NAMESPACE = "http://www.w3.org/2000/xmlns"

#: The combine value each compact assignment operator implies.
_ASSIGNMENT_COMBINES: dict[str, str | None] = {
    "=": None,
    "|=": "choice",
    "&=": "interleave",
}

#: Pattern operators and the container tag each one builds.
_OPERATORS = {
    ",": "group",
    "|": "choice",
    "&": "interleave",
}

_REPEAT_TAGS = {
    "?": "optional",
    "*": "zeroOrMore",
    "+": "oneOrMore",
}


def read_compact_syntax(
    resource: LoadedCompactResource,
    *,
    budget: BudgetCounter,
) -> FullSchema:
    """Read one admitted compact resource into the unresolved full syntax.

    Lexical and unrecoverable grammar failures raise
    :class:`~pyxsd.relaxng.errors.RelaxNGCompactSyntaxError` with the exact
    compact source evidence.  The returned schema's resource is a fresh view
    whose namespace context is the synthetic translation frame and whose
    origins are every construct read from this source.
    """
    return _Reader(resource, budget).read()


def _describe_token(token: Token) -> str:
    if token.kind is TokenKind.EOF:
        return "end of input"
    if token.kind is TokenKind.LITERAL_SEGMENT:
        return "a literal segment"
    if token.kind is TokenKind.DOCUMENTATION:
        return "a documentation comment"
    return repr(token.value)


@dataclass
class _Environment:
    """The file-local compact environment from Appendix A.1.

    Only the declaration preamble mutates one; the pattern body reads it.
    ``declared_prefixes`` and ``declared_datatypes`` separate authored
    declarations from the predeclared ``xml`` and ``xsd`` bindings so the
    duplicate-declaration constraint does not reject those bindings.
    """

    namespaces: dict[str, str | None]
    datatypes: dict[str, str]
    default_namespace: str | None
    default_declared: bool
    base_uri: str
    declared_prefixes: set[str] = field(default_factory=set)
    declared_datatypes: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _ForeignAnnotation:
    """One authored annotation attribute or element before it has a path.

    The virtual path of an annotation child is known only when its containing
    construct is built, so the opaque ElementTree element and the exact
    tokens are retained until then. ``attribute_tokens`` names the opaque
    element's own attributes and their authored name tokens.
    """

    kind: Literal["attribute", "element"]
    name: str
    namespace: str | None
    value: str | None
    token: Token
    element: ET.Element | None = None
    attribute_tokens: tuple[tuple[str, Token], ...] = ()


@dataclass(frozen=True)
class _Annotations:
    """One authored annotation fragment: attributes and element children."""

    attributes: tuple[_ForeignAnnotation, ...] = ()
    elements: tuple[_ForeignAnnotation, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.attributes or self.elements)


@dataclass
class _GrammarContent:
    """Grammar members in authored order plus bare annotation elements."""

    members: list[_Shape] = field(default_factory=list)
    annotations: list[_ForeignAnnotation] = field(default_factory=list)


@dataclass(frozen=True)
class _DataInfo:
    """The datatype attributes and children a ``data`` pattern will carry."""

    token: Token
    library: str
    type_name: str
    parameters: tuple[_Shape, ...] = ()


@dataclass(frozen=True)
class _Shape:
    """One construct awaiting path-dependent materialization.

    The virtual element path, and therefore every origin address, is known
    only once a parent allocates its child position.  A shape records the
    authored token and a builder that receives the final path and enclosing
    grammar scope.
    """

    tag: str
    token: Token
    build: Callable[[tuple[PathStep, ...], GrammarScope | None], FullNode]
    payload: object | None = None
    #: A bare ``data ... - pattern`` may only stand alone as an
    #: ``innerPattern``; mixing it with ``|``, ``&``, or ``,`` needs
    #: parentheses.  Parenthesized results clear this mark.
    inner_only: bool = False
    #: Following ``>>`` annotations, whose formal translation makes them
    #: foreign siblings of the enclosing construct, not of this node.
    following: tuple[_ForeignAnnotation, ...] = ()
    #: Whether this node came from a top-level ``particleChoice``,
    #: ``particleGroup``, or ``particleInterleave`` production.  A parent
    #: annotation applied to a plain single particle needs a ``group``
    #: wrapper, while one applied to a container attaches to that container.
    container: bool = False
    #: Whether this node came from ``repeatedPrimary``.  A parent annotation
    #: applies directly to a repetition and only wraps a plain primary.
    repeated: bool = False


class _Children:
    """Allocate same-tag sibling indices inside one virtual parent element."""

    __slots__ = ("_counts", "_path")

    def __init__(self, path: tuple[PathStep, ...]) -> None:
        self._path = path
        self._counts: dict[str, int] = {}

    def path_for(self, tag: str) -> tuple[PathStep, ...]:
        index = self._counts.get(tag, 0) + 1
        self._counts[tag] = index
        return (*self._path, PathStep(tag, index))


class _Reader:
    def __init__(self, resource: LoadedCompactResource, budget: BudgetCounter) -> None:
        self.resource = resource
        self.source_id = resource.canonical_id
        self.budget = budget
        self.tokens = lex_compact(resource.content, source_id=self.source_id, budget=budget)
        self.index = 0
        self.depth = 0
        self.origins: set[SourceOrigin] = set()
        self.findings: list[SchemaFinding] = []
        self.grammars: list[GrammarScope] = []
        self.environment: _Environment = _Environment({}, {}, None, False, self.source_id)
        self.frame: dict[str, str] = {}

    # -- entry point ------------------------------------------------------

    def read(self) -> FullSchema:
        self.environment = self._preamble()
        self.frame = self._namespace_frame(self.environment)
        token = self._peek()
        if token.kind is TokenKind.EOF or self._starts_top_level_grammar():
            shape = self._grammar_shape(top_level=True)
        else:
            shape = self._parse_pattern_shape()
            if shape.following:
                self._finding(
                    "relaxng-compact-single-element",
                    "a top-level pattern must translate to a single element",
                    shape.following[0].token,
                )
        trailing = self._peek()
        if trailing.kind is not TokenKind.EOF:
            self._fail(
                "unexpected token after the schema body",
                expected=("end of input",),
                at=trailing,
            )
        node = shape.build((PathStep(shape.tag, 1),), None)
        context = NamespaceContext()
        context.record(ET.Element(f"{{{RELAX_NG_NAMESPACE}}}grammar"), dict(self.frame))
        view = replace(
            self.resource,
            namespaces=context,
            origins=OriginTable(frozenset(self.origins)),
        )
        # FullSchema.resource becomes SchemaResource with reader dispatch.
        return FullSchema(
            resource=cast(LoadedResource, view),
            start=cast(FullPattern, node),
            grammars=tuple(self.grammars),
            findings=tuple(self.findings),
        )

    def _starts_top_level_grammar(self) -> bool:
        index = self._annotation_lookahead_index()
        token = self.tokens[index]
        if token.kind is TokenKind.EOF:
            return True
        if token.kind is TokenKind.KEYWORD and token.value in {"start", "div", "include"}:
            return True
        following = self.tokens[min(index + 1, len(self.tokens) - 1)]
        if (
            token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}
            and following.kind is TokenKind.SYMBOL
            and following.value in _ASSIGNMENT_COMBINES
        ):
            return True
        return self._is_annotation_member_start(token, following)

    def _annotation_lookahead_index(self) -> int:
        """The token index after leading documentation and one bracket group."""
        index = self.index
        while self.tokens[index].kind is TokenKind.DOCUMENTATION:
            index += 1
        if self.tokens[index].kind is TokenKind.SYMBOL and self.tokens[index].value == "[":
            depth = 0
            while index < len(self.tokens):
                token = self.tokens[index]
                if token.kind is TokenKind.SYMBOL and token.value == "[":
                    depth += 1
                elif token.kind is TokenKind.SYMBOL and token.value == "]":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
        return min(index, len(self.tokens) - 1)

    def _is_annotation_member_start(self, token: Token, following: Token) -> bool:
        """Whether *token* begins ``annotationElementNotKeyword``."""
        if token.kind not in {
            TokenKind.IDENTIFIER,
            TokenKind.QUOTED_IDENTIFIER,
            TokenKind.CNAME,
        }:
            return False
        return following.kind is TokenKind.SYMBOL and following.value == "["

    # -- declarations -----------------------------------------------------

    def _preamble(self) -> _Environment:
        environment = _Environment(
            namespaces={"xml": XML_NS},
            datatypes={"xsd": _XSD_DATATYPES},
            default_namespace=None,
            default_declared=False,
            base_uri=self.source_id,
        )
        while True:
            token = self._peek()
            if token.kind is not TokenKind.KEYWORD:
                return environment
            if token.value == "namespace":
                self._namespace_declaration(environment)
            elif token.value == "default":
                self._default_namespace_declaration(environment)
            elif token.value == "datatypes":
                self._datatypes_declaration(environment)
            else:
                return environment

    def _namespace_declaration(self, environment: _Environment) -> None:
        self._advance()
        prefix = self._identifier_or_keyword()
        self._expect_symbol("=")
        namespace, namespace_token = self._namespace_uri()
        self._check_namespace_prefix(prefix, namespace)
        self._check_namespace_uri(prefix.value, namespace, namespace_token)
        if prefix.value == "xmlns":
            return
        if prefix.value in environment.declared_prefixes:
            self._duplicate_declaration(prefix, f"the namespace prefix {prefix.value!r}")
        environment.declared_prefixes.add(prefix.value)
        environment.namespaces.setdefault(prefix.value, namespace)

    def _default_namespace_declaration(self, environment: _Environment) -> None:
        self._advance()
        keyword = self._expect_keyword("namespace")
        prefix: Token | None = None
        token = self._peek()
        if token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER, TokenKind.KEYWORD}:
            prefix = self._identifier_or_keyword()
        self._expect_symbol("=")
        namespace, namespace_token = self._namespace_uri()
        if prefix is not None:
            self._check_namespace_prefix(prefix, namespace)
            self._check_namespace_uri(prefix.value, namespace, namespace_token)
        else:
            self._check_namespace_uri(None, namespace, namespace_token)
        if environment.default_declared:
            self._duplicate_declaration(keyword, "the default namespace")
        if prefix is not None:
            if prefix.value == "xmlns":
                return
            if prefix.value in environment.declared_prefixes:
                self._duplicate_declaration(prefix, f"the namespace prefix {prefix.value!r}")
            environment.declared_prefixes.add(prefix.value)
        if not environment.default_declared:
            environment.default_namespace = namespace
            environment.default_declared = True
        if prefix is not None:
            environment.namespaces.setdefault(prefix.value, namespace)

    def _datatypes_declaration(self, environment: _Environment) -> None:
        self._advance()
        prefix = self._identifier_or_keyword()
        self._expect_symbol("=")
        value, value_token = self._parse_literal()
        if prefix.value == "xsd" and value != _XSD_DATATYPES:
            self._finding(
                "relaxng-compact-xsd-prefix",
                "the xsd datatype prefix must name the XML Schema datatype library",
                prefix,
            )
        if not _is_datatype_library_uri(value):
            self._finding(
                "relaxng-compact-datatypes-uri",
                f"the datatype library {value!r} is not an absolute URI reference",
                value_token,
            )
        if prefix.value in environment.declared_datatypes:
            self._duplicate_declaration(prefix, f"the datatype prefix {prefix.value!r}")
        environment.declared_datatypes.add(prefix.value)
        environment.datatypes.setdefault(prefix.value, value)

    def _check_namespace_prefix(self, prefix: Token, namespace: str | None) -> None:
        if prefix.value == "xmlns":
            self._finding(
                "relaxng-compact-invalid-prefix",
                "the xmlns prefix may not be declared",
                prefix,
            )
        if prefix.value == "xml" and namespace != XML_NS:
            self._finding(
                "relaxng-compact-xml-prefix",
                f"the xml prefix must be bound to {XML_NS}",
                prefix,
            )

    def _check_namespace_uri(self, prefix: str | None, namespace: str | None, token: Token) -> None:
        if namespace == XML_NS and prefix != "xml":
            self._finding(
                "relaxng-compact-xml-namespace-uri",
                "the XML namespace URI may only be bound to the xml prefix",
                token,
            )

    def _duplicate_declaration(self, token: Token, description: str) -> None:
        self._finding(
            "relaxng-compact-duplicate-declaration",
            f"{description} is declared more than once",
            token,
        )

    def _namespace_uri(self) -> tuple[str | None, Token]:
        token = self._peek()
        if token.kind is TokenKind.KEYWORD and token.value == "inherit":
            self._advance()
            return None, token
        value, first = self._parse_literal()
        return value, first

    def _namespace_frame(self, environment: _Environment) -> dict[str, str]:
        frame = {"": RELAX_NG_NAMESPACE, "xml": XML_NS}
        for prefix, uri in environment.namespaces.items():
            if not uri:
                continue
            frame[prefix] = uri
        return frame

    def _inherited_value(self, value: str | None) -> str | None:
        if value is None:
            return self.resource.inherited_namespace or None
        return value or None

    def _namespace_binding(self, token: Token, prefix: str) -> str | None:
        if prefix not in self.environment.namespaces:
            self._fail(
                f"the namespace prefix {prefix!r} is not bound in scope",
                expected=("a declared namespace prefix",),
                at=token,
            )
        return self.environment.namespaces[prefix]

    def _datatype_binding(self, token: Token, prefix: str) -> str:
        library = self.environment.datatypes.get(prefix)
        if library is None:
            self._fail(
                f"the datatype prefix {prefix!r} is not bound in scope",
                expected=("a declared datatype prefix",),
                at=token,
            )
        return library

    # -- grammar content --------------------------------------------------

    def _grammar_shape(self, *, top_level: bool, initial: _Annotations | None = None) -> _Shape:
        initial = initial if initial is not None else _Annotations()
        if top_level:
            token = self._peek()
            content = self._grammar_members(end=None, allow_include=True)
            if content.members:
                token = content.members[0].token
            elif content.annotations:
                token = content.annotations[0].token
        else:
            token = self._expect_keyword("grammar")
            self._expect_symbol("{")
            with self._nested():
                content = self._grammar_members(end="}", allow_include=True)
            self._expect_symbol("}")

        def build(path: tuple[PathStep, ...], parent_scope: GrammarScope | None) -> FullNode:
            origins = self._table(path, token)
            annotations = self._annotation_values(
                _Annotations(
                    attributes=initial.attributes,
                    elements=initial.elements + tuple(content.annotations),
                ),
                path,
            )
            scope = GrammarScope(parent=parent_scope, origins=origins, annotations=annotations)
            self.grammars.append(scope)
            children = _Children(path)
            for member in content.members:
                scope._append(member.build(children.path_for(member.tag), scope))
            return FullGrammar(scope, origins, annotations)

        return _Shape("grammar", token, build)

    def _grammar_members(self, *, end: str | None, allow_include: bool) -> _GrammarContent:
        content = _GrammarContent()
        while not self._at_grammar_end(end):
            if self._annotations_only_to_end(end):
                content.annotations.extend(self._annotations().elements)
                break
            member = self._grammar_member(allow_include=allow_include)
            if isinstance(member, _Shape):
                content.members.append(member)
            else:
                content.annotations.append(member)
        return content

    def _annotations_only_to_end(self, end: str | None) -> bool:
        token = self._peek()
        if token.kind is not TokenKind.DOCUMENTATION and not (
            token.kind is TokenKind.SYMBOL and token.value == "["
        ):
            return False
        index = self._annotation_lookahead_index()
        trailing = self.tokens[index]
        if end is None:
            return trailing.kind is TokenKind.EOF
        return trailing.kind is TokenKind.SYMBOL and trailing.value == end

    def _at_grammar_end(self, end: str | None) -> bool:
        token = self._peek()
        if end is None:
            return token.kind is TokenKind.EOF
        return token.kind is TokenKind.SYMBOL and token.value == end

    def _grammar_member(self, *, allow_include: bool) -> _Shape | _ForeignAnnotation:
        annotations = self._annotations()
        token = self._peek()
        following = self._peek(1)
        if token.kind is TokenKind.KEYWORD:
            if token.value == "start":
                return self._annotate(self._start_shape(), annotations)
            if token.value == "div":
                return self._annotate(self._div_shape(allow_include=allow_include), annotations)
            if token.value == "include":
                if not allow_include:
                    self._fail(
                        "an include body cannot contain another include",
                        expected=("'start'", "'define'", "'div'"),
                        at=token,
                    )
                return self._annotate(self._include_shape(), annotations)
        if self._is_annotation_member_start(token, following):
            if annotations:
                self._fail(
                    "an annotation element cannot have initial annotations",
                    expected=("a grammar component",),
                    at=token,
                )
            return self._annotation_element(initial=True)
        if token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
            return self._annotate(self._define_shape(), annotations)
        self._fail(
            "a grammar member must be a start, define, div, or include",
            expected=("'start'", "a definition", "'div'", "'include'"),
            at=token,
        )

    def _start_shape(self) -> _Shape:
        token = self._expect_keyword("start")
        combine = self._assignment_operator()
        pattern = self._parse_pattern_shape()

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            body = pattern.build(_Children(path).path_for(pattern.tag), scope)
            annotations = self._annotation_values(_Annotations(elements=pattern.following), path)
            return FullStart(combine, (body,), self._table(path, token), annotations)

        return _Shape("start", token, build)

    def _define_shape(self) -> _Shape:
        name = self._identifier()
        combine = self._assignment_operator()
        pattern = self._parse_pattern_shape()

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            body = pattern.build(_Children(path).path_for(pattern.tag), scope)
            origins = self._table(path, name)
            annotations = self._annotation_values(_Annotations(elements=pattern.following), path)
            return FullDefine(name.value, combine, (body,), origins, annotations)

        return _Shape("define", name, build)

    def _div_shape(self, *, allow_include: bool) -> _Shape:
        token = self._expect_keyword("div")
        self._expect_symbol("{")
        with self._nested():
            content = self._grammar_members(end="}", allow_include=allow_include)
        self._expect_symbol("}")

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            children = _Children(path)
            built = tuple(
                member.build(children.path_for(member.tag), scope) for member in content.members
            )
            annotations = self._annotation_values(
                _Annotations(elements=tuple(content.annotations)), path
            )
            return FullDiv(built, self._table(path, token), annotations)

        return _Shape("div", token, build)

    def _include_shape(self) -> _Shape:
        token = self._expect_keyword("include")
        href, _ = self._parse_literal()
        namespace = self._optional_inherit()
        content = _GrammarContent()
        if self._peek().kind is TokenKind.SYMBOL and self._peek().value == "{":
            self._advance()
            with self._nested():
                content = self._grammar_members(end="}", allow_include=False)
            self._expect_symbol("}")

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            children = _Children(path)
            built = tuple(
                member.build(children.path_for(member.tag), scope) for member in content.members
            )
            annotations = self._annotation_values(
                _Annotations(elements=tuple(content.annotations)), path
            )
            return ReferencePlaceholder(
                kind="include",
                name=None,
                href=href,
                base=self.environment.base_uri,
                ns_attribute=namespace,
                scope=scope,
                children=built,
                origins=self._table(path, token),
                annotations=annotations,
            )

        return _Shape("include", token, build)

    def _assignment_operator(self) -> str | None:
        token = self._peek()
        if token.kind is TokenKind.SYMBOL and token.value in _ASSIGNMENT_COMBINES:
            self._advance()
            return _ASSIGNMENT_COMBINES[token.value]
        self._fail(
            "expected an assignment operator",
            expected=("'='", "'|='", "'&='"),
            at=token,
        )

    # -- patterns ---------------------------------------------------------

    def _parse_pattern_shape(self) -> _Shape:
        first = self._parse_particle_shape()
        token = self._peek()
        if token.kind is not TokenKind.SYMBOL or token.value not in _OPERATORS:
            return first
        if first.inner_only:
            self._fail(
                "the - operator may not be mixed with , | or & without parentheses",
                expected=("a parenthesized pattern",),
                at=token,
            )
        operator = token.value
        operator_token = token
        parts = [first]
        self._advance()
        parts.append(self._sequence_operand(operator_token))
        while True:
            token = self._peek()
            if token.kind is TokenKind.SYMBOL and token.value == operator:
                self._advance()
                parts.append(self._sequence_operand(token))
                continue
            if token.kind is TokenKind.SYMBOL and token.value in _OPERATORS:
                self._fail(
                    "the patterns , | and & may not be mixed without parentheses",
                    expected=(repr(operator),),
                    at=token,
                )
            return self._pattern_container_shape(_OPERATORS[operator], operator_token, parts)

    def _sequence_operand(self, operator_token: Token) -> _Shape:
        operand = self._parse_particle_shape()
        if operand.inner_only:
            self._fail(
                "the - operator may not be mixed with , | or & without parentheses",
                expected=("a parenthesized pattern",),
                at=operator_token,
            )
        return operand

    def _parse_particle_shape(self) -> _Shape:
        shape = self._parse_lead_primary_shape()
        token = self._peek()
        if token.kind is TokenKind.SYMBOL and token.value == "-":
            info = shape.payload
            if shape.tag != "data" or not isinstance(info, _DataInfo):
                self._fail(
                    "only a datatype pattern can have an exception",
                    expected=("a datatype pattern",),
                    at=token,
                )
            self._advance()
            with self._nested():
                excluded = self._parse_lead_primary_shape()
            shape = replace(
                self._data_except_shape(shape, token, excluded),
                inner_only=True,
                following=shape.following + self._follow_annotations(),
            )
            trailing = self._peek()
            if trailing.kind is TokenKind.SYMBOL and trailing.value in _REPEAT_TAGS:
                self._fail(
                    "an exception pattern cannot be repeated",
                    expected=("a pattern",),
                    at=trailing,
                )
            return shape
        shape = replace(shape, following=shape.following + self._follow_annotations())
        token = self._peek()
        if token.kind is TokenKind.SYMBOL and token.value in _REPEAT_TAGS:
            self._advance()
            shape = self._repeat_shape(shape, token)
            trailing = self._peek()
            if trailing.kind is TokenKind.SYMBOL and trailing.value in _REPEAT_TAGS:
                self._fail(
                    "a pattern may have only one repetition operator",
                    expected=("a pattern",),
                    at=trailing,
                )
            shape = replace(shape, following=self._follow_annotations())
        return shape

    def _parse_lead_primary_shape(self) -> _Shape:
        annotations = self._annotations()
        token = self._peek()
        if token.kind is TokenKind.SYMBOL and token.value == "(":
            self._advance()
            with self._nested():
                shape = self._parse_pattern_shape()
            self._expect_symbol(")")
            shape = replace(shape, inner_only=False)
            if annotations:
                if shape.container or shape.repeated:
                    shape = self._annotate(shape, annotations)
                else:
                    shape = self._annotate(self._group_wrapper_shape(shape, token), annotations)
            return replace(shape, container=False, repeated=False)
        if token.kind is TokenKind.KEYWORD:
            if token.value == "grammar":
                return self._grammar_shape(top_level=False, initial=annotations)
            shape = self._keyword_primary_shape(token)
        elif token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
            self._advance()
            shape = self._reference_shape(token)
        elif token.kind is TokenKind.CNAME:
            shape = self._datatype_shape()
        elif token.kind is TokenKind.LITERAL_SEGMENT:
            lexical, first = self._parse_literal()
            shape = self._value_shape(first, "", None, lexical)
        else:
            self._fail(
                "expected a pattern",
                expected=("a pattern",),
                at=token,
            )
        return self._annotate(shape, annotations)

    def _keyword_primary_shape(self, token: Token) -> _Shape:
        if token.value == "element":
            return self._element_shape(attribute=False)
        if token.value == "attribute":
            return self._element_shape(attribute=True)
        if token.value == "list":
            return self._braced_container_shape("list", FullList)
        if token.value == "mixed":
            return self._braced_container_shape("mixed", FullMixed)
        if token.value == "empty":
            self._advance()
            return self._leaf_shape("empty", token, FullEmpty)
        if token.value == "notAllowed":
            self._advance()
            return self._leaf_shape("notAllowed", token, FullNotAllowed)
        if token.value == "text":
            self._advance()
            return self._leaf_shape("text", token, FullText)
        if token.value == "parent":
            return self._parent_reference_shape()
        if token.value == "external":
            return self._external_shape()
        if token.value in {"string", "token"}:
            return self._datatype_shape()
        self._fail(
            "this keyword cannot begin a pattern",
            expected=("a pattern",),
            at=token,
        )

    def _element_shape(self, *, attribute: bool) -> _Shape:
        token = self._advance()
        name = self._name_class_shape(is_element=not attribute)
        self._expect_symbol("{")
        with self._nested():
            body = self._parse_pattern_shape()
        self._expect_symbol("}")

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            children = _Children(path)
            name_class = cast(FullNameClass, name.build(children.path_for(name.tag), scope))
            content = body.build(children.path_for(body.tag), scope)
            origins = self._table(path, token)
            annotations = self._annotation_values(
                _Annotations(elements=name.following + body.following), path
            )
            if attribute:
                return FullAttribute(name_class, (content,), origins, annotations)
            return FullElement(name_class, (content,), origins, annotations)

        return _Shape("attribute" if attribute else "element", token, build)

    def _braced_container_shape(
        self, tag: str, constructor: type[FullList] | type[FullMixed]
    ) -> _Shape:
        token = self._expect_keyword(tag)
        self._expect_symbol("{")
        with self._nested():
            body = self._parse_pattern_shape()
        self._expect_symbol("}")

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            content = body.build(_Children(path).path_for(body.tag), scope)
            annotations = self._annotation_values(_Annotations(elements=body.following), path)
            return constructor((content,), self._table(path, token), annotations)

        return _Shape(tag, token, build)

    def _leaf_shape(
        self,
        tag: str,
        token: Token,
        constructor: type[FullEmpty] | type[FullNotAllowed] | type[FullText],
    ) -> _Shape:
        def build(path: tuple[PathStep, ...], _scope: GrammarScope | None) -> FullNode:
            return constructor((), self._table(path, token))

        return _Shape(tag, token, build)

    def _reference_shape(self, token: Token) -> _Shape:
        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            return ReferencePlaceholder(
                kind="ref",
                name=token.value,
                href=None,
                base=None,
                ns_attribute=None,
                scope=scope,
                children=(),
                origins=self._table(path, token),
                annotations=(),
            )

        return _Shape("ref", token, build)

    def _parent_reference_shape(self) -> _Shape:
        token = self._expect_keyword("parent")
        name = self._identifier()

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            return ReferencePlaceholder(
                kind="parentRef",
                name=name.value,
                href=None,
                base=None,
                ns_attribute=None,
                scope=scope,
                children=(),
                origins=self._table(path, token),
                annotations=(),
            )

        return _Shape("parentRef", token, build)

    def _external_shape(self) -> _Shape:
        token = self._expect_keyword("external")
        href, _ = self._parse_literal()
        namespace = self._optional_inherit()

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            return ReferencePlaceholder(
                kind="externalRef",
                name=None,
                href=href,
                base=self.environment.base_uri,
                ns_attribute=namespace,
                scope=scope,
                children=(),
                origins=self._table(path, token),
                annotations=(),
            )

        return _Shape("externalRef", token, build)

    def _optional_inherit(self) -> str | None:
        token = self._peek()
        if token.kind is TokenKind.KEYWORD and token.value == "inherit":
            self._advance()
            self._expect_symbol("=")
            prefix = self._identifier_or_keyword()
            return self._namespace_binding(prefix, prefix.value)
        return self.environment.default_namespace

    def _pattern_container_shape(self, tag: str, token: Token, parts: list[_Shape]) -> _Shape:
        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            children = _Children(path)
            built = tuple(part.build(children.path_for(part.tag), scope) for part in parts)
            origins = self._table(path, token)
            annotations = self._annotation_values(
                _Annotations(elements=tuple(f for part in parts for f in part.following)),
                path,
            )
            if tag == "group":
                return FullGroup(built, origins, annotations)
            if tag == "choice":
                return FullChoice(built, origins, annotations)
            return FullInterleave(built, origins, annotations)

        return _Shape(tag, token, build, container=True)

    def _group_wrapper_shape(self, shape: _Shape, token: Token) -> _Shape:
        """Wrap one particle in the ``group`` the formal annotation needs."""

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            content = shape.build(_Children(path).path_for(shape.tag), scope)
            annotations = self._annotation_values(_Annotations(elements=shape.following), path)
            return FullGroup((content,), self._table(path, token), annotations)

        return _Shape("group", token, build)

    def _repeat_shape(self, shape: _Shape, token: Token) -> _Shape:
        tag = _REPEAT_TAGS[token.value]

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            content = shape.build(_Children(path).path_for(shape.tag), scope)
            origins = self._table(path, token)
            annotations = self._annotation_values(_Annotations(elements=shape.following), path)
            if tag == "optional":
                return FullOptional((content,), origins, annotations)
            if tag == "zeroOrMore":
                return FullZeroOrMore((content,), origins, annotations)
            return FullOneOrMore((content,), origins, annotations)

        return _Shape(tag, token, build, repeated=True)

    def _value_shape(
        self,
        token: Token,
        library: str,
        type_name: str | None,
        lexical: str,
    ) -> _Shape:
        def build(path: tuple[PathStep, ...], _scope: GrammarScope | None) -> FullNode:
            return FullValue(
                datatype_library=library,
                type_name=type_name,
                lexical=lexical,
                base_uri=self.environment.base_uri,
                namespace_uri=None,
                namespaces=self._namespaces(),
                children=(),
                origins=self._table(path, token),
                annotations=(),
            )

        return _Shape("value", token, build)

    # -- datatypes --------------------------------------------------------

    def _datatype_shape(self) -> _Shape:
        token = self._peek()
        if token.kind is TokenKind.KEYWORD:
            self._advance()
            library, type_name = "", token.value
        else:
            self._advance()
            prefix, local = token.value.split(":", 1)
            library, type_name = self._datatype_binding(token, prefix), local
        if self._peek().kind is TokenKind.LITERAL_SEGMENT:
            lexical, _ = self._parse_literal()
            return self._value_shape(token, library, type_name, lexical)
        parameters: tuple[_Shape, ...] = ()
        if self._peek().kind is TokenKind.SYMBOL and self._peek().value == "{":
            parameters = self._parse_parameters()
        return self._data_shape(_DataInfo(token, library, type_name, parameters))

    def _data_shape(self, info: _DataInfo) -> _Shape:
        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            children = _Children(path)
            built: list[FullNode] = [
                parameter.build(children.path_for(parameter.tag), scope)
                for parameter in info.parameters
            ]
            return FullData(
                datatype_library=info.library,
                type_name=info.type_name,
                base_uri=self.environment.base_uri,
                namespaces=self._namespaces(),
                children=tuple(built),
                origins=self._table(path, info.token),
                annotations=(),
            )

        return _Shape("data", info.token, build, payload=info)

    def _data_except_shape(self, shape: _Shape, token: Token, excluded: _Shape) -> _Shape:
        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            data = cast(FullData, shape.build(path, scope))
            except_path = (*path, PathStep("except", 1))
            excluded_node = excluded.build((*except_path, PathStep(excluded.tag, 1)), scope)
            annotations = self._annotation_values(
                _Annotations(elements=excluded.following), except_path
            )
            exception = FullExcept((excluded_node,), self._table(except_path, token), annotations)
            return replace(data, children=(*data.children, exception))

        return _Shape("data", shape.token, build, payload=shape.payload)

    def _parse_parameters(self) -> tuple[_Shape, ...]:
        self._expect_symbol("{")
        parameters: list[_Shape] = []
        with self._nested():
            while not (self._peek().kind is TokenKind.SYMBOL and self._peek().value == "}"):
                parameters.append(self._parameter_shape())
        self._expect_symbol("}")
        return tuple(parameters)

    def _parameter_shape(self) -> _Shape:
        annotations = self._annotations()
        name = self._identifier_or_keyword()
        self._expect_symbol("=")
        value, _ = self._parse_literal()

        def build(path: tuple[PathStep, ...], _scope: GrammarScope | None) -> FullNode:
            return FullParam(name.value, value, (), self._table(path, name), ())

        return self._annotate(_Shape("param", name, build), annotations)

    # -- name classes -----------------------------------------------------

    def _name_class_shape(self, *, is_element: bool) -> _Shape:
        first = self._parse_lead_simple_name_class(is_element=is_element)
        token = self._peek()
        if token.kind is TokenKind.SYMBOL and token.value == "-":
            if first.tag not in {"nsName", "anyName"}:
                self._fail(
                    "only an nsName or anyName name class can have an exception",
                    expected=("a name class without an exception",),
                    at=token,
                )
            self._advance()
            with self._nested():
                excluded = self._parse_lead_simple_name_class(is_element=is_element)
            shape = self._name_except_shape(first, token, excluded)
            trailing = self._peek()
            if trailing.kind is TokenKind.SYMBOL and trailing.value == "|":
                self._fail(
                    "the name-class | and - operators may not be mixed without parentheses",
                    expected=("a parenthesized name class",),
                    at=trailing,
                )
            return replace(shape, following=self._follow_annotations())
        if token.kind is TokenKind.SYMBOL and token.value == "|":
            alternatives = [self._with_following(first)]
            while self._peek().kind is TokenKind.SYMBOL and self._peek().value == "|":
                self._advance()
                alternative = self._parse_lead_simple_name_class(is_element=is_element)
                alternatives.append(self._with_following(alternative))
            trailing = self._peek()
            if trailing.kind is TokenKind.SYMBOL and trailing.value == "-":
                self._fail(
                    "the name-class | and - operators may not be mixed without parentheses",
                    expected=("a parenthesized name class",),
                    at=trailing,
                )
            return self._name_choice_shape(token, alternatives)
        return self._with_following(first)

    def _with_following(self, shape: _Shape) -> _Shape:
        return replace(shape, following=shape.following + self._follow_annotations())

    def _parse_lead_simple_name_class(self, *, is_element: bool) -> _Shape:
        annotations = self._annotations()
        token = self._peek()
        if token.kind is TokenKind.SYMBOL and token.value == "(":
            self._advance()
            with self._nested():
                shape = self._name_class_shape(is_element=is_element)
            self._expect_symbol(")")
            if annotations:
                if shape.container:
                    shape = self._annotate(shape, annotations)
                else:
                    shape = self._annotate(self._name_choice_wrapper(shape, token), annotations)
            return replace(shape, container=False)
        if token.kind is TokenKind.SYMBOL and token.value == "*":
            self._advance()
            shape = self._wildcard_shape("anyName", token, None)
        elif token.kind is TokenKind.NS_NAME:
            self._advance()
            uri = self._inherited_value(self._namespace_binding(token, token.value))
            shape = self._wildcard_shape("nsName", token, uri)
        elif token.kind is TokenKind.CNAME:
            self._advance()
            prefix, local = token.value.split(":", 1)
            uri = self._inherited_value(self._namespace_binding(token, prefix))
            shape = self._name_shape(token, token.value, local, uri)
        elif token.kind in {
            TokenKind.IDENTIFIER,
            TokenKind.QUOTED_IDENTIFIER,
            TokenKind.KEYWORD,
        }:
            self._advance()
            uri = self._inherited_value(self.environment.default_namespace) if is_element else None
            shape = self._name_shape(token, token.value, token.value, uri)
        else:
            self._fail(
                "expected a name, nsName, anyName, or parenthesized name class",
                expected=("a name class",),
                at=token,
            )
        return self._annotate(shape, annotations)

    def _name_shape(self, token: Token, lexical: str, local: str, uri: str | None) -> _Shape:
        def build(path: tuple[PathStep, ...], _scope: GrammarScope | None) -> FullNode:
            return FullName(lexical, uri, local, self._table(path, token), ())

        return _Shape("name", token, build)

    def _wildcard_shape(self, tag: str, token: Token, uri: str | None) -> _Shape:
        def build(path: tuple[PathStep, ...], _scope: GrammarScope | None) -> FullNode:
            origins = self._table(path, token)
            if tag == "anyName":
                return FullAnyName(None, origins, ())
            return FullNsName(uri, None, origins, ())

        return _Shape(tag, token, build, payload=uri)

    def _name_choice_shape(self, token: Token, alternatives: list[_Shape]) -> _Shape:
        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            children = _Children(path)
            built = tuple(
                cast(
                    FullNameClass,
                    alternative.build(children.path_for(alternative.tag), scope),
                )
                for alternative in alternatives
            )
            annotations = self._annotation_values(
                _Annotations(
                    elements=tuple(f for alternative in alternatives for f in alternative.following)
                ),
                path,
            )
            return FullNameChoice(built, self._table(path, token), annotations)

        return _Shape("choice", token, build, container=True)

    def _name_choice_wrapper(self, shape: _Shape, token: Token) -> _Shape:
        """Wrap one name class in the ``choice`` a parent annotation needs."""

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            alternative = cast(
                FullNameClass,
                shape.build(_Children(path).path_for(shape.tag), scope),
            )
            annotations = self._annotation_values(_Annotations(elements=shape.following), path)
            return FullNameChoice((alternative,), self._table(path, token), annotations)

        return _Shape("choice", token, build)

    def _name_except_shape(self, wildcard: _Shape, token: Token, excluded: _Shape) -> _Shape:
        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            except_path = (*path, PathStep("except", 1))
            alternative = cast(
                FullNameClass,
                excluded.build((*except_path, PathStep(excluded.tag, 1)), scope),
            )
            annotations = self._annotation_values(
                _Annotations(elements=excluded.following), except_path
            )
            exception = FullNameExcept((alternative,), self._table(except_path, token), annotations)
            wildcard_node = cast(FullAnyName | FullNsName, wildcard.build(path, scope))
            return replace(wildcard_node, except_class=exception)

        return _Shape(wildcard.tag, wildcard.token, build)

    # -- tokens -----------------------------------------------------------

    def _peek(self, offset: int = 0) -> Token:
        index = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[index]

    def _advance(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _identifier(self) -> Token:
        token = self._peek()
        if token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
            self._advance()
            return token
        self._fail("expected an identifier", expected=("an identifier",), at=token)

    def _identifier_or_keyword(self) -> Token:
        token = self._peek()
        if token.kind in {
            TokenKind.IDENTIFIER,
            TokenKind.QUOTED_IDENTIFIER,
            TokenKind.KEYWORD,
        }:
            self._advance()
            return token
        self._fail(
            "expected an identifier or keyword",
            expected=("an identifier",),
            at=token,
        )

    def _expect_keyword(self, value: str) -> Token:
        token = self._peek()
        if token.kind is TokenKind.KEYWORD and token.value == value:
            self._advance()
            return token
        self._fail(f"expected {value!r}", expected=(repr(value),), at=token)

    def _expect_symbol(self, value: str) -> Token:
        token = self._peek()
        if token.kind is TokenKind.SYMBOL and token.value == value:
            self._advance()
            return token
        self._fail(f"expected {value!r}", expected=(repr(value),), at=token)

    def _parse_literal(self) -> tuple[str, Token]:
        first = self._peek()
        if first.kind is not TokenKind.LITERAL_SEGMENT:
            self._fail(
                "expected a literal",
                expected=("a literal segment",),
                at=first,
            )
        parts = [first.value]
        self._advance()
        while True:
            separator = self._peek()
            if separator.kind is not TokenKind.SYMBOL or separator.value != "~":
                return "".join(parts), first
            self._advance()
            segment = self._peek()
            if segment.kind is not TokenKind.LITERAL_SEGMENT:
                self._fail(
                    "expected a literal segment after '~'",
                    expected=("a literal segment",),
                    at=segment,
                )
            parts.append(segment.value)
            self._advance()

    def _annotate(self, shape: _Shape, annotations: _Annotations) -> _Shape:
        """Attach one initial annotation, with its attributes and children."""
        if not annotations:
            return shape
        build = shape.build

        def annotated(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            node = cast(Any, build(path, scope))
            values = self._annotation_values(annotations, path)
            return cast(FullNode, replace(node, annotations=(*values, *node.annotations)))

        return replace(shape, build=annotated)

    def _annotation_values(
        self, annotations: _Annotations, path: tuple[PathStep, ...]
    ) -> tuple[Annotation, ...]:
        values: list[Annotation] = []
        counts: dict[str, int] = {}
        for fragment in annotations.elements:
            element = fragment.element
            assert element is not None
            index = counts.get(element.tag, 0) + 1
            counts[element.tag] = index
            child_path = (*path, PathStep(element.tag, index))
            origins = {
                SourceOrigin(
                    self.source_id,
                    IssueLocation(self.source_id, child_path, span=fragment.token.span),
                )
            }
            for attribute_name, attribute_token in fragment.attribute_tokens:
                origins.add(
                    SourceOrigin(
                        self.source_id,
                        IssueLocation(
                            self.source_id,
                            child_path,
                            attribute=attribute_name,
                            span=attribute_token.span,
                        ),
                    )
                )
            table = OriginTable(frozenset(origins))
            self.origins.update(origins)
            values.append(
                Annotation(
                    "element",
                    fragment.name,
                    fragment.namespace,
                    None,
                    table,
                    element,
                )
            )
        for fragment in annotations.attributes:
            attribute_name = clark(fragment.namespace, fragment.name)
            origin = SourceOrigin(
                self.source_id,
                IssueLocation(
                    self.source_id,
                    path,
                    attribute=attribute_name,
                    span=fragment.token.span,
                ),
            )
            self.origins.add(origin)
            values.append(
                Annotation(
                    "attribute",
                    fragment.name,
                    fragment.namespace,
                    fragment.value,
                    OriginTable(frozenset({origin})),
                )
            )
        return tuple(values)

    def _follow_annotations(self) -> tuple[_ForeignAnnotation, ...]:
        fragments: list[_ForeignAnnotation] = []
        while self._peek().kind is TokenKind.SYMBOL and self._peek().value == ">>":
            self._advance()
            token = self._peek()
            if token.kind not in {
                TokenKind.IDENTIFIER,
                TokenKind.QUOTED_IDENTIFIER,
                TokenKind.KEYWORD,
                TokenKind.CNAME,
            }:
                self._fail(
                    "expected an annotation element after '>>'",
                    expected=("an annotation element",),
                    at=token,
                )
            fragments.append(self._annotation_element(initial=True))
        return tuple(fragments)

    def _annotations(self) -> _Annotations:
        elements: list[_ForeignAnnotation] = []
        while self._peek().kind is TokenKind.DOCUMENTATION:
            elements.append(self._documentation_annotation(self._advance()))
        attributes: list[_ForeignAnnotation] = []
        if self._peek().kind is TokenKind.SYMBOL and self._peek().value == "[":
            self._advance()
            with self._nested():
                attributes = self._annotation_attributes()
                elements.extend(self._annotation_elements())
            self._expect_symbol("]")
        return _Annotations(tuple(attributes), tuple(elements))

    def _documentation_annotation(self, token: Token) -> _ForeignAnnotation:
        element = ET.Element(f"{{{_COMPAT_ANNOTATIONS}}}documentation")
        element.text = token.value
        return _ForeignAnnotation(
            "element", "documentation", _COMPAT_ANNOTATIONS, None, token, element
        )

    def _annotation_attributes(self) -> list[_ForeignAnnotation]:
        attributes: list[_ForeignAnnotation] = []
        seen: set[str] = set()
        while True:
            token = self._peek()
            following = self._peek(1)
            if token.kind is not TokenKind.CNAME or not (
                following.kind is TokenKind.SYMBOL and following.value == "="
            ):
                return attributes
            self._advance()
            self._expect_symbol("=")
            value, _ = self._parse_literal()
            namespace, local = self._annotation_prefixed_name(token, initial=True)
            if not self._unique_attribute(seen, namespace, local, token):
                continue
            attributes.append(_ForeignAnnotation("attribute", local, namespace, value, token))

    def _annotation_elements(self) -> list[_ForeignAnnotation]:
        elements: list[_ForeignAnnotation] = []
        while self._starts_annotation_element(self._peek(), self._peek(1)):
            elements.append(self._annotation_element(initial=True))
        return elements

    def _starts_annotation_element(self, token: Token, following: Token) -> bool:
        if token.kind not in {
            TokenKind.IDENTIFIER,
            TokenKind.QUOTED_IDENTIFIER,
            TokenKind.KEYWORD,
            TokenKind.CNAME,
        }:
            return False
        return following.kind is TokenKind.SYMBOL and following.value == "["

    def _annotation_element(self, *, initial: bool) -> _ForeignAnnotation:
        token = self._advance()
        namespace, local = self._annotation_element_name(token, initial=initial)
        element = ET.Element(clark(namespace, local))
        attribute_tokens = self._annotation_attributes_content(element)
        return _ForeignAnnotation(
            "element",
            local,
            namespace,
            None,
            token,
            element,
            attribute_tokens,
        )

    def _annotation_element_name(self, token: Token, *, initial: bool) -> tuple[str | None, str]:
        if token.kind is not TokenKind.CNAME:
            return None, token.value
        prefix, local = token.value.split(":", 1)
        namespace, resolved = self._annotation_namespace(token, prefix)
        if initial and resolved and namespace == RELAX_NG_NAMESPACE:
            self._finding(
                "relaxng-compact-reserved-namespace",
                "an annotation element may not use the RELAX NG namespace",
                token,
            )
        return namespace, local

    def _annotation_prefixed_name(self, token: Token, *, initial: bool) -> tuple[str | None, str]:
        prefix, local = token.value.split(":", 1)
        namespace, resolved = self._annotation_namespace(token, prefix)
        if initial and resolved:
            if namespace is None:
                self._finding(
                    "relaxng-compact-unqualified-attribute-name",
                    "an annotation attribute name must be namespace-qualified",
                    token,
                )
            if namespace == RELAX_NG_NAMESPACE:
                self._finding(
                    "relaxng-compact-reserved-namespace",
                    "an annotation attribute may not use the RELAX NG namespace",
                    token,
                )
        if resolved and namespace == _XMLNS_NAMESPACE:
            self._finding(
                "relaxng-compact-xmlns-namespace",
                "an annotation attribute may not use the XMLNS namespace",
                token,
            )
        return namespace, local

    def _annotation_namespace(self, token: Token, prefix: str) -> tuple[str | None, bool]:
        if prefix not in self.environment.namespaces:
            self._finding(
                "relaxng-compact-unbound-prefix",
                f"the namespace prefix {prefix!r} is not bound in scope",
                token,
            )
            return None, False
        namespace = self.environment.namespaces[prefix]
        if namespace is None:
            self._finding(
                "relaxng-compact-annotation-inherit",
                "an annotation name may not use an inherited namespace",
                token,
            )
            return None, False
        return namespace or None, True

    def _annotation_attributes_content(self, element: ET.Element) -> tuple[tuple[str, Token], ...]:
        self._expect_symbol("[")
        with self._nested():
            attribute_tokens = self._nested_annotation_attributes(element)
            self._annotation_content(element)
        self._expect_symbol("]")
        return attribute_tokens

    def _nested_annotation_attributes(self, element: ET.Element) -> tuple[tuple[str, Token], ...]:
        attribute_tokens: list[tuple[str, Token]] = []
        seen: set[str] = set()
        while True:
            token = self._peek()
            following = self._peek(1)
            if not (
                token.kind
                in {
                    TokenKind.IDENTIFIER,
                    TokenKind.QUOTED_IDENTIFIER,
                    TokenKind.KEYWORD,
                    TokenKind.CNAME,
                }
                and following.kind is TokenKind.SYMBOL
                and following.value == "="
            ):
                return tuple(attribute_tokens)
            self._advance()
            self._expect_symbol("=")
            value, _ = self._parse_literal()
            if token.kind is TokenKind.CNAME:
                namespace, local = self._annotation_prefixed_name(token, initial=False)
            else:
                namespace, local = None, token.value
            attribute_name = self._unique_attribute(seen, namespace, local, token)
            if attribute_name is None:
                continue
            element.set(attribute_name, value)
            attribute_tokens.append((attribute_name, token))

    def _unique_attribute(
        self,
        seen: set[str],
        namespace: str | None,
        local: str,
        token: Token,
    ) -> str | None:
        attribute_name = clark(namespace, local)
        if attribute_name in seen:
            self._finding(
                "relaxng-compact-duplicate-attribute",
                f"the attribute {attribute_name!r} is annotated more than once",
                token,
            )
            return None
        seen.add(attribute_name)
        return attribute_name

    def _annotation_content(self, element: ET.Element) -> None:
        while True:
            token = self._peek()
            if token.kind is TokenKind.LITERAL_SEGMENT:
                value, _ = self._parse_literal()
                self._append_text(element, value)
                continue
            if self._starts_annotation_element(token, self._peek(1)):
                child = self._annotation_element(initial=False)
                assert child.element is not None
                element.append(child.element)
                continue
            return

    def _append_text(self, element: ET.Element, value: str) -> None:
        if not value:
            return
        if len(element):
            child = element[-1]
            child.tail = (child.tail or "") + value
        else:
            element.text = (element.text or "") + value

    def _namespaces(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.frame.items()))

    def _table(self, path: tuple[PathStep, ...], token: Token) -> OriginTable:
        origin = SourceOrigin(
            self.source_id,
            IssueLocation(self.source_id, path, span=token.span),
        )
        self.origins.add(origin)
        return OriginTable(frozenset({origin}))

    def _finding(self, code: str, message: str, token: Token) -> None:
        self.findings.append(
            SchemaFinding(
                code,
                message,
                IssueLocation(self.source_id, (), span=token.span),
            )
        )

    @contextmanager
    def _nested(self) -> Iterator[None]:
        self.depth += 1
        try:
            self.budget.observe("max_compact_depth", self.depth)
            yield
        finally:
            self.depth -= 1

    def _fail(
        self,
        message: str,
        *,
        expected: tuple[str, ...] = (),
        found: str | None = None,
        at: Token | None = None,
    ) -> NoReturn:
        token = at if at is not None else self._peek()
        raise RelaxNGCompactSyntaxError(
            message,
            location=IssueLocation(self.source_id, (), span=token.span),
            found=_describe_token(token) if found is None else found,
            expected=expected,
        )
