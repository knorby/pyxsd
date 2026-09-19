"""Tests for namespace capture and namespace-aware parsing.

The default policy is ``legacy``; these tests exercise the capture
machinery directly and the strict-mode behaviour added on top of it.
"""

import xml.etree.ElementTree as ET
from io import StringIO

import pytest

from pyxsd.binding import ParseModes
from pyxsd.namespaces import (
    XLINK_NS,
    XSD_NS,
    XSI_NS,
    NamespaceContext,
    NamespaceError,
    clark,
    local_name,
    namespace_of,
    parse_with_namespaces,
)
from pyxsd.parser import PyXSD
from pyxsd.validation import IssueSeverity
from pyxsd.writers.xml_tree_writer import XmlTreeWriter
from pyxsd.xsd_data_types import QName, qname_context, xsd_comparable_key, xsd_value_key


def _parse(xml: str):
    context = NamespaceContext()
    root = parse_with_namespaces(StringIO(xml), context)
    return root, context


class TestNameHelpers:
    def test_clark_round_trip(self):
        assert clark("urn:a", "x") == "{urn:a}x"
        assert clark(None, "x") == "x"
        assert clark("", "x") == "x"
        assert local_name("{urn:a}x") == "x"
        assert namespace_of("{urn:a}x") == "urn:a"

    def test_bare_name_has_no_namespace(self):
        assert local_name("x") == "x"
        assert namespace_of("x") is None


class TestCapture:
    def test_default_namespace_applies_to_content(self):
        root, context = _parse('<a xmlns="urn:d"><b/></a>')
        assert context.resolve(root, "b") == "{urn:d}b"
        assert root.tag == "{urn:d}a"

    def test_unprefixed_attribute_is_not_in_default_namespace(self):
        root, context = _parse('<a xmlns="urn:d"/>')
        assert context.resolve(root, "x", is_attribute=True) == "x"

    def test_prefixed_name_resolves(self):
        root, context = _parse('<a xmlns="urn:d" xmlns:p="urn:p"/>')
        assert context.resolve(root, "p:x") == "{urn:p}x"

    def test_nested_redeclaration_rebinds_within_its_scope(self):
        root, context = _parse('<a xmlns="urn:outer"><b><c xmlns="urn:inner"/></b></a>')
        outer_b = root.find("{urn:outer}b")
        inner_c = root.find("{urn:outer}b/{urn:inner}c")
        assert context.resolve(outer_b, "z") == "{urn:outer}z"
        assert context.resolve(inner_c, "z") == "{urn:inner}z"

    def test_nested_prefix_declaration_does_not_leak_out(self):
        root, context = _parse('<a xmlns="urn:o"><b xmlns:p="urn:p"/></a>')
        outer_b = root.find("{urn:o}b")
        assert context.resolve(outer_b, "p:x") == "{urn:p}x"
        with pytest.raises(NamespaceError):
            context.resolve(root, "p:x")

    def test_unknown_prefix_raises(self):
        root, context = _parse("<a/>")
        with pytest.raises(NamespaceError):
            context.resolve(root, "p:x")

    def test_already_expanded_name_is_returned_unchanged(self):
        root, context = _parse("<a/>")
        assert context.resolve(root, "{urn:x}y") == "{urn:x}y"

    def test_root_bindings_recorded_for_writers(self):
        _, context = _parse('<a xmlns="urn:d" xmlns:p="urn:p"/>')
        assert context.root_bindings == {"": "urn:d", "p": "urn:p"}

    def test_prefix_for(self):
        root, context = _parse('<a xmlns="urn:d" xmlns:p="urn:p"/>')
        assert context.prefix_for(root, "urn:p") == "p"
        assert context.prefix_for(root, "urn:d") == ""
        assert context.prefix_for(root, "urn:nope") is None

    def test_xsd_namespace_constant(self):
        assert XSD_NS == "http://www.w3.org/2001/XMLSchema"


def _strict_parse(schema: str, instance: str, tmp_path):
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(schema)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance)
    return PyXSD(
        instance_path,
        xsdFile=schema_path,
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
    )


_TNS_SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t"
    targetNamespace="urn:t" elementFormDefault="qualified">
  <xs:complexType name="Foo">
    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="t:Foo"/>
</xs:schema>"""


class TestSchemaComponentIdentity:
    def test_global_type_is_keyed_by_expanded_name(self, tmp_path):
        parser = _strict_parse(_TNS_SCHEMA, '<root xmlns="urn:t"><a>x</a></root>', tmp_path)
        table = parser.components
        assert table.getFromName("Foo", kind="type", namespace="urn:t") is not None
        # No cross-namespace fallback to the no-namespace form.
        assert table.getFromName("Foo", kind="type", namespace=None) is None

    def test_expanded_alias_resolves_user_type(self, tmp_path):
        parser = _strict_parse(_TNS_SCHEMA, '<root xmlns="urn:t"><a>x</a></root>', tmp_path)
        cls = parser.classes["{urn:t}Foo"]
        assert cls is parser.classes["Foo"]
        assert not parser.report.has_errors

    def test_alternate_prefix_for_schema_namespace_resolves_builtin(self, tmp_path):
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:sd="{XSD_NS}">
  <xs:element name="r" type="sd:int"/>
</xs:schema>"""
        parser = _strict_parse(schema, "<r>7</r>", tmp_path)
        element = parser.components.getFromName("r", kind="element", namespace=None)
        assert element is not None
        assert element.resolvedTypeName() == f"{{{XSD_NS}}}int"
        assert not parser.report.has_errors

    def test_unbound_type_prefix_is_reported(self, tmp_path):
        schema = f'<xs:schema xmlns:xs="{XSD_NS}"><xs:element name="r" type="p:int"/></xs:schema>'
        parser = _strict_parse(schema, "<r>7</r>", tmp_path)
        assert "unknown-namespace-prefix" in [issue.code for issue in parser.report.issues]


def _tns_schema(body: str, *, extra_ns: str = "") -> str:
    return f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t" {extra_ns}
    targetNamespace="urn:t" elementFormDefault="qualified">
{body}
</xs:schema>"""


class TestSchemaQNameReferences:
    """QName-valued schema attributes resolve against the schema scope."""

    def test_element_ref_in_target_namespace_resolves(self, tmp_path):
        schema = _tns_schema(
            '<xs:element name="a" type="xs:string"/>'
            '<xs:complexType name="Foo">'
            '<xs:sequence><xs:element ref="t:a"/></xs:sequence>'
            "</xs:complexType>"
            '<xs:element name="root" type="t:Foo"/>'
        )
        parser = _strict_parse(schema, '<root xmlns="urn:t"><a>x</a></root>', tmp_path)
        codes = [issue.code for issue in parser.report.issues]
        assert "unknown-elementRef" not in codes
        assert not parser.report.has_errors

    def test_element_ref_other_namespace_does_not_fall_back(self, tmp_path):
        schema = _tns_schema(
            '<xs:element name="a" type="xs:string"/>'
            '<xs:complexType name="Foo">'
            '<xs:sequence><xs:element ref="o:a"/></xs:sequence>'
            "</xs:complexType>"
            '<xs:element name="root" type="t:Foo"/>',
            extra_ns='xmlns:o="urn:o"',
        )
        parser = _strict_parse(schema, '<root xmlns="urn:t"><a>x</a></root>', tmp_path)
        codes = [issue.code for issue in parser.report.issues]
        assert "unknown-elementRef" in codes

    def test_unbound_ref_prefix_is_reported(self, tmp_path):
        schema = _tns_schema(
            '<xs:element name="a" type="xs:string"/>'
            '<xs:complexType name="Foo">'
            '<xs:sequence><xs:element ref="p:a"/></xs:sequence>'
            "</xs:complexType>"
            '<xs:element name="root" type="t:Foo"/>'
        )
        parser = _strict_parse(schema, '<root xmlns="urn:t"/>', tmp_path)
        codes = [issue.code for issue in parser.report.issues]
        assert "unknown-namespace-prefix" in codes
        assert "unknown-elementRef" in codes

    def test_group_ref_in_target_namespace_resolves(self, tmp_path):
        schema = _tns_schema(
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/>'
            "</xs:sequence></xs:group>"
            '<xs:complexType name="Foo"><xs:group ref="t:g"/></xs:complexType>'
            '<xs:element name="root" type="t:Foo"/>'
        )
        parser = _strict_parse(schema, '<root xmlns="urn:t"><a>x</a></root>', tmp_path)
        codes = [issue.code for issue in parser.report.issues]
        assert "unknown-group" not in codes
        assert not parser.report.has_errors

    def test_attribute_group_ref_in_target_namespace_resolves(self, tmp_path):
        schema = _tns_schema(
            '<xs:attributeGroup name="ag">'
            '<xs:attribute name="x" type="xs:int"/>'
            "</xs:attributeGroup>"
            '<xs:complexType name="Foo"><xs:attributeGroup ref="t:ag"/></xs:complexType>'
            '<xs:element name="root" type="t:Foo"/>'
        )
        parser = _strict_parse(schema, '<root xmlns="urn:t" x="5"/>', tmp_path)
        codes = [issue.code for issue in parser.report.issues]
        assert "unknown-attributeGroup" not in codes
        assert not parser.report.has_errors

    def test_substitution_group_in_target_namespace_resolves(self, tmp_path):
        schema = _tns_schema(
            '<xs:element name="h" type="xs:int"/>'
            '<xs:element name="m" type="xs:int" substitutionGroup="t:h"/>'
            '<xs:complexType name="Foo">'
            '<xs:sequence><xs:element ref="t:h"/></xs:sequence>'
            "</xs:complexType>"
            '<xs:element name="root" type="t:Foo"/>'
        )
        parser = _strict_parse(schema, '<root xmlns="urn:t"><m>7</m></root>', tmp_path)
        codes = [issue.code for issue in parser.report.issues]
        assert "unknown-substitution-head" not in codes
        assert not parser.report.has_errors

    def test_union_member_types_resolve_in_target_namespace(self, tmp_path):
        schema = _tns_schema(
            '<xs:simpleType name="A"><xs:restriction base="xs:int"/></xs:simpleType>'
            '<xs:simpleType name="U"><xs:union memberTypes="t:A"/></xs:simpleType>'
            '<xs:element name="root" type="t:U"/>'
        )
        parser = _strict_parse(schema, '<root xmlns="urn:t">5</root>', tmp_path)
        assert "U" in parser.classes
        assert parser.classes["U"]._unionMembers
        assert not parser.report.has_errors

    def test_union_rejects_value_matching_no_member(self, tmp_path):
        schema = _tns_schema(
            '<xs:simpleType name="A"><xs:restriction base="xs:int"/></xs:simpleType>'
            '<xs:simpleType name="U"><xs:union memberTypes="t:A"/></xs:simpleType>'
            '<xs:element name="root" type="t:U"/>'
        )
        parser = _strict_parse(schema, '<root xmlns="urn:t">not-an-int</root>', tmp_path)
        assert "value" in [issue.code for issue in parser.report.issues]


class TestInstanceMatching:
    """Strict mode matches instance nodes by expanded name and form default."""

    def test_qualified_local_element_matches(self, tmp_path):
        schema = _tns_schema(
            '<xs:complexType name="Foo">'
            '<xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
            "</xs:complexType>"
            '<xs:element name="root" type="t:Foo"/>'
        )
        parser = _strict_parse(schema, '<root xmlns="urn:t"><a>x</a></root>', tmp_path)
        assert not parser.report.has_errors
        assert parser.schemaRootInstance._children_

    def test_wrong_namespace_local_element_is_rejected(self, tmp_path):
        schema = _tns_schema(
            '<xs:complexType name="Foo">'
            '<xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
            "</xs:complexType>"
            '<xs:element name="root" type="t:Foo"/>'
        )
        parser = _strict_parse(
            schema,
            '<root xmlns="urn:t" xmlns:o="urn:o"><o:a>x</o:a></root>',
            tmp_path,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "unexpected-element" in codes or "order" in codes

    def test_unqualified_local_element_requires_bare_name(self, tmp_path):
        schema = (
            f'<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t" targetNamespace="urn:t" '
            'elementFormDefault="unqualified">'
            '<xs:complexType name="Foo">'
            '<xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
            "</xs:complexType>"
            '<xs:element name="root" type="t:Foo"/>'
            "</xs:schema>"
        )
        good = _strict_parse(schema, '<t:root xmlns:t="urn:t"><a>x</a></t:root>', tmp_path)
        assert not good.report.has_errors
        bad = _strict_parse(schema, '<t:root xmlns:t="urn:t"><t:a>x</t:a></t:root>', tmp_path)
        codes = [issue.code for issue in bad.report.issues]
        assert "unexpected-element" in codes or "order" in codes

    def test_qualified_attribute_matches_form_default(self, tmp_path):
        schema = (
            f'<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t" targetNamespace="urn:t" '
            'elementFormDefault="qualified" attributeFormDefault="qualified">'
            '<xs:complexType name="Foo">'
            '<xs:attribute name="x" type="xs:int" use="required"/>'
            "</xs:complexType>"
            '<xs:element name="root" type="t:Foo"/>'
            "</xs:schema>"
        )
        parser = _strict_parse(schema, '<root xmlns="urn:t" xmlns:t="urn:t" t:x="5"/>', tmp_path)
        assert not parser.report.has_errors
        parser2 = _strict_parse(schema, '<root xmlns="urn:t" xmlns:o="urn:o" o:x="5"/>', tmp_path)
        codes = [issue.code for issue in parser2.report.issues]
        assert "missing-attribute" in codes

    def test_legacy_mode_matches_by_local_name(self, tmp_path):
        parser = PyXSD(
            StringIO('<root xmlns="urn:t" xmlns:o="urn:o"><o:a>x</o:a></root>'),
            xsdFile=StringIO(_TNS_SCHEMA),
            xmlFileOutput="_No_Output_",
        )
        assert not parser.report.has_errors


def _xsi_schema(body: str) -> str:
    return _tns_schema(body)


_DERIVED_SCHEMA = _xsi_schema(
    '<xs:complexType name="B"><xs:sequence/></xs:complexType>'
    '<xs:complexType name="D"><xs:complexContent>'
    '<xs:extension base="t:B"><xs:sequence>'
    '<xs:element name="b" type="xs:string"/>'
    "</xs:sequence></xs:extension></xs:complexContent></xs:complexType>"
    '<xs:complexType name="Holder">'
    '<xs:sequence><xs:element name="item" type="t:B"/></xs:sequence>'
    "</xs:complexType>"
    '<xs:element name="root" type="t:Holder"/>'
)


class TestXsiTypeNamespaces:
    """``xsi:type`` values resolve against the instance namespace scope."""

    def test_prefixed_xsi_type_dispatches_to_derived_type(self, tmp_path):
        instance = (
            '<root xmlns="urn:t" xmlns:t="urn:t" '
            f'xmlns:xsi="{XSI_NS}"><item xsi:type="t:D"><b>y</b></item></root>'
        )
        parser = _strict_parse(_DERIVED_SCHEMA, instance, tmp_path)
        assert not parser.report.has_errors

    def test_root_xsi_type_dispatches_to_derived_type(self, tmp_path):
        schema = _xsi_schema(
            '<xs:complexType name="B"><xs:sequence/></xs:complexType>'
            '<xs:complexType name="D"><xs:complexContent>'
            '<xs:extension base="t:B"><xs:sequence>'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:sequence></xs:extension></xs:complexContent></xs:complexType>"
            '<xs:element name="root" type="t:B"/>'
        )
        instance = (
            '<root xmlns="urn:t" xmlns:t="urn:t" '
            f'xmlns:xsi="{XSI_NS}" xsi:type="t:D"><b>y</b></root>'
        )
        parser = _strict_parse(schema, instance, tmp_path)
        assert not parser.report.has_errors
        assert type(parser.schemaRootInstance).__name__ == "D"

    def test_unbound_xsi_type_prefix_is_reported(self, tmp_path):
        instance = f'<root xmlns="urn:t" xmlns:xsi="{XSI_NS}"><item xsi:type="p:D"/></root>'
        parser = _strict_parse(_DERIVED_SCHEMA, instance, tmp_path)
        assert "unknown-namespace-prefix" in [i.code for i in parser.report.issues]

    def test_xsi_type_wrong_namespace_is_rejected(self, tmp_path):
        instance = (
            '<root xmlns="urn:t" xmlns:o="urn:o" '
            f'xmlns:xsi="{XSI_NS}"><item xsi:type="o:D"/></root>'
        )
        parser = _strict_parse(_DERIVED_SCHEMA, instance, tmp_path)
        assert "xsi-type" in [i.code for i in parser.report.issues]


_MAIN_SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t" xmlns:o="urn:o"
    targetNamespace="urn:t" elementFormDefault="qualified">
  <xs:complexType name="Holder">
    <xs:sequence><xs:element name="thing" type="o:Thing"/></xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="t:Holder"/>
</xs:schema>"""

_OTHER_SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:o="urn:o"
    targetNamespace="urn:o" elementFormDefault="qualified">
  <xs:complexType name="Thing">
    <xs:sequence><xs:element name="b" type="xs:string"/></xs:sequence>
  </xs:complexType>
</xs:schema>"""

_MAIN_REF_SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t" xmlns:o="urn:o"
    targetNamespace="urn:t" elementFormDefault="qualified">
  <xs:complexType name="Holder">
    <xs:sequence><xs:element ref="o:thing"/></xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="t:Holder"/>
</xs:schema>"""

_OTHER_ELEMENT_SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:o="urn:o"
    targetNamespace="urn:o" elementFormDefault="qualified">
  <xs:element name="thing" type="xs:string"/>
</xs:schema>"""

_MULTI_INSTANCE = '<root xmlns="urn:t"><thing><b xmlns="urn:o">x</b></thing></root>'


def _multi_parse(instance, main, other, tmp_path, *, use_schema_location=False, supply_other=True):
    main_path = tmp_path / "main.xsd"
    main_path.write_text(main)
    other_path = tmp_path / "other.xsd"
    other_path.write_text(other)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance)
    kwargs = {}
    if use_schema_location:
        xsd_file = None
    else:
        xsd_file = main_path
        if supply_other:
            kwargs["namespace_schemas"] = {"urn:o": other_path}
    return PyXSD(
        instance_path,
        xsdFile=xsd_file,
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
        **kwargs,
    )


class TestMultiNamespaceComposition:
    """Cross-namespace schemas compose into one parser-owned table."""

    def test_cross_namespace_type_reference_resolves(self, tmp_path):
        parser = _multi_parse(_MULTI_INSTANCE, _MAIN_SCHEMA, _OTHER_SCHEMA, tmp_path)
        assert not parser.report.has_errors
        thing = parser.components.getFromName("Thing", kind="type", namespace="urn:o")
        assert thing is not None
        # The same local name in the other namespace is distinct.
        assert parser.components.getFromName("Thing", kind="type", namespace="urn:t") is None

    def test_cross_namespace_element_ref_resolves(self, tmp_path):
        parser = _multi_parse(
            '<root xmlns="urn:t"><o:thing xmlns:o="urn:o">x</o:thing></root>',
            _MAIN_REF_SCHEMA,
            _OTHER_ELEMENT_SCHEMA,
            tmp_path,
        )
        codes = [i.code for i in parser.report.issues]
        assert "unknown-elementRef" not in codes
        assert not parser.report.has_errors

    def test_multi_pair_schema_location_loads_all_namespaces(self, tmp_path):
        instance = (
            '<root xmlns="urn:t" xmlns:o="urn:o" '
            f'xmlns:xsi="{XSI_NS}" '
            'xsi:schemaLocation="urn:t main.xsd urn:o other.xsd">'
            '<thing><b xmlns="urn:o">x</b></thing></root>'
        )
        parser = _multi_parse(
            instance,
            _MAIN_SCHEMA,
            _OTHER_SCHEMA,
            tmp_path,
            use_schema_location=True,
        )
        assert not parser.report.has_errors
        assert parser.components.getFromName("Thing", kind="type", namespace="urn:o") is not None

    def test_namespace_schemas_supplies_namespace_only_import(self, tmp_path):
        main = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t" xmlns:o="urn:o"
            targetNamespace="urn:t" elementFormDefault="qualified">
          <xs:import namespace="urn:o"/>
          <xs:complexType name="Holder">
            <xs:sequence><xs:element name="thing" type="o:Thing"/></xs:sequence>
          </xs:complexType>
          <xs:element name="root" type="t:Holder"/>
        </xs:schema>"""
        parser = _multi_parse(
            _MULTI_INSTANCE,
            main,
            _OTHER_SCHEMA,
            tmp_path,
        )
        codes = [i.code for i in parser.report.issues]
        assert "import-unresolved" not in codes
        assert not parser.report.has_errors

    def test_unresolved_namespace_only_import_is_reported(self, tmp_path):
        main = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t"
            targetNamespace="urn:t" elementFormDefault="qualified">
          <xs:import namespace="urn:o"/>
          <xs:element name="root" type="xs:string"/>
        </xs:schema>"""
        parser = _multi_parse(
            '<root xmlns="urn:t">x</root>',
            main,
            _OTHER_SCHEMA,
            tmp_path,
            supply_other=False,
        )
        assert "import-unresolved" in [i.code for i in parser.report.issues]

    def test_legacy_mode_still_merges_by_local_name(self, tmp_path):
        main_path = tmp_path / "main.xsd"
        main_path.write_text(_MAIN_SCHEMA)
        other_path = tmp_path / "other.xsd"
        other_path.write_text(_OTHER_SCHEMA)
        parser = PyXSD(
            StringIO(_MULTI_INSTANCE),
            xsdFile=main_path,
            xmlFileOutput="_No_Output_",
            namespace_schemas={"urn:o": other_path},
        )
        assert not parser.report.has_errors


def _wildcard_main(any_decl: str) -> str:
    return f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t"
    targetNamespace="urn:t" elementFormDefault="qualified">
  <xs:complexType name="Holder">
    <xs:sequence>{any_decl}</xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="t:Holder"/>
</xs:schema>"""


_OTHER_DECL = f"""<xs:schema xmlns:xs="{XSD_NS}"
    targetNamespace="urn:o" elementFormDefault="qualified">
  <xs:element name="extra" type="xs:int"/>
</xs:schema>"""


def _wildcard_parse(instance, any_decl, tmp_path, *, other=None, mode=None):
    main_path = tmp_path / "main.xsd"
    main_path.write_text(_wildcard_main(any_decl))
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance)
    kwargs = {}
    if other is not None:
        other_path = tmp_path / "other.xsd"
        other_path.write_text(other)
        kwargs["namespace_schemas"] = {"urn:o": other_path}
    return PyXSD(
        instance_path,
        xsdFile=main_path,
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=mode or ParseModes.NAMESPACED,
        **kwargs,
    )


class TestWildcardNamespaces:
    """xs:any namespace constraints and processContents."""

    def test_other_accepts_foreign_and_rejects_target(self, tmp_path):
        decl = '<xs:any namespace="##other" processContents="skip"/>'
        accepted = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:o"/></root>', decl, tmp_path
        )
        assert not accepted.report.has_errors
        rejected = _wildcard_parse('<root xmlns="urn:t"><extra/></root>', decl, tmp_path)
        assert "unexpected-element" in [i.code for i in rejected.report.issues]

    def test_target_namespace_constraint(self, tmp_path):
        decl = '<xs:any namespace="##targetNamespace" processContents="skip"/>'
        accepted = _wildcard_parse('<root xmlns="urn:t"><extra/></root>', decl, tmp_path)
        assert not accepted.report.has_errors
        rejected = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:o"/></root>', decl, tmp_path
        )
        assert "unexpected-element" in [i.code for i in rejected.report.issues]

    def test_local_constraint(self, tmp_path):
        decl = '<xs:any namespace="##local" processContents="skip"/>'
        accepted = _wildcard_parse('<root xmlns="urn:t"><extra xmlns=""/></root>', decl, tmp_path)
        assert not accepted.report.has_errors
        rejected = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:o"/></root>', decl, tmp_path
        )
        assert "unexpected-element" in [i.code for i in rejected.report.issues]

    def test_uri_list_constraint(self, tmp_path):
        decl = '<xs:any namespace="urn:o urn:x" processContents="skip"/>'
        accepted = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:o"/></root>', decl, tmp_path
        )
        assert not accepted.report.has_errors
        rejected = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:y"/></root>', decl, tmp_path
        )
        assert "unexpected-element" in [i.code for i in rejected.report.issues]

    def test_strict_without_declaration_is_reported(self, tmp_path):
        decl = '<xs:any namespace="##other" processContents="strict"/>'
        parser = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:o"/></root>', decl, tmp_path
        )
        assert "wildcard-no-declaration" in [i.code for i in parser.report.issues]

    def test_strict_with_declaration_validates(self, tmp_path):
        decl = '<xs:any namespace="##other" processContents="strict"/>'
        parser = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:o">7</extra></root>',
            decl,
            tmp_path,
            other=_OTHER_DECL,
        )
        assert not parser.report.has_errors
        assert int(parser.schemaRootInstance.extra) == 7
        bad = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:o">x</extra></root>',
            decl,
            tmp_path,
            other=_OTHER_DECL,
        )
        assert "value" in [i.code for i in bad.report.issues]

    def test_lax_without_declaration_binds_generically(self, tmp_path):
        decl = '<xs:any namespace="##other" processContents="lax"/>'
        parser = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:o"/></root>', decl, tmp_path
        )
        assert "wildcard-no-declaration" not in [i.code for i in parser.report.issues]
        assert len(parser.schemaRootInstance._children_) == 1

    def test_skip_without_declaration_is_clean(self, tmp_path):
        decl = '<xs:any namespace="##other" processContents="skip"/>'
        parser = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:o"/></root>', decl, tmp_path
        )
        assert not parser.report.has_errors

    def test_legacy_mode_ignores_namespace_constraint(self, tmp_path):
        decl = '<xs:any namespace="##local" processContents="skip"/>'
        parser = _wildcard_parse(
            '<root xmlns="urn:t"><extra xmlns="urn:o"/></root>',
            decl,
            tmp_path,
            mode=ParseModes.STRICT,
        )
        assert not parser.report.has_errors


class TestWildcardAttributes:
    """xs:anyAttribute namespace constraint and processContents."""

    def _schema(self, any_attr: str) -> str:
        return f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t"
            targetNamespace="urn:t" elementFormDefault="qualified">
          <xs:complexType name="Holder">
            {any_attr}
          </xs:complexType>
          <xs:element name="root" type="t:Holder"/>
        </xs:schema>"""

    def _parse(self, any_attr: str, instance: str, tmp_path):
        schema_path = tmp_path / "schema.xsd"
        schema_path.write_text(self._schema(any_attr))
        instance_path = tmp_path / "instance.xml"
        instance_path.write_text(instance)
        return PyXSD(
            instance_path,
            xsdFile=schema_path,
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
            mode=ParseModes.NAMESPACED,
        )

    def test_other_attribute_passes(self, tmp_path):
        decl = '<xs:anyAttribute namespace="##other" processContents="skip"/>'
        accepted = self._parse(decl, '<root xmlns="urn:t" xmlns:o="urn:o" o:x="1"/>', tmp_path)
        assert not accepted.report.has_errors
        assert accepted.schemaRootInstance._attribs_["{urn:o}x"] == "1"

    def test_target_namespace_attribute_rejects_foreign(self, tmp_path):
        decl = '<xs:anyAttribute namespace="##targetNamespace" processContents="skip"/>'
        rejected = self._parse(decl, '<root xmlns="urn:t" xmlns:o="urn:o" o:x="1"/>', tmp_path)
        assert "{urn:o}x" not in rejected.schemaRootInstance._attribs_

    def test_strict_attribute_requires_declaration(self, tmp_path):
        decl = '<xs:anyAttribute namespace="##other" processContents="strict"/>'
        parser = self._parse(decl, '<root xmlns="urn:t" xmlns:o="urn:o" o:x="1"/>', tmp_path)
        assert "wildcard-no-declaration" in [i.code for i in parser.report.issues]


class TestQNameValueSemantics:
    """xs:QName values compare in the expanded-name value space."""

    def test_value_key_uses_expanded_name(self):
        with qname_context({"tns": "urn:t"}):
            value = QName("tns:a")
        assert xsd_value_key(value) == ("QName", ("urn:t", "a"))

    def test_prefix_spellings_for_same_uri_compare_equal(self):
        with qname_context({"p": "urn:t", "q": "urn:t"}):
            first = QName("p:a")
            second = QName("q:a")
        assert xsd_comparable_key(first) == xsd_comparable_key(second)

    def test_unprefixed_qname_uses_default_namespace(self):
        with qname_context({"": "urn:t"}):
            value = QName("a")
        assert xsd_value_key(value) == ("QName", ("urn:t", "a"))

    def test_unprefixed_qname_has_no_namespace_without_default(self):
        with qname_context({"p": "urn:t"}):
            value = QName("a")
        assert xsd_value_key(value) == ("QName", (None, "a"))

    def test_value_without_context_falls_back_to_lexical(self):
        assert xsd_value_key(QName("p:a")) == ("QName", "p:a")


_QNAME_SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t"
    targetNamespace="urn:t" elementFormDefault="qualified">
  <xs:complexType name="Holder">
    <xs:sequence>
      <xs:element name="item" maxOccurs="unbounded">
        <xs:complexType>
          <xs:attribute name="ref" type="xs:QName" use="required"/>
        </xs:complexType>
      </xs:element>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="t:Holder">
    <xs:unique name="refUnique">
      <xs:selector xpath="t:item"/>
      <xs:field xpath="@ref"/>
    </xs:unique>
  </xs:element>
</xs:schema>"""


def _qname_parse(instance, tmp_path):
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(_QNAME_SCHEMA)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance)
    return PyXSD(
        instance_path,
        xsdFile=schema_path,
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
    )


class TestQNameIdentity:
    """Identity constraints use the QName value space, not the spelling."""

    def test_prefix_spellings_collide(self, tmp_path):
        parser = _qname_parse(
            '<root xmlns="urn:t" xmlns:p="urn:t" xmlns:q="urn:t">'
            '<item ref="p:a"/><item ref="q:a"/></root>',
            tmp_path,
        )
        assert "identity-unique" in [i.code for i in parser.report.issues]

    def test_distinct_names_do_not_collide(self, tmp_path):
        parser = _qname_parse(
            '<root xmlns="urn:t" xmlns:p="urn:t"><item ref="p:a"/><item ref="p:b"/></root>',
            tmp_path,
        )
        assert not parser.report.has_errors
        assert len(parser.schemaRootInstance._children_) == 2


_NS_SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t"
    targetNamespace="urn:t" elementFormDefault="qualified">
  <xs:complexType name="Holder">
    <xs:sequence>
      <xs:element name="a" type="xs:string"/>
      <xs:element name="note" type="xs:string" nillable="true" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="t:Holder"/>
</xs:schema>"""


def _write_instance(parser):
    output = StringIO()
    XmlTreeWriter(parser.schemaRootInstance, output)
    return output.getvalue()


class TestWriterNamespaces:
    """Writers emit prefixed names and ``xmlns`` declarations in strict mode."""

    def _parse(self, instance, tmp_path, *, mode=ParseModes.NAMESPACED):
        schema_path = tmp_path / "schema.xsd"
        schema_path.write_text(_NS_SCHEMA)
        instance_path = tmp_path / "instance.xml"
        instance_path.write_text(instance)
        return PyXSD(
            instance_path,
            xsdFile=schema_path,
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
            mode=mode,
        )

    def test_strict_round_trip_preserves_expanded_names(self, tmp_path):
        instance = f'<root xmlns="urn:t" xmlns:xsi="{XSI_NS}"><a>x</a><note xsi:nil="true"/></root>'
        parser = self._parse(instance, tmp_path)
        assert not parser.report.has_errors
        output = _write_instance(parser)

        reparsed = ET.fromstring(output)
        assert [element.tag for element in reparsed.iter()] == [
            "{urn:t}root",
            "{urn:t}a",
            "{urn:t}note",
        ]
        assert reparsed.find("{urn:t}note").get(f"{{{XSI_NS}}}nil") == "true"

    def test_strict_output_declares_namespaces(self, tmp_path):
        parser = self._parse('<root xmlns="urn:t"><a>x</a></root>', tmp_path)
        output = _write_instance(parser)
        assert "xmlns:ns0" in output
        assert "urn:t" in output
        assert "<ns0:root" in output
        assert "<ns0:a>" in output

    def test_xsi_prefix_is_bound_when_used(self, tmp_path):
        instance = f'<root xmlns="urn:t" xmlns:xsi="{XSI_NS}"><note xsi:nil="true"/></root>'
        parser = self._parse(instance, tmp_path)
        output = _write_instance(parser)
        assert "xmlns:xsi" in output
        assert "xsi:nil" in output

    def test_legacy_output_unchanged(self, tmp_path):
        parser = self._parse(
            '<root xmlns="urn:t"><a>x</a></root>',
            tmp_path,
            mode=ParseModes.STRICT,
        )
        output = _write_instance(parser)
        assert "ns0:" not in output
        assert "<root" in output
        assert "<a" in output
        assert "</a>" in output

    def test_xml_namespace_uses_reserved_prefix(self, tmp_path):
        """The reserved xml namespace keeps its implicit ``xml`` prefix.

        Binding it to a generated ``nsN`` prefix makes the output
        unparseable: XML reserves the ``xml`` prefix.
        """
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t"
            xmlns:xml="http://www.w3.org/XML/1998/namespace"
            targetNamespace="urn:t" elementFormDefault="qualified">
          <xs:complexType name="Holder">
            <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
            <xs:attribute ref="xml:space"/>
          </xs:complexType>
          <xs:element name="root" type="t:Holder"/>
        </xs:schema>"""
        schema_path = tmp_path / "schema.xsd"
        schema_path.write_text(schema)
        instance_path = tmp_path / "instance.xml"
        instance_path.write_text('<root xmlns="urn:t" xml:space="preserve"><a>x</a></root>')
        parser = PyXSD(
            instance_path,
            xsdFile=schema_path,
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
            mode=ParseModes.NAMESPACED,
        )
        assert not parser.report.has_errors
        output = _write_instance(parser)

        reparsed = ET.fromstring(output)
        assert reparsed.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"
        assert "xml:space" in output
        # The xml prefix is implicit in every document; redeclaring it
        # is redundant.
        assert "xmlns:xml" not in output


def _xsi_ref_schema(attribute_site: str) -> str:
    """A schema whose only extension hook is one ``xs:attribute`` site."""
    return f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:xsi="{XSI_NS}">
  <xs:import namespace="{XSI_NS}"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:decimal">
          {attribute_site}
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""


class TestWellKnownNamespaceImports:
    """Namespace-only imports of well-known namespaces resolve to built-ins.

    XSD 1.1 §4.2.3: the schema for the XML Schema instance namespace is
    available without a schema document, so a namespace-only ``xs:import``
    of it (or a bare ``ref`` into it) resolves to the built-in attribute
    declarations (``type``, ``nil``, ``schemaLocation``,
    ``noNamespaceSchemaLocation``). Unresolvable user namespaces keep the
    ``import-unresolved`` / ``unknown-attributeRef`` errors.
    """

    def test_import_xsi_namespace_without_location(self, tmp_path):
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}"
                   xmlns:xsi="{XSI_NS}">
          <xs:import namespace="{XSI_NS}"/>
          <xs:element name="root"><xs:complexType><xs:sequence><xs:element name="a"/></xs:sequence>
            <xs:attribute ref="xsi:type" default="xs:integer"/>
          </xs:complexType></xs:element>
        </xs:schema>"""
        instance = (
            f'<root xmlns:xsi="{XSI_NS}" xmlns:xs="{XSD_NS}" xsi:type="xs:integer">1<a/></root>'
        )
        parser = _strict_parse(schema, instance, tmp_path)
        codes = [i.code for i in parser.report.issues]
        assert "import-unresolved" not in codes
        assert "unknown-attributeRef" not in codes

    def test_xsi_ref_ok_without_import(self, tmp_path):
        """The built-in declarations are present even with no ``xs:import``."""
        schema = _xsi_ref_schema('<xs:attribute ref="xsi:nil"/>')
        parser = _strict_parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}">12.5</root>',
            tmp_path,
        )
        assert "unknown-attributeRef" not in [i.code for i in parser.report.issues]
        assert not parser.report.has_errors

    def test_xsi_type_default_is_ignored_when_attribute_absent(self, tmp_path):
        """A defaulted ``xsi:type`` is never applied (complex004.n2 shape).

        The instance does not even bind the ``xs`` prefix, so an applied
        default QName would be unresolvable; the value stays a decimal.
        """
        schema = _xsi_ref_schema('<xs:attribute ref="xsi:type" default="xs:integer"/>')
        parser = _strict_parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}">123.456</root>',
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_xsi_attribute_fixed_enforced_when_present(self, tmp_path):
        """A present ``xsi:type`` must match ``fixed`` (complex005.n1 shape)."""
        schema = _xsi_ref_schema('<xs:attribute ref="xsi:type" fixed="xs:integer"/>')
        parser = _strict_parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}" xmlns:xs="{XSD_NS}" xsi:type="xs:short">1234</root>',
            tmp_path,
        )
        assert "fixed-attribute" in [i.code for i in parser.report.issues]

    def test_xsi_type_fixed_present_match_reports_no_fixed_error(self, tmp_path):
        """A matching fixed value is not a fixed violation (complex005.v1).

        The corpus pins the remaining ``xsi:type`` rejection to the
        derivation rule (a simple type is not validly derived from the
        complex declared type), which is dispatch territory — not a
        ``fixed-attribute`` mismatch.
        """
        schema = _xsi_ref_schema('<xs:attribute ref="xsi:type" fixed="xs:integer"/>')
        parser = _strict_parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}" xmlns:xs="{XSD_NS}" xsi:type="xs:integer">1234</root>',
            tmp_path,
        )
        codes = [i.code for i in parser.report.issues]
        assert "xsi-type" in codes
        assert "fixed-attribute" not in codes

    def test_xsi_nil_fixed_ignored_when_attribute_absent(self, tmp_path):
        """A fixed ``xsi:nil`` is not applied to an absent attribute.

        complex007 shape: applying it would nil the element and demand
        empty content; the value stays decimal.
        """
        schema = _xsi_ref_schema('<xs:attribute ref="xsi:nil" fixed="true"/>')
        parser = _strict_parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}">12.5</root>',
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_xsi_nil_fixed_present_enforced(self, tmp_path):
        """A present ``xsi:nil`` must match ``fixed``."""
        schema = _xsi_ref_schema('<xs:attribute ref="xsi:nil" fixed="true"/>')
        parser = _strict_parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}" xsi:nil="false">12.5</root>',
            tmp_path,
        )
        assert "fixed-attribute" in [i.code for i in parser.report.issues]

    def test_xsi_attribute_required_enforced(self, tmp_path):
        """``use="required"`` reads the instance's xsi attribute (complex009)."""
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:xsi="{XSI_NS}">
          <xs:import namespace="{XSI_NS}"/>
          <xs:complexType name="B">
            <xs:sequence><xs:element name="e" minOccurs="0" maxOccurs="5"/></xs:sequence>
            <xs:attribute ref="xsi:type" use="required"/>
          </xs:complexType>
          <xs:element name="root" type="B"/>
        </xs:schema>"""
        parser = _strict_parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}"><e/></root>',
            tmp_path,
        )
        assert "missing-attribute" in [i.code for i in parser.report.issues]

    def test_xsi_attribute_required_satisfied_when_present(self, tmp_path):
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:xsi="{XSI_NS}">
          <xs:import namespace="{XSI_NS}"/>
          <xs:complexType name="B">
            <xs:sequence><xs:element name="e" minOccurs="0" maxOccurs="5"/></xs:sequence>
            <xs:attribute ref="xsi:type" use="required"/>
          </xs:complexType>
          <xs:element name="root" type="B"/>
        </xs:schema>"""
        parser = _strict_parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}" xmlns:xs="{XSD_NS}" xsi:type="B"><e/></root>',
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_user_namespace_attribute_ref_still_unresolved(self, tmp_path):
        """No loosening: a user namespace keeps both failures."""
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:o="urn:o">
          <xs:import namespace="urn:o"/>
          <xs:element name="root">
            <xs:complexType>
              <xs:attribute ref="o:thing"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
        parser = _strict_parse(schema, "<root/>", tmp_path)
        codes = [i.code for i in parser.report.issues]
        assert "unknown-attributeRef" in codes
        assert any(
            i.code == "import-unresolved" and i.severity is IssueSeverity.ERROR
            for i in parser.report.issues
        )


class TestUnknownXsiAttributes:
    """Only the four built-in xsi attributes are special (attMd001-011).

    An attribute in the schema-instance namespace whose local name is
    not ``type``/``nil``/``schemaLocation``/``noNamespaceSchemaLocation``
    is an ordinary attribute and must be declared or admitted by a
    wildcard; it is not silently accepted because of its namespace.
    """

    def test_unknown_xsi_attribute_on_anytype_root_is_rejected(self, tmp_path):
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}">
          <xs:element name="doc" type="xs:anyType"/>
        </xs:schema>"""
        parser = _strict_parse(
            schema,
            f'<doc xmlns:xs="{XSD_NS}" xmlns:xsi="{XSI_NS}" xsi:Type="xs:int">1</doc>',
            tmp_path,
        )
        assert "unexpected-attribute" in [i.code for i in parser.report.issues]

    def test_unknown_xsi_attribute_case_variant_on_anytype_root_is_rejected(self, tmp_path):
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}">
          <xs:element name="doc" type="xs:anyType"/>
        </xs:schema>"""
        parser = _strict_parse(
            schema,
            f'<doc xmlns:xs="{XSD_NS}" xmlns:xsi="{XSI_NS}" xsi:Nil="false">1</doc>',
            tmp_path,
        )
        assert "unexpected-attribute" in [i.code for i in parser.report.issues]

    def test_unknown_xsi_attribute_with_child_is_rejected(self, tmp_path):
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}">
          <xs:element name="doc" type="xs:anyType"/>
        </xs:schema>"""
        parser = _strict_parse(
            schema,
            f'<doc xmlns:xs="{XSD_NS}" xmlns:xsi="{XSI_NS}" xsi:Type="xs:int">'
            f'<e xsi:SchemaLocation="foo foo.xsd"/></doc>',
            tmp_path,
        )
        assert "unexpected-attribute" in [i.code for i in parser.report.issues]

    def test_unknown_xsi_attribute_on_simple_element_is_rejected(self, tmp_path):
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}">
          <xs:element name="root">
            <xs:complexType>
              <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
        parser = _strict_parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}"><a xsi:blah="x">v</a></root>',
            tmp_path,
        )
        assert "unexpected-attribute" in [i.code for i in parser.report.issues]

    def test_builtin_xsi_attributes_remain_admitted(self, tmp_path):
        """A legitimate ``xsi:type`` is not reported as undeclared."""
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="urn:t"
            targetNamespace="urn:t" elementFormDefault="qualified">
          <xs:complexType name="A"><xs:sequence/></xs:complexType>
          <xs:element name="root" type="xs:anyType"/>
        </xs:schema>"""
        parser = _strict_parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}" xmlns:t="urn:t" xsi:type="t:A"/>',
            tmp_path,
        )
        assert "unexpected-attribute" not in [i.code for i in parser.report.issues]

    def test_unknown_xsi_attribute_admitted_by_xsi_wildcard(self, tmp_path):
        """A wildcard that admits the xsi namespace still admits it (wild042)."""
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}">
          <xs:complexType name="computer">
            <xs:sequence/>
            <xs:anyAttribute namespace="{XSI_NS}" processContents="skip"/>
          </xs:complexType>
          <xs:element name="computer" type="computer"/>
        </xs:schema>"""
        parser = _strict_parse(
            schema,
            f'<computer xmlns:xsi="{XSI_NS}" xsi:banana="1234"/>',
            tmp_path,
        )
        assert "unexpected-attribute" not in [i.code for i in parser.report.issues]
        assert not parser.report.has_errors


def _xlink_ref_schema(attribute_site: str, *, import_line: str = "") -> str:
    """A schema whose only extension hook is one XLink ``xs:attribute`` site."""
    return f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:xlink="{XLINK_NS}">
{import_line}
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence><xs:element name="a" type="xs:string" minOccurs="0"/></xs:sequence>
      {attribute_site}
    </xs:complexType>
  </xs:element>
</xs:schema>"""


class TestXLinkBuiltins:
    """The XLink attribute declarations are built into the namespace.

    XSD 1.1 §4.2.3 resolves a namespace name to a schema for it; the
    XLink 1.0 vocabulary is available without loading ``xlink.xsd``, so a
    bare ``ref`` into the namespace and a namespace-only ``xs:import``
    both resolve, exactly as for ``xml:*``/``xsi:*``.
    """

    def test_xlink_type_ref_resolves_without_import(self, tmp_path):
        parser = _strict_parse(
            _xlink_ref_schema('<xs:attribute ref="xlink:type" default="locator"/>'),
            f'<root xmlns:xlink="{XLINK_NS}" xlink:type="locator"><a>x</a></root>',
            tmp_path,
        )
        assert "unknown-attributeRef" not in [i.code for i in parser.report.issues]
        assert not parser.report.has_errors

    def test_xlink_href_ref_resolves_without_import(self, tmp_path):
        parser = _strict_parse(
            _xlink_ref_schema('<xs:attribute ref="xlink:href"/>'),
            f'<root xmlns:xlink="{XLINK_NS}" xlink:href="#a"><a>x</a></root>',
            tmp_path,
        )
        assert "unknown-attributeRef" not in [i.code for i in parser.report.issues]
        assert not parser.report.has_errors

    def test_import_xlink_namespace_without_location(self, tmp_path):
        parser = _strict_parse(
            _xlink_ref_schema(
                '<xs:attribute ref="xlink:href"/>',
                import_line=f'  <xs:import namespace="{XLINK_NS}"/>',
            ),
            f'<root xmlns:xlink="{XLINK_NS}" xlink:href="#a"><a>x</a></root>',
            tmp_path,
        )
        codes = [i.code for i in parser.report.issues]
        assert "import-unresolved" not in codes
        assert "unknown-attributeRef" not in codes

    def test_user_declaration_in_xlink_namespace_is_allowed(self, tmp_path):
        """User components in the XLink namespace follow the xml rules.

        The XLink namespace is not reserved like ``xsi``: a schema may
        target it and declare new components, and those coexist with the
        injected built-ins without an ``unknown-attributeRef`` or a
        spurious ``declaration-duplicate``.
        """
        schema = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:xlink="{XLINK_NS}"
            targetNamespace="{XLINK_NS}" attributeFormDefault="qualified">
          <xs:attribute name="hreflang" type="xs:string"/>
          <xs:complexType name="T">
            <xs:attribute ref="xlink:type"/>
            <xs:attribute ref="xlink:hreflang"/>
          </xs:complexType>
          <xs:element name="root" type="xlink:T"/>
        </xs:schema>"""
        parser = _strict_parse(
            schema,
            f'<xlink:root xmlns:xlink="{XLINK_NS}" xlink:type="locator" xlink:hreflang="en"/>',
            tmp_path,
        )
        codes = [i.code for i in parser.report.issues]
        assert "unknown-attributeRef" not in codes
        assert "declaration-duplicate" not in codes
        assert not parser.report.has_errors


class TestMixedAttributeConflict:
    """``complexType/@mixed`` and ``complexContent/@mixed`` must agree.

    XSD 1.1 complex type XML representation: when both attributes are
    present their values must be identical (complex002). An attribute
    on only one of the two keeps the historical override reading.
    """

    @staticmethod
    def _schema(type_open: str, content_open: str) -> str:
        return f"""<xs:schema xmlns:xs="{XSD_NS}">
  <xs:complexType name="t1" mixed="true">
    <xs:sequence><xs:element name="a" type="xs:string" minOccurs="0"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="t2"{type_open}>
    <xs:complexContent{content_open}>
      <xs:restriction base="t1">
        <xs:sequence><xs:element name="a" type="xs:string" minOccurs="0"/></xs:sequence>
      </xs:restriction>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="root" type="t2"/>
</xs:schema>"""

    def test_conflicting_mixed_attributes_rejected(self, tmp_path):
        parser = _strict_parse(
            self._schema(' mixed="true"', ' mixed="0"'),
            "<root><a>x</a></root>",
            tmp_path,
        )
        issues = parser.report.issues
        assert any(i.code == "declaration-attribute" for i in issues)

    def test_agreeing_mixed_attributes_accepted(self, tmp_path):
        parser = _strict_parse(
            self._schema(' mixed="true"', ' mixed="true"'),
            "<root><a>x</a></root>",
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_single_sided_mixed_attribute_accepted(self, tmp_path):
        parser = _strict_parse(
            self._schema("", ' mixed="true"'),
            "<root><a>x</a></root>",
            tmp_path,
        )
        assert not parser.report.has_errors
