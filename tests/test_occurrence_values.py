"""Tests for occurrence and value semantics (Phase 8).

Covers attribute/element ``default`` and ``fixed``, ``nillable`` with
``xsi:nil``, substitution groups (with element references), ``xsi:type``
dispatch, and the abstract/block/final guards.
"""

import xml.etree.ElementTree as ET

from conftest import FIXTURES_DIR
from pyxsd.parser import PyXSD

XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


def _parse(schema_text, instance_text, tmp_path):
    """Parses an inline instance against an inline schema."""
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(schema_text)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance_text)
    return PyXSD(
        instance_path,
        xsdFile=schema_path,
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
    )


def _root_instance(parser):
    """Rebuilds the parser's root instance the way parseXML does."""
    import pyxsd.element_representatives.element_representative as ermod

    schemaER = ermod.registry["schema"][0]
    root_name = parser.xmlRoot.tag.split("}")[-1]
    root_er = next(
        (element for element in schemaER.elements if element.name == root_name),
        schemaER.elements[0],
    )
    sub_cls = root_er.getType()
    return sub_cls.makeInstanceFromTag(parser.xmlRoot)


# ---------------------------------------------------------------------------
# Attribute and element defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_absent_optional_attribute_takes_default(self):
        parser = PyXSD(
            FIXTURES_DIR / "defaults" / "instance.xml",
            xsdFile=FIXTURES_DIR / "defaults" / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        instance = _root_instance(parser)
        # 'mode' is absent from the instance document; the schema
        # default supplies its typed value.
        assert str(instance.mode) == "standard"

    def test_default_is_not_injected_into_written_output(self, tmp_path):
        """Default application is programmatic only; output stays faithful."""
        output = tmp_path / "parsed.xml"
        PyXSD(
            FIXTURES_DIR / "defaults" / "instance.xml",
            xsdFile=FIXTURES_DIR / "defaults" / "schema.xsd",
            xmlFileOutput=str(output),
            transformOutputName="_No_Output_",
        )
        tree = ET.parse(output)
        assert "mode" not in tree.getroot().attrib

    def test_empty_element_takes_default_value(self):
        parser = PyXSD(
            FIXTURES_DIR / "defaults" / "instance.xml",
            xsdFile=FIXTURES_DIR / "defaults" / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        instance = _root_instance(parser)
        volume = instance._children_[0]
        assert volume._name_ == "volume"
        assert str(volume) == "5"


class TestAttributeFixed:
    def test_matching_fixed_attribute_is_accepted(self):
        parser = PyXSD(
            FIXTURES_DIR / "defaults" / "instance.xml",
            xsdFile=FIXTURES_DIR / "defaults" / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        instance = _root_instance(parser)
        assert str(instance.key) == "k-1"

    def test_conflicting_fixed_attribute_is_reported(self, tmp_path):
        parser = _parse(
            (FIXTURES_DIR / "defaults" / "schema.xsd").read_text(),
            '<settings key="WRONG"><volume/><label>fixture</label></settings>',
            tmp_path,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "fixed-attribute" in codes

    def test_absent_fixed_attribute_takes_the_fixed_value(self, tmp_path):
        parser = _parse(
            (FIXTURES_DIR / "defaults" / "schema.xsd").read_text(),
            "<settings><volume/><label>fixture</label></settings>",
            tmp_path,
        )
        instance = _root_instance(parser)
        assert str(instance.key) == "k-1"


class TestElementFixed:
    def test_conflicting_fixed_element_is_reported(self, tmp_path):
        parser = _parse(
            (FIXTURES_DIR / "defaults" / "schema.xsd").read_text(),
            '<settings key="k-1"><volume/><label>WRONG</label></settings>',
            tmp_path,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "fixed-element" in codes

    def test_nonempty_element_keeps_its_value(self, tmp_path):
        parser = _parse(
            (FIXTURES_DIR / "defaults" / "schema.xsd").read_text(),
            '<settings key="k-1"><volume>9</volume><label>fixture</label></settings>',
            tmp_path,
        )
        volume = _root_instance(parser)._children_[0]
        assert str(volume) == "9"


# ---------------------------------------------------------------------------
# nillable / xsi:nil
# ---------------------------------------------------------------------------


class TestNillable:
    def test_nillable_element_with_nil(self):
        parser = PyXSD(
            FIXTURES_DIR / "nillable" / "instance.xml",
            xsdFile=FIXTURES_DIR / "nillable" / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        instance = _root_instance(parser)
        value = instance._children_[0]
        assert value._name_ == "value"
        assert value._value_ is None
        assert value._attribs_["xsi:nil"] == "true"

    def test_nil_attribute_survives_the_write(self, tmp_path):
        output = tmp_path / "parsed.xml"
        PyXSD(
            FIXTURES_DIR / "nillable" / "instance.xml",
            xsdFile=FIXTURES_DIR / "nillable" / "schema.xsd",
            xmlFileOutput=str(output),
            transformOutputName="_No_Output_",
        )
        tree = ET.parse(output)
        nil_attr = f"{{{XSI_NS}}}nil"
        assert tree.getroot()[0].attrib[nil_attr] == "true"

    def test_nil_on_non_nillable_element_is_reported(self, tmp_path):
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t">'
            '<xs:sequence><xs:element name="v" type="xs:integer"/></xs:sequence>'
            "</xs:complexType>"
            '<xs:element name="root" type="t"/>'
            "</xs:schema>"
        )
        parser = _parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}"><v xsi:nil="true"/></root>',
            tmp_path,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "nil" in codes


# ---------------------------------------------------------------------------
# substitution groups
# ---------------------------------------------------------------------------


class TestSubstitutionGroups:
    def test_member_parsed_with_its_own_type(self):
        parser = PyXSD(
            FIXTURES_DIR / "substitution" / "instance.xml",
            xsdFile=FIXTURES_DIR / "substitution" / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        instance = _root_instance(parser)
        dot = instance._children_[1]
        assert dot._name_ == "dot"
        assert str(dot) == "3"  # xs:integer member of a string head

    def test_member_without_own_type_uses_head_type(self):
        parser = PyXSD(
            FIXTURES_DIR / "substitution" / "instance.xml",
            xsdFile=FIXTURES_DIR / "substitution" / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        instance = _root_instance(parser)
        peg = instance._children_[2]
        assert peg._name_ == "peg"
        assert str(peg) == "end"

    def test_undeclared_head_is_a_schema_error(self, tmp_path):
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root">'
            "<xs:complexType><xs:sequence>"
            '<xs:element ref="marker" minOccurs="0"/>'
            "</xs:sequence></xs:complexType>"
            "</xs:element>"
            '<xs:element name="member" substitutionGroup="nope"/>'
            "</xs:schema>"
        )
        parser = _parse(schema, "<root/>", tmp_path)
        codes = [issue.code for issue in parser.report.issues]
        assert "unknown-substitution-head" in codes


def test_substitution_round_trip_preserves_member_names(tmp_path):
    output = tmp_path / "parsed.xml"
    PyXSD(
        FIXTURES_DIR / "substitution" / "instance.xml",
        xsdFile=FIXTURES_DIR / "substitution" / "schema.xsd",
        xmlFileOutput=str(output),
        transformOutputName="_No_Output_",
    )
    tree = ET.parse(output)
    names = [child.tag for child in tree.getroot()]
    assert names == ["marker", "dot", "peg"]


# ---------------------------------------------------------------------------
# xsi:type dispatch
# ---------------------------------------------------------------------------


class TestXsiTypeDispatch:
    def test_child_dispatches_to_derived_type(self):
        parser = PyXSD(
            FIXTURES_DIR / "xsi_type" / "instance.xml",
            xsdFile=FIXTURES_DIR / "xsi_type" / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        instance = _root_instance(parser)
        first_item = instance._children_[0]
        child_names = [child._name_ for child in first_item._children_]
        # specialValueType has num AND tag; baseValueType has only num.
        assert child_names == ["num", "tag"]

    def test_second_item_keeps_declared_type(self):
        parser = PyXSD(
            FIXTURES_DIR / "xsi_type" / "instance.xml",
            xsdFile=FIXTURES_DIR / "xsi_type" / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
        instance = _root_instance(parser)
        second_item = instance._children_[1]
        child_names = [child._name_ for child in second_item._children_]
        assert child_names == ["num"]

    def test_root_element_dispatch(self, tmp_path):
        parser = _parse(
            (FIXTURES_DIR / "xsi_type" / "schema.xsd").read_text(),
            f'<holder xmlns:xsi="{XSI_NS}" xsi:type="specialValueType">'
            "<num>2</num><tag>t</tag></holder>",
            tmp_path,
        )
        instance = parser.parseXML()
        child_names = [child._name_ for child in instance._children_]
        assert child_names == ["num", "tag"]

    def test_unresolvable_xsi_type_is_reported(self, tmp_path):
        parser = _parse(
            (FIXTURES_DIR / "xsi_type" / "schema.xsd").read_text(),
            f'<holder xmlns:xsi="{XSI_NS}" xsi:type="missingType"><num>2</num></holder>',
            tmp_path,
        )
        codes = [issue.code for issue in parser.report.issues]
        assert "xsi-type" in codes


# ---------------------------------------------------------------------------
# abstract / block / final
# ---------------------------------------------------------------------------


def test_abstract_element_rejected_directly(tmp_path):
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:complexType name="t">'
        '<xs:sequence><xs:element ref="head" minOccurs="0"/></xs:sequence>'
        "</xs:complexType>"
        '<xs:element name="head" type="xs:string" abstract="true"/>'
        '<xs:element name="root" type="t"/>'
        "</xs:schema>"
    )
    parser = _parse(
        schema,
        f'<root xmlns:xsi="{XSI_NS}"><head>x</head></root>',
        tmp_path,
    )
    codes = [issue.code for issue in parser.report.issues]
    assert "abstract-element" in codes


def test_substitution_member_of_abstract_head_is_allowed(tmp_path):
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="head" type="xs:string" abstract="true"/>'
        '<xs:element name="member" substitutionGroup="head"/>'
        '<xs:complexType name="t">'
        '<xs:sequence><xs:element ref="head"/></xs:sequence>'
        "</xs:complexType>"
        '<xs:element name="root" type="t"/>'
        "</xs:schema>"
    )
    parser = _parse(schema, "<root><member>ok</member></root>", tmp_path)
    assert not parser.report.has_errors


def test_abstract_complex_type_rejected_directly(tmp_path):
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:complexType name="base" abstract="true">'
        '<xs:sequence><xs:element name="v" type="xs:integer"/></xs:sequence>'
        "</xs:complexType>"
        '<xs:element name="root" type="base"/>'
        "</xs:schema>"
    )
    parser = _parse(schema, "<root><v>1</v></root>", tmp_path)
    codes = [issue.code for issue in parser.report.issues]
    assert "abstract-type" in codes
    # parsing still completes non-fatally
    assert _root_instance(parser) is not None


def test_derived_type_of_abstract_base_is_allowed(tmp_path):
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:complexType name="base" abstract="true">'
        '<xs:sequence><xs:element name="v" type="xs:integer"/></xs:sequence>'
        "</xs:complexType>"
        '<xs:complexType name="derived">'
        "<xs:complexContent>"
        '<xs:extension base="base">'
        '<xs:sequence><xs:element name="w" type="xs:string"/></xs:sequence>'
        "</xs:extension>"
        "</xs:complexContent>"
        "</xs:complexType>"
        '<xs:element name="root" type="derived"/>'
        "</xs:schema>"
    )
    parser = _parse(schema, "<root><v>1</v><w>x</w></root>", tmp_path)
    assert not parser.report.has_errors


def test_final_blocks_derivation(tmp_path):
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:complexType name="base" final="#all">'
        '<xs:sequence><xs:element name="v" type="xs:integer"/></xs:sequence>'
        "</xs:complexType>"
        '<xs:complexType name="derived">'
        "<xs:complexContent>"
        '<xs:extension base="base">'
        '<xs:sequence><xs:element name="w" type="xs:string"/></xs:sequence>'
        "</xs:extension>"
        "</xs:complexContent>"
        "</xs:complexType>"
        '<xs:element name="root" type="derived"/>'
        "</xs:schema>"
    )
    parser = _parse(schema, "<root><v>1</v><w>x</w></root>", tmp_path)
    codes = [issue.code for issue in parser.report.issues]
    assert "final" in codes


def test_blocked_substitution_member_rejected(tmp_path):
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="head" type="xs:string" block="substitution"/>'
        '<xs:element name="member" substitutionGroup="head"/>'
        '<xs:complexType name="t">'
        '<xs:sequence><xs:element ref="head" minOccurs="0"/></xs:sequence>'
        "</xs:complexType>"
        '<xs:element name="root" type="t"/>'
        "</xs:schema>"
    )
    parser = _parse(schema, "<root><member>x</member></root>", tmp_path)
    codes = [issue.code for issue in parser.report.issues]
    assert "blocked" in codes


def test_root_matching_no_global_element_is_reported(tmp_path):
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="other"/>'
        "</xs:schema>"
    )
    parser = _parse(schema, "<root/>", tmp_path)
    codes = [issue.code for issue in parser.report.issues]
    assert "unknown-root" in codes
