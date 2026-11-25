"""Coherent XDM views of editable schemaless XML documents."""

import io
from xml.etree import ElementTree as ET

import pytest

from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.schemaless import SchemalessDocument, SchemalessElement, SchemalessRevision, parse_xml
from pyxsd.xdm.annotations import UNTYPED, UNTYPED_ATOMIC
from pyxsd.xdm.errors import XdmInputError
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy
from pyxsd.xslt.processor import XsltProcessor


def test_unedited_xml_xdm_is_source_complete_and_untyped():
    doc = parse_xml(io.BytesIO(b"<r><!--c--><?p i?><x/></r>"))
    xdm = doc.to_xdm()
    assert doc.to_xdm() is xdm
    assert {n.kind for n in xdm.descendants} >= {NodeKind.COMMENT, NodeKind.PROCESSING_INSTRUCTION}
    assert xdm.root_element.annotation.type_name == UNTYPED
    assert xdm.source_index is doc.source_index


def test_direct_store_mutation_stales_xdm():
    doc = parse_xml(io.StringIO("<r a='1'><x>old</x></r>"))
    doc.to_xdm()
    doc.root._attribs_["a"] = "2"  # deliberately bypasses mark_dirty()
    with pytest.raises(XdmInputError):
        doc.to_xdm()


def test_xslt_accepts_schemaless_document():
    sheet = XsltProcessor().compile(
        io.StringIO(
            '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">'
            '<xsl:output omit-xml-declaration="yes"/>'
            '<xsl:mode on-no-match="shallow-copy"/></xsl:stylesheet>'
        )
    )
    result = sheet.transform(parse_xml(io.StringIO("<r><x>1</x></r>")))
    assert result.serialize() == "<r><x>1</x></r>"
    source_complete = "<r><!--c--><?p i?><x>1</x></r>"
    doc = parse_xml(io.StringIO(source_complete))
    assert sheet.transform(doc).serialize() == source_complete
    doc.root.children[0]._value_[0] = "2"
    with pytest.raises(XdmInputError, match="revalidate"):
        sheet.transform(doc)


@pytest.mark.parametrize("capture_first", [False, True])
def test_direct_text_store_edit_stales_the_parse_revision(capture_first):
    doc = parse_xml(b"<r><x>old</x></r>")
    if capture_first:
        doc.to_xdm()
    doc.root.children[0]._value_[0] = "new"

    assert not doc.root.revision.dirty
    with pytest.raises(XdmInputError, match="revalidate"):
        doc.to_xdm()


@pytest.mark.parametrize(
    "edit",
    [
        lambda root: root.children[0].set_text("new"),
        lambda root: root.children[0].set_tail("new tail"),
        lambda root: root.remove_child(0),
        lambda root: root.children[0].set_text("old"),  # same signature, dirty revision
    ],
    ids=["text", "tail", "child", "same-value"],
)
@pytest.mark.parametrize("capture_first", [False, True])
def test_method_edits_stale_xdm(edit, capture_first):
    doc = parse_xml(b"<r><x>old</x>tail</r>")
    if capture_first:
        doc.to_xdm()
    edit(doc.root)

    with pytest.raises(XdmInputError, match="revalidate"):
        doc.to_xdm()


def test_replacing_the_root_with_an_equal_looking_tree_stales_xdm():
    doc = parse_xml(b"<r><x/></r>")
    doc.to_xdm()
    doc.root = parse_xml(b"<r><x/></r>").root

    with pytest.raises(XdmInputError, match="revalidate"):
        doc.to_xdm()


def test_direct_cycle_is_rejected_as_stale_xdm():
    doc = parse_xml(b"<r/>")
    doc.root._children_.append(doc.root)

    with pytest.raises(XdmInputError, match="revalidate"):
        doc.to_xdm()


def test_caller_built_element_uses_the_lexical_projection():
    root = ET.Element("{urn:x}r", {"a": "001"})
    root.text = "before"
    comment = ET.Comment("not in the lexical tree")
    comment.tail = "\rafter"
    root.append(comment)
    ET.SubElement(root, "{urn:x}x")
    root.append(ET.ProcessingInstruction("p", "not in the lexical tree"))
    root.tail = "outside"
    doc = parse_xml(root)
    root.set("a", "999")

    xdm = doc.to_xdm()

    assert doc.source_index is None
    assert doc.to_xdm() is xdm
    assert xdm.root_element.name == "{urn:x}r"
    assert xdm.root_element.string_value == "before\rafter"
    assert xdm.root_element.attributes[0].string_value == "001"
    assert xdm.root_element.attributes[0].annotation.type_name == UNTYPED_ATOMIC
    assert [node.kind for node in xdm.root_element.children] == [NodeKind.TEXT, NodeKind.ELEMENT]
    doc.root._attribs_["a"] = "002"
    with pytest.raises(XdmInputError, match="revalidate"):
        doc.to_xdm()


def test_type_interpretation_alone_leaves_xdm_untyped_and_current():
    class IntegerSnapshot:
        def scalar_value(self, node):
            return int(node.text_value())

        def attribute_value(self, node, expanded_name):
            return int(node.attribute_lexical(expanded_name))

    doc = parse_xml(b'<r a="001">002</r>')
    xdm = doc.to_xdm()
    # Exercise the existing typed-interpretation seam; public policies arrive later.
    doc.root.revision._typed_snapshot = IntegerSnapshot()

    assert doc.root.scalar_value() == 2
    assert doc.root.attribute_value("a") == 1
    assert doc.to_xdm() is xdm
    assert xdm.root_element.annotation.type_name == UNTYPED
    assert xdm.root_element.string_value == "002"
    assert xdm.root_element.attributes[0].annotation.type_name == UNTYPED_ATOMIC
    assert xdm.root_element.attributes[0].string_value == "001"


def test_revalidate_refreshes_the_edited_projection_without_comments_or_pis(tmp_path):
    path = tmp_path / "original.xml"
    path.write_bytes(b'<!--pre--><r xml:base="sub/"><x>old</x>tail<!--c--><?p i?></r>')
    doc = parse_xml(path)
    old_xdm = doc.to_xdm()
    doc.root.children[0].set_text("new")

    again = doc.revalidate()
    xdm = again.to_xdm()

    assert isinstance(again, SchemalessDocument)
    assert again.schema is None and again.report_current and again.is_valid
    assert again.report is not doc.report
    assert again.root is not doc.root and isinstance(again.root, SchemalessElement)
    assert again.root.revision is not doc.root.revision
    assert isinstance(again.root.revision, SchemalessRevision)
    assert again.root.children[0].revision is again.root.revision
    assert again.source == str(path) == xdm.source_name == again.source_index.source
    assert xdm.source_index is again.source_index and xdm.source_index is not doc.source_index
    assert again.source_index.extract(again.root.children[0].source_location.path) == b"<x>new</x>"
    assert xdm.document_uri == path.as_uri()
    assert xdm.root_element.base_uri == again.root.base_uri == (tmp_path / "sub").as_uri() + "/"
    assert {node.kind for node in xdm.descendants} == {NodeKind.ELEMENT, NodeKind.TEXT}
    assert xdm.root_element.string_value == "newtail"
    assert old_xdm.root_element.string_value == "oldtail"
    assert {n.kind for n in old_xdm.descendants} >= {
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
    }
    again.root.children[0].set_text("only in again")
    assert doc.root.children[0].text_value() == "new"
    with pytest.raises(XdmInputError, match="revalidate"):
        doc.to_xdm()


def test_revalidate_keeps_the_parser_budget_after_each_refresh():
    doc = parse_xml(b"<r/>", xml_limits=XmlLimits(max_bytes=10)).revalidate()
    doc.root.set_text("more than ten bytes")

    with pytest.raises(ResourcePolicyError, match="max_bytes=10"):
        doc.revalidate()


def test_revalidate_keeps_parser_policy(monkeypatch):
    from pyxsd.schemaless import parser

    policy = XmlParserPolicy.strict()
    doc = parse_xml(b"<r/>", xml_policy=policy)
    parse = parser.parse_with_namespaces

    def record_policy(*args, **kwargs):
        assert kwargs["policy"] is policy
        return parse(*args, **kwargs)

    # After serialization there is no DTD to distinguish the policy profiles.
    monkeypatch.setattr(parser, "parse_with_namespaces", record_policy)
    assert doc.revalidate().root._name_ == "r"


def test_same_root_wrapper_keeps_the_original_xdm_baseline():
    doc = parse_xml(b"<r><!--original--></r>")
    same = doc.transform(lambda root: root)
    assert same.to_xdm().root_element.children[0].kind is NodeKind.COMMENT

    doc.root._attribs_["a"] = "changed"
    later = doc.transform(lambda root: root)
    with pytest.raises(XdmInputError, match="revalidate"):
        later.to_xdm()


def test_different_root_wrapper_is_a_lexical_source_not_the_original_capture():
    doc = parse_xml(b"<r><x>value<!--omitted--></x></r>")
    child_doc = doc.transform(lambda root: root.children[0])

    xdm = child_doc.to_xdm()
    assert xdm.root_element.name == "x"
    assert [node.kind for node in xdm.descendants] == [NodeKind.ELEMENT, NodeKind.TEXT]
    again = child_doc.revalidate()
    assert again.source is None and again.report_current
    assert again.to_xdm().root_element.string_value == "value"
