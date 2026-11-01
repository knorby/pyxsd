"""JSON run reports: aggregation fields, failure directions, and provenance."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from xsts.baseline import Baseline, diff
from xsts.outcomes import Outcome
from xsts.report import EngineSummary, Summary, render_json, results_to_keys, summarize
from xsts.runner import CaseResult


def _result(
    test_id: str,
    outcome: Outcome,
    *,
    kind: str = "instance",
    expected: str | None = "valid",
    actual: bool | None = None,
    engine: str = "pyxsd",
    contributor: str = "W3C",
) -> CaseResult:
    set_name, group_name, _case_name = test_id.split("/")
    return CaseResult(
        test_id=test_id,
        set_name=set_name,
        contributor=contributor,
        group_name=group_name,
        kind=kind,
        engine=engine,
        outcome=outcome,
        expected=expected,
        actual=actual,
    )


def _sample_results() -> list[CaseResult]:
    """One of every verdict shape the report has to distinguish."""
    return [
        _result(
            "aset/group/schema-case",
            Outcome.PASS,
            kind="schema",
            expected="valid",
            actual=True,
        ),
        _result("bset/group/accept", Outcome.FAIL, expected="invalid", actual=True),
        _result("iset/group/case", Outcome.FAIL, expected="valid", actual=False),
        _result("gset/group/gap", Outcome.ADAPTER_GAP, expected="valid"),
        _result("tset/group/slow", Outcome.TIMEOUT, kind="schema", expected="valid"),
    ]


def test_render_json_aggregates_results_and_directions() -> None:
    summary = summarize(_sample_results())

    payload = json.loads(render_json(summary, provenance={"profile": "xsd11"}))
    engine = payload["engines"]["pyxsd"]

    assert payload["provenance"] == {"profile": "xsd11"}
    assert engine["by_kind"]["schema"]["pass"] == 1
    assert engine["failure_directions"] == {"false-accept": 1, "false-reject": 1}
    assert engine["results"]["pyxsd:iset/group/case:instance"] == "fail"
    assert engine["checked"] == 3
    assert engine["total"] == 5


def test_render_json_preserves_provenance_values_verbatim() -> None:
    provenance = {"profile": "xsd11", "case_count": 5, "suites": ["main"]}

    payload = json.loads(render_json(summarize(_sample_results()), provenance=provenance))

    assert payload["provenance"] == provenance


def test_render_json_without_provenance_keeps_existing_fields() -> None:
    results = _sample_results()
    baseline = results_to_keys(results) | {"pyxsd:iset/group/case:instance": "pass"}
    comparison = diff(Baseline(results=baseline), results_to_keys(results))
    summary = summarize(results, diff=comparison)

    payload = json.loads(render_json(summary))
    engine = payload["engines"]["pyxsd"]

    assert "provenance" not in payload
    assert payload["baseline_changes"] == [
        {
            "key": "pyxsd:iset/group/case:instance",
            "previous": "pass",
            "current": "fail",
            "kind": "regression",
        }
    ]
    # Every observed outcome is emitted; the full map uses baseline keys.
    assert engine["outcomes"] == {
        "pass": 1,
        "fail": 2,
        "adapter-gap": 1,
        "timeout": 1,
    }
    assert engine["results"] == results_to_keys(results)
    assert [failure["test_id"] for failure in engine["failures"]] == [
        "bset/group/accept",
        "iset/group/case",
    ]
    assert [set(failure) for failure in engine["failures"]] == [
        {"test_id", "kind", "expected", "actual", "detail"},
        {"test_id", "kind", "expected", "actual", "detail"},
    ]
    # Timeouts and adapter gaps are outcomes, never checked verdicts.
    assert engine["checked"] == 3
    assert engine["total"] == 5


def test_failure_directions_ignore_non_failures() -> None:
    summary = summarize(
        [
            _result("aset/group/na", Outcome.NOT_APPLICABLE, expected="invalid", actual=True),
            _result("bset/group/nc", Outcome.NOT_CHECKABLE, expected="valid", actual=False),
            _result("cset/group/gap", Outcome.ADAPTER_GAP, expected="invalid", actual=True),
            _result("dset/group/slow", Outcome.TIMEOUT, expected="valid", actual=False),
            _result("eset/group/broken", Outcome.ERROR, expected="valid", actual=False),
            _result("fset/group/meta", Outcome.METADATA_ERROR, expected="invalid", actual=True),
        ]
    )

    assert summary.engines["pyxsd"].failure_directions() == Counter()


def test_failure_directions_use_only_checkable_corpus_metadata() -> None:
    summary = summarize(
        [
            # A latent-invalid expectation is still a Boolean invalid verdict.
            _result("aset/group/latent", Outcome.FAIL, expected="invalid-latent", actual=True),
            # Legacy or unreadable metadata cannot be forced into a direction.
            _result("bset/group/unknown", Outcome.FAIL, expected="notKnown", actual=True),
            _result("cset/group/missing-expected", Outcome.FAIL, expected=None, actual=False),
            _result("dset/group/missing-actual", Outcome.FAIL, expected="valid", actual=None),
            # Inconsistent hand-built failures do not invent a direction.
            _result("eset/group/agreeing", Outcome.FAIL, expected="invalid", actual=False),
            _result("fset/group/agreeing-valid", Outcome.FAIL, expected="valid", actual=True),
            _result("gset/group/reject", Outcome.FAIL, expected="valid", actual=False),
        ]
    )

    assert summary.engines["pyxsd"].failure_directions() == Counter(
        {"false-accept": 1, "false-reject": 1}
    )


def test_render_json_rejects_malformed_provenance() -> None:
    summary = summarize(_sample_results())

    with pytest.raises(TypeError, match="provenance"):
        render_json(summary, provenance=[("profile", "xsd11")])  # type: ignore[arg-type]


def test_render_json_orders_engines_and_outcomes_stably() -> None:
    results = [
        _result(
            "bset/group/fail",
            Outcome.FAIL,
            expected="invalid",
            actual=True,
            contributor="Zed",
        ),
        _result(
            "aset/group/pass",
            Outcome.PASS,
            kind="schema",
            expected="valid",
            actual=True,
            contributor="Alpha",
        ),
        _result("zset/group/late", Outcome.TIMEOUT, engine="xmlschema", expected="valid"),
    ]

    payload = json.loads(render_json(summarize(results)))
    pyxsd = payload["engines"]["pyxsd"]

    assert list(payload["engines"]) == ["pyxsd", "xmlschema"]
    assert list(pyxsd["outcomes"]) == ["pass", "fail"]
    assert list(pyxsd["by_kind"]) == ["instance", "schema"]
    assert list(pyxsd["by_contributor"]) == ["Alpha", "Zed"]
    assert list(pyxsd["results"]) == [
        "pyxsd:aset/group/pass:schema",
        "pyxsd:bset/group/fail:instance",
    ]
    assert payload["engines"]["xmlschema"]["results"] == {
        "xmlschema:zset/group/late:instance": "timeout"
    }


def test_summarize_retains_a_snapshot_of_results() -> None:
    results = _sample_results()

    summary = summarize(results)
    results.clear()

    assert len(summary.results) == 5
    assert summary.results[0].test_id == "aset/group/schema-case"


def test_render_json_tolerates_a_summary_without_retained_results() -> None:
    # Old callers may construct a Summary directly; rendering still works and
    # simply has no full result map to publish.
    summary = Summary(
        engines={"pyxsd": EngineSummary(engine="pyxsd", outcomes=Counter({"pass": 1}))}
    )

    engine = json.loads(render_json(summary))["engines"]["pyxsd"]

    assert engine["results"] == {}
    assert engine["checked"] == 1
