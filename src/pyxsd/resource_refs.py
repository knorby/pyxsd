"""URI-aware resource references, resolved without any retrieval.

Canonical source IDs stay strings and fall into three kinds: local
filesystem paths (including a policy's virtual paths), absolute remote URIs,
and the anonymous ``<memory>`` stream name. A reference resolves with URI
semantics against a remote base and with filesystem semantics otherwise, so
a URL is never passed through ``Path`` and a local path is never read as a
URI. Nothing here opens, fetches, or otherwise touches a resource: whether a
reference may be retrieved is the resource policy's decision.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin

from pyxsd.resource_policy import ResourcePolicyError

#: What a canonical source name denotes.
ResourceKind = Literal["local", "remote", "memory"]

#: The anonymous-stream source name; it has no implicit base.
MEMORY_SOURCE = "<memory>"

#: An RFC 3986 scheme: a letter, then scheme characters, then a colon.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
#: A Windows drive letter is a path, not a URI scheme.
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class ResourceRef:
    """One canonical source name and what kind of resource it denotes."""

    kind: ResourceKind
    value: str


def classify_resource(source: str | os.PathLike[str]) -> ResourceRef:
    """Classify a source name without touching the resource it names.

    Any absolute URI scheme (``https:``, ``http:``, ``file:``, ...) is a
    remote reference: a ``file:`` URI is denied like any other remote
    reference rather than being reinterpreted as a filesystem path. A
    Windows drive letter is a local path despite its colon.
    """
    text = os.fspath(source)
    if text == MEMORY_SOURCE:
        return ResourceRef("memory", text)
    if _SCHEME.match(text) and not _DRIVE_PATH.match(text):
        return ResourceRef("remote", text)
    return ResourceRef("local", text)


def resource_base(source_id: str | os.PathLike[str] | None) -> str | None:
    """The canonical base reference of one document, or ``None``.

    The base of a dependency is its including document, not its directory;
    a local document's identity is its resolved path, a remote document's is
    its URI, and an anonymous stream's is ``<memory>``. ``None`` means the
    caller supplied no source identity at all.
    """
    if source_id is None:
        return None
    reference = classify_resource(source_id)
    if reference.kind == "memory":
        return reference.value
    return resolve_reference(reference.value, base=None)


def resolve_reference(
    location: str | os.PathLike[str], *, base: str | os.PathLike[str] | None
) -> str:
    """Resolve *location* to a canonical reference against *base*.

    *base* is the canonical name of the document that wrote *location*. A
    remote base resolves with RFC 3986 URI semantics (``urljoin``), keeping
    any query and fragment; a local or virtual base resolves with
    filesystem semantics relative to that document. An absolute reference of
    either kind is returned canonicalized rather than being joined onto a
    base of the other kind.

    ``base=None`` keeps the legacy behavior of resolving a relative local
    reference against the working directory (an anonymous stream with no
    policy). A ``<memory>`` base has no directory, so a relative reference
    against it raises :class:`~pyxsd.resource_policy.ResourcePolicyError`
    instead of silently borrowing the process cwd.
    """
    text = os.fspath(location)
    target = classify_resource(text)
    if base is None:
        if target.kind == "remote":
            return text
        return str(Path(text).resolve())
    base_text = os.fspath(base)
    base_ref = classify_resource(base_text)
    if base_ref.kind == "memory":
        if target.kind == "remote":
            return text
        if not Path(text).is_absolute():
            raise ResourcePolicyError("a relative schema dependency needs a named source base")
        return str(Path(text).resolve())
    if base_ref.kind == "remote":
        return urljoin(base_text, text)
    if target.kind == "remote":
        return text
    path = Path(text)
    if path.is_absolute():
        return str(path.resolve())
    return str((Path(base_text).parent / path).resolve())


def require_local_reference(reference: str, *, purpose: str, advisory: bool = False) -> str:
    """Return *reference* if it names a local resource, else deny it.

    Used where no policy can authorize retrieval: a URL must fail rather
    than be opened as a filesystem path. *purpose* names what was being
    loaded for the denial message. With *advisory*, the reference is one a
    schema document itself wrote (an ``xs:include``/``xs:import``
    ``schemaLocation``), which XSD treats as a hint: an unavailable remote
    document is reported like any other unavailable document, so a
    :class:`FileNotFoundError` carries the denial into that reporting.
    Anything the caller or an instance supplied raises
    :class:`~pyxsd.resource_policy.ResourcePolicyError` instead, because
    silently downgrading an ungranted retrieval to a hint would hide it.
    """
    classified = classify_resource(reference)
    if classified.kind == "remote":
        message = (
            f"the {purpose} reference '{reference}' is remote; "
            "remote loading needs a remote-capable resource policy"
        )
        if advisory:
            raise FileNotFoundError(message)
        raise ResourcePolicyError(message)
    return classified.value


__all__ = [
    "MEMORY_SOURCE",
    "ResourceKind",
    "ResourceRef",
    "classify_resource",
    "require_local_reference",
    "resolve_reference",
    "resource_base",
]
