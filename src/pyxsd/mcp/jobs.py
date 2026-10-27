"""Durable, owner-scoped jobs with idempotent submission and cancellation."""

import asyncio
import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from typing import Protocol

import anyio

from .contracts import JobRef, JobResult, ToolFault
from .errors import ToolError
from .worker_protocol import Operation, WorkerReply
from .workspaces import Principal, WorkspaceStore

_TERMINAL = ("succeeded", "failed", "cancelled")


class JobExecutor(Protocol):
    async def run(self, operation: Operation, *, deadline: float) -> WorkerReply: ...


def _digest(workspace_id: str, operation: Operation) -> str:
    data = {
        "workspace": workspace_id,
        "operation": operation.model_dump(mode="json", exclude={"request_id"}),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class JobManager:
    def __init__(
        self,
        workspaces: WorkspaceStore,
        pool: JobExecutor,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.workspaces, self.pool = workspaces, pool
        self.state, self.limits = workspaces.state, workspaces.limits
        self.clock = clock
        self.monotonic = monotonic
        self._submission_lock = anyio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._scopes: dict[str, anyio.CancelScope] = {}
        self._finished: dict[str, anyio.Event] = {}
        self._closed = False
        self._interrupted = False

    async def _ensure_tasks(self) -> None:
        if self._closed:
            raise ToolError("jobs_closed", "Job manager is closed")

    def _schedule(self, job_id: str, owner_id: str, operation: Operation) -> None:
        operation = operation.model_copy(update={"owner_id": owner_id, "request_id": job_id})
        self._finished[job_id] = anyio.Event()
        self._tasks[job_id] = asyncio.create_task(self._execute(job_id, operation))

    @staticmethod
    def _ref(row: sqlite3.Row) -> JobRef:
        return JobRef(
            id=row["id"],
            state=row["state"],
            created_at=row["created"],
            expires_at=row["expires"] or None,
        )

    @staticmethod
    def _result(row: sqlite3.Row) -> JobResult:
        return JobResult(
            id=row["id"],
            workspace_id=row["workspace"],
            state=row["state"],
            created_at=row["created"],
            started_at=row["started"],
            completed_at=row["completed"],
            expires_at=row["expires"] or None,
            result=None if row["result"] is None else json.loads(row["result"]),
            fault=None if row["fault"] is None else ToolFault(**json.loads(row["fault"])),
        )

    async def _row(self, principal: Principal, job_id: str) -> sqlite3.Row | None:
        return await self.state.run(
            lambda db: db.execute(
                "SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, principal.owner_id)
            ).fetchone()
        )

    async def sweep(self) -> None:
        for job_id, task in tuple(self._tasks.items()):
            if task.done():
                self._tasks.pop(job_id)
                self._finished.pop(job_id, None)
        await self.state.run(
            lambda db: db.execute(
                "DELETE FROM jobs WHERE state IN ('succeeded','failed','cancelled') AND expires>0 AND expires<=?",
                (self.clock(),),
            ).close()
        )

    async def submit(
        self,
        principal: Principal,
        workspace_id: str,
        operation: Operation,
        *,
        idempotency_key: str,
    ) -> JobRef:
        principal.require("pyxsd:execute")
        if (
            not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 128
            or operation.snapshot.workspace_id != workspace_id
        ):
            raise ToolError("invalid_argument", "Invalid job or idempotency key")
        if self._closed:
            raise ToolError("jobs_closed", "Job manager is closed")
        digest = _digest(workspace_id, operation)
        await self.sweep()
        async with self._submission_lock, self.workspaces.lease(principal, workspace_id):
            await self._ensure_tasks()
            existing = await self.state.run(
                lambda db: db.execute(
                    "SELECT * FROM jobs WHERE owner=? AND idempotency=?",
                    (principal.owner_id, idempotency_key),
                ).fetchone()
            )
            if existing is not None:
                if existing["digest"] != digest:
                    raise ToolError("idempotency_conflict", "Job key is bound to different input")
                if existing["state"] == "queued" and existing["id"] not in self._tasks:
                    self._schedule(existing["id"], principal.owner_id, operation)
                return self._ref(existing)
            active = await self.state.run(
                lambda db: db.execute(
                    "SELECT count(*) FROM jobs WHERE owner=? AND state IN ('queued','running')",
                    (principal.owner_id,),
                ).fetchone()[0]
            )
            if active >= self.limits.active_jobs_per_owner:
                raise ToolError("quota_exceeded", "Owner active-job limit reached")
            queued = await self.state.run(
                lambda db: db.execute(
                    "SELECT count(*) FROM jobs WHERE state IN ('queued','running')"
                ).fetchone()[0]
            )
            if queued >= self.limits.workers + self.limits.queued_jobs:
                raise ToolError("quota_exceeded", "Global job queue limit reached", retryable=True)
            job_id = secrets.token_hex(16)
            created = self.clock()
            operation = operation.model_copy(
                update={"owner_id": principal.owner_id, "request_id": job_id}
            )
            encoded = operation.model_dump(mode="json")

            def insert(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO jobs(id,owner,workspace,operation,digest,idempotency,state,created) "
                    "VALUES(?,?,?,?,?,?, 'queued', ?)",
                    (
                        job_id,
                        principal.owner_id,
                        workspace_id,
                        json.dumps(encoded, sort_keys=True),
                        digest,
                        idempotency_key,
                        created,
                    ),
                )
                # Admission owns this pin until task cleanup, including time
                # queued before the task has entered its cancellation scope.
                db.execute("UPDATE workspaces SET leases=leases+1 WHERE id=?", (workspace_id,))

            await self.state.run(insert)
            self._schedule(job_id, principal.owner_id, operation)
            row = await self._row(principal, job_id)
            assert row is not None
            return self._ref(row)

    async def _transition(
        self,
        job_id: str,
        expected: tuple[str, ...],
        state: str,
        *,
        result: object = None,
        fault: ToolFault | None = None,
        require_not_cancelled: bool = False,
    ) -> None:
        now = self.clock()
        state_value, result_value = "?", "?"
        if require_not_cancelled:
            # Resolve completion and cancellation in the same SQLite update.
            # A rejected success update alone would strand the row as running.
            state_value = "CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END"
            result_value = "CASE WHEN cancel_requested=1 THEN NULL ELSE ? END"
        sql = (
            f"UPDATE jobs SET state={state_value}, started=COALESCE(started,?), "
            f"completed=?, expires=?, result={result_value}, fault=? "
            "WHERE id=? AND state IN (" + ",".join("?" for _ in expected) + ")"
        )
        terminal = state in _TERMINAL
        values: list[object] = [
            state,
            now,
            now if terminal else None,
            now + self.limits.job_result_seconds if terminal else 0,
            None if result is None else json.dumps(result),
            None if fault is None else json.dumps(fault.model_dump(mode="json")),
            job_id,
            *expected,
        ]
        await self.state.run(lambda db: db.execute(sql, values).close())

    async def _execute(self, job_id: str, operation: Operation) -> None:
        scope = anyio.CancelScope()
        self._scopes[job_id] = scope
        try:
            with scope:
                row = await self._row(Principal(operation.owner_id, frozenset()), job_id)
                if row is None or row["state"] != "queued" or row["cancel_requested"]:
                    return
                await self._transition(job_id, ("queued",), "running")
                if getattr(operation, "kind", None) in {"export-records", "compare-schemas"}:
                    seconds = self.limits.batch_seconds
                elif operation.role == "execution":
                    seconds = self.limits.transform_seconds
                else:
                    seconds = self.limits.compile_query_seconds
                deadline = self.monotonic() + seconds
                reply = await self.pool.run(operation, deadline=deadline)
            if scope.cancelled_caught:
                with anyio.CancelScope(shield=True):
                    if self._interrupted:
                        await self._transition(
                            job_id,
                            ("queued", "running"),
                            "failed",
                            fault=ToolFault(
                                code="interrupted",
                                message="Job was interrupted by coordinator shutdown",
                            ),
                        )
                    else:
                        await self._transition(job_id, ("queued", "running"), "cancelled")
            elif reply.status == "error":
                assert reply.fault is not None
                await self._transition(job_id, ("queued", "running"), "failed", fault=reply.fault)
            else:
                row = await self._row(Principal(operation.owner_id, frozenset()), job_id)
                if row is not None and row["cancel_requested"]:
                    await self._transition(job_id, ("queued", "running"), "cancelled")
                else:
                    await self._transition(
                        job_id,
                        ("running",),
                        "succeeded",
                        result=reply.result,
                        require_not_cancelled=True,
                    )
        except ToolError as exc:
            with anyio.CancelScope(shield=True):
                await self._transition(job_id, ("queued", "running"), "failed", fault=exc.fault)
        except Exception:
            with anyio.CancelScope(shield=True):
                await self._transition(
                    job_id,
                    ("queued", "running"),
                    "failed",
                    fault=ToolFault(
                        code="worker_internal",
                        message="Job failed without a verdict",
                        retryable=True,
                    ),
                )
        finally:
            with anyio.CancelScope(shield=True):
                await self.state.run(
                    lambda db: db.execute(
                        "UPDATE workspaces SET leases=leases-1 WHERE id=? AND leases>0",
                        (operation.snapshot.workspace_id,),
                    ).close()
                )
                await self.workspaces.sweep()
            self._scopes.pop(job_id, None)
            finished = self._finished.get(job_id)
            if finished is not None:
                finished.set()

    async def status(self, principal: Principal, job_id: str) -> JobResult:
        principal.require("pyxsd:read")
        await self.sweep()
        row = await self._row(principal, job_id)
        if row is None:
            raise ToolError("not_found", "Job not found")
        return self._result(row)

    async def cancel(self, principal: Principal, job_id: str) -> JobResult:
        principal.require("pyxsd:execute")
        await self.sweep()
        row = await self._row(principal, job_id)
        if row is None:
            raise ToolError("not_found", "Job not found")
        if row["state"] in _TERMINAL:
            return self._result(row)

        def mark(db: sqlite3.Connection) -> str:
            current = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            if current == "queued":
                db.execute(
                    "UPDATE jobs SET state='cancelled', completed=?, expires=? WHERE id=?",
                    (self.clock(), self.clock() + self.limits.job_result_seconds, job_id),
                )
            else:
                db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
            return current

        state = await self.state.run(mark)
        scope = self._scopes.get(job_id)
        if scope is not None:
            scope.cancel()
        if state == "queued":
            event = self._finished.get(job_id)
            if event is not None:
                event.set()
            await self._wait_for_task(job_id)
            return await self.status(principal, job_id)
        await self._wait_for_task(job_id)
        return await self.status(principal, job_id)

    async def _wait_for_task(self, job_id: str) -> None:
        event = self._finished.get(job_id)
        if event is not None:
            with anyio.fail_after(self.limits.batch_seconds):
                await event.wait()
            return
        task = self._tasks.get(job_id)
        if task is not None and self._scopes.get(job_id) is not None:
            with anyio.fail_after(self.limits.batch_seconds):
                await asyncio.gather(task, return_exceptions=True)

    async def aclose(self) -> None:
        async with self._submission_lock:
            if self._closed:
                return
            self._closed = True
            self._interrupted = True
            for scope in tuple(self._scopes.values()):
                scope.cancel()
            if self._tasks:
                tasks, self._tasks = tuple(self._tasks.values()), {}
                await asyncio.gather(*tasks, return_exceptions=True)

    async def __aenter__(self) -> "JobManager":
        await self._ensure_tasks()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
