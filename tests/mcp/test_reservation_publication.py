"""Publication metadata and committed bytes must share a transaction."""

import sqlite3

import pytest

pytest.importorskip("pydantic")
anyio = pytest.importorskip("anyio")


def test_publication_failure_rolls_back_metadata_and_quota(tmp_path):
    async def scenario():
        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        with State(tmp_path / "state") as state:
            store = WorkspaceStore(state, Limits())
            owner = stdio_principal()
            workspace = await store.create(owner)
            await state.run(lambda db: db.execute("CREATE TABLE receipts (size INTEGER)").close())

            def publish(db: sqlite3.Connection):
                db.execute("INSERT INTO receipts VALUES (7)")

            def interrupted(db: sqlite3.Connection):
                publish(db)
                raise RuntimeError("publication interrupted")

            async with store.reserve(owner, workspace.id, 10) as reservation:
                with pytest.raises(RuntimeError, match="publication interrupted"):
                    await reservation.commit(size=7, on_commit=interrupted)
                assert await state.run(
                    lambda db: tuple(
                        db.execute("SELECT files,reserved_files FROM workspaces").fetchone()
                    )
                ) == (0, 10)
                assert (
                    await state.run(
                        lambda db: db.execute("SELECT count(*) FROM receipts").fetchone()[0]
                    )
                    == 0
                )
                await reservation.commit(size=7, on_commit=publish)
                await reservation.commit(size=7, on_commit=interrupted)
            assert await state.run(
                lambda db: tuple(
                    db.execute("SELECT files,reserved_files FROM workspaces").fetchone()
                )
            ) == (7, 0)
            assert (
                await state.run(lambda db: db.execute("SELECT size FROM receipts").fetchall()[0][0])
                == 7
            )

    anyio.run(scenario)
