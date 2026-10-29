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
    FileRef,
    FindComponentsRequest,
    InspectSchemaRequest,
    Page,
    PageResult,
    QueryDocumentRequest,
    SchemaRef,
    ToolEnvelope,
    ValidateDocumentRequest,
    ValidateDocumentsRequest,
)
from ..errors import ToolError
from ..inputs import SessionInputs
from ..output_store import OutputStore
from ..pagination import PageTokens
from ..worker_protocol import (
    BatchParseOperation,
    DescribeOperation,
    FindOperation,
    ParseOperation,
    QueryOperation,
    WorkerReply,
)
from ..workers import WorkerPool, capture_to_wire

_EXPLANATIONS: dict[str, dict[str, JsonValue]] = {
    "order": {
        "summary": "Child elements do not match the declared content-model order.",
        "actions": ["Compare the parent's children with its declared sequence or choice."],
        "uncertainty": "Recovery may identify the parent rather than one offending child.",
    },
    "invalid-attribute": {
        "summary": "An attribute is not accepted by the element declaration.",
        "actions": ["Check its expanded name, declared attributes, and wildcard policy."],
        "uncertainty": "Namespace and parse options affect attribute binding.",
    },
    "occurrence-min": {
        "summary": "Required child content is missing from its container.",
        "actions": ["Inspect the parent's required children and minimum occurrence counts."],
        "uncertainty": "The location is the container, not a nonexistent missing child.",
    },
    "unexpected-element": {
        "summary": "A child element is not accepted at this content-model position.",
        "actions": ["Check its expanded name, position, and allowed occurrence counts."],
        "uncertainty": "Earlier errors can affect content-model recovery.",
    },
    "schema-compose": {
        "summary": "Schema composition could not resolve or combine a dependency.",
        "actions": ["Inspect include/import locations and provide the complete schema closure."],
        "uncertainty": "An incomplete schema cannot establish document validity.",
    },
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


def _required_paths(refs: tuple[FileRef | SchemaRef, ...]) -> list[str]:
    paths: list[str] = []
    for ref in refs:
        if isinstance(ref, SchemaRef):
            paths.append(ref.source.path)
            paths.extend(item.path for item in ref.options.namespace_schemas.values())
        else:
            paths.append(ref.path)
    return paths


def _require_present(capture: InputCapture, paths: list[str]) -> None:
    present = {item.path for item in capture.files}
    missing = sorted({path for path in paths if path not in present})
    if missing:
        raise ToolError(
            "not_found",
            "Required file is not present in the workspace inputs",
            details={"path": missing[0], "missing": [str(path) for path in missing]},
        )


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
    async def _capture(
        self, workspace_id: str, refs: tuple[FileRef | SchemaRef, ...] = ()
    ) -> AsyncIterator[InputCapture]:
        async with self.inputs.operation_capture(workspace_id, refs) as capture:
            _require_present(capture, _required_paths(refs))
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
            async with self._capture(request.workspace_id, (request.schema_ref,)) as capture:
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
            async with self._capture(request.workspace_id, (request.schema_ref,)) as capture:
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
            refs: tuple[FileRef | SchemaRef, ...] = (request.document,)
            if request.schema_ref is not None:
                refs = (request.schema_ref, request.document)
            async with self._capture(request.workspace_id, refs) as capture:
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

    async def validate_documents(self, request: ValidateDocumentsRequest) -> ToolEnvelope:
        started, status, capture = time.monotonic(), "ok", None
        try:
            if not request.documents:
                raise ToolError("invalid_argument", "Batch validation needs at least one item")
            if len(request.documents) > self.pool.limits.corpus_max_files:
                raise ToolError("quota_exceeded", "Batch exceeds the operator file budget")
            batch_refs: tuple[FileRef | SchemaRef, ...] = (
                *(entry.schema_ref for entry in request.documents if entry.schema_ref),
                *(entry.document for entry in request.documents),
            )
            async with self.inputs.operation_capture(request.workspace_id, batch_refs) as capture:
                operation = BatchParseOperation(
                    request_id=secrets.token_hex(16),
                    capture=capture_to_wire(capture),
                    items=request.documents,
                )
                reply = await self.pool.run(
                    operation,
                    deadline=time.monotonic() + self.pool.limits.batch_seconds,
                )
                if reply.status == "error":
                    assert reply.fault is not None
                    raise ToolError(
                        reply.fault.code,
                        reply.fault.message,
                        retryable=reply.fault.retryable,
                        details=reply.fault.details,
                    )
                if not isinstance(reply.result, dict) or not isinstance(
                    reply.result.get("items"), list
                ):
                    raise ToolError("ipc_protocol", "Batch returned malformed service data")
                results: list[dict[str, JsonValue]] = []
                summary = {"total": 0, "valid": 0, "invalid": 0, "failed": 0}
                for item_request, item in zip(
                    request.documents,
                    cast(list[dict[str, JsonValue]], reply.result["items"]),
                    strict=True,
                ):
                    summary["total"] += 1
                    document = item.get("document")
                    path = (
                        document.get("path")
                        if isinstance(document, dict)
                        else cast(str, item.get("path", ""))
                    )
                    report_data = item.get("report")
                    report: JsonValue = None
                    if item.get("valid") is False:
                        summary["invalid"] += 1
                    elif item.get("valid") is True:
                        summary["valid"] += 1
                    else:
                        summary["failed"] += 1
                    if (
                        item.get("valid") is False
                        and isinstance(report_data, dict)
                        and self.outputs is not None
                    ):
                        reference = await self.outputs.put_bytes(
                            json.dumps(report_data, separators=(",", ":")).encode(),
                            name="report.json",
                            media_type="application/json",
                            kind="report",
                        )
                        from ..contracts import ArtifactRef

                        report = cast(
                            JsonValue,
                            ArtifactRef(
                                id=reference.id,
                                path=str(reference.path),
                                media_type=reference.media_type,
                                size=reference.size,
                                digest=reference.digest,
                                created_at=reference.created_at,
                                kind=reference.kind,
                            ).model_dump(mode="json"),
                        )
                    valid = item.get("valid")
                    provenance = (
                        None
                        if valid is None
                        else {
                            "generation": capture.generation,
                            "schema_fingerprint": self._fingerprint(
                                capture, item_request.schema_ref
                            ),
                        }
                    )
                    results.append(
                        {
                            "document": {"path": path},
                            "valid": valid,
                            "counts": item.get("counts"),
                            "report": report,
                            "fault": item.get("fault"),
                            "provenance": cast(JsonValue, provenance),
                        }
                    )
                return ToolEnvelope(
                    data={"results": cast(JsonValue, results), "summary": cast(JsonValue, summary)},
                    provenance={
                        "workspace_id": capture.workspace_id,
                        "generation": capture.generation,
                    },
                )
        except BaseException:
            status = "error"
            raise
        finally:
            self._audit("validate_documents", started, status, capture)

    async def query_document(self, request: QueryDocumentRequest) -> ToolEnvelope:
        started, status, capture = time.monotonic(), "ok", None
        try:
            async with self._capture(
                request.workspace_id, (request.schema_ref, request.document)
            ) as capture:
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
                envelope = self._envelope(
                    reply,
                    provenance=self._provenance(capture, request.schema_ref, request.document.path),
                    result_id=result_id,
                    offset=offset,
                )
                if request.result == "subtree":
                    envelope.warnings.append(
                        "Subtree results are serialized from bound data; source formatting and comments are not preserved."
                    )
                return envelope
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
                issue = await self._issue(request)
                if isinstance(issue, dict) and issue.get("code") != request.code:
                    raise ToolError(
                        "invalid_argument",
                        "Supplied code does not match the referenced issue",
                        details={"supplied": request.code, "actual": issue.get("code")},
                    )
                data["issue"] = issue
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
