"""Tests for namespace capture and namespace-aware parsing.

The default policy is ``legacy``; these tests exercise the capture
machinery directly and the strict-mode behaviour added on top of it.
"""

from io import StringIO

import pytest

from pyxsd.binding import ParseModes
from pyxsd.namespaces import (
    XSD_NS,
    NamespaceContext,
    NamespaceError,
    clark,
    local_name,
    namespace_of,
    parse_with_namespaces,
)
from pyxsd.parser import PyXSD


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
