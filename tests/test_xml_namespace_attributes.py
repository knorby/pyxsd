"""Attribute references into the XML namespace resolve.

The XML namespace has no schema document of its own: ``xml:space``,
``xml:lang``, ``xml:base`` and ``xml:id`` are assigned by the XML-family
specifications. A schema that uses them imports the namespace -- the
W3C schema-for-schemas itself does so **without** a ``schemaLocation``
-- and the built-in declarations must be available for the references
to resolve. XML Schema 1.1 Part 1 §4.2.3 registers this namespace's
schema in the built-in schema table, so no document is ever fetched.

Two gaps made the references fail in ``legacy`` namespace mode (the
default):

* a reference site's prefixed lexical name was never reduced to its
  local part, so ``<xs:attribute ref="xml:lang"/>`` could not select
  the ``lang`` declaration, and likewise ``xlink:href`` or ``r:id``
  could not select an imported namespace's declaration;
* the XML-namespace built-in declarations were registered only in
  ``strict`` mode, so a namespace-only ``xs:import`` of the XML
  namespace had nothing to resolve against.

Element vocabulary is deliberately *not* added for the XML namespace:
only the attribute declarations are built-in.
"""

import io

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema

XSD_NS = "http://www.w3.org/2001/XMLSchema"
XML_NS = "http://www.w3.org/XML/1998/namespace"

XS = f'xmlns:xs="{XSD_NS}"'
XML = f'xmlns:xml="{XML_NS}"'

#: The canonical XML-namespace schema's declarations, reduced to the
#: ones the tests exercise. The real W3C document is ~150 lines of the
#: same shape (fhir-single and MusicXML both vendor it).
XML_XSD = f"""<xs:schema {XS} {XML} targetNamespace="{XML_NS}">
  <xs:attribute name="lang">
    <xs:simpleType>
      <xs:union memberTypes="xs:language">
        <xs:simpleType>
          <xs:restriction base="xs:string"/>
        </xs:simpleType>
      </xs:union>
    </xs:simpleType>
  </xs:attribute>
  <xs:attribute name="space">
    <xs:simpleType>
      <xs:restriction base="xs:NCName">
        <xs:enumeration value="default"/>
        <xs:enumeration value="preserve"/>
      </xs:restriction>
    </xs:simpleType>
  </xs:attribute>
  <xs:attribute name="base" type="xs:anyURI"/>
  <xs:attribute name="id" type="xs:ID"/>
</xs:schema>
"""


def _write_schema(tmp_path, text, name="schema.xsd"):
    path = tmp_path / name
    path.write_text(text)
    return path


def _compile(tmp_path, text, mode=None):
    path = _write_schema(tmp_path, text)
    return Schema.compile(str(path)) if mode is None else Schema.compile(str(path), mode=mode)


def _error_codes(schema):
    return sorted({error.code for error in schema.report.errors})


class TestNamespaceOnlyXmlImport:
    """``xs:import`` of the XML namespace with no ``schemaLocation``.

    The imported namespace has no document: its attribute declarations
    are built-in, and a namespace-only import is enough to use them.
    """

    def test_refs_to_xml_attributes_resolve(self, tmp_path):
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML}>
  <xs:import namespace="{XML_NS}"/>
  <xs:complexType name="Holder">
    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
    <xs:attribute ref="xml:lang"/>
    <xs:attribute ref="xml:space"/>
    <xs:attribute ref="xml:base"/>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == []

    def test_unknown_xml_attribute_still_errors(self, tmp_path):
        """Only the four assigned declarations are built-in."""
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML}>
  <xs:import namespace="{XML_NS}"/>
  <xs:complexType name="Holder">
    <xs:attribute ref="xml:bogus"/>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == ["unknown-attributeRef"]

    def test_no_element_vocabulary_is_added(self, tmp_path):
        """The XML namespace gains attributes only, never elements."""
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML}>
  <xs:import namespace="{XML_NS}"/>
  <xs:complexType name="Holder">
    <xs:sequence><xs:element ref="xml:div"/></xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == ["unknown-elementRef"]

    def test_user_global_with_a_builtin_name_is_not_disturbed(self, tmp_path):
        """A user ``id`` declaration keeps its own type and legality."""
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML} targetNamespace="urn:t" xmlns:t="urn:t"
    attributeFormDefault="qualified">
  <xs:import namespace="{XML_NS}"/>
  <xs:attribute name="id" type="xs:int"/>
  <xs:complexType name="Holder">
    <xs:attribute ref="t:id"/>
    <xs:attribute ref="xml:lang"/>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == []


class TestXmlSchemaImport:
    """``xs:import`` of the XML namespace with a ``schemaLocation``.

    The fhir-single and MusicXML shapes: the namespace's real schema is
    composed, so the references select its declarations.
    """

    def test_refs_resolve_from_the_imported_document(self, tmp_path):
        _write_schema(tmp_path, XML_XSD, name="xml.xsd")
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML}>
  <xs:import namespace="{XML_NS}" schemaLocation="xml.xsd"/>
  <xs:complexType name="Holder">
    <xs:attribute ref="xml:lang"/>
    <xs:attribute ref="xml:space"/>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == []

    def test_unknown_xml_attribute_still_errors(self, tmp_path):
        _write_schema(tmp_path, XML_XSD, name="xml.xsd")
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML}>
  <xs:import namespace="{XML_NS}" schemaLocation="xml.xsd"/>
  <xs:complexType name="Holder">
    <xs:attribute ref="xml:bogus"/>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == ["unknown-attributeRef"]


class TestOtherNamespaceRefs:
    """A prefixed reference selects the imported namespace's declaration.

    The MusicXML ``xlink:href`` shape: the prefix is not special, the
    referenced document is composed, and the reference must reach it.
    """

    def test_prefixed_attribute_ref_resolves(self, tmp_path):
        _write_schema(
            tmp_path,
            f"""<xs:schema {XS} xmlns:n="urn:n" targetNamespace="urn:n"
    attributeFormDefault="qualified">
  <xs:attribute name="a" type="xs:string"/>
</xs:schema>""",
            name="n.xsd",
        )
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML} xmlns:n="urn:n">
  <xs:import namespace="urn:n" schemaLocation="n.xsd"/>
  <xs:import namespace="{XML_NS}"/>
  <xs:complexType name="Holder">
    <xs:attribute ref="n:a"/>
    <xs:attribute ref="xml:lang"/>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == []

    def test_prefixed_ref_to_an_absent_name_still_errors(self, tmp_path):
        _write_schema(
            tmp_path,
            f"""<xs:schema {XS} xmlns:n="urn:n" targetNamespace="urn:n">
  <xs:attribute name="a" type="xs:string"/>
</xs:schema>""",
            name="n.xsd",
        )
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} xmlns:n="urn:n">
  <xs:import namespace="urn:n" schemaLocation="n.xsd"/>
  <xs:complexType name="Holder">
    <xs:attribute ref="n:absent"/>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == ["unknown-attributeRef"]


class TestStrictModeUnchanged:
    """Strict namespace mode keeps resolving the same references."""

    def test_namespace_only_import_resolves_in_strict_mode(self, tmp_path):
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML}>
  <xs:import namespace="{XML_NS}"/>
  <xs:complexType name="Holder">
    <xs:attribute ref="xml:lang"/>
    <xs:attribute ref="xml:space"/>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
            mode=ParseModes.NAMESPACED,
        )
        assert _error_codes(schema) == []

    def test_unknown_xml_attribute_errors_in_strict_mode(self, tmp_path):
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML}>
  <xs:import namespace="{XML_NS}"/>
  <xs:complexType name="Holder">
    <xs:attribute ref="xml:bogus"/>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
            mode=ParseModes.NAMESPACED,
        )
        assert _error_codes(schema) == ["unknown-attributeRef"]


class TestDuplicateUsesAcrossNamespaces:
    """``xml:lang`` is not the same use as an unqualified ``lang``.

    The XHTML shape (fhir-xhtml): a type declares a plain ``lang``
    attribute (backwards compatibility) and also references
    ``xml:lang``. The two uses have distinct expanded names, so they
    are not a duplicate attribute use (attQ019) -- in either mode,
    even though legacy mode matches instances by local name.
    """

    def test_group_with_plain_lang_and_xml_lang_ref_is_legal(self, tmp_path):
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML} targetNamespace="urn:t" xmlns:t="urn:t">
  <xs:import namespace="{XML_NS}"/>
  <xs:attributeGroup name="i18n">
    <xs:attribute name="lang" type="xs:string"/>
    <xs:attribute ref="xml:lang"/>
  </xs:attributeGroup>
  <xs:complexType name="div">
    <xs:attributeGroup ref="t:i18n"/>
  </xs:complexType>
  <xs:element name="root" type="div"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == []

    def test_direct_plain_lang_and_xml_lang_ref_is_legal(self, tmp_path):
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML}>
  <xs:import namespace="{XML_NS}"/>
  <xs:complexType name="div">
    <xs:attribute name="lang" type="xs:string"/>
    <xs:attribute ref="xml:lang"/>
  </xs:complexType>
  <xs:element name="root" type="div"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == []

    def test_xml_ref_contributed_twice_is_still_a_duplicate(self, tmp_path):
        """A reference reached by a group and directly is one use twice."""
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML} targetNamespace="urn:t" xmlns:t="urn:t">
  <xs:import namespace="{XML_NS}"/>
  <xs:attributeGroup name="i18n">
    <xs:attribute ref="xml:lang"/>
  </xs:attributeGroup>
  <xs:complexType name="dup">
    <xs:attributeGroup ref="t:i18n"/>
    <xs:attribute ref="xml:lang"/>
  </xs:complexType>
  <xs:element name="root" type="dup"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == ["duplicate-attribute"]

    def test_plain_lang_twice_is_still_a_duplicate(self, tmp_path):
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML}>
  <xs:import namespace="{XML_NS}"/>
  <xs:complexType name="dup">
    <xs:attribute name="lang" type="xs:string"/>
    <xs:attribute name="lang" type="xs:string"/>
  </xs:complexType>
  <xs:element name="root" type="dup"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == ["duplicate-attribute"]


class TestXmlReferenceDefaults:
    """A resolved XML-namespace reference behaves like any other use."""

    def test_reference_site_default_is_honored(self, tmp_path):
        schema = _compile(
            tmp_path,
            f"""<xs:schema {XS} {XML}>
  <xs:import namespace="{XML_NS}"/>
  <xs:complexType name="Holder">
    <xs:attribute ref="xml:space" default="preserve"/>
  </xs:complexType>
  <xs:element name="root" type="Holder"/>
</xs:schema>""",
        )
        assert _error_codes(schema) == []
        document = schema.parse(io.StringIO("<root/>"))
        assert not document.report.has_errors
        assert document.root.space == "preserve"
