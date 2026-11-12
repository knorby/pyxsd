"""Dynamic evaluation state across nested grouping, regex, and error scopes."""

from __future__ import annotations

import dataclasses
import io
from types import MappingProxyType
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic
from pyxsd.xpath.environment import Focus
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.runtime.context import EvaluationContext
from pyxsd.xslt.runtime.frames import Frame

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"
TEST_NS = "urn:test"


def stylesheet(body: str) -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" xmlns:t="{TEST_NS}" version="3.0">{body}</xsl:stylesheet>'
    ).encode()


def transform(body: str, source: bytes) -> Any:
    compiled = XsltProcessor().compile(io.BytesIO(stylesheet(body)))
    return compiled.transform(
        source=source,
        initial_template="main",
        delivery="raw",
    )


def test_evaluation_context_preserves_nested_state() -> None:
    """A caught error restores group, regex, and lexical state exactly.

    The named template runs while a group and a regex match are active, calls
    an XSLT function from XPath, emits speculative output, raises a catchable
    dynamic error, and then reads both dynamic states again from the catch and
    from the code after the try.
    """
    result = transform(
        """
        <xsl:function name="my:tag" as="xs:string">
          <xsl:param name="text" as="xs:string"/>
          <xsl:sequence select="concat('[', $text, ']')"/>
        </xsl:function>
        <xsl:template name="probe">
          <xsl:param name="label" as="xs:string"/>
          <xsl:sequence
              select="my:tag(concat($label, ':', current-group()/@k, ':', regex-group(1)))"/>
          <xsl:try>
            <xsl:sequence select="my:tag('speculative')"/>
            <xsl:sequence select="error(QName('urn:test', 'boom'))"/>
            <xsl:catch>
              <xsl:sequence
                  select="concat('caught=', my:tag(concat(current-group()/@k, ':', regex-group(1))))"/>
            </xsl:catch>
          </xsl:try>
          <xsl:sequence
              select="concat('after=', my:tag(concat(current-group()/@k, ':', regex-group(1))))"/>
        </xsl:template>
        <xsl:template name="main">
          <xsl:for-each-group select="/*/i" group-by="@k">
            <xsl:analyze-string select="'ab'" regex="(a)(b)">
              <xsl:matching-substring>
                <xsl:call-template name="probe">
                  <xsl:with-param name="label" select="'p'"/>
                </xsl:call-template>
              </xsl:matching-substring>
            </xsl:analyze-string>
          </xsl:for-each-group>
        </xsl:template>
        """,
        b'<r><i k="a"/><i k="b"/></r>',
    )

    assert [item.lexical for item in result.principal] == [
        "[p:a:a]",
        "caught=[a:a]",
        "after=[a:a]",
        "[p:b:a]",
        "caught=[b:a]",
        "after=[b:a]",
    ]


def test_evaluation_context_defaults_and_replacement() -> None:
    context = EvaluationContext()

    assert context.focus.is_absent
    assert context.locals == {}
    assert context.final_output is True
    assert context.package_name is None
    assert context.mode is None
    assert context.tunnel_parameters == {}
    assert context.rule_context is None
    assert context.original_functions == {}
    assert isinstance(context.locals, MappingProxyType)
    assert isinstance(context.tunnel_parameters, MappingProxyType)
    assert isinstance(context.original_functions, MappingProxyType)

    focus = Focus.singleton(XdmAtomic.string("x"))
    changed = dataclasses.replace(
        context,
        focus=focus,
        locals={"x": ()},
        final_output=False,
        package_name="pkg",
        mode="m",
        tunnel_parameters={"t": ()},
    )
    assert changed is not context
    assert changed.focus is focus
    assert changed.locals == {"x": ()}
    assert changed.final_output is False
    assert changed.package_name == "pkg"
    assert changed.mode == "m"
    assert changed.tunnel_parameters == {"t": ()}
    assert context.focus.is_absent
    assert context.locals == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        changed.mode = "other"  # type: ignore[misc]


def test_evaluation_context_rejects_invalid_fields() -> None:
    with pytest.raises(TypeError, match="focus"):
        EvaluationContext(focus="focus")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="locals"):
        EvaluationContext(locals=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="final_output"):
        EvaluationContext(final_output=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="package name"):
        EvaluationContext(package_name=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="mode"):
        EvaluationContext(mode=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tunnel parameters"):
        EvaluationContext(tunnel_parameters=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="rule context"):
        EvaluationContext(rule_context=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="original functions"):
        EvaluationContext(original_functions=[])  # type: ignore[arg-type]


def test_frame_owns_its_evaluation_context() -> None:
    context = EvaluationContext(
        locals={"x": ()},
        package_name="pkg",
        mode="m",
        final_output=False,
        tunnel_parameters={"t": ()},
    )
    frame = Frame(context=context)

    assert frame.context is context
    assert frame.variables is context.locals
    assert frame.tunnel_parameters == {"t": ()}
    assert frame.package == "pkg"
    assert frame.mode == "m"
    assert frame.final_output is False
    assert frame.focus is context.focus

    replaced = dataclasses.replace(
        frame,
        context=dataclasses.replace(context, locals={"y": ()}),
    )
    assert replaced.context.locals == {"y": ()}
    assert frame.context.locals == {"x": ()}
