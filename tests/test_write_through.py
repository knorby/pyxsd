"""F1/F2: descriptor assignment must reach the writer's containers.

F1 (review finding / tracker R8): ``root.attr = value`` validated but the
value was silently dropped by ``to_string()``/``write()``, because
descriptors stored only in ``obj.__dict__`` while the writer serializes
the lexical containers built at parse time.

F2: element assignment demanded a typed instance while attribute
assignment coerced plain values; the policies are unified here.
"""

import xml.etree.ElementTree as ET
from io import StringIO

import pytest

import pyxsd
from pyxsd.xsd_data_types import Boolean, Double, Int

SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="count" type="xs:int"/>
        <xs:element name="flag" type="xs:boolean"/>
      </xs:sequence>
      <xs:attribute name="a" type="xs:int"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

INSTANCE = '<root a="1"><count>7</count><flag>true</flag></root>'


@pytest.fixture()
def doc():
    return pyxsd.Schema.compile(StringIO(SCHEMA)).parse(StringIO(INSTANCE))


def _root_of(doc):
    """The serialized output parsed back, so assertions ignore spacing."""
    return ET.fromstring(doc.to_string())


def test_lexical_spellings():
    assert Int(42).lexical() == "42"
    assert Boolean(1).lexical() == "true"
    assert Boolean(0).lexical() == "false"
    assert Double("1.5").lexical() == "1.5"
    assert Double("INF").lexical() == "INF"
    assert Double("-INF").lexical() == "-INF"
    assert Double("NaN").lexical() == "NaN"


def test_attribute_assignment_survives_serialization(doc):
    doc.root.a = 2
    assert _root_of(doc).get("a") == "2"


def test_element_assignment_survives_serialization(doc):
    doc.root.count = Int(42)
    assert _root_of(doc).findtext("count") == "42"


def test_boolean_assignment_uses_xsd_lexical(doc):
    doc.root.flag = Boolean(0)
    assert _root_of(doc).findtext("flag") == "false"


def test_assignment_visible_in_revalidate(doc):
    doc.root.count = Int(9)
    out = doc.revalidate()
    assert ET.fromstring(out.to_string()).findtext("count") == "9"


def test_repeated_element_assignment_does_not_corrupt(doc):
    # The schema declares a single occurrence; write-through must not
    # duplicate the child when the descriptor is assigned.
    doc.root.count = Int(5)
    assert len(_root_of(doc).findall("count")) == 1
