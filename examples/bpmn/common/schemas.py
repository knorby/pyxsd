"""Compile the bpmn schema once per process and reuse it.

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
SCHEMA_SECTION = "bpmn-schema"

#: Binding policy for the closure.
MODE: BindingPolicy = ParseModes.NAMESPACED


def compile_schema(variant: str | None = None, *, fresh: bool = False) -> Schema:
    """Compile (or return a process-cached) schema for this family.

    ``variant`` must be omitted: this family ships a single schema
    entry.
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
    if variant:
        raise KeyError(f"{SCHEMA_SECTION} ships a single schema entry; variants: none")
    return Schema.compile(require_cache(SCHEMA_SECTION), mode=MODE)
