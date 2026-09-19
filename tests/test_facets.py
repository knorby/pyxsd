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


# --- simpleContent complex types -------------------------------------------

EXT_INT = (
    '<xs:element name="r"><xs:complexType><xs:simpleContent>'
    '<xs:extension base="xs:int"/></xs:simpleContent></xs:complexType></xs:element>'
)


def instance(body, xml):
    parser = PyXSD(
        io.StringIO(xml),
        io.StringIO(SCHEMA.format(body=body)),
        xmlFileOutput=False,
        transformOutputName=None,
        mode=ParseModes.NAMESPACED,
    )
    return parser.schemaRootInstance


def test_simple_content_extension_value():
    assert errors(parse(EXT_INT, "<r>7</r>")) == []
    assert errors(parse(EXT_INT, "<r>abc</r>"))


def test_simple_content_extension_attrs():
    body = (
        '<xs:element name="r"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="xs:int"><xs:attribute name="a" type="xs:string" use="required"/>'
        "</xs:extension></xs:simpleContent></xs:complexType></xs:element>"
    )
    assert errors(parse(body, '<r a="x">7</r>')) == []
    assert errors(parse(body, "<r>7</r>"))


def test_simple_content_extension_user_simple_type_facets():
    body = (
        '<xs:simpleType name="Color"><xs:restriction base="xs:string">'
        '<xs:enumeration value="red"/><xs:enumeration value="green"/>'
        "</xs:restriction></xs:simpleType>"
        '<xs:element name="r"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="Color"/></xs:simpleContent></xs:complexType></xs:element>'
    )
    assert errors(parse(body, "<r>green</r>")) == []
    assert errors(parse(body, "<r>blue</r>"))


def test_simple_content_restriction_direct_facets():
    body = (
        '<xs:complexType name="base"><xs:simpleContent>'
        '<xs:extension base="xs:string"/></xs:simpleContent></xs:complexType>'
        '<xs:element name="r"><xs:complexType><xs:simpleContent>'
        '<xs:restriction base="base"><xs:maxLength value="3"/></xs:restriction>'
        "</xs:simpleContent></xs:complexType></xs:element>"
    )
    assert errors(parse(body, "<r>abc</r>")) == []
    assert errors(parse(body, "<r>abcd</r>"))


def test_simple_content_restriction_inline_type_facets():
    body = (
        '<xs:complexType name="base"><xs:simpleContent>'
        '<xs:extension base="xs:string"/></xs:simpleContent></xs:complexType>'
        '<xs:element name="r"><xs:complexType><xs:simpleContent>'
        '<xs:restriction base="base"><xs:simpleType><xs:restriction base="xs:string">'
        '<xs:enumeration value="red"/><xs:enumeration value="green"/>'
        "</xs:restriction></xs:simpleType></xs:restriction>"
        "</xs:simpleContent></xs:complexType></xs:element>"
    )
    assert errors(parse(body, "<r>green</r>")) == []
    assert errors(parse(body, "<r>blue</r>"))


def test_simple_content_root_default():
    body = (
        '<xs:element name="r" default="5"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="xs:int"/></xs:simpleContent></xs:complexType></xs:element>'
    )
    assert errors(parse(body, "<r/>")) == []
    assert instance(body, "<r/>")._value_ == ["5"]


def test_simple_content_root_fixed():
    body = (
        '<xs:element name="r" fixed="5"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="xs:int"/></xs:simpleContent></xs:complexType></xs:element>'
    )
    assert errors(parse(body, "<r>5</r>")) == []
    assert [code for code, _ in errors(parse(body, "<r>6</r>"))] == ["fixed-element"]


def test_simple_content_child_default_and_fixed():
    body = (
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="v" default="5"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="xs:int"/></xs:simpleContent></xs:complexType></xs:element>'
        "</xs:sequence></xs:complexType></xs:element>"
    )
    assert errors(parse(body, "<r><v/></r>")) == []
    assert instance(body, "<r><v/></r>")._children_[0]._value_ == ["5"]

    fixed = body.replace('default="5"', 'fixed="5"')
    assert errors(parse(fixed, "<r><v>5</v></r>")) == []
    assert [code for code, _ in errors(parse(fixed, "<r><v>6</v></r>"))] == ["fixed-element"]


def test_simple_content_nil_skips_value_validation():
    body = (
        '<xs:element name="r" nillable="true"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="xs:int"/></xs:simpleContent></xs:complexType></xs:element>'
    )
    xml = '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>'
    assert errors(parse(body, xml)) == []


# --- explicitTimezone (XSD 1.1 §4.3.16) ------------------------------------

TZ_REQUIRED = element("xs:dateTime", '<xs:explicitTimezone value="required"/>')


def codes(body, xml):
    return [code for code, _ in errors(parse(body, xml))]


def test_explicit_timezone_required_rejects_unzoned():
    assert codes(TZ_REQUIRED, "<r>2001-01-01T00:00:00</r>")
    assert errors(parse(TZ_REQUIRED, "<r>2001-01-01T00:00:00Z</r>")) == []
    assert errors(parse(TZ_REQUIRED, "<r>2001-01-01T00:00:00+05:00</r>")) == []


def test_explicit_timezone_prohibited_rejects_zoned():
    body = element("xs:dateTime", '<xs:explicitTimezone value="prohibited"/>')
    assert codes(body, "<r>2001-01-01T00:00:00Z</r>")
    assert codes(body, "<r>2001-01-01T00:00:00-05:00</r>")
    assert errors(parse(body, "<r>2001-01-01T00:00:00</r>")) == []


def test_explicit_timezone_optional_accepts_both():
    body = element("xs:dateTime", '<xs:explicitTimezone value="optional"/>')
    assert errors(parse(body, "<r>2001-01-01T00:00:00Z</r>")) == []
    assert errors(parse(body, "<r>2001-01-01T00:00:00</r>")) == []


def test_explicit_timezone_inapplicable_to_string():
    body = element("xs:string", '<xs:explicitTimezone value="required"/>')
    assert codes(body, "<r>abc</r>") == ["facet"]


def test_explicit_timezone_illegal_value():
    body = element("xs:dateTime", '<xs:explicitTimezone value="something"/>')
    assert codes(body, "<r>2001-01-01T00:00:00Z</r>") == ["facet"]


def test_explicit_timezone_duplicate_facet():
    body = element(
        "xs:dateTime",
        '<xs:explicitTimezone value="optional"/><xs:explicitTimezone value="prohibited"/>',
    )
    assert "facet" in codes(body, "<r>2001-01-01T00:00:00</r>")


def test_explicit_timezone_fixed_may_not_change():
    body = (
        '<xs:simpleType name="Base"><xs:restriction base="xs:dateTime">'
        '<xs:explicitTimezone value="optional" fixed="true"/></xs:restriction></xs:simpleType>'
        '<xs:simpleType name="Derived"><xs:restriction base="Base">'
        '<xs:explicitTimezone value="prohibited"/></xs:restriction></xs:simpleType>'
        '<xs:element name="r" type="Derived"/>'
    )
    assert "facet" in codes(body, "<r>2001-01-01T00:00:00Z</r>")


def test_explicit_timezone_fixed_restatement_allowed():
    body = (
        '<xs:simpleType name="Base"><xs:restriction base="xs:dateTime">'
        '<xs:explicitTimezone value="optional" fixed="true"/></xs:restriction></xs:simpleType>'
        '<xs:simpleType name="Derived"><xs:restriction base="Base">'
        '<xs:explicitTimezone value="optional"/></xs:restriction></xs:simpleType>'
        '<xs:element name="r" type="Derived"/>'
    )
    assert errors(parse(body, "<r>2001-01-01T00:00:00Z</r>")) == []


def test_explicit_timezone_inherited_from_base():
    body = (
        '<xs:simpleType name="Base"><xs:restriction base="xs:dateTime">'
        '<xs:explicitTimezone value="prohibited"/></xs:restriction></xs:simpleType>'
        '<xs:simpleType name="Derived"><xs:restriction base="Base"/></xs:simpleType>'
        '<xs:element name="r" type="Derived"/>'
    )
    assert codes(body, "<r>2001-01-01T00:00:00Z</r>")
    assert errors(parse(body, "<r>2001-01-01T00:00:00</r>")) == []


# --- bound restatement vs widening (d3_4_28v09 / d3_4_28si10) --------------


def test_bound_restating_base_exclusive_is_allowed():
    body = (
        '<xs:simpleType name="Base"><xs:restriction base="xs:int">'
        '<xs:maxExclusive value="5"/></xs:restriction></xs:simpleType>'
        '<xs:simpleType name="Derived"><xs:restriction base="Base">'
        '<xs:maxExclusive value="5"/></xs:restriction></xs:simpleType>'
        '<xs:element name="r" type="Derived"/>'
    )
    assert errors(parse(body, "<r>4</r>")) == []
    assert codes(body, "<r>5</r>")


def test_inclusive_bound_at_base_exclusive_is_rejected():
    upper = (
        '<xs:simpleType name="Base"><xs:restriction base="xs:int">'
        '<xs:maxExclusive value="5"/></xs:restriction></xs:simpleType>'
        '<xs:simpleType name="Derived"><xs:restriction base="Base">'
        '<xs:maxInclusive value="5"/></xs:restriction></xs:simpleType>'
        '<xs:element name="r" type="Derived"/>'
    )
    assert "facet" in codes(upper, "<r>4</r>")
    lower = (
        '<xs:simpleType name="Base"><xs:restriction base="xs:int">'
        '<xs:minExclusive value="5"/></xs:restriction></xs:simpleType>'
        '<xs:simpleType name="Derived"><xs:restriction base="Base">'
        '<xs:minInclusive value="5"/></xs:restriction></xs:simpleType>'
        '<xs:element name="r" type="Derived"/>'
    )
    assert "facet" in codes(lower, "<r>6</r>")


def test_derived_bound_outside_base_value_space_is_rejected():
    body = (
        '<xs:simpleType name="Base"><xs:restriction base="xs:int">'
        '<xs:maxInclusive value="10"/></xs:restriction></xs:simpleType>'
        '<xs:simpleType name="Derived"><xs:restriction base="Base">'
        '<xs:maxInclusive value="11"/></xs:restriction></xs:simpleType>'
        '<xs:element name="r" type="Derived"/>'
    )
    assert "facet" in codes(body, "<r>5</r>")


# --- datatype tails: unions and recurring date ordering --------------------


def test_union_inline_members_resolve_and_enforce():
    body = (
        '<xs:element name="r"><xs:simpleType><xs:union>'
        '<xs:simpleType><xs:restriction base="xs:dateTime">'
        '<xs:explicitTimezone value="prohibited"/></xs:restriction></xs:simpleType>'
        '<xs:simpleType><xs:restriction base="xs:integer"/></xs:simpleType>'
        "</xs:union></xs:simpleType></xs:element>"
    )
    assert errors(parse(body, "<r>2001-01-01T00:00:00</r>")) == []
    assert errors(parse(body, "<r>5</r>")) == []
    assert codes(body, "<r>2001-01-01T00:00:00Z</r>")


def test_union_with_no_members_is_an_empty_value_space():
    # XSD 1.1 explicitly allows a union with no member types (bug 4912);
    # its value space is empty, so every instance value is rejected.
    body = (
        '<xs:simpleType name="U"><xs:union memberTypes=""/></xs:simpleType>'
        '<xs:element name="r" type="U"/>'
    )
    assert errors(parse(body, "<r>x</r>"))
    assert not [code for code in codes(body, "<r>x</r>") if code == "atomic-required"]


def test_gDay_bounds_across_extreme_timezones():
    body = element("xs:gDay", '<xs:minInclusive value="---16+13:00"/>')
    assert errors(parse(body, "<r>---15-13:00</r>")) == []
    assert codes(body, "<r>---15+13:00</r>")


def test_gMonthDay_bounds_across_extreme_timezones():
    body = element("xs:gMonthDay", '<xs:minInclusive value="--12-12+13:00"/>')
    assert errors(parse(body, "<r>--12-12+11:00</r>")) == []
    assert codes(body, "<r>--12-12+14:00</r>")


def test_gMonthDay_rejects_impossible_day():
    body = element("xs:gMonthDay", "")
    assert codes(body, "<r>--02-30</r>")
    assert codes(body, "<r>--11-31</r>")
    assert errors(parse(body, "<r>--02-29</r>")) == []


LIST_CONTAINER = (
    '<xs:element name="r"><xs:complexType><xs:sequence>'
    '<xs:element name="e" type="{name}"/></xs:sequence></xs:complexType></xs:element>'
)

_INLINE_STATE_LIST = (
    '<xs:simpleType name="L1"><xs:list><xs:simpleType>'
    '<xs:restriction base="xs:string"><xs:enumeration value="WA"/>'
    '<xs:enumeration value="OR"/></xs:restriction></xs:simpleType></xs:list></xs:simpleType>'
)
_INLINE_ZIP_LIST = (
    '<xs:simpleType name="L2"><xs:list><xs:simpleType>'
    '<xs:restriction base="xs:positiveInteger"><xs:pattern value="[1-9]{5}"/>'
    "</xs:restriction></xs:simpleType></xs:list></xs:simpleType>"
)


def test_list_inline_item_enumeration_rejects_bad_item():
    """msData stH004/stH008: an inline item type's facets are enforced."""
    body = LIST_CONTAINER.format(name="L1") + _INLINE_STATE_LIST
    assert errors(parse(body, "<r><e>NY</e></r>"))
    assert errors(parse(body, "<r><e>WA OR CA</e></r>"))


def test_list_inline_item_max_length_rejects_long_item():
    """ST_baseTD00301m: a list item over the inline maxLength is invalid."""
    body = (
        LIST_CONTAINER.format(name="L") + '<xs:simpleType name="L"><xs:list><xs:simpleType>'
        '<xs:restriction base="xs:string"><xs:maxLength value="6"/>'
        '<xs:pattern value="a+"/></xs:restriction></xs:simpleType></xs:list></xs:simpleType>'
    )
    assert errors(parse(body, "<r><e>a aa aaa aaaaaaa</e></r>"))
    assert errors(parse(body, "<r><e>a aa aaa aaaa</e></r>")) == []


def test_union_of_inline_item_lists_rejects_bad_value():
    """msData stH004: a value matching no union member list is invalid."""
    body = (
        LIST_CONTAINER.format(name="U")
        + _INLINE_STATE_LIST
        + _INLINE_ZIP_LIST
        + '<xs:simpleType name="U"><xs:union memberTypes="L1 L2"/></xs:simpleType>'
    )
    assert errors(parse(body, "<r><e>NY</e></r>"))
    assert errors(parse(body, "<r><e>12345</e></r>")) == []
