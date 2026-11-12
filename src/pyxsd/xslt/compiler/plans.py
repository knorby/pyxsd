"""Immutable compiled XSLT plan nodes.

These definitions are the compiled representation shared by the instruction
compiler and the execution runtime.  They contain no compiled expression
logic and no runtime behavior: every plan is a frozen, slotted value whose
fields are set once during compilation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xdm import XdmSequence
    from pyxsd.xpath.environment import Focus, FunctionArity
    from pyxsd.xslt.compiler.context import StaticContext
    from pyxsd.xslt.compiler.expressions import (
        CompiledExpression,
        CompiledSequenceType,
        CompiledValueTemplate,
    )
    from pyxsd.xslt.compiler.instructions import Instruction
    from pyxsd.xslt.compiler.packages import (
        Component,
        ComponentReference,
        ContextItemSignature,
    )
    from pyxsd.xslt.compiler.patterns import CompiledPattern
    from pyxsd.xslt.runtime.accumulators import AccumulatorKey
    from pyxsd.xslt.runtime.iteration import Completion, SortKey
    from pyxsd.xslt.runtime.templates import RuleMatch, TemplateRule

__all__ = [
    "_AnalyzeStringPlan",
    "_ApplyTemplatesPlan",
    "_AssertPlan",
    "_AttributePlan",
    "_BodyPlan",
    "_BreakPlan",
    "_CallTemplatePlan",
    "_CatchPlan",
    "_ChoosePlan",
    "_ComponentPlan",
    "_ConcatPlan",
    "_ConditionalSequencePlan",
    "_CopyOfPlan",
    "_CopyPlan",
    "_DocumentPlan",
    "_ElementPlan",
    "_EvaluatePlan",
    "_FallbackPlan",
    "_ForEachGroupPlan",
    "_ForEachPlan",
    "_ForkPlan",
    "_IteratePlan",
    "_LiteralPlan",
    "_MapEntryPlan",
    "_MapPlan",
    "_MergePlan",
    "_MergeSourcePlan",
    "_ModeLayerPlan",
    "_NextIterationPlan",
    "_NumberPlan",
    "_OnEmptyPlan",
    "_OnNonEmptyPlan",
    "_OverridingPlan",
    "_ParamPlan",
    "_PerformSortPlan",
    "_Plan",
    "_RestoreVariablesPlan",
    "_ResultDocumentPlan",
    "_RuleContext",
    "_SequencePlan",
    "_SourceDocumentPlan",
    "_TextPlan",
    "_TryPlan",
    "_TvtPlan",
    "_UnsupportedPlan",
    "_ValueOfPlan",
    "_VariablePlan",
    "_WherePopulatedPlan",
    "_WithParamPlan",
]


@dataclass(frozen=True, slots=True)
class _TextPlan:
    value: str


@dataclass(frozen=True, slots=True)
class _TvtPlan:
    template: CompiledValueTemplate


@dataclass(frozen=True, slots=True)
class _SequencePlan:
    instruction: Instruction
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _ValueOfPlan:
    instruction: Instruction
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]
    separator: str | CompiledValueTemplate
    explicit_separator: bool = False


@dataclass(frozen=True, slots=True)
class _LiteralPlan:
    instruction: Instruction
    attributes: tuple[tuple[str, str | CompiledValueTemplate], ...]
    children: tuple[_Plan, ...]
    namespaces: tuple[tuple[str, str], ...]
    attribute_sets: tuple[ComponentReference, ...] = ()
    validation: str = "strip"
    type_name: str | None = None


@dataclass(frozen=True, slots=True)
class _ElementPlan:
    instruction: Instruction
    name: CompiledValueTemplate
    namespace: CompiledValueTemplate | None
    namespaces: Mapping[str, str]
    children: tuple[_Plan, ...]
    attribute_sets: tuple[ComponentReference, ...] = ()
    inherit_namespaces: bool = True
    validation: str = "strip"
    type_name: str | None = None


@dataclass(frozen=True, slots=True)
class _DocumentPlan:
    instruction: Instruction
    children: tuple[_Plan, ...]
    validation: str = "strip"
    type_name: str | None = None


@dataclass(frozen=True, slots=True)
class _CopyPlan:
    instruction: Instruction
    select: CompiledExpression | None
    children: tuple[_Plan, ...]
    copy_namespaces: bool = True
    inherit_namespaces: bool = True
    attribute_sets: tuple[ComponentReference, ...] = ()
    validation: str = "strip"
    type_name: str | None = None


@dataclass(frozen=True, slots=True)
class _CopyOfPlan:
    instruction: Instruction
    select: CompiledExpression
    copy_namespaces: bool = True
    copy_accumulators: bool = False
    validation: str = "strip"
    type_name: str | None = None


@dataclass(frozen=True, slots=True)
class _ParamPlan:
    instruction: Instruction
    name: str
    required: bool
    tunnel: bool
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]
    has_as: bool
    sequence_type: CompiledSequenceType | None


@dataclass(frozen=True, slots=True)
class _VariablePlan:
    instruction: Instruction
    name: str
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]
    has_as: bool
    sequence_type: CompiledSequenceType | None


@dataclass(frozen=True, slots=True)
class _AttributePlan:
    instruction: Instruction
    name: str | CompiledValueTemplate
    namespaces: Mapping[str, str]
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]
    namespace: CompiledValueTemplate | None = None
    separator: CompiledValueTemplate | str | None = None
    validation: str = "strip"
    type_name: str | None = None


@dataclass(frozen=True, slots=True)
class _OverridingPlan:
    instruction: Instruction
    kind: Literal["next-match", "apply-imports"]
    parameters: tuple[_WithParamPlan, ...]
    tail: bool = False


@dataclass(frozen=True, slots=True)
class _RestoreVariablesPlan:
    """Runtime-only marker restoring the lexical scope around a nested constructor."""

    variables: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _WithParamPlan:
    name: str
    tunnel: bool
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]
    has_as: bool
    sequence_type: CompiledSequenceType | None


@dataclass(frozen=True, slots=True)
class _CallTemplatePlan:
    instruction: Instruction
    reference: Any
    parameters: tuple[_WithParamPlan, ...]
    tail: bool = False


@dataclass(frozen=True, slots=True)
class _ApplyTemplatesPlan:
    instruction: Instruction
    expression: CompiledExpression
    mode: str | None
    parameters: tuple[_WithParamPlan, ...]
    sorts: tuple[SortKey, ...] = ()
    tail: bool = False


@dataclass(frozen=True, slots=True)
class _ForEachPlan:
    instruction: Instruction
    expression: CompiledExpression
    sorts: tuple[SortKey, ...]
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _IteratePlan:
    instruction: Instruction
    expression: CompiledExpression
    parameters: tuple[_ParamPlan, ...]
    parameter_names: tuple[str, ...]
    on_completion: Completion | None
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _NextIterationPlan:
    instruction: Instruction
    parameters: tuple[_WithParamPlan, ...]


@dataclass(frozen=True, slots=True)
class _BreakPlan:
    instruction: Instruction
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _ForEachGroupPlan:
    instruction: Instruction
    expression: CompiledExpression
    algorithm: str
    key_expression: CompiledExpression | None
    pattern: CompiledPattern | None
    composite: bool
    collation: str | CompiledValueTemplate | None
    sorts: tuple[SortKey, ...]
    children: tuple[_Plan, ...]
    default_collation: str | None = None
    base_uri: str | None = None


@dataclass(frozen=True, slots=True)
class _MergeSourcePlan:
    instruction: Instruction
    name: str | None
    select: CompiledExpression
    keys: tuple[SortKey, ...]
    for_each_item: CompiledExpression | None = None
    for_each_source: CompiledExpression | None = None
    streamable: bool = False
    sort_before_merge: bool = False
    validation: str | None = None
    type_name: str | None = None
    base_uri: str | None = None
    use_accumulators: frozenset[AccumulatorKey] = frozenset()


@dataclass(frozen=True, slots=True)
class _MergePlan:
    instruction: Instruction
    sources: tuple[_MergeSourcePlan, ...]
    action: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _ForkPlan:
    instruction: Instruction
    branches: tuple[tuple[_Plan, ...], ...]


@dataclass(frozen=True, slots=True)
class _SourceDocumentPlan:
    instruction: Instruction
    href: CompiledValueTemplate
    children: tuple[_Plan, ...]
    streamable: bool = False
    validation: str | None = None
    type_name: str | None = None
    use_accumulators: frozenset[AccumulatorKey] = frozenset()


@dataclass(frozen=True, slots=True)
class _PerformSortPlan:
    instruction: Instruction
    expression: CompiledExpression | None
    sorts: tuple[SortKey, ...]
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _ChoosePlan:
    instruction: Instruction
    branches: tuple[tuple[CompiledExpression, tuple[_Plan, ...]], ...]
    otherwise: tuple[_Plan, ...] | None


@dataclass(frozen=True, slots=True)
class _CatchPlan:
    """One compiled ``xsl:catch`` body and its error NameTests."""

    instruction: Instruction
    errors: tuple[str, ...] | None
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _TryPlan:
    """One compiled ``xsl:try`` with its body and ordered catch handlers."""

    instruction: Instruction
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]
    catches: tuple[_CatchPlan, ...]
    rollback_output: bool = True
    tail: bool = False


@dataclass(frozen=True, slots=True)
class _MapPlan:
    instruction: Instruction
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _MapEntryPlan:
    instruction: Instruction
    key: CompiledExpression
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _EvaluatePlan:
    instruction: Instruction
    expression: CompiledExpression
    sequence_type: CompiledSequenceType | None
    base_uri: CompiledValueTemplate | None
    schema_aware: CompiledValueTemplate | None
    context_item: CompiledExpression | None
    namespace_context: CompiledExpression | None
    with_params: tuple[_WithParamPlan, ...]
    with_params_expression: CompiledExpression | None
    xpath_version: str
    enabled: bool
    namespaces: Mapping[str, str]
    default_namespace: str | None
    functions: Mapping[str, FunctionArity]


@dataclass(frozen=True, slots=True)
class _MessagePlan:
    instruction: Instruction
    select: CompiledExpression | None
    children: tuple[_Plan, ...]
    terminate: str | CompiledValueTemplate
    error_code: CompiledValueTemplate | None


@dataclass(frozen=True, slots=True)
class _AssertPlan:
    instruction: Instruction
    test: CompiledExpression
    select: CompiledExpression | None
    children: tuple[_Plan, ...]
    error_code: CompiledValueTemplate | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class _WherePopulatedPlan:
    instruction: Instruction
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _OnEmptyPlan:
    instruction: Instruction
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _OnNonEmptyPlan:
    instruction: Instruction
    expression: CompiledExpression | None
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _ConditionalSequencePlan:
    plans: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _FallbackPlan:
    instruction: Instruction
    children: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _ConcatPlan:
    instruction: Instruction
    parts: tuple[tuple[_Plan, ...], ...]


@dataclass(frozen=True, slots=True)
class _ResultDocumentPlan:
    instruction: Instruction
    href: str | CompiledValueTemplate
    method: str | CompiledValueTemplate | None
    build_tree: str | CompiledValueTemplate | None
    format: str | CompiledValueTemplate | None
    attributes: tuple[tuple[str, str | CompiledValueTemplate], ...]
    children: tuple[_Plan, ...]
    validation: str = "strip"
    type_name: str | None = None


@dataclass(frozen=True, slots=True)
class _AnalyzeStringPlan:
    instruction: Instruction
    expression: CompiledExpression
    regex: CompiledValueTemplate
    flags: CompiledValueTemplate | None
    matching: tuple[_Plan, ...]
    non_matching: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _NumberPlan:
    instruction: Instruction
    value: CompiledExpression | None
    select: CompiledExpression | None
    level: str
    count: CompiledPattern | None
    from_pattern: CompiledPattern | None
    format: CompiledValueTemplate | str
    lang: CompiledValueTemplate | None
    ordinal: CompiledValueTemplate | None
    letter_value: CompiledValueTemplate | None
    grouping_separator: CompiledValueTemplate | None
    grouping_size: CompiledValueTemplate | None
    start_at: CompiledValueTemplate | None


@dataclass(frozen=True, slots=True)
class _UnsupportedPlan:
    instruction: Instruction


_Plan: TypeAlias = (
    _TextPlan
    | _TvtPlan
    | _SequencePlan
    | _ValueOfPlan
    | _LiteralPlan
    | _ElementPlan
    | _DocumentPlan
    | _CopyPlan
    | _CopyOfPlan
    | _ParamPlan
    | _VariablePlan
    | _AttributePlan
    | _WithParamPlan
    | _CallTemplatePlan
    | _ApplyTemplatesPlan
    | _ForEachPlan
    | _IteratePlan
    | _NextIterationPlan
    | _BreakPlan
    | _ForEachGroupPlan
    | _MergePlan
    | _ForkPlan
    | _SourceDocumentPlan
    | _PerformSortPlan
    | _OverridingPlan
    | _RestoreVariablesPlan
    | _ChoosePlan
    | _TryPlan
    | _MapPlan
    | _MapEntryPlan
    | _EvaluatePlan
    | _MessagePlan
    | _AssertPlan
    | _WherePopulatedPlan
    | _OnEmptyPlan
    | _OnNonEmptyPlan
    | _ConditionalSequencePlan
    | _FallbackPlan
    | _ConcatPlan
    | _ResultDocumentPlan
    | _AnalyzeStringPlan
    | _NumberPlan
    | _UnsupportedPlan
)


@dataclass(frozen=True, slots=True)
class _BodyPlan:
    context: StaticContext
    body: tuple[_Plan, ...]
    owner: str | None
    context_item: ContextItemSignature | None
    result_type: CompiledSequenceType | None


@dataclass(frozen=True, slots=True)
class _ModeLayerPlan:
    owner: str | None
    rules: tuple[TemplateRule, ...]


@dataclass(frozen=True, slots=True)
class _ComponentPlan:
    component: Component
    context: StaticContext
    select: CompiledExpression | None
    body: tuple[_Plan, ...]
    has_as: bool
    sequence_type: CompiledSequenceType | None
    attribute_set_parts: tuple[tuple[tuple[ComponentReference, ...], tuple[_Plan, ...]], ...] = ()
    original_functions: Mapping[int, Component] = field(default_factory=dict)
    original_variable: Component | None = None


@dataclass(frozen=True, slots=True)
class _RuleContext:
    match: RuleMatch
    focus: Focus
    mode: str | None
    package_name: str | None
    tunnel_parameters: Mapping[str, XdmSequence]
    mode_package: str | None = None
