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
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from xml.etree import ElementTree as ET

from pyxsd.source_spans import SourceIndex
from pyxsd.xdm.annotations import (
    UNTYPED,
    UNTYPED_ATOMIC,
    OccurrenceCapture,
)
from pyxsd.xdm.errors import XdmInputError
from pyxsd.xdm.nodes import (
    NodeKind,
    XdmAnnotation,
    XdmDocument,
    XdmNode,
    _document_uri_from_source,
    _read_source,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xdm.schema import SchemaPool

__all__ = [
    "CaptureSource",
    "CapturedXdmDocument",
    "DocumentValidationError",
    "capture_document",
    "copy_type_annotations",
    "element_from_xdm",
    "parse_typed_document",
    "strip_type_annotations",
]

_VALIDATION_MODES = frozenset({"strict", "lax", "preserve", "strip"})
_INPUT_TYPE_ANNOTATIONS = frozenset({"strip", "preserve", "unspecified"})

_CHANGED_MESSAGE = (
    "the bound tree changed since XDM capture; "
    "parse the document again with capture_xdm=True for a coherent XDM view"
)

_XML_DECLARATION = re.compile(r"\A<\?xml(?=\s).*?\?>", re.DOTALL)
_ENCODING_ATTRIBUTE = re.compile(r"(?i)(\bencoding\s*=\s*)([\"'])[^\"']*\2")


@dataclass(frozen=True, slots=True)
class CaptureSource:
    """The materialized bytes for one captured parse.

    ``data`` is the single revision both the semantic and XDM parsers
    consume.  A text stream is normalized to UTF-8 here: a leading BOM is
    removed and an XML declaration's ``encoding`` pseudo-attribute is
    rewritten to UTF-8, because the text is already decoded and the
    declaration can no longer describe the bytes.  A pre-parsed
    ``Element`` is captured as its ElementTree serialization.
    """

    data: bytes
    source_name: str
    document_uri: str | None

    @classmethod
    def from_input(cls, source: Any, *, max_bytes: int | None = None) -> CaptureSource:
        """Materialize one path, readable stream, byte string, or Element.

        Paths and streams are read to EOF exactly once, so short chunks are
        never mistaken for the whole document: the XDM tree and the bound
        tree are built from the same revision even for a non-seekable
        stream.  ``max_bytes`` raises the standard budget error as soon as
        the read exceeds it, before normalization, so a shortened prefix can
        never become the captured revision.  A reader that rejects a
        ``size`` argument has no chunking contract; its single value is
        accepted or rejected as a whole.
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
                data=data,
                source_name="<memory>",
                document_uri=None,
            )
        content, natural_name = _read_source(source, max_bytes=max_bytes)
        data = _normalize_text_source(content) if isinstance(content, str) else bytes(content)
        return cls(
            data=data,
            source_name=natural_name or "<memory>",
            document_uri=_document_uri_from_source(source, natural_name),
        )

    @classmethod
    def from_content(cls, content: bytes | str, *, source_name: str) -> CaptureSource:
        """Capture already-admitted bytes, such as a resource policy's data."""
        data = _normalize_text_source(content) if isinstance(content, str) else bytes(content)
        return cls(
            data=data,
            source_name=source_name,
            document_uri=None,
        )

    @property
    def digest(self) -> str:
        """The SHA-256 of the captured byte revision."""
        return hashlib.sha256(self.data).hexdigest()


def _normalize_text_source(text: str) -> bytes:
    """Encode already-decoded XML text so its declaration describes its bytes."""
    if text.startswith("\ufeff"):
        text = text[1:]
    match = _XML_DECLARATION.match(text)
    if match is not None:
        declaration = match.group(0)
        normalized = _ENCODING_ATTRIBUTE.sub('encoding="UTF-8"', declaration, count=1)
        text = normalized + text[match.end() :]
    return text.encode("utf-8")


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
        source.data,
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


# ---------------------------------------------------------------------------
# Schema-aware source input


class DocumentValidationError(XdmInputError):
    """A schema-aware source input failed validity assessment.

    ``reason`` distinguishes the XSLT error arms without teaching this layer
    about XSLT codes: ``"invalid"`` is a validity failure, ``"unknown"`` is a
    missing global declaration, and ``"type"`` is a failure against an
    explicitly requested type.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: Literal["invalid", "unknown", "type"],
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        if cause is not None:
            self.__cause__ = cause


def strip_type_annotations(document: XdmDocument) -> XdmDocument:
    """Replace every element/attribute annotation with the untyped default.

    The source tree is otherwise unchanged: names, values, namespaces, and
    node identity stay exactly as captured.  Stripping is what XSLT's
    ``input-type-annotations="strip"`` asks for.
    """
    if not isinstance(document, XdmDocument):
        raise TypeError("type annotations can only be stripped from an XdmDocument")
    stack: list[XdmNode] = [document]
    while stack:
        node = stack.pop()
        if node.kind is NodeKind.ELEMENT:
            node._apply_occurrence_annotation(XdmAnnotation(UNTYPED, False))
        elif node.kind is NodeKind.ATTRIBUTE:
            node._apply_occurrence_annotation(XdmAnnotation(UNTYPED_ATOMIC, None))
        stack.extend(reversed(node.children))
        if node.kind is NodeKind.ELEMENT:
            stack.extend(node.attributes)
    return document


def copy_type_annotations(source: XdmDocument, target: XdmDocument) -> XdmDocument:
    """Copy occurrence annotations between two trees of the same revision.

    Both trees must have identical element/attribute structure; the walk is
    positional and stops with :class:`ValueError` when it does not.  This is
    the bridge between the binder's captured tree and a grounded tree built
    with a document URI, so neither serialization nor a second schema run is
    needed.
    """
    if not isinstance(source, XdmDocument) or not isinstance(target, XdmDocument):
        raise TypeError("annotation copying requires two XdmDocument trees")
    source_stack: list[XdmNode] = [source]
    target_stack: list[XdmNode] = [target]
    while source_stack:
        if not target_stack:  # pragma: no cover - structure guard
            raise ValueError("the target tree ended before the captured tree")
        origin = source_stack.pop()
        destination = target_stack.pop()
        if origin.kind is not destination.kind or origin.name != destination.name:
            raise ValueError("the captured and grounded trees describe different revisions")
        destination._apply_occurrence_annotation(origin.annotation)
        origin_children = origin.children
        target_children = destination.children
        if len(origin_children) != len(target_children):
            raise ValueError("the captured and grounded trees describe different revisions")
        origin_attributes = origin.attributes
        target_attributes = destination.attributes
        if len(origin_attributes) != len(target_attributes):
            raise ValueError("the captured and grounded trees describe different revisions")
        for origin_attribute, target_attribute in zip(
            origin_attributes, target_attributes, strict=True
        ):
            if origin_attribute.name != target_attribute.name:
                raise ValueError("the captured and grounded trees describe different revisions")
            target_attribute._apply_occurrence_annotation(origin_attribute.annotation)
        source_stack.extend(reversed(origin_children))
        target_stack.extend(reversed(target_children))
    if target_stack:  # pragma: no cover - structure guard
        raise ValueError("the target tree extends beyond the captured tree")
    return target


def parse_typed_document(
    source: Any,
    *,
    schema_pool: SchemaPool | None = None,
    validation: str = "strip",
    input_type_annotations: str = "unspecified",
    type_name: str | None = None,
    document_uri: str | None = None,
    base_uri: str | None = None,
    source_name: str | None = None,
) -> XdmDocument:
    """Parse one source document under an explicit validation mode.

    ``validation`` is ``strict``, ``lax``, ``preserve``, or ``strip``.
    ``input_type_annotations`` is ``strip``, ``preserve``, or ``unspecified``
    and is applied after validity assessment.  A ``strict`` or ``lax`` mode
    validates through the compiled pyxsd schemas in *schema_pool*; a document
    whose root matches no global declaration is untyped under ``lax`` and an
    error under ``strict``.  ``type_name`` validates the root against one
    in-scope type after binding.

    A pre-captured :class:`~pyxsd.xdm.nodes.XdmDocument` is never reparsed:
    its annotations are preserved or stripped as requested.  An already-bound
    :class:`pyxsd.document.Document` is consumed through its coherent capture.
    """
    if validation not in _VALIDATION_MODES:
        raise ValueError(f"invalid validation mode {validation!r}")
    if input_type_annotations not in _INPUT_TYPE_ANNOTATIONS:
        raise ValueError(f"invalid input-type-annotations value {input_type_annotations!r}")
    if type_name is not None and not isinstance(type_name, str):
        raise TypeError("a validation type name must be a string or None")
    strip = input_type_annotations == "strip" or validation == "strip"

    if isinstance(source, XdmDocument):
        if validation in ("strict", "lax") and schema_pool is not None:
            _require_annotated(source, validation)
        if strip:
            return strip_type_annotations(_untyped_copy(source))
        return source

    from pyxsd.document import Document

    if isinstance(source, Document):
        if validation == "strict" and _instance_errors(source.report):
            raise DocumentValidationError(
                "the source document is not valid against the imported schema",
                reason="invalid",
            )
        if type_name is not None and schema_pool is not None:
            _validate_named_type(source.root, type_name, schema_pool)
        document = source.to_xdm()
        if strip:
            return strip_type_annotations(_untyped_copy(document))
        return document

    if validation in ("strict", "lax") and schema_pool is not None:
        return _validated_document(
            source,
            schema_pool=schema_pool,
            validation=validation,
            type_name=type_name,
            strip=strip,
            document_uri=document_uri,
            base_uri=base_uri,
            source_name=source_name,
        )

    document = XdmDocument.parse(
        source,
        schema_pool=schema_pool,
        document_uri=document_uri,
        base_uri=base_uri,
        source_name=source_name,
    )
    if strip:
        strip_type_annotations(document)
    return document


def _validated_document(
    source: Any,
    *,
    schema_pool: SchemaPool,
    validation: str,
    type_name: str | None,
    strip: bool,
    document_uri: str | None,
    base_uri: str | None,
    source_name: str | None,
) -> XdmDocument:
    """Validate *source* through one pool schema and return its grounded tree."""
    captured = CaptureSource.from_input(source)
    untyped = XdmDocument.parse(
        captured.data,
        source_name=captured.source_name,
        document_uri=captured.document_uri,
    )
    root_name = untyped.root_element.name
    schema = schema_pool.schema_for_element(root_name)
    if schema is None and type_name is not None:
        schema = schema_pool.schema_for_type(type_name)
    if schema is None:
        if validation == "strict":
            raise DocumentValidationError(
                f"no global element declaration for {root_name!r} is in the "
                "in-scope schema definitions",
                reason="unknown",
            )
        return untyped if not strip else strip_type_annotations(untyped)

    parsed = schema.parse(captured.data, capture_xdm=True)
    errors = _instance_errors(parsed.report)
    if errors and validation == "strict":
        if type_name is not None:
            raise DocumentValidationError(
                f"the source document is not valid against the type {type_name!r}",
                reason="type",
            )
        raise DocumentValidationError(
            "the source document is not valid against the imported schema",
            reason="invalid",
        )
    if type_name is not None:
        _validate_named_type(parsed.root, type_name, schema_pool)

    target = XdmDocument.parse(
        captured.data,
        source_name=source_name or captured.source_name,
        document_uri=document_uri if document_uri is not None else captured.document_uri,
        base_uri=base_uri if base_uri is not None else document_uri,
        schema_pool=schema_pool,
    )
    copy_type_annotations(parsed.to_xdm(), target)
    if strip:
        strip_type_annotations(target)
    return target


def _untyped_copy(document: XdmDocument) -> XdmDocument:
    """Parse a fresh untyped tree from the same original instance bytes.

    The caller's captured document is never mutated: two transformations of
    one ``Document`` must be able to use different annotation modes.
    """
    index = document.source_index
    return XdmDocument.parse(
        index.content,
        source_name=document.source_name,
        document_uri=document.document_uri,
        base_uri=document.base_uri,
        schema_pool=document.schema_pool,
    )


def _require_annotated(document: XdmDocument, validation: str) -> None:
    """Reject a strict request for an untyped captured tree."""
    root = document.root_element
    if validation == "strict" and root.annotation.governing_declaration is None:
        raise DocumentValidationError(
            "the source document carries no schema validity assessment",
            reason="unknown",
        )


def _validate_named_type(bound: Any, type_name: str, schema_pool: SchemaPool) -> None:
    adapter = schema_pool.get_type(type_name)
    if adapter is None:
        raise DocumentValidationError(
            f"the type {type_name!r} is not in the in-scope schema definitions",
            reason="type",
        )
    if bound is None:
        raise DocumentValidationError(
            "the source document has no root to validate against the requested type",
            reason="type",
        )
    value: Any = bound
    if adapter.is_simple() and not isinstance(bound, str):
        value = str(bound)
    if not adapter.is_valid(value):
        raise DocumentValidationError(
            f"the source document is not valid against the type {type_name!r}",
            reason="type",
        )


def _instance_errors(report: Any) -> tuple[Any, ...]:
    from pyxsd.validation import IssueSeverity

    return tuple(
        issue
        for issue in report
        if issue.phase == "instance" and issue.severity is IssueSeverity.ERROR
    )


def element_from_xdm(
    node: XdmNode,
    namespace_context: Any = None,
) -> ET.Element:
    """Convert one XDM element subtree into an ElementTree element.

    The conversion is structural, not serialized text: names, attributes,
    element children, and character data are copied directly.  Comments and
    processing instructions are outside an XML Schema document's content
    model and are omitted.  When *namespace_context* is supplied, each
    element's in-scope bindings are recorded there so QName-valued schema
    attributes resolve exactly as they do for a parsed schema document.
    """
    if not isinstance(node, XdmNode) or node.kind is not NodeKind.ELEMENT:
        raise TypeError("a schema element conversion requires an element node")
    element = ET.Element(
        node.name or "",
        {attribute.name or "": attribute.string_value for attribute in node.attributes},
    )
    if namespace_context is not None:
        namespace_context.record(element, dict(node.in_scope_namespaces))
    pending: list[str] = []
    last: ET.Element | None = None
    for child in node.children:
        if child.kind is NodeKind.TEXT:
            pending.append(child.string_value)
            continue
        if child.kind is not NodeKind.ELEMENT:
            continue
        if last is None:
            element.text = "".join(pending)
        else:
            last.tail = "".join(pending)
        pending.clear()
        child_element = element_from_xdm(child, namespace_context)
        element.append(child_element)
        last = child_element
    if last is None:
        element.text = "".join(pending)
    else:
        last.tail = "".join(pending)
    return element
