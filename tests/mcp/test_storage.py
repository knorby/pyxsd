"""Lazy transactions own persistence, not a process-lifetime lock."""

import os
import sqlite3

import pytest

pytest.importorskip("pydantic")
import anyio

from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.storage import Storage


def test_storage_creates_private_state_only_on_demand(tmp_path):
    root = tmp_path / "new/nested"
    with Storage(root) as storage:
        assert storage.root == root
        assert storage.format_root == root / "store-v2"
        assert storage.scratch.is_dir()
        assert not root.exists()
        storage._run(lambda db: True)
        for directory in (root, storage.format_root, storage.artifacts, storage.scratch):
            assert os.stat(directory).st_mode & 0o777 == 0o700
        assert not (root / "coordinator.lock").exists()


def test_storage_accepts_two_owners(tmp_path):
    with Storage(tmp_path / "store") as first, Storage(tmp_path / "store") as second:
        assert first._run(lambda db: 1) == 1
        assert second._run(lambda db: 2) == 2


def test_storage_rejects_unsafe_or_foreign_directories_on_use(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    for root in (unsafe, linked):
        with Storage(root) as storage, pytest.raises(ToolError, match="unsafe_state_directory"):
            storage._run(lambda db: True)


def test_temporary_storage_cleans_only_its_own_scratch(tmp_path):
    requested = tmp_path / "requested"
    storage = Storage(requested, persist=False)
    root = storage.scratch
    with Storage(requested, persist=False) as other:
        assert storage.root != other.root
        storage._run(lambda db: True)
        assert storage.artifacts.is_dir()
        storage.close()
        storage.close()
        assert other.scratch.is_dir()
    assert not root.exists()
    assert not requested.exists()


def test_storage_never_touches_legacy_files(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir(mode=0o700)
    database = root / "state.sqlite3"
    database.write_bytes(b"legacy")
    with Storage(root) as storage:
        storage._run(lambda db: True)
    assert database.read_bytes() == b"legacy"


def test_unknown_index_version_is_not_rewritten(tmp_path):
    with Storage(tmp_path / "state") as storage:
        storage._run(lambda db: db.execute("PRAGMA user_version = 99").close())
        index = storage.format_root / "index.sqlite3"
        before = index.read_bytes()
        with pytest.raises(ToolError, match="state_version"):
            storage._run(lambda db: True)
        assert index.read_bytes() == before


def test_initialization_failure_is_typed_and_can_retry(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("disk failure")

    with Storage(tmp_path / "broken") as storage:
        monkeypatch.setattr("pyxsd.mcp.storage.sqlite3.connect", fail)
        with pytest.raises(ToolError, match="state_io"):
            storage._run(lambda db: True)
        monkeypatch.undo()
        assert storage._run(lambda db: True) is True


def test_transactions_rollback_and_closed_store_fails(tmp_path):
    async def scenario():
        storage = Storage(tmp_path / "transactions")
        try:
            await storage.run(lambda db: db.execute("CREATE TABLE check_one(value TEXT)").close())

            def fail(db):
                db.execute("INSERT INTO check_one VALUES('discarded')")
                raise RuntimeError("rollback")

            with pytest.raises(RuntimeError, match="rollback"):
                await storage.run(fail)
            assert (
                await storage.run(
                    lambda db: db.execute("SELECT count(*) FROM check_one").fetchone()[0]
                )
                == 0
            )
        finally:
            storage.close()
        with pytest.raises(ToolError, match="state_closed"):
            await storage.run(lambda db: None)

    anyio.run(scenario)


def test_storage_close_tolerates_cleanup_failure(tmp_path, monkeypatch, caplog):
    storage = Storage(tmp_path / "cleanup", persist=False)

    def fail():
        raise OSError("cannot remove")

    monkeypatch.setattr(storage._temporary, "cleanup", fail)
    storage.close()
    assert "Unable to remove temporary MCP state" in caplog.text


def test_storage_rejects_unsupported_platform(tmp_path, monkeypatch):
    monkeypatch.delattr("pyxsd.mcp.storage.os.O_NOFOLLOW")
    with pytest.raises(ToolError, match="unsupported_platform"):
        Storage(tmp_path / "unsupported")


def test_index_symlink_is_rejected(tmp_path):
    with Storage(tmp_path / "state") as storage:
        storage._run(lambda db: True)
        index = storage.format_root / "index.sqlite3"
    index.unlink()
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    index.symlink_to(target)
    with (
        Storage(tmp_path / "state") as storage,
        pytest.raises(ToolError, match="unsafe_state_directory"),
    ):
        storage._run(lambda db: True)
    assert target.read_bytes() == b"untouched"


def test_nonbusy_sqlite_error_is_not_misclassified(tmp_path):
    with (
        Storage(tmp_path / "state") as storage,
        pytest.raises(sqlite3.OperationalError, match="no such table"),
    ):
        storage._run(lambda db: db.execute("SELECT * FROM absent"))


@pytest.mark.parametrize(
    "code, expected",
    [
        (sqlite3.SQLITE_READONLY, "state_io"),
        (sqlite3.SQLITE_CANTOPEN, "state_io"),
        (sqlite3.SQLITE_CORRUPT, "state_corrupt"),
    ],
)
def test_sqlite_storage_failures_are_typed_without_retry(tmp_path, monkeypatch, code, expected):
    def fail(*args, **kwargs):
        error = sqlite3.OperationalError("storage failure")
        error.sqlite_errorcode = code
        raise error

    monkeypatch.setattr("pyxsd.mcp.storage.sqlite3.connect", fail)
    with Storage(tmp_path / "state") as storage, pytest.raises(ToolError, match=expected) as raised:
        storage._run(lambda db: True)
    assert raised.value.fault.retryable is False


def test_cli_scope_and_conflicting_flags(tmp_path, monkeypatch):
    from pyxsd.mcp.__main__ import main

    captured = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pyxsd.mcp.server.run", captured.append)
    assert main([]) == 0
    assert captured[-1].state_dir == tmp_path / ".pyxsd/mcp"
    assert captured[-1].persist is True
    assert main(["--state-dir", str(tmp_path / "custom")]) == 0
    assert captured[-1].state_dir == tmp_path / "custom"
    nested = tmp_path / "nested"
    nested.mkdir()
    assert main(["--project-root", str(nested)]) == 0
    assert captured[-1].state_dir == nested / ".pyxsd/mcp"
    with pytest.raises(SystemExit) as raised:
        main(["--no-persist", "--state-dir", str(tmp_path / "other")])
    assert raised.value.code == 2
