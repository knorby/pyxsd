"""Heap-resident XSLT frames and the scheduler that executes them.

A transformation never maps one lexical XSLT nesting level to one Python call.
Every component invocation, template-rule dispatch, and suspended XPath
evaluation is a :class:`Frame` held by the scheduler's explicit stack.  A frame
carries its instruction cursor, lexical variables, focus, return continuation,
result-builder extent, error handler, component identity, package, and mode.

Tail positions are marked by the compiler, not guessed by the scheduler.  A
call is replaced in place only when the scheduler can prove that the calling
frame has no pending conversion, error handler, builder extent, or result
continuation beyond returning to its own caller.  Otherwise the caller frame is
kept as a heap continuation.

Budget exhaustion raises :class:`~pyxsd.xslt.errors.XsltDynamicError` with the
stable implementation code ``PYXSD-XSLT-001``.  A Python ``RecursionError`` is
never presented as the transformation's resource diagnostic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal, TypeAlias

from pyxsd.locations import IssueLocation
from pyxsd.xpath.environment import Focus
from pyxsd.xslt.errors import XsltDynamicError

__all__ = [
    "NO_GROUP",
    "NO_INPUT",
    "PYXSD_FRAME_EXHAUSTED",
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
]

#: Stable implementation error code for transformation resource limits.
PYXSD_FRAME_EXHAUSTED = "PYXSD-XSLT-001"

#: Sentinel distinguishing "no delivered child result" from an XDM value.
NO_INPUT = object()

#: Sentinel distinguishing an absent current group/key from an empty sequence.
NO_GROUP = object()

ContinuationKind: TypeAlias = Literal["resume", "append", "convert"]


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Explicit heap budgets for one transformation.

    ``max_frames`` bounds the live explicit frame stack.  ``max_steps`` bounds
    scheduler dispatch steps when set; ``None`` leaves the step count
    unbounded while still recording it.
    """

    max_frames: int = 100_000
    max_steps: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.max_frames, bool) or not isinstance(self.max_frames, int):
            raise TypeError("max_frames must be an integer")
        if self.max_frames < 1:
            raise ValueError("max_frames must be at least 1")
        if self.max_steps is not None:
            if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int):
                raise TypeError("max_steps must be an integer or None")
            if self.max_steps < 1:
                raise ValueError("max_steps must be at least 1")


class ExecutionBudget:
    """Live frame and step counters shared by nested schedulers.

    One budget belongs to a transformation.  Top-level and nested schedulers
    acquire and release frames from the same counters, so a nested execution
    cannot reset a caller's configured limits.
    """

    __slots__ = ("frames", "limits", "max_frame_depth", "steps")

    def __init__(self, limits: ExecutionLimits | None = None) -> None:
        if limits is None:
            limits = ExecutionLimits()
        if not isinstance(limits, ExecutionLimits):
            raise TypeError("limits must be an ExecutionLimits value")
        self.limits = limits
        self.steps = 0
        self.frames = 0
        self.max_frame_depth = 0

    def take_step(self) -> bool:
        """Consume one dispatch step, returning whether it was within budget."""
        if self.limits.max_steps is not None and self.steps >= self.limits.max_steps:
            return False
        self.steps += 1
        return True

    def acquire_frame(self) -> bool:
        """Acquire one live frame slot, returning whether it was within budget."""
        if self.frames >= self.limits.max_frames:
            return False
        self.frames += 1
        self.max_frame_depth = max(self.max_frame_depth, self.frames)
        return True

    def release_frame(self) -> None:
        """Release one live frame slot after a frame leaves every stack."""
        if self.frames > 0:
            self.frames -= 1


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
    variables: Mapping[str, object] = field(default_factory=dict)
    tunnel_parameters: Mapping[str, object] = field(default_factory=dict)
    focus: Focus = field(default_factory=Focus.absent)
    continuation: Continuation | None = None
    extent: object | None = None
    error_handler: ErrorHandler | None = None
    package: str | None = None
    mode: str | None = None
    location: IssueLocation | None = None
    final_output: bool = True
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
        if not isinstance(self.variables, Mapping):
            raise TypeError("frame variables must be a mapping")
        object.__setattr__(self, "variables", MappingProxyType(dict(self.variables)))
        if not isinstance(self.tunnel_parameters, Mapping):
            raise TypeError("frame tunnel parameters must be a mapping")
        object.__setattr__(
            self,
            "tunnel_parameters",
            MappingProxyType(dict(self.tunnel_parameters)),
        )
        if not isinstance(self.focus, Focus):
            raise TypeError("a frame focus must be a Focus")
        if self.error_handler is not None and not callable(self.error_handler):
            raise TypeError("a frame error handler must be callable")
        if self.conversion is not None and not callable(self.conversion):
            raise TypeError("a frame conversion must be callable")
        if not isinstance(self.final_output, bool):
            raise TypeError("final_output must be a boolean")
        if not isinstance(self.tail, bool):
            raise TypeError("tail must be a boolean")
        if not isinstance(self.values, tuple):
            raise TypeError("frame values must be a tuple")
        if self.identity is not None and not isinstance(self.identity, str):
            raise TypeError("a frame identity must be a string or None")
        if self.package is not None and not isinstance(self.package, str):
            raise TypeError("a frame package must be a string or None")
        if self.mode is not None and not isinstance(self.mode, str):
            raise TypeError("a frame mode must be a string or None")
        if self.location is not None and not isinstance(self.location, IssueLocation):
            raise TypeError("a frame location must be an IssueLocation or None")
        if self.group is not NO_GROUP and not isinstance(self.group, tuple):
            raise TypeError("a frame group must be an XDM sequence or NO_GROUP")
        if self.grouping_key is not NO_GROUP and not isinstance(self.grouping_key, tuple):
            raise TypeError("a frame grouping key must be an XDM sequence or NO_GROUP")

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
    """An error handler's request to complete the frame with one value."""

    value: object = ()


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
                    raise self._resource_error("the transformation exceeded its step budget")
                try:
                    outcome = step(frame)
                except RecursionError as exc:
                    raise self._resource_error(
                        "the transformation exceeded the Python recursion limit"
                    ) from exc
                except BaseException as exc:
                    self._unwind(exc)
                    continue
                self._handle_outcome(frame, outcome)
            return self._result
        finally:
            self._active = False
            self._release_all_frames()

    def _push(self, frame: Frame) -> None:
        if not isinstance(frame, Frame):  # pragma: no cover - callers validate
            raise TypeError("a scheduler frame must be a Frame")
        if not self.budget.acquire_frame():
            raise self._resource_error("the transformation exceeded its frame budget")
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

    def _resource_error(self, message: str) -> XsltDynamicError:
        stack = tuple(frame.diagnostic() for frame in self._frames)
        if stack:
            self.diagnostics = stack
        return XsltDynamicError(message, code=PYXSD_FRAME_EXHAUSTED)
