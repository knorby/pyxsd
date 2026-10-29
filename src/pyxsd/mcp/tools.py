"""Thin SDK registrations; domain behavior remains in Services and workers."""

import base64
import importlib.util
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Literal, cast

from mcp import types
from pydantic import ConfigDict, Field, JsonValue

from .. import __version__
from .contracts import (
    ArtifactRef,
    BatchValidationItem,
    DeleteArtifactRequest,
    ExplainIssueRequest,
    FileRef,
    FindComponentsRequest,
    InlineFile,
    InspectSchemaRequest,
    ListArtifactsRequest,
    ManageWorkspaceRequest,
    Page,
    PageResult,
    PutFilesRequest,
    QueryDocumentRequest,
    ReadArtifactRequest,
    SchemaRef,
    ToolEnvelope,
    ValidateDocumentRequest,
    ValidateDocumentsRequest,
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
    *,
    spill: bool = True,
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
        rendered = await render_result(envelope, services.outputs, spill=spill)
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
        """Report server version, enabled tool groups, source-root, and limits.

        Use this first: the reply names which tools are registered, the
        configured --source-root (if any), the request/response/corpus budgets,
        and whether the pydantic and arrow adapters are available.
        """
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
            """Inspect a schema: overview, compact stubs, full detail, or constraints.

            view "overview" summarizes namespaces, roots, and counts; "compact"
            and "full" describe components (depth controls how deep those views
            go, not the overview); "constraints" shows value/model constraints.
            roots/entry_roots values are component IDs like
            "element:{namespace}name". Incomplete schemas may still be inspected
            so their missing pieces can be diagnosed.
            """
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
            """Search schema components by text, name, kind, namespace, or relation.

            name matches bare local names across namespaces, or Clark notation
            ({namespace}name) for an exact match; unknown kinds are rejected.
            direction/depth walk uses/used_by relations. No matches is a valid
            empty result.
            """
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
            """Validate one XML document, returning paged issue details.

            A well-formed but schema-invalid document is a successful call with
            data.valid == false; malformed XML faults with invalid_xml, missing
            files fault with not_found, and a schema with unresolved
            include/import dependencies faults with schema_incomplete. Omit
            schema_ref to use the document's schema-location hints. Aggregate
            counts come back inline; page issue details with page.next_cursor.
            """
            request = ValidateDocumentRequest(
                workspace_id=workspace_id, document=document, schema_ref=schema_ref, page=page
            )
            return await _invoke(services, lambda: services.exploration.validate_document(request))

        async def validate_documents(
            workspace_id: str, documents: tuple[BatchValidationItem, ...]
        ) -> types.CallToolResult:
            """Validate many XML documents in one call, each against its own schema.

            Pass one or more items; every item may carry its own optional schema_ref
            (omitted falls back to document schema-location hints). The whole batch
            uses one input capture and one worker request. Results keep request
            order, including repeated documents. Each result is independent: a valid
            document reports counts, an invalid document retains a report artifact
            usable with pyxsd_explain_issue, and a per-document failure (missing
            file, malformed XML, broken schema) returns a typed fault without
            affecting other items. Capped by the corpus_max_files budget.
            """
            request = ValidateDocumentsRequest(workspace_id=workspace_id, documents=documents)
            return await _invoke(services, lambda: services.exploration.validate_documents(request))

        async def query_document(
            workspace_id: str,
            document: FileRef,
            schema_ref: SchemaRef,
            xpath: Annotated[
                str,
                Field(description="XPath 1.0 expression; prefixes must be bound in namespaces."),
            ],
            namespaces: dict[str, str] | None = None,
            result: Literal["summary", "subtree"] = "summary",
            page: Page = _PAGE,
        ) -> types.CallToolResult:
            """Run XPath over a document's schema-bound projection.

            Repeated nodes are identified by same-name sibling indexes in
            location.path, with exact line/column locations. result "subtree"
            returns that occurrence's XML — a serialized, reindented
            regeneration of the bound data, not the original source text.
            Requires a complete schema closure.
            """
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
            """Explain a validation issue code with curated, actionable guidance.

            With report and issue_index the explanation is checked against the
            retained validation report: a code that disagrees with the actual
            issue is rejected. Reports remain readable after workspace inputs
            are released.
            """
            request = ExplainIssueRequest(code=code, report=report, issue_index=issue_index)
            return await _invoke(services, lambda: services.exploration.explain_issue(request))

        _register(server, "pyxsd_inspect_schema", inspect_schema, read_only=True)
        _register(server, "pyxsd_find_schema_components", find_schema_components, read_only=True)
        _register(server, "pyxsd_validate_document", validate_document, read_only=True)
        _register(server, "pyxsd_validate_documents", validate_documents, read_only=True)
        _register(server, "pyxsd_query_document", query_document, read_only=True)
        _register(server, "pyxsd_explain_issue", explain_issue, read_only=True)

        async def compare_schemas(
            workspace_id: str,
            old_schema: SchemaRef,
            new_schema: SchemaRef,
            corpus: Annotated[
                tuple[FileRef, ...],
                Field(
                    description=(
                        "Optional documents validated under both schemas; count capped by the "
                        "corpus_max_files budget."
                    )
                ),
            ] = (),
        ) -> types.CallToolResult:
            """Diff two schemas structurally and optionally check a corpus for impact.

            Identity is path-independent: byte-identical schemas at different
            locations produce an empty diff, and a real edit lists the changed
            components with old/new field values. Corpus documents are
            classified valid/regressed/resolved/invalid, with first-issue
            evidence for regressions and resolutions.
            """
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
            """Preview a document transformed under a source schema (optionally retargeted).

            Runs a named pipeline of transforms and returns the projected
            result, including the steps that were applied.
            """
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
            workspace_id: str,
            schema_ref: SchemaRef,
            element: Annotated[
                str,
                Field(
                    description=(
                        "Root element to project: Clark notation ({namespace}name) is exact; a "
                        "bare local name works when it matches exactly one global element "
                        "(ambiguity lists candidates)."
                    )
                ),
            ],
            path: tuple[str, ...] = (),
        ) -> types.CallToolResult:
            """Project JSON Schema (optional Pydantic model) for a declaration.

            element accepts the component IDs returned by schema inspection
            (element:{namespace}name) or a bare local name when unambiguous;
            path steps select nested declarations and use Clark names. Roots
            with mixed content or repeated composites cannot be projected by
            the pydantic integration; scope a path to a projectable leaf.
            """
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
            """Validate JSON data against a projected declaration's JSON Schema.

            Uses the same element/path resolution as inspect_model (Clark
            notation exact, bare local names when unambiguous) and reports
            structured errors for mismatched data.
            """
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
            """Project a document's records to an Arrow table for inspection.

            element/path select the record root (same resolution rules as
            inspect_model); columns map record fields to XPath selectors. The
            preview is bounded and never writes dataset files.
            """
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
                """Export projected records from many documents as retained artifacts.

                Same projection options as preview_records, but writes parquet
                or a dataset layout for the supplied documents and returns
                artifact references for reading via pyxsd_read_artifact.
                """
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
            """Create, list, or release session workspaces.

            Workspaces hold uploaded inputs only; release removes those inputs
            but never deletes retained artifacts. An empty workspace with a
            configured --source-root captures referenced files directly from
            that tree instead.
            """
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
            """Atomically add or replace uploaded files in a workspace.

            Supplied paths are written and omitted files are retained. The
            reply carries the new generation and the full merged manifest. An
            empty files list is a no-op that returns the current manifest.
            """
            request = PutFilesRequest(workspace_id=workspace_id, files=files)

            async def operation() -> ToolEnvelope:
                await services.inputs.put_files(request.workspace_id, request.files)
                capture = await services.inputs.manifest(request.workspace_id)
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
        """List retained artifacts (reports, exports, oversized responses).

        Ordered oldest-first; use envelope page.next_cursor for continuation,
        including when the listing spills. Every entry carries the id, digest,
        media type, size, and local path needed for ranged reads.
        """
        request = ListArtifactsRequest(limit=limit, cursor=cursor)

        async def operation() -> ToolEnvelope:
            after = _decode_after(request.cursor)
            refs, next_after = await services.outputs.list(after=after, limit=request.limit)
            wire = [_artifact(item) for item in refs]
            return ToolEnvelope(
                data={"artifacts": [item.model_dump(mode="json") for item in wire]},
                page=PageResult(
                    returned=len(refs),
                    next_cursor=_encode_after(next_after),
                    truncated=next_after is not None,
                    total=None,
                ),
            )

        return await _invoke(services, operation)

    async def delete_artifact(artifact_id: str) -> types.CallToolResult:
        """Delete one retained artifact by id.

        Destructive: the artifact's bytes are removed. Session inputs and
        other artifacts are untouched.
        """
        request = DeleteArtifactRequest(artifact_id=artifact_id)

        async def operation() -> ToolEnvelope:
            await services.outputs.delete(request.artifact_id)
            return ToolEnvelope(data={"deleted": request.artifact_id})

        return await _invoke(services, operation)

    async def read_artifact(
        artifact_id: str,
        offset: Annotated[int, Field(description="Byte offset into the artifact.", ge=0)] = 0,
        length: Annotated[
            int,
            Field(
                description=(
                    "Bytes to read; the base64 reply must fit the response budget, so request "
                    "a smaller range instead of expecting a spill."
                )
            ),
        ] = 8 * 2**10,
    ) -> types.CallToolResult:
        """Read an exact bounded byte range of a retained artifact as base64.

        Returns the range plus its actual length and the artifact's total size,
        digest, media type, and local path. Length can be smaller than requested
        at end of file. Ranges that would exceed the response budget fault with
        response_limit rather than spilling into another artifact; page large
        reads with offset/length. Artifacts survive workspace release.
        """
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
                    "length": len(content),
                },
                artifacts=[ref],
            )

        # Byte-range reads never spill into a second artifact: an oversized
        # range faults with response_limit and asks for a smaller length.
        return await _invoke(services, operation, spill=False)

    _register(server, "pyxsd_list_artifacts", list_artifacts, read_only=True)
    _register(server, "pyxsd_delete_artifact", delete_artifact, read_only=False, destructive=True)
    _register(server, "pyxsd_read_artifact", read_artifact, read_only=True)
