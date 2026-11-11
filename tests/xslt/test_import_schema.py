"""``xsl:import-schema`` compilation, composition, and static errors.

Imported schemas compile through pyxsd under the processor's selected XSD
version and explicit resource policy.  The resulting ``SchemaPool`` is a
composition of those compiled schemas: named atomic types gain constructor
functions, global elements/attributes are available to ``schema-element``
tests, and conflicting declarations are rejected instead of silently picked
by name.  These tests are corpus-independent and use only public XSLT
behavior plus the compiled stylesheet's public ``schema_pool``.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
VC_NS = "http://www.w3.org/2007/XMLSchema-versioning"
INVOICE = "urn:example:invoice"
OTHER = "urn:example:other"


def _stylesheet(body: str, *, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:inv="{INVOICE}" xmlns:o="{OTHER}" version="3.0"{attributes}>'
        f"{body}</xsl:stylesheet>"
    ).encode()


def _compile(source: Any, **options: Any):
    return XsltProcessor(**options).compile(
        io.BytesIO(source) if isinstance(source, bytes) else source
    )


def _error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _inline(body: str, *, namespace: str = INVOICE) -> str:
    return (
        f'<xsl:import-schema><xs:schema targetNamespace="{namespace}">'
        f"{body}</xs:schema></xsl:import-schema>"
    )


AMOUNT_TYPE = '<xs:simpleType name="Amount"><xs:restriction base="xs:decimal"/></xs:simpleType>'


def test_inline_import_schema_exposes_named_constructor() -> None:
    compiled = _compile(
        _stylesheet(
            _inline(AMOUNT_TYPE) + '<xsl:template name="main">'
            "<xsl:sequence select=\"inv:Amount('12.5')\"/>"
            "</xsl:template>"
        )
    )

    result = compiled.transform(initial_template="main")

    assert compiled.schema_pool is not None
    assert compiled.schema_pool.xsd_version == "1.1"
    assert result.principal[0].type_name == f"{{{INVOICE}}}Amount"
    assert result.principal[0].lexical == "12.5"


def test_inline_schema_uses_stylesheet_namespace_bindings(tmp_path: Path) -> None:
    body = _inline(
        '<xs:simpleType name="Amount">'
        '<xs:restriction base="xs:decimal"/>'
        "</xs:simpleType>"
        '<xs:element name="Invoice" type="inv:Amount"/>'
    )
    compiled = _compile(
        _stylesheet(
            body
            + '<xsl:template name="main"><xsl:sequence select="inv:Amount(\'1\')"/></xsl:template>'
        )
    )

    assert compiled.schema_pool.get_type(f"{{{INVOICE}}}Amount") is not None
    assert compiled.schema_pool.get_element(f"{{{INVOICE}}}Invoice") is not None


def test_referenced_import_schema_and_relative_location(tmp_path: Path) -> None:
    schema = _write(
        tmp_path / "invoice.xsd",
        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}" '
        f'xmlns:inv="{INVOICE}">{AMOUNT_TYPE}</xs:schema>',
    )
    stylesheet = _write(
        tmp_path / "import.xsl",
        _stylesheet(
            f'<xsl:import-schema namespace="{INVOICE}" schema-location="invoice.xsd"/>'
            '<xsl:template name="main">'
            "<xsl:sequence select=\"inv:Amount('3.25')\"/>"
            "</xsl:template>"
        ).decode(),
    )

    compiled = _compile(stylesheet)
    result = compiled.transform(initial_template="main")

    assert schema.exists()
    assert result.principal[0].type_name == f"{{{INVOICE}}}Amount"
    assert result.principal[0].lexical == "3.25"


def test_inline_schema_namespace_conflict_is_static_error() -> None:
    body = (
        f'<xsl:import-schema namespace="{OTHER}">'
        f'<xs:schema targetNamespace="{INVOICE}"/>'
        "</xsl:import-schema>"
    )

    assert _error_code(lambda: _compile(_stylesheet(body))) == "XTSE0215"


def test_inline_schema_with_schema_location_is_static_error() -> None:
    body = (
        '<xsl:import-schema schema-location="invoice.xsd">'
        f'<xs:schema targetNamespace="{INVOICE}"/>'
        "</xsl:import-schema>"
    )

    assert _error_code(lambda: _compile(_stylesheet(body))) == "XTSE0215"


def test_import_schema_without_a_location_is_static_error() -> None:
    body = f'<xsl:import-schema namespace="{OTHER}"/>'

    assert _error_code(lambda: _compile(_stylesheet(body))) == "XTSE1650"


def test_missing_schema_document_is_static_error(tmp_path: Path) -> None:
    stylesheet = _write(
        tmp_path / "missing.xsl",
        _stylesheet(
            f'<xsl:import-schema namespace="{INVOICE}" '
            'schema-location="missing.xsd"/>'
            '<xsl:template name="main"/>'
        ).decode(),
    )

    assert _error_code(lambda: _compile(stylesheet)) == "XTSE1650"


def test_invalid_schema_is_static_error() -> None:
    body = _inline(
        '<xs:element name="root" type="xs:string"/><xs:element name="root" type="xs:int"/>'
    )

    assert _error_code(lambda: _compile(_stylesheet(body))) == "XTSE1660"


def test_same_namespace_imports_merge_distinct_components() -> None:
    body = (
        _inline('<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>')
        + _inline('<xs:simpleType name="B"><xs:restriction base="xs:int"/></xs:simpleType>')
        + '<xsl:template name="main">'
        "<xsl:sequence select=\"inv:A('x'), inv:B('1')\"/>"
        "</xsl:template>"
    )

    result = _compile(_stylesheet(body)).transform(initial_template="main")

    assert [item.type_name for item in result.principal] == [
        f"{{{INVOICE}}}A",
        f"{{{INVOICE}}}B",
    ]


def test_same_namespace_conflicting_components_are_static_error() -> None:
    body = (
        _inline('<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>')
        + _inline('<xs:simpleType name="A"><xs:restriction base="xs:int"/></xs:simpleType>')
        + '<xsl:template name="main"/>'
    )

    assert _error_code(lambda: _compile(_stylesheet(body))) == "XTSE0220"


def test_duplicate_same_document_import_is_deduplicated(tmp_path: Path) -> None:
    _write(
        tmp_path / "invoice.xsd",
        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}" '
        f'xmlns:inv="{INVOICE}">{AMOUNT_TYPE}</xs:schema>',
    )
    stylesheet = _write(
        tmp_path / "duplicate.xsl",
        _stylesheet(
            f'<xsl:import-schema namespace="{INVOICE}" schema-location="invoice.xsd"/>'
            f'<xsl:import-schema namespace="{INVOICE}" schema-location="invoice.xsd"/>'
            '<xsl:template name="main"><xsl:sequence select="inv:Amount(\'1\')"/></xsl:template>'
        ).decode(),
    )

    result = _compile(stylesheet).transform(initial_template="main")

    assert result.principal[0].type_name == f"{{{INVOICE}}}Amount"


def test_import_precedence_selects_the_principal_schema(tmp_path: Path) -> None:
    _write(
        tmp_path / "lower.xsl",
        _stylesheet(
            _inline('<xs:simpleType name="A"><xs:restriction base="xs:int"/></xs:simpleType>')
        ).decode(),
    )
    stylesheet = _write(
        tmp_path / "precedence.xsl",
        _stylesheet(
            '<xsl:import href="lower.xsl"/>'
            + _inline('<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>')
            + '<xsl:template name="main"><xsl:sequence select="inv:A(\'x\')"/></xsl:template>'
        ).decode(),
    )

    result = _compile(stylesheet).transform(initial_template="main")

    assert result.principal[0].lexical == "x"
    assert result.principal[0].type_name == f"{{{INVOICE}}}A"


def test_schema_import_in_imported_module_is_visible(tmp_path: Path) -> None:
    _write(
        tmp_path / "library.xsl",
        _stylesheet(
            _inline('<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>')
        ).decode(),
    )
    stylesheet = _write(
        tmp_path / "principal.xsl",
        _stylesheet(
            '<xsl:import href="library.xsl"/>'
            '<xsl:template name="main"><xsl:sequence select="inv:A(\'visible\')"/></xsl:template>'
        ).decode(),
    )

    result = _compile(stylesheet).transform(initial_template="main")

    assert result.principal[0].lexical == "visible"


def test_xsd_version_selects_conditional_components() -> None:
    body = (
        f'<xsl:import-schema><xs:schema targetNamespace="{INVOICE}" '
        f'xmlns:vc="{VC_NS}">'
        '<xs:simpleType name="Modern" vc:minVersion="1.1">'
        '<xs:restriction base="xs:string"/>'
        "</xs:simpleType>"
        "</xs:schema></xsl:import-schema>"
        '<xsl:template name="main">'
        "<xsl:sequence select=\"inv:Modern('ok')\"/>"
        "</xsl:template>"
    )

    compiled = _compile(_stylesheet(body), xsd_version="1.1")
    assert compiled.schema_pool.get_type(f"{{{INVOICE}}}Modern") is not None
    assert compiled.transform(initial_template="main").principal[0].lexical == "ok"

    assert _error_code(lambda: _compile(_stylesheet(body), xsd_version="1.0")) in {
        "XPST0017",
        "XPST0051",
    }


def test_anonymous_types_do_not_create_constructors() -> None:
    body = _inline(
        '<xs:element name="Invoice">'
        "<xs:complexType><xs:sequence>"
        '<xs:element name="amount">'
        '<xs:simpleType><xs:restriction base="xs:decimal"/></xs:simpleType>'
        "</xs:element>"
        "</xs:sequence></xs:complexType>"
        "</xs:element>"
    )

    compiled = _compile(_stylesheet(body))

    assert compiled.schema_pool.get_type(f"{{{INVOICE}}}Invoice") is None
    assert compiled.schema_pool.get_type(f"{{{INVOICE}}}amount") is None
    assert not any(
        name.endswith("}amount") or name.endswith("}Invoice")
        for name in compiled.schema_pool.constructors
    )


def test_imported_constructor_rejects_an_invalid_lexical_value() -> None:
    body = (
        _inline(AMOUNT_TYPE) + '<xsl:template name="main">'
        "<xsl:sequence select=\"inv:Amount('not-a-number')\"/>"
        "</xsl:template>"
    )

    assert _error_code(lambda: _compile(_stylesheet(body))) == "FORG0001"


def test_non_schema_aware_processor_rejects_import_schema() -> None:
    compiled = _stylesheet(_inline(AMOUNT_TYPE) + '<xsl:template name="main"/>')

    assert _error_code(lambda: _compile(compiled, schema_aware=False)) == "XTSE1650"


class _ReadOnlyPolicy:
    """A policy with only read(); schema compilation adapts its parse half."""

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
        try:
            content = self.mapping[source]
        except KeyError:
            raise ResourcePolicyError(f"denied {source!r}") from None
        return ResourceData(source, content)


def test_import_schema_honors_an_explicit_resource_policy() -> None:
    policy = _ReadOnlyPolicy(
        {
            "urn:example:invoice.xsd": (
                f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}" '
                f'xmlns:inv="{INVOICE}">{AMOUNT_TYPE}</xs:schema>'
            ).encode()
        }
    )
    compiled = _compile(
        _stylesheet(
            f'<xsl:import-schema namespace="{INVOICE}" '
            'schema-location="urn:example:invoice.xsd"/>'
            '<xsl:template name="main"><xsl:sequence select="inv:Amount(\'4\')"/></xsl:template>'
        ),
        resource_policy=policy,
    )

    result = compiled.transform(initial_template="main")

    assert result.principal[0].type_name == f"{{{INVOICE}}}Amount"
    assert ("urn:example:invoice.xsd", None, "schema") in policy.reads


def test_schema_pool_exposes_global_elements_and_attributes() -> None:
    body = _inline(
        AMOUNT_TYPE
        + '<xs:element name="Invoice" type="inv:Amount"/>'
        + '<xs:attribute name="currency" type="xs:string"/>'
    )

    compiled = _compile(_stylesheet(body))

    assert compiled.schema_pool.get_element(f"{{{INVOICE}}}Invoice") is not None
    assert compiled.schema_pool.get_attribute(f"{{{INVOICE}}}currency") is not None
    assert compiled.schema_pool.schema_for_element(f"{{{INVOICE}}}Invoice") is not None


def test_merged_namespace_proxy_exposes_both_schema_documents() -> None:
    body = (
        _inline(
            '<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>'
            '<xs:element name="left" type="inv:A"/>'
            '<xs:attribute name="leftAttr" type="xs:string"/>'
        )
        + _inline(
            '<xs:simpleType name="B"><xs:restriction base="xs:int"/></xs:simpleType>'
            '<xs:element name="right" type="inv:B"/>'
        )
        + '<xsl:template name="main"/>'
    )
    compiled = _compile(_stylesheet(body))
    pool = compiled.schema_pool
    proxy = pool.proxy(INVOICE)

    assert proxy is not None
    assert len(proxy.members) == 2
    assert proxy.target_namespace == INVOICE
    assert proxy.validity == "valid"
    assert proxy.validation_attempted == "full"
    assert proxy.is_fully_valid()
    assert proxy.xsd_version == "1.1"
    assert proxy.is_matching(INVOICE)
    assert not proxy.is_matching("urn:other")
    assert proxy.name == INVOICE
    assert proxy.tag == f"{{{XSD_NS}}}schema"
    assert dict(proxy.attrib) == {}
    assert proxy.text is None and proxy.tail is None
    assert proxy.maps is pool
    assert proxy.get("missing") is None
    assert len(proxy) == 2
    assert {element.name for element in proxy} == {
        f"{{{INVOICE}}}left",
        f"{{{INVOICE}}}right",
    }
    assert [element.name for element in proxy.iter(f"{{{INVOICE}}}left")] == [f"{{{INVOICE}}}left"]
    assert proxy.find(f"{{{INVOICE}}}left") is pool.get_element(f"{{{INVOICE}}}left")
    assert proxy.find(f"{{{INVOICE}}}missing") is None
    assert proxy.find(".") is proxy
    assert proxy.get_type(f"{{{INVOICE}}}A") is pool.get_type(f"{{{INVOICE}}}A")
    assert proxy.get_element(f"{{{INVOICE}}}right") is pool.get_element(f"{{{INVOICE}}}right")
    assert proxy.get_attribute(f"{{{INVOICE}}}leftAttr") is not None
    assert proxy.get_substitution_group(f"{{{INVOICE}}}missing") is None
    assert {item.name for item in proxy.iter_atomic_types()} == {
        f"{{{INVOICE}}}A",
        f"{{{INVOICE}}}B",
    }
    assert proxy.cast_as("1", f"{{{INVOICE}}}B") == 1
    assert proxy.is_instance("1", f"{{{INVOICE}}}B")
    assert proxy.expand_name("inv:A", {"inv": INVOICE}) == f"{{{INVOICE}}}A"
    assert proxy.expand_name(f"{{{INVOICE}}}A") == f"{{{INVOICE}}}A"
    assert "MergedSchemaProxy" in repr(proxy)

    context = proxy.get_context()
    assert context.schema is proxy
    assert context.root.value is proxy

    from elementpath import XPath2Parser

    parser = XPath2Parser(namespaces={"inv": INVOICE})
    proxy.bind_parser(parser)
    assert parser.schema is proxy
    assert "inv:A" in parser.symbol_table
    assert "inv:B" in parser.symbol_table
    assert proxy.get_type("inv:A") is pool.get_type(f"{{{INVOICE}}}A")
    assert proxy.get_element("inv:right") is pool.get_element(f"{{{INVOICE}}}right")
    assert proxy.get_attribute("inv:leftAttr") is not None


def test_merged_namespace_proxy_reports_invalid_members() -> None:
    from pyxsd.schema import Schema
    from pyxsd.xdm.schema import SchemaPool

    good = Schema.compile(
        io.BytesIO(f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}"/>'.encode())
    )
    bad = Schema.compile(
        io.BytesIO(
            (
                f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}">'
                '<xs:element name="dup" type="xs:string"/>'
                '<xs:element name="dup" type="xs:int"/>'
                "</xs:schema>"
            ).encode()
        )
    )
    pool = SchemaPool([good, bad], merge_namespaces=True)
    proxy = pool.proxy(INVOICE)

    assert proxy is not None
    assert proxy.validity == "invalid"
    assert proxy.validation_attempted == "partial"
    assert not proxy.is_fully_valid()


def test_pool_apis_expose_schema_groups_and_raw_schemas() -> None:
    body = (
        _inline('<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>')
        + _inline('<xs:simpleType name="B"><xs:restriction base="xs:int"/></xs:simpleType>')
        + '<xsl:template name="main"/>'
    )
    compiled = _compile(_stylesheet(body))
    pool = compiled.schema_pool

    assert len(pool.schema_groups[INVOICE]) == 2
    assert len(pool.raw_schemas) == 2
    assert pool.schema_for_type(f"{{{INVOICE}}}A") is not None
    assert pool.schema_for_type(f"{{{XSD_NS}}}string") is None
    assert pool.schema_for_type("urn:missing") is None
    assert pool.schema_for_element("urn:missing") is None


def test_duplicate_element_and_attribute_conflicts_are_static_errors() -> None:
    element_body = (
        _inline('<xs:element name="dup" type="xs:string"/>')
        + _inline('<xs:element name="dup" type="xs:int"/>')
        + '<xsl:template name="main"/>'
    )
    attribute_body = (
        _inline('<xs:attribute name="dup" type="xs:string"/>')
        + _inline('<xs:attribute name="dup" type="xs:int"/>')
        + '<xsl:template name="main"/>'
    )

    assert _error_code(lambda: _compile(_stylesheet(element_body))) == "XTSE0220"
    assert _error_code(lambda: _compile(_stylesheet(attribute_body))) == "XTSE0220"


def test_schema_pool_from_imports_deduplicates_the_same_document(tmp_path: Path) -> None:
    from pyxsd.xdm.schema import SchemaPool

    schema = _write(
        tmp_path / "same.xsd",
        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}">{AMOUNT_TYPE}</xs:schema>',
    )
    pool = SchemaPool.from_imports([schema, schema], merge_namespaces=True)

    assert len(pool.schema_groups[INVOICE]) == 1


def test_referenced_import_without_namespace_uses_the_schema_namespace(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "invoice.xsd",
        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}" '
        f'xmlns:inv="{INVOICE}">{AMOUNT_TYPE}</xs:schema>',
    )
    stylesheet = _write(
        tmp_path / "implicit.xsl",
        _stylesheet(
            '<xsl:import-schema schema-location="invoice.xsd"/>'
            '<xsl:template name="main"><xsl:sequence select="inv:Amount(\'2\')"/></xsl:template>'
        ).decode(),
    )

    result = _compile(stylesheet).transform(initial_template="main")

    assert result.principal[0].type_name == f"{{{INVOICE}}}Amount"


def test_schema_location_with_multiple_documents_composes_them(tmp_path: Path) -> None:
    _write(
        tmp_path / "left.xsd",
        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}">'
        '<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>'
        "</xs:schema>",
    )
    _write(
        tmp_path / "right.xsd",
        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}">'
        '<xs:simpleType name="B"><xs:restriction base="xs:int"/></xs:simpleType>'
        "</xs:schema>",
    )
    stylesheet = _write(
        tmp_path / "multi.xsl",
        _stylesheet(
            '<xsl:import-schema schema-location="left.xsd right.xsd"/>'
            '<xsl:template name="main">'
            "<xsl:sequence select=\"inv:A('a'), inv:B('1')\"/>"
            "</xsl:template>"
        ).decode(),
    )

    result = _compile(stylesheet).transform(initial_template="main")

    assert [item.type_name for item in result.principal] == [
        f"{{{INVOICE}}}A",
        f"{{{INVOICE}}}B",
    ]


def test_referenced_schema_target_namespace_mismatch_is_static_error(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "other.xsd",
        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{OTHER}"/>',
    )
    stylesheet = _write(
        tmp_path / "mismatch.xsl",
        _stylesheet(
            f'<xsl:import-schema namespace="{INVOICE}" '
            'schema-location="other.xsd"/>'
            '<xsl:template name="main"/>'
        ).decode(),
    )

    assert _error_code(lambda: _compile(stylesheet)) == "XTSE1660"


def test_multiple_inline_schemas_and_empty_namespace_are_static_errors() -> None:
    multiple = (
        "<xsl:import-schema>"
        '<xs:schema targetNamespace="urn:example:invoice"/>'
        '<xs:schema targetNamespace="urn:example:invoice"/>'
        "</xsl:import-schema>"
        '<xsl:template name="main"/>'
    )
    empty = '<xsl:import-schema namespace="  "/>'

    assert _error_code(lambda: _compile(_stylesheet(multiple))) == "XTSE0215"
    assert _error_code(lambda: _compile(_stylesheet(empty))) == "XTSE0215"


def test_import_schema_must_be_a_top_level_declaration() -> None:
    body = '<xsl:template name="main">' + _inline(AMOUNT_TYPE) + "</xsl:template>"

    assert _error_code(lambda: _compile(_stylesheet(body))) == "XTSE0010"


def test_merged_proxy_properties_and_error_arms() -> None:
    body = (
        _inline(
            '<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>'
            '<xs:complexType name="Complex"><xs:sequence/></xs:complexType>'
        )
        + _inline('<xs:simpleType name="B"><xs:restriction base="xs:int"/></xs:simpleType>')
        + '<xsl:template name="main"/>'
    )
    compiled = _compile(_stylesheet(body))
    proxy = compiled.schema_pool.proxy(INVOICE)

    assert proxy.schema is proxy
    assert proxy.pool is compiled.schema_pool
    assert proxy.base_element is None
    assert not proxy.is_assertion_based()
    assert {item.name for item in proxy.iter_atomic_types()} == {
        f"{{{INVOICE}}}A",
        f"{{{INVOICE}}}B",
    }
    assert proxy.expand_name("unprefixed") == "unprefixed"
    with pytest.raises(KeyError, match="unknown namespace prefix"):
        proxy.expand_name("missing:A")
    with pytest.raises(KeyError, match="Missing"):
        proxy.cast_as("x", f"{{{INVOICE}}}Missing")
    with pytest.raises(KeyError, match="Missing"):
        proxy.is_instance("x", f"{{{INVOICE}}}Missing")

    from elementpath import XPath2Parser

    parser = XPath2Parser(namespaces={})
    proxy.bind_parser(parser)
    assert parser.schema is proxy
    proxy.bind_parser(parser)
    assert parser.schema is proxy


def test_pool_identity_deduplication_and_raw_schemas() -> None:
    from pyxsd.schema import Schema
    from pyxsd.xdm.schema import SchemaPool

    schema = Schema.compile(
        io.BytesIO(
            (
                f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}">'
                f"{AMOUNT_TYPE}</xs:schema>"
            ).encode()
        )
    )
    pool = SchemaPool([schema, schema], merge_namespaces=True)

    assert len(pool.schema_groups[INVOICE]) == 1
    assert pool.raw_schemas == (schema,)
    assert pool.schema_for_element(f"{{{INVOICE}}}Amount") is None


def test_schema_compile_rejects_an_element_with_a_resource_policy() -> None:
    from xml.etree import ElementTree as ET

    from pyxsd.resource_policy import ResourcePolicyError
    from pyxsd.schema import Schema

    element = ET.fromstring(f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}"/>')

    with pytest.raises(ResourcePolicyError, match="Element"):
        Schema.compile(element, resource_policy=object())  # type: ignore[arg-type]


def test_substitution_group_conflict_is_static_error() -> None:
    left = _inline(
        '<xs:element name="head" type="xs:string" abstract="true"/>'
        '<xs:element name="one" type="xs:string" substitutionGroup="inv:head"/>'
    )
    right = _inline(
        '<xs:element name="head" type="xs:string" abstract="true"/>'
        '<xs:element name="two" type="xs:string" substitutionGroup="inv:head"/>'
    )

    assert _error_code(lambda: _compile(_stylesheet(left + right))) == "XTSE0220"


def test_diamond_type_and_attribute_registration_is_idempotent(tmp_path: Path) -> None:
    from pyxsd.xdm.schema import SchemaPool

    _write(
        tmp_path / "other.xsd",
        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{OTHER}">'
        '<xs:simpleType name="Shared"><xs:restriction base="xs:string"/></xs:simpleType>'
        '<xs:attribute name="sharedAttr" type="xs:string"/>'
        "</xs:schema>",
    )
    main = _write(
        tmp_path / "main.xsd",
        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}">'
        f'<xs:import namespace="{OTHER}" schemaLocation="other.xsd"/>'
        "</xs:schema>",
    )
    other = tmp_path / "other.xsd"
    pool = SchemaPool.from_imports([main, other])

    assert pool.get_type(f"{{{OTHER}}}Shared") is not None
    assert pool.get_attribute(f"{{{OTHER}}}sharedAttr") is not None


class _FullPolicy:
    """A policy that owns both halves of pyxsd's resource contract."""

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self.mapping = dict(mapping)

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        if not isinstance(source, str):
            content = source.read()
            if isinstance(content, str):
                content = content.encode("utf-8")
            return ResourceData(getattr(source, "name", "<memory>"), content)
        return ResourceData(source, self.mapping[source])

    def parse(self, data: ResourceData, context: Any) -> Any:
        from pyxsd.namespaces import parse_with_namespaces

        return parse_with_namespaces(io.BytesIO(data.content), context)


def test_import_schema_accepts_a_full_resource_policy() -> None:
    policy = _FullPolicy(
        {
            "urn:example:invoice.xsd": (
                f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}" '
                f'xmlns:inv="{INVOICE}">{AMOUNT_TYPE}</xs:schema>'
            ).encode()
        }
    )
    compiled = _compile(
        _stylesheet(
            f'<xsl:import-schema namespace="{INVOICE}" '
            'schema-location="urn:example:invoice.xsd"/>'
            '<xsl:template name="main"><xsl:sequence select="inv:Amount(\'1\')"/></xsl:template>'
        ),
        resource_policy=policy,
    )

    assert compiled.transform(initial_template="main").principal[0].type_name == (
        f"{{{INVOICE}}}Amount"
    )


def test_merged_bind_parser_is_idempotent_with_prefixes() -> None:
    body = (
        _inline('<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>')
        + _inline('<xs:simpleType name="B"><xs:restriction base="xs:int"/></xs:simpleType>')
        + '<xsl:template name="main"/>'
    )
    proxy = _compile(_stylesheet(body)).schema_pool.proxy(INVOICE)
    from elementpath import XPath2Parser

    parser = XPath2Parser(namespaces={"inv": INVOICE})
    proxy.bind_parser(parser)
    first = parser.symbol_table["inv:A"]
    proxy.bind_parser(parser)
    assert parser.symbol_table["inv:A"] is first


def test_schema_origin_and_precedence_helpers() -> None:
    from pyxsd.xdm.schema import _canonical_origin, _component_origin
    from pyxsd.xslt.compiler.packages import _schema_precedence_compare

    schema = object()
    assert _component_origin(schema, object()) == ("anonymous", id(schema))
    assert _canonical_origin("https://example.com/schema.xsd") == ("https://example.com/schema.xsd")
    assert _canonical_origin(1) == 1
    assert _schema_precedence_compare((), ()) == 0
    assert _schema_precedence_compare((), (1,)) == 1
    assert _schema_precedence_compare((1,), ()) == -1
    assert _schema_precedence_compare((2,), (1,)) == 1
    assert _schema_precedence_compare((1,), (2,)) == -1


def test_schema_resource_policy_requires_read() -> None:
    from pyxsd.xslt.compiler.packages import _schema_resource_policy

    assert _schema_resource_policy(None) is None
    with pytest.raises(TypeError, match="read"):
        _schema_resource_policy(object())


def test_package_closure_visits_a_shared_dependency_once() -> None:
    from types import SimpleNamespace

    from pyxsd.xslt.compiler.packages import _package_closure

    shared = SimpleNamespace(name="shared", version="1", dependencies=())
    root = SimpleNamespace(name="root", version="1", dependencies=(shared, shared))

    assert _package_closure(root) == (root, shared)


def test_inline_schema_compile_failure_maps_to_static_error(monkeypatch) -> None:
    from pyxsd.exceptions import PyXSDError
    from pyxsd.schema import Schema

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise PyXSDError("fatal schema input")

    monkeypatch.setattr(Schema, "compile", fail)

    assert _error_code(lambda: _compile(_stylesheet(_inline(AMOUNT_TYPE)))) == "XTSE1650"


def test_merge_schema_pools_rejects_version_mismatch_and_conflicts() -> None:
    from pyxsd.schema import Schema
    from pyxsd.xdm.schema import SchemaPool

    explicit = SchemaPool.from_schema(
        Schema.compile(
            io.BytesIO((f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{OTHER}"/>').encode())
        )
    )
    body = _stylesheet(
        _inline('<xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>')
    )

    with pytest.raises(ValueError, match="XSD versions"):
        XsltProcessor(xsd_version="1.0").compile(io.BytesIO(body), schema=explicit)

    conflicting = SchemaPool(
        [
            Schema.compile(
                io.BytesIO(
                    (
                        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{INVOICE}">'
                        '<xs:simpleType name="A"><xs:restriction base="xs:int"/></xs:simpleType>'
                        "</xs:schema>"
                    ).encode()
                )
            )
        ]
    )
    assert (
        _error_code(lambda: XsltProcessor().compile(io.BytesIO(body), schema=conflicting))
        == "XTSE0220"
    )


def test_invalid_package_validation_attributes_are_static_errors() -> None:
    annotations = _stylesheet(
        '<xsl:template name="main"/>',
        attributes=' input-type-annotations="bogus"',
    )
    validation = _stylesheet(
        '<xsl:template name="main"/>',
        attributes=' default-validation="strict"',
    )

    assert _error_code(lambda: _compile(annotations)) == "XTSE0020"
    assert _error_code(lambda: _compile(validation)) == "XTSE0020"


def test_package_validation_attribute_helpers() -> None:
    from types import SimpleNamespace

    from pyxsd.xslt.compiler.packages import (
        _package_default_validation,
        _package_input_type_annotations,
    )

    class Root:
        def __init__(self, value: str) -> None:
            self.value = value

        def attribute(self, name: str) -> str:
            del name
            return self.value

        def source_location(self) -> None:
            return None

    def descriptor(value: str) -> Any:
        module = SimpleNamespace(root=Root(value), identity=None)
        return SimpleNamespace(module=module)

    assert _package_input_type_annotations(descriptor("preserve")) == "preserve"
    assert _package_default_validation(descriptor("strip")) == "strip"
    with pytest.raises(XsltError) as caught:
        _package_input_type_annotations(descriptor("bogus"))
    assert caught.value.code == "XTSE0020"
    with pytest.raises(XsltError) as caught:
        _package_default_validation(descriptor("strict"))
    assert caught.value.code == "XTSE0020"
