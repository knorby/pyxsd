"""Helpers for attributes in the XML Schema instance namespace.

``xsi:nil`` (nillable elements) and ``xsi:type`` (runtime type
dispatch) live in the ``http://www.w3.org/2001/XMLSchema-instance``
namespace. ElementTree expands that namespace on attribute names, so
instance parsing must look for the expanded form; the raw ``xsi:type``
spelling is also accepted for documents that kept an unbound prefix.
"""

import re

XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"

XSI_TYPE = f"{{{XSI_NAMESPACE}}}type"
XSI_NIL = f"{{{XSI_NAMESPACE}}}nil"

_TRUE = re.compile(r"^(true|1)$", re.IGNORECASE)


def xsi_attr_key(attr):
    """Maps an XSI-namespace attribute name to its display spelling.

    ElementTree expands ``xsi:nil`` to Clark notation
    (``{namespace}nil``) when the document declares the namespace;
    documents that kept a raw ``xsi:`` prefix are passed through.
    The display spelling is what the writers emit and what
    instance bookkeeping keys on.
    """
    if attr in (XSI_TYPE, "xsi:type"):
        return "xsi:type"
    if attr in (XSI_NIL, "xsi:nil"):
        return "xsi:nil"
    return attr


def xsi_type_name(elementTag):
    """Returns the ``xsi:type`` value on an element, or ``None``."""
    value = elementTag.attrib.get(XSI_TYPE)
    if value is None:
        value = elementTag.attrib.get("xsi:type")
    return value


def xsi_nil_is_true(elementTag):
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
