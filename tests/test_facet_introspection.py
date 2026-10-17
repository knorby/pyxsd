import io
import re

import pytest

from pyxsd import facets
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


class TestCompilePattern:
    def test_returns_compiled_pattern(self):
        pattern = facets.compile_pattern("[0-9]{3}")
        assert isinstance(pattern, re.Pattern)
        assert pattern.fullmatch("123")

    def test_xsd_class_subtraction(self):
        assert facets.compile_pattern("[a-z-[aeiou]]+").fullmatch("rhythm")

    def test_illegal_pattern_raises(self):
        with pytest.raises(ValueError):
            facets.compile_pattern("(a)\\1")


class TestPatternsAccessor:
    def test_no_facets_returns_empty(self):
        assert String.patterns() == ()

    def test_schema_derived_type_exposes_pattern(self):
        body = ELEMENT.format(base="xs:string", facets='<xs:pattern value="[a-z]+"/>')
        patterns = root_class(body, "<r>ab</r>").patterns()
        assert len(patterns) == 1
        assert len(patterns[0]) == 1
        assert patterns[0][0].fullmatch("ab")
        assert not patterns[0][0].fullmatch("a1")

    def test_sibling_patterns_form_one_or_level(self):
        body = ELEMENT.format(
            base="xs:string",
            facets='<xs:pattern value="[a-z]+"/><xs:pattern value="[0-9]+"/>',
        )
        patterns = root_class(body, "<r>ab</r>").patterns()
        assert len(patterns) == 1
        assert len(patterns[0]) == 2

    def test_restriction_steps_accumulate_as_and_levels(self):
        body = (
            '<xs:simpleType name="A"><xs:restriction base="xs:string">'
            '<xs:pattern value="[a-z]+"/></xs:restriction></xs:simpleType>'
            '<xs:element name="r"><xs:simpleType><xs:restriction base="A">'
            '<xs:pattern value="a.*"/></xs:restriction></xs:simpleType></xs:element>'
        )
        patterns = root_class(body, "<r>abc</r>").patterns()
        assert len(patterns) == 2
        assert all(len(level) == 1 for level in patterns)
