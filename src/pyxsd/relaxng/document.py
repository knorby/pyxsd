"""Validation-only RELAX NG documents over one authoritative lexical tree.

A :class:`RelaxNGDocument` pairs a compiled :class:`RelaxNGSchema` with the
lexical occurrence tree a parse produced and one fresh report: frozen
schema-phase findings followed by this parse's instance findings.  Phase 2
exposes validation-only parsing, so the document records ``typed=False`` and
has no pattern annotations or typed projections.  Methods that would require
those Phase 3 capabilities fail explicitly instead of reaching an XSD
converter or binder.

The document shares the generic traversal, XPath, writer, XDM, and transform
behavior with XSD documents.  Schema-specific behavior is narrow: a
root-callable transform returning a lexical node yields a new document with a
stale report snapshot, and :meth:`revalidate` parses an independent clone of
the current tree into fresh nodes and evidence while keeping the recorded
namespace and base-URI frames.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from types import MappingProxyType
from typing import IO, TYPE_CHECKING, Any, NoReturn

from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, NodeLookup, PathStep
from pyxsd.relaxng.tree import NodeContext, RelaxNGElement
from pyxsd.relaxng.xml_writer import clone_relaxng, serialize_relaxng
from pyxsd.resource_policy import source_identifier

if TYPE_CHECKING:
    from pyxsd.data_conversion.contracts import Fidelity, FormatName
    from pyxsd.dtd_declarations import DocumentDeclarations
    from pyxsd.relaxng.limits import RelaxNGLimits
    from pyxsd.relaxng.schema import RelaxNGSchema
    from pyxsd.validation import ValidationReport

__all__ = ["RelaxNGDocument"]

_ANNOTATION_CAPABILITY = (
    "RELAX NG pattern capture is unavailable in this release; "
    "validation-only documents expose no pattern annotations"
)
_DATA_CONVERSION_CAPABILITY = (
    "RELAX NG data conversion is unavailable in this release; "
    "Phase 5 owns to_dict, to_data, and to_json export"
)
_TYPED_DOCUMENT_CAPABILITY = (
    "RELAX NG typed documents are not supported in this release; "
    "pass typed=False for validation-only parsing"
)
_CAPTURE_CAPABILITY = "capture must be 'none' for validation-only RELAX NG documents"

_AttributeAddress = tuple[str | None, tuple[PathStep, ...]]
_AttributeSnapshot = Mapping[_AttributeAddress, Mapping[str, str]]


class _RevalidationSource(StringIO):
    """Serialized current tree named for the original caller's source."""

    def __init__(self, text: str, name: str | None) -> None:
        super().__init__(text)
        self.name = name


class RelaxNGDocument(Document):
    """One RELAX NG parse revision with its fresh merged report.

    ``typed`` is false for every Phase 2 document.  ``interpretations_current``
    reports whether the recorded validation snapshot still belongs to the
    current, unedited lexical revision.  ``root``, ``report``, and ``source``
    keep the lexical tree, the merged schema/instance report, and the original
    caller source respectively.  Direct construction preserves the Phase 2
    invariants instead of accepting a typed or capture-bearing document.
    """

    schema: RelaxNGSchema  # type: ignore[assignment]
    root: RelaxNGElement
    typed: bool

    def __init__(
        self,
        schema: RelaxNGSchema,
        root: RelaxNGElement,
        source: Any,
        report: ValidationReport,
        *,
        typed: bool,
        capture: str = "none",
        limits: RelaxNGLimits | None = None,
        namespace_context: Any = None,
        declarations: DocumentDeclarations | None = None,
        xdm_capture: Any = None,
        interpretations_current: bool = True,
        attribute_snapshot: _AttributeSnapshot | None = None,
    ) -> None:
        if typed is not False:
            raise PyXSDError(_TYPED_DOCUMENT_CAPABILITY)
        if capture != "none":
            raise ValueError(_CAPTURE_CAPABILITY)
        super().__init__(
            schema=schema,  # type: ignore[arg-type]
            root=root,
            source=source,
            report=report,
            declarations=declarations,
            xdm_capture=xdm_capture,
        )
        self.typed = typed
        self._capture = capture
        self._limits = limits
        self._namespace_context = namespace_context
        self._revision_id = root.revision.revision_id
        self._interpretations_current = interpretations_current
        self._attribute_snapshot = (
            _snapshot_original_attributes(root)
            if attribute_snapshot is None
            else attribute_snapshot
        )

    @property
    def interpretations_current(self) -> bool:
        """Whether the validation snapshot still describes the current revision."""
        revision = self.root.revision
        return (
            self._interpretations_current
            and revision.revision_id == self._revision_id
            and not revision.dirty
        )

    @property
    def annotations(self) -> NoReturn:
        """Reject annotation access; pattern capture is absent in this release."""
        raise PyXSDError(_ANNOTATION_CAPABILITY)

    def to_dict(self, *, typed: bool = True, always_list: bool = False) -> NoReturn:
        """Reject data conversion; Phase 5 owns the RELAX NG adapter."""
        del typed, always_list
        raise PyXSDError(_DATA_CONVERSION_CAPABILITY)

    def to_data(
        self,
        *,
        format: FormatName = "pyxsd",
        fidelity: Fidelity = "best-effort",
        typed: bool = True,
        always_list: bool = False,
    ) -> NoReturn:
        """Reject data conversion; Phase 5 owns the RELAX NG adapter."""
        del format, fidelity, typed, always_list
        raise PyXSDError(_DATA_CONVERSION_CAPABILITY)

    def to_json(
        self,
        *,
        indent: int | None = None,
        format: FormatName = "pyxsd",
        fidelity: Fidelity = "best-effort",
        typed: bool = True,
        always_list: bool = False,
    ) -> NoReturn:
        """Reject JSON conversion; Phase 5 owns the RELAX NG adapter."""
        del indent, format, fidelity, typed, always_list
        raise PyXSDError(_DATA_CONVERSION_CAPABILITY)

    def to_string(self) -> str:
        """Serialize the lexical tree without XSD writer behavior or mutation."""
        return serialize_relaxng(self.root)

    def write(self, dest: str | Path | os.PathLike[str] | IO[str]) -> None:
        """Write the exact lexical serialization to a path or caller file object.

        Paths are opened and closed here with UTF-8 encoding.  Caller file
        objects are written and flushed but left open, matching the existing
        path-versus-file ownership convention.
        """
        text = self.to_string()
        if isinstance(dest, (str, os.PathLike)):
            with open(dest, "w", encoding="utf-8") as stream:
                stream.write(text)
            return
        dest.write(text)
        dest.flush()

    def _document_for_node_result(self, root: Any) -> Any:
        """Wrap a lexical node result as a stale RELAX NG document.

        A root callable may return the current root, a node from this
        revision, or any node produced by a transform.  The returned
        document keeps this revision's typed/capture, instance-limit, and
        namespace settings plus the old report snapshot.  Its
        ``interpretations_current`` is false until :meth:`revalidate`.
        An unchanged root keeps the coherent XDM capture; any other root
        starts without one.  Non-lexical results pass through unchanged.
        """
        if not isinstance(root, RelaxNGElement):
            return root
        return RelaxNGDocument(
            schema=self.schema,
            root=root,
            source=self.source,
            report=self._report,
            typed=self.typed,
            capture=self._capture,
            limits=self._limits,
            namespace_context=self._namespace_context,
            declarations=self._declarations,
            xdm_capture=self._xdm_capture if root is self.root else None,
            interpretations_current=False,
            attribute_snapshot=self._attribute_snapshot,
        )

    def revalidate(self) -> RelaxNGDocument:
        """Validate an independent clone of the current tree.

        The lexical tree is cloned into a fresh revision, serialized with the
        schema-neutral writer, and parsed against the same schema with this
        document's typed, capture, XDM, and instance-limit settings.  The
        result has fresh nodes, a fresh report, and a new revision ID; this
        document, its nodes, and its report snapshot are never refreshed.

        Serialization emits the recorded namespace declarations so prefixed
        QName values keep their external namespace context.  The recorded
        namespace and effective base-URI frames are then restored onto the
        repared nodes, because a bare string round trip cannot carry a
        source-relative base URI.  A root tail is outside the document
        element and is not carried into the new document.

        The original DTD declaration evidence is reused without retrieving a
        DTD or serializing a DOCTYPE.  Absent evidence stays absent.  Source
        addresses and XDM capture use the original caller's source identifier,
        including a named stream's name; anonymous inputs stay anonymous.
        With a trusted resource policy, an original path read may have supplied
        a different canonical identifier.  Revalidation passes a named stream
        through the policy parser, not its path reader, so that reader's
        canonical identifier is not restored.
        """
        cloned = clone_relaxng(self.root)
        again = self.schema._parse_guarded_instance(
            _RevalidationSource(serialize_relaxng(cloned), source_identifier(self.source)),
            xml_policy=None,
            xml_limits=None,
            capture=self._capture,
            capture_xdm=self._xdm_capture is not None,
            limits=self._limits,
            declarations=self._declarations,
        )
        again.source = self.source
        _restore_lexical_frames(cloned, again.root)
        return again

    def locate(self, location: IssueLocation) -> NodeLookup:
        """Resolve one parse-specific occurrence to its lexical node.

        RELAX NG parsing records an exact source address for every lexical
        node instead of building an XSD binding provenance table.  This
        lookup compares those recorded addresses; it never matches by
        attribute value, local name, or position in another revision.  An
        attribute address returns the parse-time lexical attribute value from
        this document's immutable snapshot, so later edits neither change nor
        create old addresses.  An address from another parse is ``missing``.
        """
        revision = self.root.revision
        stack = [self.root]
        while stack:
            node = stack.pop()
            recorded = revision.source_location_for(node)
            if (
                recorded is not None
                and recorded.source == location.source
                and recorded.path == location.path
            ):
                if location.attribute is None:
                    return NodeLookup(location, node, "bound")
                attributes = self._attribute_snapshot.get((recorded.source, recorded.path), {})
                value = attributes.get(location.attribute)
                if value is None:
                    return NodeLookup(location, None, "missing")
                return NodeLookup(location, value, "attribute")
            stack.extend(reversed(node._children_))
        return NodeLookup(location, None, "missing")


def _snapshot_original_attributes(root: RelaxNGElement) -> _AttributeSnapshot:
    """Freeze each addressed node's parse-time lexical attributes.

    The snapshot is keyed by the exact source occurrence the parse recorded,
    so an edited tree cannot change or add old addresses.  A source-less tree
    has no addresses and produces an empty snapshot; ``locate`` then reports
    every attribute address as missing, matching its element behavior.
    """
    revision = root.revision
    snapshot: dict[_AttributeAddress, Mapping[str, str]] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        location = revision.source_location_for(node)
        if location is not None:
            snapshot[(location.source, location.path)] = MappingProxyType(dict(node._attribs_))
        stack.extend(reversed(node._children_))
    return MappingProxyType(snapshot)


def _restore_lexical_frames(source: RelaxNGElement, target: RelaxNGElement) -> None:
    """Copy recorded namespace/base frames from *source* onto *target* nodes.

    Both trees must have the same element shape, which the lexical writer and
    parser guarantee for an unedited clone.  Source occurrences stay those of
    the fresh parse: diagnostics and ``locate`` addresses belong to the new
    revision, while namespace and base-URI frames are lexical context the
    serialized text cannot fully express.
    """
    revision = target.revision
    stack: list[tuple[RelaxNGElement, RelaxNGElement]] = [(source, target)]
    while stack:
        source_node, target_node = stack.pop()
        source_context = source_node.revision.context_for(source_node)
        target_context = revision.context_for(target_node)
        revision._replace_context(
            target_node,
            NodeContext(
                source_context.namespaces,
                source_context.base_uri,
                target_context.source,
            ),
        )
        if len(source_node._children_) != len(target_node._children_):
            raise PyXSDError("RELAX NG revalidation changed the lexical tree shape")
        stack.extend(zip(source_node._children_, target_node._children_, strict=True))
