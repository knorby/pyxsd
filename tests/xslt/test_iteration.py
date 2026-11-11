"""Sequence iteration: ``xsl:for-each``, ``xsl:iterate``, and early exit.

Every stylesheet is inline and every assertion is behavioral.  Iteration
parameters and control state are exercised through the public processor; the
frame-budget test proves that a long iteration does not grow the Python stack or
accumulate scheduler frames.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic
from pyxsd.xpath.environment import Focus
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.instructions import Instruction, TextNode
from pyxsd.xslt.errors import XsltError, XsltStaticError
from pyxsd.xslt.processor import _BreakPlan, _compile_next_iteration, _Invocation
from pyxsd.xslt.runtime.frames import ExecutionLimits, Frame
from pyxsd.xslt.runtime.iteration import IterationState

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"


def stylesheet(body: str, *, version: str = "3.0") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="{version}">{body}</xsl:stylesheet>'
    ).encode()


def compiled(source: bytes, **options: Any):
    return XsltProcessor(**options).compile(io.BytesIO(source))


def transform(
    source: bytes,
    input_source: bytes | None = None,
    *,
    delivery: str = "raw",
    **options: Any,
):
    stylesheet_ = compiled(source)
    if input_source is None:
        return stylesheet_.transform(delivery=delivery, **options)
    return stylesheet_.transform(source=input_source, delivery=delivery, **options)


def error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


# ---------------------------------------------------------------------------
# xsl:for-each


def test_for_each_sets_position_and_size_in_processing_order() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="(3, 1, 2)">
            <xsl:sequence select="concat(position(), '/', last(), ':', .)"/>
          </xsl:for-each>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("1/3:3"),
        XdmAtomic.string("2/3:1"),
        XdmAtomic.string("3/3:2"),
    )


def test_for_each_does_not_leak_body_variables_between_items() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="(1, 2, 3)">
            <xsl:variable name="value" select=". * 10"/>
            <xsl:sequence select="$value"/>
          </xsl:for-each>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.integer(10),
        XdmAtomic.integer(20),
        XdmAtomic.integer(30),
    )


def test_for_each_requires_select() -> None:
    source = stylesheet('<xsl:template name="main"><xsl:for-each/></xsl:template>')
    assert error_code(lambda: compiled(source)) == "XTSE0010"


# ---------------------------------------------------------------------------
# xsl:iterate parameters, next-iteration, break, and on-completion


def test_iterate_parameter_updates_with_next_iteration() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 4">
            <xsl:param name="total" select="0"/>
            <xsl:sequence select="concat(., ':', $total)"/>
            <xsl:next-iteration>
              <xsl:with-param name="total" select="$total + ."/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("1:0"),
        XdmAtomic.string("2:1"),
        XdmAtomic.string("3:3"),
        XdmAtomic.string("4:6"),
    )


def test_iterate_initial_parameter_uses_outer_focus_not_first_item() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="2 to 3">
            <xsl:param name="start" select="10"/>
            <xsl:sequence select=". + $start"/>
            <xsl:next-iteration/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.decimal(12),
        XdmAtomic.decimal(13),
    )


def test_next_iteration_leaves_unspecified_parameters_unchanged() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="(1, 2, 3)">
            <xsl:param name="a" select="0"/>
            <xsl:param name="b" select="100"/>
            <xsl:sequence select="concat($a, '-', $b)"/>
            <xsl:next-iteration>
              <xsl:with-param name="a" select="$a + 1"/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("0-100"),
        XdmAtomic.string("1-100"),
        XdmAtomic.string("2-100"),
    )


def test_iterate_break_appends_pending_output_and_skips_completion() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 10">
            <xsl:param name="seen" select="0"/>
            <xsl:on-completion><xsl:sequence select="'completed'"/></xsl:on-completion>
            <xsl:variable name="next" select="$seen + 1"/>
            <xsl:choose>
              <xsl:when test=". = 3">
                <xsl:sequence select="'before-break'"/>
                <xsl:break select="'break-value'"/>
              </xsl:when>
              <xsl:otherwise>
                <xsl:sequence select="concat('item', .)"/>
                <xsl:next-iteration>
                  <xsl:with-param name="seen" select="$next"/>
                </xsl:next-iteration>
              </xsl:otherwise>
            </xsl:choose>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("item1"),
        XdmAtomic.string("item2"),
        XdmAtomic.string("before-break"),
        XdmAtomic.string("break-value"),
    )


def test_iterate_break_stops_later_items_and_accepts_constructor_value() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 5">
            <xsl:choose>
              <xsl:when test=". = 2">
                <xsl:break><xsl:text>stopped</xsl:text></xsl:break>
              </xsl:when>
              <xsl:otherwise>
                <xsl:sequence select="."/>
                <xsl:next-iteration/>
              </xsl:otherwise>
            </xsl:choose>
          </xsl:iterate>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert result.principal[0] == XdmAtomic.integer(1)
    assert result.principal[1].string_value == "stopped"
    assert len(result.principal) == 2


def test_iterate_empty_input_runs_completion_with_initial_parameters() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="()">
            <xsl:param name="message" select="'empty'"/>
            <xsl:on-completion>
              <xsl:sequence select="concat($message, '-done')"/>
            </xsl:on-completion>
            <xsl:sequence select="'never'"/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("empty-done"),)


def test_iterate_completion_observes_last_next_iteration_parameters() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="(1, 2)">
            <xsl:param name="total" select="0"/>
            <xsl:on-completion select="concat('total=', $total)"/>
            <xsl:next-iteration>
              <xsl:with-param name="total" select="$total + ."/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("total=3"),)


def test_iterate_completion_has_absent_focus() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="()">
            <xsl:param name="value" select="1"/>
            <xsl:on-completion select="."/>
            <xsl:sequence select="'never'"/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(source, initial_template="main")) == "XPDY0002"


def test_iterate_parameter_type_error_preserves_code_and_cause() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:param name="value" as="xs:integer" select="'not-an-integer'"/>
            <xsl:sequence select="$value"/>
            <xsl:next-iteration/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        transform(source, initial_template="main")
    assert caught.value.code == "XTTE0590"
    assert caught.value.__cause__ is not None


def test_next_iteration_values_are_coerced_to_declared_parameter_types() -> None:
    invalid = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 2">
            <xsl:param name="x" as="xs:integer" select="0"/>
            <xsl:sequence select="concat($x instance of xs:integer, ':', $x)"/>
            <xsl:next-iteration>
              <xsl:with-param name="x" select="'not-an-integer'"/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(invalid, initial_template="main")) == "XTTE0590"

    converted = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 2">
            <xsl:param name="x" as="xs:integer" select="0"/>
            <xsl:sequence select="concat($x instance of xs:integer, ':', $x)"/>
            <xsl:next-iteration>
              <xsl:with-param name="x" select="'2'"/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(converted, initial_template="main").principal == (
        XdmAtomic.string("true:0"),
        XdmAtomic.string("true:2"),
    )


def test_next_iteration_with_param_value_rules() -> None:
    content = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 2">
            <xsl:param name="p" select="()"/>
            <xsl:sequence select="concat($p instance of document-node(), ':', count($p))"/>
            <xsl:next-iteration>
              <xsl:with-param name="p"><xsl:sequence select="'x'"/></xsl:with-param>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(content, initial_template="main").principal == (
        XdmAtomic.string("false:0"),
        XdmAtomic.string("true:1"),
    )

    empty = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 2">
            <xsl:param name="p" select="'initial'"/>
            <xsl:sequence select="concat($p instance of xs:string, ':', string-length($p))"/>
            <xsl:next-iteration><xsl:with-param name="p"/></xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(empty, initial_template="main").principal == (
        XdmAtomic.string("true:7"),
        XdmAtomic.string("true:0"),
    )


def test_iterate_body_variables_are_rebound_for_each_item() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="(2, 3)">
            <xsl:param name="seed" select="1"/>
            <xsl:variable name="current" select="$seed * ."/>
            <xsl:sequence select="$current"/>
            <xsl:next-iteration>
              <xsl:with-param name="seed" select="$seed + 1"/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.decimal(2),
        XdmAtomic.decimal(6),
    )


def test_iteration_uses_heap_frames_not_python_recursion() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 3000">
            <xsl:param name="total" select="0"/>
            <xsl:if test=". = 3000">
              <xsl:sequence select="$total"/>
            </xsl:if>
            <xsl:next-iteration>
              <xsl:with-param name="total" select="$total + ."/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    result = transform(
        source,
        initial_template="main",
        limits=ExecutionLimits(max_frames=16),
    )
    assert result.principal == (XdmAtomic.integer(4_498_500),)


# ---------------------------------------------------------------------------
# Static control placement and parameter validation


def test_next_iteration_requires_an_enclosing_iterate() -> None:
    source = stylesheet('<xsl:template name="main"><xsl:next-iteration/></xsl:template>')
    assert error_code(lambda: compiled(source)) == "XTSE3120"


def test_break_outside_iterate_is_static_error() -> None:
    source = stylesheet('<xsl:template name="main"><xsl:break/></xsl:template>')
    assert error_code(lambda: compiled(source)) == "XTSE3120"


def test_break_and_next_iteration_must_be_in_tail_position() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:break/>
            <xsl:sequence select="'after-break'"/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(source)) == "XTSE3120"

    trailing_fallback = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:break/>
            <xsl:fallback><xsl:sequence select="'ignored'"/></xsl:fallback>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(trailing_fallback, initial_template="main").principal == ()


def test_next_iteration_rejects_unknown_and_duplicate_parameters() -> None:
    unknown = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:param name="known" select="1"/>
            <xsl:next-iteration>
              <xsl:with-param name="unknown" select="2"/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(unknown)) == "XTSE3130"

    duplicate = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:param name="known" select="1"/>
            <xsl:next-iteration>
              <xsl:with-param name="known" select="2"/>
              <xsl:with-param name="known" select="3"/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(duplicate)) == "XTSE0670"


def test_iterate_static_content_errors() -> None:
    assert (
        error_code(
            lambda: compiled(stylesheet('<xsl:template name="main"><xsl:iterate/></xsl:template>'))
        )
        == "XTSE0010"
    )

    late_param = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:sequence select="1"/>
            <xsl:param name="late" select="2"/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(late_param)) == "XTSE0010"

    late_completion = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:sequence select="1"/>
            <xsl:on-completion select="2"/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(late_completion)) == "XTSE0010"

    select_and_content = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:on-completion select="1">text</xsl:on-completion>
            <xsl:sequence select="1"/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(select_and_content)) == "XTSE3125"


def test_iterate_tunnel_with_param_is_rejected() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:param name="value" tunnel="yes" select="1"/>
            <xsl:next-iteration>
              <xsl:with-param name="value" tunnel="yes" select="2"/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(source)) == "XTSE0670"


def test_iterate_body_text_content() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="(1, 2)">item<xsl:sequence select="."/><xsl:next-iteration/></xsl:iterate>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    text = "".join(
        item.string_value for item in result.principal if not isinstance(item, XdmAtomic)
    )
    assert text.replace(" ", "") == "itemitem"


def test_iterate_duplicate_parameters_and_multiple_completion_are_static_errors() -> None:
    duplicate = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:param name="value" select="1"/>
            <xsl:param name="value" select="2"/>
            <xsl:sequence select="$value"/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(duplicate)) == "XTSE0580"

    multiple = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:on-completion select="1"/>
            <xsl:on-completion select="2"/>
            <xsl:sequence select="1"/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(multiple)) == "XTSE0010"


def test_next_iteration_rejects_text_and_non_with_param_content() -> None:
    text = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:next-iteration>text</xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(text)) == "XTSE0010"

    element = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:next-iteration><xsl:sequence select="1"/></xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(element)) == "XTSE0010"


def test_break_select_with_content_is_static_error() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:break select="1">text</xsl:break>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(source)) == "XTSE3125"


def test_control_placement_is_validated_in_iteration_containers() -> None:
    sources = (
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:on-completion><xsl:break/></xsl:on-completion>
            <xsl:sequence select="."/>
          </xsl:iterate>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:param name="x"><xsl:break/></xsl:param>
            <xsl:sequence select="."/>
          </xsl:iterate>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:param name="x" select="0"/>
            <xsl:next-iteration>
              <xsl:with-param name="x"><xsl:break/></xsl:with-param>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:break><xsl:break/></xsl:break>
          </xsl:iterate>
        </xsl:template>
        """,
    )
    for source in sources:
        assert error_code(lambda source=source: compiled(stylesheet(source))) == "XTSE3120"


def test_next_iteration_with_param_content_and_empty_parameter() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="(1, 2)">
            <xsl:param name="seen" select="()"/>
            <xsl:param name="unused" select="()"/>
            <xsl:sequence select="concat(., ':', count($seen))"/>
            <xsl:next-iteration xml:space="preserve">   <xsl:with-param name="seen"><xsl:sequence select="if (empty($seen)) then . else ($seen, .)"/></xsl:with-param>   <xsl:with-param name="unused" as="xs:integer?"/></xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("1:0"),
        XdmAtomic.string("2:1"),
    )

    unknown = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="(1, 2)">
            <xsl:param name="seen" select="()"/>
            <xsl:next-iteration>
              <xsl:with-param name="unknown" select="1"/>
            </xsl:next-iteration>
          </xsl:iterate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(unknown)) == "XTSE3130"


def test_nested_iterate_inside_map_entry_is_validated_and_executed() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:map>
            <xsl:map-entry key="'nested'">
              <xsl:iterate select="(1, 2)">
                <xsl:sequence select="."/>
                <xsl:next-iteration/>
              </xsl:iterate>
            </xsl:map-entry>
          </xsl:map>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert len(result.principal) == 1


def test_iterate_stops_processing_after_break_in_nested_template_call() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="(1, 2, 3)">
            <xsl:choose>
              <xsl:when test=". = 2">
                <xsl:call-template name="stop"/>
              </xsl:when>
              <xsl:otherwise>
                <xsl:sequence select="."/>
                <xsl:next-iteration/>
              </xsl:otherwise>
            </xsl:choose>
          </xsl:iterate>
        </xsl:template>
        <xsl:template name="stop">
          <xsl:break select="'stop'"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(source)) == "XTSE3120"


def test_static_error_is_not_shadowed_by_processor_recovery() -> None:
    source = stylesheet('<xsl:template name="main"><xsl:next-iteration/></xsl:template>')
    with pytest.raises(XsltStaticError) as caught:
        compiled(source)
    assert caught.value.code == "XTSE3120"
    assert "tail position" in str(caught.value)


def test_iteration_control_rejects_shared_state_outside_iterate() -> None:
    state = IterationState("for-each", (), (), {})
    plan = _BreakPlan(
        Instruction(name=f"{{{XSLT_NS}}}break"),
        expression=None,
        children=(),
    )
    with pytest.raises(XsltStaticError) as caught:
        _Invocation._iteration_control(  # type: ignore[arg-type]
            None,
            plan,
            Frame(iteration=state),
        )
    assert caught.value.code == "XTSE3120"


def test_break_inside_conditional_content_executes() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 3">
            <xsl:on-non-empty><xsl:sequence select="'nonempty'"/></xsl:on-non-empty>
            <xsl:sequence select="."/>
            <xsl:variable name="value" select="'break'"/>
            <xsl:break select="$value"/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert result.principal == (
        XdmAtomic.string("nonempty"),
        XdmAtomic.integer(1),
        XdmAtomic.string("break"),
    )


def test_break_keeps_pending_output_from_nested_conditional_branch() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 3">
            <xsl:on-non-empty><xsl:sequence select="'nonempty'"/></xsl:on-non-empty>
            <xsl:if test=". = 1">
              <xsl:sequence select="'before'"/>
              <xsl:break select="'break'"/>
            </xsl:if>
          </xsl:iterate>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert result.principal == (
        XdmAtomic.string("nonempty"),
        XdmAtomic.string("before"),
        XdmAtomic.string("break"),
    )


def test_next_iteration_inside_conditional_content_executes() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 3">
            <xsl:on-non-empty><xsl:sequence select="'n'"/></xsl:on-non-empty>
            <xsl:sequence select="."/>
            <xsl:next-iteration/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert tuple(item.lexical for item in result.principal if isinstance(item, XdmAtomic)) == (
        "n",
        "1",
        "n",
        "2",
        "n",
        "3",
    )


def test_next_iteration_keeps_pending_output_from_nested_conditional_branch() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 2">
            <xsl:on-non-empty><xsl:sequence select="'n'"/></xsl:on-non-empty>
            <xsl:if test="true()">
              <xsl:sequence select="'before'"/>
              <xsl:next-iteration/>
            </xsl:if>
          </xsl:iterate>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert tuple(item.lexical for item in result.principal if isinstance(item, XdmAtomic)) == (
        "n",
        "before",
        "n",
        "before",
    )


def test_break_after_where_populated() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 2">
            <xsl:where-populated><xsl:sequence select="'kept'"/></xsl:where-populated>
            <xsl:break select="'break'"/>
          </xsl:iterate>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert tuple(item.lexical for item in result.principal if isinstance(item, XdmAtomic)) == (
        "kept",
        "break",
    )


def test_controls_inside_on_non_empty_content_remain_static_errors() -> None:
    sources = (
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:on-non-empty><xsl:break/></xsl:on-non-empty>
            <xsl:sequence select="."/>
          </xsl:iterate>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:on-empty><xsl:next-iteration/></xsl:on-empty>
          </xsl:iterate>
        </xsl:template>
        """,
    )
    for source in sources:
        assert error_code(lambda source=source: compiled(stylesheet(source))) == "XTSE3120"


def test_controls_inside_xsl_sequence_content_are_static_errors() -> None:
    sources = (
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:sequence><xsl:break/></xsl:sequence>
          </xsl:iterate>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:sequence><xsl:next-iteration/></xsl:sequence>
          </xsl:iterate>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 3">
            <xsl:on-non-empty><xsl:sequence select="'n'"/></xsl:on-non-empty>
            <xsl:sequence><xsl:break select="'break'"/></xsl:sequence>
          </xsl:iterate>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:iterate select="1 to 3">
            <xsl:on-non-empty><xsl:sequence select="'n'"/></xsl:on-non-empty>
            <xsl:sequence><xsl:next-iteration/></xsl:sequence>
          </xsl:iterate>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:if test="true()">
              <xsl:sequence><xsl:break/></xsl:sequence>
            </xsl:if>
          </xsl:iterate>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:iterate select="1">
            <xsl:iterate select="2">
              <xsl:sequence><xsl:break/></xsl:sequence>
            </xsl:iterate>
          </xsl:iterate>
        </xsl:template>
        """,
    )
    for source in sources:
        assert error_code(lambda source=source: compiled(stylesheet(source))) == "XTSE3120"


def test_direct_execution_of_an_iteration_control_plan_is_a_static_error() -> None:
    plan = _BreakPlan(
        Instruction(name=f"{{{XSLT_NS}}}break"),
        expression=None,
        children=(),
    )
    with pytest.raises(XsltStaticError) as caught:
        _Invocation._execute_plan(  # type: ignore[arg-type]
            None,
            plan,
            focus=Focus.absent(),
            locals_={},
            final_output=True,
            package_name=None,
        )
    assert caught.value.code == "XTSE3120"


def test_next_iteration_ignores_a_preserved_whitespace_text_instruction() -> None:
    instruction = Instruction(
        name=f"{{{XSLT_NS}}}next-iteration",
        children=(TextNode(value=" "),),
    )
    plan = _compile_next_iteration(instruction, StaticContext(), None)  # type: ignore[arg-type]
    assert plan.parameters == ()
