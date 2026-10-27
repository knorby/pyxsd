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
    path: Nonempty
    revision: Nonempty | None = None


class InlineFile(WireModel):
    path: Nonempty
    text: str


class CompileOptions(WireModel):
    binding: Literal["lax", "strict"] = "lax"
    namespaces: Literal["strict", "legacy"] = "strict"
    xsd_version: Literal["1.0", "1.1"] = "1.1"
    namespace_schemas: dict[str, FileRef] = Field(default_factory=dict)


class SchemaRef(WireModel):
    source: FileRef
    options: CompileOptions = Field(default_factory=CompileOptions)


class ArtifactRef(WireModel):
    id: Nonempty
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size: Annotated[int, Field(ge=0)]
    media_type: Nonempty
    expires_at: float


class Page(WireModel):
    cursor: Nonempty | None = None
    limit: PositiveBudget = 50


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
    idempotency_key: Nonempty | None = None


class PutFilesRequest(WireModel):
    workspace_id: Nonempty
    files: tuple[InlineFile, ...]
    expected_revision: Nonempty | None = None


class ReadArtifactRequest(WireModel):
    artifact_id: Nonempty
    offset: Annotated[int, Field(ge=0)] = 0
    length: PositiveBudget = 8 * 2**10


JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class JobRef(WireModel):
    id: Nonempty
    state: JobState
    created_at: float
    expires_at: float | None = None


class JobResult(WireModel):
    id: Nonempty
    workspace_id: Nonempty
    state: JobState
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None
    expires_at: float | None = None
    result: JsonValue = None
    fault: ToolFault | None = None


class JobStatusRequest(WireModel):
    job_id: Nonempty


class CancelJobRequest(WireModel):
    job_id: Nonempty
