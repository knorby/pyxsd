"""Deterministic conformance census: outcomes, failure signatures, and samples.

A census is measurement output, not a verdict: it records what one profile run
actually observed so failures can be grouped into families, and it never
compares against or rewrites a baseline.  Every case contributes one record and
every record contributes to exactly one signature group.

A run may also carry a ``gated`` section: cases the profile's selection excluded
and the caller measured anyway, such as a family pinned to another Unicode
version.  Those records have their own totals and are excluded from the profile
totals and signatures; a case contributes to exactly one section, never to both.

The document contains no timestamps, absolute paths, or wall-clock values, and
its JSON rendering is byte-stable for the same results.  Case records and
signature groups are sorted, so two renders of one result set are identical.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from tests.xslt.outcomes import CaseResult, Outcome

#: Every outcome the census totals, in report order; missing ones are zero.
CENSUS_OUTCOMES: tuple[Outcome, ...] = (
    Outcome.PASS,
    Outcome.FAIL,
    Outcome.TIMEOUT,
    Outcome.ADAPTER_GAP,
    Outcome.WRONG_ERROR_CODE,
    Outcome.NOT_APPLICABLE,
    Outcome.UNSUPPORTED_FEATURE,
    Outcome.CORPUS_DISPUTE,
)

#: Longest retained failure signature, in characters.
SIGNATURE_LIMIT = 200

#: Most case names a signature group records.
SAMPLE_LIMIT = 3

#: Outcomes whose signature is deliberately empty: nothing executed wrongly.
_EMPTY_SIGNATURES = frozenset({Outcome.PASS, Outcome.NOT_APPLICABLE})

#: Terminal ANSI escape sequences, such as a colour reset around a message.
_ANSI_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

#: Remaining control characters; a signature is a single display line.
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")

#: Absolute filesystem paths, which name the machine rather than the failure.
#: A token runs to the next delimiter so surrounding prose and quotes survive;
#: bare XPath fragments such as ``/out='2'`` do not match these roots.
_ABSOLUTE_PATH = re.compile(
    r"file://[^\s'\"(),;]*|/(?:Users|home|opt|tmp|var|private)/[^\s'\"(),;]*"
)

#: What an absolute filesystem path becomes in a census message.
PATH_PLACEHOLDER = "<path>"

#: Why a census carries cases outside its profile totals.
GATED_NOTE = (
    "excluded from totals: the profile declares these cases not applicable because of "
    "a declared unicode-version dependency"
)


def failure_signature(result: CaseResult) -> str:
    """One deterministic family key for a case result.

    A failure keeps the first line of its message with ANSI escapes and control
    characters removed, bounded to :data:`SIGNATURE_LIMIT` characters.  A
    timeout is its own bucket because the budget, not the message, decided it.
    A wrong error code names both codes.  A harness gap keeps its structured
    detail verbatim, which is already a fixed vocabulary.
    """
    if result.outcome in _EMPTY_SIGNATURES:
        return ""
    if result.outcome is Outcome.TIMEOUT:
        return "timeout"
    if result.outcome is Outcome.WRONG_ERROR_CODE:
        return f"expected {result.expected_error_code} got {result.actual_error_code}"
    if result.outcome is Outcome.FAIL:
        first_line = result.message.split("\n", 1)[0]
        cleaned = _CONTROL_CHARACTER.sub("", _ANSI_SEQUENCE.sub("", first_line))
        return cleaned[:SIGNATURE_LIMIT]
    return result.message


def _budget_seconds(timeout: float) -> int | float:
    """One budget as a JSON number: whole seconds stay integers."""
    return int(timeout) if timeout.is_integer() else timeout


def _sanitize_message(text: str, root: Path | None) -> str:
    """Remove checkout-specific text so a message names the failure, not the machine.

    A message may embed the absolute path the harness handed the engine.  The
    repository prefix is removed first so corpus paths stay meaningful and
    relative; any other absolute filesystem path becomes :data:`PATH_PLACEHOLDER`.
    Bare XPath fragments are not paths and pass through unchanged.
    """
    if root is not None:
        text = text.replace(f"{str(root).rstrip('/')}/", "")
    return _ABSOLUTE_PATH.sub(PATH_PLACEHOLDER, text)


def _section(
    results: Iterable[CaseResult],
) -> tuple[dict[str, int], list[dict[str, str]], list[dict[str, Any]]]:
    """One section's outcome totals, sorted case records and signature groups."""
    totals = {outcome.value: 0 for outcome in CENSUS_OUTCOMES}
    counts: Counter[str] = Counter()
    outcomes: dict[str, Counter[str]] = {}
    samples: dict[str, list[str]] = {}
    cases: list[dict[str, str]] = []
    for result in sorted(results, key=lambda item: item.name):
        outcome = str(result.outcome)
        totals[outcome] += 1
        signature = failure_signature(result)
        counts[signature] += 1
        outcomes.setdefault(signature, Counter())[outcome] += 1
        samples.setdefault(signature, []).append(result.name)
        cases.append(
            {
                "name": result.name,
                "outcome": outcome,
                "signature": signature,
                "message": result.message,
            }
        )
    signatures = [
        {
            "signature": signature,
            "count": counts[signature],
            "outcomes": {
                outcome.value: outcomes[signature][outcome.value]
                for outcome in CENSUS_OUTCOMES
                if outcomes[signature][outcome.value]
            },
            "samples": sorted(samples[signature])[:SAMPLE_LIMIT],
        }
        for signature in sorted(counts, key=lambda item: (-counts[item], item))
    ]
    return totals, cases, signatures


@dataclass(frozen=True)
class Census:
    """One profile run's normalized case records and signature groups.

    ``gated`` holds cases this profile's selection excluded and the caller
    measured anyway.  They form the separate ``gated`` section and never reach
    the profile totals or signatures.
    """

    profile: str
    corpus: str
    timeout_seconds: int | float
    results: tuple[CaseResult, ...]
    gated: tuple[CaseResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        totals, cases, signatures = _section(self.results)
        document: dict[str, Any] = {
            "profile": self.profile,
            "corpus": self.corpus,
            "timeout_seconds": self.timeout_seconds,
            "totals": totals,
            "cases": cases,
            "signatures": signatures,
        }
        if self.gated:
            gated_totals, gated_cases, _ = _section(self.gated)
            document["gated"] = {
                "note": GATED_NOTE,
                "totals": gated_totals,
                "cases": gated_cases,
            }
        return document


def _validated(results: Iterable[CaseResult], root: Path | None) -> tuple[CaseResult, ...]:
    """*results* with names checked and machine-specific message text removed."""
    entries: list[CaseResult] = []
    names: set[str] = set()
    for result in results:
        if not result.name:
            raise ValueError("a census requires named case results")
        if result.name in names:
            raise ValueError(f"duplicate case result: {result.name}")
        names.add(result.name)
        entries.append(replace(result, message=_sanitize_message(result.message, root)))
    return tuple(entries)


def build_census(
    results: Iterable[CaseResult],
    *,
    profile: str,
    corpus: str,
    timeout_seconds: float,
    root: Path | None = None,
    gated: Iterable[CaseResult] = (),
) -> Census:
    """Build one census from explicit case results, rejecting nameless doubles.

    *root* is the repository checkout whose absolute prefix messages should not
    retain; ``None`` still replaces absolute filesystem paths outside it.
    *gated* holds the cases the profile's selection excluded and the caller
    chose to measure anyway: they get their own section and never contribute to
    the profile totals.  A case contributes to exactly one section, so a name
    that appears in both is rejected.
    """
    entries = _validated(results, root)
    gated_entries = _validated(gated, root)
    shared = {result.name for result in entries} & {result.name for result in gated_entries}
    if shared:
        raise ValueError(f"case result in both sections: {min(shared)}")
    return Census(
        profile=profile,
        corpus=corpus,
        timeout_seconds=_budget_seconds(timeout_seconds),
        results=entries,
        gated=gated_entries,
    )


def render_json(census: Census) -> str:
    """Render one census as deterministic JSON with a trailing newline."""
    return json.dumps(census.to_dict(), indent=2, ensure_ascii=False) + "\n"


def write_census(path: Path, census: Census) -> None:
    """Write one census under *path*; the caller chooses a safe directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_json(census).encode("utf-8"))
