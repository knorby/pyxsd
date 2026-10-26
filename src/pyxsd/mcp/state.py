"""Private, versioned SQLite state for a single coordinator process."""

import os
import sqlite3
import stat
import threading
from collections.abc import Callable
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
            elif version != 1:
                raise ToolError("state_version", "Unsupported state database version")
            connection.execute("BEGIN IMMEDIATE")
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
