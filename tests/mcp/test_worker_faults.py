"""Defensive worker-boundary branches use explicit, deterministic fault injection."""

import io
import sys
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_workers import make_snapshot, parse_operation

from pyxsd import __version__
from pyxsd.exceptions import PyXSDError
from pyxsd.mcp import worker_main, workers
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, SchemaRef, ToolFault
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.runtimes.local import LocalRuntime
from pyxsd.mcp.worker_protocol import (
    Operation,
    WorkerConfig,
    WorkerReply,
    decode_operation,
    encode_frame,
)


class ReceiveStream:
    def __init__(self, *chunks, error=None):
        self.chunks = list(chunks)
        self.error = error

    async def receive(self, max_bytes):
        if self.chunks:
            return self.chunks.pop(0)
        if self.error is not None:
            raise self.error
        raise anyio.EndOfStream


class FakeProcess:
    def __init__(self, *, returncode=None, wait_timeout=False, missing=False):
        self.returncode = returncode
        self.wait_timeout = wait_timeout
        self.missing = missing
        self.stdin = SimpleNamespace(aclose=self._close_stream)
        self.stdout = object()
        self.closed = False
        self.killed = False

    async def _close_stream(self):
        pass

    async def wait(self):
        if self.wait_timeout and not self.killed:
            raise TimeoutError
        self.returncode = 0
        return 0

    def terminate(self):
        if self.missing:
            raise ProcessLookupError
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = 0

    async def aclose(self):
        self.closed = True


def config(**kwargs):
    return WorkerConfig(role="clean", limits=Limits(**kwargs), processor_version=__version__)


def base_operation(snapshot, role="clean"):
    return Operation(
        request_id="base",
        role=role,
        capture=workers.capture_to_wire(snapshot),
    )


def test_frame_reader_bounds_trailing_bytes_eof_and_snapshot_roundtrip(tmp_path):
    snapshot = make_snapshot(tmp_path)
    assert workers.capture_from_wire(workers.capture_to_wire(snapshot)).files == snapshot.files

    async def scenario():
        assert await workers._receive_frame(ReceiveStream(b"{", b"}\n"), 3) == b"{}\n"
        with pytest.raises(ToolError, match="ipc_limit"):
            await workers._receive_frame(ReceiveStream(b"12345"), 4)
        with pytest.raises(ToolError, match="ipc_protocol"):
            await workers._receive_frame(ReceiveStream(b"{}\n\n"), 4)
        with pytest.raises(ToolError, match="worker_died"):
            await workers._receive_frame(ReceiveStream(error=anyio.EndOfStream()), 4)

    anyio.run(scenario)


def test_worker_exchange_rejects_missing_and_broken_streams():
    async def scenario():
        process = SimpleNamespace(stdin=None, stdout=object())
        with pytest.raises(ToolError, match="worker_died"):
            await workers._Worker(process, Limits())._exchange(
                config(), request_id="bootstrap", role="clean"
            )

        class Sending:
            async def send(self, frame):
                raise anyio.BrokenResourceError

        process = SimpleNamespace(stdin=Sending(), stdout=object())
        with pytest.raises(ToolError, match="worker_died"):
            await workers._Worker(process, Limits())._exchange(
                config(), request_id="bootstrap", role="clean"
            )

    anyio.run(scenario)


def test_worker_start_rejects_invalid_ready_and_scrubs_environment(monkeypatch):
    captured = {}

    async def open_process(command, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    async def exchange(self, message, *, request_id, role):
        return WorkerReply(
            request_id=request_id,
            role=role,
            status="error",
            fault=ToolFault(code="denied", message="denied"),
        )

    closed = []

    async def close(self, *, graceful):
        closed.append(graceful)

    monkeypatch.setattr(anyio, "open_process", open_process)
    monkeypatch.setattr(workers._Worker, "_exchange", exchange)
    monkeypatch.setattr(workers._Worker, "close", close)
    monkeypatch.setenv("SECRET_TOKEN", "do-not-forward")
    monkeypatch.setenv("COVERAGE_RUN", "true")
    monkeypatch.setenv("COVERAGE_FILE", ".coverage-test")

    async def scenario():
        with pytest.raises(ToolError, match="worker_startup"):
            await workers._Worker.start(Limits(), role="clean")

    anyio.run(scenario)
    assert closed == [False]
    assert "SECRET_TOKEN" not in captured["env"]
    assert captured["env"]["COVERAGE_RUN"] == "true"
    assert captured["env"]["COVERAGE_FILE"].endswith("/.coverage-test")


def test_worker_close_escalates_and_tolerates_missing_process():
    async def scenario():
        process = FakeProcess(wait_timeout=True)
        await workers._Worker(process, Limits()).close(graceful=True)
        assert process.killed and process.closed
        missing = FakeProcess(missing=True)
        await workers._Worker(missing, Limits()).close(graceful=False)
        assert missing.closed

    anyio.run(scenario)


def test_pool_validation_stale_replacement_routing_and_close(tmp_path):
    snapshot = make_snapshot(tmp_path)
    pool = workers.WorkerPool(Limits())
    operation = parse_operation(workers, snapshot)
    base = base_operation(snapshot)
    assert pool._lane(base) in pool._lanes

    async def scenario():
        for invalid in (float("nan"), True, "later"):
            with pytest.raises(ToolError, match="invalid_argument"):
                await pool.run(operation, deadline=invalid)
        with pytest.raises(ToolError, match="invalid_argument"):
            await pool.run(base_operation(snapshot, "execution"), deadline=1)

        class Old:
            def __init__(self):
                self.process = SimpleNamespace(returncode=1)
                self.closed = False

            async def close(self, *, graceful):
                self.closed = True

        lane = pool._lane(operation)
        old = Old()
        lane.worker = old
        replacement = object()

        async def start(limits, *, role):
            return replacement

        original = workers._Worker.start
        workers._Worker.start = start
        try:
            assert await pool._ensure(lane, operation) is replacement
        finally:
            workers._Worker.start = original
        assert old.closed and lane.worker is replacement
        lane.worker = None

        closed_during_route = workers.WorkerPool(Limits())
        original_lane = closed_during_route._lane

        def close_while_routing(op):
            closed_during_route._closed = True
            return original_lane(op)

        closed_during_route._lane = close_while_routing
        with pytest.raises(ToolError, match="worker_closed"):
            await closed_during_route.run(operation, deadline=time.monotonic() + 10)
        assert closed_during_route.inflight == 0

        async with LocalRuntime(Limits()) as runtime:
            assert runtime.inflight == 0
        await runtime.aclose()

    anyio.run(scenario)


def test_timeout_while_queued_and_unrelated_retire_do_not_touch_a_worker(tmp_path):
    snapshot = make_snapshot(tmp_path)
    pool = workers.WorkerPool(Limits())
    operation = parse_operation(workers, snapshot)

    class Closing:
        def __init__(self):
            self.closed = False

        async def close(self, *, graceful):
            self.closed = True

    async def scenario():
        lane = pool._lane(operation)

        async def expect_timeout():
            with pytest.raises(ToolError, match="deadline_exceeded"):
                await pool.run(operation, deadline=time.monotonic() + 0.01)

        async with lane.lock, anyio.create_task_group() as tasks:
            tasks.start_soon(expect_timeout)
        assert pool.inflight == 0
        closing = Closing()
        await pool._retire(lane, closing, graceful=False)
        assert closing.closed
        await pool.aclose()

    anyio.run(scenario)


def test_discover_maps_worker_faults(tmp_path, monkeypatch):
    snapshot = make_snapshot(tmp_path)
    pool = workers.WorkerPool(Limits())

    async def failed(operation, *, deadline):
        return WorkerReply(
            request_id=operation.request_id,
            role="clean",
            status="error",
            fault=ToolFault(code="bad", message="failure", retryable=True, details={"code": 1}),
        )

    monkeypatch.setattr(pool, "run", failed)

    async def scenario():
        with pytest.raises(ToolError, match="bad") as raised:
            await pool.discover(snapshot, (FileRef(path="document.xml"),))
        assert raised.value.fault.retryable is True
        assert raised.value.fault.details == {"code": 1}

        async def malformed(operation, *, deadline):
            return WorkerReply(
                request_id=operation.request_id, role="clean", status="ok", result=[]
            )

        monkeypatch.setattr(pool, "run", malformed)
        with pytest.raises(ToolError, match="ipc_protocol"):
            await pool.discover(snapshot, (FileRef(path="document.xml"),))

    anyio.run(scenario)


def test_protocol_outcome_validation_and_unknown_operation(tmp_path):
    with pytest.raises(ValueError, match="successful"):
        WorkerReply(
            request_id="x",
            role="clean",
            status="ok",
            fault=ToolFault(code="x", message="x"),
        )
    with pytest.raises(ValueError, match="one fault"):
        WorkerReply(request_id="x", role="clean", status="error")
    with pytest.raises(ToolError, match="ipc_protocol"):
        decode_operation(b'{"role":"clean","kind":"unknown"}\n', max_bytes=100, role="clean")
    snapshot = make_snapshot(tmp_path)
    cache = __import__("pyxsd.mcp.cache", fromlist=["SchemaCache"]).SchemaCache(Limits())
    with pytest.raises(ToolError, match="invalid_operation"):
        worker_main._execute(base_operation(snapshot), cache, __version__)
    cache.close()


def test_worker_main_bounded_reads_and_execution_role():
    with pytest.raises(ToolError, match="ipc_limit"):
        worker_main._read_frame(io.BytesIO(b"12345"), 4)
    assert worker_main._read_frame(io.BytesIO(b"{}\n"), 4) == b"{}\n"
    assert worker_main.main(["--role", "execution"]) == 2


def test_worker_main_bootstrap_failure_restores_components(monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"")))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    assert worker_main.main(["--role", "clean"]) == 1


@pytest.mark.parametrize(
    "exception,code,fatal",
    [
        (ToolError("denied", "denied"), "denied", False),
        (PyXSDError("malformed"), "invalid_xml", False),
        (RuntimeError("secret details"), "worker_internal", True),
    ],
)
def test_worker_main_exception_replies(monkeypatch, tmp_path, exception, code, fatal):
    snapshot = make_snapshot(tmp_path)
    operation = parse_operation(workers, snapshot)
    incoming = encode_frame(config(), max_bytes=2 * 2**20) + encode_frame(
        operation, max_bytes=2 * 2**20
    )
    outgoing = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(incoming)))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=outgoing))

    def fail(*args):
        raise exception

    monkeypatch.setattr(worker_main, "_execute", fail)
    assert worker_main.main(["--role", "clean"]) == (1 if fatal else 0)
    lines = outgoing.getvalue().splitlines()
    assert len(lines) == 2
    assert code.encode() in lines[-1]
    assert operation.request_id.encode() in lines[-1]
    if code == "worker_internal":
        assert b"secret details" not in lines[-1]


def test_worker_main_oversized_operation_and_schema_error_mapping(monkeypatch, tmp_path):
    small = config(request_bytes=1024)
    incoming = encode_frame(small, max_bytes=2 * 2**20) + b"x" * 1025
    outgoing = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(incoming)))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=outgoing))
    assert worker_main.main(["--role", "clean"]) == 0
    assert b"ipc_limit" in outgoing.getvalue()

    snapshot = make_snapshot(tmp_path)
    operation = __import__(
        "pyxsd.mcp.worker_protocol", fromlist=["DiscoverOperation"]
    ).DiscoverOperation(
        request_id="discover",
        capture=workers.capture_to_wire(snapshot),
        refs=(SchemaRef(source=FileRef(path="schema.xsd")), FileRef(path="document.xml")),
    )
    incoming = encode_frame(config(), max_bytes=2 * 2**20) + encode_frame(
        operation, max_bytes=2 * 2**20
    )
    outgoing = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(incoming)))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=outgoing))

    def fail(*args):
        raise PyXSDError("bad schema")

    monkeypatch.setattr(worker_main, "_execute", fail)
    assert worker_main.main(["--role", "clean"]) == 0
    assert b"invalid_schema" in outgoing.getvalue()


def test_worker_rejects_page_limit_above_operator_budget(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    snapshot = make_snapshot(tmp_path)
    operation = parse_operation(workers, snapshot).model_copy(update={"issue_limit": 51})
    cache = SchemaCache(Limits(page_items=50))
    with pytest.raises(ToolError, match="invalid_argument"):
        worker_main._execute(operation, cache, __version__)
    cache.close()


def test_discovery_ignores_non_schema_references(tmp_path):
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.worker_protocol import DiscoverOperation

    snapshot = make_snapshot(tmp_path)
    operation = DiscoverOperation(
        request_id="files",
        capture=workers.capture_to_wire(snapshot),
        refs=(FileRef(path="document.xml"),),
    )
    cache = SchemaCache(Limits())
    assert worker_main._execute(operation, cache, __version__) == {"dependencies": []}
    cache.close()
