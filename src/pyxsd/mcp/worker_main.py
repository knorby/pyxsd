"""Isolated clean-worker entry point; stdout carries only bounded JSON frames."""

import argparse
import sys
from typing import BinaryIO, Literal, cast

from pydantic import JsonValue
from pyxsd.exceptions import PyXSDError
from pyxsd.schema_context import last_components, remember_components

from .cache import CompileRequest, SchemaCache
from .contracts import SchemaRef, ToolFault
from .errors import ToolError
from .snapshots import Dependency, WorkspacePolicyError, WorkspaceResourcePolicy
from .worker_protocol import (
    DiscoverOperation,
    Operation,
    ParseOperation,
    WorkerConfig,
    WorkerReply,
    decode_frame,
    decode_operation,
    encode_frame,
)
from .workers import snapshot_from_wire


def _read_frame(stream: BinaryIO, max_bytes: int) -> bytes:
    """Bound allocation before JSON decoding; excess input terminates the channel."""
    frame = stream.readline(max_bytes + 1)
    if len(frame) > max_bytes:
        raise ToolError("ipc_limit", "Worker input exceeds its byte limit")
    return frame


def _request(operation: Operation, processor_version: str) -> CompileRequest:
    assert isinstance(operation, ParseOperation)
    return CompileRequest(
        snapshot=snapshot_from_wire(operation.snapshot),
        schema_ref=operation.schema_ref,
        hints=operation.hints,
        processor_version=processor_version,
        owner_id=operation.owner_id,
    )


def _execute(
    operation: Operation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    if isinstance(operation, ParseOperation):
        request = _request(operation, processor_version)
        with cache.acquire(request) as schema:
            document = schema.parse("/workspace/" + operation.document.path)
            return {
                "report": cast(dict[str, JsonValue], document.report.to_dict()),
                "root_present": document.root is not None,
            }
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
                max_bytes=config.limits.request_bytes,
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
                    max_bytes=config.limits.request_bytes,
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
                    max_bytes=config.limits.request_bytes,
                )
            except (WorkspacePolicyError, PyXSDError) as exc:
                operations += 1
                code = "invalid_xml" if isinstance(operation, ParseOperation) else "invalid_schema"
                _reply(
                    sys.stdout.buffer,
                    operation,
                    role=arguments.role,
                    status="error",
                    fault=ToolFault(code=code, message=str(exc)[: config.limits.message_bytes]),
                    operations=operations,
                    max_bytes=config.limits.request_bytes,
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
                    max_bytes=config.limits.request_bytes,
                )
            if fatal:
                return 1
    finally:
        if cache is not None:
            cache.close()
        remember_components(previous)


if __name__ == "__main__":
    raise SystemExit(main())
