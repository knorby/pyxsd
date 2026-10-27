"""Worker-local projected contracts; these do not establish XML validity."""

import secrets
import time
from typing import Any, cast

from pydantic import JsonValue, ValidationError

from ..cache import CompileRequest, SchemaCache
from ..contracts import Nonempty, SchemaRef, ToolEnvelope, WireModel
from ..encoding import encode_value
from ..errors import ToolError
from ..worker_protocol import ModelOperation
from ..workers import snapshot_from_wire, snapshot_to_wire
from ..workspaces import Principal
from .exploration import ExplorationService

_WARNING = (
    "Projected-data validation is not full XSD/XML validity; model changes do not update XML."
)


class InspectModelRequest(WireModel):
    workspace_id: Nonempty
    schema_ref: SchemaRef
    element: Nonempty
    path: tuple[Nonempty, ...] = ()


class ValidateDataRequest(InspectModelRequest):
    data: JsonValue


def execute_model(
    operation: ModelOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations.pydantic import models

    request = CompileRequest(
        snapshot=snapshot_from_wire(operation.snapshot),
        schema_ref=operation.schema_ref,
        hints=(),
        processor_version=processor_version,
        owner_id=operation.owner_id,
    )
    try:
        with cache.acquire(request) as schema:
            # The registry dies with its parent schema's cache entry, never a
            # global model cache or a coordinator-side generated class.
            registry = getattr(schema, "_mcp_projected_models", None)
            if registry is None:
                registry = models(schema)
                cast(Any, schema)._mcp_projected_models = registry
            model = registry.model_for(element=operation.element, path=operation.path)
            if operation.action == "inspect":
                return {
                    "json_schema": encode_value(model.model_json_schema(by_alias=True)),
                    "validation_scope": "projected-data",
                }
            try:
                value = model.model_validate(operation.data)
            except ValidationError as exc:
                errors = exc.errors(include_url=False, include_context=False, include_input=False)
                retained = errors[: cache.limits.collected_issues]
                return {
                    "valid": False,
                    "validation_scope": "projected-data",
                    "error_count": len(errors),
                    "suppressed_count": len(errors) - len(retained),
                    "errors": [
                        {
                            "location": list(error["loc"]),
                            "type": error["type"],
                            "message": error["msg"],
                        }
                        for error in retained
                    ],
                }
            return {
                "valid": True,
                "validation_scope": "projected-data",
                "value": encode_value(value.model_dump(by_alias=True, exclude_unset=True)),
            }
    except IntegrationError as exc:
        raise ToolError("unsupported_projection", str(exc)[: cache.limits.message_bytes]) from exc


class ModelService(ExplorationService):
    async def inspect_model(
        self, principal: Principal, request: InspectModelRequest
    ) -> ToolEnvelope:
        return await self._model(principal, request)

    async def validate_data(
        self, principal: Principal, request: ValidateDataRequest
    ) -> ToolEnvelope:
        return await self._model(principal, request)

    async def _model(
        self, principal: Principal, request: InspectModelRequest | ValidateDataRequest
    ) -> ToolEnvelope:
        async with self._snapshot(
            principal, request.workspace_id, request.schema_ref.source.revision
        ) as snapshot:
            operation = ModelOperation(
                request_id=secrets.token_hex(16),
                snapshot=snapshot_to_wire(snapshot),
                owner_id=principal.owner_id,
                schema_ref=request.schema_ref,
                element=request.element,
                path=request.path,
                action="validate" if isinstance(request, ValidateDataRequest) else "inspect",
                data=request.data if isinstance(request, ValidateDataRequest) else None,
            )
            reply = await self.pool.run(
                operation, deadline=time.monotonic() + self.pool.limits.compile_query_seconds
            )
            envelope = self._envelope(
                principal,
                reply,
                provenance=self._provenance(
                    snapshot, request.schema_ref, request.schema_ref.source.path
                ),
                snapshot=snapshot,
                query_digest=self._digest(request),
            )
            return envelope.model_copy(update={"warnings": [_WARNING]})
