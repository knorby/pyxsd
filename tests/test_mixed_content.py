"""Mixed-content character data is bound verbatim.

A type whose content is mixed (or otherwise admits character data)
keeps the element's exact text, including leading and trailing
whitespace, as the first ``_value_`` entry. Element-only content keeps
the legacy behavior: insignificant whitespace is not bound.
"""

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema

XSD = "http://www.w3.org/2001/XMLSchema"


def _parse(schema_body: str, instance_text: str, tmp_path, mode=ParseModes.STRICT):
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(f'<xs:schema xmlns:xs="{XSD}">{schema_body}</xs:schema>')
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance_text)
    return Schema.compile(str(schema_path), mode=mode).parse(str(instance_path))


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
