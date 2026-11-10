"""Module-local immutable XSLT static contexts.

One :class:`StaticContext` belongs to one stylesheet element.  It records the
element's in-scope namespaces, XPath default namespace, base URI, effective
version, schema pool, visible variables/functions and their declared sequence
types, default collation, statically known document/collection types, and
processor profile options.

Contexts are immutable and module-local.  An imported module keeps its own
namespaces, base URI, defaults, and symbols: compiling an expression there never
substitutes the principal module's static context.  Includes are part of the
same package; imports expose only components that are public outside their
package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Literal, TypeAlias

from pyxsd.locations import IssueLocation
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.environment import FunctionArity, XPathStaticContext
from pyxsd.xslt.compiler.expressions import CompiledSequenceType, compile_sequence_type
from pyxsd.xslt.compiler.static import EffectiveElement, EffectiveModule
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.profiles import LanguageProfile
from pyxsd.xslt.runtime.functions import xslt_function_declarations

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
FN_NS = "http://www.w3.org/2005/xpath-functions"
_MAP_NS = "http://www.w3.org/2005/xpath-functions/map"
_ARRAY_NS = "http://www.w3.org/2005/xpath-functions/array"
_MATH_NS = "http://www.w3.org/2005/xpath-functions/math"
_ERR_NS = "http://www.w3.org/2005/xqt-errors"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

BUILT_IN_NAMESPACES: Mapping[str, str] = MappingProxyType(
    {
        "xs": XSD_NS,
        "fn": FN_NS,
        "xsl": XSLT_NS,
        "map": _MAP_NS,
        "array": _ARRAY_NS,
        "math": _MATH_NS,
        "err": _ERR_NS,
        "xml": _XML_NS,
    }
)

_VISIBILITIES = frozenset({"public", "private", "final", "abstract", "hidden"})
_VALIDATION_MODES = frozenset({"preserve", "strip"})
_INPUT_TYPE_ANNOTATIONS = frozenset({"strip", "preserve", "unspecified"})
_PROFILES = frozenset({"xslt-1.0", "xslt-2.0", "xslt-3.0", "xslt-3.1"})
_VARIABLE = f"{{{XSLT_NS}}}variable"
_PARAM = f"{{{XSLT_NS}}}param"
_FUNCTION = f"{{{XSLT_NS}}}function"
_EXPAND_TEXT = f"{{{XSLT_NS}}}expand-text"
_DEFAULT_COLLATION = f"{{{XSLT_NS}}}default-collation"
_DEFAULT_VALIDATION = f"{{{XSLT_NS}}}default-validation"
_INPUT_TYPE_ANNOTATIONS_ATTR = f"{{{XSLT_NS}}}input-type-annotations"
_XPATH_DEFAULT = f"{{{XSLT_NS}}}xpath-default-namespace"
_VERSION = f"{{{XSLT_NS}}}version"

VariableKind: TypeAlias = Literal["variable", "param"]
FunctionKey: TypeAlias = tuple[str, FunctionArity]


def _freeze(mapping: Mapping[Any, Any], label: str) -> Mapping[Any, Any]:
    if not isinstance(mapping, Mapping):
        raise TypeError(f"{label} must be a mapping")
    return MappingProxyType(dict(mapping))


def _validate_version(
    value: str,
    *,
    uri: str | None = None,
    location: IssueLocation | None = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise XsltStaticError(
            "the version attribute must be a decimal number",
            code="XTSE0110",
            uri=uri,
            location=location,
        )
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise XsltStaticError(
            f"the version attribute {value!r} is not a decimal number",
            code="XTSE0110",
            uri=uri,
            location=location,
            cause=exc,
        ) from exc
    if not number.is_finite() or number < 0:
        raise XsltStaticError(
            f"the version attribute {value!r} is not a valid version",
            code="XTSE0110",
            uri=uri,
            location=location,
        )
    return value.strip()


def _xpath_version(version: str) -> str:
    return "3.1" if Decimal(version) >= Decimal("3.0") else "2.0"


def _normalise_arity(value: Any, name: str) -> FunctionArity:
    if isinstance(value, bool):
        raise ValueError(f"function arity for {name!r} must not be a boolean")
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
        return minimum, maximum
    raise ValueError(f"function arity for {name!r} must be an integer or a (min, max) pair")


def _minimum_arity(arity: FunctionArity) -> int:
    return arity if isinstance(arity, int) else arity[0]


def _maximum_arity(arity: FunctionArity) -> int | None:
    return arity if isinstance(arity, int) else arity[1]


def _arity_range(arities: Sequence[FunctionArity]) -> tuple[int, int | None]:
    minimum = min(_minimum_arity(arity) for arity in arities)
    maximum: int | None = 0
    for arity in arities:
        top = _maximum_arity(arity)
        if top is None:
            return minimum, None
        assert maximum is not None
        maximum = max(maximum, top)
    return minimum, maximum


def _arity_matches(arity: FunctionArity, requested: int) -> bool:
    if isinstance(arity, int):
        return arity == requested
    minimum, maximum = arity
    return minimum <= requested and (maximum is None or requested <= maximum)


def _precedence_compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """Compare import precedence: a shorter prefix is higher; later imports win."""
    if left == right:
        return 0
    limit = min(len(left), len(right))
    if left[:limit] == right[:limit]:
        return 1 if len(left) < len(right) else -1
    return 1 if left > right else -1


@dataclass(frozen=True, slots=True)
class ProfileOptions:
    """Processor profile and implementation-defined static options."""

    processor_profile: str = "xslt-3.0"
    schema_aware: bool = True
    streaming: bool = False
    compatibility_mode: bool | None = None
    static_options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.processor_profile not in _PROFILES:
            raise ValueError(f"unsupported processor profile {self.processor_profile!r}")
        if not isinstance(self.schema_aware, bool) or not isinstance(self.streaming, bool):
            raise TypeError("profile schema_aware and streaming options must be booleans")
        if self.compatibility_mode is not None and not isinstance(self.compatibility_mode, bool):
            raise TypeError("compatibility_mode must be a boolean or None")
        options = _freeze(self.static_options, "static options")
        for name in options:
            if not isinstance(name, str):
                raise TypeError("static option names must be strings")
        object.__setattr__(self, "static_options", options)

    def with_option(self, name: str, value: Any) -> ProfileOptions:
        if not isinstance(name, str) or not name:
            raise ValueError("a static option requires a non-empty string name")
        return replace(self, static_options={**self.static_options, name: value})


@dataclass(frozen=True, slots=True)
class StaticVariable:
    """A visible global variable or parameter and its declared type."""

    name: str
    kind: VariableKind = "variable"
    static: bool = False
    visibility: str = "public"
    sequence_type: CompiledSequenceType | None = None
    declared_type: str | None = None
    precedence: tuple[int, ...] = ()
    module: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("a static variable requires a non-empty name")
        if self.kind not in ("variable", "param"):
            raise ValueError(f"invalid static variable kind {self.kind!r}")
        if not isinstance(self.static, bool):
            raise TypeError("static must be a boolean")
        if self.visibility not in _VISIBILITIES:
            raise ValueError(f"invalid visibility {self.visibility!r}")
        if self.sequence_type is not None and not isinstance(
            self.sequence_type, CompiledSequenceType
        ):
            raise TypeError("sequence_type must be a CompiledSequenceType or None")
        if self.declared_type is not None and not isinstance(self.declared_type, str):
            raise TypeError("declared_type must be a string or None")
        if not isinstance(self.precedence, tuple) or not all(
            isinstance(step, int) for step in self.precedence
        ):
            raise TypeError("precedence must be a tuple of integers")
        if self.module is not None and not isinstance(self.module, str):
            raise TypeError("module must be a string or None")


@dataclass(frozen=True, slots=True)
class StaticFunction:
    """A visible stylesheet function by expanded name and arity."""

    name: str
    arity: FunctionArity
    visibility: str = "public"
    parameters: tuple[CompiledSequenceType, ...] = ()
    return_type: CompiledSequenceType | None = None
    precedence: tuple[int, ...] = ()
    module: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("a static function requires a non-empty name")
        object.__setattr__(self, "arity", _normalise_arity(self.arity, self.name))
        if self.visibility not in _VISIBILITIES:
            raise ValueError(f"invalid visibility {self.visibility!r}")
        if not isinstance(self.parameters, tuple) or not all(
            isinstance(parameter, CompiledSequenceType) for parameter in self.parameters
        ):
            raise TypeError("function parameters must be CompiledSequenceType values")
        if self.return_type is not None and not isinstance(self.return_type, CompiledSequenceType):
            raise TypeError("return_type must be a CompiledSequenceType or None")
        if not isinstance(self.precedence, tuple) or not all(
            isinstance(step, int) for step in self.precedence
        ):
            raise TypeError("precedence must be a tuple of integers")
        if self.module is not None and not isinstance(self.module, str):
            raise TypeError("module must be a string or None")

    def matches(self, requested: int) -> bool:
        return _arity_matches(self.arity, requested)


@dataclass(frozen=True, slots=True)
class StaticContext:
    """One element's immutable, module-local XSLT static context."""

    namespaces: Mapping[str, str] = field(default_factory=dict)
    xpath_default_namespace: str | None = None
    base_uri: str | None = None
    version: str = "3.0"
    schema: SchemaPool | None = None
    variables: Mapping[str, StaticVariable] = field(default_factory=dict)
    functions: Mapping[FunctionKey, StaticFunction] = field(default_factory=dict)
    default_collation: str | None = None
    document_types: Mapping[str, str] = field(default_factory=dict)
    collection_types: Mapping[str, str] = field(default_factory=dict)
    default_collection_type: str | None = None
    profile: ProfileOptions = field(default_factory=ProfileOptions)
    expand_text: bool = False
    default_validation: str | None = None
    input_type_annotations: str | None = None
    uri: str | None = None
    location: IssueLocation | None = None
    module: str | None = None

    def __post_init__(self) -> None:
        namespaces = dict(BUILT_IN_NAMESPACES)
        namespaces.update(_freeze(self.namespaces, "namespaces"))
        for prefix, uri in namespaces.items():
            if not isinstance(prefix, str) or not isinstance(uri, str):
                raise TypeError("namespace prefixes and URIs must be strings")
        object.__setattr__(self, "namespaces", MappingProxyType(namespaces))

        version = _validate_version(self.version, uri=self.uri, location=self.location)
        object.__setattr__(self, "version", version)

        if self.schema is not None and not isinstance(self.schema, SchemaPool):
            raise TypeError("schema must be a SchemaPool or None")
        if not isinstance(self.profile, ProfileOptions):
            raise TypeError("profile must be a ProfileOptions value")
        if not isinstance(self.expand_text, bool):
            raise TypeError("expand_text must be a boolean")

        variables = _freeze(self.variables, "variables")
        for name, variable in variables.items():
            if not isinstance(variable, StaticVariable):
                raise TypeError("variables must contain StaticVariable values")
            if name != variable.name:
                raise ValueError(
                    f"variable mapping key {name!r} does not match its name {variable.name!r}"
                )
        object.__setattr__(self, "variables", variables)

        functions = _freeze(self.functions, "functions")
        for key, function in functions.items():
            if not isinstance(function, StaticFunction):
                raise TypeError("functions must contain StaticFunction values")
            if not isinstance(key, tuple) or key != (function.name, function.arity):
                raise ValueError(f"function mapping key {key!r} does not match its declaration")
        object.__setattr__(self, "functions", functions)

        for label, value in (
            ("default_collation", self.default_collation),
            ("xpath_default_namespace", self.xpath_default_namespace),
            ("base_uri", self.base_uri),
            ("default_collection_type", self.default_collection_type),
            ("uri", self.uri),
            ("module", self.module),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{label} must be a string or None")
        if self.location is not None and not isinstance(self.location, IssueLocation):
            raise TypeError("location must be an IssueLocation or None")

        for label, mapping in (
            ("document types", self.document_types),
            ("collection types", self.collection_types),
        ):
            frozen = _freeze(mapping, label)
            for key, value in frozen.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise TypeError(f"{label} must map strings to strings")
            object.__setattr__(
                self,
                "document_types" if label == "document types" else "collection_types",
                frozen,
            )

        if self.default_validation is not None and self.default_validation not in _VALIDATION_MODES:
            raise XsltStaticError(
                f"invalid default-validation value {self.default_validation!r}",
                code="XTSE0020",
                uri=self.uri,
                location=self.location,
            )
        if (
            self.input_type_annotations is not None
            and self.input_type_annotations not in _INPUT_TYPE_ANNOTATIONS
        ):
            raise XsltStaticError(
                f"invalid input-type-annotations value {self.input_type_annotations!r}",
                code="XTSE0020",
                uri=self.uri,
                location=self.location,
            )

    # -- views and lookups -----------------------------------------------------

    @property
    def default_namespace(self) -> str | None:
        """Alias matching the standalone XPath static context."""
        return self.xpath_default_namespace

    @property
    def xpath_version(self) -> str:
        return _xpath_version(self.version)

    @property
    def compatibility_mode(self) -> bool:
        if self.profile.compatibility_mode is not None:
            return self.profile.compatibility_mode
        return LanguageProfile.from_options(self.profile, self.version).compatibility_mode

    def variable(self, name: str) -> StaticVariable | None:
        return self.variables.get(name)

    def function(self, name: str, arity: int) -> StaticFunction | None:
        direct = self.functions.get((name, arity))
        if direct is not None:
            return direct
        for (declared_name, declared_arity), function in self.functions.items():
            if declared_name == name and _arity_matches(declared_arity, arity):
                return function
        return None

    def function_arities(self, name: str) -> tuple[int, int | None] | tuple[()]:
        arities = [
            declared for (declared_name, declared) in self.functions if declared_name == name
        ]
        if not arities:
            return ()
        return _arity_range(arities)

    def static_option(self, name: str, default: Any = None) -> Any:
        return self.profile.static_options.get(name, default)

    def document_type(self, uri: str) -> str | None:
        return self.document_types.get(uri)

    def collection_type(self, uri: str) -> str | None:
        return self.collection_types.get(uri)

    def xpath_static_context(self) -> XPathStaticContext:
        """Build the immutable Task 11 static context for this element."""
        grouped: dict[str, list[FunctionArity]] = {}
        for name, arity in self.functions:
            grouped.setdefault(name, []).append(arity)
        functions: dict[str, FunctionArity] = {}
        for name, arities in grouped.items():
            if not name.startswith("{") and ":" not in name:
                # XPath host declarations need a namespace; an unqualified
                # stylesheet function name never compiles as an expression call.
                continue
            if name.startswith("{"):
                namespace = name[1:].partition("}")[0]
                if not any(
                    prefix and bound == namespace for prefix, bound in self.namespaces.items()
                ):
                    # A Clark name with no in-scope prefix cannot be registered
                    # on the elementpath parser; it is not callable lexically.
                    continue
            if len(arities) == 1:
                functions[name] = arities[0]
                continue
            # XPathStaticContext carries one arity per name; exact overloads
            # are represented by the smallest covering range.
            functions[name] = _arity_range(arities)
        for name, arity in xslt_function_declarations(self.version).items():
            functions.setdefault(name, arity)
        variable_types = {
            name: str(variable.sequence_type)
            for name, variable in self.variables.items()
            if variable.sequence_type is not None
        }
        return XPathStaticContext(
            namespaces=dict(self.namespaces),
            default_namespace=self.xpath_default_namespace,
            variable_types=variable_types,
            functions=functions,
            schema=self.schema,
            base_uri=self.base_uri,
            default_collation=self.default_collation,
            xsd_version=self.schema.xsd_version if self.schema is not None else None,
            xslt_version=self.version,
        )

    # -- immutable updates -----------------------------------------------------

    def with_expand_text(self, value: bool) -> StaticContext:
        if not isinstance(value, bool):
            raise TypeError("expand_text must be a boolean")
        return replace(self, expand_text=value)

    def with_version(self, value: str) -> StaticContext:
        return replace(self, version=_validate_version(value, uri=self.uri))

    def with_namespaces(self, namespaces: Mapping[str, str]) -> StaticContext:
        return replace(self, namespaces=namespaces)

    def with_namespace(self, prefix: str, uri: str) -> StaticContext:
        if not isinstance(prefix, str) or not isinstance(uri, str):
            raise TypeError("namespace prefixes and URIs must be strings")
        return replace(self, namespaces={**self.namespaces, prefix: uri})

    def with_xpath_default_namespace(self, value: str | None) -> StaticContext:
        return replace(self, xpath_default_namespace=value)

    def with_base_uri(self, value: str | None) -> StaticContext:
        return replace(self, base_uri=value)

    def with_schema(self, value: SchemaPool | None) -> StaticContext:
        return replace(self, schema=value)

    def with_variables(self, variables: Mapping[str, StaticVariable]) -> StaticContext:
        return replace(self, variables=variables)

    def with_variable(self, variable: StaticVariable) -> StaticContext:
        return replace(self, variables={**self.variables, variable.name: variable})

    def with_functions(self, functions: Mapping[FunctionKey, StaticFunction]) -> StaticContext:
        return replace(self, functions=functions)

    def with_function(self, function: StaticFunction) -> StaticContext:
        key = (function.name, function.arity)
        return replace(self, functions={**self.functions, key: function})

    def with_default_collation(self, value: str | None) -> StaticContext:
        return replace(self, default_collation=value)

    def with_document_types(self, value: Mapping[str, str]) -> StaticContext:
        return replace(self, document_types=value)

    def with_collection_types(self, value: Mapping[str, str]) -> StaticContext:
        return replace(self, collection_types=value)

    def with_default_collection_type(self, value: str | None) -> StaticContext:
        return replace(self, default_collection_type=value)

    def with_profile(self, profile: ProfileOptions) -> StaticContext:
        return replace(self, profile=profile)

    def with_static_option(self, name: str, value: Any) -> StaticContext:
        return replace(self, profile=self.profile.with_option(name, value))

    def with_uri(self, value: str | None) -> StaticContext:
        return replace(self, uri=value)

    def with_location(self, value: IssueLocation | None) -> StaticContext:
        return replace(self, location=value)

    def with_default_validation(self, value: str | None) -> StaticContext:
        return replace(self, default_validation=value)

    def with_input_type_annotations(self, value: str | None) -> StaticContext:
        return replace(self, input_type_annotations=value)

    def for_child(self, element: XdmNode | EffectiveElement) -> StaticContext:
        return type(self).for_element(element, parent=self)

    # -- construction ----------------------------------------------------------

    @classmethod
    def for_element(
        cls,
        element: XdmNode | EffectiveElement,
        *,
        module: EffectiveModule | None = None,
        parent: StaticContext | None = None,
        schema: SchemaPool | None = None,
        profile: ProfileOptions | None = None,
        variables: Mapping[str, StaticVariable] | None = None,
        functions: Mapping[FunctionKey, StaticFunction] | None = None,
        location: IssueLocation | None = None,
    ) -> StaticContext:
        """Build the context for one effective or source-faithful element."""
        if module is not None and parent is not None:
            raise TypeError("module and parent are mutually exclusive")
        if module is not None and not isinstance(module, EffectiveModule):
            raise TypeError("module must be an EffectiveModule")
        if parent is not None and not isinstance(parent, StaticContext):
            raise TypeError("parent must be a StaticContext")
        fallback: StaticContext | None = parent
        if module is not None:
            fallback = cls.for_module(module, schema=schema, profile=profile)
        context = cls._from_element(
            element,
            fallback=fallback,
            schema=schema,
            profile=profile,
            module_identity=module.identity if module is not None else None,
            location=location,
        )
        if variables is not None:
            context = context.with_variables(variables)
        if functions is not None:
            context = context.with_functions(functions)
        return context

    @classmethod
    def for_module(
        cls,
        module: EffectiveModule,
        *,
        schema: SchemaPool | None = None,
        profile: ProfileOptions | None = None,
    ) -> StaticContext:
        """Build a module-local context with its visible global symbols."""
        if not isinstance(module, EffectiveModule):
            raise TypeError("module must be an EffectiveModule")
        base = cls._from_element(
            module.root,
            fallback=None,
            schema=schema,
            profile=profile,
            module_identity=module.identity,
        )
        variables, functions = _collect_symbols(
            module,
            schema=base.schema,
            profile=base.profile,
        )
        return replace(base, variables=variables, functions=functions)

    @classmethod
    def _from_element(
        cls,
        element: XdmNode | EffectiveElement,
        *,
        fallback: StaticContext | None,
        schema: SchemaPool | None,
        profile: ProfileOptions | None,
        module_identity: str | None,
        location: IssueLocation | None = None,
    ) -> StaticContext:
        defaults = _element_defaults(element, fallback)
        effective_schema = (
            schema if schema is not None else (fallback.schema if fallback is not None else None)
        )
        effective_profile = (
            profile
            if profile is not None
            else (fallback.profile if fallback is not None else ProfileOptions())
        )
        return cls(
            namespaces=defaults.namespaces,
            xpath_default_namespace=defaults.xpath_default_namespace,
            base_uri=defaults.base_uri,
            version=defaults.version,
            schema=effective_schema,
            variables=fallback.variables if fallback is not None else {},
            functions=fallback.functions if fallback is not None else {},
            default_collation=defaults.default_collation,
            document_types=fallback.document_types if fallback is not None else {},
            collection_types=fallback.collection_types if fallback is not None else {},
            default_collection_type=(
                fallback.default_collection_type if fallback is not None else None
            ),
            profile=effective_profile,
            expand_text=defaults.expand_text,
            default_validation=defaults.default_validation,
            input_type_annotations=defaults.input_type_annotations,
            uri=defaults.uri,
            location=location or defaults.location,
            module=(
                module_identity
                if module_identity is not None
                else defaults.module
                if defaults.module is not None
                else fallback.module
                if fallback is not None
                else None
            ),
        )


# ---------------------------------------------------------------------------
# Effective element defaults


@dataclass(frozen=True, slots=True)
class _ElementDefaults:
    namespaces: Mapping[str, str]
    xpath_default_namespace: str | None
    base_uri: str | None
    version: str
    expand_text: bool
    default_collation: str | None
    default_validation: str | None
    input_type_annotations: str | None
    uri: str | None
    location: IssueLocation | None
    module: str | None


def _document_uri(element: XdmNode | EffectiveElement) -> str | None:
    raw = element.raw if isinstance(element, EffectiveElement) else element
    if raw is None:
        return None
    return raw.document.document_uri


def _source_location(element: XdmNode | EffectiveElement) -> IssueLocation | None:
    return element.source_location()


def _ancestor_nodes(element: XdmNode | EffectiveElement) -> tuple[XdmNode, ...]:
    raw = element.raw if isinstance(element, EffectiveElement) else element
    if raw is None:
        return ()
    return (raw, *raw.ancestors)


def _applies_to(node: XdmNode | EffectiveElement, name: str) -> bool:
    """Whether *name* is a standard attribute spelling on this element."""
    return name.startswith("{") or node.namespace_uri == XSLT_NS


def _nearest_attribute(
    element: XdmNode | EffectiveElement,
    names: Sequence[str],
) -> tuple[bool, str | None]:
    if isinstance(element, EffectiveElement):
        for name in names:
            if not _applies_to(element.raw or element, name):
                continue
            value = element.attribute(name)
            if value is not None:
                return True, value
    for node in _ancestor_nodes(element):
        for name in names:
            if not _applies_to(node, name):
                continue
            attribute = node.attribute(name)
            if attribute is not None:
                return True, attribute.string_value
    return False, None


def _parse_yes_no(
    value: str,
    element: XdmNode | EffectiveElement,
    attribute: str,
) -> bool:
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    raise XsltStaticError(
        f"the {attribute} attribute has an invalid value {value!r}",
        code="XTSE0020",
        uri=_document_uri(element),
        location=_source_location(element),
    )


def _element_defaults(
    element: XdmNode | EffectiveElement,
    fallback: StaticContext | None,
) -> _ElementDefaults:
    if isinstance(element, EffectiveElement):
        namespaces = element.in_scope_namespaces
        base_uri = element.base_uri
        source_uri = _document_uri(element)
    elif isinstance(element, XdmNode):
        if element.kind is not NodeKind.ELEMENT:
            raise TypeError("a static context belongs to an element")
        namespaces = element.in_scope_namespaces
        base_uri = element.base_uri
        source_uri = element.document.document_uri
    else:
        raise TypeError("element must be an XdmNode or EffectiveElement")

    fallback_version = fallback.version if fallback is not None else "3.0"
    found, version_value = _nearest_attribute(element, ("version", _VERSION))
    version = _validate_version(
        version_value if found and version_value is not None else fallback_version,
        uri=source_uri,
        location=_source_location(element),
    )

    found, default_value = _nearest_attribute(element, ("xpath-default-namespace", _XPATH_DEFAULT))
    if found:
        xpath_default = default_value or None
    else:
        xpath_default = fallback.xpath_default_namespace if fallback is not None else None

    found, expand_value = _nearest_attribute(element, ("expand-text", _EXPAND_TEXT))
    if Decimal(version) < Decimal("3.0"):
        expand_text = False
    elif found:
        expand_text = _parse_yes_no(expand_value or "", element, "expand-text")
    else:
        expand_text = fallback.expand_text if fallback is not None else False

    found, collation_value = _nearest_attribute(element, ("default-collation", _DEFAULT_COLLATION))
    if found:
        collation = (collation_value or "").split()
        default_collation = collation[0] if collation else None
    else:
        default_collation = fallback.default_collation if fallback is not None else None

    found, validation_value = _nearest_attribute(
        element, ("default-validation", _DEFAULT_VALIDATION)
    )
    if found:
        default_validation = validation_value or None
        if default_validation is not None and default_validation not in _VALIDATION_MODES:
            raise XsltStaticError(
                f"invalid default-validation value {default_validation!r}",
                code="XTSE0020",
                uri=source_uri,
                location=_source_location(element),
            )
    else:
        default_validation = fallback.default_validation if fallback is not None else None

    found, annotations_value = _nearest_attribute(
        element, ("input-type-annotations", _INPUT_TYPE_ANNOTATIONS_ATTR)
    )
    if found:
        input_type_annotations = annotations_value or None
        if (
            input_type_annotations is not None
            and input_type_annotations not in _INPUT_TYPE_ANNOTATIONS
        ):
            raise XsltStaticError(
                f"invalid input-type-annotations value {input_type_annotations!r}",
                code="XTSE0020",
                uri=source_uri,
                location=_source_location(element),
            )
    else:
        input_type_annotations = fallback.input_type_annotations if fallback is not None else None

    effective_namespaces = namespaces
    if not effective_namespaces and fallback is not None:
        effective_namespaces = fallback.namespaces
    return _ElementDefaults(
        namespaces=effective_namespaces,
        xpath_default_namespace=xpath_default,
        base_uri=base_uri
        if base_uri is not None
        else fallback.base_uri
        if fallback is not None
        else None,
        version=version,
        expand_text=expand_text,
        default_collation=default_collation,
        default_validation=default_validation,
        input_type_annotations=input_type_annotations,
        uri=source_uri
        if source_uri is not None
        else fallback.uri
        if fallback is not None
        else None,
        location=_source_location(element) or (fallback.location if fallback is not None else None),
        module=_document_uri(element) or (fallback.module if fallback is not None else None),
    )


# ---------------------------------------------------------------------------
# Module symbols


def _expand_declared_name(
    value: str,
    namespaces: Mapping[str, str],
    element: EffectiveElement,
) -> str:
    name = value.strip()
    if not name:
        raise XsltStaticError(
            "a declared name must not be empty",
            code="XTSE0010",
            uri=_document_uri(element),
            location=_source_location(element),
        )
    if name.startswith("Q{"):
        close = name.find("}")
        if close < 0 or close + 1 >= len(name):
            raise XsltStaticError(
                f"the expanded name {name!r} is not a valid EQName",
                code="XTSE0010",
                uri=_document_uri(element),
                location=_source_location(element),
            )
        uri = name[2:close]
        local = name[close + 1 :]
        return local if not uri else f"{{{uri}}}{local}"
    if name.startswith("{"):
        return name
    prefix, separator, local = name.partition(":")
    if not separator:
        return name
    namespace_uri = namespaces.get(prefix)
    if namespace_uri is None:
        raise XsltStaticError(
            f"the prefix {prefix!r} is not bound in the declaration's namespace context",
            code="XTSE0280",
            uri=_document_uri(element),
            location=_source_location(element),
        )
    return f"{{{namespace_uri}}}{local}"


def _declaration_visibility(
    element: EffectiveElement,
    default_visibility: str,
) -> str:
    declared = element.attribute("visibility")
    visibility = default_visibility if declared is None else declared
    if visibility not in _VISIBILITIES:
        raise XsltStaticError(
            f"invalid visibility value {visibility!r}",
            code="XTSE0020",
            uri=_document_uri(element),
            location=_source_location(element),
        )
    return visibility


def _declaration_context(
    element: EffectiveElement,
    *,
    schema: SchemaPool | None,
    profile: ProfileOptions,
) -> StaticContext:
    return StaticContext._from_element(
        element,
        fallback=None,
        schema=schema,
        profile=profile,
        module_identity=_document_uri(element),
    )


def _compile_declared_type(
    element: EffectiveElement,
    *,
    schema: SchemaPool | None,
    profile: ProfileOptions,
) -> CompiledSequenceType | None:
    declared = element.attribute("as")
    if declared is None:
        return None
    context = _declaration_context(element, schema=schema, profile=profile)
    return compile_sequence_type(declared, context)


def _default_item_sequence(
    element: EffectiveElement,
    *,
    schema: SchemaPool | None,
    profile: ProfileOptions,
) -> CompiledSequenceType:
    context = _declaration_context(element, schema=schema, profile=profile)
    return compile_sequence_type("item()*", context)


def _variable_symbol(
    element: EffectiveElement,
    module: EffectiveModule,
    *,
    schema: SchemaPool | None,
    profile: ProfileOptions,
    default_visibility: str,
) -> StaticVariable | None:
    name = element.attribute("name")
    if not name:
        return None
    expanded = _expand_declared_name(name, element.in_scope_namespaces, element)
    sequence_type = _compile_declared_type(element, schema=schema, profile=profile)
    static_value = element.attribute("static")
    static = bool(static_value in ("yes", "true", "1"))
    kind: VariableKind = "param" if element.name == _PARAM else "variable"
    if kind == "param":
        # A static stylesheet parameter is implicitly private; an ordinary
        # global parameter is not a visibility-bearing component and remains
        # public.
        visibility = "private" if static else "public"
    else:
        visibility = _declaration_visibility(element, default_visibility)
    return StaticVariable(
        name=expanded,
        kind=kind,
        static=static,
        visibility=visibility,
        sequence_type=sequence_type,
        declared_type=element.attribute("as"),
        precedence=module.precedence,
        module=module.identity,
    )


def _function_symbol(
    element: EffectiveElement,
    module: EffectiveModule,
    *,
    schema: SchemaPool | None,
    profile: ProfileOptions,
    default_visibility: str,
) -> StaticFunction | None:
    name = element.attribute("name")
    if not name:
        return None
    expanded = _expand_declared_name(name, element.in_scope_namespaces, element)
    parameters = tuple(
        _compile_declared_type(parameter, schema=schema, profile=profile)
        or _default_item_sequence(parameter, schema=schema, profile=profile)
        for parameter in element.elements
        if parameter.name == _PARAM
    )
    return_type = _compile_declared_type(element, schema=schema, profile=profile)
    if return_type is None:
        return_type = _default_item_sequence(element, schema=schema, profile=profile)
    return StaticFunction(
        name=expanded,
        arity=len(parameters),
        visibility=_declaration_visibility(element, default_visibility),
        parameters=parameters,
        return_type=return_type,
        precedence=module.precedence,
        module=module.identity,
    )


def _collect_symbols(
    module: EffectiveModule,
    *,
    schema: SchemaPool | None,
    profile: ProfileOptions,
) -> tuple[Mapping[str, StaticVariable], Mapping[FunctionKey, StaticFunction]]:
    variables: dict[str, StaticVariable] = {}
    functions: dict[FunctionKey, StaticFunction] = {}
    visited: set[int] = set()

    # A stylesheet module graph is one implicit package assembled through
    # xsl:include and xsl:import, so private declarations remain visible here.
    # An explicit xsl:package root starts a package whose own graph defaults
    # to private.  Only xsl:use-package creates an external package boundary
    # (Task 15).
    def record(current: EffectiveModule, default_visibility: str) -> None:
        for child in current.root.elements:
            if child.name in (_VARIABLE, _PARAM):
                variable = _variable_symbol(
                    child,
                    current,
                    schema=schema,
                    profile=profile,
                    default_visibility=default_visibility,
                )
                if variable is not None:
                    previous = variables.get(variable.name)
                    if (
                        previous is None
                        or _precedence_compare(variable.precedence, previous.precedence) > 0
                    ):
                        variables[variable.name] = variable
            elif child.name == _FUNCTION:
                function = _function_symbol(
                    child,
                    current,
                    schema=schema,
                    profile=profile,
                    default_visibility=default_visibility,
                )
                if function is not None:
                    key = (function.name, function.arity)
                    previous_function = functions.get(key)
                    if (
                        previous_function is None
                        or _precedence_compare(function.precedence, previous_function.precedence)
                        > 0
                    ):
                        functions[key] = function

    def visit(current: EffectiveModule, default_visibility: str) -> None:
        if id(current) in visited:
            return
        visited.add(id(current))
        if current.root.local_name == "package":
            default_visibility = "private"
        record(current, default_visibility)
        for included in current.includes:
            visit(included, default_visibility)
        for imported in current.imports:
            visit(imported, default_visibility)

    visit(module, "private" if module.root.local_name == "package" else "public")
    return MappingProxyType(variables), MappingProxyType(functions)


__all__ = [
    "ProfileOptions",
    "StaticContext",
    "StaticFunction",
    "StaticVariable",
]
