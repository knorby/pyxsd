"""Wildcard (xs:any) particles: order and occurrence are enforced.

Wildcards used to bypass the content model entirely: any child a
wildcard's namespace constraint admitted could appear in any number, in
any position, or not at all. These tests pin the corrected behavior:
wildcard particles participate in matching like any other particle.
"""

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD

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
    return PyXSD(
        str(tmp_path / "instance.xml"),
        str(tmp_path / "schema.xsd"),
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=mode,
    )


def test_wildcard_child_in_place_parses_clean(tmp_path):
    parser = parse(
        tmp_path,
        f"<t:r {DECLS}><t:a>x</t:a><o:x/></t:r>",
    )
    assert not parser.report.has_errors
    assert len(parser.schemaRootInstance._children_) == 2


def test_missing_wildcard_occurrence_is_reported(tmp_path):
    parser = parse(tmp_path, f"<t:r {DECLS}><t:a>x</t:a></t:r>")
    assert parser.report.has_errors
    assert "occurrence-min" in [issue.code for issue in parser.report]


def test_excess_wildcard_content_is_reported(tmp_path):
    parser = parse(
        tmp_path,
        f"<t:r {DECLS}><t:a>x</t:a><o:x/><o:y/></t:r>",
    )
    assert parser.report.has_errors


def test_wildcard_before_required_element_is_reported(tmp_path):
    parser = parse(
        tmp_path,
        f"<t:r {DECLS}><o:x/><t:a>x</t:a></t:r>",
    )
    assert parser.report.has_errors


def test_optional_wildcard_needs_no_content(tmp_path):
    schema = NS_SCHEMA.replace(
        '<xs:any namespace="urn:o" processContents="skip"/>',
        '<xs:any namespace="urn:o" processContents="skip" minOccurs="0"/>',
    )
    parser = parse(tmp_path, f"<t:r {DECLS}><t:a>x</t:a></t:r>", schema_xml=schema)
    assert not parser.report.has_errors


def test_unbounded_wildcard_repeats(tmp_path):
    schema = NS_SCHEMA.replace(
        '<xs:any namespace="urn:o" processContents="skip"/>',
        '<xs:any namespace="urn:o" processContents="skip" maxOccurs="unbounded"/>',
    )
    parser = parse(
        tmp_path,
        f"<t:r {DECLS}><t:a>x</t:a><o:x/><o:y/></t:r>",
        schema_xml=schema,
    )
    assert not parser.report.has_errors
    assert len(parser.schemaRootInstance._children_) == 3


def test_declared_element_wins_over_same_namespace_wildcard(tmp_path):
    """A declared particle always takes precedence over a wildcard."""
    schema = NS_SCHEMA.replace('namespace="urn:o"', 'namespace="##any"')
    parser = parse(
        tmp_path,
        f"<t:r {DECLS}><t:a>x</t:a><o:x/></t:r>",
        schema_xml=schema,
    )
    assert not parser.report.has_errors
    # The declared particle bound the a element, not the wildcard.
    assert parser.schemaRootInstance._children_[0]._name_ == "{urn:t}a"


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
    parser = parse(
        tmp_path,
        "<r><a>x</a><whatever/></r>",
        schema_xml=legacy_schema,
        mode=ParseModes.STRICT,
    )
    assert not parser.report.has_errors
    assert len(parser.schemaRootInstance._children_) == 2


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
    parser = parse(
        tmp_path,
        "<r><a>x</a><one/><two/></r>",
        schema_xml=legacy_schema,
        mode=ParseModes.STRICT,
    )
    assert parser.report.has_errors
