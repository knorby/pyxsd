"""Legacy-mode namespace binding of references and instance attributes.

Two deferred gaps from the conformance wave, pinned here:

* a *prefixed* legacy reference whose prefix denotes a namespace never
  binds a same-local-name declaration from a *different* namespace —
  the reference names that namespace, so a foreign declaration is not
  its target (``xml:bogus`` must not reach an unrelated ``bogus``);
* a namespaced *instance* attribute binds the declared use whose
  authored namespace it names (ElementTree's Clark key), so a declared
  ``xml:lang`` use accepts an instance ``xml:lang="en"`` instead of
  reporting it undeclared.

The legacy local-name leniency itself is a documented contract for
*unprefixed* references and no-namespace instance attributes; the
guards below pin that it survives.
"""

from pyxsd.schema import Schema

XML_NS = "http://www.w3.org/XML/1998/namespace"


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


FOREIGN_ATTR_SCHEMA = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:n="urn:n" targetNamespace="urn:m">
  <xs:import namespace="urn:n" schemaLocation="N.xsd"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:attribute ref="xml:bogus"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

FOREIGN_ATTR_IMPORTED = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:n">
  <xs:attribute name="bogus" type="xs:string"/>
</xs:schema>
"""

XML_LANG_SCHEMA = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:m="urn:m" targetNamespace="urn:m">
  <xs:import namespace="http://www.w3.org/XML/1998/namespace"/>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="body" type="xs:string"/>
      </xs:sequence>
      <xs:attribute ref="xml:lang"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


class TestPrefixedLegacyReferences:
    def test_prefixed_reference_does_not_bind_a_foreign_declaration(self, tmp_path):
        _write(tmp_path, "N.xsd", FOREIGN_ATTR_IMPORTED)
        _write(tmp_path, "A.xsd", FOREIGN_ATTR_SCHEMA)
        schema = Schema.compile(str(tmp_path / "A.xsd"))
        codes = {error.code for error in schema.report.errors}
        assert "unknown-attributeRef" in codes

    def test_prefixed_reference_still_resolves_its_own_namespace(self, tmp_path):
        _write(tmp_path, "N.xsd", FOREIGN_ATTR_IMPORTED)
        schema_doc = FOREIGN_ATTR_SCHEMA.replace(
            '<xs:attribute ref="xml:bogus"/>', '<xs:attribute ref="n:bogus"/>'
        )
        _write(tmp_path, "A.xsd", schema_doc)
        schema = Schema.compile(str(tmp_path / "A.xsd"))
        assert not schema.report.has_errors

    def test_prefixed_reference_still_resolves_a_local_declaration(self, tmp_path):
        schema_doc = """<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
                   xmlns:m="urn:m" targetNamespace="urn:m">
          <xs:attribute name="local" type="xs:string"/>
          <xs:element name="root">
            <xs:complexType>
              <xs:attribute ref="m:local"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>
        """
        _write(tmp_path, "A.xsd", schema_doc)
        schema = Schema.compile(str(tmp_path / "A.xsd"))
        assert not schema.report.has_errors

    def test_unprefixed_reference_keeps_the_local_name_fallback(self, tmp_path):
        # The documented legacy leniency: an unprefixed reference (no
        # namespace named) still binds by local name alone.
        schema_doc = """<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
                   xmlns:n="urn:n" targetNamespace="urn:m">
          <xs:import namespace="urn:n" schemaLocation="N.xsd"/>
          <xs:element name="root">
            <xs:complexType>
              <xs:attribute ref="bogus"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>
        """
        _write(tmp_path, "N.xsd", FOREIGN_ATTR_IMPORTED)
        _write(tmp_path, "A.xsd", schema_doc)
        schema = Schema.compile(str(tmp_path / "A.xsd"))
        assert not schema.report.has_errors


class TestNamespacedInstanceAttributes:
    def test_declared_xml_lang_use_binds_the_instance_attribute(self, tmp_path):
        _write(tmp_path, "B.xsd", XML_LANG_SCHEMA)
        _write(
            tmp_path,
            "doc.xml",
            '<m:doc xmlns:m="urn:m" xml:lang="en"><m:body>x</m:body></m:doc>',
        )
        schema = Schema.compile(str(tmp_path / "B.xsd"))
        assert not schema.report.has_errors
        document = schema.parse(str(tmp_path / "doc.xml"))
        assert not document.report.has_errors
        assert document.root.lang == "en"

    def test_undeclared_namespaced_attribute_is_still_rejected(self, tmp_path):
        _write(tmp_path, "B.xsd", XML_LANG_SCHEMA)
        _write(
            tmp_path,
            "doc.xml",
            '<m:doc xmlns:m="urn:m" xml:bogus="x"><m:body>x</m:body></m:doc>',
        )
        schema = Schema.compile(str(tmp_path / "B.xsd"))
        document = schema.parse(str(tmp_path / "doc.xml"))
        codes = {error.code for error in document.report.errors}
        assert "unexpected-attribute" in codes

    def test_no_namespace_instance_attribute_still_binds(self, tmp_path):
        schema_doc = """<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="doc">
            <xs:complexType>
              <xs:sequence>
                <xs:element name="body" type="xs:string"/>
              </xs:sequence>
              <xs:attribute name="plain" type="xs:string"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>
        """
        _write(tmp_path, "C.xsd", schema_doc)
        _write(tmp_path, "doc.xml", '<doc plain="v"><body>x</body></doc>')
        schema = Schema.compile(str(tmp_path / "C.xsd"))
        document = schema.parse(str(tmp_path / "doc.xml"))
        assert not document.report.has_errors
        assert document.root.plain == "v"
