"""Tests for schema composition (Phase 9).

Covers ``xs:include`` (including the chameleon case where the
included schema declares no target namespace), ``xs:import``
(namespace-light merging), ``xs:redefine`` (derived redefinition of
included components) and the error paths (missing files, cycles,
namespace mismatches, malformed included schemas).
"""

import pytest

from conftest import run_parser
from pyxsd.element_representatives.element_representative import (
    ElementRepresentative,
    registry,
)
from pyxsd.parser import PyXSD


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

    def test_include_cycle_is_detected(self, tmp_path):
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
        codes = [issue.code for issue in parser.report.errors]
        assert "compose-cycle" in codes

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
