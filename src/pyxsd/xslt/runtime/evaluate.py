"""Dynamic XPath compilation for ``xsl:evaluate``.

A dynamically compiled expression may only be reused when its *complete* static
context matches.  The expression text alone is never a cache key: namespaces,
default namespace, base URI, variable declarations, allowed functions, schema
awareness, collations, XPath version, and package scope all change how a target
expression compiles.

The cache is invocation-local and bounded.  Compilation uses the same
:class:`~pyxsd.xpath.engine.XPathEngine` and XDM/schema adapters as statically
compiled XPath.  Evaluation receives an :class:`~pyxsd.xpath.environment.
ExpressionEnvironment` built by the containing transformation, so dynamic
evaluation never gains a document, collection, extension function, or other
capability the transformation does not already possess.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pyxsd.xdm import XdmAtomic, XdmMap, XdmSequence
from pyxsd.xdm.nodes import XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xpath.environment import (
    ExpressionEnvironment,
    Focus,
    FunctionArity,
    XPathStaticContext,
)
from pyxsd.xslt.compiler.plans import _EvaluatePlan
from pyxsd.xslt.errors import XsltDynamicError, error_code_from_exception, is_budget_alarm
from pyxsd.xslt.runtime.context import EvaluationContext

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xslt.processor import _Invocation

__all__ = [
    "DEFAULT_DYNAMIC_CACHE_SIZE",
    "DynamicExpressionCache",
    "StaticContextIdentity",
    "compile_cache_key",
    "evaluate_dynamic",
    "execute_dynamic_evaluate",
    "with_params_variables",
]

#: Maximum number of dynamically compiled expressions retained per invocation.
DEFAULT_DYNAMIC_CACHE_SIZE = 64

_XPATH_VERSIONS = frozenset({"1.0", "2.0", "3.0", "3.1"})
_XSLT_VERSIONS = frozenset({"1.0", "2.0", "3.0", "3.1"})


def _freeze_strings(
    mapping: Mapping[str, Any],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(mapping, Mapping):
        raise TypeError(f"{label} must be a mapping")
    values: dict[str, Any] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not key:
            raise TypeError(f"{label} names must be non-empty strings")
        values[key] = value
    return MappingProxyType(values)


def _normalise_arity(value: FunctionArity, name: str) -> FunctionArity:
    if isinstance(value, bool):
        raise TypeError(f"function arity for {name!r} must not be a boolean")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"function arity for {name!r} must be non-negative")
        return value
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], int)
        and not isinstance(value[0], bool)
        and (value[1] is None or (isinstance(value[1], int) and not isinstance(value[1], bool)))
    ):
        minimum, maximum = value
        if minimum < 0 or (maximum is not None and maximum < minimum):
            raise ValueError(f"function arity for {name!r} is invalid")
        return value
    raise ValueError(f"function arity for {name!r} must be an integer or a (min, max) pair")


def _schema_identity(schema: SchemaPool | None) -> tuple[Any, ...] | None:
    """One stable identity for the compile-relevant schema declarations."""
    if schema is None:
        return None
    return (
        id(schema),
        schema.xsd_version,
        tuple(sorted(schema.types)),
        tuple(sorted(schema.elements)),
        tuple(sorted(schema.attributes)),
        tuple(sorted(schema.substitution_groups)),
    )


@dataclass(frozen=True, slots=True)
class StaticContextIdentity:
    """The complete static context of one dynamic XPath compilation.

    Values are frozen and the cache key is derived from every field that can
    affect parsing or static analysis.  Dynamic variable *values* are
    deliberately absent; only their declared names and types participate.
    """

    xpath_version: str = "3.1"
    namespaces: Mapping[str, str] = field(default_factory=dict)
    default_namespace: str | None = None
    base_uri: str | None = None
    variable_types: Mapping[str, str] = field(default_factory=dict)
    functions: Mapping[str, FunctionArity] = field(default_factory=dict)
    schema: SchemaPool | None = None
    schema_aware: bool = False
    default_collation: str | None = None
    xslt_version: str | None = None
    package_name: str | None = None

    def __post_init__(self) -> None:
        if self.xpath_version not in _XPATH_VERSIONS:
            raise ValueError(f"unsupported dynamic XPath version {self.xpath_version!r}")
        if self.xslt_version is not None and self.xslt_version not in _XSLT_VERSIONS:
            raise ValueError(f"unsupported dynamic XSLT version {self.xslt_version!r}")
        namespaces = _freeze_strings(self.namespaces, "namespaces")
        for uri in namespaces.values():
            if not isinstance(uri, str):
                raise TypeError("namespace URIs must be strings")
        variable_types = _freeze_strings(self.variable_types, "variable types")
        for type_name in variable_types.values():
            if not isinstance(type_name, str) or not type_name:
                raise TypeError("variable types must be non-empty strings")
        functions: dict[str, FunctionArity] = {}
        for name, arity in self.functions.items():
            if not isinstance(name, str) or not name:
                raise TypeError("function names must be non-empty strings")
            if not name.startswith("{") and ":" not in name:
                raise ValueError(f"function {name!r} requires a namespace prefix or Clark name")
            functions[name] = _normalise_arity(arity, name)
        for label, value in (
            ("default_namespace", self.default_namespace),
            ("base_uri", self.base_uri),
            ("default_collation", self.default_collation),
            ("package_name", self.package_name),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{label} must be a string or None")
        if not isinstance(self.schema_aware, bool):
            raise TypeError("schema_aware must be a boolean")
        if self.schema is not None and not isinstance(self.schema, SchemaPool):
            raise TypeError("schema must be a SchemaPool or None")
        object.__setattr__(self, "namespaces", namespaces)
        object.__setattr__(self, "variable_types", variable_types)
        object.__setattr__(self, "functions", MappingProxyType(functions))

    def key(self) -> tuple[Any, ...]:
        """A hashable identity covering every compile-relevant component."""
        return (
            self.xpath_version,
            tuple(sorted(self.namespaces.items())),
            self.default_namespace,
            self.base_uri,
            tuple(sorted(self.variable_types.items())),
            tuple(
                sorted(
                    (name, arity if isinstance(arity, int) else tuple(arity))
                    for name, arity in self.functions.items()
                )
            ),
            _schema_identity(self.schema) if self.schema_aware else None,
            self.schema_aware,
            self.default_collation,
            self.xslt_version,
            self.package_name,
        )

    def xpath_static_context(self) -> XPathStaticContext:
        """Build the Task 11 static context without any XSLT function surface."""
        schema = self.schema if self.schema_aware else None
        functions: dict[str, FunctionArity] = {}
        for name, arity in self.functions.items():
            if name.startswith("{"):
                namespace = name[1:].partition("}")[0]
                if not any(
                    prefix and bound == namespace for prefix, bound in self.namespaces.items()
                ):
                    # The standalone parser registers host functions by prefix;
                    # a Clark name with no in-scope prefix is not callable here.
                    continue
            functions[name] = arity
        return XPathStaticContext(
            namespaces=dict(self.namespaces),
            default_namespace=self.default_namespace,
            variable_types=dict(self.variable_types),
            functions=functions,
            schema=schema,
            base_uri=self.base_uri,
            default_collation=self.default_collation,
            xsd_version=None if schema is None else schema.xsd_version,
            xslt_version=self.xslt_version,
        )


def compile_cache_key(expression: str, identity: StaticContextIdentity) -> tuple[Any, ...]:
    """Return the cache key for *expression* in *identity*'s static context."""
    if not isinstance(expression, str):
        raise TypeError("a dynamic XPath expression must be a string")
    if not isinstance(identity, StaticContextIdentity):
        raise TypeError("identity must be a StaticContextIdentity")
    return expression, identity.key()


class DynamicExpressionCache:
    """A bounded, invocation-local LRU cache of compiled dynamic expressions."""

    __slots__ = ("_entries", "_max_entries", "compile_count")

    def __init__(self, max_entries: int = DEFAULT_DYNAMIC_CACHE_SIZE) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise TypeError("max_entries must be an integer")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[Any, ...], CompiledExpression] = OrderedDict()
        self.compile_count = 0

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def compile(
        self,
        expression: str,
        identity: StaticContextIdentity,
    ) -> CompiledExpression:
        """Compile once per complete static context, raising ``XTDE3160`` on failure."""
        key = compile_cache_key(expression, identity)
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            return cached
        try:
            compiled = XPathEngine(identity.xpath_version, xslt_functions=False).compile(
                expression,
                identity.xpath_static_context(),
            )
        except Exception as exc:
            if is_budget_alarm(exc):
                raise
            raise XsltDynamicError(
                f"the dynamic XPath expression {expression!r} is not a valid expression: {exc}",
                code="XTDE3160",
                cause=exc,
            ) from exc
        self.compile_count += 1
        self._entries[key] = compiled
        if len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return compiled


def with_params_variables(value: XdmSequence) -> dict[str, XdmSequence]:
    """Extract the QName-keyed variable bindings from an ``xsl:with-params`` map."""
    if len(value) != 1 or not isinstance(value[0], XdmMap):
        raise XsltDynamicError(
            "xsl:evaluate/@with-params must evaluate to a single map",
            code="XTTE3165",
        )
    variables: dict[str, XdmSequence] = {}
    for key, member in value[0].entries:
        expanded = _expanded_qname(key)
        if expanded is None:
            raise XsltDynamicError(
                "xsl:evaluate/@with-params keys must be xs:QName values",
                code="XTTE3165",
            )
        variables[expanded] = member
    return variables


def _expanded_qname(item: XdmAtomic) -> str | None:
    if not item.type_name.endswith("}QName"):
        return None
    value = item.value
    local = getattr(value, "local_name", None)
    if not isinstance(local, str):
        return None
    namespace = getattr(value, "namespace", None)
    if isinstance(namespace, str) and namespace:
        return f"{{{namespace}}}{local}"
    return local


def evaluate_dynamic(
    expression: str,
    identity: StaticContextIdentity,
    environment: ExpressionEnvironment,
    *,
    focus: Focus,
    cache: DynamicExpressionCache,
) -> XdmSequence:
    """Compile and evaluate one target expression through the Task 11 engine."""
    if not isinstance(environment, ExpressionEnvironment):
        raise TypeError("environment must be an ExpressionEnvironment")
    if not isinstance(focus, Focus):
        raise TypeError("focus must be a Focus")
    if not isinstance(cache, DynamicExpressionCache):
        raise TypeError("cache must be a DynamicExpressionCache")
    compiled = cache.compile(expression, identity)
    try:
        return XPathEngine(identity.xpath_version, xslt_functions=False).evaluate(
            compiled,
            environment,
            focus=focus,
        )
    except Exception as exc:
        if is_budget_alarm(exc):
            raise
        code = error_code_from_exception(exc)
        if code == "XPST0008":
            raise XsltDynamicError(
                f"the dynamic XPath expression {expression!r} references a "
                f"variable that is not visible: {exc}",
                code="XTDE3160",
                cause=exc,
            ) from exc
        raise XsltDynamicError(
            f"dynamic XPath evaluation failed: {exc}",
            code=code,
            cause=exc,
        ) from exc


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


def execute_dynamic_evaluate(
    plan: _EvaluatePlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Evaluate one compiled ``xsl:evaluate`` instruction."""
    if not plan.enabled:
        raise XsltDynamicError(
            "xsl:evaluate is disabled for this transformation",
            code="XTDE3175",
            location=plan.instruction.location,
        )
    target_values = invocation._evaluate(plan.expression, context)
    profile = invocation.profile_for(plan.expression.static_context.xslt_version)
    target = profile.string_sequence(target_values)

    parameter_values: dict[str, XdmSequence] = {}
    variable_types: dict[str, str] = {}
    for parameter in plan.with_params:
        if parameter.expression is not None:
            value = invocation._evaluate(parameter.expression, context)
        else:
            value = invocation._execute_plans(
                parameter.children,
                context=replace(context, final_output=False),
            )
        value = invocation._coerce(
            value,
            parameter.sequence_type,
            code="XTTE0590",
            label=f"parameter {parameter.name!r}",
            package_name=context.package_name,
        )
        parameter_values[parameter.name] = value
        variable_types[parameter.name] = (
            "item()*" if parameter.sequence_type is None else str(parameter.sequence_type)
        )
    if plan.with_params_expression is not None:
        supplied = invocation._evaluate(plan.with_params_expression, context)
        for name, value in with_params_variables(supplied).items():
            parameter_values[name] = value
            variable_types[name] = "item()*"

    expression_focus = Focus.absent()
    if plan.context_item is not None:
        context_values = invocation._evaluate(plan.context_item, context)
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
        context_nodes = invocation._evaluate(plan.namespace_context, context)
        if len(context_nodes) != 1 or not isinstance(context_nodes[0], XdmNode):
            raise XsltDynamicError(
                "xsl:evaluate/@namespace-context must return a single node",
                code="XTTE3170",
                location=plan.instruction.location,
            )
        namespaces = {
            prefix: uri for prefix, uri in context_nodes[0].in_scope_namespaces.items() if prefix
        }
        default_namespace = context_nodes[0].in_scope_namespaces.get("")

    base_uri = (
        invocation._value_source(plan.base_uri, context)
        if plan.base_uri is not None
        else plan.instruction.base_uri
    )
    schema_aware = False
    if plan.schema_aware is not None:
        schema_aware = _boolean_avt(
            invocation._value_source(plan.schema_aware, context),
            "schema-aware",
        )
    schema = (
        invocation.stylesheet.schema_pool
        if schema_aware and invocation.stylesheet.processor.schema_aware
        else None
    )

    package_name = context.package_name
    available = invocation.fallback_functions(package_name)
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
        decimal_formats=invocation._decimal_formats(package_name),
        current_datetime=invocation.current_datetime,
        implicit_timezone=invocation.implicit_timezone,
        base_uri=base_uri,
    )
    result = evaluate_dynamic(
        target,
        identity,
        environment,
        focus=expression_focus,
        cache=invocation.dynamic_expressions,
    )
    if plan.sequence_type is not None:
        result = invocation._coerce(
            result,
            plan.sequence_type,
            code="XPTY0004",
            label="xsl:evaluate result",
            package_name=context.package_name,
        )
    return result
