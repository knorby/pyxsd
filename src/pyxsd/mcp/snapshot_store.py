"""Coordinator-side byte storage; XML parsing belongs exclusively to workers."""

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from itertools import islice
from pathlib import Path

import anyio

from .contracts import InlineFile
from .errors import ToolError
from .safe_io import AdmittedFile, SafeDirectory, admit_inline, validate_paths
from .snapshots import Snapshot, SnapshotFile
from .workspaces import Principal, WorkspaceStore


class SnapshotStore:
    """Keep leased workspace revisions immutable until workspace expiry.

    Revision metadata and quota commit together, after private staging is flushed
    and rehashed. A durable staging intent lets State discard interrupted writes
    on restart. Neither admission nor publication invokes an XML parser.
    """

    def __init__(self, workspaces: WorkspaceStore) -> None:
        self.workspaces = workspaces
        self.state, self.limits = workspaces.state, workspaces.limits
        self._copies = anyio.CapacityLimiter(self.limits.workers)

    def _snapshot(self, workspace_id: str, row: sqlite3.Row) -> Snapshot:
        return Snapshot(
            workspace_id,
            row["revision"],
            self.state.root / "workspaces" / workspace_id / "snapshots" / row["revision"] / "files",
            tuple(SnapshotFile(**item) for item in json.loads(row["manifest"])),
        )

    def _current(self, db: sqlite3.Connection, workspace_id: str) -> Snapshot | None:
        row = db.execute(
            "SELECT s.* FROM snapshot_heads h JOIN snapshots s "
            "ON s.workspace=h.workspace AND s.revision=h.revision "
            "WHERE h.workspace=? AND s.status='ready'",
            (workspace_id,),
        ).fetchone()
        return None if row is None else self._snapshot(workspace_id, row)

    async def get(
        self, principal: Principal, workspace_id: str, *, revision: str | None = None
    ) -> Snapshot:
        """Return internal metadata; file consumers must keep an outer workspace lease."""
        principal.require("pyxsd:read")
        async with self.workspaces.lease(principal, workspace_id):

            def lookup(db: sqlite3.Connection) -> Snapshot:
                if revision is None:
                    result = self._current(db, workspace_id)
                else:
                    row = db.execute(
                        "SELECT * FROM snapshots WHERE workspace=? AND revision=? AND status='ready'",
                        (workspace_id, revision),
                    ).fetchone()
                    result = None if row is None else self._snapshot(workspace_id, row)
                if result is None:
                    raise ToolError("not_found", "Snapshot not found")
                return result

            return await self.state.run(lookup)

    async def put_files(
        self,
        principal: Principal,
        workspace_id: str,
        files: Sequence[InlineFile],
        *,
        expected_revision: str | None,
    ) -> Snapshot:
        principal.require("pyxsd:upload")
        return await self.put_bytes(
            principal,
            workspace_id,
            admit_inline(files, self.limits),
            expected_revision=expected_revision,
        )

    async def put_bytes(
        self,
        principal: Principal,
        workspace_id: str,
        files: Sequence[AdmittedFile],
        *,
        expected_revision: str | None,
    ) -> Snapshot:
        """Internal binary admission; HTTP streaming/authentication is a separate layer."""
        principal.require("pyxsd:upload")
        validate_paths([item.path for item in files])
        if any(len(item.content) > self.limits.xml_file_bytes for item in files):
            raise ToolError("quota_exceeded", "File byte limit exceeded")
        async with self.workspaces.lease(principal, workspace_id):
            previous = await self.state.run(lambda db: self._current(db, workspace_id))
            if (None if previous is None else previous.revision) != expected_revision:
                raise ToolError("stale_reference", "Workspace revision changed")
            records = {} if previous is None else {item.path: item for item in previous.files}
            updates = {item.path: item.content for item in files}
            for path, content in updates.items():
                records[path] = SnapshotFile(
                    path, hashlib.sha256(content).hexdigest(), len(content)
                )
            validate_paths(list(records))
            entries = set(records)
            for path in records:
                parts = path.split("/")
                for index in range(1, len(parts)):
                    entries.add("/".join(parts[:index]))
                    if len(entries) > self.limits.snapshot_entries:
                        raise ToolError("quota_exceeded", "Snapshot entry limit exceeded")
            if len(entries) > self.limits.snapshot_entries:
                raise ToolError("quota_exceeded", "Snapshot entry limit exceeded")
            manifest = json.dumps(
                [asdict(records[path]) for path in sorted(records)],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            size = sum(item.size for item in records.values()) + len(manifest.encode("utf-8"))
            revision = secrets.token_hex(16)
            snapshot = Snapshot(
                workspace_id,
                revision,
                self.state.root / "workspaces" / workspace_id / "snapshots" / revision / "files",
                tuple(records[path] for path in sorted(records)),
            )
            async with self.workspaces.reserve(principal, workspace_id, size) as reservation:

                def prepare(db: sqlite3.Connection) -> None:
                    parent = snapshot.root.parent.parent
                    parent.mkdir(mode=0o700, exist_ok=True)
                    if (
                        sum(1 for _ in islice(parent.iterdir(), self.limits.snapshot_revisions))
                        >= self.limits.snapshot_revisions
                    ):
                        raise ToolError("quota_exceeded", "Snapshot revision limit exceeded")
                    try:
                        db.execute(
                            "INSERT INTO snapshots VALUES(?,?,?,'staging',?)",
                            (workspace_id, revision, manifest, reservation.id),
                        )
                        snapshot.root.parent.mkdir(mode=0o700)
                    except (sqlite3.IntegrityError, FileExistsError) as exc:
                        raise ToolError(
                            "allocation_conflict",
                            "Snapshot identifier is unavailable",
                            retryable=True,
                        ) from exc

                await self.state.run(prepare)
                published = False
                try:
                    await anyio.to_thread.run_sync(
                        self._write,
                        snapshot,
                        previous,
                        updates,
                        limiter=self._copies,
                    )
                    await anyio.lowlevel.checkpoint_if_cancelled()

                    def publish(db: sqlite3.Connection) -> None:
                        current = self._current(db, workspace_id)
                        if (None if current is None else current.revision) != expected_revision:
                            raise ToolError("stale_reference", "Workspace revision changed")
                        db.execute(
                            "UPDATE snapshots SET status='ready' WHERE workspace=? AND revision=?",
                            (workspace_id, revision),
                        )
                        db.execute(
                            "INSERT INTO snapshot_heads VALUES(?,?) ON CONFLICT(workspace) DO UPDATE SET revision=excluded.revision",
                            (workspace_id, revision),
                        )

                    with anyio.CancelScope(shield=True):
                        await reservation.commit(on_commit=publish)
                        published = True
                finally:
                    if not published:
                        with anyio.CancelScope(shield=True):
                            await anyio.to_thread.run_sync(self._discard, snapshot)
                            await self.state.run(
                                lambda db: db.execute(
                                    "DELETE FROM snapshots WHERE workspace=? AND revision=? AND status='staging'",
                                    (workspace_id, revision),
                                ).close()
                            )
            return snapshot

    @staticmethod
    def _discard(snapshot: Snapshot) -> None:
        with suppress(FileNotFoundError):
            shutil.rmtree(snapshot.root.parent)

    def _verify(self, snapshot: Snapshot) -> None:
        for item in snapshot.files:
            self._read(snapshot, item)

    def _read(self, snapshot: Snapshot, item: SnapshotFile) -> bytes:
        try:
            with SafeDirectory(snapshot.root) as directory:
                captured = directory.read(item.path, max_bytes=self.limits.xml_file_bytes)
        except FileNotFoundError as exc:
            raise ToolError("source_changed", "Snapshot resource disappeared") from exc
        if captured.digest != item.digest or len(captured.content) != item.size:
            raise ToolError("source_changed", "Snapshot bytes no longer match manifest")
        return captured.content

    def _write(
        self, snapshot: Snapshot, previous: Snapshot | None, updates: dict[str, bytes]
    ) -> None:
        snapshot.root.mkdir(mode=0o700)
        for item in snapshot.files:
            if item.path in updates:
                content = updates[item.path]
            else:
                assert previous is not None
                content = self._read(previous, item)
            destination = snapshot.root / item.path
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with destination.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        self._verify(snapshot)
        if previous is not None:
            self._verify(previous)
        directories = [Path(root) for root, _, _ in os.walk(snapshot.root, topdown=False)]
        directories.extend(snapshot.root.parents[:4])
        for directory in directories:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
