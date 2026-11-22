"""Compile pinned RELAX NG cases and validate their instance assertions.

A :class:`CaseResult` separates a wrong verdict from every reason the driver
reached no verdict at all.  Unsupported capabilities, exhausted work budgets,
per-case timeouts, and unexpected exceptions are adapter gaps: none of them is
a correct rejection of an incorrect schema or an invalid instance.  An
instance assertion has two phases: its schema must compile, and its instance
must then be well-formed XML.  A schema rejection is a schema-phase gap, never
an invalid-instance pass, and malformed XML is an instance-phase gap, never a
validity verdict.

A compact-syntax rejection is a settled verdict for an incorrect-schema case.
It remains an adapter gap for a correct schema (which must parse) and a
schema-phase gap for an instance assertion.  The compact profile's
correct-schema verdict is parser acceptance: the upstream catalog asserts
compact syntax and translation, and some accepted fragments are not legal
whole schemas.  Compiled translation parity is checked separately.
"""

from __future__ import annotations

import contextlib
import enum
import signal
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng.document import RelaxNGDocument
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError, RelaxNGLimitError
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.validation import ValidationReport

from .model import INSTANCE_KINDS, PROFILE_COMPACT_SYNTAX, CorpusCase, Outcome

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
#: Inject a parse implementation for synthetic classification tests.
#: ``schema.parse(path, typed=False, limits=...)`` is the corpus default.
ParseCallable = Callable[[RelaxNGSchema, Path, RelaxNGLimits | None], RelaxNGDocument]


class CaseFailure(enum.StrEnum):
    """The audit-level reason behind a :class:`CaseResult`."""

    #: The compiler's or validator's verdict matched the catalog expectation.
    NONE = "none"
    #: A verdict that contradicted the expectation.
    WRONG_VERDICT = "wrong-verdict"
    #: A compact-syntax rejection.  It is a verdict for an incorrect-schema
    #: case, a gap for a correct schema, and a schema-phase gap for an instance.
    COMPACT_SYNTAX = "compact-syntax"
    #: A capability gap: an unsupported library/type or another expected refusal.
    ADAPTER_GAP = "adapter-gap"
    #: The per-case wall-clock budget expired.
    TIMEOUT = "timeout"
    #: An unexpected non-pyxsd exception escaped compilation or validation.
    CRASH = "crash"
    #: A configured work budget was exhausted.
    LIMIT = "limit"
    #: The instance source could not be read or was not well-formed XML.
    INVALID_XML = "invalid-xml"
    #: The instance's schema did not compile, so no instance verdict exists.
    SCHEMA_REJECTED = "schema-rejected"


@dataclass(frozen=True)
class CaseResult:
    """One tested corpus case, with its settled outcome and audit detail.

    ``phase`` names where the result was settled.  Schema assertions always
    settle in ``"schema"``.  Instance assertions settle in ``"instance"`` when
    a compiled schema reached a validation verdict, and in ``"schema"`` when
    the schema itself was rejected first.
    """

    case: CorpusCase
    outcome: Outcome
    failure: CaseFailure
    expected_valid: bool | None
    actual_valid: bool | None
    detail: str | None = None
    phase: str = "schema"

    @property
    def satisfies_expectation(self) -> bool:
        """Whether the result is a matching verdict for the case."""
        return self.outcome is Outcome.PASS


@dataclass(frozen=True)
class _Compilation:
    """One schema-compilation observation shared by a case group."""

    schema: RelaxNGSchema | None
    failure: CaseFailure
    detail: str | None = None


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
    return case.kind == "valid-instance"


def _gap(
    case: CorpusCase,
    failure: CaseFailure,
    detail: str,
    *,
    phase: str = "schema",
) -> CaseResult:
    return CaseResult(
        case=case,
        outcome=Outcome.ADAPTER_GAP,
        failure=failure,
        expected_valid=_expected_valid(case),
        actual_valid=None,
        detail=detail,
        phase=phase,
    )


def _verdict_detail(report: ValidationReport, expected_valid: bool) -> str:
    if expected_valid:
        codes = sorted({issue.code for issue in report.issues})
        observed = ", ".join(codes) if codes else "no codes"
        return f"expected a correct schema, observed schema errors: {observed}"
    return "expected an incorrect schema, observed no schema errors"


def _instance_verdict_detail(report: ValidationReport, expected_valid: bool) -> str:
    if expected_valid:
        codes = sorted({issue.code for issue in report.issues})
        observed = ", ".join(codes) if codes else "no codes"
        return f"expected a valid instance, observed instance errors: {observed}"
    return "expected an invalid instance, observed no instance errors"


class PyXSDDriver:
    """Run pinned schema and instance corpus cases through the public engine.

    The schema phase uses :meth:`~pyxsd.relaxng.schema.RelaxNGSchema.compile`;
    the instance phase uses ``schema.parse(path, typed=False)``.  Compiled
    schemas are cached per path and case group, so every instance assertion
    still settles independently while its schema compiles once.  ``compiler``
    and ``parser`` are replaceable so synthetic tests can force each failure
    classification without depending on a particular engine defect.
    """

    def __init__(
        self,
        *,
        timeout: float | None = DEFAULT_TIMEOUT,
        limits: RelaxNGLimits | None = None,
        compiler: CompileCallable = RelaxNGSchema.compile,
        parser: ParseCallable | None = None,
    ) -> None:
        self.timeout = timeout
        self.limits = limits
        self._compiler = compiler
        self._parser = parser
        self._compilations: dict[Path, _Compilation] = {}

    def run(self, case: CorpusCase) -> CaseResult:
        """Compile or validate *case* and settle its outcome."""
        if case.kind in INSTANCE_KINDS:
            return self._run_instance(case)
        return self._run_schema(case)

    def _compile(self, path: Path) -> _Compilation:
        """Compile *path* once per driver, caching the settled observation."""
        cached = self._compilations.get(path)
        if cached is not None:
            return cached
        try:
            with time_limit(self.timeout):
                schema = self._compiler(path, limits=self.limits)
        except RelaxNGCompactSyntaxError as exc:
            # A compact syntax rejection is a settled verdict, not a generic
            # input failure.  The case kind decides whether it satisfies the
            # catalog expectation.
            outcome = _Compilation(
                None,
                CaseFailure.COMPACT_SYNTAX,
                f"{type(exc).__name__}: {exc}",
            )
        except RelaxNGLimitError as exc:
            outcome = _Compilation(None, CaseFailure.LIMIT, f"limit: {exc}")
        except TimeoutError as exc:
            outcome = _Compilation(None, CaseFailure.TIMEOUT, f"timeout: {exc}")
        except PyXSDError as exc:
            outcome = _Compilation(None, CaseFailure.ADAPTER_GAP, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # the taxonomy records every crash
            outcome = _Compilation(None, CaseFailure.CRASH, f"{type(exc).__name__}: {exc}")
        else:
            outcome = _Compilation(schema, CaseFailure.NONE)
        self._compilations[path] = outcome
        return outcome

    def _parse(
        self,
        schema: RelaxNGSchema,
        instance: Path,
    ) -> RelaxNGDocument:
        if self._parser is not None:
            return self._parser(schema, instance, self.limits)
        return schema.parse(instance, typed=False, limits=self.limits)

    def _run_schema(self, case: CorpusCase) -> CaseResult:
        expected_valid = case.kind == "correct-schema"
        compiled = self._compile(case.schema)
        if compiled.schema is None:
            assert compiled.detail is not None
            if compiled.failure is CaseFailure.COMPACT_SYNTAX:
                if case.kind == "incorrect-schema":
                    return CaseResult(
                        case=case,
                        outcome=Outcome.PASS,
                        failure=CaseFailure.NONE,
                        expected_valid=False,
                        actual_valid=False,
                    )
                # A correct compact schema must parse; its rejection remains a
                # capability gap, never a passing verdict.
                return _gap(case, CaseFailure.ADAPTER_GAP, compiled.detail)
            return _gap(case, compiled.failure, compiled.detail)

        if case.profile == PROFILE_COMPACT_SYNTAX and case.kind == "correct-schema":
            # The upstream compact catalog asserts compact-syntax acceptance
            # and translation, not whole-schema validity.  A fragment can be
            # accepted compact syntax and still fail a cross-syntax start
            # restriction; compiled translation parity is asserted separately.
            return CaseResult(
                case=case,
                outcome=Outcome.PASS,
                failure=CaseFailure.NONE,
                expected_valid=True,
                actual_valid=True,
            )

        actual_valid = not compiled.schema.report.has_errors
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
            detail=_verdict_detail(compiled.schema.report, expected_valid),
        )

    def _run_instance(self, case: CorpusCase) -> CaseResult:
        assert case.instance is not None
        expected_valid = case.kind == "valid-instance"
        compiled = self._compile(case.schema)
        if compiled.schema is None:
            assert compiled.detail is not None
            failure = (
                CaseFailure.SCHEMA_REJECTED
                if compiled.failure is CaseFailure.COMPACT_SYNTAX
                else compiled.failure
            )
            return _gap(case, failure, compiled.detail)

        if compiled.schema.report.has_errors:
            codes = sorted({issue.code for issue in compiled.schema.report.issues})
            detail = "schema did not compile"
            if codes:
                detail += f": {', '.join(codes)}"
            return _gap(case, CaseFailure.SCHEMA_REJECTED, detail)

        try:
            with time_limit(self.timeout):
                document = self._parse(compiled.schema, case.instance)
        except RelaxNGLimitError as exc:
            return _gap(case, CaseFailure.LIMIT, f"limit: {exc}", phase="instance")
        except TimeoutError as exc:
            return _gap(case, CaseFailure.TIMEOUT, f"timeout: {exc}", phase="instance")
        except PyXSDError as exc:
            return _gap(
                case,
                CaseFailure.INVALID_XML,
                f"{type(exc).__name__}: {exc}",
                phase="instance",
            )
        except Exception as exc:  # the taxonomy records every crash
            return _gap(
                case,
                CaseFailure.CRASH,
                f"{type(exc).__name__}: {exc}",
                phase="instance",
            )

        actual_valid = not document.report.has_errors
        if actual_valid == expected_valid:
            return CaseResult(
                case=case,
                outcome=Outcome.PASS,
                failure=CaseFailure.NONE,
                expected_valid=expected_valid,
                actual_valid=actual_valid,
                phase="instance",
            )
        return CaseResult(
            case=case,
            outcome=Outcome.FAIL,
            failure=CaseFailure.WRONG_VERDICT,
            expected_valid=expected_valid,
            actual_valid=actual_valid,
            detail=_instance_verdict_detail(document.report, expected_valid),
            phase="instance",
        )
