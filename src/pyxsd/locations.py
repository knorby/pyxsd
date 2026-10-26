"""Dependency-free diagnostic addresses and constraint evidence.

Paths contain expanded names and same-name sibling indices, not XPath strings:
namespace URIs may themselves contain slashes. Addresses describe one parse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LocationPrecision = Literal["exact", "container", "component", "unknown"]


@dataclass(frozen=True)
class PathStep:
    """One element occurrence; ``index`` starts at one among equal names."""

    name: str
    index: int


@dataclass(frozen=True)
class IssueLocation:
    """A source occurrence, optionally identifying one expanded attribute name."""

    source: str | None
    path: tuple[PathStep, ...]
    attribute: str | None = None


@dataclass(frozen=True)
class ConstraintInfo:
    """A failed rule and its expected/actual values, without parsing prose."""

    kind: str
    expected: object
    actual: object | None = None
