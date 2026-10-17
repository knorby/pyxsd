import io

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema
from pyxsd.xsd_data_types import String

SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
{body}
</xs:schema>"""


def root_class(body, xml):
    return type(
        Schema.compile(io.StringIO(SCHEMA.format(body=body)), mode=ParseModes.NAMESPACED)
        .parse(io.StringIO(xml))
        .root
    )


ELEMENT = (
    '<xs:element name="r"><xs:simpleType>'
    '<xs:restriction base="{base}">{facets}</xs:restriction>'
    "</xs:simpleType></xs:element>"
)


class TestEnumerationsAccessor:
    def test_no_facets_returns_empty(self):
        assert String.enumerations() == ()

    def test_string_enumeration_values(self):
        body = ELEMENT.format(
            base="xs:string",
            facets='<xs:enumeration value="red"/><xs:enumeration value="green"/>',
        )
        assert [str(v) for v in root_class(body, "<r>red</r>").enumerations()] == ["red", "green"]

    def test_restriction_intersects(self):
        body = (
            '<xs:simpleType name="A"><xs:restriction base="xs:string">'
            '<xs:enumeration value="a"/><xs:enumeration value="b"/></xs:restriction></xs:simpleType>'
            '<xs:element name="r"><xs:simpleType><xs:restriction base="A">'
            '<xs:enumeration value="b"/></xs:restriction></xs:simpleType></xs:element>'
        )
        assert [str(v) for v in root_class(body, "<r>b</r>").enumerations()] == ["b"]

    def test_integer_enumeration_values_are_typed(self):
        body = ELEMENT.format(base="xs:int", facets='<xs:enumeration value="1"/>')
        (value,) = root_class(body, "<r>1</r>").enumerations()
        assert int(value) == 1
