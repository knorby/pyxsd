"""Private immutable artifacts retain leases and release their disk reservations."""

import hashlib
import shutil

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")

import anyio

from pyxsd.mcp import artifacts
from pyxsd.mcp.config import Limits
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.state import State
from pyxsd.mcp.workspaces import Principal, WorkspaceStore, stdio_principal


async def chunks(*values):
    for value in values:
        yield value


def test_private_artifact_ranges_expiry_and_active_download(tmp_path):
    async def scenario():
        now = [100.0]
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits(), clock=lambda: now[0])
            workspace = await workspaces.create(stdio_principal())
            store = artifacts.ArtifactStore(workspaces)
            owner = Principal("stdio", frozenset({"pyxsd:read"}))
            ref = await store.put(
                owner,
                workspace.id,
                chunks(b"abc", b"def"),
                media_type="text/plain",
                limit=6,
                ttl=10,
            )
            assert ref.digest == hashlib.sha256(b"abcdef").hexdigest()
            assert ref.size == 6 and ref.expires_at == 110.0
            assert str(tmp_path) not in ref.model_dump_json()
            assert await store.read(owner, ref.id, offset=2, length=3) == b"cde"
            async with store.lease(owner, ref.id) as reader:
                now[0] = 110.0
                await store.sweep()
                assert await reader.read(offset=0, length=6) == b"abcdef"
                with pytest.raises(ToolError, match="not_found"):
                    await store.read(owner, ref.id, offset=0, length=1)
            with pytest.raises(ToolError, match="stale_reference"):
                await reader.read(offset=0, length=1)
            assert (
                await state.run(
                    lambda db: db.execute("SELECT artifacts FROM workspaces").fetchone()[0]
                )
                == 0
            )
            with pytest.raises(ToolError, match="not_found"):
                await store.read(owner, ref.id, offset=0, length=1)

    anyio.run(scenario)


def test_empty_artifact_entry_limits_and_restart(tmp_path):
    async def scenario():
        root = tmp_path / "state"
        with State(root) as state:
            workspaces = WorkspaceStore(state, Limits(artifact_entries=1))
            workspace = await workspaces.create(stdio_principal())
            store = artifacts.ArtifactStore(workspaces)
            ref = await store.put(
                stdio_principal(), workspace.id, chunks(), media_type="text/plain", limit=0, ttl=30
            )
            assert await store.read(stdio_principal(), ref.id, offset=0, length=0) == b""
            with pytest.raises(ToolError, match="quota_exceeded"):
                await store.put(
                    stdio_principal(),
                    workspace.id,
                    chunks(),
                    media_type="text/plain",
                    limit=0,
                    ttl=30,
                )
        with State(root) as state:
            store = artifacts.ArtifactStore(WorkspaceStore(state, Limits()))
            assert await store.read(stdio_principal(), ref.id, offset=0, length=0) == b""

    anyio.run(scenario)


@pytest.mark.parametrize("offset,length", [(-1, 1), (0, -1), (True, 1), (0, 24577), (4, 0)])
def test_range_limits(tmp_path, offset, length):
    async def scenario():
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            workspace = await workspaces.create(stdio_principal())
            store = artifacts.ArtifactStore(workspaces)
            ref = await store.put(
                stdio_principal(),
                workspace.id,
                chunks(b"abc"),
                media_type="text/plain",
                limit=3,
                ttl=30,
            )
            with pytest.raises(ToolError, match="invalid_argument"):
                await store.read(stdio_principal(), ref.id, offset=offset, length=length)

    anyio.run(scenario)


@pytest.mark.parametrize(
    "options",
    [
        {"limit": -1},
        {"limit": True},
        {"ttl": 0},
        {"media_type": "x" * 129},
        {"media_type": "text/plain\r\nX-Injected: yes"},
        {"expected_digest": "bad"},
    ],
)
def test_invalid_artifact_configuration(tmp_path, options):
    async def scenario():
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            workspace = await workspaces.create(stdio_principal())
            store = artifacts.ArtifactStore(workspaces)
            arguments = {"limit": 3, "ttl": 30, "media_type": "text/plain", **options}
            with pytest.raises(ToolError, match="invalid_argument"):
                await store.put(stdio_principal(), workspace.id, chunks(b"abc"), **arguments)

    anyio.run(scenario)


@pytest.mark.parametrize(
    "values,code", [((b"too long",), "quota_exceeded"), (("not bytes",), "invalid_argument")]
)
def test_failed_stream_releases_staging_and_quota(tmp_path, values, code):
    async def scenario():
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            workspace = await workspaces.create(stdio_principal())
            store = artifacts.ArtifactStore(workspaces)
            with pytest.raises(ToolError, match=code):
                await store.put(
                    stdio_principal(),
                    workspace.id,
                    chunks(*values),
                    media_type="text/plain",
                    limit=2,
                    ttl=30,
                )
            assert await state.run(
                lambda db: tuple(
                    db.execute(
                        "SELECT artifacts,reserved_artifacts,leases FROM workspaces"
                    ).fetchone()
                )
            ) == (0, 0, 0)
            assert (
                await state.run(
                    lambda db: db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
                )
                == 0
            )

    anyio.run(scenario)


def test_digest_ownership_corruption_and_workspace_release(tmp_path):
    async def scenario():
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            workspace = await workspaces.create(stdio_principal(), idempotency_key="retry")
            store = artifacts.ArtifactStore(workspaces)
            with pytest.raises(ToolError, match="digest_mismatch"):
                await store.put(
                    stdio_principal(),
                    workspace.id,
                    chunks(b"abc"),
                    media_type="text/plain",
                    limit=3,
                    ttl=30,
                    expected_digest="0" * 64,
                )
            ref = await store.put(
                stdio_principal(),
                workspace.id,
                chunks(b"abc"),
                media_type="text/plain",
                limit=3,
                ttl=30,
            )
            with pytest.raises(ToolError, match="not_found"):
                await store.read(
                    Principal("other", frozenset({"pyxsd:read"})), ref.id, offset=0, length=3
                )
            path = state.root / "workspaces" / workspace.id / "artifacts" / ref.id / "blob"
            path.write_bytes(b"bad")
            with pytest.raises(ToolError, match="source_changed"):
                await store.read(stdio_principal(), ref.id, offset=0, length=3)
            await workspaces.release(stdio_principal(), workspace.id)
            assert (
                await state.run(
                    lambda db: db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
                )
                == 0
            )

    anyio.run(scenario)


def test_missing_blob_and_nonce_collisions_never_adopt_existing_bytes(tmp_path, monkeypatch):
    async def scenario():
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            workspace = await workspaces.create(stdio_principal())
            store = artifacts.ArtifactStore(workspaces)
            ref = await store.put(
                stdio_principal(),
                workspace.id,
                chunks(b"abc"),
                media_type="text/plain",
                limit=3,
                ttl=30,
            )
            parent = state.root / "workspaces" / workspace.id / "artifacts"
            with monkeypatch.context() as patch:
                patch.setattr(artifacts.secrets, "token_hex", lambda count: ref.id)
                with pytest.raises(ToolError, match="allocation_conflict"):
                    await store.put(
                        stdio_principal(),
                        workspace.id,
                        chunks(),
                        media_type="text/plain",
                        limit=0,
                        ttl=30,
                    )
            assert await store.read(stdio_principal(), ref.id, offset=0, length=3) == b"abc"
            orphan = parent / "orphan"
            orphan.mkdir()
            (orphan / "marker").write_bytes(b"keep")
            with monkeypatch.context() as patch:
                patch.setattr(artifacts.secrets, "token_hex", lambda count: "orphan")
                with pytest.raises(ToolError, match="allocation_conflict"):
                    await store.put(
                        stdio_principal(),
                        workspace.id,
                        chunks(),
                        media_type="text/plain",
                        limit=0,
                        ttl=30,
                    )
            assert (orphan / "marker").read_bytes() == b"keep"
            (parent / ref.id / "blob").unlink()
            with pytest.raises(ToolError, match="source_changed"):
                await store.read(stdio_principal(), ref.id, offset=0, length=3)

    anyio.run(scenario)


@pytest.mark.parametrize("predelete", [False, True])
def test_failed_cleanup_retains_quota_until_startup_recovery(tmp_path, monkeypatch, predelete):
    async def broken():
        yield b"a"
        raise OSError("producer failed")

    async def scenario():
        root = tmp_path / "state"
        with State(root) as state:
            workspaces = WorkspaceStore(state, Limits())
            workspace = await workspaces.create(stdio_principal())
            store = artifacts.ArtifactStore(workspaces)
            with monkeypatch.context() as patch:
                patch.setattr(
                    artifacts.shutil,
                    "rmtree",
                    lambda path: (_ for _ in ()).throw(OSError("cleanup failed")),
                )
                with pytest.raises(OSError, match="cleanup failed"):
                    await store.put(
                        stdio_principal(),
                        workspace.id,
                        broken(),
                        media_type="text/plain",
                        limit=2,
                        ttl=30,
                    )
            assert (
                await state.run(
                    lambda db: db.execute("SELECT reserved_artifacts FROM workspaces").fetchone()[0]
                )
                == 514
            )
            if predelete:
                shutil.rmtree(state.root / "workspaces" / workspace.id / "artifacts")
        with State(root) as state:
            assert await state.run(
                lambda db: tuple(
                    db.execute("SELECT artifacts,reserved_artifacts FROM workspaces").fetchone()
                )
            ) == (0, 0)
            assert (
                await state.run(
                    lambda db: db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
                )
                == 0
            )

    anyio.run(scenario)


def test_expiry_between_lookup_and_lease_acquisition(tmp_path, monkeypatch):
    async def scenario():
        now = [100.0]
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits(), clock=lambda: now[0])
            workspace = await workspaces.create(stdio_principal())
            store = artifacts.ArtifactStore(workspaces)
            ref = await store.put(
                stdio_principal(),
                workspace.id,
                chunks(b"abc"),
                media_type="text/plain",
                limit=3,
                ttl=10,
            )
            original = state.run

            async def race(operation):
                if operation.__qualname__ == "ArtifactStore.lease.<locals>.acquire":
                    now[0] = 110.0
                    await store.sweep()
                return await original(operation)

            monkeypatch.setattr(state, "run", race)
            with pytest.raises(ToolError, match="not_found"):
                await store.read(stdio_principal(), ref.id, offset=0, length=3)
            assert (
                await original(lambda db: db.execute("SELECT leases FROM workspaces").fetchone()[0])
                == 0
            )

    anyio.run(scenario)


def test_cancelled_stream_discards_output_and_reservations(tmp_path):
    async def scenario():
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            workspace = await workspaces.create(stdio_principal())
            store = artifacts.ArtifactStore(workspaces)
            started = anyio.Event()
            scopes = []

            async def stream():
                yield b"a"
                started.set()
                await anyio.sleep_forever()

            async def publish():
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    await store.put(
                        stdio_principal(),
                        workspace.id,
                        stream(),
                        media_type="text/plain",
                        limit=10,
                        ttl=30,
                    )

            with anyio.fail_after(5):
                async with anyio.create_task_group() as group:
                    group.start_soon(publish)
                    await started.wait()
                    scopes[0].cancel()
            assert await state.run(
                lambda db: tuple(
                    db.execute(
                        "SELECT artifacts,reserved_artifacts,leases FROM workspaces"
                    ).fetchone()
                )
            ) == (0, 0, 0)
            assert (
                await state.run(
                    lambda db: db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
                )
                == 0
            )

    anyio.run(scenario)
