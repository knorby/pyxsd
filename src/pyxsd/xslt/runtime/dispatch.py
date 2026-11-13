"""Per-artifact dispatch of compiled plans to their family executors.

The invocation coordinator handles every built-in plan arm inline; plans
registered on the compiled artifact are dispatched by type through
:func:`execute_registered_plan` as a generic lookup after those built-in arms.
The executor mapping is an immutable snapshot stored on each compiled
stylesheet, so a family registers its execution once per artifact and nothing
in this module is process-global state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, TypeAlias

from pyxsd.xdm import XdmSequence
from pyxsd.xslt.compiler.plans import _Plan, _PythonInvokePlan
from pyxsd.xslt.python_rules import execute_python_plan
from pyxsd.xslt.runtime.context import EvaluationContext

__all__ = [
    "PYTHON_PLAN_EXECUTORS",
    "PlanExecutor",
    "execute_registered_plan",
]

#: One registered plan executor.  The invocation is the processor's private
#: coordinator and the context is the immutable evaluation context, mirroring
#: the built-in executor protocol.
PlanExecutor: TypeAlias = Callable[[Any, Any, EvaluationContext], XdmSequence]


def execute_registered_plan(
    plan: _Plan,
    invocation: Any,
    context: EvaluationContext,
    executors: Mapping[type[_Plan], PlanExecutor],
) -> XdmSequence:
    """Execute *plan* through the executor registered on the compiled artifact."""
    executor = executors.get(type(plan))
    if executor is None:
        raise TypeError(f"unknown compiled plan {type(plan).__name__}")
    return executor(plan, invocation, context)


#: The Python customization family's per-artifact executor registration.
PYTHON_PLAN_EXECUTORS: Mapping[type[_Plan], PlanExecutor] = MappingProxyType(
    {_PythonInvokePlan: execute_python_plan}
)
