"""Bounded, backend-verified guidance for projectable declaration routes."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import JsonValue
from pyxsd.integrations import IntegrationError
from pyxsd.integrations._shape import particle_elements, resolve_route
from pyxsd.relaxng import RelaxNGSchema
from pyxsd.relaxng.projection import ProjectionPlan, RouteContract
from pyxsd.schema import Schema

from .config import Limits
from .errors import ToolError


def resolve_projection_element(
    schema: Schema | RelaxNGSchema,
    element: str,
    *,
    scope: Literal["route", "start"] = "route",
) -> str:
    """Resolve an element spelling to the backend's route identity.

    XSD keeps :func:`resolve_global_element` behavior exactly: inspection IDs
    and unambiguous global names become Clark declaration names.  RELAX NG
    resolves a route ID, Clark name, EQName, or unambiguous local name to a
    projection route ID.  ``scope="start"`` matches the model/record backend's
    start-only rules: only start routes are considered, local-name fallback is
    applied only there, and a local route ID is an explicit error.  Language
    comes only from the compiled schema object, never from the spelling.

    Ambiguous and missing RELAX NG names raise typed errors carrying candidate
    route IDs; start scope reports only start route IDs.  An invalid RELAX NG
    compilation has no projection routes, so resolution reports that directly
    instead of inventing an XSD declaration.
    """
    if isinstance(schema, Schema):
        return resolve_global_element(schema, element)
    if not isinstance(schema, RelaxNGSchema):
        raise TypeError("resolve_projection_element requires a compiled schema")
    if scope == "start":
        return _resolve_relaxng_start_route(schema, element)
    return _resolve_relaxng_route(schema, element)


def _resolve_relaxng_start_route(schema: RelaxNGSchema, element: str) -> str:
    """Resolve exactly as the standalone model/record start selector does."""
    if schema.report.has_errors:
        raise ToolError(
            "invalid_schema",
            "RELAX NG start resolution requires a valid schema",
            details={"format": "relax-ng"},
        )
    plan = schema.projection
    selector = _eqname_to_clark(element)
    if selector in plan.routes:
        if selector in plan.roots:
            return selector
        raise ToolError(
            "unsupported_projection",
            "RELAX NG start selection requires a start route; pass local steps in path",
            details={
                "format": "relax-ng",
                "route_id": selector,
                "scope": "local",
                "candidates": cast(JsonValue, sorted(plan.roots)),
            },
        )
    uri, local = _split_route_spelling(selector)
    matches = sorted(
        route_id for route_id in plan.roots if plan.routes[route_id].name_class.contains(uri, local)
    )
    if not matches and uri is None:
        matches = sorted(
            route_id
            for route_id in plan.roots
            if (expanded := plan.routes[route_id].expanded_name) is not None
            and expanded.rsplit("}", 1)[-1] == local
        )
    if len(matches) > 1:
        raise ToolError(
            "invalid_argument",
            "RELAX NG start route name is ambiguous; pass a Clark name",
            details={"candidates": cast(JsonValue, matches)},
        )
    if not matches:
        raise _missing_relaxng_route(plan)
    return matches[0]


def _resolve_relaxng_route(schema: RelaxNGSchema, element: str) -> str:
    """Resolve every accepted RELAX NG route spelling to its stable route ID."""
    if schema.report.has_errors:
        raise ToolError(
            "invalid_schema",
            "RELAX NG route resolution requires a valid schema",
            details={"format": "relax-ng"},
        )
    plan = schema.projection
    selector = _eqname_to_clark(element)
    if selector in plan.routes:
        return selector
    try:
        route = plan.resolve_route(selector)
    except ValueError as exc:
        matches = sorted(
            item.route_id for item in plan.routes.values() if item.expanded_name == selector
        )
        raise ToolError(
            "invalid_argument",
            "RELAX NG route name is ambiguous; pass a route ID or Clark name",
            details={"candidates": cast(JsonValue, matches)},
        ) from exc
    except KeyError:
        pass
    else:
        return route.route_id
    uri, local = _split_route_spelling(selector)
    matches = sorted(
        item.route_id
        for item in plan.routes.values()
        if (item.expanded_name is not None and item.expanded_name == selector)
        or (
            item.expanded_name is not None
            and uri is None
            and item.expanded_name.rsplit("}", 1)[-1] == local
        )
        or (item.expanded_name is None and item.name_class.contains(uri, local))
    )
    if len(matches) > 1:
        raise ToolError(
            "invalid_argument",
            "RELAX NG route name is ambiguous; pass a route ID or Clark name",
            details={"candidates": cast(JsonValue, matches)},
        )
    if not matches:
        raise _missing_relaxng_route(plan)
    return matches[0]


def _missing_relaxng_route(plan: ProjectionPlan) -> ToolError:
    """Describe an unmatched route with the admitted start routes as candidates."""
    return ToolError(
        "not_found",
        "RELAX NG route was not found",
        details={"candidates": cast(JsonValue, sorted(plan.roots))},
    )


def _eqname_to_clark(value: str) -> str:
    """Convert an EQName spelling to Clark notation, including empty namespaces."""
    if value.startswith("Q{"):
        value = "{" + value[2:]
    if value.startswith("{"):
        uri, separator, local = value[1:].partition("}")
        if separator and not uri and local and "{" not in local and "}" not in local:
            return local
    return value


def _split_route_spelling(value: str) -> tuple[str | None, str]:
    """Split a Clark spelling without resolving prefixes; empty means no namespace."""
    if value.startswith("{"):
        uri, separator, local = value[1:].partition("}")
        if not separator or not local or "{" in local or "}" in local:
            return None, value
        return uri or None, local
    return None, value


def resolve_global_element(schema: Schema, element: str) -> str:
    """Resolve a user-supplied element name to a global declaration name.

    Accepts the ``element:{...}name`` IDs schema inspection returns, Clark
    notation as-is, and bare local names when exactly one global element has
    that local name. Ambiguous bare names list their candidates.
    """
    from pyxsd.introspection import describe_schema

    if element.startswith("element:"):
        element = element[len("element:") :]
    if "{" in element:
        return element
    roots = describe_schema(schema).roots
    matches = [
        root
        for root in roots
        if root.startswith("element:")
        and (root[len("element:") :] == element or root.endswith("}" + element))
    ]
    if len(matches) > 1:
        raise ToolError(
            "invalid_argument",
            "Element name is ambiguous; pass a Clark name",
            details={"candidates": [str(root) for root in sorted(matches)]},
        )
    if matches:
        return matches[0][len("element:") :]
    # No global matches: pass through unchanged so the registry's own
    # missing/unknown error path reports it.
    return element


@dataclass(frozen=True)
class _Pending:
    declaration: Any
    path: tuple[str, ...]
    ancestors: frozenset[int]
    depth: int


def _starting_point(schema: Schema, element: str, path: tuple[str, ...]) -> _Pending | None:
    """Start at the requested route, or its parent when the leaf is missing."""
    for route_path in (path, path[:-1]):
        try:
            route = resolve_route(schema, element, route_path)
        except IntegrationError:
            continue
        declaration = route.declarations[-1]
        type_id = id(declaration.getType())
        return _Pending(declaration, route_path, frozenset({type_id}), len(route_path))
    return None


def candidate_details(
    schema: Schema | RelaxNGSchema,
    *,
    element: str,
    path: tuple[str, ...],
    prepare: Callable[[str, tuple[str, ...]], object],
    limits: Limits | None = None,
    route_id: str | None = None,
) -> dict[str, JsonValue]:
    """Search descendants of one route with the caller's real projection backend.

    Only candidates the backend accepts are returned. Fixed caps keep the walk
    bounded; ``search_truncated`` is true whenever they prevented an exhaustive
    search, so an empty candidate list never overstates what was searched.
    XSD traverses declaration particles; RELAX NG traverses projection routes
    and their child contracts.  Neither path reads the other language's model.
    A caller that already resolved a RELAX NG start route passes ``route_id`` so
    guidance cannot re-resolve a local-name spelling to a different local route.
    """
    if isinstance(schema, RelaxNGSchema):
        return _relaxng_candidate_details(
            schema,
            element=element,
            path=path,
            prepare=prepare,
            limits=limits,
            route_id=route_id,
        )
    return _xsd_candidate_details(
        schema, element=element, path=path, prepare=prepare, limits=limits
    )


def _xsd_candidate_details(
    schema: Schema,
    *,
    element: str,
    path: tuple[str, ...],
    prepare: Callable[[str, tuple[str, ...]], object],
    limits: Limits | None = None,
) -> dict[str, JsonValue]:
    """Search XSD declaration descendants exactly as before the language split."""
    resolved = resolve_global_element(schema, element)
    details: dict[str, JsonValue] = {
        "element": resolved,
        "path": list(path),
        "projection_candidates": [],
        "searched_declarations": 0,
        "search_truncated": False,
    }
    start = _starting_point(schema, resolved, path)
    if start is None:
        return details

    candidates: list[dict[str, JsonValue]] = []
    searched = 0
    truncated = False
    max_depth = None if limits is None else limits.projection_search_depth
    max_searched = None if limits is None else limits.projection_searched_declarations
    max_candidates = None if limits is None else limits.projection_candidates
    queue: deque[_Pending] = deque([start])

    while queue and not truncated:
        current = queue.popleft()
        children = particle_elements(getattr(current.declaration.getType(), "_contentModel_", None))
        if max_depth is not None and current.depth >= max_depth and children:
            truncated = True
            break
        seen_names: set[str] = set()
        for edge in children:
            descriptor = edge.descriptor
            name = descriptor.instanceName(parser=schema._host)
            if name is None or name in seen_names:
                continue
            seen_names.add(name)
            if max_searched is not None and searched >= max_searched:
                truncated = True
                break
            searched += 1
            child_path = (*current.path, name)
            try:
                prepare(resolved, child_path)
            except IntegrationError:
                pass
            else:
                if max_candidates is None or len(candidates) < max_candidates:
                    candidates.append(
                        {
                            "element": resolved,
                            "path": list(child_path),
                            "repeated": edge.max_occurs is None or edge.max_occurs > 1,
                        }
                    )
                else:
                    truncated = True
            child_type = descriptor.getType()
            if isinstance(child_type, type) and id(child_type) not in current.ancestors:
                queue.append(
                    _Pending(
                        descriptor,
                        child_path,
                        current.ancestors | {id(current.declaration.getType())},
                        current.depth + 1,
                    )
                )

    details["projection_candidates"] = cast(JsonValue, candidates)
    details["searched_declarations"] = searched
    details["search_truncated"] = truncated
    return details


@dataclass(frozen=True)
class _RoutePending:
    route: RouteContract
    path: tuple[str, ...]
    ancestors: frozenset[str]
    depth: int


def _relaxng_candidate_details(
    schema: RelaxNGSchema,
    *,
    element: str,
    path: tuple[str, ...],
    prepare: Callable[[str, tuple[str, ...]], object],
    limits: Limits | None = None,
    route_id: str | None = None,
) -> dict[str, JsonValue]:
    """Search RELAX NG projection-route children with the caller's backend.

    ``element`` is resolved once to a stable route ID.  Candidate paths are
    reported from a start-route spelling so the same model/record backend can
    verify them; a local route ID is anchored through its exact child-name path
    from an admitted start.  Only exact child routes are traversed: a wildcard
    child has no single retry spelling, so it is not offered as a candidate.
    """
    plan = schema.projection
    resolved = route_id if route_id is not None else resolve_projection_element(schema, element)
    route = plan.routes[resolved]
    start_element, base_path = _relaxng_candidate_base(plan, route, element)
    details: dict[str, JsonValue] = {
        "element": start_element,
        "path": list(path),
        "projection_candidates": [],
        "searched_declarations": 0,
        "search_truncated": False,
    }
    start = _relaxng_starting_point(plan, route, base_path=base_path, requested_path=path)
    if start is None:
        return details

    candidates: list[dict[str, JsonValue]] = []
    searched = 0
    truncated = False
    max_depth = None if limits is None else limits.projection_search_depth
    max_searched = None if limits is None else limits.projection_searched_declarations
    max_candidates = None if limits is None else limits.projection_candidates
    queue: deque[_RoutePending] = deque([start])

    while queue and not truncated:
        current = queue.popleft()
        children = tuple(
            field for field in current.route.children.values() if field.child_route_id is not None
        )
        if max_depth is not None and current.depth >= max_depth and children:
            truncated = True
            break
        for field in children:
            name = field.expanded_name
            if max_searched is not None and searched >= max_searched:
                truncated = True
                break
            searched += 1
            child_path = (*current.path, name)
            try:
                prepare(start_element, child_path)
            except IntegrationError:
                pass
            else:
                if max_candidates is None or len(candidates) < max_candidates:
                    candidates.append(
                        {
                            "element": start_element,
                            "path": list(child_path),
                            "repeated": field.cardinality.repeatable,
                            "route_id": field.child_route_id,
                        }
                    )
                else:
                    truncated = True
            child_id = field.child_route_id
            assert child_id is not None
            if child_id not in current.ancestors:
                queue.append(
                    _RoutePending(
                        plan.routes[child_id],
                        child_path,
                        current.ancestors | {current.route.route_id},
                        current.depth + 1,
                    )
                )

    details["projection_candidates"] = cast(JsonValue, candidates)
    details["searched_declarations"] = searched
    details["search_truncated"] = truncated
    return details


def _relaxng_candidate_base(
    plan: ProjectionPlan,
    route: RouteContract,
    requested: str,
) -> tuple[str, tuple[str, ...]]:
    """Return a start spelling and exact child path for the requested route."""
    if route.route_id in plan.roots:
        start_id = route.route_id
        base_path: tuple[str, ...] = ()
    else:
        anchored = _relaxng_route_from_start(plan, route.route_id)
        if anchored is None:
            raise ToolError(
                "invalid_argument",
                "RELAX NG route is not reachable from a start route by exact child names",
                details={"route_id": route.route_id},
            )
        start_id, base_path = anchored
    start_route = plan.routes[start_id]
    if start_route.expanded_name is not None:
        return start_route.expanded_name, base_path
    if requested in plan.routes:
        raise ToolError(
            "invalid_argument",
            "Open-name RELAX NG start routes need an explicit element spelling",
            details={"route_id": start_id, "candidates": cast(JsonValue, sorted(plan.roots))},
        )
    return requested, base_path


def _relaxng_route_from_start(
    plan: ProjectionPlan, route_id: str
) -> tuple[str, tuple[str, ...]] | None:
    """Find the first exact child-name path from any root route."""
    for root_id in plan.roots:
        pending: deque[tuple[str, tuple[str, ...]]] = deque([(root_id, ())])
        seen: set[str] = set()
        while pending:
            current_id, path = pending.popleft()
            if current_id == route_id:
                return root_id, path
            if current_id in seen:
                continue
            seen.add(current_id)
            current = plan.routes[current_id]
            for field in current.children.values():
                assert field.child_route_id is not None
                pending.append((field.child_route_id, (*path, field.expanded_name)))
    return None


def _relaxng_starting_point(
    plan: ProjectionPlan,
    route: RouteContract,
    *,
    base_path: tuple[str, ...],
    requested_path: tuple[str, ...],
) -> _RoutePending | None:
    """Start at the requested path, or its resolvable parent."""
    for route_path in (requested_path, requested_path[:-1]):
        walked = _walk_relaxng_path(plan, route, route_path)
        if walked is None:
            continue
        current, consumed = walked
        full_path = (*base_path, *consumed)
        return _RoutePending(
            current,
            full_path,
            frozenset({current.route_id}),
            len(full_path),
        )
    return None


def _walk_relaxng_path(
    plan: ProjectionPlan,
    route: RouteContract,
    path: tuple[str, ...],
) -> tuple[RouteContract, tuple[str, ...]] | None:
    """Follow exact child names, then uniquely admitting child wildcards."""
    current = route
    consumed: list[str] = []
    for step in path:
        selector = _eqname_to_clark(step)
        field = current.children.get(selector)
        if field is not None:
            assert field.child_route_id is not None
            current = plan.routes[field.child_route_id]
            consumed.append(field.expanded_name)
            continue
        uri, local = _split_route_spelling(selector)
        wildcard = next(
            (
                item
                for item in current.wildcards
                if item.role == "child"
                and item.child_route_id is not None
                and item.name_class.contains(uri, local)
            ),
            None,
        )
        if wildcard is None:
            return None
        assert wildcard.child_route_id is not None
        current = plan.routes[wildcard.child_route_id]
        consumed.append(step)
    return current, tuple(consumed)
