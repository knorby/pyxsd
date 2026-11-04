"""Compile the showcase schemas: one registry, one policy decision per domain.

Call :func:`compile_schema` once per process and reuse the returned
:class:`pyxsd.schema.Schema`; compilation builds the Python classes and is
worth sharing across parses. The binding policy per domain encodes what
the research pinned down:

- ``citygml`` needs XSD-correct namespace resolution; the official GML
  3.1.1 closure declares ``substitutionGroup`` heads by prefixed QName,
  which only resolves in strict-namespace mode. The stock schema set also
  carries known schema-phase findings (inherent GML 3.1.1 restriction
  quirks) -- callers inspect ``schema.report`` instead of calling
  ``require_valid()`` and the README documents the findings.
- ``bpmn`` compiles cleanly in strict mode with strict namespaces.
- ``opendrive`` documents carry no namespace at all; the default policy
  (legacy namespaces) is the right one, with best-effort values.
- ``ubl`` is a namespaced, all-relative import closure that compiles
  cleanly in strict-namespace mode.
- ``musicxml`` mirrors the long-standing ``examples/musicxml`` example:
  strict namespaces with best-effort values.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from pyxsd import Schema
from pyxsd.binding import BindingPolicy, ParseModes

from .sources import require_cache


@dataclass(frozen=True)
class DomainBinding:
    """How one showcase domain's schema is compiled and found."""

    schema_section: str
    mode: BindingPolicy
    #: Alternate entry files inside the same section (OpenDRIVE ships one
    #: XSD per format version; pick by the instance header's revMinor).
    entries: dict[str, str] | None = None


DOMAINS: dict[str, DomainBinding] = {
    "citygml": DomainBinding("citygml-schema", ParseModes.NAMESPACED),
    "bpmn": DomainBinding("bpmn-schema", ParseModes.STRICT.replace(namespaces="strict")),
    "opendrive": DomainBinding(
        "opendrive-schema",
        ParseModes.LAX,
        entries={
            "1.4": "OpenDRIVE_1.4H.xsd",
            "1.5": "OpenDRIVE_1.5M.xsd",
            "1.7": "opendrive_17_core.xsd",
        },
    ),
    "ubl": DomainBinding("ubl-schema", ParseModes.NAMESPACED),
    "musicxml": DomainBinding("musicxml-schema", ParseModes.LAX.replace(namespaces="strict")),
}


def compile_schema(domain: str, variant: str | None = None, *, fresh: bool = False) -> Schema:
    """Compile (or return a process-cached) schema for one showcase domain.

    ``variant`` selects an alternate entry inside the section; OpenDRIVE
    ships one XSD per format version (``"1.4"``, ``"1.5"``, ``"1.7"``),
    picked from the instance header's revMinor. The caller owns the
    validation verdict: schemas with known stock findings (CityGML) still
    bind documents usefully, so inspect ``schema.report`` rather than
    assuming ``require_valid()`` passes.
    """
    if fresh:
        return _compile(domain, variant)
    return _cached_compile(domain, variant)


@cache
def _cached_compile(domain: str, variant: str | None) -> Schema:
    return _compile(domain, variant)


def _compile(domain: str, variant: str | None) -> Schema:
    binding = DOMAINS[domain]
    path = require_cache(binding.schema_section)
    if variant:
        entry = (binding.entries or {}).get(variant)
        if entry is None:
            raise KeyError(f"unknown {domain} schema variant: {variant!r}")
        path = path.parent / entry
        if not path.is_file():
            raise FileNotFoundError(f"missing {domain} schema variant {variant}: {path}")
    return Schema.compile(path, mode=binding.mode)
