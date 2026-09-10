"""Demo transform: CountElements.

Counts how many times each element name appears in the parsed tree and
prints a small frequency table. Demonstrates the standard transform
shape (a module named after the class, ``__init__`` taking the root,
``__call__`` doing the work) and the ``iter_tree`` helper.

Example CLI use from this directory::

    pyxsd -i instance.xml -t 'CountElements()'

The transform returns the root unchanged, so the writer still emits
the tree.
"""

from __future__ import annotations

from collections import Counter

from pyxsd.transforms import Transform, iter_tree


class CountElements(Transform):
    """Print a frequency table of element names; return the root."""

    def __init__(self, rootInstance):
        super().__init__(rootInstance)

    def __call__(self) -> object:
        counts: Counter[str] = Counter()
        for node in iter_tree(self.root):
            counts[node._name_] += 1
        print("CountElements: element frequency")
        for name, count in sorted(counts.items()):
            print(f"  {name}: {count}")
        return self.root
