"""External captures discover dependencies through a worker, never an XML parser here."""

from contextlib import asynccontextmanager

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("defusedxml")
anyio = pytest.importorskip("anyio")


def test_capture_copies_discovered_dependencies_and_replaces_previous_set(tmp_path):
    async def scenario():
        from pyxsd.mcp.config import Limits
        from pyxsd.mcp.contracts import FileRef, InlineFile, SchemaRef
        from pyxsd.mcp.snapshot_store import SnapshotStore
        from pyxsd.mcp.snapshots import Dependency
        from pyxsd.mcp.state import State
        from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

        source = tmp_path / "source"
        source.mkdir()
        (source / "root.xsd").write_bytes(b"opaque schema bytes")
        (source / "dep.xsd").write_bytes(b"original dependency")
        calls = []

        class Worker:
            async def discover(self, snapshot, refs):
                calls.append(tuple(item.path for item in snapshot.files))
                assert refs == (SchemaRef(source=FileRef(path="root.xsd")),)
                records = {item.path: item for item in snapshot.files}
                return tuple(
                    Dependency(path, records[path].digest, records[path].size)
                    if path in records
                    else Dependency(path, None, 0)
                    for path in ("root.xsd", "dep.xsd", "missing.xsd")
                )

        with State(tmp_path / "state") as state:
            workspaces = WorkspaceStore(state, Limits())
            owner = stdio_principal()
            workspace = await workspaces.create(owner)
            store = SnapshotStore(
                workspaces, source_root=lambda principal, identifier: source, discovery=Worker()
            )
            old = await store.put_files(
                owner,
                workspace.id,
                [InlineFile(path="old.xml", text="old")],
                expected_revision=None,
            )
            snapshot = await store.capture(
                owner, workspace.id, [SchemaRef(source=FileRef(path="root.xsd"))]
            )
            assert calls == [("root.xsd",), ("dep.xsd", "root.xsd")]
            assert [item.path for item in snapshot.files] == ["dep.xsd", "root.xsd"]
            assert (snapshot.root / "dep.xsd").read_bytes() == b"original dependency"
            assert (old.root / "old.xml").read_bytes() == b"old"
            assert await store.get(owner, workspace.id) == snapshot
            assert (
                await state.run(
                    lambda db: db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                )
                == 2
            )

    anyio.run(scenario)


@asynccontextmanager
async def capture_environment(tmp_path, worker, **budgets):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.snapshot_store import SnapshotStore
    from pyxsd.mcp.state import State
    from pyxsd.mcp.workspaces import WorkspaceStore, stdio_principal

    source = tmp_path / "source"
    source.mkdir()
    (source / "root.xsd").write_bytes(b"original")
    with State(tmp_path / "state") as state:
        workspaces = WorkspaceStore(state, Limits(**budgets))
        owner = stdio_principal()
        workspace = await workspaces.create(owner)
        store = SnapshotStore(workspaces, source_root=lambda *_: source, discovery=worker)
        yield source, state, store, owner, workspace


class FixedWorker:
    def __init__(self, paths=("root.xsd",), mutate=None):
        self.paths, self.mutate, self.calls = paths, mutate, 0

    async def discover(self, snapshot, refs):
        from pyxsd.mcp.snapshots import Dependency

        self.calls += 1
        if self.mutate is not None:
            self.mutate(self.calls)
        records = {item.path: item for item in snapshot.files}
        return tuple(
            Dependency(path, records[path].digest, records[path].size)
            if path in records
            else Dependency(path, None, 0)
            for path in self.paths
        )


@pytest.mark.parametrize("changes", [0, 1, 2])
def test_capture_retries_an_inconsistent_source_only_once(tmp_path, changes):
    async def scenario():
        from pyxsd.mcp.contracts import FileRef, SchemaRef
        from pyxsd.mcp.errors import ToolError

        worker = FixedWorker()
        async with capture_environment(tmp_path, worker) as (
            source,
            state,
            store,
            owner,
            workspace,
        ):

            def mutate(call):
                if call <= changes:
                    (source / "root.xsd").write_bytes(f"changed-{call}".encode())

            worker.mutate = mutate
            refs = [SchemaRef(source=FileRef(path="root.xsd"))]
            if changes == 2:
                with pytest.raises(ToolError, match="source_changed"):
                    await store.capture(owner, workspace.id, refs)
                assert (
                    await state.run(
                        lambda db: db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                    )
                    == 0
                )
                assert (
                    await state.run(
                        lambda db: db.execute(
                            "SELECT files+reserved_files FROM workspaces"
                        ).fetchone()[0]
                    )
                    == 0
                )
            else:
                snapshot = await store.capture(owner, workspace.id, refs)
                assert (snapshot.root / "root.xsd").read_bytes() == (
                    b"original" if changes == 0 else b"changed-1"
                )
            assert worker.calls == (1 if changes == 0 else 2)

    anyio.run(scenario)


@pytest.mark.parametrize(
    "case", ["unknown", "foreign", "scope", "unconfigured", "empty", "pinned", "missing"]
)
def test_capture_denials_do_not_publish_or_read_ungranted_sources(tmp_path, case):
    async def scenario():
        from pyxsd.mcp.contracts import FileRef
        from pyxsd.mcp.errors import ToolError
        from pyxsd.mcp.snapshot_store import SnapshotStore
        from pyxsd.mcp.workspaces import Principal

        worker = FixedWorker()
        async with capture_environment(tmp_path, worker) as (_, state, store, owner, workspace):
            refs = [FileRef(path="root.xsd")]
            identifier = workspace.id
            expected = "not_found"
            if case == "unknown":
                identifier = "unknown"
            elif case == "foreign":
                owner = Principal("other", owner.scopes)
            elif case == "scope":
                owner = Principal(owner.owner_id, frozenset({"pyxsd:read"}))
                expected = "forbidden"
            elif case == "unconfigured":
                store = SnapshotStore(store.workspaces)
                expected = "capture_unavailable"
            elif case == "empty":
                refs, expected = [], "invalid_argument"
            elif case == "pinned":
                refs, expected = [FileRef(path="root.xsd", revision="old")], "invalid_argument"
            else:
                refs = [FileRef(path="missing.xml")]
            with pytest.raises(ToolError, match=expected):
                await store.capture(owner, identifier, refs)
            assert worker.calls == 0
            assert (
                await state.run(
                    lambda db: db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                )
                == 0
            )

    anyio.run(scenario)


@pytest.mark.parametrize(
    "case", ["dependencies", "entries", "bytes", "bad_path", "schema_bytes", "closure_bytes"]
)
def test_capture_limits_and_dependency_paths_fail_closed(tmp_path, case):
    async def scenario():
        from pyxsd.mcp.contracts import FileRef, SchemaRef
        from pyxsd.mcp.errors import ToolError

        paths = ("root.xsd", "dep.xsd")
        budgets = {"schema_closure_files": 1} if case == "dependencies" else {}
        if case == "entries":
            budgets = {"snapshot_entries": 1}
        elif case == "bytes":
            budgets = {"xml_file_bytes": 1}
        elif case == "bad_path":
            paths = ("../outside",)
        elif case == "schema_bytes":
            budgets = {"schema_file_bytes": 1}
        elif case == "closure_bytes":
            budgets = {"schema_closure_bytes": 10}
        worker = FixedWorker(paths)
        async with capture_environment(tmp_path, worker, **budgets) as (
            source,
            state,
            store,
            owner,
            workspace,
        ):
            (source / "dep.xsd").write_bytes(b"dep")
            expected = {
                "dependencies": "schema_limit",
                "bad_path": "invalid_path",
                "schema_bytes": "schema_limit",
                "closure_bytes": "schema_limit",
            }.get(case, "quota_exceeded")
            with pytest.raises(ToolError, match=expected):
                await store.capture(
                    owner, workspace.id, [SchemaRef(source=FileRef(path="root.xsd"))]
                )
            assert (
                await state.run(
                    lambda db: db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                )
                == 0
            )

    anyio.run(scenario)


def test_capture_rechecks_absence_and_restarts_if_a_dependency_appears(tmp_path, monkeypatch):
    async def scenario():
        from pyxsd.mcp.contracts import FileRef
        from pyxsd.mcp.safe_io import SafeDirectory

        worker = FixedWorker(("root.xsd", "missing.xsd"))
        async with capture_environment(tmp_path, worker) as (source, _, store, owner, workspace):
            original = SafeDirectory.read
            appeared = False

            def read(directory, path, *, max_bytes):
                nonlocal appeared
                try:
                    return original(directory, path, max_bytes=max_bytes)
                except FileNotFoundError:
                    if path == "missing.xsd" and not appeared:
                        appeared = True
                        (source / path).write_bytes(b"appeared")
                    raise

            monkeypatch.setattr(SafeDirectory, "read", read)
            snapshot = await store.capture(owner, workspace.id, [FileRef(path="root.xsd")])
            assert worker.calls == 3
            assert (snapshot.root / "missing.xsd").read_bytes() == b"appeared"

    anyio.run(scenario)


def test_capture_cancellation_discards_private_discovery_staging(tmp_path):
    async def scenario():
        from pyxsd.mcp.contracts import FileRef

        started = anyio.Event()

        class Worker:
            async def discover(self, snapshot, refs):
                started.set()
                await anyio.sleep_forever()

        async with capture_environment(tmp_path, Worker()) as (_, state, store, owner, workspace):
            scopes = []

            async def capture():
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    await store.capture(owner, workspace.id, [FileRef(path="root.xsd")])

            with anyio.fail_after(5):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(capture)
                    await started.wait()
                    scopes[0].cancel()
            assert (
                await state.run(
                    lambda db: db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                )
                == 0
            )
            assert await state.run(
                lambda db: tuple(
                    db.execute("SELECT leases,files,reserved_files FROM workspaces").fetchone()
                )
            ) == (0, 0, 0)

    anyio.run(scenario)


@pytest.mark.parametrize("with_previous", [False, True])
def test_capture_does_not_replace_a_concurrent_managed_publication(tmp_path, with_previous):
    async def scenario():
        from pyxsd.mcp.contracts import FileRef, InlineFile
        from pyxsd.mcp.errors import ToolError

        worker = FixedWorker()
        async with capture_environment(tmp_path, worker) as (_, state, store, owner, workspace):
            previous = None
            if with_previous:
                previous = await store.put_files(
                    owner,
                    workspace.id,
                    [InlineFile(path="root.xsd", text="original")],
                    expected_revision=None,
                )
            discover = worker.discover
            published = []

            async def concurrent(snapshot, refs):
                published.append(
                    await store.put_files(
                        owner,
                        workspace.id,
                        [InlineFile(path="concurrent.xml", text="new")],
                        expected_revision=None if previous is None else previous.revision,
                    )
                )
                return await discover(snapshot, refs)

            worker.discover = concurrent
            with pytest.raises(ToolError, match="stale_reference"):
                await store.capture(owner, workspace.id, [FileRef(path="root.xsd")])
            assert await store.get(owner, workspace.id) == published[0]
            assert await state.run(
                lambda db: db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            ) == (2 if with_previous else 1)

    anyio.run(scenario)


def test_capture_seeds_namespace_schemas_and_does_not_mutate_caller_options(tmp_path):
    async def scenario():
        from pyxsd.mcp.contracts import CompileOptions, FileRef, SchemaRef

        worker = FixedWorker()
        async with capture_environment(tmp_path, worker) as (source, _, store, owner, workspace):
            (source / "extra.xsd").write_bytes(b"extra")
            ref = SchemaRef(
                source=FileRef(path="root.xsd"),
                options=CompileOptions(namespace_schemas={"urn:extra": FileRef(path="extra.xsd")}),
            )
            discover = worker.discover

            async def inspect(snapshot, refs):
                refs[0].options.namespace_schemas.clear()
                return await discover(snapshot, refs)

            worker.discover = inspect
            snapshot = await store.capture(owner, workspace.id, [ref])
            assert [item.path for item in snapshot.files] == ["extra.xsd", "root.xsd"]
            assert ref.options.namespace_schemas == {"urn:extra": FileRef(path="extra.xsd")}

    anyio.run(scenario)


def test_unchanged_capture_reuses_revision_even_at_retention_limit(tmp_path):
    async def scenario():
        from pyxsd.mcp.contracts import FileRef, SchemaRef

        worker = FixedWorker()
        async with capture_environment(tmp_path, worker, snapshot_revisions=1) as (
            _,
            state,
            store,
            owner,
            workspace,
        ):
            refs = [SchemaRef(source=FileRef(path="root.xsd"))]
            first = await store.capture(owner, workspace.id, refs)
            second = await store.capture(owner, workspace.id, refs)
            assert first == second
            assert worker.calls == 2
            assert (
                await state.run(
                    lambda db: db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                )
                == 1
            )

    anyio.run(scenario)


def test_capture_rechecks_staged_bytes_after_worker_discovery(tmp_path):
    async def scenario():
        from pyxsd.mcp.contracts import FileRef
        from pyxsd.mcp.errors import ToolError

        worker = FixedWorker()
        async with capture_environment(tmp_path, worker) as (_, state, store, owner, workspace):
            discover = worker.discover

            async def corrupt(snapshot, refs):
                result = await discover(snapshot, refs)
                (snapshot.root / "root.xsd").write_bytes(b"corrupted")
                return result

            worker.discover = corrupt
            with pytest.raises(ToolError, match="source_changed"):
                await store.capture(owner, workspace.id, [FileRef(path="root.xsd")])
            assert worker.calls == 2
            assert (
                await state.run(
                    lambda db: db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                )
                == 0
            )

    anyio.run(scenario)


def test_capture_removes_unreferenced_previous_files_even_when_root_is_unchanged(tmp_path):
    async def scenario():
        from pyxsd.mcp.contracts import FileRef, InlineFile

        worker = FixedWorker()
        async with capture_environment(tmp_path, worker) as (_, _, store, owner, workspace):
            old = await store.put_files(
                owner,
                workspace.id,
                [
                    InlineFile(path="root.xsd", text="original"),
                    InlineFile(path="extra", text="old"),
                ],
                expected_revision=None,
            )
            snapshot = await store.capture(owner, workspace.id, [FileRef(path="root.xsd")])
            assert [item.path for item in snapshot.files] == ["root.xsd"]
            assert (old.root / "extra").read_bytes() == b"old"
            assert worker.calls == 2

    anyio.run(scenario)
