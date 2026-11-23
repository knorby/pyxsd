"""The schemaless family of lexical nodes and revisions."""

from __future__ import annotations

from pyxsd._lexical import LexicalElement, LexicalRevision


class SchemalessRevision(LexicalRevision):
    """One authoritative revision of a schemaless XML tree."""

    __slots__ = ()


class SchemalessElement(LexicalElement):
    """An editable XML element with no schema binding."""

    __slots__ = ()

    @classmethod
    def _create_revision(cls) -> SchemalessRevision:
        return SchemalessRevision()
