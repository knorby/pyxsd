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
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast
from xml.etree import ElementTree as ET

from pyxsd.namespaces import XSD_NS
from pyxsd.schema_context import SchemaContext, active_context, context_report
from pyxsd.source_spans import SourceIndex
from pyxsd.validation import ValidationReport
from pyxsd.xdm.annotations import (
    UNTYPED,
    UNTYPED_ATOMIC,
    OccurrenceAnnotation,
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
from pyxsd.xdm.values import XdmAtomic

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
_XSD_UNTYPED = f"{{{XSD_NS}}}untyped"
_XSD_UNTYPED_ATOMIC = f"{{{XSD_NS}}}untypedAtomic"
_XSD_ANY_TYPE = f"{{{XSD_NS}}}anyType"

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
        reason: Literal["invalid", "lax-invalid", "unknown", "type"],
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
    _copy_subtree_annotations(source, target)
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
    and is applied after validity assessment.  ``strict`` and ``lax`` use the
    compiled pyxsd schemas in *schema_pool*; ``lax`` validates a known
    descendant declaration when the root itself is undeclared.  ``type_name``
    validates the root against one in-scope type independently of the
    validation mode.

    A pre-captured :class:`~pyxsd.xdm.nodes.XdmDocument` is never reparsed
    unless validation or type assessment requires it; an already-bound
    :class:`pyxsd.document.Document` is consumed through its coherent capture.
    """
    if validation not in _VALIDATION_MODES:
        raise ValueError(f"invalid validation mode {validation!r}")
    if input_type_annotations not in _INPUT_TYPE_ANNOTATIONS:
        raise ValueError(f"invalid input-type-annotations value {input_type_annotations!r}")
    if type_name is not None and not isinstance(type_name, str):
        raise TypeError("a validation type name must be a string or None")
    strip = input_type_annotations == "strip" or validation == "strip"

    if type_name is not None:
        return _type_validated_document(
            source,
            schema_pool=schema_pool,
            type_name=type_name,
            strip=input_type_annotations == "strip",
            document_uri=document_uri,
            base_uri=base_uri,
            source_name=source_name,
        )

    if isinstance(source, XdmDocument):
        if validation in ("strict", "lax"):
            return _validated_document(
                source,
                schema_pool=schema_pool,
                validation=validation,
                strip=strip,
                document_uri=document_uri,
                base_uri=base_uri,
                source_name=source_name,
            )
        if strip:
            return strip_type_annotations(_untyped_copy(source))
        return source

    from pyxsd.document import Document

    if isinstance(source, Document):
        if validation in ("strict", "lax"):
            return _validated_bound_document(
                source,
                schema_pool=schema_pool,
                validation=validation,
                strip=strip,
            )
        document = source.to_xdm()
        if strip:
            return strip_type_annotations(_untyped_copy(document))
        return document

    if validation in ("strict", "lax"):
        return _validated_document(
            source,
            schema_pool=schema_pool,
            validation=validation,
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
    schema_pool: SchemaPool | None,
    validation: str,
    strip: bool,
    document_uri: str | None,
    base_uri: str | None,
    source_name: str | None,
) -> XdmDocument:
    """Validate *source* through one pool schema and return its grounded tree."""
    captured = _capture_source(source)
    untyped = XdmDocument.parse(
        captured.data,
        source_name=captured.source_name,
        document_uri=captured.document_uri,
    )
    root_name = untyped.root_element.name
    schema = schema_pool.schema_for_element(root_name) if schema_pool is not None else None
    if schema is None:
        if validation == "strict":
            raise DocumentValidationError(
                f"no global element declaration for {root_name!r} is in the "
                "in-scope schema definitions",
                reason="unknown",
            )
        target = _grounded_copy(
            captured,
            schema_pool=schema_pool,
            document_uri=document_uri,
            base_uri=base_uri,
            source_name=source_name,
        )
        if schema_pool is not None:
            _lax_validate_known_children(target, schema_pool)
        if strip:
            strip_type_annotations(target)
        return target

    parsed = _parse_payload(schema, captured.data)
    errors = _instance_errors(parsed.report)
    if errors:
        raise DocumentValidationError(
            "the source document is not valid against the imported schema",
            reason="invalid" if validation == "strict" else "lax-invalid",
        )
    target = _grounded_copy(
        captured,
        schema_pool=schema_pool,
        document_uri=document_uri,
        base_uri=base_uri,
        source_name=source_name,
    )
    copy_type_annotations(parsed.to_xdm(), target)
    if strip:
        strip_type_annotations(target)
    return target


def _validated_bound_document(
    source: Any,
    *,
    schema_pool: SchemaPool | None,
    validation: str,
    strip: bool,
) -> XdmDocument:
    """Validate an already-bound Document through its coherent capture."""
    document = source.to_xdm()
    if source.root is None:
        if validation == "strict":
            raise DocumentValidationError(
                "the source document has no global element declaration",
                reason="unknown",
            )
        if schema_pool is not None:
            _lax_validate_known_children(document, schema_pool)
        return strip_type_annotations(_untyped_copy(document)) if strip else document
    errors = _instance_errors(source.report)
    if errors:
        raise DocumentValidationError(
            "the source document is not valid against its schema",
            reason="invalid" if validation == "strict" else "lax-invalid",
        )
    if strip:
        return strip_type_annotations(_untyped_copy(document))
    return document


def _parse_payload(schema: Any, payload: bytes) -> Any:
    return schema.parse(io.BytesIO(payload), capture_xdm=True)


def _capture_source(source: Any) -> CaptureSource:
    if isinstance(source, XdmDocument):
        return CaptureSource.from_content(
            source.source_index.content,
            source_name=source.source_name,
        )
    return CaptureSource.from_input(source)


def _grounded_copy(
    captured: CaptureSource,
    *,
    schema_pool: SchemaPool | None,
    document_uri: str | None,
    base_uri: str | None,
    source_name: str | None,
) -> XdmDocument:
    return XdmDocument.parse(
        captured.data,
        source_name=source_name or captured.source_name,
        document_uri=document_uri if document_uri is not None else captured.document_uri,
        base_uri=base_uri if base_uri is not None else document_uri,
        schema_pool=schema_pool,
    )


def _lax_validate_known_children(document: XdmDocument, schema_pool: SchemaPool) -> None:
    """Validate declared descendants of an undeclared root with lax semantics."""
    stack: list[XdmNode] = [document.root_element]
    while stack:
        node = stack.pop()
        if node.kind is not NodeKind.ELEMENT:
            continue
        schema = schema_pool.schema_for_element(node.name)
        if schema is None:
            stack.extend(reversed(node.children))
            continue
        parsed = schema.parse(element_from_xdm(node), capture_xdm=True)
        if _instance_errors(parsed.report):
            raise DocumentValidationError(
                "the source document is not valid against the imported schema",
                reason="lax-invalid",
            )
        _copy_subtree_annotations(parsed.to_xdm().root_element, node)


def _type_validated_document(
    source: Any,
    *,
    schema_pool: SchemaPool | None,
    type_name: str,
    strip: bool,
    document_uri: str | None,
    base_uri: str | None,
    source_name: str | None,
) -> XdmDocument:
    """Validate the actual root occurrence against the requested type."""
    if type_name == _XSD_UNTYPED:
        return strip_type_annotations(
            _untyped_document(
                source,
                document_uri=document_uri,
                base_uri=base_uri,
                source_name=source_name,
            )
        )
    document = _untyped_document(
        source,
        document_uri=document_uri,
        base_uri=base_uri,
        source_name=source_name,
    )
    root = document.root_element
    if type_name == _XSD_UNTYPED_ATOMIC:
        if any(child.kind is NodeKind.ELEMENT for child in root.children):
            raise DocumentValidationError(
                "xs:untypedAtomic validation rejects element children",
                reason="type",
            )
        root._apply_occurrence_annotation(XdmAnnotation(UNTYPED_ATOMIC, None))
        return document
    if type_name == _XSD_ANY_TYPE:
        if strip:
            strip_type_annotations(document)
        return document
    if schema_pool is None:
        raise DocumentValidationError(
            f"the type {type_name!r} is not in the in-scope schema definitions",
            reason="type",
        )
    adapter = schema_pool.get_type(type_name)
    if adapter is None:
        raise DocumentValidationError(
            f"the type {type_name!r} is not in the in-scope schema definitions",
            reason="type",
        )
    if adapter.is_simple():
        if any(child.kind is NodeKind.ELEMENT for child in root.children):
            raise DocumentValidationError(
                f"the source document is not valid against the type {type_name!r}",
                reason="type",
            )
        if not adapter.is_valid(root.string_value):
            raise DocumentValidationError(
                f"the source document is not valid against the type {type_name!r}",
                reason="type",
            )
        _annotate_simple_root(document, adapter)
        return document
    _validate_complex_occurrence(document, adapter, type_name)
    return document


def _validate_complex_occurrence(
    document: XdmDocument,
    adapter: Any,
    type_name: str,
) -> None:
    """Assess one element occurrence against a complex type's content model."""
    type_class: Any = getattr(adapter, "_type_class", None)
    schema = getattr(adapter.schema, "_schema", None)
    if not isinstance(type_class, type) or schema is None:  # pragma: no cover
        raise DocumentValidationError(
            f"the source document is not valid against the type {type_name!r}",
            reason="type",
        )
    element = element_from_xdm(document.root_element)
    report = ValidationReport()
    context = getattr(schema, "_schema_context", None)
    if context is None:  # pragma: no cover - compiled schemas carry a context
        context = SchemaContext(components=schema.components)
    host = getattr(schema, "_host", None)
    parents = _parent_index(element)
    if host is not None:  # pragma: no cover - compiled schemas carry a host
        host._elementParents = parents
        host._elementTypes = {}
    try:
        with active_context(context), context_report(context, report):
            cast(Any, type_class).makeInstanceFromTag(element)
    finally:
        if host is not None:  # pragma: no cover - compiled schemas carry a host
            host._elementParents.clear()
            host._elementTypes.clear()
    if _instance_errors(report):
        raise DocumentValidationError(
            f"the source document is not valid against the type {type_name!r}",
            reason="type",
        )
    root = document.root_element
    root._apply_occurrence_annotation(
        OccurrenceAnnotation(
            type_name=adapter.name,
            nilled=False,
            validity="valid",
            actual_type=type_class,
            validation_attempted="full",
            typed_values=(),
        )
    )


def _parent_index(element: Any) -> dict[int, Any]:
    parents: dict[int, Any] = {}
    stack = [element]
    while stack:
        parent = stack.pop()
        for child in parent:
            parents[id(child)] = parent
            stack.append(child)
    return parents


def _untyped_document(
    source: Any,
    *,
    document_uri: str | None,
    base_uri: str | None,
    source_name: str | None,
) -> XdmDocument:
    if isinstance(source, XdmDocument):
        return _untyped_copy(source)
    from pyxsd.document import Document

    if isinstance(source, Document):
        return _untyped_copy(source.to_xdm())
    return XdmDocument.parse(
        source,
        document_uri=document_uri,
        base_uri=base_uri,
        source_name=source_name,
    )


def _annotate_simple_root(document: XdmDocument, adapter: Any) -> None:
    root = document.root_element
    value = adapter.cast(root.string_value)
    root._apply_occurrence_annotation(
        OccurrenceAnnotation(
            type_name=adapter.name,
            nilled=False,
            validity="valid",
            actual_type=getattr(adapter, "_type_class", None),
            validation_attempted="full",
            typed_values=(XdmAtomic(value, adapter.name, getattr(adapter, "_type_class", None)),),
        )
    )


def _copy_subtree_annotations(origin: XdmNode, destination: XdmNode) -> None:
    stack: list[tuple[XdmNode, XdmNode]] = [(origin, destination)]
    while stack:
        source_node, target_node = stack.pop()
        if source_node.kind is not target_node.kind or source_node.name != target_node.name:
            raise ValueError("the captured and grounded trees describe different revisions")
        target_node._apply_occurrence_annotation(source_node.annotation)
        source_children = source_node.children
        target_children = target_node.children
        if len(source_children) != len(target_children):
            raise ValueError("the captured and grounded trees describe different revisions")
        source_attributes = source_node.attributes
        target_attributes = target_node.attributes
        if len(source_attributes) != len(target_attributes):
            raise ValueError("the captured and grounded trees describe different revisions")
        for source_attribute, target_attribute in zip(
            source_attributes, target_attributes, strict=True
        ):
            if source_attribute.name != target_attribute.name:
                raise ValueError("the captured and grounded trees describe different revisions")
            target_attribute._apply_occurrence_annotation(source_attribute.annotation)
        stack.extend(zip(source_children, target_children, strict=True))


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
