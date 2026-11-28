"""Literal construction and TVT depth, wrappers, and frame budgets.

These stylesheets recurse through literal result content, temporary value
templates, and wrapped component calls.  Deep chains run at 10 or 160 levels
and report a coded frame or step budget instead of ``RecursionError``.
"""

from __future__ import annotations

import io
import sys

import pytest
from tests.xslt.limits_support import assert_exhausted, deep_stylesheet
from tests.xslt.support import stylesheet

from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.builder import ResultBuilder
from pyxsd.xslt.runtime.frames import ExecutionLimits


def _tvt_stylesheet(instruction, depth):
    body = f"""<xsl:function name="local:f" as="item()+">
      <xsl:param name="n" as="xs:integer"/>
      <xsl:{instruction} expand-text="yes">{{if ($n = 0) then 'end' else local:f($n - 1)}}</xsl:{instruction}>
    </xsl:function>
    <xsl:template match="/"><xsl:sequence select="local:f({depth})"/></xsl:template>"""
    return XsltProcessor().compile(io.BytesIO(stylesheet(body, namespaces={"local": "urn:local"})))


@pytest.mark.parametrize("instruction", ["text", "sequence"])
def test_recursive_tvt_completes_without_frame_budget(instruction):
    result = _tvt_stylesheet(instruction, 160).transform(
        b"<n/>", delivery="raw", limits=ExecutionLimits(max_frames=None)
    )
    assert tuple(node.string_value for node in result.principal) == ("end",)


@pytest.mark.parametrize("instruction", ["text", "sequence"])
def test_recursive_tvt_output_matches_at_both_depths(instruction):
    limits = ExecutionLimits(max_frames=None)
    shallow = _tvt_stylesheet(instruction, 10).transform(b"<n/>", limits=limits, delivery="raw")
    deep = _tvt_stylesheet(instruction, 160).transform(b"<n/>", limits=limits, delivery="raw")
    assert (
        tuple(node.string_value for node in deep.principal)
        == tuple(node.string_value for node in shallow.principal)
        == ("end",)
    )


@pytest.mark.parametrize("instruction", ["text", "sequence"])
def test_recursive_tvt_stops_at_the_frame_budget(instruction):
    with pytest.raises(XsltDynamicError) as caught:
        _tvt_stylesheet(instruction, 160).transform(
            b"<n/>", delivery="raw", limits=ExecutionLimits(max_frames=8)
        )
    assert_exhausted(caught.value, "frame", "max_frames", 8, 9)


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ('<xsl:text expand-text="yes">a{1 + 2}b</xsl:text>', ("a3b",)),
        ('<xsl:sequence select="(1, 2)"/>', ("1", "2")),
        ('<xsl:sequence select="if (1 eq 1) then () else (1, 2)"/>', ()),
        ('<out><xsl:sequence select="(1, 2)"/></out>', ("1 2",)),
        ('<out><xsl:choose><xsl:when test="true()">a</xsl:when></xsl:choose></out>', ("a",)),
    ],
)
def test_no_additional_steps_without_component_calls(instruction, expected):
    compiled = XsltProcessor().compile(
        io.BytesIO(stylesheet(f'<xsl:template match="/">{instruction}</xsl:template>'))
    )
    result = compiled.transform(b"<n/>", delivery="raw")
    assert tuple(item.string_value for item in result.principal) == expected
    assert result._budget.steps == 4


def test_scheduled_tvt_atomizes_arrays_and_resumes_later_parts():
    body = """<xsl:function name="local:f"><xsl:sequence select="[1, 2]"/></xsl:function>
    <xsl:template match="/">
      <xsl:text expand-text="yes">a{local:f()}b{3 + 4}c{local:f()}d</xsl:text>
    </xsl:template>"""
    compiled = XsltProcessor().compile(
        io.BytesIO(stylesheet(body, namespaces={"local": "urn:local"}))
    )
    result = compiled.transform(b"<n/>", delivery="raw")
    assert result.principal[0].string_value == "a1 2b7c1 2d"


def test_tvt_preserves_legacy_evaluation_for_nonschedulable_calls():
    body = """<xsl:variable name="global" select="'g'"/>
    <xsl:function name="local:f">
      <xsl:param name="item"/>
      <xsl:sequence select="$item"/>
    </xsl:function>
    <xsl:function name="local:cached" cache="yes">
      <xsl:message>run</xsl:message>
      <xsl:sequence select="'c'"/>
    </xsl:function>
    <xsl:template match="/">
      <xsl:variable name="node" as="element()"><n>n</n></xsl:variable>
      <xsl:variable name="empty" select="()"/>
      <xsl:text expand-text="yes">{if (true()) then local:f('t') else 'x'}{
        if (local:f(true())) then local:f('s') else 'x'}{
        local:f(count((1, 2)))}{local:f($global)}{local:f($node)}{
        if ($empty) then local:f('x') else 'e'}{local:cached()}{local:cached()}</xsl:text>
    </xsl:template>"""
    compiled = XsltProcessor().compile(
        io.BytesIO(stylesheet(body, namespaces={"local": "urn:local"}))
    )
    result = compiled.transform(b"<n/>", delivery="raw")
    assert result.principal[0].string_value == "ts2gnecc"
    assert len(result.messages) == 1


@pytest.mark.parametrize("shape", ["push", "named-template"])
def test_recursive_literal_completes_without_frame_budget(shape):
    compiled, source = deep_stylesheet(shape)
    result = compiled.transform(source, delivery="raw", limits=ExecutionLimits(max_frames=None))
    (node,) = result.principal
    for _ in range(159):
        assert node.name == "out"
        (node,) = node.children
    assert node.name == "out"
    assert not node.children


@pytest.mark.parametrize("shape", ["push", "named-template"])
def test_recursive_literal_output_matches_at_both_depths(shape):
    if shape == "push":
        entry = "<xsl:apply-templates/>"
        component = """<xsl:template match="n">
          <out><xsl:apply-templates select="n"/></out>
        </xsl:template>"""
    else:
        entry = """<xsl:call-template name="nested">
          <xsl:with-param name="n" select="$depth"/>
        </xsl:call-template>"""
        component = """<xsl:template name="nested">
          <xsl:param name="n"/>
          <out><xsl:if test="$n gt 1"><xsl:call-template name="nested">
            <xsl:with-param name="n" select="$n - 1"/>
          </xsl:call-template></xsl:if></out>
        </xsl:template>"""
    body = f"""<xsl:param name="depth"/>{component}
    <xsl:template match="/">
      <xsl:variable name="result" as="element()">{entry}</xsl:variable>
      <xsl:sequence select="count($result/descendant-or-self::out) eq $depth"/>
    </xsl:template>"""
    compiled = XsltProcessor().compile(io.BytesIO(stylesheet(body)))
    for depth in (10, 160):
        result = compiled.transform(
            b"<n>" * depth + b"</n>" * depth,
            parameters={"depth": depth},
            delivery="raw",
            limits=ExecutionLimits(max_frames=None),
        )
        assert result.principal[0].string_value == "true"


@pytest.mark.parametrize("shape", ["push", "named-template"])
def test_recursive_literal_stops_at_the_frame_budget(shape):
    compiled, source = deep_stylesheet(shape)
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source, delivery="raw", limits=ExecutionLimits(max_frames=8))
    assert_exhausted(caught.value, "frame", "max_frames", 8, 9)


def test_literal_dispatch_preserves_order_and_lexical_scope():
    body = """<xsl:function name="local:items"><xsl:sequence select="[1, 2]"/></xsl:function>
    <xsl:template name="leaf"><xsl:text>leaf</xsl:text></xsl:template>
    <xsl:template match="/">
      <xsl:variable name="v" select="'outer'"/>
      <out>
        <xsl:choose>
          <xsl:when test="false()">other</xsl:when>
          <xsl:otherwise>
            <nested><xsl:sequence>
              <xsl:variable name="v" select="'inner'"/>
              <xsl:text expand-text="yes">{$v}:</xsl:text>
              <xsl:sequence select="$v"/>
              <xsl:call-template name="leaf"/>
              <xsl:sequence select="local:items()"/>
            </xsl:sequence></nested>
          </xsl:otherwise>
        </xsl:choose>
        <xsl:choose><xsl:when test="true()">:</xsl:when></xsl:choose>
        <xsl:sequence select="$v"/>
      </out>
    </xsl:template>"""
    compiled = XsltProcessor().compile(
        io.BytesIO(stylesheet(body, namespaces={"local": "urn:local"}))
    )
    result = compiled.transform(b"<n/>", delivery="raw")
    assert result.principal[0].string_value == "inner:innerleaf1 2:outer"
    assert result.principal[0].children[0].name == "nested"


def test_copy_preserves_a_subtree_deeper_than_the_python_stack():
    depth = sys.getrecursionlimit() + 50
    document = XdmDocument.parse(b'<n a="v">' * depth + b"end" + b"</n>" * depth)
    source = document.root_element
    copy = ResultBuilder.temporary().clone(source)
    assert copy.document is not document
    parent = None
    for _ in range(depth):
        assert copy is not source and copy.parent is parent and copy.name == source.name
        assert copy.attributes[0].string_value == "v"
        parent = copy
        copy, source = copy.children[0], source.children[0]
    assert copy.string_value == source.string_value == "end"


@pytest.mark.parametrize(
    ("wrapper", "expected"),
    [
        ('<xsl:if test="true()">{call}</xsl:if>', "leaf"),
        (
            '<xsl:for-each select="2"><xsl:if test=". eq 2 and position() eq last()">'
            "{call}</xsl:if></xsl:for-each>",
            "leaf",
        ),
        (
            '<xsl:for-each-group select="2" group-by=".">'
            '<xsl:if test="current-grouping-key() eq 2 and current-group() eq 2">'
            "{call}</xsl:if></xsl:for-each-group>",
            "leaf",
        ),
        (
            '<xsl:iterate select="2"><xsl:param name="p" select="3"/>'
            '<xsl:if test=". + $p eq 5">{call}</xsl:if></xsl:iterate>',
            "leaf",
        ),
        ('<xsl:try>{call}<xsl:catch errors="*">unexpected</xsl:catch></xsl:try>', "leaf"),
        (
            '<xsl:try><xsl:sequence select="error()"/>'
            '<xsl:catch errors="err:FOER0000">{call}</xsl:catch></xsl:try>',
            "leaf",
        ),
        ("<xsl:fork><xsl:sequence>{call}</xsl:sequence></xsl:fork>", "leaf"),
        ("<ext:unavailable><xsl:fallback>{call}</xsl:fallback></ext:unavailable>", "leaf"),
    ],
    ids=["if", "for-each", "group", "iterate", "try", "catch", "fork", "fallback"],
)
def test_literal_wrappers_resume_deep_calls(wrapper, expected):
    call = """<xsl:call-template name="loop">
      <xsl:with-param name="n" select="$n - 1"/>
    </xsl:call-template>"""
    body = f"""<xsl:param name="depth"/>
    <xsl:template match="/">
      <xsl:call-template name="loop"><xsl:with-param name="n" select="$depth"/></xsl:call-template>
    </xsl:template>
    <xsl:template name="loop">
      <xsl:param name="n"/>
      <xsl:choose>
        <xsl:when test="$n gt 0"><out>{wrapper.format(call=call)}</out></xsl:when>
        <xsl:otherwise><xsl:text>leaf</xsl:text></xsl:otherwise>
      </xsl:choose>
    </xsl:template>"""
    compiled = XsltProcessor().compile(
        io.BytesIO(
            stylesheet(
                body,
                namespaces={"err": "http://www.w3.org/2005/xqt-errors", "ext": "urn:ext"},
                attributes=' extension-element-prefixes="ext"',
            )
        )
    )
    for depth in (10, 160):
        result = compiled.transform(
            b"<n/>",
            parameters={"depth": depth},
            delivery="raw",
            limits=ExecutionLimits(max_frames=None),
        )
        (node,) = result.principal
        for _ in range(depth - 1):
            assert node.name == "out"
            (node,) = node.children
        assert node.name == "out"
        assert node.string_value == expected
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(
            b"<n/>",
            parameters={"depth": 160},
            delivery="raw",
            limits=ExecutionLimits(max_frames=8),
        )
    assert_exhausted(caught.value, "frame", "max_frames", 8, 9)


def test_literal_try_catches_the_same_error_at_depth():
    body = """<xsl:param name="depth"/>
    <xsl:template match="/">
      <xsl:call-template name="loop"><xsl:with-param name="n" select="$depth"/></xsl:call-template>
    </xsl:template>
    <xsl:template name="loop">
      <xsl:param name="n"/>
      <out><xsl:try>
        <xsl:choose>
          <xsl:when test="$n eq 1">
            <xsl:message terminate="yes">stop</xsl:message>
          </xsl:when>
          <xsl:otherwise>
            <xsl:call-template name="loop">
              <xsl:with-param name="n" select="$n - 1"/>
            </xsl:call-template>
          </xsl:otherwise>
        </xsl:choose>
        <xsl:catch errors="err:XTMM9000">
          <xsl:sequence select="($err:code, $err:module,
            count($err:line-number), count($err:column-number))"/>
        </xsl:catch>
      </xsl:try></out>
    </xsl:template>"""
    source = stylesheet(body, namespaces={"err": "http://www.w3.org/2005/xqt-errors"})
    compiled = XsltProcessor().compile(io.BytesIO(source))
    for depth in (1, 160):
        result = compiled.transform(
            b"<n/>",
            parameters={"depth": depth},
            delivery="raw",
            limits=ExecutionLimits(max_frames=None),
        )
        assert result.principal[0].string_value == "XTMM9000 <memory> 0 0"
        assert len(result.messages) == 1


def test_literal_next_match_resumes_at_depth():
    body = """<xsl:template match="/">
      <xsl:apply-templates/>
    </xsl:template>
    <xsl:template match="n" priority="1">
      <out><xsl:next-match/></out>
    </xsl:template>
    <xsl:template match="n">
      <xsl:choose>
        <xsl:when test="n"><xsl:apply-templates select="n"/></xsl:when>
        <xsl:otherwise>leaf</xsl:otherwise>
      </xsl:choose>
    </xsl:template>"""
    compiled = XsltProcessor().compile(io.BytesIO(stylesheet(body)))
    for depth in (10, 160):
        source = b"<n>" * depth + b"</n>" * depth
        result = compiled.transform(source, delivery="raw", limits=ExecutionLimits(max_frames=None))
        (node,) = result.principal
        for _ in range(depth - 1):
            assert node.name == "out"
            (node,) = node.children
        assert node.name == "out"
        assert node.string_value == "leaf"
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source, delivery="raw", limits=ExecutionLimits(max_frames=8))
    assert_exhausted(caught.value, "frame", "max_frames", 8, 9)
