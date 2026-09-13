"""Integration with the pinned W3C XML Schema Test Suite corpus.

The catalogue check runs whenever the submodule is present.  The full
baseline comparison is opt-in because it takes minutes; set
``PYXSD_RUN_XSTS=1`` to enable it.
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
    results = run_parallel(
        cases,
        XSD11.name,
        oracle_enabled=True,
        timeout=30.0,
        jobs=os.cpu_count() or 1,
        temp_root=tmp_path,
    )
    diff = baseline.diff(baseline.load(BASELINE), results_to_keys(results))

    summary = "\n".join(
        f"[{change.kind}] {change.key}: {change.previous} -> {change.current}"
        for change in diff.changes[:50]
    )
    assert not diff.changes, f"{len(diff.changes)} changes from the baseline:\n{summary}"
