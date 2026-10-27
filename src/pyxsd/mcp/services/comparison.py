"""Schema comparison with corpus evidence, computed in clean workers."""

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import cast

from pydantic import JsonValue
from pyxsd.exceptions import PyXSDError
from pyxsd.introspection import ComponentDescription, describe_schema
from pyxsd.schema import Schema

from ..cache import CompileRequest, SchemaCache
from ..contracts import FileRef, JobRef, Nonempty, SchemaRef, WireModel
from ..encoding import encode_value
from ..errors import ToolError
from ..jobs import JobManager
from ..snapshot_store import SnapshotStore
from ..worker_protocol import ComparisonOperation, Operation, WorkerReply
from ..workers import WorkerPool, snapshot_from_wire, snapshot_to_wire
from ..workspaces import Principal
from .exploration import ExplorationService

_CATEGORIES = ("valid", "regressed", "resolved", "invalid", "malformed", "unbindable", "skipped")


class CompareSchemasRequest(WireModel):
    workspace_id: Nonempty
    old_schema: SchemaRef
    new_schema: SchemaRef
    corpus: tuple[FileRef, ...] = ()
    idempotency_key: Nonempty


def _fingerprint(component: ComponentDescription) -> str:
    return hashlib.sha256(
        json.dumps(asdict(component), sort_keys=True, default=str).encode()
    ).hexdigest()


def _validate(schema: Schema, path: str) -> str:
    """Classify one corpus file against one compiled schema."""
    try:
        document = schema.parse("/workspace/" + path)
    except PyXSDError:
        return "malformed"
    if document.root is None:
        return "unbindable"
    return "valid" if document.report.error_count == 0 else "invalid"


def execute_comparison(
    operation: ComparisonOperation,
    cache: SchemaCache,
    processor_version: str,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, JsonValue]:
    old_request = CompileRequest(
        snapshot=snapshot_from_wire(operation.snapshot),
        schema_ref=operation.old_schema,
        hints=(),
        processor_version=processor_version,
        owner_id=operation.owner_id,
    )
    new_request = CompileRequest(
        snapshot=snapshot_from_wire(operation.snapshot),
        schema_ref=operation.new_schema,
        hints=(),
        processor_version=processor_version,
        owner_id=operation.owner_id,
    )
    with cache.acquire(old_request) as old_schema, cache.acquire(new_request) as new_schema:
        old_description, new_description = describe_schema(old_schema), describe_schema(new_schema)
        old_components = {item.id: item for item in old_description.components}
        new_components = {item.id: item for item in new_description.components}
        added = sorted(set(new_components) - set(old_components))
        removed = sorted(set(old_components) - set(new_components))
        common = sorted(set(old_components) & set(new_components))
        changed = sorted(
            item
            for item in common
            if _fingerprint(old_components[item]) != _fingerprint(new_components[item])
        )
        changed_details: list[dict[str, JsonValue]] = []
        for identifier in changed[: cache.limits.page_items]:
            old_data = asdict(old_components[identifier])
            new_data = asdict(new_components[identifier])
            changed_details.append(
                {
                    "id": identifier,
                    "fields": cast(
                        list[JsonValue],
                        sorted(
                            key
                            for key in old_data
                            if json.dumps(old_data[key], sort_keys=True, default=str)
                            != json.dumps(new_data[key], sort_keys=True, default=str)
                        ),
                    ),
                }
            )
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
            old_status = _validate(old_schema, reference.path)
            new_status = (
                "malformed" if old_status == "malformed" else _validate(new_schema, reference.path)
            )
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
            files.append(
                {"path": reference.path, "status": status, "old": old_status, "new": new_status}
            )
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
    def __init__(
        self, snapshots: SnapshotStore, pool: WorkerPool, *, jobs: JobManager | None = None
    ) -> None:
        super().__init__(snapshots, pool)
        self.limits = snapshots.limits
        self.jobs = jobs if jobs is not None else JobManager(snapshots.workspaces, self)

    async def submit(self, principal: Principal, request: CompareSchemasRequest) -> JobRef:
        principal.require("pyxsd:execute", "pyxsd:read", "pyxsd:upload")
        if len(request.corpus) > self.limits.corpus_files:
            raise ToolError("quota_exceeded", "Corpus exceeds the operator file budget")
        revisions = {
            reference.revision
            for reference in (request.old_schema.source, request.new_schema.source, *request.corpus)
            if reference.revision is not None
        }
        if len(revisions) > 1:
            raise ToolError("stale_reference", "Comparison references must share a revision")
        revision = next(iter(revisions), None)
        async with self._snapshot(principal, request.workspace_id, revision) as snapshot:
            operation = ComparisonOperation(
                request_id=secrets.token_hex(16),
                snapshot=snapshot_to_wire(snapshot),
                owner_id=principal.owner_id,
                old_schema=request.old_schema,
                new_schema=request.new_schema,
                corpus=request.corpus,
            )
            return await self.jobs.submit(
                principal, request.workspace_id, operation, idempotency_key=request.idempotency_key
            )

    async def run(self, operation: Operation, *, deadline: float) -> WorkerReply:
        if not isinstance(operation, ComparisonOperation):
            raise ToolError("invalid_argument", "Unsupported comparison operation")
        return await self.pool.run(operation, deadline=deadline)
