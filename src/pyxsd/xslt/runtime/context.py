"""Immutable dynamic evaluation context shared by XSLT execution helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from pyxsd.xpath.environment import Focus
from pyxsd.xslt.compiler.plans import _ComponentPlan, _RuleContext

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xdm import XdmSequence
    from pyxsd.xslt.compiler.packages import Component

__all__ = ["EvaluationContext"]


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """One immutable snapshot of the dynamic state an execution step needs.

    A frame owns its context and every state change produces a replacement
    with :func:`dataclasses.replace`; execution helpers never mutate one.
    ``locals`` and ``tunnel_parameters`` are copied into read-only views so a
    caller cannot mutate a context after construction.
    """

    focus: Focus = field(default_factory=Focus.absent)
    locals: Mapping[str, XdmSequence] = field(default_factory=dict)
    final_output: bool = True
    package_name: str | None = None
    mode: str | None = None
    tunnel_parameters: Mapping[str, XdmSequence] = field(default_factory=dict)
    rule_context: _RuleContext | None = None
    original_functions: Mapping[int, Component] = field(default_factory=dict)
    original_components: Mapping[tuple[str, str | None, int | None], Component] = field(
        default_factory=dict
    )
    original_plans: Mapping[tuple[str, str | None, int | None], _ComponentPlan] = field(
        default_factory=dict
    )
    count_output: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.focus, Focus):
            raise TypeError("context focus must be a Focus")
        if not isinstance(self.locals, Mapping):
            raise TypeError("context locals must be a mapping")
        object.__setattr__(self, "locals", MappingProxyType(dict(self.locals)))
        if not isinstance(self.final_output, bool):
            raise TypeError("final_output must be a boolean")
        if self.package_name is not None and not isinstance(self.package_name, str):
            raise TypeError("context package name must be a string or None")
        if self.mode is not None and not isinstance(self.mode, str):
            raise TypeError("context mode must be a string or None")
        if not isinstance(self.tunnel_parameters, Mapping):
            raise TypeError("context tunnel parameters must be a mapping")
        object.__setattr__(
            self,
            "tunnel_parameters",
            MappingProxyType(dict(self.tunnel_parameters)),
        )
        if self.rule_context is not None and not isinstance(self.rule_context, _RuleContext):
            raise TypeError("context rule context must be a _RuleContext or None")
        if not isinstance(self.original_functions, Mapping):
            raise TypeError("context original functions must be a mapping")
        object.__setattr__(
            self,
            "original_functions",
            MappingProxyType(dict(self.original_functions)),
        )
        for label, value in (
            ("original components", self.original_components),
            ("original plans", self.original_plans),
        ):
            if not isinstance(value, Mapping):
                raise TypeError(f"context {label} must be a mapping")
            object.__setattr__(
                self,
                "original_components" if label == "original components" else "original_plans",
                MappingProxyType(dict(value)),
            )
