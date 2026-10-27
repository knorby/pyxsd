"""Real Linux OCI containment gates; skipped unless explicitly enabled.

These tests require a Linux Docker-compatible daemon and a prebuilt pinned image.
They are intentionally separate from fake-runner unit coverage.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")

import anyio
from tests.mcp.test_workers import make_snapshot, parse_operation

from pyxsd.mcp import workers
from pyxsd.mcp.config import Limits
from pyxsd.mcp.runtimes.oci import OCICommandBuilder, OCIRuntime, OCISettings

pytestmark = pytest.mark.skipif(
    os.environ.get("PYXSD_RUN_MCP_OCI") != "1"
    or not sys.platform.startswith("linux")
    or not os.environ.get("PYXSD_MCP_OCI_IMAGE"),
    reason="set PYXSD_RUN_MCP_OCI=1, PYXSD_MCP_OCI_IMAGE and run on Linux",
)


def builder(tmp_path: Path):
    image = os.environ["PYXSD_MCP_OCI_IMAGE"]
    settings = OCISettings(
        image=image,
        memory_bytes=64 * 2**20,
        scratch_bytes=2 * 2**20,
        output_bytes=2**20,
        pids=16,
    )
    return OCICommandBuilder(settings, allowed_roots=(tmp_path,))


def run_probe(tmp_path: Path, code: str, *, timeout=20):
    name = f"pyxsd-containment-{os.getpid()}-{time.monotonic_ns()}"
    argv = builder(tmp_path).run_argv(
        tmp_path, role="execution", name=name, command=("python", "-I", "-c", code)
    )
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    finally:
        subprocess.run(("docker", "rm", "-f", name), capture_output=True, timeout=10)


def test_network_and_unmounted_secrets_are_unavailable(tmp_path):
    code = """
import pathlib, socket
failed = False
try:
    socket.create_connection(('10.255.255.1', 81), timeout=1)
except OSError:
    failed = True
assert failed, 'network unexpectedly available'
assert not pathlib.Path('/host-secret').exists()
assert not pathlib.Path('/workspace-sibling').exists()
try:
    pathlib.Path('/etc/shadow').read_bytes()
except (PermissionError, FileNotFoundError):
    pass
else:
    raise AssertionError('host shadow was readable')
"""
    result = run_probe(tmp_path, code)
    assert result.returncode == 0, result.stderr


def test_pid_memory_and_output_exhaustion_are_bounded(tmp_path):
    code = """
import os
children = []
try:
    while True:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        children.append(pid)
except OSError:
    pass
assert children, 'fork was never attempted'
try:
    with open('/output/fill', 'wb') as output:
        output.write(b'x' * (4 * 1024 * 1024))
except OSError:
    pass
else:
    raise AssertionError('output tmpfs exceeded its bound')
"""
    result = run_probe(tmp_path, code)
    assert result.returncode == 0, result.stderr
    memory = run_probe(tmp_path, "value = bytearray(256 * 1024 * 1024)")
    assert memory.returncode != 0


def test_timeout_cleans_container_and_descendants(tmp_path):
    name = f"pyxsd-timeout-{os.getpid()}-{time.monotonic_ns()}"
    code = "import subprocess,time; subprocess.Popen(['sleep','60']); time.sleep(60)"
    argv = builder(tmp_path).run_argv(
        tmp_path, role="execution", name=name, command=("python", "-I", "-c", code)
    )
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=1)
    finally:
        process.kill()
        process.wait(timeout=5)
        subprocess.run(("docker", "rm", "-f", name), capture_output=True, timeout=10)
    remaining = subprocess.check_output(("docker", "ps", "-aq", "--filter", f"name={name}"))
    assert not remaining.strip()


def test_real_worker_protocol_runs_inside_container(tmp_path):
    snapshot = make_snapshot(tmp_path)
    settings = builder(tmp_path).settings
    runtime = OCIRuntime(Limits(), settings, allowed_roots=(tmp_path,))

    async def scenario():
        try:
            reply = await runtime.run(
                parse_operation(workers, snapshot), deadline=time.monotonic() + 30
            )
            assert reply.status == "ok"
            assert reply.result["root_present"] is True
        finally:
            await runtime.aclose()

    anyio.run(scenario)
