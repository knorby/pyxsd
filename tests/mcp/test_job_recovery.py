"""A killed coordinator releases its lock and never replays interrupted work."""

import json
import select
import subprocess
import sys

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio

from pyxsd.mcp.config import Limits
from pyxsd.mcp.jobs import JobManager
from pyxsd.mcp.state import State
from pyxsd.mcp.workers import WorkerPool
from pyxsd.mcp.workspaces import Principal, WorkspaceStore


@pytest.mark.parametrize("phase", ["queued", "running"])
def test_killed_coordinator_recovers_without_reexecuting_jobs(tmp_path, phase):
    with (tmp_path / "child-stderr.log").open("w") as errors:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.mcp.job_crash_worker", str(tmp_path), phase],
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 15)
            assert ready, "Coordinator failed to reach crash boundary"
            line = process.stdout.readline()
            assert line, (tmp_path / "child-stderr.log").read_text()
            info = json.loads(line)
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
    assert process.returncode < 0
    assert (tmp_path / "executed").exists() is (phase == "running")

    async def recover():
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            pool = WorkerPool(Limits())
            jobs = JobManager(workspaces, pool)
            try:
                principal = Principal("alice", frozenset({"pyxsd:read"}))
                result = await jobs.status(principal, info["id"])
                assert result.state == "failed"
                assert result.fault.code == "interrupted"
                assert result.result is None
                assert result.completed_at is not None
                leases = await state.run(
                    lambda db: db.execute(
                        "SELECT leases FROM workspaces WHERE id=?", (info["workspace"],)
                    ).fetchone()[0]
                )
                assert leases == 0
                await anyio.sleep(0)
                assert not jobs._tasks
                assert (tmp_path / "executed").exists() is (phase == "running")
                if phase == "running":
                    assert (tmp_path / "executed").read_text() == "once"
            finally:
                await jobs.aclose()
                await pool.aclose()

    anyio.run(recover)
