"""Runtime node protocol for parsed XML instance trees.

The parser turns an XML document into a tree of dynamically generated
schema classes. The tree carries its data in four bookkeeping
attributes, and everything that walks the tree — the writers, the
transform framework, and user transforms — consumes that shape rather
than any concrete class. :class:`XMLNode` describes that shape as a
:class:`typing.Protocol`, so user code can annotate transforms without
importing anything dynamic.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class XMLNode(Protocol):
    """Structural type of one node in a parsed instance tree.

    Both the generated schema classes and the synthetic nodes built by
    :meth:`Transform.makeElemObj <pyxsd.transforms.Transform.makeElemObj>`
    satisfy this protocol:

    - ``_name_``: the element's tag name.
    - ``_attribs_``: attribute names mapped to their lexical values.
    - ``_children_``: child nodes in document order (a child may be
      ``None`` when an optional element is absent).
    - ``_value_``: the element's text split on line boundaries, or
      ``None`` when it has none.
    """

    _name_: str
    _attribs_: dict[str, str]
    _children_: list[Any]
    _value_: list[str] | None


__all__ = ["XMLNode"]
