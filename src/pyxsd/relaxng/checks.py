"""RELAX NG 1.0 schema correctness restrictions on the composed graph.

Section 7 of the RELAX NG specification checks a schema after it has been
transformed into the simple syntax.  This module applies those restrictions
to the final graph produced by :func:`pyxsd.relaxng.simplify.compose_grammars`.
It reports schema-phase findings through a :class:`~pyxsd.validation.ValidationReport`
and never mutates the compiled graph.

The composed graph represents the simple syntax precisely: a :class:`~pyxsd.relaxng.patterns.Ref`
edge is the simple syntax's ``ref`` element, which names one element
definition and has no children in the schema tree.  Every reachable
definition's :class:`~pyxsd.relaxng.patterns.Element` is therefore a separate
element-content root, and no walk here descends through a ``Ref`` target.
Legal recursive graphs close only through those edges, so every traversal is
finite.  Explicit stacks and identity memo tables keep the passes independent
of Python recursion depth and of all projection or derivative budgets.

Datatype payloads are opaque here.  Section 7 classifies ``data``, ``value``
and ``list`` patterns as single-string patterns without inspecting a
datatype's parameters or operations, so an unsupported decode, encode, or
value comparison can never become a schema-correctness finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from pyxsd.locations import IssueLocation
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
    Ref,
    Text,
    Value,
)
from pyxsd.relaxng.simplify import CompiledGrammar
from pyxsd.validation import ValidationReport

__all__ = ["check_compiled_grammar"]

# Contextual positions from section 7.1.  These mirror the specification's
# prohibited paths: the value names the nearest ancestor whose restriction
# applies, not a pattern constructor.
_START_CONTEXT = 0
_ELEMENT_CONTEXT = 1
_REPEAT_CONTEXT = 2
_REPEAT_GROUP_CONTEXT = 3
_REPEAT_INTERLEAVE_CONTEXT = 4
_ATTRIBUTE_CONTEXT = 5
_LIST_CONTEXT = 6
_EXCEPT_CONTEXT = 7

# Content types from section 7.2, ordered as in the specification.
_EMPTY_CONTENT = 0
_COMPLEX_CONTENT = 1
_SIMPLE_CONTENT = 2


def _location(origins: OriginTable) -> IssueLocation | None:
    """Return the most specific deterministic source location for a pattern."""
    locations = [origin.location for origin in origins if origin.location is not None]
    if not locations:
        return None
    return min(locations, key=lambda location: (-len(location.path), _location_key(location)))


def _location_key(location: IssueLocation) -> tuple[str, tuple[tuple[str, int], ...], str]:
    return (
        location.source or "",
        tuple((step.name, step.index) for step in location.path),
        location.attribute or "",
    )


@dataclass(frozen=True)
class _Facts:
    """The patterns that occur in a subtree per the section 7.3 definition."""

    attributes: tuple[NameClass, ...] = ()
    elements: tuple[NameClass, ...] = ()
    text: bool = False


_EMPTY_FACTS = _Facts()


def _open_name_class(name_class: NameClass) -> bool:
    """Return whether *name_class* denotes an infinite set of names."""
    if isinstance(name_class, (AnyName, NsName)):
        return True
    if isinstance(name_class, Name):
        return False
    if isinstance(name_class, NameChoice):
        return any(_open_name_class(item) for item in name_class.alternatives)
    if isinstance(name_class, ExceptName):
        return _open_name_class(name_class.base)
    raise TypeError(f"unknown RELAX NG name class {type(name_class).__name__}")


def _content_children(pattern: Pattern) -> tuple[Pattern, ...]:
    """Return the operands whose content types make up *pattern*'s type."""
    if isinstance(pattern, (Choice, Group, Interleave)):
        return pattern.first, pattern.second
    if isinstance(pattern, OneOrMore):
        return (pattern.pattern,)
    return ()


def _content_leaf(pattern: Pattern) -> tuple[int | None, Pattern]:
    """Return the content type of a pattern that contains no nested pattern."""
    if isinstance(pattern, (Attribute, Empty, NotAllowed)):
        return _EMPTY_CONTENT, pattern
    if isinstance(pattern, (Element, Ref, Text)):
        return _COMPLEX_CONTENT, pattern
    if isinstance(pattern, (Data, DataExcept, List, Value)):
        return _SIMPLE_CONTENT, pattern
    raise TypeError(f"unknown RELAX NG pattern {type(pattern).__name__}")


def _groupable(first: int, second: int) -> bool:
    """Return whether two content types may be grouped or interleaved."""
    return (
        first == _EMPTY_CONTENT
        or second == _EMPTY_CONTENT
        or (first == _COMPLEX_CONTENT and second == _COMPLEX_CONTENT)
    )


def _structural_children(pattern: Pattern) -> tuple[Pattern, ...]:
    """Return the schema-tree children of *pattern*, treating refs as leaves."""
    if isinstance(pattern, (Choice, Group, Interleave)):
        return pattern.first, pattern.second
    if isinstance(pattern, OneOrMore):
        return (pattern.pattern,)
    if isinstance(pattern, (Attribute, Element, List)):
        return (pattern.pattern,)
    if isinstance(pattern, DataExcept):
        return (pattern.excluded,)
    return ()


class _Checker:
    """One run of the unconditional section 7 graph passes."""

    def __init__(self, grammar: CompiledGrammar) -> None:
        self._grammar = grammar
        self._context_seen: set[tuple[int, int]] = set()
        self._content_types: dict[int, tuple[int | None, Pattern]] = {}
        self._content_checked: set[int] = set()
        self._facts: dict[int, _Facts] = {}

    def check(self, report: ValidationReport) -> None:
        self._check_context(report)
        self._check_structure(report)

    # ------------------------------------------------------------------
    # Section 7.1: contextual placement

    def _check_context(self, report: ValidationReport) -> None:
        roots: list[tuple[Pattern, int]] = [(self._grammar.start, _START_CONTEXT)]
        for name in sorted(self._grammar.definitions):
            roots.append((self._grammar.definitions[name].pattern, _ELEMENT_CONTEXT))
        for root, context in roots:
            self._walk_context(root, context, report)

    def _walk_context(self, root: Pattern, context: int, report: ValidationReport) -> None:
        pending: list[tuple[Pattern, int]] = [(root, context)]
        while pending:
            pattern, current = pending.pop()
            key = (id(pattern), current)
            if key in self._context_seen:
                continue
            self._context_seen.add(key)
            pending.extend(reversed(self._context_step(pattern, current, report)))

    def _context_step(
        self, pattern: Pattern, context: int, report: ValidationReport
    ) -> list[tuple[Pattern, int]]:
        """Check one contextual position and return the children to visit."""
        if isinstance(pattern, Choice):
            return [(pattern.first, context), (pattern.second, context)]

        if isinstance(pattern, Group):
            if context == _START_CONTEXT:
                self._error(
                    report,
                    "relaxng-start-contains-group",
                    "the start pattern must not contain a group",
                    pattern,
                )
                return []
            if context == _EXCEPT_CONTEXT:
                self._error(
                    report,
                    "relaxng-except-contains-group",
                    "a data except pattern must not contain a group",
                    pattern,
                )
                return []
            child = _REPEAT_GROUP_CONTEXT if context == _REPEAT_CONTEXT else context
            return [(pattern.first, child), (pattern.second, child)]

        if isinstance(pattern, Interleave):
            if context == _START_CONTEXT:
                self._error(
                    report,
                    "relaxng-start-contains-interleave",
                    "the start pattern must not contain an interleave",
                    pattern,
                )
                return []
            if context == _LIST_CONTEXT:
                self._error(
                    report,
                    "relaxng-list-contains-interleave",
                    "a list pattern must not contain an interleave",
                    pattern,
                )
                return []
            if context == _EXCEPT_CONTEXT:
                self._error(
                    report,
                    "relaxng-except-contains-interleave",
                    "a data except pattern must not contain an interleave",
                    pattern,
                )
                return []
            child = _REPEAT_INTERLEAVE_CONTEXT if context == _REPEAT_CONTEXT else context
            return [(pattern.first, child), (pattern.second, child)]

        if isinstance(pattern, OneOrMore):
            if context == _START_CONTEXT:
                self._error(
                    report,
                    "relaxng-start-contains-one-or-more",
                    "the start pattern must not contain a repetition",
                    pattern,
                )
                return []
            if context == _EXCEPT_CONTEXT:
                self._error(
                    report,
                    "relaxng-except-contains-one-or-more",
                    "a data except pattern must not contain a repetition",
                    pattern,
                )
                return []
            child = _REPEAT_CONTEXT if context == _ELEMENT_CONTEXT else context
            return [(pattern.pattern, child)]

        if isinstance(pattern, List):
            if context == _START_CONTEXT:
                self._error(
                    report,
                    "relaxng-start-contains-list",
                    "the start pattern must not contain a list",
                    pattern,
                )
                return []
            if context == _LIST_CONTEXT:
                self._error(
                    report,
                    "relaxng-list-contains-list",
                    "a list pattern must not contain a list",
                    pattern,
                )
                return []
            if context == _EXCEPT_CONTEXT:
                self._error(
                    report,
                    "relaxng-except-contains-list",
                    "a data except pattern must not contain a list",
                    pattern,
                )
                return []
            return [(pattern.pattern, _LIST_CONTEXT)]

        if isinstance(pattern, Attribute):
            if context == _START_CONTEXT:
                self._error(
                    report,
                    "relaxng-start-contains-attribute",
                    "the start pattern must not contain an attribute",
                    pattern,
                )
                return []
            if context == _ATTRIBUTE_CONTEXT:
                self._error(
                    report,
                    "relaxng-attribute-in-attribute",
                    "an attribute pattern must not contain an attribute",
                    pattern,
                )
                return []
            if context == _LIST_CONTEXT:
                self._error(
                    report,
                    "relaxng-list-contains-attribute",
                    "a list pattern must not contain an attribute",
                    pattern,
                )
                return []
            if context == _EXCEPT_CONTEXT:
                self._error(
                    report,
                    "relaxng-except-contains-attribute",
                    "a data except pattern must not contain an attribute",
                    pattern,
                )
                return []
            if context == _REPEAT_GROUP_CONTEXT:
                self._error(
                    report,
                    "relaxng-attribute-in-repeated-group",
                    "a group of attributes must not be repeated",
                    pattern,
                )
            elif context == _REPEAT_INTERLEAVE_CONTEXT:
                self._error(
                    report,
                    "relaxng-attribute-in-repeated-interleave",
                    "an interleave of attributes must not be repeated",
                    pattern,
                )
            elif context == _ELEMENT_CONTEXT and _open_name_class(pattern.name_class):
                self._error(
                    report,
                    "relaxng-unrepeated-attribute",
                    "an attribute with an open name class must occur in a repetition",
                    pattern,
                )
            self._check_content(pattern, report)
            return [(pattern.pattern, _ATTRIBUTE_CONTEXT)]

        if isinstance(pattern, Element):
            self._check_content(pattern, report)
            return [(pattern.pattern, _ELEMENT_CONTEXT)]

        if isinstance(pattern, Ref):
            if context == _ATTRIBUTE_CONTEXT:
                self._error(
                    report,
                    "relaxng-ref-in-attribute",
                    "an attribute pattern must not contain a reference",
                    pattern,
                )
            elif context == _LIST_CONTEXT:
                self._error(
                    report,
                    "relaxng-list-contains-ref",
                    "a list pattern must not contain a reference",
                    pattern,
                )
            elif context == _EXCEPT_CONTEXT:
                self._error(
                    report,
                    "relaxng-except-contains-ref",
                    "a data except pattern must not contain a reference",
                    pattern,
                )
            return []

        if isinstance(pattern, Text):
            if context == _START_CONTEXT:
                self._error(
                    report,
                    "relaxng-start-contains-text",
                    "the start pattern must not contain text",
                    pattern,
                )
                return []
            if context == _LIST_CONTEXT:
                self._error(
                    report,
                    "relaxng-list-contains-text",
                    "a list pattern must not contain text",
                    pattern,
                )
                return []
            if context == _EXCEPT_CONTEXT:
                self._error(
                    report,
                    "relaxng-except-contains-text",
                    "a data except pattern must not contain text",
                    pattern,
                )
            return []

        if isinstance(pattern, Empty):
            if context == _START_CONTEXT:
                self._error(
                    report,
                    "relaxng-start-contains-empty",
                    "the start pattern must not contain empty",
                    pattern,
                )
            elif context == _EXCEPT_CONTEXT:
                self._error(
                    report,
                    "relaxng-except-contains-empty",
                    "a data except pattern must not contain empty",
                    pattern,
                )
            return []

        if isinstance(pattern, Data):
            if context == _START_CONTEXT:
                self._error(
                    report,
                    "relaxng-start-contains-data",
                    "the start pattern must not contain data",
                    pattern,
                )
            return []

        if isinstance(pattern, DataExcept):
            if context == _START_CONTEXT:
                self._error(
                    report,
                    "relaxng-start-contains-data",
                    "the start pattern must not contain data",
                    pattern,
                )
                return []
            return [(pattern.excluded, _EXCEPT_CONTEXT)]

        if isinstance(pattern, Value):
            if context == _START_CONTEXT:
                self._error(
                    report,
                    "relaxng-start-contains-value",
                    "the start pattern must not contain a value",
                    pattern,
                )
            return []

        if isinstance(pattern, NotAllowed):
            return []

        raise TypeError(f"unknown RELAX NG pattern {type(pattern).__name__}")

    # ------------------------------------------------------------------
    # Section 7.2: string sequences

    def _check_content(self, pattern: Element | Attribute, report: ValidationReport) -> None:
        if id(pattern) in self._content_checked:
            return
        self._content_checked.add(id(pattern))
        content_type, offender = self._content_type(pattern.pattern)
        if content_type is not None:
            return
        subject = "element" if isinstance(pattern, Element) else "attribute"
        self._error(
            report,
            "relaxng-content-type",
            f"the content of this {subject} has no RELAX NG content type",
            offender,
        )

    def _content_type(self, root: Pattern) -> tuple[int | None, Pattern]:
        """Return the content type (or its failing pattern) without recursing."""
        memo = self._content_types
        if id(root) in memo:
            return memo[id(root)]
        pending: list[tuple[Pattern, bool]] = [(root, False)]
        while pending:
            pattern, ready = pending.pop()
            key = id(pattern)
            if key in memo:
                continue
            children = _content_children(pattern)
            if not ready and children:
                pending.append((pattern, True))
                pending.extend(
                    (child, False) for child in reversed(children) if id(child) not in memo
                )
                continue
            if not ready:
                memo[key] = _content_leaf(pattern)
                continue
            memo[key] = self._combine_content(pattern, memo)
        return memo[id(root)]

    @staticmethod
    def _combine_content(
        pattern: Pattern, memo: dict[int, tuple[int | None, Pattern]]
    ) -> tuple[int | None, Pattern]:
        if isinstance(pattern, Choice):
            first = memo[id(pattern.first)]
            second = memo[id(pattern.second)]
            if first[0] is None:
                return first
            if second[0] is None:
                return second
            return max(first[0], second[0]), pattern
        if isinstance(pattern, (Group, Interleave)):
            first = memo[id(pattern.first)]
            second = memo[id(pattern.second)]
            if first[0] is None:
                return first
            if second[0] is None:
                return second
            if not _groupable(first[0], second[0]):
                return None, pattern
            return max(first[0], second[0]), pattern
        # A node with content children is a choice, group, interleave, or
        # repetition; the leaf fall-through in _content_type handles the rest.
        child = memo[id(cast(OneOrMore, pattern).pattern)]
        if child[0] is None:
            return child
        if not _groupable(child[0], child[0]):
            return None, pattern
        return child

    # ------------------------------------------------------------------
    # Sections 7.3 and 7.4: attributes and interleave

    def _check_structure(self, report: ValidationReport) -> None:
        roots: list[Pattern] = [self._grammar.start]
        for name in sorted(self._grammar.definitions):
            roots.append(self._grammar.definitions[name].pattern)
        pending: list[tuple[Pattern, bool]] = [(root, False) for root in reversed(roots)]
        while pending:
            pattern, ready = pending.pop()
            key = id(pattern)
            if key in self._facts:
                continue
            if not ready:
                pending.append((pattern, True))
                pending.extend(
                    (child, False)
                    for child in reversed(_structural_children(pattern))
                    if id(child) not in self._facts
                )
                continue
            facts = self._facts_node(pattern)
            self._facts[key] = facts
            self._check_structure_node(pattern, report)

    def _facts_node(self, pattern: Pattern) -> _Facts:
        if isinstance(pattern, Attribute):
            return _Facts(attributes=(pattern.name_class,))
        if isinstance(pattern, Ref):
            target = cast(Element, pattern.target)
            return _Facts(elements=(target.name_class,))
        if isinstance(pattern, Text):
            return _Facts(text=True)
        if isinstance(pattern, (Choice, Group, Interleave, OneOrMore)):
            children = (
                (pattern.pattern,)
                if isinstance(pattern, OneOrMore)
                else (
                    pattern.first,
                    pattern.second,
                )
            )
            attributes: list[NameClass] = []
            elements: list[NameClass] = []
            text = False
            for child in children:
                facts = self._facts[id(child)]
                attributes.extend(facts.attributes)
                elements.extend(facts.elements)
                text = text or facts.text
            return _Facts(tuple(attributes), tuple(elements), text)
        if isinstance(pattern, (Data, DataExcept, Element, Empty, List, NotAllowed, Value)):
            return _EMPTY_FACTS
        raise TypeError(f"unknown RELAX NG pattern {type(pattern).__name__}")

    def _check_structure_node(self, pattern: Pattern, report: ValidationReport) -> None:
        if not isinstance(pattern, (Group, Interleave)):
            return
        first = self._facts[id(pattern.first)]
        second = self._facts[id(pattern.second)]
        if any(left.overlaps(right) for left in first.attributes for right in second.attributes):
            self._error(
                report,
                "relaxng-duplicate-attribute",
                "the two operands can match the same attribute name",
                pattern,
            )
        if not isinstance(pattern, Interleave):
            return
        if any(left.overlaps(right) for left in first.elements for right in second.elements):
            self._error(
                report,
                "relaxng-interleave-element-name",
                "the two operands can match the same element name",
                pattern,
            )
        if first.text and second.text:
            self._error(
                report,
                "relaxng-interleave-text",
                "text can occur in both operands of interleave",
                pattern,
            )

    # ------------------------------------------------------------------
    # Reporting

    def _error(self, report: ValidationReport, code: str, message: str, pattern: Pattern) -> None:
        report.add_error(
            message,
            code=code,
            phase="schema",
            location=_location(self._grammar.graph.origins(pattern)),
        )


def check_compiled_grammar(grammar: CompiledGrammar, report: ValidationReport) -> None:
    """Report RELAX NG 1.0 schema correctness restrictions for *grammar*.

    The checks run unconditionally on the final composed graph.  They respect
    the specification's set semantics for name classes, treat references as
    childless leaves of the simplified schema tree, and memoize every traversal
    so legal recursive element definitions terminate.  The compiled graph is
    never modified.
    """
    _Checker(grammar).check(report)
