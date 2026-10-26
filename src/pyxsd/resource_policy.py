"""Opt-in resource loading and XML parser hooks, without optional dependencies.

A policy is trusted application code, not a sandbox. Every dependency read uses
the same policy. Denials should raise ResourcePolicyError, not OSError (which
schema composition treats as an unavailable resource).
"""

from __future__ import annotations

import os
import weakref
from dataclasses import dataclass
from typing import IO, Protocol
from xml.etree import ElementTree as ET

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import NamespaceContext


class ResourcePolicyError(PyXSDError):
    """A resource/parser policy refused input; never a missing-resource hint."""


@dataclass(frozen=True)
class ResourceData:
    """Original bytes and their canonical source name, before tree rewriting."""

    source_id: str
    content: bytes


class ResourcePolicy(Protocol):
    """Application-controlled loading and parsing for a schema and its instances.

    Source IDs are canonical filesystem-shaped names (including virtual paths).
    A dependency's base is its including document, not its directory. Parsers
    must populate the supplied namespace context. Anonymous streams use
    ``<memory>`` and have no implicit filesystem base.
    """

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        """Authorize and read a named resource, retaining its original bytes."""
        ...

    def parse(self, data: ResourceData, context: NamespaceContext) -> ET.Element:
        """Parse admitted bytes and record namespace evidence."""
        ...


def source_identifier(source: object) -> str | None:
    """Name a path or named stream without substituting the process cwd."""
    name = (
        os.fspath(source)
        if isinstance(source, (str, os.PathLike))
        else getattr(source, "name", None)
    )
    return name if isinstance(name, str) else None


def read_resource(
    source: str | os.PathLike[str] | IO[str] | IO[bytes] | ET.Element,
    policy: ResourcePolicy,
    *,
    purpose: str,
    base: str | None = None,
) -> ResourceData:
    """Load paths through the policy; streams still pass through its parser."""
    if isinstance(source, ET.Element):
        raise ResourcePolicyError("an unverified Element cannot bypass resource policy parsing")
    if isinstance(source, (str, os.PathLike)):
        return policy.read(os.fspath(source), base=base, purpose=purpose)
    content = source.read()
    return ResourceData(
        source_identifier(source) or "<memory>",
        content.encode("utf-8") if isinstance(content, str) else content,
    )


def record_declarations(
    root: ET.Element,
    source: str | None,
    origins: weakref.WeakKeyDictionary[ET.Element, IssueLocation],
) -> None:
    """Record original declaration addresses before composition moves nodes."""
    stack: list[tuple[ET.Element, tuple[PathStep, ...]]] = [(root, (PathStep(root.tag, 1),))]
    while stack:
        node, path = stack.pop()
        origins[node] = IssueLocation(source, path)
        counts: dict[str, int] = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            stack.append((child, (*path, PathStep(child.tag, counts[child.tag]))))
