"""Compiled template rules and indexed, candidate-aware pattern dispatch.

The index is deliberately policy-free: it splits union patterns into their
normative alternatives, orders candidates by import precedence, priority, and
declaration order, and applies cheap kind/name filters before asking a
:class:`~pyxsd.xslt.compiler.patterns.PatternMatcher` to evaluate a pattern.
Package visibility, modes, ``on-multiple-match``, ``apply-imports``, and
``next-match`` are expressed by :class:`RuleDispatcher`, which composes one
index per package layer using only the compiled component graph.  Runtime
dispatch never changes package visibility or patches a component.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
from typing import Any

from pyxsd.xdm.values import XdmItem
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xslt.compiler.patterns import (
    CompiledPattern,
    PatternMatcher,
    PatternStatistics,
)
from pyxsd.xslt.errors import XsltDynamicError

__all__ = ["PatternIndex", "RuleDispatcher", "RuleMatch", "TemplateRule"]


def _precedence_compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """Compare import precedence: a shorter prefix is higher; later imports win."""
    if left == right:
        return 0
    limit = min(len(left), len(right))
    if left[:limit] == right[:limit]:
        return 1 if len(left) < len(right) else -1
    return 1 if left > right else -1


def _priority(value: Decimal | str | int | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("a template priority must be a decimal value")
    try:
        priority = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid template priority {value!r}") from exc
    if not priority.is_finite():
        raise ValueError("a template priority must be a finite decimal")
    return priority


@dataclass(frozen=True, slots=True)
class TemplateRule:
    """One ``xsl:template`` pattern rule consumed by :class:`PatternIndex`.

    ``priority`` is the explicit priority, or ``None`` to use each union
    alternative's normative default priority.  ``precedence`` is the import
    precedence path, ``declaration_order`` is the source order within the
    package, and ``package_layer`` is an opaque, comparable layer identity.
    """

    pattern: CompiledPattern
    mode: str | None = None
    package_layer: Hashable | None = None
    precedence: tuple[int, ...] = ()
    declaration_order: int = 0
    priority: Decimal | str | int | None = None
    name: str | None = None
    template: Any = None
    module: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, CompiledPattern):
            raise TypeError("pattern must be a CompiledPattern")
        if self.mode is not None and not isinstance(self.mode, str):
            raise TypeError("mode must be a string or None")
        if self.package_layer is not None:
            try:
                hash(self.package_layer)
            except TypeError as exc:
                raise TypeError("package_layer must be hashable or None") from exc
        if isinstance(self.declaration_order, bool) or not isinstance(self.declaration_order, int):
            raise TypeError("declaration_order must be an integer")
        if not isinstance(self.precedence, tuple) or not all(
            isinstance(step, int) and not isinstance(step, bool) for step in self.precedence
        ):
            raise TypeError("precedence must be a tuple of integers")
        if self.name is not None and not isinstance(self.name, str):
            raise TypeError("name must be a string or None")
        if self.module is not None and not isinstance(self.module, str):
            raise TypeError("module must be a string or None")
        object.__setattr__(self, "priority", _priority(self.priority))


@dataclass(frozen=True, slots=True)
class _IndexedRule:
    rule: TemplateRule
    alternative_index: int
    priority: Decimal
    alternative_order: int


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """One matching template-rule alternative in conflict-resolution order.

    ``layer_index`` identifies the package layer that contributed the rule;
    ``priority`` is the operative priority of this alternative (explicit when
    present, otherwise the pattern alternative's normative default).
    """

    rule: TemplateRule
    alternative_index: int
    priority: Decimal
    layer_index: int = -1


class RuleDispatcher:
    """Package-aware selection, conflict resolution, and rule continuations.

    The dispatcher owns one :class:`PatternIndex` per mode layer, in
    precedence order.  A rule in an earlier layer always outranks one in a
    later layer; within a layer the index applies import precedence, priority,
    and declaration order.  The compiled component graph is fixed: selection
    exposes no override hook and never mutates a rule or package.
    """

    def __init__(
        self,
        layers: Iterable[tuple[int, PatternIndex]],
        *,
        on_multiple_match: str = "use-last",
    ) -> None:
        if on_multiple_match not in ("use-last", "fail"):
            raise ValueError(f"unknown on-multiple-match value {on_multiple_match!r}")
        self.on_multiple_match = on_multiple_match
        self.layers = tuple((layer_index, index) for layer_index, index in layers)
        for layer_index, index in self.layers:
            if isinstance(layer_index, bool) or not isinstance(layer_index, int):
                raise TypeError("a rule layer index must be an integer")
            if not isinstance(index, PatternIndex):
                raise TypeError("a rule layer requires a PatternIndex")

    def matches(
        self,
        item: XdmItem,
        mode: str | None,
        environment: ExpressionEnvironment,
    ) -> tuple[RuleMatch, ...]:
        """Return every candidate in full conflict-resolution order."""
        matches: list[RuleMatch] = []
        for layer_index, index in self.layers:
            for match in index.matching(mode, layer_index, item, environment):
                matches.append(
                    RuleMatch(
                        rule=match.rule,
                        alternative_index=match.alternative_index,
                        priority=match.priority,
                        layer_index=layer_index,
                    )
                )
        return tuple(matches)

    def select(
        self,
        item: XdmItem,
        mode: str | None,
        environment: ExpressionEnvironment,
    ) -> RuleMatch | None:
        """Return the best candidate, applying ``on-multiple-match``."""
        for layer_index, index in self.layers:
            matches = tuple(
                RuleMatch(
                    rule=match.rule,
                    alternative_index=match.alternative_index,
                    priority=match.priority,
                    layer_index=layer_index,
                )
                for match in index.matching(mode, layer_index, item, environment)
            )
            if not matches:
                continue
            selected = matches[0]
            if self.on_multiple_match == "fail":
                tied = {
                    id(match.rule)
                    for match in matches
                    if match.rule.precedence == selected.rule.precedence
                    and match.priority == selected.priority
                }
                if len(tied) > 1:
                    raise XsltDynamicError(
                        "more than one template rule matches with equal precedence and priority",
                        code="XTDE0540",
                    )
            return selected
        return None

    def next_match(
        self,
        item: XdmItem,
        current: RuleMatch,
        mode: str | None,
        environment: ExpressionEnvironment,
    ) -> RuleMatch | None:
        """Return the next candidate after *current* in conflict order."""
        matches = self.matches(item, mode, environment)
        position = _position(matches, current)
        if position + 1 >= len(matches):
            return None
        return matches[position + 1]

    def apply_imports(
        self,
        item: XdmItem,
        current: RuleMatch,
        mode: str | None,
        environment: ExpressionEnvironment,
    ) -> RuleMatch | None:
        """Return the best candidate below the current rule's precedence.

        Candidates in a later package layer are always lower precedence.  In
        the current layer only rules whose import precedence is strictly lower
        are eligible; equal-precedence rules with lower priority are skipped.
        """
        matches = self.matches(item, mode, environment)
        position = _position(matches, current)
        for match in matches[position + 1 :]:
            if match.layer_index > current.layer_index:
                return match
            if _precedence_compare(match.rule.precedence, current.rule.precedence) < 0:
                return match
        return None


def _position(matches: tuple[RuleMatch, ...], current: RuleMatch) -> int:
    for position, match in enumerate(matches):
        if (
            match.rule is current.rule
            and match.alternative_index == current.alternative_index
            and match.layer_index == current.layer_index
        ):
            return position
    raise XsltDynamicError(
        "the current template rule is not a candidate for this item",
        code="XTDE0560",
    )


class PatternIndex:
    """One mode/package-layer index over compiled template rules."""

    def __init__(
        self,
        rules: Iterable[TemplateRule],
        *,
        strategy: str = "optimized",
    ) -> None:
        if strategy not in ("optimized", "forward"):
            raise ValueError(f"unknown pattern matching strategy {strategy!r}")
        self.strategy = strategy
        self.rules = tuple(rules)
        for rule in self.rules:
            if not isinstance(rule, TemplateRule):
                raise TypeError("PatternIndex accepts TemplateRule values")
        self.statistics = PatternStatistics()
        self._matchers: dict[int, PatternMatcher] = {}
        self._groups: dict[tuple[str | None, Hashable | None], tuple[_IndexedRule, ...]] = {}
        group_lists: dict[tuple[str | None, Hashable | None], list[_IndexedRule]] = {}
        for rule in self.rules:
            matcher = PatternMatcher(
                rule.pattern,
                statistics=self.statistics,
                strategy="optimized" if strategy == "optimized" else "forward",
            )
            self._matchers[id(rule.pattern)] = matcher
            for alternative in rule.pattern.alternatives:
                priority = rule.priority
                assert priority is None or isinstance(priority, Decimal)
                entry = _IndexedRule(
                    rule,
                    alternative.index,
                    priority if priority is not None else alternative.priority,
                    alternative.index,
                )
                group_lists.setdefault((rule.mode, rule.package_layer), []).append(entry)
        for key, entries in group_lists.items():
            self._groups[key] = tuple(sorted(entries, key=cmp_to_key(_compare_entries)))

    def candidates(
        self,
        mode: str | None,
        package_layer: Hashable | None,
        item: XdmItem,
    ) -> tuple[TemplateRule, ...]:
        """Return matching-kind candidates in conflict-resolution order."""
        entries = self._groups.get((mode, package_layer), ())
        selected: list[TemplateRule] = []
        seen: set[int] = set()
        for entry in entries:
            alternative = entry.rule.pattern.alternatives[entry.alternative_index]
            if not alternative.filter.matches(item):
                self.statistics.candidate_filters += 1
                continue
            key = id(entry.rule)
            if key in seen:
                continue
            seen.add(key)
            selected.append(entry.rule)
        return tuple(selected)

    def select(
        self,
        item: XdmItem,
        *,
        mode: str | None = None,
        package_layer: Hashable | None = None,
        environment: ExpressionEnvironment | None = None,
    ) -> TemplateRule | None:
        """Return the highest-ordered rule that actually matches *item*."""
        if environment is None:
            environment = ExpressionEnvironment()
        if not isinstance(environment, ExpressionEnvironment):
            raise TypeError("environment must be an ExpressionEnvironment or None")
        for entry in self._groups.get((mode, package_layer), ()):
            alternative = entry.rule.pattern.alternatives[entry.alternative_index]
            if not alternative.filter.matches(item):
                self.statistics.candidate_filters += 1
                continue
            self.statistics.candidate_tests += 1
            matcher = self._matchers[id(entry.rule.pattern)]
            if matcher.matches_alternative(entry.alternative_index, item, environment):
                return entry.rule
        return None

    def matching(
        self,
        mode: str | None,
        package_layer: Hashable | None,
        item: XdmItem,
        environment: ExpressionEnvironment,
    ) -> tuple[RuleMatch, ...]:
        """Return every matching alternative in conflict-resolution order.

        A union rule with no explicit priority is semantically a set of
        adjacent template rules, so a matching rule can occur once per
        alternative.  ``next-match`` needs those entries rather than the
        deduplicated rule list returned by :meth:`candidates`.
        """
        if not isinstance(environment, ExpressionEnvironment):
            raise TypeError("environment must be an ExpressionEnvironment")
        matches: list[RuleMatch] = []
        for entry in self._groups.get((mode, package_layer), ()):
            alternative = entry.rule.pattern.alternatives[entry.alternative_index]
            if not alternative.filter.matches(item):
                self.statistics.candidate_filters += 1
                continue
            self.statistics.candidate_tests += 1
            matcher = self._matchers[id(entry.rule.pattern)]
            if matcher.matches_alternative(entry.alternative_index, item, environment):
                matches.append(
                    RuleMatch(
                        rule=entry.rule,
                        alternative_index=entry.alternative_index,
                        priority=entry.priority,
                    )
                )
        return tuple(matches)

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self) -> Iterator[TemplateRule]:
        return iter(self.rules)


def _compare_entries(left: _IndexedRule, right: _IndexedRule) -> int:
    precedence = _precedence_compare(left.rule.precedence, right.rule.precedence)
    if precedence:
        return -precedence
    if left.priority != right.priority:
        return -1 if left.priority > right.priority else 1
    if left.rule.declaration_order != right.rule.declaration_order:
        return -1 if left.rule.declaration_order > right.rule.declaration_order else 1
    if left.alternative_order != right.alternative_order:
        return -1 if left.alternative_order > right.alternative_order else 1
    return 0
