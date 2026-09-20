"""Writer correctness tests: escaping and text preservation.

Covers the serialization defects where values containing XML special
characters produced malformed output, and where pretty-printing inserted
layout whitespace inside text-only elements.
"""

import io
import xml.etree.ElementTree as ET

from conftest import canonicalize
from pyxsd.schema import Schema
from pyxsd.writers import XmlTreeWriter

SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="xs:string"/>
</xs:schema>
"""

SCHEMA_WITH_ATTR = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:attribute name="note" type="xs:string"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def write_instance(instance_xml, schema_xml=SCHEMA, tmp_path=None):
    """Parse ``instance_xml`` in memory and return the writer output."""
    directory = tmp_path
    (directory / "instance.xml").write_text(instance_xml)
    (directory / "schema.xsd").write_text(schema_xml)
    doc = Schema.compile(str(directory / "schema.xsd")).parse(str(directory / "instance.xml"))
    out = io.StringIO()
    XmlTreeWriter(doc.root, out)
    return out.getvalue()


def reparsed_root(text):
    """Parse writer output, proving it is well-formed XML."""
    return ET.fromstring(text)


def test_special_characters_round_trip(tmp_path):
    """Ampersands and angle brackets in text survive the write cycle."""
    output = write_instance("<r>A &amp; B &lt; C</r>", tmp_path=tmp_path)
    root = reparsed_root(output)
    assert root.text == "A & B < C"
    assert "&amp;" in output
    assert "&lt;" in output


def test_attribute_values_are_escaped(tmp_path):
    """Ampersands, quotes, and angle brackets in attributes round-trip."""
    instance = '<r note="A &amp; B &quot;C&quot; &lt;tag&gt;">plain</r>'
    output = write_instance(instance, schema_xml=SCHEMA_WITH_ATTR, tmp_path=tmp_path)
    root = reparsed_root(output)
    assert root.attrib["note"] == 'A & B "C" <tag>'


def test_text_only_element_is_written_inline(tmp_path):
    """No layout whitespace is injected inside a text-only element."""
    output = write_instance("<r>Hello</r>", tmp_path=tmp_path)
    assert "<r>Hello</r>" in output
    assert "\n   Hello" not in output


def test_padded_text_is_preserved(tmp_path):
    """Leading and trailing spaces in a value are not disturbed."""
    output = write_instance("<r>  padded  </r>", tmp_path=tmp_path)
    root = reparsed_root(output)
    assert root.text == "  padded  "


def test_multiline_value_is_preserved(tmp_path):
    """A multi-line value keeps its embedded newlines exactly."""
    value = "first line\nsecond line\nthird line"
    output = write_instance(f"<r>{value}</r>", tmp_path=tmp_path)
    root = reparsed_root(output)
    assert root.text == value


def test_structured_elements_keep_indentation(tmp_path):
    """Elements with children are still pretty-printed."""
    schema = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
        <xs:element name="b" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
    instance = "<r><a>x &amp; y</a><b>z</b></r>"
    output = write_instance(instance, schema_xml=schema, tmp_path=tmp_path)
    root = reparsed_root(output)
    assert root.find("a").text == "x & y"
    # Children remain on their own indented lines.
    assert "\n    <a>" in output
    assert "\n    <b>" in output


def test_canonicalize_preserves_leaf_text():
    """Canonical comparison keeps exact leaf text (no stripping)."""
    element = ET.fromstring("<r>  hi  </r>")
    assert canonicalize(element)[2] == "  hi  "


def test_canonicalize_strips_parent_layout_text():
    """Whitespace between a parent's tags is still treated as layout."""
    element = ET.fromstring("<r>\n    <a>1</a>\n</r>")
    assert canonicalize(element)[2] == ""


def test_token_value_normalization_is_visible(tmp_path):
    """xs:token strips surrounding whitespace when the value is bound.

    The write path reproduces the bound value verbatim; normalization
    happens once, at parse time (documented XSD semantics), instead of
    being hidden by comparison helpers.
    """
    schema = SCHEMA.replace('type="xs:string"', 'type="xs:token"')
    output = write_instance("<r>  padded  </r>", schema_xml=schema, tmp_path=tmp_path)
    root = reparsed_root(output)
    assert root.text == "padded"
