"""Bounded, backend-verified guidance for projectable declaration routes."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from pydantic import JsonValue
from pyxsd.integrations import IntegrationError
from pyxsd.integrations._shape import particle_elements, resolve_route
from pyxsd.schema import Schema

from .errors import ToolError

_MAX_DEPTH = 8
_MAX_SEARCHED = 100
_MAX_CANDIDATES = 10


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
        return _Pending(declaration, route_path[1:], frozenset({type_id}), len(route_path[1:]))
    return None


def candidate_details(
    schema: Schema,
    *,
    element: str,
    path: tuple[str, ...],
    prepare: Callable[[str, tuple[str, ...]], object],
) -> dict[str, JsonValue]:
    """Search descendants of one route with the caller's real projection backend.

    Only candidates the backend accepts are returned. Fixed caps keep the walk
    bounded; ``search_truncated`` is true whenever they prevented an exhaustive
    search, so an empty candidate list never overstates what was searched.
    """
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
    queue: deque[_Pending] = deque([start])

    while queue and not truncated:
        current = queue.popleft()
        children = particle_elements(getattr(current.declaration.getType(), "_contentModel_", None))
        if current.depth >= _MAX_DEPTH and children:
            truncated = True
            break
        seen_names: set[str] = set()
        for edge in children:
            descriptor = edge.descriptor
            name = descriptor.instanceName(parser=schema._host)
            if name is None or name in seen_names:
                continue
            seen_names.add(name)
            if searched >= _MAX_SEARCHED:
                truncated = True
                break
            searched += 1
            child_path = (*current.path, name)
            try:
                prepare(resolved, child_path)
            except IntegrationError:
                pass
            else:
                if len(candidates) < _MAX_CANDIDATES:
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
