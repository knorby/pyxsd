"""The declarations a catalog case makes available before it executes.

A W3C case's ``environment`` names the schemas its stylesheet imports or its
sources are validated against, plus the packages and collections the invocation
reaches.  The executor supplies the schemas to the processor at compile time, so
a stylesheet's typed references resolve against the case's own schema set; this
module builds that pool and fingerprints every declaration that can change the
compiled artifact.

Each schema is compiled under the version its own declaration names: the
catalog's default is ``1.0``, which is not the processor default.  Compiling a
corpus schema per case would be prohibitive, so compiled schemas are cached in
one bounded per-process mapping keyed by ``(path, xsd_version)``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, cast

from tests.xslt.catalog import CatalogError
from tests.xslt.model import Schema as SchemaDeclaration
from tests.xslt.model import Source, TestCase

from pyxsd.schema import Schema
from pyxsd.xdm.schema import SchemaPool

#: How many compiled schemas one worker keeps for reuse.  The corpus declares 77
#: distinct ``(path, xsd_version)`` schemas across the whole 3.0 profile, but a
#: case touches only its own few, so a small cache covers every family.
_COMPILED_SCHEMA_CACHE_SIZE = 8

#: Compiled corpus schemas by ``(corpus path, declared xsd_version, resource
#: policy identity)``.  The corpus is pinned and immutable for a run, so for one
#: policy a hit is the same schema the compiler would build, and the entries are
#: shared by every pool that names them.  A policy can serve different bytes for
#: one path, so it is part of the identity; each entry keeps its policy object
#: alive, which keeps a live entry's identity from being recycled by another one.
_COMPILED_SCHEMAS: dict[tuple[str, str, int | None], tuple[Schema, Any]] = {}


def _source_identity(source: Source) -> tuple[str, str]:
    """One collection source's stable identity: its path or inline content."""
    if source.path is not None:
        return ("path", str(source.path))
    return ("content", source.content or "")


def environment_fingerprint(case: TestCase) -> str:
    """A stable digest of every declaration that changes a compiled artifact.

    The fingerprint covers the schema set, the package set, and the collections
    a case declares, sorted so declaration order never matters.  A schema is
    identified by its file, its declared XSD version, its role and its declared
    namespace URI: the role decides which documents the pool composes as roots
    (a ``secondary`` document the principal already includes is not one), and a
    URI is a schema-hint target a source can select.  A case that declares none
    of these has the empty fingerprint, which is also what a case with no
    environment has: both compile against no environment.
    """
    schemas = sorted(
        (
            "" if schema.path is None else str(schema.path),
            schema.role,
            schema.uri or "",
            schema.xsd_version,
        )
        for schema in case.environment.schemas
    )
    packages = sorted(
        (
            "" if package.path is None else str(package.path),
            package.role,
            package.uri or "",
            package.package_version or "",
        )
        for package in case.packages
    )
    collections = sorted(
        (
            collection.uri or "",
            tuple(sorted(_source_identity(source) for source in collection.sources)),
        )
        for collection in case.environment.collections
    )
    if not schemas and not packages and not collections:
        return ""
    payload = json.dumps([schemas, packages, collections], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_schema_pool(
    case: TestCase,
    *,
    xsd_version: str,
    resource_policy: Any = None,
) -> SchemaPool | None:
    """Compile every schema *case* declares and compose one pool.

    *xsd_version* is the processor's version: it is the pool's own version and
    must match any schema the stylesheet imports itself, so the two compose.
    Each declared schema is compiled under the version its declaration names,
    not under the processor's.  A case that declares no schema has no pool.
    """
    declarations = case.environment.schemas
    if not declarations:
        return None
    if case.document_root is None:
        raise CatalogError(f"case {case.name!r} declares schemas without a corpus root")
    compiled = [
        (declaration, _compiled_schema(case, declaration, resource_policy))
        for declaration in declarations
    ]
    # A ``secondary`` schema is a document another declaration includes,
    # imports, or redefines.  Composing it again as its own root would repeat
    # what the principal document already brought in, and a redefined type
    # would then conflict with its own redefinition; it earns its own root only
    # when no other declaration in the case covers its namespace.
    primary = {
        schema.target_namespace
        for declaration, schema in compiled
        if declaration.role != "secondary"
    }
    selected = [
        schema
        for declaration, schema in compiled
        if declaration.role != "secondary" or schema.target_namespace not in primary
    ]
    return SchemaPool(selected, xsd_version=xsd_version, merge_namespaces=True)


def _compiled_schema(
    case: TestCase,
    declaration: SchemaDeclaration,
    resource_policy: Any,
) -> Schema:
    """One declared schema, compiled once per path, version, and policy."""
    if declaration.path is None:
        raise CatalogError(f"case {case.name!r} declares a schema without a file or a namespace")
    assert case.document_root is not None  # checked by build_schema_pool
    path = case.document_root / Path(str(declaration.path))
    identity = None if resource_policy is None else id(resource_policy)
    key = (str(path), declaration.xsd_version, identity)
    cached = _COMPILED_SCHEMAS.get(key)
    if cached is None:
        if not path.is_file():
            raise CatalogError(
                f"the schema {str(declaration.path)!r} declared by case "
                f"{case.name!r} is not in the corpus"
            )
        compiled = Schema.compile(
            path,
            xsd_version=cast(Literal["1.0", "1.1"], declaration.xsd_version),
            resource_policy=resource_policy,
        )
        if len(_COMPILED_SCHEMAS) >= _COMPILED_SCHEMA_CACHE_SIZE:
            _COMPILED_SCHEMAS.pop(next(iter(_COMPILED_SCHEMAS)))
        _COMPILED_SCHEMAS[key] = (compiled, resource_policy)
        return compiled
    return cached[0]
