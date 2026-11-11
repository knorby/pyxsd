"""Grounded XSLT accumulator evaluation.

An :class:`AccumulatorDefinition` is compiled data: an initial-value
expression, the declared ``as`` sequence type, and the ordered
``xsl:accumulator-rule`` declarations.  :class:`AccumulatorManager` owns the
per-transformation, per-tree value cache.  It walks a grounded XDM tree in
document order, excluding attribute and namespace nodes as XSLT 3.0 requires,
and applies the highest-priority start/end rule at each visit.

The module also owns the static helpers used by compilation: ``use-accumulators``
token parsing, EQName expansion, literal accumulator-call discovery for cycle
analysis, and cycle detection.  No event parser, streaming cursor, or
process-global cache exists here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, TypeVar

from elementpath.helpers import is_ncname

from pyxsd.locations import IssueLocation
from pyxsd.xdm import XdmSequence
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xslt.errors import XsltDynamicError, XsltError, error_code_from_exception

__all__ = [
    "AccumulatorDefinition",
    "AccumulatorKey",
    "AccumulatorManager",
    "AccumulatorRule",
    "expand_accumulator_name",
    "find_cycle",
    "iter_accumulator_call_spans",
    "iter_accumulator_calls",
    "parse_accumulator_names",
    "same_context_accumulator_call",
]

AccumulatorKey = tuple[str | None, str]
RulePhase = Literal["start", "end"]
ValuePhase = Literal["before", "after"]

_ACCUMULATOR_CALL = re.compile(
    r"(?<![\w.:-])(?:(fn):)?accumulator-(before|after)\s*\(",
)


@dataclass(frozen=True, slots=True)
class AccumulatorRule:
    """One compiled ``xsl:accumulator-rule`` declaration.

    ``priority`` is an implementation extension: XSLT 3.0 has no priority
    attribute on an accumulator rule and uses the last matching rule in
    document order.  A missing priority is therefore ``0`` for every rule, so
    document order remains the tie-breaker; an explicit priority lets a caller
    select a rule deterministically without relying on that order.
    """

    phase: RulePhase
    pattern: Any = None
    select: Any = None
    body: tuple[Any, ...] = ()
    priority: Decimal | None = None
    order: int = 0
    instruction: Any = None
    location: IssueLocation | None = None


@dataclass(frozen=True, slots=True)
class AccumulatorDefinition:
    """One package-local ``xsl:accumulator`` declaration."""

    owner: str | None
    name: str
    initial_value: Any
    sequence_type: Any = None
    streamable: bool = False
    rules: tuple[AccumulatorRule, ...] = ()
    module: str | None = None
    location: IssueLocation | None = None

    @property
    def key(self) -> AccumulatorKey:
        return (self.owner, self.name)


@dataclass(frozen=True, slots=True)
class _AccumulatorValues:
    before: dict[XdmNode, XdmSequence] = field(default_factory=dict)
    after: dict[XdmNode, XdmSequence] = field(default_factory=dict)


class AccumulatorManager:
    """Per-invocation accumulator values with per-tree caches.

    The manager is created by one transformation.  Values are computed lazily
    for the first requested accumulator and tree, then reused for every later
    ``accumulator-before``/``accumulator-after`` call.  Definitions are
    immutable compiled data shared by every invocation; no mutable value cache
    or tree policy is stored on a definition.
    """

    def __init__(
        self,
        *,
        lookup: Callable[[str | None, str], AccumulatorDefinition | None],
        evaluate_initial: Callable[[AccumulatorDefinition, XdmNode], XdmSequence],
        evaluate_rule: Callable[
            [AccumulatorDefinition, AccumulatorRule, XdmNode, XdmSequence], XdmSequence
        ],
        coerce: Callable[[XdmSequence, AccumulatorDefinition], XdmSequence],
        matches: Callable[[AccumulatorDefinition, AccumulatorRule, XdmNode], bool],
    ) -> None:
        self._lookup = lookup
        self._evaluate_initial = evaluate_initial
        self._evaluate_rule = evaluate_rule
        self._coerce = coerce
        self._matches = matches
        self._values: dict[tuple[int, AccumulatorKey], _AccumulatorValues] = {}
        self._errors: dict[tuple[int, AccumulatorKey], XsltError] = {}
        self._evaluating: set[tuple[int, AccumulatorKey]] = set()
        self._restrictions: dict[int, frozenset[AccumulatorKey]] = {}
        self._roots: dict[int, XdmNode] = {}

    # -- lookup and applicability ------------------------------------------

    def definition(
        self,
        package_name: str | None,
        expanded_name: str,
    ) -> AccumulatorDefinition | None:
        """Resolve one expanded accumulator name in a package scope."""
        return self._lookup(package_name, expanded_name)

    def restrict(self, root: XdmNode, keys: Iterable[AccumulatorKey]) -> None:
        """Set the accumulators applicable to the tree rooted at *root*."""
        tree = self._tree_key(root)
        self._restrictions[tree] = frozenset(keys)

    def allow_all(self, root: XdmNode) -> None:
        """Make every accumulator applicable to the tree rooted at *root*."""
        tree = self._tree_key(root)
        self._restrictions.pop(tree, None)

    def _tree_key(self, root: XdmNode) -> int:
        if not isinstance(root, XdmNode):
            raise TypeError("an accumulator tree root must be an XDM node")
        tree = id(root)
        self._roots[tree] = root
        return tree

    # -- values -------------------------------------------------------------

    def value(
        self,
        definition: AccumulatorDefinition,
        node: XdmNode,
        phase: ValuePhase,
    ) -> XdmSequence:
        """Return the pre- or post-descent value at *node*."""
        if phase not in ("before", "after"):
            raise ValueError(f"unknown accumulator value phase {phase!r}")
        if not isinstance(node, XdmNode):
            raise XsltDynamicError(
                "an accumulator value requires a node context item",
                code="XTTE3360",
            )
        if node.kind in (NodeKind.ATTRIBUTE, NodeKind.NAMESPACE):
            raise XsltDynamicError(
                "an accumulator value is not defined for attribute or namespace nodes",
                code="XTTE3360",
            )
        root = node.root
        tree = self._tree_key(root)
        restriction = self._restrictions.get(tree)
        if restriction is not None and definition.key not in restriction:
            raise XsltDynamicError(
                f"the accumulator {definition.name!r} is not applicable to this tree",
                code="XTDE3362",
            )
        cache_key = (tree, definition.key)
        values = self._values.get(cache_key)
        if values is not None:
            selected = values.before if phase == "before" else values.after
            if node in selected:
                return selected[node]
        cached_error = self._errors.get(cache_key)
        if cached_error is not None:
            raise cached_error
        if cache_key in self._evaluating:
            raise XsltDynamicError(
                f"the accumulator {definition.name!r} depends on itself",
                code="XTDE3400",
            )
        try:
            values = self._compute(definition, root, cache_key)
        except XsltError:
            # A later node failed.  The requested node's value may already be
            # known; only a functionally dependent call signals the error.
            values = self._values[cache_key]
            selected = values.before if phase == "before" else values.after
            if node in selected:
                return selected[node]
            raise
        selected = values.before if phase == "before" else values.after
        try:
            return selected[node]
        except KeyError as exc:  # pragma: no cover - node is always in its own tree
            raise XsltDynamicError(
                "the accumulator value is not available for this node",
                code="XTTE3360",
            ) from exc

    def _compute(
        self,
        definition: AccumulatorDefinition,
        root: XdmNode,
        cache_key: tuple[int, AccumulatorKey],
    ) -> _AccumulatorValues:
        self._evaluating.add(cache_key)
        values = _AccumulatorValues()
        self._values[cache_key] = values
        try:
            current = self._coerce(
                self._evaluate_initial(definition, root),
                definition,
            )
            self._visit(definition, root, current, values)
            return values
        except XsltError as exc:
            self._errors[cache_key] = exc
            raise
        except Exception as exc:
            error = XsltDynamicError(
                str(exc) or type(exc).__name__,
                code=error_code_from_exception(exc),
                cause=exc,
            )
            self._errors[cache_key] = error
            raise error from exc
        finally:
            self._evaluating.discard(cache_key)

    def _visit(
        self,
        definition: AccumulatorDefinition,
        node: XdmNode,
        current: XdmSequence,
        values: _AccumulatorValues,
    ) -> XdmSequence:
        current = self._apply(definition, node, "start", current)
        values.before[node] = current
        for child in node.children:
            current = self._visit(definition, child, current, values)
        current = self._apply(definition, node, "end", current)
        values.after[node] = current
        return current

    def _apply(
        self,
        definition: AccumulatorDefinition,
        node: XdmNode,
        phase: RulePhase,
        current: XdmSequence,
    ) -> XdmSequence:
        selected: AccumulatorRule | None = None
        selected_key: tuple[Decimal, int] | None = None
        for rule in definition.rules:
            if rule.phase != phase or not self._matches(definition, rule, node):
                continue
            key = (rule.priority if rule.priority is not None else Decimal(0), rule.order)
            if selected_key is None or key > selected_key:
                selected = rule
                selected_key = key
        if selected is None:
            return current
        return self._coerce(self._evaluate_rule(definition, selected, node, current), definition)


# ---------------------------------------------------------------------------
# Static declaration helpers


def parse_accumulator_names(value: str) -> tuple[str, ...]:
    """Parse one ``use-accumulators`` token list.

    The result is either empty, the single token ``#all``, or a tuple of
    distinct EQNames.  Invalid combinations raise :class:`ValueError`; the
    caller reports the specification's ``XTSE3300`` static error.
    """
    tokens = value.split()
    if not tokens:
        return ()
    if "#all" in tokens:
        if len(tokens) != 1:
            raise ValueError("the #all token cannot be combined with accumulator names")
        return ("#all",)
    if len(set(tokens)) != len(tokens):
        raise ValueError("an accumulator name is repeated")
    return tuple(tokens)


def expand_accumulator_name(value: str, namespaces: Mapping[str, str]) -> str:
    """Expand one EQName used for an accumulator reference or declaration.

    Accumulator names have no default namespace, so an unprefixed lexical name
    is returned unchanged.  An invalid EQName, an unbound prefix, or an invalid
    local name raises :class:`ValueError`.
    """
    name = value.strip()
    if not name:
        raise ValueError("an accumulator name must not be empty")
    if name.startswith("Q{"):
        close = name.find("}")
        if close < 0:
            raise ValueError(f"invalid EQName {value!r}")
        uri = name[2:close]
        local = name[close + 1 :]
        if not is_ncname(local):
            raise ValueError(f"invalid EQName {value!r}")
        return f"{{{uri}}}{local}" if uri else local
    if name.startswith("{"):
        close = name.find("}")
        if close < 0:
            raise ValueError(f"invalid EQName {value!r}")
        uri = name[1:close]
        local = name[close + 1 :]
        if not is_ncname(local):
            raise ValueError(f"invalid EQName {value!r}")
        return f"{{{uri}}}{local}" if uri else local
    prefix, separator, local = name.partition(":")
    if not separator:
        if not is_ncname(name):
            raise ValueError(f"invalid accumulator name {value!r}")
        return name
    if not is_ncname(prefix) or not is_ncname(local):
        raise ValueError(f"invalid QName {value!r}")
    bound_uri = namespaces.get(prefix)
    if bound_uri is None:
        raise ValueError(f"the namespace prefix {prefix!r} is not bound")
    return f"{{{bound_uri}}}{local}"


def _skip_comment(source: str, start: int) -> int:
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
    return len(source)


def _read_string(source: str, start: int) -> tuple[str, int]:
    quote = source[start]
    index = start + 1
    value: list[str] = []
    while index < len(source):
        if source[index] == quote:
            if index + 1 < len(source) and source[index + 1] == quote:
                value.append(quote)
                index += 2
                continue
            return "".join(value), index + 1
        value.append(source[index])
        index += 1
    return "".join(value), len(source)


def iter_accumulator_call_spans(source: str) -> Iterator[tuple[str, str | None, int]]:
    """Yield ``(phase, literal argument, call start)`` for accumulator calls.

    String literals and nested XPath comments are skipped.  The start offset is
    the position of the function name, which lets static analysis distinguish a
    same-context call from one reached through a path or simple-map operator.
    """
    index = 0
    length = len(source)
    while index < length:
        if source.startswith("(:", index):
            index = _skip_comment(source, index)
            continue
        if source[index] in ("'", '"'):
            _, index = _read_string(source, index)
            continue
        match = _ACCUMULATOR_CALL.match(source, index)
        if match is None:
            index += 1
            continue
        phase = match.group(2)
        position = match.end()
        while position < length and source[position].isspace():
            position += 1
        if position < length and source[position] in ("'", '"'):
            value, position = _read_string(source, position)
            yield phase, value, match.start()
            index = position
            continue
        yield phase, None, match.start()
        index = match.end()


def iter_accumulator_calls(source: str) -> Iterator[tuple[str, str | None]]:
    """Yield literal ``accumulator-before``/``accumulator-after`` calls.

    String literals and nested XPath comments are skipped, so a name that only
    appears as data is not a dependency.  When the first argument is a string
    literal, the literal value is returned; a non-literal argument yields
    ``None`` because static cycle analysis cannot resolve it.
    """
    for phase, value, _start in iter_accumulator_call_spans(source):
        yield phase, value


_NODE_TEST_FUNCTIONS = frozenset(
    {
        "attribute",
        "comment",
        "document-node",
        "element",
        "node",
        "processing-instruction",
        "schema-attribute",
        "schema-element",
        "text",
    }
)


def _previous_nonspace(source: str, index: int) -> int:
    while index >= 0 and source[index].isspace():
        index -= 1
    return index


def _enclosing_open(source: str, position: int, opening: str, closing: str) -> int | None:
    stack: list[int] = []
    index = 0
    while index < position:
        if source.startswith("(:", index):
            index = _skip_comment(source, index)
            continue
        if source[index] in ("'", '"'):
            _, index = _read_string(source, index)
            continue
        char = source[index]
        if char == opening:
            stack.append(index)
        elif char == closing and stack:
            stack.pop()
        index += 1
    return stack[-1] if stack else None


def enclosing_predicate_start(source: str, position: int) -> int | None:
    """Return the ``[`` that opens the innermost predicate containing *position*."""
    return _enclosing_open(source, position, "[", "]")


def enclosing_paren_start(source: str, position: int) -> int | None:
    """Return the ``(`` that opens the innermost parenthesis containing *position*."""
    return _enclosing_open(source, position, "(", ")")


def _axis_before(source: str, token_start: int) -> str | None:
    before = _previous_nonspace(source, token_start - 1)
    if before < 1 or source[before] != ":" or source[before - 1] != ":":
        return None
    axis_end = _previous_nonspace(source, before - 2)
    if axis_end < 0:
        return None
    axis_start = axis_end
    while axis_start >= 0 and (source[axis_start].isalnum() or source[axis_start] in ("_", "-")):
        axis_start -= 1
    return source[axis_start + 1 : axis_end + 1]


def _token_axis_changes_focus(source: str, index: int) -> bool:
    end = index
    if source[index] == "*":
        token_start = index
    else:
        while index >= 0 and (source[index].isalnum() or source[index] in ("_", "-", ".", ":")):
            index -= 1
        token_start = index + 1
    token = source[token_start : end + 1]
    if "::" in token:
        axis = token.rpartition("::")[0].strip()
        return axis != "self"
    axis_name = _axis_before(source, token_start)
    if axis_name is not None:
        return axis_name != "self"
    before = _previous_nonspace(source, token_start - 1)
    return not (before >= 0 and source[before] in ("$", "?"))


def _paren_is_node_test(source: str, close_index: int) -> bool:
    opening = enclosing_paren_start(source, close_index)
    if opening is None:
        return False
    token_end = _previous_nonspace(source, opening - 1)
    if token_end < 0:
        return False
    token_start = token_end
    while token_start >= 0 and (
        source[token_start].isalnum() or source[token_start] in ("_", "-", ":", ".")
    ):
        token_start -= 1
    token = source[token_start + 1 : token_end + 1]
    return token.rpartition(":")[2] in _NODE_TEST_FUNCTIONS


def _predicate_changes_focus(source: str, predicate_start: int) -> bool:
    index = _previous_nonspace(source, predicate_start - 1)
    while index >= 0 and source[index] == "]":
        opening = enclosing_predicate_start(source, index)
        if opening is None:
            return False
        index = _previous_nonspace(source, opening - 1)
    if index < 0:
        return False
    char = source[index]
    if char == ".":
        return index > 0 and source[index - 1] == "."
    if char == ")":
        return _paren_is_node_test(source, index)
    if char == "*" or char.isalnum() or char in ("_", ":"):
        return _token_axis_changes_focus(source, index)
    return False


def same_context_accumulator_call(source: str, start: int) -> bool:
    """Whether an accumulator call's focus is the rule's own context node.

    A call inside a predicate attached to a path step (including chained
    predicates and calls nested in functions/operators within the predicate)
    addresses the step's item; ``.`` and ``self::`` predicates keep the same
    node.  A call reached through ``/`` or ``!`` outside a predicate may address
    a different node.  Any other position is treated as same-context.  This
    conservative lexical check keeps the common self-cycle static while
    deferring focus-changing recurrences to the runtime partial-value cache.
    """
    predicate = enclosing_predicate_start(source, start)
    if predicate is not None:
        return not _predicate_changes_focus(source, predicate)
    index = _previous_nonspace(source, start - 1)
    if index < 0:
        return True
    return source[index] not in ("!", "/")


K = TypeVar("K", bound=Hashable)


def find_cycle(graph: Mapping[K, Iterable[K]]) -> tuple[K, ...] | None:
    """Return one dependency cycle path, or ``None`` for an acyclic graph."""
    state: dict[K, int] = {}
    positions: dict[K, int] = {}
    stack: list[K] = []

    def visit(node: K) -> tuple[K, ...] | None:
        status = state.get(node, 0)
        if status == 1:
            return (*stack[positions[node] :], node)
        if status == 2:
            return None
        state[node] = 1
        positions[node] = len(stack)
        stack.append(node)
        for child in graph.get(node, ()):
            cycle = visit(child)
            if cycle is not None:
                return cycle
        stack.pop()
        positions.pop(node, None)
        state[node] = 2
        return None

    for node in graph:
        cycle = visit(node)
        if cycle is not None:
            return cycle
    return None
