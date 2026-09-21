"""XSD 1.0 vocabulary gate: report 1.1-only constructs in 1.0 mode.

Compiling a schema as XSD 1.0 restricts its vocabulary to XSD 1.0 (plus
the ``vc:*`` versioning attributes). This module is a lint, not a full 1.0
validation: it walks each schema document and reports, with code
``xsd11-construct``, the 1.1-only elements, attributes, and built-in types
it finds, so a user who asked for 1.0 mode is told plainly which
declarations the processor would otherwise accept. The default
``Schema.compile(xsd_version="1.1")`` runs no gate.

The check is deliberately name-based and local: it matches the 1.1-only
declarations by their local name on XSD-namespace elements, and a type
reference by the local part of its QName. That is sufficient for the
built-in 1.1 types (which are in the XSD namespace) and avoids coupling
the gate to the compositor's namespace resolution.
"""

from __future__ import annotations

from typing import Any

from pyxsd.namespaces import XSD_NS, local_name, namespace_of

#: XSD 1.1 elements with no XSD 1.0 equivalent.
XSD11_ONLY_ELEMENTS = frozenset({"assert", "assertion", "alternative", "openContent", "override"})

#: XSD 1.1 attributes (unqualified on XSD elements) with no 1.0 equivalent.
XSD11_ONLY_ATTRIBUTES = frozenset(
    {
        "notNamespace",
        "notQName",
        "defaultAttributes",
        "defaultAttributesApply",
        "inheritable",
        "xpathDefaultNamespace",
    }
)

#: XSD 1.1 built-in types that do not exist in XSD 1.0.
XSD11_ONLY_TYPE_NAMES = frozenset(
    {
        "dateTimeStamp",
        "dayTimeDuration",
        "yearMonthDuration",
        "anyAtomicType",
        "precisionDecimal",
    }
)

#: Attributes whose value is a QName (or whitespace-separated QNames) naming a type.
_TYPE_REF_ATTRIBUTES = ("type", "itemType", "memberTypes")


def _local_of_qname(value: str) -> str:
    """The local part of a lexical QName (``xs:dateTimeStamp`` → the type)."""
    return value.rsplit(":", 1)[-1]


def check_xsd10_vocabulary(schema_document_root: Any, report: Any) -> None:
    """Reports 1.1-only vocabulary found in *schema_document_root*.

    Every XSD-namespace element in the 1.1-only tables is reported, as is
    every attribute in the 1.1-only table on an XSD-namespace element and
    every ``type``/``itemType``/``memberTypes`` reference naming an XSD 1.1
    built-in type. Foreign-namespace and unqualified elements are left
    alone.
    """
    for element in schema_document_root.iter():
        tag = element.tag
        if not isinstance(tag, str):
            continue  # a comment or processing instruction
        in_xsd = namespace_of(tag) == XSD_NS
        if in_xsd:
            local = local_name(tag)
            if local in XSD11_ONLY_ELEMENTS:
                report.add_error(
                    f"{local} is an XSD 1.1 construct and is not allowed in a 1.0 schema",
                    code="xsd11-construct",
                    element=local,
                    phase="schema",
                )
        for name, value in element.attrib.items():
            if not isinstance(name, str):
                continue
            attribute = local_name(name)
            if in_xsd and attribute in XSD11_ONLY_ATTRIBUTES:
                report.add_error(
                    f"the {attribute} attribute is an XSD 1.1 construct and "
                    "is not allowed in a 1.0 schema",
                    code="xsd11-construct",
                    element=attribute,
                    phase="schema",
                )
            if in_xsd and attribute in _TYPE_REF_ATTRIBUTES and isinstance(value, str):
                for reference in value.split():
                    type_name = _local_of_qname(reference)
                    if type_name in XSD11_ONLY_TYPE_NAMES:
                        report.add_error(
                            f"the XSD 1.1 type {type_name} is not allowed in a 1.0 schema",
                            code="xsd11-construct",
                            element=type_name,
                            phase="schema",
                        )
