"""Outcome taxonomy and the result of one W3C XSLT test case.

Every executed, skipped, or disputed case lands in exactly one :class:`Outcome`
bucket.  The buckets stay separate so a report never turns a harness gap, an
unimplemented optional feature, a timeout, or a wrong error code into a pass.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass


class Outcome(enum.StrEnum):
    """The classification of one catalog case."""

    #: Result matched the expectation, including a strict expected error code.
    PASS = "pass"
    #: An error was expected but a different code was raised.
    WRONG_ERROR_CODE = "wrong-error-code"
    #: The result contradicted the expectation.
    FAIL = "fail"
    #: The declared feature profile excludes the case.
    NOT_APPLICABLE = "not-applicable"
    #: The case needs an optional feature this implementation does not provide.
    UNSUPPORTED_FEATURE = "unsupported-feature"
    #: The harness cannot execute or assert part of the case.
    ADAPTER_GAP = "adapter-gap"
    #: The case exceeded its time or resource budget.
    TIMEOUT = "timeout"
    #: The catalog expectation is disputed and needs review.
    CORPUS_DISPUTE = "corpus-dispute"


#: Outcomes that cannot have executed an assertion family.
_UNEXECUTED_OUTCOMES = frozenset(
    {Outcome.NOT_APPLICABLE, Outcome.UNSUPPORTED_FEATURE, Outcome.TIMEOUT}
)

#: The assertion family exercised by :meth:`CaseResult.expected_error`.
EXPECTED_ERROR_FAMILY = "expected-error"


@dataclass(frozen=True)
class CaseResult:
    """The explicit outcome of one case and the assertions that actually ran.

    ``coverage`` names assertion families, not catalog cases: an unimplemented
    assertion is an adapter gap and must never appear here.  Outcomes that
    cannot execute assertions reject coverage outright.  Coverage is normalised
    to an immutable frozenset.
    """

    outcome: Outcome
    name: str = ""
    expected_error_code: str | None = None
    actual_error_code: str | None = None
    message: str = ""
    coverage: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "coverage", frozenset(self.coverage))
        if self.outcome in _UNEXECUTED_OUTCOMES and self.coverage:
            raise ValueError(f"{self.outcome.value} results cannot record assertion coverage")
        if self.outcome is Outcome.WRONG_ERROR_CODE and (
            not self.expected_error_code
            or not self.actual_error_code
            or self.expected_error_code == self.actual_error_code
        ):
            raise ValueError("wrong-error-code results require distinct expected and actual codes")
        if (
            self.outcome is Outcome.PASS
            and self.expected_error_code is not None
            and self.actual_error_code is None
        ):
            raise ValueError("a passing expected-error result requires the actual code")

    @classmethod
    def passed(
        cls,
        name: str = "",
        *,
        coverage: Iterable[str] = (),
        message: str = "",
    ) -> CaseResult:
        """A result that matched its declared expectation."""
        return cls(Outcome.PASS, name=name, message=message, coverage=frozenset(coverage))

    @classmethod
    def failed(
        cls,
        name: str = "",
        *,
        message: str = "",
        coverage: Iterable[str] = (),
    ) -> CaseResult:
        """A result that contradicted its declared expectation."""
        return cls(Outcome.FAIL, name=name, message=message, coverage=frozenset(coverage))

    @classmethod
    def expected_error(
        cls,
        expected: str,
        actual: str | None = None,
        *,
        name: str = "",
        coverage: Iterable[str] = (),
    ) -> CaseResult:
        """Compare a strictly required error code with the code that occurred.

        A different code is :attr:`Outcome.WRONG_ERROR_CODE` even where broad
        conformance would accept any error.  An expected error that never
        occurs is a failure.  ``"*"`` accepts any code, but still requires an
        error.
        """
        expected = expected.strip()
        if not expected:
            raise ValueError("an expected error code is required")
        families = frozenset(coverage) | {EXPECTED_ERROR_FAMILY}
        if actual is None:
            return cls(
                Outcome.FAIL,
                name=name,
                expected_error_code=expected,
                message=f"expected error {expected} but no error was raised",
                coverage=families,
            )
        actual = actual.strip()
        if not actual:
            raise ValueError("an actual error code cannot be empty")
        if expected == "*" or actual == expected:
            return cls(
                Outcome.PASS,
                name=name,
                expected_error_code=expected,
                actual_error_code=actual,
                message=f"expected error {expected}",
                coverage=families,
            )
        return cls(
            Outcome.WRONG_ERROR_CODE,
            name=name,
            expected_error_code=expected,
            actual_error_code=actual,
            message=f"expected error {expected} but got {actual}",
            coverage=families,
        )

    @classmethod
    def not_applicable(cls, name: str = "", *, reason: str = "") -> CaseResult:
        """The declared profile excludes the case; nothing was executed."""
        return cls(Outcome.NOT_APPLICABLE, name=name, message=reason)

    @classmethod
    def unsupported_feature(cls, name: str = "", *, feature: str = "") -> CaseResult:
        """The case needs an optional feature the implementation lacks."""
        message = f"unsupported feature: {feature}" if feature else "unsupported feature"
        return cls(Outcome.UNSUPPORTED_FEATURE, name=name, message=message)

    @classmethod
    def adapter_gap(
        cls,
        name: str = "",
        *,
        detail: str = "",
        coverage: Iterable[str] = (),
    ) -> CaseResult:
        """The harness could not drive part of the case.

        ``coverage`` still names only assertion families that actually ran
        before the gap; a skipped family is never recorded.
        """
        return cls(Outcome.ADAPTER_GAP, name=name, message=detail, coverage=frozenset(coverage))

    @classmethod
    def timeout(cls, name: str = "", *, detail: str = "") -> CaseResult:
        """The case exceeded its time or resource budget; no assertion ran."""
        return cls(Outcome.TIMEOUT, name=name, message=detail)

    @classmethod
    def corpus_dispute(
        cls,
        name: str = "",
        *,
        detail: str = "",
        coverage: Iterable[str] = (),
    ) -> CaseResult:
        """The catalog expectation is disputed; record what actually ran."""
        return cls(
            Outcome.CORPUS_DISPUTE,
            name=name,
            message=detail,
            coverage=frozenset(coverage),
        )
