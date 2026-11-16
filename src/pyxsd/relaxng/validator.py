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
``After`` outside group, interleave, and repetition operands. An iterative
lexical-tree walk drives these transitions without recursing on document depth.

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

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import NamedTuple, cast
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
    "DerivationContext",
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
        if _nullable(pattern.first):
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
        excluded = text_derivative(pattern.excluded, lexical, context)
        return NotAllowed() if _nullable(excluded) else Empty()
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
    """Push a continuation operation into each top-level ``After`` leaf."""
    if isinstance(pattern, _After):
        return _after(pattern.first, function(pattern.second))
    if isinstance(pattern, Choice):
        return choice(_apply_after(function, pattern.first), _apply_after(function, pattern.second))
    if isinstance(pattern, NotAllowed):
        return pattern
    raise TypeError("start-tag derivative violated the After placement invariant")


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
    """Memoize event work using the text/list context's existing two budgets."""
    if context is None:
        return _derive_event(event, pattern, name, lexical, context)
    key = (
        event,
        pattern,
        name,
        lexical,
        context.datatype_context if event == "attribute" else None,
    )
    cached = context._event_memo.get(key)
    if cached is not None:
        return cached
    result = context.canonical_residual(_derive_event(event, pattern, name, lexical, context))
    context._counter.consume("max_memo_entries")
    context._event_memo[key] = result
    return result


def _derive_event(
    event: str,
    pattern: Pattern,
    name: str,
    lexical: str,
    context: DerivationContext | None,
) -> Pattern:
    def derive(operand: Pattern) -> Pattern:
        return _event_derivative(event, operand, name, lexical, context)

    if isinstance(pattern, Ref):
        return derive(pattern.target)
    if isinstance(pattern, Choice):
        return choice(derive(pattern.first), derive(pattern.second))
    if event == "end":
        if isinstance(pattern, _After) and _nullable(pattern.first):
            return pattern.second
        return NotAllowed()
    if isinstance(pattern, _After):
        if event == "open":
            return _apply_after(lambda rest: _after(rest, pattern.second), derive(pattern.first))
        return _after(derive(pattern.first), pattern.second)
    if event == "close":
        if isinstance(pattern, Group):
            return group(derive(pattern.first), derive(pattern.second))
        if isinstance(pattern, Interleave):
            return interleave(derive(pattern.first), derive(pattern.second))
        if isinstance(pattern, OneOrMore):
            return one_or_more(derive(pattern.pattern))
        return NotAllowed() if isinstance(pattern, Attribute) else pattern
    if isinstance(pattern, (Group, Interleave)):
        first, second = pattern.first, pattern.second
        constructor = group if isinstance(pattern, Group) else interleave
        if event == "attribute":
            return choice(constructor(derive(first), second), constructor(first, derive(second)))
        left = _apply_after(lambda rest: constructor(rest, second), derive(first))
        if isinstance(pattern, Interleave):
            return choice(left, _apply_after(lambda rest: interleave(first, rest), derive(second)))
        return choice(left, derive(second)) if _nullable(first) else left
    if isinstance(pattern, OneOrMore):
        rest = choice(one_or_more(pattern.pattern), Empty())
        derived = derive(pattern.pattern)
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
    No branch-specific scalar interpretation is collected.
    """

    event: str
    element: RelaxNGElement | None
    preceding: Pattern
    name: str | None = None
    lexical: str | None = None
    content_index: int | None = None


@dataclass(frozen=True)
class ValidationOutcome:
    """Boolean verdict, all remaining alternatives, and first-mismatch evidence."""

    valid: bool
    residual: Pattern
    mismatch: ValidationMismatch | None = None

    @property
    def residuals(self) -> tuple[Pattern, ...]:
        """Top-level viable alternatives, including nonnullable ones."""
        leaves: list[Pattern] = []
        pending = [self.residual]
        while pending:
            node = pending.pop()
            if isinstance(node, Choice):
                pending.extend((node.second, node.first))
            elif not isinstance(node, NotAllowed):
                leaves.append(node)
        return tuple(leaves)


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
