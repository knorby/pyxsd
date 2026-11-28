"""Parallel pool behaviour: ordering, failures, start methods, and payloads.

A dead worker, an unpicklable result, and an unexpected queue error all end the
pool without leaving children behind; the start method follows xdist and the
parent's thread state; and each worker gates with the parent's profile identity.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.xslt import suite as suite_module
from tests.xslt.catalog import load_catalog
from tests.xslt.ci_contract_support import (
    REPO_ROOT,
    RIGHT_SHEET,
    clear_ambient_timeout,
    fixture_repo,
)
from tests.xslt.outcomes import CaseResult, Outcome
from tests.xslt.suite import (
    ENGINE_UNICODE_VERSION,
    SUITE_PROFILES,
    SuiteProfile,
    _parallel_start_method,
    _run_payload,
    run_parallel,
    with_unicode_version,
)
from tests.xslt.support import run_probe


@pytest.fixture(autouse=True)
def _no_ambient_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's budget hermetic: the test sets it, never the shell."""
    clear_ambient_timeout(monkeypatch)


def test_parallel_execution_preserves_catalog_order(tmp_path: Path) -> None:
    root = fixture_repo(tmp_path, names=("case-001", "case-002", "case-003"))
    for name in ("case-001", "case-002", "case-003"):
        (root / "tests" / "xslt" / "corpus" / "gate" / f"{name}.xsl").write_text(
            RIGHT_SHEET, encoding="utf-8"
        )
    catalog = load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")
    cases = tuple(catalog.cases.values())
    results = run_parallel(cases, SUITE_PROFILES["xslt30"], timeout=10, jobs=2)
    assert [result.name for result in results] == [case.name for case in cases]
    assert {result.name: result.outcome for result in results} == {
        "gate/case-001": Outcome.PASS,
        "gate/case-002": Outcome.PASS,
        "gate/case-003": Outcome.PASS,
    }


def test_parallel_unexpected_parent_failure_terminates_a_stuck_worker(tmp_path):
    names = ("case-001", "case-002")
    root = fixture_repo(tmp_path, names=names)
    for name in names:
        (root / "tests" / "xslt" / "corpus" / "gate" / f"{name}.xsl").write_text(
            RIGHT_SHEET, encoding="utf-8"
        )
    run = run_probe(
        [
            sys.executable,
            "-c",
            """
import functools
import json
import multiprocessing
import os
import sys
import threading
from tests.xslt import suite
from tests.xslt.catalog import load_catalog
from tests.xslt.ci_contract_support import receive_stuck_sibling, unpicklable_payload

# The event has to come from the context the runner is about to pick.  A lock
# created in the fork context (the Linux default up to 3.13) cannot be shared
# with spawn workers, which the runner picks inside an xdist worker.
method = suite._parallel_start_method(
    in_xdist_worker="PYTEST_XDIST_WORKER" in os.environ,
    active_threads=threading.active_count(),
)
context = multiprocessing.get_context(method)
stuck = context.Event()
context.Pool = functools.partial(
    context.Pool, initializer=receive_stuck_sibling, initargs=(stuck,)
)
suite._run_payload = unpicklable_payload
cases = tuple(load_catalog(sys.argv[1]).cases.values())
try:
    suite.run_parallel(cases, suite.SUITE_PROFILES["xslt30"], timeout=20, jobs=2)
except BaseException as error:
    print(json.dumps([
        type(error).__name__,
        stuck.is_set(),
        method,
        [child.pid for child in multiprocessing.active_children()],
    ]))
else:
    raise SystemExit("run_parallel did not fail")
""",
            str(root / "tests" / "xslt" / "corpus" / "catalog.xml"),
        ],
        cwd=REPO_ROOT,
        timeout=60,
    )
    error_name, sibling_stuck, method, children = json.loads(run.stdout)
    assert error_name == "MaybeEncodingError"
    assert sibling_stuck
    assert method in {"fork", "spawn"}
    assert children == []


def test_run_parallel_terminates_instead_of_closing_on_an_unexpected_get_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingHandle:
        def get(self, timeout: float | None) -> tuple[str, CaseResult]:
            raise RuntimeError("result unmarshalling failed")

    class FakePool:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def apply_async(self, *args: object, **kwargs: object) -> FailingHandle:
            return FailingHandle()

        def terminate(self) -> None:
            self.calls.append("terminate")

        def join(self) -> None:
            self.calls.append("join")

        def close(self) -> None:
            self.calls.append("close")

    pool = FakePool()
    monkeypatch.setattr(
        suite_module.multiprocessing,
        "get_context",
        lambda method: SimpleNamespace(Pool=lambda processes: pool),
    )
    cases = (SimpleNamespace(name="gate/case-001"), SimpleNamespace(name="gate/case-002"))
    with pytest.raises(RuntimeError, match="result unmarshalling failed"):
        suite_module.run_parallel(cases, SUITE_PROFILES["xslt30"], timeout=1, jobs=2)
    assert pool.calls == ["terminate", "join"]


def test_parallel_workers_avoid_fork_in_xdist_or_multithreaded_parents() -> None:
    assert _parallel_start_method(in_xdist_worker=True, active_threads=1) == "spawn"
    assert _parallel_start_method(in_xdist_worker=False, active_threads=2) == "spawn"


def test_serial_workers_keep_the_fast_safe_start_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        suite_module.multiprocessing, "get_all_start_methods", lambda: ["fork", "spawn"]
    )
    assert _parallel_start_method(in_xdist_worker=False, active_threads=1) == "fork"
    monkeypatch.setattr(suite_module.multiprocessing, "get_all_start_methods", lambda: ["spawn"])
    assert _parallel_start_method(in_xdist_worker=False, active_threads=1) == "spawn"


def test_worker_payload_carries_the_profile_unicode_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker gates exactly as the parent did, even for a cleared profile."""
    root = fixture_repo(tmp_path)
    catalog = load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")
    case = catalog.cases["gate/case-001"]
    seen: list[str] = []

    def record(item: object, profile: SuiteProfile, **kwargs: object) -> CaseResult:
        seen.append(profile.feature_profile.unicode_version)
        return CaseResult.passed(case.name)

    monkeypatch.setattr("tests.xslt.suite.run_case", record)

    passed = (case.name, CaseResult.passed(case.name))
    assert _run_payload(("xslt30", "", case, None)) == passed
    assert seen == [""]
    assert _run_payload(("xslt30", "9.0", case, None)) == passed
    assert seen == ["", "9.0"]
    assert _run_payload(("xslt30", ENGINE_UNICODE_VERSION, case, None)) == passed
    assert seen == ["", "9.0", ENGINE_UNICODE_VERSION]

    # An unchanged version reuses the checked-in profile; another version keeps
    # the same processor identity and moves only the applicability claim.
    assert (
        with_unicode_version(SUITE_PROFILES["xslt30"], ENGINE_UNICODE_VERSION)
        is SUITE_PROFILES["xslt30"]
    )
    cleared = with_unicode_version(SUITE_PROFILES["xslt30"], "")
    assert cleared.feature_profile.unicode_version == ""
    assert cleared.processor is SUITE_PROFILES["xslt30"].processor
