"""Clean-worker diffs preserve expanded names and positional occurrences."""

import hashlib
import time

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio

from pyxsd.mcp.captures import CaptureFile, InputCapture
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef
from pyxsd.mcp.worker_protocol import StructuralDiffOperation
from pyxsd.mcp.workers import WorkerPool, capture_to_wire


@pytest.mark.parametrize("limit", [1, 50])
def test_added_removed_changed_and_unchanged_occurrences_are_bounded(tmp_path, limit):
    files = {
        "old.xml": b'<r><same/><gone/><item a="1">old</item><item>keep</item></r>',
        "new.xml": b'<r><same/><added/><item a="2">new</item><item>keep</item></r>',
    }
    manifest = []
    for path, content in files.items():
        (tmp_path / path).write_bytes(content)
        manifest.append(CaptureFile(path, hashlib.sha256(content).hexdigest(), len(content)))
    capture = InputCapture("workspace", 1, tmp_path, tuple(manifest))

    async def scenario():
        async with WorkerPool(Limits(workers=1)) as pool:
            result = await pool.run(
                StructuralDiffOperation(
                    request_id="diff",
                    capture=capture_to_wire(capture),
                    original=FileRef(path="old.xml"),
                    candidate=FileRef(path="new.xml"),
                    limit=limit,
                ),
                deadline=time.monotonic() + 10,
            )
            assert result.status == "ok"
            assert result.result["total"] == 3
            assert result.result["truncated"] is (limit == 1)
            assert len(result.result["changes"]) == min(3, limit)
            if limit == 50:
                changes = {
                    change["path"][-1]["name"]: change for change in result.result["changes"]
                }
                assert changes["added"]["kind"] == "added"
                assert changes["gone"]["kind"] == "removed"
                assert changes["item"]["kind"] == "changed"
                assert changes["item"]["path"][-1]["index"] == 1

    anyio.run(scenario)
