"""Outcome classification and baseline diffing."""

from __future__ import annotations

from xsts.baseline import Baseline, Change, diff, dump, load
from xsts.outcomes import EngineResult, Outcome, classify


def test_classify_pass_and_fail() -> None:
    assert classify(True, True) is Outcome.PASS
    assert classify(False, False) is Outcome.PASS
    assert classify(True, False) is Outcome.FAIL
    assert classify(False, True) is Outcome.FAIL


def test_classify_not_applicable_wins() -> None:
    assert classify(True, True, applicable=False) is Outcome.NOT_APPLICABLE


def test_classify_uncheckable_expectation() -> None:
    assert classify(None, True) is Outcome.NOT_CHECKABLE


def test_classify_missing_verdict_is_an_error() -> None:
    assert classify(True, None) is Outcome.ERROR


def test_engine_level_conditions_short_circuit() -> None:
    assert classify(True, None, engine=EngineResult(adapter_gap="x")) is Outcome.ADAPTER_GAP
    assert classify(True, None, engine=EngineResult(timeout=True)) is Outcome.TIMEOUT
    assert classify(True, None, engine=EngineResult(error="boom")) is Outcome.ERROR


def test_baseline_round_trip(tmp_path) -> None:
    path = tmp_path / "baseline.toml"
    results = {"pyxsd:A/g/s:schema": "pass", "pyxsd:A/g/i:instance": "fail"}
    path.write_text(dump(results, {"profile": "xsd11"}))
    loaded = load(path)

    assert loaded.meta["profile"] == "xsd11"
    assert loaded.results == results


def test_baseline_diff_classifies_changes() -> None:
    baseline = Baseline(results={"a": "pass", "b": "fail", "c": "pass"})
    result = diff(baseline, {"a": "fail", "b": "pass", "d": "pass"})

    kinds = {change.key: change.kind for change in result.changes}
    assert kinds == {"a": "regression", "b": "improvement", "c": "removed", "d": "new"}
    assert result.regressions == (Change("a", "pass", "fail"),)
    assert result.improvements == (Change("b", "fail", "pass"),)


def test_missing_baseline_is_empty(tmp_path) -> None:
    loaded = load(tmp_path / "nope.toml")
    assert loaded.results == {}
    assert not diff(loaded, {})
