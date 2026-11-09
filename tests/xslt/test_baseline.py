"""Deterministic baselines and reports keyed by corpus, profile, and case."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from tests.xslt.baseline import (
    Baseline,
    BaselineChange,
    build_baseline,
    diff_baseline,
    load_baseline,
    write_baseline,
)
from tests.xslt.outcomes import CaseResult, Outcome
from tests.xslt.report import build_report, render_json, render_toml, write_report


def test_pass_regression_and_improvement_are_explicit() -> None:
    changes = diff_baseline({"a": "pass", "b": "fail"}, {"a": "fail", "b": "pass"})

    assert [(c.kind, c.key) for c in changes] == [("regression", "a"), ("improvement", "b")]


def test_other_baseline_transitions_are_named() -> None:
    changes = diff_baseline(
        {"removed": "pass", "changed": "fail", "added": "pass"},
        {"changed": "adapter-gap", "added": "pass", "new": "fail"},
    )

    assert [(c.kind, c.key) for c in changes] == [
        ("changed", "changed"),
        ("added", "new"),
        ("removed", "removed"),
    ]
    assert changes[0].previous == "fail"
    assert changes[0].current == "adapter-gap"
    assert changes[1].previous is None
    assert changes[2].current is None


def test_identical_baselines_produce_no_changes() -> None:
    baseline = {"a": "pass", "b": "fail"}

    assert diff_baseline(baseline, dict(baseline)) == ()


def test_baseline_round_trips_through_deterministic_toml(tmp_path: Path) -> None:
    results = (
        CaseResult.passed("z/case", coverage={"xpath"}),
        CaseResult.failed("a/case", message="different"),
        CaseResult.expected_error("XTDE0040", "XTDE0041", name="m/case"),
    )
    baseline = build_baseline(results, corpus="abc123", profile="xslt30")
    path = tmp_path / "xslt30.toml"

    write_baseline(path, baseline)
    first = path.read_text(encoding="utf-8")
    loaded = load_baseline(path)
    write_baseline(path, loaded)
    second = path.read_text(encoding="utf-8")

    assert first == second
    assert loaded.meta["corpus"] == "abc123"
    assert loaded.meta["profile"] == "xslt30"
    assert loaded.cases == {
        "a/case": "fail",
        "m/case": "wrong-error-code",
        "z/case": "pass",
    }
    assert list(loaded.cases) == ["a/case", "m/case", "z/case"]
    assert tomllib.loads(first)["meta"]["corpus"] == "abc123"


def test_load_baseline_accepts_a_profile_name(tmp_path: Path) -> None:
    path = tmp_path / "xslt20.toml"
    write_baseline(
        path,
        Baseline(meta={"corpus": "rev", "profile": "xslt20"}, cases={"a": "pass"}),
    )

    loaded = load_baseline("xslt20", directory=tmp_path)

    assert loaded.meta["profile"] == "xslt20"
    assert loaded.cases == {"a": "pass"}


def test_diff_baseline_accepts_baseline_objects() -> None:
    previous = Baseline(meta={}, cases={"a": "pass"})
    current = Baseline(meta={}, cases={"a": "fail"})

    changes = diff_baseline(previous, current)

    assert changes == (BaselineChange("regression", "a", "pass", "fail"),)


def test_report_is_deterministic_and_keyed_by_corpus_profile_and_case() -> None:
    results = (
        CaseResult.passed("set/a", coverage={"xml", "xpath"}),
        CaseResult.failed("set/b", message="mismatch", coverage={"xml"}),
    )

    first = build_report(results, corpus="deadbeef", profile="xslt30")
    second = build_report(tuple(reversed(results)), corpus="deadbeef", profile="xslt30")

    assert first.to_dict() == second.to_dict()
    assert first.to_dict()["corpus"] == "deadbeef"
    assert first.to_dict()["profile"] == "xslt30"
    assert first.to_dict()["cases"]["set/a"]["outcome"] == "pass"
    assert first.to_dict()["cases"]["set/a"]["coverage"] == ["xml", "xpath"]
    assert first.to_dict()["cases"]["set/b"]["message"] == "mismatch"
    assert first.to_dict()["summary"] == {"fail": 1, "pass": 1}
    assert "created" not in render_json(first)
    assert "timestamp" not in render_json(first)


def test_json_and_toml_reports_round_trip(tmp_path: Path) -> None:
    report = build_report(
        (
            CaseResult.expected_error("XTDE0040", "XTDE0041", name="set/case"),
            CaseResult.adapter_gap("set/gap", detail="serializer absent", coverage={"xml"}),
        ),
        corpus="abc",
        profile="xslt30",
    )
    json_path = tmp_path / "report.json"
    toml_path = tmp_path / "report.toml"

    write_report(json_path, report, format="json")
    write_report(toml_path, report, format="toml")

    document = json.loads(json_path.read_text(encoding="utf-8"))
    parsed = tomllib.loads(toml_path.read_text(encoding="utf-8"))

    assert document["cases"]["set/case"]["expected_error_code"] == "XTDE0040"
    assert document["cases"]["set/case"]["actual_error_code"] == "XTDE0041"
    assert parsed["cases"]["set/case"]["outcome"] == "wrong-error-code"
    assert parsed["cases"]["set/case"]["coverage"] == ["expected-error"]
    assert parsed["cases"]["set/gap"]["message"] == "serializer absent"
    assert parsed["summary"] == {"adapter-gap": 1, "wrong-error-code": 1}
    assert render_json(report) == json_path.read_text(encoding="utf-8")
    assert render_toml(report) == toml_path.read_text(encoding="utf-8")


def test_report_omits_absent_error_fields() -> None:
    report = build_report((CaseResult.passed("set/a"),), corpus="abc", profile="xslt30")
    document = json.loads(render_json(report))

    assert document["cases"]["set/a"] == {
        "outcome": "pass",
        "coverage": [],
        "message": "",
    }
    assert "expected_error_code" not in document["cases"]["set/a"]


def test_report_rejects_unknown_outcome_strings() -> None:
    report = build_report((CaseResult(Outcome.PASS, name="set/a"),), corpus="abc", profile="xslt30")

    assert report.to_dict()["cases"]["set/a"]["outcome"] == "pass"


def test_baseline_accessors_and_extra_metadata(tmp_path: Path) -> None:
    baseline = Baseline(
        meta={"schema": 1, "corpus": "rev", "profile": "xslt30", "verified": True},
        cases={"b": "pass", "a": "fail"},
    )
    path = tmp_path / "baseline.toml"

    write_baseline(path, baseline)

    assert baseline.as_mapping() == {"a": "fail", "b": "pass"}
    assert baseline.outcome("a") == "fail"
    assert baseline.outcome("missing") is None
    assert "verified = true" in path.read_text(encoding="utf-8")
    assert tomllib.loads(path.read_text(encoding="utf-8"))["meta"]["verified"] is True


def test_baseline_rejects_unsupported_metadata_values() -> None:
    baseline = Baseline(
        meta={"schema": 1, "corpus": "rev", "profile": "xslt30", "bad": []},
        cases={},
    )

    import pytest
    from tests.xslt.baseline import render_baseline

    with pytest.raises(TypeError, match="unsupported TOML value"):
        render_baseline(baseline)


def test_build_baseline_rejects_unnamed_and_duplicate_results() -> None:
    import pytest

    with pytest.raises(ValueError, match="named case"):
        build_baseline((CaseResult.passed(""),), corpus="rev", profile="xslt30")
    with pytest.raises(ValueError, match="duplicate case"):
        build_baseline(
            (CaseResult.passed("a"), CaseResult.failed("a")),
            corpus="rev",
            profile="xslt30",
        )


def test_load_baseline_rejects_incomplete_documents(tmp_path: Path) -> None:
    import pytest

    missing_meta = tmp_path / "missing-meta.toml"
    missing_meta.write_text("[cases]\na = 'pass'\n", encoding="utf-8")
    bad_value = tmp_path / "bad-value.toml"
    bad_value.write_text("[meta]\n[corpus]\nprofile = 'x'\n[cases]\na = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="requires"):
        load_baseline(missing_meta)
    with pytest.raises(ValueError, match="outcomes must be strings"):
        load_baseline(bad_value)


def test_report_rejects_unnamed_duplicate_cases_and_unknown_formats(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="named case"):
        build_report((CaseResult.passed(""),), corpus="rev", profile="xslt30")
    with pytest.raises(ValueError, match="duplicate case"):
        build_report(
            (CaseResult.passed("a"), CaseResult.failed("a")),
            corpus="rev",
            profile="xslt30",
        )
    report = build_report((CaseResult.passed("a"),), corpus="rev", profile="xslt30")
    with pytest.raises(ValueError, match="unknown report format"):
        write_report(tmp_path / "report.xml", report, format="yaml")
