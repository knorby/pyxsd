"""Immutable normalized XSLT instruction nodes.

Compiled components never retain mutable stylesheet trees.  Each instruction is
a frozen node with its expanded element name, normalized attributes, children,
declaring package, module, base URI, namespace context, and source location.
Literal result elements remain literal nodes; XSLT actions remain distinct
nodes, including ``xsl:next-match`` and ``xsl:apply-imports``.

References discovered while composing packages (named templates, attribute
sets, and bound ``xsl:original`` targets) are attached as immutable
:class:`ComponentReference` values.  Replacing a reference never mutates a
compiled node; a new node is produced with :func:`dataclasses.replace`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TypeAlias

from pyxsd.locations import IssueLocation
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xslt.compiler.static import EffectiveElement, EffectiveNode, EffectiveText

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

__all__ = [
    "Attribute",
    "ComponentReference",
    "Instruction",
    "InstructionNode",
    "SequenceConstructor",
    "TextNode",
    "normalize_element",
    "normalize_sequence",
]

InstructionNode: TypeAlias = "Instruction | TextNode"


def _local_name(name: str) -> str:
    return name.partition("}")[2] if name.startswith("{") else name


def _namespace_of(name: str) -> str | None:
    if not name.startswith("{"):
        return None
    return name[1:].partition("}")[0]


def _freeze(mapping: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True, slots=True)
class Attribute:
    """One normalized attribute with an expanded name and lexical value."""

    name: str
    value: str
    static: bool = False

    @property
    def local_name(self) -> str:
        return _local_name(self.name)

    @property
    def namespace_uri(self) -> str | None:
        return _namespace_of(self.name)


@dataclass(frozen=True, slots=True)
class ComponentReference:
    """One resolved symbolic reference in a normalized instruction.

    ``owner`` is the declaring package of the target component.  ``original``
    marks the special ``xsl:original`` reference and is legal only inside a
    named-component override.  ``visibility`` records the target's visibility
    at binding time so a package copy can decide whether to rebind it.
    """

    kind: str
    name: str | None
    arity: int | None = None
    owner: str | None = None
    visibility: str | None = None
    original: bool = False


@dataclass(frozen=True, slots=True)
class TextNode:
    """One normalized stylesheet text node with its declaring package."""

    value: str
    owner: str | None = None
    module: str | None = None
    location: IssueLocation | None = None

    @property
    def kind(self) -> NodeKind:
        return NodeKind.TEXT


@dataclass(frozen=True, slots=True)
class Instruction:
    """One immutable normalized element node.

    ``literal`` is true for a non-XSLT element.  ``reference`` carries a single
    resolved named reference (for example ``xsl:call-template``), while
    ``references`` carries every reference attached to this node (for example
    ``use-attribute-sets`` or expressions containing ``xsl:original``).
    """

    name: str
    attributes: tuple[Attribute, ...] = ()
    children: tuple[InstructionNode, ...] = ()
    owner: str | None = None
    module: str | None = None
    base_uri: str | None = None
    namespaces: Mapping[str, str] = field(default_factory=dict)
    location: IssueLocation | None = None
    literal: bool = False
    expand_text: bool = False
    reference: ComponentReference | None = None
    references: tuple[ComponentReference, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespaces", _freeze(self.namespaces))

    @property
    def local_name(self) -> str:
        return _local_name(self.name)

    @property
    def namespace_uri(self) -> str | None:
        return _namespace_of(self.name)

    @property
    def is_xslt(self) -> bool:
        return self.namespace_uri == XSLT_NS

    def attribute_node(self, name: str) -> Attribute | None:
        expanded = name
        if not name.startswith("{") and ":" in name:
            prefix, _, local = name.partition(":")
            uri = self.namespaces.get(prefix)
            if uri is not None:
                expanded = f"{{{uri}}}{local}"
        for attribute in self.attributes:
            if attribute.name == expanded:
                return attribute
        return None

    def attribute(self, name: str) -> str | None:
        attribute = self.attribute_node(name)
        return None if attribute is None else attribute.value

    @property
    def elements(self) -> tuple[Instruction, ...]:
        return tuple(child for child in self.children if isinstance(child, Instruction))

    def with_reference(self, reference: ComponentReference | None) -> Instruction:
        return replace(self, reference=reference)

    def with_references(self, references: tuple[ComponentReference, ...]) -> Instruction:
        return replace(self, references=references)

    def with_children(self, children: tuple[InstructionNode, ...]) -> Instruction:
        return replace(self, children=children)

    def __hash__(self) -> int:
        return hash(
            (
                self.name,
                self.owner,
                self.module,
                self.base_uri,
                self.literal,
                self.location,
            )
        )


@dataclass(frozen=True, slots=True)
class SequenceConstructor:
    """One immutable sequence constructor owned by a compiled component."""

    children: tuple[InstructionNode, ...] = ()
    owner: str | None = None
    module: str | None = None
    base_uri: str | None = None
    location: IssueLocation | None = None

    @property
    def elements(self) -> tuple[Instruction, ...]:
        return tuple(child for child in self.children if isinstance(child, Instruction))

    def __iter__(self) -> Iterator[InstructionNode]:
        return iter(self.children)

    def __len__(self) -> int:
        return len(self.children)

    def with_children(self, children: tuple[InstructionNode, ...]) -> SequenceConstructor:
        return replace(self, children=children)


def _normalized_attributes(element: EffectiveElement) -> tuple[Attribute, ...]:
    return tuple(
        Attribute(name=attribute.name, value=attribute.value, static=attribute.static)
        for attribute in element.attributes
    )


def _normalize_node(
    node: EffectiveNode,
    *,
    owner: str | None,
    module: str | None,
) -> InstructionNode:
    if isinstance(node, EffectiveText):
        return TextNode(
            value=node.value,
            owner=owner,
            module=module,
            location=node.source_location(),
        )
    return normalize_element(node, owner=owner, module=module)


def normalize_element(
    element: EffectiveElement,
    *,
    owner: str | None,
    module: str | None = None,
) -> Instruction:
    """Normalize one effective element into an immutable instruction node."""
    children = tuple(
        _normalize_node(child, owner=owner, module=module) for child in element.children
    )
    namespace = element.namespace_uri
    return Instruction(
        name=element.name,
        attributes=_normalized_attributes(element),
        children=children,
        owner=owner,
        module=module,
        base_uri=element.base_uri,
        namespaces=element.in_scope_namespaces,
        location=element.source_location(),
        literal=namespace != XSLT_NS,
        expand_text=False,
    )


def normalize_sequence(
    parent: EffectiveElement,
    *,
    owner: str | None,
    module: str | None = None,
) -> SequenceConstructor:
    """Normalize the children of one effective element as a sequence constructor."""
    return SequenceConstructor(
        children=tuple(
            _normalize_node(child, owner=owner, module=module) for child in parent.children
        ),
        owner=owner,
        module=module,
        base_uri=parent.base_uri,
        location=parent.source_location(),
    )
