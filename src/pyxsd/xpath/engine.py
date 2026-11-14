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
import math
import operator
from abc import ABCMeta
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MethodType
from typing import Any, cast

from elementpath import XPathContext
from elementpath.datatypes import AnyAtomicType, UntypedAtomic
from elementpath.helpers import upper_camel_case
from elementpath.tdop import Parser
from elementpath.xpath1 import XPath1Parser
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
    XPathNode,
    XPathNodeTree,
)
from elementpath.xpath_tokens import XPathArray, XPathMap
from elementpath.xpath_tokens.functions import ExternalFunction, XPathFunction
from elementpath.xpath_tokens.tokens import ContextItemToken, VariableToken

from pyxsd import xsd_data_types as _xsd
from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmItem, XdmMap, XdmSequence, as_xdm
from pyxsd.xdm.annotations import schema_type_name
from pyxsd.xdm.legacy import (
    LegacyNodeSetError,
    ResultTreeFragment,
    is_legacy_number,
    legacy_number_from_string,
    legacy_round,
    number_to_string,
)
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
    FUNCTION_SCOPE_ATTRIBUTE,
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
_VERSION_ALIASES = {
    "1": "1.0",
    "1.0": "1.0",
    "2": "2.0",
    "3": "3.0",
    "3.0": "3.0",
    "3.1": "3.1",
    "2.0": "2.0",
}
_XSD_NS = "http://www.w3.org/2001/XMLSchema"
_FN_NS = "http://www.w3.org/2005/xpath-functions"
_UNTYPED = f"{{{_XSD_NS}}}untyped"
_UNTYPED_ATOMIC = f"{{{_XSD_NS}}}untypedAtomic"


class _LegacyXPath1Parser(XPath1Parser):
    """The XPath 1.0 grammar with evaluation-local function registration.

    elementpath's XPath 1.0 parser registers dynamic tokens on a class-level
    symbol table, which would leak one stylesheet evaluation's callbacks into
    another.  This subclass copies the table per instance and provides the
    ``_register`` seam the shared XSLT integration hooks use, mirroring the
    XPath 2.0+ parser family.
    """

    def __init__(self, namespaces: Mapping[str, str] | None = None) -> None:
        super().__init__(dict(namespaces or {}))
        self.symbol_table = copy.copy(self.symbol_table)

    def _register(self, symbol: str, class_name: str, **kwargs: Any) -> type[Any]:
        kwargs["symbol"] = symbol
        lookup_name = kwargs.setdefault("lookup_name", symbol)
        kwargs.setdefault("label", "symbol")
        bases = kwargs.get("bases", (self.token_base_class,))
        kwargs.update(
            {
                "__module__": self.__module__,
                "__qualname__": class_name,
                "__return__": None,
            }
        )
        token_class = ABCMeta(class_name, bases, kwargs)
        self.symbol_table[lookup_name] = cast(Any, token_class)
        self.tokenizer = None
        return cast(type[Any], token_class)

    def external_function(
        self,
        callback: Callable[..., Any],
        name: str | None = None,
        prefix: str | None = None,
        sequence_types: tuple[str, ...] = (),
        bp: int = 90,
    ) -> type[Any]:
        """Register one XSLT-defined function on this XPath 1.0 parser.

        elementpath's XPath 1.0 parser refuses external functions because the
        standalone grammar has no such concept.  XSLT embeds XPath 1.0 and
        defines functions in the standard function namespace; the callback is
        registered on this evaluation's symbol table.  Sequence-type matching
        belongs to the XSLT call boundary, which has already converted the
        arguments before the callback runs.
        """
        del sequence_types
        symbol: str = name or str(getattr(callback, "__name__", ""))
        namespace = _FN_NS if prefix is None else self.namespaces[prefix]
        return self._register(
            symbol,
            f"{upper_camel_case(symbol)}ExternalFunction",
            bases=(ExternalFunction,),
            namespace=namespace,
            label="external function",
            nargs=None,
            lbp=bp,
            rbp=bp,
            sequence_types=(),
            callback=staticmethod(callback),
        )


_PARSERS["1.0"] = _LegacyXPath1Parser


class _LegacyRtfValue(UntypedAtomic):
    """One result tree fragment exposed to the XPath 1.0 evaluator.

    The marker is an ``xs:untypedAtomic`` so string conversion works through
    the ordinary function set, while the fragment stays a distinct value that
    the node-set functions and the XSLT node-set contexts reject.
    """

    __slots__ = ("fragment",)

    def __init__(self, fragment: ResultTreeFragment) -> None:
        super().__init__(fragment.string_value)
        self.fragment = fragment


def _normalise_version(version: str | None) -> str:
    normalised = _VERSION_ALIASES.get((version or "").strip())
    if normalised is None:
        raise ValueError(f"unsupported XPath version: {version!r}")
    return normalised


def _arity_tuple(arity: int | tuple[int, int | None]) -> tuple[int, int | None]:
    if isinstance(arity, int):
        return arity, arity
    return arity


def _token_function_name(token: Any) -> str | None:
    """The expanded name of one function reference, when the token knows it.

    elementpath keeps the name of a built-in or declared function on the
    token's ``qname``; ``token.name`` is populated only for constructor
    functions.  An anonymous inline function has no ``qname`` and stays
    unnamed.
    """
    qname = getattr(token, "qname", None)
    local = getattr(qname, "local_name", None) if qname is not None else None
    if not isinstance(local, str) or not local:
        return None
    uri = getattr(qname, "uri", None)
    return f"{{{uri}}}{local}" if isinstance(uri, str) and uri else local


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


def _is_result_tree(node: XdmNode) -> bool:
    """Whether *node* is a document the result builder constructed.

    XSLT 1.0 variables and parameters initialized by content carry a result
    tree fragment.  The builder marks its document with the ``<result>``
    source name, while a parsed source or resource document keeps its own
    name, so the legacy model can tell the two apart without a hidden flag on
    XDM 3.1 values.
    """
    return isinstance(node, XdmDocument) and node.source_name == "<result>"


class _ValueBridge:
    """Convert XDM values to elementpath values and back for one evaluation."""

    def __init__(
        self,
        parser: Any,
        static_context: XPathStaticContext,
        environment: ExpressionEnvironment,
        *,
        version: str | None = None,
    ) -> None:
        self.parser = parser
        self.static_context = static_context
        self.environment = environment
        self._legacy = version == "1.0" and static_context.xslt_version == "1.0"
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
        if isinstance(item, ResultTreeFragment):
            return _LegacyRtfValue(item)
        if isinstance(item, XdmNode):
            if self._legacy and item.kind is NodeKind.DOCUMENT and _is_result_tree(item):
                # An XSLT 1.0 content-initialized variable, parameter, or
                # with-parameter is a result tree fragment, not a node-set.
                return _LegacyRtfValue(ResultTreeFragment(cast(XdmDocument, item)))
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
                    context_binder=_context_target_binder(value, self),
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
        if isinstance(value, _LegacyRtfValue):
            return (value.fragment,)
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
            name = _token_function_name(token)

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
    free_variable_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.source or not self.source.strip():
            raise ValueError("an XPath expression requires non-empty source")
        if not isinstance(self.static_context, XPathStaticContext):
            raise TypeError("static_context must be an XPathStaticContext")
        if self.version not in _PARSERS:
            raise ValueError(f"unsupported XPath version: {self.version!r}")
        object.__setattr__(self, "variable_names", frozenset(self.variable_names))
        object.__setattr__(self, "free_variable_names", frozenset(self.free_variable_names))


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
        stack.extend(getattr(token, "_values", ()) or ())
    return frozenset(names)


def _expression_free_variable_names(
    root: Any,
    static_context: XPathStaticContext,
) -> frozenset[str]:
    """Return expanded variable names *root* references from an outer scope.

    XPath binders (``for``/``let``/``some``/``every`` and inline function
    parameters) introduce names that are local to the expression, so a static
    visibility check must not treat those references as free.  The walk follows
    the parsed tree rather than the source text, which keeps string literals,
    comments, and locally bound names out of the result.
    """
    names: set[str] = set()

    def expand(value: str) -> str:
        return _expanded_variable_name(value, static_context)

    def visit(token: Any, bound: frozenset[str]) -> None:
        if isinstance(token, VariableToken):
            name = expand(token.value)
            if name not in bound:
                names.add(name)
            return

        symbol = getattr(token, "symbol", None)
        items = tuple(getattr(token, "_items", ()) or ())
        if symbol in ("for", "let", "some", "every"):
            local = bound
            for index in range(0, len(items) - 1, 2):
                visit(items[index + 1], local)
                binding = items[index]
                local = local | {expand(binding.value)}
            visit(items[-1], local)
            return
        if symbol == "function" and getattr(token, "body", None) is not None:
            local = bound | {expand(varname) for varname in getattr(token, "varnames", ()) or ()}
            visit(token.body, local)
            return
        for item in items:
            visit(item, bound)
        for value in getattr(token, "_values", ()) or ():
            visit(value, bound)

    visit(root, frozenset())
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

    def __init__(self, version: str = "3.1", *, xslt_functions: bool = True) -> None:
        if not isinstance(xslt_functions, bool):
            raise TypeError("xslt_functions must be a boolean")
        self._version = _normalise_version(version)
        self._parser_class = _PARSERS[self._version]
        self._xslt_functions = xslt_functions

    @property
    def version(self) -> str:
        return self._version

    @property
    def xslt_functions(self) -> bool:
        """Whether XSLT-only functions such as ``key`` are registered."""
        return self._xslt_functions

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

        parser = _build_parser(
            self._version,
            static_context,
            functions=None,
            key=self._xslt_functions,
        )
        root_token = parser.parse(source)
        variable_names = _expression_variable_names(root_token, static_context)
        free_variable_names = _expression_free_variable_names(root_token, static_context)

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
            free_variable_names=free_variable_names,
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
            key=self._xslt_functions,
            base_uri=environment.base_uri,
            decimal_formats=environment.decimal_formats,
        )
        bridge = _ValueBridge(parser, compiled.static_context, environment, version=self._version)
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

        try:
            result = parser.parse(compiled.source).evaluate(context)
            return bridge.to_xdm_sequence(result)
        except LegacyNodeSetError as exc:
            from pyxsd.xslt.errors import XsltDynamicError

            raise XsltDynamicError(str(exc), code=exc.code) from exc


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
    if version == "1.0":
        namespaces = dict(static_context.namespaces)
        namespaces.setdefault("fn", _FN_NS)
        namespaces.setdefault("xs", _XSD_NS)
        parser = parser_class(namespaces=namespaces)
        parser.base_uri = base_uri if base_uri is not None else static_context.base_uri
        parser.default_collation = static_context.default_collation
        parser.variable_types = dict(static_context.variable_types)
    else:
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
    if version == "1.0":
        _install_legacy_1_0_overrides(parser)
    else:
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
        setattr(marked, FUNCTION_SCOPE_ATTRIBUTE, True)
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
        active_target = target
        bind_context = getattr(target, "with_context", None)
        if callable(bind_context):
            active_target = bind_context(_live_context_item(holder, bridge))
        if active_target is None:
            raise RuntimeError(f"host function {name!r} has no environment implementation")
        return bridge.sequence_to_elementpath(
            call_host_function(
                active_target,
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
    inherited_evaluate = token_class.evaluate

    def evaluate(self: Any, context: Any = None) -> Any:
        previous = holder.xpath_context
        holder.xpath_context = context
        try:
            return inherited_evaluate(self, context)
        finally:
            holder.xpath_context = previous

    token_class.evaluate = evaluate

    inherited_call = token_class.__call__

    def call(self: Any, *args: Any, context: Any = None) -> Any:
        # elementpath saves the focus present when a function reference was
        # evaluated onto the function item and prefers it over the caller's
        # context in ``XPathFunction.__call__``.  A declared function saved in
        # a variable must observe each call's live focus instead, so the saved
        # focus is cleared for the call and restored afterwards.
        saved_context = self.context
        self.context = None
        try:
            return inherited_call(self, *args, context=context)
        finally:
            self.context = saved_context

    token_class.__call__ = call


def _live_context_item(holder: _BridgeHolder, bridge: _ValueBridge) -> Any:
    """The context item of the call being evaluated, if it has one."""
    raw = getattr(holder.xpath_context, "item", None)
    if raw is None:
        return None
    return bridge.to_xdm_sequence(raw)[0]


def _context_target_binder(target: Any, bridge: _ValueBridge) -> Callable[[Any], Any] | None:
    """Bind one variable-held function target to the focus of each call.

    Extension targets expose their focus-sensitive callback through
    ``with_context``, so every invocation re-resolves the target from the
    calling elementpath context's item.  Targets without ``with_context`` are
    focus-independent and keep the environment-bound value.
    """
    bind_context = getattr(target, "with_context", None)
    if not callable(bind_context):
        return None

    def bind(context: Any) -> Any:
        raw = getattr(context, "item", None) if context is not None else None
        item: XdmItem | None = None
        if raw is not None:
            sequence = bridge.to_xdm_sequence(raw)
            if sequence:
                item = sequence[0]
        return bind_context(item)

    return bind


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
        top_root: XdmNode | None = None
        if top_argument is not None:
            top = bridge.to_xdm_sequence(top_argument)
            if not top:  # pragma: no cover - elementpath enforces node()
                return []
            if len(top) != 1 or not isinstance(top[0], XdmNode):  # pragma: no cover
                raise ValueError("the third key() argument must be a single node")
            top_root = top[0].root
        nodes: list[XdmNode] = []
        for item in values:
            value = cast(XdmAtomic, item)
            nodes.extend(index.get(value.lexical, ()))
            nodes.extend(index.get(value.value, ()))
            nodes.extend(index.get(value, ()))
        unique: dict[int, XdmNode] = {id(node): node for node in nodes}
        ordered = sorted(unique.values(), key=lambda node: node.document_order)
        if top_root is not None:
            ordered = [node for node in ordered if node.root is top_root]
        return [bridge.adapt_item(node) for node in ordered]

    token_class = parser.external_function(
        key,
        name="key",
        sequence_types=("xs:string", "xs:anyAtomicType*", "node()", "element()*"),
    )
    token_class.label = "function"
    token_class.nargs = (2, 3)


def _install_legacy_1_0_overrides(parser: Any) -> None:
    """Install the XPath 1.0 conversions the shared token library leaves out.

    elementpath's XPath 1.0 grammar is complete, but a few evaluation details
    follow XPath 2.0 rules: general-comparison equality does not coerce a
    string to a number, ``string()`` spells infinities ``INF``, ``number()``
    accepts exponent notation, and boolean conversion of the legacy marker does
    not know that a result tree fragment is an always-true node-set.  The
    native profile owns those conversions here rather than inheriting 2.0
    behavior.
    """

    def active_context(token: Any, context: Any) -> Any:
        stored = getattr(token, "context", None)
        return context if stored is None else stored

    def is_fragment(value: Any) -> bool:
        return isinstance(value, _LegacyRtfValue)

    def legacy_equality(self: Any, context: Any = None) -> bool:
        op = operator.eq if self.symbol == "=" else operator.ne
        for left, right in self.iter_comparison_data(active_context(self, context)):
            if isinstance(left, bool) or isinstance(right, bool):
                left, right = bool(left), bool(right)
            elif is_legacy_number(left) or is_legacy_number(right):
                left, right = _legacy_number_value(left), _legacy_number_value(right)
            else:
                left, right = _legacy_string_value(left), _legacy_string_value(right)
            if op(left, right):
                return True
        return False

    def legacy_boolean(self: Any, context: Any = None) -> bool:
        active = active_context(self, context)
        items = list(self._items[0].select(active))
        if any(is_fragment(item) for item in items):
            return True
        return self.boolean_value(items)

    def legacy_not(self: Any, context: Any = None) -> bool:
        return not legacy_boolean(self, context)

    def legacy_count(self: Any, context: Any = None) -> int:
        active = active_context(self, context)
        items = list(self._items[0].select(active))
        if any(is_fragment(item) for item in items):
            raise LegacyNodeSetError(
                "fn:count() requires a node-set, but a result tree fragment "
                "was supplied; a native XSLT 1.0 processor does not convert "
                "a result tree fragment where a node-set is required"
            )
        return len(items)

    def legacy_id(base: Any) -> Any:
        def select(self: Any, context: Any = None) -> Any:
            active = active_context(self, context)
            if is_fragment(self.get_argument(active)):
                raise LegacyNodeSetError(
                    "fn:id() requires a node-set, but a result tree fragment "
                    "was supplied; a native XSLT 1.0 processor does not convert "
                    "a result tree fragment where a node-set is required"
                )
            return base.select(self, context)

        return select

    def legacy_string(self: Any, context: Any = None) -> str:
        active = active_context(self, context)
        if not self:
            item = getattr(active, "item", None) if active is not None else None
            return _legacy_string_value(item)
        items = ordered_items(self, active, 0)
        return _legacy_string_value(items[0]) if items else ""

    def legacy_number(self: Any, context: Any = None) -> float:
        active = active_context(self, context)
        if not self:
            item = getattr(active, "item", None) if active is not None else None
            return _legacy_number_value(item)
        items = ordered_items(self, active, 0)
        return _legacy_number_value(items[0]) if items else math.nan

    def ordered_items(token: Any, active: Any, index: int) -> list[Any]:
        items = list(token._items[index].select(active))
        if items and all(isinstance(item, XPathNode) for item in items):
            # XPath 1.0 node-sets are unordered; every conversion uses the
            # first node in document order, not the axis order.
            unique: dict[int, Any] = {id(item): item for item in items}
            return sorted(unique.values(), key=lambda node: node.position)
        return items

    def string_argument(token: Any, active: Any, index: int) -> str:
        items = ordered_items(token, active, index)
        return _legacy_string_value(items[0]) if items else ""

    def context_string_argument(token: Any, active: Any, index: int) -> str:
        if index < len(token._items):
            return string_argument(token, active, index)
        item = getattr(active, "item", None) if active is not None else None
        return _legacy_string_value(item)

    def number_argument(token: Any, active: Any, index: int) -> float:
        items = ordered_items(token, active, index)
        return _legacy_number_value(items[0]) if items else math.nan

    def legacy_concat(self: Any, context: Any = None) -> str:
        active = active_context(self, context)
        return "".join(string_argument(self, active, index) for index in range(len(self)))

    def legacy_contains(self: Any, context: Any = None) -> bool:
        active = active_context(self, context)
        return string_argument(self, active, 1) in string_argument(self, active, 0)

    def legacy_starts_with(self: Any, context: Any = None) -> bool:
        active = active_context(self, context)
        return string_argument(self, active, 0).startswith(string_argument(self, active, 1))

    def legacy_substring_before(self: Any, context: Any = None) -> str:
        active = active_context(self, context)
        source = string_argument(self, active, 0)
        search = string_argument(self, active, 1)
        index = source.find(search)
        return "" if index < 0 else source[:index]

    def legacy_substring_after(self: Any, context: Any = None) -> str:
        active = active_context(self, context)
        source = string_argument(self, active, 0)
        search = string_argument(self, active, 1)
        index = source.find(search)
        return "" if index < 0 else source[index + len(search) :]

    def legacy_substring(self: Any, context: Any = None) -> str:
        active = active_context(self, context)
        source = string_argument(self, active, 0)
        start = number_argument(self, active, 1)
        if math.isnan(start) or math.isinf(start):
            return ""
        begin = int(legacy_round(start)) - 1
        if len(self) == 2:
            return source[max(begin, 0) :]
        length = number_argument(self, active, 2)
        if math.isnan(length) or length <= 0:
            return ""
        if math.isinf(length):
            return source[max(begin, 0) :]
        stop = begin + int(legacy_round(length))
        return source[slice(max(begin, 0), max(stop, 0))]

    def legacy_string_length(self: Any, context: Any = None) -> int:
        active = active_context(self, context)
        return len(context_string_argument(self, active, 0))

    def legacy_normalize_space(self: Any, context: Any = None) -> str:
        active = active_context(self, context)
        return " ".join(context_string_argument(self, active, 0).split())

    def legacy_translate(self: Any, context: Any = None) -> str:
        active = active_context(self, context)
        source = string_argument(self, active, 0)
        mapping = string_argument(self, active, 1)
        replacement = string_argument(self, active, 2)
        if len(mapping) == len(replacement):
            return source.translate(str.maketrans(mapping, replacement))
        if len(mapping) > len(replacement):
            kept = len(replacement)
            return source.translate(str.maketrans(mapping[:kept], replacement, mapping[kept:]))
        return source.translate(str.maketrans(mapping, replacement[: len(mapping)]))

    def legacy_sum(self: Any, context: Any = None) -> float:
        active = active_context(self, context)
        items = ordered_items(self, active, 0)
        if not items:
            return 0.0
        total = 0.0
        for item in items:
            total += _legacy_number_value(item)
        return total

    def registered(symbol: str, base: Any, class_name: str, evaluate: Any) -> Any:
        options: dict[str, Any] = {"bases": (base,), "evaluate": evaluate}
        for attribute in ("label", "nargs", "sequence_types"):
            if hasattr(base, attribute):
                options[attribute] = getattr(base, attribute)
        return parser._register(symbol, class_name, **options)

    for symbol, class_name, evaluate in (
        ("=", "_PyxsdLegacyEqual", legacy_equality),
        ("!=", "_PyxsdLegacyNotEqual", legacy_equality),
        ("boolean", "_PyxsdLegacyBoolean", legacy_boolean),
        ("not", "_PyxsdLegacyNot", legacy_not),
        ("count", "_PyxsdLegacyCount", legacy_count),
        ("string", "_PyxsdLegacyString", legacy_string),
        ("number", "_PyxsdLegacyNumber", legacy_number),
        ("concat", "_PyxsdLegacyConcat", legacy_concat),
        ("contains", "_PyxsdLegacyContains", legacy_contains),
        ("starts-with", "_PyxsdLegacyStartsWith", legacy_starts_with),
        ("substring-before", "_PyxsdLegacySubstringBefore", legacy_substring_before),
        ("substring-after", "_PyxsdLegacySubstringAfter", legacy_substring_after),
        ("substring", "_PyxsdLegacySubstring", legacy_substring),
        ("string-length", "_PyxsdLegacyStringLength", legacy_string_length),
        ("normalize-space", "_PyxsdLegacyNormalizeSpace", legacy_normalize_space),
        ("translate", "_PyxsdLegacyTranslate", legacy_translate),
    ):
        registered(symbol, parser.symbol_table[symbol], class_name, evaluate)
    sum_base = parser.symbol_table["sum"]
    parser._register(
        "sum",
        "_PyxsdLegacySum",
        bases=(sum_base,),
        label=sum_base.label,
        nargs=1,
        sequence_types=sum_base.sequence_types,
        evaluate=legacy_sum,
    )
    id_base = parser.symbol_table["id"]
    id_options: dict[str, Any] = {
        "bases": (id_base,),
        "label": id_base.label,
        "nargs": id_base.nargs,
        "sequence_types": id_base.sequence_types,
        "select": legacy_id(id_base),
    }
    parser._register("id", "_PyxsdLegacyId", **id_options)
    _install_legacy_path_operators(parser)


def _install_legacy_path_operators(parser: Any) -> None:
    """Allow a function call as the left operand of ``/`` and ``//``.

    XPath 1.0's ``FilterExpr '/' RelativeLocationPath`` production admits any
    primary expression, including a function call.  elementpath's XPath 1.0
    grammar only admits the path-step and ``$`` forms, so ``document('x')/y``
    would be rejected as a syntax error.  The XSLT function set makes that
    production meaningful (``document()``, ``key()``, and ``id()`` all return
    node-sets), so the native profile restores it.
    """

    for symbol in ("/", "//"):
        base = parser.symbol_table[symbol]

        def led(self: Any, left: Any, _base: Any = base) -> Any:
            if isinstance(left, XPathFunction):
                self[:] = left, self.parser.expression(75)
                return self
            return _base.led(self, left)

        parser._register(
            symbol,
            f"_PyxsdLegacyPath{symbol.replace('/', 'Slash')}",
            bases=(base,),
            label=base.label,
            lbp=base.lbp,
            led=led,
        )


def _legacy_string_value(value: Any) -> str:
    """The XPath 1.0 string conversion of one evaluator value."""
    if isinstance(value, _LegacyRtfValue):
        return value.value
    if isinstance(value, XPathNode):
        return value.string_value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return number_to_string(value)
    if isinstance(value, Decimal):
        logical = format(value, "f")
        return logical.rstrip("0").rstrip(".") if "." in logical else logical
    if isinstance(value, int):
        return str(value)
    if isinstance(value, UntypedAtomic):
        return value.value
    return str(value)


def _legacy_number_value(value: Any) -> float:
    """The XPath 1.0 number conversion of one evaluator value."""
    if isinstance(value, _LegacyRtfValue):
        return legacy_number_from_string(value.value)
    if isinstance(value, XPathNode):
        return legacy_number_from_string(value.string_value)
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, float):
        return value
    if isinstance(value, (int, Decimal)):
        return float(value)
    if isinstance(value, UntypedAtomic):
        return legacy_number_from_string(value.value)
    if isinstance(value, str):
        return legacy_number_from_string(value)
    return math.nan


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
        #: The elementpath context of the call currently being evaluated.  A
        #: declared external function reads it to expose the live focus item to
        #: a contextual Python extension instead of the focus captured when the
        #: expression environment was built.
        self.xpath_context: Any = None


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
