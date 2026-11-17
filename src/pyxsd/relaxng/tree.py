"""Authoritative lexical element tree for RELAX NG instance documents.

One :class:`LexicalRevision` owns a single element graph.  Every element stores
its expanded name, lexical attributes, ordered children, and character data
exactly once.  The revision stores the namespace frame, effective base URI, and
source occurrence of each element; elements are views over that revision rather
than a second object graph.  Mutations update the same stores and dirty the
owning revision.

The nodes satisfy :class:`pyxsd.nodes.XMLNode` without inheriting
:class:`pyxsd.schema_base.SchemaBase`; no XSD declaration, particle, or
binding semantics enter this tree.
"""

from __future__ import annotations

import itertools
import operator
import threading
import weakref
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import XML_NS, NamespaceContext
from pyxsd.relaxng.origins import SourceOrigin

if TYPE_CHECKING:
    from pyxsd.relaxng.evidence import CandidateSelection

__all__ = ["LexicalRevision", "NodeContext", "RelaxNGElement"]

_XML_BASE = f"{{{XML_NS}}}base"

_TYPED_ACCESS_CAPABILITY = (
    "RELAX NG typed scalar access requires a typed parse; pass typed=True when parsing"
)


class _TypedSnapshot(Protocol):
    """One document's typed access over a valid, current lexical revision."""

    def scalar_value(self, node: RelaxNGElement) -> Any:
        """Interpret the element's leading character data."""

    def attribute_value(self, node: RelaxNGElement, expanded_name: str) -> Any:
        """Interpret one present attribute value by expanded name."""


_revision_ids = itertools.count(1)
_revision_lock = threading.Lock()


def _next_revision_id() -> int:
    """Return a process-unique revision identifier."""
    with _revision_lock:
        return next(_revision_ids)


@dataclass(frozen=True)
class NodeContext:
    """The lexical context recorded for one element occurrence.

    ``namespaces`` is the in-scope prefix mapping, or ``None`` when no
    namespace context was supplied.  ``base_uri`` is the effective base URI
    after any ``xml:base`` values.  ``source`` links the occurrence to its
    original document position for diagnostics.  The namespace mapping is
    copied into an immutable view so later caller mutations cannot change a
    recorded frame.
    """

    namespaces: Mapping[str, str] | None = None
    base_uri: str | None = None
    source: IssueLocation | None = None

    def __post_init__(self) -> None:
        if self.namespaces is not None:
            object.__setattr__(self, "namespaces", MappingProxyType(dict(self.namespaces)))


_EMPTY_CONTEXT = NodeContext()


class LexicalRevision:
    """The single revision that owns a lexical RELAX NG element graph.

    The revision ID identifies one parse revision.  ``dirty`` becomes true
    after the first successful mutation and stays true: writers and traversal
    read the current tree, while prior interpretations are no longer current
    evidence.  Contexts are held weakly so removing a subtree does not keep it
    alive.  A typed parse attaches one immutable interpretation snapshot to
    the revision; mutations make that snapshot stale rather than refreshing it.
    """

    __slots__ = ("_contexts", "_dirty", "_root", "_typed_snapshot", "revision_id")

    revision_id: int
    _dirty: bool
    _root: RelaxNGElement | None
    _contexts: weakref.WeakKeyDictionary[RelaxNGElement, NodeContext]
    _typed_snapshot: _TypedSnapshot | None

    def __init__(self) -> None:
        self.revision_id = _next_revision_id()
        self._dirty = False
        self._root: RelaxNGElement | None = None
        self._contexts: weakref.WeakKeyDictionary[RelaxNGElement, NodeContext] = (
            weakref.WeakKeyDictionary()
        )
        self._typed_snapshot = None

    @property
    def root(self) -> RelaxNGElement:
        """The root element adopted by this revision."""
        return cast("RelaxNGElement", self._root)

    @property
    def dirty(self) -> bool:
        """Whether a successful mutation has occurred in this revision."""
        return self._dirty

    def mark_dirty(self) -> None:
        """Mark this revision as edited; repeated calls are idempotent."""
        self._dirty = True

    def context_for(self, node: RelaxNGElement) -> NodeContext:
        """Return the recorded context of *node*, or an empty context."""
        return self._contexts.get(node, _EMPTY_CONTEXT)

    def namespaces_for(self, node: RelaxNGElement) -> Mapping[str, str] | None:
        """Return the recorded namespace frame of *node*."""
        return self.context_for(node).namespaces

    def base_uri_for(self, node: RelaxNGElement) -> str | None:
        """Return the effective base URI of *node*."""
        return self.context_for(node).base_uri

    def source_location_for(self, node: RelaxNGElement) -> IssueLocation | None:
        """Return the source occurrence of *node*."""
        return self.context_for(node).source

    def _adopt_root(self, root: RelaxNGElement) -> None:
        self._root = root

    def _attach_typed_snapshot(self, snapshot: _TypedSnapshot) -> None:
        """Attach the one typed interpretation that describes this revision.

        A revision never replaces typed evidence in place: a second, different
        snapshot is a construction error, and revalidation works on a fresh
        revision.  Idempotent reattachment of the same snapshot is allowed so a
        stale-transform document can share the revision.
        """
        if self._typed_snapshot is not None and self._typed_snapshot is not snapshot:
            raise ValueError("this revision already owns typed interpretation evidence")
        self._typed_snapshot = snapshot

    def _register_context(self, node: RelaxNGElement, context: NodeContext) -> None:
        self._contexts[node] = context

    def _replace_context(self, node: RelaxNGElement, context: NodeContext) -> None:
        self._contexts[node] = context


class RelaxNGElement:
    """One node of the authoritative lexical tree.

    The four :class:`~pyxsd.nodes.XMLNode` fields are the lexical stores:
    ``_name_`` is the expanded (Clark) element name, ``_attribs_`` maps expanded
    attribute names to lexical values, ``_children_`` is the one ordered child
    list, and ``_value_`` holds the element's leading character data as a
    single entry (``None`` when absent, ``[""]`` when explicitly empty).
    ``_tail_`` holds character data following this element in its parent.
    """

    __slots__ = (
        "__weakref__",
        "_attribs_",
        "_children_",
        "_name_",
        "_parent_",
        "_revision",
        "_tail_",
        "_value_",
    )

    _name_: str
    _attribs_: dict[str, str]
    _children_: list[RelaxNGElement]
    _value_: list[str] | None
    _tail_: str | None
    _parent_: RelaxNGElement | None
    _revision: LexicalRevision

    def __init__(self) -> None:
        raise TypeError("RelaxNGElement values are created by from_etree() or clone_relaxng()")

    @classmethod
    def _new(
        cls,
        name: str,
        revision: LexicalRevision,
        *,
        attributes: Mapping[str, str] | None = None,
        text: str | None = None,
        tail: str | None = None,
    ) -> RelaxNGElement:
        node = object.__new__(cls)
        node._name_ = name
        node._attribs_ = dict(attributes) if attributes is not None else {}
        node._children_ = []
        node._value_ = [text] if text is not None else None
        node._tail_ = tail
        node._parent_ = None
        node._revision = revision
        return node

    @classmethod
    def from_etree(
        cls,
        element: ET.Element,
        *,
        namespaces: NamespaceContext | None = None,
        base_uri: str | None = None,
        source: IssueLocation | SourceOrigin | None = None,
    ) -> RelaxNGElement:
        """Build an independent lexical tree from an ElementTree subtree.

        - ``element`` is the parsed source subtree.  Its expanded names and
          lexical values are copied; it is never retained.
        - ``namespaces`` supplies the recorded in-scope frame of every source
          element.  Without it, elements record no namespace frame and the
          writer generates prefixes as needed.
        - ``base_uri`` is the source document's base URI.  Each ``xml:base``
          value is resolved into the element's effective base.
        - ``source`` is the root occurrence (or an authored origin carrying
          one).  Child occurrences receive paths extended with expanded names
          and same-name sibling indices.

        The returned root is the sole member of a fresh
        :class:`LexicalRevision`.
        """
        if not isinstance(element, ET.Element):
            raise TypeError("element must be an xml.etree.ElementTree.Element")
        revision = cls._create_revision()
        location = _root_location(source, element)
        frame = None if namespaces is None else dict(namespaces.bindings_for(element))
        root = cls._new(
            element.tag,
            revision,
            attributes=element.attrib,
            text=element.text,
            tail=element.tail,
        )
        revision._adopt_root(root)
        revision._register_context(
            root, NodeContext(frame, _effective_base(base_uri, element), location)
        )
        stack: list[
            tuple[
                ET.Element,
                RelaxNGElement,
                str | None,
                IssueLocation | None,
            ]
        ] = [(element, root, _effective_base(base_uri, element), location)]
        while stack:
            source_element, target_element, parent_base, parent_location = stack.pop()
            counts: dict[str, int] = {}
            for source_child in source_element:
                index = counts.get(source_child.tag, 0) + 1
                counts[source_child.tag] = index
                child_location = _child_location(parent_location, source_child.tag, index)
                child_base = _effective_base(parent_base, source_child)
                child_frame = (
                    None if namespaces is None else dict(namespaces.bindings_for(source_child))
                )
                child = cls._new(
                    source_child.tag,
                    revision,
                    attributes=source_child.attrib,
                    text=source_child.text,
                    tail=source_child.tail,
                )
                target_element._children_.append(child)
                child._parent_ = target_element
                revision._register_context(
                    child, NodeContext(child_frame, child_base, child_location)
                )
                stack.append((source_child, child, child_base, child_location))
        return root

    @classmethod
    def _create_revision(cls) -> LexicalRevision:
        return LexicalRevision()

    @classmethod
    def _clone_new(cls, source: RelaxNGElement, revision: LexicalRevision) -> RelaxNGElement:
        """Create one clone node through this concrete class's factory.

        Cloning resolves the factory on the *concrete* node class, so a
        generated projection subclass keeps its own construction contract
        instead of collapsing to :class:`RelaxNGElement`.
        """
        return cls._new(
            source._name_,
            revision,
            attributes=source._attribs_,
            text=source.text_value(),
            tail=source._tail_,
        )

    @property
    def revision(self) -> LexicalRevision:
        """The revision that owns this node."""
        return self._revision

    @property
    def parent(self) -> RelaxNGElement | None:
        """The parent node, or ``None`` for a root or detached subtree."""
        return self._parent_

    @property
    def children(self) -> tuple[RelaxNGElement, ...]:
        """The element children in document order."""
        return tuple(self._children_)

    @property
    def attributes(self) -> Mapping[str, str]:
        """A read-only view of the expanded attribute store."""
        return MappingProxyType(self._attribs_)

    @property
    def namespaces(self) -> Mapping[str, str] | None:
        """The recorded in-scope namespace frame, or ``None``."""
        return self._revision.context_for(self).namespaces

    @property
    def base_uri(self) -> str | None:
        """The effective base URI recorded for this element."""
        return self._revision.context_for(self).base_uri

    @property
    def source_location(self) -> IssueLocation | None:
        """The source occurrence recorded for this element."""
        return self._revision.context_for(self).source

    def text_value(self) -> str | None:
        """Return the element's exact leading character data.

        ``None`` means the element has no character data at that position;
        ``""`` is present but empty.  Child tails belong to the children.
        """
        if self._value_ is None:
            return None
        return self._value_[0]

    def tail_value(self) -> str | None:
        """Return the character data immediately following this element."""
        return self._tail_

    def attribute_lexical(self, expanded_name: str) -> str | None:
        """Return one attribute's lexical value, or ``None`` when absent."""
        return self._attribs_.get(expanded_name)

    def scalar_value(self) -> Any:
        """Return this element's typed leading-character-data interpretation.

        ``None`` means the element has no leading character data, or its
        accepted derivation assigned no scalar interpretation there.  An
        explicitly present empty lexical value is a real scalar position and
        differs from absence.  A validation-only document and a document with
        no typed evidence fail with the unsupported-capability
        :class:`~pyxsd.exceptions.PyXSDError`; an invalid typed document raises
        :class:`~pyxsd.exceptions.ValidationError`, and an edited revision
        raises :class:`~pyxsd.relaxng.errors.StaleInterpretationError`.
        """
        snapshot = self._revision._typed_snapshot
        if snapshot is None:
            raise PyXSDError(_TYPED_ACCESS_CAPABILITY)
        return snapshot.scalar_value(self)

    def attribute_value(self, expanded_name: str) -> Any:
        """Return one present attribute's typed interpretation.

        ``None`` means the attribute is absent or the accepted derivation
        assigned no scalar interpretation to it.  Freshness and validity gates
        match :meth:`scalar_value`.
        """
        snapshot = self._revision._typed_snapshot
        if snapshot is None:
            raise PyXSDError(_TYPED_ACCESS_CAPABILITY)
        return snapshot.attribute_value(self, expanded_name)

    def assign(
        self,
        *,
        selections: tuple[CandidateSelection, ...] = (),
        **fields: Any,
    ) -> None:
        """Encode and apply one atomic transaction of projected field writes.

        Keys are generated projection aliases or the route's scalar descriptor
        name.  Every value is shape-checked, encoded, and context-checked before
        any lexical store changes; ``selections`` name pre-commit
        :class:`~pyxsd.relaxng.evidence.CandidateSelection` facts that must
        coexist in one complete accepting derivation.  A successful write
        dirties the whole revision but never certifies document validity or
        refreshes typed evidence.  Lexical :meth:`set_text` and
        :meth:`set_attribute` remain available and never choose a datatype.
        """
        from pyxsd.relaxng.binding import assign_fields

        assign_fields(self, dict(fields), selections)

    def iter_content(self) -> Iterator[str | RelaxNGElement]:
        """Yield text, child elements, and child tails in document order.

        Present-but-empty text is yielded as ``""``; an absent tail is
        omitted.  The child list is snapshotted so iteration cannot observe a
        partially applied structural edit.
        """
        text = self.text_value()
        if text is not None:
            yield text
        for child in tuple(self._children_):
            yield child
            if child._tail_ is not None:
                yield child._tail_

    def set_name(self, name: str) -> None:
        """Replace the expanded element name and dirty the revision."""
        _check_name(name, "element")
        self._name_ = name
        self._touch()

    def set_text(self, lexical: str) -> None:
        """Replace the leading character data with *lexical*."""
        if not isinstance(lexical, str):
            raise TypeError("text must be a string")
        self._value_ = [lexical]
        self._touch()

    def clear_text(self) -> None:
        """Remove the element's leading character data."""
        self._value_ = None
        self._touch()

    def set_tail(self, lexical: str) -> None:
        """Replace the character data following this element."""
        if not isinstance(lexical, str):
            raise TypeError("tail must be a string")
        self._tail_ = lexical
        self._touch()

    def clear_tail(self) -> None:
        """Remove the character data following this element."""
        self._tail_ = None
        self._touch()

    def set_attribute(self, expanded_name: str, lexical: str) -> None:
        """Set one expanded attribute name to a lexical string.

        This is a lexical edit only: it does not reinterpret ``xml:base`` or
        namespace declarations.  Recorded context is edited explicitly with
        :meth:`set_base_uri` and :meth:`set_namespace_bindings`.
        """
        _check_name(expanded_name, "attribute")
        if not isinstance(lexical, str):
            raise TypeError("attribute value must be a string")
        self._attribs_[expanded_name] = lexical
        self._touch()

    def delete_attribute(self, expanded_name: str) -> None:
        """Delete one expanded attribute, raising ``KeyError`` when absent."""
        _check_name(expanded_name, "attribute")
        if expanded_name not in self._attribs_:
            raise KeyError(expanded_name)
        del self._attribs_[expanded_name]
        self._touch()

    def set_namespace_bindings(self, mappings: Mapping[str, str]) -> None:
        """Replace the recorded in-scope namespace frame of this element."""
        if not isinstance(mappings, Mapping):
            raise TypeError("namespace bindings must be a mapping")
        context = self._revision.context_for(self)
        self._revision._replace_context(
            self, NodeContext(mappings, context.base_uri, context.source)
        )
        self._touch()

    def set_base_uri(self, base_uri: str | None) -> None:
        """Replace the recorded effective base URI of this element."""
        if base_uri is not None and not isinstance(base_uri, str):
            raise TypeError("base_uri must be a string or None")
        context = self._revision.context_for(self)
        self._revision._replace_context(
            self, NodeContext(context.namespaces, base_uri, context.source)
        )
        self._touch()

    def append_child(self, child: RelaxNGElement) -> None:
        """Append a detached element from the same revision."""
        self._validate_child(child)
        self._children_.append(child)
        child._parent_ = self
        self._touch()

    def insert_child(self, index: int, child: RelaxNGElement) -> None:
        """Insert a detached element from the same revision at *index*."""
        position = operator.index(index)
        self._validate_child(child)
        self._children_.insert(position, child)
        child._parent_ = self
        self._touch()

    def remove_child(self, child_or_index: RelaxNGElement | int) -> RelaxNGElement:
        """Remove a child by node or index and return it with no parent."""
        if isinstance(child_or_index, RelaxNGElement):
            if child_or_index._parent_ is not self:
                raise ValueError("the node is not a child of this element")
            position = self._children_.index(child_or_index)
            removed = self._children_.pop(position)
        else:
            position = operator.index(child_or_index)
            removed = self._children_.pop(position)
        removed._parent_ = None
        self._touch()
        return removed

    def replace_child(self, index: int, child: RelaxNGElement) -> RelaxNGElement:
        """Replace the child at *index* with a detached same-revision element."""
        position = operator.index(index)
        current = self._children_[position]
        if current is child:
            self._touch()
            return current
        self._validate_child(child)
        self._children_[position] = child
        current._parent_ = None
        child._parent_ = self
        self._touch()
        return current

    def clear_children(self) -> None:
        """Detach every child, leaving this element with no children."""
        for child in self._children_:
            child._parent_ = None
        self._children_.clear()
        self._touch()

    def to_etree(self) -> ET.Element:
        """Copy this subtree to an independent ElementTree structure."""
        root = ET.Element(self._name_, dict(self._attribs_))
        _copy_text_and_tail(root, self)
        stack: list[tuple[RelaxNGElement, ET.Element]] = [(self, root)]
        while stack:
            source, target = stack.pop()
            for child in source._children_:
                target_child = ET.Element(child._name_, dict(child._attribs_))
                _copy_text_and_tail(target_child, child)
                target.append(target_child)
                stack.append((child, target_child))
        return root

    def _validate_child(self, child: RelaxNGElement) -> None:
        if not isinstance(child, RelaxNGElement):
            raise TypeError("child must be a RelaxNGElement")
        if child._revision is not self._revision:
            raise ValueError("a child must belong to the same revision")
        if child._parent_ is not None:
            raise ValueError("the child is already attached")
        ancestor: RelaxNGElement | None = self
        while ancestor is not None:
            if ancestor is child:
                raise ValueError("a child cannot contain its ancestor")
            ancestor = ancestor._parent_

    def _touch(self) -> None:
        self._revision.mark_dirty()


def _check_name(name: str, role: str) -> None:
    if not isinstance(name, str):
        raise TypeError(f"{role} name must be a string")
    if not name:
        raise ValueError(f"{role} name must not be empty")


def _copy_text_and_tail(target: ET.Element, source: RelaxNGElement) -> None:
    text = source.text_value()
    if text is not None:
        target.text = text
    if source._tail_ is not None:
        target.tail = source._tail_


def _root_location(
    source: IssueLocation | SourceOrigin | None, element: ET.Element
) -> IssueLocation | None:
    if source is None:
        return None
    if isinstance(source, SourceOrigin):
        location = source.location
    elif isinstance(source, IssueLocation):
        location = source
    else:
        raise TypeError("source must be an IssueLocation, SourceOrigin, or None")
    if location is None:
        return None
    if location.path:
        return location
    return replace(location, path=(PathStep(element.tag, 1),))


def _child_location(parent: IssueLocation | None, name: str, index: int) -> IssueLocation | None:
    if parent is None:
        return None
    return IssueLocation(parent.source, (*parent.path, PathStep(name, index)))


def _effective_base(parent_base: str | None, element: ET.Element) -> str | None:
    raw = element.get(_XML_BASE)
    if raw is None or raw == "":
        return parent_base
    if parent_base:
        return urljoin(parent_base, raw)
    return raw
