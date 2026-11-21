"""Authorized RELAX NG resource loading and canonical dependency closure.

A compilation owns one :class:`ResourceLoader`.  Every read goes through the
caller's :class:`~pyxsd.resource_policy.ResourcePolicy` when one is supplied;
otherwise only a local, caller-supplied resource is opened, and a remote
reference is a policy denial rather than a filesystem path.

References resolve against the containing resource and its accumulated
``xml:base``, never against a process-global directory.  Canonical resources
are deduplicated while every distinct reference edge is retained as evidence
for cache identity.
"""

from __future__ import annotations

import hashlib
import io
import os
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal, cast
from urllib.parse import unquote, urljoin
from xml.etree import ElementTree as ET

from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
from pyxsd.relaxng.uris import is_uri_reference, xlink_escape
from pyxsd.resource_policy import (
    ResourceData,
    ResourcePolicy,
    ResourcePolicyError,
    ResourcePurpose,
    read_resource,
    source_identifier,
    validate_purpose,
)
from pyxsd.resource_refs import (
    MEMORY_SOURCE,
    classify_resource,
    require_local_reference,
    resolve_reference,
)
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

__all__ = [
    "LoadedCompactResource",
    "LoadedResource",
    "ResourceDependency",
    "ResourceLoader",
    "ResourceReference",
    "SchemaResource",
    "resolve_xml_base",
]

#: The source forms a RELAX NG compilation accepts.  An already-built
#: :class:`xml.etree.ElementTree.Element` is deliberately rejected: it cannot
#: carry the namespace and policy evidence a policy's parser records.
ResourceSource = str | os.PathLike[str] | IO[str] | IO[bytes]

#: The syntaxes one loaded resource can be read as.  A compilation selects
#: one for its root and every include/external dependency inherits it.
ResourceSyntax = Literal["xml", "compact"]


@dataclass(frozen=True)
class ResourceReference:
    """One authored reference edge inside the dependency closure.

    ``reference`` is the authored value, ``base`` the effective (possibly
    ``xml:base``-adjusted) base it resolved against, ``containing_id`` the
    resource that wrote it, and ``canonical_id`` the resource it reached.
    Multiple edges to one canonical resource prove why that resource is in
    the closure; they do not create duplicate dependencies.
    """

    canonical_id: str
    containing_id: str
    reference: str
    base: str | None
    xml_base: str | None
    location: IssueLocation | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class ResourceDependency:
    """One canonical resource in a compilation's complete dependency closure.

    ``source_id`` is the policy's canonical name for the bytes (normally the
    same as ``canonical_id``), ``purpose`` the authorized read purpose, and
    ``content_sha256`` a content identity that does not depend on a checkout
    path.  ``references`` records every authored edge that reached it.
    """

    canonical_id: str
    source_id: str
    purpose: ResourcePurpose
    content_sha256: str
    references: tuple[ResourceReference, ...] = ()


@dataclass(frozen=True, eq=False, repr=False)
class LoadedResource:
    """One parsed resource with its namespace evidence and source origins.

    Identity is per loader: :meth:`ResourceLoader.load` and
    :meth:`ResourceLoader.resolve` return the same object for one canonical
    resource, so object identity distinguishes a resource that was actually
    read from one that merely shares its name.
    """

    canonical_id: str
    root: ET.Element
    namespaces: NamespaceContext
    origins: OriginTable

    def __repr__(self) -> str:
        return f"LoadedResource(canonical_id={self.canonical_id!r})"

    @property
    def syntax(self) -> Literal["xml"]:
        """The syntax this resource was read as."""
        return "xml"

    def with_namespace(self, namespace: str) -> LoadedResource:
        """A read-context view with an inherited RELAX NG ``ns``.

        Transfer happens before the full-syntax reader resolves names and
        QName values. Only the root is copied; namespace evidence for its
        children is retained, and the loader's cached authored tree is never
        changed. An explicit root ``ns`` always wins.
        """
        if "ns" in self.root.attrib or not namespace:
            return self
        root = copy(self.root)
        root.attrib = dict(self.root.attrib)
        root.set("ns", namespace)
        context = NamespaceContext()
        for node in self.root.iter():
            context.record(node, self.namespaces.bindings_for(node))
        context.record(root, self.namespaces.bindings_for(self.root))
        return LoadedResource(self.canonical_id, root, context, self.origins)

    def namespace_at(self, origins: OriginTable) -> str:
        """The inherited RELAX NG namespace at an authored reference."""
        origin = next(iter(origins))
        assert origin.location is not None
        node = self.root
        namespace = node.get("ns", "")
        for step in origin.location.path[1:]:
            node = [child for child in node if child.tag == step.name][step.index - 1]
            namespace = node.get("ns", namespace)
        return namespace


@dataclass(frozen=True, eq=False, repr=False)
class LoadedCompactResource:
    """One admitted compact-syntax resource, parsed after namespace views.

    ``content`` is the exact admitted original input.  Parsing is deferred
    until a view has applied its inherited namespace, because one canonical
    compact resource may be referenced with different inherited namespaces.
    ``namespaces`` and ``origins`` are empty until a reader parses the view.
    Identity is per loader, exactly as for :class:`LoadedResource`.
    """

    canonical_id: str
    content: bytes
    namespaces: NamespaceContext
    origins: OriginTable
    inherited_namespace: str = ""

    def __repr__(self) -> str:
        return f"LoadedCompactResource(canonical_id={self.canonical_id!r})"

    @property
    def syntax(self) -> Literal["compact"]:
        """The syntax this resource is read as."""
        return "compact"

    def with_namespace(self, namespace: str) -> LoadedCompactResource:
        """A view whose compact environment inherits *namespace*.

        An explicit compact namespace declaration still wins; the inherited
        value applies only where the compact environment says ``inherit``.
        The admitted bytes and every piece of evidence are shared, and the
        loader's cached resource is never changed.
        """
        if not namespace or namespace == self.inherited_namespace:
            return self
        return LoadedCompactResource(
            self.canonical_id,
            self.content,
            self.namespaces,
            self.origins,
            namespace,
        )

    def namespace_at(self, origins: OriginTable) -> str:
        """The namespace a compact reference without ``ns`` inherits."""
        return self.inherited_namespace


#: Any resource a loader admits; consumers select behavior through ``syntax``.
SchemaResource = LoadedResource | LoadedCompactResource


def _validate_syntax(syntax: str) -> ResourceSyntax:
    """Return *syntax* if known, else reject it before any resource is read."""
    if syntax not in ("xml", "compact"):
        raise ValueError(
            f"unknown RELAX NG resource syntax {syntax!r}; expected 'xml' or 'compact'"
        )
    return cast(ResourceSyntax, syntax)


@dataclass
class _DependencyRecord:
    source_id: str
    purpose: ResourcePurpose
    content_sha256: str
    references: list[ResourceReference] = field(default_factory=list)


def _is_directory_base(base: str) -> bool:
    """Whether a local base names a directory rather than a document."""
    return base.endswith(("/", os.sep))


def _normalize_local(path: str, *, trailing: bool) -> str:
    """Resolve a local path, preserving a trailing directory separator."""
    resolved = str(Path(path).resolve())
    if trailing and not _is_directory_base(resolved):
        resolved += "/"
    return resolved


def _decode_local(reference: str) -> str:
    """Decode an XLink-escaped local URI reference to a filesystem path.

    RELAX NG hands the escaped URI reference to the resolver.  A local
    filesystem resolver maps that reference back to bytes, so a Unicode or
    space-bearing name reaches the file it names rather than a literal
    ``%HH`` name.  Malformed UTF-8 escapes are a resource error, never a
    silently different path.
    """
    try:
        return unquote(reference, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ResourcePolicyError(
            f"the local schema reference {reference!r} is not valid UTF-8"
        ) from exc


def _join_local(base: str, reference: str) -> str:
    """Resolve *reference* against a local base with RFC 3986 merge rules.

    The merged path is formed before dot segments are removed, and it is a
    directory when its final segment is empty, ``.``, or ``..``.  This keeps
    ``xml:base="."`` a directory instead of collapsing it to the containing
    file's basename.
    """
    reference = _decode_local(reference)
    if Path(reference).is_absolute():
        merged = reference
    else:
        directory = base if _is_directory_base(base) else str(Path(base).parent)
        separator = "" if _is_directory_base(directory) else os.sep
        merged = f"{directory}{separator}{reference}"
    final_segment = merged.replace(os.sep, "/").rsplit("/", 1)[-1]
    return _normalize_local(merged, trailing=final_segment in ("", ".", ".."))


def resolve_xml_base(base: str, xml_base: str) -> str:
    """Resolve an ``xml:base`` value against an already-effective base.

    The result keeps directory semantics: a base ending in ``/`` is a
    directory, not the file basename before the slash.  A remote base uses
    RFC 3986 joining; an absolute local or remote ``xml:base`` replaces the
    current base.  An anonymous memory base cannot resolve a relative value
    because it has no directory.  The value is transformed by the XLink 1.0
    section 5.4 escaping procedure first, as RELAX NG section 4.5 requires.

    A base URI has no fragment component (RFC 3986 section 5.1), so a raw
    ``#`` is rejected before it can become a filesystem character.  A
    percent-encoded ``%23`` remains a legal filename character.
    """
    escaped = xlink_escape(xml_base)
    if "#" in escaped:
        raise ResourcePolicyError("a RELAX NG xml:base value must not use a fragment identifier")
    if not is_uri_reference(escaped):
        raise ResourcePolicyError("a RELAX NG xml:base value must be a legal URI reference")
    return _resolve_against(base, escaped)


def _resolve_against(base: str, reference: str) -> str:
    """Resolve one resource reference against an effective base."""
    if reference == "":
        return base
    target = classify_resource(reference)
    base_kind = classify_resource(base).kind
    if target.kind == "remote":
        return reference
    if base_kind == "remote":
        return urljoin(base, reference)
    if base_kind == "memory":
        if Path(reference).is_absolute():
            return _join_local(base, reference)
        raise ResourcePolicyError("a relative schema dependency needs a named source base")
    return _join_local(base, reference)


def _origin_table(root: ET.Element, canonical_id: str) -> OriginTable:
    """Record one element origin for every occurrence in *root*."""
    origins: set[SourceOrigin] = set()
    stack: list[tuple[ET.Element, tuple[PathStep, ...]]] = [(root, (PathStep(root.tag, 1),))]
    while stack:
        node, path = stack.pop()
        origins.add(SourceOrigin(canonical_id, IssueLocation(canonical_id, path)))
        counts: dict[str, int] = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            stack.append((child, (*path, PathStep(child.tag, counts[child.tag]))))
    return OriginTable(frozenset(origins))


class ResourceLoader:
    """Load, resolve, and record the RELAX NG resource closure.

    ``policy`` authorizes every non-caller-supplied read.  ``xml_policy`` and
    ``xml_limits`` configure parsing when no policy is present; a policy owns
    parsing when it is, because its parser is what records namespace evidence.
    """

    def __init__(
        self,
        policy: ResourcePolicy | None = None,
        *,
        xml_policy: XmlParserPolicy | None = None,
        xml_limits: XmlLimits | None = None,
    ) -> None:
        self._policy = policy
        self._xml_policy = xml_policy
        self._xml_limits = xml_limits
        self._resources: dict[str, SchemaResource] = {}
        self._dependencies: dict[str, _DependencyRecord] = {}
        self._anonymous = 0

    def load(
        self,
        source: ResourceSource | ET.Element,
        *,
        purpose: ResourcePurpose = "schema",
        syntax: ResourceSyntax = "xml",
    ) -> SchemaResource:
        """Read and admit one explicitly supplied resource.

        A stream is read once through the policy's admission path.  An
        unnamed stream has no implicit base; each such stream is a distinct
        resource so two anonymous schemas cannot be conflated.  A stream
        whose name is supplied keeps that name as its source identity.

        An XML resource is parsed immediately.  A compact resource retains
        its admitted bytes so parsing can happen after a namespace view is
        applied.  One canonical resource keeps the syntax it was first
        loaded as; a compilation never mixes the two.
        """
        checked = validate_purpose(purpose)
        checked_syntax = _validate_syntax(syntax)
        requested = self._requested_id(source)
        if requested is not None:
            cached = self._resources.get(requested)
            if cached is not None:
                self._check_cached_syntax(cached, checked_syntax)
                return cached
        data = self._read(source, requested, checked)
        canonical = self._canonical_id(data, requested)
        existing = self._resources.get(canonical)
        if existing is not None:
            self._check_cached_syntax(existing, checked_syntax)
            self._remember_alias(requested, canonical, existing)
            return existing
        if checked_syntax == "compact":
            resource: SchemaResource = LoadedCompactResource(
                canonical_id=canonical,
                content=data.content,
                namespaces=NamespaceContext(),
                origins=OriginTable(),
            )
        else:
            context = NamespaceContext()
            root = self._parse(data, context)
            resource = LoadedResource(
                canonical_id=canonical,
                root=root,
                namespaces=context,
                origins=_origin_table(root, canonical),
            )
        self._resources[canonical] = resource
        self._remember_alias(requested, canonical, resource)
        self._dependencies[canonical] = _DependencyRecord(
            source_id=data.source_id,
            purpose=checked,
            content_sha256=hashlib.sha256(data.content).hexdigest(),
        )
        return resource

    def resolve(
        self,
        reference: str,
        containing: SchemaResource,
        base: str | None,
        *,
        location: IssueLocation | None = None,
    ) -> SchemaResource:
        """Load *reference* against *containing* and its effective *base*.

        ``base`` is already normalized: it includes every in-scope
        ``xml:base`` and is never parsed as URI text a second time.  Only the
        authored reference is XLink-escaped and resolved.  A raw ``#`` in
        that authored reference is a URI fragment and rejects retrieval
        before any dot-segment or filesystem normalization, so a ``%23``
        escape remains a legal filename character.

        The dependency is loaded as the containing resource's syntax; a
        suffix never selects a syntax.  A missing resource raises the
        ordinary ``OSError`` or policy denial; a malformed XML resource
        raises :class:`xml.etree.ElementTree.ParseError`.  Composition
        decides whether such a required dependency is a schema finding.  A
        failed read records no dependency.
        """
        reference = xlink_escape(reference)
        if "#" in reference:
            raise ResourcePolicyError(
                "a RELAX NG include or externalRef href must not use a fragment "
                "identifier for XML retrieval"
            )
        reference_kind = classify_resource(reference).kind
        absolute = reference_kind == "remote" or (
            reference_kind == "local" and Path(reference).is_absolute()
        )
        resolved_base = containing.canonical_id
        if not absolute and base is not None and base != "":
            resolved_base = base
        canonical = _resolve_against(resolved_base, reference)
        cached = self._resources.get(canonical)
        if cached is not None:
            self._check_cached_syntax(cached, containing.syntax)
            resource = cached
        else:
            resource = self.load(canonical, syntax=containing.syntax)
        self._record_reference(
            containing_id=containing.canonical_id,
            reference=reference,
            base=resolved_base,
            xml_base=base,
            canonical_id=resource.canonical_id,
            location=location,
        )
        return resource

    def dependencies(self) -> tuple[ResourceDependency, ...]:
        """Return the complete, deterministic dependency closure.

        The explicitly supplied root is a dependency too, so cache identity
        covers the whole compilation input.  Each canonical resource appears
        once; every distinct authored reference edge is retained.
        """
        result: list[ResourceDependency] = []
        for canonical, record in sorted(self._dependencies.items()):
            references = tuple(
                sorted(
                    set(record.references),
                    key=lambda item: (
                        item.containing_id,
                        item.reference,
                        item.base or "",
                        item.xml_base or "",
                        item.canonical_id,
                        tuple((step.name, step.index) for step in item.location.path)
                        if item.location is not None
                        else (),
                    ),
                )
            )
            result.append(
                ResourceDependency(
                    canonical_id=canonical,
                    source_id=record.source_id,
                    purpose=record.purpose,
                    content_sha256=record.content_sha256,
                    references=references,
                )
            )
        return tuple(result)

    def _requested_id(self, source: ResourceSource | ET.Element) -> str | None:
        if isinstance(source, (str, os.PathLike)):
            return resolve_reference(os.fspath(source), base=None)
        if isinstance(source, ET.Element):
            return None
        name = source_identifier(source)
        if name:
            return name
        self._anonymous += 1
        if self._anonymous == 1:
            return MEMORY_SOURCE
        return f"<memory#{self._anonymous}>"

    def _read(
        self,
        source: ResourceSource | ET.Element,
        requested: str | None,
        purpose: ResourcePurpose,
    ) -> ResourceData:
        if self._policy is not None:
            return read_resource(source, self._policy, purpose=purpose)
        if isinstance(source, ET.Element):
            raise ResourcePolicyError("an unverified Element cannot bypass resource policy parsing")
        if isinstance(source, (str, os.PathLike)):
            identifier = resolve_reference(os.fspath(source), base=None)
            local = require_local_reference(identifier, purpose=purpose)
            return ResourceData(identifier, Path(local).read_bytes())
        content = source.read()
        payload = content.encode("utf-8") if isinstance(content, str) else content
        return ResourceData(requested or MEMORY_SOURCE, payload)

    def _canonical_id(self, data: ResourceData, requested: str | None) -> str:
        if data.source_id == MEMORY_SOURCE and requested is not None and requested != MEMORY_SOURCE:
            return requested
        return cast(str, data.source_id or requested)

    def _parse(self, data: ResourceData, context: NamespaceContext) -> ET.Element:
        if self._policy is not None:
            return self._policy.parse(data, context)
        return parse_with_namespaces(
            io.BytesIO(data.content),
            context,
            policy=self._xml_policy,
            limits=self._xml_limits,
        )

    def _check_cached_syntax(self, resource: SchemaResource, syntax: ResourceSyntax) -> None:
        if resource.syntax != syntax:
            raise ValueError(
                f"resource {resource.canonical_id!r} is already loaded as "
                f"{resource.syntax!r} syntax, not {syntax!r}"
            )

    def _remember_alias(
        self,
        requested: str | None,
        canonical: str,
        resource: SchemaResource,
    ) -> None:
        if requested is not None and requested != canonical:
            self._resources[requested] = resource

    def _record_reference(
        self,
        *,
        containing_id: str,
        reference: str,
        base: str,
        xml_base: str | None,
        canonical_id: str,
        location: IssueLocation | None,
    ) -> None:
        record = self._dependencies[canonical_id]
        edge = ResourceReference(
            canonical_id=canonical_id,
            containing_id=containing_id,
            reference=reference,
            base=base,
            xml_base=xml_base,
            location=location,
        )
        if edge not in record.references:
            record.references.append(edge)
