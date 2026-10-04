"""Schema-scoped contextual scalar extraction; no optional backend imports."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from pyxsd.document import Document
from pyxsd.schema import Schema

from . import IntegrationError
from ._shape import (
    DeclarationRoute,
    ElementShape,
    ShapeSet,
    particle_elements,
    resolve_route,
    scalar_kind,
)
from ._values import guard_node, nil_state, read_scalar
from .projection import FieldSource


@dataclass(frozen=True)
class ColumnBinding:
    name: str
    source: FieldSource
    start: int
    shapes: tuple[ElementShape, ...]
    scalar: type
    nullable: bool


@dataclass(frozen=True)
class ProjectionPlan:
    schema: Schema
    route: DeclarationRoute
    row_shapes: tuple[ElementShape, ...]
    columns: tuple[ColumnBinding, ...]


def _edge_nullable(particle: Any, edge: Any, optional: bool = False) -> bool:
    """Track optional groups and choices along the already compiled particle tree."""
    optional = optional or particle.min_occurs == 0 or particle.kind == "choice"
    if particle is edge:
        return optional
    for child in particle.children:
        if any(leaf is edge for leaf in particle_elements(child)):
            return _edge_nullable(child, edge, optional)
    raise IntegrationError("column edge is not in its enclosing particle")


def compile_projection(
    schema: Schema, *, element: str, path: tuple[str, ...], columns: Mapping[str, FieldSource]
) -> ProjectionPlan:
    """Prepare fixed scalar bindings and structural guards before reading XML."""
    if not isinstance(columns, Mapping) or not columns:
        raise IntegrationError("columns must be a nonempty mapping of output names to FieldSource")
    columns = dict(columns)
    try:
        route = resolve_route(schema, element, path)
    except IntegrationError as exc:
        name = next(iter(columns))
        raise IntegrationError(
            f"column {name!r} source {columns[name]!r} row route {(element, path)!r}: {exc}"
        ) from exc
    shapes = ShapeSet(schema)
    guards: dict[tuple[str, ...], ElementShape] = {}

    def guard(names: tuple[str, ...]) -> ElementShape:
        if names not in guards:
            guards[names] = shapes.shallow(names[0], names[1:])
        return guards[names]

    row_shapes: tuple[ElementShape, ...] = ()
    bindings = []
    for name, source in dict(columns).items():
        try:
            if not isinstance(name, str) or not name:
                raise IntegrationError("output name must be a nonempty string")
            if not isinstance(source, FieldSource):
                raise IntegrationError("column source must be a FieldSource")
            if not row_shapes:
                row_shapes = tuple(guard(route.path[: i + 1]) for i in range(len(route.path)))
            start = 0 if source.scope == "root" else len(path)
            if source.scope == "ancestor":
                start -= source.levels
            if start < 0:
                raise IntegrationError("ancestor source climbs above the configured root")
            names = route.path[: start + 1]
            current = guard(names)
            column_shapes = [current]
            nullable = False
            for step in source.path:
                # Resolution rejects multiple particle positions, even with a reused descriptor.
                next_route = resolve_route(schema, names[0], (*names[1:], step))
                edge = next_route.edges[-1]
                if edge.max_occurs is None or edge.max_occurs > 1:
                    raise IntegrationError("downward column route is potentially repeated")
                nullable = nullable or current.nillable or _edge_nullable(current.particle, edge)
                names = (*names, step)
                current = guard(names)
                column_shapes.append(current)
            if source.attribute is None:
                scalar = current.scalar
                nullable = nullable or current.nillable
                if scalar is None:
                    raise IntegrationError("selected endpoint is a struct, not a scalar")
            else:
                attrs = [
                    a for a in current.attributes if a.name == source.attribute and not a.prohibited
                ]
                if len(attrs) != 1:
                    raise IntegrationError("attribute endpoint is missing or prohibited")
                attr = attrs[0]
                scalar = attr.scalar
                nullable = nullable or (
                    not attr.required and attr.default is None and attr.fixed is None
                )
            if scalar_kind(scalar) == "list":
                raise IntegrationError("list-valued columns are unsupported")
            bindings.append(
                ColumnBinding(name, source, start, tuple(column_shapes), scalar, nullable)
            )
        except IntegrationError as exc:
            raise IntegrationError(
                f"column {name!r} source {source!r} on {route.path}: {exc}"
            ) from exc
    return ProjectionPlan(schema, route, row_shapes, tuple(bindings))


def _topology(root: Any) -> dict[int, int]:
    """Check cycles before Document.findall's tree projection, counting occurrences."""
    active: set[int] = set()
    counts: dict[int, int] = {}
    pending = [(root, False)]
    while pending:
        node, exiting = pending.pop()
        key = id(node)
        if exiting:
            active.remove(key)
            continue
        if key in active:
            raise IntegrationError("cyclic bound input while establishing occurrence routes")
        active.add(key)
        counts[key] = counts.get(key, 0) + 1
        pending.append((node, True))
        pending.extend((child, False) for child in reversed(getattr(node, "_children_", []) or []))
    return counts


def _column_value(column: ColumnBinding, frames: list[tuple[Any, dict[str, str]]]) -> Any:
    node, bindings = frames[column.start]
    for index, shape in enumerate(column.shapes):
        bindings, attributes, grouped = guard_node(shape, node, bindings, current_nil=True)
        if index == len(column.shapes) - 1:
            if column.source.attribute is not None:
                return attributes.get("@" + column.source.attribute)
            return read_scalar(shape, node, bindings, current_nil=True)
        if nil_state(node, current=True):
            return None
        next_shape = column.shapes[index + 1]
        children = grouped[id(next_shape.declaration)]
        if not children:
            return None  # guard_node already enforced activated-group requirements.
        if len(children) != 1:
            shape.fail("multiple occurrences on singleton column route")
        node = children[0]
    raise AssertionError("column must contain its starting shape")


def iter_projected_rows(
    plan: ProjectionPlan,
    document: Document,
    *,
    selector: str | None,
    namespaces: dict[str, str] | None,
) -> Iterator[dict[str, Any]]:
    """Extract one row per selected occurrence from an already prepared document."""
    if document.schema is not plan.schema:
        raise IntegrationError("document belongs to another compiled schema")
    counts = _topology(document.root)
    candidates = (
        [document.root] if selector is None else document.findall(selector, namespaces=namespaces)
    )
    selected = {id(node) for node in candidates}
    if any(counts.get(key, 0) > 1 for key in selected):
        raise IntegrationError("selected occurrence has ambiguous multi-parent reuse")
    accounted: set[int] = set()
    ordinal = 0

    def walk(
        node: Any, depth: int, frames: list[tuple[Any, dict[str, str]]]
    ) -> Iterator[dict[str, Any]]:
        nonlocal ordinal
        shape = plan.row_shapes[depth]
        inherited = frames[-1][1] if frames else {}
        if depth == len(plan.row_shapes) - 1 and id(node) in selected:
            ordinal += 1
            accounted.add(id(node))
            row_frames = [*frames, (node, inherited)]
            row = {}
            for column in plan.columns:
                try:
                    row[column.name] = _column_value(column, row_frames)
                except (IntegrationError, TypeError, ValueError) as exc:
                    raise IntegrationError(
                        f"row {ordinal} column {column.name!r} source {column.source!r} "
                        f"route {column.shapes[-1].path}: {exc}"
                    ) from exc
            try:
                guard_node(shape, node, inherited, current_nil=True)
            except IntegrationError as exc:
                raise IntegrationError(
                    f"row {ordinal} structural route {shape.path}: {exc}"
                ) from exc
            yield row
            return
        bindings, _, grouped = guard_node(shape, node, inherited, current_nil=True)
        frames = [*frames, (node, bindings)]
        if depth < len(plan.row_shapes) - 1 and not nil_state(node, current=True):
            child_shape = plan.row_shapes[depth + 1]
            for child in grouped[id(child_shape.declaration)]:
                yield from walk(child, depth + 1, frames)

    if getattr(document.root, "_descriptor_", None) is not plan.route.declarations[0]:
        raise IntegrationError(f"document root does not match configured route {plan.route.path}")
    yield from walk(document.root, 0, [])
    if selected != accounted:
        raise IntegrationError(
            f"selector candidate does not match configured declaration route {plan.route.path}"
        )
