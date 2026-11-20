"""``to_data`` transform: the JSON data export codec as a callable transform."""

from typing import Any

from pyxsd.data_conversion.contracts import (
    Fidelity,
    FormatName,
    JsonValue,
    validate_format,
)
from pyxsd.data_conversion.export import bound_to_data
from pyxsd.exceptions import PyXSDError
from pyxsd.transforms.transform import Transform

_RELAXNG_SCHEMA_DIRECTED_ERROR = (
    'ToData schema-directed formats ("pyxsd-json", "parker", "badgerfish", '
    '"jsonml") are XSD-only; use RelaxNGDocument.to_data(format=...) or the '
    'MCP pyxsd_export_data tool for RELAX NG. Native ToData (format="pyxsd") '
    "exports the lexical grouped tree."
)


class ToData(Transform):
    """Exports the bound tree under an explicit JSON data contract.

    Usable both as ``doc.transform(ToData(**options))`` and from the CLI
    as ``--transform ToData(**options)``. The CLI constructs a transform
    with the tree root, while ``Document.transform`` calls it with the
    root; ``__call__`` therefore accepts an optional explicit root and
    otherwise uses the one given at construction. Keyword arguments pass
    through to :func:`pyxsd.data_conversion.export.bound_to_data`.

    Native ``format="pyxsd"`` also works on a RELAX NG lexical tree and
    produces the generic grouped export; it never manufactures RELAX NG typed
    wrappers or tags. The schema-directed formats are XSD-only on a RELAX NG
    tree and fail explicitly with a pointer to the RELAX NG document API.
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
        options = {**self.kwargs, **kwargs}
        requested_format = validate_format(options.get("format", "pyxsd"))
        if requested_format != "pyxsd":
            # Keep RELAX NG out of the XSD layout planner. Import lazily so a
            # cold transform import does not load the RELAX NG package.
            from pyxsd.relaxng.tree import RelaxNGElement

            if isinstance(target, RelaxNGElement):
                raise PyXSDError(_RELAXNG_SCHEMA_DIRECTED_ERROR)
        return bound_to_data(target, **options)
