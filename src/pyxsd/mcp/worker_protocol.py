"""Bounded, role-validated JSON messages used across worker process boundaries.

This codec is not authentication or a sandbox. The coordinator chooses models
and identity bindings; neither is accepted from a tool argument or worker reply.
"""

import json
from typing import Annotated, Literal, NoReturn, TypeVar

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from .config import Limits
from .contracts import FileRef, Nonempty, SchemaRef, ToolFault, WireModel
from .errors import ToolError

T = TypeVar("T", bound=WireModel)


class SnapshotFileMessage(WireModel):
    path: Nonempty
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size: Annotated[int, Field(ge=0)]


class SnapshotMessage(WireModel):
    """Internal mounted snapshot identity; never exposed as public resource metadata."""

    workspace_id: Nonempty
    revision: Nonempty
    root: Nonempty
    files: tuple[SnapshotFileMessage, ...]


class WorkerConfig(WireModel):
    request_id: Literal["bootstrap"] = "bootstrap"
    role: Literal["clean", "execution"]
    limits: Limits
    processor_version: Nonempty


class Operation(WireModel):
    request_id: Nonempty
    role: Literal["clean", "execution"] = "clean"
    snapshot: SnapshotMessage
    owner_id: Nonempty


class ParseOperation(Operation):
    kind: Literal["parse"] = "parse"
    schema_ref: SchemaRef | None
    document: FileRef
    hints: tuple[tuple[str | None, str], ...] = ()
    issue_offset: Annotated[int, Field(ge=0)] = 0
    issue_limit: Annotated[int, Field(gt=0)] = 50


class DescribeOperation(Operation):
    kind: Literal["describe"] = "describe"
    schema_ref: SchemaRef
    component_ids: tuple[Nonempty, ...] = ()
    view: Literal["overview", "compact", "full", "constraints"] = "overview"
    depth: Annotated[int, Field(ge=0)] | None = 1
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(gt=0)] = 50


class FindOperation(Operation):
    kind: Literal["find"] = "find"
    schema_ref: SchemaRef
    text: str | None = None
    component_kind: Nonempty | None = None
    name: Nonempty | None = None
    namespace: str | None = None
    component_id: Nonempty | None = None
    direction: Literal["uses", "used_by"] = "uses"
    depth: Annotated[int, Field(ge=0)] = 1
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(gt=0)] = 50


class QueryOperation(Operation):
    kind: Literal["query"] = "query"
    schema_ref: SchemaRef
    document: FileRef
    xpath: Nonempty
    namespaces: dict[str, str] = Field(default_factory=dict)
    result: Literal["summary", "subtree"] = "summary"
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(gt=0)] = 50


class DiscoverOperation(Operation):
    kind: Literal["discover"] = "discover"
    refs: tuple[FileRef | SchemaRef, ...]


class StructuralDiffOperation(Operation):
    kind: Literal["structural-diff"] = "structural-diff"
    original: FileRef
    candidate: FileRef
    limit: Annotated[int, Field(gt=0)] = 50


class ModelOperation(Operation):
    kind: Literal["model"] = "model"
    schema_ref: SchemaRef
    element: Nonempty
    path: tuple[Nonempty, ...] = ()
    action: Literal["inspect", "validate"] = "inspect"
    data: JsonValue = None


class FieldSourceSpec(WireModel):
    scope: Literal["record", "root", "ancestor"] = "record"
    levels: Annotated[int, Field(ge=0)] = 0
    path: tuple[Nonempty, ...] = ()
    attribute: Nonempty | None = None


class RecordsOperation(Operation):
    kind: Literal["records"] = "records"
    schema_ref: SchemaRef
    document: FileRef
    element: Nonempty
    path: tuple[Nonempty, ...] = ()
    selector: str | None = None
    namespaces: dict[str, str] = Field(default_factory=dict)
    columns: dict[str, FieldSourceSpec] | None = None
    exact_count: bool = True


class ExportOperation(Operation):
    kind: Literal["export-records"] = "export-records"
    schema_ref: SchemaRef
    documents: tuple[FileRef, ...]
    element: Nonempty
    path: tuple[Nonempty, ...] = ()
    selector: str | None = None
    namespaces: dict[str, str] = Field(default_factory=dict)
    columns: dict[str, FieldSourceSpec] | None = None
    format: Literal["parquet", "dataset"] = "parquet"
    output_directory: Nonempty = "pending"


CleanOperation = Annotated[
    ParseOperation
    | DescribeOperation
    | FindOperation
    | QueryOperation
    | DiscoverOperation
    | StructuralDiffOperation
    | ModelOperation
    | RecordsOperation
    | ExportOperation,
    Field(discriminator="kind"),
]
_OPERATION: TypeAdapter[
    ParseOperation
    | DescribeOperation
    | FindOperation
    | QueryOperation
    | DiscoverOperation
    | StructuralDiffOperation
    | ModelOperation
    | RecordsOperation
    | ExportOperation
] = TypeAdapter(CleanOperation)


class WorkerReply(WireModel):
    request_id: Nonempty
    role: Literal["clean", "execution"]
    status: Literal["ok", "error"]
    result: JsonValue = None
    fault: ToolFault | None = None
    operations: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def consistent_outcome(self) -> "WorkerReply":
        if self.status == "ok" and self.fault is not None:
            raise ValueError("A successful worker reply cannot carry a fault")
        if self.status == "error" and (self.fault is None or self.result is not None):
            raise ValueError("An error reply needs one fault and no result")
        return self


def _budget(max_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ToolError("invalid_argument", "IPC byte limit must be a positive integer")


def _object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> NoReturn:
    raise ValueError("Nonfinite JSON number")


def encode_frame(message: WireModel, *, max_bytes: int) -> bytes:
    """Serialize one wire model, including its newline in the byte budget."""
    _budget(max_bytes)
    if not isinstance(message, WireModel):
        raise ToolError("ipc_protocol", "IPC requires a wire model")
    try:
        frame = (
            json.dumps(
                message.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise ToolError("ipc_protocol", "IPC message cannot be encoded") from exc
    if len(frame) > max_bytes:
        raise ToolError("ipc_limit", "IPC frame exceeds its byte limit")
    return frame


def _decode_object(
    frame: bytes, *, max_bytes: int, expected: dict[str, str]
) -> dict[str, JsonValue]:
    _budget(max_bytes)
    if len(frame) > max_bytes:
        raise ToolError("ipc_limit", "IPC frame exceeds its byte limit")
    try:
        if not frame.endswith(b"\n") or frame.count(b"\n") != 1:
            raise ValueError("Incomplete frame")
        data = json.loads(
            frame.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
        if not isinstance(data, dict) or any(
            key not in data or data[key] != value for key, value in expected.items()
        ):
            raise ValueError("Channel identity mismatch")
        return data
    except (ValueError, TypeError, RecursionError) as exc:
        raise ToolError("ipc_protocol", "Invalid IPC frame or channel identity") from exc


def decode_frame(
    frame: bytes,
    model: type[T],
    *,
    max_bytes: int,
    expected: dict[str, str],
) -> T:
    """Validate one complete frame against the channel's model and identities."""
    _decode_object(frame, max_bytes=max_bytes, expected=expected)
    try:
        return model.model_validate_json(frame)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ToolError("ipc_protocol", "Invalid IPC frame or channel identity") from exc


def decode_operation(
    frame: bytes, *, max_bytes: int, role: str
) -> (
    ParseOperation
    | DescribeOperation
    | FindOperation
    | QueryOperation
    | DiscoverOperation
    | StructuralDiffOperation
    | ModelOperation
    | RecordsOperation
    | ExportOperation
):
    """Decode the closed operation union selected by the coordinator's worker role."""
    _decode_object(frame, max_bytes=max_bytes, expected={"role": role})
    try:
        return _OPERATION.validate_json(frame)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ToolError("ipc_protocol", "Invalid worker operation") from exc
