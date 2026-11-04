"""Compile the citygml schema once per process and reuse it.

The binding policy encodes what the research pinned down for this
family's schema closure; see the family README for the findings.
Compilation builds the Python classes and is worth sharing across
parses.
"""

from __future__ import annotations

from functools import cache

from pyxsd import Schema
from pyxsd.binding import BindingPolicy, ParseModes

from .sources import require_cache

#: Manifest section that carries this family's schema closure.
SCHEMA_SECTION = "citygml-schema"

#: Binding policy for the closure.
MODE: BindingPolicy = ParseModes.NAMESPACED

#: Alternate entry files inside the same section (OpenDRIVE ships one XSD
#: per format version; pick by the instance header's revMinor); empty when
#: the family ships a single schema entry.
ENTRIES: dict[str, str] = {}


def compile_schema(variant: str | None = None, *, fresh: bool = False) -> Schema:
    """Compile (or return a process-cached) schema for this family.

    ``variant`` selects an alternate entry inside the section.
    The caller owns the validation verdict: schemas with known stock
    findings still bind documents usefully, so inspect ``schema.report``
    rather than assuming ``require_valid()`` passes.
    """
    if fresh:
        return _compile(variant)
    return _cached_compile(variant)


@cache
def _cached_compile(variant: str | None) -> Schema:
    return _compile(variant)


def _compile(variant: str | None) -> Schema:
    path = require_cache(SCHEMA_SECTION)
    if variant:
        entry = ENTRIES.get(variant)
        if entry is None:
            raise KeyError(f"unknown {SCHEMA_SECTION} schema variant: {variant!r}")
        path = path.parent / entry
        if not path.is_file():
            raise FileNotFoundError(f"missing schema variant {variant}: {path}")
    return Schema.compile(path, mode=MODE)
