"""Pins that each checked-in profile baseline covers the catalog and reproduces.

The rendering is compared both through the CLI's in-memory ``--check`` and
against the checked-in bytes, so a drifted or hand-edited file fails even when
its parsed content would re-render identically.
"""

from __future__ import annotations

import pytest
from tests import report_xslt
from tests.xslt import baseline as baseline_module
from tests.xslt.catalog import load_catalog
from tests.xslt.ci_contract_support import CORPUS, REPO_ROOT, baseline_file, clear_ambient_timeout
from tests.xslt.selection import select_cases
from tests.xslt.suite import SUITE_PROFILES


@pytest.fixture(autouse=True)
def _no_ambient_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every fixture's budget hermetic: the test sets it, never the shell."""
    clear_ambient_timeout(monkeypatch)


@pytest.mark.skipif(not CORPUS.is_file(), reason="xslt30-test corpus is not checked out")
def test_real_catalog_smoke_covers_every_baseline_case() -> None:
    catalog = load_catalog(CORPUS)
    assert not catalog.metadata_errors
    for name, profile in SUITE_PROFILES.items():
        declared = {case.name for case in select_cases(catalog, profile.feature_profile)}
        recorded = set(baseline_module.load_baseline(name).cases)
        assert recorded == declared


# ---------------------------------------------------------------------------
# Report shape and reproducibility


@pytest.mark.skipif(not CORPUS.is_file(), reason="xslt30-test corpus is not checked out")
@pytest.mark.parametrize("profile", sorted(SUITE_PROFILES))
def test_checked_in_baseline_is_reproducible(
    profile: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert report_xslt.run(["--profile", profile, "--check"]) == 0
    first = capsys.readouterr()
    assert report_xslt.run(["--profile", profile, "--check"]) == 0
    second = capsys.readouterr()
    assert first.err == second.err
    # ``--check`` renders in memory; pin the checked-in bytes too, so a
    # hand-edited or drifted file fails even when its parsed content re-renders.
    baseline_path = baseline_file(REPO_ROOT, profile)
    assert (
        baseline_module.render_baseline(baseline_module.load_baseline(profile)).encode()
        == baseline_path.read_bytes()
    )
