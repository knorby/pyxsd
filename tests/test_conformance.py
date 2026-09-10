"""Conformance corpus runner.

Every case in ``tests/conformance/cases.toml`` must pass: valid cases
parse clean, invalid cases report exactly the expected diagnostic codes.
"""

import pytest

from conformance_runner import load_cases, run_case

ALL_CASES = load_cases()


def test_corpus_has_cases():
    """The corpus itself is intact: unique ids, known areas."""
    ids = [case["id"] for case in ALL_CASES]
    assert len(ids) >= 40
    assert len(ids) == len(set(ids))
    for case in ALL_CASES:
        assert "/" in case["id"], case["id"]


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case["id"])
def test_conformance_case(case, tmp_path):
    """Run one corpus case through the real PyXSD pipeline."""
    passed, detail = run_case(case, tmp_path)
    assert passed, detail
