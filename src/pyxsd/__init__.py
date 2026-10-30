"""pyxsd: schema-guided XML-to-Python object mapping with a transform pipeline."""

from typing import TYPE_CHECKING

__version__ = "1.2.1"

__all__ = [
    "BindingPolicy",
    "Document",
    "Extensions",
    "NamespaceError",
    "ParseModes",
    "PyXSDError",
    "PyXSDWarning",
    "Schema",
    "ValidationError",
    "XMLNode",
    "XPathError",
    "XmlLimits",
    "XmlParserPolicy",
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
    from pyxsd.extensions import Extensions
    from pyxsd.nodes import XMLNode
    from pyxsd.schema import Schema, compile, parse
    from pyxsd.xml_policy import XmlLimits, XmlParserPolicy


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
    if name == "Extensions":
        from pyxsd.extensions import Extensions

        return Extensions
    if name == "Schema":
        from pyxsd.schema import Schema

        return Schema
    if name == "XMLNode":
        from pyxsd.nodes import XMLNode

        return XMLNode
    if name in ("XmlLimits", "XmlParserPolicy"):
        from pyxsd import xml_policy

        return getattr(xml_policy, name)
    if name == "compile":
        from pyxsd.schema import compile

        return compile
    if name == "parse":
        from pyxsd.schema import parse

        return parse
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
