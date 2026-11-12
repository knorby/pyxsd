"""Source and constructed occurrence validation share one assessment path.

A compiled stylesheet's schema pool is shared by every invocation.  Requested
source types, nilled source occurrences, and constructed nodes must therefore
assess against invocation-private schema/host/context views, apply the same
annotations to the original XDM occurrences, and stay stable when eight
threads validate through one compiled artifact.
"""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pyxsd.schema import Schema
from pyxsd.xdm.document import parse_typed_document
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xdm.validation import ValidationRequest, validate_occurrence
from pyxsd.xslt import XsltProcessor

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
CONSISTENCY = "urn:example:consistency"

INVOICE_TYPE = f"{{{CONSISTENCY}}}Invoice"
LINE_TYPE = f"{{{CONSISTENCY}}}Line"
PRICE_TYPE = f"{{{CONSISTENCY}}}Price"
IDDOC_TYPE = f"{{{CONSISTENCY}}}IdDoc"
AMOUNT_TYPE = f"{{{XSD_NS}}}decimal"

SCHEMA = f"""
<xs:schema xmlns:xs="{XSD_NS}" xmlns:c="{CONSISTENCY}"
    targetNamespace="{CONSISTENCY}" elementFormDefault="qualified">
  <xs:complexType name="Line">
    <xs:sequence>
      <xs:element name="value" type="xs:decimal"/>
    </xs:sequence>
    <xs:attribute name="code" type="xs:int" use="required"/>
    <xs:attribute name="note" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="Invoice">
    <xs:sequence>
      <xs:element name="line" type="c:Line" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:ID"/>
    <xs:attribute name="kind" type="xs:string" default="std"/>
  </xs:complexType>
  <xs:complexType name="Price">
    <xs:simpleContent>
      <xs:extension base="xs:decimal">
        <xs:attribute name="currency" type="xs:string" default="EUR"/>
      </xs:extension>
    </xs:simpleContent>
  </xs:complexType>
  <xs:complexType name="IdDoc">
    <xs:sequence>
      <xs:element name="ref" type="xs:IDREF"/>
      <xs:element name="id" type="xs:ID"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="Invoice" type="c:Invoice"/>
  <xs:element name="price" type="c:Price" nillable="true"/>
  <xs:element name="amount" type="xs:decimal"/>
  <xs:element name="idDoc" type="c:IdDoc"/>
</xs:schema>
"""

COMPLEX_SOURCE = (
    f'<c:Invoice xmlns:c="{CONSISTENCY}" id="i1">'
    '<c:line code="1" note="n"><c:value>12.50</c:value></c:line>'
    "</c:Invoice>"
).encode()

NILLED_SOURCE = (
    f'<c:price xmlns:c="{CONSISTENCY}" xmlns:xsi="{XSI_NS}" xsi:nil="true" currency="USD"/>'
).encode()

SIMPLE_CONTENT_SOURCE = (
    f'<c:price xmlns:c="{CONSISTENCY}" currency="USD">12.50</c:price>'
).encode()

SIMPLE_SOURCE = f'<c:amount xmlns:c="{CONSISTENCY}">12.50</c:amount>'.encode()

IDDOC_SOURCE = (
    f'<c:untyped xmlns:c="{CONSISTENCY}"><c:ref>missing</c:ref><c:id>one</c:id></c:untyped>'
).encode()

IDDOC_DECL_SOURCE = (
    f'<c:idDoc xmlns:c="{CONSISTENCY}"><c:ref>missing</c:ref><c:id>one</c:id></c:idDoc>'
).encode()


def _schema_and_pool() -> tuple[Schema, SchemaPool]:
    schema = Schema.compile(io.BytesIO(SCHEMA.encode()))
    assert not schema.report.has_errors, schema.report
    return schema, SchemaPool.from_schema(schema)


def _stylesheet(body: str) -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:c="{CONSISTENCY}" version="3.0">'
        f"<xsl:import-schema>{SCHEMA}</xsl:import-schema>"
        f"{body}</xsl:stylesheet>"
    ).encode()


def _compiled_stylesheet():
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="o">'
        '<xsl:element name="c:Invoice" type="c:Invoice">'
        '<xsl:attribute name="id">i1</xsl:attribute>'
        '<xsl:element name="c:line" type="c:Line">'
        '<xsl:attribute name="code">1</xsl:attribute>'
        '<xsl:attribute name="note">n</xsl:attribute>'
        '<xsl:element name="c:value"><xsl:text>12.50</xsl:text></xsl:element>'
        "</xsl:element>"
        "</xsl:element>"
        "</xsl:variable>"
        '<xsl:sequence select="$o/c:Invoice"/>'
        "</xsl:template>"
    )
    return XsltProcessor().compile(io.BytesIO(_stylesheet(body)))


def _annotation(node: XdmNode):
    return node.annotation


def _signature(node: XdmNode):
    annotation = _annotation(node)
    return (
        node.kind,
        node.name,
        annotation.type_name,
        annotation.nilled,
        annotation.validity,
        annotation.validation_attempted,
        annotation.actual_type,
        tuple((value.type_name, value.lexical) for value in annotation.typed_values),
        annotation.default_value,
        annotation.fixed_value,
        annotation.value_source,
        annotation.id_kind,
        annotation.governing_declaration,
        annotation.local_declaration,
        annotation.anonymous_type,
        annotation.repeated,
        annotation.wildcard,
        annotation.substitution_group_head,
        annotation.xsi_type,
        annotation.xsi_type_lexical,
        annotation.nil_lexical,
        annotation.nil_declared,
    )


def _assert_annotations_agree(source: XdmNode, constructed: XdmNode) -> None:
    assert _signature(source) == _signature(constructed)
    source_attributes = {attribute.name: attribute for attribute in source.attributes}
    constructed_attributes = {attribute.name: attribute for attribute in constructed.attributes}
    assert source_attributes.keys() == constructed_attributes.keys()
    for name, source_attribute in source_attributes.items():
        assert _signature(source_attribute) == _signature(constructed_attributes[name])
    source_children = [child for child in source.children if child.kind is NodeKind.ELEMENT]
    constructed_children = [
        child for child in constructed.children if child.kind is NodeKind.ELEMENT
    ]
    assert len(source_children) == len(constructed_children)
    for source_child, constructed_child in zip(source_children, constructed_children, strict=True):
        _assert_annotations_agree(source_child, constructed_child)


def _primed_host() -> tuple[Schema, SchemaPool, Any, Any]:
    schema, pool = _schema_and_pool()
    schema.parse(io.BytesIO(COMPLEX_SOURCE))
    host = schema._host
    return schema, pool, host._elementParents, host._elementTypes


def test_validation_preserves_the_compiled_host() -> None:
    schema, pool, parents, types = _primed_host()

    parse_typed_document(COMPLEX_SOURCE, schema_pool=pool, type_name=INVOICE_TYPE)

    assert schema._host._elementParents is parents
    assert schema._host._elementTypes is types


def test_nilled_validation_preserves_the_compiled_host() -> None:
    schema, pool, parents, types = _primed_host()

    parse_typed_document(NILLED_SOURCE, schema_pool=pool, type_name=PRICE_TYPE)

    assert schema._host._elementParents is parents
    assert schema._host._elementTypes is types


def test_declaration_validation_preserves_the_compiled_host() -> None:
    schema, pool, parents, types = _primed_host()

    parse_typed_document(COMPLEX_SOURCE, schema_pool=pool, validation="strict")

    assert schema._host._elementParents is parents
    assert schema._host._elementTypes is types


def test_ignored_document_ids_do_not_invalidate_occurrences() -> None:
    _, pool = _schema_and_pool()

    document = parse_typed_document(IDDOC_SOURCE, schema_pool=pool, type_name=IDDOC_TYPE)

    assert document.root_element.annotation.validity == "valid"
    reference, identifier = [
        child for child in document.root_element.children if child.kind is NodeKind.ELEMENT
    ]
    assert reference.annotation.id_kind == "IDREF"
    assert reference.annotation.validity == "valid"
    assert identifier.annotation.id_kind == "ID"
    assert identifier.annotation.validity == "valid"


def test_ignored_fragment_ids_do_not_invalidate_constructed_declarations() -> None:
    _, pool = _schema_and_pool()
    constructed = XdmDocument.parse(IDDOC_DECL_SOURCE)

    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strict"),
        schema_pool=pool,
    )

    assert constructed.root_element.annotation.validity == "valid"
    reference, identifier = [
        child for child in constructed.root_element.children if child.kind is NodeKind.ELEMENT
    ]
    assert reference.annotation.validity == "valid"
    assert identifier.annotation.validity == "valid"


def test_strip_preserves_id_classification() -> None:
    from pyxsd.xdm.document import strip_type_annotations

    _, pool = _schema_and_pool()
    document = parse_typed_document(COMPLEX_SOURCE, schema_pool=pool, type_name=INVOICE_TYPE)
    identifier = document.root_element.attribute("id")
    assert identifier is not None
    assert identifier.annotation.id_kind == "ID"

    strip_type_annotations(document)

    assert identifier.type_name == f"{{{XSD_NS}}}untypedAtomic"
    assert identifier.annotation.id_kind == "ID"


def test_source_and_constructed_annotations_agree() -> None:
    _, pool = _schema_and_pool()

    source = parse_typed_document(COMPLEX_SOURCE, schema_pool=pool, type_name=INVOICE_TYPE)
    constructed = XdmDocument.parse(COMPLEX_SOURCE)
    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strip", INVOICE_TYPE),
        schema_pool=pool,
    )
    _assert_annotations_agree(source.root_element, constructed.root_element)

    simple_content = parse_typed_document(
        SIMPLE_CONTENT_SOURCE, schema_pool=pool, type_name=PRICE_TYPE
    )
    constructed_content = XdmDocument.parse(SIMPLE_CONTENT_SOURCE)
    validate_occurrence(
        constructed_content.root_element,
        ValidationRequest("strip", PRICE_TYPE),
        schema_pool=pool,
    )
    _assert_annotations_agree(simple_content.root_element, constructed_content.root_element)

    nilled = parse_typed_document(NILLED_SOURCE, schema_pool=pool, type_name=PRICE_TYPE)
    constructed_nilled = XdmDocument.parse(NILLED_SOURCE)
    validate_occurrence(
        constructed_nilled.root_element,
        ValidationRequest("strip", PRICE_TYPE),
        schema_pool=pool,
    )
    _assert_annotations_agree(nilled.root_element, constructed_nilled.root_element)

    simple = parse_typed_document(SIMPLE_SOURCE, schema_pool=pool, type_name=AMOUNT_TYPE)
    constructed_simple = XdmDocument.parse(SIMPLE_SOURCE)
    validate_occurrence(
        constructed_simple.root_element,
        ValidationRequest("strip", AMOUNT_TYPE),
        schema_pool=pool,
    )
    _assert_annotations_agree(simple.root_element, constructed_simple.root_element)


def _check_requested_source(pool: SchemaPool) -> None:
    document = parse_typed_document(COMPLEX_SOURCE, schema_pool=pool, type_name=INVOICE_TYPE)
    root = document.root_element
    assert root.type_name == INVOICE_TYPE
    assert root.annotation.governing_declaration is not None
    root_attributes = {attribute.name: attribute for attribute in root.attributes}
    assert root_attributes["id"].type_name == f"{{{XSD_NS}}}ID"
    assert root_attributes["kind"].type_name == f"{{{XSD_NS}}}string"
    assert root_attributes["kind"].annotation.value_source == "default"
    line = next(child for child in root.children if child.kind is NodeKind.ELEMENT)
    assert line.type_name == LINE_TYPE
    attributes = {attribute.name: attribute for attribute in line.attributes}
    assert attributes["code"].type_name == f"{{{XSD_NS}}}int"
    assert attributes["note"].type_name == f"{{{XSD_NS}}}string"
    value = next(child for child in line.children if child.kind is NodeKind.ELEMENT)
    assert value.type_name == AMOUNT_TYPE


def _check_nilled_source(pool: SchemaPool) -> None:
    document = parse_typed_document(NILLED_SOURCE, schema_pool=pool, type_name=PRICE_TYPE)
    root = document.root_element
    assert root.type_name == PRICE_TYPE
    assert root.nilled is True
    assert root.annotation.typed_values == ()
    currency = root.attribute("currency")
    assert currency is not None
    assert currency.type_name == f"{{{XSD_NS}}}string"


def _check_constructed(compiled) -> None:
    root = compiled.transform(initial_template="main", delivery="raw").principal[0]
    assert root.type_name == INVOICE_TYPE
    root_attributes = {attribute.name: attribute for attribute in root.attributes}
    assert root_attributes["id"].type_name == f"{{{XSD_NS}}}ID"
    assert root_attributes["kind"].type_name == f"{{{XSD_NS}}}string"
    assert root_attributes["kind"].annotation.value_source == "default"
    line = next(child for child in root.children if child.kind is NodeKind.ELEMENT)
    assert line.type_name == LINE_TYPE
    attributes = {attribute.name: attribute for attribute in line.attributes}
    assert attributes["code"].type_name == f"{{{XSD_NS}}}int"
    assert attributes["note"].type_name == f"{{{XSD_NS}}}string"
    value = next(child for child in line.children if child.kind is NodeKind.ELEMENT)
    assert value.type_name == AMOUNT_TYPE


def test_concurrent_occurrence_validation() -> None:
    compiled = _compiled_stylesheet()
    pool = compiled.schema_pool
    assert pool is not None
    failures: list[tuple[int, int]] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        iteration = 0
        try:
            barrier.wait()
            for _ in range(10):
                _check_requested_source(pool)
                _check_nilled_source(pool)
                _check_constructed(compiled)
                iteration += 1
        except BaseException:
            failures.append((threading.get_ident(), iteration))
            barrier.abort()
            raise

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker) for _ in range(8)]
        for future in futures:
            future.result()

    assert failures == []
