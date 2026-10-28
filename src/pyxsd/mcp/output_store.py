"""Immutable local outputs indexed by a small SQLite metadata table."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
from builtins import list as list_type
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import anyio

from pydantic import JsonValue

from .config import Limits
from .errors import ToolError
from .storage import Storage

_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    digest TEXT NOT NULL,
    created REAL NOT NULL,
    kind TEXT NOT NULL,
    metadata TEXT NOT NULL
);
PRAGMA user_version = 1;
COMMIT;
"""

_COLUMNS = ("id", "path", "media_type", "size", "digest", "created", "kind", "metadata")
_ID = re.compile(r"^[0-9a-f]{32}$")
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_MEDIA = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ATTEMPTS = 4


@dataclass(frozen=True)
class OutputRef:
    id: str
    path: Path
    media_type: str
    size: int
    digest: str
    created_at: float
    kind: str


class OutputStore:
    """Own output files and their restart-readable metadata; never input history."""

    def __init__(self, storage: Storage, limits: Limits) -> None:
        self.storage, self.limits = storage, limits
        self._lock = anyio.Lock()
        self._closed = False
        self._initialize()
        self._reconcile()

    def _initialize(self) -> None:
        connection = self.storage.connection
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            connection.executescript(_SCHEMA)
        elif version != 1:
            raise ToolError("state_version", "Unsupported output index version")
        rows = connection.execute("PRAGMA table_info(artifacts)").fetchall()
        if tuple(row["name"] for row in rows) != _COLUMNS:
            raise ToolError("state_version", "Output index schema is unavailable")

    def _reconcile(self) -> None:
        indexed = {
            row[0] for row in self.storage.connection.execute("SELECT id FROM artifacts").fetchall()
        }
        for path in self.storage.artifacts.iterdir():
            if path.name in indexed:
                continue
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ToolError("state_closed", "Output store is closed")

    @staticmethod
    def _arguments(
        name: str,
        media_type: str,
        kind: str,
        metadata: dict[str, JsonValue] | None,
        limit: int,
    ) -> bytes:
        if (
            _NAME.fullmatch(name) is None
            or _MEDIA.fullmatch(media_type) is None
            or _KIND.fullmatch(kind) is None
            or (metadata is not None and not isinstance(metadata, dict))
        ):
            raise ToolError("invalid_argument", "Invalid output metadata")
        try:
            encoded = json.dumps(
                {} if metadata is None else metadata,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ToolError("invalid_argument", "Output metadata is not JSON") from exc
        if len(encoded) > limit:
            raise ToolError("metadata_limit", "Output metadata exceeds its byte limit")
        return encoded

    def _allocate(self) -> tuple[str, Path]:
        for _ in range(_ATTEMPTS):
            identifier = secrets.token_hex(16)
            path = self.storage.artifacts / identifier
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                continue
            return identifier, path
        raise ToolError("allocation_conflict", "Output identifier is unavailable", retryable=True)

    async def put_bytes(
        self,
        content: bytes,
        *,
        name: str,
        media_type: str,
        kind: str,
        metadata: dict[str, JsonValue] | None = None,
    ) -> OutputRef:
        encoded = self._arguments(name, media_type, kind, metadata, self.limits.result_bytes)
        if not isinstance(content, bytes):
            raise ToolError("invalid_argument", "Output content must be bytes")
        if len(content) > self.limits.output_bytes:
            raise ToolError("quota_exceeded", "Output byte limit exceeded")
        async with self._lock:
            self._ensure_open()
            return await anyio.to_thread.run_sync(
                self._put_sync, content, name, media_type, kind, encoded.decode("ascii")
            )

    def _put_sync(
        self,
        content: bytes,
        name: str,
        media_type: str,
        kind: str,
        metadata: str,
    ) -> OutputRef:
        identifier, directory = self._allocate()
        final = directory / name
        stage = directory / (name + ".tmp")
        connection = self.storage.connection
        try:
            with stage.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            stage.rename(final)
            created = time.time()
            digest = hashlib.sha256(content).hexdigest()
            relative = str(final.relative_to(self.storage.format_root))
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        relative,
                        media_type,
                        len(content),
                        digest,
                        created,
                        kind,
                        metadata,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return OutputRef(
            id=identifier,
            path=final,
            media_type=media_type,
            size=len(content),
            digest=digest,
            created_at=created,
            kind=kind,
        )

    def _row(self, artifact_id: str) -> sqlite3.Row:
        row = None
        if _ID.fullmatch(artifact_id) is not None:
            row = self.storage.connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise ToolError("not_found", "Artifact not found")
        return cast(sqlite3.Row, row)

    def _path(self, row: sqlite3.Row) -> Path:
        path = self.storage.format_root / row["path"]
        try:
            path.relative_to(self.storage.artifacts)
        except ValueError as exc:
            raise ToolError("artifact_changed", "Artifact path is outside the store") from exc
        return path

    def _ref(self, row: sqlite3.Row) -> OutputRef:
        return OutputRef(
            id=row["id"],
            path=self._path(row),
            media_type=row["media_type"],
            size=row["size"],
            digest=row["digest"],
            created_at=row["created"],
            kind=row["kind"],
        )

    def _verified(self, row: sqlite3.Row, *, offset: int, length: int) -> bytes:
        path = self._path(row)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise ToolError("artifact_missing", "Artifact file is missing") from exc
        except OSError as exc:
            raise ToolError("artifact_changed", "Artifact cannot be read safely") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ToolError("artifact_changed", "Artifact is not a regular file")
            digest, size = hashlib.sha256(), 0
            selected = bytearray()
            with os.fdopen(descriptor, "rb", closefd=False) as file:
                while True:
                    chunk = file.read(1 << 20)
                    if not chunk:
                        break
                    start = size
                    size += len(chunk)
                    digest.update(chunk)
                    if start < offset + length and size > offset:
                        selected.extend(chunk[max(0, offset - start) : offset + length - start])
        finally:
            os.close(descriptor)
        if size != row["size"] or digest.hexdigest() != row["digest"]:
            raise ToolError("artifact_changed", "Artifact bytes no longer match the index")
        return bytes(selected)

    async def get(self, artifact_id: str) -> OutputRef:
        async with self._lock:
            self._ensure_open()
            return await anyio.to_thread.run_sync(self._get_sync, artifact_id)

    def _get_sync(self, artifact_id: str) -> OutputRef:
        row = self._row(artifact_id)
        self._verified(row, offset=0, length=0)
        return self._ref(row)

    async def read_with_ref(
        self, artifact_id: str, *, offset: int, length: int
    ) -> tuple[OutputRef, bytes]:
        """Return metadata and a range after one integrity scan under one lock."""
        if type(offset) is not int or offset < 0 or type(length) is not int or length <= 0:
            raise ToolError("invalid_argument", "Invalid artifact range")
        async with self._lock:
            self._ensure_open()
            return await anyio.to_thread.run_sync(
                self._read_with_ref_sync, artifact_id, offset, length
            )

    def _read_with_ref_sync(
        self, artifact_id: str, offset: int, length: int
    ) -> tuple[OutputRef, bytes]:
        row = self._row(artifact_id)
        return self._ref(row), self._verified(row, offset=offset, length=length)

    async def read(self, artifact_id: str, *, offset: int, length: int) -> bytes:
        _, content = await self.read_with_ref(artifact_id, offset=offset, length=length)
        return content

    async def list(
        self, *, after: tuple[float, str] | None = None, limit: int = 50
    ) -> tuple[list_type[OutputRef], tuple[float, str] | None]:
        if type(limit) is not int or not 1 <= limit <= self.limits.page_items:
            raise ToolError("invalid_argument", "Invalid output page size")
        if after is not None and (
            not isinstance(after, tuple)
            or len(after) != 2
            or type(after[0]) not in (int, float)
            or not math.isfinite(after[0])
            or not isinstance(after[1], str)
            or _ID.fullmatch(after[1]) is None
        ):
            raise ToolError("invalid_argument", "Invalid output listing position")
        async with self._lock:
            self._ensure_open()
            return await anyio.to_thread.run_sync(self._list_sync, after, limit)

    def _list_sync(
        self, after: tuple[float, str] | None, limit: int
    ) -> tuple[list_type[OutputRef], tuple[float, str] | None]:
        if after is None:
            rows = self.storage.connection.execute(
                "SELECT * FROM artifacts ORDER BY created,id LIMIT ?", (limit + 1,)
            ).fetchall()
        else:
            rows = self.storage.connection.execute(
                "SELECT * FROM artifacts WHERE created>? OR (created=? AND id>?) "
                "ORDER BY created,id LIMIT ?",
                (after[0], after[0], after[1], limit + 1),
            ).fetchall()
        more = len(rows) > limit
        page = rows[:limit]
        refs = [self._ref(row) for row in page]
        next_after = None if not more else (page[-1]["created"], page[-1]["id"])
        return refs, next_after

    async def delete(self, artifact_id: str) -> None:
        async with self._lock:
            self._ensure_open()
            await anyio.to_thread.run_sync(self._delete_sync, artifact_id)

    def _delete_sync(self, artifact_id: str) -> None:
        if _ID.fullmatch(artifact_id) is None:
            return
        row = self.storage.connection.execute(
            "SELECT path FROM artifacts WHERE id=?", (artifact_id,)
        ).fetchone()
        if row is not None:
            connection = self.storage.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        shutil.rmtree(self.storage.artifacts / artifact_id, ignore_errors=True)

    def close(self) -> None:
        self._closed = True
