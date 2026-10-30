"""Long-lived clean subprocesses with bounded JSON IPC and lifecycle enforcement."""

import hashlib
import json
import math
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import anyio
from anyio.abc import ByteReceiveStream, Process

from pyxsd import __version__

from .captures import CaptureDependency, CaptureFile, InputCapture
from .config import Limits
from .contracts import FileRef, SchemaRef, WireModel
from .errors import ToolError
from .worker_protocol import (
    BatchParseOperation,
    CaptureFileMessage,
    CaptureMessage,
    ComparisonOperation,
    DescribeOperation,
    DiscoverOperation,
    ExportOperation,
    FindOperation,
    ModelOperation,
    Operation,
    ParseOperation,
    QueryOperation,
    RecordsOperation,
    StructuralDiffOperation,
    WorkerConfig,
    WorkerReply,
    decode_frame,
    encode_frame,
)

WORKER_BOOTSTRAP = (
    "import os,runpy;"
    "__import__('coverage').process_startup() if "
    "os.getenv('COVERAGE_PROCESS_CONFIG') or os.getenv('COVERAGE_PROCESS_START') "
    "else None;"
    "runpy.run_module('pyxsd.mcp.worker_main',run_name='__main__')"
)


def capture_to_wire(capture: InputCapture) -> CaptureMessage:
    return CaptureMessage(
        root=str(capture.root),
        files=tuple(
            CaptureFileMessage(path=item.path, digest=item.digest, size=item.size)
            for item in capture.files
        ),
    )


def capture_from_wire(message: CaptureMessage) -> InputCapture:
    return InputCapture(
        "source",
        0,
        Path(message.root),
        tuple(CaptureFile(item.path, item.digest, item.size) for item in message.files),
    )


async def _receive_frame(stream: ByteReceiveStream, max_bytes: int) -> bytes:
    """Read exactly one bounded newline frame, rejecting trailing/flooded output."""
    data = bytearray()
    while True:
        try:
            chunk = await stream.receive(min(65536, max_bytes + 1 - len(data)))
        except (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
            raise ToolError(
                "worker_died", "Clean worker ended without a verdict", retryable=True
            ) from exc
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ToolError("ipc_limit", "Worker reply exceeds its byte limit")
        newline = data.find(b"\n")
        if newline >= 0:
            if newline != len(data) - 1:
                raise ToolError("ipc_protocol", "Worker sent trailing protocol bytes")
            return bytes(data)


class _Worker:
    def __init__(self, process: Process, limits: Limits) -> None:
        self.process, self.limits = process, limits

    @classmethod
    async def start(
        cls,
        limits: Limits,
        *,
        role: Literal["clean", "execution"],
    ) -> "_Worker":
        environment = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
        }
        # Forward only coverage's explicit test instrumentation, never the
        # coordinator's general environment or credentials.
        for name in ("COVERAGE_PROCESS_START", "COVERAGE_FILE"):
            if name in os.environ:
                environment[name] = str(Path(os.environ[name]).resolve())
        for name in ("COVERAGE_PROCESS_CONFIG", "COVERAGE_RUN"):
            if name in os.environ:
                environment[name] = os.environ[name]
        process = await anyio.open_process(
            (sys.executable, "-I", "-u", "-c", WORKER_BOOTSTRAP, "--role", role),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=os.path.sep,
            env=environment,
            start_new_session=True,
        )
        worker = cls(process, limits)
        try:
            config = WorkerConfig(role=role, limits=limits, processor_version=__version__)
            reply = await worker._exchange(config, request_id="bootstrap", role=role)
            if reply.status != "ok" or reply.result != {"ready": True}:
                raise ToolError(
                    "worker_startup", "Clean worker rejected its configuration", retryable=True
                )
            return worker
        except BaseException:
            with anyio.CancelScope(shield=True):
                await worker.close(graceful=False)
            raise

    async def _exchange(
        self, message: WireModel, *, request_id: str, role: Literal["clean", "execution"]
    ) -> WorkerReply:
        if self.process.stdin is None or self.process.stdout is None:
            raise ToolError("worker_died", "Clean worker streams are unavailable", retryable=True)
        frame = encode_frame(message, max_bytes=self.limits.request_bytes)
        try:
            await self.process.stdin.send(frame)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError) as exc:
            raise ToolError("worker_died", "Clean worker is unavailable", retryable=True) from exc
        data = await _receive_frame(self.process.stdout, self.limits.output_bytes)
        return decode_frame(
            data,
            WorkerReply,
            max_bytes=self.limits.output_bytes,
            expected={"request_id": request_id, "role": role},
        )

    async def run(self, operation: Operation) -> WorkerReply:
        return await self._exchange(operation, request_id=operation.request_id, role=operation.role)

    async def close(self, *, graceful: bool) -> None:
        """Drain on recycle; always terminate before returning on failure/cancel."""
        if self.process.returncode is None and graceful and self.process.stdin is not None:
            try:
                await self.process.stdin.aclose()
                with anyio.fail_after(2):
                    await self.process.wait()
            except (TimeoutError, OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
                pass
        if self.process.returncode is None:
            try:
                self.process.terminate()
                with anyio.fail_after(1):
                    await self.process.wait()
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
            except ProcessLookupError:
                pass
        await self.process.aclose()


@dataclass
class _Lane:
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    worker: _Worker | None = None


class WorkerPool:
    """Route related operations to one serial clean worker and bound queueing.

    Deadlines are absolute ``time.monotonic()`` values. Every lane owns a separate
    SchemaCache inside its subprocess. This local runtime is not a security sandbox.
    """

    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self._lanes = tuple(_Lane() for _ in range(limits.workers))
        self._slots = anyio.CapacityLimiter(limits.workers + limits.worker_queue)
        self._closed = False
        self._inflight = 0

    @property
    def inflight(self) -> int:
        return self._inflight

    def _lane(self, operation: Operation) -> _Lane:
        route: object
        if isinstance(
            operation, (ParseOperation, DescribeOperation, FindOperation, QueryOperation)
        ):
            schema = operation.schema_ref
            route = None if schema is None else schema.model_dump(mode="json")
        elif isinstance(operation, DiscoverOperation):
            route = [ref.model_dump(mode="json") for ref in operation.refs]
        else:
            route = None
        identity = json.dumps(
            [route, [(item.path, item.digest, item.size) for item in operation.capture.files]],
            sort_keys=True,
        ).encode()
        index = int.from_bytes(hashlib.sha256(identity).digest()[:8]) % len(self._lanes)
        return self._lanes[index]

    async def _ensure(self, lane: _Lane, operation: Operation) -> _Worker:
        if lane.worker is not None and lane.worker.process.returncode is None:
            return lane.worker
        if lane.worker is not None:
            await self._retire(lane, lane.worker, graceful=False)
        lane.worker = await _Worker.start(self.limits, role=operation.role)
        return lane.worker

    def _prepare(self, operation: Operation) -> Operation:
        return operation

    async def _retire(self, lane: _Lane, worker: _Worker, *, graceful: bool) -> None:
        if lane.worker is worker:
            lane.worker = None
        with anyio.CancelScope(shield=True):
            await worker.close(graceful=graceful)

    async def run(self, operation: Operation, *, deadline: float) -> WorkerReply:
        if self._closed:
            raise ToolError("worker_closed", "Worker pool is closed")
        if (
            not isinstance(
                operation,
                (
                    ParseOperation,
                    BatchParseOperation,
                    DescribeOperation,
                    FindOperation,
                    QueryOperation,
                    DiscoverOperation,
                    StructuralDiffOperation,
                    ModelOperation,
                    RecordsOperation,
                    ExportOperation,
                    ComparisonOperation,
                ),
            )
            or operation.role != "clean"
        ):
            raise ToolError("invalid_argument", "Unsupported clean-worker operation")
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ToolError("invalid_argument", "Worker deadline must be finite")
        lane = self._lane(operation)
        worker = None
        completed = False
        acquired = False
        try:
            try:
                self._slots.acquire_nowait()
                acquired = True
            except anyio.WouldBlock as exc:
                raise ToolError("queue_overflow", "Worker queue is full", retryable=True) from exc
            self._inflight += 1
            with anyio.fail_after(max(0.0, deadline - time.monotonic())):
                async with lane.lock:
                    if self._closed:
                        raise ToolError("worker_closed", "Worker pool is closed")
                    worker = await self._ensure(lane, operation)
                    reply = await worker.run(self._prepare(operation))
                    completed = True
                    if reply.operations >= self.limits.worker_operations:
                        await self._retire(lane, worker, graceful=True)
                    return reply
        except TimeoutError as exc:
            if worker is not None:
                await self._retire(lane, worker, graceful=False)
            raise ToolError(
                "deadline_exceeded", "Worker operation exceeded its deadline", retryable=True
            ) from exc
        except BaseException:
            if worker is not None and not completed:
                await self._retire(lane, worker, graceful=False)
            raise
        finally:
            if acquired:
                self._slots.release()
                self._inflight -= 1

    async def discover(
        self, capture: InputCapture, refs: tuple[FileRef | SchemaRef, ...]
    ) -> tuple[CaptureDependency, ...]:
        """B2 discovery adapter: all XML parsing remains in the clean subprocess."""
        operation = DiscoverOperation(
            request_id=secrets.token_hex(16),
            capture=capture_to_wire(capture),
            refs=refs,
        )
        reply = await self.run(
            operation, deadline=time.monotonic() + self.limits.compile_query_seconds
        )
        if reply.status == "error":
            assert reply.fault is not None
            raise ToolError(
                reply.fault.code,
                reply.fault.message,
                retryable=reply.fault.retryable,
                details=reply.fault.details,
            )
        if not isinstance(reply.result, dict) or not isinstance(
            reply.result.get("dependencies"), list
        ):
            raise ToolError("ipc_protocol", "Worker returned malformed dependencies")
        items = cast(list[dict[str, Any]], reply.result["dependencies"])
        return tuple(CaptureDependency(**item) for item in items)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for lane in self._lanes:
            async with lane.lock:
                if lane.worker is not None:
                    await self._retire(lane, lane.worker, graceful=True)

    async def __aenter__(self) -> "WorkerPool":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
