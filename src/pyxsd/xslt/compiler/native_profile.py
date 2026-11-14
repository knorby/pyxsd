"""Static feature gate for native pre-3.0 processor profiles.

A native XSLT 2.0 processor compiles exactly the XSLT 2.0 element
vocabulary.  Every XSLT-namespace element a stylesheet module carries is
checked against that vocabulary before the compiled package is returned, so
a feature that arrived with XSLT 3.0 — packages, ``xsl:evaluate``, the
higher-order instructions, 3.0-only attributes and serialization parameters
— is a static error with the profile named in the message, never a silently
ignored version check.

The gate is profile-owned: it runs only when the compiled package's
profile is a native pre-3.0 processor, and the vocabulary it enforces is
the language definition, not an implementation preference.  Elements
outside the XSLT namespace (literal result elements, extension
instructions) are not gated here.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from pyxsd.locations import IssueLocation
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xslt.compiler.context import ProfileOptions
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.profiles import XSLT20_REGEX_FLAGS, LanguageProfile

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

__all__ = ["assert_native_profile_language"]

#: Every element the XSLT 2.0 Recommendation defines, declarations and
#: instructions together (XSLT 2.0 Appendix D).  An XSLT-namespace element
#: outside this set under a native 2.0 processor is a feature from a later
#: version of the language.
XSLT20_ELEMENTS = frozenset(
    {
        "analyze-string",
        "apply-imports",
        "apply-templates",
        "attribute",
        "attribute-set",
        "call-template",
        "character-map",
        "choose",
        "comment",
        "copy",
        "copy-of",
        "decimal-format",
        "document",
        "element",
        "fallback",
        "for-each",
        "for-each-group",
        "function",
        "if",
        "import",
        "import-schema",
        "include",
        "key",
        "matching-substring",
        "message",
        "namespace",
        "namespace-alias",
        "next-match",
        "non-matching-substring",
        "number",
        "otherwise",
        "output",
        "output-character",
        "param",
        "perform-sort",
        "preserve-space",
        "processing-instruction",
        "result-document",
        "sequence",
        "sort",
        "strip-space",
        "stylesheet",
        "template",
        "text",
        "transform",
        "value-of",
        "variable",
        "when",
        "with-param",
    }
)

#: The Serialization 2.0 output methods (XSLT 2.0 §20).  The core methods
#: are these unprefixed NCNames; every other name selects an output method
#: this processor does not implement for the profile.
XSLT20_OUTPUT_METHODS = frozenset({"xml", "xhtml", "html", "text"})

#: Serialization attributes that arrived after XSLT 2.0.  They are rejected
#: wherever an XSLT-namespace output element carries them.
XSLT30_ONLY_OUTPUT_ATTRIBUTES = frozenset(
    {
        "allow-duplicate-names",
        "build-tree",
        "html-version",
        "item-separator",
        "json-node-output-method",
        "parameter-document",
        "suppress-indentation",
    }
)

#: Standard XSLT attributes that arrived with XSLT 3.0.
XSLT30_ONLY_STANDARD_ATTRIBUTES = frozenset(
    {
        "default-mode",
        "expand-text",
        "static",
        "streamable",
        "visibility",
    }
)

#: Element-specific attributes that arrived with XSLT 3.0.  Every name is
#: absent from the XSLT 2.0 syntax summary of its element, so a native 2.0
#: processor reports it as an unknown attribute rather than compiling it.
XSLT30_ONLY_ELEMENT_ATTRIBUTES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "copy": frozenset({"copy-accumulators"}),
        "copy-of": frozenset({"copy-accumulators"}),
        "for-each-group": frozenset({"bind-group", "bind-grouping-key", "composite"}),
        "key": frozenset({"composite"}),
        "number": frozenset({"start-at"}),
    }
)


def _element_local(node: Any) -> str:
    name = node.name or ""
    return name.rpartition("}")[2] if name.startswith("{") else name


def _attribute_triples(node: Any) -> list[tuple[str | None, str, str]]:
    """Return one (namespace, local, value) triple per attribute of *node*."""
    triples: list[tuple[str | None, str, str]] = []
    for attribute in node.attributes:
        name = attribute.name or ""
        if name.startswith("{"):
            namespace, _, local = name[1:].partition("}")
            triples.append((namespace, local, attribute.string_value))
        else:
            triples.append((None, name, attribute.string_value))
    return triples


def _constant_avt_value(value: str) -> str | None:
    """Return the constant string of a one-part literal AVT, if it is one."""
    if not (value.startswith("{") and value.endswith("}") and "{" not in value[1:-1]):
        return None
    inner = value[1:-1].strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in ("'", '"'):
        return inner[1:-1]
    return None


def _is_unsupported_method(value: str) -> bool:
    """Whether a static method value names a method outside Serialization 2.0."""
    candidate = value.strip()
    if candidate.startswith("Q{") or candidate.startswith("{"):
        return True
    if ":" in candidate:
        return True
    # Output method names are NCNames and therefore case-sensitive.
    return candidate not in XSLT20_OUTPUT_METHODS


def _attribute_value(node: Any, local: str) -> str | None:
    attribute = node.attribute(local)
    return None if attribute is None else attribute.string_value


def _check_method_attribute(
    node: Any,
    *,
    uri: str | None,
    location: IssueLocation | None,
    allow_avt: bool,
) -> None:
    value = _attribute_value(node, "method")
    if value is None:
        return
    candidate = value.strip()
    if allow_avt and ("{" in candidate or "}" in candidate):
        constant = _constant_avt_value(candidate)
        if constant is None:
            # A dynamic attribute value template is a runtime decision;
            # XTDE0030 reports an unsupported effective value then.
            return
        candidate = constant
    if _is_unsupported_method(candidate):
        raise XsltStaticError(
            f"the serialization method {value!r} is not a Serialization 2.0 "
            "output method; a native XSLT 2.0 processor supports only "
            "xml, xhtml, html, and text",
            code="XTSE0020",
            uri=uri,
            location=location,
        )


def _check_output_attributes(node: Any, *, uri: str | None, location: IssueLocation | None) -> None:
    local = _element_local(node)
    rejected = set(XSLT30_ONLY_OUTPUT_ATTRIBUTES)
    if local == "output":
        # ``format`` is not an XSLT 2.0 ``xsl:output`` attribute; a named
        # output definition is selected with ``xsl:output/@name``.  On
        # ``xsl:result-document`` it is the XSLT 2.0 way to select one.
        rejected.add("format")
    for namespace, attribute_local, _value in _attribute_triples(node):
        if attribute_local in rejected and namespace in (None, XSLT_NS):
            raise XsltStaticError(
                f"the attribute {attribute_local!r} is not defined by XSLT 2.0; a "
                "native XSLT 2.0 processor cannot compile it",
                code="XTSE0090",
                uri=uri,
                location=location,
            )
    _check_method_attribute(node, uri=uri, location=location, allow_avt=local == "result-document")


def _check_analyze_string_flags(
    node: Any, *, uri: str | None, location: IssueLocation | None
) -> None:
    flags = _attribute_value(node, "flags")
    if flags is None:
        return
    candidate = flags.strip()
    if "{" in candidate or "}" in candidate:
        constant = _constant_avt_value(candidate)
        if constant is None:
            return
        candidate = constant
    if any(token not in XSLT20_REGEX_FLAGS for token in candidate.split()):
        raise XsltStaticError(
            f"the regular-expression flags {flags!r} are not XSLT 2.0 flags (i, m, s, x)",
            code="XTSE0020",
            uri=uri,
            location=location,
        )


def _check_standard_attributes(
    node: Any, *, uri: str | None, location: IssueLocation | None
) -> None:
    local = _element_local(node)
    rejected = set(XSLT30_ONLY_STANDARD_ATTRIBUTES)
    rejected |= XSLT30_ONLY_ELEMENT_ATTRIBUTES.get(local, frozenset())
    for namespace, attribute_local, _value in _attribute_triples(node):
        if attribute_local in rejected and namespace in (None, XSLT_NS):
            raise XsltStaticError(
                f"the attribute {attribute_local!r} is not defined by XSLT 2.0; a "
                "native XSLT 2.0 processor cannot compile it",
                code="XTSE0090",
                uri=uri,
                location=location,
            )


def _visit(node: Any, *, uri: str | None) -> None:
    if node.kind is not NodeKind.ELEMENT:
        return
    name = node.name or ""
    if name.startswith(f"{{{XSLT_NS}}}"):
        local = name[len(XSLT_NS) + 2 :]
        location = node.source_location()
        if local not in XSLT20_ELEMENTS:
            raise XsltStaticError(
                f"the xsl:{local} element is not defined by XSLT 2.0; a native "
                "XSLT 2.0 processor cannot compile it",
                code="XTSE0010",
                uri=uri,
                location=location,
            )
        if local in ("output", "result-document"):
            _check_output_attributes(node, uri=uri, location=location)
        if local == "analyze-string":
            _check_analyze_string_flags(node, uri=uri, location=location)
        _check_standard_attributes(node, uri=uri, location=location)
    for child in node.children:
        _visit(child, uri=uri)


def assert_native_profile_language(module: Any, profile: ProfileOptions) -> None:
    """Gate one stylesheet module against the native profile's vocabulary.

    The gate runs only for native pre-3.0 processor profiles; a 3.0
    processor applying compatibility rules to an early effective version
    never sees it.  *module* is the compiled module whose ``document`` and
    ``identity`` describe one stylesheet module of the package closure.
    """
    resolved = LanguageProfile.from_options(profile)
    if Decimal(resolved.processor_version) >= Decimal("3.0"):
        return
    uri = getattr(module, "identity", None)
    _visit(module.document.root_element, uri=uri)
