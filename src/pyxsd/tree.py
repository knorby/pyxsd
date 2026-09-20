"""Tree traversal helpers shared by the public API and the transforms.

:func:`iter_tree` yields every node of a bound instance tree in
pre-order; it backs :meth:`pyxsd.document.Document.walk`,
:meth:`pyxsd.transforms.transform.Transform.walk`, and direct user
iteration.
"""

from collections.abc import Iterator
from typing import Any


def iter_tree(instance: Any) -> Iterator[Any]:
    """Yield every tree node at or below ``instance``, depth-first.

    Lists (and tuples) are descended into item by item and
    dictionaries by value; anything without both ``_children_`` and
    ``_attribs_`` is skipped. Each yielded node is visited before its
    children (pre-order). This generator powers
    :meth:`~pyxsd.transforms.transform.Transform.walk` and is the
    supported way to iterate a tree directly::

        for node in iter_tree(root):
            ...

    - ``instance``: a tree node, or a list/dict of them.
    """
    if isinstance(instance, (list, tuple)):
        for item in instance:
            yield from iter_tree(item)
    elif isinstance(instance, dict):
        for item in instance.values():
            yield from iter_tree(item)
    elif hasattr(instance, "_children_") and hasattr(instance, "_attribs_"):
        yield instance
        for child in instance._children_:
            yield from iter_tree(child)
