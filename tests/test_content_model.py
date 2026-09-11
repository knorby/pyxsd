"""Particle-level content-model tests (R2, R3, R4, R8).

The legacy flat order checks matched element occurrences by name and
dropped unmatched children silently. These tests pin the compiled
particle model: complete consumption of closed content models,
per-particle (not per-leaf) occurrence semantics, and restriction
replacing the base particle tree.
"""

from pyxsd.parser import PyXSD

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


def _parse(schema_body, instance, tmp_path):
    schema = f"<xs:schema {XS}>{schema_body}</xs:schema>"
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(schema)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance)
    return PyXSD(
        instance_path,
        xsdFile=schema_path,
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
    )


def _element(name, type_="xs:string", attrs=""):
    return f'<xs:element name="{name}" type="{type_}" {attrs}/>'


def _root(model):
    return f'<xs:element name="r"><xs:complexType>{model}</xs:complexType></xs:element>'


def _codes(parser):
    return [issue.code for issue in parser.report.issues]


class TestClosedContentModel:
    def test_trailing_undeclared_child_is_rejected(self, tmp_path):
        parser = _parse(
            _root("<xs:sequence>" + _element("a") + "</xs:sequence>"),
            "<r><a/><z/></r>",
            tmp_path,
        )
        assert "unexpected-element" in _codes(parser)

    def test_trailing_repeat_of_declared_child_is_rejected(self, tmp_path):
        parser = _parse(
            _root("<xs:sequence>" + _element("a") + _element("b") + "</xs:sequence>"),
            "<r><a/><b/><a/></r>",
            tmp_path,
        )
        assert "order" in _codes(parser)

    def test_unknown_choice_branch_is_rejected(self, tmp_path):
        parser = _parse(
            _root("<xs:choice>" + _element("a") + _element("b") + "</xs:choice>"),
            "<r><z/></r>",
            tmp_path,
        )
        assert "unexpected-element" in _codes(parser)

    def test_children_of_empty_type_are_rejected(self, tmp_path):
        parser = _parse(_root(""), "<r><z/></r>", tmp_path)
        assert "unexpected-element" in _codes(parser)

    def test_valid_sequence_stays_clean(self, tmp_path):
        parser = _parse(
            _root("<xs:sequence>" + _element("a") + _element("b") + "</xs:sequence>"),
            "<r><a/><b/></r>",
            tmp_path,
        )
        assert not parser.report.has_errors


class TestParticleOccurrences:
    def test_repeated_choice_accepts_repeat_of_one_branch(self, tmp_path):
        parser = _parse(
            _root('<xs:choice maxOccurs="2">' + _element("a") + _element("b") + "</xs:choice>"),
            "<r><a/><a/></r>",
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_optional_sequence_with_required_child_can_be_absent(self, tmp_path):
        parser = _parse(
            _root('<xs:sequence minOccurs="0">' + _element("a") + "</xs:sequence>"),
            "<r/>",
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_repeated_group_repeats_as_a_unit(self, tmp_path):
        group = (
            '<xs:group name="g"><xs:sequence>'
            + _element("a")
            + _element("b")
            + "</xs:sequence></xs:group>"
        )
        ref = _root('<xs:group ref="g" minOccurs="2" maxOccurs="2"/>')
        schema = group + ref
        valid = _parse(schema, "<r><a/><b/><a/><b/></r>", tmp_path)
        assert not valid.report.has_errors
        invalid = _parse(schema, "<r><a/><a/><b/><b/></r>", tmp_path)
        assert invalid.report.has_errors

    def test_nested_choice_in_sequence(self, tmp_path):
        model = (
            "<xs:sequence>"
            + _element("a")
            + "<xs:choice>"
            + _element("b")
            + _element("c")
            + "</xs:choice>"
            + "</xs:sequence>"
        )
        valid = _parse(_root(model), "<r><a/><b/></r>", tmp_path)
        assert not valid.report.has_errors
        missing = _parse(_root(model), "<r><a/></r>", tmp_path)
        assert missing.report.has_errors


class TestSharedGroupReference:
    def test_optional_reference_does_not_relax_required_one(self, tmp_path):
        schema = (
            '<xs:group name="g"><xs:sequence>' + _element("a") + "</xs:sequence></xs:group>"
            '<xs:complexType name="Required"><xs:group ref="g"/></xs:complexType>'
            '<xs:complexType name="Optional">'
            '<xs:group ref="g" minOccurs="0"/>'
            "</xs:complexType>" + _element("r", "Required")
        )
        parser = _parse(schema, "<r/>", tmp_path)
        assert "occurrence-min" in _codes(parser)


class TestComplexRestriction:
    SCHEMA = (
        '<xs:complexType name="B"><xs:sequence>'
        + _element("a")
        + _element("b", attrs='minOccurs="0"')
        + "</xs:sequence></xs:complexType>"
        '<xs:complexType name="D"><xs:complexContent>'
        '<xs:restriction base="B"><xs:sequence>' + _element("a") + "</xs:sequence></xs:restriction>"
        "</xs:complexContent></xs:complexType>" + _element("r", "D")
    )

    def test_restriction_removes_base_particle(self, tmp_path):
        parser = _parse(self.SCHEMA, "<r><a/><b/></r>", tmp_path)
        assert "unexpected-element" in _codes(parser)

    def test_restriction_allows_remaining_particle(self, tmp_path):
        parser = _parse(self.SCHEMA, "<r><a/></r>", tmp_path)
        assert not parser.report.has_errors


class TestDerivedSimpleTypeDispatch:
    """Schema-derived simple types must not take the complex path (R1)."""

    SCHEMA = '<xs:simpleType name="T"><xs:restriction base="xs:int"/></xs:simpleType>' + _element(
        "r", "T"
    )

    def test_derived_simple_type_root(self, tmp_path):
        parser = _parse(self.SCHEMA, "<r>42</r>", tmp_path)
        assert not parser.report.has_errors

    def test_derived_simple_type_as_child(self, tmp_path):
        schema = '<xs:simpleType name="T"><xs:restriction base="xs:int"/></xs:simpleType>' + _root(
            '<xs:sequence><xs:element name="c" type="T"/></xs:sequence>'
        )
        parser = _parse(schema, "<r><c>42</c></r>", tmp_path)
        assert not parser.report.has_errors

    def test_derived_simple_type_invalid_value(self, tmp_path):
        parser = _parse(self.SCHEMA, "<r>abc</r>", tmp_path)
        assert "value" in _codes(parser)
