"""XSLT 3.0 Pattern30 syntax: scanning, parsing, and the pattern AST.

Patterns are neither XPath expressions nor sequence types.  This module owns
the Pattern30 lexical scanner and recursive-descent grammar in their own right,
the immutable parse-tree nodes those productions build, and the syntactic
default priority of each union alternative.  Predicate expressions and kind
tests are compiled to their immutable forms here, but nothing in this module
evaluates XPath, matches a node, or selects a dispatch plan; that work lives in
:mod:`pyxsd.xslt.compiler.patterns`.

The scanner deliberately leaves predicate expressions opaque and the parser
consumes them by balanced delimiters, so a pattern such as ``a[b = ']']`` keeps
its predicate grammar independent of the pattern grammar.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, NoReturn, TypeAlias

from pyxsd.locations import IssueLocation
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xdm.values import XdmAtomic, XdmSequence
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xpath.environment import XPathStaticContext
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.expressions import (
    ItemType,
    compile_sequence_type,
    validate_variable_references,
)
from pyxsd.xslt.errors import XsltStaticError

FN_NS = "http://www.w3.org/2005/xpath-functions"
_INTERNAL_NS = "urn:pyxsd:xslt:pattern"
_CURRENT_VARIABLE = f"{{{_INTERNAL_NS}}}current"
_CURRENT_REFERENCE = f"$Q{{{_INTERNAL_NS}}}current"

_PATTERN_AXES = frozenset(
    {"child", "descendant", "attribute", "self", "descendant-or-self", "namespace"}
)
_NODE_KINDS = frozenset(NodeKind)

_TYPE_KIND_KINDS: dict[str, frozenset[NodeKind]] = {
    "node": _NODE_KINDS,
    "text": frozenset({NodeKind.TEXT}),
    "comment": frozenset({NodeKind.COMMENT}),
    "namespace-node": frozenset({NodeKind.NAMESPACE}),
    "processing-instruction": frozenset({NodeKind.PROCESSING_INSTRUCTION}),
    "element": frozenset({NodeKind.ELEMENT}),
    "attribute": frozenset({NodeKind.ATTRIBUTE}),
    "schema-element": frozenset({NodeKind.ELEMENT}),
    "schema-attribute": frozenset({NodeKind.ATTRIBUTE}),
    "document-node": frozenset({NodeKind.DOCUMENT}),
}
_KIND_TEST_NAMES = frozenset(_TYPE_KIND_KINDS)
_TYPED_POLICIES: dict[str, str] = {
    "yes": "yes",
    "true": "yes",
    "1": "yes",
    "no": "no",
    "false": "no",
    "0": "no",
    "strict": "strict",
    "lax": "lax",
    "unspecified": "unspecified",
}

_XPATH_WHITESPACE = " \t\r\n"
_NCNAME = re.compile(r"[^\W\d][\w.\-\u00B7\u0300-\u036F\u203F-\u2040]*", re.UNICODE)
_NCNAME_START = re.compile(r"[^\W\d]", re.UNICODE)
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_ERROR_CODE = re.compile(r"\[(?:err:)?([A-Z]{4}[0-9]{4})\]")

__all__ = [
    "AbsoluteOnly",
    "AbsolutePath",
    "AxisName",
    "AxisStep",
    "CompiledPredicate",
    "DescendantPath",
    "FunctionArgument",
    "FunctionBase",
    "IntersectExceptExpr",
    "KindTest",
    "NameTest",
    "NodeTest",
    "ParsedAlternative",
    "PathExpr",
    "PostfixStep",
    "RelativePath",
    "RootedBase",
    "RootedPath",
    "StepExpr",
    "UnionExpr",
    "VariableBase",
    "parse_pattern",
]

AxisName: TypeAlias = Literal[
    "child",
    "child-or-top",
    "attribute",
    "attribute-or-top",
    "self",
    "descendant",
    "descendant-or-self",
    "namespace",
    "namespace-or-top",
]


def _error_code(exc: BaseException, default: str = "XPST0003") -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code.rsplit(":", 1)[-1]
    match = _ERROR_CODE.search(str(exc))
    return default if match is None else match.group(1)


def _static_error(
    context: StaticContext,
    message: str,
    code: str = "XTSE0340",
    *,
    location: IssueLocation | None = None,
    cause: BaseException | None = None,
) -> XsltStaticError:
    return XsltStaticError(
        message,
        code=code,
        uri=context.uri,
        location=location or context.location,
        cause=cause,
    )


# ---------------------------------------------------------------------------
# Tokenizer


@dataclass(frozen=True, slots=True)
class _Token:
    kind: Literal["name", "string", "number", "punct", "eof"]
    value: str
    offset: int
    end: int


class _Scanner:
    """Lex a pattern while leaving predicate expressions opaque."""

    def __init__(self, source: str, context: StaticContext, location: IssueLocation | None) -> None:
        self.source = source
        self.context = context
        self.location = location
        self.tokens: list[_Token] = []
        self.index = 0

    def scan(self) -> tuple[_Token, ...]:
        source = self.source
        length = len(source)
        while self.index < length:
            character = source[self.index]
            if character in _XPATH_WHITESPACE:
                self.index += 1
                continue
            if source.startswith("(:", self.index):
                self.index = self._skip_comment(self.index, source)
                continue
            if character in ("'", '"'):
                self._scan_string(self.index)
                continue
            if source.startswith("Q{", self.index):
                self._scan_eqname(self.index)
                continue
            if character == "*" and source.startswith("*:", self.index):
                local = _NCNAME.match(source, self.index + 2)
                if local is not None:
                    self._emit("name", self.index, local.end())
                    continue
            if character == "*":
                self._emit("name", self.index, self.index + 1)
                continue
            match = _NCNAME.match(source, self.index)
            if match is not None:
                end = match.end()
                if end < length and source[end] == ":" and not source.startswith("::", end):
                    if end + 1 < length and source[end + 1] == "*":
                        self._emit("name", self.index, end + 2)
                        continue
                    local = _NCNAME.match(source, end + 1)
                    if local is not None:
                        self._emit("name", self.index, local.end())
                        continue
                self._emit("name", self.index, end)
                continue
            if character.isdigit() or (
                character == "." and self.index + 1 < length and source[self.index + 1].isdigit()
            ):
                match = _NUMBER.match(source, self.index)
                assert match is not None
                self._emit("number", self.index, match.end())
                continue
            if source.startswith("//", self.index):
                self._emit("punct", self.index, self.index + 2)
                continue
            if source.startswith("::", self.index):
                self._emit("punct", self.index, self.index + 2)
                continue
            self._emit("punct", self.index, self.index + 1)
        self.tokens.append(_Token("eof", "", length, length))
        return tuple(self.tokens)

    def _emit(
        self, kind: Literal["name", "string", "number", "punct"], start: int, end: int
    ) -> None:
        self.tokens.append(_Token(kind, self.source[start:end], start, end))
        self.index = end

    def _skip_comment(self, start: int, source: str) -> int:
        depth = 0
        index = start
        while index < len(source):
            if source.startswith("(:", index):
                depth += 1
                index += 2
                continue
            if source.startswith(":)", index):
                depth -= 1
                index += 2
                if depth == 0:
                    return index
                continue
            index += 1
        raise _static_error(
            self.context,
            f"unterminated XPath comment in pattern {self.source!r}",
            "XTSE0340",
            location=self.location,
        )

    def _scan_string(self, start: int) -> None:
        source = self.source
        quote = source[start]
        index = start + 1
        while index < len(source):
            if source[index] == quote:
                if index + 1 < len(source) and source[index + 1] == quote:
                    index += 2
                    continue
                self.tokens.append(
                    _Token(
                        "string",
                        source[start + 1 : index].replace(quote * 2, quote),
                        start,
                        index + 1,
                    )
                )
                self.index = index + 1
                return
            index += 1
        raise _static_error(
            self.context,
            f"unterminated string literal in pattern {self.source!r}",
            "XTSE0340",
            location=self.location,
        )

    def _scan_eqname(self, start: int) -> None:
        source = self.source
        close = source.find("}", start + 2)
        if close < 0:
            raise _static_error(
                self.context,
                f"unterminated EQName in pattern {source!r}",
                "XTSE0340",
                location=self.location,
            )
        local = _NCNAME.match(source, close + 1)
        if local is None and not (close + 1 < len(source) and source[close + 1] == "*"):
            raise _static_error(
                self.context,
                f"an EQName requires a local name or '*' in pattern {source!r}",
                "XTSE0340",
                location=self.location,
            )
        end = close + 1 if local is None else local.end()
        if local is None:
            end = close + 2
        self.tokens.append(_Token("name", source[start:end], start, end))
        self.index = end


# ---------------------------------------------------------------------------
# Pattern AST


@dataclass(frozen=True, slots=True)
class NameTest:
    """One XPath NameTest with its expanded and wildcard constraints."""

    name: str | None
    local: str | None
    namespace: str | None
    any_namespace: bool = True

    def __post_init__(self) -> None:
        if self.name is not None and (self.local is not None or self.namespace is not None):
            raise ValueError("an exact name test cannot carry wildcard parts")

    def matches(self, node: XdmNode) -> bool:
        if self.name is not None:
            return node.name == self.name
        if self.local is not None and node.local_name != self.local:
            return False
        if self.any_namespace:
            return True
        return (node.namespace_uri or "") == (self.namespace or "")


@dataclass(frozen=True, slots=True)
class KindTest:
    """One NodeTest kind test, compiled by the sequence-type grammar."""

    item_type: ItemType
    source: str
    declarations: tuple[str, ...] = ()
    child_declarations: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return self.item_type.kind


NodeTest: TypeAlias = NameTest | KindTest


@dataclass(frozen=True, slots=True)
class CompiledPredicate:
    """One XPath predicate with its source available for diagnostics."""

    source: str
    expression: CompiledExpression


@dataclass(frozen=True, slots=True)
class AxisStep:
    """One forward axis step with its per-context predicate list."""

    axis: AxisName
    top_axis: AxisName
    node_test: NodeTest
    predicates: tuple[CompiledPredicate, ...]


@dataclass(frozen=True, slots=True)
class PostfixStep:
    """One parenthesized union expression used as a step."""

    union: UnionExpr
    predicates: tuple[CompiledPredicate, ...]


StepExpr: TypeAlias = AxisStep | PostfixStep


@dataclass(frozen=True, slots=True)
class RelativePath:
    """Steps with the separator that precedes each of them."""

    steps: tuple[tuple[str, StepExpr], ...]

    def __post_init__(self) -> None:
        if not self.steps or self.steps[0][0] != "":
            raise ValueError("a relative path requires a first step")


@dataclass(frozen=True, slots=True)
class AbsoluteOnly:
    """The pattern ``/``."""


@dataclass(frozen=True, slots=True)
class AbsolutePath:
    """A path beginning with ``/``."""

    relative: RelativePath


@dataclass(frozen=True, slots=True)
class DescendantPath:
    """A path beginning with ``//``."""

    relative: RelativePath


@dataclass(frozen=True, slots=True)
class VariableBase:
    """A rooted pattern beginning with a variable reference."""

    name: str


@dataclass(frozen=True, slots=True)
class FunctionArgument:
    """One FunctionCallP argument: a variable reference or a literal."""

    kind: Literal["variable", "literal"]
    value: XdmSequence
    name: str | None = None


@dataclass(frozen=True, slots=True)
class FunctionBase:
    """A rooted pattern beginning with a permitted function call."""

    local: str
    arguments: tuple[FunctionArgument, ...]


RootedBase: TypeAlias = VariableBase | FunctionBase


@dataclass(frozen=True, slots=True)
class RootedPath:
    """A rooted path with predicates and an optional following path."""

    base: RootedBase
    predicates: tuple[CompiledPredicate, ...]
    following_separator: str | None = None
    following: RelativePath | None = None


PathExpr: TypeAlias = RelativePath | AbsoluteOnly | AbsolutePath | DescendantPath | RootedPath


@dataclass(frozen=True, slots=True)
class IntersectExceptExpr:
    """One path with optional ``intersect``/``except`` operands."""

    first: PathExpr
    rest: tuple[tuple[Literal["intersect", "except"], PathExpr], ...] = ()


@dataclass(frozen=True, slots=True)
class UnionExpr:
    """Two or more intersect/except alternatives joined by ``|``/``union``."""

    alternatives: tuple[IntersectExceptExpr, ...]


@dataclass(frozen=True, slots=True)
class ParsedAlternative:
    """One syntactic pattern alternative with its normative default priority.

    The priority is computed from the pattern as written (XSLT 3.0 section 6.5)
    before typed-mode reinterpretation.  Candidate filters, forward-plan
    markers, and the compiled pattern itself belong to
    :mod:`pyxsd.xslt.compiler.patterns`.
    """

    priority: Decimal
    path: IntersectExceptExpr | None = None
    predicate: CompiledExpression | None = None


# ---------------------------------------------------------------------------
# Parser


class _Parser:
    def __init__(
        self,
        source: str,
        context: StaticContext,
        location: IssueLocation | None,
        tokens: tuple[_Token, ...],
        *,
        typed: str = "unspecified",
    ) -> None:
        self.source = source
        self.context = context
        self.location = location
        self.tokens = tokens
        self.typed = typed
        self.index = 0

    # -- token helpers ---------------------------------------------------------

    def _peek(self, offset: int = 0) -> _Token:
        return self.tokens[min(self.index + offset, len(self.tokens) - 1)]

    def _advance(self) -> _Token:
        token = self._peek()
        self.index = min(self.index + 1, len(self.tokens) - 1)
        return token

    def _is(self, value: str, offset: int = 0) -> bool:
        return self._peek(offset).value == value

    def _expect(self, value: str) -> _Token:
        token = self._peek()
        if token.value != value:
            self._fail(f"expected {value!r}, found {token.value!r}")
        return self._advance()

    def _fail(self, message: str, code: str = "XTSE0340") -> NoReturn:
        raise _static_error(
            self.context,
            f"invalid pattern {self.source!r}: {message}",
            code,
            location=self.location,
        )

    def _advance_past(self, offset: int) -> None:
        while self._peek().kind != "eof" and self._peek().offset < offset:
            self.index += 1

    # -- grammar ---------------------------------------------------------------

    def parse(self) -> tuple[ParsedAlternative, ...]:
        if self._is("."):
            return self._predicate_pattern()
        union = self._union_expr_p()
        if self._peek().kind != "eof":
            self._fail(f"unexpected token {self._peek().value!r} at offset {self._peek().offset}")
        flattened: list[IntersectExceptExpr] = []
        for alternative in union.alternatives:
            flattened.extend(_flatten_alternative(alternative))
        alternatives: list[ParsedAlternative] = []
        for alternative in flattened:
            # The default priority is based on the pattern syntax as written
            # (section 6.5), so compute it before the typed-mode
            # reinterpretation changes the matching structure.
            priority = _priority_for_intersect_except(alternative)
            alternatives.append(
                ParsedAlternative(priority, path=self._typed_alternative(alternative))
            )
        return tuple(alternatives)

    # -- typed-mode pattern interpretation -------------------------------------

    def _typed_alternative(self, expr: IntersectExceptExpr) -> IntersectExceptExpr:
        if self.typed not in ("strict", "lax"):
            return expr
        return IntersectExceptExpr(
            self._typed_path(expr.first),
            tuple((operator, self._typed_path(path)) for operator, path in expr.rest),
        )

    def _typed_path(self, path: PathExpr) -> PathExpr:
        if isinstance(path, RelativePath):
            return self._typed_relative(path)
        if isinstance(path, AbsolutePath):
            return AbsolutePath(self._typed_relative(path.relative))
        if isinstance(path, DescendantPath):
            return DescendantPath(self._typed_relative(path.relative))
        if isinstance(path, RootedPath):
            following = None if path.following is None else self._typed_relative(path.following)
            return replace(path, following=following)
        return path

    def _typed_relative(self, relative: RelativePath) -> RelativePath:
        steps: list[tuple[str, StepExpr]] = [
            (separator, self._typed_step(step, first=index == 0))
            for index, (separator, step) in enumerate(relative.steps)
        ]
        return RelativePath(tuple(steps))

    def _typed_step(self, step: StepExpr, *, first: bool) -> StepExpr:
        if isinstance(step, PostfixStep):
            # A parenthesized step contains further RelativePathExprP
            # alternatives, each with its own first step.
            return PostfixStep(
                UnionExpr(
                    tuple(
                        self._typed_alternative(alternative)
                        for alternative in step.union.alternatives
                    )
                ),
                step.predicates,
            )
        if not first or not isinstance(step.node_test, NameTest):
            return step
        name = step.node_test.name
        if name is None or _principal_kind(step.axis) != "element":
            return step
        replacement = self._typed_name_test(name)
        if replacement is None:
            return step
        return AxisStep(step.axis, step.top_axis, replacement, step.predicates)

    def _typed_name_test(self, name: str) -> KindTest | None:
        """Return ``schema-element(name)`` for a typed-mode first step.

        ``None`` means the lax mode retains the original name test because
        the in-scope schema components have no global declaration for it.
        """
        pool = self.context.schema
        declaration = None if pool is None else pool.get_element(name)
        if declaration is None:
            if self.typed == "strict":
                raise _static_error(
                    self.context,
                    f"the element name {name!r} in a strict typed-mode pattern "
                    "has no global element declaration",
                    "XTSE3105",
                    location=self.location,
                )
            return None
        declarations = self._element_declarations(name)
        return KindTest(
            ItemType("schema-element", name=name, lexical=name),
            name,
            declarations,
        )

    def _predicate_pattern(self) -> tuple[ParsedAlternative, ...]:
        self._advance()
        predicates = self._predicate_list()
        if self._peek().kind != "eof":
            self._fail("a predicate pattern cannot be combined with a union")
        expression = _compile_expression(self.source, self.context, self.location)
        return (
            ParsedAlternative(
                Decimal(1) if predicates else Decimal(-1),
                predicate=expression,
            ),
        )

    def _union_expr_p(self) -> UnionExpr:
        alternatives = [self._intersect_except_expr_p()]
        while self._is("|") or self._is("union"):
            self._advance()
            alternatives.append(self._intersect_except_expr_p())
        return UnionExpr(tuple(alternatives))

    def _intersect_except_expr_p(self) -> IntersectExceptExpr:
        first = self._path_expr_p()
        rest: list[tuple[Literal["intersect", "except"], PathExpr]] = []
        while self._is("intersect") or self._is("except"):
            operator = self._advance().value
            rest.append((operator, self._path_expr_p()))  # type: ignore[arg-type]
        return IntersectExceptExpr(first, tuple(rest))

    def _path_expr_p(self) -> PathExpr:
        if self._is("//"):
            self._advance()
            return DescendantPath(self._relative_path_expr_p())
        if self._is("/"):
            self._advance()
            if self._can_start_step(self._peek()):
                return AbsolutePath(self._relative_path_expr_p())
            return AbsoluteOnly()
        if self._is("$"):
            return self._rooted_path()
        token = self._peek()
        if token.kind == "name" and self._is("(", 1) and token.value not in _KIND_TEST_NAMES:
            return self._rooted_path()
        return self._relative_path_expr_p()

    @staticmethod
    def _can_start_step(token: _Token) -> bool:
        if token.kind == "name":
            return True
        return token.value in {"@", "("}

    def _rooted_path(self) -> RootedPath:
        if self._is("$"):
            self._advance()
            token = self._peek()
            if token.kind != "name":
                self._fail("a variable reference requires a name")
            self._advance()
            expanded = self._variable_name(token.value)
            self._variable_value(expanded, token.value)
            base: RootedBase = VariableBase(expanded)
        else:
            base = self._function_call()
        predicates = self._predicate_list()
        separator: str | None = None
        following: RelativePath | None = None
        if self._is("/") or self._is("//"):
            separator = self._advance().value
            following = self._relative_path_expr_p()
        return RootedPath(base, predicates, separator, following)

    def _function_call(self) -> FunctionBase:
        token = self._peek()
        assert token.kind == "name"
        self._advance()
        expanded, local = self._function_name(token.value)
        if expanded != FN_NS or local not in {"doc", "id", "element-with-id", "key", "root"}:
            self._fail(
                f"the rooted function {token.value!r} is not doc, id, element-with-id, key, or root"
            )
        self._expect("(")
        arguments: list[FunctionArgument] = []
        if not self._is(")"):
            while True:
                arguments.append(self._function_argument())
                if self._is(","):
                    self._advance()
                    continue
                break
        self._expect(")")
        _validate_function_arity(self, local, len(arguments), token.value)
        return FunctionBase(local, tuple(arguments))

    def _function_argument(self) -> FunctionArgument:
        token = self._peek()
        if token.value == "$":
            self._advance()
            name = self._peek()
            if name.kind != "name":
                self._fail("a variable argument requires a name")
            self._advance()
            expanded = self._variable_name(name.value)
            self._variable_value(expanded, name.value)
            return FunctionArgument("variable", (), expanded)
        if token.kind == "string":
            self._advance()
            return FunctionArgument("literal", (XdmAtomic.string(token.value),))
        if token.kind == "number":
            self._advance()
            return FunctionArgument("literal", (_numeric_literal(token.value),))
        self._fail("a pattern function argument must be a variable reference or a literal")
        raise AssertionError("unreachable")  # pragma: no cover

    def _relative_path_expr_p(self) -> RelativePath:
        steps: list[tuple[str, StepExpr]] = [("", self._step_expr_p())]
        while self._is("/") or self._is("//"):
            separator = self._advance().value
            steps.append((separator, self._step_expr_p()))
        return RelativePath(tuple(steps))

    def _step_expr_p(self) -> StepExpr:
        if self._is("("):
            self._advance()
            union = self._union_expr_p()
            self._expect(")")
            return PostfixStep(union, self._predicate_list())
        axis: str = "child"
        explicit = False
        if self._is("@"):
            self._advance()
            axis = "attribute"
            explicit = True
        elif (
            self._peek().kind == "name"
            and self._is("::", 1)
            and self._peek().value in _PATTERN_AXES
        ):
            axis = self._advance().value
            self._expect("::")
            explicit = True
        node_test = self._node_test(axis)
        predicates = self._predicate_list()
        top_axis = _top_axis(axis, node_test, explicit)
        return AxisStep(axis, top_axis, node_test, predicates)  # type: ignore[arg-type]

    def _node_test(self, axis: str) -> NodeTest:
        token = self._peek()
        if token.kind != "name":
            self._fail(f"expected a node test at offset {token.offset}")
        if token.value in _KIND_TEST_NAMES and self._is("(", 1):
            return self._kind_test(axis, token)
        self._advance()
        return self._name_test(axis, token.value)

    def _kind_test(self, axis: str, token: _Token) -> KindTest:
        open_token = self._peek(1)
        inner, end = self._balanced(open_token.offset, "(", ")")
        del inner
        source = self.source[token.offset : end]
        try:
            compiled = compile_sequence_type(source, self.context, location=self.location)
        except XsltStaticError:
            raise
        item_type = compiled.item_type
        assert item_type is not None and item_type.kind in _KIND_TEST_NAMES
        declarations: tuple[str, ...] = ()
        if item_type.kind in {"schema-element", "schema-attribute"}:
            declarations = self._declarations(item_type)
        child_declarations: tuple[str, ...] = ()
        if (
            item_type.kind == "document-node"
            and item_type.child is not None
            and item_type.child.kind in {"schema-element", "schema-attribute"}
        ):
            child_declarations = self._declarations(item_type.child)
        self._advance_past(end)
        return KindTest(item_type, source, declarations, child_declarations)

    def _declarations(self, item_type: ItemType) -> tuple[str, ...]:
        assert item_type.name is not None
        pool = self.context.schema
        assert pool is not None
        if item_type.kind == "schema-attribute":
            declaration = pool.get_attribute(item_type.name)
            return () if declaration is None else (item_type.name,)
        if pool.get_element(item_type.name) is None:  # pragma: no cover - checked first
            return ()
        return self._element_declarations(item_type.name)

    def _element_declarations(self, name: str) -> tuple[str, ...]:
        """The declaration names accepted by ``schema-element(name)``.

        XPath 3.0 section 2.5.5.4 includes direct and indirect members of the
        substitution group, while the schema pool exposes only direct edges.
        """
        pool = self.context.schema
        assert pool is not None
        names = {name}
        pending = {name}
        while pending:
            discovered: set[str] = set()
            for current in pending:
                for member in pool.get_substitution_group(current) or frozenset():
                    assert member.name is not None
                    discovered.add(member.name)
            pending = discovered - names
            names.update(pending)
        return tuple(sorted(names))

    def _name_test(self, axis: str, raw: str) -> NameTest:
        if raw == "*":
            return NameTest(None, None, None)
        if raw.startswith("*:"):
            return NameTest(None, raw[2:], None)
        if raw.startswith("Q{"):
            close = raw.find("}")
            uri = raw[2:close]
            local = raw[close + 1 :]
            if local == "*":
                return NameTest(None, None, uri, any_namespace=False)
            return NameTest(f"{{{uri}}}{local}" if uri else local, None, None)
        if raw.endswith(":*"):
            prefix = raw[:-2]
            uri = self._prefix(prefix)
            return NameTest(None, None, uri, any_namespace=False)
        prefix, separator, local = raw.partition(":")
        if separator:
            return NameTest(f"{{{self._prefix(prefix)}}}{local}", None, None)
        namespace = (
            self.context.xpath_default_namespace if _principal_kind(axis) == "element" else None
        )
        if namespace:
            return NameTest(f"{{{namespace}}}{raw}", None, None)
        return NameTest(raw, None, None)

    def _prefix(self, prefix: str) -> str:
        uri = self.context.namespaces.get(prefix)
        if uri is None:
            self._fail(
                f"the namespace prefix {prefix!r} is not bound in the static context",
                "XPST0081",
            )
        return uri

    def _function_name(self, raw: str) -> tuple[str, str]:
        if raw.startswith("Q{"):
            close = raw.find("}")
            uri = raw[2:close]
            local = raw[close + 1 :]
            return (uri or ""), local
        prefix, separator, local = raw.partition(":")
        if separator:
            return self._prefix(prefix), local
        return FN_NS, raw

    def _variable_name(self, raw: str) -> str:
        if raw.startswith("Q{"):
            close = raw.find("}")
            uri = raw[2:close]
            local = raw[close + 1 :]
            return f"{{{uri}}}{local}"
        prefix, separator, local = raw.partition(":")
        if separator:
            return f"{{{self._prefix(prefix)}}}{local}"
        return raw

    def _variable_value(self, expanded: str, lexical: str) -> XdmSequence | None:
        if expanded not in self.context.variable_names:
            self._fail(
                f"the variable {lexical!r} is not declared in the static context",
                "XPST0008",
            )
        return None

    def _predicate_list(self) -> tuple[CompiledPredicate, ...]:
        predicates: list[CompiledPredicate] = []
        while self._is("["):
            open_token = self._peek()
            inner, end = self._balanced(open_token.offset, "[", "]")
            predicates.append(
                CompiledPredicate(inner, _compile_expression(inner, self.context, self.location))
            )
            self._advance_past(end)
        return tuple(predicates)

    def _balanced(self, start: int, open_character: str, close_character: str) -> tuple[str, int]:
        source = self.source
        depth = 0
        index = start
        quote: str | None = None
        comment_depth = 0
        while index < len(source):
            character = source[index]
            if comment_depth:
                if source.startswith("(:", index):
                    comment_depth += 1
                    index += 2
                    continue
                if source.startswith(":)", index):
                    comment_depth -= 1
                    index += 2
                    continue
                index += 1
                continue
            if quote is not None:
                if character == quote:
                    if index + 1 < len(source) and source[index + 1] == quote:
                        index += 2
                        continue
                    quote = None
                index += 1
                continue
            if character in ("'", '"'):
                quote = character
            elif source.startswith("(:", index):
                comment_depth = 1
                index += 2
                continue
            elif character == open_character:
                depth += 1
            elif character == close_character:
                depth -= 1
                if depth == 0:
                    return source[start + 1 : index], index + 1
            index += 1
        self._fail(f"unterminated {open_character}{close_character} section")
        raise AssertionError("unreachable")  # pragma: no cover


def _validate_function_arity(parser: _Parser, local: str, count: int, lexical: str) -> None:
    if local == "root":
        if count:
            parser._fail("root() accepts no arguments")
        return
    if local == "doc" and count != 1:
        parser._fail(f"{lexical}() expects 1 argument", "XPST0017")
    if local in {"id", "element-with-id"} and count not in (1, 2):
        parser._fail(f"{lexical}() expects 1 or 2 arguments", "XPST0017")
    if local == "key" and count not in (2, 3):
        parser._fail(f"{lexical}() expects 2 or 3 arguments", "XPST0017")


def _numeric_literal(raw: str) -> XdmAtomic:
    if "e" in raw or "E" in raw:
        return XdmAtomic.double(float(raw))
    if "." in raw:
        return XdmAtomic.decimal(raw)
    return XdmAtomic.integer(int(raw))


def _principal_kind(axis: str) -> Literal["element", "attribute", "namespace"]:
    if axis in {"attribute", "attribute-or-top"}:
        return "attribute"
    if axis in {"namespace", "namespace-or-top"}:
        return "namespace"
    return "element"


def _top_axis(axis: str, node_test: NodeTest, explicit: bool) -> AxisName:
    if axis == "child":
        if isinstance(node_test, KindTest) and node_test.kind == "document-node":
            return "child" if explicit else "self"
        return "child-or-top"
    if axis == "attribute":
        return "attribute-or-top"
    if axis == "namespace":
        return "namespace-or-top"
    return axis  # type: ignore[return-value]


def _rewrite_current(source: str, context: StaticContext, location: IssueLocation | None) -> str:
    """Replace ``current()`` with the pattern-candidate variable.

    ``fn:current`` is not available in the standalone XPath engine and a
    zero-arity host function token cannot be followed by a path.  The pattern
    candidate is a fixed dependency, so a reserved variable is both simpler and
    more precise; ``current()`` in a pattern always denotes that candidate.
    """
    tokens = _Scanner(source, context, location).scan()
    replacements: list[tuple[int, int]] = []
    for index, token in enumerate(tokens[:-1]):
        if token.kind != "name":
            continue
        if not _is_current_function_name(token.value, context):
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].value != "(":
            continue
        if index + 2 >= len(tokens) or tokens[index + 2].value != ")":
            continue
        replacements.append((token.offset, tokens[index + 2].end))
    if not replacements:
        return source
    pieces: list[str] = []
    cursor = 0
    for start, end in replacements:
        pieces.append(source[cursor:start])
        pieces.append(_CURRENT_REFERENCE)
        cursor = end
    pieces.append(source[cursor:])
    return "".join(pieces)


def _function_local_name(name: str, context: StaticContext) -> str | None:
    if name.startswith("Q{"):
        close = name.find("}")
        if close >= 0 and name[2:close] == FN_NS:
            return name[close + 1 :]
        return None
    prefix, separator, local = name.partition(":")
    if not separator:
        return name
    return local if context.namespaces.get(prefix) == FN_NS else None


_PATTERN_FORBIDDEN_FUNCTIONS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "current-group": ("XTSE1060", "2.0"),
        "current-grouping-key": ("XTSE1070", "2.0"),
        "current-merge-group": ("XTSE3470", "3.0"),
        "current-merge-key": ("XTSE3500", "3.0"),
    }
)


def _reject_pattern_context_functions(
    source: str,
    context: StaticContext,
    location: IssueLocation | None,
) -> None:
    """Report XSLT context-dependent functions that patterns cannot use."""
    tokens = _Scanner(source, context, location).scan()
    for index, token in enumerate(tokens[:-1]):
        if token.kind != "name":
            continue
        local = _function_local_name(token.value, context)
        forbidden = _PATTERN_FORBIDDEN_FUNCTIONS.get(local or "")
        if forbidden is None or tokens[index + 1].value not in ("(", "#"):
            continue
        code, minimum = forbidden
        if Decimal(context.version) < Decimal(minimum):
            continue
        raise _static_error(
            context,
            f"{token.value} is not available in a pattern",
            code,
            location=location,
        )


def _is_current_function_name(name: str, context: StaticContext) -> bool:
    if name == "current":
        return True
    if name.startswith("Q{"):
        close = name.find("}")
        return close >= 0 and name[2:close] == FN_NS and name[close + 1 :] == "current"
    prefix, separator, local = name.partition(":")
    if not separator:
        return False
    return local == "current" and context.namespaces.get(prefix) == FN_NS


def _compile_expression(
    source: str, context: StaticContext, location: IssueLocation | None
) -> CompiledExpression:
    if not source.strip():
        raise _static_error(
            context,
            "an XPath predicate must not be empty",
            "XPST0003",
            location=location,
        )
    _reject_pattern_context_functions(source, context, location)
    static_context = context.xpath_static_context()
    variable_types = dict(static_context.variable_types)
    variable_types[_CURRENT_VARIABLE] = "item()"
    namespaces = dict(static_context.namespaces)
    namespaces.setdefault("__pyxsd_pattern", _INTERNAL_NS)
    static_context = XPathStaticContext(
        namespaces=namespaces,
        default_namespace=static_context.default_namespace,
        variable_types=variable_types,
        functions=dict(static_context.functions),
        schema=static_context.schema,
        base_uri=static_context.base_uri,
        default_collation=static_context.default_collation,
        xsd_version=static_context.xsd_version,
        xslt_version=static_context.xslt_version,
    )
    rewritten = _rewrite_current(source, context, location)
    engine = XPathEngine(context.xpath_version)
    try:
        compiled = engine.compile(rewritten, static_context)
    except Exception as exc:
        raise _static_error(
            context,
            f"invalid pattern predicate {source!r}: {exc}",
            _error_code(exc),
            location=location,
            cause=exc,
        ) from exc
    validate_variable_references(
        context,
        compiled.free_variable_names - {_CURRENT_VARIABLE},
        source=source,
        location=location,
    )
    return compiled


# ---------------------------------------------------------------------------
# Default priority


def _priority_for_intersect_except(expr: IntersectExceptExpr) -> Decimal:
    return _priority_for_path(expr.first)


def _priority_for_path(path: PathExpr) -> Decimal:
    if isinstance(path, AbsoluteOnly):
        return Decimal("-0.5")
    if isinstance(path, RelativePath) and len(path.steps) == 1:
        step = path.steps[0][1]
        if isinstance(step, AxisStep):
            return _priority_for_step(step)
        return _priority_for_postfix(step)
    return Decimal("0.5")


def _priority_for_postfix(step: PostfixStep) -> Decimal:
    # An empty, predicate-free top-level parenthesized union is flattened into
    # its alternatives by :func:`_flatten_alternative`, so a surviving postfix
    # step is a genuine selection expression and has the generic priority.
    del step
    return Decimal("0.5")


def _priority_for_step(step: AxisStep) -> Decimal:
    if step.predicates:
        return Decimal("0.5")
    node_test = step.node_test
    if isinstance(node_test, NameTest):
        if node_test.name is not None:
            return Decimal(0)
        if node_test.local is None and node_test.namespace is None:
            return Decimal("-0.5")
        return Decimal("-0.25")
    return _priority_for_kind(node_test.item_type)


def _priority_for_kind(item_type: ItemType) -> Decimal:
    kind = item_type.kind
    if kind in {"schema-element", "schema-attribute"}:
        return Decimal("0.25")
    if kind in {"element", "attribute"}:
        has_name = item_type.name is not None and not item_type.wildcard
        has_type = item_type.type_name is not None
        if has_name and has_type:
            return Decimal("0.25")
        if has_type:
            return Decimal(0)
        if has_name:
            return Decimal(0)
        return Decimal("-0.5")
    if kind == "document-node":
        if item_type.child is None:
            return Decimal("-0.5")
        return _priority_for_kind(item_type.child)
    if kind == "processing-instruction":
        return Decimal(0) if item_type.target is not None else Decimal("-0.5")
    return Decimal("-0.5")


def _flatten_alternative(expr: IntersectExceptExpr) -> tuple[IntersectExceptExpr, ...]:
    """Strip grouping parentheses and split a top-level union alternative.

    XSLT 3.0 section 6.5 rules 1 and 2 strip a top-level ParenthesizedExprP and
    then treat each union alternative as an adjacent template rule.  A postfix
    step with predicates is a genuine selection and is not split.
    """
    if expr.rest:
        return (expr,)
    path = expr.first
    if isinstance(path, RelativePath) and len(path.steps) == 1:
        step = path.steps[0][1]
        if isinstance(step, PostfixStep) and not step.predicates:
            flattened: list[IntersectExceptExpr] = []
            for alternative in step.union.alternatives:
                flattened.extend(_flatten_alternative(alternative))
            return tuple(flattened)
    return (expr,)


# ---------------------------------------------------------------------------
# Syntax entry point


def parse_pattern(
    source: str,
    context: StaticContext,
    *,
    typed: str = "unspecified",
    location: IssueLocation | None = None,
) -> tuple[ParsedAlternative, ...]:
    """Scan and parse one Pattern30 into syntactic alternatives.

    *typed* selects the mode's ``xsl:mode/@typed`` policy.  ``strict`` and
    ``lax`` reinterpret first-step element name tests as ``schema-element``
    tests as required by XSLT 3.0 section 6.6.3; the other policies leave the
    pattern unchanged.
    """
    policy = _normalized_typed_policy(typed)
    tokens = _Scanner(source, context, location).scan()
    return _Parser(source, context, location, tokens, typed=policy).parse()


def _normalized_typed_policy(value: str) -> str:
    """Validate a ``typed`` mode policy and normalize boolean synonyms."""
    if not isinstance(value, str):
        raise TypeError("typed must be a string")
    policy = _TYPED_POLICIES.get(value.strip())
    if policy is None:
        raise ValueError(f"unknown typed mode policy {value!r}")
    return policy
