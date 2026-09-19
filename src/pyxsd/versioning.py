"""XSD 1.1 conditional inclusion (the ``vc:*`` schema-versioning attributes).

Every schema document is pre-processed before any other schema handling
(XSD 1.1 Part 1 §4.2.2): an element carrying one of the six versioning
attributes is tested against the processor's declared XSD version and its
supported built-in type and facet sets, and an element whose test fails is
removed together with its entire subtree. The element-representative run
therefore never sees the excluded declarations.

This module owns the feature tables the test consults. They are built from
the datatype lattice itself so a type cannot be advertised as available
while pyxsd cannot actually support it; the facet table is likewise the
single source the rest of the schema phase shares.
"""

from __future__ import annotations

import decimal
import re
import xml.etree.ElementTree as ET

from pyxsd import xsd_data_types
from pyxsd.namespaces import (
    XSD_NS,
    NamespaceContext,
    NamespaceError,
    clark,
    local_name,
    namespace_of,
)
from pyxsd.validation import ValidationReport
from pyxsd.xsd_data_types import TypeList, XsdDataType

#: Namespace of the versioning attributes (XSD 1.1 §1.3.1.3).
VC_NS = "http://www.w3.org/2007/XMLSchema-versioning"

#: The XSD version this processor claims; XSD 1.1 §4.2.2's variable ``V``.
PROCESSOR_VERSION = decimal.Decimal("1.1")

MIN_VERSION = clark(VC_NS, "minVersion")
MAX_VERSION = clark(VC_NS, "maxVersion")
TYPE_AVAILABLE = clark(VC_NS, "typeAvailable")
TYPE_UNAVAILABLE = clark(VC_NS, "typeUnavailable")
FACET_AVAILABLE = clark(VC_NS, "facetAvailable")
FACET_UNAVAILABLE = clark(VC_NS, "facetUnavailable")


def _supported_builtin_type_names() -> frozenset[str]:
    """The local names of the built-in XSD types pyxsd supports.

    Derived from the classes' own declared ``name`` attributes, exactly as
    the element-representative table is, so the availability test can never
    drift from the datatype lattice (and stays honest when a type such as
    ``xs:dateTimeStamp`` is not implemented yet).
    """
    return frozenset(
        klass.name
        for klass in vars(xsd_data_types).values()
        if isinstance(klass, type)
        and issubclass(klass, XsdDataType)
        and klass is not XsdDataType
        and "name" in klass.__dict__
        and klass is not TypeList
    )


#: Built-in type local names automatically known to the processor. A
#: ``vc:typeAvailable`` naming any XSD-namespace type outside this set (or a
#: type in any other namespace) is treated as unavailable.
SUPPORTED_TYPE_NAMES: frozenset[str] = _supported_builtin_type_names()

#: Facet local names accepted and enforced by the schema phase. Kept here so
#: the conditional-inclusion test and facet legality share one table; a 1.1
#: facet is added only when enforcement lands.
SUPPORTED_FACET_NAMES: frozenset[str] = frozenset(
    {
        "length",
        "minLength",
        "maxLength",
        "minInclusive",
        "maxInclusive",
        "minExclusive",
        "maxExclusive",
        "totalDigits",
        "fractionDigits",
        "pattern",
        "enumeration",
        "whiteSpace",
        "assertion",
    }
)

#: ``xs:decimal``'s lexical space: an optional sign, then a digit string that
#: may carry a fractional part. No exponent, no ``NaN``/``INF``.
_DECIMAL_RE = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)\Z")


def _parse_version(text: str) -> decimal.Decimal | None:
    """Parse a ``vc:minVersion``/``vc:maxVersion`` lexical decimal value."""
    value = text.strip()
    if _DECIMAL_RE.match(value) is None:
        return None
    return decimal.Decimal(value)


def _expand_qname(element: ET.Element, token: str, namespaces: NamespaceContext) -> str | None:
    """Expand one QName token to a Clark name, or ``None`` if unusable.

    The lexical form must be a QName (the ``xs:QName`` list item); an
    unprefixed token resolves in no namespace, matching QName-valued
    attribute content.
    """
    if xsd_data_types.QName._pattern.fullmatch(token) is None:
        return None
    try:
        return namespaces.resolve(element, token, is_attribute=True)
    except NamespaceError:
        return None


def _type_available(name: str) -> bool:
    return namespace_of(name) == XSD_NS and local_name(name) in SUPPORTED_TYPE_NAMES


def _facet_available(name: str) -> bool:
    return namespace_of(name) == XSD_NS and local_name(name) in SUPPORTED_FACET_NAMES


def _availability_ignores(
    element: ET.Element,
    namespaces: NamespaceContext,
    report: ValidationReport,
) -> bool:
    """Whether an availability attribute rejects *element* (§4.2.2 clauses 1-4).

    An ``...Available`` attribute rejects the element when any listed item is
    unavailable; an ``...Unavailable`` attribute rejects it when every listed
    item is available (the empty list is vacuously all-available, so it
    rejects — the mirror of the empty ``...Available`` list, which keeps the
    element). A lexically invalid or unresolvable QName is reported as
    ``versioning-invalid`` and its condition is skipped; the document is
    already in error, and guessing a verdict would be worse than leaving the
    element for the remaining checks.
    """
    clauses = (
        (TYPE_AVAILABLE, _type_available, False),
        (TYPE_UNAVAILABLE, _type_available, True),
        (FACET_AVAILABLE, _facet_available, False),
        (FACET_UNAVAILABLE, _facet_available, True),
    )
    for attribute, is_available, ignore_when_all_available in clauses:
        raw = element.get(attribute)
        if raw is None:
            continue
        usable = True
        availability: list[bool] = []
        for token in raw.split():
            expanded = _expand_qname(element, token, namespaces)
            if expanded is None:
                report.add_error(
                    f"the value {raw!r} of vc:{local_name(attribute)} contains "
                    f"an illegal QName {token!r}",
                    code="versioning-invalid",
                    element=local_name(element.tag),
                    phase="schema",
                )
                usable = False
                continue
            availability.append(is_available(expanded))
        if not usable:
            continue
        if ignore_when_all_available:
            if all(availability):
                return True
        elif not all(availability):
            return True
    return False


def _is_ignored(
    element: ET.Element,
    namespaces: NamespaceContext,
    report: ValidationReport,
) -> bool:
    """Whether *element* is removed by §4.2.2's conditions.

    Version selectors are evaluated first so an element excluded by version
    is removed before any of its attributes (including an illegal
    availability value) are examined — the pre-processed document does not
    contain them.
    """
    version_selector_ignores = False
    if MIN_VERSION in element.attrib:
        minimum = _parse_version(element.get(MIN_VERSION, ""))
        if minimum is None:
            report.add_error(
                f"vc:minVersion value {element.get(MIN_VERSION)!r} is not a valid xs:decimal",
                code="versioning-invalid",
                element=local_name(element.tag),
                phase="schema",
            )
        elif minimum > PROCESSOR_VERSION:
            version_selector_ignores = True
    if not version_selector_ignores and MAX_VERSION in element.attrib:
        maximum = _parse_version(element.get(MAX_VERSION, ""))
        if maximum is None:
            report.add_error(
                f"vc:maxVersion value {element.get(MAX_VERSION)!r} is not a valid xs:decimal",
                code="versioning-invalid",
                element=local_name(element.tag),
                phase="schema",
            )
        elif maximum <= PROCESSOR_VERSION:
            version_selector_ignores = True
    if version_selector_ignores:
        return True
    return _availability_ignores(element, namespaces, report)


def _keep_schema_root_attributes(root: ET.Element) -> None:
    """Trims an ignored ``<xs:schema>`` per §4.2.2.

    Only ``targetNamespace``, ``vc:minVersion`` and ``vc:maxVersion``
    survive; the children are removed by the caller.
    """
    for key in list(root.attrib):
        if key not in (MIN_VERSION, MAX_VERSION, "targetNamespace"):
            del root.attrib[key]


def apply_conditional_inclusion(
    root: ET.Element,
    namespaces: NamespaceContext,
    report: ValidationReport,
) -> None:
    """Removes the elements a schema document's ``vc:*`` selectors exclude.

    Walks the tree top-down: an ignored element is detached with its whole
    subtree, and no descendant of an ignored element is examined. The
    ``<xs:schema>`` root is special (XSD 1.1 §4.2.2): if it is ignored the
    document is reduced to an empty schema that keeps only its target
    namespace and the version selectors, rather than the root being removed.
    """
    if _is_ignored(root, namespaces, report):
        for child in list(root):
            root.remove(child)
        _keep_schema_root_attributes(root)
        return
    _filter_children(root, namespaces, report)


def _filter_children(
    parent: ET.Element,
    namespaces: NamespaceContext,
    report: ValidationReport,
) -> None:
    for child in list(parent):
        if _is_ignored(child, namespaces, report):
            parent.remove(child)
        else:
            _filter_children(child, namespaces, report)
