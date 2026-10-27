"""Durable job transitions, cancellation and idempotency use real worker operations."""

import hashlib
import os
import signal
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_workers import make_snapshot as base_snapshot
from tests.mcp.test_workers import parse_operation

from pyxsd.mcp.config import Limits
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.state import State
from pyxsd.mcp.workspaces import Principal

SCOPES = frozenset({"pyxsd:read", "pyxsd:upload", "pyxsd:execute"})
TERMINAL = {"succeeded", "failed", "cancelled"}


@asynccontextmanager
async def harness(tmp_path: Path, limits: Limits | None = None):
    from pyxsd.mcp.jobs import JobManager
    from pyxsd.mcp.workers import WorkerPool
    from pyxsd.mcp.workspaces import WorkspaceStore

    limits = limits or Limits(workers=1)
    now = [1_000.0]
    with State(tmp_path / "state") as state:
        workspaces = WorkspaceStore(state, limits, clock=lambda: now[0])
        principal = Principal("alice", SCOPES)
        workspace = await workspaces.create(principal)
        snapshot = replace(source(tmp_path / "sources"), workspace_id=workspace.id)
        pool = WorkerPool(limits)
        jobs = JobManager(workspaces, pool, clock=lambda: now[0])
        try:
            yield jobs, workspaces, pool, principal, workspace, snapshot, now
        finally:
            await jobs.aclose()
            await pool.aclose()


def source(path: Path, *, large: bool = False):
    path.mkdir(parents=True, exist_ok=True)
    return base_snapshot(path, large=large)


def operation(workers, snapshot, request_id="request", document="document.xml"):
    return parse_operation(workers, snapshot, request_id, document)


async def terminal(jobs, job_id):
    with anyio.fail_after(10):
        while True:
            result = await jobs.status(Principal("alice", SCOPES), job_id)
            if result.state in TERMINAL:
                return result
            await anyio.sleep(0.01)


@pytest.mark.parametrize("transition", ["running", "succeeded", "failed", "cancelled"])
@pytest.mark.parametrize("committed", [False, True])
def test_transition_failure_preserves_committed_outcome(tmp_path, transition, committed):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            workspaces,
            pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            from pyxsd.mcp import workers
            from pyxsd.mcp.contracts import ToolFault
            from pyxsd.mcp.worker_protocol import WorkerReply

            original = jobs._transition
            pool_run = pool.run
            injected = False

            async def outcome(op, *, deadline):
                if transition == "failed":
                    return WorkerReply(
                        request_id=op.request_id,
                        role="clean",
                        status="error",
                        fault=ToolFault(code="test_failure", message="Worker failed"),
                    )
                reply = await pool_run(op, deadline=deadline)
                if transition == "cancelled":
                    await workspaces.state.run(
                        lambda db: db.execute(
                            "UPDATE jobs SET cancel_requested=1 WHERE id=?", (op.request_id,)
                        ).close()
                    )
                return reply

            pool.run = outcome

            async def interrupted(job_id, expected, state, **kwargs):
                nonlocal injected
                if state != transition or injected:
                    return await original(job_id, expected, state, **kwargs)
                injected = True
                if committed:
                    await original(job_id, expected, state, **kwargs)
                raise OSError("Injected transition interruption")

            jobs._transition = interrupted
            ref = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="transition"
            )
            result = await terminal(jobs, ref.id)
            assert injected
            if transition == "succeeded" and committed:
                assert result.state == "succeeded"
                assert result.result["root_present"] is True
                assert result.fault is None
            elif transition == "cancelled" and committed:
                assert result.state == "cancelled"
                assert result.result is None
                assert result.fault is None
            else:
                assert result.state == "failed"
                assert result.result is None
                assert result.fault.code == (
                    "test_failure" if transition == "failed" and committed else "worker_internal"
                )
            replay = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="transition"
            )
            assert replay.id == ref.id
            assert (await jobs.status(principal, replay.id)).state == result.state

    anyio.run(scenario)


def test_killed_worker_fails_job_without_automatic_resubmission(tmp_path, monkeypatch):
    from pyxsd.mcp import workers

    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            _workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            ready, proceed = anyio.Event(), anyio.Event()
            original = workers._Worker.run
            processes = []

            async def paused(worker, op):
                processes.append(worker.process)
                ready.set()
                await proceed.wait()
                return await original(worker, op)

            monkeypatch.setattr(workers._Worker, "run", paused)
            ref = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="killed"
            )
            with anyio.fail_after(10):
                await ready.wait()
                os.kill(processes[0].pid, signal.SIGKILL)
                await processes[0].wait()
                proceed.set()
                result = await terminal(jobs, ref.id)
            assert result.state == "failed"
            assert result.fault.code == "worker_died"
            assert result.result is None
            assert processes[0].returncode is not None
            replay = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="killed"
            )
            assert replay.id == ref.id
            assert (await jobs.status(principal, replay.id)).state == "failed"
            assert len(processes) == 1

    anyio.run(scenario)


def test_success_replay_conflict_scopes_and_closed(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            _workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            first = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="one"
            )
            result = await terminal(jobs, first.id)
            assert result.state == "succeeded"
            assert result.result["root_present"] is True
            duplicate = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="one"
            )
            assert duplicate.id == first.id
            with pytest.raises(ToolError, match="idempotency_conflict"):
                await jobs.submit(
                    principal,
                    workspace.id,
                    operation(workers, snapshot, "different", "other.xml"),
                    idempotency_key="one",
                )
            with pytest.raises(ToolError, match="not_found"):
                await jobs.status(Principal("bob", SCOPES), first.id)
            with pytest.raises(ToolError, match="forbidden"):
                await jobs.submit(
                    Principal("alice", frozenset({"pyxsd:read"})),
                    workspace.id,
                    operation(workers, snapshot),
                    idempotency_key="scope",
                )
            await jobs.aclose()
            with pytest.raises(ToolError, match="jobs_closed"):
                await jobs.submit(
                    principal, workspace.id, operation(workers, snapshot), idempotency_key="closed"
                )

    anyio.run(scenario)


def test_deadline_and_invalid_xml_are_failures_not_verdicts(tmp_path):
    from pyxsd.mcp import snapshots as snapshot_module

    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            _workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            jobs.monotonic = lambda: -1.0
            ref = await jobs.submit(
                principal,
                workspace.id,
                operation(workers, snapshot),
                idempotency_key="deadline",
            )
            result = await terminal(jobs, ref.id)
            assert result.state == "failed"
            assert result.fault.code == "deadline_exceeded"
            jobs.monotonic = time.monotonic

            content = b"<root>"
            path = snapshot.root / "bad.xml"
            path.write_bytes(content)
            bad_snapshot = replace(
                snapshot,
                files=(
                    *snapshot.files,
                    snapshot_module.SnapshotFile(
                        "bad.xml", hashlib.sha256(content).hexdigest(), len(content)
                    ),
                ),
            )
            malformed = await jobs.submit(
                principal,
                workspace.id,
                operation(workers, bad_snapshot, "bad", "bad.xml"),
                idempotency_key="malformed",
            )
            result = await terminal(jobs, malformed.id)
            assert result.state == "failed"
            assert result.fault.code == "invalid_xml"

    anyio.run(scenario)


def test_running_cancellation_waits_for_cleanup_and_never_publishes_success(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            workspaces,
            _pool,
            principal,
            workspace,
            _snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            large = replace(source(tmp_path / "cancel", large=True), workspace_id=workspace.id)
            ref = await jobs.submit(
                principal,
                workspace.id,
                operation(workers, large, "large", "large.xml"),
                idempotency_key="cancel",
            )
            with anyio.fail_after(5):
                while (await jobs.status(principal, ref.id)).state != "running":
                    await anyio.sleep(0.01)
            result = await jobs.cancel(principal, ref.id)
            assert result.state == "cancelled"
            assert result.completed_at is not None
            assert result.result is None
            await anyio.sleep(0.05)
            assert (await jobs.status(principal, ref.id)).state == "cancelled"
            await workspaces.release(principal, workspace.id)

    anyio.run(scenario)


def test_queued_cancellation_does_not_start_work(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            _workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            gate = anyio.Event()
            called = []
            original = jobs._execute
            original_run = _pool.run

            async def counted(op, *, deadline):
                called.append(op.request_id)
                return await original_run(op, deadline=deadline)

            _pool.run = counted

            async def gated(job_id, op):
                await gate.wait()
                return await original(job_id, op)

            jobs._execute = gated
            ref = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="queued"
            )
            assert (await jobs.status(principal, ref.id)).state == "queued"
            result = await jobs.cancel(principal, ref.id)
            assert result.state == "cancelled"
            gate.set()
            await anyio.sleep(0.05)
            assert called == []
            assert (await jobs.status(principal, ref.id)).state == "cancelled"

    anyio.run(scenario)


def test_owner_active_and_global_queue_limits(tmp_path):
    async def scenario():
        async with harness(tmp_path, Limits(workers=1, queued_jobs=1)) as (
            jobs,
            workspaces,
            _pool,
            principal,
            workspace,
            _snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            large = replace(source(tmp_path / "large", large=True), workspace_id=workspace.id)
            original_run = _pool.run

            async def gated(op, *, deadline):
                await anyio.sleep(0.2)
                return await original_run(op, deadline=deadline)

            _pool.run = gated
            first = await jobs.submit(
                principal,
                workspace.id,
                operation(workers, large, "first", "large.xml"),
                idempotency_key="first",
            )
            with pytest.raises(ToolError, match="quota_exceeded"):
                await jobs.submit(
                    principal,
                    workspace.id,
                    operation(workers, large, "second", "large.xml"),
                    idempotency_key="second",
                )
            others = []
            for name in ("bob", "carol"):
                owner = Principal(name, SCOPES)
                other_workspace = await workspaces.create(owner)
                other_snapshot = replace(
                    source(tmp_path / name, large=True), workspace_id=other_workspace.id
                )
                others.append((owner, other_workspace, other_snapshot))
            second = await jobs.submit(
                others[0][0],
                others[0][1].id,
                operation(workers, others[0][2], "bob", "large.xml"),
                idempotency_key="bob",
            )
            with pytest.raises(ToolError, match="quota_exceeded"):
                await jobs.submit(
                    others[1][0],
                    others[1][1].id,
                    operation(workers, others[1][2], "carol", "large.xml"),
                    idempotency_key="carol",
                )
            await jobs.cancel(principal, first.id)
            await jobs.cancel(others[0][0], second.id)

    anyio.run(scenario)


def test_restart_marks_interrupted_failed_without_rerunning_and_result_expiry(tmp_path):
    async def scenario():
        async with harness(tmp_path, Limits(job_result_seconds=1)) as (
            jobs,
            workspaces,
            pool,
            principal,
            workspace,
            snapshot,
            now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            ref = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="restart"
            )
            assert (await terminal(jobs, ref.id)).state == "succeeded"
            state = workspaces.state
            await state.run(
                lambda db: db.execute(
                    "UPDATE jobs SET state='running', completed=NULL, expires=0 WHERE id=?",
                    (ref.id,),
                ).close()
            )
            root = state.root
        with State(root) as recovered:
            from pyxsd.mcp.workspaces import WorkspaceStore

            workspaces = WorkspaceStore(recovered, Limits())
            pool = __import__("pyxsd.mcp.workers", fromlist=["WorkerPool"]).WorkerPool(Limits())
            from pyxsd.mcp.jobs import JobManager

            jobs = JobManager(workspaces, pool, clock=lambda: now[0])
            result = await jobs.status(principal, ref.id)
            assert result.state == "failed"
            assert result.fault.code == "interrupted"
            assert result.result is None
            await jobs.aclose()
            await pool.aclose()

    anyio.run(scenario)


def test_result_expiry_removes_terminal_metadata(tmp_path):
    async def scenario():
        async with harness(tmp_path, Limits(job_result_seconds=1)) as (
            jobs,
            _workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            ref = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="expiry"
            )
            assert (await terminal(jobs, ref.id)).state == "succeeded"
            now[0] += 2
            with pytest.raises(ToolError, match="not_found"):
                await jobs.status(principal, ref.id)

    anyio.run(scenario)


def test_invalid_submission_and_terminal_or_unknown_cancellation(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            _workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            for key in ("", "x" * 129, None):
                with pytest.raises(ToolError, match="invalid_argument"):
                    await jobs.submit(
                        principal, workspace.id, operation(workers, snapshot), idempotency_key=key
                    )
            with pytest.raises(ToolError, match="invalid_argument"):
                await jobs.submit(
                    principal,
                    "different-workspace",
                    operation(workers, snapshot),
                    idempotency_key="mismatch",
                )
            with pytest.raises(ToolError, match="not_found"):
                await jobs.cancel(principal, "missing")
            ref = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="terminal"
            )
            assert (await terminal(jobs, ref.id)).state == "succeeded"
            repeated = await jobs.cancel(principal, ref.id)
            assert repeated.state == "succeeded"
            await jobs.aclose()
            with pytest.raises(ToolError, match="jobs_closed"):
                await jobs.__aenter__()

    anyio.run(scenario)


def test_shutdown_interruption_and_generic_failure(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            workspaces,
            pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            large = replace(source(tmp_path / "shutdown", large=True), workspace_id=workspace.id)
            ref = await jobs.submit(
                principal,
                workspace.id,
                operation(workers, large, "shutdown", "large.xml"),
                idempotency_key="shutdown",
            )
            with anyio.fail_after(5):
                while (await jobs.status(principal, ref.id)).state != "running":
                    await anyio.sleep(0.01)
            await jobs.aclose()
            result = await jobs.status(principal, ref.id)
            assert result.state == "failed"
            assert result.fault.code == "interrupted"
            assert result.result is None

            from pyxsd.mcp.jobs import JobManager

            failure_manager = JobManager(workspaces, pool)
            original = pool.run

            async def fail(*_args, **_kwargs):
                raise RuntimeError("secret failure")

            pool.run = fail
            other = await failure_manager.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="failure"
            )
            result = await terminal(failure_manager, other.id)
            assert result.state == "failed"
            assert result.fault.code == "worker_internal"
            assert result.result is None
            pool.run = original
            await failure_manager.aclose()

    anyio.run(scenario)


def test_cancel_requested_reply_and_missing_completion_events(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            workspaces,
            pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            original = pool.run

            async def cancel_before_reply(op, *, deadline):
                await workspaces.state.run(
                    lambda db: db.execute("UPDATE jobs SET cancel_requested=1").close()
                )
                return await original(op, deadline=deadline)

            pool.run = cancel_before_reply
            ref = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="requested"
            )
            result = await terminal(jobs, ref.id)
            assert result.state == "cancelled"
            pool.run = original

            jobs._finished.clear()
            second = await jobs.submit(
                principal,
                workspace.id,
                operation(workers, snapshot),
                idempotency_key="missing-event",
            )
            result = await terminal(jobs, second.id)
            assert result.state == "succeeded"

            gate = anyio.Event()
            original_execute = jobs._execute

            async def gated(job_id, op):
                await gate.wait()
                return await original_execute(job_id, op)

            jobs._execute = gated
            queued = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="no-event"
            )
            jobs._finished.clear()
            assert (await jobs.cancel(principal, queued.id)).state == "cancelled"
            gate.set()

    anyio.run(scenario)


def test_running_cancellation_without_completion_event_and_context_manager(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            _workspaces,
            _pool,
            principal,
            workspace,
            _snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            large = replace(source(tmp_path / "no-event", large=True), workspace_id=workspace.id)
            ref = await jobs.submit(
                principal,
                workspace.id,
                operation(workers, large, "no-event", "large.xml"),
                idempotency_key="no-event",
            )
            with anyio.fail_after(5):
                while (await jobs.status(principal, ref.id)).state != "running":
                    await anyio.sleep(0.01)
            jobs._finished.clear()
            assert (await jobs.cancel(principal, ref.id)).state == "cancelled"
            async with jobs as manager:
                assert manager is jobs
            await jobs.aclose()

    anyio.run(scenario)


def test_cancellation_between_final_read_and_success_commit_is_terminal(tmp_path, monkeypatch):
    """A cancellation racing the success SQL must not strand a running row."""
    from pyxsd.mcp import workers

    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            _workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            ready, release = anyio.Event(), anyio.Event()
            transition = jobs._transition

            async def paused(job_id, expected, state, **kwargs):
                if state == "succeeded":
                    ready.set()
                    await release.wait()
                await transition(job_id, expected, state, **kwargs)

            monkeypatch.setattr(jobs, "_transition", paused)
            ref = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="commit-race"
            )
            results = []

            async def cancel():
                results.append(await jobs.cancel(principal, ref.id))

            with anyio.fail_after(10):
                await ready.wait()
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(cancel)
                    while not (await jobs._row(principal, ref.id))["cancel_requested"]:
                        await anyio.sleep(0)
                    release.set()
            assert results[0].state == "cancelled"
            assert results[0].result is None
            assert results[0].completed_at is not None
            assert (await jobs.status(principal, ref.id)).state == "cancelled"

    anyio.run(scenario)


def test_shutdown_during_admission_leaves_no_orphaned_job(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            ready, proceed = anyio.Event(), anyio.Event()
            original = jobs.sweep

            async def paused_sweep():
                await original()
                ready.set()
                await proceed.wait()

            jobs.sweep = paused_sweep

            async def submit():
                with pytest.raises(ToolError, match="jobs_closed"):
                    await jobs.submit(
                        principal,
                        workspace.id,
                        operation(workers, snapshot),
                        idempotency_key="shutdown-admission",
                    )

            with anyio.fail_after(5):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(submit)
                    await ready.wait()
                    await jobs.aclose()
                    proceed.set()
            count = await workspaces.state.run(
                lambda db: db.execute("SELECT count(*) FROM jobs").fetchone()[0]
            )
            assert count == 0

    anyio.run(scenario)


def test_completed_jobs_release_runtime_handles_but_keep_durable_results(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            _workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            refs = []
            for index in range(3):
                ref = await jobs.submit(
                    principal,
                    workspace.id,
                    operation(workers, snapshot),
                    idempotency_key=f"retained-{index}",
                )
                refs.append(ref)
                assert (await terminal(jobs, ref.id)).state == "succeeded"
            await anyio.lowlevel.checkpoint()
            with anyio.fail_after(5):
                while jobs._tasks:
                    await jobs.sweep()
                    await anyio.sleep(0.01)
            assert not jobs._finished
            for ref in refs:
                assert (await jobs.status(principal, ref.id)).result["root_present"] is True

    anyio.run(scenario)


@pytest.mark.parametrize("after_commit", [False, True])
def test_submission_retry_after_journal_failure_does_not_strand_or_duplicate_work(
    tmp_path, monkeypatch, after_commit
):
    from pyxsd.mcp import workers

    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            workspaces,
            pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            state_run, pool_run = workspaces.state.run, pool.run
            injected = False
            calls = []

            async def interrupted(callback):
                nonlocal injected
                if callback.__name__ == "insert" and not injected:
                    injected = True
                    if after_commit:
                        await state_run(callback)
                    raise OSError("Injected admission journal failure")
                return await state_run(callback)

            async def counted(op, *, deadline):
                calls.append(op.request_id)
                return await pool_run(op, deadline=deadline)

            monkeypatch.setattr(workspaces.state, "run", interrupted)
            monkeypatch.setattr(pool, "run", counted)
            op = operation(workers, snapshot)
            with pytest.raises(OSError, match="Injected admission"):
                await jobs.submit(principal, workspace.id, op, idempotency_key="retry-admission")
            ref = await jobs.submit(principal, workspace.id, op, idempotency_key="retry-admission")
            result = await terminal(jobs, ref.id)
            assert result.state == "succeeded"
            assert result.result["root_present"] is True
            assert calls == [ref.id]

    anyio.run(scenario)


@pytest.mark.parametrize("phase", ["queued", "running"])
@pytest.mark.parametrize("after_commit", [False, True])
def test_cancel_journal_failure_keeps_a_recoverable_state(
    tmp_path, monkeypatch, phase, after_commit
):
    from pyxsd.mcp import workers

    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            workspaces,
            pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            gate, ready = anyio.Event(), anyio.Event()
            execute, pool_run, state_run = jobs._execute, pool.run, workspaces.state.run
            injected = False

            async def queued(job_id, op):
                ready.set()
                await gate.wait()
                await execute(job_id, op)

            async def running(op, *, deadline):
                ready.set()
                await gate.wait()
                return await pool_run(op, deadline=deadline)

            async def interrupted(callback):
                nonlocal injected
                if callback.__name__ == "mark" and not injected:
                    injected = True
                    if after_commit:
                        await state_run(callback)
                    raise OSError("Injected cancellation journal failure")
                return await state_run(callback)

            monkeypatch.setattr(workspaces.state, "run", interrupted)
            if phase == "queued":
                monkeypatch.setattr(jobs, "_execute", queued)
            else:
                monkeypatch.setattr(pool, "run", running)
            ref = await jobs.submit(
                principal,
                workspace.id,
                operation(workers, snapshot),
                idempotency_key="cancel-fault",
            )
            with anyio.fail_after(5):
                await ready.wait()
                with pytest.raises(OSError, match="Injected cancellation"):
                    await jobs.cancel(principal, ref.id)
                gate.set()
                result = await terminal(jobs, ref.id)
            assert injected
            assert result.state == ("cancelled" if after_commit else "succeeded")
            if after_commit:
                assert result.result is None
            else:
                assert result.result["root_present"] is True

    anyio.run(scenario)


def test_queued_job_pins_its_managed_snapshot_until_execution_finishes(tmp_path, monkeypatch):
    from pyxsd.mcp import workers
    from pyxsd.mcp.safe_io import AdmittedFile
    from pyxsd.mcp.snapshot_store import SnapshotStore

    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            managed = await SnapshotStore(workspaces).put_bytes(
                principal,
                workspace.id,
                [
                    AdmittedFile(item.path, (snapshot.root / item.path).read_bytes())
                    for item in snapshot.files
                ],
                expected_revision=None,
            )
            gate = anyio.Event()
            execute = jobs._execute

            async def queued(job_id, op):
                await gate.wait()
                await execute(job_id, op)

            monkeypatch.setattr(jobs, "_execute", queued)
            ref = await jobs.submit(
                principal, workspace.id, operation(workers, managed), idempotency_key="pin-queued"
            )
            try:
                await workspaces.release(principal, workspace.id)
                assert managed.root.exists(), "Queued job lost its pinned snapshot"
            finally:
                gate.set()
            result = await terminal(jobs, ref.id)
            assert result.state == "succeeded"
            assert result.result["root_present"] is True
            await jobs._wait_for_task(ref.id)
            assert not managed.root.exists()

    anyio.run(scenario)


def test_active_workspace_release_waits_for_job_cleanup(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            workspaces,
            _pool,
            principal,
            workspace,
            _snapshot,
            _now,
        ):
            workers = __import__("pyxsd.mcp.workers", fromlist=["x"])
            large = replace(source(tmp_path / "release", large=True), workspace_id=workspace.id)
            ref = await jobs.submit(
                principal,
                workspace.id,
                operation(workers, large, "release", "large.xml"),
                idempotency_key="release",
            )
            with anyio.fail_after(5):
                while (await jobs.status(principal, ref.id)).state != "running":
                    await anyio.sleep(0.01)
            await workspaces.release(principal, workspace.id)
            assert (workspaces.state.root / "workspaces" / workspace.id).exists()
            result = await jobs.cancel(principal, ref.id)
            assert result.state == "cancelled"
            assert not (workspaces.state.root / "workspaces" / workspace.id).exists()

    anyio.run(scenario)
