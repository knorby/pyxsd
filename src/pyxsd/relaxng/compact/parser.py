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

Documentation tokens are accepted and discarded here; bracket, following, and
grammar annotations are completed by the next stage.  One reader instance owns
all of its state, so separate compilations never share an environment, token
stream, grammar scope, or budget observation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import NoReturn, cast
from xml.etree import ElementTree as ET

from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import XML_NS, NamespaceContext
from pyxsd.relaxng.compact.lexer import Token, TokenKind, lex_compact
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError
from pyxsd.relaxng.limits import BudgetCounter
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
from pyxsd.relaxng.resources import LoadedCompactResource, LoadedResource
from pyxsd.relaxng.syntax import (
    RELAX_NG_NAMESPACE,
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
)

__all__ = ["read_compact_syntax"]

#: The datatype library the predeclared ``xsd`` prefix selects.
_XSD_DATATYPES = "http://www.w3.org/2001/XMLSchema-datatypes"

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
    """

    namespaces: dict[str, str | None]
    datatypes: dict[str, str]
    default_namespace: str | None
    default_declared: bool
    base_uri: str


@dataclass(frozen=True)
class _DataInfo:
    """The datatype attributes and children a ``data`` pattern will carry."""

    token: Token
    library: str
    type_name: str
    parameters: tuple[_Shape, ...] = ()
    except_shape: _Shape | None = None
    except_token: Token | None = None


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
        self.grammars: list[GrammarScope] = []
        self.environment: _Environment = _Environment({}, {}, None, False, self.source_id)
        self.frame: dict[str, str] = {}

    # -- entry point ------------------------------------------------------

    def read(self) -> FullSchema:
        self.environment = self._preamble()
        self.frame = self._namespace_frame(self.environment)
        self._annotations()
        token = self._peek()
        if token.kind is TokenKind.EOF or self._starts_top_level_grammar():
            shape = self._grammar_shape(top_level=True)
        else:
            shape = self._parse_pattern_shape()
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
            findings=(),
        )

    def _starts_top_level_grammar(self) -> bool:
        token = self._peek()
        if token.kind is TokenKind.KEYWORD and token.value in {"start", "div", "include"}:
            return True
        if token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
            following = self._peek(1)
            return following.kind is TokenKind.SYMBOL and following.value in _ASSIGNMENT_COMBINES
        return False

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
        environment.namespaces.setdefault(prefix.value, self._namespace_uri())

    def _default_namespace_declaration(self, environment: _Environment) -> None:
        self._advance()
        self._expect_keyword("namespace")
        prefix: Token | None = None
        token = self._peek()
        if token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER, TokenKind.KEYWORD}:
            prefix = self._identifier_or_keyword()
        self._expect_symbol("=")
        namespace = self._namespace_uri()
        if not environment.default_declared:
            environment.default_namespace = namespace
            environment.default_declared = True
        if prefix is not None:
            environment.namespaces.setdefault(prefix.value, namespace)

    def _datatypes_declaration(self, environment: _Environment) -> None:
        self._advance()
        prefix = self._identifier_or_keyword()
        self._expect_symbol("=")
        value, _ = self._parse_literal()
        environment.datatypes.setdefault(prefix.value, value)

    def _namespace_uri(self) -> str | None:
        token = self._peek()
        if token.kind is TokenKind.KEYWORD and token.value == "inherit":
            self._advance()
            return None
        value, _ = self._parse_literal()
        return value

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

    def _grammar_shape(self, *, top_level: bool) -> _Shape:
        if top_level:
            token = self._peek()
            members = self._grammar_members(end=None, allow_include=True)
            if members:
                token = members[0].token
        else:
            token = self._expect_keyword("grammar")
            self._expect_symbol("{")
            with self._nested():
                members = self._grammar_members(end="}", allow_include=True)
            self._expect_symbol("}")

        def build(path: tuple[PathStep, ...], parent_scope: GrammarScope | None) -> FullNode:
            origins = self._table(path, token)
            scope = GrammarScope(parent=parent_scope, origins=origins, annotations=())
            self.grammars.append(scope)
            children = _Children(path)
            for member in members:
                scope._append(member.build(children.path_for(member.tag), scope))
            return FullGrammar(scope, origins, ())

        return _Shape("grammar", token, build)

    def _grammar_members(self, *, end: str | None, allow_include: bool) -> list[_Shape]:
        members: list[_Shape] = []
        while not self._at_grammar_end(end):
            members.append(self._grammar_member(allow_include=allow_include))
        return members

    def _at_grammar_end(self, end: str | None) -> bool:
        token = self._peek()
        if end is None:
            return token.kind is TokenKind.EOF
        return token.kind is TokenKind.SYMBOL and token.value == end

    def _grammar_member(self, *, allow_include: bool) -> _Shape:
        self._annotations()
        token = self._peek()
        if token.kind is TokenKind.KEYWORD:
            if token.value == "start":
                return self._start_shape()
            if token.value == "div":
                return self._div_shape()
            if token.value == "include":
                if not allow_include:
                    self._fail(
                        "an include body cannot contain another include",
                        expected=("'start'", "'define'", "'div'"),
                        at=token,
                    )
                return self._include_shape()
        if token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
            return self._define_shape()
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
            return FullStart(combine, (body,), self._table(path, token), ())

        return _Shape("start", token, build)

    def _define_shape(self) -> _Shape:
        name = self._identifier()
        combine = self._assignment_operator()
        pattern = self._parse_pattern_shape()

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            body = pattern.build(_Children(path).path_for(pattern.tag), scope)
            origins = self._table(path, name)
            return FullDefine(name.value, combine, (body,), origins, ())

        return _Shape("define", name, build)

    def _div_shape(self) -> _Shape:
        token = self._expect_keyword("div")
        self._expect_symbol("{")
        with self._nested():
            members = self._grammar_members(end="}", allow_include=True)
        self._expect_symbol("}")

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            children = _Children(path)
            built = tuple(member.build(children.path_for(member.tag), scope) for member in members)
            return FullDiv(built, self._table(path, token), ())

        return _Shape("div", token, build)

    def _include_shape(self) -> _Shape:
        token = self._expect_keyword("include")
        href, _ = self._parse_literal()
        namespace = self._optional_inherit()
        members: list[_Shape] = []
        if self._peek().kind is TokenKind.SYMBOL and self._peek().value == "{":
            self._advance()
            with self._nested():
                members = self._grammar_members(end="}", allow_include=False)
            self._expect_symbol("}")

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            children = _Children(path)
            built = tuple(member.build(children.path_for(member.tag), scope) for member in members)
            return ReferencePlaceholder(
                kind="include",
                name=None,
                href=href,
                base=self.environment.base_uri,
                ns_attribute=namespace,
                scope=scope,
                children=built,
                origins=self._table(path, token),
                annotations=(),
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
        self._annotations()
        first = self._parse_particle_shape()
        token = self._peek()
        if token.kind is not TokenKind.SYMBOL or token.value not in _OPERATORS:
            return first
        operator = token.value
        parts = [first]
        self._advance()
        parts.append(self._parse_particle_shape())
        while True:
            token = self._peek()
            if token.kind is TokenKind.SYMBOL and token.value == operator:
                self._advance()
                parts.append(self._parse_particle_shape())
                continue
            if token.kind is TokenKind.SYMBOL and token.value in _OPERATORS:
                self._fail(
                    "the patterns , | and & may not be mixed without parentheses",
                    expected=(repr(operator),),
                    at=token,
                )
            return self._pattern_container_shape(_OPERATORS[operator], token, parts)

    def _parse_particle_shape(self) -> _Shape:
        shape = self._parse_lead_primary_shape()
        token = self._peek()
        has_exception = False
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
            shape = self._data_shape(replace(info, except_shape=excluded, except_token=token))
            has_exception = True
        token = self._peek()
        if token.kind is TokenKind.SYMBOL and token.value in _REPEAT_TAGS:
            if has_exception:
                self._fail(
                    "an exception pattern cannot be repeated",
                    expected=("a pattern",),
                    at=token,
                )
            self._advance()
            shape = self._repeat_shape(shape, token)
            trailing = self._peek()
            if trailing.kind is TokenKind.SYMBOL and trailing.value in _REPEAT_TAGS:
                self._fail(
                    "a pattern may have only one repetition operator",
                    expected=("a pattern",),
                    at=trailing,
                )
        return shape

    def _parse_lead_primary_shape(self) -> _Shape:
        self._annotations()
        token = self._peek()
        if token.kind is TokenKind.SYMBOL and token.value == "(":
            self._advance()
            with self._nested():
                shape = self._parse_pattern_shape()
            self._expect_symbol(")")
            return shape
        if token.kind is TokenKind.KEYWORD:
            return self._keyword_primary_shape(token)
        if token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER}:
            self._advance()
            return self._reference_shape(token)
        if token.kind is TokenKind.CNAME:
            return self._datatype_shape()
        if token.kind is TokenKind.LITERAL_SEGMENT:
            lexical, first = self._parse_literal()
            return self._value_shape(first, "", None, lexical)
        self._fail(
            "expected a pattern",
            expected=("a pattern",),
            at=token,
        )

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
        if token.value == "grammar":
            return self._grammar_shape(top_level=False)
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
            if attribute:
                return FullAttribute(name_class, (content,), origins, ())
            return FullElement(name_class, (content,), origins, ())

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
            return constructor((content,), self._table(path, token))

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
            if tag == "group":
                return FullGroup(built, origins, ())
            if tag == "choice":
                return FullChoice(built, origins, ())
            return FullInterleave(built, origins, ())

        return _Shape(tag, token, build)

    def _repeat_shape(self, shape: _Shape, token: Token) -> _Shape:
        tag = _REPEAT_TAGS[token.value]

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            content = shape.build(_Children(path).path_for(shape.tag), scope)
            origins = self._table(path, token)
            if tag == "optional":
                return FullOptional((content,), origins, ())
            if tag == "zeroOrMore":
                return FullZeroOrMore((content,), origins, ())
            return FullOneOrMore((content,), origins, ())

        return _Shape(tag, token, build)

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
            if info.except_shape is not None and info.except_token is not None:
                except_path = (*path, PathStep("except", 1))
                excluded = info.except_shape.build(
                    (*except_path, PathStep(info.except_shape.tag, 1)), scope
                )
                built.append(
                    FullExcept(
                        (excluded,),
                        self._table(except_path, info.except_token),
                        (),
                    )
                )
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

    def _parse_parameters(self) -> tuple[_Shape, ...]:
        self._expect_symbol("{")
        parameters: list[_Shape] = []
        with self._nested():
            while not (self._peek().kind is TokenKind.SYMBOL and self._peek().value == "}"):
                parameters.append(self._parameter_shape())
        self._expect_symbol("}")
        return tuple(parameters)

    def _parameter_shape(self) -> _Shape:
        self._annotations()
        name = self._identifier_or_keyword()
        self._expect_symbol("=")
        value, _ = self._parse_literal()

        def build(path: tuple[PathStep, ...], _scope: GrammarScope | None) -> FullNode:
            return FullParam(name.value, value, (), self._table(path, name), ())

        return _Shape("param", name, build)

    # -- name classes -----------------------------------------------------

    def _name_class_shape(self, *, is_element: bool) -> _Shape:
        self._annotations()
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
            return shape
        if token.kind is TokenKind.SYMBOL and token.value == "|":
            alternatives = [first]
            while self._peek().kind is TokenKind.SYMBOL and self._peek().value == "|":
                self._advance()
                alternatives.append(self._parse_lead_simple_name_class(is_element=is_element))
            trailing = self._peek()
            if trailing.kind is TokenKind.SYMBOL and trailing.value == "-":
                self._fail(
                    "the name-class | and - operators may not be mixed without parentheses",
                    expected=("a parenthesized name class",),
                    at=trailing,
                )
            return self._name_choice_shape(token, alternatives)
        return first

    def _parse_lead_simple_name_class(self, *, is_element: bool) -> _Shape:
        self._annotations()
        token = self._peek()
        if token.kind is TokenKind.SYMBOL and token.value == "(":
            self._advance()
            with self._nested():
                shape = self._name_class_shape(is_element=is_element)
            self._expect_symbol(")")
            return shape
        if token.kind is TokenKind.SYMBOL and token.value == "*":
            self._advance()
            return self._wildcard_shape("anyName", token, None)
        if token.kind is TokenKind.NS_NAME:
            self._advance()
            uri = self._inherited_value(self._namespace_binding(token, token.value))
            return self._wildcard_shape("nsName", token, uri)
        if token.kind is TokenKind.CNAME:
            self._advance()
            prefix, local = token.value.split(":", 1)
            uri = self._inherited_value(self._namespace_binding(token, prefix))
            return self._name_shape(token, token.value, local, uri)
        if token.kind in {TokenKind.IDENTIFIER, TokenKind.QUOTED_IDENTIFIER, TokenKind.KEYWORD}:
            self._advance()
            uri = self._inherited_value(self.environment.default_namespace) if is_element else None
            return self._name_shape(token, token.value, token.value, uri)
        self._fail(
            "expected a name, nsName, anyName, or parenthesized name class",
            expected=("a name class",),
            at=token,
        )

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
            return FullNameChoice(built, self._table(path, token), ())

        return _Shape("choice", token, build)

    def _name_except_shape(self, wildcard: _Shape, token: Token, excluded: _Shape) -> _Shape:
        uri = cast(str | None, wildcard.payload)

        def build(path: tuple[PathStep, ...], scope: GrammarScope | None) -> FullNode:
            except_path = (*path, PathStep("except", 1))
            alternative = cast(
                FullNameClass,
                excluded.build((*except_path, PathStep(excluded.tag, 1)), scope),
            )
            exception = FullNameExcept((alternative,), self._table(except_path, token), ())
            origins = self._table(path, wildcard.token)
            if wildcard.tag == "anyName":
                return FullAnyName(exception, origins, ())
            return FullNsName(uri, exception, origins, ())

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

    def _annotations(self) -> None:
        """Consume documentation annotations; bracket forms arrive next."""
        while self._peek().kind is TokenKind.DOCUMENTATION:
            self._advance()

    def _namespaces(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.frame.items()))

    def _table(self, path: tuple[PathStep, ...], token: Token) -> OriginTable:
        origin = SourceOrigin(
            self.source_id,
            IssueLocation(self.source_id, path, span=token.span),
        )
        self.origins.add(origin)
        return OriginTable(frozenset({origin}))

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
