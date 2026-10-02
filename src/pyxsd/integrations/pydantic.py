"""Generate standalone Pydantic models and typed snapshots of bound XML.

Install ``pyxsd[pydantic]``. Models validate the projected data contract;
whole-document XML validation remains ``Document.revalidate()``.
"""

from __future__ import annotations

import base64
import json
import keyword
import math
import re
from dataclasses import asdict
from decimal import Decimal
from types import GenericAlias
from typing import Annotated, Any

try:
    from pydantic import (
        BaseModel,
        BeforeValidator,
        ConfigDict,
        Field,
        RootModel,
        create_model,
        model_validator,
    )
except ModuleNotFoundError as exc:
    if exc.name != "pydantic":
        raise
    raise ImportError("Install pyxsd[pydantic] to use generated Pydantic models") from exc

from pyxsd import xsd_data_types as xd
from pyxsd.document import Document
from pyxsd.schema import Schema

from . import IntegrationError
from ._shape import ElementShape, ShapeSet, list_item_type, scalar_kind
from ._values import (
    check_counts,
    lexical_value,
    plain_value,
    prepare_document,
    project,
    value_equal,
)

__all__ = ["ModelSet", "models"]


def _scalar_input(cls: type, value: Any) -> Any:
    kind = scalar_kind(cls)
    try:
        if kind == "qname":
            if not isinstance(value, str):
                raise ValueError("QName requires an expanded-name string")
            if value.startswith("{"):
                uri, local = value[1:].split("}", 1)
                if not uri:
                    raise ValueError("QName namespace must not be empty")
                bindings, lexical = {"p": uri}, "p:" + local
            else:
                if ":" in value:
                    raise ValueError(
                        "QName input requires an expanded name, not an undeclared prefix"
                    )
                bindings, lexical = {}, value
            with xd.qname_context(bindings):
                return plain_value(cls, cls(lexical))
        if kind == "list":
            if type(value) is not list:
                raise ValueError("XSD list requires a Python list")
            item_type = list_item_type(cls)
            items = [_scalar_input(item_type, item) for item in value]
            tokens = [_input_lexical(item_type, item) for item in items]
            if any(not token or any(c in " \t\r\n" for c in token) for token in tokens):
                raise ValueError("XSD list items must each be one nonempty lexical token")
            return plain_value(cls, cls(" ".join(tokens)))
        lexical = _input_lexical(cls, value)
        return plain_value(cls, cls(lexical))
    except (TypeError, ValueError, IntegrationError) as exc:
        raise ValueError(str(exc)) from exc


def _input_lexical(cls: type, value: Any) -> str:
    kind = scalar_kind(cls)
    if kind == "bool" and type(value) is bool:
        return "true" if value else "false"
    if kind == "int" and type(value) is int:
        return str(value)
    if kind == "decimal" and isinstance(value, (str, Decimal)):
        return format(value, "f") if isinstance(value, Decimal) else value
    if kind == "float" and type(value) is float:
        return (
            "NaN"
            if math.isnan(value)
            else ("INF" if value > 0 else "-INF")
            if math.isinf(value)
            else str(value)
        )
    if kind in ("base64", "hex") and type(value) is bytes:
        return base64.b64encode(value).decode("ascii") if kind == "base64" else value.hex()
    if kind == "str" and isinstance(value, str):
        return value
    raise ValueError(f"{cls.__name__} requires its projected Python type")


def _annotation(cls: type) -> Any:
    kind = scalar_kind(cls)
    facets = getattr(cls, "_facetConstraints_", None)
    if facets is not None and (
        (facets.patterns and kind not in ("str",)) or (kind == "qname" and not facets.is_empty)
    ):
        raise IntegrationError(
            f"{cls.__name__}: lexical/contextual facets cannot be enforced from projected input"
        )
    if getattr(cls, "_assertionFacets_", None) and kind != "str":
        raise IntegrationError(f"{cls.__name__}: lexical assertion facets need XML evidence")
    annotation: Any
    if kind == "list":
        annotation = GenericAlias(list, _annotation(list_item_type(cls)))
    else:
        annotation = {
            "bool": bool,
            "int": int,
            "decimal": Decimal,
            "float": float,
            "base64": bytes,
            "hex": bytes,
            "qname": str,
            "str": str,
        }[kind]
    constraints: dict[str, Any] = {}
    if kind in ("int", "decimal", "float"):
        constraints.update(ge=getattr(cls, "_min", None), le=getattr(cls, "_max", None))
        if facets is not None:
            constraints.update(
                ge=facets.min_inclusive if facets.min_inclusive is not None else constraints["ge"],
                le=facets.max_inclusive if facets.max_inclusive is not None else constraints["le"],
                gt=facets.min_exclusive,
                lt=facets.max_exclusive,
            )
    if facets is not None and kind in ("str", "list", "base64", "hex"):
        constraints.update(
            min_length=facets.length if facets.length is not None else facets.min_length,
            max_length=facets.length if facets.length is not None else facets.max_length,
        )
    metadata = {"x-pyxsd-type": getattr(cls, "name", cls.__name__)}
    if facets is not None:
        metadata["x-pyxsd-facets"] = json.loads(json.dumps(asdict(facets), default=str))
    return Annotated[
        annotation,
        Field(**constraints, json_schema_extra=metadata),
        BeforeValidator(lambda value: _scalar_input(cls, value)),
    ]


def _safe_name(alias: str, used: set[str]) -> str:
    if alias == "$":
        stem = "xml_value"
    elif alias == "$nil":
        stem = "xml_nil"
    else:
        attribute = alias.startswith("@")
        local = alias.lstrip("@").rsplit("}", 1)[-1]
        stem = ("attr_" if attribute else "") + re.sub(r"\W", "_", local)
    if (
        not stem
        or stem.startswith("_")
        or stem[0].isdigit()
        or keyword.iskeyword(stem)
        or hasattr(BaseModel, stem)
        or stem.startswith("model_")
    ):
        stem = "field_" + stem
    name, index = stem, 2
    while name in used:
        name, index = f"{stem}_{index}", index + 1
    used.add(name)
    return name


def _before(shape: ElementShape, aliases: dict[str, str]) -> Any:
    def validate(value: Any) -> Any:
        if isinstance(value, BaseModel):
            value = value.model_dump(by_alias=True, exclude_unset=True)
        if not isinstance(value, dict):
            raise ValueError("complex element requires a dictionary")
        data: dict[str, Any] = {}
        for key, item in value.items():
            alias = (
                key
                if key in aliases
                else next((a for a, name in aliases.items() if name == key), None)
            )
            if alias is None:
                raise ValueError(f"unknown field {key!r}")
            if alias in data:
                raise ValueError(f"conflicting alias/name inputs for {alias}")
            data[alias] = item
        nil = data.get("$nil", False)
        if nil and shape.declaration.getFixed() is not None:
            raise ValueError("nil element has a fixed value")
        if type(nil) is not bool:
            raise ValueError("nil state must be a boolean")
        for attr in shape.attributes:
            if attr.alias in data:
                if attr.prohibited or data[attr.alias] is None:
                    raise ValueError(f"prohibited/null attribute {attr.name}")
                if attr.fixed is not None and not value_equal(
                    attr.scalar,
                    _scalar_input(attr.scalar, data[attr.alias]),
                    lexical_value(attr.scalar, attr.fixed, declaration=attr.declaration),
                ):
                    raise ValueError(f"fixed attribute {attr.name} violated")
            elif attr.required:
                raise ValueError(f"required attribute {attr.name} is missing")
        counts: dict[int, int] = {}
        for child in shape.children:
            present = child.name in data
            item = data.get(child.name)
            if nil and present:
                raise ValueError("nil element cannot contain child content")
            if present and child.repeated:
                if not isinstance(item, list) or type(item) is not list:
                    raise ValueError(f"{child.name} requires a list")
                counts[id(child.declaration)] = len(item)
            else:
                counts[id(child.declaration)] = int(present)
            if (
                present
                and item is None
                and (child.repeated or not (child.nillable and child.primitive))
            ):
                raise ValueError(f"{child.name} is not nullable")
            if present and child.primitive and child.declaration.getFixed() is not None:
                assert child.scalar is not None
                expected = lexical_value(
                    child.scalar, child.declaration.getFixed(), declaration=child.declaration
                )
                for supplied in item if child.repeated else [item]:
                    if supplied is None or not value_equal(
                        child.scalar, _scalar_input(child.scalar, supplied), expected
                    ):
                        raise ValueError(f"fixed child {child.name} violated")
        if "$" in data:
            assert shape.scalar is not None
            if nil or data["$"] is None:
                raise ValueError(
                    "nil element cannot contain simple content; non-nil content cannot be null"
                )
            if shape.declaration.getFixed() is not None and not value_equal(
                shape.scalar,
                _scalar_input(shape.scalar, data["$"]),
                lexical_value(
                    shape.scalar, shape.declaration.getFixed(), declaration=shape.declaration
                ),
            ):
                raise ValueError("fixed simple content violated")
        elif shape.scalar is not None and not nil:
            raise ValueError("simple content value is required")
        if not nil:
            try:
                check_counts(shape, counts)
            except IntegrationError as exc:
                raise ValueError(str(exc)) from exc
        return data

    return validate


def _field_schema(schema: dict[str, Any]) -> None:
    # A missing optional non-nillable field has an internal None placeholder,
    # not a nullable input/default in the JSON Schema contract.
    if schema.get("default", object()) is None:
        schema.pop("default")


class ModelSet:
    """A compiled-schema-scoped registry of generated Pydantic models."""

    def __init__(self, schema: Schema) -> None:
        self.schema = schema
        self._shapes = ShapeSet(schema)
        self._models: dict[tuple[str, ...], type[BaseModel]] = {}

    def model_for(self, *, element: str, path: tuple[str, ...] = ()) -> type[BaseModel]:
        """Resolve a global declaration or an unambiguous local declaration path."""
        return self._model(self._shapes.resolve(element, path))

    def _model(self, shape: ElementShape) -> type[BaseModel]:
        if shape.path in self._models:
            return self._models[shape.path]
        name = f"XmlModel{len(self._models) + 1}_" + re.sub(r"\W", "_", shape.name)
        model: type[BaseModel]
        if shape.primitive:
            assert shape.scalar is not None
            annotation = _annotation(shape.scalar)
            if shape.nillable:
                annotation = annotation | None
            fixed = shape.declaration.getFixed()

            def validate_root(value: Any) -> Any:
                assert shape.scalar is not None
                if value is None and shape.nillable:
                    if fixed is not None:
                        raise ValueError("nil element has a fixed value")
                    return None
                converted = _scalar_input(shape.scalar, value)
                if fixed is not None and not value_equal(
                    shape.scalar,
                    converted,
                    lexical_value(shape.scalar, fixed, declaration=shape.declaration),
                ):
                    raise ValueError("fixed element value violated")
                return converted

            root_base: Any = RootModel
            validators: dict[str, Any] = {
                "xml_contract": model_validator(mode="before")(validate_root)
            }
            model = create_model(name, __base__=root_base[annotation], __validators__=validators)
        else:
            fields: dict[str, Any] = {}
            aliases: dict[str, str] = {}
            used: set[str] = {"xml_contract"}

            def add(
                alias: str,
                annotation: Any,
                default: Any = None,
                *,
                factory: Any = None,
                minimum: int | None = None,
                maximum: int | None = None,
            ) -> None:
                other_aliases = {child.name for child in shape.children if child.name != alias}
                field_name = _safe_name(alias, used | other_aliases)
                used.add(field_name)
                aliases[alias] = field_name
                info = (
                    Field(
                        default_factory=factory,
                        alias=alias,
                        min_length=minimum,
                        max_length=maximum,
                    )
                    if factory is not None
                    else Field(
                        default=default,
                        alias=alias,
                        min_length=minimum,
                        max_length=maximum,
                    )
                )
                fields[field_name] = (annotation, info)

            if shape.nillable:
                add("$nil", bool, False)
            if shape.scalar is not None:
                add("$", _annotation(shape.scalar), None if shape.nillable else ...)
            for attr in shape.attributes:
                if attr.prohibited:
                    continue
                default = attr.fixed if attr.fixed is not None else attr.default
                effective = (
                    lexical_value(attr.scalar, default, declaration=attr.declaration)
                    if default is not None
                    else None
                )
                add(attr.alias, _annotation(attr.scalar), ... if attr.required else effective)

            def required(particle: Any) -> set[int]:
                if particle is None or particle.min_occurs == 0 or particle.kind == "choice":
                    return set()
                if particle.kind == "element":
                    return {id(particle.descriptor)}
                return {key for child in particle.children for key in required(child)}

            required_children = set() if shape.nillable else required(shape.particle)
            for child in shape.children:
                if child.primitive:
                    assert child.scalar is not None
                    annotation = _annotation(child.scalar)
                    if child.nillable:
                        annotation = annotation | None
                else:
                    annotation = self._model(child)
                add(
                    child.name,
                    GenericAlias(list, annotation) if child.repeated else annotation,
                    ... if id(child.declaration) in required_children else None,
                    factory=list
                    if child.repeated and id(child.declaration) not in required_children
                    else None,
                    minimum=child.minimum if child.repeated else None,
                    maximum=child.maximum if child.repeated else None,
                )
            metadata: dict[str, Any] = {
                "x-pyxsd-element": shape.name,
                "x-pyxsd-declaration-path": list(shape.path),
                "x-pyxsd-validation": "projected-data; use Document.revalidate for XML validity",
            }

            def model_schema(description: dict[str, Any]) -> None:
                description.update(metadata)
                for property_schema in description.get("properties", {}).values():
                    _field_schema(property_schema)

            validators = {"xml_contract": model_validator(mode="before")(_before(shape, aliases))}
            model = create_model(
                name,
                __config__=ConfigDict(
                    extra="forbid",
                    strict=True,
                    validate_by_name=True,
                    json_schema_extra=model_schema,
                ),
                __validators__=validators,
                **fields,
            )
        self._models[shape.path] = model
        return model

    def from_document(self, document: Document, *, revalidate: bool = False) -> BaseModel:
        """Snapshot a bound document; request fresh XML validation explicitly."""
        document = prepare_document(self.schema, document, revalidate)
        declaration = document.root._descriptor_
        return self.from_node(document.root, element=declaration.expandedName)

    def from_node(self, node: Any, *, element: str, path: tuple[str, ...] = ()) -> BaseModel:
        """Snapshot an occurrence matching the selected compiled declaration."""
        shape = self._shapes.resolve(element, path)
        model = self._model(shape)
        return model.model_validate(project(shape, node, explicit=True))


def models(schema: Schema) -> ModelSet:
    """Create a model registry without requiring instance XML."""
    return ModelSet(schema)
