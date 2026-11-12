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

import pytest

from pyxsd.schema import Schema
from pyxsd.xdm.document import DocumentValidationError, parse_typed_document
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xdm.validation import (
    ConstructedValidationError,
    ValidationRequest,
    validate_document,
    validate_occurrence,
)
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
  <xs:element name="implicit"/>
  <xs:element name="n" type="xs:int" default="7"/>
  <xs:element name="m" type="xs:int" fixed="7"/>
  <xs:element name="f" type="xs:string" fixed="bar"/>
  <xs:element name="d" type="xs:string" default="foo"/>
  <xs:element name="x" type="xs:int" fixed="7"/>
  <xs:element name="anyN" type="xs:anyType" nillable="true"/>
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

IMPLICIT_SOURCE = f'<c:implicit xmlns:c="{CONSISTENCY}"><c:child/></c:implicit>'.encode()

ANY_NILLED_SOURCE = (
    f'<c:anyN xmlns:c="{CONSISTENCY}" xmlns:xsi="{XSI_NS}" xsi:nil="true"/>'
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
        getattr(annotation, "validity", None),
        getattr(annotation, "validation_attempted", None),
        getattr(annotation, "actual_type", None),
        tuple(
            (value.type_name, value.lexical) for value in getattr(annotation, "typed_values", ())
        ),
        getattr(annotation, "default_value", None),
        getattr(annotation, "fixed_value", None),
        getattr(annotation, "value_source", None),
        getattr(annotation, "id_kind", None),
        getattr(annotation, "governing_declaration", None),
        getattr(annotation, "local_declaration", None),
        getattr(annotation, "anonymous_type", None),
        getattr(annotation, "repeated", None),
        getattr(annotation, "wildcard", None),
        getattr(annotation, "substitution_group_head", None),
        getattr(annotation, "xsi_type", None),
        getattr(annotation, "xsi_type_lexical", None),
        getattr(annotation, "nil_lexical", None),
        getattr(annotation, "nil_declared", None),
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
    host._elementParents[10**9] = "parent-sentinel"
    host._elementTypes[10**9 + 1] = "type-sentinel"
    return schema, pool, host._elementParents, host._elementTypes


def _assert_host_unchanged(schema: Schema, parents: Any, types: Any) -> None:
    assert schema._host._elementParents is parents
    assert schema._host._elementTypes is types
    assert parents[10**9] == "parent-sentinel"
    assert types[10**9 + 1] == "type-sentinel"


def test_validation_preserves_the_compiled_host() -> None:
    schema, pool, parents, types = _primed_host()

    parse_typed_document(COMPLEX_SOURCE, schema_pool=pool, type_name=INVOICE_TYPE)

    _assert_host_unchanged(schema, parents, types)


def test_nilled_validation_preserves_the_compiled_host() -> None:
    schema, pool, parents, types = _primed_host()

    parse_typed_document(NILLED_SOURCE, schema_pool=pool, type_name=PRICE_TYPE)

    _assert_host_unchanged(schema, parents, types)


def test_declaration_validation_preserves_the_compiled_host() -> None:
    schema, pool, parents, types = _primed_host()

    parse_typed_document(COMPLEX_SOURCE, schema_pool=pool, validation="strict")

    _assert_host_unchanged(schema, parents, types)


def test_requested_type_with_strict_validation_checks_document_ids() -> None:
    _, pool = _schema_and_pool()

    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(
            IDDOC_SOURCE,
            schema_pool=pool,
            validation="strict",
            type_name=IDDOC_TYPE,
        )
    assert caught.value.reason == "type"

    accepted = parse_typed_document(
        IDDOC_SOURCE,
        schema_pool=pool,
        validation="strip",
        type_name=IDDOC_TYPE,
    )
    assert accepted.root_element.annotation.validity == "valid"


def test_declaration_identity_constraints_agree() -> None:
    _, pool = _schema_and_pool()

    with pytest.raises(DocumentValidationError) as source_error:
        parse_typed_document(IDDOC_DECL_SOURCE, schema_pool=pool, validation="strict")
    assert source_error.value.reason == "invalid"

    with pytest.raises(ConstructedValidationError) as constructed_error:
        validate_document(
            XdmDocument.parse(IDDOC_DECL_SOURCE),
            ValidationRequest("strict"),
            schema_pool=pool,
        )
    assert constructed_error.value.reason == "document-constraint"


def test_source_and_constructed_declaration_annotations_agree() -> None:
    _, pool = _schema_and_pool()

    source = parse_typed_document(COMPLEX_SOURCE, schema_pool=pool, validation="strict")
    constructed = XdmDocument.parse(COMPLEX_SOURCE)
    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strict"),
        schema_pool=pool,
    )
    _assert_annotations_agree(source.root_element, constructed.root_element)
    kind = next(
        attribute for attribute in source.root_element.attributes if attribute.name == "kind"
    )
    assert kind.string_value == "std"
    assert kind.annotation.value_source == "default"

    nilled = parse_typed_document(NILLED_SOURCE, schema_pool=pool, validation="strict")
    constructed_nilled = XdmDocument.parse(NILLED_SOURCE)
    validate_occurrence(
        constructed_nilled.root_element,
        ValidationRequest("strict"),
        schema_pool=pool,
    )
    _assert_annotations_agree(nilled.root_element, constructed_nilled.root_element)
    assert nilled.root_element.nilled is True
    assert nilled.root_element.annotation.typed_values == ()
    nil_attribute = nilled.root_element.attribute(f"{{{XSI_NS}}}nil")
    constructed_nil_attribute = constructed_nilled.root_element.attribute(f"{{{XSI_NS}}}nil")
    assert nil_attribute is not None
    assert constructed_nil_attribute is not None
    assert _signature(nil_attribute) == _signature(constructed_nil_attribute)


@pytest.mark.parametrize(
    ("local", "default", "fixed", "value_source", "typed_value"),
    [
        ("n", "7", None, "default", "7"),
        ("m", None, "7", "fixed", "7"),
        ("f", None, "bar", "fixed", "bar"),
        ("d", "foo", None, "default", "foo"),
    ],
)
def test_simple_declaration_defaults_and_fixed_metadata(
    local: str,
    default: str | None,
    fixed: str | None,
    value_source: str,
    typed_value: str,
) -> None:
    _, pool = _schema_and_pool()
    source_bytes = f'<c:{local} xmlns:c="{CONSISTENCY}"/>'.encode()

    source = parse_typed_document(source_bytes, schema_pool=pool, validation="strict")
    constructed = XdmDocument.parse(source_bytes)
    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strict"),
        schema_pool=pool,
    )

    _assert_annotations_agree(source.root_element, constructed.root_element)
    annotation = source.root_element.annotation
    assert annotation.default_value == default
    assert annotation.fixed_value == fixed
    assert annotation.value_source == value_source
    assert annotation.governing_declaration is not None
    assert tuple(value.lexical for value in annotation.typed_values) == (typed_value,)


def test_simple_declaration_present_fixed_value_metadata() -> None:
    _, pool = _schema_and_pool()
    source_bytes = f'<c:f xmlns:c="{CONSISTENCY}">bar</c:f>'.encode()

    source = parse_typed_document(source_bytes, schema_pool=pool, validation="strict")
    constructed = XdmDocument.parse(source_bytes)
    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strict"),
        schema_pool=pool,
    )

    _assert_annotations_agree(source.root_element, constructed.root_element)
    annotation = source.root_element.annotation
    assert annotation.fixed_value == "bar"
    assert annotation.value_source == "instance"
    assert annotation.governing_declaration is not None
    assert tuple(value.lexical for value in annotation.typed_values) == ("bar",)


def test_non_governing_declaration_facets_are_ignored_for_requested_types() -> None:
    _, pool = _schema_and_pool()
    decimal_type = f"{{{XSD_NS}}}decimal"
    string_type = f"{{{XSD_NS}}}string"

    present = f'<c:x xmlns:c="{CONSISTENCY}">12</c:x>'.encode()
    source = parse_typed_document(present, schema_pool=pool, type_name=decimal_type)
    constructed = XdmDocument.parse(present)
    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strip", decimal_type),
        schema_pool=pool,
    )
    _assert_annotations_agree(source.root_element, constructed.root_element)
    annotation = source.root_element.annotation
    assert annotation.default_value is None
    assert annotation.fixed_value is None
    assert annotation.value_source is None
    assert annotation.governing_declaration is None
    assert tuple(value.lexical for value in annotation.typed_values) == ("12",)

    empty_fixed = f'<c:x xmlns:c="{CONSISTENCY}"/>'.encode()
    with pytest.raises(DocumentValidationError) as source_error:
        parse_typed_document(empty_fixed, schema_pool=pool, type_name=decimal_type)
    assert source_error.value.reason == "type"
    with pytest.raises(ConstructedValidationError) as constructed_error:
        validate_occurrence(
            XdmDocument.parse(empty_fixed).root_element,
            ValidationRequest("strip", decimal_type),
            schema_pool=pool,
        )
    assert constructed_error.value.reason == "type"

    empty_default = f'<c:d xmlns:c="{CONSISTENCY}"/>'.encode()
    source = parse_typed_document(empty_default, schema_pool=pool, type_name=string_type)
    constructed = XdmDocument.parse(empty_default)
    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strip", string_type),
        schema_pool=pool,
    )
    _assert_annotations_agree(source.root_element, constructed.root_element)
    annotation = source.root_element.annotation
    assert annotation.default_value is None
    assert annotation.fixed_value is None
    assert annotation.value_source is None
    assert tuple(value.lexical for value in annotation.typed_values) == ("",)


def test_governing_declaration_fixed_facets_still_apply() -> None:
    _, pool = _schema_and_pool()
    valid = f'<c:x xmlns:c="{CONSISTENCY}">7</c:x>'.encode()
    invalid = f'<c:x xmlns:c="{CONSISTENCY}">12</c:x>'.encode()

    source = parse_typed_document(valid, schema_pool=pool, validation="strict")
    constructed = XdmDocument.parse(valid)
    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strict"),
        schema_pool=pool,
    )
    _assert_annotations_agree(source.root_element, constructed.root_element)
    annotation = source.root_element.annotation
    assert annotation.fixed_value == "7"
    assert annotation.governing_declaration is not None
    assert tuple(value.lexical for value in annotation.typed_values) == ("7",)

    with pytest.raises(DocumentValidationError) as source_error:
        parse_typed_document(invalid, schema_pool=pool, validation="strict")
    assert source_error.value.reason == "invalid"
    with pytest.raises(ConstructedValidationError) as constructed_error:
        validate_occurrence(
            XdmDocument.parse(invalid).root_element,
            ValidationRequest("strict"),
            schema_pool=pool,
        )
    assert constructed_error.value.reason == "invalid"


def test_simple_declaration_fixed_value_mismatch_is_rejected() -> None:
    _, pool = _schema_and_pool()
    source_bytes = f'<c:f xmlns:c="{CONSISTENCY}">wrong</c:f>'.encode()

    with pytest.raises(DocumentValidationError) as source_error:
        parse_typed_document(source_bytes, schema_pool=pool, validation="strict")
    assert source_error.value.reason == "invalid"

    with pytest.raises(ConstructedValidationError) as constructed_error:
        validate_occurrence(
            XdmDocument.parse(source_bytes).root_element,
            ValidationRequest("strict"),
            schema_pool=pool,
        )
    assert constructed_error.value.reason == "invalid"


def test_nilled_any_type_declarations_keep_nilled_state_and_metadata() -> None:
    _, pool = _schema_and_pool()
    any_type = f"{{{XSD_NS}}}anyType"

    source = parse_typed_document(ANY_NILLED_SOURCE, schema_pool=pool, validation="strict")
    constructed = XdmDocument.parse(ANY_NILLED_SOURCE)
    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strict"),
        schema_pool=pool,
    )

    _assert_annotations_agree(source.root_element, constructed.root_element)
    for node in (source.root_element, constructed.root_element):
        annotation = node.annotation
        assert annotation.type_name == any_type
        assert annotation.nilled is True
        assert annotation.typed_values == ()
        assert annotation.governing_declaration is not None


def test_declaration_implicit_any_type_uses_any_type_assessment() -> None:
    _, pool = _schema_and_pool()
    any_type = f"{{{XSD_NS}}}anyType"

    source = parse_typed_document(IMPLICIT_SOURCE, schema_pool=pool, validation="strict")
    constructed = XdmDocument.parse(IMPLICIT_SOURCE)
    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strict"),
        schema_pool=pool,
    )

    assert source.root_element.type_name == any_type
    assert constructed.root_element.type_name == any_type
    _assert_annotations_agree(source.root_element, constructed.root_element)


def test_invalid_declaration_xsi_type_is_rejected_consistently() -> None:
    _, pool = _schema_and_pool()
    invalid = (
        f'<c:Invoice xmlns:c="{CONSISTENCY}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="c:Missing"><c:line code="1"><c:value>1</c:value></c:line>'
        "</c:Invoice>"
    ).encode()

    with pytest.raises(DocumentValidationError) as source_error:
        parse_typed_document(invalid, schema_pool=pool, validation="strict")
    assert source_error.value.reason == "invalid"

    with pytest.raises(ConstructedValidationError) as constructed_error:
        validate_occurrence(
            XdmDocument.parse(invalid).root_element,
            ValidationRequest("strict"),
            schema_pool=pool,
        )
    assert constructed_error.value.reason == "invalid"


def test_invalid_xsi_nil_lexical_is_rejected_for_requested_types() -> None:
    _, pool = _schema_and_pool()
    invalid = (
        f'<c:amount xmlns:c="{CONSISTENCY}" xmlns:xsi="{XSI_NS}" xsi:nil="yes">12.50</c:amount>'
    ).encode()

    with pytest.raises(DocumentValidationError) as source_error:
        parse_typed_document(invalid, schema_pool=pool, type_name=AMOUNT_TYPE)
    assert source_error.value.reason == "type"

    with pytest.raises(ConstructedValidationError) as constructed_error:
        validate_occurrence(
            XdmDocument.parse(invalid).root_element,
            ValidationRequest("strip", AMOUNT_TYPE),
            schema_pool=pool,
        )
    assert constructed_error.value.reason == "type"


def test_invalid_xsi_nil_lexical_is_rejected_for_declarations() -> None:
    _, pool = _schema_and_pool()
    invalid = (
        f'<c:amount xmlns:c="{CONSISTENCY}" xmlns:xsi="{XSI_NS}" xsi:nil="yes">12.50</c:amount>'
    ).encode()

    with pytest.raises(DocumentValidationError) as source_error:
        parse_typed_document(invalid, schema_pool=pool, validation="strict")
    assert source_error.value.reason == "invalid"

    with pytest.raises(ConstructedValidationError) as constructed_error:
        validate_occurrence(
            XdmDocument.parse(invalid).root_element,
            ValidationRequest("strict"),
            schema_pool=pool,
        )
    assert constructed_error.value.reason == "invalid"


def test_untyped_atomic_requested_type_resolves_xsi_type() -> None:
    _, pool = _schema_and_pool()
    untyped_type = f"{{{XSD_NS}}}untypedAtomic"
    same = (
        f'<c:amount xmlns:c="{CONSISTENCY}" xmlns:xs="{XSD_NS}" '
        f'xmlns:xsi="{XSI_NS}" xsi:type="xs:untypedAtomic">12.50</c:amount>'
    ).encode()

    source = parse_typed_document(same, schema_pool=pool, type_name=untyped_type)
    constructed = XdmDocument.parse(same)
    validate_occurrence(
        constructed.root_element,
        ValidationRequest("strip", untyped_type),
        schema_pool=pool,
    )
    assert source.root_element.type_name == untyped_type
    assert source.root_element.annotation.xsi_type == untyped_type
    _assert_annotations_agree(source.root_element, constructed.root_element)

    override = (
        f'<c:amount xmlns:c="{CONSISTENCY}" xmlns:xs="{XSD_NS}" '
        f'xmlns:xsi="{XSI_NS}" xsi:type="xs:decimal">12.50</c:amount>'
    ).encode()
    with pytest.raises(DocumentValidationError) as source_error:
        parse_typed_document(override, schema_pool=pool, type_name=untyped_type)
    assert source_error.value.reason == "type"
    with pytest.raises(ConstructedValidationError) as constructed_error:
        validate_occurrence(
            XdmDocument.parse(override).root_element,
            ValidationRequest("strip", untyped_type),
            schema_pool=pool,
        )
    assert constructed_error.value.reason == "type"


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
