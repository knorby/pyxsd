"""Tests for occurrence and value semantics (Phase 8).

Covers attribute/element ``default`` and ``fixed``, ``nillable`` with
``xsi:nil``, substitution groups (with element references), ``xsi:type``
dispatch, and the abstract/block/final guards.
"""

import xml.etree.ElementTree as ET

from conftest import FIXTURES_DIR
from pyxsd.schema import Schema

XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XSI_NS_DECL = f'xmlns:xsi="{XSI_NS}"'


def _parse(schema_text, instance_text, tmp_path):
    """Compiles an inline schema and binds an inline instance document."""
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(schema_text)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance_text)
    return Schema.compile(str(schema_path)).parse(str(instance_path))


def _root_instance(doc):
    """Rebuilds the document's root instance from its raw element tree."""
    import pyxsd.element_representatives.element_representative as ermod
    from pyxsd.namespaces import NamespaceContext, parse_with_namespaces

    schemaER = ermod.registry["schema"][0]
    xml_root = parse_with_namespaces(doc.source, NamespaceContext())
    root_name = xml_root.tag.split("}")[-1]
    root_er = next(
        (element for element in schemaER.elements if element.name == root_name),
        schemaER.elements[0],
    )
    sub_cls = root_er.getType()
    return sub_cls.makeInstanceFromTag(xml_root)


# ---------------------------------------------------------------------------
# Attribute and element defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_absent_optional_attribute_takes_default(self):
        doc = Schema.compile(str(FIXTURES_DIR / "defaults" / "schema.xsd")).parse(
            str(FIXTURES_DIR / "defaults" / "instance.xml")
        )
        instance = _root_instance(doc)
        # 'mode' is absent from the instance document; the schema
        # default supplies its typed value.
        assert str(instance.mode) == "standard"

    def test_default_is_not_injected_into_written_output(self, tmp_path):
        """Default application is programmatic only; output stays faithful."""
        output = tmp_path / "parsed.xml"
        Schema.compile(str(FIXTURES_DIR / "defaults" / "schema.xsd")).parse(
            str(FIXTURES_DIR / "defaults" / "instance.xml")
        ).write(str(output))
        tree = ET.parse(output)
        assert "mode" not in tree.getroot().attrib

    def test_empty_element_takes_default_value(self):
        doc = Schema.compile(str(FIXTURES_DIR / "defaults" / "schema.xsd")).parse(
            str(FIXTURES_DIR / "defaults" / "instance.xml")
        )
        instance = _root_instance(doc)
        volume = instance._children_[0]
        assert volume._name_ == "volume"
        assert str(volume) == "5"


class TestAttributeFixed:
    def test_matching_fixed_attribute_is_accepted(self):
        doc = Schema.compile(str(FIXTURES_DIR / "defaults" / "schema.xsd")).parse(
            str(FIXTURES_DIR / "defaults" / "instance.xml")
        )
        instance = _root_instance(doc)
        assert str(instance.key) == "k-1"

    def test_conflicting_fixed_attribute_is_reported(self, tmp_path):
        doc = _parse(
            (FIXTURES_DIR / "defaults" / "schema.xsd").read_text(),
            '<settings key="WRONG"><volume/><label>fixture</label></settings>',
            tmp_path,
        )
        codes = [issue.code for issue in doc.report.issues]
        assert "fixed-attribute" in codes

    def test_absent_fixed_attribute_takes_the_fixed_value(self, tmp_path):
        doc = _parse(
            (FIXTURES_DIR / "defaults" / "schema.xsd").read_text(),
            "<settings><volume/><label>fixture</label></settings>",
            tmp_path,
        )
        instance = _root_instance(doc)
        assert str(instance.key) == "k-1"


class TestElementFixed:
    def test_conflicting_fixed_element_is_reported(self, tmp_path):
        doc = _parse(
            (FIXTURES_DIR / "defaults" / "schema.xsd").read_text(),
            '<settings key="k-1"><volume/><label>WRONG</label></settings>',
            tmp_path,
        )
        codes = [issue.code for issue in doc.report.issues]
        assert "fixed-element" in codes

    def test_nonempty_element_keeps_its_value(self, tmp_path):
        doc = _parse(
            (FIXTURES_DIR / "defaults" / "schema.xsd").read_text(),
            '<settings key="k-1"><volume>9</volume><label>fixture</label></settings>',
            tmp_path,
        )
        volume = _root_instance(doc)._children_[0]
        assert str(volume) == "9"


class TestFixedValueSpaceEquality:
    """Fixed checks compare XSD values, not lexical spellings (R10)."""

    def _codes(self, doc):
        return [issue.code for issue in doc.report.issues]

    def test_hex_case_is_equivalent(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="r"><xs:complexType>'
            '<xs:attribute name="h" type="xs:hexBinary" fixed="FF"/>'
            "</xs:complexType></xs:element></xs:schema>",
            '<r h="ff"/>',
            tmp_path,
        )
        assert "fixed-attribute" not in self._codes(doc)

    def test_list_whitespace_is_equivalent(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="r"><xs:complexType><xs:sequence>'
            '<xs:element name="n" type="xs:NMTOKENS" fixed="a b"/>'
            "</xs:sequence></xs:complexType></xs:element></xs:schema>",
            "<r><n>a  b</n></r>",
            tmp_path,
        )
        assert "fixed-element" not in self._codes(doc)

    def test_timezone_equivalent_datetimes(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="r"><xs:complexType><xs:sequence>'
            '<xs:element name="t" type="xs:dateTime" '
            'fixed="1999-12-31T19:00:00-05:00"/>'
            "</xs:sequence></xs:complexType></xs:element></xs:schema>",
            "<r><t>2000-01-01T00:00:00Z</t></r>",
            tmp_path,
        )
        assert "fixed-element" not in self._codes(doc)

    def test_genuine_conflict_still_reported(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="r"><xs:complexType><xs:sequence>'
            '<xs:element name="t" type="xs:dateTime" '
            'fixed="1999-12-31T19:00:00-05:00"/>'
            "</xs:sequence></xs:complexType></xs:element></xs:schema>",
            "<r><t>2000-01-01T00:00:01Z</t></r>",
            tmp_path,
        )
        assert "fixed-element" in self._codes(doc)


class TestMixedContentFixed:
    """An element ``fixed`` on mixed or ur-type content constrains the
    character content (MS-Additional isDefault070/077, SUN
    valueConstraint00701m1/00801m1)."""

    def _codes(self, doc):
        return [issue.code for issue in doc.report.issues]

    def test_root_mixed_content_mismatch_is_reported(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" fixed="abc"><xs:complexType mixed="true">'
            '<xs:sequence minOccurs="0"><xs:element name="e1"/>'
            '<xs:element name="e2"/></xs:sequence></xs:complexType>'
            "</xs:element></xs:schema>",
            "<root>not_fixed</root>",
            tmp_path,
        )
        assert "fixed-element" in self._codes(doc)

    def test_root_mixed_content_match_is_accepted(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" fixed="abc"><xs:complexType mixed="true">'
            '<xs:sequence minOccurs="0"><xs:element name="e1"/>'
            '<xs:element name="e2"/></xs:sequence></xs:complexType>'
            "</xs:element></xs:schema>",
            "<root>abc</root>",
            tmp_path,
        )
        assert "fixed-element" not in self._codes(doc)

    def test_untyped_child_fixed_mismatch_is_reported(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" type="ct"/>'
            '<xs:complexType name="ct"><xs:sequence>'
            '<xs:element name="a" fixed="fixed_value"/>'
            "</xs:sequence></xs:complexType></xs:schema>",
            "<root><a>not fixed</a></root>",
            tmp_path,
        )
        assert "fixed-element" in self._codes(doc)

    def test_mixed_content_with_element_children_conflicts(self, tmp_path):
        # SUN valueConstraint00701m1: even a matching character sequence
        # conflicts when the element carries element children.
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" fixed="part1 part2">'
            '<xs:complexType mixed="true"><xs:sequence minOccurs="0" maxOccurs="unbounded">'
            '<xs:element name="separator" minOccurs="0" maxOccurs="unbounded"/>'
            "</xs:sequence></xs:complexType></xs:element></xs:schema>",
            "<root>part1 <separator/>part2</root>",
            tmp_path,
        )
        assert "fixed-element" in self._codes(doc)

    def test_mixed_content_plain_text_match_is_accepted(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" fixed="part1 part2">'
            '<xs:complexType mixed="true"><xs:sequence minOccurs="0" maxOccurs="unbounded">'
            '<xs:element name="separator" minOccurs="0" maxOccurs="unbounded"/>'
            "</xs:sequence></xs:complexType></xs:element></xs:schema>",
            "<root>part1 part2</root>",
            tmp_path,
        )
        assert "fixed-element" not in self._codes(doc)

    def test_xsi_type_mixed_override_mismatch_is_reported(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" fixed="alpha beta"/>'
            '<xs:complexType name="Text" mixed="true"/>'
            "</xs:schema>",
            f'<root {XSI_NS_DECL} xsi:type="Text">beta alpha</root>',
            tmp_path,
        )
        assert "fixed-element" in self._codes(doc)

    def test_xsi_type_mixed_override_match_is_accepted(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" fixed="alpha beta"/>'
            '<xs:complexType name="Text" mixed="true"/>'
            "</xs:schema>",
            f'<root {XSI_NS_DECL} xsi:type="Text">alpha beta</root>',
            tmp_path,
        )
        assert "fixed-element" not in self._codes(doc)

    def test_empty_mixed_content_takes_the_fixed_value(self, tmp_path):
        # MS isDefault076: an empty element takes the fixed value rather
        # than being compared against it.
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" fixed="abc">'
            '<xs:complexType mixed="true"/></xs:element></xs:schema>',
            "<root/>",
            tmp_path,
        )
        assert "fixed-element" not in self._codes(doc)

    def test_empty_untyped_child_takes_the_fixed_value(self, tmp_path):
        # MS isDefault073: an empty untyped child with fixed="fixed".
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="root" type="ct"/>'
            '<xs:complexType name="ct"><xs:sequence>'
            '<xs:element name="b" fixed="fixed"/>'
            "</xs:sequence></xs:complexType></xs:schema>",
            "<root><b/></root>",
            tmp_path,
        )
        assert "fixed-element" not in self._codes(doc)


# ---------------------------------------------------------------------------
# nillable / xsi:nil
# ---------------------------------------------------------------------------


class TestNillable:
    def test_nillable_element_with_nil(self):
        doc = Schema.compile(str(FIXTURES_DIR / "nillable" / "schema.xsd")).parse(
            str(FIXTURES_DIR / "nillable" / "instance.xml")
        )
        instance = _root_instance(doc)
        value = instance._children_[0]
        assert value._name_ == "value"
        assert value._value_ is None
        assert value._attribs_["xsi:nil"] == "true"

    def test_nil_attribute_survives_the_write(self, tmp_path):
        output = tmp_path / "parsed.xml"
        Schema.compile(str(FIXTURES_DIR / "nillable" / "schema.xsd")).parse(
            str(FIXTURES_DIR / "nillable" / "instance.xml")
        ).write(str(output))
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
        doc = _parse(
            schema,
            f'<root xmlns:xsi="{XSI_NS}"><v xsi:nil="true"/></root>',
            tmp_path,
        )
        codes = [issue.code for issue in doc.report.issues]
        assert "nil" in codes


# ---------------------------------------------------------------------------
# substitution groups
# ---------------------------------------------------------------------------


class TestSubstitutionGroups:
    def test_member_parsed_with_its_own_type(self):
        doc = Schema.compile(str(FIXTURES_DIR / "substitution" / "schema.xsd")).parse(
            str(FIXTURES_DIR / "substitution" / "instance.xml")
        )
        instance = _root_instance(doc)
        dot = instance._children_[1]
        assert dot._name_ == "dot"
        assert str(dot) == "3"  # xs:integer member of a string head

    def test_member_without_own_type_uses_head_type(self):
        doc = Schema.compile(str(FIXTURES_DIR / "substitution" / "schema.xsd")).parse(
            str(FIXTURES_DIR / "substitution" / "instance.xml")
        )
        instance = _root_instance(doc)
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
        doc = _parse(schema, "<root/>", tmp_path)
        codes = [issue.code for issue in doc.report.issues]
        assert "unknown-substitution-head" in codes


def test_substitution_round_trip_preserves_member_names(tmp_path):
    output = tmp_path / "parsed.xml"
    Schema.compile(str(FIXTURES_DIR / "substitution" / "schema.xsd")).parse(
        str(FIXTURES_DIR / "substitution" / "instance.xml")
    ).write(str(output))
    tree = ET.parse(output)
    names = [child.tag for child in tree.getroot()]
    assert names == ["marker", "dot", "peg"]


# ---------------------------------------------------------------------------
# xsi:type dispatch
# ---------------------------------------------------------------------------


class TestXsiTypeDispatch:
    def test_child_dispatches_to_derived_type(self):
        doc = Schema.compile(str(FIXTURES_DIR / "xsi_type" / "schema.xsd")).parse(
            str(FIXTURES_DIR / "xsi_type" / "instance.xml")
        )
        instance = _root_instance(doc)
        first_item = instance._children_[0]
        child_names = [child._name_ for child in first_item._children_]
        # specialValueType has num AND tag; baseValueType has only num.
        assert child_names == ["num", "tag"]

    def test_second_item_keeps_declared_type(self):
        doc = Schema.compile(str(FIXTURES_DIR / "xsi_type" / "schema.xsd")).parse(
            str(FIXTURES_DIR / "xsi_type" / "instance.xml")
        )
        instance = _root_instance(doc)
        second_item = instance._children_[1]
        child_names = [child._name_ for child in second_item._children_]
        assert child_names == ["num"]

    def test_root_element_dispatch(self, tmp_path):
        doc = _parse(
            (FIXTURES_DIR / "xsi_type" / "schema.xsd").read_text(),
            f'<holder xmlns:xsi="{XSI_NS}" xsi:type="specialValueType">'
            "<num>2</num><tag>t</tag></holder>",
            tmp_path,
        )
        instance = doc.root
        child_names = [child._name_ for child in instance._children_]
        assert child_names == ["num", "tag"]

    def test_unresolvable_xsi_type_is_reported(self, tmp_path):
        doc = _parse(
            (FIXTURES_DIR / "xsi_type" / "schema.xsd").read_text(),
            f'<holder xmlns:xsi="{XSI_NS}" xsi:type="missingType"><num>2</num></holder>',
            tmp_path,
        )
        codes = [issue.code for issue in doc.report.issues]
        assert "xsi-type" in codes


class TestXsiTypeDerivation:
    """xsi:type must name a type validly derived from the declared type."""

    _BASE = (
        '<xs:complexType name="baseValueType"><xs:sequence>'
        '<xs:element name="num" type="xs:integer"/>'
        "</xs:sequence></xs:complexType>"
    )
    _EXTENDED = (
        '<xs:complexType name="specialValueType"><xs:complexContent>'
        '<xs:extension base="baseValueType"><xs:sequence>'
        '<xs:element name="tag" type="xs:string"/>'
        "</xs:sequence></xs:extension>"
        "</xs:complexContent></xs:complexType>"
    )
    _UNRELATED = (
        '<xs:complexType name="otherType"><xs:sequence>'
        '<xs:element name="num" type="xs:integer"/>'
        '<xs:element name="tag" type="xs:string"/>'
        "</xs:sequence></xs:complexType>"
    )

    def _schema(self, extra_decl=""):
        return (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            + self._BASE
            + extra_decl
            + '<xs:element name="holder" type="baseValueType"/>'
            "</xs:schema>"
        )

    def test_valid_extension_is_accepted(self, tmp_path):
        doc = _parse(
            self._schema(self._EXTENDED),
            f'<holder {XSI_NS_DECL} xsi:type="specialValueType"><num>1</num><tag>t</tag></holder>',
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_unrelated_type_is_rejected(self, tmp_path):
        doc = _parse(
            self._schema(self._UNRELATED),
            f'<holder {XSI_NS_DECL} xsi:type="otherType"><num>1</num><tag>t</tag></holder>',
            tmp_path,
        )
        assert any(issue.code == "xsi-type" for issue in doc.report.issues)

    def test_element_block_rejects_extension(self, tmp_path):
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            + self._BASE
            + self._EXTENDED
            + '<xs:element name="holder" type="baseValueType" block="extension"/>'
            "</xs:schema>"
        )
        doc = _parse(
            schema,
            f'<holder {XSI_NS_DECL} xsi:type="specialValueType"><num>1</num><tag>t</tag></holder>',
            tmp_path,
        )
        assert any(issue.code == "xsi-type" for issue in doc.report.issues)

    def test_type_block_rejects_extension(self, tmp_path):
        base = self._BASE.replace('name="baseValueType"', 'name="baseValueType" block="extension"')
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            + base
            + self._EXTENDED
            + '<xs:element name="holder" type="baseValueType"/>'
            "</xs:schema>"
        )
        doc = _parse(
            schema,
            f'<holder {XSI_NS_DECL} xsi:type="specialValueType"><num>1</num><tag>t</tag></holder>',
            tmp_path,
        )
        assert any(issue.code == "xsi-type" for issue in doc.report.issues)

    def test_unrelated_primitive_is_rejected(self, tmp_path):
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="holder" type="xs:int"/>'
            "</xs:schema>"
        )
        doc = _parse(
            schema,
            f'<holder xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            f'{XSI_NS_DECL} xsi:type="xs:string">oops</holder>',
            tmp_path,
        )
        assert any(issue.code == "xsi-type" for issue in doc.report.issues)

    def test_child_xsi_type_derivation_is_checked(self, tmp_path):
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            + self._BASE
            + self._UNRELATED
            + '<xs:element name="root"><xs:complexType><xs:sequence>'
            '<xs:element name="item" type="baseValueType"/>'
            "</xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        doc = _parse(
            schema,
            f'<root><item {XSI_NS_DECL} xsi:type="otherType">'
            "<num>1</num><tag>t</tag></item></root>",
            tmp_path,
        )
        assert any(issue.code == "xsi-type" for issue in doc.report.issues)


class TestSubstitutionMemberConstraints:
    """The member declaration, not the head, supplies value constraints."""

    def _schema(self, member_attrs=""):
        return (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="h" type="xs:int"/>'
            f'<xs:element name="m" type="xs:int" substitutionGroup="h" {member_attrs}/>'
            '<xs:element name="r"><xs:complexType><xs:sequence>'
            '<xs:element ref="h"/>'
            "</xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )

    def test_member_fixed_is_enforced(self, tmp_path):
        doc = _parse(self._schema('fixed="7"'), "<r><m>8</m></r>", tmp_path)
        assert any(issue.code == "fixed-element" for issue in doc.report.issues)

    def test_member_fixed_allows_matching_value(self, tmp_path):
        doc = _parse(self._schema('fixed="7"'), "<r><m>7</m></r>", tmp_path)
        assert not doc.report.has_errors

    def test_head_fixed_does_not_constrain_member(self, tmp_path):
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="h" type="xs:int" fixed="7"/>'
            '<xs:element name="m" type="xs:int" substitutionGroup="h"/>'
            '<xs:element name="r"><xs:complexType><xs:sequence>'
            '<xs:element ref="h"/>'
            "</xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        doc = _parse(schema, "<r><m>8</m></r>", tmp_path)
        assert not doc.report.has_errors

    def test_member_nillable_accepts_nil(self, tmp_path):
        doc = _parse(
            self._schema('nillable="true"'),
            f'<r {XSI_NS_DECL}><m xsi:nil="true"/></r>',
            tmp_path,
        )
        assert not doc.report.has_errors


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
    doc = _parse(
        schema,
        f'<root xmlns:xsi="{XSI_NS}"><head>x</head></root>',
        tmp_path,
    )
    codes = [issue.code for issue in doc.report.issues]
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
    doc = _parse(schema, "<root><member>ok</member></root>", tmp_path)
    assert not doc.report.has_errors


def test_abstract_complex_type_rejected_directly(tmp_path):
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:complexType name="base" abstract="true">'
        '<xs:sequence><xs:element name="v" type="xs:integer"/></xs:sequence>'
        "</xs:complexType>"
        '<xs:element name="root" type="base"/>'
        "</xs:schema>"
    )
    doc = _parse(schema, "<root><v>1</v></root>", tmp_path)
    codes = [issue.code for issue in doc.report.issues]
    assert "abstract-type" in codes
    # parsing still completes non-fatally
    assert _root_instance(doc) is not None


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
    doc = _parse(schema, "<root><v>1</v><w>x</w></root>", tmp_path)
    assert not doc.report.has_errors


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
    doc = _parse(schema, "<root><v>1</v><w>x</w></root>", tmp_path)
    codes = [issue.code for issue in doc.report.issues]
    assert "final" in codes


def test_final_default_blocks_derivation(tmp_path):
    """Saxon simple005: a schema's ``finalDefault`` supplies a type's
    effective ``final`` when the declaration states none."""
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" finalDefault="extension">'
        '<xs:simpleType name="pubDate"><xs:restriction base="xs:date">'
        '<xs:pattern value="2012.*"/></xs:restriction></xs:simpleType>'
        '<xs:complexType name="pubType"><xs:simpleContent>'
        '<xs:extension base="pubDate">'
        '<xs:attribute name="country" type="xs:string"/>'
        "</xs:extension></xs:simpleContent></xs:complexType>"
        '<xs:element name="root" type="pubType"/>'
        "</xs:schema>"
    )
    doc = _parse(schema, "<root country='x'>2012-01-01</root>", tmp_path)
    codes = [issue.code for issue in doc.report.issues]
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
    doc = _parse(schema, "<root><member>x</member></root>", tmp_path)
    codes = [issue.code for issue in doc.report.issues]
    assert "blocked" in codes


def test_root_matching_no_global_element_is_reported(tmp_path):
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="other"/>'
        "</xs:schema>"
    )
    doc = _parse(schema, "<root/>", tmp_path)
    codes = [issue.code for issue in doc.report.issues]
    assert "unknown-root" in codes


class TestPrimitiveRootValues:
    """Primitive-typed roots validate like primitive children."""

    def _codes(self, doc):
        return [issue.code for issue in doc.report.issues]

    def _parse_int(self, attrs, instance, tmp_path):
        return _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            f'<xs:element name="r" type="xs:int"{attrs}/>'
            "</xs:schema>",
            instance,
            tmp_path,
        )

    def test_empty_integer_root_is_invalid(self, tmp_path):
        doc = self._parse_int("", "<r/>", tmp_path)
        assert "value" in self._codes(doc)

    def test_empty_integer_root_takes_default(self, tmp_path):
        doc = self._parse_int(' default="7"', "<r/>", tmp_path)
        assert not doc.report.has_errors

    def test_empty_string_root_is_valid(self, tmp_path):
        doc = _parse(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>",
            "<r/>",
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_fixed_integer_root_conflict(self, tmp_path):
        doc = self._parse_int(' fixed="7"', "<r>8</r>", tmp_path)
        assert "fixed-element" in self._codes(doc)

    def test_child_content_on_integer_root_is_rejected(self, tmp_path):
        doc = self._parse_int("", "<r><a>7</a></r>", tmp_path)
        assert "unexpected-element" in self._codes(doc)

    def test_nillable_integer_root_with_nil(self, tmp_path):
        doc = self._parse_int(
            ' nillable="true"',
            f'<r {XSI_NS_DECL} xsi:nil="true"/>',
            tmp_path,
        )
        assert not doc.report.has_errors


class TestPrimitiveChildValues:
    """An attribute value must not stand in for a simple element's text."""

    _SCHEMA = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="a" type="xs:int"/>'
        "</xs:sequence></xs:complexType></xs:element>"
        "</xs:schema>"
    )

    def test_attribute_is_not_the_element_value(self, tmp_path):
        doc = _parse(self._SCHEMA, '<r><a stray="7"/></r>', tmp_path)
        # The empty lexical form is invalid for xs:int; the stray
        # attribute does not supply the value 7.
        assert any(issue.code == "value" for issue in doc.report.issues)

    def test_text_value_still_binds(self, tmp_path):
        doc = _parse(self._SCHEMA, "<r><a>7</a></r>", tmp_path)
        assert not doc.report.has_errors

    def test_child_elements_on_simple_type_are_rejected(self, tmp_path):
        doc = _parse(self._SCHEMA, "<r><a>7<b/></a></r>", tmp_path)
        assert any(issue.code == "unexpected-element" for issue in doc.report.issues)
