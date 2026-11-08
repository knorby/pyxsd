"""``to_data`` transform: the JSON data export codec as a callable transform."""

from typing import Any

from pyxsd.data_conversion.contracts import Fidelity, FormatName, JsonValue
from pyxsd.data_conversion.export import bound_to_data
from pyxsd.transforms.transform import Transform


class ToData(Transform):
    """Exports the bound tree under an explicit JSON data contract.

    Usable both as ``doc.transform(ToData(**options))`` and from the CLI
    as ``--transform ToData(**options)``. The CLI constructs a transform
    with the tree root, while ``Document.transform`` calls it with the
    root; ``__call__`` therefore accepts an optional explicit root and
    otherwise uses the one given at construction. Keyword arguments pass
    through to :func:`pyxsd.data_conversion.export.bound_to_data`.
    """

    def __init__(
        self,
        root: Any = None,
        *,
        format: FormatName = "pyxsd",
        fidelity: Fidelity = "best-effort",
        typed: bool = True,
        always_list: bool = False,
    ) -> None:
        super().__init__(root)
        self.kwargs: dict[str, Any] = {
            "format": format,
            "fidelity": fidelity,
            "typed": typed,
            "always_list": always_list,
        }

    def __call__(self, root: Any = None, **kwargs: Any) -> JsonValue:
        target = self.root if root is None else root
        return bound_to_data(target, **{**self.kwargs, **kwargs})
