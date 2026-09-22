"""Task 7: public document XPath / ElementPath facade.

Queries run over a projection of the bound tree and return the *original
bound nodes*, so callers can keep using the object model. The projection
reflects the writer containers (post-write-through), which is the query
contract documented on ``Document.xpath``/``find``/``findall``.
"""

from io import StringIO

import pytest

import pyxsd
from pyxsd.xsd_data_types import Int

SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" type="xs:int" maxOccurs="unbounded"/>
        <xs:element name="count" type="xs:int"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

INSTANCE = "<root><item>1</item><item>2</item><item>3</item><count>7</count></root>"

NS_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:x" xmlns:t="urn:x" elementFormDefault="qualified">
  <xs:element name="root" type="t:Root"/>
  <xs:complexType name="Root">
    <xs:sequence>
      <xs:element name="item" type="xs:int"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>"""

NS_INSTANCE = '<root xmlns="urn:x"><item>1</item></root>'


def _doc(schema=SCHEMA, instance=INSTANCE):
    return pyxsd.Schema.compile(StringIO(schema)).parse(StringIO(instance))


def test_xpath_positional_returns_bound_nodes():
    doc = _doc()
    (node,) = doc.xpath("/root/item[2]")
    assert int(node) == 2
    assert node is doc.xpath("/root/item[position() = 2]")[0]


def test_xpath_scalar_results():
    doc = _doc()
    assert doc.xpath("count(/root/item)") == 3


def test_find_and_findall():
    doc = _doc()
    nodes = doc.findall("item")
    assert [int(n) for n in nodes] == [1, 2, 3]
    assert int(doc.find("count")) == 7


def test_namespaced_query():
    doc = _doc(NS_SCHEMA, NS_INSTANCE)
    (node,) = doc.xpath("/t:root/t:item", namespaces={"t": "urn:x"})
    assert int(node) == 1
    assert node._descriptor_.getNamespace() == "urn:x"
    assert int(doc.find("{urn:x}item")) == 1


def test_mutation_visible_to_queries():
    doc = _doc()
    doc.root.count = Int(9)  # single occurrence: Task 1 write-through applies
    assert doc.find("count")._value_ == "9"


def test_no_root_raises():
    doc = _doc(instance="<other><item>1</item></other>")
    assert doc.root is None
    with pytest.raises(pyxsd.PyXSDError):
        doc.xpath("/root/item")
    with pytest.raises(pyxsd.PyXSDError):
        doc.findall("item")
