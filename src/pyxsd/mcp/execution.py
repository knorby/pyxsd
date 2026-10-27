"""Disposable trusted-local execution; not an operating-system sandbox."""

import hashlib
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import anyio

from pydantic import JsonValue

from .config import Limits
from .contracts import FileRef, SchemaRef, WireModel
from .errors import ToolError
from .snapshots import Snapshot
from .transforms import (
    PipelineTransformSpec,
    PythonTransformSpec,
    TransformSpec,
    normalize_transform,
    resolve_sources,
)
from .worker_protocol import SnapshotMessage, WorkerReply, decode_frame, encode_frame
from .workers import WORKER_BOOTSTRAP, snapshot_to_wire


class ExecutionRequest(WireModel):
    request_id: str
    snapshot: SnapshotMessage
    schema_ref: SchemaRef
    document: FileRef
    transform: TransformSpec
    sources: dict[str, str]
    output_directory: str
    limits: Limits


@dataclass(frozen=True)
class CapturedCandidate:
    content: bytes
    digest: str
    checkpoints: JsonValue
    process_returncode: int
    checkpoint_candidates: tuple[bytes, ...] = ()


def capture_candidate(directory_fd: int, limit: int, *, name: str = "candidate.xml") -> bytes:
    """Read one private regular inode after execution has stopped."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except OSError as exc:
        raise ToolError("unsafe_candidate", "Candidate is missing or unsafe") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ToolError("unsafe_candidate", "Candidate must be a private regular file")
        if metadata.st_size > limit:
            raise ToolError("output_limit", "Candidate exceeds its output budget")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ToolError("output_limit", "Candidate exceeds its output budget")
        return content
    finally:
        os.close(fd)


class TrustedLocalExecution:
    """Explicit local-only code execution with launching-user privileges.

    Never choose this backend as an isolated-runtime fallback. The caller owns
    authorization and must refuse it on HTTP before invoking this class.
    """

    def __init__(self, root: Path, limits: Limits) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root, self.limits = root, limits

    async def run(
        self,
        snapshot: Snapshot,
        schema_ref: SchemaRef,
        document: FileRef,
        transform: PythonTransformSpec | PipelineTransformSpec,
        *,
        registry: dict[str, str],
        deadline: float,
    ) -> CapturedCandidate:
        transform = normalize_transform(transform, self.limits)
        sources = (
            resolve_sources(transform, registry, self.limits)
            if isinstance(transform, PipelineTransformSpec)
            else {}
        )
        with tempfile.TemporaryDirectory(prefix="execution-", dir=self.root) as directory:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                request = ExecutionRequest(
                    request_id="execution",
                    snapshot=snapshot_to_wire(snapshot),
                    schema_ref=schema_ref,
                    document=document,
                    transform=transform,
                    sources=sources,
                    output_directory=directory,
                    limits=self.limits,
                )
                frame = encode_frame(request, max_bytes=self.limits.request_bytes)
                environment = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "LC_ALL": "C"}
                for name in ("COVERAGE_PROCESS_CONFIG", "COVERAGE_RUN"):
                    if name in os.environ:
                        environment[name] = os.environ[name]
                bootstrap = WORKER_BOOTSTRAP.replace(
                    "pyxsd.mcp.worker_main", "pyxsd.mcp.execution_main"
                )
                process = await anyio.open_process(
                    (sys.executable, "-I", "-u", "-c", bootstrap),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    cwd=directory,
                    start_new_session=True,
                )
                output = bytearray()
                try:
                    with anyio.fail_after(max(0, deadline - time.monotonic())):
                        assert process.stdin is not None and process.stdout is not None
                        await process.stdin.send(frame)
                        await process.stdin.aclose()
                        while True:
                            try:
                                chunk = await process.stdout.receive(
                                    min(65536, self.limits.request_bytes + 1 - len(output))
                                )
                            except anyio.EndOfStream:
                                break
                            output.extend(chunk)
                            if len(output) > self.limits.request_bytes:
                                raise ToolError("ipc_limit", "Execution stdout exceeds its budget")
                        await process.wait()
                except TimeoutError as exc:
                    raise ToolError("deadline_exceeded", "Transform exceeded its deadline") from exc
                finally:
                    with anyio.CancelScope(shield=True):
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        await process.aclose()
                reply = None
                if output:
                    reply = decode_frame(
                        bytes(output),
                        WorkerReply,
                        max_bytes=self.limits.request_bytes,
                        expected={"request_id": "execution", "role": "execution"},
                    )
                    if reply.status == "error":
                        assert reply.fault is not None
                        raise ToolError(reply.fault.code, reply.fault.message)
                content = capture_candidate(directory_fd, self.limits.output_bytes)
                if reply is None or process.returncode != 0:
                    raise ToolError("ipc_protocol", "Execution ended without a complete result")
                checkpoints = []
                remaining = self.limits.output_bytes - len(content)
                if isinstance(transform, PipelineTransformSpec):
                    for index, step in enumerate(transform.steps):
                        if step.checkpoint:
                            retained = capture_candidate(
                                directory_fd, remaining, name=f"checkpoint-{index}.xml"
                            )
                            remaining -= len(retained)
                            checkpoints.append(retained)
                return CapturedCandidate(
                    content,
                    hashlib.sha256(content).hexdigest(),
                    reply.result,
                    process.returncode,
                    tuple(checkpoints),
                )
            finally:
                os.close(directory_fd)
