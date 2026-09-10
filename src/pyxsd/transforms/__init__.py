"""Transform framework for pyxsd.

Custom transforms subclass :class:`Transform` (or :class:`Displayer`
for transforms that write output) from this package::

    from pyxsd.transforms import Transform

The shipped transforms are :class:`~pyxsd.transforms.print_data.PrintData`
and :class:`~pyxsd.transforms.send_tree_to_pyxsd.SendTreeToPyXSD`.
The historical crystallography library (CellSizer and friends) moved to
``examples/transforms/`` in the repository.
"""

from pyxsd.transforms.displayer import Displayer
from pyxsd.transforms.transform import Transform, iter_tree

__all__ = ["Displayer", "Transform", "iter_tree"]
