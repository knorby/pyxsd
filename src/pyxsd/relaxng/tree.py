"""RELAX NG compatibility and assignment for lexical XML nodes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Self
from xml.etree import ElementTree as ET

from pyxsd._lexical.tree import LexicalElement, LexicalRevision, NodeContext
from pyxsd._lexical.tree import _effective_base as _effective_base
from pyxsd.locations import IssueLocation
from pyxsd.namespaces import NamespaceContext
from pyxsd.relaxng.origins import SourceOrigin

if TYPE_CHECKING:
    from pyxsd.relaxng.evidence import CandidateSelection

__all__ = ["LexicalRevision", "NodeContext", "RelaxNGElement"]


class RelaxNGElement(LexicalElement):
    """The lexical node used by RELAX NG documents and their projections.

    Typed scalar and attribute access requires a typed parse and returns
    ``None`` when no interpretation is available at that position. An invalid
    typed document raises :class:`~pyxsd.exceptions.ValidationError`; edits
    make the interpretation stale and raise
    :class:`~pyxsd.relaxng.errors.StaleInterpretationError` on typed access.
    """

    __slots__ = ()

    _typed_access_capability = (
        "RELAX NG typed scalar access requires a typed parse; pass typed=True when parsing"
    )
    # The validator diagnoses explicitly supplied ElementTree non-element nodes.
    _keep_non_element_tags = True

    def __init__(self) -> None:
        raise TypeError("RelaxNGElement values are created by from_etree() or clone_relaxng()")

    @classmethod
    def from_etree(
        cls,
        element: ET.Element,
        *,
        namespaces: NamespaceContext | None = None,
        base_uri: str | None = None,
        source: IssueLocation | SourceOrigin | None = None,
    ) -> Self:
        """Build a RELAX NG lexical tree, accepting authored origins as source links."""
        if not isinstance(element, ET.Element):
            raise TypeError("element must be an xml.etree.ElementTree.Element")
        if isinstance(source, SourceOrigin):
            source = source.location
        elif source is not None and not isinstance(source, IssueLocation):
            raise TypeError("source must be an IssueLocation, SourceOrigin, or None")
        return cls._from_etree(element, namespaces=namespaces, base_uri=base_uri, source=source)

    @staticmethod
    def _child_type() -> type[RelaxNGElement]:
        return RelaxNGElement

    def assign_fields(
        self,
        fields: Mapping[str, object],
        *,
        selections: tuple[CandidateSelection, ...] = (),
    ) -> None:
        """Encode and apply one atomic transaction from a field mapping.

        This is the collision-safe entry point: every key is resolved as a
        generated projection alias or the route's scalar descriptor name, so an
        XML field named ``assign``, ``assign_fields``, or ``selections`` cannot
        shadow the transaction API.  Every value is shape-checked, encoded from
        the plan's compile-time patterns, and context-checked before any lexical
        store changes.  A successful write dirties the whole revision but never
        certifies document validity or refreshes typed evidence.
        """
        if not isinstance(fields, Mapping):
            raise TypeError("assign_fields requires a mapping of generated field names")
        from pyxsd.relaxng.binding import _assign_field_map

        _assign_field_map(self, dict(fields), selections)

    def assign(
        self,
        *,
        selections: tuple[CandidateSelection, ...] = (),
        **fields: Any,
    ) -> None:
        """Convenience keyword form of :meth:`assign_fields`.

        Field aliases are reserved away from ``assign`` and ``assign_fields``;
        use :meth:`assign_fields` for a field whose alias is literally
        ``selections`` so the mapping key is not confused with this keyword.
        """
        self.assign_fields(fields, selections=selections)
