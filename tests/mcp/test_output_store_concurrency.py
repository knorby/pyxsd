"""Real-process schedules for first publication and immutable open readers."""

import multiprocessing
import os
import sqlite3
import threading
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
import anyio

from pyxsd.mcp.config import Limits
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.output_store import OutputStore
from pyxsd.mcp.storage import Storage


def _writer(root, barrier, results, content):
    async def scenario():
        with Storage(Path(root)) as storage:
            store = OutputStore(storage, Limits())
            barrier.wait(timeout=15)
            ref = await store.put_bytes(
                content, name="a.txt", media_type="text/plain", kind="result"
            )
            results.put((ref.id, content))

    try:
        anyio.run(scenario)
    except BaseException:
        barrier.abort()
        raise


def _reader(root, identifier, opened, resume, results, before_open=False):
    async def scenario():
        with Storage(Path(root)) as storage:
            store = OutputStore(storage, Limits())
            original = store._verified

            def paused(row, descriptor, *, offset, length):
                opened.wait(timeout=15)
                resume.wait(timeout=15)
                return original(row, descriptor, offset=offset, length=length)

            if before_open:
                original_open = os.open

                def paused_open(path, *args, **kwargs):
                    if str(path).endswith("a.txt"):
                        opened.wait(timeout=15)
                        resume.wait(timeout=15)
                    return original_open(path, *args, **kwargs)

                os.open = paused_open
            else:
                store._verified = paused
            results.put(await store.read(identifier, offset=0, length=100))

    try:
        anyio.run(scenario)
    except BaseException:
        opened.abort()
        resume.abort()
        raise


def _finish(processes):
    for process in processes:
        process.join(timeout=20)
    try:
        assert [process.exitcode for process in processes] == [0] * len(processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def test_first_publication_race(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    root = tmp_path / "state"
    processes = [
        ctx.Process(target=_writer, args=(root, barrier, results, content))
        for content in (b"one", b"two")
    ]
    for process in processes:
        process.start()
    _finish(processes)
    refs = [results.get(timeout=5) for _ in processes]
    assert len({identifier for identifier, _ in refs}) == 2

    async def check():
        with Storage(root) as storage:
            store = OutputStore(storage, Limits())
            assert len((await store.list())[0]) == 2
            for identifier, content in refs:
                assert await store.read(identifier, offset=0, length=100) == content

    anyio.run(check)


def test_open_reader_survives_delete_without_blocking_publish(tmp_path):
    root = tmp_path / "state"

    async def seed():
        with Storage(root) as storage:
            return await OutputStore(storage, Limits()).put_bytes(
                b"original", name="a.txt", media_type="text/plain", kind="result"
            )

    ref = anyio.run(seed)
    ctx = multiprocessing.get_context("spawn")
    opened, resume, results = ctx.Barrier(2), ctx.Barrier(2), ctx.Queue()
    process = ctx.Process(target=_reader, args=(root, ref.id, opened, resume, results))
    process.start()
    try:
        opened.wait(timeout=15)

        async def mutate():
            with Storage(root, busy_timeout_ms=100) as storage:
                store = OutputStore(storage, Limits())
                await store.delete(ref.id)
                with pytest.raises(ToolError, match="not_found"):
                    await store.get(ref.id)
                await store.put_bytes(
                    b"new", name="new.txt", media_type="text/plain", kind="result"
                )

        anyio.run(mutate)
        resume.wait(timeout=15)
    finally:
        opened.abort()
        resume.abort()
        _finish([process])
    assert results.get(timeout=5) == b"original"


def test_descriptor_open_is_inside_the_delete_reservation(tmp_path):
    root = tmp_path / "state"

    async def seed():
        with Storage(root) as storage:
            return await OutputStore(storage, Limits()).put_bytes(
                b"original", name="a.txt", media_type="text/plain", kind="result"
            )

    ref = anyio.run(seed)
    ctx = multiprocessing.get_context("spawn")
    opened, resume, results = ctx.Barrier(2), ctx.Barrier(2), ctx.Queue()
    process = ctx.Process(target=_reader, args=(root, ref.id, opened, resume, results, True))
    process.start()
    try:
        opened.wait(timeout=15)

        async def blocked_delete():
            with (
                Storage(root, busy_timeout_ms=1) as storage,
                pytest.raises(ToolError, match="state_busy"),
            ):
                await OutputStore(storage, Limits()).delete(ref.id)

        anyio.run(blocked_delete)
        resume.wait(timeout=15)
    finally:
        opened.abort()
        resume.abort()
        _finish([process])
    assert results.get(timeout=5) == b"original"


def _delete(root, identifier, barrier):
    try:

        async def scenario():
            with Storage(Path(root)) as storage:
                barrier.wait(timeout=15)
                await OutputStore(storage, Limits()).delete(identifier)

        anyio.run(scenario)
    except BaseException:
        barrier.abort()
        raise


def test_concurrent_deletes_are_idempotent(tmp_path):
    root = tmp_path / "state"

    async def seed():
        with Storage(root) as storage:
            return await OutputStore(storage, Limits()).put_bytes(
                b"original", name="a.txt", media_type="text/plain", kind="result"
            )

    ref = anyio.run(seed)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    processes = [ctx.Process(target=_delete, args=(root, ref.id, barrier)) for _ in range(2)]
    for process in processes:
        process.start()
    _finish(processes)
    assert not ref.path.exists()


def test_busy_timeout_is_typed_and_retryable(tmp_path):
    root = tmp_path / "state"
    with Storage(root, busy_timeout_ms=1) as storage:
        storage._run(lambda db: db.execute("CREATE TABLE sample(value)").close())
        connection = sqlite3.connect(storage.format_root / "index.sqlite3", isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(ToolError, match="state_busy") as raised:
                storage._run(lambda db: True)
            assert raised.value.fault.retryable is True
        finally:
            connection.close()
        assert storage._run(lambda db: True) is True


def _crash(root, phase, identifier, ready):
    async def scenario():
        with Storage(Path(root)) as storage:
            store = OutputStore(storage, Limits())
            if phase == "publish":
                original = store._run

                def before_commit(operation, *, create=False):
                    if not create:
                        ready.wait(timeout=15)
                        os._exit(17)
                    return original(operation, create=create)

                store._run = before_commit
                await store.put_bytes(
                    b"orphan", name="a.txt", media_type="text/plain", kind="result"
                )
            else:

                def before_unlink(*args, **kwargs):
                    ready.wait(timeout=15)
                    os._exit(17)

                import pyxsd.mcp.output_store as module

                module.shutil.rmtree = before_unlink
                await store.delete(identifier)

    try:
        anyio.run(scenario)
    except BaseException:
        ready.abort()
        raise


@pytest.mark.parametrize("phase", ["publish", "delete"])
def test_crash_orphans_remain_unlisted_and_are_not_reconciled(tmp_path, phase):
    root = tmp_path / "state"

    async def seed():
        with Storage(root) as storage:
            return await OutputStore(storage, Limits()).put_bytes(
                b"original", name="seed.txt", media_type="text/plain", kind="result"
            )

    ref = anyio.run(seed)
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Barrier(2)
    process = ctx.Process(target=_crash, args=(root, phase, ref.id, ready))
    process.start()
    try:
        ready.wait(timeout=15)
        process.join(timeout=15)
        assert process.exitcode == 17
    finally:
        ready.abort()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    async def check():
        with Storage(root) as storage:
            store = OutputStore(storage, Limits())
            listed, _ = await store.list()
            assert [item.id for item in listed] == ([ref.id] if phase == "publish" else [])
            bodies = sorted(path.read_bytes() for path in storage.artifacts.glob("*/*"))
            assert bodies == ([b"original", b"orphan"] if phase == "publish" else [b"original"])
            fresh = await store.put_bytes(
                b"fresh", name="fresh.txt", media_type="text/plain", kind="result"
            )
            assert await store.read(fresh.id, offset=0, length=5) == b"fresh"

    anyio.run(check)


def test_verification_error_and_commit_error_close_reader_descriptor(tmp_path, monkeypatch):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, Limits())
            ref = await store.put_bytes(
                b"original", name="a.txt", media_type="text/plain", kind="result"
            )
            original = store._open_verified_source
            opened = []

            def track(db, identifier):
                row, descriptor = original(db, identifier)
                opened.append(descriptor)
                return row, descriptor

            monkeypatch.setattr(store, "_open_verified_source", track)
            ref.path.write_bytes(b"changed")
            with pytest.raises(ToolError, match="artifact_changed"):
                await store.get(ref.id)
            with pytest.raises(OSError):
                os.fstat(opened[-1])

            original_run = storage._run

            def commit_failure(operation, *, create=True):
                def fail(db):
                    operation(db)
                    raise sqlite3.OperationalError("commit failed")

                return original_run(fail, create=create)

            monkeypatch.setattr(storage, "_run", commit_failure)
            with pytest.raises(sqlite3.OperationalError, match="commit failed"):
                await store.get(ref.id)
            with pytest.raises(OSError):
                os.fstat(opened[-1])
            monkeypatch.undo()
            ref.path.write_bytes(b"original")
            assert await store.get(ref.id) == ref

    anyio.run(scenario)


def test_cancellation_waits_for_reader_cleanup(tmp_path, monkeypatch):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, Limits())
            ref = await store.put_bytes(
                b"original", name="a.txt", media_type="text/plain", kind="result"
            )
            started, release, descriptors = anyio.Event(), threading.Event(), []
            original = store._verified

            def blocked(row, descriptor, *, offset, length):
                descriptors.append(descriptor)
                anyio.from_thread.run_sync(started.set)
                assert release.wait(timeout=15)
                return original(row, descriptor, offset=offset, length=length)

            monkeypatch.setattr(store, "_verified", blocked)
            try:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(store.get, ref.id)
                    await started.wait()
                    tasks.cancel_scope.cancel()
                    release.set()
            finally:
                release.set()
            with pytest.raises(OSError):
                os.fstat(descriptors[0])
            monkeypatch.undo()
            assert await store.get(ref.id) == ref

    anyio.run(scenario)
