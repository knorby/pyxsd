"""Character maps, Unicode normalization, and URI-attribute escaping.

The unit tests exercise the serializer directly; the integration tests compile
stylesheets so character-map names are resolved through the package exactly as
``xsl:output`` and ``xsl:result-document`` declare them.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from tests.xslt.support import stylesheet, transform

from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt.errors import XsltSerializationError, XsltStaticError
from pyxsd.xslt.processor import XsltProcessor
from pyxsd.xslt.serialization import SerializationParameters, Serializer

XHTML_NS = "http://www.w3.org/1999/xhtml"
SECONDARY_URI = "http://example.com/secondary"


def _serialize(sequence: tuple[object, ...], **values: object) -> str:
    parameters = SerializationParameters.merge(explicit=values)
    return Serializer.serialize(sequence, parameters)  # type: ignore[arg-type]


def _code(action: Any) -> str | None:
    with pytest.raises((XsltSerializationError, XsltStaticError)) as error:
        action()
    return error.value.code


def _compile(body: str, *, attributes: str = ""):
    source = stylesheet(body, attributes=attributes)
    return XsltProcessor().compile(io.BytesIO(source))


# ---------------------------------------------------------------------------
# Character substitution order


def test_text_and_attribute_character_maps_replace_before_escaping() -> None:
    document = XdmDocument.parse(b'<r x="q">x &amp; y</r>')

    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            character_maps=(("x", "<"), ("q", "&")),
        )
        == '<r x="&">< &amp; y</r>'
    )


def test_character_map_replacements_are_not_escaped_or_remapped() -> None:
    document = XdmDocument.parse(b"<r>a</r>")

    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            character_maps=(("a", "b"), ("b", "c")),
        )
        == "<r>b</r>"
    )
    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            character_maps=(("a", "&<>"),),
        )
        == "<r>&<></r>"
    )


def test_character_maps_skip_cdata_sections() -> None:
    document = XdmDocument.parse(b"<r>x</r>")

    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            cdata_section_elements=("r",),
            character_maps=(("x", "y"),),
        )
        == "<r><![CDATA[x]]></r>"
    )


def test_cdata_content_is_normalized_but_not_character_mapped() -> None:
    document = XdmDocument.parse(b"<r>x e&#x301;</r>")

    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            cdata_section_elements=("r",),
            normalization_form="NFC",
            character_maps=(("x", "X"),),
        )
        == "<r><![CDATA[x \u00e9]]></r>"
    )


def test_html_raw_text_is_mapped_and_normalized_without_escaping() -> None:
    script = XdmDocument.parse(b"<script>x e&#x301; &amp;</script>")
    style = XdmDocument.parse(b"<style>e&#x301;</style>")

    assert (
        _serialize(
            (script,),
            method="html",
            character_maps=(("x", "<"),),
            normalization_form="NFC",
        )
        == "<script>< \u00e9 &</script>"
    )
    assert _serialize((style,), method="html", normalization_form="NFC") == (
        "<style>\u00e9</style>"
    )


def test_uri_attributes_percent_escape_before_output_escaping() -> None:
    document = XdmDocument.parse('<a href="a b%20&amp;\u00e9"/>'.encode())

    assert _serialize((document,), method="html", character_maps=(("\u00e9", "E"),)) == (
        '<a href="a b%20&amp;%C3%A9"></a>'
    )
    assert (
        _serialize(
            (document,),
            method="html",
            escape_uri_attributes=False,
            character_maps=(("\u00e9", "E"),),
        )
        == '<a href="a b%20&amp;E"></a>'
    )


def test_only_declared_uri_attributes_are_escaped() -> None:
    document = XdmDocument.parse('<div href="\u00e9"/>'.encode())

    assert _serialize((document,), method="html", character_maps=(("\u00e9", "E"),)) == (
        '<div href="E"></div>'
    )


def test_xhtml_uri_attributes_escape_in_the_xhtml_namespace() -> None:
    document = XdmDocument.parse(f'<a xmlns="{XHTML_NS}" href="\u00e9"/>'.encode())

    assert _serialize((document,), method="xhtml", omit_xml_declaration=True) == (
        f'<a xmlns="{XHTML_NS}" href="%C3%A9" />'
    )


# ---------------------------------------------------------------------------
# Normalization


def test_normalization_forms_apply_to_text_and_attributes() -> None:
    document = XdmDocument.parse(b'<r a="e&#x301;">e&#x301;</r>')

    assert _serialize((document,), omit_xml_declaration=True, normalization_form="NFC") == (
        '<r a="\u00e9">\u00e9</r>'
    )
    assert _serialize((document,), omit_xml_declaration=True, normalization_form="NFD") == (
        '<r a="e\u0301">e\u0301</r>'
    )
    assert _serialize((document,), omit_xml_declaration=True, normalization_form="none") == (
        '<r a="e\u0301">e\u0301</r>'
    )

    ligature = XdmDocument.parse("<r>\ufb01</r>".encode())
    assert _serialize((ligature,), omit_xml_declaration=True, normalization_form="NFKC") == (
        "<r>fi</r>"
    )


def test_fully_normalized_is_accepted_and_behaves_as_nfc() -> None:
    parameters = SerializationParameters.merge(explicit={"normalization-form": "fully-normalized"})
    assert parameters.normalization_form == "NFC"

    document = XdmDocument.parse(b"<r>e&#x301;</r>")
    assert _serialize(
        (document,), omit_xml_declaration=True, normalization_form="fully-normalized"
    ) == ("<r>\u00e9</r>")


def test_character_mapping_precedes_normalization() -> None:
    document = XdmDocument.parse(b"<r>e&#x301;</r>")

    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            normalization_form="NFC",
            character_maps=(("\u00e9", "Z"),),
        )
        == "<r>\u00e9</r>"
    )
    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            normalization_form="NFC",
            character_maps=(("e", "\u00e9"),),
        )
        == "<r>\u00e9\u0301</r>"
    )


def test_normalization_applies_to_json_strings_and_text_output() -> None:
    assert (
        _serialize((XdmAtomic.string("e\u0301"),), method="json", normalization_form="NFC")
        == '"\u00e9"'
    )

    parameters = SerializationParameters(method="text", normalization_form="NFC")
    assert Serializer.serialize((XdmAtomic.string("e\u0301"),), parameters) == "\u00e9"

    mapped = SerializationParameters(
        method="text",
        normalization_form="NFC",
        character_maps=(("x", "\u00e9"),),
    )
    assert Serializer.serialize((XdmAtomic.string("xx"),), mapped) == "\u00e9\u00e9"


# ---------------------------------------------------------------------------
# Package-level character-map composition


def test_nested_character_maps_compose() -> None:
    compiled = _compile(
        '<xsl:character-map name="a"><xsl:output-character character="a" string="1"/>'
        "</xsl:character-map>"
        '<xsl:character-map name="b" use-character-maps="a">'
        '<xsl:output-character character="b" string="2"/>'
        "<junk/>"
        "</xsl:character-map>"
        '<xsl:template name="main"><r>ab</r></xsl:template>'
    )

    assert dict(compiled.package.character_map_pairs("b")) == {"a": "1", "b": "2"}
    assert dict(compiled.package.character_map_pairs("a b")) == {"a": "1", "b": "2"}
    assert compiled.package.character_map_pairs(None) == ()


def test_later_character_mappings_override_earlier_ones() -> None:
    nested = (
        '<xsl:character-map name="a"><xsl:output-character character="x" string="1"/>'
        "</xsl:character-map>"
        '<xsl:character-map name="b" use-character-maps="a">'
        '<xsl:output-character character="x" string="2"/>'
        "</xsl:character-map>"
        '<xsl:template name="main"><r/></xsl:template>'
    )
    assert dict(_compile(nested).package.character_map_pairs("b")) == {"x": "2"}

    referenced = (
        '<xsl:character-map name="a"><xsl:output-character character="x" string="1"/>'
        "</xsl:character-map>"
        '<xsl:character-map name="b"><xsl:output-character character="x" string="2"/>'
        "</xsl:character-map>"
        '<xsl:character-map name="c" use-character-maps="a b">'
        '<xsl:output-character character="x" string="3"/>'
        "</xsl:character-map>"
        '<xsl:template name="main"><r/></xsl:template>'
    )
    assert dict(_compile(referenced).package.character_map_pairs("c")) == {"x": "3"}
    assert dict(_compile(referenced).package.character_map_pairs("a b")) == {"x": "2"}

    repeated = (
        '<xsl:character-map name="a"><xsl:output-character character="x" string="1"/>'
        "</xsl:character-map>"
        '<xsl:character-map name="b" use-character-maps="a a">'
        '<xsl:output-character character="x" string="1"/>'
        "</xsl:character-map>"
        '<xsl:template name="main"><r/></xsl:template>'
    )
    assert dict(_compile(repeated).package.character_map_pairs("b")) == {"x": "1"}


def test_undeclared_character_map_names_raise_xtse1590() -> None:
    output = (
        '<xsl:output use-character-maps="missing"/><xsl:template name="main"><r/></xsl:template>'
    )
    assert _code(lambda: _compile(output)) == "XTSE1590"

    declared_map = (
        '<xsl:character-map name="m" use-character-maps="missing"/>'
        '<xsl:output use-character-maps="m"/>'
        '<xsl:template name="main"><r/></xsl:template>'
    )
    assert _code(lambda: _compile(declared_map)) == "XTSE1590"


def test_character_map_self_reference_raises_xtse1600() -> None:
    recursive = '<xsl:character-map name="a" use-character-maps="a"/>'
    body = recursive + '<xsl:template name="main"><r/></xsl:template>'

    assert _code(lambda: _compile(body)) == "XTSE1600"


def test_invalid_output_character_raises_xtse0010() -> None:
    multi = (
        '<xsl:character-map name="m">'
        '<xsl:output-character character="ab" string="x"/>'
        "</xsl:character-map>"
        '<xsl:template name="main"><r/></xsl:template>'
    )
    assert _code(lambda: _compile(multi)) == "XTSE0010"

    missing = (
        '<xsl:character-map name="m"><xsl:output-character character="a"/>'
        "</xsl:character-map>"
        '<xsl:template name="main"><r/></xsl:template>'
    )
    assert _code(lambda: _compile(missing)) == "XTSE0010"


# ---------------------------------------------------------------------------
# Default-namespace resolution and delivery


def test_use_character_maps_ignores_the_default_namespace_on_output() -> None:
    compiled = _compile(
        '<xsl:output method="xml" omit-xml-declaration="yes" use-character-maps="m"/>'
        '<xsl:character-map name="m"><xsl:output-character character="x" string="y"/>'
        "</xsl:character-map>"
        '<xsl:template name="main"><xsl:value-of select="\'x\'"/></xsl:template>',
        attributes=' xmlns="urn:def"',
    )

    assert compiled.package.output(None).attribute("use-character-maps") == "m"
    result = compiled.transform(delivery="stylesheet", initial_template="main")
    assert result.principal_entry.serialized == "y"
    assert dict(result.principal_entry.character_maps) == {"x": "y"}


def test_use_character_maps_ignores_the_default_namespace_on_result_document() -> None:
    source = stylesheet(
        '<xsl:character-map name="m"><xsl:output-character character="x" string="y"/>'
        "</xsl:character-map>"
        '<xsl:template name="main">'
        f'<xsl:result-document href="{SECONDARY_URI}" method="xml" '
        'omit-xml-declaration="yes" use-character-maps="m">x</xsl:result-document>'
        "</xsl:template>",
        attributes=' xmlns="urn:def"',
    )

    result = transform(source, delivery="stylesheet", initial_template="main")

    secondary = result.secondary[SECONDARY_URI]
    assert secondary.serialized == "y"
    assert dict(secondary.character_maps) == {"x": "y"}


def test_stylesheet_delivery_applies_character_maps_to_principal_and_secondary() -> None:
    source = stylesheet(
        '<xsl:output method="xml" omit-xml-declaration="yes" use-character-maps="m"/>'
        '<xsl:character-map name="m"><xsl:output-character character="x" string="y"/>'
        "</xsl:character-map>"
        '<xsl:character-map name="other"><xsl:output-character character="x" string="z"/>'
        "</xsl:character-map>"
        '<xsl:template name="main">'
        f'<xsl:result-document href="{SECONDARY_URI}" use-character-maps="other">'
        "<s>x</s>"
        "</xsl:result-document>"
        "<r>x</r>"
        "</xsl:template>",
        attributes=' exclude-result-prefixes="xs"',
    )

    result = transform(source, delivery="stylesheet", initial_template="main")

    assert result.principal_entry.serialized == "<r>y</r>"
    assert dict(result.principal_entry.character_maps) == {"x": "y"}
    assert result.serialize() == "<r>y</r>"

    secondary = result.secondary[SECONDARY_URI]
    assert secondary.serialized == "<s>z</s>"
    assert dict(secondary.character_maps) == {"x": "z"}
    assert result.serialize(SECONDARY_URI) == "<s>z</s>"


def test_result_document_inherits_the_named_format_character_map() -> None:
    source = stylesheet(
        '<xsl:output name="f" method="xml" omit-xml-declaration="yes" use-character-maps="m"/>'
        '<xsl:character-map name="m"><xsl:output-character character="x" string="y"/>'
        "</xsl:character-map>"
        '<xsl:template name="main">'
        f'<xsl:result-document href="{SECONDARY_URI}" format="f"><s>x</s></xsl:result-document>'
        "<r>x</r>"
        "</xsl:template>",
        attributes=' exclude-result-prefixes="xs"',
    )

    result = transform(source, delivery="stylesheet", initial_template="main")

    assert result.principal_entry.serialized is not None
    assert result.principal_entry.serialized.endswith("<r>x</r>")
    assert result.principal_entry.character_maps == ()
    secondary = result.secondary[SECONDARY_URI]
    assert secondary.serialized == "<s>y</s>"
    assert dict(secondary.character_maps) == {"x": "y"}


def test_explicit_serialization_parameters_override_entry_character_maps() -> None:
    source = stylesheet(
        '<xsl:output method="xml" omit-xml-declaration="yes" use-character-maps="m"/>'
        '<xsl:character-map name="m"><xsl:output-character character="x" string="y"/>'
        "</xsl:character-map>"
        '<xsl:template name="main"><r>x</r></xsl:template>',
        attributes=' exclude-result-prefixes="xs"',
    )

    result = transform(source, delivery="stylesheet", initial_template="main")

    assert result.principal_entry.serialized == "<r>y</r>"
    assert result.serialize(parameters={"character_maps": (("x", "q"),)}) == "<r>q</r>"
    assert result.serialize(parameters={"character_maps": ()}) == "<r>x</r>"


def test_json_result_uses_character_mapped_strings() -> None:
    source = stylesheet(
        '<xsl:output method="json" use-character-maps="m"/>'
        '<xsl:character-map name="m"><xsl:output-character character="x" string="y"/>'
        "</xsl:character-map>"
        '<xsl:template name="main"><xsl:sequence select="map{&quot;k&quot;: &quot;x&quot;}"/></xsl:template>'
    )

    result = transform(source, delivery="stylesheet", initial_template="main")

    assert result.principal_entry.serialized == '{"k":"y"}'
