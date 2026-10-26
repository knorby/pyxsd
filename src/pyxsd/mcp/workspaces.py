"""Owner-scoped workspace lifecycle and transactional disk reservations."""

import secrets
import shutil
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

import anyio

from .config import Limits
from .contracts import WireModel
from .errors import ToolError
from .state import State


@dataclass(frozen=True)
class Principal:
    """Trusted identity injected by a transport, never accepted in tool arguments."""

    owner_id: str
    scopes: frozenset[str]

    def require(self, *scopes: str) -> None:
        if not self.scopes.issuperset(scopes):
            raise ToolError("forbidden", "Required permission is missing")


def stdio_principal() -> Principal:
    """Local state belongs to the process's OS user, not a client-supplied owner."""
    return Principal(
        "stdio",
        frozenset(
            {
                "pyxsd:read",
                "pyxsd:upload",
                "pyxsd:execute",
                "pyxsd:write",
                "pyxsd:replace",
                "pyxsd:write-invalid",
            }
        ),
    )


class Workspace(WireModel):
    id: str
    created_at: float
    expires_at: float


def _workspace(row: sqlite3.Row) -> Workspace:
    return Workspace(id=row["id"], created_at=row["created"], expires_at=row["expires"])


class WorkspaceStore:
    def __init__(
        self, state: State, limits: Limits, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.state, self.limits, self.clock = state, limits, clock

    def _cleanup(self, db: sqlite3.Connection) -> None:
        now = self.clock()
        for row in db.execute(
            "SELECT id FROM workspaces WHERE status='closing' AND leases=0"
        ).fetchall():
            path = self.state.root / "workspaces" / row["id"]
            if path.exists():
                shutil.rmtree(path)
            db.execute("DELETE FROM snapshots WHERE workspace=?", (row["id"],))
            db.execute("DELETE FROM reservations WHERE workspace=?", (row["id"],))
            db.execute(
                "UPDATE workspaces SET status='closed', closed=?, files=0, artifacts=0, "
                "reserved_files=0, reserved_artifacts=0 WHERE id=?",
                (now, row["id"]),
            )
        db.execute(
            "DELETE FROM workspaces WHERE status='closed' AND (create_key IS NULL OR closed<=?)",
            (now - self.limits.workspace_idle_seconds,),
        )

    async def sweep(self) -> None:
        # Persist the closing intent before touching files. A crash or failed
        # cleanup must never make a partially removed workspace leasable again.
        await self.state.run(
            lambda db: db.execute(
                "UPDATE workspaces SET status='closing' WHERE status='open' AND expires<=?",
                (self.clock(),),
            ).close()
        )
        await self.state.run(self._cleanup)

    async def create(
        self, principal: Principal, *, idempotency_key: str | None = None
    ) -> Workspace:
        principal.require("pyxsd:upload")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128
        ):
            raise ToolError("invalid_argument", "Creation key must contain 1 to 128 characters")
        await self.sweep()

        def create(db: sqlite3.Connection) -> Workspace:
            if idempotency_key is not None:
                prior = db.execute(
                    "SELECT * FROM workspaces WHERE owner=? AND create_key=?",
                    (principal.owner_id, idempotency_key),
                ).fetchone()
                if prior is not None:
                    if prior["status"] != "open":
                        raise ToolError("stale_reference", "Workspace creation result has expired")
                    return _workspace(prior)
                retained = db.execute(
                    "SELECT count(*) FROM workspaces WHERE create_key IS NOT NULL"
                ).fetchone()[0]
                if retained >= self.limits.workspaces:
                    raise ToolError("quota_exceeded", "Workspace retry-record limit reached")
            if (
                db.execute("SELECT count(*) FROM workspaces WHERE status!='closed'").fetchone()[0]
                >= self.limits.workspaces
            ):
                raise ToolError("quota_exceeded", "Workspace limit reached")
            identifier = secrets.token_hex(16)
            now = self.clock()
            try:
                db.execute(
                    "INSERT INTO workspaces(id,owner,create_key,created,expires) VALUES(?,?,?,?,?)",
                    (
                        identifier,
                        principal.owner_id,
                        idempotency_key,
                        now,
                        now + self.limits.workspace_idle_seconds,
                    ),
                )
                (self.state.root / "workspaces" / identifier).mkdir(mode=0o700)
            except (sqlite3.IntegrityError, FileExistsError) as exc:
                raise ToolError(
                    "allocation_conflict", "Workspace identifier is unavailable", retryable=True
                ) from exc
            return Workspace(
                id=identifier, created_at=now, expires_at=now + self.limits.workspace_idle_seconds
            )

        return await self.state.run(create)

    async def list(self, principal: Principal) -> tuple[Workspace, ...]:
        principal.require("pyxsd:read")
        await self.sweep()

        def listing(db: sqlite3.Connection) -> tuple[Workspace, ...]:
            return tuple(
                _workspace(row)
                for row in db.execute(
                    "SELECT * FROM workspaces WHERE owner=? AND status='open' ORDER BY created,id",
                    (principal.owner_id,),
                )
            )

        return await self.state.run(listing)

    @asynccontextmanager
    async def lease(self, principal: Principal, workspace_id: str) -> AsyncIterator[Workspace]:
        await self.sweep()

        def acquire(db: sqlite3.Connection) -> Workspace | None:
            row = db.execute(
                "SELECT * FROM workspaces WHERE id=? AND owner=? AND status='open' AND expires>?",
                (workspace_id, principal.owner_id, self.clock()),
            ).fetchone()
            if row is None:
                return None
            expires = self.clock() + self.limits.workspace_idle_seconds
            db.execute(
                "UPDATE workspaces SET leases=leases+1, expires=? WHERE id=?",
                (expires, workspace_id),
            )
            return Workspace(id=workspace_id, created_at=row["created"], expires_at=expires)

        workspace = await self.state.run(acquire)
        if workspace is None:
            raise ToolError("not_found", "Workspace not found")
        try:
            yield workspace
        finally:

            def relinquish(db: sqlite3.Connection) -> None:
                db.execute("UPDATE workspaces SET leases=leases-1 WHERE id=?", (workspace_id,))

            with anyio.CancelScope(shield=True):
                await self.state.run(relinquish)
                await self.sweep()

    async def release(self, principal: Principal, workspace_id: str) -> None:
        principal.require("pyxsd:upload")

        def release(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE workspaces SET status='closing' WHERE id=? AND owner=? AND status='open'",
                (workspace_id, principal.owner_id),
            )

        await self.state.run(release)
        await self.sweep()

    @asynccontextmanager
    async def reserve(
        self,
        principal: Principal,
        workspace_id: str,
        size: int,
        *,
        kind: Literal["files", "artifacts"] = "files",
    ) -> AsyncIterator["Reservation"]:
        principal.require("pyxsd:upload")
        if type(size) is not int or size < 0 or kind not in ("files", "artifacts"):
            raise ToolError("invalid_argument", "Invalid quota reservation")
        async with self.lease(principal, workspace_id):
            identifier = secrets.token_hex(16)

            def reserve(db: sqlite3.Connection) -> None:
                row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
                used = row[kind] + row[f"reserved_{kind}"]
                limit = (
                    self.limits.workspace_bytes
                    if kind == "files"
                    else self.limits.artifact_bytes_per_workspace
                )
                total = db.execute(
                    "SELECT coalesce(sum(files+artifacts+reserved_files+reserved_artifacts),0) FROM workspaces"
                ).fetchone()[0]
                if used + size > limit or total + size > self.limits.managed_bytes:
                    raise ToolError("quota_exceeded", "Workspace or aggregate disk limit reached")
                db.execute(
                    "INSERT INTO reservations(id,workspace,kind,reserved) VALUES(?,?,?,?)",
                    (identifier, workspace_id, kind, size),
                )
                db.execute(
                    f"UPDATE workspaces SET reserved_{kind}=reserved_{kind}+? WHERE id=?",
                    (size, workspace_id),
                )

            await self.state.run(reserve)
            try:
                yield Reservation(self.state, identifier)
            finally:

                def finish(db: sqlite3.Connection) -> None:
                    row = db.execute(
                        "SELECT * FROM reservations WHERE id=?", (identifier,)
                    ).fetchone()
                    if row["committed"] is None:
                        if (
                            db.execute(
                                "SELECT 1 FROM snapshots WHERE reservation=? AND status='staging'",
                                (identifier,),
                            ).fetchone()
                            is not None
                        ):
                            # Failed staging cleanup still occupies disk. Keep
                            # the reservation until recovery removes the files.
                            return
                        db.execute(
                            f"UPDATE workspaces SET reserved_{kind}=reserved_{kind}-? WHERE id=?",
                            (size, workspace_id),
                        )
                    db.execute("DELETE FROM reservations WHERE id=?", (identifier,))

                with anyio.CancelScope(shield=True):
                    await self.state.run(finish)


class Reservation:
    def __init__(self, state: State, identifier: str) -> None:
        self._state, self._id = state, identifier

    @property
    def id(self) -> str:
        """Internal durable-operation linkage, never a caller authorization token."""
        return self._id

    async def commit(
        self,
        *,
        size: int | None = None,
        on_commit: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """Commit usage and optional SQL-only publication metadata atomically.

        The callback runs once, in the state transaction's worker thread. It
        must not perform filesystem publication or start a nested transaction.
        A callback failure rolls back both its metadata and the usage charge.
        Repeating a successful commit does not invoke the callback again.
        """

        def commit(db: sqlite3.Connection) -> None:
            row = db.execute("SELECT * FROM reservations WHERE id=?", (self._id,)).fetchone()
            if row is None:
                raise ToolError("stale_reference", "Reservation is no longer active")
            actual = row["reserved"] if size is None else size
            if type(actual) is not int or not 0 <= actual <= row["reserved"]:
                raise ToolError("invalid_argument", "Committed size exceeds reservation")
            if row["committed"] is not None:
                if row["committed"] != actual:
                    raise ToolError(
                        "idempotency_conflict", "Reservation already committed a different size"
                    )
                return
            if on_commit is not None:
                on_commit(db)
            kind = row["kind"]
            db.execute(
                f"UPDATE workspaces SET reserved_{kind}=reserved_{kind}-?, {kind}={kind}+? WHERE id=?",
                (row["reserved"], actual, row["workspace"]),
            )
            db.execute("UPDATE reservations SET committed=? WHERE id=?", (actual, self._id))

        await self._state.run(commit)
