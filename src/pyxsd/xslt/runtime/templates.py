"""Compiled template rules, pattern dispatch, and template application.

The index is deliberately policy-free: it splits union patterns into their
normative alternatives, orders candidates by import precedence, priority, and
declaration order, and applies cheap kind/name filters before asking a
:class:`~pyxsd.xslt.compiler.patterns.PatternMatcher` to evaluate a pattern.
Package visibility, modes, ``on-multiple-match``, ``apply-imports``, and
``next-match`` are expressed by :class:`RuleDispatcher`, which composes one
index per package layer using only the compiled component graph.  Runtime
dispatch never changes package visibility or patches a component.

The module also executes ``xsl:apply-templates``, ``xsl:call-template``,
``xsl:next-match``/``xsl:apply-imports``, and the built-in template rules.
Rule frames, template parameter binding, shallow copying, and the apply
scheduler state live here; they use the invocation's shared environment and
frame services rather than a second dispatch path.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
from typing import TYPE_CHECKING, Any, cast

from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xdm.values import XdmArray, XdmAtomic, XdmItem, XdmSequence
from pyxsd.xpath.environment import ExpressionEnvironment, Focus
from pyxsd.xslt.compiler.packages import Component
from pyxsd.xslt.compiler.patterns import (
    CompiledPattern,
    PatternMatcher,
    PatternStatistics,
)
from pyxsd.xslt.compiler.plans import (
    _ApplyTemplatesPlan,
    _BodyPlan,
    _CallTemplatePlan,
    _OverridingPlan,
    _ParamPlan,
    _Plan,
    _RuleContext,
)
from pyxsd.xslt.errors import XsltDynamicError, XsltError
from pyxsd.xslt.precedence import compare_precedence
from pyxsd.xslt.results import _append_complex_content, _NodeFactory
from pyxsd.xslt.runtime.context import EvaluationContext
from pyxsd.xslt.runtime.frames import (
    _INHERIT,
    NO_GROUP,
    Call,
    Continue,
    Frame,
    FrameResult,
)
from pyxsd.xslt.runtime.iteration import sorted_items

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xslt.processor import _Invocation

__all__ = [
    "PatternIndex",
    "RuleDispatcher",
    "RuleMatch",
    "TemplateRule",
    "apply_frame",
    "apply_templates",
    "apply_templates_step",
    "builtin_step",
    "builtin_value",
    "builtin_variant",
    "call_template_step",
    "copy_item",
    "execute_continuation",
    "execute_overriding",
    "overriding_step",
    "resolve_mode",
    "resolve_mode_package",
    "resolve_template",
    "rule_frame",
    "shallow_copy",
    "step_apply",
]


def _is_import_descendant(
    candidate: tuple[int, ...],
    current: tuple[int, ...],
) -> bool:
    """Whether *candidate* lives in a stylesheet level imported by *current*."""
    return len(candidate) > len(current) and candidate[: len(current)] == current


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
        environment_for: Callable[[TemplateRule], ExpressionEnvironment] | None = None,
    ) -> tuple[RuleMatch, ...]:
        """Return every candidate in full conflict-resolution order."""
        matches: list[RuleMatch] = []
        for layer_index, index in self.layers:
            for match in index.matching(
                mode,
                layer_index,
                item,
                environment,
                environment_for=environment_for,
            ):
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
        environment_for: Callable[[TemplateRule], ExpressionEnvironment] | None = None,
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
                for match in index.matching(
                    mode,
                    layer_index,
                    item,
                    environment,
                    environment_for=environment_for,
                )
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
        environment_for: Callable[[TemplateRule], ExpressionEnvironment] | None = None,
    ) -> RuleMatch | None:
        """Return the next candidate after *current* in conflict order."""
        matches = self.matches(item, mode, environment, environment_for)
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
        environment_for: Callable[[TemplateRule], ExpressionEnvironment] | None = None,
    ) -> RuleMatch | None:
        """Return the best rule in a stylesheet level imported by the current one.

        XSLT 3.0 §6.8 considers only descendants of the current rule's
        stylesheet level in the import tree.  Import precedence is the path of
        import counters, so a descendant's path must extend the current path.
        Package layers below the current one are dependency levels and are
        likewise eligible; sibling imports and equal-precedence rules are not.
        """
        matches = self.matches(item, mode, environment, environment_for)
        position = _position(matches, current)
        for match in matches[position + 1 :]:
            if match.layer_index > current.layer_index:
                return match
            if match.layer_index == current.layer_index and _is_import_descendant(
                match.rule.precedence,
                current.rule.precedence,
            ):
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
        *,
        environment_for: Callable[[TemplateRule], ExpressionEnvironment] | None = None,
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
        seen_explicit: set[int] = set()
        for entry in self._groups.get((mode, package_layer), ()):
            alternative = entry.rule.pattern.alternatives[entry.alternative_index]
            if not alternative.filter.matches(item):
                self.statistics.candidate_filters += 1
                continue
            self.statistics.candidate_tests += 1
            matcher = self._matchers[id(entry.rule.pattern)]
            selected_environment = (
                environment_for(entry.rule) if environment_for is not None else environment
            )
            if not matcher.matches_alternative(
                entry.alternative_index,
                item,
                selected_environment,
            ):
                continue
            if entry.rule.priority is not None:
                if id(entry.rule) in seen_explicit:
                    continue
                seen_explicit.add(id(entry.rule))
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
    precedence = compare_precedence(left.rule.precedence, right.rule.precedence)
    if precedence:
        return -precedence
    if left.priority != right.priority:
        return -1 if left.priority > right.priority else 1
    if left.rule.declaration_order != right.rule.declaration_order:
        return -1 if left.rule.declaration_order > right.rule.declaration_order else 1
    if left.alternative_order != right.alternative_order:
        return -1 if left.alternative_order > right.alternative_order else 1
    return 0


# ---------------------------------------------------------------------------
# Template application execution


@dataclass(frozen=True, slots=True)
class _ApplyState:
    items: XdmSequence
    mode: str | None
    template_parameters: Mapping[str, XdmSequence]
    tunnel_parameters: Mapping[str, XdmSequence]
    index: int = 0
    package_name: str | None = None
    mode_package: str | None = None
    group: object = NO_GROUP
    grouping_key: object = NO_GROUP


def _array_member_items(item: XdmArray) -> XdmSequence:
    return tuple(member for sequence in item.members for member in sequence)


def rule_frame(
    invocation: _Invocation,
    match: RuleMatch,
    context: EvaluationContext,
    template_parameters: Mapping[str, XdmSequence],
    *,
    mode_package: str | None = None,
    group: object = _INHERIT,
    grouping_key: object = _INHERIT,
) -> Frame:
    rule = match.rule
    body = cast(_BodyPlan, rule.template)
    rule_focus = invocation._apply_context_item(
        body.context_item, context.focus, package_name=body.owner
    )
    rule_context = replace(
        context,
        focus=rule_focus,
        final_output=True,
        package_name=body.owner,
        mode=rule.mode,
        original_functions={},
    )
    locals_, index = bind_template_parameters(
        invocation,
        body.body,
        template_parameters,
        rule_context,
    )
    conversion = None
    if body.result_type is not None:
        conversion = lambda value: invocation._coerce(  # noqa: E731
            cast(XdmSequence, value),
            body.result_type,
            code="XTTE0505",
            label="template rule result",
            package_name=body.owner,
            clear_output_escaping=not context.final_output,
        )
    active_rule = _RuleContext(
        match=match,
        focus=rule_focus,
        mode=rule.mode,
        package_name=body.owner,
        tunnel_parameters=context.tunnel_parameters,
        mode_package=mode_package,
    )
    effective_group, effective_grouping_key = invocation._inherited_group(group, grouping_key)
    frame = Frame(
        kind="rule",
        component=active_rule,
        identity=f"rule {rule.name or rule.pattern.source}",
        program=body.body[index:],
        context=replace(
            rule_context,
            locals=locals_,
            rule_context=active_rule,
            count_output=context.count_output and conversion is None,
        ),
        location=rule.pattern.context.location,
        conversion=conversion,
        group=effective_group,
        grouping_key=effective_grouping_key,
    )
    return invocation.frame_boundary(frame) if conversion is not None else frame


def bind_template_parameters(
    invocation: _Invocation,
    body: tuple[_Plan, ...],
    template_parameters: Mapping[str, XdmSequence],
    context: EvaluationContext,
) -> tuple[dict[str, XdmSequence], int]:
    locals_: dict[str, XdmSequence] = {}
    index = 0
    while index < len(body) and isinstance(body[index], _ParamPlan):
        parameter = cast(_ParamPlan, body[index])
        supplied = (
            context.tunnel_parameters.get(parameter.name)
            if parameter.tunnel
            else template_parameters.get(parameter.name)
        )
        value = (
            supplied
            if supplied is not None
            else invocation._parameter_default(
                parameter,
                replace(context, locals=locals_),
            )
        )
        value = invocation._coerce_parameter(
            parameter, value, code="XTTE0590", package_name=context.package_name
        )
        invocation._bind(locals_, parameter.name, value)
        index += 1
    return locals_, index


def apply_frame(
    invocation: _Invocation,
    items: XdmSequence,
    mode: str | None,
    template_parameters: Mapping[str, XdmSequence],
    tunnel_parameters: Mapping[str, XdmSequence],
    *,
    tail: bool = False,
    package_name: str | None = None,
    mode_package: str | None = None,
    group: object = _INHERIT,
    grouping_key: object = _INHERIT,
    final_output: bool = True,
    count_output: bool = False,
) -> Frame:
    effective_group, effective_grouping_key = invocation._inherited_group(group, grouping_key)
    state = _ApplyState(
        items=items,
        mode=mode,
        template_parameters=template_parameters,
        tunnel_parameters=tunnel_parameters,
        package_name=package_name,
        mode_package=mode_package,
        group=effective_group,
        grouping_key=effective_grouping_key,
    )
    return Frame(
        kind="apply",
        identity=f"apply-templates mode={mode!r}",
        program=state,
        context=EvaluationContext(
            package_name=package_name,
            mode=mode,
            tunnel_parameters=tunnel_parameters,
            final_output=final_output,
            count_output=count_output,
        ),
        tail=tail,
        group=effective_group,
        grouping_key=effective_grouping_key,
    )


def _item_description(item: XdmItem) -> str:
    """One item's node kind for a diagnostic, or its XDM class for atomics."""
    return item.kind.value if isinstance(item, XdmNode) else type(item).__name__


def step_apply(invocation: _Invocation, frame: Frame) -> FrameResult:
    frame = invocation._consume_input(frame)
    state = cast(_ApplyState, frame.program)
    if state.index >= len(state.items):
        return invocation._return_frame(frame)
    item = state.items[state.index]
    focus = Focus.singleton(item, position=state.index + 1, size=len(state.items))
    dispatcher = invocation._dispatcher(state.mode, state.mode_package)
    properties = invocation._mode_properties(state.mode, state.mode_package)
    rule = dispatcher.select(
        item,
        state.mode,
        invocation._environment(EvaluationContext(package_name=state.package_name)),
        invocation._pattern_environment,
    )
    if rule is not None and _warning_enabled(properties.get("warning-on-multiple-match")):
        candidates = dispatcher.matches(
            item,
            state.mode,
            invocation._environment(EvaluationContext(package_name=state.package_name)),
            invocation._pattern_environment,
        )
        tied = {
            id(candidate.rule)
            for candidate in candidates
            if candidate.rule.precedence == rule.rule.precedence
            and candidate.priority == rule.priority
        }
        if len(tied) > 1:
            invocation.warnings.append(
                "more than one template rule matches "
                f"{_item_description(item)} at priority {rule.priority}"
            )
    elif rule is None and _warning_enabled(properties.get("warning-on-no-match")):
        invocation.warnings.append(f"no template rule matches {_item_description(item)}")
    next_state = replace(state, index=state.index + 1)
    resume = replace(frame, program=next_state)
    last = state.index + 1 == len(state.items)
    tail = frame.tail and last
    if rule is None:
        builtin = builtin_step(invocation, item, state, resume, tail)
        if builtin is not None:
            if frame.context.count_output and isinstance(builtin, Continue):
                invocation._admit_output(builtin.frame.values[len(resume.values) :], frame.context)
            return builtin
        return Continue(replace(frame, program=next_state))
    child = rule_frame(
        invocation,
        rule,
        EvaluationContext(
            focus=focus,
            package_name=state.package_name,
            tunnel_parameters=state.tunnel_parameters,
            final_output=frame.context.final_output,
            count_output=frame.context.count_output,
        ),
        state.template_parameters,
        mode_package=state.mode_package,
        group=state.group,
        grouping_key=state.grouping_key,
    )
    return Call(child, resume=resume, tail=tail)


def _warning_enabled(value: str | None) -> bool:
    return (value or "").strip() in {"yes", "true", "1"}


def builtin_step(
    invocation: _Invocation,
    item: XdmItem,
    state: _ApplyState,
    resume: Frame,
    tail: bool,
) -> FrameResult | None:
    variant = builtin_variant(invocation, state.mode, state.mode_package)
    if variant == "fail":
        raise XsltDynamicError(
            "no template rule matches and the mode forbids the built-in rule",
            code="XTDE0555",
        )
    if isinstance(item, XdmArray):
        if variant in ("deep-copy", "shallow-copy"):
            return Continue(
                replace(
                    resume,
                    values=(
                        *resume.values,
                        copy_item(item, deep=variant == "deep-copy"),
                    ),
                )
            )
        if variant in ("text-only-copy", "shallow-skip"):
            if not item.members:
                return None
            members = _array_member_items(item)
            invocation._check_typed_mode(members, state.mode, state.mode_package)
            child = apply_frame(
                invocation,
                members,
                state.mode,
                state.template_parameters,
                state.tunnel_parameters,
                tail=tail,
                package_name=state.package_name,
                mode_package=state.mode_package,
                group=state.group,
                grouping_key=state.grouping_key,
                final_output=resume.context.final_output,
                count_output=resume.context.count_output,
            )
            return Call(child, resume=resume, tail=tail)
        return None
    if variant == "text-only-copy":
        if isinstance(item, XdmNode) and item.kind in (
            NodeKind.DOCUMENT,
            NodeKind.ELEMENT,
        ):
            if not item.children:
                return None
            invocation._check_typed_mode(item.children, state.mode, state.mode_package)
            child = apply_frame(
                invocation,
                item.children,
                state.mode,
                state.template_parameters,
                state.tunnel_parameters,
                tail=tail,
                package_name=state.package_name,
                mode_package=state.mode_package,
                group=state.group,
                grouping_key=state.grouping_key,
                final_output=resume.context.final_output,
                count_output=resume.context.count_output,
            )
            return Call(child, resume=resume, tail=tail)
        if isinstance(item, XdmNode) and item.kind in (NodeKind.TEXT, NodeKind.ATTRIBUTE):
            return Continue(
                replace(
                    resume,
                    values=(*resume.values, invocation._text_node(item.string_value)),
                )
            )
        if isinstance(item, XdmAtomic):
            return Continue(
                replace(
                    resume,
                    values=(*resume.values, invocation._text_node(item.string_value)),
                )
            )
        return None
    if variant == "deep-skip":
        if isinstance(item, XdmNode) and item.kind is NodeKind.DOCUMENT:
            invocation._check_typed_mode(item.children, state.mode, state.mode_package)
            child = apply_frame(
                invocation,
                item.children,
                state.mode,
                state.template_parameters,
                state.tunnel_parameters,
                tail=tail,
                package_name=state.package_name,
                mode_package=state.mode_package,
                group=state.group,
                grouping_key=state.grouping_key,
                final_output=resume.context.final_output,
                count_output=resume.context.count_output,
            )
            return Call(child, resume=resume, tail=tail)
        return None
    if variant == "shallow-skip":
        if isinstance(item, XdmNode) and item.kind in (
            NodeKind.DOCUMENT,
            NodeKind.ELEMENT,
        ):
            attributes: XdmSequence = ()
            if item.kind is NodeKind.ELEMENT:
                attributes = apply_templates(
                    invocation,
                    item.attributes,
                    state.mode,
                    state.template_parameters,
                    state.tunnel_parameters,
                    package_name=state.package_name,
                    mode_package=state.mode_package,
                )
            if attributes:
                resume = replace(
                    resume,
                    values=resume.values + tuple(attributes),
                )
            if not item.children:
                return Continue(resume)
            invocation._check_typed_mode(item.children, state.mode, state.mode_package)
            child = apply_frame(
                invocation,
                item.children,
                state.mode,
                state.template_parameters,
                state.tunnel_parameters,
                tail=tail,
                package_name=state.package_name,
                mode_package=state.mode_package,
                group=state.group,
                grouping_key=state.grouping_key,
                final_output=resume.context.final_output,
                count_output=resume.context.count_output,
            )
            return Call(child, resume=resume, tail=tail)
        return None
    if variant == "deep-copy":
        return Continue(
            replace(
                resume,
                values=(*resume.values, copy_item(item, deep=True)),
            )
        )
    assert variant == "shallow-copy"
    return Continue(
        replace(
            resume,
            values=(
                *resume.values,
                shallow_copy(invocation, item, state.mode, state.template_parameters, state),
            ),
        )
    )


def builtin_variant(
    invocation: _Invocation, mode: str | None, mode_package: str | None = None
) -> str:
    # Compilation validates the value against the XSLT 3.0 enumeration
    # (XTSE0020), so the runtime selection only reads the stored property.
    return invocation._mode_properties(mode, mode_package).get("on-no-match", "text-only-copy")


def builtin_value(
    invocation: _Invocation,
    item: XdmItem,
    mode: str | None,
    template_parameters: Mapping[str, XdmSequence],
    tunnel_parameters: Mapping[str, XdmSequence],
    package_name: str | None = None,
    mode_package: str | None = None,
) -> XdmSequence:
    """Evaluate the built-in rule for *item* outside the apply frame."""
    variant = builtin_variant(invocation, mode, mode_package)
    if variant == "fail":
        raise XsltDynamicError(
            "no template rule matches and the mode forbids the built-in rule",
            code="XTDE0555",
        )
    if isinstance(item, XdmArray):
        if variant in ("deep-copy", "shallow-copy"):
            return (item,)
        if variant in ("text-only-copy", "shallow-skip"):
            return apply_templates(
                invocation,
                _array_member_items(item),
                mode,
                template_parameters,
                tunnel_parameters,
                package_name=package_name,
                mode_package=mode_package,
            )
        return ()
    if variant == "text-only-copy":
        if isinstance(item, XdmNode):
            if item.kind in (NodeKind.DOCUMENT, NodeKind.ELEMENT):
                return apply_templates(
                    invocation,
                    item.children,
                    mode,
                    template_parameters,
                    tunnel_parameters,
                    package_name=package_name,
                    mode_package=mode_package,
                )
            if item.kind in (NodeKind.TEXT, NodeKind.ATTRIBUTE):
                return (invocation._text_node(item.string_value),)
            return ()
        if isinstance(item, XdmAtomic):
            return (invocation._text_node(item.string_value),)
        return ()
    if variant == "deep-skip":
        if isinstance(item, XdmNode) and item.kind is NodeKind.DOCUMENT:
            return apply_templates(
                invocation,
                item.children,
                mode,
                template_parameters,
                tunnel_parameters,
                package_name=package_name,
                mode_package=mode_package,
            )
        return ()
    if variant == "shallow-skip":
        if isinstance(item, XdmNode) and item.kind in (
            NodeKind.DOCUMENT,
            NodeKind.ELEMENT,
        ):
            values: list[XdmItem] = []
            if item.kind is NodeKind.ELEMENT:
                values.extend(
                    apply_templates(
                        invocation,
                        item.attributes,
                        mode,
                        template_parameters,
                        tunnel_parameters,
                        package_name=package_name,
                        mode_package=mode_package,
                    )
                )
            values.extend(
                apply_templates(
                    invocation,
                    item.children,
                    mode,
                    template_parameters,
                    tunnel_parameters,
                    package_name=package_name,
                    mode_package=mode_package,
                )
            )
            return tuple(values)
        return ()
    if variant == "deep-copy":
        return (copy_item(item, deep=True),)
    return (
        shallow_copy(
            invocation,
            item,
            mode,
            template_parameters,
            _ApplyState(
                (),
                mode,
                template_parameters,
                tunnel_parameters,
                package_name=package_name,
                mode_package=mode_package,
            ),
        ),
    )


def copy_item(item: XdmItem, *, deep: bool) -> XdmItem:
    if not isinstance(item, XdmNode):
        return item
    return _NodeFactory().clone(item, parent=None, deep=deep)


def shallow_copy(
    invocation: _Invocation,
    item: XdmItem,
    mode: str | None,
    template_parameters: Mapping[str, XdmSequence],
    state: _ApplyState,
) -> XdmItem:
    if not isinstance(item, XdmNode):
        return item
    if item.kind is NodeKind.DOCUMENT:
        factory = _NodeFactory(base_uri=item.base_uri)
        document = factory.document
        children = apply_templates(
            invocation,
            item.children,
            mode,
            template_parameters,
            state.tunnel_parameters,
            package_name=state.package_name,
            mode_package=state.mode_package,
        )
        _append_complex_content(factory, document, children, allow_attributes=False)
        document._finalize_document_order()
        return document
    if item.kind is not NodeKind.ELEMENT:
        return _NodeFactory().clone(item, parent=None, deep=False)
    factory = _NodeFactory(base_uri=item.base_uri)
    element = factory.element(
        item.name or "",
        namespaces=dict(item.in_scope_namespaces),
        base_uri=item.base_uri,
        annotation=item.annotation,
        prefix=item.prefix,
    )
    attributes = apply_templates(
        invocation,
        item.attributes,
        mode,
        template_parameters,
        state.tunnel_parameters,
        package_name=state.package_name,
        mode_package=state.mode_package,
    )
    _append_complex_content(factory, element, attributes, allow_attributes=True)
    children = apply_templates(
        invocation,
        item.children,
        mode,
        template_parameters,
        state.tunnel_parameters,
        package_name=state.package_name,
        mode_package=state.mode_package,
    )
    _append_complex_content(factory, element, children, allow_attributes=False)
    return element


def call_template_step(
    invocation: _Invocation,
    plan: _CallTemplatePlan,
    frame: Frame,
    cursor: int,
) -> FrameResult:
    context = frame.context
    template, tunnel = invocation._plan_parameters(plan.parameters, context)
    component = resolve_template(invocation, plan.reference, context.original_components)
    child = invocation._component_frame(
        component,
        template_parameters=template,
        context=replace(
            context,
            tunnel_parameters={**context.tunnel_parameters, **tunnel},
        ),
        group=frame.group,
        grouping_key=frame.grouping_key,
    )
    return Call(
        child,
        resume=replace(frame, cursor=cursor + 1),
        tail=plan.tail,
    )


def apply_templates_step(
    invocation: _Invocation,
    plan: _ApplyTemplatesPlan,
    frame: Frame,
    cursor: int,
    *,
    constructor: bool = False,
) -> FrameResult:
    context = frame.context
    items = invocation._evaluate(
        plan.expression,
        context if constructor else replace(context, original_functions={}),
    )
    if not constructor:
        invocation.profile_for(plan.expression.static_context.xslt_version).check_legacy_node_set(
            items, "the select attribute of xsl:apply-templates"
        )
    items = sorted_items(invocation, items, plan.sorts, context)
    resolved_mode = resolve_mode(invocation, plan.mode, context.mode, context.package_name)
    mode_package = resolve_mode_package(invocation, plan.mode, frame)
    invocation._check_typed_mode(items, resolved_mode, mode_package)
    template, tunnel = invocation._plan_parameters(plan.parameters, context)
    child = apply_frame(
        invocation,
        items,
        resolved_mode,
        template,
        {**context.tunnel_parameters, **tunnel},
        tail=plan.tail,
        package_name=context.package_name,
        mode_package=mode_package,
        group=frame.group,
        grouping_key=frame.grouping_key,
        final_output=context.final_output if constructor else True,
        count_output=context.count_output,
    )
    return Call(
        child,
        resume=replace(frame, cursor=cursor + 1),
        tail=plan.tail,
    )


def overriding_step(
    invocation: _Invocation,
    plan: _OverridingPlan,
    frame: Frame,
    cursor: int,
) -> FrameResult:
    context = frame.context
    rule_context, item, template, effective_tunnel, match = continuation_state(
        invocation,
        plan.kind,
        context,
        plan.parameters,
    )
    if match is None:
        value = builtin_value(
            invocation,
            item,
            rule_context.mode,
            template,
            effective_tunnel,
            rule_context.package_name,
            rule_context.mode_package,
        )
        return Continue(replace(frame, cursor=cursor + 1, values=frame.values + tuple(value)))
    child = rule_frame(
        invocation,
        match,
        replace(
            context,
            focus=rule_context.focus,
            tunnel_parameters=effective_tunnel,
        ),
        template,
        mode_package=rule_context.mode_package,
        group=frame.group,
        grouping_key=frame.grouping_key,
    )
    return Call(
        child,
        resume=replace(frame, cursor=cursor + 1),
        tail=plan.tail,
    )


def execute_overriding(
    plan: _OverridingPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    return execute_continuation(invocation, plan.kind, context, plan.parameters)


def execute_continuation(
    invocation: _Invocation,
    kind: str,
    context: EvaluationContext,
    parameters: tuple[Any, ...] = (),
) -> XdmSequence:
    """Run one ``next-match`` or ``apply-imports`` continuation synchronously."""
    rule_context, item, template, effective_tunnel, match = continuation_state(
        invocation,
        kind,
        context,
        parameters,
    )
    if match is None:
        return builtin_value(
            invocation,
            item,
            rule_context.mode,
            template,
            effective_tunnel,
            rule_context.package_name,
            rule_context.mode_package,
        )
    frame = rule_frame(
        invocation,
        match,
        replace(
            context,
            focus=rule_context.focus,
            tunnel_parameters=effective_tunnel,
        ),
        template,
        mode_package=rule_context.mode_package,
    )
    return cast(XdmSequence, invocation.execute_frame(frame))


def continuation_state(
    invocation: _Invocation,
    kind: str,
    context: EvaluationContext,
    parameters: tuple[Any, ...] = (),
) -> tuple[
    _RuleContext, XdmItem, Mapping[str, XdmSequence], Mapping[str, XdmSequence], RuleMatch | None
]:
    """Resolve the shared state of one rule continuation."""
    rule_context = context.rule_context
    template_parameters, tunnel_parameters = invocation._plan_parameters(parameters, context)
    if rule_context is None:
        raise XsltDynamicError(
            "xsl:next-match and xsl:apply-imports require a current template rule",
            code="XTDE0560",
        )
    item = rule_context.focus.item
    if item is None:  # pragma: no cover - a current rule always has a focus item
        raise XsltDynamicError("the current template rule has no context item")
    effective_tunnel = {
        **rule_context.tunnel_parameters,
        **tunnel_parameters,
    }
    match = continuation_match(invocation, rule_context, kind)
    return rule_context, item, template_parameters, effective_tunnel, match


def continuation_match(
    invocation: _Invocation,
    rule_context: _RuleContext,
    kind: str,
) -> RuleMatch | None:
    """Select the continuation rule for one role-specific operation."""
    dispatcher = invocation._dispatcher(rule_context.mode, rule_context.mode_package)
    environment = invocation._environment(
        EvaluationContext(package_name=rule_context.package_name, final_output=False)
    )
    item = rule_context.focus.item
    assert item is not None
    if kind == "next-match":
        return dispatcher.next_match(
            item,
            rule_context.match,
            rule_context.mode,
            environment,
            invocation._pattern_environment,
        )
    assert kind == "apply-imports"
    return dispatcher.apply_imports(
        item,
        rule_context.match,
        rule_context.mode,
        environment,
        invocation._pattern_environment,
    )


def apply_templates(
    invocation: _Invocation,
    items: XdmSequence,
    mode: str | None,
    template_parameters: Mapping[str, XdmSequence],
    tunnel_parameters: Mapping[str, XdmSequence],
    *,
    package_name: str | None = None,
    mode_package: str | None = None,
    final_output: bool = True,
    count_output: bool = False,
) -> XdmSequence:
    invocation._check_typed_mode(items, mode, mode_package)
    frame = apply_frame(
        invocation,
        items,
        mode,
        template_parameters,
        tunnel_parameters,
        package_name=package_name,
        mode_package=mode_package,
        final_output=final_output,
        count_output=count_output,
    )
    with invocation._merge_scope(None, None, ()):
        return cast(XdmSequence, invocation.execute_frame(frame))


def resolve_template(
    invocation: _Invocation,
    reference: Any,
    originals: Mapping[tuple[str, str | None, int | None], Component] | None = None,
) -> Component:
    if originals is not None and not getattr(reference, "original", False):
        target = originals.get(
            (getattr(reference, "kind", "template"), reference.name, reference.arity)
        )
        if target is not None:
            if target.abstract:
                raise XsltDynamicError(
                    "an invocation of an abstract component is not permitted",
                    code="XTDE3052",
                )
            return target
    stylesheet = getattr(invocation, "stylesheet", None)
    packages = getattr(stylesheet, "_packages", {})
    package = packages.get(
        getattr(reference, "containing", None) or getattr(invocation.package, "name", None)
    )
    if package is None:
        package = invocation.package
    if getattr(reference, "original", False):
        target = package.original_target(
            reference.name,
            kind="template",
        )
        if target is not None:
            return target
    component = package.component("template", reference.name)
    if component is None:
        raise XsltError(
            f"the named template {reference.name!r} is not visible at run time",
            code="XTDE0040",
        )
    return component


def resolve_mode(
    invocation: _Invocation,
    mode: str | None,
    current: str | None,
    package_name: str | None = None,
) -> str | None:
    if mode == "#current":
        return current
    if mode == "#default":
        stylesheet = getattr(invocation, "stylesheet", None)
        packages = getattr(stylesheet, "_packages", {})
        package = packages.get(package_name)
        if package is None:
            package = invocation.package
        return package.default_mode
    return mode


def resolve_mode_package(invocation: _Invocation, mode: str | None, frame: Frame) -> str | None:
    """The package whose composed mode a mode reference selects.

    An explicit mode reference binds in the package containing the
    instruction.  ``#current`` instead keeps the package of the mode
    that selected the current template rule, so a base rule invoked from
    a using package's mode keeps that package's override composition.
    """
    if mode == "#current":
        context = frame.context.rule_context
        return frame.package if context is None else context.mode_package
    return frame.package


def execute_call_template(
    plan: _CallTemplatePlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Run one ``xsl:call-template`` in a synchronous nested constructor."""
    template, tunnel = invocation._plan_parameters(plan.parameters, context)
    component = resolve_template(invocation, plan.reference, context.original_components)
    return invocation._execute_component(
        component,
        template_parameters=template,
        context=replace(
            context,
            tunnel_parameters={**context.tunnel_parameters, **tunnel},
        ),
    )


def execute_apply_templates(
    plan: _ApplyTemplatesPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Run one ``xsl:apply-templates`` in a synchronous nested constructor."""
    items = invocation._evaluate(plan.expression, context)
    items = sorted_items(invocation, items, plan.sorts, context)
    resolved_mode = resolve_mode(
        invocation,
        plan.mode,
        context.mode,
        context.package_name,
    )
    active_rule = context.rule_context
    mode_package = (
        active_rule.mode_package
        if plan.mode == "#current" and active_rule is not None
        else context.package_name
    )
    invocation._check_typed_mode(items, resolved_mode, mode_package)
    template, tunnel = invocation._plan_parameters(plan.parameters, context)
    return apply_templates(
        invocation,
        items,
        resolved_mode,
        template,
        {**context.tunnel_parameters, **tunnel},
        package_name=context.package_name,
        mode_package=mode_package,
        final_output=context.final_output,
        count_output=context.count_output,
    )
