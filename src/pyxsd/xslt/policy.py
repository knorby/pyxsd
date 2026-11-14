"""Application authorization for XSLT output destinations.

Authorization alone never writes a result. Results remain in memory until an
application explicitly requests a separate output operation.

The module also owns the policy state that crosses an ``fn:transform`` nesting
boundary.  Everything else about a nested transformation stays separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pyxsd.xslt.runtime.frames import ExecutionBudget, ExecutionLimits

__all__ = [
    "NestedTransformationPolicy",
    "OutputPolicy",
]


class OutputPolicy(Protocol):
    """Authorize one absolute output URI before its content is constructed."""

    def authorize(self, uri: str, *, principal: bool) -> None:
        """Raise a resource policy error when the destination is not allowed."""
        ...


@dataclass(frozen=True, slots=True)
class NestedTransformationPolicy:
    """The only state a nested ``fn:transform`` call inherits from its caller.

    Exactly the execution state the transformation contract allows to cross
    the nesting boundary: the immutable processor snapshot carrying the
    extension, resource, package, and output policies (a nested call may
    narrow it, never loosen it), the cumulative budget ledger with its limits
    shared across every nesting depth and never reset, and the stable dynamic
    context values.  Runtime stacks, caches, global values, and results never
    cross it.
    """

    processor: Any
    limits: ExecutionLimits
    budget: ExecutionBudget
    current_datetime: datetime | None
    implicit_timezone: Any

    def __post_init__(self) -> None:
        if not isinstance(self.limits, ExecutionLimits):
            raise TypeError("limits must be an ExecutionLimits value")
        if not isinstance(self.budget, ExecutionBudget):
            raise TypeError("budget must be an ExecutionBudget value")

    @classmethod
    def from_transformation(cls, transformation: Any) -> NestedTransformationPolicy:
        """Capture the inheritable state of one running transformation."""
        return cls(
            processor=transformation.stylesheet.processor,
            limits=transformation.limits,
            budget=transformation.budget,
            current_datetime=transformation.current_datetime,
            implicit_timezone=transformation.implicit_timezone,
        )
