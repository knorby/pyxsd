"""JSON projection yields coherent, untyped XML for the XSLT node model."""

import io

import pytest

from pyxsd.schemaless import MappingRule, NodeMapping, XmlMapping, attribute, from_data
from pyxsd.xdm import XdmMap
from pyxsd.xdm.annotations import UNTYPED, UNTYPED_ATOMIC
from pyxsd.xdm.errors import XdmInputError
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.processor import XsltProcessor


def test_projected_xdm_tracks_lexical_xml_not_descriptor_annotations():
    class OrderView:
        __slots__ = ()
        id = attribute("id", type="xs:integer")

    nodes = NodeMapping()
    nodes.register(OrderView, path="/order")
    rules = {
        "/before": MappingRule(kind="text"),
        "/line": MappingRule(kind="element", namespace="urn:orders", prefix="o", type="xs:integer"),
        "/after": MappingRule(kind="text"),
    }
    source = from_data({"id": "007", "before": "pre", "line": "001", "after": "post"})
    doc = source.to_xml(XmlMapping.compact("order", rules=rules), node_mapping=nodes)
    xdm = doc.to_xdm()
    root = xdm.root_element
    assert doc.to_xdm() is xdm
    assert doc.source is None and doc.source_index is None
    assert doc.root.id == 7
    assert root.annotation.type_name == UNTYPED
    assert root.attributes[0].annotation.type_name == UNTYPED_ATOMIC
    assert root.attributes[0].string_value == "007"
    lead, child, tail = root.children
    assert (lead.kind, lead.string_value, tail.kind, tail.string_value) == (
        NodeKind.TEXT,
        "pre",
        NodeKind.TEXT,
        "post",
    )
    assert (child.name, child.annotation.type_name, child.string_value) == (
        "{urn:orders}line",
        UNTYPED,
        "1",
    )
    doc.root.children[0].set_text("2")
    with pytest.raises(XdmInputError, match="revalidate"):
        doc.to_xdm()
    assert doc.revalidate().to_xdm().root_element.string_value == "pre2post"
    assert source.value_at("/line") == "001"


def test_xslt_2_node_axes_work_on_the_projection_but_not_on_the_native_map():
    sheet = XsltProcessor(language="2.0", xpath="2.0").compile(
        io.BytesIO(b"""
          <xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="2.0">
            <xsl:output method="text"/>
            <xsl:param name="native"/>
            <xsl:template match="/" name="main">
              <xsl:value-of select="child::order/@id"/>
            </xsl:template>
          </xsl:stylesheet>
        """)
    )
    source = from_data({"id": 7})
    xml = source.to_xml(XmlMapping.compact("order"))
    assert sheet.transform(xml).serialize() == "7"
    native = source.to_xdm()
    assert isinstance(native[0], XdmMap)
    assert sheet.transform(native).serialize() == ""
    with pytest.raises(XsltDynamicError) as caught:
        sheet.transform(parameters={"native": native}, initial_template="main")
    assert caught.value.code == "XPTY0004"
