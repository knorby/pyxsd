"""Grounded XDM 3.1 nodes with source provenance.

The node layer parses XML itself instead of routing through the lossy public
XPath projection: comments, processing instructions, namespace bindings,
``xml:base``, document identity, document order, annotations, and source
locations all survive.  ElementTree is deliberately not used even as storage,
because a second side-table representation would still lose source namespace
declarations and node identity.

Nodes are :class:`~pyxsd.xdm.values.XdmItem` objects with stable object
identity.  Sequences are ordinary XDM sequences, so a node is never confused
with an atomic value.  The module imports no stylesheet execution code and
does not stream.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import urljoin
from xml.parsers import expat

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import XML_NS, XSD_NS, XSI_NS, clark, local_name
from pyxsd.source_spans import SourceIndex, SourceSpan
from pyxsd.xdm.values import XdmAtomic, XdmItem, XdmSequence

__all__ = [
    "NodeKind",
    "XdmAnnotation",
    "XdmAnnotations",
    "XdmDocument",
    "XdmNode",
]

_NAMESPACE_SEPARATOR = "\x1f"
_XSD_PREFIX = f"{{{XSD_NS}}}"
_UNTYPED = f"{_XSD_PREFIX}untyped"
_UNTYPED_ATOMIC = f"{_XSD_PREFIX}untypedAtomic"
_EMPTY_NAMESPACES: Mapping[str, str] = MappingProxyType({})


class NodeKind(StrEnum):
    """The seven XDM node kinds."""

    DOCUMENT = "document"
    ELEMENT = "element"
    ATTRIBUTE = "attribute"
    TEXT = "text"
    NAMESPACE = "namespace"
    PROCESSING_INSTRUCTION = "processing-instruction"
    COMMENT = "comment"


@dataclass(frozen=True, slots=True)
class XdmAnnotation:
    """An XDM type annotation with source evidence kept separate.

    ``type_name`` is the node's XDM type annotation, or ``None`` when the
    node kind has no type annotation.  ``nilled`` is the XDM nilled property,
    present only on elements.  ``xsi_type``, ``nil_lexical``, and
    ``nil_declared`` are recorded source evidence from an unvalidated parse;
    they do not change the XDM properties.  ``validity`` and
    ``governing_declaration`` are provisional fields for the later
    schema-aware annotation layer.
    """

    type_name: str | None
    nilled: bool | None = False
    validity: str = "not-validated"
    governing_declaration: Any = None
    xsi_type: str | None = None
    nil_lexical: str | None = None
    nil_declared: bool = False

    @property
    def actual_type(self) -> str | None:
        """The actual type name, matching the eventual occurrence API."""
        return self.type_name


XdmAnnotations = XdmAnnotation


@dataclass(frozen=True, slots=True)
class _ExpandedName:
    """A decoded ElementTree/Expat expanded name with its source prefix."""

    uri: str | None
    local: str
    prefix: str

    @property
    def clark(self) -> str:
        return clark(self.uri, self.local)


_DEFAULT_ANNOTATIONS = XdmAnnotation(None, None)


class XdmNode(XdmItem):
    """One grounded XDM node.

    Navigation returns the same node objects on every access, so identity is
    stable.  Attributes and namespace nodes are not children; they have their
    own accessors and a document-order position between their parent element
    and its first child.
    """

    __slots__ = (
        "_annotations",
        "_attributes",
        "_base_uri",
        "_children",
        "_document",
        "_document_order",
        "_document_uri",
        "_in_scope_namespaces",
        "_kind",
        "_name",
        "_namespace_declarations",
        "_namespace_nodes",
        "_namespace_uri",
        "_node_id",
        "_parent",
        "_path",
        "_prefix",
        "_source_index",
        "_source_name",
        "_target",
        "_value",
    )

    def __init__(
        self,
        kind: NodeKind,
        *,
        document: XdmDocument,
        node_id: int,
        source_name: str,
        source_index: SourceIndex,
        parent: XdmNode | None = None,
        name: str | None = None,
        value: str = "",
        target: str | None = None,
        annotations: XdmAnnotation = _DEFAULT_ANNOTATIONS,
        base_uri: str | None = None,
        document_uri: str | None = None,
        in_scope_namespaces: Mapping[str, str] = _EMPTY_NAMESPACES,
        namespace_declarations: tuple[tuple[str, str | None], ...] = (),
        path: tuple[PathStep, ...] = (),
        prefix: str = "",
        namespace_uri: str | None = None,
    ) -> None:
        self._kind = kind
        self._document = document
        self._node_id = node_id
        self._source_name = source_name
        self._source_index = source_index
        self._parent = parent
        self._name = name
        self._value = value
        self._target = target
        self._annotations = annotations
        self._base_uri = base_uri
        self._document_uri = document_uri
        self._in_scope_namespaces = in_scope_namespaces
        self._namespace_declarations = namespace_declarations
        self._path = path
        self._prefix = prefix
        self._namespace_uri = namespace_uri
        self._children: list[XdmNode] = []
        self._attributes: list[XdmNode] = []
        self._namespace_nodes: list[XdmNode] = []
        self._document_order = -1

    # -- identity and structure ------------------------------------------------

    @property
    def kind(self) -> NodeKind:
        return self._kind

    @property
    def node_id(self) -> int:
        """A document-local identity number; the document node is zero."""
        return self._node_id

    @property
    def document_order(self) -> int:
        """The node's position in its document's total document order."""
        return self._document_order

    @property
    def parent(self) -> XdmNode | None:
        return self._parent

    @property
    def children(self) -> tuple[XdmNode, ...]:
        return tuple(self._children)

    @property
    def attributes(self) -> tuple[XdmNode, ...]:
        return tuple(self._attributes)

    @property
    def namespace_nodes(self) -> tuple[XdmNode, ...]:
        return tuple(self._namespace_nodes)

    @property
    def document(self) -> XdmDocument:
        return self._document

    @property
    def root(self) -> XdmNode:
        node: XdmNode = self
        while node._parent is not None:
            node = node._parent
        return node

    @property
    def ancestors(self) -> tuple[XdmNode, ...]:
        result: list[XdmNode] = []
        node = self._parent
        while node is not None:
            result.append(node)
            node = node._parent
        return tuple(result)

    @property
    def descendants(self) -> tuple[XdmNode, ...]:
        result: list[XdmNode] = []
        stack = list(reversed(self._children))
        while stack:
            node = stack.pop()
            result.append(node)
            stack.extend(reversed(node._children))
        return tuple(result)

    @property
    def following_sibling(self) -> XdmNode | None:
        parent = self._parent
        if parent is None:
            return None
        siblings = parent._children
        for index, sibling in enumerate(siblings):
            if sibling is self:
                if index + 1 < len(siblings):
                    return siblings[index + 1]
                return None
        return None

    @property
    def preceding_sibling(self) -> XdmNode | None:
        parent = self._parent
        if parent is None:
            return None
        siblings = parent._children
        for index, sibling in enumerate(siblings):
            if sibling is self:
                if index:
                    return siblings[index - 1]
                return None
        return None

    @property
    def next_sibling(self) -> XdmNode | None:
        return self.following_sibling

    @property
    def previous_sibling(self) -> XdmNode | None:
        return self.preceding_sibling

    def attribute(self, name: str) -> XdmNode | None:
        """Return the attribute with expanded *name*, or ``None``."""
        for attribute in self._attributes:
            if attribute._name == name:
                return attribute
        return None

    def namespace_node(self, prefix: str) -> XdmNode | None:
        """Return the in-scope namespace node for *prefix*, or ``None``."""
        for namespace in self._namespace_nodes:
            if namespace._prefix == prefix:
                return namespace
        return None

    def compare_document_order(self, other: object) -> int | None:
        """Compare document positions, or ``None`` across documents/items."""
        if not isinstance(other, XdmNode):
            return None
        if self._document is not other._document:
            return None
        if self._document_order == other._document_order:
            return 0
        return -1 if self._document_order < other._document_order else 1

    def is_before(self, other: object) -> bool:
        return self.compare_document_order(other) == -1

    def is_after(self, other: object) -> bool:
        return self.compare_document_order(other) == 1

    # -- names, values, and annotations ---------------------------------------

    @property
    def name(self) -> str | None:
        """The node name, PI target, or ``None`` when the node name is absent."""
        return self._name

    @property
    def local_name(self) -> str | None:
        if self._kind is NodeKind.NAMESPACE:
            return self._name
        if self._kind in (NodeKind.ELEMENT, NodeKind.ATTRIBUTE):
            return local_name(cast(str, self._name))
        return None

    @property
    def prefix(self) -> str:
        return self._prefix

    @property
    def namespace_uri(self) -> str | None:
        return self._namespace_uri

    @property
    def target(self) -> str | None:
        """The processing-instruction target, or ``None``."""
        return self._target

    @property
    def annotations(self) -> XdmAnnotation:
        return self._annotations

    @property
    def annotation(self) -> XdmAnnotation:
        """The occurrence annotation; singular forward-compatible accessor."""
        return self._annotations

    @property
    def type_name(self) -> str | None:
        """The XDM type annotation, or ``None`` when the kind has none."""
        return self._annotations.type_name

    @property
    def nilled(self) -> bool | None:
        """The XDM nilled property, present only on elements."""
        return self._annotations.nilled

    @property
    def string_value(self) -> str:
        if self._kind in (
            NodeKind.TEXT,
            NodeKind.ATTRIBUTE,
            NodeKind.NAMESPACE,
            NodeKind.PROCESSING_INSTRUCTION,
            NodeKind.COMMENT,
        ):
            return self._value
        return "".join(node._value for node in self.descendants if node.kind is NodeKind.TEXT)

    @property
    def typed_value(self) -> XdmSequence:
        captured = getattr(self._annotations, "typed_values", None)
        if captured is not None:
            return captured
        if self._kind in (NodeKind.DOCUMENT, NodeKind.ELEMENT):
            return (XdmAtomic.untyped_atomic(self.string_value),)
        if self._kind in (NodeKind.TEXT, NodeKind.ATTRIBUTE):
            return (XdmAtomic.untyped_atomic(self._value),)
        return (XdmAtomic.string(self._value),)

    def _apply_occurrence_annotation(self, annotation: Any) -> None:
        """Install one captured schema occurrence annotation (internal)."""
        self._annotations = annotation

    # -- namespaces and base URIs ---------------------------------------------

    @property
    def namespace_binding(self) -> str | None:
        """The URI bound by a namespace node, or ``None`` for other kinds."""
        if self._kind is not NodeKind.NAMESPACE:
            return None
        return self._value

    @property
    def bound_uri(self) -> str | None:
        """Alias for :attr:`namespace_binding`."""
        return self.namespace_binding

    @property
    def in_scope_namespaces(self) -> Mapping[str, str]:
        return self._in_scope_namespaces

    @property
    def namespace_declarations(self) -> tuple[tuple[str, str | None], ...]:
        """The namespace declarations authored on this element, in order."""
        return self._namespace_declarations

    @property
    def base_uri(self) -> str | None:
        """The XDM base URI; namespace nodes have no base URI."""
        return self._base_uri

    @property
    def document_uri(self) -> str | None:
        """The XDM document URI, present only on the document node."""
        if self._kind is not NodeKind.DOCUMENT:
            return None
        return self._document_uri

    @property
    def owner_document_uri(self) -> str | None:
        """The document URI of the document that owns this node."""
        return self._document_uri

    # -- provenance ------------------------------------------------------------

    def source_location(self) -> IssueLocation | None:
        """The element occurrence or attribute occurrence for this node."""
        if self._kind is NodeKind.ELEMENT:
            return IssueLocation(self._source_name, self._path)
        if self._kind is NodeKind.ATTRIBUTE:
            return IssueLocation(self._source_name, self._path, self._name)
        return None

    def source_span(self) -> SourceSpan | None:
        """The exact source byte range for an element occurrence."""
        if self._kind is not NodeKind.ELEMENT:
            return None
        return self._source_index.span(self._path)

    # -- internal tree building ------------------------------------------------

    def _append_child(self, child: XdmNode) -> None:
        self._children.append(child)

    def _append_attribute(self, attribute: XdmNode) -> None:
        self._attributes.append(attribute)

    def _append_namespace_node(self, namespace: XdmNode) -> None:
        self._namespace_nodes.append(namespace)

    def _append_text(self, data: str) -> None:
        self._value += data


class XdmDocument(XdmNode):
    """An XDM document node holding one parsed XML document."""

    __slots__ = ("_root_element", "_schema_pool")

    def __init__(
        self,
        *,
        source_name: str,
        source_index: SourceIndex,
        document_uri: str | None,
        base_uri: str | None,
        schema_pool: Any = None,
    ) -> None:
        super().__init__(
            NodeKind.DOCUMENT,
            document=self,
            node_id=0,
            source_name=source_name,
            source_index=source_index,
            annotations=XdmAnnotation(None, None),
            base_uri=base_uri,
            document_uri=document_uri,
            in_scope_namespaces=_EMPTY_NAMESPACES,
        )
        self._root_element: XdmNode | None = None
        self._schema_pool = schema_pool

    @classmethod
    def parse(
        cls,
        source: Any,
        *,
        source_index: SourceIndex | None = None,
        schema_pool: Any = None,
        document_uri: str | None = None,
        base_uri: str | None = None,
        source_name: str | None = None,
    ) -> XdmDocument:
        """Parse a path, bytes-like object, or readable stream into a document.

        A missing *source_index* is built from the same bytes the node parser
        consumes.  Passing one lets a caller reuse an already-built index; it
        must describe the same revision of the source.  *document_uri* and
        *base_uri* default from a path-like source, and *schema_pool* is
        retained for the schema-aware annotation layer.
        """
        content, natural_name = _read_source(source)
        if source_name is None:
            source_name = natural_name
        if source_index is None:
            data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
            source_index = SourceIndex(source_name or "<memory>", data)
        if source_name is None:
            source_name = source_index.source
        if document_uri is None:
            document_uri = _document_uri_from_source(source, natural_name)
        if base_uri is None:
            base_uri = document_uri
        document = cls(
            source_name=source_name,
            source_index=source_index,
            document_uri=document_uri,
            base_uri=base_uri,
            schema_pool=schema_pool,
        )
        builder = _Builder(document)
        parser = expat.ParserCreate(namespace_separator=_NAMESPACE_SEPARATOR)
        parser.namespace_prefixes = True
        parser.StartDoctypeDeclHandler = builder.start_doctype
        parser.EndDoctypeDeclHandler = builder.end_doctype
        parser.StartNamespaceDeclHandler = builder.start_namespace_declaration
        parser.StartElementHandler = builder.start_element
        parser.EndElementHandler = builder.end_element
        parser.CharacterDataHandler = builder.characters
        parser.CommentHandler = builder.comment
        parser.ProcessingInstructionHandler = builder.processing_instruction
        try:
            parser.Parse(content, True)
        except expat.ExpatError as exc:
            raise PyXSDError(f"{source_name}: malformed XML: {exc}") from exc
        document._finalize_document_order()
        return document

    @property
    def source_index(self) -> SourceIndex:
        return self._source_index

    @property
    def source_name(self) -> str:
        return self._source_name

    @property
    def schema_pool(self) -> Any:
        return self._schema_pool

    @property
    def root_element(self) -> XdmNode:
        return cast(XdmNode, self._root_element)

    def _finalize_document_order(self) -> None:
        order = 0
        stack: list[XdmNode] = [self]
        while stack:
            node = stack.pop()
            node._document_order = order
            order += 1
            stack.extend(reversed(node._children))
            if node.kind is NodeKind.ELEMENT:
                stack.extend(reversed(node._attributes))
                stack.extend(reversed(node._namespace_nodes))


class _Builder:
    """One parse's element stack, namespace frames, and sibling counters."""

    def __init__(self, document: XdmDocument) -> None:
        self._document = document
        self._next_id = 1
        self._elements: list[XdmNode] = []
        self._namespaces: list[dict[str, str]] = [{"xml": XML_NS}]
        self._pending_declarations: list[tuple[str, str | None]] = []
        self._paths: list[PathStep] = []
        self._counts: list[dict[str, int]] = [{}]
        self._in_doctype = False

    def start_doctype(
        self,
        name: str,
        system_id: str | None,
        public_id: str | None,
        has_internal_subset: bool,
    ) -> None:
        del name, system_id, public_id, has_internal_subset
        self._in_doctype = True

    def end_doctype(self) -> None:
        self._in_doctype = False

    def start_namespace_declaration(self, prefix: str | None, uri: str | None) -> None:
        self._pending_declarations.append((prefix or "", uri))

    def start_element(self, raw_name: str, raw_attributes: dict[str, str]) -> None:
        expanded = _decode_name(raw_name)
        parent = self._elements[-1] if self._elements else self._document

        declarations = tuple(self._pending_declarations)
        self._pending_declarations.clear()
        frame = dict(self._namespaces[-1])
        for prefix, uri in declarations:
            if uri is None:
                frame.pop(prefix, None)
            else:
                frame[prefix] = uri
        self._namespaces.append(frame)
        in_scope_namespaces = MappingProxyType(frame)

        decoded_attributes = tuple(
            (_decode_name(raw), value) for raw, value in raw_attributes.items()
        )
        attribute_values = {name.clark: value for name, value in decoded_attributes}
        xml_base = attribute_values.get(f"{{{XML_NS}}}base")
        base_uri = _apply_xml_base(parent.base_uri, xml_base)
        annotations = _element_annotations(attribute_values, frame)

        counts = self._counts[-1]
        index = counts.get(expanded.clark, 0) + 1
        counts[expanded.clark] = index
        path = (*self._paths, PathStep(expanded.clark, index))

        element = self._new_node(
            NodeKind.ELEMENT,
            parent=parent,
            name=expanded.clark,
            annotations=annotations,
            base_uri=base_uri,
            in_scope_namespaces=in_scope_namespaces,
            namespace_declarations=declarations,
            path=path,
            prefix=expanded.prefix,
            namespace_uri=expanded.uri,
        )
        parent._append_child(element)
        self._elements.append(element)
        self._paths.append(path[-1])
        self._counts.append({})
        if self._document._root_element is None:
            self._document._root_element = element

        for name, value in decoded_attributes:
            attribute = self._new_node(
                NodeKind.ATTRIBUTE,
                parent=element,
                name=name.clark,
                value=value,
                annotations=XdmAnnotation(_UNTYPED_ATOMIC, None),
                base_uri=base_uri,
                in_scope_namespaces=in_scope_namespaces,
                path=path,
                prefix=name.prefix,
                namespace_uri=name.uri,
            )
            element._append_attribute(attribute)

        for prefix in sorted(frame, key=_namespace_sort_key):
            uri = frame[prefix]
            namespace = self._new_node(
                NodeKind.NAMESPACE,
                parent=element,
                name=prefix or None,
                value=uri,
                annotations=XdmAnnotation(None, None),
                base_uri=None,
                in_scope_namespaces=in_scope_namespaces,
                prefix=prefix,
                namespace_uri=None,
            )
            element._append_namespace_node(namespace)

    def end_element(self, name: str) -> None:
        del name
        self._elements.pop()
        self._paths.pop()
        self._counts.pop()
        self._namespaces.pop()

    def characters(self, data: str) -> None:
        parent = self._elements[-1]
        if parent._children and parent._children[-1].kind is NodeKind.TEXT:
            parent._children[-1]._append_text(data)
            return
        text = self._new_node(
            NodeKind.TEXT,
            parent=parent,
            value=data,
            annotations=XdmAnnotation(_UNTYPED_ATOMIC, None),
            base_uri=parent.base_uri,
            in_scope_namespaces=parent.in_scope_namespaces,
        )
        parent._append_child(text)

    def comment(self, data: str) -> None:
        if self._in_doctype:
            return
        parent = self._elements[-1] if self._elements else self._document
        node = self._new_node(
            NodeKind.COMMENT,
            parent=parent,
            value=data,
            annotations=XdmAnnotation(None, None),
            base_uri=parent.base_uri,
            in_scope_namespaces=parent.in_scope_namespaces,
        )
        parent._append_child(node)

    def processing_instruction(self, target: str, data: str) -> None:
        if self._in_doctype:
            return
        parent = self._elements[-1] if self._elements else self._document
        node = self._new_node(
            NodeKind.PROCESSING_INSTRUCTION,
            parent=parent,
            name=target,
            value=data,
            target=target,
            annotations=XdmAnnotation(None, None),
            base_uri=parent.base_uri,
            in_scope_namespaces=parent.in_scope_namespaces,
        )
        parent._append_child(node)

    def _new_node(
        self,
        kind: NodeKind,
        *,
        parent: XdmNode,
        name: str | None = None,
        value: str = "",
        target: str | None = None,
        annotations: XdmAnnotation,
        base_uri: str | None,
        in_scope_namespaces: Mapping[str, str],
        namespace_declarations: tuple[tuple[str, str | None], ...] = (),
        path: tuple[PathStep, ...] = (),
        prefix: str = "",
        namespace_uri: str | None = None,
    ) -> XdmNode:
        node = XdmNode(
            kind,
            document=self._document,
            node_id=self._next_id,
            source_name=self._document.source_name,
            source_index=self._document.source_index,
            parent=parent,
            name=name,
            value=value,
            target=target,
            annotations=annotations,
            base_uri=base_uri,
            document_uri=self._document.document_uri,
            in_scope_namespaces=in_scope_namespaces,
            namespace_declarations=namespace_declarations,
            path=path,
            prefix=prefix,
            namespace_uri=namespace_uri,
        )
        self._next_id += 1
        return node


_READ_CHUNK = 65536


def _read_source(source: Any, *, max_bytes: int | None = None) -> tuple[bytes | str, str | None]:
    """Read one path, bytes-like object, or readable stream.

    Streams are read until EOF because a readable stream may return short
    chunks.  ``max_bytes`` raises the standard budget error as soon as the
    read exceeds the limit, so a truncated prefix is never returned.  Text
    and binary chunks are preserved and never mixed.
    """
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        with open(path, "rb") as stream:
            return _read_stream(stream.read, max_bytes), path
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source), None
    read = getattr(source, "read", None)
    if read is None:
        raise TypeError("XdmDocument.parse requires a path, bytes, or readable stream")
    name = getattr(source, "name", None)
    natural_name = name if isinstance(name, str) else None
    return _read_stream(read, max_bytes), natural_name


def _read_stream(read: Any, max_bytes: int | None) -> bytes | str:
    """Read a readable stream to EOF, optionally stopping past a byte cap."""
    parts: list[bytes | str] = []
    total = 0
    text: bool | None = None
    while True:
        size = _READ_CHUNK
        if max_bytes is not None:
            size = min(size, max_bytes + 1 - total)
        one_shot = False
        try:
            chunk = read(size)
        except TypeError:
            # A reader that accepts no size argument has no chunking
            # contract; its single return value is the whole source.
            one_shot = True
            chunk = read()
        if isinstance(chunk, str):
            if text is False:
                raise TypeError("an XML source stream mixed str and bytes chunks")
            text = True
        elif isinstance(chunk, (bytes, bytearray, memoryview)):
            if text is True:
                raise TypeError("an XML source stream mixed str and bytes chunks")
            text = False
            chunk = bytes(chunk)
        else:
            raise TypeError("an XML source stream must return str or bytes")
        if not chunk:
            break
        parts.append(chunk)
        total += len(chunk.encode("utf-8")) if isinstance(chunk, str) else len(chunk)
        if max_bytes is not None and total > max_bytes:
            # Never hand a truncated source to the semantic parser: a
            # shortened prefix can still be well-formed (for example after
            # BOM removal or declaration normalization) and would silently
            # become the captured revision.
            raise _budget_error(max_bytes)
        if one_shot:
            break
    if text:
        return "".join(cast(list[str], parts))
    return b"".join(cast(list[bytes], parts))


def _budget_error(max_bytes: int) -> Exception:
    """The standard byte-budget failure, imported lazily to avoid a cycle."""
    from pyxsd.resource_policy import ResourcePolicyError

    return ResourcePolicyError(f"the XML input exceeds max_bytes={max_bytes}")


def _document_uri_from_source(source: Any, natural_name: str | None) -> str | None:
    if isinstance(source, (str, os.PathLike)):
        return Path(os.fspath(source)).resolve().as_uri()
    if natural_name is None or natural_name.startswith("<"):
        return None
    return Path(natural_name).resolve().as_uri()


def _decode_name(raw_name: str) -> _ExpandedName:
    """Decode Expat's namespace-prefixed name spelling into expanded parts."""
    parts = raw_name.split(_NAMESPACE_SEPARATOR)
    if len(parts) == 1:
        return _ExpandedName(None, parts[0], "")
    if len(parts) == 2:
        return _ExpandedName(parts[0], parts[1], "")
    return _ExpandedName(parts[0], parts[1], parts[2])


def _namespace_sort_key(prefix: str) -> tuple[bool, str]:
    return (prefix != "", prefix)


def _apply_xml_base(parent_base: str | None, xml_base: str | None) -> str | None:
    if xml_base is None or not xml_base:
        return parent_base
    if parent_base is None:
        return xml_base
    return urljoin(parent_base, xml_base)


def _resolve_qname(lexical: str, namespaces: Mapping[str, str]) -> str:
    lexical = lexical.strip()
    prefix, separator, local = lexical.partition(":")
    if separator:
        uri = namespaces.get(prefix)
        if uri is None:
            return lexical
        return clark(uri, local)
    uri = namespaces.get("")
    if uri is None:
        return lexical
    return clark(uri, lexical)


def _element_annotations(
    attributes: Mapping[str, str], namespaces: Mapping[str, str]
) -> XdmAnnotation:
    lexical_type = attributes.get(f"{{{XSI_NS}}}type")
    xsi_type = None if lexical_type is None else _resolve_qname(lexical_type, namespaces)
    nil_lexical = attributes.get(f"{{{XSI_NS}}}nil")
    nil_declared = False if nil_lexical is None else nil_lexical.strip() in {"true", "1"}
    return XdmAnnotation(
        _UNTYPED,
        False,
        xsi_type=xsi_type,
        nil_lexical=nil_lexical,
        nil_declared=nil_declared,
    )
