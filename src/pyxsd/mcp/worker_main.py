"""Isolated clean-worker entry point; stdout carries only bounded JSON frames."""

import argparse
import io
import json
import re
import sys
from collections import Counter
from contextlib import ExitStack
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

from .cache import CompileRequest, SchemaCache, require_complete_schema
from .captures import CaptureDependency, InputPolicyError, InputResourcePolicy
from .contracts import BatchValidationItem, SchemaRef, ToolFault
from .encoding import encode_value
from .errors import ToolError
from .worker_protocol import (
    BatchParseOperation,
    ComparisonOperation,
    DescribeOperation,
    DiscoverOperation,
    ExportOperation,
    FindOperation,
    ModelOperation,
    Operation,
    ParseOperation,
    QueryOperation,
    RecordsOperation,
    StructuralDiffOperation,
    WorkerConfig,
    WorkerReply,
    decode_frame,
    decode_operation,
    encode_frame,
)
from .workers import capture_from_wire

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
        capture=capture_from_wire(operation.capture),
        schema_ref=schema_ref,
        hints=getattr(operation, "hints", ()),
        processor_version=processor_version,
    )


def _parse(
    operation: ParseOperation | QueryOperation, cache: SchemaCache, processor_version: str
) -> Document:
    if operation.schema_ref is None:
        policy = InputResourcePolicy(capture_from_wire(operation.capture), cache.limits)
        document = parse_document(
            "/workspace/" + operation.document.path,
            mode=ParseModes.LAX,
            resource_policy=policy,
        )
        require_complete_schema(document.schema)
        return document
    request = _for_schema(operation, operation.schema_ref, processor_version)
    with cache.acquire(request) as schema:
        require_complete_schema(schema)
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


def _missing_file(exc: BaseException) -> bool:
    """Whether a parser error wraps a vanished instance file, not bad XML."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, FileNotFoundError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _parse_batch(
    operation: BatchParseOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    if len(operation.items) > cache.limits.corpus_max_files:
        raise ToolError("invalid_argument", "Batch exceeds the operator file budget")
    items: list[dict[str, JsonValue] | None] = [None] * len(operation.items)
    groups: dict[str, list[int]] = {}
    present = {file.path for file in operation.capture.files}
    for index, current in enumerate(operation.items):
        required = [current.document.path]
        if current.schema_ref is not None:
            required.append(current.schema_ref.source.path)
            required.extend(
                file.path for file in current.schema_ref.options.namespace_schemas.values()
            )
        missing = sorted(set(required) - present)
        if missing:
            items[index] = _batch_fault(
                current, "not_found", "Required input is missing: " + missing[0], cache
            )
            continue
        key = (
            "hint"
            if current.schema_ref is None
            else json.dumps(current.schema_ref.model_dump(mode="json"), sort_keys=True)
        )
        groups.setdefault(key, []).append(index)
    for indexes in groups.values():
        schema_ref = operation.items[indexes[0]].schema_ref
        document: Document
        if schema_ref is None:
            for index in indexes:
                current = operation.items[index]
                try:
                    policy = InputResourcePolicy(capture_from_wire(operation.capture), cache.limits)
                    document = parse_document(
                        "/workspace/" + current.document.path,
                        mode=ParseModes.LAX,
                        resource_policy=policy,
                    )
                    require_complete_schema(document.schema)
                except ToolError as exc:
                    items[index] = _batch_fault(current, exc.fault.code, exc.fault.message, cache)
                    continue
                except (FileNotFoundError, InputPolicyError, PyXSDError) as exc:
                    code = "not_found" if _missing_file(exc) else "invalid_xml"
                    items[index] = _batch_fault(current, code, str(exc), cache)
                    continue
                items[index] = _batch_outcome(current, document)
            continue
        request = _for_schema(operation, schema_ref, processor_version)
        with ExitStack() as stack:
            try:
                schema = stack.enter_context(cache.acquire(request))
                require_complete_schema(schema)
            except ToolError as exc:
                for index in indexes:
                    current = operation.items[index]
                    items[index] = _batch_fault(current, exc.fault.code, exc.fault.message, cache)
                continue
            except (InputPolicyError, PyXSDError) as exc:
                for index in indexes:
                    items[index] = _batch_fault(
                        operation.items[index], "invalid_schema", str(exc), cache
                    )
                continue
            for index in indexes:
                current = operation.items[index]
                try:
                    document = schema.parse("/workspace/" + current.document.path)
                except (FileNotFoundError, InputPolicyError, PyXSDError) as exc:
                    code = "not_found" if _missing_file(exc) else "invalid_xml"
                    items[index] = _batch_fault(current, code, str(exc), cache)
                    continue
                items[index] = _batch_outcome(current, document)
    return {"items": cast(list[JsonValue], items)}


def _batch_fault(
    item: BatchValidationItem, code: str, message: str, cache: SchemaCache
) -> dict[str, JsonValue]:
    return {
        "document": {"path": item.document.path},
        "valid": None,
        "counts": None,
        "report": None,
        "fault": {"code": code, "message": message[: cache.limits.message_bytes]},
    }


def _batch_outcome(item: BatchValidationItem, document: Document) -> dict[str, JsonValue]:
    counts = cast(
        dict[str, JsonValue], cast(dict[str, JsonValue], document.report.to_dict())["counts"]
    )
    invalid = int(cast(int, counts["error"])) > 0
    return {
        "document": {"path": item.document.path},
        "valid": not invalid,
        "counts": counts,
        "report": cast(dict[str, JsonValue], document.report.to_dict()) if invalid else None,
        "fault": None,
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
        entry_source = "/workspace/" + operation.schema_ref.source.path
        entry_roots = [
            root
            for root in description.roots
            if (loc := describe_component(schema, root).location) is not None
            and loc.source == entry_source
        ]
        result: dict[str, JsonValue] = {
            "view": operation.view,
            "target_namespace": description.target_namespace,
            "roots": list(description.roots),
            "entry_roots": [str(root) for root in entry_roots],
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
    from pyxsd.introspection import COMPONENT_KINDS

    if operation.component_kind is not None and operation.component_kind not in COMPONENT_KINDS:
        raise ToolError(
            "invalid_argument",
            "Unknown component kind; expected one of: " + ", ".join(sorted(COMPONENT_KINDS)),
        )
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
            # Clark notation matches exactly; a bare name matches the local
            # name across namespaces.
            if "{" in operation.name:
                found = tuple(item for item in found if item.name == operation.name)
            else:
                found = tuple(
                    item
                    for item in found
                    if item.name == operation.name
                    or (item.name is not None and item.name.endswith("}" + operation.name))
                )
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
        result["source_faithful"] = False
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
    if isinstance(operation, RecordsOperation):
        from .services.records import execute_records

        return execute_records(operation, cache, processor_version)
    if isinstance(operation, ExportOperation):
        from .services.records import execute_export

        return execute_export(operation, cache, processor_version)
    if isinstance(operation, ComparisonOperation):
        from .services.comparison import execute_comparison

        return execute_comparison(operation, cache, processor_version)
    if isinstance(operation, ModelOperation):
        from .services.models import execute_model

        return execute_model(operation, cache, processor_version)
    limit = getattr(operation, "issue_limit", getattr(operation, "limit", 1))
    if limit > cache.limits.page_items:
        raise ToolError("invalid_argument", "Page limit exceeds the operator budget")
    if isinstance(operation, ParseOperation):
        return _report(_parse(operation, cache, processor_version), operation)
    if isinstance(operation, BatchParseOperation):
        return _parse_batch(operation, cache, processor_version)
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
        capture = capture_from_wire(operation.capture)
        dependencies: dict[str, CaptureDependency] = {}
        for ref in operation.refs:
            if not isinstance(ref, SchemaRef):
                continue
            request = CompileRequest(
                capture=capture,
                schema_ref=ref,
                hints=(),
                processor_version=processor_version,
            )
            with cache.acquire(request) as schema:
                policy = schema.resource_policy
                assert isinstance(policy, InputResourcePolicy)
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
            except (InputPolicyError, PyXSDError) as exc:
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
