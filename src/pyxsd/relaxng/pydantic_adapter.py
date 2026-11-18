"""Standalone Pydantic models for finite RELAX NG projection routes.

Models are built from the compiled :class:`~pyxsd.relaxng.projection.RouteContract`
graph before any instance XML exists.  A class belongs to one route, not to one
document path, so recursive routes reuse finite model classes and repeated
resolution returns the same class object.  Task 1 deliberately keeps scalar
annotations as ``Any`` placeholders; scalar interpretation and standalone
route validation are later phases.  The compiled route contract is retained on
each model and field so those later hooks do not change route or class identity.
"""

from __future__ import annotations

import re
from types import GenericAlias
from typing import Annotated, Any

from pyxsd.integrations import IntegrationError
from pyxsd.integrations.pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    _safe_name,
    create_model,
)
from pyxsd.namespaces import clark, local_name
from pyxsd.relaxng.contracts import ScalarContract
from pyxsd.relaxng.projection import Cardinality, ProjectionPlan, RouteContract
from pyxsd.relaxng.schema import RelaxNGSchema

__all__ = ["RelaxNGModelSet"]

#: One JSON Schema description of a schema-static scalar contract.
ContractMetadata = dict[str, Any]


def _contract_metadata(contract: ScalarContract) -> ContractMetadata:
    """Render one contract deterministically for field metadata.

    The complete public contract is retained, not just a display name, so the
    scalar phase can replace annotations without consulting path or evidence
    state that these models deliberately do not retain.
    """
    return {
        "kind": contract.kind,
        "library_uri": contract.library_uri,
        "type_name": contract.type_name,
        "parameters": [
            {"name": parameter.name, "value": parameter.value} for parameter in contract.parameters
        ],
        "restrictions": [
            {"kind": restriction.kind, "pattern_identity": restriction.pattern_identity}
            for restriction in contract.restrictions
        ],
        "pattern_identity": contract.pattern_identity,
    }


def _field_metadata(
    contracts: tuple[ScalarContract, ...],
    cardinality: Cardinality,
    *,
    child_route_id: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "x-pyxsd-contracts": [_contract_metadata(contract) for contract in contracts],
        "x-pyxsd-cardinality": {
            "minimum": cardinality.minimum,
            "maximum": cardinality.maximum,
        },
    }
    if child_route_id is not None:
        metadata["x-pyxsd-child-route"] = child_route_id
    return metadata


def _route_metadata(plan: ProjectionPlan, route: RouteContract) -> dict[str, Any]:
    start = route.route_id in plan.roots
    if start:
        validation = (
            "projected data for this start route; use RelaxNGDocument validation for XML validity"
        )
    else:
        validation = (
            "local route content only; ancestor and sibling constraints are not "
            "validated; use RelaxNGDocument validation for XML validity"
        )
    return {
        "x-pyxsd-route": route.route_id,
        "x-pyxsd-element": route.expanded_name,
        "x-pyxsd-scope": "start" if start else "local",
        "x-pyxsd-validation": validation,
    }


def _route_label(route: RouteContract) -> str:
    name = route.expanded_name if route.expanded_name is not None else "an open-name element"
    return f"{name} ({route.route_id})"


def _model_stem(route: RouteContract) -> str:
    """Return a deterministic class stem without a grammar or route digest."""
    local = "open_name" if route.expanded_name is None else route.expanded_name.rsplit("}", 1)[-1]
    return "RelaxNGModel_" + re.sub(r"[^0-9A-Za-z_]", "_", local)


def _model_names(plan: ProjectionPlan) -> dict[str, str]:
    used: set[str] = set()
    names: dict[str, str] = {}
    for route in plan.routes.values():
        base = _model_stem(route)
        name = base
        index = 2
        while name in used:
            name = f"{base}_{index}"
            index += 1
        used.add(name)
        names[route.route_id] = name
    return names


def _parse_name(value: object, *, what: str) -> tuple[str | None, str]:
    """Parse a local, Clark, or EQName spelling into an expanded component.

    Prefix-form EQNames need a namespace context that this API does not accept,
    so they are rejected rather than resolved against process state.
    """
    if not isinstance(value, str) or not value:
        raise IntegrationError(f"{what} must be a nonempty element name")
    if value.startswith(("Q{", "{")):
        body = value[2:] if value.startswith("Q{") else value[1:]
        uri, separator, local = body.partition("}")
        if not separator or not local or "{" in local or "}" in local:
            raise IntegrationError(f"{what} {value!r}: malformed Clark or EQName")
        return uri or None, local
    if ":" in value:
        raise IntegrationError(f"{what} {value!r}: prefix-form EQName requires a namespace context")
    if "}" in value:
        raise IntegrationError(f"{what} {value!r}: malformed element name")
    return None, value


def _field_info(
    alias: str,
    *,
    metadata: dict[str, Any],
    cardinality: Cardinality,
    whole: bool = False,
) -> Any:
    repeatable = cardinality.repeatable
    options: dict[str, Any] = {
        "alias": alias,
        "json_schema_extra": metadata,
    }
    if repeatable:
        options["min_length"] = cardinality.minimum
        options["max_length"] = cardinality.maximum
    if whole or cardinality.minimum > 0:
        options["default"] = ...
    elif repeatable:
        options["default_factory"] = list
    else:
        options["default"] = None
    return Field(**options)


class RelaxNGModelSet:
    """A compiled-RELAX-NG-schema-scoped registry of route Pydantic models.

    The registry is built from :attr:`RelaxNGSchema.projection` only.  It never
    reads instance XML, pattern capture, or binding evidence, so models exist
    before the first document is parsed and cannot inherit instance-specific
    route choices.
    """

    def __init__(self, schema: RelaxNGSchema) -> None:
        if not isinstance(schema, RelaxNGSchema):
            raise TypeError("RelaxNGModelSet requires a RelaxNGSchema")
        schema.require_valid()
        self.schema = schema
        self._plan = schema.projection
        self._names = _model_names(self._plan)
        self._models: dict[str, type[BaseModel]] = {}

    def model_for(self, *, element: str, path: tuple[str, ...] = ()) -> type[BaseModel]:
        """Return the one cached model class for an admitted route.

        ``element`` accepts a local name, a Clark name (``{uri}local``), or an
        EQName (``Q{uri}local``).  Each ``path`` step is an exact expanded child
        name, or one uniquely matching child wildcard.  Unknown and ambiguous
        resolutions fail with :class:`~pyxsd.integrations.IntegrationError`.
        """
        route = self._resolve(element=element, path=path)
        return self._model(route)

    def _resolve(self, *, element: str, path: tuple[str, ...]) -> RouteContract:
        if not isinstance(path, tuple) or any(
            not isinstance(step, str) or not step for step in path
        ):
            raise IntegrationError("model route path must be a tuple of nonempty element names")
        uri, local = _parse_name(element, what="element")
        route = self._start_route(element, uri, local)
        for index in range(len(path)):
            route = self._child_route(route, element=element, path=path[: index + 1])
        return route

    def _start_route(self, element: str, uri: str | None, local: str) -> RouteContract:
        matches = [
            route_id
            for route_id in self._plan.roots
            if self._plan.routes[route_id].name_class.contains(uri, local)
        ]
        if not matches and uri is None:
            # A local spelling may identify a uniquely named start route in a
            # namespace.  It never resolves a wildcard route: those admit names,
            # not local-name identities.
            matches = [
                route_id
                for route_id in self._plan.roots
                if (expanded := self._plan.routes[route_id].expanded_name) is not None
                and local_name(expanded) == local
            ]
        if not matches:
            raise IntegrationError(f"RELAX NG start route {element!r}: no admitted start route")
        if len(matches) > 1:
            raise IntegrationError(
                f"RELAX NG start route {element!r}: ambiguous; use a Clark or "
                "EQName spelling with the namespace"
            )
        return self._plan.routes[matches[0]]

    def _child_route(
        self, route: RouteContract, *, element: str, path: tuple[str, ...]
    ) -> RouteContract:
        step = path[-1]
        uri, local = _parse_name(step, what="path step")
        key = clark(uri, local)
        field = route.children.get(key)
        if field is not None:
            assert field.child_route_id is not None
            return self._plan.routes[field.child_route_id]
        for wildcard in route.wildcards:
            if wildcard.role == "child" and wildcard.name_class.contains(uri, local):
                assert wildcard.child_route_id is not None
                return self._plan.routes[wildcard.child_route_id]
        raise IntegrationError(
            f"RELAX NG route {(element, *path)}: child {step!r} is not admitted by "
            f"{_route_label(route)}"
        )

    def _check_route(self, route: RouteContract) -> None:
        if route.generic_reason is not None:
            raise IntegrationError(
                f"RELAX NG route {_route_label(route)} is generic: "
                f"{route.generic_reason}; Pydantic fields cannot represent this "
                "content without losing required XML information"
            )
        if route.content.mixed:
            raise IntegrationError(
                f"RELAX NG route {_route_label(route)} has mixed content; Pydantic "
                "record fields cannot preserve text and child order"
            )
        if route.wildcards:
            roles = " and ".join(sorted({wildcard.role for wildcard in route.wildcards}))
            raise IntegrationError(
                f"RELAX NG route {_route_label(route)} has {roles} wildcard content; "
                "arbitrary XML names cannot be represented as fixed Pydantic fields "
                "without losing required XML structure"
            )

    def _reachable(self, route: RouteContract) -> list[str]:
        pending = [route]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current.route_id in seen:
                continue
            seen.add(current.route_id)
            self._check_route(current)
            for name in sorted(current.children):
                child = current.children[name]
                assert child.child_route_id is not None
                pending.append(self._plan.routes[child.child_route_id])
        return [route_id for route_id in self._plan.routes if route_id in seen]

    def _model(self, route: RouteContract) -> type[BaseModel]:
        cached = self._models.get(route.route_id)
        if cached is not None:
            return cached
        reachable = self._reachable(route)
        new = {
            route_id: self._build(self._plan.routes[route_id])
            for route_id in reachable
            if route_id not in self._models
        }
        namespace = {model.__name__: model for model in self._models.values()}
        namespace.update({model.__name__: model for model in new.values()})
        for model in new.values():
            model.model_rebuild(_types_namespace=namespace, raise_errors=True)
        self._models.update(new)
        return self._models[route.route_id]

    def _build(self, route: RouteContract) -> type[BaseModel]:
        if route.content.scalar_contracts and not route.attributes and not route.children:
            return self._root_model(route)
        return self._record_model(route)

    def _root_model(self, route: RouteContract) -> type[BaseModel]:
        metadata = _route_metadata(self._plan, route)
        metadata["x-pyxsd-contracts"] = [
            _contract_metadata(contract) for contract in route.content.scalar_contracts
        ]
        metadata["x-pyxsd-cardinality"] = {"minimum": 1, "maximum": 1}
        annotation = Annotated[Any, Field(json_schema_extra=metadata)]
        return create_model(
            self._names[route.route_id],
            __base__=RootModel[annotation],
            __module__=__name__,
        )

    def _record_model(self, route: RouteContract) -> type[BaseModel]:
        route_metadata = _route_metadata(self._plan, route)
        child_aliases = set(route.children)
        field_extras: dict[str, dict[str, Any]] = {}
        fields: dict[str, Any] = {}
        used: set[str] = {"xml_contract"}

        def add(
            alias: str,
            annotation: Any,
            *,
            cardinality: Cardinality,
            contracts: tuple[ScalarContract, ...],
            child_route_id: str | None = None,
            whole: bool = False,
        ) -> None:
            field_name = _safe_name(alias, used | (child_aliases - {alias}))
            used.add(field_name)
            field_metadata = _field_metadata(
                contracts,
                cardinality,
                child_route_id=child_route_id,
            )
            field_extras[alias] = field_metadata
            fields[field_name] = (
                annotation,
                _field_info(
                    alias,
                    metadata=field_metadata,
                    cardinality=cardinality,
                    whole=whole,
                ),
            )

        if route.content.scalar_contracts:
            add(
                "$",
                Any,
                cardinality=Cardinality(1, 1),
                contracts=route.content.scalar_contracts,
                whole=True,
            )
        for name in sorted(route.attributes):
            field = route.attributes[name]
            add(
                "@" + field.expanded_name,
                Any,
                cardinality=field.cardinality,
                contracts=field.scalar_contracts,
            )
        for name in sorted(route.children):
            field = route.children[name]
            assert field.child_route_id is not None
            child_name = self._names[field.child_route_id]
            annotation: Any = (
                GenericAlias(list, child_name) if field.cardinality.repeatable else child_name
            )
            add(
                field.expanded_name,
                annotation,
                cardinality=field.cardinality,
                contracts=field.scalar_contracts,
                child_route_id=field.child_route_id,
            )

        def model_schema(description: dict[str, Any]) -> None:
            description.update(route_metadata)
            properties = description.get("properties", {})
            for alias, extra in field_extras.items():
                properties[alias].update(extra)

        return create_model(
            self._names[route.route_id],
            __config__=ConfigDict(
                extra="forbid",
                strict=True,
                validate_by_name=True,
                json_schema_extra=model_schema,
            ),
            __module__=__name__,
            **fields,
        )
