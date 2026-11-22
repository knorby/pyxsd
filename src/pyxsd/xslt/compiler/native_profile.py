"""Static feature gate for native pre-3.0 processor profiles.

A native XSLT 2.0 processor compiles exactly the XSLT 2.0 language
vocabulary.  Every XSLT-namespace element a stylesheet module carries is
checked before the compiled package is returned against the bundled XSLT 2.0
stylesheet schema: the element itself, every unqualified and XSLT-namespace
attribute it carries, and the mode-token vocabulary of ``@mode``.  A feature
that arrived with XSLT 3.0 — packages, ``xsl:evaluate``, the higher-order
instructions, 3.0-only attributes and serialization parameters, ``#unnamed``
— is a static error with the profile named in the message, never a silently
ignored version check.

The gate is profile-owned: it runs only when the compiled package's profile
is a native pre-3.0 processor.  It derives the vocabulary from
``schema-for-xslt20.xsd`` rather than a hand-maintained denylist, so the
general class of unknown attribute is covered and the schema's own attribute
declarations, complex-type inheritance, and simple-type enumerations decide
what XSLT 2.0 permits.  Elements outside the XSLT namespace (literal result
elements, extension instructions) are not gated here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib.resources import as_file
from importlib.resources import files as resource_files
from types import MappingProxyType
from typing import Any
from xml.etree import ElementTree

from pyxsd.locations import IssueLocation
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xslt.compiler.context import ProfileOptions
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.profiles import XSLT20_REGEX_FLAGS, LanguageProfile

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"

#: The bundled, packaged XSLT 2.0 stylesheet schema (the same resource the
#: advisory inspection profile uses).  It is the vocabulary source for this
#: gate; the Recommendation remains normative where the two might differ.
XSLT20_SCHEMA_RESOURCE = "schema-for-xslt20.xsd"

#: The bundled XSLT 3.0 stylesheet schema, used for the general XTSE0090
#: attribute check on a 3.0 processor.
XSLT30_SCHEMA_RESOURCE = "schema-for-xslt30.xsd"

#: Attributes the Recommendation defines that the bundled 3.0 schema omits, plus
#: the two public engine extension attributes.  Keeping this explicit means the
#: schema remains the default source of truth without rejecting supported syntax.
XSLT30_SCHEMA_SUPPLEMENTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "accumulator-rule": frozenset({"priority"}),
        "evaluate": frozenset({"xpath-version"}),
        "function": frozenset({"identity-sensitive"}),
    }
)

__all__ = ["assert_native_profile_language"]

#: The Serialization 2.0 output methods (XSLT 2.0 §20).  The core methods
#: are these unprefixed NCNames; every other name selects an output method
#: this processor does not implement for the profile.
XSLT20_OUTPUT_METHODS = frozenset({"xml", "xhtml", "html", "text"})


@dataclass(frozen=True, slots=True)
class _XSLT20Vocabulary:
    """The XSLT 2.0 element/attribute/mode vocabulary from the schema."""

    elements: frozenset[str]
    attributes: Mapping[str, frozenset[str]]
    mode_tokens: Mapping[str, frozenset[str]]


def _xsd_local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _xsd_name(name: str) -> str:
    return f"{{{XSD_NS}}}{name}"


def _local_name(value: str) -> str:
    return value.rpartition(":")[2]


def _element_type(element: Any, declarations: Mapping[str, Any]) -> Any:
    """Resolve one schema element's type: reference, inline type, or head."""
    reference = element.get("type")
    if reference:
        return reference
    inline = next(
        (child for child in element if _xsd_local(child.tag) == "complexType"),
        None,
    )
    if inline is not None:
        return inline
    # The bundle assigns every concrete XSLT element an inline type or a
    # substitution head that carries one (xsl:stylesheet -> xsl:transform).
    head = element.attrib["substitutionGroup"]
    return _element_type(declarations[_local_name(head)], declarations)


def _attribute_declarations(
    type_ref: Any,
    complex_types: Mapping[str, Any],
) -> dict[str, str | None]:
    """Collect one type's attribute declarations, following its base.

    The bundled schema expresses inheritance with ``complexContent`` or
    ``simpleContent`` extension/restriction; each branch is followed so the
    standard attributes reach every element.  A later declaration of the same
    name wins, matching XSD restriction behavior.
    """
    if isinstance(type_ref, str):
        return _attribute_declarations(
            complex_types[_local_name(type_ref)],
            complex_types,
        )
    declarations: dict[str, str | None] = {}
    for child in type_ref:
        tag = _xsd_local(child.tag)
        if tag == "attribute":
            declarations[child.attrib["name"]] = child.get("type")
        elif tag in ("complexContent", "simpleContent"):
            for inner in child:
                declarations.update(_attribute_declarations(inner.attrib["base"], complex_types))
                for attribute in inner:
                    if _xsd_local(attribute.tag) == "attribute":
                        declarations[attribute.attrib["name"]] = attribute.get("type")
    return declarations


def _simple_type_enumerations(
    type_ref: Any,
    simple_types: Mapping[str, Any],
) -> frozenset[str]:
    """Collect the ``#token`` vocabulary of one simple type."""
    if isinstance(type_ref, str):
        declared = simple_types.get(_local_name(type_ref))
        if declared is None:
            # An XSD built-in type (such as xs:NCName) has no enumerations in
            # this schema bundle; it contributes no special mode tokens.
            return frozenset()
        return _simple_type_enumerations(
            declared,
            simple_types,
        )
    values: set[str] = set()
    for node in type_ref.iter():
        tag = _xsd_local(node.tag)
        if tag == "enumeration" and node.get("value") is not None:
            values.add(node.get("value"))
        elif tag == "union":
            for member in (node.get("memberTypes") or "").split():
                values |= _simple_type_enumerations(member, simple_types)
    return frozenset(values)


@lru_cache(maxsize=2)
def _schema_vocabulary(resource_name: str) -> _XSLT20Vocabulary:
    """Load one bundled XSLT schema's element and attribute vocabulary."""
    resource = resource_files("pyxsd.xslt") / "resources" / resource_name
    with as_file(resource) as path:
        root = ElementTree.parse(path).getroot()
    complex_types: dict[str, Any] = {
        node.attrib["name"]: node for node in root.findall(_xsd_name("complexType"))
    }
    simple_types: dict[str, Any] = {
        node.attrib["name"]: node for node in root.findall(_xsd_name("simpleType"))
    }
    declarations: dict[str, Any] = {
        node.attrib["name"]: node for node in root.findall(_xsd_name("element"))
    }
    elements: set[str] = set()
    attributes: dict[str, frozenset[str]] = {}
    mode_tokens: dict[str, frozenset[str]] = {}
    for name, element in declarations.items():
        if element.get("abstract") in ("true", "1"):
            continue
        type_ref = _element_type(element, declarations)
        declared = _attribute_declarations(type_ref, complex_types)
        elements.add(name)
        attributes[name] = frozenset(declared)
        mode_type = declared.get("mode")
        if mode_type:
            mode_tokens[name] = _simple_type_enumerations(mode_type, simple_types)
    return _XSLT20Vocabulary(
        frozenset(elements),
        MappingProxyType(attributes),
        MappingProxyType(mode_tokens),
    )


def _xslt20_vocabulary() -> _XSLT20Vocabulary:
    """Load the XSLT 2.0 element, attribute, and mode vocabulary."""
    return _schema_vocabulary(XSLT20_SCHEMA_RESOURCE)


def _xslt30_vocabulary() -> _XSLT20Vocabulary:
    """Load the XSLT 3.0 element and attribute vocabulary."""
    return _schema_vocabulary(XSLT30_SCHEMA_RESOURCE)


def _attribute_text(attribute: Any) -> str:
    """One attribute's value from a raw or effective stylesheet element."""
    value = getattr(attribute, "string_value", None)
    return value if isinstance(value, str) else attribute.value


def _attribute_triples(node: Any) -> list[tuple[str | None, str, str]]:
    """Return one (namespace, local, value) triple per attribute of *node*."""
    triples: list[tuple[str | None, str, str]] = []
    for attribute in node.attributes:
        name = attribute.name or ""
        if name.startswith("{"):
            namespace, _, local = name[1:].partition("}")
            triples.append((namespace, local, _attribute_text(attribute)))
        else:
            triples.append((None, name, _attribute_text(attribute)))
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


def _check_attributes(
    node: Any,
    local: str,
    vocabulary: _XSLT20Vocabulary,
    *,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    """Reject an attribute the XSLT 2.0 schema does not declare on *local*."""
    allowed = vocabulary.attributes[local]
    for namespace, attribute_local, _value in _attribute_triples(node):
        if namespace in (None, XSLT_NS) and attribute_local not in allowed:
            raise XsltStaticError(
                f"the attribute {attribute_local!r} is not defined for xsl:{local} "
                "by XSLT 2.0; a native XSLT 2.0 processor cannot compile it",
                code="XTSE0090",
                uri=uri,
                location=location,
            )


def _check_30_attributes(
    node: Any,
    local: str,
    vocabulary: _XSLT20Vocabulary,
    *,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    """Reject an attribute the XSLT 3.0 schema does not define on *local*.

    XTSE0090 covers attributes in no namespace and attributes in the XSLT
    namespace.  A standard attribute is written unprefixed on an XSLT element;
    the XSLT-namespaced spelling is for literal result elements and is therefore
    an error here.  Shadow attributes have already been replaced in the
    effective view this check walks.
    """
    allowed = vocabulary.attributes[local] | XSLT30_SCHEMA_SUPPLEMENTS.get(local, frozenset())
    for namespace, attribute_local, _value in _attribute_triples(node):
        if namespace == XSLT_NS:
            raise XsltStaticError(
                f"the XSLT-namespaced attribute {attribute_local!r} is not permitted "
                f"on xsl:{local}",
                code="XTSE0090",
                uri=uri,
                location=location,
            )
        if namespace is not None:
            continue
        if attribute_local in allowed:
            continue
        raise XsltStaticError(
            f"the attribute {attribute_local!r} is not defined for xsl:{local} by XSLT 3.0",
            code="XTSE0090",
            uri=uri,
            location=location,
        )


def _check_mode_tokens(
    node: Any,
    local: str,
    vocabulary: _XSLT20Vocabulary,
    *,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    """Reject a ``#`` mode token the XSLT 2.0 schema does not define.

    The schema's ``mode`` type enumerates ``#default`` and ``#current`` for
    ``xsl:apply-templates``; its ``modes`` type enumerates ``#all`` and
    ``#default`` for ``xsl:template``.  Those tokens are XSLT 2.0 vocabulary,
    so only tokens absent from the schema (``#unnamed`` and ``#all`` on
    ``xsl:apply-templates``) are rejected here.
    """
    value = _attribute_value(node, "mode")
    if value is None:
        return
    allowed = vocabulary.mode_tokens.get(local, frozenset())
    for token in value.split():
        if token.startswith("#") and token not in allowed:
            raise XsltStaticError(
                f"the mode token {token!r} is not defined for xsl:{local} by XSLT 2.0; "
                "a native XSLT 2.0 processor cannot compile it",
                code="XTSE0550",
                uri=uri,
                location=location,
            )


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


def _visit(node: Any, *, uri: str | None, vocabulary: _XSLT20Vocabulary) -> None:
    if node.kind is not NodeKind.ELEMENT:
        return
    name = node.name or ""
    if name.startswith(f"{{{XSLT_NS}}}"):
        local = name[len(XSLT_NS) + 2 :]
        location = node.source_location()
        if local not in vocabulary.elements:
            raise XsltStaticError(
                f"the xsl:{local} element is not defined by XSLT 2.0; a native "
                "XSLT 2.0 processor cannot compile it",
                code="XTSE0010",
                uri=uri,
                location=location,
            )
        _check_attributes(node, local, vocabulary, uri=uri, location=location)
        _check_mode_tokens(node, local, vocabulary, uri=uri, location=location)
        if local in ("output", "result-document"):
            _check_method_attribute(
                node,
                uri=uri,
                location=location,
                allow_avt=local == "result-document",
            )
        if local == "analyze-string":
            _check_analyze_string_flags(node, uri=uri, location=location)
    for child in node.children:
        _visit(child, uri=uri, vocabulary=vocabulary)


def _version_attribute(node: Any, *, xslt_namespace: bool) -> str | None:
    """One element's declared forwards-compatibility version, if it has one."""
    target = f"{{{XSLT_NS}}}version" if xslt_namespace else "version"
    for attribute in node.attributes:
        if attribute.name == target:
            return _attribute_text(attribute)
    return None


def _forwards_compatible_30(node: Any, inherited: bool) -> bool:
    """Whether *node* enables XSLT section 3.11 forwards-compatible processing."""
    name = node.name or ""
    from_xslt_namespace = not name.startswith(f"{{{XSLT_NS}}}")
    version = _version_attribute(node, xslt_namespace=from_xslt_namespace)
    if version is None:
        return inherited
    try:
        return Decimal(version.strip()) > Decimal("3.0")
    except (InvalidOperation, ValueError):
        return inherited


def _visit_30(
    node: Any,
    *,
    uri: str | None,
    vocabulary: _XSLT20Vocabulary,
    forwards_compatible: bool,
) -> None:
    """Check one XSLT 3.0 module's element attributes for XTSE0090."""
    if node.kind is not NodeKind.ELEMENT:
        return
    current = _forwards_compatible_30(node, forwards_compatible)
    name = node.name or ""
    if name.startswith(f"{{{XSLT_NS}}}"):
        local = name[len(XSLT_NS) + 2 :]
        if local in vocabulary.attributes and not current:
            _check_30_attributes(
                node,
                local,
                vocabulary,
                uri=uri,
                location=node.source_location(),
            )
    for child in node.children:
        _visit_30(
            child,
            uri=uri,
            vocabulary=vocabulary,
            forwards_compatible=current,
        )


def assert_native_profile_language(module: Any, profile: ProfileOptions) -> None:
    """Gate one stylesheet module against its processor's language vocabulary.

    A native pre-3.0 processor is gated against the matching bundled schema.  A
    3.0 processor still owns XTSE0090 for every XSLT element, so the same
    vocabulary check runs there too; forwards-compatible declarations are
    exempt, exactly as section 3.11 requires.
    """
    resolved = LanguageProfile.from_options(profile)
    if resolved.processor_version == "1.0":
        from pyxsd.xslt.profile10 import assert_xslt10_language

        assert_xslt10_language(module, resolved)
        return
    uri = getattr(module, "identity", None)
    if Decimal(resolved.processor_version) >= Decimal("3.0"):
        _visit_30(
            module.root,
            uri=uri,
            vocabulary=_xslt30_vocabulary(),
            forwards_compatible=False,
        )
        return
    _visit(module.document.root_element, uri=uri, vocabulary=_xslt20_vocabulary())
