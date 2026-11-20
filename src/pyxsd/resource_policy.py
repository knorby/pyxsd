"""Opt-in resource loading and XML parser hooks, without optional dependencies.

A policy is trusted application code, not a sandbox. Every dependency read uses
the same policy. Denials should raise ResourcePolicyError, not OSError (which
schema composition treats as an unavailable resource).
"""

from __future__ import annotations

import os
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Final, Literal, Protocol, cast
from xml.etree import ElementTree as ET

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import NamespaceContext
from pyxsd.validation import ValidationReport

if TYPE_CHECKING:
    from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

#: Why a resource is being read.
#:
#: ``"schema"`` is an explicitly supplied schema, a schema a caller mapped for
#: a namespace, or an ordinary schema dependency; ``"schema-hint"`` is a schema
#: an instance document selected through ``xsi:schemaLocation`` or
#: ``xsi:noNamespaceSchemaLocation``; ``"instance"`` is an instance document;
#: ``"text"`` is a raw text resource read by a stylesheet; and
#: ``"collection"`` names an application-supplied collection of nodes.
ResourcePurpose = Literal["schema", "schema-hint", "instance", "text", "collection"]

_PURPOSES: Final = frozenset(("schema", "schema-hint", "instance", "text", "collection"))


def validate_purpose(purpose: str) -> ResourcePurpose:
    """Return *purpose* if it is a known resource purpose, else raise.

    Internal callers pass the purpose of a read through several layers; an
    unknown value is a programming error rather than an authorization
    decision, so it is rejected before a policy can act on it.
    """
    if purpose not in _PURPOSES:
        raise ValueError(
            f"unknown resource purpose {purpose!r}; expected 'schema', 'schema-hint', "
            "'instance', 'text', or 'collection'"
        )
    return cast(ResourcePurpose, purpose)


class ResourcePolicyError(PyXSDError):
    """A resource/parser policy refused input; never a missing-resource hint."""


@dataclass(frozen=True)
class ResourceData:
    """Original bytes and their canonical source name, before tree rewriting."""

    source_id: str
    content: bytes


class ResourcePolicy(Protocol):
    """Application-controlled loading and parsing for a schema and its instances.

    Source IDs are canonical strings: a local path (including a virtual path
    a policy defines, such as ``/workspace/...``), an absolute remote URI, or
    ``<memory>`` for an anonymous stream. A dependency's base is its
    including document, not its directory: a policy resolves a remote base
    with URI semantics and any other base with its own filesystem semantics.
    ``purpose`` is a :data:`ResourcePurpose`: ``"schema"`` for an explicitly
    supplied schema, a caller's namespace mapping, and an ordinary schema
    dependency; ``"schema-hint"`` for a schema an instance document selected
    through ``xsi:schemaLocation`` or ``xsi:noNamespaceSchemaLocation``; and
    ``"instance"`` for an instance document. Parsers must populate the
    supplied namespace context. Anonymous streams use ``<memory>`` and have
    no implicit filesystem base.
    """

    def read(self, source: str, *, base: str | None, purpose: ResourcePurpose) -> ResourceData:
        """Authorize and read a named resource, retaining its original bytes."""
        ...

    def parse(self, data: ResourceData, context: NamespaceContext) -> ET.Element:
        """Parse admitted bytes and record namespace evidence."""
        ...


def new_validation_report(policy: ResourcePolicy | None) -> ValidationReport:
    """Use a policy's optional report budget, preserving unlimited core defaults."""
    factory = getattr(policy, "new_report", None)
    return ValidationReport() if factory is None else factory()


def source_identifier(source: object) -> str | None:
    """Name a path or named stream without substituting the process cwd."""
    name = (
        os.fspath(source)
        if isinstance(source, (str, os.PathLike))
        else getattr(source, "name", None)
    )
    return name if isinstance(name, str) else None


def is_root_read_source(source: object) -> bool:
    """Whether *source* is an explicit path whose first policy read is the root.

    An anonymous stream has no implicit base, so a policy compilation entered
    for it must still let its dependencies authorize local-to-remote loading.
    """
    return isinstance(source, (str, os.PathLike))


@contextmanager
def policy_compilation(policy: ResourcePolicy | None, *, root_read: bool) -> Iterator[None]:
    """Enter a policy's optional per-compilation closure scope.

    :class:`~pyxsd.remote.RemoteResourcePolicy` advertises the hook through the
    private ``_pyxsd_compilation_scope`` capability marker, not through the
    public ``compilation()`` method's spelling, so a custom policy may define
    an unrelated ``compilation`` attribute without breaking compilation. A
    policy that wants the same per-compilation state publishes the marker
    itself. ``root_read`` is false for an anonymous stream schema, whose
    dependencies must still authorize local-to-remote loading.
    """
    enter = getattr(policy, "_pyxsd_compilation_scope", None)
    if enter is None:
        yield
    else:
        with enter(root_read=root_read):
            yield


def reject_parser_override(
    resource_policy: ResourcePolicy | None,
    xml_policy: XmlParserPolicy | None,
    xml_limits: XmlLimits | None,
) -> None:
    """Refuse parser arguments that a trusted ResourcePolicy would swallow.

    Shared by every schema compiler so a policy/parser combination is rejected
    identically, before any input is read.
    """
    if resource_policy is not None and (xml_policy is not None or xml_limits is not None):
        raise ValueError(
            "xml_policy and xml_limits cannot be combined with resource_policy: "
            "ResourcePolicy.parse() is trusted application code"
        )


def read_resource(
    source: str | os.PathLike[str] | IO[str] | IO[bytes] | ET.Element,
    policy: ResourcePolicy,
    *,
    purpose: ResourcePurpose,
    base: str | None = None,
) -> ResourceData:
    """Load paths through the policy; streams still pass through its parser."""
    checked = validate_purpose(purpose)
    if isinstance(source, ET.Element):
        raise ResourcePolicyError("an unverified Element cannot bypass resource policy parsing")
    if isinstance(source, (str, os.PathLike)):
        return policy.read(os.fspath(source), base=base, purpose=checked)
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
