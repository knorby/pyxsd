"""Transform framework for pyxsd.

Custom transforms subclass :class:`Transform` (or :class:`Displayer`
for transforms that write output) from this package::

    from pyxsd.transforms import Transform

The shipped transforms are :class:`~pyxsd.transforms.print_data.PrintData`,
:class:`~pyxsd.transforms.to_dict.ToDict`, and
:class:`~pyxsd.transforms.to_data.ToData`.
The historical crystallography library (CellSizer and friends) moved to
``examples/legacy/`` in the repository.
"""

from pyxsd.transforms.displayer import Displayer
from pyxsd.transforms.to_data import ToData
from pyxsd.transforms.to_dict import ToDict
from pyxsd.transforms.transform import Transform, iter_tree

__all__ = ["Displayer", "ToData", "ToDict", "Transform", "iter_tree"]
