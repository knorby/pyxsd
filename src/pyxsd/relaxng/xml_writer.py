"""RELAX NG compatibility entry points for lexical XML writing and cloning."""

from __future__ import annotations

from pyxsd._lexical.xml_writer import clone_lexical, serialize_lexical
from pyxsd.relaxng.tree import RelaxNGElement

__all__ = ["clone_relaxng", "serialize_relaxng"]


def serialize_relaxng(root: RelaxNGElement) -> str:
    """Serialize a lexical RELAX NG subtree as a compact XML fragment."""
    if not isinstance(root, RelaxNGElement):
        raise TypeError("root must be a RelaxNGElement")
    return serialize_lexical(root)


def clone_relaxng(root: RelaxNGElement) -> RelaxNGElement:
    """Return an independent lexical RELAX NG copy with a fresh revision."""
    if not isinstance(root, RelaxNGElement):
        raise TypeError("root must be a RelaxNGElement")
    return clone_lexical(root)
