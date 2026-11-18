"""RELAX NG data-export dispatch and its freshness policy.

Every public data format owns one RELAX NG adapter entry.  Dispatch validates
the shared format/fidelity names and the native-only option rules exactly as
the XSD export entry points do, then applies one freshness gate before the
adapter runs: a typed export from a format that materializes schema-directed
values requires a valid document whose evidence still describes the current
lexical revision.  Native ``typed=False`` output and JsonML stay lexical, so
they may export a dirty or invalid current tree; they never consult evidence.

No adapter is allowed to fall back to an XSD converter.  Until the concrete
RELAX NG adapters land, each production registry entry raises
:class:`RelaxNGUnsupportedAdapterError` explicitly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pyxsd.data_conversion.contracts import (
    Fidelity,
    FormatName,
    JsonValue,
    validate_fidelity,
    validate_format,
)
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.relaxng.errors import StaleInterpretationError

if TYPE_CHECKING:
    from pyxsd.relaxng.document import RelaxNGDocument

__all__ = [
    "ExportAdapter",
    "RelaxNGUnsupportedAdapterError",
    "export_relaxng",
]


class RelaxNGUnsupportedAdapterError(PyXSDError):
    """A requested RELAX NG export format still has no concrete adapter."""

    def __init__(self, format: FormatName) -> None:
        super().__init__(f"RELAX NG data export adapter for format {format!r} is not implemented")
        self.format = format


class ExportAdapter(Protocol):
    """One RELAX NG-owned data-format exporter.

    ``requires_current_typed_evidence`` marks adapters whose typed output
    materializes schema-directed values.  Dispatch checks it before
    :meth:`export`, so even an unfinished adapter never sees a stale or
    invalid typed document.  The export options are the XSD-compatible
    ``fidelity``, ``typed``, and ``always_list`` contract; concrete adapters
    may use them to keep native raw/Python values distinct from later
    JSON-safe tagging.
    """

    format: FormatName
    requires_current_typed_evidence: bool

    def export(
        self,
        document: RelaxNGDocument,
        *,
        fidelity: Fidelity,
        typed: bool,
        always_list: bool,
    ) -> JsonValue:
        """Return *document* under this adapter's format contract."""
        ...


class _UnimplementedAdapter:
    """Production placeholder that never delegates to an XSD converter."""

    __slots__ = ("format", "requires_current_typed_evidence")

    def __init__(
        self,
        format: FormatName,
        *,
        requires_current_typed_evidence: bool,
    ) -> None:
        self.format = format
        self.requires_current_typed_evidence = requires_current_typed_evidence

    def export(
        self,
        document: RelaxNGDocument,
        *,
        fidelity: Fidelity,
        typed: bool,
        always_list: bool,
    ) -> JsonValue:
        del document, fidelity, typed, always_list
        raise RelaxNGUnsupportedAdapterError(self.format)


#: The single registry every RELAX NG export path resolves through.
_ADAPTERS: dict[FormatName, ExportAdapter] = {
    "pyxsd": _UnimplementedAdapter("pyxsd", requires_current_typed_evidence=True),
    "pyxsd-json": _UnimplementedAdapter("pyxsd-json", requires_current_typed_evidence=True),
    "parker": _UnimplementedAdapter("parker", requires_current_typed_evidence=True),
    "badgerfish": _UnimplementedAdapter("badgerfish", requires_current_typed_evidence=True),
    "jsonml": _UnimplementedAdapter("jsonml", requires_current_typed_evidence=False),
}


def _validate_native_options(
    format: FormatName,
    *,
    typed: bool,
    always_list: bool,
) -> None:
    """Apply the XSD native-only option rule before any adapter runs."""
    if format == "pyxsd":
        return
    if typed is not True:
        raise ValueError("typed is a native-only data-export option")
    if always_list is not False:
        raise ValueError("always_list is a native-only data-export option")


def _require_current_typed_evidence(document: RelaxNGDocument) -> None:
    """Reject a typed export that cannot materialize schema-directed values."""
    if not document.typed:
        raise PyXSDError(
            "a typed RELAX NG data export requires a typed document with current evidence"
        )
    if not document.interpretations_current:
        raise StaleInterpretationError(
            "the RELAX NG typed interpretation is stale; call revalidate() before typed reads"
        )
    if document.report.has_errors:
        raise ValidationError(
            "the RELAX NG document is invalid; typed data export requires a valid document",
            document.report,
        )


def export_relaxng(
    document: RelaxNGDocument,
    *,
    format: FormatName = "pyxsd",
    fidelity: Fidelity = "best-effort",
    typed: bool = True,
    always_list: bool = False,
) -> JsonValue:
    """Export a RELAX NG document through its format's RELAX NG adapter.

    Format and fidelity names are validated first, then non-default
    ``typed``/``always_list`` values are rejected for every format except
    native ``pyxsd``.  A typed export whose adapter materializes
    schema-directed values requires a valid, current typed document; native
    ``typed=False`` and JsonML skip that gate and read the current lexical
    tree.  This function never imports or calls an XSD converter.
    """
    requested_format = validate_format(format)
    requested_fidelity = validate_fidelity(fidelity)
    _validate_native_options(requested_format, typed=typed, always_list=always_list)

    adapter = _ADAPTERS[requested_format]
    if typed and adapter.requires_current_typed_evidence:
        _require_current_typed_evidence(document)

    return adapter.export(
        document,
        fidelity=requested_fidelity,
        typed=typed,
        always_list=always_list,
    )
