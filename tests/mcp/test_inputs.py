"""Workspaces are session input sets; captures are verified temporary copies."""

import threading

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio

from pyxsd.mcp.captures import CaptureDependency
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, InlineFile, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.inputs import InputCapture, SessionInputs

XSD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="root" type="xs:string"/></xs:schema>'
)
DOC = "<root>value</root>"


def test_put_files_empty_is_noop(tmp_path):
    async def scenario():
        store = inputs(tmp_path)
        workspace = await store.create()
        await store.put_files(workspace.id, [InlineFile(path="a.xml", text=DOC)])
        directory = store._root / workspace.id / "files"
        before_stat = directory.stat()
        async with store.capture(workspace.id) as before:
            expected, generation = before.files, before.generation
        await store.put_files(workspace.id, [])
        assert directory.stat().st_ino == before_stat.st_ino
        assert directory.stat().st_mtime_ns == before_stat.st_mtime_ns
        manifest = await store.manifest(workspace.id)
        assert manifest.files == expected
        assert manifest.generation == generation
        async with store.capture(workspace.id) as after:
            assert after.generation == generation
            assert after.files == expected
        with pytest.raises(ToolError, match="not_found"):
            await store.put_files("missing", [])
        with pytest.raises(ToolError, match="not_found"):
            await store.manifest("missing")

    anyio.run(scenario)


def inputs(root, limits=None, **kwargs):
    return SessionInputs(root / "scratch", limits or Limits(workspaces=4), **kwargs)


def test_workspaces_are_session_only_and_outputs_are_independent(tmp_path):
    async def scenario():
        first = inputs(tmp_path)
        one = await first.create()
        two = await first.create()
        assert [item.id for item in await first.list()] == [one.id, two.id]
        await first.put_files(one.id, [InlineFile(path="schema.xsd", text=XSD)])
        await first.release(one.id)
        assert [item.id for item in await first.list()] == [two.id]
        with pytest.raises(ToolError, match="not_found"):
            async with first.capture(one.id):
                pass
        with pytest.raises(ToolError, match="not_found"):
            await first.put_files(one.id, [InlineFile(path="a.xml", text="<a/>")])
        with pytest.raises(ToolError, match="not_found"):
            await first.release(one.id)
        restarted = inputs(tmp_path)
        assert await restarted.list() == []
        with pytest.raises(ToolError, match="not_found"):
            async with restarted.capture(two.id):
                pass

    anyio.run(scenario)


def test_workspace_limit_and_invalid_identifiers(tmp_path):
    async def scenario():
        store = inputs(tmp_path, Limits(workspaces=1))
        workspace = await store.create()
        with pytest.raises(ToolError, match="quota_exceeded"):
            await store.create()
        with pytest.raises(ToolError, match="not_found"):
            await store.put_files("../bad", [InlineFile(path="a.xml", text="<a/>")])
        assert (await store.list())[0].id == workspace.id

    anyio.run(scenario)


def test_failed_batches_leave_the_previous_input_set_intact(tmp_path):
    async def scenario():
        store = inputs(tmp_path)
        workspace = await store.create()
        await store.put_files(workspace.id, [InlineFile(path="schema.xsd", text=XSD)])
        with pytest.raises(ToolError, match="invalid_path"):
            await store.put_files(
                workspace.id,
                [
                    InlineFile(path="document.xml", text=DOC),
                    InlineFile(path="../escape.xml", text="bad"),
                ],
            )
        async with store.capture(workspace.id) as capture:
            assert [item.path for item in capture.files] == ["schema.xsd"]

    anyio.run(scenario)


def test_capture_is_a_verified_temporary_copy_with_a_generation(tmp_path):
    async def scenario():
        store = inputs(tmp_path)
        workspace = await store.create()
        await store.put_files(
            workspace.id,
            [
                InlineFile(path="schema.xsd", text=XSD),
                InlineFile(path="nested/document.xml", text=DOC),
            ],
        )
        async with store.capture(workspace.id) as capture:
            assert isinstance(capture, InputCapture)
            assert capture.workspace_id == workspace.id
            assert capture.generation == 1
            assert capture.root.is_dir()
            assert [item.path for item in capture.files] == ["nested/document.xml", "schema.xsd"]
            assert (capture.root / "schema.xsd").read_text() == XSD
            assert (capture.root / "nested/document.xml").read_text() == DOC
            root = capture.root
        assert not root.exists()

    anyio.run(scenario)


def test_capture_is_removed_after_an_exception(tmp_path):
    async def scenario():
        store = inputs(tmp_path)
        workspace = await store.create()
        await store.put_files(workspace.id, [InlineFile(path="a.xml", text="<a/>")])
        root = None
        with pytest.raises(RuntimeError, match="operation failed"):
            async with store.capture(workspace.id) as capture:
                root = capture.root
                raise RuntimeError("operation failed")
        assert root is not None and not root.exists()

    anyio.run(scenario)


def test_transient_entry_and_byte_limits_apply_per_input_set(tmp_path):
    async def scenario():
        limits = Limits(
            workspaces=2,
            input_entries=3,
            workspace_bytes=16,
            xml_file_bytes=16,
            inline_bytes=64,
        )
        store = inputs(tmp_path, limits)
        workspace = await store.create()
        with pytest.raises(ToolError, match="quota_exceeded"):
            await store.put_files(
                workspace.id,
                [
                    InlineFile(path="a/b/c.xml", text="<a/>"),
                    InlineFile(path="a/b/d.xml", text="<b/>"),
                ],
            )
        with pytest.raises(ToolError, match="quota_exceeded"):
            await store.put_files(workspace.id, [InlineFile(path="large.xml", text="x" * 17)])
        await store.put_files(workspace.id, [InlineFile(path="a/b.xml", text="<a/>")])
        async with store.capture(workspace.id) as capture:
            assert capture.generation == 1

    anyio.run(scenario)


def test_capture_and_upload_cannot_observe_a_partial_batch(tmp_path, monkeypatch):
    async def scenario():
        store = inputs(tmp_path)
        workspace = await store.create()
        await store.put_files(workspace.id, [InlineFile(path="old.xml", text="<old/>")])
        reached, release = threading.Event(), threading.Event()
        original = store._write_batch

        def blocked(root, files):
            reached.set()
            release.wait(timeout=10)
            original(root, files)

        monkeypatch.setattr(store, "_write_batch", blocked)
        visible = []

        async def upload():
            await store.put_files(
                workspace.id,
                [InlineFile(path="new.xml", text="<new/>")],
            )

        async def capture():
            async with store.capture(workspace.id) as snapshot:
                visible.append([item.path for item in snapshot.files])

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(upload)
            await anyio.to_thread.run_sync(reached.wait)
            tasks.start_soon(capture)
            await anyio.sleep(0.02)
            assert visible == []
            release.set()
        assert visible == [["new.xml", "old.xml"]]

    anyio.run(scenario)


def test_source_capture_materializes_files_for_real_worker_discovery(tmp_path):
    from pyxsd.mcp.workers import WorkerPool

    source = tmp_path / "source"
    source.mkdir()
    (source / "schema.xsd").write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:include schemaLocation="included.xsd"/>'
        '<xs:element name="root" type="xs:string"/></xs:schema>'
    )
    (source / "included.xsd").write_text(XSD)
    pool = WorkerPool(Limits(workers=1))

    async def scenario():
        try:
            store = inputs(tmp_path, source_root=source, discovery=pool)
            workspace = await store.create()
            refs = (SchemaRef(source=FileRef(path="schema.xsd")),)
            async with store.capture_source(workspace.id, refs) as capture:
                assert [item.path for item in capture.files] == ["included.xsd", "schema.xsd"]
                assert (capture.root / "schema.xsd").is_file()
                assert (capture.root / "included.xsd").is_file()
        finally:
            await pool.aclose()

    anyio.run(scenario)


@pytest.mark.parametrize("has_previous", [False, True])
def test_failed_upload_rename_restores_the_previous_input_set(tmp_path, monkeypatch, has_previous):
    from pathlib import Path

    async def scenario():
        store = inputs(tmp_path)
        workspace = await store.create()
        if has_previous:
            await store.put_files(workspace.id, [InlineFile(path="old.xml", text="<old/>")])
        original = Path.rename

        def fail_staged_rename(self, target):
            if self.name.startswith("upload-"):
                raise OSError("simulated rename failure")
            return original(self, target)

        monkeypatch.setattr(Path, "rename", fail_staged_rename)
        with pytest.raises(OSError, match="simulated rename failure"):
            await store.put_files(workspace.id, [InlineFile(path="new.xml", text="<new/>")])
        monkeypatch.setattr(Path, "rename", original)

        async with store.capture(workspace.id) as capture:
            assert [item.path for item in capture.files] == (["old.xml"] if has_previous else [])
            if has_previous:
                assert (capture.root / "old.xml").read_text() == "<old/>"

    anyio.run(scenario)


def test_source_capture_discovers_dependencies_and_retries_one_change(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "schema.xsd").write_text(XSD)
    (source / "document.xml").write_text(DOC)
    (source / "import.xsd").write_text(XSD)
    calls = []
    changes = [True, False, False]

    class Discovery:
        async def discover(self, capture, refs):
            calls.append((capture.generation, tuple(ref.source.path for ref in refs)))
            if changes.pop(0):
                (source / "schema.xsd").write_text(XSD + " ")
            return (CaptureDependency("import.xsd", None, 0),)

    async def scenario():
        store = inputs(tmp_path, source_root=source, discovery=Discovery())
        workspace = await store.create()
        refs = (SchemaRef(source=FileRef(path="schema.xsd")),)
        async with store.capture_source(workspace.id, refs) as capture:
            assert [item.path for item in capture.files] == ["import.xsd", "schema.xsd"]
            assert capture.generation == 0
        assert len(calls) == 3

    anyio.run(scenario)


def test_source_capture_requires_configuration_and_unpinned_paths(tmp_path):
    from pydantic import ValidationError

    async def scenario():
        store = inputs(tmp_path)
        workspace = await store.create()
        with pytest.raises(ToolError, match="capture_unavailable"):
            async with store.capture_source(workspace.id, (FileRef(path="a.xml"),)):
                pass
        with pytest.raises(ValidationError):
            FileRef(path="a.xml", revision="one")

    anyio.run(scenario)


def test_create_collision_exhaustion_and_empty_capture(tmp_path, monkeypatch):
    import secrets

    original = secrets.token_hex

    async def scenario():
        store = inputs(tmp_path)
        (store._root / ("a" * 32)).mkdir()
        monkeypatch.setattr(secrets, "token_hex", lambda _n: "a" * 32)
        with pytest.raises(ToolError, match="allocation_conflict"):
            await store.create()
        monkeypatch.setattr(secrets, "token_hex", original)
        workspace = await store.create()
        async with store.capture(workspace.id) as capture:
            assert capture.files == ()

    anyio.run(scenario)


def test_input_byte_limit_and_tampered_area_detection(tmp_path):
    async def scenario():
        limits = Limits(workspace_bytes=8, xml_file_bytes=64, inline_bytes=64)
        store = inputs(tmp_path, limits)
        workspace = await store.create()
        with pytest.raises(ToolError, match="quota_exceeded"):
            await store.put_files(workspace.id, [InlineFile(path="a.xml", text="x" * 9)])
        await store.put_files(workspace.id, [InlineFile(path="a.xml", text="<a/>")])
        (store._root / workspace.id / "files/a.xml").write_bytes(b"tampered")
        with pytest.raises(ToolError, match="source_changed"):
            async with store.capture(workspace.id):
                pass

    anyio.run(scenario)


def test_source_capture_reports_missing_and_nonretry_failures(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "schema.xsd").write_text(XSD)

    class EmptyDiscovery:
        async def discover(self, capture, refs):
            return ()

    async def scenario():
        store = inputs(tmp_path / "store", source_root=tree, discovery=EmptyDiscovery())
        workspace = await store.create()
        with pytest.raises(ToolError, match="invalid_argument"):
            async with store.capture_source(workspace.id, ()):
                pass
        with pytest.raises(ToolError, match="not_found"):
            async with store.capture_source(workspace.id, (FileRef(path="missing.xml"),)):
                pass

    anyio.run(scenario)


def test_source_capture_schema_and_entry_limits(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "schema.xsd").write_text(XSD)

    class Discovery:
        async def discover(self, capture, refs):
            return (CaptureDependency("extra.xsd", None, 0),)

    async def scenario():
        limits = Limits(schema_closure_files=2, input_entries=1)
        store = inputs(tmp_path / "store", limits, source_root=tree, discovery=Discovery())
        workspace = await store.create()
        refs = (SchemaRef(source=FileRef(path="schema.xsd")),)
        with pytest.raises(ToolError, match="quota_exceeded"):
            async with store.capture_source(workspace.id, refs):
                pass
        large = inputs(
            tmp_path / "large",
            Limits(schema_file_bytes=4),
            source_root=tree,
            discovery=Discovery(),
        )
        workspace = await large.create()
        with pytest.raises(ToolError, match="schema_limit"):
            async with large.capture_source(workspace.id, refs):
                pass

    anyio.run(scenario)


def test_missing_source_dependency_is_recorded_without_failing_capture(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "schema.xsd").write_text(XSD)

    class Discovery:
        async def discover(self, capture, refs):
            return (CaptureDependency("missing.xsd", None, 0),)

    async def scenario():
        store = inputs(tmp_path / "store", source_root=tree, discovery=Discovery())
        workspace = await store.create()
        refs = (SchemaRef(source=FileRef(path="schema.xsd")),)
        async with store.capture_source(workspace.id, refs) as capture:
            assert [item.path for item in capture.files] == ["schema.xsd"]

    anyio.run(scenario)


def test_capture_source_returns_real_workspace_id(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "schema.xsd").write_text(XSD)

    class Discovery:
        async def discover(self, capture, refs):
            return ()

    async def scenario():
        store = inputs(tmp_path / "store", source_root=tree, discovery=Discovery())
        workspace = await store.create()
        refs = (SchemaRef(source=FileRef(path="schema.xsd")),)
        async with store.capture_source(workspace.id, refs) as capture:
            assert capture.workspace_id == workspace.id
            assert capture.generation == 0

    anyio.run(scenario)
