"""Integration with the pinned W3C XML Schema Test Suite corpus.

The catalogue check runs whenever the submodule is present.  The full
baseline comparison is opt-in because it takes minutes; set
``PYXSD_RUN_XSTS=1`` to enable it.

Baseline contract: ``baseline-xsd11.toml`` is the source of truth for
per-case verdicts, passing and failing -- it has never been 100% passing,
and its failing entries are expected, not bugs.  The gate's ``assert not
diff.changes`` means no new failures (a pass becoming fail drifts) and
anything fixed stays fixed (a fail becoming pass drifts until its entry
is moved in the same commit as the intentional fix).  Never regenerate
the baseline wholesale to make the gate green; move only the entries an
intentional behavior change actually flips, in that change's commit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from xsts import baseline
from xsts.report import results_to_keys
from xsts.runner import build_cases, corpus_available, load_default_catalog, run_parallel
from xsts.selection import XSD11

BASELINE = Path(__file__).resolve().parent / "xsts" / "baseline-xsd11.toml"

pytestmark = pytest.mark.skipif(
    not corpus_available(),
    reason="xsdtests corpus not checked out (git submodule update --init tests/xsts/corpus)",
)


def test_catalog_parses_and_covers_the_expected_case_count() -> None:
    cases = build_cases(load_default_catalog(), XSD11)

    schema_cases = [case for case in cases if case.kind == "schema"]
    instance_cases = [case for case in cases if case.kind == "instance"]
    assert len(schema_cases) == 15378
    assert len(instance_cases) == 26357
    assert not [case for case in cases if case.metadata_error]


@pytest.mark.skipif(
    os.environ.get("PYXSD_RUN_XSTS") != "1",
    reason="set PYXSD_RUN_XSTS=1 to run the full suite",
)
def test_full_run_matches_the_baseline(tmp_path: Path) -> None:
    cases = build_cases(load_default_catalog(), XSD11)
    # The per-case budget is a harness knob, not a verdict: 30 s covers
    # every case with headroom on a developer machine, but GitHub's
    # standard runners are 2-4x slower, where the slowest instance
    # cases (particlesZ034/reZ003-class) would cross it and report
    # bogus timeouts. CI sets PYXSD_XSTS_TIMEOUT=180 for that reason;
    # a case that legitimately needs more than the budget on the
    # machine it runs on should be profiled, not silently re-budgeted.
    # The dev default is 60 s because the repeated-particle fast path
    # moved the regex cases (reZ005/reZ006) to a 24-43 s band on a dev
    # machine: at 30 they still straddle the budget and report timeouts
    # on a loaded machine. particlesZ036_b1 now passes at about 38 s on a
    # modern laptop, also close to the default; set PYXSD_XSTS_TIMEOUT=180
    # when running locally under load or when matching CI.
    case_timeout = float(os.environ.get("PYXSD_XSTS_TIMEOUT", "60"))
    results = run_parallel(
        cases,
        XSD11.name,
        oracle_enabled=True,
        timeout=case_timeout,
        jobs=os.cpu_count() or 1,
        temp_root=tmp_path,
        # Must match tests/report_xsts.py, the baseline generator: it always
        # synthesizes permissive schemas for groups with no schemaTest. With
        # synthesis off, every such group flips to adapter-gap and the ~50
        # affected keys drift away from their recorded verdicts.
        synthesize_missing_schema=True,
    )
    diff = baseline.diff(baseline.load(BASELINE), results_to_keys(results))

    summary = "\n".join(
        f"[{change.kind}] {change.key}: {change.previous} -> {change.current}"
        for change in diff.changes[:50]
    )
    assert not diff.changes, f"{len(diff.changes)} changes from the baseline:\n{summary}"
