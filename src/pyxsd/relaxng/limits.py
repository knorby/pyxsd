"""Immutable budget declarations for RELAX NG compilation and validation.

``None`` leaves a budget unlimited, which preserves the core's behavior on
trusted input. A configured budget is a non-negative integer and the bound is
inclusive: observing exactly the configured work count succeeds, and the next
unit raises :class:`~pyxsd.relaxng.errors.RelaxNGLimitError`. Exhaustion never
turns into semantic invalidity. ``max_diagnostics`` instead bounds retained
findings; suppressed findings remain counted and do not erase the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from pyxsd.relaxng.errors import RelaxNGLimitError

__all__ = [
    "BudgetCounter",
    "RelaxNGLimits",
]


def _validate_budget(name: str, value: int | None) -> None:
    """Accept a non-negative integer or ``None``; reject everything else."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"RELAX NG budget {name} must be a non-negative integer or None")


@dataclass(frozen=True)
class RelaxNGLimits:
    """Optional budgets for pattern-graph, evidence, capture, and diagnostics.

    A pattern-node budget counts distinct semantic graph nodes. Derivative,
    memo-entry, evidence-state, evidence-edge, scalar-witness, binding-summary,
    capture-entry, and projection-route budgets bound their named work.
    ``max_diagnostics`` is a truncatable retention budget, not a work budget.
    The compiler's graph and semantic correctness checks run regardless
    of these values; a projection budget never skips a correctness check.
    """

    max_pattern_nodes: int | None = None
    max_derivative_states: int | None = None
    max_memo_entries: int | None = None
    max_evidence_states: int | None = None
    max_evidence_edges: int | None = None
    max_scalar_witnesses: int | None = None
    max_binding_summaries: int | None = None
    max_capture_entries: int | None = None
    max_projection_routes: int | None = None
    max_diagnostics: int | None = None

    def __post_init__(self) -> None:
        for field in fields(self):
            _validate_budget(field.name, getattr(self, field.name))


_LIMIT_NAMES = frozenset(field.name for field in fields(RelaxNGLimits))


class BudgetCounter:
    """One stage's running observation of the configured budgets.

    A counter is per compilation or per parse; it counts each budget name
    independently and never mutates its :class:`RelaxNGLimits` value.
    """

    def __init__(self, stage: str, limits: RelaxNGLimits | None = None) -> None:
        self._stage = stage
        self._limits = limits
        self._observed: dict[str, int] = {}

    def consume(self, budget: str, amount: int = 1) -> None:
        """Record *amount* units of *budget*, raising when the bound is crossed."""
        configured = self._configured(budget)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("amount must be a non-negative integer")
        observed = self._observed.get(budget, 0) + amount
        self._observed[budget] = observed
        if configured is not None and observed > configured:
            raise RelaxNGLimitError(self._stage, budget, configured, observed)

    def _configured(self, budget: str) -> int | None:
        if budget not in _LIMIT_NAMES:
            raise ValueError(f"unknown RELAX NG budget {budget!r}")
        if self._limits is None:
            return None
        return getattr(self._limits, budget)
