"""XSD 1.1 feature substrate: the assertion and CTA XPath 2.0 subsets.

Task 2 covers only the shared parse/whitelist/evaluate substrate that the
``xs:assert``/``xs:assertion`` (Tasks 3-4) and conditional type assignment
(Tasks 5-6) phases consume. The admitted and rejected shapes below are
condensed from the Saxon ``Assert``/``CTA`` corpus and the IBM
``typeAlternatives`` suite, under XSD 1.1 §3.13.1 (assertions) and §3.3.2.1
(conditional type assignment).
"""

import xml.etree.ElementTree as ET

import pytest

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
