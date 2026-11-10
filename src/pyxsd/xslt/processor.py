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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast
from urllib.parse import urljoin, urlsplit

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
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.continuation import ContinuationMachine, Suspension
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus
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
from pyxsd.xslt.compiler.instructions import Instruction, TextNode
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
from pyxsd.xslt.compiler.patterns import compile_pattern
from pyxsd.xslt.compiler.static import EffectiveModule, StaticPreprocessor
from pyxsd.xslt.errors import XsltError, XsltStaticError
from pyxsd.xslt.results import (
    ResultEntry,
    ResultShapeError,
    TransformationResult,
    _append_complex_content,
    _build_result_tree,
    _NodeFactory,
)
from pyxsd.xslt.runtime.frames import (
    NO_INPUT,
    Call,
    Continue,
    Frame,
    FrameResult,
    Return,
)
from pyxsd.xslt.runtime.templates import PatternIndex, TemplateRule
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

_ABSENT = object()
_AbsentType = object
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


@dataclass(frozen=True, slots=True)
class _LiteralPlan:
    instruction: Instruction
    attributes: tuple[tuple[str, str | CompiledValueTemplate], ...]
    children: tuple[_Plan, ...]
    namespaces: tuple[tuple[str, str], ...]


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
    tail: bool = False


@dataclass(frozen=True, slots=True)
class _ChoosePlan:
    instruction: Instruction
    branches: tuple[tuple[CompiledExpression, tuple[_Plan, ...]], ...]
    otherwise: tuple[_Plan, ...] | None


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
    | _WithParamPlan
    | _CallTemplatePlan
    | _ApplyTemplatesPlan
    | _ChoosePlan
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


@dataclass(frozen=True, slots=True)
class _ApplyState:
    items: XdmSequence
    mode: str | None
    template_parameters: Mapping[str, XdmSequence]
    tunnel_parameters: Mapping[str, XdmSequence]
    index: int = 0


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

    def __post_init__(self) -> None:
        if self.language != "3.0":
            raise ValueError(f"unsupported XSLT language profile {self.language!r}")
        if self.xpath != "3.1":
            raise ValueError("XSLT 3.0 requires XPath 3.1")
        if not isinstance(self.schema_aware, bool):
            raise TypeError("schema_aware must be a boolean")
        if self.default_collation is not None and not isinstance(self.default_collation, str):
            raise TypeError("default_collation must be a string or None")
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
            select_source = attributes.attribute("select")
            if select_source is not None and component.kind != "variable":
                raise XsltStaticError(
                    "select is not permitted on this component declaration",
                    code="XTSE0010",
                    uri=module.identity,
                    location=component.location,
                )
            select = (
                compile_xpath(select_source, context, location=component.location)
                if select_source is not None
                else None
            )
            body = _mark_tail(_compile_sequence(component.body.children, context, module))
            if select is not None and _has_plan_content(body):
                raise XsltStaticError(
                    "a variable with a select attribute must have empty content",
                    code="XTSE0620",
                    uri=module.identity,
                    location=component.location,
                )
            as_source = attributes.attribute("as")
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
                )
            )

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
    )


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
    context = context.with_namespaces(declaration.namespaces)
    if declaration.base_uri is not None:
        context = context.with_base_uri(declaration.base_uri)
    if module.identity is not None:
        context = context.with_uri(module.identity)
    context = context.with_location(declaration.location)
    expand_text = declaration.attribute("expand-text")
    if expand_text is not None:
        context = context.with_expand_text(
            _yes_no(expand_text, declaration, "expand-text", context)
        )
    return context


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
    return tuple(plans)


def _compile_with_params(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> tuple[_WithParamPlan, ...]:
    result: list[_WithParamPlan] = []
    for child in instruction.children:
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
        result.append(
            _WithParamPlan(
                name=_expanded_name(name, child.namespaces),
                tunnel=_attribute_yes_no(child, "tunnel", context),
                expression=(
                    compile_xpath(select, context, location=child.location)
                    if select is not None
                    else None
                ),
                children=children,
                has_as=as_source is not None,
                sequence_type=(
                    compile_sequence_type(as_source, context, location=child.location)
                    if as_source is not None
                    else None
                ),
            )
        )
    return tuple(result)


def _instruction_mode(instruction: Instruction) -> str | None:
    value = instruction.attribute("mode")
    if value is None or value.strip() == "#unnamed":
        return None
    normalized = value.strip()
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
    if isinstance(plan, (_CallTemplatePlan, _ApplyTemplatesPlan)):
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


def _compile_instruction(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    exclusions = _exclusions(module)
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
        )
    local = instruction.local_name
    if local == "sequence":
        select = instruction.attribute("select")
        children = _compile_sequence(instruction.children, context, module)
        if select is not None and _has_plan_content(children):
            raise XsltStaticError(
                "xsl:sequence with a select attribute must have empty content",
                code="XTSE0010",
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
        return _ApplyTemplatesPlan(
            instruction=instruction,
            expression=expression,
            mode=_instruction_mode(instruction),
            parameters=_compile_with_params(instruction, context, module),
        )
    if local == "with-param":
        raise XsltStaticError(
            "xsl:with-param is only permitted inside a call or apply instruction",
            code="XTSE0010",
            uri=context.uri,
            location=location,
        )
    select = instruction.attribute("select")
    if select is not None:
        compile_xpath(select, context, location=location)
    test = instruction.attribute("test")
    if test is not None:
        compile_xpath(test, context, location=location)
    _compile_sequence(instruction.children, context, module)
    return _UnsupportedPlan(instruction)


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


def _has_plan_content(plans: Iterable[_Plan]) -> bool:
    for plan in plans:
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


def _effective_boolean(sequence: XdmSequence) -> bool:
    """Evaluate an XDM effective boolean value for control-flow tests."""
    if not sequence:
        return False
    first = sequence[0]
    if isinstance(first, XdmNode):
        return True
    if isinstance(first, XdmAtomic):
        try:
            return bool(first.cast(f"{{{_XSD_NS}}}boolean").value)
        except (TypeError, ValueError):
            return first.lexical != ""
    raise XsltError(
        "the effective boolean value of a function item is not defined",
        code="FORG0006",
    )


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
        self._mode_index_cache: dict[str | None, tuple[tuple[str | None, PatternIndex], ...]] = {}

    # -- top level ---------------------------------------------------------

    def run(self) -> TransformationResult:
        for component in self.stylesheet._globals:
            self._global_value(component)
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
    ) -> XdmSequence:
        frame = self._component_frame(
            component,
            arguments=arguments,
            template_parameters=template_parameters,
            tunnel_parameters=tunnel_parameters,
            final_output=final_output,
            focus=focus,
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
    ) -> Frame:
        plan = self._component_plan(component)
        package_name = component.owner
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
        locals_, index = self._bind_component_parameters(
            component,
            plan.body,
            arguments=arguments,
            template_parameters=template_parameters,
            tunnel_parameters=tunnel_parameters,
            focus=resolved_focus,
            package_name=package_name,
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
        return Frame(
            kind="component",
            component=component,
            identity=self._component_identity(component),
            program=plan.body[index:],
            variables=locals_,
            focus=resolved_focus,
            continuation=None,
            package=package_name,
            location=component.location,
            final_output=final_output,
            conversion=conversion,
        )

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
                    else self._parameter_default(parameter, focus, locals_, package_name)
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
        if frame.kind == "xpath":
            return self._step_xpath(frame)
        if frame.kind == "apply":
            return self._step_apply(frame)
        return self._step_instruction(frame)

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
        if isinstance(plan, _ParamPlan):
            raise XsltError(
                "xsl:param is only supported at the start of a template or function",
                code="XTDE0000",
            )
        if isinstance(plan, _SequencePlan):
            if plan.expression is not None:
                return self._start_expression(plan.expression, frame, cursor)
            return Continue(
                replace(
                    frame,
                    program=plan.children + program[cursor + 1 :],
                    cursor=0,
                )
            )
        if isinstance(plan, _ChoosePlan):
            branch = self._choose_branch(plan, frame)
            if branch is None:
                return Continue(replace(frame, cursor=cursor + 1))
            return Continue(replace(frame, program=branch + program[cursor + 1 :], cursor=0))
        if isinstance(plan, _CallTemplatePlan):
            return self._call_template_step(plan, frame, cursor)
        if isinstance(plan, _ApplyTemplatesPlan):
            return self._apply_templates_step(plan, frame, cursor)
        value = self._execute_plan(
            plan,
            focus=frame.focus,
            locals_=self._frame_variables(frame),
            final_output=frame.final_output,
            package_name=frame.package,
        )
        return Continue(replace(frame, cursor=cursor + 1, values=frame.values + tuple(value)))

    def _start_expression(
        self,
        expression: CompiledExpression,
        frame: Frame,
        cursor: int,
    ) -> FrameResult:
        environment = self._environment(self._frame_variables(frame), frame.package)
        machine = self.xpath_machine(expression, environment.variables, self.stylesheet_functions())
        if machine is None:
            value = self._evaluate(
                expression, frame.focus, self._frame_variables(frame), frame.package
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
    ) -> tuple[_Plan, ...] | None:
        for test, branch in plan.branches:
            if _effective_boolean(
                self._evaluate(test, frame.focus, self._frame_variables(frame), frame.package)
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
        )

    def _plan_parameters_for(
        self,
        parameters: tuple[_WithParamPlan, ...],
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
    ) -> tuple[Mapping[str, XdmSequence], Mapping[str, XdmSequence]]:
        template: dict[str, XdmSequence] = {}
        tunnel: dict[str, XdmSequence] = {}
        for parameter in parameters:
            if parameter.expression is not None:
                value = self._evaluate(parameter.expression, focus, locals_, package_name)
            elif parameter.children:
                value = self._execute_plans(
                    parameter.children,
                    focus=focus,
                    locals_=locals_,
                    final_output=False,
                    package_name=package_name,
                )
            else:
                value = ()
            (tunnel if parameter.tunnel else template)[parameter.name] = value
        return MappingProxyType(template), MappingProxyType(tunnel)

    def _resolve_template(self, reference: Any) -> Component:
        if getattr(reference, "original", False):
            target = self.package.original_target(
                reference.name,
                kind="template",
            )
            if target is not None:
                return target
        component = self.package.component("template", reference.name)
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
    ) -> FrameResult:
        template, tunnel = self._plan_parameters(plan.parameters, frame)
        component = self._resolve_template(plan.reference)
        child = self._component_frame(
            component,
            template_parameters=template,
            tunnel_parameters=tunnel,
            final_output=frame.final_output,
            focus=frame.focus,
        )
        return Call(
            child,
            resume=replace(frame, cursor=cursor + 1),
            tail=plan.tail,
        )

    def _resolve_mode(self, mode: str | None, current: str | None) -> str | None:
        if mode == "#current":
            return current
        if mode == "#default":
            return self.package.default_mode
        return mode

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
        template, tunnel = self._plan_parameters(plan.parameters, frame)
        child = self._apply_frame(
            items,
            self._resolve_mode(plan.mode, frame.mode),
            template,
            tunnel,
            tail=plan.tail,
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
    ) -> Frame:
        state = _ApplyState(
            items=items,
            mode=mode,
            template_parameters=template_parameters,
            tunnel_parameters=tunnel_parameters,
        )
        return Frame(
            kind="apply",
            identity=f"apply-templates mode={mode!r}",
            program=state,
            mode=mode,
            tail=tail,
        )

    def _mode_indexes(
        self,
        mode: str | None,
    ) -> tuple[tuple[str | None, PatternIndex], ...]:
        indexes = self._mode_index_cache.get(mode)
        if indexes is None:
            layers = self.stylesheet._modes.get(mode, ())
            indexes = tuple((layer.owner, PatternIndex(layer.rules)) for layer in layers)
            self._mode_index_cache[mode] = indexes
        return indexes

    def _rule_frame(
        self,
        rule: TemplateRule,
        focus: Focus,
        template_parameters: Mapping[str, XdmSequence],
        tunnel_parameters: Mapping[str, XdmSequence],
    ) -> Frame:
        body = cast(_BodyPlan, rule.template)
        rule_focus = self._apply_context_item(body.context_item, focus)
        locals_, index = self._bind_template_parameters(
            body.body,
            template_parameters,
            tunnel_parameters,
            rule_focus,
            body.owner,
        )
        conversion = None
        if body.result_type is not None:
            conversion = lambda value: _coerce_sequence(  # noqa: E731
                cast(XdmSequence, value),
                body.result_type,
                code="XTTE0505",
                label="template rule result",
            )
        return Frame(
            kind="rule",
            component=rule,
            identity=f"rule {rule.name or rule.pattern}",
            program=body.body[index:],
            variables=locals_,
            focus=rule_focus,
            package=body.owner,
            mode=rule.mode,
            location=rule.pattern.context.location,
            final_output=True,
            conversion=conversion,
        )

    def _bind_template_parameters(
        self,
        body: tuple[_Plan, ...],
        template_parameters: Mapping[str, XdmSequence],
        tunnel_parameters: Mapping[str, XdmSequence],
        focus: Focus,
        package_name: str | None,
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
                else self._parameter_default(parameter, focus, locals_, package_name)
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
        rule = self._select_rule(self._mode_indexes(state.mode), item, state.mode)
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
        )
        return Call(rule_frame, resume=resume, tail=tail)

    def _builtin_step(
        self,
        item: XdmItem,
        state: _ApplyState,
        resume: Frame,
        tail: bool,
    ) -> FrameResult | None:
        if isinstance(item, XdmNode):
            if item.kind in (NodeKind.DOCUMENT, NodeKind.ELEMENT):
                if not item.children:
                    return None
                child = self._apply_frame(
                    item.children,
                    state.mode,
                    MappingProxyType({}),
                    state.tunnel_parameters,
                    tail=tail,
                )
                return Call(child, resume=resume, tail=tail)
            if item.kind in (NodeKind.TEXT, NodeKind.ATTRIBUTE):
                return Continue(
                    replace(
                        resume,
                        values=(*resume.values, self._text_node(item.string_value)),
                    )
                )
            return None
        return Continue(
            replace(
                resume,
                values=(*resume.values, self._text_node(self._string_value(item))),
            )
        )

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
                )
            sequence = self._execute_plans(
                parameter.children,
                focus=focus,
                locals_=locals_,
                final_output=False,
                package_name=package_name,
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
            declaration = component.declaration
            plan = self._component_plan(component)
            owner_package = self.stylesheet._packages.get(component.owner)
            if plan.select is not None and owner_package is not None:
                for name in _referenced_variables(plan.select.source):
                    referenced = owner_package.variable(name)
                    if referenced is not None:
                        self._global_value(referenced)
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
                value = self._evaluate(plan.select, focus, {}, component.owner)
            elif plan.body:
                if plan.has_as:
                    value = self._execute_plans(
                        plan.body,
                        focus=focus,
                        locals_={},
                        final_output=False,
                        package_name=component.owner,
                    )
                else:
                    sequence = self._execute_plans(
                        plan.body,
                        focus=focus,
                        locals_={},
                        final_output=False,
                        package_name=component.owner,
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
    ) -> XdmSequence:
        result: list[XdmItem] = []
        local_scope = dict(locals_)
        for plan in plans:
            if isinstance(plan, _ParamPlan):
                raise XsltError(
                    "xsl:param is only supported at the start of a template or function",
                    code="XTDE0000",
                )
            result.extend(
                self._execute_plan(
                    plan,
                    focus=focus,
                    locals_=local_scope,
                    final_output=final_output,
                    package_name=package_name,
                )
            )
        return tuple(result)

    def _execute_plan(
        self,
        plan: _Plan,
        *,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
        package_name: str | None,
    ) -> XdmSequence:
        if isinstance(plan, _TextPlan):
            return (self._text_node(plan.value),)
        if isinstance(plan, _TvtPlan):
            return (
                self._text_node(self._value_template(plan.template, focus, locals_, package_name)),
            )
        if isinstance(plan, _SequencePlan):
            if plan.expression is not None:
                return self._evaluate(plan.expression, focus, locals_, package_name)
            return self._execute_plans(
                plan.children,
                focus=focus,
                locals_=locals_,
                final_output=final_output,
                package_name=package_name,
            )
        if isinstance(plan, _ValueOfPlan):
            if plan.expression is not None:
                values = self._evaluate(plan.expression, focus, locals_, package_name)
            else:
                values = self._execute_plans(
                    plan.children,
                    focus=focus,
                    locals_=locals_,
                    final_output=final_output,
                    package_name=package_name,
                )
            separator = self._value_source(plan.separator, focus, locals_, package_name)
            text = _simple_content_string(values, separator)
            return (self._text_node(text),)
        if isinstance(plan, _LiteralPlan):
            return (self._literal_element(plan, focus, locals_, final_output, package_name),)
        if isinstance(plan, _ChoosePlan):
            branch = None
            for test, candidate in plan.branches:
                if _effective_boolean(self._evaluate(test, focus, locals_, package_name)):
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
            )
        if isinstance(plan, _CallTemplatePlan):
            template, tunnel = self._plan_parameters_for(
                plan.parameters, focus, locals_, package_name
            )
            component = self._resolve_template(plan.reference)
            return self._execute_component(
                component,
                template_parameters=template,
                tunnel_parameters=tunnel,
                final_output=final_output,
                focus=focus,
            )
        if isinstance(plan, _ApplyTemplatesPlan):
            items = self._evaluate(plan.expression, focus, locals_, package_name)
            template, tunnel = self._plan_parameters_for(
                plan.parameters, focus, locals_, package_name
            )
            return self._apply_templates(
                items,
                self._resolve_mode(plan.mode, None),
                template,
                tunnel,
            )
        if isinstance(plan, _ResultDocumentPlan):
            self._result_document(plan, focus, locals_, final_output, package_name)
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

    def _literal_element(
        self,
        plan: _LiteralPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
        package_name: str | None,
    ) -> XdmNode:
        factory = _NodeFactory()
        attributes = tuple(
            (name, self._value_source(value, focus, locals_, package_name))
            for name, value in plan.attributes
        )
        element = factory.element(
            plan.instruction.name,
            attributes=attributes,
            namespaces=dict(plan.namespaces),
            base_uri=plan.instruction.base_uri,
        )
        content = self._execute_plans(
            plan.children,
            focus=focus,
            locals_=locals_,
            final_output=final_output,
            package_name=package_name,
        )
        _append_complex_content(factory, element, content, allow_attributes=True)
        return element

    def _result_document(
        self,
        plan: _ResultDocumentPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
        package_name: str | None,
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
    ) -> XdmSequence:
        frame = self._apply_frame(
            items,
            mode,
            template_parameters,
            tunnel_parameters,
        )
        return cast(XdmSequence, self.execute_frame(frame))

    def _select_rule(
        self,
        indexes: tuple[tuple[str | None, PatternIndex], ...],
        item: XdmItem,
        mode: str | None,
    ) -> TemplateRule | None:
        for layer_index, (owner, index) in enumerate(indexes):
            environment = self._environment({}, owner)
            rule = index.select(
                item,
                mode=mode,
                package_layer=layer_index,
                environment=environment,
            )
            if rule is not None:
                return rule
        return None

    # -- expressions and values -------------------------------------------

    def _evaluate(
        self,
        expression: CompiledExpression,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
    ) -> XdmSequence:
        engine = XPathEngine(expression.version)
        return engine.evaluate(
            expression,
            self._environment(locals_, package_name),
            focus=focus,
        )

    def _environment(
        self,
        locals_: Mapping[str, XdmSequence],
        package_name: str | None,
    ) -> ExpressionEnvironment:
        variables: dict[str, Any] = {}
        for component in self.stylesheet._package_variables.get(package_name, ()):
            assert component.name is not None
            value = self.globals.get((component.owner, component.name))
            if value is not None:
                self._bind(variables, component.name, value)
        for key, value in locals_.items():
            self._bind(variables, key, value)
        return ExpressionEnvironment(
            variables=variables,
            current_datetime=self.current_datetime,
            implicit_timezone=self.implicit_timezone,
            base_uri=None,
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
            parts.append(" ".join(self._string_value(item) for item in values))
        return "".join(parts)

    def _string_value(self, item: XdmItem) -> str:
        return _item_string(item)


_VARIABLE_REFERENCE = re.compile(r"\$([A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?)")


def _referenced_variables(source: str) -> tuple[str, ...]:
    return tuple(_VARIABLE_REFERENCE.findall(source))


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
