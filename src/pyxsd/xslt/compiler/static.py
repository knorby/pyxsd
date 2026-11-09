"""Normative XSLT static preprocessing over source-faithful module trees.

The effective module is a filtered view of the raw source tree, not a second
independent XML parse.  It applies, in order:

* the stylesheet whitespace rules of XSLT 3.0 section 4.3;
* ``use-when`` conditional inclusion and root-level exclusion;
* shadow attributes (``_name``) as static value templates;
* static variables and parameters in their restricted static context; and
* simplified-stylesheet normalization.

Comments and processing instructions disappear from the effective view (the
raw module keeps them).  Literal result elements keep their original
namespace bindings and base URI, and a shadow attribute is replaced by its
computed target attribute so it can never be re-evaluated later as an ordinary
attribute value template.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, TypeAlias

from pyxsd import __version__
from pyxsd.locations import IssueLocation
from pyxsd.xdm import XdmAtomic, XdmSequence, as_xdm
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, XPathStaticContext
from pyxsd.xslt.compiler.modules import ModuleKind, SourceModule
from pyxsd.xslt.errors import XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XML_NS = "http://www.w3.org/XML/1998/namespace"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
FN_NS = "http://www.w3.org/2005/xpath-functions"

_STYLESHEET = f"{{{XSLT_NS}}}stylesheet"
_TEMPLATE = f"{{{XSLT_NS}}}template"
_USE_WHEN = f"{{{XSLT_NS}}}use-when"
_XPATH_DEFAULT = f"{{{XSLT_NS}}}xpath-default-namespace"
_XML_SPACE = f"{{{XML_NS}}}space"

_ALWAYS_STRIP_PARENTS = frozenset(
    f"{{{XSLT_NS}}}{name}"
    for name in (
        "accumulator",
        "analyze-string",
        "apply-imports",
        "apply-templates",
        "attribute-set",
        "call-template",
        "character-map",
        "choose",
        "evaluate",
        "fork",
        "merge",
        "merge-source",
        "mode",
        "next-iteration",
        "next-match",
        "override",
        "package",
        "stylesheet",
        "transform",
        "use-package",
    )
)
_STRIP_BEFORE = frozenset(
    f"{{{XSLT_NS}}}{name}" for name in ("param", "sort", "context-item", "on-completion")
)
_STRIP_AFTER_CATCH = f"{{{XSLT_NS}}}catch"

_KNOWN_XSLT_ELEMENTS = frozenset(
    {
        "accept",
        "accumulator",
        "accumulator-rule",
        "analysis",
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
        "iterate",
        "key",
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
        "stream",
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
    }
)

_KNOWN_XSD_TYPES = frozenset(
    {
        "ENTITIES",
        "ENTITY",
        "ID",
        "IDREF",
        "IDREFS",
        "NCName",
        "NMTOKEN",
        "NMTOKENS",
        "NOTATION",
        "Name",
        "QName",
        "anyAtomicType",
        "anySimpleType",
        "anyType",
        "anyURI",
        "base64Binary",
        "boolean",
        "byte",
        "date",
        "dateTime",
        "dateTimeStamp",
        "dayTimeDuration",
        "decimal",
        "double",
        "duration",
        "float",
        "gDay",
        "gMonth",
        "gMonthDay",
        "gYear",
        "gYearMonth",
        "hexBinary",
        "int",
        "integer",
        "language",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "normalizedString",
        "positiveInteger",
        "short",
        "string",
        "time",
        "token",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
        "untyped",
        "untypedAtomic",
        "yearMonthDuration",
    }
)

_KNOWN_FUNCTIONS = frozenset(
    {
        "available-system-properties",
        "boolean",
        "codepoints-to-string",
        "concat",
        "contains",
        "count",
        "current",
        "current-date",
        "current-dateTime",
        "current-group",
        "current-grouping-key",
        "current-time",
        "element-available",
        "empty",
        "ends-with",
        "exists",
        "false",
        "format-date",
        "format-dateTime",
        "format-number",
        "format-time",
        "function-available",
        "generate-id",
        "key",
        "local-name",
        "lower-case",
        "matches",
        "name",
        "namespace-uri",
        "normalize-space",
        "not",
        "number",
        "position",
        "replace",
        "reverse",
        "root",
        "round",
        "starts-with",
        "string",
        "string-length",
        "string-to-codepoints",
        "subsequence",
        "substring",
        "substring-after",
        "substring-before",
        "sum",
        "system-property",
        "tokenize",
        "translate",
        "true",
        "type-available",
        "unparsed-entity-uri",
        "upper-case",
    }
)


_MAP_NS = "http://www.w3.org/2005/xpath-functions/map"
_ARRAY_NS = "http://www.w3.org/2005/xpath-functions/array"
_MATH_NS = "http://www.w3.org/2005/xpath-functions/math"

_KNOWN_MAP_FUNCTIONS = frozenset(
    {
        "contains",
        "entry",
        "find",
        "for-each",
        "get",
        "keys",
        "merge",
        "put",
        "remove",
        "size",
    }
)
_KNOWN_ARRAY_FUNCTIONS = frozenset(
    {
        "append",
        "filter",
        "flatten",
        "fold-left",
        "fold-right",
        "for-each",
        "for-each-pair",
        "get",
        "head",
        "insert-before",
        "join",
        "put",
        "remove",
        "reverse",
        "size",
        "sort",
        "subarray",
        "tail",
    }
)
_KNOWN_MATH_FUNCTIONS = frozenset(
    {
        "acos",
        "asin",
        "atan",
        "atan2",
        "cos",
        "exp",
        "exp10",
        "log",
        "log10",
        "pi",
        "pow",
        "sin",
        "sqrt",
        "tan",
    }
)

_SYSTEM_PROPERTIES = MappingProxyType(
    {
        "xsl:version": "3.0",
        "xsl:vendor": "pyxsd",
        "xsl:vendor-url": "https://pyxsd.knorby.com",
        "xsl:product-name": "pyxsd",
        "xsl:product-version": __version__,
        "xsl:is-schema-aware": "yes",
        "xsl:supports-serialization": "yes",
        "xsl:supports-backwards-compatibility": "yes",
        "xsl:supports-namespace-axis": "yes",
        "xsl:supports-streaming": "no",
        "xsl:supports-dynamic-evaluation": "yes",
        "xsl:supports-higher-order-functions": "yes",
        "xsl:supports-xpath-3.1": "yes",
        "xsl:xpath-version": "3.1",
        "xsl:xsd-version": "1.1",
    }
)

_STATIC_FUNCTIONS: Mapping[str, Any] = MappingProxyType(
    {
        "fn:system-property": 1,
        "fn:function-available": (1, 2),
        "fn:element-available": 1,
        "fn:type-available": 1,
        "fn:available-system-properties": 0,
    }
)

_CODES = re.compile(r"\[(?:err:)?([A-Z]{4}[0-9]{4})\]")


def _local_name(name: str) -> str:
    if name.startswith("{"):
        return name.partition("}")[2]
    return name


def _namespace_of(name: str) -> str | None:
    if name.startswith("{"):
        return name[1:].partition("}")[0]
    return None


def _name_of(node: XdmNode) -> str:
    name = node.name
    assert name is not None
    return name


def _error_code(exc: BaseException, default: str = "XTSE0010") -> str:
    match = _CODES.search(str(exc))
    return default if match is None else match.group(1)


def _ebv(sequence: XdmSequence, element: XdmNode) -> bool:
    """Evaluate the effective boolean value of one static expression result."""
    if not sequence:
        return False
    first = sequence[0]
    if isinstance(first, XdmNode):
        return True
    if len(sequence) != 1 or not isinstance(first, XdmAtomic):
        raise XsltStaticError(
            "a static expression used as a condition must produce one atomic value",
            code="XPTY0004",
            uri=element.document.document_uri,
            location=element.source_location(),
        )
    value = first.value
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and math.isnan(value):
            return False
        return value != 0
    return bool(value)


def _string_value(sequence: XdmSequence, element: XdmNode) -> str:
    parts: list[str] = []
    for item in sequence:
        if isinstance(item, XdmAtomic):
            parts.append(item.lexical)
        elif isinstance(item, XdmNode):
            parts.append(item.string_value)
        else:
            raise XsltStaticError(
                "a static value template cannot stringify this item",
                code="XPTY0004",
                uri=element.document.document_uri,
                location=element.source_location(),
            )
    return " ".join(parts)


def _expand_name(value: str, namespaces: Mapping[str, str], element: XdmNode) -> str:
    name = value.strip()
    if not name:
        raise XsltStaticError(
            "a static variable name must not be empty",
            code="XTSE0010",
            uri=element.document.document_uri,
            location=element.source_location(),
        )
    if name.startswith("Q{"):
        return "{" + name[2:]
    if name.startswith("{"):
        return name
    prefix, separator, local = name.partition(":")
    if not separator:
        return name
    uri = namespaces.get(prefix)
    if uri is None:
        raise XsltStaticError(
            f"the prefix {prefix!r} is not bound in the stylesheet namespace context",
            code="XTSE0280",
            uri=element.document.document_uri,
            location=element.source_location(),
        )
    return f"{{{uri}}}{local}"


def _lexical_name(expanded: str, namespaces: Mapping[str, str]) -> str:
    if not expanded.startswith("{"):
        return expanded
    uri, _, local = expanded[1:].partition("}")
    for prefix, bound in namespaces.items():
        if bound == uri and prefix:
            return f"{prefix}:{local}"
    return expanded


@dataclass(frozen=True, slots=True)
class EffectiveAttribute:
    """One attribute in the effective module.

    ``static`` marks a value produced by a shadow attribute; it is already the
    final string and must never be evaluated as an attribute value template.
    """

    name: str
    value: str
    raw: XdmNode | None = None
    static: bool = False

    @property
    def local_name(self) -> str | None:
        return _local_name(self.name)

    @property
    def namespace_uri(self) -> str | None:
        return _namespace_of(self.name)

    @property
    def prefix(self) -> str:
        return self.raw.prefix if self.raw is not None else ""

    def source_location(self) -> IssueLocation | None:
        return None if self.raw is None else self.raw.source_location()


@dataclass(frozen=True, slots=True)
class EffectiveText:
    """One effective text node with its raw source node retained."""

    value: str
    raw: XdmNode | None = None

    @property
    def kind(self) -> NodeKind:
        return NodeKind.TEXT

    @property
    def string_value(self) -> str:
        return self.value

    def source_location(self) -> IssueLocation | None:
        return None if self.raw is None else self.raw.source_location()


EffectiveNode: TypeAlias = "EffectiveElement | EffectiveText"


@dataclass(frozen=True, slots=True)
class EffectiveElement:
    """One immutable element in the effective module view."""

    name: str
    attributes: tuple[EffectiveAttribute, ...]
    children: tuple[EffectiveNode, ...]
    raw: XdmNode | None = None
    base_uri: str | None = None
    in_scope_namespaces: Mapping[str, str] = field(default_factory=dict)
    namespace_declarations: tuple[tuple[str, str | None], ...] = ()
    synthetic: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.in_scope_namespaces, MappingProxyType):
            object.__setattr__(
                self,
                "in_scope_namespaces",
                MappingProxyType(dict(self.in_scope_namespaces)),
            )

    @property
    def kind(self) -> NodeKind:
        return NodeKind.ELEMENT

    @property
    def local_name(self) -> str | None:
        return _local_name(self.name)

    @property
    def namespace_uri(self) -> str | None:
        return _namespace_of(self.name)

    @property
    def prefix(self) -> str:
        return self.raw.prefix if self.raw is not None else ""

    def attribute_node(self, name: str) -> EffectiveAttribute | None:
        expanded = name
        if not name.startswith("{") and ":" in name:
            prefix, _, local = name.partition(":")
            uri = self.in_scope_namespaces.get(prefix)
            if uri is not None:
                expanded = f"{{{uri}}}{local}"
        for attribute in self.attributes:
            if attribute.name == expanded:
                return attribute
        return None

    def attribute(self, name: str) -> str | None:
        attribute = self.attribute_node(name)
        return None if attribute is None else attribute.value

    @property
    def elements(self) -> tuple[EffectiveElement, ...]:
        return tuple(child for child in self.children if isinstance(child, EffectiveElement))

    @property
    def descendants(self) -> tuple[EffectiveNode, ...]:
        result: list[EffectiveNode] = []
        stack = list(reversed(self.children))
        while stack:
            node = stack.pop()
            result.append(node)
            if isinstance(node, EffectiveElement):
                stack.extend(reversed(node.children))
        return tuple(result)

    def source_location(self) -> IssueLocation | None:
        return None if self.raw is None else self.raw.source_location()

    def source_span(self) -> Any:
        return None if self.raw is None else self.raw.source_span()

    def find(self, name: str) -> EffectiveElement | None:
        for child in self.elements:
            if child.name == name or child.local_name == name or child.attribute("name") == name:
                return child
        return None


@dataclass(frozen=True, slots=True, eq=False)
class EffectiveModule:
    """The preprocessed view of one source module and its direct graph edges."""

    source: SourceModule
    root: EffectiveElement
    includes: tuple[EffectiveModule, ...]
    imports: tuple[EffectiveModule, ...]
    static_variables: Mapping[str, XdmSequence]
    simplified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.static_variables, MappingProxyType):
            object.__setattr__(
                self,
                "static_variables",
                MappingProxyType(dict(self.static_variables)),
            )

    @property
    def raw(self) -> SourceModule:
        return self.source

    @property
    def document(self) -> Any:
        return self.source.document

    @property
    def identity(self) -> str | None:
        return self.source.identity

    @property
    def uri(self) -> str | None:
        return self.source.identity

    @property
    def version(self) -> str | None:
        return self.attribute("version")

    def find(self, name: str) -> EffectiveElement | None:
        """Find a declaration by component name or element name, recursively."""
        seen: set[int] = set()

        def visit(module: EffectiveModule) -> EffectiveElement | None:
            if id(module) in seen:
                return None
            seen.add(id(module))
            direct = module.root.find(name)
            if direct is not None:
                return direct
            for descendant in module.root.descendants:
                if not isinstance(descendant, EffectiveElement):
                    continue
                if (
                    descendant.name == name
                    or descendant.local_name == name
                    or descendant.attribute("name") == name
                ):
                    return descendant
            for child in (*module.includes, *module.imports):
                found = visit(child)
                if found is not None:
                    return found
            return None

        return visit(self)

    def attribute(self, name: str) -> str | None:
        return self.root.attribute(name)

    def attribute_node(self, name: str) -> EffectiveAttribute | None:
        return self.root.attribute_node(name)

    def source_location(self, name: str) -> IssueLocation | None:
        element = self.find(name)
        return None if element is None else element.source_location()

    @property
    def strip_space(self) -> tuple[str, ...]:
        return self._space_values("strip-space")

    @property
    def preserve_space(self) -> tuple[str, ...]:
        return self._space_values("preserve-space")

    def _space_values(self, local: str) -> tuple[str, ...]:
        values: list[str] = []
        seen: set[int] = set()

        def visit(module: EffectiveModule) -> None:
            if id(module) in seen:
                return
            seen.add(id(module))
            declaration_name = f"{{{XSLT_NS}}}{local}"
            for declaration in module.root.elements:
                if declaration.name != declaration_name:
                    continue
                value = declaration.attribute("elements")
                if value:
                    values.extend(value.split())
            for dependent in (*module.includes, *module.imports):
                visit(dependent)

        visit(self)
        return tuple(values)


@dataclass(frozen=True, slots=True)
class _Binding:
    kind: Literal["variable", "param"]
    value: XdmSequence


class _Scope:
    """Ordered static-variable bindings visible at one stylesheet position."""

    def __init__(self) -> None:
        self._bindings: dict[str, _Binding] = {}

    def declare(
        self,
        name: str,
        kind: Literal["variable", "param"],
        value: XdmSequence,
        element: XdmNode,
    ) -> None:
        previous = self._bindings.get(name)
        if previous is not None and (previous.kind != kind or previous.value != value):
            raise XsltStaticError(
                f"the static {kind} ${name} is inconsistent with its earlier declaration",
                code="XTSE3450",
                uri=element.document.document_uri,
                location=element.source_location(),
            )
        self._bindings[name] = _Binding(kind, value)

    def visible(self, element: XdmNode) -> dict[str, XdmSequence]:
        namespaces = element.in_scope_namespaces
        values: dict[str, XdmSequence] = {}
        for name, binding in self._bindings.items():
            values[name] = binding.value
            lexical = _lexical_name(name, namespaces)
            if lexical != name:
                values[lexical] = binding.value
        return values

    def snapshot(self) -> Mapping[str, XdmSequence]:
        return {name: binding.value for name, binding in self._bindings.items()}


def _system_property(name: str) -> str:
    return _SYSTEM_PROPERTIES.get(name, "")


class _StaticEvaluator:
    """Evaluate one restricted static expression for preprocessing."""

    def __init__(self, scope: _Scope) -> None:
        self._scope = scope
        self._engine = XPathEngine()

    def evaluate(
        self,
        expression: str,
        element: XdmNode,
        *,
        default_namespace: str | None = None,
    ) -> XdmSequence:
        namespaces = dict(element.in_scope_namespaces)
        namespaces.setdefault("fn", FN_NS)
        namespaces.setdefault("xs", XSD_NS)
        namespaces.setdefault("xsl", XSLT_NS)
        context = XPathStaticContext(
            namespaces=namespaces,
            default_namespace=default_namespace,
            functions=_STATIC_FUNCTIONS,
            base_uri=element.base_uri,
        )
        functions = {
            "fn:system-property": _system_property,
            "fn:function-available": lambda name, arity=None: self._function_available(
                name, element, arity
            ),
            "fn:element-available": lambda name: self._element_available(name, element),
            "fn:type-available": lambda name: self._type_available(name, element),
            "fn:available-system-properties": lambda: tuple(_SYSTEM_PROPERTIES),
        }
        environment = ExpressionEnvironment(
            variables=self._scope.visible(element),
            functions=functions,
            base_uri=element.base_uri,
        )
        try:
            compiled = self._engine.compile(expression, context)
            return self._engine.evaluate(compiled, environment, focus=Focus.absent())
        except Exception as exc:
            code = _error_code(exc)
            raise XsltStaticError(
                f"static expression failed: {exc}",
                code=code,
                uri=element.document.document_uri,
                location=element.source_location(),
                cause=exc,
            ) from exc

    def _function_available(self, name: str, element: XdmNode, arity: int | None = None) -> bool:
        del arity
        expanded = self._resolve_name(name, element, FN_NS)
        if expanded is None:
            return False
        if expanded.startswith(f"{{{FN_NS}}}"):
            return _local_name(expanded) in _KNOWN_FUNCTIONS
        if expanded.startswith(f"{{{XSD_NS}}}"):
            return _local_name(expanded) in _KNOWN_XSD_TYPES
        for namespace, names in (
            (_MAP_NS, _KNOWN_MAP_FUNCTIONS),
            (_ARRAY_NS, _KNOWN_ARRAY_FUNCTIONS),
            (_MATH_NS, _KNOWN_MATH_FUNCTIONS),
        ):
            if expanded.startswith(f"{{{namespace}}}"):
                return _local_name(expanded) in names
        return False

    def _element_available(self, name: str, element: XdmNode) -> bool:
        if ":" not in name and not name.startswith(("{", "Q{")):
            default = element.in_scope_namespaces.get("")
            if default != XSLT_NS:
                return False
            return name in _KNOWN_XSLT_ELEMENTS
        expanded = self._resolve_name(name, element, None)
        return (
            expanded is not None
            and expanded.startswith(f"{{{XSLT_NS}}}")
            and _local_name(expanded) in _KNOWN_XSLT_ELEMENTS
        )

    def _type_available(self, name: str, element: XdmNode) -> bool:
        expanded = self._resolve_name(name, element, element.in_scope_namespaces.get(""))
        return (
            expanded is not None
            and expanded.startswith(f"{{{XSD_NS}}}")
            and _local_name(expanded) in _KNOWN_XSD_TYPES
        )

    def _resolve_name(
        self, name: str, element: XdmNode, default_namespace: str | None
    ) -> str | None:
        name = name.strip()
        if name.startswith("Q{"):
            return "{" + name[2:]
        if name.startswith("{"):
            return name
        if ":" not in name:
            return name if default_namespace is None else f"{{{default_namespace}}}{name}"
        prefix, _, local = name.partition(":")
        uri = element.in_scope_namespaces.get(prefix)
        if uri is None:
            uri = {
                "fn": FN_NS,
                "xs": XSD_NS,
                "xsl": XSLT_NS,
                "map": _MAP_NS,
                "array": _ARRAY_NS,
                "math": _MATH_NS,
            }.get(prefix)
        return None if uri is None else f"{{{uri}}}{local}"


def _static_avt(
    template: str,
    element: XdmNode,
    evaluator: _StaticEvaluator,
    default_namespace: str | None,
) -> str:
    """Evaluate one shadow-attribute value template in the static context."""
    result: list[str] = []
    index = 0
    length = len(template)
    while index < length:
        character = template[index]
        if character == "{":
            if index + 1 < length and template[index + 1] == "{":
                result.append("{")
                index += 2
                continue
            expression, index = _expression_body(template, index + 1, element)
            value = evaluator.evaluate(
                expression,
                element,
                default_namespace=default_namespace,
            )
            result.append(_string_value(value, element))
            continue
        if character == "}":
            if index + 1 < length and template[index + 1] == "}":
                result.append("}")
                index += 2
                continue
            raise XsltStaticError(
                "an unescaped right curly bracket appears in a value template",
                code="XTSE0370",
                uri=element.document.document_uri,
                location=element.source_location(),
            )
        result.append(character)
        index += 1
    return "".join(result)


def _expression_body(template: str, start: int, element: XdmNode) -> tuple[str, int]:
    """Return one embedded expression and the index after its closing brace."""
    index = start
    depth = 0
    quote: str | None = None
    while index < len(template):
        character = template[index]
        if quote is not None:
            if character == quote:
                if index + 1 < len(template) and template[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character in ("'", '"'):
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            if depth == 0:
                return template[start:index], index + 1
            depth -= 1
        index += 1
    raise XsltStaticError(
        "an unescaped left curly bracket appears in a value template",
        code="XTSE0350",
        uri=element.document.document_uri,
        location=element.source_location(),
    )


class _Graph:
    """Mutable include/import edges collected while one module is processed."""

    def __init__(self) -> None:
        self.includes: list[EffectiveModule] = []
        self.imports: list[EffectiveModule] = []


class StaticPreprocessor:
    """Apply normative XSLT static preprocessing to one loaded module."""

    def apply(
        self,
        module: SourceModule,
        static_parameters: Mapping[str, Any] | None = None,
    ) -> EffectiveModule:
        """Return the effective view of *module* under the supplied parameters."""
        return _Apply(module, static_parameters or {}).run()


class _Apply:
    def __init__(self, module: SourceModule, parameters: Mapping[str, Any]) -> None:
        self._module = module
        self._parameters = dict(parameters)
        self._scope = _Scope()
        self._evaluator = _StaticEvaluator(self._scope)
        self._active: list[tuple[str | None, ModuleKind | None]] = []

    def run(self) -> EffectiveModule:
        return self._process_module(self._module, None, None, None)

    def _process_module(
        self,
        source: SourceModule,
        via: ModuleKind | None,
        owner_uri: str | None,
        location: IssueLocation | None,
    ) -> EffectiveModule:
        key = source.identity
        if key is not None:
            for active, active_kind in self._active:
                if active == key:
                    code = "XTSE0210" if (active_kind or via) == "import" else "XTSE0180"
                    message = (
                        "a stylesheet module directly or indirectly imports itself"
                        if code == "XTSE0210"
                        else "a stylesheet module directly or indirectly includes itself"
                    )
                    raise XsltStaticError(
                        message,
                        code=code,
                        uri=owner_uri or key,
                        location=location,
                    )
            self._active.append((key, via))
        try:
            if source.simplified:
                root = self._process_simplified(source)
                includes: tuple[EffectiveModule, ...] = ()
                imports: tuple[EffectiveModule, ...] = ()
            else:
                root, includes, imports = self._process_ordinary(source)
            return EffectiveModule(
                source=source,
                root=root,
                includes=includes,
                imports=imports,
                static_variables=self._scope.snapshot(),
                simplified=source.simplified,
            )
        finally:
            if key is not None:
                self._active.pop()

    def _process_ordinary(
        self, source: SourceModule
    ) -> tuple[EffectiveElement, tuple[EffectiveModule, ...], tuple[EffectiveModule, ...]]:
        raw_root = source.root
        graph = _Graph()
        prepared = self._prepared_shadow_values(raw_root)
        include = self._use_when(raw_root, prepared)
        values = self._attribute_values(raw_root, prepared)
        attributes = self._effective_attributes(raw_root, values, prepared)
        if include:
            children = self._process_children(
                raw_root.children,
                source,
                top_level=True,
                parent=raw_root,
                xml_space=False,
                graph=graph,
            )
        else:
            children = ()
        if any(
            isinstance(child, EffectiveText) and not child.value.isspace() for child in children
        ):
            raise XsltStaticError(
                "an xsl:stylesheet, xsl:transform, or xsl:package element "
                "must not have text node children",
                code="XTSE0120",
                uri=raw_root.document.document_uri,
                location=raw_root.source_location(),
            )
        root = EffectiveElement(
            name=_name_of(raw_root),
            attributes=attributes,
            children=children,
            raw=raw_root,
            base_uri=raw_root.base_uri,
            in_scope_namespaces=raw_root.in_scope_namespaces,
            namespace_declarations=raw_root.namespace_declarations,
        )
        return root, tuple(graph.includes), tuple(graph.imports)

    def _process_simplified(self, source: SourceModule) -> EffectiveElement:
        raw_root = source.root
        literal = self._process_element(
            raw_root,
            source,
            top_level=False,
            parent=None,
            xml_space=False,
        )
        template_children: tuple[EffectiveNode, ...] = () if literal is None else (literal,)
        template = EffectiveElement(
            name=_TEMPLATE,
            attributes=(EffectiveAttribute("match", "/"),),
            children=template_children,
            raw=raw_root,
            base_uri=raw_root.base_uri,
            in_scope_namespaces=raw_root.in_scope_namespaces,
            namespace_declarations=(),
            synthetic=True,
        )
        version = source.version or ""
        return EffectiveElement(
            name=_STYLESHEET,
            attributes=(EffectiveAttribute("version", version),),
            children=(template,),
            raw=raw_root,
            base_uri=raw_root.base_uri,
            in_scope_namespaces=raw_root.in_scope_namespaces,
            namespace_declarations=(),
            synthetic=True,
        )

    # -- element processing ----------------------------------------------------

    def _process_element(
        self,
        raw: XdmNode,
        source: SourceModule,
        *,
        top_level: bool,
        parent: XdmNode | None,
        xml_space: bool,
    ) -> EffectiveElement | None:
        prepared = self._prepared_shadow_values(raw)
        if not self._use_when(raw, prepared):
            return None
        values = self._attribute_values(raw, prepared)
        attributes = self._effective_attributes(raw, values, prepared)
        if (
            not top_level
            and raw.name in (f"{{{XSLT_NS}}}variable", f"{{{XSLT_NS}}}param")
            and any(
                attribute.name == "static" and attribute.value in ("yes", "true", "1")
                for attribute in attributes
            )
        ):
            raise XsltStaticError(
                "the static attribute is permitted only on a top-level declaration",
                code="XTSE0010",
                uri=raw.document.document_uri,
                location=raw.source_location(),
            )
        state = xml_space
        explicit = raw.attribute(_XML_SPACE)
        if explicit is not None:
            if explicit.string_value == "preserve":
                state = True
            elif explicit.string_value == "default":
                state = False
        children = self._process_children(
            raw.children,
            source,
            top_level=False,
            parent=raw,
            xml_space=state,
            graph=None,
        )
        return EffectiveElement(
            name=_name_of(raw),
            attributes=attributes,
            children=children,
            raw=raw,
            base_uri=raw.base_uri,
            in_scope_namespaces=raw.in_scope_namespaces,
            namespace_declarations=raw.namespace_declarations,
        )

    def _process_children(
        self,
        raw_children: Sequence[XdmNode],
        source: SourceModule,
        *,
        top_level: bool,
        parent: XdmNode,
        xml_space: bool,
        graph: _Graph | None,
    ) -> tuple[EffectiveNode, ...]:
        candidates: list[EffectiveNode] = []
        for raw in raw_children:
            if raw.kind in (NodeKind.COMMENT, NodeKind.PROCESSING_INSTRUCTION):
                continue
            if raw.kind is NodeKind.TEXT:
                previous = candidates[-1] if candidates else None
                if isinstance(previous, EffectiveText):
                    candidates[-1] = EffectiveText(previous.value + raw.string_value, previous.raw)
                else:
                    candidates.append(EffectiveText(raw.string_value, raw))
                continue
            assert raw.kind is NodeKind.ELEMENT
            element = self._process_element(
                raw,
                source,
                top_level=top_level,
                parent=parent,
                xml_space=xml_space,
            )
            if element is None:
                continue
            candidates.append(element)
            if top_level:
                self._declare_static(raw, element)
                if graph is not None and raw.name in {
                    f"{{{XSLT_NS}}}include",
                    f"{{{XSLT_NS}}}import",
                }:
                    self._attach_graph(raw, element, source, graph)

        return self._strip_whitespace(
            candidates,
            parent=parent,
            xml_space=xml_space,
        )

    def _strip_whitespace(
        self,
        candidates: list[EffectiveNode],
        *,
        parent: XdmNode,
        xml_space: bool,
    ) -> tuple[EffectiveNode, ...]:
        parent_name = parent.name
        parent_is_text = parent_name == f"{{{XSLT_NS}}}text"
        result: list[EffectiveNode] = []
        for index, node in enumerate(candidates):
            if not isinstance(node, EffectiveText) or parent_is_text:
                result.append(node)
                continue
            if not node.value.isspace():
                result.append(node)
                continue
            if parent_name in _ALWAYS_STRIP_PARENTS:
                continue
            following = _next_element(candidates, index + 1)
            if following is not None and following.name in _STRIP_BEFORE:
                continue
            preceding = _previous_element(candidates, index - 1)
            if preceding is not None and preceding.name == _STRIP_AFTER_CATCH:
                continue
            if not xml_space:
                continue
            result.append(node)
        return tuple(result)

    # -- attributes and conditional inclusion ----------------------------------

    def _prepared_shadow_values(self, raw: XdmNode) -> dict[str, str]:
        prepared: dict[str, str] = {}
        if raw.namespace_uri != XSLT_NS:
            return prepared
        for name in ("_xpath-default-namespace", "_use-when"):
            attribute = raw.attribute(name)
            if attribute is None:
                continue
            default = self._default_namespace(raw, prepared)
            prepared[name[1:]] = _static_avt(
                attribute.string_value,
                raw,
                self._evaluator,
                default,
            )
        return prepared

    def _use_when(self, raw: XdmNode, prepared: Mapping[str, str]) -> bool:
        expression: str | None = None
        if raw.namespace_uri == XSLT_NS:
            expression = prepared.get("use-when")
            if expression is None:
                attribute = raw.attribute("use-when")
                if attribute is None:
                    attribute = raw.attribute(_USE_WHEN)
                if attribute is not None:
                    expression = attribute.string_value
        else:
            attribute = raw.attribute(_USE_WHEN)
            if attribute is not None:
                expression = attribute.string_value
        if expression is None:
            return True
        default = self._default_namespace(raw, prepared)
        value = self._evaluator.evaluate(
            expression,
            raw,
            default_namespace=default,
        )
        return _ebv(value, raw)

    def _default_namespace(self, raw: XdmNode, prepared: Mapping[str, str]) -> str | None:
        value = prepared.get("xpath-default-namespace")
        if value is None:
            attribute = raw.attribute("xpath-default-namespace")
            if attribute is None:
                attribute = raw.attribute(_XPATH_DEFAULT)
            value = None if attribute is None else attribute.string_value
        return value or None

    def _attribute_values(self, raw: XdmNode, prepared: Mapping[str, str]) -> dict[str, str]:
        values = {_name_of(attribute): attribute.string_value for attribute in raw.attributes}
        values.update(prepared)
        return values

    def _effective_attributes(
        self,
        raw: XdmNode,
        values: Mapping[str, str],
        prepared: Mapping[str, str],
    ) -> tuple[EffectiveAttribute, ...]:
        shadow: dict[str, tuple[str, XdmNode]] = {}
        if raw.namespace_uri == XSLT_NS:
            for attribute in raw.attributes:
                name = _name_of(attribute)
                if (
                    _namespace_of(name) is None
                    and name.startswith("_")
                    and len(name) > 1
                    and not name[1:].startswith("_")
                ):
                    target = name[1:]
                    if target in prepared:
                        value = prepared[target]
                    else:
                        value = _static_avt(
                            attribute.string_value,
                            raw,
                            self._evaluator,
                            values.get("xpath-default-namespace"),
                        )
                    shadow[target] = (value, attribute)

        attributes: list[EffectiveAttribute] = []
        emitted: set[str] = set()
        for attribute in raw.attributes:
            name = _name_of(attribute)
            if name in ("use-when", _USE_WHEN):
                continue
            if (
                raw.namespace_uri == XSLT_NS
                and _namespace_of(name) is None
                and name.startswith("_")
                and len(name) > 1
                and not name[1:].startswith("_")
            ):
                continue
            if name in shadow:
                value, shadow_raw = shadow[name]
                attributes.append(
                    EffectiveAttribute(name=name, value=value, raw=shadow_raw, static=True)
                )
                emitted.add(name)
                continue
            attributes.append(
                EffectiveAttribute(
                    name=name,
                    value=attribute.string_value,
                    raw=attribute,
                )
            )
            emitted.add(name)
        for name, (value, shadow_raw) in shadow.items():
            if name in emitted:
                continue
            attributes.append(
                EffectiveAttribute(name=name, value=value, raw=shadow_raw, static=True)
            )
        return tuple(attributes)

    # -- static variables and graph edges --------------------------------------

    def _declare_static(self, raw: XdmNode, element: EffectiveElement) -> None:
        if raw.name not in (f"{{{XSLT_NS}}}variable", f"{{{XSLT_NS}}}param"):
            return
        if element.attribute("static") not in ("yes", "true", "1"):
            return
        kind: Literal["variable", "param"] = (
            "param" if raw.name == f"{{{XSLT_NS}}}param" else "variable"
        )
        name = element.attribute("name")
        if not name:
            raise XsltStaticError(
                "a static variable or parameter requires a name",
                code="XTSE0010",
                uri=raw.document.document_uri,
                location=raw.source_location(),
            )
        expanded = _expand_name(name, raw.in_scope_namespaces, raw)
        if kind == "variable" and element.attribute("visibility") not in (None, "private"):
            raise XsltStaticError(
                "a static variable must have private visibility",
                code="XTSE0010",
                uri=raw.document.document_uri,
                location=raw.source_location(),
            )
        if kind == "param" and element.attribute("visibility") not in (None, "private"):
            raise XsltStaticError(
                "a static parameter must have private visibility",
                code="XTSE0010",
                uri=raw.document.document_uri,
                location=raw.source_location(),
            )
        content = [
            child
            for child in element.children
            if not (isinstance(child, EffectiveText) and child.value.isspace())
        ]
        if content:
            raise XsltStaticError(
                "a static variable or parameter must have empty content",
                code="XTSE0010",
                uri=raw.document.document_uri,
                location=raw.source_location(),
            )
        external = self._external_parameter(name, expanded, raw)
        select = element.attribute("select")
        if external is None and select is None:
            raise XsltStaticError(
                "a static variable or parameter requires a select attribute",
                code="XTSE0010",
                uri=raw.document.document_uri,
                location=raw.source_location(),
            )
        if external is not None:
            value = external
        else:
            assert select is not None
            value = self._evaluator.evaluate(
                select,
                raw,
                default_namespace=element.attribute("xpath-default-namespace"),
            )
        self._scope.declare(expanded, kind, value, raw)

    def _external_parameter(self, name: str, expanded: str, raw: XdmNode) -> XdmSequence | None:
        for key in (expanded, name, _local_name(expanded) or ""):
            if key in self._parameters:
                return as_xdm(self._parameters[key])
        return None

    def _attach_graph(
        self,
        raw: XdmNode,
        element: EffectiveElement,
        source: SourceModule,
        graph: _Graph,
    ) -> None:
        child = source.resolved_references.get(raw.node_id)
        if child is None:
            href = element.attribute("href")
            if not href:
                raise XsltStaticError(
                    "an xsl:include or xsl:import requires an href attribute",
                    code="XTSE0010",
                    uri=raw.document.document_uri,
                    location=raw.source_location(),
                )
            kind: ModuleKind = "include" if raw.name == f"{{{XSLT_NS}}}include" else "import"
            child = source.loader.resolve_reference(
                kind,
                href,
                base_uri=raw.base_uri,
                owner_uri=source.identity,
                location=raw.source_location(),
            )
        kind = "include" if raw.name == f"{{{XSLT_NS}}}include" else "import"
        effective = self._process_module(child, kind, source.identity, raw.source_location())
        if kind == "include":
            graph.includes.append(effective)
        else:
            graph.imports.append(effective)


def _next_element(candidates: Sequence[EffectiveNode], start: int) -> EffectiveElement | None:
    return next(
        (node for node in candidates[start:] if isinstance(node, EffectiveElement)),
        None,
    )


def _previous_element(candidates: Sequence[EffectiveNode], start: int) -> EffectiveElement | None:
    return next(
        (node for node in reversed(candidates[: start + 1]) if isinstance(node, EffectiveElement)),
        None,
    )


__all__ = [
    "EffectiveAttribute",
    "EffectiveElement",
    "EffectiveModule",
    "EffectiveNode",
    "EffectiveText",
    "StaticPreprocessor",
]
