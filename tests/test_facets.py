"""Unit tests for XSD facet enforcement.

Facets are exercised through concrete schemas rather than by calling the
machinery directly, so the tests pin the user-visible contract: a
violation is reported as an error on the parser's report, and a
conforming value is silent.
"""

import io

import pytest

from pyxsd.binding import BindingPolicy, ParseModes
from pyxsd.parser import PyXSD

SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
{body}
</xs:schema>"""


def parse(body, xml, mode=ParseModes.NAMESPACED):
    parser = PyXSD(
        io.StringIO(xml),
        io.StringIO(SCHEMA.format(body=body)),
        xmlFileOutput=False,
        transformOutputName=None,
        mode=mode,
    )
    return parser.report


def errors(report):
    return [(issue.code, issue.message) for issue in report.errors]


ELEMENT = '<xs:element name="r">{simple}</xs:element>'
SIMPLE = '<xs:simpleType><xs:restriction base="{base}">{facets}</xs:restriction></xs:simpleType>'


def element(base, facets):
    return ELEMENT.format(simple=SIMPLE.format(base=base, facets=facets))


# --- length family ---------------------------------------------------------


def test_length_accepts_exact():
    assert errors(parse(element("xs:string", '<xs:length value="3"/>'), "<r>abc</r>")) == []


def test_length_rejects_other():
    assert errors(parse(element("xs:string", '<xs:length value="3"/>'), "<r>abcd</r>"))


def test_min_length_and_max_length():
    body = element("xs:string", '<xs:minLength value="2"/><xs:maxLength value="3"/>')
    assert errors(parse(body, "<r>ab</r>")) == []
    assert errors(parse(body, "<r>a</r>"))
    assert errors(parse(body, "<r>abcd</r>"))


def test_length_counts_hex_binary_octets():
    body = element("xs:hexBinary", '<xs:length value="1"/>')
    assert errors(parse(body, "<r>FF</r>")) == []
    assert errors(parse(body, "<r>FFFF</r>"))


def test_length_counts_base64_octets():
    body = element("xs:base64Binary", '<xs:length value="1"/>')
    assert errors(parse(body, "<r>QQ==</r>")) == []
    assert errors(parse(body, "<r>QUI=</r>"))


def test_length_counts_list_items():
    body = element("xs:NMTOKENS", '<xs:length value="2"/>')
    assert errors(parse(body, "<r>a b</r>")) == []
    assert errors(parse(body, "<r>a b c</r>"))


# --- bounds ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("facets", "ok", "bad"),
    [
        ('<xs:minInclusive value="1"/>', "1", "0"),
        ('<xs:minExclusive value="1"/>', "2", "1"),
        ('<xs:maxInclusive value="5"/>', "5", "6"),
        ('<xs:maxExclusive value="5"/>', "4", "5"),
    ],
)
def test_numeric_bounds(facets, ok, bad):
    body = element("xs:int", facets)
    assert errors(parse(body, f"<r>{ok}</r>")) == []
    assert errors(parse(body, f"<r>{bad}</r>"))


def test_decimal_bounds_use_value_space():
    body = element("xs:decimal", '<xs:maxInclusive value="1.0"/>')
    assert errors(parse(body, "<r>1.00</r>")) == []
    assert errors(parse(body, "<r>1.01</r>"))


def test_date_bounds_across_timezone_spellings():
    body = element("xs:date", '<xs:minInclusive value="2000-01-01Z"/>')
    assert errors(parse(body, "<r>2000-01-01+00:00</r>")) == []
    assert errors(parse(body, "<r>1999-12-31Z</r>"))


# --- enumeration -----------------------------------------------------------


def test_enumeration_string():
    body = element("xs:string", '<xs:enumeration value="a"/><xs:enumeration value="b"/>')
    assert errors(parse(body, "<r>b</r>")) == []
    assert errors(parse(body, "<r>c</r>"))


def test_enumeration_is_value_space_not_lexical():
    body = element("xs:decimal", '<xs:enumeration value="1.0"/>')
    assert errors(parse(body, "<r>1.00</r>")) == []
    assert errors(parse(body, "<r>1.01</r>"))


def test_enumeration_duration_by_month_equivalence():
    body = element("xs:duration", '<xs:enumeration value="P1Y"/>')
    assert errors(parse(body, "<r>P12M</r>")) == []
    assert errors(parse(body, "<r>P1M</r>"))


def test_enumeration_boolean():
    body = element("xs:boolean", '<xs:enumeration value="true"/>')
    assert errors(parse(body, "<r>1</r>")) == []
    assert errors(parse(body, "<r>false</r>"))


def test_enumeration_list():
    body = element("xs:NMTOKENS", '<xs:enumeration value="a b"/>')
    assert errors(parse(body, "<r>a b</r>")) == []
    assert errors(parse(body, "<r>a c</r>"))


# --- whiteSpace ------------------------------------------------------------


def test_whitespace_collapse_happens_before_length():
    body = element("xs:string", '<xs:whiteSpace value="collapse"/><xs:length value="3"/>')
    assert errors(parse(body, "<r> a  b </r>")) == []
    assert errors(parse(body, "<r>  a b  c </r>"))


def test_whitespace_replace():
    body = element("xs:string", '<xs:whiteSpace value="replace"/><xs:pattern value="a b"/>')
    assert errors(parse(body, "<r>a\tb</r>")) == []
    assert errors(parse(body, "<r>a  b</r>"))


# --- digits ----------------------------------------------------------------


def test_total_digits():
    body = element("xs:decimal", '<xs:totalDigits value="3"/>')
    assert errors(parse(body, "<r>12.3</r>")) == []
    assert errors(parse(body, "<r>1234</r>"))


def test_fraction_digits():
    body = element("xs:decimal", '<xs:fractionDigits value="1"/>')
    assert errors(parse(body, "<r>1.2</r>")) == []
    assert errors(parse(body, "<r>1.23</r>"))


# --- pattern ---------------------------------------------------------------


def test_pattern_is_full_match():
    body = element("xs:string", '<xs:pattern value="[0-9]{3}"/>')
    assert errors(parse(body, "<r>123</r>")) == []
    assert errors(parse(body, "<r>1234</r>"))
    assert errors(parse(body, "<r>x123</r>"))


def test_pattern_xsd_character_class_subtraction():
    body = element("xs:string", '<xs:pattern value="[a-z-[aeiou]]+"/>')
    assert errors(parse(body, "<r>rhythm</r>")) == []
    assert errors(parse(body, "<r>rain</r>"))


def test_pattern_xsd_digit_class():
    body = element("xs:string", r'<xs:pattern value="\d+"/>')
    assert errors(parse(body, "<r>123</r>")) == []
    assert errors(parse(body, "<r>12x</r>"))


def test_pattern_on_list_matches_whole_lexical():
    # A list type's pattern is matched against the whole list text, not
    # each item (confirmed against xmlschema).
    body = element("xs:NMTOKENS", '<xs:pattern value="[a-z]+( [a-z]+)*"/>')
    assert errors(parse(body, "<r>ab cd</r>")) == []
    assert errors(parse(body, "<r>ab 1</r>"))


def test_pattern_uses_lexical_not_python_rendering():
    # float spelling survives the Python float rendering (1.0E-2 would
    # become 0.01) and boolean spelling survives bool rendering.
    body = element("xs:float", '<xs:pattern value="...[Ee].."/>')
    assert errors(parse(body, "<r>1.0E-2</r>")) == []
    assert errors(parse(body, "<r>0.01</r>"))

    body = element("xs:boolean", '<xs:pattern value="1|0"/>')
    assert errors(parse(body, "<r>0</r>")) == []
    assert errors(parse(body, "<r>false</r>"))


def test_illegal_pattern_is_a_schema_error():
    report = parse(element("xs:string", '<xs:pattern value="(a)\\1"/>'), "<r>aa</r>")
    codes = [code for code, _ in errors(report)]
    assert "facet" in codes


def test_lazy_quantifier_is_a_schema_error():
    report = parse(element("xs:string", '<xs:pattern value="a+?"/>'), "<r>a</r>")
    assert "facet" in [code for code, _ in errors(report)]


# --- schema-time legality --------------------------------------------------


def test_inapplicable_facet_is_a_schema_error():
    report = parse(element("xs:int", '<xs:length value="3"/>'), "<r>123</r>")
    assert "facet" in [code for code, _ in errors(report)]


def test_digit_facet_on_string_is_a_schema_error():
    report = parse(element("xs:string", '<xs:totalDigits value="3"/>'), "<r>abc</r>")
    assert "facet" in [code for code, _ in errors(report)]


def test_invalid_enumeration_literal_is_a_schema_error():
    report = parse(element("xs:int", '<xs:enumeration value="abc"/>'), "<r>1</r>")
    assert "facet" in [code for code, _ in errors(report)]


# --- derivation and policy -------------------------------------------------


def test_repeated_restriction_merges_patterns():
    body = (
        '<xs:simpleType name="A"><xs:restriction base="xs:string">'
        '<xs:pattern value="[a-z]+"/></xs:restriction></xs:simpleType>'
        '<xs:element name="r"><xs:simpleType><xs:restriction base="A">'
        '<xs:pattern value="a.*"/></xs:restriction></xs:simpleType></xs:element>'
    )
    assert errors(parse(body, "<r>abc</r>")) == []
    assert errors(parse(body, "<r>abc1</r>"))
    assert errors(parse(body, "<r>bcd</r>"))


def test_repeated_restriction_intersects_enumerations():
    body = (
        '<xs:simpleType name="A"><xs:restriction base="xs:string">'
        '<xs:enumeration value="a"/><xs:enumeration value="b"/>'
        "</xs:restriction></xs:simpleType>"
        '<xs:element name="r"><xs:simpleType><xs:restriction base="A">'
        '<xs:enumeration value="b"/>'
        "</xs:restriction></xs:simpleType></xs:element>"
    )
    assert errors(parse(body, "<r>b</r>")) == []
    assert errors(parse(body, "<r>a</r>"))
    assert errors(parse(body, "<r>c</r>"))


def test_inherited_facets_still_apply():
    body = (
        '<xs:simpleType name="A"><xs:restriction base="xs:string">'
        '<xs:maxLength value="3"/></xs:restriction></xs:simpleType>'
        '<xs:element name="r"><xs:simpleType><xs:restriction base="A">'
        '<xs:minLength value="2"/></xs:restriction></xs:simpleType></xs:element>'
    )
    assert errors(parse(body, "<r>ab</r>")) == []
    assert errors(parse(body, "<r>a</r>"))
    assert errors(parse(body, "<r>abcd</r>"))


def test_facets_off_disables_enforcement():
    mode = BindingPolicy().replace(facets="off")
    body = element("xs:string", '<xs:length value="3"/>')
    assert errors(parse(body, "<r>abcd</r>", mode=mode)) == []


# --- attributes, fixed values, nil -----------------------------------------


def test_attribute_facets():
    body = (
        '<xs:element name="r"><xs:complexType><xs:attribute name="a">'
        '<xs:simpleType><xs:restriction base="xs:string"><xs:maxLength value="2"/>'
        "</xs:restriction></xs:simpleType></xs:attribute></xs:complexType></xs:element>"
    )
    assert errors(parse(body, '<r a="ab"/>')) == []
    assert errors(parse(body, '<r a="abc"/>'))


def test_fixed_value_must_satisfy_facets():
    body = (
        '<xs:element name="r" fixed="abcd"><xs:simpleType>'
        '<xs:restriction base="xs:string"><xs:maxLength value="3"/></xs:restriction>'
        "</xs:simpleType></xs:element>"
    )
    assert errors(parse(body, "<r/>"))


def test_nil_bypasses_facets():
    body = (
        '<xs:element name="r" nillable="true"><xs:simpleType>'
        '<xs:restriction base="xs:string"><xs:minLength value="2"/></xs:restriction>'
        "</xs:simpleType></xs:element>"
    )
    xml = '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>'
    assert errors(parse(body, xml)) == []
