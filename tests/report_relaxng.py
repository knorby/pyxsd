#!/usr/bin/env python3
"""Deterministic report for the pinned RELAX NG schema corpus.

The compiler phase owns the 623 correct/incorrect schema assertions from the
pinned Jing/Trang catalogs.  This report runs each required schema case,
compares the settled outcomes with the checked-in baseline, and prints every
unresolved failure and every actually excluded feature dependency.  It never
substitutes a pass rate for per-case evidence.

Examples::

    python tests/report_relaxng.py
    python tests/report_relaxng.py --json
    python tests/report_relaxng.py --write-baseline tests/relaxng/baseline.toml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.relaxng.baseline import (
    Baseline,
    BaselineDiff,
    BaselineError,
    diff,
)
from tests.relaxng.baseline import (
    load as load_baseline,
)
from tests.relaxng.baseline import (
    save as save_baseline,
)
from tests.relaxng.catalog import (
    CatalogError,
    load_xml_syntax_catalog,
    load_xsd_datatype_catalog,
)
from tests.relaxng.dispositions import (
    Disposition,
    DispositionError,
    load_dispositions,
    validate_dispositions,
)
from tests.relaxng.driver import DEFAULT_TIMEOUT, CaseResult, PyXSDDriver
from tests.relaxng.model import (
    EXCLUDED_PROFILES,
    INSTANCE_KINDS,
    SCHEMA_KINDS,
    CorpusCase,
    Outcome,
)

__all__ = [
    "BASELINE",
    "CATALOG_COMMIT",
    "CORPUS",
    "DISPOSITIONS",
    "BaselineUpdateViolation",
    "ExcludedDependency",
    "baseline_update_violations",
    "case_dispositions",
    "deferred_instance_cases",
    "excluded_dependencies",
    "load_cases",
    "main",
    "render_json",
    "render_report",
    "required_schema_cases",
    "results_to_keys",
    "run_cases",
    "save_baseline_checked",
    "schema_cases",
    "unresolved_failures",
]

#: The upstream commit pinned by ``tests/relaxng/corpus/PROVENANCE.md``.
CATALOG_COMMIT = "263a35a5e3ebbfccf8eb9752077e59cde9acc9a1"

_ROOT = Path(__file__).resolve().parent
CORPUS = _ROOT / "relaxng" / "corpus"
BASELINE = _ROOT / "relaxng" / "baseline.toml"
DISPOSITIONS = _ROOT / "relaxng" / "dispositions.toml"

_PROFILE_LABEL = "core+xsd-datatype"


@dataclass(frozen=True)
class ExcludedDependency:
    """One disposition group that names an actual excluded feature."""

    entry_id: str
    title: str
    feature: str
    cases: tuple[str, ...]
    rationale: str
    links: tuple[str, ...]


@dataclass(frozen=True)
class BaselineUpdateViolation:
    """One previously passing case a proposed baseline would remove."""

    case_id: str
    previous: str
    current: str | None


def load_cases(root: Path = CORPUS) -> tuple[CorpusCase, ...]:
    """Read both pinned catalogs, in their stable catalog order."""
    return load_xml_syntax_catalog(root) + load_xsd_datatype_catalog(root)


def schema_cases(cases: Iterable[CorpusCase]) -> tuple[CorpusCase, ...]:
    """Every correct/incorrect schema assertion in *cases*."""
    return tuple(case for case in cases if case.kind in SCHEMA_KINDS)


def required_schema_cases(cases: Iterable[CorpusCase]) -> tuple[CorpusCase, ...]:
    """The schema assertions the declared profile makes release-required."""
    return tuple(case for case in schema_cases(cases) if case.is_required)


def deferred_instance_cases(cases: Iterable[CorpusCase]) -> tuple[CorpusCase, ...]:
    """Instance assertions owned by the later validation phase."""
    return tuple(case for case in cases if case.kind in INSTANCE_KINDS)


def run_cases(
    cases: Iterable[CorpusCase],
    *,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> tuple[CaseResult, ...]:
    """Run each case through the native compiler driver."""
    driver = PyXSDDriver(timeout=timeout)
    return tuple(driver.run(case) for case in cases)


def results_to_keys(results: Iterable[CaseResult]) -> dict[str, str]:
    """The baseline representation of one run."""
    return {result.case.id: result.outcome.value for result in results}


def case_dispositions(
    cases: Iterable[CorpusCase],
    dispositions: Sequence[Disposition],
) -> dict[str, str]:
    """Exactly one effective disposition category for every case.

    Explicit entries may document a required case or exclude a case whose
    profile is genuinely outside the release; :func:`validate_dispositions`
    has already rejected every other shape.
    """
    case_map = {case.id: case for case in cases}
    validate_dispositions(tuple(dispositions), case_map)
    claimed = {case_id: entry.category for entry in dispositions for case_id in entry.cases}
    return {case.id: claimed.get(case.id, case.profile) for case in case_map.values()}


def excluded_dependencies(
    cases: Iterable[CorpusCase],
    dispositions: Sequence[Disposition],
) -> tuple[ExcludedDependency, ...]:
    """Every actual excluded-feature dependency, in stable entry order."""
    case_map = {case.id: case for case in cases}
    validate_dispositions(tuple(dispositions), case_map)
    entries = [
        ExcludedDependency(
            entry_id=entry.id,
            title=entry.title,
            feature=entry.feature or entry.category,
            cases=entry.cases,
            rationale=entry.rationale,
            links=entry.links,
        )
        for entry in dispositions
        if entry.category in EXCLUDED_PROFILES
    ]
    return tuple(sorted(entries, key=lambda entry: entry.entry_id))


def unresolved_failures(results: Iterable[CaseResult]) -> tuple[CaseResult, ...]:
    """In-scope results that did not reach the catalog verdict."""
    failures = [
        result for result in results if result.case.is_required and not result.satisfies_expectation
    ]
    return tuple(sorted(failures, key=lambda result: result.case.id))


def baseline_update_violations(
    previous: Baseline,
    proposed: Mapping[str, str],
) -> tuple[BaselineUpdateViolation, ...]:
    """Proposed entries that would remove or downgrade an existing pass."""
    violations: list[BaselineUpdateViolation] = []
    for case_id, outcome in sorted(previous.results.items()):
        if outcome != Outcome.PASS.value:
            continue
        current = proposed.get(case_id)
        if current != Outcome.PASS.value:
            violations.append(
                BaselineUpdateViolation(
                    case_id=case_id,
                    previous=outcome,
                    current=current,
                )
            )
    return tuple(violations)


def save_baseline_checked(
    path: Path,
    results: Mapping[str, str],
    meta: Mapping[str, object] | None = None,
) -> None:
    """Write a baseline, refusing to remove or downgrade an existing pass."""
    previous = load_baseline(path)
    violations = baseline_update_violations(previous, results)
    if violations:
        details = ", ".join(
            f"{violation.case_id} ({violation.previous} -> {violation.current or 'missing'})"
            for violation in violations
        )
        raise BaselineError(f"refusing to remove {len(violations)} passing case(s): {details}")
    save_baseline(path, results, meta)


def _baseline_summary(baseline: Baseline, comparison: BaselineDiff) -> list[str]:
    if not comparison:
        return ["baseline: matches"]
    lines = [f"baseline changes ({len(comparison.changes)}):"]
    for change in comparison.changes:
        lines.append(
            f"  [{change.kind}] {change.key}: "
            f"{change.previous or 'missing'} -> {change.current or 'missing'}"
        )
    return lines


def render_report(
    cases: Iterable[CorpusCase],
    results: Iterable[CaseResult],
    baseline: Baseline,
    dispositions: Sequence[Disposition],
    *,
    deferred: int = 0,
) -> str:
    """Render a deterministic, per-case audit of one schema run."""
    all_schema = tuple(schema_cases(cases))
    required = required_schema_cases(all_schema)
    results = tuple(result for result in results if result.case.is_required)
    comparison = diff(baseline, results_to_keys(results))
    failures = unresolved_failures(results)
    excluded = excluded_dependencies(all_schema, dispositions)
    assignments = case_dispositions(all_schema, dispositions)

    profile_counts = Counter(case.profile for case in required)
    outcome_counts = Counter(result.outcome.value for result in results)
    recorded = set(results_to_keys(results))
    missing = [case.id for case in required if case.id not in recorded]

    lines = [
        "RELAX NG schema conformance",
        f"catalog: pinned Jing/Trang {CATALOG_COMMIT}",
        f"scope: {len(required)} required schema assertions"
        f" (core: {profile_counts.get('core', 0)},"
        f" xsd-datatype: {profile_counts.get('xsd-datatype', 0)})",
    ]
    if len(all_schema) != len(required):
        lines.append(f"excluded schema assertions: {len(all_schema) - len(required)}")
    if deferred:
        lines.append(f"deferred instance assertions: {deferred}")
    lines.append(f"dispositions: {len(assignments)} schema cases assigned exactly one")
    lines.append(
        "outcomes: "
        + ", ".join(f"{name} {count}" for name, count in sorted(outcome_counts.items()))
        + (f", missing {len(missing)}" if missing else "")
    )
    if failures:
        lines.append(f"unresolved in-scope failures ({len(failures)}):")
        for failure in failures:
            detail = f" ({failure.failure.value})"
            if failure.detail:
                detail += f": {failure.detail}"
            lines.append(
                f"  {failure.case.id} [{failure.case.kind}] {failure.outcome.value}{detail}"
            )
    else:
        lines.append("unresolved in-scope failures: none")
    if excluded:
        lines.append(f"excluded-feature dependencies ({len(excluded)}):")
        for dependency in excluded:
            lines.append(f"  {dependency.entry_id} [{dependency.feature}] {dependency.title}")
            lines.append(f"    cases: {', '.join(dependency.cases)}")
            lines.append(f"    rationale: {dependency.rationale}")
            if dependency.links:
                lines.append(f"    links: {', '.join(dependency.links)}")
    else:
        lines.append("excluded-feature dependencies: none")
    lines.extend(_baseline_summary(baseline, comparison))
    return "\n".join(lines) + "\n"


def render_json(
    cases: Iterable[CorpusCase],
    results: Iterable[CaseResult],
    baseline: Baseline,
    dispositions: Sequence[Disposition],
    *,
    deferred: int = 0,
) -> str:
    """Render the same evidence as :func:`render_report`, as stable JSON."""
    all_schema = tuple(schema_cases(cases))
    required = required_schema_cases(all_schema)
    results = tuple(result for result in results if result.case.is_required)
    comparison = diff(baseline, results_to_keys(results))
    excluded = excluded_dependencies(all_schema, dispositions)
    profile_counts = Counter(case.profile for case in required)
    payload = {
        "catalog": CATALOG_COMMIT,
        "scope": {
            "required_schema_cases": len(required),
            "core": profile_counts.get("core", 0),
            "xsd_datatype": profile_counts.get("xsd-datatype", 0),
            "excluded_schema_cases": len(all_schema) - len(required),
            "deferred_instance_assertions": deferred,
        },
        "results": {
            result.case.id: {
                "kind": result.case.kind,
                "profile": result.case.profile,
                "outcome": result.outcome.value,
                "failure": result.failure.value,
                "detail": result.detail,
            }
            for result in sorted(results, key=lambda item: item.case.id)
        },
        "unresolved": [
            {
                "case": failure.case.id,
                "kind": failure.case.kind,
                "profile": failure.case.profile,
                "outcome": failure.outcome.value,
                "failure": failure.failure.value,
                "detail": failure.detail,
            }
            for failure in unresolved_failures(results)
        ],
        "excluded": [
            {
                "entry": dependency.entry_id,
                "feature": dependency.feature,
                "title": dependency.title,
                "cases": list(dependency.cases),
                "rationale": dependency.rationale,
                "links": list(dependency.links),
            }
            for dependency in excluded
        ],
        "baseline_changes": [
            {
                "case": change.key,
                "kind": change.kind,
                "previous": change.previous,
                "current": change.current,
            }
            for change in comparison.changes
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _metadata(schema_count: int) -> dict[str, object]:
    return {
        "catalog": CATALOG_COMMIT,
        "profile": _PROFILE_LABEL,
        "schema_cases": schema_count,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=CORPUS)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--dispositions", type=Path, default=DISPOSITIONS)
    parser.add_argument("--write-baseline", type=Path, default=None)
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("PYXSD_RELAXNG_TIMEOUT", "60")),
        help="per-case seconds (default 60; PYXSD_RELAXNG_TIMEOUT overrides)",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the report; return a process exit status."""
    args = _parse_args(argv)
    try:
        cases = load_cases(args.root)
        all_schema = schema_cases(cases)
        required = required_schema_cases(all_schema)
        dispositions = load_dispositions(args.dispositions)
        validate_dispositions(dispositions, {case.id: case for case in all_schema})
    except (CatalogError, DispositionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.list_only:
        print(f"{len(required)} schema cases would run")
        return 0

    results = run_cases(required, timeout=args.timeout)
    outcomes = results_to_keys(results)
    try:
        if args.write_baseline is not None:
            save_baseline_checked(
                args.write_baseline,
                outcomes,
                _metadata(len(required)),
            )
            baseline = load_baseline(args.write_baseline)
        else:
            baseline = load_baseline(args.baseline)
    except BaselineError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    deferred = len(deferred_instance_cases(cases))
    if args.json:
        sys.stdout.write(
            render_json(
                all_schema,
                results,
                baseline,
                dispositions,
                deferred=deferred,
            )
        )
    else:
        sys.stdout.write(
            render_report(
                all_schema,
                results,
                baseline,
                dispositions,
                deferred=deferred,
            )
        )

    comparison = diff(baseline, outcomes)
    failures = unresolved_failures(results)
    if failures or (args.enforce and comparison):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
