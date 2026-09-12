"""Form-default context through schema composition.

A component spliced in from another document must keep that
document's ``elementFormDefault``/``attributeFormDefault``, and an
explicit ``form`` attribute on a local declaration must win over any
default. Composition must not rewrite imported declarations with the
host schema's defaults.
"""

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

MAIN_SCHEMA = f"""<xs:schema {XS}
    targetNamespace="urn:m" xmlns:m="urn:m" xmlns:o="urn:o"
    elementFormDefault="qualified">
  <xs:import namespace="urn:o" schemaLocation="o.xsd"/>
  <xs:element name="r" type="o:T"/>
</xs:schema>"""

IMPORTED_SCHEMA = f"""<xs:schema {XS}
    targetNamespace="urn:o"
    elementFormDefault="unqualified" attributeFormDefault="qualified">
  <xs:complexType name="T">
    <xs:sequence>
      <xs:element name="a" type="xs:int"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string" use="required"/>
  </xs:complexType>
</xs:schema>"""

VALID_INSTANCE = '<m:r xmlns:m="urn:m" xmlns:o="urn:o" o:id="1"><a>7</a></m:r>'

INVALID_INSTANCE = '<m:r xmlns:m="urn:m" xmlns:o="urn:o" id="1"><o:a>7</o:a></m:r>'


def _parse(tmp_path, schema, instance, main_schema=MAIN_SCHEMA):
    (tmp_path / "main.xsd").write_text(main_schema)
    (tmp_path / "o.xsd").write_text(IMPORTED_SCHEMA)
    (tmp_path / "instance.xml").write_text(instance)
    return PyXSD(
        tmp_path / "instance.xml",
        xsdFile=tmp_path / "main.xsd",
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
    )


def test_imported_components_keep_source_form_defaults(tmp_path):
    """Valid instance with source forms parses without errors."""
    parser = _parse(tmp_path, MAIN_SCHEMA, VALID_INSTANCE)
    assert [issue.code for issue in parser.report] == []


def test_host_defaults_are_not_applied_to_imported_components(tmp_path):
    """An instance using the host's forms is rejected."""
    parser = _parse(tmp_path, MAIN_SCHEMA, INVALID_INSTANCE)
    codes = [issue.code for issue in parser.report]
    assert "unexpected-element" in codes
    assert "unexpected-attribute" in codes


def test_explicit_form_overrides_element_default(tmp_path):
    """``form`` on a local element beats the schema's default."""
    schema = f"""<xs:schema {XS} targetNamespace="urn:t"
        xmlns:t="urn:t" elementFormDefault="qualified">
      <xs:complexType name="T">
        <xs:sequence>
          <xs:element name="a" type="xs:string" form="unqualified"/>
          <xs:element name="b" type="xs:string"/>
        </xs:sequence>
      </xs:complexType>
      <xs:element name="root" type="t:T"/>
    </xs:schema>"""
    instance = '<t:root xmlns:t="urn:t"><a>1</a><t:b>2</t:b></t:root>'
    parser = _parse(tmp_path, schema, instance, main_schema=schema)
    assert [issue.code for issue in parser.report] == []


def test_explicit_form_overrides_attribute_default(tmp_path):
    """``form`` on a local attribute beats the schema's default."""
    schema = f"""<xs:schema {XS} targetNamespace="urn:t"
        xmlns:t="urn:t" attributeFormDefault="qualified">
      <xs:complexType name="T">
        <xs:sequence>
          <xs:element name="a" type="xs:string"/>
        </xs:sequence>
        <xs:attribute name="id" type="xs:string" form="unqualified"/>
      </xs:complexType>
      <xs:element name="root" type="t:T"/>
    </xs:schema>"""
    instance = '<t:root xmlns:t="urn:t" id="x"><a>1</a></t:root>'
    parser = _parse(tmp_path, schema, instance, main_schema=schema)
    assert [issue.code for issue in parser.report] == []


def test_explicit_qualified_form_in_unqualified_schema(tmp_path):
    """``form="qualified"`` inside an unqualified schema qualifies."""
    schema = f"""<xs:schema {XS} targetNamespace="urn:t"
        xmlns:t="urn:t">
      <xs:complexType name="T">
        <xs:sequence>
          <xs:element name="a" type="xs:string" form="qualified"/>
        </xs:sequence>
      </xs:complexType>
      <xs:element name="root" type="t:T"/>
    </xs:schema>"""
    instance = '<root xmlns="urn:t" xmlns:t="urn:t"><t:a>1</t:a></root>'
    parser = _parse(tmp_path, schema, instance, main_schema=schema)
    assert [issue.code for issue in parser.report] == []
