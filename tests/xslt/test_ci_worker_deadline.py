"""Deadline and budget handling for the parallel worker pool.

The short-grace reaping modes, an empty budgeted selection, the deadline
worker's setup failure, and the per-case budget interrupt live here; the long
grace mode and the non-deadline pool contract are separate modules.
"""

from __future__ import annotations

import json
import os
import sys
import time
from threading import Barrier
from types import SimpleNamespace

import pytest
from tests.xslt import ci_contract_support
from tests.xslt.ci_contract_support import REPO_ROOT, RIGHT_SHEET, fixture_repo
from tests.xslt.suite import SUITE_PROFILES, case_budget, run_parallel
from tests.xslt.support import run_probe


@pytest.fixture(autouse=True)
def _no_ambient_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's budget hermetic: the test sets it, never the shell."""
    ci_contract_support.clear_ambient_timeout(monkeypatch)


def test_parallel_accepts_an_empty_budgeted_selection():
    assert run_parallel((), SUITE_PROFILES["xslt30"], timeout=5, jobs=4) == ()


def test_deadline_worker_aborts_barrier_when_setup_fails(monkeypatch, tmp_path):
    from threading import BrokenBarrierError

    barrier = Barrier(2)
    monkeypatch.setattr(ci_contract_support, "Barrier", lambda parties: barrier)
    monkeypatch.setenv("PYXSD_TEST_XSLT_WORKER_PID", str(tmp_path / "missing" / "worker.pid"))
    with pytest.raises(BrokenBarrierError):
        ci_contract_support.deadline_case(
            SimpleNamespace(local_name="case-001"), SUITE_PROFILES["xslt30"]
        )
    assert barrier.broken


@pytest.mark.parametrize(("jobs", "count", "grace"), [(1, 2, 0.5), (2, 1, 0.5)])
def test_parallel_deadline_reaps_an_uninterruptible_worker(tmp_path, jobs, count, grace):
    # The (2, 2) mode keeps the harness's own 5 s grace. The serial and
    # single-case modes exercise the same termination path with a short one.
    names = ("case-001", "case-002")[:count]
    root = fixture_repo(tmp_path, names=names)
    for name in names:
        (root / "tests" / "xslt" / "corpus" / "gate" / f"{name}.xsl").write_text(
            RIGHT_SHEET, encoding="utf-8"
        )
    pid_path = tmp_path / "worker.pid"
    run = run_probe(
        [
            sys.executable,
            "-c",
            """
import json
import multiprocessing
import os
import sys
from tests.xslt import suite
from tests.xslt.catalog import load_catalog
from tests.xslt.ci_contract_support import deadline_case, deadline_worker

suite.execute_case = deadline_case
suite._run_payload = deadline_worker
suite._PARENT_GRACE = float(sys.argv[3])
cases = tuple(load_catalog(sys.argv[1]).cases.values())
results = suite.run_parallel(cases, suite.SUITE_PROFILES["xslt30"], timeout=1, jobs=int(sys.argv[2]))
print(json.dumps([
    os.getpid(),
    [proc.pid for proc in multiprocessing.active_children()],
    [[result.name, result.outcome.value, result.message] for result in results],
]))
""",
            str(root / "tests" / "xslt" / "corpus" / "catalog.xml"),
            str(jobs),
            str(grace),
        ],
        env={**os.environ, "PYXSD_TEST_XSLT_WORKER_PID": str(pid_path)},
        cwd=REPO_ROOT,
        timeout=60,
    )
    parent, children, results = json.loads(run.stdout)
    assert int(pid_path.read_text(encoding="ascii")) != parent
    assert children == []
    expected = [["gate/case-001", "timeout", "the case exceeded its 1 s budget"]]
    if count == 2:
        expected.append(["gate/case-002", "pass", ""])
    assert results == expected


def test_case_budget_interrupts_a_long_case() -> None:
    with pytest.raises(TimeoutError, match="budget"), case_budget(0.05):
        time.sleep(1)
    with case_budget(None):
        pass
