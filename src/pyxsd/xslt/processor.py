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
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast
from urllib.parse import urljoin, urlsplit

from pyxsd.document import Document
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.schema import Schema
from pyxsd.xdm import XdmAtomic, XdmItem, XdmSequence, as_xdm
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus
from pyxsd.xslt.compiler.context import (
    ProfileOptions,
    StaticContext,
    StaticFunction,
    StaticVariable,
)
from pyxsd.xslt.compiler.expressions import (
    CompiledValueTemplate,
    compile_avt,
    compile_tvt,
    compile_xpath,
)
from pyxsd.xslt.compiler.instructions import Instruction, TextNode
from pyxsd.xslt.compiler.modules import ModuleLoader, SourceModule
from pyxsd.xslt.compiler.packages import (
    CompiledPackage,
    Component,
    ComponentKey,
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
from pyxsd.xslt.runtime.templates import PatternIndex, TemplateRule

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
    expression: CompiledExpression
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
    | _ResultDocumentPlan
    | _UnsupportedPlan
)


@dataclass(frozen=True, slots=True)
class _BodyPlan:
    context: StaticContext
    body: tuple[_Plan, ...]


@dataclass(frozen=True, slots=True)
class _ComponentPlan:
    component: Component
    context: StaticContext
    select: CompiledExpression | None
    body: tuple[_Plan, ...]
    has_as: bool


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
    _components: Mapping[ComponentKey, _ComponentPlan]
    _modes: Mapping[ModeKey, tuple[tuple[TemplateRule, ...], ...]]
    _globals: tuple[Component, ...]
    _static_variables: Mapping[str, XdmSequence]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_components", MappingProxyType(dict(self._components)))
        object.__setattr__(self, "_modes", MappingProxyType(dict(self._modes)))
        object.__setattr__(
            self, "static_parameters", MappingProxyType(dict(self.static_parameters))
        )
        object.__setattr__(
            self, "_static_variables", MappingProxyType(dict(self._static_variables))
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
        return sum(len(layer) for layer in self._modes.get(name, ()))

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
        return _Invocation(self, options).run()


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
    profile = processor._profile()
    collation = processor.default_collation
    components: dict[ComponentKey, _ComponentPlan] = {}
    modes: dict[ModeKey, tuple[tuple[TemplateRule, ...], ...]] = {}
    for component in package.components.values():
        module = _component_module(component, modules, principal)
        context = _context_for(
            module,
            component.declaration,
            package,
            schema,
            profile,
            collation,
            include_package_symbols=component.owner == package.name,
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
        body = _compile_sequence(
            component.body.children,
            context,
            module,
        )
        if select is not None and _has_plan_content(body):
            raise XsltStaticError(
                "a variable with a select attribute must have empty content",
                code="XTSE0620",
                uri=module.identity,
                location=component.location,
            )
        components[(component.kind, component.name, component.arity)] = _ComponentPlan(
            component=component,
            context=context,
            select=select,
            body=body,
            has_as=attributes.attribute("as") is not None,
        )

    for mode_name, mode in package.modes.items():
        layers: list[tuple[TemplateRule, ...]] = []
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
                context = _context_for(
                    module,
                    rule.declaration,
                    package,
                    schema,
                    profile,
                    collation,
                    include_package_symbols=rule.owner == package.name,
                )
                pattern = compile_pattern(rule.pattern, context, location=rule.location)
                body = _compile_sequence(rule.body.children, context, module)
                rules.append(
                    TemplateRule(
                        pattern=pattern,
                        mode=mode_name,
                        package_layer=layer_index,
                        precedence=rule.precedence,
                        declaration_order=rule.declaration_order,
                        priority=rule.priority,
                        name=rule.name,
                        template=_BodyPlan(context=context, body=body),
                        module=rule.module,
                    )
                )
            layers.append(tuple(rules))
        modes[mode_name] = tuple(layers)

    globals_ = tuple(
        sorted(
            (
                component
                for component in package.components.values()
                if component.kind == "variable" and not component.hidden
            ),
            key=lambda component: component.declaration_order,
        )
    )
    static_variables: dict[str, XdmSequence] = {}
    for module in package.modules:
        for name, value in module.static_variables.items():
            static_variables.setdefault(name, value)
    return CompiledStylesheet(
        processor=processor,
        package=package,
        schema_pool=schema,
        profile=profile,
        static_parameters=static_parameters,
        _components=components,
        _modes=modes,
        _globals=globals_,
        _static_variables=static_variables,
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
    *,
    include_package_symbols: bool,
) -> StaticContext:
    base = StaticContext.for_module(module, schema=schema, profile=profile)
    variables = dict(base.variables)
    functions = dict(base.functions)
    for component in package.components.values() if include_package_symbols else ():
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


def _compile_sequence(
    children: Iterable[Any],
    context: StaticContext,
    module: EffectiveModule,
) -> tuple[_Plan, ...]:
    plans: list[_Plan] = []
    for child in children:
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
        return _SequencePlan(
            instruction=instruction,
            expression=(
                compile_xpath(select, context, location=location) if select is not None else None
            ),
            children=_compile_sequence(instruction.children, context, module),
        )
    if local == "value-of":
        select = instruction.attribute("select")
        if select is None:
            raise XsltStaticError(
                "xsl:value-of requires a select attribute",
                code="XTSE0010",
                uri=context.uri,
                location=location,
            )
        separator = instruction.attribute("separator")
        return _ValueOfPlan(
            instruction=instruction,
            expression=compile_xpath(select, context, location=location),
            separator=(
                compile_avt(separator, context, location=location) if separator is not None else " "
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
        return _ParamPlan(
            instruction=instruction,
            name=_expanded_name(name, instruction.namespaces),
            required=required,
            tunnel=tunnel,
            expression=(
                compile_xpath(select, context, location=location) if select is not None else None
            ),
            children=children,
            has_as=instruction.attribute("as") is not None,
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
        if prefix == "" and "#default" in excluded:
            continue
        if exclude_all:
            if uri not in used:
                continue
        elif prefix in excluded:
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
    parameters: Mapping[str, XdmSequence]
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
    global_parameters = _parameter_sequences(package, parameters)

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
            expanded = _expanded_name(initial_function, package.namespaces)
            if any(
                candidate.kind == "function"
                and candidate.name == expanded
                and not candidate.hidden
                and candidate.visibility in ("public", "final")
                for candidate in package.components.values()
            ):
                raise ValueError(
                    "initial_function_arguments do not match the initial function arity"
                )
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


def _require_absolute(uri: str, label: str) -> str:
    if not isinstance(uri, str) or not uri or not urlsplit(uri).scheme:
        raise XsltInvocationError(f"{label} must be a non-empty absolute URI, got {uri!r}")
    return uri


# ---------------------------------------------------------------------------
# Invocation execution


class _Invocation:
    """One transformation's complete dynamic state."""

    def __init__(self, stylesheet: CompiledStylesheet, options: _InvocationOptions) -> None:
        self.stylesheet = stylesheet
        self.package = stylesheet.package
        self.options = options
        self.globals: dict[str, XdmSequence] = {}
        self._evaluating: list[str] = []
        self.secondary: dict[str, ResultEntry] = {}
        self.final_uris: set[str] = set()
        if options.output_uri is not None:
            self.final_uris.add(options.output_uri)
        base = ExpressionEnvironment()
        self.current_datetime = base.current_datetime
        self.implicit_timezone = base.implicit_timezone
        self.output_attributes = self._output_attributes()

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

    def _output_attributes(self) -> Mapping[str, str]:
        output = self.package.output(None)
        if output is None:
            return MappingProxyType({})
        return MappingProxyType(dict(output.attributes))

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
        return self.stylesheet._components[(component.kind, component.name, component.arity)]

    def _execute_component(
        self,
        component: Component,
        *,
        arguments: tuple[XdmSequence, ...] = (),
        template_parameters: Mapping[str, XdmSequence] | None = None,
        tunnel_parameters: Mapping[str, XdmSequence] | None = None,
        final_output: bool,
    ) -> XdmSequence:
        plan = self._component_plan(component)
        if component.kind == "function":
            focus = Focus.absent()
        elif self.options.global_context_item is not None:
            focus = Focus.singleton(self.options.global_context_item)
        else:
            focus = Focus.absent()
        locals_: dict[str, XdmSequence] = {}
        body = plan.body
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
                    else self._parameter_default(parameter, focus, locals_)
                )
            self._bind(locals_, parameter.name, value)
            index += 1
        return self._execute_plans(
            body[index:], focus=focus, locals_=locals_, final_output=final_output
        )

    def _parameter_default(
        self,
        parameter: _ParamPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
    ) -> XdmSequence:
        if parameter.expression is not None:
            return self._evaluate(parameter.expression, focus, locals_)
        if parameter.children:
            if parameter.has_as:
                return self._execute_plans(
                    parameter.children,
                    focus=focus,
                    locals_=locals_,
                    final_output=False,
                )
            sequence = self._execute_plans(
                parameter.children,
                focus=focus,
                locals_=locals_,
                final_output=False,
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
        key = component.name
        cached = self.globals.get(key)
        if cached is not None:
            return cached
        if key in self._evaluating:
            raise XsltError(
                f"circular dependency in the global variable {key!r}",
                code="XTDE0640",
            )
        self._evaluating.append(key)
        try:
            declaration = component.declaration
            plan = self._component_plan(component)
            if plan.select is not None:
                for name in _referenced_variables(plan.select.source):
                    referenced = self.package.variable(name)
                    if referenced is not None:
                        self._global_value(referenced)
            static = declaration.attribute("static")
            if static is not None and _yes_no_runtime(static):
                static_value = self.stylesheet._static_variables[key]
                assert static_value is not None
                self.globals[key] = static_value
                return static_value
            is_param = declaration.local_name == "param"
            if is_param and key in self.options.parameters:
                value = self.options.parameters[key]
                self.globals[key] = value
                return value
            if is_param and _yes_no_runtime(declaration.attribute("required")):
                raise XsltError(
                    f"the required global parameter {key!r} was not supplied",
                    code="XTDE0050",
                )
            if plan.select is not None:
                value = self._evaluate(plan.select, self._global_focus(component), {})
            elif plan.body:
                if plan.has_as:
                    value = self._execute_plans(
                        plan.body,
                        focus=self._global_focus(component),
                        locals_={},
                        final_output=False,
                    )
                else:
                    sequence = self._execute_plans(
                        plan.body,
                        focus=self._global_focus(component),
                        locals_={},
                        final_output=False,
                    )
                    value = (_build_result_tree(sequence),)
            elif plan.has_as:
                value = ()
            else:
                value = (XdmAtomic.string(""),)
            self.globals[key] = value
            return value
        finally:
            self._evaluating.pop()

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
                    plan, focus=focus, locals_=local_scope, final_output=final_output
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
    ) -> XdmSequence:
        if isinstance(plan, _TextPlan):
            return (self._text_node(plan.value),)
        if isinstance(plan, _TvtPlan):
            return (self._text_node(self._value_template(plan.template, focus, locals_)),)
        if isinstance(plan, _SequencePlan):
            if plan.expression is not None:
                return self._evaluate(plan.expression, focus, locals_)
            return self._execute_plans(
                plan.children, focus=focus, locals_=locals_, final_output=final_output
            )
        if isinstance(plan, _ValueOfPlan):
            values = self._evaluate(plan.expression, focus, locals_)
            separator = self._value_source(plan.separator, focus, locals_)
            text = separator.join(self._string_value(item) for item in values)
            return (self._text_node(text),)
        if isinstance(plan, _LiteralPlan):
            return (self._literal_element(plan, focus, locals_),)
        if isinstance(plan, _ResultDocumentPlan):
            self._result_document(plan, focus, locals_, final_output)
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
    ) -> XdmNode:
        factory = _NodeFactory()
        attributes = tuple(
            (name, self._value_source(value, focus, locals_)) for name, value in plan.attributes
        )
        element = factory.element(
            plan.instruction.name,
            attributes=attributes,
            namespaces=dict(plan.namespaces),
            base_uri=plan.instruction.base_uri,
        )
        content = self._execute_plans(
            plan.children, focus=focus, locals_=locals_, final_output=True
        )
        _append_complex_content(factory, element, content, allow_attributes=True)
        return element

    def _result_document(
        self,
        plan: _ResultDocumentPlan,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
        final_output: bool,
    ) -> None:
        if not final_output:
            raise XsltError(
                "xsl:result-document is not permitted in temporary output state",
                code="XTDE1480",
            )
        href = self._value_source(plan.href, focus, locals_)
        method = (
            self._value_source(plan.method, focus, locals_) if plan.method is not None else None
        )
        format_name = (
            self._value_source(plan.format, focus, locals_) if plan.format is not None else None
        )
        format_attributes: dict[str, str] = {}
        if format_name is not None:
            declaration = self.package.output(format_name)
            if declaration is None:
                raise XsltError(
                    f"the named output format {format_name!r} is not declared",
                    code="XTDE1460",
                )
            format_attributes.update(declaration.attributes)
        evaluated = {
            name: self._value_source(value, focus, locals_) for name, value in plan.attributes
        }
        effective_method = method or format_attributes.get("method", "xml")
        build_tree = (
            self._value_source(plan.build_tree, focus, locals_)
            if plan.build_tree is not None
            else format_attributes.get("build-tree")
        )
        uri = self._secondary_uri(href)
        if uri in self.final_uris:
            raise XsltError(
                f"the transformation produced two final result trees with URI {uri!r}",
                code="XTDE1490",
            )
        self.final_uris.add(uri)
        sequence = self._execute_plans(
            plan.children, focus=focus, locals_=locals_, final_output=True
        )
        tree = None
        if self._build_tree(build_tree, effective_method):
            tree = _build_result_tree(sequence, output_uri=uri)
        serialization = dict(format_attributes)
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
        layers = self.stylesheet._modes.get(mode, ())
        indexes = tuple(PatternIndex(layer) for layer in layers)
        result: list[XdmItem] = []
        size = len(items)
        for position, item in enumerate(items, 1):
            focus = Focus.singleton(item, position=position, size=size)
            rule = self._select_rule(indexes, item, mode)
            if rule is None:
                result.extend(self._builtin(item, mode, tunnel_parameters))
                continue
            body = cast(_BodyPlan, rule.template)
            locals_: dict[str, XdmSequence] = {}
            index = 0
            while index < len(body.body) and isinstance(body.body[index], _ParamPlan):
                parameter = cast(_ParamPlan, body.body[index])
                supplied = (
                    tunnel_parameters.get(parameter.name)
                    if parameter.tunnel
                    else template_parameters.get(parameter.name)
                )
                value = (
                    supplied
                    if supplied is not None
                    else self._parameter_default(parameter, focus, locals_)
                )
                self._bind(locals_, parameter.name, value)
                index += 1
            result.extend(
                self._execute_plans(
                    body.body[index:], focus=focus, locals_=locals_, final_output=True
                )
            )
        return tuple(result)

    def _select_rule(
        self,
        indexes: tuple[PatternIndex, ...],
        item: XdmItem,
        mode: str | None,
    ) -> TemplateRule | None:
        environment = self._environment({})
        for index in indexes:
            rule = index.select(
                item,
                mode=mode,
                package_layer=0,
                environment=environment,
            )
            if rule is not None:
                return rule
        return None

    def _builtin(
        self,
        item: XdmItem,
        mode: str | None,
        tunnel_parameters: Mapping[str, XdmSequence],
    ) -> XdmSequence:
        if isinstance(item, XdmNode):
            if item.kind in (NodeKind.DOCUMENT, NodeKind.ELEMENT):
                return self._apply_templates(
                    item.children,
                    mode,
                    MappingProxyType({}),
                    tunnel_parameters,
                )
            if item.kind in (NodeKind.TEXT, NodeKind.ATTRIBUTE):
                return (self._text_node(item.string_value),)
            return ()
        return (self._text_node(self._string_value(item)),)

    # -- expressions and values -------------------------------------------

    def _evaluate(
        self,
        expression: CompiledExpression,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
    ) -> XdmSequence:
        engine = XPathEngine(expression.version)
        return engine.evaluate(
            expression,
            self._environment(locals_),
            focus=focus,
        )

    def _environment(self, locals_: Mapping[str, XdmSequence]) -> ExpressionEnvironment:
        variables: dict[str, Any] = {}
        for key, value in self.globals.items():
            self._bind(variables, key, value)
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
    ) -> str:
        if isinstance(source, str):
            return source
        return self._value_template(source, focus, locals_)

    def _value_template(
        self,
        template: CompiledValueTemplate,
        focus: Focus,
        locals_: Mapping[str, XdmSequence],
    ) -> str:
        parts: list[str] = []
        for part in template.parts:
            if part.kind == "text":
                parts.append(part.text)
                continue
            assert part.expression is not None
            values = self._evaluate(part.expression, focus, locals_)
            parts.append(" ".join(self._string_value(item) for item in values))
        return "".join(parts)

    def _string_value(self, item: XdmItem) -> str:
        if isinstance(item, XdmAtomic):
            return item.lexical
        if isinstance(item, XdmNode):
            return item.string_value
        raise XsltError(
            "atomization of a function item is not permitted here",
            code="FOTY0013",
        )


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
