"""Validation at XSLT construction and delivery boundaries.

These tests exercise public transformation behavior only: constructed element,
attribute, document, and result-document validation, ``default-validation``
scoping, typed intermediate trees read by later XPath, and the exact XSLT
error codes for each validation arm.  No test serializes a result and re-parses
it to obtain an annotation.
"""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic, XdmMap
from pyxsd.xdm.annotations import OccurrenceAnnotation
from pyxsd.xdm.nodes import NodeKind, XdmDocument
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xdm.validation import (
    ConstructedValidationError,
    ValidationRequest,
    validate_document,
    validate_occurrence,
)
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.runtime.builder import validate_constructed
from pyxsd.xslt.runtime.instructions import (
    AttributeInstruction,
    CopyInstruction,
    DocumentInstruction,
    ElementInstruction,
    TemporaryTreeInstruction,
    TextInstruction,
    construct_instruction,
)

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
VAL = "urn:example:validation"
OUT_URI = "http://example.com/constructed.xml"

SCHEMA = f"""
<xs:schema xmlns:xs="{XSD_NS}" xmlns:v="{VAL}" targetNamespace="{VAL}"
    elementFormDefault="qualified">
  <xs:simpleType name="Amount">
    <xs:restriction base="xs:decimal"/>
  </xs:simpleType>
  <xs:simpleType name="Code">
    <xs:restriction base="xs:NCName"/>
  </xs:simpleType>
  <xs:simpleType name="Amount2">
    <xs:restriction base="v:Amount"/>
  </xs:simpleType>
  <xs:simpleType name="QNameLike">
    <xs:restriction base="xs:QName"/>
  </xs:simpleType>
  <xs:simpleType name="QNameUnion">
    <xs:union memberTypes="xs:string xs:QName"/>
  </xs:simpleType>
  <xs:simpleType name="QNameList">
    <xs:list itemType="xs:QName"/>
  </xs:simpleType>
  <xs:complexType name="Line">
    <xs:sequence>
      <xs:element name="value" type="xs:decimal"/>
    </xs:sequence>
    <xs:attribute name="code" type="xs:int" use="required"/>
    <xs:attribute name="note" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="Order">
    <xs:sequence>
      <xs:element name="line" type="v:Line" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string"/>
    <xs:attribute name="kind" type="xs:string" default="std"/>
    <xs:attribute ref="v:glob"/>
  </xs:complexType>
  <xs:complexType name="SpecialOrder">
    <xs:complexContent>
      <xs:extension base="v:Order">
        <xs:sequence>
          <xs:element name="code" type="xs:string"/>
        </xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:complexType name="Unrelated">
    <xs:sequence/>
  </xs:complexType>
  <xs:complexType name="Defaulted">
    <xs:sequence>
      <xs:element name="count" type="xs:int" default="7" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="RequiredDefaulted">
    <xs:sequence>
      <xs:element name="count" type="xs:int" default="7"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="ChoiceDefaulted">
    <xs:choice>
      <xs:element name="a" type="xs:int" default="1" minOccurs="0"/>
      <xs:element name="b" type="xs:int" default="1" minOccurs="0"/>
    </xs:choice>
  </xs:complexType>
  <xs:complexType name="SeqDefaulted">
    <xs:sequence>
      <xs:element name="a" type="xs:int" default="1" minOccurs="0"/>
      <xs:element name="b" type="xs:int"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="LaxWild">
    <xs:sequence>
      <xs:element name="known" type="xs:string"/>
      <xs:any namespace="##other" processContents="lax"
          minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="StrictWild">
    <xs:sequence>
      <xs:element name="known" type="xs:string"/>
      <xs:any namespace="##other" processContents="strict"
          minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="IdDoc">
    <xs:sequence>
      <xs:element name="ref" type="xs:IDREF"/>
      <xs:element name="id" type="xs:ID"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="QNameDoc">
    <xs:attribute name="ref" type="xs:QName"/>
  </xs:complexType>
  <xs:complexType name="Entry">
    <xs:attribute name="key" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="UniqueDoc">
    <xs:sequence>
      <xs:element name="entry" type="v:Entry" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="Order" type="v:Order"/>
  <xs:element name="amount" type="v:Amount"/>
  <xs:element name="defaulted" type="v:Defaulted"/>
  <xs:element name="requiredDefaulted" type="v:RequiredDefaulted"/>
  <xs:element name="choiceDefaulted" type="v:ChoiceDefaulted"/>
  <xs:element name="seqDefaulted" type="v:SeqDefaulted"/>
  <xs:element name="nilledDefaulted" type="v:Defaulted" nillable="true"/>
  <xs:element name="nilledAmount" type="v:Amount" nillable="true"/>
  <xs:element name="laxWild" type="v:LaxWild"/>
  <xs:element name="strictWild" type="v:StrictWild"/>
  <xs:element name="idDoc" type="v:IdDoc"/>
  <xs:element name="nilledOrder" type="v:Order" nillable="true"/>
  <xs:element name="qnameDoc" type="v:QNameDoc"/>
  <xs:element name="uniqueDoc" type="v:UniqueDoc">
    <xs:unique name="u">
      <xs:selector xpath="v:entry"/>
      <xs:field xpath="@key"/>
    </xs:unique>
  </xs:element>
  <xs:attribute name="status" type="v:Code"/>
  <xs:attribute name="fixedStatus" type="xs:string" fixed="X"/>
  <xs:attribute name="glob" type="xs:string" default="g"/>
</xs:schema>
"""


def _stylesheet(body: str, *, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:v="{VAL}" xmlns:xsi="{XSI_NS}" xmlns:o="urn:example:other" '
        f'version="3.0"{attributes}>'
        f"<xsl:import-schema>{SCHEMA}</xsl:import-schema>"
        f"{body}</xsl:stylesheet>"
    ).encode()


def _stylesheet_without_schema(body: str, *, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:v="{VAL}" version="3.0"{attributes}>{body}</xsl:stylesheet>'
    ).encode()


def _compile(body: str, *, attributes: str = "", **options: Any):
    return XsltProcessor(**options).compile(io.BytesIO(_stylesheet(body, attributes=attributes)))


def _transform(
    body: str,
    *,
    source: bytes = b"<in/>",
    attributes: str = "",
    processor_options: dict[str, Any] | None = None,
    delivery: str = "document",
    **options: Any,
):
    compiled = _compile(body, attributes=attributes, **(processor_options or {}))
    return compiled.transform(source, initial_template="main", delivery=delivery, **options)


def _transform_without_schema(
    body: str,
    *,
    delivery: str = "document",
    **options: Any,
):
    compiled = XsltProcessor().compile(io.BytesIO(_stylesheet_without_schema(body)))
    return compiled.transform(initial_template="main", delivery=delivery, **options)


def _error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def _values(result: Any) -> tuple[str, ...]:
    return tuple(
        item.lexical if isinstance(item, XdmAtomic) else str(item) for item in result.principal
    )


def _line(value: str = "1.25", code: str = "1") -> str:
    return (
        f'<xsl:element name="v:line">'
        f'<xsl:attribute name="code">{code}</xsl:attribute>'
        f'<xsl:attribute name="note">n</xsl:attribute>'
        f'<xsl:element name="v:value"><xsl:text>{value}</xsl:text></xsl:element>'
        f"</xsl:element>"
    )


def _order(*lines: str, ident: str = "X") -> str:
    return (
        f'<xsl:element name="v:Order">'
        f'<xsl:attribute name="id">{ident}</xsl:attribute>'
        f"{''.join(lines)}"
        f"</xsl:element>"
    )


# ---------------------------------------------------------------------------
# Typed intermediate trees and later XPath access


def test_typed_intermediate_and_result_validation() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:Order" type="v:Order">'
        '<xsl:attribute name="id">X</xsl:attribute>'
        '<xsl:element name="v:line" type="v:Line">'
        '<xsl:attribute name="code">1</xsl:attribute>'
        '<xsl:element name="v:value"><xsl:text>1.25</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="sum($o//v:value)"/>'
        '<xsl:sequence select="$o/* instance of element(*, v:Order)"/>'
        '<xsl:sequence select="($o//v:value)[1] instance of element(*, xs:decimal)"/>'
        "</xsl:template>"
    )

    result = _transform(body)

    assert result.principal[0] == XdmAtomic.decimal(Decimal("1.25"))
    assert result.principal[1] == XdmAtomic.boolean(True)
    assert result.principal[2] == XdmAtomic.boolean(True)


def test_typed_intermediate_invalid_content_is_xtte1540() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:element name="v:Order" type="v:Order">'
        '<xsl:element name="v:line">'
        '<xsl:element name="v:value"><xsl:text>not-a-decimal</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:element>"
        "</xsl:template>"
    )

    assert _error_code(lambda: _transform(body)) == "XTTE1540"


# ---------------------------------------------------------------------------
# Element validation


def test_strict_element_validation_annotates_tree_and_adds_defaults() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        f'<xsl:element name="v:Order" validation="strict">{_line()}</xsl:element>'
        "</xsl:variable>"
        '<xsl:sequence select="string($o/v:Order/@kind)"/>'
        '<xsl:sequence select="$o/v:Order instance of element(*, v:Order)"/>'
        '<xsl:sequence select="$o/v:Order/v:line/@code instance of attribute(*, xs:int)"/>'
        '<xsl:sequence select="data($o/v:Order/v:line/v:value) instance of xs:decimal"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body)) == ("std", "true", "true", "true")


def test_strict_element_invalid_content_is_xtte1510() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:element name="v:Order" validation="strict">'
        '<xsl:attribute name="id">X</xsl:attribute>'
        "</xsl:element>"
        "</xsl:template>"
    )

    assert _error_code(lambda: _transform(body)) == "XTTE1510"


def test_strict_element_without_declaration_is_xtte1512() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:element name="v:undeclared" validation="strict">'
        '<xsl:element name="v:Order">'
        f"{_line()}"
        "</xsl:element>"
        "</xsl:element>"
        "</xsl:template>"
    )

    assert _error_code(lambda: _transform(body)) == "XTTE1512"


def test_lax_element_without_declaration_validates_known_descendants() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:undeclared" validation="lax">'
        f'<xsl:element name="v:Order">{_line()}</xsl:element>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="$o/*"/>'
        '<xsl:sequence select="$o/v:undeclared/v:Order instance of element(*, v:Order)"/>'
        '<xsl:sequence select="data($o//v:value) instance of xs:decimal"/>'
        "</xsl:template>"
    )

    result = _transform(body, delivery="raw")
    assert result.principal[0].type_name == f"{{{XSD_NS}}}anyType"
    assert result.principal[1] == XdmAtomic.boolean(True)
    assert result.principal[2] == XdmAtomic.boolean(True)


def test_lax_element_with_invalid_known_content_is_xtte1515() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:element name="v:undeclared" validation="lax">'
        '<xsl:element name="v:Order"><xsl:attribute name="id">X</xsl:attribute></xsl:element>'
        "</xsl:element>"
        "</xsl:template>"
    )

    assert _error_code(lambda: _transform(body)) == "XTTE1515"


def test_strict_lax_and_strict_wildcards_follow_process_contents() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="lax">'
        '<xsl:element name="v:laxWild" validation="strict">'
        '<xsl:element name="v:known"><xsl:text>k</xsl:text></xsl:element>'
        '<xsl:element name="o:unknown"><xsl:text>x</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="$lax/v:laxWild/o:unknown instance of element(o:unknown, xs:anyType)"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body)) == ("true",)

    strict_body = (
        '<xsl:template name="main">'
        '<xsl:element name="v:strictWild" validation="strict">'
        '<xsl:element name="v:known"><xsl:text>k</xsl:text></xsl:element>'
        '<xsl:element name="o:unknown"><xsl:text>x</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(strict_body)) == "XTTE1510"


# ---------------------------------------------------------------------------
# Named-type validation


def test_named_simple_type_success_and_failure() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:amount" type="v:Amount">12.50</xsl:element>'
        "</xsl:variable>"
        '<xsl:sequence select="$o/* instance of element(*, v:Amount)"/>'
        '<xsl:sequence select="data($o/*) instance of xs:decimal"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body)) == ("true", "true")

    invalid = (
        '<xsl:template name="main">'
        '<xsl:element name="v:amount" type="v:Amount">not-a-decimal</xsl:element>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(invalid)) == "XTTE1540"


def test_named_complex_type_annotates_nested_elements_and_attributes() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        f'<xsl:element name="v:Order" type="v:Order">{_line()}</xsl:element>'
        "</xsl:variable>"
        '<xsl:sequence select="$o/v:Order/v:line instance of element(*, v:Line)"/>'
        '<xsl:sequence select="$o/v:Order/v:line/@code instance of attribute(*, xs:int)"/>'
        '<xsl:sequence select="$o/v:Order/v:line/v:value instance of element(*, xs:decimal)"/>'
        "</xsl:template>"
    )

    assert _values(_transform(body)) == ("true", "true", "true")


def test_named_type_untyped_untyped_atomic_and_any_type() -> None:
    untyped = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:x" type="xs:untyped"><v:child/></xsl:element>'
        "</xsl:variable>"
        '<xsl:sequence select="$o/v:x instance of element(v:x, xs:untyped)"/>'
        '<xsl:sequence select="$o/v:x/v:child instance of element(v:child, xs:untyped)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(untyped)) == ("true", "true")

    atomic = (
        '<xsl:template name="main">'
        '<xsl:element name="v:x" type="xs:untypedAtomic">text</xsl:element>'
        "</xsl:template>"
    )
    atomic_result = _transform(atomic, delivery="raw")
    assert atomic_result.principal[0].type_name == f"{{{XSD_NS}}}untypedAtomic"

    rejected = (
        '<xsl:template name="main">'
        '<xsl:element name="v:x" type="xs:untypedAtomic"><v:child/></xsl:element>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(rejected)) == "XTTE1540"

    any_type = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:x" type="xs:anyType"><v:child/></xsl:element>'
        "</xsl:variable>"
        '<xsl:sequence select="$o/v:x"/>'
        "</xsl:template>"
    )
    any_result = _transform(any_type, delivery="raw")
    assert any_result.principal[0].type_name == f"{{{XSD_NS}}}anyType"


def test_nilled_occurrences_and_non_nillable_rejection() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:nilledOrder" validation="strict">'
        f'<xsl:attribute name="xsi:nil" namespace="{XSI_NS}">true</xsl:attribute>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="nilled($o/*)"/>'
        '<xsl:sequence select="count($o/*/node())"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body)) == ("true", "0")

    rejected = (
        '<xsl:template name="main">'
        '<xsl:element name="v:Order" validation="strict">'
        f'<xsl:attribute name="xsi:nil" namespace="{XSI_NS}">true</xsl:attribute>'
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(rejected)) == "XTTE1510"


def test_default_element_value_supplies_typed_value_without_text() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:defaulted" validation="strict">'
        '<xsl:element name="v:count"/>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="data($o/v:defaulted/v:count)"/>'
        '<xsl:sequence select="string($o/v:defaulted/v:count)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body)) == ("7", "")


# ---------------------------------------------------------------------------
# Attribute validation


def test_attribute_named_type_strict_and_lax_validation() -> None:
    typed = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:attribute name="v:amount" type="v:Amount">12.5</xsl:attribute>'
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, v:Amount)"/>'
        '<xsl:sequence select="data($a) instance of xs:decimal"/>'
        "</xsl:template>"
    )
    assert _values(_transform(typed)) == ("true", "true")

    strict = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:attribute name="v:status" validation="strict">ok</xsl:attribute>'
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, v:Code)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(strict)) == ("true",)

    unknown = (
        '<xsl:template name="main">'
        '<xsl:attribute name="v:missing" validation="strict">x</xsl:attribute>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(unknown)) == "XTTE1512"

    invalid = (
        '<xsl:template name="main">'
        '<xsl:attribute name="v:status" validation="strict">has space</xsl:attribute>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(invalid)) == "XTTE1510"

    lax = (
        '<xsl:template name="main">'
        '<xsl:attribute name="v:status" validation="lax">has space</xsl:attribute>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(lax)) == "XTTE1515"


def test_attribute_type_static_and_qname_errors() -> None:
    complex_type = (
        '<xsl:template name="main">'
        '<xsl:attribute name="v:x" type="v:Line">x</xsl:attribute>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _compile(complex_type)) == "XTSE1530"

    qname_type = (
        '<xsl:template name="main">'
        '<xsl:attribute name="v:x" type="v:QNameLike">v:amount</xsl:attribute>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(qname_type)) == "XTTE1545"


# ---------------------------------------------------------------------------
# Document and result-document validation


def test_document_validation_annotates_the_single_element_child() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="d" as="document-node()">'
        '<xsl:document validation="strict">'
        f'<xsl:element name="v:Order">{_line()}</xsl:element>'
        "</xsl:document>"
        "</xsl:variable>"
        '<xsl:sequence select="$d/v:Order instance of element(*, v:Order)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body)) == ("true",)


def test_document_validation_rejects_non_document_shapes() -> None:
    two_elements = (
        '<xsl:template name="main">'
        '<xsl:document validation="strict"><a/><b/></xsl:document>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(two_elements)) == "XTTE1550"

    text_and_element = (
        '<xsl:template name="main">'
        '<xsl:document validation="strict">'
        "<xsl:text>text</xsl:text>"
        f'<xsl:element name="v:Order">{_line()}</xsl:element>'
        "</xsl:document>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(text_and_element)) == "XTTE1550"


def test_document_validation_maps_idref_failures_to_xtte1555() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:document validation="strict">'
        '<xsl:element name="v:idDoc">'
        '<xsl:element name="v:ref"><xsl:text>missing</xsl:text></xsl:element>'
        '<xsl:element name="v:id"><xsl:text>one</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:document>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(body)) == "XTTE1555"


def test_result_document_validation_annotates_the_delivered_tree() -> None:
    body = (
        '<xsl:template name="main">'
        f'<xsl:result-document href="{OUT_URI}" validation="strict">'
        f'<xsl:element name="v:Order">{_line()}</xsl:element>'
        "</xsl:result-document>"
        "</xsl:template>"
    )

    result = _transform(body, delivery="raw")

    entry = result.secondary[OUT_URI]
    assert entry.tree is not None
    assert entry.document is entry.tree
    assert entry.tree.root_element.type_name == f"{{{VAL}}}Order"
    assert entry.raw[0].kind is NodeKind.ELEMENT


def test_result_document_validation_failure_and_type_semantics() -> None:
    invalid = (
        '<xsl:template name="main">'
        f'<xsl:result-document href="{OUT_URI}" validation="strict">'
        '<xsl:element name="v:Order"><xsl:attribute name="id">X</xsl:attribute></xsl:element>'
        "</xsl:result-document>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(invalid, delivery="raw")) == "XTTE1510"

    typed = (
        '<xsl:template name="main">'
        f'<xsl:result-document href="{OUT_URI}" type="v:Order">'
        f'<xsl:element name="v:Order">{_line()}</xsl:element>'
        "</xsl:result-document>"
        "</xsl:template>"
    )
    result = _transform(typed, delivery="raw")
    assert result.secondary[OUT_URI].document.root_element.type_name == f"{{{VAL}}}Order"


def test_result_document_does_not_validate_a_raw_map_delivery() -> None:
    body = (
        '<xsl:template name="main">'
        f'<xsl:result-document href="{OUT_URI}" method="json" build-tree="no" '
        'validation="strict">'
        "<xsl:map>"
        '<xsl:map-entry key="\'k\'" select="1"/>'
        "</xsl:map>"
        "</xsl:result-document>"
        "</xsl:template>"
    )

    result = _transform(body, delivery="raw")

    entry = result.secondary[OUT_URI]
    assert entry.tree is None
    assert len(entry.raw) == 1
    assert isinstance(entry.raw[0], XdmMap)


# ---------------------------------------------------------------------------
# default-validation scoping, modes, and static errors


def test_default_validation_scoping_nearest_and_module() -> None:
    preserved = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<out xsl:validation="preserve" xsl:default-validation="preserve">'
        '<xsl:copy-of select="/v:Order"/>'
        "</out>"
        "</xsl:variable>"
        '<xsl:sequence select="$c/out instance of element(*, xs:anyType)"/>'
        '<xsl:sequence select="$c/out/v:Order instance of element(*, v:Order)"/>'
        "</xsl:template>"
    )
    assert _values(
        _transform(
            preserved,
            source=b'<v:Order xmlns:v="urn:example:validation"><v:line code="1" note="n"><v:value>1.25</v:value></v:line></v:Order>',
            processor_options={"validation": "strict"},
        )
    ) == ("true", "true")

    stripped = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<out xsl:validation="preserve">'
        '<mid xsl:default-validation="strip">'
        '<xsl:copy-of select="/v:Order"/>'
        "</mid>"
        "</out>"
        "</xsl:variable>"
        '<xsl:sequence select="$c/out/mid/v:Order instance of element(v:Order, xs:untyped)"/>'
        "</xsl:template>"
    )
    assert _values(
        _transform(
            stripped,
            source=b'<v:Order xmlns:v="urn:example:validation"><v:line code="1" note="n"><v:value>1.25</v:value></v:line></v:Order>',
            processor_options={"validation": "strict"},
        )
    ) == ("true",)


def test_default_validation_does_not_cross_an_imported_module() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="lib" as="element()">'
        '<xsl:call-template name="lib:t"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$lib instance of element(*, xs:anyType)"/>'
        '<xsl:sequence select="$lib/v:Order instance of element(*, v:Order)"/>'
        "</xsl:template>"
    )
    library = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:v="{VAL}" xmlns:lib="{VAL}:lib" version="3.0" '
        'default-validation="preserve">'
        '<xsl:template name="lib:t"><lib:out><xsl:copy-of select="/v:Order"/></lib:out>'
        "</xsl:template>"
        "</xsl:stylesheet>"
    ).encode()
    principal = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:v="{VAL}" xmlns:lib="{VAL}:lib" version="3.0">'
        "<xsl:import-schema>"
        f"{SCHEMA}"
        "</xsl:import-schema>"
        '<xsl:include href="library.xsl"/>'
        f"{body}</xsl:stylesheet>"
    ).encode()

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "library.xsl").write_bytes(library)
        (root / "main.xsl").write_bytes(principal)
        compiled = XsltProcessor().compile(root / "main.xsl")
        result = compiled.transform(
            b'<v:Order xmlns:v="urn:example:validation"><v:line code="1" note="n"><v:value>1.25</v:value></v:line></v:Order>',
            initial_template="main",
            validation="strict",
            delivery="raw",
        )

    assert _values(result) == ("true", "true")


def test_typed_modes_reject_untyped_and_untyped_modes_reject_typed() -> None:
    untyped = (
        '<xsl:mode name="m" typed="yes"/>'
        '<xsl:template name="main">'
        '<xsl:apply-templates select="/*" mode="m"/>'
        "</xsl:template>"
        '<xsl:template match="*" mode="m"/>'
    )
    assert _error_code(lambda: _transform(untyped)) == "XTTE3100"

    no_mode = (
        '<xsl:mode name="m" typed="no"/>'
        '<xsl:template name="main">'
        '<xsl:apply-templates select="/*" mode="m"/>'
        "</xsl:template>"
        '<xsl:template match="*" mode="m"/>'
    )
    assert (
        _error_code(
            lambda: _transform(
                no_mode,
                source=b'<v:Order xmlns:v="urn:example:validation"><v:line code="1" note="n"><v:value>1.25</v:value></v:line></v:Order>',
                processor_options={"validation": "strict"},
            )
        )
        == "XTTE3110"
    )


def test_static_validation_attribute_errors() -> None:
    both = (
        '<xsl:template name="main">'
        '<xsl:element name="v:amount" type="v:Amount" validation="strict"/>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _compile(both)) == "XTSE1505"

    both_result = (
        '<xsl:template name="main">'
        f'<xsl:result-document href="{OUT_URI}" type="v:Order" validation="strict"/>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _compile(both_result)) == "XTSE1505"

    unknown = (
        '<xsl:template name="main"><xsl:element name="v:amount" type="v:Missing"/></xsl:template>'
    )
    assert _error_code(lambda: _compile(unknown)) == "XTSE1520"


def test_non_schema_aware_construction_validation_is_xtse1660() -> None:
    strict = (
        '<xsl:template name="main"><xsl:element name="out" validation="strict"/></xsl:template>'
    )
    assert (
        _error_code(
            lambda: XsltProcessor(schema_aware=False).compile(
                io.BytesIO(_stylesheet_without_schema(strict))
            )
        )
        == "XTSE1660"
    )

    typed = '<xsl:template name="main"><xsl:element name="out" type="xs:string"/></xsl:template>'
    assert (
        _error_code(
            lambda: XsltProcessor(schema_aware=False).compile(
                io.BytesIO(_stylesheet_without_schema(typed))
            )
        )
        == "XTSE1660"
    )

    preserved = (
        '<xsl:template name="main">'
        '<xsl:element name="out" validation="preserve"><xsl:text>x</xsl:text></xsl:element>'
        "</xsl:template>"
    )
    compiled = XsltProcessor(schema_aware=False).compile(
        io.BytesIO(_stylesheet_without_schema(preserved))
    )
    result = compiled.transform(initial_template="main", delivery="raw")
    assert result.principal[0].local_name == "out"


# ---------------------------------------------------------------------------
# Additional public and construction-seam coverage


def _schema_pool() -> SchemaPool:
    return SchemaPool.from_imports([io.BytesIO(SCHEMA.encode())])


def test_validation_request_and_public_machinery_guards() -> None:
    with pytest.raises(ValueError, match="validation mode"):
        ValidationRequest("bogus")
    with pytest.raises(TypeError, match="type name"):
        ValidationRequest("strip", 1)  # type: ignore[arg-type]

    document = XdmDocument.parse(b"<root><child/></root>")
    with pytest.raises(ValueError, match="construction operation"):
        validate_occurrence(
            document.root_element,
            ValidationRequest(),
            schema_pool=None,
            operation="bogus",  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="XdmDocument"):
        validate_document(object(), ValidationRequest(), schema_pool=None)  # type: ignore[arg-type]

    validate_occurrence(document, ValidationRequest("strip"), schema_pool=None)
    assert document.root_element.type_name == f"{{{XSD_NS}}}untyped"
    assert document.root_element.children[0].type_name == f"{{{XSD_NS}}}untyped"

    preserve = XdmDocument.parse(b"<root/>")
    validate_document(preserve, ValidationRequest("preserve"), schema_pool=None)
    assert preserve.root_element.type_name == f"{{{XSD_NS}}}untyped"


def test_lax_without_a_schema_pool_annotates_any_type() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:element name="out" validation="lax"><xsl:text>x</xsl:text></xsl:element>'
        "</xsl:template>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(_stylesheet_without_schema(body)))
    result = compiled.transform(initial_template="main", delivery="raw")

    assert result.principal[0].type_name == f"{{{XSD_NS}}}anyType"


def test_builtin_named_types_do_not_require_an_imported_schema() -> None:
    element = (
        '<xsl:template name="main">'
        '<xsl:variable name="o"><xsl:element name="out" type="xs:string">hello</xsl:element></xsl:variable>'
        '<xsl:sequence select="string($o/*)"/>'
        '<xsl:sequence select="$o/* instance of element(*, xs:string)"/>'
        "</xsl:template>"
    )
    assert _values(_transform_without_schema(element)) == ("hello", "true")

    attribute = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:attribute name="a" type="xs:int">3</xsl:attribute>'
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, xs:int)"/>'
        "</xsl:template>"
    )
    assert _values(_transform_without_schema(attribute)) == ("true",)

    document = (
        '<xsl:template name="main">'
        '<xsl:document type="xs:string"><out>hello</out></xsl:document>'
        "</xsl:template>"
    )
    result = _transform_without_schema(document, delivery="raw")
    assert result.principal[0].root_element.type_name == f"{{{XSD_NS}}}string"

    result_document = (
        '<xsl:template name="main">'
        f'<xsl:result-document href="{OUT_URI}" type="xs:string"><out>hello</out></xsl:result-document>'
        "</xsl:template>"
    )
    delivered = _transform_without_schema(result_document, delivery="raw")
    assert delivered.secondary[OUT_URI].document.root_element.type_name == f"{{{XSD_NS}}}string"

    copied = '<xsl:template name="main"><xsl:copy-of select="42" type="xs:integer"/></xsl:template>'
    assert _values(_transform_without_schema(copied, delivery="raw")) == ("42",)


def test_named_simple_type_rejects_element_children() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:element name="v:amount" type="v:Amount"><v:x/></xsl:element>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(body)) == "XTTE1540"


def test_xsi_type_override_resolution_and_failures() -> None:
    special = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:Order" type="v:Order">'
        f'<xsl:attribute name="xsi:type" namespace="{XSI_NS}">v:SpecialOrder</xsl:attribute>'
        f"{_line()}"
        '<xsl:element name="v:code"><xsl:text>c</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="$o/v:Order"/>'
        "</xsl:template>"
    )
    result = _transform(special, delivery="raw")
    assert result.principal[0].type_name == f"{{{VAL}}}SpecialOrder"

    unknown = special.replace("v:SpecialOrder", "v:Missing")
    assert _error_code(lambda: _transform(unknown)) == "XTTE1540"

    unrelated = special.replace("v:SpecialOrder", "v:Unrelated")
    assert _error_code(lambda: _transform(unrelated)) == "XTTE1540"


def test_attribute_lax_unknown_declaration_and_untyped_atomic_type() -> None:
    lax = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:attribute name="v:missing" validation="lax">x</xsl:attribute>'
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, xs:untypedAtomic)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(lax)) == ("true",)

    untyped_atomic = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:attribute name="a" type="xs:untypedAtomic">x</xsl:attribute>'
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, xs:untypedAtomic)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(untyped_atomic)) == ("true",)


def test_qname_union_and_list_attribute_types_are_xtte1545() -> None:
    for type_name in ("v:QNameUnion", "v:QNameList"):
        body = (
            '<xsl:template name="main">'
            f'<xsl:attribute name="a" type="{type_name}">v:amount</xsl:attribute>'
            "</xsl:template>"
        )
        assert _error_code(lambda body=body: _transform(body)) == "XTTE1545"


def test_attribute_type_runtime_guards() -> None:
    pool = _schema_pool()
    attribute = XdmDocument.parse(b'<root a="not-a-decimal"/>').root_element.attributes[0]

    with pytest.raises(ConstructedValidationError) as complex_type:
        validate_occurrence(
            attribute,
            ValidationRequest("strip", f"{{{XSD_NS}}}anyType"),
            schema_pool=pool,
        )
    assert complex_type.value.reason == "type"

    with pytest.raises(ConstructedValidationError) as no_pool:
        validate_occurrence(
            attribute,
            ValidationRequest("strip", f"{{{VAL}}}Amount"),
            schema_pool=None,
        )
    assert no_pool.value.reason == "type"

    with pytest.raises(ConstructedValidationError) as unknown:
        validate_occurrence(
            attribute,
            ValidationRequest("strip", f"{{{VAL}}}Missing"),
            schema_pool=pool,
        )
    assert unknown.value.reason == "type"

    with pytest.raises(ConstructedValidationError) as invalid:
        validate_occurrence(
            attribute,
            ValidationRequest("strip", f"{{{VAL}}}Amount"),
            schema_pool=pool,
        )
    assert invalid.value.reason == "type"


def test_namespaced_default_attribute_creates_a_prefix() -> None:
    pool = _schema_pool()
    document = XdmDocument.parse(
        b'<Order xmlns="urn:example:validation"><line code="1"><value>1.25</value></line></Order>'
    )
    validate_occurrence(document.root_element, ValidationRequest("strict"), schema_pool=pool)

    attributes = {attribute.name: attribute for attribute in document.root_element.attributes}
    default = attributes[f"{{{VAL}}}glob"]
    assert default.string_value == "g"
    assert default.namespace_uri == VAL
    assert any(node.prefix == default.prefix for node in document.root_element.namespace_nodes)


def test_literal_result_element_type_and_validation() -> None:
    typed = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<v:amount xsl:type="v:Amount">12.50</v:amount>'
        "</xsl:variable>"
        '<xsl:sequence select="$o/v:amount instance of element(*, v:Amount)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(typed)) == ("true",)

    strict = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<v:amount xsl:validation="strict">12.50</v:amount>'
        "</xsl:variable>"
        '<xsl:sequence select="$o/v:amount instance of element(*, v:Amount)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(strict)) == ("true",)


def test_element_and_attribute_namespace_avts() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        f'<xsl:element name="Order" namespace="{VAL}" validation="strict">'
        f"{_line()}"
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="$o/* instance of element(*, v:Order)"/>'
        '<xsl:variable name="a" as="attribute()">'
        f'<xsl:attribute name="status" namespace="{VAL}" validation="strict">ok</xsl:attribute>'
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, v:Code)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body)) == ("true", "true")


def test_document_type_and_copy_document_nodes() -> None:
    typed_document = (
        '<xsl:template name="main">'
        '<xsl:variable name="d" as="document-node()">'
        '<xsl:document type="v:Order">'
        f'<xsl:element name="v:Order">{_line()}</xsl:element>'
        "</xsl:document>"
        "</xsl:variable>"
        '<xsl:sequence select="$d/v:Order instance of element(*, v:Order)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(typed_document)) == ("true",)

    shallow = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy select="/" validation="preserve"/>'
        "</xsl:variable>"
        '<xsl:sequence select="count($c/node())"/>'
        "</xsl:template>"
    )
    assert _values(
        _transform(
            shallow,
            source=b'<v:Order xmlns:v="urn:example:validation"><v:line code="1">'
            b"<v:value>1.25</v:value></v:line></v:Order>",
            processor_options={"validation": "strict"},
        )
    ) == ("0",)

    copied = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy select="/" validation="preserve" xsl:default-validation="preserve">'
        '<xsl:copy-of select="/" validation="preserve"/>'
        "</xsl:copy>"
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order instance of element(*, v:Order)"/>'
        "</xsl:template>"
    )
    assert _values(
        _transform(
            copied,
            source=b'<v:Order xmlns:v="urn:example:validation"><v:line code="1">'
            b"<v:value>1.25</v:value></v:line></v:Order>",
            processor_options={"validation": "strict"},
        )
    ) == ("true",)


def test_copy_attribute_strict_validation() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:for-each select="/root/@v:status">'
        '<xsl:copy validation="strict"/>'
        "</xsl:for-each>"
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, v:Code)"/>'
        "</xsl:template>"
    )
    assert _values(
        _transform(
            body,
            source=b'<root xmlns:v="urn:example:validation" v:status="ok"/>',
        )
    ) == ("true",)


def test_result_document_default_validation_keeps_raw_and_tree_separate() -> None:
    body = (
        '<xsl:template name="main">'
        f'<xsl:result-document href="{OUT_URI}"><xsl:copy-of select="/v:Order" validation="preserve"/></xsl:result-document>'
        "</xsl:template>"
    )
    source = (
        b'<v:Order xmlns:v="urn:example:validation"><v:line code="1">'
        b"<v:value>1.25</v:value></v:line></v:Order>"
    )

    stripped = _transform(
        body, source=source, delivery="raw", processor_options={"validation": "strict"}
    )
    entry = stripped.secondary[OUT_URI]
    assert entry.raw[0].type_name == f"{{{VAL}}}Order"
    assert entry.document is not None
    assert entry.document.root_element.type_name == f"{{{XSD_NS}}}untyped"

    preserved = _transform(
        body,
        source=source,
        delivery="raw",
        attributes=' default-validation="preserve"',
        processor_options={"validation": "strict"},
    )
    entry = preserved.secondary[OUT_URI]
    assert entry.document is not None
    assert entry.document.root_element.type_name == f"{{{VAL}}}Order"


def test_principal_tree_default_validation_is_applied_at_delivery() -> None:
    body = '<xsl:template name="main"><xsl:copy-of select="/v:Order"/></xsl:template>'
    source = (
        b'<v:Order xmlns:v="urn:example:validation"><v:line code="1">'
        b"<v:value>1.25</v:value></v:line></v:Order>"
    )

    stripped = _transform(body, source=source, processor_options={"validation": "strict"})
    assert stripped.principal_tree.root_element.type_name == f"{{{XSD_NS}}}untyped"

    preserved = _transform(
        body,
        source=source,
        attributes=' default-validation="preserve"',
        processor_options={"validation": "strict"},
    )
    assert preserved.principal_tree.root_element.type_name == f"{{{VAL}}}Order"


def test_construction_instruction_validation_boundaries() -> None:
    pool = _schema_pool()
    order = ElementInstruction(
        f"{{{VAL}}}Order",
        content=(
            ElementInstruction(
                f"{{{VAL}}}line",
                attributes=(("code", "1"),),
                content=(
                    ElementInstruction(f"{{{VAL}}}value", content=(TextInstruction("1.25"),)),
                ),
            ),
        ),
        validation="strict",
        schema_pool=pool,
    )
    element = construct_instruction(order)[0]
    assert element.type_name == f"{{{VAL}}}Order"

    document = construct_instruction(
        DocumentInstruction(content=(order,), validation="strict", schema_pool=pool)
    )[0]
    assert isinstance(document, XdmDocument)
    assert document.root_element.type_name == f"{{{VAL}}}Order"

    attribute = construct_instruction(
        AttributeInstruction(f"{{{VAL}}}status", "ok", validation="strict", schema_pool=pool)
    )[0]
    assert attribute.type_name == f"{{{VAL}}}Code"

    source = XdmDocument.parse(b'<a code="1"/>').root_element
    source._apply_occurrence_annotation(
        OccurrenceAnnotation(
            type_name=f"{{{VAL}}}Code",
            validation_attempted="full",
        )
    )
    copied = construct_instruction(
        CopyInstruction(source, validation="preserve", schema_pool=pool)
    )[0]
    assert copied.type_name == f"{{{XSD_NS}}}anyType"

    copied_document = construct_instruction(
        CopyInstruction(
            XdmDocument.parse(b"<a/>"),
            validation="strip",
            schema_pool=pool,
        )
    )[0]
    assert isinstance(copied_document, XdmDocument)
    assert copied_document.root_element.type_name == f"{{{XSD_NS}}}untyped"


def test_element_type_unknown_runtime_guard_and_non_element_occurrence() -> None:
    pool = _schema_pool()
    node = XdmDocument.parse(b'<root a="1"/>').root_element
    with pytest.raises(ConstructedValidationError) as unknown:
        validate_occurrence(
            node,
            ValidationRequest("strip", f"{{{VAL}}}Missing"),
            schema_pool=pool,
        )
    assert unknown.value.reason == "type"

    document = XdmDocument.parse(b"<root>text</root>")
    validate_occurrence(
        document.root_element.children[0], ValidationRequest("strict"), schema_pool=None
    )


def test_xsi_type_equal_requested_unprefixed_and_unresolved_prefix() -> None:
    pool = _schema_pool()
    same = XdmDocument.parse(
        (
            f'<Order xmlns="{VAL}" xmlns:xsi="{XSI_NS}" xsi:type="Order"><line code="1">'
            "<value>1.25</value></line></Order>"
        ).encode()
    )
    validate_occurrence(
        same.root_element,
        ValidationRequest("strip", f"{{{VAL}}}Order"),
        schema_pool=pool,
    )
    assert same.root_element.type_name == f"{{{VAL}}}Order"

    unresolved = (
        '<xsl:template name="main">'
        '<xsl:element name="v:Order" type="v:Order">'
        f'<xsl:attribute name="xsi:type" namespace="{XSI_NS}">bad:Missing</xsl:attribute>'
        f"{_line()}"
        "</xsl:element>"
        "</xsl:template>"
    )
    unresolved_result = _transform(unresolved, delivery="raw")
    assert unresolved_result.principal[0].type_name == f"{{{VAL}}}Order"


def test_attribute_type_complex_and_untyped_guards() -> None:
    pool = _schema_pool()
    attribute = XdmDocument.parse(b'<root a="1"/>').root_element.attributes[0]
    for type_name in (f"{{{VAL}}}Line", f"{{{XSD_NS}}}untyped"):
        with pytest.raises(ConstructedValidationError) as error:
            validate_occurrence(
                attribute,
                ValidationRequest("strip", type_name),
                schema_pool=pool,
            )
        assert error.value.reason == "type"


def test_namespaced_default_attribute_skips_existing_prefixes() -> None:
    pool = _schema_pool()
    document = XdmDocument.parse(
        (
            f'<Order xmlns="{VAL}" xmlns:ns1="urn:unused"><line code="1">'
            "<value>1.25</value></line></Order>"
        ).encode()
    )
    validate_occurrence(document.root_element, ValidationRequest("strict"), schema_pool=pool)

    default = next(
        attribute
        for attribute in document.root_element.attributes
        if attribute.name == f"{{{VAL}}}glob"
    )
    assert default.prefix == "ns2"
    assert any(node.prefix == "ns2" for node in document.root_element.namespace_nodes)


def test_temporary_tree_instruction_validation_boundary() -> None:
    pool = _schema_pool()
    order = ElementInstruction(
        f"{{{VAL}}}Order",
        content=(
            ElementInstruction(
                f"{{{VAL}}}line",
                attributes=(("code", "1"), ("note", "n")),
                content=(
                    ElementInstruction(f"{{{VAL}}}value", content=(TextInstruction("1.25"),)),
                ),
            ),
        ),
    )
    document = construct_instruction(
        TemporaryTreeInstruction(content=(order,), validation="strict", schema_pool=pool)
    )[0]
    assert isinstance(document, XdmDocument)
    assert document.root_element.type_name == f"{{{VAL}}}Order"


def test_type_validation_copies_all_attributes_and_unprefixed_xsi_type() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:Order" type="v:Order">'
        f"{_line()}"
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="string($o/v:Order/v:line/@note)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body)) == ("n",)

    unprefixed = (
        '<xsl:template name="main">'
        '<xsl:element name="v:Order" type="v:Order">'
        '<xsl:attribute name="xsi:type">Order</xsl:attribute>'
        f"{_line()}"
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(unprefixed)) == "XTTE1540"


def test_builder_document_validation_boundary() -> None:
    document = XdmDocument.parse(b"<root><child/></root>")
    validate_constructed(document, validation="strip", schema_pool=None)
    assert document.root_element.type_name == f"{{{XSD_NS}}}untyped"
    assert document.root_element.children[0].type_name == f"{{{XSD_NS}}}untyped"


def test_xsl_element_attribute_sets_and_copy_document_content() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:Order" use-attribute-sets="attrs" validation="strict">'
        f"{_line()}"
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="string($o/v:Order/@id)"/>'
        "</xsl:template>"
        '<xsl:attribute-set name="attrs">'
        '<xsl:attribute name="id">X</xsl:attribute>'
        "</xsl:attribute-set>"
    )
    assert _values(_transform(body)) == ("X",)

    copied = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy select="/" validation="preserve" xsl:default-validation="preserve">'
        '<xsl:copy-of select="/" validation="preserve"/>'
        "</xsl:copy>"
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order instance of element(*, v:Order)"/>'
        "</xsl:template>"
    )
    assert _values(
        _transform(
            copied,
            source=b'<v:Order xmlns:v="urn:example:validation"><v:line code="1" '
            b'note="n"><v:value>1.25</v:value></v:line></v:Order>',
            processor_options={"validation": "strict"},
        )
    ) == ("true",)


def test_xsl_copy_select_forms_and_empty_element_copy() -> None:
    empty = '<xsl:template name="main"><xsl:copy select="()"/></xsl:template>'
    assert _values(_transform(empty, delivery="raw")) == ()

    multiple = '<xsl:template name="main"><xsl:copy select="(1, 2)"/></xsl:template>'
    assert _error_code(lambda: _transform(multiple, delivery="raw")) == "XTTE3180"

    atomic = '<xsl:template name="main"><xsl:copy select="\'text\'"/></xsl:template>'
    assert _values(_transform(atomic, delivery="raw")) == ("text",)

    no_context = '<xsl:function name="v:f" visibility="public"><xsl:copy/></xsl:function>'
    compiled = _compile(no_context)
    assert (
        _error_code(lambda: compiled.transform(initial_function="v:f", delivery="raw"))
        == "XTTE0945"
    )

    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy select="/v:Order" use-attribute-sets="attrs" validation="preserve"/>'
        "</xsl:variable>"
        '<xsl:sequence select="string($c/v:Order/@id)"/>'
        "</xsl:template>"
        '<xsl:attribute-set name="attrs">'
        '<xsl:attribute name="id">Y</xsl:attribute>'
        "</xsl:attribute-set>"
    )
    assert _values(
        _transform(
            body,
            source=b'<v:Order xmlns:v="urn:example:validation"><v:line code="1" '
            b'note="n"><v:value>1.25</v:value></v:line></v:Order>',
        )
    ) == ("Y",)


def test_xsl_copy_of_attribute_simple_type_does_not_raise_copy_type() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:copy-of select="/root/@v:status" type="xs:string"/>'
        "</xsl:variable>"
        '<xsl:sequence select="$a instance of attribute(*, xs:string)"/>'
        "</xsl:template>"
    )
    assert _values(
        _transform(body, source=b'<root xmlns:v="urn:example:validation" v:status="ok"/>')
    ) == ("true",)


def test_constructed_name_forms_and_errors() -> None:
    qname = (
        '<xsl:template name="main">'
        '<xsl:variable name="o"><xsl:element name="Q{{urn:q}}foo"/></xsl:variable>'
        '<xsl:sequence select="namespace-uri($o/*)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(qname, delivery="raw")) == ("urn:q",)

    clark = (
        '<xsl:template name="main">'
        '<xsl:variable name="o"><xsl:element name="{{urn:q}}foo"/></xsl:variable>'
        '<xsl:sequence select="namespace-uri($o/*)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(clark, delivery="raw")) == ("urn:q",)

    default_namespace = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        'xmlns="urn:default" version="3.0"><xsl:import-schema>'
        f"{SCHEMA}"
        "</xsl:import-schema>"
        '<xsl:template name="main">'
        '<xsl:variable name="o"><xsl:element name="out"/></xsl:variable>'
        '<xsl:sequence select="namespace-uri($o/*)"/>'
        "</xsl:template></xsl:stylesheet>"
    ).encode()
    compiled = XsltProcessor().compile(io.BytesIO(default_namespace))
    assert _values(compiled.transform(initial_template="main", delivery="raw")) == ("urn:default",)

    for body, code in (
        ('<xsl:element name="{}"/>', "XTDE0820"),
        ('<xsl:element name="1bad"/>', "XTDE0820"),
        ('<xsl:element name="Q{{urn:q}}1bad"/>', "XTDE0820"),
        ('<xsl:element name="p:x"/>', "XTDE0830"),
        (
            '<xsl:element name="x" namespace="http://www.w3.org/2000/xmlns/"/>',
            "XTDE0835",
        ),
    ):
        stylesheet = f'<xsl:template name="main">{body}</xsl:template>'
        assert _error_code(lambda s=stylesheet: _transform(s, delivery="raw")) == code


def test_construction_static_attribute_errors() -> None:
    no_name = '<xsl:template name="main"><xsl:element/></xsl:template>'
    assert _error_code(lambda: _compile(no_name)) == "XTSE0010"

    no_select = '<xsl:template name="main"><xsl:copy-of/></xsl:template>'
    assert _error_code(lambda: _compile(no_select)) == "XTSE0010"

    any_type_attribute = (
        '<xsl:template name="main">'
        '<xsl:attribute name="a" type="xs:anyType">x</xsl:attribute>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _compile(any_type_attribute)) == "XTSE1530"


def test_typed_mode_skips_non_element_items_and_accepts_typed_nodes() -> None:
    text = (
        '<xsl:mode name="m" typed="yes"/>'
        '<xsl:template name="main"><xsl:apply-templates select="\'text\'" mode="m"/></xsl:template>'
        '<xsl:template match="text()" mode="m"/>'
    )
    text_result = _transform(text, delivery="raw")
    assert text_result.principal[0].string_value == "text"

    typed = (
        '<xsl:mode name="m" typed="yes"/>'
        '<xsl:template name="main"><xsl:apply-templates select="/*" mode="m"/></xsl:template>'
        '<xsl:template match="*" mode="m"><xsl:text>ok</xsl:text></xsl:template>'
    )
    typed_result = _transform(
        typed,
        source=b'<v:Order xmlns:v="urn:example:validation"><v:line code="1" '
        b'note="n"><v:value>1.25</v:value></v:line></v:Order>',
        processor_options={"validation": "strict"},
    )
    assert typed_result.principal[0].string_value == "ok"

    unspecified = (
        '<xsl:mode name="m" typed="unspecified"/>'
        '<xsl:template name="main"><xsl:apply-templates select="/*" mode="m"/></xsl:template>'
        '<xsl:template match="*" mode="m"><xsl:text>ok</xsl:text></xsl:template>'
    )
    unspecified_result = _transform(unspecified)
    assert unspecified_result.principal[0].string_value == "ok"


def test_construction_validation_value_is_xtse0020() -> None:
    body = '<xsl:template name="main"><xsl:element name="out" validation="bogus"/></xsl:template>'
    assert _error_code(lambda: _compile(body)) == "XTSE0020"


def test_named_simple_type_attributes_and_xsi_type_overrides() -> None:
    extra_attribute = (
        '<xsl:template name="main">'
        '<xsl:element name="v:amount" type="v:Amount">'
        '<xsl:attribute name="extra">x</xsl:attribute>12.5'
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(extra_attribute)) == "XTTE1540"

    invalid_override = (
        '<xsl:template name="main">'
        '<xsl:element name="v:amount" type="v:Amount">'
        f'<xsl:attribute name="xsi:type" namespace="{XSI_NS}">v:Code</xsl:attribute>12.5'
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(invalid_override)) == "XTTE1540"

    derived = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:amount" type="v:Amount">'
        f'<xsl:attribute name="xsi:type" namespace="{XSI_NS}">v:Amount2</xsl:attribute>12.5'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="$o/*"/>'
        "</xsl:template>"
    )
    result = _transform(derived, delivery="raw")
    assert result.principal[0].type_name == f"{{{VAL}}}Amount2"


def test_named_type_document_constraints() -> None:
    idref = (
        '<xsl:template name="main">'
        '<xsl:document type="v:IdDoc">'
        '<xsl:element name="v:idDoc">'
        '<xsl:element name="v:ref"><xsl:text>missing</xsl:text></xsl:element>'
        '<xsl:element name="v:id"><xsl:text>one</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:document>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(idref)) == "XTTE1555"

    duplicate_key = (
        '<xsl:template name="main">'
        '<xsl:document type="v:UniqueDoc">'
        '<xsl:element name="v:uniqueDoc">'
        '<xsl:element name="v:entry"><xsl:attribute name="key">k</xsl:attribute></xsl:element>'
        '<xsl:element name="v:entry"><xsl:attribute name="key">k</xsl:attribute></xsl:element>'
        "</xsl:element>"
        "</xsl:document>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(duplicate_key)) == "XTTE1555"


def test_lax_validation_checks_declared_attributes_and_any_type_children() -> None:
    invalid_attribute = (
        '<xsl:template name="main">'
        '<xsl:element name="v:undeclared" validation="lax">'
        '<xsl:attribute name="v:status">has space</xsl:attribute>'
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(invalid_attribute)) == "XTTE1515"

    valid_attribute = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:undeclared" validation="lax">'
        '<xsl:attribute name="v:status">ok</xsl:attribute>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="$o/*/@v:status instance of attribute(*, v:Code)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(valid_attribute)) == ("true",)

    invalid_any_type_child = (
        '<xsl:template name="main">'
        '<xsl:element name="v:any" type="xs:anyType">'
        '<xsl:element name="v:Order"><xsl:attribute name="id">X</xsl:attribute></xsl:element>'
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(invalid_any_type_child)) == "XTTE1540"


def test_initial_typed_mode_invocation_checks_the_selected_nodes() -> None:
    stylesheet = (
        '<xsl:mode name="m" typed="yes"/>'
        '<xsl:template match="*" mode="m"><xsl:text>ok</xsl:text></xsl:template>'
    )
    compiled = _compile(stylesheet)

    assert (
        _error_code(
            lambda: compiled.transform(
                source=b"<in/>",
                initial_mode="m",
                delivery="raw",
            )
        )
        == "XTTE3100"
    )

    typed = compiled.transform(
        source=(
            b'<v:Order xmlns:v="urn:example:validation"><v:line code="1" '
            b'note="n"><v:value>1.25</v:value></v:line></v:Order>'
        ),
        initial_mode="m",
        validation="strict",
        delivery="raw",
    )
    assert typed.principal[0].string_value == "ok"


def test_fixed_attribute_constraints_are_enforced() -> None:
    valid = (
        '<xsl:template name="main">'
        '<xsl:variable name="a" as="attribute()">'
        '<xsl:attribute name="v:fixedStatus" validation="strict">X</xsl:attribute>'
        "</xsl:variable>"
        '<xsl:sequence select="string($a)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(valid)) == ("X",)

    invalid = (
        '<xsl:template name="main">'
        '<xsl:attribute name="v:fixedStatus" validation="strict">Y</xsl:attribute>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(invalid)) == "XTTE1510"


def test_xsl_copy_select_runs_the_constructor_with_the_selected_focus() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:copy select="/doc/foo"><xsl:value-of select="."/></xsl:copy>'
        "</xsl:template>"
    )
    result = _transform(body, source=b"<doc><foo>hello</foo><bar>bye</bar></doc>", delivery="raw")
    assert result.principal[0].string_value == "hello"


def test_xsl_copy_type_on_attribute_is_xtte1535() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:copy select="/root/@v:status" type="v:Line"/>'
        "</xsl:template>"
    )
    assert (
        _error_code(
            lambda: _transform(
                body,
                source=b'<root xmlns:v="urn:example:validation" v:status="ok"/>',
            )
        )
        == "XTTE1535"
    )


def test_namespace_sensitive_copy_without_namespaces_is_xtte0950() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:copy-of select="/v:qnameDoc/@ref" validation="preserve" copy-namespaces="no"/>'
        "</xsl:template>"
    )
    assert (
        _error_code(
            lambda: _transform(
                body,
                source=(
                    b'<v:qnameDoc xmlns:v="urn:example:validation" xmlns:q="urn:q" ref="q:value"/>'
                ),
                processor_options={"validation": "strict"},
            )
        )
        == "XTTE0950"
    )


def test_xsl_copy_inherit_namespaces_controls_constructor_children() -> None:
    source = (
        b'<v:Order xmlns:v="urn:example:validation" xmlns:extra="urn:extra">'
        b'<v:line code="1" note="n"><v:value>1.25</v:value></v:line></v:Order>'
    )
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy select="/v:Order" inherit-namespaces="no" validation="preserve">'
        '<xsl:element name="plain"><xsl:text>x</xsl:text></xsl:element>'
        "</xsl:copy>"
        "</xsl:variable>"
        '<xsl:sequence select="$c/v:Order"/>'
        "</xsl:template>"
    )
    plain = _transform(body, source=source, delivery="raw").principal[0].children[0]
    assert "extra" not in plain.in_scope_namespaces

    inherited = body.replace('inherit-namespaces="no"', 'inherit-namespaces="yes"')
    plain = _transform(inherited, source=source, delivery="raw").principal[0].children[0]
    assert plain.in_scope_namespaces["extra"] == "urn:extra"


def test_concurrent_schema_aware_validation_is_invocation_local() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:Order" type="v:Order">'
        '<xsl:attribute name="id">X</xsl:attribute>'
        '<xsl:element name="v:line" type="v:Line">'
        '<xsl:attribute name="code">1</xsl:attribute>'
        '<xsl:attribute name="note">n</xsl:attribute>'
        '<xsl:element name="v:value"><xsl:text>1.25</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="$o/v:Order"/>'
        '<xsl:sequence select="sum($o//v:value)"/>'
        "</xsl:template>"
    )
    compiled = _compile(body)
    failures: list[tuple[int, int]] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait()
            for iteration in range(20):
                result = compiled.transform(initial_template="main", delivery="raw")
                root = result.principal[0]
                valid = (
                    root.type_name == f"{{{VAL}}}Order" and result.principal[1].lexical == "1.25"
                )
                for line in root.children:
                    if line.kind is not NodeKind.ELEMENT:
                        continue
                    valid = valid and line.type_name == f"{{{VAL}}}Line"
                    attributes = {attribute.name: attribute for attribute in line.attributes}
                    valid = valid and attributes["code"].type_name == f"{{{XSD_NS}}}int"
                    valid = valid and attributes["note"].type_name == f"{{{XSD_NS}}}string"
                    for value in line.children:
                        if value.kind is NodeKind.ELEMENT:
                            valid = valid and (value.type_name == f"{{{XSD_NS}}}decimal")
                if not valid:
                    failures.append((threading.get_ident(), iteration))
        except BaseException:
            barrier.abort()
            raise

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(8)]
        for future in futures:
            future.result()

    assert failures == []


def test_lax_validation_leaves_undeclared_attributes_untyped() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:undeclared" validation="lax">'
        '<xsl:attribute name="plain">x</xsl:attribute>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="$o/*/@plain instance of attribute(*, xs:untypedAtomic)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body)) == ("true",)


def test_simple_type_rejects_nilled_occurrence() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:element name="v:amount" type="v:Amount">'
        f'<xsl:attribute name="xsi:nil" namespace="{XSI_NS}">true</xsl:attribute>'
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(body)) == "XTTE1540"


def test_namespace_sensitive_detection_uses_typed_values_and_false_path() -> None:
    from pyxsd import xsd_data_types as xsd
    from pyxsd.xdm.annotations import OccurrenceAnnotation
    from pyxsd.xdm.validation import namespace_sensitive

    document = XdmDocument.parse(b"<root/>")
    node = document.root_element
    assert not namespace_sensitive(node)
    node._apply_occurrence_annotation(
        OccurrenceAnnotation(
            type_name=f"{{{XSD_NS}}}string",
            validation_attempted="full",
            typed_values=(
                XdmAtomic.string("first"),
                XdmAtomic.typed(xsd.QName("x"), xsd.QName),
            ),
        )
    )
    assert namespace_sensitive(node)


def test_present_empty_and_absent_optional_defaulted_elements() -> None:
    present = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:defaulted" validation="strict">'
        '<xsl:element name="v:count"/>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="count($o/v:defaulted/v:count)"/>'
        '<xsl:sequence select="data($o/v:defaulted/v:count)"/>'
        '<xsl:sequence select="string($o/v:defaulted/v:count)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(present)) == ("1", "7", "")

    absent = (
        '<xsl:template name="main">'
        '<xsl:variable name="o"><xsl:element name="v:defaulted" validation="strict"/></xsl:variable>'
        '<xsl:sequence select="count($o/v:defaulted/v:count)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(absent)) == ("0",)


def test_absent_defaulted_element_is_not_inserted_into_choice() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:choiceDefaulted" validation="strict">'
        '<xsl:element name="v:b"><xsl:text>2</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="count($o/v:choiceDefaulted/*)"/>'
        '<xsl:sequence select="name($o/v:choiceDefaulted/*[1])"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body)) == ("1", "v:b")


def test_absent_defaulted_element_does_not_break_sequence_order() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:seqDefaulted" validation="strict">'
        '<xsl:element name="v:b"><xsl:text>2</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="count($o/v:seqDefaulted/*)"/>'
        '<xsl:sequence select="name($o/v:seqDefaulted/*[1])"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body)) == ("1", "v:b")


def test_nilled_element_does_not_receive_defaulted_children() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:nilledDefaulted" validation="strict">'
        f'<xsl:attribute name="xsi:nil" namespace="{XSI_NS}">true</xsl:attribute>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="nilled($o/*)"/>'
        '<xsl:sequence select="count($o/v:nilledDefaulted/node())"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body)) == ("true", "0")


def test_required_defaulted_element_remains_invalid() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:element name="v:requiredDefaulted" validation="strict"/>'
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(body)) == "XTTE1510"


def test_initial_mode_checks_only_nodes_actually_processed() -> None:
    direct_document = (
        '<xsl:mode name="m" typed="yes"/><xsl:template match="/" mode="m"><out/></xsl:template>'
    )
    compiled = _compile(direct_document)
    result = compiled.transform(source=b"<in/>", initial_mode="m", delivery="raw")
    assert result.principal[0].local_name == "out"

    recursive = (
        '<xsl:mode name="m" typed="yes"/><xsl:template match="*" mode="m"><out/></xsl:template>'
    )
    recursive_compiled = _compile(recursive)
    assert (
        _error_code(
            lambda: recursive_compiled.transform(
                source=b"<in/>",
                initial_mode="m",
                delivery="raw",
            )
        )
        == "XTTE3100"
    )


def test_simple_types_accept_schema_location_attributes() -> None:
    for local in ("schemaLocation", "noNamespaceSchemaLocation"):
        body = (
            '<xsl:template name="main">'
            '<xsl:variable name="o">'
            '<xsl:element name="v:amount" type="v:Amount">'
            f'<xsl:attribute name="xsi:{local}" namespace="{XSI_NS}">urn:example schema.xsd</xsl:attribute>'
            "12.5"
            "</xsl:element>"
            "</xsl:variable>"
            '<xsl:sequence select="$o/* instance of element(*, v:Amount)"/>'
            '<xsl:sequence select="data($o/*)"/>'
            "</xsl:template>"
        )
        assert _values(_transform(body)) == ("true", "12.5")

    untyped = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:x" type="xs:untypedAtomic">'
        f'<xsl:attribute name="xsi:schemaLocation" namespace="{XSI_NS}">urn:x x.xsd</xsl:attribute>'
        "text"
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="string($o/*)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(untyped)) == ("text",)


def test_simple_types_reject_unknown_xsi_and_untyped_atomic_attributes() -> None:
    unknown_xsi = (
        '<xsl:template name="main">'
        '<xsl:element name="v:amount" type="v:Amount">'
        f'<xsl:attribute name="xsi:foo" namespace="{XSI_NS}">x</xsl:attribute>12.5'
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(unknown_xsi)) == "XTTE1540"

    untyped_extra = (
        '<xsl:template name="main">'
        '<xsl:element name="v:x" type="xs:untypedAtomic">'
        '<xsl:attribute name="extra">x</xsl:attribute>text'
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(untyped_extra)) == "XTTE1540"

    untyped_nil = (
        '<xsl:template name="main">'
        '<xsl:element name="v:x" type="xs:untypedAtomic">'
        f'<xsl:attribute name="xsi:nil" namespace="{XSI_NS}">true</xsl:attribute>'
        "</xsl:element>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(untyped_nil)) == "XTTE1540"

    nillable_simple = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="v:nilledAmount" type="v:Amount">'
        f'<xsl:attribute name="xsi:nil" namespace="{XSI_NS}">true</xsl:attribute>'
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="nilled($o/*)"/>'
        "</xsl:template>"
    )
    assert _values(_transform(nillable_simple)) == ("true",)


def test_named_type_document_constraints_without_a_global_declaration() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:document type="v:IdDoc">'
        '<xsl:element name="v:other">'
        '<xsl:element name="v:ref"><xsl:text>missing</xsl:text></xsl:element>'
        '<xsl:element name="v:id"><xsl:text>one</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:document>"
        "</xsl:template>"
    )
    assert _error_code(lambda: _transform(body)) == "XTTE1555"


def test_document_copy_without_namespaces_checks_namespace_sensitive_content() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:copy select="/" validation="preserve" copy-namespaces="no">'
        '<xsl:copy-of select="/v:qnameDoc" validation="preserve"/>'
        "</xsl:copy>"
        "</xsl:template>"
    )
    assert (
        _error_code(
            lambda: _transform(
                body,
                source=(
                    b'<v:qnameDoc xmlns:v="urn:example:validation" xmlns:q="urn:q" ref="q:value"/>'
                ),
                processor_options={"validation": "strict"},
            )
        )
        == "XTTE0950"
    )


def test_non_namespace_sensitive_document_copy_without_namespaces_succeeds() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="c">'
        '<xsl:copy select="/" validation="preserve" copy-namespaces="no"/>'
        "</xsl:variable>"
        '<xsl:sequence select="count($c/node())"/>'
        "</xsl:template>"
    )
    assert _values(_transform(body, source=b"<root><child/></root>", delivery="raw")) == ("0",)
