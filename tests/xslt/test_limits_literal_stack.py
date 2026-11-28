"""Stack exhaustion inside literal content reports the Python recursion bound.

Every excluded literal wrapper starts a child probe that recurses 160 levels
through a non-tail component call.  The error is coded and uncatchable, and the
message names the interpreter limit and the absent frame budget.
"""

from __future__ import annotations

import pytest
from tests.xslt.limits_support import assert_literal_python_stack
from tests.xslt.support import stylesheet


@pytest.mark.parametrize(
    ("wrapper", "attributes"),
    [
        ("<xsl:element name='part'>{call}</xsl:element>", ""),
        ("<xsl:document>{call}</xsl:document>", ""),
        ("<xsl:copy select='/*'>{call}</xsl:copy>", ""),
        ("<xsl:value-of>{call}</xsl:value-of>", ""),
        ("<xsl:attribute name='a'>{call}</xsl:attribute>", ""),
        ("<xsl:comment>{call}</xsl:comment>", ""),
        ("<xsl:processing-instruction name='p'>{call}</xsl:processing-instruction>", ""),
        ("<xsl:namespace name='p'>{call}</xsl:namespace>", ""),
        ("<xsl:variable name='v'>{call}</xsl:variable><xsl:sequence select='$v'/>", ""),
        ("<xsl:message>{call}</xsl:message>", ""),
        ("<xsl:where-populated>{call}</xsl:where-populated>", ""),
        ("<xsl:on-empty><xsl:sequence>{call}</xsl:sequence></xsl:on-empty>", ""),
        ("<xsl:on-non-empty><xsl:sequence>{call}</xsl:sequence></xsl:on-non-empty>text", ""),
        (
            "<xsl:analyze-string select=\"'a'\" regex='a'>"
            "<xsl:matching-substring>{call}</xsl:matching-substring>"
            "</xsl:analyze-string>",
            "",
        ),
        ("<xsl:perform-sort><xsl:sort select='1'/>{call}</xsl:perform-sort>", ""),
        ("<xsl:iterate select='1'><xsl:on-completion>{call}</xsl:on-completion></xsl:iterate>", ""),
        ("<xsl:iterate select='1'><xsl:break>{call}</xsl:break></xsl:iterate>", ""),
        ("<xsl:map><xsl:map-entry key='1'>{call}</xsl:map-entry></xsl:map>", ""),
        ("<xsl:map>{call}</xsl:map>", ""),
        ("<xsl:result-document href='{{concat(\"out-\", $n)}}'>{call}</xsl:result-document>", ""),
        ("<xsl:assert test='false()'>{call}</xsl:assert>", ""),
        ("<xsl:source-document href='{uri}'>{call}</xsl:source-document>", ""),
        (
            "<xsl:call-template name='sink'><xsl:with-param name='p'>{call}</xsl:with-param>"
            "</xsl:call-template>",
            "",
        ),
        (
            "<xsl:iterate select='1'><xsl:param name='p'>{call}</xsl:param>"
            "<xsl:sequence select='$p'/></xsl:iterate>",
            "",
        ),
        (
            "<xsl:iterate select='1'><xsl:param name='p' select='0'/>"
            "<xsl:next-iteration><xsl:with-param name='p'>{call}</xsl:with-param>"
            "</xsl:next-iteration></xsl:iterate>",
            "",
        ),
        (
            "<xsl:for-each select='(1, 2)'><xsl:sort>{call}</xsl:sort>"
            "<xsl:sequence select='.'/></xsl:for-each>",
            "",
        ),
        (
            "<xsl:merge><xsl:merge-source select='1'><xsl:merge-key>{call}</xsl:merge-key>"
            "</xsl:merge-source><xsl:merge-action/></xsl:merge>",
            "",
        ),
        (
            "<xsl:evaluate xpath=\"'$p'\"><xsl:with-param name='p'>{call}</xsl:with-param>"
            "</xsl:evaluate>",
            "",
        ),
        (
            "<xsl:merge><xsl:merge-source select='1'><xsl:merge-key select='.'/></xsl:merge-source>"
            "<xsl:merge-action><xsl:for-each select='1'><part>{call}</part></xsl:for-each>"
            "</xsl:merge-action></xsl:merge>",
            "",
        ),
    ],
    ids=[
        "element",
        "document",
        "copy",
        "value-of",
        "attribute",
        "comment",
        "processing-instruction",
        "namespace",
        "variable",
        "message",
        "where-populated",
        "on-empty",
        "on-non-empty",
        "analyze-string",
        "perform-sort",
        "on-completion",
        "break",
        "map-entry",
        "map",
        "result-document",
        "assert",
        "source-document",
        "with-param",
        "iterate-param",
        "next-iteration",
        "sort-key",
        "merge-key",
        "evaluate-param",
        "live-merge",
    ],
)
def test_literal_excluded_wrappers_report_python_stack(wrapper, attributes, tmp_path):
    document = tmp_path / "source.xml"
    document.write_bytes(b"<n/>")
    call = """<xsl:call-template name="loop">
      <xsl:with-param name="n" select="$n - 1"/>
    </xsl:call-template>"""
    body = f"""<xsl:template match="/">
      <xsl:call-template name="loop"><xsl:with-param name="n" select="160"/></xsl:call-template>
    </xsl:template>
    <xsl:template name="sink"><xsl:param name="p"/><xsl:sequence select="$p"/></xsl:template>
    <xsl:template name="loop">
      <xsl:param name="n"/>
      <xsl:if test="$n gt 0"><out>{wrapper.format(call=call, uri=document.as_uri())}</out></xsl:if>
    </xsl:template>"""
    assert_literal_python_stack(stylesheet(body, attributes=attributes))
