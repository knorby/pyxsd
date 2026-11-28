"""The subprocess probe boundary: wait failures, pipes, and tree kills."""

from __future__ import annotations

import selectors
import subprocess
import sys
from types import SimpleNamespace

import pytest
from tests.xslt import support
from tests.xslt.support import run_probe


@pytest.mark.parametrize(
    "failure_type", [subprocess.TimeoutExpired, RuntimeError, KeyboardInterrupt]
)
def test_probe_wait_failure_kills_busy_descendants(tmp_path, monkeypatch, failure_type):
    fcntl = pytest.importorskip("fcntl")
    lease = tmp_path / "worker.lock"
    self_exit = tmp_path / "worker-self-exit"
    original = subprocess.Popen.communicate
    failure = (
        subprocess.TimeoutExpired("probe", 40)
        if failure_type is subprocess.TimeoutExpired
        else failure_type("injected wait failure")
    )
    waits = []

    def after_worker_starts(process, *args, timeout=None, **kwargs):
        waits.append(timeout)
        if len(waits) > 1:
            return original(process, *args, timeout=timeout, **kwargs)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=20), "worker did not start"
        assert process.stdout.readline().startswith("ready")
        raise failure

    monkeypatch.setattr(subprocess.Popen, "communicate", after_worker_starts)
    with pytest.raises(failure_type) as caught:
        run_probe(
            [
                sys.executable,
                "-c",
                """\
import subprocess
import sys

worker = subprocess.Popen([sys.executable, "-c", '''
import fcntl
import signal
import sys
from pathlib import Path

def expire(*args):
    Path(sys.argv[2]).touch()
    raise SystemExit("the outer watchdog left this worker running")

with open(sys.argv[1], "w") as lease:
    fcntl.flock(lease, fcntl.LOCK_EX)
    signal.signal(signal.SIGALRM, expire)
    signal.alarm(25)
    print("ready", flush=True)
    while True:
        pass
''', *sys.argv[1:]])
worker.wait()
""",
                str(lease),
                str(self_exit),
            ],
            timeout=40,
        )
    assert caught.value is failure
    assert waits == [40, support._PROBE_CLEANUP_TIMEOUT]
    # Neither process can hold the inherited output pipes or the lease
    # after run_probe has drained the pipes and reaped the direct child.
    with lease.open() as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not self_exit.exists(), "the worker exited on its own watchdog"


def test_probe_closes_pipes_and_reaps_when_cleanup_drain_stalls(monkeypatch):
    failure = RuntimeError("initial wait failed")
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    monkeypatch.setattr(support.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(support, "_kill_probe_tree", lambda pid: process.kill())
    waits = []

    def communicate(*, timeout):
        waits.append(timeout)
        if len(waits) == 1:
            raise failure
        raise subprocess.TimeoutExpired("drain", timeout)

    wait = process.wait

    def reap(*, timeout):
        assert process.stdout.closed and process.stderr.closed
        waits.append(timeout)
        return wait(timeout=timeout)

    monkeypatch.setattr(process, "communicate", communicate)
    monkeypatch.setattr(process, "wait", reap)
    with pytest.raises(RuntimeError) as caught:
        run_probe(["probe"], timeout=40)
    assert caught.value is failure
    assert waits == [40, support._PROBE_CLEANUP_TIMEOUT, support._PROBE_CLEANUP_TIMEOUT]
    assert process.returncode is not None


def test_probe_uses_windows_process_tree_termination(monkeypatch):
    calls = []

    def record(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(support, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(support.subprocess, "run", record)
    support._kill_probe_tree(12345)
    assert calls == [
        (
            ["taskkill", "/F", "/T", "/PID", "12345"],
            {"capture_output": True, "timeout": support._PROBE_CLEANUP_TIMEOUT},
        )
    ]
