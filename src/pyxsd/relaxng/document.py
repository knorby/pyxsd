"""Validation-only RELAX NG documents over one authoritative lexical tree.

A :class:`RelaxNGDocument` pairs a compiled :class:`RelaxNGSchema` with the
lexical occurrence tree a parse produced and one fresh report: frozen
schema-phase findings followed by this parse's instance findings.  Phase 2
exposes validation-only parsing, so the document records ``typed=False`` and
has no pattern annotations or typed projections.  Methods that would require
those Phase 3 capabilities fail explicitly instead of reaching an XSD
converter, binder, or revalidation path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, NoReturn

from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.xml_writer import serialize_relaxng

if TYPE_CHECKING:
    from pyxsd.data_conversion.contracts import Fidelity, FormatName
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
        xdm_capture: Any = None,
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
            xdm_capture=xdm_capture,
        )
        self.typed = typed
        self._capture = capture
        self._limits = limits
        self._namespace_context = namespace_context
        self._revision_id = root.revision.revision_id

    @property
    def interpretations_current(self) -> bool:
        """Whether the validation snapshot still describes the current revision."""
        revision = self.root.revision
        return revision.revision_id == self._revision_id and not revision.dirty

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

    def revalidate(self) -> NoReturn:
        """Reject revalidation until the RELAX NG document adapter lands."""
        raise PyXSDError("RELAX NG document revalidation is not supported in this release")
