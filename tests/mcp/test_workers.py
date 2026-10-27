"""Real clean-worker subprocesses preserve verdicts and lifecycle bounds."""

import hashlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio

from pyxsd.mcp import snapshots
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.errors import ToolError

XSD = b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="root"><xs:complexType><xs:sequence><xs:element name="item" type="xs:int" minOccurs="0" maxOccurs="unbounded"/></xs:sequence></xs:complexType></xs:element></xs:schema>'


def make_snapshot(tmp_path: Path, *, large: bool = False):
    files = {"schema.xsd": XSD, "document.xml": b"<root><item>3</item></root>"}
    if large:
        files["large.xml"] = b"<root>" + b"<item>4</item>" * 200_000 + b"</root>"
    manifest = []
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
        manifest.append(
            snapshots.SnapshotFile(name, hashlib.sha256(content).hexdigest(), len(content))
        )
    return snapshots.Snapshot("workspace", "revision", tmp_path, tuple(manifest))


def parse_operation(workers, snapshot, request_id="request", document="document.xml"):
    return workers.ParseOperation(
        request_id=request_id,
        snapshot=workers.snapshot_to_wire(snapshot),
        owner_id="owner",
        schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
        document=FileRef(path=document),
        hints=(),
    )


def worker_pid(pool, operation):
    lane = pool._lane(operation)
    assert lane.worker is not None
    return lane.worker.process.pid


def test_real_worker_parses_discovers_and_recycles(tmp_path):
    from pyxsd.mcp import workers

    snapshot = make_snapshot(tmp_path)

    async def scenario():
        pool = workers.WorkerPool(Limits(workers=1, worker_operations=2))
        operation = parse_operation(workers, snapshot)
        try:
            first = await pool.run(operation, deadline=time.monotonic() + 10)
            assert first.status == "ok"
            assert first.result["root_present"] is True
            assert first.result["report"]["counts"] == {"error": 0, "warning": 0}
            pid = worker_pid(pool, operation)
            second = await pool.run(
                parse_operation(workers, snapshot, "second"), deadline=time.monotonic() + 10
            )
            assert second.operations == 2
            assert pool._lane(operation).worker is None
            await pool.run(
                parse_operation(workers, snapshot, "third"), deadline=time.monotonic() + 10
            )
            assert worker_pid(pool, operation) != pid
            dependencies = await pool.discover(
                snapshot, (SchemaRef(source=FileRef(path="schema.xsd")),)
            )
            assert [(item.path, item.digest is not None) for item in dependencies] == [
                ("schema.xsd", True)
            ]
        finally:
            await pool.aclose()
        with pytest.raises(ToolError, match="worker_closed"):
            await pool.run(operation, deadline=time.monotonic() + 1)

    anyio.run(scenario)


def test_worker_death_is_retryable_and_never_a_false_verdict(tmp_path):
    from pyxsd.mcp import workers

    snapshot = make_snapshot(tmp_path)

    async def scenario():
        pool = workers.WorkerPool(Limits(workers=1))
        operation = parse_operation(workers, snapshot)
        try:
            await pool.run(operation, deadline=time.monotonic() + 10)
            pid = worker_pid(pool, operation)
            os.kill(pid, signal.SIGKILL)
            with pytest.raises(ToolError, match="worker_died") as raised:
                await pool.run(operation, deadline=time.monotonic() + 2)
            assert raised.value.fault.retryable is True
            assert raised.value.fault.details.get("report") is None
            recovered = await pool.run(operation, deadline=time.monotonic() + 10)
            assert recovered.status == "ok"
            assert worker_pid(pool, operation) != pid
        finally:
            await pool.aclose()

    anyio.run(scenario)


def test_deadline_and_cancellation_terminate_work_and_allow_recovery(tmp_path):
    from pyxsd.mcp import workers

    snapshot = make_snapshot(tmp_path, large=True)

    async def scenario():
        pool = workers.WorkerPool(Limits(workers=1))
        small = parse_operation(workers, snapshot)
        large = parse_operation(workers, snapshot, "large", "large.xml")
        try:
            await pool.run(small, deadline=time.monotonic() + 10)
            lane = pool._lane(small)
            first_worker = lane.worker
            with pytest.raises(ToolError, match="deadline_exceeded"):
                await pool.run(large, deadline=time.monotonic() + 0.01)
            assert first_worker.process.returncode is not None
            assert lane.worker is None
            with anyio.CancelScope() as scope:

                async def cancel_inflight():
                    await anyio.sleep(0.01)
                    scope.cancel()

                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(cancel_inflight)
                    await pool.run(large, deadline=time.monotonic() + 30)
            assert scope.cancelled_caught
            recovered = await pool.run(small, deadline=time.monotonic() + 10)
            assert recovered.status == "ok"
            second_worker = lane.worker
            assert second_worker is not None
            assert second_worker.process.pid != first_worker.process.pid
        finally:
            await pool.aclose()

    anyio.run(scenario)


def test_queue_capacity_and_cancellation_release_slots(tmp_path):
    from pyxsd.mcp import workers

    snapshot = make_snapshot(tmp_path, large=True)

    async def scenario():
        pool = workers.WorkerPool(Limits(workers=1, queued_jobs=1))
        first = parse_operation(workers, snapshot, "first", "large.xml")
        second = parse_operation(workers, snapshot, "second")

        async def occupy(operation):
            await pool.run(operation, deadline=time.monotonic() + 30)

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(occupy, first)
                await anyio.wait_all_tasks_blocked()
                tasks.start_soon(occupy, second)
                await anyio.wait_all_tasks_blocked()
                with pytest.raises(ToolError, match="queue_overflow") as raised:
                    await pool.run(
                        parse_operation(workers, snapshot, "overflow"),
                        deadline=time.monotonic() + 1,
                    )
                assert raised.value.fault.retryable is True
                tasks.cancel_scope.cancel()
        assert pool.inflight == 0
        recovered = await pool.run(second, deadline=time.monotonic() + 10)
        assert recovered.status == "ok"
        await pool.aclose()

    anyio.run(scenario)


def test_snapshot_capture_uses_the_real_worker_discovery_adapter(tmp_path):
    from pyxsd.mcp import workers
    from pyxsd.mcp.snapshot_store import SnapshotStore
    from pyxsd.mcp.state import State
    from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

    source = tmp_path / "source"
    source.mkdir()
    make_snapshot(source)

    async def scenario():
        limits = Limits(workers=1)
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, limits)
            principal = stdio_principal()
            workspace = await workspaces.create(principal)
            pool = workers.WorkerPool(limits)
            store = SnapshotStore(
                workspaces, source_root=lambda _principal, _workspace: source, discovery=pool
            )
            refs = (
                FileRef(path="document.xml"),
                SchemaRef(source=FileRef(path="schema.xsd")),
            )
            try:
                snapshot = await store.capture(principal, workspace.id, refs)
                assert {item.path for item in snapshot.files} == {"document.xml", "schema.xsd"}
                assert await store.capture(principal, workspace.id, refs) == snapshot
            finally:
                await pool.aclose()

    anyio.run(scenario)


def test_repeated_parses_have_a_bounded_process_memory_plateau(tmp_path):
    from pyxsd.mcp import workers

    snapshot = make_snapshot(tmp_path)

    def rss_kib(pid):
        return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)]).strip())

    async def scenario():
        pool = workers.WorkerPool(Limits(workers=1, worker_operations=1000))
        operation = parse_operation(workers, snapshot)
        try:
            for _ in range(10):
                await pool.run(operation, deadline=time.monotonic() + 10)
            before = rss_kib(worker_pid(pool, operation))
            for _ in range(100):
                await pool.run(operation, deadline=time.monotonic() + 10)
            after = rss_kib(worker_pid(pool, operation))
            print(f"worker RSS after warmup={before} KiB after 100 parses={after} KiB")
            assert after <= before + 128 * 1024
        finally:
            await pool.aclose()

    anyio.run(scenario)
