"""Prolog-only DTD declaration collection.

When a compiled schema can validate ``ENTITY``/``ENTITIES`` values, the
declaration evidence has to come from the document's DTD. This module
collects it with a raw :mod:`xml.parsers.expat` parser that stops at the
first root start tag, so the instance body is never expanded or parsed a
second time. External subsets and entities are never retrieved: an
external subset only makes the collected evidence incomplete.

Denials installed by
:func:`pyxsd.xml_policy.configure_expat_security` keep their existing
behavior and ordering: the collector chains every handler it replaces.
Ordinary prolog parse failures become ``None`` so the ordinary parser can
report them in its own vocabulary; policy denials propagate unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO
from xml.parsers import expat

from pyxsd.xml_policy import XmlParserPolicy, configure_expat_security

#: Bytes fed to Expat per read when *source* is a binary stream.
_CHUNK_SIZE = 65536


@dataclass(frozen=True)
class DocumentDeclarations:
    """Unparsed-entity declaration evidence from one document prolog.

    ``complete`` is ``False`` when the DOCTYPE carries an external
    ``SYSTEM``/``PUBLIC`` subset that may add declarations; such a subset
    is never retrieved. An absent DOCTYPE yields an empty, complete set.
    """

    unparsed_entities: frozenset[str]
    complete: bool


class _PrologComplete(Exception):
    """Raised at the first root start tag to stop the prolog parse."""


def collect_document_declarations(
    source: bytes | BinaryIO, *, policy: XmlParserPolicy
) -> DocumentDeclarations | None:
    """Collect unparsed-entity declarations from *source*'s prolog.

    Only the prolog and internal subset are examined: parsing stops at the
    root start tag, and the instance body is never expanded. External
    subsets and entities are never retrieved. Returns ``None`` when the
    prolog itself is not well-formed, leaving the diagnosis to the
    ordinary parser; policy denials raise as they would without this
    collector.
    """
    parser = expat.ParserCreate()
    configure_expat_security(parser, policy)
    entities: set[str] = set()
    external_subset = False
    # configure_expat_security may have installed policy handlers; chain
    # them so denial behavior and ordering survive the bookkeeping.
    # Typeshed's handler signatures narrow optional Expat arguments, so
    # chain through ``Callable[..., Any]`` exactly as xml_policy does.
    previous_doctype: Callable[..., Any] | None = parser.StartDoctypeDeclHandler
    previous_unparsed: Callable[..., Any] | None = parser.UnparsedEntityDeclHandler

    def record_doctype(
        name: str, sysid: str | None, pubid: str | None, has_internal_subset: bool
    ) -> None:
        nonlocal external_subset
        if previous_doctype is not None:
            previous_doctype(name, sysid, pubid, has_internal_subset)
        if sysid is not None or pubid is not None:
            external_subset = True

    def record_unparsed_entity(
        entity_name: str,
        base: str | None,
        system_id: str | None,
        public_id: str | None,
        notation_name: str | None,
    ) -> None:
        if previous_unparsed is not None:
            previous_unparsed(entity_name, base, system_id, public_id, notation_name)
        # Expat reports an entity here only when it has an NDATA notation.
        entities.add(entity_name)

    parser.StartDoctypeDeclHandler = record_doctype
    parser.UnparsedEntityDeclHandler = record_unparsed_entity
    parser.StartElementHandler = _stop_at_root

    try:
        if isinstance(source, (bytes, bytearray, memoryview)):
            parser.Parse(source, True)
        else:
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    break
                parser.Parse(chunk, False)
            parser.Parse(b"", True)
    except _PrologComplete:
        pass
    except expat.ExpatError:
        return None
    return DocumentDeclarations(frozenset(entities), not external_subset)


def _stop_at_root(*args: object) -> None:
    """End collection: the body after the root start is never parsed."""
    raise _PrologComplete


__all__ = [
    "DocumentDeclarations",
    "collect_document_declarations",
]
