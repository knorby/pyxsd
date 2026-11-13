"""Adapt one compiled XSLT stylesheet to the document-transform interface.

:mod:`pyxsd.document_transform` owns the dispatch protocol; this module
owns the stylesheet-side conversion, validation, and result handling.
``CompiledStylesheet.as_transform`` only constructs the adapter wired to
its compiled artifact and extension snapshot, so the engine keeps a
single invocation path: the adapter runs the ordinary public
``CompiledStylesheet.transform`` with explicit document delivery.

The adapter is a read-only coordination object.  Its ``on_result``
callback observes the complete transformation result; it never writes to
the pipeline, the compiled artifact, or the target schema.  As with
direct parsing, transforms that bind to one target schema must not run
concurrently or reentrantly on that schema.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pyxsd.document_transform import AncillaryResultError
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.schema import Schema
from pyxsd.xdm.document import element_from_xdm
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt.extensions import XsltExtensions
from pyxsd.xslt.processor import CompiledStylesheet
from pyxsd.xslt.results import TransformationResult

if TYPE_CHECKING:
    from pyxsd.document import Document

__all__ = ["XsltDocumentTransform"]


class XsltDocumentTransform:
    """One compiled stylesheet exposed as a :class:`DocumentTransform`.

    Constructed by :meth:`CompiledStylesheet.as_transform`; the wired
    artifact and its extension snapshot are fixed for the adapter's
    lifetime.  Every call runs one fresh invocation of the stylesheet
    and never mutates the compiled state.
    """

    __slots__ = ("_extensions", "_on_result", "_stylesheet", "_target_schema")

    def __init__(
        self,
        stylesheet: CompiledStylesheet,
        *,
        extensions: XsltExtensions,
        target_schema: Schema,
        on_result: Callable[[TransformationResult], None] | None = None,
    ) -> None:
        if not isinstance(stylesheet, CompiledStylesheet):
            raise TypeError("stylesheet must be a compiled XsltProcessor stylesheet")
        if not isinstance(extensions, XsltExtensions):
            raise TypeError("extensions must be the compiled artifact's XsltExtensions snapshot")
        if not isinstance(target_schema, Schema):
            raise TypeError("target_schema must be a compiled pyxsd Schema")
        if on_result is not None and not callable(on_result):
            raise TypeError("on_result must be a callable or None")
        self._stylesheet = stylesheet
        self._extensions = extensions
        self._target_schema = target_schema
        self._on_result = on_result

    def transform_document(self, document: Document) -> Document:
        """Returns *document* transformed by the wired stylesheet.

        The source document must carry a coherent captured XDM view
        (``Schema.parse(..., capture_xdm=True)`` and no later bound-tree
        edit); the stylesheet runs over that view with explicit document
        delivery, so a raw principal result that is not a document is
        rejected by the delivery boundary.  Secondary results and
        messages are ancillary output: the transform is rejected with
        :class:`~pyxsd.document_transform.AncillaryResultError` unless
        ``on_result`` was supplied, in which case the callback receives
        the complete :class:`~pyxsd.xslt.results.TransformationResult`
        before binding; its return value is ignored and an exception it
        raises aborts the transform.

        The principal result is bound directly to the target schema --
        structurally, with no serialization or reparse -- through
        ``target_schema.parse(..., capture_xdm=True)`` and must validate:
        a failing report raises :class:`~pyxsd.exceptions.ValidationError`
        carrying that fresh report.  On success a fresh document with its
        own report, provenance, and captured XDM is returned.
        """
        result = self._stylesheet.transform(document, delivery="document")
        if result.secondary or result.messages:
            if self._on_result is None:
                raise AncillaryResultError(_ancillary_message(result))
            self._on_result(result)
        converted = self._bind_principal(result.principal_tree)
        if not converted.is_valid:
            raise ValidationError(
                "the transformation result is not valid against the target schema: "
                f"{converted.report.error_count} error(s); inspect ValidationError.report",
                converted.report,
            )
        return converted

    def _bind_principal(self, tree: XdmDocument) -> Document:
        """Bind the delivered principal document to the target schema."""
        root_element = tree.root_element
        if root_element is None:
            raise PyXSDError(
                "the stylesheet's principal result has no document element; "
                "it cannot be bound as a schema document"
            )
        element = element_from_xdm(root_element, self._target_schema.namespace_context)
        return self._target_schema.parse(element, capture_xdm=True)


def _ancillary_message(result: TransformationResult) -> str:
    """Describe what the stylesheet produced beyond its principal result."""
    produced: list[str] = []
    if result.secondary:
        produced.append(f"{len(result.secondary)} secondary result(s)")
    if result.messages:
        produced.append(f"{len(result.messages)} message(s)")
    return (
        "the stylesheet produced "
        + " and ".join(produced)
        + "; supply on_result to receive the complete transformation result"
    )
