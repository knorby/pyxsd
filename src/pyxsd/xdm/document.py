"""Opt-in coherent XDM capture for ordinary schema-bound documents.

``Schema.parse(..., capture_xdm=True)`` uses this module to retain three
things from one parse: the source-complete XDM tree, the occurrence
annotations produced by the same binding run, and an immutable snapshot of
the bound instance tree.  ``Document.to_xdm()`` exposes the XDM tree only
while the bound tree still matches that snapshot, so a caller that edits
attributes, text, tails, child lists, or replaces an object is told to
reparse instead of receiving a view of XML the bound tree no longer
represents.

Capture is deliberately opt-in.  Ordinary parsing never imports this module,
never builds a second tree, and never retains bound-tree bookkeeping.  The
module is independent of stylesheet execution and does not stream.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from io import StringIO
from typing import Any
from xml.etree import ElementTree as ET

from pyxsd.source_spans import SourceIndex
from pyxsd.xdm.annotations import OccurrenceCapture
from pyxsd.xdm.errors import XdmInputError
from pyxsd.xdm.nodes import XdmDocument, _document_uri_from_source, _read_source

__all__ = [
    "CaptureSource",
    "CapturedXdmDocument",
    "capture_document",
]

_CHANGED_MESSAGE = (
    "the bound tree changed since XDM capture; "
    "parse the document again with capture_xdm=True for a coherent XDM view"
)


@dataclass(frozen=True, slots=True)
class CaptureSource:
    """The materialized source bytes for one captured parse.

    ``content`` is what the semantic XDM parser consumes (``str`` for text
    streams, ``bytes`` otherwise), while ``data`` is the UTF-8/byte
    serialization used for the source index and its digest.  A text stream
    is therefore digested in one canonical form even though Expat parses
    the text object directly.
    """

    content: bytes | str
    data: bytes
    source_name: str
    document_uri: str | None

    @classmethod
    def from_input(cls, source: Any) -> CaptureSource:
        """Materialize one path, readable stream, byte string, or Element.

        Paths and streams are read exactly once, so the XDM tree and the
        bound tree are built from the same revision even for a non-seekable
        stream.  A pre-parsed ``Element`` has no lexical source left, so its
        ElementTree serialization is the captured revision.
        """
        if isinstance(source, ET.Element):
            captured = source
            if getattr(source, "tail", None):
                # A root element's tail is outside the document; serializing
                # it would produce junk after the document element.  A
                # shallow copy excludes it without touching the caller's
                # element (nodes are shared, only the tail attribute differs).
                captured = copy.copy(source)
                captured.tail = None
            data = ET.tostring(captured, encoding="utf-8")
            return cls(
                content=data,
                data=data,
                source_name="<memory>",
                document_uri=None,
            )
        content, natural_name = _read_source(source)
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return cls(
            content=content,
            data=data,
            source_name=natural_name or "<memory>",
            document_uri=_document_uri_from_source(source, natural_name),
        )

    @classmethod
    def from_content(cls, content: bytes | str, *, source_name: str) -> CaptureSource:
        """Capture already-admitted bytes, such as a resource policy's data."""
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return cls(
            content=content,
            data=data,
            source_name=source_name,
            document_uri=None,
        )

    @property
    def digest(self) -> str:
        """The SHA-256 of the captured byte revision."""
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True, slots=True)
class _NodeState:
    """One bound node's observable fields at capture time."""

    identity: int
    name: Any
    attributes: tuple[tuple[Any, Any], ...]
    value: Any
    tail: Any
    children: tuple[int, ...]


class _InvalidBoundTree(Exception):
    """The current bound tree cannot be compared with the snapshot."""


@dataclass(frozen=True, slots=True)
class CapturedXdmDocument:
    """An immutable captured XDM tree plus its bound-tree snapshot."""

    xdm: XdmDocument
    root: Any
    signature: tuple[_NodeState, ...]
    #: Strong references keep captured node identities from being reused by
    #: a later allocation after a caller removes a subtree.
    nodes: tuple[Any, ...]

    def validated_xdm(self) -> XdmDocument:
        """Return the captured tree if the bound tree is still unchanged."""
        try:
            current, _ = _bound_snapshot(self.root)
            coherent = current == self.signature
        except (_InvalidBoundTree, TypeError, ValueError) as exc:
            raise XdmInputError(_CHANGED_MESSAGE) from exc
        if not coherent:
            raise XdmInputError(_CHANGED_MESSAGE)
        return self.xdm


def capture_document(
    source: CaptureSource,
    root: Any,
    *,
    annotations: OccurrenceCapture,
) -> CapturedXdmDocument:
    """Build the XDM tree and bound-tree snapshot for one parse.

    The source index is built from the same bytes the XDM parser consumes,
    so source spans and the capture digest always describe this revision.
    Occurrence annotations are attached before the snapshot is taken; a
    later bound-tree edit cannot alter them.
    """
    index = SourceIndex(source.source_name, source.data)
    xdm = XdmDocument.parse(
        StringIO(source.content) if isinstance(source.content, str) else source.content,
        source_index=index,
        source_name=source.source_name,
        document_uri=source.document_uri,
    )
    annotations.apply(xdm)
    signature, nodes = _bound_snapshot(root)
    return CapturedXdmDocument(
        xdm=xdm,
        root=root,
        signature=signature,
        nodes=nodes,
    )


def _bound_snapshot(root: Any) -> tuple[tuple[_NodeState, ...], tuple[Any, ...]]:
    """Return a pre-order snapshot of the bound tree's observable fields.

    Identity is part of the signature, so replacing a node object with an
    equal-looking one is rejected.  The returned node tuple keeps every
    captured object alive, which also makes the recorded ``id`` values
    unique for the lifetime of the capture.
    """
    states: list[_NodeState] = []
    nodes: list[Any] = []
    seen: set[int] = set()
    stack = [] if root is None else [root]
    while stack:
        node = stack.pop()
        identity = id(node)
        if identity in seen:
            raise _InvalidBoundTree("the bound tree contains a cycle")
        seen.add(identity)

        attributes = getattr(node, "_attribs_", {})
        if not isinstance(attributes, Mapping):
            raise _InvalidBoundTree("the bound node attribute container is not a mapping")
        try:
            attribute_items = tuple(attributes.items())
            children = tuple(getattr(node, "_children_", ()))
        except (TypeError, ValueError) as exc:
            raise _InvalidBoundTree("the bound node container cannot be walked") from exc

        value = getattr(node, "_value_", None)
        if isinstance(value, (list, tuple)):
            value = tuple(value)

        states.append(
            _NodeState(
                identity=identity,
                name=getattr(node, "_name_", None),
                attributes=attribute_items,
                value=value,
                tail=getattr(node, "_tail_", None),
                children=tuple(id(child) for child in children),
            )
        )
        nodes.append(node)
        stack.extend(reversed(children))
    return tuple(states), tuple(nodes)
