"""Disposable coordinator used only by the real crash-recovery regression."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import anyio
from tests.mcp.test_workers import make_snapshot, parse_operation

from pyxsd.mcp import workers
from pyxsd.mcp.config import Limits
from pyxsd.mcp.jobs import JobManager
from pyxsd.mcp.state import State
from pyxsd.mcp.workspaces import Principal, WorkspaceStore


async def main(root: Path, phase: str) -> None:
    with State(root / "state") as state:
        workspaces = WorkspaceStore(state, Limits(workers=1))
        principal = Principal("alice", frozenset({"pyxsd:read", "pyxsd:upload", "pyxsd:execute"}))
        workspace = await workspaces.create(principal)
        source = root / "source"
        source.mkdir()
        snapshot = replace(make_snapshot(source), workspace_id=workspace.id)

        class BlockingPool:
            async def run(self, operation, *, deadline):
                (root / "executed").write_text("once", encoding="ascii")
                await anyio.sleep_forever()

        jobs = JobManager(workspaces, BlockingPool())
        if phase == "queued":

            async def hold(*_args):
                await anyio.sleep_forever()

            jobs._execute = hold
        ref = await jobs.submit(
            principal, workspace.id, parse_operation(workers, snapshot), idempotency_key="crash"
        )
        while (await jobs.status(principal, ref.id)).state != phase:
            await anyio.sleep(0)
        print(json.dumps({"id": ref.id, "workspace": workspace.id}), flush=True)
        await anyio.sleep_forever()


if __name__ == "__main__":
    anyio.run(main, Path(sys.argv[1]), sys.argv[2])
