"""Construction instructions and sequence-constructor results.

Every construction instruction returns an XDM sequence.  Element and document
instructions build typed intermediate trees directly through the builder; no
instruction serializes a node or reparses it to cross an internal boundary.
"""

from __future__ import annotations

import pytest

from pyxsd.xdm import XdmArray, XdmAtomic, XdmItem, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmAnnotation, XdmDocument, XdmNode
from pyxsd.xslt.compiler import instructions as compiled
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.builder import ResultBuilder
from pyxsd.xslt.runtime.instructions import (
    AttributeInstruction,
    CommentInstruction,
    CopyInstruction,
    DocumentInstruction,
    ElementInstruction,
    LiteralResultInstruction,
    NamespaceInstruction,
    ProcessingInstruction,
    SequenceInstruction,
    TemporaryTreeInstruction,
    TextInstruction,
    construct_instruction,
    construct_into,
    construct_sequence,
)

UNTYPED = "{http://www.w3.org/2001/XMLSchema}untyped"
UNTYPED_ATOMIC = "{http://www.w3.org/2001/XMLSchema}untypedAtomic"


def _code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


def _xml(xml: bytes) -> XdmDocument:
    return XdmDocument.parse(xml)


def _require_element(item: XdmItem) -> XdmNode:
    assert isinstance(item, XdmNode)
    return item


# ---------------------------------------------------------------------------
# Sequence constructors


def test_construct_sequence_returns_an_xdm_sequence() -> None:
    sequence = construct_sequence(
        (
            TextInstruction("text"),
            CommentInstruction("comment"),
            ProcessingInstruction("pi", "value"),
            AttributeInstruction("id", "1"),
            NamespaceInstruction("p", "urn:p"),
        )
    )

    assert isinstance(sequence, tuple)
    assert all(isinstance(item, XdmItem) for item in sequence)
    assert [item.kind for item in sequence] == [
        NodeKind.TEXT,
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
        NodeKind.ATTRIBUTE,
        NodeKind.NAMESPACE,
    ]
    assert sequence[0].string_value == "text"
    assert sequence[1].string_value == "comment"
    assert sequence[2].target == "pi"
    assert sequence[3].name == "id"
    assert sequence[4].namespace_binding == "urn:p"


def test_empty_sequence_constructor_returns_an_empty_sequence() -> None:
    assert construct_sequence(()) == ()
    assert construct_instruction(SequenceInstruction(())) == ()


def test_sequence_instruction_preserves_raw_items() -> None:
    raw = (
        XdmAtomic.integer(1),
        XdmAtomic.string("two"),
        XdmMap.from_mapping({"k": 1}),
        XdmArray([1, 2]),
    )
    assert construct_sequence((SequenceInstruction(raw),)) == raw


def test_sequence_instruction_rejects_non_xdm_members() -> None:
    with pytest.raises(TypeError, match="XDM"):
        SequenceInstruction((object(),))  # type: ignore[arg-type]


def test_element_instruction_constructs_attributes_namespaces_and_content() -> None:
    instruction = ElementInstruction(
        "{urn:out}root",
        attributes=(("id", "1"),),
        namespaces={"p": "urn:p"},
        base_uri="http://example.com/base/",
        content=(
            AttributeInstruction("extra", "2", prefix=""),
            TextInstruction("value"),
            CommentInstruction("comment"),
        ),
    )

    sequence = construct_instruction(instruction)

    assert len(sequence) == 1
    element = _require_element(sequence[0])
    assert element.kind is NodeKind.ELEMENT
    assert element.name == "{urn:out}root"
    assert element.base_uri == "http://example.com/base/"
    assert element.annotation.type_name == UNTYPED
    assert [attribute.name for attribute in element.attributes] == ["id", "extra"]
    assert element.in_scope_namespaces["p"] == "urn:p"
    assert [child.kind for child in element.children] == [
        NodeKind.TEXT,
        NodeKind.COMMENT,
    ]
    assert element.children[0].string_value == "value"


def test_element_instruction_content_atomics_use_a_single_space() -> None:
    instruction = ElementInstruction(
        "root",
        content=(SequenceInstruction((XdmAtomic.integer(1), XdmAtomic.integer(2))),),
    )
    element = _require_element(construct_instruction(instruction)[0])
    assert element.children[0].string_value == "1 2"


def test_element_instruction_nested_elements() -> None:
    instruction = ElementInstruction(
        "root",
        content=(ElementInstruction("child", content=(TextInstruction("value"),)),),
    )
    element = _require_element(construct_instruction(instruction)[0])
    child = _require_element(element.children[0])
    assert child.name == "child"
    assert child.parent is element
    assert child.children[0].string_value == "value"


def test_element_instruction_applies_explicit_annotation() -> None:
    annotation = XdmAnnotation("urn:typed", False)
    instruction = ElementInstruction("root", annotation=annotation)
    element = _require_element(construct_instruction(instruction)[0])
    assert element.annotation is annotation


def test_document_instruction_wraps_content_in_a_document() -> None:
    instruction = DocumentInstruction(
        content=(
            TextInstruction("head"),
            ElementInstruction("root", content=(TextInstruction("body"),)),
            CommentInstruction("tail"),
        ),
        document_uri="http://example.com/out.xml",
        base_uri="http://example.com/base/",
    )

    sequence = construct_instruction(instruction)

    assert len(sequence) == 1
    document = sequence[0]
    assert isinstance(document, XdmDocument)
    assert document.document_uri == "http://example.com/out.xml"
    assert document.base_uri == "http://example.com/base/"
    assert [child.kind for child in document.children] == [
        NodeKind.TEXT,
        NodeKind.ELEMENT,
        NodeKind.COMMENT,
    ]
    assert document.root_element.name == "root"
    assert document.root_element.children[0].string_value == "body"


def test_document_instruction_can_be_final_output() -> None:
    instruction = DocumentInstruction(
        content=(ElementInstruction("root"),),
        final_output=True,
    )
    assert instruction.final_output is True
    document = construct_instruction(instruction)[0]
    assert isinstance(document, XdmDocument)


def test_temporary_tree_instruction_marks_temporary_output() -> None:
    instruction = TemporaryTreeInstruction(
        content=(ElementInstruction("root"),),
        base_uri="http://example.com/temp",
    )
    assert instruction.final_output is False
    document = construct_instruction(instruction)[0]
    assert isinstance(document, XdmDocument)
    assert document.base_uri == "http://example.com/temp"
    assert document.root_element.name == "root"


def test_construction_rolls_back_a_failed_document_sequence() -> None:
    instruction = DocumentInstruction(
        content=(
            TextInstruction("discarded"),
            AttributeInstruction("late", "attribute"),
        )
    )
    with pytest.raises(XsltDynamicError) as error:
        construct_instruction(instruction)
    assert _code(error.value) == "XTDE0420"


def test_element_instruction_rejects_attribute_after_content() -> None:
    instruction = ElementInstruction(
        "root",
        content=(TextInstruction("content"), AttributeInstruction("late", "value")),
    )
    with pytest.raises(XsltDynamicError) as error:
        construct_instruction(instruction)
    assert _code(error.value) == "XTDE0410"


def test_document_instruction_rejects_function_items() -> None:
    instruction = DocumentInstruction(
        content=(SequenceInstruction((XdmMap.from_mapping({"k": 1}),)),)
    )
    with pytest.raises(XsltDynamicError) as error:
        construct_instruction(instruction)
    assert _code(error.value) == "XTDE0450"


# ---------------------------------------------------------------------------
# Literal result elements


def test_literal_result_uses_retained_static_context() -> None:
    source = compiled.Instruction(
        name="{urn:out}root",
        attributes=(compiled.Attribute("{urn:q}attr", "value"),),
        namespaces={"p": "urn:out", "q": "urn:q"},
        base_uri="http://example.com/style.xsl",
        literal=True,
    )
    instruction = LiteralResultInstruction.from_instruction(
        source,
        content=(TextInstruction("body"),),
    )

    element = _require_element(construct_instruction(instruction)[0])

    assert element.name == "{urn:out}root"
    assert element.prefix == "p"
    assert element.base_uri == "http://example.com/style.xsl"
    assert element.attributes[0].name == "{urn:q}attr"
    assert element.attributes[0].prefix == "q"
    assert element.in_scope_namespaces["p"] == "urn:out"
    assert element.in_scope_namespaces["q"] == "urn:q"
    assert element.children[0].string_value == "body"


def test_literal_result_applies_namespace_aliases() -> None:
    source = compiled.Instruction(
        name="{urn:old}root",
        namespaces={"old": "urn:old"},
        base_uri="http://example.com/base/",
        literal=True,
    )
    instruction = LiteralResultInstruction.from_instruction(
        source,
        namespace_aliases={"old": "new"},
    )
    element = _require_element(construct_instruction(instruction)[0])
    assert element.prefix == "new"
    assert element.in_scope_namespaces["new"] == "urn:old"


def test_literal_result_direct_construction_without_static_context() -> None:
    instruction = LiteralResultInstruction(
        "root",
        attributes=(("id", "1"),),
        content=(TextInstruction("value"),),
    )
    element = _require_element(construct_instruction(instruction)[0])
    assert element.name == "root"
    assert element.base_uri is None
    assert element.attributes[0].string_value == "1"
    assert element.children[0].string_value == "value"


# ---------------------------------------------------------------------------
# Copies


def test_copy_instruction_deep_and_shallow() -> None:
    source = _xml(b"<root a='1'><child>text</child><!--comment--></root>").root_element
    source._apply_occurrence_annotation(XdmAnnotation("urn:type", False))

    deep = _require_element(construct_instruction(CopyInstruction(source))[0])
    shallow = _require_element(construct_instruction(CopyInstruction(source, deep=False))[0])

    assert deep.annotation is source.annotation
    assert [child.kind for child in deep.children] == [
        NodeKind.ELEMENT,
        NodeKind.COMMENT,
    ]
    assert deep.children[0].string_value == "text"
    assert shallow.annotation is source.annotation
    assert len(shallow.attributes) == 1
    assert shallow.children == ()


def test_copy_instruction_without_namespaces() -> None:
    source = _xml(
        b"<p:root xmlns:p='urn:p' xmlns:unused='urn:unused'><p:child/></p:root>"
    ).root_element
    copied = _require_element(
        construct_instruction(CopyInstruction(source, copy_namespaces=False))[0]
    )
    in_scope = {node.prefix: node.string_value for node in copied.namespace_nodes}
    assert in_scope["p"] == "urn:p"
    assert "unused" not in in_scope


def test_copy_instruction_of_document_and_simple_nodes() -> None:
    document = _xml(b"<root><!--comment--><?pi value?></root>")
    copied_document = construct_instruction(CopyInstruction(document))[0]
    assert isinstance(copied_document, XdmDocument)
    assert copied_document.root_element.name == "root"
    assert [child.kind for child in copied_document.root_element.children] == [
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
    ]

    instruction_node = document.root_element.children[1]
    copied_instruction = _require_element(
        construct_instruction(CopyInstruction(instruction_node))[0]
    )
    assert copied_instruction.kind is NodeKind.PROCESSING_INSTRUCTION
    assert copied_instruction.target == "pi"


def test_copy_attribute_instruction_is_parentless() -> None:
    attribute = _xml(b"<root a='1'/>").root_element.attributes[0]
    copied = _require_element(construct_instruction(CopyInstruction(attribute))[0])
    assert copied.kind is NodeKind.ATTRIBUTE
    assert copied.parent is None
    assert copied.string_value == "1"


# ---------------------------------------------------------------------------
# Instruction execution into a builder


def test_construct_into_returns_the_result_sequence() -> None:
    builder = ResultBuilder.document()
    sequence = construct_into(
        builder,
        (TextInstruction("head"), ElementInstruction("root")),
    )
    assert sequence == builder.to_sequence()
    assert [item.kind for item in sequence] == [NodeKind.TEXT, NodeKind.ELEMENT]


def test_construct_into_rolls_back_failed_construction() -> None:
    builder = ResultBuilder.document()
    with pytest.raises(XsltDynamicError) as error:
        construct_into(
            builder,
            (TextInstruction("discarded"), AttributeInstruction("late", "value")),
        )
    assert _code(error.value) == "XTDE0420"
    assert builder.to_sequence() == ()

    construct_into(builder, (TextInstruction("kept"),))
    assert builder.to_sequence()[0].string_value == "kept"


def test_construct_into_flattens_document_results() -> None:
    builder = ResultBuilder.document()
    construct_into(
        builder,
        (
            DocumentInstruction(content=(ElementInstruction("first"),)),
            DocumentInstruction(content=(ElementInstruction("second"),)),
        ),
    )
    assert [child.name for child in builder.to_sequence()] == ["first", "second"]


def test_construct_into_keeps_temporary_output_state() -> None:
    builder = ResultBuilder.temporary()
    construct_into(builder, (TextInstruction("temp"),))
    assert builder.final_output is False
    with pytest.raises(XsltDynamicError):
        construct_into(
            builder,
            (TextInstruction("discarded"), AttributeInstruction("late", "value")),
        )
    assert builder.to_sequence()[0].string_value == "temp"


# ---------------------------------------------------------------------------
# Simple-content construction validation


def test_attribute_instruction_validates_its_name() -> None:
    with pytest.raises(XsltDynamicError) as error:
        construct_instruction(AttributeInstruction("xmlns", "urn:x"))
    assert _code(error.value) == "XTDE0855"
    with pytest.raises(XsltDynamicError) as error:
        construct_instruction(AttributeInstruction("", "value"))
    assert _code(error.value) == "XTDE0850"


def test_namespace_instruction_validates_its_binding() -> None:
    with pytest.raises(XsltDynamicError) as error:
        construct_instruction(NamespaceInstruction("p", ""))
    assert _code(error.value) == "XTDE0930"
    with pytest.raises(XsltDynamicError) as error:
        construct_instruction(NamespaceInstruction("1bad", "urn:p"))
    assert _code(error.value) == "XTDE0920"


def test_comment_and_processing_instruction_sanitize_content() -> None:
    comment = _require_element(construct_instruction(CommentInstruction("a--b-"))[0])
    instruction = _require_element(
        construct_instruction(ProcessingInstruction("pi", "  value?>"))[0]
    )
    assert comment.string_value == "a- -b- "
    assert instruction.string_value == "value? >"


def test_text_instruction_keeps_annotations_untyped_atomic() -> None:
    text = _require_element(construct_instruction(TextInstruction("value"))[0])
    assert text.annotation.type_name == UNTYPED_ATOMIC


# ---------------------------------------------------------------------------
# Instruction data validation and static-context overrides


def test_instruction_dataclasses_validate_their_inputs() -> None:
    with pytest.raises(TypeError, match="text instruction"):
        TextInstruction(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="attribute instruction"):
        AttributeInstruction(1, "value")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="attribute instruction"):
        AttributeInstruction("name", 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="prefix"):
        AttributeInstruction("name", "value", prefix=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="namespace instruction"):
        NamespaceInstruction(1, "urn:p")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="namespace instruction"):
        NamespaceInstruction("p", 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="comment instruction"):
        CommentInstruction(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="processing instruction"):
        ProcessingInstruction(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="processing instruction"):
        ProcessingInstruction("pi", 1)  # type: ignore[arg-type]

    node = _xml(b"<root/>").root_element
    with pytest.raises(TypeError, match="XDM node"):
        CopyInstruction(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="booleans"):
        CopyInstruction(node, deep=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="booleans"):
        CopyInstruction(node, copy_namespaces=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="final_output"):
        DocumentInstruction(final_output=1)  # type: ignore[arg-type]


def test_unknown_construction_instruction_is_rejected() -> None:
    with pytest.raises(TypeError, match="unknown construction instruction"):
        construct_instruction(object())  # type: ignore[arg-type]


def test_literal_result_from_instruction_accepts_explicit_evaluated_attributes() -> None:
    source = compiled.Instruction(
        name="root",
        attributes=(compiled.Attribute("id", "{unevaluated}"),),
        namespaces={},
        literal=True,
    )
    instruction = LiteralResultInstruction.from_instruction(
        source,
        attributes=(("id", "evaluated"),),
    )
    element = _require_element(construct_instruction(instruction)[0])
    assert element.attributes[0].string_value == "evaluated"
