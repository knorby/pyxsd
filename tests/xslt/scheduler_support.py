"""Named stylesheets and helpers shared by the scheduler test modules.

The scheduling contract and the deep mixed-recursion proof both run these
inline, corpus-independent stylesheets.  Test modules import the names they
use explicitly; nothing here is a test.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from functools import partial
from typing import Any

from tests.xslt.support import stylesheet as shared_stylesheet

from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt import CompiledStylesheet, ExecutionLimits, XsltProcessor

MY_NS = "urn:my"

my_stylesheet = partial(shared_stylesheet, namespaces={"my": MY_NS})


NAMED_TAIL = my_stylesheet(
    """
    <xsl:template name="count">
      <xsl:param name="n" select="0"/>
      <xsl:choose>
        <xsl:when test="$n = 0"><xsl:text>done</xsl:text></xsl:when>
        <xsl:otherwise>
          <xsl:call-template name="count">
            <xsl:with-param name="n" select="$n - 1"/>
          </xsl:call-template>
        </xsl:otherwise>
      </xsl:choose>
    </xsl:template>
    <xsl:template name="xsl:initial-template">
      <xsl:call-template name="count">
        <xsl:with-param name="n" select="10000"/>
      </xsl:call-template>
    </xsl:template>
    """
)

APPLY_TAIL = my_stylesheet(
    """
    <xsl:template name="xsl:initial-template">
      <xsl:apply-templates select="/*/a" mode="m">
        <xsl:with-param name="depth" select="10000" tunnel="yes"/>
      </xsl:apply-templates>
    </xsl:template>
    <xsl:template match="a" mode="m">
      <xsl:param name="depth" tunnel="yes" select="0"/>
      <xsl:choose>
        <xsl:when test="$depth = 0"><xsl:text>done</xsl:text></xsl:when>
        <xsl:otherwise>
          <xsl:apply-templates select="." mode="m">
            <xsl:with-param name="depth" select="$depth - 1" tunnel="yes"/>
          </xsl:apply-templates>
        </xsl:otherwise>
      </xsl:choose>
    </xsl:template>
    """
)

FUNCTION_TAIL = my_stylesheet(
    """
    <xsl:function name="my:count" as="xs:string">
      <xsl:param name="n" as="xs:integer"/>
      <xsl:sequence select="if ($n = 0) then 'done' else my:count($n - 1)"/>
    </xsl:function>
    <xsl:template name="xsl:initial-template">
      <xsl:sequence select="my:count(10000)"/>
    </xsl:template>
    """
)

HIGHER_ORDER = my_stylesheet(
    """
    <xsl:function name="my:next" as="xs:integer">
      <xsl:param name="n" as="xs:integer"/>
      <xsl:sequence select="$n + 1"/>
    </xsl:function>
    <xsl:function name="my:apply" as="xs:integer">
      <xsl:param name="f" as="function(*)"/>
      <xsl:param name="x" as="xs:integer"/>
      <xsl:sequence select="$f($x) + 1"/>
    </xsl:function>
    <xsl:template name="xsl:initial-template">
      <xsl:sequence
        select="(function($x) { my:next($x) })(40) + 1 + my:apply(function($v) { $v * 2 }, 20)"/>
    </xsl:template>
    """
)

NON_TAIL = my_stylesheet(
    """
    <xsl:function name="my:count" as="xs:integer">
      <xsl:param name="n" as="xs:integer"/>
      <xsl:sequence select="if ($n = 0) then 0 else my:count($n - 1) + 1"/>
    </xsl:function>
    <xsl:template name="xsl:initial-template">
      <xsl:sequence select="my:count(10000)"/>
    </xsl:template>
    """
)

MUTUAL = my_stylesheet(
    """
    <xsl:template name="even">
      <xsl:param name="n" select="0"/>
      <xsl:choose>
        <xsl:when test="$n = 0"><xsl:text>even</xsl:text></xsl:when>
        <xsl:otherwise>
          <xsl:call-template name="odd"><xsl:with-param name="n" select="$n - 1"/></xsl:call-template>
        </xsl:otherwise>
      </xsl:choose>
    </xsl:template>
    <xsl:template name="odd">
      <xsl:param name="n" select="0"/>
      <xsl:choose>
        <xsl:when test="$n = 0"><xsl:text>odd</xsl:text></xsl:when>
        <xsl:otherwise>
          <xsl:call-template name="even"><xsl:with-param name="n" select="$n - 1"/></xsl:call-template>
        </xsl:otherwise>
      </xsl:choose>
    </xsl:template>
    <xsl:template name="xsl:initial-template">
      <xsl:call-template name="even"><xsl:with-param name="n" select="10000"/></xsl:call-template>
    </xsl:template>
    """
)

RESULT_ORDER = my_stylesheet(
    """
    <xsl:template name="xsl:initial-template">
      <xsl:apply-templates select="/*/a">
        <xsl:with-param name="prefix" select="'item-'" tunnel="yes"/>
      </xsl:apply-templates>
    </xsl:template>
    <xsl:template match="a">
      <xsl:param name="prefix" tunnel="yes" select="''"/>
      <xsl:value-of select="concat($prefix, @n)"/>
    </xsl:template>
    """
)

TYPE_CONVERSION = my_stylesheet(
    """
    <xsl:function name="my:add" as="xs:integer">
      <xsl:param name="left" as="xs:integer"/>
      <xsl:param name="right" as="xs:integer"/>
      <xsl:sequence select="$left + $right"/>
    </xsl:function>
    <xsl:template name="xsl:initial-template">
      <xsl:sequence select="my:add('40', '1')"/>
    </xsl:template>
    """
)

CONTEXT_RESET = my_stylesheet(
    """
    <xsl:function name="my:context-name" as="xs:string">
      <xsl:sequence select="name(.)"/>
    </xsl:function>
    <xsl:template match="doc">
      <xsl:sequence select="my:context-name()"/>
    </xsl:template>
    """
)

TUNNEL = my_stylesheet(
    """
    <xsl:template name="outer">
      <xsl:param name="value" select="'outer'"/>
      <xsl:call-template name="inner">
        <xsl:with-param name="value" select="$value"/>
        <xsl:with-param name="tunnel-value" select="'tunnel'" tunnel="yes"/>
      </xsl:call-template>
    </xsl:template>
    <xsl:template name="inner">
      <xsl:param name="value" select="'missing'"/>
      <xsl:param name="tunnel-value" tunnel="yes" select="'no-tunnel'"/>
      <xsl:value-of select="concat($value, '-', $tunnel-value)"/>
    </xsl:template>
    <xsl:template name="xsl:initial-template">
      <xsl:call-template name="outer"/>
    </xsl:template>
    """
)

STYLESHEETS: Mapping[str, bytes] = {
    "named-tail-10000.xsl": NAMED_TAIL,
    "apply-tail-10000.xsl": APPLY_TAIL,
    "xpath-function-tail-10000.xsl": FUNCTION_TAIL,
    "xpath-higher-order-roundtrip.xsl": HIGHER_ORDER,
    "non-tail-exhaustion.xsl": NON_TAIL,
    "mutual-tail-10000.xsl": MUTUAL,
    "result-order.xsl": RESULT_ORDER,
    "type-conversion.xsl": TYPE_CONVERSION,
    "context-reset.xsl": CONTEXT_RESET,
    "tunnel-parameters.xsl": TUNNEL,
}


def compile_stylesheet(source: bytes) -> CompiledStylesheet:
    return XsltProcessor().compile(io.BytesIO(source))


def text(tree: XdmNode) -> str:
    return "".join(
        child.string_value
        for child in tree.children
        if child.kind in (NodeKind.TEXT, NodeKind.ELEMENT)
    ).strip()


def run_stylesheet(
    name: str,
    *,
    source: Any = None,
    limits: ExecutionLimits | None = None,
    initial_template: str | None = "xsl:initial-template",
) -> str:
    compiled = compile_stylesheet(STYLESHEETS[name])
    if source is None and name == "apply-tail-10000.xsl":
        source = b"<r><a/></r>"
    if source is None and name == "result-order.xsl":
        source = b"<r><a n='1'/><a n='2'/><a n='3'/></r>"
    if source is None and name == "context-reset.xsl":
        source = XdmDocument.parse(b"<doc/>")
    options: dict[str, Any] = {}
    if source is not None:
        options["source"] = source
    result = compiled.transform(
        **options,
        initial_template=initial_template,
        delivery="document",
        limits=limits,
    )
    return text(result.principal_tree)
