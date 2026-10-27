"""One-shot transform process; only the coordinator decides candidate validity."""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyxsd import Document, Schema
from pyxsd.binding import ParseModes
from pyxsd.schema_base import SchemaBase

from .contracts import ToolFault
from .errors import ToolError
from .execution import ExecutionRequest
from .snapshots import WorkspaceResourcePolicy
from .transforms import PythonTransformSpec, normalize_transform
from .worker_protocol import WorkerReply, decode_frame, encode_frame
from .workers import snapshot_from_wire


@dataclass(frozen=True)
class TransformContext:
    source_path: str
    revision: str
    output_directory: str


def _apply(
    document: Document,
    source: str,
    context: TransformContext,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> Document:
    namespace: dict[str, Any] = {"__name__": "__pyxsd_transform__"}
    exec(compile(source, "<transform>", "exec"), namespace)
    function = namespace.get("transform")
    if not callable(function):
        raise ToolError("invalid_transform", "Source must define transform(document, context)")
    value = function(document, context, *args, **(kwargs or {}))
    if isinstance(value, Document):
        document = value
    elif isinstance(value, SchemaBase):
        document.root = value
    elif value is not None:
        raise ToolError(
            "invalid_transform_result", "Transform must return Document, bound root, or None"
        )
    if document.root is None:
        raise ToolError("unbindable", "Transform produced no usable root")
    return document


def execute(request: ExecutionRequest) -> dict[str, Any]:
    spec = normalize_transform(request.transform, request.limits)
    policy = WorkspaceResourcePolicy(snapshot_from_wire(request.snapshot), request.limits)
    options = request.schema_ref.options
    schema = Schema.compile(
        "/workspace/" + request.schema_ref.source.path,
        mode=(ParseModes.LAX if options.binding == "lax" else ParseModes.STRICT).replace(
            namespaces=options.namespaces
        ),
        xsd_version=options.xsd_version,
        namespace_schemas={
            key: "/workspace/" + value.path for key, value in options.namespace_schemas.items()
        },
        resource_policy=policy,
    )
    document = schema.parse("/workspace/" + request.document.path)
    if document.root is None:
        raise ToolError("unbindable", "Source document has no usable root")
    context = TransformContext(
        request.document.path, request.snapshot.revision, request.output_directory
    )
    checkpoints = []
    if isinstance(spec, PythonTransformSpec):
        document = _apply(document, spec.source, context)
    else:
        for index, step in enumerate(spec.steps):
            document = _apply(document, request.sources[step.name], context, step.args, step.kwargs)
            if step.checkpoint:
                checkpoint = document.to_string().encode("utf-8")
                if len(checkpoint) > request.limits.output_bytes:
                    raise ToolError("output_limit", "Checkpoint exceeds its output budget")
                with open(
                    Path(request.output_directory) / f"checkpoint-{index}.xml", "xb"
                ) as stream:
                    stream.write(checkpoint)
                # Advisory only: user code can mutate this validator. The
                # coordinator performs the mandatory independent clean parse.
                checkpoints.append(document.revalidate().report.to_dict())
    candidate = document.to_string().encode("utf-8")
    if len(candidate) > request.limits.output_bytes:
        raise ToolError("output_limit", "Candidate exceeds its output budget")
    with open(Path(request.output_directory) / "candidate.xml", "xb") as stream:
        stream.write(candidate)
    return {"checkpoints": checkpoints}


def main() -> int:
    maximum = 2 * 2**20
    try:
        request = decode_frame(
            sys.stdin.buffer.readline(maximum + 1),
            ExecutionRequest,
            max_bytes=maximum,
            expected={"request_id": "execution"},
        )
        result = execute(request)
        reply = WorkerReply(request_id="execution", role="execution", status="ok", result=result)
    except ToolError as exc:
        reply = WorkerReply(
            request_id="execution", role="execution", status="error", fault=exc.fault
        )
    except Exception:
        reply = WorkerReply(
            request_id="execution",
            role="execution",
            status="error",
            fault=ToolFault(
                code="transform_failed", message="Transform failed without a candidate"
            ),
        )
    sys.stdout.buffer.write(encode_frame(reply, max_bytes=maximum))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
