"""Leased, owner-scoped opaque blobs; never publish arbitrary host paths."""

import hashlib
import os
import re
import secrets
import shutil
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from itertools import islice
from pathlib import Path
from typing import BinaryIO

import anyio

from .contracts import ArtifactRef
from .errors import ToolError
from .safe_io import SafeDirectory
from .workspaces import Principal, WorkspaceStore


class ArtifactReader:
    """A download capability valid only inside its store's lease context."""

    def __init__(self, store: "ArtifactStore", workspace: str, ref: ArtifactRef) -> None:
        self._store, self._workspace, self.ref = store, workspace, ref
        self._active = True

    async def read(self, *, offset: int, length: int) -> bytes:
        if not self._active:
            raise ToolError("stale_reference", "Artifact download lease has ended")
        if (
            type(offset) is not int
            or type(length) is not int
            or offset < 0
            or not 0 <= length <= self._store.limits.result_bytes
            or offset > self.ref.size
        ):
            raise ToolError("invalid_argument", "Invalid artifact byte range")

        def read() -> bytes:
            path = self._store._path(self._workspace, self.ref.id)
            try:
                with SafeDirectory(path) as directory:
                    item = directory.read("blob", max_bytes=self.ref.size)
            except FileNotFoundError as exc:
                raise ToolError("source_changed", "Artifact bytes are unavailable") from exc
            if item.digest != self.ref.digest or len(item.content) != self.ref.size:
                raise ToolError("source_changed", "Artifact bytes no longer match their digest")
            return item.content[offset : offset + length]

        return await anyio.to_thread.run_sync(read, limiter=self._store._copies)


class ArtifactStore:
    """Immutable blobs charged to workspace quotas, including metadata overhead.

    Streams are async byte iterators; filesystem I/O runs off the event loop.
    Reads verify the entire bounded blob before returning a bounded range. This
    is not streaming verification: peak read buffering is workers * output_bytes.
    Actual filesystem block/inode enforcement remains a deployment requirement.
    """

    def __init__(self, workspaces: WorkspaceStore) -> None:
        self.workspaces = workspaces
        self.state, self.limits = workspaces.state, workspaces.limits
        self._copies = anyio.CapacityLimiter(self.limits.workers)

    def _path(self, workspace: str, identifier: str) -> Path:
        return self.state.root / "workspaces" / workspace / "artifacts" / identifier

    async def sweep(self) -> None:
        await self.state.run(
            lambda db: db.execute(
                "UPDATE artifacts SET status='closing' WHERE status='ready' AND expires<=?",
                (self.workspaces.clock(),),
            ).close()
        )

        def cleanup(db: sqlite3.Connection) -> None:
            for row in db.execute(
                "SELECT * FROM artifacts WHERE status='closing' AND leases=0"
            ).fetchall():
                with suppress(FileNotFoundError):
                    shutil.rmtree(self._path(row["workspace"], row["id"]))
                db.execute(
                    "UPDATE workspaces SET artifacts=artifacts-? WHERE id=?",
                    (row["charged"], row["workspace"]),
                )
                db.execute("DELETE FROM artifacts WHERE id=?", (row["id"],))

        await self.state.run(cleanup)

    async def put(
        self,
        principal: Principal,
        workspace_id: str,
        stream: AsyncIterator[bytes],
        *,
        media_type: str,
        limit: int,
        ttl: int,
        expected_digest: str | None = None,
    ) -> ArtifactRef:
        """Retain a derived result; read permission does not grant external writes."""
        principal.require("pyxsd:read")
        if (
            type(limit) is not int
            or not 0 <= limit <= self.limits.output_bytes
            or type(ttl) is not int
            or ttl <= 0
            or not 1 <= len(media_type) <= 128
            or re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type) is None
            or (
                expected_digest is not None
                and re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
            )
        ):
            raise ToolError("invalid_argument", "Invalid artifact configuration")
        await self.sweep()
        async with self.workspaces.reserve(
            principal, workspace_id, limit + 512, kind="artifacts"
        ) as reservation:
            identifier = secrets.token_hex(16)
            path = self._path(workspace_id, identifier)

            def prepare(db: sqlite3.Connection) -> None:
                path.parent.mkdir(mode=0o700, exist_ok=True)
                if (
                    len(tuple(islice(path.parent.iterdir(), self.limits.artifact_entries)))
                    >= self.limits.artifact_entries
                ):
                    raise ToolError("quota_exceeded", "Artifact entry limit reached")
                try:
                    db.execute(
                        "INSERT INTO artifacts(id,workspace,media_type,reservation,status) VALUES(?,?,?,?,'staging')",
                        (identifier, workspace_id, media_type, reservation.id),
                    )
                    path.mkdir(mode=0o700)
                except (sqlite3.IntegrityError, FileExistsError) as exc:
                    raise ToolError(
                        "allocation_conflict", "Artifact identifier is unavailable", retryable=True
                    ) from exc

            await self.state.run(prepare)
            published = False
            try:
                size, digest = await self._write(path / "blob", stream, limit)
                if expected_digest is not None and digest != expected_digest:
                    raise ToolError("digest_mismatch", "Artifact digest did not match")
                ref = ArtifactRef(
                    id=identifier,
                    digest=digest,
                    size=size,
                    media_type=media_type,
                    expires_at=self.workspaces.clock()
                    + min(ttl, self.limits.workspace_idle_seconds),
                )
                await anyio.lowlevel.checkpoint_if_cancelled()

                def publish(db: sqlite3.Connection) -> None:
                    db.execute(
                        "UPDATE artifacts SET status='ready',digest=?,size=?,expires=?,charged=? WHERE id=?",
                        (digest, size, ref.expires_at, size + 512, identifier),
                    )

                with anyio.CancelScope(shield=True):
                    await reservation.commit(size=size + 512, on_commit=publish)
                    published = True
                return ref
            finally:
                if not published:

                    def discard(db: sqlite3.Connection) -> None:
                        shutil.rmtree(path)
                        db.execute("DELETE FROM artifacts WHERE id=?", (identifier,))

                    with anyio.CancelScope(shield=True):
                        await self.state.run(discard)

    async def _write(self, path: Path, stream: AsyncIterator[bytes], limit: int) -> tuple[int, str]:
        file: BinaryIO = await anyio.to_thread.run_sync(lambda: path.open("xb"))
        digest, size = hashlib.sha256(), 0
        try:
            async for chunk in stream:
                if not isinstance(chunk, bytes):
                    raise ToolError("invalid_argument", "Artifact streams must yield bytes")
                size += len(chunk)
                if size > limit:
                    raise ToolError("quota_exceeded", "Artifact output byte limit exceeded")
                digest.update(chunk)
                await anyio.to_thread.run_sync(file.write, chunk)

            def flush() -> None:
                file.flush()
                os.fsync(file.fileno())
                for directory in path.parents[:5]:
                    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)

            await anyio.to_thread.run_sync(flush)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(file.close)
        return size, digest.hexdigest()

    @asynccontextmanager
    async def lease(self, principal: Principal, artifact_id: str) -> AsyncIterator[ArtifactReader]:
        principal.require("pyxsd:read")
        await self.sweep()

        def lookup(db: sqlite3.Connection) -> sqlite3.Row | None:
            return db.execute(
                "SELECT a.* FROM artifacts a JOIN workspaces w ON a.workspace=w.id "
                "WHERE a.id=? AND w.owner=? AND w.status='open' AND a.status='ready' AND a.expires>?",
                (artifact_id, principal.owner_id, self.workspaces.clock()),
            ).fetchone()

        row = await self.state.run(lookup)
        if row is None:
            raise ToolError("not_found", "Artifact not found")
        async with self.workspaces.lease(principal, row["workspace"]):

            def acquire(db: sqlite3.Connection) -> None:
                updated = db.execute(
                    "UPDATE artifacts SET leases=leases+1 WHERE id=? AND status='ready' AND expires>?",
                    (artifact_id, self.workspaces.clock()),
                ).rowcount
                if updated != 1:
                    raise ToolError("not_found", "Artifact not found")

            await self.state.run(acquire)
            reader = ArtifactReader(
                self,
                row["workspace"],
                ArtifactRef(
                    id=artifact_id,
                    digest=row["digest"],
                    size=row["size"],
                    media_type=row["media_type"],
                    expires_at=row["expires"],
                ),
            )
            try:
                yield reader
            finally:
                reader._active = False
                with anyio.CancelScope(shield=True):
                    await self.state.run(
                        lambda db: db.execute(
                            "UPDATE artifacts SET leases=leases-1 WHERE id=?", (artifact_id,)
                        ).close()
                    )
                    await self.sweep()

    async def read(
        self, principal: Principal, artifact_id: str, *, offset: int, length: int
    ) -> bytes:
        async with self.lease(principal, artifact_id) as reader:
            return await reader.read(offset=offset, length=length)
