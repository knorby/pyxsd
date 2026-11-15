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

XSLT 1.0 §2.5 requires a 1.0 processor to process a stylesheet whose
`xsl:stylesheet` version is not `1.0` in forwards-compatible mode:

    An element enables forwards-compatible mode for itself, its attributes,
    its descendants and their attributes if either it is an xsl:stylesheet
    element whose version attribute is not equal to 1.0, or it is a literal
    result element that has an xsl:version attribute whose value is not equal
    to 1.0, or it is a literal result element that does not have an
    xsl:version attribute and that is the document element of a stylesheet
    using the simplified syntax.  A literal result element that has an
    xsl:version attribute whose value is equal to 1.0 disables
    forwards-compatible mode for itself, its attributes, its descendants and
    their attributes.  (XSLT 1.0 §2.5)

In that mode the vocabulary check no longer rejects later-version attributes
or elements: §2.5 says an attribute XSLT 1.0 does not allow the element to
have "must be ignored", a top-level element XSLT 1.0 does not allow "must be
ignored along with its content", and an element XSLT 1.0 does not allow in a
template must not be an error unless instantiated, at which point fallback
applies (see :func:`forwards_compatible_instruction` and the compile path that
implements §15 Fallback).  A stylesheet with an effective version of exactly
`1.0` keeps the strict gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
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
    "forwards_compatible_declaration",
    "forwards_compatible_instruction",
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
    if attribute is None:
        return None
    # A source node hands back the attribute node; an effective element hands
    # back its value, so the declaration resolver serves both views.
    return attribute if isinstance(attribute, str) else attribute.string_value


def _version_is_forwards_compatible(value: str | None) -> bool:
    """Whether a ``version`` declaration enables forwards-compatible mode.

    XSLT 1.0 §2.5 compares the declared version numerically against ``1.0``;
    ``1`` and ``1.00`` therefore keep the strict gate.  A value that is not a
    Number is reported by the version validator before this gate runs, so it
    keeps the strict reading here rather than guessing a mode from invalid
    input.
    """
    if value is None:
        return False
    try:
        return Decimal(value) != Decimal("1.0")
    except (InvalidOperation, ValueError):  # pragma: no cover - validator runs first
        return False


def forwards_compatible_declaration(node: Any) -> tuple[bool, bool]:
    """The forwards-compatible state one element declares under §2.5.

    Returns ``(declares, enabled)``: ``declares`` is false when the element
    carries no declaration this rule recognizes, and ``enabled`` is the mode it
    starts for itself, its attributes, and its descendants.  Only an
    ``xsl:stylesheet``/``xsl:transform`` element and a literal result element
    can change the mode; XSLT 1.0 has no version attribute on any other XSLT
    element, so a later version's ``version`` attribute there is an unknown
    attribute of the forward-compatible reading and never a mode switch.

    The vocabulary gate walks the source document with this, and the compile
    path resolves each element's inherited mode with it, so the two never
    disagree about where §2.5's switch lives.  *node* is a raw node or an
    effective element: both expose ``name`` and ``attribute``.
    """
    name = node.name or ""
    if name.startswith(f"{{{XSLT_NS}}}"):
        local = name[len(XSLT_NS) + 2 :]
        if local not in ("stylesheet", "transform"):
            return False, False
        return True, _version_is_forwards_compatible(_attribute_value(node, "version"))
    declared = _attribute_value(node, f"{{{XSLT_NS}}}version")
    if declared is None:
        # §2.5's third clause enables the mode for a literal result element
        # without xsl:version at the document element of a simplified
        # stylesheet; a compiled module always carries that attribute
        # (XTSE0150 otherwise), so the clause has no reachable state here.
        return False, False
    return True, _version_is_forwards_compatible(declared)


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


def forwards_compatible_instruction(
    local_name: str,
    options: Any,
    forwards_compatible: bool,
) -> bool:
    """Whether one XSLT instruction is processed in forwards-compatible mode.

    The compile path calls this before an XSLT-namespace instruction is
    compiled, so an instruction XSLT 1.0 does not define never gains the
    later language's semantics under the native 1.0 profile.  *options* is the
    compilation's ``ProfileOptions``-shaped profile and *forwards_compatible*
    the §2.5 mode resolved for the instruction's nearest declaration site (see
    :func:`forwards_compatible_declaration`): a non-1.0 processor profile, a
    1.0 vocabulary element, or an element outside the mode keeps the ordinary
    compile path (and therefore the gate's strict reading when the stylesheet
    is not forwarded).
    """
    # `LanguageProfile.from_options(options).legacy` owns this identity.
    # Comparing the declared profile name here duplicates that rule on purpose:
    # this predicate runs once per XSLT instruction of every compilation, and
    # building a profile per instruction costs more than the comparison saves.
    if getattr(options, "processor_profile", None) != "xslt-1.0":
        return False
    if local_name in XSLT10_ELEMENTS:
        return False
    return forwards_compatible


def _visit(
    node: Any,
    *,
    uri: str | None,
    profile: LanguageProfile,
    forwards_compatible: bool,
) -> None:
    if node.kind is not NodeKind.ELEMENT:
        return
    declares, enabled = forwards_compatible_declaration(node)
    current = enabled if declares else forwards_compatible
    name = node.name or ""
    if name.startswith(f"{{{XSLT_NS}}}"):
        local = name[len(XSLT_NS) + 2 :]
        location = node.source_location()
        if local not in XSLT10_ELEMENTS:
            if not current:
                raise XsltStaticError(
                    f"the xsl:{local} element is not defined by XSLT 1.0; a native "
                    f"XSLT {profile.processor_version} processor cannot compile it",
                    code="XTSE0010",
                    uri=uri,
                    location=location,
                )
            # §2.5: a top-level element XSLT 1.0 does not allow is ignored
            # with its content, and one in a template must not be an error
            # unless instantiated.  Rejecting it here would break both rules;
            # the compile path owns the fallback (see _compile_instruction).
        elif not current:
            _check_attributes(node, local, uri=uri, location=location)
            if local in ("template", "apply-templates"):
                _check_mode_tokens(node, uri=uri, location=location)
            if local == "output":
                _check_output_method(node, uri=uri, location=location)
        # In forwards-compatible mode §2.5 requires an attribute XSLT 1.0 does
        # not allow (and an optional attribute with a value XSLT 1.0 does not
        # allow) to be ignored, so none of those checks apply.
    for child in node.children:
        _visit(
            child,
            uri=uri,
            profile=profile,
            forwards_compatible=current,
        )


def assert_xslt10_language(module: Any, profile: LanguageProfile) -> None:
    """Gate one stylesheet module against the native XSLT 1.0 vocabulary."""
    uri = getattr(module, "identity", None)
    _visit(
        module.document.root_element,
        uri=uri,
        profile=profile,
        forwards_compatible=False,
    )
