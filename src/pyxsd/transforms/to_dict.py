"""``to_dict`` transform: the dict export codec as a callable transform."""

from typing import Any

from pyxsd.dict_export import bound_to_dict
from pyxsd.transforms.transform import Transform


class ToDict(Transform):
    """Exports the bound tree as a plain dict.

    Usable both as ``doc.transform(ToDict(**options))`` and from the CLI
    as ``--transform to_dict(**options)``. The CLI constructs a transform
    with the tree root, while ``Document.transform`` calls it with the
    root; ``__call__`` therefore accepts an optional explicit root and
    otherwise uses the one given at construction. Keyword arguments pass
    through to :func:`pyxsd.dict_export.bound_to_dict`.
    """

    def __init__(self, root: Any = None, **kwargs: Any) -> None:
        super().__init__(root)
        self.kwargs = kwargs

    def __call__(self, root: Any = None, **kwargs: Any) -> dict:
        target = self.root if root is None else root
        return bound_to_dict(target, **{**self.kwargs, **kwargs})
