"""Persistent outputs are immutable files with a small restart-readable index."""

import hashlib
import json
import os
import secrets
import sqlite3

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")

import anyio

from pyxsd.mcp.config import Limits
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.output_store import OutputStore
from pyxsd.mcp.storage import Storage


def limits(**overrides):
    fields = {"output_bytes": 1024, "page_items": 3, "result_bytes": 1024}
    fields.update(overrides)
    return Limits(**fields)


def execute(storage, sql, parameters=()):
    """Inject/inspect database fixtures through the synchronized transaction API."""
    return storage._run(lambda db: db.execute(sql, parameters))


def initialize(store):
    store._run(lambda db: True, create=True)


def test_binary_roundtrip_ranges_and_restart(tmp_path):
    content = b"\x00prefix-\xff-suffix\x00"

    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits())
            ref = await store.put_bytes(
                content,
                name="candidate.xml",
                media_type="application/xml",
                kind="candidate",
            )
            assert ref.path == storage.artifacts / ref.id / "candidate.xml"
            assert ref.size == len(content)
            assert ref.digest == hashlib.sha256(content).hexdigest()
            assert await store.get(ref.id) == ref
            assert await store.read(ref.id, offset=0, length=8) == content[:8]
            assert await store.read(ref.id, offset=8, length=8) == content[8:16]
            assert await store.read(ref.id, offset=len(content), length=8) == b""
            store.close()
        with Storage(tmp_path / "state") as storage:
            reopened = OutputStore(storage, limits())
            assert await reopened.get(ref.id) == ref
            assert await reopened.read(ref.id, offset=0, length=len(content)) == content

    anyio.run(scenario)


def test_index_has_only_justified_artifact_metadata(tmp_path):
    with Storage(tmp_path / "state") as storage:
        initialize(OutputStore(storage, limits()))
        rows = execute(storage, "PRAGMA table_info(artifacts)").fetchall()
        assert [row["name"] for row in rows] == [
            "id",
            "path",
            "media_type",
            "size",
            "digest",
            "created",
            "kind",
            "metadata",
        ]
        assert execute(storage, "PRAGMA user_version").fetchone()[0] == 1
        tables = {
            row[0]
            for row in execute(
                storage, "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables == {"artifacts"}


def test_unknown_index_version_is_not_rewritten(tmp_path):
    with Storage(tmp_path / "state") as storage:
        execute(storage, "PRAGMA user_version = 2")
        before = (storage.format_root / "index.sqlite3").read_bytes()
        with pytest.raises(ToolError, match="state_version"):
            initialize(OutputStore(storage, limits()))
        after = (storage.format_root / "index.sqlite3").read_bytes()
        assert after == before


def test_metadata_is_bounded_and_has_no_lifecycle_columns(tmp_path):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits(result_bytes=48))
            ref = await store.put_bytes(
                b"x",
                name="report.json",
                media_type="application/json",
                kind="report",
                metadata={"issue_count": 2},
            )
            row = execute(storage, "SELECT metadata FROM artifacts").fetchone()
            assert json.loads(row["metadata"]) == {"issue_count": 2}
            with pytest.raises(ToolError, match="metadata_limit"):
                await store.put_bytes(
                    b"x",
                    name="large.json",
                    media_type="application/json",
                    kind="report",
                    metadata={"pad": "x" * 64},
                )
            assert ref.kind == "report"

    anyio.run(scenario)


def test_file_names_media_types_kinds_and_limits_are_validated(tmp_path):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits(output_bytes=4, page_items=2))
            for name in ("", "../escape", "a/b", ".hidden", "..", "a" * 129):
                with pytest.raises(ToolError, match="invalid_argument"):
                    await store.put_bytes(b"x", name=name, media_type="text/xml", kind="candidate")
            with pytest.raises(ToolError, match="invalid_argument"):
                await store.put_bytes(b"x", name="ok.xml", media_type="bad", kind="candidate")
            with pytest.raises(ToolError, match="invalid_argument"):
                await store.put_bytes(b"x", name="ok.xml", media_type="text/xml", kind="")
            with pytest.raises(ToolError, match="quota_exceeded"):
                await store.put_bytes(
                    b"12345", name="ok.xml", media_type="text/xml", kind="candidate"
                )
            assert not storage.artifacts.exists()

    anyio.run(scenario)


def test_tampered_and_missing_files_are_not_substituted(tmp_path):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits())
            ref = await store.put_bytes(
                b"original", name="candidate.xml", media_type="application/xml", kind="candidate"
            )
            ref.path.write_bytes(b"tampered")
            with pytest.raises(ToolError, match="artifact_changed"):
                await store.get(ref.id)
            with pytest.raises(ToolError, match="artifact_changed"):
                await store.read(ref.id, offset=0, length=4)
            ref.path.unlink()
            with pytest.raises(ToolError, match="artifact_missing"):
                await store.get(ref.id)
            with pytest.raises(ToolError, match="not_found"):
                await store.get("missing")

    anyio.run(scenario)


def test_listing_is_deterministic_bounded_and_keyset_paged(tmp_path):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits(page_items=2))
            refs = []
            for index in range(5):
                refs.append(
                    await store.put_bytes(
                        str(index).encode(),
                        name=f"result-{index}.json",
                        media_type="application/json",
                        kind="result",
                    )
                )
            first, after = await store.list(limit=2)
            ordered = sorted(refs, key=lambda item: (item.created_at, item.id))
            assert [item.id for item in first] == [item.id for item in ordered[:2]]
            second, after = await store.list(after=after, limit=2)
            assert [item.id for item in second] == [item.id for item in ordered[2:4]]
            third, after = await store.list(after=after, limit=2)
            assert [item.id for item in third] == [ordered[4].id]
            assert after is None
            with pytest.raises(ToolError, match="invalid_argument"):
                await store.list(limit=0)
            with pytest.raises(ToolError, match="invalid_argument"):
                await store.list(after=("bad", "id"), limit=2)

    anyio.run(scenario)


def test_delete_is_idempotent_and_never_deletes_legacy_state(tmp_path):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            legacy = storage.root / "workspaces" / "old"
            storage.root.mkdir(mode=0o700)
            legacy.mkdir(parents=True)
            marker = legacy / "keep.xml"
            marker.write_bytes(b"legacy")
            store = OutputStore(storage, limits())
            ref = await store.put_bytes(
                b"value",
                name="export.parquet",
                media_type="application/octet-stream",
                kind="export",
            )
            await store.delete(ref.id)
            assert not ref.path.exists()
            with pytest.raises(ToolError, match="not_found"):
                await store.get(ref.id)
            await store.delete(ref.id)
            assert marker.read_bytes() == b"legacy"

    anyio.run(scenario)


def test_anchor_identifiers_collide_cleanly_and_never_reuse_ids(tmp_path, monkeypatch):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits())
            first = await store.put_bytes(
                b"one", name="a.xml", media_type="application/xml", kind="candidate"
            )
            await store.delete(first.id)
            (storage.artifacts / first.id).mkdir()
            monkeypatch.setattr(secrets, "token_hex", lambda _n: first.id)
            with pytest.raises(ToolError, match="allocation_conflict") as raised:
                await store.put_bytes(
                    b"two", name="b.xml", media_type="application/xml", kind="candidate"
                )
            assert raised.value.fault.retryable is True

    anyio.run(scenario)


def test_startup_preserves_unindexed_new_store_files(tmp_path):
    with Storage(tmp_path / "state") as storage:
        initialize(OutputStore(storage, limits()))
        orphan = storage.artifacts / "orphan"
        orphan.mkdir()
        (orphan / "stale.xml").write_bytes(b"stale")
        legacy = storage.root / "workspaces" / "legacy"
        legacy.mkdir(parents=True)
        (legacy / "keep.xml").write_bytes(b"keep")
        OutputStore(storage, limits())
        assert (orphan / "stale.xml").read_bytes() == b"stale"
        assert (legacy / "keep.xml").read_bytes() == b"keep"


def test_failed_index_insert_removes_finished_file(tmp_path):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits())
            initialize(store)
            execute(
                storage,
                "CREATE TRIGGER reject BEFORE INSERT ON artifacts "
                "BEGIN SELECT RAISE(ABORT, 'insert failed'); END",
            )
            with pytest.raises(sqlite3.IntegrityError, match="insert failed"):
                await store.put_bytes(
                    b"value", name="candidate.xml", media_type="application/xml", kind="candidate"
                )
            assert not any(storage.artifacts.iterdir())

    anyio.run(scenario)


@pytest.mark.parametrize("failure", ["fsync", "allocation"])
def test_output_write_failure_is_typed_and_retryable_after_repair(tmp_path, monkeypatch, failure):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits())

            def denied(*args):
                raise PermissionError("fsync denied")

            if failure == "fsync":
                monkeypatch.setattr(os, "fsync", denied)
            else:
                monkeypatch.setattr(store, "_allocate", denied)
            with pytest.raises(ToolError, match="state_io"):
                await store.put_bytes(b"x", name="a.txt", media_type="text/plain", kind="result")
            assert await store.list(limit=3) == ([], None)
            assert not any(storage.artifacts.iterdir())
            monkeypatch.undo()
            ref = await store.put_bytes(b"x", name="a.txt", media_type="text/plain", kind="result")
            assert await store.read(ref.id, offset=0, length=1) == b"x"

    anyio.run(scenario)


def test_read_write_delete_are_coordinated(tmp_path):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits())
            ref = await store.put_bytes(
                b"abcdefgh", name="candidate.xml", media_type="application/xml", kind="candidate"
            )
            started = anyio.Event()
            release = anyio.Event()
            outcomes = []

            async def blocked_read():
                started.set()
                await release.wait()
                try:
                    outcomes.append(await store.read(ref.id, offset=0, length=4))
                except ToolError as exc:
                    outcomes.append(exc.fault.code)

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(blocked_read)
                await started.wait()
                await store.delete(ref.id)
                release.set()
            assert outcomes == ["not_found"]
            assert not ref.path.exists()

    anyio.run(scenario)


def test_index_schema_missing_table_is_rejected(tmp_path):
    with Storage(tmp_path / "state") as storage:
        execute(storage, "PRAGMA user_version = 1")
        with pytest.raises(ToolError, match="state_version"):
            initialize(OutputStore(storage, limits()))


def test_invalid_metadata_content_types_and_closed_store_are_rejected(tmp_path, monkeypatch):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits())
            with pytest.raises(ToolError, match="invalid_argument"):
                await store.put_bytes(
                    b"x", name="a.json", media_type="application/json", kind="report", metadata=[]
                )
            with pytest.raises(ToolError, match="invalid_argument"):
                await store.put_bytes(
                    b"x",
                    name="a.json",
                    media_type="application/json",
                    kind="report",
                    metadata={"bad": object()},
                )
            with pytest.raises(ToolError, match="invalid_argument"):
                await store.put_bytes(
                    bytearray(b"x"),
                    name="a.xml",
                    media_type="application/xml",
                    kind="candidate",
                )
            ref = await store.put_bytes(
                b"x", name="a.xml", media_type="application/xml", kind="candidate"
            )
            for method in (
                lambda: store.get(ref.id),
                lambda: store.read(ref.id, offset=0, length=1),
                lambda: store.list(limit=1),
                lambda: store.delete(ref.id),
            ):
                store.close()
                with pytest.raises(ToolError, match="state_closed"):
                    await method()
                store._closed = False

            def unreadable(*_args, **_kwargs):
                raise PermissionError("denied")

            with monkeypatch.context() as patch:
                patch.setattr(os, "open", unreadable)
                with pytest.raises(ToolError, match="artifact_changed"):
                    await store.get(ref.id)

    anyio.run(scenario)


def test_malicious_paths_and_nonregular_files_are_rejected(tmp_path):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits())
            ref = await store.put_bytes(
                b"value", name="candidate.xml", media_type="application/xml", kind="candidate"
            )
            execute(storage, "UPDATE artifacts SET path='../outside.xml' WHERE id=?", (ref.id,))
            with pytest.raises(ToolError, match="artifact_changed"):
                await store.get(ref.id)
            execute(
                storage,
                "UPDATE artifacts SET path=? WHERE id=?",
                (
                    str(
                        (storage.artifacts / ref.id / "candidate.xml").relative_to(
                            storage.format_root
                        )
                    ),
                    ref.id,
                ),
            )
            ref.path.unlink()
            ref.path.mkdir()
            with pytest.raises(ToolError, match="artifact_changed"):
                await store.get(ref.id)

    anyio.run(scenario)


def test_invalid_ranges_and_listing_positions_are_rejected(tmp_path):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits())
            ref = await store.put_bytes(
                b"value", name="candidate.xml", media_type="application/xml", kind="candidate"
            )
            for offset, length in ((-1, 1), (0, 0), ("0", 1), (0, "1")):
                with pytest.raises(ToolError, match="invalid_argument"):
                    await store.read(ref.id, offset=offset, length=length)
            with pytest.raises(ToolError, match="invalid_argument"):
                await store.list(after=(0.0, "../bad"), limit=1)

    anyio.run(scenario)


def test_delete_failure_leaves_artifact_readable_and_bad_ids_are_ignored(tmp_path):
    async def scenario():
        with Storage(tmp_path / "state") as storage:
            store = OutputStore(storage, limits())
            ref = await store.put_bytes(
                b"value", name="candidate.xml", media_type="application/xml", kind="candidate"
            )
            execute(
                storage,
                "CREATE TRIGGER reject_delete BEFORE DELETE ON artifacts "
                "BEGIN SELECT RAISE(ABORT, 'delete failed'); END",
            )
            with pytest.raises(sqlite3.IntegrityError, match="delete failed"):
                await store.delete(ref.id)
            assert await store.get(ref.id) == ref
            execute(storage, "DROP TRIGGER reject_delete")
            await store.delete("../bad")
            assert await store.get(ref.id) == ref

    anyio.run(scenario)


def test_startup_leaves_unindexed_files_and_links_untouched(tmp_path):
    with Storage(tmp_path / "state") as storage:
        initialize(OutputStore(storage, limits()))
        target = tmp_path / "outside"
        target.write_bytes(b"outside")
        unindexed = storage.artifacts / "unindexed"
        unindexed.mkdir()
        (unindexed / "stale").write_bytes(b"stale")
        file_orphan = storage.artifacts / "file-orphan"
        file_orphan.write_bytes(b"file")
        link_orphan = storage.artifacts / "link-orphan"
        link_orphan.symlink_to(target)
        OutputStore(storage, limits())
        assert (unindexed / "stale").read_bytes() == b"stale"
        assert file_orphan.read_bytes() == b"file"
        assert link_orphan.is_symlink()
        assert target.read_bytes() == b"outside"
