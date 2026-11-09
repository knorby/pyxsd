"""Deterministic per-profile baselines and explicit baseline changes.

A baseline records one outcome per ``set/case`` plus the corpus commit and
profile identity.  Comparing two baselines names regressions and improvements
separately; it never treats every difference as interchangeable.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.xslt.outcomes import CaseResult

#: The checked-in baseline directory used by the CI contract.
BASELINE_DIR = Path(__file__).parent / "baselines"

#: Baseline document schema version.
BASELINE_SCHEMA = 1

_BARE_KEY = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _toml_key(value: str) -> str:
    if value and all(character in _BARE_KEY for character in value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    raise TypeError(f"unsupported TOML value: {value!r}")


@dataclass(frozen=True)
class Baseline:
    """One profile's recorded outcomes."""

    meta: Mapping[str, Any]
    cases: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", dict(self.meta))
        object.__setattr__(
            self,
            "cases",
            {str(name): str(outcome) for name, outcome in sorted(self.cases.items())},
        )

    def as_mapping(self) -> dict[str, str]:
        """The case outcomes as a plain dictionary."""
        return dict(self.cases)

    def outcome(self, name: str) -> str | None:
        """One case's recorded outcome, or ``None`` when it is absent."""
        return self.cases.get(name)


@dataclass(frozen=True)
class BaselineChange:
    """One difference between two baselines."""

    kind: str
    key: str
    previous: str | None
    current: str | None


def _case_map(value: Baseline | Mapping[str, str]) -> dict[str, str]:
    if isinstance(value, Baseline):
        return dict(value.cases)
    return {str(name): str(outcome) for name, outcome in value.items()}


def diff_baseline(
    previous: Baseline | Mapping[str, str],
    current: Baseline | Mapping[str, str],
) -> tuple[BaselineChange, ...]:
    """Name every change, distinguishing regressions from improvements."""
    before = _case_map(previous)
    after = _case_map(current)
    changes: list[BaselineChange] = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            changes.append(BaselineChange("added", key, None, new))
        elif new is None:
            changes.append(BaselineChange("removed", key, old, None))
        elif old == new:
            continue
        elif old == "pass" and new != "pass":
            changes.append(BaselineChange("regression", key, old, new))
        elif old != "pass" and new == "pass":
            changes.append(BaselineChange("improvement", key, old, new))
        else:
            changes.append(BaselineChange("changed", key, old, new))
    return tuple(changes)


def build_baseline(results: Iterable[CaseResult], *, corpus: str, profile: str) -> Baseline:
    """Build a deterministic baseline from explicit case results."""
    cases: dict[str, str] = {}
    for result in results:
        if not result.name:
            raise ValueError("a baseline requires named case results")
        if result.name in cases:
            raise ValueError(f"duplicate case result: {result.name}")
        cases[result.name] = str(result.outcome)
    return Baseline(
        meta={"schema": BASELINE_SCHEMA, "corpus": corpus, "profile": profile},
        cases=cases,
    )


def render_baseline(baseline: Baseline) -> str:
    """Render one baseline as deterministic TOML."""
    lines = ["[meta]"]
    for key in ("schema", "corpus", "profile"):
        if key in baseline.meta:
            lines.append(f"{_toml_key(key)} = {_toml_value(baseline.meta[key])}")
    for key in sorted(set(baseline.meta) - {"schema", "corpus", "profile"}):
        lines.append(f"{_toml_key(key)} = {_toml_value(baseline.meta[key])}")
    lines.append("")
    lines.append("[cases]")
    for name, outcome in sorted(baseline.cases.items()):
        lines.append(f"{_toml_key(name)} = {_toml_value(outcome)}")
    return "\n".join(lines) + "\n"


def write_baseline(path: Path, baseline: Baseline) -> None:
    """Write one baseline under *path*; the caller chooses a safe directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_baseline(baseline).encode("utf-8"))


def load_baseline(source: str | Path, *, directory: Path | None = None) -> Baseline:
    """Load a baseline by path or by profile name.

    A profile name resolves under *directory* (default: the checked-in
    ``tests/xslt/baselines`` directory).  A path is used as given.
    """
    if isinstance(source, Path) or "/" in str(source) or str(source).endswith(".toml"):
        path = Path(source)
    else:
        path = (directory or BASELINE_DIR) / f"{source}.toml"
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    meta = document.get("meta")
    cases = document.get("cases")
    if not isinstance(meta, dict) or not isinstance(cases, dict):
        raise ValueError(f"baseline {path} requires [meta] and [cases] tables")
    if not all(isinstance(value, str) for value in cases.values()):
        raise ValueError(f"baseline {path} case outcomes must be strings")
    return Baseline(meta=meta, cases=cases)
