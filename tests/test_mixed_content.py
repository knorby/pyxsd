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

# A mixed root with a wildcard: the undeclared ``extra`` child binds as a
# generic pass-through instance (``makeGenericInstance``), and the mixed
# root keeps the serialization free of writer-generated layout whitespace.
WILDCARD_MIXED_DOC = (
    '<xs:element name="doc"><xs:complexType mixed="true"><xs:sequence>'
    '<xs:any processContents="skip" minOccurs="0" maxOccurs="unbounded"/>'
    "</xs:sequence></xs:complexType></xs:element>"
)

# The ur-type declaration binds ``AnyType``, whose class marker makes the
# element character-data-admitting.
ANY_TYPE_ROOT = '<xs:element name="extra" type="xs:anyType"/>'

MIXED_P_ATTR = (
    '<xs:element name="p"><xs:complexType mixed="true">'
    '<xs:choice minOccurs="0" maxOccurs="unbounded">'
    '<xs:element name="em" type="xs:string"/>'
    "</xs:choice>"
    '<xs:attribute name="lang" type="xs:string"/>'
    "</xs:complexType></xs:element>"
)

MIXED_WITH_ELEMENT_ONLY_CHILD = (
    '<xs:element name="p"><xs:complexType mixed="true"><xs:sequence>'
    '<xs:element name="list"><xs:complexType><xs:sequence>'
    '<xs:element name="item" type="xs:string" maxOccurs="unbounded"/>'
    "</xs:sequence></xs:complexType></xs:element>"
    "</xs:sequence></xs:complexType></xs:element>"
)

NESTED_MIXED = (
    '<xs:element name="p"><xs:complexType mixed="true">'
    '<xs:choice minOccurs="0" maxOccurs="unbounded">'
    '<xs:element name="note"><xs:complexType mixed="true"><xs:sequence>'
    '<xs:element name="em" type="xs:string" minOccurs="0"/>'
    "</xs:sequence></xs:complexType></xs:element>"
    "</xs:choice>"
    '<xs:attribute name="lang" type="xs:string"/>'
    "</xs:complexType></xs:element>"
)

HANDOFF_INSTANCE = "<p>Before <em>emphasis</em> between <citation>[1]</citation> after.</p>"


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

    # TestMixedTextCapture — <p/> is valid (choice minOccurs="0") and binds no text
    def test_empty_mixed_element_binds_none(self, tmp_path):
        doc = _parse(MIXED_P, "<p/>", tmp_path)
        assert doc.is_valid
        assert doc.root._value_ is None


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
        assert doc.is_valid
        first, second = doc.root._children_
        assert first._tail_ == " "
        assert not hasattr(second, "_tail_")

    def test_missing_tail_records_nothing(self, tmp_path):
        doc = _parse(MIXED_P, "<p><em>a</em><em>b</em></p>", tmp_path)
        assert doc.is_valid
        assert len(doc.root._children_) == 2
        assert all(not hasattr(child, "_tail_") for child in doc.root._children_)

    def test_element_only_child_order_unchanged(self, tmp_path):
        body = (
            '<xs:element name="a"><xs:complexType><xs:sequence>'
            '<xs:element name="b" type="xs:string" maxOccurs="2"/>'
            "</xs:sequence></xs:complexType></xs:element>"
        )
        doc = _parse(body, "<a><b>1</b><b>2</b></a>", tmp_path)
        assert doc.is_valid
        assert [str(child) for child in doc.root._children_] == ["1", "2"]


class TestAnyTypeAndWildcardBinding:
    """Generic pass-through content keeps character data verbatim.

    ``xs:anyType`` elements and undeclared children admitted by a
    wildcard admit character data. The class marker on ``AnyType`` and
    the instance marker ``makeGenericInstance`` sets are what the writer
    and dict export read to tell such content apart from element-only
    content, so these tests fail when either marker is dropped.
    """

    def test_wildcard_generic_text_binds_verbatim(self, tmp_path):
        doc = _parse(WILDCARD_MIXED_DOC, "<doc><extra>  spaced  text  </extra></doc>", tmp_path)
        assert doc.is_valid
        assert doc.root._children_[0]._value_ == ["  spaced  text  "]

    def test_wildcard_generic_subtree_serializes_inline(self, tmp_path):
        doc = _parse(
            WILDCARD_MIXED_DOC, "<doc><extra>lead <b>bold</b> tail</extra></doc>", tmp_path
        )
        extra = doc.root._children_[0]
        assert extra._value_ == ["lead "]
        assert extra._children_[0]._tail_ == " tail"
        assert _ordered_text(doc.to_string()) == ["lead ", "bold", " tail"]

    def test_anytype_text_binds_verbatim(self, tmp_path):
        doc = _parse(ANY_TYPE_ROOT, "<extra>  spaced  text  </extra>", tmp_path)
        assert doc.is_valid
        assert doc.root._value_ == ["  spaced  text  "]

    def test_anytype_subtree_serializes_inline(self, tmp_path):
        doc = _parse(ANY_TYPE_ROOT, "<extra>lead <b>bold</b> tail</extra>", tmp_path)
        assert doc.root._value_ == ["lead "]
        assert doc.root._children_[0]._tail_ == " tail"
        assert _ordered_text(doc.to_string()) == ["lead ", "bold", " tail"]


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
        assert " between " in "".join(ET.fromstring(doc.to_string()).itertext())

    def test_element_only_output_byte_identical(self, tmp_path):
        body = (
            '<xs:element name="a"><xs:complexType><xs:sequence>'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:sequence></xs:complexType></xs:element>"
        )
        pretty = "<a>\n  <b>x</b>\n</a>"
        before = _parse(body, pretty, tmp_path).to_string()
        # reparse the serialization and confirm output is stable (legacy block
        # layout).
        again = _parse(body, before, tmp_path).to_string()
        assert again == before

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

    # TestMixedSerialization — mixed parent, element-only child with children:
    # mixed-level text is exact; layout whitespace stays inside the element-only subtree
    def test_element_only_child_with_children_in_mixed(self, tmp_path):
        doc = _parse(
            MIXED_WITH_ELEMENT_ONLY_CHILD, "<p>a <list><item>b</item></list> c</p>", tmp_path
        )
        assert doc.is_valid
        assert doc.root._value_ == ["a "]
        assert doc.root._children_[0]._tail_ == " c"
        again = _parse(MIXED_WITH_ELEMENT_ONLY_CHILD, doc.to_string(), tmp_path)
        assert again.is_valid
        assert again.root._value_ == ["a "]
        assert again.root._children_[0]._tail_ == " c"

    # TestMixedSerialization — attributes on a mixed element round-trip
    def test_attributes_on_mixed_element_round_trip(self, tmp_path):
        doc = _parse(MIXED_P_ATTR, '<p lang="en">Before <em>x</em> after</p>', tmp_path)
        assert doc.is_valid
        assert doc.root._attribs_ == {"lang": "en"}
        assert 'lang = "en"' in doc.to_string()
        assert _ordered_text(doc.to_string()) == ["Before ", "x", " after"]

    # TestMixedSerialization — no leading text
    def test_no_leading_text_mixed_round_trip(self, tmp_path):
        doc = _parse(MIXED_P, "<p><em>x</em> tail</p>", tmp_path)
        assert doc.is_valid
        assert doc.root._value_ is None
        assert doc.root._children_[0]._tail_ == " tail"
        assert _ordered_text(doc.to_string()) == ["x", " tail"]


class TestMixedProjections:
    """The XPath and dict projections expose the preserved character data."""

    def test_xpath_sees_full_text(self, tmp_path):
        doc = _parse(MIXED_P, TestMixedSerialization.MIXED_XML, tmp_path)
        assert doc.xpath("string(/p)") == "Before emphasis between [1] after."

    def test_element_only_layout_whitespace_in_text_query(self, tmp_path):
        # Documented consequence of the document-faithful projection: a
        # pretty-printed element-only document's recorded layout tails are
        # part of text-valued query results. The leading indentation is
        # not bound (legacy element-only behavior), so only the child
        # tails appear.
        body = (
            '<xs:element name="a"><xs:complexType><xs:sequence>'
            '<xs:element name="b" type="xs:string" maxOccurs="2"/>'
            "</xs:sequence></xs:complexType></xs:element>"
        )
        doc = _parse(body, "<a>\n  <b>x</b>\n  <b>y</b>\n</a>", tmp_path)
        assert doc.is_valid
        assert doc.xpath("string(/a)") == "x\n  y\n"

    def test_findall_returns_bound_nodes_with_tails(self, tmp_path):
        doc = _parse(MIXED_P, TestMixedSerialization.MIXED_XML, tmp_path)
        em = doc.find("em")
        assert em._tail_ == " between "

    def test_dict_export_keeps_all_character_data(self, tmp_path):
        doc = _parse(MIXED_P, TestMixedSerialization.MIXED_XML, tmp_path)
        exported = doc.to_dict(typed=False)
        assert exported["$"] == "Before  between  after."
        assert exported["em"] == "emphasis"
        assert exported["citation"] == "[1]"

    def test_dict_export_element_only_unchanged(self, tmp_path):
        body = (
            '<xs:element name="a"><xs:complexType><xs:sequence>'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:sequence></xs:complexType></xs:element>"
        )
        doc = _parse(body, "<a><b>x</b></a>", tmp_path)
        assert doc.to_dict(typed=False) == {"b": "x"}

    def test_dict_export_pretty_element_only_unchanged(self, tmp_path):
        # Element-only content records whitespace tails on its children
        # (they are inert there), and the export ignores them exactly as
        # the writer does; only character-data-admitting content gains
        # the ``$`` text.
        body = (
            '<xs:element name="a"><xs:complexType><xs:sequence>'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:sequence></xs:complexType></xs:element>"
        )
        doc = _parse(body, "<a>\n  <b>x</b>\n</a>", tmp_path)
        assert doc.to_dict(typed=False) == {"b": "x"}

    # TestMixedProjections — nested mixed dict export
    def test_dict_export_nested_mixed(self, tmp_path):
        doc = _parse(NESTED_MIXED, '<p lang="en">a <note>x <em>y</em> z</note> b</p>', tmp_path)
        assert doc.is_valid
        assert doc.to_dict() == {"@lang": "en", "note": {"em": "y", "$": "x  z"}, "$": "a  b"}

    # TestMixedProjections — dict options leave the mixed $ intact
    def test_dict_export_mixed_typed(self, tmp_path):
        doc = _parse(MIXED_P, HANDOFF_INSTANCE, tmp_path)
        assert doc.is_valid
        assert doc.to_dict(typed=True) == {
            "em": "emphasis",
            "citation": "[1]",
            "$": "Before  between  after.",
        }

    def test_dict_export_mixed_always_list(self, tmp_path):
        doc = _parse(MIXED_P, HANDOFF_INSTANCE, tmp_path)
        assert doc.is_valid
        assert doc.to_dict(always_list=True) == {
            "em": ["emphasis"],
            "citation": ["[1]"],
            "$": "Before  between  after.",
        }


class TestMixedValidation:
    """Preserving mixed text does not relax instance validation."""

    def test_undeclared_child_still_rejected(self, tmp_path):
        doc = _parse(MIXED_P, "<p>text <bogus>x</bogus></p>", tmp_path)
        assert not doc.is_valid
        assert any(i.code == "unexpected-element" for i in doc.report.issues)

    def test_sequence_occurrence_limits_enforced_in_mixed(self, tmp_path):
        body = (
            '<xs:element name="p"><xs:complexType mixed="true">'
            '<xs:sequence><xs:element name="em" type="xs:string"/></xs:sequence>'
            "</xs:complexType></xs:element>"
        )
        doc = _parse(body, "<p><em>a</em><em>b</em></p>", tmp_path)
        assert not doc.is_valid

    def test_element_only_character_content_still_rejected(self, tmp_path):
        body = (
            '<xs:element name="a"><xs:complexType><xs:sequence>'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:sequence></xs:complexType></xs:element>"
        )
        doc = _parse(body, "<a>stray<b>x</b></a>", tmp_path)
        assert any(i.code == "unexpected-character" for i in doc.report.issues)
