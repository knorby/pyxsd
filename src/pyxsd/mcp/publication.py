"""Idempotent exact-byte publication into coordinator-managed revisions only."""

import hashlib
import sqlite3
from typing import Literal

from pydantic import Field

from .contracts import Nonempty, WireModel
from .errors import ToolError
from .previews import PreviewService
from .safe_io import AdmittedFile, SafeDirectory, validate_paths
from .snapshots import Snapshot
from .workspaces import Principal


class CommitTransformRequest(WireModel):
    preview_id: Nonempty
    expected_candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination: Nonempty
    mode: Literal["new", "replace"] = "new"
    expected_destination_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)
    allow_invalid: bool = False


class PublicationReceipt(WireModel):
    preview_id: Nonempty
    workspace_id: Nonempty
    revision: Nonempty
    destination: Nonempty
    candidate_digest: Nonempty
    valid: bool


class PublicationService:
    def __init__(self, previews: PreviewService, *, write_invalid: bool = False) -> None:
        self.previews = previews
        self.snapshots, self.artifacts = previews.snapshots, previews.artifacts
        self.state = self.snapshots.state
        self.write_invalid = write_invalid
        self._lock = self.state.publication_lock

    async def commit(
        self, principal: Principal, request: CommitTransformRequest
    ) -> PublicationReceipt:
        principal.require("pyxsd:write", "pyxsd:read", "pyxsd:upload")
        if request.mode == "replace":
            principal.require("pyxsd:replace")
        validate_paths([request.destination])
        encoded = request.model_dump_json()
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        async with self._lock:
            row = await self.state.run(
                lambda db: db.execute(
                    "SELECT * FROM publications WHERE owner=? AND idempotency=?",
                    (principal.owner_id, request.idempotency_key),
                ).fetchone()
            )
            if row is not None:
                if row["digest"] != digest:
                    raise ToolError("idempotency_conflict", "Publication key has different input")
                if row["receipt"] is not None:
                    return PublicationReceipt.model_validate_json(row["receipt"])
            consumed = await self.state.run(
                lambda db: db.execute(
                    "SELECT idempotency FROM publications WHERE owner=? AND preview=?",
                    (principal.owner_id, request.preview_id),
                ).fetchone()
            )
            if consumed is not None and consumed["idempotency"] != request.idempotency_key:
                raise ToolError("preview_consumed", "Preview already has a publication")
            record = await self.previews.get(principal, request.preview_id)
            if record.candidate.digest != request.expected_candidate_digest:
                raise ToolError("candidate_mismatch", "Candidate digest differs from preview")
            if not record.well_formed:
                raise ToolError("invalid_candidate", "Malformed XML cannot be published")
            if not record.valid:
                if not self.write_invalid or not request.allow_invalid:
                    raise ToolError("invalid_candidate", "Publication requires a valid candidate")
                principal.require("pyxsd:write-invalid")
            async with (
                self.snapshots.workspaces.lease(principal, record.workspace_id),
                self.artifacts.lease(principal, record.candidate.id) as reader,
            ):
                current = await self.snapshots.get(principal, record.workspace_id)
                if current.revision != record.revision:
                    raise ToolError("stale_reference", "Source or schema revision changed")
                destination = next(
                    (item for item in current.files if item.path == request.destination), None
                )
                if request.mode == "new":
                    if destination is not None:
                        raise ToolError("publication_conflict", "Destination already exists")
                elif (
                    destination is None or request.expected_destination_digest != destination.digest
                ):
                    raise ToolError("publication_conflict", "Replacement digest does not match")
                with SafeDirectory(current.root) as directory:
                    for item in current.files:
                        actual = directory.read(
                            item.path, max_bytes=self.previews.limits.xml_file_bytes
                        )
                        if actual.digest != item.digest:
                            raise ToolError("source_changed", "Snapshot bytes changed")
                content = b"".join(
                    [
                        await reader.read(
                            offset=offset,
                            length=min(self.previews.limits.result_bytes, reader.ref.size - offset),
                        )
                        for offset in range(0, reader.ref.size, self.previews.limits.result_bytes)
                    ]
                )
                if hashlib.sha256(content).hexdigest() != request.expected_candidate_digest:
                    raise ToolError("candidate_mismatch", "Retained bytes differ from candidate")

                def intent(db: sqlite3.Connection) -> None:
                    consumed = db.execute(
                        "SELECT owner,idempotency FROM publications WHERE preview=?", (record.id,)
                    ).fetchone()
                    if consumed is not None:
                        # The shared coordinator lock and the checks above bind
                        # an existing intent to this exact owner, key and input.
                        return
                    db.execute(
                        "INSERT INTO publications VALUES(?,?,?,?,?,?,NULL)",
                        (
                            principal.owner_id,
                            request.idempotency_key,
                            digest,
                            record.id,
                            record.workspace_id,
                            encoded,
                        ),
                    )

                await self.state.run(intent)
                receipts = []

                def publish(db: sqlite3.Connection, snapshot: Snapshot) -> None:
                    receipt = PublicationReceipt(
                        preview_id=record.id,
                        workspace_id=record.workspace_id,
                        revision=snapshot.revision,
                        destination=request.destination,
                        candidate_digest=request.expected_candidate_digest,
                        valid=record.valid,
                    )
                    db.execute(
                        "UPDATE publications SET receipt=? WHERE owner=? AND idempotency=?",
                        (receipt.model_dump_json(), principal.owner_id, request.idempotency_key),
                    )
                    receipts.append(receipt)

                await self.snapshots._publish_files(
                    principal,
                    record.workspace_id,
                    [AdmittedFile(request.destination, content)],
                    expected_revision=record.revision,
                    on_publish=publish,
                )
                return receipts[0]
