#!/usr/bin/env python3
"""Deterministic report for the pinned RELAX NG conformance corpus.

The schema phase owns the 623 correct/incorrect schema assertions and the
instance phase owns the 1,621 valid/invalid instance assertions from the
pinned Jing/Trang catalogs.  This report runs each required case in both
phases, compares the settled outcomes with the checked-in baseline, and prints
every unresolved failure and every actually excluded feature dependency.  It
never substitutes a pass rate for per-case evidence.

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
    diff_instances,
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
    "instance_cases",
    "instance_update_violations",
    "load_cases",
    "main",
    "render_json",
    "render_report",
    "required_instance_cases",
    "required_schema_cases",
    "results_to_keys",
    "run_cases",
    "save_baseline_checked",
    "schema_cases",
    "timeout_from_environment",
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


def instance_cases(cases: Iterable[CorpusCase]) -> tuple[CorpusCase, ...]:
    """Every valid/invalid instance assertion in *cases*."""
    return tuple(case for case in cases if case.kind in INSTANCE_KINDS)


def required_instance_cases(cases: Iterable[CorpusCase]) -> tuple[CorpusCase, ...]:
    """The instance assertions the declared profile makes release-required."""
    return tuple(case for case in instance_cases(cases) if case.is_required)


def deferred_instance_cases(cases: Iterable[CorpusCase]) -> tuple[CorpusCase, ...]:
    """Instance assertions, kept under the Phase 1 report's historical name.

    The schema-phase report used to defer these to the validation phase; the
    full Phase 2 report now executes them.  The alias preserves that report's
    internal call site and its deferred-count wording.
    """
    return instance_cases(cases)


def timeout_from_environment() -> float:
    """Read the corpus timeout, rejecting a budget that disables the alarm.

    ``setitimer(0)`` cancels the per-case alarm instead of enforcing a zero
    second limit, so a non-positive value must fail rather than silently make
    the corpus gate unbounded.
    """
    timeout = float(os.environ.get("PYXSD_RELAXNG_TIMEOUT", "60"))
    if timeout <= 0:
        raise ValueError("PYXSD_RELAXNG_TIMEOUT must be positive")
    return timeout


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


def _update_violations(
    previous: Mapping[str, str],
    proposed: Mapping[str, str],
) -> tuple[BaselineUpdateViolation, ...]:
    violations: list[BaselineUpdateViolation] = []
    for case_id, outcome in sorted(previous.items()):
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


def baseline_update_violations(
    previous: Baseline,
    proposed: Mapping[str, str],
) -> tuple[BaselineUpdateViolation, ...]:
    """Proposed schema entries that would remove or downgrade an existing pass."""
    return _update_violations(previous.results, proposed)


def instance_update_violations(
    previous: Baseline,
    proposed: Mapping[str, str],
) -> tuple[BaselineUpdateViolation, ...]:
    """Proposed instance entries that would remove or downgrade an existing pass."""
    return _update_violations(previous.instances, proposed)


def save_baseline_checked(
    path: Path,
    results: Mapping[str, str],
    meta: Mapping[str, object] | None = None,
    *,
    instances: Mapping[str, str] | None = None,
) -> None:
    """Write a baseline, refusing to remove or downgrade an existing pass."""
    previous = load_baseline(path)
    violations = baseline_update_violations(previous, results)
    violations += instance_update_violations(previous, instances or {})
    if violations:
        details = ", ".join(
            f"{violation.case_id} ({violation.previous} -> {violation.current or 'missing'})"
            for violation in violations
        )
        raise BaselineError(f"refusing to remove {len(violations)} passing case(s): {details}")
    save_baseline(path, results, meta, instances=instances)


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


def _instance_baseline_summary(
    baseline: Baseline,
    comparison: BaselineDiff,
) -> list[str]:
    if not comparison:
        return ["instance baseline: matches"]
    lines = [f"instance baseline changes ({len(comparison.changes)}):"]
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
    instance_results: Iterable[CaseResult] | None = None,
    instance_baseline: Baseline | None = None,
) -> str:
    """Render a deterministic, per-case audit of one run.

    Without ``instance_results`` this is the Phase 1 schema-phase report and
    keeps its exact wording, including the deferred-instance count.  Supplying
    instance results adds the Phase 2 instance section, per-case failures, and
    its own baseline comparison; every result remains individually auditable.
    """
    all_cases = tuple(cases)
    all_schema = tuple(schema_cases(all_cases))
    required = required_schema_cases(all_schema)
    schema_results = tuple(
        result for result in results if result.case.kind in SCHEMA_KINDS and result.case.is_required
    )
    comparison = diff(baseline, results_to_keys(schema_results))
    failures = unresolved_failures(schema_results)
    full = instance_results is not None
    instance_result_values = (
        tuple(result for result in instance_results if result.case.is_required)
        if instance_results is not None
        else ()
    )
    if full:
        all_instances = instance_cases(all_cases)
        required_instances = required_instance_cases(all_instances)
        instance_comparison = (
            diff_instances(instance_baseline, results_to_keys(instance_result_values))
            if instance_baseline is not None
            else BaselineDiff(())
        )
        instance_failures = unresolved_failures(instance_result_values)
        excluded = excluded_dependencies(all_cases, dispositions)
        all_assignments = case_dispositions(all_cases, dispositions)
        schema_ids = {case.id for case in all_schema}
        instance_ids = {case.id for case in all_instances}
        schema_assignment_count = sum(case_id in schema_ids for case_id in all_assignments)
        instance_assignment_count = sum(case_id in instance_ids for case_id in all_assignments)
    else:
        all_instances = ()
        required_instances = ()
        instance_comparison = BaselineDiff(())
        instance_failures = ()
        excluded = excluded_dependencies(all_schema, dispositions)
        assignments = case_dispositions(all_schema, dispositions)
        schema_assignment_count = len(assignments)
        instance_assignment_count = 0

    profile_counts = Counter(case.profile for case in required)
    outcome_counts = Counter(result.outcome.value for result in schema_results)
    recorded = set(results_to_keys(schema_results))
    missing = [case.id for case in required if case.id not in recorded]

    lines = [
        "RELAX NG schema conformance",
        f"catalog: pinned Jing/Trang {CATALOG_COMMIT}",
        f"scope: {len(required)} required schema assertions"
        f" (core: {profile_counts.get('core', 0)},"
        f" xsd-datatype: {profile_counts.get('xsd-datatype', 0)})",
    ]
    if full:
        instance_profile_counts = Counter(case.profile for case in required_instances)
        lines.append(
            f"instance scope: {len(required_instances)} required instance assertions"
            f" (core: {instance_profile_counts.get('core', 0)},"
            f" xsd-datatype: {instance_profile_counts.get('xsd-datatype', 0)})"
        )
        if len(all_instances) != len(required_instances):
            lines.append(
                f"excluded instance assertions: {len(all_instances) - len(required_instances)}"
            )
    if len(all_schema) != len(required):
        lines.append(f"excluded schema assertions: {len(all_schema) - len(required)}")
    if deferred and not full:
        lines.append(f"deferred instance assertions: {deferred}")
    lines.append(f"dispositions: {schema_assignment_count} schema cases assigned exactly one")
    if full:
        lines.append(
            f"instance dispositions: {instance_assignment_count} instance cases assigned exactly one"
        )
    lines.append(
        "outcomes: "
        + ", ".join(f"{name} {count}" for name, count in sorted(outcome_counts.items()))
        + (f", missing {len(missing)}" if missing else "")
    )
    if full:
        instance_outcome_counts = Counter(result.outcome.value for result in instance_result_values)
        instance_recorded = set(results_to_keys(instance_result_values))
        instance_missing = [
            case.id for case in required_instances if case.id not in instance_recorded
        ]
        lines.append(
            "instance outcomes: "
            + ", ".join(
                f"{name} {count}" for name, count in sorted(instance_outcome_counts.items())
            )
            + (f", missing {len(instance_missing)}" if instance_missing else "")
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
    if full:
        if instance_failures:
            lines.append(f"unresolved instance failures ({len(instance_failures)}):")
            for failure in instance_failures:
                detail = f" ({failure.failure.value}, {failure.phase})"
                if failure.detail:
                    detail += f": {failure.detail}"
                lines.append(
                    f"  {failure.case.id} [{failure.case.kind}] {failure.outcome.value}{detail}"
                )
        else:
            lines.append("unresolved instance failures: none")
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
    if full and instance_baseline is not None:
        lines.extend(_instance_baseline_summary(instance_baseline, instance_comparison))
    return "\n".join(lines) + "\n"


def render_json(
    cases: Iterable[CorpusCase],
    results: Iterable[CaseResult],
    baseline: Baseline,
    dispositions: Sequence[Disposition],
    *,
    deferred: int = 0,
    instance_results: Iterable[CaseResult] | None = None,
    instance_baseline: Baseline | None = None,
) -> str:
    """Render the same evidence as :func:`render_report`, as stable JSON."""
    all_cases = tuple(cases)
    all_schema = tuple(schema_cases(all_cases))
    required = required_schema_cases(all_schema)
    schema_results = tuple(
        result for result in results if result.case.kind in SCHEMA_KINDS and result.case.is_required
    )
    comparison = diff(baseline, results_to_keys(schema_results))
    full = instance_results is not None
    instance_result_values = (
        tuple(result for result in instance_results if result.case.is_required)
        if instance_results is not None
        else ()
    )
    if full:
        all_instances = instance_cases(all_cases)
        required_instances = required_instance_cases(all_instances)
        instance_comparison = (
            diff_instances(instance_baseline, results_to_keys(instance_result_values))
            if instance_baseline is not None
            else BaselineDiff(())
        )
        excluded = excluded_dependencies(all_cases, dispositions)
    else:
        all_instances = ()
        required_instances = ()
        instance_comparison = BaselineDiff(())
        excluded = excluded_dependencies(all_schema, dispositions)
    profile_counts = Counter(case.profile for case in required)
    scope: dict[str, int] = {
        "required_schema_cases": len(required),
        "core": profile_counts.get("core", 0),
        "xsd_datatype": profile_counts.get("xsd-datatype", 0),
        "excluded_schema_cases": len(all_schema) - len(required),
        "deferred_instance_assertions": deferred,
    }
    if full:
        instance_profile_counts = Counter(case.profile for case in required_instances)
        scope.update(
            {
                "required_instance_assertions": len(required_instances),
                "instance_core": instance_profile_counts.get("core", 0),
                "instance_xsd_datatype": instance_profile_counts.get("xsd-datatype", 0),
                "excluded_instance_cases": len(all_instances) - len(required_instances),
            }
        )
    payload = {
        "catalog": CATALOG_COMMIT,
        "scope": scope,
        "results": {
            result.case.id: {
                "kind": result.case.kind,
                "profile": result.case.profile,
                "outcome": result.outcome.value,
                "failure": result.failure.value,
                "detail": result.detail,
                "phase": result.phase,
            }
            for result in sorted(schema_results, key=lambda item: item.case.id)
        },
        "unresolved": [
            {
                "case": failure.case.id,
                "kind": failure.case.kind,
                "profile": failure.case.profile,
                "outcome": failure.outcome.value,
                "failure": failure.failure.value,
                "detail": failure.detail,
                "phase": failure.phase,
            }
            for failure in unresolved_failures(schema_results)
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
    if full:
        payload.update(
            {
                "instance_results": {
                    result.case.id: {
                        "kind": result.case.kind,
                        "profile": result.case.profile,
                        "outcome": result.outcome.value,
                        "failure": result.failure.value,
                        "detail": result.detail,
                        "phase": result.phase,
                    }
                    for result in sorted(instance_result_values, key=lambda item: item.case.id)
                },
                "instance_unresolved": [
                    {
                        "case": failure.case.id,
                        "kind": failure.case.kind,
                        "profile": failure.case.profile,
                        "outcome": failure.outcome.value,
                        "failure": failure.failure.value,
                        "detail": failure.detail,
                        "phase": failure.phase,
                    }
                    for failure in unresolved_failures(instance_result_values)
                ],
                "instance_baseline_changes": [
                    {
                        "case": change.key,
                        "kind": change.kind,
                        "previous": change.previous,
                        "current": change.current,
                    }
                    for change in instance_comparison.changes
                ],
            }
        )
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _metadata(schema_count: int, instance_count: int) -> dict[str, object]:
    return {
        "catalog": CATALOG_COMMIT,
        "profile": _PROFILE_LABEL,
        "schema_cases": schema_count,
        "instance_cases": instance_count,
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
        required_schema = required_schema_cases(all_schema)
        all_instances = instance_cases(cases)
        required_instances = required_instance_cases(all_instances)
        dispositions = load_dispositions(args.dispositions)
        validate_dispositions(dispositions, {case.id: case for case in cases})
    except (CatalogError, DispositionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.list_only:
        print(f"{len(required_schema)} schema cases would run")
        print(f"{len(required_instances)} instance cases would run")
        return 0

    schema_results = run_cases(required_schema, timeout=args.timeout)
    instance_results = run_cases(required_instances, timeout=args.timeout)
    schema_outcomes = results_to_keys(schema_results)
    instance_outcomes = results_to_keys(instance_results)
    try:
        if args.write_baseline is not None:
            save_baseline_checked(
                args.write_baseline,
                schema_outcomes,
                _metadata(len(required_schema), len(required_instances)),
                instances=instance_outcomes,
            )
            baseline = load_baseline(args.write_baseline)
        else:
            baseline = load_baseline(args.baseline)
    except BaselineError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.write(
            render_json(
                cases,
                schema_results,
                baseline,
                dispositions,
                instance_results=instance_results,
                instance_baseline=baseline,
            )
        )
    else:
        sys.stdout.write(
            render_report(
                cases,
                schema_results,
                baseline,
                dispositions,
                instance_results=instance_results,
                instance_baseline=baseline,
            )
        )

    schema_comparison = diff(baseline, schema_outcomes)
    instance_comparison = diff_instances(baseline, instance_outcomes)
    schema_failures = unresolved_failures(schema_results)
    instance_failures = unresolved_failures(instance_results)
    if (
        schema_failures
        or instance_failures
        or (args.enforce and (schema_comparison or instance_comparison))
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
