"""Immutable transformation results and in-memory result-tree construction.

The raw principal and secondary sequences are always retained.  Tree delivery
is a separate view built directly from XDM nodes, never by serializing and
reparsing XML.  Secondary results live only in memory: producing a result never
writes a file or touches the network.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from pyxsd.exceptions import PyXSDError
from pyxsd.source_spans import SourceIndex
from pyxsd.xdm.nodes import NodeKind, XdmAnnotation, XdmDocument, XdmNode
from pyxsd.xdm.values import XdmAtomic, XdmItem, XdmSequence
from pyxsd.xslt.errors import XsltError

__all__ = [
    "Message",
    "ResultEntry",
    "ResultShapeError",
    "TransformationResult",
]

_RESULT_SOURCE = "<result>"
_RESULT_INDEX = SourceIndex(_RESULT_SOURCE, b"<result/>")
_DEFAULT_ANNOTATION = XdmAnnotation(None, None)
_CONSTRUCTED_ANNOTATIONS = {
    NodeKind.ELEMENT: XdmAnnotation("{http://www.w3.org/2001/XMLSchema}untyped", False),
    NodeKind.ATTRIBUTE: XdmAnnotation("{http://www.w3.org/2001/XMLSchema}untypedAtomic", None),
    NodeKind.TEXT: XdmAnnotation("{http://www.w3.org/2001/XMLSchema}untypedAtomic", None),
}
_XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
_XML_NS = "http://www.w3.org/XML/1998/namespace"


class ResultShapeError(PyXSDError):
    """The requested result view does not have the required shape.

    A raw sequence of atomics, maps, or arrays is a valid principal result but
    is not a document tree.  Tree delivery must be requested explicitly (or by
    the stylesheet's output defaults); the raw sequence is never silently
    wrapped.
    """


@dataclass(frozen=True, slots=True)
class Message:
    """One stylesheet message or diagnostic event."""

    text: str
    code: str | None = None
    uri: str | None = None
    terminate: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("a message text must be a string")
        for label, value in (("code", self.code), ("uri", self.uri)):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"a message {label} must be a string or None")
        if not isinstance(self.terminate, bool):
            raise TypeError("a message terminate flag must be a boolean")


def _absolute_uri(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty absolute URI string")
    if not urlsplit(value).scheme:
        raise ValueError(f"{label} must be an absolute URI, got {value!r}")
    return value


def _freeze_parameters(parameters: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(parameters, Mapping):
        raise TypeError("serialization parameters must be a mapping")
    frozen: dict[str, str] = {}
    for key, value in parameters.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("serialization parameters must map strings to strings")
        frozen[key] = value
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class ResultEntry:
    """One principal or secondary result in every available view.

    ``raw`` is the exact XDM sequence.  ``tree`` is the delivered document when
    tree construction was requested and possible.  ``serialized`` is reserved
    for an explicitly requested serialization (Task 16 never serializes), while
    ``serialization`` records the effective output parameters.  ``output_uri``
    identifies the principal result or the absolute URI of a secondary result.
    """

    raw: XdmSequence
    output_uri: str | None = None
    tree: XdmDocument | None = None
    serialized: str | None = None
    serialization: Mapping[str, str] = field(default_factory=dict)
    messages: tuple[Message, ...] = ()

    def __post_init__(self) -> None:
        raw = tuple(self.raw)
        if not all(isinstance(item, XdmItem) for item in raw):
            raise TypeError("a result raw sequence must contain XDM items")
        object.__setattr__(self, "raw", raw)
        if self.output_uri is not None:
            _absolute_uri(self.output_uri, "output_uri")
        if self.tree is not None and not isinstance(self.tree, XdmDocument):
            raise TypeError("a result tree must be an XdmDocument or None")
        if self.serialized is not None and not isinstance(self.serialized, str):
            raise TypeError("a serialized result must be a string or None")
        object.__setattr__(self, "serialization", _freeze_parameters(self.serialization))
        messages = tuple(self.messages)
        if not all(isinstance(message, Message) for message in messages):
            raise TypeError("result messages must be Message values")
        object.__setattr__(self, "messages", messages)


@dataclass(frozen=True, slots=True)
class TransformationResult:
    """The principal and secondary results of one transformation."""

    principal_entry: ResultEntry
    secondary: Mapping[str, ResultEntry] = field(default_factory=dict)
    messages: tuple[Message, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.principal_entry, ResultEntry):
            raise TypeError("principal_entry must be a ResultEntry")
        secondary: dict[str, ResultEntry] = {}
        if not isinstance(self.secondary, Mapping):
            raise TypeError("secondary results must be a mapping")
        for uri, entry in self.secondary.items():
            _absolute_uri(uri, "secondary result URI")
            if not isinstance(entry, ResultEntry):
                raise TypeError("secondary results must be ResultEntry values")
            if entry.output_uri != uri:
                raise ValueError(
                    f"secondary result entry output_uri {entry.output_uri!r} does not match {uri!r}"
                )
            secondary[uri] = entry
        object.__setattr__(self, "secondary", MappingProxyType(secondary))
        messages = tuple(self.messages)
        if not all(isinstance(message, Message) for message in messages):
            raise TypeError("result messages must be Message values")
        object.__setattr__(self, "messages", messages)

    @property
    def principal(self) -> XdmSequence:
        """The raw principal XDM sequence, before delivery normalization."""
        return self.principal_entry.raw

    @property
    def principal_tree(self) -> XdmDocument:
        """The delivered principal document, or an explicitly document-shaped raw result.

        Raises :class:`ResultShapeError` rather than wrapping a non-document
        sequence or silently serializing and reparsing it.
        """
        tree = self.principal_entry.tree
        if tree is not None:
            return tree
        raw = self.principal_entry.raw
        if len(raw) == 1 and isinstance(raw[0], XdmDocument):
            return raw[0]
        raise ResultShapeError(
            "the principal result is not a document tree; request delivery='document', "
            "honor a tree-building output method, or inspect the raw sequence"
        )


def _namespace(name: str) -> str | None:
    if not name.startswith("{"):
        return None
    return name[1:].partition("}")[0]


def _prefix_for(namespace: str | None, namespaces: Mapping[str, str]) -> str:
    if namespace is None:
        return ""
    default = namespaces.get("")
    if default == namespace:
        return ""
    for prefix, uri in namespaces.items():
        if uri == namespace and prefix:
            return prefix
    return ""


class _NodeFactory:
    """One constructed document and the nodes that belong to it."""

    def __init__(
        self,
        *,
        document_uri: str | None = None,
        base_uri: str | None = None,
        schema_pool: object = None,
    ) -> None:
        self._next_id = 1
        self.document = XdmDocument(
            source_name=_RESULT_SOURCE,
            source_index=_RESULT_INDEX,
            document_uri=document_uri,
            base_uri=base_uri if base_uri is not None else document_uri,
            schema_pool=schema_pool,
        )

    def _new(
        self,
        kind: NodeKind,
        *,
        parent: XdmNode | None,
        name: str | None = None,
        value: str = "",
        target: str | None = None,
        base_uri: str | None = None,
        in_scope_namespaces: Mapping[str, str] | None = None,
        namespace_declarations: tuple[tuple[str, str | None], ...] = (),
        prefix: str = "",
        namespace_uri: str | None = None,
        annotations: XdmAnnotation | None = None,
    ) -> XdmNode:
        if annotations is None:
            annotations = _CONSTRUCTED_ANNOTATIONS.get(kind, _DEFAULT_ANNOTATION)
        node = XdmNode(
            kind,
            document=self.document,
            node_id=self._next_id,
            source_name=_RESULT_SOURCE,
            source_index=_RESULT_INDEX,
            parent=parent,
            name=name,
            value=value,
            target=target,
            annotations=annotations,
            base_uri=base_uri if base_uri is not None else self.document.base_uri,
            document_uri=self.document.document_uri,
            in_scope_namespaces=in_scope_namespaces or {},
            namespace_declarations=namespace_declarations,
            prefix=prefix,
            namespace_uri=namespace_uri,
        )
        self._next_id += 1
        return node

    def element(
        self,
        name: str,
        *,
        attributes: Iterable[tuple[str, str]] = (),
        namespaces: Mapping[str, str] | None = None,
        base_uri: str | None = None,
    ) -> XdmNode:
        """Create one detached element in this factory's document."""
        scope = dict(namespaces or {})
        scope.setdefault("xml", _XML_NS)
        namespace = _namespace(name)
        prefix = _prefix_for(namespace, scope)
        declarations = tuple(
            (declared_prefix, uri)
            for declared_prefix, uri in scope.items()
            if declared_prefix != "xml"
        )
        element = self._new(
            NodeKind.ELEMENT,
            parent=None,
            name=name,
            base_uri=base_uri,
            in_scope_namespaces=scope,
            namespace_declarations=declarations,
            prefix=prefix,
            namespace_uri=namespace,
        )
        for attribute_name, value in attributes:
            attribute_namespace = _namespace(attribute_name)
            attribute = self._new(
                NodeKind.ATTRIBUTE,
                parent=element,
                name=attribute_name,
                value=value,
                base_uri=base_uri,
                in_scope_namespaces=scope,
                prefix=_prefix_for(attribute_namespace, scope),
                namespace_uri=attribute_namespace,
            )
            element._append_attribute(attribute)
        for declared_prefix, uri in declarations:
            namespace_node = self._new(
                NodeKind.NAMESPACE,
                parent=element,
                name=declared_prefix or None,
                value=uri,
                in_scope_namespaces=scope,
                prefix=declared_prefix,
            )
            element._append_namespace_node(namespace_node)
        return element

    def text(self, value: str, *, parent: XdmNode | None = None) -> XdmNode:
        return self._new(NodeKind.TEXT, parent=parent, value=value)

    def clone(self, source: XdmNode, *, parent: XdmNode) -> XdmNode:
        """Deep-copy one node (and its subtree) into this document."""
        if source.kind is NodeKind.ELEMENT:
            node = self._new(
                NodeKind.ELEMENT,
                parent=parent,
                name=source.name,
                base_uri=source.base_uri,
                in_scope_namespaces=source.in_scope_namespaces,
                namespace_declarations=source.namespace_declarations,
                prefix=source.prefix,
                namespace_uri=source.namespace_uri,
                annotations=source.annotation,
            )
            for attribute in source.attributes:
                node._append_attribute(self.clone(attribute, parent=node))
            for namespace in source.namespace_nodes:
                node._append_namespace_node(self.clone(namespace, parent=node))
            for child in source.children:
                node._append_child(self.clone(child, parent=node))
            return node
        value = source.string_value
        node = self._new(
            source.kind,
            parent=parent,
            name=source.name,
            value=value,
            target=source.target,
            base_uri=source.base_uri,
            in_scope_namespaces=source.in_scope_namespaces,
            prefix=source.prefix,
            namespace_uri=source.namespace_uri,
            annotations=source.annotation,
        )
        return node

    def finalize(self) -> XdmDocument:
        self.document._finalize_document_order()
        return self.document

    def _append(self, parent: XdmNode, child: XdmNode) -> None:
        parent._append_child(child)
        if (
            parent.kind is NodeKind.DOCUMENT
            and child.kind is NodeKind.ELEMENT
            and self.document._root_element is None
        ):
            self.document._root_element = child


def _append_text(factory: _NodeFactory, parent: XdmNode, value: str) -> XdmNode | None:
    if not value:
        return None
    children = parent.children
    if children and children[-1].kind is NodeKind.TEXT:
        children[-1]._append_text(value)
        return children[-1]
    text = factory.text(value, parent=parent)
    factory._append(parent, text)
    return text


def _append_complex_content(
    factory: _NodeFactory,
    parent: XdmNode,
    sequence: Iterable[XdmItem],
    *,
    allow_attributes: bool,
) -> None:
    """Apply XSLT complex-content construction to *sequence* under *parent*."""
    seen_non_attribute = False
    last_atomic = False
    for item in sequence:
        if isinstance(item, XdmAtomic):
            value = item.lexical
            if last_atomic and value:
                value = " " + value
            _append_text(factory, parent, value)
            seen_non_attribute = True
            last_atomic = True
            continue
        if not isinstance(item, XdmNode):
            raise XsltError(
                "a final result tree cannot contain a function item",
                code="XTDE0450",
            )
        if item.kind is NodeKind.DOCUMENT:
            _append_complex_content(
                factory, parent, item.children, allow_attributes=allow_attributes
            )
            last_atomic = False
            continue
        if item.kind in (NodeKind.ATTRIBUTE, NodeKind.NAMESPACE):
            if not allow_attributes or parent.kind is NodeKind.DOCUMENT:
                raise XsltError(
                    "a document result tree cannot contain attribute or namespace nodes",
                    code="XTDE0420",
                )
            if seen_non_attribute:
                raise XsltError(
                    "an attribute or namespace node must precede other content",
                    code="XTDE0410",
                )
            if item.kind is NodeKind.ATTRIBUTE:
                parent._append_attribute(factory.clone(item, parent=parent))
            else:
                parent._append_namespace_node(factory.clone(item, parent=parent))
            last_atomic = False
            continue
        if item.kind is NodeKind.TEXT:
            _append_text(factory, parent, item.string_value)
            seen_non_attribute = True
            last_atomic = False
            continue
        factory._append(parent, factory.clone(item, parent=parent))
        seen_non_attribute = True
        last_atomic = False


def _build_result_tree(
    sequence: Iterable[XdmItem],
    *,
    output_uri: str | None = None,
    base_uri: str | None = None,
    schema_pool: object = None,
) -> XdmDocument:
    """Build one final document directly from XDM content, without reparsing."""
    factory = _NodeFactory(
        document_uri=output_uri,
        base_uri=base_uri,
        schema_pool=schema_pool,
    )
    _append_complex_content(
        factory,
        factory.document,
        sequence,
        allow_attributes=False,
    )
    return factory.finalize()
