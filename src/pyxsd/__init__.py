"""pyxsd: schema-guided XML-to-Python object mapping with a transform pipeline."""

from typing import TYPE_CHECKING

__version__ = "1.0.0"

__all__ = [
    "BindingPolicy",
    "Document",
    "NamespaceError",
    "ParseModes",
    "PyXSDError",
    "PyXSDWarning",
    "Schema",
    "ValidationError",
    "XMLNode",
    "XPathError",
    "__version__",
    "compile",
    "parse",
]

if TYPE_CHECKING:
    from pyxsd.binding import BindingPolicy, ParseModes
    from pyxsd.document import Document
    from pyxsd.exceptions import (
        NamespaceError,
        PyXSDError,
        PyXSDWarning,
        ValidationError,
        XPathError,
    )
    from pyxsd.nodes import XMLNode
    from pyxsd.schema import Schema, compile, parse


def __getattr__(name: str):
    # Lazy imports so that ``import pyxsd`` does not pull the parser
    # stack (and the ER tag registry) unless the API is actually used.
    if name in ("BindingPolicy", "ParseModes"):
        from pyxsd import binding

        return getattr(binding, name)
    if name == "Document":
        from pyxsd.document import Document

        return Document
    if name in (
        "NamespaceError",
        "PyXSDError",
        "PyXSDWarning",
        "ValidationError",
        "XPathError",
    ):
        from pyxsd import exceptions

        return getattr(exceptions, name)
    if name == "Schema":
        from pyxsd.schema import Schema

        return Schema
    if name == "XMLNode":
        from pyxsd.nodes import XMLNode

        return XMLNode
    if name == "compile":
        from pyxsd.schema import compile

        return compile
    if name == "parse":
        from pyxsd.schema import parse

        return parse
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
