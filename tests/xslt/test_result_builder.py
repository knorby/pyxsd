"""Transactional result-tree construction and sequence normalization.

The builder owns every legal state transition: element nesting, attributes and
namespaces before content, duplicate attribute replacement, namespace fixup,
text merging, and node copying.  Construction is incremental and in-memory;
these tests never serialize a tree or reparse it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pyxsd.xdm import XdmArray, XdmAtomic, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmAnnotation, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.builder import (
    ResultBuilder,
    _attribute_prefix,
    _new_prefix,
    _NodeFactory,
    _prefix_for,
    build_result_tree,
)
from pyxsd.xslt.runtime.frames import Continue, Frame, Return, Scheduler

XML_NS = "http://www.w3.org/XML/1998/namespace"
XMLNS_NS = "http://www.w3.org/2000/xmlns/"
UNTYPED = "{http://www.w3.org/2001/XMLSchema}untyped"
UNTYPED_ATOMIC = "{http://www.w3.org/2001/XMLSchema}untypedAtomic"


def _code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


def _xml(xml: bytes) -> XdmDocument:
    return XdmDocument.parse(xml)


def _element(builder: ResultBuilder, name: str = "root") -> XdmNode:
    return builder.element(name)


def _string(builder: ResultBuilder) -> str:
    return "".join(item.string_value for item in builder.to_sequence() if isinstance(item, XdmNode))


# ---------------------------------------------------------------------------
# Transaction extents


def test_builder_transitions_and_rollback() -> None:
    builder = ResultBuilder.document()
    with builder.transaction() as extent:
        builder.element("{urn:test}root")
        builder.attribute("id", "a")
        builder.text("hello")
        extent.rollback()
    assert builder.to_sequence() == ()
    with pytest.raises(XsltDynamicError, match="XTDE0410"):
        builder.attribute("late", "not-allowed")


def test_builder_commits_successful_transactions() -> None:
    builder = ResultBuilder.document(base_uri="http://example.com/out")
    with builder.transaction() as extent:
        root = builder.element("root")
        builder.attribute("id", "a")
        builder.text("hello")
    assert builder.to_sequence() == (root,)
    assert root.attributes[0].string_value == "a"
    assert _string(builder) == "hello"
    assert extent.committed is True
    extent.rollback()
    assert builder.to_sequence() == (root,)
    assert _string(builder) == "hello"
    assert builder.to_tree().base_uri == "http://example.com/out"


def test_rollback_restores_the_open_element_stack() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root")
    with builder.transaction() as extent:
        builder.end_element()
        assert builder.current is builder.root
        extent.rollback()
    assert builder.current is root
    attribute = builder.attribute("id", "1")
    assert attribute.parent is root
    builder.end_element()
    assert builder.to_sequence() == (root,)


def test_rollback_restores_nested_open_elements_and_current_parent() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root")
    child = builder.element("child")
    with builder.transaction() as extent:
        builder.end_element()
        builder.end_element()
        extent.rollback()
    assert builder.current is child
    assert child.parent is root
    builder.attribute("id", "1")
    assert child.attributes[0].string_value == "1"


def test_nested_transactions_restore_text_and_structure() -> None:
    builder = ResultBuilder.document()
    builder.text("base")
    with builder.transaction() as outer:
        builder.text("outer")
        with builder.transaction() as inner:
            builder.text("inner")
            builder.element("child")
            inner.rollback()
        assert _string(builder) == "baseouter"
        outer.rollback()
    assert _string(builder) == "base"
    assert builder.to_sequence()[0].string_value == "base"
    assert [child.kind for child in builder.root.children] == [NodeKind.TEXT]


def test_rollback_restores_root_element_and_identity_state() -> None:
    builder = ResultBuilder.document()
    with builder.transaction() as extent:
        builder.element("first")
        builder.end_element()
        extent.rollback()
    assert builder.to_sequence() == ()
    second = builder.element("second")
    builder.end_element()
    tree = builder.to_tree()
    assert tree.root_element is second
    assert second.node_id > 0
    assert second.document_order >= 0


def test_rollback_is_idempotent_and_commit_after_rollback_does_nothing() -> None:
    builder = ResultBuilder.document()
    extent = builder.transaction()
    builder.text("discarded")
    extent.rollback()
    extent.rollback()
    extent.commit()
    assert builder.to_sequence() == ()
    assert extent.committed is False
    assert extent.rolled_back is True


def test_exception_inside_transaction_rolls_back() -> None:
    builder = ResultBuilder.document()
    with pytest.raises(RuntimeError), builder.transaction():
        builder.text("gone")
        raise RuntimeError("failure")
    assert builder.to_sequence() == ()


def test_transaction_extent_integrates_with_scheduler_rollback() -> None:
    builder = ResultBuilder.document()
    extent = builder.transaction()

    def failing(frame: Frame) -> Continue:
        del frame
        builder.text("discarded")
        raise XsltDynamicError("construction failed")

    scheduler = Scheduler()
    with pytest.raises(XsltDynamicError, match="construction failed"):
        scheduler.run(Frame(kind="test", extent=extent), failing)
    assert builder.to_sequence() == ()

    def succeeding(frame: Frame) -> Return:
        del frame
        builder.text("kept")
        return Return("done")

    result = scheduler.run(Frame(kind="test", extent=builder.transaction()), succeeding)
    assert result == "done"
    assert _string(builder) == "kept"


def test_transaction_snapshot_restores_namespaces_and_attributes() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root", namespaces={"p": "urn:p"})
    builder.attribute("id", "1")
    with builder.transaction() as extent:
        builder.namespace("q", "urn:q")
        builder.attribute("extra", "2")
        extent.rollback()
    assert "q" not in root.in_scope_namespaces
    assert [attribute.name for attribute in root.attributes] == ["id"]
    assert all(node.prefix != "q" for node in root.namespace_nodes)


def test_rollback_restores_replaced_attributes_and_renamed_namespaces() -> None:
    source = _xml(b"<s xmlns:p='urn:new'/>")
    conflicting = source.root_element.namespace_node("p")
    assert conflicting is not None

    builder = ResultBuilder.document()
    root = builder.element("{urn:old}root", namespaces={"p": "urn:old"})
    builder.attribute("id", "original")
    with builder.transaction() as extent:
        builder.attribute("id", "replacement")
        builder.copy(conflicting)
        assert root.prefix == "ns1"
        extent.rollback()
    restored = root.attribute("id")
    assert restored is not None and restored.string_value == "original"
    assert root.prefix == "p"
    namespace = root.namespace_node("p")
    assert namespace is not None and namespace.namespace_binding == "urn:old"


# ---------------------------------------------------------------------------
# Element state and illegal ordering


def test_document_builder_creates_a_final_tree_directly() -> None:
    builder = ResultBuilder.document(
        document_uri="http://example.com/result.xml",
        base_uri="http://example.com/base/",
    )
    root = builder.element("root")
    builder.attribute("id", "1")
    builder.text("value")
    builder.end_element()
    tree = builder.to_tree()

    assert tree.document_uri == "http://example.com/result.xml"
    assert tree.base_uri == "http://example.com/base/"
    assert tree.root_element is root
    assert builder.to_sequence() == (root,)


def test_imperative_construction_sets_parent_links_and_root() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root")
    child = builder.element("child")
    text = builder.text("value")
    assert text is not None

    assert root.parent is builder.root
    assert child.parent is root
    assert text.parent is child
    assert child.ancestors == (root, builder.root)
    assert text.root is builder.root
    assert builder.root.root_element is root
    builder.end_element()
    builder.end_element()
    assert builder.to_tree().root_element is root


def test_attribute_namespace_comment_and_pi_have_parent_links() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root")
    attribute = builder.attribute("id", "1")
    namespace = builder.namespace("p", "urn:p")
    comment = builder.comment("comment")
    instruction = builder.processing_instruction("pi", "value")
    for node in (attribute, namespace, comment, instruction):
        assert node.parent is root
        assert node.ancestors == (root, builder.root)
        assert node.root is builder.root


def test_constructed_elements_have_the_implicit_xml_namespace_node() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root")
    child = builder.element("child", inherit_namespaces=False)
    for element in (root, child):
        namespace = element.namespace_node("xml")
        assert namespace is not None
        assert namespace.name == "xml"
        assert namespace.prefix == "xml"
        assert namespace.namespace_binding == XML_NS


def test_constructed_descendants_inherit_parent_namespaces() -> None:
    builder = ResultBuilder.document()
    root = builder.element("{urn:root}root", namespaces={"p": "urn:p", "": "urn:root"})
    child = builder.element("child")
    grandchild = builder.element("{urn:grand}grand")
    builder.end_element()
    overridden = builder.element("overridden", namespaces={"p": "urn:other"})
    builder.end_element()
    isolated = builder.element("isolated", inherit_namespaces=False)
    builder.end_element()
    builder.end_element()

    assert child.parent is root
    assert child.in_scope_namespaces["p"] == "urn:p"
    assert "" not in child.in_scope_namespaces
    assert child.namespace_node("p") is not None
    assert grandchild.parent is child
    assert grandchild.in_scope_namespaces["p"] == "urn:p"
    assert grandchild.in_scope_namespaces[""] == "urn:grand"
    assert overridden.in_scope_namespaces["p"] == "urn:other"
    assert "p" not in isolated.in_scope_namespaces

    detached_builder = ResultBuilder.temporary()
    detached = detached_builder.element("detached")
    detached_builder.end_element()
    builder.append((detached,))
    appended = root.children[-1]
    assert appended.name == "detached"
    assert appended.parent is root
    assert appended.in_scope_namespaces["p"] == "urn:p"
    assert appended.namespace_node("p") is not None

    no_namespace_builder = ResultBuilder.temporary()
    no_namespace = no_namespace_builder.element("plain")
    no_namespace_builder.end_element()
    builder.append((no_namespace,))
    plain = root.children[-1]
    assert plain.name == "plain"
    assert "" not in plain.in_scope_namespaces
    assert plain.in_scope_namespaces["p"] == "urn:p"


def test_copied_namespaced_attribute_receives_namespace_fixup() -> None:
    source = _xml(b"<s xmlns:p='urn:p' p:a='1'/>").root_element.attributes[0]
    builder = ResultBuilder.document()
    root = builder.element("root")
    copied = builder.copy(source)

    assert copied.parent is root
    assert copied.prefix == "p"
    assert copied.namespace_uri == "urn:p"
    assert root.in_scope_namespaces["p"] == "urn:p"
    assert root.namespace_node("p") is not None
    assert copied.annotation is source.annotation


def test_temporary_builder_preserves_temporary_output_state() -> None:
    builder = ResultBuilder.temporary(base_uri="http://example.com/temp")
    assert builder.final_output is False
    with builder.transaction():
        root = builder.element("temp")
        builder.text("value")
        builder.end_element()
    assert builder.final_output is False
    assert builder.to_sequence() == (root,)
    assert builder.to_tree().base_uri == "http://example.com/temp"
    with builder.transaction() as extent:
        builder.element("discarded")
        extent.rollback()
    assert builder.final_output is False
    assert builder.to_sequence() == (root,)


def test_attribute_requires_an_open_element() -> None:
    builder = ResultBuilder.document()
    with pytest.raises(XsltDynamicError) as error:
        builder.attribute("id", "1")
    assert _code(error.value) == "XTDE0410"
    with pytest.raises(XsltDynamicError) as error:
        builder.namespace("p", "urn:p")
    assert _code(error.value) == "XTDE0410"


def test_attribute_after_child_content_is_rejected() -> None:
    builder = ResultBuilder.document()
    _element(builder)
    builder.text("child")
    with pytest.raises(XsltDynamicError) as error:
        builder.attribute("late", "no")
    assert _code(error.value) == "XTDE0410"
    with pytest.raises(XsltDynamicError) as error:
        builder.namespace("p", "urn:p")
    assert _code(error.value) == "XTDE0410"


def test_end_element_without_an_open_element_is_rejected() -> None:
    builder = ResultBuilder.document()
    with pytest.raises(XsltDynamicError, match="no element"):
        builder.end_element()
    builder.element("root")
    assert builder.end_element().name == "root"
    with pytest.raises(XsltDynamicError, match="no element"):
        builder.end_element()


def test_duplicate_attributes_replace_earlier_values() -> None:
    builder = ResultBuilder.document()
    root = _element(builder)
    builder.attribute("id", "first")
    replacement = builder.attribute("id", "second")
    builder.attribute("{urn:other}id", "other")
    assert [attribute.name for attribute in root.attributes] == ["id", "{urn:other}id"]
    assert root.attribute("id") is replacement
    assert replacement.string_value == "second"
    assert root.attributes[1].string_value == "other"


def test_attribute_name_validation() -> None:
    builder = ResultBuilder.document()
    _element(builder)
    with pytest.raises(XsltDynamicError) as error:
        builder.attribute("xmlns", "urn:x")
    assert _code(error.value) == "XTDE0855"
    with pytest.raises(XsltDynamicError) as error:
        builder.attribute("{http://www.w3.org/2000/xmlns/}p", "urn:x")
    assert _code(error.value) == "XTDE0850"
    with pytest.raises(XsltDynamicError) as error:
        builder.attribute("", "value")
    assert _code(error.value) == "XTDE0850"


def test_element_name_validation() -> None:
    builder = ResultBuilder.document()
    with pytest.raises(XsltDynamicError) as error:
        builder.element("")
    assert _code(error.value) == "XTDE0820"
    with pytest.raises(XsltDynamicError) as error:
        builder.element("{urn:x}bad:local")
    assert _code(error.value) == "XTDE0820"


# ---------------------------------------------------------------------------
# Namespace fixup, aliases, and conflicts


def test_namespace_fixup_generates_prefixes_for_namespaced_content() -> None:
    builder = ResultBuilder.document()
    root = builder.element("{urn:root}root", namespaces={"": "urn:root"})
    attribute = builder.attribute("{urn:attr}value", "v")
    assert root.prefix == ""
    assert root.namespace_uri == "urn:root"
    assert attribute.prefix == "ns1"
    assert root.in_scope_namespaces["ns1"] == "urn:attr"
    assert any(node.prefix == "ns1" for node in root.namespace_nodes)


def test_namespace_fixup_reuses_an_existing_nondefault_prefix() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root", namespaces={"p": "urn:p"})
    attribute = builder.attribute("{urn:p}value", "v")
    assert attribute.prefix == "p"
    assert root.in_scope_namespaces["p"] == "urn:p"


def test_namespace_fixup_chooses_xml_for_the_xml_namespace() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root")
    attribute = builder.attribute(f"{{{XML_NS}}}lang", "en")
    assert attribute.prefix == "xml"
    assert root.in_scope_namespaces["xml"] == XML_NS


def test_namespace_aliases_rewrite_prefixes() -> None:
    builder = ResultBuilder.document()
    root = builder.element(
        "{urn:old}root",
        namespaces={"old": "urn:old", "keep": "urn:keep"},
        namespace_aliases={"old": "new"},
    )
    assert root.prefix == "new"
    assert root.in_scope_namespaces["new"] == "urn:old"
    assert "old" not in root.in_scope_namespaces
    assert root.in_scope_namespaces["keep"] == "urn:keep"


def test_namespace_aliases_can_target_the_default_namespace() -> None:
    builder = ResultBuilder.document()
    root = builder.element(
        "{urn:default}root",
        namespaces={"old": "urn:default"},
        namespace_aliases={"old": "#default"},
    )
    assert root.prefix == ""
    assert root.in_scope_namespaces[""] == "urn:default"


def test_namespace_alias_conflicts_are_rejected() -> None:
    builder = ResultBuilder.document()
    with pytest.raises(XsltDynamicError) as error:
        builder.element(
            "root",
            namespaces={"a": "urn:one", "b": "urn:two"},
            namespace_aliases={"a": "b"},
        )
    assert _code(error.value) == "XTDE0430"


def test_namespace_conflict_and_duplicate_handling() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root", namespaces={"p": "urn:p"})
    duplicate = builder.namespace("p", "urn:p")
    assert duplicate.prefix == "p"
    assert len([node for node in root.namespace_nodes if node.prefix == "p"]) == 1
    with pytest.raises(XsltDynamicError) as error:
        builder.namespace("p", "urn:other")
    assert _code(error.value) == "XTDE0430"


def test_namespace_validation_and_default_namespace_error() -> None:
    builder = ResultBuilder.document()
    _element(builder, "root")
    with pytest.raises(XsltDynamicError) as error:
        builder.namespace("", "urn:default")
    assert _code(error.value) == "XTDE0440"
    for prefix, uri, expected in (
        ("1bad", "urn:p", "XTDE0920"),
        ("xmlns", "urn:p", "XTDE0920"),
        ("p", "", "XTDE0930"),
        ("p", XMLNS_NS, "XTDE0905"),
        ("xml", "urn:not-xml", "XTDE0925"),
        ("p", XML_NS, "XTDE0925"),
    ):
        with pytest.raises(XsltDynamicError) as error:
            builder.namespace(prefix, uri)
        assert _code(error.value) == expected
    builder.namespace("xml", XML_NS)


def test_append_namespace_nodes_applies_fixup_and_deduplicates() -> None:
    source = _xml(b"<s xmlns:p='urn:p' xmlns:q='urn:q'/>")
    namespace_p = source.root_element.namespace_node("p")
    namespace_q = source.root_element.namespace_node("q")
    assert namespace_p is not None and namespace_q is not None
    builder = ResultBuilder.document()
    root = builder.element("root")
    builder.append((namespace_p, namespace_p, namespace_q))
    assert root.in_scope_namespaces["p"] == "urn:p"
    assert root.in_scope_namespaces["q"] == "urn:q"
    assert len([node for node in root.namespace_nodes if node.prefix == "p"]) == 1


def test_append_namespace_node_rejects_default_on_no_namespace_element() -> None:
    source = _xml(b"<s xmlns='urn:default'/>")
    namespace = source.root_element.namespace_node("")
    assert namespace is not None
    builder = ResultBuilder.document()
    _element(builder, "root")
    with pytest.raises(XsltDynamicError) as error:
        builder.append((namespace,))
    assert _code(error.value) == "XTDE0440"


def test_append_attribute_after_content_is_rejected() -> None:
    source = _xml(b"<s a='1'/>")
    attribute = source.root_element.attributes[0]
    builder = ResultBuilder.document()
    _element(builder)
    builder.text("content")
    with pytest.raises(XsltDynamicError) as error:
        builder.append((attribute,))
    assert _code(error.value) == "XTDE0410"


# ---------------------------------------------------------------------------
# Text, comments, and processing instructions


def test_adjacent_text_merges_and_zero_length_text_disappears() -> None:
    builder = ResultBuilder.document()
    root = _element(builder)
    first = builder.text("a")
    second = builder.text("b")
    assert first is second
    assert first is not None and first.string_value == "ab"
    assert builder.text("") is None
    builder.append((XdmAtomic.integer(1), XdmAtomic.integer(2)))
    assert root.children[0].string_value == "ab1 2"
    builder.append((XdmAtomic.string(""), XdmAtomic.string("z")))
    assert root.children[0].string_value == "ab1 2 z"


def test_zero_length_atomic_strings_keep_their_separators() -> None:
    middle = ResultBuilder.document()
    middle_root = _element(middle)
    middle.append((XdmAtomic.string("a"), XdmAtomic.string(""), XdmAtomic.string("b")))
    assert middle_root.children[0].string_value == "a  b"

    ending = ResultBuilder.document()
    ending_root = _element(ending)
    ending.append((XdmAtomic.string("a"), XdmAtomic.string("")))
    assert ending_root.children[0].string_value == "a "

    empties = ResultBuilder.document()
    empties_root = _element(empties)
    empties.append(
        (
            XdmAtomic.string(""),
            XdmAtomic.string(""),
            XdmAtomic.string(""),
        )
    )
    assert empties_root.children[0].string_value == "  "


def test_append_text_nodes_merges_with_existing_text() -> None:
    text = _xml(b"<s>source</s>").root_element.children[0]
    builder = ResultBuilder.document()
    _element(builder)
    builder.text("head")
    builder.append((text,))
    assert builder.to_sequence()[0].children[0].string_value == "headsource"


def test_comments_and_processing_instructions_are_sanitized() -> None:
    builder = ResultBuilder.document()
    root = _element(builder)
    comment = builder.comment("a--b-")
    instruction = builder.processing_instruction("xml-stylesheet", "  href?>")
    assert comment.kind is NodeKind.COMMENT
    assert comment.string_value == "a- -b- "
    assert instruction.kind is NodeKind.PROCESSING_INSTRUCTION
    assert instruction.target == "xml-stylesheet"
    assert instruction.string_value == "href? >"
    assert [child.kind for child in root.children] == [
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
    ]


def test_processing_instruction_target_validation() -> None:
    builder = ResultBuilder.document()
    _element(builder)
    for target in ("xml", "XML", "1bad", "bad name"):
        with pytest.raises(XsltDynamicError) as error:
            builder.processing_instruction(target, "value")
        assert _code(error.value) == "XTDE0890"


# ---------------------------------------------------------------------------
# Copies and annotations


def _typed_source() -> XdmNode:
    source = _xml(
        b"<p:root xmlns:p='urn:p' xmlns:unused='urn:unused' a='1'>"
        b"<p:child>text</p:child><!--comment--><?pi value?></p:root>"
    ).root_element
    source._apply_occurrence_annotation(XdmAnnotation("urn:element-type", False))
    source.attributes[0]._apply_occurrence_annotation(XdmAnnotation("urn:attribute-type", None))
    return source


def test_builder_constructed_annotations_are_untyped() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root", attributes=(("id", "1"),))
    namespace = builder.namespace("p", "urn:p")
    comment = builder.comment("c")
    instruction = builder.processing_instruction("pi", "v")
    text = builder.text("value")

    assert root.annotation.type_name == UNTYPED
    assert root.annotation.nilled is False
    assert root.attributes[0].annotation.type_name == UNTYPED_ATOMIC
    assert text is not None and text.annotation.type_name == UNTYPED_ATOMIC
    assert comment.annotation.type_name is None
    assert instruction.annotation.type_name is None
    assert namespace.annotation.type_name is None


def test_explicit_annotations_and_schema_pool_are_retained() -> None:
    pool = SchemaPool()
    annotation = XdmAnnotation("urn:typed", False)
    builder = ResultBuilder.document(schema_pool=pool)
    root = builder.element("root", annotation=annotation)
    attribute = builder.attribute("id", "1", annotation=XdmAnnotation("urn:attr", None))
    text = builder.text("value", annotation=XdmAnnotation("urn:text", None))

    assert builder.root.schema_pool is pool
    assert root.annotation is annotation
    assert attribute.annotation.type_name == "urn:attr"
    assert text is not None and text.annotation.type_name == "urn:text"


def test_deep_copy_preserves_structure_annotations_and_namespaces() -> None:
    source = _typed_source()
    builder = ResultBuilder.document()
    parent = _element(builder, "out")
    copied = builder.copy(source)

    assert copied is not source
    assert copied.parent is parent
    assert copied.annotation is source.annotation
    assert copied.attributes[0].annotation is source.attributes[0].annotation
    assert copied.name == "{urn:p}root"
    assert copied.prefix == "p"
    assert [child.kind for child in copied.children] == [
        NodeKind.ELEMENT,
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
    ]
    assert copied.children[0].string_value == "text"
    assert any(node.prefix == "unused" for node in copied.namespace_nodes)


def test_shallow_copy_copies_name_attributes_and_namespaces_only() -> None:
    source = _typed_source()
    builder = ResultBuilder.document()
    _element(builder, "out")
    copied = builder.copy(source, deep=False)
    assert copied.parent is not None and copied.parent.name == "out"
    assert copied.annotation is source.annotation
    assert len(copied.attributes) == 1
    assert copied.attributes[0].string_value == "1"
    assert copied.children == ()
    assert any(node.prefix == "p" for node in copied.namespace_nodes)


def test_copy_without_namespaces_still_fixes_required_bindings() -> None:
    source = _typed_source()
    builder = ResultBuilder.document()
    _element(builder, "out")
    copied = builder.copy(source, copy_namespaces=False)
    in_scope = {node.prefix: node.string_value for node in copied.namespace_nodes}
    assert in_scope["p"] == "urn:p"
    assert "unused" not in in_scope
    assert copied.prefix == "p"
    assert copied.attributes[0].prefix == ""


def test_clone_document_and_detached_nodes() -> None:
    source = _xml(b"<root a='1'><child/></root>")
    builder = ResultBuilder.temporary()
    document_clone = builder.clone(source)
    assert isinstance(document_clone, XdmDocument)
    assert document_clone is not source
    assert document_clone.root_element is not source.root_element
    assert document_clone.root_element.name == "root"

    attribute = builder.clone(source.root_element.attributes[0])
    assert attribute.kind is NodeKind.ATTRIBUTE
    assert attribute.parent is None
    builder.element("target")
    builder.append((attribute,))
    assert builder.to_sequence()[0].attributes[0].string_value == "1"

    comment = _xml(b"<s><!--c--></s>").root_element.children[0]
    copied_comment = builder.copy(comment)
    assert copied_comment.string_value == "c"


def test_copy_of_an_attribute_requires_legal_position() -> None:
    attribute = _xml(b"<s a='1'/>").root_element.attributes[0]
    builder = ResultBuilder.document()
    _element(builder)
    builder.text("content")
    with pytest.raises(XsltDynamicError) as error:
        builder.copy(attribute)
    assert _code(error.value) == "XTDE0410"


# ---------------------------------------------------------------------------
# Raw sequences and final-result-tree wrapping


def test_wrap_normalizes_atomics_into_a_document() -> None:
    tree = ResultBuilder.wrap(
        (XdmAtomic.integer(1), XdmAtomic.integer(2)),
        output_uri="http://example.com/out.xml",
    )
    assert tree.document_uri == "http://example.com/out.xml"
    assert tree.base_uri == "http://example.com/out.xml"
    assert [child.kind for child in tree.children] == [NodeKind.TEXT]
    assert tree.children[0].string_value == "1 2"


def test_wrap_uses_an_item_separator_between_all_items() -> None:
    first = _xml(b"<one/>").root_element
    second = _xml(b"<two/>").root_element
    tree = ResultBuilder.wrap((first, second), item_separator="|")
    assert [child.kind for child in tree.children] == [
        NodeKind.ELEMENT,
        NodeKind.TEXT,
        NodeKind.ELEMENT,
    ]
    assert tree.children[1].string_value == "|"

    atomics = ResultBuilder.wrap((XdmAtomic.integer(1), XdmAtomic.integer(2)), item_separator=",")
    assert atomics.children[0].string_value == "1,2"

    empty = ResultBuilder.wrap(())
    assert empty.children == ()


def test_wrap_rejects_attributes_namespaces_and_function_items() -> None:
    document = _xml(b"<s a='1' xmlns:p='urn:p'/>")
    attribute = document.root_element.attributes[0]
    namespace = document.root_element.namespace_node("p")
    assert namespace is not None

    with pytest.raises(XsltDynamicError) as error:
        ResultBuilder.wrap((attribute,))
    assert _code(error.value) == "XTDE0420"
    with pytest.raises(XsltDynamicError) as error:
        ResultBuilder.wrap((namespace,))
    assert _code(error.value) == "XTDE0420"
    with pytest.raises(XsltDynamicError) as error:
        ResultBuilder.wrap((XdmMap.from_mapping({"k": 1}),))
    assert _code(error.value) == "XTDE0450"


def test_wrap_flattens_arrays_in_complex_content() -> None:
    tree = ResultBuilder.wrap(
        (
            XdmArray([XdmAtomic.string("a"), XdmArray([XdmAtomic.integer(1)])]),
            XdmAtomic.string("b"),
        )
    )
    assert tree.children[0].string_value == "a 1 b"


def test_wrap_flattens_documents_without_reparsing() -> None:
    source = _xml(b"<root>text<child/></root>")
    tree = ResultBuilder.wrap((source,))
    assert len(tree.children) == 1
    assert tree.children[0].name == "root"
    assert tree.children[0] is not source.root_element
    assert tree.children[0].document is tree
    assert tree.children[0].children[0].string_value == "text"


def test_append_documents_flattens_children_in_order() -> None:
    first = _xml(b"<one/>")
    second = _xml(b"<two/>")
    builder = ResultBuilder.document()
    builder.append((first, second))
    assert [child.name for child in builder.to_sequence()] == ["one", "two"]


def test_append_flattens_nested_arrays_of_nodes_atomics_and_maps() -> None:
    source = _xml(b"<element/>").root_element
    builder = ResultBuilder.document()
    builder.append(
        (
            XdmArray(
                [
                    XdmAtomic.string("head"),
                    XdmArray([source, XdmAtomic.integer(1)]),
                    XdmAtomic.string("tail"),
                ]
            ),
        )
    )
    sequence = builder.to_sequence()
    assert sequence[0].string_value == "head"
    assert sequence[1].name == "element"
    assert sequence[1].parent is builder.root
    assert sequence[2].string_value == "1 tail"

    with pytest.raises(XsltDynamicError) as error:
        builder.append((XdmArray([XdmMap.from_mapping({"k": 1})]),))
    assert _code(error.value) == "XTDE0450"


def test_append_function_items_is_rejected() -> None:
    builder = ResultBuilder.document()
    with pytest.raises(XsltDynamicError) as error:
        builder.append((XdmMap.from_mapping({"k": 1}),))
    assert _code(error.value) == "XTDE0450"
    with pytest.raises(XsltDynamicError):
        builder.append(("not an XDM item",))


# ---------------------------------------------------------------------------
# Detached node construction used by construction instructions


def test_detached_nodes_are_parentless_and_typed() -> None:
    builder = ResultBuilder.temporary()
    attribute = builder.attribute_node("{urn:p}id", "1", prefix="p")
    text = builder.text_node("value")
    comment = builder.comment_node("a--b")
    instruction = builder.processing_instruction_node("pi", "value")
    namespace = builder.namespace_node("p", "urn:p")

    assert attribute.kind is NodeKind.ATTRIBUTE and attribute.parent is None
    assert attribute.prefix == "p"
    assert attribute.namespace_uri == "urn:p"
    assert text.kind is NodeKind.TEXT and text.parent is None
    assert text.string_value == "value"
    assert comment.parent is None and comment.string_value == "a- -b"
    assert instruction.target == "pi"
    assert namespace.kind is NodeKind.NAMESPACE and namespace.parent is None
    assert namespace.namespace_binding == "urn:p"


def test_detached_node_validation() -> None:
    builder = ResultBuilder.temporary()
    with pytest.raises(XsltDynamicError) as error:
        builder.attribute_node("xmlns", "urn:x")
    assert _code(error.value) == "XTDE0855"
    with pytest.raises(XsltDynamicError) as error:
        builder.namespace_node("p", "")
    assert _code(error.value) == "XTDE0930"
    with pytest.raises(XsltDynamicError) as error:
        builder.processing_instruction_node("xml", "value")
    assert _code(error.value) == "XTDE0890"


def test_element_attributes_parameter_and_duplicates() -> None:
    builder = ResultBuilder.document()
    root = builder.element(
        "root",
        attributes=(
            ("id", "first"),
            ("id", "second"),
            ("{urn:p}value", "v"),
        ),
        namespaces={"p": "urn:p"},
    )
    assert [attribute.name for attribute in root.attributes] == ["id", "{urn:p}value"]
    assert root.attribute("id") is not None
    assert root.attribute("id").string_value == "second"
    assert root.attribute("{urn:p}value").prefix == "p"


def test_nested_elements_receive_parent_base_uri_and_text_base() -> None:
    builder = ResultBuilder.document(base_uri="http://example.com/base")
    root = builder.element("root")
    child = builder.element("child")
    text = builder.text("value")
    assert child.base_uri == "http://example.com/base"
    assert text is not None and text.base_uri == "http://example.com/base"
    builder.end_element()
    assert builder.end_element() is root


@dataclass(slots=True)
class _ProbeExtent:
    """One protocol double proving the builder extent matches the frame seam."""

    calls: list[str] = field(default_factory=list)

    def rollback(self) -> None:
        self.calls.append("rollback")


def test_extent_protocol_matches_scheduler_contract() -> None:
    probe = _ProbeExtent()
    scheduler = Scheduler()

    def step(frame: Frame) -> Continue:
        del frame
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        scheduler.run(Frame(kind="probe", extent=probe), step)
    assert probe.calls == ["rollback"]


# ---------------------------------------------------------------------------
# Helper branches and namespace-fixup edge cases


def test_prefix_helpers_choose_expected_prefixes() -> None:
    assert _prefix_for(None, {"p": "urn:p"}) == ""
    assert _prefix_for("urn:x", {"": "urn:x"}) == ""
    assert _prefix_for("urn:x", {"p": "urn:y", "q": "urn:x"}) == "q"
    assert _prefix_for("urn:missing", {"p": "urn:p"}) == ""
    assert _attribute_prefix(None, {}) == ""
    assert _attribute_prefix("urn:x", {"": "urn:x"}) == ""
    assert _attribute_prefix("urn:x", {"p": "urn:x"}) == "p"
    assert _attribute_prefix("urn:missing", {"p": "urn:p"}) == ""
    assert _new_prefix({"ns1": "urn:one", "ns2": "urn:two"}, "urn:new") == "ns3"


def test_non_clark_malformed_element_name_is_rejected() -> None:
    builder = ResultBuilder.document()
    with pytest.raises(XsltDynamicError) as error:
        builder.element("bad name")
    assert _code(error.value) == "XTDE0820"


def test_namespace_alias_validation_errors() -> None:
    for namespaces, aliases, expected in (
        ({"p": "urn:p"}, {"p": "xmlns"}, "XTDE0905"),
        ({"p": "urn:p"}, {"p": "xml"}, "XTDE0925"),
        ({"p": XML_NS}, {}, "XTDE0925"),
        ({"xmlns": "urn:x"}, {}, "XTDE0905"),
    ):
        builder = ResultBuilder.document()
        with pytest.raises(XsltDynamicError) as error:
            builder.element("root", namespaces=namespaces, namespace_aliases=aliases)
        assert _code(error.value) == expected


def test_element_with_an_occupied_default_namespace_gets_a_prefix() -> None:
    builder = ResultBuilder.document()
    element = builder.element("{urn:new}root", namespaces={"": "urn:occupied"})
    assert element.prefix == "ns1"
    assert element.in_scope_namespaces["ns1"] == "urn:new"
    assert element.in_scope_namespaces[""] == "urn:occupied"


def test_element_initial_attribute_namespace_fixup() -> None:
    builder = ResultBuilder.document()
    element = builder.element("root", attributes=(("{urn:q}a", "v"),))
    assert element.attributes[0].prefix == "ns1"
    assert element.in_scope_namespaces["ns1"] == "urn:q"


def test_element_rejects_xmlns_attributes_in_initial_scope() -> None:
    builder = ResultBuilder.document()
    with pytest.raises(XsltDynamicError) as error:
        builder.element("root", attributes=((f"{{{XMLNS_NS}}}p", "urn:p"),))
    assert _code(error.value) == "XTDE0850"
    with pytest.raises(XsltDynamicError) as error:
        builder.element("root", attributes=(("xmlns", "urn:p"),))
    assert _code(error.value) == "XTDE0855"


def test_element_explicit_prefix_must_match_the_element_namespace() -> None:
    builder = ResultBuilder.document()
    element = builder.element(
        "{urn:x}root",
        namespaces={"p": "urn:y"},
        prefix="p",
    )
    assert element.prefix == ""
    assert element.in_scope_namespaces[""] == "urn:x"


def test_detached_attribute_without_namespace_drops_its_prefix() -> None:
    builder = ResultBuilder.temporary()
    attribute = builder.attribute_node("id", "1", prefix="p")
    assert attribute.prefix == ""
    assert attribute.namespace_uri is None


def test_namespace_fixup_renames_a_conflicting_element_prefix() -> None:
    source = _xml(b"<s xmlns:p='urn:new'/>")
    namespace = source.root_element.namespace_node("p")
    assert namespace is not None

    builder = ResultBuilder.document()
    root = builder.element("{urn:old}root", namespaces={"p": "urn:old"})
    assert root.prefix == "p"
    builder.copy(namespace)
    assert root.prefix == "ns1"
    assert root.in_scope_namespaces["ns1"] == "urn:old"
    assert root.in_scope_namespaces["p"] == "urn:new"


def test_namespace_fixup_reuses_an_alternate_prefix_for_the_element() -> None:
    source = _xml(b"<s xmlns:p='urn:new'/>")
    namespace = source.root_element.namespace_node("p")
    assert namespace is not None

    builder = ResultBuilder.document()
    root = builder.element(
        "{urn:old}root",
        namespaces={"p": "urn:old", "alt": "urn:old"},
    )
    attribute = builder.attribute("{urn:old}value", "1")
    assert root.prefix == "p"
    builder.copy(namespace)
    assert root.prefix == "alt"
    assert root.in_scope_namespaces["p"] == "urn:new"
    assert attribute.prefix == "alt"
    namespaces = [(node.prefix, node.name, node.string_value) for node in root.namespace_nodes]
    assert ("alt", "alt", "urn:old") in namespaces
    assert ("p", "p", "urn:new") in namespaces
    assert len({prefix for prefix, _, _ in namespaces}) == len(namespaces)
    for prefix, name, _ in namespaces:
        assert name == (prefix or None)


def test_namespace_fixup_generated_prefix_keeps_node_identity_consistent() -> None:
    source = _xml(b"<s xmlns:p='urn:new'/>")
    namespace = source.root_element.namespace_node("p")
    assert namespace is not None

    builder = ResultBuilder.document()
    root = builder.element("{urn:old}root", namespaces={"p": "urn:old"})
    attribute = builder.attribute("{urn:old}value", "1")
    builder.copy(namespace)
    renamed = root.namespace_node("ns1")
    assert renamed is not None
    assert renamed.name == "ns1"
    assert renamed.string_value == "urn:old"
    assert attribute.prefix == "ns1"
    assert root.namespace_node("p") is not None
    assert root.namespace_node("p").string_value == "urn:new"
    assert len({node.prefix for node in root.namespace_nodes}) == len(root.namespace_nodes)


def test_namespace_fixup_renames_a_conflicting_default_namespace() -> None:
    source = _xml(b"<s xmlns='urn:new'/>")
    namespace = source.root_element.namespace_node("")
    assert namespace is not None

    builder = ResultBuilder.document()
    root = builder.element("{urn:old}root", namespaces={"": "urn:old"})
    builder.copy(namespace)
    assert root.prefix == "ns1"
    assert root.in_scope_namespaces["ns1"] == "urn:old"
    assert root.in_scope_namespaces[""] == "urn:new"
    prefixes = [node.prefix for node in root.namespace_nodes]
    assert len(prefixes) == len(set(prefixes))


def test_copying_attribute_or_namespace_in_illegal_positions() -> None:
    source = _xml(b"<s a='1' xmlns:p='urn:p'/>")
    attribute = source.root_element.attributes[0]
    namespace = source.root_element.namespace_node("p")
    assert namespace is not None

    builder = ResultBuilder.document()
    with pytest.raises(XsltDynamicError) as error:
        builder.copy(attribute)
    assert _code(error.value) == "XTDE0420"
    with pytest.raises(XsltDynamicError) as error:
        builder.copy(namespace)
    assert _code(error.value) == "XTDE0420"

    builder.element("root")
    builder.text("content")
    with pytest.raises(XsltDynamicError) as error:
        builder.copy(namespace)
    assert _code(error.value) == "XTDE0410"


def test_copy_of_text_merges_into_existing_text() -> None:
    source_text = _xml(b"<s>source</s>").root_element.children[0]
    builder = ResultBuilder.document()
    root = builder.element("root")
    builder.text("head")
    copied = builder.copy(source_text)
    assert copied.string_value == "source"
    assert root.children[0].string_value == "headsource"


def test_copy_of_a_document_into_an_element_and_shallow_document_copy() -> None:
    source = _xml(b"<root><child/></root>")
    builder = ResultBuilder.document()
    parent = builder.element("parent")
    copied = builder.copy(source)
    assert isinstance(copied, XdmDocument)
    assert parent.children[0].name == "root"

    shallow = builder.copy(source, deep=False)
    assert isinstance(shallow, XdmDocument)
    assert shallow.children == ()
    assert len(parent.children) == 1


def test_builder_properties_and_final_output_validation() -> None:
    with pytest.raises(TypeError, match="final_output"):
        ResultBuilder(_NodeFactory(), final_output=1)  # type: ignore[arg-type]

    pool = SchemaPool()
    builder = ResultBuilder.document(
        document_uri="http://example.com/out",
        base_uri="http://example.com/base",
        schema_pool=pool,
    )
    assert builder.document_uri == "http://example.com/out"
    assert builder.base_uri == "http://example.com/base"
    assert builder.schema_pool is pool
    assert builder.finalized is False
    builder.to_tree()
    assert builder.finalized is True


def test_build_result_tree_module_entry_point() -> None:
    tree = build_result_tree(
        (XdmAtomic.integer(1), XdmAtomic.integer(2)),
        item_separator=",",
    )
    assert tree.children[0].string_value == "1,2"


def test_copy_without_namespaces_covers_scope_variants() -> None:
    builder = ResultBuilder.document()

    builder.append((_xml(b"<root a='1'/>").root_element,))
    plain = builder.to_sequence()[0]
    plain_builder = ResultBuilder.temporary()
    plain_copy = plain_builder.copy(plain, copy_namespaces=False)
    assert plain_copy.attributes[0].prefix == ""

    default = _xml(b"<root xmlns='urn:r'><child/></root>").root_element
    default_copy = ResultBuilder.temporary()
    copied_default = default_copy.copy(default, copy_namespaces=False)
    assert copied_default.prefix == ""
    assert copied_default.in_scope_namespaces[""] == "urn:r"

    xml_attribute = _xml(b"<root xml:lang='en'/>").root_element
    xml_builder = ResultBuilder.temporary()
    copied_xml = xml_builder.copy(xml_attribute, copy_namespaces=False)
    assert copied_xml.attributes[0].prefix == "xml"
    assert copied_xml.in_scope_namespaces["xml"] == XML_NS

    prefixed = _xml(b"<p:root xmlns:p='urn:p' p:a='1'/>").root_element
    prefixed_builder = ResultBuilder.temporary()
    copied_prefixed = prefixed_builder.copy(prefixed, copy_namespaces=False)
    assert copied_prefixed.in_scope_namespaces["p"] == "urn:p"
    assert copied_prefixed.attributes[0].prefix == "p"

    unbound = _xml(b"<root xmlns:p='urn:p' p:a='1'/>").root_element
    unbound_builder = ResultBuilder.temporary()
    copied_unbound = unbound_builder.copy(unbound, copy_namespaces=False)
    assert copied_unbound.in_scope_namespaces["p"] == "urn:p"


def test_copy_scope_generates_a_prefix_for_a_conflicting_attribute_prefix() -> None:
    factory = _NodeFactory()
    source = factory.element("{urn:e}root", namespaces={"ns1": "urn:e"})
    attribute = factory.attribute(
        "{urn:two}a",
        "1",
        parent=source,
        prefix="ns1",
    )
    source._append_attribute(attribute)
    builder = ResultBuilder.temporary()
    copied = builder.copy(source, copy_namespaces=False)
    assert copied.prefix == "ns1"
    assert copied.attributes[0].prefix == "ns2"
    assert copied.in_scope_namespaces["ns2"] == "urn:two"


def test_clone_document_skips_non_element_children_when_finding_the_root() -> None:
    source = _xml(b"<!--lead--><root/><!--tail-->")
    builder = ResultBuilder.temporary()
    clone = builder.clone(source)
    assert isinstance(clone, XdmDocument)
    assert clone.children[0].kind is NodeKind.COMMENT
    assert clone.root_element is clone.children[1]
    assert clone.root_element.name == "root"


def test_append_namespace_conflict_without_element_prefix_is_rejected() -> None:
    source = _xml(b"<s xmlns:p='urn:new'/>")
    namespace = source.root_element.namespace_node("p")
    assert namespace is not None
    builder = ResultBuilder.document()
    builder.element("root", namespaces={"p": "urn:old"})
    with pytest.raises(XsltDynamicError) as error:
        builder.copy(namespace)
    assert _code(error.value) == "XTDE0430"


def test_attribute_explicit_prefix_paths() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root", namespaces={"p": "urn:p"})
    reused = builder.attribute("{urn:p}a", "1", prefix="p")
    assert reused.prefix == "p"
    declared = builder.attribute("{urn:q}b", "2", prefix="q")
    assert declared.prefix == "q"
    assert root.in_scope_namespaces["q"] == "urn:q"


def test_attribute_prefix_validation() -> None:
    builder = ResultBuilder.document()
    builder.element("root")
    for prefix, expected in (
        ("1bad", "XTDE0850"),
        ("xmlns", "XTDE0850"),
        ("xml", "XTDE0925"),
    ):
        with pytest.raises(XsltDynamicError) as error:
            builder.attribute("{urn:x}a", "1", prefix=prefix)
        assert _code(error.value) == expected


def test_namespace_declaration_for_the_xml_binding_adds_its_node() -> None:
    builder = ResultBuilder.document()
    root = builder.element("root")
    node = builder.namespace("xml", XML_NS)
    assert node.parent is root
    assert node.namespace_binding == XML_NS
    assert root.namespace_node("xml") is node


def test_detached_attribute_rejects_an_invalid_prefix() -> None:
    builder = ResultBuilder.temporary()
    with pytest.raises(XsltDynamicError) as error:
        builder.attribute_node("{urn:p}a", "1", prefix="1bad")
    assert _code(error.value) == "XTDE0850"


def test_copy_attribute_into_an_empty_element_attaches_directly() -> None:
    attribute = _xml(b"<s a='1'/>").root_element.attributes[0]
    builder = ResultBuilder.document()
    root = builder.element("root")
    copied = builder.copy(attribute)
    assert copied.parent is root
    assert root.attributes[0] is copied
    assert copied.string_value == "1"
