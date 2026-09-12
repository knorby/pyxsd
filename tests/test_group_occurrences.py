"""Group references must not mutate the shared group declarations.

A named group's element representatives are shared by every type that
references the group. Folding a reference site's occurrence limits
onto those shared representatives made validation depend on schema
declaration order: a ``minOccurs="0"`` reference silently relaxed
every other reference to the same group. Each reference now works on
per-use copies; the group's declarations stay immutable.
"""

from pyxsd.parser import PyXSD

SCHEMA_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  {declarations}
  <xs:group name="g">
    <xs:sequence>
      <xs:element name="a" type="xs:int"/>
    </xs:sequence>
  </xs:group>
  <xs:complexType name="Optional">
    <xs:group ref="g" minOccurs="0"/>
  </xs:complexType>
  <xs:complexType name="Required">
    <xs:group ref="g"/>
  </xs:complexType>
  <xs:element name="r" type="Required"/>
</xs:schema>
"""

OPTIONAL_FIRST = SCHEMA_TEMPLATE.format(declarations="<!-- optional type declared first -->")

# Same schema with a decoy declaration so textual order differs; the
# group definition's position relative to the referencing types is
# what the old bug was sensitive to.
NESTED_GROUPS_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:group name="inner">
    <xs:sequence>
      <xs:element name="a" type="xs:int" minOccurs="2" maxOccurs="2"/>
    </xs:sequence>
  </xs:group>
  <xs:group name="outer">
    <xs:sequence>
      <xs:group ref="inner"/>
    </xs:sequence>
  </xs:group>
  <xs:complexType name="Twice">
    <xs:group ref="outer" minOccurs="1"/>
  </xs:complexType>
  <xs:complexType name="Once">
    <xs:group ref="inner" maxOccurs="1"/>
  </xs:complexType>
  <xs:element name="r" type="Twice"/>
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


def test_optional_reference_does_not_relax_required_reference(tmp_path):
    """A minOccurs=0 group reference must not relax a plain reference."""
    parser = parse(OPTIONAL_FIRST, "<r/>", tmp_path)
    codes = [issue.code for issue in parser.report]
    assert "occurrence-min" in codes


def test_required_reference_still_accepts_one_child(tmp_path):
    parser = parse(OPTIONAL_FIRST, "<r><a>1</a></r>", tmp_path)
    assert not parser.report.has_errors


def test_required_reference_rejects_two_children(tmp_path):
    parser = parse(OPTIONAL_FIRST, "<r><a>1</a><a>2</a></r>", tmp_path)
    assert parser.report.has_errors


def _descriptor_mins(typeClass):
    """minOccurs of every element descriptor installed on a class."""
    mins = []
    for name in typeClass._elementNames_:
        descriptor = typeClass.__dict__[name]
        if hasattr(descriptor, "getMinOccurs"):
            mins.append(descriptor.getMinOccurs())
    return sorted(mins)


def test_group_element_declarations_stay_unmutated(tmp_path):
    """Each type sees its own folded limits; no cross-type leakage."""
    parser = parse(OPTIONAL_FIRST, "<r><a>1</a></r>", tmp_path)
    classes = parser.getClasses()
    # Required's reference folds to min 1; Optional's folds to min 0.
    assert _descriptor_mins(classes["Required"]) == [1]
    assert _descriptor_mins(classes["Optional"]) == [0]


def test_nested_group_occurrence_product(tmp_path):
    """Occurrence limits multiply through nested group references."""
    parser = parse(NESTED_GROUPS_SCHEMA, "<r><a>1</a><a>2</a></r>", tmp_path)
    assert not parser.report.has_errors
    parser = parse(NESTED_GROUPS_SCHEMA, "<r><a>1</a></r>", tmp_path)
    codes = [issue.code for issue in parser.report]
    assert "occurrence-min" in codes
