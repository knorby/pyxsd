"""Worker-local projected contracts; these do not establish XML validity."""

import secrets
import time
from typing import Any, cast

from pydantic import JsonValue, ValidationError

from ..cache import CompileRequest, SchemaCache, require_complete_schema
from ..contracts import Nonempty, SchemaRef, ToolEnvelope, WireModel
from ..encoding import encode_value
from ..errors import ToolError
from ..projection_guidance import candidate_details, resolve_projection_element
from ..worker_protocol import ModelOperation
from ..workers import capture_from_wire, capture_to_wire
from .exploration import ExplorationService

_WARNING = (
    "Projected-data validation is not full XSD/XML validity; model changes do not update XML."
)
_RELAXNG_WARNING = (
    "RELAX NG route-model validation covers only the selected start or local route; "
    "it is not whole-XML validity, and model changes do not update XML."
)


class InspectModelRequest(WireModel):
    workspace_id: Nonempty
    schema_ref: SchemaRef
    element: Nonempty
    path: tuple[Nonempty, ...] = ()


class ValidateDataRequest(InspectModelRequest):
    data: JsonValue


def _model_selection(schema: Any, element: str) -> tuple[str, Any]:
    """Map one user spelling to the model registry's accepted route identity.

    XSD keeps the existing Clark-declaration path.  RELAX NG standalone models
    start at a start route; a local route ID is a truthful, explicit limitation
    until selector round trips are available, so it names the start routes that
    can still be inspected with a ``path``.
    """
    from pyxsd.relaxng import RelaxNGSchema

    resolved = resolve_projection_element(schema, element)
    if not isinstance(schema, RelaxNGSchema):
        return resolved, None
    plan = schema.projection
    route = plan.routes[resolved]
    if resolved not in plan.roots:
        raise ToolError(
            "unsupported_projection",
            "RELAX NG standalone model selection requires a start route; "
            "select a start route and pass local steps in path",
            details={
                "format": "relax-ng",
                "route_id": resolved,
                "scope": "local",
                "candidates": cast(JsonValue, sorted(plan.roots)),
            },
        )
    if route.expanded_name is not None:
        return route.expanded_name, route
    if element in plan.routes:
        raise ToolError(
            "unsupported_projection",
            "RELAX NG open-name start routes require an explicit element spelling",
            details={
                "format": "relax-ng",
                "route_id": resolved,
                "scope": "start",
                "candidates": cast(JsonValue, sorted(plan.roots)),
            },
        )
    return element, route


def _route_scope(model: Any) -> dict[str, JsonValue]:
    """Describe the selected RELAX NG route; XSD keeps its projected-data scope."""
    route = getattr(model, "__relaxng_route__", None)
    if route is None:
        return {}
    scope = getattr(model, "__relaxng_scope__", None)
    return {
        "route_scope": scope,
        "route_id": getattr(model, "__relaxng_route_id__", None),
        "route_element": getattr(model, "__relaxng_element__", None),
        "whole_xml_validity": False,
    }


def execute_model(
    operation: ModelOperation, cache: SchemaCache, processor_version: str
) -> dict[str, JsonValue]:
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations.pydantic import models
    from pyxsd.relaxng import RelaxNGSchema

    request = CompileRequest(
        capture=capture_from_wire(operation.capture),
        schema_ref=operation.schema_ref,
        hints=(),
        processor_version=processor_version,
    )
    with cache.acquire(request) as schema:
        require_complete_schema(schema)
        if isinstance(schema, RelaxNGSchema):
            # Route registries are cheap and schema-scoped; the frozen RELAX NG
            # schema owns no mutable registry slot, so build one per request.
            registry: Any = models(schema)
        else:
            # The registry dies with its parent schema's cache entry, never a
            # global model cache or a coordinator-side generated class.
            registry = getattr(schema, "_mcp_projected_models", None)
            if registry is None:
                registry = models(schema)
                cast(Any, schema)._mcp_projected_models = registry
        element, _route = _model_selection(schema, operation.element)
        try:
            model = registry.model_for(element=element, path=operation.path)
        except IntegrationError as exc:
            details = candidate_details(
                schema,
                element=element,
                path=operation.path,
                prepare=lambda candidate_element, candidate_path: registry.model_for(
                    element=candidate_element, path=candidate_path
                ),
                limits=cache.limits,
            )
            raise ToolError(
                "unsupported_projection",
                str(exc)[: cache.limits.message_bytes],
                details=details,
            ) from exc
        scope = _route_scope(model)
        if operation.action == "inspect":
            return {
                "json_schema": encode_value(model.model_json_schema(by_alias=True)),
                "validation_scope": "projected-data",
                **scope,
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
                **scope,
            }
        return {
            "valid": True,
            "validation_scope": "projected-data",
            "value": encode_value(value.model_dump(by_alias=True, exclude_unset=True)),
            **scope,
        }


class ModelService(ExplorationService):
    async def inspect_model(self, request: InspectModelRequest) -> ToolEnvelope:
        return await self._model(request)

    async def validate_data(self, request: ValidateDataRequest) -> ToolEnvelope:
        return await self._model(request)

    async def _model(self, request: InspectModelRequest | ValidateDataRequest) -> ToolEnvelope:
        async with self._capture(request.workspace_id, (request.schema_ref,)) as capture:
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
                operation,
                deadline=(
                    None
                    if self.pool.limits.compile_query_seconds is None
                    else time.monotonic() + self.pool.limits.compile_query_seconds
                ),
            )
            envelope = self._envelope(
                reply,
                provenance=self._provenance(
                    capture, request.schema_ref, request.schema_ref.source.path
                ),
                result_id=result_id,
            )
            warning = _RELAXNG_WARNING if request.schema_ref.format == "relax-ng" else _WARNING
            return envelope.model_copy(update={"warnings": [warning]})
