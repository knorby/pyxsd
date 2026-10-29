"""Comparison-specific semantic identity, isolated from core introspection ids.

Core component ids intentionally include source locations because navigation
needs stable per-file addresses. Schema comparison needs the opposite: moving
a schema directory or editing one declaration must not re-identify every other
component. This module derives comparison identities from declaration
structure (owner chains and expanded names) and never from absolute paths.
"""

import hashlib
import json
import posixpath
from dataclasses import asdict
from typing import Any

from pyxsd.introspection import ComponentDescription, SchemaDescription


def _canonical(source: str | None, entry_path: str) -> str:
    if source is None:
        return "<unknown>"
    normalized = posixpath.normpath(source)
    entry = posixpath.normpath(entry_path)
    if normalized == entry:
        return "<entry>"
    root = posixpath.dirname(entry)
    return posixpath.relpath(normalized, root) if root else normalized


def _hash(kind: str, namespace: str | None, owner_key: str | None, role: object) -> str:
    digest = hashlib.sha256(
        json.dumps([kind, namespace, owner_key, role], ensure_ascii=True, default=str).encode()
    ).hexdigest()[:16]
    return f"{kind}@{digest}"


def comparison_keys(description: SchemaDescription, entry_path: str) -> dict[str, str]:
    """Map every component's introspection id to its comparison identity.

    Globals keep their ``kind:{name}`` ids. Named locals derive identity from
    kind, namespace, the owner's identity and the local name, so inserting or
    removing a sibling never renames the others. Anonymous locals use a
    non-positional structural role (canonical source plus declaration path
    step names); genuinely indistinguishable anonymous roles get a source-order
    discriminator so no two components ever share an identity.
    """
    by_id = {component.id: component for component in description.components}
    keys: dict[str, str] = {}
    claimed: set[str] = set()

    def key_of(component: ComponentDescription) -> str:
        existing = keys.get(component.id)
        if existing is not None:
            return existing
        if component.global_declaration and component.name is not None:
            candidate = component.id
        else:
            owner = by_id.get(component.owner_id) if component.owner_id else None
            owner_key = None if owner is None else key_of(owner)
            if component.name is not None:
                candidate = _hash(component.kind, component.namespace, owner_key, component.name)
            else:
                role: object = ("<unknown>",)
                if component.location is not None:
                    role = (
                        _canonical(component.location.source, entry_path),
                        tuple(step.name for step in component.location.path),
                    )
                candidate = _hash(component.kind, component.namespace, owner_key, role)
        result = candidate
        order = 1
        while result in claimed:
            result = f"{candidate}#{order}"
            order += 1
        keys[component.id] = result
        claimed.add(result)
        return result

    for component in description.components:
        key_of(component)
    return keys


def _remap_particle(particle: dict[str, Any] | None, keys: dict[str, str]) -> None:
    if particle is None:
        return
    if particle["component_id"] is not None:
        particle["component_id"] = keys.get(particle["component_id"])
    for child in particle["children"]:
        _remap_particle(child, keys)


def semantic_dict(component: ComponentDescription, keys: dict[str, str]) -> dict[str, Any]:
    """A location-free, identity-remapped view used for content fingerprints."""
    data: dict[str, Any] = asdict(component)
    data.pop("id")
    data.pop("location")
    data["owner_id"] = keys.get(data["owner_id"])
    for member in data["members"]:
        if member["component_id"] is not None:
            member["component_id"] = keys.get(member["component_id"])
    _remap_particle(data["particle"], keys)
    return data


def fingerprint(component: ComponentDescription, keys: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(semantic_dict(component, keys), sort_keys=True, default=str).encode()
    ).hexdigest()
