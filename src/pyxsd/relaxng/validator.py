"""Clark text, datatype, and list derivatives for RELAX NG validation.

The simplified pattern graph from Phase 1 carries every semantic constructor
except the derivative algorithm's internal ``After`` continuation, which is
defined here as :class:`_After`.  ``After p1 p2`` means "match ``p1``, then an
end-tag, then ``p2``".  It is residual state only: it never appears in an
authored or public pattern, and this module does not add it to the Phase 1
IR.

A text derivative takes one text node's exact lexical value, so its result can
depend on that value, the in-scope datatype context (base URI and prefix
mapping), the pattern's datatype identity, and any residual continuation.
Those results therefore live in parse-local tables supplied by
:class:`DerivationContext`, never in the schema's immutable interning pool.
``DerivationContext.pool`` is retained as the schema-side graph reference for
later element/attribute event derivatives; it is deliberately left untouched
by text and list derivative work.

RELAX NG whitespace is exactly space, tab, carriage return, and line feed.
:func:`xml_whitespace_tokens` scans those four characters directly and keeps
every token's exact start and end offsets for later list interpretations.
Python's Unicode-aware splitting is never used.

Budget accounting marks two distinct kinds of parse work: each distinct
semantic residual and each retained memo entry.  Exhausting either budget
raises :class:`~pyxsd.relaxng.errors.RelaxNGLimitError`; it is never reported
as an invalid document.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple, cast

from pyxsd.relaxng.datatypes import Datatype, DatatypeContext
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
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
    nullable,
    one_or_more,
)
from pyxsd.relaxng.whitespace import XML_WHITESPACE

__all__ = [
    "DerivationContext",
    "TokenSpan",
    "list_derivative",
    "nullable",
    "text_derivative",
    "xml_whitespace_tokens",
]

#: The schema-side datatype context a compiled datatype without a retained
#: context is compared in.  The Phase 1 ``Value`` contract keeps the real
#: context on the compiled datatype (``ScalarDatatype.context``).
_EMPTY_DATATYPE_CONTEXT = DatatypeContext(None, {})


class TokenSpan(NamedTuple):
    """One XML-whitespace-delimited list token and its exact source offsets.

    ``start`` is inclusive and ``end`` is exclusive, so ``lexical[start:end]``
    is ``text``.  Offsets are semantic evidence for later list interpretations;
    they do not participate in derivative memo keys.
    """

    text: str
    start: int
    end: int


@dataclass(frozen=True, eq=False, repr=False)
class _After(Pattern):
    """Internal residual: match ``first``, then an end-tag, then ``second``.

    The continuation is residual state, not an authored RELAX NG pattern.  It
    has deterministic structural equality and hashing so the smart
    constructors and parse-local memo tables can compare equivalent residuals.
    Nullability is defined by Clark's algorithm to be false, but the algorithm
    never asks the Phase 1 nullability function about an ``After`` node: its
    invariants keep ``After`` out of ``Group`` and repetition operands until an
    end-tag derivative removes it.
    """

    first: Pattern
    second: Pattern

    def __hash__(self) -> int:
        cached = self.__dict__.get("_after_hash")
        if cached is not None:
            return cached
        # Hash long continuation chains bottom-up so a deeply nested stack of
        # end-tag continuations does not recurse through this method.
        spine: list[_After] = []
        node: Pattern = self
        while isinstance(node, _After) and "_after_hash" not in node.__dict__:
            spine.append(node)
            node = node.second
        value = node.__dict__["_after_hash"] if isinstance(node, _After) else hash(node)
        for item in reversed(spine):
            value = hash((item.first, value))
            object.__setattr__(item, "_after_hash", value)
        return self.__dict__["_after_hash"]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _After):
            return NotImplemented
        left: Pattern = self
        right: Pattern = other
        while True:
            if left is right:
                return True
            if not isinstance(left, _After) or not isinstance(right, _After):
                return left == right
            if left.first != right.first:
                return False
            left, right = left.second, right.second

    def __repr__(self) -> str:
        parts: list[str] = []
        pending: list[str | Pattern] = [self]
        while pending:
            value = pending.pop()
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, _After):
                parts.append("After(first=")
                pending.append(")")
                pending.append(value.second)
                pending.append(", second=")
                pending.append(value.first)
            else:
                parts.append(repr(value))
        return "".join(parts)


def _after(first: Pattern, second: Pattern) -> Pattern:
    """Build Clark's ``after`` continuation with its identity reduction."""
    if isinstance(first, NotAllowed) or isinstance(second, NotAllowed):
        return NotAllowed()
    return _After(first, second)


@dataclass(eq=False)
class DerivationContext:
    """One parse's derivative inputs, parse-local memo tables, and budgets.

    ``datatype_context`` is the instance-side context for every lexical value
    in this parse.  ``pool`` is the schema-side semantic graph; residual
    construction never interns into it because derivative results depend on
    instance text and namespace context.  ``limits`` supplies the optional
    derivative-state and memo-entry bounds, or ``None`` for unlimited work.

    The tables are semantic interning tables as well as memo tables: a
    repeated derivative returns the one canonical residual node, which keeps
    later evidence comparison stable without touching the schema graph.
    """

    datatype_context: DatatypeContext
    pool: PatternPool
    limits: RelaxNGLimits | None = None
    _counter: BudgetCounter = field(init=False, repr=False, compare=False)
    _residuals: dict[Pattern, Pattern] = field(init=False, repr=False, compare=False)
    _text_memo: dict[tuple[Pattern, str, DatatypeContext], Pattern] = field(
        init=False, repr=False, compare=False
    )
    _list_memo: dict[tuple[Pattern, tuple[str, ...], DatatypeContext], Pattern] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.datatype_context, DatatypeContext):
            raise TypeError("datatype_context must be a DatatypeContext")
        if not isinstance(self.pool, PatternPool):
            raise TypeError("pool must be a PatternPool")
        if self.limits is not None and not isinstance(self.limits, RelaxNGLimits):
            raise TypeError("limits must be a RelaxNGLimits value or None")
        self._counter = BudgetCounter("validate", self.limits)
        self._residuals = {}
        self._text_memo = {}
        self._list_memo = {}

    def canonical_residual(self, pattern: Pattern) -> Pattern:
        """Return the canonical parse-local node for *pattern*.

        A newly observed semantic residual consumes one
        ``max_derivative_states`` unit; revisiting an equivalent one consumes
        nothing.
        """
        canonical = self._residuals.get(pattern)
        if canonical is not None:
            return canonical
        self._counter.consume("max_derivative_states")
        self._residuals[pattern] = pattern
        return pattern

    def remember_text(
        self,
        key: tuple[Pattern, str, DatatypeContext],
        result: Pattern,
    ) -> None:
        """Retain one text-derivative memo entry."""
        self._counter.consume("max_memo_entries")
        self._text_memo[key] = result

    def remember_list(
        self,
        key: tuple[Pattern, tuple[str, ...], DatatypeContext],
        result: Pattern,
    ) -> None:
        """Retain one list-derivative memo entry."""
        self._counter.consume("max_memo_entries")
        self._list_memo[key] = result


def xml_whitespace_tokens(lexical: str) -> tuple[TokenSpan, ...]:
    """Split *lexical* into tokens on XML whitespace only.

    Each returned :class:`TokenSpan` keeps the exact half-open offsets of the
    token in *lexical*.  Space, tab, carriage return, and line feed are the
    only separators; Unicode whitespace such as U+00A0 or U+2028 remains token
    content.
    """
    if not isinstance(lexical, str):
        raise TypeError("list lexical text must be a string")
    tokens: list[TokenSpan] = []
    start = -1
    for index, character in enumerate(lexical):
        if character in XML_WHITESPACE:
            if start >= 0:
                tokens.append(TokenSpan(lexical[start:index], start, index))
                start = -1
        elif start < 0:
            start = index
    if start >= 0:
        tokens.append(TokenSpan(lexical[start:], start, len(lexical)))
    return tuple(tokens)


def text_derivative(pattern: Pattern, lexical: str, context: DerivationContext) -> Pattern:
    """Return the residual of *pattern* after one text node.

    Results are memoized in *context* by the pattern (which carries datatype
    identity and residual continuations), the exact lexical text, and the
    in-scope datatype context.
    """
    if not isinstance(lexical, str):
        raise TypeError("derivative lexical text must be a string")
    key = (pattern, lexical, context.datatype_context)
    cached = context._text_memo.get(key)
    if cached is not None:
        return cached
    result = context.canonical_residual(_text_derivative(pattern, lexical, context))
    context.remember_text(key, result)
    return result


def list_derivative(
    pattern: Pattern,
    tokens: Iterable[TokenSpan],
    context: DerivationContext,
) -> Pattern:
    """Return the residual of *pattern* after a sequence of list tokens.

    Token offsets are retained by :func:`xml_whitespace_tokens` for later
    evidence but do not affect derivative semantics, so the memo key is the
    sequence of token texts.  An empty sequence is the original pattern and
    consumes no derivative work.
    """
    token_texts: list[str] = []
    for token in tokens:
        if not isinstance(token, TokenSpan):
            raise TypeError("list derivative tokens must be TokenSpan values")
        if not isinstance(token.text, str):
            raise TypeError("a list token's text must be a string")
        token_texts.append(token.text)
    if not token_texts:
        return pattern
    key = (pattern, tuple(token_texts), context.datatype_context)
    cached = context._list_memo.get(key)
    if cached is not None:
        return cached
    result: Pattern = pattern
    for token_text in token_texts:
        result = text_derivative(result, token_text, context)
    result = context.canonical_residual(result)
    context.remember_list(key, result)
    return result


def _text_derivative(
    pattern: Pattern,
    lexical: str,
    context: DerivationContext,
) -> Pattern:
    """Dispatch one text derivative; callers memoize the result."""
    if isinstance(pattern, _After):
        return _after(text_derivative(pattern.first, lexical, context), pattern.second)
    if isinstance(pattern, Ref):
        return text_derivative(pattern.target, lexical, context)
    if isinstance(pattern, Choice):
        return choice(
            text_derivative(pattern.first, lexical, context),
            text_derivative(pattern.second, lexical, context),
        )
    if isinstance(pattern, Interleave):
        return choice(
            interleave(text_derivative(pattern.first, lexical, context), pattern.second),
            interleave(pattern.first, text_derivative(pattern.second, lexical, context)),
        )
    if isinstance(pattern, Group):
        first = text_derivative(pattern.first, lexical, context)
        result = group(first, pattern.second)
        if nullable(pattern.first):
            return choice(result, text_derivative(pattern.second, lexical, context))
        return result
    if isinstance(pattern, OneOrMore):
        return group(
            text_derivative(pattern.pattern, lexical, context),
            choice(one_or_more(pattern.pattern), Empty()),
        )
    if isinstance(pattern, Text):
        return Text()
    if isinstance(pattern, (Empty, NotAllowed, Attribute, Element)):
        return NotAllowed()
    if isinstance(pattern, List):
        tokens = xml_whitespace_tokens(lexical)
        return (
            Empty() if nullable(list_derivative(pattern.pattern, tokens, context)) else NotAllowed()
        )
    if isinstance(pattern, Data):
        datatype = cast(Datatype, pattern.datatype)
        return Empty() if datatype.allows(lexical, context.datatype_context) else NotAllowed()
    if isinstance(pattern, DataExcept):
        datatype = cast(Datatype, pattern.datatype)
        if not datatype.allows(lexical, context.datatype_context):
            return NotAllowed()
        excluded = text_derivative(pattern.excluded, lexical, context)
        return NotAllowed() if nullable(excluded) else Empty()
    if isinstance(pattern, Value):
        datatype = cast(Datatype, pattern.datatype)
        schema_context = _schema_datatype_context(datatype)
        if datatype.equal(
            pattern.lexical,
            schema_context,
            lexical,
            context.datatype_context,
        ):
            return Empty()
        return NotAllowed()
    raise TypeError(f"unknown RELAX NG pattern {type(pattern).__name__}")


def _schema_datatype_context(datatype: Datatype) -> DatatypeContext:
    """Return the schema-side context retained by a compiled datatype.

    Phase 1's compiled datatypes keep the schema-side :class:`DatatypeContext`
    in a ``context`` attribute; ``ScalarDatatype`` does so for every library it
    implements.  A context-independent custom datatype may omit it, in which
    case an empty context is the correct comparison input.
    """
    context = getattr(datatype, "context", None)
    if isinstance(context, DatatypeContext):
        return context
    return _EMPTY_DATATYPE_CONTEXT
