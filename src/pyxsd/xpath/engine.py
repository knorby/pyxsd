"""Versioned, schema-aware XPath evaluation without an XSLT transformation.

The engine compiles an expression against an immutable
:class:`XPathStaticContext` and evaluates it with an invocation-local
:class:`ExpressionEnvironment`.  Every evaluation receives a fresh parser and
dynamic ``XPathContext``; compiled expressions never retain mutable token
state, and no parser class-level symbol table is modified.

XDM nodes are exposed to elementpath through grounded adapter nodes, so path
expressions, predicates, attributes, comments, text, and processing
instructions keep Task 8 identity and Task 7 typed values.  Results are
converted back to XDM sequences, maps, arrays, and function items rather than
collapsing into Python lists or strings.

Expressions that Task 5's continuation compiler can lower are evaluated by
``ContinuationMachine`` with an explicit heap continuation stack.  Unsupported
constructs use the full elementpath evaluator, which is the documented
capability boundary.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MethodType
from typing import Any, cast

from elementpath import XPathContext
from elementpath.datatypes import AnyAtomicType
from elementpath.tdop import Parser
from elementpath.xpath2 import XPath2Parser
from elementpath.xpath3 import XPath30Parser, XPath31Parser
from elementpath.xpath_nodes import (
    AttributeNode,
    CommentNode,
    DocumentNode,
    ElementNode,
    NamespaceNode,
    ProcessingInstructionNode,
    TextNode,
    XPathNodeTree,
)
from elementpath.xpath_tokens import XPathArray, XPathMap
from elementpath.xpath_tokens.functions import XPathFunction
from elementpath.xpath_tokens.tokens import ContextItemToken, VariableToken

from pyxsd import xsd_data_types as _xsd
from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmItem, XdmMap, XdmSequence, as_xdm
from pyxsd.xdm.annotations import schema_type_name
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath import continuation
from pyxsd.xpath.continuation import ContinuationMachine, XPathClosure
from pyxsd.xpath.environment import (
    ExpressionEnvironment,
    Focus,
    FocusKind,
    XPathStaticContext,
)
from pyxsd.xpath.functions import (
    FUNCTION_SCOPE_VARIABLE,
    ContextFunction,
    ContextFunctionToken,
    HostFunctionToken,
    atomic_to_elementpath,
    call_host_function,
    clone_function_token,
    function_arity,
    sequence_to_elementpath,
)

__all__ = [
    "CompiledExpression",
    "ExpressionEnvironment",
    "Focus",
    "XPathEngine",
    "XPathStaticContext",
]

_PARSERS: dict[str, type[Any]] = {
    "2.0": XPath2Parser,
    "3.0": XPath30Parser,
    "3.1": XPath31Parser,
}
_VERSION_ALIASES = {"2": "2.0", "3": "3.0", "3.0": "3.0", "3.1": "3.1", "2.0": "2.0"}
_XSD_NS = "http://www.w3.org/2001/XMLSchema"
_UNTYPED = f"{{{_XSD_NS}}}untyped"
_UNTYPED_ATOMIC = f"{{{_XSD_NS}}}untypedAtomic"


def _normalise_version(version: str | None) -> str:
    normalised = _VERSION_ALIASES.get((version or "").strip())
    if normalised is None:
        raise ValueError(f"unsupported XPath version: {version!r}")
    return normalised


def _arity_tuple(arity: int | tuple[int, int | None]) -> tuple[int, int | None]:
    if isinstance(arity, int):
        return arity, arity
    return arity


# ---------------------------------------------------------------------------
# Grounded XDM node adapters


class _NodeAdapterMixin:
    """Base for grounded adapters; every concrete node kind adds its protocol."""

    _xdm: XdmNode

    @property
    def xdm(self) -> XdmNode:
        return self._xdm


class _XdmDocumentAdapter(DocumentNode, _NodeAdapterMixin):
    __slots__ = ("_factory", "_xdm")

    def __init__(self, xdm: XdmDocument, factory: _ValueBridge) -> None:
        self._xdm = xdm
        self._factory = factory
        self.name = None
        self.value = xdm
        self.parent = None
        self.position = xdm.document_order
        self.children = []
        self.tree = XPathNodeTree(
            self,
            uri=xdm.document_uri,
            namespaces=dict(xdm.in_scope_namespaces),
        )
        cast(Any, self.tree.elements)[xdm] = self
        for child in xdm.children:
            self.children.append(factory.adapt_node(child, self))

    @property
    def string_value(self) -> str:
        return self._xdm.string_value


class _XdmElementAdapter(ElementNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(
        self,
        xdm: XdmNode,
        parent: DocumentNode | ElementNode | None,
        factory: _ValueBridge,
    ) -> None:
        self._xdm = xdm
        self.name = xdm.name
        self.value = xdm
        self.parent = parent
        self.position = xdm.document_order
        self.children = []
        self.tree = (
            parent.tree
            if parent is not None
            else XPathNodeTree(
                self,
                uri=xdm.document_uri,
                namespaces=dict(xdm.in_scope_namespaces),
            )
        )
        self.xsd_type = None
        self.xsd_element = None
        self._nsmap = dict(xdm.in_scope_namespaces)
        cast(Any, self.tree.elements)[xdm] = self
        for child in xdm.children:
            self.children.append(factory.adapt_node(child, self))
        self._attributes = [factory.adapt_node(attribute, self) for attribute in xdm.attributes]
        self._namespace_nodes = [
            factory.adapt_node(namespace, self) for namespace in xdm.namespace_nodes
        ]

    @property
    def attributes(self) -> list[AttributeNode]:
        return self._attributes

    @property
    def namespace_nodes(self) -> list[NamespaceNode]:
        return self._namespace_nodes

    @property
    def string_value(self) -> str:
        return self._xdm.string_value

    @property
    def type_name(self) -> str | None:
        return self._xdm.type_name or _UNTYPED

    @property
    def nilled(self) -> bool:
        return bool(self._xdm.nilled)

    @property
    def iter_typed_values(self) -> Any:
        for item in self._xdm.typed_value:
            yield atomic_to_elementpath(cast(XdmAtomic, item))


class _XdmAttributeAdapter(AttributeNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(self, xdm: XdmNode, parent: ElementNode | None, factory: _ValueBridge) -> None:
        del factory
        self._xdm = xdm
        self.name = xdm.name
        self.value = xdm.string_value
        self.parent = parent
        self.position = xdm.document_order
        self.xsd_type = None

    @property
    def string_value(self) -> str:
        return self._xdm.string_value

    @property
    def type_name(self) -> str | None:
        return self._xdm.type_name or _UNTYPED_ATOMIC

    @property
    def iter_typed_values(self) -> Any:
        for item in self._xdm.typed_value:
            yield atomic_to_elementpath(cast(XdmAtomic, item))

    @property
    def base_uri(self) -> str | None:
        return self.parent.base_uri if self.parent is not None else None


class _XdmTextAdapter(TextNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(
        self, xdm: XdmNode, parent: DocumentNode | ElementNode | None, factory: _ValueBridge
    ) -> None:
        del factory
        self._xdm = xdm
        self.name = None
        self.value = xdm.string_value
        self.parent = cast(ElementNode | None, parent)
        self.position = xdm.document_order


class _XdmCommentAdapter(CommentNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(
        self,
        xdm: XdmNode,
        parent: DocumentNode | ElementNode | None,
        factory: _ValueBridge,
    ) -> None:
        del factory
        self._xdm = xdm
        super().__init__(xdm.string_value, None, xdm.document_order)
        self.parent = parent


class _XdmProcessingInstructionAdapter(ProcessingInstructionNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(
        self,
        xdm: XdmNode,
        parent: DocumentNode | ElementNode | None,
        factory: _ValueBridge,
    ) -> None:
        del factory
        self._xdm = xdm
        super().__init__(xdm.target or "", xdm.string_value, None, xdm.document_order)
        self.parent = parent


class _XdmNamespaceAdapter(NamespaceNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(self, xdm: XdmNode, parent: ElementNode, factory: _ValueBridge) -> None:
        del factory
        self._xdm = xdm
        super().__init__(xdm.prefix, xdm.bound_uri or "", parent, xdm.document_order)


class _FocusContextItemToken(ContextItemToken):
    """A context item expression that rejects an absent focus exactly."""

    def evaluate(self, context: Any = None) -> Any:
        if context is None or context.item is None:
            raise self.missing_context()
        return context.item

    def select(self, context: Any = None) -> Any:
        if context is None or context.item is None:
            raise self.missing_context()
        yield context.item


# ---------------------------------------------------------------------------
# Schema pool bridge


class _PoolSchemaProxy:
    """A schema proxy facade over every namespace in one SchemaPool."""

    __slots__ = ("_namespaces", "_pool")

    def __init__(self, pool: SchemaPool, namespaces: Mapping[str, str]) -> None:
        self._pool = pool
        self._namespaces = dict(namespaces)

    @property
    def xsd_version(self) -> str:
        return self._pool.xsd_version

    def is_fully_valid(self) -> bool:
        return all(proxy.is_fully_valid() for proxy in self._pool.schemas.values())

    def expand(self, qname: str) -> str:
        if qname.startswith("{"):
            return qname
        prefix, separator, local = qname.partition(":")
        if separator:
            uri = self._namespaces.get(prefix)
            if uri is None:
                raise KeyError(prefix)
            return f"{{{uri}}}{local}"
        return qname

    def cast_as(self, obj: Any, type_qname: str) -> Any:
        adapter = self._pool.get_type(self.expand(type_qname))
        if adapter is None:
            raise KeyError(type_qname)
        return adapter.cast(obj, self._namespaces)

    def is_instance(self, obj: Any, type_qname: str) -> bool:
        adapter = self._pool.get_type(self.expand(type_qname))
        if adapter is None:
            raise KeyError(type_qname)
        return adapter.is_valid(obj, namespaces=self._namespaces)

    def get_type(self, qname: str) -> Any:
        return self._pool.get_type(self.expand(qname))

    def get_element(self, qname: str) -> Any:
        return self._pool.get_element(self.expand(qname))

    def get_attribute(self, qname: str) -> Any:
        return self._pool.get_attribute(self.expand(qname))

    def get_substitution_group(self, qname: str) -> Any:
        return self._pool.get_substitution_group(self.expand(qname))


# ---------------------------------------------------------------------------
# Value bridge


class _ValueBridge:
    """Convert XDM values to elementpath values and back for one evaluation."""

    def __init__(
        self,
        parser: Any,
        static_context: XPathStaticContext,
        environment: ExpressionEnvironment,
    ) -> None:
        self.parser = parser
        self.static_context = static_context
        self.environment = environment
        self._adapters: dict[int, Any] = {}
        self._context: XPathContext | None = None

    # XDM -> elementpath

    def adapt_document(self, document: XdmDocument) -> _XdmDocumentAdapter:
        adapter = self._adapters.get(id(document))
        if adapter is None:
            adapter = _XdmDocumentAdapter(document, self)
            self._adapters[id(document)] = adapter
        assert isinstance(adapter, _XdmDocumentAdapter)
        return adapter

    def adapt_node(self, node: XdmNode, parent: Any = None) -> Any:
        existing = self._adapters.get(id(node))
        if existing is not None:
            return existing
        if node.kind is NodeKind.DOCUMENT:
            return self.adapt_document(cast(XdmDocument, node))
        if parent is None:
            if node.kind is NodeKind.ELEMENT:
                return self.adapt_node(node.document)
            raise ValueError(f"a {node.kind.value} node requires a parent adapter")
        if node.kind is NodeKind.ELEMENT:
            adapter: Any = _XdmElementAdapter(node, parent, self)
        elif node.kind is NodeKind.ATTRIBUTE:
            adapter = _XdmAttributeAdapter(node, parent, self)
        elif node.kind is NodeKind.TEXT:
            adapter = _XdmTextAdapter(node, parent, self)
        elif node.kind is NodeKind.COMMENT:
            adapter = _XdmCommentAdapter(node, parent, self)
        elif node.kind is NodeKind.PROCESSING_INSTRUCTION:
            adapter = _XdmProcessingInstructionAdapter(node, parent, self)
        elif node.kind is NodeKind.NAMESPACE:
            adapter = _XdmNamespaceAdapter(node, parent, self)
        else:  # pragma: no cover - all seven kinds are handled above
            raise TypeError(f"unsupported XDM node kind: {node.kind!r}")
        self._adapters[id(node)] = adapter
        return adapter

    def _adapt_rootless_node(self, node: XdmNode) -> Any:
        """Adapt a parentless node used as an expression focus or value."""
        if node.kind is NodeKind.ELEMENT:
            return _XdmElementAdapter(node, None, self)
        if node.kind is NodeKind.ATTRIBUTE:
            return _XdmAttributeAdapter(node, None, self)
        if node.kind is NodeKind.TEXT:
            return _XdmTextAdapter(node, None, self)
        if node.kind is NodeKind.COMMENT:
            return _XdmCommentAdapter(node, None, self)
        if node.kind is NodeKind.PROCESSING_INSTRUCTION:
            return _XdmProcessingInstructionAdapter(node, None, self)
        raise ValueError(f"a detached {node.kind.value} node has no parent")

    def adapt_item(self, item: XdmItem) -> Any:
        if isinstance(item, XdmNode):
            if item.kind is NodeKind.DOCUMENT:
                return self.adapt_document(cast(XdmDocument, item))
            if item.parent is None:
                adapter = self._adapters.get(id(item))
                if adapter is None:
                    adapter = self._adapt_rootless_node(item)
                    self._adapters[id(item)] = adapter
                return adapter
            self.adapt_document(item.document)
            adapter = self._adapters.get(id(item))
            assert adapter is not None
            return adapter
        if isinstance(item, XdmAtomic):
            return atomic_to_elementpath(item)
        if isinstance(item, XdmMap):
            entries = [
                (atomic_to_elementpath(key), self.sequence_to_elementpath(value))
                for key, value in item.entries
            ]
            return XPathMap(self.parser, items=entries)
        if isinstance(item, XdmArray):
            return XPathArray(
                self.parser,
                items=[self.sequence_to_elementpath(member) for member in item.members],
            )
        if isinstance(item, XdmFunction):
            return HostFunctionToken(
                self.parser,
                item,
                item_converter=self.adapt_item,
                name=item.name,
                arity=item.arity,
            )
        raise TypeError(f"{type(item).__name__} is not an XDM item")

    def sequence_to_elementpath(self, sequence: XdmSequence) -> Any:
        return sequence_to_elementpath(sequence, item_converter=self.adapt_item)

    def variable_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, value in self.environment.variables.items():
            if isinstance(value, XdmFunction) or (
                callable(value) and not isinstance(value, XdmItem)
            ):
                values[name] = HostFunctionToken(
                    self.parser,
                    value,
                    item_converter=self.adapt_item,
                    name=name,
                )
            else:
                values[name] = self.sequence_to_elementpath(as_xdm(value))
        return values

    # elementpath -> XDM

    def to_xdm_sequence(self, value: Any) -> XdmSequence:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            items: list[XdmItem] = []
            for member in value:
                items.extend(self.to_xdm_sequence(member))
            return tuple(items)
        if isinstance(value, _NodeAdapterMixin):
            return (value.xdm,)
        if isinstance(value, XdmItem):
            return (value,)
        if isinstance(value, XPathMap):
            entries: list[tuple[XdmAtomic, Any]] = []
            for key, member in value.items():
                converted_key = self.to_xdm_sequence(key)
                if len(converted_key) != 1 or not isinstance(
                    converted_key[0], XdmAtomic
                ):  # pragma: no cover - elementpath map keys are atomic
                    raise TypeError("an XDM map key must atomize to one atomic value")
                entries.append((converted_key[0], self.to_xdm_sequence(member)))
            return (XdmMap(entries),)
        if isinstance(value, XPathArray):
            return (XdmArray(self.to_xdm_sequence(member) for member in value.items()),)
        if isinstance(value, HostFunctionToken):
            target = value._target
            if isinstance(target, XdmFunction):
                return (target,)
            return (
                XdmFunction(
                    lambda arguments, environment, closure: call_host_function(
                        target, arguments, environment
                    ),
                    arity=value.nargs,
                    name=value.name or None,
                ),
            )
        if isinstance(value, XPathClosure):
            return (_xpath_closure_to_xdm(value, self),)
        if isinstance(value, continuation.HostFunction):
            return (
                XdmFunction(
                    lambda arguments, environment, closure: call_host_function(
                        value.callback, arguments, environment
                    ),
                    arity=value.arity if value.arity is not None else (0, None),
                    name=value.name or None,
                ),
            )
        if isinstance(value, XPathFunction):
            return (self._function_item(value),)
        if isinstance(value, _xsd.XsdDataType):
            type_name = schema_type_name(type(value))
            if type_name is not None:
                return (XdmAtomic(value, type_name, type(value)),)
        if isinstance(value, (AnyAtomicType, str, bool, int, float, Decimal)):
            return as_xdm(value)
        try:
            return as_xdm(value)
        except TypeError as exc:
            raise TypeError(f"cannot convert XPath result {value!r} to XDM") from exc

    def _function_item(self, token: XPathFunction) -> XdmFunction:
        context = self._context
        assert context is not None
        nargs = getattr(token, "nargs", None)
        arity = (0, None) if nargs is None else _arity_tuple(nargs)
        name = getattr(token, "name", None)
        if not isinstance(name, str) or not name:
            name = None

        def implementation(arguments: Any, environment: Any, closure: Any) -> XdmSequence:
            del environment, closure
            converted = tuple(
                sequence_to_elementpath(argument, item_converter=self.adapt_item)
                for argument in arguments
            )
            clone = clone_function_token(token)
            return self.to_xdm_sequence(clone(*converted, context=context))

        return XdmFunction(implementation, arity=arity, name=name)


# ---------------------------------------------------------------------------
# Compiled expression and engine


@dataclass(frozen=True)
class CompiledExpression:
    """An immutable expression compiled against one static context."""

    source: str
    static_context: XPathStaticContext
    version: str
    continuation_plan: continuation.CompiledExpression | None = field(default=None)
    variable_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.source or not self.source.strip():
            raise ValueError("an XPath expression requires non-empty source")
        if not isinstance(self.static_context, XPathStaticContext):
            raise TypeError("static_context must be an XPathStaticContext")
        if self.version not in _PARSERS:
            raise ValueError(f"unsupported XPath version: {self.version!r}")
        object.__setattr__(self, "variable_names", frozenset(self.variable_names))


def _expression_variable_names(
    root: Any,
    static_context: XPathStaticContext,
) -> frozenset[str]:
    """Return expanded variable names referenced by one parsed expression."""
    names: set[str] = set()
    stack = [root]
    while stack:
        token = stack.pop()
        if isinstance(token, VariableToken):
            names.add(_expanded_variable_name(token.value, static_context))
        stack.extend(getattr(token, "_items", ()) or ())
    return frozenset(names)


def _expanded_variable_name(value: str, static_context: XPathStaticContext) -> str:
    if value.startswith("Q{"):
        close = value.find("}")
        assert close >= 0
        uri = value[2:close]
        local = value[close + 1 :]
        return local if not uri else f"{{{uri}}}{local}"
    if value.startswith("{"):
        return value
    prefix, separator, local = value.partition(":")
    if not separator:
        return value
    bound_uri = static_context.namespaces.get(prefix)
    return value if bound_uri is None else f"{{{bound_uri}}}{local}"


class XPathEngine:
    """Compile and evaluate standalone XPath expressions."""

    def __init__(self, version: str = "3.1") -> None:
        self._version = _normalise_version(version)
        self._parser_class = _PARSERS[self._version]

    @property
    def version(self) -> str:
        return self._version

    @property
    def parser_class(self) -> type[Any]:
        return self._parser_class

    def compile(
        self,
        source: str,
        static_context: XPathStaticContext | None = None,
    ) -> CompiledExpression:
        """Parse *source* and freeze every static decision it depends on."""
        if static_context is None:
            static_context = XPathStaticContext()
        if not isinstance(static_context, XPathStaticContext):
            raise TypeError("static_context must be an XPathStaticContext")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("an XPath expression requires non-empty source")

        parser = _build_parser(self._version, static_context, functions=None, key=True)
        root_token = parser.parse(source)
        variable_names = _expression_variable_names(root_token, static_context)

        plan: continuation.CompiledExpression | None = None
        if self._version == "3.1":
            try:
                plan = continuation.compile_expression(
                    source,
                    variable_types=static_context.variable_types,
                    default_collation=static_context.default_collation,
                )
            except Exception:
                # The optional seam lowers a subset; any parse/lowering miss
                # falls back to the full elementpath evaluator.
                plan = None
        return CompiledExpression(
            source=source,
            static_context=static_context,
            version=self._version,
            continuation_plan=plan,
            variable_names=variable_names,
        )

    def evaluate(
        self,
        compiled: CompiledExpression,
        environment: ExpressionEnvironment,
        *,
        focus: Focus | XdmItem | Sequence[Any] | None = None,
    ) -> XdmSequence:
        """Evaluate one compiled expression with fresh invocation-local state."""
        if not isinstance(compiled, CompiledExpression):
            raise TypeError("compiled must be a CompiledExpression")
        if not isinstance(environment, ExpressionEnvironment):
            raise TypeError("environment must be an ExpressionEnvironment")
        if compiled.version != self._version:
            raise ValueError("the compiled expression belongs to a different XPath version")

        resolved = _resolve_focus(focus)
        if resolved.is_empty:
            return ()

        parser = _build_parser(
            self._version,
            compiled.static_context,
            functions=environment.functions,
            key=True,
            base_uri=environment.base_uri,
            decimal_formats=environment.decimal_formats,
        )
        bridge = _ValueBridge(parser, compiled.static_context, environment)
        context = _build_context(compiled.static_context, environment, resolved, bridge)
        bridge._context = context
        holder = parser._pyxsd_bridge_holder
        assert holder is not None
        holder.bridge = bridge
        _install_function_overrides(parser, holder, environment)

        if compiled.continuation_plan is not None:
            variables = _continuation_variables(environment, bridge)
            if variables is not None:
                result = ContinuationMachine(compiled.continuation_plan, variables).run()
                return bridge.to_xdm_sequence(result)

        result = parser.parse(compiled.source).evaluate(context)
        return bridge.to_xdm_sequence(result)


# ---------------------------------------------------------------------------
# Parser and context construction


def _build_parser(
    version: str,
    static_context: XPathStaticContext,
    *,
    functions: Mapping[str, Any] | None,
    key: bool,
    base_uri: str | None = None,
    decimal_formats: Mapping[str | None, Mapping[str, str]] | None = None,
) -> Any:
    parser_class = _PARSERS[version]
    holder = _BridgeHolder()
    options: dict[str, Any] = {
        "namespaces": dict(static_context.namespaces),
        "default_namespace": static_context.default_namespace,
        "variable_types": dict(static_context.variable_types),
        "base_uri": base_uri if base_uri is not None else static_context.base_uri,
        "default_collation": static_context.default_collation,
        "xsd_version": static_context.xsd_version,
    }
    if decimal_formats and version != "2.0":
        options["decimal_formats"] = {
            name: dict(attributes) for name, attributes in decimal_formats.items()
        }
    parser = parser_class(**options)
    parser._pyxsd_bridge_holder = holder
    _install_syntax_only_parse(parser)
    _install_focus_token(parser)
    _install_focus_functions(parser)
    _install_function_scope(parser)
    _register_schema_constructors(parser, static_context)

    for name in static_context.functions:
        _register_declared_function(
            parser,
            name,
            static_context,
            _function_target(static_context, name, functions),
            holder,
        )
    if key and "key" not in parser.symbol_table:
        _register_key_function(parser, holder)
    return parser


def _install_syntax_only_parse(parser: Any) -> None:
    """Parse syntax and static names without evaluating resource functions."""

    def parse_syntax_only(self: Any, source: str) -> Any:
        if self.tokenizer is None:
            self.tokenizer = self.create_tokenizer(self.symbol_table)
        root_token = Parser.parse(self, source)
        if root_token.label in ("sequence type", "function test"):
            raise root_token.error("XPST0003", "not allowed in XPath expression")
        return root_token

    parser.parse = MethodType(parse_syntax_only, parser)


def _install_focus_token(parser: Any) -> None:
    parser._register(
        ".",
        "_FocusContextItemToken",
        bases=(_FocusContextItemToken,),
        label="context item expression",
        bp=0,
    )


def _install_focus_functions(parser: Any) -> None:
    """Make position()/last() reject an absent context item explicitly."""

    for symbol in ("position", "last"):
        base = parser.symbol_table.get(symbol)
        if base is None:  # pragma: no cover - built into every supported parser
            continue

        def evaluate(self: Any, context: Any = None, _base: Any = base) -> Any:
            if context is None or context.item is None:
                raise self.missing_context()
            return _base.evaluate(self, context)

        parser._register(
            symbol,
            f"_Focus{symbol.title()}Function",
            bases=(base,),
            label=base.label,
            nargs=base.nargs,
            sequence_types=base.sequence_types,
            evaluate=evaluate,
        )


def _install_function_scope(parser: Any) -> None:
    """Mark the body of an inline/higher-order function call.

    XPath keeps the caller's focus for an inline function body, but XSLT
    clears its own dynamic components (current item, current group, merge and
    regex captures) for a function invocation.  The reserved context variable
    lets one evaluation's ``ContextFunctionToken`` distinguish the two
    without any process-global state.
    """
    base = parser.symbol_table.get("function")
    if base is None:  # pragma: no cover - every supported parser has it
        return

    def call(self: Any, *args: Any, context: Any = None) -> Any:
        active = self.context if context is None else context
        if active is None:  # pragma: no cover - XPath supplies a context
            return base.__call__(self, *args, context=context)
        marked = copy.copy(active)
        variables = dict(getattr(marked, "variables", {}) or {})
        variables[FUNCTION_SCOPE_VARIABLE] = True
        marked.variables = variables
        return base.__call__(self, *args, context=marked)

    parser._register(
        "function",
        "_PyxsdScopedInlineFunction",
        bases=(base,),
        label=base.label,
        __call__=call,
    )


def _register_schema_constructors(parser: Any, static_context: XPathStaticContext) -> None:
    pool = static_context.schema
    if pool is None:
        return
    parser.schema = _PoolSchemaProxy(pool, parser.namespaces)
    from elementpath.namespaces import get_prefixed_name

    for adapter in pool.types.values():
        if adapter.is_builtin or not adapter.is_atomic() or adapter.name is None:
            continue
        symbol = get_prefixed_name(adapter.name, parser.namespaces)
        if ":" not in symbol or symbol.startswith("{"):
            continue
        if symbol not in parser.symbol_table:
            parser.schema_constructor(adapter.name)


def _register_declared_function(
    parser: Any,
    name: str,
    static_context: XPathStaticContext,
    target: Any,
    holder: _BridgeHolder,
) -> None:
    arity = static_context.functions[name]
    prefix, local = _split_function_name(name, static_context, parser.namespaces)

    def callback(*arguments: Any) -> Any:
        bridge = holder.bridge
        if bridge is None:
            raise RuntimeError(f"host function {name!r} has no evaluation context")
        if target is None:
            raise RuntimeError(f"host function {name!r} has no environment implementation")
        return bridge.sequence_to_elementpath(
            call_host_function(
                target,
                (bridge.to_xdm_sequence(argument) for argument in arguments),
                bridge.environment,
            )
        )

    token_class = parser.external_function(
        callback,
        name=local,
        prefix=prefix,
        sequence_types=("item()*",),
    )
    token_class.label = "function"
    token_class.nargs = arity


def _function_target(
    static_context: XPathStaticContext,
    name: str,
    functions: Mapping[str, Any] | None,
) -> Any:
    if functions is None:
        return None
    if name in functions:
        return functions[name]
    expanded = static_context.expand_name(name)
    if expanded is not None and expanded in functions:
        return functions[expanded]
    lexical = static_context.lexical_name(name)
    if lexical is not None and lexical in functions:
        return functions[lexical]
    return None


def _split_function_name(
    name: str,
    static_context: XPathStaticContext,
    namespaces: Mapping[str, str],
) -> tuple[str | None, str]:
    if name.startswith("{"):
        expanded = name
        lexical = static_context.lexical_name(expanded)
        if lexical is None or ":" not in lexical:
            raise ValueError(f"no namespace prefix is bound for {name!r}")
        prefix, _, local = lexical.partition(":")
        return prefix, local
    prefix, separator, local = name.partition(":")
    if not separator:
        raise ValueError(f"function {name!r} requires a namespace prefix")
    if prefix not in namespaces:
        raise ValueError(f"unknown namespace prefix {prefix!r}")
    return prefix, local


def _register_key_function(parser: Any, holder: _BridgeHolder) -> None:
    def key(
        name_argument: Any,
        value_argument: Any,
        top_argument: Any = None,
    ) -> Any:
        bridge = holder.bridge
        assert bridge is not None
        name_sequence = bridge.to_xdm_sequence(name_argument)
        name = (
            name_sequence[0].lexical
            if name_sequence and isinstance(name_sequence[0], XdmAtomic)
            else ""
        )
        index = bridge.environment.key_indexes.get(name)
        if index is None:
            return []
        values = bridge.to_xdm_sequence(value_argument)
        if top_argument is not None:
            del top_argument
        nodes: list[XdmNode] = []
        for item in values:
            value = cast(XdmAtomic, item)
            nodes.extend(index.get(value.lexical, ()))
            nodes.extend(index.get(value.value, ()))
            nodes.extend(index.get(value, ()))
        unique: dict[int, XdmNode] = {id(node): node for node in nodes}
        ordered = sorted(unique.values(), key=lambda node: node.document_order)
        return [bridge.adapt_item(node) for node in ordered]

    token_class = parser.external_function(
        key,
        name="key",
        sequence_types=("xs:string", "xs:anyAtomicType*", "node()", "element()*"),
    )
    token_class.label = "function"
    token_class.nargs = (2, 3)


def _install_function_overrides(
    parser: Any,
    holder: _BridgeHolder,
    environment: ExpressionEnvironment,
) -> None:
    """Replace built-in callbacks with invocation-local XSLT-aware behavior.

    Each override target receives the context item (or ``None``) and a tuple of
    XDM argument sequences.  Installing the override on this parser instance
    only avoids a process-global callback slot, so concurrent evaluations of the
    same compiled expression stay independent.
    """
    for name, target in environment.function_overrides.items():
        symbol = name.rpartition("}")[2] if name.startswith("{") else name.rpartition(":")[2]
        base = parser.symbol_table.get(symbol)
        if base is None:
            # XPath 2.0 has no generate-id(); a stylesheet that calls it fails
            # at compile time, so there is nothing to override here.
            continue
        if isinstance(target, ContextFunction):

            def read_sequence(value: Any) -> Any:
                active_bridge = holder.bridge
                if active_bridge is None:  # pragma: no cover - installed first
                    raise RuntimeError("a context function has no evaluation context")
                return active_bridge.to_xdm_sequence(value)

            def adapt_item(value: Any) -> Any:
                active_bridge = holder.bridge
                if active_bridge is None:  # pragma: no cover - installed first
                    raise RuntimeError("a context function has no evaluation context")
                return active_bridge.adapt_item(value)

            parser._register(
                symbol,
                f"_PyxsdContext_{symbol.replace('-', '_')}",
                bases=(ContextFunctionToken,),
                label="function",
                nargs=base.nargs,
                sequence_types=base.sequence_types,
                namespace=base.namespace,
                direct=staticmethod(target.direct),
                dynamic=staticmethod(target.dynamic) if target.dynamic is not None else None,
                sequence_reader=staticmethod(read_sequence),
                item_converter=staticmethod(adapt_item),
            )
            continue
        class_name = f"_PyxsdOverride_{symbol.replace('-', '_')}"

        def evaluate(
            self: Any,
            context: Any = None,
            _base: Any = base,
            _target: Any = target,
        ) -> Any:
            bridge = holder.bridge
            if bridge is None:  # pragma: no cover - the bridge is installed first
                raise RuntimeError("a function override has no evaluation context")
            active = context if context is not None else self.context
            arguments: list[Any] = []
            for index in range(len(self)):
                token = self._items[index]
                arguments.append(bridge.to_xdm_sequence(list(token.select(active))))
            item = None
            raw_item = getattr(active, "item", None) if active is not None else None
            if raw_item is not None:
                item = next(iter(bridge.to_xdm_sequence(raw_item)), None)
            result = _target(item, tuple(arguments))
            return bridge.sequence_to_elementpath(as_xdm(result))

        parser._register(
            symbol,
            class_name,
            bases=(base,),
            label=base.label,
            nargs=base.nargs,
            sequence_types=base.sequence_types,
            evaluate=evaluate,
        )


class _BridgeHolder:
    """Per-parser bridge used only by callbacks registered on that parser.

    A fresh holder belongs to each parser instance produced for one evaluation.
    Callbacks close over their holder, so concurrent evaluations never share a
    bridge or a mutable callback slot.
    """

    def __init__(self) -> None:
        self.bridge: _ValueBridge | None = None


def _build_context(
    static_context: XPathStaticContext,
    environment: ExpressionEnvironment,
    focus: Focus,
    bridge: _ValueBridge,
) -> XPathContext:
    documents = {
        uri: bridge.adapt_document(document) for uri, document in environment.documents.items()
    }
    collections = {
        uri: [bridge.adapt_item(node) for node in nodes]
        for uri, nodes in environment.collections.items()
    }
    default_collection = [bridge.adapt_item(node) for node in environment.default_collection]
    variables = bridge.variable_values()
    kwargs: dict[str, Any] = {
        "namespaces": dict(static_context.namespaces),
        "variables": variables,
        "current_dt": environment.current_datetime,
        "timezone": environment.timezone,
        "documents": documents or None,
        "collections": collections or None,
        "text_resources": dict(environment.text_resources),
    }
    if default_collection:
        kwargs["default_collection"] = default_collection
    if focus.kind is FocusKind.ABSENT:
        context = XPathContext(item="", **kwargs)
        cast(Any, context).item = None
        context.root = None
        context.document = None
        cast(Any, context).position = None
        cast(Any, context).size = None
    elif focus.item is not None and isinstance(focus.item, XdmNode):
        item = bridge.adapt_item(focus.item)
        if isinstance(item, DocumentNode):
            context = XPathContext(root=item, item=item, **kwargs)
        elif isinstance(item, ElementNode):
            context = XPathContext(root=item.tree.root_node, item=item, **kwargs)
        else:
            context = XPathContext(item=item, **kwargs)
    elif focus.item is not None:
        context = XPathContext(item=bridge.adapt_item(focus.item), **kwargs)
    else:  # pragma: no cover - Focus validates this
        raise ValueError("focus item is missing")
    if focus.kind is not FocusKind.ABSENT:
        context.position = focus.position
        context.size = focus.size
    return context


def _resolve_focus(focus: Focus | XdmItem | Sequence[Any] | None) -> Focus:
    if focus is None:
        return Focus.absent()
    if isinstance(focus, Focus):
        return focus
    if isinstance(focus, (str, bytes, bytearray)):
        sequence = as_xdm(focus)
        return Focus.singleton(sequence[0])
    if isinstance(focus, Sequence):
        if len(focus) == 0:
            return Focus.empty()
        if len(focus) > 1:
            raise ValueError("a focus accepts at most one context item")
        item = as_xdm(focus[0])
        if len(item) != 1:
            raise ValueError("a focus context item must be exactly one XDM item")
        return Focus.singleton(item[0])
    sequence = as_xdm(focus)
    if len(sequence) != 1:
        raise ValueError("a focus context item must be exactly one XDM item")
    return Focus.singleton(sequence[0])


def _continuation_variables(
    environment: ExpressionEnvironment,
    bridge: _ValueBridge,
) -> dict[str, Any] | None:
    values: dict[str, Any] = {}
    for name, value in environment.variables.items():
        if isinstance(value, XdmFunction) and isinstance(value.closure, XPathClosure):
            return None
        if isinstance(value, XdmFunction) or (callable(value) and not isinstance(value, XdmItem)):
            values[name] = _continuation_host_function(value, bridge, name)
            continue
        sequence = as_xdm(value)
        converted: list[Any] = []
        for item in sequence:
            if isinstance(item, XdmAtomic):
                converted.append(atomic_to_elementpath(item))
            elif isinstance(item, XdmFunction):
                if isinstance(item.closure, XPathClosure):
                    return None
                converted.append(_continuation_host_function(item, bridge, name))
            else:
                return None
        values[name] = converted[0] if len(converted) == 1 else tuple(converted)
    return values


def _continuation_host_function(
    target: Any,
    bridge: _ValueBridge,
    name: str,
) -> continuation.HostFunction:
    def callback(*arguments: Any) -> Any:
        xdm_arguments = tuple(as_xdm(argument) for argument in arguments)
        result = call_host_function(target, xdm_arguments, bridge.environment)
        return bridge.sequence_to_elementpath(result)

    arity = function_arity(target)
    host_arity = arity if isinstance(arity, int) else None
    if isinstance(arity, tuple) and arity[0] == arity[1]:
        host_arity = arity[0]
    return continuation.HostFunction(callback, name=name, arity=host_arity)


def _xpath_closure_to_xdm(closure: XPathClosure, bridge: _ValueBridge) -> XdmFunction:
    def implementation(arguments: Any, environment: Any, captured: Any) -> XdmSequence:
        del environment, captured
        native = tuple(bridge.sequence_to_elementpath(argument) for argument in arguments)
        return bridge.to_xdm_sequence(continuation.evaluate_closure(closure, native))

    return XdmFunction(implementation, arity=closure.arity, closure=closure)
