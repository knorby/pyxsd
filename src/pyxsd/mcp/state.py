"""Private, versioned SQLite state for a single coordinator process."""

import os
import shutil
import sqlite3
import stat
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

import anyio

from .errors import ToolError

T = TypeVar("T")

_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, create_key TEXT,
    created REAL NOT NULL, expires REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', closed REAL,
    leases INTEGER NOT NULL DEFAULT 0,
    files INTEGER NOT NULL DEFAULT 0, artifacts INTEGER NOT NULL DEFAULT 0,
    reserved_files INTEGER NOT NULL DEFAULT 0, reserved_artifacts INTEGER NOT NULL DEFAULT 0,
    UNIQUE(owner, create_key)
);
CREATE TABLE reservations (
    id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, reserved INTEGER NOT NULL, committed INTEGER
);
PRAGMA user_version = 1;
COMMIT;
"""

_SNAPSHOTS = """
BEGIN IMMEDIATE;
CREATE TABLE snapshots (
    workspace TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    revision TEXT NOT NULL, manifest TEXT NOT NULL, status TEXT NOT NULL,
    reservation TEXT REFERENCES reservations(id) ON DELETE SET NULL,
    PRIMARY KEY(workspace, revision)
);
CREATE TABLE snapshot_heads (
    workspace TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    revision TEXT NOT NULL,
    FOREIGN KEY(workspace, revision) REFERENCES snapshots(workspace, revision) ON DELETE CASCADE
);
PRAGMA user_version = 2;
COMMIT;
"""

_ARTIFACTS = """
BEGIN IMMEDIATE;
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    media_type TEXT NOT NULL, status TEXT NOT NULL, digest TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0, expires REAL NOT NULL DEFAULT 0,
    charged INTEGER NOT NULL DEFAULT 0, leases INTEGER NOT NULL DEFAULT 0,
    reservation TEXT REFERENCES reservations(id) ON DELETE SET NULL
);
PRAGMA user_version = 3;
COMMIT;
"""


class State:
    """Own the private state directory and process lock until explicitly closed.

    The configured directory is operator-trusted, never a tool argument. POSIX
    owner-only permissions and a process lock prevent a second coordinator from
    resetting live leases during restart recovery. SQL callbacks run transactionally
    in a worker thread, not on the server event loop.
    """

    def __init__(self, root: Path) -> None:
        try:
            import fcntl
        except ImportError as exc:
            raise ToolError("unsupported_platform", "State requires POSIX file locking") from exc
        if not hasattr(os, "O_NOFOLLOW"):
            raise ToolError("unsupported_platform", "State requires no-follow file opens")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ToolError("unsafe_state_directory", "State directory must be private and owned")
        self.root = root
        self._lock = threading.RLock()
        self._closed = False
        self._fd = os.open(root / "coordinator.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._fd)
            raise ToolError("state_locked", "Another coordinator owns this state") from exc
        connection = None
        try:
            connection = sqlite3.connect(
                root / "state.sqlite3", check_same_thread=False, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.executescript(_SCHEMA)
                version = 1
            if version == 1:
                connection.executescript(_SNAPSHOTS)
                version = 2
            if version == 2:
                connection.executescript(_ARTIFACTS)
            elif version != 3:
                raise ToolError("state_version", "Unsupported state database version")
            # Staging intents precede filesystem writes. Only ready revisions
            # were charged as committed usage; interrupted staging is discarded
            # before clearing the corresponding transient quota reservations.
            for row in connection.execute(
                "SELECT workspace,revision FROM snapshots WHERE status='staging'"
            ).fetchall():
                path = root / "workspaces" / row["workspace"] / "snapshots" / row["revision"]
                with suppress(FileNotFoundError):
                    shutil.rmtree(path)
            for row in connection.execute(
                "SELECT workspace,id FROM artifacts WHERE status='staging'"
            ).fetchall():
                path = root / "workspaces" / row["workspace"] / "artifacts" / row["id"]
                with suppress(FileNotFoundError):
                    shutil.rmtree(path)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM snapshots WHERE status='staging'")
            connection.execute("DELETE FROM artifacts WHERE status='staging'")
            connection.execute("UPDATE artifacts SET leases=0")
            connection.execute("DELETE FROM reservations")
            connection.execute(
                "UPDATE workspaces SET leases=0, reserved_files=0, reserved_artifacts=0"
            )
            connection.commit()
            self._connection = connection
            (root / "workspaces").mkdir(mode=0o700, exist_ok=True)
        except BaseException:
            if connection is not None:
                connection.close()
            os.close(self._fd)
            raise

    async def run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Commit a callback atomically; cancellation waits for its transaction."""
        return await anyio.to_thread.run_sync(self._run, operation)

    def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            if self._closed:
                raise ToolError("state_closed", "State database is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                result = operation(self._connection)
                self._connection.commit()
                return result
            except BaseException:
                self._connection.rollback()
                raise

    def close(self) -> None:
        """Close state after services and their active leases have drained."""
        with self._lock:
            if not self._closed:
                self._connection.close()
                os.close(self._fd)
                self._closed = True

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
