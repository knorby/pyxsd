"""Outcome taxonomy and expected-versus-actual comparison.

A run of one test produces an :class:`EngineResult` from a driver.  The
comparison against the selected expectation yields one :class:`Outcome`.
Every test must land in exactly one bucket; nothing is silently dropped.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Outcome(enum.StrEnum):
    """The classification of one tested case."""

    #: Engine verdict matched the selected expectation.
    PASS = "pass"
    #: Engine verdict contradicted the selected expectation.
    FAIL = "fail"
    #: Profile excluded the test (its version tokens do not apply).
    NOT_APPLICABLE = "not-applicable"
    #: The expectation is not a Boolean (notKnown, indeterminate, ...).
    NOT_CHECKABLE = "not-checkable"
    #: The harness cannot drive the case (no schema, unrepresentable bundle).
    ADAPTER_GAP = "adapter-gap"
    #: The catalogue metadata is ambiguous or incomplete.
    METADATA_ERROR = "metadata-error"
    #: The engine did not finish within the time budget.
    TIMEOUT = "timeout"
    #: The engine or harness raised unexpectedly.
    ERROR = "error"


#: Outcomes that mean "the engine is missing something" and are tracked for regressions.
FAILING_OUTCOMES = frozenset({Outcome.FAIL})


@dataclass
class EngineResult:
    """What a driver observed for one case.

    ``schema_valid`` and ``instance_valid`` are ``None`` when the phase could
    not be evaluated.  ``adapter_gap`` names why the harness could not drive
    the case at all; when set, the other fields are ignored.
    """

    schema_valid: bool | None = None
    instance_valid: bool | None = None
    schema_error: str | None = None
    instance_error: str | None = None
    adapter_gap: str | None = None
    timeout: bool = False
    error: str | None = None


def classify(
    expected_valid: bool | None,
    actual_valid: bool | None,
    *,
    applicable: bool = True,
    engine: EngineResult | None = None,
) -> Outcome:
    """Compare one phase's expectation and observation into an outcome."""
    if not applicable:
        return Outcome.NOT_APPLICABLE
    if engine is not None:
        if engine.adapter_gap is not None:
            return Outcome.ADAPTER_GAP
        if engine.timeout:
            return Outcome.TIMEOUT
        if engine.error is not None:
            return Outcome.ERROR
    if expected_valid is None:
        return Outcome.NOT_CHECKABLE
    if actual_valid is None:
        return Outcome.ERROR
    return Outcome.PASS if actual_valid == expected_valid else Outcome.FAIL


def is_regression(previous: Outcome | None, current: Outcome) -> bool:
    """Whether moving from *previous* to *current* is a change worth flagging.

    A pass becoming a fail is the classic regression, but a fail becoming a
    pass is equally notable: it means the baseline is stale and the suite is
    now measuring something the project has not acknowledged.  Both are
    reported as changes; the baseliner decides whether to accept them.
    """
    if previous is None:
        return False
    return previous != current
