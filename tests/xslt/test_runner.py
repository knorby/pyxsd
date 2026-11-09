"""Execution glue: selection, explicit outcomes, and corpus disputes.

The executor is deliberately dumb: it returns result snapshots or raises an
execution error.  It never decides whether its own result passed.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from tests.xslt.assertions import ResultSnapshot
from tests.xslt.model import Dependency, Environment, Invocation
from tests.xslt.model import TestCase as XmlTestCase
from tests.xslt.outcomes import Outcome
from tests.xslt.runner import (
    CaseExecutionError,
    run_cases,
    unsatisfiable_dependencies,
)
from tests.xslt.selection import XSLT30_NON_STREAMING


def make_case(
    name: str,
    result_xml: str = "<result><assert>true()</assert></result>",
    *,
    dependencies: tuple[Dependency, ...] = (),
) -> XmlTestCase:
    test_set, local_name = name.split("/", 1)
    return XmlTestCase(
        name=name,
        local_name=local_name,
        test_set=test_set,
        description="",
        source=PurePosixPath(test_set) / f"_{local_name}-test-set.xml",
        environment=Environment(),
        invocation=Invocation(),
        dependencies=dependencies,
        result_xml=result_xml,
    )


def test_run_cases_executes_only_applicable_cases() -> None:
    applicable = make_case("set/a")
    streaming = make_case("set/b", dependencies=(Dependency("feature", "streaming"),))
    unsupported = make_case("set/c", dependencies=(Dependency("feature", "XML_1.1"),))
    seen: list[str] = []

    def executor(case: XmlTestCase) -> ResultSnapshot:
        seen.append(case.name)
        return ResultSnapshot(xml="<out/>")

    results = run_cases((applicable, streaming, unsupported), executor, XSLT30_NON_STREAMING)

    assert [result.outcome for result in results] == [
        Outcome.PASS,
        Outcome.NOT_APPLICABLE,
        Outcome.UNSUPPORTED_FEATURE,
    ]
    assert seen == ["set/a"]
    assert results[1].coverage == frozenset()
    assert results[2].coverage == frozenset()


def test_unsatisfiable_inherited_specs_are_corpus_disputes() -> None:
    case = make_case(
        "package-version/package-version-912a",
        dependencies=(Dependency("spec", "XSLT30+"), Dependency("spec", "XSLT20")),
    )
    called = False

    def executor(_: XmlTestCase) -> ResultSnapshot:
        nonlocal called
        called = True
        return ResultSnapshot(xml="<out/>")

    assert unsatisfiable_dependencies(case) is not None
    result = run_cases((case,), executor, XSLT30_NON_STREAMING)[0]

    assert result.outcome is Outcome.CORPUS_DISPUTE
    assert not called
    assert "XSLT30+" in result.message and "XSLT20" in result.message


def test_satisfiable_and_negative_dependency_combinations_are_not_disputes() -> None:
    satisfiable = make_case(
        "set/a", dependencies=(Dependency("spec", "XSLT30+"), Dependency("spec", "XSLT20+"))
    )
    negative = make_case(
        "set/b",
        dependencies=(Dependency("spec", "XSLT30"), Dependency("spec", "XSLT20", satisfied=False)),
    )
    contradiction = make_case(
        "set/c",
        dependencies=(Dependency("spec", "XSLT30"), Dependency("spec", "XSLT30", satisfied=False)),
    )

    assert unsatisfiable_dependencies(satisfiable) is None
    assert unsatisfiable_dependencies(negative) is None
    assert unsatisfiable_dependencies(contradiction) is not None


def test_feature_contradictions_are_corpus_disputes() -> None:
    case = make_case(
        "set/a",
        dependencies=(
            Dependency("feature", "higher_order_functions"),
            Dependency("feature", "higher_order_functions", satisfied=False),
        ),
    )

    assert unsatisfiable_dependencies(case) is not None
    result = run_cases((case,), lambda _: ResultSnapshot(xml="<out/>"), XSLT30_NON_STREAMING)[0]
    assert result.outcome is Outcome.CORPUS_DISPUTE
    assert "higher_order_functions" in result.message


def test_unknown_spec_tokens_do_not_create_disputes() -> None:
    case = make_case(
        "set/a",
        dependencies=(Dependency("spec", "XSLT30"), Dependency("spec", "future-version")),
    )

    assert unsatisfiable_dependencies(case) is None


def test_executor_error_is_compared_by_the_harness() -> None:
    expected = make_case("set/a", '<result><error code="XTDE0040"/></result>')
    unexpected = make_case("set/b")

    def executor(case: XmlTestCase) -> ResultSnapshot:
        if case.name == "set/a":
            raise CaseExecutionError("XTDE0040", "boom")
        return ResultSnapshot(error_code="XTDE0041")

    results = run_cases((expected, unexpected), executor, XSLT30_NON_STREAMING)

    assert results[0].outcome is Outcome.PASS
    assert results[0].actual_error_code == "XTDE0040"
    assert results[1].outcome is Outcome.FAIL
    assert "unexpected error" in results[1].message


def test_executor_timeout_is_its_own_outcome() -> None:
    def executor(_: XmlTestCase) -> ResultSnapshot:
        raise TimeoutError("budget exhausted")

    result = run_cases((make_case("set/a"),), executor, XSLT30_NON_STREAMING)[0]

    assert result.outcome is Outcome.TIMEOUT
    assert result.message == "budget exhausted"
    assert result.coverage == frozenset()


def test_unexpected_executor_exception_propagates() -> None:
    def executor(_: XmlTestCase) -> Any:
        raise RuntimeError("harness defect")

    with pytest.raises(RuntimeError, match="harness defect"):
        run_cases((make_case("set/a"),), executor, XSLT30_NON_STREAMING)


def test_corpus_order_is_preserved() -> None:
    cases = (make_case("set/z"), make_case("set/a"), make_case("set/m"))

    results = run_cases(cases, lambda _: ResultSnapshot(xml="<out/>"), XSLT30_NON_STREAMING)

    assert [result.name for result in results] == ["set/z", "set/a", "set/m"]


def test_adapter_gaps_are_reported_with_only_executed_coverage() -> None:
    case = make_case(
        "set/a",
        "<result><all-of><assert-xml><![CDATA[<out/>]]></assert-xml><assert-posture-and-sweep/></all-of></result>",
    )

    result = run_cases((case,), lambda _: ResultSnapshot(xml="<out/>"), XSLT30_NON_STREAMING)[0]

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.coverage == frozenset({"xml"})


def test_case_execution_error_validates_and_defaults_its_message() -> None:
    with pytest.raises(ValueError, match="execution error code"):
        CaseExecutionError("  ")
    error = CaseExecutionError("XTDE0040")

    assert error.code == "XTDE0040"
    assert str(error) == "XTDE0040"
    assert error.message == ""


def test_spec_token_matching_edges() -> None:
    from tests.xslt.runner import _token_matches

    assert _token_matches("XSLT30", (3, 0))
    assert not _token_matches("XSLT30", (3, 1))
    assert _token_matches("XSLT30+", (3, 1))
    assert not _token_matches("future-version", (3, 0))


def test_spec_candidate_scan_ignores_non_spec_and_empty_dependencies() -> None:
    mixed = make_case(
        "set/a",
        dependencies=(
            Dependency("feature", "higher_order_functions"),
            Dependency("spec", "XSLT30"),
            Dependency("spec", ""),
        ),
    )
    unknown_only = make_case("set/b", dependencies=(Dependency("spec", "future-version"),))

    assert unsatisfiable_dependencies(mixed) is None
    assert unsatisfiable_dependencies(unknown_only) is None


def test_unsupported_schema_version_is_reported_in_the_detail() -> None:
    from dataclasses import replace

    from tests.xslt.model import Environment, Schema

    case = replace(
        make_case("set/a"),
        environment=Environment(
            schemas=(
                Schema(role="schema", xsd_version="1.0"),
                Schema(role="schema", xsd_version="2.0"),
            )
        ),
    )

    result = run_cases((case,), lambda _: ResultSnapshot(xml="<out/>"), XSLT30_NON_STREAMING)[0]

    assert result.outcome is Outcome.UNSUPPORTED_FEATURE
    assert "unsupported feature: XSD version 2.0" in result.message


def test_inapplicable_detail_falls_back_for_supported_cases() -> None:
    from tests.xslt.runner import _inapplicable_detail

    assert _inapplicable_detail(make_case("set/a"), XSLT30_NON_STREAMING) == "optional feature"


CORPUS = Path(__file__).parent / "corpus"


@pytest.mark.skipif(not (CORPUS / "catalog.xml").is_file(), reason="XSLT corpus not checked out")
def test_real_corpus_surfaces_only_the_known_dependency_disputes() -> None:
    from tests.xslt.catalog import load_catalog

    catalog = load_catalog(CORPUS / "catalog.xml")

    disputes = [case.name for case in catalog.cases.values() if unsatisfiable_dependencies(case)]

    assert disputes == [
        "package-version/package-version-912a",
        "package-version/package-version-912b",
    ]
