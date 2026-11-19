"""Schema comparison with corpus evidence, computed in clean workers."""

import json
import secrets
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from typing import cast
from xml.etree import ElementTree as ET

from pydantic import JsonValue
from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.introspection import ComponentDescription, describe_schema
from pyxsd.relaxng import RelaxNGSchema
from pyxsd.relaxng.comparison import compare_relaxng_structure
from pyxsd.relaxng.datatypes import DatatypeRegistrationError, UnsupportedDatatypeError
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.schema import Schema
from pyxsd.xml_policy import XmlTreeLimitError

from ..cache import CompiledSchema, CompileRequest, SchemaCache, require_complete_schema
from ..captures import InputPolicyError
from ..contracts import FileRef, Nonempty, SchemaRef, WireModel
from ..encoding import encode_value
from ..errors import ToolError
from ..inputs import SessionInputs
from ..worker_protocol import ComparisonOperation
from ..workers import WorkerPool, capture_from_wire, capture_to_wire
from . import comparison_identity
from .exploration import ExplorationService

_CATEGORIES = (
    "valid",
    "regressed",
    "resolved",
    "invalid",
    "malformed",
    "unbindable",
    "skipped",
    "unknown",
)

_CORPUS_CONCLUSION = (
    "Structural impact is potential, not observed; corpus evidence applies "
    "only to the tested files and options. No universal compatibility "
    "conclusion follows from a corpus sample."
)

_STRUCTURAL_CONCLUSION = (
    "No corpus documents were selected; only structural output was produced. "
    "No compatibility conclusion or observed corpus evidence was computed."
)


class CompareSchemasRequest(WireModel):
    workspace_id: Nonempty
    old_schema: SchemaRef
    new_schema: SchemaRef
    corpus: tuple[FileRef, ...] = ()
    corpus_glob: Nonempty | None = None


@dataclass(frozen=True)
class _Classification:
    """One schema's corpus verdict, or the operational reason it has none."""

    status: str
    document: Document | None = None
    fault: dict[str, JsonValue] | None = None


def _fingerprint(component: ComponentDescription, keys: dict[str, str]) -> str:
    return comparison_identity.fingerprint(component, keys)


def _bounded_message(exc: BaseException, message_bytes: int | None) -> str:
    message = str(exc)
    return message if message_bytes is None else message[:message_bytes]


def _unknown_fault(exc: PyXSDError, message_bytes: int | None) -> dict[str, JsonValue]:
    """Name an operational outcome without turning it into a document verdict."""
    details: dict[str, JsonValue] = {}
    if isinstance(exc, RelaxNGLimitError):
        code = "limit_exceeded"
        details = {
            "stage": exc.stage,
            "budget": exc.budget,
            "configured": exc.configured,
            "observed": exc.observed,
        }
    elif isinstance(exc, (UnsupportedDatatypeError, DatatypeRegistrationError)):
        code = "unsupported_capability"
    elif isinstance(exc, ValidationError):
        code = "invalid_schema"
    elif isinstance(exc, XmlTreeLimitError):
        code = "limit_exceeded"
        details = {"code": "xml_limit"}
    elif isinstance(exc, InputPolicyError):
        code = "limit_exceeded" if exc.code in ("schema_limit", "xml_limit") else exc.code
        details = {"code": exc.code}
    else:
        code = "unknown"
    return {
        "code": code,
        "message": _bounded_message(exc, message_bytes),
        "details": details,
    }


def _source_failure(exc: BaseException) -> bool:
    """Whether an exception chain ends in an unreadable or malformed source."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, (OSError, ET.ParseError)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _classify(
    schema: CompiledSchema, path: str, *, message_bytes: int | None = None
) -> _Classification:
    """Classify one corpus file through a schema's public, language-native parse.

    Only a returned document report decides a validity verdict. Malformed input
    is a decided source failure. Every other exception (budgets, unsupported
    datatypes, unavailable schemas, resource policy outcomes) has no verdict and
    is reported as ``unknown`` with its operational fault.
    """
    try:
        document = (
            schema.parse("/workspace/" + path, typed=False)
            if isinstance(schema, RelaxNGSchema)
            else schema.parse("/workspace/" + path)
        )
    except PyXSDError as exc:
        if isinstance(exc, InputPolicyError) or not _source_failure(exc):
            return _Classification("unknown", fault=_unknown_fault(exc, message_bytes))
        return _Classification("malformed")
    if document.root is None:
        return _Classification("unbindable", document)
    return _Classification("valid" if document.report.error_count == 0 else "invalid", document)


def _evidence(document: Document, limit: int | None) -> list[dict[str, JsonValue]]:
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


def _xsd_structural(
    old_schema: Schema,
    new_schema: Schema,
    operation: ComparisonOperation,
    cache: SchemaCache,
) -> dict[str, JsonValue]:
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
    return {
        "added": encode_value(added),
        "removed": encode_value(removed),
        "changed": encode_value(changed),
        "changed_details": cast(JsonValue, changed_details),
        "changed_details_truncated": len(changed) > len(changed_details),
        "namespace_changed": old_description.target_namespace != new_description.target_namespace,
        "target_namespace": new_description.target_namespace,
        "totals": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
        },
    }


def _structural(
    old_schema: CompiledSchema,
    new_schema: CompiledSchema,
    operation: ComparisonOperation,
    cache: SchemaCache,
) -> dict[str, JsonValue]:
    if isinstance(old_schema, Schema) and isinstance(new_schema, Schema):
        return _xsd_structural(old_schema, new_schema, operation, cache)
    if isinstance(old_schema, RelaxNGSchema) and isinstance(new_schema, RelaxNGSchema):
        return compare_relaxng_structure(
            old_schema, new_schema, detail_limit=cache.limits.page_items
        )
    return {
        "status": "not_comparable",
        "old_format": operation.old_schema.format,
        "new_format": operation.new_schema.format,
        "statement": "Structural comparison requires schemas in the same language.",
    }


def _file_status(old_status: str, new_status: str) -> str:
    """Combine two schema-side outcomes without deciding an unknown one."""
    if old_status == "unknown" or new_status == "unknown":
        return "unknown"
    if old_status == "malformed" or new_status == "malformed":
        return "malformed"
    if old_status == "unbindable" and new_status == "unbindable":
        return "unbindable"
    old_bad = old_status in {"invalid", "unbindable"}
    new_bad = new_status in {"invalid", "unbindable"}
    if not old_bad and new_bad:
        return "regressed"
    if old_bad and not new_bad:
        return "resolved"
    if old_bad:
        return "invalid"
    return "valid"


def _corpus_section(
    old_schema: CompiledSchema,
    new_schema: CompiledSchema,
    operation: ComparisonOperation,
    cache: SchemaCache,
    clock: Callable[[], float],
) -> dict[str, JsonValue]:
    files: list[dict[str, JsonValue]] = []
    totals = dict.fromkeys(_CATEGORIES, 0)
    corpus_start = clock()
    for reference in operation.corpus:
        if (
            cache.limits.corpus_file_seconds is not None
            and clock() - corpus_start >= cache.limits.corpus_file_seconds
        ):
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
        old = _classify(old_schema, reference.path, message_bytes=cache.limits.message_bytes)
        if old.status == "malformed":
            # Well-formedness is a property of the shared source, not a schema.
            new = _Classification("malformed")
        else:
            new = _classify(new_schema, reference.path, message_bytes=cache.limits.message_bytes)
        status = _file_status(old.status, new.status)
        entry: dict[str, JsonValue] = {
            "path": reference.path,
            "status": status,
            "old": old.status,
            "new": new.status,
        }
        faults: dict[str, JsonValue] = {}
        if old.fault is not None:
            faults["old"] = cast(JsonValue, old.fault)
        if new.fault is not None:
            faults["new"] = cast(JsonValue, new.fault)
        if faults:
            entry["faults"] = cast(JsonValue, faults)
        if status == "regressed":
            entry["evidence"] = cast(
                JsonValue, _evidence(cast(Document, new.document), cache.limits.page_items)
            )
        elif status == "resolved":
            entry["evidence"] = cast(
                JsonValue, _evidence(cast(Document, old.document), cache.limits.page_items)
            )
        files.append(entry)
        totals[status] += 1
    corpus: dict[str, JsonValue] = {
        "status": "completed" if files else "empty",
        "files": cast(JsonValue, files),
        "totals": dict(totals),
        "regressions": [item["path"] for item in files if item["status"] == "regressed"],
        "resolutions": [item["path"] for item in files if item["status"] == "resolved"],
        "unknowns": [item["path"] for item in files if item["status"] == "unknown"],
    }
    if totals["unknown"]:
        corpus["statement"] = (
            "Operational failures left some corpus documents without a verdict; "
            "those files are not compatibility evidence."
        )
    elif not files:
        corpus["statement"] = "No corpus documents were selected; no compatibility claim is made."
    return corpus


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
    with ExitStack() as leases:
        old_schema = leases.enter_context(cache.acquire(old_request))
        new_schema = (
            old_schema
            if operation.old_schema == operation.new_schema
            else leases.enter_context(cache.acquire(new_request))
        )
        require_complete_schema(old_schema)
        require_complete_schema(new_schema)
        structural = _structural(old_schema, new_schema, operation, cache)
        if not operation.corpus and not operation.corpus_requested:
            return {
                "structural": structural,
                "conclusions": {
                    "corpus_only": False,
                    "structural_only": True,
                    "statement": _STRUCTURAL_CONCLUSION,
                },
            }
        corpus = _corpus_section(old_schema, new_schema, operation, cache, clock)
        return {
            "structural": structural,
            "corpus": corpus,
            "conclusions": {
                "corpus_only": True,
                "statement": _CORPUS_CONCLUSION,
            },
        }


class ComparisonService(ExplorationService):
    def __init__(self, inputs: SessionInputs, pool: WorkerPool) -> None:
        super().__init__(inputs, pool)
        self.limits = inputs.limits

    async def compare(self, request: CompareSchemasRequest) -> dict[str, JsonValue]:
        if request.corpus and request.corpus_glob is not None:
            raise ToolError("invalid_argument", "Provide either corpus or corpus_glob, not both")
        corpus = request.corpus
        selection: dict[str, JsonValue] | None = None
        if request.corpus_glob is not None:
            corpus = await self.inputs.select_corpus(request.workspace_id, request.corpus_glob)
            selection = {"glob": request.corpus_glob, "matched": len(corpus)}
        if self.limits.corpus_files is not None and len(corpus) > self.limits.corpus_files:
            raise ToolError("quota_exceeded", "Corpus exceeds the operator file budget")
        corpus_requested = bool(corpus) or request.corpus_glob is not None
        async with self._capture(
            request.workspace_id,
            (request.old_schema, request.new_schema, *corpus),
        ) as capture:
            operation = ComparisonOperation(
                request_id=secrets.token_hex(16),
                capture=capture_to_wire(capture),
                old_schema=request.old_schema,
                new_schema=request.new_schema,
                corpus=corpus,
                corpus_requested=corpus_requested,
            )
            deadline = (
                None
                if self.limits.batch_seconds is None
                else time.monotonic() + self.limits.batch_seconds
            )
            reply = await self.pool.run(operation, deadline=deadline)
        if reply.status == "error":
            assert reply.fault is not None
            raise ToolError(
                reply.fault.code,
                reply.fault.message,
                retryable=reply.fault.retryable,
                details=reply.fault.details,
            )
        if not isinstance(reply.result, dict):
            raise ToolError("ipc_protocol", "Comparison returned no result object")
        return {**reply.result, "corpus_selection": selection}
