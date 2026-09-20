"""Wildcard (xs:any) particles: order and occurrence are enforced.

Wildcards used to bypass the content model entirely: any child a
wildcard's namespace constraint admitted could appear in any number, in
any position, or not at all. These tests pin the corrected behavior:
wildcard particles participate in matching like any other particle.
"""

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema

NS_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:t" xmlns:t="urn:t"
           elementFormDefault="qualified">
  <xs:element name="r">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
        <xs:any namespace="urn:o" processContents="skip"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

DECLS = 'xmlns:t="urn:t" xmlns:o="urn:o"'


def parse(tmp_path, instance_xml, schema_xml=NS_SCHEMA, mode=ParseModes.NAMESPACED):
    (tmp_path / "instance.xml").write_text(instance_xml)
    (tmp_path / "schema.xsd").write_text(schema_xml)
    return Schema.compile(str(tmp_path / "schema.xsd"), mode=mode).parse(
        str(tmp_path / "instance.xml")
    )


def test_wildcard_child_in_place_parses_clean(tmp_path):
    doc = parse(
        tmp_path,
        f"<t:r {DECLS}><t:a>x</t:a><o:x/></t:r>",
    )
    assert not doc.report.has_errors
    assert len(doc.root._children_) == 2


def test_missing_wildcard_occurrence_is_reported(tmp_path):
    doc = parse(tmp_path, f"<t:r {DECLS}><t:a>x</t:a></t:r>")
    assert doc.report.has_errors
    assert "occurrence-min" in [issue.code for issue in doc.report]


def test_excess_wildcard_content_is_reported(tmp_path):
    doc = parse(
        tmp_path,
        f"<t:r {DECLS}><t:a>x</t:a><o:x/><o:y/></t:r>",
    )
    assert doc.report.has_errors


def test_wildcard_before_required_element_is_reported(tmp_path):
    doc = parse(
        tmp_path,
        f"<t:r {DECLS}><o:x/><t:a>x</t:a></t:r>",
    )
    assert doc.report.has_errors


def test_optional_wildcard_needs_no_content(tmp_path):
    schema = NS_SCHEMA.replace(
        '<xs:any namespace="urn:o" processContents="skip"/>',
        '<xs:any namespace="urn:o" processContents="skip" minOccurs="0"/>',
    )
    doc = parse(tmp_path, f"<t:r {DECLS}><t:a>x</t:a></t:r>", schema_xml=schema)
    assert not doc.report.has_errors


def test_unbounded_wildcard_repeats(tmp_path):
    schema = NS_SCHEMA.replace(
        '<xs:any namespace="urn:o" processContents="skip"/>',
        '<xs:any namespace="urn:o" processContents="skip" maxOccurs="unbounded"/>',
    )
    doc = parse(
        tmp_path,
        f"<t:r {DECLS}><t:a>x</t:a><o:x/><o:y/></t:r>",
        schema_xml=schema,
    )
    assert not doc.report.has_errors
    assert len(doc.root._children_) == 3


def test_declared_element_wins_over_same_namespace_wildcard(tmp_path):
    """A declared particle always takes precedence over a wildcard."""
    schema = NS_SCHEMA.replace('namespace="urn:o"', 'namespace="##any"')
    doc = parse(
        tmp_path,
        f"<t:r {DECLS}><t:a>x</t:a><o:x/></t:r>",
        schema_xml=schema,
    )
    assert not doc.report.has_errors
    # The declared particle bound the a element, not the wildcard.
    assert doc.root._children_[0]._name_ == "{urn:t}a"


def test_legacy_mode_still_binds_undeclared_children(tmp_path):
    """Legacy mode keeps treating any undeclared child as wildcard content."""
    legacy_schema = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
        <xs:any processContents="lax" minOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
    doc = parse(
        tmp_path,
        "<r><a>x</a><whatever/></r>",
        schema_xml=legacy_schema,
        mode=ParseModes.STRICT,
    )
    assert not doc.report.has_errors
    assert len(doc.root._children_) == 2


def test_legacy_mode_excess_wildcard_content_is_reported(tmp_path):
    """Even in legacy mode, exceeding a wildcard's occurrence limit errors."""
    legacy_schema = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="r">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
        <xs:any processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
    doc = parse(
        tmp_path,
        "<r><a>x</a><one/><two/></r>",
        schema_xml=legacy_schema,
        mode=ParseModes.STRICT,
    )
    assert doc.report.has_errors


# --- XSD 1.1 exclusions reach element wildcard admission ---------------------

NOT_NAMESPACE_ELEMENT_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="eden">
    <xs:complexType>
      <xs:sequence>
        <xs:any notNamespace="http://apple.com/ http://devil.com/"
                processContents="skip" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def test_not_namespace_element_wildcard_rejects_the_excluded_namespace(tmp_path):
    # wild002.n1
    doc = parse(
        tmp_path,
        '<eden xmlns:evil="http://devil.com/"><adam/><evil:eve/></eden>',
        schema_xml=NOT_NAMESPACE_ELEMENT_SCHEMA,
    )
    assert doc.report.has_errors
    doc = parse(
        tmp_path,
        '<eden xmlns:c="http://genesis.com/"><adam/><c:cain/></eden>',
        schema_xml=NOT_NAMESPACE_ELEMENT_SCHEMA,
    )
    assert not doc.report.has_errors


NOT_QNAME_ELEMENT_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="eden">
    <xs:complexType>
      <xs:sequence>
        <xs:any notQName="xml:space" processContents="skip"
                minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def test_not_qname_element_wildcard_rejects_an_exact_expanded_name(tmp_path):
    # wild028.n1
    doc = parse(
        tmp_path,
        "<eden><adam/><xml:space/></eden>",
        schema_xml=NOT_QNAME_ELEMENT_SCHEMA,
    )
    assert doc.report.has_errors
    doc = parse(
        tmp_path,
        "<eden><adam/><eve/></eden>",
        schema_xml=NOT_QNAME_ELEMENT_SCHEMA,
    )
    assert not doc.report.has_errors


POSITIONAL_NOT_QNAME_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="eden">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
        <xs:element name="a" type="xs:string"/>
        <xs:element name="a" type="xs:string"/>
        <xs:any notQName="a" processContents="skip" minOccurs="1" maxOccurs="1"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def test_excluded_name_cannot_fill_a_wildcard_occurrence(tmp_path):
    # wild029.n1
    doc = parse(
        tmp_path,
        "<eden><a/><a/><a/><a/></eden>",
        schema_xml=POSITIONAL_NOT_QNAME_SCHEMA,
    )
    assert doc.report.has_errors
    doc = parse(
        tmp_path,
        "<eden><a/><a/><a/><b/></eden>",
        schema_xml=POSITIONAL_NOT_QNAME_SCHEMA,
    )
    assert not doc.report.has_errors


DEFINED_ELEMENT_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="zing">
    <xs:all>
      <xs:element name="name" type="xs:string"/>
      <xs:any namespace="##any" notQName="##defined" processContents="skip"/>
    </xs:all>
  </xs:complexType>
  <xs:element name="zing" type="zing"/>
  <xs:element name="zang"/>
</xs:schema>
"""


def test_defined_marker_rejects_global_element_declarations(tmp_path):
    # wild052.n1/n2
    doc = parse(
        tmp_path,
        "<zing><name/><zing/></zing>",
        schema_xml=DEFINED_ELEMENT_SCHEMA,
    )
    assert doc.report.has_errors
    doc = parse(
        tmp_path,
        "<zing><name/><zang/></zing>",
        schema_xml=DEFINED_ELEMENT_SCHEMA,
    )
    assert doc.report.has_errors


def test_defined_marker_does_not_cover_local_declarations(tmp_path):
    # wild052.v2: the local ``name`` declaration is not a top-level one
    doc = parse(
        tmp_path,
        "<zing><name/><name/></zing>",
        schema_xml=DEFINED_ELEMENT_SCHEMA,
    )
    assert not doc.report.has_errors


DEFINED_SIBLING_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="zing"/>
  <xs:complexType name="zing">
    <xs:sequence>
      <xs:element name="a" type="xs:string"/>
      <xs:element name="b" type="xs:string"/>
      <xs:element name="c" type="xs:string"/>
      <xs:any notQName="##definedSibling" processContents="skip"
              maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>
"""


def test_defined_sibling_rejects_a_repeated_sibling(tmp_path):
    # wild070.n1/n2
    doc = parse(
        tmp_path,
        "<root><a/><b/><c/><a/></root>",
        schema_xml=DEFINED_SIBLING_SCHEMA,
    )
    assert doc.report.has_errors
    doc = parse(
        tmp_path,
        "<root><a/><b/><c/><d/><a/></root>",
        schema_xml=DEFINED_SIBLING_SCHEMA,
    )
    assert doc.report.has_errors


def test_defined_sibling_admits_nonsibling_content(tmp_path):
    # wild070.v1
    doc = parse(
        tmp_path,
        "<root><a/><b/><c/><d/><e/></root>",
        schema_xml=DEFINED_SIBLING_SCHEMA,
    )
    assert not doc.report.has_errors


DEFINED_SIBLING_SUBSTITUTION_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="zing"/>
  <xs:complexType name="zing">
    <xs:all>
      <xs:element ref="a" minOccurs="0"/>
      <xs:element name="b" type="xs:string" minOccurs="0"/>
      <xs:any notQName="##definedSibling" processContents="skip"
              minOccurs="0" maxOccurs="unbounded"/>
    </xs:all>
  </xs:complexType>
  <xs:element name="a" type="xs:string"/>
  <xs:element name="A" substitutionGroup="a"/>
</xs:schema>
"""


def test_defined_sibling_includes_substitution_group_members(tmp_path):
    # wild072.n1/n2
    doc = parse(
        tmp_path,
        "<root><a/><a/></root>",
        schema_xml=DEFINED_SIBLING_SUBSTITUTION_SCHEMA,
    )
    assert doc.report.has_errors
    doc = parse(
        tmp_path,
        "<root><a/><A/></root>",
        schema_xml=DEFINED_SIBLING_SUBSTITUTION_SCHEMA,
    )
    assert doc.report.has_errors


def test_defined_sibling_substitution_member_is_admitted_at_its_particle(tmp_path):
    # wild072.v1: A fills the referenced head's particle; d/e are wildcard
    doc = parse(
        tmp_path,
        "<root><A/><b/><d/><e/></root>",
        schema_xml=DEFINED_SIBLING_SUBSTITUTION_SCHEMA,
    )
    assert not doc.report.has_errors


DEFINED_SIBLING_NOT_NAMESPACE_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="product" type="ProductType"/>
  <xs:complexType name="ProductType">
    <xs:sequence>
      <xs:element name="number" type="xs:string"/>
      <xs:element name="name" type="xs:string"/>
      <xs:any minOccurs="0" maxOccurs="unbounded"
              notNamespace="http://www.w3.org/1999/xhtml"
              notQName="##definedSibling"
              processContents="lax"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>
"""


def test_defined_sibling_with_a_namespace_constraint(tmp_path):
    # wild084.n1
    doc = parse(
        tmp_path,
        "<product><number>557</number><name>x</name><number>12345</number></product>",
        schema_xml=DEFINED_SIBLING_NOT_NAMESPACE_SCHEMA,
    )
    assert doc.report.has_errors
    doc = parse(
        tmp_path,
        "<product><number>557</number><name>x</name><extra>y</extra></product>",
        schema_xml=DEFINED_SIBLING_NOT_NAMESPACE_SCHEMA,
    )
    assert not doc.report.has_errors
