"""Helpers for attributes in the XML Schema instance namespace.

``xsi:nil`` (nillable elements) and ``xsi:type`` (runtime type
dispatch) live in the ``http://www.w3.org/2001/XMLSchema-instance``
namespace. ElementTree expands that namespace on attribute names, so
instance parsing must look for the expanded form; the raw ``xsi:type``
spelling is also accepted for documents that kept an unbound prefix.
"""

import re
from typing import Any

XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"

XSI_TYPE = f"{{{XSI_NAMESPACE}}}type"
XSI_NIL = f"{{{XSI_NAMESPACE}}}nil"

#: Clark names of every built-in xsi-namespace attribute declaration
#: (XSD 1.1 §3.2.7.2) mapped to their conventional display spellings.
_XSI_BUILTIN_KEYS = {
    XSI_TYPE: "xsi:type",
    "xsi:type": "xsi:type",
    XSI_NIL: "xsi:nil",
    "xsi:nil": "xsi:nil",
    f"{{{XSI_NAMESPACE}}}schemaLocation": "xsi:schemaLocation",
    "xsi:schemaLocation": "xsi:schemaLocation",
    f"{{{XSI_NAMESPACE}}}noNamespaceSchemaLocation": "xsi:noNamespaceSchemaLocation",
    "xsi:noNamespaceSchemaLocation": "xsi:noNamespaceSchemaLocation",
}

_TRUE = re.compile(r"^(true|1)$", re.IGNORECASE)

#: The lexical space of ``xs:boolean``; the built-in ``xsi:nil``
#: declaration (XSD 1.1 §3.2.7.2) is typed by it. Matching is
#: case-insensitive so the historical lenient reading of the attribute
#: is preserved; only a value outside the boolean vocabulary is
#: reported.
_NIL_VALUE = re.compile(r"^(true|false|1|0)$", re.IGNORECASE)


def xsi_attr_key(attr: str) -> str:
    """Maps an XSI-namespace attribute name to its display spelling.

    ElementTree expands ``xsi:nil`` to Clark notation
    (``{namespace}nil``) when the document declares the namespace;
    documents that kept a raw ``xsi:`` prefix are passed through.
    The display spelling is what the writers emit and what
    instance bookkeeping keys on. All four built-in declarations
    (``type``, ``nil``, ``schemaLocation``,
    ``noNamespaceSchemaLocation``) share the mapping, so a schema
    declaration in the xsi namespace matches the instance bookkeeping.
    """
    return _XSI_BUILTIN_KEYS.get(attr, attr)


def xsi_type_name(elementTag: Any) -> str | None:
    """Returns the ``xsi:type`` value on an element, or ``None``.

    The value is a QName, whose whitespace facet is *collapse*: the
    lexical form may be padded with whitespace and newlines (the SUN
    ``typeDef00601m1_p`` document wraps the value over several lines),
    which must not leak into the prefix the QName resolver sees.
    """
    value = elementTag.attrib.get(XSI_TYPE)
    if value is None:
        value = elementTag.attrib.get("xsi:type")
    if value is None:
        return None
    return " ".join(value.split())


def xsi_nil_is_true(elementTag: Any) -> bool:
    """Returns True when an element carries ``xsi:nil="true"``.

    ``xsi:nil="false"`` (or any other value) counts as no nil marker,
    matching the XSD 1.0 boolean treatment of the attribute.
    """
    value = elementTag.attrib.get(XSI_NIL)
    if value is None:
        value = elementTag.attrib.get("xsi:nil")
    if value is None:
        return False
    return value.strip().lower() in ("true", "1")


def invalid_xsi_nil_value(value: Any) -> str | None:
    """The value when ``xsi:nil`` is outside the boolean lexical space.

    The built-in ``xsi:nil`` attribute declaration is typed
    ``xs:boolean``, so its value must be ``true``/``false``/``1``/``0``
    regardless of any wildcard that admits the xsi namespace
    (wild042.n1). Surrounding whitespace is allowed (collapsed by the
    datatype's whiteSpace facet). Returns ``None`` for a valid value.
    """
    if value is None:
        return None
    if _NIL_VALUE.match(str(value).strip()):
        return None
    return str(value)
