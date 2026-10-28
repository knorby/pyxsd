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
from ..workers import capture_from_wire, capture_to_wire
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
        capture=capture_from_wire(operation.capture),
        schema_ref=operation.schema_ref,
        hints=(),
        processor_version=processor_version,
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
    async def inspect_model(self, request: InspectModelRequest) -> ToolEnvelope:
        return await self._model(request)

    async def validate_data(self, request: ValidateDataRequest) -> ToolEnvelope:
        return await self._model(request)

    async def _model(self, request: InspectModelRequest | ValidateDataRequest) -> ToolEnvelope:
        async with self._capture(request.workspace_id) as capture:
            result_id = self._result_id(request, capture)
            operation = ModelOperation(
                request_id=secrets.token_hex(16),
                capture=capture_to_wire(capture),
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
                reply,
                provenance=self._provenance(
                    capture, request.schema_ref, request.schema_ref.source.path
                ),
                result_id=result_id,
            )
            return envelope.model_copy(update={"warnings": [_WARNING]})
