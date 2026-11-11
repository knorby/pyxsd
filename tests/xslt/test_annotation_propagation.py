"""Annotation propagation through ``xsl:copy`` and ``xsl:copy-of``.

The tests assert public transformation results and later XPath typed access.
They cover preserve, strip, strict, lax, and named-type validation on copied
elements, attributes, and document nodes, plus the copy-specific error arms.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
VAL = "urn:example:copy"
OTHER = "urn:example:other"

SCHEMA = f"""
<xs:schema xmlns:xs="{XSD_NS}" xmlns:v="{VAL}" targetNamespace="{VAL}"
    elementFormDefault="qualified">
  <xs:simpleType name="Amount">
    <xs:restriction base="xs:decimal"/>
  </xs:simpleType>
  <xs:complexType name="Line">
    <xs:sequence><xs:element name="value" type="xs:decimal"/></xs:sequence>
    <xs:attribute name="code" type="xs:int" use="required"/>
  </xs:complexType>
  <xs:complexType name="Order">
    <xs:sequence><xs:element name="line" type="v:Line" maxOccurs="unbounded"/></xs:sequence>
    <xs:attribute name="id" type="xs:string"/>
  </xs:complexType>
  <xs:element name="Order" type="v:Order"/>
  <xs:element name="amount" type="v:Amount"/>
  <xs:attribute name="status" type="v:Amount"/>
</xs:schema>
"""

SOURCE = (
    f'<v:Order xmlns:v="{VAL}"><v:line code="1"><v:value>1.25</v:value></v:line>'
    f'<v:line code="2"><v:value>2.75</v:value></v:line></v:Order>'
).encode()


def _stylesheet(body: str, *, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:v="{VAL}" xmlns:o="{OTHER}" version="3.0"{attributes}>'
        f"<xsl:import-schema>{SCHEMA}</xsl:import-schema>"
        f"{body}</xsl:stylesheet>"
    ).encode()


def _compile(body: str, *, attributes: str = "", **options: Any):
    return XsltProcessor(**options).compile(io.BytesIO(_stylesheet(body, attributes=attributes)))


def _transform(
    body: str,
    *,
    source: bytes = SOURCE,
    attributes: str = "",
    processor_options: dict[str, Any] | None = None,
    delivery: str = "raw",
    **options: Any,
):
    compiled = _compile(body, attributes=attributes, **(processor_options or {}))
    return compiled.transform(source, initial_template="main", delivery=delivery, **options)


def _error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def _values(result: Any) -> tuple[str, ...]:
    return tuple(
        item.lexical if isinstance(item, XdmAtomic) else str(item) for item in result.principal
    )


def test_copy_of_preserve_retains_annotations_and_typed_values() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy-of select="/v:Order" validation="preserve"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order instance of element(*, v:Order)"/>'
        '<xsl:sequence select="($c//v:value)[1] instance of element(*, xs:decimal)"/>'
        '<xsl:sequence select="sum($c//v:value)"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body, processor_options={"validation": "strict"})) == (
        "true",
        "true",
        "4.0",
    )


def test_copy_of_strip_removes_annotations_and_typed_values() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy-of select="/v:Order" validation="strip"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order instance of element(v:Order, xs:untyped)"/>'
        '<xsl:sequence select="($c//v:value)[1] instance of element(v:value, xs:untyped)"/>'
        '<xsl:sequence select="data(($c//v:value)[1]) instance of xs:untypedAtomic"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body, processor_options={"validation": "strict"})) == (
        "true",
        "true",
        "true",
    )


def test_copy_of_strict_revalidates_an_untyped_source_tree() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy-of select="/v:Order" validation="strict"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order instance of element(*, v:Order)"/>'
        '<xsl:sequence select="($c//v:value)[1] instance of element(*, xs:decimal)"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body)) == ("true", "true")


def test_copy_of_named_type_annotates_the_copy() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy-of select="/v:Order" type="v:Order"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order instance of element(*, v:Order)"/>'
        '<xsl:sequence select="($c//v:line)[1] instance of element(*, v:Line)"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body)) == ("true", "true")


def test_copy_of_type_simple_and_copy_of_atomic_values() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="amounts" as="item()*">'
        '<xsl:copy-of select="/v:Order/v:line/v:value" type="xs:decimal"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$amounts instance of element(*, xs:decimal)*"/>'
        '<xsl:sequence select="sum($amounts)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body, processor_options={"validation": "strict"})) == ("true", "4.0")

    atomic = (
        '<xsl:template name="main"><xsl:copy-of select="42" validation="strict"/></xsl:template>'
    )
    result = _transform(atomic)
    assert result.principal == (XdmAtomic.integer(42),)


def test_copy_of_complex_type_on_an_attribute_is_xtte1535() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:copy-of select="/v:Order/v:line[1]/@code" type="v:Line"/>'
        "</xsl:template>"
    )

    assert _error_code(lambda: _transform(body)) == "XTTE1535"


def test_copy_of_document_type_validation_uses_the_single_element_child() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy-of select="/" type="v:Order"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order instance of element(*, v:Order)"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body)) == ("true",)

    invalid = (
        '<xsl:template name="main">'
        '<xsl:copy-of select="/v:Order/v:line[1]" type="v:Order"/>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(invalid)) == "XTTE1540"


def test_copy_shallow_element_preserves_root_any_type_and_children() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:for-each select="/v:Order">'
        '<xsl:copy validation="preserve" xsl:default-validation="preserve">'
        '<xsl:copy-of select="v:line"/>'
        "</xsl:copy>"
        "</xsl:for-each>"
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order instance of element(*, xs:anyType)"/>'
        '<xsl:sequence select="$c/v:Order/v:line[1] instance of element(*, v:Line)"/>'
        '<xsl:sequence select="sum($c//v:value)"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body, processor_options={"validation": "strict"})) == (
        "true",
        "true",
        "4.0",
    )


def test_copy_shallow_element_strip_removes_child_annotations() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:for-each select="/v:Order">'
        '<xsl:copy validation="strip">'
        '<xsl:copy-of select="v:line"/>'
        "</xsl:copy>"
        "</xsl:for-each>"
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order instance of element(v:Order, xs:untyped)"/>'
        '<xsl:sequence select="$c/v:Order/v:line[1] instance of element(v:line, xs:untyped)"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body, processor_options={"validation": "strict"})) == ("true", "true")


def test_copy_attribute_preserve_and_strip_semantics() -> None:
    preserve = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:for-each select="/v:Order/v:line[1]/@code">'
        '<xsl:copy validation="preserve"/>'
        "</xsl:for-each>"
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, xs:int)"/>'
        '<xsl:sequence select="data($a)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(preserve, processor_options={"validation": "strict"})) == (
        "true",
        "1",
    )

    strip = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:for-each select="/v:Order/v:line[1]/@code">'
        '<xsl:copy validation="strip"/>'
        "</xsl:for-each>"
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, xs:untypedAtomic)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(strip, processor_options={"validation": "strict"})) == ("true",)


def test_copy_of_attribute_preserve_retains_and_strip_removes() -> None:
    preserve = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:copy-of select="/v:Order/v:line[1]/@code" validation="preserve"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, xs:int)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(preserve, processor_options={"validation": "strict"})) == ("true",)

    strip = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:copy-of select="/v:Order/v:line[1]/@code" validation="strip"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, xs:untypedAtomic)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(strip, processor_options={"validation": "strict"})) == ("true",)


def test_copy_of_default_validation_comes_from_the_containing_element() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<out xsl:default-validation="preserve">'
        '<xsl:copy-of select="/v:Order"/>'
        "</out>"
        "</xsl:variable>"
        '<xsl:sequence select="$c/out/v:Order instance of element(*, v:Order)"/>'
        '<xsl:sequence select="sum($c//v:value)"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body, processor_options={"validation": "strict"})) == ("true", "4.0")


def test_copy_of_preserve_does_not_annotate_the_root_any_type() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy-of select="/v:Order" validation="preserve"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order instance of element(*, v:Order)"/>'
        '<xsl:sequence select="not($c/v:Order instance of element(*, xs:anyType))"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body, processor_options={"validation": "strict"})) == ("true", "true")


def test_constructed_result_keeps_raw_and_delivered_views_separate() -> None:
    uri = "http://example.com/copied.xml"
    body = (
        '<xsl:template name="main">'
        f'<xsl:result-document href="{uri}" validation="strict">'
        '<xsl:copy-of select="/v:Order"/>'
        "</xsl:result-document>"
        "</xsl:template>"
    )

    result = _transform(body, delivery="raw")

    entry = result.secondary[uri]
    assert entry.document is entry.tree
    assert entry.tree.root_element.type_name == f"{{{VAL}}}Order"
    assert entry.raw[0].kind is NodeKind.ELEMENT
    assert entry.raw[0] is not entry.tree.root_element


def test_copy_of_preserve_keeps_source_evidence() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy-of select="/v:Order" validation="preserve"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order/@id instance of attribute(*, xs:string)"/>'
        "</xsl:template>"
    )

    source = SOURCE.replace(
        b'<v:Order xmlns:v="urn:example:copy">', b'<v:Order xmlns:v="urn:example:copy" id="A">'
    )
    assert _values(_transform(body, source=source, processor_options={"validation": "strict"})) == (
        "true",
    )
