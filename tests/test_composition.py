"""Tests for schema composition (Phase 9).

Covers ``xs:include`` (including the chameleon case where the
included schema declares no target namespace), ``xs:import``
(namespace-light merging), ``xs:redefine`` (derived redefinition of
included components) and the error paths (missing files, cycles,
namespace mismatches, malformed included schemas).
"""

from io import StringIO

import pytest

from conftest import run_parser
from pyxsd.element_representatives.element_representative import (
    ElementRepresentative,
    registry,
)
from pyxsd.parser import PyXSD

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


def _parse_text(schema, instance="<r/>"):
    """Parses an inline schema and instance from strings."""
    return PyXSD(
        StringIO(instance),
        xsdFile=StringIO(schema),
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
    )


class TestParserLocalRegistry:
    """Declarations from one parser must not leak into another (R14)."""

    def test_second_parser_does_not_reuse_first_parsers_types(self):
        first = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="B"><xs:sequence>'
            '<xs:element name="old" type="xs:string"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:element name="r1" type="B"/>'
            "</xs:schema>",
            "<r1><old/></r1>",
        )
        assert not first.report.has_errors

        second = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="B"><xs:sequence>'
            '<xs:element name="new" type="xs:string"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:complexType name="D"><xs:complexContent>'
            '<xs:extension base="B"/>'
            "</xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="D"/>'
            "</xs:schema>",
            "<r><new/></r>",
        )
        # The second schema's D extends the second schema's B, not the
        # first parser's B (which expected 'old').
        assert not second.report.has_errors

    def test_element_and_type_may_share_a_name(self):
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:element name="T" type="xs:string"/>'
            '<xs:complexType name="T"><xs:sequence>'
            '<xs:element name="v" type="xs:string"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:complexType name="D"><xs:complexContent>'
            '<xs:extension base="T"/>'
            "</xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="D"/>'
            "</xs:schema>",
            "<r><v/></r>",
        )
        assert not parser.report.has_errors
        assert "D" in parser.classes
        base_names = [base.__name__ for base in parser.classes["D"].__mro__]
        assert "T" in base_names


class TestComposeFixture:
    """The compose fixture: include + redefine + import in one schema."""

    @pytest.fixture()
    def parser(self):
        return run_parser("compose")

    def test_included_types_become_classes(self, parser):
        assert "commonType" in parser.classes
        assert "unitType" in parser.classes

    def test_redefine_creates_derived_class(self, parser):
        assert "widgetType" in parser.classes
        widget_cls = parser.classes["widgetType"]
        element_names = [descriptor.name for descriptor in widget_cls()._getElements()]
        assert element_names == ["name", "count"]
        assert list(widget_cls._elementNames_) == ["count"]

    def test_redefine_keeps_original_under_base_name(self, parser):
        original = ElementRepresentative.getFromName("widgetType|base")
        assert original is not None
        assert original.name == "widgetType|base"

    def test_redefined_class_derives_from_original(self, parser):
        widget_cls = parser.classes["widgetType"]
        base_names = [base.__name__ for base in widget_cls.__mro__]
        assert "widgetType|base" in base_names

    def test_imported_types_available(self, parser):
        assert "extraType" in parser.classes

    def test_valid_parse_has_clean_report(self, parser):
        assert not parser.report.has_errors

    def test_chameleon_include_components_are_used(self, parser):
        """The chameleon include's simpleType works as an attribute type."""
        assert registry.get("unitType") is not None

    def test_composition_tags_are_removed_from_schema_tree(self, parser):
        schema_er = registry["schema"][0]
        root_tags = {child.tag.split("}")[-1] for child in schema_er.xsdElement}
        assert "include" not in root_tags
        assert "redefine" not in root_tags
        assert "import" not in root_tags


class TestCompositionErrors:
    """Error paths of the composition machinery."""

    def test_missing_include_file_is_reported(self, tmp_path):
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="no-such-file.xsd"/>\n'
            '  <xs:element name="root"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text("<root/>")
        parser = PyXSD(
            tmp_path / "instance.xml",
            xsdFile=tmp_path / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        codes = [issue.code for issue in parser.report.errors]
        assert "schema-compose" in codes

    def test_malformed_include_is_reported(self, tmp_path):
        (tmp_path / "bad.xsd").write_text("<xs:schema><not closed")
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="bad.xsd"/>\n'
            '  <xs:element name="root"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text("<root/>")
        parser = PyXSD(
            tmp_path / "instance.xml",
            xsdFile=tmp_path / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        codes = [issue.code for issue in parser.report.errors]
        assert "schema-compose" in codes

    def test_include_cycle_is_deduplicated(self, tmp_path):
        (tmp_path / "a.xsd").write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="b.xsd"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "b.xsd").write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="a.xsd"/>\n'
            "</xs:schema>\n"
        )
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="a.xsd"/>\n'
            '  <xs:element name="root"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text("<root/>")
        parser = PyXSD(
            tmp_path / "instance.xml",
            xsdFile=tmp_path / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        # A legal include cycle is deduplicated rather than rejected:
        # the schema composes cleanly.
        assert not parser.report.has_errors

    def test_include_namespace_mismatch_is_reported(self, tmp_path):
        (tmp_path / "other.xsd").write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:other">\n'
            "</xs:schema>\n"
        )
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="other.xsd"/>\n'
            '  <xs:element name="root"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text("<root/>")
        parser = PyXSD(
            tmp_path / "instance.xml",
            xsdFile=tmp_path / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        codes = [issue.code for issue in parser.report.errors]
        assert "compose-namespace" in codes

    def test_import_of_schema_namespace_is_skipped(self, tmp_path):
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:import namespace="http://www.w3.org/2001/XMLSchema"/>\n'
            '  <xs:element name="root"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text("<root/>")
        parser = PyXSD(
            tmp_path / "instance.xml",
            xsdFile=tmp_path / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        assert len(parser.report) == 0


# ---------------------------------------------------------------------------
# Standalone include/import against hand-written schemas


class TestCompositionCorrectness:
    """R14 composition defects fixed alongside the parser-local table."""

    def test_element_ref_inside_a_group_resolves(self):
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:element name="a" type="xs:string"/>'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element ref="a"/>'
            "</xs:sequence></xs:group>"
            '<xs:element name="r"><xs:complexType>'
            '<xs:group ref="g"/>'
            "</xs:complexType></xs:element>"
            "</xs:schema>",
            "<r><a>x</a></r>",
        )
        assert not parser.report.has_errors
        root = parser.schemaRootInstance
        # The ref site adopts the global declaration's name, so the
        # child binds to the ``a`` descriptor.
        assert [descriptor.name for descriptor in root._getElements()] == ["a"]
        assert root.a == "x"

    def test_nested_attribute_group_references_merge(self):
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:attributeGroup name="g1">'
            '<xs:attribute name="a" type="xs:int"/>'
            "</xs:attributeGroup>"
            '<xs:attributeGroup name="g2">'
            '<xs:attributeGroup ref="g1"/>'
            "</xs:attributeGroup>"
            '<xs:element name="r"><xs:complexType>'
            '<xs:attributeGroup ref="g2"/>'
            "</xs:complexType></xs:element>"
            "</xs:schema>",
            '<r a="5"/>',
        )
        assert not parser.report.has_errors
        assert parser.schemaRootInstance._attribs_["a"] == "5"

    def test_import_without_schema_location_is_allowed(self):
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:import namespace="urn:unused"/>'
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>",
            "<r>hi</r>",
        )
        assert not parser.report.has_errors

    def test_single_level_group_redefine(self, tmp_path):
        (tmp_path / "base.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/>'
            "</xs:sequence></xs:group>"
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:group ref="g"/>'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:sequence></xs:group>"
            "</xs:redefine>"
            '<xs:element name="r"><xs:complexType>'
            '<xs:group ref="g"/>'
            "</xs:complexType></xs:element>"
            "</xs:schema>"
        )
        (tmp_path / "instance.xml").write_text("<r><a/><b/></r>")
        parser = PyXSD(
            tmp_path / "instance.xml",
            xsdFile=tmp_path / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        assert not parser.report.has_errors

    def test_single_level_attribute_group_redefine(self, tmp_path):
        (tmp_path / "base.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:attributeGroup name="ag">'
            '<xs:attribute name="a" type="xs:string"/>'
            "</xs:attributeGroup>"
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:attributeGroup name="ag">'
            '<xs:attributeGroup ref="ag"/>'
            '<xs:attribute name="b" type="xs:string"/>'
            "</xs:attributeGroup>"
            "</xs:redefine>"
            '<xs:element name="r"><xs:complexType>'
            '<xs:attributeGroup ref="ag"/>'
            "</xs:complexType></xs:element>"
            "</xs:schema>"
        )
        (tmp_path / "instance.xml").write_text('<r a="1" b="2"/>')
        parser = PyXSD(
            tmp_path / "instance.xml",
            xsdFile=tmp_path / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        assert not parser.report.has_errors
        assert parser.schemaRootInstance._attribs_ == {"a": "1", "b": "2"}
