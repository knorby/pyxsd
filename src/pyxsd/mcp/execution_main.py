"""One-shot transform process; only the coordinator decides candidate validity."""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyxsd import Document, Schema, relaxng
from pyxsd.binding import ParseModes
from pyxsd.relaxng import RelaxNGDocument, RelaxNGElement, RelaxNGSchema
from pyxsd.schema_base import SchemaBase

from .captures import InputResourcePolicy
from .config import relaxng_limits_for_mcp
from .contracts import CompileOptions, SchemaRef, ToolFault
from .errors import ToolError
from .execution import ExecutionRequest
from .transforms import PythonTransformSpec, normalize_transform
from .worker_protocol import WorkerReply, decode_frame, encode_frame
from .workers import capture_from_wire


@dataclass(frozen=True)
class TransformContext:
    source_path: str
    generation: int
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
    elif isinstance(value, RelaxNGElement):
        # The RELAX NG document hook wraps a lexical tree (including a
        # generated projection subclass) as a stale document. An XSD document
        # passes the node through, so a cross-language root is rejected here.
        wrapped = document._document_for_node_result(value)
        if not isinstance(wrapped, Document):
            raise ToolError(
                "invalid_transform_result",
                "Transform must return Document, bound root, or None",
            )
        document = wrapped
    elif isinstance(value, SchemaBase):
        # Keep the XSD root-callable contract: a bound root becomes the root of
        # this document. A RELAX NG document has no XSD root; its schema-specific
        # hook rejects the value instead of adopting foreign semantics.
        if isinstance(document, RelaxNGDocument):
            raise ToolError(
                "invalid_transform_result",
                "Transform must return Document, bound root, or None",
            )
        document.root = value
    elif value is not None:
        raise ToolError(
            "invalid_transform_result", "Transform must return Document, bound root, or None"
        )
    if document.root is None:
        raise ToolError("unbindable", "Transform produced no usable root")
    return document


def _reject_relaxng_xsd_options(reference: SchemaRef) -> None:
    """Keep XSD-only compile inputs out of RELAX NG before any read.

    Preview execution names its schema language explicitly, so a RELAX NG
    reference with nondefault XSD semantics must fail instead of silently
    ignoring the caller's requested behavior. This mirrors the cache's
    compiler dispatch.
    """
    options = reference.options
    defaults = CompileOptions()
    rejected: list[str] = []
    if options.binding != defaults.binding:
        rejected.append("binding")
    if options.namespaces != defaults.namespaces:
        rejected.append("namespaces")
    if options.xsd_version != defaults.xsd_version:
        rejected.append("xsd_version")
    if options.namespace_schemas != defaults.namespace_schemas:
        rejected.append("namespace_schemas")
    if rejected:
        raise ToolError(
            "invalid_argument",
            "RELAX NG schemas accept only default XSD compile options",
            details={"format": "relax-ng", "rejected": list(rejected)},
        )


def _compile_preview_schema(
    reference: SchemaRef, policy: InputResourcePolicy
) -> Schema | RelaxNGSchema:
    """Compile the explicitly named preview language through its public API."""
    path = "/workspace/" + reference.source.path
    if reference.format == "relax-ng":
        _reject_relaxng_xsd_options(reference)
        return relaxng.compile(
            path,
            resource_policy=policy,
            limits=relaxng_limits_for_mcp(policy.limits),
        )
    options = reference.options
    return Schema.compile(
        path,
        mode=(ParseModes.LAX if options.binding == "lax" else ParseModes.STRICT).replace(
            namespaces=options.namespaces
        ),
        xsd_version=options.xsd_version,
        namespace_schemas={
            key: "/workspace/" + value.path for key, value in options.namespace_schemas.items()
        },
        resource_policy=policy,
    )


def _parse_preview_document(schema: Schema | RelaxNGSchema, path: str) -> Document:
    """Parse one source document through the schema's public instance method.

    RELAX NG previews use the normal typed public mode. Transform code may
    rely on generated descriptors and typed values, and typed parsing never
    changes the boolean verdict. The validation/query worker remains
    validation-only because it produces verdicts rather than user-visible
    transformed trees.
    """
    if isinstance(schema, RelaxNGSchema):
        return schema.parse(path, typed=True)
    return schema.parse(path)


def execute(request: ExecutionRequest) -> dict[str, Any]:
    spec = normalize_transform(request.transform, request.limits)
    policy = InputResourcePolicy(capture_from_wire(request.capture), request.limits)
    schema = _compile_preview_schema(request.schema_ref, policy)
    document = _parse_preview_document(schema, "/workspace/" + request.document.path)
    if document.root is None:
        raise ToolError("unbindable", "Source document has no usable root")
    context = TransformContext(
        request.document.path,
        capture_from_wire(request.capture).generation,
        request.output_directory,
    )
    checkpoints = []
    if isinstance(spec, PythonTransformSpec):
        document = _apply(document, spec.source, context)
    else:
        for index, step in enumerate(spec.steps):
            document = _apply(document, request.sources[step.name], context, step.args, step.kwargs)
            if step.checkpoint:
                checkpoint = document.to_string().encode("utf-8")
                if (
                    request.limits.output_bytes is not None
                    and len(checkpoint) > request.limits.output_bytes
                ):
                    raise ToolError("output_limit", "Checkpoint exceeds its output budget")
                with open(
                    Path(request.output_directory) / f"checkpoint-{index}.xml", "xb"
                ) as stream:
                    stream.write(checkpoint)
                # Advisory only: user code can mutate this validator. The
                # coordinator performs the mandatory independent clean parse.
                checkpoints.append(document.revalidate().report.to_dict())
    candidate = document.to_string().encode("utf-8")
    if request.limits.output_bytes is not None and len(candidate) > request.limits.output_bytes:
        raise ToolError("output_limit", "Candidate exceeds its output budget")
    with open(Path(request.output_directory) / "candidate.xml", "xb") as stream:
        stream.write(candidate)
    return {"checkpoints": checkpoints}


def _byte_budget(value: str) -> int | None:
    if value == "unlimited":
        return None
    if not value.isdecimal() or int(value) <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer or 'unlimited'")
    return int(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request-bytes", type=_byte_budget, default=None)
    arguments = parser.parse_args(argv)
    request = None
    try:
        frame = (
            sys.stdin.buffer.readline()
            if arguments.request_bytes is None
            else sys.stdin.buffer.readline(arguments.request_bytes + 1)
        )
        request = decode_frame(
            frame,
            ExecutionRequest,
            max_bytes=arguments.request_bytes,
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
    reply_budget = request.limits.request_bytes if request is not None else arguments.request_bytes
    sys.stdout.buffer.write(encode_frame(reply, max_bytes=reply_budget))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
