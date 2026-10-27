"""Real process death must retain one exact-byte publication receipt on retry."""

import hashlib
import json
import select
import subprocess
import sys

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("mcp")

import anyio

from pyxsd.mcp.config import Settings
from pyxsd.mcp.publication import CommitTransformRequest, PublicationService
from pyxsd.mcp.safe_io import SafeDirectory
from pyxsd.mcp.server import Services


@pytest.mark.parametrize("phase", ["after-intent", "after-staging", "after-receipt"])
def test_killed_publisher_retries_exact_bytes_without_execution(tmp_path, phase):
    with (tmp_path / "stderr.log").open("w") as errors:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.mcp.publication_crash_worker", str(tmp_path), phase],
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 20)
            assert ready, "Publisher did not reach crash boundary"
            line = process.stdout.readline()
            assert line, (tmp_path / "stderr.log").read_text()
            info = json.loads(line)
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
    assert process.returncode < 0

    async def recover():
        async with Services(Settings(state_dir=tmp_path / "state")) as services:
            publication = PublicationService(services.previews)
            request = CommitTransformRequest.model_validate(info["request"])
            receipt = await publication.commit(services.principal, request)
            assert await publication.commit(services.principal, request) == receipt
            snapshot = await services.snapshots.get(services.principal, info["workspace"])
            with SafeDirectory(snapshot.root) as directory:
                actual = directory.read("recovered.xml", max_bytes=8192).content
            assert hashlib.sha256(actual).hexdigest() == request.expected_candidate_digest
            count = await services.state.run(
                lambda db: db.execute(
                    "SELECT count(*) FROM snapshots WHERE status='ready'"
                ).fetchone()[0]
            )
            assert count == 2
            assert not services.jobs._tasks

    anyio.run(recover)
