"""Edge-case writer tests from the compatibility review.

Two serialization gaps remain after the earlier escaping work:

* an unqualified tree carrying an attribute in the reserved XML
  namespace (``xml:space``, ``xml:lang``) was rendered with its Clark
  name, which is not well-formed XML;
* carriage returns in element text were written literally, so XML
  end-of-line normalization changed them when the output was read
  back.

The CLI error-boundary tests live in ``test_cli.py``.
"""

import io
import xml.etree.ElementTree as ET

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD
from pyxsd.writers import XmlTreeWriter

XML_NS = "http://www.w3.org/XML/1998/namespace"

SCHEMA_WITH_XML_SPACE = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:xml="http://www.w3.org/XML/1998/namespace">
  <xs:element name="r">
    <xs:complexType>
      <xs:attribute ref="xml:space"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

SCHEMA_STRING = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="xs:string"/>
</xs:schema>
"""


def parse(instance_xml, schema_xml):
    """Parse an in-memory instance and return the parser."""
    return PyXSD(
        io.StringIO(instance_xml),
        io.StringIO(schema_xml),
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
    )


def write(root):
    """Serialize a parsed tree and return the text."""
    out = io.StringIO()
    XmlTreeWriter(root, out)
    return out.getvalue()


def test_unqualified_tree_with_xml_space_is_well_formed():
    """The reserved xml prefix is used even without Clark element names."""
    parser = parse('<r xml:space="preserve"/>', SCHEMA_WITH_XML_SPACE)
    assert [i.code for i in parser.report] == []
    root = parser.schemaRootInstance
    assert root._attribs_[f"{{{XML_NS}}}space"] == "preserve"

    output = write(root)
    assert f"{{{XML_NS}}}space" not in output
    assert 'xml:space = "preserve"' in output

    reparsed = ET.fromstring(output)
    assert reparsed.attrib[f"{{{XML_NS}}}space"] == "preserve"


def test_carriage_return_in_text_round_trips():
    """A literal CR must be written as a character reference."""
    parser = parse("<r>A&#13;B&#13;&#10;C</r>", SCHEMA_STRING)
    assert parser.schemaRootInstance._value_ == ["A\rB\r\nC"]

    output = write(parser.schemaRootInstance)
    assert "&#13;" in output

    reparsed = ET.fromstring(output)
    assert reparsed.text == "A\rB\r\nC"
