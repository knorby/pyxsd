"""Internal schema-neutral XML tree and writer."""

from pyxsd._lexical.tree import LexicalElement, LexicalRevision, NodeContext
from pyxsd._lexical.xml_writer import clone_lexical, serialize_lexical

__all__ = [
    "LexicalElement",
    "LexicalRevision",
    "NodeContext",
    "clone_lexical",
    "serialize_lexical",
]
