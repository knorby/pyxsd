"""Public XSLT processor, compiled stylesheet, and invocation validation.

The processor and compiled stylesheet are immutable.  Compilation goes through
the Task 12 module front end and the Task 15 package/component graph; mode
patterns use the Task 14 pattern compiler.  Each transformation creates fresh
invocation state and never mutates the compiled artifact.

This first vertical slice executes only the instructions needed for initial
templates and functions, simple value/sequence/literal-result construction, and
explicit document delivery.  Other instruction families fail with an explicit
dynamic error instead of being approximated.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, is_dataclass, replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast
from urllib.parse import urljoin, urlsplit

from elementpath.exceptions import MissingContextError
from elementpath.helpers import is_ncname

from pyxsd.document import Document
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.schema import Schema
from pyxsd.xdm import (
    XdmArray,
    XdmAtomic,
    XdmFunction,
    XdmItem,
    XdmMap,
    XdmSequence,
    as_xdm,
)
from pyxsd.xdm.annotations import builtin_type_classes
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.continuation import ContinuationMachine, HostFunction, Suspension
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, FocusKind, FunctionArity
from pyxsd.xslt.compiler.context import (
    ProfileOptions,
    StaticContext,
    StaticFunction,
    StaticVariable,
)
from pyxsd.xslt.compiler.expressions import (
    CompiledSequenceType,
    CompiledValueTemplate,
    ItemType,
    compile_avt,
    compile_sequence_type,
    compile_tvt,
    compile_xpath,
)
from pyxsd.xslt.compiler.instructions import ComponentReference, Instruction, TextNode
from pyxsd.xslt.compiler.modules import ModuleLoader, SourceModule
from pyxsd.xslt.compiler.packages import (
    CompiledPackage,
    Component,
    ContextItemSignature,
    Mode,
    ModeKey,
    ModeRule,
    PackageResolver,
    compile_package_set,
)
from pyxsd.xslt.compiler.patterns import (
    CompiledPattern,
    PatternMatcher,
    compile_pattern,
)
from pyxsd.xslt.compiler.static import (
    EffectiveAttribute,
    EffectiveElement,
    EffectiveModule,
    StaticPreprocessor,
)
from pyxsd.xslt.errors import ERROR_NAMESPACE, XsltDynamicError, XsltError, XsltStaticError
from pyxsd.xslt.profiles import RecoveryPolicy
from pyxsd.xslt.results import (
    ResultEntry,
    ResultShapeError,
    TransformationResult,
    _append_complex_content,
    _build_result_tree,
    _NodeFactory,
)
from pyxsd.xslt.runtime.evaluate import (
    DynamicExpressionCache,
    StaticContextIdentity,
    evaluate_dynamic,
    with_params_variables,
)
from pyxsd.xslt.runtime.frames import (
    NO_GROUP,
    NO_INPUT,
    Call,
    Continue,
    Frame,
    FrameResult,
    Return,
)
from pyxsd.xslt.runtime.functions import ABSENT, XsltContextBridge
from pyxsd.xslt.runtime.instructions import (
    construct_attribute,
    construct_map,
    construct_map_entry,
    is_deemed_empty,
    is_vacuous,
)
from pyxsd.xslt.runtime.iteration import (
    GROUP_ADJACENT,
    GROUP_BY,
    GROUP_ENDING_WITH,
    GROUP_STARTING_WITH,
    Completion,
    IterationItem,
    IterationState,
    ResolvedSortKey,
    SortKey,
    adjacent_grouping_key,
    atomize_key_sequence,
    group_adjacent,
    group_by,
    group_ending_with,
    group_starting_with,
    grouping_key_set,
    resolve_collation,
    sort_records,
)
from pyxsd.xslt.runtime.templates import (
    PatternIndex,
    RuleDispatcher,
    RuleMatch,
    TemplateRule,
)
from pyxsd.xslt.runtime.transformation import ComponentCall, Transformation

__all__ = [
    "CompiledStylesheet",
    "ResultEntry",
    "ResultShapeError",
    "TransformationResult",
    "XsltInvocationError",
    "XsltProcessor",
]

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XML_NS = "http://www.w3.org/XML/1998/namespace"
FN_NS = "http://www.w3.org/2005/xpath-functions"

_ABSENT = object()
_AbsentType = object
_INHERIT = object()
_DELIVERIES = frozenset({"stylesheet", "raw", "document"})

#: Literal-result attributes whose names are XSLT-defined metadata, not output.
_LITERAL_XSLT_ATTRIBUTES = frozenset(
    {
        "default-collation",
        "default-validation",
        "exclude-result-prefixes",
        "expand-text",
        "extension-element-prefixes",
        "input-type-annotations",
        "use-when",
        "version",
        "xpath-default-namespace",
    }
)

#: Every element name defined by the XSLT 3.0 schema.  A child in the XSLT
#: namespace outside this set is a forwards-compatible candidate, not one of the
#: known instructions this runtime intentionally reports as unimplemented.
_XSLT30_ELEMENTS = frozenset(
    {
        "accept",
        "accumulator",
        "accumulator-rule",
        "analyze-string",
        "apply-imports",
        "apply-templates",
        "assert",
        "attribute",
        "attribute-set",
        "break",
        "call-template",
        "catch",
        "character-map",
        "choose",
        "comment",
        "context-item",
        "copy",
        "copy-of",
        "decimal-format",
        "declaration",
        "document",
        "element",
        "evaluate",
        "expose",
        "fallback",
        "for-each",
        "for-each-group",
        "fork",
        "function",
        "global-context-item",
        "if",
        "import",
        "import-schema",
        "include",
        "instruction",
        "iterate",
        "key",
        "literal-result-element",
        "map",
        "map-entry",
        "matching-substring",
        "merge",
        "merge-action",
        "merge-key",
        "merge-source",
        "message",
        "mode",
        "namespace",
        "namespace-alias",
        "next-iteration",
        "next-match",
        "non-matching-substring",
        "number",
        "on-completion",
        "on-empty",
        "on-non-empty",
        "otherwise",
        "output",
        "output-character",
        "override",
        "package",
        "param",
        "perform-sort",
        "preserve-space",
        "processing-instruction",
        "result-document",
        "sequence",
        "sort",
        "source-document",
        "strip-space",
        "stylesheet",
        "template",
        "text",
        "transform",
        "try",
        "use-package",
        "value-of",
        "variable",
        "when",
        "where-populated",
        "with-param",
    }
)

_PROCESSOR_VERSION = Decimal("3.0")


class XsltInvocationError(ValueError):
    """An invalid host invocation option combination.

    ``code`` carries the XSLT dynamic error code when the specification gives
    one (for example ``XTDE0044`` for an initial mode without a match
    selection).  The `fn:transform` adapter can reuse this validator and map the
    code without conflating source, global context, and selection inputs.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Compiled plans


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


@dataclass(frozen=True, slots=True)
class _UnsupportedPlan:
    instruction: Instruction


_Plan: TypeAlias = (
    _TextPlan
    | _TvtPlan
    | _SequencePlan
    | _ValueOfPlan
    | _LiteralPlan
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
    | _PerformSortPlan
    | _OverridingPlan
    | _RestoreVariablesPlan
    | _ChoosePlan
    | _MapPlan
    | _MapEntryPlan
    | _EvaluatePlan
    | _AssertPlan
    | _WherePopulatedPlan
    | _OnEmptyPlan
    | _OnNonEmptyPlan
    | _ConditionalSequencePlan
    | _FallbackPlan
    | _ConcatPlan
    | _ResultDocumentPlan
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
class _KeyDefinition:
    name: str
    match: CompiledPattern
    use: CompiledExpression
    owner: str | None
    composite: bool = False


@dataclass(frozen=True, slots=True)
class _RuleContext:
    match: RuleMatch
    focus: Focus
    mode: str | None
    package_name: str | None
    tunnel_parameters: Mapping[str, XdmSequence]


@dataclass(frozen=True, slots=True)
class _TemplateContext:
    component: Component
    rule_context: _RuleContext | None
    original_functions: Mapping[int, Component] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _ApplyState:
    items: XdmSequence
    mode: str | None
    template_parameters: Mapping[str, XdmSequence]
    tunnel_parameters: Mapping[str, XdmSequence]
    index: int = 0
    package_name: str | None = None
    group: object = NO_GROUP
    grouping_key: object = NO_GROUP


@dataclass(frozen=True, slots=True)
class _XPathState:
    machine: ContinuationMachine


# ---------------------------------------------------------------------------
# Processor configuration


@dataclass(frozen=True, slots=True)
class XsltProcessor:
    """Immutable processor identity and resource configuration.

    The default is an XSLT 3.0 processor with XPath 3.1 and schema awareness.
    Native XSLT 2.0/1.0 profiles and extension registries belong to later
    tasks; this processor rejects a profile it cannot execute instead of
    silently running different semantics.
    """

    language: str = "3.0"
    xpath: str = "3.1"
    schema_aware: bool = True
    default_collation: str | None = None
    implicit_timezone: Any = None
    resource_policy: Any = None
    xml_policy: Any = None
    xml_limits: Any = None
    resolver: PackageResolver | None = None
    static_options: Mapping[str, Any] = field(default_factory=dict)
    recovery_policy: RecoveryPolicy | None = None

    def __post_init__(self) -> None:
        if self.language != "3.0":
            raise ValueError(f"unsupported XSLT language profile {self.language!r}")
        if self.xpath != "3.1":
            raise ValueError("XSLT 3.0 requires XPath 3.1")
        if not isinstance(self.schema_aware, bool):
            raise TypeError("schema_aware must be a boolean")
        if self.default_collation is not None and not isinstance(self.default_collation, str):
            raise TypeError("default_collation must be a string or None")
        if self.recovery_policy is not None and not isinstance(
            self.recovery_policy, RecoveryPolicy
        ):
            raise TypeError("recovery_policy must be a RecoveryPolicy or None")
        if not isinstance(self.static_options, Mapping):
            raise TypeError("static options must be a mapping")
        options: dict[str, Any] = {}
        for name, value in self.static_options.items():
            if not isinstance(name, str):
                raise TypeError("static option names must be strings")
            options[name] = value
        object.__setattr__(self, "static_options", MappingProxyType(options))
        if self.resource_policy is not None and (
            self.xml_policy is not None or self.xml_limits is not None
        ):
            raise ResourcePolicyError(
                "a resource policy and an explicit parser policy or limits are "
                "mutually exclusive; the policy owns resource handling"
            )

    def _profile(self) -> ProfileOptions:
        return ProfileOptions(
            processor_profile="xslt-3.0",
            schema_aware=self.schema_aware,
            static_options=self.static_options,
        )

    def compile(
        self,
        source: Any,
        *,
        static_parameters: Mapping[str, Any] | None = None,
        schema: Any = None,
    ) -> CompiledStylesheet:
        """Compile one ordinary, simplified, or package stylesheet.

        Compilation completes the module/package closure and every expression,
        pattern, and value template this vertical slice can execute.  A static
        failure raises :class:`~pyxsd.xslt.errors.XsltStaticError`; no partial
        executable artifact is returned.
        """
        if not isinstance(static_parameters, (Mapping, type(None))):
            raise TypeError("static_parameters must be a mapping or None")
        parameters = {} if static_parameters is None else dict(static_parameters)
        pool = self._schema_pool(schema)
        if isinstance(source, EffectiveModule):
            effective = source
        else:
            loader = ModuleLoader(
                self.resource_policy,
                xml_policy=self.xml_policy,
                xml_limits=self.xml_limits,
            )
            loaded: SourceModule = loader.load(source)
            effective = StaticPreprocessor().apply(loaded, parameters)
        resolver = PackageResolver()
        if self.resolver is not None:
            from pyxsd.xslt.compiler.packages import _descriptor

            principal_key = _descriptor(effective).key
            for descriptor in self.resolver:
                if descriptor.key != principal_key:
                    resolver.register(descriptor)
        package = compile_package_set(effective, resolver=resolver)
        return _compile_stylesheet(
            self,
            package,
            effective,
            pool,
            parameters,
        )

    def _schema_pool(self, schema: Any) -> SchemaPool | None:
        if schema is None:
            return None
        if not self.schema_aware:
            raise ValueError("a schema cannot be supplied when schema_aware is False")
        if isinstance(schema, SchemaPool):
            return schema
        if not isinstance(schema, Schema):
            raise TypeError("schema must be a compiled Schema or SchemaPool")
        return SchemaPool.from_schema(schema)


# ---------------------------------------------------------------------------
# Compiled stylesheet


@dataclass(frozen=True, slots=True)
class CompiledStylesheet:
    """One immutable compiled stylesheet or package."""

    processor: XsltProcessor
    package: CompiledPackage
    schema_pool: SchemaPool | None
    profile: ProfileOptions
    static_parameters: Mapping[str, Any]
    _components: Mapping[tuple[str | None, str, str | None, int | None], _ComponentPlan]
    _modes: Mapping[ModeKey, tuple[_ModeLayerPlan, ...]]
    _globals: tuple[Component, ...]
    _package_variables: Mapping[str | None, tuple[Component, ...]]
    _static_variables: Mapping[str | None, Mapping[str, XdmSequence]]
    _packages: Mapping[str | None, CompiledPackage]
    _keys: Mapping[str | None, tuple[_KeyDefinition, ...]]
    _package_modes: Mapping[str | None, Mapping[ModeKey, tuple[_ModeLayerPlan, ...]]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_components", MappingProxyType(dict(self._components)))
        object.__setattr__(self, "_modes", MappingProxyType(dict(self._modes)))
        object.__setattr__(
            self, "static_parameters", MappingProxyType(dict(self.static_parameters))
        )
        object.__setattr__(
            self,
            "_package_variables",
            MappingProxyType(dict(self._package_variables)),
        )
        object.__setattr__(
            self,
            "_static_variables",
            MappingProxyType(
                {
                    name: MappingProxyType(dict(values))
                    for name, values in self._static_variables.items()
                }
            ),
        )
        object.__setattr__(self, "_packages", MappingProxyType(dict(self._packages)))
        object.__setattr__(self, "_keys", MappingProxyType(dict(self._keys)))
        object.__setattr__(
            self,
            "_package_modes",
            MappingProxyType(
                {name: MappingProxyType(dict(modes)) for name, modes in self._package_modes.items()}
            ),
        )

    @property
    def language(self) -> str:
        return self.processor.language

    @property
    def xpath(self) -> str:
        return self.processor.xpath

    @property
    def name(self) -> str | None:
        return self.package.name

    @property
    def version(self) -> str:
        return self.package.version

    @property
    def mode_names(self) -> tuple[str | None, ...]:
        return tuple(self._modes)

    def mode_rule_count(self, mode: str | None = None) -> int:
        """Number of compiled template rules in *mode*, including package layers."""
        name = None if mode in (None, "#unnamed") else _expanded_name(mode, self.package.namespaces)
        return sum(len(layer.rules) for layer in self._modes.get(name, ()))

    def function_available(self, name: str, arity: int) -> bool:
        return self.package.function(name, arity) is not None

    def transform(
        self,
        source: Any = _ABSENT,
        *,
        parameters: Mapping[str, Any] | None = None,
        global_context_item: Any = _ABSENT,
        initial_match_selection: Any = _ABSENT,
        template_parameters: Mapping[str, Any] | None = None,
        tunnel_parameters: Mapping[str, Any] | None = None,
        initial_template: str | None = None,
        initial_mode: str | None = None,
        initial_function: str | None = None,
        initial_function_arguments: Sequence[Any] = (),
        output_uri: str | None = None,
        delivery: str = "stylesheet",
        limits: Any = None,
    ) -> TransformationResult:
        """Run one fresh invocation of this compiled stylesheet."""
        options = _prepare_invocation(
            self,
            source=source,
            parameters=parameters,
            global_context_item=global_context_item,
            initial_match_selection=initial_match_selection,
            template_parameters=template_parameters,
            tunnel_parameters=tunnel_parameters,
            initial_template=initial_template,
            initial_mode=initial_mode,
            initial_function=initial_function,
            initial_function_arguments=initial_function_arguments,
            output_uri=output_uri,
            delivery=delivery,
        )
        return _Invocation(self, options, limits=limits).run()


# ---------------------------------------------------------------------------
# Static compilation


def _compile_stylesheet(
    processor: XsltProcessor,
    package: CompiledPackage,
    principal: EffectiveModule,
    schema: SchemaPool | None,
    static_parameters: Mapping[str, Any],
) -> CompiledStylesheet:
    modules = _module_map(package)
    packages = _package_map(package)
    profile = processor._profile()
    collation = processor.default_collation
    components: dict[tuple[str | None, str, str | None, int | None], _ComponentPlan] = {}
    keys: dict[str | None, list[_KeyDefinition]] = {}
    for package_name, current_package in packages.items():
        for component in current_package.components.values():
            if component.owner != package_name:
                continue
            module = _component_module(component, modules, principal)
            owner_package = packages.get(component.owner, package)
            context = _context_for(
                module,
                component.declaration,
                owner_package,
                schema,
                profile,
                collation,
            )
            attributes = component.declaration
            context = _original_context(context, attributes)
            select_source = attributes.attribute("select")
            if select_source is not None and component.kind != "variable":
                raise XsltStaticError(
                    "select is not permitted on this component declaration",
                    code="XTSE0010",
                    uri=module.identity,
                    location=component.location,
                )
            if component.kind == "key":
                for declaration in component.declarations:
                    keys.setdefault(component.owner, []).append(
                        _compile_key(declaration, component.owner, context, module)
                    )
                continue
            select = (
                compile_xpath(select_source, context, location=component.location)
                if select_source is not None
                else None
            )
            body = _mark_tail(_compile_sequence(component.body.children, context, module))
            _validate_iteration_controls(body, None)
            if select is not None and _has_plan_content(body):
                raise XsltStaticError(
                    "a variable with a select attribute must have empty content",
                    code="XTSE0620",
                    uri=module.identity,
                    location=component.location,
                )
            as_source = attributes.attribute("as")
            attribute_set_parts: tuple[
                tuple[tuple[ComponentReference, ...], tuple[_Plan, ...]], ...
            ] = ()
            if component.kind == "attribute-set":
                attribute_set_parts = _compile_attribute_set_parts(
                    component,
                    context,
                    module,
                )
            original_functions, original_variable = _component_originals(
                component,
                owner_package,
            )
            components[(component.owner, component.kind, component.name, component.arity)] = (
                _ComponentPlan(
                    component=component,
                    context=context,
                    select=select,
                    body=body,
                    has_as=as_source is not None,
                    sequence_type=(
                        compile_sequence_type(as_source, context, location=component.location)
                        if as_source is not None and component.kind == "variable"
                        else None
                    ),
                    attribute_set_parts=attribute_set_parts,
                    original_functions=original_functions,
                    original_variable=original_variable,
                )
            )

    package_modes: dict[str | None, dict[ModeKey, tuple[_ModeLayerPlan, ...]]] = {
        package_name: _compile_package_modes(
            current_package,
            packages,
            modules,
            principal,
            schema,
            profile,
            collation,
        )
        for package_name, current_package in packages.items()
    }
    modes = package_modes.get(package.name, {})
    globals_: list[Component] = []
    package_variables: dict[str | None, tuple[Component, ...]] = {}
    static_variables: dict[str | None, dict[str, XdmSequence]] = {}
    for package_name, current_package in packages.items():
        package_variables[package_name] = tuple(
            component
            for component in current_package.components.values()
            if component.kind == "variable" and not component.hidden
        )
        values: dict[str, XdmSequence] = {}
        for module in current_package.modules:
            for name, value in module.static_variables.items():
                values.setdefault(name, value)
        static_variables[package_name] = values
        for component in current_package.components.values():
            if (
                component.kind != "variable"
                or component.hidden
                or component.owner != package_name
                or component.name is None
            ):
                continue
            globals_.append(component)
    globals_.sort(
        key=lambda component: (
            component.owner is None,
            component.owner or "",
            component.declaration_order,
        )
    )
    return CompiledStylesheet(
        processor=processor,
        package=package,
        schema_pool=schema,
        profile=profile,
        static_parameters=static_parameters,
        _components=components,
        _modes=modes,
        _globals=tuple(globals_),
        _package_variables=package_variables,
        _static_variables=static_variables,
        _packages=packages,
        _keys={name: tuple(definitions) for name, definitions in keys.items()},
        _package_modes=package_modes,
    )


def _compile_package_modes(
    package: CompiledPackage,
    packages: Mapping[str | None, CompiledPackage],
    modules: Mapping[str, EffectiveModule],
    principal: EffectiveModule,
    schema: SchemaPool | None,
    profile: ProfileOptions,
    collation: str | None,
) -> dict[ModeKey, tuple[_ModeLayerPlan, ...]]:
    modes: dict[ModeKey, tuple[_ModeLayerPlan, ...]] = {}
    for mode_name, mode in package.modes.items():
        layers: list[_ModeLayerPlan] = []
        for layer_index, layer in enumerate(mode.rule_layers):
            rules: list[TemplateRule] = []
            for rule in layer.rules:
                module = _rule_module(rule, modules, principal)
                if rule.declaration.attribute("select") is not None:
                    raise XsltStaticError(
                        "select is not permitted on a template rule",
                        code="XTSE0010",
                        uri=module.identity,
                        location=rule.location,
                    )
                owner_package = packages.get(rule.owner, package)
                context = _context_for(
                    module,
                    rule.declaration,
                    owner_package,
                    schema,
                    profile,
                    collation,
                )
                pattern = compile_pattern(rule.pattern, context, location=rule.location)
                body = _mark_tail(_compile_sequence(rule.body.children, context, module))
                _validate_iteration_controls(body, None)
                as_source = rule.declaration.attribute("as")
                rules.append(
                    TemplateRule(
                        pattern=pattern,
                        mode=mode_name,
                        package_layer=layer_index,
                        precedence=rule.precedence,
                        declaration_order=rule.declaration_order,
                        priority=rule.priority,
                        name=rule.name,
                        template=_BodyPlan(
                            context=context,
                            body=body,
                            owner=rule.owner,
                            context_item=_context_item_signature(rule.declaration, context),
                            result_type=(
                                compile_sequence_type(
                                    as_source,
                                    context,
                                    location=rule.location,
                                )
                                if as_source is not None
                                else None
                            ),
                        ),
                        module=rule.module,
                    )
                )
            layers.append(_ModeLayerPlan(owner=layer.owner, rules=tuple(rules)))
        modes[mode_name] = tuple(layers)
    return modes


def _component_originals(
    component: Component,
    package: CompiledPackage,
) -> tuple[Mapping[int, Component], Component | None]:
    """Return original function/variable targets referenced by one override."""
    functions: dict[int, Component] = {}
    variable: Component | None = None
    nodes: list[Any] = [component.declaration, *component.body.children]
    for instruction in _walk_instructions(nodes):
        for reference in instruction.references:
            if not reference.original:
                continue
            if reference.kind == "function" and reference.arity is not None:
                target = package.original_target(
                    reference.name or "",
                    kind="function",
                    arity=reference.arity,
                )
                assert target is not None
                functions[reference.arity] = target
            elif reference.kind == "variable":
                target = package.original_target(reference.name or "", kind="variable")
                assert target is not None
                variable = target
    return MappingProxyType(functions), variable


def _walk_instructions(nodes: Iterable[Any]) -> Iterator[Instruction]:
    for node in nodes:
        if isinstance(node, Instruction):
            yield node
            yield from _walk_instructions(node.children)


def _compile_key(
    declaration: Instruction,
    owner: str | None,
    context: StaticContext,
    module: EffectiveModule,
) -> _KeyDefinition:
    name = declaration.attribute("name")
    match = declaration.attribute("match")
    use = declaration.attribute("use")
    assert name is not None and match is not None and use is not None
    return _KeyDefinition(
        name=_expanded_name(name, declaration.namespaces),
        match=compile_pattern(match, context, location=declaration.location),
        use=compile_xpath(use, context, location=declaration.location),
        owner=owner,
        composite=(declaration.attribute("composite") or "").strip() in ("yes", "true", "1"),
    )


def _compile_attribute_set_parts(
    component: Component,
    context: StaticContext,
    module: EffectiveModule,
) -> tuple[tuple[tuple[ComponentReference, ...], tuple[_Plan, ...]], ...]:
    parts: list[tuple[tuple[ComponentReference, ...], tuple[_Plan, ...]]] = []
    for declaration in component.declarations:
        references = tuple(
            reference for reference in declaration.references if reference.kind == "attribute-set"
        )
        body: list[_Plan] = []
        for child in declaration.children:
            if isinstance(child, TextNode):
                if child.value.strip():
                    raise XsltStaticError(
                        "an attribute set permits only xsl:attribute children",
                        code="XTSE0010",
                        uri=module.identity,
                        location=child.location,
                    )
                continue
            if not (child.is_xslt and child.local_name == "attribute"):
                raise XsltStaticError(
                    "an attribute set permits only xsl:attribute children",
                    code="XTSE0010",
                    uri=module.identity,
                    location=child.location,
                )
            body.append(_compile_instruction(child, context, module))
        _validate_iteration_controls(tuple(body), None)
        parts.append((references, tuple(body)))
    return tuple(parts)


def _module_map(package: CompiledPackage) -> dict[str, EffectiveModule]:
    result: dict[str, EffectiveModule] = {}

    def visit(current: CompiledPackage) -> None:
        for module in current.modules:
            if module.identity is not None:
                result[module.identity] = module
        for dependency in current.dependencies:
            visit(dependency)

    visit(package)
    return result


def _package_map(package: CompiledPackage) -> dict[str | None, CompiledPackage]:
    result: dict[str | None, CompiledPackage] = {}

    def visit(current: CompiledPackage) -> None:
        result.setdefault(current.name, current)
        for dependency in current.dependencies:
            visit(dependency)

    visit(package)
    return result


def _component_module(
    component: Component,
    modules: Mapping[str, EffectiveModule],
    principal: EffectiveModule,
) -> EffectiveModule:
    if component.module is not None and component.module in modules:
        return modules[component.module]
    return principal


def _rule_module(
    rule: ModeRule,
    modules: Mapping[str, EffectiveModule],
    principal: EffectiveModule,
) -> EffectiveModule:
    if rule.module is not None and rule.module in modules:
        return modules[rule.module]
    return principal


def _context_for(
    module: EffectiveModule,
    declaration: Instruction,
    package: CompiledPackage,
    schema: SchemaPool | None,
    profile: ProfileOptions,
    default_collation: str | None,
) -> StaticContext:
    base = StaticContext.for_module(module, schema=schema, profile=profile)
    variables = dict(base.variables)
    functions = dict(base.functions)
    for component in package.components.values():
        if component.kind == "variable" and component.name is not None:
            if component.name not in variables:
                kind = (
                    component.signature.variable_kind
                    if component.signature is not None
                    else "variable"
                )
                variables[component.name] = StaticVariable(
                    name=component.name,
                    kind=cast(Literal["variable", "param"], kind),
                    visibility=component.visibility,
                    module=component.module,
                )
        elif (
            component.kind == "function"
            and component.name is not None
            and component.arity is not None
        ):
            key = (component.name, component.arity)
            if key not in functions:
                functions[key] = StaticFunction(
                    name=component.name,
                    arity=component.arity,
                    visibility=component.visibility,
                    module=component.module,
                )
    context = base.with_variables(variables).with_functions(functions)
    if context.default_collation is None and default_collation is not None:
        context = context.with_default_collation(default_collation)
    return _instruction_context(context, declaration)


def _instruction_context(
    context: StaticContext,
    instruction: Instruction,
) -> StaticContext:
    """Apply one instruction's nearest standard-attribute scope to *context*.

    ``Instruction`` is already the normalized declaration view, so wrapping its
    attributes in an :class:`~pyxsd.xslt.compiler.static.EffectiveElement` lets
    the shared ``StaticContext.for_element`` implementation apply the nearest
    ``[xsl:]version``, ``expand-text``, namespaces, base URI, and other standard
    attributes without duplicating the precedence rules.
    """
    element = EffectiveElement(
        name=instruction.name,
        attributes=tuple(
            EffectiveAttribute(name=attribute.name, value=attribute.value, static=attribute.static)
            for attribute in instruction.attributes
        ),
        children=(),
        raw=None,
        base_uri=instruction.base_uri,
        in_scope_namespaces=instruction.namespaces,
        synthetic=True,
    )
    return StaticContext.for_element(
        element,
        parent=context,
        location=instruction.location,
    )


def _context_item_signature(
    declaration: Instruction,
    context: StaticContext,
) -> ContextItemSignature | None:
    for child in declaration.elements:
        if child.namespace_uri == XSLT_NS and child.local_name == "context-item":
            use = child.attribute("use") or "optional"
            if use not in ("required", "optional", "absent"):
                raise XsltStaticError(
                    f"invalid context-item use {use!r}",
                    code="XTSE0020",
                    uri=context.uri,
                    location=child.location,
                )
            as_source = child.attribute("as")
            if use == "absent" and as_source is not None:
                raise XsltStaticError(
                    "xsl:context-item with use='absent' must not have an as attribute",
                    code="XTSE3088",
                    uri=context.uri,
                    location=child.location,
                )
            return ContextItemSignature(
                use,
                compile_sequence_type(
                    as_source or "item()",
                    context,
                    location=child.location,
                ),
            )
    return None


def _compile_sequence(
    children: Iterable[Any],
    context: StaticContext,
    module: EffectiveModule,
) -> tuple[_Plan, ...]:
    plans: list[_Plan] = []
    for child in children:
        if isinstance(child, Instruction) and child.is_xslt and child.local_name == "context-item":
            continue
        if isinstance(child, TextNode):
            if context.expand_text:
                plans.append(
                    _TvtPlan(
                        compile_tvt(
                            child.value,
                            context,
                            location=child.location,
                        )
                    )
                )
            else:
                plans.append(_TextPlan(child.value))
            continue
        plans.append(_compile_instruction(child, context, module))
    return _conditional_sequence(plans)


def _conditional_sequence(plans: Iterable[_Plan]) -> tuple[_Plan, ...]:
    """Wrap a sequence constructor that contains conditional-content markers."""
    body = tuple(plans)
    if not any(isinstance(plan, (_OnEmptyPlan, _OnNonEmptyPlan)) for plan in body):
        return body
    leading = 0
    while leading < len(body) and isinstance(body[leading], _ParamPlan):
        leading += 1
    parameters = body[:leading]
    content = body[leading:]
    on_empty = next(
        (index for index, plan in enumerate(content) if isinstance(plan, _OnEmptyPlan)),
        None,
    )
    for index, plan in enumerate(content):
        if isinstance(plan, _OnEmptyPlan) and on_empty != index:
            raise XsltStaticError(
                "a sequence constructor permits at most one xsl:on-empty",
                code="XTSE0010",
                uri=plan.instruction.module,
                location=plan.instruction.location,
            )
        if on_empty is not None and index > on_empty:
            if isinstance(plan, _FallbackPlan):
                continue
            instruction = getattr(plan, "instruction", None)
            raise XsltStaticError(
                "xsl:on-empty must be the last instruction in its sequence constructor",
                code="XTSE0010",
                uri=getattr(instruction, "module", None),
                location=getattr(instruction, "location", None),
            )
    return (*parameters, _ConditionalSequencePlan(content))


def _compile_with_params(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
    *,
    children: Iterable[Any] | None = None,
) -> tuple[_WithParamPlan, ...]:
    result: list[_WithParamPlan] = []
    source = instruction.children if children is None else children
    for child in source:
        if isinstance(child, TextNode):
            raise XsltStaticError(
                "xsl:with-param is the only permitted content here",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        if not (child.is_xslt and child.local_name == "with-param"):
            raise XsltStaticError(
                "xsl:with-param is the only permitted content here",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        result.append(_compile_parameter(child, context, module))
    return tuple(result)


def _compile_parameter(
    child: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _WithParamPlan:
    name = child.attribute("name")
    if not name:
        raise XsltStaticError(
            "xsl:with-param requires a name",
            code="XTSE0010",
            uri=context.uri,
            location=child.location,
        )
    select = child.attribute("select")
    children = _compile_sequence(child.children, context, module)
    if select is not None and _has_plan_content(children):
        raise XsltStaticError(
            "a parameter with a select attribute must have empty content",
            code="XTSE0620",
            uri=context.uri,
            location=child.location,
        )
    as_source = child.attribute("as")
    return _WithParamPlan(
        name=_expanded_name(name, child.namespaces),
        tunnel=_attribute_yes_no(child, "tunnel", context),
        expression=(
            compile_xpath(select, context, location=child.location) if select is not None else None
        ),
        children=children,
        has_as=as_source is not None,
        sequence_type=(
            compile_sequence_type(as_source, context, location=child.location)
            if as_source is not None
            else None
        ),
    )


def _instruction_mode(instruction: Instruction) -> str | None:
    value = instruction.attribute("mode")
    if value is None:
        return "#default"
    normalized = value.strip()
    if normalized == "#unnamed":
        return None
    if normalized in ("#current", "#default"):
        return normalized
    return _expanded_name(normalized, instruction.namespaces)


def _mark_tail(plans: tuple[_Plan, ...]) -> tuple[_Plan, ...]:
    """Mark semantically safe tail calls in one sequence constructor.

    Only the final plan of a sequence is eligible.  The marker is propagated
    through control-flow and nested sequence constructors, but never through a
    literal result element, value template, parameter default, or result
    document because those have pending construction or conversion work.
    """
    if not plans:
        return plans
    body = list(plans)
    body[-1] = _tail_marked_plan(body[-1])
    return tuple(body)


def _tail_marked_plan(plan: _Plan) -> _Plan:
    if isinstance(plan, (_CallTemplatePlan, _ApplyTemplatesPlan, _OverridingPlan)):
        return replace(plan, tail=True)
    if isinstance(plan, _ChoosePlan):
        return replace(
            plan,
            branches=tuple((test, _mark_tail(branch)) for test, branch in plan.branches),
            otherwise=(None if plan.otherwise is None else _mark_tail(plan.otherwise)),
        )
    if isinstance(plan, _SequencePlan) and plan.expression is None and plan.children:
        return replace(plan, children=_mark_tail(plan.children))
    return plan


def _exclusions(module: EffectiveModule) -> tuple[str, ...]:
    value = module.root.attribute("exclude-result-prefixes") or ""
    return tuple(value.split())


def _original_context(context: StaticContext, instruction: Instruction) -> StaticContext:
    """Declare the lexical ``xsl:original`` symbol used by an override expression."""
    variables = dict(context.variables)
    functions = dict(context.functions)
    variable_name = f"{{{XSLT_NS}}}original"
    function_name = f"{{{XSLT_NS}}}original"
    for reference in instruction.references:
        if not reference.original:
            continue
        if reference.kind == "variable":
            variables.setdefault(
                variable_name,
                StaticVariable(name=variable_name, kind="variable", visibility="private"),
            )
        elif reference.kind == "function" and reference.arity is not None:
            functions.setdefault(
                (function_name, reference.arity),
                StaticFunction(
                    name=function_name,
                    arity=reference.arity,
                    visibility="private",
                ),
            )
    if len(variables) == len(context.variables) and len(functions) == len(context.functions):
        return context
    return context.with_variables(variables).with_functions(functions)


def _split_sort_children(
    instruction: Instruction,
) -> tuple[tuple[Instruction, ...], tuple[Any, ...]]:
    """Split leading ``xsl:sort`` children from one instruction's content."""
    sorts: list[Instruction] = []
    body: list[Any] = []
    pending_whitespace: list[Any] = []
    content_started = False
    for child in instruction.children:
        if not content_started and isinstance(child, TextNode) and not child.value.strip():
            pending_whitespace.append(child)
            continue
        if (
            not content_started
            and isinstance(child, Instruction)
            and child.is_xslt
            and child.local_name == "sort"
        ):
            sorts.append(child)
            pending_whitespace.clear()
            continue
        content_started = True
        body.extend(pending_whitespace)
        pending_whitespace.clear()
        body.append(child)
    if not content_started:
        body.extend(pending_whitespace)
    return tuple(sorts), tuple(body)


def _compile_sort_keys(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> tuple[SortKey, ...]:
    sorts, _ = _split_sort_children(instruction)
    result: list[SortKey] = []
    for index, sort in enumerate(sorts):
        if index > 0 and sort.attribute("stable") is not None:
            raise XsltStaticError(
                "only the first xsl:sort in a sort key specification may have stable",
                code="XTSE1017",
                uri=context.uri,
                location=sort.location,
            )
        result.append(_compile_sort_key(sort, context, module))
    return tuple(result)


def _compile_sort_key(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> SortKey:
    context = _instruction_context(context, instruction)
    context = _original_context(context, instruction)
    select = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select is not None and _has_plan_content(children, allow_fallback=True):
        raise XsltStaticError(
            "xsl:sort with a select attribute must have empty content",
            code="XTSE1015",
            uri=context.uri,
            location=instruction.location,
        )
    expression = (
        compile_xpath(select, context, location=instruction.location)
        if select is not None
        else (
            compile_xpath(".", context, location=instruction.location)
            if not _has_plan_content(children, allow_fallback=True)
            else None
        )
    )

    def avt(name: str) -> CompiledValueTemplate | None:
        value = instruction.attribute(name)
        return (
            compile_avt(value, context, location=instruction.location)
            if value is not None
            else None
        )

    return SortKey(
        instruction=instruction,
        select=expression,
        children=() if select is not None else children,
        lang=avt("lang"),
        order=avt("order"),
        collation=avt("collation"),
        stable=avt("stable"),
        case_order=avt("case-order"),
        data_type=avt("data-type"),
        base_uri=instruction.base_uri,
        xslt_version=context.version,
    )


def _compile_completion(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> Completion:
    context = _instruction_context(context, instruction)
    select_source = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select_source is not None and _has_plan_content(children, allow_fallback=True):
        raise XsltStaticError(
            f"xsl:{instruction.local_name} with a select attribute must have "
            "empty content except for xsl:fallback",
            code="XTSE3125",
            uri=context.uri,
            location=instruction.location,
        )
    return Completion(
        instruction=instruction,
        select=(
            compile_xpath(select_source, context, location=instruction.location)
            if select_source is not None
            else None
        ),
        children=children,
    )


def _compile_iterate(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _IteratePlan:
    select_source = instruction.attribute("select")
    if select_source is None:
        raise XsltStaticError(
            "xsl:iterate requires a select attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    context = _instruction_context(context, instruction)
    parameters: list[_ParamPlan] = []
    body_children: list[Any] = []
    on_completion: Completion | None = None
    body_started = False
    for child in instruction.children:
        if isinstance(child, TextNode):
            body_started = True
            body_children.append(child)
            continue
        if child.is_xslt and child.local_name == "param":
            if body_started or on_completion is not None:
                raise XsltStaticError(
                    "xsl:param children of xsl:iterate must precede all other content",
                    code="XTSE0010",
                    uri=context.uri,
                    location=child.location,
                )
            parameters.append(cast(_ParamPlan, _compile_instruction(child, context, module)))
            continue
        if child.is_xslt and child.local_name == "on-completion":
            if body_started:
                raise XsltStaticError(
                    "xsl:on-completion must precede the xsl:iterate sequence constructor",
                    code="XTSE0010",
                    uri=context.uri,
                    location=child.location,
                )
            if on_completion is not None:
                raise XsltStaticError(
                    "xsl:iterate permits at most one xsl:on-completion",
                    code="XTSE0010",
                    uri=context.uri,
                    location=child.location,
                )
            on_completion = _compile_completion(child, context, module)
            continue
        body_started = True
        body_children.append(child)
    parameter_names: list[str] = []
    for parameter in parameters:
        if parameter.name in parameter_names:
            raise XsltStaticError(
                f"the iteration parameter {parameter.name!r} is declared more than once",
                code="XTSE0580",
                uri=context.uri,
                location=parameter.instruction.location,
            )
        parameter_names.append(parameter.name)
    body = _mark_tail(_compile_sequence(body_children, context, module))
    _validate_iteration_controls(body, tuple(parameter_names))
    return _IteratePlan(
        instruction=instruction,
        expression=compile_xpath(select_source, context, location=instruction.location),
        parameters=tuple(parameters),
        parameter_names=tuple(parameter_names),
        on_completion=on_completion,
        children=body,
    )


def _compile_for_each_group(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _ForEachGroupPlan:
    select_source = instruction.attribute("select")
    if select_source is None:
        raise XsltStaticError(
            "xsl:for-each-group requires a select attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    context = _instruction_context(context, instruction)
    algorithms = [
        name
        for name in (GROUP_BY, GROUP_ADJACENT, GROUP_STARTING_WITH, GROUP_ENDING_WITH)
        if instruction.attribute(name) is not None
    ]
    if len(algorithms) != 1:
        raise XsltStaticError(
            "xsl:for-each-group requires exactly one grouping algorithm",
            code="XTSE1080",
            uri=context.uri,
            location=instruction.location,
        )
    algorithm = algorithms[0]
    collation_source = instruction.attribute("collation")
    composite_source = instruction.attribute("composite")
    composite = _attribute_yes_no(instruction, "composite", context)
    if algorithm in (GROUP_STARTING_WITH, GROUP_ENDING_WITH) and (
        collation_source is not None or composite_source is not None
    ):
        raise XsltStaticError(
            "collation and composite are not permitted with this grouping algorithm",
            code="XTSE1090",
            uri=context.uri,
            location=instruction.location,
        )
    key_expression = None
    pattern = None
    source = instruction.attribute(algorithm)
    assert source is not None
    if algorithm in (GROUP_BY, GROUP_ADJACENT):
        key_expression = compile_xpath(source, context, location=instruction.location)
    else:
        pattern = compile_pattern(source, context, location=instruction.location)
    _, body_children = _split_sort_children(instruction)
    body = _mark_tail(_compile_sequence(body_children, context, module))
    return _ForEachGroupPlan(
        instruction=instruction,
        expression=compile_xpath(select_source, context, location=instruction.location),
        algorithm=algorithm,
        key_expression=key_expression,
        pattern=pattern,
        composite=composite,
        collation=(
            compile_avt(collation_source, context, location=instruction.location)
            if collation_source is not None
            else None
        ),
        sorts=_compile_sort_keys(instruction, context, module),
        children=body,
        default_collation=context.default_collation,
        base_uri=instruction.base_uri,
    )


def _compile_perform_sort(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _PerformSortPlan:
    context = _instruction_context(context, instruction)
    select_source = instruction.attribute("select")
    sorts, body_children = _split_sort_children(instruction)
    children = _compile_sequence(body_children, context, module)
    if select_source is not None and _has_plan_content(children, allow_fallback=True):
        raise XsltStaticError(
            "xsl:perform-sort with a select attribute may have only xsl:sort and "
            "xsl:fallback content",
            code="XTSE1040",
            uri=context.uri,
            location=instruction.location,
        )
    if not sorts:
        raise XsltStaticError(
            "xsl:perform-sort requires at least one xsl:sort",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    return _PerformSortPlan(
        instruction=instruction,
        expression=(
            compile_xpath(select_source, context, location=instruction.location)
            if select_source is not None
            else None
        ),
        sorts=_compile_sort_keys(instruction, context, module),
        children=() if select_source is not None else children,
    )


def _compile_next_iteration(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _NextIterationPlan:
    context = _instruction_context(context, instruction)
    parameters: list[_WithParamPlan] = []
    for child in instruction.children:
        if isinstance(child, TextNode):
            if child.value.strip():
                raise XsltStaticError(
                    "xsl:next-iteration permits only xsl:with-param children",
                    code="XTSE0010",
                    uri=context.uri,
                    location=child.location,
                )
            continue
        if not (child.is_xslt and child.local_name == "with-param"):
            raise XsltStaticError(
                "xsl:next-iteration permits only xsl:with-param children",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        parameters.append(_compile_parameter(child, context, module))
    for parameter in parameters:
        if parameter.tunnel:
            raise XsltStaticError(
                "xsl:with-param in xsl:next-iteration must not be a tunnel parameter",
                code="XTSE0670",
                uri=context.uri,
                location=instruction.location,
            )
    return _NextIterationPlan(instruction=instruction, parameters=tuple(parameters))


def _compile_break(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _BreakPlan:
    context = _instruction_context(context, instruction)
    select_source = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select_source is not None and _has_plan_content(children, allow_fallback=True):
        raise XsltStaticError(
            "xsl:break with a select attribute must have empty content except for xsl:fallback",
            code="XTSE3125",
            uri=context.uri,
            location=instruction.location,
        )
    return _BreakPlan(
        instruction=instruction,
        expression=(
            compile_xpath(select_source, context, location=instruction.location)
            if select_source is not None
            else None
        ),
        children=() if select_source is not None else children,
    )


def _validate_iteration_controls(
    plans: tuple[_Plan, ...],
    parameters: tuple[str, ...] | None,
) -> None:
    """Check ``xsl:next-iteration``/``xsl:break`` placement and names."""
    meaningful = [index for index, plan in enumerate(plans) if not isinstance(plan, _FallbackPlan)]
    last = meaningful[-1] if meaningful else -1
    for index, plan in enumerate(plans):
        _validate_control_plan(plan, parameters, tail=index == last)


def _validate_control_plan(
    plan: _Plan,
    parameters: tuple[str, ...] | None,
    *,
    tail: bool,
) -> None:
    if isinstance(plan, (_NextIterationPlan, _BreakPlan)):
        if parameters is None or not tail:
            raise XsltStaticError(
                f"xsl:{plan.instruction.local_name} is permitted only in a tail "
                "position within an xsl:iterate body",
                code="XTSE3120",
                uri=plan.instruction.module,
                location=plan.instruction.location,
            )
        if isinstance(plan, _NextIterationPlan):
            seen: set[str] = set()
            for parameter in plan.parameters:
                if parameter.name in seen:
                    raise XsltStaticError(
                        f"the iteration parameter {parameter.name!r} is supplied more than once",
                        code="XTSE0670",
                        uri=plan.instruction.module,
                        location=parameter.instruction.location
                        if hasattr(parameter, "instruction")
                        else plan.instruction.location,
                    )
                seen.add(parameter.name)
                if parameter.name not in parameters:
                    raise XsltStaticError(
                        f"xsl:next-iteration supplies the unknown parameter {parameter.name!r}",
                        code="XTSE3130",
                        uri=plan.instruction.module,
                        location=plan.instruction.location,
                    )
        for children in _plan_children(plan):
            _validate_iteration_controls(children, None)
        return
    if isinstance(plan, _IteratePlan):
        for children in _plan_children(plan):
            _validate_iteration_controls(children, None)
        _validate_iteration_controls(plan.children, plan.parameter_names)
        return
    if isinstance(plan, _SequencePlan):
        if plan.expression is None:
            _validate_iteration_controls(plan.children, parameters if tail else None)
        return
    if isinstance(plan, _ChoosePlan):
        for _, branch in plan.branches:
            _validate_iteration_controls(branch, parameters if tail else None)
        if plan.otherwise is not None:
            _validate_iteration_controls(plan.otherwise, parameters if tail else None)
        return
    if isinstance(plan, _ConditionalSequencePlan):
        _validate_iteration_controls(plan.plans, parameters if tail else None)
        return
    if isinstance(plan, _ConcatPlan):
        for part in plan.parts:
            _validate_iteration_controls(part, None)
        return
    for sort in _plan_sort_keys(plan):
        _validate_iteration_controls(sort.children, None)
    for children in _plan_children(plan):
        _validate_iteration_controls(children, None)


def _plan_sort_keys(plan: _Plan) -> tuple[SortKey, ...]:
    if isinstance(plan, (_ForEachPlan, _ForEachGroupPlan, _PerformSortPlan)):
        return plan.sorts
    if isinstance(plan, _ApplyTemplatesPlan):
        return plan.sorts
    return ()


def _plan_children(plan: _Plan) -> tuple[tuple[_Plan, ...], ...]:
    """Return sequence-constructor plan groups nested in one plan."""
    if isinstance(plan, _ValueOfPlan):
        return (plan.children,)
    if isinstance(plan, _LiteralPlan):
        return (plan.children,)
    if isinstance(plan, _VariablePlan):
        return (plan.children,)
    if isinstance(plan, _ParamPlan):
        return (plan.children,)
    if isinstance(plan, _AttributePlan):
        return (plan.children,)
    if isinstance(plan, _MapPlan):
        return (plan.children,)
    if isinstance(plan, _MapEntryPlan):
        return (plan.children,)
    if isinstance(plan, _AssertPlan):
        return (plan.children,)
    if isinstance(plan, _WherePopulatedPlan):
        return (plan.children,)
    if isinstance(plan, (_OnEmptyPlan, _OnNonEmptyPlan)):
        return (plan.children,)
    if isinstance(plan, _FallbackPlan):
        return (plan.children,)
    if isinstance(plan, _ResultDocumentPlan):
        return (plan.children,)
    if isinstance(plan, _IteratePlan):
        groups = [parameter.children for parameter in plan.parameters]
        if plan.on_completion is not None:
            groups.append(plan.on_completion.children)
        return tuple(groups)
    if isinstance(plan, _NextIterationPlan):
        return tuple(parameter.children for parameter in plan.parameters)
    if isinstance(plan, _BreakPlan):
        return (plan.children,)
    if isinstance(plan, _ForEachPlan):
        return (plan.children,)
    if isinstance(plan, _ForEachGroupPlan):
        return (plan.children,)
    if isinstance(plan, _PerformSortPlan):
        return (plan.children,)
    if isinstance(plan, _CallTemplatePlan):
        return tuple(parameter.children for parameter in plan.parameters)
    if isinstance(plan, _ApplyTemplatesPlan):
        return tuple(parameter.children for parameter in plan.parameters)
    if isinstance(plan, _OverridingPlan):
        return tuple(parameter.children for parameter in plan.parameters)
    return ()


def _compile_instruction(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    exclusions = _exclusions(module)
    context = _instruction_context(context, instruction)
    context = _original_context(context, instruction)
    location = instruction.location
    if instruction.literal:
        attributes: list[tuple[str, str | CompiledValueTemplate]] = []
        for attribute in instruction.attributes:
            if attribute.namespace_uri == XSLT_NS:
                continue
            if attribute.namespace_uri is None and attribute.name in _LITERAL_XSLT_ATTRIBUTES:
                continue
            attributes.append(
                (
                    attribute.name,
                    compile_avt(attribute.value, context, location=location),
                )
            )
        namespaces = _literal_namespaces(
            instruction,
            attributes,
            exclusions,
        )
        return _LiteralPlan(
            instruction=instruction,
            attributes=tuple(attributes),
            children=_compile_sequence(instruction.children, context, module),
            namespaces=namespaces,
            attribute_sets=tuple(
                reference
                for reference in instruction.references
                if reference.kind == "attribute-set"
            ),
        )
    local = instruction.local_name
    if local == "sequence":
        select = instruction.attribute("select")
        children = _compile_sequence(instruction.children, context, module)
        if select is not None and _has_plan_content(children, allow_fallback=True):
            raise XsltStaticError(
                "xsl:sequence with a select attribute must have empty content except "
                "for xsl:fallback",
                code="XTSE3185",
                uri=context.uri,
                location=location,
            )
        return _SequencePlan(
            instruction=instruction,
            expression=(
                compile_xpath(select, context, location=location) if select is not None else None
            ),
            children=children,
        )
    if local == "value-of":
        select = instruction.attribute("select")
        children = _compile_sequence(instruction.children, context, module)
        if select is not None and _has_plan_content(children):
            raise XsltStaticError(
                "xsl:value-of with a select attribute must have empty content",
                code="XTSE0870",
                uri=context.uri,
                location=location,
            )
        separator = instruction.attribute("separator")
        separator_default = " " if select is not None else ""
        return _ValueOfPlan(
            instruction=instruction,
            expression=(
                compile_xpath(select, context, location=location) if select is not None else None
            ),
            children=children,
            separator=(
                compile_avt(separator, context, location=location)
                if separator is not None
                else separator_default
            ),
            explicit_separator=separator is not None,
        )
    if local == "text":
        text = "".join(child.value for child in instruction.children if isinstance(child, TextNode))
        expand = instruction.attribute("expand-text")
        enabled = (
            context.expand_text
            if expand is None
            else _yes_no(expand, instruction, "expand-text", context)
        )
        if enabled:
            return _TvtPlan(compile_tvt(text, context.with_expand_text(True), location=location))
        return _TextPlan(text)
    if local == "for-each":
        select = instruction.attribute("select")
        if select is None:
            raise XsltStaticError(
                "xsl:for-each requires a select attribute",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        _, body_children = _split_sort_children(instruction)
        body = _mark_tail(_compile_sequence(body_children, context, module))
        return _ForEachPlan(
            instruction=instruction,
            expression=compile_xpath(select, context, location=location),
            sorts=_compile_sort_keys(instruction, context, module),
            children=body,
        )
    if local == "iterate":
        return _compile_iterate(instruction, context, module)
    if local == "next-iteration":
        return _compile_next_iteration(instruction, context, module)
    if local == "break":
        return _compile_break(instruction, context, module)
    if local == "for-each-group":
        return _compile_for_each_group(instruction, context, module)
    if local == "perform-sort":
        return _compile_perform_sort(instruction, context, module)
    if local in ("sort", "on-completion"):
        raise XsltStaticError(
            f"xsl:{local} is only permitted in a containing instruction",
            code="XTSE0010",
            uri=context.uri,
            location=location,
        )
    if local == "param":
        name = instruction.attribute("name")
        if not name:
            raise XsltStaticError(
                "xsl:param requires a name",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        required = _attribute_yes_no(instruction, "required", context)
        tunnel = _attribute_yes_no(instruction, "tunnel", context)
        select = instruction.attribute("select")
        children = _compile_sequence(instruction.children, context, module)
        if select is not None and _has_plan_content(children):
            raise XsltStaticError(
                "a parameter with a select attribute must have empty content",
                code="XTSE0620",
                uri=context.uri,
                location=location,
            )
        if required and (select is not None or _has_plan_content(children)):
            raise XsltStaticError(
                "a required parameter must not have a default value",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        as_source = instruction.attribute("as")
        return _ParamPlan(
            instruction=instruction,
            name=_expanded_name(name, instruction.namespaces),
            required=required,
            tunnel=tunnel,
            expression=(
                compile_xpath(select, context, location=location) if select is not None else None
            ),
            children=children,
            has_as=as_source is not None,
            sequence_type=(
                compile_sequence_type(as_source, context, location=location)
                if as_source is not None
                else None
            ),
        )
    if local == "variable":
        name = instruction.attribute("name")
        if not name:
            raise XsltStaticError(
                "xsl:variable requires a name",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        select = instruction.attribute("select")
        children = _compile_sequence(instruction.children, context, module)
        if select is not None and _has_plan_content(children):
            raise XsltStaticError(
                "a variable with a select attribute must have empty content",
                code="XTSE0620",
                uri=context.uri,
                location=location,
            )
        as_source = instruction.attribute("as")
        return _VariablePlan(
            instruction=instruction,
            name=_expanded_name(name, instruction.namespaces),
            expression=(
                compile_xpath(select, context, location=location) if select is not None else None
            ),
            children=children,
            has_as=as_source is not None,
            sequence_type=(
                compile_sequence_type(as_source, context, location=location)
                if as_source is not None
                else None
            ),
        )
    if local == "attribute":
        name = instruction.attribute("name")
        if not name:
            raise XsltStaticError(
                "xsl:attribute requires a name",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        select = instruction.attribute("select")
        children = _compile_sequence(instruction.children, context, module)
        if select is not None and _has_plan_content(children):
            raise XsltStaticError(
                "an attribute with a select attribute must have empty content",
                code="XTSE0620",
                uri=context.uri,
                location=location,
            )
        return _AttributePlan(
            instruction=instruction,
            name=compile_avt(name, context, location=location),
            namespaces=instruction.namespaces,
            expression=(
                compile_xpath(select, context, location=location) if select is not None else None
            ),
            children=children,
        )
    if local in ("next-match", "apply-imports"):
        return _OverridingPlan(
            instruction=instruction,
            kind=cast(Literal["next-match", "apply-imports"], local),
            parameters=_compile_with_params(instruction, context, module),
        )
    if local == "result-document":
        href = instruction.attribute("href")
        method = instruction.attribute("method")
        build_tree = instruction.attribute("build-tree")
        format_name = instruction.attribute("format")
        extra_attributes: list[tuple[str, str | CompiledValueTemplate]] = []
        for attribute in instruction.attributes:
            if attribute.name in {
                "href",
                "method",
                "build-tree",
                "format",
                "type",
                "validation",
            }:
                continue
            extra_attributes.append(
                (
                    attribute.name,
                    compile_avt(attribute.value, context, location=location),
                )
            )
        return _ResultDocumentPlan(
            instruction=instruction,
            href=(compile_avt(href, context, location=location) if href is not None else ""),
            method=(
                compile_avt(method, context, location=location) if method is not None else None
            ),
            build_tree=(
                compile_avt(build_tree, context, location=location)
                if build_tree is not None
                else None
            ),
            format=(
                compile_avt(format_name, context, location=location)
                if format_name is not None
                else None
            ),
            attributes=tuple(extra_attributes),
            children=_compile_sequence(instruction.children, context, module),
        )
    if local == "choose":
        branches: list[tuple[CompiledExpression, tuple[_Plan, ...]]] = []
        otherwise: tuple[_Plan, ...] | None = None
        for child in instruction.children:
            if isinstance(child, TextNode):
                raise XsltStaticError(
                    "xsl:choose permits only xsl:when and xsl:otherwise children",
                    code="XTSE0010",
                    uri=context.uri,
                    location=child.location,
                )
            if child.is_xslt and child.local_name == "when":
                test = child.attribute("test")
                if test is None:
                    raise XsltStaticError(
                        "xsl:when requires a test attribute",
                        code="XTSE0010",
                        uri=context.uri,
                        location=child.location,
                    )
                branches.append(
                    (
                        compile_xpath(test, context, location=child.location),
                        _compile_sequence(child.children, context, module),
                    )
                )
                continue
            if child.is_xslt and child.local_name == "otherwise":
                if otherwise is not None:
                    raise XsltStaticError(
                        "xsl:choose permits at most one xsl:otherwise",
                        code="XTSE0010",
                        uri=context.uri,
                        location=child.location,
                    )
                otherwise = _compile_sequence(child.children, context, module)
                continue
            raise XsltStaticError(
                "xsl:choose permits only xsl:when and xsl:otherwise children",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        return _ChoosePlan(
            instruction=instruction,
            branches=tuple(branches),
            otherwise=otherwise,
        )
    if local == "if":
        test = instruction.attribute("test")
        if test is None:
            raise XsltStaticError(
                "xsl:if requires a test attribute",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        return _ChoosePlan(
            instruction=instruction,
            branches=(
                (
                    compile_xpath(test, context, location=location),
                    _compile_sequence(instruction.children, context, module),
                ),
            ),
            otherwise=None,
        )
    if local == "map":
        if instruction.attribute("select") is not None:
            raise XsltStaticError(
                "xsl:map does not have a select attribute",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        return _MapPlan(
            instruction=instruction,
            children=_compile_sequence(instruction.children, context, module),
        )
    if local == "map-entry":
        key = instruction.attribute("key")
        if key is None:
            raise XsltStaticError(
                "xsl:map-entry requires a key attribute",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        select = instruction.attribute("select")
        children = _compile_sequence(instruction.children, context, module)
        if select is not None and _has_plan_content(children, allow_fallback=True):
            raise XsltStaticError(
                "xsl:map-entry with a select attribute must have empty content except "
                "for xsl:fallback",
                code="XTSE3280",
                uri=context.uri,
                location=location,
            )
        return _MapEntryPlan(
            instruction=instruction,
            key=compile_xpath(key, context, location=location),
            expression=(
                compile_xpath(select, context, location=location) if select is not None else None
            ),
            children=children,
        )
    if local == "evaluate":
        return _compile_evaluate(instruction, context, module)
    if local == "assert":
        test = instruction.attribute("test")
        if test is None:
            raise XsltStaticError(
                "xsl:assert requires a test attribute",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        select = instruction.attribute("select")
        error_code = instruction.attribute("error-code")
        return _AssertPlan(
            instruction=instruction,
            test=compile_xpath(test, context, location=location),
            select=(
                compile_xpath(select, context, location=location) if select is not None else None
            ),
            children=_compile_sequence(instruction.children, context, module),
            error_code=(
                compile_avt(error_code, context, location=location)
                if error_code is not None
                else None
            ),
            enabled=_assertion_checking_enabled(context),
        )
    if local == "where-populated":
        if instruction.attribute("select") is not None:
            raise XsltStaticError(
                "xsl:where-populated does not have a select attribute",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        return _WherePopulatedPlan(
            instruction=instruction,
            children=_compile_sequence(instruction.children, context, module),
        )
    if local in ("on-empty", "on-non-empty"):
        select = instruction.attribute("select")
        children = _compile_sequence(instruction.children, context, module)
        if select is not None and _has_plan_content(children, allow_fallback=True):
            raise XsltStaticError(
                f"xsl:{local} with a select attribute must have empty content except "
                "for xsl:fallback",
                code="XTSE3185",
                uri=context.uri,
                location=location,
            )
        plan_type = _OnEmptyPlan if local == "on-empty" else _OnNonEmptyPlan
        return plan_type(
            instruction=instruction,
            expression=(
                compile_xpath(select, context, location=location) if select is not None else None
            ),
            children=children,
        )
    if local == "fallback":
        return _FallbackPlan(
            instruction=instruction,
            children=_compile_sequence(instruction.children, context, module),
        )
    if local == "call-template":
        reference = instruction.reference
        if reference is None:
            raise XsltStaticError(
                "xsl:call-template requires a visible named template",
                code="XTSE0650",
                uri=context.uri,
                location=location,
            )
        return _CallTemplatePlan(
            instruction=instruction,
            reference=reference,
            parameters=_compile_with_params(instruction, context, module),
        )
    if local == "apply-templates":
        select = instruction.attribute("select")
        expression = (
            compile_xpath(select, context, location=location)
            if select is not None
            else compile_xpath("child::node()", context, location=location)
        )
        _, remaining = _split_sort_children(instruction)
        return _ApplyTemplatesPlan(
            instruction=instruction,
            expression=expression,
            mode=_instruction_mode(instruction),
            parameters=_compile_with_params(
                instruction,
                context,
                module,
                children=remaining,
            ),
            sorts=_compile_sort_keys(instruction, context, module),
        )
    if local == "with-param":
        raise XsltStaticError(
            "xsl:with-param is only permitted inside a call or apply instruction",
            code="XTSE0010",
            uri=context.uri,
            location=location,
        )
    if instruction.is_xslt and local not in _XSLT30_ELEMENTS:
        return _compile_forward_instruction(instruction, context, module)
    select = instruction.attribute("select")
    if select is not None:
        compile_xpath(select, context, location=location)
    test = instruction.attribute("test")
    if test is not None:
        compile_xpath(test, context, location=location)
    _compile_sequence(instruction.children, context, module)
    return _UnsupportedPlan(instruction)


def _assertion_checking_enabled(context: StaticContext) -> bool:
    value = context.static_option("assertions", False)
    if not isinstance(value, bool):
        raise TypeError("the assertions static option must be a boolean")
    return value


def _dynamic_evaluation_enabled(context: StaticContext) -> bool:
    value = context.static_option("dynamic-evaluation", True)
    if not isinstance(value, bool):
        raise TypeError("the dynamic-evaluation static option must be a boolean")
    return value


def _dynamic_xpath_version(instruction: Instruction, context: StaticContext) -> str:
    raw = instruction.attribute("xpath-version")
    if raw is None:
        return context.xpath_version
    value = raw.strip()
    if value not in ("2.0", "3.0", "3.1"):
        raise XsltStaticError(
            f"xsl:evaluate has an unsupported xpath-version {raw!r}",
            code="XTSE0020",
            uri=context.uri,
            location=instruction.location,
        )
    if Decimal(value) > Decimal(context.xpath_version):
        raise XsltStaticError(
            f"xsl:evaluate requests XPath {value} but the processor supports "
            f"{context.xpath_version}",
            code="XTSE0020",
            uri=context.uri,
            location=instruction.location,
        )
    return value


def _static_function_arity_range(arities: Sequence[FunctionArity]) -> FunctionArity:
    minimum = min(arity if isinstance(arity, int) else arity[0] for arity in arities)
    maximum = max(
        arity if isinstance(arity, int) else (arity[1] if arity[1] is not None else arity[0])
        for arity in arities
    )
    return minimum if minimum == maximum else (minimum, maximum)


def _dynamic_function_declarations(context: StaticContext) -> Mapping[str, FunctionArity]:
    grouped: dict[str, list[FunctionArity]] = {}
    for (name, arity), function in context.functions.items():
        if function.visibility in ("hidden", "private"):
            continue
        if not name.startswith("{") and ":" not in name:
            continue
        grouped.setdefault(name, []).append(arity)
    declarations: dict[str, FunctionArity] = {}
    for name, arities in grouped.items():
        declarations[name] = (
            arities[0] if len(arities) == 1 else _static_function_arity_range(arities)
        )
    return MappingProxyType(declarations)


def _compile_evaluate_content(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> tuple[tuple[_WithParamPlan, ...], tuple[tuple[_Plan, ...], ...]]:
    parameters: list[_WithParamPlan] = []
    fallbacks: list[tuple[_Plan, ...]] = []
    for child in instruction.children:
        if isinstance(child, TextNode):
            raise XsltStaticError(
                "xsl:evaluate permits only xsl:with-param and xsl:fallback content",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        if child.is_xslt and child.local_name == "with-param":
            parameters.append(_compile_parameter(child, context, module))
            continue
        if child.is_xslt and child.local_name == "fallback":
            fallbacks.append(_compile_sequence(child.children, context, module))
            continue
        raise XsltStaticError(
            "xsl:evaluate permits only xsl:with-param and xsl:fallback content",
            code="XTSE0010",
            uri=context.uri,
            location=child.location,
        )
    return tuple(parameters), tuple(fallbacks)


def _compile_evaluate(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    xpath_source = instruction.attribute("xpath")
    if xpath_source is None:
        raise XsltStaticError(
            "xsl:evaluate requires an xpath attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    expression = compile_xpath(xpath_source, context, location=instruction.location)
    with_params, fallback = _compile_evaluate_content(instruction, context, module)
    enabled = _dynamic_evaluation_enabled(context)
    if not enabled and fallback:
        return _ConcatPlan(instruction, fallback)
    select_with_params = instruction.attribute("with-params")
    context_item = instruction.attribute("context-item")
    namespace_context = instruction.attribute("namespace-context")
    base_uri = instruction.attribute("base-uri")
    schema_aware = instruction.attribute("schema-aware")
    as_source = instruction.attribute("as")
    namespaces = {prefix: uri for prefix, uri in instruction.namespaces.items() if prefix}
    return _EvaluatePlan(
        instruction=instruction,
        expression=expression,
        sequence_type=(
            compile_sequence_type(as_source, context, location=instruction.location)
            if as_source is not None
            else None
        ),
        base_uri=(
            compile_avt(base_uri, context, location=instruction.location)
            if base_uri is not None
            else None
        ),
        schema_aware=(
            compile_avt(schema_aware, context, location=instruction.location)
            if schema_aware is not None
            else None
        ),
        context_item=(
            compile_xpath(context_item, context, location=instruction.location)
            if context_item is not None
            else None
        ),
        namespace_context=(
            compile_xpath(namespace_context, context, location=instruction.location)
            if namespace_context is not None
            else None
        ),
        with_params=with_params,
        with_params_expression=(
            compile_xpath(select_with_params, context, location=instruction.location)
            if select_with_params is not None
            else None
        ),
        xpath_version=_dynamic_xpath_version(instruction, context),
        enabled=enabled,
        namespaces=MappingProxyType(dict(namespaces)),
        default_namespace=context.xpath_default_namespace,
        functions=_dynamic_function_declarations(context),
    )


def _forwards_compatible(version: str) -> bool:
    try:
        return Decimal(version) > _PROCESSOR_VERSION
    except (InvalidOperation, ValueError):
        return False


def _compile_forward_instruction(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    if not _forwards_compatible(context.version):
        raise XsltStaticError(
            f"xsl:{instruction.local_name} is not defined in XSLT 3.0",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    fallbacks = [
        child for child in instruction.elements if child.is_xslt and child.local_name == "fallback"
    ]
    if not fallbacks:
        raise XsltStaticError(
            f"xsl:{instruction.local_name} is not defined in XSLT 3.0 and has no xsl:fallback",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    return _ConcatPlan(
        instruction,
        tuple(_compile_sequence(fallback.children, context, module) for fallback in fallbacks),
    )


def _literal_namespaces(
    instruction: Instruction,
    attributes: Sequence[tuple[str, str | CompiledValueTemplate]],
    exclusions: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    excluded = set(exclusions)
    exclude_all = "#all" in excluded
    used: set[str | None] = {_namespace(instruction.name)}
    used.update(_namespace(name) for name, _ in attributes)
    result: list[tuple[str, str]] = []
    for prefix, uri in instruction.namespaces.items():
        if prefix in ("xml", "xsl"):
            continue
        if uri in used:
            result.append((prefix, uri))
            continue
        if prefix == "" and "#default" in excluded:
            continue
        if exclude_all or prefix in excluded:
            continue
        result.append((prefix, uri))
    return tuple(result)


def _expanded_name(name: str, namespaces: Mapping[str, str]) -> str:
    if name.startswith("{"):
        return name
    prefix, separator, local = name.partition(":")
    if not separator:
        return name
    uri = namespaces.get(prefix)
    if uri is None:
        return name
    return f"{{{uri}}}{local}"


def _namespace(name: str) -> str | None:
    if not name.startswith("{"):
        return None
    return name[1:].partition("}")[0]


def _assertion_error_code(value: str, namespaces: Mapping[str, str]) -> str:
    """Resolve one effective ``xsl:assert/@error-code`` EQName.

    An invalid EQName defaults to the assertion code XTMM9001 as XSLT 3.0
    section 23.2 requires.  Standard error-namespace names are stored in their
    familiar local form, while a custom namespace keeps its expanded form.
    """
    text = value.strip()
    default = "XTMM9001"
    if not text:
        return default
    if text.startswith("Q{"):
        close = text.find("}")
        if close < 0:
            return default
        namespace = text[2:close]
        local = text[close + 1 :]
        if not is_ncname(local):
            return default
        expanded = f"{{{namespace}}}{local}" if namespace else local
    else:
        if text.startswith("{"):
            return default
        prefix, separator, local = text.partition(":")
        if separator:
            if not is_ncname(prefix) or not is_ncname(local):
                return default
            bound = namespaces.get(prefix)
            if not bound:
                return default
            expanded = f"{{{bound}}}{local}"
        else:
            if not is_ncname(text):
                return default
            expanded = text
    standard = f"{{{ERROR_NAMESPACE}}}"
    if expanded.startswith(standard):
        return expanded[len(standard) :]
    return expanded


def _boolean_avt(value: str, attribute: str) -> bool:
    normalized = value.strip()
    if normalized in ("yes", "true", "1"):
        return True
    if normalized in ("no", "false", "0"):
        return False
    raise XsltDynamicError(
        f"the effective value of {attribute} is not a boolean: {value!r}",
        code="XTDE0030",
    )


def _dynamic_variable_names(
    values: Mapping[str, XdmSequence],
    namespaces: Mapping[str, str],
) -> dict[str, XdmSequence]:
    """Bind dynamic variables by expanded and in-scope lexical names."""
    result: dict[str, XdmSequence] = {}
    for name, value in values.items():
        result[name] = value
        if not name.startswith("{"):
            continue
        namespace, _, local = name[1:].partition("}")
        for prefix, uri in namespaces.items():
            if prefix and uri == namespace:
                result[f"{prefix}:{local}"] = value
                break
    return result


def _has_plan_content(
    plans: Iterable[_Plan],
    *,
    allow_fallback: bool = False,
) -> bool:
    for plan in plans:
        if allow_fallback and isinstance(plan, _FallbackPlan):
            continue
        if isinstance(plan, _TextPlan):
            if plan.value.strip():
                return True
            continue
        return True
    return False


def _yes_no(
    value: str,
    instruction: Instruction,
    attribute: str,
    context: StaticContext,
) -> bool:
    normalized = value.strip()
    if normalized in ("yes", "true", "1"):
        return True
    if normalized in ("no", "false", "0"):
        return False
    raise XsltStaticError(
        f"{attribute} has an invalid boolean value {value!r}",
        code="XTSE0020",
        uri=context.uri,
        location=instruction.location,
    )


def _attribute_yes_no(
    instruction: Instruction,
    attribute: str,
    context: StaticContext,
) -> bool:
    value = instruction.attribute(attribute)
    if value is None:
        return False
    return _yes_no(value, instruction, attribute, context)


# ---------------------------------------------------------------------------
# Invocation validation


@dataclass(frozen=True, slots=True)
class _InvocationOptions:
    global_context_item: XdmItem | None
    initial_match_selection: XdmSequence | _AbsentType
    template: Component | None
    mode: Mode | None
    function: Component | None
    function_arguments: tuple[XdmSequence, ...]
    template_parameters: Mapping[str, XdmSequence]
    tunnel_parameters: Mapping[str, XdmSequence]
    parameters: Mapping[tuple[str | None, str], XdmSequence]
    output_uri: str | None
    delivery: str
    entry: Literal["template", "function", "mode"]


def _prepare_invocation(
    stylesheet: CompiledStylesheet,
    *,
    source: Any,
    parameters: Mapping[str, Any] | None,
    global_context_item: Any,
    initial_match_selection: Any,
    template_parameters: Mapping[str, Any] | None,
    tunnel_parameters: Mapping[str, Any] | None,
    initial_template: str | None,
    initial_mode: str | None,
    initial_function: str | None,
    initial_function_arguments: Sequence[Any],
    output_uri: str | None,
    delivery: str,
) -> _InvocationOptions:
    package = stylesheet.package
    if delivery not in _DELIVERIES:
        raise XsltInvocationError(
            f"delivery must be one of {sorted(_DELIVERIES)}, got {delivery!r}"
        )
    if output_uri is not None:
        _require_absolute(output_uri, "output_uri")

    source_supplied = source is not _ABSENT
    global_supplied = global_context_item is not _ABSENT
    selection_supplied = initial_match_selection is not _ABSENT

    if source_supplied and global_context_item is not _ABSENT:
        raise XsltInvocationError(
            "source and global_context_item are alternative defaults; supply only one"
        )
    if source_supplied and initial_match_selection is not _ABSENT:
        raise XsltInvocationError(
            "source and initial_match_selection are alternative defaults; supply only one"
        )
    if initial_template is not None and initial_function is not None:
        raise XsltInvocationError("initial_template and initial_function are mutually exclusive")
    if initial_template is not None and selection_supplied:
        raise XsltInvocationError("initial_match_selection is not valid with initial_template")
    if initial_function is not None and selection_supplied:
        raise XsltInvocationError("initial_match_selection is not valid with initial_function")
    if initial_template is not None and initial_mode is not None:
        raise XsltInvocationError("initial_template and initial_mode are mutually exclusive")
    if initial_function is not None and initial_mode is not None:
        raise XsltInvocationError("initial_function and initial_mode are mutually exclusive")

    arguments = _function_arguments(initial_function, initial_function_arguments)
    if initial_function is None and arguments:
        raise ValueError("initial_function_arguments requires an initial_function")
    if initial_function is not None and (template_parameters or tunnel_parameters):
        raise XsltInvocationError(
            "template_parameters and tunnel_parameters are not valid with an initial_function"
        )

    default_global: XdmItem | None = None
    default_selection: XdmSequence | _AbsentType = _ABSENT
    if source_supplied:
        default_selection, default_global = _source_inputs(source)

    explicit_global: XdmItem | None = None
    if global_supplied:
        explicit_global = _global_context(global_context_item)
    explicit_selection: XdmSequence | _AbsentType = _ABSENT
    if selection_supplied:
        explicit_selection = as_xdm(initial_match_selection)

    global_item = explicit_global if global_supplied else default_global
    selection = explicit_selection if explicit_selection is not _ABSENT else default_selection

    template_params = _parameter_sequences(package, template_parameters)
    tunnel = _parameter_sequences(package, tunnel_parameters)
    global_parameters = _global_parameter_sequences(package, parameters)

    template_component: Component | None = None
    function_component: Component | None = None
    mode: Mode | None = None
    if initial_function is not None:
        function_component = package.function(initial_function, len(arguments))
        if function_component is not None and function_component.visibility not in (
            "public",
            "final",
        ):
            function_component = None
        if function_component is None:
            raise XsltError(
                f"the initial function {initial_function!r} with arity {len(arguments)} "
                "is not a public or final stylesheet function",
                code="XTDE0041",
            )
        entry: Literal["template", "function", "mode"] = "function"
    elif initial_template is not None:
        template_component = package.named_template(initial_template)
        if template_component is not None and template_component.visibility not in (
            "public",
            "final",
        ):
            template_component = None
        if template_component is None:
            raise XsltError(
                f"the initial named template {initial_template!r} is not public or final",
                code="XTDE0040",
            )
        entry = "template"
    elif initial_mode is not None:
        entry = "mode"
        mode = _initial_mode(package, initial_mode)
        if selection is _ABSENT and explicit_global is not None:
            selection = (explicit_global,)
        if selection is _ABSENT:
            raise XsltInvocationError(
                "an initial mode requires source or initial_match_selection",
                code="XTDE0044",
            )
    elif source_supplied or selection_supplied:
        entry = "mode"
        mode = _default_mode(package)
    else:
        entry = "template"
        template_component = package.named_template("xsl:initial-template")
        if template_component is not None and template_component.visibility not in (
            "public",
            "final",
        ):
            template_component = None
        if template_component is None:
            raise XsltError(
                "the stylesheet does not define the initial template xsl:initial-template",
                code="XTDE0040",
            )
    return _InvocationOptions(
        global_context_item=global_item,
        initial_match_selection=selection,
        template=template_component,
        mode=mode,
        function=function_component,
        function_arguments=arguments,
        template_parameters=template_params,
        tunnel_parameters=tunnel,
        parameters=global_parameters,
        output_uri=output_uri,
        delivery=delivery,
        entry=entry,
    )


def _source_inputs(source: Any) -> tuple[XdmSequence, XdmItem | None]:
    if isinstance(source, Document):
        document = source.to_xdm()
        return (document,), document
    if isinstance(source, XdmDocument):
        return (source,), source
    if isinstance(source, XdmItem):
        item_sequence: XdmSequence = (source,)
        global_item = source.document if isinstance(source, XdmNode) else None
        return item_sequence, global_item
    if isinstance(source, str) and source.lstrip().startswith("<"):
        document = XdmDocument.parse(source.encode("utf-8"))
        return (document,), document
    if isinstance(source, (str, os.PathLike, bytes, bytearray, memoryview)):
        document = XdmDocument.parse(source)
        return (document,), document
    if hasattr(source, "read"):
        document = XdmDocument.parse(source)
        return (document,), document
    sequence: XdmSequence = as_xdm(source)
    global_item = None
    if len(sequence) == 1 and isinstance(sequence[0], XdmNode):
        global_item = sequence[0].document
    return sequence, global_item


def _global_context(value: Any) -> XdmItem | None:
    sequence = as_xdm(value)
    if len(sequence) > 1:
        raise XsltInvocationError(
            "global_context_item must be empty or a single item",
            code=None,
        )
    return sequence[0] if sequence else None


def _function_arguments(
    initial_function: str | None,
    values: Sequence[Any],
) -> tuple[XdmSequence, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError("initial_function_arguments must be a sequence of values")
    del initial_function
    return tuple(as_xdm(value) for value in values)


def _parameter_sequences(
    package: CompiledPackage,
    mapping: Mapping[str, Any] | None,
) -> Mapping[str, XdmSequence]:
    if mapping is None:
        return MappingProxyType({})
    if not isinstance(mapping, Mapping):
        raise TypeError("parameters must be a mapping or None")
    result: dict[str, XdmSequence] = {}
    for name, value in mapping.items():
        if not isinstance(name, str):
            raise TypeError("parameter names must be strings")
        try:
            component = package.variable(name)
        except XsltStaticError:
            component = None
        expanded = (
            component.name
            if component is not None and component.name is not None
            else _expanded_name(name, package.namespaces)
        )
        result[expanded] = as_xdm(value)
    return MappingProxyType(result)


def _global_parameter_sequences(
    package: CompiledPackage,
    mapping: Mapping[str, Any] | None,
) -> Mapping[tuple[str | None, str], XdmSequence]:
    if mapping is None:
        return MappingProxyType({})
    if not isinstance(mapping, Mapping):
        raise TypeError("parameters must be a mapping or None")
    result: dict[tuple[str | None, str], XdmSequence] = {}
    for name, value in mapping.items():
        if not isinstance(name, str):
            raise TypeError("parameter names must be strings")
        try:
            component = package.variable(name)
        except XsltStaticError:
            component = None
        if component is not None and component.name is not None:
            result[(component.owner, component.name)] = as_xdm(value)
    return MappingProxyType(result)


def _initial_mode(package: CompiledPackage, name: str) -> Mode:
    wanted = None if name == "#unnamed" else name
    mode = package.mode(wanted)
    if mode is None:
        raise XsltError(
            f"the initial mode {name!r} is not a visible mode in the stylesheet",
            code="XTDE0045",
        )
    return mode


def _default_mode(package: CompiledPackage) -> Mode | None:
    if package.default_mode is None:
        return package.mode(None)
    return package.mode(package.default_mode)


_XSD_NS = "http://www.w3.org/2001/XMLSchema"


def _type_error(code: str, message: str) -> XsltError:
    return XsltError(message, code=code)


def _coerce_sequence(
    sequence: XdmSequence,
    sequence_type: CompiledSequenceType,
    *,
    code: str,
    label: str,
) -> XdmSequence:
    """Apply a narrow but normative function-conversion boundary.

    The implementation atomizes for atomic required types, casts
    untyped/atomic values to the required built-in atomic type, and enforces
    cardinality.  It does not pretend to be the full F&O function conversion
    algorithm; unsupported conversions raise the specification's type error
    rather than passing an untyped value through.
    """
    if sequence_type.item_type is None:
        if sequence:
            raise _type_error(
                code,
                f"{label} requires empty-sequence(), got {len(sequence)} item(s)",
            )
        return ()
    item_type = sequence_type.item_type
    items = (
        _atomize_items(sequence, code=code, label=label)
        if item_type.kind == "atomic"
        else tuple(sequence)
    )
    _check_cardinality(items, sequence_type, code=code, label=label)
    if item_type.kind == "atomic":
        converted: list[XdmItem] = []
        for item in items:
            assert isinstance(item, XdmAtomic)
            converted.append(_coerce_atomic(item, item_type, code=code, label=label))
        return tuple(converted)
    for item in items:
        if not _matches_item_type(item, item_type):
            raise _type_error(
                code,
                f"{label} requires {sequence_type}, got {type(item).__name__}",
            )
    return tuple(items)


def _atomize_items(
    sequence: XdmSequence,
    *,
    code: str,
    label: str,
) -> tuple[XdmAtomic, ...]:
    atomized: list[XdmAtomic] = []
    for item in sequence:
        if isinstance(item, XdmAtomic):
            atomized.append(item)
            continue
        if isinstance(item, XdmNode):
            atomized.extend(value for value in item.typed_value if isinstance(value, XdmAtomic))
            continue
        raise _type_error(
            "FOTY0013",
            f"{label} cannot atomize a function item",
        )
    return tuple(atomized)


def _coerce_atomic(
    item: XdmAtomic,
    item_type: ItemType,
    *,
    code: str,
    label: str,
) -> XdmAtomic:
    required = item_type.name
    if required is None or required == f"{{{_XSD_NS}}}anyAtomicType":
        return item
    if item.type_name == required:
        return item
    try:
        return item.cast(required)
    except (TypeError, ValueError) as exc:
        raise _type_error(
            code,
            f"{label} requires {item_type}, cannot convert {item.lexical!r}",
        ) from exc


def _check_cardinality(
    items: Sequence[XdmItem],
    sequence_type: CompiledSequenceType,
    *,
    code: str,
    label: str,
) -> None:
    count = len(items)
    occurrence = sequence_type.occurrence
    valid = (
        (occurrence == "" and count == 1)
        or (occurrence == "?" and count <= 1)
        or (occurrence == "+" and count >= 1)
        or occurrence == "*"
    )
    if not valid:
        raise _type_error(
            code,
            f"{label} requires {sequence_type}, got {count} item(s)",
        )


def _matches_item_type(item: XdmItem, item_type: ItemType) -> bool:
    kind = item_type.kind
    if kind == "item":
        return True
    if kind == "node":
        return isinstance(item, XdmNode)
    if kind == "text":
        return isinstance(item, XdmNode) and item.kind is NodeKind.TEXT
    if kind == "comment":
        return isinstance(item, XdmNode) and item.kind is NodeKind.COMMENT
    if kind == "namespace-node":
        return isinstance(item, XdmNode) and item.kind is NodeKind.NAMESPACE
    if kind == "processing-instruction":
        return (
            isinstance(item, XdmNode)
            and item.kind is NodeKind.PROCESSING_INSTRUCTION
            and (item_type.target is None or item.target == item_type.target)
        )
    if kind in ("element", "schema-element"):
        return (
            isinstance(item, XdmNode)
            and item.kind is NodeKind.ELEMENT
            and (item_type.wildcard or item_type.name is None or item.name == item_type.name)
        )
    if kind in ("attribute", "schema-attribute"):
        return (
            isinstance(item, XdmNode)
            and item.kind is NodeKind.ATTRIBUTE
            and (item_type.wildcard or item_type.name is None or item.name == item_type.name)
        )
    if kind == "document-node":
        return isinstance(item, XdmNode) and item.kind is NodeKind.DOCUMENT
    if kind == "function":
        return isinstance(item, (XdmFunction, XdmMap, XdmArray))
    if kind == "map":
        return isinstance(item, XdmMap)
    if kind == "array":
        return isinstance(item, XdmArray)
    return False  # pragma: no cover - every ItemType kind is handled above


def _item_string(item: XdmItem) -> str:
    if isinstance(item, XdmAtomic):
        return item.lexical
    if isinstance(item, XdmNode):
        return item.string_value
    raise XsltError(
        "atomization of a function item is not permitted here",
        code="FOTY0013",
    )


def _simple_content_string(sequence: XdmSequence, separator: str) -> str:
    values: list[str] = []
    pending_text = ""
    for item in sequence:
        if isinstance(item, XdmNode) and item.kind is NodeKind.TEXT:
            pending_text += item.string_value
            continue
        if pending_text:
            values.append(pending_text)
            pending_text = ""
        values.append(_item_string(item))
    if pending_text:
        values.append(pending_text)
    return separator.join(values)


_EBV_STRING_TYPES = frozenset(
    {
        "anyURI",
        "ENTITY",
        "ID",
        "IDREF",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "normalizedString",
        "string",
        "token",
        "untypedAtomic",
    }
)
_EBV_NUMERIC_TYPES = frozenset(
    {
        "byte",
        "decimal",
        "double",
        "float",
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
_BUILTIN_TYPE_NAMES: dict[type[Any], str] | None = None


def _builtin_class_names() -> Mapping[type[Any], str]:
    """Map every canonical built-in datatype class to its XSD local name."""
    global _BUILTIN_TYPE_NAMES
    if _BUILTIN_TYPE_NAMES is None:
        _BUILTIN_TYPE_NAMES = {value: name for name, value in builtin_type_classes().items()}
    return _BUILTIN_TYPE_NAMES


def _primitive_local_name(item: XdmAtomic) -> str:
    """Classify an atomic value by its nearest built-in datatype ancestor."""
    names = _builtin_class_names()
    datatype = item.datatype
    if datatype is not None:
        for base in datatype.__mro__:
            name = names.get(base)
            if name is not None:
                return name
    for base in type(item.value).__mro__:
        name = names.get(base)
        if name is not None:
            return name
    return item.type_name.rpartition("}")[2]


def _effective_boolean(sequence: XdmSequence) -> bool:
    """Evaluate the XPath effective boolean value of one XDM sequence.

    Nodes are true by their presence.  A single boolean, string-family, or
    numeric atomic follows the specification's value rules.  A multi-item
    atomic sequence, a function item, or an unsupported atomic type raises
    ``FORG0006`` instead of guessing from the first item.
    """
    if not sequence:
        return False
    first = sequence[0]
    if isinstance(first, XdmNode):
        return True
    if not isinstance(first, XdmAtomic):
        raise XsltError(
            "the effective boolean value of a function item is not defined",
            code="FORG0006",
        )
    if len(sequence) != 1:
        raise XsltError(
            "the effective boolean value of a multi-item atomic sequence is not defined",
            code="FORG0006",
        )
    local = _primitive_local_name(first)
    if local == "boolean":
        return bool(first.value)
    if local in _EBV_STRING_TYPES:
        return first.lexical != ""
    if local in _EBV_NUMERIC_TYPES:
        value = first.value
        # NaN is the only numeric value not equal to itself.
        if value != value:
            return False
        return value != 0
    raise XsltError(
        f"the effective boolean value of {local or 'this atomic type'} is not defined",
        code="FORG0006",
    )


def _require_absolute(uri: str, label: str) -> str:
    if not isinstance(uri, str) or not uri or not urlsplit(uri).scheme:
        raise XsltInvocationError(f"{label} must be a non-empty absolute URI, got {uri!r}")
    return uri


# ---------------------------------------------------------------------------
# Invocation execution


class _IterationControlSignal(Exception):
    """A valid iterate-body control reached through synchronous conditional content.

    ``xsl:on-non-empty`` and ``xsl:on-empty`` are executed by the conditional
    sequence evaluator, which normally returns a value synchronously.  A
    control instruction in that content must instead unwind to the body frame so
    the iteration frame can observe it.  The signal carries the conditional
    output produced before the control and the lexical focus at the control.
    """

    __slots__ = ("focus", "pending", "plan", "variables")

    def __init__(
        self,
        plan: _NextIterationPlan | _BreakPlan,
        variables: Mapping[str, XdmSequence],
        focus: Focus,
    ) -> None:
        super().__init__(plan.instruction.local_name)
        self.plan = plan
        self.variables = dict(variables)
        self.focus = focus
        self.pending: tuple[XdmItem, ...] = ()

    def prepend(self, values: Sequence[XdmItem]) -> None:
        """Add already-produced conditional output before this control."""
        self.pending = (*values, *self.pending)


class _Invocation(Transformation):
    """One transformation's complete dynamic state and instruction runtime."""

    def __init__(
        self,
        stylesheet: CompiledStylesheet,
        options: _InvocationOptions,
        *,
        limits: Any = None,
    ) -> None:
        super().__init__(stylesheet, options, limits=limits)
        self._mode_dispatchers: dict[tuple[str | None, str | None], RuleDispatcher] = {}
        self._pattern_environments: dict[int, ExpressionEnvironment] = {}
        self.dynamic_expressions = DynamicExpressionCache()
        self._active_iteration_frame: Frame | None = None

    # -- top level ---------------------------------------------------------

    def run(self) -> TransformationResult:
        sequence = self._execute_entry()
        tree = None
        if self.options.delivery == "document" or (
            self.options.delivery == "stylesheet"
            and self._build_tree(
                self.output_attributes.get("build-tree"),
                self.output_attributes.get("method", "xml"),
            )
        ):
            tree = _build_result_tree(sequence, output_uri=self.options.output_uri)
        entry = ResultEntry(
            sequence,
            output_uri=self.options.output_uri,
            tree=tree,
            serialization=self.output_attributes,
        )
        return TransformationResult(
            entry,
            secondary=self.secondary,
            messages=(),
        )

    def _execute_entry(self) -> XdmSequence:
        options = self.options
        if options.entry == "function":
            assert options.function is not None
            return self._execute_component(
                options.function,
                arguments=options.function_arguments,
                final_output=False,
            )
        if options.entry == "template":
            assert options.template is not None
            return self._execute_component(
                options.template,
                template_parameters=options.template_parameters,
                tunnel_parameters=options.tunnel_parameters,
                final_output=True,
            )
        assert options.initial_match_selection is not _ABSENT
        return self._apply_templates(
            cast(XdmSequence, options.initial_match_selection),
            options.mode.name if options.mode is not None else None,
            options.template_parameters,
            options.tunnel_parameters,
            package_name=self.package.name,
        )

    # -- components and parameters ----------------------------------------

    def _component_plan(self, component: Component) -> _ComponentPlan:
        return self.stylesheet._components[
            (component.owner, component.kind, component.name, component.arity)
        ]

    def _execute_component(
        self,
        component: Component,
        *,
        arguments: tuple[XdmSequence, ...] = (),
        template_parameters: Mapping[str, XdmSequence] | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        final_output: bool,
        focus: Focus | None = None,
        mode: str | None = None,
        rule_context: _RuleContext | None = None,
        group: object = _INHERIT,
        grouping_key: object = _INHERIT,
    ) -> XdmSequence:
        frame = self._component_frame(
            component,
            arguments=arguments,
            template_parameters=template_parameters,
            tunnel_parameters=tunnel_parameters,
            final_output=final_output,
            focus=focus,
            mode=mode,
            rule_context=rule_context,
            group=group,
            grouping_key=grouping_key,
        )
        return cast(XdmSequence, self.execute_frame(frame))

    def _component_identity(self, component: Component) -> str:
        name = component.name or "<unnamed>"
        if component.kind == "function":
            return f"function {name}#{component.arity}"
        return f"template {name}"

    def _component_frame(
        self,
        component: Component,
        *,
        arguments: tuple[XdmSequence, ...] = (),
        template_parameters: Mapping[str, XdmSequence] | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        final_output: bool,
        focus: Focus | None = None,
        mode: str | None = None,
        rule_context: _RuleContext | None = None,
        group: object = _INHERIT,
        grouping_key: object = _INHERIT,
    ) -> Frame:
        plan = self._component_plan(component)
        package_name = component.owner
        context_signature: ContextItemSignature | None = None
        if component.kind == "function":
            resolved_focus = Focus.absent()
        else:
            if focus is None:
                resolved_focus = (
                    Focus.singleton(self.options.global_context_item)
                    if self.options.global_context_item is not None
                    else Focus.absent()
                )
            else:
                resolved_focus = focus
            context_signature = component.signature.context_item if component.signature else None
            resolved_focus = self._apply_context_item(context_signature, resolved_focus)
        effective_rule_context = rule_context
        if context_signature is not None and context_signature.use == "absent":
            effective_rule_context = None
        effective_tunnel = tunnel_parameters or {}
        locals_, index = self._bind_component_parameters(
            component,
            plan.body,
            arguments=arguments,
            template_parameters=template_parameters,
            tunnel_parameters=effective_tunnel,
            focus=resolved_focus,
            package_name=package_name,
            mode=mode,
        )
        conversion = None
        declaration_as = component.declaration.attribute("as")
        signature = component.signature
        if (
            declaration_as is not None
            and signature is not None
            and signature.return_type is not None
        ):
            conversion = lambda value: self._coerce_component_result(  # noqa: E731
                component, cast(XdmSequence, value)
            )
        effective_group, effective_grouping_key = self._inherited_group(
            group,
            grouping_key,
            function=component.kind == "function",
        )
        frame = Frame(
            kind="component",
            component=(
                _TemplateContext(
                    component,
                    effective_rule_context,
                    plan.original_functions,
                )
                if plan.original_functions
                or (component.kind == "template" and effective_rule_context is not None)
                else component
            ),
            identity=self._component_identity(component),
            program=plan.body[index:],
            variables=locals_,
            tunnel_parameters=effective_tunnel,
            focus=resolved_focus,
            continuation=None,
            package=package_name,
            mode=mode,
            location=component.location,
            final_output=final_output,
            conversion=conversion,
            group=effective_group,
            grouping_key=effective_grouping_key,
        )
        return self.frame_boundary(frame) if conversion is not None else frame

    def _bind_component_parameters(
        self,
        component: Component,
        body: tuple[_Plan, ...],
        *,
        arguments: tuple[XdmSequence, ...],
        template_parameters: Mapping[str, XdmSequence] | None,
        tunnel_parameters: Mapping[str, XdmSequence] | None,
        focus: Focus,
        package_name: str | None,
        mode: str | None = None,
    ) -> tuple[dict[str, XdmSequence], int]:
        locals_: dict[str, XdmSequence] = {}
        index = 0
        positional = 0
        while index < len(body) and isinstance(body[index], _ParamPlan):
            parameter = cast(_ParamPlan, body[index])
            if component.kind == "function":
                value = arguments[positional]
                positional += 1
            else:
                supplied = (
                    (tunnel_parameters or {}).get(parameter.name)
                    if parameter.tunnel
                    else (template_parameters or {}).get(parameter.name)
                )
                value = (
                    supplied
                    if supplied is not None
                    else self._parameter_default(
                        parameter,
                        focus,
                        locals_,
                        package_name,
                        mode=mode,
                        tunnel_parameters=tunnel_parameters,
                    )
                )
            value = self._coerce_parameter(
                parameter,
                value,
                code=("XTTE0790" if component.kind == "function" else "XTTE0590"),
            )
            self._bind(locals_, parameter.name, value)
            index += 1
        return locals_, index

    # -- frame dispatch ----------------------------------------------------

    def step(self, frame: Frame) -> FrameResult:
        previous = (self.current_group, self.current_grouping_key)
        previous_iteration = self._active_iteration_frame
        self.current_group = None if frame.group is NO_GROUP else cast(XdmSequence, frame.group)
        self.current_grouping_key = (
            None if frame.grouping_key is NO_GROUP else cast(XdmSequence, frame.grouping_key)
        )
        state = frame.iteration
        self._active_iteration_frame = (
            frame if isinstance(state, IterationState) and state.kind == "iterate" else None
        )
        try:
            if frame.kind == "xpath":
                return self._step_xpath(frame)
            if frame.kind == "apply":
                return self._step_apply(frame)
            if frame.kind == "iteration":
                return self._step_iterate(frame)
            return self._step_instruction(frame)
        finally:
            self.current_group, self.current_grouping_key = previous
            self._active_iteration_frame = previous_iteration

    def _consume_input(self, frame: Frame) -> Frame:
        if frame.input is NO_INPUT:
            return frame
        value = frame.input
        materialized = value if isinstance(value, tuple) else (value,)
        return replace(
            frame,
            input=NO_INPUT,
            values=frame.values + tuple(materialized),
        )

    def _frame_variables(self, frame: Frame) -> Mapping[str, XdmSequence]:
        return cast(Mapping[str, XdmSequence], frame.variables)

    def _inherited_group(
        self,
        group: object,
        grouping_key: object,
        *,
        function: bool = False,
    ) -> tuple[object, object]:
        """Resolve a frame's group state from the caller or clear it for a call."""
        if function:
            return NO_GROUP, NO_GROUP
        if group is _INHERIT:
            group = NO_GROUP if self.current_group is None else tuple(self.current_group)
        if grouping_key is _INHERIT:
            grouping_key = (
                NO_GROUP if self.current_grouping_key is None else tuple(self.current_grouping_key)
            )
        return group, grouping_key

    @contextmanager
    def _group_scope(
        self,
        group: XdmSequence | None,
        grouping_key: XdmSequence | None,
    ) -> Iterator[None]:
        previous = (self.current_group, self.current_grouping_key)
        self.current_group = group
        self.current_grouping_key = grouping_key
        try:
            yield
        finally:
            self.current_group, self.current_grouping_key = previous

    def _frame_tunnel(self, frame: Frame) -> Mapping[str, XdmSequence]:
        return cast(Mapping[str, XdmSequence], frame.tunnel_parameters)

    def _rule_context(self, frame: Frame) -> _RuleContext | None:
        component = frame.component
        if isinstance(component, _RuleContext):
            return component
        if isinstance(component, _TemplateContext):
            return component.rule_context
        return None

    def _original_functions(self, frame: Frame) -> Mapping[int, Component]:
        component = frame.component
        if isinstance(component, _TemplateContext):
            return component.original_functions
        return {}

    def _return_frame(self, frame: Frame) -> Return:
        value: object = tuple(frame.values)
        if frame.conversion is not None:
            value = frame.conversion(value)
        return Return(value)

    def _step_instruction(self, frame: Frame) -> FrameResult:
        frame = self._consume_input(frame)
        program = cast(tuple[_Plan, ...], frame.program or ())
        cursor = cast(int, frame.cursor)
        if cursor >= len(program):
            return self._return_frame(frame)
        plan = program[cursor]
        rule_context = self._rule_context(frame)
        original_functions = self._original_functions(frame)
        if isinstance(plan, (_NextIterationPlan, _BreakPlan)):
            return self._iteration_control(plan, frame)
        if isinstance(plan, _ForEachPlan):
            return self._start_iteration_plan(plan, frame, cursor)
        if isinstance(plan, _IteratePlan):
            return self._start_iteration_plan(plan, frame, cursor)
        if isinstance(plan, _ForEachGroupPlan):
            return self._start_iteration_plan(plan, frame, cursor)
        if isinstance(plan, _PerformSortPlan):
            value = self._perform_sort(
                plan,
                frame.focus,
                self._frame_variables(frame),
                frame.package,
                mode=frame.mode,
                tunnel_parameters=self._frame_tunnel(frame),
                rule_context=rule_context,
                original_functions=original_functions,
            )
            return Continue(replace(frame, cursor=cursor + 1, values=frame.values + tuple(value)))
        if isinstance(plan, _ParamPlan):
            raise XsltError(
                "xsl:param is only supported at the start of a template or function",
                code="XTDE0000",
            )
        if isinstance(plan, _VariablePlan):
            value = self._variable_value(
                plan,
                frame.focus,
                self._frame_variables(frame),
                frame.package,
                mode=frame.mode,
                tunnel_parameters=self._frame_tunnel(frame),
                rule_context=rule_context,
                original_functions=original_functions,
            )
            return Continue(
                replace(
                    frame,
                    cursor=cursor + 1,
                    variables={**frame.variables, plan.name: value},
                )
            )
        if isinstance(plan, _RestoreVariablesPlan):
            return Continue(
                replace(
                    frame,
                    cursor=cursor + 1,
                    variables=plan.variables,
                )
            )
        if isinstance(plan, _SequencePlan):
            if plan.expression is not None:
                return self._start_expression(plan.expression, frame, cursor)
            return Continue(
                replace(
                    frame,
                    program=(
                        *plan.children,
                        _RestoreVariablesPlan(frame.variables),
                        *program[cursor + 1 :],
                    ),
                    cursor=0,
                )
            )
        if isinstance(plan, _ChoosePlan):
            branch = self._choose_branch(plan, frame, original_functions)
            if branch is None:
                return Continue(replace(frame, cursor=cursor + 1))
            return Continue(
                replace(
                    frame,
                    program=(
                        *branch,
                        _RestoreVariablesPlan(frame.variables),
                        *program[cursor + 1 :],
                    ),
                    cursor=0,
                )
            )
        if isinstance(plan, _CallTemplatePlan):
            return self._call_template_step(plan, frame, cursor, rule_context)
        if isinstance(plan, _ApplyTemplatesPlan):
            return self._apply_templates_step(plan, frame, cursor)
        if isinstance(plan, _OverridingPlan):
            return self._overriding_step(plan, frame, cursor, rule_context)
        try:
            value = self._execute_plan(
                plan,
                focus=frame.focus,
                locals_=self._frame_variables(frame),
                final_output=frame.final_output,
                package_name=frame.package,
                mode=frame.mode,
                tunnel_parameters=self._frame_tunnel(frame),
                rule_context=rule_context,
                original_functions=original_functions,
            )
        except _IterationControlSignal as signal:
            control_frame = replace(
                frame,
                variables=signal.variables,
                focus=signal.focus,
                values=frame.values + signal.pending,
            )
            return self._iteration_control(signal.plan, control_frame)
        return Continue(replace(frame, cursor=cursor + 1, values=frame.values + tuple(value)))

    # -- iteration, sorting, and grouping ---------------------------------

    def _start_iteration_plan(
        self,
        plan: _ForEachPlan | _IteratePlan | _ForEachGroupPlan,
        frame: Frame,
        cursor: int,
    ) -> FrameResult:
        variables = self._frame_variables(frame)
        rule_context = self._rule_context(frame)
        originals = self._original_functions(frame)
        if isinstance(plan, _ForEachPlan):
            items = self._evaluate(
                plan.expression,
                frame.focus,
                variables,
                frame.package,
                original_functions=originals,
            )
            items = self._sorted_items(
                items,
                plan.sorts,
                frame.focus,
                variables,
                frame.package,
                mode=frame.mode,
                tunnel_parameters=self._frame_tunnel(frame),
                rule_context=rule_context,
                original_functions=originals,
            )
            state = IterationState(
                kind="for-each",
                body=plan.children,
                items=tuple(
                    IterationItem(item, position, len(items))
                    for position, item in enumerate(items, start=1)
                ),
                base_variables=dict(variables),
            )
        elif isinstance(plan, _IteratePlan):
            items = self._evaluate(
                plan.expression,
                frame.focus,
                variables,
                frame.package,
                original_functions=originals,
            )
            parameters = self._initial_iteration_parameters(
                plan.parameters,
                frame.focus,
                variables,
                frame.package,
                mode=frame.mode,
                tunnel_parameters=self._frame_tunnel(frame),
                rule_context=rule_context,
                original_functions=originals,
            )
            state = IterationState(
                kind="iterate",
                body=plan.children,
                items=tuple(
                    IterationItem(item, position, len(items))
                    for position, item in enumerate(items, start=1)
                ),
                base_variables=dict(variables),
                parameters=parameters,
                parameter_plans={parameter.name: parameter for parameter in plan.parameters},
                on_completion=plan.on_completion,
            )
        else:
            groups = self._grouped_items(plan, frame, rule_context, originals)
            state = IterationState(
                kind="for-each-group",
                body=plan.children,
                items=tuple(
                    IterationItem(
                        group.initial_item,
                        position,
                        len(groups),
                        group.items,
                        group.grouping_key,
                    )
                    for position, group in enumerate(groups, start=1)
                ),
                base_variables=dict(variables),
            )
        child = Frame(
            kind="iteration",
            component=frame.component,
            identity=f"xsl:{state.kind}",
            program=state,
            variables=frame.variables,
            tunnel_parameters=frame.tunnel_parameters,
            focus=frame.focus,
            package=frame.package,
            mode=frame.mode,
            location=plan.instruction.location,
            final_output=frame.final_output,
            group=frame.group,
            grouping_key=frame.grouping_key,
        )
        return Call(child, resume=replace(frame, cursor=cursor + 1))

    def _step_iterate(self, frame: Frame) -> FrameResult:
        state = cast(IterationState, frame.program)
        body_returned = frame.input is not NO_INPUT
        frame = self._consume_input(frame)
        if body_returned:
            if state.control == "break":
                state.control = None
                return self._return_frame(frame)
            if state.control == "next":
                state.parameters = state.pending_parameters or state.parameters
                state.pending_parameters = None
            state.control = None
            state.index += 1
        if state.index >= len(state.items):
            return self._complete_iteration(frame, state)
        entry = state.items[state.index]
        variables: dict[str, XdmSequence] = dict(state.base_variables)
        if state.kind == "iterate":
            for name, value in state.parameters.items():
                self._bind(variables, name, value)
        group = frame.group if entry.group is None else entry.group
        if entry.group is None:
            grouping_key = frame.grouping_key
        else:
            grouping_key = NO_GROUP if entry.grouping_key is None else entry.grouping_key
        child = Frame(
            kind="instruction",
            component=frame.component,
            identity=f"iteration body {state.index + 1}",
            program=state.body,
            variables=variables,
            tunnel_parameters=frame.tunnel_parameters,
            focus=Focus.singleton(
                entry.item,
                position=entry.position,
                size=entry.size,
            ),
            package=frame.package,
            mode=frame.mode,
            location=frame.location,
            final_output=frame.final_output,
            group=NO_GROUP if group is None else group,
            grouping_key=NO_GROUP if grouping_key is None else grouping_key,
            iteration=state,
        )
        return Call(child, resume=frame, tail=False)

    def _complete_iteration(self, frame: Frame, state: IterationState) -> FrameResult:
        completion = state.on_completion
        if completion is None:
            return self._return_frame(frame)
        variables: dict[str, XdmSequence] = dict(state.base_variables)
        for name, value in state.parameters.items():
            self._bind(variables, name, value)
        try:
            if completion.select is not None:
                value = self._evaluate(
                    completion.select,
                    Focus.absent(),
                    variables,
                    frame.package,
                    original_functions=self._original_functions(frame),
                )
            else:
                value = self._execute_plans(
                    completion.children,
                    focus=Focus.absent(),
                    locals_=variables,
                    final_output=frame.final_output,
                    package_name=frame.package,
                    mode=frame.mode,
                    tunnel_parameters=self._frame_tunnel(frame),
                    rule_context=self._rule_context(frame),
                    original_functions=self._original_functions(frame),
                )
        except MissingContextError as exc:
            error = XsltDynamicError(
                "xsl:on-completion has no context item",
                code="XPDY0002",
                location=completion.instruction.location,
                cause=exc,
            )
            error.__cause__ = exc
            error.__suppress_context__ = True
            raise error from exc
        return self._return_frame(replace(frame, values=frame.values + tuple(value)))

    def _iteration_control(
        self,
        plan: _NextIterationPlan | _BreakPlan,
        frame: Frame,
    ) -> FrameResult:
        state = frame.iteration
        if not isinstance(state, IterationState) or state.kind != "iterate":
            raise XsltStaticError(
                f"xsl:{plan.instruction.local_name} requires an enclosing xsl:iterate",
                code="XTSE3120",
                uri=plan.instruction.module,
                location=plan.instruction.location,
            )
        if isinstance(plan, _NextIterationPlan):
            supplied = self._iteration_parameter_values(plan.parameters, frame, state)
            state.pending_parameters = {**state.parameters, **supplied}
            state.control = "next"
            return self._return_frame(frame)
        value = self._break_value(plan, frame)
        state.control = "break"
        return self._return_frame(replace(frame, values=frame.values + tuple(value)))

    def _break_value(self, plan: _BreakPlan, frame: Frame) -> XdmSequence:
        if plan.expression is not None:
            return self._evaluate(
                plan.expression,
                frame.focus,
                self._frame_variables(frame),
                frame.package,
                original_functions=self._original_functions(frame),
            )
        return self._execute_plans(
            plan.children,
            focus=frame.focus,
            locals_=self._frame_variables(frame),
            final_output=frame.final_output,
            package_name=frame.package,
            mode=frame.mode,
            tunnel_parameters=self._frame_tunnel(frame),
            rule_context=self._rule_context(frame),
            original_functions=self._original_functions(frame),
        )

    def _initial_iteration_parameters(
        self,
        parameters: tuple[_ParamPlan, ...],
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> dict[str, XdmSequence]:
        values: dict[str, XdmSequence] = {}
        environment = dict(locals_)
        for parameter in parameters:
            value = self._parameter_default(
                parameter,
                focus,
                environment,
                package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
            )
            value = self._coerce_parameter(parameter, value, code="XTTE0590")
            self._bind(values, parameter.name, value)
            self._bind(environment, parameter.name, value)
        return values

    def _iteration_parameter_values(
        self,
        parameters: tuple[_WithParamPlan, ...],
        frame: Frame,
        state: IterationState,
    ) -> dict[str, XdmSequence]:
        values: dict[str, XdmSequence] = {}
        for parameter in parameters:
            if parameter.expression is not None:
                value = self._evaluate(
                    parameter.expression,
                    frame.focus,
                    self._frame_variables(frame),
                    frame.package,
                    original_functions=self._original_functions(frame),
                )
            elif parameter.children:
                sequence = self._execute_plans(
                    parameter.children,
                    focus=frame.focus,
                    locals_=self._frame_variables(frame),
                    final_output=False,
                    package_name=frame.package,
                    mode=frame.mode,
                    tunnel_parameters=self._frame_tunnel(frame),
                    rule_context=self._rule_context(frame),
                    original_functions=self._original_functions(frame),
                )
                value = sequence if parameter.has_as else (_build_result_tree(sequence),)
            else:
                value = () if parameter.has_as else (XdmAtomic.string(""),)
            if parameter.sequence_type is not None:
                value = _coerce_sequence(
                    value,
                    parameter.sequence_type,
                    code="XTTE0590",
                    label=f"parameter {parameter.name!r}",
                )
            declared = state.parameter_plans[parameter.name]
            value = self._coerce_parameter(declared, value, code="XTTE0590")
            self._bind(values, parameter.name, value)
        return values

    # -- sort and group algorithms ---------------------------------------

    def _resolved_sort_keys(
        self,
        sorts: tuple[SortKey, ...],
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
    ) -> tuple[ResolvedSortKey, ...]:
        resolved: list[ResolvedSortKey] = []
        for sort in sorts:
            order = "ascending"
            if sort.order is not None:
                order = self._value_source(sort.order, focus, locals_, package_name).strip()
                if order not in ("ascending", "descending"):
                    raise XsltDynamicError(
                        f"xsl:sort order must be ascending or descending, got {order!r}",
                        code="XTDE0030",
                        location=sort.instruction.location,
                    )
            data_type = "native"
            if sort.data_type is not None:
                data_type_value = self._value_source(
                    sort.data_type,
                    focus,
                    locals_,
                    package_name,
                ).strip()
                local = (
                    data_type_value[2:].partition("}")[2]
                    if data_type_value.startswith("Q{")
                    else data_type_value.rpartition(":")[2]
                )
                if local == "text":
                    data_type = "text"
                elif local == "number":
                    data_type = "number"
                elif not local:
                    raise XsltDynamicError(
                        "xsl:sort data-type must not be empty",
                        code="XTDE0030",
                        location=sort.instruction.location,
                    )
            case_order = None
            if sort.case_order is not None:
                case_order = self._value_source(
                    sort.case_order,
                    focus,
                    locals_,
                    package_name,
                ).strip()
                if case_order not in ("upper-first", "lower-first"):
                    raise XsltDynamicError(
                        "xsl:sort case-order must be upper-first or lower-first, "
                        f"got {case_order!r}",
                        code="XTDE0030",
                        location=sort.instruction.location,
                    )
            if sort.stable is not None:
                stable = self._value_source(sort.stable, focus, locals_, package_name)
                if stable.strip() not in (
                    "yes",
                    "true",
                    "1",
                    "no",
                    "false",
                    "0",
                ):
                    raise XsltDynamicError(
                        f"xsl:sort stable has an invalid boolean value {stable!r}",
                        code="XTDE0030",
                        location=sort.instruction.location,
                    )
            language = ""
            if sort.lang is not None:
                language = self._value_source(sort.lang, focus, locals_, package_name).strip()
            collation_uri = None
            if sort.collation is not None:
                collation_uri = self._value_source(
                    sort.collation,
                    focus,
                    locals_,
                    package_name,
                ).strip()
            collation = resolve_collation(
                collation_uri,
                code="XTDE1035",
                default=None,
                base_uri=sort.base_uri,
                language=language if collation_uri is None else "",
                case_order=case_order if collation_uri is None else None,
            )
            resolved.append(
                ResolvedSortKey(
                    order=order,
                    collation=collation,
                    data_type=data_type,
                )
            )
        return tuple(resolved)

    def _sort_values_for(
        self,
        item: XdmItem,
        position: int,
        size: int,
        sorts: tuple[SortKey, ...],
        resolved: tuple[ResolvedSortKey, ...],
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        mode: str | None,
        tunnel_parameters: Mapping[str, XdmSequence] | None,
        rule_context: _RuleContext | None,
        original_functions: Mapping[int, Component] | None,
    ) -> tuple[XdmSequence, ...]:
        focus = Focus.singleton(item, position=position, size=size)
        values: list[XdmSequence] = []
        for sort, key in zip(sorts, resolved, strict=True):
            if sort.select is not None:
                value = self._evaluate(
                    sort.select,
                    focus,
                    locals_,
                    package_name,
                    original_functions=original_functions,
                )
            else:
                value = self._execute_plans(
                    sort.children,
                    focus=focus,
                    locals_=locals_,
                    final_output=False,
                    package_name=package_name,
                    mode=mode,
                    tunnel_parameters=tunnel_parameters,
                    rule_context=rule_context,
                    original_functions=original_functions,
                )
            atomics = atomize_key_sequence(value)
            if len(atomics) > 1:
                compatibility = self.profile_for(sort.xslt_version).compatibility_mode
                if compatibility:
                    atomics = atomics[:1]
                else:
                    raise XsltDynamicError(
                        "a sort key value must contain at most one item after atomization",
                        code="XTTE1020",
                        location=sort.instruction.location,
                    )
            if not atomics:
                values.append(())
                continue
            atomic = atomics[0]
            if key.data_type == "text":
                values.append((XdmAtomic.string(atomic.lexical),))
            else:
                values.append((atomic,))
        return tuple(values)

    def _sorted_items(
        self,
        items: XdmSequence,
        sorts: tuple[SortKey, ...],
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> tuple[XdmItem, ...]:
        if not sorts:
            return tuple(items)
        resolved = self._resolved_sort_keys(sorts, focus, locals_, package_name)
        key_values = tuple(
            self._sort_values_for(
                item,
                position,
                len(items),
                sorts,
                resolved,
                locals_,
                package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
            for position, item in enumerate(items, start=1)
        )
        return sort_records(tuple(items), resolved, key_values)

    def _perform_sort(
        self,
        plan: _PerformSortPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmSequence:
        if plan.expression is not None:
            items = self._evaluate(
                plan.expression,
                focus,
                locals_,
                package_name,
                original_functions=original_functions,
            )
        else:
            items = self._execute_plans(
                plan.children,
                focus=focus,
                locals_=locals_,
                final_output=False,
                package_name=package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
        return self._sorted_items(
            items,
            plan.sorts,
            focus,
            locals_,
            package_name,
            mode=mode,
            tunnel_parameters=tunnel_parameters,
            rule_context=rule_context,
            original_functions=original_functions,
        )

    def _group_collation(
        self,
        plan: _ForEachGroupPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
    ) -> Any:
        uri = (
            None
            if plan.collation is None
            else self._value_source(plan.collation, focus, locals_, package_name).strip()
        )
        return resolve_collation(
            uri,
            code="XTDE1110",
            default=plan.default_collation,
            base_uri=plan.base_uri,
        )

    def _grouped_items(
        self,
        plan: _ForEachGroupPlan,
        frame: Frame,
        rule_context: _RuleContext | None,
        original_functions: Mapping[int, Component] | None,
    ) -> tuple[Any, ...]:
        variables = self._frame_variables(frame)
        population = self._evaluate(
            plan.expression,
            frame.focus,
            variables,
            frame.package,
            original_functions=original_functions,
        )
        collation = self._group_collation(plan, frame.focus, variables, frame.package)
        if plan.algorithm == GROUP_BY:
            assert plan.key_expression is not None
            key_sets = tuple(
                grouping_key_set(
                    self._evaluate(
                        plan.key_expression,
                        Focus.singleton(item, position=position, size=len(population)),
                        variables,
                        frame.package,
                        original_functions=original_functions,
                    ),
                    composite=plan.composite,
                    collation=collation,
                )
                for position, item in enumerate(population, start=1)
            )
            groups = group_by(population, key_sets, collation)
        elif plan.algorithm == GROUP_ADJACENT:
            assert plan.key_expression is not None
            keys = tuple(
                adjacent_grouping_key(
                    self._evaluate(
                        plan.key_expression,
                        Focus.singleton(item, position=position, size=len(population)),
                        variables,
                        frame.package,
                        original_functions=original_functions,
                    ),
                    composite=plan.composite,
                )
                for position, item in enumerate(population, start=1)
            )
            groups = group_adjacent(population, keys, collation)
        else:
            assert plan.pattern is not None
            matcher = PatternMatcher(plan.pattern)
            environment = self._pattern_environment_for(plan.pattern, frame.package)
            with self._group_scope(None, None):
                matches = tuple(matcher.matches(item, environment) for item in population)
            if plan.algorithm == GROUP_STARTING_WITH:
                groups = group_starting_with(population, matches)
            else:
                groups = group_ending_with(population, matches)
        if not plan.sorts:
            return groups
        resolved = self._resolved_sort_keys(
            plan.sorts,
            frame.focus,
            variables,
            frame.package,
        )
        key_values: list[tuple[XdmSequence, ...]] = []
        for position, group in enumerate(groups, start=1):
            with self._group_scope(group.items, group.grouping_key):
                key_values.append(
                    self._sort_values_for(
                        group.initial_item,
                        position,
                        len(groups),
                        plan.sorts,
                        resolved,
                        variables,
                        frame.package,
                        mode=frame.mode,
                        tunnel_parameters=self._frame_tunnel(frame),
                        rule_context=rule_context,
                        original_functions=original_functions,
                    )
                )
        return sort_records(groups, resolved, tuple(key_values))

    def _run_iteration_plan(
        self,
        plan: _ForEachPlan | _IteratePlan | _ForEachGroupPlan,
        *,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
        package_name: str | None,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmSequence:
        component = (
            _TemplateContext(
                cast(Component, None),
                rule_context,
                dict(original_functions or {}),
            )
            if rule_context is not None or original_functions
            else None
        )
        parent = Frame(
            kind="instruction",
            component=component,
            program=(plan,),
            focus=focus,
            variables=locals_,
            tunnel_parameters=tunnel_parameters or {},
            package=package_name,
            mode=mode,
            final_output=final_output,
            group=NO_GROUP if self.current_group is None else self.current_group,
            grouping_key=(
                NO_GROUP if self.current_grouping_key is None else self.current_grouping_key
            ),
        )
        return cast(XdmSequence, self.execute_frame(parent))

    def _start_expression(
        self,
        expression: CompiledExpression,
        frame: Frame,
        cursor: int,
    ) -> FrameResult:
        originals = self._original_functions(frame)
        functions: dict[tuple[str, int], HostFunction] = dict(
            self.stylesheet_functions(frame.package)
        )
        for arity, component in originals.items():
            functions[(f"{{{XSLT_NS}}}original", arity)] = HostFunction(
                ComponentCall(component),
                name=f"{{{XSLT_NS}}}original",
                arity=arity,
                suspend=True,
            )
        machine: ContinuationMachine | None = None
        if not self._expression_references_global(expression, frame.package):
            environment = self._environment(
                self._frame_variables(frame),
                frame.package,
                original_functions=originals,
            )
            machine = self.xpath_machine(
                expression,
                environment.variables,
                functions,
            )
        if machine is None:
            value = self._evaluate(
                expression,
                frame.focus,
                self._frame_variables(frame),
                frame.package,
                original_functions=originals,
            )
            return Continue(
                replace(
                    frame,
                    cursor=cursor + 1,
                    values=frame.values + tuple(value),
                )
            )
        xpath_frame = Frame(
            kind="xpath",
            identity=f"xpath {expression.source}",
            program=_XPathState(machine),
            focus=frame.focus,
            package=frame.package,
            location=getattr(expression.static_context, "location", None),
            group=frame.group,
            grouping_key=frame.grouping_key,
        )
        return Call(
            xpath_frame,
            resume=replace(frame, cursor=cursor + 1),
            tail=False,
        )

    def _choose_branch(
        self,
        plan: _ChoosePlan,
        frame: Frame,
        original_functions: Mapping[int, Component] | None = None,
    ) -> tuple[_Plan, ...] | None:
        for test, branch in plan.branches:
            if _effective_boolean(
                self._evaluate(
                    test,
                    frame.focus,
                    self._frame_variables(frame),
                    frame.package,
                    original_functions=original_functions,
                )
            ):
                return branch
        return plan.otherwise

    def _plan_parameters(
        self,
        parameters: tuple[_WithParamPlan, ...],
        frame: Frame,
    ) -> tuple[Mapping[str, XdmSequence], Mapping[str, XdmSequence]]:
        return self._plan_parameters_for(
            parameters,
            frame.focus,
            self._frame_variables(frame),
            frame.package,
            mode=frame.mode,
            tunnel_parameters=self._frame_tunnel(frame),
            rule_context=self._rule_context(frame),
            original_functions=self._original_functions(frame),
        )

    def _plan_parameters_for(
        self,
        parameters: tuple[_WithParamPlan, ...],
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> tuple[Mapping[str, XdmSequence], Mapping[str, XdmSequence]]:
        template: dict[str, XdmSequence] = {}
        tunnel: dict[str, XdmSequence] = {}
        for parameter in parameters:
            if parameter.expression is not None:
                value = self._evaluate(
                    parameter.expression,
                    focus,
                    locals_,
                    package_name,
                    original_functions=original_functions,
                )
            elif parameter.children:
                value = self._execute_plans(
                    parameter.children,
                    focus=focus,
                    locals_=locals_,
                    final_output=False,
                    package_name=package_name,
                    mode=mode,
                    tunnel_parameters=tunnel_parameters,
                    rule_context=rule_context,
                    original_functions=original_functions,
                )
            else:
                value = ()
            if parameter.sequence_type is not None:
                value = _coerce_sequence(
                    value,
                    parameter.sequence_type,
                    code="XTTE0590",
                    label=f"parameter {parameter.name!r}",
                )
            (tunnel if parameter.tunnel else template)[parameter.name] = value
        return MappingProxyType(template), MappingProxyType(tunnel)

    def _resolve_template(self, reference: Any) -> Component:
        stylesheet = getattr(self, "stylesheet", None)
        packages = getattr(stylesheet, "_packages", {})
        package = packages.get(
            getattr(reference, "containing", None) or getattr(self.package, "name", None)
        )
        if package is None:
            package = self.package
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

    def _call_template_step(
        self,
        plan: _CallTemplatePlan,
        frame: Frame,
        cursor: int,
        rule_context: _RuleContext | None,
    ) -> FrameResult:
        template, tunnel = self._plan_parameters(plan.parameters, frame)
        effective_tunnel = {**self._frame_tunnel(frame), **tunnel}
        component = self._resolve_template(plan.reference)
        child = self._component_frame(
            component,
            template_parameters=template,
            tunnel_parameters=effective_tunnel,
            final_output=frame.final_output,
            focus=frame.focus,
            mode=frame.mode,
            rule_context=rule_context,
            group=frame.group,
            grouping_key=frame.grouping_key,
        )
        return Call(
            child,
            resume=replace(frame, cursor=cursor + 1),
            tail=plan.tail,
        )

    def _resolve_mode(
        self,
        mode: str | None,
        current: str | None,
        package_name: str | None = None,
    ) -> str | None:
        if mode == "#current":
            return current
        if mode == "#default":
            stylesheet = getattr(self, "stylesheet", None)
            packages = getattr(stylesheet, "_packages", {})
            package = packages.get(package_name)
            if package is None:
                package = self.package
            return package.default_mode
        return mode

    def _overriding_rule(
        self,
        plan: _OverridingPlan,
        rule_context: _RuleContext,
        item: XdmItem,
    ) -> RuleMatch | None:
        dispatcher = self._dispatcher(rule_context.mode, rule_context.package_name)
        environment = self._environment({}, rule_context.package_name)
        if plan.kind == "next-match":
            return dispatcher.next_match(
                item,
                rule_context.match,
                rule_context.mode,
                environment,
                self._pattern_environment,
            )
        return dispatcher.apply_imports(
            item,
            rule_context.match,
            rule_context.mode,
            environment,
            self._pattern_environment,
        )

    def _overriding_step(
        self,
        plan: _OverridingPlan,
        frame: Frame,
        cursor: int,
        rule_context: _RuleContext | None,
    ) -> FrameResult:
        template, tunnel = self._plan_parameters(plan.parameters, frame)
        if rule_context is None:
            raise XsltDynamicError(
                "xsl:next-match and xsl:apply-imports require a current template rule",
                code="XTDE0560",
            )
        item = rule_context.focus.item
        if item is None:  # pragma: no cover - a current rule always has a focus item
            raise XsltDynamicError("the current template rule has no context item")
        effective_tunnel = {**rule_context.tunnel_parameters, **tunnel}
        match = self._overriding_rule(plan, rule_context, item)
        if match is None:
            value = self._builtin_value(
                item,
                rule_context.mode,
                template,
                effective_tunnel,
                rule_context.package_name,
            )
            return Continue(replace(frame, cursor=cursor + 1, values=frame.values + tuple(value)))
        child = self._rule_frame(
            match,
            rule_context.focus,
            template,
            effective_tunnel,
            group=frame.group,
            grouping_key=frame.grouping_key,
        )
        return Call(
            child,
            resume=replace(frame, cursor=cursor + 1),
            tail=plan.tail,
        )

    def _apply_templates_step(
        self,
        plan: _ApplyTemplatesPlan,
        frame: Frame,
        cursor: int,
    ) -> FrameResult:
        items = self._evaluate(
            plan.expression,
            frame.focus,
            self._frame_variables(frame),
            frame.package,
        )
        items = self._sorted_items(
            items,
            plan.sorts,
            frame.focus,
            self._frame_variables(frame),
            frame.package,
            mode=frame.mode,
            tunnel_parameters=self._frame_tunnel(frame),
            rule_context=self._rule_context(frame),
            original_functions=self._original_functions(frame),
        )
        template, tunnel = self._plan_parameters(plan.parameters, frame)
        child = self._apply_frame(
            items,
            self._resolve_mode(plan.mode, frame.mode, frame.package),
            template,
            {**self._frame_tunnel(frame), **tunnel},
            tail=plan.tail,
            package_name=frame.package,
            group=frame.group,
            grouping_key=frame.grouping_key,
        )
        return Call(
            child,
            resume=replace(frame, cursor=cursor + 1),
            tail=plan.tail,
        )

    def _apply_frame(
        self,
        items: XdmSequence,
        mode: str | None,
        template_parameters: Mapping[str, XdmSequence],
        tunnel_parameters: Mapping[str, XdmSequence],
        *,
        tail: bool = False,
        package_name: str | None = None,
        group: object = _INHERIT,
        grouping_key: object = _INHERIT,
    ) -> Frame:
        effective_group, effective_grouping_key = self._inherited_group(group, grouping_key)
        state = _ApplyState(
            items=items,
            mode=mode,
            template_parameters=template_parameters,
            tunnel_parameters=tunnel_parameters,
            package_name=package_name,
            group=effective_group,
            grouping_key=effective_grouping_key,
        )
        return Frame(
            kind="apply",
            identity=f"apply-templates mode={mode!r}",
            program=state,
            tunnel_parameters=tunnel_parameters,
            mode=mode,
            package=package_name,
            tail=tail,
            group=effective_group,
            grouping_key=effective_grouping_key,
        )

    def _mode_layers(
        self,
        mode: str | None,
        package_name: str | None,
    ) -> tuple[_ModeLayerPlan, ...]:
        principal = self.stylesheet._package_modes.get(self.package.name, {})
        if mode is not None and mode in principal:
            return principal.get(mode, ())
        package_modes = self.stylesheet._package_modes.get(package_name, {})
        return package_modes.get(mode, ())

    def _dispatcher(self, mode: str | None, package_name: str | None = None) -> RuleDispatcher:
        key = (mode, package_name)
        dispatcher = self._mode_dispatchers.get(key)
        if dispatcher is None:
            layers = tuple(
                (layer_index, PatternIndex(layer.rules))
                for layer_index, layer in enumerate(self._mode_layers(mode, package_name))
            )
            properties = self._mode_properties(mode, package_name)
            dispatcher = RuleDispatcher(
                layers,
                on_multiple_match=properties.get("on-multiple-match", "use-last"),
            )
            self._mode_dispatchers[key] = dispatcher
        return dispatcher

    def _mode_properties(
        self,
        mode: str | None,
        package_name: str | None = None,
    ) -> Mapping[str, str]:
        if mode is None:
            package = self.stylesheet._packages[package_name]
            declaration = package.mode(mode)
            return {} if declaration is None else declaration.properties
        principal = self.stylesheet._packages[self.package.name]
        declaration = principal.mode(mode)
        if declaration is not None:
            return declaration.properties
        package = self.stylesheet._packages[package_name]
        declaration = package.mode(mode)
        if declaration is None:
            return {}
        return declaration.properties

    def _pattern_environment(self, rule: TemplateRule) -> ExpressionEnvironment:
        body = cast(_BodyPlan, rule.template)
        return self._pattern_environment_for(rule.pattern, body.owner)

    def _pattern_environment_for(
        self,
        pattern: CompiledPattern,
        package_name: str | None,
    ) -> ExpressionEnvironment:
        key = id(pattern)
        environment = self._pattern_environments.get(key)
        if environment is None:
            environment = self._environment(
                {},
                package_name,
                referenced=_pattern_variable_names(pattern),
                static_context=pattern.context,
            )
            self._pattern_environments[key] = environment
        return environment

    def _rule_frame(
        self,
        match: RuleMatch,
        focus: Focus,
        template_parameters: Mapping[str, XdmSequence],
        tunnel_parameters: Mapping[str, XdmSequence],
        *,
        group: object = _INHERIT,
        grouping_key: object = _INHERIT,
    ) -> Frame:
        rule = match.rule
        body = cast(_BodyPlan, rule.template)
        rule_focus = self._apply_context_item(body.context_item, focus)
        locals_, index = self._bind_template_parameters(
            body.body,
            template_parameters,
            tunnel_parameters,
            rule_focus,
            body.owner,
            mode=rule.mode,
        )
        conversion = None
        if body.result_type is not None:
            conversion = lambda value: _coerce_sequence(  # noqa: E731
                cast(XdmSequence, value),
                body.result_type,
                code="XTTE0505",
                label="template rule result",
            )
        context = _RuleContext(
            match=match,
            focus=rule_focus,
            mode=rule.mode,
            package_name=body.owner,
            tunnel_parameters=tunnel_parameters,
        )
        effective_group, effective_grouping_key = self._inherited_group(group, grouping_key)
        frame = Frame(
            kind="rule",
            component=context,
            identity=f"rule {rule.name or rule.pattern}",
            program=body.body[index:],
            variables=locals_,
            tunnel_parameters=tunnel_parameters,
            focus=rule_focus,
            package=body.owner,
            mode=rule.mode,
            location=rule.pattern.context.location,
            final_output=True,
            conversion=conversion,
            group=effective_group,
            grouping_key=effective_grouping_key,
        )
        return self.frame_boundary(frame) if conversion is not None else frame

    def _bind_template_parameters(
        self,
        body: tuple[_Plan, ...],
        template_parameters: Mapping[str, XdmSequence],
        tunnel_parameters: Mapping[str, XdmSequence],
        focus: Focus,
        package_name: str | None,
        *,
        mode: str | None = None,
    ) -> tuple[dict[str, XdmSequence], int]:
        locals_: dict[str, XdmSequence] = {}
        index = 0
        while index < len(body) and isinstance(body[index], _ParamPlan):
            parameter = cast(_ParamPlan, body[index])
            supplied = (
                tunnel_parameters.get(parameter.name)
                if parameter.tunnel
                else template_parameters.get(parameter.name)
            )
            value = (
                supplied
                if supplied is not None
                else self._parameter_default(
                    parameter,
                    focus,
                    locals_,
                    package_name,
                    mode=mode,
                    tunnel_parameters=tunnel_parameters,
                )
            )
            value = self._coerce_parameter(parameter, value, code="XTTE0590")
            self._bind(locals_, parameter.name, value)
            index += 1
        return locals_, index

    def _step_apply(self, frame: Frame) -> FrameResult:
        frame = self._consume_input(frame)
        state = cast(_ApplyState, frame.program)
        if state.index >= len(state.items):
            return self._return_frame(frame)
        item = state.items[state.index]
        focus = Focus.singleton(item, position=state.index + 1, size=len(state.items))
        rule = self._dispatcher(state.mode, state.package_name).select(
            item,
            state.mode,
            self._environment({}, state.package_name),
            self._pattern_environment,
        )
        next_state = replace(state, index=state.index + 1)
        resume = replace(frame, program=next_state)
        last = state.index + 1 == len(state.items)
        tail = frame.tail and last
        if rule is None:
            builtin = self._builtin_step(item, state, resume, tail)
            if builtin is not None:
                return builtin
            return Continue(replace(frame, program=next_state))
        rule_frame = self._rule_frame(
            rule,
            focus,
            state.template_parameters,
            state.tunnel_parameters,
            group=state.group,
            grouping_key=state.grouping_key,
        )
        return Call(rule_frame, resume=resume, tail=tail)

    def _builtin_step(
        self,
        item: XdmItem,
        state: _ApplyState,
        resume: Frame,
        tail: bool,
    ) -> FrameResult | None:
        variant = self._builtin_variant(state.mode, state.package_name)
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
                            self._copy_item(item, deep=variant == "deep-copy"),
                        ),
                    )
                )
            if variant in ("text-only-copy", "shallow-skip"):
                if not item.members:
                    return None
                child = self._apply_frame(
                    _array_member_items(item),
                    state.mode,
                    state.template_parameters,
                    state.tunnel_parameters,
                    tail=tail,
                    package_name=state.package_name,
                    group=state.group,
                    grouping_key=state.grouping_key,
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
                child = self._apply_frame(
                    item.children,
                    state.mode,
                    state.template_parameters,
                    state.tunnel_parameters,
                    tail=tail,
                    package_name=state.package_name,
                    group=state.group,
                    grouping_key=state.grouping_key,
                )
                return Call(child, resume=resume, tail=tail)
            if isinstance(item, XdmNode) and item.kind in (NodeKind.TEXT, NodeKind.ATTRIBUTE):
                return Continue(
                    replace(
                        resume,
                        values=(*resume.values, self._text_node(item.string_value)),
                    )
                )
            if isinstance(item, XdmAtomic):
                return Continue(
                    replace(
                        resume,
                        values=(*resume.values, self._text_node(item.lexical)),
                    )
                )
            return None
        if variant == "deep-skip":
            if isinstance(item, XdmNode) and item.kind is NodeKind.DOCUMENT:
                child = self._apply_frame(
                    item.children,
                    state.mode,
                    state.template_parameters,
                    state.tunnel_parameters,
                    tail=tail,
                    package_name=state.package_name,
                    group=state.group,
                    grouping_key=state.grouping_key,
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
                    attributes = self._apply_templates(
                        item.attributes,
                        state.mode,
                        state.template_parameters,
                        state.tunnel_parameters,
                        package_name=state.package_name,
                    )
                if attributes:
                    resume = replace(
                        resume,
                        values=resume.values + tuple(attributes),
                    )
                if not item.children:
                    return Continue(resume)
                child = self._apply_frame(
                    item.children,
                    state.mode,
                    state.template_parameters,
                    state.tunnel_parameters,
                    tail=tail,
                    package_name=state.package_name,
                    group=state.group,
                    grouping_key=state.grouping_key,
                )
                return Call(child, resume=resume, tail=tail)
            return None
        if variant == "deep-copy":
            return Continue(
                replace(
                    resume,
                    values=(*resume.values, self._copy_item(item, deep=True)),
                )
            )
        assert variant == "shallow-copy"
        return Continue(
            replace(
                resume,
                values=(
                    *resume.values,
                    self._shallow_copy(item, state.mode, state.template_parameters, state),
                ),
            )
        )

    def _builtin_variant(self, mode: str | None, package_name: str | None = None) -> str:
        variant = self._mode_properties(mode, package_name).get("on-no-match", "text-only-copy")
        if variant not in {
            "text-only-copy",
            "deep-copy",
            "shallow-copy",
            "deep-skip",
            "shallow-skip",
            "fail",
        }:
            raise XsltStaticError(
                f"invalid on-no-match value {variant!r}",
                code="XTSE0020",
            )
        return variant

    def _builtin_value(
        self,
        item: XdmItem,
        mode: str | None,
        template_parameters: Mapping[str, XdmSequence],
        tunnel_parameters: Mapping[str, XdmSequence],
        package_name: str | None = None,
    ) -> XdmSequence:
        """Evaluate the built-in rule for *item* outside the apply frame."""
        variant = self._builtin_variant(mode, package_name)
        if variant == "fail":
            raise XsltDynamicError(
                "no template rule matches and the mode forbids the built-in rule",
                code="XTDE0555",
            )
        if isinstance(item, XdmArray):
            if variant in ("deep-copy", "shallow-copy"):
                return (item,)
            if variant in ("text-only-copy", "shallow-skip"):
                return self._apply_templates(
                    _array_member_items(item),
                    mode,
                    template_parameters,
                    tunnel_parameters,
                    package_name=package_name,
                )
            return ()
        if variant == "text-only-copy":
            if isinstance(item, XdmNode):
                if item.kind in (NodeKind.DOCUMENT, NodeKind.ELEMENT):
                    return self._apply_templates(
                        item.children,
                        mode,
                        template_parameters,
                        tunnel_parameters,
                        package_name=package_name,
                    )
                if item.kind in (NodeKind.TEXT, NodeKind.ATTRIBUTE):
                    return (self._text_node(item.string_value),)
                return ()
            if isinstance(item, XdmAtomic):
                return (self._text_node(item.lexical),)
            return ()
        if variant == "deep-skip":
            if isinstance(item, XdmNode) and item.kind is NodeKind.DOCUMENT:
                return self._apply_templates(
                    item.children,
                    mode,
                    template_parameters,
                    tunnel_parameters,
                    package_name=package_name,
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
                        self._apply_templates(
                            item.attributes,
                            mode,
                            template_parameters,
                            tunnel_parameters,
                            package_name=package_name,
                        )
                    )
                values.extend(
                    self._apply_templates(
                        item.children,
                        mode,
                        template_parameters,
                        tunnel_parameters,
                        package_name=package_name,
                    )
                )
                return tuple(values)
            return ()
        if variant == "deep-copy":
            return (self._copy_item(item, deep=True),)
        return (
            self._shallow_copy(
                item,
                mode,
                template_parameters,
                _ApplyState(
                    (),
                    mode,
                    template_parameters,
                    tunnel_parameters,
                    package_name=package_name,
                ),
            ),
        )

    def _copy_item(self, item: XdmItem, *, deep: bool) -> XdmItem:
        if not isinstance(item, XdmNode):
            return item
        return _NodeFactory().clone(item, parent=None, deep=deep)

    def _shallow_copy(
        self,
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
            children = self._apply_templates(
                item.children,
                mode,
                template_parameters,
                state.tunnel_parameters,
                package_name=state.package_name,
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
        attributes = self._apply_templates(
            item.attributes,
            mode,
            template_parameters,
            state.tunnel_parameters,
            package_name=state.package_name,
        )
        _append_complex_content(factory, element, attributes, allow_attributes=True)
        children = self._apply_templates(
            item.children,
            mode,
            template_parameters,
            state.tunnel_parameters,
            package_name=state.package_name,
        )
        _append_complex_content(factory, element, children, allow_attributes=False)
        return element

    def _step_xpath(self, frame: Frame) -> FrameResult:
        state = cast(_XPathState, frame.program)
        machine = state.machine
        if frame.input is NO_INPUT:
            outcome = machine.run_until_suspension()
        else:
            native = self.sequence_to_native(frame.input)
            if native is None:
                raise XsltError(
                    "a stylesheet function returned a value outside the scheduler bridge",
                    code=None,
                )
            outcome = machine.resume(native)
        self.note_machine(machine)
        if isinstance(outcome, Suspension):
            return self._suspend_xpath(frame, outcome)
        return Return(self.native_to_xdm(outcome))

    def _suspend_xpath(self, frame: Frame, suspension: Suspension) -> Call:
        target = suspension.request.target
        if not isinstance(target, ComponentCall) or target.kind != "function":
            raise XsltError(
                "XPath requested host work that is not a stylesheet function",
                code=None,
            )
        arguments = self.component_arguments(suspension.request.arguments)
        child = self._component_frame(
            cast(Component, target.component),
            arguments=arguments,
            final_output=False,
        )
        return Call(child, resume=frame, tail=False)

    def _apply_context_item(
        self,
        signature: ContextItemSignature | None,
        focus: Focus,
    ) -> Focus:
        if signature is None:
            return focus
        if signature.use == "absent":
            return Focus.absent()
        if focus.is_absent:
            if signature.use == "required":
                raise XsltError(
                    "the template requires a context item but none was supplied",
                    code="XTTE3090",
                )
            return focus
        assert focus.item is not None
        sequence = _coerce_sequence(
            (focus.item,),
            signature.sequence_type,
            code="XTTE0590",
            label="context item",
        )
        return Focus.singleton(
            sequence[0],
            position=focus.position,
            size=focus.size,
        )

    def _coerce_parameter(
        self,
        parameter: _ParamPlan,
        value: XdmSequence,
        *,
        code: str,
    ) -> XdmSequence:
        if parameter.sequence_type is None:
            return value
        return _coerce_sequence(
            value,
            parameter.sequence_type,
            code=code,
            label=f"parameter {parameter.name!r}",
        )

    def _coerce_component_result(
        self,
        component: Component,
        value: XdmSequence,
    ) -> XdmSequence:
        declaration_as = component.declaration.attribute("as")
        signature = component.signature
        if declaration_as is None or signature is None or signature.return_type is None:
            return value
        code = "XTTE0780" if component.kind == "function" else "XTTE0505"
        label = "function result" if component.kind == "function" else "template result"
        return _coerce_sequence(value, signature.return_type, code=code, label=label)

    def _parameter_default(
        self,
        parameter: _ParamPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
    ) -> XdmSequence:
        if parameter.expression is not None:
            return self._evaluate(parameter.expression, focus, locals_, package_name)
        if parameter.children:
            if parameter.has_as:
                return self._execute_plans(
                    parameter.children,
                    focus=focus,
                    locals_=locals_,
                    final_output=False,
                    package_name=package_name,
                    mode=mode,
                    tunnel_parameters=tunnel_parameters,
                )
            sequence = self._execute_plans(
                parameter.children,
                focus=focus,
                locals_=locals_,
                final_output=False,
                package_name=package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
            )
            return (_build_result_tree(sequence),)
        if parameter.required:
            raise XsltError(
                f"the required parameter {parameter.name!r} was not supplied",
                code="XTDE0700",
            )
        if parameter.has_as:
            return ()
        return (XdmAtomic.string(""),)

    def _global_value(self, component: Component) -> XdmSequence:
        assert component.name is not None
        key = (component.owner, component.name)
        cached = self.globals.get(key)
        if cached is not None:
            return cached
        if key in self._evaluating:
            raise XsltError(
                f"circular dependency in the global variable {component.name!r}",
                code="XTDE0640",
            )
        self._evaluating.append(key)
        try:
            with self._group_scope(None, None):
                declaration = component.declaration
                plan = self._component_plan(component)
                original_locals: dict[str, XdmSequence] = {}
                if plan.original_variable is not None:
                    original_locals[f"{{{XSLT_NS}}}original"] = self._global_value(
                        plan.original_variable
                    )
                original_functions = plan.original_functions
                static = declaration.attribute("static")
                if static is not None and _yes_no_runtime(static):
                    static_values = self.stylesheet._static_variables.get(component.owner, {})
                    static_value = static_values[component.name]
                    self.globals[key] = static_value
                    return static_value
                is_param = declaration.local_name == "param"
                if is_param and key in self.options.parameters:
                    value = self.options.parameters[key]
                    value = self._coerce_global(plan, component, value)
                    self.globals[key] = value
                    return value
                if is_param and _yes_no_runtime(declaration.attribute("required")):
                    raise XsltError(
                        f"the required global parameter {component.name!r} was not supplied",
                        code="XTDE0050",
                    )
                focus = self._global_focus(component)
                if plan.select is not None:
                    value = self._evaluate(
                        plan.select,
                        focus,
                        original_locals,
                        component.owner,
                        original_functions=original_functions,
                    )
                elif plan.body:
                    if plan.has_as:
                        value = self._execute_plans(
                            plan.body,
                            focus=focus,
                            locals_=original_locals,
                            final_output=False,
                            package_name=component.owner,
                            original_functions=original_functions,
                        )
                    else:
                        sequence = self._execute_plans(
                            plan.body,
                            focus=focus,
                            locals_=original_locals,
                            final_output=False,
                            package_name=component.owner,
                            original_functions=original_functions,
                        )
                        value = (_build_result_tree(sequence),)
                elif plan.has_as:
                    value = ()
                else:
                    value = (XdmAtomic.string(""),)
                value = self._coerce_global(plan, component, value)
                self.globals[key] = value
                return value
        finally:
            self._evaluating.pop()

    def _coerce_global(
        self,
        plan: _ComponentPlan,
        component: Component,
        value: XdmSequence,
    ) -> XdmSequence:
        if plan.sequence_type is None:
            return value
        code = "XTTE0590" if component.declaration.local_name == "param" else "XTTE0570"
        return _coerce_sequence(
            value,
            plan.sequence_type,
            code=code,
            label=f"global variable {component.name!r}",
        )

    def _global_focus(self, component: Component) -> Focus:
        if component.owner != self.package.name:
            return Focus.absent()
        item = self.options.global_context_item
        return Focus.singleton(item) if item is not None else Focus.absent()

    # -- plan execution ----------------------------------------------------

    def _execute_plans(
        self,
        plans: Iterable[_Plan],
        *,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
        package_name: str | None,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmSequence:
        result: list[XdmItem] = []
        local_scope = dict(locals_)
        try:
            for plan in plans:
                if isinstance(plan, _ParamPlan):
                    raise XsltError(
                        "xsl:param is only supported at the start of a template or function",
                        code="XTDE0000",
                    )
                if isinstance(plan, _VariablePlan):
                    local_scope[plan.name] = self._variable_value(
                        plan,
                        focus,
                        local_scope,
                        package_name,
                        mode=mode,
                        tunnel_parameters=tunnel_parameters,
                        rule_context=rule_context,
                        original_functions=original_functions,
                    )
                    continue
                result.extend(
                    self._execute_plan(
                        plan,
                        focus=focus,
                        locals_=local_scope,
                        final_output=final_output,
                        package_name=package_name,
                        mode=mode,
                        tunnel_parameters=tunnel_parameters,
                        rule_context=rule_context,
                        original_functions=original_functions,
                    )
                )
        except _IterationControlSignal as signal:
            signal.prepend(result)
            raise
        return tuple(result)

    def _execute_plan(
        self,
        plan: _Plan,
        *,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
        package_name: str | None,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmSequence:
        if isinstance(plan, _TextPlan):
            return (self._text_node(plan.value),)
        if isinstance(plan, (_NextIterationPlan, _BreakPlan)):
            active = getattr(self, "_active_iteration_frame", None)
            if active is not None and isinstance(active.iteration, IterationState):
                raise _IterationControlSignal(plan, locals_, focus)
            raise XsltStaticError(
                f"xsl:{plan.instruction.local_name} requires an enclosing xsl:iterate",
                code="XTSE3120",
                uri=plan.instruction.module,
                location=plan.instruction.location,
            )
        if isinstance(plan, (_ForEachPlan, _IteratePlan, _ForEachGroupPlan)):
            return self._run_iteration_plan(
                plan,
                focus=focus,
                locals_=locals_,
                final_output=final_output,
                package_name=package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
        if isinstance(plan, _PerformSortPlan):
            return self._perform_sort(
                plan,
                focus,
                locals_,
                package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
        if isinstance(plan, _TvtPlan):
            return (
                self._text_node(self._value_template(plan.template, focus, locals_, package_name)),
            )
        if isinstance(plan, _AttributePlan):
            return self._attribute_value(
                plan,
                focus,
                locals_,
                final_output,
                package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
        if isinstance(plan, _SequencePlan):
            if plan.expression is not None:
                return self._evaluate(
                    plan.expression,
                    focus,
                    locals_,
                    package_name,
                    original_functions=original_functions,
                )
            return self._execute_plans(
                plan.children,
                focus=focus,
                locals_=locals_,
                final_output=final_output,
                package_name=package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
        if isinstance(plan, _ValueOfPlan):
            if plan.expression is not None:
                values = self._evaluate(
                    plan.expression,
                    focus,
                    locals_,
                    package_name,
                    original_functions=original_functions,
                )
            else:
                values = self._execute_plans(
                    plan.children,
                    focus=focus,
                    locals_=locals_,
                    final_output=final_output,
                    package_name=package_name,
                    mode=mode,
                    tunnel_parameters=tunnel_parameters,
                    rule_context=rule_context,
                    original_functions=original_functions,
                )
            if plan.expression is not None and not plan.explicit_separator:
                profile = self.profile_for(plan.expression.static_context.xslt_version)
                if profile.compatibility_mode:
                    return (self._text_node(profile.string_sequence(values)),)
            separator = self._value_source(plan.separator, focus, locals_, package_name)
            text = _simple_content_string(values, separator)
            return (self._text_node(text),)
        if isinstance(plan, _LiteralPlan):
            return (
                self._literal_element(
                    plan,
                    focus,
                    locals_,
                    final_output,
                    package_name,
                    mode=mode,
                    tunnel_parameters=tunnel_parameters,
                    rule_context=rule_context,
                    original_functions=original_functions,
                ),
            )
        if isinstance(plan, _ChoosePlan):
            branch = None
            for test, candidate in plan.branches:
                if _effective_boolean(
                    self._evaluate(
                        test,
                        focus,
                        locals_,
                        package_name,
                        original_functions=original_functions,
                    )
                ):
                    branch = candidate
                    break
            if branch is None:
                branch = plan.otherwise
            if branch is None:
                return ()
            return self._execute_plans(
                branch,
                focus=focus,
                locals_=locals_,
                final_output=final_output,
                package_name=package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
        if isinstance(plan, _MapPlan):
            values = self._execute_plans(
                plan.children,
                focus=focus,
                locals_=locals_,
                final_output=final_output,
                package_name=package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
            return (construct_map(values),)
        if isinstance(plan, _MapEntryPlan):
            key = self._evaluate(
                plan.key,
                focus,
                locals_,
                package_name,
                original_functions=original_functions,
            )
            if plan.expression is not None:
                value = self._evaluate(
                    plan.expression,
                    focus,
                    locals_,
                    package_name,
                    original_functions=original_functions,
                )
            else:
                value = self._execute_plans(
                    plan.children,
                    focus=focus,
                    locals_=locals_,
                    final_output=False,
                    package_name=package_name,
                    mode=mode,
                    tunnel_parameters=tunnel_parameters,
                    rule_context=rule_context,
                    original_functions=original_functions,
                )
            return (construct_map_entry(key, value),)
        if isinstance(plan, _EvaluatePlan):
            return self._dynamic_evaluate(
                plan,
                focus,
                locals_,
                package_name,
                final_output=final_output,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
        if isinstance(plan, _AssertPlan):
            self._assert_value(
                plan,
                focus,
                locals_,
                package_name,
                final_output=final_output,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
            return ()
        if isinstance(plan, _WherePopulatedPlan):
            values = self._execute_plans(
                plan.children,
                focus=focus,
                locals_=locals_,
                final_output=final_output,
                package_name=package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
            return tuple(item for item in values if not is_deemed_empty(item))
        if isinstance(plan, _ConditionalSequencePlan):
            return self._execute_conditional_sequence(
                plan,
                focus,
                locals_,
                final_output=final_output,
                package_name=package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
        if isinstance(plan, _FallbackPlan):
            return ()
        if isinstance(plan, _ConcatPlan):
            result: list[XdmItem] = []
            for part in plan.parts:
                result.extend(
                    self._execute_plans(
                        part,
                        focus=focus,
                        locals_=locals_,
                        final_output=final_output,
                        package_name=package_name,
                        mode=mode,
                        tunnel_parameters=tunnel_parameters,
                        rule_context=rule_context,
                        original_functions=original_functions,
                    )
                )
            return tuple(result)
        if isinstance(plan, _CallTemplatePlan):
            template, tunnel = self._plan_parameters_for(
                plan.parameters,
                focus,
                locals_,
                package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
            component = self._resolve_template(plan.reference)
            return self._execute_component(
                component,
                template_parameters=template,
                tunnel_parameters={**(tunnel_parameters or {}), **tunnel},
                final_output=final_output,
                focus=focus,
                mode=mode,
                rule_context=rule_context,
            )
        if isinstance(plan, _ApplyTemplatesPlan):
            items = self._evaluate(
                plan.expression,
                focus,
                locals_,
                package_name,
                original_functions=original_functions,
            )
            items = self._sorted_items(
                items,
                plan.sorts,
                focus,
                locals_,
                package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
            template, tunnel = self._plan_parameters_for(
                plan.parameters,
                focus,
                locals_,
                package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
            return self._apply_templates(
                items,
                self._resolve_mode(plan.mode, mode, package_name),
                template,
                {**(tunnel_parameters or {}), **tunnel},
                package_name=package_name,
            )
        if isinstance(plan, _OverridingPlan):
            template, tunnel = self._plan_parameters_for(
                plan.parameters,
                focus,
                locals_,
                package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
            return self._execute_overriding(
                plan,
                rule_context,
                template,
                tunnel,
            )
        if isinstance(plan, _ResultDocumentPlan):
            self._result_document(
                plan,
                focus,
                locals_,
                final_output,
                package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
            )
            return ()
        if isinstance(plan, _UnsupportedPlan):
            raise XsltError(
                f"the instruction xsl:{plan.instruction.local_name} is not supported by the "
                "Task 16 execution slice",
                code=None,
            )
        raise TypeError(  # pragma: no cover - every _Plan arm is handled above
            f"unknown compiled plan {type(plan).__name__}"
        )

    def _execute_conditional_sequence(
        self,
        plan: _ConditionalSequencePlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        *,
        final_output: bool,
        package_name: str | None,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmSequence:
        result: list[XdmItem] = []
        deferred: list[tuple[_OnNonEmptyPlan, Mapping[str, XdmSequence]]] = []
        local_scope = dict(locals_)
        populated = False
        try:
            for item in plan.plans:
                if isinstance(item, _ParamPlan):
                    raise XsltError(
                        "xsl:param is only supported at the start of a template or function",
                        code="XTDE0000",
                    )
                if isinstance(item, _VariablePlan):
                    local_scope[item.name] = self._variable_value(
                        item,
                        focus,
                        local_scope,
                        package_name,
                        mode=mode,
                        tunnel_parameters=tunnel_parameters,
                        rule_context=rule_context,
                        original_functions=original_functions,
                    )
                    continue
                if isinstance(item, _OnNonEmptyPlan):
                    if populated:
                        result.extend(
                            self._conditional_value(
                                item,
                                focus,
                                local_scope,
                                final_output,
                                package_name,
                                mode=mode,
                                tunnel_parameters=tunnel_parameters,
                                rule_context=rule_context,
                                original_functions=original_functions,
                            )
                        )
                    else:
                        deferred.append((item, dict(local_scope)))
                    continue
                if isinstance(item, _OnEmptyPlan):
                    if not populated:
                        result.clear()
                        result.extend(
                            self._conditional_value(
                                item,
                                focus,
                                local_scope,
                                final_output,
                                package_name,
                                mode=mode,
                                tunnel_parameters=tunnel_parameters,
                                rule_context=rule_context,
                                original_functions=original_functions,
                            )
                        )
                    continue
                values = self._execute_plan(
                    item,
                    focus=focus,
                    locals_=local_scope,
                    final_output=final_output,
                    package_name=package_name,
                    mode=mode,
                    tunnel_parameters=tunnel_parameters,
                    rule_context=rule_context,
                    original_functions=original_functions,
                )
                for value in values:
                    if not populated and not is_vacuous(value):
                        for deferred_plan, deferred_scope in deferred:
                            result.extend(
                                self._conditional_value(
                                    deferred_plan,
                                    focus,
                                    deferred_scope,
                                    final_output,
                                    package_name,
                                    mode=mode,
                                    tunnel_parameters=tunnel_parameters,
                                    rule_context=rule_context,
                                    original_functions=original_functions,
                                )
                            )
                        deferred.clear()
                        populated = True
                    result.append(value)
        except _IterationControlSignal as signal:
            if not populated and any(not is_vacuous(value) for value in signal.pending):
                for deferred_plan, deferred_scope in deferred:
                    result.extend(
                        self._conditional_value(
                            deferred_plan,
                            focus,
                            deferred_scope,
                            final_output,
                            package_name,
                            mode=mode,
                            tunnel_parameters=tunnel_parameters,
                            rule_context=rule_context,
                            original_functions=original_functions,
                        )
                    )
                deferred.clear()
            signal.prepend(result)
            raise
        return tuple(result)

    def _conditional_value(
        self,
        plan: _OnEmptyPlan | _OnNonEmptyPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
        package_name: str | None,
        *,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmSequence:
        if plan.expression is not None:
            return self._evaluate(
                plan.expression,
                focus,
                locals_,
                package_name,
                original_functions=original_functions,
            )
        return self._execute_plans(
            plan.children,
            focus=focus,
            locals_=locals_,
            final_output=final_output,
            package_name=package_name,
            mode=mode,
            tunnel_parameters=tunnel_parameters,
            rule_context=rule_context,
            original_functions=original_functions,
        )

    def _assert_value(
        self,
        plan: _AssertPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        final_output: bool,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> None:
        if not plan.enabled:
            return
        failure: BaseException | None = None
        satisfied = False
        try:
            satisfied = _effective_boolean(
                self._evaluate(
                    plan.test,
                    focus,
                    locals_,
                    package_name,
                    original_functions=original_functions,
                )
            )
        except Exception as exc:
            failure = exc
        if satisfied:
            return
        message_failure: BaseException | None = None
        detail = ""
        try:
            message_values: list[XdmItem] = []
            if plan.select is not None:
                message_values.extend(
                    self._evaluate(
                        plan.select,
                        focus,
                        locals_,
                        package_name,
                        original_functions=original_functions,
                    )
                )
            message_values.extend(
                self._execute_plans(
                    plan.children,
                    focus=focus,
                    locals_=locals_,
                    final_output=False,
                    package_name=package_name,
                    mode=mode,
                    tunnel_parameters=tunnel_parameters,
                    rule_context=rule_context,
                    original_functions=original_functions,
                )
            )
            if message_values:
                document = _build_result_tree(message_values)
                detail = document.string_value.strip()
        except Exception as exc:
            message_failure = exc
            detail = f"message construction failed: {exc}"
        code = "XTMM9001"
        if plan.error_code is not None:
            try:
                selected = self._value_source(plan.error_code, focus, locals_, package_name)
                code = _assertion_error_code(selected, plan.instruction.namespaces)
            except Exception as exc:
                if message_failure is None:
                    message_failure = exc
                    if not detail:
                        detail = f"message construction failed: {exc}"
                code = "XTMM9001"
        if detail:
            message = f"assertion failed: {detail} (test: {plan.test.source})"
        else:
            message = f"assertion failed: {plan.test.source} evaluated to false"
        raise XsltDynamicError(
            message,
            code=code,
            location=plan.instruction.location,
            cause=failure or message_failure,
        )

    def _dynamic_evaluate(
        self,
        plan: _EvaluatePlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        final_output: bool,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmSequence:
        if not plan.enabled:
            raise XsltDynamicError(
                "xsl:evaluate is disabled for this transformation",
                code="XTDE3175",
                location=plan.instruction.location,
            )
        target_values = self._evaluate(
            plan.expression,
            focus,
            locals_,
            package_name,
            original_functions=original_functions,
        )
        profile = self.profile_for(plan.expression.static_context.xslt_version)
        target = profile.string_sequence(target_values)

        parameter_values: dict[str, XdmSequence] = {}
        variable_types: dict[str, str] = {}
        for parameter in plan.with_params:
            if parameter.expression is not None:
                value = self._evaluate(
                    parameter.expression,
                    focus,
                    locals_,
                    package_name,
                    original_functions=original_functions,
                )
            else:
                value = self._execute_plans(
                    parameter.children,
                    focus=focus,
                    locals_=locals_,
                    final_output=False,
                    package_name=package_name,
                    mode=mode,
                    tunnel_parameters=tunnel_parameters,
                    rule_context=rule_context,
                    original_functions=original_functions,
                )
            if parameter.sequence_type is not None:
                value = _coerce_sequence(
                    value,
                    parameter.sequence_type,
                    code="XTTE0590",
                    label=f"parameter {parameter.name!r}",
                )
            parameter_values[parameter.name] = value
            variable_types[parameter.name] = (
                "item()*" if parameter.sequence_type is None else str(parameter.sequence_type)
            )
        if plan.with_params_expression is not None:
            supplied = self._evaluate(
                plan.with_params_expression,
                focus,
                locals_,
                package_name,
                original_functions=original_functions,
            )
            for name, value in with_params_variables(supplied).items():
                parameter_values[name] = value
                variable_types[name] = "item()*"

        expression_focus = Focus.absent()
        if plan.context_item is not None:
            context_values = self._evaluate(
                plan.context_item,
                focus,
                locals_,
                package_name,
                original_functions=original_functions,
            )
            if len(context_values) > 1:
                raise XsltDynamicError(
                    "xsl:evaluate/@context-item must return at most one item",
                    code="XTTE3210",
                    location=plan.instruction.location,
                )
            if context_values:
                expression_focus = Focus.singleton(context_values[0])

        namespaces = dict(plan.namespaces)
        default_namespace = plan.default_namespace
        if plan.namespace_context is not None:
            context_nodes = self._evaluate(
                plan.namespace_context,
                focus,
                locals_,
                package_name,
                original_functions=original_functions,
            )
            if len(context_nodes) != 1 or not isinstance(context_nodes[0], XdmNode):
                raise XsltDynamicError(
                    "xsl:evaluate/@namespace-context must return a single node",
                    code="XTTE3170",
                    location=plan.instruction.location,
                )
            namespaces = {
                prefix: uri
                for prefix, uri in context_nodes[0].in_scope_namespaces.items()
                if prefix
            }
            default_namespace = context_nodes[0].in_scope_namespaces.get("")

        base_uri = (
            self._value_source(plan.base_uri, focus, locals_, package_name)
            if plan.base_uri is not None
            else plan.instruction.base_uri
        )
        schema_aware = False
        if plan.schema_aware is not None:
            schema_aware = _boolean_avt(
                self._value_source(plan.schema_aware, focus, locals_, package_name),
                "schema-aware",
            )
        schema = (
            self.stylesheet.schema_pool
            if schema_aware and self.stylesheet.processor.schema_aware
            else None
        )

        available = self.fallback_functions(package_name)
        functions = {name: available[name] for name in plan.functions if name in available}
        identity = StaticContextIdentity(
            xpath_version=plan.xpath_version,
            namespaces=namespaces,
            default_namespace=default_namespace,
            base_uri=base_uri,
            variable_types=variable_types,
            functions=plan.functions,
            schema=schema,
            schema_aware=schema_aware,
            default_collation=plan.expression.static_context.default_collation,
            xslt_version=plan.expression.static_context.xslt_version,
            package_name=package_name,
        )
        variables = _dynamic_variable_names(parameter_values, namespaces)
        environment = ExpressionEnvironment(
            variables=variables,
            functions=functions,
            decimal_formats=self._decimal_formats(package_name),
            current_datetime=self.current_datetime,
            implicit_timezone=self.implicit_timezone,
            base_uri=base_uri,
        )
        result = evaluate_dynamic(
            target,
            identity,
            environment,
            focus=expression_focus,
            cache=self.dynamic_expressions,
        )
        if plan.sequence_type is not None:
            result = _coerce_sequence(
                result,
                plan.sequence_type,
                code="XPTY0004",
                label="xsl:evaluate result",
            )
        return result

    def _literal_element(
        self,
        plan: _LiteralPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
        package_name: str | None,
        *,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmNode:
        factory = _NodeFactory()
        attributes: list[tuple[str, str]] = []
        for reference in plan.attribute_sets:
            component = self._resolve_attribute_set(reference, package_name)
            attributes.extend(
                (attribute.name or "", attribute.string_value)
                for attribute in self._attribute_set_attributes(
                    component,
                    focus,
                    package_name,
                )
                if attribute.kind is NodeKind.ATTRIBUTE
            )
        attributes.extend(
            (name, self._value_source(value, focus, locals_, package_name))
            for name, value in plan.attributes
        )
        element = factory.element(
            plan.instruction.name,
            attributes=tuple(attributes),
            namespaces=dict(plan.namespaces),
            base_uri=plan.instruction.base_uri,
        )
        content = self._execute_plans(
            plan.children,
            focus=focus,
            locals_=locals_,
            final_output=final_output,
            package_name=package_name,
            mode=mode,
            tunnel_parameters=tunnel_parameters,
            rule_context=rule_context,
            original_functions=original_functions,
        )
        _append_complex_content(factory, element, content, allow_attributes=True)
        return element

    def _variable_value(
        self,
        plan: _VariablePlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmSequence:
        if plan.expression is not None:
            value = self._evaluate(
                plan.expression,
                focus,
                locals_,
                package_name,
                original_functions=original_functions,
            )
        elif plan.children:
            sequence = self._execute_plans(
                plan.children,
                focus=focus,
                locals_=locals_,
                final_output=False,
                package_name=package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
            value = sequence if plan.has_as else (_build_result_tree(sequence),)
        elif plan.has_as:
            value = ()
        else:
            value = (XdmAtomic.string(""),)
        if plan.sequence_type is not None:
            value = _coerce_sequence(
                value,
                plan.sequence_type,
                code="XTTE0570",
                label=f"variable {plan.name!r}",
            )
        return value

    def _attribute_value(
        self,
        plan: _AttributePlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
        package_name: str | None,
        *,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        rule_context: _RuleContext | None = None,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmSequence:
        lexical = self._value_source(plan.name, focus, locals_, package_name)
        prefix, separator, local = lexical.partition(":")
        if not lexical:
            raise XsltDynamicError("an attribute name must not be empty", code="XTDE0850")
        if separator:
            uri = plan.namespaces.get(prefix)
            if uri is None:
                raise XsltDynamicError(
                    f"the attribute prefix {prefix!r} is not bound",
                    code="XTDE0850",
                )
            name = f"{{{uri}}}{local}"
        else:
            name = lexical
        if plan.expression is not None:
            value = self._evaluate(
                plan.expression,
                focus,
                locals_,
                package_name,
                original_functions=original_functions,
            )
        else:
            value = self._execute_plans(
                plan.children,
                focus=focus,
                locals_=locals_,
                final_output=final_output,
                package_name=package_name,
                mode=mode,
                tunnel_parameters=tunnel_parameters,
                rule_context=rule_context,
                original_functions=original_functions,
            )
        return construct_attribute(
            name,
            _simple_content_string(value, " "),
            prefix=prefix if separator else None,
        )

    def _execute_overriding(
        self,
        plan: _OverridingPlan,
        rule_context: _RuleContext | None,
        template_parameters: Mapping[str, XdmSequence],
        tunnel_parameters: Mapping[str, XdmSequence],
    ) -> XdmSequence:
        if rule_context is None:
            raise XsltDynamicError(
                "xsl:next-match and xsl:apply-imports require a current template rule",
                code="XTDE0560",
            )
        item = rule_context.focus.item
        if item is None:  # pragma: no cover - a current rule always has a focus item
            raise XsltDynamicError("the current template rule has no context item")
        match = self._overriding_rule(plan, rule_context, item)
        if match is None:
            return self._builtin_value(
                item,
                rule_context.mode,
                template_parameters,
                {**rule_context.tunnel_parameters, **tunnel_parameters},
                rule_context.package_name,
            )
        frame = self._rule_frame(
            match,
            rule_context.focus,
            template_parameters,
            {**rule_context.tunnel_parameters, **tunnel_parameters},
        )
        return cast(XdmSequence, self.execute_frame(frame))

    def _resolve_attribute_set(
        self,
        reference: ComponentReference,
        package_name: str | None,
    ) -> Component:
        package = self.stylesheet._packages[
            reference.containing or package_name or self.package.name
        ]
        if reference.original:
            component = package.original_target(reference.name or "", kind="attribute-set")
        else:
            component = package.attribute_set(reference.name or "")
        if component is None:
            raise XsltDynamicError(
                f"the attribute set {reference.name!r} is not visible at run time",
                code="XTDE0710",
            )
        return component

    def _attribute_set_attributes(
        self,
        component: Component,
        focus: Focus,
        package_name: str | None,
    ) -> tuple[XdmNode, ...]:
        key = (component.owner, component.name)
        if key in self.evaluating_attribute_sets:
            raise XsltStaticError(
                "an attribute set cannot depend on itself",
                code="XTSE0720",
            )
        self.evaluating_attribute_sets.append(key)
        try:
            plan = self._component_plan(component)
            attributes: list[XdmNode] = []
            for references, body in plan.attribute_set_parts:
                for reference in references:
                    target = self._resolve_attribute_set(reference, component.owner or package_name)
                    attributes.extend(self._attribute_set_attributes(target, focus, target.owner))
                for instruction in body:
                    attributes.extend(
                        cast(
                            Iterable[XdmNode],
                            self._execute_plan(
                                instruction,
                                focus=focus,
                                locals_={},
                                final_output=False,
                                package_name=component.owner,
                            ),
                        )
                    )
            return tuple(attributes)
        finally:
            self.evaluating_attribute_sets.pop()

    def _result_document(
        self,
        plan: _ResultDocumentPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
        package_name: str | None,
        *,
        mode: str | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
    ) -> None:
        if not final_output:
            raise XsltError(
                "xsl:result-document is not permitted in temporary output state",
                code="XTDE1480",
            )
        href = self._value_source(plan.href, focus, locals_, package_name)
        method = (
            self._value_source(plan.method, focus, locals_, package_name)
            if plan.method is not None
            else None
        )
        format_name = (
            self._value_source(plan.format, focus, locals_, package_name)
            if plan.format is not None
            else None
        )
        base_attributes: dict[str, str] = {}
        if format_name is not None:
            declaration = self.package.output(format_name)
            if declaration is None:
                raise XsltError(
                    f"the named output format {format_name!r} is not declared",
                    code="XTDE1460",
                )
            base_attributes.update(declaration.attributes)
        else:
            declaration = self.package.output(None)
            if declaration is not None:
                base_attributes.update(declaration.attributes)
        evaluated = {
            name: self._value_source(value, focus, locals_, package_name)
            for name, value in plan.attributes
        }
        effective_method = method or base_attributes.get("method", "xml")
        build_tree = (
            self._value_source(plan.build_tree, focus, locals_, package_name)
            if plan.build_tree is not None
            else base_attributes.get("build-tree")
        )
        uri = self._secondary_uri(href)
        if uri in self.final_uris:
            raise XsltError(
                f"the transformation produced two final result trees with URI {uri!r}",
                code="XTDE1490",
            )
        self.final_uris.add(uri)
        sequence = self._execute_plans(
            plan.children,
            focus=focus,
            locals_=locals_,
            final_output=True,
            package_name=package_name,
            mode=mode,
            tunnel_parameters=tunnel_parameters,
        )
        tree = None
        if self._build_tree(build_tree, effective_method):
            tree = _build_result_tree(sequence, output_uri=uri)
        serialization = dict(base_attributes)
        serialization.update(evaluated)
        serialization["method"] = effective_method
        if build_tree is not None:
            serialization["build-tree"] = build_tree
        self.secondary[uri] = ResultEntry(
            sequence,
            output_uri=uri,
            tree=tree,
            serialization=serialization,
        )

    def _secondary_uri(self, href: str) -> str:
        base = self.options.output_uri
        target = base or "" if not href else urljoin(base, href) if base is not None else href
        if not target or not urlsplit(target).scheme:
            raise XsltError(
                f"cannot determine an absolute URI for xsl:result-document href {href!r}",
                code=None,
            )
        return target

    def _build_tree(self, value: str | None, method: str) -> bool:
        if value is not None:
            normalized = value.strip()
            if normalized in ("yes", "true", "1"):
                return True
            if normalized in ("no", "false", "0"):
                return False
            raise XsltError(
                f"invalid build-tree value {value!r}",
                code="XTDE0030",
            )
        return _method_local(method) not in ("json", "adaptive")

    def _text_node(self, value: str) -> XdmNode:
        return _NodeFactory().text(value)

    # -- modes and built-in templates -------------------------------------

    def _apply_templates(
        self,
        items: XdmSequence,
        mode: str | None,
        template_parameters: Mapping[str, XdmSequence],
        tunnel_parameters: Mapping[str, XdmSequence],
        *,
        package_name: str | None = None,
    ) -> XdmSequence:
        frame = self._apply_frame(
            items,
            mode,
            template_parameters,
            tunnel_parameters,
            package_name=package_name,
        )
        return cast(XdmSequence, self.execute_frame(frame))

    # -- expressions and values -------------------------------------------

    def _evaluate(
        self,
        expression: CompiledExpression,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        original_functions: Mapping[int, Component] | None = None,
    ) -> XdmSequence:
        engine = XPathEngine(expression.version)
        attempted: set[str] = set()
        while True:
            try:
                return engine.evaluate(
                    expression,
                    self._environment(
                        locals_,
                        package_name,
                        original_functions=original_functions,
                        focus=focus,
                        expression=expression,
                    ),
                    focus=focus,
                )
            except Exception as exc:
                if not str(getattr(exc, "code", "")).endswith("XPST0008"):
                    raise
                name = _unknown_variable_name(exc)
                component = self._global_component(package_name, name)
                if component is None or name in attempted:
                    raise
                attempted.add(name)
                self._global_value(component)

    def _expression_references_global(
        self,
        expression: CompiledExpression,
        package_name: str | None,
    ) -> bool:
        for component in self.stylesheet._package_variables.get(package_name, ()):
            if component.name is not None and component.name in expression.variable_names:
                return True
        return False

    def _global_component(
        self,
        package_name: str | None,
        name: str,
    ) -> Component | None:
        package = self.stylesheet._packages.get(package_name) or self.package
        for component in self.stylesheet._package_variables.get(package_name, ()):
            assert component.name is not None
            if component.name == name or _lexical_name(component.name, package.namespaces) == name:
                return component
        return None

    def _environment(
        self,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
        *,
        referenced: set[str] | None = None,
        original_functions: Mapping[int, Component] | None = None,
        focus: Focus | None = None,
        expression: CompiledExpression | None = None,
        static_context: StaticContext | None = None,
    ) -> ExpressionEnvironment:
        variables: dict[str, Any] = {}
        package = self.stylesheet._packages.get(package_name) or self.package
        for component in self.stylesheet._package_variables.get(package_name, ()):
            assert component.name is not None
            lexical = _lexical_name(component.name, package.namespaces)
            if referenced is None:
                value = self.globals.get((component.owner, component.name))
                if value is None:
                    continue
            else:
                if component.name not in referenced and lexical not in referenced:
                    continue
                if any(
                    name == component.name or _lexical_name(name, package.namespaces) == lexical
                    for name in locals_
                ):
                    continue
                value = self._global_value(component)
            self._bind(variables, component.name, value)
        for key, value in locals_.items():
            self._bind(variables, key, value)
        functions = dict(self.fallback_functions(package_name))
        if original_functions:
            for _arity, component in original_functions.items():
                functions[f"{{{XSLT_NS}}}original"] = self._component_dispatcher(
                    f"{{{XSLT_NS}}}original",
                    [component],
                )
        bridge = self._context_bridge(
            package_name,
            Focus.absent() if focus is None else focus,
            expression,
            static_context,
        )
        overrides: dict[str, Any] = dict(bridge.function_overrides())
        return ExpressionEnvironment(
            variables=variables,
            functions=functions,
            function_overrides=overrides,
            decimal_formats=self._decimal_formats(package_name),
            current_datetime=self.current_datetime,
            implicit_timezone=self.implicit_timezone,
            base_uri=None,
        )

    def _decimal_formats(
        self,
        package_name: str | None,
    ) -> Mapping[str | None, Mapping[str, str]]:
        package = self.stylesheet._packages.get(package_name) or self.package
        formats: dict[str | None, Mapping[str, str]] = {}
        for definition in package.decimal_formats:
            formats[definition.name] = dict(definition.attributes)
        return formats

    def _context_bridge(
        self,
        package_name: str | None,
        focus: Focus,
        expression: CompiledExpression | None,
        static_context: StaticContext | None = None,
    ) -> XsltContextBridge:
        package = self.stylesheet._packages.get(package_name) or self.package
        static = (
            static_context
            if static_context is not None
            else (None if expression is None else expression.static_context)
        )
        return XsltContextBridge(
            self,
            package_name=package_name,
            focus=focus,
            current_item=focus.item if focus.kind is FocusKind.ITEM else None,
            group=(ABSENT if self.current_group is None else tuple(self.current_group)),
            grouping_key=(
                ABSENT if self.current_grouping_key is None else tuple(self.current_grouping_key)
            ),
            namespaces=package.namespaces if static is None else static.namespaces,
            default_namespace=None if static is None else static.default_namespace,
            base_uri=None if static is None else static.base_uri,
            xpath_version=(
                expression.version
                if expression is not None
                else getattr(static, "xpath_version", self.stylesheet.xpath)
            ),
            xslt_version=(
                None
                if static is None
                else getattr(static, "xslt_version", None) or getattr(static, "version", None)
            ),
            key_lookup=self._key_override_for(package_name),
        )

    def _key_override_for(self, package_name: str | None) -> Callable[..., XdmSequence]:
        def override(
            context_item: XdmItem | None,
            arguments: tuple[XdmSequence, ...],
        ) -> XdmSequence:
            return self._key_override(context_item, arguments, package_name)

        return override

    def _key_override(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
        package_name: str | None,
    ) -> XdmSequence:
        name_sequence = arguments[0]
        if not name_sequence or not isinstance(name_sequence[0], XdmAtomic):
            raise XsltDynamicError("a key name must be a string", code="XPTY0004")
        lexical = name_sequence[0].lexical
        package = self.stylesheet._packages.get(package_name) or self.package
        expanded = _expanded_name(lexical, package.namespaces)
        definitions = tuple(
            definition
            for definition in self.stylesheet._keys.get(package_name, ())
            if definition.name == expanded
        )
        if not definitions:
            raise XsltDynamicError(
                f"the key {lexical!r} is not declared in this package",
                code="XTDE1260",
            )
        document = self._key_document(context_item, arguments)
        composite = any(definition.composite for definition in definitions)
        index = self._key_index(definitions, document, package_name)
        nodes: list[XdmNode] = []
        atoms = _key_atomics(arguments[1])
        if composite:
            composite_key = tuple(_key_value(value) for value in atoms)
            nodes.extend(index.get(composite_key, ()))
        else:
            for item in atoms:
                nodes.extend(index.get(_key_value(item), ()))
                nodes.extend(index.get(item.lexical, ()))
        unique: dict[int, XdmNode] = {id(node): node for node in nodes}
        ordered = sorted(unique.values(), key=lambda node: node.document_order)
        return tuple(ordered)

    def _key_document(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...] = (),
    ) -> XdmDocument:
        if len(arguments) > 2:
            top = arguments[2]
            if len(top) != 1 or not isinstance(top[0], XdmNode):
                raise XsltDynamicError(
                    "key() requires a single node as its third argument",
                    code="XPTY0004",
                )
            return top[0].document
        if not isinstance(context_item, XdmNode):
            raise XsltDynamicError(
                "key() with two arguments requires a context node",
                code="XTDE1270",
            )
        document = context_item.document
        if not isinstance(document, XdmDocument):
            raise XsltDynamicError(
                "the key context node must have a document root",
                code="XTDE1270",
            )
        return document

    def _key_index(
        self,
        definitions: tuple[_KeyDefinition, ...],
        document: XdmDocument,
        package_name: str | None,
    ) -> Mapping[Any, tuple[XdmNode, ...]]:
        name = definitions[0].name
        key = (id(document), package_name, name)
        cached = self.key_indexes.get(key)
        if cached is not None and self.key_index_documents.get(id(document)) is document:
            return cached
        index: dict[Any, list[XdmNode]] = {}
        for definition in definitions:
            matcher = PatternMatcher(definition.match)
            environment = self._pattern_environment_for(definition.match, definition.owner)
            for node in _document_nodes(document):
                if not matcher.matches(node, environment):
                    continue
                with self._group_scope(None, None):
                    values = self._evaluate(
                        definition.use,
                        Focus.singleton(node),
                        {},
                        package_name,
                    )
                atomics = _key_atomics(values)
                if definition.composite:
                    composite_key = tuple(_key_value(value) for value in atomics)
                    index.setdefault(composite_key, []).append(node)
                    continue
                for value in atomics:
                    index.setdefault(_key_value(value), []).append(node)
                    index.setdefault(value.lexical, []).append(node)
        frozen = MappingProxyType({value: tuple(nodes) for value, nodes in index.items()})
        self.key_indexes[key] = frozen
        self.key_index_documents[id(document)] = document
        return frozen

    def _invoke_component_callable(
        self,
        component: Any,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        return self._execute_component(
            cast(Component, component),
            arguments=arguments,
            final_output=False,
        )

    def _bind(self, variables: dict[str, Any], name: str, value: XdmSequence) -> None:
        variables[name] = value
        lexical = self._lexical(name)
        if lexical != name:
            variables[lexical] = value

    def _lexical(self, name: str) -> str:
        return _lexical_name(name, self.package.namespaces)

    def _value_source(
        self,
        source: str | CompiledValueTemplate,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
    ) -> str:
        if isinstance(source, str):
            return source
        return self._value_template(source, focus, locals_, package_name)

    def _value_template(
        self,
        template: CompiledValueTemplate,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
    ) -> str:
        parts: list[str] = []
        for part in template.parts:
            if part.kind == "text":
                parts.append(part.text)
                continue
            assert part.expression is not None
            values = self._evaluate(part.expression, focus, locals_, package_name)
            if not template.text_value_template:
                profile = self.profile_for(part.expression.static_context.xslt_version)
                if profile.compatibility_mode:
                    parts.append(profile.string_sequence(values))
                    continue
            parts.append(" ".join(self._string_value(item) for item in values))
        return "".join(parts)

    def _string_value(self, item: XdmItem) -> str:
        return _item_string(item)


def _unknown_variable_name(error: BaseException) -> str:
    match = re.search(r"unknown variable ['\"]?\$?([^'\"\s]+)", str(error))
    assert match is not None
    return match.group(1)


def _pattern_variable_names(pattern: CompiledPattern) -> set[str]:
    names: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, CompiledExpression):
            names.update(value.variable_names)
            return
        if isinstance(value, CompiledPattern):
            for alternative in value.alternatives:
                visit(alternative)
            return
        if is_dataclass(value) and not isinstance(value, type):
            for item in fields(value):
                visit(getattr(value, item.name))
            return
        if isinstance(value, (tuple, list)):
            for item in value:
                visit(item)

    visit(pattern)
    return names


def _document_nodes(document: XdmDocument) -> Iterator[XdmNode]:
    """Yield every node of *document* in document order.

    The traversal includes element attributes and namespace nodes, which key
    match patterns can select just like ordinary child nodes.
    """
    yield document
    root = document.root_element
    if root is not None:
        yield from _node_tree(root)


def _node_tree(node: XdmNode) -> Iterator[XdmNode]:
    yield node
    if node.kind is NodeKind.ELEMENT:
        yield from node.attributes
        yield from node.namespace_nodes
    for child in node.children:
        yield from _node_tree(child)


def _key_atomics(sequence: XdmSequence) -> tuple[XdmAtomic, ...]:
    atomics: list[XdmAtomic] = []
    for item in sequence:
        if isinstance(item, XdmAtomic):
            atomics.append(item)
            continue
        if isinstance(item, XdmNode):
            atomics.extend(value for value in item.typed_value if isinstance(value, XdmAtomic))
            continue
        raise XsltDynamicError("a key value cannot atomize a function item", code="FOTY0013")
    return tuple(atomics)


def _array_member_items(item: XdmArray) -> XdmSequence:
    return tuple(member for sequence in item.members for member in sequence)


def _key_value(item: XdmAtomic) -> object:
    value = item.value
    try:
        hash(value)
    except TypeError:  # pragma: no cover - atomic values are hashable
        return item.lexical
    return value


def _method_local(method: str) -> str:
    if method.startswith("Q{"):
        return method.rpartition("}")[2]
    return method.rpartition(":")[2]


def _lexical_name(name: str, namespaces: Mapping[str, str]) -> str:
    if not name.startswith("{"):
        return name
    namespace, _, local = name[1:].partition("}")
    for prefix, uri in namespaces.items():
        if uri == namespace and prefix:
            return f"{prefix}:{local}"
    return name


def _yes_no_runtime(value: str | None) -> bool:
    """Read one boolean that the static front end has already validated."""
    if value is None:
        return False
    return value.strip() not in ("no", "false", "0")
