"""Compile pinned RELAX NG schema cases and classify what happened.

A :class:`CaseResult` separates a wrong verdict from every reason the compiler
reached no verdict at all.  Unsupported capabilities, exhausted work budgets,
per-case timeouts, and unexpected exceptions are adapter gaps: none of them is
a correct rejection of an incorrect schema.  Instance assertions belong to the
later validation phase, so the driver reports them as deferred capability gaps
instead of inventing a verdict.
"""

from __future__ import annotations

import contextlib
import enum
import signal
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.validation import ValidationReport

from .model import INSTANCE_KINDS, CorpusCase, Outcome

__all__ = [
    "DEFAULT_TIMEOUT",
    "CaseFailure",
    "CaseResult",
    "PyXSDDriver",
    "time_limit",
]

#: Default per-case wall-clock budget, in seconds.
DEFAULT_TIMEOUT = 60.0

CompileCallable = Callable[..., RelaxNGSchema]


class CaseFailure(enum.StrEnum):
    """The audit-level reason behind a :class:`CaseResult`."""

    #: The compiler's verdict matched the catalog expectation.
    NONE = "none"
    #: The compiler reached a verdict that contradicted the expectation.
    WRONG_VERDICT = "wrong-verdict"
    #: A capability gap: an unsupported library/type or another expected refusal.
    ADAPTER_GAP = "adapter-gap"
    #: The per-case wall-clock budget expired.
    TIMEOUT = "timeout"
    #: An unexpected non-pyxsd exception escaped compilation.
    CRASH = "crash"
    #: A configured compiler work budget was exhausted.
    LIMIT = "limit"


@dataclass(frozen=True)
class CaseResult:
    """One tested corpus case, with its settled outcome and audit detail."""

    case: CorpusCase
    outcome: Outcome
    failure: CaseFailure
    expected_valid: bool | None
    actual_valid: bool | None
    detail: str | None = None

    @property
    def satisfies_expectation(self) -> bool:
        """Whether the result is a matching verdict for the case."""
        return self.outcome is Outcome.PASS


def _alarm_available() -> bool:
    return (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )


@contextlib.contextmanager
def time_limit(seconds: float | None) -> Iterator[None]:
    """Raise :class:`TimeoutError` when the block exceeds *seconds*.

    Uses ``SIGALRM`` on the main thread.  Where that facility is unavailable
    (Windows, a worker thread) the limit is skipped rather than silently
    reported as a violation.
    """
    if seconds is None or not _alarm_available():
        yield
        return

    def handle(signum: int, frame: object) -> None:
        del signum, frame
        raise TimeoutError(f"case exceeded {seconds:g}s")

    previous = signal.signal(signal.SIGALRM, handle)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _expected_valid(case: CorpusCase) -> bool | None:
    if case.kind == "correct-schema":
        return True
    if case.kind == "incorrect-schema":
        return False
    return None


def _gap(case: CorpusCase, failure: CaseFailure, detail: str) -> CaseResult:
    return CaseResult(
        case=case,
        outcome=Outcome.ADAPTER_GAP,
        failure=failure,
        expected_valid=_expected_valid(case),
        actual_valid=None,
        detail=detail,
    )


def _verdict_detail(report: ValidationReport, expected_valid: bool) -> str:
    if expected_valid:
        codes = sorted({issue.code for issue in report.issues})
        observed = ", ".join(codes) if codes else "no codes"
        return f"expected a correct schema, observed schema errors: {observed}"
    return "expected an incorrect schema, observed no schema errors"


class PyXSDDriver:
    """Run schema corpus cases through :class:`~pyxsd.relaxng.schema.RelaxNGSchema`.

    ``compiler`` is replaceable so synthetic tests can force each failure
    classification without depending on a particular compiler defect.
    """

    def __init__(
        self,
        *,
        timeout: float | None = DEFAULT_TIMEOUT,
        limits: RelaxNGLimits | None = None,
        compiler: CompileCallable = RelaxNGSchema.compile,
    ) -> None:
        self.timeout = timeout
        self.limits = limits
        self._compiler = compiler

    def run(self, case: CorpusCase) -> CaseResult:
        """Compile *case* and settle its outcome."""
        if case.kind in INSTANCE_KINDS:
            return _gap(
                case,
                CaseFailure.ADAPTER_GAP,
                "instance validation is not supported in the compiler phase",
            )

        expected_valid = case.kind == "correct-schema"
        try:
            with time_limit(self.timeout):
                schema = self._compiler(case.schema, limits=self.limits)
        except RelaxNGLimitError as exc:
            return _gap(case, CaseFailure.LIMIT, f"limit: {exc}")
        except TimeoutError as exc:
            return _gap(case, CaseFailure.TIMEOUT, f"timeout: {exc}")
        except PyXSDError as exc:
            return _gap(case, CaseFailure.ADAPTER_GAP, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # the taxonomy records every crash
            return _gap(case, CaseFailure.CRASH, f"{type(exc).__name__}: {exc}")

        actual_valid = not schema.report.has_errors
        if actual_valid == expected_valid:
            return CaseResult(
                case=case,
                outcome=Outcome.PASS,
                failure=CaseFailure.NONE,
                expected_valid=expected_valid,
                actual_valid=actual_valid,
            )
        return CaseResult(
            case=case,
            outcome=Outcome.FAIL,
            failure=CaseFailure.WRONG_VERDICT,
            expected_valid=expected_valid,
            actual_valid=actual_valid,
            detail=_verdict_detail(schema.report, expected_valid),
        )
