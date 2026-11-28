"""XSLT construction and sequence-instruction execution.

A construction instruction is data: it names the node kind, the evaluated
simple-content or attribute values, and any nested sequence constructor.  Every
instruction returns an XDM sequence, exactly like an XSLT sequence
constructor.  Nodes are produced by :class:`~pyxsd.xslt.runtime.builder.ResultBuilder`
directly through the XDM layer; an instruction never serializes a node and
reparses it to pass its value to the next instruction.

The module also owns the complete execution bodies for construction,
conditional, map, assertion, numbering, analysis, try/catch, and grounded
construct instructions.  They receive the invocation coordinator and an
immutable :class:`~pyxsd.xslt.runtime.context.EvaluationContext`, and use the
invocation's shared evaluation services instead of a second evaluation path.

Literal result elements retain the compiled instruction's static namespace and
base-URI context.  ``xsl:namespace-alias`` remappings are applied by the
builder while the retained namespace nodes are installed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeAlias, cast

from elementpath.datatypes import QName
from elementpath.exceptions import ElementPathError
from elementpath.helpers import is_ncname

from pyxsd.xdm import XdmArray, XdmAtomic, XdmItem, XdmMap, XdmSequence, as_xdm
from pyxsd.xdm.annotations import builtin_type_classes
from pyxsd.xdm.legacy import ResultTreeFragment
from pyxsd.xdm.nodes import NodeKind, XdmAnnotation, XdmDocument, XdmNode
from pyxsd.xdm.validation import _annotate_any_type_copy, namespace_sensitive
from pyxsd.xpath.continuation import XPathEvaluationError
from pyxsd.xpath.environment import Focus
from pyxsd.xslt.compiler.instructions import Instruction
from pyxsd.xslt.compiler.patterns import CompiledPattern, PatternMatcher
from pyxsd.xslt.compiler.plans import (
    _AnalyzeStringPlan,
    _AssertPlan,
    _AttributePlan,
    _CatchPlan,
    _ChoosePlan,
    _ConcatPlan,
    _ConditionalSequencePlan,
    _CopyOfPlan,
    _CopyPlan,
    _DocumentPlan,
    _ElementPlan,
    _ForkPlan,
    _LiteralPlan,
    _MapEntryPlan,
    _MapPlan,
    _MessagePlan,
    _NumberPlan,
    _OnEmptyPlan,
    _OnNonEmptyPlan,
    _ParamPlan,
    _Plan,
    _RestoreVariablesPlan,
    _SequencePlan,
    _SourceDocumentPlan,
    _TryPlan,
    _ValueOfPlan,
    _VariablePlan,
    _WherePopulatedPlan,
)
from pyxsd.xslt.errors import (
    ERROR_NAMESPACE,
    XsltDynamicError,
    XsltError,
    error_code_from_exception,
    is_budget_alarm,
    is_static_error_code,
)
from pyxsd.xslt.profiles import LanguageProfile
from pyxsd.xslt.results import (
    Message,
    _append_complex_content,
    _build_result_tree,
    _NodeFactory,
)
from pyxsd.xslt.runtime.accumulators import accumulator_copy_names
from pyxsd.xslt.runtime.builder import (
    AttributeName,
    NamespaceAlias,
    ResultBuilder,
    validate_constructed,
    validate_delivered,
)
from pyxsd.xslt.runtime.context import EvaluationContext
from pyxsd.xslt.runtime.frames import (
    Call,
    CatchResult,
    Continue,
    Frame,
    FrameResult,
    _IterationControlSignal,
)
from pyxsd.xslt.runtime.numbering import format_place_marker, place_marker
from pyxsd.xslt.runtime.text import XPathRegexError, analyze_string, compile_xpath_regex

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xslt.processor import _Invocation

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
    "attribute_value",
    "choose_branch",
    "construct_attribute",
    "construct_grounded_document",
    "construct_instruction",
    "construct_into",
    "construct_map",
    "construct_map_entry",
    "construct_sequence",
    "copy_of_value",
    "copy_value",
    "document_value",
    "element_value",
    "execute_analyze_string",
    "execute_assert",
    "execute_choose",
    "execute_concat",
    "execute_conditional_sequence",
    "execute_fork",
    "execute_map",
    "execute_map_entry",
    "execute_message",
    "execute_number",
    "execute_source_document",
    "execute_try",
    "execute_value_of",
    "execute_where_populated",
    "is_deemed_empty",
    "is_vacuous",
    "literal_element",
    "parameter_value",
    "try_step",
    "variable_value",
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


def _freeze_attributes(
    attributes: Iterable[tuple[AttributeName, str] | tuple[str, str]],
) -> tuple[tuple[AttributeName, str], ...]:
    """Normalize attribute entries, keeping each lexical prefix.

    A plain string name is accepted for callers that construct an instruction
    by hand; it denotes an unprefixed attribute.
    """
    entries: list[tuple[AttributeName, str]] = []
    for head, value in attributes:
        name = head if isinstance(head, AttributeName) else AttributeName(head)
        entries.append((name, value))
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class ElementInstruction:
    """Construct one element with attributes and sequence-constructor content."""

    name: str
    attributes: tuple[tuple[AttributeName, str], ...] = ()
    namespaces: Mapping[str, str] = field(default_factory=dict)
    content: tuple[ConstructionInstruction, ...] = ()
    base_uri: str | None = None
    annotation: XdmAnnotation | None = None
    prefix: str | None = None
    validation: str | None = None
    type_name: str | None = None
    schema_pool: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _freeze_attributes(self.attributes))
        object.__setattr__(self, "namespaces", _freeze(self.namespaces))
        object.__setattr__(self, "content", tuple(self.content))


@dataclass(frozen=True, slots=True)
class LiteralResultInstruction:
    """Construct one literal result element using retained static context."""

    name: str
    attributes: tuple[tuple[AttributeName, str], ...] = ()
    namespaces: Mapping[str, str] = field(default_factory=dict)
    content: tuple[ConstructionInstruction, ...] = ()
    base_uri: str | None = None
    namespace_aliases: Mapping[str, str] = field(default_factory=dict)
    excluded_namespaces: frozenset[str] = frozenset()
    namespace_alias_rules: tuple[NamespaceAlias, ...] = ()
    annotation: XdmAnnotation | None = None
    prefix: str | None = None
    validation: str | None = None
    type_name: str | None = None
    schema_pool: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _freeze_attributes(self.attributes))
        object.__setattr__(self, "namespaces", _freeze(self.namespaces))
        object.__setattr__(self, "content", tuple(self.content))
        object.__setattr__(self, "namespace_aliases", _freeze(self.namespace_aliases))
        object.__setattr__(self, "excluded_namespaces", frozenset(self.excluded_namespaces))
        object.__setattr__(self, "namespace_alias_rules", tuple(self.namespace_alias_rules))

    @classmethod
    def from_instruction(
        cls,
        instruction: Instruction,
        *,
        attributes: Iterable[tuple[AttributeName, str] | tuple[str, str]] | None = None,
        content: Iterable[ConstructionInstruction] = (),
        namespace_aliases: Mapping[str, str] | None = None,
        excluded_namespaces: Iterable[str] = (),
        namespace_alias_rules: Iterable[NamespaceAlias] = (),
        annotation: XdmAnnotation | None = None,
        validation: str | None = None,
        type_name: str | None = None,
        schema_pool: object = None,
    ) -> LiteralResultInstruction:
        """Create a literal result from one compiled instruction's static state."""
        if attributes is None:
            attributes = tuple(
                (AttributeName(attribute.name, attribute.prefix), attribute.value)
                for attribute in instruction.attributes
            )
        return cls(
            name=instruction.name,
            attributes=_freeze_attributes(attributes),
            namespaces=instruction.namespaces,
            content=tuple(content),
            base_uri=instruction.base_uri,
            namespace_aliases=namespace_aliases or {},
            excluded_namespaces=frozenset(excluded_namespaces),
            namespace_alias_rules=tuple(namespace_alias_rules),
            annotation=annotation,
            prefix=instruction.prefix,
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
    attributes: Iterable[tuple[AttributeName, str] | tuple[str, str]],
    namespaces: Mapping[str, str],
    content: Iterable[ConstructionInstruction],
    base_uri: str | None,
    annotation: XdmAnnotation | None,
    prefix: str | None,
    namespace_aliases: Mapping[str, str] | None = None,
    excluded_namespaces: frozenset[str] = frozenset(),
    namespace_alias_rules: Iterable[NamespaceAlias] = (),
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
            excluded_namespaces=excluded_namespaces,
            namespace_alias_rules=namespace_alias_rules,
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
            excluded_namespaces=instruction.excluded_namespaces,
            namespace_alias_rules=instruction.namespace_alias_rules,
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
        if copied.kind is NodeKind.ELEMENT:
            builder.finalize_root(copied)
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


# ---------------------------------------------------------------------------
# Instruction-family execution


def _constructed_qname(
    lexical: str,
    namespace_value: str | None,
    namespaces: Mapping[str, str],
    *,
    default_namespace: bool,
    code: str,
    namespace_code: str | None = None,
    prefix_code: str | None = None,
) -> str:
    """Resolve a dynamically constructed element or attribute name."""
    text = lexical.strip()
    if not text:
        raise XsltDynamicError("a constructed name must not be empty", code=code)
    if text.startswith("Q{"):
        close = text.find("}")
        if close < 0 or not is_ncname(text[close + 1 :]):
            raise XsltDynamicError(f"the constructed name {text!r} is not a valid QName", code=code)
        namespace = text[2:close]
        local = text[close + 1 :]
        return f"{{{namespace}}}{local}" if namespace else local
    if text.startswith("{"):
        return text
    prefix, separator, local = text.partition(":")
    if not separator:
        prefix, local = "", text
    if not is_ncname(local) or (separator and not is_ncname(prefix)):
        raise XsltDynamicError(f"the constructed name {text!r} is not a valid QName", code=code)
    if namespace_value is not None:
        if namespace_value == "http://www.w3.org/2000/xmlns/":
            raise XsltDynamicError(
                "the xmlns namespace cannot be used for a constructed name",
                code=namespace_code or code,
            )
        return f"{{{namespace_value}}}{local}" if namespace_value else local
    if separator:
        uri = namespaces.get(prefix)
        if uri is None:
            raise XsltDynamicError(
                f"the namespace prefix {prefix!r} is not bound in the static context",
                code=prefix_code or code,
            )
        return f"{{{uri}}}{local}"
    if default_namespace:
        uri = namespaces.get("")
        if uri:
            return f"{{{uri}}}{text}"
    return text


def _lexical_prefix(lexical: str) -> str:
    """The prefix an ``xsl:element`` lexical name contributes to its name.

    XSLT 3.0 §11.2.2 copies the prefix of the lexical QName in ``name`` (or
    its absence) to the expanded QName.  An EQName or Clark name carries no
    prefix.  The caller has already validated the name.
    """
    text = lexical.strip()
    if text.startswith("Q{") or text.startswith("{"):
        return ""
    prefix, separator, _ = text.partition(":")
    return prefix if separator else ""


def _diagnostic_error_code(
    value: str, namespaces: Mapping[str, str], *, default: str = "XTMM9001"
) -> str:
    """Resolve an effective diagnostic ``error-code`` EQName.

    An invalid EQName uses the caller's default. Standard error-namespace names
    use their familiar local form; a custom namespace keeps its expanded form.
    An unprefixed EQName is in no namespace (XSLT 3.0 section 5.4.2), so it
    keeps the ``Q{}local`` form ``fn:error`` uses instead of collapsing to a
    bare local name that would read as a standard-namespace code.
    """
    text = value.strip()
    if not text:
        return default
    if text.startswith("Q{"):
        close = text.find("}")
        if close < 0:
            return default
        namespace = text[2:close]
        local = text[close + 1 :]
        if not is_ncname(local):
            return default
        expanded = f"{{{namespace}}}{local}" if namespace else f"Q{{}}{local}"
    else:
        if text.startswith("{"):
            return default
        prefix, separator, local = text.partition(":")
        if separator:
            if not is_ncname(prefix) or not is_ncname(local):
                return default
            bound = namespaces.get(prefix)
            if not bound:
                return default
            expanded = f"{{{bound}}}{local}"
        else:
            if not is_ncname(text):
                return default
            expanded = f"Q{{}}{text}"
    standard = f"{{{ERROR_NAMESPACE}}}"
    if expanded.startswith(standard):
        return expanded[len(standard) :]
    return expanded


def _item_string(item: XdmItem) -> str:
    """The string value of one simple-content item.

    XSLT 2.0 §5.7.2 atomizes the sequence before casting to string, so a
    schema-typed element or attribute contributes its *typed value*: a
    canonical xs:duration rather than the source lexical form.  A node whose
    atomization is not one atomic value (an element with complex content)
    falls back to its string value, which is also what an untyped node yields.
    With the XPath 3.1 feature an array is atomized by atomizing its members
    recursively (XSLT 3.0 §27.7.1), so it contributes their strings rather than
    a FOTY0013; a map or other function item still raises FOTY0013.
    """
    if isinstance(item, XdmAtomic):
        return item.string_value
    if isinstance(item, XdmNode):
        typed = item.typed_value
        if len(typed) == 1 and isinstance(typed[0], XdmAtomic):
            return typed[0].string_value
        return item.string_value
    if isinstance(item, XdmArray):
        return " ".join(_array_member_strings(item))
    if isinstance(item, ResultTreeFragment):
        return item.string_value
    raise XsltError(
        "atomization of a function item is not permitted here",
        code="FOTY0013",
    )


def _array_member_strings(item: XdmArray) -> list[str]:
    """Atomize one array's members into the strings simple content uses."""
    return [
        text
        for member in item.members
        for member_item in member
        for text in _atomized_strings(member_item)
    ]


def _atomized_strings(item: XdmItem) -> list[str]:
    """Atomize one item, flattening arrays but never a map or other function."""
    if isinstance(item, XdmArray):
        return _array_member_strings(item)
    return [_item_string(item)]


def _text_nodes_are_adjacent(left: XdmNode, right: XdmNode) -> bool:
    """Whether two text nodes are adjacent in their own tree.

    XSLT 2.0 §5.7.2 merges *adjacent* text nodes before atomization; source
    text nodes from different parents are separate items and the separator
    applies between them.  Text nodes constructed by a sequence constructor are
    parentless until attached, so two parentless text nodes are adjacent.
    """
    left_parent = left.parent
    right_parent = right.parent
    if left_parent is None and right_parent is None:
        return True
    if left_parent is None or left_parent is not right_parent:
        return False
    children = left_parent.children
    try:
        index = children.index(left)
    except ValueError:  # pragma: no cover - the node is in its parent's children
        return False
    return index + 1 < len(children) and children[index + 1] is right


def _simple_content_string(sequence: XdmSequence, separator: str) -> str:
    values: list[str] = []
    pending_text = ""
    pending_node: XdmNode | None = None
    for item in sequence:
        if isinstance(item, XdmNode) and item.kind is NodeKind.TEXT:
            if not item.string_value:
                # XSLT 2.0 section 5.7.2 discards zero-length text nodes.
                continue
            if pending_node is not None and _text_nodes_are_adjacent(pending_node, item):
                pending_text += item.string_value
            else:
                if pending_node is not None:
                    values.append(pending_text)
                pending_text = item.string_value
                pending_node = item
            continue
        if pending_node is not None:
            values.append(pending_text)
            pending_text = ""
            pending_node = None
        if isinstance(item, XdmArray):
            # Atomization flattens arrays in place, so a custom separator
            # applies between each flattened member, not just between arrays.
            values.extend(_array_member_strings(item))
            continue
        values.append(_item_string(item))
    if pending_node is not None:
        values.append(pending_text)
    return separator.join(values)


_STRING_ATOMIC_LOCALS = frozenset(
    {
        "anyURI",
        "ENTITIES",
        "ENTITY",
        "ID",
        "IDREF",
        "IDREFS",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "NMTOKENS",
        "normalizedString",
        "string",
        "token",
        "untypedAtomic",
    }
)


_NUMERIC_ATOMIC_LOCALS = frozenset(
    {
        "byte",
        "decimal",
        "double",
        "float",
        "int",
        "integer",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "short",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)


_START_AT = re.compile(r"-?[0-9]+")


def _analyze_string_value(sequence: XdmSequence) -> str:
    """Apply the function conversion rules for ``xsl:analyze-string/@select``."""
    if not sequence:
        return ""
    if len(sequence) > 1:
        raise XsltDynamicError(
            "xsl:analyze-string select must be a single string",
            code="XPTY0004",
        )
    item = sequence[0]
    if isinstance(item, XdmNode):
        return item.string_value
    if isinstance(item, XdmAtomic):
        local = (
            item.type_name.partition("}")[2]
            if item.type_name.startswith("{")
            else item.type_name.rpartition(":")[2]
        )
        if local in _STRING_ATOMIC_LOCALS:
            return item.lexical
    raise XsltDynamicError(
        "xsl:analyze-string select must be a string or node",
        code="XPTY0004",
    )


def _number_values(sequence: XdmSequence) -> tuple[int, ...]:
    """Convert an atomized ``xsl:number/@value`` sequence to integers."""
    numbers: list[int] = []
    for item in sequence:
        if isinstance(item, XdmNode):
            atomics = tuple(value for value in item.typed_value if isinstance(value, XdmAtomic))
        elif isinstance(item, XdmAtomic):
            atomics = (item,)
        else:
            raise XsltDynamicError(
                "xsl:number value cannot atomize a function item",
                code="FOTY0013",
            )
        for atomic in atomics:
            rounded = _rounded_integer(atomic)
            if rounded is None or rounded < 0:
                raise XsltDynamicError(
                    "xsl:number value must convert to a non-negative integer",
                    code="XTDE0980",
                )
            numbers.append(rounded)
    return tuple(numbers)


def _legacy_number_values(sequence: XdmSequence) -> tuple[int | None, ...]:
    """XSLT 1.0 behavior for ``xsl:number/@value`` (XSLT 3.0 §12.2).

    Every item after the first is discarded.  An empty sequence becomes
    ``NaN``, and a value that cannot be converted to an integer is formatted
    as the literal ``NaN`` instead of raising ``XTDE0980``.
    """
    if not sequence:
        return (None,)
    first = sequence[0]
    if isinstance(first, XdmNode):
        atomics = [value for value in first.typed_value if isinstance(value, XdmAtomic)]
    elif isinstance(first, XdmAtomic):
        atomics = [first]
    else:  # pragma: no cover - the XPath 1.0 grammar has no function items
        raise XsltDynamicError(
            "xsl:number value cannot atomize a function item",
            code="FOTY0013",
        )
    if not atomics:
        return (None,)
    rounded = _rounded_integer(atomics[0])
    if rounded is None:
        return (None,)
    if rounded < 0:
        raise XsltDynamicError(
            "xsl:number value must convert to a non-negative integer",
            code="XTDE0980",
        )
    return (rounded,)


def _rounded_integer(item: XdmAtomic) -> int | None:
    local = (
        item.type_name.partition("}")[2]
        if item.type_name.startswith("{")
        else item.type_name.rpartition(":")[2]
    )
    value: object = item.value
    if local not in _NUMERIC_ATOMIC_LOCALS:
        if local not in _STRING_ATOMIC_LOCALS:
            return None
        lexical = item.lexical.strip()
        if lexical in ("INF", "+INF", "-INF", "NaN"):
            return None
        try:
            value = Decimal(lexical)
        except InvalidOperation:
            return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int((value + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR))
    assert isinstance(value, float)
    if math.isnan(value) or math.isinf(value):
        return None
    return math.floor(value + 0.5)


def _rebase_place_marker(
    numbers: Sequence[int],
    start_at: str | None,
) -> tuple[int, ...]:
    if start_at is None:
        return tuple(numbers)
    parts = start_at.split()
    if not parts or any(_START_AT.fullmatch(part) is None for part in parts):
        raise XsltDynamicError(
            "xsl:number start-at must be a whitespace-separated integer sequence",
            code="XTDE0030",
        )
    starts = [int(part) for part in parts]
    return tuple(
        number + (starts[index] if index < len(starts) else starts[-1]) - 1
        for index, number in enumerate(numbers)
    )


_EBV_STRING_TYPES = frozenset(
    {
        "anyURI",
        "ENTITY",
        "ID",
        "IDREF",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "normalizedString",
        "string",
        "token",
        "untypedAtomic",
    }
)


_EBV_NUMERIC_TYPES = frozenset(
    {
        "byte",
        "decimal",
        "double",
        "float",
        "int",
        "integer",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "short",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)


_BUILTIN_TYPE_NAMES: dict[type[Any], str] | None = None


def _builtin_class_names() -> Mapping[type[Any], str]:
    """Map every canonical built-in datatype class to its XSD local name."""
    global _BUILTIN_TYPE_NAMES
    if _BUILTIN_TYPE_NAMES is None:
        _BUILTIN_TYPE_NAMES = {value: name for name, value in builtin_type_classes().items()}
    return _BUILTIN_TYPE_NAMES


def _primitive_local_name(item: XdmAtomic) -> str:
    """Classify an atomic value by its nearest built-in datatype ancestor."""
    names = _builtin_class_names()
    datatype = item.datatype
    if datatype is not None:
        for base in datatype.__mro__:
            name = names.get(base)
            if name is not None:
                return name
    for base in type(item.value).__mro__:
        name = names.get(base)
        if name is not None:
            return name
    return item.type_name.rpartition("}")[2]


def _effective_boolean(sequence: XdmSequence) -> bool:
    """Evaluate the XPath effective boolean value of one XDM sequence.

    Nodes are true by their presence.  A single boolean, string-family, or
    numeric atomic follows the specification's value rules.  A multi-item
    atomic sequence, a function item, or an unsupported atomic type raises
    ``FORG0006`` instead of guessing from the first item.
    """
    if not sequence:
        return False
    first = sequence[0]
    if isinstance(first, ResultTreeFragment):
        # XSLT 1.0 §11.1: a result tree fragment is a node-set with one root
        # node, so its boolean conversion is always true.
        return True
    if isinstance(first, XdmNode):
        return True
    if not isinstance(first, XdmAtomic):
        raise XsltError(
            "the effective boolean value of a function item is not defined",
            code="FORG0006",
        )
    if len(sequence) != 1:
        raise XsltError(
            "the effective boolean value of a multi-item atomic sequence is not defined",
            code="FORG0006",
        )
    local = _primitive_local_name(first)
    if local == "boolean":
        return bool(first.value)
    if local in _EBV_STRING_TYPES:
        return first.lexical != ""
    if local in _EBV_NUMERIC_TYPES:
        value = first.value
        # NaN is the only numeric value not equal to itself.
        if value != value:
            return False
        return value != 0
    raise XsltError(
        f"the effective boolean value of {local or 'this atomic type'} is not defined",
        code="FORG0006",
    )


def _error_qname(code: str) -> XdmAtomic:
    """Return one expanded error code as an ``xs:QName`` atomic item.

    Implementation codes are not required to be lexical QNames.  A code whose
    local part is not an NCName is preserved as a string rather than making a
    caught error unreadable; all specification and ``fn:error`` codes use the
    typed branch.
    """
    if code.startswith("{"):
        uri, _, local = code[1:].partition("}")
    else:
        uri, local = "", code
    if is_ncname(local):
        return cast(XdmAtomic, as_xdm(QName(uri, local))[0])
    return XdmAtomic.string(code)


def literal_element(
    plan: _LiteralPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmNode:
    factory, element = _prepare_literal_element(plan, invocation, context)
    content = invocation._execute_plans(plan.children, context=context)
    return _finish_literal_element(plan, invocation, context, factory, element, content)


def _prepare_literal_element(
    plan: _LiteralPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> tuple[_NodeFactory, XdmNode]:
    factory = _NodeFactory()
    attribute_set_attributes: list[tuple[AttributeName, str]] = []
    for reference in plan.attribute_sets:
        component = invocation._resolve_attribute_set(reference, context.package_name)
        attribute_set_attributes.extend(
            (
                AttributeName(attribute.name or "", attribute.prefix),
                attribute.string_value,
            )
            for attribute in invocation._attribute_set_attributes(component, context)
            if attribute.kind is NodeKind.ATTRIBUTE
        )
    attributes = tuple(
        (name, invocation._value_source(value, context)) for name, value in plan.attributes
    )
    element = factory.element(
        plan.instruction.name,
        attributes=attributes,
        attribute_set_attributes=tuple(attribute_set_attributes),
        namespaces=dict(plan.namespaces),
        base_uri=plan.instruction.base_uri,
        prefix=plan.instruction.prefix,
        excluded_namespaces=plan.excluded_namespaces,
        namespace_alias_rules=plan.namespace_aliases,
    )
    return factory, element


def _finish_literal_element(
    plan: _LiteralPlan,
    invocation: _Invocation,
    context: EvaluationContext,
    factory: _NodeFactory,
    element: XdmNode,
    content: XdmSequence,
) -> XdmNode:
    _append_complex_content(factory, element, content, allow_attributes=True)
    validate_constructed(
        element,
        validation=plan.validation,
        type_name=plan.type_name,
        schema_pool=invocation._package_schema_pool(context.package_name),
    )
    factory.finalize_root(element)
    return element


def element_value(
    plan: _ElementPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmNode:
    lexical = invocation._value_source(plan.name, context)
    namespace_value = (
        None if plan.namespace is None else invocation._value_source(plan.namespace, context)
    )
    name = _constructed_qname(
        lexical,
        namespace_value,
        plan.namespaces,
        default_namespace=True,
        code="XTDE0820",
        namespace_code="XTDE0835",
        prefix_code="XTDE0830",
    )
    attributes: list[tuple[AttributeName, str]] = []
    for reference in plan.attribute_sets:
        component = invocation._resolve_attribute_set(reference, context.package_name)
        attributes.extend(
            (
                AttributeName(attribute.name or "", attribute.prefix),
                attribute.string_value,
            )
            for attribute in invocation._attribute_set_attributes(component, context)
            if attribute.kind is NodeKind.ATTRIBUTE
        )
    builder = ResultBuilder.temporary(base_uri=plan.instruction.base_uri)
    with builder.transaction():
        builder.element(
            name,
            attributes=tuple(attributes),
            base_uri=plan.instruction.base_uri,
            prefix=_lexical_prefix(lexical),
            force_prefix=True,
            inherit_namespaces=plan.inherit_namespaces,
        )
        builder.append(
            invocation._execute_plans(plan.children, context=context),
            inherit_namespaces=plan.inherit_namespaces,
        )
        element = builder.root.root_element
        builder.to_tree()
    validate_constructed(
        element,
        validation=plan.validation,
        type_name=plan.type_name,
        schema_pool=invocation._package_schema_pool(context.package_name),
    )
    return element


def document_value(
    plan: _DocumentPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmDocument:
    pool = invocation._package_schema_pool(context.package_name)
    builder = ResultBuilder.document(
        base_uri=plan.instruction.base_uri,
        schema_pool=pool,
        final_output=False,
    )
    with builder.transaction():
        builder.append(invocation._execute_plans(plan.children, context=context))
        document = builder.to_tree()
    return validate_delivered(
        document,
        validation=plan.validation,
        type_name=plan.type_name,
        schema_pool=pool,
    )


def copy_value(
    plan: _CopyPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    if plan.select is not None:
        selected = invocation._evaluate(plan.select, context)
        if not selected:
            return ()
        if len(selected) != 1:
            raise XsltDynamicError(
                "xsl:copy select must evaluate to exactly one item",
                code="XTTE3180",
            )
        item = selected[0]
        content_focus = Focus.singleton(item)
    else:
        if context.focus.item is None:
            raise XsltDynamicError(
                "xsl:copy requires an existing context item",
                code="XTTE0945",
            )
        item = context.focus.item
        content_focus = context.focus
    if not isinstance(item, XdmNode):
        return (item,)
    content_context = replace(context, focus=content_focus)
    pool = invocation._package_schema_pool(context.package_name)
    factory = _NodeFactory(base_uri=plan.instruction.base_uri)
    if item.kind is NodeKind.DOCUMENT:
        document = factory.document
        content = invocation._execute_plans(plan.children, context=content_context)
        _append_complex_content(
            factory,
            document,
            content,
            allow_attributes=False,
            inherit_namespaces=plan.inherit_namespaces,
        )
        document._finalize_document_order()
        check_namespace_sensitive_copy(
            document,
            validation=plan.validation,
            type_name=plan.type_name,
            copy_namespaces=plan.copy_namespaces,
        )
        return (
            validate_delivered(
                document,
                validation=plan.validation,
                type_name=plan.type_name,
                schema_pool=pool,
            ),
        )
    clone = factory.clone(
        item,
        parent=None,
        deep=False,
        copy_namespaces=plan.copy_namespaces,
        copy_attributes=False,
        # A parentless xsl:copy keeps the copied node's base; if it is later
        # attached to a constructed tree, _adopts_parent_base rebases it.
        preserve_base=True,
    )
    clone._adopts_parent_base = clone.kind is NodeKind.ELEMENT
    if item.kind is NodeKind.ATTRIBUTE:
        check_copy_type(item, plan.type_name, pool)
    if item.kind is NodeKind.ELEMENT:
        attributes: list[XdmNode] = []
        for reference in plan.attribute_sets:
            component = invocation._resolve_attribute_set(reference, context.package_name)
            attributes.extend(invocation._attribute_set_attributes(component, content_context))
        if attributes:
            _append_complex_content(factory, clone, attributes, allow_attributes=True)
        content = invocation._execute_plans(plan.children, context=content_context)
        if content:
            _append_complex_content(
                factory,
                clone,
                content,
                allow_attributes=True,
                inherit_namespaces=plan.inherit_namespaces,
            )
    check_namespace_sensitive_copy(
        clone,
        validation=plan.validation,
        type_name=plan.type_name,
        copy_namespaces=plan.copy_namespaces,
    )
    validate_constructed(
        clone,
        validation=plan.validation,
        type_name=plan.type_name,
        schema_pool=pool,
        operation="copy",
    )
    if clone.kind is NodeKind.ELEMENT:
        factory.finalize_root(clone)
    return (clone,)


def check_namespace_sensitive_copy(
    node: XdmNode,
    *,
    validation: str,
    type_name: str | None,
    copy_namespaces: bool,
) -> None:
    """Reject a copy that cannot preserve namespace-sensitive content.

    XSLT 3.0 section 11.9 makes this a type error when ``copy-namespaces="no"``
    and validation is preserved, and also when the copy produces a parentless
    attribute with a QName or NOTATION typed value: an attribute node has no
    namespace nodes of its own, so the value's prefix would no longer resolve.
    """
    if validation != "preserve" or type_name is not None:
        return
    if node.kind is NodeKind.ATTRIBUTE and namespace_sensitive(node):
        raise XsltDynamicError(
            "namespace-sensitive content cannot be copied without a parent element",
            code="XTTE0950",
        )
    if copy_namespaces:
        return
    if namespace_sensitive(node):
        raise XsltDynamicError(
            "namespace-sensitive content cannot be copied without namespace nodes",
            code="XTTE0950",
        )


def deep_copy_item(
    item: XdmItem,
    invocation: _Invocation,
    *,
    copy_namespaces: bool = True,
    copy_accumulators: bool = True,
    validation: str = "preserve",
    type_name: str | None = None,
    schema_pool: Any = None,
) -> XdmItem:
    """Copy one item according to XSLT 3.0 section 11.9.2.

    Defaults are ``fn:copy-of``'s fixed options (section 18.3):
    ``copy-namespaces="yes"``, ``copy-accumulators="yes"``, and
    ``validation="preserve"``.  ``xsl:copy-of`` passes its own attributes.
    An atomic value or function item is returned unchanged, and a copy is a
    new node in a new temporary tree, never the original.
    """
    if isinstance(item, ResultTreeFragment):
        # XSLT 1.0 §11.1: copying a result tree fragment copies its
        # children, never the fragment's root node.
        item = item.document
    if not isinstance(item, XdmNode):
        return item
    check_copy_type(item, type_name, schema_pool)
    factory = _NodeFactory()
    clone = factory.clone(
        item,
        parent=None,
        deep=True,
        copy_namespaces=copy_namespaces,
    )
    names = accumulator_copy_names(invocation) if copy_accumulators else ()
    invocation.accumulators.copy_values(
        item,
        clone,
        enabled=copy_accumulators,
        names=names,
    )
    check_namespace_sensitive_copy(
        clone,
        validation=validation,
        type_name=type_name,
        copy_namespaces=copy_namespaces,
    )
    if clone.kind is NodeKind.DOCUMENT:
        return validate_delivered(
            cast(XdmDocument, clone),
            validation=validation,
            type_name=type_name,
            schema_pool=schema_pool,
        )
    validate_constructed(
        clone,
        validation=validation,
        type_name=type_name,
        schema_pool=schema_pool,
        operation="copy-of",
    )
    if clone.kind is NodeKind.ELEMENT:
        factory.finalize_root(clone)
    return clone


def copy_of_value(
    plan: _CopyOfPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    items = invocation._evaluate(plan.select, context)
    pool = invocation._package_schema_pool(context.package_name)
    return tuple(
        deep_copy_item(
            item,
            invocation,
            copy_namespaces=plan.copy_namespaces,
            copy_accumulators=plan.copy_accumulators,
            validation=plan.validation,
            type_name=plan.type_name,
            schema_pool=pool,
        )
        for item in items
    )


def snapshot_item(item: XdmItem, invocation: _Invocation) -> XdmItem:
    """One item of an ``fn:snapshot`` sequence (XSLT 3.0 section 18.4).

    A parentless node behaves exactly like ``copy-of``: it is deep-copied on
    its own.  Any other node is copied with a chain of shallow ancestor copies,
    so the copy keeps its ancestors' names, attributes, and namespaces but
    none of their other children or siblings, and every copied ancestor
    carries the mandated ``xs:anyType`` annotation.  Atomic values and function
    items are returned unchanged.
    """
    if not isinstance(item, XdmNode):
        return item
    if item.parent is None:
        return deep_copy_item(item, invocation)
    factory = _NodeFactory(base_uri=item.root.base_uri)
    clone = factory.clone(item, parent=None, deep=True, copy_namespaces=True)
    result = clone
    current = clone
    ancestor: XdmNode | None = item.parent
    parent_copy: XdmNode
    while ancestor is not None:
        if ancestor.kind is NodeKind.DOCUMENT:
            parent_copy = factory.document
        else:
            parent_copy = factory.clone(
                ancestor,
                parent=None,
                deep=False,
                copy_namespaces=True,
            )
            _annotate_any_type_copy(parent_copy)
        attached = _attach_grafted_node(factory, parent_copy, current)
        if current is result:
            result = attached
        current = parent_copy
        ancestor = ancestor.parent
    if isinstance(current, XdmDocument):
        current._finalize_document_order()
    else:
        factory.finalize_root(current)
    # The copied subtree must already sit in its destination tree: accumulator
    # values are cached per tree root, and the root is the grafted top here.
    invocation.accumulators.copy_values(
        item,
        clone,
        enabled=True,
        names=accumulator_copy_names(invocation),
    )
    return result


def _attach_grafted_node(
    factory: _NodeFactory,
    parent: XdmNode,
    node: XdmNode,
) -> XdmNode:
    """Attach an already-copied node under *parent* and return its tree node.

    A ``snapshot()`` ancestor copy is shallow and is filled with exactly one
    copied node, so no content merging applies; attributes and namespace nodes
    take their own parent slots, and ``attach_namespace`` may hand back an
    equivalent declaration the shallow parent already carries.
    """
    if node.kind is NodeKind.ATTRIBUTE:
        factory.attach_attribute(parent, node)
        return node
    if node.kind is NodeKind.NAMESPACE:
        return factory.attach_namespace(parent, node) or node
    factory.attach_node(parent, node)
    return node


def check_copy_type(
    item: XdmNode,
    type_name: str | None,
    schema_pool: Any,
) -> None:
    if type_name is None or item.kind is not NodeKind.ATTRIBUTE:
        return
    adapter = schema_pool.get_type(type_name) if schema_pool is not None else None
    if adapter is not None and not adapter.is_simple():
        raise XsltDynamicError(
            "a copy of an attribute cannot be validated against a complex type",
            code="XTTE1535",
        )


def parameter_value(
    sequence: XdmSequence,
    profile: LanguageProfile,
    *,
    content: bool,
) -> XdmSequence:
    """Bind one ``xsl:with-param`` value in the profile's value model.

    XSLT 1.0 §11.6 specifies a with-parameter exactly like a variable: content
    initialization is a result tree fragment when content is present and the
    empty string when content is absent (§11.2).  A later processor delivers
    the constructed sequence, or the empty sequence for absent content.
    """
    if not content:
        return (XdmAtomic.string(""),) if profile.legacy else ()
    if profile.result_tree_fragments:
        return (ResultTreeFragment(_build_result_tree(sequence)),)
    return sequence


def variable_value(
    plan: _VariablePlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    if plan.expression is not None:
        value = invocation._evaluate(plan.expression, context)
    elif plan.children:
        sequence = invocation._execute_plans(
            plan.children,
            context=replace(context, final_output=False),
        )
        value = (
            sequence
            if plan.has_as
            else (_build_result_tree(sequence, base_uri=plan.instruction.base_uri),)
        )
    elif plan.has_as:
        value = ()
    else:
        # XSLT 1.0 §11.2 and XSLT 2.0/3.0 agree: an empty-content
        # variable-binding element without @select binds an empty string.
        value = (XdmAtomic.string(""),)
    if plan.sequence_type is not None:
        value = invocation._coerce(
            value,
            plan.sequence_type,
            code="XTTE0570",
            label=f"variable {plan.name!r}",
            package_name=context.package_name,
            clear_output_escaping=True,
        )
    invocation.profile_for().check_xdm_2_0_value(value, f"the value of the variable {plan.name!r}")
    return value


def attribute_value(
    plan: _AttributePlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    lexical = invocation._value_source(plan.name, context)
    namespace_value = (
        None if plan.namespace is None else invocation._value_source(plan.namespace, context)
    )
    prefix, separator, _ = lexical.partition(":")
    if not lexical:
        raise XsltDynamicError("an attribute name must not be empty", code="XTDE0850")
    name = _constructed_qname(
        lexical,
        namespace_value,
        plan.namespaces,
        default_namespace=False,
        code="XTDE0850",
        namespace_code="XTDE0865",
        prefix_code="XTDE0860",
    )
    if plan.expression is not None:
        value = invocation._evaluate(plan.expression, context)
    else:
        value = invocation._execute_plans(plan.children, context=context)
    separator_value = (
        invocation._value_source(plan.separator, context) if plan.separator is not None else " "
    )
    attributes = construct_attribute(
        name,
        _simple_content_string(value, separator_value),
        prefix=prefix if separator else None,
    )
    validate_constructed(
        cast(XdmNode, attributes[0]),
        validation=plan.validation,
        type_name=plan.type_name,
        schema_pool=invocation._package_schema_pool(context.package_name),
        operation="attribute",
    )
    return attributes


def execute_fork(
    plan: _ForkPlan, invocation: _Invocation, *, context: EvaluationContext
) -> XdmSequence:
    result: list[XdmItem] = []
    for branch in plan.branches:
        result.extend(invocation._execute_plans(branch, context=context))
    return tuple(result)


def execute_source_document(
    plan: _SourceDocumentPlan,
    invocation: _Invocation,
    *,
    context: EvaluationContext,
) -> XdmSequence:
    package_name = context.package_name
    href = invocation._value_template(plan.href, context).strip()
    document = invocation.document_resources.resolve(
        href,
        base=plan.instruction.base_uri,
        validation=plan.validation,
        type_name=plan.type_name,
        schema_pool=invocation._package_schema_pool(package_name),
        input_type_annotations=invocation._document_annotations(
            package_name,
            validation=plan.validation,
            type_name=plan.type_name,
        ),
        package=package_name,
    )
    invocation.accumulators.restrict(document, plan.use_accumulators)
    return invocation._execute_plans(
        plan.children,
        context=replace(context, focus=Focus.singleton(document)),
    )


def execute_analyze_string(
    plan: _AnalyzeStringPlan,
    invocation: _Invocation,
    *,
    context: EvaluationContext,
) -> XdmSequence:
    values = invocation._evaluate(plan.expression, context)
    text = _analyze_string_value(values)
    regex_source = invocation._value_source(plan.regex, context)
    flags = invocation._value_source(plan.flags, context) if plan.flags is not None else ""
    profile = invocation.profile_for(plan.expression.static_context.xslt_version)
    try:
        regex = compile_xpath_regex(
            regex_source,
            flags,
            allowed_flags=profile.regex_flags,
        )
    except XPathRegexError as exc:
        code = "XTDE1145" if exc.code == "FORX0001" else "XTDE1140"
        raise XsltDynamicError(str(exc), code=code) from exc
    if profile.native and regex.match("", 0) is not None:
        # A native XSLT 2.0 processor follows XSLT 2.0 §15.1, where a
        # zero-length match is a dynamic error; the 3.0 algorithm (used by the
        # 3.0 processor) skips the empty position instead.
        raise XsltDynamicError(
            "xsl:analyze-string requires a regular expression that does not match "
            "a zero-length string",
            code="XTDE1150",
        )
    substrings = analyze_string(regex, text)
    result: list[XdmItem] = []
    for position, substring in enumerate(substrings, start=1):
        branch = plan.matching if substring.matched else plan.non_matching
        if not branch:
            continue
        branch_context = replace(
            context,
            focus=Focus.singleton(
                XdmAtomic.string(substring.value),
                position=position,
                size=len(substrings),
            ),
        )
        with invocation._regex_scope(substring.groups if substring.matched else None):
            result.extend(invocation._execute_plans(branch, context=branch_context))
    return tuple(result)


def execute_number(
    plan: _NumberPlan,
    invocation: _Invocation,
    *,
    context: EvaluationContext,
) -> XdmSequence:
    if plan.value is not None:
        values = invocation._evaluate(plan.value, context)
        profile = invocation.profile_for(plan.value.static_context.xslt_version)
        if profile.legacy or profile.compatibility_mode:
            numbers = _legacy_number_values(values)
        else:
            numbers = _number_values(values)
    else:
        if plan.select is not None:
            selected = invocation._evaluate(plan.select, context)
            if len(selected) != 1 or not isinstance(selected[0], XdmNode):
                raise XsltDynamicError(
                    "xsl:number select must evaluate to a single node",
                    code="XTTE1000",
                )
            node = selected[0]
        else:
            item = context.focus.item
            if not isinstance(item, XdmNode):
                raise XsltDynamicError(
                    "xsl:number without select or value requires a context node",
                    code="XTTE0990",
                )
            node = item
        count_match = number_matcher(invocation, plan.count, node, context)
        from_match = (
            number_matcher(invocation, plan.from_pattern, node, context)
            if plan.from_pattern is not None
            else None
        )
        counter = invocation.numbering_counter(plan, node, count_match, from_match)
        numbers = place_marker(node, plan.level, count_match, from_match, counter=counter)
    start_at = (
        invocation._value_source(plan.start_at, context) if plan.start_at is not None else None
    )
    if all(number is not None for number in numbers):
        numbers = _rebase_place_marker(cast(Sequence[int], numbers), start_at)
    format_ = (
        plan.format
        if isinstance(plan.format, str)
        else invocation._value_source(plan.format, context)
    )
    lang = invocation._value_source(plan.lang, context) if plan.lang is not None else ""
    ordinal = invocation._value_source(plan.ordinal, context) if plan.ordinal is not None else "no"
    letter_value = (
        invocation._value_source(plan.letter_value, context)
        if plan.letter_value is not None
        else ""
    )
    grouping_separator = (
        invocation._value_source(plan.grouping_separator, context)
        if plan.grouping_separator is not None
        else None
    )
    grouping_size = (
        invocation._value_source(plan.grouping_size, context)
        if plan.grouping_size is not None
        else None
    )
    text = format_place_marker(
        numbers,
        format_,
        lang=lang,
        ordinal=ordinal.strip().lower() in ("yes", "1", "true"),
        letter_value=letter_value,
        grouping_separator=grouping_separator,
        grouping_size=grouping_size,
    )
    return (invocation._text_node(text),)


def number_matcher(
    invocation: _Invocation,
    pattern: CompiledPattern | None,
    selected: XdmNode,
    context: EvaluationContext,
) -> Callable[[XdmNode], bool]:
    if pattern is None:
        kind = selected.kind
        name = selected.name

        def implied(candidate: XdmNode) -> bool:
            return candidate.kind is kind and candidate.name == name

        return implied
    matcher = PatternMatcher(
        pattern,
        # An element-rooted temporary tree has no document node above its root,
        # so a top-level pattern step's parent context is itself a parentless
        # element.  The top-axis adjustment belongs to the candidate, not to
        # that parent; without it a count pattern cannot match a descendant of
        # the temporary tree.  Template matching keeps its existing behavior.
        rootless=selected.root.kind is NodeKind.ELEMENT,
    )
    environment = invocation._pattern_environment_for(
        pattern,
        context.package_name,
        locals_=context.locals,
    )

    def matches(candidate: XdmNode) -> bool:
        return matcher.matches(candidate, environment)

    return matches


def execute_assert(
    plan: _AssertPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> None:
    if not plan.enabled:
        return
    failure: BaseException | None = None
    satisfied = False
    try:
        satisfied = _effective_boolean(invocation._evaluate(plan.test, context))
    except Exception as exc:
        if is_budget_alarm(exc):
            raise
        failure = exc
    if satisfied:
        return
    message_failure: BaseException | None = None
    detail = ""
    value: tuple[XdmItem, ...] = ()
    try:
        message_values: list[XdmItem] = []
        if plan.select is not None:
            message_values.extend(invocation._evaluate(plan.select, context))
        message_values.extend(
            invocation._execute_plans(
                plan.children,
                context=replace(context, final_output=False),
            )
        )
        if message_values:
            document = _build_result_tree(message_values)
            detail = document.string_value.strip()
            value = (document,)
    except RecursionError:
        # Let the scheduler diagnose stack exhaustion before formatting it
        # as an assertion failure.
        raise
    except Exception as exc:
        if is_budget_alarm(exc):
            raise
        message_failure = exc
        detail = f"message construction failed: {exc}"
    code = "XTMM9001"
    if plan.error_code is not None:
        try:
            selected = invocation._value_source(plan.error_code, context)
            code = _diagnostic_error_code(selected, plan.instruction.namespaces)
        except Exception as exc:
            if is_budget_alarm(exc):
                raise
            if message_failure is None:
                message_failure = exc
                if not detail:
                    detail = f"message construction failed: {exc}"
            code = "XTMM9001"
    if detail:
        message = f"assertion failed: {detail} (test: {plan.test.source})"
    else:
        message = f"assertion failed: {plan.test.source} evaluated to false"
    raise XsltDynamicError(
        message,
        code=code,
        location=plan.instruction.location,
        cause=failure or message_failure,
        value=value,
    )


def execute_message(
    plan: _MessagePlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> None:
    content: XdmDocument | None = None
    try:
        values = () if plan.select is None else invocation._evaluate(plan.select, context)
        values += invocation._execute_plans(
            plan.children, context=replace(context, final_output=False)
        )
        # XSLT 3.0 section 18.1: the sequence constructor builds a document
        # node whose string value is the displayed text; a listener or a
        # conformance assertion inspects that node, not only the text.
        content = _build_result_tree(values)
        text = content.string_value
    except (XsltError, ElementPathError, XPathEvaluationError) as error:
        if catchable_error(invocation, error, invocation.diagnostics) is None:
            raise
        text = ""
    terminate = invocation._value_source(plan.terminate, context).strip()
    if terminate not in ("yes", "true", "1", "no", "false", "0"):
        raise XsltDynamicError("invalid xsl:message terminate value", code="XTDE0030")
    terminating = terminate in ("yes", "true", "1")
    code = (
        "XTMM9000"
        if plan.error_code is None
        else _diagnostic_error_code(
            invocation._value_source(plan.error_code, context),
            plan.instruction.namespaces,
            default="XTMM9000",
        )
    )
    invocation.messages.append(Message(text, code=code, terminate=terminating, content=content))
    if terminating:
        raise XsltDynamicError(text, code=code, location=plan.instruction.location)


def execute_conditional_sequence(
    plan: _ConditionalSequencePlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    result: list[XdmItem] = []
    deferred: list[tuple[_OnNonEmptyPlan, Mapping[str, XdmSequence]]] = []
    local_scope = dict(context.locals)
    populated = False
    try:
        for item in plan.plans:
            if isinstance(item, _ParamPlan):
                raise XsltError(
                    "xsl:param is only supported at the start of a template or function",
                    code="XTDE0000",
                )
            item_context = replace(context, locals=local_scope)
            if isinstance(item, _VariablePlan):
                local_scope[item.name] = variable_value(item, invocation, item_context)
                continue
            if isinstance(item, _OnNonEmptyPlan):
                if populated:
                    result.extend(conditional_value(invocation, item, item_context))
                else:
                    deferred.append((item, dict(local_scope)))
                continue
            if isinstance(item, _OnEmptyPlan):
                if not populated:
                    result.clear()
                    result.extend(conditional_value(invocation, item, item_context))
                continue
            values = invocation._execute_plan(item, context=item_context)
            for value in values:
                if not populated and not is_vacuous(value):
                    for deferred_plan, deferred_scope in deferred:
                        result.extend(
                            conditional_value(
                                invocation,
                                deferred_plan,
                                replace(context, locals=deferred_scope),
                            )
                        )
                    deferred.clear()
                    populated = True
                result.append(value)
    except _IterationControlSignal as signal:
        if not populated and any(not is_vacuous(value) for value in signal.pending):
            for deferred_plan, deferred_scope in deferred:
                result.extend(
                    conditional_value(
                        invocation,
                        deferred_plan,
                        replace(context, locals=deferred_scope),
                    )
                )
            deferred.clear()
        signal.prepend(result)
        raise
    return tuple(result)


def conditional_value(
    invocation: _Invocation,
    plan: _OnEmptyPlan | _OnNonEmptyPlan,
    context: EvaluationContext,
) -> XdmSequence:
    if plan.expression is not None:
        return invocation._evaluate(plan.expression, context)
    return invocation._execute_plans(plan.children, context=context)


def choose_branch(
    invocation: _Invocation,
    plan: _ChoosePlan,
    context: EvaluationContext,
) -> tuple[_Plan, ...] | None:
    for test, branch in plan.branches:
        if _effective_boolean(invocation._evaluate(test, context)):
            return branch
    return plan.otherwise


def try_step(invocation: _Invocation, plan: _TryPlan, frame: Frame, cursor: int) -> FrameResult:
    """Run one try body as a child frame with a catch error handler.

    A tail try whose caller has no remaining work can replace that caller,
    transferring already-produced output and the caller's continuation to
    the try frame.  The handler therefore starts a catch frame with the
    same committed output, and a tail call in the catch body can replace
    the catch frame without growing the explicit stack.
    """
    program = cast(tuple[_Plan, ...], frame.program or ())
    remaining = program[cursor + 1 :]
    replace_caller = (
        plan.tail
        and all(isinstance(item, _RestoreVariablesPlan) for item in remaining)
        and frame.error_handler is None
        and frame.extent is None
        and frame.conversion is None
    )
    base_values = frame.values if replace_caller else ()
    child = Frame(
        kind="literal-content" if frame.kind == "literal-content" else "instruction",
        component=frame.component,
        identity="xsl:try",
        program=try_body_program(plan),
        context=replace(frame.context, count_output=False)
        if frame.context.count_output
        else frame.context,
        continuation=frame.continuation if replace_caller else None,
        extent=invocation.result_extent() if plan.rollback_output else None,
        error_handler=try_handler(invocation, plan, frame, base_values),
        location=plan.instruction.location,
        values=base_values,
        group=frame.group,
        grouping_key=frame.grouping_key,
        iteration=frame.iteration,
    )
    if replace_caller:
        return Continue(child)
    return Call(child, resume=replace(frame, cursor=cursor + 1), tail=False)


def try_body_program(plan: _TryPlan) -> tuple[_Plan, ...]:
    if plan.expression is None:
        return plan.children
    return (
        _SequencePlan(
            instruction=plan.instruction,
            expression=plan.expression,
            children=(),
        ),
    )


def catch_program(catch: _CatchPlan) -> tuple[_Plan, ...]:
    if catch.expression is None:
        return catch.children
    return (
        _SequencePlan(
            instruction=catch.instruction,
            expression=catch.expression,
            children=(),
        ),
    )


def try_handler(
    invocation: _Invocation,
    plan: _TryPlan,
    frame: Frame,
    base_values: tuple[object, ...],
) -> Any:
    def handler(
        error: BaseException,
        stack: tuple[Any, ...],
    ) -> CatchResult | None:
        caught = catchable_error(invocation, error, stack)
        if caught is None:
            return None
        catch = select_catch(plan, caught)
        if catch is None:
            return None
        variables: dict[str, XdmSequence] = dict(frame.context.locals)
        variables.update(catch_variables(caught))
        catch_frame = Frame(
            kind="literal-content" if frame.kind == "literal-content" else "instruction",
            component=frame.component,
            identity="xsl:catch",
            program=catch_program(catch),
            context=replace(frame.context, locals=variables, count_output=False),
            location=catch.instruction.location,
            values=base_values,
            group=frame.group,
            grouping_key=frame.grouping_key,
            iteration=frame.iteration,
        )
        return CatchResult(frame=catch_frame)

    return handler


def execute_try(
    plan: _TryPlan, invocation: _Invocation, *, context: EvaluationContext
) -> XdmSequence:
    """Run a try in a synchronous nested sequence constructor."""
    extent = invocation.result_extent() if plan.rollback_output else None
    try:
        if plan.expression is not None:
            value = invocation._evaluate(plan.expression, context)
        else:
            value = invocation._execute_plans(plan.children, context=context)
    except BaseException as error:
        caught = catchable_error(invocation, error, invocation.diagnostics)
        catch = None if caught is None else select_catch(plan, caught)
        if caught is None or catch is None:
            if extent is not None:
                extent.rollback()
            raise
        if extent is not None:
            extent.rollback()
        variables: dict[str, XdmSequence] = dict(context.locals)
        variables.update(catch_variables(caught))
        catch_context = replace(context, locals=variables)
        if catch.expression is not None:
            return invocation._evaluate(catch.expression, catch_context)
        return invocation._execute_plans(catch.children, context=catch_context)
    else:
        if extent is not None:
            extent.commit()
        return value


def catchable_error(
    invocation: _Invocation,
    error: BaseException,
    stack: tuple[Any, ...] | None,
) -> XsltError | None:
    """Return the deliberate XSLT dynamic error a catch may consume.

    XPath engine errors are converted here with their original exception
    retained as the cause.  Static errors, foreign callback failures, and
    every other Python exception keep their identity and are not caught.
    """
    if isinstance(error, XsltError):
        return error if error.catchable else None
    if isinstance(error, (ElementPathError, XPathEvaluationError)):
        code = error_code_from_exception(error)
        if code is None or is_static_error_code(code):
            return None
        return invocation.boundary_error(error, stack=stack, catchable=True)
    return None


def select_catch(plan: _TryPlan, error: XsltError) -> _CatchPlan | None:
    for catch in plan.catches:
        if error.matches_catch(
            catch.errors,
            namespaces=catch.instruction.namespaces,
        ):
            return catch
    return None


def catch_variables(error: XsltError) -> dict[str, XdmSequence]:
    variables: dict[str, XdmSequence] = {}
    code = error.expanded_code or ""
    variables[f"{{{ERROR_NAMESPACE}}}code"] = (_error_qname(code),)
    variables[f"{{{ERROR_NAMESPACE}}}description"] = (
        (XdmAtomic.string(error.description),) if error.has_description else ()
    )
    variables[f"{{{ERROR_NAMESPACE}}}value"] = tuple(error.value)
    module = (
        error.module or error.uri or (None if error.location is None else error.location.source)
    )
    variables[f"{{{ERROR_NAMESPACE}}}module"] = (
        () if module is None else (XdmAtomic.string(module),)
    )
    variables[f"{{{ERROR_NAMESPACE}}}line-number"] = (
        () if error.line_number is None else (XdmAtomic.integer(error.line_number),)
    )
    variables[f"{{{ERROR_NAMESPACE}}}column-number"] = (
        () if error.column_number is None else (XdmAtomic.integer(error.column_number),)
    )
    return variables


def execute_value_of(
    plan: _ValueOfPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Evaluate one ``xsl:value-of`` into its constructed text node."""
    if plan.expression is not None:
        values = invocation._evaluate(plan.expression, context)
    else:
        values = invocation._execute_plans(plan.children, context=context)
    if plan.expression is not None and not plan.explicit_separator:
        profile = invocation.profile_for(plan.expression.static_context.xslt_version)
        if profile.compatibility_mode:
            return (
                invocation._text_node(
                    profile.string_sequence(values),
                    disable_output_escaping=plan.disable_output_escaping,
                ),
            )
    separator = invocation._value_source(plan.separator, context)
    text = _simple_content_string(values, separator)
    return (invocation._text_node(text, disable_output_escaping=plan.disable_output_escaping),)


def execute_choose(
    plan: _ChoosePlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Evaluate the first satisfied ``xsl:when`` or ``xsl:otherwise``."""
    branch = choose_branch(invocation, plan, context)
    if branch is None:
        return ()
    return invocation._execute_plans(branch, context=context)


def execute_map(
    plan: _MapPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Construct one ``xsl:map`` from its already merged entries."""
    values = invocation._execute_plans(plan.children, context=context)
    return (construct_map(values),)


def execute_map_entry(
    plan: _MapEntryPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Construct one ``xsl:map-entry`` key and value."""
    key = invocation._evaluate(plan.key, context)
    if plan.expression is not None:
        value = invocation._evaluate(plan.expression, context)
    else:
        value = invocation._execute_plans(
            plan.children,
            context=replace(context, final_output=False),
        )
    return (construct_map_entry(key, value),)


def execute_where_populated(
    plan: _WherePopulatedPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Evaluate ``xsl:where-populated`` content and drop deemed-empty items."""
    values = invocation._execute_plans(plan.children, context=context)
    return tuple(item for item in values if not is_deemed_empty(item))


def execute_concat(
    plan: _ConcatPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Evaluate every ``xsl:fork``-style concatenated part in order."""
    result: list[XdmItem] = []
    for part in plan.parts:
        result.extend(invocation._execute_plans(part, context=context))
    return tuple(result)
