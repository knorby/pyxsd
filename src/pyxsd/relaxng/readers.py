"""Cycle-free reader dispatch between the RELAX NG syntax frontends.

One compilation selects a syntax for its root before any resource is read, and
every dependency inherits the containing resource's syntax.  This module owns
that selection and the one dispatch point from an admitted
:class:`~pyxsd.relaxng.resources.SchemaResource` to the unresolved full-syntax
tree; the resource loader stays syntax-neutral and the XML reader stays
XML-only.
"""

from __future__ import annotations

import os
from typing import Literal, cast
from urllib.parse import urlsplit

from pyxsd.relaxng.compact import read_compact_syntax
from pyxsd.relaxng.limits import BudgetCounter
from pyxsd.relaxng.resources import (
    LoadedCompactResource,
    ResourceSyntax,
    SchemaResource,
)
from pyxsd.relaxng.syntax import FullSchema, read_full_syntax
from pyxsd.resource_policy import source_identifier
from pyxsd.resource_refs import classify_resource

__all__ = ["SchemaSyntax", "read_schema_syntax", "select_root_syntax"]

#: The caller's root-frontend selection; ``auto`` examines only the source name.
SchemaSyntax = Literal["auto", "xml", "compact"]

_SCHEMA_SYNTAXES = ("auto", "xml", "compact")


def _validate_schema_syntax(syntax: object) -> SchemaSyntax:
    """Return a known root selection, or reject it before any read."""
    if syntax not in _SCHEMA_SYNTAXES:
        raise ValueError(
            f"unknown RELAX NG schema syntax {syntax!r}; expected 'auto', 'xml', or 'compact'"
        )
    return cast(SchemaSyntax, syntax)


def _named_path(name: str) -> str:
    """The path component that selects a syntax for a local or remote name."""
    if classify_resource(name).kind == "remote":
        return urlsplit(name).path
    return os.fspath(name)


def select_root_syntax(
    source: object,
    syntax: SchemaSyntax = "auto",
) -> ResourceSyntax:
    """Select the root reader without reading or inspecting its contents.

    An explicit ``xml`` or ``compact`` always wins.  ``auto`` selects compact
    only for a named source whose path component ends in ``.rnc``,
    case-insensitively; a URL's query and fragment never participate.
    Anonymous streams and every other named source stay XML.
    """
    selected = _validate_schema_syntax(syntax)
    if selected != "auto":
        return selected
    name = source_identifier(source)
    if name is None:
        return "xml"
    return "compact" if _named_path(name).lower().endswith(".rnc") else "xml"


def read_schema_syntax(resource: SchemaResource, *, budget: BudgetCounter) -> FullSchema:
    """Read one admitted resource through the frontend its syntax selects.

    Both readers build the same unresolved full-syntax tree.  The budget is
    supplied by the compilation so nested compact resources cannot reset a
    cumulative token count.
    """
    if isinstance(resource, LoadedCompactResource):
        return read_compact_syntax(resource, budget=budget)
    return read_full_syntax(resource)
