"""Clark event, text, datatype, and list derivatives for RELAX NG validation.

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
``DerivationContext.pool`` is retained as the schema-side graph reference; all
event derivatives deliberately leave it untouched. Start-tag transitions keep
``After`` outside group, interleave, and repetition operands. Event, text, and
recovery projections run on explicit postorder stacks, so pattern depth never
consumes Python call depth; a dependency cycle without an element boundary is
rejected deterministically instead of recursing. An iterative lexical-tree walk
drives document events without recursing on document depth.

RELAX NG whitespace is exactly space, tab, carriage return, and line feed.
:func:`xml_whitespace_tokens` scans those four characters directly and keeps
every token's exact start and end offsets for later list interpretations.
Python's Unicode-aware splitting is never used.

Budget accounting marks two distinct kinds of parse work: each distinct
semantic residual and each retained memo entry.  Exhausting either budget
raises :class:`~pyxsd.relaxng.errors.RelaxNGLimitError`; it is never reported
as an invalid document.  :func:`validate_element` is the fail-fast contract;
:func:`validate_element_recovering` adds deterministic diagnostic recovery,
where every recovered state is nonaccepting evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Final, NamedTuple, cast
from xml.etree import ElementTree as ET

from pyxsd.namespaces import XML_NS
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
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.whitespace import XML_WHITESPACE

__all__ = [
    "RECOVERY_CONSUMED_ATTRIBUTE",
    "RECOVERY_CONTINUED_CONTENT",
    "RECOVERY_IGNORED_ATTRIBUTE",
    "RECOVERY_IGNORED_SUBTREE",
    "RECOVERY_MISSING_ATTRIBUTE",
    "RECOVERY_MISSING_CONTENT",
    "DerivationContext",
    "RecoveryOutcome",
    "TokenSpan",
    "ValidationMismatch",
    "ValidationOutcome",
    "attribute_derivative",
    "children_derivative",
    "end_tag_derivative",
    "expanded_name",
    "list_derivative",
    "nullable",
    "start_tag_close_derivative",
    "start_tag_open_derivative",
    "text_derivative",
    "validate_element",
    "validate_element_recovering",
    "xml_whitespace_tokens",
]

#: An unexpected element occurrence was reported and its subtree ignored.
RECOVERY_IGNORED_SUBTREE: Final = "ignored-subtree"
#: An attribute occurrence was rejected and was not declared in the residual.
RECOVERY_IGNORED_ATTRIBUTE: Final = "ignored-attribute"
#: A declared attribute occurrence was rejected and its requirement consumed.
RECOVERY_CONSUMED_ATTRIBUTE: Final = "consumed-attribute"
#: A still-required attribute was reported and removed before content continued.
RECOVERY_MISSING_ATTRIBUTE: Final = "missing-attribute"
#: Character data was reported and content continuation retained.
RECOVERY_CONTINUED_CONTENT: Final = "continued-content"
#: Required content was reported missing and its continuation was popped.
RECOVERY_MISSING_CONTENT: Final = "missing-content"

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


def _nullable(pattern: Pattern) -> bool:
    """Guard the residual boundary; schema nullability still owns its fixed point.

    Clark's placement invariant keeps ``After`` out of the operands passed to
    schema nullability. Only a raw continuation needs this extra case.
    """
    return False if isinstance(pattern, _After) else nullable(pattern)


@dataclass(eq=False)
class DerivationContext:
    """One parse's derivative inputs, parse-local memo tables, and budgets.

    ``datatype_context`` is the current occurrence's instance-side context.
    The tree walker switches it for each event and restores it on exit.
    ``pool`` is the schema-side semantic graph; residual
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
    _event_memo: dict[tuple[str, Pattern, str, str, DatatypeContext | None], Pattern] = field(
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
        self._event_memo = {}

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
    """Dispatch text derivatives iteratively over pattern depth."""
    return _derive_text_iterative(pattern, lexical, context)


def _text_derivation_children(pattern: Pattern) -> tuple[Pattern, ...]:
    """Return the operands whose text derivative this node needs.

    ``Group`` derives its second operand only when the first is nullable;
    scheduling it unconditionally would consume memo and residual budget for
    work the recursive rule never performs.
    """
    if isinstance(pattern, _After):
        return (pattern.first,)
    if isinstance(pattern, Ref):
        return (pattern.target,)
    if isinstance(pattern, Choice):
        return (pattern.first, pattern.second)
    if isinstance(pattern, Interleave):
        return (pattern.first, pattern.second)
    if isinstance(pattern, Group):
        return (pattern.first, pattern.second) if _nullable(pattern.first) else (pattern.first,)
    if isinstance(pattern, OneOrMore):
        return (pattern.pattern,)
    if isinstance(pattern, DataExcept):
        return (pattern.excluded,)
    return ()


def _derive_text_iterative(
    pattern: Pattern,
    lexical: str,
    context: DerivationContext,
) -> Pattern:
    """Compute the text derivative without a Python frame per pattern node.

    Children are evaluated before their parents, so memo entries and semantic
    residuals are recorded in the same order as the recursive Clark rules.
    A dependency cycle with no element boundary is not a legal simplified
    RELAX NG graph and is rejected deterministically instead of recursing.
    """
    results: dict[int, Pattern] = {}
    active: set[int] = set()
    pending: list[tuple[Pattern, bool]] = [(pattern, False)]
    while pending:
        node, ready = pending.pop()
        key = id(node)
        if not ready:
            if key in results:
                continue
            if node is not pattern:
                memo_key = (node, lexical, context.datatype_context)
                cached = context._text_memo.get(memo_key)
                if cached is not None:
                    results[key] = cached
                    continue
            active.add(key)
            pending.append((node, True))
            for child in reversed(_text_derivation_children(node)):
                child_key = id(child)
                if child_key in results:
                    continue
                if child_key in active:
                    raise TypeError("cyclic RELAX NG text derivative has no element boundary")
                pending.append((child, False))
            continue
        active.discard(key)
        raw = _text_derivative_node(node, lexical, context, results)
        if node is pattern:
            results[key] = raw
            continue
        result = context.canonical_residual(raw)
        context.remember_text((node, lexical, context.datatype_context), result)
        results[key] = result
    return results[id(pattern)]


def _text_derivative_node(
    pattern: Pattern,
    lexical: str,
    context: DerivationContext,
    results: dict[int, Pattern],
) -> Pattern:
    """Compute one node's text derivative from its already-derived operands."""
    if isinstance(pattern, _After):
        return _after(results[id(pattern.first)], pattern.second)
    if isinstance(pattern, Ref):
        return results[id(pattern.target)]
    if isinstance(pattern, Choice):
        return choice(results[id(pattern.first)], results[id(pattern.second)])
    if isinstance(pattern, Interleave):
        return choice(
            interleave(results[id(pattern.first)], pattern.second),
            interleave(pattern.first, results[id(pattern.second)]),
        )
    if isinstance(pattern, Group):
        first = results[id(pattern.first)]
        result = group(first, pattern.second)
        if _nullable(pattern.first):
            return choice(result, results[id(pattern.second)])
        return result
    if isinstance(pattern, OneOrMore):
        return group(
            results[id(pattern.pattern)],
            choice(one_or_more(pattern.pattern), Empty()),
        )
    if isinstance(pattern, Text):
        return Text()
    if isinstance(pattern, (Empty, NotAllowed, Attribute, Element)):
        return NotAllowed()
    if isinstance(pattern, List):
        tokens = xml_whitespace_tokens(lexical)
        return (
            Empty()
            if _nullable(list_derivative(pattern.pattern, tokens, context))
            else NotAllowed()
        )
    if isinstance(pattern, Data):
        datatype = cast(Datatype, pattern.datatype)
        return Empty() if datatype.allows(lexical, context.datatype_context) else NotAllowed()
    if isinstance(pattern, DataExcept):
        datatype = cast(Datatype, pattern.datatype)
        if not datatype.allows(lexical, context.datatype_context):
            return NotAllowed()
        return NotAllowed() if _nullable(results[id(pattern.excluded)]) else Empty()
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


def _apply_after(function: Callable[[Pattern], Pattern], pattern: Pattern) -> Pattern:
    """Push a continuation operation into each top-level ``After`` leaf.

    The traversal is iterative so a deeply nested continuation tree cannot
    exhaust Python's call stack.  ``After`` and ``Choice`` are the only
    admitted constructors; anything else violates the start-tag placement
    invariant and is rejected exactly as before.
    """
    results: dict[int, Pattern] = {}
    pending: list[tuple[Pattern, bool]] = [(pattern, False)]
    while pending:
        node, ready = pending.pop()
        key = id(node)
        if key in results:
            continue
        if not ready:
            if isinstance(node, _After):
                children: tuple[Pattern, ...] = ()
            elif isinstance(node, Choice):
                children = (node.first, node.second)
            elif isinstance(node, NotAllowed):
                children = ()
            else:
                raise TypeError("start-tag derivative violated the After placement invariant")
            pending.append((node, True))
            for child in reversed(children):
                pending.append((child, False))
            continue
        if isinstance(node, _After):
            results[key] = _after(node.first, function(node.second))
        elif isinstance(node, Choice):
            results[key] = choice(
                results[id(node.first)],
                results[id(node.second)],
            )
        else:
            results[key] = NotAllowed()
    return results[id(pattern)]


def start_tag_open_derivative(
    pattern: Pattern, expanded_name: str, context: DerivationContext | None = None
) -> Pattern:
    """Derive an element start, pushing surrounding operators into continuations."""
    return _event_derivative("open", pattern, expanded_name, "", context)


def attribute_derivative(
    pattern: Pattern, expanded_name: str, lexical: str, context: DerivationContext
) -> Pattern:
    """Consume one attribute, retaining every unordered assignment and weak match."""
    return _event_derivative("attribute", pattern, expanded_name, lexical, context)


def start_tag_close_derivative(
    pattern: Pattern, context: DerivationContext | None = None
) -> Pattern:
    """Reject still-required attributes, without touching nested element content."""
    return _event_derivative("close", pattern, "", "", context)


def end_tag_derivative(pattern: Pattern, context: DerivationContext | None = None) -> Pattern:
    """Pop every continuation whose content matches the empty sequence."""
    return _event_derivative("end", pattern, "", "", context)


def _event_derivative(
    event: str,
    pattern: Pattern,
    name: str,
    lexical: str,
    context: DerivationContext | None,
) -> Pattern:
    """Memoize event work using the text/list context's two budgets.

    The traversal is iterative: an authored or simplified pattern may be much
    deeper than Python's call stack allows.  Children are finalised before
    their parent, preserving the recursive rule's residual and memo order.
    """
    if context is not None:
        key = _event_memo_key(event, pattern, name, lexical, context)
        cached = context._event_memo.get(key)
        if cached is not None:
            return cached
    return _derive_event_iterative(event, pattern, name, lexical, context)


def _event_memo_key(
    event: str,
    pattern: Pattern,
    name: str,
    lexical: str,
    context: DerivationContext,
) -> tuple[str, Pattern, str, str, DatatypeContext | None]:
    """Return the complete parse-local memo key for one event derivative."""
    return (
        event,
        pattern,
        name,
        lexical,
        context.datatype_context if event == "attribute" else None,
    )


def _event_derivation_children(event: str, pattern: Pattern) -> tuple[Pattern, ...]:
    """Return event operands in the order the Clark rules derive them."""
    if isinstance(pattern, Ref):
        return (pattern.target,)
    if isinstance(pattern, Choice):
        return (pattern.first, pattern.second)
    if event == "end":
        return ()
    if isinstance(pattern, _After):
        return (pattern.first,)
    if event == "close":
        if isinstance(pattern, (Group, Interleave)):
            return (pattern.first, pattern.second)
        if isinstance(pattern, OneOrMore):
            return (pattern.pattern,)
        return ()
    if isinstance(pattern, (Group, Interleave)):
        if event == "attribute":
            return (pattern.first, pattern.second)
        if isinstance(pattern, Interleave):
            return (pattern.first, pattern.second)
        return (pattern.first, pattern.second) if _nullable(pattern.first) else (pattern.first,)
    if isinstance(pattern, OneOrMore):
        return (pattern.pattern,)
    return ()


def _derive_event_iterative(
    event: str,
    pattern: Pattern,
    name: str,
    lexical: str,
    context: DerivationContext | None,
) -> Pattern:
    """Compute one event derivative over an explicit postorder stack."""
    results: dict[int, Pattern] = {}
    active: set[int] = set()
    pending: list[tuple[Pattern, bool]] = [(pattern, False)]
    while pending:
        node, ready = pending.pop()
        key = id(node)
        if not ready:
            if key in results:
                continue
            if context is not None:
                cached = context._event_memo.get(
                    _event_memo_key(event, node, name, lexical, context)
                )
                if cached is not None:
                    results[key] = cached
                    continue
            active.add(key)
            pending.append((node, True))
            for child in reversed(_event_derivation_children(event, node)):
                child_key = id(child)
                if child_key in results:
                    continue
                if child_key in active:
                    raise TypeError("cyclic RELAX NG event derivative has no element boundary")
                pending.append((child, False))
            continue
        active.discard(key)
        raw = _event_derivative_node(event, node, name, lexical, context, results)
        if context is None:
            results[key] = raw
            continue
        result = context.canonical_residual(raw)
        context._counter.consume("max_memo_entries")
        context._event_memo[_event_memo_key(event, node, name, lexical, context)] = result
        results[key] = result
    return results[id(pattern)]


def _event_derivative_node(
    event: str,
    pattern: Pattern,
    name: str,
    lexical: str,
    context: DerivationContext | None,
    results: dict[int, Pattern],
) -> Pattern:
    """Compute one event derivative from its already-derived operands."""
    if isinstance(pattern, Ref):
        return results[id(pattern.target)]
    if isinstance(pattern, Choice):
        return choice(results[id(pattern.first)], results[id(pattern.second)])
    if event == "end":
        if isinstance(pattern, _After) and _nullable(pattern.first):
            return pattern.second
        return NotAllowed()
    if isinstance(pattern, _After):
        if event == "open":
            return _apply_after(
                lambda rest: _after(rest, pattern.second),
                results[id(pattern.first)],
            )
        return _after(results[id(pattern.first)], pattern.second)
    if event == "close":
        if isinstance(pattern, Group):
            return group(results[id(pattern.first)], results[id(pattern.second)])
        if isinstance(pattern, Interleave):
            return interleave(results[id(pattern.first)], results[id(pattern.second)])
        if isinstance(pattern, OneOrMore):
            return one_or_more(results[id(pattern.pattern)])
        return NotAllowed() if isinstance(pattern, Attribute) else pattern
    if isinstance(pattern, (Group, Interleave)):
        first, second = pattern.first, pattern.second
        constructor = group if isinstance(pattern, Group) else interleave
        if event == "attribute":
            return choice(
                constructor(results[id(first)], second),
                constructor(first, results[id(second)]),
            )
        left = _apply_after(
            lambda rest: constructor(rest, second),
            results[id(first)],
        )
        if isinstance(pattern, Interleave):
            return choice(
                left,
                _apply_after(
                    lambda rest: interleave(first, rest),
                    results[id(second)],
                ),
            )
        if _nullable(first):
            return choice(left, results[id(second)])
        return left
    if isinstance(pattern, OneOrMore):
        rest = choice(one_or_more(pattern.pattern), Empty())
        derived = results[id(pattern.pattern)]
        if event == "open":
            return _apply_after(lambda tail: group(tail, rest), derived)
        return group(derived, rest)
    if event == "open" and isinstance(pattern, Element):
        uri, local = expanded_name(name)
        return (
            _after(pattern.pattern, Empty())
            if pattern.name_class.contains(uri, local)
            else NotAllowed()
        )
    if event == "attribute" and isinstance(pattern, Attribute):
        uri, local = expanded_name(name)
        if pattern.name_class.contains(uri, local):
            cx = cast(DerivationContext, context)
            if (_nullable(pattern.pattern) and _whitespace(lexical)) or _nullable(
                text_derivative(pattern.pattern, lexical, cx)
            ):
                return Empty()
    return NotAllowed()


def expanded_name(name: str) -> tuple[str | None, str]:
    """Split an expanded Clark event name into its namespace URI and local part."""
    if name.startswith("{"):
        uri, local = name[1:].split("}", 1)
        return uri, local
    return None, name


def _whitespace(lexical: str) -> bool:
    return all(character in XML_WHITESPACE for character in lexical)


@dataclass(frozen=True)
class ValidationMismatch:
    """Earliest definitive mismatch and its entire preceding expectation pattern.

    ``element`` identifies the occurrence (the parent for character data);
    ``content_index`` identifies text/tails within its normalized content.
    Attribute names and exact lexical values remain available to diagnostics.
    No branch-specific scalar interpretation is collected.  ``recovery`` names
    the diagnostic continuation used for this finding, or ``None`` for a
    fail-fast finding.
    """

    event: str
    element: RelaxNGElement | None
    preceding: Pattern
    name: str | None = None
    lexical: str | None = None
    content_index: int | None = None
    recovery: str | None = None


def _viable_residuals(pattern: Pattern) -> tuple[Pattern, ...]:
    """Top-level viable alternatives, including nonnullable ones."""
    leaves: list[Pattern] = []
    pending = [pattern]
    while pending:
        node = pending.pop()
        if isinstance(node, Choice):
            pending.extend((node.second, node.first))
        elif not isinstance(node, NotAllowed):
            leaves.append(node)
    return tuple(leaves)


@dataclass(frozen=True)
class ValidationOutcome:
    """Boolean verdict, all remaining alternatives, and first-mismatch evidence."""

    valid: bool
    residual: Pattern
    mismatch: ValidationMismatch | None = None

    @property
    def residuals(self) -> tuple[Pattern, ...]:
        """Top-level viable alternatives, including nonnullable ones."""
        return _viable_residuals(self.residual)


@dataclass(frozen=True)
class RecoveryOutcome:
    """A diagnostic traversal with nonaccepting recovery states.

    ``valid`` is true only when the traversal found no mismatch.  A recovery
    continuation is diagnostic evidence, never accepting evidence, so a
    recovered traversal stays invalid even when its residual is nullable.
    ``mismatches`` retains every independent finding in event order, and
    ``residuals`` preserves all still-viable recovery states.  Report
    incompleteness comes only from suppressed retained findings; exhausted
    derivative or memo work raises instead of producing this value.
    """

    valid: bool
    residual: Pattern
    mismatches: tuple[ValidationMismatch, ...] = ()
    recovered: bool = False

    @property
    def residuals(self) -> tuple[Pattern, ...]:
        """Top-level viable recovery alternatives, including nonnullable ones."""
        return _viable_residuals(self.residual)


class _Event(NamedTuple):
    kind: str
    element: RelaxNGElement | None
    name: str | None = None
    lexical: str | None = None
    content_index: int | None = None


def _content_events(
    children: Iterable[str | RelaxNGElement], owner: RelaxNGElement | None
) -> list[_Event | RelaxNGElement]:
    """Project the lexical tree to Clark's adjacent-text-coalesced child model."""
    content: list[str | RelaxNGElement] = []
    text: list[str] = []
    for child in children:
        if isinstance(child, str):
            text.append(child)
        elif child._name_ is ET.Comment or child._name_ is ET.ProcessingInstruction:
            # Its tail is a separate item from iter_content; its own text is
            # comment/PI data, not a character node.
            continue
        else:
            if text:
                content.append("".join(text))
                text = []
            content.append(child)
    if text:
        content.append("".join(text))
    if not content:
        content.append("")
    if len(content) == 1 and isinstance(content[0], str):
        return [
            _Event(
                "weak_text" if _whitespace(content[0]) else "text",
                owner,
                lexical=content[0],
                content_index=0,
            )
        ]
    return [
        _Event("text", owner, lexical=child, content_index=index)
        if isinstance(child, str)
        else child
        for index, child in enumerate(content)
        if not isinstance(child, str) or not _whitespace(child)
    ]


def _instance_events(
    pending: Iterable[_Event | RelaxNGElement],
) -> Iterator[_Event]:
    """Expand tree events iteratively; document depth is not Python call depth."""
    stack = list(reversed(tuple(pending)))
    while stack:
        item = stack.pop()
        if isinstance(item, _Event):
            yield item
            continue
        if not isinstance(item._name_, str) or not item._name_:
            yield _Event("structure", item)
        else:
            yield _Event("start_tag_open", item, item._name_)
            for name, lexical in sorted(item.attributes.items()):
                if name in (f"{{{XML_NS}}}base", f"{{{XML_NS}}}space", "xmlns") or name.startswith(
                    ("{http://www.w3.org/2000/xmlns/}", "xmlns:")
                ):
                    continue
                yield _Event("attribute", item, name, lexical)
            yield _Event("start_tag_close", item)
            stack.append(_Event("end_tag", item))
            stack.extend(reversed(_content_events(item.iter_content(), item)))


def _run_events(
    pattern: Pattern, events: Iterable[_Event], context: DerivationContext
) -> tuple[Pattern, ValidationMismatch | None]:
    original_context = context.datatype_context
    try:
        for event in events:
            previous = pattern
            owner = event.element
            context.datatype_context = (
                original_context
                if owner is None
                else DatatypeContext(owner.base_uri, owner.namespaces or {})
            )
            if event.kind == "structure":
                pattern = NotAllowed()
            elif event.kind == "start_tag_open":
                pattern = start_tag_open_derivative(pattern, cast(str, event.name), context)
            elif event.kind == "attribute":
                pattern = attribute_derivative(
                    pattern, cast(str, event.name), cast(str, event.lexical), context
                )
            elif event.kind == "start_tag_close":
                pattern = start_tag_close_derivative(pattern, context)
            elif event.kind == "end_tag":
                pattern = end_tag_derivative(pattern, context)
            else:
                pattern = text_derivative(pattern, cast(str, event.lexical), context)
                if event.kind == "weak_text":
                    pattern = context.canonical_residual(choice(previous, pattern))
            if isinstance(pattern, NotAllowed):
                return pattern, ValidationMismatch(
                    "text" if event.kind == "weak_text" else event.kind,
                    owner,
                    previous,
                    event.name,
                    event.lexical,
                    event.content_index,
                )
        return pattern, None
    finally:
        context.datatype_context = original_context


def children_derivative(
    pattern: Pattern, children: Iterable[str | RelaxNGElement], context: DerivationContext
) -> Pattern:
    """Consume ordered children with Clark's weak and mixed-whitespace rules."""
    result, _ = _run_events(pattern, _instance_events(_content_events(children, None)), context)
    return result


def _current_position_children(pattern: Pattern) -> tuple[Pattern, ...]:
    """Operands a current-position projection must transform first."""
    if isinstance(pattern, _After):
        return (pattern.first,)
    if isinstance(pattern, (Choice, Group, Interleave)):
        return (pattern.first, pattern.second)
    if isinstance(pattern, OneOrMore):
        return (pattern.pattern,)
    if isinstance(pattern, Ref):
        return (pattern.target,)
    return ()


def _pop_continuation_children(pattern: Pattern) -> tuple[Pattern, ...]:
    """Operands whose continuations an end-tag recovery must retain."""
    if isinstance(pattern, _After):
        return ()
    if isinstance(pattern, Choice):
        return (pattern.first, pattern.second)
    if isinstance(pattern, Ref):
        return (pattern.target,)
    return ()


def _project_current_position(
    pattern: Pattern,
    children_of: Callable[[Pattern], tuple[Pattern, ...]],
    combine: Callable[[Pattern, Callable[[Pattern], Pattern]], Pattern],
    memo: dict[int, Pattern] | None = None,
) -> Pattern:
    """Project one recovery state without recursing on pattern depth.

    ``combine`` receives a node and a child lookup.  Reference cycles are
    pre-seeded with ``empty`` exactly as the recursive projections did, so a
    back edge contributes the empty current-position requirement and recovery
    never selects a branch.
    """
    memo = {} if memo is None else memo
    results: dict[int, Pattern] = {}
    in_progress: set[int] = set()
    pending: list[tuple[Pattern, bool]] = [(pattern, False)]

    def child(operand: Pattern) -> Pattern:
        key = id(operand)
        if key in results:
            return results[key]
        return memo[key]

    while pending:
        node, ready = pending.pop()
        key = id(node)
        if not ready:
            if key in results:
                continue
            cached = memo.get(key)
            if cached is not None:
                results[key] = cached
                continue
            if isinstance(node, Ref):
                memo[key] = Empty()
            in_progress.add(key)
            pending.append((node, True))
            for operand in reversed(children_of(node)):
                operand_key = id(operand)
                if operand_key in results or operand_key in in_progress:
                    continue
                pending.append((operand, False))
            continue
        result = combine(node, child)
        results[key] = result
        in_progress.discard(key)
        if isinstance(node, Ref):
            memo[key] = result
    return results[id(pattern)]


def _without_attributes_node(pattern: Pattern, child: Callable[[Pattern], Pattern]) -> Pattern:
    if isinstance(pattern, Attribute):
        return Empty()
    if isinstance(pattern, _After):
        return _after(child(pattern.first), pattern.second)
    if isinstance(pattern, Choice):
        return choice(child(pattern.first), child(pattern.second))
    if isinstance(pattern, Group):
        return group(child(pattern.first), child(pattern.second))
    if isinstance(pattern, Interleave):
        return interleave(child(pattern.first), child(pattern.second))
    if isinstance(pattern, OneOrMore):
        return one_or_more(child(pattern.pattern))
    if isinstance(pattern, Ref):
        return child(pattern.target)
    if isinstance(pattern, Element):
        return pattern
    if isinstance(pattern, (Data, DataExcept, Empty, List, NotAllowed, Text, Value)):
        return pattern
    raise TypeError(f"unknown RELAX NG pattern {type(pattern).__name__}")


def _without_attributes(pattern: Pattern, _memo: dict[int, Pattern] | None = None) -> Pattern:
    """Remove current-position attribute requirements from *pattern*.

    Child element contents and post-end-tag continuations keep their own
    attribute expectations.  Reference cycles are treated as empty current
    position requirements, so recovery terminates without selecting a branch.
    """
    return _project_current_position(
        pattern,
        _current_position_children,
        _without_attributes_node,
        _memo,
    )


def _consume_attribute_node(
    pattern: Pattern,
    child: Callable[[Pattern], Pattern],
    uri: str | None,
    local: str,
) -> Pattern:
    if isinstance(pattern, Attribute):
        return Empty() if pattern.name_class.contains(uri, local) else pattern
    if isinstance(pattern, _After):
        return _after(child(pattern.first), pattern.second)
    if isinstance(pattern, Choice):
        return choice(child(pattern.first), child(pattern.second))
    if isinstance(pattern, Group):
        return group(child(pattern.first), child(pattern.second))
    if isinstance(pattern, Interleave):
        return interleave(child(pattern.first), child(pattern.second))
    if isinstance(pattern, OneOrMore):
        return one_or_more(child(pattern.pattern))
    if isinstance(pattern, Ref):
        return child(pattern.target)
    if isinstance(pattern, Element):
        return pattern
    if isinstance(pattern, (Data, DataExcept, Empty, List, NotAllowed, Text, Value)):
        return pattern
    raise TypeError(f"unknown RELAX NG pattern {type(pattern).__name__}")


def _consume_attribute(
    pattern: Pattern, uri: str | None, local: str, _memo: dict[int, Pattern] | None = None
) -> Pattern:
    """Account for one failed or unexpected attribute occurrence.

    Matching required attributes become empty; other names remain required so a
    later missing-attribute finding is not suppressed.
    """
    return _project_current_position(
        pattern,
        _current_position_children,
        lambda node, child: _consume_attribute_node(node, child, uri, local),
        _memo,
    )


def _relax_scalars_node(pattern: Pattern, child: Callable[[Pattern], Pattern]) -> Pattern:
    if isinstance(pattern, _After):
        return _after(child(pattern.first), pattern.second)
    if isinstance(pattern, Choice):
        return choice(child(pattern.first), child(pattern.second))
    if isinstance(pattern, Group):
        return group(child(pattern.first), child(pattern.second))
    if isinstance(pattern, Interleave):
        return interleave(child(pattern.first), child(pattern.second))
    if isinstance(pattern, OneOrMore):
        return one_or_more(child(pattern.pattern))
    if isinstance(pattern, Ref):
        return child(pattern.target)
    if isinstance(pattern, (Data, DataExcept, List, Value)):
        return Text()
    if isinstance(pattern, (Attribute, Element, Empty, NotAllowed, Text)):
        return pattern
    raise TypeError(f"unknown RELAX NG pattern {type(pattern).__name__}")


def _relax_scalars(pattern: Pattern, _memo: dict[int, Pattern] | None = None) -> Pattern:
    """Let a failed scalar occurrence consume one current-position text slot.

    Only current-position scalar content is relaxed; child element contents,
    attributes, and post-end-tag continuations keep their expectations.
    """
    return _project_current_position(
        pattern,
        _current_position_children,
        _relax_scalars_node,
        _memo,
    )


def _pop_continuations_node(pattern: Pattern, child: Callable[[Pattern], Pattern]) -> Pattern:
    if isinstance(pattern, _After):
        return pattern.second
    if isinstance(pattern, Choice):
        return choice(child(pattern.first), child(pattern.second))
    if isinstance(pattern, Ref):
        return child(pattern.target)
    return Empty()


def _pop_continuations(pattern: Pattern, _memo: dict[int, Pattern] | None = None) -> Pattern:
    """Accept an end tag whose required content was missing.

    Every ``After`` leaf yields its continuation; a leaf with no continuation
    yields the empty state so the parent's content can still be diagnosed.
    Recovery states are nonaccepting regardless of this projection.
    """
    return _project_current_position(
        pattern,
        _pop_continuation_children,
        _pop_continuations_node,
        _memo,
    )


def _expects_attribute_name(pattern: Pattern, uri: str | None, local: str) -> bool:
    """Whether *pattern* declares a current-position attribute with this name.

    A declared attribute is "consumed" by recovery even when its value fails;
    an undeclared name is merely "ignored".  Child element contents are not
    entered, and reference cycles are guarded.
    """
    pending = [pattern]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        key = id(node)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(node, _After):
            pending.append(node.first)
        elif isinstance(node, Ref):
            pending.append(node.target)
        elif isinstance(node, (Choice, Group, Interleave)):
            pending.extend((node.first, node.second))
        elif isinstance(node, OneOrMore):
            pending.append(node.pattern)
        elif isinstance(node, Attribute):
            if node.name_class.contains(uri, local):
                return True
        elif isinstance(node, Element):
            continue
        elif not isinstance(node, (Data, DataExcept, Empty, List, NotAllowed, Text, Value)):
            raise TypeError(f"unknown RELAX NG pattern {type(node).__name__}")
    return False


def _run_recovery_events(
    pattern: Pattern, events: Iterable[_Event], context: DerivationContext
) -> tuple[Pattern, tuple[ValidationMismatch, ...]]:
    """Continue through independent findings while keeping recovery nonaccepting.

    Unexpected subtrees are skipped whole, failed attributes are accounted for,
    and missing attributes are removed before content continues.  A rejected
    text advances only the scalar slot it occupied, so a later required element
    or scalar is still reported independently; every recovery continuation
    remains a diagnostic state and the caller never treats it as accepting
    evidence.
    """
    original_context = context.datatype_context
    mismatches: list[ValidationMismatch] = []
    skipping: RelaxNGElement | None = None
    try:
        for event in events:
            owner = event.element
            if skipping is not None:
                if event.kind == "end_tag" and owner is skipping:
                    skipping = None
                continue
            previous = pattern
            context.datatype_context = (
                original_context
                if owner is None
                else DatatypeContext(owner.base_uri, owner.namespaces or {})
            )
            if event.kind == "structure":
                mismatches.append(ValidationMismatch("structure", owner, previous))
                return NotAllowed(), tuple(mismatches)
            if event.kind == "start_tag_open":
                derived = start_tag_open_derivative(previous, cast(str, event.name), context)
                if isinstance(derived, NotAllowed):
                    mismatches.append(
                        ValidationMismatch(
                            "start_tag_open",
                            owner,
                            previous,
                            event.name,
                            recovery=RECOVERY_IGNORED_SUBTREE,
                        )
                    )
                    skipping = owner
                else:
                    pattern = derived
                continue
            if event.kind == "attribute":
                derived = attribute_derivative(
                    previous, cast(str, event.name), cast(str, event.lexical), context
                )
                if isinstance(derived, NotAllowed):
                    uri, local = expanded_name(cast(str, event.name))
                    declared = _expects_attribute_name(previous, uri, local)
                    mismatches.append(
                        ValidationMismatch(
                            "attribute",
                            owner,
                            previous,
                            event.name,
                            event.lexical,
                            event.content_index,
                            recovery=(
                                RECOVERY_CONSUMED_ATTRIBUTE
                                if declared
                                else RECOVERY_IGNORED_ATTRIBUTE
                            ),
                        )
                    )
                    pattern = context.canonical_residual(_consume_attribute(previous, uri, local))
                else:
                    pattern = derived
                continue
            if event.kind == "start_tag_close":
                derived = start_tag_close_derivative(previous, context)
                if isinstance(derived, NotAllowed):
                    mismatches.append(
                        ValidationMismatch(
                            "start_tag_close",
                            owner,
                            previous,
                            recovery=RECOVERY_MISSING_ATTRIBUTE,
                        )
                    )
                    pattern = context.canonical_residual(_without_attributes(previous))
                else:
                    pattern = derived
                continue
            if event.kind == "end_tag":
                derived = end_tag_derivative(previous, context)
                if isinstance(derived, NotAllowed):
                    mismatches.append(
                        ValidationMismatch(
                            "end_tag",
                            owner,
                            previous,
                            recovery=RECOVERY_MISSING_CONTENT,
                        )
                    )
                    pattern = context.canonical_residual(_pop_continuations(previous))
                else:
                    pattern = derived
                continue
            derived = text_derivative(previous, cast(str, event.lexical), context)
            if event.kind == "weak_text":
                derived = context.canonical_residual(choice(previous, derived))
            if isinstance(derived, NotAllowed):
                relaxed = text_derivative(
                    context.canonical_residual(_relax_scalars(previous)),
                    cast(str, event.lexical),
                    context,
                )
                mismatches.append(
                    ValidationMismatch(
                        "text",
                        owner,
                        previous,
                        event.name,
                        event.lexical,
                        event.content_index,
                        recovery=RECOVERY_CONTINUED_CONTENT,
                    )
                )
                if isinstance(relaxed, NotAllowed):
                    pattern = previous
                else:
                    pattern = context.canonical_residual(choice(previous, relaxed))
            else:
                pattern = derived
        return pattern, tuple(mismatches)
    finally:
        context.datatype_context = original_context


def validate_element_recovering(
    root: RelaxNGElement, start: Pattern, *, limits: RelaxNGLimits | None = None
) -> RecoveryOutcome:
    """Validate one lexical element and retain every independent finding.

    Unlike the fail-fast :func:`validate_element`, this traversal continues
    after unexpected subtrees or attributes, missing attributes or content, and
    failed scalars.  Recovery states are diagnostic only: any finding makes the
    outcome invalid even if later content would satisfy a relaxed residual.
    Exhausted derivative or memo budgets raise
    :class:`~pyxsd.relaxng.errors.RelaxNGLimitError`; retention caps never
    change this traversal.
    """
    context = DerivationContext(_EMPTY_DATATYPE_CONTEXT, PatternPool(), limits)
    residual, mismatches = _run_recovery_events(start, _instance_events([root]), context)
    if not mismatches and not _nullable(residual):
        mismatches = (ValidationMismatch("document_end", root, residual),)
    valid = not mismatches
    recovered = any(mismatch.recovery is not None for mismatch in mismatches)
    return RecoveryOutcome(valid, residual, mismatches, recovered=recovered)


def validate_element(
    root: RelaxNGElement, start: Pattern, *, limits: RelaxNGLimits | None = None
) -> ValidationOutcome:
    """Validate one lexical element without choosing a derivation or modifying it.

    The first definitive mismatch retains the preceding residual for Task 4
    diagnostics. Recovery and public scalar interpretations are separate passes.
    All retained event/text/list work shares one parse-local budget counter.
    """
    context = DerivationContext(_EMPTY_DATATYPE_CONTEXT, PatternPool(), limits)
    residual, mismatch = _run_events(start, _instance_events([root]), context)
    valid = mismatch is None and _nullable(residual)
    if not valid and mismatch is None:
        mismatch = ValidationMismatch("document_end", root, residual)
    return ValidationOutcome(valid, residual, mismatch)
