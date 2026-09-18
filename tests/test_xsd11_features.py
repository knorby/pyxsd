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
