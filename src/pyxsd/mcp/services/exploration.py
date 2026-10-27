"""Schema/document exploration with authenticated pages and complete provenance."""

import hashlib
import json
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from pydantic import JsonValue

from ..artifacts import ArtifactStore
from ..audit import AuditLogger
from ..contracts import (
    ExplainIssueRequest,
    FileRef,
    FindComponentsRequest,
    InspectSchemaRequest,
    Page,
    PageResult,
    QueryDocumentRequest,
    SchemaRef,
    ToolEnvelope,
    ValidateDocumentRequest,
)
from ..errors import ToolError
from ..pagination import CursorCodec
from ..snapshot_store import SnapshotStore
from ..snapshots import Snapshot
from ..worker_protocol import (
    DescribeOperation,
    FindOperation,
    ParseOperation,
    QueryOperation,
    WorkerReply,
)
from ..workers import WorkerPool, snapshot_to_wire
from ..workspaces import Principal

_EXPLANATIONS: dict[str, dict[str, JsonValue]] = {
    "missing-attribute": {
        "summary": "A required attribute is absent from an element.",
        "actions": ["Inspect the element location and add the declared attribute."],
        "uncertainty": "Defaults and parse modes can affect the bound tree, not this requirement.",
    },
    "value": {
        "summary": "Text content does not satisfy the declared datatype or facets.",
        "actions": ["Compare the lexical value with the declared type and constraint evidence."],
        "uncertainty": "A parse mode does not relax facet validation.",
    },
    "unknown-root": {
        "summary": "The document root matches no global element declaration.",
        "actions": ["Check the root's expanded name and the schemas selected for validation."],
        "uncertainty": "An unbound root cannot be queried as a usable document tree.",
    },
    "declaration-duplicate": {
        "summary": "Two declarations occupy the same symbol space or exclusive declaration slot.",
        "actions": ["Compare each declaration's source occurrence and expanded name."],
        "uncertainty": "Introspection preserves invalid duplicate declarations for diagnosis.",
    },
    "identity-key": {
        "summary": "A key field is missing or a key value is duplicated in its constraint scope.",
        "actions": ["Inspect the key selector, fields and related occurrence locations."],
        "uncertainty": "Identity constraints use the processor's documented XPath subset.",
    },
    "identity-keyref": {
        "summary": "A keyref value has no matching key or unique value in scope.",
        "actions": ["Check the referenced constraint and the candidate key occurrence."],
        "uncertainty": "A keyref check depends on the selected validation root and options.",
    },
    "schema-hint": {
        "summary": "An advisory schema-location hint could not be used.",
        "actions": ["Check the hinted resource and explicit namespace/schema mappings."],
        "uncertainty": "Policy denials are failures, not harmless missing hints.",
    },
}


def _valid_report(report: JsonValue) -> bool:
    if not isinstance(report, dict):
        return False
    counts = report.get("counts")
    return isinstance(counts, dict) and counts.get("error") == 0


class ExplorationService:
    def __init__(
        self,
        snapshots: SnapshotStore,
        pool: WorkerPool,
        *,
        artifacts: ArtifactStore | None = None,
        cursors: CursorCodec | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.snapshots, self.pool = snapshots, pool
        self.artifacts = artifacts
        self.cursors = CursorCodec() if cursors is None else cursors
        self.audit = AuditLogger() if audit is None else audit

    @asynccontextmanager
    async def _snapshot(
        self, principal: Principal, workspace_id: str, revision: str | None
    ) -> AsyncIterator[Snapshot]:
        async with self.snapshots.workspaces.lease(principal, workspace_id):
            try:
                snapshot = await self.snapshots.get(principal, workspace_id, revision=revision)
            except ToolError as exc:
                if revision is not None and exc.fault.code == "not_found":
                    raise ToolError("stale_reference", "Requested revision is unavailable") from exc
                raise
            yield snapshot

    @staticmethod
    def _revision(document: FileRef, schema: SchemaRef | None) -> str | None:
        schema_revision = None if schema is None else schema.source.revision
        if (
            document.revision is not None
            and schema_revision is not None
            and document.revision != schema_revision
        ):
            raise ToolError("stale_reference", "Document and schema revisions differ")
        return document.revision or schema_revision

    @staticmethod
    def _fingerprint(snapshot: Snapshot, schema: SchemaRef | None) -> str:
        data = {
            "files": [(item.path, item.digest, item.size) for item in snapshot.files],
            "schema": None if schema is None else schema.model_dump(mode="json"),
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def _provenance(
        self, snapshot: Snapshot, schema: SchemaRef | None, source_path: str
    ) -> dict[str, JsonValue]:
        item = next((item for item in snapshot.files if item.path == source_path), None)
        return {
            "workspace_id": snapshot.workspace_id,
            "revision": snapshot.revision,
            "source": {
                "path": source_path,
                "digest": None if item is None else item.digest,
                "size": None if item is None else item.size,
            },
            "schema_fingerprint": self._fingerprint(snapshot, schema),
            "options": (
                {"hinted": True} if schema is None else schema.options.model_dump(mode="json")
            ),
        }

    @staticmethod
    def _digest(request: Any) -> str:
        data = request.model_dump(mode="json", exclude={"page"})
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def _position(
        self, principal: Principal, page: Page, revision: str, digest: str
    ) -> tuple[int, int]:
        if page.limit > self.pool.limits.page_items:
            raise ToolError("invalid_argument", "Page limit exceeds the operator budget")
        offset = (
            0
            if page.cursor is None
            else self.cursors.decode_page(principal, revision, digest, page.cursor)
        )
        return offset, page.limit

    def _envelope(
        self,
        principal: Principal,
        reply: WorkerReply,
        *,
        provenance: dict[str, JsonValue],
        snapshot: Snapshot,
        query_digest: str,
        offset: int = 0,
    ) -> ToolEnvelope:
        if reply.status == "error":
            assert reply.fault is not None
            raise ToolError(
                reply.fault.code,
                reply.fault.message,
                retryable=reply.fault.retryable,
                details={**reply.fault.details, **provenance},
            )
        if not isinstance(reply.result, dict):
            raise ToolError("ipc_protocol", "Worker returned malformed service data")
        data = dict(reply.result)
        page_data = data.pop("page", None)
        page = None
        if isinstance(page_data, dict):
            returned = int(cast(int, page_data["returned"]))
            truncated = bool(page_data["truncated"])
            next_cursor = (
                self.cursors.encode_page(
                    principal, snapshot.revision, query_digest, offset + returned
                )
                if truncated
                else None
            )
            page = PageResult(
                returned=returned,
                next_cursor=next_cursor,
                truncated=truncated,
                total=int(cast(int, page_data["total"])),
            )
        return ToolEnvelope(data=data, provenance=provenance, page=page)

    def _audit(
        self,
        operation: str,
        principal: Principal,
        started: float,
        status: str,
        snapshot: Snapshot | None,
    ) -> None:
        self.audit.emit(
            operation=operation,
            owner_id=principal.owner_id,
            status=status,
            duration=time.monotonic() - started,
            workspace_id=None if snapshot is None else snapshot.workspace_id,
            revision=None if snapshot is None else snapshot.revision,
        )

    async def inspect_schema(
        self, principal: Principal, request: InspectSchemaRequest
    ) -> ToolEnvelope:
        started, status = time.monotonic(), "ok"
        snapshot = None
        try:
            async with self._snapshot(
                principal, request.workspace_id, request.schema_ref.source.revision
            ) as snapshot:
                digest = self._digest(request)
                offset, limit = self._position(principal, request.page, snapshot.revision, digest)
                operation = DescribeOperation(
                    request_id=secrets.token_hex(16),
                    snapshot=snapshot_to_wire(snapshot),
                    owner_id=principal.owner_id,
                    schema_ref=request.schema_ref,
                    component_ids=request.component_ids,
                    view=request.view,
                    depth=request.depth,
                    offset=offset,
                    limit=limit,
                )
                reply = await self.pool.run(
                    operation, deadline=time.monotonic() + self.pool.limits.compile_query_seconds
                )
                return self._envelope(
                    principal,
                    reply,
                    provenance=self._provenance(
                        snapshot, request.schema_ref, request.schema_ref.source.path
                    ),
                    snapshot=snapshot,
                    query_digest=digest,
                    offset=offset,
                )
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("inspect_schema", principal, started, status, snapshot)

    async def find_schema_components(
        self, principal: Principal, request: FindComponentsRequest
    ) -> ToolEnvelope:
        started, status = time.monotonic(), "ok"
        snapshot = None
        try:
            async with self._snapshot(
                principal, request.workspace_id, request.schema_ref.source.revision
            ) as snapshot:
                digest = self._digest(request)
                offset, limit = self._position(principal, request.page, snapshot.revision, digest)
                operation = FindOperation(
                    request_id=secrets.token_hex(16),
                    snapshot=snapshot_to_wire(snapshot),
                    owner_id=principal.owner_id,
                    schema_ref=request.schema_ref,
                    text=request.text,
                    component_kind=request.kind,
                    name=request.name,
                    namespace=request.namespace,
                    component_id=request.component_id,
                    direction=request.direction,
                    depth=request.depth,
                    offset=offset,
                    limit=limit,
                )
                reply = await self.pool.run(
                    operation, deadline=time.monotonic() + self.pool.limits.compile_query_seconds
                )
                return self._envelope(
                    principal,
                    reply,
                    provenance=self._provenance(
                        snapshot, request.schema_ref, request.schema_ref.source.path
                    ),
                    snapshot=snapshot,
                    query_digest=digest,
                    offset=offset,
                )
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("find_schema_components", principal, started, status, snapshot)

    async def validate_document(
        self, principal: Principal, request: ValidateDocumentRequest
    ) -> ToolEnvelope:
        started, status = time.monotonic(), "ok"
        snapshot = None
        try:
            revision = self._revision(request.document, request.schema_ref)
            async with self._snapshot(principal, request.workspace_id, revision) as snapshot:
                digest = self._digest(request)
                offset, limit = self._position(principal, request.page, snapshot.revision, digest)
                operation = ParseOperation(
                    request_id=secrets.token_hex(16),
                    snapshot=snapshot_to_wire(snapshot),
                    owner_id=principal.owner_id,
                    schema_ref=request.schema_ref,
                    document=request.document,
                    issue_offset=offset,
                    issue_limit=limit,
                )
                reply = await self.pool.run(
                    operation, deadline=time.monotonic() + self.pool.limits.compile_query_seconds
                )
                if reply.status == "ok" and isinstance(reply.result, dict):
                    report = reply.result.get("report")
                    reply = reply.model_copy(
                        update={
                            "result": {
                                **reply.result,
                                "valid": _valid_report(report),
                                "binding_available": reply.result.get("root_present"),
                            }
                        }
                    )
                return self._envelope(
                    principal,
                    reply,
                    provenance=self._provenance(
                        snapshot, request.schema_ref, request.document.path
                    ),
                    snapshot=snapshot,
                    query_digest=digest,
                    offset=offset,
                )
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("validate_document", principal, started, status, snapshot)

    async def query_document(
        self, principal: Principal, request: QueryDocumentRequest
    ) -> ToolEnvelope:
        started, status = time.monotonic(), "ok"
        snapshot = None
        try:
            revision = self._revision(request.document, request.schema_ref)
            async with self._snapshot(principal, request.workspace_id, revision) as snapshot:
                digest = self._digest(request)
                offset, limit = self._position(principal, request.page, snapshot.revision, digest)
                operation = QueryOperation(
                    request_id=secrets.token_hex(16),
                    snapshot=snapshot_to_wire(snapshot),
                    owner_id=principal.owner_id,
                    schema_ref=request.schema_ref,
                    document=request.document,
                    xpath=request.xpath,
                    namespaces=request.namespaces,
                    result=request.result,
                    offset=offset,
                    limit=limit,
                )
                reply = await self.pool.run(
                    operation, deadline=time.monotonic() + self.pool.limits.compile_query_seconds
                )
                return self._envelope(
                    principal,
                    reply,
                    provenance=self._provenance(
                        snapshot, request.schema_ref, request.document.path
                    ),
                    snapshot=snapshot,
                    query_digest=digest,
                    offset=offset,
                )
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("query_document", principal, started, status, snapshot)

    async def explain_issue(
        self, principal: Principal, request: ExplainIssueRequest
    ) -> ToolEnvelope:
        started, status = time.monotonic(), "ok"
        try:
            known = request.code in _EXPLANATIONS
            data: dict[str, JsonValue] = (
                {"code": request.code, "known": True, **_EXPLANATIONS[request.code]}
                if known
                else {
                    "code": request.code,
                    "known": False,
                    "summary": "Unknown issue code",
                    "actions": [],
                    "uncertainty": "No curated explanation is available for this code.",
                }
            )
            if request.report is not None or request.issue_index is not None:
                if request.report is None or request.issue_index is None or self.artifacts is None:
                    raise ToolError("invalid_argument", "Issue lookup needs a report and index")
                data["issue"] = await self._issue(principal, request)
            return ToolEnvelope(data=data)
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("explain_issue", principal, started, status, None)

    async def _issue(self, principal: Principal, request: ExplainIssueRequest) -> JsonValue:
        assert (
            request.report is not None
            and request.issue_index is not None
            and self.artifacts is not None
        )
        ref = request.report
        chunks = []
        for offset in range(0, ref.size, self.artifacts.limits.result_bytes):
            chunks.append(
                await self.artifacts.read(
                    principal,
                    ref.id,
                    offset=offset,
                    length=min(self.artifacts.limits.result_bytes, ref.size - offset),
                )
            )
        try:
            report = json.loads(b"".join(chunks))
            return report["issues"][request.issue_index]
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ToolError("not_found", "Issue reference is unavailable") from exc
