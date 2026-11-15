"""Validated residual dispositions for checked-in XSLT conformance cases.

A disposition explains *why* a known non-passing case is understood.  It is
metadata only: loading or classifying never rewrites a baseline outcome or a
report verdict, and the raw checked denominator stays whatever the profile
recorded.

The census is deliberately explicit.  Every entry lists case IDs (no
patterns), and every non-passing case that no entry claims is returned by
:func:`unclassified` so it stays visibly unclassified instead of being
absorbed by a broad match.  An entry whose cases now pass remains valid
historical metadata but does not describe an active residual, which
:func:`active_entries` reports.

Baselines are the only source of truth for what a profile recorded; pass
``Baseline.as_mapping()`` (or a synthetic mapping with the same shape) as
the outcomes argument.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tests.xslt.baseline import Baseline
from tests.xslt.outcomes import Outcome

#: Closed vocabulary of disposition categories.
CATEGORIES = frozenset(
    {
        "declared-non-goal",
        "implementation-gap",
        "adapter-limit",
        "platform-limit",
        "queried-corpus",
        "spec-correct-divergence",
        "performance-budget",
    }
)

#: The profiles a disposition may name, widest profile first.
PROFILES = ("xslt30", "xslt20", "xslt10")

#: One recorded outcome, as a baseline string or an :class:`Outcome`.
OutcomeValue = str | Outcome


class DispositionError(Exception):
    """Raised when dispositions cannot be read or do not match reality."""


@dataclass(frozen=True)
class Disposition:
    """One reviewed cluster of residual non-passing cases.

    ``id`` is the stable tracking identifier, ``profiles`` names the profiles
    the cases were reviewed under, ``cases`` lists explicit case IDs (no
    patterns), and ``links`` holds public specification or upstream URLs.
    Internal adjudication paths belong in non-emitted TOML comments, not in
    ``links``.  ``reason`` carries the reviewed explanation of the residual.
    """

    id: str
    title: str
    category: str
    profiles: tuple[str, ...]
    cases: tuple[str, ...]
    reason: str
    links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "profiles", tuple(self.profiles))
        object.__setattr__(self, "cases", tuple(self.cases))
        object.__setattr__(self, "links", tuple(self.links))


_SCALAR_FIELDS = ("id", "title", "category", "reason")
_LIST_FIELDS = ("profiles", "cases", "links")
_FIELDS = frozenset(_SCALAR_FIELDS + _LIST_FIELDS)


def load_dispositions(
    path: Path,
    *,
    baselines: Mapping[str, Baseline] | None = None,
) -> tuple[Disposition, ...]:
    """Read the ``[[dispositions]]`` array of tables from *path*.

    The document shape is always checked (tables, string scalars, arrays of
    strings, no unknown fields).  Structural validation always runs;
    membership in a profile's baseline is checked only when *baselines*
    supplies one, so a caller can validate the contract without corpus data.
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
    loaded = tuple(dispositions)
    validate_dispositions(loaded, baselines)
    return loaded


def validate_dispositions(
    dispositions: tuple[Disposition, ...],
    baselines: Mapping[str, Baseline] | None = None,
) -> None:
    """Check every disposition, collecting all problems before raising.

    Structural problems -- blank required fields, a category outside the
    closed vocabulary, an unknown profile, a blank or duplicated case ID, or
    one case claimed twice for a profile -- are always reported.  When
    *baselines* is supplied, each claimed case must also be recorded in the
    named profile's baseline.  Any recorded outcome is allowed: a case that
    no longer fails simply leaves the active residual view, and no outcome is
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
        if not entry.reason.strip():
            problems.append(f"{label}: reason is blank")
        if entry.category not in CATEGORIES:
            problems.append(f"{label}: unknown category {entry.category!r}")
        if not entry.profiles:
            problems.append(f"{label}: no profiles listed")
        if not entry.cases:
            problems.append(f"{label}: no cases listed")
        for case_id in entry.cases:
            if not case_id.strip():
                problems.append(f"{label}: case id is blank")
        for case_id in sorted({case for case in entry.cases if entry.cases.count(case) > 1}):
            problems.append(f"{label}: duplicate case {case_id!r}")
        for profile in entry.profiles:
            if profile not in PROFILES:
                problems.append(f"{label}: unknown profile {profile!r}")
                continue
            baseline = None
            if baselines is not None:
                baseline = baselines.get(profile)
                if baseline is None:
                    problems.append(f"{label}: no baseline for profile {profile!r}")
                    continue
            for case_id in entry.cases:
                owner = claimed.setdefault((profile, case_id), entry.id)
                if owner != entry.id:
                    problems.append(
                        f"{label}: case {case_id!r} in profile {profile!r}"
                        f" already claimed by {owner!r}"
                    )
                    continue
                if baseline is not None and case_id not in baseline.cases:
                    problems.append(f"{label}: unknown case {case_id!r} in the {profile} baseline")
    if problems:
        raise DispositionError("\n".join(problems))


def unclassified(
    outcomes: Mapping[str, OutcomeValue],
    dispositions: tuple[Disposition, ...],
    profile: str,
) -> tuple[str, ...]:
    """Per case, the sorted IDs that are not passing and that no entry claims.

    Only cases present in *outcomes* are considered, so a case the profile no
    longer selects never becomes an unclassified residual.  A pass is never
    returned, whatever an entry claims or omits.
    """
    claimed = {
        case_id for entry in dispositions if profile in entry.profiles for case_id in entry.cases
    }
    return tuple(
        sorted(
            case_id
            for case_id, outcome in outcomes.items()
            if outcome != Outcome.PASS and case_id not in claimed
        )
    )


def active_entries(
    profile: str,
    outcomes: Mapping[str, OutcomeValue],
    dispositions: tuple[Disposition, ...],
) -> tuple[Disposition, ...]:
    """The entries that still describe a non-passing case under *profile*.

    An entry that does not name *profile*, or whose cases all pass (or are
    absent from *outcomes*), is historical metadata and is omitted.
    """
    return tuple(
        entry
        for entry in dispositions
        if profile in entry.profiles
        and any(outcomes.get(case_id, Outcome.PASS) != Outcome.PASS for case_id in entry.cases)
    )
