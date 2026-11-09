"""Source-faithful XSLT module loading and canonical module identity.

The loader reads the principal stylesheet and its ``xsl:include``/
``xsl:import`` graph through the ordinary resource seams.  Each physical
module is parsed once into an :class:`~pyxsd.xdm.nodes.XdmDocument`, which
retains comments, processing instructions, namespace declarations, ``xml:base``
resolution, and per-element source locations.  The include/import graph is
kept separate from later static-resource graphs, and canonical identity drives
both cache reuse and cycle detection.

Inclusion elements carrying ``use-when``, a shadow ``href``, or an ancestor
``use-when`` are deliberately left unresolved here: the static preprocessor
must evaluate their normative restricted static context before deciding
whether the resource is needed at all.  An excluded include therefore never
retrieves its target.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import url2pathname

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation
from pyxsd.resource_policy import (
    ResourceData,
    ResourcePolicy,
    ResourcePolicyError,
    source_identifier,
)
from pyxsd.resource_refs import MEMORY_SOURCE, classify_resource, require_local_reference
from pyxsd.source_spans import SourceIndex
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy
from pyxsd.xslt.errors import XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ModuleKind = Literal["include", "import"]

_MODULE_ELEMENTS = frozenset({"stylesheet", "transform", "package"})
_INCLUDE = f"{{{XSLT_NS}}}include"
_IMPORT = f"{{{XSLT_NS}}}import"


def _local_name(name: str | None) -> str:
    if name and name.startswith("{"):
        return name.partition("}")[2]
    return name or ""


def _namespace_of(name: str | None) -> str | None:
    if name and name.startswith("{"):
        return name[1:].partition("}")[0]
    return None


def _conditional(element: XdmNode) -> bool:
    """Whether this element's inclusion depends on static preprocessing.

    Only XSLT elements reach this helper: the module root and direct
    ``xsl:include``/``xsl:import`` children.  A namespaced ``xsl:use-when``
    is still checked because it is the legal spelling on those elements.
    """
    return any(
        element.attribute(name) is not None
        for name in ("use-when", f"{{{XSLT_NS}}}use-when", "_use-when")
    )


def _resolve_reference(href: str, base_uri: str | None) -> str:
    """Resolve one include/import href against the containing element base."""
    target = href.strip()
    if not target:
        raise ResourcePolicyError("a stylesheet reference must not be empty")
    joined = urljoin(base_uri, target) if base_uri else target
    parts = urlsplit(joined)
    if parts.scheme == "file":
        return str(Path(url2pathname(parts.path)).resolve())
    if parts.scheme:
        return urlunsplit(parts._replace(fragment=""))
    if base_uri is None and not os.path.isabs(target):
        raise ResourcePolicyError("a relative stylesheet reference needs a base URI")
    return str(Path(joined).resolve())


def _canonical_identity(reference: str | None) -> str | None:
    """Return one canonical identity for a source name, or ``None``."""
    if reference is None or reference.startswith(MEMORY_SOURCE):
        return None
    classified = classify_resource(reference)
    parts = urlsplit(reference)
    if classified.kind == "local":
        return Path(reference).resolve().as_uri()
    if parts.scheme == "file":
        return Path(url2pathname(parts.path)).resolve().as_uri()
    return urlunsplit(parts._replace(fragment=""))


def _document_uri(reference: str | None) -> str | None:
    if reference is None or reference.startswith(MEMORY_SOURCE):
        return None
    classified = classify_resource(reference)
    if classified.kind == "remote":
        return reference
    return Path(reference).resolve().as_uri()


def _value(element: XdmNode, name: str) -> str | None:
    attribute = element.attribute(name)
    return None if attribute is None else attribute.string_value


@dataclass(frozen=True, slots=True, eq=False)
class SourceModule:
    """One parsed stylesheet module and its directly authored graph edges.

    ``document`` is the raw, source-faithful tree.  ``includes`` and
    ``imports`` contain the modules already resolved by the loader;
    conditional edges remain for :class:`~pyxsd.xslt.compiler.static.
    StaticPreprocessor` to evaluate and resolve.
    """

    source_id: str
    identity: str | None
    document: XdmDocument
    root: XdmNode
    simplified: bool
    version: str | None
    includes: tuple[SourceModule, ...]
    imports: tuple[SourceModule, ...]
    loader: ModuleLoader
    resolved_references: Mapping[int, SourceModule] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resolved_references",
            MappingProxyType(dict(self.resolved_references)),
        )

    @property
    def base_uri(self) -> str | None:
        """The module root's base URI after ``xml:base`` resolution."""
        return self.root.base_uri

    @property
    def uri(self) -> str | None:
        """Alias for the canonical module identity."""
        return self.identity

    def find(self, name: str) -> XdmNode | None:
        """Find a directly authored declaration by component name or element name."""
        for child in self.root.children:
            if child.kind is not NodeKind.ELEMENT:
                continue
            if child.name == name:
                return child
            if _value(child, "name") == name:
                return child
            if child.local_name == name:
                return child
        return None

    def attribute(self, name: str) -> str | None:
        """Return one effective raw attribute value on the module root."""
        return _value(self.root, name)

    def source_location(self, name: str) -> IssueLocation | None:
        """Return the source location of a direct declaration named *name*."""
        element = self.find(name)
        return None if element is None else element.source_location()


class ModuleLoader:
    """Load source-faithful stylesheet modules once per canonical identity."""

    def __init__(
        self,
        resource_policy: ResourcePolicy | None = None,
        *,
        xml_policy: XmlParserPolicy | None = None,
        xml_limits: XmlLimits | None = None,
    ) -> None:
        if resource_policy is not None and (xml_policy is not None or xml_limits is not None):
            raise ResourcePolicyError(
                "a resource policy and an explicit parser policy or limits are mutually "
                "exclusive; the policy owns resource handling"
            )
        self.resource_policy = resource_policy
        self._xml_policy = xml_policy
        self._xml_limits = xml_limits
        self._cache: dict[str, SourceModule] = {}
        self._loading: list[tuple[str, ModuleKind | None]] = []
        self._anonymous = 0

    def load(self, source: Any) -> SourceModule:
        """Load one principal stylesheet module and its directly resolvable graph."""
        source_id = self._source_id(source)
        identity = _canonical_identity(source_id)
        if identity is not None and identity in self._cache:
            return self._cache[identity]
        return self._load_module(
            source,
            source_id=source_id,
            identity=identity,
            via=None,
            owner_uri=identity,
            location=None,
        )

    def resolve_reference(
        self,
        kind: ModuleKind,
        href: str,
        *,
        base_uri: str | None,
        owner_uri: str | None,
        location: IssueLocation | None,
    ) -> SourceModule:
        """Resolve one conditional include/import after static preprocessing."""
        try:
            target = _resolve_reference(href, base_uri)
        except (OSError, PyXSDError) as exc:
            raise XsltStaticError(
                f"cannot resolve stylesheet module {href!r}: {exc}",
                code="XTSE0165",
                uri=owner_uri,
                location=location,
                cause=exc,
            ) from exc
        identity = _canonical_identity(target)
        if identity is not None and identity in self._cache:
            return self._cache[identity]
        return self._load_module(
            target,
            source_id=target,
            identity=identity,
            via=kind,
            owner_uri=owner_uri,
            location=location,
        )

    # -- reading and parsing ---------------------------------------------------

    def _source_id(self, source: Any) -> str:
        if isinstance(source, (str, os.PathLike)):
            return os.fspath(source)
        name = source_identifier(source)
        if name is not None:
            return name
        self._anonymous += 1
        return f"{MEMORY_SOURCE}:{self._anonymous}"

    def _read(self, source: Any, *, source_id: str) -> tuple[bytes, str]:
        if self.resource_policy is not None:
            target = os.fspath(source) if isinstance(source, (str, os.PathLike)) else source
            data = self.resource_policy.read(target, base=None, purpose="schema")
            if not isinstance(data, ResourceData):  # pragma: no cover - policy contract
                raise TypeError("a resource policy read must return ResourceData")
            return data.content, data.source_id or source_id
        if isinstance(source, (str, os.PathLike)):
            path = require_local_reference(os.fspath(source), purpose="schema")
            with open(path, "rb") as stream:
                return stream.read(), path
        content = source.read()
        if isinstance(content, str):
            return content.encode("utf-8"), source_id
        return bytes(content), source_id

    def _load_module(
        self,
        source: Any,
        *,
        source_id: str,
        identity: str | None,
        via: ModuleKind | None,
        owner_uri: str | None,
        location: IssueLocation | None,
    ) -> SourceModule:
        if identity is not None:
            for loading_identity, loading_kind in self._loading:
                if loading_identity == identity:
                    if (loading_kind or via) == "import":
                        code = "XTSE0210"
                        message = "a stylesheet module directly or indirectly imports itself"
                    else:
                        code = "XTSE0180"
                        message = "a stylesheet module directly or indirectly includes itself"
                    raise XsltStaticError(
                        message,
                        code=code,
                        uri=identity,
                        location=location,
                    )
            self._loading.append((identity, via))
        try:
            try:
                content, actual_id = self._read(source, source_id=source_id)
            except (OSError, PyXSDError) as exc:
                raise XsltStaticError(
                    f"cannot retrieve stylesheet module {source_id!r}: {exc}",
                    code="XTSE0165",
                    uri=owner_uri,
                    location=location,
                    cause=exc,
                ) from exc

            identity = _canonical_identity(actual_id) or identity
            document_uri = _document_uri(actual_id)
            try:
                index = SourceIndex(
                    actual_id,
                    content,
                    policy=self._xml_policy,
                    limits=self._xml_limits,
                )
                document = XdmDocument.parse(
                    content,
                    source_index=index,
                    source_name=actual_id,
                    document_uri=document_uri,
                    base_uri=document_uri,
                )
            except (PyXSDError, OSError) as exc:
                raise XsltStaticError(
                    f"cannot parse stylesheet module {actual_id!r}: {exc}",
                    code="XTSE0165",
                    uri=_canonical_identity(actual_id) or owner_uri,
                    location=location,
                    cause=exc,
                ) from exc

            module = self._build_module(
                source_id=actual_id,
                identity=_canonical_identity(actual_id) or identity,
                document=document,
            )
            if module.identity is not None:
                self._cache[module.identity] = module
            return module
        finally:
            if identity is not None and self._loading:
                self._loading.pop()

    def _build_module(
        self,
        *,
        source_id: str,
        identity: str | None,
        document: XdmDocument,
    ) -> SourceModule:
        root = document.root_element
        namespace = _namespace_of(root.name)
        local = _local_name(root.name)
        if namespace == XSLT_NS:
            if local not in _MODULE_ELEMENTS:
                raise XsltStaticError(
                    f"unsupported stylesheet module root {local!r}",
                    code="XTSE0010",
                    uri=identity,
                    location=root.source_location(),
                )
            simplified = False
            version = _value(root, "version")
        else:
            version = _value(root, f"{{{XSLT_NS}}}version")
            if version is None:
                raise XsltStaticError(
                    "a simplified stylesheet module requires an xsl:version attribute",
                    code="XTSE0150",
                    uri=identity,
                    location=root.source_location(),
                )
            simplified = True

        resolved: dict[int, SourceModule] = {}
        if not simplified and not _conditional(root):
            for child in root.children:
                if child.kind is not NodeKind.ELEMENT:
                    continue
                if child.name not in (_INCLUDE, _IMPORT):
                    continue
                kind: ModuleKind = "include" if child.name == _INCLUDE else "import"
                if _conditional(child) or child.attribute("_href") is not None:
                    continue
                href = _value(child, "href")
                if href is None:
                    continue
                resolved[child.node_id] = self.resolve_reference(
                    kind,
                    href,
                    base_uri=child.base_uri,
                    owner_uri=identity,
                    location=child.source_location(),
                )

        includes = tuple(
            resolved[child.node_id]
            for child in root.children
            if child.node_id in resolved and child.name == _INCLUDE
        )
        imports = tuple(
            resolved[child.node_id]
            for child in root.children
            if child.node_id in resolved and child.name == _IMPORT
        )
        return SourceModule(
            source_id=source_id,
            identity=identity,
            document=document,
            root=root,
            simplified=simplified,
            version=version,
            includes=includes,
            imports=imports,
            loader=self,
            resolved_references=resolved,
        )


__all__ = ["ModuleKind", "ModuleLoader", "SourceModule"]
