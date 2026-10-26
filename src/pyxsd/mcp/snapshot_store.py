"""Coordinator-side byte storage; XML parsing belongs exclusively to workers."""

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import asdict
from itertools import islice
from pathlib import Path
from typing import Protocol

import anyio

from .contracts import FileRef, InlineFile, SchemaRef
from .errors import ToolError
from .safe_io import AdmittedFile, CapturedFile, SafeDirectory, admit_inline, validate_paths
from .snapshots import Dependency, Snapshot, SnapshotFile
from .workspaces import Principal, WorkspaceStore


class DiscoveryWorker(Protocol):
    """Implement through a constrained clean worker, never coordinator-side XML parsing."""

    async def discover(
        self, snapshot: Snapshot, refs: tuple[FileRef | SchemaRef, ...]
    ) -> tuple[Dependency, ...]:
        """Return schema-only dependency attempts, including missing virtual paths."""
        ...


class _MoreFiles(Exception):
    pass


class SnapshotStore:
    """Keep leased workspace revisions immutable until workspace expiry.

    Revision metadata and quota commit together, after private staging is flushed
    and rehashed. A durable staging intent lets State discard interrupted writes
    on restart. Neither admission nor publication invokes an XML parser.
    """

    def __init__(
        self,
        workspaces: WorkspaceStore,
        *,
        source_root: Callable[[Principal, str], Path] | None = None,
        discovery: DiscoveryWorker | None = None,
    ) -> None:
        self.workspaces = workspaces
        self.state, self.limits = workspaces.state, workspaces.limits
        self._copies = anyio.CapacityLimiter(self.limits.workers)
        self._captures = anyio.CapacityLimiter(self.limits.workers)
        self._source_root, self._discovery = source_root, discovery

    async def capture(
        self, principal: Principal, workspace_id: str, refs: Sequence[FileRef | SchemaRef]
    ) -> Snapshot:
        """Copy an operator-granted root, retrying one inconsistent capture.

        FileRef inputs are instances; SchemaRef inputs include their compilation
        options. The root resolver is trusted configuration, never a client path.
        Discovery observes only retained or quota-reserved private revisions. Internal
        persistence requires upload scope as well as source-read permission.
        Captures are verified, not atomic multi-file filesystem transactions.
        Active captures are limited to workers; each retains at most workspace_bytes
        of source buffers plus one xml_file_bytes verification buffer. This is not
        a streaming-corpus interface or a claim of runtime memory containment.
        """
        principal.require("pyxsd:read", "pyxsd:upload")
        async with self.workspaces.lease(principal, workspace_id), self._captures:
            if self._source_root is None or self._discovery is None:
                raise ToolError(
                    "capture_unavailable", "No source grant/discovery worker configured"
                )
            inputs = tuple(ref.model_copy(deep=True) for ref in refs)
            sources = [
                file
                for ref in inputs
                for file in (
                    (ref.source, *ref.options.namespace_schemas.values())
                    if isinstance(ref, SchemaRef)
                    else (ref,)
                )
            ]
            if not sources or any(file.revision is not None for file in sources):
                raise ToolError("invalid_argument", "Capture needs unpinned source references")
            paths = tuple(dict.fromkeys(file.path for file in sources))
            validate_paths(paths)
            root = self._source_root(principal, workspace_id)
            previous = await self.state.run(lambda db: self._current(db, workspace_id))
            revision = None if previous is None else previous.revision
            try:
                return await self._capture_once(
                    principal, workspace_id, root, inputs, paths, revision
                )
            except ToolError as exc:
                if exc.fault.code != "source_changed":
                    raise
            return await self._capture_once(principal, workspace_id, root, inputs, paths, revision)

    async def _capture_once(
        self,
        principal: Principal,
        workspace_id: str,
        root: Path,
        refs: tuple[FileRef | SchemaRef, ...],
        paths: tuple[str, ...],
        revision: str | None,
    ) -> Snapshot:
        captured: dict[str, CapturedFile] = {}
        missing: set[str] = set()
        schema_paths = {
            file.path
            for ref in refs
            if isinstance(ref, SchemaRef)
            for file in (ref.source, *ref.options.namespace_schemas.values())
        }
        assert self._discovery is not None
        worker = self._discovery

        def current(db: sqlite3.Connection) -> Snapshot | None:
            result = self._current(db, workspace_id)
            if (None if result is None else result.revision) != revision:
                raise ToolError("stale_reference", "Workspace revision changed")
            return result

        previous = await self.state.run(current)
        previous_files = {} if previous is None else {item.path: item for item in previous.files}
        with SafeDirectory(root) as directory:

            def check_schema_limits() -> None:
                items = [captured[path] for path in schema_paths if path in captured]
                if (
                    len(schema_paths) > self.limits.schema_closure_files
                    or any(len(item.content) > self.limits.schema_file_bytes for item in items)
                    or sum(len(item.content) for item in items) > self.limits.schema_closure_bytes
                ):
                    raise ToolError("schema_limit", "Capture schema closure limit exceeded")

            def read(paths: Sequence[str], *, required: bool) -> bool:
                if not required:
                    schema_paths.update(paths)
                check_schema_limits()
                changed = False
                for path in paths:
                    if path in captured or path in missing:
                        continue
                    if len(captured) + len(missing) >= self.limits.snapshot_entries:
                        raise ToolError("quota_exceeded", "Capture entry limit exceeded")
                    remaining = self.limits.workspace_bytes - sum(
                        len(item.content) for item in captured.values()
                    )
                    try:
                        item = directory.read(
                            path, max_bytes=min(remaining, self.limits.xml_file_bytes)
                        )
                    except FileNotFoundError as exc:
                        if required:
                            raise ToolError("not_found", "Capture source not found") from exc
                        missing.add(path)
                    else:
                        captured[path] = item
                        check_schema_limits()
                        changed = True
                validate_paths([*captured, *missing])
                return changed

            def verify() -> None:
                for item in captured.values():
                    directory.verify(item, max_bytes=self.limits.xml_file_bytes)
                for path in missing:
                    try:
                        directory.read(path, max_bytes=self.limits.xml_file_bytes)
                    except FileNotFoundError:
                        continue
                    raise ToolError(
                        "source_changed", "A missing dependency appeared", retryable=True
                    )

            async def discover(snapshot: Snapshot) -> None:
                found = await worker.discover(snapshot, refs)
                names = tuple(dict.fromkeys(item.path for item in found))
                validate_paths(names)
                expanded = await anyio.to_thread.run_sync(
                    lambda: read(names, required=False), limiter=self._copies
                )
                await anyio.to_thread.run_sync(verify, limiter=self._copies)
                await anyio.to_thread.run_sync(self._verify, snapshot, limiter=self._copies)
                if expanded:
                    raise _MoreFiles

            await anyio.to_thread.run_sync(lambda: read(paths, required=True), limiter=self._copies)
            while True:
                try:
                    if previous is not None and all(
                        previous_files.get(path)
                        == SnapshotFile(path, item.digest, len(item.content))
                        for path, item in captured.items()
                    ):
                        # Inspect retained bytes first: unchanged captures must not
                        # consume a revision on every mounted-source inspection.
                        await discover(previous)
                        files = tuple(
                            SnapshotFile(path, captured[path].digest, len(captured[path].content))
                            for path in sorted(captured)
                        )
                        if files == previous.files:
                            await anyio.lowlevel.checkpoint_if_cancelled()
                            await self.state.run(current)
                            return previous
                    return await self._publish_files(
                        principal,
                        workspace_id,
                        [AdmittedFile(path, item.content) for path, item in captured.items()],
                        expected_revision=revision,
                        replace=True,
                        before_publish=discover,
                    )
                except _MoreFiles:
                    continue

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
        return await self._publish_files(
            principal, workspace_id, files, expected_revision=expected_revision
        )

    async def _publish_files(
        self,
        principal: Principal,
        workspace_id: str,
        files: Sequence[AdmittedFile],
        *,
        expected_revision: str | None,
        replace: bool = False,
        before_publish: Callable[[Snapshot], Awaitable[None]] | None = None,
    ) -> Snapshot:
        validate_paths([item.path for item in files])
        if any(len(item.content) > self.limits.xml_file_bytes for item in files):
            raise ToolError("quota_exceeded", "File byte limit exceeded")
        async with self.workspaces.lease(principal, workspace_id):
            previous = await self.state.run(lambda db: self._current(db, workspace_id))
            if (None if previous is None else previous.revision) != expected_revision:
                raise ToolError("stale_reference", "Workspace revision changed")
            records = (
                {} if previous is None or replace else {item.path: item for item in previous.files}
            )
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
                    if before_publish is not None:
                        await before_publish(snapshot)
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
