"""Auditable dispositions and release blockers for RELAX NG cases.

A case's declared profile is its default disposition: ``core`` and
``xsd-datatype`` are release-required, while ``compact-syntax`` and
``dtd-compatibility`` are outside this plan's profile.  Explicit disposition
entries add rationale for exclusions and may group cases for reporting; they
can never reclassify a required case as excluded.

Only an actual excluded-feature dependency permits ``profile-excluded``.
Every excluded case must name that dependency.  A core or XSD-datatype case
that fails, or that cannot reach a verdict, remains a release blocker.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .baseline import Baseline
from .model import (
    EXCLUDED_PROFILES,
    INSTANCE_KINDS,
    PROFILES,
    CorpusCase,
    Outcome,
    satisfies_expected,
)

CATEGORIES = PROFILES

_SCALAR_FIELDS = ("id", "title", "category", "rationale")
_LIST_FIELDS = ("cases", "links")
_FIELDS = frozenset(_SCALAR_FIELDS + _LIST_FIELDS + ("feature",))


class DispositionError(Exception):
    """Raised when dispositions cannot be read or do not match the corpus."""


@dataclass(frozen=True)
class Disposition:
    """One adjudicated group of corpus cases."""

    id: str
    title: str
    category: str
    cases: tuple[str, ...]
    rationale: str
    feature: str | None = None
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReleaseViolation:
    """One reason the current baseline cannot be released."""

    case_id: str
    reason: str
    outcome: str | None
    category: str


def load_dispositions(path: Path) -> tuple[Disposition, ...]:
    """Read the ``[[dispositions]]`` array of tables from *path*.

    Only shape is checked here; :func:`validate_dispositions` checks the
    entries against the selected corpus cases.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DispositionError(f"{path}: cannot read dispositions: {exc}") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise DispositionError(f"{path}: malformed TOML: {exc}") from exc

    unknown_sections = set(data) - {"dispositions"}
    if unknown_sections:
        raise DispositionError(f"{path}: unknown sections {sorted(unknown_sections)}")
    entries = data.get("dispositions", [])
    if not isinstance(entries, list):
        raise DispositionError(f"{path}: expected a [[dispositions]] array of tables")

    dispositions: list[Disposition] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise DispositionError(f"{path}: entry {index} is not a table")
        where = f"{path}: entry {index}"
        for field in entry:
            if field not in _FIELDS:
                raise DispositionError(f"{where}: unknown field {field!r}")
        values: dict[str, object] = {}
        for field in _SCALAR_FIELDS:
            value = entry.get(field)
            if not isinstance(value, str):
                raise DispositionError(f"{where}: {field} must be a string")
            values[field] = value
        feature = entry.get("feature")
        if feature is not None and not isinstance(feature, str):
            raise DispositionError(f"{where}: feature must be a string")
        values["feature"] = feature
        for field in _LIST_FIELDS:
            value = entry.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise DispositionError(f"{where}: {field} must be an array of strings")
            values[field] = tuple(value)
        dispositions.append(Disposition(**values))  # type: ignore[arg-type]
    return tuple(dispositions)


def validate_dispositions(
    dispositions: tuple[Disposition, ...],
    cases: Mapping[str, CorpusCase],
) -> None:
    """Check dispositions against the closed profile vocabulary and corpus.

    Every excluded-profile case needs exactly one explicit entry naming the
    actual excluded feature.  Required cases need no entry: their profile is
    the disposition, and no entry may move them out of scope.
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    claimed: dict[str, str] = {}
    for entry in dispositions:
        label = entry.id or "<blank id>"
        if not entry.id.strip():
            problems.append(f"{label}: tracking id is blank")
        elif entry.id in seen_ids:
            problems.append(f"{label}: duplicate tracking id")
        else:
            seen_ids.add(entry.id)
        if not entry.title.strip():
            problems.append(f"{label}: title is blank")
        if not entry.rationale.strip():
            problems.append(f"{label}: rationale is blank")
        if entry.category not in CATEGORIES:
            problems.append(f"{label}: unknown category {entry.category!r}")
            continue
        excluded = entry.category in EXCLUDED_PROFILES
        if excluded:
            if not entry.feature or not entry.feature.strip():
                problems.append(f"{label}: excluded category requires a feature dependency")
            elif entry.feature != entry.category:
                problems.append(
                    f"{label}: feature {entry.feature!r} does not match category {entry.category!r}"
                )
        elif entry.feature is not None:
            problems.append(f"{label}: required category must not name an excluded feature")
        if not entry.cases:
            problems.append(f"{label}: no cases listed")
        seen_cases: set[str] = set()
        for case_id in entry.cases:
            if case_id in seen_cases:
                problems.append(f"{label}: duplicate case {case_id!r}")
                continue
            seen_cases.add(case_id)
            case = cases.get(case_id)
            if case is None:
                problems.append(f"{label}: unknown case {case_id!r}")
                continue
            if case.profile != entry.category:
                problems.append(
                    f"{label}: case {case_id!r} has profile {case.profile!r}, "
                    f"not category {entry.category!r}"
                )
            owner = claimed.setdefault(case_id, entry.id)
            if owner != entry.id:
                problems.append(
                    f"{label}: duplicate claim for case {case_id!r} already owned by {owner!r}"
                )

    claimed_ids = set(claimed)
    for case_id, case in sorted(cases.items()):
        if case.profile in EXCLUDED_PROFILES and case_id not in claimed_ids:
            problems.append(
                f"excluded case {case_id!r} ({case.profile}) has no feature disposition"
            )
    if problems:
        raise DispositionError("\n".join(problems))


def release_violations(
    cases: Mapping[str, CorpusCase],
    baseline: Baseline,
    dispositions: tuple[Disposition, ...],
) -> tuple[ReleaseViolation, ...]:
    """The current baseline's release blockers, in stable case-id order.

    Required cases must record ``pass``.  A wrong verdict, adapter gap,
    timeout, limit exhaustion, missing result, or profile exclusion is a
    blocker.  Excluded cases are not executed by the release gate, so an
    absent result, ``pass``, or ``profile-excluded`` is acceptable.  Baseline
    keys that no longer name a catalog case are also blockers.

    Instance assertions read :attr:`Baseline.instances`; schema assertions
    read :attr:`Baseline.results`.  A schema-only case map ignores instance
    baseline entries, so the Phase 1 schema gate remains independent of the
    Phase 2 instance corpus.  For compatibility, an instance case may still
    record its outcome in ``results`` when no ``instances`` entry exists.
    """
    validate_dispositions(dispositions, cases)
    violations: list[ReleaseViolation] = []
    unknown = sorted(set(baseline.results) - set(cases))
    for case_id in unknown:
        violations.append(
            ReleaseViolation(
                case_id=case_id,
                reason="baseline records an unknown case",
                outcome=baseline.results[case_id],
                category="",
            )
        )
    if any(case.kind in INSTANCE_KINDS for case in cases.values()):
        for case_id in sorted(set(baseline.instances) - set(cases)):
            violations.append(
                ReleaseViolation(
                    case_id=case_id,
                    reason="baseline records an unknown instance case",
                    outcome=baseline.instances[case_id],
                    category="",
                )
            )
    for case_id in sorted(cases):
        case = cases[case_id]
        if case.profile in EXCLUDED_PROFILES:
            continue
        if case.kind in INSTANCE_KINDS:
            outcome = baseline.instances.get(case.id, baseline.results.get(case.id))
        else:
            outcome = baseline.results.get(case.id)
        if outcome is None:
            violations.append(
                ReleaseViolation(
                    case_id=case.id,
                    reason="required case has no recorded outcome",
                    outcome=None,
                    category=case.profile,
                )
            )
            continue
        if not satisfies_expected(case, Outcome(outcome)):
            violations.append(
                ReleaseViolation(
                    case_id=case.id,
                    reason="required case did not pass",
                    outcome=outcome,
                    category=case.profile,
                )
            )
    return tuple(sorted(violations, key=lambda violation: violation.case_id))
