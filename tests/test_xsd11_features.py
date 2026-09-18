"""XSD 1.1 feature substrate: the assertion and CTA XPath 2.0 subsets.

Task 2 covers only the shared parse/whitelist/evaluate substrate that the
``xs:assert``/``xs:assertion`` (Tasks 3-4) and conditional type assignment
(Tasks 5-6) phases consume. The admitted and rejected shapes below are
condensed from the Saxon ``Assert``/``CTA`` corpus and the IBM
``typeAlternatives`` suite, under XSD 1.1 §3.13.1 (assertions) and §3.3.2.1
(conditional type assignment).
"""

import io
import xml.etree.ElementTree as ET

import pytest

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD
from pyxsd.xpath_assertions import (
    CompiledXPath,
    _AssertionChecker,
    evaluate,
    parse_assertion_xpath,
    parse_cta_xpath,
)
from pyxsd.xpath_subset import XPathError

XS = "http://www.w3.org/2001/XMLSchema"
NS = {"xs": XS}


# --- assertion subset -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "@attr = 'x'",
        "child",
        "count(.//b) le 4",
        "@a and not(@b)",
        "exists(@y) ne exists(a/b)",
        "not(.//disallowed)",
        "not(a[preceding::a[not(b)]])",
        "data(.) instance of xs:untypedAtomic",
        "$value lt current-date()",
        "chess:result = ('black wins', 'draw')",
    ],
)
def test_xpath2_assertion_admits_subset_shapes(text: str) -> None:
    compiled = parse_assertion_xpath(text, {"xs": XS, "chess": "http://chess.example/"})
    assert isinstance(compiled, CompiledXPath)
    assert compiled.text == text


@pytest.mark.parametrize(
    "text",
    [
        "doc('assert.xml')",
        "doc-available('assert.xml')",
        "collection('/tmp/data')",
        "resolve-uri('http://example.com/a')",
        "namespace::*",
    ],
)
def test_xpath2_assertion_rejects_out_of_subset(text: str) -> None:
    with pytest.raises(XPathError):
        parse_assertion_xpath(text, NS)


def test_xpath2_assertion_rejects_unbound_prefix() -> None:
    with pytest.raises(XPathError):
        parse_assertion_xpath("p:foo", NS)


def test_xpath2_assertion_rejects_empty_expression() -> None:
    with pytest.raises(XPathError):
        parse_assertion_xpath("   ", NS)


def test_xpath2_assertion_rejects_unknown_ast_node_type() -> None:
    class _MysteryToken:
        symbol = "mystery"

    with pytest.raises(XPathError):
        _AssertionChecker().check(_MysteryToken())


# --- conditional type assignment subset -------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "@a = '1'",
        "@a and not(@b)",
        "@c2:min=1",
        "@a eq '1'",
        "(@a = '1')",
        "@a = '1' or @b = '2' and not(@c = '3')",
    ],
)
def test_xpath2_cta_admits_subset_shapes(text: str) -> None:
    compiled = parse_cta_xpath(text, {"xs": XS, "c2": "http://cta.example/"})
    assert isinstance(compiled, CompiledXPath)


@pytest.mark.parametrize(
    "text",
    [
        "child",
        "a/b",
        "..",
        ".",
        "text()",
        "count(@a)",
        "resolve-QName(@kind, .)",
        "@a/child::b",
        "@a[@b]",
    ],
)
def test_xpath2_cta_rejects_out_of_subset(text: str) -> None:
    with pytest.raises(XPathError):
        parse_cta_xpath(text, {"xs": XS})


def test_xpath2_cta_rejects_unbound_prefix() -> None:
    with pytest.raises(XPathError):
        parse_cta_xpath("p:foo = '1'", NS)


# --- evaluation adapter -----------------------------------------------------


def test_xpath2_evaluate_attribute_comparison_true() -> None:
    compiled = parse_assertion_xpath("@x = 'a'", NS)
    assert evaluate(compiled, ET.fromstring("<temp x='a'/>")) is True


def test_xpath2_evaluate_attribute_comparison_false() -> None:
    compiled = parse_assertion_xpath("@x = 'a'", NS)
    assert evaluate(compiled, ET.fromstring("<temp x='b'/>")) is False


def test_xpath2_evaluate_value_variable() -> None:
    compiled = parse_assertion_xpath("$value gt 3", NS)
    root = ET.fromstring("<temp/>")
    assert evaluate(compiled, root, value=5) is True
    assert evaluate(compiled, root, value=1) is False


def test_xpath2_evaluate_maps_missing_variable_to_xpath_error() -> None:
    compiled = parse_assertion_xpath("$value gt 3", NS)
    with pytest.raises(XPathError):
        evaluate(compiled, ET.fromstring("<temp/>"))


# --- xs:assert on complex types ---------------------------------------------

SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"{extra}>
{body}
</xs:schema>"""


def parse(body, xml, extra=""):
    parser = PyXSD(
        io.StringIO(xml),
        io.StringIO(SCHEMA.format(body=body, extra=extra)),
        xmlFileOutput=False,
        transformOutputName=None,
        mode=ParseModes.NAMESPACED,
    )
    return parser.report


def errors(report):
    return [issue.code for issue in report.errors]


ASSERT_TEMP = (
    '<xs:element name="temp"><xs:complexType><xs:sequence/>'
    '<xs:attribute name="x" use="required"/>'
    '<xs:assert test="@x &gt; 300"/></xs:complexType></xs:element>'
)


def test_assert_satisfied_passes() -> None:
    assert errors(parse(ASSERT_TEMP, '<temp x="304"/>')) == []


def test_assert_violated_reports_assert_failed() -> None:
    assert errors(parse(ASSERT_TEMP, '<temp x="204"/>')) == ["assert-failed"]


def test_assert_out_of_subset_reports_assert_invalid() -> None:
    body = (
        '<xs:element name="temp"><xs:complexType><xs:sequence/>'
        "<xs:assert test=\"doc('other.xml')\"/></xs:complexType></xs:element>"
    )
    assert errors(parse(body, "<temp/>")) == ["assert-invalid"]


def test_assert_dynamic_error_reports_assert_failed() -> None:
    body = (
        '<xs:element name="temp"><xs:complexType><xs:sequence/>'
        '<xs:attribute name="x" use="required"/>'
        '<xs:assert test="100 div xs:integer(@x) &gt; 50"/>'
        "</xs:complexType></xs:element>"
    )
    assert errors(parse(body, '<temp x="0"/>')) == ["assert-failed"]


def test_assert_value_comparison_uses_declared_attribute_type() -> None:
    body = (
        '<xs:element name="XList" type="ArrayType"/>'
        '<xs:complexType name="ArrayType"><xs:sequence>'
        '<xs:element name="entry" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>'
        "</xs:sequence>"
        '<xs:attribute name="length" type="xs:nonNegativeInteger"/>'
        '<xs:assert test="@length eq count(./entry)"/>'
        "</xs:complexType>"
    )
    ok = '<XList length="2"><entry>a</entry><entry>b</entry></XList>'
    bad = '<XList length="4"><entry>a</entry><entry>b</entry></XList>'
    assert errors(parse(body, ok)) == []
    assert errors(parse(body, bad)) == ["assert-failed"]


def test_assert_on_base_type_applies_to_derived_type() -> None:
    body = (
        '<xs:element name="message" type="derivedType"/>'
        '<xs:complexType name="baseType"><xs:sequence/>'
        '<xs:attribute name="mustUnderstand" type="xs:string"/>'
        '<xs:assert test="@mustUnderstand"/>'
        "</xs:complexType>"
        '<xs:complexType name="derivedType"><xs:complexContent>'
        '<xs:restriction base="baseType"><xs:sequence/>'
        '<xs:attribute name="mustUnderstand" type="xs:string"/>'
        "</xs:restriction></xs:complexContent></xs:complexType>"
    )
    assert errors(parse(body, '<message mustUnderstand="YES"/>')) == []
    assert errors(parse(body, "<message/>")) == ["assert-failed"]


def test_assert_xpath_default_namespace_target_namespace() -> None:
    body = (
        '<xs:element name="temp"><xs:complexType><xs:sequence>'
        '<xs:element name="child" minOccurs="0"/>'
        "</xs:sequence>"
        '<xs:assert test="empty(child)" '
        'xpathDefaultNamespace="##targetNamespace"/>'
        "</xs:complexType></xs:element>"
    )
    extra = ' targetNamespace="urn:t" xmlns:t="urn:t" elementFormDefault="qualified"'
    assert errors(parse(body, '<temp xmlns="urn:t"/>', extra)) == []
    assert errors(parse(body, '<temp xmlns="urn:t"><child/></temp>', extra)) == ["assert-failed"]


def test_assert_value_variable_is_typed_simple_content() -> None:
    body = (
        '<xs:element name="temp"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="xs:date">'
        '<xs:attribute name="startDate" type="xs:date" use="required"/>'
        '<xs:assert test="$value instance of xs:date"/>'
        "</xs:extension></xs:simpleContent></xs:complexType></xs:element>"
    )
    assert errors(parse(body, '<temp startDate="2008-06-01">2008-07-01</temp>')) == []


def test_assert_descendant_element_uses_declared_type() -> None:
    body = (
        '<xs:element name="temp"><xs:complexType><xs:sequence>'
        '<xs:element name="d" type="xs:date"/>'
        "</xs:sequence>"
        '<xs:assert test="data(child::d[1]) instance of xs:date"/>'
        "</xs:complexType></xs:element>"
    )
    assert errors(parse(body, "<temp><d>2008-07-01</d></temp>")) == []


def test_assert_in_scope_prefixes_sees_instance_bindings() -> None:
    body = (
        '<xs:element name="x"><xs:complexType><xs:sequence/>'
        "<xs:assert test=\"in-scope-prefixes(.) = 'a'\"/>"
        "</xs:complexType></xs:element>"
    )
    assert errors(parse(body, '<x xmlns:a="urn:a"/>')) == []


# --- xs:assertion on simple types -------------------------------------------


def simple_element(base: str, facets: str, *, extra_element: str = "") -> str:
    """An element whose anonymous simple type restricts *base* with *facets*."""
    return (
        f'<xs:element name="temp">{extra_element}<xs:simpleType>'
        f'<xs:restriction base="{base}">{facets}</xs:restriction>'
        "</xs:simpleType></xs:element>"
    )


def test_assertion_simple_type_satisfied() -> None:
    body = simple_element("xs:integer", '<xs:assertion test="$value gt 0"/>')
    assert errors(parse(body, "<temp>5</temp>")) == []


def test_assertion_simple_type_violated_is_assert_failed() -> None:
    body = simple_element("xs:integer", '<xs:assertion test="$value gt 0"/>')
    assert errors(parse(body, "<temp>-1</temp>")) == ["assert-failed"]


def test_assertion_out_of_subset_is_assert_invalid() -> None:
    body = simple_element("xs:string", "<xs:assertion test=\"doc('other.xml')\"/>")
    assert errors(parse(body, "<temp>x</temp>")) == ["assert-invalid"]


def test_assertion_unbound_prefix_is_assert_invalid() -> None:
    body = simple_element("xs:string", '<xs:assertion test="p:foo = 1"/>')
    assert errors(parse(body, "<temp>x</temp>")) == ["assert-invalid"]


def test_assertion_empty_test_is_assert_invalid() -> None:
    body = simple_element("xs:string", '<xs:assertion test=""/>')
    assert errors(parse(body, "<temp>x</temp>")) == ["assert-invalid"]


def test_assertion_typed_numeric_value() -> None:
    body = simple_element("xs:int", '<xs:assertion test="$value mod 2 = 0"/>')
    assert errors(parse(body, "<temp>4</temp>")) == []
    assert errors(parse(body, "<temp>5</temp>")) == ["assert-failed"]


def test_assertion_typed_date_value() -> None:
    body = simple_element(
        "xs:date",
        "<xs:assertion test=\"$value lt xs:date('2010-01-01')\"/>",
    )
    assert errors(parse(body, "<temp>2009-06-01</temp>")) == []
    assert errors(parse(body, "<temp>2011-06-01</temp>")) == ["assert-failed"]


def test_assertion_typed_value_is_a_date_instance() -> None:
    body = simple_element("xs:date", '<xs:assertion test="$value instance of xs:date"/>')
    assert errors(parse(body, "<temp>2009-06-01</temp>")) == []


def test_assertion_list_value_is_a_sequence() -> None:
    body = (
        '<xs:simpleType name="ints"><xs:list itemType="xs:integer"/></xs:simpleType>'
        + simple_element(
            "ints",
            '<xs:assertion test="count($value) eq count(distinct-values($value))"/>',
        )
    )
    assert errors(parse(body, "<temp>1 3 5</temp>")) == []
    assert errors(parse(body, "<temp>1 3 3</temp>")) == ["assert-failed"]


def test_assertion_union_value_uses_member_type() -> None:
    body = (
        '<xs:simpleType name="u"><xs:union memberTypes="xs:date xs:dateTime"/></xs:simpleType>'
        + simple_element(
            "u",
            "<xs:assertion test=\"starts-with(string($value), '2008')\"/>",
        )
    )
    assert errors(parse(body, "<temp>2008-06-01</temp>")) == []
    assert errors(parse(body, "<temp>2009-06-01</temp>")) == ["assert-failed"]


def test_assertion_context_item_undefined_is_assert_failed() -> None:
    body = simple_element("xs:date", '<xs:assertion test=". castable as xs:date"/>')
    assert errors(parse(body, "<temp>2009-06-01</temp>")) == ["assert-failed"]


def test_assertion_position_undefined_is_assert_failed() -> None:
    body = simple_element("xs:date", '<xs:assertion test="position() le 50"/>')
    assert errors(parse(body, "<temp>2009-06-01</temp>")) == ["assert-failed"]


def test_assertion_last_undefined_is_assert_failed() -> None:
    body = simple_element("xs:date", '<xs:assertion test="last() le 50"/>')
    assert errors(parse(body, "<temp>2009-06-01</temp>")) == ["assert-failed"]


def test_assertion_root_path_undefined_is_assert_failed() -> None:
    body = simple_element("xs:string", "<xs:assertion test=\"/root = 'present'\"/>")
    assert errors(parse(body, "<temp>present</temp>")) == ["assert-failed"]


def test_assertion_dynamic_error_is_assert_failed() -> None:
    body = simple_element(
        "xs:date",
        "<xs:assertion test=\"xs:date(concat(string($value), '!!!')) gt xs:date('1900-01-01')\"/>",
    )
    assert errors(parse(body, "<temp>2009-06-01</temp>")) == ["assert-failed"]


def test_assertion_accumulates_across_restriction_steps() -> None:
    body = (
        '<xs:simpleType name="base">'
        '<xs:restriction base="xs:string">'
        "<xs:assertion test=\"ends-with($value, 'xyz')\"/>"
        "</xs:restriction></xs:simpleType>"
        '<xs:simpleType name="derived"><xs:restriction base="base">'
        '<xs:assertion test="string-length($value) gt 3"/>'
        "</xs:restriction></xs:simpleType>"
        '<xs:element name="message" type="derived"/>'
    )
    assert errors(parse(body, "<message>abcdxyz</message>")) == []
    assert errors(parse(body, "<message>abcd</message>")) == ["assert-failed"]
    assert errors(parse(body, "<message>xyz</message>")) == ["assert-failed"]


def test_assertion_runs_alongside_other_facets() -> None:
    body = simple_element(
        "xs:string",
        '<xs:maxLength value="3"/><xs:assertion test="$value = \'abcd\'"/>',
    )
    assert errors(parse(body, "<temp>abcd</temp>")) == ["value"]
    assert errors(parse(body, "<temp>ab</temp>")) == ["assert-failed"]


def test_assertion_xpath_default_namespace_resolves_type_names() -> None:
    body = simple_element(
        "xs:string",
        '<xs:assertion test="$value castable as double" '
        'xpathDefaultNamespace="http://www.w3.org/2001/XMLSchema"/>',
    )
    assert errors(parse(body, "<temp>23.5</temp>")) == []


def test_assertion_on_simple_content_restriction() -> None:
    body = (
        '<xs:element name="root"><xs:complexType><xs:simpleContent>'
        '<xs:restriction base="xs:string">'
        "<xs:assertion test=\"$value = 'ok'\"/>"
        "</xs:restriction></xs:simpleContent></xs:complexType></xs:element>"
    )
    assert errors(parse(body, "<root>ok</root>")) == []
    assert errors(parse(body, "<root>bad</root>")) == ["assert-failed"]


def test_assertion_on_attribute_value_reports_assert_failed() -> None:
    body = (
        '<xs:element name="root"><xs:complexType><xs:attribute name="n">'
        '<xs:simpleType><xs:restriction base="xs:int">'
        '<xs:assertion test="$value mod 2 = 0"/>'
        "</xs:restriction></xs:simpleType></xs:attribute>"
        "</xs:complexType></xs:element>"
    )
    assert errors(parse(body, '<root n="4"/>')) == []
    assert errors(parse(body, '<root n="3"/>')) == ["assert-failed"]


def test_assertion_failing_union_member_falls_through_to_next() -> None:
    body = (
        '<xs:simpleType name="EvenInt"><xs:restriction base="xs:int">'
        '<xs:assertion test="$value mod 2 = 0"/></xs:restriction></xs:simpleType>'
        '<xs:simpleType name="OldDate"><xs:restriction base="xs:date">'
        "<xs:assertion test=\"$value lt xs:date('2010-01-01')\"/>"
        "</xs:restriction></xs:simpleType>"
        '<xs:element name="Example"><xs:simpleType>'
        '<xs:union memberTypes="EvenInt OldDate xs:date"/>'
        "</xs:simpleType></xs:element>"
    )
    # The date fails OldDate's assertion, so the plain xs:date member wins.
    assert errors(parse(body, "<Example>2010-10-10</Example>")) == []


# --- xs:alternative schema phase --------------------------------------------

_ALT_BASE = '<xs:complexType name="Base"><xs:sequence/></xs:complexType>'
_ALT_DERIVED = (
    '<xs:complexType name="Derived"><xs:complexContent>'
    '<xs:extension base="Base"><xs:sequence/></xs:extension>'
    "</xs:complexContent></xs:complexType>"
)
_ALT_TEMP = '<xs:element name="temp" type="Base">{alternatives}</xs:element>'


def _alt(*alternatives: str) -> str:
    return _ALT_BASE + _ALT_DERIVED + _ALT_TEMP.format(alternatives="".join(alternatives))


def test_alternative_derived_type_is_accepted() -> None:
    body = _alt('<xs:alternative test="@kind =\'d\'" type="Derived"/>')
    assert errors(parse(body, "<temp/>")) == []


def test_alternative_non_derived_type_reports_alternative_invalid() -> None:
    body = _alt('<xs:alternative test="@kind" type="xs:string"/>')
    assert errors(parse(body, "<temp/>")) == ["alternative-invalid"]


def test_alternative_unresolved_type_reports_alternative_invalid() -> None:
    body = _alt('<xs:alternative test="@kind" type="Missing"/>')
    assert errors(parse(body, "<temp/>")) == ["alternative-invalid"]


def test_alternative_out_of_subset_test_reports_alternative_invalid() -> None:
    body = _alt('<xs:alternative test="child::x" type="Derived"/>')
    assert errors(parse(body, "<temp/>")) == ["alternative-invalid"]


def test_alternative_missing_type_reports_alternative_invalid() -> None:
    body = _alt('<xs:alternative test="@kind"/>')
    assert errors(parse(body, "<temp/>")) == ["alternative-invalid"]


def test_alternative_type_and_inline_type_reports_alternative_invalid() -> None:
    body = _alt(
        '<xs:alternative test="@kind" type="Derived">'
        "<xs:complexType><xs:sequence/></xs:complexType></xs:alternative>"
    )
    assert errors(parse(body, "<temp/>")) == ["alternative-invalid"]


def test_alternative_final_default_without_test_is_accepted() -> None:
    body = _alt(
        '<xs:alternative test="@kind =\'d\'" type="Derived"/>',
        '<xs:alternative type="Base"/>',
    )
    assert errors(parse(body, "<temp/>")) == []


def test_alternative_non_final_without_test_reports_alternative_invalid() -> None:
    body = _alt(
        '<xs:alternative type="Derived"/>',
        '<xs:alternative test="@kind" type="Base"/>',
    )
    assert errors(parse(body, "<temp/>")) == ["alternative-invalid"]


def test_alternative_xs_error_default_is_accepted() -> None:
    body = _alt(
        '<xs:alternative test="@kind" type="Derived"/>',
        '<xs:alternative type="xs:error"/>',
    )
    report = parse(body, "<temp/>")
    # The schema is legal: xs:error is always an admissible alternative.
    # At instance phase the selected xs:error makes <temp/> invalid.
    assert [i.code for i in report.for_phase("schema") if i.severity.name == "ERROR"] == []
    assert errors(report) == ["alternative-error"]


def test_alternative_constructor_function_test_is_accepted() -> None:
    body = _alt('<xs:alternative test="xs:int(@n) &gt; 0" type="Derived"/>')
    assert errors(parse(body, "<temp/>")) == []


def test_alternative_cast_test_is_accepted() -> None:
    body = _alt('<xs:alternative test="@kind cast as xs:int = 1" type="Derived"/>')
    assert errors(parse(body, "<temp/>")) == []


def test_alternative_inline_derived_type_is_accepted() -> None:
    body = _alt(
        '<xs:alternative test="@kind">'
        "<xs:complexType><xs:complexContent>"
        '<xs:extension base="Base"><xs:sequence/></xs:extension>'
        "</xs:complexContent></xs:complexType></xs:alternative>"
    )
    assert errors(parse(body, "<temp/>")) == []


def test_alternative_later_type_derived_from_earlier_is_allowed() -> None:
    # IBM S3_12/s3_12v08: a broad first alternative followed by narrower
    # ones is valid.  XSD 1.1 §3.3.2.1/§3.12 has no "required derivation
    # ordering" rule, so pyxsd deliberately does not reject this.
    body = _alt(
        '<xs:alternative test="@a and @b" type="Base"/>',
        '<xs:alternative test="@a" type="Derived"/>',
    )
    assert errors(parse(body, "<temp/>")) == []


# --- xs:alternative instance phase (conditional type assignment) ------------

#: ``Base`` requires ``shared``; ``TypeA`` adds ``a``; ``TypeB`` adds ``b``.
#: Selection is observable because the extra element an instance may carry
#: is exactly the one the selected type declares.
_CTA_TYPES = (
    '<xs:complexType name="Base"><xs:sequence>'
    '<xs:element name="shared" type="xs:string"/>'
    '</xs:sequence><xs:attribute name="kind" type="xs:string"/></xs:complexType>'
    '<xs:complexType name="TypeA"><xs:complexContent>'
    '<xs:extension base="Base"><xs:sequence>'
    '<xs:element name="a" type="xs:string"/>'
    "</xs:sequence></xs:extension></xs:complexContent></xs:complexType>"
    '<xs:complexType name="TypeB"><xs:complexContent>'
    '<xs:extension base="Base"><xs:sequence>'
    '<xs:element name="b" type="xs:string"/>'
    "</xs:sequence></xs:extension></xs:complexContent></xs:complexType>"
)
_CTA_TEMP = '<xs:element name="temp" type="Base">{alternatives}</xs:element>'


def _cta(*alternatives: str) -> str:
    return _CTA_TYPES + _CTA_TEMP.format(alternatives="".join(alternatives))


_CTA_TWO_WAY = _cta(
    '<xs:alternative test="@kind = \'a\'" type="TypeA"/>',
    '<xs:alternative test="@kind = \'b\'" type="TypeB"/>',
)


def test_alternative_instance_selects_first_matching_type() -> None:
    assert errors(parse(_CTA_TWO_WAY, '<temp kind="a"><shared/><a>x</a></temp>')) == []
    assert errors(parse(_CTA_TWO_WAY, '<temp kind="b"><shared/><b>x</b></temp>')) == []


def test_alternative_instance_selects_only_the_matching_type() -> None:
    # ``b`` is not admissible under TypeA, and ``a`` is not under TypeB:
    # the wrong alternative governing would make these valid.
    assert errors(parse(_CTA_TWO_WAY, '<temp kind="a"><shared/><b>x</b></temp>')) != []
    assert errors(parse(_CTA_TWO_WAY, '<temp kind="b"><shared/><a>x</a></temp>')) != []


def test_alternative_instance_no_match_uses_declared_type() -> None:
    # No test is true and there is no test-free default: the element's
    # declared type (Base, accepting just ``shared``) governs.
    assert errors(parse(_CTA_TWO_WAY, '<temp kind="c"><shared/></temp>')) == []


def test_alternative_instance_xsi_type_overrides_selection() -> None:
    # CTA would select TypeA (kind="a"), which refuses ``b``; the explicit
    # xsi:type TypeB governs instead (XSD 1.1: xsi:type takes precedence).
    xml = (
        '<temp xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'kind="a" xsi:type="TypeB"><shared/><b>x</b></temp>'
    )
    assert errors(parse(_CTA_TWO_WAY, xml)) == []


def test_alternative_instance_first_true_test_wins() -> None:
    # ``@kind`` (existence) and ``@kind = 'a'`` are both true; the first
    # alternative's TypeA governs, so ``a`` is admissible and ``b`` is not.
    body = _cta(
        '<xs:alternative test="@kind" type="TypeA"/>',
        '<xs:alternative test="@kind = \'a\'" type="TypeB"/>',
    )
    assert errors(parse(body, '<temp kind="a"><shared/><a>x</a></temp>')) == []
    assert errors(parse(body, '<temp kind="a"><shared/><b>x</b></temp>')) != []


def test_alternative_instance_test_free_default_governs() -> None:
    body = _cta(
        '<xs:alternative test="@kind = \'a\'" type="TypeA"/>',
        '<xs:alternative type="TypeB"/>',
    )
    assert errors(parse(body, '<temp kind="z"><shared/><b>x</b></temp>')) == []
    assert errors(parse(body, '<temp kind="z"><shared/><a>x</a></temp>')) != []


def test_alternative_instance_dynamic_error_is_false() -> None:
    # A test whose cast raises a dynamic error is treated as false, so the
    # next alternative is tried (XSD 1.1 §3.12.6; Saxon CTA cta0016).
    body = _cta(
        '<xs:alternative test="@kind cast as xs:int = 1" type="TypeA"/>',
        '<xs:alternative type="TypeB"/>',
    )
    assert errors(parse(body, '<temp kind="abc"><shared/><b>x</b></temp>')) == []


def test_alternative_instance_xs_error_default_is_invalid() -> None:
    body = _cta(
        '<xs:alternative test="@kind = \'a\'" type="TypeA"/>',
        '<xs:alternative type="xs:error"/>',
    )
    assert errors(parse(body, '<temp kind="z"><shared/></temp>')) == ["alternative-error"]


def test_alternative_instance_invalid_simple_content_reports_value() -> None:
    # A selected simple-content complex type (a restriction of a mixed
    # complex type) whose lexical value is invalid must be reported, not
    # crash on a missing ``_unvalidated`` shell (Saxon CTA cta0001).
    body = (
        '<xs:complexType name="AnyContent" mixed="true"><xs:sequence>'
        '<xs:any processContents="skip" minOccurs="0" maxOccurs="unbounded"/>'
        '</xs:sequence><xs:attribute name="kind" type="xs:string"/></xs:complexType>'
        '<xs:complexType name="DateContent"><xs:simpleContent>'
        '<xs:restriction base="AnyContent"><xs:simpleType>'
        '<xs:restriction base="xs:date"/>'
        "</xs:simpleType></xs:restriction></xs:simpleContent></xs:complexType>"
        '<xs:element name="temp" type="AnyContent">'
        '<xs:alternative test="@kind = \'date\'" type="DateContent"/>'
        "</xs:element>"
    )
    assert errors(parse(body, '<temp kind="date">not-a-date</temp>')) == ["value"]


def _cta_inherited(inheritable: str) -> str:
    return (
        '<xs:complexType name="Doc"><xs:sequence>'
        '<xs:element ref="chap" maxOccurs="unbounded"/>'
        "</xs:sequence>"
        f'<xs:attribute name="kind" type="xs:string" inheritable="{inheritable}"/>'
        "</xs:complexType>"
        '<xs:complexType name="ChapA"><xs:sequence>'
        '<xs:element name="a"/></xs:sequence></xs:complexType>'
        '<xs:complexType name="ChapB"><xs:sequence>'
        '<xs:element name="b"/></xs:sequence></xs:complexType>'
        '<xs:element name="doc" type="Doc"/>'
        '<xs:element name="chap">'
        '<xs:alternative test="@kind = \'a\'" type="ChapA"/>'
        '<xs:alternative test="@kind = \'b\'" type="ChapB"/>'
        "</xs:element>"
    )


def test_alternative_instance_inheritable_attribute_is_in_scope() -> None:
    # XSD 1.1 §3.4.2.5 attribute inheritance: an inheritable attribute on
    # an ancestor is visible to a descendant's CTA test (Saxon cta0009).
    body = _cta_inherited("true")
    assert errors(parse(body, '<doc kind="a"><chap><a/></chap></doc>')) == []
    assert errors(parse(body, '<doc kind="a"><chap><b/></chap></doc>')) != []


def test_alternative_instance_non_inheritable_attribute_not_in_scope() -> None:
    # The same attribute with inheritable="false" is not inherited, so the
    # descendant's tests are false and the declared ur-type governs
    # (Saxon cta0012).
    body = _cta_inherited("false")
    assert errors(parse(body, '<doc kind="a"><chap><b/></chap></doc>')) == []
