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

from typing import TYPE_CHECKING, Any, NoReturn

from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng.tree import RelaxNGElement

if TYPE_CHECKING:
    from pyxsd.data_conversion.contracts import Fidelity, FormatName, JsonValue
    from pyxsd.relaxng.limits import RelaxNGLimits
    from pyxsd.relaxng.schema import RelaxNGSchema
    from pyxsd.validation import ValidationReport

__all__ = ["RelaxNGDocument"]

_ANNOTATION_CAPABILITY = (
    "RELAX NG pattern annotations are not available on a validation-only document; "
    "reparse with typed=True and capture='patterns'"
)
_TYPED_EXPORT_CAPABILITY = (
    "typed=True RELAX NG export is not supported in this release; "
    "use typed=False for lexical export"
)


class RelaxNGDocument(Document):
    """One RELAX NG parse revision with its fresh merged report.

    ``typed`` is false for every Phase 2 document.  ``interpretations_current``
    reports whether the recorded validation snapshot still belongs to the
    current, unedited lexical revision.  ``root``, ``report``, and ``source``
    keep the lexical tree, the merged schema/instance report, and the original
    caller source respectively.
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
        """Reject annotation access; validation-only documents have no capture."""
        raise PyXSDError(_ANNOTATION_CAPABILITY)

    def to_dict(self, *, typed: bool = True, always_list: bool = False) -> dict[str, Any]:
        """Export lexically; typed projections are a later capability."""
        if typed:
            raise PyXSDError(_TYPED_EXPORT_CAPABILITY)
        return super().to_dict(typed=False, always_list=always_list)

    def to_data(
        self,
        *,
        format: FormatName = "pyxsd",
        fidelity: Fidelity = "best-effort",
        typed: bool = True,
        always_list: bool = False,
    ) -> JsonValue:
        """Export lexical data contracts; schema-aware/typed routes are absent."""
        if typed:
            raise PyXSDError(_TYPED_EXPORT_CAPABILITY)
        if format == "pyxsd-json":
            raise PyXSDError(
                "the schema-aware pyxsd-json RELAX NG export is not supported in this release"
            )
        return super().to_data(
            format=format,
            fidelity=fidelity,
            typed=False,
            always_list=always_list,
        )

    def revalidate(self) -> NoReturn:
        """Reject revalidation until the RELAX NG document adapter lands."""
        raise PyXSDError("RELAX NG document revalidation is not supported in this release")
