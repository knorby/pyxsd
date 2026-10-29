"""Static protocol models, independent of schema-generated domain models."""

from typing import Annotated, Literal

try:
    from pydantic import BaseModel, ConfigDict, Field, JsonValue
except ModuleNotFoundError as exc:
    if exc.name == "pydantic":
        raise ImportError("Install pyxsd[mcp] to use the MCP integration") from exc
    raise

Nonempty = Annotated[str, Field(min_length=1)]
PositiveBudget = Annotated[int, Field(gt=0)]


class WireModel(BaseModel):
    """Reject coercion and unknown fields at the protocol boundary."""

    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, validate_default=True, allow_inf_nan=False
    )


class FileRef(WireModel):
    path: Annotated[
        Nonempty, Field(description="Workspace-relative or capture-relative file path.")
    ]


class InlineFile(WireModel):
    path: Annotated[
        Nonempty, Field(description="Workspace-relative destination path for this upload.")
    ]
    text: Annotated[str, Field(description="Full UTF-8 text of the file to store.")]


class CompileOptions(WireModel):
    binding: Annotated[
        Literal["lax", "strict"], Field(description="Attribute/element binding strictness.")
    ] = "lax"
    namespaces: Annotated[
        Literal["strict", "legacy"],
        Field(description="Namespace handling mode; strict requires qualified names."),
    ] = "strict"
    xsd_version: Annotated[Literal["1.0", "1.1"], Field(description="XSD spec version target.")] = (
        "1.1"
    )
    namespace_schemas: Annotated[
        dict[str, FileRef],
        Field(description="Extra schemas registered for specific namespaces (hint resolution)."),
    ] = Field(default_factory=dict)


class SchemaRef(WireModel):
    source: Annotated[FileRef, Field(description="Entry schema file this reference resolves.")]
    options: CompileOptions = Field(default_factory=CompileOptions)


class ArtifactRef(WireModel):
    id: Nonempty
    path: Nonempty
    media_type: Nonempty
    size: Annotated[int, Field(ge=0)]
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    created_at: float
    kind: Nonempty


class Page(WireModel):
    cursor: Annotated[
        Nonempty | None, Field(description="Opaque cursor from page.next_cursor.")
    ] = None
    limit: Annotated[
        PositiveBudget,
        Field(description="Maximum items to return; must fit the page_items budget."),
    ] = 50


class PageResult(WireModel):
    returned: Annotated[int, Field(ge=0)]
    next_cursor: Nonempty | None = None
    truncated: bool = False
    total: Annotated[int, Field(ge=0)] | None = None


class ToolFault(WireModel):
    code: Nonempty
    message: Nonempty
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ToolEnvelope(WireModel):
    status: Literal["ok", "error"] = "ok"
    data: JsonValue = None
    warnings: list[str] = Field(default_factory=list)
    provenance: dict[str, JsonValue] = Field(default_factory=dict)
    page: PageResult | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)


class InspectSchemaRequest(WireModel):
    workspace_id: Nonempty
    schema_ref: SchemaRef
    component_ids: tuple[Nonempty, ...] = ()
    view: Literal["overview", "compact", "full", "constraints"] = "overview"
    depth: Annotated[int, Field(ge=0)] | None = 1
    page: Page = Field(default_factory=Page)


class FindComponentsRequest(WireModel):
    workspace_id: Nonempty
    schema_ref: SchemaRef
    text: str | None = None
    kind: Nonempty | None = None
    name: Nonempty | None = None
    namespace: str | None = None
    component_id: Nonempty | None = None
    direction: Literal["uses", "used_by"] = "uses"
    depth: Annotated[int, Field(ge=0)] = 1
    page: Page = Field(default_factory=Page)


class ValidateDocumentRequest(WireModel):
    workspace_id: Nonempty
    document: FileRef
    schema_ref: SchemaRef | None = None
    page: Page = Field(default_factory=Page)


class BatchValidationItem(WireModel):
    """One document in a batch validation request, with its own schema."""

    document: FileRef = Field(description="Workspace-relative path of the XML document.")
    schema_ref: SchemaRef | None = Field(
        default=None,
        description=(
            "Schema for this document; omitted falls back to document schema-location hints."
        ),
    )


class ValidateDocumentsRequest(WireModel):
    workspace_id: Nonempty
    documents: tuple[BatchValidationItem, ...] = Field(
        description=(
            "One or more items, each with its own optional schema. Results keep request "
            "order; reports are retained only for invalid documents."
        )
    )


class QueryDocumentRequest(WireModel):
    workspace_id: Nonempty
    document: FileRef
    schema_ref: SchemaRef
    xpath: Nonempty
    namespaces: dict[str, str] = Field(default_factory=dict)
    result: Literal["summary", "subtree"] = "summary"
    page: Page = Field(default_factory=Page)


class ExplainIssueRequest(WireModel):
    code: Nonempty
    report: ArtifactRef | None = None
    issue_index: Annotated[int, Field(ge=0)] | None = None


class ManageWorkspaceRequest(WireModel):
    action: Literal["create", "list", "release"]
    workspace_id: Nonempty | None = None


class PutFilesRequest(WireModel):
    workspace_id: Nonempty
    files: tuple[InlineFile, ...]


class ReadArtifactRequest(WireModel):
    artifact_id: Annotated[Nonempty, Field(description="Artifact id from a reference or listing.")]
    offset: Annotated[int, Field(description="Byte offset into the artifact.", ge=0)] = 0
    length: Annotated[
        PositiveBudget,
        Field(description="Bytes to read; oversized ranges fault with response_limit."),
    ] = 8 * 2**10


class ListArtifactsRequest(WireModel):
    cursor: Nonempty | None = None
    limit: PositiveBudget = 50


class DeleteArtifactRequest(WireModel):
    artifact_id: Nonempty
