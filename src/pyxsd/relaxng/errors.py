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
from pyxsd.locations import IssueLocation

__all__ = [
    "AmbiguousValueError",
    "RelaxNGCompactSyntaxError",
    "RelaxNGLimitError",
    "StaleInterpretationError",
]


class RelaxNGCompactSyntaxError(PyXSDError):
    """A compact-syntax source could not be read into a schema.

    Lexical, escape, tokenization, and unrecoverable grammar failures raise
    this exception. ``location`` is the exact source occurrence when known,
    ``found`` describes the offending token, and ``expected`` lists the tokens
    that would have been accepted. The string form starts with
    ``source:line:column`` when span evidence is available.
    """

    def __init__(
        self,
        message: str,
        *,
        location: IssueLocation | None = None,
        found: str | None = None,
        expected: tuple[str, ...] = (),
    ) -> None:
        self._location = location
        self._found = found
        self._expected = tuple(sorted(expected))
        super().__init__(self._render(message))

    @property
    def location(self) -> IssueLocation | None:
        """The source occurrence where the failure was detected."""
        return self._location

    @property
    def found(self) -> str | None:
        """A description of the token that was found, when known."""
        return self._found

    @property
    def expected(self) -> tuple[str, ...]:
        """Descriptions of acceptable tokens, sorted deterministically."""
        return self._expected

    def _render(self, message: str) -> str:
        details = [message]
        if self._found is not None:
            details.append(f"found {self._found}")
        if self._expected:
            details.append(f"expected {', '.join(self._expected)}")
        description = "; ".join(details)
        location = self._location
        if location is None:
            return description
        span = location.span
        if span is None:
            return description
        if location.source is None:
            return f"{span.line}:{span.column}: {description}"
        return f"{location.source}:{span.line}:{span.column}: {description}"


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
