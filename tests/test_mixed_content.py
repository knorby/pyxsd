"""Mixed-content character data is bound verbatim and written inline.

A type whose content is mixed (or otherwise admits character data)
keeps the element's exact text, including leading and trailing
whitespace, as the first ``_value_`` entry. Element-only content keeps
the legacy behavior: insignificant whitespace is not bound. The writer
serializes a bound mixed element without generating any whitespace
inside it, so the ordered text survives a parse/serialize/parse round
trip.
"""

import xml.etree.ElementTree as ET

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema

XSD = "http://www.w3.org/2001/XMLSchema"


def _parse(schema_body: str, instance_text: str, tmp_path, mode=ParseModes.STRICT):
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(f'<xs:schema xmlns:xs="{XSD}">{schema_body}</xs:schema>')
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance_text)
    return Schema.compile(str(schema_path), mode=mode).parse(str(instance_path))


def _parse_document(schema_text: str, instance_text: str, tmp_path, mode=ParseModes.STRICT):
    """Compiles a complete schema document (already inside ``xs:schema``)."""
    schema_path = tmp_path / "schema-document.xsd"
    schema_path.write_text(schema_text)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance_text)
    return Schema.compile(str(schema_path), mode=mode).parse(str(instance_path))


def _ordered_text(xml_text: str) -> list[str]:
    """Returns the serialized document's text pieces in document order.

    The writer's leading creation comment is before the root element,
    where ElementTree ignores it, so the serialization parses as-is.
    """
    return list(ET.fromstring(xml_text).itertext())


MIXED_P = (
    '<xs:element name="p"><xs:complexType mixed="true">'
    '<xs:choice minOccurs="0" maxOccurs="unbounded">'
    '<xs:element name="em" type="xs:string"/>'
    '<xs:element name="citation" type="xs:string"/>'
    "</xs:choice></xs:complexType></xs:element>"
)


class TestMixedTextCapture:
    def test_leading_text_is_verbatim(self, tmp_path):
        doc = _parse(MIXED_P, "<p>Before <em>emphasis</em> after.</p>", tmp_path)
        assert doc.is_valid
        assert doc.root._value_ == ["Before "]  # trailing space preserved

    def test_whitespace_only_text_is_preserved(self, tmp_path):
        doc = _parse(MIXED_P, "<p> <em>x</em></p>", tmp_path)
        assert doc.root._value_ == [" "]

    def test_element_only_text_behavior_unchanged(self, tmp_path):
        body = (
            '<xs:element name="a"><xs:complexType><xs:sequence>'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:sequence></xs:complexType></xs:element>"
        )
        doc = _parse(body, "<a>\n  <b>x</b>\n</a>", tmp_path)
        assert doc.root._value_ is None  # legacy: insignificant whitespace not bound


class TestMixedTailCapture:
    def test_tails_recorded_in_order(self, tmp_path):
        doc = _parse(
            MIXED_P,
            "<p>Before <em>emphasis</em> between <citation>[1]</citation> after.</p>",
            tmp_path,
        )
        assert doc.is_valid
        em, citation = doc.root._children_
        assert em._tail_ == " between "
        assert citation._tail_ == " after."

    def test_whitespace_only_tail_preserved(self, tmp_path):
        doc = _parse(MIXED_P, "<p><em>a</em> <em>b</em></p>", tmp_path)
        first, second = doc.root._children_
        assert first._tail_ == " "
        assert not hasattr(second, "_tail_")

    def test_missing_tail_records_nothing(self, tmp_path):
        doc = _parse(MIXED_P, "<p><em>a</em><em>b</em></p>", tmp_path)
        assert all(not hasattr(child, "_tail_") for child in doc.root._children_)

    def test_element_only_child_order_unchanged(self, tmp_path):
        body = (
            '<xs:element name="a"><xs:complexType><xs:sequence>'
            '<xs:element name="b" type="xs:string" maxOccurs="2"/>'
            "</xs:sequence></xs:complexType></xs:element>"
        )
        doc = _parse(body, "<a><b>1</b><b>2</b></a>", tmp_path)
        assert [str(child) for child in doc.root._children_] == ["1", "2"]


class TestMixedSerialization:
    """The bound writer keeps mixed content inline and ordered."""

    MIXED_XML = "<p>Before <em>emphasis</em> between <citation>[1]</citation> after.</p>"

    def test_ordered_text_survives_binding(self, tmp_path):
        doc = _parse(MIXED_P, self.MIXED_XML, tmp_path)
        doc.require_valid()
        assert _ordered_text(doc.to_string()) == [
            "Before ",
            "emphasis",
            " between ",
            "[1]",
            " after.",
        ]

    def test_round_trip_is_stable(self, tmp_path):
        doc = _parse(MIXED_P, self.MIXED_XML, tmp_path)
        again = doc.revalidate()
        assert _ordered_text(again.to_string()) == _ordered_text(doc.to_string())

    def test_whitespace_only_separators_round_trip(self, tmp_path):
        doc = _parse(MIXED_P, "<p><em>a</em> <em>b</em> </p>", tmp_path)
        assert _ordered_text(doc.to_string()) == ["a", " ", "b", " "]

    def test_nested_mixed_inline(self, tmp_path):
        body = (
            '<xs:element name="p"><xs:complexType mixed="true">'
            '<xs:choice minOccurs="0" maxOccurs="unbounded">'
            '<xs:element name="em"><xs:complexType mixed="true">'
            '<xs:choice minOccurs="0" maxOccurs="unbounded">'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:choice></xs:complexType></xs:element>"
            "</xs:choice></xs:complexType></xs:element>"
        )
        doc = _parse(body, "<p>x <em>y <b>z</b> w</em> v</p>", tmp_path)
        assert _ordered_text(doc.to_string()) == ["x ", "y ", "z", " w", " v"]

    def test_write_through_keeps_tail(self, tmp_path):
        doc = _parse(MIXED_P, self.MIXED_XML, tmp_path)
        doc.root.em = "replaced"
        assert " between " in "".join(ET.fromstring(doc.to_string().split("\n", 1)[1]).itertext())

    def test_element_only_output_byte_identical(self, tmp_path):
        body = (
            '<xs:element name="a"><xs:complexType><xs:sequence>'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:sequence></xs:complexType></xs:element>"
        )
        pretty = "<a>\n  <b>x</b>\n</a>"
        before = _parse(body, pretty, tmp_path).to_string()
        # reparse the serialization and confirm output is stable (legacy block layout)
        assert _parse(body, before, tmp_path).to_string() == before

    def test_consumer_shape_strict_namespaces(self, tmp_path):
        ns = "urn:oii:2:content"
        schema_text = (
            f'<xs:schema xmlns:xs="{XSD}" targetNamespace="{ns}" '
            f'xmlns:c="{ns}" elementFormDefault="qualified">'
            '<xs:element name="p"><xs:complexType mixed="true">'
            '<xs:choice minOccurs="0" maxOccurs="unbounded">'
            '<xs:element name="em" type="xs:string"/>'
            '<xs:element name="citation" type="xs:string"/>'
            "</xs:choice></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        instance_text = (
            f'<p xmlns="{ns}">Before <em>emphasis</em> between <citation>[1]</citation> after.</p>'
        )
        doc = _parse_document(
            schema_text,
            instance_text,
            tmp_path,
            mode=ParseModes.LAX.replace(namespaces="strict"),
        )
        doc.require_valid()
        assert _ordered_text(doc.to_string()) == [
            "Before ",
            "emphasis",
            " between ",
            "[1]",
            " after.",
        ]
