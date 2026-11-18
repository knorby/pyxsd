"""Run-time ``as`` applies the function conversion rules, not just a code."""

from __future__ import annotations

import io

import pytest

from pyxsd.schema import Schema
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xdm.type_tests import matches_schema_element
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
SUP = "urn:example:supplied"

_SCHEMA = (
    f'<xs:schema xmlns:xs="{XSD_NS}" xmlns:sup="{SUP}" targetNamespace="{SUP}" '
    'elementFormDefault="qualified">'
    '<xs:simpleType name="PartNumber"><xs:restriction base="xs:string">'
    '<xs:pattern value="[0-9]{3}-[A-Z]{2}"/></xs:restriction></xs:simpleType>'
    '<xs:simpleType name="Small"><xs:restriction base="xs:int">'
    '<xs:maxInclusive value="10"/></xs:restriction></xs:simpleType>'
    '<xs:simpleType name="Base"><xs:restriction base="xs:string">'
    '<xs:pattern value="[0-9]{3}-[A-Z]{2}"/></xs:restriction></xs:simpleType>'
    '<xs:simpleType name="Derived"><xs:restriction base="sup:Base">'
    '<xs:maxLength value="6"/></xs:restriction></xs:simpleType>'
    '<xs:complexType name="Entry"><xs:simpleContent>'
    '<xs:extension base="sup:PartNumber">'
    '<xs:attribute name="code" type="sup:PartNumber"/>'
    '<xs:attribute ref="sup:code"/>'
    "</xs:extension></xs:simpleContent></xs:complexType>"
    '<xs:attribute name="code" type="sup:PartNumber"/>'
    '<xs:element name="doc" type="sup:PartNumber"/>'
    '<xs:element name="other" type="sup:Small"/>'
    '<xs:element name="derived" type="sup:Derived"/>'
    '<xs:element name="maybe" type="xs:string" nillable="true"/>'
    '<xs:element name="entry" type="sup:Entry"/>'
    '<xs:element name="wrap"><xs:complexType><xs:sequence>'
    '<xs:element name="inner" type="xs:string"/>'
    "</xs:sequence></xs:complexType></xs:element>"
    "</xs:schema>"
)


def _pool() -> SchemaPool:
    return SchemaPool(
        (Schema.compile(io.StringIO(_SCHEMA), xsd_version="1.0"),),
        xsd_version="1.1",
        merge_namespaces=True,
    )


def _stylesheet(body: str) -> bytes:
    return (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xmlns:sup="{SUP}">{body}</xsl:stylesheet>'
    ).encode()


def _run(body: str, **options: object) -> str:
    compiled = XsltProcessor().compile(io.BytesIO(_stylesheet(body)), schema=_pool())
    source = options.pop("source", b"<doc/>")
    assert isinstance(source, bytes)
    return compiled.transform(io.BytesIO(source), **options).principal_entry.serialized


def _code(body: str, **options: object) -> str:
    with pytest.raises(XsltError) as caught:
        _run(body, **options)
    return caught.value.code


# -- atomic conversions ----------------------------------------------------------


def test_global_variable_content_casts_to_a_supplied_type() -> None:
    serialized = _run(
        '<xsl:variable name="v" as="sup:PartNumber"><v>123-AB</v></xsl:variable>'
        '<xsl:template name="main"><out><xsl:value-of select="$v"/>'
        '<xsl:value-of select="$v instance of sup:PartNumber"/>'
        '<xsl:value-of select="$v instance of xs:untypedAtomic"/></out></xsl:template>',
        initial_template="main",
    )

    assert ">123-ABtruefalse</out>" in serialized


def test_function_parameter_and_result_cast_to_a_supplied_type() -> None:
    serialized = _run(
        '<xsl:function name="sup:pn" as="sup:PartNumber">'
        '<xsl:param name="p" as="sup:Small"/>'
        '<xsl:value-of select="&quot;123-AB&quot;"/></xsl:function>'
        '<xsl:variable name="e"><v>5</v></xsl:variable>'
        '<xsl:template name="main"><out><xsl:value-of '
        'select="sup:pn($e/v) instance of sup:PartNumber"/></out></xsl:template>',
        initial_template="main",
    )

    assert ">true</out>" in serialized


def test_template_result_casts_to_a_supplied_type() -> None:
    serialized = _run(
        '<xsl:template name="t" as="sup:PartNumber">'
        '<xsl:value-of select="&quot;123-AB&quot;"/></xsl:template>'
        '<xsl:template name="main"><out><xsl:call-template name="t"/></out></xsl:template>',
        initial_template="main",
    )

    assert ">123-AB</out>" in serialized


def test_parameter_default_casts_to_a_supplied_type() -> None:
    serialized = _run(
        '<xsl:template name="main"><xsl:param name="p" as="sup:Small">'
        '<v>5</v></xsl:param><out><xsl:value-of select="$p"/></out></xsl:template>',
        initial_template="main",
    )

    assert ">5</out>" in serialized


# -- the same four boundaries keep their declared error codes ---------------------


def test_conversion_failures_keep_the_declared_error_codes() -> None:
    assert (
        _code(
            '<xsl:variable name="v" select="&quot;bad&quot;" as="sup:PartNumber"/>'
            '<xsl:template name="main"><xsl:value-of select="$v"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0570"
    )
    assert (
        _code(
            '<xsl:template name="main"><xsl:param name="p" as="sup:Small" select="11"/>'
            "</xsl:template>",
            initial_template="main",
        )
        == "XTTE0590"
    )
    assert (
        _code(
            '<xsl:function name="sup:f" as="sup:Small"><xsl:sequence select="11"/>'
            '</xsl:function><xsl:template name="main">'
            '<xsl:value-of select="sup:f()"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0780"
    )
    assert (
        _code(
            '<xsl:template name="t" as="sup:Small"><xsl:sequence select="11"/>'
            '</xsl:template><xsl:template name="main">'
            '<xsl:call-template name="t"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0505"
    )


def test_a_string_is_not_cast_to_a_non_string_supplied_type() -> None:
    # XPath 3.1 section 3.1.5.2 casts xs:untypedAtomic, not an xs:string, to a
    # numeric type; a lexical string that happens to parse is still an error.
    assert (
        _code(
            '<xsl:variable name="v" select="xs:string(5)" as="sup:Small"/>'
            '<xsl:template name="main"><xsl:value-of select="$v"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0570"
    )


# -- node item types -------------------------------------------------------------


def test_element_test_uses_the_type_annotation() -> None:
    matching = _run(
        '<xsl:variable name="e" as="element(*, sup:PartNumber)">'
        '<sup:doc xsl:validation="strict">123-AB</sup:doc></xsl:variable>'
        '<xsl:template name="main"><out><xsl:value-of '
        'select="$e instance of element(*, sup:PartNumber)"/></out></xsl:template>',
        initial_template="main",
    )
    assert ">true</out>" in matching

    # xs:string is not derived from sup:Small.
    assert (
        _code(
            '<xsl:variable name="e" as="element(*, sup:Small)">'
            '<sup:doc xsl:type="xs:string">x</sup:doc></xsl:variable>'
            '<xsl:template name="main"><xsl:value-of select="$e"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0570"
    )


def test_schema_element_test_uses_the_declaration() -> None:
    matching = _run(
        '<xsl:template name="t" as="schema-element(sup:doc)">'
        '<sup:doc xsl:validation="strict">123-AB</sup:doc></xsl:template>'
        '<xsl:template name="main"><out><xsl:call-template name="t"/></out></xsl:template>',
        initial_template="main",
    )
    assert "<sup:doc>123-AB</sup:doc>" in matching

    # The element is typed xs:integer, which does not derive from personType.
    assert (
        _code(
            '<xsl:template name="t" as="schema-element(sup:doc)">'
            '<sup:doc xsl:type="xs:integer">5</sup:doc></xsl:template>'
            '<xsl:template name="main"><xsl:call-template name="t"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0505"
    )


def test_node_tests_reject_a_mismatched_name_or_kind() -> None:
    # A differently named element satisfies neither element test.
    for test in ("element(sup:doc)", "schema-element(sup:doc)"):
        assert (
            _code(
                f'<xsl:variable name="e" as="{test}"><sup:other/></xsl:variable>'
                '<xsl:template name="main"><xsl:value-of select="$e"/></xsl:template>',
                initial_template="main",
            )
            == "XTTE0570"
        )

    # An element cannot satisfy an attribute test.
    assert (
        _code(
            '<xsl:variable name="e" as="attribute(sup:code)"><v a="1"/></xsl:variable>'
            '<xsl:template name="main"><xsl:value-of select="$e"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0570"
    )


def test_attribute_tests_use_name_and_type_annotation() -> None:
    matching = _run(
        '<xsl:variable name="e"><sup:entry xsl:validation="strict" code="123-AB">'
        "123-AB</sup:entry></xsl:variable>"
        '<xsl:variable name="a" select="$e/*/@code" '
        'as="attribute(*, sup:PartNumber)"/>'
        '<xsl:template name="main"><out><xsl:value-of select="$a"/></out></xsl:template>',
        initial_template="main",
    )
    assert ">123-AB</out>" in matching

    # A differently named attribute satisfies neither attribute test.
    for test in ("attribute(sup:code)", "schema-attribute(sup:code)"):
        assert (
            _code(
                f'<xsl:variable name="a" select="/doc/@other" as="{test}"/>'
                '<xsl:template name="main"><xsl:value-of select="$a"/></xsl:template>',
                initial_template="main",
                source=b"<doc other='x'/>",
            )
            == "XTTE0570"
        )

    # The attribute's annotation is sup:PartNumber, which is not derived from
    # xs:integer even though its lexical form parses as one.
    assert (
        _code(
            '<xsl:variable name="e"><sup:entry xsl:validation="strict" code="123-AB">'
            "123-AB</sup:entry></xsl:variable>"
            '<xsl:variable name="a" select="$e/*/@code" '
            'as="attribute(*, xs:integer)"/>'
            '<xsl:template name="main"><xsl:value-of select="$a"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0570"
    )


def test_an_untyped_element_does_not_satisfy_a_typed_test() -> None:
    assert (
        _code(
            '<xsl:variable name="e" as="element(*, sup:PartNumber)"><v>123-AB</v>'
            '</xsl:variable><xsl:template name="main"><xsl:value-of select="$e"/>'
            "</xsl:template>",
            initial_template="main",
        )
        == "XTTE0570"
    )


def test_schema_element_test_uses_an_anonymous_declared_type() -> None:
    serialized = _run(
        '<xsl:template name="t" as="schema-element(sup:wrap)">'
        '<sup:wrap xsl:validation="strict"><sup:inner>x</sup:inner></sup:wrap>'
        "</xsl:template>"
        '<xsl:template name="main"><out><xsl:call-template name="t"/></out></xsl:template>',
        initial_template="main",
    )

    assert "<sup:inner>x</sup:inner>" in serialized

    # An unvalidated element carries no actual type, so it cannot satisfy the
    # anonymous declaration either.
    assert (
        _code(
            '<xsl:template name="t" as="schema-element(sup:wrap)">'
            "<sup:wrap><sup:inner>x</sup:inner></sup:wrap></xsl:template>"
            '<xsl:template name="main"><xsl:call-template name="t"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0505"
    )

    # The declaration's anonymous type is complex, so a node annotated
    # xs:integer cannot match it.
    assert (
        _code(
            '<xsl:template name="t" as="schema-element(sup:wrap)">'
            '<sup:wrap xsl:type="xs:integer">5</sup:wrap></xsl:template>'
            '<xsl:template name="main"><xsl:call-template name="t"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0505"
    )


def test_schema_attribute_test_uses_the_declaration() -> None:
    """A global attribute declaration is matched by expanded name.

    The fixture's complex type declares a local attribute with the same
    local name as the global declaration that follows it; both are distinct
    components, so the global one still resolves through ``SchemaPool``.
    """
    serialized = _run(
        '<xsl:variable name="e"><sup:entry xsl:validation="strict" sup:code="123-AB">'
        "123-AB</sup:entry></xsl:variable>"
        '<xsl:variable name="a" select="$e/*/@sup:code" as="schema-attribute(sup:code)"/>'
        '<xsl:template name="main"><out><xsl:value-of select="$a"/></out></xsl:template>',
        initial_template="main",
    )

    assert ">123-AB</out>" in serialized


def test_a_derived_type_satisfies_its_base_type() -> None:
    # sup:Derived restricts sup:Base; function conversion accepts a value
    # already in the base type's value space (XPath 3.1 section 3.1.5.2).
    serialized = _run(
        '<xsl:template name="t"><xsl:param name="p" as="sup:Base"/>'
        '<out><xsl:value-of select="$p"/></out></xsl:template>'
        '<xsl:template name="main"><xsl:call-template name="t">'
        '<xsl:with-param name="p"><sup:derived xsl:validation="strict">'
        "123-AB</sup:derived></xsl:with-param>"
        "</xsl:call-template></xsl:template>",
        initial_template="main",
    )

    assert ">123-AB</out>" in serialized


def test_element_test_rejects_a_nilled_element_unless_nillable() -> None:
    # XPath 3.1 section 2.5.5.3: a nilled element matches an element test only
    # when the type name carries the nillable (?) marker.
    assert (
        _code(
            '<xsl:variable name="e" as="element(*, xs:string)">'
            '<sup:maybe xsl:validation="strict" xsi:nil="true"/></xsl:variable>'
            '<xsl:template name="main"><xsl:value-of select="$e"/></xsl:template>',
            initial_template="main",
        )
        == "XTTE0570"
    )
    nillable = _run(
        '<xsl:variable name="e" as="element(*, xs:string?)">'
        '<sup:maybe xsl:validation="strict" xsi:nil="true"/></xsl:variable>'
        '<xsl:template name="main"><out><xsl:value-of select="count($e)"/></out></xsl:template>',
        initial_template="main",
    )
    assert ">1</out>" in nillable


@pytest.mark.parametrize("test", ["element(*)", "element(sup:maybe)"])
def test_untyped_element_tests_match_a_nilled_element(test: str) -> None:
    # The nilled rule in XPath 3.1 section 2.5.5.3 applies only to a typed
    # element test: element(*) and element(Name) match a nilled element
    # regardless of its nilled property.
    serialized = _run(
        f'<xsl:variable name="e" as="{test}">'
        '<sup:maybe xsl:validation="strict" xsi:nil="true"/></xsl:variable>'
        '<xsl:template name="main"><out><xsl:value-of select="count($e)"/>'
        "</out></xsl:template>",
        initial_template="main",
    )
    assert ">1</out>" in serialized


def test_a_declaration_absent_from_the_pool_does_not_match() -> None:
    element = XdmDocument.parse(b"<doc/>").root_element

    assert not matches_schema_element(element, f"{{{SUP}}}missing", _pool())


# -- bare type QName in a kind test ---------------------------------------------


def test_a_bare_type_name_in_a_kind_test_uses_no_namespace() -> None:
    schema = (
        f'<xs:schema xmlns:xs="{XSD_NS}">'
        '<xs:simpleType name="X"><xs:restriction base="xs:string"/></xs:simpleType>'
        '<xs:element name="doc" type="X"/></xs:schema>'
    )
    stylesheet = (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}">'
        f"<xsl:import-schema>{schema}</xsl:import-schema>"
        '<xsl:template name="main"><out>'
        '<xsl:value-of select="/doc instance of element(*, X)"/>'
        '<xsl:value-of select="/doc instance of schema-element(doc)"/>'
        "</out></xsl:template></xsl:stylesheet>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(stylesheet.encode()))

    result = compiled.transform(
        io.BytesIO(b"<doc>x</doc>"), initial_template="main", validation="strict"
    )

    assert ">truetrue</out>" in result.principal_entry.serialized
