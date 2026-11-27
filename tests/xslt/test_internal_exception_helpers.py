"""Focused helpers behind the internal-exception remediation.

These tests exercise the host-node bridge and the small conversion helpers
directly, so every defensive branch added for the remediation is reached
without relying on a corpus case to happen to take it.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
from elementpath import (
    CommentNode,
    NamespaceNode,
    ProcessingInstructionNode,
    TextNode,
    build_node_tree,
)
from elementpath.xpath_nodes import EtreeElementNode
from tests.xslt.support import stylesheet, transform

from pyxsd.xdm.document import parse_typed_document
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xdm.values import XdmAtomic
from pyxsd.xpath.engine import (
    XPathEngine,
    _XdmAdapterRegistry,
    host_node_to_xdm,
    standalone_node_adapter,
)
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, XPathStaticContext
from pyxsd.xslt.errors import XsltDynamicError, XsltStaticError
from pyxsd.xslt.processor import _Invocation
from pyxsd.xslt.runtime.frames import PYXSD_FRAME_EXHAUSTED, Frame, Scheduler
from pyxsd.xslt.runtime.transformation import Transformation

APP = "urn:example:app"


def _element(uri: str = "urn:x") -> ET.Element:
    root = ET.Element(f"{{{uri}}}root", {"a": "1", "{urn:y}b": "2"})
    root.text = "t"
    root.append(ET.Comment("c"))
    root.append(ET.ProcessingInstruction("pi", "v"))
    root.append(ET.Element(f"{{{uri}}}empty"))
    root[-1].tail = "tail"
    return root


def test_host_tree_serializer_preserves_nodes_tails_and_attributes() -> None:
    node = EtreeElementNode(_element(), nsmap={"p": "urn:x"})
    converted = host_node_to_xdm(node)

    assert converted.kind is NodeKind.ELEMENT
    assert [child.kind for child in converted.children] == [
        NodeKind.TEXT,
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
        NodeKind.ELEMENT,
        NodeKind.TEXT,
    ]
    assert converted.children[2].string_value == "v"
    assert converted.children[4].string_value == "tail"
    assert converted.children[3].children == ()
    assert {attribute.name for attribute in converted.attributes} == {"a", "{urn:y}b"}
    # One element vocabulary is serialized as the default namespace, while the
    # attribute vocabulary receives a generated prefix.
    assert converted.in_scope_namespaces[""] == "urn:x"
    assert "urn:y" in {*converted.in_scope_namespaces.values()}


def test_host_tree_serializer_keeps_prefixes_for_several_vocabularies() -> None:
    root = _element()
    root.append(ET.Element("{urn:z}other"))
    converted = host_node_to_xdm(EtreeElementNode(root, nsmap={"p": "urn:x"}))

    assert {child.name for child in converted.children if child.kind is NodeKind.ELEMENT} == {
        "{urn:x}empty",
        "{urn:z}other",
    }
    assert converted.in_scope_namespaces["p"] == "urn:x"
    assert "urn:z" in {*converted.in_scope_namespaces.values()}


def test_host_node_bridge_converts_raw_trees_and_documents() -> None:
    element = host_node_to_xdm(ET.fromstring("<a><b/></a>"))
    document = host_node_to_xdm(ET.ElementTree(ET.fromstring("<a><b/></a>")))
    host_document = build_node_tree(ET.ElementTree(ET.fromstring("<a><b/></a>")))
    host_element = host_document.children[0]

    assert element.kind is NodeKind.ELEMENT
    assert document.kind is NodeKind.DOCUMENT
    assert host_node_to_xdm(host_document).kind is NodeKind.DOCUMENT
    assert host_node_to_xdm(host_element).name == "a"


def test_host_node_bridge_converts_attribute_nodes() -> None:
    element = EtreeElementNode(_element(), nsmap={"q": "urn:y"})

    attribute = host_node_to_xdm(
        SimpleNamespace(node_kind="attribute", parent=element, name="q:b", string_value="2")
    )
    assert attribute.name == "{urn:y}b"
    assert attribute.string_value == "2"
    assert (
        host_node_to_xdm(
            SimpleNamespace(node_kind="attribute", parent=element, name="a", string_value="1")
        ).name
        == "a"
    )

    missing = SimpleNamespace(
        node_kind="attribute", parent=element, name="{urn:missing}x", string_value="x"
    )
    with pytest.raises(TypeError, match="not in its converted parent"):
        host_node_to_xdm(missing)
    with pytest.raises(TypeError, match="parentless"):
        host_node_to_xdm(SimpleNamespace(node_kind="attribute", parent=None))


def test_host_node_bridge_converts_namespace_nodes() -> None:
    element = EtreeElementNode(_element(), nsmap={"p": "urn:x", "q": "urn:y"})

    default = host_node_to_xdm(NamespaceNode("p", "urn:x", parent=element))
    assert default.string_value == "urn:x"
    assert default.prefix in {"", "p"}
    multi = _element()
    multi.append(ET.Element("{urn:z}other"))
    keep = host_node_to_xdm(
        NamespaceNode("p", "urn:x", parent=EtreeElementNode(multi, nsmap={"p": "urn:x"}))
    )
    assert keep.prefix == "p"
    # The host prefix is remapped to the converted tree's binding for the URI.
    remapped = host_node_to_xdm(NamespaceNode("other", "urn:y", parent=element))
    assert remapped.string_value == "urn:y"
    assert remapped.name in {"q", "ns1"}
    # An unused declaration has no serialization form; it stays a standalone
    # namespace node beside the converted parent.
    standalone = host_node_to_xdm(NamespaceNode("unused", "urn:unused", parent=element))
    assert standalone.kind is NodeKind.NAMESPACE
    assert standalone.string_value == "urn:unused"


def test_host_node_bridge_converts_text_children_and_rejects_absent_ones() -> None:
    element = EtreeElementNode(_element(), nsmap={"p": "urn:x"})

    assert host_node_to_xdm(TextNode("t", parent=element)).kind is NodeKind.TEXT
    assert host_node_to_xdm(CommentNode("c", parent=element)).kind is NodeKind.COMMENT
    assert (
        host_node_to_xdm(ProcessingInstructionNode("pi", "v", parent=element)).kind
        is NodeKind.PROCESSING_INSTRUCTION
    )
    with pytest.raises(TypeError, match="not in its converted parent"):
        host_node_to_xdm(TextNode("absent", parent=element))


def test_standalone_node_adapter_covers_documents_and_single_nodes() -> None:
    registry = _XdmAdapterRegistry()
    document = XdmDocument.parse(b"<a>x</a>")

    assert standalone_node_adapter(document, registry) is not None
    assert standalone_node_adapter(document.root_element.children[0], registry) is not None


def test_transformation_native_to_xdm_grounds_host_nodes() -> None:
    transformation = Transformation()
    host_document = build_node_tree(ET.ElementTree(ET.fromstring("<a><b/></a>")))

    converted = transformation.native_to_xdm(host_document.children[0])
    assert converted[0].kind is NodeKind.ELEMENT

    assert transformation.native_to_xdm(ET.fromstring("<a/>"))[0].kind is NodeKind.ELEMENT
    assert (
        transformation.native_to_xdm(ET.ElementTree(ET.fromstring("<a/>")))[0].kind
        is NodeKind.DOCUMENT
    )


def test_sequence_bridge_adapts_a_node_outside_its_document() -> None:
    document = XdmDocument.parse(b"<a/>")
    orphan = XdmNode(
        NodeKind.TEXT,
        document=document,
        node_id=-1,
        source_name=document.source_name,
        source_index=document.source_index,
        parent=document,
        value="orphan",
    )

    engine = XPathEngine("3.1")
    compiled = engine.compile("string(.)", XPathStaticContext())
    assert engine.evaluate(compiled, ExpressionEnvironment(), focus=Focus(item=orphan)) == (
        XdmAtomic.string("orphan"),
    )


class _InvocationStub:
    def __init__(self) -> None:
        self._adapter_registry = _XdmAdapterRegistry()


def _manual_node(
    kind: NodeKind, document: XdmDocument, *, parent: XdmNode | None, value: str = "", name=None
) -> XdmNode:
    return XdmNode(
        kind,
        document=document,
        node_id=-1,
        source_name=document.source_name,
        source_index=document.source_index,
        parent=parent,
        value=value,
        name=name,
    )


def test_node_to_native_adapts_each_grounded_node_shape() -> None:
    document = XdmDocument.parse(b"<e>text<f/></e>")
    root = document.root_element
    text = root.children[0]

    stub = _InvocationStub()
    adapted = _Invocation._node_to_native(stub, text)
    assert adapted is not None
    # The same node now reuses the registry adapter.
    assert _Invocation._node_to_native(stub, text) is adapted

    # A node parented into a converted document but absent from its tree.
    orphan_stub = _InvocationStub()
    orphan = _manual_node(NodeKind.TEXT, document, parent=root, value="orphan")
    assert _Invocation._node_to_native(orphan_stub, orphan) is not None

    # A node whose root is an element rather than a document.
    manual_root = _manual_node(NodeKind.ELEMENT, document, parent=None, name="e")
    child = _manual_node(NodeKind.ELEMENT, document, parent=manual_root, name="f")
    assert _Invocation._node_to_native(_InvocationStub(), child) is not None

    detached_stub = _InvocationStub()
    detached_text = _manual_node(NodeKind.TEXT, document, parent=None, value="detached")
    assert _Invocation._node_to_native(detached_stub, detached_text) is not None
    detached_element = _manual_node(NodeKind.ELEMENT, document, parent=None, name="e")
    assert _Invocation._node_to_native(_InvocationStub(), detached_element) is not None
    assert _Invocation._node_to_native(_InvocationStub(), document) is not None


def test_number_count_pattern_with_a_variable_uses_local_environment() -> None:
    body = """
      <xsl:param name="flag" select="true()"/>
      <xsl:template match="/">
        <out>
          <xsl:for-each select="doc/item"><xsl:number count="item[$flag]"/></xsl:for-each>
        </out>
      </xsl:template>
    """
    result = transform(stylesheet(body), b"<doc><item/><item/></doc>", delivery="stylesheet")
    serialized = result.principal_entry.serialized
    assert serialized is not None
    assert ">12</out>" in serialized


def test_a_transformation_never_changes_the_process_recursion_limit() -> None:
    """The scheduler must not touch interpreter state shared with other threads."""
    limit = sys.getrecursionlimit()

    def recursive(n: int) -> int:
        return 0 if n == 0 else recursive(n - 1) + 1

    body = """
      <xsl:template match="/">
        <out/>
      </xsl:template>
    """
    transform(stylesheet(body), b"<doc/>", delivery="stylesheet")

    assert sys.getrecursionlimit() == limit
    # A nested Python call in the same thread still sees the same ceiling.
    assert recursive(50) == 50
    assert sys.getrecursionlimit() == limit


def test_a_recursion_failure_while_handling_an_outcome_is_a_resource_failure() -> None:
    """The scheduler's own transition code is inside the same recursion boundary."""
    from pyxsd.xslt.runtime.frames import Continue

    class Exploding(Scheduler):
        def _handle_outcome(self, frame: Frame, outcome: object) -> None:
            del frame, outcome
            raise RecursionError("maximum recursion depth exceeded")

    with pytest.raises(XsltDynamicError) as caught:
        Exploding().run(Frame(), lambda frame: Continue(frame))
    assert caught.value.code == PYXSD_FRAME_EXHAUSTED


def test_a_deep_python_recursion_is_reported_as_a_resource_failure() -> None:
    """A RecursionError becomes the stable PYXSD-XSLT-001 diagnostic."""

    def exhausted(frame: Frame) -> object:
        del frame
        raise RecursionError("maximum recursion depth exceeded")

    with pytest.raises(XsltDynamicError) as caught:
        Scheduler().run(Frame(), exhausted)  # type: ignore[arg-type]
    assert caught.value.code == PYXSD_FRAME_EXHAUSTED


def _xpath(expression: str) -> object:
    engine = XPathEngine("3.1")
    return engine.evaluate(
        engine.compile(expression, XPathStaticContext()),
        ExpressionEnvironment(),
    )


def test_format_integer_conversion_paths() -> None:
    def label(value: object) -> str:
        (item,) = value  # type: ignore[misc]
        return item.string_value

    assert label(_xpath("format-integer((), 'A')")) == ""
    assert label(_xpath("format-integer(1, 'A')")) == "A"
    assert label(_xpath("format-integer(xs:untypedAtomic('1'), 'A')")) == "A"
    for expression in (
        "format-integer((1, 2), 'A')",
        "format-integer(true(), 'A')",
        "format-integer(1.5, 'A')",
    ):
        with pytest.raises(Exception) as caught:
            _xpath(expression)
        assert getattr(caught.value, "code", "").endswith("XPTY0004")
    with pytest.raises(Exception) as caught:
        _xpath("format-integer(xs:untypedAtomic('x'), 'A')")
    assert getattr(caught.value, "code", "").endswith("FORG0001")


def test_format_integer_uses_a_typed_node_value(tmp_path) -> None:
    schema = tmp_path / "integer.xsd"
    schema.write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="a" type="xs:integer"/></xs:schema>',
        encoding="utf-8",
    )
    pool = SchemaPool.from_imports([schema], xsd_version="1.1")
    document = parse_typed_document(b"<a>1</a>", schema_pool=pool, validation="strict")

    engine = XPathEngine("3.1")
    compiled = engine.compile("format-integer(., 'A')", XPathStaticContext())
    (result,) = engine.evaluate(
        compiled, ExpressionEnvironment(), focus=Focus(item=document.root_element)
    )
    assert result.string_value == "A"


def test_subsequence_reaches_the_base_function() -> None:
    result = _xpath("subsequence((1, 2, 3), 2)")
    assert [item.string_value for item in result] == ["2", "3"]  # type: ignore[union-attr]


def test_stylesheet_function_node_results_cross_the_bridge() -> None:
    body = """
      <xsl:function name="app:document" as="document-node()">
        <xsl:sequence select="parse-xml('&lt;x>hi&lt;/x>')"/>
      </xsl:function>
      <xsl:function name="app:text-node" as="text()"><xsl:text>hi</xsl:text></xsl:function>
      <xsl:function name="app:comment-node" as="comment()"><xsl:comment>c</xsl:comment></xsl:function>
      <xsl:function name="app:element-node" as="element()"><e><f/></e></xsl:function>
      <xsl:function name="app:identity" as="node()">
        <xsl:param name="n" as="node()"/>
        <xsl:sequence select="$n"/>
      </xsl:function>
      <xsl:template match="/">
        <out>
          <doc><xsl:value-of select="name(app:document()/*)"/></doc>
          <text><xsl:value-of select="app:text-node()"/></text>
          <comment><xsl:value-of select="app:comment-node()"/></comment>
          <element><xsl:value-of select="name(app:element-node()/*)"/></element>
          <adapted><xsl:value-of select="name(app:identity(doc/a))"/></adapted>
        </out>
      </xsl:template>
    """
    result = transform(
        stylesheet(body, namespaces={"app": APP}), b"<doc><a/></doc>", delivery="stylesheet"
    )
    serialized = result.principal_entry.serialized
    assert serialized is not None
    assert "<doc>x</doc>" in serialized
    assert "<text>hi</text>" in serialized
    assert "<comment>c</comment>" in serialized
    assert "<element>f</element>" in serialized
    assert "<adapted>a</adapted>" in serialized


def test_scheduler_resume_bridges_constructed_nodes() -> None:
    body = """
      <xsl:function name="app:atom-text" as="item()+">
        <xsl:text>t</xsl:text><xsl:sequence select="1"/>
      </xsl:function>
      <xsl:function name="app:atom-document" as="item()+">
        <xsl:sequence select="parse-xml('&lt;x/>')"/><xsl:sequence select="1"/>
      </xsl:function>
      <xsl:function name="app:atom-comment" as="item()+">
        <xsl:comment>c</xsl:comment><xsl:sequence select="1"/>
      </xsl:function>
      <xsl:function name="app:atom-element" as="item()+">
        <e><f/></e><xsl:sequence select="1"/>
      </xsl:function>
      <xsl:function name="app:child-node" as="item()+">
        <xsl:variable name="v"><e>text</e></xsl:variable>
        <xsl:sequence select="$v/e"/><xsl:sequence select="1"/>
      </xsl:function>
      <xsl:function name="app:repeated-node" as="item()+">
        <xsl:variable name="v"><e/></xsl:variable>
        <xsl:sequence select="($v/e, $v/e, 1)"/>
      </xsl:function>
      <xsl:template match="/">
        <out>
          <xsl:for-each select="1 to 2">
            <xsl:sequence select="app:atom-text()"/>
            <xsl:sequence select="app:atom-document()"/>
            <xsl:sequence select="app:atom-comment()"/>
            <xsl:sequence select="app:atom-element()"/>
            <xsl:sequence select="app:child-node()"/>
            <xsl:sequence select="app:repeated-node()"/>
          </xsl:for-each>
        </out>
      </xsl:template>
    """
    result = transform(stylesheet(body, namespaces={"app": APP}), b"<doc/>", delivery="stylesheet")
    serialized = result.principal_entry.serialized
    assert serialized is not None
    assert "text" in serialized
    assert "<e>" in serialized


def test_static_parse_xml_of_an_empty_sequence_is_empty() -> None:
    body = """
      <xsl:variable name="v" static="yes" select="parse-xml(())"/>
      <xsl:template match="/"><out><xsl:value-of select="count($v)"/></out></xsl:template>
    """
    result = transform(stylesheet(body), b"<doc/>", delivery="stylesheet")
    serialized = result.principal_entry.serialized
    assert serialized is not None
    assert ">0</out>" in serialized


def test_static_parse_xml_rejects_malformed_content() -> None:
    body = """
      <xsl:variable name="v" static="yes" select="parse-xml('&lt;a>')"/>
      <xsl:template match="/"><out/></xsl:template>
    """
    with pytest.raises(XsltStaticError) as caught:
        transform(stylesheet(body), b"<doc/>", delivery="stylesheet")
    assert caught.value.code == "FODC0006"
