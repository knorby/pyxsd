"""The schemaless document: shared document operations over a lexical tree.

A :class:`SchemalessDocument` pairs one editable
:class:`~pyxsd.schemaless.tree.SchemalessElement` tree with the parse's report
and source evidence. Its :attr:`~pyxsd.document.Document.schema` is ``None``:
nothing was validated against XSD or RELAX NG, and schema-directed operations
fail explicitly instead of implying validation. Traversal, XPath, lexical
writing, transforms, and the native ``pyxsd`` data export are shared with the
schema-bound document family.

The default typed view is lexical. Every scalar read returns the authoritative
character data as a string until a later type policy replaces that
interpretation; the lexical stores are never rewritten by a read.
"""

from __future__ import annotations

import os.path
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

from pyxsd._lexical.xml_writer import serialize_lexical
from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError
from pyxsd.schemaless.tree import SchemalessElement
from pyxsd.validation import ValidationReport

if TYPE_CHECKING:
    from pyxsd.data_conversion.contracts import Fidelity, FormatName, JsonValue
    from pyxsd.source_spans import SourceIndex
    from pyxsd.xdm.document import CaptureSource
    from pyxsd.xdm.nodes import XdmDocument


class _LexicalScalarSnapshot:
    """The default schemaless typed view: authoritative lexical strings.

    The revision-owned snapshot protocol lets one document interpret a lexical
    revision without storing a second value per node. Until a type policy
    supplies inference or explicit pins, both scalar reads return the lexical
    store unchanged, so ``"02138"`` never becomes a number.
    """

    __slots__ = ()

    def scalar_value(self, node: SchemalessElement) -> str | None:
        """Return the element's leading character data exactly as stored."""
        return node.text_value()

    def attribute_value(self, node: SchemalessElement, expanded_name: str) -> str | None:
        """Return one present attribute's lexical value exactly as stored."""
        return node.attribute_lexical(expanded_name)


_LEXICAL_SNAPSHOT = _LexicalScalarSnapshot()


class SchemalessDocument(Document):
    """One XML parse with no schema binding.

    ``root`` is the editable lexical tree, ``report`` is the parse's report
    (never a validation verdict), ``source`` is the recorded source name, and
    ``source_index`` is the byte index for that source (``None`` for a
    caller-built tree). ``report_current`` is true only while the report still
    describes the tree: a lexical edit or a transform wrapper makes it stale.
    """

    root: SchemalessElement

    def __init__(
        self,
        root: SchemalessElement,
        source: str | None,
        report: ValidationReport,
        *,
        source_index: SourceIndex | None = None,
        capture_source: CaptureSource | None = None,
        report_current: bool = True,
    ) -> None:
        # The parse's materialized bytes are retained for a later coherent
        # XDM view; the base class's separately-shaped XDM capture stays None.
        super().__init__(schema=None, root=root, source=source, report=report)
        self.source_index = source_index
        self._capture_source = capture_source
        self._report_current = report_current
        if root.revision._typed_snapshot is None:
            root.revision._attach_typed_snapshot(_LEXICAL_SNAPSHOT)

    @property
    def report_current(self) -> bool:
        """Whether the report still describes the current lexical revision.

        A successful edit dirties the revision, and a transform wrapper
        deliberately keeps the old report snapshot, so both report stale until
        the document is parsed again.
        """
        return self._report_current and not self.root.revision.dirty

    def to_xdm(self) -> XdmDocument:
        """Rejects XDM access until a coherent schemaless view is connected."""
        raise PyXSDError(
            "XDM access is not connected for a schemaless document; "
            "the lexical tree and to_string() are the authoritative views"
        )

    def to_dict(self, *, typed: bool = True, always_list: bool = False) -> dict:
        """Returns the lexical tree as a plain dict.

        ``typed=True`` still returns the authoritative lexical strings: the
        default schemaless scalar view interprets nothing. ``typed=False``
        returns the same strings through the generic lexical fallback.
        """
        from pyxsd.dict_export import bound_to_dict

        return bound_to_dict(
            self.root,
            typed=typed,
            always_list=always_list,
            scalar_reader=SchemalessElement.scalar_value,
            attribute_reader=SchemalessElement.attribute_value,
        )

    def to_data(
        self,
        *,
        format: FormatName = "pyxsd",
        fidelity: Fidelity = "best-effort",
        typed: bool = True,
        always_list: bool = False,
    ) -> JsonValue:
        """Returns the lexical tree under the native ``pyxsd`` data contract.

        The schema-directed formats need a compiled schema to choose a
        layout, so a schemaless document rejects them explicitly. ``typed``,
        ``always_list``, and ``fidelity`` keep their native meanings.
        """
        if format != "pyxsd":
            raise PyXSDError(
                f"the {format!r} data format requires an XSD or RELAX NG schema; "
                "a schemaless document exports only the native 'pyxsd' format"
            )
        from pyxsd.data_conversion.export import bound_to_data

        return bound_to_data(
            self.root,
            format="pyxsd",
            fidelity=fidelity,
            typed=typed,
            always_list=always_list,
            scalar_reader=SchemalessElement.scalar_value,
            attribute_reader=SchemalessElement.attribute_value,
        )

    def write(self, dest: str | Path | os.PathLike[str] | IO[str]) -> None:
        """Writes the lexical tree to *dest* (a path or a file object)."""
        text = serialize_lexical(self.root)
        if isinstance(dest, (str, os.PathLike)):
            with open(dest, "w", encoding="utf-8") as output:
                output.write(text)
        else:
            dest.write(text)
            dest.flush()

    def to_string(self) -> str:
        """Returns the lexical tree serialized as compact XML text."""
        return serialize_lexical(self.root)

    def _document_for_node_result(self, root: Any) -> Any:
        """Wrap a lexical transform result as a stale schemaless document.

        The result keeps this document's report snapshot and is therefore
        stale until the tree is parsed again. Only a transform that returns
        the current root keeps the parse's source label and materialized
        source/XDM capture; any other root begins without them.
        """
        if not isinstance(root, SchemalessElement):
            return root
        unchanged = root is self.root
        return SchemalessDocument(
            root,
            cast("str | None", self.source) if unchanged else None,
            self._report,
            source_index=self.source_index if unchanged else None,
            capture_source=self._capture_source if unchanged else None,
            report_current=False,
        )
