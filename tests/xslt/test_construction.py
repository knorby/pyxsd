"""Construction instructions and sequence-constructor results.

Every construction instruction returns an XDM sequence.  Element and document
instructions build typed intermediate trees directly through the builder; no
instruction serializes a node or reparses it to cross an internal boundary.
"""

from __future__ import annotations

import pytest
from tests.xslt.support import stylesheet as _stylesheet
from tests.xslt.support import transform as _transform

from pyxsd.xdm import XdmArray, XdmAtomic, XdmItem, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmAnnotation, XdmDocument, XdmNode
from pyxsd.xslt.compiler import instructions as compiled
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.builder import (
    NamespaceAlias,
    ResultBuilder,
    _bind_prefix,
    _element_prefix,
    _prepare_scope,
)
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
XML_NS = "http://www.w3.org/XML/1998/namespace"


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


def test_prepared_scope_separates_copied_namespaces_from_available_bindings() -> None:
    scope = _prepare_scope(
        {"p": "urn:p", "q": "urn:p", "old": "urn:old", "new": "urn:new"},
        excluded=frozenset({"urn:p"}),
        aliases=(NamespaceAlias("urn:old", "urn:new", "new"),),
        element_namespace="urn:old",
    )

    assert scope.element_namespace == "urn:new"
    assert dict(scope.copied_namespaces) == {"new": "urn:new"}
    # Excluded and aliased-literal bindings remain available to name and fix
    # up constructed nodes even though neither is copied.
    assert dict(scope.available_bindings) == {
        "p": "urn:p",
        "q": "urn:p",
        "old": "urn:old",
        "new": "urn:new",
        "xml": XML_NS,
    }


def test_prepared_scope_ignores_a_nonmatching_alias_rule() -> None:
    scope = _prepare_scope(
        {"old": "urn:old", "new": "urn:new"},
        aliases=(
            NamespaceAlias("urn:other", "urn:target", "target"),
            NamespaceAlias("urn:old", "urn:new", "new"),
        ),
        element_namespace="urn:old",
    )

    assert scope.element_namespace == "urn:new"


def test_element_prefix_selection_uses_an_available_binding() -> None:
    assert _element_prefix("urn:x", {}, {"p": "urn:x"}, requested=None) == "p"
    assert _element_prefix(XML_NS, {}, requested=None) == "xml"


def test_bind_prefix_falls_back_when_the_preferred_prefix_is_taken() -> None:
    scope = {"p": "urn:other"}

    assert _bind_prefix(scope, "urn:new", "p", is_attribute=True) == "ns1"
    assert scope == {"p": "urn:other", "ns1": "urn:new"}


def test_literal_result_applies_compiled_namespace_alias_rules() -> None:
    source = compiled.Instruction(
        name="{urn:old}root",
        attributes=(compiled.Attribute("{urn:old}id", "1"),),
        namespaces={"old": "urn:old", "new": "urn:new"},
        literal=True,
    )
    instruction = LiteralResultInstruction.from_instruction(
        source,
        namespace_alias_rules=(
            NamespaceAlias("urn:other", "urn:target", "target"),
            NamespaceAlias("urn:old", "urn:new", "new"),
        ),
    )

    element = _require_element(construct_instruction(instruction)[0])

    assert element.name == "{urn:new}root"
    assert element.prefix == "new"
    assert element.attributes[0].name == "{urn:new}id"
    assert element.attributes[0].prefix == "new"
    assert element.namespace_declarations == (("new", "urn:new"),)


def test_literal_result_excludes_only_copied_namespaces() -> None:
    source = compiled.Instruction(
        name="{urn:keep}root",
        namespaces={"keep": "urn:keep", "unused": "urn:unused"},
        literal=True,
    )
    instruction = LiteralResultInstruction.from_instruction(
        source,
        excluded_namespaces=frozenset({"urn:unused"}),
    )

    element = _require_element(construct_instruction(instruction)[0])

    assert element.namespace_uri == "urn:keep"
    assert element.prefix == "keep"
    assert "unused" not in element.in_scope_namespaces
    assert element.in_scope_namespaces["keep"] == "urn:keep"


def test_literal_result_keeps_its_lexical_prefix_over_a_default_binding() -> None:
    source = compiled.Instruction(
        name="{urn:a}item",
        namespaces={"": "urn:a", "p": "urn:a"},
        literal=True,
        prefix="p",
    )
    instruction = LiteralResultInstruction.from_instruction(source)

    element = _require_element(construct_instruction(instruction)[0])

    assert element.name == "{urn:a}item"
    assert element.prefix == "p"
    assert element.namespace_uri == "urn:a"
    assert dict(element.in_scope_namespaces)["p"] == "urn:a"
    assert element.namespace_declarations == (("", "urn:a"), ("p", "urn:a"))


def test_literal_result_prefers_its_lexical_prefix_over_another_binding() -> None:
    source = compiled.Instruction(
        name="{urn:a}item",
        namespaces={"q": "urn:a", "p": "urn:a"},
        literal=True,
        prefix="p",
    )
    instruction = LiteralResultInstruction.from_instruction(source)

    element = _require_element(construct_instruction(instruction)[0])

    assert element.prefix == "p"
    assert element.namespace_uri == "urn:a"
    assert element.namespace_declarations == (("q", "urn:a"), ("p", "urn:a"))


def test_literal_result_without_a_lexical_prefix_uses_a_default_binding() -> None:
    instruction = LiteralResultInstruction(
        name="{urn:a}item",
        namespaces={"": "urn:a", "p": "urn:a"},
    )

    element = _require_element(construct_instruction(instruction)[0])

    assert element.prefix == ""
    assert element.namespace_uri == "urn:a"


def test_bug_0302_literal_result_prefixes_and_declarations_match_the_corpus() -> None:
    source = _stylesheet(
        """
        <xsl:output method="xml" encoding="iso-8859-1"/>
        <xsl:template match="/" xmlns:z="http://z.uri/">
          <xsl:variable name="tmp">
            <z:a a-foo="a-bar"/>
            <z:b b-foo="b-bar"/>
            <c c-foo="c-bar"/>
          </xsl:variable>
          <out changed="2004-04-06">
            <xsl:copy-of select="$tmp"/>
          </out>
        </xsl:template>
        """,
        version="2.0",
        attributes=' exclude-result-prefixes="#default" xmlns="http://abc.uri/"',
        declare_xs=False,
    )
    result = _transform(source, b"<doc/>", delivery="stylesheet")

    out = result.principal_tree.root_element
    assert out.name == "{http://abc.uri/}out"
    assert out.prefix == ""
    assert out.namespace_uri == "http://abc.uri/"
    assert out.namespace_declarations == (
        ("", "http://abc.uri/"),
        ("z", "http://z.uri/"),
    )
    children = [child for child in out.children if child.kind is NodeKind.ELEMENT]
    assert [(child.name, child.prefix) for child in children] == [
        ("{http://z.uri/}a", "z"),
        ("{http://z.uri/}b", "z"),
        ("{http://abc.uri/}c", ""),
    ]
    assert result.principal_entry.serialized == (
        '<?xml version="1.0" encoding="iso-8859-1"?>'
        '<out xmlns="http://abc.uri/" xmlns:z="http://z.uri/" changed="2004-04-06">'
        '<z:a a-foo="a-bar"/><z:b b-foo="b-bar"/><c c-foo="c-bar"/></out>'
    )


def test_nested_default_rebinding_and_undeclaration_keep_element_prefixes() -> None:
    result = _transform(
        _stylesheet(
            """
            <xsl:template name="main">
              <a xmlns="urn:1"><b xmlns="urn:2"><c xmlns=""/></b></a>
            </xsl:template>
            """,
            declare_xs=False,
        ),
        None,
        delivery="stylesheet",
        initial_template="main",
    )

    a = result.principal_tree.root_element
    b = a.children[0]
    c = b.children[0]
    assert [(node.prefix, node.namespace_uri) for node in (a, b, c)] == [
        ("", "urn:1"),
        ("", "urn:2"),
        ("", None),
    ]
    assert a.namespace_declarations == (("", "urn:1"),)
    assert b.namespace_declarations == (("", "urn:2"),)
    assert c.namespace_declarations == ()
    assert "" not in c.in_scope_namespaces
    assert result.principal_entry.serialized == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<a xmlns="urn:1"><b xmlns="urn:2"><c xmlns=""/></b></a>'
    )


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


def test_construct_into_returns_the_appended_sequence() -> None:
    builder = ResultBuilder.document()
    sequence = construct_into(
        builder,
        (TextInstruction("head"), ElementInstruction("root")),
    )
    assert [item.kind for item in sequence] == [NodeKind.TEXT, NodeKind.ELEMENT]
    assert [item.kind for item in builder.to_sequence()] == [
        NodeKind.TEXT,
        NodeKind.ELEMENT,
    ]
    assert builder.to_sequence()[1].name == sequence[1].name == "root"


def test_construct_into_returns_only_the_new_sequence_from_a_nonempty_builder() -> None:
    builder = ResultBuilder.document()
    builder.append((XdmAtomic.string("existing"),))
    appended = construct_into(builder, (TextInstruction("new"),))
    assert len(appended) == 1
    assert appended[0].string_value == "new"
    assert builder.to_sequence()[0].string_value == "existingnew"


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


def test_builder_element_generates_a_prefix_only_when_no_binding_exists() -> None:
    builder = ResultBuilder.temporary()

    element = builder.element("{urn:x}item", namespaces={"": "urn:other"})

    assert element.prefix == "ns1"
    assert element.namespace_uri == "urn:x"
    assert element.namespace_declarations == (("", "urn:other"), ("ns1", "urn:x"))
    assert dict(element.in_scope_namespaces)["ns1"] == "urn:x"


def test_builder_element_falls_back_when_the_requested_prefix_is_unbound() -> None:
    builder = ResultBuilder.temporary()

    element = builder.element("{urn:a}item", namespaces={"p": "urn:a"}, prefix="missing")

    assert element.prefix == "p"
    assert element.namespace_uri == "urn:a"


def test_builder_unprefixed_attribute_never_uses_the_default_namespace() -> None:
    builder = ResultBuilder.temporary()

    element = builder.element(
        "{urn:a}out",
        attributes=(("id", "1"),),
        namespaces={"": "urn:a"},
    )

    attribute = element.attributes[0]
    assert attribute.name == "id"
    assert attribute.prefix == ""
    assert attribute.namespace_uri is None


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


# ---------------------------------------------------------------------------
# Delivered element-rooted temporary trees
#
# The instruction-level home: these tests cross the actual expression boundary
# with ``delivery="raw"``, so they observe the trees a variable binding
# delivers, not a builder's internal state.


def _delivered_trees(body: str) -> tuple[XdmNode, ...]:
    result = _transform(
        _stylesheet(body),
        None,
        delivery="raw",
        initial_template="main",
    )
    return tuple(item for item in result.principal if isinstance(item, XdmNode))


def test_a_delivered_element_rooted_sequence_is_finalized_per_tree() -> None:
    trees = _delivered_trees(
        """
        <xsl:template name="main">
          <xsl:variable name="x" as="element()*">
            <a><x/></a><b><x/></b><c><x/></c>
          </xsl:variable>
          <xsl:sequence select="$x"/>
        </xsl:template>"""
    )
    assert [tree.name for tree in trees] == ["a", "b", "c"]
    assert all(tree.parent is None for tree in trees)
    assert all(tree.document.children == () for tree in trees)
    assert len({tree.document for tree in trees}) == 3
    positions = [[node.document_order for node in (tree, *tree.descendants)] for tree in trees]
    assert all(order >= 0 for tree_positions in positions for order in tree_positions)
    assert all(len(set(tree_positions)) == len(tree_positions) for tree_positions in positions)
    # Construction (sequence) order gives each tree its own increasing range.
    assert max(positions[0]) < min(positions[1])
    assert max(positions[1]) < min(positions[2])


def test_two_temporary_tree_sequences_never_share_positions_or_identity() -> None:
    trees = _delivered_trees(
        """
        <xsl:template name="main">
          <xsl:variable name="x" as="element()*"><a><x/></a><b><x/></b></xsl:variable>
          <xsl:variable name="y" as="element()*"><c><y/></c><d><y/></d></xsl:variable>
          <xsl:sequence select="$x, $y"/>
        </xsl:template>"""
    )
    assert [tree.name for tree in trees] == ["a", "b", "c", "d"]
    documents = [tree.document for tree in trees]
    assert len({id(document) for document in documents}) == 4
    positions = [node.document_order for tree in trees for node in (tree, *tree.descendants)]
    assert len(set(positions)) == len(positions)
    assert positions == sorted(positions)


def test_repeated_construction_keeps_union_and_axis_order_stable() -> None:
    result = _transform(
        _stylesheet(
            """
            <xsl:template name="main">
              <out>
                <xsl:for-each select="1 to 200">
                  <xsl:variable name="x" as="element()*">
                    <a><v/></a><b><v/></b><c><v/></c>
                  </xsl:variable>
                  <xsl:for-each select="$x | $x//node()">
                    <xsl:value-of select="name()"/><xsl:text> </xsl:text>
                  </xsl:for-each>
                  <xsl:text>|</xsl:text>
                </xsl:for-each>
              </out>
            </xsl:template>"""
        ),
        None,
        initial_template="main",
    )
    chunks = result.principal_tree.string_value.split("|")[:-1]
    assert len(chunks) == 200
    assert set(chunks) == {"a v b v c v "}


def test_preceding_axes_in_delivered_temporary_trees_are_tree_local() -> None:
    result = _transform(
        _stylesheet(
            """
            <xsl:template name="main">
              <xsl:variable name="x" as="element()*">
                <a><v/><v/></a><b><v/></b><c><v/></c>
              </xsl:variable>
              <out>
                <xsl:for-each select="$x//v">
                  <xsl:value-of
                      select="concat(count(preceding::*), ':', count(preceding-sibling::*), ';')"/>
                </xsl:for-each>
              </out>
            </xsl:template>"""
        ),
        None,
        initial_template="main",
    )
    # a's first v has no preceding element (a is an ancestor); the second v has
    # the first v preceding it and as its preceding sibling.  b's and c's v
    # belong to different trees, so nothing from a precedes them.
    assert result.principal_tree.string_value == "0:0;1:1;0:0;0:0;"


def test_a_copy_instruction_delivers_a_finalized_element_tree() -> None:
    source = _xml(b"<root a='1'><child/></root>").root_element
    copied = _require_element(construct_instruction(CopyInstruction(source))[0])
    assert copied.parent is None
    assert copied.document.document_order == 0
    assert all(node.document_order >= 0 for node in (copied, *copied.descendants))
