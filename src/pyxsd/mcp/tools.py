"""Thin SDK registrations; domain behavior remains in Services and workers."""

import base64
import importlib.util
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal, cast

from mcp import types
from pydantic import ConfigDict, JsonValue

from .. import __version__
from .contracts import (
    ArtifactRef,
    DeleteArtifactRequest,
    ExplainIssueRequest,
    FileRef,
    FindComponentsRequest,
    InlineFile,
    InspectSchemaRequest,
    ListArtifactsRequest,
    ManageWorkspaceRequest,
    Page,
    PutFilesRequest,
    QueryDocumentRequest,
    ReadArtifactRequest,
    SchemaRef,
    ToolEnvelope,
    ValidateDocumentRequest,
)
from .errors import ToolError
from .output_store import OutputRef
from .previews import PreviewTransformRequest
from .responses import render_result
from .services.models import InspectModelRequest, ValidateDataRequest
from .services.records import ExportRecordsRequest, PreviewRecordsRequest
from .transforms import TransformSpec
from .worker_protocol import FieldSourceSpec

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from .server import Services

_PAGE = Page()


def capability_data(services: "Services") -> dict[str, JsonValue]:
    settings = services.settings
    return {
        "version": __version__,
        "transport": "stdio",
        "execution_backend": settings.execution_backend,
        "enabled_groups": cast(list[JsonValue], sorted(settings.enabled_groups)),
        "source_root": None if settings.source_root is None else str(settings.source_root),
        "limits": settings.limits.model_dump(mode="json"),
        "adapters": {
            "pydantic": {
                "enabled": settings.pydantic,
                "available": importlib.util.find_spec("pydantic") is not None,
            },
            "arrow": {
                "mode": settings.arrow,
                "available": services.arrow_available,
            },
        },
    }


def _artifact(reference: OutputRef) -> ArtifactRef:
    return ArtifactRef(
        id=reference.id,
        path=str(reference.path),
        media_type=reference.media_type,
        size=reference.size,
        digest=reference.digest,
        created_at=reference.created_at,
        kind=reference.kind,
    )


def _plain(envelope: ToolEnvelope) -> types.CallToolResult:
    data = envelope.model_dump(mode="json")
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=data,
        is_error=envelope.status == "error",
    )


async def _invoke(
    services: "Services",
    operation: Callable[[], Awaitable[ToolEnvelope]],
) -> types.CallToolResult:
    try:
        try:
            envelope = await operation()
        except ToolError as exc:
            envelope = ToolEnvelope(
                status="error",
                data={"fault": exc.fault.model_dump(mode="json")},
                provenance=exc.fault.details,
            )
        rendered = await render_result(envelope, services.outputs)
        return types.CallToolResult.model_validate(
            {**rendered, "isError": envelope.status == "error"}
        )
    except ToolError as exc:
        return _plain(
            ToolEnvelope(
                status="error",
                data={"fault": exc.fault.model_dump(mode="json")},
                provenance=exc.fault.details,
            )
        )


def _encode_after(after: tuple[float, str] | None) -> str | None:
    if after is None:
        return None
    return (
        base64.urlsafe_b64encode(json.dumps([after[0], after[1]], separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode("ascii")
    )


def _decode_after(cursor: str | None) -> tuple[float, str] | None:
    if cursor is None:
        return None
    try:
        if not 1 <= len(cursor) <= 128:
            raise ValueError("length")
        data = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(data, list)
            or len(data) != 2
            or type(data[0]) not in (int, float)
            or not isinstance(data[1], str)
        ):
            raise ValueError("payload")
        return float(data[0]), data[1]
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ToolError("invalid_cursor", "Artifact cursor is invalid") from exc


def _register(
    server: "MCPServer",
    name: str,
    function: Callable[..., Awaitable[types.CallToolResult]],
    *,
    read_only: bool,
    destructive: bool = False,
) -> None:
    annotations = types.ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=not destructive,
        open_world_hint=False,
    )
    server.add_tool(function, name=name, annotations=annotations, structured_output=False)
    tool = server._tool_manager._tools[name]
    tool.parameters["additionalProperties"] = False
    generated = tool.fn_metadata.arg_model

    strict_arguments = type(
        "StrictArguments",
        (generated,),
        {"model_config": ConfigDict(extra="forbid", arbitrary_types_allowed=True)},
    )
    tool.fn_metadata.arg_model = strict_arguments
    # CallToolResult carries the actual error flag. Advertise the envelope held
    # inside structuredContent, not CallToolResult's own transport schema.
    tool.fn_metadata.output_schema = ToolEnvelope.model_json_schema()


def register_tools(server: "MCPServer", services: "Services") -> None:
    exploration = "exploration" in services.settings.enabled_groups
    workspaces = "workspaces" in services.settings.enabled_groups

    async def capabilities() -> types.CallToolResult:
        return _plain(ToolEnvelope(data=capability_data(services)))

    _register(server, "pyxsd_capabilities", capabilities, read_only=True)

    if exploration:

        async def inspect_schema(
            workspace_id: str,
            schema_ref: SchemaRef,
            component_ids: tuple[str, ...] = (),
            view: Literal["overview", "compact", "full", "constraints"] = "overview",
            depth: int | None = 1,
            page: Page = _PAGE,
        ) -> types.CallToolResult:
            request = InspectSchemaRequest(
                workspace_id=workspace_id,
                schema_ref=schema_ref,
                component_ids=component_ids,
                view=view,
                depth=depth,
                page=page,
            )
            return await _invoke(services, lambda: services.exploration.inspect_schema(request))

        async def find_schema_components(
            workspace_id: str,
            schema_ref: SchemaRef,
            text: str | None = None,
            kind: str | None = None,
            name: str | None = None,
            namespace: str | None = None,
            component_id: str | None = None,
            direction: Literal["uses", "used_by"] = "uses",
            depth: int = 1,
            page: Page = _PAGE,
        ) -> types.CallToolResult:
            request = FindComponentsRequest(
                workspace_id=workspace_id,
                schema_ref=schema_ref,
                text=text,
                kind=kind,
                name=name,
                namespace=namespace,
                component_id=component_id,
                direction=direction,
                depth=depth,
                page=page,
            )
            return await _invoke(
                services, lambda: services.exploration.find_schema_components(request)
            )

        async def validate_document(
            workspace_id: str,
            document: FileRef,
            schema_ref: SchemaRef | None = None,
            page: Page = _PAGE,
        ) -> types.CallToolResult:
            request = ValidateDocumentRequest(
                workspace_id=workspace_id, document=document, schema_ref=schema_ref, page=page
            )
            return await _invoke(services, lambda: services.exploration.validate_document(request))

        async def query_document(
            workspace_id: str,
            document: FileRef,
            schema_ref: SchemaRef,
            xpath: str,
            namespaces: dict[str, str] | None = None,
            result: Literal["summary", "subtree"] = "summary",
            page: Page = _PAGE,
        ) -> types.CallToolResult:
            request = QueryDocumentRequest(
                workspace_id=workspace_id,
                document=document,
                schema_ref=schema_ref,
                xpath=xpath,
                namespaces=namespaces or {},
                result=result,
                page=page,
            )
            return await _invoke(services, lambda: services.exploration.query_document(request))

        async def explain_issue(
            code: str, report: ArtifactRef | None = None, issue_index: int | None = None
        ) -> types.CallToolResult:
            request = ExplainIssueRequest(code=code, report=report, issue_index=issue_index)
            return await _invoke(services, lambda: services.exploration.explain_issue(request))

        _register(server, "pyxsd_inspect_schema", inspect_schema, read_only=True)
        _register(server, "pyxsd_find_schema_components", find_schema_components, read_only=True)
        _register(server, "pyxsd_validate_document", validate_document, read_only=True)
        _register(server, "pyxsd_query_document", query_document, read_only=True)
        _register(server, "pyxsd_explain_issue", explain_issue, read_only=True)

        async def compare_schemas(
            workspace_id: str,
            old_schema: SchemaRef,
            new_schema: SchemaRef,
            corpus: tuple[FileRef, ...] = (),
        ) -> types.CallToolResult:
            from .services.comparison import CompareSchemasRequest

            request = CompareSchemasRequest(
                workspace_id=workspace_id,
                old_schema=old_schema,
                new_schema=new_schema,
                corpus=corpus,
            )

            async def operation() -> ToolEnvelope:
                return ToolEnvelope(data=await services.comparison.compare(request))

            return await _invoke(services, operation)

        _register(server, "pyxsd_compare_schemas", compare_schemas, read_only=True)

    if "execution" in services.settings.enabled_groups:

        async def preview_transform(
            workspace_id: str,
            document: FileRef,
            source_schema: SchemaRef,
            transform: TransformSpec,
            target_schema: SchemaRef | None = None,
        ) -> types.CallToolResult:
            request = PreviewTransformRequest(
                workspace_id=workspace_id,
                document=document,
                source_schema=source_schema,
                target_schema=target_schema,
                transform=transform,
            )

            async def operation() -> ToolEnvelope:
                record = await services.previews.preview(workspace_id, request)
                return ToolEnvelope(data=record.model_dump(mode="json"))

            return await _invoke(services, operation)

        _register(server, "pyxsd_preview_transform", preview_transform, read_only=False)

    if services.settings.pydantic:

        async def inspect_model(
            workspace_id: str, schema_ref: SchemaRef, element: str, path: tuple[str, ...] = ()
        ) -> types.CallToolResult:
            request = InspectModelRequest(
                workspace_id=workspace_id, schema_ref=schema_ref, element=element, path=path
            )
            return await _invoke(services, lambda: services.models.inspect_model(request))

        async def validate_data(
            workspace_id: str,
            schema_ref: SchemaRef,
            element: str,
            data: JsonValue,
            path: tuple[str, ...] = (),
        ) -> types.CallToolResult:
            request = ValidateDataRequest(
                workspace_id=workspace_id,
                schema_ref=schema_ref,
                element=element,
                path=path,
                data=data,
            )
            return await _invoke(services, lambda: services.models.validate_data(request))

        _register(server, "pyxsd_inspect_model", inspect_model, read_only=True)
        _register(server, "pyxsd_validate_data", validate_data, read_only=True)

    if services.arrow_available:

        async def preview_records(
            workspace_id: str,
            schema_ref: SchemaRef,
            document: FileRef,
            element: str,
            path: tuple[str, ...] = (),
            selector: str | None = None,
            namespaces: dict[str, str] | None = None,
            columns: dict[str, FieldSourceSpec] | None = None,
            exact_count: bool = True,
        ) -> types.CallToolResult:
            request = PreviewRecordsRequest(
                workspace_id=workspace_id,
                schema_ref=schema_ref,
                document=document,
                element=element,
                path=path,
                selector=selector,
                namespaces=namespaces or {},
                columns=columns,
                exact_count=exact_count,
            )
            return await _invoke(services, lambda: services.records.preview_records(request))

        _register(server, "pyxsd_preview_records", preview_records, read_only=True)

        if "execution" in services.settings.enabled_groups:

            async def export_records(
                workspace_id: str,
                schema_ref: SchemaRef,
                documents: tuple[FileRef, ...],
                element: str,
                path: tuple[str, ...] = (),
                selector: str | None = None,
                namespaces: dict[str, str] | None = None,
                columns: dict[str, FieldSourceSpec] | None = None,
                format: Literal["parquet", "dataset"] = "parquet",
            ) -> types.CallToolResult:
                request = ExportRecordsRequest(
                    workspace_id=workspace_id,
                    schema_ref=schema_ref,
                    documents=documents,
                    element=element,
                    path=path,
                    selector=selector,
                    namespaces=namespaces or {},
                    columns=columns,
                    format=format,
                )

                async def operation() -> ToolEnvelope:
                    return ToolEnvelope(data=await services.records.export_records(request))

                return await _invoke(services, operation)

            _register(server, "pyxsd_export_records", export_records, read_only=False)

    if workspaces:

        async def manage_workspace(
            action: Literal["create", "list", "release"],
            workspace_id: str | None = None,
        ) -> types.CallToolResult:
            request = ManageWorkspaceRequest(action=action, workspace_id=workspace_id)

            async def operation() -> ToolEnvelope:
                if request.action == "create":
                    workspace = await services.inputs.create()
                    return ToolEnvelope(data=workspace.__dict__)
                if request.action == "list":
                    items = await services.inputs.list()
                    return ToolEnvelope(data={"workspaces": [item.__dict__ for item in items]})
                if request.workspace_id is None:
                    raise ToolError("invalid_argument", "Release requires a workspace ID")
                await services.inputs.release(request.workspace_id)
                return ToolEnvelope(data={"released": request.workspace_id})

            return await _invoke(services, operation)

        async def put_files(
            workspace_id: str,
            files: tuple[InlineFile, ...],
        ) -> types.CallToolResult:
            request = PutFilesRequest(workspace_id=workspace_id, files=files)

            async def operation() -> ToolEnvelope:
                await services.inputs.put_files(request.workspace_id, request.files)
                async with services.inputs.capture(request.workspace_id) as capture:
                    return ToolEnvelope(
                        data={
                            "workspace_id": capture.workspace_id,
                            "generation": capture.generation,
                            "files": [
                                {"path": item.path, "digest": item.digest, "size": item.size}
                                for item in capture.files
                            ],
                        }
                    )

            return await _invoke(services, operation)

        _register(
            server, "pyxsd_manage_workspace", manage_workspace, read_only=False, destructive=True
        )
        _register(server, "pyxsd_put_files", put_files, read_only=False)

    async def list_artifacts(limit: int = 50, cursor: str | None = None) -> types.CallToolResult:
        request = ListArtifactsRequest(limit=limit, cursor=cursor)

        async def operation() -> ToolEnvelope:
            after = _decode_after(request.cursor)
            refs, next_after = await services.outputs.list(after=after, limit=request.limit)
            wire = [_artifact(item) for item in refs]
            return ToolEnvelope(
                data={
                    "artifacts": [item.model_dump(mode="json") for item in wire],
                    "next_cursor": _encode_after(next_after),
                }
            )

        return await _invoke(services, operation)

    async def delete_artifact(artifact_id: str) -> types.CallToolResult:
        request = DeleteArtifactRequest(artifact_id=artifact_id)

        async def operation() -> ToolEnvelope:
            await services.outputs.delete(request.artifact_id)
            return ToolEnvelope(data={"deleted": request.artifact_id})

        return await _invoke(services, operation)

    async def read_artifact(
        artifact_id: str, offset: int = 0, length: int = 8 * 2**10
    ) -> types.CallToolResult:
        request = ReadArtifactRequest(artifact_id=artifact_id, offset=offset, length=length)

        async def operation() -> ToolEnvelope:
            reference, content = await services.outputs.read_with_ref(
                request.artifact_id, offset=request.offset, length=request.length
            )
            ref = _artifact(reference)
            return ToolEnvelope(
                data={
                    "encoding": "base64",
                    "data": base64.b64encode(content).decode("ascii"),
                    "path": ref.path,
                    "media_type": ref.media_type,
                    "digest": ref.digest,
                    "size": ref.size,
                    "offset": request.offset,
                },
                artifacts=[ref],
            )

        return await _invoke(services, operation)

    _register(server, "pyxsd_list_artifacts", list_artifacts, read_only=True)
    _register(server, "pyxsd_delete_artifact", delete_artifact, read_only=False, destructive=True)
    _register(server, "pyxsd_read_artifact", read_artifact, read_only=True)
