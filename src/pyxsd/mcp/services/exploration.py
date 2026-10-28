"""Schema/document exploration over temporary session input captures."""

import hashlib
import json
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from pydantic import JsonValue

from ..audit import AuditLogger
from ..captures import InputCapture
from ..contracts import (
    ExplainIssueRequest,
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
from ..inputs import SessionInputs
from ..output_store import OutputStore
from ..pagination import PageTokens
from ..worker_protocol import (
    DescribeOperation,
    FindOperation,
    ParseOperation,
    QueryOperation,
    WorkerReply,
)
from ..workers import WorkerPool, capture_to_wire

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
        inputs: SessionInputs,
        pool: WorkerPool,
        *,
        outputs: OutputStore | None = None,
        tokens: PageTokens | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.inputs, self.pool = inputs, pool
        self.outputs = outputs
        self.tokens = PageTokens() if tokens is None else tokens
        self.audit = AuditLogger() if audit is None else audit

    @asynccontextmanager
    async def _capture(self, workspace_id: str) -> AsyncIterator[InputCapture]:
        async with self.inputs.capture(workspace_id) as capture:
            yield capture

    @staticmethod
    def _fingerprint(capture: InputCapture, schema: SchemaRef | None) -> str:
        data = {
            "files": [(item.path, item.digest, item.size) for item in capture.files],
            "schema": None if schema is None else schema.model_dump(mode="json"),
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def _provenance(
        self, capture: InputCapture, schema: SchemaRef | None, source_path: str
    ) -> dict[str, JsonValue]:
        item = next((item for item in capture.files if item.path == source_path), None)
        return {
            "workspace_id": capture.workspace_id,
            "generation": capture.generation,
            "source": {
                "path": source_path,
                "digest": None if item is None else item.digest,
                "size": None if item is None else item.size,
            },
            "schema_fingerprint": self._fingerprint(capture, schema),
            "options": (
                {"hinted": True} if schema is None else schema.options.model_dump(mode="json")
            ),
        }

    @staticmethod
    def _result_id(request: Any, capture: InputCapture) -> str:
        data = {
            "request": request.model_dump(mode="json", exclude={"page"}),
            "generation": capture.generation,
            "files": [(item.path, item.digest, item.size) for item in capture.files],
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def _position(self, page: Page, result_id: str) -> tuple[int, int]:
        if page.limit > self.pool.limits.page_items:
            raise ToolError("invalid_argument", "Page limit exceeds the operator budget")
        offset = 0 if page.cursor is None else self.tokens.decode_page(result_id, page.cursor)
        return offset, page.limit

    def _envelope(
        self,
        reply: WorkerReply,
        *,
        provenance: dict[str, JsonValue],
        result_id: str,
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
                self.tokens.encode_page(result_id, offset + returned) if truncated else None
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
        started: float,
        status: str,
        capture: InputCapture | None,
    ) -> None:
        self.audit.emit(
            operation=operation,
            status=status,
            duration=time.monotonic() - started,
            workspace_id=None if capture is None else capture.workspace_id,
            generation=None if capture is None else capture.generation,
        )

    async def inspect_schema(self, request: InspectSchemaRequest) -> ToolEnvelope:
        started, status, capture = time.monotonic(), "ok", None
        try:
            async with self._capture(request.workspace_id) as capture:
                result_id = self._result_id(request, capture)
                offset, limit = self._position(request.page, result_id)
                reply = await self.pool.run(
                    DescribeOperation(
                        request_id=secrets.token_hex(16),
                        capture=capture_to_wire(capture),
                        schema_ref=request.schema_ref,
                        component_ids=request.component_ids,
                        view=request.view,
                        depth=request.depth,
                        offset=offset,
                        limit=limit,
                    ),
                    deadline=time.monotonic() + self.pool.limits.compile_query_seconds,
                )
                return self._envelope(
                    reply,
                    provenance=self._provenance(
                        capture, request.schema_ref, request.schema_ref.source.path
                    ),
                    result_id=result_id,
                    offset=offset,
                )
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("inspect_schema", started, status, capture)

    async def find_schema_components(self, request: FindComponentsRequest) -> ToolEnvelope:
        started, status, capture = time.monotonic(), "ok", None
        try:
            async with self._capture(request.workspace_id) as capture:
                result_id = self._result_id(request, capture)
                offset, limit = self._position(request.page, result_id)
                reply = await self.pool.run(
                    FindOperation(
                        request_id=secrets.token_hex(16),
                        capture=capture_to_wire(capture),
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
                    ),
                    deadline=time.monotonic() + self.pool.limits.compile_query_seconds,
                )
                return self._envelope(
                    reply,
                    provenance=self._provenance(
                        capture, request.schema_ref, request.schema_ref.source.path
                    ),
                    result_id=result_id,
                    offset=offset,
                )
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("find_schema_components", started, status, capture)

    async def validate_document(self, request: ValidateDocumentRequest) -> ToolEnvelope:
        started, status, capture = time.monotonic(), "ok", None
        try:
            async with self._capture(request.workspace_id) as capture:
                result_id = self._result_id(request, capture)
                offset, limit = self._position(request.page, result_id)
                reply = await self.pool.run(
                    ParseOperation(
                        request_id=secrets.token_hex(16),
                        capture=capture_to_wire(capture),
                        schema_ref=request.schema_ref,
                        document=request.document,
                        issue_offset=offset,
                        issue_limit=limit,
                    ),
                    deadline=time.monotonic() + self.pool.limits.compile_query_seconds,
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
                envelope = self._envelope(
                    reply,
                    provenance=self._provenance(capture, request.schema_ref, request.document.path),
                    result_id=result_id,
                    offset=offset,
                )
                report_data = (
                    envelope.data.get("report") if isinstance(envelope.data, dict) else None
                )
                if self.outputs is not None and isinstance(report_data, dict):
                    reference = await self.outputs.put_bytes(
                        json.dumps(report_data, separators=(",", ":")).encode(),
                        name="report.json",
                        media_type="application/json",
                        kind="report",
                    )
                    from ..contracts import ArtifactRef

                    envelope = envelope.model_copy(
                        update={
                            "artifacts": [
                                ArtifactRef(
                                    id=reference.id,
                                    path=str(reference.path),
                                    media_type=reference.media_type,
                                    size=reference.size,
                                    digest=reference.digest,
                                    created_at=reference.created_at,
                                    kind=reference.kind,
                                )
                            ]
                        }
                    )
                return envelope
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("validate_document", started, status, capture)

    async def query_document(self, request: QueryDocumentRequest) -> ToolEnvelope:
        started, status, capture = time.monotonic(), "ok", None
        try:
            async with self._capture(request.workspace_id) as capture:
                result_id = self._result_id(request, capture)
                offset, limit = self._position(request.page, result_id)
                reply = await self.pool.run(
                    QueryOperation(
                        request_id=secrets.token_hex(16),
                        capture=capture_to_wire(capture),
                        schema_ref=request.schema_ref,
                        document=request.document,
                        xpath=request.xpath,
                        namespaces=request.namespaces,
                        result=request.result,
                        offset=offset,
                        limit=limit,
                    ),
                    deadline=time.monotonic() + self.pool.limits.compile_query_seconds,
                )
                return self._envelope(
                    reply,
                    provenance=self._provenance(capture, request.schema_ref, request.document.path),
                    result_id=result_id,
                    offset=offset,
                )
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("query_document", started, status, capture)

    async def explain_issue(self, request: ExplainIssueRequest) -> ToolEnvelope:
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
                if request.report is None or request.issue_index is None or self.outputs is None:
                    raise ToolError("invalid_argument", "Issue lookup needs a report and index")
                data["issue"] = await self._issue(request)
            return ToolEnvelope(data=data)
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("explain_issue", started, status, None)

    async def _issue(self, request: ExplainIssueRequest) -> JsonValue:
        assert request.report is not None and request.issue_index is not None
        assert self.outputs is not None
        ref = request.report
        chunks = []
        for offset in range(0, ref.size, self.outputs.limits.result_bytes):
            chunks.append(
                await self.outputs.read(
                    ref.id,
                    offset=offset,
                    length=min(self.outputs.limits.result_bytes, ref.size - offset),
                )
            )
        try:
            report = json.loads(b"".join(chunks))
            return report["issues"][request.issue_index]
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ToolError("not_found", "Issue reference is unavailable") from exc
