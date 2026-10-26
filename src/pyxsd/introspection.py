"""Immutable, native schema descriptions independent of optional projections.

Component IDs describe declarations, not generated Python object identities.
Descriptions are a snapshot of a compiled schema; compile again after changing
schema sources or overlays. Particle trees, rather than member occurrence
summaries alone, retain the relationships between choices and repeated groups.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal

from pyxsd.locations import IssueLocation
from pyxsd.namespaces import XSD_NS, namespace_of

if TYPE_CHECKING:
    from pyxsd.schema import Schema


@dataclass(frozen=True)
class FacetDescription:
    """An effective facet; patterns retain AND-of-OR restriction groups."""

    name: str
    value: Any
    representation: str = "value"


@dataclass(frozen=True)
class WildcardDescription:
    """A wildcard's effective namespace and validation policy."""

    namespace: str
    process_contents: str
    target_namespace: str | None
    not_namespace: tuple[str, ...]
    not_qname: tuple[str, ...]


@dataclass(frozen=True)
class ParticleDescription:
    """A compiled particle, including group boundaries and declaration links."""

    kind: str
    min_occurs: int
    max_occurs: int | None
    component_id: str | None
    children: tuple[ParticleDescription, ...]
    wildcard: WildcardDescription | None = None
    open_content: bool = False


@dataclass(frozen=True)
class MemberDescription:
    """An XML member, accessor and type; particle trees govern group constraints."""

    kind: str
    xml_name: str
    python_name: str
    python_type: str
    component_id: str | None
    min_occurs: int
    max_occurs: int | None
    default: str | None
    fixed: str | None
    nillable: bool
    use: str | None
    facets: tuple[FacetDescription, ...]
    occurrence_precision: Literal["exact", "conservative"] = "conservative"


@dataclass(frozen=True)
class IdentityDescription:
    """An authored key, unique or keyref, without claiming XPath equivalence."""

    kind: str
    name: str | None
    selector: str | None
    fields: tuple[str, ...]
    refer: str | None


@dataclass(frozen=True)
class ComponentDescription:
    """One declaration with native constraints and generated binding metadata."""

    id: str
    kind: str
    name: str | None
    namespace: str | None
    global_declaration: bool
    location: IssueLocation | None
    owner_id: str | None
    python_name: str | None
    python_type: str | None
    members: tuple[MemberDescription, ...]
    particle: ParticleDescription | None
    facets: tuple[FacetDescription, ...]
    assertions: tuple[str, ...]
    default: str | None
    fixed: str | None
    nillable: bool
    mixed: bool
    derivation: str | None
    base_types: tuple[str, ...]
    substitution_group: str | None
    attribute_wildcard: WildcardDescription | None
    documentation: str | None
    identity_constraints: tuple[IdentityDescription, ...]


@dataclass(frozen=True)
class SchemaDescription:
    """A deterministic schema inventory; roots are global element IDs."""

    target_namespace: str | None
    roots: tuple[str, ...]
    components: tuple[ComponentDescription, ...]


def describe_schema(schema: Schema) -> SchemaDescription:
    """Return the native inventory, without requiring schema validity."""
    descriptions = tuple(_index(schema).components.values())
    return SchemaDescription(
        schema.target_namespace,
        tuple(c.id for c in descriptions if c.kind == "element" and c.global_declaration),
        descriptions,
    )


def describe_component(schema: Schema, component_id: str) -> ComponentDescription:
    """Return one exact declaration, raising KeyError for an unknown ID."""
    return _index(schema).components[component_id]


def find_components(
    schema: Schema,
    *,
    text: str | None = None,
    kind: str | None = None,
    namespace: str | None = None,
) -> tuple[ComponentDescription, ...]:
    """Filter by literal case-insensitive text, component kind and namespace.

    None leaves a filter unrestricted; an empty namespace selects unqualified
    declarations. No caller expression is evaluated.
    """
    needle = text.casefold() if text is not None else ""
    return tuple(
        c
        for c in _index(schema).components.values()
        if (kind is None or c.kind == kind)
        and (namespace is None or (c.namespace or "") == namespace)
        and needle in " ".join((c.id, c.name or "", c.python_name or "")).casefold()
    )


def related_components(
    schema: Schema,
    component_id: str,
    *,
    direction: Literal["uses", "used_by"],
    depth: int,
) -> tuple[ComponentDescription, ...]:
    """Breadth-first declaration traversal, excluding the seed and duplicates."""
    if direction not in ("uses", "used_by"):
        raise ValueError("direction must be uses or used_by")
    if depth < 0:
        raise ValueError("depth must be non-negative")
    index = _index(schema)
    index.components[component_id]
    graph = index.edges if direction == "uses" else index.reverse
    seen = {component_id}
    frontier = [component_id]
    result = []
    for _ in range(depth):
        following = []
        for parent in frontier:
            for child in graph[parent]:
                if child not in seen:
                    seen.add(child)
                    following.append(child)
                    result.append(index.components[child])
        frontier = following
        if not frontier:
            break
    return tuple(result)


class _Index:
    def __init__(self, schema: Schema) -> None:
        from pyxsd.element_representatives.element_representative import componentKind
        from pyxsd.stubs import _inventory

        inventory = _inventory(schema)
        self.entries = {id(entry.cls): entry for entry in inventory}
        self.names = {id(entry.cls): entry.name for entry in inventory}
        all_representatives = {}
        pending = [er for group in schema.components.values() for er in group]
        while pending:
            er = pending.pop()
            if er is None or id(er) in all_representatives:
                continue
            all_representatives[id(er)] = er
            pending.extend(reversed(er.processedChildren))
        representatives = {
            key: er
            for key, er in all_representatives.items()
            if componentKind(er) not in (None, "schema")
        }
        collisions: dict[str, list[tuple[int, Any]]] = {}
        for key, er in representatives.items():
            collisions.setdefault(_component_id(schema, er), []).append((key, er))
        self.ids: dict[int, str] = {}
        for base_id, declarations in collisions.items():
            declarations.sort(key=lambda item: _declaration_order(schema, item[1]))
            for occurrence, (key, _) in enumerate(declarations, start=1):
                self.ids[key] = base_id if occurrence == 1 else f"{base_id}#{occurrence}"
        self.node_ids = {id(er.xsdElement): self.ids[key] for key, er in representatives.items()}
        self.type_ers = {
            id(er._generatedClass): er
            for er in representatives.values()
            if componentKind(er) == "type"
            and isinstance(getattr(er, "_generatedClass", None), type)
        }
        self.type_ids = {key: self.ids[id(er)] for key, er in self.type_ers.items()}
        self.classes: dict[str, type] = {}
        self.elements: dict[str, Any] = {}
        self.components = {}
        for key, er in representatives.items():
            component_id = self.ids[key]
            cls = _runtime_class(er)
            if cls is not None:
                self.classes[component_id] = cls
            if componentKind(er) == "element" and er.isGlobalDeclaration():
                self.elements[component_id] = er
            self.components[component_id] = self._describe(schema, er, cls)
        self.edges: dict[str, tuple[str, ...]] = {}
        reverse: dict[str, list[str]] = {key: [] for key in self.components}
        for er_id, er in representatives.items():
            component_id = self.ids[er_id]
            description = self.components[component_id]
            links = [
                self.type_ids.get(id(self.classes.get(component_id))),
                *description.base_types,
                description.substitution_group,
                *(member.component_id for member in description.members),
                *self._child_ids(er),
            ]
            if er.xsdElement.get("ref") is not None:
                from pyxsd.element_representatives.element_representative import componentKind

                links.append(f"{componentKind(er)}:{er.resolveSchemaQName(er.ref)}")
            self.edges[component_id] = tuple(
                dict.fromkeys(v for v in links if v in reverse and v != component_id)
            )
            for target in self.edges[component_id]:
                reverse[target].append(component_id)
        self.reverse = {key: tuple(value) for key, value in reverse.items()}

    def _child_ids(self, er: Any) -> tuple[str, ...]:
        pending = list(er.processedChildren)
        result = []
        while pending:
            child = pending.pop()
            if child is None:
                continue
            component_id = self.ids.get(id(child))
            if component_id is not None:
                result.append(component_id)
            else:
                pending.extend(child.processedChildren)
        return tuple(result)

    def _describe(self, schema: Schema, er: Any, cls: type | None) -> ComponentDescription:
        from pyxsd.element_representatives.element_representative import componentKind
        from pyxsd.schema_base import SchemaBase
        from pyxsd.stubs import _call

        kind = componentKind(er)
        entry = self.entries.get(id(cls))
        owner = er.parent
        while owner is not None and id(owner) not in self.ids:
            owner = owner.parent
        name = (
            _xml_name(er)
            if "name" in er.xsdElement.attrib or "ref" in er.xsdElement.attrib
            else None
        )
        base_types = tuple(
            dict.fromkeys(
                self.type_ids.get(id(base), base.__name__)
                for base in getattr(cls, "__bases__", ())
                if base.__name__ not in ("SchemaBase", "object")
            )
        )
        content = getattr(cls, "_instanceContentModel_", None) or getattr(
            cls, "_contentModel_", None
        )
        if kind == "group":
            from pyxsd.content_model import compile_own_content

            content = compile_own_content(er, schema._host)
        nodes = tuple(_owned_xml(er.xsdElement))
        substitution = er.xsdElement.get("substitutionGroup")
        return ComponentDescription(
            id=self.ids[id(er)],
            kind=kind,
            name=name,
            namespace=namespace_of(name) if name is not None else er.getNamespace(),
            global_declaration=er.isGlobalDeclaration(),
            location=schema.declaration_origins.get(er.xsdElement),
            owner_id=self.ids.get(id(owner)),
            python_name=entry.name if entry is not None else None,
            python_type=self.names.get(id(cls), getattr(cls, "__name__", None)),
            members=(
                self._members(cls)
                if cls is not None
                else tuple(
                    self._member(attr, "attribute", attr_name, None)
                    for attr_name, attr in getattr(er, "attributes", {}).items()
                )
            ),
            particle=self._particle(content) if content is not None else None,
            facets=self._facets(cls),
            assertions=tuple(
                assertion.test
                for base in reversed(getattr(cls, "__mro__", ()))
                for assertion in base.__dict__.get("_assertions_", ())
            ),
            default=_call(er, "getDefault"),
            fixed=_call(er, "getFixed"),
            nillable=_call(er, "isNillable") is True,
            mixed=cls is SchemaBase or _call(self.type_ers.get(id(cls)), "effectiveMixed") is True,
            derivation=getattr(cls, "_derivation_", None),
            base_types=base_types,
            substitution_group=(
                f"element:{er.resolveSchemaQName(substitution)}" if substitution else None
            ),
            attribute_wildcard=_wildcard(
                getattr(cls, "effectiveAttributeWildcard_", None), schema.target_namespace
            ),
            documentation="\n".join(
                "".join(node.itertext()).strip()
                for node in nodes
                if node.tag == f"{{{XSD_NS}}}documentation"
            )
            or None,
            identity_constraints=tuple(
                _identity(node)
                for node in nodes
                if node.tag in {f"{{{XSD_NS}}}{kind}" for kind in ("key", "unique", "keyref")}
            ),
        )

    def _members(self, cls: type) -> tuple[MemberDescription, ...]:
        from pyxsd.element_representatives.attribute import Attribute
        from pyxsd.element_representatives.element import Element

        members = {}
        shadowed: set[str] = set()
        for base in cls.__mro__:
            for kind, field_name in (
                ("element", "_elementNames_"),
                ("attribute", "_attributeNames_"),
            ):
                for name in base.__dict__.get(field_name, ()):
                    descriptor = base.__dict__.get(name)
                    if name in shadowed or not isinstance(descriptor, (Element, Attribute)):
                        continue
                    members[name] = self._member(
                        descriptor, kind, name, getattr(cls, "_contentModel_", None)
                    )
            shadowed.update(base.__dict__)
        return tuple(members.values())

    def _member(self, descriptor: Any, kind: str, name: str, model: Any) -> MemberDescription:
        from pyxsd.stubs import _call, _effective_occurs

        use = _call(descriptor, "getUse") if kind == "attribute" else None
        maximum: int | None
        if kind == "attribute":
            minimum, maximum = (int(use == "required"), 0 if use == "prohibited" else 1)
        else:
            minimum, maximum = _effective_occurs(model, descriptor)
        resolved = _runtime_class(descriptor)
        return MemberDescription(
            kind=kind,
            xml_name=_xml_name(descriptor),
            python_name=name,
            python_type=self.names.get(id(resolved), getattr(resolved, "__name__", "Any")),
            component_id=self.node_ids.get(id(descriptor.xsdElement)),
            min_occurs=minimum,
            max_occurs=maximum,
            default=_call(descriptor, "getDefault"),
            fixed=_call(descriptor, "getFixed"),
            nillable=_call(descriptor, "isNillable") is True,
            use=use,
            facets=self._facets(resolved),
            occurrence_precision="exact" if kind == "attribute" else "conservative",
        )

    def _facets(self, cls: type | None) -> tuple[FacetDescription, ...]:
        constraints = getattr(cls, "_facetConstraints_", None)
        if cls is None or constraints is None:
            return ()
        result = []
        for field in fields(constraints):
            value = getattr(constraints, field.name)
            if value is None or value == ():
                continue
            representation = "value"
            if field.name == "patterns":
                authored = []
                for base in reversed(cls.__mro__):
                    entry = self.entries.get(id(base))
                    if entry is not None and entry.er is not None:
                        for node in _owned_xml(entry.er.xsdElement):
                            if node.tag == f"{{{XSD_NS}}}restriction":
                                group = tuple(
                                    p.get("value", "") for p in node.findall(f"{{{XSD_NS}}}pattern")
                                )
                                if group:
                                    authored.append(group)
                if len(authored) == len(value):
                    value, representation = tuple(authored), "xsd"
                else:
                    value, representation = (
                        tuple(tuple(p.pattern for p in group) for group in value),
                        "python-regex",
                    )
            result.append(FacetDescription(field.name, _freeze(value), representation))
        return tuple(result)

    def _particle(self, particle: Any) -> ParticleDescription:
        return ParticleDescription(
            particle.kind,
            particle.min_occurs,
            particle.max_occurs,
            self.node_ids.get(id(getattr(particle.descriptor, "xsdElement", None))),
            tuple(self._particle(child) for child in particle.children),
            _wildcard(particle.spec, None),
            particle.open_content,
        )


def _index(schema: Schema) -> _Index:
    index = schema._introspection_index
    if index is None:
        index = _Index(schema)
        schema._introspection_index = index
    return index


def _declaration_order(schema: Schema, er: Any) -> tuple[str, tuple[tuple[str, int], ...]]:
    location = schema.declaration_origins.get(er.xsdElement)
    if location is None:
        return "", ()
    return location.source or "", tuple((step.name, step.index) for step in location.path)


def _component_id(schema: Schema, er: Any) -> str:
    from pyxsd.element_representatives.element_representative import componentKind

    kind = componentKind(er)
    if er.isGlobalDeclaration() and er.xsdElement.get("name") is not None:
        return f"{kind}:{_xml_name(er)}"
    location = schema.declaration_origins.get(er.xsdElement)
    address = (
        (location.source, tuple((step.name, step.index) for step in location.path))
        if location
        else (None, er.name)
    )
    digest = sha256(
        json.dumps((kind, er.getNamespace(), address), ensure_ascii=True).encode()
    ).hexdigest()
    return f"{kind}@{digest}"


def _runtime_class(er: Any) -> type | None:
    from pyxsd.stubs import _call

    cls = getattr(er, "_generatedClass", None)
    if not isinstance(cls, type):
        cls = _call(er, "getType")
    return cls if isinstance(cls, type) else None


def _xml_name(er: Any) -> str:
    if type(er).__name__ in ("Element", "Attribute"):
        return er.expandedInstanceName(is_attribute=type(er).__name__ == "Attribute")
    return er.expandedName


def _owned_xml(root: Any):
    yield root
    for child in root:
        if child.tag not in (f"{{{XSD_NS}}}complexType", f"{{{XSD_NS}}}simpleType"):
            yield from _owned_xml(child)


def _wildcard(spec: Any, target: str | None) -> WildcardDescription | None:
    if spec is None:
        return None
    return WildcardDescription(
        spec.namespace,
        spec.process_contents,
        spec.effective_target(target),
        tuple(sorted(spec.not_namespace)),
        tuple(sorted(spec.not_qname)),
    )


def _identity(node: Any) -> IdentityDescription:
    selector = node.find(f"{{{XSD_NS}}}selector")
    return IdentityDescription(
        node.tag.rsplit("}", 1)[-1],
        node.get("name"),
        selector.get("xpath") if selector is not None else None,
        tuple(f.get("xpath", "") for f in node.findall(f"{{{XSD_NS}}}field")),
        node.get("refer"),
    )


def _freeze(value: Any) -> Any:
    from pyxsd.xsd_data_types import Boolean

    if isinstance(value, Boolean):
        return bool(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    return value


def _stub_selection(
    schema: Schema, component_ids: tuple[str, ...], depth: int | None
) -> tuple[frozenset[int], frozenset[int]]:
    index = _index(schema)
    selected = set()
    for component_id in component_ids:
        describe_component(schema, component_id)
        selected.add(component_id)
        selected.update(
            c.id
            for c in related_components(
                schema,
                component_id,
                direction="uses",
                depth=depth if depth is not None else len(index.components),
            )
        )
    return (
        frozenset(id(index.classes[key]) for key in selected if key in index.classes),
        frozenset(id(index.elements[key]) for key in selected if key in index.elements),
    )
