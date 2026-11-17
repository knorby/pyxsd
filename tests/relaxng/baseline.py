"""Checked-in baseline of per-case outcomes for the RELAX NG corpus.

The baseline is diffable TOML::

    [meta]
    profile = "core"

    [results]
    "spectest-0001" = "pass"

    [instances]
    "spectest-0001/valid-1" = "pass"

``[results]`` records schema assertions and ``[instances]`` records instance
assertions.  Results are always written in sorted key order, and the loader
rejects duplicate keys, unknown sections, malformed values, and non-scalar
metadata.  A missing baseline file yields an empty baseline, matching the XSTS
harness convention without importing its XSD-specific runner.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .model import Outcome

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_OUTCOME_VALUES = frozenset(outcome.value for outcome in Outcome)
_META_VALUE_TYPES = (str, int, float, bool)


class BaselineError(Exception):
    """Raised when a baseline file cannot be read or would not round-trip."""


@dataclass(frozen=True)
class Baseline:
    """Recorded outcomes plus provenance metadata.

    ``results`` holds schema assertions; ``instances`` holds instance
    assertions.  Keeping them separate preserves the Phase 1 schema gate's
    exact key set while extending the same baseline discipline to Phase 2.
    """

    meta: Mapping[str, object] = field(default_factory=dict)
    results: Mapping[str, str] = field(default_factory=dict)
    instances: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_meta(self.meta)
        _validate_results(self.results)
        _validate_results(self.instances)


@dataclass(frozen=True)
class Change:
    """One case whose recorded outcome changed between runs."""

    key: str
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


def _validate_meta(meta: Mapping[str, object]) -> None:
    for key, value in meta.items():
        if not isinstance(key, str) or not _BARE_KEY.fullmatch(key):
            raise BaselineError(f"metadata key {key!r} is not a TOML bare key")
        if isinstance(value, (list, dict)) or value is None:
            raise BaselineError(f"metadata {key!r} must be a scalar value")


def _validate_results(results: Mapping[str, str]) -> None:
    for key, value in results.items():
        if not isinstance(key, str) or not key.strip():
            raise BaselineError("case ids must be non-blank strings")
        if value not in _OUTCOME_VALUES:
            raise BaselineError(f"unknown outcome {value!r} for case {key!r}")


def load(path: Path) -> Baseline:
    """Read a baseline file.  A missing file yields an empty baseline."""
    if not path.is_file():
        return Baseline()
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except OSError as exc:
        raise BaselineError(f"{path}: cannot read baseline: {exc}") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise BaselineError(f"{path}: malformed baseline: {exc}") from exc

    unknown = set(data) - {"meta", "results", "instances"}
    if unknown:
        raise BaselineError(f"{path}: unknown sections {sorted(unknown)}")
    meta = data.get("meta", {})
    results = data.get("results", {})
    instances = data.get("instances", {})
    if (
        not isinstance(meta, dict)
        or not isinstance(results, dict)
        or not isinstance(instances, dict)
    ):
        raise BaselineError(f"{path}: meta, results, and instances must be tables")
    try:
        return Baseline(
            meta=dict(meta),
            results={str(k): str(v) for k, v in results.items()},
            instances={str(k): str(v) for k, v in instances.items()},
        )
    except BaselineError as exc:
        raise BaselineError(f"{path}: {exc}") from exc


def dump(
    results: Mapping[str, str],
    meta: Mapping[str, object] | None = None,
    *,
    instances: Mapping[str, str] | None = None,
) -> str:
    """Render a baseline as deterministic TOML."""
    metadata = dict(meta or {})
    instance_results = dict(instances or {})
    try:
        _validate_meta(metadata)
        _validate_results(results)
        _validate_results(instance_results)
    except BaselineError as exc:
        raise BaselineError(f"cannot write baseline: {exc}") from exc
    lines: list[str] = ["[meta]"]
    for key in sorted(metadata):
        lines.append(f"{key} = {json.dumps(metadata[key])}")
    lines.append("")
    lines.append("[results]")
    for key in sorted(results):
        lines.append(f"{json.dumps(key)} = {json.dumps(results[key])}")
    if instance_results:
        lines.append("")
        lines.append("[instances]")
        for key in sorted(instance_results):
            lines.append(f"{json.dumps(key)} = {json.dumps(instance_results[key])}")
    lines.append("")
    return "\n".join(lines)


def save(
    path: Path,
    results: Mapping[str, str],
    meta: Mapping[str, object] | None = None,
    *,
    instances: Mapping[str, str] | None = None,
) -> None:
    """Write a baseline to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(results, meta, instances=instances), encoding="utf-8")


def _diff(previous: Mapping[str, str], current: Mapping[str, str]) -> BaselineDiff:
    changes: list[Change] = []
    for key in sorted(set(previous) | set(current)):
        before = previous.get(key)
        now = current.get(key)
        if before != now:
            changes.append(Change(key=key, previous=before, current=now))
    return BaselineDiff(changes=tuple(changes))


def diff(baseline: Baseline, current: Mapping[str, str]) -> BaselineDiff:
    """Every schema-case key whose outcome changed, appeared, or disappeared."""
    return _diff(baseline.results, current)


def diff_instances(baseline: Baseline, current: Mapping[str, str]) -> BaselineDiff:
    """Every instance-case key whose outcome changed, appeared, or disappeared."""
    return _diff(baseline.instances, current)
