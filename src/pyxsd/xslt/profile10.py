"""Native XSLT 1.0 language profile: vocabulary, output, and legacy values.

A native XSLT 1.0 processor compiles exactly the XSLT 1.0 language.  Every
XSLT-namespace element a stylesheet module carries is checked before the
compiled package is returned against the element/attribute vocabulary of the
XSLT 1.0 Recommendation (the syntax summaries in §7 through §16 and §5), and
the ``xsl:output`` method is restricted to Serialization 1.0's ``xml``,
``html``, and ``text``.  No XSLT 1.0 stylesheet schema is bundled with this
project; the Recommendation's own element and attribute lists are therefore the
source of truth rather than a hand-maintained denylist of later features.

A feature that arrived with XSLT 2.0 or 3.0 — schema imports and schema-aware
construction, packages, functions, grouping, regular expressions, result
documents, 2.0-only attributes and serialization parameters — is a static
error with the native profile named in the message, never a silently ignored
version check.  Elements outside the XSLT namespace (literal result elements
and extension instructions) are not gated here.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pyxsd.locations import IssueLocation
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.profiles import LanguageProfile

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

__all__ = [
    "XSLT10_ELEMENTS",
    "XSLT10_OUTPUT_ATTRIBUTES",
    "XSLT10_OUTPUT_METHODS",
    "assert_xslt10_language",
]

#: The XSLT 1.0 output methods (XSLT 1.0 §16).  ``xhtml`` and the 2.0/3.0
#: methods are not XSLT 1.0 vocabulary.
XSLT10_OUTPUT_METHODS = frozenset({"xml", "html", "text"})

#: The ``xsl:output`` attributes defined by XSLT 1.0 §16.
XSLT10_OUTPUT_ATTRIBUTES = frozenset(
    {
        "cdata-section-elements",
        "doctype-public",
        "doctype-system",
        "encoding",
        "indent",
        "media-type",
        "method",
        "omit-xml-declaration",
        "standalone",
        "version",
    }
)

#: One entry per XSLT 1.0 element, carrying the unqualified and XSLT-namespace
#: attributes its syntax summary permits.  Namespace declarations, ``xml:*``
#: attributes, and attributes in foreign namespaces are outside this table.
XSLT10_ATTRIBUTES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # Top-level elements.
        "stylesheet": frozenset(
            {"version", "extension-element-prefixes", "exclude-result-prefixes", "id"}
        ),
        "transform": frozenset(
            {"version", "extension-element-prefixes", "exclude-result-prefixes", "id"}
        ),
        "template": frozenset({"match", "name", "priority", "mode"}),
        "variable": frozenset({"name", "select"}),
        "param": frozenset({"name", "select"}),
        "include": frozenset({"href"}),
        "import": frozenset({"href"}),
        "strip-space": frozenset({"elements"}),
        "preserve-space": frozenset({"elements"}),
        "output": XSLT10_OUTPUT_ATTRIBUTES,
        "key": frozenset({"name", "match", "use"}),
        "decimal-format": frozenset(
            {
                "name",
                "decimal-separator",
                "grouping-separator",
                "infinity",
                "minus-sign",
                "NaN",
                "percent",
                "per-mille",
                "zero-digit",
                "digit",
                "pattern-separator",
            }
        ),
        "attribute-set": frozenset({"name", "use-attribute-sets"}),
        "namespace-alias": frozenset({"stylesheet-prefix", "result-prefix"}),
        # Instruction elements.
        "apply-templates": frozenset({"select", "mode"}),
        "call-template": frozenset({"name"}),
        "apply-imports": frozenset(),
        "for-each": frozenset({"select"}),
        "value-of": frozenset({"select", "disable-output-escaping"}),
        "copy-of": frozenset({"select"}),
        "number": frozenset(
            {
                "level",
                "count",
                "from",
                "value",
                "format",
                "lang",
                "letter-value",
                "grouping-separator",
                "grouping-size",
            }
        ),
        "choose": frozenset(),
        "when": frozenset({"test"}),
        "otherwise": frozenset(),
        "if": frozenset({"test"}),
        "text": frozenset({"disable-output-escaping"}),
        "copy": frozenset({"use-attribute-sets"}),
        "with-param": frozenset({"name", "select"}),
        "sort": frozenset({"select", "lang", "data-type", "order", "case-order"}),
        "message": frozenset({"terminate"}),
        "fallback": frozenset(),
        "processing-instruction": frozenset({"name"}),
        "comment": frozenset(),
        "element": frozenset({"name", "namespace", "use-attribute-sets"}),
        "attribute": frozenset({"name", "namespace"}),
    }
)

#: The concrete XSLT 1.0 element vocabulary.
XSLT10_ELEMENTS = frozenset(XSLT10_ATTRIBUTES)


def _attribute_triples(node: Any) -> list[tuple[str | None, str, str]]:
    triples: list[tuple[str | None, str, str]] = []
    for attribute in node.attributes:
        name = attribute.name or ""
        if name.startswith("{"):
            namespace, _, local = name[1:].partition("}")
            triples.append((namespace, local, attribute.string_value))
        else:
            triples.append((None, name, attribute.string_value))
    return triples


def _attribute_value(node: Any, local: str) -> str | None:
    attribute = node.attribute(local)
    return None if attribute is None else attribute.string_value


def _check_attributes(
    node: Any,
    local: str,
    *,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    allowed = XSLT10_ATTRIBUTES[local]
    for namespace, name, _value in _attribute_triples(node):
        if namespace in (None, XSLT_NS) and name not in allowed:
            raise XsltStaticError(
                f"the attribute {name!r} is not defined for xsl:{local} by XSLT 1.0; "
                "a native XSLT 1.0 processor cannot compile it",
                code="XTSE0090",
                uri=uri,
                location=location,
            )


def _check_mode_tokens(
    node: Any,
    *,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    """Reject the XSLT 2.0/3.0 ``#`` mode tokens (XSLT 1.0 modes are QNames)."""
    value = _attribute_value(node, "mode")
    if value is None:
        return
    for token in value.split():
        if token.startswith("#"):
            raise XsltStaticError(
                f"the mode token {token!r} is not defined by XSLT 1.0; "
                "a native XSLT 1.0 processor names modes with a QName",
                code="XTSE0550",
                uri=uri,
                location=location,
            )


def _check_output_method(
    node: Any,
    *,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    value = _attribute_value(node, "method")
    if value is None:
        return
    candidate = value.strip()
    if candidate.startswith("Q{") or candidate.startswith("{") or ":" in candidate:
        supported = False
    else:
        # Output method names are NCNames and therefore case-sensitive.
        supported = candidate in XSLT10_OUTPUT_METHODS
    if not supported:
        raise XsltStaticError(
            f"the serialization method {value!r} is not a Serialization 1.0 "
            "output method; a native XSLT 1.0 processor supports only "
            "xml, html, and text",
            code="XTSE0020",
            uri=uri,
            location=location,
        )


def _visit(
    node: Any,
    *,
    uri: str | None,
    profile: LanguageProfile,
) -> None:
    if node.kind is not NodeKind.ELEMENT:
        return
    name = node.name or ""
    if name.startswith(f"{{{XSLT_NS}}}"):
        local = name[len(XSLT_NS) + 2 :]
        location = node.source_location()
        if local not in XSLT10_ELEMENTS:
            raise XsltStaticError(
                f"the xsl:{local} element is not defined by XSLT 1.0; a native "
                f"XSLT {profile.processor_version} processor cannot compile it",
                code="XTSE0010",
                uri=uri,
                location=location,
            )
        _check_attributes(node, local, uri=uri, location=location)
        if local in ("template", "apply-templates"):
            _check_mode_tokens(node, uri=uri, location=location)
        if local == "output":
            _check_output_method(node, uri=uri, location=location)
    for child in node.children:
        _visit(child, uri=uri, profile=profile)


def assert_xslt10_language(module: Any, profile: LanguageProfile) -> None:
    """Gate one stylesheet module against the native XSLT 1.0 vocabulary."""
    uri = getattr(module, "identity", None)
    _visit(module.document.root_element, uri=uri, profile=profile)
