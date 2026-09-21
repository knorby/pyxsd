"""Transform framework for pyxsd.

Custom transforms subclass :class:`Transform` (or :class:`Displayer`
for transforms that write output) from this package::

    from pyxsd.transforms import Transform

The shipped transform is :class:`~pyxsd.transforms.print_data.PrintData`.
The historical crystallography library (CellSizer and friends) moved to
``examples/legacy/`` in the repository.
"""

from pyxsd.transforms.displayer import Displayer
from pyxsd.transforms.to_dict import ToDict
from pyxsd.transforms.transform import Transform, iter_tree

__all__ = ["Displayer", "ToDict", "Transform", "iter_tree"]
