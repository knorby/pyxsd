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
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from pyxsd.xdm import XdmAtomic, XdmMap, XdmSequence
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xpath.environment import (
    ExpressionEnvironment,
    Focus,
    FunctionArity,
    XPathStaticContext,
)
from pyxsd.xslt.errors import XsltDynamicError, error_code_from_exception

__all__ = [
    "DEFAULT_DYNAMIC_CACHE_SIZE",
    "DynamicExpressionCache",
    "StaticContextIdentity",
    "compile_cache_key",
    "evaluate_dynamic",
    "with_params_variables",
]

#: Maximum number of dynamically compiled expressions retained per invocation.
DEFAULT_DYNAMIC_CACHE_SIZE = 64

_XPATH_VERSIONS = frozenset({"2.0", "3.0", "3.1"})
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
