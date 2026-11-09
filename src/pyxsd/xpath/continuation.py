"""Continuation-safe evaluation seam for mixed XPath and XSLT recursion.

elementpath 5.1.4 evaluates XPath expressions with synchronous Python calls.
A stylesheet function invoked from XPath therefore grows the Python call stack
by one evaluator frame per mixed recursion step, and no public hook suspends
an evaluation before the call.  Raising an exception unwinds the evaluator and
loses its pending operands.

This module keeps that limitation explicit and contains a substrate layer that
does provide the missing seam.  ``compile_expression()`` parses with the
installed elementpath 3.1 parser and lowers a supported expression subset to
an immutable :class:`Plan`.  :class:`ContinuationMachine` evaluates a plan with
an explicit heap-resident continuation stack; an XPath operand waiting for a
host (XSLT) call is a continuation frame, not a Python frame.  A host call may
return a :class:`TailCall` or :class:`Invoke` control value, which the machine
consumes before any value reaches XPath.  Tail-control values are rejected if
they are nested in a host result.

Supported constructs are literals, variable references, arithmetic and
comparison operators, ``if``, ``let``, inline functions, and dynamic calls.
Operator semantics are delegated to the same elementpath operator classes the
parser produced, so the supported subset evaluates identically to elementpath.
Node paths, predicates, FLWOR expressions, sequence constructors, and static
function calls are rejected at compile time instead of being approximated; the
production compiler (later stages) must extend this subset before the engine
uses the seam for those expressions.

``probe_mixed_recursion()`` exercises the seam at 10,000 mixed steps and
reports the result as a Task 4 :class:`CapabilityOutcome` subtype.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from elementpath import XPathContext
from elementpath.xpath3 import XPath31Parser
from elementpath.xpath_tokens import XPathArray, XPathMap
from elementpath.xpath_tokens.tokens import ValueToken

from pyxsd.xpath.capabilities import CapabilityOutcome

__all__ = [
    "PYTHON_DEPTH_BUDGET",
    "CallPlan",
    "CompiledExpression",
    "ContinuationError",
    "ContinuationMachine",
    "ContinuationProbe",
    "HostFunction",
    "IfPlan",
    "InlineFunctionPlan",
    "Invoke",
    "LetPlan",
    "LiteralPlan",
    "OperatorPlan",
    "Plan",
    "TailCall",
    "TailControlLeak",
    "UnsupportedContinuationExpression",
    "VariablePlan",
    "XPathClosure",
    "XPathEvaluationError",
    "compile_expression",
    "evaluate",
    "evaluate_closure",
    "probe_mixed_recursion",
]


class ContinuationError(Exception):
    """Base class for seam failures that are not elementpath errors."""


class UnsupportedContinuationExpression(ContinuationError):
    """The expression uses a construct the continuation compiler cannot lower."""


class XPathEvaluationError(ContinuationError):
    """An XPath dynamic or type error raised by the continuation seam."""

    code: str

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        detail = f"[{code}] {message}" if message else f"[{code}]"
        super().__init__(detail)


class TailControlLeak(ContinuationError):
    """A host tail-control value escaped as an XDM result."""


@dataclass(frozen=True)
class HostFunction:
    """A host function callback reachable from XPath as a function item.

    The callback is called by :class:`ContinuationMachine` with one Python
    frame.  It returns either an XDM value, a :class:`TailCall`, or an
    :class:`Invoke`.  It must never call back into a machine synchronously;
    request nested work through the control values instead.
    """

    callback: Callable[..., object]
    name: str = ""
    arity: int | None = None

    def __post_init__(self) -> None:
        if not callable(self.callback):
            raise TypeError("a host function callback must be callable")
        if self.arity is not None and self.arity < 0:
            raise ValueError("a host function arity must be non-negative")


@dataclass(frozen=True)
class TailCall:
    """Replace the current host invocation with *target* and *arguments*."""

    target: object
    arguments: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", tuple(self.arguments))


@dataclass(frozen=True)
class Invoke:
    """Invoke *target* and pass its result to *then* before returning."""

    target: object
    arguments: tuple[object, ...] = ()
    then: Callable[[object], object] = lambda value: value

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", tuple(self.arguments))
        if not callable(self.then):
            raise TypeError("an invocation continuation must be callable")


class Plan:
    """An immutable compiled expression description."""

    __slots__ = ()


@dataclass(frozen=True)
class LiteralPlan(Plan):
    """A constant XDM value."""

    value: object

    def __post_init__(self) -> None:
        _reject_nested_control(self.value, "literal plan")


@dataclass(frozen=True)
class VariablePlan(Plan):
    """A reference to a variable in the invocation environment."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a variable plan requires a name")


@dataclass(frozen=True)
class OperatorPlan(Plan):
    """A unary or binary elementpath operator with compiled operands."""

    operator_class: type[Any]
    symbol: str
    label: object
    operands: tuple[Plan, ...]

    def __post_init__(self) -> None:
        if len(self.operands) not in (1, 2):
            raise ValueError("an operator plan requires one or two operands")


@dataclass(frozen=True)
class IfPlan(Plan):
    """An XPath ``if`` expression."""

    condition: Plan
    then: Plan
    otherwise: Plan


@dataclass(frozen=True)
class LetPlan(Plan):
    """An XPath ``let`` expression with sequential bindings."""

    bindings: tuple[tuple[str, Plan], ...]
    body: Plan

    def __post_init__(self) -> None:
        if not self.bindings:
            raise ValueError("a let plan requires at least one binding")


@dataclass(frozen=True)
class InlineFunctionPlan(Plan):
    """An inline XPath function definition."""

    parameters: tuple[str, ...]
    body: Plan


@dataclass(frozen=True)
class CallPlan(Plan):
    """A dynamic XPath function call."""

    callee: Plan
    arguments: tuple[Plan, ...]


@dataclass(frozen=True)
class CompiledExpression:
    """An immutable compiled expression, reusable across invocations."""

    source: str
    plan: Plan
    variable_types: Mapping[str, str] = field(default_factory=dict)
    parser_class: type[Any] = XPath31Parser
    parser_version: str = "3.1"

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("a compiled expression requires source text")
        object.__setattr__(self, "variable_types", MappingProxyType(dict(self.variable_types)))


@dataclass(frozen=True)
class XPathClosure:
    """An inline function value with its captured lexical environment."""

    parameters: tuple[str, ...]
    body: Plan
    environment: Mapping[str, object]
    origin: CompiledExpression

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    @property
    def arity(self) -> int:
        return len(self.parameters)


_OPERATOR_SYMBOLS = frozenset(
    {"+", "-", "*", "div", "idiv", "mod", "=", "!=", "<", ">", "<=", ">="}
)


def _control_candidates(value: object) -> Iterator[object]:
    """Yield every nested value the seam must inspect for tail-control values.

    Mappings and the elementpath XDM map/array token types are scanned by their
    items rather than by their (empty or token-only) sequence iteration.
    Text scalars are skipped; every other collection is iterated, so an
    arbitrary host-returned container cannot hide a control value.
    """
    if isinstance(value, (Mapping, XPathMap)):
        for key, item in value.items():
            yield key
            yield item
    elif isinstance(value, XPathArray):
        yield from value.items()
    elif isinstance(value, (str, bytes, bytearray)):
        return
    elif isinstance(value, Collection):
        yield from value


def _reject_nested_control(value: object, label: str) -> None:
    if isinstance(value, (TailCall, Invoke)):
        raise TailControlLeak(f"tail control value escaped as {label}")
    for item in _control_candidates(value):
        _reject_nested_control(item, label)


class _Compiler:
    """Lower an elementpath token tree to a continuation plan."""

    def __init__(self, parser: Any) -> None:
        self._parser = parser

    def compile(self, token: Any) -> Plan:
        symbol = token.symbol
        class_name = type(token).__name__

        if class_name == "_IntegerLiteral":
            return LiteralPlan(int(token.value))
        if class_name == "_DecimalLiteral":
            return LiteralPlan(Decimal(token.value))
        if class_name == "_StringLiteral":
            return LiteralPlan(str(token.value))
        if class_name == "_TrueFunction":
            return LiteralPlan(True)
        if class_name == "_FalseFunction":
            return LiteralPlan(False)
        if class_name == "VariableToken":
            return VariablePlan(str(token.value))
        if class_name == "_IfExpression":
            return IfPlan(self.compile(token[0]), self.compile(token[1]), self.compile(token[2]))
        if class_name == "_LetExpression":
            return self._compile_let(token)
        if class_name == "_InlineFunction":
            return self._compile_inline(token)
        if class_name == "_LeftParenthesisExpression":
            return self._compile_parenthesized(token)
        if symbol in _OPERATOR_SYMBOLS and len(token) in (1, 2):
            return OperatorPlan(
                type(token), str(symbol), token.label, tuple(self.compile(t) for t in token)
            )
        raise UnsupportedContinuationExpression(
            f"unsupported XPath construct {class_name} ({symbol!r})"
        )

    def _compile_let(self, token: Any) -> Plan:
        bindings: list[tuple[str, Plan]] = []
        last = len(token) - 1
        for index in range(0, last, 2):
            name = token[index].value
            if not isinstance(name, str):
                raise UnsupportedContinuationExpression("a let binding requires a variable")
            bindings.append((name, self.compile(token[index + 1])))
        if not bindings:
            raise UnsupportedContinuationExpression("a let expression requires a binding")
        return LetPlan(tuple(bindings), self.compile(token[last]))

    def _compile_inline(self, token: Any) -> Plan:
        body = getattr(token, "body", None)
        if body is None:
            raise UnsupportedContinuationExpression("an inline function requires a body")
        parameters = tuple(token.varnames or (child.value for child in token))
        if not all(isinstance(name, str) for name in parameters):
            raise UnsupportedContinuationExpression("inline function parameters must be names")
        return InlineFunctionPlan(parameters, self.compile(body))

    def _compile_parenthesized(self, token: Any) -> Plan:
        if len(token) == 0:
            raise UnsupportedContinuationExpression("empty sequences are outside the seam subset")
        if len(token) == 1 and token[0].span[0] > token.span[0]:
            return self.compile(token[0])
        callee = self.compile(token[0])
        arguments: list[Plan] = []
        for child in token[1:]:
            arguments.extend(self._argument_plans(child))
        return CallPlan(callee, tuple(arguments))

    def _argument_plans(self, token: Any) -> list[Plan]:
        if token.symbol == ",":
            return self._argument_plans(token[0]) + self._argument_plans(token[1])
        return [self.compile(token)]


def compile_expression(
    source: str,
    *,
    variable_types: Mapping[str, str] | None = None,
) -> CompiledExpression:
    """Compile *source* with elementpath and lower it to a continuation plan."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("an expression source must be a non-empty string")
    types = dict(variable_types or {})
    parser = XPath31Parser(variable_types=types)
    token = parser.parse(source)
    return CompiledExpression(
        source=source,
        plan=_Compiler(parser).compile(token),
        variable_types=types,
    )


### Evaluation machine


@dataclass(frozen=True)
class _EvalAction:
    plan: Plan
    environment: Mapping[str, object]


@dataclass(frozen=True)
class _InvokeAction:
    target: object
    arguments: tuple[object, ...]


class _Frame:
    __slots__ = ()


@dataclass(frozen=True)
class _OperatorFrame(_Frame):
    plan: OperatorPlan
    remaining: tuple[Plan, ...]
    values: tuple[object, ...]
    environment: Mapping[str, object]


@dataclass(frozen=True)
class _IfFrame(_Frame):
    then: Plan
    otherwise: Plan
    environment: Mapping[str, object]


@dataclass(frozen=True)
class _LetFrame(_Frame):
    bindings: tuple[tuple[str, Plan], ...]
    index: int
    environment: Mapping[str, object]
    body: Plan


@dataclass(frozen=True)
class _CallCalleeFrame(_Frame):
    arguments: tuple[Plan, ...]
    environment: Mapping[str, object]


@dataclass(frozen=True)
class _CallArgumentsFrame(_Frame):
    callee: object
    remaining: tuple[Plan, ...]
    values: tuple[object, ...]
    environment: Mapping[str, object]


@dataclass(frozen=True)
class _ResumeTarget:
    frame: _Frame


@dataclass(frozen=True)
class _TransformTarget:
    transform: Callable[[object], object]


@dataclass(frozen=True)
class _DeliverAction:
    target: _ResumeTarget | _TransformTarget
    value: object


#: A Python frame depth above this value proves the seam accumulated frames.
PYTHON_DEPTH_BUDGET = 200


def _python_frame_depth() -> int:
    frame: Any = sys._getframe(1)
    depth = 0
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


class ContinuationMachine:
    """Evaluate one compiled expression or closure on an explicit stack."""

    def __init__(
        self,
        compiled: CompiledExpression,
        variables: Mapping[str, object] | None = None,
    ) -> None:
        self._compiled = compiled
        self._parser: Any = compiled.parser_class()
        self._context = XPathContext(item="")
        self._actions: list[object] = [
            _EvalAction(compiled.plan, dict(variables or {})),
        ]
        self._result: object = None
        self._started = False
        self.max_python_depth = 0

    @classmethod
    def for_closure(
        cls,
        closure: XPathClosure,
        arguments: Sequence[object],
    ) -> ContinuationMachine:
        """Create a machine that invokes an already-created XPath closure."""
        machine = cls(closure.origin)
        machine._actions = [_InvokeAction(closure, tuple(arguments))]
        return machine

    @property
    def started(self) -> bool:
        """Whether :meth:`run` has already been called."""
        return self._started

    def run(self) -> object:
        """Run the machine to completion, returning one XDM result value."""
        if self._started:
            raise ContinuationError("a continuation machine can run only once")
        self._started = True
        while self._actions:
            action = self._actions.pop()
            if isinstance(action, _EvalAction):
                self._evaluate(action.plan, action.environment)
            elif isinstance(action, _InvokeAction):
                self._invoke(action.target, action.arguments)
            else:
                assert isinstance(action, _DeliverAction)
                self._deliver_to(action.target, action.value)
        result = self._result
        _reject_nested_control(result, "XDM result")
        return result

    def _evaluate(self, plan: Plan, environment: Mapping[str, object]) -> None:
        if isinstance(plan, LiteralPlan):
            self._deliver(plan.value)
        elif isinstance(plan, VariablePlan):
            if plan.name not in environment:
                raise XPathEvaluationError("XPST0008", f"unknown variable ${plan.name}")
            self._deliver(environment[plan.name])
        elif isinstance(plan, IfPlan):
            self._actions.append(_ResumeTarget(_IfFrame(plan.then, plan.otherwise, environment)))
            self._actions.append(_EvalAction(plan.condition, environment))
        elif isinstance(plan, LetPlan):
            self._actions.append(_ResumeTarget(_LetFrame(plan.bindings, 0, environment, plan.body)))
            self._actions.append(_EvalAction(plan.bindings[0][1], environment))
        elif isinstance(plan, InlineFunctionPlan):
            self._deliver(XPathClosure(plan.parameters, plan.body, environment, self._compiled))
        elif isinstance(plan, CallPlan):
            self._actions.append(_ResumeTarget(_CallCalleeFrame(plan.arguments, environment)))
            self._actions.append(_EvalAction(plan.callee, environment))
        else:
            assert isinstance(plan, OperatorPlan)
            first, *remaining = plan.operands
            self._actions.append(
                _ResumeTarget(_OperatorFrame(plan, tuple(remaining), (), environment))
            )
            self._actions.append(_EvalAction(first, environment))

    def _resume(self, frame: _Frame, value: object) -> None:
        if isinstance(frame, _OperatorFrame):
            self._resume_operator(frame, value)
        elif isinstance(frame, _IfFrame):
            branch = frame.then if self._boolean(value) else frame.otherwise
            self._actions.append(_EvalAction(branch, frame.environment))
        elif isinstance(frame, _LetFrame):
            self._resume_let(frame, value)
        elif isinstance(frame, _CallCalleeFrame):
            self._resume_callee(frame, value)
        else:
            assert isinstance(frame, _CallArgumentsFrame)
            self._resume_call_arguments(frame, value)

    def _resume_operator(self, frame: _OperatorFrame, value: object) -> None:
        values = (*frame.values, value)
        if frame.remaining:
            self._actions.append(
                _ResumeTarget(
                    _OperatorFrame(frame.plan, frame.remaining[1:], values, frame.environment)
                )
            )
            self._actions.append(_EvalAction(frame.remaining[0], frame.environment))
            return
        token = frame.plan.operator_class(self._parser)
        token.symbol = frame.plan.symbol
        token.label = frame.plan.label
        for operand in values:
            token.append(ValueToken(self._parser, value=operand))
        self._deliver(token.evaluate(self._context))

    def _resume_let(self, frame: _LetFrame, value: object) -> None:
        name = frame.bindings[frame.index][0]
        environment = {**frame.environment, name: value}
        next_index = frame.index + 1
        if next_index < len(frame.bindings):
            self._actions.append(
                _ResumeTarget(_LetFrame(frame.bindings, next_index, environment, frame.body))
            )
            self._actions.append(_EvalAction(frame.bindings[next_index][1], environment))
        else:
            self._actions.append(_EvalAction(frame.body, environment))

    def _resume_callee(self, frame: _CallCalleeFrame, callee: object) -> None:
        if not isinstance(callee, (HostFunction, XPathClosure)):
            raise XPathEvaluationError(
                "XPTY0004", f"an XPath function expected, not {type(callee)!r}"
            )
        if not frame.arguments:
            self._invoke(callee, ())
            return
        self._actions.append(
            _ResumeTarget(_CallArgumentsFrame(callee, frame.arguments[1:], (), frame.environment))
        )
        self._actions.append(_EvalAction(frame.arguments[0], frame.environment))

    def _resume_call_arguments(self, frame: _CallArgumentsFrame, value: object) -> None:
        values = (*frame.values, value)
        if frame.remaining:
            self._actions.append(
                _ResumeTarget(
                    _CallArgumentsFrame(
                        frame.callee, frame.remaining[1:], values, frame.environment
                    )
                )
            )
            self._actions.append(_EvalAction(frame.remaining[0], frame.environment))
        else:
            self._invoke(frame.callee, values)

    def _invoke(self, target: object, arguments: tuple[object, ...]) -> None:
        self._sample_depth()
        if isinstance(target, HostFunction):
            self._check_arity(target.arity, len(arguments), target.name or "host function")
            self._deliver_host_result(target.callback(*arguments))
        elif isinstance(target, XPathClosure):
            self._check_arity(target.arity, len(arguments), "inline function")
            environment = {**target.environment}
            environment.update(zip(target.parameters, arguments, strict=True))
            self._actions.append(_EvalAction(target.body, environment))
        else:
            raise XPathEvaluationError(
                "XPTY0004", f"an XPath function expected, not {type(target)!r}"
            )

    def _deliver_host_result(self, value: object) -> None:
        self._sample_depth()
        if not isinstance(value, (TailCall, Invoke)):
            _reject_nested_control(value, "host result")
        self._deliver(value)

    def _deliver_to(self, target: _ResumeTarget | _TransformTarget, value: object) -> None:
        if isinstance(target, _ResumeTarget):
            self._resume(target.frame, value)
        else:
            self._deliver_host_result(target.transform(value))

    def _deliver(self, value: object) -> None:
        if isinstance(value, TailCall):
            self._actions.append(_InvokeAction(value.target, value.arguments))
            return
        if isinstance(value, Invoke):
            self._actions.append(_TransformTarget(value.then))
            self._actions.append(_InvokeAction(value.target, value.arguments))
            return
        if self._actions:
            pending = self._actions.pop()
            assert isinstance(pending, (_ResumeTarget, _TransformTarget))
            self._actions.append(_DeliverAction(pending, value))
        else:
            self._result = value

    def _boolean(self, value: object) -> bool:
        return bool(ValueToken(self._parser).boolean_value(value))

    def _check_arity(self, expected: int | None, actual: int, label: str) -> None:
        if expected is not None and expected != actual:
            raise XPathEvaluationError(
                "XPTY0004", f"{label} expects {expected} arguments, received {actual}"
            )

    def _sample_depth(self) -> None:
        self.max_python_depth = max(self.max_python_depth, _python_frame_depth())


def evaluate(
    compiled: CompiledExpression,
    variables: Mapping[str, object] | None = None,
) -> object:
    """Evaluate a compiled expression once with a fresh machine."""
    return ContinuationMachine(compiled, variables).run()


def evaluate_closure(closure: XPathClosure, arguments: Sequence[object]) -> object:
    """Invoke an XPath closure once with a fresh machine."""
    return ContinuationMachine.for_closure(closure, arguments).run()


### Gate probe


@dataclass(frozen=True)
class ContinuationProbe(CapabilityOutcome):
    """A Task 4 capability outcome carrying continuation probe details."""

    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    @property
    def diagnostic(self) -> str:
        """The diagnostic text, named for the Task 5 gate contract."""
        return self.detail

    def as_dict(self) -> dict[str, Any]:
        """A JSON-ready representation including probe details."""
        result = super().as_dict()
        result["details"] = _plain_value(dict(self.details))
        return result


def _plain_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    return value


@dataclass(frozen=True)
class _ProbeRun:
    result: object
    expected: object
    checks: Mapping[str, bool]
    max_python_depth: int
    recursion_limit: int


def _contains_control(value: object) -> bool:
    if isinstance(value, (TailCall, Invoke)):
        return True
    return any(_contains_control(item) for item in _control_candidates(value))


def _mixed_recursion(depth: int) -> tuple[object, int]:
    """XPath arithmetic pending across a host -> XPath recursive round trip."""
    compiled = compile_expression(
        "function($n) { if ($n = 0) then 0 else ($recurse($n - 1) + 1) }",
        variable_types={"recurse": "function(*)"},
    )
    holder: dict[str, XPathClosure] = {}

    def recurse(n: object) -> object:
        return TailCall(holder["closure"], (n,))

    host = HostFunction(recurse, name="recurse", arity=1)
    closure = evaluate(compiled, {"recurse": host})
    assert isinstance(closure, XPathClosure)
    holder["closure"] = closure
    machine = ContinuationMachine.for_closure(closure, (depth,))
    return machine.run(), machine.max_python_depth


def _higher_order_xpath(depth: int) -> tuple[object, int]:
    """An inline higher-order XPath function calls a host function."""
    compiled = compile_expression(
        "function($n) {"
        " if ($n = 0) then 0"
        " else (function($f, $x) { $f($x) + 1 })($recurse, $n - 1)"
        " }",
        variable_types={"recurse": "function(*)"},
    )
    holder: dict[str, XPathClosure] = {}

    def recurse(n: object) -> object:
        return TailCall(holder["closure"], (n,))

    host = HostFunction(recurse, name="recurse", arity=1)
    closure = evaluate(compiled, {"recurse": host})
    assert isinstance(closure, XPathClosure)
    holder["closure"] = closure
    machine = ContinuationMachine.for_closure(closure, (depth,))
    return machine.run(), machine.max_python_depth


def _higher_order_host() -> tuple[object, int]:
    """A host function invokes an XPath closure and resumes its own arithmetic."""
    compiled = compile_expression(
        "$apply(function($x) { $x + 1 }, $n)",
        variable_types={"apply": "function(*)", "n": "xs:integer"},
    )

    def apply(closure: object, n: object) -> object:
        def then(value: object) -> object:
            return value + 1  # type: ignore[operator]

        return Invoke(closure, (n,), then)

    machine = ContinuationMachine(
        compiled,
        {"apply": HostFunction(apply, name="apply", arity=2), "n": 4},
    )
    return machine.run(), machine.max_python_depth


def _mutual_recursion(depth: int) -> tuple[object, int]:
    """Two XPath closures recurse through mutually recursive host functions."""
    compiled_a = compile_expression(
        "function($n) { if ($n = 0) then true() else $even($n - 1) }",
        variable_types={"even": "function(*)"},
    )
    compiled_b = compile_expression(
        "function($n) { if ($n = 0) then false() else $odd($n - 1) }",
        variable_types={"odd": "function(*)"},
    )
    closures: dict[str, XPathClosure] = {}

    def even(n: object) -> object:
        return True if n == 0 else TailCall(closures["b"], (n,))

    def odd(n: object) -> object:
        return False if n == 0 else TailCall(closures["a"], (n,))

    even_host = HostFunction(even, name="even", arity=1)
    odd_host = HostFunction(odd, name="odd", arity=1)
    closure_a = evaluate(compiled_a, {"even": even_host})
    closure_b = evaluate(compiled_b, {"odd": odd_host})
    assert isinstance(closure_a, XPathClosure)
    assert isinstance(closure_b, XPathClosure)
    closures["a"] = closure_a
    closures["b"] = closure_b
    machine = ContinuationMachine.for_closure(closure_a, (depth,))
    return machine.run(), machine.max_python_depth


def _error_propagation(depth: int) -> tuple[bool, int]:
    """A host error unwinds pending XPath arithmetic and a caught error recovers."""
    compiled = compile_expression(
        "function($n) { if ($n = 0) then $explode(0) else ($recurse($n - 1) + 1) }",
        variable_types={"explode": "function(*)", "recurse": "function(*)"},
    )
    holder: dict[str, XPathClosure] = {}

    def recurse(n: object) -> object:
        return TailCall(holder["closure"], (n,))

    def explode(_: object) -> object:
        raise XPathEvaluationError("FOAR0001", "host exploded")

    host = HostFunction(recurse, name="recurse", arity=1)
    boom = HostFunction(explode, name="explode", arity=1)
    closure = evaluate(compiled, {"explode": boom, "recurse": host})
    assert isinstance(closure, XPathClosure)
    holder["closure"] = closure
    machine = ContinuationMachine.for_closure(closure, (depth,))
    error_seen = False
    try:
        machine.run()
    except XPathEvaluationError as exc:
        error_seen = exc.code == "FOAR0001"

    recovered = evaluate(
        compile_expression("$n + 1", variable_types={"n": "xs:integer"}),
        {"n": 1},
    )
    return error_seen and recovered == 2, machine.max_python_depth


def _run_probe(depth: int) -> _ProbeRun:
    recursion_limit = sys.getrecursionlimit()
    main_result, main_depth = _mixed_recursion(depth)
    hof_result, hof_depth = _higher_order_xpath(depth)
    host_hof_result, host_hof_depth = _higher_order_host()
    mutual_result, mutual_depth = _mutual_recursion(depth)
    error_ok, error_depth = _error_propagation(min(depth, 1_000))
    expected_mutual = depth % 2 == 1
    checks = {
        "pending_arithmetic": main_result == depth,
        "higher_order_xpath": hof_result == depth,
        "higher_order_host": host_hof_result == 6,
        "mutual_recursion": mutual_result is expected_mutual,
        "error_propagation": error_ok,
    }
    return _ProbeRun(
        result=main_result,
        expected=depth,
        checks=MappingProxyType(checks),
        max_python_depth=max(
            main_depth,
            hof_depth,
            host_hof_depth,
            mutual_depth,
            error_depth,
        ),
        recursion_limit=recursion_limit,
    )


def _probe_outcome(
    status: str,
    detail: str,
    *,
    depth: int,
    details: Mapping[str, object] | None = None,
    evidence: Sequence[str] = (),
    baseline_python_depth: int | None = None,
) -> ContinuationProbe:
    evidence_lines = (*evidence, detail)
    base_details: dict[str, object] = {"depth": depth, "tail_marker_leaked": False}
    if baseline_python_depth is not None:
        base_details["baseline_python_depth"] = baseline_python_depth
    base_details.update(details or {})
    return ContinuationProbe(
        name="mixed-continuation",
        status=status,
        detail=detail,
        evidence=evidence_lines,
        xpath_version="3.1",
        parser="XPath31Parser",
        parser_version="3.1",
        evaluator="pyxsd.xpath.continuation",
        evaluator_version="1",
        details=base_details,
    )


def probe_mixed_recursion(depth: int = 10_000) -> ContinuationProbe:
    """Run the mixed XPath/XSLT continuation gate at *depth* steps.

    A ``pass`` requires the recursive result at the requested depth, pending
    XPath arithmetic, higher-order calls, mutual recursion, error propagation,
    caught-error recovery, no leaked tail-control value, an unchanged Python
    recursion limit, and a shallow Python frame depth.  ``RecursionError`` is
    reported as a defect with its text in the diagnostic.
    """
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        raise ValueError("depth must be a positive integer")
    baseline_python_depth = _python_frame_depth()
    details: dict[str, object]
    try:
        run = _run_probe(depth)
    except RecursionError as exc:
        return _probe_outcome(
            "defect",
            f"RecursionError: {exc}",
            depth=depth,
            details={"python_recursion_limit": sys.getrecursionlimit()},
            baseline_python_depth=baseline_python_depth,
        )
    except UnsupportedContinuationExpression as exc:
        return _probe_outcome(
            "adapter-requirement",
            f"continuation compiler gap: {exc}",
            depth=depth,
            baseline_python_depth=baseline_python_depth,
        )
    except Exception as exc:
        return _probe_outcome(
            "defect",
            f"{type(exc).__name__}: {exc}",
            depth=depth,
            baseline_python_depth=baseline_python_depth,
        )

    leaked = _contains_control(run.result)
    python_depth_delta = run.max_python_depth - baseline_python_depth
    details = {
        "depth": depth,
        "tail_marker_leaked": leaked,
        "max_python_depth": run.max_python_depth,
        "baseline_python_depth": baseline_python_depth,
        "python_depth_delta": python_depth_delta,
        "python_depth_budget": PYTHON_DEPTH_BUDGET,
        "python_recursion_limit": run.recursion_limit,
        "checks": dict(run.checks),
    }
    failed = sorted(name for name, passed in run.checks.items() if not passed)
    if run.result != run.expected:
        detail = f"mixed recursion returned {run.result!r}, expected {run.expected!r}"
        return _probe_outcome(
            "defect",
            detail,
            depth=depth,
            details=details,
            baseline_python_depth=baseline_python_depth,
        )
    if failed:
        detail = "continuation checks failed: " + ", ".join(failed)
        return _probe_outcome(
            "defect",
            detail,
            depth=depth,
            details=details,
            baseline_python_depth=baseline_python_depth,
        )
    if leaked:
        return _probe_outcome(
            "defect",
            "a tail-control value escaped as an XDM result",
            depth=depth,
            details=details,
            baseline_python_depth=baseline_python_depth,
        )
    if run.recursion_limit != sys.getrecursionlimit():
        return _probe_outcome(
            "defect",
            "the Python recursion limit changed during the probe",
            depth=depth,
            details=details,
            baseline_python_depth=baseline_python_depth,
        )
    if python_depth_delta > PYTHON_DEPTH_BUDGET:
        detail = (
            f"Python frame depth delta {python_depth_delta} exceeded the "
            f"{PYTHON_DEPTH_BUDGET}-frame budget"
        )
        return _probe_outcome(
            "defect",
            detail,
            depth=depth,
            details=details,
            baseline_python_depth=baseline_python_depth,
        )
    return _probe_outcome(
        "pass",
        f"evaluated {depth} mixed XPath/host recursion steps with pending operands",
        depth=depth,
        details=details,
        baseline_python_depth=baseline_python_depth,
        evidence=(
            "compiled plans evaluated with an explicit heap continuation stack",
            "supported subset: literals, variables, arithmetic/comparison, if, let, "
            "inline functions, dynamic calls",
            f"maximum Python frame depth delta {python_depth_delta} "
            f"(baseline {baseline_python_depth})",
            "tail-control values consumed before XDM delivery",
        ),
    )
