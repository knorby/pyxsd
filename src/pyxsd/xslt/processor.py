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
from typing import Any, Literal, cast
from urllib.parse import urljoin, urlsplit

from elementpath.exceptions import ElementPathError
from elementpath.helpers import is_ncname

from pyxsd.document import Document
from pyxsd.locations import IssueLocation
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
from pyxsd.xpath.continuation import (
    ContinuationMachine,
    HostFunction,
    Suspension,
    XPathEvaluationError,
)
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, FocusKind, FunctionArity
from pyxsd.xslt.compiler.context import (
    ProfileOptions,
    StaticContext,
    StaticFunction,
    StaticVariable,
)
from pyxsd.xslt.compiler.dispatch import (
    InstructionCompiler,
    compile_instruction,
    register_instruction_compilers,
    supports_instruction,
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
from pyxsd.xslt.compiler.instructions import (
    ComponentReference,
    Instruction,
    TextNode,
    instruction_name,
)
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
from pyxsd.xslt.compiler.plans import (
    _AnalyzeStringPlan,
    _ApplyTemplatesPlan,
    _AssertPlan,
    _AttributePlan,
    _BodyPlan,
    _BreakPlan,
    _CallTemplatePlan,
    _CatchPlan,
    _ChoosePlan,
    _ComponentPlan,
    _ConcatPlan,
    _ConditionalSequencePlan,
    _CopyOfPlan,
    _CopyPlan,
    _DocumentPlan,
    _ElementPlan,
    _EvaluatePlan,
    _FallbackPlan,
    _ForEachGroupPlan,
    _ForEachPlan,
    _ForkPlan,
    _IteratePlan,
    _LiteralPlan,
    _MapEntryPlan,
    _MapPlan,
    _MergePlan,
    _MergeSourcePlan,
    _ModeLayerPlan,
    _NextIterationPlan,
    _NumberPlan,
    _OnEmptyPlan,
    _OnNonEmptyPlan,
    _OverridingPlan,
    _ParamPlan,
    _PerformSortPlan,
    _Plan,
    _RestoreVariablesPlan,
    _ResultDocumentPlan,
    _SequencePlan,
    _SourceDocumentPlan,
    _TextPlan,
    _TryPlan,
    _TvtPlan,
    _UnsupportedPlan,
    _ValueOfPlan,
    _VariablePlan,
    _WherePopulatedPlan,
    _WithParamPlan,
)
from pyxsd.xslt.compiler.static import (
    EffectiveAttribute,
    EffectiveElement,
    EffectiveModule,
    SpaceRule,
    StaticPreprocessor,
)
from pyxsd.xslt.errors import (
    ERROR_NAMESPACE,
    XsltDynamicError,
    XsltError,
    XsltStaticError,
    error_code_from_exception,
    is_static_error_code,
)
from pyxsd.xslt.profiles import RecoveryPolicy
from pyxsd.xslt.results import (
    ResultEntry,
    ResultShapeError,
    TransformationResult,
    _build_result_tree,
    _NodeFactory,
)
from pyxsd.xslt.runtime.accumulators import (
    AccumulatorDefinition,
    AccumulatorKey,
    AccumulatorRule,
    RulePhase,
    create_accumulator_manager,
    expand_accumulator_name,
    find_cycle,
    iter_accumulator_call_spans,
    parse_accumulator_names,
    same_context_accumulator_call,
)
from pyxsd.xslt.runtime.builder import validate_delivered
from pyxsd.xslt.runtime.context import EvaluationContext
from pyxsd.xslt.runtime.evaluate import (
    DynamicExpressionCache,
    execute_dynamic_evaluate,
)
from pyxsd.xslt.runtime.frames import (
    _INHERIT,
    NO_GROUP,
    NO_INPUT,
    Call,
    Continue,
    Frame,
    FrameResult,
    Return,
    _IterationControlSignal,
    _TemplateContext,
)
from pyxsd.xslt.runtime.functions import ABSENT, XsltContextBridge
from pyxsd.xslt.runtime.instructions import (
    _item_string,
    attribute_value,
    choose_branch,
    copy_of_value,
    copy_value,
    document_value,
    element_value,
    execute_analyze_string,
    execute_assert,
    execute_choose,
    execute_concat,
    execute_conditional_sequence,
    execute_fork,
    execute_map,
    execute_map_entry,
    execute_number,
    execute_source_document,
    execute_try,
    execute_value_of,
    execute_where_populated,
    literal_element,
    try_step,
    variable_value,
)
from pyxsd.xslt.runtime.iteration import (
    GROUP_ADJACENT,
    GROUP_BY,
    GROUP_ENDING_WITH,
    GROUP_STARTING_WITH,
    Completion,
    IterationState,
    SortKey,
    execute_iteration,
    execute_merge,
    iteration_control,
    perform_sort,
    start_iteration_plan,
    step_iterate,
)
from pyxsd.xslt.runtime.numbering import (
    DecimalFormat,
    decimal_format_attributes,
    effective_decimal_formats,
    validate_decimal_formats,
)
from pyxsd.xslt.runtime.resources import DocumentResources
from pyxsd.xslt.runtime.templates import (
    PatternIndex,
    RuleDispatcher,
    TemplateRule,
    apply_templates,
    apply_templates_step,
    call_template_step,
    execute_apply_templates,
    execute_call_template,
    execute_overriding,
    overriding_step,
    step_apply,
)
from pyxsd.xslt.runtime.transformation import ComponentCall, Transformation
from pyxsd.xslt.runtime.whitespace import _shared_name_test, apply_source_space_rules

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
_DELIVERIES = frozenset({"stylesheet", "raw", "document"})
_VALIDATION_MODES = frozenset({"strict", "lax", "preserve", "strip"})
UNTYPED_TYPE = "{http://www.w3.org/2001/XMLSchema}untyped"
ANY_TYPE = "{http://www.w3.org/2001/XMLSchema}anyType"

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
# Runtime-only plan state


@dataclass(frozen=True, slots=True)
class _KeyDefinition:
    name: str
    match: CompiledPattern
    use: CompiledExpression
    owner: str | None
    composite: bool = False


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
    xsd_version: str = "1.1"
    validation: str = "strip"
    validation_type: str | None = None
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
        if self.xsd_version not in ("1.0", "1.1"):
            raise ValueError(f"xsd_version must be '1.0' or '1.1', not {self.xsd_version!r}")
        if self.validation not in _VALIDATION_MODES:
            raise ValueError(
                f"validation must be one of {sorted(_VALIDATION_MODES)}, got {self.validation!r}"
            )
        if self.validation_type is not None and not isinstance(self.validation_type, str):
            raise TypeError("validation_type must be a string or None")
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
        package = compile_package_set(
            effective,
            resolver=resolver,
            xsd_version=self.xsd_version,
            resource_policy=self.resource_policy,
        )
        if not self.schema_aware:
            if self.validation == "strict":
                raise XsltStaticError(
                    "a non-schema-aware processor cannot use strict validation",
                    code="XTSE1660",
                    uri=effective.identity,
                    location=effective.root.source_location(),
                )
            _reject_non_schema_aware_features(package)
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
    _package_schema_pools: Mapping[str | None, SchemaPool | None]
    _keys: Mapping[str | None, tuple[_KeyDefinition, ...]]
    _package_modes: Mapping[str | None, Mapping[ModeKey, tuple[_ModeLayerPlan, ...]]]
    _accumulators: Mapping[AccumulatorKey, AccumulatorDefinition] = field(default_factory=dict)
    _mode_accumulators: Mapping[tuple[str | None, ModeKey], frozenset[AccumulatorKey]] = field(
        default_factory=dict
    )
    _space_rules: Mapping[str | None, tuple[SpaceRule, ...]] = field(default_factory=dict)

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
        object.__setattr__(
            self,
            "_package_schema_pools",
            MappingProxyType(dict(self._package_schema_pools)),
        )
        object.__setattr__(self, "_keys", MappingProxyType(dict(self._keys)))
        object.__setattr__(
            self,
            "_package_modes",
            MappingProxyType(
                {name: MappingProxyType(dict(modes)) for name, modes in self._package_modes.items()}
            ),
        )
        object.__setattr__(self, "_accumulators", MappingProxyType(dict(self._accumulators)))
        object.__setattr__(
            self,
            "_mode_accumulators",
            MappingProxyType(dict(self._mode_accumulators)),
        )
        object.__setattr__(self, "_space_rules", MappingProxyType(dict(self._space_rules)))

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
        validation: str | None = None,
        validation_type: str | None = None,
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
            validation=validation,
            validation_type=validation_type,
        )
        return _Invocation(self, options, limits=limits).run()


def _reject_non_schema_aware_features(package: CompiledPackage) -> None:
    """Reject stylesheet features a non-schema-aware processor cannot honor."""
    from pyxsd.xslt.compiler.packages import _package_closure

    for current in _package_closure(package):
        for module in current.modules:
            for node in (module.document.root_element, *module.document.root_element.descendants):
                if node.kind is not NodeKind.ELEMENT:
                    continue
                if node.name == f"{{{XSLT_NS}}}import-schema":
                    raise XsltStaticError(
                        "a non-schema-aware processor cannot process xsl:import-schema",
                        code="XTSE1650",
                        uri=module.identity,
                        location=node.source_location(),
                    )
                # Only XSLT-defined attributes are schema-related.  An
                # unprefixed ``type`` or ``validation`` on a literal result
                # element is an ordinary literal attribute, and a foreign
                # namespaced attribute is always the user's own.
                is_xslt_element = node.name.startswith(f"{{{XSLT_NS}}}")
                for attribute in node.attributes:
                    name = attribute.name or ""
                    if name.startswith("{"):
                        if not name.startswith(f"{{{XSLT_NS}}}"):
                            continue
                        local = name.rpartition("}")[2]
                    else:
                        if not is_xslt_element:
                            continue
                        local = name
                    if local == "type":
                        raise XsltStaticError(
                            "a non-schema-aware processor cannot process a type attribute",
                            code="XTSE1660",
                            uri=module.identity,
                            location=node.source_location(),
                        )
                    if local not in ("validation", "default-validation"):
                        continue
                    value = attribute.string_value.strip()
                    if value not in ("strip", "preserve", "lax"):
                        raise XsltStaticError(
                            "a non-schema-aware processor cannot use this validation value",
                            code="XTSE1660",
                            uri=module.identity,
                            location=node.source_location(),
                        )
                typed_attribute = node.attribute("typed")
                if (
                    node.name == f"{{{XSLT_NS}}}mode"
                    and typed_attribute is not None
                    and typed_attribute.string_value.strip() in ("yes", "strict")
                ):
                    raise XsltStaticError(
                        "a non-schema-aware processor cannot use a typed mode",
                        code="XTSE1660",
                        uri=module.identity,
                        location=node.source_location(),
                    )


def _merge_schema_pools(
    primary: SchemaPool | None,
    imported: SchemaPool | None,
) -> SchemaPool | None:
    """Compose an explicit caller schema pool with imported schemas."""
    if primary is None:
        return imported
    if imported is None:
        return primary
    if primary.xsd_version != imported.xsd_version:
        raise ValueError("the explicit schema pool and imported schemas use different XSD versions")
    try:
        return SchemaPool(
            (*primary.raw_schemas, *imported.raw_schemas),
            xsd_version=primary.xsd_version,
            merge_namespaces=True,
        )
    except ValueError as exc:
        raise XsltStaticError(
            str(exc),
            code="XTSE0220",
            cause=exc,
        ) from exc


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
    space_rules = {
        package_name: _package_space_rules(current_package)
        for package_name, current_package in packages.items()
    }
    for current_package in packages.values():
        validate_decimal_formats(current_package.decimal_formats)
    profile = processor._profile()
    collation = processor.default_collation
    package_schemas: dict[str | None, SchemaPool | None] = {
        package_name: _merge_schema_pools(schema, current_package.schema_pool)
        for package_name, current_package in packages.items()
    }
    components: dict[tuple[str | None, str, str | None, int | None], _ComponentPlan] = {}
    keys: dict[str | None, list[_KeyDefinition]] = {}
    accumulators: dict[AccumulatorKey, AccumulatorDefinition] = {}
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
                package_schemas.get(component.owner, schema),
                profile,
                collation,
            )
            attributes = component.declaration
            context = _original_context(context, attributes)
            if component.kind == "accumulator":
                accumulators[(component.owner, component.name or "")] = _compile_accumulator(
                    component,
                    context,
                    module,
                )
                continue
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

    _validate_accumulator_graph(accumulators, packages)
    package_modes: dict[str | None, dict[ModeKey, tuple[_ModeLayerPlan, ...]]] = {
        package_name: _compile_package_modes(
            current_package,
            packages,
            modules,
            principal,
            package_schemas,
            profile,
            collation,
        )
        for package_name, current_package in packages.items()
    }
    mode_accumulators: dict[tuple[str | None, ModeKey], frozenset[AccumulatorKey]] = {}
    for package_name, current_package in packages.items():
        for mode_name, mode in current_package.modes.items():
            value = mode.properties.get("use-accumulators")
            owner_package = packages.get(mode.owner, current_package)
            mode_accumulators[(package_name, mode_name)] = (
                frozenset()
                if value is None
                else _resolve_mode_accumulators(value, owner_package, mode)
            )
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
            for variable_name, static_value in module.static_variables.items():
                values.setdefault(variable_name, static_value)
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
        schema_pool=package_schemas.get(package.name, schema),
        profile=profile,
        static_parameters=static_parameters,
        _components=components,
        _modes=modes,
        _globals=tuple(globals_),
        _package_variables=package_variables,
        _static_variables=static_variables,
        _packages=packages,
        _package_schema_pools=package_schemas,
        _keys={name: tuple(definitions) for name, definitions in keys.items()},
        _package_modes=package_modes,
        _accumulators=accumulators,
        _mode_accumulators=mode_accumulators,
        _space_rules=space_rules,
    )


_XSLT_STANDARD_ATTRIBUTES = frozenset(
    {
        "default-collation",
        "default-mode",
        "default-validation",
        "exclude-result-prefixes",
        "expand-text",
        "extension-element-prefixes",
        "use-when",
        "version",
        "xpath-default-namespace",
    }
)

_ACCUMULATOR_ATTRIBUTES = frozenset({"as", "initial-value", "name", "streamable"})
_ACCUMULATOR_RULE_ATTRIBUTES = frozenset({"match", "phase", "priority", "select"})


def _validate_accumulator_attributes(
    instruction: Instruction,
    allowed: frozenset[str],
    module: EffectiveModule,
) -> None:
    """Reject an unprefixed attribute not defined for an accumulator element."""
    for attribute in instruction.attributes:
        if attribute.namespace_uri is not None:
            continue
        local = attribute.local_name
        if local in allowed or local in _XSLT_STANDARD_ATTRIBUTES:
            continue
        raise XsltStaticError(
            f"the attribute {local!r} is not permitted on xsl:{instruction.local_name}",
            code="XTSE0090",
            uri=module.identity,
            location=instruction.location,
        )


def _compile_accumulator(
    component: Component,
    context: StaticContext,
    module: EffectiveModule,
) -> AccumulatorDefinition:
    """Compile one package-local ``xsl:accumulator`` declaration."""
    declaration = component.declaration
    _validate_accumulator_attributes(declaration, _ACCUMULATOR_ATTRIBUTES, module)
    name = component.name
    if name is None:  # pragma: no cover - package composition requires a name
        raise XsltStaticError(
            "an xsl:accumulator requires a name",
            code="XTSE0010",
            uri=module.identity,
            location=declaration.location,
        )
    initial_source = declaration.attribute("initial-value")
    if initial_source is None:
        raise XsltStaticError(
            "an xsl:accumulator requires an initial-value attribute",
            code="XTSE0010",
            uri=module.identity,
            location=declaration.location,
        )
    initial_value = compile_xpath(initial_source, context, location=declaration.location)
    as_source = declaration.attribute("as")
    sequence_type = compile_sequence_type(
        as_source or "item()*",
        context,
        location=declaration.location,
    )
    streamable = _attribute_yes_no(declaration, "streamable", context)
    rules: list[AccumulatorRule] = []
    for order, child in enumerate(component.body.children):
        if not (
            isinstance(child, Instruction)
            and child.is_xslt
            and child.local_name == "accumulator-rule"
        ):
            raise XsltStaticError(
                "xsl:accumulator permits only xsl:accumulator-rule children",
                code="XTSE0010",
                uri=module.identity,
                location=getattr(child, "location", declaration.location),
            )
        rules.append(
            _compile_accumulator_rule(
                child,
                context,
                module,
                sequence_type,
                as_source,
                order,
            )
        )
    if not rules:
        raise XsltStaticError(
            "an xsl:accumulator requires at least one xsl:accumulator-rule",
            code="XTSE0010",
            uri=module.identity,
            location=declaration.location,
        )
    return AccumulatorDefinition(
        owner=component.owner,
        name=name,
        initial_value=initial_value,
        sequence_type=sequence_type,
        streamable=streamable,
        rules=tuple(rules),
        module=module.identity,
        location=declaration.location,
    )


def _compile_accumulator_rule(
    rule: Instruction,
    context: StaticContext,
    module: EffectiveModule,
    sequence_type: CompiledSequenceType,
    as_source: str | None,
    order: int,
) -> AccumulatorRule:
    _validate_accumulator_attributes(rule, _ACCUMULATOR_RULE_ATTRIBUTES, module)
    base_context = _instruction_context(context, rule)
    match = rule.attribute("match")
    if match is None:
        raise XsltStaticError(
            "an xsl:accumulator-rule requires a match attribute",
            code="XTSE0010",
            uri=module.identity,
            location=rule.location,
        )
    pattern = compile_pattern(match, base_context, location=rule.location)
    rule_context = base_context.with_variable(
        StaticVariable(
            name="value",
            sequence_type=sequence_type,
            declared_type=as_source,
            module=module.identity,
        )
    )
    phase_source = rule.attribute("phase")
    phase = "start" if phase_source is None else phase_source.strip()
    if phase in ("end", "finish"):
        phase = "end"
    elif phase != "start":
        raise XsltStaticError(
            f"invalid xsl:accumulator-rule phase {phase_source!r}",
            code="XTSE0020",
            uri=module.identity,
            location=rule.location,
        )
    priority_source = rule.attribute("priority")
    priority: Decimal | None = None
    if priority_source is not None:
        try:
            priority = Decimal(priority_source.strip())
        except (InvalidOperation, ValueError) as exc:
            raise XsltStaticError(
                f"invalid xsl:accumulator-rule priority {priority_source!r}",
                code="XTSE0020",
                uri=module.identity,
                location=rule.location,
                cause=exc,
            ) from exc
        if not priority.is_finite():
            raise XsltStaticError(
                "an xsl:accumulator-rule priority must be a finite decimal",
                code="XTSE0020",
                uri=module.identity,
                location=rule.location,
            )
    select_source = rule.attribute("select")
    content = tuple(
        child
        for child in rule.children
        if not (isinstance(child, TextNode) and not child.value.strip())
    )
    if select_source is not None:
        if content:
            raise XsltStaticError(
                "an xsl:accumulator-rule with a select attribute must have empty content",
                code="XTSE0010",
                uri=module.identity,
                location=rule.location,
            )
        select = compile_xpath(select_source, rule_context, location=rule.location)
        body: tuple[_Plan, ...] = ()
    else:
        select = None
        body = _compile_sequence(content, rule_context, module)
    return AccumulatorRule(
        phase=cast(RulePhase, phase),
        pattern=pattern,
        select=select,
        body=body,
        priority=priority,
        order=order,
        instruction=rule,
        location=rule.location,
    )


def _validate_accumulator_graph(
    accumulators: Mapping[AccumulatorKey, AccumulatorDefinition],
    packages: Mapping[str | None, CompiledPackage],
) -> None:
    """Reject cyclic or package-illegal accumulator dependencies statically."""
    graph: dict[AccumulatorKey, set[AccumulatorKey]] = {key: set() for key in accumulators}
    for definition in accumulators.values():
        package = packages.get(definition.owner)
        if package is None:  # pragma: no cover - every definition has a package
            continue
        graph[definition.key].update(_accumulator_references(definition, package))
    cycle = find_cycle(graph)
    if cycle is None:
        return
    first = accumulators[cycle[0]]
    names = " -> ".join(name for _owner, name in cycle)
    raise XsltStaticError(
        f"the accumulators {names} form a cyclic dependency",
        code="XTDE3400",
        uri=first.module,
        location=first.location,
    )


def _accumulator_references(
    definition: AccumulatorDefinition,
    package: CompiledPackage,
) -> set[AccumulatorKey]:
    references: set[AccumulatorKey] = set()
    _collect_accumulator_references(
        references,
        definition.initial_value.source,
        definition.initial_value.static_context.namespaces,
        package,
        definition,
        self_reference_guard=False,
    )
    for rule in definition.rules:
        _collect_accumulator_references(
            references,
            rule.pattern.source,
            rule.pattern.context.namespaces,
            package,
            definition,
            self_reference_guard=True,
        )
        if rule.select is not None:
            _collect_accumulator_references(
                references,
                rule.select.source,
                rule.select.static_context.namespaces,
                package,
                definition,
                self_reference_guard=True,
            )
        _plan_accumulator_references(
            references,
            rule.body,
            package,
            definition,
            self_reference_guard=True,
        )
    return references


def _collect_accumulator_references(
    references: set[AccumulatorKey],
    source: str,
    namespaces: Mapping[str, str],
    package: CompiledPackage,
    definition: AccumulatorDefinition,
    *,
    self_reference_guard: bool,
) -> None:
    for _phase, raw_name, start in iter_accumulator_call_spans(source):
        if raw_name is None:
            continue
        key = _resolve_accumulator_reference(raw_name, namespaces, package, definition)
        if (
            key == definition.key
            and self_reference_guard
            and not same_context_accumulator_call(source, start)
        ):
            # A same-accumulator call after a path or simple-map operator can
            # address a preceding node.  It is a legitimate recurrence, not a
            # cycle; the runtime partial-value cache resolves it.
            continue
        references.add(key)


def _resolve_accumulator_reference(
    raw_name: str,
    namespaces: Mapping[str, str],
    package: CompiledPackage,
    definition: AccumulatorDefinition,
) -> AccumulatorKey:
    try:
        expanded = expand_accumulator_name(raw_name, namespaces)
    except ValueError as exc:
        raise XsltStaticError(
            f"invalid accumulator reference {raw_name!r}",
            code="XTDE3340",
            uri=definition.module,
            location=definition.location,
            cause=exc,
        ) from exc
    component = package.accumulator(expanded)
    if component is None or component.kind != "accumulator" or component.name is None:
        raise XsltStaticError(
            f"the accumulator {raw_name!r} is not declared in this package",
            code="XTDE3340",
            uri=definition.module,
            location=definition.location,
        )
    return (component.owner, component.name)


def _plan_accumulator_references(
    references: set[AccumulatorKey],
    value: Any,
    package: CompiledPackage,
    definition: AccumulatorDefinition,
    *,
    self_reference_guard: bool,
) -> None:
    """Scan compiled plan expressions, never raw literal stylesheet text."""
    if isinstance(value, CompiledExpression):
        _collect_accumulator_references(
            references,
            value.source,
            value.static_context.namespaces,
            package,
            definition,
            self_reference_guard=self_reference_guard,
        )
        return
    if isinstance(value, CompiledValueTemplate):
        for expression in value.expressions:
            _collect_accumulator_references(
                references,
                expression.source,
                expression.static_context.namespaces,
                package,
                definition,
                self_reference_guard=self_reference_guard,
            )
        return
    if isinstance(value, (tuple, list)):
        for item in value:
            _plan_accumulator_references(
                references,
                item,
                package,
                definition,
                self_reference_guard=self_reference_guard,
            )
        return
    if is_dataclass(value) and not isinstance(
        value,
        (
            Instruction,
            StaticContext,
            CompiledPattern,
            CompiledSequenceType,
            ItemType,
            IssueLocation,
        ),
    ):
        for field_info in fields(value):
            _plan_accumulator_references(
                references,
                getattr(value, field_info.name),
                package,
                definition,
                self_reference_guard=self_reference_guard,
            )


def _resolve_mode_accumulators(
    value: str,
    package: CompiledPackage,
    mode: Mode,
) -> frozenset[AccumulatorKey]:
    """Resolve one ``xsl:mode/@use-accumulators`` list in its package."""
    namespaces = mode.property_namespaces.get("use-accumulators", mode.namespaces)
    return _resolve_use_accumulators(
        value,
        package,
        namespaces,
        uri=mode.module,
        location=mode.location,
    )


def _declared_streamable(component: Component) -> bool:
    """Whether a component's normalized ``streamable`` flag is true."""
    signature = component.signature
    if signature is None:
        return False
    raw = signature.flags.get("streamable", "no")
    return raw.strip() in ("yes", "true", "1")


def _compile_use_accumulators(
    value: str | None,
    context: StaticContext,
    instruction: Instruction,
) -> frozenset[AccumulatorKey]:
    """Resolve one instruction's ``use-accumulators`` attribute at compile time."""
    if value is None:
        return frozenset()
    package = context.package
    if package is None:
        raise XsltStaticError(
            "use-accumulators requires a containing package",
            code="XTSE3300",
            uri=instruction.module,
            location=instruction.location,
        )
    return _resolve_use_accumulators(
        value,
        package,
        instruction.namespaces,
        uri=instruction.module,
        location=instruction.location,
    )


def _resolve_use_accumulators(
    value: str,
    package: CompiledPackage,
    namespaces: Mapping[str, str],
    *,
    uri: str | None,
    location: IssueLocation | None,
) -> frozenset[AccumulatorKey]:
    """Resolve and validate one ``use-accumulators`` token list."""
    try:
        tokens = parse_accumulator_names(value)
    except ValueError as exc:
        raise XsltStaticError(
            "invalid use-accumulators value",
            code="XTSE3300",
            uri=uri,
            location=location,
            cause=exc,
        ) from exc
    if tokens == ("#all",):
        return frozenset(
            (component.owner, component.name)
            for component in package.components.values()
            if component.kind == "accumulator"
            and component.owner == package.name
            and component.name is not None
            and not component.hidden
        )
    keys: set[AccumulatorKey] = set()
    for token in tokens:
        try:
            expanded = expand_accumulator_name(token, namespaces)
        except ValueError as exc:
            raise XsltStaticError(
                f"invalid accumulator name {token!r} in use-accumulators",
                code="XTSE3300",
                uri=uri,
                location=location,
                cause=exc,
            ) from exc
        component = package.accumulator(expanded)
        if component is None or component.kind != "accumulator" or component.name is None:
            raise XsltStaticError(
                f"the accumulator {token!r} is not declared in this package",
                code="XTSE3300",
                uri=uri,
                location=location,
            )
        if not _declared_streamable(component):
            raise XsltStaticError(
                f"the accumulator {token!r} is not declared streamable",
                code="XTSE3300",
                uri=uri,
                location=location,
            )
        key = (component.owner, component.name)
        if key in keys:
            raise XsltStaticError(
                f"the accumulator {token!r} appears more than once in use-accumulators",
                code="XTSE3300",
                uri=uri,
                location=location,
            )
        keys.add(key)
    return frozenset(keys)


def _compile_package_modes(
    package: CompiledPackage,
    packages: Mapping[str | None, CompiledPackage],
    modules: Mapping[str, EffectiveModule],
    principal: EffectiveModule,
    package_schemas: Mapping[str | None, SchemaPool | None],
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
                    package_schemas.get(rule.owner),
                    profile,
                    collation,
                )
                pattern = compile_pattern(
                    rule.pattern,
                    context,
                    typed=mode.typed,
                    location=rule.location,
                )
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


def _package_space_rules(package: CompiledPackage) -> tuple[SpaceRule, ...]:
    """Return one package's space declarations in declaration order.

    The traversal follows each module's declarations and then its includes and
    imports, exactly as the effective-module graph records them.  Both
    declaration kinds are kept in one sequence so equal-precedence ties can use
    declaration order; the runtime selects the best match by import precedence,
    NameTest priority, and then this order.
    """
    rules: list[SpaceRule] = []
    locations: list[IssueLocation | None] = []
    seen: set[int] = set()
    names = (f"{{{XSLT_NS}}}strip-space", f"{{{XSLT_NS}}}preserve-space")

    def visit(module: EffectiveModule) -> None:
        if id(module) in seen:
            return
        seen.add(id(module))
        for declaration in module.root.elements:
            if declaration.name not in names:
                continue
            elements = declaration.attribute("elements")
            if not elements:
                continue
            default_namespace = declaration.xpath_default_namespace
            tokens: list[str] = []
            for token in elements.split():
                if (
                    default_namespace is not None
                    and token != "*"
                    and ":" not in token
                    and not token.startswith("Q{")
                ):
                    token = f"Q{{{default_namespace}}}{token}"
                tokens.append(token)
            rules.append(
                SpaceRule(
                    elements=tuple(tokens),
                    namespaces=declaration.in_scope_namespaces,
                    preserve=declaration.name == names[1],
                    module=module.identity,
                    base_uri=declaration.base_uri,
                    precedence=module.precedence,
                )
            )
            locations.append(declaration.source_location())
        for dependent in (*module.includes, *module.imports):
            visit(dependent)

    for module in package.modules:
        visit(module)
    for index, rule in enumerate(rules):
        for previous_index in range(index):
            previous = rules[previous_index]
            if previous.precedence != rule.precedence or previous.preserve == rule.preserve:
                continue
            shared = _shared_name_test(previous, rule)
            if shared is not None:
                raise XsltStaticError(
                    f"the NameTest {shared!r} appears in both xsl:strip-space and "
                    "xsl:preserve-space at the same import precedence",
                    code="XTSE0270",
                    uri=rule.module,
                    location=locations[index],
                )
    return tuple(rules)


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
        if component.hidden:
            continue
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
    context = replace(context, package=package)
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
        plan = _compile_instruction(child, context, module)
        plans.append(plan)
        if isinstance(plan, (_VariablePlan, _ParamPlan)):
            context = context.with_variable_name(plan.name)
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
    if isinstance(plan, _TryPlan):
        # A try body always keeps its handler, so its final call is not a tail
        # call.  A catch body has no handler of its own; when the try itself is
        # in tail position its final call can still replace the catch frame.
        return replace(
            plan,
            catches=tuple(
                replace(catch, children=_mark_tail(catch.children)) for catch in plan.catches
            ),
            tail=True,
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
    select = compile_xpath(select_source, context, location=instruction.location)
    parameters: list[_ParamPlan] = []
    body_children: list[Any] = []
    on_completion: Completion | None = None
    on_completion_instruction: Instruction | None = None
    body_started = False
    for child in instruction.children:
        if isinstance(child, TextNode):
            body_started = True
            body_children.append(child)
            continue
        if child.is_xslt and child.local_name == "param":
            if body_started or on_completion_instruction is not None:
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
            if on_completion_instruction is not None:
                raise XsltStaticError(
                    "xsl:iterate permits at most one xsl:on-completion",
                    code="XTSE0010",
                    uri=context.uri,
                    location=child.location,
                )
            on_completion_instruction = child
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
        context = context.with_variable_name(parameter.name)
    if on_completion_instruction is not None:
        on_completion = _compile_completion(on_completion_instruction, context, module)
    body = _mark_tail(_compile_sequence(body_children, context, module))
    _validate_iteration_controls(body, tuple(parameter_names))
    return _IteratePlan(
        instruction=instruction,
        expression=select,
        parameters=tuple(parameters),
        parameter_names=tuple(parameter_names),
        on_completion=on_completion,
        children=body,
    )


def _compile_merge(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _MergePlan:
    context = _instruction_context(context, instruction)
    sources: list[_MergeSourcePlan] = []
    action: tuple[_Plan, ...] | None = None
    for child in instruction.children:
        if isinstance(child, TextNode):
            raise XsltStaticError(
                "xsl:merge permits only xsl:merge-source, xsl:merge-action, and xsl:fallback",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        if not (child.is_xslt and child.local_name in ("merge-source", "merge-action", "fallback")):
            raise XsltStaticError(
                "xsl:merge permits only xsl:merge-source, xsl:merge-action, and xsl:fallback",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        if child.local_name == "fallback":
            continue
        if child.local_name == "merge-source":
            if action is not None:
                raise XsltStaticError(
                    "xsl:merge-source must precede xsl:merge-action",
                    code="XTSE0010",
                    uri=context.uri,
                    location=child.location,
                )
            sources.append(_compile_merge_source(child, context, module))
            continue
        if action is not None:
            raise XsltStaticError(
                "xsl:merge permits at most one xsl:merge-action",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        action_context = _instruction_context(context, child)
        action = _compile_sequence(child.children, action_context, module)
    if not sources:
        raise XsltStaticError(
            "xsl:merge requires at least one xsl:merge-source",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    if action is None:
        raise XsltStaticError(
            "xsl:merge requires an xsl:merge-action",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    names = [source.name for source in sources if source.name is not None]
    if len(names) != len(set(names)):
        raise XsltStaticError(
            "two sibling xsl:merge-source elements have the same name",
            code="XTSE3190",
            uri=context.uri,
            location=instruction.location,
        )
    key_counts = {len(source.keys) for source in sources}
    if len(key_counts) > 1:
        raise XsltStaticError(
            "all xsl:merge-source elements must have the same number of xsl:merge-key children",
            code="XTSE2200",
            uri=context.uri,
            location=instruction.location,
        )
    return _MergePlan(instruction=instruction, sources=tuple(sources), action=action)


def _compile_merge_source(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _MergeSourcePlan:
    context = _instruction_context(context, instruction)
    select_source = instruction.attribute("select")
    if select_source is None:
        raise XsltStaticError(
            "xsl:merge-source requires a select attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    for_each_item_source = instruction.attribute("for-each-item")
    for_each_source_source = instruction.attribute("for-each-source")
    streamable = _attribute_yes_no(instruction, "streamable", context)
    sort_before_merge = _attribute_yes_no(instruction, "sort-before-merge", context)
    use_accumulators = instruction.attribute("use-accumulators")
    if for_each_item_source is not None and (
        for_each_source_source is not None or use_accumulators is not None or streamable
    ):
        raise XsltStaticError(
            "xsl:merge-source for-each-item excludes for-each-source, use-accumulators, "
            "and streamable",
            code="XTSE3195",
            uri=context.uri,
            location=instruction.location,
        )
    if use_accumulators is not None and for_each_source_source is None:
        raise XsltStaticError(
            "xsl:merge-source use-accumulators requires for-each-source",
            code="XTSE3195",
            uri=context.uri,
            location=instruction.location,
        )
    validation = instruction.attribute("validation")
    type_name = instruction.attribute("type")
    if validation is not None and type_name is not None:
        raise XsltStaticError(
            "xsl:merge-source validation and type are mutually exclusive",
            code="XTSE1505",
            uri=context.uri,
            location=instruction.location,
        )
    if validation is not None and validation.strip() not in _VALIDATION_MODES:
        raise XsltStaticError(
            f"xsl:merge-source has an invalid validation value {validation!r}",
            code="XTSE0020",
            uri=context.uri,
            location=instruction.location,
        )
    if (validation is not None or type_name is not None) and for_each_source_source is None:
        raise XsltStaticError(
            "xsl:merge-source validation and type require for-each-source",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    name = instruction.attribute("name")
    if name is not None:
        name = name.strip()
        if not is_ncname(name):
            raise XsltStaticError(
                f"xsl:merge-source has an invalid name {name!r}",
                code="XTSE0010",
                uri=context.uri,
                location=instruction.location,
            )
    keys: list[SortKey] = []
    for child in instruction.children:
        if isinstance(child, TextNode):
            raise XsltStaticError(
                "xsl:merge-source permits only xsl:merge-key and xsl:fallback content",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        if child.is_xslt and child.local_name == "fallback":
            continue
        if not (child.is_xslt and child.local_name == "merge-key"):
            raise XsltStaticError(
                "xsl:merge-source permits only xsl:merge-key and xsl:fallback content",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        keys.append(_compile_merge_key(child, context, module))
    if not keys:
        raise XsltStaticError(
            "xsl:merge-source requires at least one xsl:merge-key",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    return _MergeSourcePlan(
        instruction=instruction,
        name=name,
        select=compile_xpath(select_source, context, location=instruction.location),
        keys=tuple(keys),
        for_each_item=(
            compile_xpath(for_each_item_source, context, location=instruction.location)
            if for_each_item_source is not None
            else None
        ),
        for_each_source=(
            compile_xpath(for_each_source_source, context, location=instruction.location)
            if for_each_source_source is not None
            else None
        ),
        streamable=streamable,
        sort_before_merge=sort_before_merge,
        validation=None if validation is None else validation.strip(),
        type_name=(
            None
            if type_name is None
            else _compile_validation_type(type_name, context, instruction, "xsl:merge-source")
        ),
        base_uri=instruction.base_uri,
        use_accumulators=_compile_use_accumulators(
            use_accumulators,
            context,
            instruction,
        ),
    )


def _compile_merge_key(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> SortKey:
    context = _instruction_context(context, instruction)
    select_source = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select_source is not None and _has_plan_content(children, allow_fallback=True):
        raise XsltStaticError(
            "xsl:merge-key with a select attribute must have empty content except for xsl:fallback",
            code="XTSE3200",
            uri=context.uri,
            location=instruction.location,
        )
    if instruction.attribute("stable") is not None:
        raise XsltStaticError(
            "xsl:merge-key does not have a stable attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    expression = (
        compile_xpath(select_source, context, location=instruction.location)
        if select_source is not None
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
        children=() if select_source is not None else children,
        lang=avt("lang"),
        order=avt("order"),
        collation=avt("collation"),
        case_order=avt("case-order"),
        data_type=avt("data-type"),
        base_uri=instruction.base_uri,
        xslt_version=context.version,
    )


def _compile_fork(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _ForkPlan:
    context = _instruction_context(context, instruction)
    elements: list[Instruction] = []
    for child in instruction.children:
        if isinstance(child, TextNode):
            raise XsltStaticError(
                "xsl:fork permits only xsl:sequence, xsl:for-each-group, and xsl:fallback",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        if child.is_xslt and child.local_name == "fallback":
            continue
        elements.append(child)
    if len(elements) == 1 and elements[0].is_xslt and elements[0].local_name == "for-each-group":
        return _ForkPlan(
            instruction=instruction,
            branches=((_compile_instruction(elements[0], context, module),),),
        )
    branches: list[tuple[_Plan, ...]] = []
    for child in elements:
        if not (child.is_xslt and child.local_name == "sequence"):
            raise XsltStaticError(
                "xsl:fork permits only xsl:sequence, xsl:for-each-group, and xsl:fallback",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        branches.append((_compile_instruction(child, context, module),))
    return _ForkPlan(instruction=instruction, branches=tuple(branches))


def _compile_source_document(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _SourceDocumentPlan:
    context = _instruction_context(context, instruction)
    href = instruction.attribute("href")
    if href is None:
        raise XsltStaticError(
            "xsl:source-document requires an href attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    streamable = _attribute_yes_no(instruction, "streamable", context)
    validation = instruction.attribute("validation")
    type_name = instruction.attribute("type")
    if validation is not None and type_name is not None:
        raise XsltStaticError(
            "xsl:source-document validation and type are mutually exclusive",
            code="XTSE1505",
            uri=context.uri,
            location=instruction.location,
        )
    if validation is not None and validation.strip() not in _VALIDATION_MODES:
        raise XsltStaticError(
            f"xsl:source-document has an invalid validation value {validation!r}",
            code="XTSE0020",
            uri=context.uri,
            location=instruction.location,
        )
    return _SourceDocumentPlan(
        instruction=instruction,
        href=compile_avt(href, context, location=instruction.location),
        children=_compile_sequence(instruction.children, context, module),
        streamable=streamable,
        validation=None if validation is None else validation.strip(),
        type_name=(
            None
            if type_name is None
            else _compile_validation_type(type_name, context, instruction, "xsl:source-document")
        ),
        use_accumulators=_compile_use_accumulators(
            instruction.attribute("use-accumulators"),
            context,
            instruction,
        ),
    )


def _compile_validation_type(
    value: str,
    context: StaticContext,
    instruction: Instruction,
    label: str,
) -> str:
    """Resolve an [xsl:]type QName against the in-scope schema definitions."""
    text = value.strip()
    if not text:
        raise XsltStaticError(
            f"{label} has an empty type attribute",
            code="XTSE1520",
            uri=context.uri,
            location=instruction.location,
        )
    if text.startswith("Q{"):
        close = text.find("}")
        if close < 0:
            raise XsltStaticError(
                f"{label} has an invalid EQName {text!r}",
                code="XTSE1520",
                uri=context.uri,
                location=instruction.location,
            )
        namespace_uri = text[2:close]
        local = text[close + 1 :]
        expanded = f"{{{namespace_uri}}}{local}" if namespace_uri else local
    else:
        prefix, separator, local = text.partition(":")
        if separator:
            bound_uri = context.namespaces.get(prefix)
            if bound_uri is None:
                raise XsltStaticError(
                    f"the namespace prefix {prefix!r} is not bound in the static context",
                    code="XTSE1520",
                    uri=context.uri,
                    location=instruction.location,
                )
            expanded = f"{{{bound_uri}}}{local}"
        else:
            default = context.xpath_default_namespace
            expanded = f"{{{default}}}{text}" if default else text
    if _is_builtin_type_name(expanded):
        return expanded
    pool = context.schema
    if pool is None or pool.get_type(expanded) is None:
        raise XsltStaticError(
            f"the type {text!r} is not in the in-scope schema definitions",
            code="XTSE1520",
            uri=context.uri,
            location=instruction.location,
        )
    return expanded


def _is_builtin_type_name(name: str) -> bool:

    namespace, _, local = name[1:].partition("}") if name.startswith("{") else ("", "", name)
    return namespace == "http://www.w3.org/2001/XMLSchema" and (
        local in builtin_type_classes() or local in ("untyped", "untypedAtomic")
    )


def _compile_construction_validation(
    instruction: Instruction,
    context: StaticContext,
    *,
    label: str,
    literal: bool = False,
    attribute: bool = False,
) -> tuple[str, str | None]:
    """Resolve one construction instruction's validation and type request."""
    if literal:
        type_value = instruction.attribute(f"{{{XSLT_NS}}}type")
        validation = instruction.attribute(f"{{{XSLT_NS}}}validation")
    else:
        type_value = instruction.attribute("type")
        validation = instruction.attribute("validation")
    if validation is not None and type_value is not None:
        raise XsltStaticError(
            f"{label} type and validation are mutually exclusive",
            code="XTSE1505",
            uri=context.uri,
            location=instruction.location,
        )
    if validation is not None and validation.strip() not in _VALIDATION_MODES:
        raise XsltStaticError(
            f"{label} has an invalid validation value {validation!r}",
            code="XTSE0020",
            uri=context.uri,
            location=instruction.location,
        )
    effective = (
        validation.strip() if validation is not None else (context.default_validation or "strip")
    )
    type_name = (
        None
        if type_value is None
        else _compile_validation_type(type_value, context, instruction, label)
    )
    if type_name is not None and attribute:
        _require_simple_construction_type(type_name, context, instruction, label)
    return effective, type_name


def _require_simple_construction_type(
    type_name: str,
    context: StaticContext,
    instruction: Instruction,
    label: str,
) -> None:
    if type_name in (UNTYPED_TYPE, ANY_TYPE):
        raise XsltStaticError(
            f"{label} cannot validate an attribute against a complex type",
            code="XTSE1530",
            uri=context.uri,
            location=instruction.location,
        )
    pool = context.schema
    adapter = pool.get_type(type_name) if pool is not None else None
    if adapter is not None and not adapter.is_simple():
        raise XsltStaticError(
            f"{label} cannot validate an attribute against a complex type",
            code="XTSE1530",
            uri=context.uri,
            location=instruction.location,
        )


def _compile_analyze_string(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _AnalyzeStringPlan:
    context = _instruction_context(context, instruction)
    select = instruction.attribute("select")
    regex = instruction.attribute("regex")
    if select is None or regex is None:
        raise XsltStaticError(
            "xsl:analyze-string requires select and regex attributes",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    matching: tuple[_Plan, ...] | None = None
    non_matching: tuple[_Plan, ...] | None = None
    fallback_seen = False
    for child in instruction.children:
        if isinstance(child, TextNode):
            if child.value.strip():
                raise XsltStaticError(
                    "xsl:analyze-string permits only matching and non-matching branches",
                    code="XTSE0010",
                    uri=context.uri,
                    location=child.location,
                )
            continue
        if not child.is_xslt:
            raise XsltStaticError(
                "xsl:analyze-string permits only matching and non-matching branches",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        local = child.local_name
        if local == "fallback":
            fallback_seen = True
            continue
        if fallback_seen:
            raise XsltStaticError(
                "xsl:fallback must follow the analyze-string branches",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        if local == "matching-substring":
            if matching is not None or non_matching is not None:
                raise XsltStaticError(
                    "xsl:matching-substring must occur once and before any non-matching branch",
                    code="XTSE0010",
                    uri=context.uri,
                    location=child.location,
                )
            matching = _compile_sequence(
                child.children,
                _instruction_context(context, child),
                module,
            )
        elif local == "non-matching-substring":
            if non_matching is not None:
                raise XsltStaticError(
                    "xsl:non-matching-substring must occur at most once",
                    code="XTSE0010",
                    uri=context.uri,
                    location=child.location,
                )
            non_matching = _compile_sequence(
                child.children,
                _instruction_context(context, child),
                module,
            )
        else:
            raise XsltStaticError(
                "xsl:analyze-string permits only matching and non-matching branches",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
    if matching is None and non_matching is None:
        raise XsltStaticError(
            "xsl:analyze-string requires a matching or non-matching branch",
            code="XTSE1130",
            uri=context.uri,
            location=instruction.location,
        )
    flags = instruction.attribute("flags")
    return _AnalyzeStringPlan(
        instruction=instruction,
        expression=compile_xpath(select, context, location=instruction.location),
        regex=compile_avt(regex, context, location=instruction.location),
        flags=compile_avt(flags, context, location=instruction.location)
        if flags is not None
        else None,
        matching=matching or (),
        non_matching=non_matching or (),
    )


def _compile_number(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _NumberPlan:
    context = _instruction_context(context, instruction)
    value = instruction.attribute("value")
    select = instruction.attribute("select")
    level = instruction.attribute("level") or "single"
    count = instruction.attribute("count")
    from_source = instruction.attribute("from")
    if value is not None and (
        select is not None
        or instruction.attribute("level") is not None
        or count is not None
        or from_source is not None
    ):
        raise XsltStaticError(
            "xsl:number value may not be combined with select, level, count, or from",
            code="XTSE0975",
            uri=context.uri,
            location=instruction.location,
        )
    if level not in ("single", "multiple", "any"):
        raise XsltStaticError(
            f"invalid xsl:number level {level!r}",
            code="XTSE0020",
            uri=context.uri,
            location=instruction.location,
        )
    children = _compile_sequence(instruction.children, context, module)
    if _has_plan_content(children, allow_fallback=True):
        raise XsltStaticError(
            "xsl:number must have empty content except for xsl:fallback",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    format_source = instruction.attribute("format")
    return _NumberPlan(
        instruction=instruction,
        value=(
            compile_xpath(value, context, location=instruction.location)
            if value is not None
            else None
        ),
        select=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        level=level,
        count=(
            compile_pattern(count, context, location=instruction.location)
            if count is not None
            else None
        ),
        from_pattern=(
            compile_pattern(from_source, context, location=instruction.location)
            if from_source is not None
            else None
        ),
        format=(
            compile_avt(format_source, context, location=instruction.location)
            if format_source is not None
            else "1"
        ),
        lang=_number_avt(instruction, "lang", context),
        ordinal=_number_avt(instruction, "ordinal", context),
        letter_value=_number_avt(instruction, "letter-value", context),
        grouping_separator=_number_avt(instruction, "grouping-separator", context),
        grouping_size=_number_avt(instruction, "grouping-size", context),
        start_at=_number_avt(instruction, "start-at", context),
    )


def _number_avt(
    instruction: Instruction,
    name: str,
    context: StaticContext,
) -> CompiledValueTemplate | None:
    source = instruction.attribute(name)
    if source is None:
        return None
    return compile_avt(source, context, location=instruction.location)


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
    if isinstance(plan, _TryPlan):
        nested_parameters = parameters if tail else None
        _validate_iteration_controls(plan.children, nested_parameters)
        for catch in plan.catches:
            _validate_iteration_controls(catch.children, nested_parameters)
        return
    if isinstance(plan, _SequencePlan):
        if plan.expression is None:
            _validate_iteration_controls(plan.children, None)
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
    if isinstance(plan, _MergePlan):
        groups = [plan.action]
        groups.extend(key.children for source in plan.sources for key in source.keys)
        return tuple(groups)
    if isinstance(plan, _ForkPlan):
        return plan.branches
    if isinstance(plan, _SourceDocumentPlan):
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


_CATCH_VARIABLE_NAMES = (
    "code",
    "description",
    "value",
    "module",
    "line-number",
    "column-number",
)


def _catch_context(context: StaticContext) -> StaticContext:
    """Declare the six standard error variables for one catch body."""
    variables = dict(context.variables)
    for name in _CATCH_VARIABLE_NAMES:
        expanded = f"{{{ERROR_NAMESPACE}}}{name}"
        variables.setdefault(
            expanded,
            StaticVariable(name=expanded, kind="variable", visibility="private"),
        )
    return context.with_variables(variables)


def _compile_catch(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _CatchPlan:
    """Compile one ``xsl:catch`` body with its error variables in scope."""
    context = _catch_context(_instruction_context(context, instruction))
    errors_source = instruction.attribute("errors")
    errors = None if errors_source is None else tuple(errors_source.split())
    select = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select is not None and _has_plan_content(children, allow_fallback=True):
        raise XsltStaticError(
            "xsl:catch with a select attribute must have empty content except for xsl:fallback",
            code="XTSE3150",
            uri=context.uri,
            location=instruction.location,
        )
    return _CatchPlan(
        instruction=instruction,
        errors=errors,
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
    )


def _compile_try(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _TryPlan:
    """Compile one ``xsl:try`` body and its ordered catch declarations."""
    context = _instruction_context(context, instruction)
    select = instruction.attribute("select")
    rollback_source = instruction.attribute("rollback-output")
    rollback_output = (
        True
        if rollback_source is None
        else _attribute_yes_no(instruction, "rollback-output", context)
    )
    body: list[Any] = []
    catches: list[_CatchPlan] = []
    seen_catch = False
    for child in instruction.children:
        if isinstance(child, TextNode):
            if seen_catch:
                if child.value.strip():
                    raise XsltStaticError(
                        "xsl:try permits only xsl:catch and xsl:fallback after the first xsl:catch",
                        code="XTSE0010",
                        uri=context.uri,
                        location=child.location,
                    )
                continue
            body.append(child)
            continue
        if child.is_xslt and child.local_name == "fallback":
            continue
        if child.is_xslt and child.local_name == "catch":
            seen_catch = True
            catches.append(_compile_catch(child, context, module))
            continue
        if seen_catch:
            raise XsltStaticError(
                "xsl:try permits only xsl:catch and xsl:fallback after the first xsl:catch",
                code="XTSE0010",
                uri=context.uri,
                location=child.location,
            )
        body.append(child)
    if not catches:
        raise XsltStaticError(
            "xsl:try requires at least one xsl:catch",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    body_plans = _compile_sequence(body, context, module)
    if select is not None and _has_plan_content(body_plans, allow_fallback=True):
        raise XsltStaticError(
            "xsl:try with a select attribute must have no content other than "
            "xsl:catch and xsl:fallback",
            code="XTSE3140",
            uri=context.uri,
            location=instruction.location,
        )
    return _TryPlan(
        instruction=instruction,
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=() if select is not None else body_plans,
        catches=tuple(catches),
        rollback_output=rollback_output,
    )


def _compile_instruction(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    """Compile one normalized instruction into its immutable plan.

    Literal result elements stay explicit.  Every XSLT instruction family is
    compiled by the dedicated compiler registered under its expanded name, and
    an unregistered instruction keeps the explicit forwards-compatible or
    unsupported handling.
    """
    context = _instruction_context(context, instruction)
    context = _original_context(context, instruction)
    if instruction.literal:
        return _compile_literal_result(instruction, context, module)
    if not supports_instruction(instruction.name):
        return _compile_unhandled_instruction(instruction, context, module)
    return compile_instruction(instruction, context, module)


def _compile_literal_result(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    attributes: list[tuple[str, str | CompiledValueTemplate]] = []
    for attribute in instruction.attributes:
        if attribute.namespace_uri == XSLT_NS:
            continue
        if attribute.namespace_uri is None and attribute.name in _LITERAL_XSLT_ATTRIBUTES:
            continue
        attributes.append(
            (
                attribute.name,
                compile_avt(attribute.value, context, location=instruction.location),
            )
        )
    namespaces = _literal_namespaces(
        instruction,
        attributes,
        _exclusions(module),
    )
    validation, type_name = _compile_construction_validation(
        instruction,
        context,
        label="a literal result element",
        literal=True,
    )
    return _LiteralPlan(
        instruction=instruction,
        attributes=tuple(attributes),
        children=_compile_sequence(instruction.children, context, module),
        namespaces=namespaces,
        attribute_sets=tuple(
            reference for reference in instruction.references if reference.kind == "attribute-set"
        ),
        validation=validation,
        type_name=type_name,
    )


def _compile_element(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    name = instruction.attribute("name")
    if name is None:
        raise XsltStaticError(
            "xsl:element requires a name attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    validation, type_name = _compile_construction_validation(
        instruction,
        context,
        label="xsl:element",
    )
    return _ElementPlan(
        instruction=instruction,
        name=compile_avt(name, context, location=instruction.location),
        namespace=(
            compile_avt(
                instruction.attribute("namespace") or "",
                context,
                location=instruction.location,
            )
            if instruction.attribute("namespace") is not None
            else None
        ),
        namespaces=instruction.namespaces,
        children=_compile_sequence(instruction.children, context, module),
        attribute_sets=tuple(
            reference for reference in instruction.references if reference.kind == "attribute-set"
        ),
        inherit_namespaces=_attribute_yes_no(
            instruction, "inherit-namespaces", context, default=True
        ),
        validation=validation,
        type_name=type_name,
    )


def _compile_document(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    validation, type_name = _compile_construction_validation(
        instruction,
        context,
        label="xsl:document",
    )
    return _DocumentPlan(
        instruction=instruction,
        children=_compile_sequence(instruction.children, context, module),
        validation=validation,
        type_name=type_name,
    )


def _compile_copy(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    select = instruction.attribute("select")
    validation, type_name = _compile_construction_validation(
        instruction,
        context,
        label="xsl:copy",
    )
    return _CopyPlan(
        instruction=instruction,
        select=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=_compile_sequence(instruction.children, context, module),
        copy_namespaces=_attribute_yes_no(instruction, "copy-namespaces", context, default=True),
        inherit_namespaces=_attribute_yes_no(
            instruction, "inherit-namespaces", context, default=True
        ),
        attribute_sets=tuple(
            reference for reference in instruction.references if reference.kind == "attribute-set"
        ),
        validation=validation,
        type_name=type_name,
    )


def _compile_copy_of(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    select = instruction.attribute("select")
    if select is None:
        raise XsltStaticError(
            "xsl:copy-of requires a select attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    validation, type_name = _compile_construction_validation(
        instruction,
        context,
        label="xsl:copy-of",
    )
    return _CopyOfPlan(
        instruction=instruction,
        select=compile_xpath(select, context, location=instruction.location),
        copy_namespaces=_attribute_yes_no(instruction, "copy-namespaces", context, default=True),
        copy_accumulators=_attribute_yes_no(instruction, "copy-accumulators", context),
        validation=validation,
        type_name=type_name,
    )


def _compile_sequence_instruction(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    select = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select is not None and _has_plan_content(children, allow_fallback=True):
        raise XsltStaticError(
            "xsl:sequence with a select attribute must have empty content except for xsl:fallback",
            code="XTSE3185",
            uri=context.uri,
            location=instruction.location,
        )
    return _SequencePlan(
        instruction=instruction,
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
    )


def _compile_value_of(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    select = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select is not None and _has_plan_content(children):
        raise XsltStaticError(
            "xsl:value-of with a select attribute must have empty content",
            code="XTSE0870",
            uri=context.uri,
            location=instruction.location,
        )
    separator = instruction.attribute("separator")
    separator_default = " " if select is not None else ""
    return _ValueOfPlan(
        instruction=instruction,
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
        separator=(
            compile_avt(separator, context, location=instruction.location)
            if separator is not None
            else separator_default
        ),
        explicit_separator=separator is not None,
    )


def _compile_text(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    text = "".join(child.value for child in instruction.children if isinstance(child, TextNode))
    expand = instruction.attribute("expand-text")
    enabled = (
        context.expand_text
        if expand is None
        else _yes_no(expand, instruction, "expand-text", context)
    )
    if enabled:
        return _TvtPlan(
            compile_tvt(text, context.with_expand_text(True), location=instruction.location)
        )
    return _TextPlan(text)


def _compile_for_each(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    select = instruction.attribute("select")
    if select is None:
        raise XsltStaticError(
            "xsl:for-each requires a select attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    _, body_children = _split_sort_children(instruction)
    body = _mark_tail(_compile_sequence(body_children, context, module))
    return _ForEachPlan(
        instruction=instruction,
        expression=compile_xpath(select, context, location=instruction.location),
        sorts=_compile_sort_keys(instruction, context, module),
        children=body,
    )


def _compile_contained_merge(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    raise XsltStaticError(
        f"xsl:{instruction.local_name} is only permitted in a containing merge instruction",
        code="XTSE0010",
        uri=context.uri,
        location=instruction.location,
    )


def _compile_legacy_stream(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    raise XsltStaticError(
        "xsl:stream was renamed to xsl:source-document in XSLT 3.0",
        code="XTSE0010",
        uri=context.uri,
        location=instruction.location,
    )


def _compile_contained_sort(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    raise XsltStaticError(
        f"xsl:{instruction.local_name} is only permitted in a containing instruction",
        code="XTSE0010",
        uri=context.uri,
        location=instruction.location,
    )


def _compile_param(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    name = instruction.attribute("name")
    if not name:
        raise XsltStaticError(
            "xsl:param requires a name",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
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
            location=instruction.location,
        )
    if required and (select is not None or _has_plan_content(children)):
        raise XsltStaticError(
            "a required parameter must not have a default value",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    as_source = instruction.attribute("as")
    return _ParamPlan(
        instruction=instruction,
        name=_expanded_name(name, instruction.namespaces),
        required=required,
        tunnel=tunnel,
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
        has_as=as_source is not None,
        sequence_type=(
            compile_sequence_type(as_source, context, location=instruction.location)
            if as_source is not None
            else None
        ),
    )


def _compile_variable(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    name = instruction.attribute("name")
    if not name:
        raise XsltStaticError(
            "xsl:variable requires a name",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    select = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select is not None and _has_plan_content(children):
        raise XsltStaticError(
            "a variable with a select attribute must have empty content",
            code="XTSE0620",
            uri=context.uri,
            location=instruction.location,
        )
    as_source = instruction.attribute("as")
    return _VariablePlan(
        instruction=instruction,
        name=_expanded_name(name, instruction.namespaces),
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
        has_as=as_source is not None,
        sequence_type=(
            compile_sequence_type(as_source, context, location=instruction.location)
            if as_source is not None
            else None
        ),
    )


def _compile_attribute(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    name = instruction.attribute("name")
    if not name:
        raise XsltStaticError(
            "xsl:attribute requires a name",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    select = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select is not None and _has_plan_content(children):
        raise XsltStaticError(
            "an attribute with a select attribute must have empty content",
            code="XTSE0620",
            uri=context.uri,
            location=instruction.location,
        )
    validation, type_name = _compile_construction_validation(
        instruction,
        context,
        label="xsl:attribute",
        attribute=True,
    )
    separator = instruction.attribute("separator")
    return _AttributePlan(
        instruction=instruction,
        name=compile_avt(name, context, location=instruction.location),
        namespaces=instruction.namespaces,
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
        namespace=(
            compile_avt(
                instruction.attribute("namespace") or "",
                context,
                location=instruction.location,
            )
            if instruction.attribute("namespace") is not None
            else None
        ),
        separator=(
            compile_avt(separator, context, location=instruction.location)
            if separator is not None
            else " "
        ),
        validation=validation,
        type_name=type_name,
    )


def _compile_overriding(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    return _OverridingPlan(
        instruction=instruction,
        kind=cast(Literal["next-match", "apply-imports"], instruction.local_name),
        parameters=_compile_with_params(instruction, context, module),
    )


def _compile_result_document(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
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
                compile_avt(attribute.value, context, location=instruction.location),
            )
        )
    validation, type_name = _compile_construction_validation(
        instruction,
        context,
        label="xsl:result-document",
    )
    return _ResultDocumentPlan(
        instruction=instruction,
        href=(
            compile_avt(href, context, location=instruction.location) if href is not None else ""
        ),
        method=(
            compile_avt(method, context, location=instruction.location)
            if method is not None
            else None
        ),
        build_tree=(
            compile_avt(build_tree, context, location=instruction.location)
            if build_tree is not None
            else None
        ),
        format=(
            compile_avt(format_name, context, location=instruction.location)
            if format_name is not None
            else None
        ),
        attributes=tuple(extra_attributes),
        children=_compile_sequence(instruction.children, context, module),
        validation=validation,
        type_name=type_name,
    )


def _compile_analyze_string_part(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    raise XsltStaticError(
        f"xsl:{instruction.local_name} is only permitted inside xsl:analyze-string",
        code="XTSE0010",
        uri=context.uri,
        location=instruction.location,
    )


def _compile_choose(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
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


def _compile_if(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    test = instruction.attribute("test")
    if test is None:
        raise XsltStaticError(
            "xsl:if requires a test attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    return _ChoosePlan(
        instruction=instruction,
        branches=(
            (
                compile_xpath(test, context, location=instruction.location),
                _compile_sequence(instruction.children, context, module),
            ),
        ),
        otherwise=None,
    )


def _compile_stray_catch(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    raise XsltStaticError(
        "xsl:catch is only permitted inside xsl:try",
        code="XTSE0010",
        uri=context.uri,
        location=instruction.location,
    )


def _compile_map(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    if instruction.attribute("select") is not None:
        raise XsltStaticError(
            "xsl:map does not have a select attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    return _MapPlan(
        instruction=instruction,
        children=_compile_sequence(instruction.children, context, module),
    )


def _compile_map_entry(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    key = instruction.attribute("key")
    if key is None:
        raise XsltStaticError(
            "xsl:map-entry requires a key attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    select = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select is not None and _has_plan_content(children, allow_fallback=True):
        raise XsltStaticError(
            "xsl:map-entry with a select attribute must have empty content except for xsl:fallback",
            code="XTSE3280",
            uri=context.uri,
            location=instruction.location,
        )
    return _MapEntryPlan(
        instruction=instruction,
        key=compile_xpath(key, context, location=instruction.location),
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
    )


def _compile_assert(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    test = instruction.attribute("test")
    if test is None:
        raise XsltStaticError(
            "xsl:assert requires a test attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    select = instruction.attribute("select")
    error_code = instruction.attribute("error-code")
    return _AssertPlan(
        instruction=instruction,
        test=compile_xpath(test, context, location=instruction.location),
        select=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=_compile_sequence(instruction.children, context, module),
        error_code=(
            compile_avt(error_code, context, location=instruction.location)
            if error_code is not None
            else None
        ),
        enabled=_assertion_checking_enabled(context),
    )


def _compile_where_populated(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    if instruction.attribute("select") is not None:
        raise XsltStaticError(
            "xsl:where-populated does not have a select attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    return _WherePopulatedPlan(
        instruction=instruction,
        children=_compile_sequence(instruction.children, context, module),
    )


def _compile_conditional_content(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    select = instruction.attribute("select")
    children = _compile_sequence(instruction.children, context, module)
    if select is not None and _has_plan_content(children, allow_fallback=True):
        raise XsltStaticError(
            f"xsl:{instruction.local_name} with a select attribute must have empty content "
            "except for xsl:fallback",
            code="XTSE3185",
            uri=context.uri,
            location=instruction.location,
        )
    plan_type = _OnEmptyPlan if instruction.local_name == "on-empty" else _OnNonEmptyPlan
    return plan_type(
        instruction=instruction,
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
    )


def _compile_fallback(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    return _FallbackPlan(
        instruction=instruction,
        children=_compile_sequence(instruction.children, context, module),
    )


def _compile_call_template(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    reference = instruction.reference
    if reference is None:
        raise XsltStaticError(
            "xsl:call-template requires a visible named template",
            code="XTSE0650",
            uri=context.uri,
            location=instruction.location,
        )
    return _CallTemplatePlan(
        instruction=instruction,
        reference=reference,
        parameters=_compile_with_params(instruction, context, module),
    )


def _compile_apply_templates(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    select = instruction.attribute("select")
    expression = (
        compile_xpath(select, context, location=instruction.location)
        if select is not None
        else compile_xpath("child::node()", context, location=instruction.location)
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


def _compile_stray_with_param(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    raise XsltStaticError(
        "xsl:with-param is only permitted inside a call or apply instruction",
        code="XTSE0010",
        uri=context.uri,
        location=instruction.location,
    )


def _compile_unhandled_instruction(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    """Compile an instruction with no registered family compiler.

    Forwards-compatible names keep the fallback handling; every other known
    XSLT 3.0 element still compiles its expression attributes and content before
    reporting the explicit unsupported plan.
    """
    local = instruction.local_name
    if instruction.is_xslt and local not in _XSLT30_ELEMENTS:
        return _compile_forward_instruction(instruction, context, module)
    select = instruction.attribute("select")
    if select is not None:
        compile_xpath(select, context, location=instruction.location)
    test = instruction.attribute("test")
    if test is not None:
        compile_xpath(test, context, location=instruction.location)
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


#: Every instruction family, keyed by expanded XSLT name.  Literal result
#: elements and unregistered instructions stay outside this table.
_INSTRUCTION_COMPILERS: Mapping[str, InstructionCompiler] = {
    instruction_name("analyze-string"): _compile_analyze_string,
    instruction_name("apply-imports"): _compile_overriding,
    instruction_name("apply-templates"): _compile_apply_templates,
    instruction_name("assert"): _compile_assert,
    instruction_name("attribute"): _compile_attribute,
    instruction_name("break"): _compile_break,
    instruction_name("call-template"): _compile_call_template,
    instruction_name("catch"): _compile_stray_catch,
    instruction_name("choose"): _compile_choose,
    instruction_name("copy"): _compile_copy,
    instruction_name("copy-of"): _compile_copy_of,
    instruction_name("document"): _compile_document,
    instruction_name("element"): _compile_element,
    instruction_name("evaluate"): _compile_evaluate,
    instruction_name("fallback"): _compile_fallback,
    instruction_name("for-each"): _compile_for_each,
    instruction_name("for-each-group"): _compile_for_each_group,
    instruction_name("fork"): _compile_fork,
    instruction_name("if"): _compile_if,
    instruction_name("iterate"): _compile_iterate,
    instruction_name("map"): _compile_map,
    instruction_name("map-entry"): _compile_map_entry,
    instruction_name("matching-substring"): _compile_analyze_string_part,
    instruction_name("merge"): _compile_merge,
    instruction_name("merge-action"): _compile_contained_merge,
    instruction_name("merge-key"): _compile_contained_merge,
    instruction_name("merge-source"): _compile_contained_merge,
    instruction_name("next-iteration"): _compile_next_iteration,
    instruction_name("next-match"): _compile_overriding,
    instruction_name("non-matching-substring"): _compile_analyze_string_part,
    instruction_name("number"): _compile_number,
    instruction_name("on-completion"): _compile_contained_sort,
    instruction_name("on-empty"): _compile_conditional_content,
    instruction_name("on-non-empty"): _compile_conditional_content,
    instruction_name("param"): _compile_param,
    instruction_name("perform-sort"): _compile_perform_sort,
    instruction_name("result-document"): _compile_result_document,
    instruction_name("sequence"): _compile_sequence_instruction,
    instruction_name("sort"): _compile_contained_sort,
    instruction_name("source-document"): _compile_source_document,
    instruction_name("stream"): _compile_legacy_stream,
    instruction_name("text"): _compile_text,
    instruction_name("try"): _compile_try,
    instruction_name("value-of"): _compile_value_of,
    instruction_name("variable"): _compile_variable,
    instruction_name("where-populated"): _compile_where_populated,
    instruction_name("with-param"): _compile_stray_with_param,
}

register_instruction_compilers(_INSTRUCTION_COMPILERS)


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
    if name.startswith("Q{"):
        close = name.find("}")
        if close >= 0 and close + 1 < len(name):
            eqname_uri = name[2:close]
            local = name[close + 1 :]
            return local if not eqname_uri else f"{{{eqname_uri}}}{local}"
        return name
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
    *,
    default: bool = False,
) -> bool:
    value = instruction.attribute(attribute)
    if value is None:
        return default
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
    validation: str = "strip"
    validation_type: str | None = None
    input_type_annotations: str | None = None


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
    validation: str | None = None,
    validation_type: str | None = None,
) -> _InvocationOptions:
    package = stylesheet.package
    if delivery not in _DELIVERIES:
        raise XsltInvocationError(
            f"delivery must be one of {sorted(_DELIVERIES)}, got {delivery!r}"
        )
    if output_uri is not None:
        _require_absolute(output_uri, "output_uri")
    if validation is not None and validation not in _VALIDATION_MODES:
        raise XsltInvocationError(
            f"validation must be one of {sorted(_VALIDATION_MODES)}, got {validation!r}"
        )
    if validation_type is not None and not isinstance(validation_type, str):
        raise TypeError("validation_type must be a string or None")
    effective_validation = stylesheet.processor.validation if validation is None else validation
    effective_type = (
        stylesheet.processor.validation_type if validation_type is None else validation_type
    )
    if effective_type is not None and effective_validation in ("strip", "preserve"):
        effective_validation = "strict"
    if not stylesheet.processor.schema_aware and effective_validation == "strict":
        raise XsltInvocationError(
            "a non-schema-aware processor cannot use strict validation",
            code=None,
        )

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
        default_selection, default_global = _source_inputs(
            source,
            schema_pool=stylesheet.schema_pool,
            validation=effective_validation,
            validation_type=effective_type,
            input_type_annotations=package.input_type_annotations,
            namespaces=package.namespaces,
            space_rules=stylesheet._space_rules.get(package.name, ()),
        )

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
        validation=effective_validation,
        validation_type=effective_type,
        input_type_annotations=package.input_type_annotations,
    )


def _source_inputs(
    source: Any,
    *,
    schema_pool: SchemaPool | None = None,
    validation: str = "strip",
    validation_type: str | None = None,
    input_type_annotations: str | None = None,
    namespaces: Mapping[str, str] | None = None,
    space_rules: Sequence[SpaceRule] = (),
) -> tuple[XdmSequence, XdmItem | None]:
    annotations = "unspecified" if input_type_annotations is None else input_type_annotations

    def _document(value: Any) -> XdmDocument:
        document = _typed_source(
            value,
            schema_pool=schema_pool,
            validation=validation,
            validation_type=validation_type,
            input_type_annotations=annotations,
            namespaces=namespaces or {},
        )
        return apply_source_space_rules(document, rules=space_rules)

    if isinstance(source, Document):
        document = _document(source)
        return (document,), document
    if isinstance(source, XdmDocument):
        document = _document(source)
        return (document,), document
    if isinstance(source, XdmItem):
        item_sequence: XdmSequence = (source,)
        global_item = source.document if isinstance(source, XdmNode) else None
        return item_sequence, global_item
    if isinstance(source, str) and source.lstrip().startswith("<"):
        document = _document(source.encode("utf-8"))
        return (document,), document
    if isinstance(source, (str, os.PathLike, bytes, bytearray, memoryview)):
        document = _document(source)
        return (document,), document
    if hasattr(source, "read"):
        document = _document(source)
        return (document,), document
    sequence: XdmSequence = as_xdm(source)
    global_item = None
    if len(sequence) == 1 and isinstance(sequence[0], XdmNode):
        global_item = sequence[0].document
    return sequence, global_item


def _typed_source(
    source: Any,
    *,
    schema_pool: SchemaPool | None,
    validation: str,
    validation_type: str | None,
    input_type_annotations: str,
    namespaces: Mapping[str, str],
) -> XdmDocument:
    """Parse one source input, converting validation failures to XSLT codes."""
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    type_name = None
    if validation_type is not None:
        if schema_pool is None:
            expanded = _expanded_name(validation_type, namespaces)
            if _is_builtin_type_name(expanded):
                local = expanded.rpartition("}")[2]
                type_name = expanded if local in ("untyped", "untypedAtomic", "anyType") else None
            else:
                raise XsltDynamicError(
                    f"the type {validation_type!r} is not in the in-scope schema definitions",
                    code="XTSE1520",
                )
        else:
            type_name = _resolve_type_name(validation_type, schema_pool, namespaces)
    try:
        return parse_typed_document(
            source,
            schema_pool=schema_pool,
            validation=validation,
            input_type_annotations=input_type_annotations,
            type_name=type_name,
        )
    except DocumentValidationError as exc:
        code = {
            "unknown": "XTTE1512",
            "invalid": "XTTE1510",
            "lax-invalid": "XTTE1515",
            "type": "XTTE1540",
        }[exc.reason]
        raise XsltDynamicError(str(exc), code=code, cause=exc) from exc


def _resolve_type_name(
    type_name: str,
    schema_pool: SchemaPool,
    namespaces: Mapping[str, str],
) -> str:
    """Return *type_name* if the pool knows it, else raise XPST0051."""
    expanded = _expanded_name(type_name, namespaces)
    if _is_builtin_type_name(expanded):
        return expanded
    if schema_pool.get_type(expanded) is None and schema_pool.get_type(type_name) is None:
        raise XsltDynamicError(
            f"the type {type_name!r} is not in the in-scope schema definitions",
            code="XTSE1520",
        )
    return expanded if expanded.startswith("{") else type_name


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


def _require_absolute(uri: str, label: str) -> str:
    if not isinstance(uri, str) or not uri or not urlsplit(uri).scheme:
        raise XsltInvocationError(f"{label} must be a non-empty absolute URI, got {uri!r}")
    return uri


# ---------------------------------------------------------------------------
# Invocation execution


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
        self.current_merge_group: Mapping[str, XdmSequence] | None = None
        self.current_merge_key: XdmSequence | None = None
        self.current_merge_sources: tuple[str, ...] = ()
        self.current_regex_groups: tuple[str, ...] | None = None
        validation_type = options.validation_type
        if validation_type is not None and stylesheet.schema_pool is not None:
            validation_type = _expanded_name(validation_type, self.package.namespaces)
        self.document_resources = DocumentResources(
            self.documents,
            policy=getattr(stylesheet.processor, "resource_policy", None),
            schema_pool=stylesheet.schema_pool,
            validation=options.validation,
            validation_type=validation_type,
            input_type_annotations=options.input_type_annotations or "unspecified",
            space_rules=stylesheet._space_rules,
        )
        self._accumulator_matchers: dict[int, PatternMatcher] = {}
        self.accumulators = create_accumulator_manager(self)
        self._mark_initial_selection()

    def _package_schema_pool(self, package_name: str | None) -> object:
        return self.stylesheet._package_schema_pools.get(package_name, self.stylesheet.schema_pool)

    def _package_input_type_annotations(self, package_name: str | None) -> str:
        package = self.stylesheet._packages[package_name]
        return package.input_type_annotations or "unspecified"

    def _document_annotations(
        self,
        package_name: str | None,
        *,
        validation: str | None,
        type_name: str | None,
    ) -> str:
        if validation is not None or type_name is not None:
            return "preserve"
        return self._package_input_type_annotations(package_name)

    # -- accumulator state --------------------------------------------------

    def _mark_initial_selection(self) -> None:
        """Set accumulator applicability for the invocation's initial trees."""
        options = self.options
        if options.entry == "mode":
            selection = cast(XdmSequence, options.initial_match_selection)
            mode = options.mode
            keys = self.stylesheet._mode_accumulators.get(
                (self.package.name, None if mode is None else mode.name),
                frozenset(),
            )
            for item in selection:
                if isinstance(item, XdmNode):
                    self.accumulators.restrict(item.root, keys)
            return
        global_item = options.global_context_item
        if isinstance(global_item, XdmNode):
            self.accumulators.allow_all(global_item.root)

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
            tree = validate_delivered(
                tree,
                validation=self.package.default_validation or "strip",
                schema_pool=self._package_schema_pool(self.package.name),
            )
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
                context=EvaluationContext(final_output=False),
            )
        if options.entry == "template":
            assert options.template is not None
            global_item = options.global_context_item
            return self._execute_component(
                options.template,
                template_parameters=options.template_parameters,
                context=EvaluationContext(
                    focus=(Focus.absent() if global_item is None else Focus.singleton(global_item)),
                    tunnel_parameters=options.tunnel_parameters or {},
                ),
            )
        assert options.initial_match_selection is not _ABSENT
        selection = cast(XdmSequence, options.initial_match_selection)
        mode_name = options.mode.name if options.mode is not None else None
        self._check_typed_mode(selection, mode_name, self.package.name)
        return apply_templates(
            self,
            selection,
            mode_name,
            options.template_parameters,
            options.tunnel_parameters,
            package_name=self.package.name,
            mode_package=self.package.name,
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
        context: EvaluationContext,
        group: object = _INHERIT,
        grouping_key: object = _INHERIT,
    ) -> XdmSequence:
        frame = self._component_frame(
            component,
            arguments=arguments,
            template_parameters=template_parameters,
            context=context,
            group=group,
            grouping_key=grouping_key,
        )
        regex_scope = (
            self._regex_scope(None)
            if component.kind == "function"
            else self._regex_scope(self.current_regex_groups)
        )
        with regex_scope, self._merge_scope(None, None, ()):
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
        context: EvaluationContext,
        group: object = _INHERIT,
        grouping_key: object = _INHERIT,
    ) -> Frame:
        plan = self._component_plan(component)
        package_name = component.owner
        context_signature: ContextItemSignature | None = None
        if component.kind == "function":
            resolved_focus = Focus.absent()
        else:
            resolved_focus = context.focus
            context_signature = component.signature.context_item if component.signature else None
            resolved_focus = self._apply_context_item(context_signature, resolved_focus)
        effective_rule_context = context.rule_context
        if context_signature is not None and context_signature.use == "absent":
            effective_rule_context = None
        component_context = replace(
            context,
            focus=resolved_focus,
            locals={},
            package_name=package_name,
            original_functions={},
        )
        locals_, index = self._bind_component_parameters(
            component,
            plan.body,
            arguments=arguments,
            template_parameters=template_parameters,
            context=component_context,
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
                _TemplateContext(component)
                if plan.original_functions
                or (component.kind == "template" and effective_rule_context is not None)
                else component
            ),
            identity=self._component_identity(component),
            program=plan.body[index:],
            context=replace(
                component_context,
                locals=locals_,
                rule_context=effective_rule_context,
                original_functions=plan.original_functions,
            ),
            continuation=None,
            location=component.location,
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
        context: EvaluationContext,
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
                    context.tunnel_parameters.get(parameter.name)
                    if parameter.tunnel
                    else (template_parameters or {}).get(parameter.name)
                )
                value = (
                    supplied
                    if supplied is not None
                    else self._parameter_default(
                        parameter,
                        replace(context, locals=locals_),
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
                return step_apply(self, frame)
            if frame.kind == "iteration":
                return step_iterate(self, frame)
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

    @contextmanager
    def _regex_scope(self, groups: tuple[str, ...] | None, /) -> Iterator[None]:
        previous = self.current_regex_groups
        self.current_regex_groups = groups
        try:
            yield
        finally:
            self.current_regex_groups = previous

    @contextmanager
    def _merge_scope(
        self,
        group: Mapping[str, XdmSequence] | None,
        key: XdmSequence | None,
        sources: tuple[str, ...],
    ) -> Iterator[None]:
        previous = (
            self.current_merge_group,
            self.current_merge_key,
            self.current_merge_sources,
        )
        self.current_merge_group = group
        self.current_merge_key = key
        self.current_merge_sources = sources
        try:
            yield
        finally:
            (
                self.current_merge_group,
                self.current_merge_key,
                self.current_merge_sources,
            ) = previous

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
        if isinstance(plan, (_NextIterationPlan, _BreakPlan)):
            return iteration_control(self, plan, frame)
        if isinstance(plan, _ForEachPlan):
            return start_iteration_plan(self, plan, frame, cursor)
        if isinstance(plan, _IteratePlan):
            return start_iteration_plan(self, plan, frame, cursor)
        if isinstance(plan, _ForEachGroupPlan):
            return start_iteration_plan(self, plan, frame, cursor)
        if isinstance(plan, _PerformSortPlan):
            value = perform_sort(plan, self, frame.context)
            return Continue(replace(frame, cursor=cursor + 1, values=frame.values + tuple(value)))
        if isinstance(plan, _ParamPlan):
            raise XsltError(
                "xsl:param is only supported at the start of a template or function",
                code="XTDE0000",
            )
        if isinstance(plan, _VariablePlan):
            value = variable_value(plan, self, frame.context)
            return Continue(
                replace(
                    frame,
                    cursor=cursor + 1,
                    context=replace(
                        frame.context,
                        locals={**frame.context.locals, plan.name: value},
                    ),
                )
            )
        if isinstance(plan, _RestoreVariablesPlan):
            return Continue(
                replace(
                    frame,
                    cursor=cursor + 1,
                    context=replace(
                        frame.context,
                        locals=cast(Mapping[str, XdmSequence], plan.variables),
                    ),
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
                        _RestoreVariablesPlan(frame.context.locals),
                        *program[cursor + 1 :],
                    ),
                    cursor=0,
                )
            )
        if isinstance(plan, _ChoosePlan):
            branch = choose_branch(self, plan, frame.context)
            if branch is None:
                return Continue(replace(frame, cursor=cursor + 1))
            return Continue(
                replace(
                    frame,
                    program=(
                        *branch,
                        _RestoreVariablesPlan(frame.context.locals),
                        *program[cursor + 1 :],
                    ),
                    cursor=0,
                )
            )
        if isinstance(plan, _TryPlan):
            return try_step(self, plan, frame, cursor)
        if isinstance(plan, _CallTemplatePlan):
            return call_template_step(self, plan, frame, cursor)
        if isinstance(plan, _ApplyTemplatesPlan):
            return apply_templates_step(self, plan, frame, cursor)
        if isinstance(plan, _OverridingPlan):
            return overriding_step(self, plan, frame, cursor)
        try:
            value = self._execute_plan(plan, context=frame.context)
        except _IterationControlSignal as signal:
            control_frame = replace(
                frame,
                context=replace(
                    frame.context,
                    locals=signal.variables,
                    focus=signal.focus,
                ),
                values=frame.values + signal.pending,
            )
            return iteration_control(self, signal.plan, control_frame)
        return Continue(replace(frame, cursor=cursor + 1, values=frame.values + tuple(value)))

    # -- try/catch ---------------------------------------------------------

    # -- iteration, sorting, and grouping ---------------------------------

    # -- merging, splitting, and source documents -------------------------

    # -- sort and group algorithms ---------------------------------------

    def _start_expression(
        self,
        expression: CompiledExpression,
        frame: Frame,
        cursor: int,
    ) -> FrameResult:
        context = frame.context
        originals = context.original_functions
        functions: dict[tuple[str, int], HostFunction] = dict(
            self.stylesheet_functions(context.package_name)
        )
        for arity, component in originals.items():
            functions[(f"{{{XSLT_NS}}}original", arity)] = HostFunction(
                ComponentCall(component),
                name=f"{{{XSLT_NS}}}original",
                arity=arity,
                suspend=True,
            )
        machine: ContinuationMachine | None = None
        if not self._expression_references_global(expression, context.package_name):
            environment = self._environment(context)
            machine = self.xpath_machine(
                expression,
                environment.variables,
                functions,
            )
        if machine is None:
            value = self._evaluate(expression, context)
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
            context=EvaluationContext(
                focus=context.focus,
                package_name=context.package_name,
            ),
            location=getattr(expression.static_context, "location", None),
            group=frame.group,
            grouping_key=frame.grouping_key,
        )
        return Call(
            xpath_frame,
            resume=replace(frame, cursor=cursor + 1),
            tail=False,
        )

    def _plan_parameters(
        self,
        parameters: tuple[_WithParamPlan, ...],
        context: EvaluationContext,
    ) -> tuple[Mapping[str, XdmSequence], Mapping[str, XdmSequence]]:
        template: dict[str, XdmSequence] = {}
        tunnel: dict[str, XdmSequence] = {}
        for parameter in parameters:
            if parameter.expression is not None:
                value = self._evaluate(parameter.expression, context)
            elif parameter.children:
                value = self._execute_plans(
                    parameter.children,
                    context=replace(context, final_output=False),
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

    def _mode_layers(
        self,
        mode: str | None,
        mode_package: str | None,
    ) -> tuple[_ModeLayerPlan, ...]:
        package_modes = self.stylesheet._package_modes.get(
            mode_package, self.stylesheet._package_modes.get(self.package.name, {})
        )
        return package_modes.get(mode, ())

    def _dispatcher(self, mode: str | None, mode_package: str | None = None) -> RuleDispatcher:
        key = (mode, mode_package)
        dispatcher = self._mode_dispatchers.get(key)
        if dispatcher is None:
            layers = tuple(
                (layer_index, PatternIndex(layer.rules))
                for layer_index, layer in enumerate(self._mode_layers(mode, mode_package))
            )
            properties = self._mode_properties(mode, mode_package)
            dispatcher = RuleDispatcher(
                layers,
                on_multiple_match=properties.get("on-multiple-match", "use-last"),
            )
            self._mode_dispatchers[key] = dispatcher
        return dispatcher

    def _mode_properties(
        self,
        mode: str | None,
        mode_package: str | None = None,
    ) -> Mapping[str, str]:
        # A mode reference resolves in the package that owns the selected
        # mode.  A used package's private mode may reuse a name that the
        # using package also declares, so the mode's package wins over the
        # principal package.
        package = self.stylesheet._packages.get(
            mode_package, self.stylesheet._packages[self.package.name]
        )
        declaration = package.mode(mode)
        return {} if declaration is None else declaration.properties

    def _check_typed_mode(
        self,
        items: XdmSequence,
        mode: str | None,
        package_name: str | None,
    ) -> None:
        typed = self._mode_properties(mode, package_name).get("typed", "unspecified")
        if typed == "unspecified":
            return
        for item in items:
            if not isinstance(item, XdmNode) or item.kind not in (
                NodeKind.ELEMENT,
                NodeKind.ATTRIBUTE,
            ):
                continue
            untyped = item.type_name in (
                UNTYPED_TYPE,
                "{http://www.w3.org/2001/XMLSchema}untypedAtomic",
            )
            if typed in ("yes", "strict", "lax") and untyped:
                raise XsltDynamicError(
                    "an untyped node cannot be processed in a typed mode",
                    code="XTTE3100",
                )
            if typed == "no" and not untyped:
                raise XsltDynamicError(
                    "a typed node cannot be processed in an untyped mode",
                    code="XTTE3110",
                )

    def _pattern_environment(self, rule: TemplateRule) -> ExpressionEnvironment:
        body = cast(_BodyPlan, rule.template)
        return self._pattern_environment_for(rule.pattern, body.owner)

    def _pattern_environment_for(
        self,
        pattern: CompiledPattern,
        package_name: str | None,
        *,
        locals_: Mapping[str, XdmSequence] | None = None,
    ) -> ExpressionEnvironment:
        if locals_ is not None:
            return self._environment(
                EvaluationContext(locals=locals_, package_name=package_name),
                referenced=_pattern_variable_names(pattern),
                static_context=pattern.context,
                regex_captures=None,
            )
        key = id(pattern)
        environment = self._pattern_environments.get(key)
        if environment is None:
            environment = self._environment(
                EvaluationContext(package_name=package_name),
                referenced=_pattern_variable_names(pattern),
                static_context=pattern.context,
                regex_captures=None,
            )
            self._pattern_environments[key] = environment
        return environment

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
            context=EvaluationContext(final_output=False),
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

    def _coerce(
        self,
        value: XdmSequence,
        sequence_type: CompiledSequenceType | None,
        *,
        code: str,
        label: str,
    ) -> XdmSequence:
        """Apply one compiled sequence type through the invocation's coercion."""
        if sequence_type is None:
            return value
        return _coerce_sequence(value, sequence_type, code=code, label=label)

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
        context: EvaluationContext,
    ) -> XdmSequence:
        # Parameter defaults never inherit the caller's xsl:original functions
        # or current template rule; preserve that lexical boundary.
        default_context = replace(
            context,
            final_output=False,
            rule_context=None,
            original_functions={},
        )
        if parameter.expression is not None:
            return self._evaluate(parameter.expression, default_context)
        if parameter.children:
            if parameter.has_as:
                return self._execute_plans(parameter.children, context=default_context)
            sequence = self._execute_plans(parameter.children, context=default_context)
            return (_build_result_tree(sequence),)
        if parameter.required:
            raise XsltError(
                f"the required parameter {parameter.name!r} was not supplied",
                code="XTDE0700",
            )
        if parameter.has_as:
            return ()
        return (XdmAtomic.string(""),)

    def _outside_try_error(self, error: BaseException) -> BaseException:
        """Mark an error raised while initializing an outer variable.

        XSLT 3.0 section 8.3 excludes errors from evaluating references to
        variables declared outside the try.  Globals are lazy here, so the
        first reference may be the one that triggers their initializer.  A
        dynamic error escaping that initializer is wrapped as non-catchable;
        static errors and foreign Python failures keep their own identity.
        """
        if isinstance(error, XsltStaticError):
            return error
        if isinstance(error, XsltError):
            if not error.catchable:
                return error
            message = error.args[0] if error.args else str(error)
            wrapped = XsltDynamicError(
                message,
                code=error.code,
                phase=error.phase or "dynamic",
                uri=error.uri,
                module=error.module,
                package=error.package,
                location=error.location,
                stack=error.stack,
                cause=error,
                allow_catch=False,
                description=error.description,
                has_description=error.has_description,
                value=error.value,
                line_number=error.line_number,
                column_number=error.column_number,
            )
            wrapped.__cause__ = error
            wrapped.__suppress_context__ = True
            return wrapped
        if isinstance(error, (ElementPathError, XPathEvaluationError)):
            code = error_code_from_exception(error)
            if code is not None and is_static_error_code(code):
                return error
            return self.boundary_error(error, catchable=False)
        return error

    def _evaluate_global(self, component: Component) -> XdmSequence:
        """Evaluate one lazy global and mark escaping errors non-catchable."""
        try:
            return self._global_value(component)
        except BaseException as global_error:
            marked = self._outside_try_error(global_error)
            if marked is global_error:
                raise
            raise marked from global_error

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
            with self._group_scope(None, None), self._merge_scope(None, None, ()):
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
                context = EvaluationContext(
                    focus=focus,
                    locals=original_locals,
                    final_output=False,
                    package_name=component.owner,
                    original_functions=original_functions,
                )
                if plan.select is not None:
                    value = self._evaluate(plan.select, context)
                elif plan.body:
                    if plan.has_as:
                        value = self._execute_plans(plan.body, context=context)
                    else:
                        sequence = self._execute_plans(plan.body, context=context)
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
        context: EvaluationContext,
    ) -> XdmSequence:
        result: list[XdmItem] = []
        local_scope = dict(context.locals)
        try:
            for plan in plans:
                if isinstance(plan, _ParamPlan):
                    raise XsltError(
                        "xsl:param is only supported at the start of a template or function",
                        code="XTDE0000",
                    )
                if isinstance(plan, _VariablePlan):
                    local_scope[plan.name] = variable_value(
                        plan,
                        self,
                        replace(context, locals=local_scope),
                    )
                    continue
                result.extend(
                    self._execute_plan(
                        plan,
                        context=replace(context, locals=local_scope),
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
        context: EvaluationContext,
    ) -> XdmSequence:
        if isinstance(plan, _TextPlan):
            return (self._text_node(plan.value),)
        if isinstance(plan, (_NextIterationPlan, _BreakPlan)):
            active = getattr(self, "_active_iteration_frame", None)
            if active is not None and isinstance(active.iteration, IterationState):
                raise _IterationControlSignal(plan, context.locals, context.focus)
            raise XsltStaticError(
                f"xsl:{plan.instruction.local_name} requires an enclosing xsl:iterate",
                code="XTSE3120",
                uri=plan.instruction.module,
                location=plan.instruction.location,
            )
        if isinstance(plan, (_ForEachPlan, _IteratePlan, _ForEachGroupPlan)):
            return execute_iteration(plan, self, context=context)
        if isinstance(plan, _MergePlan):
            return execute_merge(plan, self, context=context)
        if isinstance(plan, _ForkPlan):
            return execute_fork(plan, self, context=context)
        if isinstance(plan, _SourceDocumentPlan):
            return execute_source_document(plan, self, context=context)
        if isinstance(plan, _PerformSortPlan):
            return perform_sort(plan, self, context)
        if isinstance(plan, _TvtPlan):
            return (self._text_node(self._value_template(plan.template, context)),)
        if isinstance(plan, _AttributePlan):
            return attribute_value(plan, self, context)
        if isinstance(plan, _SequencePlan):
            if plan.expression is not None:
                return self._evaluate(plan.expression, context)
            return self._execute_plans(plan.children, context=context)
        if isinstance(plan, _ValueOfPlan):
            return execute_value_of(plan, self, context)
        if isinstance(plan, _LiteralPlan):
            return (literal_element(plan, self, context),)
        if isinstance(plan, _ElementPlan):
            return (element_value(plan, self, context),)
        if isinstance(plan, _DocumentPlan):
            return (document_value(plan, self, context),)
        if isinstance(plan, _CopyPlan):
            return copy_value(plan, self, context)
        if isinstance(plan, _CopyOfPlan):
            return copy_of_value(plan, self, context)
        if isinstance(plan, _ChoosePlan):
            return execute_choose(plan, self, context)
        if isinstance(plan, _TryPlan):
            return execute_try(plan, self, context=context)
        if isinstance(plan, _MapPlan):
            return execute_map(plan, self, context)
        if isinstance(plan, _MapEntryPlan):
            return execute_map_entry(plan, self, context)
        if isinstance(plan, _EvaluatePlan):
            return execute_dynamic_evaluate(plan, self, context)
        if isinstance(plan, _AssertPlan):
            execute_assert(plan, self, context)
            return ()
        if isinstance(plan, _WherePopulatedPlan):
            return execute_where_populated(plan, self, context)
        if isinstance(plan, _ConditionalSequencePlan):
            return execute_conditional_sequence(plan, self, context)
        if isinstance(plan, _FallbackPlan):
            return ()
        if isinstance(plan, _ConcatPlan):
            return execute_concat(plan, self, context)
        if isinstance(plan, _CallTemplatePlan):
            return execute_call_template(plan, self, context)
        if isinstance(plan, _ApplyTemplatesPlan):
            return execute_apply_templates(plan, self, context)
        if isinstance(plan, _OverridingPlan):
            return execute_overriding(plan, self, context)
        if isinstance(plan, _ResultDocumentPlan):
            self._result_document(plan, context)
            return ()
        if isinstance(plan, _AnalyzeStringPlan):
            return execute_analyze_string(plan, self, context=context)
        if isinstance(plan, _NumberPlan):
            return execute_number(plan, self, context=context)
        if isinstance(plan, _UnsupportedPlan):
            raise XsltError(
                f"the instruction xsl:{plan.instruction.local_name} is not supported by the "
                "Task 16 execution slice",
                code=None,
            )
        raise TypeError(  # pragma: no cover - every _Plan arm is handled above
            f"unknown compiled plan {type(plan).__name__}"
        )

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
        context: EvaluationContext,
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
                    target = self._resolve_attribute_set(
                        reference,
                        component.owner or context.package_name,
                    )
                    attributes.extend(
                        self._attribute_set_attributes(
                            target,
                            replace(context, package_name=target.owner),
                        )
                    )
                for instruction in body:
                    attributes.extend(
                        cast(
                            Iterable[XdmNode],
                            self._execute_plan(
                                instruction,
                                context=replace(
                                    context,
                                    locals={},
                                    final_output=False,
                                    package_name=component.owner,
                                    mode=None,
                                    tunnel_parameters={},
                                    rule_context=None,
                                    original_functions={},
                                ),
                            ),
                        )
                    )
            return tuple(attributes)
        finally:
            self.evaluating_attribute_sets.pop()

    def _result_document(
        self,
        plan: _ResultDocumentPlan,
        context: EvaluationContext,
    ) -> None:
        if not context.final_output:
            raise XsltError(
                "xsl:result-document is not permitted in temporary output state",
                code="XTDE1480",
            )
        href = self._value_source(plan.href, context)
        method = self._value_source(plan.method, context) if plan.method is not None else None
        format_name = self._value_source(plan.format, context) if plan.format is not None else None
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
        evaluated = {name: self._value_source(value, context) for name, value in plan.attributes}
        effective_method = method or base_attributes.get("method", "xml")
        build_tree = (
            self._value_source(plan.build_tree, context)
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
        try:
            sequence = self._execute_plans(
                plan.children,
                context=replace(
                    context,
                    final_output=True,
                    rule_context=None,
                    original_functions={},
                ),
            )
            tree = None
            pool = self._package_schema_pool(context.package_name)
            if self._build_tree(build_tree, effective_method):
                tree = _build_result_tree(sequence, output_uri=uri)
                tree = validate_delivered(
                    tree,
                    validation=plan.validation,
                    type_name=plan.type_name,
                    schema_pool=pool,
                )
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
        except BaseException:
            self.final_uris.discard(uri)
            self.secondary.pop(uri, None)
            raise

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

    # -- expressions and values -------------------------------------------

    def _evaluate(
        self,
        expression: CompiledExpression,
        context: EvaluationContext,
    ) -> XdmSequence:
        engine = XPathEngine(expression.version)
        attempted: set[str] = set()
        while True:
            try:
                return engine.evaluate(
                    expression,
                    self._environment(context, expression=expression),
                    focus=context.focus,
                )
            except Exception as exc:
                if not str(getattr(exc, "code", "")).endswith("XPST0008"):
                    raise
                name = _unknown_variable_name(exc)
                component = self._global_component(context.package_name, name)
                if component is None or name in attempted:
                    raise
                attempted.add(name)
                self._evaluate_global(component)

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
        context: EvaluationContext,
        *,
        referenced: set[str] | None = None,
        expression: CompiledExpression | None = None,
        static_context: StaticContext | None = None,
        regex_captures: object | None = _INHERIT,
    ) -> ExpressionEnvironment:
        variables: dict[str, Any] = {}
        package_name = context.package_name
        locals_ = context.locals
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
                value = self._evaluate_global(component)
            self._bind(variables, component.name, value)
        for key, value in locals_.items():
            self._bind(variables, key, value)
        functions = dict(self.fallback_functions(package_name))
        if context.original_functions:
            for _arity, component in context.original_functions.items():
                functions[f"{{{XSLT_NS}}}original"] = self._component_dispatcher(
                    f"{{{XSLT_NS}}}original",
                    [component],
                )
        decimal_formats = self._decimal_format_objects(package_name)
        bridge = self._context_bridge(
            package_name,
            context.focus,
            expression,
            static_context,
            regex_captures=regex_captures,
            decimal_formats=decimal_formats,
        )
        overrides: dict[str, Any] = dict(bridge.function_overrides())
        return ExpressionEnvironment(
            variables=variables,
            functions=functions,
            function_overrides=overrides,
            decimal_formats={
                name: decimal_format_attributes(decimal_format)
                for name, decimal_format in decimal_formats.items()
            },
            current_datetime=self.current_datetime,
            implicit_timezone=self.implicit_timezone,
            base_uri=None,
        )

    def _decimal_format_objects(
        self,
        package_name: str | None,
    ) -> Mapping[str | None, DecimalFormat]:
        package = self.stylesheet._packages.get(package_name) or self.package
        definitions = {
            definition.name: dict(definition.attributes) for definition in package.decimal_formats
        }
        return effective_decimal_formats(definitions)

    def _decimal_formats(
        self,
        package_name: str | None,
    ) -> Mapping[str | None, Mapping[str, str]]:
        return {
            name: decimal_format_attributes(decimal_format)
            for name, decimal_format in self._decimal_format_objects(package_name).items()
        }

    def _context_bridge(
        self,
        package_name: str | None,
        focus: Focus,
        expression: CompiledExpression | None,
        static_context: StaticContext | None = None,
        *,
        regex_captures: object | None = _INHERIT,
        decimal_formats: Mapping[str | None, DecimalFormat] | None = None,
    ) -> XsltContextBridge:
        package = self.stylesheet._packages.get(package_name) or self.package
        static = (
            static_context
            if static_context is not None
            else (None if expression is None else expression.static_context)
        )
        captures: Any = (
            ABSENT
            if regex_captures is not _INHERIT or self.current_regex_groups is None
            else tuple(self.current_regex_groups)
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
            merge_group=(ABSENT if self.current_merge_group is None else self.current_merge_group),
            merge_sources=self.current_merge_sources,
            merge_key=(ABSENT if self.current_merge_key is None else tuple(self.current_merge_key)),
            regex_captures=captures,
            decimal_formats=(
                decimal_formats
                if decimal_formats is not None
                else self._decimal_format_objects(package_name)
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
                with self._group_scope(None, None), self._merge_scope(None, None, ()):
                    values = self._evaluate(
                        definition.use,
                        EvaluationContext(
                            focus=Focus.singleton(node),
                            package_name=package_name,
                        ),
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
            context=EvaluationContext(final_output=False),
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
        context: EvaluationContext,
    ) -> str:
        if isinstance(source, str):
            return source
        return self._value_template(source, context)

    def _value_template(
        self,
        template: CompiledValueTemplate,
        context: EvaluationContext,
    ) -> str:
        parts: list[str] = []
        for part in template.parts:
            if part.kind == "text":
                parts.append(part.text)
                continue
            assert part.expression is not None
            values = self._evaluate(
                part.expression,
                replace(context, original_functions={}),
            )
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
