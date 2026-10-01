"""Private, backend-independent views of already compiled declarations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyxsd import xsd_data_types as xd
from pyxsd.schema import Schema

from . import IntegrationError


@dataclass(eq=False)
class AttributeShape:
    name: str
    declaration: Any
    scalar: type
    required: bool
    prohibited: bool
    default: str | None
    fixed: str | None

    @property
    def alias(self) -> str:
        return "@" + self.name


@dataclass(eq=False)
class ElementShape:
    schema: Schema
    name: str
    path: tuple[str, ...]
    declaration: Any
    cls: type
    scalar: type | None
    nillable: bool
    minimum: int = 1
    maximum: int | None = 1
    children: list[ElementShape] = field(default_factory=list)
    attributes: list[AttributeShape] = field(default_factory=list)
    particle: Any = None

    @property
    def primitive(self) -> bool:
        return getattr(self.cls, "_contentKind_", "simple") != "complex"

    @property
    def repeated(self) -> bool:
        return self.maximum is None or self.maximum > 1

    def fail(self, reason: str) -> None:
        raise IntegrationError(f"{'/'.join(self.path)}: {reason}")


def scalar_kind(cls: type) -> str:
    """Classify native types, with bool and list types before their scalar bases."""
    if getattr(cls, "_unionMembers", None):
        raise IntegrationError(f"{cls.__name__}: heterogeneous union is unsupported")
    if issubclass(cls, xd.XsdList) or issubclass(cls, (xd.IDREFS, xd.ENTITIES, xd.NMTOKENS)):
        return "list"
    for base, kind in (
        (xd.Boolean, "bool"),
        (xd.Integer, "int"),
        (xd.Decimal, "decimal"),
        (xd.Double, "float"),
        (xd.Base64Binary, "base64"),
        (xd.HexBinary, "hex"),
        (xd.QName, "qname"),
        (xd.String, "str"),
    ):
        if issubclass(cls, base):
            if issubclass(cls, (xd.AnyType, xd.AnyAtomicType)):
                break
            return kind
    raise IntegrationError(f"{cls.__name__}: unsupported scalar type")


def list_item_type(cls: type) -> type:
    if issubclass(cls, xd.XsdList):
        return cls.itemType
    return xd.NMTOKEN if issubclass(cls, xd.NMTOKENS) else xd.NCName


def particle_elements(particle: Any) -> list[Any]:
    if particle is None:
        return []
    if particle.kind == "element":
        return [particle]
    return [leaf for child in particle.children for leaf in particle_elements(child)]


class ShapeSet:
    """Resolve global/local declarations without treating local aliases as identities."""

    def __init__(self, schema: Schema) -> None:
        schema.require_valid()
        self.schema = schema
        self._cache: dict[tuple[str, ...], ElementShape] = {}

    def resolve(self, element: str, path: tuple[str, ...] = ()) -> ElementShape:
        if not isinstance(path, tuple) or not all(isinstance(step, str) for step in path):
            raise IntegrationError("declaration path must be a tuple of expanded names")
        key = (element, *path)
        if key in self._cache:
            return self._cache[key]
        candidates = {
            id(decl): decl
            for entries in self.schema.components.values()
            for decl in entries
            if type(decl).__name__ == "Element"
            and decl.isGlobalDeclaration()
            and decl.expandedName == element
        }
        if len(candidates) != 1:
            raise IntegrationError(f"{element}: global element declaration is missing or ambiguous")
        declaration = next(iter(candidates.values()))
        for step in path:
            candidates = {
                id(p.descriptor): p.descriptor
                for p in particle_elements(getattr(declaration.getType(), "_contentModel_", None))
                if p.descriptor.instanceName(parser=self.schema._host) == step
            }
            if len(candidates) != 1:
                raise IntegrationError(f"{key}: local declaration {step!r} is missing or ambiguous")
            declaration = next(iter(candidates.values()))
        shape = self._build(declaration, key, set())
        self._cache[key] = shape
        return shape

    def _build(self, declaration: Any, path: tuple[str, ...], active: set[type]) -> ElementShape:
        cls = declaration.getType()
        if not isinstance(cls, type):
            raise IntegrationError(f"{path}: unresolved declaration type")
        if cls in active:
            raise IntegrationError(f"{path}: recursive type graph is unsupported")
        primitive = getattr(cls, "_contentKind_", "simple") != "complex"
        scalar = cls if primitive else getattr(cls, "_simpleContentType_", None)
        shape = ElementShape(
            self.schema,
            declaration.instanceName(parser=self.schema._host),
            path,
            declaration,
            cls,
            scalar,
            declaration.isNillable(),
        )
        if scalar is not None:
            scalar_kind(scalar)
        if primitive:
            return shape
        if not getattr(cls, "_elementOnly_", True) and scalar is None:
            shape.fail("mixed content is unsupported")
        if getattr(cls, "hasWildcardAttributes_", False):
            shape.fail("attribute wildcard is unsupported")
        shape.particle = getattr(cls, "_contentModel_", None)
        if shape.particle is None and scalar is None:
            shape.fail("unrepresentable content model")
        if getattr(cls, "_openContent_", None) or getattr(cls, "_openContentSpec_", None):
            shape.fail("open content wildcard is unsupported")
        self._check_particle(shape, shape.particle)
        names: set[str] = set()
        for p in particle_elements(shape.particle):
            name = p.descriptor.instanceName(parser=self.schema._host)
            if name in names:
                shape.fail(f"ambiguous declaration positions for {name}")
            names.add(name)
            child = self._build(p.descriptor, (*path, name), active | {cls})
            child.minimum, child.maximum = p.min_occurs, p.max_occurs
            shape.children.append(child)
        attributes: dict[str, Any] = {}
        for base in reversed(cls.__mro__):
            for key in base.__dict__.get("_attributeNames_", ()):
                descriptor = base.__dict__[key]
                attributes[descriptor.instanceName(is_attribute=True, parser=self.schema._host)] = (
                    descriptor
                )
        for name, descriptor in attributes.items():
            attr_type = descriptor.getType()
            scalar_kind(attr_type)
            shape.attributes.append(
                AttributeShape(
                    name,
                    descriptor,
                    attr_type,
                    descriptor.getUse() == "required",
                    descriptor.getUse() == "prohibited",
                    descriptor.getDefault(),
                    descriptor.getFixed(),
                )
            )
        return shape

    def _check_particle(self, shape: ElementShape, particle: Any) -> None:
        if particle is None or particle.kind == "element":
            return
        if particle.kind == "any" or particle.open_content:
            shape.fail("wildcard/open content is unsupported")
        if particle.max_occurs is None or particle.max_occurs > 1:
            shape.fail("repeated composite group is unsupported")
        if particle.kind == "choice" and any(p.kind != "element" for p in particle.children):
            shape.fail("complex choice is unsupported")
        for child in particle.children:
            self._check_particle(shape, child)
