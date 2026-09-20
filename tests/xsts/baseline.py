"""Checked-in baseline of per-test outcomes.

The baseline is how the suite becomes a gate without demanding that pyxsd be
correct everywhere yet.  A run compares every test's outcome to the recorded
one; *any* change is surfaced — a passing test that starts failing is a
regression, and a failing test that starts passing is a baseline that needs
regenerating.  The report keeps the known-failure census separate from those
changes.

The file is TOML so it is diffable and reviewable::

    [meta]
    profile = "xsd11"

    [results]
    "pyxsd:set/group/name:schema" = "pass"
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .outcomes import Outcome

#: Baseline keys look like ``engine:set/group/test:kind``.
Key = str


@dataclass(frozen=True)
class Baseline:
    """A recorded outcome per test key, plus provenance metadata."""

    meta: dict[str, object] = field(default_factory=dict)
    results: dict[Key, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Change:
    """One test key whose outcome differs from the baseline."""

    key: Key
    previous: str | None
    current: str | None

    @property
    def kind(self) -> str:
        if self.previous is None:
            return "new"
        if self.current is None:
            return "removed"
        if self.previous == Outcome.PASS.value and self.current != Outcome.PASS.value:
            return "regression"
        if self.previous != Outcome.PASS.value and self.current == Outcome.PASS.value:
            return "improvement"
        return "changed"


@dataclass(frozen=True)
class BaselineDiff:
    """Every difference between a baseline and a fresh run."""

    changes: tuple[Change, ...]

    @property
    def regressions(self) -> tuple[Change, ...]:
        return tuple(change for change in self.changes if change.kind == "regression")

    @property
    def improvements(self) -> tuple[Change, ...]:
        return tuple(change for change in self.changes if change.kind == "improvement")

    def __bool__(self) -> bool:
        return bool(self.changes)


def load(path: Path) -> Baseline:
    """Read a baseline file. A missing file yields an empty baseline."""
    if not path.is_file():
        return Baseline()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    meta = data.get("meta", {})
    results = data.get("results", {})
    if not isinstance(meta, dict) or not isinstance(results, dict):
        raise ValueError(f"{path}: malformed baseline")
    return Baseline(meta=dict(meta), results={str(k): str(v) for k, v in results.items()})


def dump(results: dict[Key, str], meta: dict[str, object] | None = None) -> str:
    """Render a baseline as TOML text."""
    lines: list[str] = ["[meta]"]
    for key, value in (meta or {}).items():
        lines.append(f"{key} = {json.dumps(value)}")
    lines.append("")
    lines.append("[results]")
    for key in sorted(results):
        lines.append(f"{json.dumps(key)} = {json.dumps(results[key])}")
    lines.append("")
    return "\n".join(lines)


def save(path: Path, results: dict[Key, str], meta: dict[str, object] | None = None) -> None:
    """Write a baseline to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(results, meta), encoding="utf-8")


def diff(baseline: Baseline, current: dict[Key, str]) -> BaselineDiff:
    """Every key whose outcome changed, appeared, or disappeared."""
    changes: list[Change] = []
    for key in sorted(set(baseline.results) | set(current)):
        previous = baseline.results.get(key)
        now = current.get(key)
        if previous != now:
            changes.append(Change(key=key, previous=previous, current=now))
    return BaselineDiff(changes=tuple(changes))
