"""A supplied pool and a package's own imports resolve at compile time."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from pyxsd.schema import Schema
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
SUPPLIED = "urn:example:supplied"

_SUPPLIED_XSD = (
    f'<xs:schema xmlns:xs="{XSD_NS}" xmlns:sup="{SUPPLIED}" '
    f'targetNamespace="{SUPPLIED}" elementFormDefault="qualified">'
    '<xs:simpleType name="PartNumber"><xs:restriction base="xs:string"/></xs:simpleType>'
    '<xs:element name="code" type="xs:string"/>'
    '<xs:element name="doc" type="xs:string"/>'
    '<xs:attribute name="kind" type="xs:string"/>'
    "</xs:schema>"
)
_EXTRA_XSD = (
    f'<xs:schema xmlns:xs="{XSD_NS}" xmlns:sup="{SUPPLIED}" targetNamespace="{SUPPLIED}">'
    '<xs:simpleType name="Extra"><xs:restriction base="xs:string"/></xs:simpleType>'
    "</xs:schema>"
)
_IMPORTED_XSD = (
    f'<xs:schema xmlns:xs="{XSD_NS}" xmlns:sup="{SUPPLIED}" targetNamespace="{SUPPLIED}">'
    '<xs:simpleType name="Imported"><xs:restriction base="xs:string"/></xs:simpleType>'
    "</xs:schema>"
)


def _schema_file(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _pool(tmp_path: Path) -> SchemaPool:
    return SchemaPool(
        (Schema.compile(_schema_file(tmp_path, "supplied.xsd", _SUPPLIED_XSD), xsd_version="1.0"),),
        xsd_version="1.1",
        merge_namespaces=True,
    )


def _stylesheet(body: str) -> str:
    return (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:xs="{XSD_NS}" xmlns:sup="{SUPPLIED}">{body}</xsl:stylesheet>'
    )


def _compile(body: str, *, schema: SchemaPool | None = None, **options: object):
    processor = XsltProcessor(**options)
    return processor.compile(io.BytesIO(_stylesheet(body).encode()), schema=schema)


# -- caller pool -----------------------------------------------------------------


def test_supplied_type_on_a_global_variable_compiles_and_evaluates(tmp_path: Path) -> None:
    compiled = _compile(
        '<xsl:variable name="v" select="sup:PartNumber(&quot;123-AB&quot;)" as="sup:PartNumber"/>'
        '<xsl:template name="main"><out><xsl:value-of select="$v"/>'
        '<xsl:value-of select="$v instance of sup:PartNumber"/></out></xsl:template>',
        schema=_pool(tmp_path),
    )

    result = compiled.transform(initial_template="main")

    # Namespace declarations on the literal result element vary; assert the content.
    assert ">123-ABtrue</out>" in result.principal_entry.serialized


def test_supplied_type_on_a_function_parameter_and_result(tmp_path: Path) -> None:
    compiled = _compile(
        '<xsl:function name="sup:check" as="xs:boolean">'
        '<xsl:param name="p" as="sup:PartNumber"/>'
        '<xsl:sequence select="$p instance of sup:PartNumber"/></xsl:function>'
        '<xsl:template name="main">'
        '<xsl:sequence select="sup:check(sup:PartNumber(&quot;1&quot;))"/></xsl:template>',
        schema=_pool(tmp_path),
    )

    assert compiled.transform(initial_template="main").principal_entry.serialized.endswith("true")


def test_supplied_schema_element_and_attribute_declarations_compile(tmp_path: Path) -> None:
    compiled = _compile(
        '<xsl:variable name="e" as="schema-element(sup:doc)" select="()"/>'
        '<xsl:variable name="a" as="schema-attribute(sup:kind)" select="()"/>'
        '<xsl:template name="main"/>',
        schema=_pool(tmp_path),
    )

    assert compiled.schema_pool is not None


def test_unknown_supplied_type_stays_xpst0051(tmp_path: Path) -> None:
    with pytest.raises(XsltError) as caught:
        _compile(
            '<xsl:variable name="v" select="()" as="sup:NotThere"/><xsl:template name="main"/>',
            schema=_pool(tmp_path),
        )
    assert caught.value.code == "XPST0051"


def test_supplied_type_resolves_in_cast_and_treat_as(tmp_path: Path) -> None:
    compiled = _compile(
        '<xsl:template name="main"><out>'
        '<xsl:value-of select="(&quot;123-AB&quot; cast as sup:PartNumber)'
        ' instance of sup:PartNumber"/>'
        '<xsl:value-of select="(sup:PartNumber(&quot;123-AB&quot;) treat as sup:PartNumber)'
        ' instance of sup:PartNumber"/>'
        "</out></xsl:template>",
        schema=_pool(tmp_path),
    )

    serialized = compiled.transform(initial_template="main").principal_entry.serialized

    assert ">truetrue</out>" in serialized


def test_schema_element_pattern_matches_a_source_validated_against_the_supplied_pool(
    tmp_path: Path,
) -> None:
    # No root template and no explicit apply-templates: the built-in rules reach the
    # document element, which the engine evaluates correctly.  A stylesheet that also
    # applies templates to a typed node hangs in elementpath's tokenizer rebuild
    # (pre-existing, unrelated to this change; see Follow-ups).
    compiled = _compile(
        '<xsl:template match="schema-element(sup:doc)"><hit/></xsl:template>'
        '<xsl:template match="*"><nope/></xsl:template>',
        schema=_pool(tmp_path),
    )

    result = compiled.transform(
        io.BytesIO(f'<sup:doc xmlns:sup="{SUPPLIED}">x</sup:doc>'.encode()),
        validation="strict",
    )

    assert "<hit" in result.principal_entry.serialized
    assert "<nope" not in result.principal_entry.serialized


# -- the package's own imports ---------------------------------------------------


def test_import_schema_type_in_a_global_as_compiles(tmp_path: Path) -> None:
    _schema_file(tmp_path, "imported.xsd", _IMPORTED_XSD)
    stylesheet = _schema_file(
        tmp_path,
        "imported.xsl",
        _stylesheet(
            f'<xsl:import-schema namespace="{SUPPLIED}" schema-location="imported.xsd"/>'
            '<xsl:variable name="v" select="sup:Imported(&quot;x&quot;)" as="sup:Imported"/>'
            '<xsl:template name="main">'
            '<xsl:sequence select="$v instance of sup:Imported"/></xsl:template>'
        ),
    )

    compiled = XsltProcessor().compile(stylesheet)

    assert compiled.transform(initial_template="main").principal_entry.serialized.endswith("true")


def test_inline_import_schema_type_in_a_global_as_compiles() -> None:
    inline = (
        f'<xs:schema targetNamespace="{SUPPLIED}">'
        '<xs:simpleType name="Inline"><xs:restriction base="xs:string"/></xs:simpleType>'
        "</xs:schema>"
    )
    compiled = _compile(
        f"<xsl:import-schema>{inline}</xsl:import-schema>"
        '<xsl:variable name="v" select="sup:Inline(&quot;x&quot;)" as="sup:Inline"/>'
        '<xsl:template name="main">'
        '<xsl:sequence select="$v instance of sup:Inline"/></xsl:template>'
    )

    assert compiled.transform(initial_template="main").principal_entry.serialized.endswith("true")


# -- both sources in one package -------------------------------------------------


def test_supplied_pool_and_import_schema_both_reach_signatures(tmp_path: Path) -> None:
    _schema_file(tmp_path, "extra.xsd", _EXTRA_XSD)
    stylesheet = _schema_file(
        tmp_path,
        "both.xsl",
        _stylesheet(
            f'<xsl:import-schema namespace="{SUPPLIED}" schema-location="extra.xsd"/>'
            '<xsl:variable name="p" select="sup:PartNumber(&quot;1&quot;)" as="sup:PartNumber"/>'
            '<xsl:variable name="e" select="sup:Extra(&quot;o&quot;)" as="sup:Extra"/>'
            '<xsl:template name="main"><out>'
            '<xsl:value-of select="$p instance of sup:PartNumber"/>'
            '<xsl:value-of select="$e instance of sup:Extra"/></out></xsl:template>'
        ),
    )

    compiled = XsltProcessor().compile(stylesheet, schema=_pool(tmp_path))

    serialized = compiled.transform(initial_template="main").principal_entry.serialized

    assert ">truetrue</out>" in serialized


def test_supplied_pool_composes_with_a_stylesheet_schema_import(tmp_path: Path) -> None:
    # The caller pool supplies the declaration type; the import supplies a type used
    # only in an expression.  Both contributions must be usable in one stylesheet.
    _schema_file(tmp_path, "extra.xsd", _EXTRA_XSD)
    stylesheet = _schema_file(
        tmp_path,
        "composed.xsl",
        _stylesheet(
            f'<xsl:import-schema namespace="{SUPPLIED}" schema-location="extra.xsd"/>'
            '<xsl:variable name="v" select="sup:PartNumber(&quot;1&quot;)" as="sup:PartNumber"/>'
            '<xsl:template name="main"><out><xsl:value-of select="$v"/>'
            '<xsl:value-of select="sup:Extra(&quot;ok&quot;) instance of sup:Extra"/></out>'
            "</xsl:template>"
        ),
    )

    compiled = XsltProcessor().compile(stylesheet, schema=_pool(tmp_path))

    serialized = compiled.transform(initial_template="main").principal_entry.serialized

    assert ">1true</out>" in serialized


def test_supplied_pool_and_import_of_the_same_document_do_not_conflict(tmp_path: Path) -> None:
    _schema_file(tmp_path, "supplied.xsd", _SUPPLIED_XSD)
    stylesheet = _schema_file(
        tmp_path,
        "duplicate.xsl",
        _stylesheet(
            f'<xsl:import-schema namespace="{SUPPLIED}" schema-location="supplied.xsd"/>'
            '<xsl:variable name="v" select="sup:PartNumber(&quot;1&quot;)" as="sup:PartNumber"/>'
            '<xsl:template name="main"><xsl:sequence select="$v instance of sup:PartNumber"/>'
            "</xsl:template>"
        ),
    )

    compiled = XsltProcessor().compile(stylesheet, schema=_pool(tmp_path))

    assert compiled.transform(initial_template="main").principal_entry.serialized.endswith("true")


def test_supplied_pool_conflicting_with_an_imported_type_is_xtse0220(tmp_path: Path) -> None:
    _schema_file(
        tmp_path,
        "conflict.xsd",
        (
            f'<xs:schema xmlns:xs="{XSD_NS}" xmlns:sup="{SUPPLIED}" '
            f'targetNamespace="{SUPPLIED}">'
            '<xs:simpleType name="PartNumber"><xs:restriction base="xs:int"/>'
            "</xs:simpleType></xs:schema>"
        ),
    )
    stylesheet = _schema_file(
        tmp_path,
        "conflict.xsl",
        _stylesheet(
            f'<xsl:import-schema namespace="{SUPPLIED}" schema-location="conflict.xsd"/>'
            '<xsl:template name="main"/>'
        ),
    )

    with pytest.raises(XsltError) as caught:
        XsltProcessor().compile(stylesheet, schema=_pool(tmp_path))

    assert caught.value.code == "XTSE0220"
