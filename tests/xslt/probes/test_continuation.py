"""Contract tests for the continuation seam and the Task 5 gate.

The seam is deliberately a compiled-expression layer: elementpath's evaluator
cannot suspend, so the tests prove that the supported expression subset keeps
pending operands on an explicit heap stack while a host call runs a nested
XPath evaluation.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from elementpath import XPathContext
from elementpath.xpath3 import XPath31Parser

from pyxsd.xpath import continuation
from pyxsd.xpath.capabilities import CapabilityOutcome
from pyxsd.xpath.continuation import (
    CompiledExpression,
    ContinuationError,
    ContinuationMachine,
    ContinuationProbe,
    HostFunction,
    Invoke,
    LetPlan,
    LiteralPlan,
    OperatorPlan,
    TailCall,
    TailControlLeak,
    UnsupportedContinuationExpression,
    VariablePlan,
    XPathClosure,
    XPathEvaluationError,
    compile_expression,
    evaluate,
    evaluate_closure,
    probe_mixed_recursion,
)

### Gate


def test_continuation_probe() -> None:
    recursion_limit = sys.getrecursionlimit()

    outcome = probe_mixed_recursion(depth=10_000)

    assert outcome.status == "pass", outcome.diagnostic
    assert "RecursionError" not in outcome.diagnostic
    assert outcome.details["tail_marker_leaked"] is False
    assert sys.getrecursionlimit() == recursion_limit


def test_continuation_probe_metadata_and_checks() -> None:
    outcome = probe_mixed_recursion(depth=1)

    assert isinstance(outcome, CapabilityOutcome)
    assert isinstance(outcome, ContinuationProbe)
    assert outcome.name == "mixed-continuation"
    assert outcome.xpath_version == "3.1"
    assert outcome.parser == "XPath31Parser"
    assert outcome.parser_version == "3.1"
    assert outcome.evaluator == "pyxsd.xpath.continuation"
    assert outcome.evaluator_version == "1"
    assert outcome.diagnostic == outcome.detail
    assert outcome.details["depth"] == 1
    assert outcome.details["max_python_depth"] <= continuation.PYTHON_DEPTH_BUDGET
    assert outcome.details["checks"] == {
        "pending_arithmetic": True,
        "higher_order_xpath": True,
        "higher_order_host": True,
        "mutual_recursion": True,
        "error_propagation": True,
    }
    assert outcome.as_dict()["details"]["depth"] == 1
    assert json.loads(json.dumps(outcome.as_dict()))["details"]["checks"]["mutual_recursion"]
    assert continuation._plain_value(("a", {"b": [1]})) == ["a", {"b": [1]}]


def test_probe_details_are_immutable() -> None:
    outcome = probe_mixed_recursion(depth=2)

    with pytest.raises(TypeError):
        outcome.details["depth"] = 3  # type: ignore[index]


@pytest.mark.parametrize("depth", [0, -1, True, 1.5, "10"])
def test_probe_rejects_invalid_depth(depth: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        probe_mixed_recursion(depth=depth)


def test_probe_reports_recursion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def recurse(_: int) -> continuation._ProbeRun:
        raise RecursionError("the Python stack overflowed")

    monkeypatch.setattr(continuation, "_run_probe", recurse)

    outcome = probe_mixed_recursion(depth=5)

    assert outcome.status == "defect"
    assert "RecursionError" in outcome.detail
    assert outcome.details["tail_marker_leaked"] is False


def test_probe_reports_compiler_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    def compile_gap(_: int) -> continuation._ProbeRun:
        raise UnsupportedContinuationExpression("no continuation plan")

    monkeypatch.setattr(continuation, "_run_probe", compile_gap)

    outcome = probe_mixed_recursion(depth=5)

    assert outcome.status == "adapter-requirement"
    assert "continuation plan" in outcome.detail


def test_probe_reports_unexpected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(_: int) -> continuation._ProbeRun:
        raise RuntimeError("boom")

    monkeypatch.setattr(continuation, "_run_probe", crash)

    outcome = probe_mixed_recursion(depth=5)

    assert outcome.status == "defect"
    assert "RuntimeError: boom" in outcome.detail


def _probe_run(
    *,
    result: object = 2,
    expected: object = 2,
    checks: dict[str, bool] | None = None,
    max_python_depth: int = 10,
    recursion_limit: int | None = None,
) -> continuation._ProbeRun:
    return continuation._ProbeRun(
        result=result,
        expected=expected,
        checks=continuation.MappingProxyType(
            checks or {"pending_arithmetic": True},
        ),
        max_python_depth=max_python_depth,
        recursion_limit=sys.getrecursionlimit() if recursion_limit is None else recursion_limit,
    )


def test_probe_reports_wrong_recursive_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(continuation, "_run_probe", lambda _: _probe_run(result=1, expected=2))

    outcome = probe_mixed_recursion(depth=2)

    assert outcome.status == "defect"
    assert "returned 1" in outcome.detail


def test_probe_reports_failed_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        continuation,
        "_run_probe",
        lambda _: _probe_run(checks={"pending_arithmetic": False, "mutual_recursion": False}),
    )

    outcome = probe_mixed_recursion(depth=2)

    assert outcome.status == "defect"
    assert "mutual_recursion, pending_arithmetic" in outcome.detail


def test_probe_reports_tail_control_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    leaked = [TailCall(HostFunction(lambda: 1))]
    monkeypatch.setattr(
        continuation,
        "_run_probe",
        lambda _: _probe_run(result=leaked, expected=leaked),
    )

    outcome = probe_mixed_recursion(depth=2)

    assert outcome.status == "defect"
    assert "tail-control" in outcome.detail
    assert outcome.details["tail_marker_leaked"] is True


def test_probe_reports_changed_recursion_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(continuation, "_run_probe", lambda _: _probe_run(recursion_limit=0))

    outcome = probe_mixed_recursion(depth=2)

    assert outcome.status == "defect"
    assert "recursion limit" in outcome.detail


def test_probe_reports_excessive_python_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        continuation,
        "_run_probe",
        lambda _: _probe_run(max_python_depth=continuation.PYTHON_DEPTH_BUDGET + 1),
    )

    outcome = probe_mixed_recursion(depth=2)

    assert outcome.status == "defect"
    assert "frame depth" in outcome.detail


def test_probe_builds_an_outcome_without_optional_details() -> None:
    outcome = continuation._probe_outcome("defect", "broken", depth=3)

    assert outcome.details == {"depth": 3, "tail_marker_leaked": False}


### Compiler and supported semantics


@pytest.mark.parametrize(
    ("source", "variable_types", "variables"),
    [
        ("1 + 2", {}, {}),
        ("5 - 2", {}, {}),
        ("3 * 4", {}, {}),
        ("3 div 2", {}, {}),
        ("5 idiv 2", {}, {}),
        ("5 mod 2", {}, {}),
        ("3 = 3", {}, {}),
        ("3 != 4", {}, {}),
        ("3 < 4", {}, {}),
        ("3 > 4", {}, {}),
        ("3 <= 3", {}, {}),
        ("3 >= 4", {}, {}),
        ("1.5 + 1.5", {}, {}),
        ("'ab'", {}, {}),
        ("true()", {}, {}),
        ("false()", {}, {}),
        ("$n - 1", {"n": "xs:integer"}, {"n": 5}),
        ("$n * 3", {"n": "xs:integer"}, {"n": 5}),
        ("+-$n", {"n": "xs:integer"}, {"n": 5}),
        ("($n)", {"n": "xs:integer"}, {"n": 7}),
        ("if ($n = 0) then 10 else 20", {"n": "xs:integer"}, {"n": 0}),
        ("if ($n = 0) then 10 else 20", {"n": "xs:integer"}, {"n": 1}),
        ("if ($n) then 10 else 20", {"n": "xs:integer"}, {"n": 0}),
        ("if ($n) then 10 else 20", {"n": "xs:integer"}, {"n": 2}),
        ("let $x := 1, $y := $x + 1 return $y", {}, {}),
        ("let $a := 2 return (function($x){$x + $a})(3)", {}, {}),
        ("(function($x){$x + 1})(2)", {}, {}),
        ("(function($x){$x + $n})($m)", {"n": "xs:integer", "m": "xs:integer"}, {"n": 2, "m": 3}),
        ("(function(){41})()", {}, {}),
    ],
)
def test_supported_expressions_match_elementpath(
    source: str,
    variable_types: dict[str, str],
    variables: dict[str, object],
) -> None:
    compiled = compile_expression(source, variable_types=variable_types)
    parser = XPath31Parser(variable_types=variable_types)
    expected = parser.parse(source).evaluate(XPathContext(item="", variables=variables))

    assert evaluate(compiled, variables) == expected


def test_compiled_expression_is_immutable_and_reusable() -> None:
    compiled = compile_expression("$n + 1", variable_types={"n": "xs:integer"})

    with pytest.raises(dataclasses.FrozenInstanceError):
        compiled.source = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        compiled.variable_types["n"] = "xs:string"  # type: ignore[index]
    assert evaluate(compiled, {"n": 1}) == 2
    assert evaluate(compiled, {"n": 5}) == 6

    machine = ContinuationMachine(compiled, {"n": 1})
    assert machine.started is False
    assert machine.run() == 2
    assert machine.started is True
    with pytest.raises(ContinuationError, match="only once"):
        machine.run()


def test_closure_environment_is_frozen() -> None:
    compiled = compile_expression("function($x) { $x + $n }", variable_types={"n": "xs:integer"})
    closure = evaluate(compiled, {"n": 2})

    assert isinstance(closure, XPathClosure)
    assert closure.arity == 1
    with pytest.raises(TypeError):
        closure.environment["n"] = 3  # type: ignore[index]
    assert evaluate_closure(closure, (3,)) == 5


def test_zero_argument_and_grouping_calls() -> None:
    host = HostFunction(lambda: 41, arity=0)
    host_call = compile_expression("$host()", variable_types={"host": "function(*)"})
    assert evaluate(host_call, {"host": host}) == 41

    grouping = compile_expression("($n)", variable_types={"n": "xs:integer"})
    assert evaluate(grouping, {"n": 7}) == 7

    closure_group = compile_expression("($f)", variable_types={"f": "function(*)"})
    closure = evaluate(closure_group, {"f": HostFunction(lambda: 3, arity=0)})
    assert isinstance(closure, HostFunction)

    multi_argument = compile_expression("$host(1, 2, 3)", variable_types={"host": "function(*)"})
    assert evaluate(multi_argument, {"host": HostFunction(lambda *args: sum(args))}) == 6


@pytest.mark.parametrize(
    "source",
    ["//a", "count((1, 2))", "1 to 3", "()", "xs:integer('3')"],
)
def test_compiler_rejects_unsupported_constructs(source: str) -> None:
    with pytest.raises(UnsupportedContinuationExpression):
        compile_expression(source)


@pytest.mark.parametrize("source", ["", "   "])
def test_compiler_rejects_blank_source(source: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        compile_expression(source)


def test_compiler_reports_syntax_errors() -> None:
    with pytest.raises(Exception, match="unexpected end of source"):
        compile_expression("1 +")


class _FakeInlineToken:
    symbol = "function"
    varnames: Any = None
    body: Any = None

    def __init__(self, varnames: Any, body: Any) -> None:
        self.varnames = varnames
        self.body = body

    def __iter__(self) -> Any:
        return iter(())


_FakeInlineToken.__name__ = "_InlineFunction"


class _FakeLetToken:
    symbol = "let"


_FakeLetToken.__name__ = "_LetExpression"


class _FakeLetWithLength(_FakeLetToken):
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index: int) -> Any:
        return self._items[index]


_FakeLetWithLength.__name__ = "_LetExpression"


def test_compiler_defensive_inline_and_let_branches() -> None:
    compiler = continuation._Compiler(XPath31Parser())
    literal = XPath31Parser().parse("1")

    with pytest.raises(UnsupportedContinuationExpression, match="requires a body"):
        compiler.compile(_FakeInlineToken(None, None))
    with pytest.raises(UnsupportedContinuationExpression, match="parameters must be names"):
        compiler.compile(_FakeInlineToken([1], literal))

    class _NameToken:
        value = 1

    with pytest.raises(UnsupportedContinuationExpression, match="requires a variable"):
        compiler.compile(_FakeLetWithLength([_NameToken(), literal, literal]))
    with pytest.raises(UnsupportedContinuationExpression, match="requires a binding"):
        compiler.compile(_FakeLetWithLength([literal]))


def test_plan_validation_and_host_protocol_validation() -> None:
    control = TailCall(HostFunction(lambda: 1))
    with pytest.raises(TailControlLeak):
        LiteralPlan(control)
    with pytest.raises(TailControlLeak):
        LiteralPlan([control])
    assert LiteralPlan([1, (2, 3)]).value == [1, (2, 3)]
    with pytest.raises(ValueError, match="name"):
        VariablePlan("")
    with pytest.raises(ValueError, match="one or two"):
        OperatorPlan(int, "+", "operator", ())
    with pytest.raises(ValueError, match="one or two"):
        OperatorPlan(int, "+", "operator", (LiteralPlan(1), LiteralPlan(2), LiteralPlan(3)))
    with pytest.raises(ValueError, match="binding"):
        LetPlan((), LiteralPlan(1))

    with pytest.raises(TypeError, match="callable"):
        HostFunction(42)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative"):
        HostFunction(lambda: 1, arity=-1)
    with pytest.raises(TypeError, match="continuation must be callable"):
        Invoke(HostFunction(lambda: 1), then=42)  # type: ignore[arg-type]

    tail = TailCall(HostFunction(lambda: 1), [1, 2])  # type: ignore[arg-type]
    assert tail.arguments == (1, 2)
    invoke = Invoke(HostFunction(lambda: 1), [1])  # type: ignore[arg-type]
    assert invoke.arguments == (1,)


def test_empty_compiled_expression_is_rejected() -> None:
    with pytest.raises(ValueError, match="source text"):
        CompiledExpression("", LiteralPlan(1))


### Host control values and errors


def test_tail_call_chain_is_consumed() -> None:
    second = HostFunction(lambda value: value + 1, name="second", arity=1)
    first = HostFunction(lambda value: TailCall(second, (value,)), name="first", arity=1)
    compiled = compile_expression("$first(4)", variable_types={"first": "function(*)"})

    assert evaluate(compiled, {"first": first}) == 5


def test_invoke_continuation_resumes_host_arithmetic() -> None:
    def apply(closure: object, value: object) -> object:
        return Invoke(closure, (value,), lambda result: result + 1)  # type: ignore[operator]

    compiled = compile_expression(
        "$apply(function($x){$x + 1}, 4)",
        variable_types={"apply": "function(*)"},
    )

    assert evaluate(compiled, {"apply": HostFunction(apply, arity=2)}) == 6


def test_invoke_continuation_may_return_another_control_value() -> None:
    final = HostFunction(lambda: 9, name="final", arity=0)

    def apply(closure: object, value: object) -> object:
        return Invoke(closure, (value,), lambda _: TailCall(final))

    compiled = compile_expression(
        "$apply(function($x){$x + 1}, 4)",
        variable_types={"apply": "function(*)"},
    )

    assert evaluate(compiled, {"apply": HostFunction(apply, arity=2)}) == 9


def test_invoke_continuation_cannot_leak_nested_control_value() -> None:
    def apply(closure: object, value: object) -> object:
        return Invoke(closure, (value,), lambda _: [TailCall(HostFunction(lambda: 1))])

    compiled = compile_expression(
        "$apply(function($x){$x + 1}, 4)",
        variable_types={"apply": "function(*)"},
    )

    with pytest.raises(TailControlLeak):
        evaluate(compiled, {"apply": HostFunction(apply, arity=2)})


def test_nested_tail_control_in_a_host_result_is_rejected() -> None:
    host = HostFunction(lambda: [TailCall(HostFunction(lambda: 1))], arity=0)
    compiled = compile_expression("$host()", variable_types={"host": "function(*)"})

    with pytest.raises(TailControlLeak):
        evaluate(compiled, {"host": host})


def test_tail_call_to_a_non_function_is_rejected() -> None:
    host = HostFunction(lambda: TailCall(42), arity=0)
    compiled = compile_expression("$host()", variable_types={"host": "function(*)"})

    with pytest.raises(XPathEvaluationError, match="XPTY0004"):
        evaluate(compiled, {"host": host})


def test_invoke_of_a_non_function_is_rejected() -> None:
    host = HostFunction(lambda: Invoke(42), arity=0)
    compiled = compile_expression("$host()", variable_types={"host": "function(*)"})

    with pytest.raises(XPathEvaluationError, match="XPTY0004"):
        evaluate(compiled, {"host": host})


def test_host_error_propagates_and_a_fresh_machine_recovers() -> None:
    def explode() -> object:
        raise XPathEvaluationError("FOAR0001", "host exploded")

    compiled = compile_expression("$explode() + 1", variable_types={"explode": "function(*)"})

    with pytest.raises(XPathEvaluationError) as caught:
        evaluate(compiled, {"explode": HostFunction(explode, arity=0)})
    assert caught.value.code == "FOAR0001"
    assert "host exploded" in str(caught.value)

    assert evaluate(compile_expression("1 + 1")) == 2


def test_elementpath_operator_error_propagates() -> None:
    compiled = compile_expression("1 div $zero", variable_types={"zero": "xs:integer"})

    with pytest.raises(Exception) as caught:
        evaluate(compiled, {"zero": 0})
    assert getattr(caught.value, "code", "").rsplit(":", 1)[-1] == "FOAR0001"


def test_missing_variable_and_non_function_callee() -> None:
    compiled = compile_expression("$n + 1", variable_types={"n": "xs:integer"})
    with pytest.raises(XPathEvaluationError, match="XPST0008"):
        evaluate(compiled)

    call = compile_expression("$n(1)", variable_types={"n": "function(*)"})
    with pytest.raises(XPathEvaluationError, match="XPTY0004"):
        evaluate(call, {"n": 5})


def test_arity_checks_for_host_and_inline_functions() -> None:
    host = HostFunction(lambda: 1, arity=0)
    compiled = compile_expression("$host(1)", variable_types={"host": "function(*)"})
    with pytest.raises(XPathEvaluationError, match="expects 0 arguments"):
        evaluate(compiled, {"host": host})

    untyped = HostFunction(lambda *args: len(args))
    compiled = compile_expression("$host(1, 2)", variable_types={"host": "function(*)"})
    assert evaluate(compiled, {"host": untyped}) == 2

    closure = evaluate(compile_expression("function($x) { $x }"))
    assert isinstance(closure, XPathClosure)
    call = compile_expression("$f(1, 2)", variable_types={"f": "function(*)"})
    with pytest.raises(XPathEvaluationError, match="inline function expects 1"):
        evaluate(call, {"f": closure})


### Continuation behavior and concurrency


def test_mixed_recursion_is_stack_safe_at_ten_thousand() -> None:
    result, depth = continuation._mixed_recursion(10_000)

    assert result == 10_000
    assert depth <= continuation.PYTHON_DEPTH_BUDGET


def test_mutual_recursion_is_stack_safe() -> None:
    result, depth = continuation._mutual_recursion(10_000)
    assert result is False
    assert depth <= continuation.PYTHON_DEPTH_BUDGET

    result, depth = continuation._mutual_recursion(10_001)
    assert result is True
    assert depth <= continuation.PYTHON_DEPTH_BUDGET


def test_error_propagation_through_pending_frames() -> None:
    recovered, depth = continuation._error_propagation(1_000)

    assert recovered is True
    assert depth <= continuation.PYTHON_DEPTH_BUDGET


def _thread_worker(index: int, barrier: Barrier) -> tuple[str, int]:
    try:
        barrier.wait()
        outcome = probe_mixed_recursion(depth=50 + index)
        return outcome.status, int(outcome.details["depth"])
    except BaseException:
        barrier.abort()
        raise


def test_simultaneous_probes_preserve_independent_state() -> None:
    workers = 4
    barrier = Barrier(workers)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_thread_worker, index, barrier) for index in range(workers)]
        results = [future.result() for future in futures]

    assert results == [("pass", 50 + index) for index in range(workers)]


def test_shared_compiled_plans_preserve_thread_local_invocation_state() -> None:
    workers = 4
    barrier = Barrier(workers)
    compiled = compile_expression(
        "function($n) { if ($n = 0) then 0 else ($recurse($n - 1) + 1) }",
        variable_types={"recurse": "function(*)"},
    )

    def worker(index: int) -> int:
        try:
            holder: dict[str, XPathClosure] = {}

            def recurse(value: object) -> object:
                return TailCall(holder["closure"], (value,))

            closure = evaluate(compiled, {"recurse": HostFunction(recurse, arity=1)})
            assert isinstance(closure, XPathClosure)
            holder["closure"] = closure
            barrier.wait()
            return int(ContinuationMachine.for_closure(closure, (100 + index,)).run())
        except BaseException:
            barrier.abort()
            raise

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, index) for index in range(workers)]
        results = [future.result() for future in futures]

    assert results == [100 + index for index in range(workers)]
