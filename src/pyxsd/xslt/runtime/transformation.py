"""Per-invocation XSLT dynamic state and scheduler-facing helpers.

A :class:`Transformation` is created for one invocation of a compiled
stylesheet.  It owns the state that must never be shared between invocations:
global values, result entries and final URIs, stable dynamic context values,
execution budgets, mode indexes, and the heap-resident scheduler.

The processor subclasses this object and supplies the instruction step.  The
helpers here keep XPath suspension and stylesheet-function calls on that same
scheduler: a suspended :class:`~pyxsd.xpath.continuation.ContinuationMachine`
becomes an ordinary frame, and a component call requested by XPath becomes a
child frame.  Unsupported XPath constructs are reported as unavailable so the
caller can use the full elementpath evaluator with unchanged semantics.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any

from pyxsd.xdm import XdmAtomic, XdmFunction, XdmItem, XdmSequence, as_xdm
from pyxsd.xpath import continuation
from pyxsd.xpath.continuation import CompiledExpression as ContinuationPlan
from pyxsd.xpath.continuation import ContinuationMachine, HostFunction, XPathClosure
from pyxsd.xpath.engine import CompiledExpression
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xpath.functions import atomic_to_elementpath
from pyxsd.xslt.errors import XsltDynamicError, XsltError
from pyxsd.xslt.runtime.frames import (
    ExecutionBudget,
    ExecutionLimits,
    Frame,
    FrameDiagnostic,
    FrameResult,
    Scheduler,
)

__all__ = ["ComponentCall", "Transformation"]


class ComponentCall:
    """One scheduler-owned call to a compiled stylesheet component."""

    __slots__ = ("component", "kind")

    def __init__(self, component: object, kind: str = "function") -> None:
        if kind not in ("function", "template"):
            raise ValueError(f"unknown component call kind {kind!r}")
        self.component = component
        self.kind = kind

    def __call__(self, *arguments: object) -> object:
        del arguments
        raise XsltDynamicError("a component call target must be executed by the XSLT scheduler")

    def __repr__(self) -> str:  # pragma: no cover - diagnostic convenience
        return f"ComponentCall({self.component!r}, {self.kind!r})"


class _SecondaryExtent:
    """A production rollback boundary for one typed frame.

    A frame that performs an ``as`` conversion is already a non-eliminable
    boundary.  This extent snapshots secondary results and final URIs when the
    frame starts, and discards entries added by that frame if it unwinds.
    """

    __slots__ = ("_final_uri_keys", "_final_uris", "_secondary", "_secondary_keys")

    def __init__(self, transformation: Transformation) -> None:
        self._secondary = transformation.secondary
        self._final_uris = transformation.final_uris
        self._secondary_keys = frozenset(transformation.secondary)
        self._final_uri_keys = frozenset(transformation.final_uris)

    def rollback(self) -> None:
        """Discard secondary entries and final URIs created by this frame."""
        for key in tuple(self._secondary):
            if key not in self._secondary_keys:
                del self._secondary[key]
        for uri in tuple(self._final_uris):
            if uri not in self._final_uri_keys:
                self._final_uris.discard(uri)


class Transformation:
    """One transformation's complete dynamic state and frame scheduler."""

    def __init__(
        self,
        stylesheet: Any = None,
        options: Any = None,
        *,
        limits: ExecutionLimits | None = None,
    ) -> None:
        self.stylesheet: Any = stylesheet
        self.package: Any = None if stylesheet is None else stylesheet.package
        self.options: Any = options
        self.globals: dict[tuple[str | None, str], XdmSequence] = {}
        self._evaluating: list[tuple[str | None, str]] = []
        self.secondary: dict[str, Any] = {}
        self.final_uris: set[str] = set()
        self._nested_diagnostics: tuple[FrameDiagnostic, ...] = ()
        output_uri = None if options is None else getattr(options, "output_uri", None)
        if output_uri is not None:
            self.final_uris.add(output_uri)
        base = ExpressionEnvironment()
        self.current_datetime = base.current_datetime
        self.implicit_timezone = base.implicit_timezone
        self.output_attributes = self._read_output_attributes()
        self.limits = limits if limits is not None else ExecutionLimits()
        self.budget = ExecutionBudget(self.limits)
        self.scheduler = Scheduler(self.limits, budget=self.budget)
        self._xpath_plans: dict[int, ContinuationPlan | None] = {}
        self._function_table: dict[tuple[str, int], HostFunction] | None = None
        self._fallback_function_table: dict[str, Any] | None = None
        self.last_error: tuple[BaseException, tuple[FrameDiagnostic, ...]] | None = None
        self.xpath_machines = 0
        self.max_python_depth = 0

    def _read_output_attributes(self) -> Mapping[str, str]:
        if self.package is None:
            return {}
        output = self.package.output(None)
        if output is None:
            return {}
        return dict(output.attributes)

    @property
    def diagnostics(self) -> tuple[FrameDiagnostic, ...]:
        """The last diagnostic stack produced by the scheduler."""
        stack = self.scheduler.diagnostics
        if stack:
            return stack
        return getattr(self, "_nested_diagnostics", ())

    def execute_frame(self, frame: Frame) -> object:
        """Execute one frame with this transformation's step function.

        A nested execution (for example an instruction inside temporary tree
        construction) receives its own scheduler so it cannot corrupt the
        active heap stack.  Suspension/resumption still uses the same
        transformation state and step function.
        """
        if self.scheduler.active:
            self._nested_diagnostics = self.scheduler.diagnostics
            nested = Scheduler(self.limits, budget=self.budget)
            try:
                return nested.run(frame, self.step)
            finally:
                self._nested_diagnostics = nested.diagnostics
        return self.scheduler.run(frame, self.step)

    def frame_boundary(self, frame: Frame) -> Frame:
        """Attach production rollback and diagnostic boundaries to *frame*.

        Typed frames already cannot be tail-eliminated.  Code paths that
        install an ``as`` conversion call this so secondary-result rollback and
        diagnostic capture are exercised by the real XSLT instruction bridge,
        not only by the scheduler protocol tests.  ``xsl:try`` remains Task 26.
        """
        return replace(
            frame,
            extent=_SecondaryExtent(self),
            error_handler=self._record_error,
        )

    def _record_error(
        self,
        error: BaseException,
        stack: tuple[FrameDiagnostic, ...],
    ) -> None:
        self.last_error = (error, stack)
        return None

    def step(self, frame: Frame) -> FrameResult:
        """Advance one frame; the processor subclass implements this."""
        raise NotImplementedError("a Transformation subclass must implement step()")

    # -- XPath scheduling ---------------------------------------------------

    def xpath_plan(self, expression: CompiledExpression) -> ContinuationPlan | None:
        """Return a continuation plan for *expression*, or ``None`` to fall back.

        The Task 11 engine cannot declare stylesheet functions while lowering
        its optional plan.  Recompiling here with the expression's full static
        context makes static stylesheet-function calls schedulable without
        changing the standalone engine's contract.
        """
        key = id(expression)
        if key in self._xpath_plans:
            return self._xpath_plans[key]
        static = expression.static_context
        try:
            plan = continuation.compile_expression(
                expression.source,
                variable_types=static.variable_types,
                default_collation=static.default_collation,
                namespaces=static.namespaces,
                functions=dict(static.functions),
            )
        except Exception:
            plan = None
        self._xpath_plans[key] = plan
        return plan

    def xpath_machine(
        self,
        expression: CompiledExpression,
        variables: Mapping[str, object],
        functions: Mapping[tuple[str, int], object] | None = None,
    ) -> ContinuationMachine | None:
        """Create a schedulable machine, or ``None`` for the evaluator fallback."""
        plan = self.xpath_plan(expression)
        if plan is None:
            return None
        native = self.variables_to_native(variables)
        if native is None:
            return None
        machine = ContinuationMachine(plan, native, functions)
        self.xpath_machines += 1
        return machine

    def variables_to_native(
        self,
        variables: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Convert XDM variable values for the continuation machine.

        Values outside the supported atomic/closure subset return ``None`` so
        the caller uses the full elementpath evaluator without changing
        semantics.
        """
        result: dict[str, object] = {}
        for name, value in variables.items():
            converted = self.sequence_to_native(value)
            if converted is None:
                return None
            result[name] = converted
        return result

    def sequence_to_native(self, value: object) -> object | None:
        """Convert one XDM sequence to the machine's native value convention."""
        if isinstance(value, XPathClosure):
            return value
        if isinstance(value, tuple):
            members: list[object] = []
            for item in value:
                converted = self._item_to_native(item)
                if converted is None:
                    return None
                members.append(converted)
            if not members:
                return ()
            if len(members) == 1:
                return members[0]
            return tuple(members)
        if isinstance(value, list):
            members = []
            for item in value:
                converted = self.sequence_to_native(item)
                if converted is None:
                    return None
                members.append(converted)
            return tuple(members)
        converted = self._item_to_native(value)
        if converted is None:
            return None
        return converted

    def _item_to_native(self, item: object) -> object | None:
        if isinstance(item, XdmAtomic):
            return atomic_to_elementpath(item)
        if isinstance(item, XdmFunction):
            closure = item.closure
            if isinstance(closure, XPathClosure):
                return closure
            return None
        if isinstance(item, (str, bool, int, float, Decimal)):
            return item
        return None

    def native_to_xdm(self, value: object) -> XdmSequence:
        """Convert one elementpath/host result into an XDM sequence."""
        if isinstance(value, XdmItem):
            return (value,)
        if isinstance(value, XPathClosure):
            return (self.closure_item(value),)
        if isinstance(value, (list, tuple)):
            items: list[XdmItem] = []
            for member in value:
                items.extend(self.native_to_xdm(member))
            return tuple(items)
        if isinstance(value, continuation.Suspension):
            raise XsltDynamicError("a suspended XPath result cannot become an XDM value")
        if isinstance(value, (continuation.TailCall, continuation.Invoke)):
            raise XsltDynamicError("a tail-control value cannot become an XDM result")
        try:
            return as_xdm(value)
        except TypeError as exc:
            raise XsltDynamicError(
                f"cannot convert XPath result {type(value).__name__} to an XDM value"
            ) from exc

    @staticmethod
    def closure_item(closure: XPathClosure) -> XdmFunction:
        """Wrap one XPath closure as an XDM function item."""

        def implementation(arguments: Any, environment: Any, captured: Any) -> XdmSequence:
            del arguments, environment, captured
            raise XsltError(
                "an XPath closure must be invoked through the XSLT scheduler",
                code=None,
            )

        return XdmFunction(
            implementation,
            arity=closure.arity,
            closure=closure,
        )

    def stylesheet_functions(self) -> Mapping[tuple[str, int], HostFunction]:
        """Scheduler-aware host items for every visible stylesheet function."""
        if self._function_table is not None:
            return self._function_table
        table: dict[tuple[str, int], HostFunction] = {}
        if self.package is not None:
            for component in self.package.components.values():
                name = component.name
                arity = component.arity
                if (
                    component.kind != "function"
                    or name is None
                    or arity is None
                    or component.hidden
                    or component.abstract
                ):
                    continue
                table[(name, arity)] = HostFunction(
                    ComponentCall(component),
                    name=name,
                    arity=arity,
                    suspend=True,
                )
        self._function_table = table
        return table

    def fallback_functions(self) -> Mapping[str, Any]:
        """Host callables for the full evaluator when scheduling is unavailable.

        The standalone engine needs a target for every declared stylesheet
        function.  These callables enter the same production component bridge;
        they are used only when the scheduler path has already declined an
        expression (for example because a node-valued variable is referenced).
        """
        if self._fallback_function_table is not None:
            return self._fallback_function_table
        table: dict[str, Any] = {}
        if self.package is not None:
            for component in self.package.components.values():
                name = component.name
                if (
                    component.kind != "function"
                    or name is None
                    or component.hidden
                    or component.abstract
                ):
                    continue
                table[name] = self._component_callable(component)
        self._fallback_function_table = table
        return table

    def _component_callable(self, component: Any) -> Any:
        def call(*arguments: object) -> XdmSequence:
            converted = tuple(as_xdm(argument) for argument in arguments)
            return self._invoke_component_callable(component, converted)

        return call

    def _invoke_component_callable(
        self,
        component: Any,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        raise NotImplementedError(
            "a Transformation subclass must implement _invoke_component_callable()"
        )

    def component_arguments(self, arguments: tuple[object, ...]) -> tuple[XdmSequence, ...]:
        """Convert scheduler-native arguments into XDM argument sequences."""
        return tuple(self.native_to_xdm(argument) for argument in arguments)

    def note_machine(self, machine: ContinuationMachine) -> None:
        """Record the deepest Python frame a scheduled machine reached."""
        self.max_python_depth = max(self.max_python_depth, machine.max_python_depth)
