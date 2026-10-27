"""OCI argv/preflight tests are not presented as containment evidence."""

import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")

import anyio
from tests.mcp.test_workers import make_snapshot, parse_operation

from pyxsd.mcp import workers
from pyxsd.mcp.config import Limits
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.worker_protocol import WorkerReply

IMAGE = "example/pyxsd-worker@sha256:" + "a" * 64


def settings(oci, **kwargs):
    return oci.OCISettings(**{"image": IMAGE, **kwargs})


def test_deterministic_hardened_argv_and_real_pinned_base(tmp_path):
    from pyxsd.mcp.runtimes import oci

    snapshot = tmp_path / "revision"
    snapshot.mkdir()
    builder = oci.OCICommandBuilder(settings(oci), allowed_roots=(tmp_path,))
    argv = builder.run_argv(snapshot, role="clean", name="worker-test")
    assert argv[:4] == ("docker", "run", "--rm", "--interactive")
    for flags in (
        ("--network", "none"),
        ("--read-only",),
        ("--user", "65532:65532"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges:true"),
        ("--security-opt", "seccomp=default"),
        ("--pids-limit", "64"),
        ("--memory", str(256 * 2**20)),
        ("--memory-swap", str(256 * 2**20)),
        ("--cpus", "1.0"),
        ("--init",),
        ("--stop-timeout", "1"),
        ("--workdir", "/workspace"),
    ):
        assert tuple(flags) in argv or all(part in argv for part in flags)
    mount = f"type=bind,source={snapshot.resolve()},target=/workspace,readonly"
    assert mount in argv
    assert any(f"size={64 * 2**20}" in part for part in argv)
    assert any(f"size={16 * 2**20}" in part for part in argv)
    assert "worker-test" in argv
    assert argv[-3:] == ("--role", "clean") or argv[-2:] == ("--role", "clean")
    dockerfile = (Path(__file__).parents[2] / "containers/mcp-worker/Dockerfile").read_text()
    assert (
        "python:3.13-slim@sha256:d3449c0493345868b09708d7056dce5edda3a75233ea86008fc80c934d355a10"
        in dockerfile
    )
    assert "USER 65532:65532" in dockerfile


def test_local_image_id_is_also_a_pinned_reference(tmp_path):
    from pyxsd.mcp.runtimes import oci

    image = "sha256:" + "b" * 64
    assert settings(oci, image=image).image == image
    runtime = oci.OCIRuntime(
        Limits(),
        settings(oci, image=image),
        allowed_roots=(tmp_path,),
        runner=Probe(image=image),
    )
    anyio.run(runtime.preflight)


@pytest.mark.parametrize("image", ["worker:latest", "worker@sha256:bad", "worker"])
def test_image_requires_a_full_digest(image):
    from pyxsd.mcp.runtimes import oci

    with pytest.raises(ToolError, match="image_digest"):
        settings(oci, image=image)


@pytest.mark.parametrize("outside", ["/etc", "/var/run/docker.sock", "/home/user/.ssh"])
def test_unapproved_socket_device_and_secret_mounts_are_refused(outside, tmp_path):
    from pyxsd.mcp.runtimes import oci

    builder = oci.OCICommandBuilder(settings(oci), allowed_roots=(tmp_path,))
    with pytest.raises(ToolError, match="mount_denied"):
        builder.run_argv(Path(outside), role="clean", name="denied")


@pytest.mark.parametrize(
    "options",
    [
        {"memory_bytes": 0},
        {"memory_bytes": True},
        {"scratch_bytes": 0},
        {"scratch_bytes": True},
        {"output_bytes": 0},
        {"output_bytes": True},
        {"pids": 0},
        {"pids": 65},
        {"pids": True},
        {"cpus": "bad"},
        {"cpus": "0"},
        {"user": "0:0"},
        {"user": "root"},
    ],
)
def test_invalid_limits_and_identity_are_refused(options):
    from pyxsd.mcp.runtimes import oci

    with pytest.raises(ToolError, match="invalid_argument"):
        settings(oci, **options)


def test_invalid_role_name_command_and_empty_mount_roots(tmp_path):
    from pyxsd.mcp.runtimes import oci

    with pytest.raises(ToolError, match="invalid_argument"):
        oci.OCICommandBuilder(settings(oci), allowed_roots=())
    builder = oci.OCICommandBuilder(settings(oci), allowed_roots=(tmp_path,))
    for options in (
        {"role": "host"},
        {"name": "bad/name"},
        {"command": ()},
        {"command": ("python", "")},
    ):
        arguments = {"role": "clean", "name": "worker", **options}
        with pytest.raises(ToolError, match="invalid_argument"):
            builder.run_argv(tmp_path, **arguments)


def test_outside_existing_directory_and_custom_command(tmp_path):
    from pyxsd.mcp.runtimes import oci

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    builder = oci.OCICommandBuilder(settings(oci), allowed_roots=(allowed,))
    with pytest.raises(ToolError, match="mount_denied"):
        builder.run_argv(outside, role="clean", name="denied")
    argv = builder.run_argv(
        allowed, role="execution", name="probe", command=("python", "-c", "pass")
    )
    assert argv[-3:] == ("python", "-c", "pass")


def test_symlink_and_missing_mounts_are_refused(tmp_path):
    from pyxsd.mcp.runtimes import oci

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    builder = oci.OCICommandBuilder(settings(oci), allowed_roots=(tmp_path,))
    with pytest.raises(ToolError, match="mount_denied"):
        builder.run_argv(link, role="clean", name="denied")
    with pytest.raises(ToolError, match="mount_denied"):
        builder.run_argv(tmp_path / "missing", role="clean", name="denied")


class Probe:
    def __init__(
        self,
        *,
        os_name="linux",
        security="name=seccomp,profile=default",
        inspect_rc=0,
        version_rc=0,
        info_rc=0,
        image_matches=True,
        image=IMAGE,
    ):
        self.os_name = os_name
        self.security = security
        self.inspect_rc = inspect_rc
        self.version_rc = version_rc
        self.info_rc = info_rc
        self.image_matches = image_matches
        self.image = image
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        if argv[:2] == ("docker", "version"):
            return subprocess.CompletedProcess(argv, self.version_rc, self.os_name + "\n", "")
        if argv[:2] == ("docker", "info"):
            return subprocess.CompletedProcess(argv, self.info_rc, self.security + "\n", "")
        if argv[:3] == ("docker", "image", "inspect"):
            output = self.image if self.image_matches else "sha256:" + "0" * 64
            return subprocess.CompletedProcess(argv, self.inspect_rc, output + "\n", "missing")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_preflight_checks_linux_seccomp_and_image_without_fallback(tmp_path):
    from pyxsd.mcp.runtimes import oci

    probe = Probe()
    runtime = oci.OCIRuntime(Limits(), settings(oci), allowed_roots=(tmp_path,), runner=probe)

    async def scenario():
        await runtime.preflight()
        await runtime.preflight()

    anyio.run(scenario)
    assert len(probe.calls) == 3
    for bad in (
        Probe(os_name="darwin"),
        Probe(version_rc=1),
        Probe(info_rc=1),
        Probe(security="name=apparmor"),
        Probe(security="name=seccomp,profile=unconfined"),
        Probe(inspect_rc=1),
        Probe(image_matches=False),
    ):
        failing = oci.OCIRuntime(Limits(), settings(oci), allowed_roots=(tmp_path,), runner=bad)
        with pytest.raises(ToolError, match="runtime_unavailable"):
            anyio.run(failing.preflight)


def test_runtime_reuses_snapshot_mount_replaces_it_and_cleans_containers(tmp_path, monkeypatch):
    from pyxsd.mcp.runtimes import oci

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    probe = Probe()
    runtime = oci.OCIRuntime(Limits(), settings(oci), allowed_roots=(tmp_path,), runner=probe)
    starts = []
    prepared = []

    class FakeWorker:
        def __init__(self, command):
            self.command = command
            self.process = type("Process", (), {"returncode": None})()

        async def run(self, operation):
            prepared.append(operation)
            return WorkerReply(
                request_id=operation.request_id,
                role=operation.role,
                status="ok",
                result={"ready": True},
                operations=1,
            )

        async def close(self, *, graceful):
            self.process.returncode = 0

    async def start(limits, *, role, command=None):
        starts.append(command)
        return FakeWorker(command)

    monkeypatch.setattr(workers._Worker, "start", start)

    async def scenario():
        snapshot = make_snapshot(first)
        operation = parse_operation(workers, snapshot)
        await runtime.run(operation, deadline=time.monotonic() + 5)
        await runtime.run(operation, deadline=time.monotonic() + 5)
        other = parse_operation(workers, make_snapshot(second), "other")
        await runtime.run(other, deadline=time.monotonic() + 5)
        await runtime.aclose()

    anyio.run(scenario)
    assert len(starts) == 2
    assert [item.snapshot.root for item in prepared] == ["/workspace"] * 3
    removed = [call for call, _ in probe.calls if call[:3] == ("docker", "rm", "-f")]
    assert len(removed) == 2


def test_default_runner_and_cleanup_failure(tmp_path, monkeypatch):
    from pyxsd.mcp.runtimes import oci

    called = {}

    def run(argv, **kwargs):
        called["argv"] = argv
        called.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(oci.subprocess, "run", run)
    assert oci._run(("docker", "version"), 2).returncode == 0
    assert called["timeout"] == 2 and called["check"] is False

    class FailedCleanup(Probe):
        def __call__(self, argv, timeout):
            if argv[:3] == ("docker", "rm", "-f"):
                return subprocess.CompletedProcess(argv, 1, "", "failed")
            return super().__call__(argv, timeout)

    runtime = oci.OCIRuntime(
        Limits(), settings(oci), allowed_roots=(tmp_path,), runner=FailedCleanup()
    )

    async def scenario():
        with pytest.raises(ToolError, match="containment_cleanup"):
            await runtime._remove("worker")

    anyio.run(scenario)


def test_retire_without_container_name_has_no_cleanup_call(tmp_path):
    from pyxsd.mcp.runtimes import oci

    probe = Probe()
    runtime = oci.OCIRuntime(Limits(), settings(oci), allowed_roots=(tmp_path,), runner=probe)

    class LocalWorker:
        container_name = None

        async def close(self, *, graceful):
            pass

    async def scenario():
        lane = runtime._lanes[0]
        lane.worker = None
        await runtime._retire(lane, LocalWorker(), graceful=False)

    anyio.run(scenario)
    assert not probe.calls


def test_start_failure_never_retries_without_containment(tmp_path, monkeypatch):
    from pyxsd.mcp.runtimes import oci

    probe = Probe()
    runtime = oci.OCIRuntime(Limits(), settings(oci), allowed_roots=(tmp_path,), runner=probe)

    async def start(limits, *, role, command=None):
        raise ToolError("worker_startup", "denied")

    monkeypatch.setattr(workers._Worker, "start", start)

    async def scenario():
        with pytest.raises(ToolError, match="worker_startup"):
            await runtime.run(
                parse_operation(workers, make_snapshot(tmp_path)),
                deadline=time.monotonic() + 5,
            )

    anyio.run(scenario)
    assert sum(call[:3] == ("docker", "rm", "-f") for call, _ in probe.calls) == 1
