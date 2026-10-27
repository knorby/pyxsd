"""Isolated clean-worker entry point; stdout carries only bounded JSON frames."""

import argparse
import io
import re
import sys
from collections import Counter
from typing import BinaryIO, Literal, cast

from elementpath.exceptions import ElementPathError

from pydantic import JsonValue
from pyxsd import parse as parse_document
from pyxsd.binding import ParseModes
from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError
from pyxsd.introspection import (
    describe_component,
    describe_schema,
    find_components,
    related_components,
)
from pyxsd.schema_base import SchemaBase
from pyxsd.schema_context import last_components, remember_components
from pyxsd.writers.xml_tree_writer import XmlTreeWriter

from .cache import CompileRequest, SchemaCache
from .contracts import SchemaRef, ToolFault
from .encoding import encode_value
from .errors import ToolError
from .snapshots import Dependency, WorkspacePolicyError, WorkspaceResourcePolicy
from .worker_protocol import (
    DescribeOperation,
    DiscoverOperation,
    FindOperation,
    Operation,
    ParseOperation,
    QueryOperation,
    StructuralDiffOperation,
    WorkerConfig,
    WorkerReply,
    decode_frame,
    decode_operation,
    encode_frame,
)
from .workers import snapshot_from_wire

_EXTERNAL_XPATH = re.compile(
    r"(?:^|[^\w.-])(?:[A-Za-z_][\w.-]*:)?"
    r"(?:doc|document|collection|uri-collection|json-doc|unparsed-text|unparsed-text-lines)\s*\("
)
_PARSER_POSITION = re.compile(r"line (\d+), column (\d+)")


def _read_frame(stream: BinaryIO, max_bytes: int) -> bytes:
    """Bound allocation before JSON decoding; excess input terminates the channel."""
    frame = stream.readline(max_bytes + 1)
    if len(frame) > max_bytes:
        raise ToolError("ipc_limit", "Worker input exceeds its byte limit")
    return frame


def _for_schema(
    operation: Operation, schema_ref: SchemaRef, processor_version: str
) -> CompileRequest:
    return CompileRequest(
        snapshot=snapshot_from_wire(operation.snapshot),
        schema_ref=schema_ref,
        hints=getattr(operation, "hints", ()),
        processor_version=processor_version,
        owner_id=operation.owner_id,
    )


def _parse(
    operation: ParseOperation | QueryOperation, cache: SchemaCache, processor_version: str
) -> Document:
    if operation.schema_ref is None:
        policy = WorkspaceResourcePolicy(snapshot_from_wire(operation.snapshot), cache.limits)
        return parse_document(
            "/workspace/" + operation.document.path,
            mode=ParseModes.LAX,
            resource_policy=policy,
        )
    request = _for_schema(operation, operation.schema_ref, processor_version)
    with cache.acquire(request) as schema:
        return schema.parse("/workspace/" + operation.document.path)


def _page(total: int, offset: int, returned: int) -> dict[str, JsonValue]:
    return {
        "returned": returned,
        "truncated": offset + returned < total,
        "total": total,
    }


def _report(document: Document, operation: ParseOperation) -> dict[str, JsonValue]:
    data = cast(dict[str, JsonValue], document.report.to_dict())
    issues = cast(list[JsonValue], data["issues"])
    page_issues = issues[operation.issue_offset : operation.issue_offset + operation.issue_limit]
    data["issues"] = page_issues
    page = _page(len(issues), operation.issue_offset, len(page_issues))
    counts = cast(dict[str, JsonValue], data["counts"])
    page["total"] = int(cast(int, counts["error"])) + int(cast(int, counts["warning"]))
    return {
        "report": data,
        "page": page,
        "root_present": document.root is not None,
    }


def _describe(
    operation: DescribeOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    request = _for_schema(operation, operation.schema_ref, processor_version)
    with cache.acquire(request) as schema:
        description = describe_schema(schema)
        selected_ids = operation.component_ids or description.roots
        try:
            selected = [describe_component(schema, item) for item in selected_ids]
        except KeyError as exc:
            raise ToolError("not_found", "Schema component not found") from exc
        if operation.view in ("compact", "full"):
            return {
                "view": operation.view,
                "stub": schema.stub(
                    compact=operation.view == "compact",
                    component_ids=selected_ids,
                    depth=operation.depth,
                ),
            }
        counts = cast(
            dict[str, JsonValue],
            dict(sorted(Counter(item.kind for item in description.components).items())),
        )
        result: dict[str, JsonValue] = {
            "view": operation.view,
            "target_namespace": description.target_namespace,
            "roots": list(description.roots),
            "counts": counts,
        }
        if operation.view == "constraints" or operation.component_ids:
            page = selected[operation.offset : operation.offset + operation.limit]
            result["components"] = encode_value(page)
            result["page"] = _page(len(selected), operation.offset, len(page))
        return result


def _find(
    operation: FindOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    request = _for_schema(operation, operation.schema_ref, processor_version)
    with cache.acquire(request) as schema:
        try:
            if operation.component_id is not None:
                seed = describe_component(schema, operation.component_id)
                found = (
                    seed,
                    *related_components(
                        schema,
                        operation.component_id,
                        direction=operation.direction,
                        depth=operation.depth,
                    ),
                )
            else:
                found = find_components(
                    schema,
                    text=operation.text,
                    kind=operation.component_kind,
                    namespace=operation.namespace,
                )
        except KeyError as exc:
            raise ToolError("not_found", "Schema component not found") from exc
        if operation.name is not None:
            found = tuple(item for item in found if item.name == operation.name)
        page = found[operation.offset : operation.offset + operation.limit]
        return {
            "components": encode_value(page),
            "page": _page(len(found), operation.offset, len(page)),
        }


def _node_summary(document: Document, node: SchemaBase, subtree: bool) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"name": getattr(node, "_name_", None)}
    provenance = document._provenance
    location = provenance.bound_location(node) if provenance is not None else None
    result["location"] = encode_value(location)
    if subtree:
        output = io.StringIO()
        XmlTreeWriter(node, output)
        result["subtree"] = output.getvalue()
    else:
        result["text"] = encode_value(getattr(node, "_value_", None))
    return result


def _query(
    operation: QueryOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    if _EXTERNAL_XPATH.search(operation.xpath):
        raise ToolError("invalid_query", "External-resource XPath functions are disabled")
    document = _parse(operation, cache, processor_version)
    if document.root is None:
        raise ToolError("unbindable", "The document has no usable bound root")
    try:
        value = document.xpath(operation.xpath, namespaces=operation.namespaces)
    except (ElementPathError, ValueError, TypeError) as exc:
        raise ToolError("invalid_query", "XPath or namespace mapping is invalid") from exc
    if isinstance(value, list):
        page = value[operation.offset : operation.offset + operation.limit]
        results: JsonValue
        if all(isinstance(item, SchemaBase) for item in page):
            results = [
                _node_summary(document, item, operation.result == "subtree") for item in page
            ]
        else:
            results = encode_value(page)
        return {"results": results, "page": _page(len(value), operation.offset, len(page))}
    return {"results": encode_value(value)}


def _execute(
    operation: Operation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    limit = getattr(operation, "issue_limit", getattr(operation, "limit", 1))
    if limit > cache.limits.page_items:
        raise ToolError("invalid_argument", "Page limit exceeds the operator budget")
    if isinstance(operation, ParseOperation):
        return _report(_parse(operation, cache, processor_version), operation)
    if isinstance(operation, DescribeOperation):
        return _describe(operation, cache, processor_version)
    if isinstance(operation, FindOperation):
        return _find(operation, cache, processor_version)
    if isinstance(operation, QueryOperation):
        return _query(operation, cache, processor_version)
    if isinstance(operation, StructuralDiffOperation):
        from .structural import compare

        return compare(operation, cache.limits)
    if isinstance(operation, DiscoverOperation):
        snapshot = snapshot_from_wire(operation.snapshot)
        dependencies: dict[str, Dependency] = {}
        for ref in operation.refs:
            if not isinstance(ref, SchemaRef):
                continue
            request = CompileRequest(
                snapshot=snapshot,
                schema_ref=ref,
                hints=(),
                processor_version=processor_version,
                owner_id=operation.owner_id,
            )
            with cache.acquire(request) as schema:
                policy = schema.resource_policy
                assert isinstance(policy, WorkspaceResourcePolicy)
                for item in policy.dependencies:
                    dependencies[item.path] = item
        return {
            "dependencies": [
                {"path": item.path, "digest": item.digest, "size": item.size}
                for _, item in sorted(dependencies.items())
            ]
        }
    raise ToolError("invalid_operation", "Worker operation is not supported")


def _reply(
    stream: BinaryIO,
    operation: Operation | None,
    *,
    role: Literal["clean", "execution"],
    status: Literal["ok", "error"],
    result: JsonValue = None,
    fault: ToolFault | None = None,
    operations: int,
    max_bytes: int,
    request_id: str = "unknown",
) -> None:
    message = WorkerReply(
        request_id=request_id if operation is None else operation.request_id,
        role=role,
        status=status,
        result=result,
        fault=fault,
        operations=operations,
    )
    stream.write(encode_frame(message, max_bytes=max_bytes))
    stream.flush()


def _parser_details(exc: BaseException) -> dict[str, JsonValue]:
    match = _PARSER_POSITION.search(str(exc))
    return {} if match is None else {"line": int(match[1]), "column": int(match[2])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--role", required=True, choices=("clean", "execution"))
    arguments = parser.parse_args(argv)
    if arguments.role != "clean":
        return 2
    previous = last_components()
    cache = None
    operations = 0
    try:
        try:
            config = decode_frame(
                _read_frame(sys.stdin.buffer, 2 * 2**20),
                WorkerConfig,
                max_bytes=2 * 2**20,
                expected={"request_id": "bootstrap", "role": arguments.role},
            )
            cache = SchemaCache(config.limits)
            _reply(
                sys.stdout.buffer,
                None,
                role=arguments.role,
                status="ok",
                result={"ready": True},
                operations=0,
                max_bytes=config.limits.output_bytes,
                request_id="bootstrap",
            )
        except BaseException:
            return 1
        while True:
            frame = sys.stdin.buffer.readline(config.limits.request_bytes + 1)
            if not frame:
                return 0
            operation = None
            fatal = False
            try:
                if len(frame) > config.limits.request_bytes:
                    raise ToolError("ipc_limit", "Worker operation exceeds its byte limit")
                operation = decode_operation(
                    frame, max_bytes=config.limits.request_bytes, role=arguments.role
                )
                result = _execute(operation, cache, config.processor_version)
                operations += 1
                _reply(
                    sys.stdout.buffer,
                    operation,
                    role=arguments.role,
                    status="ok",
                    result=result,
                    operations=operations,
                    max_bytes=config.limits.output_bytes,
                )
            except ToolError as exc:
                operations += 1
                _reply(
                    sys.stdout.buffer,
                    operation,
                    role=arguments.role,
                    status="error",
                    fault=exc.fault,
                    operations=operations,
                    max_bytes=config.limits.output_bytes,
                )
            except (WorkspacePolicyError, PyXSDError) as exc:
                operations += 1
                code = (
                    "invalid_xml"
                    if isinstance(operation, (ParseOperation, QueryOperation))
                    else "invalid_schema"
                )
                _reply(
                    sys.stdout.buffer,
                    operation,
                    role=arguments.role,
                    status="error",
                    fault=ToolFault(
                        code=code,
                        message=str(exc)[: config.limits.message_bytes],
                        details=_parser_details(exc),
                    ),
                    operations=operations,
                    max_bytes=config.limits.output_bytes,
                )
            except Exception:
                operations += 1
                fatal = True
                _reply(
                    sys.stdout.buffer,
                    operation,
                    role=arguments.role,
                    status="error",
                    fault=ToolFault(
                        code="worker_internal",
                        message="Worker operation failed without a verdict",
                        retryable=True,
                    ),
                    operations=operations,
                    max_bytes=config.limits.output_bytes,
                )
            if fatal:
                return 1
    finally:
        if cache is not None:
            cache.close()
        remember_components(previous)


if __name__ == "__main__":
    raise SystemExit(main())
