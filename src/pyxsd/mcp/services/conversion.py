"""Shared data-conversion adapters over public schema/document methods.

Every conversion runs in a clean worker against the cached compiled schema.
Only JSON-safe data, generated schemas, and verdicts cross the process
boundary; documents and typed evidence die with the worker operation.
"""

import secrets
from typing import NoReturn, cast

from pydantic import JsonValue
from pyxsd.data_conversion.contracts import (
    Fidelity,
    FormatName,
    validate_fidelity,
    validate_format,
)
from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng.document import RelaxNGDocument
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.schema import Schema

from ..cache import CompileRequest, SchemaCache, require_complete_schema
from ..captures import InputPolicyError
from ..config import Limits
from ..contracts import (
    DocumentJsonSchemaRequest,
    ExportDataRequest,
    ImportDataRequest,
    ToolEnvelope,
)
from ..encoding import encode_value
from ..errors import ToolError
from ..worker_protocol import (
    DocumentJsonSchemaOperation,
    ExportDataOperation,
    ImportDataOperation,
)
from ..workers import capture_from_wire, capture_to_wire
from .exploration import ExplorationService, _valid_report

CompiledDocumentSchema = Schema | RelaxNGSchema
ConvertedDocument = Document | RelaxNGDocument


def _validated_options(
    format: str,
    fidelity: str,
    *,
    typed: bool = True,
    always_list: bool = False,
    json_schema: bool = False,
) -> tuple[FormatName, Fidelity]:
    """Apply the public conversion option contract before doing any work."""
    try:
        requested_format = validate_format(format)
        requested_fidelity = validate_fidelity(fidelity)
    except ValueError as exc:
        raise ToolError("invalid_argument", str(exc)) from exc
    if requested_format != "pyxsd":
        option = "JSON Schema" if json_schema else "data-export"
        if not typed:
            raise ToolError("invalid_argument", f"typed is a native-only {option} option")
        if always_list:
            raise ToolError("invalid_argument", f"always_list is a native-only {option} option")
    return requested_format, requested_fidelity


def _conversion_fault(exc: PyXSDError, *, format: str, limits: Limits) -> NoReturn:
    """Expose a public conversion error as one typed, bounded fault."""
    maximum = limits.message_bytes
    message = str(exc) if maximum is None else str(exc)[:maximum]
    raise ToolError("invalid_data", message, details={"format": format}) from exc


def _schema_fault(exc: BaseException, limits: Limits) -> NoReturn:
    """Classify a schema-acquisition failure without inventing a verdict."""
    from ..worker_main import _operational_fault

    fault = _operational_fault(exc, "invalid_schema", limits.message_bytes)
    raise ToolError(
        fault.code,
        fault.message,
        retryable=fault.retryable,
        details=fault.details,
    ) from exc


def _parse_document(
    operation: ExportDataOperation, schema: CompiledDocumentSchema, cache: SchemaCache
) -> ConvertedDocument:
    """Parse the exported document through its schema's ordinary public parse."""
    path = operation.document.path
    if path not in {item.path for item in operation.capture.files}:
        raise ToolError(
            "not_found",
            "Required input is missing: " + path,
            details={"role": "document", "path": path},
        )
    try:
        return schema.parse("/workspace/" + path)
    except (InputPolicyError, PyXSDError) as exc:
        from ..worker_main import _operational_fault

        fault = _operational_fault(exc, "invalid_xml", cache.limits.message_bytes)
        raise ToolError(
            fault.code,
            fault.message,
            retryable=fault.retryable,
            details=fault.details,
        ) from exc


def _export_data(
    document: ConvertedDocument, operation: ExportDataOperation, cache: SchemaCache
) -> JsonValue:
    """Call the public export method and type its conversion failures."""
    try:
        return document.to_data(
            format=operation.format,
            fidelity=operation.fidelity,
            typed=operation.typed,
            always_list=operation.always_list,
        )
    except PyXSDError as exc:
        _conversion_fault(exc, format=operation.format, limits=cache.limits)


def _import_document(
    schema: CompiledDocumentSchema, operation: ImportDataOperation, cache: SchemaCache
) -> ConvertedDocument:
    """Call the public import method; malformed tags/shapes are faults."""
    try:
        return schema.from_data(
            operation.data,
            format=operation.format,
            element=operation.element,
            namespaces=operation.namespaces,
            fidelity=operation.fidelity,
        )
    except PyXSDError as exc:
        _conversion_fault(exc, format=operation.format, limits=cache.limits)


def _page_report(
    document: ConvertedDocument, offset: int, limit: int | None
) -> dict[str, JsonValue]:
    """Page an ordinary validation report without retaining the document."""
    data = cast(dict[str, JsonValue], document.report.to_dict())
    issues = cast(list[JsonValue], data["issues"])
    end = None if limit is None else offset + limit
    page_issues = issues[offset:end]
    data["issues"] = page_issues
    counts = cast(dict[str, int], data["counts"])
    total = counts["error"] + counts["warning"]
    return {
        "report": data,
        "page": {
            "returned": len(page_issues),
            "truncated": offset + len(page_issues) < total,
            "total": total,
        },
        "root_present": document.root is not None,
    }


def _compile_request(
    operation: ExportDataOperation | ImportDataOperation | DocumentJsonSchemaOperation,
    processor_version: str,
) -> CompileRequest:
    return CompileRequest(
        capture=capture_from_wire(operation.capture),
        schema_ref=operation.schema_ref,
        hints=(),
        processor_version=processor_version,
    )


def execute_export_data(
    operation: ExportDataOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    """Export one document under the requested public data contract."""
    request = _compile_request(operation, processor_version)
    try:
        with cache.acquire(request) as schema:
            require_complete_schema(schema)
            document = _parse_document(operation, schema, cache)
            data = _export_data(document, operation, cache)
    except ToolError:
        raise
    except (InputPolicyError, PyXSDError) as exc:
        _schema_fault(exc, cache.limits)
    return {
        "format": operation.format,
        "fidelity": operation.fidelity,
        "data": data,
    }


def execute_import_data(
    operation: ImportDataOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    """Import JSON-compatible data and return its document report, not the document."""
    request = _compile_request(operation, processor_version)
    try:
        with cache.acquire(request) as schema:
            require_complete_schema(schema)
            document = _import_document(schema, operation, cache)
            result = _page_report(document, operation.issue_offset, operation.issue_limit)
    except ToolError:
        raise
    except (InputPolicyError, PyXSDError) as exc:
        _schema_fault(exc, cache.limits)
    return {
        "format": operation.format,
        "fidelity": operation.fidelity,
        **result,
    }


def execute_document_json_schema(
    operation: DocumentJsonSchemaOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    """Describe one schema's public data contract as Draft 2020-12 JSON Schema."""
    request = _compile_request(operation, processor_version)
    try:
        with cache.acquire(request) as schema:
            require_complete_schema(schema)
            try:
                generated = schema.json_schema(
                    format=operation.format,
                    element=operation.element,
                    fidelity=operation.fidelity,
                    typed=operation.typed,
                    always_list=operation.always_list,
                )
            except PyXSDError as exc:
                _conversion_fault(exc, format=operation.format, limits=cache.limits)
    except ToolError:
        raise
    except (InputPolicyError, PyXSDError) as exc:
        _schema_fault(exc, cache.limits)
    return {
        "format": operation.format,
        "fidelity": operation.fidelity,
        "json_schema": encode_value(generated),
    }


class ConversionService(ExplorationService):
    """Workspace captures and worker leases for shared conversion operations."""

    async def export_data(self, request: ExportDataRequest) -> ToolEnvelope:
        format, fidelity = _validated_options(
            request.format,
            request.fidelity,
            typed=request.typed,
            always_list=request.always_list,
        )
        refs = (request.schema_ref, request.document)
        async with self._capture(request.workspace_id, refs) as capture:
            result_id = self._result_id(request, capture)
            reply = await self.pool.run(
                ExportDataOperation(
                    request_id=secrets.token_hex(16),
                    capture=capture_to_wire(capture),
                    schema_ref=request.schema_ref,
                    document=request.document,
                    format=format,
                    fidelity=fidelity,
                    typed=request.typed,
                    always_list=request.always_list,
                ),
                deadline=self._deadline(self.pool.limits.compile_query_seconds),
            )
            return self._envelope(
                reply,
                provenance=self._provenance(capture, request.schema_ref, request.document.path),
                result_id=result_id,
            )

    async def import_data(self, request: ImportDataRequest) -> ToolEnvelope:
        format, fidelity = _validated_options(request.format, request.fidelity)
        async with self._capture(request.workspace_id, (request.schema_ref,)) as capture:
            result_id = self._result_id(request, capture)
            offset, limit = self._position(request.page, result_id)
            reply = await self.pool.run(
                ImportDataOperation(
                    request_id=secrets.token_hex(16),
                    capture=capture_to_wire(capture),
                    schema_ref=request.schema_ref,
                    data=request.data,
                    format=format,
                    element=request.element,
                    namespaces=request.namespaces,
                    fidelity=fidelity,
                    issue_offset=offset,
                    issue_limit=limit,
                ),
                deadline=self._deadline(self.pool.limits.compile_query_seconds),
            )
            if reply.status == "ok" and isinstance(reply.result, dict):
                report = reply.result.get("report")
                result = {
                    key: value for key, value in reply.result.items() if key != "root_present"
                }
                result["valid"] = _valid_report(report)
                result["binding_available"] = reply.result.get("root_present")
                reply = reply.model_copy(update={"result": result})
            envelope = self._envelope(
                reply,
                provenance=self._provenance(
                    capture, request.schema_ref, request.schema_ref.source.path
                ),
                result_id=result_id,
                offset=offset,
            )
            data = cast(dict[str, JsonValue], envelope.data)
            if data.get("valid") is True:
                envelope = envelope.model_copy(
                    update={"data": {**data, "report": None}, "page": None}
                )
            return envelope

    async def document_json_schema(self, request: DocumentJsonSchemaRequest) -> ToolEnvelope:
        format, fidelity = _validated_options(
            request.format,
            request.fidelity,
            typed=request.typed,
            always_list=request.always_list,
            json_schema=True,
        )
        async with self._capture(request.workspace_id, (request.schema_ref,)) as capture:
            result_id = self._result_id(request, capture)
            reply = await self.pool.run(
                DocumentJsonSchemaOperation(
                    request_id=secrets.token_hex(16),
                    capture=capture_to_wire(capture),
                    schema_ref=request.schema_ref,
                    format=format,
                    element=request.element,
                    fidelity=fidelity,
                    typed=request.typed,
                    always_list=request.always_list,
                ),
                deadline=self._deadline(self.pool.limits.compile_query_seconds),
            )
            return self._envelope(
                reply,
                provenance=self._provenance(
                    capture, request.schema_ref, request.schema_ref.source.path
                ),
                result_id=result_id,
            )
