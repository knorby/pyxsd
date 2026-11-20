"""Explicit, mutually exclusive outcomes for one catalog case.

The strict expected-error comparison is the sharp edge: a different error code
is its own outcome, never a pass, and an expected error that never occurs is a
failure.  Assertion-family coverage records only families that actually ran.
"""

from __future__ import annotations

import pytest
from tests.xslt.outcomes import CaseResult, Outcome


def test_wrong_error_is_not_a_strict_pass() -> None:
    result = CaseResult.expected_error("XTDE0420", actual="XTDE0040")

    assert result.outcome is Outcome.WRONG_ERROR_CODE
    assert result.expected_error_code == "XTDE0420"
    assert result.actual_error_code == "XTDE0040"


def test_matching_error_code_is_a_strict_pass() -> None:
    result = CaseResult.expected_error("XTDE0420", actual="XTDE0420")

    assert result.outcome is Outcome.PASS
    assert result.expected_error_code == "XTDE0420"
    assert result.actual_error_code == "XTDE0420"


def test_any_error_code_is_accepted_only_when_an_error_happens() -> None:
    occurred = CaseResult.expected_error("*", actual="XTDE0040")
    missing = CaseResult.expected_error("*")

    assert occurred.outcome is Outcome.PASS
    assert occurred.expected_error_code == "*"
    assert missing.outcome is Outcome.FAIL
    assert missing.actual_error_code is None


def test_expected_error_that_does_not_happen_is_a_failure() -> None:
    result = CaseResult.expected_error("XTDE0420")

    assert result.outcome is Outcome.FAIL
    assert result.expected_error_code == "XTDE0420"
    assert result.actual_error_code is None
    assert "XTDE0420" in result.message


def test_error_codes_are_stripped() -> None:
    result = CaseResult.expected_error(" XTDE0420 ", actual=" XTDE0420 ")

    assert result.outcome is Outcome.PASS
    assert result.expected_error_code == "XTDE0420"
    assert result.actual_error_code == "XTDE0420"


def test_an_expected_error_code_is_required() -> None:
    with pytest.raises(ValueError, match="expected error code"):
        CaseResult.expected_error("  ")


def test_an_actual_error_code_cannot_be_empty() -> None:
    with pytest.raises(ValueError, match="actual error code"):
        CaseResult.expected_error("XTDE0420", actual=" ")


def test_expected_error_records_the_executed_assertion_family() -> None:
    result = CaseResult.expected_error("XTDE0420", actual="XTDE0040", coverage=["assert-message"])

    assert result.coverage == frozenset({"expected-error", "assert-message"})


def test_the_outcome_taxonomy_is_distinct() -> None:
    assert {outcome.value for outcome in Outcome} == {
        "pass",
        "wrong-error-code",
        "fail",
        "not-applicable",
        "unsupported-feature",
        "adapter-gap",
        "timeout",
        "corpus-dispute",
    }
    assert len(Outcome) == 8


def test_pass_and_failure_record_executed_assertions() -> None:
    passed = CaseResult.passed("case", coverage={"assert-xml"})
    failed = CaseResult.failed("case", message="different tree", coverage={"assert-xml"})

    assert passed.outcome is Outcome.PASS
    assert passed.coverage == frozenset({"assert-xml"})
    assert failed.outcome is Outcome.FAIL
    assert failed.message == "different tree"
    assert failed.coverage == frozenset({"assert-xml"})


def test_not_applicable_and_unsupported_feature_have_no_coverage() -> None:
    not_applicable = CaseResult.not_applicable("case", reason="requires streaming")
    unsupported = CaseResult.unsupported_feature("case", feature="XML_1.1")

    assert not_applicable.outcome is Outcome.NOT_APPLICABLE
    assert not_applicable.message == "requires streaming"
    assert not_applicable.coverage == frozenset()
    assert unsupported.outcome is Outcome.UNSUPPORTED_FEATURE
    assert unsupported.message == "unsupported feature: XML_1.1"
    assert unsupported.coverage == frozenset()


def test_timeout_has_no_coverage() -> None:
    result = CaseResult.timeout("case", detail="10s budget exhausted")

    assert result.outcome is Outcome.TIMEOUT
    assert result.message == "10s budget exhausted"
    assert result.coverage == frozenset()


def test_adapter_gap_records_only_executed_families() -> None:
    result = CaseResult.adapter_gap(
        "case", detail="serialization evaluator absent", coverage={"assert-xml"}
    )

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.coverage == frozenset({"assert-xml"})


def test_corpus_dispute_is_its_own_outcome() -> None:
    result = CaseResult.corpus_dispute(
        "case", detail="upstream expectation is inconsistent", coverage={"assert"}
    )

    assert result.outcome is Outcome.CORPUS_DISPUTE
    assert result.coverage == frozenset({"assert"})


@pytest.mark.parametrize(
    "outcome", [Outcome.NOT_APPLICABLE, Outcome.UNSUPPORTED_FEATURE, Outcome.TIMEOUT]
)
def test_unexecuted_outcomes_reject_coverage(outcome: Outcome) -> None:
    with pytest.raises(ValueError, match="cannot record assertion coverage"):
        CaseResult(outcome, coverage=frozenset({"assert"}))


@pytest.mark.parametrize(
    ("expected", "actual"),
    [(None, "XTDE0040"), ("XTDE0420", None), ("XTDE0420", "XTDE0420")],
)
def test_wrong_error_result_requires_distinct_codes(
    expected: str | None, actual: str | None
) -> None:
    with pytest.raises(ValueError, match="wrong-error-code"):
        CaseResult(
            Outcome.WRONG_ERROR_CODE,
            expected_error_code=expected,
            actual_error_code=actual,
        )


def test_a_passing_expected_error_requires_the_actual_code() -> None:
    with pytest.raises(ValueError, match="actual code"):
        CaseResult(Outcome.PASS, expected_error_code="XTDE0420")


def test_direct_construction_can_represent_an_unexpected_failure() -> None:
    result = CaseResult(Outcome.FAIL, name="case", message="unexpected error", coverage=["assert"])

    assert result.name == "case"
    assert result.expected_error_code is None
    assert result.actual_error_code is None
    assert result.coverage == frozenset({"assert"})
