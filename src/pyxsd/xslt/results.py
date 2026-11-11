"""Immutable transformation results and in-memory result-tree construction.

The raw principal and secondary sequences are always retained.  Tree delivery
is a separate view built directly from XDM nodes, never by serializing and
reparsing XML.  Secondary results live only in memory: producing a result never
writes a file or touches the network.

Construction itself lives in :mod:`pyxsd.xslt.runtime.builder`, which owns the
node factory, sequence normalization, and transactional builder state.  This
module keeps the immutable public result views and re-exports the construction
seams consumed by the processor.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from pyxsd.exceptions import PyXSDError
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xdm.values import XdmItem, XdmSequence
from pyxsd.xslt.runtime.builder import (
    _append_complex_content as _append_complex_content,
)
from pyxsd.xslt.runtime.builder import (
    _NodeFactory as _NodeFactory,
)
from pyxsd.xslt.runtime.builder import (
    _prefix_for as _prefix_for,
)
from pyxsd.xslt.runtime.builder import build_result_tree

_build_result_tree = build_result_tree

__all__ = [
    "Message",
    "ResultEntry",
    "ResultShapeError",
    "TransformationResult",
]


class ResultShapeError(PyXSDError):
    """The requested result view does not have the required shape.

    A raw sequence of atomics, maps, or arrays is a valid principal result but
    is not a document tree.  Tree delivery must be requested explicitly (or by
    the stylesheet's output defaults); the raw sequence is never silently
    wrapped.
    """


@dataclass(frozen=True, slots=True)
class Message:
    """One stylesheet message or diagnostic event."""

    text: str
    code: str | None = None
    uri: str | None = None
    terminate: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("a message text must be a string")
        for label, value in (("code", self.code), ("uri", self.uri)):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"a message {label} must be a string or None")
        if not isinstance(self.terminate, bool):
            raise TypeError("a message terminate flag must be a boolean")


def _absolute_uri(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty absolute URI string")
    if not urlsplit(value).scheme:
        raise ValueError(f"{label} must be an absolute URI, got {value!r}")
    return value


def _freeze_parameters(parameters: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(parameters, Mapping):
        raise TypeError("serialization parameters must be a mapping")
    frozen: dict[str, str] = {}
    for key, value in parameters.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("serialization parameters must map strings to strings")
        frozen[key] = value
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class ResultEntry:
    """One principal or secondary result in every available view.

    ``raw`` is the exact XDM sequence.  ``tree`` is the delivered document when
    tree construction was requested and possible.  ``serialized`` is reserved
    for an explicitly requested serialization (Task 16 never serializes), while
    ``serialization`` records the effective output parameters.  ``output_uri``
    identifies the principal result or the absolute URI of a secondary result.
    """

    raw: XdmSequence
    output_uri: str | None = None
    tree: XdmDocument | None = None
    serialized: str | None = None
    serialization: Mapping[str, str] = field(default_factory=dict)
    messages: tuple[Message, ...] = ()

    def __post_init__(self) -> None:
        raw = tuple(self.raw)
        if not all(isinstance(item, XdmItem) for item in raw):
            raise TypeError("a result raw sequence must contain XDM items")
        object.__setattr__(self, "raw", raw)
        if self.output_uri is not None:
            _absolute_uri(self.output_uri, "output_uri")
        if self.tree is not None and not isinstance(self.tree, XdmDocument):
            raise TypeError("a result tree must be an XdmDocument or None")
        if self.serialized is not None and not isinstance(self.serialized, str):
            raise TypeError("a serialized result must be a string or None")
        object.__setattr__(self, "serialization", _freeze_parameters(self.serialization))
        messages = tuple(self.messages)
        if not all(isinstance(message, Message) for message in messages):
            raise TypeError("result messages must be Message values")
        object.__setattr__(self, "messages", messages)

    @property
    def document(self) -> XdmDocument | None:
        """The separately delivered document, when sequence normalization built one."""
        return self.tree


@dataclass(frozen=True, slots=True)
class TransformationResult:
    """The principal and secondary results of one transformation."""

    principal_entry: ResultEntry
    secondary: Mapping[str, ResultEntry] = field(default_factory=dict)
    messages: tuple[Message, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.principal_entry, ResultEntry):
            raise TypeError("principal_entry must be a ResultEntry")
        secondary: dict[str, ResultEntry] = {}
        if not isinstance(self.secondary, Mapping):
            raise TypeError("secondary results must be a mapping")
        for uri, entry in self.secondary.items():
            _absolute_uri(uri, "secondary result URI")
            if not isinstance(entry, ResultEntry):
                raise TypeError("secondary results must be ResultEntry values")
            if entry.output_uri != uri:
                raise ValueError(
                    f"secondary result entry output_uri {entry.output_uri!r} does not match {uri!r}"
                )
            secondary[uri] = entry
        object.__setattr__(self, "secondary", MappingProxyType(secondary))
        messages = tuple(self.messages)
        if not all(isinstance(message, Message) for message in messages):
            raise TypeError("result messages must be Message values")
        object.__setattr__(self, "messages", messages)

    @property
    def principal(self) -> XdmSequence:
        """The raw principal XDM sequence, before delivery normalization."""
        return self.principal_entry.raw

    @property
    def principal_tree(self) -> XdmDocument:
        """The delivered principal document, or an explicitly document-shaped raw result.

        Raises :class:`ResultShapeError` rather than wrapping a non-document
        sequence or silently serializing and reparsing it.
        """
        tree = self.principal_entry.tree
        if tree is not None:
            return tree
        raw = self.principal_entry.raw
        if len(raw) == 1 and isinstance(raw[0], XdmDocument):
            return raw[0]
        raise ResultShapeError(
            "the principal result is not a document tree; request delivery='document', "
            "honor a tree-building output method, or inspect the raw sequence"
        )
