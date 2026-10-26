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
