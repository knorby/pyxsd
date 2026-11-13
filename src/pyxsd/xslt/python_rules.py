"""Restricted Python callbacks compiled into synthetic XSLT customization layers.

A :class:`PythonRules` value is a compilation-time registry, like
:class:`~pyxsd.xslt.extensions.XsltExtensions`.  It describes named-component
overrides and named-mode template rules; the processor compiles the unmodified
stylesheet first, binds every rule against that artifact, and then compiles a
synthetic module or package whose only job is to route the selected component
or rule through one Python callback.

Callbacks receive exactly one :class:`PythonRuleContext`.  The available
delegation operations are fixed by the registered role: a named-component
override may call the original implementation, while a mode rule may continue
with ``next-match`` or ``apply-imports``.  A callback returns an ordinary XDM
value; ``None`` is a type error, never the empty sequence, and an uncaught host
exception keeps its identity as the cause of a non-catchable dynamic error.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape, quoteattr

from elementpath.helpers import is_ncname

from pyxsd.xdm import XdmItem, XdmSequence, as_xdm
from pyxsd.xslt.compiler.components import (
    Component,
    ComponentKey,
    _expand_name,
)
from pyxsd.xslt.compiler.static import EffectiveModule
from pyxsd.xslt.errors import XsltDynamicError, XsltError, XsltStaticError

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xslt.compiler.packages import CompiledPackage
    from pyxsd.xslt.compiler.plans import _ComponentPlan

_PlanKey = tuple[str | None, str, str | None, int | None]

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

#: The private instruction namespace used by generated customization layers.
PYTHON_RULES_NS = "urn:pyxsd:python-rules"
PYTHON_INVOKE = f"{{{PYTHON_RULES_NS}}}invoke"

#: Fixed identities of generated layers; neither is a retrievable resource.
PYTHON_ORIGINAL_NAME = "urn:pyxsd:python-rules:original"
PYTHON_APPLICATION_NAME = "urn:pyxsd:python-rules:application"

__all__ = [
    "PYTHON_INVOKE",
    "PYTHON_RULES_NS",
    "PythonRuleContext",
    "PythonRules",
    "bind_python_rules",
    "build_python_layer",
    "execute_python_rule",
]


def _rule_name(value: str) -> str:
    """Validate one template/function name without expanding its prefix."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a Python rule name must be a non-empty QName or expanded name")
    name = value.strip()
    if name.startswith("Q{") or name.startswith("{"):
        return name
    prefix, separator, local = name.partition(":")
    if not separator:
        if not is_ncname(name):
            raise ValueError(f"{value!r} is not a valid QName or expanded name")
        return name
    if not is_ncname(prefix) or not is_ncname(local):
        raise ValueError(f"{value!r} is not a valid QName or expanded name")
    return name


def _rule_priority(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("a Python rule priority must be a finite decimal number")
    try:
        priority = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid Python rule priority {value!r}") from exc
    if not priority.is_finite():
        raise ValueError("a Python rule priority must be finite")
    return priority


def _callback(value: Callable[[PythonRuleContext], Any]) -> Callable[[PythonRuleContext], Any]:
    if not callable(value):
        raise TypeError("a Python rule callback must be callable")
    return value


@dataclass(frozen=True, slots=True)
class _PythonTemplateRule:
    name: str
    callback: Callable[[PythonRuleContext], Any]


@dataclass(frozen=True, slots=True)
class _PythonFunctionRule:
    name: str
    arity: int
    callback: Callable[[PythonRuleContext], Any]


@dataclass(frozen=True, slots=True)
class _PythonModeRule:
    mode: str | None
    match: str
    callback: Callable[[PythonRuleContext], Any]
    priority: Decimal | None = None


_PythonRule = _PythonTemplateRule | _PythonFunctionRule | _PythonModeRule


@dataclass(frozen=True, slots=True)
class _BoundPythonRule:
    """One validated rule and the artifact values it delegates to."""

    rule: _PythonRule
    component: Component | None = None
    plan: _ComponentPlan | None = None
    original_abstract: bool = False


@dataclass(frozen=True, slots=True)
class PythonRuleContext:
    """The restricted dynamic context passed to every Python rule callback.

    ``parameters`` is the current bound local frame.  Delegation is legal only
    for the registered role; an unavailable operation raises ``XTDE0560`` and
    an abstract original without an implementation raises ``XTDE0040``.
    """

    context_item: XdmItem | None = None
    parameters: Mapping[str, XdmSequence] = field(default_factory=dict)
    _invoke_original: Callable[[tuple[Any, ...]], XdmSequence] | None = field(
        default=None, repr=False, compare=False
    )
    _next_match: Callable[[], XdmSequence] | None = field(default=None, repr=False, compare=False)
    _apply_imports: Callable[[], XdmSequence] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.context_item is not None and not isinstance(self.context_item, XdmItem):
            raise TypeError("a Python rule context item must be an XDM item or None")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("Python rule parameters must be a mapping")
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(self.parameters)),
        )
        for label, callback in (
            ("invoke_original", self._invoke_original),
            ("next_match", self._next_match),
            ("apply_imports", self._apply_imports),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"Python rule {label} must be callable or None")

    def invoke_original(self, *arguments: Any) -> XdmSequence:
        """Execute the XML component this named override replaced."""
        if self._invoke_original is None:
            raise XsltDynamicError(
                "invoke_original is only available to a named-component override",
                code="XTDE0560",
            )
        return self._invoke_original(tuple(arguments))

    def next_match(self) -> XdmSequence:
        """Continue with the next template rule in conflict-resolution order."""
        if self._next_match is None:
            raise XsltDynamicError(
                "next_match is only available inside a mode rule",
                code="XTDE0560",
            )
        return self._next_match()

    def apply_imports(self) -> XdmSequence:
        """Continue with the next lower import-precedence template rule."""
        if self._apply_imports is None:
            raise XsltDynamicError(
                "apply_imports is only available inside a mode rule",
                code="XTDE0560",
            )
        return self._apply_imports()


@dataclass(frozen=True, slots=True)
class PythonRules:
    """An immutable set of Python component overrides and mode rules."""

    _entries: tuple[_PythonRule, ...] = ()
    _bound: tuple[_BoundPythonRule, ...] = ()

    @classmethod
    def empty(cls) -> PythonRules:
        """Return an empty registry."""
        return cls()

    def override_template(
        self,
        name: str,
        callback: Callable[[PythonRuleContext], Any],
    ) -> PythonRules:
        """Return a registry that replaces one named template."""
        return replace(
            self,
            _entries=(*self._entries, _PythonTemplateRule(_rule_name(name), _callback(callback))),
            _bound=(),
        )

    def override_function(
        self,
        name: str,
        arity: int,
        callback: Callable[[PythonRuleContext], Any],
    ) -> PythonRules:
        """Return a registry that replaces one stylesheet function."""
        if isinstance(arity, bool) or not isinstance(arity, int) or arity < 0:
            raise ValueError("a Python function rule arity must be a non-negative integer")
        return replace(
            self,
            _entries=(
                *self._entries,
                _PythonFunctionRule(_rule_name(name), arity, _callback(callback)),
            ),
            _bound=(),
        )

    def add_mode_rule(
        self,
        mode: str | None,
        match: str,
        callback: Callable[[PythonRuleContext], Any],
        *,
        priority: Decimal | int | str | None = None,
    ) -> PythonRules:
        """Return a registry that adds one template rule to a named mode."""
        if mode is not None and not isinstance(mode, str):
            raise TypeError("a Python mode rule mode must be a string or None")
        if not isinstance(match, str) or not match.strip():
            raise ValueError("a Python mode rule requires a non-empty match pattern")
        return replace(
            self,
            _entries=(
                *self._entries,
                _PythonModeRule(
                    None if mode is None else _rule_name(mode),
                    match.strip(),
                    _callback(callback),
                    _rule_priority(priority),
                ),
            ),
            _bound=(),
        )

    @property
    def entries(self) -> tuple[_PythonRule, ...]:
        return self._entries

    @property
    def bound(self) -> tuple[_BoundPythonRule, ...]:
        return self._bound

    @property
    def rule_count(self) -> int:
        return len(self._entries)

    @property
    def original_components(self) -> Mapping[ComponentKey, Component]:
        return MappingProxyType(
            {
                (item.component.kind, item.component.name, item.component.arity): item.component
                for item in self._bound
                if item.component is not None
            }
        )

    @property
    def original_plans(self) -> Mapping[ComponentKey, _ComponentPlan]:
        return MappingProxyType(
            {
                (item.component.kind, item.component.name, item.component.arity): item.plan
                for item in self._bound
                if item.component is not None and item.plan is not None
            }
        )

    def resolve(
        self,
        namespaces: Mapping[str, str],
        *,
        identity: str | None = None,
    ) -> PythonRules:
        """Resolve lexical names against one stylesheet's in-scope namespaces."""
        entries: list[_PythonRule] = []
        for entry in self._entries:
            if isinstance(entry, _PythonModeRule):
                entries.append(
                    replace(
                        entry,
                        mode=(
                            None
                            if entry.mode is None
                            else _expand_name(
                                entry.mode,
                                namespaces,
                                uri=identity,
                                code="XTSE0280",
                            )
                        ),
                    )
                )
                continue
            entries.append(
                replace(
                    entry,
                    name=_expand_name(
                        entry.name,
                        namespaces,
                        uri=identity,
                        code="XTSE0280",
                    ),
                )
            )
        return replace(self, _entries=tuple(entries))

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)


def _static_error(
    message: str,
    code: str,
    identity: str | None,
) -> XsltStaticError:
    return XsltStaticError(message, code=code, uri=identity)


def _bind_component(
    rule: _PythonTemplateRule | _PythonFunctionRule,
    component: Component,
    components: Mapping[_PlanKey, _ComponentPlan],
) -> _BoundPythonRule:
    plan = components.get((component.owner, component.kind, component.name, component.arity))
    return _BoundPythonRule(
        rule=rule,
        component=component,
        plan=plan,
        original_abstract=component.visibility == "abstract",
    )


def _bind_mode(rule: _PythonModeRule) -> _BoundPythonRule:
    return _BoundPythonRule(rule=rule)


def bind_python_rules(
    rules: PythonRules,
    package: CompiledPackage,
    components: Mapping[_PlanKey, _ComponentPlan],
    *,
    identity: str | None = None,
) -> PythonRules:
    """Validate every rule against the first compiled artifact and bind it."""
    bound: list[_BoundPythonRule] = []
    for rule in rules.entries:
        if isinstance(rule, _PythonModeRule):
            if rule.mode is None or rule.mode == "#unnamed":
                raise _static_error(
                    "a Python mode rule requires a public named mode",
                    "XTSE3050",
                    identity,
                )
            mode = package.mode(rule.mode)
            if mode is None or mode.name is None or mode.visibility != "public":
                raise _static_error(
                    f"the mode {rule.mode!r} is not a public named mode",
                    "XTSE3050",
                    identity,
                )
            bound.append(_bind_mode(rule))
            continue
        if isinstance(rule, _PythonTemplateRule):
            component = package.named_template(rule.name)
            if component is None:
                raise _static_error(
                    f"the named template {rule.name!r} is not visible in the compiled package",
                    "XTSE0010",
                    identity,
                )
        else:
            component = package.function(rule.name, rule.arity)
            if component is None:
                raise _static_error(
                    f"the stylesheet function {rule.name!r} with arity {rule.arity} "
                    "is not visible in the compiled package",
                    "XTSE0010",
                    identity,
                )
        if component.visibility in ("private", "final"):
            raise _static_error(
                f"the component {component.name!r} is {component.visibility} and cannot "
                "be overridden by a Python rule",
                "XTSE3050",
                identity,
            )
        bound.append(_bind_component(rule, component, components))
    return replace(rules, _bound=tuple(bound))


# ---------------------------------------------------------------------------
# Synthetic layer generation


def _eqname(name: str) -> str:
    if name.startswith("{"):
        uri, _, local = name[1:].partition("}")
        return f"Q{{{uri}}}{local}"
    return name


def _sequence_type_text(value: Any) -> str:
    return str(value)


def _namespace_declarations(effective: EffectiveModule) -> str:
    declarations = [
        f'xmlns:xsl="{XSLT_NS}"',
        f'xmlns:py="{PYTHON_RULES_NS}"',
    ]
    seen = {"xsl", "py", "xml", "xmlns"}
    for prefix, uri in sorted(effective.root.in_scope_namespaces.items()):
        if not uri or prefix in seen:
            continue
        seen.add(prefix)
        declarations.append(
            f'xmlns="{escape(uri)}"' if not prefix else f'xmlns:{prefix}="{escape(uri)}"'
        )
    return " ".join(declarations)


def _root_attributes(effective: EffectiveModule) -> str:
    attributes: list[str] = ['version="3.0"', _namespace_declarations(effective)]
    for name in ("xpath-default-namespace", "default-validation", "input-type-annotations"):
        value = effective.root.attribute(name)
        if value is not None:
            attributes.append(f"{name}={quoteattr(value)}")
    return " ".join(attributes)


def _parameter_declarations(component: Component, *, function: bool) -> str:
    signature = component.signature
    assert signature is not None
    parameters: list[str] = []
    for parameter in signature.parameters:
        attributes = [f"name={quoteattr(_eqname(parameter.name))}"]
        attributes.append(f"as={quoteattr(_sequence_type_text(parameter.sequence_type))}")
        if not function:
            if parameter.required:
                attributes.append('required="yes"')
            if parameter.tunnel:
                attributes.append('tunnel="yes"')
        parameters.append(f"<xsl:param {' '.join(attributes)}/>")
    return "".join(parameters)


def _context_item_declaration(component: Component) -> str:
    signature = component.signature
    assert signature is not None
    context_item = signature.context_item
    assert context_item is not None
    if context_item.use == "absent":
        return '<xsl:context-item use="absent"/>'
    return (
        f"<xsl:context-item use={quoteattr(context_item.use)} "
        f"as={quoteattr(_sequence_type_text(context_item.sequence_type))}/>"
    )


def _component_attributes(component: Component) -> str:
    attributes = ['visibility="public"']
    declaration = component.declaration
    signature = component.signature
    assert signature is not None
    assert signature.return_type is not None
    attributes.append(f"as={quoteattr(_sequence_type_text(signature.return_type))}")
    for name in (
        "identity-sensitive",
        "new-each-time",
        "override-extension-functions",
        "streamable",
        "posture",
        "sweep",
    ):
        value = declaration.attribute(name)
        if value is not None:
            attributes.append(f"{name}={quoteattr(value)}")
    return " ".join(attributes)


def _rule_declaration(index: int, bound: _BoundPythonRule) -> str:
    rule = bound.rule
    invoke = f'<py:invoke rule="{index}"/>'
    if isinstance(rule, _PythonTemplateRule):
        component = bound.component
        assert component is not None
        return (
            f"<xsl:template name={quoteattr(_eqname(rule.name))} "
            f"{_component_attributes(component)}>"
            f"{_context_item_declaration(component)}"
            f"{_parameter_declarations(component, function=False)}"
            f"{invoke}</xsl:template>"
        )
    if isinstance(rule, _PythonFunctionRule):
        component = bound.component
        assert component is not None
        return (
            f"<xsl:function name={quoteattr(_eqname(rule.name))} "
            f"{_component_attributes(component)}>"
            f"{_parameter_declarations(component, function=True)}"
            f"{invoke}</xsl:function>"
        )
    attributes = [
        f"match={quoteattr(rule.match)}",
        f"mode={quoteattr(_eqname(rule.mode or ''))}",
    ]
    if rule.priority is not None:
        attributes.append(f"priority={quoteattr(str(rule.priority))}")
    return f"<xsl:template {' '.join(attributes)}>{invoke}</xsl:template>"


def build_python_layer(
    rules: PythonRules,
    effective: EffectiveModule,
    *,
    dependency_name: str | None = None,
    package_name: str | None = None,
) -> str:
    """Return the generated in-memory stylesheet for *rules*.

    With *dependency_name* and *package_name*, the layer is a synthetic
    application package that uses *dependency_name* and overrides its selected
    components.  Otherwise it is an ordinary importing module with a single
    ``xsl:import`` placeholder resolved through ``SourceModule``.
    """
    if len(rules.bound) != len(rules.entries):
        raise ValueError("a Python layer requires every rule to be bound")
    declarations = "".join(
        _rule_declaration(index, bound) for index, bound in enumerate(rules.bound)
    )
    root = _root_attributes(effective)
    if dependency_name is None or package_name is None:
        return (
            f"<xsl:stylesheet {root}>"
            f'<xsl:import href="{PYTHON_ORIGINAL_NAME}" _href="{PYTHON_ORIGINAL_NAME}"/>'
            f"{declarations}</xsl:stylesheet>"
        )
    accepts = (
        "".join(
            f'<xsl:accept component={quoteattr(kind)} names="*" visibility={quoteattr(visibility)}/>'
            for visibility in ("public", "abstract")
            for kind in ("template", "function", "variable", "attribute-set")
        )
        + '<xsl:accept component="mode" names="*" visibility="public"/>'
    )
    return (
        f"<xsl:package name={quoteattr(package_name)} {root}>"
        f"<xsl:use-package name={quoteattr(dependency_name)}>"
        f"{accepts}<xsl:override>{declarations}</xsl:override>"
        "</xsl:use-package></xsl:package>"
    )


# ---------------------------------------------------------------------------
# Runtime execution


def execute_python_rule(
    rule_index: int,
    invocation: Any,
    context: Any,
) -> XdmSequence:
    """Execute one bound Python rule in the current evaluation context."""
    stylesheet = getattr(invocation, "stylesheet", None)
    extensions = getattr(stylesheet, "extensions", None)
    rules = getattr(extensions, "python_rules", None)
    bound = None if rules is None else _bound_at(rules, rule_index)
    if bound is None:
        raise XsltDynamicError(
            f"the Python rule {rule_index} is not bound in this compiled artifact",
            code=None,
        )
    assert rules is not None
    rule = bound.rule
    if isinstance(rule, _PythonModeRule):
        rule_context = PythonRuleContext(
            context_item=context.focus.item,
            parameters=context.locals,
            _next_match=lambda: _continue(invocation, context, "next-match"),
            _apply_imports=lambda: _continue(invocation, context, "apply-imports"),
        )
    else:
        rule_context = PythonRuleContext(
            context_item=context.focus.item,
            parameters=context.locals,
            _invoke_original=lambda arguments: _invoke_original(
                rules,
                bound,
                invocation,
                context,
                arguments,
            ),
        )
    try:
        result = rule.callback(rule_context)
    except XsltError:
        raise
    except Exception as exc:
        raise XsltDynamicError(
            f"the Python rule callback raised {type(exc).__name__}: {exc}",
            code=None,
            allow_catch=False,
            cause=exc,
        ) from exc
    if result is None:
        raise XsltDynamicError(
            "the Python rule callback returned None; return () for the empty sequence",
            code="XPTY0004",
        )
    try:
        return tuple(as_xdm(result))
    except (TypeError, ValueError) as exc:
        raise XsltDynamicError(
            f"the Python rule callback returned an unusable result: {exc}",
            code="XPTY0004",
            cause=exc,
        ) from exc


def _bound_at(rules: PythonRules, index: int) -> _BoundPythonRule | None:
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if index < 0 or index >= len(rules.bound):
        return None
    return rules.bound[index]


def _continue(invocation: Any, context: Any, kind: str) -> XdmSequence:
    from pyxsd.xslt.runtime.templates import execute_continuation

    return execute_continuation(invocation, kind, context)


def _original_context(rules: PythonRules, context: Any) -> Any:
    return replace(
        context,
        original_components=rules.original_components,
        original_plans=rules.original_plans,
        original_functions={},
    )


def _invoke_original(
    rules: PythonRules,
    bound: _BoundPythonRule,
    invocation: Any,
    context: Any,
    arguments: Sequence[Any],
) -> XdmSequence:
    component = bound.component
    plan = bound.plan
    if bound.original_abstract or component is None or plan is None:
        raise XsltDynamicError(
            "the overridden component is abstract and has no executable original",
            code="XTDE0040",
        )
    assert component.signature is not None
    parameters = tuple(parameter.name for parameter in component.signature.parameters)
    original_context = _original_context(rules, context)
    if component.kind == "function":
        if arguments:
            if len(arguments) != len(parameters):
                raise XsltDynamicError(
                    f"invoke_original for {component.name!r} requires {len(parameters)} "
                    f"arguments, got {len(arguments)}",
                    code="XPTY0004",
                )
            values = tuple(as_xdm(argument) for argument in arguments)
        else:
            values = tuple(context.locals.get(name, ()) for name in parameters)
        return invocation._execute_component(
            component,
            plan=plan,
            arguments=values,
            context=original_context,
        )
    if arguments:
        if len(arguments) > len(parameters):
            raise XsltDynamicError(
                f"invoke_original for {component.name!r} accepts at most "
                f"{len(parameters)} arguments, got {len(arguments)}",
                code="XPTY0004",
            )
        template_parameters = {
            name: as_xdm(argument) for name, argument in zip(parameters, arguments, strict=False)
        }
    else:
        template_parameters = {
            name: context.locals[name] for name in parameters if name in context.locals
        }
    return invocation._execute_component(
        component,
        plan=plan,
        template_parameters=template_parameters,
        context=original_context,
    )
