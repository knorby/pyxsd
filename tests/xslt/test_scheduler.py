"""Heap-resident scheduling, tail calls, and mixed XPath/XSLT recursion.

The stylesheets in this module are inline and corpus-independent.  They prove
that named-template, apply-templates, and stylesheet-function tail chains run at
10,000 steps under the ordinary Python recursion limit, that non-tail exhaustion
is a documented resource error rather than ``RecursionError``, and that pending
XPath arithmetic, higher-order calls, tunnel parameters, conversions, context
resets, and result order keep their semantics across explicit frames.
"""

from __future__ import annotations

import dataclasses
import io
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from elementpath.exceptions import MissingContextError

from pyxsd import xsd_data_types as xsd
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.xdm import XdmAtomic, XdmFunction, as_xdm
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xpath import continuation
from pyxsd.xpath.continuation import (
    ContinuationError,
    ContinuationMachine,
    FunctionCallPlan,
    HostFunction,
    HostRequest,
    Suspension,
    TailCall,
    UnsupportedContinuationExpression,
    XPathClosure,
    XPathEvaluationError,
    compile_expression,
)
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import XPathStaticContext
from pyxsd.xslt import (
    CompiledStylesheet,
    ExecutionLimits,
    XsltDynamicError,
    XsltProcessor,
)
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.processor import (
    _ABSENT,
    _effective_boolean,
    _Invocation,
    _prepare_invocation,
)
from pyxsd.xslt.runtime.frames import (
    NO_INPUT,
    PYXSD_FRAME_EXHAUSTED,
    Call,
    CatchResult,
    Continuation,
    Continue,
    ExecutionBudget,
    Frame,
    FrameDiagnostic,
    Raise,
    Return,
    Scheduler,
    SchedulerViolation,
)
from pyxsd.xslt.runtime.transformation import ComponentCall, Transformation

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"


def _stylesheet(body: str) -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="3.0">{body}</xsl:stylesheet>'
    ).encode()


NAMED_TAIL = _stylesheet(
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

APPLY_TAIL = _stylesheet(
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

FUNCTION_TAIL = _stylesheet(
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

HIGHER_ORDER = _stylesheet(
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

NON_TAIL = _stylesheet(
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

MUTUAL = _stylesheet(
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

RESULT_ORDER = _stylesheet(
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

TYPE_CONVERSION = _stylesheet(
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

CONTEXT_RESET = _stylesheet(
    """
    <xsl:function name="my:context-name" as="xs:string">
      <xsl:sequence select="name(.)"/>
    </xsl:function>
    <xsl:template match="doc">
      <xsl:sequence select="my:context-name()"/>
    </xsl:template>
    """
)

TUNNEL = _stylesheet(
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


def _text(tree: XdmNode) -> str:
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
    return _text(result.principal_tree)


### Deep mixed recursion


def test_deep_mixed_recursion() -> None:
    recursion_limit = sys.getrecursionlimit()

    assert run_stylesheet("named-tail-10000.xsl") == "done"
    assert run_stylesheet("apply-tail-10000.xsl") == "done"
    assert run_stylesheet("xpath-function-tail-10000.xsl") == "done"
    assert run_stylesheet("xpath-higher-order-roundtrip.xsl") == "83"
    assert sys.getrecursionlimit() == recursion_limit


def test_non_tail_budget_is_not_recursion_error() -> None:
    recursion_limit = sys.getrecursionlimit()

    with pytest.raises(XsltDynamicError) as caught:
        run_stylesheet("non-tail-exhaustion.xsl", limits=ExecutionLimits(max_frames=100))

    assert caught.value.code == "PYXSD-XSLT-001"
    assert caught.value.code == PYXSD_FRAME_EXHAUSTED
    assert caught.value.phase == "dynamic"
    assert "RecursionError" not in str(caught.value)
    assert sys.getrecursionlimit() == recursion_limit


def test_non_tail_recursion_completes_with_a_sufficient_heap_budget() -> None:
    assert run_stylesheet("non-tail-exhaustion.xsl") == "10000"


def test_mutual_recursion_and_tunnel_parameters() -> None:
    assert run_stylesheet("mutual-tail-10000.xsl") == "even"
    assert run_stylesheet("tunnel-parameters.xsl") == "outer-tunnel"


def test_result_order_tunnel_parameters_and_context_reset() -> None:
    assert run_stylesheet("result-order.xsl") == "item-1item-2item-3"
    with pytest.raises(MissingContextError, match="XPDY0002"):
        run_stylesheet("context-reset.xsl", initial_template=None)


def test_type_conversion_across_scheduled_function_calls() -> None:
    assert run_stylesheet("type-conversion.xsl") == "41"


def test_typed_tail_callee_conversion_does_not_consume_caller_output() -> None:
    named = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="inner" as="xs:integer">
              <xsl:sequence select="1"/>
            </xsl:template>
            <xsl:template name="outer">
              <xsl:text>prefix</xsl:text>
              <xsl:call-template name="inner"/>
            </xsl:template>
            <xsl:template name="xsl:initial-template">
              <xsl:call-template name="outer"/>
            </xsl:template>
            """
        )
    )
    assert _text(named.transform(delivery="document").principal_tree) == "prefix1"

    trailing = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="inner" as="xs:integer">
              <xsl:sequence select="1"/>
            </xsl:template>
            <xsl:template name="outer">
              <xsl:text>prefix</xsl:text>
              <xsl:call-template name="inner"/>
              <xsl:text>suffix</xsl:text>
            </xsl:template>
            <xsl:template name="xsl:initial-template">
              <xsl:call-template name="outer"/>
            </xsl:template>
            """
        )
    )
    assert _text(trailing.transform(delivery="document").principal_tree) == "prefix1suffix"

    applied = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="xsl:initial-template">
              <xsl:text>prefix</xsl:text>
              <xsl:apply-templates select="/*/*" mode="m"/>
            </xsl:template>
            <xsl:template match="a" mode="m" as="xs:integer">
              <xsl:sequence select="1"/>
            </xsl:template>
            """
        )
    )
    result = applied.transform(
        source=b"<r><a/></r>",
        initial_template="xsl:initial-template",
        delivery="document",
    )
    assert _text(result.principal_tree) == "prefix1"


def test_tunnel_parameters_inherit_through_named_templates_and_apply_templates() -> None:
    named = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="outer">
              <xsl:call-template name="middle">
                <xsl:with-param name="t" select="'T'" tunnel="yes"/>
              </xsl:call-template>
            </xsl:template>
            <xsl:template name="middle">
              <xsl:call-template name="inner"/>
            </xsl:template>
            <xsl:template name="inner">
              <xsl:param name="t" tunnel="yes" select="'missing'"/>
              <xsl:value-of select="$t"/>
            </xsl:template>
            <xsl:template name="xsl:initial-template">
              <xsl:call-template name="outer"/>
            </xsl:template>
            """
        )
    )
    assert _text(named.transform(delivery="document").principal_tree) == "T"

    applied = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="xsl:initial-template">
              <xsl:apply-templates select="/*/*" mode="m">
                <xsl:with-param name="t" select="'T'" tunnel="yes"/>
              </xsl:apply-templates>
            </xsl:template>
            <xsl:template match="a" mode="m">
              <xsl:apply-templates select="." mode="n"/>
            </xsl:template>
            <xsl:template match="a" mode="n">
              <xsl:param name="t" tunnel="yes" select="'missing'"/>
              <xsl:value-of select="$t"/>
            </xsl:template>
            """
        )
    )
    result = applied.transform(
        source=b"<r><a/></r>",
        initial_template="xsl:initial-template",
        delivery="document",
    )
    assert _text(result.principal_tree) == "T"


def test_with_param_as_conversion_is_applied_before_target_binding() -> None:
    compiled = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="inner">
              <xsl:param name="x"/>
              <xsl:sequence select="$x instance of xs:integer"/>
              <xsl:sequence select="$x + 0.5"/>
            </xsl:template>
            <xsl:template name="xsl:initial-template">
              <xsl:call-template name="inner">
                <xsl:with-param name="x" select="'5'" as="xs:integer"/>
              </xsl:call-template>
            </xsl:template>
            """
        )
    )
    principal = compiled.transform(delivery="raw").principal

    assert principal[0] == XdmAtomic.boolean(True)
    assert principal[1].value == 5.5


def test_effective_boolean_strings_and_multi_item_atomic_sequences() -> None:
    for value in ("'false'", "'0'", "'text'"):
        compiled = compile_stylesheet(
            _stylesheet(
                f"""
                <xsl:template name="xsl:initial-template">
                  <xsl:choose>
                    <xsl:when test="{value}"><xsl:text>true-branch</xsl:text></xsl:when>
                    <xsl:otherwise><xsl:text>false-branch</xsl:text></xsl:otherwise>
                  </xsl:choose>
                </xsl:template>
                """
            )
        )
        result = compiled.transform(delivery="document")
        assert _text(result.principal_tree) == "true-branch"

    empty = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="xsl:initial-template">
              <xsl:choose>
                <xsl:when test="''"><xsl:text>true-branch</xsl:text></xsl:when>
                <xsl:otherwise><xsl:text>false-branch</xsl:text></xsl:otherwise>
              </xsl:choose>
            </xsl:template>
            """
        )
    )
    assert _text(empty.transform(delivery="document").principal_tree) == "false-branch"

    multi = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="xsl:initial-template">
              <xsl:choose>
                <xsl:when test="('a', 'b')"><xsl:text>true-branch</xsl:text></xsl:when>
                <xsl:otherwise><xsl:text>false-branch</xsl:text></xsl:otherwise>
              </xsl:choose>
            </xsl:template>
            """
        )
    )
    with pytest.raises(XsltError) as caught:
        multi.transform(delivery="document")
    assert caught.value.code == "FORG0006"


def test_current_mode_survives_named_templates_and_literal_construction() -> None:
    literal = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template match="/" mode="m">
              <out><xsl:apply-templates select="/*/a" mode="#current"/></out>
            </xsl:template>
            <xsl:template match="a" mode="m"><xsl:text>M</xsl:text></xsl:template>
            <xsl:template match="a"><xsl:text>U</xsl:text></xsl:template>
            """
        )
    )
    result = literal.transform(source=b"<doc><a/></doc>", initial_mode="m", delivery="document")
    assert _text(result.principal_tree) == "M"

    named = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template match="/" mode="m">
              <xsl:call-template name="wrap"/>
            </xsl:template>
            <xsl:template name="wrap">
              <out><xsl:apply-templates select="/*/a" mode="#current"/></out>
            </xsl:template>
            <xsl:template match="a" mode="m"><xsl:text>M</xsl:text></xsl:template>
            <xsl:template match="a"><xsl:text>U</xsl:text></xsl:template>
            """
        )
    )
    result = named.transform(source=b"<doc><a/></doc>", initial_mode="m", delivery="document")
    assert _text(result.principal_tree) == "M"


def test_unused_node_variables_do_not_disable_scheduler_and_fallback_functions_work() -> None:
    unused = compile_stylesheet(
        _stylesheet(
            """
            <xsl:variable name="doc" select="/*"/>
            <xsl:function name="my:done" as="xs:string">
              <xsl:sequence select="'done'"/>
            </xsl:function>
            <xsl:template name="xsl:initial-template">
              <xsl:sequence select="my:done()"/>
            </xsl:template>
            """
        )
    )
    result = unused.transform(
        source=b"<r><a/></r>",
        initial_template="xsl:initial-template",
        delivery="document",
    )
    assert _text(result.principal_tree) == "done"

    fallback = compile_stylesheet(
        _stylesheet(
            """
            <xsl:variable name="doc" select="/*"/>
            <xsl:function name="my:one" as="xs:integer">
              <xsl:sequence select="1"/>
            </xsl:function>
            <xsl:template name="xsl:initial-template">
              <xsl:value-of select="count($doc/*) + my:one()"/>
            </xsl:template>
            """
        )
    )
    result = fallback.transform(
        source=b"<r><a/><a/></r>",
        initial_template="xsl:initial-template",
        delivery="document",
    )
    assert _text(result.principal_tree) == "3"


def test_nested_schedulers_share_the_configured_frame_budget() -> None:
    compiled = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="loop">
              <x><xsl:call-template name="loop"/></x>
            </xsl:template>
            <xsl:template name="xsl:initial-template">
              <xsl:call-template name="loop"/>
            </xsl:template>
            """
        )
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="document", limits=ExecutionLimits(max_frames=50))

    assert caught.value.code == PYXSD_FRAME_EXHAUSTED
    assert "frame budget" in str(caught.value)
    assert "RecursionError" not in str(caught.value)


def test_production_typed_frame_rolls_back_secondary_output_and_records_error() -> None:
    compiled = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template match="/" mode="m" as="xs:integer">
              <xsl:result-document href="http://example.com/secondary"><one/></xsl:result-document>
              <xsl:sequence select="'not-an-integer'"/>
            </xsl:template>
            """
        )
    )
    options = _prepare_invocation(
        compiled,
        source=b"<doc/>",
        parameters=None,
        global_context_item=_ABSENT,
        initial_match_selection=_ABSENT,
        template_parameters=None,
        tunnel_parameters=None,
        initial_template=None,
        initial_mode="m",
        initial_function=None,
        initial_function_arguments=(),
        output_uri=None,
        delivery="document",
    )
    invocation = _Invocation(compiled, options)

    with pytest.raises(XsltError) as caught:
        invocation.run()

    assert caught.value.code == "XTTE0505"
    assert invocation.secondary == {}
    assert "http://example.com/secondary" not in invocation.final_uris
    assert invocation.last_error is not None
    recorded, stack = invocation.last_error
    assert recorded is caught.value
    assert stack and stack[-1].kind == "rule"
    assert invocation.diagnostics


def test_choose_effective_boolean_edges_and_builtin_empty_elements() -> None:
    choose = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="xsl:initial-template">
              <xsl:choose>
                <xsl:when test="()">empty</xsl:when>
                <xsl:when test="/*/missing">missing</xsl:when>
                <xsl:when test="'text'">text</xsl:when>
              </xsl:choose>
            </xsl:template>
            """
        )
    )
    assert [item.string_value for item in choose.transform(delivery="raw").principal] == ["text"]

    no_branch = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="xsl:initial-template">
              <xsl:choose><xsl:when test="false()"/></xsl:choose>
            </xsl:template>
            """
        )
    )
    assert no_branch.transform(delivery="raw").principal == ()

    builtin = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="xsl:initial-template">
              <xsl:apply-templates select="/*/*"/>
            </xsl:template>
            """
        )
    )
    assert builtin.transform(source=b"<r><empty/></r>", delivery="raw").principal == ()

    node = XdmDocument.parse(b"<doc/>").children[0]
    assert _effective_boolean((node,)) is True
    assert _effective_boolean((XdmAtomic.integer(0),)) is False
    assert _effective_boolean((XdmAtomic.integer(1),)) is True
    assert _effective_boolean((XdmAtomic.double(float("nan")),)) is False
    with pytest.raises(XsltError, match="FORG0006"):
        _effective_boolean((XdmFunction(lambda arguments, environment, captured: (), arity=0),))
    with pytest.raises(XsltError, match="FORG0006"):
        _effective_boolean(
            (
                XdmAtomic(
                    xsd.Date("2020-01-01"),
                    f"{{{XSD_NS}}}date",
                    xsd.Date,
                ),
            )
        )


def test_literal_construction_uses_synchronous_nested_component_branches() -> None:
    compiled = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="inner"><xsl:text>inner</xsl:text></xsl:template>
            <xsl:template name="outer">
              <out><xsl:call-template name="inner"/></out>
            </xsl:template>
            <xsl:template name="xsl:initial-template">
              <xsl:call-template name="outer"/>
            </xsl:template>
            """
        )
    )
    assert _text(compiled.transform(delivery="document").principal_tree) == "inner"

    choose = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="xsl:initial-template">
              <out>
                <xsl:choose>
                  <xsl:when test="true()"><xsl:text>chosen</xsl:text></xsl:when>
                  <xsl:otherwise><xsl:text>other</xsl:text></xsl:otherwise>
                </xsl:choose>
              </out>
            </xsl:template>
            """
        )
    )
    assert _text(choose.transform(delivery="document").principal_tree) == "chosen"

    empty_choose = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="xsl:initial-template">
              <out><xsl:choose><xsl:when test="false()"/></xsl:choose></out>
            </xsl:template>
            """
        )
    )
    assert _text(empty_choose.transform(delivery="document").principal_tree) == ""

    apply = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template match="/" mode="m">
              <out><xsl:apply-templates select="/*/*"/></out>
            </xsl:template>
            <xsl:template match="a"><xsl:text>a</xsl:text></xsl:template>
            """
        )
    )
    result = apply.transform(source=b"<doc><a/></doc>", initial_mode="m", delivery="document")
    assert _text(result.principal_tree) == "a"


def test_with_param_content_and_empty_sequence_forms() -> None:
    compiled = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="inner">
              <xsl:param name="x" select="'default'"/>
              <xsl:value-of select="$x"/>
            </xsl:template>
            <xsl:template name="xsl:initial-template">
              <xsl:call-template name="inner">
                <xsl:with-param name="x"><xsl:text>content</xsl:text></xsl:with-param>
              </xsl:call-template>
              <xsl:call-template name="inner"><xsl:with-param name="x"/></xsl:call-template>
            </xsl:template>
            """
        )
    )
    assert _text(compiled.transform(delivery="document").principal_tree) == "content"


def test_apply_templates_mode_resolution_helpers() -> None:
    compiled = compile_stylesheet(
        _stylesheet(
            """
            <xsl:template name="xsl:initial-template">
              <xsl:apply-templates select="/*/*" mode="#current"/>
            </xsl:template>
            <xsl:template match="a" mode="#unnamed"><xsl:text>a</xsl:text></xsl:template>
            """
        )
    )
    result = compiled.transform(source=b"<r><a/></r>", delivery="document")
    assert _text(result.principal_tree) == "a"


def test_static_validation_for_call_apply_choose_and_with_param() -> None:
    cases = [
        (
            '<xsl:template name="m"><xsl:call-template/></xsl:template>',
            "XTSE0650",
        ),
        (
            '<xsl:template name="m"><xsl:call-template name="m"><xsl:with-param/></xsl:call-template></xsl:template>',
            "XTSE0010",
        ),
        (
            '<xsl:template name="m"><xsl:call-template name="m">bad</xsl:call-template></xsl:template>',
            "XTSE0010",
        ),
        (
            '<xsl:template name="m"><xsl:with-param name="x"/></xsl:template>',
            "XTSE0010",
        ),
        (
            '<xsl:template name="m"><xsl:call-template name="m"><bad/></xsl:call-template></xsl:template>',
            "XTSE0010",
        ),
        (
            '<xsl:template name="m"><xsl:call-template name="m">'
            '<xsl:with-param name="x" select="1">text</xsl:with-param>'
            "</xsl:call-template></xsl:template>",
            "XTSE0620",
        ),
        (
            '<xsl:template name="m"><xsl:choose>text</xsl:choose></xsl:template>',
            "XTSE0010",
        ),
        (
            '<xsl:template name="m"><xsl:choose><xsl:when/></xsl:choose></xsl:template>',
            "XTSE0010",
        ),
        (
            '<xsl:template name="m"><xsl:choose><xsl:when test="true()"/>'
            "<xsl:otherwise/><xsl:otherwise/></xsl:choose></xsl:template>",
            "XTSE0010",
        ),
        (
            '<xsl:template name="m"><xsl:choose><xsl:when test="true()"/><bad/></xsl:choose></xsl:template>',
            "XTSE0010",
        ),
    ]
    for body, code in cases:
        with pytest.raises(XsltError) as caught:
            compile_stylesheet(_stylesheet(body))
        assert caught.value.code == code

    whitespace = compile_stylesheet(
        _stylesheet(
            '<xsl:template name="m">'
            '<xsl:call-template name="m"> <xsl:with-param name="x"/> </xsl:call-template>'
            "</xsl:template>"
        )
    )
    assert whitespace is not None


def test_runtime_original_template_and_missing_component_fallback(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.packages import PackageResolver

    library = tmp_path / "library.xsl"
    library.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:app="urn:app"
              name="urn:library" package-version="1.0" version="3.0">
              <xsl:template name="app:start" visibility="public"><base/></xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:app="urn:app"
              name="urn:principal" version="3.0">
              <xsl:use-package name="urn:library" package-version="1.0">
                <xsl:override>
                  <xsl:template name="app:start">
                    <using><xsl:call-template name="xsl:original"/></using>
                  </xsl:template>
                </xsl:override>
              </xsl:use-package>
              <xsl:template name="xsl:initial-template" visibility="public">
                <xsl:call-template name="app:start"/>
              </xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(principal)
    result = compiled.transform(delivery="document")
    using = result.principal_tree.children[0]
    assert using.local_name == "using"
    base = next(child for child in using.children if child.kind is NodeKind.ELEMENT)
    assert base.local_name == "base"


def test_missing_runtime_template_and_unsupported_suspension_are_explicit() -> None:
    class _Package:
        default_mode = "default"

        def original_target(self, name: str, *, kind: str) -> object | None:
            del name, kind
            return None

        def component(self, kind: str, name: str) -> object | None:
            del kind, name
            return None

    class _Reference:
        name = "{urn:missing}template"
        original = True

    fake = type("Fake", (), {"package": _Package()})()
    with pytest.raises(XsltError) as caught:
        _Invocation._resolve_template(fake, _Reference())
    assert caught.value.code == "XTDE0040"

    assert _Invocation._resolve_mode(fake, "#current", "current") == "current"
    assert _Invocation._resolve_mode(fake, "#default", None) == "default"
    assert _Invocation._resolve_mode(fake, "explicit", None) == "explicit"

    with pytest.raises(XsltError, match="not a stylesheet function"):
        _Invocation._suspend_xpath(object(), Frame(), Suspension(HostRequest(object())))


def test_stylesheet_function_returning_a_node_is_outside_the_scheduler_bridge() -> None:
    compiled = compile_stylesheet(
        _stylesheet(
            """
            <xsl:function name="my:produce" as="element()"><a/></xsl:function>
            <xsl:template name="xsl:initial-template">
              <xsl:sequence select="my:produce()"/>
            </xsl:template>
            """
        )
    )
    with pytest.raises(XsltError, match="outside the scheduler bridge"):
        compiled.transform(delivery="raw")


### Scheduler mechanics


class _Extent:
    def __init__(self) -> None:
        self.rolled_back = 0

    def rollback(self) -> None:
        self.rolled_back += 1


def _run_script(
    script: tuple[str, ...],
    *,
    limits: ExecutionLimits | None = None,
    extras: dict[str, Any] | None = None,
) -> tuple[object, Scheduler]:
    """A tiny step language used to exercise the scheduler itself."""

    scheduler = Scheduler(limits)
    data = extras or {}

    def step(frame: Frame) -> Any:
        if frame.input is not NO_INPUT:
            materialized = frame.input if isinstance(frame.input, tuple) else (frame.input,)
            frame = dataclasses.replace(
                frame,
                input=NO_INPUT,
                values=frame.values + tuple(materialized),
            )
        if frame.kind != "entry" or frame.cursor >= len(script):
            value: object = tuple(frame.values)
            if frame.conversion is not None:
                value = frame.conversion(value)
            return Return(value)
        command = cast_program(frame)[frame.cursor]  # type: ignore[index]
        if command == "tail":
            return Call(
                Frame(kind="component", values=("child",)),
                resume=replace_cursor(frame, values=(*frame.values, "caller")),
                tail=True,
            )
        if command == "call":
            return Call(
                Frame(kind="component", values=("child",)),
                resume=replace_cursor(frame, values=(*frame.values, "caller")),
            )
        if command == "return":
            return Return(tuple(frame.values))
        if command == "raise":
            raise data["error"]
        if command == "recurse":
            return Call(Frame(kind="entry", program=("recurse",)), resume=frame)
        raise AssertionError(f"unknown script command {command!r}")

    entry = Frame(
        kind="entry",
        program=script,
        variables=data.get("variables", {}),
        continuation=data.get("continuation"),
        extent=data.get("extent"),
        error_handler=data.get("error_handler"),
        conversion=data.get("conversion"),
        identity="entry",
        package="pkg",
        mode="mode",
        location=data.get("location"),
    )
    return scheduler.run(entry, step), scheduler


def cast_program(frame: Frame) -> Any:
    return frame.program


def replace_cursor(frame: Frame, **changes: Any) -> Frame:
    return dataclasses.replace(frame, cursor=frame.cursor + 1, **changes)


def test_tail_call_replaces_safe_frames_and_preserves_values() -> None:
    result, scheduler = _run_script(("tail",))

    assert result == ("caller", "child")
    assert scheduler.replaced_frames == 1
    assert scheduler.max_frame_depth == 1


def test_tail_call_preserves_accumulated_result_values() -> None:
    scheduler = Scheduler()

    def step(frame: Frame) -> Any:
        if frame.kind == "entry":
            return Call(
                Frame(kind="component", program=("finish",)),
                resume=dataclasses.replace(
                    frame,
                    values=(*frame.values, "appended"),
                    cursor=1,
                ),
                tail=True,
            )
        return Return(frame.values)

    entry = Frame(kind="entry", program=("go",), values=("kept",))
    assert scheduler.run(entry, step) == ("kept", "appended")


@pytest.mark.parametrize(
    "changes",
    [
        {"conversion": lambda value: value},
        {"error_handler": lambda error, stack: None},
        {"extent": _Extent()},
        {"continuation": Continuation("append", target=[])},
    ],
)
def test_tail_call_keeps_frames_with_pending_work(changes: dict[str, Any]) -> None:
    result, scheduler = _run_script(("tail",), extras=changes)

    assert result == ("caller", "child")
    assert scheduler.replaced_frames == 0
    assert scheduler.max_frame_depth == 2
    continuation_value = changes.get("continuation")
    if continuation_value is not None:
        assert continuation_value.target == [("caller", "child")]


def test_tail_call_keeps_an_explicit_child_continuation() -> None:
    scheduler = Scheduler()
    target = Frame(kind="parent", program=("resume",))
    child = Frame(
        kind="child",
        program=("finish",),
        continuation=Continuation("resume", target=target, frame=target),
    )

    def step(frame: Frame) -> Any:
        if frame.kind == "entry":
            return Call(child, tail=True)
        if frame.kind == "parent":
            return Return(("parent", frame.input))
        return Return(("body",))

    assert scheduler.run(Frame(kind="entry", program=("go",)), step) == (
        "parent",
        ("body",),
    )
    assert scheduler.replaced_frames == 1


def test_call_protocol_violations_are_explicit() -> None:
    scheduler = Scheduler()

    with pytest.raises(SchedulerViolation, match="resume frame"):
        scheduler.run(
            Frame(kind="entry"),
            lambda frame: Call(Frame(kind="component")),
        )

    with pytest.raises(SchedulerViolation, match="cannot replace"):
        scheduler.run(
            Frame(kind="entry", conversion=lambda value: value),
            lambda frame: Call(Frame(kind="component"), tail=True),
        )

    with pytest.raises(SchedulerViolation, match="unknown frame result"):
        scheduler.run(Frame(kind="entry"), lambda frame: object())  # type: ignore[arg-type,return-value]


def test_continue_and_return_transitions() -> None:
    scheduler = Scheduler()

    def step(frame: Frame) -> Any:
        if frame.cursor == 0:
            return Continue(dataclasses.replace(frame, cursor=1, values=("a",)))
        return Return(frame.values)

    assert scheduler.run(Frame(kind="entry", program=("x", "y")), step) == ("a",)


def test_raise_result_propagates() -> None:
    error = ValueError("boom")
    with pytest.raises(ValueError, match="boom"):
        _run_script(("raise",), extras={"error": error})


def test_raise_outcome_can_be_caught_by_a_frame_handler() -> None:
    def handler(error: BaseException, stack: tuple[FrameDiagnostic, ...]) -> CatchResult:
        return CatchResult(("caught", str(error)))

    entry = Frame(kind="entry", error_handler=handler)
    assert Scheduler().run(entry, lambda frame: Raise(ValueError("boom"))) == (
        "caught",
        "boom",
    )


def test_error_handler_on_a_frame_with_a_continuation_resumes_it() -> None:
    resumed: list[object] = []

    def handler(error: BaseException, stack: tuple[FrameDiagnostic, ...]) -> CatchResult:
        return CatchResult(("recovered",))

    target = Frame(
        kind="target",
        continuation=Continuation("append", target=resumed),
    )
    entry = Frame(kind="entry")
    child = Frame(kind="child", error_handler=handler)
    child = dataclasses.replace(
        child,
        continuation=Continuation("resume", target=entry, frame=target),
    )

    def step(frame: Frame) -> Any:
        if frame.kind == "entry":
            return Call(child, resume=frame)
        if frame.kind == "target":
            return Return(frame.input)
        raise ValueError("boom")

    assert Scheduler().run(entry, step) == ("recovered",)
    assert resumed == [("recovered",)]


def test_calls_with_child_continuations_are_kept() -> None:
    scheduler = Scheduler()
    parent = Frame(kind="parent")
    target = Frame(kind="target")
    child = Frame(
        kind="child",
        continuation=Continuation("resume", target=target, frame=target),
    )
    entry = Frame(
        kind="entry",
        continuation=Continuation("resume", target=parent, frame=parent),
    )

    def step(frame: Frame) -> Any:
        if frame.kind == "entry":
            return Call(child, tail=True)
        if frame.kind == "parent":
            return Return(("parent", frame.input))
        return Return("child")

    with pytest.raises(SchedulerViolation, match="resume frame"):
        scheduler.run(entry, step)


def test_resume_continuation_without_a_live_target_is_pushed() -> None:
    target = Frame(kind="gone")
    child = Frame(
        kind="child",
        continuation=Continuation("resume", target=target, frame=Frame(kind="next")),
    )
    entry = Frame(kind="entry")

    def step(frame: Frame) -> Any:
        if frame.kind == "entry":
            return Call(child, tail=True)
        if frame.kind == "next":
            return Return(frame.input)
        return Return("value")

    assert Scheduler().run(entry, step) == "value"


def test_resume_continuation_scans_live_frames_before_pushing() -> None:
    scheduler = Scheduler()
    scheduler._frames = [Frame(kind="first"), Frame(kind="second")]  # type: ignore[attr-defined]
    resumed = Frame(kind="resumed")

    scheduler._continue(  # type: ignore[attr-defined]
        Continuation("resume", target=Frame(kind="gone"), frame=resumed),
        "value",
    )

    assert [frame.kind for frame in scheduler._frames] == ["first", "second", "resumed"]  # type: ignore[attr-defined]
    assert scheduler._frames[-1].input == "value"  # type: ignore[attr-defined]

    scheduler._continue(  # type: ignore[attr-defined]
        Continuation("resume", frame=resumed),
        "other",
    )
    assert scheduler._frames[-1].input == "other"  # type: ignore[attr-defined]


def test_resource_error_without_live_frames_keeps_diagnostics_empty() -> None:
    scheduler = Scheduler()
    error = scheduler._resource_error("empty")  # type: ignore[attr-defined]

    assert error.code == PYXSD_FRAME_EXHAUSTED
    assert scheduler.diagnostics == ()


def test_nested_continuation_following_and_direct_results() -> None:
    appended: list[object] = []
    converted: list[object] = []
    frame = Frame(
        kind="entry",
        continuation=Continuation(
            "convert",
            target=lambda value: converted.append(value) or value,
            following=Continuation(
                "append",
                target=appended,
                following=Continuation("convert", target=lambda value: ("final", value)),
            ),
        ),
    )
    assert Scheduler().run(frame, lambda frame: Return("value")) == ("final", "value")
    assert converted == ["value"]
    assert appended == ["value"]

    direct = Frame(
        kind="entry",
        continuation=Continuation("convert", target=lambda value: value.upper()),
    )
    assert Scheduler().run(direct, lambda frame: Return("value")) == "VALUE"


def test_scheduler_frame_depth_property_is_live() -> None:
    scheduler = Scheduler()
    depths: list[int] = []

    def step(frame: Frame) -> Any:
        depths.append(scheduler.frame_depth)
        return Return(())

    scheduler.run(Frame(), step)
    assert depths == [1]
    assert scheduler.frame_depth == 0


def test_error_handler_catches_and_rolls_back_inner_extents() -> None:
    extent = _Extent()
    location = IssueLocation(source="memory.xsl", path=(PathStep(name="template", index=2),))
    seen: list[tuple[BaseException, tuple[FrameDiagnostic, ...]]] = []

    def handler(error: BaseException, stack: tuple[FrameDiagnostic, ...]) -> CatchResult:
        seen.append((error, stack))
        return CatchResult(("caught",))

    entry = Frame(
        kind="entry",
        program=("child",),
        error_handler=handler,
        identity="outer",
    )
    child = Frame(kind="rule", extent=extent, identity="inner", location=location)
    error = XsltError("bad", code="XTDE0000")

    def step(frame: Frame) -> Any:
        if frame.kind == "entry":
            return Call(child, resume=entry)
        raise error

    result = Scheduler().run(entry, step)

    assert result == ("caught",)
    assert extent.rolled_back == 1
    assert seen[0][0] is error
    assert [item.identity for item in seen[0][1]] == ["outer", "inner"]
    assert [item.kind for item in seen[0][1]] == ["entry", "rule"]
    assert seen[0][1][1].location == location
    assert seen[0][1][0].package is None


def test_error_handler_can_decline_and_the_stack_is_recorded() -> None:
    scheduler = Scheduler()
    seen: list[tuple[FrameDiagnostic, ...]] = []

    def decline(error: BaseException, stack: tuple[FrameDiagnostic, ...]) -> None:
        seen.append(stack)
        return None

    entry = Frame(kind="entry", error_handler=decline, identity="outer")
    child = Frame(kind="rule", identity="inner")

    def step(frame: Frame) -> Any:
        if frame.kind == "entry":
            return Call(child, resume=dataclasses.replace(frame, cursor=1))
        raise XsltError("bad", code="XTDE0000")

    with pytest.raises(XsltError):
        scheduler.run(entry, step)
    assert [item.identity for item in scheduler.diagnostics] == ["outer", "inner"]
    assert [item.identity for item in seen[0]] == ["outer", "inner"]


def test_invalid_error_handler_result_is_rejected() -> None:
    entry = Frame(kind="entry", error_handler=lambda error, stack: object())

    with pytest.raises(SchedulerViolation, match="CatchResult"):
        Scheduler().run(entry, lambda frame: Raise(ValueError("boom")))


def test_rollback_is_optional_for_extents() -> None:
    entry = Frame(kind="entry")
    child = Frame(kind="child", extent=object())

    def step(frame: Frame) -> Any:
        if frame.kind == "entry":
            return Call(child, resume=frame)
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        Scheduler().run(entry, step)


def test_resource_budgets_and_python_recursion_are_documented_errors() -> None:
    scheduler = Scheduler(ExecutionLimits(max_frames=2))

    def recurse(frame: Frame) -> Any:
        return Call(Frame(kind="entry", program=("recurse",)), resume=frame)

    with pytest.raises(XsltDynamicError) as caught:
        scheduler.run(Frame(kind="entry", identity="entry"), recurse)
    assert caught.value.code == PYXSD_FRAME_EXHAUSTED
    assert [item.identity for item in scheduler.diagnostics] == ["entry", None]

    with pytest.raises(XsltDynamicError) as caught:
        _run_script(("tail",), limits=ExecutionLimits(max_steps=1))
    assert caught.value.code == PYXSD_FRAME_EXHAUSTED

    def overflow(frame: Frame) -> Any:
        raise RecursionError("python stack")

    with pytest.raises(XsltDynamicError) as caught:
        Scheduler().run(Frame(kind="entry"), overflow)
    assert caught.value.code == PYXSD_FRAME_EXHAUSTED
    assert "RecursionError" not in str(caught.value)


def test_scheduler_budget_contract_and_frame_release() -> None:
    budget = ExecutionBudget(ExecutionLimits(max_frames=1))
    with pytest.raises(TypeError, match="ExecutionBudget"):
        Scheduler(budget=object())  # type: ignore[arg-type]
    assert budget.take_step() is True
    assert budget.acquire_frame() is True
    assert budget.acquire_frame() is False
    budget.release_frame()
    budget.release_frame()
    assert budget.frames == 0


def test_scheduler_rejects_invalid_configuration_and_reentrancy() -> None:
    with pytest.raises(TypeError, match="ExecutionLimits"):
        Scheduler(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="entry"):
        Scheduler().run(object(), lambda frame: Return(()))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="step"):
        Scheduler().run(Frame(), object())  # type: ignore[arg-type]

    scheduler = Scheduler()
    with pytest.raises(SchedulerViolation, match="reentrant"):
        scheduler.run(
            Frame(),
            lambda frame: scheduler.run(Frame(), lambda inner: Return(())),
        )
    with pytest.raises(TypeError, match="frame"):
        scheduler._push(object())  # type: ignore[arg-type]


def test_scheduler_can_run_sequential_executions() -> None:
    scheduler = Scheduler()

    def step(frame: Frame) -> Any:
        return Return(tuple(frame.values))

    assert scheduler.run(Frame(values=(1,)), step) == (1,)
    assert scheduler.run(Frame(values=(2,)), step) == (2,)
    assert scheduler.steps == 2


def test_execution_limits_validation() -> None:
    with pytest.raises(TypeError, match="max_frames"):
        ExecutionLimits(max_frames="1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_frames"):
        ExecutionLimits(max_frames=0)
    with pytest.raises(TypeError, match="max_steps"):
        ExecutionLimits(max_steps=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_steps"):
        ExecutionLimits(max_steps=0)


def test_frame_and_continuation_contracts() -> None:
    with pytest.raises(ValueError, match="kind"):
        Frame(kind="")
    with pytest.raises(TypeError, match="variables"):
        Frame(variables=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tunnel parameters"):
        Frame(tunnel_parameters=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="focus"):
        Frame(focus="focus")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="error handler"):
        Frame(error_handler=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="conversion"):
        Frame(conversion=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="final_output"):
        Frame(final_output=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tail"):
        Frame(tail=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="values"):
        Frame(values=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="identity"):
        Frame(identity=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="package"):
        Frame(package=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="mode"):
        Frame(mode=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="location"):
        Frame(location="here")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="continuation kind"):
        Continuation("later")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="resume continuation requires"):
        Continuation("resume")
    with pytest.raises(TypeError, match="resume continuation target"):
        Continuation("resume", frame=Frame(), target=1)
    with pytest.raises(TypeError, match="append continuation"):
        Continuation("append", target=())
    with pytest.raises(ValueError, match="append continuation"):
        Continuation("append", target=[], frame=Frame())
    with pytest.raises(TypeError, match="convert continuation"):
        Continuation("convert", target=1)
    with pytest.raises(ValueError, match="convert continuation"):
        Continuation("convert", target=lambda value: value, frame=Frame())

    with pytest.raises(TypeError, match="child frame"):
        Call(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="resume"):
        Call(Frame(), resume=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tail flag"):
        Call(Frame(), tail=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="exception"):
        Raise(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="HostRequest"):
        Suspension(object())  # type: ignore[arg-type]


def test_continuation_append_and_convert_transitions() -> None:
    appended: list[object] = []
    converted: list[object] = []
    target = Frame(
        kind="target",
        continuation=Continuation(
            "convert",
            target=lambda value: converted.append(value) or value,
            following=Continuation("append", target=appended),
        ),
    )

    def direct(frame: Frame) -> Any:
        return Return(("value",))

    scheduler = Scheduler()
    assert scheduler.run(target, direct) == ("value",)
    assert converted == [("value",)]
    assert appended == [("value",)]


### Continuation-machine suspension


def test_host_request_and_suspension_contracts() -> None:
    with pytest.raises(TypeError, match="tail flag"):
        HostRequest(object(), tail=1)  # type: ignore[arg-type]
    request = HostRequest(object(), [1, 2])  # type: ignore[arg-type]
    assert request.arguments == (1, 2)
    assert request.tail is False
    with pytest.raises(TypeError, match="suspend flag"):
        HostFunction(lambda: 1, suspend=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative"):
        FunctionCallPlan("f", -1, ())
    with pytest.raises(ValueError, match="name"):
        FunctionCallPlan("", 0, ())
    with pytest.raises(ValueError, match="arity must match"):
        FunctionCallPlan("f", 1, ())


def test_static_function_calls_suspend_and_resume() -> None:
    compiled = compile_expression(
        "f:count($n - 1)",
        variable_types={"n": "xs:integer"},
        namespaces={"f": MY_NS},
        functions={f"{{{MY_NS}}}count": 1},
    )
    calls: list[object] = []

    def count(value: object) -> object:
        calls.append(value)
        return 0 if value == 0 else 99

    machine = ContinuationMachine(
        compiled,
        {"n": 3},
        {(f"{{{MY_NS}}}count", 1): HostFunction(count, name="count", arity=1, suspend=True)},
    )

    outcome = machine.run_until_suspension()
    assert isinstance(outcome, Suspension)
    assert machine.suspended is True
    assert outcome.request.arguments == (2,)
    assert outcome.request.tail is True
    assert calls == []

    assert machine.resume(7) == 7
    assert machine.suspended is False
    assert calls == []


def test_suspended_machine_run_and_resume_contracts() -> None:
    compiled = compile_expression(
        "f:constant()",
        namespaces={"f": MY_NS},
        functions={f"{{{MY_NS}}}constant": 0},
    )
    host = HostFunction(lambda: 5, name="constant", arity=0, suspend=True)
    machine = ContinuationMachine(
        compiled,
        functions={(f"{{{MY_NS}}}constant", 0): host},
    )

    assert isinstance(machine.run_until_suspension(), Suspension)
    with pytest.raises(ContinuationError, match="can run only once"):
        machine.run_until_suspension()
    with pytest.raises(ContinuationError, match="can run only once"):
        machine.run()

    suspended = ContinuationMachine(
        compiled,
        functions={(f"{{{MY_NS}}}constant", 0): host},
    )
    with pytest.raises(ContinuationError, match="scheduler-owned"):
        suspended.run()

    completed = ContinuationMachine(
        compiled,
        functions={(f"{{{MY_NS}}}constant", 0): HostFunction(lambda: 5, arity=0)},
    )
    assert completed.run() == 5
    with pytest.raises(ContinuationError, match="not suspended"):
        completed.resume(1)

    fresh = ContinuationMachine(compiled, functions={(f"{{{MY_NS}}}constant", 0): host})
    with pytest.raises(ContinuationError, match="not started"):
        fresh.resume(1)


def test_static_function_machine_errors_and_compilation_fallback() -> None:
    compiled = compile_expression(
        "f:missing()",
        namespaces={"f": MY_NS},
        functions={f"{{{MY_NS}}}missing": 0},
    )
    machine = ContinuationMachine(compiled)
    with pytest.raises(XPathEvaluationError, match="XPST0017"):
        machine.run()

    compiled_with_args = compile_expression(
        "f:pair(1, 2)",
        namespaces={"f": MY_NS},
        functions={f"{{{MY_NS}}}pair": 2},
    )
    host = HostFunction(lambda left, right: left + right, name="pair", arity=2)
    assert (
        ContinuationMachine(compiled_with_args, functions={(f"{{{MY_NS}}}pair", 2): host}).run()
        == 3
    )


def test_static_function_registration_rejects_invalid_names() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        compile_expression("1 + 1", functions={"": 0})
    compiled = compile_expression(
        "1 + 1",
        namespaces={"unbound": MY_NS},
        functions={"q:unreachable": 0, "{urn:none}missing": 0},
    )
    assert continuation.evaluate(compiled) == 2


def test_static_function_lexical_registration_and_unresolvable_token() -> None:
    compiled = compile_expression(
        "f:constant()",
        namespaces={"f": MY_NS},
        functions={"f:constant": 0},
    )
    host = HostFunction(lambda: 7, name="constant", arity=0)
    assert ContinuationMachine(compiled, functions={(f"{{{MY_NS}}}constant", 0): host}).run() == 7

    class _BadExternal:
        label = "external function"
        name = None
        value = None
        namespace = None

        def __len__(self) -> int:
            return 0

    class _Outer:
        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> object:
            assert index == 0
            return _BadExternal()

    compiler = continuation._Compiler(continuation.XPath31Parser())
    with pytest.raises(UnsupportedContinuationExpression, match="resolvable name"):
        compiler._compile_static_call(_Outer())


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("1 +", Exception),
        ("count((1, 2))", UnsupportedContinuationExpression),
        ("//a", UnsupportedContinuationExpression),
    ],
)
def test_continuation_compiler_keeps_its_subset_boundary(
    source: str,
    error: type[BaseException],
) -> None:
    with pytest.raises(error):
        compile_expression(source)


### Transformation state and XPath bridge


def test_transformation_default_state_and_step_contract() -> None:
    transformation = Transformation()

    assert isinstance(transformation.limits, ExecutionLimits)
    assert transformation.diagnostics == ()
    assert transformation.stylesheet_functions() == {}
    assert transformation.fallback_functions() == {}
    with pytest.raises(NotImplementedError, match="step"):
        transformation.step(Frame())
    with pytest.raises(NotImplementedError, match="component_callable"):
        transformation._invoke_component_callable(None, ())


def test_transformation_scheduled_xpath_and_fallbacks() -> None:
    static = XPathStaticContext(
        namespaces={"f": MY_NS},
        variable_types={"n": "xs:integer"},
        functions={f"{{{MY_NS}}}count": 1},
    )
    expression = XPathEngine("3.1").compile("f:count($n - 1)", static)
    transformation = Transformation()
    plan = transformation.xpath_plan(expression)
    assert plan is not None
    assert transformation.xpath_plan(expression) is plan

    machine = transformation.xpath_machine(
        expression,
        {"n": 3},
        {(f"{{{MY_NS}}}count", 1): HostFunction(lambda value: value, arity=1)},
    )
    assert machine is not None
    assert machine.run() == 2
    assert transformation.xpath_machines == 1

    unsupported = XPathEngine("3.1").compile("//a", XPathStaticContext())
    assert transformation.xpath_plan(unsupported) is None
    assert transformation.xpath_machine(unsupported, {}) is None

    node = XdmDocument.parse(b"<a/>").children[0]
    assert transformation.sequence_to_native((node,)) is None
    assert transformation.variables_to_native({"node": (node,)}) is None
    assert transformation.xpath_machine(expression, {"n": (node,)}) is None
    assert transformation.sequence_to_native(()) == ()
    assert transformation.sequence_to_native([XdmAtomic.integer(1)]) == (1,)
    assert transformation.sequence_to_native([(node,)]) is None
    assert transformation.sequence_to_native(object()) is None

    closure = continuation.evaluate(compile_expression("function($x) { $x }"))
    assert transformation.sequence_to_native(closure) is closure
    opaque = XdmFunction(lambda arguments, environment, captured: (), arity=0)
    assert transformation.sequence_to_native((opaque,)) is None


def test_transformation_reads_output_attributes_and_output_uri() -> None:
    compiled = compile_stylesheet(
        _stylesheet(
            '<xsl:output method="text"/>'
            '<xsl:template name="xsl:initial-template"><xsl:text>done</xsl:text></xsl:template>'
        )
    )

    class _Options:
        output_uri = "http://example.com/out.xml"

    transformation = Transformation(compiled, _Options())
    assert transformation.final_uris == {"http://example.com/out.xml"}
    assert transformation.output_attributes["method"] == "text"


def test_transformation_diagnostics_expose_failed_scheduler_stack() -> None:
    def step(frame: Frame) -> Any:
        raise ValueError("boom")

    transformation = _SteppingTransformation(step)
    with pytest.raises(ValueError, match="boom"):
        transformation.execute_frame(Frame(kind="entry", identity="entry"))

    assert [item.identity for item in transformation.diagnostics] == ["entry"]


def test_transformation_value_conversions() -> None:
    transformation = Transformation()

    assert transformation.sequence_to_native((XdmAtomic.integer(1),)) == 1
    assert transformation.sequence_to_native((XdmAtomic.integer(1), XdmAtomic.integer(2))) == (1, 2)
    assert transformation.native_to_xdm(3) == (XdmAtomic.integer(3),)
    item = XdmAtomic.integer(4)
    assert transformation.native_to_xdm(item) == (item,)
    assert transformation.native_to_xdm([1, 2]) == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
    )

    closure = continuation.evaluate(compile_expression("function($x) { $x }"))
    assert isinstance(closure, XPathClosure)
    item = transformation.closure_item(closure)
    assert isinstance(item, XdmFunction)
    assert item.closure is closure
    (wrapped,) = transformation.native_to_xdm(closure)
    assert isinstance(wrapped, XdmFunction)
    assert wrapped.closure is closure
    with pytest.raises(XsltError, match="scheduler"):
        item.invoke((as_xdm(1),))

    with pytest.raises(XsltDynamicError, match="suspended"):
        transformation.native_to_xdm(Suspension(HostRequest(object())))
    with pytest.raises(XsltDynamicError, match="tail-control"):
        transformation.native_to_xdm(TailCall(object()))
    with pytest.raises(XsltDynamicError, match="cannot convert"):
        transformation.native_to_xdm(object())


def test_transformation_uses_compiled_stylesheet_state_and_functions() -> None:
    compiled = compile_stylesheet(HIGHER_ORDER)
    transformation = Transformation(compiled)

    table = transformation.stylesheet_functions()
    assert (f"{{{MY_NS}}}next", 1) in table
    assert transformation.stylesheet_functions() is table
    fallback = transformation.fallback_functions()
    assert f"{{{MY_NS}}}next" in fallback
    assert transformation.fallback_functions() is fallback
    assert callable(fallback[f"{{{MY_NS}}}next"])
    entry = next(iter(table.values()))
    assert isinstance(entry, HostFunction)
    assert entry.suspend is True
    target = entry.callback
    assert isinstance(target, ComponentCall)
    assert target.kind == "function"
    with pytest.raises(XsltDynamicError, match="scheduler"):
        target()

    arguments = transformation.component_arguments((1, "two"))
    assert arguments[0] == (XdmAtomic.integer(1),)
    assert arguments[1] == (XdmAtomic.string("two"),)


def test_component_call_validation_and_repr() -> None:
    with pytest.raises(ValueError, match="component call kind"):
        ComponentCall(object(), "other")
    call = ComponentCall(object())
    assert "ComponentCall" in repr(call)


class _SteppingTransformation(Transformation):
    def __init__(self, step: Any) -> None:
        super().__init__()
        self._step = step

    def step(self, frame: Frame) -> Any:
        return self._step(frame)


def test_secondary_extent_rollback_preserves_earlier_entries() -> None:
    transformation = Transformation()
    transformation.secondary["keep"] = object()
    transformation.final_uris.add("http://example.com/keep")
    frame = transformation.frame_boundary(Frame(kind="rule"))

    transformation.secondary["added"] = object()
    transformation.final_uris.add("http://example.com/added")
    assert frame.extent is not None
    frame.extent.rollback()

    assert tuple(transformation.secondary) == ("keep",)
    assert transformation.final_uris == {"http://example.com/keep"}


def test_transformation_nested_execution_tracks_nested_diagnostics() -> None:
    transformation = _SteppingTransformation(lambda frame: Return(frame.kind))

    result = transformation.execute_frame(Frame(kind="outer"))
    assert result == "outer"

    def nested(frame: Frame) -> Any:
        if frame.kind == "outer":
            assert transformation.execute_frame(Frame(kind="inner")) == "inner"
            return Return("outer")
        return Return("inner")

    transformation = _SteppingTransformation(nested)
    assert transformation.execute_frame(Frame(kind="outer")) == "outer"
    assert transformation.diagnostics == ()


def test_frame_diagnostic_and_input_contracts() -> None:
    location = IssueLocation(source="memory.xsl", path=(PathStep(name="template", index=1),))
    frame = Frame(
        kind="rule",
        identity="rule doc",
        component=object(),
        package="pkg",
        mode="m",
        location=location,
    )
    diagnostic = frame.diagnostic()
    assert diagnostic.kind == "rule"
    assert diagnostic.identity == "rule doc"
    assert diagnostic.package == "pkg"
    assert diagnostic.mode == "m"
    assert diagnostic.location == location
    assert frame.input is NO_INPUT
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.kind = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        frame.variables["x"] = 1  # type: ignore[index]


def test_xpath_closure_as_a_component_argument_keeps_working() -> None:
    closure = continuation.evaluate(compile_expression("function($x) { $x * 2 }"))
    assert isinstance(closure, XPathClosure)
    transformation = Transformation()
    (wrapped,) = transformation.native_to_xdm(closure)
    assert isinstance(wrapped, XdmFunction)
    assert transformation.sequence_to_native((wrapped,)) is closure


def test_transformation_runs_with_a_real_scheduler_frame() -> None:
    def step(frame: Frame) -> Any:
        if frame.cursor == 0:
            return Continue(
                dataclasses.replace(
                    frame,
                    cursor=1,
                    values=(*frame.values, "one"),
                )
            )
        return Return(tuple(frame.values))

    transformation = _SteppingTransformation(step)
    assert transformation.execute_frame(Frame(program=("a", "b"))) == ("one",)
