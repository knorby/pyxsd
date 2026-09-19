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
from pyxsd.binding import ParseModes
from pyxsd.element_representatives.element_representative import (
    ElementRepresentative,
    registry,
)
from pyxsd.parser import PyXSD
from pyxsd.validation import IssueSeverity

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


def _parse_text(schema, instance="<r/>"):
    """Parses an inline schema and instance from strings."""
    return PyXSD(
        StringIO(instance),
        xsdFile=StringIO(schema),
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
    )


def _schema_errors(report):
    return [issue for issue in report.for_phase("schema") if issue.severity is IssueSeverity.ERROR]


def _schema_warning_codes(report):
    return {
        issue.code
        for issue in report.for_phase("schema")
        if issue.severity is IssueSeverity.WARNING
    }


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

    def test_missing_include_file_is_warning(self, tmp_path):
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
        # XSD treats an unresolvable schemaLocation as a hint, not an
        # error: the schema is still valid and only a warning is raised.
        assert not _schema_errors(parser.report)
        assert "schema-compose" in _schema_warning_codes(parser.report)

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

    def test_circular_attribute_group_is_accepted(self):
        # attgC010: XSD 1.1 allows circular attribute group definitions.
        parser = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="test"><xs:attributeGroup ref="test"/></xs:complexType>'
            '<xs:attributeGroup name="test">'
            '<xs:attributeGroup ref="test"/>'
            '<xs:attribute name="foo" type="xs:int"/>'
            "</xs:attributeGroup>"
            '<xs:element name="T" type="test"/>'
            "</xs:schema>",
            '<T foo="3"/>',
        )
        assert not parser.report.has_errors
        assert parser.schemaRootInstance._attribs_["foo"] == "3"

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


# ---------------------------------------------------------------------------
# Unresolved-resource severity (Task 11)


class TestUnresolvedResourceSeverity:
    """A ``schemaLocation`` that cannot be retrieved is a hint, not an error.

    XSD treats an unresolvable schema reference as non-fatal: the schema
    remains valid and the parser reports a schema-phase warning. Rule
    violations in the composing document must still poison it.
    """

    def _parser(self, tmp_path, schema, files=None, mode=None):
        (tmp_path / "schema.xsd").write_text(schema, encoding="utf-8")
        for name, content in (files or {}).items():
            (tmp_path / name).write_text(content, encoding="utf-8")
        (tmp_path / "instance.xml").write_text("<root/>", encoding="utf-8")
        return PyXSD(
            tmp_path / "instance.xml",
            xsdFile=tmp_path / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
            mode=mode or ParseModes.NAMESPACED,
        )

    def test_missing_include_is_warning_not_error(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:include schemaLocation="0"/>'
            '<xs:element name="root"/></xs:schema>',
        )
        assert not _schema_errors(parser.report)
        assert "schema-compose" in _schema_warning_codes(parser.report)

    def test_missing_import_warns(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:import namespace="urn:x" schemaLocation="0"/>'
            '<xs:element name="root"/></xs:schema>',
        )
        assert not _schema_errors(parser.report)
        assert "import-unresolved" in _schema_warning_codes(parser.report)

    def test_missing_redefine_is_warning_not_error(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="0"/>'
            '<xs:element name="root"/></xs:schema>',
        )
        assert not _schema_errors(parser.report)
        assert "schema-compose" in _schema_warning_codes(parser.report)

    def test_namespace_only_import_is_warning(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:import namespace="urn:unused"/>'
            '<xs:element name="root"/></xs:schema>',
        )
        assert not _schema_errors(parser.report)
        assert "import-unresolved" in _schema_warning_codes(parser.report)

    def test_malformed_include_errors(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:include schemaLocation="bad.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"bad.xsd": "<xs:schema><not closed"},
        )
        assert "schema-compose" in {i.code for i in _schema_errors(parser.report)}

    def test_include_without_schema_location_is_still_an_error(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:include/><xs:element name="root"/></xs:schema>',
        )
        assert "schema-compose" in {i.code for i in _schema_errors(parser.report)}

    def test_redefine_without_schema_location_is_still_an_error(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine/><xs:element name="root"/></xs:schema>',
        )
        assert "schema-compose" in {i.code for i in _schema_errors(parser.report)}

    def test_include_namespace_mismatch_is_still_an_error(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">'
            '<xs:include schemaLocation="other.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"other.xsd": f'<xs:schema {XS} targetNamespace="urn:b"/>'},
        )
        assert "compose-namespace" in {i.code for i in _schema_errors(parser.report)}

    def test_redefine_with_content_and_missing_base_is_an_error(self, tmp_path):
        # Redefining a component requires the base document: a missing
        # base cannot be verified, so the schema is not valid.
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="0">'
            '<xs:group name="g"><xs:sequence><xs:element name="e"/>'
            "</xs:sequence></xs:group></xs:redefine>"
            '<xs:element name="root"/></xs:schema>',
        )
        assert "schema-compose" in {i.code for i in _schema_errors(parser.report)}

    def test_redefine_annotations_are_allowed_with_missing_base(self, tmp_path):
        # W3C annotB025: duplicate annotation on xs:redefine is valid, so
        # only the missing-resource warning remains.
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="0">'
            "<xs:annotation/><xs:annotation/></xs:redefine>"
            '<xs:element name="root"/></xs:schema>',
        )
        assert not _schema_errors(parser.report)
        assert "schema-compose" in _schema_warning_codes(parser.report)

    def test_include_duplicate_annotation_is_an_error(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:include schemaLocation="base.xsd">'
            "<xs:annotation/><xs:annotation/></xs:include>"
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": f"<xs:schema {XS}/>"},
        )
        assert "schema-compose" in {i.code for i in _schema_errors(parser.report)}

    def test_import_duplicate_annotation_is_an_error(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:import namespace="urn:b" schemaLocation="base.xsd">'
            "<xs:annotation/><xs:annotation/></xs:import>"
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": f'<xs:schema {XS} targetNamespace="urn:b"/>'},
        )
        assert "schema-compose" in {i.code for i in _schema_errors(parser.report)}

    def test_self_import_is_an_error(self, tmp_path):
        # Importing a schema's own target namespace is a rule violation;
        # with a reference into that namespace the unresolved import is
        # fatal rather than a hint.
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">'
            '<xs:import namespace="urn:a"/>'
            '<xs:complexType name="ct"><xs:sequence>'
            '<xs:element ref="a:e"/></xs:sequence></xs:complexType>'
            '<xs:element name="e" type="xs:string"/>'
            '<xs:element name="root" type="ct"/></xs:schema>',
        )
        assert "import-unresolved" in {i.code for i in _schema_errors(parser.report)}
        assert "import-unresolved" not in _schema_warning_codes(parser.report)

    def test_reference_into_unresolved_import_is_an_error(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS} xmlns:b="urn:b">'
            '<xs:import namespace="urn:b"/>'
            '<xs:element name="root">'
            "<xs:complexType><xs:sequence>"
            '<xs:element ref="b:x"/>'
            "</xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>",
        )
        assert "import-unresolved" in {i.code for i in _schema_errors(parser.report)}
        assert "import-unresolved" not in _schema_warning_codes(parser.report)

    def test_duplicate_id_on_import_and_declaration_is_an_error(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f"<xs:schema {XS}>"
            '<xs:import namespace="urn:b" id="a"/>'
            '<xs:group name="grp" id="a"><xs:sequence>'
            '<xs:element name="e"/></xs:sequence></xs:group>'
            '<xs:element name="root"/></xs:schema>',
        )
        assert "declaration-duplicate" in {i.code for i in _schema_errors(parser.report)}


# ---------------------------------------------------------------------------
# Composition and redefine legality (Task 12)


class TestComposeInvalid:
    """Structural ``xs:redefine`` / ``xs:import`` rule violations.

    These are the composition rules that hold regardless of whether the
    referenced resource resolves: a redefine must name a component that
    exists in the base, the same base component must not be redefined
    twice, an import's ``namespace`` must match the imported document,
    a redefine's base must share the redefining schema's namespace (or
    be a chameleon), and a chameleon self-reference must be qualified
    into the redefining namespace.
    """

    def _parser(self, tmp_path, schema, files=None):
        (tmp_path / "schema.xsd").write_text(schema, encoding="utf-8")
        for name, content in (files or {}).items():
            (tmp_path / name).write_text(content, encoding="utf-8")
        (tmp_path / "instance.xml").write_text("<root/>", encoding="utf-8")
        return PyXSD(
            tmp_path / "instance.xml",
            xsdFile=tmp_path / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
            mode=ParseModes.NAMESPACED,
        )

    def _error_codes(self, parser):
        return {i.code for i in _schema_errors(parser.report)}

    def test_redefine_of_missing_base_component_is_error(self, tmp_path):
        # The base document defines group ``g``; redefining group ``h``
        # would *add* a new component, which a redefine must not do.
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="h"><xs:sequence>'
            '<xs:element name="e"/></xs:sequence></xs:group>'
            '</xs:redefine><xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS}><xs:group name="g">'
                '<xs:sequence><xs:element name="e"/></xs:sequence>'
                "</xs:group></xs:schema>"
            },
        )
        assert "compose-invalid" in self._error_codes(parser)

    def test_redefine_duplicate_same_base_component_is_error(self, tmp_path):
        base = (
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="a"/></xs:sequence></xs:group>'
            "</xs:schema>"
        )
        redefiner = (
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:group ref="g"/><xs:element name="b"/>'
            "</xs:sequence></xs:group></xs:redefine></xs:schema>"
        )
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:include schemaLocation="a.xsd"/>'
            '<xs:include schemaLocation="b.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": base, "a.xsd": redefiner, "b.xsd": redefiner},
        )
        assert "compose-invalid" in self._error_codes(parser)

    def test_redefine_chain_is_not_duplicate(self, tmp_path):
        # A redefine may legitimately redefine a component that was itself
        # redefined by its own base document (a chain); only a second
        # redefine of the *same* base document is a conflict.
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="a.xsd">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:group ref="g"/><xs:element name="c"/>'
            "</xs:sequence></xs:group></xs:redefine>"
            '<xs:element name="root"/></xs:schema>',
            files={
                "a.xsd": f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
                '<xs:group name="g"><xs:sequence>'
                '<xs:group ref="g"/><xs:element name="b"/>'
                "</xs:sequence></xs:group></xs:redefine></xs:schema>",
                "base.xsd": f'<xs:schema {XS}><xs:group name="g">'
                '<xs:sequence><xs:element name="a"/></xs:sequence>'
                "</xs:group></xs:schema>",
            },
        )
        assert "compose-invalid" not in self._error_codes(parser)

    def test_import_namespace_mismatch_is_error(self, tmp_path):
        # An import with a namespace attribute cannot absorb a document
        # that declares no target namespace (that is an include).
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS} xmlns:b="urn:b">'
            '<xs:import namespace="urn:b" schemaLocation="base.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": f'<xs:schema {XS}><xs:element name="e"/></xs:schema>'},
        )
        assert "compose-invalid" in self._error_codes(parser)

    def test_import_namespace_match_is_valid(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS} xmlns:b="urn:b">'
            '<xs:import namespace="urn:b" schemaLocation="base.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS} targetNamespace="urn:b">'
                '<xs:element name="e"/></xs:schema>'
            },
        )
        assert "compose-invalid" not in self._error_codes(parser)

    def test_import_illegal_child_is_error(self, tmp_path):
        # notatF033: the only legal child of ``xs:import`` (and
        # ``xs:include``) is an annotation; a nested declaration such as
        # a notation is not a legal directive child.
        parser = self._parser(
            tmp_path,
            f"<xs:schema {XS}>"
            "<xs:import>"
            '<xs:notation name="jpeg" public="image/jpeg"/>'
            "</xs:import>"
            '<xs:element name="root"/></xs:schema>',
        )
        assert "declaration-child" in self._error_codes(parser)

    def test_import_annotation_child_is_valid(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f"<xs:schema {XS}>"
            '<xs:import schemaLocation="base.xsd">'
            "<xs:annotation/>"
            "</xs:import>"
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": f'<xs:schema {XS}><xs:element name="e"/></xs:schema>'},
        )
        assert "declaration-child" not in self._error_codes(parser)

    def test_redefine_base_namespace_mismatch_is_error(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="e"/></xs:sequence></xs:group>'
            '</xs:redefine><xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS} targetNamespace="urn:b">'
                '<xs:group name="g"><xs:sequence>'
                '<xs:element name="e"/></xs:sequence></xs:group>'
                "</xs:schema>"
            },
        )
        assert "compose-invalid" in self._error_codes(parser)

    def test_chameleon_redefine_unqualified_self_reference_is_error(self, tmp_path):
        # A no-namespace base redefined by a namespaced schema has its
        # component ported into the redefining namespace, so the self
        # reference must be qualified into it; an unqualified ``ref``
        # names no component there.
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:choice>'
            '<xs:group ref="g"/><xs:element name="b"/>'
            "</xs:choice></xs:group></xs:redefine>"
            '<xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS}><xs:group name="g">'
                '<xs:sequence><xs:element name="a"/></xs:sequence>'
                "</xs:group></xs:schema>"
            },
        )
        assert "compose-invalid" in self._error_codes(parser)

    def test_chameleon_redefine_qualified_self_reference_is_valid(self, tmp_path):
        parser = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:choice>'
            '<xs:group ref="a:g"/><xs:element name="b"/>'
            "</xs:choice></xs:group></xs:redefine>"
            '<xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS}><xs:group name="g">'
                '<xs:sequence><xs:element name="a"/></xs:sequence>'
                "</xs:group></xs:schema>"
            },
        )
        assert "compose-invalid" not in self._error_codes(parser)

    def _attribute_group_redefine(self, tmp_path, base_body, derived_body):
        return self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            f'<xs:attributeGroup name="ag">{derived_body}</xs:attributeGroup>'
            '</xs:redefine><xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS}><xs:attributeGroup name="ag">'
                f"{base_body}</xs:attributeGroup></xs:schema>"
            },
        )

    def test_attribute_group_redefine_adds_attribute_is_error(self, tmp_path):
        parser = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a" type="xs:string"/>',
            '<xs:attribute name="a" type="xs:string"/><xs:attribute name="b" type="xs:string"/>',
        )
        assert "compose-invalid" in self._error_codes(parser)

    def test_attribute_group_redefine_reorders_is_error(self, tmp_path):
        parser = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a"/><xs:attribute name="b"/>',
            '<xs:attribute name="b"/><xs:attribute name="a"/>',
        )
        assert "compose-invalid" in self._error_codes(parser)

    def test_attribute_group_redefine_drops_fixed_is_error(self, tmp_path):
        parser = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a" type="xs:string" fixed="x"/>',
            '<xs:attribute name="a" type="xs:string" default="x"/>',
        )
        assert "compose-invalid" in self._error_codes(parser)

    def test_attribute_group_redefine_self_reference_duplicate_is_error(self, tmp_path):
        parser = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a" type="xs:string"/>',
            '<xs:attributeGroup ref="ag"/><xs:attribute name="a" type="xs:string"/>',
        )
        assert "compose-invalid" in self._error_codes(parser)

    def test_attribute_group_redefine_valid_restriction_is_accepted(self, tmp_path):
        parser = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a"/><xs:attribute name="b"/>',
            '<xs:attribute name="a"/><xs:attribute name="b"/>',
        )
        assert "compose-invalid" not in self._error_codes(parser)

    def test_attribute_group_redefine_valid_extension_is_accepted(self, tmp_path):
        parser = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a" type="xs:string"/>',
            '<xs:attributeGroup ref="ag"/><xs:attribute name="b" type="xs:string"/>',
        )
        assert "compose-invalid" not in self._error_codes(parser)


# ---------------------------------------------------------------------------
# xs:override component replacement (Task 10)


class TestOverrideComposition:
    """XSD 1.1 ``xs:override`` replaces components wholesale.

    Unlike ``xs:redefine`` an override need not modify an existing
    component (it may match nothing, in which case it is ignored) and it
    may replace a component with one of a different kind in the same
    symbol space (a simple type for a complex type). References from
    inside the overriding component resolve to the override, not to the
    replaced base definition.
    """

    def _parser(self, tmp_path, schema, instance, files=None):
        (tmp_path / "schema.xsd").write_text(schema, encoding="utf-8")
        for name, content in (files or {}).items():
            (tmp_path / name).write_text(content, encoding="utf-8")
        (tmp_path / "instance.xml").write_text(instance, encoding="utf-8")
        return PyXSD(
            tmp_path / "instance.xml",
            xsdFile=tmp_path / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )

    def _error_codes(self, parser):
        return {i.code for i in parser.report.errors}

    def test_override_replaces_type_and_group(self, tmp_path):
        base = (
            f"<xs:schema {XS}>"
            '<xs:complexType name="T"><xs:sequence>'
            '<xs:element name="old" type="xs:string"/></xs:sequence></xs:complexType>'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/></xs:sequence></xs:group>'
            "</xs:schema>"
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:complexType name="T"><xs:sequence>'
            '<xs:group ref="g"/></xs:sequence></xs:complexType>'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="b" type="xs:string"/></xs:sequence></xs:group>'
            "</xs:override>"
            '<xs:element name="r" type="T"/>'
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<r><b/></r>", {"base.xsd": base})
        assert not parser.report.has_errors
        # The overriding type wholly replaces the base, which is dropped.
        assert "T" in parser.classes
        assert "T|base" not in parser.classes
        assert [d.name for d in parser.classes["T"]()._getElements()] == ["b"]

    def test_override_replaces_complex_type_with_simple_type(self, tmp_path):
        # over013: the override matches by symbol space, so a simpleType
        # may replace a complexType of the same name.
        base = (
            f"<xs:schema {XS}>"
            '<xs:complexType name="structuredDate"><xs:sequence>'
            '<xs:element name="year"/></xs:sequence></xs:complexType>'
            '<xs:element name="doc" type="structuredDate"/>'
            "</xs:schema>"
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:simpleType name="structuredDate">'
            '<xs:restriction base="xs:date"/></xs:simpleType>'
            "</xs:override>"
            '<xs:element name="r" type="structuredDate"/>'
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<r>2001-01-01</r>", {"base.xsd": base})
        assert not parser.report.has_errors

    def test_override_uses_component_added_by_the_overriding_document(self, tmp_path):
        # A brand-new component declared by the overriding schema (here a
        # simple type used by the overriding element) is available, as in
        # over028/over019.
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="smallInt"/>'
            "</xs:override>"
            '<xs:simpleType name="smallInt">'
            '<xs:restriction base="xs:int"><xs:maxInclusive value="16"/></xs:restriction>'
            "</xs:simpleType>"
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<doc>16</doc>", {"base.xsd": base})
        assert not parser.report.has_errors

    def test_override_matching_nothing_is_ignored_not_added(self, tmp_path):
        # over026: a declaration matching nothing in the target set is
        # silently ignored (XSD 1.1 §4.2.5), so it does not satisfy a
        # reference into it.
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:simpleType name="ghost">'
            '<xs:restriction base="xs:string"/></xs:simpleType>'
            "</xs:override>"
            '<xs:element name="r" type="ghost"/>'
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<r>x</r>", {"base.xsd": base})
        assert "unknown-type" in self._error_codes(parser)
        assert "override-invalid" not in self._error_codes(parser)

    def test_override_of_a_missing_component_is_legal(self, tmp_path):
        # The same as above but no reference to the ignored component:
        # overriding a component the target set lacks is not an error.
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:simpleType name="ghost">'
            '<xs:restriction base="xs:string"/></xs:simpleType>'
            "</xs:override>"
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<r>x</r>", {"base.xsd": base})
        assert "override-invalid" not in self._error_codes(parser)
        assert "compose-invalid" not in self._error_codes(parser)
        assert not parser.report.has_errors

    def test_override_self_reference_resolves_to_the_override(self, tmp_path):
        # over006: ``ref="section"`` inside the overriding declaration
        # names the overriding (recursive) element, not the base copy.
        base = (
            f"<xs:schema {XS}>"
            '<xs:element name="section">'
            "<xs:complexType><xs:sequence>"
            '<xs:element name="head" type="xs:string"/>'
            '<xs:element ref="section" minOccurs="1" maxOccurs="unbounded"/>'
            "</xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="section">'
            "<xs:complexType><xs:sequence>"
            '<xs:element name="head" type="xs:string"/>'
            '<xs:element ref="section" minOccurs="0" maxOccurs="unbounded"/>'
            "</xs:sequence>"
            '<xs:attribute name="nr" type="xs:decimal" use="required"/>'
            "</xs:complexType></xs:element>"
            "</xs:override>"
            "</xs:schema>"
        )
        parser = self._parser(
            tmp_path,
            schema,
            '<section nr="1"><head>a</head><section nr="2"><head>b</head></section></section>',
            {"base.xsd": base},
        )
        assert not parser.report.has_errors

    def test_illegal_override_child_is_override_invalid(self, tmp_path):
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:sequence><xs:element name="e"/></xs:sequence>'
            "</xs:override>"
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<r>x</r>", {"base.xsd": base})
        assert "override-invalid" in self._error_codes(parser)

    def test_duplicate_override_target_in_one_block_is_override_invalid(self, tmp_path):
        # over021: the same component named twice in one xs:override.
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:date"/>'
            '<xs:element name="doc" type="xs:time"/>'
            "</xs:override>"
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<doc>2001-01-01</doc>", {"base.xsd": base})
        assert "override-invalid" in self._error_codes(parser)

    def test_duplicate_override_across_blocks_is_override_invalid(self, tmp_path):
        # over022: the same base component overridden by two blocks.
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:date"/>'
            "</xs:override>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:time"/>'
            "</xs:override>"
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<doc>2001-01-01</doc>", {"base.xsd": base})
        assert "override-invalid" in self._error_codes(parser)

    def test_override_namespace_mismatch_is_error(self, tmp_path):
        # over016: a no-namespace overriding document may not override a
        # namespaced base document.
        base = f'<xs:schema {XS} targetNamespace="urn:b"><xs:element name="doc"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:string"/>'
            "</xs:override>"
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<doc>x</doc>", {"base.xsd": base})
        assert "compose-invalid" in self._error_codes(parser)

    def test_override_missing_base_with_content_is_error(self, tmp_path):
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="absent.xsd">'
            '<xs:element name="doc" type="xs:string"/>'
            "</xs:override>"
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<r>x</r>")
        assert "schema-compose" in self._error_codes(parser)

    def test_override_type_ignores_host_default_open_content(self, tmp_path):
        # open043: a type defined within xs:override takes its default open
        # content from the overridden document, not the overriding host.
        base = (
            f"<xs:schema {XS}>"
            '<xs:complexType name="beta"><xs:sequence/></xs:complexType>'
            '<xs:element name="doc" type="beta"/>'
            "</xs:schema>"
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:complexType name="beta"><xs:sequence/></xs:complexType>'
            "</xs:override>"
            '<xs:defaultOpenContent mode="suffix" appliesToEmpty="true">'
            '<xs:any namespace="urn:open" processContents="lax"/>'
            "</xs:defaultOpenContent>"
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>"
        )
        parser = self._parser(
            tmp_path, schema, '<doc><extra xmlns="urn:open"/></doc>', {"base.xsd": base}
        )
        # The override's beta does not inherit the host's default open
        # content, so the wildcard-admitted extra element is rejected.
        assert "unexpected-element" in {i.code for i in parser.report.errors}

    def test_nested_override(self, tmp_path):
        # top overrides mid, which overrides base; the innermost
        # declaration (top) wins.
        base = (
            f"<xs:schema {XS}>"
            '<xs:simpleType name="T"><xs:restriction base="xs:string"/></xs:simpleType>'
            "</xs:schema>"
        )
        mid = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:simpleType name="T"><xs:restriction base="xs:string">'
            '<xs:maxLength value="5"/></xs:restriction></xs:simpleType>'
            "</xs:override>"
            "</xs:schema>"
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="mid.xsd">'
            '<xs:simpleType name="T"><xs:restriction base="xs:string">'
            '<xs:maxLength value="3"/></xs:restriction></xs:simpleType>'
            "</xs:override>"
            '<xs:element name="r" type="T"/>'
            "</xs:schema>"
        )
        parser = self._parser(tmp_path, schema, "<r>abc</r>", {"base.xsd": base, "mid.xsd": mid})
        assert not parser.report.has_errors


def test_redefine_base_reference_keeps_its_namespace_prefix(tmp_path):
    """A redefine self-reference must stay in the redefining namespace.

    Dropping the prefix made ``base="a:c"`` rewrite to an unprefixed
    ``c|base``, which resolved through the default XML Schema namespace
    instead of the target namespace, so the renamed original could not be
    found (XSTS defaultAttributesApply s3_4_2_4ii03-ii07).
    """
    (tmp_path / "base.xsd").write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:a" xmlns:a="urn:a">'
        '<xs:complexType name="c"><xs:sequence/></xs:complexType>'
        '<xs:element name="root" type="a:c"/></xs:schema>'
    )
    (tmp_path / "main.xsd").write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:a" xmlns:a="urn:a" defaultAttributes="a:da">'
        '<xs:redefine schemaLocation="base.xsd">'
        '<xs:complexType name="c"><xs:complexContent>'
        '<xs:extension base="a:c"><xs:sequence/></xs:extension>'
        "</xs:complexContent></xs:complexType></xs:redefine>"
        '<xs:attributeGroup name="da">'
        '<xs:attribute name="extra" type="xs:boolean" use="required"/></xs:attributeGroup>'
        "</xs:schema>"
    )
    parser = PyXSD(
        StringIO('<a:root xmlns:a="urn:a"/>'),
        str(tmp_path / "main.xsd"),
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
    )
    codes = [issue.code for issue in parser.report.errors]
    assert "unknown-type" not in codes
    # The redefined type is declared in the host document, so the host's
    # default attribute group applies and the attribute is required.
    assert "missing-attribute" in codes
