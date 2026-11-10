"""Variable and parameter scope, laziness, and lexical binding behavior."""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xpath.continuation import XPathEvaluationError
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError, XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"


def stylesheet(body: str, *, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="3.0"{attributes}>{body}</xsl:stylesheet>'
    ).encode()


def transform(
    source: bytes,
    input_source: bytes | None = None,
    *,
    compile_parameters: dict[str, Any] | None = None,
    delivery: str = "document",
    **options: Any,
):
    compiled = XsltProcessor().compile(io.BytesIO(source), static_parameters=compile_parameters)
    if input_source is None:
        return compiled.transform(delivery=delivery, **options)
    return compiled.transform(source=input_source, delivery=delivery, **options)


def element_children(node: Any) -> tuple[Any, ...]:
    return tuple(child for child in node.children if child.kind is NodeKind.ELEMENT)


def error_code(call: Callable[[], object]) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def test_global_static_and_local_variable_scope() -> None:
    source = stylesheet(
        """
        <xsl:variable name="g" select="'global'"/>
        <xsl:param name="s" static="yes" select="'static-default'"/>
        <xsl:template name="shadow">
          <xsl:variable name="g" select="'local'"/>
          <xsl:value-of select="$g"/>
        </xsl:template>
        <xsl:template name="main">
          <xsl:value-of select="$g"/><xsl:text>|</xsl:text>
          <xsl:value-of select="$s"/><xsl:text>|</xsl:text>
          <xsl:call-template name="shadow"/><xsl:text>|</xsl:text>
          <xsl:value-of select="$g"/>
        </xsl:template>
        """
    )
    result = transform(
        source,
        compile_parameters={"s": "static-value"},
        initial_template="main",
        parameters={"s": "runtime-value"},
    )
    assert result.principal_tree.string_value == "global|static-value|local|global"


def test_local_variable_value_forms_and_coercion() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="tree"><a>text</a></xsl:variable>
          <xsl:variable name="items" as="element()*"><b/><c/></xsl:variable>
          <xsl:variable name="typed" as="xs:integer" select="'9'"/>
          <xsl:variable name="plain"/>
          <xsl:sequence select="name($tree/*), count($items), $typed instance of xs:integer,
                                string-length($plain)"/>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main", delivery="raw")
    assert result.principal == (
        XdmAtomic.string("a"),
        XdmAtomic.integer(2),
        XdmAtomic.boolean(True),
        XdmAtomic.integer(0),
    )


def test_local_variable_scope_ends_with_its_sequence_constructor() -> None:
    scoped = stylesheet(
        """
        <xsl:template name="main">
          <out><xsl:variable name="x" select="1"/><xsl:value-of select="$x"/></out>
        </xsl:template>
        """
    )
    result = transform(scoped, initial_template="main")
    assert element_children(result.principal_tree)[0].string_value == "1"

    escaped = stylesheet(
        """
        <xsl:template name="main">
          <out><xsl:variable name="x" select="1"/><xsl:value-of select="$x"/></out>
          <xsl:value-of select="$x"/>
        </xsl:template>
        """
    )
    with pytest.raises(XPathEvaluationError) as caught:
        transform(escaped, initial_template="main")
    assert caught.value.code == "XPST0008"


def test_local_variables_are_visible_only_after_declaration() -> None:
    early = stylesheet(
        """
        <xsl:template name="main">
          <xsl:value-of select="$x"/>
          <xsl:variable name="x" select="1"/>
        </xsl:template>
        """
    )
    with pytest.raises(XPathEvaluationError) as caught:
        transform(early, initial_template="main")
    assert caught.value.code == "XPST0008"


def test_forward_global_references_are_lazy_and_circularity_is_reported() -> None:
    forward = stylesheet(
        """
        <xsl:variable name="a" select="$b + 1"/>
        <xsl:variable name="b" select="41"/>
        <xsl:template name="main"><xsl:sequence select="$a"/></xsl:template>
        """
    )
    assert transform(forward, initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(42),
    )

    circular = stylesheet(
        """
        <xsl:variable name="a" select="$b"/>
        <xsl:variable name="b" select="$a"/>
        <xsl:template name="main"><literal/></xsl:template>
        """
    )
    assert error_code(lambda: transform(circular, initial_template="main")) == "XTDE0640"


def test_variable_select_and_content_are_mutually_exclusive() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="x" select="1"><a/></xsl:variable>
        </xsl:template>
        """
    )
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor().compile(io.BytesIO(source))
    assert caught.value.code == "XTSE0620"

    missing_name = stylesheet('<xsl:template name="main"><xsl:variable select="1"/></xsl:template>')
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor().compile(io.BytesIO(missing_name))
    assert caught.value.code == "XTSE0010"


def test_tunnel_parameters_flow_through_builtin_rules() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:apply-templates select="/*">
            <xsl:with-param name="t" select="'tunnel'" tunnel="yes"/>
          </xsl:apply-templates>
        </xsl:template>
        <xsl:template match="doc/item">
          <xsl:param name="t" tunnel="yes" select="'missing'"/>
          <xsl:value-of select="$t"/>
        </xsl:template>
        """
    )
    result = transform(source, b"<doc><item/></doc>", initial_template="main")
    assert result.principal_tree.string_value == "tunnel"


def test_non_tunnel_parameters_do_not_cross_intermediate_named_templates() -> None:
    source = stylesheet(
        """
        <xsl:template name="outer">
          <xsl:param name="p" select="'outer'"/>
          <xsl:call-template name="middle">
            <xsl:with-param name="p" select="$p"/>
          </xsl:call-template>
        </xsl:template>
        <xsl:template name="middle">
          <xsl:call-template name="inner"/>
        </xsl:template>
        <xsl:template name="inner">
          <xsl:param name="p" select="'default'"/>
          <xsl:value-of select="$p"/>
        </xsl:template>
        <xsl:template name="main"><xsl:call-template name="outer"/></xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert result.principal_tree.string_value == "default"


def test_next_match_and_apply_imports_accept_lexical_and_tunnel_parameters() -> None:
    source = stylesheet(
        """
        <xsl:template match="item" priority="2">
          <xsl:param name="p" select="'top'"/>
          <xsl:value-of select="$p"/>
          <xsl:next-match>
            <xsl:with-param name="p" select="'next'"/>
            <xsl:with-param name="t" select="'tunnel'" tunnel="yes"/>
          </xsl:next-match>
        </xsl:template>
        <xsl:template match="item" priority="1">
          <xsl:param name="p" select="'missing'"/>
          <xsl:param name="t" tunnel="yes" select="'missing'"/>
          <xsl:value-of select="$p"/><xsl:text>:</xsl:text>
          <xsl:value-of select="$t"/>
        </xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    result = transform(source, b"<doc><item/></doc>")
    assert result.principal_tree.string_value == "topnext:tunnel"


def test_parameter_after_content_inside_construction_is_reported() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <out><literal/><xsl:param name="p"/></out>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(source, initial_template="main")) == "XTDE0000"


def test_nested_sequence_without_select_is_evaluated_in_construction() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <out><xsl:sequence><xsl:sequence select="1"/></xsl:sequence></out>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert element_children(result.principal_tree)[0].string_value == "1"


def test_required_parameter_without_a_value_is_reported() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:param name="required" required="yes"/>
          <xsl:value-of select="$required"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(source, initial_template="main")) == "XTDE0700"
