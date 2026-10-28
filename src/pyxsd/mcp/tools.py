"""Thin SDK registrations; domain behavior remains in Services and workers."""

import base64
import importlib.util
import json
import sqlite3
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal, cast

from mcp import types
from pydantic import ConfigDict, JsonValue

from .. import __version__
from .contracts import (
    ArtifactRef,
    ExplainIssueRequest,
    FileRef,
    FindComponentsRequest,
    InlineFile,
    InspectSchemaRequest,
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
from .previews import PreviewTransformRequest
from .publication import CommitTransformRequest
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
        "source_aliases": cast(list[JsonValue], sorted(settings.source_roots)),
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
    workspace_id: str | None,
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
        if workspace_id is None:
            return _plain(envelope)
        rendered = await render_result(
            services.current_principal(), workspace_id, envelope, services.artifacts
        )
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
            return await _invoke(
                services,
                workspace_id,
                lambda: services.exploration.inspect_schema(services.current_principal(), request),
            )

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
                services,
                workspace_id,
                lambda: services.exploration.find_schema_components(
                    services.current_principal(), request
                ),
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
            return await _invoke(
                services,
                workspace_id,
                lambda: services.exploration.validate_document(
                    services.current_principal(), request
                ),
            )

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
            return await _invoke(
                services,
                workspace_id,
                lambda: services.exploration.query_document(services.current_principal(), request),
            )

        async def explain_issue(
            code: str, report: ArtifactRef | None = None, issue_index: int | None = None
        ) -> types.CallToolResult:
            request = ExplainIssueRequest(code=code, report=report, issue_index=issue_index)
            return await _invoke(
                services,
                None,
                lambda: services.exploration.explain_issue(services.current_principal(), request),
            )

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
                result = await services.comparison.compare(services.current_principal(), request)
                return ToolEnvelope(data=result)

            return await _invoke(services, workspace_id, operation)

        _register(server, "pyxsd_compare_schemas", compare_schemas, read_only=True)

    if "execution" in services.settings.enabled_groups:

        async def commit_transform(
            preview_id: str,
            expected_candidate_digest: str,
            destination: str,
            idempotency_key: str,
            mode: Literal["new", "replace"] = "new",
            expected_destination_digest: str | None = None,
            allow_invalid: bool = False,
        ) -> types.CallToolResult:
            request = CommitTransformRequest(
                preview_id=preview_id,
                expected_candidate_digest=expected_candidate_digest,
                destination=destination,
                idempotency_key=idempotency_key,
                mode=mode,
                expected_destination_digest=expected_destination_digest,
                allow_invalid=allow_invalid,
            )

            async def operation() -> ToolEnvelope:
                receipt = await services.publication.commit(services.current_principal(), request)
                return ToolEnvelope(data=receipt.model_dump(mode="json"))

            return await _invoke(services, None, operation)

        _register(
            server, "pyxsd_commit_transform", commit_transform, read_only=False, destructive=True
        )

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
                record = await services.previews.preview(services.current_principal(), request)
                return ToolEnvelope(data=record.model_dump(mode="json"))

            return await _invoke(services, workspace_id, operation)

        _register(server, "pyxsd_preview_transform", preview_transform, read_only=False)

    if services.settings.pydantic:

        async def inspect_model(
            workspace_id: str, schema_ref: SchemaRef, element: str, path: tuple[str, ...] = ()
        ) -> types.CallToolResult:
            request = InspectModelRequest(
                workspace_id=workspace_id, schema_ref=schema_ref, element=element, path=path
            )
            return await _invoke(
                services,
                workspace_id,
                lambda: services.models.inspect_model(services.current_principal(), request),
            )

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
            return await _invoke(
                services,
                workspace_id,
                lambda: services.models.validate_data(services.current_principal(), request),
            )

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
            return await _invoke(
                services,
                workspace_id,
                lambda: services.records.preview_records(services.current_principal(), request),
            )

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
                    result = await services.records.export_records(
                        services.current_principal(), request
                    )
                    return ToolEnvelope(data=result)

                return await _invoke(services, workspace_id, operation)

            _register(server, "pyxsd_export_records", export_records, read_only=False)

    if workspaces:

        async def manage_workspace(
            action: Literal["create", "list", "release"],
            workspace_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> types.CallToolResult:
            request = ManageWorkspaceRequest(
                action=action, workspace_id=workspace_id, idempotency_key=idempotency_key
            )

            async def operation() -> ToolEnvelope:
                if request.action == "create":
                    workspace = await services.workspaces.create(
                        services.current_principal(), idempotency_key=request.idempotency_key
                    )
                    return ToolEnvelope(data=workspace.model_dump(mode="json"))
                if request.action == "list":
                    items = await services.workspaces.list(services.current_principal())
                    return ToolEnvelope(
                        data={"workspaces": [item.model_dump(mode="json") for item in items]}
                    )
                if request.workspace_id is None:
                    raise ToolError("invalid_argument", "Release requires a workspace ID")
                await services.workspaces.release(
                    services.current_principal(), request.workspace_id
                )
                return ToolEnvelope(data={"released": request.workspace_id})

            return await _invoke(services, workspace_id, operation)

        async def put_files(
            workspace_id: str,
            files: tuple[InlineFile, ...],
            expected_revision: str | None = None,
        ) -> types.CallToolResult:
            request = PutFilesRequest(
                workspace_id=workspace_id, files=files, expected_revision=expected_revision
            )

            async def operation() -> ToolEnvelope:
                snapshot = await services.snapshots.put_files(
                    services.current_principal(),
                    request.workspace_id,
                    request.files,
                    expected_revision=request.expected_revision,
                )
                return ToolEnvelope(
                    data={
                        "workspace_id": snapshot.workspace_id,
                        "revision": snapshot.revision,
                        "files": [
                            {"path": item.path, "digest": item.digest, "size": item.size}
                            for item in snapshot.files
                        ],
                    }
                )

            return await _invoke(services, workspace_id, operation)

        async def read_artifact(
            artifact_id: str, offset: int = 0, length: int = 8 * 2**10
        ) -> types.CallToolResult:
            request = ReadArtifactRequest(artifact_id=artifact_id, offset=offset, length=length)

            async def operation() -> ToolEnvelope:
                async with services.artifacts.lease(
                    services.current_principal(), request.artifact_id
                ) as reader:
                    content = await reader.read(offset=request.offset, length=request.length)
                    ref = reader.ref
                return ToolEnvelope(
                    data={
                        "encoding": "base64",
                        "data": base64.b64encode(content).decode("ascii"),
                        "media_type": ref.media_type,
                        "digest": ref.digest,
                        "size": ref.size,
                        "offset": request.offset,
                    },
                    artifacts=[ref],
                )

            try:
                workspace = await _artifact_workspace(services, request.artifact_id)
            except ToolError as exc:
                return _plain(
                    ToolEnvelope(
                        status="error",
                        data={"fault": exc.fault.model_dump(mode="json")},
                        provenance=exc.fault.details,
                    )
                )
            return await _invoke(services, workspace, operation)

        _register(
            server, "pyxsd_manage_workspace", manage_workspace, read_only=False, destructive=True
        )
        _register(server, "pyxsd_put_files", put_files, read_only=False)
        _register(server, "pyxsd_read_artifact", read_artifact, read_only=True)


async def _artifact_workspace(services: "Services", artifact_id: str) -> str:
    def lookup(db: sqlite3.Connection) -> str:
        row = db.execute("SELECT workspace FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise ToolError("not_found", "Artifact not found")
        return row["workspace"]

    return await services.state.run(lookup)
