"""pyxsd: schema-guided XML-to-Python object mapping with a transform pipeline."""

from typing import TYPE_CHECKING

__version__ = "1.0.0.dev0"

__all__ = ["PyXSD", "XMLNode", "__version__"]

if TYPE_CHECKING:
    from pyxsd.nodes import XMLNode
    from pyxsd.parser import PyXSD


def __getattr__(name: str):
    # Lazy imports so that ``import pyxsd`` does not pull the parser
    # stack (and the ER tag registry) unless the API is actually used.
    if name == "PyXSD":
        from pyxsd.parser import PyXSD

        return PyXSD
    if name == "XMLNode":
        from pyxsd.nodes import XMLNode

        return XMLNode
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
