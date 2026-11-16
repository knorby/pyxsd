"""The execution-budget alarm is an instrument signal, not an engine failure.

The conformance harness bounds every case with ``case_budget``, which raises
``TimeoutError`` from ``SIGALRM``.  XSLT 3.0 section 5.5.4 turns a dynamic
error raised while matching a pattern into a non-match, so the pattern
evaluator's broad handler used to cancel that alarm: the runaway case kept
running past its budget instead of stopping at it.  The engine's own
``ExecutionLimits`` budget must survive the same handlers for the same reason.

These tests pin that both alarms pass through every conversion the engine
performs while evaluating a case, and that genuine pattern and expression
failures keep the behavior they had before.
"""

from __future__ import annotations

import io
import time
from pathlib import PurePosixPath
from typing import Any

import pytest
from tests.xslt.model import Environment, Invocation, Source, Stylesheet
from tests.xslt.model import TestCase as _TestCase
from tests.xslt.outcomes import Outcome
from tests.xslt.suite import SUITE_PROFILES, run_case
from tests.xslt.support import stylesheet

from pyxsd.resource_policy import ResourceData
from pyxsd.xdm.nodes import XdmDocument, XdmNode
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, XPathStaticContext
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler import patterns as pattern_module
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.patterns import PatternMatcher, compile_pattern
from pyxsd.xslt.errors import (
    IMPLEMENTATION_BUDGET_CODE,
    XsltDynamicError,
    is_budget_alarm,
)
from pyxsd.xslt.runtime.evaluate import (
    DynamicExpressionCache,
    StaticContextIdentity,
    evaluate_dynamic,
)
from pyxsd.xslt.runtime.transformation import Transformation

PROFILE = SUITE_PROFILES["xslt30"]

#: One alarm per budget the engine can run into: the harness's wall-clock
#: ``TimeoutError`` and the invocation-local ``ExecutionLimits`` budget, which
#: carries the implementation code and cannot be caught by ``xsl:try``.
BUDGET_ALARMS = (
    pytest.param(TimeoutError("the case exceeded its 0.1 s budget"), id="timeout-alarm"),
    pytest.param(
        XsltDynamicError(
            "the transformation exceeded its step budget",
            code=IMPLEMENTATION_BUDGET_CODE,
            allow_catch=False,
        ),
        id="execution-limits",
    ),
)


def _context() -> StaticContext:
    return StaticContext(
        namespaces={},
        version="3.0",
        uri="urn:example:budget-alarm",
    )


def _matcher(pattern: str) -> PatternMatcher:
    return PatternMatcher(compile_pattern(pattern, _context()))


def _first_item(xml: str = "<r><item code='a'/><item code='b'/></r>") -> XdmNode:
    document = XdmDocument.parse(xml.encode("utf-8"))
    return document.root_element.children[0]


def _raise(alarm: BaseException) -> Any:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise alarm

    return fail


# ---------------------------------------------------------------------------
# Pattern evaluation: the reported defect


def test_budget_alarm_predicate_separates_alarms_from_failures() -> None:
    alarm = TimeoutError("budget")
    exhausted = XsltDynamicError("budget", code=IMPLEMENTATION_BUDGET_CODE, allow_catch=False)
    genuine = XsltDynamicError("division by zero", code="FOAR0001")
    # A stylesheet can name any code in ``error-code``; only the engine's own
    # non-catchable limit is an alarm.
    forged = XsltDynamicError("budget", code=IMPLEMENTATION_BUDGET_CODE)

    assert is_budget_alarm(alarm)
    assert is_budget_alarm(exhausted)
    assert not is_budget_alarm(genuine)
    assert not is_budget_alarm(forged)
    assert not is_budget_alarm(RuntimeError("harness defect"))


@pytest.mark.parametrize("alarm", BUDGET_ALARMS)
def test_pattern_evaluation_propagates_the_budget_alarm(
    monkeypatch: pytest.MonkeyPatch, alarm: BaseException
) -> None:
    matcher = _matcher("item[@code = 'a']")
    monkeypatch.setattr("pyxsd.xslt.compiler.patterns._evaluate", _raise(alarm))

    with pytest.raises(type(alarm)) as caught:
        matcher.matches(_first_item(), ExpressionEnvironment())

    assert caught.value is alarm
    assert matcher.statistics.errors_ignored == 0


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(XsltDynamicError("division by zero", code="FOAR0001"), id="coded-error"),
        pytest.param(
            XsltDynamicError("forged budget", code=IMPLEMENTATION_BUDGET_CODE),
            id="forged-budget-code",
        ),
        pytest.param(RuntimeError("matching defect"), id="uncoded-error"),
    ],
)
def test_pattern_failures_remain_non_matches(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    matcher = _matcher("item[@code = 'a']")
    monkeypatch.setattr("pyxsd.xslt.compiler.patterns._evaluate", _raise(failure))

    assert matcher.matches(_first_item(), ExpressionEnvironment()) is False
    assert matcher.statistics.errors_ignored == 1


def test_run_case_records_a_pattern_budget_alarm_as_a_timeout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instrument's verdict: an alarm during matching is a ``timeout``."""
    (tmp_path / "alarm.xsl").write_bytes(
        stylesheet(
            '<xsl:template match="/">'
            '<out><xsl:apply-templates select="r/item"/></out>'
            "</xsl:template>"
            "<xsl:template match=\"item[@code = 'a']\"><one/></xsl:template>"
            '<xsl:template match="item"><other/></xsl:template>'
        )
    )
    case = _TestCase(
        name="synthetic/budget-alarm",
        local_name="budget-alarm",
        test_set="synthetic",
        description="a case whose pattern matching expires the budget",
        source=PurePosixPath("synthetic/_budget-alarm-test-set.xml"),
        environment=Environment(
            sources=(Source(role=".", content="<r><item code='a'/><item code='b'/></r>"),)
        ),
        invocation=Invocation(
            stylesheets=(Stylesheet(role="principal", path=PurePosixPath("alarm.xsl")),)
        ),
        result_xml="<result><assert>true()</assert></result>",
        document_root=tmp_path,
    )
    assert run_case(case, PROFILE).outcome is Outcome.PASS

    evaluated = pattern_module._evaluate

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.4)
        return evaluated(*args, **kwargs)

    monkeypatch.setattr(pattern_module, "_evaluate", slow)
    started = time.monotonic()
    result = run_case(case, PROFILE, timeout=0.1)
    elapsed = time.monotonic() - started

    assert result.outcome is Outcome.TIMEOUT
    assert "budget" in result.message
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# Compilation conversions: the same alarm during a case's compile phase


def test_stylesheet_expression_compile_propagates_the_budget_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    monkeypatch.setattr(XPathEngine, "compile", _raise(alarm))

    with pytest.raises(TimeoutError) as caught:
        XsltProcessor().compile(
            io.BytesIO(
                stylesheet('<xsl:template match="/"><xsl:value-of select="1 + 1"/></xsl:template>')
            )
        )

    assert caught.value is alarm


def test_stylesheet_pattern_compile_propagates_the_budget_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    monkeypatch.setattr(XPathEngine, "compile", _raise(alarm))

    with pytest.raises(TimeoutError) as caught:
        XsltProcessor().compile(io.BytesIO(stylesheet('<xsl:template match="item[@code]"/>')))

    assert caught.value is alarm


def test_static_expression_compile_propagates_the_budget_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    monkeypatch.setattr(XPathEngine, "compile", _raise(alarm))

    with pytest.raises(TimeoutError) as caught:
        XsltProcessor().compile(
            io.BytesIO(stylesheet('<xsl:template match="/"/>', attributes=' use-when="true()"'))
        )

    assert caught.value is alarm


# ---------------------------------------------------------------------------
# XPath engine compile fallback


def test_engine_compile_fallback_does_not_swallow_the_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine's optional seam must not turn an alarm into "no plan"."""
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    monkeypatch.setattr("pyxsd.xpath.continuation.compile_expression", _raise(alarm))

    with pytest.raises(TimeoutError) as caught:
        XPathEngine("3.1").compile("1 + 1", XPathStaticContext())

    assert caught.value is alarm


def test_engine_compile_fallback_still_falls_back_for_a_lowering_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pyxsd.xpath.continuation.compile_expression", _raise(RuntimeError("unsupported"))
    )

    compiled = XPathEngine("3.1").compile("1 + 1", XPathStaticContext())

    assert compiled.continuation_plan is None


# ---------------------------------------------------------------------------
# Foreign I/O boundaries: a policy or destination timeout is the alarm

DOCUMENT_URI = "urn:test:document"
TEXT_URI = "urn:test:text"


class _TimeoutPolicy:
    """A resource policy that raises the caller's alarm for instance and text reads.

    The stylesheet's own module read shares this policy (purpose ``schema``),
    so it serves those bytes back unchanged and fails only where the transform
    loads a source document or a text resource.
    """

    def __init__(self, alarm: BaseException) -> None:
        self.alarm = alarm

    def read(self, source, *, base, purpose) -> ResourceData:
        if purpose in ("instance", "text"):
            raise self.alarm
        stream = getattr(source, "getvalue", None)
        return ResourceData("<memory>", b"" if stream is None else stream())


class _TimeoutDestination:
    """A binary destination whose write raises the caller's alarm."""

    def __init__(self, alarm: BaseException) -> None:
        self.alarm = alarm

    def write(self, data: bytes) -> None:
        raise self.alarm


def test_document_read_propagates_the_budget_alarm() -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f"<out><xsl:value-of select=\"doc('{DOCUMENT_URI}')/a\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=_TimeoutPolicy(alarm)).compile(io.BytesIO(source))

    with pytest.raises(TimeoutError) as caught:
        compiled.transform()

    assert caught.value is alarm


def test_text_read_propagates_the_budget_alarm() -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f"<out><xsl:value-of select=\"unparsed-text('{TEXT_URI}')\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=_TimeoutPolicy(alarm)).compile(io.BytesIO(source))

    with pytest.raises(TimeoutError) as caught:
        compiled.transform()

    assert caught.value is alarm


def test_result_write_propagates_the_budget_alarm() -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    source = stylesheet(
        '<xsl:template name="xsl:initial-template"><primary/></xsl:template>',
        attributes=' exclude-result-prefixes="xs"',
    )
    result = (
        XsltProcessor()
        .compile(io.BytesIO(source))
        .transform(output_uri="https://example.test/out.xml")
    )

    with pytest.raises(TimeoutError) as caught:
        result.write(_TimeoutDestination(alarm))

    assert caught.value is alarm


# ---------------------------------------------------------------------------
# Dynamic expression evaluation: the same alarm converted one layer up


def _identity() -> StaticContextIdentity:
    return StaticContextIdentity(
        xpath_version="3.1",
        namespaces={},
        default_namespace=None,
        base_uri=None,
        variable_types={},
        functions={},
        schema_aware=False,
        default_collation="http://www.w3.org/2005/xpath-functions/collation/codepoint",
        xslt_version="3.0",
        package_name=None,
    )


def test_dynamic_expression_compile_propagates_the_budget_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    monkeypatch.setattr(XPathEngine, "compile", _raise(alarm))

    with pytest.raises(TimeoutError) as caught:
        DynamicExpressionCache().compile("1 + 1", _identity())

    assert caught.value is alarm


def test_dynamic_expression_evaluation_propagates_the_budget_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    cache = DynamicExpressionCache()
    monkeypatch.setattr(XPathEngine, "evaluate", _raise(alarm))

    with pytest.raises(TimeoutError) as caught:
        evaluate_dynamic(
            "1 + 1",
            _identity(),
            ExpressionEnvironment(),
            focus=Focus.absent(),
            cache=cache,
        )

    assert caught.value is alarm


# ---------------------------------------------------------------------------
# The remaining conversions on the evaluation path


@pytest.mark.parametrize("alarm", BUDGET_ALARMS)
def test_accumulator_evaluation_propagates_the_budget_alarm(
    monkeypatch: pytest.MonkeyPatch, alarm: BaseException
) -> None:
    source = stylesheet(
        '<xsl:mode use-accumulators="#all"/>'
        '<xsl:template match="/">'
        "<out><xsl:value-of select=\"accumulator-before('count')\"/></out>"
        "</xsl:template>"
        '<xsl:accumulator name="count" initial-value="0">'
        '<xsl:accumulator-rule match="item" select="$value + 1"/>'
        "</xsl:accumulator>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(source))
    monkeypatch.setattr("pyxsd.xslt.runtime.accumulators.accumulator_rule_matches", _raise(alarm))

    with pytest.raises(type(alarm)) as caught:
        compiled.transform(source=b"<r><item/></r>")

    assert caught.value is alarm


@pytest.mark.parametrize("alarm", BUDGET_ALARMS)
def test_accumulator_error_recovery_does_not_hide_the_alarm(
    monkeypatch: pytest.MonkeyPatch, alarm: BaseException
) -> None:
    """A value already known must not let a later alarm look like success.

    Accumulator evaluation recovers from a failure at a later node by returning
    the requested node's value when it is already computed.  A budget alarm is
    not such a failure, so it must still end the run.
    """
    source = stylesheet(
        '<xsl:mode use-accumulators="#all"/>'
        '<xsl:template match="/">'
        "<out><xsl:value-of select=\"accumulator-before('count')\"/></out>"
        "</xsl:template>"
        '<xsl:accumulator name="count" initial-value="0">'
        '<xsl:accumulator-rule match="item" select="$value + 1"/>'
        "</xsl:accumulator>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(source))

    def rule(invocation, definition, selected, node, old_value):
        if node.string_value == "b":
            raise alarm
        return old_value

    monkeypatch.setattr("pyxsd.xslt.runtime.accumulators.evaluate_accumulator_rule", rule)

    with pytest.raises(type(alarm)) as caught:
        compiled.transform(source=b"<r><item>a</item><item>b</item></r>")

    assert caught.value is alarm


def test_assert_test_evaluation_propagates_the_budget_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    source = stylesheet(
        '<xsl:template name="xsl:initial-template"><xsl:assert test="true()"/></xsl:template>'
    )
    compiled = XsltProcessor(static_options={"assertions": True}).compile(io.BytesIO(source))
    monkeypatch.setattr("pyxsd.xslt.runtime.instructions._effective_boolean", _raise(alarm))

    with pytest.raises(TimeoutError) as caught:
        compiled.transform()

    assert caught.value is alarm


def test_assert_message_construction_propagates_the_budget_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<xsl:assert test="false()">detail</xsl:assert>'
        "</xsl:template>"
    )
    compiled = XsltProcessor(static_options={"assertions": True}).compile(io.BytesIO(source))
    monkeypatch.setattr("pyxsd.xslt.runtime.instructions._build_result_tree", _raise(alarm))

    with pytest.raises(TimeoutError) as caught:
        compiled.transform()

    assert caught.value is alarm


def test_assert_error_code_selection_propagates_the_budget_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<xsl:assert test="false()" error-code="\'XTMM9001\'"/>'
        "</xsl:template>"
    )
    compiled = XsltProcessor(static_options={"assertions": True}).compile(io.BytesIO(source))
    monkeypatch.setattr("pyxsd.xslt.runtime.instructions._diagnostic_error_code", _raise(alarm))

    with pytest.raises(TimeoutError) as caught:
        compiled.transform()

    assert caught.value is alarm


def test_continuation_plan_failure_does_not_swallow_the_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed plan compile falls back to the evaluator, but not for an alarm."""
    expression = XPathEngine("3.1").compile("1 + 1", XPathStaticContext())
    alarm = TimeoutError("the case exceeded its 0.1 s budget")
    monkeypatch.setattr("pyxsd.xpath.continuation.compile_expression", _raise(alarm))

    with pytest.raises(TimeoutError) as caught:
        Transformation().xpath_plan(expression)

    assert caught.value is alarm
