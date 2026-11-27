"""Heap-resident XSLT frames and the scheduler that executes them.

A transformation uses explicit :class:`Frame` objects for component
invocations, template-rule dispatches, and suspended XPath evaluations.
Nested schedulers and work within a dispatch can still use the Python stack. A frame
carries its instruction cursor, lexical variables, focus, return continuation,
result-builder extent, error handler, component identity, package, and mode.

Tail positions are marked by the compiler, not guessed by the scheduler.  A
call is replaced in place only when the scheduler can prove that the calling
frame has no pending conversion, error handler, builder extent, or result
continuation beyond returning to its own caller.  Otherwise the caller frame is
kept as a heap continuation.

Budget exhaustion raises an uncatchable
:class:`~pyxsd.xslt.errors.XsltDynamicError` with code ``PYXSD-XSLT-001``.
The scheduler converts ``RecursionError`` caught at its guards into the
same diagnostic. Close to the recursion limit, CPython can fail inside
its own machinery before a guard runs and surface an uncoded built-in
exception. This was observed on CPython 3.13 under a deeply nested host
stack. The scheduler never changes the process-wide recursion limit.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import TYPE_CHECKING, Literal, TypeAlias

from pyxsd.locations import IssueLocation
from pyxsd.xpath.environment import _DEFAULT_MAX_ANNOTATIONS, Focus
from pyxsd.xslt.errors import IMPLEMENTATION_BUDGET_CODE, XsltDynamicError
from pyxsd.xslt.runtime.context import EvaluationContext

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xdm import XdmItem, XdmSequence
    from pyxsd.xslt.compiler.packages import Component
    from pyxsd.xslt.compiler.plans import _BreakPlan, _NextIterationPlan

__all__ = [
    "NO_GROUP",
    "NO_INPUT",
    "PYXSD_FRAME_EXHAUSTED",
    "_INHERIT",
    "Call",
    "CatchResult",
    "Continuation",
    "Continue",
    "ExecutionBudget",
    "ExecutionLimits",
    "Frame",
    "FrameDiagnostic",
    "FrameResult",
    "Raise",
    "Return",
    "Scheduler",
    "SchedulerViolation",
    "_IterationControlSignal",
    "_TemplateContext",
    "resource_error",
]

#: Stable implementation error code for transformation resource limits.
PYXSD_FRAME_EXHAUSTED = IMPLEMENTATION_BUDGET_CODE

#: Sentinel distinguishing "no delivered child result" from an XDM value.
NO_INPUT = object()

#: Sentinel distinguishing an absent current group/key from an empty sequence.
NO_GROUP = object()


class _IterationControlSignal(Exception):
    """A valid iterate-body control reached through synchronous conditional content.

    ``xsl:on-non-empty`` and ``xsl:on-empty`` are executed by the conditional
    sequence evaluator, which normally returns a value synchronously.  A
    control instruction in that content must instead unwind to the body frame so
    the iteration frame can observe it.  The signal carries the conditional
    output produced before the control and the lexical focus at the control.
    """

    __slots__ = ("focus", "pending", "plan", "variables")

    def __init__(
        self,
        plan: _NextIterationPlan | _BreakPlan,
        variables: Mapping[str, XdmSequence],
        focus: Focus,
    ) -> None:
        super().__init__(plan.instruction.local_name)
        self.plan = plan
        self.variables = dict(variables)
        self.focus = focus
        self.pending: tuple[XdmItem, ...] = ()

    def prepend(self, values: Sequence[XdmItem]) -> None:
        """Add already-produced conditional output before this control."""
        self.pending = (*values, *self.pending)


#: Sentinel selecting the caller's current group state instead of a literal value.
_INHERIT = object()


def resource_error(message: str) -> XsltDynamicError:
    """Build one execution-resource diagnostic with the stable local code."""
    return XsltDynamicError(message, code=PYXSD_FRAME_EXHAUSTED, allow_catch=False)


def _validate_optional_limit(name: str, value: int | None) -> None:
    """Validate one optional positive integer execution limit."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer or None")
    if value < 1:
        raise ValueError(f"{name} must be at least 1")


@dataclass(frozen=True, slots=True)
class _TemplateContext:
    """Opaque component identity retained by a recomposition frame."""

    component: Component


ContinuationKind: TypeAlias = Literal["resume", "append", "convert"]


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Explicit budgets for one transformation and its nested invocations.

    ``max_frames`` defaults to 100,000 live explicit frames. Non-tail
    arithmetic recursion keeps frames for its pending work. Mixed XSLT/XPath
    calls, recursive sequence or node construction, and template push can
    also accumulate explicit frames, but can reach the Python recursion
    limit first. A deep XPath path can reach that limit within a single
    dispatch. Frame-budget exhaustion and ``RecursionError`` caught at
    scheduler guards raise an uncatchable
    :class:`~pyxsd.xslt.errors.XsltDynamicError` with code
    ``PYXSD-XSLT-001``. Close to the recursion limit, CPython can fail
    inside its own machinery before a guard runs and surface an uncoded
    built-in exception, as observed on CPython 3.13 under a deeply nested
    host stack.

    A safe tail call replaces its frame in place. Use ``max_steps`` to
    bound the number of scheduler dispatches in a tail-call chain;
    ``max_frames`` does not bound the length of that chain. ``max_steps``
    does not interrupt work within one dispatch. ``max_documents`` bounds
    loaded XML documents. These and other limits described here default
    to ``None`` unless stated otherwise.

    With the default ``max_steps=None``, the processor does not stop an
    unbounded safe tail recursion; callers bound such a chain by setting
    ``max_steps``.

    ``max_resource_bytes`` bounds octets the transformation runtime acquires.
    Top-level stylesheet compilation reads and schema loads made by
    ``xsl:import-schema`` are outside this budget. A host
    ``ResourcePolicy.read()`` can allocate a whole resource before handing
    it over; the budget charges its returned bytes at that handoff.

    ``max_annotations`` defaults to 262,144 atomic type annotation entries
    recorded cumulatively across the transformation and its nested calls.
    It governs runtime tables; static XPath evaluation during stylesheet
    compilation precedes invocation and uses the XPath engine's own default.
    Each runtime table retains the values for its invocation's
    lifetime, so disabling the cap allows memory to grow with the number
    of distinct typed values bridged to or from XPath.

    ``max_output_nodes`` counts raw principal and secondary output nodes,
    their descendants and owned attributes, once by identity per result
    entry. Namespace nodes and implicit normalization document nodes are
    excluded; text nodes that normalization merges are counted separately.
    This bounds delivered nodes, not memory: provisional constructor and
    copy trees are built before their final nodes can be admitted.
    ``max_output_bytes`` bounds bytes encoded for stylesheet delivery and
    later :meth:`~pyxsd.xslt.results.TransformationResult.write` calls.
    Writing a result again spends the same budget. To deliver again after
    exhausting that budget, run the transformation with a larger limit or
    with ``None``.

    ``None`` disables any limit. Nested transforms inherit the settings
    unchanged and share the cumulative ledger; frames count while live.
    Exhaustion raises an uncatchable
    :class:`~pyxsd.xslt.errors.XsltDynamicError` with code ``PYXSD-XSLT-001``.
    """

    max_frames: int | None = 100_000
    max_steps: int | None = None
    max_documents: int | None = None
    max_output_nodes: int | None = None
    max_output_bytes: int | None = None
    max_resource_bytes: int | None = None
    max_annotations: int | None = _DEFAULT_MAX_ANNOTATIONS

    def __post_init__(self) -> None:
        _validate_optional_limit("max_frames", self.max_frames)
        _validate_optional_limit("max_steps", self.max_steps)
        _validate_optional_limit("max_documents", self.max_documents)
        _validate_optional_limit("max_output_nodes", self.max_output_nodes)
        _validate_optional_limit("max_output_bytes", self.max_output_bytes)
        _validate_optional_limit("max_resource_bytes", self.max_resource_bytes)
        _validate_optional_limit("max_annotations", self.max_annotations)


class ExecutionBudget:
    """One invocation's shared ledger for :class:`ExecutionLimits`.

    Frames default to a limit of 100,000; steps, documents, resource bytes,
    output nodes and encoded output bytes default to ``None`` (unlimited).
    The default annotation cap is 262,144 entries across runtime tables,
    including those in nested invocations.
    Nested transforms inherit all settings. Frames are counted
    while live; the other limits are cumulative, including later result
    writes. A failed charge is reported as an uncatchable
    :class:`~pyxsd.xslt.errors.XsltDynamicError` (``PYXSD-XSLT-001``).
    """

    __slots__ = (
        "_output_lock",
        "annotations",
        "documents",
        "frames",
        "limits",
        "max_frame_depth",
        "output_bytes",
        "output_nodes",
        "resource_bytes",
        "steps",
    )

    def __init__(self, limits: ExecutionLimits | None = None) -> None:
        if limits is None:
            limits = ExecutionLimits()
        if not isinstance(limits, ExecutionLimits):
            raise TypeError("limits must be an ExecutionLimits value")
        self.limits = limits
        self.annotations = 0
        self.steps = 0
        self.frames = 0
        self.documents = 0
        self.output_nodes = 0
        self.output_bytes = 0
        self._output_lock = Lock() if limits.max_output_bytes is not None else None
        self.resource_bytes = 0
        self.max_frame_depth = 0

    def take_step(self) -> bool:
        """Consume one dispatch step, returning whether it was within budget."""
        if self.limits.max_steps is not None and self.steps >= self.limits.max_steps:
            return False
        self.steps += 1
        return True

    def acquire_frame(self) -> bool:
        """Acquire one live frame slot, returning whether it was within budget."""
        if self.limits.max_frames is not None and self.frames >= self.limits.max_frames:
            return False
        self.frames += 1
        self.max_frame_depth = max(self.max_frame_depth, self.frames)
        return True

    def release_frame(self) -> None:
        """Release one live frame slot after a frame leaves every stack."""
        if self.frames > 0:
            self.frames -= 1

    def take_document(self) -> bool:
        """Consume one document slot, returning whether it was within budget."""
        if self.limits.max_documents is not None and self.documents >= self.limits.max_documents:
            return False
        self.documents += 1
        return True

    def take_output_nodes(self, count: int) -> bool:
        """Record delivered result nodes, returning whether they fit the budget."""
        total = self.output_nodes + count
        if self.limits.max_output_nodes is not None and total > self.limits.max_output_nodes:
            return False
        self.output_nodes = total
        return True

    def take_output_bytes(self, count: int) -> bool:
        """Record bytes for a thread-confined counter; use reservations for writes."""
        total = self.output_bytes + count
        if self.limits.max_output_bytes is not None and total > self.limits.max_output_bytes:
            return False
        self.output_bytes = total
        return True

    def charge_resource_bytes(self, count: int) -> None:
        """Charge acquired resource octets against the enabled limit."""
        limit = self.limits.max_resource_bytes
        if limit is None:
            return
        self.resource_bytes += count
        if self.resource_bytes > limit:
            raise resource_error(
                self.exhaustion("resource byte", "max_resource_bytes", self.resource_bytes)
            )

    def charge_annotation(self) -> None:
        """Admit one new annotation across the invocation's runtime tables."""
        limit = self.limits.max_annotations
        if limit is None:
            return
        attempted = self.annotations + 1
        if attempted > limit:
            raise resource_error(
                self.exhaustion("atomic type-annotation", "max_annotations", attempted)
            )
        self.annotations = attempted

    def _take_output_chunk(self, count: int) -> tuple[int, int]:
        """Reserve a chunk's allowed prefix and return (allowed, attempted total)."""
        limit = self.limits.max_output_bytes
        assert limit is not None and self._output_lock is not None
        # Only result writes can spend the balance on more than one thread.
        # Never hold the ledger lock while invoking the caller's destination.
        with self._output_lock:
            attempted = self.output_bytes + count
            allowed = min(count, limit - self.output_bytes)
            self.output_bytes += allowed
            return allowed, attempted

    def exhaustion(self, label: str, field: str, amount: int) -> str:
        """Describe an attempted charge beyond the configured limit."""
        return (
            f"the transformation exceeded its {label} budget: "
            f"{field}={getattr(self.limits, field)}, cumulative amount={amount}"
        )


@dataclass(frozen=True, slots=True)
class FrameDiagnostic:
    """One stable, data-only entry in a transformation diagnostic stack."""

    kind: str
    identity: str | None = None
    component: object | None = None
    package: str | None = None
    mode: str | None = None
    location: IssueLocation | None = None


@dataclass(frozen=True, slots=True)
class Continuation:
    """One pending action after a frame completes.

    ``resume`` delivers the value to *frame* by setting its ``input``.
    ``append`` appends the value to a list *target* and then follows *following*.
    ``convert`` applies *target* to the value and then follows *following*.
    """

    kind: ContinuationKind
    target: object | None = None
    frame: Frame | None = None
    following: Continuation | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("resume", "append", "convert"):
            raise ValueError(f"unknown continuation kind {self.kind!r}")
        if self.kind == "resume":
            if not isinstance(self.frame, Frame):
                raise TypeError("a resume continuation requires a frame")
            if self.target is not None and not isinstance(self.target, Frame):
                raise TypeError("a resume continuation target must be a frame or None")
        elif self.kind == "append":
            if not isinstance(self.target, list):
                raise TypeError("an append continuation requires a list target")
            if self.frame is not None:
                raise ValueError("an append continuation has no frame")
        else:
            if not callable(self.target):
                raise TypeError("a convert continuation requires a callable target")
            if self.frame is not None:
                raise ValueError("a convert continuation has no frame")


ErrorHandler: TypeAlias = Callable[[BaseException, tuple[FrameDiagnostic, ...]], object | None]


@dataclass(frozen=True, slots=True)
class Frame:
    """One heap-resident execution frame.

    ``program`` and ``cursor`` are opaque to the scheduler: the runtime's step
    function interprets them.  The remaining fields are the semantic state the
    scheduler must preserve when it pushes, replaces, or unwinds a frame.
    """

    kind: str = "instruction"
    component: object | None = None
    identity: str | None = None
    program: object | None = None
    cursor: object = 0
    context: EvaluationContext = field(default_factory=EvaluationContext)
    continuation: Continuation | None = None
    extent: object | None = None
    error_handler: ErrorHandler | None = None
    location: IssueLocation | None = None
    tail: bool = False
    values: tuple[object, ...] = ()
    input: object = NO_INPUT
    conversion: Callable[[object], object] | None = None
    group: object = NO_GROUP
    grouping_key: object = NO_GROUP
    iteration: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("a frame requires a non-empty kind")
        if not isinstance(self.context, EvaluationContext):
            raise TypeError("a frame context must be an EvaluationContext")
        if self.error_handler is not None and not callable(self.error_handler):
            raise TypeError("a frame error handler must be callable")
        if self.conversion is not None and not callable(self.conversion):
            raise TypeError("a frame conversion must be callable")
        if not isinstance(self.tail, bool):
            raise TypeError("tail must be a boolean")
        if not isinstance(self.values, tuple):
            raise TypeError("frame values must be a tuple")
        if self.identity is not None and not isinstance(self.identity, str):
            raise TypeError("a frame identity must be a string or None")
        if self.location is not None and not isinstance(self.location, IssueLocation):
            raise TypeError("a frame location must be an IssueLocation or None")
        if self.group is not NO_GROUP and not isinstance(self.group, tuple):
            raise TypeError("a frame group must be an XDM sequence or NO_GROUP")
        if self.grouping_key is not NO_GROUP and not isinstance(self.grouping_key, tuple):
            raise TypeError("a frame grouping key must be an XDM sequence or NO_GROUP")

    @property
    def variables(self) -> Mapping[str, object]:
        """Lexical variables owned by this frame's evaluation context."""
        return self.context.locals

    @property
    def tunnel_parameters(self) -> Mapping[str, object]:
        """Tunnel parameters owned by this frame's evaluation context."""
        return self.context.tunnel_parameters

    @property
    def focus(self) -> Focus:
        """The context item, position, and size of this frame."""
        return self.context.focus

    @property
    def package(self) -> str | None:
        """The package whose components this frame executes."""
        return self.context.package_name

    @property
    def mode(self) -> str | None:
        """The mode this frame executes in."""
        return self.context.mode

    @property
    def final_output(self) -> bool:
        """Whether this frame produces final rather than temporary output."""
        return self.context.final_output

    def diagnostic(self) -> FrameDiagnostic:
        """One data-only diagnostic entry for this frame."""
        return FrameDiagnostic(
            kind=self.kind,
            identity=self.identity,
            component=self.component,
            package=self.package,
            mode=self.mode,
            location=self.location,
        )


@dataclass(frozen=True, slots=True)
class Continue:
    """The step produced an updated frame that remains on top of the stack."""

    frame: Frame


@dataclass(frozen=True, slots=True)
class Call:
    """The step requests a child frame.

    ``resume`` is the caller frame to restore after a non-eliminable call.
    ``tail`` marks a semantically safe tail call; the scheduler may replace the
    caller in place instead of pushing another frame.
    """

    frame: Frame
    resume: Frame | None = None
    tail: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.frame, Frame):
            raise TypeError("a call requires a child frame")
        if self.resume is not None and not isinstance(self.resume, Frame):
            raise TypeError("a call resume must be a frame or None")
        if not isinstance(self.tail, bool):
            raise TypeError("a call tail flag must be a boolean")


@dataclass(frozen=True, slots=True)
class Return:
    """The current frame completed with one value."""

    value: object


@dataclass(frozen=True, slots=True)
class Raise:
    """The step failed with one exception."""

    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise TypeError("a raise result requires an exception")


FrameResult: TypeAlias = Continue | Call | Return | Raise


@dataclass(frozen=True, slots=True)
class CatchResult:
    """An error handler's request to complete the frame with one value.

    ``value`` completes the frame directly.  ``frame`` instead schedules a
    catch body on the same explicit stack, inheriting the failed frame's
    continuation, so a tail call in the catch body remains eligible for
    scheduler replacement.  The two forms are mutually exclusive.
    """

    value: object = ()
    frame: Frame | None = None

    def __post_init__(self) -> None:
        if self.frame is None:
            return
        if not isinstance(self.frame, Frame):
            raise TypeError("a catch result frame must be a Frame or None")
        if self.value != ():
            raise ValueError("a catch result cannot carry both a frame and a value")


class SchedulerViolation(RuntimeError):
    """The runtime step function violated the frame protocol."""


class Scheduler:
    """Execute frames on one explicit heap stack until a value is returned."""

    def __init__(
        self,
        limits: ExecutionLimits | None = None,
        *,
        budget: ExecutionBudget | None = None,
    ) -> None:
        if budget is None:
            budget = ExecutionBudget(limits)
        elif not isinstance(budget, ExecutionBudget):
            raise TypeError("budget must be an ExecutionBudget value")
        self.limits = budget.limits
        self.budget = budget
        self._frames: list[Frame] = []
        self._active = False
        self._result: object = None
        self._has_result = False
        self.replaced_frames = 0
        self.diagnostics: tuple[FrameDiagnostic, ...] = ()

    @property
    def active(self) -> bool:
        """Whether a run is currently dispatching frames."""
        return self._active

    @property
    def frame_depth(self) -> int:
        """The number of live frames during a run."""
        return len(self._frames)

    @property
    def steps(self) -> int:
        """Dispatch steps consumed by this scheduler's shared budget."""
        return self.budget.steps

    @property
    def max_frame_depth(self) -> int:
        """Deepest live frame count observed by this scheduler's budget."""
        return self.budget.max_frame_depth

    def run(self, entry: Frame, step: Callable[[Frame], FrameResult]) -> object:
        """Run *entry* to completion with *step* producing frame transitions."""
        if self._active:
            raise SchedulerViolation("a scheduler cannot run reentrantly")
        if not isinstance(entry, Frame):
            raise TypeError("a scheduler entry must be a Frame")
        if not callable(step):
            raise TypeError("a scheduler step must be callable")
        self._frames = []
        self._result = None
        self._has_result = False
        self.diagnostics = ()
        self._active = True
        try:
            self._push(entry)
            while self._frames:
                frame = self._frames[-1]
                if not self.budget.take_step():
                    raise self._resource_error(
                        self.budget.exhaustion("step", "max_steps", self.budget.steps + 1)
                    )
                try:
                    outcome = step(frame)
                except RecursionError as exc:
                    raise self._python_stack_error() from exc
                except BaseException as exc:
                    self._unwind(exc)
                    continue
                try:
                    self._handle_outcome(frame, outcome)
                except RecursionError as exc:
                    raise self._python_stack_error() from exc
            return self._result
        finally:
            self._active = False
            self._release_all_frames()

    def _push(self, frame: Frame) -> None:
        if not isinstance(frame, Frame):  # pragma: no cover - callers validate
            raise TypeError("a scheduler frame must be a Frame")
        if not self.budget.acquire_frame():
            raise self._resource_error(
                self.budget.exhaustion("frame", "max_frames", self.budget.frames + 1)
            )
        self._frames.append(frame)

    def _release_all_frames(self) -> None:
        for _ in self._frames:
            self.budget.release_frame()
        self._frames = []

    def _handle_outcome(self, frame: Frame, outcome: FrameResult) -> None:
        if isinstance(outcome, Continue):
            self._frames[-1] = outcome.frame
            return
        if isinstance(outcome, Return):
            self._frames.pop()
            self.budget.release_frame()
            self._finish(frame, outcome.value)
            return
        if isinstance(outcome, Raise):
            self._unwind(outcome.error)
            return
        if isinstance(outcome, Call):
            self._call(frame, outcome)
            return
        raise SchedulerViolation(f"unknown frame result {type(outcome).__name__}")

    def _call(self, frame: Frame, outcome: Call) -> None:
        child = outcome.frame
        caller = outcome.resume if outcome.resume is not None else frame
        if (
            outcome.tail
            and self._tail_safe(caller, child)
            and not (child.continuation is not None and caller.continuation is not None)
        ):
            continuation = caller.continuation if child.continuation is None else child.continuation
            self._frames[-1] = replace(
                child,
                continuation=continuation,
                values=caller.values + child.values,
            )
            self.replaced_frames += 1
            return
        if outcome.resume is None:
            raise SchedulerViolation(
                "a call that cannot replace its caller requires a resume frame"
            )
        if child.continuation is None:
            child = replace(
                child,
                continuation=Continuation(
                    "resume",
                    target=frame,
                    frame=outcome.resume,
                ),
            )
        self._push(child)

    def _tail_safe(self, frame: Frame, child: Frame) -> bool:
        """Whether a tail call may discard *frame* instead of keeping it."""
        if frame.error_handler is not None:
            return False
        if frame.extent is not None:
            return False
        if frame.conversion is not None:
            return False
        if child.conversion is not None and (frame.values or child.values):
            return False
        continuation = frame.continuation
        return continuation is None or continuation.kind == "resume"

    def _finish(self, frame: Frame, value: object) -> None:
        self._commit(frame)
        if frame.continuation is None:
            self._result = value
            self._has_result = True
            return
        self._continue(frame.continuation, value)

    def _continue(self, continuation: Continuation, value: object) -> None:
        if continuation.kind == "resume":
            assert isinstance(continuation.frame, Frame)
            resumed = replace(continuation.frame, input=value)
            if isinstance(continuation.target, Frame):
                for index in range(len(self._frames) - 1, -1, -1):
                    if self._frames[index] is continuation.target:
                        self._frames[index] = resumed
                        return
            self._push(resumed)
            return
        if continuation.kind == "append":
            assert isinstance(continuation.target, list)
            continuation.target.append(value)
            if continuation.following is not None:
                self._continue(continuation.following, value)
            else:
                self._result = value
                self._has_result = True
            return
        assert callable(continuation.target)
        converted = continuation.target(value)
        if continuation.following is None:
            self._result = converted
            self._has_result = True
            return
        self._continue(continuation.following, converted)

    def _unwind(self, error: BaseException) -> None:
        stack = tuple(frame.diagnostic() for frame in self._frames)
        self.diagnostics = stack
        while self._frames:
            frame = self._frames.pop()
            self.budget.release_frame()
            self._rollback(frame)
            if frame.error_handler is None:
                continue
            handled = frame.error_handler(error, stack)
            if handled is None:
                continue
            if not isinstance(handled, CatchResult):
                raise SchedulerViolation(
                    "an error handler must return CatchResult or None"
                ) from error
            if handled.frame is not None:
                catch_frame = handled.frame
                if frame.continuation is not None:
                    catch_frame = replace(catch_frame, continuation=frame.continuation)
                self._push(catch_frame)
                return
            if frame.continuation is None:
                self._result = handled.value
                self._has_result = True
            else:
                self._continue(frame.continuation, handled.value)
            return
        raise error

    def _rollback(self, frame: Frame) -> None:
        rollback = getattr(frame.extent, "rollback", None)
        if callable(rollback):
            rollback()

    def _commit(self, frame: Frame) -> None:
        commit = getattr(frame.extent, "commit", None)
        if callable(commit):
            commit()

    def _python_stack_error(self) -> XsltDynamicError:
        limit = self.limits.max_frames
        configured = "None (no frame budget)" if limit is None else str(limit)
        return self._resource_error(
            "the transformation exceeded the Python recursion limit: "
            f"Python recursion limit={sys.getrecursionlimit()}, "
            f"max_frames={configured}, frame depth={self.budget.frames}"
        )

    def _resource_error(self, message: str) -> XsltDynamicError:
        stack = tuple(frame.diagnostic() for frame in self._frames)
        if stack:
            self.diagnostics = stack
        return resource_error(message)
