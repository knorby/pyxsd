"""Pinned OCI workers with coordinator-owned mounts and forced cleanup.

Unit tests validate construction only. Real containment requires the explicit
Linux gate in tests/mcp/test_oci_containment.py; flags alone are not a safety claim.
"""

import re
import secrets
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anyio

from ..config import Limits
from ..errors import ToolError
from ..worker_protocol import Operation
from ..workers import WORKER_BOOTSTRAP, WorkerPool, _Lane, _Worker

Runner = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]


def _run(argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


@dataclass(frozen=True)
class OCISettings:
    image: str
    memory_bytes: int = 256 * 2**20
    scratch_bytes: int = 64 * 2**20
    output_bytes: int = 16 * 2**20
    pids: int = 64
    cpus: str = "1.0"
    user: str = "65532:65532"

    def __post_init__(self) -> None:
        if (
            re.fullmatch(
                r"(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9./_-]+@sha256:[0-9a-f]{64})",
                self.image,
            )
            is None
        ):
            raise ToolError("image_digest", "OCI image requires a full pinned digest")
        if (
            type(self.memory_bytes) is not int
            or self.memory_bytes <= 0
            or type(self.scratch_bytes) is not int
            or self.scratch_bytes <= 0
            or type(self.output_bytes) is not int
            or self.output_bytes <= 0
            or type(self.pids) is not int
            or not 1 <= self.pids <= 64
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", self.cpus) is None
            or float(self.cpus) <= 0
            or re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", self.user) is None
        ):
            raise ToolError("invalid_argument", "Invalid OCI resource limits or identity")


class OCICommandBuilder:
    def __init__(self, settings: OCISettings, *, allowed_roots: Sequence[Path]) -> None:
        if not allowed_roots:
            raise ToolError("invalid_argument", "OCI requires a managed mount root")
        self.settings = settings
        self.allowed_roots = tuple(root.resolve() for root in allowed_roots)

    def _source(self, snapshot_root: Path) -> Path:
        if snapshot_root.is_symlink() or not snapshot_root.is_dir():
            raise ToolError("mount_denied", "OCI mount must be an existing regular directory")
        source = snapshot_root.resolve()
        if not any(source == root or source.is_relative_to(root) for root in self.allowed_roots):
            raise ToolError("mount_denied", "OCI mount is outside coordinator-managed roots")
        return source

    def run_argv(
        self,
        snapshot_root: Path,
        *,
        role: Literal["clean", "execution"],
        name: str,
        command: tuple[str, ...] | None = None,
    ) -> tuple[str, ...]:
        source = self._source(snapshot_root)
        if role not in ("clean", "execution"):
            raise ToolError("invalid_argument", "Unsupported OCI worker role")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) is None:
            raise ToolError("invalid_argument", "Invalid OCI worker name")
        if command is not None and (
            not command or any(not isinstance(part, str) or not part for part in command)
        ):
            raise ToolError("invalid_argument", "Invalid OCI worker command")
        settings = self.settings
        argv = [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--user",
            settings.user,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--security-opt",
            "seccomp=default",
            "--pids-limit",
            str(settings.pids),
            "--memory",
            str(settings.memory_bytes),
            "--memory-swap",
            str(settings.memory_bytes),
            "--cpus",
            settings.cpus,
            "--init",
            "--stop-timeout",
            "1",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={settings.scratch_bytes}",
            "--tmpfs",
            f"/output:rw,noexec,nosuid,nodev,size={settings.output_bytes}",
            "--mount",
            f"type=bind,source={source},target=/workspace,readonly",
            "--workdir",
            "/workspace",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONIOENCODING=utf-8",
            settings.image,
        ]
        argv.extend(command or ("python", "-I", "-u", "-c", WORKER_BOOTSTRAP, "--role", role))
        return tuple(argv)


class OCIRuntime(WorkerPool):
    """WorkerPool using one hardened container per lane and snapshot mount."""

    def __init__(
        self,
        limits: Limits,
        settings: OCISettings,
        *,
        allowed_roots: Sequence[Path],
        runner: Runner | None = None,
    ) -> None:
        super().__init__(limits)
        self.builder = OCICommandBuilder(settings, allowed_roots=allowed_roots)
        self._runner = _run if runner is None else runner
        self._preflighted = False
        self._preflight_lock = anyio.Lock()

    async def _call(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return await anyio.to_thread.run_sync(self._runner, argv, 10.0)

    async def preflight(self) -> None:
        async with self._preflight_lock:
            if self._preflighted:
                return
            version = await self._call(("docker", "version", "--format", "{{.Server.Os}}"))
            info = await self._call(("docker", "info", "--format", "{{json .SecurityOptions}}"))
            image = await self._call(
                (
                    "docker",
                    "image",
                    "inspect",
                    self.builder.settings.image,
                    "--format",
                    "{{.Id}}",
                )
            )
            if (
                version.returncode != 0
                or version.stdout.strip() != "linux"
                or info.returncode != 0
                or "seccomp" not in info.stdout
                or "unconfined" in info.stdout
                or image.returncode != 0
                or self.builder.settings.image.rsplit("@", 1)[-1] not in image.stdout
            ):
                raise ToolError(
                    "runtime_unavailable",
                    "OCI Linux, seccomp, or pinned-image verification failed",
                )
            self._preflighted = True

    async def _remove(self, name: str) -> None:
        result = await self._call(("docker", "rm", "-f", name))
        if result.returncode != 0:
            raise ToolError(
                "containment_cleanup", "OCI worker could not be removed", retryable=True
            )

    async def _ensure(self, lane: _Lane, operation: Operation) -> _Worker:
        mount_root = operation.snapshot.root
        if lane.worker is not None and (
            lane.worker.process.returncode is not None
            or getattr(lane.worker, "mount_root", None) != mount_root
        ):
            await self._retire(lane, lane.worker, graceful=False)
        if lane.worker is not None:
            return lane.worker
        name = "pyxsd-" + secrets.token_hex(16)
        command = self.builder.run_argv(Path(mount_root), role=operation.role, name=name)
        await self.preflight()
        try:
            worker = await _Worker.start(self.limits, role=operation.role, command=command)
        except BaseException:
            await self._remove(name)
            raise
        worker.mount_root = mount_root
        worker.container_name = name
        lane.worker = worker
        return worker

    def _prepare(self, operation: Operation) -> Operation:
        snapshot = operation.snapshot.model_copy(update={"root": "/workspace"})
        return operation.model_copy(update={"snapshot": snapshot})

    async def _retire(self, lane: _Lane, worker: _Worker, *, graceful: bool) -> None:
        name = getattr(worker, "container_name", None)
        try:
            await super()._retire(lane, worker, graceful=graceful)
        finally:
            if name is not None:
                await self._remove(name)
