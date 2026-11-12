"""XSLT 3.0 imported schemas and package-local schema pools.

This module turns ``xsl:import-schema`` declarations into compiled schema pools
and attaches each pool to its own package.  It validates inline and referenced
schema documents, applies import precedence per target namespace, adapts an
explicit resource policy, and checks that separately compiled package pools can
still form one consistent schema.  It does not compose packages themselves.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, replace
from functools import cmp_to_key
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation
from pyxsd.namespaces import NamespaceContext
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.resource_refs import classify_resource, resolve_reference
from pyxsd.xdm.document import element_from_xdm
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xslt.compiler.components import _error
from pyxsd.xslt.compiler.static import EffectiveElement, EffectiveModule
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.precedence import compare_precedence

if TYPE_CHECKING:
    from pyxsd.xslt.compiler.packages import CompiledPackage

_XSD_NS = "http://www.w3.org/2001/XMLSchema"
_XSD_SCHEMA = f"{{{_XSD_NS}}}schema"
_IMPORT_SCHEMA = "{http://www.w3.org/1999/XSL/Transform}import-schema"


@dataclass(frozen=True, slots=True)
class _SchemaImport:
    """One authored ``xsl:import-schema`` declaration.

    ``inline`` is the raw XDM ``xs:schema`` element when the declaration is
    inline; otherwise ``locations`` holds the resolved-at-compile
    ``schema-location`` tokens and ``base_uri`` is the declaration's base.
    """

    namespace: str | None
    locations: tuple[str, ...]
    inline: Any
    base_uri: str | None
    precedence: tuple[int, ...]
    module: str | None
    location: IssueLocation | None


def _parse_schema_import(element: EffectiveElement, module: EffectiveModule) -> _SchemaImport:
    """Validate and record one ``xsl:import-schema`` declaration."""
    namespace = element.attribute("namespace")
    if namespace is not None:
        namespace = namespace.strip()
        if not namespace:
            raise _error(
                "the namespace attribute of xsl:import-schema must not be empty",
                "XTSE0020",
                element=element,
                module=module,
            )
    schema_location = element.attribute("schema-location")
    inline = tuple(child for child in element.elements if child.name == _XSD_SCHEMA)
    if len(inline) > 1:
        raise _error(
            "xsl:import-schema accepts at most one inline xs:schema",
            "XTSE0010",
            element=element,
            module=module,
        )
    if inline and schema_location is not None:
        raise _error(
            "xsl:import-schema must not have both an inline xs:schema and a "
            "schema-location attribute",
            "XTSE0215",
            element=element,
            module=module,
        )
    if inline:
        inline_element = inline[0]
        target = inline_element.attribute("targetNamespace")
        if namespace is not None and target is None:
            raise _error(
                "the namespace attribute requires the inline schema to declare "
                "the same targetNamespace",
                "XTSE0215",
                element=element,
                module=module,
            )
        if namespace is not None and target is not None and namespace != target:
            raise _error(
                "the namespace attribute conflicts with the inline schema's targetNamespace",
                "XTSE0215",
                element=element,
                module=module,
            )
        if namespace is None:
            namespace = target
        if inline_element.raw is None:  # pragma: no cover - authored declarations have raw nodes
            raise _error(
                "the inline xs:schema has no source tree",
                "XTSE0220",
                element=element,
                module=module,
            )
        return _SchemaImport(
            namespace=namespace,
            locations=(),
            inline=inline_element.raw,
            base_uri=element.base_uri,
            precedence=module.precedence,
            module=module.identity,
            location=element.source_location(),
        )
    locations = tuple((schema_location or "").split())
    return _SchemaImport(
        namespace=namespace,
        locations=locations,
        inline=None,
        base_uri=element.base_uri,
        precedence=module.precedence,
        module=module.identity,
        location=element.source_location(),
    )


def _package_closure(package: CompiledPackage) -> tuple[CompiledPackage, ...]:
    """Return *package* and every used package, once each."""
    seen: set[tuple[str | None, str]] = set()
    result: list[CompiledPackage] = []
    stack = [package]
    while stack:
        current = stack.pop()
        key = (current.name, current.version)
        if key in seen:
            continue
        seen.add(key)
        result.append(current)
        stack.extend(current.dependencies)
    return tuple(result)


def _attach_schema_pools(
    package: CompiledPackage,
    *,
    xsd_version: str,
    resource_policy: Any,
) -> CompiledPackage:
    """Attach each package's own imported-schema pool to the package graph."""
    dependencies = tuple(
        _attach_schema_pools(dependency, xsd_version=xsd_version, resource_policy=resource_policy)
        for dependency in package.dependencies
    )
    pool = _compile_schema_imports(
        package,
        xsd_version=xsd_version,
        resource_policy=resource_policy,
    )
    return replace(package, dependencies=dependencies, schema_pool=pool)


def _check_schema_consistency(package: CompiledPackage) -> None:
    """Require all packages' imported components to form one valid schema."""
    pools = [
        current.schema_pool
        for current in _package_closure(package)
        if current.schema_pool is not None
    ]
    if len(pools) < 2:
        return
    schemas = [schema for pool in pools for schema in pool.raw_schemas]
    try:
        SchemaPool(schemas, xsd_version=pools[0].xsd_version, merge_namespaces=True)
    except ValueError as exc:
        raise XsltStaticError(
            str(exc),
            code="XTSE0220",
            uri=package.modules[0].identity if package.modules else None,
            location=package.location,
            cause=exc,
        ) from exc


def _compile_schema_imports(
    package: CompiledPackage,
    *,
    xsd_version: str,
    resource_policy: Any,
) -> SchemaPool | None:
    """Compile one package's own ``xsl:import-schema`` declarations."""
    declarations = list(package.schema_imports)
    if not declarations:
        return None

    compiled: list[tuple[str | None, tuple[int, ...], Any]] = []
    for declaration in declarations:
        for schema in _compile_schema_declaration(
            declaration,
            xsd_version=xsd_version,
            resource_policy=resource_policy,
        ):
            if schema.report.has_errors:
                raise XsltStaticError(
                    f"the schema imported by xsl:import-schema is not valid: "
                    f"{schema.report.error_count} error(s)",
                    code="XTSE0220",
                    uri=declaration.module,
                    location=declaration.location,
                )
            if (
                declaration.namespace is not None
                and schema.target_namespace != declaration.namespace
            ):
                raise XsltStaticError(
                    f"the imported schema targets {schema.target_namespace!r}, not the "
                    f"declared namespace {declaration.namespace!r}",
                    code="XTSE0220",
                    uri=declaration.module,
                    location=declaration.location,
                )
            compiled.append((schema.target_namespace, declaration.precedence, schema))

    by_namespace: dict[str | None, list[tuple[tuple[int, ...], Any]]] = {}
    for namespace, precedence, schema in compiled:
        by_namespace.setdefault(namespace, []).append((precedence, schema))
    selected: list[Any] = []
    for entries in by_namespace.values():
        highest = max(
            (precedence for precedence, _schema in entries),
            key=cmp_to_key(compare_precedence),
        )
        selected.extend(schema for precedence, schema in entries if precedence == highest)
    if not selected:
        return None

    try:
        return SchemaPool(selected, xsd_version=xsd_version, merge_namespaces=True)
    except ValueError as exc:
        raise XsltStaticError(
            str(exc),
            code="XTSE0220",
            uri=package.modules[0].identity if package.modules else None,
            location=package.location,
            cause=exc,
        ) from exc


def _compile_schema_declaration(
    declaration: _SchemaImport,
    *,
    xsd_version: str,
    resource_policy: Any,
) -> tuple[Any, ...]:
    """Compile one declaration's inline or referenced schema documents."""
    from pyxsd.schema import Schema

    version = cast(Literal["1.0", "1.1"], xsd_version)
    policy = _schema_resource_policy(resource_policy)
    if declaration.inline is not None:
        context = NamespaceContext()
        element = element_from_xdm(declaration.inline, context)
        base = (
            None if declaration.base_uri is None else _local_schema_reference(declaration.base_uri)
        )
        try:
            return (
                Schema.compile(
                    element,
                    xsd_version=version,
                    namespace_context=context,
                    resource_policy=policy,
                    base_uri=base,
                ),
            )
        except ResourcePolicyError as exc:
            raise XsltStaticError(
                f"the inline schema dependency could not be loaded: {exc}",
                code="XTSE1650",
                uri=declaration.module,
                location=declaration.location,
                cause=exc,
            ) from exc
        except PyXSDError as exc:
            raise XsltStaticError(
                f"the inline schema could not be compiled: {exc}",
                code="XTSE0220",
                uri=declaration.module,
                location=declaration.location,
                cause=exc,
            ) from exc
    result: list[Any] = []
    for location in declaration.locations:
        try:
            target = _local_schema_reference(resolve_reference(location, base=declaration.base_uri))
            kind = classify_resource(target).kind
            if kind == "local" and not os.path.exists(target):
                # ``schema-location`` is a hint.  A missing document is not
                # an error unless a stylesheet reference later needs a name
                # from its namespace.
                continue
            if kind == "remote" and policy is None:
                continue
            result.append(
                Schema.compile(
                    target,
                    xsd_version=version,
                    resource_policy=policy,
                )
            )
        except ResourcePolicyError as exc:
            raise XsltStaticError(
                f"the schema document {location!r} could not be loaded: {exc}",
                code="XTSE1650",
                uri=declaration.module,
                location=declaration.location,
                cause=exc,
            ) from exc
        except PyXSDError as exc:
            raise XsltStaticError(
                f"the schema document {location!r} could not be loaded: {exc}",
                code="XTSE0220",
                uri=declaration.module,
                location=declaration.location,
                cause=exc,
            ) from exc
    return tuple(result)


def _local_schema_reference(reference: str) -> str:
    """Turn a ``file:`` URI into the local path pyxsd's policy contract uses."""
    parts = urlsplit(reference)
    if parts.scheme != "file":
        return reference
    from pathlib import Path
    from urllib.request import url2pathname

    return str(Path(url2pathname(parts.path)).resolve())


def _schema_resource_policy(policy: Any) -> Any:
    """Adapt an explicit read-only policy to pyxsd's compile contract."""
    if policy is None:
        return None
    if callable(getattr(policy, "parse", None)):
        return policy
    if not callable(getattr(policy, "read", None)):
        raise TypeError("a resource policy must provide read()")
    return _ReadOnlySchemaPolicy(policy)


class _ReadOnlySchemaPolicy:
    """Give a read-only policy pyxsd's parsing half without a second engine."""

    def __init__(self, policy: Any) -> None:
        self._policy = policy

    def read(self, source: str, *, base: str | None, purpose: str) -> Any:
        return self._policy.read(source, base=base, purpose=purpose)

    def parse(self, data: Any, context: NamespaceContext) -> Any:
        from pyxsd.namespaces import parse_with_namespaces

        return parse_with_namespaces(io.BytesIO(data.content), context)
