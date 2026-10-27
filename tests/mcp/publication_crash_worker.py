"""Disposable coordinator stopped at a real publication durability boundary."""

import json
import sys
from pathlib import Path

import anyio
from tests.mcp.test_previews import harness, preview
from tests.mcp.test_publication import REPAIR

from pyxsd.mcp.publication import CommitTransformRequest, PublicationService


async def main(root: Path, phase: str) -> None:
    async with harness(root) as (previews, principal, workspace):
        record = await preview(previews, principal, workspace, REPAIR)
        await previews.pool.aclose()
        request = CommitTransformRequest(
            preview_id=record.id,
            expected_candidate_digest=record.candidate.digest,
            destination="recovered.xml",
            idempotency_key="crash-recovery",
        )
        original = previews.snapshots._publish_files

        async def stop():
            print(json.dumps({"request": request.model_dump(), "workspace": workspace}), flush=True)
            await anyio.sleep_forever()

        async def paused(*args, **kwargs):
            if phase == "after-intent":
                await stop()
            if phase == "after-staging":

                async def staged(_snapshot):
                    await stop()

                kwargs["before_publish"] = staged
            result = await original(*args, **kwargs)
            if phase == "after-receipt":
                await stop()
            return result

        previews.snapshots._publish_files = paused
        await PublicationService(previews).commit(principal, request)


if __name__ == "__main__":
    anyio.run(main, Path(sys.argv[1]), sys.argv[2])
