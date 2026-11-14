"""The XSLT 1.0 legacy value model, isolated from XDM 3.1.

XSLT 1.0 has two value forms that XDM 3.1 does not define: result tree
fragments and node-sets.  A result tree fragment is the value of a variable,
parameter, or with-parameter that is initialized by content rather than by a
``select`` expression; it behaves as a node-set with one root node for string
and boolean conversion, and it may not be used where a node-set is required
(``count($fragment)``, ``$fragment/item``, ``xsl:apply-templates``).

This module keeps those values and their XPath 1.0 conversions out of the XDM
3.1 value classes: an :class:`XdmAtomic` stays an XDM atomic, and an XSLT 1.0
processor never projects a legacy value onto a sequence.  Only the XPath 1.0
engine and the XSLT 1.0 profile import it.

The number conversions are the XPath 1.0 (IEEE-754 double) rules: ``number('')``
is ``NaN``, the string ``Infinity`` and ``-Infinity`` are the only infinite
spellings, a number is written without a redundant fractional part, and
``round()`` rounds half toward positive infinity.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from pyxsd.xdm.nodes import XdmDocument, XdmNode
from pyxsd.xdm.values import XdmAtomic, XdmItem, XdmSequence

__all__ = [
    "LegacyNodeSetError",
    "ResultTreeFragment",
    "is_legacy_number",
    "legacy_boolean",
    "legacy_boolean_sequence",
    "legacy_node_set",
    "legacy_number",
    "legacy_number_from_string",
    "legacy_number_sequence",
    "legacy_round",
    "legacy_string",
    "legacy_string_sequence",
    "number_to_string",
]

_XML_WHITESPACE = " \t\r\n"
#: The XPath 1.0 ``Number`` production: no exponent, no leading plus sign, and
#: no ``Infinity``/``NaN`` spelling.
_NUMBER = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


class LegacyNodeSetError(TypeError):
    """A result tree fragment was used where a node-set is required.

    XSLT 1.0 §11.1 permits string and boolean conversion of a result tree
    fragment but no node-set operation.  The error carries the XPath type-error
    code so an XSLT boundary can report it unchanged.
    """

    code = "XPTY0004"


@dataclass(frozen=True, slots=True)
class ResultTreeFragment(XdmItem):
    """One XSLT 1.0 result tree fragment with a distinct value kind."""

    document: XdmDocument

    def __post_init__(self) -> None:
        if not isinstance(self.document, XdmDocument):
            raise TypeError("a result tree fragment requires an XDM document node")

    @property
    def string_value(self) -> str:
        """The string-value of the fragment's root node."""
        return self.document.string_value

    def __str__(self) -> str:
        return self.string_value


def legacy_node_set(nodes: Iterable[XdmNode]) -> tuple[XdmNode, ...]:
    """Return *nodes* as a XPath 1.0 node-set in document order.

    Identity is XDM node identity, so a node reached through two paths appears
    once.  The result is ordered by document order, as every XPath 1.0
    location path and union is.
    """
    unique: dict[int, XdmNode] = {}
    for node in nodes:
        if not isinstance(node, XdmNode):
            raise TypeError("a node-set can contain only XDM nodes")
        unique.setdefault(id(node), node)
    return tuple(sorted(unique.values(), key=lambda node: node.document_order))


def number_to_string(value: float) -> str:
    """Return the XPath 1.0 string form of one IEEE-754 double."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    if value.is_integer():
        return str(int(value))
    text = repr(value)
    if "e" in text or "E" in text:
        # XPath 1.0 §4.2 represents numbers in decimal form; expand the
        # shortest round-trip spelling instead of leaking exponent notation.
        return format(Decimal(text), "f")
    return text


def legacy_number_from_string(text: str) -> float:
    """Convert one string to a number using the XPath 1.0 grammar."""
    candidate = text.strip(_XML_WHITESPACE)
    if _NUMBER.fullmatch(candidate) is None:
        return math.nan
    try:
        return float(candidate)
    except ValueError:  # pragma: no cover - the grammar already guarantees a float
        return math.nan


def legacy_round(value: float) -> float:
    """XPath 1.0 ``round()``: half toward positive infinity, preserving zero."""
    if math.isnan(value) or math.isinf(value):
        return value
    if -0.5 <= value < 0:
        return -0.0
    return float(math.floor(value + 0.5))


def _atomic_string(item: XdmAtomic) -> str:
    value = item.value
    if isinstance(value, float):
        return number_to_string(value)
    if isinstance(value, Decimal):
        logical = format(value, "f")
        return logical.rstrip("0").rstrip(".") if "." in logical else logical
    if isinstance(value, int):
        return str(value)
    return item.lexical


def legacy_string(value: XdmItem) -> str:
    """Return the XPath 1.0 string conversion of one legacy value."""
    if isinstance(value, ResultTreeFragment):
        return value.string_value
    if isinstance(value, XdmNode):
        return value.string_value
    if isinstance(value, XdmAtomic):
        return _atomic_string(value)
    raise TypeError(f"{type(value).__name__} has no XSLT 1.0 string value")


def legacy_string_sequence(sequence: XdmSequence) -> str:
    """Return the string conversion of one XSLT 1.0 expression result.

    In XPath 1.0 every expression produces a single value; a node-set converts
    through the string-value of its first node in document order.
    """
    if not sequence:
        return ""
    if all(isinstance(item, XdmNode) for item in sequence):
        ordered = legacy_node_set(cast(Iterable[XdmNode], sequence))
        return "" if not ordered else legacy_string(ordered[0])
    return legacy_string(sequence[0])


def legacy_boolean(value: XdmItem) -> bool:
    """Return the XPath 1.0 boolean conversion of one legacy value."""
    if isinstance(value, ResultTreeFragment):
        # A result tree fragment is a node-set with one root node, so it is
        # always true even when its string-value is empty.
        return True
    if isinstance(value, XdmNode):
        return True
    if isinstance(value, XdmAtomic):
        raw = value.value
        if isinstance(raw, (int, float, Decimal)):
            return not (isinstance(raw, float) and math.isnan(raw)) and raw != 0
        return bool(_atomic_string(value))
    raise TypeError(f"{type(value).__name__} has no XSLT 1.0 boolean value")


def legacy_boolean_sequence(sequence: XdmSequence) -> bool:
    """Return the boolean conversion of one XSLT 1.0 expression result."""
    return False if not sequence else legacy_boolean(sequence[0])


def _number_of_atomic(item: XdmAtomic) -> float:
    raw = item.value
    if isinstance(raw, (int, float, Decimal)):
        return float(raw)
    return legacy_number_from_string(_atomic_string(item))


def legacy_number(value: XdmItem) -> float:
    """Return the XPath 1.0 number conversion of one legacy value."""
    if isinstance(value, ResultTreeFragment):
        return legacy_number_from_string(value.string_value)
    if isinstance(value, XdmNode):
        return legacy_number_from_string(value.string_value)
    if isinstance(value, XdmAtomic):
        return _number_of_atomic(value)
    raise TypeError(f"{type(value).__name__} has no XSLT 1.0 number value")


def legacy_number_sequence(sequence: XdmSequence) -> float:
    """Return the number conversion of one XSLT 1.0 expression result."""
    return math.nan if not sequence else legacy_number(sequence[0])


def is_legacy_number(value: Any) -> bool:
    """Whether *value* is an XPath 1.0 numeric value (booleans excluded)."""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
