"""The document-transform dispatch interface for schema-bound documents.

:class:`pyxsd.document.Document.transform` accepts two kinds of
transforms: the legacy root callable and -- dispatched first -- any
object implementing the :class:`DocumentTransform` protocol.  A
document transform consumes and produces whole schema-bound documents,
so it can rebind the result to another schema, require validation, and
deliver a fresh document: things a root callable cannot express.

The protocol is defined here; implementations live with their engines
(the XSLT adapter is
:class:`pyxsd.xslt.adapter.XsltDocumentTransform`).  The module is
import-light so ``Document.transform`` can dispatch without dragging an
engine into a cold import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pyxsd.exceptions import PyXSDError

if TYPE_CHECKING:
    from pyxsd.document import Document

__all__ = ["AncillaryResultError", "DocumentTransform"]


@runtime_checkable
class DocumentTransform(Protocol):
    """A transform that consumes and produces whole schema-bound documents."""

    def transform_document(self, document: Document) -> Document:
        """Returns *document* transformed as one whole document."""
        ...


class AncillaryResultError(PyXSDError):
    """A document transform produced ancillary results no callback received.

    Secondary results and stylesheet messages are ancillary output: a
    transform that produces them is rejected unless the caller supplied
    an ``on_result`` callback to receive the complete transformation
    result.  The rejection is the only delivery: the error carries no
    result, and supplying the callback grants read access only -- it
    never authorizes writes to the pipeline, the transform's inputs, or
    its target schema.
    """
