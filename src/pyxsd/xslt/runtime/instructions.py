"""Immutable construction instructions and their XDM sequence results.

A construction instruction is data: it names the node kind, the evaluated
simple-content or attribute values, and any nested sequence constructor.  Every
instruction returns an XDM sequence, exactly like an XSLT sequence
constructor.  Nodes are produced by :class:`~pyxsd.xslt.runtime.builder.ResultBuilder`
directly through the XDM layer; an instruction never serializes a node and
reparses it to pass its value to the next instruction.

Literal result elements retain the compiled instruction's static namespace and
base-URI context.  ``xsl:namespace-alias`` remappings are applied by the
builder while the retained namespace nodes are installed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TypeAlias

from pyxsd.xdm import XdmItem, XdmSequence
from pyxsd.xdm.nodes import XdmAnnotation, XdmNode
from pyxsd.xslt.compiler.instructions import Instruction
from pyxsd.xslt.runtime.builder import ResultBuilder

__all__ = [
    "AttributeInstruction",
    "CommentInstruction",
    "ConstructionInstruction",
    "CopyInstruction",
    "DocumentInstruction",
    "ElementInstruction",
    "LiteralResultInstruction",
    "NamespaceInstruction",
    "ProcessingInstruction",
    "SequenceInstruction",
    "TemporaryTreeInstruction",
    "TextInstruction",
    "construct_attribute",
    "construct_instruction",
    "construct_into",
    "construct_sequence",
]


def _freeze(mapping: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True, slots=True)
class TextInstruction:
    """Construct one text node."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a text instruction value must be a string")


@dataclass(frozen=True, slots=True)
class AttributeInstruction:
    """Construct one parentless attribute node."""

    name: str
    value: str
    prefix: str | None = None
    annotation: XdmAnnotation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not isinstance(self.value, str):
            raise TypeError("an attribute instruction requires string name and value")
        if self.prefix is not None and not isinstance(self.prefix, str):
            raise TypeError("an attribute instruction prefix must be a string or None")


@dataclass(frozen=True, slots=True)
class NamespaceInstruction:
    """Construct one parentless namespace node."""

    prefix: str
    uri: str

    def __post_init__(self) -> None:
        if not isinstance(self.prefix, str) or not isinstance(self.uri, str):
            raise TypeError("a namespace instruction requires string prefix and URI")


@dataclass(frozen=True, slots=True)
class CommentInstruction:
    """Construct one comment node."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a comment instruction value must be a string")


@dataclass(frozen=True, slots=True)
class ProcessingInstruction:
    """Construct one processing-instruction node."""

    target: str
    value: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not isinstance(self.value, str):
            raise TypeError("a processing instruction requires string target and value")


@dataclass(frozen=True, slots=True)
class SequenceInstruction:
    """Return a raw XDM sequence unchanged."""

    items: tuple[XdmItem, ...] = ()

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if not all(isinstance(item, XdmItem) for item in items):
            raise TypeError("a sequence instruction requires XDM items")
        object.__setattr__(self, "items", items)


@dataclass(frozen=True, slots=True)
class ElementInstruction:
    """Construct one element with attributes and sequence-constructor content."""

    name: str
    attributes: tuple[tuple[str, str], ...] = ()
    namespaces: Mapping[str, str] = field(default_factory=dict)
    content: tuple[ConstructionInstruction, ...] = ()
    base_uri: str | None = None
    annotation: XdmAnnotation | None = None
    prefix: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", tuple(self.attributes))
        object.__setattr__(self, "namespaces", _freeze(self.namespaces))
        object.__setattr__(self, "content", tuple(self.content))


@dataclass(frozen=True, slots=True)
class LiteralResultInstruction:
    """Construct one literal result element using retained static context."""

    name: str
    attributes: tuple[tuple[str, str], ...] = ()
    namespaces: Mapping[str, str] = field(default_factory=dict)
    content: tuple[ConstructionInstruction, ...] = ()
    base_uri: str | None = None
    namespace_aliases: Mapping[str, str] = field(default_factory=dict)
    annotation: XdmAnnotation | None = None
    prefix: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", tuple(self.attributes))
        object.__setattr__(self, "namespaces", _freeze(self.namespaces))
        object.__setattr__(self, "content", tuple(self.content))
        object.__setattr__(self, "namespace_aliases", _freeze(self.namespace_aliases))

    @classmethod
    def from_instruction(
        cls,
        instruction: Instruction,
        *,
        attributes: Iterable[tuple[str, str]] | None = None,
        content: Iterable[ConstructionInstruction] = (),
        namespace_aliases: Mapping[str, str] | None = None,
        annotation: XdmAnnotation | None = None,
    ) -> LiteralResultInstruction:
        """Create a literal result from one compiled instruction's static state."""
        if attributes is None:
            attributes = tuple(
                (attribute.name, attribute.value) for attribute in instruction.attributes
            )
        return cls(
            name=instruction.name,
            attributes=tuple(attributes),
            namespaces=instruction.namespaces,
            content=tuple(content),
            base_uri=instruction.base_uri,
            namespace_aliases=namespace_aliases or {},
            annotation=annotation,
        )


@dataclass(frozen=True, slots=True)
class CopyInstruction:
    """Copy one node deeply or shallowly."""

    source: XdmNode
    deep: bool = True
    copy_namespaces: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.source, XdmNode):
            raise TypeError("a copy instruction requires an XDM node")
        if not isinstance(self.deep, bool) or not isinstance(self.copy_namespaces, bool):
            raise TypeError("copy instruction flags must be booleans")


@dataclass(frozen=True, slots=True)
class DocumentInstruction:
    """Construct one document node from a sequence constructor."""

    content: tuple[ConstructionInstruction, ...] = ()
    document_uri: str | None = None
    base_uri: str | None = None
    final_output: bool = False
    schema_pool: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", tuple(self.content))
        if not isinstance(self.final_output, bool):
            raise TypeError("a document instruction final_output flag must be a boolean")


@dataclass(frozen=True, slots=True)
class TemporaryTreeInstruction:
    """Construct one implicit temporary tree document."""

    content: tuple[ConstructionInstruction, ...] = ()
    document_uri: str | None = None
    base_uri: str | None = None
    schema_pool: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", tuple(self.content))

    @property
    def final_output(self) -> bool:
        return False


ConstructionInstruction: TypeAlias = (
    TextInstruction
    | AttributeInstruction
    | NamespaceInstruction
    | CommentInstruction
    | ProcessingInstruction
    | SequenceInstruction
    | ElementInstruction
    | LiteralResultInstruction
    | CopyInstruction
    | DocumentInstruction
    | TemporaryTreeInstruction
)


def _construct_element(
    name: str,
    *,
    attributes: Iterable[tuple[str, str]],
    namespaces: Mapping[str, str],
    content: Iterable[ConstructionInstruction],
    base_uri: str | None,
    annotation: XdmAnnotation | None,
    prefix: str | None,
    namespace_aliases: Mapping[str, str] | None = None,
) -> XdmSequence:
    builder = ResultBuilder.temporary(base_uri=base_uri)
    with builder.transaction():
        builder.element(
            name,
            attributes=attributes,
            namespaces=namespaces,
            namespace_aliases=namespace_aliases,
            base_uri=base_uri,
            annotation=annotation,
            prefix=prefix,
        )
        builder.append(construct_sequence(content))
        element = builder.root.root_element
        builder.to_tree()
    return (element,)


def _construct_document(
    content: Iterable[ConstructionInstruction],
    *,
    document_uri: str | None,
    base_uri: str | None,
    final_output: bool,
    schema_pool: object,
) -> XdmSequence:
    builder = ResultBuilder.document(
        document_uri=document_uri,
        base_uri=base_uri,
        schema_pool=schema_pool,
        final_output=final_output,
    )
    with builder.transaction():
        builder.append(construct_sequence(content))
        document = builder.to_tree()
    return (document,)


def construct_instruction(instruction: ConstructionInstruction) -> XdmSequence:
    """Construct one instruction and return its XDM sequence."""
    if isinstance(instruction, TextInstruction):
        return (ResultBuilder.temporary().text_node(instruction.value),)
    if isinstance(instruction, AttributeInstruction):
        builder = ResultBuilder.temporary()
        return (
            builder.attribute_node(
                instruction.name,
                instruction.value,
                prefix=instruction.prefix,
                annotation=instruction.annotation,
            ),
        )
    if isinstance(instruction, NamespaceInstruction):
        builder = ResultBuilder.temporary()
        return (builder.namespace_node(instruction.prefix, instruction.uri),)
    if isinstance(instruction, CommentInstruction):
        return (ResultBuilder.temporary().comment_node(instruction.value),)
    if isinstance(instruction, ProcessingInstruction):
        builder = ResultBuilder.temporary()
        return (builder.processing_instruction_node(instruction.target, instruction.value),)
    if isinstance(instruction, SequenceInstruction):
        return instruction.items
    if isinstance(instruction, ElementInstruction):
        return _construct_element(
            instruction.name,
            attributes=instruction.attributes,
            namespaces=instruction.namespaces,
            content=instruction.content,
            base_uri=instruction.base_uri,
            annotation=instruction.annotation,
            prefix=instruction.prefix,
        )
    if isinstance(instruction, LiteralResultInstruction):
        return _construct_element(
            instruction.name,
            attributes=instruction.attributes,
            namespaces=instruction.namespaces,
            content=instruction.content,
            base_uri=instruction.base_uri,
            annotation=instruction.annotation,
            prefix=instruction.prefix,
            namespace_aliases=instruction.namespace_aliases,
        )
    if isinstance(instruction, CopyInstruction):
        builder = ResultBuilder.temporary(base_uri=instruction.source.base_uri)
        with builder.transaction():
            copied = builder.clone(
                instruction.source,
                deep=instruction.deep,
                copy_namespaces=instruction.copy_namespaces,
            )
        return (copied,)
    if isinstance(instruction, DocumentInstruction):
        return _construct_document(
            instruction.content,
            document_uri=instruction.document_uri,
            base_uri=instruction.base_uri,
            final_output=instruction.final_output,
            schema_pool=instruction.schema_pool,
        )
    if isinstance(instruction, TemporaryTreeInstruction):
        return _construct_document(
            instruction.content,
            document_uri=instruction.document_uri,
            base_uri=instruction.base_uri,
            final_output=False,
            schema_pool=instruction.schema_pool,
        )
    raise TypeError(f"unknown construction instruction {type(instruction).__name__}")


def construct_attribute(
    name: str,
    value: str,
    *,
    prefix: str | None = None,
    annotation: XdmAnnotation | None = None,
) -> XdmSequence:
    """Construct one parentless attribute for a dynamic ``xsl:attribute``.

    Attribute sets and dynamic attribute instructions both evaluate to
    attribute nodes; the caller appends the returned sequence to an element
    through the complex-content rules, where duplicate names keep the last
    value.
    """
    builder = ResultBuilder.temporary()
    return (
        builder.attribute_node(
            name,
            value,
            prefix=prefix,
            annotation=annotation,
        ),
    )


def construct_sequence(instructions: Iterable[ConstructionInstruction]) -> XdmSequence:
    """Evaluate a sequence constructor into one XDM sequence."""
    result: list[XdmItem] = []
    for instruction in instructions:
        result.extend(construct_instruction(instruction))
    return tuple(result)


def construct_into(
    builder: ResultBuilder,
    instructions: Iterable[ConstructionInstruction],
) -> XdmSequence:
    """Append one sequence constructor to *builder* and return that sequence.

    The returned value is exactly the constructed sequence appended by this
    call, not pre-existing builder content.  A failure rolls the builder back
    and raises instead of returning a partial sequence.
    """
    sequence = construct_sequence(instructions)
    with builder.transaction():
        builder.append(sequence)
    return sequence
