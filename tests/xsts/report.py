"""Aggregate a run into counts that make the state of the engine legible.

The report deliberately separates:
- the pass/fail count against the suite's expectations,
- tests the profile did not claim,
- tests with non-Boolean expectations the suite does not decide,
- cases the harness cannot drive,
- outstanding per-test failures grouped by contributor and by kind,
- and the direction of each settled failure (false accept or false reject).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from .baseline import BaselineDiff
from .outcomes import Outcome
from .runner import CaseResult
from .selection import expected_is_valid

#: Outcomes that represent a settled, checkable verdict.
CHECKED = frozenset({Outcome.PASS, Outcome.FAIL})


@dataclass
class EngineSummary:
    """Counts for a single engine."""

    engine: str
    outcomes: Counter[str] = field(default_factory=Counter)
    by_contributor: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    by_kind: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    failures: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.outcomes.values())

    @property
    def checked(self) -> int:
        return sum(self.outcomes[outcome.value] for outcome in CHECKED)

    @property
    def passed(self) -> int:
        return self.outcomes[Outcome.PASS.value]

    @property
    def failed(self) -> int:
        return self.outcomes[Outcome.FAIL.value]

    def failure_directions(self) -> Counter[str]:
        """Count settled failures by false accept and false reject.

        Direction comes only from the corpus expectation and the observed
        verdict, so a disposition cannot reclassify a failure or move it out
        of the raw checked denominator.  A failure whose expectation is not
        Boolean, or whose metadata is missing, is not forced into a direction.
        """
        directions: Counter[str] = Counter()
        for failure in self.failures:
            if failure.expected is None or failure.actual is None:
                continue
            expected_valid = expected_is_valid(failure.expected)
            if expected_valid is None:
                continue
            if failure.actual is True and expected_valid is False:
                directions["false-accept"] += 1
            elif failure.actual is False and expected_valid is True:
                directions["false-reject"] += 1
        return directions


@dataclass
class Summary:
    """Counts for a whole run, per engine."""

    engines: dict[str, EngineSummary] = field(default_factory=dict)
    diff: BaselineDiff | None = None
    results: list[CaseResult] = field(default_factory=list)

    def engines_sorted(self) -> list[EngineSummary]:
        return [self.engines[name] for name in sorted(self.engines)]


def summarize(results: list[CaseResult], diff: BaselineDiff | None = None) -> Summary:
    """Fold per-case results into per-engine counts."""
    summary = Summary(diff=diff, results=list(results))
    for result in results:
        engine = summary.engines.setdefault(result.engine, EngineSummary(result.engine))
        engine.outcomes[result.outcome.value] += 1
        engine.by_contributor[result.contributor][result.outcome.value] += 1
        engine.by_kind[result.kind][result.outcome.value] += 1
        if result.outcome is Outcome.FAIL:
            engine.failures.append(result)
    return summary


def render_text(summary: Summary, *, max_failures: int = 20) -> str:
    """A human-readable report."""
    lines: list[str] = []
    for engine in summary.engines_sorted():
        lines.append(f"== {engine.engine} ==")
        lines.append(f"  cases: {engine.total}")
        for outcome in Outcome:
            count = engine.outcomes[outcome.value]
            if count:
                lines.append(f"  {outcome.value:<16} {count}")
        if engine.checked:
            rate = 100.0 * engine.passed / engine.checked
            lines.append(f"  pass rate (checked): {rate:.2f}% ({engine.passed}/{engine.checked})")
        lines.append("  by contributor:")
        for contributor in sorted(engine.by_contributor):
            counts = engine.by_contributor[contributor]
            checked = sum(counts[outcome.value] for outcome in CHECKED)
            passed = counts[Outcome.PASS.value]
            rate = f"{100.0 * passed / checked:.1f}%" if checked else "n/a"
            lines.append(f"    {contributor or '(none)':<12} {rate:>7}  passed={passed}")
        if engine.failures:
            lines.append(f"  first {min(max_failures, len(engine.failures))} failures:")
            for failure in engine.failures[:max_failures]:
                detail = f" - {failure.detail}" if failure.detail else ""
                lines.append(
                    f"    {failure.test_id} [{failure.kind}] expected={failure.expected}"
                    f" actual={failure.actual}{detail}"
                )
        lines.append("")
    if summary.diff is not None and summary.diff.changes:
        lines.append(f"== baseline changes: {len(summary.diff.changes)} ==")
        for change in summary.diff.changes[:max_failures]:
            lines.append(f"  [{change.kind}] {change.key}: {change.previous} -> {change.current}")
    return "\n".join(lines)


def _ordered_counts(counts: Counter[str]) -> dict[str, int]:
    """Observed counts in outcome-taxonomy order, for deterministic output."""
    return {outcome.value: counts[outcome.value] for outcome in Outcome if counts[outcome.value]}


def render_json(summary: Summary, *, provenance: Mapping[str, object] | None = None) -> str:
    """A machine-readable report.

    Every engine reports its checked denominator, kind split, failure
    directions, and full result map in the same key format as a baseline.
    ``provenance`` is published verbatim when supplied and omitted otherwise,
    so existing consumers keep their shape.
    """
    if provenance is not None and not isinstance(provenance, Mapping):
        raise TypeError("provenance must be a mapping")
    payload: dict[str, object] = {"engines": {}}
    engines: dict[str, object] = {}
    for engine in summary.engines_sorted():
        engine_results = results_to_keys(
            [result for result in summary.results if result.engine == engine.engine]
        )
        engines[engine.engine] = {
            "total": engine.total,
            "checked": engine.checked,
            "outcomes": _ordered_counts(engine.outcomes),
            "by_kind": {
                kind: _ordered_counts(engine.by_kind[kind]) for kind in sorted(engine.by_kind)
            },
            "by_contributor": {
                name: _ordered_counts(counts)
                for name, counts in sorted(engine.by_contributor.items())
            },
            "failure_directions": dict(sorted(engine.failure_directions().items())),
            "results": dict(sorted(engine_results.items())),
            "failures": [
                {
                    "test_id": failure.test_id,
                    "kind": failure.kind,
                    "expected": failure.expected,
                    "actual": failure.actual,
                    "detail": failure.detail,
                }
                for failure in engine.failures
            ],
        }
    payload["engines"] = engines
    if provenance is not None:
        payload["provenance"] = dict(provenance)
    if summary.diff is not None:
        payload["baseline_changes"] = [
            {
                "key": change.key,
                "previous": change.previous,
                "current": change.current,
                "kind": change.kind,
            }
            for change in summary.diff.changes
        ]
    return json.dumps(payload, indent=2)


def results_to_keys(results: list[CaseResult]) -> dict[str, str]:
    """The baseline representation of a run."""
    return {result.key: result.outcome.value for result in results}
