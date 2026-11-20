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
from typing import Any, cast
from urllib.parse import urlsplit

from pyxsd.exceptions import PyXSDError
from pyxsd.xdm.nodes import XdmDocument, XdmNode
from pyxsd.xdm.values import XdmItem, XdmSequence
from pyxsd.xslt.errors import XsltDynamicError, is_budget_alarm
from pyxsd.xslt.policy import OutputPolicy
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
from pyxsd.xslt.serialization import SerializationParameters, Serializer
from pyxsd.xslt.serialization.characters import character_map_pairs

_build_result_tree = build_result_tree

__all__ = [
    "Message",
    "ResultEntry",
    "ResultShapeError",
    "TransformationResult",
    "delivered_entries",
    "serialization_parameters",
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
    tree construction was requested and possible.  ``serialized`` is the
    serialized string when ``delivery="stylesheet"`` produced one, while
    ``serialization`` records the effective output parameters.  ``output_uri``
    identifies the principal result or the absolute URI of a secondary result.
    ``character_maps`` holds the resolved ``(character, replacement)`` pairs for
    the effective output, so an explicit ``serialize()`` or ``write()``
    reproduces the transformation's own serialization.
    """

    raw: XdmSequence
    output_uri: str | None = None
    tree: XdmDocument | None = None
    serialized: str | None = None
    serialization: Mapping[str, str] = field(default_factory=dict)
    character_maps: tuple[tuple[str, str], ...] = ()
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
        object.__setattr__(self, "character_maps", character_map_pairs(self.character_maps))
        messages = tuple(self.messages)
        if not all(isinstance(message, Message) for message in messages):
            raise TypeError("result messages must be Message values")
        object.__setattr__(self, "messages", messages)

    @property
    def document(self) -> XdmDocument | None:
        """The separately delivered document, when sequence normalization built one."""
        return self.tree


#: The ``fn:transform`` delivery formats and the invocation delivery each one
#: runs under.  ``serialized`` uses the invocation-level stylesheet delivery,
#: which serializes the principal and every secondary result together.
DELIVERY_FORMATS: Mapping[str, str] = MappingProxyType(
    {"raw": "raw", "document": "document", "serialized": "stylesheet"}
)


def delivered_entries(
    result: TransformationResult,
    *,
    delivery: str,
    principal_parameters: Mapping[str, Any] | None = None,
) -> list[tuple[str, XdmSequence]]:
    """Return the ``fn:transform`` view of one transformation result.

    The principal entry is keyed by its absolute output URI, or ``"output"``
    when no base output URI was supplied; secondary entries are keyed by their
    absolute URIs.  ``delivery`` selects each entry's value: the raw sequence,
    the delivered document node, or the serialized string.  A serialized
    principal honors *principal_parameters* on top of its retained effective
    output attributes; secondary results always serialize with their own.

    F&O 3.1 section 16.3.2 gives the map one entry for the principal result and
    one for each secondary result, so a result document that resolves to the
    base output URI *is* the entry at that key.  XSLT 3.0 section 25.2 allows
    that: an empty principal delivers no final result tree at its URI, so one
    result document may occupy it.  The principal entry is therefore omitted
    when the secondary map already holds its key, and the secondary loop emits
    the value for that key.  The map never receives two entries for one key.
    """
    if delivery not in DELIVERY_FORMATS:
        raise ValueError(f"delivery must be one of {sorted(DELIVERY_FORMATS)}, got {delivery!r}")
    entries: list[tuple[str, XdmSequence]] = []
    principal = result.principal_entry
    key = principal.output_uri or "output"
    occupied = principal.output_uri is not None and principal.output_uri in result.secondary
    if not occupied:
        if delivery == "raw":
            entries.append((key, principal.raw))
        elif delivery == "document":
            # Document delivery always builds the principal tree; serialized
            # delivery always populates the serialized string.
            entries.append((key, cast(XdmSequence, (principal.tree,))))
        else:
            entries.append(
                (key, cast(XdmSequence, (result.serialize(None, parameters=principal_parameters),)))
            )
    if delivery == "serialized":
        for uri, entry in result.secondary.items():
            entries.append((uri, cast(XdmSequence, (entry.serialized,))))
        return entries
    for uri, entry in result.secondary.items():
        if delivery == "raw":
            entries.append((uri, entry.raw))
        else:
            tree = entry.tree
            if tree is None:
                # A secondary result built with build-tree="no" carries no
                # delivered tree yet; document delivery builds one now.
                tree = build_result_tree(entry.raw, output_uri=uri)
            entries.append((uri, (tree,)))
    return entries


def serialization_parameters(
    entry: ResultEntry,
    parameters: Mapping[str, Any] | None,
) -> SerializationParameters:
    """Resolve one entry's effective output attributes, with explicit overrides.

    The entry already stores effective values: QName-list attributes are
    expanded at their declaration sites and character-map names are resolved to
    pairs by the processor.  Override keys use the same ``xsl:output`` attribute
    vocabulary, plus the ``character_maps`` field, and are layered on top
    without touching the entry or its retained views.
    """
    attributes: dict[str, Any] = {"character_maps": entry.character_maps}
    attributes.update(entry.serialization)
    if parameters is not None:
        attributes.update(parameters)
    return SerializationParameters.from_attributes(attributes)


@dataclass(frozen=True, slots=True)
class TransformationResult:
    """The principal and secondary results of one transformation.

    The result retains its invocation's output policy only so an explicit
    :meth:`write` can authorize the same destination again.  Producing the
    result, :meth:`serialize`, and :meth:`principal_tree` never write.
    """

    principal_entry: ResultEntry
    secondary: Mapping[str, ResultEntry] = field(default_factory=dict)
    messages: tuple[Message, ...] = ()
    _output_policy: OutputPolicy | None = field(default=None, compare=False, repr=False)

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

    def _entry(self, uri: str | None) -> ResultEntry:
        """Select the principal entry, or exactly one secondary entry by URI."""
        if uri is None:
            return self.principal_entry
        return self.secondary[uri]

    def serialize(
        self,
        uri: str | None = None,
        *,
        parameters: Mapping[str, Any] | None = None,
    ) -> str:
        """Return one result as a string, without authorization or I/O.

        *uri* selects the principal result when ``None`` and exactly that
        secondary key otherwise.  *parameters* override the entry's retained
        effective output attributes for this call only.
        """
        entry = self._entry(uri)
        resolved = serialization_parameters(entry, parameters)
        return Serializer.serialize(entry.raw, resolved)

    def write(
        self,
        destination: Any,
        uri: str | None = None,
        *,
        parameters: Mapping[str, Any] | None = None,
    ) -> None:
        """Write one result after re-authorizing its absolute output URI.

        *uri* selects the principal result when ``None`` and exactly that
        secondary key otherwise.  Writing is always an explicit host operation:
        the entry must already carry an absolute output URI, and a configured
        output policy authorizes this destination again with
        ``principal=(uri is None)``.  A destination ``OSError`` becomes a
        ``code=None`` dynamic error retaining the original exception, except
        for an execution-budget alarm, which is re-raised: ``TimeoutError`` is
        one class, so a destination's own timeout and the caller's alarm are
        indistinguishable here, and the alarm is the likelier origin once a
        budget is set.
        """
        entry = self._entry(uri)
        if entry.output_uri is None:
            raise XsltDynamicError(
                "writing a result requires an absolute output URI",
                code="XTDE1480",
            )
        policy = self._output_policy
        if policy is not None:
            policy.authorize(entry.output_uri, principal=uri is None)
        resolved = serialization_parameters(entry, parameters)
        try:
            Serializer.write(entry.raw, resolved, destination)
        except OSError as error:
            if is_budget_alarm(error):
                raise
            raise XsltDynamicError(
                f"cannot write the result {entry.output_uri!r}: {error}",
                cause=error,
            ) from error


def _result_node_count(sequence: XdmSequence) -> int:
    """Count the distinct nodes one delivered result sequence contains.

    Every top-level node, its descendant nodes, and the attributes owned by
    those nodes contribute one unit.  Namespace nodes are properties of an
    element rather than independently delivered nodes and are not counted.
    The identity set prevents a sequence that contains both an ancestor and a
    descendant from charging the same node twice.
    """
    seen: set[int] = set()
    total = 0
    for item in sequence:
        if not isinstance(item, XdmNode):
            continue
        for node in (item, *item.descendants):
            identity = id(node)
            if identity in seen:
                continue
            seen.add(identity)
            total += 1 + len(node.attributes)
    return total
