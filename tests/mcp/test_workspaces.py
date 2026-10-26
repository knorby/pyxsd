"""Workspace ownership, lifecycle and transactional quota reservations."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.state import State
    from pyxsd.mcp.workspaces import WorkspaceStore

    now = [1000.0]
    state = State(tmp_path / "state")
    result = WorkspaceStore(
        state, Limits(workspace_bytes=10, managed_bytes=15), clock=lambda: now[0]
    )
    yield result, now
    state.close()


def principal(name="alice", scopes=None):
    from pyxsd.mcp.workspaces import Principal

    return Principal(name, frozenset({"pyxsd:read", "pyxsd:upload"} if scopes is None else scopes))


@pytest.mark.anyio
async def test_owner_isolation_unknown_and_foreign_ids_are_indistinguishable(store):
    from pyxsd.mcp.errors import ToolError

    workspaces, _ = store
    owner, foreign = principal(), principal("bob")
    workspace = await workspaces.create(owner, idempotency_key="create-1")
    assert await workspaces.create(owner, idempotency_key="create-1") == workspace
    assert len(workspace.id) == 32
    assert await workspaces.list(owner) == (workspace,)
    assert await workspaces.list(foreign) == ()
    faults = []
    for caller, identifier in [(foreign, workspace.id), (owner, "unknown")]:
        with pytest.raises(ToolError) as exc:
            async with workspaces.lease(caller, identifier):
                pytest.fail("inaccessible workspace was leased")
        faults.append(exc.value.fault)
    assert faults[0] == faults[1]
    assert faults[0].code == "not_found"
    await workspaces.release(foreign, workspace.id)
    async with workspaces.lease(owner, workspace.id) as leased:
        assert leased.id == workspace.id
    await workspaces.release(owner, workspace.id)
    await workspaces.release(owner, workspace.id)
    await workspaces.release(owner, "unknown")
    assert await workspaces.list(owner) == ()


@pytest.mark.anyio
async def test_release_blocks_new_leases_but_preserves_active_files(store):
    from pyxsd.mcp.errors import ToolError

    workspaces, now = store
    owner = principal()
    workspace = await workspaces.create(owner)
    path = workspaces.state.root / "workspaces" / workspace.id
    (path / "input.xml").write_text("<r/>")
    async with workspaces.lease(owner, workspace.id):
        now[0] += 4000
        await workspaces.release(owner, workspace.id)
        assert (path / "input.xml").read_text() == "<r/>"
        with pytest.raises(ToolError, match="not_found"):
            async with workspaces.lease(owner, workspace.id):
                pytest.fail("closing workspace was leased")
    assert not path.exists()


@pytest.mark.anyio
async def test_idle_expiry_skips_active_leases_and_restarts_on_release(store):
    from pyxsd.mcp.errors import ToolError

    workspaces, now = store
    owner = principal()
    workspace = await workspaces.create(owner, idempotency_key="once")
    path = workspaces.state.root / "workspaces" / workspace.id
    async with workspaces.lease(owner, workspace.id):
        now[0] += 4000
        await workspaces.sweep()
        assert path.exists()
        assert [item.id for item in await workspaces.list(owner)] == [workspace.id]
        async with workspaces.lease(owner, workspace.id) as nested:
            assert nested.id == workspace.id
        assert path.exists()
    assert [item.id for item in await workspaces.list(owner)] == [workspace.id]
    assert (await workspaces.create(owner, idempotency_key="once")).id == workspace.id
    now[0] += 4000
    await workspaces.sweep()
    assert not path.exists()
    with pytest.raises(ToolError, match="stale_reference"):
        await workspaces.create(owner, idempotency_key="once")
    now[0] += 4000
    await workspaces.sweep()
    assert (await workspaces.create(owner, idempotency_key="once")).id != workspace.id


@pytest.mark.anyio
async def test_quota_reservations_commit_only_actual_bytes_and_rollback_on_error(store):
    from pyxsd.mcp.errors import ToolError

    workspaces, _ = store
    owner = principal()
    workspace = await workspaces.create(owner)
    async with workspaces.reserve(owner, workspace.id, 10) as reservation:
        await reservation.commit(size=6)
        await reservation.commit(size=6)
    with pytest.raises(ToolError, match="quota_exceeded"):
        async with workspaces.reserve(owner, workspace.id, 5):
            pytest.fail("committed bytes disappeared")
    with pytest.raises(RuntimeError, match="cancelled"):
        async with workspaces.reserve(owner, workspace.id, 4):
            raise RuntimeError("cancelled upload")
    async with workspaces.reserve(owner, workspace.id, 4) as reservation:
        await reservation.commit()
    await workspaces.release(owner, workspace.id)
    fresh = await workspaces.create(owner)
    async with workspaces.reserve(owner, fresh.id, 10):
        pass


@pytest.mark.anyio
async def test_aggregate_and_artifact_quotas_include_reservations(store):
    from pyxsd.mcp.errors import ToolError

    workspaces, _ = store
    owner = principal()
    first, second = await workspaces.create(owner), await workspaces.create(owner)
    async with workspaces.reserve(owner, first.id, 10):
        with pytest.raises(ToolError, match="quota_exceeded"):
            async with workspaces.reserve(owner, second.id, 6, kind="artifacts"):
                pytest.fail("aggregate quota was bypassed")
        async with workspaces.reserve(owner, second.id, 5, kind="artifacts"):
            pass


@pytest.mark.anyio
async def test_simultaneous_reservations_cannot_both_spend_the_last_bytes(store):
    import anyio

    from pyxsd.mcp.errors import ToolError

    workspaces, _ = store
    owner = principal()
    workspace = await workspaces.create(owner)
    ready, release = anyio.Event(), anyio.Event()
    arrived = 0
    outcomes = []

    async def upload():
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await ready.wait()
        try:
            async with workspaces.reserve(owner, workspace.id, 10):
                outcomes.append("reserved")
                await release.wait()
        except ToolError as exc:
            outcomes.append(exc.fault.code)
            release.set()

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(upload)
            tasks.start_soon(upload)
    assert sorted(outcomes) == ["quota_exceeded", "reserved"]


@pytest.mark.anyio
async def test_scope_checks_and_workspace_count_are_enforced(store):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.errors import ToolError
    from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

    original, _ = store
    limited = WorkspaceStore(original.state, Limits(workspaces=1))
    with pytest.raises(ToolError, match="forbidden"):
        await limited.create(principal(scopes={"pyxsd:read"}))
    with pytest.raises(ToolError, match="forbidden"):
        await limited.list(principal(scopes={"pyxsd:upload"}))
    workspace = await limited.create(principal())
    with pytest.raises(ToolError, match="quota_exceeded"):
        await limited.create(principal())
    await limited.release(principal(), workspace.id)
    assert (await limited.create(principal())).id != workspace.id
    assert stdio_principal() == stdio_principal()
    stdio_principal().require("pyxsd:read", "pyxsd:upload")


@pytest.mark.anyio
async def test_restart_recovers_workspace_and_committed_accounting(tmp_path):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.errors import ToolError
    from pyxsd.mcp.state import State
    from pyxsd.mcp.workspaces import WorkspaceStore

    root = tmp_path / "state"
    with State(root) as state:
        workspaces = WorkspaceStore(state, Limits(workspace_bytes=10))
        workspace = await workspaces.create(principal(), idempotency_key="restart")
        async with workspaces.reserve(principal(), workspace.id, 10) as reservation:
            await reservation.commit()
    with State(root) as state:
        workspaces = WorkspaceStore(state, Limits(workspace_bytes=10))
        assert (await workspaces.create(principal(), idempotency_key="restart")).id == workspace.id
        with pytest.raises(ToolError, match="quota_exceeded"):
            async with workspaces.reserve(principal(), workspace.id, 1):
                pytest.fail("restart forgot committed bytes")


@pytest.mark.anyio
async def test_closing_intent_is_durable_before_files_are_deleted(store, monkeypatch):
    import shutil
    import sqlite3

    workspaces, _ = store
    workspace = await workspaces.create(principal())
    original = shutil.rmtree
    observed = []

    def remove(path):
        with sqlite3.connect(workspaces.state.root / "state.sqlite3") as independent:
            observed.append(
                independent.execute(
                    "SELECT status FROM workspaces WHERE id=?", (workspace.id,)
                ).fetchone()[0]
            )
        original(path)

    monkeypatch.setattr(shutil, "rmtree", remove)
    await workspaces.release(principal(), workspace.id)
    assert observed == ["closing"]


@pytest.mark.anyio
async def test_failed_cleanup_stays_closed_to_new_users_and_can_retry(store, monkeypatch):
    import shutil

    from pyxsd.mcp.errors import ToolError

    workspaces, _ = store
    workspace = await workspaces.create(principal())
    with monkeypatch.context() as patch:

        def failed_remove(path):
            raise OSError("filesystem unavailable")

        patch.setattr(shutil, "rmtree", failed_remove)
        with pytest.raises(OSError, match="filesystem unavailable"):
            await workspaces.release(principal(), workspace.id)
        rows = await workspaces.state.run(
            lambda db: db.execute(
                "SELECT status FROM workspaces WHERE id=?", (workspace.id,)
            ).fetchone()
        )
        assert rows[0] == "closing"
    await workspaces.sweep()
    with pytest.raises(ToolError, match="not_found"):
        async with workspaces.lease(principal(), workspace.id):
            pytest.fail("failed release resurrected the workspace")


@pytest.mark.anyio
async def test_invalid_reservations_and_commits_do_not_change_accounting(store):
    from pyxsd.mcp.errors import ToolError

    workspaces, _ = store
    workspace = await workspaces.create(principal())
    for size, kind in [(-1, "files"), (True, "files"), (1, "other")]:
        with pytest.raises(ToolError, match="invalid_argument"):
            async with workspaces.reserve(principal(), workspace.id, size, kind=kind):
                pytest.fail("invalid reservation accepted")
    async with workspaces.reserve(principal(), workspace.id, 10) as reservation:
        for size in (-1, 11, True):
            with pytest.raises(ToolError, match="invalid_argument"):
                await reservation.commit(size=size)
        await reservation.commit(size=5)
        with pytest.raises(ToolError, match="idempotency_conflict"):
            await reservation.commit(size=4)
    with pytest.raises(ToolError, match="stale_reference"):
        await reservation.commit()
    async with workspaces.reserve(principal(), workspace.id, 5):
        pass


@pytest.mark.anyio
async def test_artifact_budget_is_separate_from_input_budget(store):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.errors import ToolError
    from pyxsd.mcp.workspaces import WorkspaceStore

    original, _ = store
    workspaces = WorkspaceStore(original.state, Limits(artifact_bytes_per_workspace=4))
    workspace = await workspaces.create(principal())
    with pytest.raises(ToolError, match="quota_exceeded"):
        async with workspaces.reserve(principal(), workspace.id, 5, kind="artifacts"):
            pytest.fail("artifact budget ignored")
    async with workspaces.reserve(principal(), workspace.id, 0) as empty:
        await empty.commit()


@pytest.mark.anyio
async def test_cancellation_releases_reservations_and_leases(store):
    import anyio

    workspaces, _ = store
    workspace = await workspaces.create(principal())
    with anyio.CancelScope() as scope:
        async with workspaces.reserve(principal(), workspace.id, 10):
            scope.cancel()
            await anyio.sleep(0)
    async with workspaces.reserve(principal(), workspace.id, 10):
        pass
    await workspaces.release(principal(), workspace.id)
    assert not (workspaces.state.root / "workspaces" / workspace.id).exists()


@pytest.mark.anyio
async def test_release_recovers_an_already_missing_managed_directory(store):
    workspaces, _ = store
    workspace = await workspaces.create(principal())
    (workspaces.state.root / "workspaces" / workspace.id).rmdir()
    await workspaces.release(principal(), workspace.id)
    assert await workspaces.list(principal()) == ()


@pytest.mark.anyio
async def test_abrupt_restart_releases_only_uncommitted_reservations(tmp_path):
    import subprocess
    import sys

    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.state import State
    from pyxsd.mcp.workspaces import WorkspaceStore

    root = tmp_path / "state"
    script = """
import asyncio, os, sys
from pathlib import Path
from pyxsd.mcp.config import Limits
from pyxsd.mcp.state import State
from pyxsd.mcp.workspaces import Principal, WorkspaceStore
async def crash():
    state = State(Path(sys.argv[1]))
    store = WorkspaceStore(state, Limits(workspace_bytes=10))
    owner = Principal('alice', frozenset({'pyxsd:read', 'pyxsd:upload'}))
    workspace = await store.create(owner, idempotency_key='crash')
    async with store.reserve(owner, workspace.id, 10):
        print(workspace.id, flush=True)
        os._exit(0)
asyncio.run(crash())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    with State(root) as state:
        workspaces = WorkspaceStore(state, Limits(workspace_bytes=10))
        workspace = await workspaces.create(principal(), idempotency_key="crash")
        assert workspace.id == result.stdout.strip()
        async with workspaces.reserve(principal(), workspace.id, 10):
            pass
        await workspaces.release(principal(), workspace.id)


def test_state_lock_and_version_protect_restart_recovery(tmp_path):
    import sqlite3

    from pyxsd.mcp.errors import ToolError
    from pyxsd.mcp.state import State

    root = tmp_path / "state"
    with State(root) as state:
        with pytest.raises(ToolError, match="state_locked"):
            State(root)
        state.close()
        state.close()
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(ToolError, match="state_version"):
        State(root)
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("PRAGMA user_version = 3")
    with State(root):
        pass


@pytest.mark.anyio
async def test_state_rollback_closed_state_and_initialization_failure(tmp_path, monkeypatch):
    import sqlite3

    from pyxsd.mcp.errors import ToolError
    from pyxsd.mcp.state import State

    root = tmp_path / "state"
    with State(root) as state:

        def failing(db):
            db.execute(
                "INSERT INTO workspaces(id,owner,created,expires) VALUES('rollback','alice',0,1)"
            )
            raise RuntimeError("abort transaction")

        with pytest.raises(RuntimeError, match="abort transaction"):
            await state.run(failing)
        assert (
            await state.run(lambda db: db.execute("SELECT count(*) FROM workspaces").fetchone()[0])
            == 0
        )
    with pytest.raises(ToolError, match="state_closed"):
        await state.run(lambda db: None)
    with monkeypatch.context() as patch:

        def unavailable(*args, **kwargs):
            raise OSError("database unavailable")

        patch.setattr(sqlite3, "connect", unavailable)
        with pytest.raises(OSError, match="database unavailable"):
            State(root)
    with State(root):
        pass


def test_state_fails_closed_without_private_directory_or_platform_primitives(tmp_path, monkeypatch):
    import os
    import sys

    from pyxsd.mcp.errors import ToolError
    from pyxsd.mcp.state import State

    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(ToolError, match="unsafe_state_directory"):
        State(root)
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "fcntl", None)
        with pytest.raises(ToolError, match="unsupported_platform"):
            State(tmp_path / "missing-lock")
    with monkeypatch.context() as patch:
        patch.delattr(os, "O_NOFOLLOW")
        with pytest.raises(ToolError, match="unsupported_platform"):
            State(tmp_path / "missing-nofollow")


@pytest.mark.anyio
async def test_idempotency_tombstones_are_bounded_without_silent_early_reuse(store):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.errors import ToolError
    from pyxsd.mcp.workspaces import WorkspaceStore

    original, now = store
    workspaces = WorkspaceStore(original.state, Limits(workspaces=1), clock=lambda: now[0])
    first = await workspaces.create(principal(), idempotency_key="first")
    await workspaces.release(principal(), first.id)
    with pytest.raises(ToolError, match="quota_exceeded"):
        await workspaces.create(principal(), idempotency_key="second")
    # Unkeyed creation needs no durable retry record and can reuse active capacity.
    unkeyed = await workspaces.create(principal())
    await workspaces.release(principal(), unkeyed.id)
    count = await workspaces.state.run(
        lambda db: db.execute("SELECT count(*) FROM workspaces").fetchone()[0]
    )
    assert count == 1
    now[0] += 4000
    await workspaces.sweep()
    assert (await workspaces.create(principal(), idempotency_key="second")).id != first.id


@pytest.mark.anyio
async def test_creation_rejects_oversize_keys_and_never_overwrites_colliding_paths(
    store, monkeypatch
):
    import secrets

    from pyxsd.mcp.errors import ToolError

    workspaces, _ = store
    for key in ("", "x" * 129, 1, True):
        with pytest.raises(ToolError, match="invalid_argument"):
            await workspaces.create(principal(), idempotency_key=key)
    first = await workspaces.create(principal())
    with monkeypatch.context() as patch:
        patch.setattr(secrets, "token_hex", lambda size: first.id)
        with pytest.raises(ToolError, match="allocation_conflict") as exc:
            await workspaces.create(principal())
        assert exc.value.fault.retryable
    orphan = workspaces.state.root / "workspaces" / ("0" * 32)
    orphan.mkdir()
    (orphan / "marker").write_text("preserve")
    with monkeypatch.context() as patch:
        patch.setattr(secrets, "token_hex", lambda size: "0" * 32)
        with pytest.raises(ToolError, match="allocation_conflict"):
            await workspaces.create(principal())
    assert (orphan / "marker").read_text() == "preserve"
    assert [workspace.id for workspace in await workspaces.list(principal())] == [first.id]
