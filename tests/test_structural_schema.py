"""Tests for structural schema elements: xs:all, xs:union, group and
attributeGroup references, and the xs:any/xs:anyAttribute wildcards.
"""

import logging

from conftest import run_parser

# ---------------------------------------------------------------------------
# xs:all
# ---------------------------------------------------------------------------


class TestAllCompositor:
    def test_fixture_parses_clean(self):
        doc = run_parser("all")
        assert doc.report.has_errors is False

    def test_elements_declared_from_all(self):
        doc = run_parser("all")
        flag_cls = doc.schema.classes["flagSet"]
        assert flag_cls._elementNames_ == ["red", "green", "blue"]

    def test_any_order_accepted(self):
        doc = run_parser("all")
        root = doc.root
        # blue comes before red in the instance; both accepted (order
        # checking in an 'all' compositor is order-agnostic). Children
        # are recorded in declaration order.
        names = sorted(child._name_ for child in root._children_)
        assert names == ["blue", "red"]

    def test_typed_values(self):
        doc = run_parser("all")
        root = doc.root
        values = {child._name_: child for child in root._children_}
        assert str(values["red"]) == "bright"

    def test_missing_required_element(self, tmp_path):
        schema = tmp_path / "schema.xsd"
        instance = tmp_path / "instance.xml"
        schema.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="flagSet"><xs:all>'
            '<xs:element name="red" type="xs:string"/>'
            '<xs:element name="blue" type="xs:string" minOccurs="0"/>'
            "</xs:all></xs:complexType>"
            '<xs:element name="flags" type="flagSet"/></xs:schema>'
        )
        instance.write_text("<flags><blue>x</blue></flags>")
        from pyxsd.schema import Schema

        compiled = Schema.compile(str(schema))
        doc = compiled.parse(str(instance))
        codes = [issue.code for issue in doc.report.issues]
        assert "occurrence-min" in codes

    def test_too_many_occurrences(self, tmp_path):
        from pyxsd.schema import Schema

        schema = tmp_path / "schema.xsd"
        instance = tmp_path / "instance.xml"
        schema.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="flagSet"><xs:all>'
            '<xs:element name="red" type="xs:string"/>'
            "</xs:all></xs:complexType>"
            '<xs:element name="flags" type="flagSet"/></xs:schema>'
        )
        instance.write_text("<flags><red>a</red><red>b</red></flags>")
        compiled = Schema.compile(str(schema))
        doc = compiled.parse(str(instance))
        codes = [issue.code for issue in doc.report.issues]
        assert "occurrence-max" in codes

    def test_maxOccurs_in_all_warns(self, caplog, tmp_path):
        """XSD 1.0 forbids maxOccurs > 1 inside xs:all."""
        from pyxsd.schema import Schema

        schema = tmp_path / "schema.xsd"
        instance = tmp_path / "instance.xml"
        schema.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="flagSet"><xs:all>'
            '<xs:element name="red" type="xs:string" maxOccurs="2"/>'
            "</xs:all></xs:complexType>"
            '<xs:element name="flags" type="flagSet"/></xs:schema>'
        )
        instance.write_text("<flags><red>a</red></flags>")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="pyxsd.element_representatives.complex_type"):
            Schema.compile(str(schema)).parse(str(instance))
        assert any("maxOccurs" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# groups + attributeGroups
# ---------------------------------------------------------------------------


class TestGroups:
    def test_fixture_parses_clean(self):
        doc = run_parser("groups")
        assert doc.report.has_errors is False

    def test_group_elements_flattened_in_order(self):
        doc = run_parser("groups")
        point_cls = doc.schema.classes["auditPoint"]
        assert point_cls._elementNames_ == ["x", "y"]

    def test_attribute_group_merged(self):
        doc = run_parser("groups")
        point_cls = doc.schema.classes["auditPoint"]
        assert point_cls._attributeNames_ == ["by", "note"]

    def test_instance_values(self):
        doc = run_parser("groups")
        root = doc.root
        assert str(root.by) == "kali"
        assert str(root.note) == "first point"
        names = [child._name_ for child in root._children_]
        assert names == ["x", "y"]

    def test_unknown_group_ref(self, tmp_path):
        from pyxsd.schema import Schema

        schema = tmp_path / "schema.xsd"
        instance = tmp_path / "instance.xml"
        schema.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t"><xs:group ref="missing"/></xs:complexType>'
            '<xs:element name="root" type="t"/></xs:schema>'
        )
        instance.write_text("<root/>")
        compiled = Schema.compile(str(schema))
        doc = compiled.parse(str(instance))
        codes = [issue.code for issue in doc.report.issues]
        assert "unknown-group" in codes

    def test_circular_group_ref(self, tmp_path):
        from pyxsd.schema import Schema

        schema = tmp_path / "schema.xsd"
        instance = tmp_path / "instance.xml"
        schema.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t"><xs:group ref="a"/></xs:complexType>'
            '<xs:element name="root" type="t"/>'
            '<xs:group name="a"><xs:sequence>'
            '<xs:element name="one" type="xs:string"/>'
            '<xs:group ref="b"/></xs:sequence></xs:group>'
            '<xs:group name="b"><xs:sequence>'
            '<xs:element name="two" type="xs:string"/>'
            '<xs:group ref="a"/></xs:sequence></xs:group>'
            "</xs:schema>"
        )
        instance.write_text("<root/>")
        compiled = Schema.compile(str(schema))
        doc = compiled.parse(str(instance))
        codes = [issue.code for issue in doc.report.issues]
        assert "circular-group" in codes

    def test_wrong_order_in_group_sequence(self, tmp_path):
        from pyxsd.schema import Schema

        schema = tmp_path / "schema.xsd"
        instance = tmp_path / "instance.xml"
        schema.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t"><xs:group ref="coords"/></xs:complexType>'
            '<xs:element name="root" type="t"/>'
            '<xs:group name="coords"><xs:sequence>'
            '<xs:element name="x" type="xs:integer"/>'
            '<xs:element name="y" type="xs:integer"/>'
            "</xs:sequence></xs:group></xs:schema>"
        )
        instance.write_text("<root><y>2</y><x>1</x></root>")
        compiled = Schema.compile(str(schema))
        doc = compiled.parse(str(instance))
        codes = [issue.code for issue in doc.report.issues]
        assert "order" in codes

    def test_unknown_attribute_group_ref(self, tmp_path):
        from pyxsd.schema import Schema

        schema = tmp_path / "schema.xsd"
        instance = tmp_path / "instance.xml"
        schema.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t"><xs:attributeGroup ref="missing"/></xs:complexType>'
            '<xs:element name="root" type="t"/></xs:schema>'
        )
        instance.write_text('<root by="x"/>')
        compiled = Schema.compile(str(schema))
        doc = compiled.parse(str(instance))
        codes = [issue.code for issue in doc.report.issues]
        assert "unknown-attributeGroup" in codes


# ---------------------------------------------------------------------------
# unions
# ---------------------------------------------------------------------------


class TestUnions:
    def test_fixture_parses_clean(self):
        doc = run_parser("unions")
        assert doc.report.has_errors is False

    def test_named_member_union_integer(self):
        doc = run_parser("unions")
        root = doc.root
        size = root.size
        assert isinstance(size, doc.schema.classes["sizeOrName"])
        assert size.memberValue == 42

    def test_inline_member_union_token(self):
        doc = run_parser("unions")
        root = doc.root
        tag = root.tag
        assert isinstance(tag, doc.schema.classes["tokenOrCount"])
        # the raw text stays on the instance; the stripped form is in
        # _value_ (set by primitiveValueFor)
        assert tag._value_ == ["padded"]

    def test_string_member_catches_all(self, tmp_path):
        from pyxsd.schema import Schema

        schema = tmp_path / "schema.xsd"
        instance = tmp_path / "instance.xml"
        schema.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:simpleType name="u"><xs:union memberTypes="xs:integer xs:string"/>'
            "</xs:simpleType>"
            '<xs:complexType name="t"><xs:sequence>'
            '<xs:element name="v" type="u"/></xs:sequence></xs:complexType>'
            '<xs:element name="root" type="t"/></xs:schema>'
        )
        instance.write_text("<root><v>1.5</v></root>")
        compiled = Schema.compile(str(schema))
        doc = compiled.parse(str(instance))
        assert doc.root.v.memberValue == "1.5"

    def test_no_matching_member_reports_error(self, tmp_path):
        from pyxsd.schema import Schema

        schema = tmp_path / "schema.xsd"
        instance = tmp_path / "instance.xml"
        schema.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:simpleType name="u"><xs:union memberTypes="xs:date xs:integer"/>'
            "</xs:simpleType>"
            '<xs:complexType name="t"><xs:sequence>'
            '<xs:element name="v" type="u"/></xs:sequence></xs:complexType>'
            '<xs:element name="root" type="t"/></xs:schema>'
        )
        instance.write_text("<root><v>not a date or integer</v></root>")
        compiled = Schema.compile(str(schema))
        doc = compiled.parse(str(instance))
        codes = [issue.code for issue in doc.report.issues]
        assert "value" in codes

    def test_union_membership_introspection(self):
        doc = run_parser("unions")
        union = doc.schema.classes["sizeOrName"]
        from pyxsd.xsd_data_types import Integer, String

        assert union._unionMembers == [Integer, String]


# ---------------------------------------------------------------------------
# wildcards
# ---------------------------------------------------------------------------


class TestWildcards:
    def test_fixture_parses_clean(self):
        doc = run_parser("wildcards")
        assert doc.report.has_errors is False

    def test_undeclared_children_parsed_generically(self):
        doc = run_parser("wildcards")
        root = doc.root
        children = {child._name_: child for child in root._children_}
        assert "title" in children
        extra = [child for child in root._children_ if child._name_ == "extra"]
        assert len(extra) == 2

    def test_generic_instance_attributes_and_text(self):
        doc = run_parser("wildcards")
        root = doc.root
        extra = next(child for child in root._children_ if child._name_ == "extra")
        assert extra._attribs_ == {"unit": "m"}
        assert extra._value_ == ["3.5"]

    def test_generic_children_recursed(self):
        doc = run_parser("wildcards")
        root = doc.root
        extra = next(child for child in root._children_ if child._name_ == "extra")
        assert extra._children_ == []

    def test_any_attribute_accepted(self):
        doc = run_parser("wildcards")
        root = doc.root
        assert root._attribs_.get("source") == "web"
        codes = [issue.code for issue in doc.report.issues]
        assert "unexpected-attribute" not in codes

    def test_no_wildcard_still_reports_unexpected(self, tmp_path):
        from pyxsd.schema import Schema

        schema = tmp_path / "schema.xsd"
        instance = tmp_path / "instance.xml"
        schema.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t"><xs:sequence>'
            '<xs:element name="v" type="xs:string"/></xs:sequence></xs:complexType>'
            '<xs:element name="root" type="t"/></xs:schema>'
        )
        instance.write_text('<root stray="1"><v>x</v></root>')
        compiled = Schema.compile(str(schema))
        doc = compiled.parse(str(instance))
        codes = [issue.code for issue in doc.report.issues]
        assert "unexpected-attribute" in codes
