"""RELAX NG exception contracts.

Compilation and validation report semantic findings through their report
objects. These exceptions mark interruptions that a report cannot express:
an exhausted budget, a value contract that keeps several alternatives, and
an interpretation whose schema revision is no longer current.

Every exception derives from :class:`pyxsd.exceptions.PyXSDError`, so callers
may catch either the specific contract or the common family.
"""

from __future__ import annotations

from pyxsd.exceptions import PyXSDError

__all__ = [
    "AmbiguousValueError",
    "RelaxNGLimitError",
    "StaleInterpretationError",
]


class RelaxNGLimitError(PyXSDError):
    """A configured RELAX NG budget was exhausted.

    Budget exhaustion is not semantic invalidity: a schema or instance that
    exceeds a caller's budget has no verdict. The attributes name the work
    that stopped and the observation that crossed the configured value.
    """

    def __init__(self, stage: str, budget: str, configured: int, observed: int) -> None:
        super().__init__(
            f"RELAX NG {stage} budget {budget} exhausted: "
            f"configured {configured}, observed {observed}"
        )
        self._stage = stage
        self._budget = budget
        self._configured = configured
        self._observed = observed

    @property
    def stage(self) -> str:
        """The compiler or validator stage that exhausted the budget."""
        return self._stage

    @property
    def budget(self) -> str:
        """The :class:`~pyxsd.relaxng.limits.RelaxNGLimits` field name."""
        return self._budget

    @property
    def configured(self) -> int:
        """The caller's configured value for this budget."""
        return self._configured

    @property
    def observed(self) -> int:
        """The observed count that exceeded the configured value."""
        return self._observed

    def __reduce__(self) -> tuple[type[RelaxNGLimitError], tuple[str, str, int, int]]:
        # BaseException pickles with the message alone, which cannot rebuild
        # the typed attributes through this constructor.
        return type(self), (self._stage, self._budget, self._configured, self._observed)


class AmbiguousValueError(PyXSDError):
    """No single scalar interpretation is authoritative for a position.

    Raised by later value interfaces when several schema-defined contracts
    survive. Callers inspect the alternatives instead of guessing a type.
    """


class StaleInterpretationError(PyXSDError):
    """A retained interpretation belongs to an older schema revision.

    Raised by later value interfaces when freshness is required and a
    document or annotation was compiled against a schema that has changed.
    """
