"""Expanded-name classes with set semantics.

A RELAX NG name class denotes a set of expanded names.  The concrete classes
mirror the simple syntax: an exact :class:`Name`, the :class:`AnyName` and
:class:`NsName` wildcards, a union :class:`NameChoice`, and the
:class:`ExceptName` difference used by ``anyName``/``nsName`` exclusions.

A namespace URI is represented as ``str | None`` where ``None`` is the absence
of a namespace.  XML Namespaces treats the empty string as that same absence,
so both forms normalize to ``None``.  Local names are compared literally; an
empty local name is an ordinary member, never a wildcard.

:meth:`NameClass.overlaps` answers whether the two sets share any expanded
name.  It is symmetric by construction and decides the question over a finite
set of representative names: every namespace and local name mentioned by
either class, the absence of a namespace, and one deterministic fresh value
per dimension.  Name classes can distinguish names only by equality, so an
unmentioned value is interchangeable with every other unmentioned value.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

__all__ = [
    "AnyName",
    "ExceptName",
    "Name",
    "NameChoice",
    "NameClass",
    "NsName",
]

_FRESH_NAMESPACE = "urn:pyxsd:relaxng:unmentioned-namespace"
_FRESH_LOCAL = "pyxsd-unmentioned-local"


def _namespace(uri: str | None) -> str | None:
    """Normalize an absent or empty namespace URI to ``None``."""
    return uri or None


def _fresh(mentioned: Collection[object], seed: str) -> str:
    """Return a deterministic string that is not one of *mentioned*."""
    candidate = seed
    while candidate in mentioned:
        candidate += "\x00"
    return candidate


def _mention(
    name_class: NameClass,
    namespaces: set[str | None],
    local_names: set[str],
) -> None:
    """Record every namespace and local name *name_class* names literally."""
    if isinstance(name_class, Name):
        namespaces.add(name_class.uri)
        local_names.add(name_class.local)
    elif isinstance(name_class, NsName):
        namespaces.add(name_class.uri)
    elif isinstance(name_class, NameChoice):
        for alternative in name_class.alternatives:
            _mention(alternative, namespaces, local_names)
    elif isinstance(name_class, ExceptName):
        _mention(name_class.base, namespaces, local_names)
        _mention(name_class.excluded, namespaces, local_names)
    elif not isinstance(name_class, AnyName):
        raise TypeError(f"unknown RELAX NG name class {type(name_class).__name__}")


class NameClass:
    """A set of expanded names.

    Subclasses implement :meth:`contains`; overlap is derived from membership.
    """

    def contains(self, uri: str | None, local: str) -> bool:
        """Return whether the expanded name is a member of this class."""
        raise NotImplementedError("a name class must implement contains()")

    def overlaps(self, other: NameClass) -> bool:
        """Return whether this class and *other* share any expanded name."""
        namespaces: set[str | None] = {None}
        local_names: set[str] = set()
        _mention(self, namespaces, local_names)
        _mention(other, namespaces, local_names)
        fresh_namespace = _fresh(namespaces, _FRESH_NAMESPACE)
        fresh_local = _fresh(local_names, _FRESH_LOCAL)
        return any(
            self.contains(uri, local) and other.contains(uri, local)
            for uri in (*namespaces, fresh_namespace)
            for local in (*local_names, fresh_local)
        )


@dataclass(frozen=True)
class Name(NameClass):
    """Exactly one expanded name."""

    uri: str | None
    local: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "uri", _namespace(self.uri))

    def contains(self, uri: str | None, local: str) -> bool:
        return self.uri == _namespace(uri) and self.local == local


@dataclass(frozen=True)
class AnyName(NameClass):
    """Every expanded name."""

    def contains(self, uri: str | None, local: str) -> bool:
        return True


@dataclass(frozen=True)
class NsName(NameClass):
    """Every expanded name in one namespace, including the no-namespace one."""

    uri: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "uri", _namespace(self.uri))

    def contains(self, uri: str | None, local: str) -> bool:
        return self.uri == _namespace(uri)


@dataclass(frozen=True)
class NameChoice(NameClass):
    """The union of two or more name classes."""

    alternatives: tuple[NameClass, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "alternatives", tuple(self.alternatives))

    def contains(self, uri: str | None, local: str) -> bool:
        return any(alternative.contains(uri, local) for alternative in self.alternatives)


@dataclass(frozen=True)
class ExceptName(NameClass):
    """The names in *base* that are not in *excluded*."""

    base: NameClass
    excluded: NameClass

    def contains(self, uri: str | None, local: str) -> bool:
        return self.base.contains(uri, local) and not self.excluded.contains(uri, local)
