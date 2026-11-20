"""Integration with the pinned W3C XSLT 3.0 test catalog, per profile.

The catalogue/profiles smoke checks run whenever the submodule is present.
The full baseline comparison is opt-in because it takes tens of minutes; set
``PYXSD_RUN_XSLT=1`` to enable it, ``PYXSD_XSLT_PROFILE`` to select one
profile (default: all three), and ``PYXSD_XSLT_TIMEOUT`` for the per-case
budget.

Baseline contract: each profile has its own file under
``tests/xslt/baselines`` and records every case the profile declares
applicable.  The verdicts include expected failures; they are the ledger, not
a claim of conformance.  The gate's ``assert not diff.changes`` means no new
failure (a pass becoming fail drifts) and anything fixed stays fixed (a fail
becoming pass drifts until its entry is moved in the same commit as the
intentional fix).  Never regenerate a baseline wholesale to make the gate
green; move only the entries an intentional behavior change actually flips.

``timeout`` records the per-case harness budget, not a semantic verdict.  Drift
is exactly the ``fail``↔``timeout`` pair: a slower machine can expire the
budget before reaching the same failure, so that pair is reported without
failing the gate.  Every other non-pass transition fails it, including a
passing case becoming a timeout, and a baseline recorded under a different
budget is refused before any verdict is compared.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from elementpath.regex import unicode_version
from tests import report_xslt
from tests.xslt.baseline import build_baseline, gate_changes, load_baseline
from tests.xslt.catalog import load_catalog
from tests.xslt.model import Dependency
from tests.xslt.outcomes import Outcome
from tests.xslt.selection import inapplicability, select_cases
from tests.xslt.suite import (
    ENGINE_UNICODE_VERSION,
    SUITE_PROFILES,
    default_jobs,
    run_parallel,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = REPO_ROOT / "tests" / "xslt" / "corpus" / "catalog.xml"

pytestmark = pytest.mark.skipif(
    not CORPUS.is_file(),
    reason="xslt30-test corpus not checked out (git submodule update --init tests/xslt/corpus)",
)

#: The three profiles select independent sets from the same pinned catalog.
#: The Unicode-version gate is part of selection: 1,580 cases declare a
#: ``unicode-version`` dependency that differs from the engine's tables, so
#: they are not applicable to the 3.0 and 2.0 profiles.  The 1.0 profile
#: already excluded them through the inherited ``XSLT20+`` spec dependency.
#: The numbering-capability gate is part of selection too: 107 cases declare a
#: ``combinations_for_numbering`` dependency, and the sequences the engine does
#: not implement (or does not implement as wide) leave the set — 31 cases for
#: the 3.0 and 2.0 profiles, 29 for 1.0, whose two Greek cases already excluded
#: themselves through ``XSLT20+``.
EXPECTED_CASES = {"xslt30": 8846, "xslt20": 6371, "xslt10": 1949}

#: Total cases in the pinned catalogue, including cases outside every profile.
EXPECTED_TOTAL = 14601

_configured = os.environ.get("PYXSD_XSLT_PROFILE")
if _configured is not None and _configured not in SUITE_PROFILES:
    raise ValueError(f"PYXSD_XSLT_PROFILE must be one of {sorted(SUITE_PROFILES)}")
RUN_PROFILES = tuple(sorted(SUITE_PROFILES)) if _configured is None else (_configured,)


def test_catalog_parses_and_profiles_select_the_declared_cases() -> None:
    catalog = load_catalog(CORPUS)

    assert not catalog.metadata_errors
    assert len(catalog.cases) == EXPECTED_TOTAL
    assert catalog.corpus_revision == report_xslt.pinned_corpus(REPO_ROOT)
    for name, expected in EXPECTED_CASES.items():
        selected = select_cases(catalog, SUITE_PROFILES[name].feature_profile)
        assert len(selected) == expected, name
        assert len({case.name for case in selected}) == expected


def test_every_profile_declares_the_engine_unicode_version() -> None:
    """The gate compares the tables the engine reports, never a constant."""
    assert unicode_version() == ENGINE_UNICODE_VERSION
    assert ENGINE_UNICODE_VERSION
    for name, profile in SUITE_PROFILES.items():
        assert profile.feature_profile.unicode_version == ENGINE_UNICODE_VERSION, name


def test_unicode_version_gate_selects_exactly_the_version_pinned_cases() -> None:
    catalog = load_catalog(CORPUS)
    gated = {
        case.name: case
        for case in catalog.cases.values()
        if any(dependency.kind == "unicode-version" for dependency in case.dependencies)
    }

    # The pinned metadata declares two versions, inherited into every case.
    assert len(gated) == 1580
    assert {
        dependency.value
        for case in gated.values()
        for dependency in case.dependencies
        if dependency.kind == "unicode-version"
    } == {"9.0", "6.0"}
    assert {
        test_set: sum(1 for case in gated.values() if case.test_set == test_set)
        for test_set in {case.test_set for case in gated.values()}
    } == {"unicode-90": 1460, "regex-classes": 120}

    selected: dict[str, set[str]] = {}
    for name in ("xslt30", "xslt20"):
        profile = SUITE_PROFILES[name].feature_profile
        chosen = select_cases(catalog, profile)
        selected[name] = {case.name for case in chosen}
        assert len(chosen) == EXPECTED_CASES[name], name
        assert not selected[name] & set(gated), name
        for case in gated.values():
            assert inapplicability(case, profile) is Outcome.NOT_APPLICABLE, case.name

    # The gate reads the declared version: the same case stays applicable when
    # its declaration names the engine's own major.minor.
    sample = next(case for case in gated.values() if case.test_set == "unicode-90")
    matching = replace(
        sample,
        dependencies=(
            Dependency("unicode-version", ".".join(ENGINE_UNICODE_VERSION.split(".")[:2])),
        ),
    )
    assert inapplicability(matching, SUITE_PROFILES["xslt30"].feature_profile) is None

    # Clearing the claim restores exactly the gated families, and nothing else.
    cleared = replace(SUITE_PROFILES["xslt30"].feature_profile, unicode_version="")
    restored = {case.name for case in select_cases(catalog, cleared)}
    assert restored - selected["xslt30"] == set(gated)
    assert not selected["xslt30"] - restored
    assert len(restored) == 10426

    # Unrelated dependencies still exclude a case from a version-cleared profile.
    unsupported = [
        case
        for case in catalog.cases.values()
        if case.name not in restored
        and inapplicability(case, cleared) is Outcome.UNSUPPORTED_FEATURE
    ]
    older = [
        case
        for case in catalog.cases.values()
        if "XSLT10" in case.specs and case.name not in restored
    ]
    assert unsupported
    assert older
    for case in older:
        assert inapplicability(case, cleared) is Outcome.NOT_APPLICABLE, case.name


def test_each_baseline_names_the_pinned_catalog_and_profile() -> None:
    catalog = load_catalog(CORPUS)

    for name in SUITE_PROFILES:
        recorded = load_baseline(name)
        assert recorded.meta["corpus"] == catalog.corpus_revision
        assert recorded.meta["profile"] == name


@pytest.mark.skipif(
    os.environ.get("PYXSD_RUN_XSLT") != "1",
    reason="set PYXSD_RUN_XSLT=1 to run the full suite",
)
@pytest.mark.parametrize("profile_name", RUN_PROFILES)
def test_full_run_matches_the_baseline(profile_name: str) -> None:
    profile = SUITE_PROFILES[profile_name]
    catalog = load_catalog(CORPUS)
    cases = select_cases(catalog, profile.feature_profile)
    # The budget is a harness knob, not a verdict.  The checked-in baselines
    # were recorded with the same budget the CI jobs use; regex and Unicode
    # cases that need more are recorded as timeouts, not as passes.
    timeout = float(os.environ.get("PYXSD_XSLT_TIMEOUT", "5"))

    def progress(done: int, total: int) -> None:
        if done % 1000 == 0 or done == total:
            print(f"  ...{profile_name}: {done}/{total} cases", flush=True)

    results = run_parallel(
        cases,
        profile,
        timeout=timeout,
        jobs=default_jobs(),
        on_progress=progress,
    )
    current = build_baseline(results, corpus=catalog.corpus_revision, profile=profile_name)
    recorded = load_baseline(profile_name)
    recorded_budget = recorded.meta.get("timeout_seconds")
    assert recorded_budget == timeout, (
        f"{profile_name} baseline records timeout budget {recorded_budget!r}, "
        f"but this run uses {timeout:g}s; verdicts recorded under different budgets "
        "are not comparable -- regenerate the baseline deliberately with --write-baseline"
    )
    changes, drift = gate_changes(recorded, current)
    if drift:
        print(
            f"  {profile_name}: {len(drift)} harness-budget changes (not semantic)",
            flush=True,
        )

    summary = "\n".join(
        f"[{change.kind}] {change.key}: {change.previous} -> {change.current}"
        for change in changes[:50]
    )
    assert not changes, f"{len(changes)} changes in {profile_name}:\n{summary}"
