"""Private local storage lifetime for the MCP server.

One process owns the selected directory. Persistent mode keeps the artifact
index and result files; temporary mode removes the whole directory on an
orderly close. Legacy workspace databases are never opened or rewritten here.
"""

import logging
import os
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import anyio

from .errors import ToolError

T = TypeVar("T")

_FORMAT_DIRECTORY = "store-v2"
_SUPPORTED_VERSIONS = (0, 1)


def _private_directory(path: Path, *, parents: bool) -> None:
    path.mkdir(mode=0o700, parents=parents, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ToolError("unsafe_state_directory", "State directory must be private and owned")


class Storage:
    """Own the private store root and its process lock until explicitly closed."""

    def __init__(self, root: Path, *, persist: bool = True) -> None:
        try:
            import fcntl
        except ImportError as exc:
            raise ToolError("unsupported_platform", "State requires POSIX file locking") from exc
        if not hasattr(os, "O_NOFOLLOW"):
            raise ToolError("unsupported_platform", "State requires no-follow file opens")

        self._temporary = None if persist else tempfile.TemporaryDirectory(prefix="pyxsd-mcp-")
        selected = Path(root) if persist else Path(self._temporary.name)  # type: ignore[union-attr]
        self._lock = threading.RLock()
        self._closed = False
        self._connection: sqlite3.Connection | None = None
        self._fd: int | None = None
        try:
            _private_directory(selected, parents=persist)
            self.root = selected
            self.format_root = selected / _FORMAT_DIRECTORY
            self.artifacts = self.format_root / "artifacts"
            self.scratch = self.format_root / "scratch"
            _private_directory(self.format_root, parents=False)
            _private_directory(self.artifacts, parents=False)
            _private_directory(self.scratch, parents=False)
            self.lock_path = selected / "coordinator.lock"
            if persist and (
                (selected / "state.sqlite3").exists() or (selected / "workspaces").is_dir()
            ):
                logging.getLogger(__name__).warning(
                    "Legacy MCP state at %s (for example %s) is not loaded; new outputs use %s",
                    selected,
                    selected / "state.sqlite3",
                    self.format_root,
                )
            self._fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ToolError("state_locked", "Another coordinator owns this state") from exc
            connection = sqlite3.connect(
                self.format_root / "index.sqlite3",
                check_same_thread=False,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in _SUPPORTED_VERSIONS:
                connection.close()
                raise ToolError("state_version", "Unsupported state database version")
            self._connection = connection
        except BaseException:
            if self._fd is not None:
                os.close(self._fd)
            self._cleanup_temporary()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._closed or self._connection is None:
            raise ToolError("state_closed", "State database is closed")
        return self._connection

    async def run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Run one transaction off the event loop, rolling back on failure."""
        return await anyio.to_thread.run_sync(self._run, operation)

    def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            connection = self.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = operation(connection)
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._cleanup_temporary()

    def _cleanup_temporary(self) -> None:
        temporary, self._temporary = self._temporary, None
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                logging.getLogger(__name__).warning(
                    "Unable to remove temporary MCP state at %s", temporary.name
                )

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
