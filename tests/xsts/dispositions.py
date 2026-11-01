"""Validated residual dispositions for checked-in XSTS failures.

A disposition explains *why* a known failure is understood.  It is metadata
only: loading and validating dispositions never rewrites an outcome, and the
raw checked denominator stays ``pass + fail`` regardless of what is
classified here.  Validation reads only ``pyxsd:`` baseline keys -- never
``xmlschema`` oracle outcomes -- so a disposition cannot borrow agreement
from the differential engine, and an entry whose case no longer fails is
reported as stale instead of being silently kept.

The failure census is deliberately explicit: every entry lists case IDs, and
every current failure that no entry claims is returned by
:func:`unclassified_failures` so it stays visibly unclassified rather than
being absorbed by a broad pattern match.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .baseline import Baseline
from .outcomes import Outcome
from .runner import Case
from .selection import PROFILES

#: The only engine whose baseline outcomes may justify a disposition.
ENGINE = "pyxsd"

#: Closed vocabulary of disposition categories.
CATEGORIES = frozenset(
    {
        "implementation-gap",
        "version-switch-candidate",
        "platform-limit",
        "queried-corpus",
        "spec-correct-divergence",
        "adapter-limit",
    }
)

#: Cross-profile expectation relations, derived from corpus expectations only.
SAME_VALID = "same-valid"
SAME_INVALID = "same-invalid"
INVALID_IN_1_0_VALID_IN_1_1 = "invalid-in-1.0-valid-in-1.1"
VALID_IN_1_0_INVALID_IN_1_1 = "valid-in-1.0-invalid-in-1.1"
NOT_COMPARABLE = "not-comparable"


class DispositionError(Exception):
    """Raised when dispositions cannot be read or do not match reality."""


@dataclass(frozen=True)
class Disposition:
    """One adjudicated cluster of residual failures.

    ``id`` is the stable tracking identifier, ``profiles`` names the profiles
    the cases fail under, ``cases`` lists explicit case IDs (no patterns),
    and ``links`` holds public specification or upstream URLs.  Internal
    tracker paths belong in non-emitted TOML comments, not in ``links``.
    """

    id: str
    title: str
    category: str
    profiles: tuple[str, ...]
    cases: tuple[str, ...]
    rationale: str
    links: tuple[str, ...] = ()


_SCALAR_FIELDS = ("id", "title", "category", "rationale")
_LIST_FIELDS = ("profiles", "cases", "links")
_FIELDS = frozenset(_SCALAR_FIELDS + _LIST_FIELDS)


def load_dispositions(path: Path) -> tuple[Disposition, ...]:
    """Read the ``[[dispositions]]`` array of tables from *path*.

    Only the shape is checked here (tables, string scalars, arrays of
    strings, no unknown fields); semantic validation against baselines and
    the corpus is :func:`validate_dispositions`.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DispositionError(f"{path}: cannot read dispositions: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise DispositionError(f"{path}: malformed TOML: {exc}") from exc
    entries = data.get("dispositions")
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
        for field in _LIST_FIELDS:
            value = entry.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise DispositionError(f"{where}: {field} must be an array of strings")
            values[field] = tuple(value)
        dispositions.append(Disposition(**values))  # type: ignore[arg-type]
    return tuple(dispositions)


def validate_dispositions(
    dispositions: tuple[Disposition, ...],
    baselines: Mapping[str, Baseline],
    cases_by_profile: Mapping[str, Mapping[str, Case]],
) -> None:
    """Check every disposition against the current baselines and corpus.

    Raises :class:`DispositionError` listing every problem (so one stale
    entry does not hide the others) when an entry has a blank required
    field, a category outside the closed vocabulary, an unknown profile or
    case, a case claimed twice, or a case whose ``pyxsd:`` baseline outcome
    is missing or no longer ``fail``.  Outcomes themselves are never
    modified.
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    claimed: dict[tuple[str, str], str] = {}
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
        if not entry.profiles:
            problems.append(f"{label}: no profiles listed")
        if not entry.cases:
            problems.append(f"{label}: no cases listed")
        if len(set(entry.cases)) != len(entry.cases):
            for case_id in sorted({c for c in entry.cases if entry.cases.count(c) > 1}):
                problems.append(f"{label}: duplicate case {case_id!r}")
        for profile in entry.profiles:
            if profile not in PROFILES:
                problems.append(f"{label}: unknown profile {profile!r}")
                continue
            if profile not in baselines:
                problems.append(f"{label}: no baseline for profile {profile!r}")
                continue
            if profile not in cases_by_profile:
                problems.append(f"{label}: no cases for profile {profile!r}")
                continue
            cases = cases_by_profile[profile]
            baseline = baselines[profile]
            for case_id in entry.cases:
                case = cases.get(case_id)
                if case is None:
                    problems.append(f"{label}: unknown case {case_id!r} in profile {profile!r}")
                    continue
                owner = claimed.setdefault((profile, case_id), entry.id)
                if owner != entry.id:
                    problems.append(
                        f"{label}: case {case_id!r} in profile {profile!r}"
                        f" already claimed by {owner!r}"
                    )
                    continue
                key = f"{ENGINE}:{case_id}:{case.kind}"
                outcome = baseline.results.get(key)
                if outcome is None:
                    problems.append(f"{label}: {key} is not recorded in the {profile} baseline")
                elif outcome != Outcome.FAIL.value:
                    problems.append(f"{label}: {key} is no longer a failure (baseline: {outcome})")
    if problems:
        raise DispositionError("\n".join(problems))


def unclassified_failures(
    dispositions: tuple[Disposition, ...],
    baselines: Mapping[str, Baseline],
) -> dict[str, tuple[str, ...]]:
    """Per profile, the sorted case IDs that fail but no entry claims.

    Only ``pyxsd:`` keys with the ``fail`` outcome count, so oracle results
    and non-verdict outcomes never enter the census.
    """
    claimed: dict[str, set[str]] = {}
    for entry in dispositions:
        for profile in entry.profiles:
            claimed.setdefault(profile, set()).update(entry.cases)
    census: dict[str, tuple[str, ...]] = {}
    for profile, baseline in baselines.items():
        failing = {
            key.removeprefix(f"{ENGINE}:").rsplit(":", 1)[0]
            for key, value in baseline.results.items()
            if key.startswith(f"{ENGINE}:") and value == Outcome.FAIL.value
        }
        census[profile] = tuple(sorted(failing - claimed.get(profile, set())))
    return census


def expectation_relation(case10: Case, case11: Case) -> str:
    """How the same test's corpus expectations differ across XSD versions.

    Derived only from the two profiles' selected expectations, never from
    outcomes; a non-Boolean expectation on either side is ``not-comparable``
    rather than being forced into valid/invalid.
    """
    expected10 = case10.expected_valid
    expected11 = case11.expected_valid
    if expected10 is None or expected11 is None:
        return NOT_COMPARABLE
    if expected10 is False and expected11 is True:
        return INVALID_IN_1_0_VALID_IN_1_1
    if expected10 is True and expected11 is False:
        return VALID_IN_1_0_INVALID_IN_1_1
    return SAME_VALID if expected10 else SAME_INVALID
