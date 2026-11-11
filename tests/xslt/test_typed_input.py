"""Schema-aware XSLT source input: validation modes, annotations, and types.

A stylesheet that imports a schema feeds the same ``SchemaPool`` and
annotation adapters to its source documents, ``document()`` results, and
expression static contexts.  These tests assert public transformation
behavior: typed values, ``schema-element`` matching, ``xsi:type`` selection,
validation modes, annotation stripping, and the documented error arms.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from pyxsd.resource_policy import ResourceData
from pyxsd.schema import Schema
from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, XPathStaticContext
from pyxsd.xslt import XsltInvocationError, XsltProcessor
from pyxsd.xslt.errors import XsltError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
INVOICE = "urn:example:invoice"

EXPECTED_DECIMAL = XdmAtomic.decimal(Decimal("12.50"))

INVOICE_XSD = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}"
    xmlns:inv="{INVOICE}">
  <xs:simpleType name="Amount">
    <xs:restriction base="xs:decimal"/>
  </xs:simpleType>
  <xs:complexType name="InvoiceType">
    <xs:sequence>
      <xs:element name="amount" type="xs:decimal"/>
      <xs:element name="note" type="xs:string" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="SpecialInvoice">
    <xs:complexContent>
      <xs:extension base="inv:InvoiceType">
        <xs:sequence>
          <xs:element name="code" type="xs:string"/>
        </xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="Invoice" type="inv:InvoiceType"/>
  <xs:element name="amount" type="inv:Amount"/>
</xs:schema>"""

TYPED_INVOICE = (
    f'<inv:Invoice xmlns:inv="{INVOICE}"><amount>12.50</amount></inv:Invoice>'
).encode()
UNTYPED_INVOICE = b"<Invoice><amount>12.50</amount></Invoice>"


def _stylesheet(body: str, *, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:inv="{INVOICE}" version="3.0"{attributes}>'
        f"{body}</xsl:stylesheet>"
    ).encode()


def _import_schema(schema: str = INVOICE_XSD) -> str:
    return f"<xsl:import-schema>{schema}</xsl:import-schema>"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def compile_stylesheet(
    path: str | Path,
    *,
    schemas: list[Path] | tuple[Path, ...] = (),
    validation: str = "strict",
    xsd_version: str = "1.1",
    resource_policy: Any = None,
):
    processor = XsltProcessor(
        validation=validation,
        xsd_version=xsd_version,
        resource_policy=resource_policy,
    )
    pool = SchemaPool.from_imports(list(schemas), xsd_version=xsd_version) if schemas else None
    return processor.compile(path, schema=pool)


def evaluate(expression: str, compiled: Any, source: Any = None):
    from pyxsd.xdm.document import parse_typed_document

    engine = XPathEngine("3.1")
    static = XPathStaticContext(
        namespaces={"inv": INVOICE, "xs": XSD_NS},
        schema=compiled.schema_pool,
    )
    compiled_expression = engine.compile(expression, static)
    if source is None:
        return engine.evaluate(compiled_expression, ExpressionEnvironment())
    document = parse_typed_document(
        source,
        schema_pool=compiled.schema_pool,
        validation="strict",
    )
    return engine.evaluate(
        compiled_expression,
        ExpressionEnvironment(),
        focus=(document.root_element,),
    )


def _typed_invoice_stylesheet() -> bytes:
    return _stylesheet(
        _import_schema() + '<xsl:template match="/">'
        '<xsl:sequence select="/inv:Invoice/amount + 0"/>'
        "</xsl:template>"
    )


def _typed_invoice_value_stylesheet() -> bytes:
    return _stylesheet(
        _import_schema() + '<xsl:template match="/">'
        '<xsl:sequence select="/inv:Invoice/amount"/>'
        "</xsl:template>"
    )


def _untyped_invoice_stylesheet() -> bytes:
    return _stylesheet(
        '<xsl:template match="/"><xsl:sequence select="/inv:Invoice/amount + 0"/></xsl:template>'
    )


def _error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


# ---------------------------------------------------------------------------
# Typed source input


def test_import_schema_and_typed_source(tmp_path: Path) -> None:
    _write(tmp_path / "invoice.xsd", INVOICE_XSD)
    stylesheet = _write(
        tmp_path / "typed-input.xsl",
        _stylesheet(
            f'<xsl:import-schema namespace="{INVOICE}" schema-location="invoice.xsd"/>'
            '<xsl:template match="/">'
            '<xsl:sequence select="/inv:Invoice/amount + 0"/>'
            "</xsl:template>"
        ).decode(),
    )
    compiled = compile_stylesheet(stylesheet)

    result = compiled.transform(TYPED_INVOICE)

    assert result.principal == (EXPECTED_DECIMAL,)
    matched = evaluate("self::schema-element(inv:Invoice)", compiled, TYPED_INVOICE)
    assert len(matched) == 1
    untyped = compile_stylesheet(
        io.BytesIO(_untyped_invoice_stylesheet()),
        validation="strip",
    )
    assert untyped.transform(UNTYPED_INVOICE).principal != (EXPECTED_DECIMAL,)


@pytest.mark.parametrize(
    ("validation", "expected"),
    [
        ("strict", f"{{{XSD_NS}}}decimal"),
        ("lax", f"{{{XSD_NS}}}decimal"),
        ("preserve", f"{{{XSD_NS}}}untyped"),
        ("strip", f"{{{XSD_NS}}}untyped"),
    ],
)
def test_source_validation_modes(validation: str, expected: str) -> None:
    compiled = compile_stylesheet(
        io.BytesIO(_typed_invoice_value_stylesheet()),
        validation=validation,
    )

    result = compiled.transform(TYPED_INVOICE)

    assert result.principal[0].type_name == expected


def test_strict_invalid_source_raises_xtte1510() -> None:
    compiled = compile_stylesheet(io.BytesIO(_typed_invoice_stylesheet()))
    invalid = (
        f'<inv:Invoice xmlns:inv="{INVOICE}"><amount>not-a-number</amount></inv:Invoice>'
    ).encode()

    assert _error_code(lambda: compiled.transform(invalid)) == "XTTE1510"


def test_strict_unknown_root_raises_xtte1512() -> None:
    compiled = compile_stylesheet(io.BytesIO(_typed_invoice_stylesheet()))
    unknown = (
        f'<inv:NotDeclared xmlns:inv="{INVOICE}"><amount>1</amount></inv:NotDeclared>'
    ).encode()

    assert _error_code(lambda: compiled.transform(unknown)) == "XTTE1512"


def test_lax_unknown_root_stays_untyped() -> None:
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema()
                + '<xsl:template match="/"><xsl:sequence select="//amount"/></xsl:template>'
            )
        ),
        validation="lax",
    )
    unknown = (
        f'<inv:NotDeclared xmlns:inv="{INVOICE}"><amount>1</amount></inv:NotDeclared>'
    ).encode()

    result = compiled.transform(unknown)

    assert result.principal[0].type_name == f"{{{XSD_NS}}}untyped"


def test_validation_type_selects_a_named_type() -> None:
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema()
                + '<xsl:template match="/"><xsl:sequence select=". + 0"/></xsl:template>'
            )
        ),
        validation="strict",
    )
    source = f'<inv:amount xmlns:inv="{INVOICE}">12.5</inv:amount>'.encode()

    typed = compiled.transform(source, validation_type="inv:Amount")
    untyped = compiled.transform(source, validation="preserve")

    assert typed.principal == (XdmAtomic.decimal(Decimal("12.5")),)
    assert untyped.principal[0].type_name == f"{{{XSD_NS}}}double"


def test_unknown_validation_type_is_static_error() -> None:
    compiled = compile_stylesheet(io.BytesIO(_typed_invoice_stylesheet()))

    assert (
        _error_code(lambda: compiled.transform(TYPED_INVOICE, validation_type="inv:Missing"))
        == "XPST0051"
    )


def test_xsi_type_selects_the_derived_type() -> None:
    compiled = compile_stylesheet(io.BytesIO(_typed_invoice_stylesheet()))
    source = (
        f'<inv:Invoice xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:SpecialInvoice">'
        "<amount>1.5</amount><code>special</code>"
        "</inv:Invoice>"
    ).encode()

    result = compiled.transform(source)

    assert result.principal == (XdmAtomic.decimal(Decimal("1.5")),)
    assert evaluate("self::schema-element(inv:Invoice)", compiled, source)
    assert evaluate("self::element(*, inv:SpecialInvoice)", compiled, source)
    assert not evaluate("self::element(*, inv:InvoiceType)", compiled, source)


# ---------------------------------------------------------------------------
# Annotations


@pytest.mark.parametrize(
    ("annotations", "matched"),
    [("preserve", True), ("strip", False)],
)
def test_input_type_annotations_control_schema_element_matching(
    annotations: str, matched: bool
) -> None:
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template match="/">'
                '<xsl:apply-templates select="/inv:Invoice"/>'
                "</xsl:template>"
                '<xsl:template match="schema-element(inv:Invoice)">'
                "<matched/>"
                "</xsl:template>"
                '<xsl:template match="*">'
                "<untyped/>"
                "</xsl:template>",
                attributes=f' input-type-annotations="{annotations}"',
            )
        )
    )

    result = compiled.transform(TYPED_INVOICE, delivery="document")

    assert result.principal_tree.root_element.local_name == ("matched" if matched else "untyped")


def test_preserve_and_strip_a_typed_document(tmp_path: Path) -> None:
    schema = Schema.compile(io.BytesIO(INVOICE_XSD.encode()))
    typed = schema.parse(io.BytesIO(TYPED_INVOICE), capture_xdm=True)
    stylesheet = _stylesheet(
        _import_schema() + '<xsl:template match="/">'
        '<xsl:sequence select="/inv:Invoice/amount"/>'
        "</xsl:template>"
    )

    preserved = compile_stylesheet(
        io.BytesIO(stylesheet),
        validation="preserve",
    ).transform(typed)
    stripped = compile_stylesheet(
        io.BytesIO(
            stylesheet.replace(b'version="3.0"', b'version="3.0" input-type-annotations="strip"')
        ),
        validation="preserve",
    ).transform(typed)

    assert preserved.principal[0].type_name == f"{{{XSD_NS}}}decimal"
    assert stripped.principal[0].type_name == f"{{{XSD_NS}}}untyped"


# ---------------------------------------------------------------------------
# document() and xsl:source-document


class _Policy:
    def __init__(self, mapping: dict[str, bytes]) -> None:
        self.mapping = dict(mapping)
        self.reads: list[tuple[object, str | None, str]] = []

    def read(self, source: Any, *, base: str | None, purpose: str) -> ResourceData:
        self.reads.append((source, base, purpose))
        if not isinstance(source, str):
            content = source.read()
            if isinstance(content, str):
                content = content.encode("utf-8")
            return ResourceData(getattr(source, "name", "<memory>"), content)
        return ResourceData(source, self.mapping[source])


def test_document_function_input_is_typed() -> None:
    policy = _Policy({"urn:doc": TYPED_INVOICE})
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                "<xsl:sequence select=\"document('urn:doc')/inv:Invoice/amount + 0\"/>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    result = compiled.transform(initial_template="main")

    assert result.principal == (EXPECTED_DECIMAL,)
    assert ("urn:doc", None, "instance") in policy.reads


@pytest.mark.parametrize(
    ("validation", "expected"),
    [
        ("strict", f"{{{XSD_NS}}}decimal"),
        ("strip", f"{{{XSD_NS}}}untyped"),
    ],
)
def test_source_document_validation_attribute(validation: str, expected: str) -> None:
    policy = _Policy({"urn:doc": TYPED_INVOICE})
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                f'<xsl:source-document href="urn:doc" validation="{validation}">'
                '<xsl:sequence select="/inv:Invoice/amount"/>'
                "</xsl:source-document>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    result = compiled.transform(initial_template="main")

    assert result.principal[0].type_name == expected


def test_source_document_strict_invalid_raises_xtte1510() -> None:
    policy = _Policy(
        {
            "urn:doc": (
                f'<inv:Invoice xmlns:inv="{INVOICE}"><amount>bad</amount></inv:Invoice>'
            ).encode()
        }
    )
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                '<xsl:source-document href="urn:doc" validation="strict"/>'
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1510"


def test_source_document_type_attribute_validates_named_type() -> None:
    policy = _Policy({"urn:doc": f'<inv:amount xmlns:inv="{INVOICE}">7.5</inv:amount>'.encode()})
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                '<xsl:source-document href="urn:doc" type="inv:Amount">'
                '<xsl:sequence select="data(*)"/>'
                "</xsl:source-document>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    result = compiled.transform(initial_template="main")

    assert result.principal[0].lexical == "7.5"
    assert result.principal[0].type_name == f"{{{INVOICE}}}Amount"


def test_source_document_type_failure_raises_xtte1540() -> None:
    policy = _Policy({"urn:doc": f'<inv:amount xmlns:inv="{INVOICE}">bad</inv:amount>'.encode()})
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                '<xsl:source-document href="urn:doc" type="inv:Amount"/>'
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_source_document_unknown_type_is_static_error() -> None:
    policy = _Policy({"urn:doc": TYPED_INVOICE})
    stylesheet = _stylesheet(
        _import_schema() + '<xsl:template name="main">'
        '<xsl:source-document href="urn:doc" type="inv:Missing"/>'
        "</xsl:template>"
    )

    assert (
        _error_code(lambda: compile_stylesheet(io.BytesIO(stylesheet), resource_policy=policy))
        == "XPST0051"
    )


def test_merge_source_validation_is_applied() -> None:
    policy = _Policy({"urn:a": TYPED_INVOICE})
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                "<out>"
                "<xsl:merge>"
                "<xsl:merge-source for-each-source=\"('urn:a')\" "
                'select="/inv:Invoice/amount" validation="strict">'
                '<xsl:merge-key select="."/>'
                "</xsl:merge-source>"
                "<xsl:merge-action>"
                '<xsl:sequence select="current-merge-group()"/>'
                "</xsl:merge-action>"
                "</xsl:merge>"
                "</out>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    result = compiled.transform(initial_template="main", delivery="document")

    assert result.principal_tree.string_value == "12.50"


# ---------------------------------------------------------------------------
# Constructors and schema versions


def test_imported_constructor_supports_arithmetic_and_matching() -> None:
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                "<xsl:sequence select=\"inv:Amount('12.50') + 0\"/>"
                "</xsl:template>"
            )
        )
    )

    result = compiled.transform(initial_template="main")

    assert result.principal == (EXPECTED_DECIMAL,)
    assert evaluate("inv:Amount('1.5') + 0", compiled) == (XdmAtomic.decimal(Decimal("1.5")),)


def test_typed_input_uses_the_selected_xsd_version() -> None:
    schema = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}"
        xmlns:inv="{INVOICE}" xmlns:vc="http://www.w3.org/2007/XMLSchema-versioning">
      <xs:element name="Invoice" type="xs:decimal" vc:minVersion="1.1"/>
    </xs:schema>"""
    stylesheet = _stylesheet(
        _import_schema(schema)
        + '<xsl:template match="/"><xsl:sequence select=". + 0"/></xsl:template>'
    )
    source = f'<inv:Invoice xmlns:inv="{INVOICE}">4.5</inv:Invoice>'.encode()

    modern = compile_stylesheet(io.BytesIO(stylesheet), xsd_version="1.1")
    assert modern.schema_pool.xsd_version == "1.1"
    assert modern.transform(source).principal == (XdmAtomic.decimal(Decimal("4.5")),)

    legacy = compile_stylesheet(io.BytesIO(stylesheet), xsd_version="1.0")
    assert legacy.schema_pool.xsd_version == "1.0"
    assert _error_code(lambda: legacy.transform(source)) == "XTTE1512"


def test_strip_an_xdm_document_does_not_mutate_the_original() -> None:
    from pyxsd.xdm.document import parse_typed_document, strip_type_annotations

    typed = parse_typed_document(
        TYPED_INVOICE,
        schema_pool=SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode()))),
        validation="strict",
    )
    stripped = strip_type_annotations(
        parse_typed_document(
            TYPED_INVOICE,
            schema_pool=SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode()))),
            validation="strict",
        )
    )

    assert typed.root_element.children[0].type_name == f"{{{XSD_NS}}}decimal"
    assert stripped.root_element.children[0].type_name == f"{{{XSD_NS}}}untyped"


def test_typed_xdm_document_strict_requires_an_assessment() -> None:
    from pyxsd.xdm.nodes import XdmDocument

    compiled = compile_stylesheet(io.BytesIO(_typed_invoice_stylesheet()))
    untyped = XdmDocument.parse(TYPED_INVOICE)

    assert _error_code(lambda: compiled.transform(untyped)) == "XTTE1512"


def test_parse_typed_document_argument_validation() -> None:
    from pyxsd.xdm.document import parse_typed_document

    with pytest.raises(ValueError, match="validation"):
        parse_typed_document(TYPED_INVOICE, validation="bogus")
    with pytest.raises(ValueError, match="input-type-annotations"):
        parse_typed_document(TYPED_INVOICE, input_type_annotations="bogus")
    with pytest.raises(TypeError, match="type name"):
        parse_typed_document(TYPED_INVOICE, type_name=1)  # type: ignore[arg-type]


def test_element_from_xdm_preserves_text_and_tail() -> None:
    from pyxsd.xdm.document import element_from_xdm
    from pyxsd.xdm.nodes import XdmDocument

    document = XdmDocument.parse(b"<root>before<a/>after<b/>tail</root>")
    element = element_from_xdm(document.root_element)

    assert element.text == "before"
    assert [child.tag for child in element] == ["a", "b"]
    assert element[0].tail == "after"
    assert element[1].tail == "tail"
    with pytest.raises(TypeError, match="element node"):
        element_from_xdm(document)


def test_copy_and_strip_annotation_contracts() -> None:
    from pyxsd.xdm.document import copy_type_annotations, strip_type_annotations
    from pyxsd.xdm.nodes import XdmDocument

    source = XdmDocument.parse(b"<root><child/></root>")
    target = XdmDocument.parse(b"<root><child/></root>")
    assert copy_type_annotations(source, target) is target

    other = XdmDocument.parse(b"<root><different/></root>")
    with pytest.raises(ValueError, match="different revisions"):
        copy_type_annotations(source, other)
    with pytest.raises(TypeError, match="two XdmDocument"):
        copy_type_annotations(source, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="stripped"):
        strip_type_annotations(object())  # type: ignore[arg-type]


def test_document_resources_constructor_validation() -> None:
    from pyxsd.xslt.runtime.resources import DocumentResources

    with pytest.raises(ValueError, match="validation"):
        DocumentResources({}, validation="bogus")
    with pytest.raises(TypeError, match="validation_type"):
        DocumentResources({}, validation_type=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="input-type-annotations"):
        DocumentResources({}, input_type_annotations="bogus")


def test_document_source_strict_and_type_validation() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    invalid = schema_document("bad")
    with pytest.raises(Exception) as caught:
        parse_typed_document(
            invalid,
            schema_pool=pool,
            validation="strict",
        )
    assert getattr(caught.value, "reason", None) == "invalid"

    typed = parse_typed_document(
        amount_document("12.5"),
        schema_pool=pool,
        validation="strict",
        type_name=f"{{{INVOICE}}}Amount",
    )
    assert typed.root_element.type_name == f"{{{INVOICE}}}Amount"


def schema_document(amount: str):
    schema = Schema.compile(io.BytesIO(INVOICE_XSD.encode()))
    return schema.parse(
        io.BytesIO(
            f'<inv:Invoice xmlns:inv="{INVOICE}"><amount>{amount}</amount></inv:Invoice>'.encode()
        ),
        capture_xdm=True,
    )


def amount_document(amount: str):
    schema = Schema.compile(io.BytesIO(INVOICE_XSD.encode()))
    return schema.parse(
        io.BytesIO(f'<inv:amount xmlns:inv="{INVOICE}">{amount}</inv:amount>'.encode()),
        capture_xdm=True,
    )


def test_parse_typed_document_type_name_found_by_type_only() -> None:
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    source = f'<inv:Undeclared xmlns:inv="{INVOICE}">1</inv:Undeclared>'.encode()

    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(
            source,
            schema_pool=pool,
            validation="strict",
            type_name=f"{{{INVOICE}}}Amount",
        )
    assert caught.value.reason in {"unknown", "type"}


def test_parse_typed_document_strips_a_pre_captured_tree() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    typed = parse_typed_document(TYPED_INVOICE, schema_pool=pool, validation="strict")
    stripped = parse_typed_document(
        typed,
        schema_pool=pool,
        validation="preserve",
        input_type_annotations="strip",
    )

    assert typed.root_element.children[0].type_name == f"{{{XSD_NS}}}decimal"
    assert stripped.root_element.children[0].type_name == f"{{{XSD_NS}}}untyped"


def test_named_type_validation_contract() -> None:
    from pyxsd.xdm.document import DocumentValidationError, _validate_named_type

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))

    with pytest.raises(DocumentValidationError) as caught:
        _validate_named_type(None, f"{{{INVOICE}}}Amount", pool)
    assert caught.value.reason == "type"
    with pytest.raises(DocumentValidationError) as caught:
        _validate_named_type("1", f"{{{INVOICE}}}Missing", pool)
    assert caught.value.reason == "type"


def test_document_validation_error_carries_a_cause() -> None:
    from pyxsd.xdm.document import DocumentValidationError

    cause = ValueError("cause")
    error = DocumentValidationError("message", reason="invalid", cause=cause)

    assert error.reason == "invalid"
    assert error.__cause__ is cause


def test_strip_type_annotations_handles_attributes() -> None:
    from pyxsd.xdm.document import parse_typed_document, strip_type_annotations

    schema = Schema.compile(
        io.BytesIO(
            (
                f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}">'
                '<xs:element name="Invoice">'
                '<xs:complexType><xs:attribute name="currency" type="xs:string"/>'
                "</xs:complexType>"
                "</xs:element>"
                "</xs:schema>"
            ).encode()
        )
    )
    pool = SchemaPool.from_schema(schema)
    typed = parse_typed_document(
        f'<inv:Invoice xmlns:inv="{INVOICE}" currency="EUR"/>'.encode(),
        schema_pool=pool,
        validation="strict",
    )
    attribute = typed.root_element.attributes[0]
    assert attribute.type_name == f"{{{XSD_NS}}}string"

    strip_type_annotations(typed)

    assert attribute.type_name == f"{{{XSD_NS}}}untypedAtomic"


def test_copy_type_annotations_structure_mismatches() -> None:
    from pyxsd.xdm.document import copy_type_annotations
    from pyxsd.xdm.nodes import XdmDocument

    source = XdmDocument.parse(b"<root><a/></root>")
    fewer_children = XdmDocument.parse(b"<root/>")
    fewer_attributes = XdmDocument.parse(b"<root a='1'><a/></root>")
    different_attribute = XdmDocument.parse(b"<root b='1'><a/></root>")

    for target in (fewer_children, fewer_attributes, different_attribute):
        with pytest.raises(ValueError, match="different revisions"):
            copy_type_annotations(source, target)


def test_document_resources_rejects_an_invalid_override_mode() -> None:
    from pyxsd.xslt.runtime.resources import DocumentResources

    resources = DocumentResources({}, policy=_Policy({}))

    with pytest.raises(ValueError, match="validation"):
        resources.resolve("urn:doc", base=None, validation="bogus")


def test_typed_xdm_document_can_be_reused_under_strict() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    typed = parse_typed_document(TYPED_INVOICE, schema_pool=pool, validation="strict")
    compiled = compile_stylesheet(io.BytesIO(_typed_invoice_value_stylesheet()))

    result = compiled.transform(typed)

    assert result.principal[0].type_name == f"{{{XSD_NS}}}decimal"


def test_named_type_validation_simple_string_arms() -> None:
    from pyxsd.xdm.document import DocumentValidationError, _validate_named_type

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))

    _validate_named_type("12.5", f"{{{INVOICE}}}Amount", pool)
    with pytest.raises(DocumentValidationError) as caught:
        _validate_named_type("bad", f"{{{INVOICE}}}Amount", pool)
    assert caught.value.reason == "type"


def test_element_from_xdm_skips_comments() -> None:
    from pyxsd.xdm.document import element_from_xdm
    from pyxsd.xdm.nodes import XdmDocument

    document = XdmDocument.parse(b"<root><!--note--><a/></root>")
    element = element_from_xdm(document.root_element)

    assert [child.tag for child in element] == ["a"]


def test_copy_type_annotations_rejects_attribute_name_mismatch() -> None:
    from pyxsd.xdm.document import copy_type_annotations
    from pyxsd.xdm.nodes import XdmDocument

    source = XdmDocument.parse(b"<root a='1'/>")
    target = XdmDocument.parse(b"<root b='1'/>")

    with pytest.raises(ValueError, match="different revisions"):
        copy_type_annotations(source, target)


def test_processor_and_invocation_validation_arguments() -> None:
    with pytest.raises(ValueError, match="xsd_version"):
        XsltProcessor(xsd_version="2.0")
    with pytest.raises(ValueError, match="validation"):
        XsltProcessor(validation="bogus")
    with pytest.raises(TypeError, match="validation_type"):
        XsltProcessor(validation_type=1)  # type: ignore[arg-type]

    compiled = compile_stylesheet(io.BytesIO(_untyped_invoice_stylesheet()))
    with pytest.raises(XsltInvocationError, match="validation"):
        compiled.transform(TYPED_INVOICE, validation="bogus")
    with pytest.raises(TypeError, match="validation_type"):
        compiled.transform(TYPED_INVOICE, validation_type=1)  # type: ignore[arg-type]


def test_builtin_validation_type_without_a_schema_is_accepted() -> None:
    compiled = compile_stylesheet(
        io.BytesIO(_untyped_invoice_stylesheet()),
        validation="preserve",
    )

    result = compiled.transform(TYPED_INVOICE, validation_type="xs:anyType")

    assert result.principal[0].type_name == f"{{{XSD_NS}}}double"


def test_unknown_validation_type_without_a_schema_is_an_error() -> None:
    compiled = compile_stylesheet(
        io.BytesIO(_untyped_invoice_stylesheet()),
        validation="preserve",
    )

    assert (
        _error_code(lambda: compiled.transform(TYPED_INVOICE, validation_type="inv:Missing"))
        == "XPST0051"
    )


@pytest.mark.parametrize(
    ("type_source", "code"),
    [
        ("", "XPST0003"),
        ("missing:Amount", "XPST0081"),
        ("inv:Missing", "XPST0051"),
    ],
)
def test_source_document_type_static_errors(type_source: str, code: str) -> None:
    stylesheet = _stylesheet(
        _import_schema() + '<xsl:template name="main">'
        f'<xsl:source-document href="urn:doc" type="{type_source}"/>'
        "</xsl:template>"
    )

    assert _error_code(lambda: compile_stylesheet(io.BytesIO(stylesheet))) == code


def test_source_document_type_eqname_and_default_namespace() -> None:
    policy = _Policy({"urn:doc": f'<inv:amount xmlns:inv="{INVOICE}">1</inv:amount>'.encode()})
    eqname = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                f'<xsl:source-document href="urn:doc" type="Q{{{INVOICE}}}Amount">'
                '<xsl:sequence select="data(*)"/>'
                "</xsl:source-document>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )
    defaulted = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                '<xsl:source-document href="urn:doc" type="Amount">'
                '<xsl:sequence select="data(*)"/>'
                "</xsl:source-document>"
                "</xsl:template>",
                attributes=f' xpath-default-namespace="{INVOICE}"',
            )
        ),
        resource_policy=policy,
    )

    assert eqname.transform(initial_template="main").principal[0].type_name == (
        f"{{{INVOICE}}}Amount"
    )
    assert defaulted.transform(initial_template="main").principal[0].type_name == (
        f"{{{INVOICE}}}Amount"
    )


def test_invalid_type_eqname_and_unpooled_type() -> None:
    bad_eqname = _stylesheet(
        _import_schema() + '<xsl:template name="main">'
        '<xsl:source-document href="urn:doc" type="Q{urn:example:invoice"/>'
        "</xsl:template>"
    )
    unpooled = _stylesheet(
        '<xsl:template name="main">'
        '<xsl:source-document href="urn:doc" type="Amount"/>'
        "</xsl:template>"
    )

    assert _error_code(lambda: compile_stylesheet(io.BytesIO(bad_eqname))) == "XPST0003"
    assert _error_code(lambda: compile_stylesheet(io.BytesIO(unpooled))) == "XPST0051"


def test_explicit_schema_without_imports_is_used() -> None:
    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    compiled = XsltProcessor(validation="strict").compile(
        io.BytesIO(_untyped_invoice_stylesheet()),
        schema=pool,
    )

    result = compiled.transform(TYPED_INVOICE)

    assert result.principal[0].type_name == f"{{{XSD_NS}}}decimal"
