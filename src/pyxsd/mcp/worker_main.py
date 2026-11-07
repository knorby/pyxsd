"""Isolated clean-worker entry point; stdout carries only bounded JSON frames."""

import argparse
import base64
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
from pyxsd.source_spans import SourceIndex
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

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
        required = [(current.document.path, "document")]
        if current.schema_ref is not None:
            required.append((current.schema_ref.source.path, "schema"))
            required.extend(
                (file.path, "schema")
                for file in current.schema_ref.options.namespace_schemas.values()
            )
        absent = [(path, role) for path, role in required if path not in present]
        if absent:
            missing = sorted({path for path, _role in absent})
            path, role = absent[0]
            items[index] = _batch_fault(
                current,
                "not_found",
                "Required input is missing: " + path,
                cache,
                details={"role": role, "path": path, "missing": [str(path) for path in missing]},
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
    item: BatchValidationItem,
    code: str,
    message: str,
    cache: SchemaCache,
    *,
    details: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    fault: dict[str, JsonValue] = {
        "code": code,
        "message": message[: cache.limits.message_bytes],
    }
    if details is not None:
        fault["details"] = details
    return {
        "document": {"path": item.document.path},
        "valid": None,
        "counts": None,
        "report": None,
        "fault": fault,
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


def _error_code_counts(report: dict[str, object]) -> dict[str, int]:
    issues = report.get("issues", [])
    if not isinstance(issues, list):
        return {}
    counts: Counter[str] = Counter()
    for issue in issues:
        if isinstance(issue, dict) and issue.get("severity") == "error":
            code = issue.get("code")
            if isinstance(code, str):
                counts[code] += 1
    return dict(sorted(counts.items()))


def _probe_summary(report: dict[str, object]) -> dict[str, JsonValue]:
    counts = report.get("counts")
    error_count = 0
    warning_count = 0
    if isinstance(counts, dict):
        error_count = int(cast(int, counts.get("error", 0)))
        warning_count = int(cast(int, counts.get("warning", 0)))
    return {
        "error_count": error_count,
        "warning_count": warning_count,
        "codes": cast(JsonValue, _error_code_counts(report)),
    }


def _has_prefixed_unknown_head(report: dict[str, object]) -> bool:
    issues = report.get("issues", [])
    if not isinstance(issues, list):
        return False
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("code") != "unknown-substitution-head":
            continue
        details = issue.get("details")
        if isinstance(details, dict) and details.get("prefixed") is True:
            return True
    return False


def _bounded_probe_message(message: str, cache: SchemaCache) -> str:
    limits = getattr(cache, "limits", None)
    maximum = getattr(limits, "message_bytes", None)
    if maximum is None or len(message.encode("utf-8")) <= maximum:
        return message
    return message.encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


def _namespace_probe(
    operation: DescribeOperation,
    cache: SchemaCache,
    processor_version: str,
    current_report: dict[str, object],
) -> dict[str, JsonValue]:
    """Compile the same schema with strict namespaces and compare measured evidence."""
    if operation.schema_ref.options.namespaces != "legacy":
        return {}
    if not _has_prefixed_unknown_head(current_report):
        return {}
    strict_options = operation.schema_ref.options.model_copy(update={"namespaces": "strict"})
    strict_ref = operation.schema_ref.model_copy(update={"options": strict_options})
    request = _for_schema(operation, strict_ref, processor_version)
    alternative: dict[str, object]
    try:
        with cache.acquire(request) as schema:
            alternative = schema.report.to_dict()
    except ToolError as exc:
        return {
            "status": "error",
            "fault": {
                "code": exc.fault.code,
                "message": _bounded_probe_message(exc.fault.message, cache),
                "details": cast(JsonValue, exc.fault.details),
            },
        }
    except PyXSDError as exc:
        return {
            "status": "error",
            "fault": {
                "code": "invalid_schema",
                "message": _bounded_probe_message(str(exc), cache),
                "details": {},
            },
        }
    current_summary = _probe_summary(current_report)
    alternative_summary = _probe_summary(alternative)
    current_codes = cast(dict[str, int], current_summary["codes"])
    alternative_codes = cast(dict[str, int], alternative_summary["codes"])
    introduced = sorted(set(alternative_codes) - set(current_codes))
    resolved = sorted(set(current_codes) - set(alternative_codes))
    probe: dict[str, JsonValue] = {
        "status": "ok",
        "current": current_summary,
        "alternative": alternative_summary,
        "introduced_codes": cast(JsonValue, introduced),
        "resolved_codes": cast(JsonValue, resolved),
    }
    head_code = "unknown-substitution-head"
    measured_improvement = (
        current_codes.get(head_code, 0) > alternative_codes.get(head_code, 0)
        and int(cast(int, alternative_summary["error_count"]))
        < int(cast(int, current_summary["error_count"]))
        and not introduced
    )
    if measured_improvement:
        probe["recommendation"] = {
            "schema_ref": encode_value(strict_ref),
            "explanation": (
                "Strict namespaces resolved the prefixed substitution head without "
                "introducing another issue code."
            ),
        }
    return probe


def _describe(
    operation: DescribeOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    request = _for_schema(operation, operation.schema_ref, processor_version)
    if operation.view == "diagnostics":
        with cache.acquire(request) as schema:
            current_report = schema.report.to_dict()
        probe = _namespace_probe(operation, cache, processor_version, current_report)
        recommendation = probe.pop("recommendation", None)
        recommendations: list[dict[str, JsonValue]] = []
        if isinstance(recommendation, dict):
            recommendations.append(recommendation)
        return {
            "view": "diagnostics",
            "report": encode_value(current_report),
            "recommendations": cast(JsonValue, recommendations),
            "namespace_probe": cast(JsonValue, probe if probe else None),
        }
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


_SOURCE_UNAVAILABLE = "The occurrence has no addressable original source bytes"


def _source_index(operation: QueryOperation, cache: SchemaCache) -> SourceIndex:
    """Index the operation document's captured bytes once, under the MCP budget."""
    policy = InputResourcePolicy(capture_from_wire(operation.capture), cache.limits)
    try:
        data = policy.read("/workspace/" + operation.document.path, base=None, purpose="instance")
    except InputPolicyError as exc:
        raise ToolError(exc.code, str(exc).partition(": ")[2] or str(exc)) from exc
    limits = cache.limits
    return SourceIndex(
        data.source_id,
        data.content,
        policy=XmlParserPolicy.strict(),
        limits=XmlLimits(
            max_bytes=limits.xml_file_bytes,
            max_depth=limits.xml_depth,
            max_elements=limits.xml_elements,
        ),
    )


def _node_summary(
    document: Document, node: SchemaBase, index: SourceIndex | None
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"name": getattr(node, "_name_", None)}
    provenance = document._provenance
    location = provenance.bound_location(node) if provenance is not None else None
    result["location"] = encode_value(location)
    if index is None:
        result["text"] = encode_value(getattr(node, "_value_", None))
        return result
    span = None if location is None else index.span(location.path)
    result["encoding"] = "base64"
    if location is None or span is None:
        result["data"] = None
        result["byte_length"] = None
        result["span"] = None
        result["source_faithful"] = False
        result["fault"] = {"code": "source_unavailable", "message": _SOURCE_UNAVAILABLE}
        return result
    chunk = cast(bytes, index.extract(location.path))
    result["data"] = base64.b64encode(chunk).decode("ascii")
    result["byte_length"] = len(chunk)
    result["span"] = {"start": span.start, "end": span.end}
    result["source_faithful"] = True
    result["fault"] = None
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
            index = (
                _source_index(operation, cache) if operation.result == "subtree" and page else None
            )
            results = [_node_summary(document, item, index) for item in page]
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


def _reply_fault(
    stream: BinaryIO,
    operation: Operation | None,
    *,
    role: Literal["clean", "execution"],
    fault: ToolFault,
    operations: int,
    max_bytes: int,
) -> None:
    """Preserve a fault's code while fitting its reply inside the IPC budget."""
    message = fault.message
    details: dict[str, JsonValue] = fault.details
    reduced_details = False
    while True:
        bounded = fault.model_copy(update={"message": message, "details": details})
        try:
            _reply(
                stream,
                operation,
                role=role,
                status="error",
                fault=bounded,
                operations=operations,
                max_bytes=max_bytes,
            )
        except ToolError as exc:
            if exc.fault.code != "ipc_limit":
                raise
            if fault.details and not reduced_details:
                details = {"details_truncated": True}
                reduced_details = True
            elif len(message) > 1:
                message = message[: max(1, len(message) // 2)]
            elif details:
                details = {}
            else:
                raise
        else:
            return


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
                _reply_fault(
                    sys.stdout.buffer,
                    operation,
                    role=arguments.role,
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
                _reply_fault(
                    sys.stdout.buffer,
                    operation,
                    role=arguments.role,
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
                _reply_fault(
                    sys.stdout.buffer,
                    operation,
                    role=arguments.role,
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
