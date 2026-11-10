"""Normative XSLT 3.0 Pattern30 compilation and candidate-aware matching.

Patterns are neither XPath expressions nor sequence types.  This module parses
the Pattern30 grammar in its own right, computes the normative default priority,
and matches with two plans:

* an optimized right-to-left plan for selection patterns without parenthesized
  postfix steps.  It applies cheap kind/name/type filters before walking
  parents and ancestors, and uses the candidate's own sibling focus for
  positional predicates; and
* a formal ``root(.)//(EE)``-equivalent forward plan for complex patterns.
  It is memoized per tree and environment, so an accidental whole-tree scan is
  visible in :class:`PatternStatistics` rather than hidden behind elapsed time.

Both plans share one predicate evaluator, rooted-function implementation, and
schema-test implementation, and every dynamic error raised while matching one
item is treated as a non-match as required by XSLT 3.0 section 5.5.4.

A bare ``map(*)`` is a sequence type and is rejected with ``XTSE0340``; the
generalized item pattern is ``.[. instance of map(*)]``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, NoReturn, TypeAlias, cast
from urllib.parse import urljoin

from pyxsd.locations import IssueLocation
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xdm.values import XdmAtomic, XdmItem, XdmSequence, as_xdm
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, XPathStaticContext
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.expressions import ItemType, compile_sequence_type
from pyxsd.xslt.errors import XsltStaticError

XSD_NS = "http://www.w3.org/2001/XMLSchema"
FN_NS = "http://www.w3.org/2005/xpath-functions"
_INTERNAL_NS = "urn:pyxsd:xslt:pattern"
_CURRENT_VARIABLE = f"{{{_INTERNAL_NS}}}current"
_CURRENT_REFERENCE = f"$Q{{{_INTERNAL_NS}}}current"

_PATTERN_AXES = frozenset(
    {"child", "descendant", "attribute", "self", "descendant-or-self", "namespace"}
)
_NODE_KINDS = frozenset(NodeKind)
_ELEMENT_AXIS_KINDS = frozenset(
    {NodeKind.ELEMENT, NodeKind.TEXT, NodeKind.COMMENT, NodeKind.PROCESSING_INSTRUCTION}
)
_DESCENDANT_OR_SELF_KINDS = frozenset(
    {
        NodeKind.DOCUMENT,
        NodeKind.ELEMENT,
        NodeKind.TEXT,
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
    }
)
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
_NUMERIC_LOCALS = frozenset(
    {
        "decimal",
        "double",
        "float",
        "byte",
        "int",
        "integer",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "short",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)
_STRING_LOCALS = frozenset(
    {
        "anyURI",
        "ENTITIES",
        "ENTITY",
        "ID",
        "IDREF",
        "IDREFS",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "NMTOKENS",
        "normalizedString",
        "string",
        "token",
        "untypedAtomic",
    }
)
_XPATH_WHITESPACE = " \t\r\n"
_NCNAME = re.compile(r"[^\W\d][\w.\-\u00B7\u0300-\u036F\u203F-\u2040]*", re.UNICODE)
_NCNAME_START = re.compile(r"[^\W\d]", re.UNICODE)
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_ERROR_CODE = re.compile(r"\[(?:err:)?([A-Z]{4}[0-9]{4})\]")

__all__ = [
    "CompiledPattern",
    "PatternAlternative",
    "PatternMatcher",
    "PatternStatistics",
    "compile_pattern",
    "default_priority",
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


def _local_name(name: str) -> str:
    return name.partition("}")[2] if name.startswith("{") else name


def _namespace_of(name: str) -> str | None:
    if not name.startswith("{"):
        return None
    return name[1:].partition("}")[0]


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
class CandidateShape:
    """A cheap kind/name filter for one possible selected node."""

    kinds: frozenset[NodeKind]
    name: str | None = None
    local: str | None = None
    namespace: str | None = None
    any_namespace: bool = True
    target: str | None = None

    def matches(self, item: XdmItem) -> bool:
        if not isinstance(item, XdmNode):
            return False
        if item.kind not in self.kinds:
            return False
        if self.name is not None and item.name != self.name:
            return False
        if self.local is not None and item.local_name != self.local:
            return False
        if not self.any_namespace and (item.namespace_uri or "") != (self.namespace or ""):
            return False
        return self.target is None or item.target == self.target


@dataclass(frozen=True, slots=True)
class CandidateFilter:
    """Candidate shapes for one selection alternative."""

    shapes: tuple[CandidateShape, ...] = ()
    all_items: bool = False

    def matches(self, item: XdmItem) -> bool:
        if self.all_items:
            return True
        return any(shape.matches(item) for shape in self.shapes)


@dataclass(frozen=True, slots=True)
class PatternAlternative:
    """One normative union alternative with its default priority."""

    index: int
    priority: Decimal
    predicate: CompiledExpression | None = None
    path: IntersectExceptExpr | None = None
    filter: CandidateFilter = field(default_factory=CandidateFilter)
    requires_forward: bool = False

    def __post_init__(self) -> None:
        if (self.predicate is None) == (self.path is None):
            raise ValueError("a pattern alternative is either a predicate or a selection")


@dataclass(frozen=True, slots=True)
class CompiledPattern:
    """One immutable compiled Pattern30 with normative per-alternative priorities."""

    source: str
    context: StaticContext
    alternatives: tuple[PatternAlternative, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("a pattern requires non-empty source")
        if not isinstance(self.context, StaticContext):
            raise TypeError("context must be a StaticContext")
        if not self.alternatives or not all(
            isinstance(alternative, PatternAlternative) for alternative in self.alternatives
        ):
            raise TypeError("alternatives must be a non-empty tuple of PatternAlternative")

    @property
    def default_priorities(self) -> tuple[Decimal, ...]:
        return tuple(alternative.priority for alternative in self.alternatives)


# ---------------------------------------------------------------------------
# Parser


class _Parser:
    def __init__(
        self,
        source: str,
        context: StaticContext,
        location: IssueLocation | None,
        tokens: tuple[_Token, ...],
    ) -> None:
        self.source = source
        self.context = context
        self.location = location
        self.tokens = tokens
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

    def parse(self) -> tuple[PatternAlternative, ...]:
        if self._is("."):
            return self._predicate_pattern()
        union = self._union_expr_p()
        if self._peek().kind != "eof":
            self._fail(f"unexpected token {self._peek().value!r} at offset {self._peek().offset}")
        alternatives: list[PatternAlternative] = []
        for index, alternative in enumerate(union.alternatives):
            alternatives.append(
                PatternAlternative(
                    index=index,
                    priority=_priority_for_intersect_except(alternative),
                    path=alternative,
                    filter=_candidate_filter(alternative),
                    requires_forward=_contains_postfix(alternative),
                )
            )
        return tuple(alternatives)

    def _predicate_pattern(self) -> tuple[PatternAlternative, ...]:
        self._advance()
        predicates = self._predicate_list()
        if self._peek().kind != "eof":
            self._fail("a predicate pattern cannot be combined with a union")
        expression = _compile_expression(self.source, self.context, self.location)
        return (
            PatternAlternative(
                index=0,
                priority=Decimal(1) if predicates else Decimal(-1),
                predicate=expression,
                filter=CandidateFilter(all_items=True),
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
        members = pool.get_substitution_group(item_type.name) or frozenset()
        names = {item_type.name}
        names.update(member.name for member in members if member.name is not None)
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
        if self.context.variable(expanded) is None:
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
        if token.value not in {"current", "fn:current", f"Q{{{FN_NS}}}current"}:
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
    )
    rewritten = _rewrite_current(source, context, location)
    engine = XPathEngine(context.xpath_version)
    try:
        return engine.compile(rewritten, static_context)
    except Exception as exc:
        raise _static_error(
            context,
            f"invalid pattern predicate {source!r}: {exc}",
            _error_code(exc),
            location=location,
            cause=exc,
        ) from exc


# ---------------------------------------------------------------------------
# Default priority and candidate filters


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
    if step.predicates:
        return Decimal("0.5")
    priorities = [
        _priority_for_intersect_except(alternative) for alternative in step.union.alternatives
    ]
    return max(priorities)


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


def _candidate_filter(expr: IntersectExceptExpr) -> CandidateFilter:
    return CandidateFilter(_shapes_for_path(expr.first))


def _shapes_for_path(path: PathExpr) -> tuple[CandidateShape, ...]:
    if isinstance(path, RelativePath):
        return _shapes_for_step(path.steps[-1][1])
    if isinstance(path, AbsoluteOnly):
        return (CandidateShape(frozenset({NodeKind.DOCUMENT})),)
    if isinstance(path, (AbsolutePath, DescendantPath)):
        return _shapes_for_step(path.relative.steps[-1][1])
    return (CandidateShape(_NODE_KINDS),)


def _shapes_for_step(step: StepExpr) -> tuple[CandidateShape, ...]:
    if isinstance(step, PostfixStep):
        shapes: list[CandidateShape] = []
        for alternative in step.union.alternatives:
            shapes.extend(_shapes_for_path(alternative.first))
        return tuple(shapes)
    possible = _AXIS_KINDS[step.axis]
    node_test = step.node_test
    if isinstance(node_test, NameTest):
        shapes = [
            CandidateShape(
                frozenset({_principal_kind_node(step.axis)}),
                name=node_test.name,
                local=node_test.local,
                namespace=node_test.namespace,
                any_namespace=node_test.any_namespace,
            )
        ]
        return tuple(shape for shape in shapes if shape.kinds <= possible)
    kinds = _TYPE_KIND_KINDS[node_test.kind] & possible
    if not kinds:
        return ()
    name: str | None = None
    target: str | None = None
    if node_test.kind in {"element", "attribute"}:
        item_type = node_test.item_type
        if item_type.name is not None and not item_type.wildcard:
            name = item_type.name
    if node_test.kind == "processing-instruction":
        target = node_test.item_type.target
    return (CandidateShape(frozenset(kinds), name=name, target=target),)


def _principal_kind_node(axis: str) -> NodeKind:
    principal = _principal_kind(axis)
    return {
        "element": NodeKind.ELEMENT,
        "attribute": NodeKind.ATTRIBUTE,
        "namespace": NodeKind.NAMESPACE,
    }[principal]


_AXIS_KINDS: dict[str, frozenset[NodeKind]] = {
    "child": _ELEMENT_AXIS_KINDS,
    "child-or-top": _ELEMENT_AXIS_KINDS,
    "attribute": frozenset({NodeKind.ATTRIBUTE}),
    "attribute-or-top": frozenset({NodeKind.ATTRIBUTE}),
    "self": _NODE_KINDS,
    "descendant": _ELEMENT_AXIS_KINDS,
    "descendant-or-self": _DESCENDANT_OR_SELF_KINDS,
    "namespace": frozenset({NodeKind.NAMESPACE}),
    "namespace-or-top": frozenset({NodeKind.NAMESPACE}),
}


def _contains_postfix(expr: IntersectExceptExpr) -> bool:
    return _path_contains_postfix(expr.first) or any(
        _path_contains_postfix(path) for _, path in expr.rest
    )


def _path_contains_postfix(path: PathExpr) -> bool:
    if isinstance(path, (AbsolutePath, DescendantPath)):
        return _relative_contains_postfix(path.relative)
    if isinstance(path, RelativePath):
        return _relative_contains_postfix(path)
    if isinstance(path, RootedPath):
        return path.following is not None and _relative_contains_postfix(path.following)
    return False


def _relative_contains_postfix(path: RelativePath) -> bool:
    for _, step in path.steps:
        if isinstance(step, PostfixStep):
            return True
        if isinstance(step, AxisStep) and step.predicates:
            # Predicates are XPath, not patterns, but their grammar is complete
            # and does not contain pattern postfix steps.
            continue
    return False


# ---------------------------------------------------------------------------
# Matching statistics and public matcher


@dataclass(slots=True)
class PatternStatistics:
    """Operation counters for one matcher, index, or diagnostic run."""

    candidate_filters: int = 0
    candidate_tests: int = 0
    ancestor_steps: int = 0
    predicate_evaluations: int = 0
    equivalent_expression_evaluations: int = 0
    function_evaluations: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_invalidations: int = 0
    errors_ignored: int = 0


class PatternMatcher:
    """Match one compiled pattern with an optimized or formal plan."""

    def __init__(
        self,
        pattern: CompiledPattern,
        *,
        statistics: PatternStatistics | None = None,
        strategy: Literal["optimized", "forward"] = "optimized",
    ) -> None:
        if not isinstance(pattern, CompiledPattern):
            raise TypeError("pattern must be a CompiledPattern")
        if strategy not in ("optimized", "forward"):
            raise ValueError(f"unknown pattern matching strategy {strategy!r}")
        self.pattern = pattern
        self.strategy = strategy
        self.statistics = statistics or PatternStatistics()
        self._environment: ExpressionEnvironment | None = None
        self._matches: dict[tuple[int, XdmItem], bool] = {}

    def matches(self, item: XdmItem, environment: ExpressionEnvironment) -> bool:
        """Whether any alternative of the pattern matches *item*."""
        if not isinstance(environment, ExpressionEnvironment):
            raise TypeError("environment must be an ExpressionEnvironment")
        self._select_environment(environment)
        return any(
            self.matches_alternative(alternative.index, item, environment)
            for alternative in self.pattern.alternatives
        )

    def matches_alternative(
        self, index: int, item: XdmItem, environment: ExpressionEnvironment
    ) -> bool:
        """Match one normative union alternative by index."""
        if not isinstance(environment, ExpressionEnvironment):
            raise TypeError("environment must be an ExpressionEnvironment")
        self._select_environment(environment)
        alternative = self.pattern.alternatives[index]
        key = (index, item)
        cached = self._matches.get(key)
        if cached is not None:
            self.statistics.cache_hits += 1
            return cached
        self.statistics.cache_misses += 1
        try:
            result = self._evaluate_alternative(alternative, item, environment)
        except Exception:
            self.statistics.errors_ignored += 1
            result = False
        self._matches[key] = result
        return result

    def _evaluate_alternative(
        self,
        alternative: PatternAlternative,
        item: XdmItem,
        environment: ExpressionEnvironment,
    ) -> bool:
        if alternative.predicate is not None:
            result = _evaluate(alternative.predicate, item, environment, Focus.singleton(item))
            return bool(result)
        if not isinstance(item, XdmNode):
            return False
        path = alternative.path
        assert path is not None
        if self.strategy == "forward" or alternative.requires_forward:
            return self._forward_match(alternative, path, item, environment)
        return self._reverse_intersect_except(path, item, environment)

    def _select_environment(self, environment: ExpressionEnvironment) -> None:
        if environment is self._environment:
            return
        if self._environment is not None:
            self.statistics.cache_invalidations += 1
        self._environment = environment
        self._matches.clear()

    # -- formal whole-tree plan ------------------------------------------------

    def _forward_match(
        self,
        alternative: PatternAlternative,
        path: IntersectExceptExpr,
        item: XdmNode,
        environment: ExpressionEnvironment,
    ) -> bool:
        root = item.root
        self.statistics.equivalent_expression_evaluations += 1
        nodes: dict[XdmNode, None] = {}
        for context_node in (root, *root.descendants):
            for node in _evaluate_intersect_except(
                path,
                context_node,
                environment,
                item,
                self.statistics,
                self.pattern.context.schema,
            ):
                nodes[node] = None
        return item in nodes

    # -- optimized right-to-left plan -----------------------------------------

    def _reverse_intersect_except(
        self,
        expr: IntersectExceptExpr,
        item: XdmNode,
        environment: ExpressionEnvironment,
    ) -> bool:
        if not self._reverse_path(expr.first, item, environment):
            return False
        for operator, path in expr.rest:
            matched = self._reverse_path(path, item, environment)
            if operator == "intersect" and not matched:
                return False
            if operator == "except" and matched:
                return False
        return True

    def _reverse_path(
        self, path: PathExpr, item: XdmNode, environment: ExpressionEnvironment
    ) -> bool:
        if isinstance(path, AbsoluteOnly):
            return item.kind is NodeKind.DOCUMENT and item.parent is None
        if isinstance(path, AbsolutePath):
            root = item.root
            if root.kind is not NodeKind.DOCUMENT:
                return False
            return self._reverse_relative(
                path.relative, item, environment, root, top=False, descendant_entry=False
            )
        if isinstance(path, DescendantPath):
            root = item.root
            if root.kind is not NodeKind.DOCUMENT:
                return False
            return self._reverse_relative(
                path.relative, item, environment, root, top=False, descendant_entry=True
            )
        if isinstance(path, RootedPath):
            return self._reverse_rooted(path, item, environment)
        root = item.root
        return self._reverse_relative(
            path, item, environment, root, top=True, descendant_entry=True
        )

    def _reverse_relative(
        self,
        relative: RelativePath,
        item: XdmNode,
        environment: ExpressionEnvironment,
        start: XdmNode,
        *,
        top: bool,
        descendant_entry: bool,
    ) -> bool:
        steps = relative.steps
        if len(steps) == 1:
            step = steps[0][1]
            if not descendant_entry:
                return self._step_selects(step, item, start, environment, top=top)
            for context_node in self._reverse_contexts(step, item, top=top):
                if _is_descendant_or_self(context_node, start) and (
                    self._step_selects(step, item, context_node, environment, top=top)
                ):
                    return True
            return False
        separator, last = steps[-1]
        prefix = RelativePath(steps[:-1])
        for context_node in self._reverse_contexts(last, item, top=False):
            if not self._step_selects(last, item, context_node, environment, top=False):
                continue
            if separator == "/":
                if self._reverse_relative(
                    prefix,
                    context_node,
                    environment,
                    start,
                    top=top,
                    descendant_entry=descendant_entry,
                ):
                    return True
                continue
            for possible in (context_node, *context_node.ancestors):
                self.statistics.ancestor_steps += 1
                if self._reverse_relative(
                    prefix,
                    possible,
                    environment,
                    start,
                    top=top,
                    descendant_entry=descendant_entry,
                ):
                    return True
        return False

    def _reverse_rooted(
        self, path: RootedPath, item: XdmNode, environment: ExpressionEnvironment
    ) -> bool:
        base = _rooted_base(path.base, item, environment, self.statistics)
        base = self._filter(base, path.predicates, environment, item)
        if path.following is None:
            return any(node is item for node in base)
        assert path.following_separator is not None
        for node in base:
            if self._reverse_relative(
                path.following,
                item,
                environment,
                node,
                top=False,
                descendant_entry=path.following_separator == "//",
            ):
                return True
        return False

    def _reverse_contexts(self, step: StepExpr, item: XdmNode, *, top: bool) -> tuple[XdmNode, ...]:
        if isinstance(step, PostfixStep):  # pragma: no cover - forward plan owns postfix
            return ()
        effective = step.top_axis if top else step.axis
        kind = item.kind
        if effective == "child":
            parent = item.parent
            return () if parent is None or kind not in _ELEMENT_AXIS_KINDS else (parent,)
        if effective == "child-or-top":
            if kind not in _ELEMENT_AXIS_KINDS:
                return ()
            parent = item.parent
            if parent is not None:
                return (parent,)
            return (item,)
        if effective == "attribute":
            parent = item.parent
            return () if parent is None or kind is not NodeKind.ATTRIBUTE else (parent,)
        if effective == "attribute-or-top":
            if kind is not NodeKind.ATTRIBUTE:
                return ()
            parent = item.parent
            return (item,) if parent is None else (parent,)
        if effective == "namespace":
            parent = item.parent
            return () if parent is None or kind is not NodeKind.NAMESPACE else (parent,)
        if effective == "namespace-or-top":
            if kind is not NodeKind.NAMESPACE:
                return ()
            parent = item.parent
            return (item,) if parent is None else (parent,)
        if effective == "self":
            return (item,)
        if effective == "descendant":
            if kind not in _ELEMENT_AXIS_KINDS:
                return ()
            ancestors = item.ancestors
            self.statistics.ancestor_steps += len(ancestors)
            return ancestors
        if kind not in _DESCENDANT_OR_SELF_KINDS:
            return ()
        ancestors = (item, *item.ancestors)
        self.statistics.ancestor_steps += len(ancestors) - 1
        return ancestors

    def _step_selects(
        self,
        step: StepExpr,
        item: XdmNode,
        context_node: XdmNode,
        environment: ExpressionEnvironment,
        *,
        top: bool,
    ) -> bool:
        if isinstance(step, PostfixStep):  # pragma: no cover - forward plan owns postfix
            return False
        nodes = _axis_nodes(step, context_node, top)
        nodes = tuple(
            node
            for node in nodes
            if _node_test_matches(step.node_test, node, step.axis, self.pattern.context.schema)
        )
        nodes = self._filter(nodes, step.predicates, environment, item)
        return any(node is item for node in nodes)

    # -- shared predicate and rooted evaluation -------------------------------

    def _filter(
        self,
        nodes: Sequence[XdmNode],
        predicates: Sequence[CompiledPredicate],
        environment: ExpressionEnvironment,
        candidate: XdmNode,
    ) -> tuple[XdmNode, ...]:
        result = tuple(nodes)
        for predicate in predicates:
            filtered: list[XdmNode] = []
            size = len(result)
            for position, node in enumerate(result, 1):
                self.statistics.predicate_evaluations += 1
                value = _evaluate(
                    predicate.expression,
                    candidate,
                    environment,
                    Focus.singleton(node, position=position, size=size),
                )
                if _predicate_true(value, position):
                    filtered.append(node)
            result = tuple(filtered)
        return result


# ---------------------------------------------------------------------------
# Axis and node-test semantics


def _axis_nodes(step: AxisStep, context_node: XdmNode, top: bool) -> tuple[XdmNode, ...]:
    axis = step.top_axis if top else step.axis
    if axis == "child":
        return context_node.children
    if axis == "child-or-top":
        if context_node.parent is None and context_node.kind in _ELEMENT_AXIS_KINDS:
            return (context_node,)
        return context_node.children
    if axis == "attribute":
        return context_node.attributes
    if axis == "attribute-or-top":
        if context_node.parent is None and context_node.kind is NodeKind.ATTRIBUTE:
            return (context_node,)
        return context_node.attributes
    if axis == "namespace":
        return context_node.namespace_nodes
    if axis == "namespace-or-top":
        if context_node.parent is None and context_node.kind is NodeKind.NAMESPACE:
            return (context_node,)
        return context_node.namespace_nodes
    if axis == "self":
        return (context_node,)
    if axis == "descendant":
        return context_node.descendants
    return (context_node, *context_node.descendants)


def _node_test_matches(
    node_test: NodeTest, node: XdmNode, axis: str, schema: SchemaPool | None
) -> bool:
    if isinstance(node_test, NameTest):
        if node.kind is not _principal_kind_node(axis):
            return False
        return node_test.matches(node)
    item_type = node_test.item_type
    if not _kind_matches(item_type, node, schema):
        return False
    if node_test.declarations:
        return _declaration_matches(node, node_test.declarations, item_type.kind)
    if node_test.child_declarations:
        child = item_type.child
        assert child is not None
        return _declaration_matches(
            cast(XdmDocument, node).root_element,
            node_test.child_declarations,
            child.kind,
        )
    return True


def _kind_matches(item_type: ItemType, node: XdmNode, schema: SchemaPool | None) -> bool:
    kind = item_type.kind
    if kind == "node":
        return True
    if kind == "text":
        return node.kind is NodeKind.TEXT
    if kind == "comment":
        return node.kind is NodeKind.COMMENT
    if kind == "namespace-node":
        return node.kind is NodeKind.NAMESPACE
    if kind == "processing-instruction":
        if node.kind is not NodeKind.PROCESSING_INSTRUCTION:
            return False
        return item_type.target is None or node.target == item_type.target
    if kind in {"element", "schema-element"}:
        if node.kind is not NodeKind.ELEMENT:
            return False
    elif kind in {"attribute", "schema-attribute"}:
        if node.kind is not NodeKind.ATTRIBUTE:
            return False
    elif kind == "document-node":
        if node.kind is not NodeKind.DOCUMENT:
            return False
        if item_type.child is None:
            return True
        return _kind_matches(item_type.child, cast(XdmDocument, node).root_element, schema)
    else:  # pragma: no cover - every node kind is handled above
        return False
    if (
        kind in {"element", "attribute"}
        and item_type.name is not None
        and not item_type.wildcard
        and node.name != item_type.name
    ):
        return False
    if kind in {"element", "attribute"} and item_type.type_name is not None:
        return _type_matches(node, item_type.type_name, item_type.nillable, schema)
    return True


def _declaration_matches(node: XdmNode, declarations: tuple[str, ...], kind: str) -> bool:
    declaration = node.annotation.governing_declaration
    key = _declaration_key(declaration, require_global=kind == "schema-element")
    if key is None or key not in declarations:
        return False
    if kind == "schema-attribute":
        return node.kind is NodeKind.ATTRIBUTE and node.name in declarations
    return node.kind is NodeKind.ELEMENT


def _declaration_key(declaration: Any, *, require_global: bool) -> str | None:
    if declaration is None:
        return None
    name = getattr(declaration, "name", None)
    if not isinstance(name, str) or not name:
        return None
    if name.startswith("{"):
        return name
    if require_global:
        is_global = getattr(declaration, "isGlobalDeclaration", None)
        if callable(is_global) and not is_global():
            return None
    get_namespace = getattr(declaration, "getNamespace", None)
    namespace = get_namespace() if callable(get_namespace) else None
    if namespace is None:
        parent = getattr(declaration, "parent", None)
        namespace = getattr(parent, "target_namespace", None)
    return f"{{{namespace}}}{name}" if namespace else name


def _type_matches(node: XdmNode, type_name: str, nillable: bool, schema: SchemaPool | None) -> bool:
    annotation = node.annotation
    if annotation.nilled and not nillable:
        return False
    actual_name = annotation.type_name or node.type_name
    if actual_name == type_name:
        return True
    actual_class = getattr(annotation, "actual_type", None)
    declared_class = _declared_class(type_name, schema)
    if isinstance(actual_class, type) and isinstance(declared_class, type):
        return issubclass(actual_class, declared_class)
    return _builtin_derives(actual_name, type_name)


def _declared_class(type_name: str, schema: SchemaPool | None) -> type[Any] | None:
    declared = schema.get_type(type_name) if schema is not None else None
    if declared is not None:
        return getattr(declared, "_type_class", None)
    return _builtin_class(type_name)


def _builtin_class(type_name: str) -> type[Any] | None:
    namespace = _namespace_of(type_name)
    local = _local_name(type_name)
    if namespace != XSD_NS:
        return None
    from pyxsd.xdm.annotations import builtin_type_classes

    return builtin_type_classes().get(local)


def _builtin_derives(actual_name: str | None, declared_name: str) -> bool:
    if actual_name is None:
        return False
    actual_local = _local_name(actual_name)
    declared_local = _local_name(declared_name)
    if declared_local == "anyType":
        return True
    if actual_local == declared_local:
        return True
    if declared_local == "anySimpleType":
        return actual_local not in {"anyType", "untyped"}
    if declared_local == "anyAtomicType":
        return actual_local not in {"anyType", "untyped", "anySimpleType"}
    actual_class = _builtin_class(actual_name)
    declared_class = _builtin_class(declared_name)
    if isinstance(actual_class, type) and isinstance(declared_class, type):
        return issubclass(actual_class, declared_class)
    return False


# ---------------------------------------------------------------------------
# Forward evaluation (formal root(.)//EE plan)


def _evaluate_intersect_except(
    expr: IntersectExceptExpr,
    context_node: XdmNode,
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
    schema: SchemaPool | None,
) -> tuple[XdmNode, ...]:
    result = _evaluate_path(expr.first, context_node, environment, candidate, statistics, schema)
    for operator, path in expr.rest:
        other = set(_evaluate_path(path, context_node, environment, candidate, statistics, schema))
        if operator == "intersect":
            result = tuple(node for node in result if node in other)
        else:
            result = tuple(node for node in result if node not in other)
    return result


def _evaluate_union(
    union: UnionExpr,
    context_node: XdmNode,
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
    schema: SchemaPool | None,
) -> tuple[XdmNode, ...]:
    nodes: dict[XdmNode, None] = {}
    for alternative in union.alternatives:
        for node in _evaluate_intersect_except(
            alternative, context_node, environment, candidate, statistics, schema
        ):
            nodes[node] = None
    return tuple(sorted(nodes, key=lambda node: (id(node.root), node.document_order)))


def _evaluate_path(
    path: PathExpr,
    context_node: XdmNode,
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
    schema: SchemaPool | None,
) -> tuple[XdmNode, ...]:
    if isinstance(path, RelativePath):
        return _evaluate_steps(
            path, (context_node,), environment, candidate, statistics, schema, top=True
        )
    if isinstance(path, AbsoluteOnly):
        root = context_node.root
        return (root,) if root.kind is NodeKind.DOCUMENT else ()
    if isinstance(path, AbsolutePath):
        root = context_node.root
        if root.kind is not NodeKind.DOCUMENT:
            return ()
        return _evaluate_steps(
            path.relative,
            (root,),
            environment,
            candidate,
            statistics,
            schema,
            top=False,
        )
    if isinstance(path, DescendantPath):
        root = context_node.root
        if root.kind is not NodeKind.DOCUMENT:
            return ()
        return _evaluate_steps(
            path.relative,
            (root, *root.descendants),
            environment,
            candidate,
            statistics,
            schema,
            top=False,
        )
    base = _rooted_base(path.base, context_node, environment, statistics)
    base = _filter_sequence(base, path.predicates, environment, candidate, statistics)
    if path.following is None:
        return base
    assert path.following_separator is not None
    results: list[XdmNode] = []
    for node in base:
        results.extend(
            _evaluate_steps(
                path.following,
                (node, *node.descendants) if path.following_separator == "//" else (node,),
                environment,
                candidate,
                statistics,
                schema,
                top=False,
            )
        )
    return tuple(results)


def _evaluate_steps(
    relative: RelativePath,
    contexts: tuple[XdmNode, ...],
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
    schema: SchemaPool | None,
    *,
    top: bool,
) -> tuple[XdmNode, ...]:
    current = contexts
    first = True
    for separator, step in relative.steps:
        produced: list[XdmNode] = []
        if first:
            for context_node in current:
                produced.extend(
                    _evaluate_step(
                        step,
                        context_node,
                        environment,
                        candidate,
                        statistics,
                        schema,
                        top=top,
                    )
                )
            first = False
        elif separator == "/":
            for context_node in current:
                produced.extend(
                    _evaluate_step(
                        step,
                        context_node,
                        environment,
                        candidate,
                        statistics,
                        schema,
                        top=False,
                    )
                )
        else:
            for context_node in current:
                for descendant in (context_node, *context_node.descendants):
                    produced.extend(
                        _evaluate_step(
                            step,
                            descendant,
                            environment,
                            candidate,
                            statistics,
                            schema,
                            top=False,
                        )
                    )
        current = tuple(produced)
    return current


def _evaluate_step(
    step: StepExpr,
    context_node: XdmNode,
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
    schema: SchemaPool | None,
    *,
    top: bool,
) -> tuple[XdmNode, ...]:
    if isinstance(step, PostfixStep):
        nodes = _evaluate_union(
            step.union, context_node, environment, candidate, statistics, schema
        )
        return _filter_sequence(nodes, step.predicates, environment, candidate, statistics)
    nodes = tuple(
        node
        for node in _axis_nodes(step, context_node, top)
        if _node_test_matches(step.node_test, node, step.axis, schema)
    )
    return _filter_sequence(nodes, step.predicates, environment, candidate, statistics)


def _filter_sequence(
    nodes: Sequence[XdmNode],
    predicates: Sequence[CompiledPredicate],
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
) -> tuple[XdmNode, ...]:
    result = tuple(nodes)
    for predicate in predicates:
        filtered: list[XdmNode] = []
        size = len(result)
        for position, node in enumerate(result, 1):
            statistics.predicate_evaluations += 1
            value = _evaluate(
                predicate.expression,
                candidate,
                environment,
                Focus.singleton(node, position=position, size=size),
            )
            if _predicate_true(value, position):
                filtered.append(node)
        result = tuple(filtered)
    return result


# ---------------------------------------------------------------------------
# Rooted bases


def _rooted_base(
    base: RootedBase,
    focus: XdmNode,
    environment: ExpressionEnvironment,
    statistics: PatternStatistics,
) -> tuple[XdmNode, ...]:
    if isinstance(base, VariableBase):
        value = environment.variables.get(base.name)
        if value is None:
            return ()
        return tuple(item for item in as_xdm(value) if isinstance(item, XdmNode))
    statistics.function_evaluations += 1
    local = base.local
    if local == "root":
        return (focus.root,)
    arguments = [_argument_value(argument, environment) for argument in base.arguments]
    if local == "doc":
        uri = _singleton_string(arguments[0])
        document = _document_for_uri(uri, environment, focus)
        return () if document is None else (document,)
    if local in {"id", "element-with-id"}:
        tokens = _id_tokens(arguments[0])
        root = focus.root
        if len(arguments) > 1:
            node = _singleton_node(arguments[1])
            if node is None:
                return ()
            root = node.root
        return _id_nodes(root, tokens)
    nodes = _key_nodes(arguments, environment)
    if len(arguments) == 2:
        root = focus.root
        nodes = tuple(node for node in nodes if node.root is root)
    return nodes


def _argument_value(argument: FunctionArgument, environment: ExpressionEnvironment) -> XdmSequence:
    if argument.kind == "literal":
        return argument.value
    assert argument.name is not None
    value = environment.variables.get(argument.name)
    if value is None:
        return ()
    return as_xdm(value)


def _singleton_string(sequence: XdmSequence) -> str:
    if len(sequence) != 1:
        raise ValueError("a rooted function argument requires one string value")
    item = sequence[0]
    if isinstance(item, XdmAtomic):
        return item.lexical
    if isinstance(item, XdmNode):
        return item.string_value
    raise TypeError("a rooted function argument is not string-valued")


def _singleton_node(sequence: XdmSequence) -> XdmNode | None:
    if len(sequence) != 1 or not isinstance(sequence[0], XdmNode):
        return None
    return sequence[0]


def _id_tokens(sequence: XdmSequence) -> frozenset[str]:
    tokens: set[str] = set()
    for item in sequence:
        if isinstance(item, XdmAtomic):
            text = item.lexical
        elif isinstance(item, XdmNode):
            text = item.string_value
        else:
            raise TypeError("an id argument is not string-valued")
        tokens.update(token for token in text.split() if token)
    return frozenset(tokens)


def _document_for_uri(
    uri: str, environment: ExpressionEnvironment, focus: XdmNode
) -> XdmDocument | None:
    candidates = [uri]
    base = environment.base_uri or focus.base_uri
    if base:
        candidates.append(urljoin(base, uri))
    for candidate in candidates:
        document = environment.documents.get(candidate)
        if document is not None:
            return document
    for document in environment.documents.values():
        if document.document_uri in candidates:
            return document
    return None


def _id_nodes(root: XdmNode, tokens: frozenset[str]) -> tuple[XdmNode, ...]:
    if not tokens:
        return ()
    found: dict[XdmNode, None] = {}
    for node in (root, *root.descendants):
        if node.kind is not NodeKind.ELEMENT:
            continue
        if (
            getattr(node.annotation, "id_kind", None) in {"ID", "id"}
            and node.string_value in tokens
        ):
            found[node] = None
        for attribute in node.attributes:
            if (
                getattr(attribute.annotation, "id_kind", None) in {"ID", "id"}
                and attribute.string_value in tokens
            ):
                found[node] = None
    return tuple(sorted(found, key=lambda node: node.document_order))


def _key_nodes(
    arguments: Sequence[XdmSequence], environment: ExpressionEnvironment
) -> tuple[XdmNode, ...]:
    name = _singleton_string(arguments[0])
    index = environment.key_indexes.get(name)
    if index is None:
        return ()
    nodes: dict[XdmNode, None] = {}
    for value in arguments[1]:
        if isinstance(value, XdmAtomic):
            keys: tuple[Any, ...] = (value.lexical, value.value, value)
        elif isinstance(value, XdmNode):
            keys = (value.string_value,)
        else:
            continue
        for key in keys:
            for node in index.get(key, ()):
                nodes[node] = None
    top: XdmNode | None = None
    if len(arguments) > 2:
        top = _singleton_node(arguments[2])
    ordered = sorted(nodes, key=lambda node: (id(node.root), node.document_order))
    if top is not None:
        ordered = [node for node in ordered if node.root is top.root]
    return tuple(ordered)


# ---------------------------------------------------------------------------
# Predicate evaluation


def _evaluate(
    expression: CompiledExpression,
    candidate: XdmItem,
    environment: ExpressionEnvironment,
    focus: Focus,
) -> XdmSequence:
    variables = dict(environment.variables)
    variables[_CURRENT_VARIABLE] = (candidate,)
    local = ExpressionEnvironment(
        variables=variables,
        functions=environment.functions,
        key_indexes=environment.key_indexes,
        documents=environment.documents,
        collections=environment.collections,
        default_collection=environment.default_collection,
        text_resources=environment.text_resources,
        current_datetime=environment.current_datetime,
        implicit_timezone=environment.implicit_timezone,
        base_uri=environment.base_uri,
    )
    return XPathEngine(expression.version).evaluate(expression, local, focus=focus)


def _predicate_true(value: XdmSequence, position: int) -> bool:
    if len(value) == 1 and isinstance(value[0], XdmAtomic):
        atomic = value[0]
        if _local_name(atomic.type_name) in _NUMERIC_LOCALS:
            return _numeric_equals_position(atomic.value, position)
    return _effective_boolean_value(value)


def _numeric_equals_position(value: Any, position: int) -> bool:
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(value == position)


def _effective_boolean_value(value: XdmSequence) -> bool:
    if not value:
        return False
    first = value[0]
    if isinstance(first, XdmNode):
        return True
    if len(value) != 1 or not isinstance(first, XdmAtomic):
        raise TypeError("a predicate does not have an effective boolean value")
    local = _local_name(first.type_name)
    if local == "boolean":
        return bool(first.value)
    if local in _NUMERIC_LOCALS:
        if isinstance(first.value, float) and math.isnan(first.value):
            return False
        return first.value != 0
    if local in _STRING_LOCALS:
        return bool(str(first.value))
    raise TypeError("a predicate does not have an effective boolean value")


def _is_descendant_or_self(node: XdmNode, start: XdmNode) -> bool:
    if node is start:
        return True
    if node.root is not start.root:
        return False
    return node.document_order > start.document_order


# ---------------------------------------------------------------------------
# Public compilation and priority helpers


def compile_pattern(
    source: str,
    context: StaticContext,
    *,
    location: IssueLocation | None = None,
) -> CompiledPattern:
    """Parse one Pattern30 in *context* and freeze its normative alternatives."""
    if not isinstance(context, StaticContext):
        raise TypeError("context must be a StaticContext")
    if not isinstance(source, str):
        raise TypeError("a pattern source must be a string")
    if not source.strip():
        raise _static_error(context, "a pattern must not be empty", "XTSE0340", location=location)
    tokens = _Scanner(source, context, location).scan()
    alternatives = _Parser(source, context, location, tokens).parse()
    return CompiledPattern(source, context, alternatives)


def default_priority(
    pattern: CompiledPattern | str, context: StaticContext | None = None
) -> Decimal:
    """Return the highest default priority of *pattern*'s union alternatives."""
    if isinstance(pattern, str):
        if context is None:
            context = StaticContext()
        pattern = compile_pattern(pattern, context)
    elif not isinstance(pattern, CompiledPattern):
        raise TypeError("pattern must be a CompiledPattern or a string")
    return max(pattern.default_priorities)
