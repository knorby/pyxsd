"""Dependency-free specifications for named contextual scalar columns."""

from dataclasses import dataclass
from typing import Literal

from . import IntegrationError

__all__ = ["FieldSource"]


@dataclass(frozen=True, kw_only=True)
class FieldSource:
    """Select a scalar or attribute by expanded XML names, relative to a row."""

    scope: Literal["record", "ancestor", "root"] = "record"
    path: tuple[str, ...] = ()
    attribute: str | None = None
    levels: int = 0

    def __post_init__(self) -> None:
        if self.scope not in ("record", "ancestor", "root"):
            raise IntegrationError("source scope must be record, ancestor, or root")
        if type(self.levels) is not int or (
            self.levels <= 0 if self.scope == "ancestor" else self.levels != 0
        ):
            raise IntegrationError(
                "ancestor levels must be positive; other scopes require levels=0"
            )
        if not isinstance(self.path, tuple) or not all(
            isinstance(name, str) and name and name != "$" and not name.startswith("@")
            for name in self.path
        ):
            raise IntegrationError("source path must be a tuple of nonempty expanded element names")
        if self.attribute is not None and (
            not isinstance(self.attribute, str)
            or not self.attribute
            or self.attribute.startswith("@")
        ):
            raise IntegrationError("source attribute must be a nonempty expanded name without @")
