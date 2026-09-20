"""Declaration bookkeeping across inheritance and repeated uses.

A derived type may collide with an inherited declaration, and a group
may be referenced more than once. Every declaration must stay visible:
no child may be silently dropped or mis-bound because a Python accessor
key was reused, and repeated uses must bind every occurrence.
"""

from pyxsd.schema import Schema

BASE_ELEMENT = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="B">
    <xs:sequence>
      <xs:element name="%(name)s" type="xs:int"/>
    </xs:sequence>
  </xs:complexType>
"""


def _derived_schema(body):
    return (
        BASE_ELEMENT % {"name": body.get("base_element", "code_element")}
        + """  <xs:complexType name="D">
    <xs:complexContent>
      <xs:extension base="B">
        <xs:sequence>
"""
        + body["sequence"]
        + """        </xs:sequence>
        <xs:attribute name="code" type="xs:string"/>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="r" type="D"/>
</xs:schema>
"""
    )


def parse(schema_xml, instance_xml, tmp_path):
    (tmp_path / "instance.xml").write_text(instance_xml)
    (tmp_path / "schema.xsd").write_text(schema_xml)
    return Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))


def codes(doc):
    return [issue.code for issue in doc.report]


def test_inherited_element_survives_alias_allocation(tmp_path):
    """The derived alias must not shadow an inherited element.

    The derived element ``code`` collides with the derived attribute
    ``code`` and must move to ``code_element_2`` because ``code_element``
    is a real inherited element.
    """
    schema = _derived_schema({"sequence": '          <xs:element name="code" type="xs:int"/>\n'})
    valid = '<r code="x"><code_element>5</code_element><code>7</code></r>'
    doc = parse(schema, valid, tmp_path)
    assert not doc.report.has_errors
    root = doc.root
    assert [child._name_ for child in root._children_] == ["code_element", "code"]
    assert root.code == "x"
    assert root.code_element == 5
    assert root.code_element_2 == 7


def test_invalid_inherited_element_is_still_validated(tmp_path):
    """Lexical validation of the inherited child is not bypassed.

    The invalid primitive is not bound (the documented invalid-value
    policy), but the report must carry the error; previously the
    inherited declaration was shadowed away and no error appeared.
    """
    schema = _derived_schema({"sequence": '          <xs:element name="code" type="xs:int"/>\n'})
    invalid = '<r code="x"><code_element>bad</code_element><code>7</code></r>'
    doc = parse(schema, invalid, tmp_path)
    assert "value" in codes(doc)
    root = doc.root
    assert [child._name_ for child in root._children_] == ["code"]
    assert root.code_element_2 == 7


def test_inherited_element_and_derived_attribute_collision(tmp_path):
    """Base element ``code`` + derived attribute ``code`` both bind."""
    schema = _derived_schema({"base_element": "code", "sequence": ""})
    instance = '<r code="a"><code>7</code></r>'
    doc = parse(schema, instance, tmp_path)
    assert not doc.report.has_errors
    root = doc.root
    assert root.code == "a"
    assert root.code_element == 7
    assert root._attribs_["code"] == "a"


def test_derived_element_and_inherited_attribute_collision(tmp_path):
    """Base attribute ``code`` + derived element ``code`` both bind.

    Binding must reach the declaration's own descriptor; a plain
    ``setattr`` for the element would find the inherited attribute
    descriptor first and raise ``TypeError``.
    """
    schema = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="B">
    <xs:attribute name="code" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="D">
    <xs:complexContent>
      <xs:extension base="B">
        <xs:sequence>
          <xs:element name="code" type="xs:int"/>
        </xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="r" type="D"/>
</xs:schema>
"""
    instance = '<r code="a"><code>7</code></r>'
    doc = parse(schema, instance, tmp_path)
    assert not doc.report.has_errors
    root = doc.root
    assert root.code == "a"
    assert root.code_element == 7


def test_alias_avoids_both_attributes_and_elements_named_element(tmp_path):
    """``code_element`` may be an attribute name too; use the next suffix."""
    schema = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="itemType">
    <xs:sequence>
      <xs:element name="code" type="xs:int"/>
    </xs:sequence>
    <xs:attribute name="code" type="xs:string"/>
    <xs:attribute name="code_element" type="xs:string"/>
  </xs:complexType>
  <xs:element name="item" type="itemType"/>
</xs:schema>
"""
    instance = '<item code="x" code_element="y"><code>1</code></item>'
    doc = parse(schema, instance, tmp_path)
    assert not doc.report.has_errors
    item = doc.root
    assert item.code == "x"
    assert item.code_element == "y"
    assert item.code_element_2 == 1


def test_repeated_group_use_binds_every_occurrence(tmp_path):
    """A declaration used once scalar and once repeated still binds a list."""
    schema = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:group name="G">
    <xs:sequence>
      <xs:element name="a" type="xs:int"/>
    </xs:sequence>
  </xs:group>
  <xs:element name="r">
    <xs:complexType>
      <xs:sequence>
        <xs:group ref="G"/>
        <xs:group ref="G" minOccurs="2" maxOccurs="2"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
    instance = "<r><a>1</a><a>2</a><a>3</a></r>"
    doc = parse(schema, instance, tmp_path)
    assert not doc.report.has_errors
    root = doc.root
    assert root.a == [1, 2, 3]
    assert [child._value_ for child in root._children_] == [["1"], ["2"], ["3"]]
