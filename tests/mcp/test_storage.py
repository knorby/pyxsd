"""The local store owns startup, persistence, locking, and cleanup."""

import os
import sqlite3

import pytest

pytest.importorskip("pydantic")

from pyxsd.mcp.errors import ToolError


def test_storage_creates_a_private_new_format_store(tmp_path):
    from pyxsd.mcp.storage import Storage

    root = tmp_path / "new" / "nested"
    with Storage(root) as storage:
        assert storage.root == root
        assert storage.format_root == root / "store-v2"
        assert storage.artifacts.is_dir()
        assert storage.scratch.is_dir()
        assert storage.lock_path == root / "coordinator.lock"
        assert stat_mode(root) == 0o700
        assert stat_mode(storage.format_root) == 0o700
        assert stat_mode(storage.artifacts) == 0o700
        assert stat_mode(storage.scratch) == 0o700


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def test_storage_rejects_a_second_owner(tmp_path):
    from pyxsd.mcp.storage import Storage

    first = Storage(tmp_path / "store")
    try:
        with pytest.raises(ToolError, match="state_locked"):
            Storage(tmp_path / "store")
    finally:
        first.close()


def test_storage_rejects_unsafe_or_foreign_directories(tmp_path):
    from pyxsd.mcp.storage import Storage

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    with pytest.raises(ToolError, match="unsafe_state_directory"):
        Storage(unsafe)

    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ToolError, match="unsafe_state_directory"):
        Storage(linked)


def test_temporary_storage_uses_an_independent_root_and_cleans_up(tmp_path):
    from pyxsd.mcp.storage import Storage

    requested = tmp_path / "requested"
    storage = Storage(requested, persist=False)
    temporary_root = storage.root
    try:
        assert temporary_root != requested
        assert not requested.exists()
        assert storage.format_root == temporary_root / "store-v2"
        assert storage.format_root.is_dir()
        assert storage.artifacts.is_dir()
        assert storage.scratch.is_dir()
    finally:
        storage.close()
    assert not temporary_root.exists()


def test_storage_never_touches_legacy_files_and_warns(tmp_path, caplog):
    from pyxsd.mcp.storage import Storage

    root = tmp_path / "legacy"
    root.mkdir(mode=0o700)
    database = root / "state.sqlite3"
    database.write_bytes(b"legacy database")
    workspaces = root / "workspaces" / "old" / "snapshot.xml"
    workspaces.parent.mkdir(parents=True)
    workspaces.write_bytes(b"<old/>")
    before = (database.read_bytes(), workspaces.read_bytes())

    with caplog.at_level("WARNING"), Storage(root) as storage:
        assert storage.format_root == root / "store-v2"
        assert (
            not (storage.format_root / "index.sqlite3").read_bytes().startswith(b"legacy database")
        )

    assert (database.read_bytes(), workspaces.read_bytes()) == before
    notices = [record.message for record in caplog.records if "legacy" in record.message]
    assert len(notices) == 1
    assert str(database) in notices[0]


def test_storage_rejects_unknown_index_versions_without_rewriting(tmp_path):
    from pyxsd.mcp.storage import Storage

    root = tmp_path / "versioned"
    root.mkdir(mode=0o700)
    format_root = root / "store-v2"
    format_root.mkdir(mode=0o700)
    index = format_root / "index.sqlite3"
    connection = sqlite3.connect(index)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    before = index.read_bytes()

    with pytest.raises(ToolError, match="state_version"):
        Storage(root)
    assert index.read_bytes() == before


def test_storage_cleans_up_when_initialization_fails(tmp_path, monkeypatch):
    from pyxsd.mcp.storage import Storage

    def fail(*_args, **_kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr("pyxsd.mcp.storage.sqlite3.connect", fail)
    root = tmp_path / "broken"
    with pytest.raises(OSError, match="disk failure"):
        Storage(root)
    monkeypatch.undo()
    with Storage(root):
        pass


def test_storage_can_reopen_after_close_and_allows_repeated_close(tmp_path):
    from pyxsd.mcp.storage import Storage

    root = tmp_path / "repeat"
    first = Storage(root)
    first.close()
    first.close()
    with Storage(root):
        pass


def test_storage_transactions_rollback_and_report_a_closed_store(tmp_path):
    import anyio

    from pyxsd.mcp.storage import Storage

    async def scenario():
        storage = Storage(tmp_path / "transactions")
        try:
            await storage.run(lambda db: db.execute("CREATE TABLE check_one(value TEXT)").close())
            with pytest.raises(RuntimeError, match="rollback"):
                await storage.run(
                    lambda db: (
                        db.execute("INSERT INTO check_one VALUES('discarded')"),
                        (_ for _ in ()).throw(RuntimeError("rollback")),
                    )
                )
            count = await storage.run(
                lambda db: db.execute("SELECT count(*) FROM check_one").fetchone()[0]
            )
            assert count == 0
        finally:
            storage.close()
        with pytest.raises(ToolError, match="state_closed"):
            await storage.run(lambda db: None)

    anyio.run(scenario)


def test_storage_accepts_the_current_index_version(tmp_path):
    from pyxsd.mcp.storage import Storage

    root = tmp_path / "current"
    root.mkdir(mode=0o700)
    format_root = root / "store-v2"
    format_root.mkdir(mode=0o700)
    connection = sqlite3.connect(format_root / "index.sqlite3")
    connection.execute("PRAGMA user_version = 1")
    connection.close()
    with Storage(root):
        pass


def test_storage_cleanup_and_close_tolerate_missing_resources(tmp_path, monkeypatch, caplog):
    from pyxsd.mcp.storage import Storage

    storage = Storage(tmp_path / "cleanup", persist=False)
    storage._connection.close()
    storage._connection = None
    os.close(storage._fd)
    storage._fd = None
    temporary = storage._temporary

    def fail():
        raise OSError("cannot remove")

    monkeypatch.setattr(temporary, "cleanup", fail)
    with caplog.at_level("WARNING"):
        storage.close()
    assert "Unable to remove temporary MCP state" in caplog.text


def test_storage_rejects_unsupported_platforms(tmp_path, monkeypatch):
    import builtins

    from pyxsd.mcp.storage import Storage

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ToolError, match="unsupported_platform"):
        Storage(tmp_path / "no-fcntl")
    monkeypatch.undo()

    monkeypatch.delattr("pyxsd.mcp.storage.os.O_NOFOLLOW")
    with pytest.raises(ToolError, match="unsupported_platform"):
        Storage(tmp_path / "no-follow")


def test_cli_selects_the_default_store_and_rejects_conflicting_flags(tmp_path, monkeypatch):
    from pyxsd.mcp.__main__ import main

    home = tmp_path / "home"
    captured = []
    monkeypatch.setattr(
        "pyxsd.mcp.config.default_state_dir", lambda: home / ".local/state/pyxsd/mcp"
    )
    monkeypatch.setattr("pyxsd.mcp.server.run", captured.append)
    assert main([]) == 0
    assert captured[-1].state_dir == home / ".local/state/pyxsd/mcp"
    assert captured[-1].persist is True

    monkeypatch.setattr("pyxsd.mcp.server.run", captured.append)
    assert main(["--state-dir", str(tmp_path / "custom")]) == 0
    assert captured[-1].state_dir == tmp_path / "custom"

    with pytest.raises(SystemExit) as raised:
        main(["--no-persist", "--state-dir", str(tmp_path / "other")])
    assert raised.value.code == 2
