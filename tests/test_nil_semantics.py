"""xsi:nil semantics: empty nil is valid, content-bearing nil is not.

Nilled elements used to be handled inconsistently: a nilled complex
element still validated its normal content model (so a valid empty nil
was rejected for missing required children), while a content-bearing
nil was accepted silently. These tests pin the corrected, centralized
behavior.
"""

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD

XSI_DECL = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'

COMPLEX_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="vType">
    <xs:sequence>
      <xs:element name="a" type="xs:integer"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="r">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="v" type="vType" nillable="true"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

PRIMITIVE_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="v" type="xs:integer" nillable="true"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

ROOT_COMPLEX_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="rType">
    <xs:sequence>
      <xs:element name="a" type="xs:integer"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="r" type="rType" nillable="true"/>
</xs:schema>
"""

ROOT_PRIMITIVE_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r" type="xs:integer" nillable="true"/>
</xs:schema>
"""


def parse(tmp_path, instance_xml, schema_xml, mode=ParseModes.STRICT):
    (tmp_path / "instance.xml").write_text(instance_xml)
    (tmp_path / "schema.xsd").write_text(schema_xml)
    return PyXSD(
        str(tmp_path / "instance.xml"),
        str(tmp_path / "schema.xsd"),
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=mode,
    )


def codes(parser):
    return [issue.code for issue in parser.report.issues]


def test_empty_nilled_complex_child_binds_clean(tmp_path):
    """A valid empty nil of a complex type needs no content validation."""
    parser = parse(
        tmp_path,
        f'<r {XSI_DECL}><v xsi:nil="true"/></r>',
        COMPLEX_SCHEMA,
    )
    assert not parser.report.has_errors
    v = parser.schemaRootInstance._children_[0]
    assert v._nil_ is True
    assert v._children_ == []


def test_nilled_complex_child_with_children_is_reported(tmp_path):
    """A nilled element may not carry element content."""
    parser = parse(
        tmp_path,
        f'<r {XSI_DECL}><v xsi:nil="true"><a>7</a></v></r>',
        COMPLEX_SCHEMA,
    )
    assert "nil" in codes(parser)
    v = parser.schemaRootInstance._children_[0]
    assert v._children_ == []


def test_content_bearing_nilled_primitive_is_reported(tmp_path):
    """A nilled element may not carry character content."""
    parser = parse(
        tmp_path,
        f'<r {XSI_DECL}><v xsi:nil="true">7</v></r>',
        PRIMITIVE_SCHEMA,
    )
    assert "nil" in codes(parser)
    v = parser.schemaRootInstance._children_[0]
    assert v._value_ is None


def test_empty_nilled_primitive_still_binds(tmp_path):
    parser = parse(
        tmp_path,
        f'<r {XSI_DECL}><v xsi:nil="true"/></r>',
        PRIMITIVE_SCHEMA,
    )
    assert not parser.report.has_errors
    v = parser.schemaRootInstance._children_[0]
    assert v._nil_ is True
    assert v._value_ is None


def test_nilled_child_attribute_validation_continues(tmp_path):
    """Attributes are still validated on a nilled element."""
    schema = PRIMITIVE_SCHEMA.replace(
        '<xs:element name="v" type="xs:integer" nillable="true"/>',
        '<xs:element name="v" type="xs:integer" nillable="true">'
        '<xs:complexType><xs:simpleContent><xs:extension base="xs:integer">'
        '<xs:attribute name="id" type="xs:string" use="required"/>'
        "</xs:extension></xs:simpleContent></xs:complexType>"
        "</xs:element>",
    )
    parser = parse(
        tmp_path,
        f'<r {XSI_DECL}><v xsi:nil="true"/></r>',
        schema,
    )
    assert "missing-attribute" in codes(parser)


def test_root_primitive_nilled_with_text_is_reported(tmp_path):
    """A nilled root of primitive type rejects character content."""
    parser = parse(
        tmp_path,
        f'<r {XSI_DECL} xsi:nil="true">not an int</r>',
        ROOT_PRIMITIVE_SCHEMA,
    )
    assert "nil" in codes(parser)


def test_root_primitive_nilled_empty_is_clean(tmp_path):
    parser = parse(
        tmp_path,
        f'<r {XSI_DECL} xsi:nil="true"/>',
        ROOT_PRIMITIVE_SCHEMA,
    )
    assert not parser.report.has_errors


def test_root_complex_nilled_empty_is_clean(tmp_path):
    """A nilled root of complex type skips content validation."""
    parser = parse(
        tmp_path,
        f'<r {XSI_DECL} xsi:nil="true"/>',
        ROOT_COMPLEX_SCHEMA,
    )
    assert not parser.report.has_errors
    assert parser.schemaRootInstance._nil_ is True
    assert parser.schemaRootInstance._children_ == []


def test_root_complex_nilled_with_children_is_reported(tmp_path):
    parser = parse(
        tmp_path,
        f'<r {XSI_DECL} xsi:nil="true"><a>7</a></r>',
        ROOT_COMPLEX_SCHEMA,
    )
    assert "nil" in codes(parser)


def test_nilled_root_with_fixed_value_is_reported(tmp_path):
    """xsi:nil and a declaration's fixed value cannot both apply."""
    schema = ROOT_PRIMITIVE_SCHEMA.replace('nillable="true"', 'nillable="true" fixed="7"')
    nilled = parse(tmp_path, f'<r {XSI_DECL} xsi:nil="true"/>', schema)
    assert "nil" in codes(nilled)

    matching = parse(tmp_path, f"<r {XSI_DECL}>7</r>", schema)
    assert not matching.report.has_errors


def test_nilled_child_with_fixed_value_is_reported(tmp_path):
    """The same fixed conflict is reported for nilled children."""
    schema = PRIMITIVE_SCHEMA.replace('nillable="true"', 'nillable="true" fixed="7"')
    parser = parse(tmp_path, f'<r {XSI_DECL}><v xsi:nil="true"/></r>', schema)
    assert "nil" in codes(parser)


def test_whitespace_only_nilled_root_is_reported(tmp_path):
    """Whitespace is character content; a nilled element must be empty."""
    parser = parse(tmp_path, f'<r {XSI_DECL} xsi:nil="true"> </r>', ROOT_PRIMITIVE_SCHEMA)
    assert "nil" in codes(parser)


def test_whitespace_only_nilled_child_is_reported(tmp_path):
    parser = parse(
        tmp_path,
        f'<r {XSI_DECL}><v xsi:nil="true"> </v></r>',
        COMPLEX_SCHEMA,
    )
    assert "nil" in codes(parser)
