"""Immutable RELAX NG pattern IR, smart constructors, interning, and nullability.

The simplified syntax of RELAX NG 1.0 is a small algebraic pattern language.
This module holds the semantic nodes only: source origins live in the
:class:`~pyxsd.relaxng.origins.OriginTable` stored beside each interned node,
never inside the node, so identical structures authored in different places
share one semantic node.

``Choice``, ``Group`` and ``Interleave`` are binary, matching the simple
syntax and James Clark's derivative algorithm.  The lowercase constructors
(:func:`choice`, :func:`group`, :func:`interleave`, :func:`one_or_more`) are
the supported way to build patterns: they apply the algebraic identities for
``empty`` and ``notAllowed``, and :func:`choice` flattens, deduplicates, and
orders its leaves deterministically.  Direct construction with the node
classes remains available for tests and for rebuilding an existing graph.

A legal recursive element definition makes the pattern graph cyclic.  The
internal :class:`Ref` edge closes that cycle: reference resolution binds it
once, and nullability traverses it under an identity memo, so the least fixed
point is reached without unbounded recursion.

``Data``, ``DataExcept``, and ``Value`` carry the compiled datatype contract
of the datatype stage in an opaque ``datatype`` field.  The contract must be
hashable and value-comparable, and its ``repr`` must be deterministic because
canonical choice order sorts non-empty leaves by ``repr``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, fields

from pyxsd.relaxng.limits import BudgetCounter
from pyxsd.relaxng.names import NameClass
from pyxsd.relaxng.origins import OriginTable

__all__ = [
    "Attribute",
    "Choice",
    "Data",
    "DataExcept",
    "Element",
    "Empty",
    "Group",
    "Interleave",
    "List",
    "NotAllowed",
    "OneOrMore",
    "Pattern",
    "PatternPool",
    "Ref",
    "Text",
    "Value",
    "choice",
    "group",
    "interleave",
    "nullable",
    "one_or_more",
]


class Pattern:
    """One node of the simplified RELAX NG pattern graph."""


@dataclass(frozen=True, eq=False, repr=False)
class _StructuralPattern(Pattern):
    """Dataclass value operations without Python recursion through operands.

    The hash is the same field-tuple hash a frozen dataclass would generate.
    Caching it beside the immutable fields makes repeated pool lookups cheap;
    neither the cache nor source origins participate in semantic identity.
    Ref deliberately does not inherit these operations: it remains opaque.
    """

    def __hash__(self) -> int:
        pending = [(self, False)]
        while pending:
            node, ready = pending.pop()
            if "_semantic_hash" in node.__dict__:
                continue
            values = tuple(getattr(node, item.name) for item in fields(node))
            if not ready:
                pending.append((node, True))
                pending.extend(
                    (value, False) for value in values if isinstance(value, _StructuralPattern)
                )
            else:
                object.__setattr__(node, "_semantic_hash", hash(values))
        return self.__dict__["_semantic_hash"]

    def __getstate__(self) -> dict[str, object]:
        # Python salts field hashes per process; never serialize that cache.
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return NotImplemented
        pending = [(self, other)]
        seen: set[tuple[int, int]] = set()
        while pending:
            first, second = pending.pop()
            key = (id(first), id(second))
            if first is second or key in seen:
                continue
            seen.add(key)
            if type(first) is not type(second):
                return False
            for item in fields(first):
                left, right = getattr(first, item.name), getattr(second, item.name)
                if isinstance(left, _StructuralPattern) and isinstance(right, _StructuralPattern):
                    pending.append((left, right))
                elif (left,) != (right,):
                    return False
        return True

    def __repr__(self) -> str:
        parts: list[str] = []
        pending: list[str | _StructuralPattern] = [self]
        while pending:
            value = pending.pop()
            if isinstance(value, _StructuralPattern):
                parts.append(f"{type(value).__qualname__}(")
                pending.append(")")
                items = fields(value)
                for index in range(len(items) - 1, -1, -1):
                    item = items[index]
                    operand = getattr(value, item.name)
                    pending.append(
                        operand if isinstance(operand, _StructuralPattern) else repr(operand)
                    )
                    pending.append(f"{item.name}=")
                    if index:
                        pending.append(", ")
            else:
                parts.append(value)
        return "".join(parts)


@dataclass(frozen=True, eq=False, repr=False)
class Empty(_StructuralPattern):
    """Matches the empty sequence."""


@dataclass(frozen=True, eq=False, repr=False)
class NotAllowed(_StructuralPattern):
    """Matches nothing."""


@dataclass(frozen=True, eq=False, repr=False)
class Text(_StructuralPattern):
    """Matches any run of character data."""


@dataclass(frozen=True, eq=False, repr=False)
class Choice(_StructuralPattern):
    """Matches either operand."""

    first: Pattern
    second: Pattern


@dataclass(frozen=True, eq=False, repr=False)
class Group(_StructuralPattern):
    """Matches the first operand followed by the second."""

    first: Pattern
    second: Pattern


@dataclass(frozen=True, eq=False, repr=False)
class Interleave(_StructuralPattern):
    """Matches the operands in any interleaving."""

    first: Pattern
    second: Pattern


@dataclass(frozen=True, eq=False, repr=False)
class OneOrMore(_StructuralPattern):
    """Matches one or more repetitions of its operand."""

    pattern: Pattern


@dataclass(frozen=True, eq=False, repr=False)
class Element(_StructuralPattern):
    """Matches an element with a name in *name_class* and *pattern* content."""

    name_class: NameClass
    pattern: Pattern


@dataclass(frozen=True, eq=False, repr=False)
class Attribute(_StructuralPattern):
    """Matches an attribute with a name in *name_class* and *pattern* value."""

    name_class: NameClass
    pattern: Pattern


@dataclass(frozen=True, eq=False, repr=False)
class List(_StructuralPattern):
    """Matches whitespace-delimited tokens against *pattern*."""

    pattern: Pattern


@dataclass(frozen=True, eq=False, repr=False)
class Data(_StructuralPattern):
    """Matches a lexical value allowed by *datatype*."""

    datatype: object


@dataclass(frozen=True, eq=False, repr=False)
class DataExcept(_StructuralPattern):
    """Matches a value allowed by *datatype* and not by *excluded*."""

    datatype: object
    excluded: Pattern


@dataclass(frozen=True, eq=False, repr=False)
class Value(_StructuralPattern):
    """Matches a value equal to the schema-side *lexical* value.

    The compiled *datatype* retains the schema-side context needed to
    interpret context-sensitive types such as QNames; the lexical string is
    kept verbatim beside it.
    """

    datatype: object
    lexical: str


class Ref(Pattern):
    """An internal write-once edge that closes a legal recursive graph.

    Reference resolution replaces a reference to a named element definition
    with a reference to that element pattern, which can make the graph
    cyclic.  This edge is not an authored RELAX NG pattern and is opaque to
    interning.  Composition binds it exactly once, before the graph is used.
    """

    def __init__(self) -> None:
        self._target: Pattern | None = None

    def bind(self, target: Pattern) -> None:
        """Point this reference at *target*; allowed only once."""
        if self._target is not None:
            raise ValueError("RELAX NG pattern reference is already bound")
        self._target = target

    @property
    def target(self) -> Pattern:
        """The pattern this reference points at."""
        if self._target is None:
            raise ValueError("RELAX NG pattern reference is unbound")
        return self._target

    def __repr__(self) -> str:
        return f"Ref({id(self):x})"


def _choice_leaves(pattern: Pattern) -> Iterator[Pattern]:
    """Yield the flattened choice leaves of *pattern*, excluding notAllowed."""
    pending = [pattern]
    while pending:
        node = pending.pop()
        if isinstance(node, Choice):
            pending.append(node.second)
            pending.append(node.first)
        elif not isinstance(node, NotAllowed):
            yield node


def _choice_key(pattern: Pattern) -> tuple[bool, str]:
    """Empty first, then the existing deterministic representation order."""
    return not isinstance(pattern, Empty), repr(pattern)


def choice(first: Pattern, second: Pattern) -> Pattern:
    """Build a flattened, deduplicated choice: empty first, then repr order."""
    leaves: dict[Pattern, None] = {}
    for pattern in (first, second):
        for leaf in _choice_leaves(pattern):
            leaves.setdefault(leaf, None)
    ordered = sorted(leaves, key=_choice_key)
    if not ordered:
        return NotAllowed()
    result = ordered[0]
    for leaf in ordered[1:]:
        result = Choice(result, leaf)
    return result


def group(first: Pattern, second: Pattern) -> Pattern:
    """Build a binary group with the ``empty``/``notAllowed`` identities."""
    if isinstance(first, NotAllowed) or isinstance(second, NotAllowed):
        return NotAllowed()
    if isinstance(first, Empty):
        return second
    if isinstance(second, Empty):
        return first
    return Group(first, second)


def interleave(first: Pattern, second: Pattern) -> Pattern:
    """Build an interleave with the ``empty``/``notAllowed`` identities."""
    if isinstance(first, NotAllowed) or isinstance(second, NotAllowed):
        return NotAllowed()
    if isinstance(first, Empty):
        return second
    if isinstance(second, Empty):
        return first
    return Interleave(first, second)


def one_or_more(pattern: Pattern) -> Pattern:
    """Build a repetition with the ``empty``/``notAllowed`` identities."""
    if isinstance(pattern, NotAllowed):
        return NotAllowed()
    if isinstance(pattern, Empty):
        return Empty()
    return OneOrMore(pattern)


def nullable(pattern: Pattern) -> bool:
    """Return whether *pattern* matches the empty sequence.

    Nullability is the least fixed point of the pattern algebra: every node
    starts out non-nullable and is re-evaluated until no value changes.  The
    operators are monotone, so the iteration only ever moves ``False`` to
    ``True`` and legal recursive graphs terminate without recursing forever.
    Only dependencies that can affect nullability are traversed; an
    ``element`` or ``attribute`` is non-nullable regardless of its content.
    """
    nodes = _nullability_order(pattern)
    values = {id(node): False for node in nodes}
    while True:
        changed = False
        for node in nodes:
            value = _evaluate_nullable(node, values)
            if values[id(node)] != value:
                values[id(node)] = value
                changed = True
        if not changed:
            return values[id(pattern)]


def _nullability_children(pattern: Pattern) -> tuple[Pattern, ...]:
    """Return the operands that participate in *pattern*'s nullability."""
    if isinstance(pattern, (Choice, Group, Interleave)):
        return (pattern.first, pattern.second)
    if isinstance(pattern, OneOrMore):
        return (pattern.pattern,)
    if isinstance(pattern, Ref):
        return (pattern.target,)
    return ()


def _nullability_order(pattern: Pattern) -> list[Pattern]:
    """Return the reachable nullability dependencies, children first."""
    ordered: list[Pattern] = []
    seen: set[int] = set()
    stack: list[tuple[Pattern, bool]] = [(pattern, False)]
    while stack:
        node, expanded = stack.pop()
        key = id(node)
        if expanded:
            ordered.append(node)
        elif key not in seen:
            seen.add(key)
            stack.append((node, True))
            for child in reversed(_nullability_children(node)):
                stack.append((child, False))
    return ordered


def _evaluate_nullable(pattern: Pattern, values: dict[int, bool]) -> bool:
    if isinstance(pattern, (Empty, Text)):
        return True
    if isinstance(pattern, NotAllowed):
        return False
    if isinstance(pattern, Choice):
        return values[id(pattern.first)] or values[id(pattern.second)]
    if isinstance(pattern, (Group, Interleave)):
        return values[id(pattern.first)] and values[id(pattern.second)]
    if isinstance(pattern, OneOrMore):
        return values[id(pattern.pattern)]
    if isinstance(pattern, Ref):
        return values[id(pattern.target)]
    if isinstance(pattern, (Attribute, Data, DataExcept, Element, List, Value)):
        return False
    raise TypeError(f"unknown RELAX NG pattern {type(pattern).__name__}")


class PatternPool:
    """Semantic interning, origin tables, and the pattern-node budget.

    A pool keeps one canonical instance per semantic pattern.  Origin tables
    are recorded beside the canonical nodes and are never part of their
    identity, so identical structures from different sources intern together
    while their authored origins accumulate.  Every newly interned semantic
    node consumes one unit of the ``max_pattern_nodes`` budget; a cache hit
    consumes nothing.
    """

    def __init__(self, budget: BudgetCounter | None = None) -> None:
        self._budget = budget if budget is not None else BudgetCounter("compile")
        self._nodes: dict[Pattern, Pattern] = {}
        self._origins: dict[Pattern, OriginTable] = {}

    def __len__(self) -> int:
        return len(self._nodes)

    def intern(self, pattern: Pattern, origins: OriginTable | None = None) -> Pattern:
        """Return the canonical node for *pattern* and record *origins*."""
        canonical = self._intern(pattern)
        if origins is not None:
            recorded = self._origins.get(canonical, OriginTable())
            self._origins[canonical] = recorded.union(origins)
        return canonical

    def origins(self, pattern: Pattern) -> OriginTable:
        """Return the recorded origins of a canonical node."""
        return self._origins.get(pattern, OriginTable())

    def _intern(self, pattern: Pattern) -> Pattern:
        completed: dict[int, Pattern] = {}
        pending = [(pattern, False)]
        while pending:
            node, ready = pending.pop()
            canonical = self._nodes.get(node)
            if canonical is not None:
                completed[id(node)] = canonical
                continue
            if not ready:
                pending.append((node, True))
                pending.extend((child, False) for child in reversed(_operands(node)))
                continue
            rebuilt = self._rebuild(node, completed)
            completed[id(node)] = self._store(rebuilt)
        return completed[id(pattern)]

    def _store(self, pattern: Pattern) -> Pattern:
        canonical = self._nodes.get(pattern)
        if canonical is None:
            self._budget.consume("max_pattern_nodes")
            self._nodes[pattern] = pattern
            canonical = pattern
        return canonical

    def _rebuild(self, pattern: Pattern, completed: dict[int, Pattern]) -> Pattern:
        """Intern the children of *pattern* and apply the smart constructors."""
        if isinstance(pattern, Choice):
            first, second = completed[id(pattern.first)], completed[id(pattern.second)]
            last = first.second if isinstance(first, Choice) else first
            # Canonical first operands are already ordered left folds. Appending
            # a larger leaf preserves that form without re-flattening every
            # prefix of a deep choice produced by the smart constructor.
            if (
                not isinstance(first, NotAllowed)
                and not isinstance(second, (Choice, NotAllowed))
                and _choice_key(last) < _choice_key(second)
            ):
                return Choice(first, second)
            result = choice(first, second)
            # Flattening can create prefixes absent from either input tree.
            # Intern those too, so every child of a canonical node is canonical.
            leaves: list[Pattern] = []
            while isinstance(result, Choice):
                leaves.append(result.second)
                result = result.first
            for leaf in reversed(leaves):
                result = self._store(Choice(result, leaf))
            return result
        if isinstance(pattern, Group):
            return group(completed[id(pattern.first)], completed[id(pattern.second)])
        if isinstance(pattern, Interleave):
            return interleave(completed[id(pattern.first)], completed[id(pattern.second)])
        if isinstance(pattern, OneOrMore):
            return one_or_more(completed[id(pattern.pattern)])
        if isinstance(pattern, Element):
            return Element(pattern.name_class, completed[id(pattern.pattern)])
        if isinstance(pattern, Attribute):
            return Attribute(pattern.name_class, completed[id(pattern.pattern)])
        if isinstance(pattern, List):
            return List(completed[id(pattern.pattern)])
        if isinstance(pattern, DataExcept):
            return DataExcept(pattern.datatype, completed[id(pattern.excluded)])
        if isinstance(pattern, (Data, Empty, NotAllowed, Ref, Text, Value)):
            return pattern
        raise TypeError(f"unknown RELAX NG pattern {type(pattern).__name__}")


def _operands(pattern: Pattern) -> tuple[Pattern, ...]:
    """Tree operands only; references close cycles and stay opaque."""
    if isinstance(pattern, (Choice, Group, Interleave)):
        return pattern.first, pattern.second
    if isinstance(pattern, (Element, Attribute, List, OneOrMore)):
        return (pattern.pattern,)
    if isinstance(pattern, DataExcept):
        return (pattern.excluded,)
    return ()
