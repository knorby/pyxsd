"""Same-named element and attribute declarations must not collide.

A complex type may legally declare an element and an attribute with
the same name. The generated class keeps both descriptors: the
attribute retains the natural accessor (``item.code``) and the element
is re-keyed (``item.code_element``, numeric suffix when that is
taken). Matching and binding keep using declaration names, so no
child is silently dropped and identity constraints still run.
"""

from pyxsd.parser import PyXSD

SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="itemType">
    <xs:sequence>
      <xs:element name="code" type="xs:int"/>
    </xs:sequence>
    <xs:attribute name="code" type="xs:string"/>
  </xs:complexType>
  <xs:element name="items">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" type="itemType" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

IDENTITY_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="itemType">
    <xs:sequence>
      <xs:element name="code" type="xs:int"/>
    </xs:sequence>
    <xs:attribute name="code" type="xs:string"/>
  </xs:complexType>
  <xs:element name="items">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" type="itemType" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
    <xs:unique name="codeUnique">
      <xs:selector xpath="item"/>
      <xs:field xpath="code"/>
    </xs:unique>
  </xs:element>
</xs:schema>
"""

SUFFIX_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="itemType">
    <xs:sequence>
      <xs:element name="code" type="xs:int"/>
      <xs:element name="code_element" type="xs:int"/>
    </xs:sequence>
    <xs:attribute name="code" type="xs:string"/>
  </xs:complexType>
  <xs:element name="items">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" type="itemType"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def parse(schema_xml, instance_xml, tmp_path):
    (tmp_path / "instance.xml").write_text(instance_xml)
    (tmp_path / "schema.xsd").write_text(schema_xml)
    return PyXSD(
        str(tmp_path / "instance.xml"),
        str(tmp_path / "schema.xsd"),
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
    )


def test_element_and_attribute_same_name_both_bind(tmp_path):
    """The element child is bound and validated, not silently dropped."""
    instance = "<items><item code='external'><code>7</code></item></items>"
    parser = parse(SCHEMA, instance, tmp_path)
    assert not parser.report.has_errors
    items = parser.schemaRootInstance
    item = items._children_[0]
    # The attribute keeps the natural accessor.
    assert item.code == "external"
    # The element is bound under its aliased accessor and validated.
    assert item.code_element == 7
    assert item._children_[0]._value_ == ["7"]


def test_invalid_element_value_is_reported_despite_collision(tmp_path):
    """Lexical validation of the same-named element is not bypassed."""
    instance = "<items><item code='external'><code>NaN</code></item></items>"
    parser = parse(SCHEMA, instance, tmp_path)
    codes = [issue.code for issue in parser.report]
    assert "value" in codes


def test_identity_constraint_sees_element_despite_collision(tmp_path):
    """xs:unique over the same-named element field actually evaluates."""
    instance = (
        "<items>"
        "<item code='external'><code>7</code></item>"
        "<item code='internal'><code>7</code></item>"
        "</items>"
    )
    parser = parse(IDENTITY_SCHEMA, instance, tmp_path)
    codes = [issue.code for issue in parser.report]
    assert "identity-unique" in codes


def test_identity_constraint_accepts_distinct_element_values(tmp_path):
    instance = (
        "<items>"
        "<item code='external'><code>7</code></item>"
        "<item code='internal'><code>9</code></item>"
        "</items>"
    )
    parser = parse(IDENTITY_SCHEMA, instance, tmp_path)
    assert not parser.report.has_errors


def test_alias_gets_numeric_suffix_when_taken(tmp_path):
    """``<name>_element`` occupied by a real element gets a suffix."""
    instance = "<items><item code='x'><code>1</code><code_element>2</code_element></item></items>"
    parser = parse(SUFFIX_SCHEMA, instance, tmp_path)
    assert not parser.report.has_errors
    item = parser.schemaRootInstance._children_[0]
    assert item.code == "x"
    # The element actually named 'code_element' keeps its natural alias.
    assert item.code_element == 2
    # The element named 'code' moved to the next free suffixed key.
    assert item.code_element_2 == 1


def test_descriptor_bookkeeping_reflects_both_declarations(tmp_path):
    instance = "<items><item code='x'><code>1</code></item></items>"
    parser = parse(SCHEMA, instance, tmp_path)
    itemType = parser.getClasses()["itemType"]
    assert "code" in itemType._attributeNames_
    assert "code_element" in itemType._elementNames_
