"""Lazy project output storage with independent temporary session scratch."""

import logging
import os
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TypeVar

import anyio

from .errors import ToolError

T = TypeVar("T")


def _private_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ToolError("unsafe_state_directory", "State directory must be private and owned")


class Storage:
    """Own scratch eagerly, but open/create retained state only on demand."""

    def __init__(self, root: Path, *, persist: bool = True, busy_timeout_ms: int = 5000) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            raise ToolError("unsupported_platform", "State requires no-follow file opens")
        self._temporary = tempfile.TemporaryDirectory(prefix="pyxsd-mcp-")
        self.scratch = Path(self._temporary.name)
        self.root = Path(root).absolute() if persist else self.scratch / "outputs"
        self.format_root = self.root / "store-v2"
        self.artifacts = self.format_root / "artifacts"
        self._lock = threading.RLock()
        self._closed = False
        self._connection: sqlite3.Connection | None = None
        self._busy_timeout_ms = busy_timeout_ms

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        if self._closed:
            raise ToolError("state_closed", "State database is closed")
        if self._connection is not None:
            return self._connection
        index = self.format_root / "index.sqlite3"
        if not create and not index.exists():
            return None
        for directory in (self.root, self.format_root, self.artifacts):
            _private_directory(directory, create=create)
        if index.is_symlink():
            raise ToolError("unsafe_state_directory", "State index must not be a symlink")
        connection = sqlite3.connect(
            index.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True,
            timeout=self._busy_timeout_ms / 1000,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        self._connection = connection
        return connection

    async def run(
        self, operation: Callable[[sqlite3.Connection], T], *, create: bool = True
    ) -> T | None:
        """Execute a bounded transaction off-loop; None denotes an absent store."""
        return await anyio.to_thread.run_sync(partial(self._run, operation, create=create))

    def _run(
        self, operation: Callable[[sqlite3.Connection], T], *, create: bool = True
    ) -> T | None:
        with self._lock:
            connection = None
            try:
                connection = self._connect(create=create)
                if connection is None:
                    return None
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise ToolError("state_version", "Unsupported state database version")
                result = operation(connection)
                connection.commit()
                return result
            except BaseException as exc:
                if connection is not None:
                    connection.rollback()
                if isinstance(exc, sqlite3.DatabaseError):
                    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise ToolError(
                            "state_busy", "State is busy; retry the operation", retryable=True
                        ) from exc
                    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                        raise ToolError("state_corrupt", "State database is corrupt") from exc
                    if code in (
                        sqlite3.SQLITE_IOERR,
                        sqlite3.SQLITE_READONLY,
                        sqlite3.SQLITE_CANTOPEN,
                        sqlite3.SQLITE_FULL,
                    ):
                        raise ToolError("state_io", "Unable to access local state") from exc
                if isinstance(exc, OSError):
                    raise ToolError("state_io", "Unable to access local state") from exc
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            try:
                self._temporary.cleanup()
            except OSError:
                logging.getLogger(__name__).warning(
                    "Unable to remove temporary MCP state at %s", self.scratch
                )

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
