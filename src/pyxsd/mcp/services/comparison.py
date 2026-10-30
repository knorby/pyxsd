"""Schema comparison with corpus evidence, computed in clean workers."""

import json
import secrets
import time
from collections.abc import Callable
from typing import cast

from pydantic import JsonValue
from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError
from pyxsd.introspection import ComponentDescription, describe_schema
from pyxsd.schema import Schema

from ..cache import CompileRequest, SchemaCache, require_complete_schema
from ..contracts import FileRef, Nonempty, SchemaRef, WireModel
from ..encoding import encode_value
from ..errors import ToolError
from ..inputs import SessionInputs
from ..worker_protocol import ComparisonOperation
from ..workers import WorkerPool, capture_from_wire, capture_to_wire
from . import comparison_identity
from .exploration import ExplorationService

_CATEGORIES = ("valid", "regressed", "resolved", "invalid", "malformed", "unbindable", "skipped")


class CompareSchemasRequest(WireModel):
    workspace_id: Nonempty
    old_schema: SchemaRef
    new_schema: SchemaRef
    corpus: tuple[FileRef, ...] = ()


def _fingerprint(component: ComponentDescription, keys: dict[str, str]) -> str:
    return comparison_identity.fingerprint(component, keys)


def _classify(schema: Schema, path: str) -> tuple[str, Document | None]:
    """Classify one corpus file against one compiled schema, keeping the parse."""
    try:
        document = schema.parse("/workspace/" + path)
    except PyXSDError:
        return "malformed", None
    if document.root is None:
        return "unbindable", document
    return ("valid" if document.report.error_count == 0 else "invalid"), document


def _evidence(document: Document, limit: int) -> list[dict[str, JsonValue]]:
    data = cast(dict[str, JsonValue], document.report.to_dict())
    issues = cast(list[dict[str, JsonValue]], data["issues"])
    return [
        {
            "code": issue.get("code"),
            "message": issue.get("message"),
            "location": issue.get("location"),
        }
        for issue in issues[:limit]
    ]


def execute_comparison(
    operation: ComparisonOperation,
    cache: SchemaCache,
    processor_version: str,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, JsonValue]:
    old_request = CompileRequest(
        capture=capture_from_wire(operation.capture),
        schema_ref=operation.old_schema,
        hints=(),
        processor_version=processor_version,
    )
    new_request = CompileRequest(
        capture=capture_from_wire(operation.capture),
        schema_ref=operation.new_schema,
        hints=(),
        processor_version=processor_version,
    )
    with cache.acquire(old_request) as old_schema, cache.acquire(new_request) as new_schema:
        require_complete_schema(old_schema)
        require_complete_schema(new_schema)
        old_description, new_description = describe_schema(old_schema), describe_schema(new_schema)
        old_keys = comparison_identity.comparison_keys(
            old_description, "/workspace/" + operation.old_schema.source.path
        )
        new_keys = comparison_identity.comparison_keys(
            new_description, "/workspace/" + operation.new_schema.source.path
        )
        old_prints = {
            old_keys[item.id]: _fingerprint(item, old_keys) for item in old_description.components
        }
        new_prints = {
            new_keys[item.id]: _fingerprint(item, new_keys) for item in new_description.components
        }
        added = sorted(set(new_prints) - set(old_prints))
        removed = sorted(set(old_prints) - set(new_prints))
        common = sorted(set(old_prints) & set(new_prints))
        changed = sorted(item for item in common if old_prints[item] != new_prints[item])
        old_by_key = {old_keys[item.id]: item for item in old_description.components}
        new_by_key = {new_keys[item.id]: item for item in new_description.components}
        changed_details: list[dict[str, JsonValue]] = []
        for identifier in changed[: cache.limits.page_items]:
            old_data = comparison_identity.semantic_dict(old_by_key[identifier], old_keys)
            new_data = comparison_identity.semantic_dict(new_by_key[identifier], new_keys)
            changes = [
                {
                    "field": field,
                    "old": encode_value(old_data[field]),
                    "new": encode_value(new_data[field]),
                }
                for field in sorted(old_data)
                if json.dumps(old_data[field], sort_keys=True, default=str)
                != json.dumps(new_data[field], sort_keys=True, default=str)
            ]
            changed_details.append({"id": identifier, "changes": cast(JsonValue, changes)})
        structural: dict[str, JsonValue] = {
            "added": encode_value(added),
            "removed": encode_value(removed),
            "changed": encode_value(changed),
            "changed_details": cast(JsonValue, changed_details),
            "changed_details_truncated": len(changed) > len(changed_details),
            "namespace_changed": old_description.target_namespace
            != new_description.target_namespace,
            "target_namespace": new_description.target_namespace,
            "totals": {
                "added": len(added),
                "removed": len(removed),
                "changed": len(changed),
            },
        }
        files: list[dict[str, JsonValue]] = []
        totals = dict.fromkeys(_CATEGORIES, 0)
        corpus_start = clock()
        for reference in operation.corpus:
            if clock() - corpus_start >= cache.limits.corpus_file_seconds:
                files.append(
                    {
                        "path": reference.path,
                        "status": "skipped",
                        "old": "skipped",
                        "new": "skipped",
                    }
                )
                totals["skipped"] += 1
                continue
            old_status, old_document = _classify(old_schema, reference.path)
            if old_status == "malformed":
                new_status, new_document = "malformed", None
            else:
                new_status, new_document = _classify(new_schema, reference.path)
            if old_status == "malformed" or new_status == "malformed":
                status = "malformed"
            elif old_status == "unbindable" and new_status == "unbindable":
                status = "unbindable"
            else:
                old_bad = old_status in {"invalid", "unbindable"}
                new_bad = new_status in {"invalid", "unbindable"}
                if not old_bad and new_bad:
                    status = "regressed"
                elif old_bad and not new_bad:
                    status = "resolved"
                elif old_bad:
                    status = "invalid"
                else:
                    status = "valid"
            entry: dict[str, JsonValue] = {
                "path": reference.path,
                "status": status,
                "old": old_status,
                "new": new_status,
            }
            if status == "regressed":
                entry["evidence"] = cast(
                    JsonValue, _evidence(cast(Document, new_document), cache.limits.page_items)
                )
            elif status == "resolved":
                entry["evidence"] = cast(
                    JsonValue, _evidence(cast(Document, old_document), cache.limits.page_items)
                )
            files.append(entry)
            totals[status] += 1
        corpus: dict[str, JsonValue] = {
            "files": cast(JsonValue, files),
            "totals": dict(totals),
            "regressions": [item["path"] for item in files if item["status"] == "regressed"],
            "resolutions": [item["path"] for item in files if item["status"] == "resolved"],
        }
        return {
            "structural": structural,
            "corpus": corpus,
            "conclusions": {
                "corpus_only": True,
                "statement": (
                    "Structural impact is potential, not observed; corpus evidence applies "
                    "only to the tested files and options. No universal compatibility "
                    "conclusion follows from a corpus sample."
                ),
            },
        }


class ComparisonService(ExplorationService):
    def __init__(self, inputs: SessionInputs, pool: WorkerPool) -> None:
        super().__init__(inputs, pool)
        self.limits = inputs.limits

    async def compare(self, request: CompareSchemasRequest) -> dict[str, JsonValue]:
        if len(request.corpus) > self.limits.corpus_files:
            raise ToolError("quota_exceeded", "Corpus exceeds the operator file budget")
        async with self._capture(
            request.workspace_id,
            (request.old_schema, request.new_schema, *request.corpus),
        ) as capture:
            operation = ComparisonOperation(
                request_id=secrets.token_hex(16),
                capture=capture_to_wire(capture),
                old_schema=request.old_schema,
                new_schema=request.new_schema,
                corpus=request.corpus,
            )
            deadline = time.monotonic() + self.limits.batch_seconds
            reply = await self.pool.run(operation, deadline=deadline)
        if reply.status == "error":
            assert reply.fault is not None
            raise ToolError(reply.fault.code, reply.fault.message)
        if not isinstance(reply.result, dict):
            raise ToolError("ipc_protocol", "Comparison returned no result object")
        return reply.result
