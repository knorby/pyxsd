"""Particle-level content-model tests.

The legacy flat order checks matched element occurrences by name and
dropped unmatched children silently. These tests pin the compiled
particle model: complete consumption of closed content models,
per-particle (not per-leaf) occurrence semantics, and restriction
replacing the base particle tree.
"""

import xml.etree.ElementTree as ET

from pyxsd.content_model import Particle
from pyxsd.schema import Schema

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


def _parse(schema_body, instance, tmp_path):
    schema = f"<xs:schema {XS}>{schema_body}</xs:schema>"
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(schema)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance)
    return Schema.compile(str(schema_path)).parse(str(instance_path))


def _element(name, type_="xs:string", attrs=""):
    return f'<xs:element name="{name}" type="{type_}" {attrs}/>'


def _root(model):
    return f'<xs:element name="r"><xs:complexType>{model}</xs:complexType></xs:element>'


def _codes(doc):
    return [issue.code for issue in doc.report.issues]


class TestClosedContentModel:
    def test_trailing_undeclared_child_is_rejected(self, tmp_path):
        doc = _parse(
            _root("<xs:sequence>" + _element("a") + "</xs:sequence>"),
            "<r><a/><z/></r>",
            tmp_path,
        )
        assert "unexpected-element" in _codes(doc)

    def test_trailing_repeat_of_declared_child_is_rejected(self, tmp_path):
        doc = _parse(
            _root("<xs:sequence>" + _element("a") + _element("b") + "</xs:sequence>"),
            "<r><a/><b/><a/></r>",
            tmp_path,
        )
        assert "order" in _codes(doc)

    def test_unknown_choice_branch_is_rejected(self, tmp_path):
        doc = _parse(
            _root("<xs:choice>" + _element("a") + _element("b") + "</xs:choice>"),
            "<r><z/></r>",
            tmp_path,
        )
        assert "unexpected-element" in _codes(doc)

    def test_children_of_empty_type_are_rejected(self, tmp_path):
        doc = _parse(_root(""), "<r><z/></r>", tmp_path)
        assert "unexpected-element" in _codes(doc)

    def test_empty_required_choice_rejects_empty_element(self, tmp_path):
        # Saxon complex022.n1: an empty choice with minOccurs=1 is
        # unsatisfiable, so even an empty element is invalid.
        doc = _parse(_root("<xs:choice/>"), "<r/>", tmp_path)
        assert "occurrence-min" in _codes(doc)

    def test_optional_empty_choice_accepts_empty_element(self, tmp_path):
        doc = _parse(_root('<xs:choice minOccurs="0"/>'), "<r/>", tmp_path)
        assert not doc.report.has_errors

    def test_valid_sequence_stays_clean(self, tmp_path):
        doc = _parse(
            _root("<xs:sequence>" + _element("a") + _element("b") + "</xs:sequence>"),
            "<r><a/><b/></r>",
            tmp_path,
        )
        assert not doc.report.has_errors


class TestParticleOccurrences:
    def test_repeated_choice_accepts_repeat_of_one_branch(self, tmp_path):
        doc = _parse(
            _root('<xs:choice maxOccurs="2">' + _element("a") + _element("b") + "</xs:choice>"),
            "<r><a/><a/></r>",
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_optional_sequence_with_required_child_can_be_absent(self, tmp_path):
        doc = _parse(
            _root('<xs:sequence minOccurs="0">' + _element("a") + "</xs:sequence>"),
            "<r/>",
            tmp_path,
        )
        assert not doc.report.has_errors

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
        doc = _parse(schema, "<r/>", tmp_path)
        assert "occurrence-min" in _codes(doc)


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
        doc = _parse(self.SCHEMA, "<r><a/><b/></r>", tmp_path)
        assert "unexpected-element" in _codes(doc)

    def test_restriction_allows_remaining_particle(self, tmp_path):
        doc = _parse(self.SCHEMA, "<r><a/></r>", tmp_path)
        assert not doc.report.has_errors


class TestDerivedSimpleTypeDispatch:
    """Schema-derived simple types must not take the complex path."""

    SCHEMA = '<xs:simpleType name="T"><xs:restriction base="xs:int"/></xs:simpleType>' + _element(
        "r", "T"
    )

    def test_derived_simple_type_root(self, tmp_path):
        doc = _parse(self.SCHEMA, "<r>42</r>", tmp_path)
        assert not doc.report.has_errors

    def test_derived_simple_type_as_child(self, tmp_path):
        schema = '<xs:simpleType name="T"><xs:restriction base="xs:int"/></xs:simpleType>' + _root(
            '<xs:sequence><xs:element name="c" type="T"/></xs:sequence>'
        )
        doc = _parse(schema, "<r><c>42</c></r>", tmp_path)
        assert not doc.report.has_errors

    def test_derived_simple_type_invalid_value(self, tmp_path):
        doc = _parse(self.SCHEMA, "<r>abc</r>", tmp_path)
        assert "value" in _codes(doc)


class TestAllExtensionComposition:
    """An ``all`` extending an ``all`` composes into one unordered group.

    XSD 1.1 §3.4.2.3.3 clause 4.2.3.2: the base all's particles are
    followed by the extension all's, with the extension's ``minOccurs``.
    Treating the extension as ``sequence[base, own]`` would require the
    base's members to precede the extension's (rejecting a valid
    interleaving) while letting a partially present optional group
    through (Saxon all314).
    """

    def test_all_extension_is_order_insensitive(self, tmp_path):
        doc = _parse(
            '<xs:complexType name="b"><xs:all>'
            '<xs:element name="a"/></xs:all></xs:complexType>'
            '<xs:complexType name="t"><xs:complexContent>'
            '<xs:extension base="b"><xs:all>'
            '<xs:element name="c"/></xs:all>'
            "</xs:extension></xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="t"/>',
            "<r><c/><a/></r>",
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_all_extension_requires_every_member_when_group_present(self, tmp_path):
        # Saxon all314: both groups are optional, but a present group
        # requires all of its members
        schema = (
            '<xs:complexType name="b"><xs:all minOccurs="0">'
            '<xs:element name="a"/></xs:all></xs:complexType>'
            '<xs:complexType name="t"><xs:complexContent>'
            '<xs:extension base="b"><xs:all minOccurs="0">'
            '<xs:element name="c"/></xs:all>'
            "</xs:extension></xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="t"/>'
        )
        assert not _parse(schema, "<r><c/><a/></r>", tmp_path).report.has_errors
        assert not _parse(schema, "<r/>", tmp_path).report.has_errors
        assert _parse(schema, "<r><a/></r>", tmp_path).report.has_errors
        assert _parse(schema, "<r><c/></r>", tmp_path).report.has_errors

    def test_all_extension_keeps_base_required_members(self, tmp_path):
        doc = _parse(
            '<xs:complexType name="b"><xs:all>'
            '<xs:element name="a"/></xs:all></xs:complexType>'
            '<xs:complexType name="t"><xs:complexContent>'
            '<xs:extension base="b"><xs:all>'
            '<xs:element name="c"/></xs:all>'
            "</xs:extension></xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="t"/>',
            "<r><c/></r>",
            tmp_path,
        )
        assert doc.report.has_errors


class TestAllGroupReferenceMembers:
    """A group reference to an ``all`` group inside an ``all`` (all007).

    The reference is transparent: its group's members are members of the
    enclosing ``all`` and may appear in any order alongside the other
    members.
    """

    SCHEMA = (
        '<xs:complexType name="t"><xs:all>'
        '<xs:element name="a" minOccurs="0" maxOccurs="5"/>'
        '<xs:group ref="allgroup"/></xs:all></xs:complexType>'
        '<xs:group name="allgroup"><xs:all>'
        '<xs:element name="b" minOccurs="1" maxOccurs="5"/>'
        '<xs:element name="c" minOccurs="2" maxOccurs="unbounded"/>'
        '<xs:element name="d" minOccurs="1" maxOccurs="1"/>'
        "</xs:all></xs:group>"
        '<xs:element name="r" type="t"/>'
    )

    def test_members_match_in_any_order(self, tmp_path):
        # Saxon all007.v01 (an all001 document)
        doc = _parse(
            self.SCHEMA,
            "<r><a/><b/><d/><c/><a/><c/><c/><a/><a/><b/></r>",
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_group_member_occurrence_limits_are_enforced(self, tmp_path):
        # Saxon all007.n01 (too few c elements)
        doc = _parse(
            self.SCHEMA,
            "<r><a/><b/><d/><a/><c/><a/><a/><b/></r>",
            tmp_path,
        )
        assert doc.report.has_errors


class TestElementOnlyCharacters:
    """Character data under element-only complex content is invalid.

    XSD §3.4.3.2 (Element Locally Valid (Complex Type)): an element with
    an element-only content type has no character content other than
    whitespace. Mixed types and simple content are unaffected. The
    all-extends-all composition makes this observable (Saxon all307:
    the same document that is valid against a mixed type is invalid
    against its element-only twin).
    """

    def test_text_under_element_only_content_is_rejected(self, tmp_path):
        doc = _parse(
            _root("<xs:sequence>" + _element("a") + "</xs:sequence>"),
            "<r>stray<a/>text</r>",
            tmp_path,
        )
        assert "unexpected-character" in _codes(doc)

    def test_whitespace_between_children_is_fine(self, tmp_path):
        doc = _parse(
            _root("<xs:sequence>" + _element("a") + "</xs:sequence>"),
            "<r>\n  <a/>\n</r>",
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_mixed_content_keeps_its_text(self, tmp_path):
        doc = _parse(
            '<xs:element name="r"><xs:complexType mixed="true"><xs:sequence>'
            + _element("a")
            + "</xs:sequence></xs:complexType></xs:element>",
            "<r>text<a/>tail</r>",
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_all_extension_element_only_composition_rejects_text(self, tmp_path):
        # Saxon all307.n01: all306's document (with text) against the
        # element-only twin of the type
        schema = (
            '<xs:complexType name="b"><xs:all>'
            '<xs:element name="a" minOccurs="0" maxOccurs="5"/>'
            '<xs:element name="b" minOccurs="0" maxOccurs="5"/>'
            '<xs:element name="c" minOccurs="0" maxOccurs="unbounded"/>'
            "</xs:all></xs:complexType>"
            '<xs:complexType name="t"><xs:complexContent>'
            '<xs:extension base="b"><xs:all>'
            '<xs:element name="d" minOccurs="0" maxOccurs="1"/>'
            '<xs:element name="e" minOccurs="0" maxOccurs="4"/>'
            "</xs:all></xs:extension></xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="t"/>'
        )
        doc = _parse(schema, "<r><b/>text<a/>text<d/>text<a/></r>", tmp_path)
        assert doc.report.has_errors


class TestMixedInheritanceThroughEmptyExtension:
    """An extension with empty explicit content inherits its base's content type.

    XSD 1.1 §3.4.2.3.3 clause 4.2.2: when the base's content type is
    element-only or mixed and the extension's effective content is empty
    (an attribute-only or bare extension), the derived type's content
    type *is the base's*. A mixed base therefore keeps its text
    allowance through such an extension.
    """

    def test_attribute_extension_of_mixed_sequence_base_keeps_text(self, tmp_path):
        doc = _parse(
            '<xs:complexType name="b" mixed="true"><xs:sequence>'
            '<xs:element name="a" minOccurs="0"/></xs:sequence></xs:complexType>'
            '<xs:complexType name="t"><xs:complexContent>'
            '<xs:extension base="b"><xs:attribute name="x" type="xs:string"/></xs:extension>'
            "</xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="t"/>',
            "<r>text<a/></r>",
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_bare_extension_of_mixed_sequence_base_keeps_text(self, tmp_path):
        doc = _parse(
            '<xs:complexType name="b" mixed="true"><xs:sequence>'
            '<xs:element name="a" minOccurs="0"/></xs:sequence></xs:complexType>'
            '<xs:complexType name="t"><xs:complexContent>'
            '<xs:extension base="b"/></xs:complexContent></xs:complexType>'
            '<xs:element name="r" type="t"/>',
            "<r>text</r>",
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_attribute_extension_of_mixed_all_base_keeps_text(self, tmp_path):
        doc = _parse(
            '<xs:complexType name="b" mixed="true"><xs:all>'
            '<xs:element name="a" minOccurs="0"/></xs:all></xs:complexType>'
            '<xs:complexType name="t"><xs:complexContent>'
            '<xs:extension base="b"><xs:attribute name="x" type="xs:string"/></xs:extension>'
            "</xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="t"/>',
            "<r>text<a/></r>",
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_element_only_base_still_rejects_text_through_empty_extension(self, tmp_path):
        # the inheritance is the base's content type, not a blanket
        # allowance: an element-only base stays element-only
        doc = _parse(
            '<xs:complexType name="b"><xs:sequence>'
            '<xs:element name="a" minOccurs="0"/></xs:sequence></xs:complexType>'
            '<xs:complexType name="t"><xs:complexContent>'
            '<xs:extension base="b"><xs:attribute name="x" type="xs:string"/></xs:extension>'
            "</xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="t"/>',
            "<r>text<a/></r>",
            tmp_path,
        )
        assert "unexpected-character" in _codes(doc)


class TestRepeatedParticleSearchCost:
    """The Z034/Z036 shape: ``sequence[a{1,unbounded}]{1,100}, b, ...``.

    ``_ends_repeated`` walks a repetition frontier one step at a time.
    Once a step's end positions are all already reachable within the
    occurrence bounds (the Z034/Z036 frontiers shrink by one position per
    step), every later step is contained in the accumulated result too,
    so the walk must stop; it otherwise expands each bounded repetition
    O(n) times per starting position on ~1000-element documents.
    """

    NODES = 101

    @staticmethod
    def _model():
        return Particle(
            "sequence",
            1,
            1,
            [
                Particle("sequence", 1, 100, [Particle("element", 1, None, [], "a")]),
                Particle("element", 1, 1, [], "b"),
                Particle("sequence", 1, 100, [Particle("element", 1, None, [], "a")]),
            ],
        )

    @staticmethod
    def _nodes():
        return (
            [ET.Element("a") for _ in range(10)]
            + [ET.Element("b")]
            + [ET.Element("a") for _ in range(90)]
        )

    def test_exact_end_positions_are_preserved(self):
        from pyxsd import content_model

        nodes = self._nodes()
        ctx = content_model._MatchContext({}, content_model._name_of, None, False, None)
        ends = content_model._ends_repeated(self._model(), nodes, 0, ctx, {}, 0)
        assert ends == frozenset(range(12, self.NODES + 1))
        assert content_model.match_content(self._model(), nodes) == (True, [])

    def test_repetition_walk_stops_at_the_fixed_point(self, monkeypatch):
        from pyxsd import content_model

        calls = 0
        original = content_model._ends_one

        def counting(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(content_model, "_ends_one", counting)
        assert content_model.match_content(self._model(), self._nodes()) == (True, [])
        assert calls <= 5 * self.NODES**2, calls
