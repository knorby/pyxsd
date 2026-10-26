"""Immutable revisions preserve bytes and publish quota with metadata."""

import importlib
import sqlite3

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("defusedxml")
anyio = pytest.importorskip("anyio")


def test_v1_state_upgrades_without_losing_committed_usage(tmp_path):
    from pyxsd.mcp.state import _SCHEMA, State

    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.executescript(_SCHEMA)
        db.execute(
            "INSERT INTO workspaces(id,owner,created,expires,files,leases) VALUES('legacy','stdio',0,1000000000000,7,9)"
        )
    with State(root) as state:

        async def inspect():
            assert await state.run(lambda db: db.execute("PRAGMA user_version").fetchone()[0]) == 3
            assert await state.run(
                lambda db: tuple(db.execute("SELECT files,leases FROM workspaces").fetchone())
            ) == (7, 0)

        anyio.run(inspect)


def test_snapshot_updates_preserve_old_revisions_and_original_binary_bytes(tmp_path):
    async def scenario():
        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import InlineFile
        from pyxsd.mcp.errors import ToolError
        from pyxsd.mcp.safe_io import AdmittedFile
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import Principal, WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            store = module.SnapshotStore(workspaces)
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            first = await store.put_files(
                owner,
                workspace.id,
                [InlineFile(path="schema/root.xsd", text="<schema>")],
                expected_revision=None,
            )
            binary = b'<?xml version="1.0" encoding="ISO-8859-1"?><r>caf\xe9</r>'
            second = await store.put_bytes(
                owner,
                workspace.id,
                [AdmittedFile("instance.xml", binary)],
                expected_revision=first.revision,
            )
            assert second.revision != first.revision
            assert [item.path for item in second.files] == ["instance.xml", "schema/root.xsd"]
            assert (second.root / "instance.xml").read_bytes() == binary
            assert (first.root / "schema/root.xsd").read_bytes() == b"<schema>"
            assert not (first.root / "instance.xml").exists()
            assert await store.get(owner, workspace.id) == second
            assert await store.get(owner, workspace.id, revision=first.revision) == first
            with pytest.raises(ToolError, match="stale_reference"):
                await store.put_files(owner, workspace.id, [], expected_revision=first.revision)
            other = Principal("other", owner.scopes)
            with pytest.raises(ToolError, match="not_found"):
                await store.get(other, workspace.id)
            with pytest.raises(ToolError, match="not_found"):
                await store.get(owner, workspace.id, revision="../outside")
            with pytest.raises(ToolError, match="forbidden"):
                await store.put_files(
                    Principal(owner.owner_id, frozenset()), workspace.id, [], expected_revision=None
                )
            saved_id, saved_revision = workspace.id, second.revision
        with State(tmp_path / "state") as restarted:
            restored = module.SnapshotStore(WorkspaceStore(restarted, Limits()))
            assert (await restored.get(owner, saved_id)).revision == saved_revision
            assert await restarted.run(
                lambda db: db.execute("SELECT files FROM workspaces").fetchone()[0]
            ) > len(binary)

    anyio.run(scenario)


@pytest.mark.parametrize("budget", ["snapshot_entries", "snapshot_revisions"])
def test_empty_files_cannot_bypass_snapshot_metadata_budgets(tmp_path, budget):
    async def scenario():
        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import InlineFile
        from pyxsd.mcp.errors import ToolError
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        limits = Limits.model_validate({budget: 1})
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, limits)
            store = module.SnapshotStore(workspaces)
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            first = await store.put_files(
                owner, workspace.id, [InlineFile(path="a", text="")], expected_revision=None
            )
            with pytest.raises(ToolError, match="quota_exceeded"):
                await store.put_files(
                    owner,
                    workspace.id,
                    [InlineFile(path="b", text="")],
                    expected_revision=first.revision,
                )
            assert await store.get(owner, workspace.id) == first

    anyio.run(scenario)


@pytest.mark.parametrize("collision", ["filesystem", "record"])
def test_snapshot_allocation_never_adopts_an_existing_directory(tmp_path, monkeypatch, collision):
    async def scenario():
        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import InlineFile
        from pyxsd.mcp.errors import ToolError
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            store = module.SnapshotStore(workspaces)
            expected = None
            if collision == "record":
                first = await store.put_files(
                    owner,
                    workspace.id,
                    [InlineFile(path="x", text="original")],
                    expected_revision=None,
                )
                expected = first.revision
                existing = first.root.parent
            else:
                existing = state.root / "workspaces" / workspace.id / "snapshots" / ("a" * 32)
                existing.mkdir(parents=True)
            (existing / "marker").write_bytes(b"keep")
            monkeypatch.setattr(module.secrets, "token_hex", lambda _: existing.name)
            with pytest.raises(ToolError, match="allocation_conflict"):
                await store.put_files(
                    owner,
                    workspace.id,
                    [InlineFile(path="x", text="x")],
                    expected_revision=expected,
                )
            assert (existing / "marker").read_bytes() == b"keep"
            if collision == "record":
                assert (existing / "files" / "x").read_bytes() == b"original"
                assert (await store.get(owner, workspace.id)).revision == expected
            else:
                assert not (existing / "files").exists()

    anyio.run(scenario)


@pytest.mark.parametrize("operator_removed", [False, True])
def test_failed_cleanup_keeps_reserved_disk_charged_until_restart(
    tmp_path, monkeypatch, operator_removed
):
    async def scenario():
        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import InlineFile
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            store = module.SnapshotStore(workspaces)

            def broken_write(snapshot, previous, updates):
                snapshot.root.mkdir()
                (snapshot.root / "x").write_bytes(b"x")
                raise OSError("write failed")

            def broken_cleanup(snapshot):
                raise PermissionError("cleanup denied")

            monkeypatch.setattr(store, "_write", broken_write)
            monkeypatch.setattr(store, "_discard", broken_cleanup)
            with pytest.raises(PermissionError, match="cleanup denied"):
                await store.put_files(
                    owner, workspace.id, [InlineFile(path="x", text="x")], expected_revision=None
                )
            assert (
                await state.run(
                    lambda db: db.execute("SELECT reserved_files FROM workspaces").fetchone()[0]
                )
                > 0
            )
            pending = state.root / "workspaces" / workspace.id / "snapshots"
            assert len(list(pending.iterdir())) == 1
        if operator_removed:
            import shutil

            shutil.rmtree(next(pending.iterdir()))
        with State(tmp_path / "state") as restarted:
            assert not list(pending.iterdir())
            assert (
                await restarted.run(
                    lambda db: db.execute("SELECT reserved_files FROM workspaces").fetchone()[0]
                )
                == 0
            )

    anyio.run(scenario)


@pytest.mark.parametrize("damage", ["bytes", "missing", "size"])
def test_mutated_revision_cannot_be_copied_into_a_new_snapshot(tmp_path, damage):
    async def scenario():
        import json

        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import InlineFile
        from pyxsd.mcp.errors import ToolError
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            store = module.SnapshotStore(workspaces)
            first = await store.put_files(
                owner, workspace.id, [InlineFile(path="x", text="x")], expected_revision=None
            )
            if damage == "bytes":
                (first.root / "x").write_bytes(b"y")
            elif damage == "missing":
                (first.root / "x").unlink()
            else:

                def corrupt(db):
                    manifest = json.loads(
                        db.execute("SELECT manifest FROM snapshots").fetchone()[0]
                    )
                    manifest[0]["size"] += 1
                    db.execute("UPDATE snapshots SET manifest=?", (json.dumps(manifest),))

                await state.run(corrupt)
            with pytest.raises(ToolError, match="source_changed"):
                await store.put_files(owner, workspace.id, [], expected_revision=first.revision)
            assert (await store.get(owner, workspace.id)).revision == first.revision
            assert len(list(first.root.parent.parent.iterdir())) == 1

    anyio.run(scenario)


def test_simultaneous_snapshot_updates_publish_only_one_head(tmp_path, monkeypatch):
    async def scenario():
        import threading

        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import InlineFile
        from pyxsd.mcp.errors import ToolError
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            store = module.SnapshotStore(workspaces)
            barrier = threading.Barrier(2, timeout=5)
            write = store._write

            def simultaneous(*args):
                try:
                    write(*args)
                    barrier.wait()
                except BaseException:
                    barrier.abort()
                    raise

            monkeypatch.setattr(store, "_write", simultaneous)
            results = []

            async def put(text):
                try:
                    results.append(
                        await store.put_files(
                            owner,
                            workspace.id,
                            [InlineFile(path="x", text=text)],
                            expected_revision=None,
                        )
                    )
                except ToolError as exc:
                    results.append(exc.fault.code)

            with anyio.fail_after(10):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(put, "a")
                    tasks.start_soon(put, "b")
            assert results.count("stale_reference") == 1
            head = await store.get(owner, workspace.id)
            assert head in results
            assert (
                await state.run(
                    lambda db: db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
                )
                == 1
            )

    anyio.run(scenario)


def test_publication_flushes_directory_entries_as_well_as_file_bytes(tmp_path, monkeypatch):
    async def scenario():
        import os
        import stat

        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import InlineFile
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        flushed = set()
        fsync = os.fsync

        def record(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                flushed.add((info.st_dev, info.st_ino))
            fsync(fd)

        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            store = module.SnapshotStore(workspaces)
            monkeypatch.setattr(module.os, "fsync", record)
            snapshot = await store.put_files(
                owner, workspace.id, [InlineFile(path="nested/x", text="x")], expected_revision=None
            )
            for directory in (
                snapshot.root / "nested",
                snapshot.root,
                snapshot.root.parent,
                snapshot.root.parent.parent,
                snapshot.root.parent.parent.parent,
            ):
                info = directory.stat()
                assert (info.st_dev, info.st_ino) in flushed

    anyio.run(scenario)


def test_shared_file_budget_also_bounds_directory_metadata(tmp_path):
    async def scenario():
        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import InlineFile
        from pyxsd.mcp.errors import ToolError
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits(snapshot_entries=2))
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            store = module.SnapshotStore(workspaces)
            with pytest.raises(ToolError, match="quota_exceeded"):
                await store.put_files(
                    owner,
                    workspace.id,
                    [InlineFile(path="a/b/c/x", text="")],
                    expected_revision=None,
                )
            assert (
                await state.run(
                    lambda db: db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
                )
                == 0
            )

    anyio.run(scenario)


@pytest.mark.parametrize("kind", ["file", "manifest"])
def test_snapshot_disk_reservation_includes_manifest_metadata(tmp_path, kind):
    async def scenario():
        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.errors import ToolError
        from pyxsd.mcp.safe_io import AdmittedFile
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        limits = Limits(xml_file_bytes=2) if kind == "file" else Limits(workspace_bytes=8)
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, limits)
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            store = module.SnapshotStore(workspaces)
            with pytest.raises(ToolError, match="quota_exceeded"):
                await store.put_bytes(
                    owner,
                    workspace.id,
                    [AdmittedFile("x", b"xxx" if kind == "file" else b"")],
                    expected_revision=None,
                )
            with pytest.raises(ToolError, match="not_found"):
                await store.get(owner, workspace.id)
            assert await state.run(
                lambda db: tuple(
                    db.execute("SELECT files,reserved_files FROM workspaces").fetchone()
                )
            ) == (0, 0)

    anyio.run(scenario)


def test_cancelled_copy_cannot_enter_the_publication_transaction(tmp_path, monkeypatch):
    async def scenario():
        import threading

        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import InlineFile
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            store = module.SnapshotStore(workspaces)
            ready = anyio.Event()
            release = threading.Event()
            write = store._write
            scope = anyio.CancelScope()

            def blocked(*args):
                write(*args)
                anyio.from_thread.run_sync(ready.set)
                if not release.wait(5):
                    raise TimeoutError("copy test did not release worker")

            monkeypatch.setattr(store, "_write", blocked)

            async def put():
                with scope:
                    await store.put_files(
                        owner,
                        workspace.id,
                        [InlineFile(path="x", text="x")],
                        expected_revision=None,
                    )

            with anyio.fail_after(10):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(put)
                    try:
                        await ready.wait()
                        scope.cancel()
                    finally:
                        release.set()
            assert (
                await state.run(
                    lambda db: db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
                )
                == 0
            )
            assert await state.run(
                lambda db: tuple(
                    db.execute("SELECT files,reserved_files FROM workspaces").fetchone()
                )
            ) == (0, 0)
            assert not list((state.root / "workspaces" / workspace.id / "snapshots").iterdir())

    anyio.run(scenario)


def test_workspace_release_purges_snapshot_data_but_keeps_only_retry_tombstone(
    tmp_path, monkeypatch
):
    async def scenario():
        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import InlineFile
        from pyxsd.mcp.errors import ToolError
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        module = importlib.import_module("pyxsd.mcp.snapshot_store")
        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            owner = stdio_principal()
            workspace = await workspaces.create(owner, idempotency_key="retry")
            store = module.SnapshotStore(workspaces)
            first = await store.put_files(
                owner, workspace.id, [InlineFile(path="x", text="x")], expected_revision=None
            )

            def broken_write(*args):
                raise OSError("write failed")

            def broken_cleanup(*args):
                raise PermissionError("cleanup denied")

            monkeypatch.setattr(store, "_write", broken_write)
            monkeypatch.setattr(store, "_discard", broken_cleanup)
            with pytest.raises(PermissionError, match="cleanup denied"):
                await store.put_files(owner, workspace.id, [], expected_revision=first.revision)
            await workspaces.release(owner, workspace.id)
            assert not (state.root / "workspaces" / workspace.id).exists()
            assert await state.run(
                lambda db: tuple(
                    db.execute("SELECT files,reserved_files FROM workspaces").fetchone()
                )
            ) == (0, 0)
            for table in ("snapshots", "snapshot_heads", "reservations"):
                assert (
                    await state.run(
                        lambda db, table=table: db.execute(
                            f"SELECT count(*) FROM {table}"
                        ).fetchone()[0]
                    )
                    == 0
                )
            with pytest.raises(ToolError, match="stale_reference"):
                await workspaces.create(owner, idempotency_key="retry")

    anyio.run(scenario)
