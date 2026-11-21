"""Dependency-free diagnostic addresses and constraint evidence.

Paths contain expanded names and same-name sibling indices, not XPath strings:
namespace URIs may themselves contain slashes. Addresses describe one parse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

LocationPrecision = Literal["exact", "container", "component", "unknown"]


@dataclass(frozen=True)
class PathStep:
    """One element occurrence; ``index`` starts at one among equal names."""

    name: str
    index: int


@dataclass(frozen=True)
class SourceTextSpan:
    """A half-open character range in original source text.

    Offsets are zero-based original characters; ``end`` points one past the
    last included character. Lines and columns are one-based, and ``end_line``
    and ``end_column`` use the same half-open convention as ``end``.
    """

    start: int
    end: int
    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class IssueLocation:
    """A source occurrence, optionally identifying one expanded attribute name.

    ``span`` carries exact original-text evidence for textual sources; XML
    occurrences that have only a path leave it ``None``.
    """

    source: str | None
    path: tuple[PathStep, ...]
    attribute: str | None = None
    span: SourceTextSpan | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class ConstraintInfo:
    """A failed rule and its expected/actual values, without parsing prose."""

    kind: str
    expected: object
    actual: object | None = None


@dataclass(frozen=True)
class NodeLookup:
    """An occurrence lookup, including source nodes that binding discarded."""

    location: IssueLocation
    bound: Any
    status: Literal["bound", "unbound", "missing", "attribute"]


class ParseProvenance:
    """One parse's addresses and bound objects, without retaining source nodes."""

    def __init__(self, root: Any, source: str | None) -> None:
        self.source = source
        self._sources: dict[int, IssueLocation] = {}
        self._attributes: dict[tuple[PathStep, ...], dict[str, str]] = {}
        self._bound: dict[tuple[PathStep, ...], Any] = {}
        self._bound_locations: dict[int, IssueLocation] = {}
        stack: list[tuple[Any, tuple[PathStep, ...]]] = [(root, (PathStep(root.tag, 1),))]
        while stack:
            node, path = stack.pop()
            self._sources[id(node)] = IssueLocation(source, path)
            self._attributes[path] = dict(node.attrib)
            counts: dict[str, int] = {}
            for child in node:
                counts[child.tag] = counts.get(child.tag, 0) + 1
                stack.append((child, (*path, PathStep(child.tag, counts[child.tag]))))

    def source_location(self, node: Any) -> IssueLocation | None:
        """Return the address for this exact source object, never by value."""
        return self._sources.get(id(node))

    def bound_location(self, node: Any) -> IssueLocation | None:
        """Return a bound object's original occurrence, if it came from this parse."""
        return self._bound_locations.get(id(node))

    def bind(self, source_node: Any, bound: Any) -> None:
        """Record a successful binding (including a raw-value fallback)."""
        location = self.source_location(source_node)
        if location is not None and bound is not None:
            self._bound[location.path] = bound
            self._bound_locations[id(bound)] = location

    def locate(self, location: IssueLocation) -> NodeLookup:
        """Resolve an address only within this source revision's provenance."""
        if location.source != self.source or location.path not in self._attributes:
            return NodeLookup(location, None, "missing")
        if location.attribute is not None:
            attributes = self._attributes[location.path]
            if location.attribute in attributes:
                return NodeLookup(location, attributes[location.attribute], "attribute")
            return NodeLookup(location, None, "missing")
        if location.path in self._bound:
            return NodeLookup(location, self._bound[location.path], "bound")
        return NodeLookup(location, None, "unbound")
