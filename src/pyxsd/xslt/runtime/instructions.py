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
from typing import TypeAlias, cast

from pyxsd.xdm import XdmArray, XdmAtomic, XdmItem, XdmMap, XdmSequence
from pyxsd.xdm.nodes import NodeKind, XdmAnnotation, XdmDocument, XdmNode
from pyxsd.xslt.compiler.instructions import Instruction
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.builder import (
    ResultBuilder,
    validate_constructed,
    validate_delivered,
)

__all__ = [
    "AttributeInstruction",
    "CommentInstruction",
    "ConstructionInstruction",
    "CopyInstruction",
    "DocumentInstruction",
    "ElementInstruction",
    "LiteralResultInstruction",
    "MapEntryInstruction",
    "MapInstruction",
    "NamespaceInstruction",
    "ProcessingInstruction",
    "SequenceInstruction",
    "TemporaryTreeInstruction",
    "TextInstruction",
    "construct_attribute",
    "construct_grounded_document",
    "construct_instruction",
    "construct_into",
    "construct_map",
    "construct_map_entry",
    "construct_sequence",
    "is_deemed_empty",
    "is_vacuous",
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
    validation: str | None = None
    type_name: str | None = None
    schema_pool: object = None

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
    validation: str | None = None
    type_name: str | None = None
    schema_pool: object = None

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
    validation: str | None = None
    type_name: str | None = None
    schema_pool: object = None

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
        validation: str | None = None,
        type_name: str | None = None,
        schema_pool: object = None,
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
            validation=validation,
            type_name=type_name,
            schema_pool=schema_pool,
        )


@dataclass(frozen=True, slots=True)
class CopyInstruction:
    """Copy one node deeply or shallowly."""

    source: XdmNode
    deep: bool = True
    copy_namespaces: bool = True
    validation: str | None = None
    type_name: str | None = None
    schema_pool: object = None

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
    validation: str | None = None
    type_name: str | None = None

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
    validation: str | None = None
    type_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", tuple(self.content))

    @property
    def final_output(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class MapEntryInstruction:
    """Construct one singleton map from an already evaluated key and value."""

    key: XdmAtomic
    value: tuple[XdmItem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.key, XdmAtomic):
            raise TypeError("a map entry instruction requires an atomic key")
        value = tuple(self.value)
        if not all(isinstance(item, XdmItem) for item in value):
            raise TypeError("a map entry instruction requires XDM value items")
        object.__setattr__(self, "value", value)


@dataclass(frozen=True, slots=True)
class MapInstruction:
    """Construct one map by merging already constructed singleton/merged maps."""

    maps: tuple[XdmMap, ...] = ()

    def __post_init__(self) -> None:
        maps = tuple(self.maps)
        if not all(isinstance(item, XdmMap) for item in maps):
            raise TypeError("a map instruction requires XDM maps")
        object.__setattr__(self, "maps", maps)


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
    | MapEntryInstruction
    | MapInstruction
)


def is_vacuous(item: XdmItem) -> bool:
    """Whether *item* is vacuous for ``xsl:on-empty``/``xsl:on-non-empty``.

    An element with no children is not vacuous: only zero-length text nodes,
    childless document nodes, zero-length atomics, and arrays whose flattened
    members are all vacuous count.  Maps are never vacuous.
    """
    if isinstance(item, XdmNode):
        if item.kind is NodeKind.TEXT:
            return item.string_value == ""
        return item.kind is NodeKind.DOCUMENT and not item.children
    if isinstance(item, XdmAtomic):
        return item.lexical == ""
    if isinstance(item, XdmArray):
        return all(is_vacuous(member) for sequence in item.members for member in sequence)
    return False


def is_deemed_empty(item: XdmItem) -> bool:
    """Whether *item* is deemed empty for ``xsl:where-populated``.

    Unlike :func:`is_vacuous`, an element with no children is deemed empty,
    while any other zero-string-valued node (including a comment or processing
    instruction) is deemed empty and an element with a child never is.
    """
    if isinstance(item, XdmNode):
        if item.kind in (NodeKind.DOCUMENT, NodeKind.ELEMENT):
            return not item.children
        return item.string_value == ""
    if isinstance(item, XdmAtomic):
        return item.lexical == ""
    if isinstance(item, XdmMap):
        return item.size == 0
    if isinstance(item, XdmArray):
        return all(is_deemed_empty(member) for sequence in item.members for member in sequence)
    return False


def _require_map_key(sequence: XdmSequence) -> XdmAtomic:
    """Apply the function conversion rules for an ``xs:anyAtomicType`` key."""
    atomized: list[XdmAtomic] = []
    for item in sequence:
        if isinstance(item, XdmAtomic):
            atomized.append(item)
            continue
        if isinstance(item, XdmNode):
            atomized.extend(value for value in item.typed_value if isinstance(value, XdmAtomic))
            continue
        raise XsltDynamicError(
            "a map key cannot atomize a function item",
            code="FOTY0013",
        )
    if len(atomized) != 1:
        raise XsltDynamicError(
            f"a map key requires exactly one atomic value, got {len(atomized)}",
            code="XPTY0004",
        )
    key = atomized[0]
    if key.type_name.endswith("}untypedAtomic"):
        return XdmAtomic.string(key.lexical)
    return key


def construct_map_entry(key: XdmSequence, value: XdmSequence) -> XdmMap:
    """Construct one map entry, converting its key and preserving its value."""
    return XdmMap(((_require_map_key(key), tuple(value)),))


def construct_map(maps: XdmSequence) -> XdmMap:
    """Merge map results with ``duplicates="reject"`` and the exact error code."""
    entries: list[tuple[XdmAtomic, XdmSequence]] = []
    for item in maps:
        if not isinstance(item, XdmMap):
            raise XsltDynamicError(
                "xsl:map content must evaluate to a sequence of maps",
                code="XTTE3375",
            )
        for key, value in item.entries:
            duplicate = next(
                (existing for existing, _ in entries if existing.same_key(key)),
                None,
            )
            if duplicate is not None:
                raise XsltDynamicError(
                    f"xsl:map contains the duplicate key {key.lexical!r}",
                    code="XTDE3365",
                )
            entries.append((key, value))
    return XdmMap(entries)


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
    validation: str | None = None,
    type_name: str | None = None,
    schema_pool: object = None,
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
    if validation is not None or type_name is not None:
        validate_constructed(
            element,
            validation=validation or "strip",
            type_name=type_name,
            schema_pool=schema_pool,
        )
    return (element,)


def construct_grounded_document(
    content: bytes | bytearray | memoryview,
    *,
    uri: str,
    source_name: str | None = None,
    schema_pool: object = None,
) -> XdmDocument:
    """Parse one already-admitted resource into a grounded XDM document.

    The caller supplies admitted bytes, never a path, stream, or retrieval
    callback: authorization belongs to the resource layer.  The resulting
    document has one stable URI identity and no streaming cursor or bounded
    memory claim.
    """
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise TypeError("a grounded document requires admitted bytes")
    if not isinstance(uri, str) or not uri:
        raise ValueError("a grounded document requires a non-empty URI")
    if source_name is not None and not isinstance(source_name, str):
        raise TypeError("a grounded document source name must be a string or None")
    return XdmDocument.parse(
        bytes(content),
        source_name=source_name or uri,
        document_uri=uri,
        base_uri=uri,
        schema_pool=schema_pool,
    )


def _construct_document(
    content: Iterable[ConstructionInstruction],
    *,
    document_uri: str | None,
    base_uri: str | None,
    final_output: bool,
    schema_pool: object,
    validation: str | None = None,
    type_name: str | None = None,
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
    if validation is not None or type_name is not None:
        validate_delivered(
            document,
            validation=validation or "strip",
            type_name=type_name,
            schema_pool=schema_pool,
        )
    return (document,)


def construct_instruction(instruction: ConstructionInstruction) -> XdmSequence:
    """Construct one instruction and return its XDM sequence."""
    if isinstance(instruction, TextInstruction):
        return (ResultBuilder.temporary().text_node(instruction.value),)
    if isinstance(instruction, AttributeInstruction):
        builder = ResultBuilder.temporary()
        attribute = builder.attribute_node(
            instruction.name,
            instruction.value,
            prefix=instruction.prefix,
            annotation=instruction.annotation,
        )
        if instruction.validation is not None or instruction.type_name is not None:
            validate_constructed(
                attribute,
                validation=instruction.validation or "strip",
                type_name=instruction.type_name,
                schema_pool=instruction.schema_pool,
                operation="attribute",
            )
        return (attribute,)
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
    if isinstance(instruction, MapEntryInstruction):
        return (XdmMap(((instruction.key, instruction.value),)),)
    if isinstance(instruction, MapInstruction):
        return (construct_map(tuple(instruction.maps)),)
    if isinstance(instruction, ElementInstruction):
        return _construct_element(
            instruction.name,
            attributes=instruction.attributes,
            namespaces=instruction.namespaces,
            content=instruction.content,
            base_uri=instruction.base_uri,
            annotation=instruction.annotation,
            prefix=instruction.prefix,
            validation=instruction.validation,
            type_name=instruction.type_name,
            schema_pool=instruction.schema_pool,
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
            validation=instruction.validation,
            type_name=instruction.type_name,
            schema_pool=instruction.schema_pool,
        )
    if isinstance(instruction, CopyInstruction):
        builder = ResultBuilder.temporary(base_uri=instruction.source.base_uri)
        with builder.transaction():
            copied = builder.clone(
                instruction.source,
                deep=instruction.deep,
                copy_namespaces=instruction.copy_namespaces,
            )
        if instruction.validation is not None or instruction.type_name is not None:
            if copied.kind is NodeKind.DOCUMENT:
                validate_delivered(
                    cast(XdmDocument, copied),
                    validation=instruction.validation or "strip",
                    type_name=instruction.type_name,
                    schema_pool=instruction.schema_pool,
                )
            else:
                validate_constructed(
                    copied,
                    validation=instruction.validation or "strip",
                    type_name=instruction.type_name,
                    schema_pool=instruction.schema_pool,
                    operation="copy",
                )
        return (copied,)
    if isinstance(instruction, DocumentInstruction):
        return _construct_document(
            instruction.content,
            document_uri=instruction.document_uri,
            base_uri=instruction.base_uri,
            final_output=instruction.final_output,
            schema_pool=instruction.schema_pool,
            validation=instruction.validation,
            type_name=instruction.type_name,
        )
    if isinstance(instruction, TemporaryTreeInstruction):
        return _construct_document(
            instruction.content,
            document_uri=instruction.document_uri,
            base_uri=instruction.base_uri,
            final_output=False,
            schema_pool=instruction.schema_pool,
            validation=instruction.validation,
            type_name=instruction.type_name,
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
