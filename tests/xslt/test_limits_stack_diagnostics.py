"""Coded stack diagnostics for non-tail chains, serialization, and tail calls.

Each probe runs away from the test runner's stack and asserts the message names
the interpreter recursion limit and the configured frame budget.  Safe tail
calls keep a constant live frame count and are bounded by steps, not frames.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest
from tests.xslt.limits_support import (
    assert_exhausted,
    assert_literal_python_stack,
    assert_python_stack_message,
    deep_stylesheet,
)
from tests.xslt.support import run_probe, stylesheet

from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError, XsltSerializationError
from pyxsd.xslt.processor import _Invocation
from pyxsd.xslt.runtime.frames import (
    Continuation,
    ExecutionLimits,
    Frame,
    Return,
    Scheduler,
)
from pyxsd.xslt.serialization import Serializer


@pytest.mark.parametrize(
    ("shape", "cap", "depth_pattern"),
    [
        ("sequence", 8, r"[1-9][0-9]*"),
        ("function-constructor", 8, r"[1-9][0-9]*"),
        ("static-path", 1, "2"),
    ],
)
def test_deep_non_tail_execution_reports_the_bound_that_fired(shape, cap, depth_pattern):
    # Run the probe away from the test runner's stack. Only the static path
    # has structural frame depth: all its path steps run in one dispatch.
    run = run_probe(
        [
            sys.executable,
            "-c",
            """
import json
import sys
from tests.xslt.limits_support import deep_stylesheet
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.frames import ExecutionLimits

compiled, source = deep_stylesheet(sys.argv[1])
results = []
for limits in (ExecutionLimits(), ExecutionLimits(max_frames=int(sys.argv[2]))):
    try:
        compiled.transform(source, delivery="raw", limits=limits)
    except XsltDynamicError as error:
        results.append([type(error).__name__, error.code, error.catchable, error.args[0]])
print(json.dumps([sys.getrecursionlimit(), results]))
""",
            shape,
            str(cap),
        ],
        cwd=Path(__file__).resolve().parents[2],
        timeout=40,
    )
    recursion_limit, (stack_error, frame_error) = json.loads(run.stdout)
    signature = ["XsltDynamicError", "PYXSD-XSLT-001", False]
    assert stack_error[:3] == frame_error[:3] == signature
    prefix = (
        "the transformation exceeded the Python recursion limit: "
        f"Python recursion limit={recursion_limit}, max_frames=100000, frame depth="
    )
    assert re.fullmatch(re.escape(prefix) + depth_pattern, stack_error[3])
    assert frame_error[3] == (
        f"the transformation exceeded its frame budget: max_frames={cap}, "
        f"cumulative amount={cap + 1}"
    )


@pytest.mark.parametrize(
    "limits",
    [ExecutionLimits(), ExecutionLimits(max_frames=200_000), ExecutionLimits(max_frames=None)],
)
def test_python_stack_diagnostic_names_the_configured_frame_budget(limits):
    compiled, source = deep_stylesheet("static-path")
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source, delivery="raw", limits=limits)
    error = caught.value
    assert type(error) is XsltDynamicError
    assert error.code == "PYXSD-XSLT-001"
    assert error.catchable is False
    budget = (
        "max_frames=None (no frame budget)"
        if limits.max_frames is None
        else f"max_frames={limits.max_frames}"
    )
    assert error.args[0] == (
        f"the transformation exceeded the Python recursion limit: "
        f"Python recursion limit={sys.getrecursionlimit()}, {budget}, frame depth=2"
    )


def test_deep_result_serialization_reports_a_coded_error():
    depth = sys.getrecursionlimit() + 50
    document = XdmDocument.parse(b"<n>" * depth + b"</n>" * depth)
    with pytest.raises(XsltSerializationError) as caught:
        Serializer.serialize((document,))
    assert caught.value.code == "PYXSD-XSLT-001"
    assert caught.value.args[0] == (
        f"serialization exceeded the Python recursion limit: "
        f"Python recursion limit={sys.getrecursionlimit()}"
    )
    assert isinstance(caught.value.__cause__, RecursionError)


def test_python_stack_diagnostic_when_a_return_continuation_overflows():
    def overflow(value):
        raise RecursionError("continuation overflow")

    scheduler = Scheduler()
    entry = Frame(kind="entry", continuation=Continuation("convert", target=overflow))
    with pytest.raises(XsltDynamicError) as caught:
        scheduler.run(entry, lambda frame: Return(None))
    assert caught.value.code == "PYXSD-XSLT-001"
    assert caught.value.catchable is False
    assert caught.value.args[0] == (
        f"the transformation exceeded the Python recursion limit: "
        f"Python recursion limit={sys.getrecursionlimit()}, max_frames=100000, frame depth=0"
    )


def test_assertion_content_propagates_stack_exhaustion(monkeypatch):
    def overflow(self, plans, *, context):
        raise RecursionError("stack exhausted during assertion content")

    source = stylesheet(
        '<xsl:template match="/"><xsl:assert test="false()">msg</xsl:assert></xsl:template>'
    )
    compiled = XsltProcessor(static_options={"assertions": True}).compile(io.BytesIO(source))
    monkeypatch.setattr(_Invocation, "_execute_plans", overflow)
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(b"<n/>", delivery="raw", limits=ExecutionLimits(max_frames=None))
    assert type(caught.value) is XsltDynamicError
    assert caught.value.code == "PYXSD-XSLT-001"
    assert not caught.value.catchable
    assert_python_stack_message(caught.value.args[0])


def test_value_template_in_scheduled_literal_content_reports_python_stack():
    body = """<xsl:template name="empty"/>
    <xsl:function name="local:f">
      <xsl:param name="n"/>
      <xsl:if test="$n gt 0">
        <out><xsl:call-template name="empty"/>
          <xsl:text expand-text="yes">{local:f($n - 1)}</xsl:text>
        </out>
      </xsl:if>
    </xsl:function>
    <xsl:template match="/"><xsl:sequence select="local:f(160)"/></xsl:template>"""
    assert_literal_python_stack(stylesheet(body, namespaces={"local": "urn:local"}))


def _tail_stylesheet(stop):
    termination = '<xsl:when test="$n = 0"><xsl:sequence select="0"/></xsl:when>' if stop else ""
    return XsltProcessor().compile(
        io.BytesIO(
            stylesheet(
                f"""<xsl:template name="count">
                  <xsl:param name="n" as="xs:integer"/>
                  <xsl:choose>{termination}<xsl:when test="true()">
                    <xsl:call-template name="count">
                      <xsl:with-param name="n" select="$n - 1"/>
                    </xsl:call-template>
                  </xsl:when></xsl:choose>
                </xsl:template>
                <xsl:template name="xsl:initial-template">
                  <!-- 2,000 levels: far past the Python stack ceiling for this
                       shape, and past max_frames=8 in the test below. -->
                  <xsl:call-template name="count"><xsl:with-param name="n" select="2000"/>
                  </xsl:call-template>
                </xsl:template>"""
            )
        )
    )


def test_safe_tail_calls_do_not_consume_additional_live_frames():
    result = _tail_stylesheet(True).transform(delivery="raw", limits=ExecutionLimits(max_frames=8))
    assert result.principal == (XdmAtomic.integer(0),)


def test_step_budget_stops_unbounded_tail_calls_within_the_frame_budget():
    with pytest.raises(XsltDynamicError) as caught:
        _tail_stylesheet(False).transform(
            delivery="raw", limits=ExecutionLimits(max_frames=8, max_steps=64)
        )
    assert_exhausted(caught.value, "step", "max_steps", 64, 65)
