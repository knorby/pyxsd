"""Versioned, schema-aware XPath evaluation without an XSLT transformation.

The engine compiles an expression against an immutable
:class:`XPathStaticContext` and evaluates it with an invocation-local
:class:`ExpressionEnvironment`.  Every evaluation receives a fresh dynamic
``XPathContext``; compiled expressions never retain mutable token state, and no
parser class-level symbol table is modified.

An environment may carry an :class:`XPathParserCache` to reuse one built parser
per distinct static configuration for the lifetime of one transformation.  The
cache supplies every dynamic-context component per evaluation through the
bucket's bridge holder, so a reused parser answers with the running evaluation's
focus, variables, override targets, and declared-function implementations.  An
environment without a cache builds a parser per evaluation, which is the
standalone behaviour.

XDM nodes are exposed to elementpath through grounded adapter nodes, so path
expressions, predicates, attributes, comments, text, and processing
instructions keep Task 8 identity and Task 7 typed values.  Results are
converted back to XDM sequences, maps, arrays, and function items rather than
collapsing into Python lists or strings.

An environment may also carry an :class:`_XdmAdapterRegistry` to reuse one
adapter tree per document for the lifetime of one transformation.  A document
is registered only when its document order is finalized (``document_order ==
0``), so a builder tree that may still grow is never registered and is adapted
afresh for each evaluation, as is every detached temporary tree.  Registered
trees keep one adapter object per XDM node for the whole transformation, which
is what makes node identity, document order, and union deduplication agree
across evaluations.  A node handed to an evaluation that its own document does
not contain — a descendant of a temporary tree — is adapted from its root so
its ancestors and siblings exist for that evaluation.  An XSLT 1.0 result tree
fragment never reaches this layer as a node (it is a legacy fragment wrapper),
so a 1.0 transformation reuses adapter trees only for parsed and resource
documents.

If a future builder path appends to a document after its document order is
finalized, the fix is a structural revision counter on ``XdmDocument`` that the
admission rule compares, not a cache flush: adapters of a registered tree are
immutable and are never dropped while the transformation lives.

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
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MethodType, SimpleNamespace
from typing import Any, cast

from elementpath import XPathContext
from elementpath.datatypes import (
    AbstractDateTime,
    AnyAtomicType,
    ArithmeticProxy,
    DayTimeDuration,
    Duration,
    QName,
    UntypedAtomic,
)
from elementpath.datatypes.datetime import get_comparable_datetimes
from elementpath.exceptions import ElementPathError
from elementpath.helpers import upper_camel_case
from elementpath.namespaces import get_expanded_name
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
from elementpath.xpath_tokens import ProxyToken, XPathArray, XPathConstructor, XPathMap, XPathToken
from elementpath.xpath_tokens.functions import ExternalFunction, XPathFunction
from elementpath.xpath_tokens.tokens import ContextItemToken, VariableToken

from pyxsd import xsd_data_types as _xsd
from pyxsd.namespaces import XML_NS
from pyxsd.xdm import (
    XdmArray,
    XdmAtomic,
    XdmFunction,
    XdmItem,
    XdmMap,
    XdmSequence,
    as_xdm,
    type_tests,
)
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
    ContextFunctionToken,
    HostFunctionToken,
    atomic_to_elementpath,
    call_host_function,
    clone_function_token,
    function_arity,
    install_date_time,
    install_deep_equal,
    install_regex_replace,
    install_string_conversion_functions,
    install_string_function,
    sequence_to_elementpath,
)
from pyxsd.xpath.numeric import install_numeric_functions
from pyxsd.xpath.regex_cache import install_regex_translation_cache

# Memoise elementpath's regex translation at this import seam.  The call has to
# follow the elementpath imports above: the installer sweeps the modules that
# have already bound ``translate_pattern``, and patching its definition module
# covers any elementpath module imported later.
install_regex_translation_cache()

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


class _XdmMapValue(XPathMap):
    """A bridged XDM map whose dynamic call applies function conversion.

    XPath 3.1 treats a map as a function ``function(xs:anyAtomicType) as
    item()*``; a dynamic call therefore atomizes its argument (so a node key
    is looked up by typed value), converts an untyped atomic key to
    ``xs:string``, and rejects any other cardinality with ``XPTY0004``.
    elementpath's own map call accepts only an already-adapted atomic and
    reports the wrong code otherwise, so the bridge owns the conversion.
    """

    def _call_key(self, args: tuple[Any, ...]) -> Any:
        values: list[Any] = []
        if len(args) == 1:
            values = list(self.atomize_item(args[0]))
        if len(values) != 1:
            raise self.error("XPTY0004", "a map call requires exactly one atomic argument")
        key = values[0]
        return str(key) if isinstance(key, UntypedAtomic) else key

    def __call__(self, *args: Any, context: Any = None) -> Any:
        return super().__call__(self._call_key(args), context=context)


class _XdmArrayValue(XPathArray):
    """A bridged XDM array whose dynamic call applies function conversion.

    An array behaves as ``function(xs:integer) as item()*``: the argument is
    atomized, an untyped atomic value is cast to ``xs:integer``, and any other
    cardinality or type is ``XPTY0004``.
    """

    def _call_position(self, args: tuple[Any, ...]) -> int:
        values: list[Any] = []
        if len(args) == 1:
            values = list(self.atomize_item(args[0]))
        if len(values) != 1:
            raise self.error("XPTY0004", "an array call requires exactly one xs:integer argument")
        position = values[0]
        if isinstance(position, UntypedAtomic):
            try:
                return int(_xsd.Integer(str(position)))
            except (TypeError, ValueError) as exc:
                raise self.error("FORG0001", str(exc)) from None
        return position

    def __call__(self, *args: Any, context: Any = None) -> Any:
        return super().__call__(self._call_position(args), context=context)


class _NodeAdapterMixin:
    """Base for grounded adapters; every concrete node kind adds its protocol."""

    _xdm: XdmNode

    @property
    def xdm(self) -> XdmNode:
        return self._xdm


class _XdmDocumentAdapter(DocumentNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(self, xdm: XdmDocument, factory: Any) -> None:
        self._xdm = xdm
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

    @property
    def base_uri(self) -> str | None:
        return self._xdm.base_uri


class _XdmElementAdapter(ElementNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(
        self,
        xdm: XdmNode,
        parent: DocumentNode | ElementNode | None,
        factory: Any,
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
    def node_name(self) -> QName | None:
        return _adapter_node_name(self._xdm, is_attribute=False)

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
    def base_uri(self) -> str | None:
        return self._xdm.base_uri

    @property
    def iter_typed_values(self) -> Any:
        for item in self._xdm.typed_value:
            yield atomic_to_elementpath(cast(XdmAtomic, item))


class _XdmAttributeAdapter(AttributeNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(self, xdm: XdmNode, parent: ElementNode | None, factory: Any) -> None:
        del factory
        self._xdm = xdm
        self.name = xdm.name
        self.value = xdm.string_value
        self.parent = parent
        self.position = xdm.document_order
        self.xsd_type = None

    @property
    def node_name(self) -> QName | None:
        return _adapter_node_name(self._xdm, is_attribute=True)

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
        return self._xdm.base_uri


class _XdmTextAdapter(TextNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(
        self, xdm: XdmNode, parent: DocumentNode | ElementNode | None, factory: Any
    ) -> None:
        del factory
        self._xdm = xdm
        self.name = None
        self.value = xdm.string_value
        self.parent = cast(ElementNode | None, parent)
        self.position = xdm.document_order

    @property
    def base_uri(self) -> str | None:
        return self._xdm.base_uri


class _XdmCommentAdapter(CommentNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(
        self,
        xdm: XdmNode,
        parent: DocumentNode | ElementNode | None,
        factory: Any,
    ) -> None:
        del factory
        self._xdm = xdm
        super().__init__(xdm.string_value, None, xdm.document_order)
        self.parent = parent

    @property
    def base_uri(self) -> str | None:
        return self._xdm.base_uri


class _XdmProcessingInstructionAdapter(ProcessingInstructionNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(
        self,
        xdm: XdmNode,
        parent: DocumentNode | ElementNode | None,
        factory: Any,
    ) -> None:
        del factory
        self._xdm = xdm
        super().__init__(xdm.target or "", xdm.string_value, None, xdm.document_order)
        self.parent = parent

    @property
    def base_uri(self) -> str | None:
        return self._xdm.base_uri


class _XdmNamespaceAdapter(NamespaceNode, _NodeAdapterMixin):
    __slots__ = ("_xdm",)

    def __init__(self, xdm: XdmNode, parent: ElementNode | None, factory: Any) -> None:
        del factory
        self._xdm = xdm
        super().__init__(xdm.prefix, xdm.bound_uri or "", parent, xdm.document_order)

    @property
    def base_uri(self) -> str | None:
        return self._xdm.base_uri


def _adapter_node_name(xdm: XdmNode, *, is_attribute: bool) -> QName | None:
    """The lexical QName of one element or attribute adapter.

    XDM keeps the prefix the construction or copy path selected, which a
    namespace map cannot recover when a default and a prefixed binding name
    the same URI.  An attribute in a namespace always needs a non-empty
    prefix, so a missing stored prefix falls back to a binding for the URI.
    """
    local = xdm.local_name or ""
    prefix = xdm.prefix
    namespace = xdm.namespace_uri
    if (
        namespace is not None
        and not prefix
        and (is_attribute or xdm.in_scope_namespaces.get("") != namespace)
    ):
        prefix = next(
            (
                candidate
                for candidate, uri in xdm.in_scope_namespaces.items()
                if candidate and uri == namespace
            ),
            "",
        )
    lexical = f"{prefix}:{local}" if prefix else local
    return QName(namespace, lexical)


def _adapter_node(node: XdmNode, parent: Any, factory: Any) -> Any:
    """Construct the adapter for one non-document node of the tree.

    The two factories — an evaluation's bridge for ephemeral and detached
    builds and an :class:`_XdmAdapterRegistry` for registered builds — share
    this dispatch so they cannot disagree about which adapter a kind gets.
    Documents are handled by their factory before this point: a document is
    built once and every other node of its tree is reached through it.
    """
    if node.kind is NodeKind.ELEMENT:
        return _XdmElementAdapter(node, parent, factory)
    if node.kind is NodeKind.ATTRIBUTE:
        return _XdmAttributeAdapter(node, parent, factory)
    if node.kind is NodeKind.TEXT:
        return _XdmTextAdapter(node, parent, factory)
    if node.kind is NodeKind.COMMENT:
        return _XdmCommentAdapter(node, parent, factory)
    if node.kind is NodeKind.PROCESSING_INSTRUCTION:
        return _XdmProcessingInstructionAdapter(node, parent, factory)
    if node.kind is NodeKind.NAMESPACE:
        return _XdmNamespaceAdapter(node, parent, factory)
    raise TypeError(  # pragma: no cover - the six non-document kinds are handled above
        f"unsupported XDM node kind: {node.kind!r}"
    )


def _document_node_count(document: XdmDocument) -> int:
    """How many tree nodes *document* holds, in the census's unit.

    The count is of child-axis nodes: the document itself, elements, text,
    comments, and processing instructions.  Attribute and namespace adapters
    are built eagerly beside their parent element and measured about three
    further adapters per element, so counting them here would triple the
    budget's unit.  The stage's own census counts these same child-axis nodes,
    so the budget and the census are stated in one unit.
    """
    count = 0
    stack: list[XdmNode] = [document]
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(node.children)
    return count


def _rootless_node_count(root: XdmNode) -> int:
    """How many child-axis nodes an element-rooted temporary tree holds.

    The tree has no document node above its root, so the count starts at the
    root and follows the same unit as :func:`_document_node_count`: elements,
    text, comments, and processing instructions, not attributes or namespaces.
    """
    count = 0
    stack: list[XdmNode] = [root]
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(node.children)
    return count


class _XdmAdapterRegistry:
    """One transformation's XDM→elementpath adapter trees.

    A document is registered by identity once its document order is finalized
    (``document.document_order == 0``).  Every adapter of a registered tree
    lives in ``nodes`` and is reused by every later evaluation of the same
    transformation.  One registry belongs to one transformation invocation: it
    is never process-global, never shared between transformations, and never
    shared between threads.

    Admission is capped twice: by document count and by a total node budget.
    The budget's unit is the census's child-axis node count — the document,
    elements, text, comments, and processing instructions, as
    ``_document_node_count`` walks them — not the adapter count, which is about
    two and a half times larger because attribute and namespace adapters are
    built eagerly beside their element.  Counting adapters would refuse the
    documents the stage measures in that same census unit.

    A document that would exceed either cap is refused and adapted for the
    current evaluation only, exactly as before; nothing is ever evicted,
    because dropping a document would remove its identity mapping while
    function items that captured its adapters keep the adapters alive.
    Identity is injective only while the map and the live adapter set agree.
    Refusal is per document, so a smaller document that still fits is admitted
    after a larger one was refused; the admitted total never exceeds the
    budget.
    """

    __slots__ = (
        "_admitted_nodes",
        "_built",
        "_documents",
        "_max_documents",
        "_max_nodes",
        "_nodes_built",
        "_reused",
        "_rootless",
        "annotations",
        "nodes",
    )

    def __init__(self, *, max_documents: int = 512, max_nodes: int = 250_000) -> None:
        if isinstance(max_documents, bool) or not isinstance(max_documents, int):
            raise TypeError("max_documents must be an integer")
        if max_documents < 1:
            raise ValueError("max_documents must be at least 1")
        if isinstance(max_nodes, bool) or not isinstance(max_nodes, int):
            raise TypeError("max_nodes must be an integer")
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1")
        #: Every adapter of every registered tree, keyed by XDM node identity.
        #: The adapter holds its XDM node, so an id in this map is never
        #: recycled into another node.
        self.nodes: dict[int, Any] = {}
        #: Type annotations of bridged atomic values, shared by every
        #: evaluation in the transformation so a value passed to
        #: ``xsl:evaluate`` keeps its schema-defined type.
        self.annotations: dict[int, tuple[Any, str]] = {}
        self._documents: dict[int, _XdmDocumentAdapter] = {}
        #: Finalized element-rooted temporary trees, keyed by the identity of
        #: the document that carries them; the value is the admitted
        #: child-axis node count.
        self._rootless: dict[int, int] = {}
        self._max_documents = max_documents
        self._max_nodes = max_nodes
        self._admitted_nodes = 0
        self._built = 0
        self._reused = 0
        self._nodes_built = 0

    def document(self, document: XdmDocument) -> _XdmDocumentAdapter | None:
        """The adapter for *document*, registering it on first use.

        ``None`` means the document is not registrable: its document order is
        not finalized (a builder tree that may still grow), the registry
        already holds ``max_documents`` documents, or admitting it would
        exceed the node budget.  The caller then adapts it for this evaluation
        only, exactly as before.
        """
        adapter = self._documents.get(id(document))
        if adapter is not None:
            self._reused += 1
            return adapter
        if id(document) in self._rootless:
            # The document carries a registered element-rooted tree, which is a
            # different adapter view of the same nodes; adapt the empty
            # document for this evaluation only.
            return None
        if document.document_order != 0:
            return None
        if self.document_count >= self._max_documents:
            return None
        count = _document_node_count(document)
        if self._admitted_nodes + count > self._max_nodes:
            return None
        adapter = _XdmDocumentAdapter(document, self)
        self._documents[id(document)] = adapter
        self.nodes[id(document)] = adapter
        self._admitted_nodes += count
        self._built += 1
        return adapter

    def rootless(self, root: XdmNode) -> Any | None:
        """The adapter for a finalized element-rooted temporary tree.

        The tree's root has no document parent, so it is not built by
        :meth:`document`; the root adapter and every descendant are still
        registered in ``nodes`` and reused by later evaluations.  ``None``
        means the tree is not registrable: its document order is not finalized,
        the document already carries another registered view, the document
        count is exhausted, or admitting the tree would exceed the node budget.
        """
        adapter = self.nodes.get(id(root))
        if adapter is not None:
            self._reused += 1
            return adapter
        document = root.document
        if document.document_order != 0:
            return None
        if id(document) in self._documents or id(document) in self._rootless:
            return None
        if len(self._documents) + len(self._rootless) >= self._max_documents:
            return None
        count = _rootless_node_count(root)
        if self._admitted_nodes + count > self._max_nodes:
            return None
        adapter = _adapter_node(root, None, self)
        self.nodes[id(root)] = adapter
        self._nodes_built += 1
        self._rootless[id(document)] = count
        self._admitted_nodes += count
        self._built += 1
        return adapter

    def lookup(self, node: XdmNode) -> Any | None:
        """The registered adapter for *node*, or ``None`` when unregistered."""
        return self.nodes.get(id(node))

    def adapt_node(self, node: XdmNode, parent: Any = None) -> Any:
        """Construct and register one node of the tree currently being built.

        This is the factory protocol the adapter constructors already call
        (``factory.adapt_node(child, self)``), so a registered document is
        built with the registry as its factory and every descendant lands in
        ``nodes`` in one pass.  A node that is already registered returns its
        existing adapter, so the method is idempotent.
        """
        adapter = self.nodes.get(id(node))
        if adapter is not None:
            return adapter
        adapter = _adapter_node(node, parent, self)
        self.nodes[id(node)] = adapter
        self._nodes_built += 1
        return adapter

    def clear(self) -> None:
        """Drop every registered document and its node adapters.

        Never needed for correctness; used by tests.
        """
        self.nodes.clear()
        self._documents.clear()
        self._rootless.clear()
        self._admitted_nodes = 0

    @property
    def documents_built(self) -> int:
        """How many adapter trees this registry has constructed."""
        return self._built

    @property
    def documents_reused(self) -> int:
        """How many lookups were answered by an existing tree."""
        return self._reused

    @property
    def document_count(self) -> int:
        """How many registered document views are currently held."""
        return len(self._documents) + len(self._rootless)

    @property
    def nodes_built(self) -> int:
        """How many non-document adapters this registry has constructed."""
        return self._nodes_built

    @property
    def admitted_nodes(self) -> int:
        """How many child-axis nodes the admitted documents hold."""
        return self._admitted_nodes


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


class _AnnotatedString(str):
    """A unique string payload carrying its XDM type annotation."""


class _AnnotatedDecimal(Decimal):
    """A unique decimal payload carrying its XDM type annotation."""


class _AnnotatedFloat(float):
    """A unique double payload carrying its XDM type annotation."""


#: Annotation entries retained for values that cannot carry an attribute
#: (booleans and substrate values whose copy is not distinct).
_ANNOTATION_LIMIT = 4096


def _annotate_atomic_value(value: Any, annotation: str) -> Any:
    """Return a distinct substrate value annotated with its XDM type.

    ``elementpath`` returns the same object for equal string payloads, so an
    identity-keyed side table cannot distinguish two declared types.  A
    subclass instance is distinct for each adaptation and still satisfies the
    substrate's value-space checks.  Values that cannot be subclassed or
    copied keep the side table.
    """
    if isinstance(value, bool):
        return value
    if type(value) is str:
        annotated: Any = _AnnotatedString(value)
    elif isinstance(value, Decimal):
        annotated = _AnnotatedDecimal(value)
    elif type(value) is float:
        annotated = _AnnotatedFloat(value)
    else:
        annotated = copy.copy(value)
        if annotated is value:
            return value
    try:
        annotated._pyxsd_type = annotation
    except AttributeError:  # pragma: no cover - substrate scalar proxies
        return value
    return annotated


def _record_annotation(
    annotations: dict[int, tuple[Any, str]], value: Any, annotation: str
) -> None:
    """Record one annotation for a value that cannot carry an attribute."""
    if len(annotations) >= _ANNOTATION_LIMIT:
        del annotations[next(iter(annotations))]
    annotations[id(value)] = (value, annotation)


class _PoolSchemaProxy:
    """A schema proxy facade over every namespace in one SchemaPool."""

    __slots__ = ("_namespaces", "_parser", "_pool")

    def __init__(self, pool: SchemaPool, namespaces: Mapping[str, str], parser: Any = None) -> None:
        self._pool = pool
        self._namespaces = dict(namespaces)
        self._parser = parser

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
        annotation = self._annotation(obj)
        if annotation is not None:
            return self._annotation_is_user_type(annotation, adapter)
        from elementpath.datatypes import UntypedAtomic

        if isinstance(obj, UntypedAtomic):
            # XDM 3.1 section 2.7.2: xs:untypedAtomic is not derived from any
            # other atomic type, and value-space validation does not change
            # an item's dynamic type.
            return (adapter.name or "").rpartition("}")[2] in (
                "untypedAtomic",
                "anyAtomicType",
                "anySimpleType",
            )
        return adapter.is_valid(obj, namespaces=self._namespaces)

    def _annotation_is_user_type(self, annotation: str, adapter: Any) -> bool:
        """Whether an XDM type annotation names *adapter*'s type or a subtype."""
        if annotation == adapter.name:
            return True
        local = annotation.rpartition("}")[2].rpartition(":")[2]
        required = (adapter.name or "").rpartition("}")[2].rpartition(":")[2]
        if annotation == _UNTYPED:
            return required == "anyType"
        if annotation == _UNTYPED_ATOMIC:
            return required in ("anyAtomicType", "anySimpleType", "anyType")
        xsd_prefix = f"{{{_XSD_NS}}}"
        if annotation.startswith(xsd_prefix) and (adapter.name or "").startswith(xsd_prefix):
            from pyxsd.xdm.values import is_xsd_subtype

            return is_xsd_subtype(local, required)
        annotated = self._pool.get_type(annotation)
        if annotated is None:
            return False
        recorded = SimpleNamespace(
            type_name=annotation, actual_type=getattr(annotated, "_type_class", None)
        )
        return type_tests.derives_from(recorded, adapter.name, self._pool)

    def _annotation(self, obj: Any) -> str | None:
        """The XDM type annotation of one bridged value, when it has one.

        The substrate's value classes carry no user-defined type name, so the
        bridge records it beside the converted object.  A value produced by an
        XPath operation (not by a variable, parameter, or function result) has
        no record and keeps the substrate's value-space answer.
        """
        attached = getattr(obj, "_pyxsd_type", None)
        if isinstance(attached, str):
            return attached
        holder = getattr(self._parser, "_pyxsd_bridge_holder", None)
        bridge = getattr(holder, "bridge", None) if holder is not None else None
        record = getattr(bridge, "_annotations", {}).get(id(obj)) if bridge is not None else None
        return None if record is None else record[1]

    def get_type(self, qname: str) -> Any:
        return self._pool.get_type(self.expand(qname))

    def get_element(self, qname: str) -> Any:
        return self._pool.get_element(self.expand(qname))

    def get_attribute(self, qname: str) -> Any:
        return self._pool.get_attribute(self.expand(qname))

    def get_substitution_group(self, qname: str) -> Any:
        return self._pool.get_substitution_group(self.expand(qname))

    def has_element(self, name: str) -> bool:
        return type_tests.declaration_exists(self._pool, "schema-element", name)

    def has_attribute(self, name: str) -> bool:
        return type_tests.declaration_exists(self._pool, "schema-attribute", name)

    def matches_element_test(
        self,
        node: XdmNode,
        name: str | None,
        type_name: str | None,
        nillable: bool,
    ) -> bool:
        return type_tests.matches_element_test(node, name, type_name, nillable, self._pool)

    def matches_attribute_test(
        self,
        node: XdmNode,
        name: str | None,
        type_name: str | None,
    ) -> bool:
        return type_tests.matches_attribute_test(node, name, type_name, self._pool)

    def matches_schema_element(self, node: XdmNode, name: str) -> bool:
        return type_tests.matches_schema_element(node, name, self._pool)

    def matches_schema_attribute(self, node: XdmNode, name: str) -> bool:
        return type_tests.matches_schema_attribute(node, name, self._pool)


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
        registry: _XdmAdapterRegistry | None = None,
    ) -> None:
        self.parser = parser
        self.static_context = static_context
        self.environment = environment
        self._legacy = version == "1.0" and static_context.xslt_version == "1.0"
        self._ephemeral: dict[int, Any] = {}
        self._registry = registry
        self._context: XPathContext | None = None
        #: Type annotations of the XdmAtomic values converted for this
        #: evaluation, shared with the transformation's adapter registry.  The
        #: map holds the converted object so its id cannot be recycled while
        #: the annotation is live; elementpath's schema proxy consults it
        #: before falling back to a value-space test.
        self._annotations = registry.annotations if registry is not None else {}

    # XDM -> elementpath

    def adapt_document(self, document: XdmDocument) -> _XdmDocumentAdapter:
        if self._registry is not None:
            registered = self._registry.document(document)
            if registered is not None:
                return registered
        adapter = self._ephemeral.get(id(document))
        if adapter is None:
            adapter = _XdmDocumentAdapter(document, self)
            self._ephemeral[id(document)] = adapter
        return adapter

    def adapt_node(self, node: XdmNode, parent: Any = None) -> Any:
        existing = self._lookup(node)
        if existing is not None:
            return existing
        if node.kind is NodeKind.DOCUMENT:
            return self.adapt_document(cast(XdmDocument, node))
        if parent is None:
            if node.kind is NodeKind.ELEMENT:
                return self.adapt_node(node.document)
            raise ValueError(f"a {node.kind.value} node requires a parent adapter")
        adapter = _adapter_node(node, parent, self)
        self._ephemeral[id(node)] = adapter
        return adapter

    def _lookup(self, node: XdmNode) -> Any | None:
        adapter = self._ephemeral.get(id(node))
        if adapter is None and self._registry is not None:
            adapter = self._registry.lookup(node)
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
        if node.kind is NodeKind.NAMESPACE:
            return _XdmNamespaceAdapter(node, None, self)
        raise ValueError(  # pragma: no cover - every non-document kind is handled above
            f"a detached {node.kind.value} node has no parent"
        )

    def adapt_item(self, item: XdmItem) -> Any:
        if isinstance(item, XdmAtomic):
            converted = atomic_to_elementpath(item)
            record = self._annotations.get(id(item))
            annotation = record[1] if record is not None else item.type_name
            annotated = _annotate_atomic_value(converted, annotation)
            if annotated is converted:
                _record_annotation(self._annotations, converted, annotation)
            return annotated
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
                adapter = self._lookup(item)
                if adapter is None and self._registry is not None:
                    adapter = self._registry.rootless(item)
                if adapter is None:
                    adapter = self._adapt_rootless_node(item)
                    self._ephemeral[id(item)] = adapter
                return adapter
            adapter = self._lookup(item)
            if (
                adapter is None
                and self._registry is not None
                and item.root.kind is NodeKind.ELEMENT
            ):
                # A descendant of a finalized element-rooted temporary tree:
                # admit the tree through its parentless root before falling
                # back to a per-evaluation adaptation.
                adapter = self._registry.rootless(item.root)
            if adapter is None:
                self.adapt_document(item.document)
                adapter = self._lookup(item)
            if adapter is None:
                # The node's document does not contain it: an element-rooted
                # temporary tree from a sequence constructor.  Adapt its root
                # so ancestors and siblings exist for this evaluation, then
                # take the node's own adapter from the tree that was built.
                root_adapter = self._adapt_rootless_node(item.root)
                self._ephemeral[id(item.root)] = root_adapter
                adapter = self._lookup(item)
            assert adapter is not None
            return adapter
        if isinstance(item, XdmAtomic):
            return atomic_to_elementpath(item)
        if isinstance(item, XdmMap):
            entries = [
                (atomic_to_elementpath(key), self.sequence_to_elementpath(value))
                for key, value in item.entries
            ]
            return _XdmMapValue(self.parser, items=entries)
        if isinstance(item, XdmArray):
            return _XdmArrayValue(
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
                to_xdm=self.to_xdm_sequence,
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
                    to_xdm=self.to_xdm_sequence,
                )
            else:
                values[name] = self._variable_value(value)
        return values

    def _variable_value(self, value: Any) -> Any:
        """Bind one variable, keeping an already-adapted typed atomic.

        ``xsl:evaluate`` passes a value that was adapted by another evaluation
        in the same transformation.  Re-wrapping it would replace its dynamic
        type with the primitive payload class; the shared annotation record
        keeps the schema-defined type.
        """
        if isinstance(value, (list, tuple)):
            converted = [self._variable_value(item) for item in value]
            if not converted:
                return []
            if len(converted) == 1:
                return converted[0]
            return converted
        if not isinstance(value, XdmItem) and (
            id(value) in self._annotations or getattr(value, "_pyxsd_type", None) is not None
        ):
            return value
        return self.sequence_to_elementpath(as_xdm(value))

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
        annotation = getattr(value, "_pyxsd_type", None)
        record = self._annotations.get(id(value)) if annotation is None else None
        if annotation is not None or record is not None:
            converted = as_xdm(value)
            # An atomic adapted by another evaluation keeps its schema-defined
            # annotation beside the primitive XdmAtomic.
            if annotation is None:
                assert record is not None
                annotation = record[1]
            _record_annotation(self._annotations, converted[0], annotation)
            return converted
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
        arity = _function_item_arity(token)
        name = getattr(token, "name", None)
        if not isinstance(name, str) or not name:
            name = _token_function_name(token)
        # The token's parser bucket owns every callback the function's body can
        # reach, so a deferred invocation restores that bucket's dynamic slots.
        # A function item is commonly called after the evaluation that created
        # it has ended, and its body must still see that evaluation's bridge,
        # override targets, and declared-function implementations.
        holder = getattr(getattr(token, "parser", None), "_pyxsd_bridge_holder", None)
        state = (
            None
            if holder is None
            else (holder.bridge, holder.xpath_context, holder.overrides, holder.functions)
        )

        def implementation(arguments: Any, environment: Any, closure: Any) -> XdmSequence:
            del environment, closure
            converted = tuple(
                sequence_to_elementpath(argument, item_converter=self.adapt_item)
                for argument in arguments
            )
            clone = clone_function_token(token)
            return _with_holder_state(
                holder, state, lambda: self.to_xdm_sequence(clone(*converted, context=context))
            )

        return XdmFunction(implementation, arity=arity, name=name)


def _with_holder_state(
    holder: _BridgeHolder | None,
    state: tuple[Any, Any, Any, Any] | None,
    call: Callable[[], XdmSequence],
) -> XdmSequence:
    """Run *call* with a bucket's dynamic slots restored to *state*.

    A deferred function item belongs to the evaluation that created it, not to
    whichever evaluation happens to invoke it, so its body must see that
    evaluation's bridge, override targets, and declared-function
    implementations.  Restoring the previous values afterwards keeps a nested
    or enclosing evaluation's slots intact.
    """
    if holder is None or state is None:
        return call()
    previous = (holder.bridge, holder.xpath_context, holder.overrides, holder.functions)
    holder.bridge, holder.xpath_context, holder.overrides, holder.functions = state
    try:
        return call()
    finally:
        holder.bridge, holder.xpath_context, holder.overrides, holder.functions = previous


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
            key=self._xslt_functions,
        )
        root_token = parser.parse(source)
        variable_names = _expression_variable_names(root_token, static_context)
        free_variable_names = _expression_free_variable_names(root_token, static_context)

        plan: continuation.CompiledExpression | None = None
        if self._version == "3.1" and not static_context.compatibility_mode:
            # The optional seam lowers a subset of XPath 3.1 to a continuation
            # machine.  It does not model XPath 1.0 compatibility conversions,
            # so a compatibility-mode expression keeps the full evaluator.
            try:
                plan = continuation.compile_expression(
                    source,
                    variable_types=static_context.variable_types,
                    default_collation=static_context.default_collation,
                )
            except Exception as exc:
                from pyxsd.xslt.errors import is_budget_alarm

                if is_budget_alarm(exc):
                    raise
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
        """Evaluate one compiled expression with invocation-local dynamic state.

        When ``environment.parser_cache`` is supplied, the parser and its symbol
        table are reused across evaluations of the same static configuration;
        every dynamic-context component still comes from *environment*.
        """
        if not isinstance(compiled, CompiledExpression):
            raise TypeError("compiled must be a CompiledExpression")
        if not isinstance(environment, ExpressionEnvironment):
            raise TypeError("environment must be an ExpressionEnvironment")
        if compiled.version != self._version:
            raise ValueError("the compiled expression belongs to a different XPath version")

        # A bad handle is a programming error even when this evaluation would
        # not use it, so it is rejected before the empty-focus short circuit.
        cache = environment.parser_cache
        if cache is not None and not isinstance(cache, XPathParserCache):
            raise TypeError("parser_cache must be an XPathParserCache or None")
        registry = environment.adapter_registry
        if registry is not None and not isinstance(registry, _XdmAdapterRegistry):
            raise TypeError("adapter_registry must be an _XdmAdapterRegistry or None")

        resolved = _resolve_focus(focus)
        if resolved.is_empty:
            return ()

        if cache is None:
            parser = _build_parser(
                self._version,
                compiled.static_context,
                key=self._xslt_functions,
                base_uri=environment.base_uri,
                decimal_formats=environment.decimal_formats,
            )
            holder = parser._pyxsd_bridge_holder
            assert holder is not None
            _install_function_overrides(
                parser, holder, _override_symbols(environment.function_overrides, parser)
            )
        else:
            bucket = cache.bucket(
                self._version,
                compiled.static_context,
                environment,
                xslt_functions=self._xslt_functions,
            )
            parser, holder = bucket.parser, bucket.holder

        bridge = _ValueBridge(
            parser,
            compiled.static_context,
            environment,
            version=self._version,
            registry=registry,
        )
        context = _build_context(compiled.static_context, environment, resolved, bridge)
        bridge._context = context

        previous = (holder.bridge, holder.xpath_context, holder.overrides, holder.functions)
        holder.bridge = bridge
        holder.xpath_context = None
        holder.overrides = _overrides_by_symbol(environment.function_overrides, parser)
        holder.functions = environment.functions
        try:
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
        finally:
            holder.bridge, holder.xpath_context, holder.overrides, holder.functions = previous


# ---------------------------------------------------------------------------
# Parser and context construction


def _arity_key(arity: int | tuple[int, int | None]) -> object:
    """A hashable form of one declared-function arity for the parser key."""
    return arity if isinstance(arity, int) else tuple(arity)


def _decimal_formats_key(
    decimal_formats: Mapping[str | None, Mapping[str, str]],
) -> tuple[tuple[tuple[bool, str], tuple[tuple[str, str], ...]], ...]:
    """The statically known decimal formats, in a hashable canonical order.

    Each entry is tagged with whether it is the default format.  ``None`` and
    the lexical QName ``"None"`` are both valid, distinct keys of
    ``decimal_formats``; without the tag they would share one key component and
    the second configuration would reuse the first one's table.
    """
    return tuple(
        sorted(
            ((name is None, name if name is not None else ""), tuple(sorted(attributes.items())))
            for name, attributes in decimal_formats.items()
        )
    )


def _override_key(function_overrides: Mapping[str, Any]) -> frozenset[str]:
    """The bucket identity of an override table.

    Two lexical spellings of one function address one parser symbol, so they
    must share a bucket; the local name is that identity.  Installation still
    resolves the exact symbol against the built parser.
    """
    return frozenset(
        name.rpartition("}")[2] if name.startswith("{") else name.rpartition(":")[2]
        for name in function_overrides
    )


def _override_symbol(parser: Any, name: str) -> str:
    """The parser symbol that an override of the function *name* replaces.

    elementpath registers a declared function that collides with a built-in in
    another namespace under its expanded name, moves the built-in to its own
    expanded key, and leaves a :class:`ProxyToken` at the bare local symbol.
    An override has to follow the same table: prefer the expanded key that
    holds the real function token, and only fall back to the local symbol.
    Resolving to the proxy would read ``nargs`` (and every other signature
    attribute) off a token class that has none.
    """
    local = name.rpartition("}")[2] if name.startswith("{") else name.rpartition(":")[2]
    candidates: list[str] = []
    if name.startswith("{"):
        candidates.append(name)
    else:
        prefix, separator, _ = name.partition(":")
        uri = parser.namespaces.get(prefix) if separator else None
        if uri is not None:
            candidates.append(f"{{{uri}}}{local}")
    candidates.append(local)
    for candidate in candidates:
        base = parser.symbol_table.get(candidate)
        if base is not None and not issubclass(base, ProxyToken):
            return candidate
    return candidates[0]


def _override_symbols(function_overrides: Mapping[str, Any], parser: Any) -> list[str]:
    """The parser symbols an override table replaces, in table order."""
    return [_override_symbol(parser, name) for name in function_overrides]


def _overrides_by_symbol(function_overrides: Mapping[str, Any], parser: Any) -> dict[str, Any]:
    """The override table keyed by the parser symbol it replaces.

    Later duplicates win, exactly as the registration loop did.
    """
    return dict(
        zip(
            _override_symbols(function_overrides, parser),
            function_overrides.values(),
            strict=True,
        )
    )


def _parser_key(
    version: str,
    xslt_functions: bool,
    static_context: XPathStaticContext,
    environment: ExpressionEnvironment,
) -> tuple[object, ...]:
    """The complete static configuration one built parser bakes in.

    Every component the parser copies at construction is here, so a change to
    any one of them builds another bucket instead of silently answering with
    another configuration's namespaces, arities, schema, or decimal formats.
    """
    return (
        version,
        xslt_functions,
        tuple(sorted(static_context.namespaces.items())),
        static_context.default_namespace,
        tuple(sorted(static_context.variable_types.items())),
        tuple(sorted((name, _arity_key(a)) for name, a in static_context.functions.items())),
        id(static_context.schema),
        static_context.base_uri,
        environment.base_uri,
        static_context.default_collation,
        static_context.xsd_version,
        static_context.xslt_version,
        static_context.compatibility_mode,
        _decimal_formats_key(environment.decimal_formats),
        _override_key(environment.function_overrides),
    )


class XPathParserCache:
    """One transformation's built elementpath parsers.

    Buckets are keyed by the complete static configuration a parser bakes in.
    Every dynamic-context component is supplied per evaluation through the
    bucket's ``_BridgeHolder``.  One cache belongs to one transformation
    invocation: it is never process-global, never shared between
    transformations, and never shared between threads.
    """

    __slots__ = ("_buckets", "_built", "_max_buckets", "_reused")

    def __init__(self, *, max_buckets: int = 256) -> None:
        if isinstance(max_buckets, bool) or not isinstance(max_buckets, int):
            raise TypeError("max_buckets must be an integer")
        if max_buckets < 1:
            raise ValueError("max_buckets must be at least 1")
        self._buckets: OrderedDict[tuple[object, ...], _ParserBucket] = OrderedDict()
        self._max_buckets = max_buckets
        self._built = 0
        self._reused = 0

    def bucket(
        self,
        version: str,
        static_context: XPathStaticContext,
        environment: ExpressionEnvironment,
        *,
        xslt_functions: bool,
    ) -> _ParserBucket:
        """The parser bucket for this static configuration, built on first use."""
        key = _parser_key(version, xslt_functions, static_context, environment)
        cached = self._buckets.get(key)
        if cached is not None and cached.static_context.schema is static_context.schema:
            self._buckets.move_to_end(key)
            self._reused += 1
            return cached

        parser = _build_parser(
            version,
            static_context,
            key=xslt_functions,
            base_uri=environment.base_uri,
            decimal_formats=environment.decimal_formats,
        )
        holder = parser._pyxsd_bridge_holder
        assert holder is not None
        _install_function_overrides(
            parser, holder, _override_symbols(environment.function_overrides, parser)
        )
        bucket = _ParserBucket(parser, holder, static_context, key)
        self._buckets[key] = bucket
        self._buckets.move_to_end(key)
        self._built += 1
        while len(self._buckets) > self._max_buckets:
            self._buckets.popitem(last=False)
        return bucket

    def clear(self) -> None:
        """Drop every bucket.  Never needed for correctness; used by tests."""
        self._buckets.clear()

    @property
    def buckets_built(self) -> int:
        """How many parsers this cache has constructed."""
        return self._built

    @property
    def buckets_reused(self) -> int:
        """How many lookups were answered by an existing bucket."""
        return self._reused

    @property
    def bucket_count(self) -> int:
        """How many buckets are currently held."""
        return len(self._buckets)


class _ParserBucket:
    """One built parser plus the per-evaluation slots its callbacks read."""

    __slots__ = ("holder", "key", "parser", "static_context")

    def __init__(
        self,
        parser: Any,
        holder: _BridgeHolder,
        static_context: XPathStaticContext,
        key: tuple[object, ...],
    ) -> None:
        self.parser = parser
        self.holder = holder
        self.static_context = static_context
        self.key = key


def _build_parser(
    version: str,
    static_context: XPathStaticContext,
    *,
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
            "variable_types": {},
            "base_uri": base_uri if base_uri is not None else static_context.base_uri,
            "default_collation": static_context.default_collation,
            "xsd_version": static_context.xsd_version,
            "compatibility_mode": static_context.compatibility_mode,
        }
        if decimal_formats and version != "2.0":
            options["decimal_formats"] = {
                name: dict(attributes) for name, attributes in decimal_formats.items()
            }
        parser = parser_class(**options)
    parser._pyxsd_bridge_holder = holder
    _install_syntax_only_parse(parser)
    if version != "1.0":
        _install_node_kind_test(parser)
        _install_descendant_path_order(parser)
        _install_lookup_path_step(parser)
    _install_focus_token(parser)
    _install_focus_functions(parser)
    _install_function_scope(parser)
    if version == "1.0":
        _install_legacy_1_0_overrides(parser)
        install_numeric_functions(parser, include_round=False)
    else:
        _install_constructor_partial_application(parser)
        _install_comparison_string_atomization(parser)
        _install_lang_function(parser)
        install_string_function(parser)
        install_string_conversion_functions(parser)
        install_deep_equal(parser)
        install_date_time(parser)
        install_numeric_functions(parser)
        _install_instance_expression(parser)
        _install_temporal_components(parser)
        _install_temporal_arithmetic(parser)
        install_regex_replace(parser)
        if version == "2.0":
            _install_xpath20_regex_surface(parser)
        _register_schema_constructors(parser, static_context)
        _install_schema_kind_tests(parser)
        if static_context.compatibility_mode:
            _install_compatibility_comparisons(parser)
        # A declared type may be a user-defined schema type the parser can only
        # resolve once its pool proxy is bound; pyxsd validated the declaration
        # already (XPST0051/XPST0008), so assign the full map here instead of
        # letting the constructor reject it.
        parser.variable_types = dict(static_context.variable_types)

    for name in static_context.functions:
        _register_declared_function(parser, name, static_context, holder)
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
        _bind_kind_test_type_names(root_token, self.default_namespace)
        return root_token

    parser.parse = MethodType(parse_syntax_only, parser)


def _bind_kind_test_type_names(root: Any, default_namespace: str | None) -> None:
    """Bind a bare type QName in an element/attribute kind test.

    elementpath leaves ``(name)`` tokens in the type slot of ``element(N, T)``
    and ``attribute(N, T)`` unexpanded, so ``element(*, docType)`` carries an
    empty type name.  XPath 3.1 expands that QName with the default
    element/type namespace of the static context.
    """
    stack = [root]
    while stack:
        token = stack.pop()
        items: Any = getattr(token, "_items", ())
        if getattr(token, "symbol", None) in ("element", "attribute") and len(items) >= 2:
            type_token = items[1]
            if type_token.symbol == "(name)" and not type_token.name:
                value = type_token.value
                type_token.name = f"{{{default_namespace}}}{value}" if default_namespace else value
        stack.extend(items)


def _install_descendant_path_order(parser: Any) -> None:
    """Return ``//`` results in document order.

    ``A//B`` means ``A/descendant-or-self::node()/B``.  Applying ``B`` to an
    ancestor yields that ancestor's matching children before the descendants'
    matching children, so a naive traversal of ``//text()`` grouped the
    element's own text children ahead of descendant text.  XPath requires the
    node sequence of a path expression in document order; sorting by the
    adapter's document position restores it.
    """
    base = parser.symbol_table.get("//")
    if base is None:  # pragma: no cover - every supported parser declares it
        return

    def select(self: Any, context: Any = None) -> Iterator[Any]:
        yield from sorted(base.select(self, context), key=lambda item: item.position)

    parser._register(
        "//",
        "_PyxsdDescendantPath",
        bases=(base,),
        label=base.label,
        bp=getattr(base, "bp", 75),
        select=select,
    )


def _install_lookup_path_step(parser: Any) -> None:
    """Let a lookup expression be a path step (XPath 3.1 section 3.3.1.2).

    ``E?*`` is a ``PostfixExpr``, and the grammar makes every ``PostfixExpr``
    a ``StepExpr``, so ``array?* / child`` is legal.  elementpath's ``/`` only
    admits a fixed symbol set as its left step, so a lookup token is rejected
    with ``XPST0003``; the lookup symbol joins that set on this parser
    instance, leaving the shared parser class untouched.
    """
    path_steps = getattr(parser, "PATH_STEP_SYMBOLS", None)
    if path_steps is None or "?" in path_steps:  # pragma: no cover - XPath 2.0+ only
        return
    parser.PATH_STEP_SYMBOLS = path_steps | {"?"}


def _install_node_kind_test(parser: Any) -> None:
    """Let ``node()`` match a document node reached through a variable.

    The substrate yields a document node only when it is the context root, so
    ``parent::node()`` and ``instance of node()`` failed for a document loaded
    by ``document()`` or held in a variable.  XPath 2.0 and later match every
    node, so the test yields the document node too; XPath 1.0 keeps the
    substrate's dummy-document masking.
    """
    base = parser.symbol_table.get("node")
    if base is None:  # pragma: no cover - every supported parser declares it
        return

    def select(self: Any, context: Any = None) -> Iterator[Any]:
        if context is None:  # pragma: no cover - token evaluation supplies it
            raise self.missing_context()
        for item in context.iter_children_or_self():
            if isinstance(item, XPathNode):  # pragma: no branch - iter yields nodes
                yield item

    parser._register(
        "node",
        "_PyxsdNodeKindTest",
        bases=(base,),
        label=base.label,
        nargs=getattr(base, "nargs", 0),
        select=select,
    )


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


def _install_constructor_partial_application(parser: Any) -> None:
    """Treat only a bare ``?`` as a constructor's partial application.

    elementpath's ``XPathConstructor.nud`` marks the constructor as a partial
    function whenever the argument's root token *is* ``?``.  XPath's placeholder
    is the bare lookup token, but a lookup expression such as ``$map?value``
    also has the ``?`` symbol, so ``xs:integer($map?value)`` silently evaluates
    to a function item instead of casting.  A lookup expression carries an
    operand and is a normal argument.  The corrected token class is registered
    on this parser instance only; elementpath's own class table is untouched.
    """

    def is_placeholder(token: Any) -> bool:
        return token.symbol == "?" and not len(token)

    def nud(self: Any) -> Any:
        if not self.parser.parse_arguments:
            return self
        try:
            self.parser.advance("(")
            self[0:] = (self.parser.expression(5),)
            if self.parser.next_token.symbol == ",":
                raise self.error("XPST0017", "Too many arguments: expected at most 1 argument")
            self.parser.advance(")")
        except SyntaxError:
            raise self.error("XPST0017") from None
        else:
            if is_placeholder(self[0]):
                self.to_partial_function()
            return self

    def evaluate(self: Any, context: Any = None) -> Any:
        arg = self.data_value(self.get_argument(context))
        if arg is None:
            return []
        elif arg == "?" and is_placeholder(self[0]):
            raise self.error("XPTY0004", "cannot evaluate a partial function")
        try:
            return self.cast(arg.value if isinstance(arg, UntypedAtomic) else arg)
        except ElementPathError as err:
            err.token = self
            raise
        except (TypeError, ValueError) as err:
            raise self.error("FORG0001", err) from None

    for symbol, base in list(parser.symbol_table.items()):
        if not isinstance(base, type) or not issubclass(base, XPathConstructor):
            continue
        # Dual-use keywords (boolean, string, QName, dateTime) and the error
        # constructor provide their own parsing and evaluation; only the plain
        # one-argument constructors use elementpath's generic implementation.
        if getattr(base, "nud", None) is not XPathConstructor.nud:
            continue
        if getattr(base, "evaluate", None) is not XPathConstructor.evaluate:
            continue
        parser._register(
            symbol,
            f"_PyxsdConstructor_{symbol.replace('-', '_').replace(':', '_')}",
            bases=(base,),
            label=base.label,
            nargs=getattr(base, "nargs", None),
            sequence_types=getattr(base, "sequence_types", ()),
            nud=nud,
            evaluate=evaluate,
        )


def _install_comparison_string_atomization(parser: Any) -> None:
    """Compare two untyped atomic operands as strings in general comparisons.

    Functions and Operators 3.1 section 3.7.2 casts both untyped atomic
    operands of a general comparison to ``xs:string``.  elementpath hands the
    ``UntypedAtomic`` values to Python's comparison operators, which compare
    them numerically, so two untyped nodes can disagree with a string
    comparison.  Value comparisons are already correct and keep their own
    tokens.  The corrected comparison tokens live on this parser instance only.
    """

    def iter_comparison_data(self: Any, context: Any) -> Iterator[Any]:
        for left, right in XPathToken.iter_comparison_data(self, context):
            yield continuation.comparison_operands(left, right)

    for symbol in ("=", "!=", "<", ">", "<=", ">="):
        base = parser.symbol_table[symbol]
        parser._register(
            symbol,
            f"_PyxsdComparison_{symbol.replace('=', 'eq').replace('!', 'ne')}".replace(
                "<", "lt"
            ).replace(">", "gt"),
            bases=(base,),
            label=base.label,
            nargs=getattr(base, "nargs", (2, 2)),
            sequence_types=getattr(base, "sequence_types", ()),
            iter_comparison_data=iter_comparison_data,
        )


_XPATH20_REGEX_FLAGS = frozenset("imsx")


def _install_xpath20_regex_surface(parser: Any) -> None:
    """Restore the XPath 2.0 regular-expression flag vocabulary.

    elementpath's XPath 2.0 parser gates the XPath 3.0 ``q`` flag on
    ``parser.version > '2'``; the lexical comparison is true for ``"2.0"``, so
    a native 2.0 profile accepts a flag that XPath 2.0 does not define.  The
    arity of ``tokenize`` is left as elementpath declares it: the W3C XSLT 2.0
    catalog uses its third argument for the 2.0 flag vocabulary.  The corrected
    tokens live on this parser instance only.
    """

    def validate_flags(token: Any, context: Any, index: int) -> None:
        if len(token) <= index:
            return
        flags = token.get_argument(context, index, required=True, cls=str)
        for flag in flags:
            if flag not in _XPATH20_REGEX_FLAGS:
                raise token.error("FORX0001", f"Invalid regular expression flag {flag!r}")

    def flags_checked(base: Any, index: int) -> Callable[..., Any]:
        def evaluate(self: Any, context: Any = None) -> Any:
            validate_flags(self, context, index)
            return base.evaluate(self, context)

        return evaluate

    for symbol, index in (("matches", 2), ("replace", 3)):
        base = parser.symbol_table[symbol]
        parser._register(
            symbol,
            f"_PyxsdXPath20{symbol.title()}",
            bases=(base,),
            label=base.label,
            nargs=getattr(base, "nargs", None),
            sequence_types=getattr(base, "sequence_types", ()),
            evaluate=flags_checked(base, index),
        )
    tokenize = parser.symbol_table["tokenize"]
    parser._register(
        "tokenize",
        "_PyxsdXPath20Tokenize",
        bases=(tokenize,),
        label=tokenize.label,
        nargs=getattr(tokenize, "nargs", None),
        sequence_types=getattr(tokenize, "sequence_types", ()),
        evaluate=flags_checked(tokenize, 2),
    )


#: Sequence-type kind tests decided by the annotation-aware matcher.
_ANNOTATION_KIND_TESTS = (
    "schema-attribute(",
    "schema-element(",
    "namespace-node(",
)


def _install_instance_expression(parser: Any) -> None:
    """Match ``instance of`` item by item against the declared sequence type.

    The substrate evaluates the sequence-type token once with the *outer*
    context, so a node kind test examines whatever the context item is rather
    than each item of the left operand.  ``document-node(element(a))?`` then
    answered true for a two-document sequence.  The sequence type's own source
    text is matched against the whole value, which applies the occurrence
    indicator and each item's kind test correctly.
    """
    from elementpath.sequence_types import match_sequence_type

    base = parser.symbol_table.get("instance")
    if base is None:  # pragma: no cover - every supported parser declares it
        return

    def evaluate(self: Any, context: Any = None) -> Any:
        if getattr(self[1], "label", None) == "kind test":
            sequence_type = _kind_test_source(self[1])
        else:
            sequence_type = self[1].source
        values = list(self[0].select(context))
        annotated = _match_annotated_atomic_type(values, sequence_type, self.parser)
        if annotated is _DECLINED:
            return base.evaluate(self, context)
        if annotated is not None:
            return annotated
        if (
            "schema-attribute(" in sequence_type
            or "schema-element(" in sequence_type
            or sequence_type.startswith(_ANNOTATION_KIND_TESTS)
            or _needs_annotation_match(sequence_type)
        ):
            return bool(_match_tolerant_sequence_type(values, sequence_type, self.parser))
        try:
            return match_sequence_type(values, sequence_type, self.parser)
        except (ElementPathError, KeyError, ValueError):  # pragma: no cover - substrate-only
            return base.evaluate(self, context)

    parser._register(
        "instance",
        "_PyxsdInstanceExpression",
        bases=(base,),
        label=base.label,
        bp=getattr(base, "bp", 60),
        evaluate=evaluate,
    )


class _Declined:
    """The atomic type target is known but the value has no annotation."""

    __slots__ = ()


#: Sentinel: the atomic target is known but the value's annotation is not.
_DECLINED = _Declined()


def _match_annotated_atomic_type(
    values: Sequence[Any],
    sequence_type: str,
    parser: Any,
) -> bool | _Declined | None:
    """Match a plain atomic type test through the item's type annotation.

    The substrate's ``is_instance`` decides a built-in atomic type from the
    Python payload class, so a schema-defined type value (whose payload is the
    primitive root) answers ``xs:int`` false.  The schema proxy knows the
    recorded annotation and the derivation rules.  Returns ``None`` when the
    sequence type is not a plain atomic type name.
    """
    from elementpath.namespaces import get_expanded_name

    schema = getattr(parser, "schema", None)
    if schema is None:
        return None
    occurrence = ""
    if sequence_type[-1:] in ("*", "+", "?"):
        occurrence = sequence_type[-1]
        sequence_type = sequence_type[:-1]
    try:
        expanded = get_expanded_name(sequence_type, parser.namespaces)
    except (KeyError, ValueError):
        return None
    adapter = schema.get_type(expanded)
    if adapter is None or not adapter.is_atomic():
        return None
    if values and any(schema._annotation(value) is None for value in values):
        # The value did not come through the bridge (for example a cast inside
        # one expression), so its dynamic type is unknown here.  The base
        # sequence-type token is the pre-existing, correct answer.
        return _DECLINED
    matched = all(bool(schema.is_instance(value, expanded)) for value in values)
    if not matched:
        return False
    if occurrence == "":
        return len(values) == 1
    if occurrence == "?":
        return len(values) <= 1
    if occurrence == "+":
        return bool(values)
    return True


def _needs_annotation_match(sequence_type: str) -> bool:
    """Whether a kind test carries a type name to match by annotation.

    An ``element(N, T)``/``attribute(N, T)`` test is a type-annotation test
    (XPath 3.1 sections 2.5.5.3 and 2.5.5.5), including for built-in types such
    as ``xs:anyType`` and list types; a value-space test on the atomized value
    mis-answers them.  A name-only test keeps the substrate's matcher, which
    also covers nodes from a host function's own elementpath tree.
    """
    if sequence_type.startswith("document-node("):
        return _needs_annotation_match(sequence_type[len("document-node(") : -1])
    if not sequence_type.startswith(("element(", "attribute(")):
        return False
    inner = sequence_type[sequence_type.find("(") + 1 : -1]
    return "," in inner


def _kind_test_source(token: Any) -> str:
    """Reconstruct a kind test's sequence type from its token tree.

    The substrate's ``source`` property writes ``attribute *`` for
    ``attribute(*)`` and ``my:filetype attribute xs:NOTATION`` for a
    two-argument attribute test, which is not valid sequence-type syntax.  The
    children already carry the correct name and type sources.
    """
    symbol = getattr(token, "symbol", "")
    arguments = ", ".join(
        source
        for child in token
        for source in (getattr(child, "source", ""),)
        if isinstance(source, str) and source
    )
    occurrence = getattr(token, "occurrence", "") or ""
    return f"{symbol}({arguments}){occurrence}"


def _match_tolerant_sequence_type(
    values: Sequence[Any],
    sequence_type: str,
    parser: Any,
) -> bool | None:
    """Match node-kind sequence types against each item's type annotation.

    ``match_sequence_type`` decides ``element(N, T)`` and ``attribute(N, T)``
    through ``is_instance`` on the atomized value, which mis-answers annotation
    tests (``xs:anyType``, list and notation types, user-defined types) and has
    no ``schema-element``/``schema-attribute`` kind tests.  This matcher applies
    the same predicates the parser's kind tests use, to every item of the
    sequence, and then the occurrence indicator.  It returns ``None`` for a
    sequence type outside its scope.
    """
    from elementpath.namespaces import get_expanded_name

    from pyxsd.xdm import type_tests
    from pyxsd.xdm.nodes import NodeKind, XdmNode

    occurrence = ""
    if sequence_type[-1:] in ("*", "+", "?"):
        occurrence = sequence_type[-1]
        sequence_type = sequence_type[:-1]

    schema = getattr(parser, "schema", None)

    def node_of(item: Any) -> XdmNode | None:
        node = getattr(item, "xdm", None)
        if isinstance(node, XdmNode):
            return node
        return item if isinstance(item, XdmNode) else None

    def expanded(name: str) -> str:
        return get_expanded_name(name, parser.namespaces)

    def element_test(inner: str, item: Any) -> bool:
        node = node_of(item)
        if node is None or node.kind is not NodeKind.ELEMENT:
            return False
        name, _, type_text = inner.partition(",")
        name = name.strip()
        type_name = type_text.strip() or None
        nillable = type_name is not None and type_name.endswith("?")
        if type_name is not None:
            type_name = type_name.rstrip("?")
        expanded_name = None if name in ("", "*") else expanded(name)
        expanded_type = None if type_name is None else expanded(type_name)
        if schema is None:
            return type_tests.matches_element_test(
                node, expanded_name, expanded_type, nillable, None
            )
        return bool(schema.matches_element_test(node, expanded_name, expanded_type, nillable))

    def attribute_test(inner: str, item: Any) -> bool:
        node = node_of(item)
        if node is None or node.kind is not NodeKind.ATTRIBUTE:
            return False
        name, _, type_text = inner.partition(",")
        name = name.strip()
        type_name = type_text.strip() or None
        expanded_name = None if name in ("", "*") else expanded(name)
        expanded_type = None if type_name is None else expanded(type_name)
        if schema is None:
            return type_tests.matches_attribute_test(node, expanded_name, expanded_type, None)
        return bool(schema.matches_attribute_test(node, expanded_name, expanded_type))

    def declaration_test(kind: str, inner: str, item: Any) -> bool:
        node = node_of(item)
        if node is None:
            return False
        name = expanded(inner.strip())
        assert schema is not None
        if kind == "schema-attribute":
            return bool(schema.matches_schema_attribute(node, name))
        return bool(schema.matches_schema_element(node, name))

    def document_test(inner: str, item: Any) -> bool:
        node = node_of(item)
        if node is None or node.kind is not NodeKind.DOCUMENT:
            return False
        if not inner:
            return True
        if inner.startswith("schema-element("):
            return any(
                declaration_test("schema-element", inner[len("schema-element(") : -1], child)
                for child in node.children
            )
        element_inner = inner[len("element(") : -1]
        return any(element_test(element_inner, child) for child in node.children)

    if sequence_type.startswith("document-node("):
        kind, inner = "document-node", sequence_type[len("document-node(") : -1]
    elif sequence_type.startswith("element("):
        kind, inner = "element", sequence_type[len("element(") : -1]
    elif sequence_type.startswith("attribute("):
        kind, inner = "attribute", sequence_type[len("attribute(") : -1]
    elif sequence_type.startswith(("schema-attribute(", "schema-element(")):
        kind, _, inner = sequence_type.partition("(")
        inner = inner[:-1]
    elif sequence_type.startswith("namespace-node("):
        kind, inner = "namespace-node", ""
    else:
        return None

    if kind == "document-node":
        matched = all(document_test(inner, item) for item in values)
    elif kind == "element":
        matched = all(element_test(inner, item) for item in values)
    elif kind == "attribute":
        matched = all(attribute_test(inner, item) for item in values)
    elif kind == "namespace-node":
        matched = all(
            (node := node_of(item)) is not None and node.kind is NodeKind.NAMESPACE
            for item in values
        )
    else:
        matched = all(declaration_test(kind, inner, item) for item in values)
    if not matched:
        return False
    if occurrence == "":
        return len(values) == 1
    if occurrence == "?":
        return len(values) <= 1
    if occurrence == "+":
        return bool(values)
    return True


def _install_temporal_components(parser: Any) -> None:
    """Return the exact fractional second of a dateTime or time.

    The substrate builds ``Decimal('{}.{}'.format(second, microsecond))``, so a
    fractional second of ``30.012`` becomes ``30.12000`` and canonicalises to
    ``30.12``.  The F&O value is the second plus the six-digit fraction, which
    is what the W3C component-extraction cases require.
    """
    from elementpath.datatypes import DateTime, Time

    for symbol, expected in (
        ("seconds-from-dateTime", DateTime),
        ("seconds-from-time", Time),
    ):
        base = parser.symbol_table.get(symbol)
        if base is None:  # pragma: no cover - every supported parser declares them
            continue

        def evaluate(self: Any, context: Any = None, _expected: Any = expected) -> Any:
            item = self.get_argument(context, cls=_expected)
            if item is None:
                return []
            if item.microsecond:
                fraction = f"{item.microsecond:06d}".rstrip("0")
                return Decimal(f"{item.second}.{fraction}")
            return item.second

        parser._register(
            symbol,
            f"_Pyxsd{symbol.title().replace('-', '')}",
            bases=(base,),
            label=base.label,
            nargs=getattr(base, "nargs", 1),
            sequence_types=getattr(base, "sequence_types", ()),
            evaluate=evaluate,
        )


def _install_temporal_arithmetic(parser: Any) -> None:
    """Add and subtract date/time values without mutating the operands.

    The substrate's binary operator applies the implicit timezone by assigning
    ``tzinfo`` on the operand objects.  A variable's bridged value is one such
    object, so a later reading of the variable observed the added timezone.
    Its ``fromtimedelta`` also concatenates a negative duration's microseconds
    onto the already-rounded seconds (``-44.7s`` became ``-45.3s``), and a
    ``dayTimeDuration + dateTime`` raises instead of adding.

    This override evaluates the operands once, applies the implicit timezone to
    copies for a date/time difference, keeps the duration/date operands'
    timezone rules, and maps an out-of-range result to FODT0001/FODT0002.
    """
    import operator

    for symbol, operation in (("+", operator.add), ("-", operator.sub)):
        base = parser.symbol_table[symbol]

        def evaluate(
            self: Any,
            context: Any = None,
            _operation: Any = operation,
            _base: Any = base,
        ) -> Any:
            del _operation
            if len(self) != 2:
                return _base.evaluate(self, context)
            left = _arithmetic_operand(self, context, 0)
            right = _arithmetic_operand(self, context, 1)
            if left is None or right is None:
                return []
            if isinstance(left, AbstractDateTime) and isinstance(right, AbstractDateTime):
                if self.symbol == "+":
                    raise self.error("XPTY0004", "cannot add two date/time values")
                return _temporal_difference(self, left, right, context)
            if (
                self.symbol == "+"
                and isinstance(left, Duration)
                and isinstance(right, AbstractDateTime)
            ):
                # Addition is symmetric; the substrate's DayTimeDuration only
                # accepts a date/time as its right-hand operand.
                left, right = right, left
            # Numeric promotion from the substrate's operand normalization: a
            # float paired with a Decimal or a Duration is converted to that
            # operand's type before the Python operation.
            if isinstance(left, float) and isinstance(right, Duration):
                left = Decimal(left)
            if isinstance(right, float) and isinstance(left, Duration):
                right = Decimal(right)
            if isinstance(left, float) and isinstance(right, Decimal):
                right = type(left)(right)
            elif isinstance(right, float) and isinstance(left, Decimal):
                left = type(right)(left)
            try:
                if self.symbol == "+":
                    return left + right
                return left - right
            except TypeError as err:
                raise self.error("XPTY0004", err) from None
            except (ValueError, OverflowError) as err:
                if isinstance(left, AbstractDateTime) or isinstance(right, AbstractDateTime):
                    raise self.error("FODT0001", err) from None
                if isinstance(left, Duration) or isinstance(right, Duration):
                    raise self.error("FODT0002", err) from None
                raise self.error("FOAR0002", err) from None  # pragma: no cover - Decimal overflow

        parser._register(
            symbol,
            f"_Pyxsd{'Plus' if symbol == '+' else 'Minus'}Operator",
            bases=(base,),
            label=base.label,
            bp=getattr(base, "bp", 40),
            evaluate=evaluate,
        )


def _arithmetic_operand(token: Any, context: Any, index: int) -> Any:
    """One arithmetic operand, with the substrate's untypedAtomic conversion."""
    value = token.get_argument(context, index=index, cls=ArithmeticProxy)
    if isinstance(value, ElementNode):  # pragma: no cover - operands are atomized
        value = token._items[index].data_value(value)
    if isinstance(value, UntypedAtomic):
        value = token.cast_to_double(value.value)
    return value


def _temporal_difference(
    token: Any,
    left: AbstractDateTime,
    right: AbstractDateTime,
    context: Any,
) -> DayTimeDuration:
    """The exact duration between two date/time values.

    The operands are copied before the implicit timezone is applied, so a
    variable's stored value keeps its own timezone.  The microseconds are
    summed with the whole seconds rather than appended, which keeps a negative
    fractional difference exact.
    """
    from copy import copy

    first = copy(left)
    second = copy(right)
    timezone = getattr(context, "timezone", None) if context is not None else None
    if timezone is not None:
        if first.tzinfo is None:
            first.tzinfo = timezone
        if second.tzinfo is None:
            second.tzinfo = timezone
    try:
        if 1 <= first.year <= 9999 and 1 <= second.year <= 9999:
            start, end = get_comparable_datetimes(first._dt, second._dt)
            delta = start - end
            seconds = Decimal(delta.days * 86400 + delta.seconds) + (
                Decimal(delta.microseconds) / Decimal(1_000_000)
            )
        else:
            # Extended years have no ``datetime`` equivalent; subtract the
            # Gregorian day offsets directly, as the in-range branch does.
            delta = first.todelta() - second.todelta()
            seconds = Decimal(delta.days * 86400 + delta.seconds) + (
                Decimal(delta.microseconds) / Decimal(1_000_000)
            )
    except (ValueError, OverflowError) as err:  # pragma: no cover - constructor rejects it
        raise token.error("FODT0001", err) from None
    return DayTimeDuration(seconds=seconds)


def _install_lang_function(parser: Any) -> None:
    """Read ``xml:lang`` through grounded adapters, not just etree nodes.

    elementpath implements ``fn:lang`` only for its own ``EtreeElementNode``
    and returns false for any other element node, so every bridged attribute
    and element ignored the language in scope.  The language is inherited from
    the nearest ancestor element that declares it.  The corrected token lives
    on this parser instance only.
    """
    base = parser.symbol_table["lang"]

    def language_in_scope(item: Any) -> str | None:
        node = item
        while node is not None:
            if isinstance(node, ElementNode):
                for attribute in node.attributes:
                    name = getattr(attribute, "node_name", None)
                    if name is not None and name.namespace == XML_NS and name.local_name == "lang":
                        return attribute.string_value
            node = getattr(node, "parent", None)
        return None

    def evaluate(self: Any, context: Any = None) -> Any:
        if len(self) > 1:
            item = self.get_argument(context, index=1, default_to_context=True)
        else:
            item = None if context is None else context.item
        if not isinstance(item, ElementNode):
            raise self.error("XPTY0004")
        language = language_in_scope(item)
        if language is None:
            return False
        test_language = (self.get_argument(context, cls=str) or "").strip().lower()
        language = language.strip().lower()
        return language == test_language or (
            language.startswith(test_language)
            and len(language) > len(test_language)
            and language[len(test_language)] == "-"
        )

    parser._register(
        "lang",
        "_PyxsdLangFunction",
        bases=(base,),
        label=base.label,
        nargs=getattr(base, "nargs", (1, 2)),
        sequence_types=getattr(base, "sequence_types", ()),
        evaluate=evaluate,
    )


def _register_schema_constructors(parser: Any, static_context: XPathStaticContext) -> None:
    pool = static_context.schema
    if pool is None:
        return
    parser.schema = _PoolSchemaProxy(pool, parser.namespaces, parser)
    from elementpath.namespaces import get_prefixed_name

    for adapter in pool.types.values():
        if adapter.is_builtin or not adapter.is_atomic() or adapter.name is None:
            continue
        symbol = get_prefixed_name(adapter.name, parser.namespaces)
        if ":" not in symbol or symbol.startswith("{"):
            continue
        if symbol not in parser.symbol_table:
            parser.schema_constructor(adapter.name)


def _install_schema_kind_tests(parser: Any) -> None:
    """Replace elementpath's kind tests with the XPath 3.1 annotation rules.

    ``element(*, T)`` and ``attribute(*, T)`` are *type annotation* tests
    (XPath 3.1 sections 2.5.5.3 and 2.5.5.5), not value-space tests, and
    ``schema-element``/``schema-attribute`` are filters over the candidates of
    their axis (sections 2.5.5.4 and 2.5.5.6): an empty match is the empty
    sequence, never an error.  A declaration that is absent from the in-scope
    schema components is a *static* error, raised while the token is parsed,
    which is what ``XPathEngine.compile`` runs for every stylesheet expression.

    The test is installed for every schema-aware evaluation, including a
    parser with no bound pool: every node is then untyped and the untyped rule
    fails closed for a named type.  XPath 1.0 has no kind tests.
    """
    element_base = parser.symbol_table.get("element")
    attribute_base = parser.symbol_table.get("attribute")
    schema_element_base = parser.symbol_table.get("schema-element")
    schema_attribute_base = parser.symbol_table.get("schema-attribute")
    if None in (element_base, attribute_base, schema_element_base, schema_attribute_base):
        return  # pragma: no cover - every XPath 2.0+ parser has these tokens

    def element_name(token: Any, namespaces: MutableMapping[str, str]) -> str | None:
        return None if token.symbol == "*" else get_expanded_name(token.source, namespaces)

    def attribute_name(token: Any, namespaces: MutableMapping[str, str]) -> str | None:
        if token.symbol == "*":
            return None
        source = token.source
        if ":" not in source and not source.startswith(("Q{", "{")):
            # An unprefixed attribute name is in no namespace; the default
            # element/type namespace never applies (section 2.5.5.5).
            return source
        return get_expanded_name(source, namespaces)

    def select_element(self: Any, context: Any = None) -> Iterator[Any]:
        if context is None:  # pragma: no cover - token evaluation supplies it
            raise self.missing_context()
        schema = self.parser.schema
        if not self:
            for item in context.iter_children_or_self():
                if isinstance(item, ElementNode):
                    yield item
            return
        name = element_name(self[0], self.parser.namespaces)
        type_name = self[1].name if len(self) == 2 else None
        nillable = len(self) == 2 and self[1].occurrence in ("*", "?")
        for item in context.iter_children_or_self():
            node = getattr(item, "xdm", None)
            if node is None:  # pragma: no cover - every bridged node has its XDM node
                continue
            matches = (
                type_tests.matches_element_test(node, name, type_name, nillable, None)
                if schema is None
                else schema.matches_element_test(node, name, type_name, nillable)
            )
            if matches:
                yield item

    def select_attribute(self: Any, context: Any = None) -> Iterator[Any]:
        if self.label == "axis":
            yield from attribute_base.select(self, context)
            return
        if context is None:  # pragma: no cover - token evaluation supplies it
            raise self.missing_context()
        schema = self.parser.schema
        name = attribute_name(self[0], self.parser.namespaces) if self else None
        type_name = self[1].name if len(self) == 2 else None
        candidates = (
            context.iter_attributes() if context.axis is None else context.iter_children_or_self()
        )
        for item in candidates:
            node = getattr(item, "xdm", None)
            if node is None:  # pragma: no cover - every bridged node has its XDM node
                continue
            matches = (
                type_tests.matches_attribute_test(node, name, type_name, None)
                if schema is None
                else schema.matches_attribute_test(node, name, type_name)
            )
            if matches:
                yield item

    def check_declaration(self: Any, kind: str) -> None:
        proxy = self.parser.schema
        name = get_expanded_name(self[0].source, self.parser.namespaces)
        exists = proxy is not None and (
            proxy.has_element(name) if kind == "schema-element" else proxy.has_attribute(name)
        )
        if not exists:
            raise self.error(
                "XPST0008",
                f"no {kind} declaration {self[0].source!r} is in the in-scope schema definitions",
            )

    def nud_schema_element(self: Any) -> Any:
        schema_element_base.nud(self)
        check_declaration(self, "schema-element")
        return self

    def nud_schema_attribute(self: Any) -> Any:
        schema_attribute_base.nud(self)
        check_declaration(self, "schema-attribute")
        return self

    def declaration_name(self: Any) -> str:
        return get_expanded_name(self[0].source, self.parser.namespaces)

    def select_schema_element(self: Any, context: Any = None) -> Iterator[Any]:
        if context is None:  # pragma: no cover - token evaluation supplies it
            raise self.missing_context()
        proxy = self.parser.schema
        assert proxy is not None  # nud rejected an absent pool
        name = declaration_name(self)
        for item in context.iter_children_or_self():
            node = getattr(item, "xdm", None)
            if node is None:  # pragma: no cover - every bridged node has its XDM node
                continue
            if proxy.matches_schema_element(node, name):
                yield item

    def select_schema_attribute(self: Any, context: Any = None) -> Iterator[Any]:
        if context is None:  # pragma: no cover - token evaluation supplies it
            raise self.missing_context()
        proxy = self.parser.schema
        assert proxy is not None  # nud rejected an absent pool
        name = declaration_name(self)
        candidates = (
            context.iter_attributes() if context.axis is None else context.iter_children_or_self()
        )
        for item in candidates:
            node = getattr(item, "xdm", None)
            if node is None:  # pragma: no cover - every bridged node has its XDM node
                continue
            if proxy.matches_schema_attribute(node, name):
                yield item

    parser._register(
        "element",
        "_PyxsdElementKindTest",
        bases=(element_base,),
        label=element_base.label,
        select=select_element,
    )
    parser._register(
        "attribute",
        "_PyxsdAttributeKindTest",
        bases=(attribute_base,),
        label=attribute_base.label,
        select=select_attribute,
    )
    parser._register(
        "schema-element",
        "_PyxsdSchemaElementKindTest",
        bases=(schema_element_base,),
        label=schema_element_base.label,
        nud=nud_schema_element,
        select=select_schema_element,
    )
    parser._register(
        "schema-attribute",
        "_PyxsdSchemaAttributeKindTest",
        bases=(schema_attribute_base,),
        label=schema_attribute_base.label,
        nud=nud_schema_attribute,
        select=select_schema_attribute,
    )


def _function_item_arity(token: XPathFunction) -> int | tuple[int, int | None]:
    """The arity one function token reports when converted to an XDM item.

    A reference or variadic token has an explicit ``nargs`` (an integer or a
    ``(minimum, maximum)`` range).  An anonymous inline function and a partial
    application have no ``nargs``; their parsed parameter count is the arity.
    """
    nargs = getattr(token, "nargs", None)
    if isinstance(nargs, tuple):
        return _arity_tuple(nargs)
    if isinstance(nargs, int):
        return nargs
    return len(token)


def _external_function(
    parser: Any,
    callback: Callable[..., Any],
    *,
    name: str,
    prefix: str | None,
    sequence_types: tuple[str, ...],
) -> Any:
    """Register one declared function, allowing a reserved name when prefixed.

    elementpath refuses every ``external_function`` whose symbol is in the
    XPath reserved-name set, even though the restriction applies only to the
    standard function namespace.  A namespaced function whose local name is
    reserved but has no parser token (``my:switch``) is therefore legal; the
    per-parser set is shadowed for the registration and restored immediately.
    A name that also owns a grammar token (``node``, ``function``) still
    collides with that token and stays a parser-level residual.
    """
    if prefix is None or name not in parser.RESERVED_FUNCTION_NAMES:
        return parser.external_function(
            callback,
            name=name,
            prefix=prefix,
            sequence_types=sequence_types,
        )
    reserved = parser.RESERVED_FUNCTION_NAMES
    parser.RESERVED_FUNCTION_NAMES = reserved - {name}
    try:
        return parser.external_function(
            callback,
            name=name,
            prefix=prefix,
            sequence_types=sequence_types,
        )
    finally:
        parser.RESERVED_FUNCTION_NAMES = reserved


def _register_declared_function(
    parser: Any,
    name: str,
    static_context: XPathStaticContext,
    holder: _BridgeHolder,
) -> None:
    arity = static_context.functions[name]
    prefix, local = _split_function_name(name, static_context, parser.namespaces)

    def callback(*arguments: Any) -> Any:
        bridge = holder.bridge
        if bridge is None:
            raise RuntimeError(f"host function {name!r} has no evaluation context")
        # The implementation is a dynamic-context component: a reused parser
        # answers with the target of the evaluation that is running.
        target = _function_target(static_context, name, holder.functions)
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

    token_class = _external_function(
        parser,
        callback,
        name=local,
        prefix=prefix,
        sequence_types=("item()*",),
    )
    token_class.label = "function"
    token_class.nargs = arity

    def evaluate(self: Any, context: Any = None) -> Any:
        # Evaluate every argument expression as its full XDM sequence.  The
        # elementpath external-function default converts all but a first
        # ``item()*`` argument to one item, which would reject an ordinary
        # sequence argument such as ``xs:integer*``; declared-function
        # conversion belongs to the XSLT call boundary instead.
        previous = holder.xpath_context
        holder.xpath_context = context
        try:
            # ``evaluate`` returns a single value for most expressions and a
            # sequence for the rest; the bridge flattens either.  A partial
            # application's ``?`` placeholder is represented by its ``select``
            # value, because evaluating it returns the placeholder token.
            arguments = [
                list(child.select(context)) if child.symbol == "?" else child.evaluate(context)
                for child in self
            ]
            return self.callback(*arguments)
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
    functions: Mapping[str, Any],
) -> Any:
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


def _active_context(token: Any, context: Any) -> Any:
    stored = getattr(token, "context", None)
    return context if stored is None else stored


_COMPARISON_OPERATORS = {
    "=": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    ">": operator.gt,
    "<=": operator.le,
    ">=": operator.ge,
}


def _legacy_general_comparison(
    token: Any,
    context: Any = None,
    relational: bool | None = None,
) -> bool:
    """XPath 1.0 §3.4 general comparison over one evaluator token.

    The shared 2.0+ comparison token atomizes both operands first, so an empty
    node-set yields no pairs and loses the 1.0 rule that a node-set compared
    with a boolean converts to a boolean; it also applies the boolean rule to
    relational operators, which 1.0 defines numerically.  Both the native 1.0
    grammar and XPath 1.0 compatibility mode use this evaluation.
    """
    op = _COMPARISON_OPERATORS[token.symbol]
    if relational is None:
        relational = token.symbol in ("<", ">", "<=", ">=")
    active = _active_context(token, context)
    left_items = list(token._items[0].select(active))
    right_items = list(token._items[1].select(active))
    left_nodes = _is_node_set(left_items)
    right_nodes = _is_node_set(right_items)

    if left_nodes and right_nodes:
        for left in left_items:
            left_value: Any = _legacy_string_value(left)
            if relational:
                left_value = legacy_number_from_string(left_value)
            for right in right_items:
                right_value: Any = _legacy_string_value(right)
                if relational:
                    right_value = legacy_number_from_string(right_value)
                if op(left_value, right_value):
                    return True
        return False
    if left_nodes or right_nodes:
        nodes, other = (left_items, right_items) if left_nodes else (right_items, left_items)
        nodes_first = left_nodes
        if len(other) == 1 and isinstance(other[0], bool):
            node_boolean = bool(nodes)
            if relational:
                other_number = _legacy_number_value(other[0])
                return (
                    op(float(node_boolean), other_number)
                    if nodes_first
                    else op(other_number, float(node_boolean))
                )
            return op(node_boolean, other[0]) if nodes_first else op(other[0], node_boolean)
        if relational or (len(other) == 1 and is_legacy_number(other[0])):
            target = _legacy_number_value(other[0]) if other else math.nan
            for node in nodes:
                value = _legacy_number_value(node)
                if op(value, target) if nodes_first else op(target, value):
                    return True
            return False
        other_text = _legacy_string_value(other[0]) if other else ""
        for node in nodes:
            text = _legacy_string_value(node)
            if op(text, other_text) if nodes_first else op(other_text, text):
                return True
        return False

    left = left_items[0] if left_items else None
    right = right_items[0] if right_items else None
    if relational:
        return op(_legacy_number_value(left), _legacy_number_value(right))
    if isinstance(left, bool) or isinstance(right, bool):
        left, right = _legacy_boolean_value(left), _legacy_boolean_value(right)
    elif is_legacy_number(left) or is_legacy_number(right):
        left, right = _legacy_number_value(left), _legacy_number_value(right)
    else:
        left, right = _legacy_string_value(left), _legacy_string_value(right)
    return op(left, right)


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

    def legacy_comparison(self: Any, context: Any = None, relational: bool | None = None) -> bool:
        """XPath 1.0 §3.4 general comparison (see the shared implementation)."""
        return _legacy_general_comparison(self, context, relational)

    def legacy_relational(self: Any, context: Any = None) -> bool:
        return legacy_comparison(self, context, True)

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
        for item in items:
            if is_fragment(item):
                raise LegacyNodeSetError(
                    "fn:count() requires a node-set, but a result tree fragment "
                    "was supplied; a native XSLT 1.0 processor does not convert "
                    "a result tree fragment where a node-set is required"
                )
            if not isinstance(item, XPathNode):
                raise LegacyNodeSetError(
                    "fn:count() requires a node-set, but an atomic value was "
                    "supplied; a native XSLT 1.0 processor does not convert an "
                    "atomic value to a node-set"
                )
        return len(items)

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
        if math.isnan(start):
            return ""
        if len(self) == 2:
            # §4.2: substring(s, -Infinity) is the whole string, while
            # substring(s, +Infinity) is empty.
            if math.isinf(start):
                return source if start < 0 else ""
            begin = int(legacy_round(start)) - 1
            return source[max(begin, 0) :]
        if math.isinf(start):
            # A three-argument call needs positions below start + length;
            # with an infinite start no such position exists.
            return ""
        begin = int(legacy_round(start)) - 1
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

    def legacy_comparison_led(self: Any, left: Any) -> Any:
        """XPath 1.0 comparisons are left-recursive and may chain."""
        self[:] = left, self.parser.expression(30)
        return self

    def registered(symbol: str, base: Any, class_name: str, evaluate: Any) -> Any:
        options: dict[str, Any] = {"bases": (base,), "evaluate": evaluate}
        for attribute in ("label", "nargs", "sequence_types"):
            if hasattr(base, attribute):
                options[attribute] = getattr(base, attribute)
        if symbol in ("=", "!=", "<", ">", "<=", ">="):
            options["led"] = legacy_comparison_led
        return parser._register(symbol, class_name, **options)

    for symbol, class_name, evaluate in (
        ("=", "_PyxsdLegacyEqual", legacy_comparison),
        ("!=", "_PyxsdLegacyNotEqual", legacy_comparison),
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
    for symbol in ("<", ">", "<=", ">="):
        base = parser.symbol_table[symbol]
        registered(symbol, base, f"_PyxsdLegacyComparison{symbol}", legacy_relational)
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
    _install_legacy_path_operators(parser)


def _install_compatibility_comparisons(parser: Any) -> None:
    """Install the XPath 1.0 general comparison on a 2.0+ parser.

    XSLT 3.0 §3.9.1 runs an effective-version-1.0 element's expressions with
    XPath 1.0 compatibility mode.  elementpath's parsers apply the mode to
    function arguments and arithmetic but leave general comparison to the
    later operator table, so the six general comparison operators are
    re-registered here.  Nothing else about the 2.0+ grammar changes.
    """
    for symbol in ("=", "!=", "<", ">", "<=", ">="):
        base = parser.symbol_table.get(symbol)
        if base is None:  # pragma: no cover - every supported parser has them
            continue
        parser._register(
            symbol,
            f"_PyxsdCompatibility{'_'.join(str(ord(c)) for c in symbol)}",
            bases=(base,),
            label=getattr(base, "label", symbol),
            nargs=getattr(base, "nargs", 2),
            sequence_types=getattr(base, "sequence_types", ()),
            evaluate=_legacy_general_comparison,
        )


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
            if isinstance(left, XPathFunction) or left.symbol in (
                "$",
                "(",
                "(string)",
                "(integer)",
                "(decimal)",
                "(float)",
            ):
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


def _is_node_set(items: list[Any]) -> bool:
    """Whether one XPath 1.0 operand value is a node-set.

    XPath 1.0 has no empty sequence: an empty value can only be an empty
    node-set.  Every non-empty node-set contains only nodes.
    """
    return not items or all(isinstance(item, XPathNode) for item in items)


def _legacy_boolean_value(value: Any) -> bool:
    """The XPath 1.0 boolean conversion of one scalar evaluator value.

    A node-set is converted by its own non-emptiness before this helper runs;
    the values here are booleans, numbers, strings, and the legacy marker.
    """
    if isinstance(value, _LegacyRtfValue):
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, UntypedAtomic):
        return value.value != ""
    if is_legacy_number(value):
        return not (isinstance(value, float) and math.isnan(value)) and value != 0
    return _legacy_string_value(value) != ""


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
    symbols: Iterable[str],
) -> None:
    """Replace built-in callbacks with invocation-local XSLT-aware behavior.

    One token class is created per symbol and carries the bucket's *holder*,
    never a target: an override's implementation is a dynamic-context component
    (XSLT 3.0 section 5.3.4), so ``ContextFunctionToken`` reads the running
    evaluation's target from ``holder.overrides``.  Only the symbol set is baked
    into the parser, which is what lets one parser serve every evaluation of a
    static configuration.  Installing the override on this parser instance only
    avoids a process-global callback slot, so concurrent transformations stay
    independent.

    Each symbol is installed once, in first-seen order.  Two lexical spellings
    of one function (a Clark name and a prefixed name) both address the same
    parser symbol, and installing the second would capture the first override
    class as its plain base; a plain-callable override then re-enters itself
    through ``ContextFunctionToken.__call__`` until the stack is exhausted.
    One class per symbol also keeps the bucket key's ``frozenset`` honest: a
    single-spelling evaluation shares the bucket a duplicate-spelling one built.
    """
    for symbol in dict.fromkeys(symbols):
        base = parser.symbol_table.get(symbol)
        if base is None:
            # XPath 2.0 has no generate-id(); a stylesheet that calls it fails
            # at compile time, so there is nothing to override here.
            continue
        parser._register(
            symbol,
            f"_PyxsdOverride_{symbol.replace('-', '_')}",
            bases=(ContextFunctionToken,),
            label=base.label,
            nargs=base.nargs,
            sequence_types=base.sequence_types,
            namespace=getattr(base, "namespace", None),
            holder=holder,
            plain_base=base,
        )


class _BridgeHolder:
    """Per-evaluation state of the bucket's parser.

    One holder belongs to one bucket.  ``evaluate`` assigns the four fields
    before the evaluation and restores them afterwards, so a nested evaluation
    on the same bucket (an ``xsl:evaluate`` inside a predicate, a host callback,
    pattern matching inside pattern matching) sees its own values and the
    enclosing evaluation gets its own back.  A bucket's callbacks close over
    their holder, so concurrent transformations never share a bridge or a
    mutable callback slot.
    """

    __slots__ = ("bridge", "functions", "overrides", "xpath_context")

    def __init__(self) -> None:
        self.bridge: _ValueBridge | None = None
        #: The elementpath context of the call currently being evaluated.  A
        #: declared external function reads it to expose the live focus item to
        #: a contextual Python extension instead of the focus captured when the
        #: expression environment was built.
        self.xpath_context: Any = None
        #: The override table of the evaluation that is running, keyed by the
        #: parser symbol an override replaces.  ``{}`` outside an evaluation.
        self.overrides: Mapping[str, Any] = {}
        #: The declared-function implementations of the evaluation that is
        #: running.  ``{}`` outside an evaluation.
        self.functions: Mapping[str, Any] = {}


class _XsltXPathContext(XPathContext):
    """The dynamic context with grounded non-element following semantics.

    elementpath's ``following`` iterator only handles element context items.
    The XSLT/XPath axes are defined for every node kind, and an attribute's
    following axis begins with its owner element's children; the grounded
    adapters therefore supply the generic document-order sweep that
    ``iter_followings`` would otherwise skip.
    """

    def iter_followings(self) -> Any:
        item = self.item
        if isinstance(item, (ElementNode, DocumentNode)) or not isinstance(item, XPathNode):
            yield from super().iter_followings()
            return
        root = self.root or item.root_node
        status = self.axis
        self.axis = "following"
        try:
            for node in root.iter_descendants(with_self=False):
                if node.position > item.position:
                    self.item = node
                    yield node
        finally:
            self.item = item
            self.axis = status


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
        context = _XsltXPathContext(item="", **kwargs)
        cast(Any, context).item = None
        context.root = None
        context.document = None
        cast(Any, context).position = None
        cast(Any, context).size = None
    elif focus.item is not None and isinstance(focus.item, XdmNode):
        item = bridge.adapt_item(focus.item)
        if isinstance(item, DocumentNode):
            context = _XsltXPathContext(root=item, item=item, **kwargs)
        elif isinstance(item, ElementNode):
            context = _XsltXPathContext(root=item.tree.root_node, item=item, **kwargs)
        else:
            root_node = item.root_node
            if isinstance(root_node, (DocumentNode, ElementNode)):
                context = _XsltXPathContext(root=root_node, item=item, **kwargs)
            else:
                context = _XsltXPathContext(item=item, **kwargs)
    elif focus.item is not None:
        context = _XsltXPathContext(item=bridge.adapt_item(focus.item), **kwargs)
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
    """One sequence per variable in the representation the machine reads.

    A one-item variable is the item itself; every other cardinality is a Python
    list.  ``ValueToken.select`` expands a list item by item, so an empty
    variable is an empty *sequence* and a multi-item variable is several
    operands.  A tuple is one item to elementpath, which would make an
    empty-sequence comparison atomize ``()`` as a value instead of comparing an
    empty sequence.
    """
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
        values[name] = converted[0] if len(converted) == 1 else converted
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
