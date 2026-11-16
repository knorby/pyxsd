"""Immutable schema-side source identities and origin sets.

A :class:`SourceOrigin` names one authored construct in one source. A
:class:`OriginTable` is the immutable set of origins for one semantic object;
interning may merge equal patterns, so origins are unioned beside the semantic
key instead of becoming part of it. Origins describe where a construct was
authored, never which instance occurrence matched it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from pyxsd.locations import IssueLocation

__all__ = [
    "OriginTable",
    "SourceOrigin",
]


@dataclass(frozen=True)
class SourceOrigin:
    """One authored source occurrence, with its exact address when known."""

    source_id: str
    location: IssueLocation | None


@dataclass(frozen=True)
class OriginTable:
    """An immutable, deduplicated set of authored origins.

    The constructor copies and deduplicates any iterable, including another
    table, so later mutation of the input cannot change a table.
    """

    origins: frozenset[SourceOrigin] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "origins", frozenset(self.origins))

    def __iter__(self) -> Iterator[SourceOrigin]:
        return iter(self.origins)

    def __len__(self) -> int:
        return len(self.origins)

    def __contains__(self, origin: object) -> bool:
        return origin in self.origins

    def union(self, *others: OriginTable) -> OriginTable:
        """Return a new table holding every origin in this table and *others*."""
        combined = set(self.origins)
        for other in others:
            combined.update(other.origins)
        return OriginTable(frozenset(combined))
