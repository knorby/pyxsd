"""RELAX NG documents over one authoritative lexical tree.

A :class:`RelaxNGDocument` pairs a compiled :class:`RelaxNGSchema` with the
lexical occurrence tree a parse produced and one fresh report: frozen
schema-phase findings followed by this parse's instance findings.  A typed,
valid document also owns one immutable
:class:`~pyxsd.relaxng.evidence.EvidenceGraph`; the generic nodes expose
``scalar_value()`` and ``attribute_value()`` through it.  A parse requested
with ``capture="patterns"`` additionally owns one immutable, parse-scoped
:class:`~pyxsd.relaxng.annotations.AnnotationTable`; its records may be a sound
subset when the independent capture budget is exhausted.  Validation-only
documents compute no evidence and reject typed access explicitly.  Invalid
typed documents carry no authoritative evidence and reject typed access with
their report.

The document shares the generic traversal, XPath, writer, XDM, and transform
behavior with XSD documents.  Schema-specific behavior is narrow: a
root-callable transform returning a lexical node yields a new document with a
stale report snapshot and the same evidence, and :meth:`revalidate` parses an
independent clone of the current tree into fresh nodes and evidence while
keeping the recorded namespace and base-URI frames.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from copy import copy
from io import StringIO
from pathlib import Path
from types import MappingProxyType
from typing import IO, TYPE_CHECKING, Any, NoReturn, cast

from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.locations import IssueLocation, NodeLookup, PathStep
from pyxsd.relaxng.annotations import (
    AnnotationCapture,
    AnnotationTable,
    typed_value_at_site,
)
from pyxsd.relaxng.contracts import ScalarContract
from pyxsd.relaxng.errors import StaleInterpretationError
from pyxsd.relaxng.evidence import EvidenceGraph, EvidenceOccurrence
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

_ANNOTATION_VALIDATION_ONLY = (
    "RELAX NG pattern capture is unavailable for a validation-only document; "
    "reparse with typed=True, capture='patterns'"
)
_ANNOTATION_NOT_CAPTURED = (
    "RELAX NG pattern annotations were not captured for this parse; reparse with capture='patterns'"
)
_DATA_CONVERSION_CAPABILITY = (
    "RELAX NG data conversion is unavailable in this release; "
    "Phase 5 owns to_dict, to_data, and to_json export"
)
_TYPED_EVIDENCE_CAPABILITY = "a valid typed RELAX NG document requires complete binding evidence"
_TYPED_ANNOTATION_CAPABILITY = (
    "a valid typed RELAX NG document parsed with capture='patterns' requires "
    "a pattern annotation table"
)
_CAPTURE_CAPABILITY = "capture='patterns' requires a typed RELAX NG document"

_AttributeAddress = tuple[str | None, tuple[PathStep, ...]]
_AttributeSnapshot = Mapping[_AttributeAddress, Mapping[str, str]]


class _RevalidationSource(StringIO):
    """Serialized current tree named for the original caller's source."""

    def __init__(self, text: str, name: str | None) -> None:
        super().__init__(text)
        self.name = name


def _require_current_revision(node: RelaxNGElement, revision_id: int, current: bool) -> None:
    """Reject a node whose revision is edited or no longer the snapshot's."""
    revision = node.revision
    if not current or revision.revision_id != revision_id or revision.dirty:
        raise StaleInterpretationError(
            "the RELAX NG typed interpretation is stale; call revalidate() before typed reads"
        )


def _raise_invalid_document(report: ValidationReport) -> NoReturn:
    """Reject typed reads from an invalid document with its report."""
    raise ValidationError(
        "the RELAX NG document is invalid; typed scalar access requires a valid document",
        report,
    )


class _TypedSnapshotState:
    """Revision-owned typed snapshot state, freshness, and conflict identity.

    One lexical revision owns at most one snapshot.  A snapshot records the
    evidence graph and report it was built from so a second document can be
    accepted only when it carries the identical objects, as the stale
    transform path does.  A transform marks the shared snapshot stale; a
    replacement snapshot is never installed over existing evidence.
    """

    __slots__ = ("_current", "_evidence", "_report", "_revision_id")

    def __init__(
        self,
        *,
        evidence: EvidenceGraph | None,
        report: ValidationReport,
        revision_id: int,
        current: bool = True,
    ) -> None:
        self._evidence = evidence
        self._report = report
        self._revision_id = revision_id
        self._current = current

    @property
    def current(self) -> bool:
        """Whether typed reads through this snapshot are still authoritative."""
        return self._current

    def mark_stale(self) -> None:
        """Conservatively make every read through this snapshot stale."""
        self._current = False

    def owns(
        self,
        *,
        evidence: EvidenceGraph | None,
        report: ValidationReport,
        revision_id: int,
    ) -> bool:
        """Whether this is the identical snapshot a document carries."""
        return (
            self._evidence is evidence
            and self._report is report
            and self._revision_id == revision_id
        )


class _InvalidTypedInterpretation(_TypedSnapshotState):
    """Typed access for a parsed document whose instance report has errors."""

    __slots__ = ()

    def __init__(self, *, report: ValidationReport, revision_id: int, current: bool = True) -> None:
        super().__init__(evidence=None, report=report, revision_id=revision_id, current=current)

    def scalar_value(self, node: RelaxNGElement) -> Any:
        """Reject invalid-document scalar access after the freshness gate."""
        _require_current_revision(node, self._revision_id, self._current)
        _raise_invalid_document(self._report)

    def attribute_value(self, node: RelaxNGElement, expanded_name: str) -> Any:
        """Reject invalid-document attribute access after the freshness gate."""
        del expanded_name
        _require_current_revision(node, self._revision_id, self._current)
        _raise_invalid_document(self._report)


class _TypedInterpretation(_TypedSnapshotState):
    """Typed scalar access over one immutable, valid evidence snapshot.

    The snapshot is attached to its lexical revision.  Freshness is measured
    against that revision: any successful lexical edit dirtying the whole
    revision or a transform marking the snapshot stale makes reads fail.  A
    document transform may share the revision and this snapshot, while its
    own ``interpretations_current`` flag records that the transform result is
    not a freshly validated document.
    """

    __slots__ = (
        "_attribute_sites",
        "_contracts",
        "_fingerprint",
        "_text_sites",
    )

    def __init__(
        self,
        *,
        evidence: EvidenceGraph,
        report: ValidationReport,
        grammar_fingerprint: str,
        revision_id: int,
        current: bool = True,
    ) -> None:
        super().__init__(evidence=evidence, report=report, revision_id=revision_id, current=current)
        self._fingerprint = grammar_fingerprint
        text_sites: dict[RelaxNGElement, EvidenceOccurrence] = {}
        attribute_sites: dict[tuple[RelaxNGElement, str], EvidenceOccurrence] = {}
        for occurrence in evidence.occurrences:
            if occurrence.kind == "text" and occurrence.content_index == 0:
                text_sites.setdefault(occurrence.element, occurrence)
            elif occurrence.kind == "attribute" and occurrence.name is not None:
                attribute_sites.setdefault((occurrence.element, occurrence.name), occurrence)
        self._text_sites = MappingProxyType(text_sites)
        self._attribute_sites = MappingProxyType(attribute_sites)
        self._contracts: dict[EvidenceOccurrence, tuple[ScalarContract, ...]] = {}

    def scalar_value(self, node: RelaxNGElement) -> Any:
        """Interpret the element's leading character data, or return ``None``."""
        _require_current_revision(node, self._revision_id, self._current)
        if node.text_value() is None:
            return None
        occurrence = self._text_sites.get(node)
        if occurrence is None:
            return None
        return self._value_at(occurrence)

    def attribute_value(self, node: RelaxNGElement, expanded_name: str) -> Any:
        """Interpret one present attribute, or return ``None`` when absent."""
        _require_current_revision(node, self._revision_id, self._current)
        if node.attribute_lexical(expanded_name) is None:
            return None
        return self._value_at(self._attribute_sites.get((node, expanded_name)))

    def _contracts_at(self, occurrence: EvidenceOccurrence) -> tuple[ScalarContract, ...]:
        cached = self._contracts.get(occurrence)
        if cached is None:
            cached = cast(EvidenceGraph, self._evidence).schema_contracts_at(occurrence)
            self._contracts[occurrence] = cached
        return cached

    def _value_at(self, occurrence: EvidenceOccurrence | None) -> Any:
        if occurrence is None:
            return None
        return typed_value_at_site(
            cast(EvidenceGraph, self._evidence),
            occurrence,
            grammar_fingerprint=self._fingerprint,
            revision=self._revision_id,
            contracts=self._contracts_at(occurrence),
        )


class RelaxNGDocument(Document):
    """One RELAX NG parse revision with its fresh merged report.

    ``typed`` records whether this parse requested typed access.
    ``interpretations_current`` reports whether the recorded validation and
    evidence snapshot still belongs to the current, unedited lexical revision.
    ``binding_evidence`` is the immutable evidence graph for a valid typed
    document, or ``None`` for validation-only and invalid typed documents.
    ``annotations`` is the immutable pattern-annotation table for a valid typed
    parse requested with ``capture="patterns"``, materialized lazily on first
    access, and raises an explicit capability error when capture did not run.
    ``root``, ``report``, and ``source`` keep the lexical tree, the merged
    schema/instance report, and the original caller source respectively.
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
        binding_evidence: EvidenceGraph | None = None,
        annotations: AnnotationTable | AnnotationCapture | None = None,
    ) -> None:
        if not isinstance(typed, bool):
            raise TypeError("typed must be a bool")
        if not isinstance(interpretations_current, bool):
            raise TypeError("interpretations_current must be a bool")
        if not isinstance(capture, str) or capture not in ("none", "patterns"):
            raise ValueError("capture must be 'none' or 'patterns'")
        if not typed and capture != "none":
            raise ValueError(_CAPTURE_CAPABILITY)
        if binding_evidence is not None and not isinstance(binding_evidence, EvidenceGraph):
            raise TypeError("binding_evidence must be an EvidenceGraph or None")
        if annotations is not None and not isinstance(
            annotations, (AnnotationTable, AnnotationCapture)
        ):
            raise TypeError("annotations must be an AnnotationTable, AnnotationCapture, or None")
        if not typed and binding_evidence is not None:
            raise ValueError("a validation-only RELAX NG document carries no binding evidence")
        if not typed and annotations is not None:
            raise ValueError("a validation-only RELAX NG document carries no annotations")
        if typed and binding_evidence is not None and report.has_errors:
            raise ValueError("an invalid RELAX NG document cannot carry binding evidence")
        if typed and annotations is not None and report.has_errors:
            raise ValueError("an invalid RELAX NG document cannot carry annotations")
        if annotations is not None and capture != "patterns":
            raise ValueError("pattern annotations require capture='patterns'")
        if typed and binding_evidence is None and not report.has_errors:
            raise PyXSDError(_TYPED_EVIDENCE_CAPABILITY)
        revision_id = root.revision.revision_id
        if (
            annotations is not None
            and interpretations_current
            and annotations.revision_id != revision_id
        ):
            raise ValueError("the annotation table belongs to a different RELAX NG parse revision")
        if typed and capture == "patterns" and not report.has_errors and annotations is None:
            raise PyXSDError(_TYPED_ANNOTATION_CAPABILITY)
        existing = root.revision._typed_snapshot
        if not typed and existing is not None:
            raise ValueError(
                "a validation-only RELAX NG document cannot wrap a revision "
                "that already owns typed evidence"
            )
        if typed and existing is not None:
            state = cast(_TypedSnapshotState, existing)
            if not state.owns(
                evidence=binding_evidence,
                report=report,
                revision_id=revision_id,
            ):
                raise ValueError(
                    "the RELAX NG revision already owns a different typed interpretation snapshot"
                )
        if binding_evidence is not None:
            occurrences = set(binding_evidence.occurrences)
            occurrences.update(node.occurrence for node in binding_evidence.retained_nodes)
            occurrences.update(node.occurrence for node in binding_evidence.accepting_roots)
            occurrences.update(binding_evidence._schema_patterns)
            for edge in binding_evidence.retained_edges:
                occurrences.update(
                    node.occurrence for node in (*edge.predecessors, *edge.successors)
                )
                occurrences.update(witness.occurrence for witness in edge.witnesses)
            pending = list(occurrences)
            while pending:
                parent = pending.pop().parent
                if parent is not None and parent not in occurrences:
                    occurrences.add(parent)
                    pending.append(parent)
            documents = [site for site in binding_evidence.occurrences if site.kind == "document"]
            if (
                len(documents) != 1
                or documents[0].element is not root
                or any(site.element.revision is not root.revision for site in occurrences)
            ):
                raise ValueError("binding evidence must belong to the supplied root and revision")
        super().__init__(
            schema=schema,  # type: ignore[arg-type]
            root=root,
            source=source,
            report=report,
            declarations=declarations,
            xdm_capture=xdm_capture,
        )
        self.typed = typed
        self._binding_evidence = binding_evidence
        self._annotations = annotations
        self._capture = capture
        self._limits = limits
        self._namespace_context = namespace_context
        self._revision_id = revision_id
        self._interpretations_current = interpretations_current
        self._attribute_snapshot = (
            _snapshot_original_attributes(root)
            if attribute_snapshot is None
            else attribute_snapshot
        )
        if typed:
            self._attach_typed_interpretation()

    def _attach_typed_interpretation(self) -> None:
        """Attach or share a snapshot, including internal stale transform history."""
        revision = self.root.revision
        existing = revision._typed_snapshot
        if existing is not None:
            state = cast(_TypedSnapshotState, existing)
            if not state.owns(
                evidence=self._binding_evidence, report=self._report, revision_id=self._revision_id
            ):
                raise ValueError(
                    "the RELAX NG revision already owns a different typed interpretation snapshot"
                )
            if not self._interpretations_current:
                state.mark_stale()
        else:
            if self._binding_evidence is None:
                snapshot: _TypedSnapshotState = _InvalidTypedInterpretation(
                    report=self._report,
                    revision_id=self._revision_id,
                    current=self._interpretations_current,
                )
            else:
                snapshot = _TypedInterpretation(
                    evidence=self._binding_evidence,
                    report=self._report,
                    grammar_fingerprint=self.schema._grammar_fingerprint,
                    revision_id=self._revision_id,
                    current=self._interpretations_current,
                )
            revision._attach_typed_snapshot(snapshot)

    @property
    def binding_evidence(self) -> EvidenceGraph | None:
        """Internal read-only evidence snapshot for later Phase 3 tasks.

        This is not a stable public API.  It is the immutable graph for a
        valid typed document and ``None`` for validation-only and invalid
        typed documents.
        """
        return self._binding_evidence

    @property
    def interpretations_current(self) -> bool:
        """Whether the validation snapshot still describes the current revision.

        A dirty revision is not current.  A current typed snapshot can also be
        conservatively marked stale by a transform sharing its revision; both
        documents then report stale until revalidation.
        """
        revision = self.root.revision
        snapshot = revision._typed_snapshot
        snapshot_current = snapshot is None or cast(_TypedSnapshotState, snapshot).current
        return (
            self._interpretations_current
            and revision.revision_id == self._revision_id
            and not revision.dirty
            and snapshot_current
        )

    @property
    def annotations(self) -> AnnotationTable:
        """Return this parse revision's immutable pattern annotations.

        Validation-only documents raise an explicit capability error instead
        of pretending capture ran.  A valid typed parse that did not request
        ``capture="patterns"`` raises the same capability family.  An invalid
        typed document raises :class:`~pyxsd.exceptions.ValidationError` with
        its report, and an edited or transformed revision raises
        :class:`~pyxsd.relaxng.errors.StaleInterpretationError` before either
        validity or capture is considered.

        Materialization is deferred until this property is read.  A valid
        capture-on parse therefore succeeds whenever a capture-off parse does;
        a declared datatype decoder failure surfaces here, when annotations are
        explicitly requested, and is retried on the next access because no
        table is cached after a failed build.  Successful tables are cached for
        this parse revision.
        """
        if not self.typed:
            raise PyXSDError(_ANNOTATION_VALIDATION_ONLY)
        if not self.interpretations_current:
            raise StaleInterpretationError(
                "the RELAX NG annotation view is stale; call revalidate() before typed reads"
            )
        if self._binding_evidence is None:
            _raise_invalid_document(self._report)
        if self._annotations is None:
            raise PyXSDError(_ANNOTATION_NOT_CAPTURED)
        if isinstance(self._annotations, AnnotationCapture):
            return self._annotations.materialize()
        return self._annotations

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
        A typed transform conservatively marks the revision's snapshot stale,
        including the revision shared with this document; a clone receives a
        stale marker snapshot so node reads fail instead of returning
        uninterpreted lexical content.  No evidence is refreshed here.
        An unchanged root keeps the coherent XDM capture; any other root
        starts without one.  Non-lexical results pass through unchanged.
        """
        if not isinstance(root, RelaxNGElement):
            return root
        # Historical evidence may belong to the pre-transform tree.  Only this
        # internal path carries it across roots, with every typed read gated by
        # a stale snapshot; the public constructor always requires ownership.
        if not self.typed and root.revision._typed_snapshot is not None:
            raise ValueError(
                "a validation-only RELAX NG document cannot wrap a revision "
                "that already owns typed evidence"
            )
        stale = copy(self)
        stale.root = root
        stale._revision_id = root.revision.revision_id
        stale._interpretations_current = False
        stale._xdm_capture = self._xdm_capture if root is self.root else None
        if self.typed:
            stale._attach_typed_interpretation()
        return stale

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
            typed=self.typed,
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
