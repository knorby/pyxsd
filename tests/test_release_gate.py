"""Release-gate coverage tests (Phase 14).

Targets library paths left uncovered by the feature phases: facet
element representatives, element-representative helpers, composition
error branches, union member resolution, repeated descriptor names,
forced-value validation, fixed-attribute checks, and transform-loading
fallbacks.
"""

import logging
import xml.etree.ElementTree as ET
from io import StringIO

import pytest

import pyxsd.element_representatives.element_representative as ermod
from pyxsd.binding import ParseModes
from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.exceptions import PyXSDError
from pyxsd.parser import PyXSD

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


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


def _schema_er():
    return ermod.registry["schema"][0]


# ---------------------------------------------------------------------------
# Facet element representatives
# ---------------------------------------------------------------------------


FACET_SCHEMA = f"""\
<xs:schema {XS}>
  <xs:simpleType name="boundedString">
    <xs:restriction base="xs:string">
      <xs:length value="4"/>
      <xs:pattern value="[a-z]+"/>
      <xs:enumeration value="abcd"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="rangeInt">
    <xs:restriction base="xs:int">
      <xs:minInclusive value="0"/>
      <xs:maxInclusive value="10"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="rangeIntExclusive">
    <xs:restriction base="xs:int">
      <xs:minExclusive value="-1"/>
      <xs:maxExclusive value="11"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="listType">
    <xs:list itemType="xs:int"/>
  </xs:simpleType>
  <xs:complexType name="derived">
    <xs:complexContent>
      <xs:extension base="rangeInt"/>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="boundedString"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


class TestFacetElementRepresentatives:
    def test_facets_are_recorded_on_the_simple_type(self, tmp_path):
        parser = _parse(FACET_SCHEMA, "<doc><a>abcd</a></doc>", tmp_path)
        assert not parser.report.has_errors

        simple = _schema_er().simpleTypes["boundedString"]
        assert simple.length == "4"
        assert simple.patterns == ["[a-z]+"]
        assert "abcd" in simple.enumerations

        range_type = _schema_er().simpleTypes["rangeInt"]
        assert range_type.minInclusive == "0"
        assert range_type.maxInclusive == "10"

        exclusive_type = _schema_er().simpleTypes["rangeIntExclusive"]
        assert exclusive_type.minExclusive == "-1"
        assert exclusive_type.maxExclusive == "11"

    def test_list_facet_records_item_type(self, tmp_path):
        _parse(FACET_SCHEMA, "<doc><a>abcd</a></doc>", tmp_path)
        list_type = _schema_er().simpleTypes["listType"]
        assert list_type.listItemType == "xs:int"
        # The List ER itself carries the marker type for later lookup.
        list_er = list_type.processedChildren[0]
        assert list_er.type == "xs:list"


# ---------------------------------------------------------------------------
# ElementRepresentative helpers
# ---------------------------------------------------------------------------


class TestElementRepresentativeHelpers:
    def _bare_er(self, name="probe"):
        element = ET.fromstring(
            f'<xs:element name="{name}" xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
        )
        return ElementRepresentative(element, None)

    def test_str_and_describe(self):
        er = self._bare_er()
        assert "ElementRepresentative[probe]" in str(er)
        described = er.describe()
        assert isinstance(described, str)
        assert "name -> probe" in described

    def test_get_containing_type_without_parent_errors(self, caplog):
        er = self._bare_er()
        assert er.getContainingType() is None
        assert er.getContainingTypeName() is None

    def test_type_from_name_unknown_prefixed_name(self, caplog):
        caplog.clear()
        assert ElementRepresentative.typeFromName("xs:noSuchType", None) is None
        assert any("noSuchType" in record.getMessage() for record in caplog.records)

    def test_type_from_name_unknown_local_name(self, caplog):
        caplog.clear()
        assert ElementRepresentative.typeFromName("noSuchType", None) is None
        assert any("noSuchType" in record.getMessage() for record in caplog.records)

    def test_get_from_name_with_multiple_entries_warns(self):
        er = self._bare_er("multiEntry")
        ermod.registry["multiEntry"].append(er)
        assert ElementRepresentative.getFromName("multiEntry") is None

    def test_add_super_class_name_guards(self, tmp_path):
        _parse(FACET_SCHEMA, "<doc><a>abcd</a></doc>", tmp_path)
        # The inline type for 'doc' has no superclass of its own.
        complex_er = ermod.registry["doc|complexType"][0]
        complex_er.addSuperClassName("someBase")
        complex_er.addSuperClassName("someBase")  # duplicate is dropped
        assert complex_er.superClassNames == ["someBase"]

    def test_try_convert(self):
        assert ElementRepresentative.tryConvert("42") == 42
        assert ElementRepresentative.tryConvert("3.5") == 3.5
        assert ElementRepresentative.tryConvert("true") is True
        assert ElementRepresentative.tryConvert("false") is False
        assert ElementRepresentative.tryConvert("n/a") == "n/a"


# ---------------------------------------------------------------------------
# Union and attributeGroup edge cases in xsd_type
# ---------------------------------------------------------------------------


class TestXsdTypeEdges:
    def test_union_with_unresolvable_named_member_reports_and_skips_it(self, tmp_path):
        """A ``memberTypes`` name that resolves to no type is an error.

        pyxsd stays lax: it reports ``unknown-type`` and still builds the
        union from the members that did resolve, so the instance parses.
        """
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="v">
          <xs:simpleType>
            <xs:union memberTypes="xs:date xs:noSuchType"/>
          </xs:simpleType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        parser = _parse(schema, "<doc><v>2024-01-02</v></doc>", tmp_path)
        codes = {issue.code for issue in parser.report.for_phase("schema")}
        assert "unknown-type" in codes

        root = _root_instance(parser)
        member = root.v
        assert member.memberValue == "2024-01-02"
        assert len(member._unionMembers) == 1

    def test_union_inline_member_resolves_in_namespaced_mode(self, tmp_path, caplog):
        """An inline union member is built from its own ER and enforced.

        A namespaced inline type is registered under its bare pipe name,
        so a name lookup in ``typeFromName`` used to miss it and skip the
        member; building the member ER directly makes the value space
        include both the named and the inline member.
        """
        schema = f"""\
<xs:schema {XS} targetNamespace="urn:t" xmlns:t="urn:t" elementFormDefault="qualified">
  <xs:attribute name="lang">
    <xs:simpleType>
      <xs:union memberTypes="xs:language">
        <xs:simpleType>
          <xs:restriction base="xs:string"><xs:enumeration value=""/></xs:restriction>
        </xs:simpleType>
      </xs:union>
    </xs:simpleType>
  </xs:attribute>
  <xs:element name="root">
    <xs:complexType><xs:attribute ref="t:lang"/></xs:complexType>
  </xs:element>
</xs:schema>
"""
        schema_path = tmp_path / "schema.xsd"
        schema_path.write_text(schema)
        caplog.set_level(logging.WARNING)
        parser = PyXSD(
            StringIO('<t:root xmlns:t="urn:t"/>'),
            xsdFile=schema_path,
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
            mode=ParseModes.NAMESPACED,
        )
        assert not parser.report.has_errors
        assert not any(
            "could not be built and was skipped" in record.getMessage() for record in caplog.records
        )
        union_cls = parser.classes["lang|simpleType"]
        assert len(union_cls._unionMembers) == 2

    def test_union_equality_hash_and_repr(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="v">
          <xs:simpleType>
            <xs:union memberTypes="xs:integer xs:date"/>
          </xs:simpleType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        parser = _parse(schema, "<doc><v>7</v></doc>", tmp_path)
        root = _root_instance(parser)
        member = root.v
        assert member == 7
        assert member != "7"
        assert repr(member) == repr(7)
        assert str(member) == str(7)
        assert hash(member) == hash(7)

    def test_attribute_group_conflict_is_reported(self, tmp_path):
        # A local attribute and a group-contributed attribute of the
        # same expanded name are duplicate attribute uses (attQ009); the
        # local declaration does not silently win.
        schema = f"""\
<xs:schema {XS}>
  <xs:attributeGroup name="shared">
    <xs:attribute name="color" type="xs:string"/>
  </xs:attributeGroup>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence/>
      <xs:attribute name="color" type="xs:int"/>
      <xs:attributeGroup ref="shared"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        parser = _parse(schema, '<doc color="5"/>', tmp_path)
        assert any(issue.code == "duplicate-attribute" for issue in parser.report.issues)

    def test_repeated_element_names_get_disambiguated(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="dup" type="xs:string"/>
        <xs:element name="dup" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        parser = _parse(schema, "<doc><dup>a</dup><dup>b</dup></doc>", tmp_path)
        root = _root_instance(parser)
        names = [descriptor.name for descriptor in root._getElements()]
        assert names == ["dup", "dup"]
        # Both descriptors are disambiguated on the class ('dup', 'dup|2').
        assert type(root)._elementNames_ == ["dup", "dup|2"]
        # Declaration-order consumption: each child is matched to the
        # next particle of that name, and repeated uses aggregate so no
        # occurrence is silently overwritten.
        assert root.__dict__["dup"] == ["a", "b"]


# ---------------------------------------------------------------------------
# Forced values, fixed values, wildcards in schema_base
# ---------------------------------------------------------------------------


class TestSchemaBaseEdges:
    def test_invalid_default_value_is_reported(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="v" type="xs:int" default="abc"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        parser = _parse(schema, "<doc><v/></doc>", tmp_path)
        assert parser.report.has_errors
        assert any(issue.code == "default" for issue in parser.report.issues)

    def test_invalid_fixed_element_value_is_reported(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="v" type="xs:int" fixed="7"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        parser = _parse(schema, "<doc><v>9</v></doc>", tmp_path)
        assert parser.report.has_errors
        assert any(issue.code == "fixed-element" for issue in parser.report.issues)

    def test_invalid_fixed_attribute_value_is_reported(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence/>
      <xs:attribute name="n" type="xs:int" fixed="abc"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        parser = _parse(schema, '<doc n="5"/>', tmp_path)
        assert parser.report.has_errors
        assert any(issue.code == "fixed-attribute" for issue in parser.report.issues)

    def test_mismatched_fixed_attribute_value_is_reported(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence/>
      <xs:attribute name="n" type="xs:int" fixed="5"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        parser = _parse(schema, '<doc n="7"/>', tmp_path)
        assert parser.report.has_errors
        assert any(issue.code == "fixed-attribute" for issue in parser.report.issues)

    def test_wildcard_ignores_namespace_declarations(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="title" type="xs:string"/>
        <xs:any maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:anyAttribute/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        instance = '<doc xmlns:z="urn:z" z:extra="1" unit="m"><title>t</title><extra/></doc>'
        parser = _parse(schema, instance, tmp_path)
        assert not parser.report.has_errors
        root = _root_instance(parser)
        # Namespace declarations never reach the instance attributes.
        assert not any(key.startswith("xmlns") for key in root._attribs_)
        assert root._attribs_["unit"] == "m"

    def test_choice_fallback_limits_from_first_descriptor(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc">
    <xs:complexType>
      <xs:choice>
        <xs:element name="a" type="xs:string" maxOccurs="2"/>
        <xs:element name="b" type="xs:string"/>
      </xs:choice>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        # Valid: one 'a' within its per-descriptor maxOccurs.
        parser = _parse(schema, "<doc><a/></doc>", tmp_path)
        assert not parser.report.has_errors

    def test_group_ref_occurrences_repeat_the_group_as_a_unit(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:group name="pair">
    <xs:sequence>
      <xs:element name="x" type="xs:string"/>
      <xs:element name="y" type="xs:string"/>
    </xs:sequence>
  </xs:group>
  <xs:element name="doc">
    <xs:complexType>
      <xs:group ref="pair" minOccurs="2" maxOccurs="unbounded"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
        # The reference repeats the whole (x, y) group; two repeats are
        # x, y, x, y, not four independent element occurrences.
        parser = _parse(schema, "<doc><x/><y/><x/><y/></doc>", tmp_path)
        assert not parser.report.has_errors

        # The reference site carries the occurrence attributes.
        ref_er = next(
            er
            for entries in ermod.registry.values()
            for er in entries
            if getattr(er, "isRefSite", False)
        )
        assert ref_er.maxOccurs == "unbounded"

        # x, x, y, y is not two repeats of the group.
        misordered = _parse(schema, "<doc><x/><x/><y/><y/></doc>", tmp_path)
        assert misordered.report.has_errors

        # One repeat does not satisfy the minimum of two.
        bad = _parse(schema, "<doc><x/><y/></doc>", tmp_path)
        assert any(issue.code == "occurrence-min" for issue in bad.report.issues)


# ---------------------------------------------------------------------------
# Parser error branches
# ---------------------------------------------------------------------------


class TestParserErrorBranches:
    def test_malformed_schema_file_object_raises(self):
        with pytest.raises(PyXSDError, match="not well-formed"):
            PyXSD(StringIO("<x/>"), xsdFile=StringIO("<xs:schema>"), xmlFileOutput="_No_Output_")

    def test_include_without_schema_location(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:include/>
  <xs:element name="doc" type="xs:string"/>
</xs:schema>
"""
        parser = _parse(schema, "<doc>hi</doc>", tmp_path)
        assert any(issue.code == "schema-compose" for issue in parser.report.issues)

    def test_redefine_without_schema_location(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:redefine/>
  <xs:element name="doc" type="xs:string"/>
</xs:schema>
"""
        parser = _parse(schema, "<doc>hi</doc>", tmp_path)
        assert any(issue.code == "schema-compose" for issue in parser.report.issues)

    def test_redefine_cycle_is_reported(self, tmp_path):
        (tmp_path / "cycle.xsd").write_text(
            f'<xs:schema {XS}><xs:include schemaLocation="cycle.xsd"/></xs:schema>'
        )
        schema = f"""\
<xs:schema {XS}>
  <xs:redefine schemaLocation="cycle.xsd"/>
  <xs:element name="doc" type="xs:string"/>
</xs:schema>
"""
        parser = _parse(schema, "<doc>hi</doc>", tmp_path)
        codes = {issue.code for issue in parser.report.issues}
        assert "compose-cycle" in codes

    def test_multiple_roots_with_the_same_name(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc" type="xs:string"/>
  <xs:element name="doc" type="xs:string"/>
</xs:schema>
"""
        parser = _parse(schema, "<doc>hi</doc>", tmp_path)
        assert any(issue.code == "multiple-roots" for issue in parser.report.issues)

    def test_abstract_root_is_rejected(self, tmp_path):
        schema = f"""\
<xs:schema {XS}>
  <xs:element name="doc" type="xs:string" abstract="true"/>
</xs:schema>
"""
        parser = _parse(schema, "<doc>hi</doc>", tmp_path)
        assert any(issue.code == "abstract-element" for issue in parser.report.issues)

    def test_complex_content_extension_resolves_base(self, tmp_path):
        parser = _parse(FACET_SCHEMA, "<doc><a>abcd</a></doc>", tmp_path)
        assert not parser.report.has_errors
        # The derivedType complexType was produced from the complexContent
        # extension and carries the base type in its superclass names.
        complex_er = _schema_er().complexTypes["derived"]
        assert complex_er.superClassNames == ["rangeInt"]

    def test_load_class_from_file_missing(self, tmp_path):
        parser = _parse(FACET_SCHEMA, "<doc><a>abcd</a></doc>", tmp_path)
        with pytest.raises(ImportError, match="was not found"):
            parser.loadClassFromFile("definitelyNotHere")

    def test_send_tree_to_pyxsd_default_names(self, tmp_path, monkeypatch):
        import pyxsd.transforms.send_tree_to_pyxsd as stp

        monkeypatch.chdir(tmp_path)
        parser = _parse(FACET_SCHEMA, "<doc><a>abcd</a></doc>", tmp_path)
        root = _root_instance(parser)

        transform = stp.SendTreeToPyXSD(root)
        result = transform(
            xsdFile=str(tmp_path / "schema.xsd"),
            xmlFileOutput=False,
            transformOutputName=None,
        )
        assert result is root
        # Falsy parsed output falls back to the historical default name.
        assert (tmp_path / "tempFileParsed.xml").is_file()
        # No transforms were supplied, so no transformed file is written;
        # the None transformOutputName branch still resolved its default.
        assert not (tmp_path / "tempFileTransformed.xml").is_file()

    def test_load_transform_file_by_normalized_name_handles_errors(self, tmp_path):
        from pyxsd.parser import _loadTransformFileByNormalizedName

        # A directory that does not exist yields None rather than raising.
        assert _loadTransformFileByNormalizedName("PrintData", tmp_path / "missing") is None
        # A directory without a matching module yields None.
        assert _loadTransformFileByNormalizedName("Nope", tmp_path) is None


def _root_instance(parser):
    """Rebuilds the parser's root instance the way parseXML does."""
    schema_er = _schema_er()
    root_name = parser.xmlRoot.tag.split("}")[-1]
    root_er = next(
        (element for element in schema_er.elements if element.name == root_name),
        schema_er.elements[0],
    )
    sub_cls = root_er.getType()
    return sub_cls.makeInstanceFromTag(parser.xmlRoot)
