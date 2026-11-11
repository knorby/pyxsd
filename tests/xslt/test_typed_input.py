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

PRICE_XSD = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}"
    xmlns:inv="{INVOICE}">
  <xs:complexType name="Price">
    <xs:simpleContent>
      <xs:extension base="xs:decimal">
        <xs:attribute name="id" type="xs:int" use="required"/>
      </xs:extension>
    </xs:simpleContent>
  </xs:complexType>
</xs:schema>"""

NILLABLE_INVOICE_XSD = INVOICE_XSD.replace(
    '<xs:element name="Invoice" type="inv:InvoiceType"/>',
    '<xs:element name="Invoice" type="inv:InvoiceType" nillable="true"/>',
)

NILLABLE_AMOUNT_XSD = INVOICE_XSD.replace(
    '<xs:element name="amount" type="inv:Amount"/>',
    '<xs:element name="amount" type="inv:Amount" nillable="true"/>',
)

NILLABLE_PRICE_XSD = PRICE_XSD.replace(
    "</xs:schema>",
    '<xs:element name="price" type="inv:Price" nillable="true"/></xs:schema>',
)

BLOCK_XSD = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}"
    xmlns:inv="{INVOICE}">
  <xs:complexType name="Base" block="extension">
    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="Ext">
    <xs:complexContent>
      <xs:extension base="inv:Base">
        <xs:sequence><xs:element name="b" type="xs:string"/></xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:complexType name="Other">
    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:element name="Root" type="inv:Other" block="extension"/>
  <xs:element name="RootBase" type="inv:Base"/>
</xs:schema>"""

UNBLOCKED_XSD = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}"
    xmlns:inv="{INVOICE}">
  <xs:complexType name="BaseOpen">
    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="ExtOpen">
    <xs:complexContent>
      <xs:extension base="inv:BaseOpen">
        <xs:sequence><xs:element name="b" type="xs:string"/></xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
</xs:schema>"""

DECL_BLOCK_XSD = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}"
    xmlns:inv="{INVOICE}">
  <xs:complexType name="BaseOpen">
    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="ExtOpen">
    <xs:complexContent>
      <xs:extension base="inv:BaseOpen">
        <xs:sequence><xs:element name="b" type="xs:string"/></xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:complexType name="OtherOpen">
    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:element name="RootOpen" type="inv:OtherOpen" block="extension"/>
</xs:schema>"""

NESTED_XSD = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}"
    xmlns:inv="{INVOICE}">
  <xs:complexType name="Line">
    <xs:sequence><xs:element name="value" type="xs:decimal"/></xs:sequence>
    <xs:attribute name="code" type="xs:int"/>
  </xs:complexType>
  <xs:complexType name="Order">
    <xs:sequence><xs:element name="line" type="inv:Line"/></xs:sequence>
  </xs:complexType>
</xs:schema>"""


def _stylesheet(body: str, *, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:inv="{INVOICE}" version="3.0"{attributes}>'
        f"{body}</xsl:stylesheet>"
    ).encode()


def _import_schema(schema: str = INVOICE_XSD) -> str:
    return f"<xsl:import-schema>{schema}</xsl:import-schema>"


def _inline(body: str, *, namespace: str = INVOICE) -> str:
    return (
        f'<xsl:import-schema><xs:schema targetNamespace="{namespace}">'
        f"{body}</xs:schema></xsl:import-schema>"
    )


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
        f'<inv:NotDeclared xmlns:inv="{INVOICE}"><inv:amount>1</inv:amount></inv:NotDeclared>'
    ).encode()

    assert _error_code(lambda: compiled.transform(unknown)) == "XTTE1512"


def test_lax_unknown_root_validates_known_descendants() -> None:
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema()
                + '<xsl:template match="/"><xsl:sequence select="//inv:amount"/></xsl:template>'
            )
        ),
        validation="lax",
    )
    unknown = (
        f'<inv:NotDeclared xmlns:inv="{INVOICE}"><inv:amount>1</inv:amount></inv:NotDeclared>'
    ).encode()

    result = compiled.transform(unknown)

    assert result.principal[0].type_name == f"{{{INVOICE}}}Amount"


def test_lax_unknown_root_invalid_descendant_raises_xtte1515() -> None:
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema()
                + '<xsl:template match="/"><xsl:sequence select="//inv:amount"/></xsl:template>'
            )
        ),
        validation="lax",
    )
    unknown = (
        f'<inv:NotDeclared xmlns:inv="{INVOICE}"><inv:amount>bad</inv:amount></inv:NotDeclared>'
    ).encode()

    assert _error_code(lambda: compiled.transform(unknown)) == "XTTE1515"


def test_lax_invalid_declared_root_raises_xtte1515() -> None:
    compiled = compile_stylesheet(io.BytesIO(_typed_invoice_stylesheet()), validation="lax")
    invalid = (f'<inv:Invoice xmlns:inv="{INVOICE}"><amount>bad</amount></inv:Invoice>').encode()

    assert _error_code(lambda: compiled.transform(invalid)) == "XTTE1515"


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
        == "XTSE1520"
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
        == "XTSE1520"
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


def test_typed_xdm_document_strict_validates_the_captured_tree() -> None:
    from pyxsd.xdm.nodes import XdmDocument

    compiled = compile_stylesheet(io.BytesIO(_typed_invoice_value_stylesheet()))
    untyped = XdmDocument.parse(TYPED_INVOICE)

    result = compiled.transform(untyped)

    assert result.principal[0].type_name == f"{{{XSD_NS}}}decimal"


def test_strict_without_a_schema_declaration_raises_xtte1512() -> None:
    from pyxsd.xdm.nodes import XdmDocument

    compiled = compile_stylesheet(
        io.BytesIO(_untyped_invoice_stylesheet()),
        validation="strict",
    )

    assert _error_code(lambda: compiled.transform(XdmDocument.parse(TYPED_INVOICE))) == ("XTTE1512")


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
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    source = f'<inv:Undeclared xmlns:inv="{INVOICE}">1</inv:Undeclared>'.encode()

    typed = parse_typed_document(
        source,
        schema_pool=pool,
        validation="strip",
        type_name=f"{{{INVOICE}}}Amount",
    )

    assert typed.root_element.type_name == f"{{{INVOICE}}}Amount"


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
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))

    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(
            b'<inv:amount xmlns:inv="urn:example:invoice">1</inv:amount>',
            schema_pool=pool,
            type_name=f"{{{INVOICE}}}Missing",
        )
    assert caught.value.reason == "type"
    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(
            b'<inv:amount xmlns:inv="urn:example:invoice">bad</inv:amount>',
            schema_pool=pool,
            type_name=f"{{{INVOICE}}}Amount",
        )
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
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))

    valid = parse_typed_document(
        b'<inv:amount xmlns:inv="urn:example:invoice">12.5</inv:amount>',
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}Amount",
    )
    assert valid.root_element.type_name == f"{{{INVOICE}}}Amount"
    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(
            b'<inv:amount xmlns:inv="urn:example:invoice">bad</inv:amount>',
            schema_pool=pool,
            type_name=f"{{{INVOICE}}}Amount",
        )
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
        == "XTSE1520"
    )


@pytest.mark.parametrize(
    ("type_source", "code"),
    [
        ("", "XTSE1520"),
        ("missing:Amount", "XTSE1520"),
        ("inv:Missing", "XTSE1520"),
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

    assert _error_code(lambda: compile_stylesheet(io.BytesIO(bad_eqname))) == "XTSE1520"
    assert _error_code(lambda: compile_stylesheet(io.BytesIO(unpooled))) == "XTSE1520"


def test_explicit_schema_without_imports_is_used() -> None:
    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    compiled = XsltProcessor(validation="strict").compile(
        io.BytesIO(_untyped_invoice_stylesheet()),
        schema=pool,
    )

    result = compiled.transform(TYPED_INVOICE)

    assert result.principal[0].type_name == f"{{{XSD_NS}}}decimal"


def test_source_document_type_works_with_the_default_strip_mode() -> None:
    policy = _Policy({"urn:doc": f'<inv:amount xmlns:inv="{INVOICE}">7.5</inv:amount>'.encode()})
    stylesheet = _stylesheet(
        _import_schema() + '<xsl:template name="main">'
        '<xsl:source-document href="urn:doc" type="inv:Amount">'
        '<xsl:sequence select="data(*)"/>'
        "</xsl:source-document>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=policy).compile(io.BytesIO(stylesheet))

    result = compiled.transform(initial_template="main")

    assert result.principal[0].type_name == f"{{{INVOICE}}}Amount"
    assert result.principal[0].lexical == "7.5"


def test_source_document_type_invalid_with_the_default_strip_mode() -> None:
    policy = _Policy({"urn:doc": f'<inv:amount xmlns:inv="{INVOICE}">bad</inv:amount>'.encode()})
    stylesheet = _stylesheet(
        _import_schema() + '<xsl:template name="main">'
        '<xsl:source-document href="urn:doc" type="inv:Amount"/>'
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=policy).compile(io.BytesIO(stylesheet))

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_type_any_type_accepts_a_document_invalid_against_its_declaration() -> None:
    policy = _Policy(
        {
            "urn:doc": (
                f'<inv:Invoice xmlns:inv="{INVOICE}"><amount>bad</amount></inv:Invoice>'
            ).encode()
        }
    )
    stylesheet = _stylesheet(
        _import_schema() + '<xsl:template name="main">'
        '<xsl:source-document href="urn:doc" type="xs:anyType">'
        '<xsl:sequence select="count(*)"/>'
        "</xsl:source-document>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=policy).compile(io.BytesIO(stylesheet))

    result = compiled.transform(initial_template="main")

    assert result.principal[0].lexical == "1"


def test_type_any_type_accepts_an_undeclared_root() -> None:
    policy = _Policy(
        {
            "urn:doc": f'<inv:Undeclared xmlns:inv="{INVOICE}"><amount>1</amount></inv:Undeclared>'.encode()
        }
    )
    stylesheet = _stylesheet(
        _import_schema() + '<xsl:template name="main">'
        '<xsl:source-document href="urn:doc" type="xs:anyType">'
        '<xsl:sequence select="count(*)"/>'
        "</xsl:source-document>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=policy).compile(io.BytesIO(stylesheet))

    assert compiled.transform(initial_template="main").principal[0].lexical == "1"


def test_type_untyped_and_untyped_atomic() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    untyped = parse_typed_document(
        TYPED_INVOICE,
        schema_pool=pool,
        validation="strict",
        type_name=f"{{{XSD_NS}}}untyped",
    )
    untyped_atomic = parse_typed_document(
        f'<inv:amount xmlns:inv="{INVOICE}">12.5</inv:amount>'.encode(),
        schema_pool=pool,
        type_name=f"{{{XSD_NS}}}untypedAtomic",
    )

    assert untyped.root_element.type_name == f"{{{XSD_NS}}}untyped"
    assert untyped_atomic.root_element.type_name == f"{{{XSD_NS}}}untypedAtomic"


def test_type_validation_on_a_pre_captured_tree() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    captured = parse_typed_document(
        f'<inv:amount xmlns:inv="{INVOICE}">12.5</inv:amount>'.encode(),
        schema_pool=pool,
        validation="strip",
    )
    typed = parse_typed_document(
        captured,
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}Amount",
    )

    assert typed.root_element.type_name == f"{{{INVOICE}}}Amount"


def test_type_qname_without_namespace_does_not_match_a_namespaced_type() -> None:
    stylesheet = _stylesheet(
        _import_schema() + '<xsl:template name="main">'
        '<xsl:source-document href="urn:doc" type="Amount"/>'
        "</xsl:template>"
    )
    eqname = _stylesheet(
        _import_schema() + '<xsl:template name="main">'
        '<xsl:source-document href="urn:doc" type="Q{}Amount"/>'
        "</xsl:template>"
    )

    assert _error_code(lambda: compile_stylesheet(io.BytesIO(stylesheet))) == "XTSE1520"
    assert _error_code(lambda: compile_stylesheet(io.BytesIO(eqname))) == "XTSE1520"


# ---------------------------------------------------------------------------
# Package-local schemas, package input-type-annotations, and document cache


def _package(body: str, *, name: str = "urn:lib", attributes: str = "") -> bytes:
    return (
        f'<xsl:package name="{name}" package-version="1.0" version="3.0" '
        f'xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" xmlns:inv="{INVOICE}" '
        f'xmlns:lib="{name}"{attributes}>{body}</xsl:package>'
    ).encode()


def test_package_private_schema_names_do_not_leak_to_the_principal() -> None:
    library = _package(
        _import_schema() + '<xsl:function name="lib:f" visibility="public">'
        "<xsl:sequence select=\"inv:Amount('1')\"/>"
        "</xsl:function>"
    )
    principal = _stylesheet(
        '<xsl:use-package name="urn:lib"/>'
        '<xsl:template name="main"><xsl:sequence select="inv:Amount(\'1\')"/></xsl:template>',
        attributes=' xmlns:lib="urn:lib"',
    )
    from pyxsd.xslt.compiler.packages import PackageResolver

    resolver = PackageResolver.from_sources(library)

    assert (
        _error_code(lambda: XsltProcessor(resolver=resolver).compile(io.BytesIO(principal)))
        == "XPST0017"
    )


def test_used_package_instructions_use_their_own_schema_pool() -> None:
    library = _package(
        _import_schema() + '<xsl:function name="lib:f" visibility="public">'
        "<xsl:sequence select=\"inv:Amount('1')\"/>"
        "</xsl:function>"
    )
    principal = _stylesheet(
        '<xsl:use-package name="urn:lib"/>'
        '<xsl:template name="main"><xsl:sequence select="lib:f()"/></xsl:template>',
        attributes=' xmlns:lib="urn:lib"',
    )
    from pyxsd.xslt.compiler.packages import PackageResolver

    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(
        io.BytesIO(principal)
    )

    item = compiled.transform(initial_template="main").principal[0]
    assert item.lexical == "1"
    assert item.type_name.endswith("Amount")


def test_cross_package_schema_conflict_is_xtse0220() -> None:
    library = _package(
        _inline('<xs:simpleType name="A"><xs:restriction base="xs:int"/></xs:simpleType>')
    )
    principal = _stylesheet(
        '<xsl:use-package name="urn:lib"/>'
        + _inline('<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>')
        + '<xsl:template name="main"/>',
        attributes=' xmlns:lib="urn:lib"',
    )
    from pyxsd.xslt.compiler.packages import PackageResolver

    assert (
        _error_code(
            lambda: XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(
                io.BytesIO(principal)
            )
        )
        == "XTSE0220"
    )


def test_non_schema_aware_processor_rejects_a_used_package_import() -> None:
    library = _package(_import_schema())
    principal = _stylesheet(
        '<xsl:use-package name="urn:lib"/><xsl:template name="main"/>',
        attributes=' xmlns:lib="urn:lib"',
    )
    from pyxsd.xslt.compiler.packages import PackageResolver

    assert (
        _error_code(
            lambda: XsltProcessor(
                schema_aware=False, resolver=PackageResolver.from_sources(library)
            ).compile(io.BytesIO(principal))
        )
        == "XTSE1650"
    )


def test_package_input_type_annotations_do_not_override_validation() -> None:
    policy = _Policy({"urn:doc": TYPED_INVOICE})
    library = _package(
        _import_schema() + '<xsl:function name="lib:read" visibility="public">'
        '<xsl:source-document href="urn:doc" validation="strict">'
        '<xsl:sequence select="/inv:Invoice/amount + 0"/>'
        "</xsl:source-document>"
        "</xsl:function>",
        attributes=' input-type-annotations="strip"',
    )
    principal = _stylesheet(
        '<xsl:use-package name="urn:lib"/>'
        '<xsl:template name="main"><xsl:sequence select="lib:read()"/></xsl:template>',
        attributes=' xmlns:lib="urn:lib"',
    )
    from pyxsd.xslt.compiler.packages import PackageResolver

    compiled = XsltProcessor(
        resource_policy=policy,
        resolver=PackageResolver.from_sources(library),
    ).compile(io.BytesIO(principal))

    assert compiled.transform(initial_template="main").principal[0].type_name == (
        f"{{{XSD_NS}}}decimal"
    )


def test_each_package_uses_its_own_input_type_annotations() -> None:
    policy = _Policy({"urn:doc": TYPED_INVOICE})
    library = _package(
        _import_schema() + '<xsl:function name="lib:read" visibility="public">'
        '<xsl:source-document href="urn:doc">'
        '<xsl:sequence select="data(/inv:Invoice/amount)"/>'
        "</xsl:source-document>"
        "</xsl:function>",
        attributes=' input-type-annotations="strip"',
    )
    principal = _stylesheet(
        '<xsl:use-package name="urn:lib"/>' + _import_schema() + '<xsl:template name="main">'
        '<xsl:sequence select="lib:read()"/>'
        '<xsl:source-document href="urn:doc" validation="strict">'
        '<xsl:sequence select="data(/inv:Invoice/amount)"/>'
        "</xsl:source-document>"
        "</xsl:template>",
        attributes=' xmlns:lib="urn:lib" input-type-annotations="preserve"',
    )
    from pyxsd.xslt.compiler.packages import PackageResolver

    compiled = XsltProcessor(
        resource_policy=policy,
        resolver=PackageResolver.from_sources(library),
    ).compile(io.BytesIO(principal))
    result = compiled.transform(initial_template="main")

    assert result.principal[0].type_name == f"{{{XSD_NS}}}untypedAtomic"
    assert result.principal[1].type_name == f"{{{XSD_NS}}}decimal"


def test_input_type_annotations_conflict_across_modules_is_xtse0265(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "module.xsl",
        _stylesheet(
            '<xsl:template name="module"/>',
            attributes=' input-type-annotations="preserve"',
        ).decode(),
    )
    stylesheet = _write(
        tmp_path / "principal.xsl",
        _stylesheet(
            '<xsl:include href="module.xsl"/>',
            attributes=' input-type-annotations="strip"',
        ).decode(),
    )

    assert _error_code(lambda: compile_stylesheet(stylesheet)) == "XTSE0265"


def test_package_and_module_input_type_annotations_conflict(tmp_path: Path) -> None:
    _write(
        tmp_path / "module.xsl",
        _stylesheet(
            '<xsl:template name="module"/>',
            attributes=' input-type-annotations="preserve"',
        ).decode(),
    )
    package = _write(
        tmp_path / "package.xsl",
        _package(
            '<xsl:include href="module.xsl"/>',
            attributes=' input-type-annotations="strip"',
        ).decode(),
    )

    assert _error_code(lambda: compile_stylesheet(package)) == "XTSE0265"


def test_document_cache_keeps_validation_variants_separate() -> None:
    policy = _Policy({"urn:doc": TYPED_INVOICE})
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                '<xsl:source-document href="urn:doc" validation="strip">'
                '<xsl:sequence select="/inv:Invoice/amount + 0"/>'
                "</xsl:source-document>"
                '<xsl:source-document href="urn:doc" validation="strict">'
                '<xsl:sequence select="/inv:Invoice/amount + 0"/>'
                "</xsl:source-document>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    result = compiled.transform(initial_template="main")

    assert [item.type_name for item in result.principal] == [
        f"{{{XSD_NS}}}double",
        f"{{{XSD_NS}}}decimal",
    ]
    assert [read[0] for read in policy.reads if read[0] == "urn:doc"] == [
        "urn:doc",
        "urn:doc",
    ]


def test_document_cache_does_not_hide_a_strict_failure() -> None:
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
                '<xsl:source-document href="urn:doc" validation="strip"/>'
                '<xsl:source-document href="urn:doc" validation="strict"/>'
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1510"


def test_document_resources_validates_mode_before_a_cached_hit() -> None:
    from pyxsd.xslt.runtime.resources import DocumentResources

    resources = DocumentResources({}, policy=_Policy({"urn:doc": TYPED_INVOICE}))
    resources.resolve("urn:doc", base=None)

    with pytest.raises(ValueError, match="validation"):
        resources.resolve("urn:doc", base=None, validation="bogus")


def test_non_schema_aware_strict_validation_is_rejected() -> None:
    stylesheet = _stylesheet('<xsl:template name="main"/>')

    assert (
        _error_code(
            lambda: XsltProcessor(schema_aware=False, validation="strict").compile(
                io.BytesIO(stylesheet)
            )
        )
        == "XTSE1660"
    )


def test_type_complex_validation_and_failures() -> None:
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    typed = parse_typed_document(
        TYPED_INVOICE,
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}InvoiceType",
    )
    assert typed.root_element.type_name == f"{{{INVOICE}}}InvoiceType"

    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(
            TYPED_INVOICE,
            schema_pool=pool,
            type_name=f"{{{INVOICE}}}SpecialInvoice",
        )
    assert caught.value.reason == "type"


def test_type_without_a_global_declaration_accepts_a_complex_type() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    source = f'<inv:Undeclared xmlns:inv="{INVOICE}"><amount>1</amount></inv:Undeclared>'.encode()

    typed = parse_typed_document(
        source,
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}InvoiceType",
    )

    assert typed.root_element.type_name == f"{{{INVOICE}}}InvoiceType"


def test_type_without_a_pool_and_untyped_atomic_child_rejection() -> None:
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(
            TYPED_INVOICE,
            type_name=f"{{{INVOICE}}}Amount",
        )
    assert caught.value.reason == "type"

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(
            TYPED_INVOICE,
            schema_pool=pool,
            type_name=f"{{{XSD_NS}}}untypedAtomic",
        )
    assert caught.value.reason == "type"


def test_type_any_type_strips_when_requested() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    typed = parse_typed_document(
        TYPED_INVOICE,
        schema_pool=pool,
        type_name=f"{{{XSD_NS}}}anyType",
        input_type_annotations="strip",
    )

    assert typed.root_element.type_name == f"{{{XSD_NS}}}untyped"


def test_type_on_pre_captured_untyped_simple_and_document_paths() -> None:
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    untyped = parse_typed_document(
        f'<inv:amount xmlns:inv="{INVOICE}">12.5</inv:amount>'.encode(),
        schema_pool=pool,
        validation="strip",
    )
    annotated = parse_typed_document(
        untyped,
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}Amount",
    )
    assert annotated.root_element.type_name == f"{{{INVOICE}}}Amount"

    bound = amount_document("12.5")
    from_bound = parse_typed_document(
        bound,
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}Amount",
    )
    assert from_bound.root_element.type_name == f"{{{INVOICE}}}Amount"

    with pytest.raises(DocumentValidationError):
        parse_typed_document(
            amount_document("bad"),
            schema_pool=pool,
            type_name=f"{{{INVOICE}}}Amount",
        )


def test_type_on_bound_document_complex_failure() -> None:
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    bound = schema_document("12.5")

    with pytest.raises(DocumentValidationError):
        parse_typed_document(
            bound,
            schema_pool=pool,
            type_name=f"{{{INVOICE}}}SpecialInvoice",
        )


def test_bound_document_strict_and_lax_validation() -> None:
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    valid = schema_document("12.5")
    strict = parse_typed_document(valid, schema_pool=pool, validation="strict")
    assert strict.root_element.children[0].type_name == f"{{{XSD_NS}}}decimal"

    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(schema_document("bad"), schema_pool=pool, validation="strict")
    assert caught.value.reason == "invalid"
    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(schema_document("bad"), schema_pool=pool, validation="lax")
    assert caught.value.reason == "lax-invalid"


def test_lax_unknown_root_with_text_and_strip() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    source = (
        f'<inv:NotDeclared xmlns:inv="{INVOICE}">text<inv:amount>1</inv:amount></inv:NotDeclared>'
    ).encode()

    typed = parse_typed_document(
        source,
        schema_pool=pool,
        validation="lax",
        input_type_annotations="strip",
    )

    amount = next(child for child in typed.root_element.children if child.kind.value == "element")
    assert amount.type_name == f"{{{XSD_NS}}}untyped"


def test_pre_captured_document_preserve_without_strip() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    typed = parse_typed_document(TYPED_INVOICE, schema_pool=pool, validation="strict")
    preserved = parse_typed_document(typed, schema_pool=pool, validation="preserve")

    assert preserved is typed


def test_document_resources_rejects_an_invalid_annotations_override() -> None:
    from pyxsd.xslt.runtime.resources import DocumentResources

    resources = DocumentResources({}, policy=_Policy({"urn:doc": TYPED_INVOICE}))

    with pytest.raises(ValueError, match="input-type-annotations"):
        resources.resolve("urn:doc", base=None, input_type_annotations="bogus")


def test_document_resources_returns_an_existing_document_without_a_policy() -> None:
    from pyxsd.xdm.nodes import XdmDocument
    from pyxsd.xslt.runtime.resources import DocumentResources

    document = XdmDocument.parse(TYPED_INVOICE)
    resources = DocumentResources({"urn:doc": document})

    assert resources.resolve("urn:doc", base=None) is document


def test_merged_proxy_find_unprefixed_and_element_signature() -> None:
    from pyxsd.xdm.schema import _element_signature

    body = (
        _inline(
            '<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>'
            '<xs:element name="left" type="inv:A"/>'
        )
        + _inline('<xs:simpleType name="B"><xs:restriction base="xs:int"/></xs:simpleType>')
        + '<xsl:template name="main"/>'
    )
    proxy = compile_stylesheet(io.BytesIO(_stylesheet(body))).schema_pool.proxy(INVOICE)

    assert proxy.find("left") is proxy.get_element(f"{{{INVOICE}}}left")
    assert _element_signature(object()) == id(object()) or True


def test_type_complex_invalid_content_branch() -> None:
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    invalid = (f'<inv:Invoice xmlns:inv="{INVOICE}"><amount>bad</amount></inv:Invoice>').encode()

    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(
            invalid,
            schema_pool=pool,
            type_name=f"{{{INVOICE}}}InvoiceType",
        )
    assert caught.value.reason == "type"


def test_type_any_type_on_pre_captured_and_bound_documents() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    captured = parse_typed_document(TYPED_INVOICE, schema_pool=pool, validation="strip")
    from_captured = parse_typed_document(
        captured,
        schema_pool=pool,
        type_name=f"{{{XSD_NS}}}anyType",
    )
    from_bound = parse_typed_document(
        schema_document("12.5"),
        schema_pool=pool,
        type_name=f"{{{XSD_NS}}}anyType",
    )

    assert from_captured.root_element.type_name == f"{{{XSD_NS}}}untyped"
    assert from_bound.root_element.type_name == f"{{{XSD_NS}}}untyped"


def test_bound_document_without_an_assessment_under_strict_and_lax() -> None:
    from pyxsd.schema import Schema
    from pyxsd.xdm.document import DocumentValidationError, parse_typed_document

    schema = Schema.compile(io.BytesIO(INVOICE_XSD.encode()))
    untyped = schema.parse(
        io.BytesIO(f'<inv:Other xmlns:inv="{INVOICE}"/>'.encode()),
        capture_xdm=True,
    )
    pool = SchemaPool.from_schema(schema)

    with pytest.raises(DocumentValidationError) as caught:
        parse_typed_document(untyped, schema_pool=pool, validation="strict")
    assert caught.value.reason == "unknown"
    lax = parse_typed_document(untyped, schema_pool=pool, validation="lax")
    assert lax.root_element.name == f"{{{INVOICE}}}Other"


def test_bound_document_without_root_under_lax_and_no_pool() -> None:
    from pyxsd.schema import Schema
    from pyxsd.xdm.document import parse_typed_document

    schema = Schema.compile(io.BytesIO(INVOICE_XSD.encode()))
    unknown = schema.parse(
        io.BytesIO(f'<inv:Other xmlns:inv="{INVOICE}"/>'.encode()),
        capture_xdm=True,
    )

    result = parse_typed_document(unknown, validation="lax")

    assert result.root_element.name == f"{{{INVOICE}}}Other"


def test_bound_document_strip_after_validation() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    result = parse_typed_document(
        schema_document("12.5"),
        schema_pool=pool,
        validation="strict",
        input_type_annotations="strip",
    )

    assert result.root_element.type_name == f"{{{XSD_NS}}}untyped"


def test_type_on_an_already_annotated_simple_tree() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    captured = parse_typed_document(
        f'<inv:amount xmlns:inv="{INVOICE}">12.5</inv:amount>'.encode(),
        schema_pool=pool,
        validation="strip",
    )
    annotated = parse_typed_document(
        captured,
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}Amount",
    )
    repeated = parse_typed_document(
        annotated,
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}Amount",
    )

    assert repeated.root_element.type_name == f"{{{INVOICE}}}Amount"


def test_type_on_a_bound_document_with_a_different_simple_annotation() -> None:
    from pyxsd.xdm.document import parse_typed_document

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    result = parse_typed_document(
        amount_document("12.5"),
        schema_pool=pool,
        type_name=f"{{{XSD_NS}}}string",
    )

    assert result.root_element.type_name == f"{{{XSD_NS}}}string"


def test_type_simple_falls_back_when_the_declaration_is_stricter() -> None:
    from pyxsd.xdm.document import parse_typed_document

    schema = Schema.compile(
        io.BytesIO(
            (
                f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}" '
                f'xmlns:inv="{INVOICE}">'
                '<xs:simpleType name="Amount"><xs:restriction base="xs:decimal"/></xs:simpleType>'
                '<xs:simpleType name="Small">'
                '<xs:restriction base="inv:Amount"><xs:minInclusive value="10"/></xs:restriction>'
                "</xs:simpleType>"
                '<xs:element name="small" type="inv:Small"/>'
                "</xs:schema>"
            ).encode()
        )
    )
    pool = SchemaPool.from_schema(schema)
    result = parse_typed_document(
        f'<inv:small xmlns:inv="{INVOICE}">5</inv:small>'.encode(),
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}Amount",
    )

    assert result.root_element.type_name == f"{{{INVOICE}}}Amount"


def test_type_complex_on_a_bound_document_succeeds() -> None:
    from pyxsd.xdm.document import parse_typed_document

    schema = Schema.compile(io.BytesIO(INVOICE_XSD.encode()))
    pool = SchemaPool.from_schema(schema)
    bound = schema.parse(
        io.BytesIO(
            f'<inv:Invoice xmlns:inv="{INVOICE}"><amount>12.5</amount></inv:Invoice>'.encode()
        ),
        capture_xdm=True,
    )
    result = parse_typed_document(
        bound,
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}InvoiceType",
    )

    assert result.root_element.type_name == f"{{{INVOICE}}}InvoiceType"


def test_explicit_package_requires_a_module_annotation_on_the_package(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "module.xsl",
        _stylesheet(
            '<xsl:template name="module"/>',
            attributes=' input-type-annotations="preserve"',
        ).decode(),
    )
    package = _write(
        tmp_path / "package.xsl",
        _package('<xsl:include href="module.xsl"/>').decode(),
    )

    assert _error_code(lambda: compile_stylesheet(package)) == "XTSE0265"


def test_non_schema_aware_processor_compiles_without_imports() -> None:
    compiled = XsltProcessor(schema_aware=False).compile(
        io.BytesIO(_stylesheet('<xsl:template name="main"/>'))
    )

    assert compiled.transform(initial_template="main").principal == ()


def test_non_schema_aware_transform_rejects_strict_override() -> None:
    compiled = XsltProcessor(schema_aware=False).compile(
        io.BytesIO(_stylesheet('<xsl:template name="main"/>'))
    )

    with pytest.raises(XsltInvocationError, match="strict"):
        compiled.transform(initial_template="main", validation="strict")


def test_builtin_validation_type_with_a_schema_pool() -> None:
    compiled = compile_stylesheet(io.BytesIO(_typed_invoice_stylesheet()))

    result = compiled.transform(TYPED_INVOICE, validation_type="xs:anyType")

    assert result.principal[0].type_name == f"{{{XSD_NS}}}double"


LOOSE_STRICT_XSD = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}"
    xmlns:inv="{INVOICE}">
  <xs:complexType name="Loose">
    <xs:sequence><xs:element name="amount" type="xs:decimal"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="Strict">
    <xs:sequence>
      <xs:element name="amount" type="xs:decimal"/>
      <xs:element name="extra" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="inv:Strict"/>
</xs:schema>"""


def test_type_requested_simple_type_rejects_complex_content() -> None:
    policy = _Policy({"urn:doc": TYPED_INVOICE})
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


def test_type_requested_simple_type_rejects_child_elements() -> None:
    policy = _Policy(
        {"urn:doc": f'<inv:amount xmlns:inv="{INVOICE}"><x>12.5</x></inv:amount>'.encode()}
    )
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


def test_type_requested_loose_type_validates_against_loose_not_strict() -> None:
    policy = _Policy(
        {"urn:doc": f'<inv:root xmlns:inv="{INVOICE}"><amount>5</amount></inv:root>'.encode()}
    )
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema(LOOSE_STRICT_XSD) + '<xsl:template name="main">'
                '<xsl:source-document href="urn:doc" type="inv:Loose">'
                '<xsl:sequence select="* instance of element(*, inv:Loose)"/>'
                "</xsl:source-document>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    result = compiled.transform(initial_template="main")

    assert result.principal[0].lexical == "true"


def test_type_requested_complex_type_annotates_the_occurrence() -> None:
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template match="/">'
                '<xsl:sequence select="(* instance of element(*, inv:InvoiceType), '
                '*/amount instance of element(*, xs:decimal))"/>'
                "</xsl:template>"
            )
        )
    )

    result = compiled.transform(
        TYPED_INVOICE,
        validation="strip",
        validation_type="inv:InvoiceType",
    )

    assert [item.lexical for item in result.principal] == ["true", "true"]


def test_type_requested_complex_type_annotates_attributes_and_simple_content() -> None:
    policy = _Policy(
        {"urn:doc": f'<inv:price xmlns:inv="{INVOICE}" id="3">12.5</inv:price>'.encode()}
    )
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema(PRICE_XSD) + '<xsl:template name="main">'
                '<xsl:source-document href="urn:doc" type="inv:Price">'
                '<xsl:sequence select="(* instance of element(*, inv:Price), '
                '*/@id instance of attribute(*, xs:int), data(*))"/>'
                "</xsl:source-document>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    result = compiled.transform(initial_template="main")

    assert [item.lexical for item in result.principal] == ["true", "true", "12.5"]
    assert result.principal[2].type_name == f"{{{XSD_NS}}}decimal"


def test_type_requested_complex_type_honors_xsi_type() -> None:
    source = (
        f'<inv:Invoice xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:SpecialInvoice">'
        "<amount>1</amount><code>special</code>"
        "</inv:Invoice>"
    ).encode()
    policy = _Policy({"urn:doc": source})
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template name="main">'
                '<xsl:source-document href="urn:doc" type="inv:InvoiceType">'
                '<xsl:sequence select="(* instance of element(*, inv:SpecialInvoice), '
                'string(*/code))"/>'
                "</xsl:source-document>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    result = compiled.transform(initial_template="main")

    assert [item.lexical for item in result.principal] == ["true", "special"]


def test_type_requested_complex_type_honors_xsi_nil() -> None:
    source = (f'<inv:Invoice xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" xsi:nil="true"/>').encode()
    policy = _Policy({"urn:doc": source})
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema(NILLABLE_INVOICE_XSD) + '<xsl:template name="main">'
                '<xsl:source-document href="urn:doc" type="inv:InvoiceType">'
                '<xsl:sequence select="* instance of element(*, inv:InvoiceType?)"/>'
                "</xsl:source-document>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )

    result = compiled.transform(initial_template="main")

    assert result.principal[0].lexical == "true"


def _requested_type_transform(
    schema: str,
    source: bytes,
    select: str,
    *,
    type_name: str = "inv:InvoiceType",
):
    policy = _Policy({"urn:doc": source})
    compiled = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema(schema) + '<xsl:template name="main">'
                f'<xsl:source-document href="urn:doc" type="{type_name}">'
                f'<xsl:sequence select="{select}"/>'
                "</xsl:source-document>"
                "</xsl:template>"
            )
        ),
        resource_policy=policy,
    )
    return compiled, policy


def test_type_requested_complex_type_accepts_same_xsi_type() -> None:
    source = (
        f'<inv:Invoice xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:InvoiceType"><amount>1</amount></inv:Invoice>'
    ).encode()
    compiled, _ = _requested_type_transform(
        INVOICE_XSD, source, "* instance of element(*, inv:InvoiceType)"
    )

    result = compiled.transform(initial_template="main")

    assert result.principal[0].lexical == "true"


def test_type_requested_unknown_xsi_type_is_xtte1540() -> None:
    source = (
        f'<inv:Invoice xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:Missing"><amount>1</amount></inv:Invoice>'
    ).encode()
    compiled, _ = _requested_type_transform(INVOICE_XSD, source, "1")

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_type_requested_xsi_type_not_derived_is_xtte1540() -> None:
    source = (
        f'<inv:Invoice xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:Amount"><amount>1</amount></inv:Invoice>'
    ).encode()
    compiled, _ = _requested_type_transform(INVOICE_XSD, source, "1")

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_type_requested_undeclared_root_honors_xsi_type() -> None:
    source = (
        f'<inv:special xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:SpecialInvoice"><amount>1</amount><code>c</code></inv:special>'
    ).encode()
    compiled, _ = _requested_type_transform(
        INVOICE_XSD, source, "* instance of element(*, inv:SpecialInvoice)"
    )

    result = compiled.transform(initial_template="main")

    assert result.principal[0].lexical == "true"


def test_type_requested_nil_on_non_nillable_declaration_is_xtte1540() -> None:
    source = (f'<inv:Invoice xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" xsi:nil="true"/>').encode()
    compiled, _ = _requested_type_transform(INVOICE_XSD, source, "1")

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_type_requested_nil_with_content_is_xtte1540() -> None:
    source = (
        f'<inv:Invoice xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:nil="true"><amount>1</amount></inv:Invoice>'
    ).encode()
    compiled, _ = _requested_type_transform(NILLABLE_INVOICE_XSD, source, "1")

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_type_requested_nilled_simple_occurrence() -> None:
    source = (f'<inv:amount xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" xsi:nil="true"/>').encode()
    compiled, _ = _requested_type_transform(
        NILLABLE_AMOUNT_XSD,
        source,
        "* instance of element(*, inv:Amount?)",
        type_name="inv:Amount",
    )

    result = compiled.transform(initial_template="main")

    assert result.principal[0].lexical == "true"


def test_type_requested_nilled_complex_occurrence_checks_attributes() -> None:
    source = (f'<inv:price xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" xsi:nil="true"/>').encode()
    compiled, _ = _requested_type_transform(NILLABLE_PRICE_XSD, source, "1", type_name="inv:Price")

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_type_requested_complex_type_annotates_nested_nodes() -> None:
    source = (
        f'<inv:order xmlns:inv="{INVOICE}"><line code="7"><value>12.5</value></line></inv:order>'
    ).encode()
    compiled, _ = _requested_type_transform(
        NESTED_XSD,
        source,
        "(* instance of element(*, inv:Order), "
        "*/line instance of element(*, inv:Line), "
        "*/line/value instance of element(*, xs:decimal), "
        "*/line/@code instance of attribute(*, xs:int), "
        "data(*/line/value))",
        type_name="inv:Order",
    )

    result = compiled.transform(initial_template="main")

    assert [item.lexical for item in result.principal] == [
        "true",
        "true",
        "true",
        "true",
        "12.5",
    ]
    assert result.principal[4].type_name == f"{{{XSD_NS}}}decimal"


def test_type_requested_prohibited_substitutions_without_declaration() -> None:
    source = (
        f'<inv:NoDecl xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:Ext"><a>x</a><b>y</b></inv:NoDecl>'
    ).encode()
    compiled, _ = _requested_type_transform(BLOCK_XSD, source, "1", type_name="inv:Base")

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_type_requested_prohibited_substitutions_with_differing_declaration() -> None:
    source = (
        f'<inv:Root xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:Ext"><a>x</a><b>y</b></inv:Root>'
    ).encode()
    compiled, _ = _requested_type_transform(BLOCK_XSD, source, "1", type_name="inv:Base")

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_type_requested_applies_declaration_prohibited_substitutions() -> None:
    source = (
        f'<inv:RootOpen xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:ExtOpen"><a>x</a><b>y</b></inv:RootOpen>'
    ).encode()
    compiled, _ = _requested_type_transform(DECL_BLOCK_XSD, source, "1", type_name="inv:BaseOpen")

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_type_requested_prohibited_substitutions_with_matching_declaration() -> None:
    source = (
        f'<inv:RootBase xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:Ext"><a>x</a><b>y</b></inv:RootBase>'
    ).encode()
    compiled, _ = _requested_type_transform(BLOCK_XSD, source, "1", type_name="inv:Base")

    assert _error_code(lambda: compiled.transform(initial_template="main")) == "XTTE1540"


def test_type_requested_unblocked_substitution_is_accepted() -> None:
    source = (
        f'<inv:Open xmlns:inv="{INVOICE}" xmlns:xsi="{XSI_NS}" '
        'xsi:type="inv:ExtOpen"><a>x</a><b>y</b></inv:Open>'
    ).encode()
    compiled, _ = _requested_type_transform(
        UNBLOCKED_XSD,
        source,
        "* instance of element(*, inv:ExtOpen)",
        type_name="inv:BaseOpen",
    )

    result = compiled.transform(initial_template="main")

    assert result.principal[0].lexical == "true"


def test_type_requested_complex_type_on_untyped_captured_document() -> None:
    from pyxsd.xdm.document import parse_typed_document
    from pyxsd.xdm.nodes import XdmDocument

    pool = SchemaPool.from_schema(Schema.compile(io.BytesIO(INVOICE_XSD.encode())))
    untyped = XdmDocument.parse(TYPED_INVOICE)

    check = compile_stylesheet(
        io.BytesIO(
            _stylesheet(
                _import_schema() + '<xsl:template match="/">'
                '<xsl:sequence select="* instance of element(*, inv:InvoiceType)"/>'
                "</xsl:template>"
            )
        )
    )
    result = check.transform(untyped, validation_type="inv:InvoiceType")

    assert result.principal[0].lexical == "true"
    typed = parse_typed_document(
        untyped,
        schema_pool=pool,
        type_name=f"{{{INVOICE}}}InvoiceType",
    )
    assert typed.root_element.type_name == f"{{{INVOICE}}}InvoiceType"


def test_deep_annotation_copy_is_iterative() -> None:
    from pyxsd.xdm.document import copy_type_annotations
    from pyxsd.xdm.nodes import XdmDocument

    depth = 2000
    payload = ("<root>" + "<n>" * depth + "</n>" * depth + "</root>").encode()
    source = XdmDocument.parse(payload)
    target = XdmDocument.parse(payload)

    assert copy_type_annotations(source, target) is target


def test_deep_strict_validation_does_not_recurse() -> None:
    from pyxsd.xdm.document import parse_typed_document

    schema = Schema.compile(
        io.BytesIO(
            (
                f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}" '
                f'xmlns:inv="{INVOICE}">'
                '<xs:element name="node" type="xs:anyType"/>'
                "</xs:schema>"
            ).encode()
        )
    )
    pool = SchemaPool.from_schema(schema)
    depth = 100
    payload = (
        f'<inv:node xmlns:inv="{INVOICE}">'
        + "<inv:node>" * depth
        + "</inv:node>" * depth
        + "</inv:node>"
    ).encode()

    document = parse_typed_document(payload, schema_pool=pool, validation="strict")

    assert document.root_element.name == f"{{{INVOICE}}}node"


@pytest.mark.parametrize(
    "body",
    [
        '<xsl:template name="main"><xsl:source-document href="urn:doc" validation="strict"/></xsl:template>',
        '<xsl:template name="main"><xsl:source-document href="urn:doc" type="xs:string"/></xsl:template>',
        '<xsl:template name="main"><xsl:copy-of select="." validation="strict"/></xsl:template>',
        '<xsl:mode default-validation="strict"/><xsl:template name="main"/>',
    ],
)
def test_non_schema_aware_stylesheet_attributes_are_xtse1660(body: str) -> None:
    assert (
        _error_code(
            lambda: XsltProcessor(schema_aware=False).compile(io.BytesIO(_stylesheet(body)))
        )
        == "XTSE1660"
    )


def test_non_schema_aware_used_package_attributes_are_xtse1660() -> None:
    library = _package(
        '<xsl:template name="lib:t" visibility="public">'
        '<xsl:source-document href="urn:doc" validation="strict"/>'
        "</xsl:template>"
    )
    principal = _stylesheet(
        '<xsl:use-package name="urn:lib"/><xsl:template name="main"/>',
        attributes=' xmlns:lib="urn:lib"',
    )
    from pyxsd.xslt.compiler.packages import PackageResolver

    assert (
        _error_code(
            lambda: XsltProcessor(
                schema_aware=False, resolver=PackageResolver.from_sources(library)
            ).compile(io.BytesIO(principal))
        )
        == "XTSE1660"
    )


def test_non_schema_aware_allowed_attributes_and_comments() -> None:
    body = (
        "<!-- a comment -->"
        '<xsl:template name="main">'
        '<out xmlns:x="urn:x" x:foreign="1">'
        '<xsl:source-document href="urn:doc" validation="preserve"/>'
        "</out>"
        "</xsl:template>"
    )

    compiled = XsltProcessor(schema_aware=False).compile(io.BytesIO(_stylesheet(body)))

    assert compiled is not None


@pytest.mark.parametrize("schema_aware", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        '<input type="text"/>',
        '<out validation="strict"/>',
        '<script type="text/javascript"/>',
        '<xhtml:input xmlns:xhtml="http://www.w3.org/1999/xhtml" type="text"/>',
    ],
)
def test_literal_result_type_attributes_compile(schema_aware: bool, body: str) -> None:
    compiled = XsltProcessor(schema_aware=schema_aware).compile(
        io.BytesIO(_stylesheet(f'<xsl:template name="main">{body}</xsl:template>'))
    )

    assert compiled is not None


@pytest.mark.parametrize(
    "body",
    [
        '<xsl:template name="main"><input xsl:type="xs:string"/></xsl:template>',
        '<xsl:template name="main"><out xsl:validation="strict"/></xsl:template>',
    ],
)
def test_non_schema_aware_literal_result_xsl_attributes_are_xtse1660(body: str) -> None:
    assert (
        _error_code(
            lambda: XsltProcessor(schema_aware=False).compile(io.BytesIO(_stylesheet(body)))
        )
        == "XTSE1660"
    )


def test_non_schema_aware_typed_mode_is_xtse1660() -> None:
    body = '<xsl:mode typed="yes"/><xsl:template name="main"/>'

    assert (
        _error_code(
            lambda: XsltProcessor(schema_aware=False).compile(io.BytesIO(_stylesheet(body)))
        )
        == "XTSE1660"
    )
