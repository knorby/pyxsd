"""The text output method from Serialization 3.1 section 10."""

from __future__ import annotations

from pyxsd.xdm import XdmAtomic, XdmItem, XdmSequence
from pyxsd.xdm.nodes import XdmNode
from pyxsd.xslt.errors import XsltSerializationError
from pyxsd.xslt.serialization.parameters import SerializationParameters

__all__ = ["serialize_text"]


def _string_value(item: XdmItem) -> str:
    if isinstance(item, XdmAtomic):
        return item.lexical
    if isinstance(item, XdmNode):
        return item.string_value
    raise XsltSerializationError(
        f"the text output method cannot serialize a {type(item).__name__} item",
        code="SENR0001",
    )


def serialize_text(sequence: XdmSequence, parameters: SerializationParameters) -> str:
    """Return the text-method serialization of *sequence*."""
    separator = " " if parameters.item_separator is None else parameters.item_separator
    return separator.join(_string_value(item) for item in sequence)
