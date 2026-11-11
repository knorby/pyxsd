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

from pyxsd.locations import IssueLocation
from pyxsd.xdm import XdmAtomic, XdmFunction, XdmItem, XdmSequence, as_xdm
from pyxsd.xdm.nodes import XdmDocument, XdmNode
from pyxsd.xpath import continuation
from pyxsd.xpath.continuation import CompiledExpression as ContinuationPlan
from pyxsd.xpath.continuation import ContinuationMachine, HostFunction, XPathClosure
from pyxsd.xpath.engine import CompiledExpression
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xpath.functions import atomic_to_elementpath
from pyxsd.xslt.errors import XsltDynamicError, XsltError, error_code_from_exception
from pyxsd.xslt.profiles import LanguageProfile, RecoveryPolicy, RecoveryRule
from pyxsd.xslt.runtime.frames import (
    ExecutionBudget,
    ExecutionLimits,
    Frame,
    FrameDiagnostic,
    FrameResult,
    Scheduler,
)
from pyxsd.xslt.runtime.functions import XSLT_CONTEXT_FUNCTION_NAMES

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
        recovery_policy: RecoveryPolicy | None = None,
    ) -> None:
        if recovery_policy is None:
            processor = getattr(stylesheet, "processor", None)
            configured = getattr(processor, "recovery_policy", None)
            recovery_policy = (
                configured if isinstance(configured, RecoveryPolicy) else RecoveryPolicy()
            )
        if not isinstance(recovery_policy, RecoveryPolicy):
            raise TypeError("recovery_policy must be a RecoveryPolicy or None")
        self.recovery_policy = recovery_policy
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
        self.current_group: XdmSequence | None = None
        self.current_grouping_key: XdmSequence | None = None
        self.limits = limits if limits is not None else ExecutionLimits()
        self.budget = ExecutionBudget(self.limits)
        self.scheduler = Scheduler(self.limits, budget=self.budget)
        self._xpath_plans: dict[int, ContinuationPlan | None] = {}
        self._function_tables: dict[str | None, dict[tuple[str, int], HostFunction]] = {}
        self._fallback_function_tables: dict[str | None, dict[str, Any]] = {}
        self.last_error: tuple[BaseException, tuple[FrameDiagnostic, ...]] | None = None
        self.xpath_machines = 0
        self.max_python_depth = 0
        # Every cache below belongs to exactly one invocation.  Including the
        # item itself keeps a generated identity unique even if Python reuses
        # an earlier object's address after garbage collection.
        self.generated_ids: dict[int, tuple[XdmItem, str]] = {}
        self.generated_id_count = 0
        self.key_indexes: dict[tuple[int, str | None, str], Mapping[Any, tuple[XdmNode, ...]]] = {}
        self.key_index_documents: dict[int, XdmDocument] = {}
        self.documents: dict[str, XdmDocument] = {}
        self.evaluating_attribute_sets: list[tuple[str | None, str | None]] = []
        # A subclass may install an invocation-local accumulator manager.  The
        # base transformation keeps the absent state so bridge unit tests and
        # non-XSLT schedulers do not fabricate accumulator values.
        self.accumulators: Any = None

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

    # -- language profile and recovery -------------------------------------

    def profile_for(self, effective_version: str | None = None) -> LanguageProfile:
        """Return the active language profile for one element's effective version."""
        profile = getattr(self.stylesheet, "profile", None)
        return LanguageProfile.from_options(profile, effective_version)

    def recover(self, error: BaseException, rule: RecoveryRule | str) -> bool:
        """Whether *error* may use the profile rule selected by *rule*.

        Only a deliberately converted dynamic error with a code is eligible.  A
        raw Python callback failure never becomes recoverable merely because the
        surrounding transformation is executing under a recovery policy.
        """
        if not isinstance(error, XsltDynamicError) or not error.catchable:
            return False
        profile = self.profile_for()
        resolved = profile.recovery_rule_for(rule)
        if resolved is None or not resolved.matches(error.code):
            return False
        return self.recovery_policy.allows(resolved, profile)

    def execute_frame(self, frame: Frame) -> object:
        """Execute one frame with this transformation's step function.

        A nested execution (for example an instruction inside temporary tree
        construction) receives its own scheduler so it cannot corrupt the
        active heap stack.  Suspension/resumption still uses the same
        transformation state and step function.

        Every scheduler exit is an XSLT error boundary for errors the XSLT layer
        already owns: frame-derived context is attached to an existing
        :class:`~pyxsd.xslt.errors.XsltError` without replacing it.  Parser,
        resource, XPath, and Python exceptions outside the XSLT error hierarchy
        keep their own identity here; an extension boundary that deliberately
        converts one calls :meth:`boundary_error` explicitly.
        """
        if self.scheduler.active:
            self._nested_diagnostics = self.scheduler.diagnostics
            nested = Scheduler(self.limits, budget=self.budget)
            try:
                return self._run_scheduler(nested, frame)
            finally:
                self._nested_diagnostics = nested.diagnostics
        return self._run_scheduler(self.scheduler, frame)

    def _run_scheduler(self, scheduler: Scheduler, frame: Frame) -> object:
        try:
            return scheduler.run(frame, self.step)
        except XsltError as error:
            self.boundary_error(error, stack=scheduler.diagnostics)
            raise

    def boundary_error(
        self,
        error: BaseException,
        *,
        stack: tuple[FrameDiagnostic, ...] | None = None,
        catchable: bool = False,
    ) -> XsltError:
        """Attach XSLT context to *error*, wrapping only at this boundary.

        A foreign exception is wrapped with its identity preserved as the
        cause.  ``catchable`` is an explicit caller decision: only an error the
        conversion site deliberately treats as an XSLT dynamic error may enter
        a coded ``xsl:catch`` or an implementation recovery rule.
        """
        if not isinstance(catchable, bool):
            raise TypeError("catchable must be a boolean")
        frames = self.diagnostics if stack is None else tuple(stack)
        _, package = self._stylesheet_identity()
        if isinstance(error, XsltError):
            error.attach_diagnostics(frames, package=package)
            return error
        module, effective_package, location = self._boundary_identity(frames)
        wrapped = XsltDynamicError(
            str(error) or type(error).__name__,
            code=error_code_from_exception(error),
            uri=module,
            module=module,
            package=effective_package,
            location=location,
            stack=frames,
            cause=error,
            allow_catch=catchable,
        )
        wrapped.__cause__ = error
        wrapped.__suppress_context__ = True
        return wrapped

    def _stylesheet_identity(self) -> tuple[str | None, str | None]:
        stylesheet = self.stylesheet
        if stylesheet is None:
            return None, None
        package = getattr(stylesheet, "package", None)
        return None, getattr(package, "name", None)

    def _boundary_identity(
        self,
        frames: tuple[FrameDiagnostic, ...],
    ) -> tuple[str | None, str | None, IssueLocation | None]:
        module, package = self._stylesheet_identity()
        location = None
        if frames:
            last = frames[-1]
            location = last.location
            if location is not None:
                module = location.source
            if last.package is not None:
                package = last.package
        return module, package, location

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
                functions={
                    name: arity
                    for name, arity in static.functions.items()
                    if name not in XSLT_CONTEXT_FUNCTION_NAMES
                },
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
        referenced = plan.variable_names
        selected = {name: value for name, value in variables.items() if name in referenced}
        native = self.variables_to_native(selected)
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

    def stylesheet_functions(
        self,
        package_name: str | None = None,
    ) -> Mapping[tuple[str, int], HostFunction]:
        """Scheduler-aware host items for every visible stylesheet function.

        One table belongs to each package scope.  A private function in a
        library package is callable from that package's own templates while
        remaining invisible to the principal package; selecting a table by the
        current component's package preserves that static decision at run time.
        """
        cached = self._function_tables.get(package_name)
        if cached is not None:
            return cached
        table: dict[tuple[str, int], HostFunction] = {}
        package = self._package_for(package_name)
        if package is not None:
            for component in package.components.values():
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
        self._function_tables[package_name] = table
        return table

    def fallback_functions(self, package_name: str | None = None) -> Mapping[str, Any]:
        """Host callables for the full evaluator when scheduling is unavailable.

        The standalone engine needs a target for every declared stylesheet
        function.  These callables enter the same production component bridge;
        they are used only when the scheduler path has already declined an
        expression (for example because a node-valued variable is referenced).
        """
        cached = self._fallback_function_tables.get(package_name)
        if cached is not None:
            return cached
        components: dict[str, list[Any]] = {}
        package = self._package_for(package_name)
        if package is not None:
            for component in package.components.values():
                name = component.name
                if (
                    component.kind != "function"
                    or name is None
                    or component.hidden
                    or component.abstract
                ):
                    continue
                components.setdefault(name, []).append(component)
        table = {
            name: self._component_dispatcher(name, overloads)
            for name, overloads in components.items()
        }
        self._fallback_function_tables[package_name] = table
        return table

    def _package_for(self, package_name: str | None) -> Any:
        stylesheet = self.stylesheet
        if stylesheet is None:
            return None
        return stylesheet._packages.get(package_name)

    def _component_dispatcher(self, name: str, components: list[Any]) -> Any:
        by_arity = {
            component.arity: component for component in components if component.arity is not None
        }

        def dispatch(*arguments: object) -> XdmSequence:
            component = by_arity.get(len(arguments))
            if component is None:
                raise XsltError(
                    f"the stylesheet function {name!r} is not available with "
                    f"arity {len(arguments)}",
                    code="XPTY0004",
                )
            converted = tuple(as_xdm(argument) for argument in arguments)
            return self._invoke_component_callable(component, converted)

        return dispatch

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

    # -- invocation-local node identities ----------------------------------

    def generated_id(self, item: XdmItem) -> str:
        """Return a stable, unique identifier for *item* in this invocation.

        The cache retains the item so Python cannot recycle an address into a
        duplicate identifier.  Two invocations never share values because the
        map and counter live on this object.
        """
        cached = self.generated_ids.get(id(item))
        if cached is not None and cached[0] is item:
            return cached[1]
        self.generated_id_count += 1
        identifier = f"ID{self.generated_id_count}"
        self.generated_ids[id(item)] = (item, identifier)
        return identifier
