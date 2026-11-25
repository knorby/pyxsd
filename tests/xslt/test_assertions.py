"""Result assertions are independent of the engine under test.

The evaluator consumes a synthetic raw result or a result snapshot and decides
the verdict itself.  Every assertion family, nested combination, expected-error
strictness, adapter gap, and in-scope namespace recovery is exercised here
without importing the XSLT implementation.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from elementpath import AttributeNode, TextNode, UnsupportedFeatureError, build_node_tree
from elementpath.exceptions import ElementPathSyntaxError
from elementpath.xpath3 import XPath31Parser
from elementpath.xpath_tokens import XPathArray, XPathMap
from tests.xslt.assertions import (
    AllOf,
    AnyOf,
    Assertion,
    AssertionContext,
    AssertionCount,
    AssertionDeepEq,
    AssertionDispute,
    AssertionEmpty,
    AssertionEq,
    AssertionFailure,
    AssertionFalse,
    AssertionGap,
    AssertionMessage,
    AssertionPermutation,
    AssertionResultDocument,
    AssertionSerialization,
    AssertionSerializationError,
    AssertionSerializationMatches,
    AssertionStringValue,
    AssertionTrue,
    AssertionType,
    AssertionWarning,
    AssertionXML,
    AssertionXPath,
    Not,
    ResultSnapshot,
    _context_node_path,
    _context_root,
    _deep_equal,
    _is_gap,
    _namespace_map,
    _unexpected_error,
    _xml_mismatch,
    _xml_namespace_scopes,
    assert_case,
    evaluate_assertion,
    parse_result,
)
from tests.xslt.catalog import load_catalog
from tests.xslt.model import Environment, Invocation
from tests.xslt.model import TestCase as XmlTestCase
from tests.xslt.outcomes import Outcome
from tests.xslt.suite import SUITE_PROFILES, run_case

FIXTURES = Path(__file__).parent / "fixtures"
XSD = "http://www.w3.org/2001/XMLSchema"
XQT = "http://www.w3.org/2005/xqt-errors"


class XdmAtomic:
    """A tiny symbolic stand-in for a future XDM atomic value."""

    @staticmethod
    def string(value: str) -> str:
        return value


def as_xdm(value: Any) -> Any:
    return value


class XdmMap(dict[str, Any]):
    """A minimal map test double; a Mapping is enough for assertion binding."""

    @classmethod
    def empty(cls) -> XdmMap:
        return cls()

    def put(self, key: str, value: Any) -> XdmMap:
        self[key] = value
        return self


def make_case(
    result_xml: str,
    *,
    name: str = "set/case-001",
    local_name: str = "case-001",
    test_set: str = "set",
    source: str = "set/test-set.xml",
    root: Path | None = None,
    result_base: str | None = None,
    namespaces: dict[str, str] | None = None,
) -> XmlTestCase:
    try:
        count = len(list(ET.fromstring(result_xml).iter())) if result_xml else 0
    except ET.ParseError:
        count = 0
    scope = tuple(sorted((namespaces or {}).items()))
    return XmlTestCase(
        name=name,
        local_name=local_name,
        test_set=test_set,
        description="",
        source=PurePosixPath(source),
        environment=Environment(),
        invocation=Invocation(),
        result_xml=result_xml,
        document_root=root,
        result_base=PurePosixPath(result_base) if result_base is not None else None,
        namespace_scopes=tuple(scope for _ in range(count)),
    )


def expected_error_case(code: str) -> str:
    return f'<result xmlns="http://www.w3.org/2012/10/xslt-test-catalog"><error code="{code}"/></result>'


def test_nested_any_of_and_raw_map_assertion() -> None:
    assertion = AnyOf(
        [
            AssertionXPath("map:size($result) = 2"),
            AllOf([AssertionEmpty("$missing"), Not(AssertionTrue())]),
        ]
    )
    raw_map = (
        XdmMap.empty().put(XdmAtomic.string("a"), as_xdm(1)).put(XdmAtomic.string("b"), as_xdm(2))
    )

    assert evaluate_assertion(assertion, raw_result=raw_map)


def test_xml_and_deep_equal_canonical_comparison() -> None:
    snapshot = ResultSnapshot(xml='<out xmlns:q="urn:x"><q:a b="2" a="1"/></out>')

    assert evaluate_assertion(
        AssertionXML('<out xmlns:p="urn:x"><p:a a="1" b="2"/></out>', ignore_prefixes=True),
        result=snapshot,
    )
    assert not evaluate_assertion(
        AssertionXML('<out xmlns:p="urn:x"><p:a a="1" b="2"/></out>'), result=snapshot
    )
    assert not evaluate_assertion(AssertionXML("<out><a/></out>"), result=snapshot)


def test_xml_fragment_wrapper_and_order_are_significant() -> None:
    snapshot = ResultSnapshot(xml="<a/><b/>")

    assert evaluate_assertion(AssertionXML("<a/><b/>"), result=snapshot)
    assert not evaluate_assertion(AssertionXML("<b/><a/>"), result=snapshot)
    assert not evaluate_assertion(AssertionXML("<a/>"), result=snapshot)


def test_xml_mismatch_detail_names_the_first_differing_path() -> None:
    case = make_case(
        "<result><assert-xml><![CDATA[<out><a>1</a><b/></out>]]></assert-xml></result>"
    )

    result = assert_case(case, ResultSnapshot(xml="<out><a>2</a><b/></out>"))

    assert result.outcome is Outcome.FAIL
    assert result.message.splitlines() == [
        "actual XML differs from the expected XML: text differs at /out/a",
        "expected: 1",
        "actual: 2",
    ]


@pytest.mark.parametrize(
    ("expected", "actual", "first_line", "expected_fragment", "actual_fragment"),
    [
        (
            "<out><a>1</a></out>",
            "<out><b>1</b></out>",
            "element names differ at /out/a",
            "<a>1</a>",
            "<b>1</b>",
        ),
        (
            '<out x="1"/>',
            '<out x="2"/>',
            "attributes differ at /out",
            '<out x="1"></out>',
            '<out x="2"></out>',
        ),
        (
            '<out xmlns:p="urn:x" p:x="1"/>',
            '<out xmlns:p="urn:x" p:x="2"/>',
            "attributes differ at /out",
            '<out {urn:x}x="1"></out>',
            '<out {urn:x}x="2"></out>',
        ),
        (
            '<p:out xmlns:p="urn:x"/>',
            '<q:out xmlns:q="urn:x"/>',
            "element prefixes differ at /p:out",
            "<p:out></p:out>",
            "<q:out></q:out>",
        ),
        (
            "<out><!--a--></out>",
            "<out><!--b--></out>",
            "comment text differs at /out",
            "<!--a-->",
            "<!--b-->",
        ),
        (
            "<out><?pi a?></out>",
            "<out><?pi b?></out>",
            "processing instruction differs at /out",
            "<?pi a?>",
            "<?pi b?>",
        ),
        (
            "<out><a/></out>",
            "<out>text</out>",
            "node kinds differ at /out/a",
            "<a></a>",
            "text",
        ),
        (
            "<out>text</out>",
            "<out/>",
            "an expected node is missing at /out",
            "text",
            "(none)",
        ),
        (
            "<out/>",
            "<out>text</out>",
            "an unexpected node appeared at /out",
            "(none)",
            "text",
        ),
        (
            "<out><a/><a/></out>",
            "<out><a/><a/><a/></out>",
            "an unexpected node appeared at /out/a[3]",
            "(none)",
            "<a></a>",
        ),
        (
            "<out><a/><a/></out>",
            "<out><a/></out>",
            "an expected node is missing at /out/a[2]",
            "<a></a>",
            "(none)",
        ),
        (
            "<out><a>x</a><b>1</b></out>",
            "<out><a>x</a><b>2</b></out>",
            "text differs at /out/b",
            "1",
            "2",
        ),
        (
            "<out><!--x--><b>1</b></out>",
            "<out><!--x--><b>2</b></out>",
            "text differs at /out/b",
            "1",
            "2",
        ),
        (
            "<out><?pi x?><b>1</b></out>",
            "<out><?pi x?><b>2</b></out>",
            "text differs at /out/b",
            "1",
            "2",
        ),
    ],
)
def test_xml_mismatch_names_the_first_structural_difference(
    expected: str,
    actual: str,
    first_line: str,
    expected_fragment: str,
    actual_fragment: str,
) -> None:
    lines = _xml_mismatch(expected, actual, False).splitlines()

    assert lines == [
        f"actual XML differs from the expected XML: {first_line}",
        f"expected: {expected_fragment}",
        f"actual: {actual_fragment}",
    ]


def test_xml_mismatch_falls_back_to_lengths_and_serialized_prefixes() -> None:
    empty = _xml_mismatch("", "<out><a>1</a></out>", False).splitlines()
    unparsed = _xml_mismatch("<out>", "<out/>", False).splitlines()

    assert empty == [
        "actual XML differs from the expected XML: expected 0 characters, actual 19 characters",
        "expected: ",
        "actual: <out><a>1</a></out>",
    ]
    assert unparsed == [
        "actual XML differs from the expected XML: expected 5 characters, actual 6 characters",
        "expected: <out>",
        "actual: <out/>",
    ]


def test_xml_mismatch_bounds_fragments_and_never_raises() -> None:
    bounded = _xml_mismatch(
        f"<out><a>{'x' * 400}</a></out>", f"<out><a>{'y' * 400}</a></out>", False
    ).splitlines()
    identical = _xml_mismatch("<out/>", "<out/>", False)

    assert bounded == [
        "actual XML differs from the expected XML: text differs at /out/a",
        f"expected: {'x' * 120}",
        f"actual: {'y' * 120}",
    ]
    assert identical.startswith(
        "actual XML differs from the expected XML: expected 6 characters, actual 6 characters"
    )


def test_xml_mismatch_for_an_empty_expected_result_is_a_failure() -> None:
    case = make_case("<result><assert-xml><![CDATA[]]></assert-xml></result>")

    result = assert_case(case, ResultSnapshot(xml="<out/>"))

    assert result.outcome is Outcome.FAIL
    assert result.message.splitlines()[0] == (
        "actual XML differs from the expected XML: expected 0 characters, actual 6 characters"
    )


def test_xml_expected_files_resolve_against_the_case_document(tmp_path: Path) -> None:
    (tmp_path / "set").mkdir()
    (tmp_path / "set" / "expected.xml").write_text("<out><a>1</a></out>", encoding="utf-8")
    case = make_case(
        "<result><assert-xml file='expected.xml'/></result>",
        root=tmp_path,
        result_base="set",
    )

    assert assert_case(case, ResultSnapshot(xml="<out><a>1</a></out>")).outcome is Outcome.PASS
    missing = make_case(
        "<result><assert-xml file='missing.xml'/></result>",
        root=tmp_path,
        result_base="set",
    )
    result = assert_case(missing, ResultSnapshot(xml="<out/>"))
    assert result.outcome is Outcome.CORPUS_DISPUTE
    assert "missing.xml" in result.message


def test_xml_without_an_actual_document_is_an_adapter_gap() -> None:
    case = make_case("<result><assert-xml><![CDATA[<a/>]]></assert-xml></result>")

    result = assert_case(case, ResultSnapshot(serialized="<a/>"))

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.coverage == frozenset()


def test_string_value_count_type_empty_and_boolean_assertions() -> None:
    snapshot = ResultSnapshot(raw=("  alpha \n beta ",), xml="<out>  alpha \n beta </out>")

    assert evaluate_assertion(AssertionStringValue("alpha beta"), result=snapshot)
    assert evaluate_assertion(
        AssertionStringValue("  alpha \n beta ", normalize_space=False), result=snapshot
    )
    assert not evaluate_assertion(
        AssertionStringValue("alpha beta", normalize_space=False), result=snapshot
    )
    assert evaluate_assertion(AssertionCount(1), result=snapshot)
    assert not evaluate_assertion(AssertionCount(2), result=snapshot)
    assert evaluate_assertion(AssertionType("xs:string"), result=snapshot)
    assert not evaluate_assertion(AssertionType("xs:integer"), result=snapshot)
    assert evaluate_assertion(AssertionEmpty(), result=ResultSnapshot(raw=()))
    assert not evaluate_assertion(AssertionEmpty(), result=snapshot)
    assert evaluate_assertion(AssertionEmpty("()"), result=ResultSnapshot(raw=()))
    assert evaluate_assertion(AssertionTrue(), result=ResultSnapshot(raw=(True,)))
    assert not evaluate_assertion(AssertionTrue(), result=ResultSnapshot(raw=(False,)))
    assert evaluate_assertion(AssertionFalse(), result=ResultSnapshot(raw=(False,)))
    assert not evaluate_assertion(AssertionFalse(), result=ResultSnapshot(raw=(True,)))
    assert not evaluate_assertion(AssertionTrue(), result=ResultSnapshot(raw=(1,)))


def test_raw_value_equality_and_permutation() -> None:
    snapshot = ResultSnapshot(raw=(2, 1))

    assert evaluate_assertion(AssertionEq("2"), result=ResultSnapshot(raw=(2,)))
    assert not evaluate_assertion(AssertionEq("3"), result=ResultSnapshot(raw=(2,)))
    assert evaluate_assertion(AssertionDeepEq("(2, 1)"), result=snapshot)
    assert not evaluate_assertion(AssertionDeepEq("(1, 2)"), result=snapshot)
    assert evaluate_assertion(AssertionPermutation("(1, 2)"), result=snapshot)
    assert not evaluate_assertion(AssertionPermutation("(1, 3)"), result=snapshot)
    assert not evaluate_assertion(AssertionPermutation("(1)"), result=snapshot)


def test_document_results_participate_in_type_and_count_assertions() -> None:
    case = make_case(
        "<result><all-of><assert-type>document-node()</assert-type><assert-count>1</assert-count></all-of></result>"
    )

    result = assert_case(case, ResultSnapshot(xml="<out/>"))

    assert result.outcome is Outcome.PASS
    assert result.coverage == frozenset({"type", "count"})


def test_message_assertions_search_every_message() -> None:
    assertion = parse_result(
        '<result xmlns="http://www.w3.org/2012/10/xslt-test-catalog">'
        "<assert-message><assert-xml><![CDATA[<msg>ok</msg>]]></assert-xml></assert-message>"
        "</result>"
    )
    snapshot = ResultSnapshot(
        xml="<out/>",
        messages=(ResultSnapshot(xml="<msg>no</msg>"), ResultSnapshot(xml="<msg>ok</msg>")),
    )

    assert evaluate_assertion(assertion, result=snapshot)
    assert not evaluate_assertion(
        parse_result(
            "<result><assert-message><assert-xml><![CDATA[<msg>missing</msg>]]></assert-xml></assert-message></result>"
        ),
        result=snapshot,
    )
    assert not evaluate_assertion(
        parse_result("<result><assert-message><assert>true()</assert></assert-message></result>"),
        result=ResultSnapshot(xml="<out/>", messages=()),
    )
    with pytest.raises(AssertionGap):
        evaluate_assertion(
            parse_result(
                "<result><assert-message><assert>true()</assert></assert-message></result>"
            ),
            result=ResultSnapshot(xml="<out/>"),
        )


def test_secondary_result_assertions_resolve_case_relative_uris() -> None:
    assertion = parse_result(
        '<result><assert-result-document uri="out.xml"><assert-xml><![CDATA[<out/>]]></assert-xml>'
        "</assert-result-document></result>"
    )
    case = make_case("<result/>")
    snapshot = ResultSnapshot(
        xml="<principal/>", secondary={"case-001/out.xml": ResultSnapshot(xml="<out/>")}
    )

    assert evaluate_assertion(assertion, result=snapshot, case=case)
    assert evaluate_assertion(
        assertion,
        result=ResultSnapshot(
            xml="<principal/>", secondary={"out.xml": ResultSnapshot(xml="<out/>")}
        ),
        case=case,
    )
    assert not evaluate_assertion(
        assertion,
        result=ResultSnapshot(
            xml="<principal/>", secondary={"case-001/out.xml": ResultSnapshot(xml="<other/>")}
        ),
        case=case,
    )
    assert not evaluate_assertion(
        assertion, result=ResultSnapshot(xml="<principal/>", secondary={}), case=case
    )
    with pytest.raises(AssertionGap):
        evaluate_assertion(assertion, result=ResultSnapshot(xml="<principal/>"), case=case)


def test_serialization_assertions_use_the_engine_serialization() -> None:
    snapshot = ResultSnapshot(xml="<out/>", serialized="<out/>\r\n", serialization_method="xml")

    assert evaluate_assertion(AssertionSerialization("<out/>\n"), result=snapshot)
    assert not evaluate_assertion(AssertionSerialization("<other/>"), result=snapshot)
    assert evaluate_assertion(
        AssertionSerializationMatches("<out[^>]*>", flags="i"), result=snapshot
    )
    assert not evaluate_assertion(AssertionSerializationMatches("^<other/>$"), result=snapshot)
    with pytest.raises(AssertionGap):
        evaluate_assertion(AssertionSerialization("<out/>"), result=ResultSnapshot(xml="<out/>"))
    assert evaluate_assertion(
        AssertionSerialization("<out/>", method="html"),
        result=ResultSnapshot(serialized="<out/>", serialization_method="xml"),
    )
    assert not evaluate_assertion(
        AssertionSerialization("<other/>", method="html"),
        result=ResultSnapshot(serialized="<out/>", serialization_method="xml"),
    )


def test_serialization_error_and_warning_assertions() -> None:
    assert evaluate_assertion(
        AssertionSerializationError("SERE0014"),
        result=ResultSnapshot(serialized=None, serialization_error_code="SERE0014"),
    )
    assert not evaluate_assertion(
        AssertionSerializationError("SERE0014"),
        result=ResultSnapshot(serialized=None, serialization_error_code="SERE0001"),
    )
    assert not evaluate_assertion(
        AssertionSerializationError("SERE0014"), result=ResultSnapshot(xml="<out/>")
    )
    assert evaluate_assertion(AssertionWarning(), result=ResultSnapshot(warnings=("w",)))
    assert not evaluate_assertion(AssertionWarning(), result=ResultSnapshot(warnings=()))
    with pytest.raises(AssertionGap):
        evaluate_assertion(AssertionWarning(), result=ResultSnapshot(xml="<out/>"))


def test_expected_errors_are_strict_and_nested_any_of_works() -> None:
    direct = make_case(expected_error_case("XTDE0040"))

    passed = assert_case(direct, ResultSnapshot(error_code="XTDE0040"))
    wrong = assert_case(direct, ResultSnapshot(error_code="XTDE0041"))
    missing = assert_case(direct, ResultSnapshot(xml="<out/>"))

    assert passed.outcome is Outcome.PASS
    assert passed.expected_error_code == "XTDE0040"
    assert passed.actual_error_code == "XTDE0040"
    assert passed.coverage == frozenset({"expected-error"})
    assert wrong.outcome is Outcome.WRONG_ERROR_CODE
    assert wrong.expected_error_code == "XTDE0040"
    assert wrong.actual_error_code == "XTDE0041"
    assert missing.outcome is Outcome.FAIL

    nested = make_case(
        '<result><any-of><error code="XTDE0040"/><assert-xml><![CDATA[<out/>]]></assert-xml></any-of></result>'
    )
    assert assert_case(nested, ResultSnapshot(error_code="XTDE0040")).outcome is Outcome.PASS
    assert (
        assert_case(nested, ResultSnapshot(error_code="XTDE0041")).outcome
        is Outcome.WRONG_ERROR_CODE
    )
    assert assert_case(nested, ResultSnapshot(xml="<out/>")).outcome is Outcome.PASS
    assert assert_case(nested, ResultSnapshot(xml="<other/>")).outcome is Outcome.FAIL


def test_not_error_expectation_fails_when_an_error_occurs() -> None:
    case = make_case(
        '<result><not><any-of><error code="*"/><assert-xml><![CDATA[<out/>]]></assert-xml></any-of></not></result>'
    )

    assert assert_case(case, ResultSnapshot(error_code="XTDE0040")).outcome is Outcome.FAIL
    assert assert_case(case, ResultSnapshot(xml="<other/>")).outcome is Outcome.PASS


@pytest.mark.parametrize(
    "assertion",
    [
        AssertionXPath("true()"),
        AssertionXML("<out/>"),
        AssertionStringValue("x"),
        AssertionCount(1),
        AssertionType("item()"),
        AssertionEmpty(),
        AssertionTrue(),
        AssertionFalse(),
        AssertionEq("1"),
        AssertionDeepEq("1"),
        AssertionPermutation("1"),
        AssertionSerialization("<out/>"),
        AssertionSerializationMatches("x"),
        AssertionWarning(),
    ],
)
def test_every_error_mode_arm_reports_the_raised_error(assertion: Assertion) -> None:
    snapshot = ResultSnapshot(error_code="XTDE0040", error_message="[XTDE0040] boom")

    verdict = assertion.evaluate_verdict(AssertionContext(snapshot, error_mode=True))

    assert verdict.status == "fail"
    assert verdict.detail == "unexpected error: XTDE0040: boom"


def test_unexpected_error_detail_keeps_the_code_message_and_bounds_its_length() -> None:
    def detail(result: ResultSnapshot) -> str:
        return _unexpected_error(AssertionContext(result, error_mode=True)).detail

    assert (
        detail(ResultSnapshot(error_code="XTDE0040", error_message="bad\nname\r\n\nhere"))
        == "unexpected error: XTDE0040: bad name here"
    )
    assert detail(ResultSnapshot(error_code="XTDE0040")) == "unexpected error: XTDE0040"
    assert detail(ResultSnapshot(error_message="bare message")) == "unexpected error: bare message"
    assert detail(ResultSnapshot()) == "unexpected error: unknown error"
    assert (
        detail(
            ResultSnapshot(serialization_error_code="SERE0014", serialization_error_message="no")
        )
        == "unexpected error: SERE0014: no"
    )
    bounded = detail(ResultSnapshot(error_code="XTDE0040", error_message="x" * 300))
    assert len(bounded) == len("unexpected error: ") + 200
    assert bounded.startswith("unexpected error: XTDE0040: x")


def test_assert_case_keeps_the_error_detail_in_the_failure_message() -> None:
    case = make_case("<result><assert-xml><![CDATA[<out/>]]></assert-xml></result>")

    result = assert_case(
        case,
        ResultSnapshot(
            error_code="XTDE0650", error_message="[XTDE0650] circularity at corpus/x.xsl"
        ),
    )

    assert result.outcome is Outcome.FAIL
    assert result.message == "unexpected error: XTDE0650: circularity at corpus/x.xsl"


def test_error_code_qname_normalization_is_deliberate() -> None:
    case = make_case(
        '<result><error code="err:XTDE0040"/></result>',
        namespaces={"err": XQT},
    )
    assert assert_case(case, ResultSnapshot(error_code="XTDE0040")).outcome is Outcome.PASS

    custom = make_case(
        '<result><error code="my:ABCD9999"/></result>',
        namespaces={"my": "urn:custom"},
    )
    assert (
        assert_case(custom, ResultSnapshot(error_code="Q{urn:custom}ABCD9999")).outcome
        is Outcome.PASS
    )
    assert (
        assert_case(custom, ResultSnapshot(error_code="Q{urn:other}ABCD9999")).outcome
        is Outcome.WRONG_ERROR_CODE
    )
    assert (
        assert_case(custom, ResultSnapshot(error_code="ABCD9999")).outcome
        is Outcome.WRONG_ERROR_CODE
    )


def test_unresolved_expected_error_prefix_is_a_dispute() -> None:
    case = make_case('<result><error code="nope:XTDE0040"/></result>')

    result = assert_case(case, ResultSnapshot(error_code="XTDE0040"))

    assert result.outcome is Outcome.CORPUS_DISPUTE
    assert "nope" in result.message


def test_unsupported_assertion_and_unexecuted_families_are_adapter_gaps() -> None:
    unsupported = make_case(
        "<result><assert-posture-and-sweep><xpath>.</xpath></assert-posture-and-sweep></result>"
    )
    gap = assert_case(unsupported, ResultSnapshot(xml="<out/>"))
    assert gap.outcome is Outcome.ADAPTER_GAP
    assert gap.coverage == frozenset()

    unmatched = make_case(
        "<result><all-of><assert-xml><![CDATA[<other/>]]></assert-xml><serialization-matches>x</serialization-matches></all-of></result>"
    )
    failed = assert_case(unmatched, ResultSnapshot(xml="<out/>"))
    assert failed.outcome is Outcome.FAIL
    assert failed.coverage == frozenset({"xml"})

    partly_unavailable = make_case(
        "<result><any-of><assert-xml><![CDATA[<other/>]]></assert-xml><serialization-matches>x</serialization-matches></any-of></result>"
    )
    gap = assert_case(partly_unavailable, ResultSnapshot(xml="<out/>"))
    assert gap.outcome is Outcome.ADAPTER_GAP
    assert gap.coverage == frozenset({"xml"})


def test_invalid_or_missing_expected_results_are_corpus_disputes() -> None:
    assert (
        assert_case(make_case(""), ResultSnapshot(xml="<out/>")).outcome is Outcome.CORPUS_DISPUTE
    )
    assert (
        assert_case(make_case("<result><assert>unclosed"), ResultSnapshot(xml="<out/>")).outcome
        is Outcome.CORPUS_DISPUTE
    )
    assert (
        assert_case(
            make_case("<result><assert-count>many</assert-count></result>"),
            ResultSnapshot(raw=(1,)),
        ).outcome
        is Outcome.CORPUS_DISPUTE
    )


def test_assert_case_reports_pass_fail_and_coverage() -> None:
    case = make_case(
        "<result><all-of><assert>true()</assert><assert-xml><![CDATA[<out/>]]></assert-xml></all-of></result>"
    )

    passed = assert_case(case, ResultSnapshot(xml="<out/>"))
    failed = assert_case(case, ResultSnapshot(xml="<other/>"))

    assert passed.outcome is Outcome.PASS
    assert passed.coverage == frozenset({"xpath", "xml"})
    assert failed.outcome is Outcome.FAIL
    assert failed.coverage == frozenset({"xpath", "xml"})


def test_assertions_recover_test_set_and_assertion_local_namespaces() -> None:
    catalog = load_catalog(FIXTURES / "assertions" / "assertion-test-set.xml")
    case = catalog.cases["assertion/namespace-001"]

    assert case.document_root == (FIXTURES / "assertions").resolve()
    assert case.result_base == PurePosixPath("assertion-test-set.xml").parent
    assert len(case.namespace_scopes) == 4
    assert ("xs", XSD) in case.namespace_scopes[0]
    assert ("local", XSD) in case.namespace_scopes[3]

    result = assert_case(case, ResultSnapshot(xml="<out/>"))

    assert result.outcome is Outcome.PASS
    assert result.coverage == frozenset({"xpath"})


def test_a_message_assertion_inspects_the_message_document() -> None:
    """A message's value is the document node its constructor builds.

    XSLT 3.0 section 18.1 defines the message content as a document node, so
    ``assert-message`` can read its nodes: an absolute path, markup that spans
    several top-level nodes, and a comment-only message.  The case is a
    synthetic catalog case that executes end to end; the message family in the
    coverage proves the assertions ran instead of being skipped as a gap.
    """
    catalog = load_catalog(FIXTURES / "assertions" / "assertion-test-set.xml")
    case = catalog.cases["assertion/message-001"]

    result = run_case(case, SUITE_PROFILES["xslt30"])

    assert result.outcome is Outcome.PASS
    assert result.coverage == frozenset({"xpath", "string", "xml", "message"})


def test_a_result_variable_assertion_needs_no_context_item() -> None:
    """``<output tree="no" result-var="result"/>`` binds the raw sequence.

    The catalog makes the assertion read that variable, so a multi-item result
    is legal and no context item exists.  Before the driver supported it the
    case was an adapter gap; the declared variable name is ``result``, the name
    the runner always binds (``runner/assert.xsl``).
    """
    catalog = load_catalog(FIXTURES / "assertions" / "assertion-test-set.xml")
    case = catalog.cases["assertion/result-var-001"]

    result = run_case(case, SUITE_PROFILES["xslt30"])

    assert result.outcome is Outcome.PASS
    assert result.coverage == frozenset({"xpath"})


def test_parse_result_recovers_namespaces_from_serialized_xml() -> None:
    assertion = parse_result(
        f'<result xmlns="http://www.w3.org/2012/10/xslt-test-catalog" xmlns:xs="{XSD}">'
        "<assert>xs:integer('3') = 3</assert></result>"
    )

    assert evaluate_assertion(assertion, result=ResultSnapshot(xml="<out/>"))


def test_parse_result_covers_every_catalog_assertion_shape() -> None:
    assertion = parse_result(
        '<result xmlns="http://www.w3.org/2012/10/xslt-test-catalog"><all-of>'
        "<assert>true()</assert>"
        "<assert-xml><![CDATA[<a/>]]></assert-xml>"
        "<assert-string-value> x </assert-string-value>"
        "<assert-count>2</assert-count>"
        "<assert-type>xs:string</assert-type>"
        "<assert-empty/>"
        "<assert-true/>"
        "<assert-false/>"
        "<assert-eq>1</assert-eq>"
        "<assert-deep-eq>1</assert-deep-eq>"
        "<assert-permutation>1</assert-permutation>"
        '<assert-serialization method="text">x</assert-serialization>'
        '<serialization-matches flags="i">x</serialization-matches>'
        '<assert-serialization-error code="SERE0014"/>'
        "<assert-message><assert>true()</assert></assert-message>"
        '<assert-result-document uri="out.xml"><assert-xml><![CDATA[<a/>]]></assert-xml></assert-result-document>'
        "<assert-warning/>"
        '<error code="XTDE0040"/>'
        "<any-of><assert>true()</assert></any-of>"
        "<not><assert>false()</assert></not>"
        "</all-of></result>"
    )

    assert isinstance(assertion, AllOf)
    assert [type(child).__name__ for child in assertion.assertions] == [
        "AssertionXPath",
        "AssertionXML",
        "AssertionStringValue",
        "AssertionCount",
        "AssertionType",
        "AssertionEmpty",
        "AssertionTrue",
        "AssertionFalse",
        "AssertionEq",
        "AssertionDeepEq",
        "AssertionPermutation",
        "AssertionSerialization",
        "AssertionSerializationMatches",
        "AssertionSerializationError",
        "AssertionMessage",
        "AssertionResultDocument",
        "AssertionWarning",
        "AssertionExpectedError",
        "AnyOf",
        "Not",
    ]


def test_evaluate_assertion_binds_a_single_raw_item_as_the_context() -> None:
    assert evaluate_assertion(AssertionXPath(". = 3"), raw_result=3)
    assert not evaluate_assertion(AssertionXPath(". = 3"), raw_result=4)
    assert evaluate_assertion(AssertionXPath("count($result) = 1"), raw_result=3)
    assert evaluate_assertion(AssertionXPath("count($result) = 0"), raw_result=())


def test_zero_item_xpath_context_is_an_empty_document_node() -> None:
    # Upstream uses a document node built with <xsl:document/>, not an element.
    assert evaluate_assertion(AssertionXPath("count(.) = 1"), raw_result=())
    assert evaluate_assertion(AssertionXPath("string(.) = ''"), raw_result=())
    assert evaluate_assertion(AssertionXPath("empty(/*)"), raw_result=())
    assert evaluate_assertion(AssertionXPath("boolean(.)"), raw_result=())
    assert not evaluate_assertion(AssertionXPath("empty(.)"), raw_result=())
    assert evaluate_assertion(AssertionXPath("count($result) = 0"), raw_result=())


def test_multi_item_xpath_context_is_an_explicit_adapter_gap() -> None:
    for expression in (
        ". = 1",
        "count(.) = 1",
        "string(.) = ''",
        "boolean(.)",
        "empty(.)",
        "count($result) = 2",
    ):
        with pytest.raises(AssertionGap, match="multi-item"):
            evaluate_assertion(AssertionXPath(expression), raw_result=(1, 2))


def test_a_path_step_assertion_uses_the_principal_result_element() -> None:
    # arrays-304 and maps-017 deliver one rootless element and no tree; its
    # own serialization is the only node context available.
    result = ResultSnapshot(raw=("x",), xml="<out>x</out>")
    case = make_case("<result><assert>/out = 'x'</assert></result>")

    assert evaluate_assertion(AssertionXPath("/out = 'x'"), result=result)
    assert assert_case(case, result).outcome is Outcome.PASS


def test_a_message_path_step_is_still_a_gap() -> None:
    # An xsl:message keeps only its string value, so there is no node to
    # supply.  Parsing that text as markup must not invent one.
    assertion = AssertionMessage(AssertionXPath("/smart = 'x'"))
    message = ResultSnapshot(raw=("Smart element",), xml="Smart element")

    with pytest.raises(AssertionGap, match="node context item"):
        evaluate_assertion(assertion, result=ResultSnapshot(messages=(message,)))


def test_a_multi_item_result_has_no_context_item() -> None:
    # The node fallback reads one delivered item; several items still pass no
    # context item, so a serialization cannot be substituted for them.
    result = ResultSnapshot(raw=("x", "y"), xml="<out>x</out>")

    with pytest.raises(AssertionGap, match="multi-item"):
        evaluate_assertion(AssertionXPath("/out = 'x'"), result=result)


def test_a_path_step_without_a_serialization_stays_a_gap() -> None:
    # One item, but nothing that describes it as a node: no markup exists to
    # re-parse, so the gap stands instead of an invented context.
    case = make_case("<result><assert>/out = 'x'</assert></result>")

    assert assert_case(case, ResultSnapshot(raw=("x",))).outcome is Outcome.ADAPTER_GAP


def test_multi_item_raw_results_gap_only_context_using_assertions() -> None:
    context_using = make_case("<result><assert>. = 1</assert></result>")
    empty_expression = make_case("<result><assert-empty>count($result) = 2</assert-empty></result>")
    type_case = make_case("<result><assert-type>xs:integer*</assert-type></result>")
    count_case = make_case("<result><assert-count>2</assert-count></result>")
    value_case = make_case("<result><assert-deep-eq>(1, 2)</assert-deep-eq></result>")
    empty_case = make_case("<result><assert-empty/></result>")

    assert assert_case(context_using, raw_result=(1, 2)).outcome is Outcome.ADAPTER_GAP
    assert assert_case(empty_expression, raw_result=(1, 2)).outcome is Outcome.ADAPTER_GAP
    assert assert_case(context_using, raw_result=(1, 2)).coverage == frozenset()
    assert assert_case(type_case, raw_result=(1, 2)).outcome is Outcome.PASS
    assert assert_case(count_case, raw_result=(1, 2)).outcome is Outcome.PASS
    assert assert_case(value_case, raw_result=(1, 2)).outcome is Outcome.PASS
    assert assert_case(empty_case, raw_result=(1, 2)).outcome is Outcome.FAIL


def test_assertion_base_requires_an_implementation() -> None:
    with pytest.raises(NotImplementedError):
        Assertion().evaluate_verdict(AssertionContext(ResultSnapshot()))


def test_dispute_and_gap_exceptions_are_distinct() -> None:
    assert issubclass(AssertionDispute, Exception)
    assert issubclass(AssertionGap, Exception)
    with pytest.raises(AssertionDispute):
        parse_result("<result><assert-count>not-a-number</assert-count></result>")
    with pytest.raises(AssertionGap):
        evaluate_assertion(AssertionXPath("not-a-function(1)"), result=ResultSnapshot(xml="<out/>"))


# ---------------------------------------------------------------------------
# Coverage edges: every adapter branch and every defensive diagnostic


@pytest.mark.parametrize(
    "assertion",
    [
        AssertionXPath("true()"),
        AssertionXML("<a/>"),
        AssertionStringValue("x"),
        AssertionCount(1),
        AssertionType("xs:string"),
        AssertionEmpty(),
        AssertionTrue(),
        AssertionFalse(),
        AssertionEq("1"),
        AssertionDeepEq("1"),
        AssertionPermutation("1"),
        AssertionSerialization("x"),
        AssertionSerializationMatches("x"),
        AssertionWarning(),
    ],
    ids=lambda assertion: type(assertion).__name__,
)
def test_every_success_assertion_fails_in_error_mode(assertion: Any) -> None:
    context = AssertionContext(ResultSnapshot(error_code="XTDE0040"), error_mode=True)

    verdict = assertion.evaluate_verdict(context)

    assert verdict.status == "fail"
    assert not context.coverage


def test_optional_boolean_and_bom_parsing() -> None:
    assert (
        parse_result(
            '<result><assert-xml ignore-prefixes="true"><![CDATA[<a/>]]></assert-xml></result>'
        ).ignore_prefixes
        is True
    )
    assert (
        parse_result(
            '<result><assert-xml ignore-prefixes="1"><![CDATA[<a/>]]></assert-xml></result>'
        ).ignore_prefixes
        is True
    )
    assert (
        parse_result(
            '<result><assert-xml ignore-prefixes="maybe"><![CDATA[<a/>]]></assert-xml></result>'
        ).ignore_prefixes
        is False
    )
    assert (
        parse_result(
            '<result><assert-string-value normalize-space="false">x</assert-string-value></result>'
        ).normalize_space
        is False
    )
    assert (
        parse_result(
            '<result><assert-serialization bom="true">x</assert-serialization></result>'
        ).bom
        is True
    )


def test_actual_xml_fragments_and_malformed_actuals() -> None:
    assert evaluate_assertion(AssertionXML("<a/><b/>"), result=ResultSnapshot(xml="<a/><b/>"))
    assert not evaluate_assertion(AssertionXML("<a/>"), result=ResultSnapshot(xml="<a>"))
    assert not evaluate_assertion(AssertionXPath("true()"), result=ResultSnapshot(xml="<a>"))


def test_empty_document_snapshot_is_a_document_node() -> None:
    case = make_case(
        "<result><all-of><assert-type>document-node()</assert-type><assert-count>1</assert-count></all-of></result>"
    )

    assert assert_case(case, ResultSnapshot(xml="")).outcome is Outcome.PASS


def test_xpath_binding_accepts_tree_element_map_and_array_values() -> None:
    tree = ET.ElementTree(ET.fromstring("<a/>"))
    parser = XPath31Parser(namespaces={"map": "http://www.w3.org/2005/xpath-functions/map"})
    mapping = XPathMap(parser, {"a": 1})
    array = XPathArray(parser, [1, 2])

    assert evaluate_assertion(AssertionXPath("count($result) = 1"), raw_result=(tree,))
    assert evaluate_assertion(
        AssertionXPath("count($result) = 1"), raw_result=(ET.fromstring("<a/>"),)
    )
    assert evaluate_assertion(AssertionXPath("map:size($result) = 1"), raw_result=(mapping,))
    assert evaluate_assertion(AssertionXPath("array:size($result) = 2"), raw_result=(array,))
    assert evaluate_assertion(AssertionXPath("array:size($result) = 2"), raw_result=([1, 2],))
    assert evaluate_assertion(AssertionXPath("array:size($result) = 2"), raw_result=((1, 2),))


def test_xpath_gap_and_runtime_failure_are_distinct() -> None:
    assert _is_gap(UnsupportedFeatureError("x"))
    assert _is_gap(ElementPathSyntaxError("x"))
    assert not _is_gap(RuntimeError("x"))
    with pytest.raises(AssertionGap):
        evaluate_assertion(AssertionXPath("1 +"), result=ResultSnapshot(xml="<out/>"))
    assert not evaluate_assertion(AssertionXPath("1 eq 'a'"), result=ResultSnapshot(xml="<out/>"))


def test_expected_expression_gaps_and_disputes() -> None:
    with pytest.raises(AssertionGap):
        evaluate_assertion(AssertionEq("not-a-function()"), result=ResultSnapshot(raw=(1,)))
    with pytest.raises(AssertionDispute):
        evaluate_assertion(AssertionEq("1 div 0"), result=ResultSnapshot(raw=(1,)))


def test_map_and_array_expected_expressions() -> None:
    parser = XPath31Parser()
    mapping = XPathMap(parser, {"a": 1})
    array = XPathArray(parser, [1, 2])

    assert evaluate_assertion(AssertionDeepEq("map{'a': 1}"), result=ResultSnapshot(raw=(mapping,)))
    assert evaluate_assertion(AssertionDeepEq("[1, 2]"), result=ResultSnapshot(raw=(array,)))


def test_deep_equal_reports_incomparable_values() -> None:
    with pytest.raises(AssertionFailure):
        _deep_equal(1, 1)  # type: ignore[arg-type]


def test_string_value_conversion_errors_are_failures() -> None:
    assert not evaluate_assertion(AssertionStringValue("x"), result=ResultSnapshot(raw=(object(),)))


def test_expected_files_require_a_root_and_stay_inside_it(tmp_path: Path) -> None:
    with pytest.raises(AssertionGap):
        evaluate_assertion(AssertionXML("", file="expected.xml"), result=ResultSnapshot(xml="<a/>"))
    case = make_case("<result/>", root=tmp_path, result_base="set")
    with pytest.raises(AssertionDispute):
        evaluate_assertion(
            AssertionXML("", file="/absolute.xml"), result=ResultSnapshot(xml="<a/>"), case=case
        )
    with pytest.raises(AssertionDispute):
        evaluate_assertion(
            AssertionXML("", file="../../outside.xml"),
            result=ResultSnapshot(xml="<a/>"),
            case=case,
        )


@pytest.mark.parametrize(
    ("expected", "actual", "equal"),
    [
        ("<?xml version='1.0'?><a/>", "<a/>", True),
        ("<a xmlns='urn:x'/>", "<a xmlns='urn:x'/>", True),
        ("<a xml:lang='en'/>", "<a xml:lang='en'/>", True),
        ("<a xml:lang='en'/>", "<a xml:lang='fr'/>", False),
        ("<a><!--x--></a>", "<a><!--x--></a>", True),
        ("<a><!--x--></a>", "<a><!--y--></a>", False),
        ("<a><?pi x?></a>", "<a><?pi x?></a>", True),
        ("<a><?pi x?></a>", "<a><?pi y?></a>", False),
        ("<a><?one x?></a>", "<a><?two x?></a>", False),
        ("<a>xy</a>", "<a>xy</a>", True),
        ("<a>xy</a>", "<a>xz</a>", False),
        ("text", "<a/>", False),
        ("<a/>", "text", False),
        ("<a b='1'/>", "<a b='2'/>", False),
        ("<a><b/></a>", "<a/>", False),
        ("<a/>\n", "<a/>", True),
        ("<a/>\n", "<a/>\n", True),
        ("<a/> \n <b/>", "<a/><b/>", True),
        (" x ", " x ", True),
    ],
)
def test_xml_canonical_edge_cases(expected: str, actual: str, equal: bool) -> None:
    assert evaluate_assertion(AssertionXML(expected), result=ResultSnapshot(xml=actual)) is equal


def test_malformed_expected_xml_is_a_dispute() -> None:
    with pytest.raises(AssertionDispute):
        evaluate_assertion(AssertionXML("<a>"), result=ResultSnapshot(xml="<a/>"))


def test_xml_1_1_expected_results_are_an_adapter_gap() -> None:
    with pytest.raises(AssertionGap, match=r"XML 1\.1"):
        evaluate_assertion(
            AssertionXML("<a>&#1;</a>", xml_version="1.1"),
            result=ResultSnapshot(xml="<a/>"),
        )


def test_namespace_scopes_fall_back_to_the_serialized_document() -> None:
    assert _xml_namespace_scopes("<result>") == ()
    assert "" not in _namespace_map((("", "urn:default"),))


def test_parse_result_rejects_wrong_roots_and_container_shapes() -> None:
    with pytest.raises(AssertionDispute):
        parse_result("<other/>")
    with pytest.raises(AssertionDispute):
        parse_result('<result xmlns="urn:other"><assert>true()</assert></result>')
    with pytest.raises(AssertionDispute):
        parse_result("<result><assert>true()</assert><assert>false()</assert></result>")
    for xml in (
        "<result><not/></result>",
        "<result><assert-message/></result>",
        "<result><assert-message><assert>true()</assert><assert>false()</assert></assert-message></result>",
        "<result><assert-result-document uri='x'/></result>",
        "<result><assert-result-document><assert>true()</assert></assert-result-document></result>",
    ):
        with pytest.raises(AssertionDispute):
            parse_result(xml)


def test_parse_result_pads_short_namespace_scope_lists() -> None:
    assertion = parse_result(
        "<result><all-of><assert>true()</assert></all-of></result>",
        namespace_scopes=((),),
    )

    assert isinstance(assertion, AllOf)
    assert isinstance(assertion.assertions[0], AssertionXPath)


def test_assert_case_accepts_raw_results_and_absent_results() -> None:
    case = make_case("<result><assert-empty/></result>")

    assert assert_case(case).outcome is Outcome.PASS
    assert assert_case(case, raw_result=()).outcome is Outcome.PASS
    assert assert_case(case, raw_result=(1,)).outcome is Outcome.FAIL
    assert assert_case(case, raw_result=[1]).outcome is Outcome.FAIL
    assert assert_case(case, raw_result=(1, 2)).outcome is Outcome.FAIL


def test_evaluate_assertion_accepts_sequences_and_missing_values() -> None:
    assert evaluate_assertion(AssertionCount(0))
    assert evaluate_assertion(AssertionCount(2), raw_result=[1, 2])
    assert evaluate_assertion(AssertionCount(2), raw_result=(1, 2))
    assert not evaluate_assertion(AssertionCount(1), raw_result=[1, 2])


def test_result_snapshot_normalizes_raw_lists() -> None:
    snapshot = ResultSnapshot(raw=[1, 2])  # type: ignore[arg-type]

    assert snapshot.raw == (1, 2)


def test_serialization_encoding_mismatch_is_a_failure() -> None:
    snapshot = ResultSnapshot(
        serialized="<a/>", serialization_method="xml", serialization_encoding="utf-8"
    )

    assert not evaluate_assertion(
        AssertionSerialization("<a/>", encoding="utf-16"), result=snapshot
    )


def test_invalid_serialization_pattern_is_a_dispute() -> None:
    with pytest.raises(AssertionDispute):
        evaluate_assertion(
            AssertionSerializationMatches("["),
            result=ResultSnapshot(serialized="<a/>"),
        )


def test_message_gaps_propagate() -> None:
    assertion = AssertionMessage(AssertionXML("<a/>"))

    with pytest.raises(AssertionGap):
        evaluate_assertion(
            assertion,
            result=ResultSnapshot(xml="<out/>", messages=(ResultSnapshot(),)),
        )


def test_secondary_result_without_a_case_matches_the_exact_uri() -> None:
    assertion = AssertionResultDocument("out.xml", AssertionXML("<out/>"))

    assert evaluate_assertion(
        assertion,
        result=ResultSnapshot(xml="<out/>", secondary={"out.xml": ResultSnapshot(xml="<out/>")}),
    )


def test_error_code_display_and_actual_normalization_variants() -> None:
    empty_namespace = make_case('<result><error code="Q{}UIOP9876"/></result>')
    assert (
        assert_case(empty_namespace, ResultSnapshot(error_code="Q{}UIOP9876")).outcome
        is Outcome.PASS
    )
    assert (
        assert_case(empty_namespace, ResultSnapshot(error_code="UIOP9876")).outcome
        is Outcome.WRONG_ERROR_CODE
    )
    custom = make_case('<result><error code="my:X"/></result>', namespaces={"my": "urn:custom"})
    assert (
        assert_case(custom, ResultSnapshot(error_code="other:X")).outcome
        is Outcome.WRONG_ERROR_CODE
    )
    assert (
        assert_case(
            make_case('<result><error code="*"/></result>'),
            ResultSnapshot(error_code="XTDE0040"),
        ).outcome
        is Outcome.PASS
    )


def test_all_of_reports_the_first_wrong_error() -> None:
    case = make_case(
        '<result><all-of><error code="XTDE0040"/><error code="XTDE0041"/></all-of></result>'
    )

    result = assert_case(case, ResultSnapshot(error_code="XTDE0099"))

    assert result.outcome is Outcome.WRONG_ERROR_CODE
    assert result.expected_error_code == "XTDE0040"
    assert result.coverage == frozenset({"expected-error"})


def test_xpath_assertions_use_the_error_namespace_default() -> None:
    case = make_case(
        '<result><assert>namespace-uri-from-QName(xs:QName("err:XTDE0040")) = "http://www.w3.org/2005/xqt-errors"</assert></result>'
    )

    assert assert_case(case, ResultSnapshot(xml="<out/>")).outcome is Outcome.PASS


def test_xml_declaration_without_terminator_is_left_alone() -> None:
    with pytest.raises(AssertionDispute):
        evaluate_assertion(AssertionXML("<?xml junk"), result=ResultSnapshot(xml="<a/>"))


def test_cdata_and_text_are_merged_for_canonical_comparison() -> None:
    assert evaluate_assertion(
        AssertionXML("<a><![CDATA[x]]>y</a>"), result=ResultSnapshot(xml="<a>xy</a>")
    )


def test_err_prefixed_actual_error_code_normalizes() -> None:
    case = make_case('<result><error code="XTDE0040"/></result>')

    assert assert_case(case, ResultSnapshot(error_code="err:XTDE0040")).outcome is Outcome.PASS


def test_assert_eq_requires_singletons() -> None:
    assert not evaluate_assertion(AssertionEq("(1, 2)"), raw_result=(1,))
    assert not evaluate_assertion(AssertionEq("1"), raw_result=(1, 2))


def test_serialization_assertions_reject_serialization_errors() -> None:
    snapshot = ResultSnapshot(serialization_error_code="SERE0014")

    assert not evaluate_assertion(AssertionSerialization("<a/>"), result=snapshot)
    assert not evaluate_assertion(AssertionSerializationMatches("<a/>"), result=snapshot)


def test_not_of_a_passing_assertion_fails() -> None:
    assert not evaluate_assertion(Not(AssertionTrue()), raw_result=True)


def test_parse_empty_result_is_a_dispute() -> None:
    with pytest.raises(AssertionDispute):
        parse_result("")


def test_assert_case_reports_malformed_actual_xml_as_a_failure() -> None:
    case = make_case("<result><assert-xml><![CDATA[<a/>]]></assert-xml></result>")

    result = assert_case(case, ResultSnapshot(xml="<a>"))

    assert result.outcome is Outcome.FAIL
    assert "well formed" in result.message
    assert result.coverage == frozenset({"xml"})


def test_all_of_propagates_a_gap_when_no_assertion_failed() -> None:
    with pytest.raises(AssertionGap):
        evaluate_assertion(
            AllOf([AssertionXML("<a/>"), AssertionWarning()]),
            result=ResultSnapshot(xml="<a/>"),
        )


CORPUS = Path(__file__).parent / "corpus"


@pytest.mark.skipif(not (CORPUS / "catalog.xml").is_file(), reason="XSLT corpus not checked out")
def test_real_corpus_result_assertions_all_parse() -> None:
    catalog = load_catalog(CORPUS / "catalog.xml")

    for case in catalog.cases.values():
        parse_result(case.result_xml, case.namespace_scopes)


def test_expected_serialization_files_use_declared_or_assertion_encoding(
    tmp_path: Path,
) -> None:
    (tmp_path / "set").mkdir()
    latin = "<out>café</out>".encode("iso-8859-1")
    (tmp_path / "set" / "declared.out").write_bytes(
        b'<?xml version="1.0" encoding="iso-8859-1"?>' + latin
    )
    (tmp_path / "set" / "explicit.out").write_bytes(latin)
    (tmp_path / "set" / "broken.out").write_bytes(b"\xff")
    case = make_case("<result/>", root=tmp_path, result_base="set")

    assert evaluate_assertion(
        AssertionSerialization(file="declared.out"),
        result=ResultSnapshot(
            serialized='<?xml version="1.0" encoding="iso-8859-1"?>' + "<out>café</out>"
        ),
        case=case,
    )
    assert evaluate_assertion(
        AssertionSerialization(file="explicit.out", encoding="iso-8859-1"),
        result=ResultSnapshot(serialized="<out>café</out>", serialization_encoding="iso-8859-1"),
        case=case,
    )
    with pytest.raises(AssertionDispute, match="decode"):
        evaluate_assertion(
            AssertionSerialization(file="broken.out"),
            result=ResultSnapshot(serialized="<out/>"),
            case=case,
        )


def test_serialization_bom_and_prefix_options_are_explicit() -> None:
    snapshot = ResultSnapshot(serialized="<a/>", serialization_bom=True)

    assert evaluate_assertion(AssertionSerialization("<a/>", bom=True), result=snapshot)
    assert not evaluate_assertion(AssertionSerialization("<a/>", bom=False), result=snapshot)
    with pytest.raises(AssertionGap, match="BOM"):
        evaluate_assertion(
            AssertionSerialization("<a/>", bom=True),
            result=ResultSnapshot(serialized="<a/>"),
        )
    with pytest.raises(AssertionGap, match="prefix-insensitive"):
        evaluate_assertion(
            AssertionSerialization("<a/>", ignore_prefixes=True),
            result=ResultSnapshot(serialized="<a/>"),
        )
    assert (
        parse_result(
            '<result><assert-serialization ignore-prefixes="true">x</assert-serialization></result>'
        ).ignore_prefixes
        is True
    )


def test_secondary_results_match_absolute_uris_by_suffix() -> None:
    assertion = AssertionResultDocument("out.xml", AssertionXML("<out/>"))
    snapshot = ResultSnapshot(
        xml="<principal/>",
        secondary={"file:///tmp/case-001/out.xml": ResultSnapshot(xml="<out/>")},
    )

    assert evaluate_assertion(assertion, result=snapshot)
    ambiguous = ResultSnapshot(
        xml="<principal/>",
        secondary={
            "file:///tmp/a/out.xml": ResultSnapshot(xml="<out/>"),
            "file:///tmp/b/out.xml": ResultSnapshot(xml="<out/>"),
        },
    )
    with pytest.raises(AssertionGap, match="ambiguous"):
        evaluate_assertion(assertion, result=ambiguous)


def test_invalid_type_assertions_are_adapter_gaps() -> None:
    with pytest.raises(AssertionGap, match="type assertion"):
        evaluate_assertion(AssertionType("not a type"), raw_result=1)


def test_absolute_xpath_paths_use_the_result_document_root() -> None:
    snapshot = ResultSnapshot(xml="<out><a>1</a><a>2</a></out>")

    assert evaluate_assertion(AssertionXPath("/out"), result=snapshot)
    assert evaluate_assertion(AssertionXPath("count(//a) = 2"), result=snapshot)
    assert not evaluate_assertion(AssertionXPath("empty(/out/node())"), result=snapshot)
    assert evaluate_assertion(AssertionXPath("count(/*) = 1"), result=snapshot)


def test_absolute_xpath_paths_use_an_element_item_document_root() -> None:
    element = ET.fromstring("<out><a/></out>")

    assert evaluate_assertion(AssertionXPath("/out"), raw_result=(element,))
    assert evaluate_assertion(AssertionXPath("count(//a) = 1"), raw_result=(element,))


def test_context_node_path_detection_separates_context_and_variable_roots() -> None:
    parser = XPath31Parser()
    for expression in (
        "/out",
        "/",
        "count(//a)",
        "(//a)[1]",
        "/out/a",
        "a",
        "a/b",
        "./a",
        ".//a",
        "..",
        "../a",
        "child::a",
        "descendant::a",
        "@a",
        "attribute::a",
        "*",
        "text()",
        "node()",
        "count(a)",
        "[a]",
    ):
        assert _context_node_path(parser.parse(expression)), expression
    for expression in (
        "1 + 2",
        "$result/a",
        "$result//a",
        "$result/a[1]",
        "$result[foo/bar]",
        "$result[child::foo]",
        "$result/(a|b)",
        "doc('x')/a",
        "(1 to 5)[. > 2]",
        "[]",
        "$v + 1",
        "let $x := 1 return $x",
        "1 instance of xs:integer",
        "1 instance of array(*)",
        "1 instance of map(*)",
        "xs:integer('1')",
    ):
        assert not _context_node_path(parser.parse(expression)), expression


_CONTEXT_PATH_EXPRESSIONS = (
    "/out",
    "a",
    "./a",
    ".//a",
    "..",
    "../a",
    "child::a",
    "descendant::a",
    "@a",
    "attribute::a",
    "*",
    "text()",
    "node()",
    "count(a)",
)


@pytest.mark.parametrize(
    "raw",
    [
        (1,),
        ("atomic",),
        (XPathMap(XPath31Parser(), {"a": 1}),),
        (XPathArray(XPath31Parser(), [1, 2]),),
    ],
    ids=["integer", "string", "map", "array"],
)
@pytest.mark.parametrize("expression", _CONTEXT_PATH_EXPRESSIONS)
def test_context_node_paths_with_non_node_context_are_adapter_gaps(
    expression: str,
    raw: tuple[Any, ...],
) -> None:
    with pytest.raises(AssertionGap, match="XPTY0020"):
        evaluate_assertion(AssertionXPath(expression), raw_result=raw)


def test_absolute_expected_expression_without_node_context_is_an_adapter_gap() -> None:
    with pytest.raises(AssertionGap, match="context-item path"):
        evaluate_assertion(AssertionEq("/out"), raw_result=(1,))
    with pytest.raises(AssertionGap, match="context-item path"):
        evaluate_assertion(AssertionEq("count(a)"), raw_result=(1,))


def test_variable_rooted_paths_remain_evaluable() -> None:
    element = ET.fromstring("<out><a>1</a><a>2</a></out>")
    snapshot = ResultSnapshot(raw=(element,))

    assert evaluate_assertion(AssertionXPath("$result/a[1] = 1"), result=snapshot)
    assert evaluate_assertion(AssertionXPath("count($result/a) = 2"), result=snapshot)
    assert evaluate_assertion(AssertionXPath("count($result//a) = 2"), result=snapshot)
    assert not _context_node_path(XPath31Parser().parse("$result/a[1]"))


def test_assert_case_reports_context_path_atomic_context_as_adapter_gap() -> None:
    case = make_case("<result><assert>/out</assert></result>")

    result = assert_case(case, raw_result=(1,))

    assert result.outcome is Outcome.ADAPTER_GAP
    assert "XPTY0020" in result.message


def test_assert_case_reports_relative_path_atomic_context_as_adapter_gap() -> None:
    case = make_case("<result><assert>a</assert></result>")

    result = assert_case(case, raw_result=(1,))

    assert result.outcome is Outcome.ADAPTER_GAP
    assert "XPTY0020" in result.message


def test_blank_xml_result_is_a_true_empty_document() -> None:
    snapshot = ResultSnapshot(xml="")

    assert evaluate_assertion(AssertionXPath("count(/*) = 0"), result=snapshot)
    assert evaluate_assertion(AssertionXPath("empty(/*)"), result=snapshot)
    assert evaluate_assertion(AssertionXPath("count(.) = 1"), result=snapshot)


def test_assert_case_absolute_paths_are_not_empty() -> None:
    case = make_case("<result><assert>/out</assert></result>")

    result = assert_case(case, ResultSnapshot(xml="<out/>"))

    assert result.outcome is Outcome.PASS
    assert result.coverage == frozenset({"xpath"})


def test_string_value_joins_multi_item_results_context_free() -> None:
    snapshot = ResultSnapshot(raw=(1, 2))

    assert evaluate_assertion(AssertionStringValue("1 2"), result=snapshot)
    assert not evaluate_assertion(AssertionStringValue(""), result=snapshot)
    assert evaluate_assertion(AssertionStringValue(""), result=ResultSnapshot(raw=()))


def test_assert_case_string_value_joins_multi_item_results() -> None:
    case = make_case("<result><assert-string-value>1 2</assert-string-value></result>")

    result = assert_case(case, raw_result=(1, 2))

    assert result.outcome is Outcome.PASS
    assert result.coverage == frozenset({"string"})


def test_declared_serialization_encoding_must_be_reported() -> None:
    assert evaluate_assertion(
        AssertionSerialization("<a/>", method="xml"),
        result=ResultSnapshot(serialized="<a/>", serialization_method="xml"),
    )
    assert evaluate_assertion(
        AssertionSerialization("<a/>", method="html"),
        result=ResultSnapshot(serialized="<a/>", serialization_method="xml"),
    )
    assert evaluate_assertion(
        AssertionSerialization("<a/>", method="html"),
        result=ResultSnapshot(serialized="<a/>"),
    )
    with pytest.raises(AssertionGap, match="encoding is unavailable"):
        evaluate_assertion(
            AssertionSerialization("<a/>", encoding="utf-8"),
            result=ResultSnapshot(serialized="<a/>"),
        )
    assert evaluate_assertion(
        AssertionSerialization("<a/>", encoding="utf-8"),
        result=ResultSnapshot(serialized="<a/>", serialization_encoding="utf-8"),
    )
    assert not evaluate_assertion(
        AssertionSerialization("<a/>", encoding="utf-16"),
        result=ResultSnapshot(serialized="<a/>", serialization_encoding="utf-8"),
    )


def test_assert_case_gaps_unreported_serialization_metadata() -> None:
    encoding_case = make_case(
        '<result><assert-serialization encoding="utf-8"><![CDATA[<a/>]]></assert-serialization></result>'
    )

    encoding_result = assert_case(encoding_case, ResultSnapshot(serialized="<a/>"))

    assert encoding_result.outcome is Outcome.ADAPTER_GAP
    assert "encoding is unavailable" in encoding_result.message


def test_context_root_for_document_element_text_and_attribute_nodes() -> None:
    document = build_node_tree(ET.ElementTree(ET.fromstring("<out a='1'>text</out>")))
    element = document.children[0]
    text = next(node for node in document.iter() if isinstance(node, TextNode))
    attribute = next(node for node in document.iter() if isinstance(node, AttributeNode))

    assert _context_root(document) is document
    assert _context_root(element) is document
    assert _context_root(text) is document
    assert _context_root(attribute) is document
    assert _context_root(TextNode("x", parent=document)) is document
    assert _context_root(TextNode("x")) is None
    assert _context_root(1) is None


def test_xml_assertion_ignores_insignificant_whitespace() -> None:
    """A pretty-printed expectation matches compact markup."""
    assertion = AssertionXML(expected="<out>\n  <e>true</e>\n  <f>false</f>\n</out>")
    result = ResultSnapshot(xml="<out><e>true</e><f>false</f></out>")
    assert assertion.evaluate_verdict(AssertionContext(result)).passed


def test_xpath_assertion_sees_result_comments_and_processing_instructions() -> None:
    """The assertion tree keeps comments and PIs with their surrounding text."""
    result = ResultSnapshot(xml="<out><e><!--note-->hello</e><f><?pi data?></f></out>")
    for expression in (
        "/out/e[comment()='note']='hello'",
        "/out/f[processing-instruction('pi')='data']",
    ):
        assertion = AssertionXPath(expression=expression)
        assert assertion.evaluate_verdict(AssertionContext(result)).passed


def test_xml_assertion_keeps_whitespace_significant_for_compact_expectations() -> None:
    """Whitespace normalization is scoped to a pretty-printed expectation."""
    assertion = AssertionXML(expected="<out><e>true</e></out>")
    result = ResultSnapshot(xml="<out>\n  <e>true</e>\n</out>")

    verdict = assertion.evaluate_verdict(AssertionContext(result))
    assert not verdict.passed


def test_xml_assertion_does_not_hide_actual_indentation() -> None:
    """A pretty expectation is normalized; actual extra text still differs."""
    assertion = AssertionXML(expected="<out>\n  <e>true</e>\n</out>")
    result = ResultSnapshot(xml="<out>\n  <e>true</e>\n  extra\n</out>")

    verdict = assertion.evaluate_verdict(AssertionContext(result))
    assert not verdict.passed


def test_actual_error_codes_accept_clark_and_eqname_forms() -> None:
    """The engine's ``{uri}local`` and the catalog's ``Q{uri}local`` name one code."""
    from tests.xslt.assertions import (
        _display_code,
        _normalize_actual_code,
        _normalize_expected_code,
    )

    assert _normalize_actual_code("{urn:errors}notice") == _normalize_expected_code(
        "Q{urn:errors}notice", ()
    )
    assert _normalize_actual_code("Q{}UIOP9876") == _normalize_expected_code("Q{}UIOP9876", ())
    assert _normalize_actual_code("XTMM9000") == ("http://www.w3.org/2005/xqt-errors", "XTMM9000")
    assert _display_code(_normalize_actual_code("{urn:errors}notice")) == "Q{urn:errors}notice"
