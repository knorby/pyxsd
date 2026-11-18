"""Standalone Pydantic models for finite RELAX NG projection routes.

Models are built from the compiled :class:`~pyxsd.relaxng.projection.RouteContract`
graph before any instance XML exists.  A class belongs to one route, not to one
document path, so recursive routes reuse finite model classes and repeated
resolution returns the same class object.

Pydantic record input is an unordered data projection.  Every generated class
retains its compiled ``RouteContract`` and a truthful layout value as class
metadata through ``__relaxng_route__``, ``__relaxng_route_id__``,
``__relaxng_layout__``, ``__relaxng_scope__``, ``__relaxng_start__``,
``__relaxng_element__``, and ``__relaxng_input_order__``.  The model JSON
Schema repeats the route ID, layout, scope, and unordered-input statement.
Task 3 and Task 4 use that retained route, not field order or insertion order,
to reconstruct canonical lexical XML.  Root, ``$``, and attribute scalar
annotations come from :mod:`pyxsd.relaxng.pydantic_values`: unique decodable
contracts use ordinary Python values, while ambiguous or nondecodable
positions use codec-checked alternatives and scoped datatype context.  A
nonempty content contract still requires a supplied value, while an
empty-capable scalar uses ``None`` for real empty content and never invents an
empty lexical string.  Full standalone route validation is a later phase.
"""

from __future__ import annotations

import re
from types import GenericAlias
from typing import Any, cast

from pyxsd.integrations import IntegrationError
from pyxsd.integrations.pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    _safe_name,
    clean_field_schema,
    create_model,
    model_validator,
)
from pyxsd.namespaces import clark, local_name
from pyxsd.relaxng.contracts import ScalarContract
from pyxsd.relaxng.projection import (
    Cardinality,
    ContentContract,
    ProjectionPlan,
    RouteContract,
)
from pyxsd.relaxng.pydantic_values import (
    ScalarCodec,
    model_dump_json_with_scalar_wrappers,
    model_dump_with_scalar_wrappers,
    relaxng_scalar_annotation,
)
from pyxsd.relaxng.schema import RelaxNGSchema

__all__ = ["RelaxNGModelSet"]

#: Validator names start with an underscore, which ``_safe_name`` never emits.
_CHILD_PRESENCE_VALIDATOR = "__pyxsd_relaxng_child_presence__"

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


def _route_metadata(plan: ProjectionPlan, route: RouteContract, layout: str) -> dict[str, Any]:
    start = route.route_id in plan.roots
    if start:
        validation = (
            "unordered projected data for this start route; Task 3/4 reconstruct "
            "canonical lexical XML from the retained compiled route; use "
            "RelaxNGDocument validation for XML validity"
        )
    else:
        validation = (
            "unordered local route content only; ancestor and sibling constraints "
            "are not validated; Task 3/4 reconstruct canonical lexical XML from the "
            "retained compiled route; use RelaxNGDocument validation for XML validity"
        )
    return {
        "x-pyxsd-route": route.route_id,
        "x-pyxsd-layout": layout,
        "x-pyxsd-element": route.expanded_name,
        "x-pyxsd-scope": "start" if start else "local",
        "x-pyxsd-input-order": "unordered",
        "x-pyxsd-validation": validation,
    }


def _route_label(route: RouteContract) -> str:
    name = route.expanded_name if route.expanded_name is not None else "an open-name element"
    return f"{name} ({route.route_id})"


def _child_content(model: ContentContract) -> ContentContract | None:
    """Erase attribute-only content and collapse one-operand ordering nodes."""
    results: dict[int, ContentContract | None] = {}
    pending: list[tuple[ContentContract, bool]] = [(model, False)]
    while pending:
        node, ready = pending.pop()
        if id(node) in results:
            continue
        if not ready:
            pending.append((node, True))
            for operand in node.operands:
                if id(operand) not in results:
                    pending.append((operand, False))
            continue
        if node.kind in ("attribute", "empty"):
            results[id(node)] = None
            continue
        operands = tuple(
            part for operand in node.operands if (part := results[id(operand)]) is not None
        )
        if node.kind in ("group", "interleave") and len(operands) == 1:
            result = operands[0]
        elif node.operands and not operands:
            result = None
        else:
            result = ContentContract(node.kind, operands)
        results[id(node)] = result
    return results[id(model)]


def _content_cannot_match(model: ContentContract) -> bool:
    """Whether the whole content model is impossible (``notAllowed``)."""
    results: dict[int, bool] = {}
    pending: list[tuple[ContentContract, bool]] = [(model, False)]
    while pending:
        node, ready = pending.pop()
        if id(node) in results:
            continue
        if not ready:
            pending.append((node, True))
            for operand in node.operands:
                if id(operand) not in results:
                    pending.append((operand, False))
            continue
        if node.kind == "notAllowed":
            value = True
        elif node.kind in ("empty", "attribute", "child", "element", "scalar"):
            value = False
        elif node.kind in ("group", "interleave"):
            value = any(results[id(operand)] for operand in node.operands)
        elif node.kind in ("choice", "repeat"):
            value = all(results[id(operand)] for operand in node.operands)
        else:
            value = False
        results[id(node)] = value
    return results[id(model)]


def _flat_interleave(model: ContentContract) -> bool:
    """Whether a content tree is only flat singular child structure.

    Singular optional choices (``empty`` or one child) and nested interleaves
    remain order-immaterial.  Groups and repeats introduce order or multiplicity
    and therefore are not flat.
    """
    if model.kind in ("child", "empty"):
        return True
    if model.kind == "choice":
        return all(_flat_interleave(operand) for operand in model.operands)
    if model.kind != "interleave":
        return False
    return all(_flat_interleave(operand) for operand in model.operands)


def _route_layout(route: RouteContract) -> str:
    """Describe the retained child layout without reading instance content.

    ``record`` is an interleave whose operands are only flat singular child
    elements, whose relative order is immaterial.  ``interleaved`` is an
    interleave containing a repeatable child or nested ordered/choice
    structure, where canonical reconstruction still needs the compiled order.
    ``ordered`` is a group/sequence.  Input through Pydantic is always
    unordered; this value only records what the retained route requires.
    """
    if _content_cannot_match(route.content.model):
        return "generic"
    if route.content.scalar_contracts:
        return "scalar"
    if route.generic_reason is not None or route.wildcards or route.content.mixed:
        return "generic"
    model = _child_content(route.content.model)
    if model is None:
        return "empty"
    children = tuple(route.children.values())
    if len(children) == 1 and children[0].cardinality.repeatable:
        return "repeated"
    if model.kind == "choice":
        return "choice"
    if model.kind == "repeat":
        return "repeated"
    if model.kind == "interleave":
        if _flat_interleave(model) and not any(field.cardinality.repeatable for field in children):
            return "record"
        return "interleaved"
    return "ordered"


def _content_may_be_empty(model: ContentContract) -> bool:
    """Whether the compiled content admits no scalar occurrence.

    Attributes never contribute element character content.  A text scalar is
    nullable even though atomic and list scalars are not; this is the scalar
    presence distinction Task 1 must preserve as ``None`` rather than ``""``.
    """
    results: dict[int, bool] = {}
    pending: list[tuple[ContentContract, bool]] = [(model, False)]
    while pending:
        node, ready = pending.pop()
        if id(node) in results:
            continue
        if not ready:
            pending.append((node, True))
            for operand in node.operands:
                if id(operand) not in results:
                    pending.append((operand, False))
            continue
        if node.kind in ("attribute", "child", "element", "empty"):
            value = True
        elif node.kind == "scalar":
            value = any(contract.kind == "text" for contract in node.scalar_contracts)
        elif node.kind == "notAllowed":
            value = False
        elif node.kind in ("group", "interleave"):
            value = all(results[id(operand)] for operand in node.operands)
        elif node.kind == "choice":
            value = any(results[id(operand)] for operand in node.operands)
        elif node.kind == "repeat":
            value = all(results[id(operand)] for operand in node.operands)
        else:
            value = False
        results[id(node)] = value
    return results[id(model)]


def _scalar_annotation(
    contracts: tuple[ScalarContract, ...],
    *,
    codec: ScalarCodec,
    may_be_empty: bool,
) -> Any:
    return relaxng_scalar_annotation(
        contracts,
        codec=codec,
        may_be_empty=may_be_empty,
    )


def _root_scalar_annotation(
    contracts: tuple[ScalarContract, ...],
    *,
    codec: ScalarCodec,
    may_be_empty: bool,
) -> Any:
    return relaxng_scalar_annotation(
        contracts,
        codec=codec,
        may_be_empty=may_be_empty,
    )


def _attach_route(
    model: type[BaseModel], plan: ProjectionPlan, route: RouteContract, layout: str
) -> None:
    """Attach route identity and retained layout to the generated class."""
    scope = "start" if route.route_id in plan.roots else "local"
    target = cast(Any, model)
    target.__relaxng_route__ = route
    target.__relaxng_route_id__ = route.route_id
    target.__relaxng_layout__ = layout
    target.__relaxng_scope__ = scope
    target.__relaxng_start__ = scope == "start"
    target.__relaxng_element__ = route.expanded_name
    target.__relaxng_input_order__ = "unordered"
    target.model_dump = model_dump_with_scalar_wrappers
    target.model_dump_json = model_dump_json_with_scalar_wrappers


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


def _is_none(value: Any) -> bool:
    """Field exclusion predicate for omitted optional attributes/children."""
    return value is None


def _field_info(
    alias: str,
    *,
    metadata: dict[str, Any],
    cardinality: Cardinality,
    omit_none: bool = False,
) -> Any:
    repeatable = cardinality.repeatable
    options: dict[str, Any] = {
        "alias": alias,
        "json_schema_extra": metadata,
    }
    if omit_none:
        options["exclude_if"] = _is_none
    if repeatable:
        options["min_length"] = cardinality.minimum
        options["max_length"] = cardinality.maximum
    if cardinality.minimum > 0:
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
        self._codec = ScalarCodec(schema, self._plan, schema._grammar_fingerprint)
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
            # A local spelling directly matches any start name class that
            # admits (None, local), including an AnyName wildcard.  If no class
            # admits it, a uniquely named start route in a namespace may still
            # be selected by local name.
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
        if _content_cannot_match(route.content.model):
            raise IntegrationError(
                f"RELAX NG route {_route_label(route)} cannot match any document: "
                "its content is notAllowed"
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
        layout = _route_layout(route)
        may_be_empty = _content_may_be_empty(route.content.model)
        metadata = _route_metadata(self._plan, route, layout)
        metadata["x-pyxsd-contracts"] = [
            _contract_metadata(contract) for contract in route.content.scalar_contracts
        ]
        metadata["x-pyxsd-cardinality"] = {
            "minimum": 0 if may_be_empty else 1,
            "maximum": 1,
        }
        annotation = _root_scalar_annotation(
            route.content.scalar_contracts,
            codec=self._codec,
            may_be_empty=may_be_empty,
        )
        root_base: Any = RootModel
        model = create_model(
            self._names[route.route_id],
            __base__=root_base[annotation],
            __config__=ConfigDict(json_schema_extra=metadata),
            __module__=__name__,
        )
        _attach_route(model, self._plan, route, layout)
        return model

    def _record_model(self, route: RouteContract) -> type[BaseModel]:
        layout = _route_layout(route)
        may_be_empty = _content_may_be_empty(route.content.model)
        route_metadata = _route_metadata(self._plan, route, layout)
        child_aliases = set(route.children)
        field_extras: dict[str, dict[str, Any]] = {}
        child_fields: list[tuple[str, str, bool]] = []
        fields: dict[str, Any] = {}
        used: set[str] = {"xml_contract", _CHILD_PRESENCE_VALIDATOR}

        def add(
            alias: str,
            annotation: Any,
            *,
            cardinality: Cardinality,
            contracts: tuple[ScalarContract, ...],
            child_route_id: str | None = None,
        ) -> None:
            field_name = _safe_name(alias, used | (child_aliases - {alias}))
            used.add(field_name)
            field_metadata = _field_metadata(
                contracts,
                cardinality,
                child_route_id=child_route_id,
            )
            field_extras[alias] = field_metadata
            if child_route_id is not None:
                child_fields.append((alias, field_name, cardinality.repeatable))
            fields[field_name] = (
                annotation,
                _field_info(
                    alias,
                    metadata=field_metadata,
                    cardinality=cardinality,
                    omit_none=(
                        cardinality.minimum == 0 and not cardinality.repeatable and alias != "$"
                    ),
                ),
            )

        if route.content.scalar_contracts:
            add(
                "$",
                _scalar_annotation(
                    route.content.scalar_contracts,
                    codec=self._codec,
                    may_be_empty=may_be_empty,
                ),
                cardinality=Cardinality(0 if may_be_empty else 1, 1),
                contracts=route.content.scalar_contracts,
            )
        for name in sorted(route.attributes):
            field = route.attributes[name]
            add(
                "@" + field.expanded_name,
                _scalar_annotation(
                    field.scalar_contracts,
                    codec=self._codec,
                    may_be_empty=False,
                ),
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
            for property_schema in properties.values():
                clean_field_schema(property_schema)

        def validate_children(value: Any) -> Any:
            if isinstance(value, dict):
                for alias, field_name, repeatable in child_fields:
                    for key in (alias, field_name):
                        if key not in value:
                            continue
                        item = value[key]
                        if item is None:
                            raise ValueError(
                                f"child content must be omitted, not None, when absent: {alias!r}"
                            )
                        if (
                            repeatable
                            and isinstance(item, list)
                            and any(entry is None for entry in item)
                        ):
                            raise ValueError(f"child content items must not be None: {alias!r}")
            return value

        validators: dict[str, Any] = {
            _CHILD_PRESENCE_VALIDATOR: model_validator(mode="before")(validate_children)
        }
        model = create_model(
            self._names[route.route_id],
            __config__=ConfigDict(
                extra="forbid",
                strict=True,
                validate_by_name=True,
                serialize_by_alias=True,
                json_schema_extra=model_schema,
            ),
            __validators__=validators,
            __module__=__name__,
            **fields,
        )
        _attach_route(model, self._plan, route, layout)
        return model
