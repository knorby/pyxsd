"""Contract for the deterministic XSLT conformance census.

The census is measurement output: outcome totals for every bucket, one
normalized signature per case, and signature groups that make failure families
visible.  These checks use synthetic results only -- the corpus is never run
here -- and pin ordering, normalization, byte stability, and the CLI's refusal
to combine census mode with a baseline mode.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tests import report_xslt
from tests.xslt import baseline as baseline_module
from tests.xslt import census
from tests.xslt.catalog import load_catalog
from tests.xslt.outcomes import CaseResult, Outcome

REPO_ROOT = Path(__file__).resolve().parents[2]

GATE_CATALOG = """\
<?xml version="1.0" encoding="UTF-8"?>
<catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog">
  <test-set name="gate" file="gate/_gate-test-set.xml"/>
</catalog>
"""

GATE_TEST_SET = """\
<?xml version="1.0" encoding="UTF-8"?>
<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog" name="gate">
  <environment name="gate-env">
    <source role="."><content><![CDATA[<doc/>]]></content></source>
  </environment>
  <test-case name="case-001">
    <environment ref="gate-env"/>
    <test><stylesheet file="case-001.xsl"/></test>
    <result><assert-xml><![CDATA[<out>right</out>]]></assert-xml></result>
  </test-case>
</test-set>
"""

SHEET = """\
<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:template match="/"><out>wrong</out></xsl:template>
</xsl:stylesheet>
"""


@pytest.fixture(autouse=True)
def _no_ambient_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every fixture's budget hermetic: the test sets it, never the shell."""
    monkeypatch.delenv("PYXSD_XSLT_TIMEOUT", raising=False)


def _fixture_repo(tmp_path: Path) -> Path:
    """A synthetic repo with one case, its stylesheet, and no baseline."""
    root = tmp_path / "repo"
    corpus = root / "tests" / "xslt" / "corpus"
    gate = corpus / "gate"
    gate.mkdir(parents=True)
    (corpus / "catalog.xml").write_text(GATE_CATALOG, encoding="utf-8")
    (gate / "_gate-test-set.xml").write_text(GATE_TEST_SET, encoding="utf-8")
    (gate / "case-001.xsl").write_text(SHEET, encoding="utf-8")
    (root / "tests" / "xslt" / "baselines").mkdir(parents=True)
    return root


def _catalog(root: Path):
    return load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")


def _write_baseline(root: Path) -> Path:
    path = root / "tests" / "xslt" / "baselines" / "xslt30.toml"
    baseline_module.write_baseline(
        path,
        baseline_module.Baseline(
            meta={
                "schema": baseline_module.BASELINE_SCHEMA,
                "corpus": _catalog(root).corpus_revision,
                "profile": "xslt30",
                "timeout_seconds": report_xslt.DEFAULT_TIMEOUT,
            },
            cases={"gate/case-001": "fail"},
        ),
    )
    return path


def _mixed_results() -> tuple[CaseResult, ...]:
    """One result per outcome, deliberately out of case-name order."""
    return (
        CaseResult.not_applicable("h", reason="outside the declared feature profile"),
        CaseResult.expected_error("XTDE0420", actual="XTDE0040", name="g"),
        CaseResult.unsupported_feature("f", feature="maps"),
        CaseResult.corpus_dispute("e", detail="unsatisfiable inherited spec dependencies"),
        CaseResult.adapter_gap("d", detail="packages"),
        CaseResult.timeout("c", detail="the case exceeded its 5 s budget"),
        CaseResult.failed("b", message="ValueError: boom"),
        CaseResult.passed("a"),
    )


def _census(results: tuple[CaseResult, ...], *, timeout: float = 5.0) -> census.Census:
    return census.build_census(
        results,
        profile="xslt30",
        corpus="pinned",
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# Totals


def test_totals_cover_every_outcome_in_fixed_order() -> None:
    document = _census(_mixed_results()).to_dict()

    assert list(document["totals"]) == [outcome.value for outcome in census.CENSUS_OUTCOMES]
    assert set(document["totals"]) == {outcome.value for outcome in Outcome}
    assert set(document["totals"].values()) == {1}
    assert sum(document["totals"].values()) == len(document["cases"])


def test_totals_default_missing_outcomes_to_zero() -> None:
    document = _census((CaseResult.passed("only"),)).to_dict()

    assert document["totals"]["pass"] == 1
    assert {outcome: count for outcome, count in document["totals"].items() if count} == {"pass": 1}
    assert len(document["totals"]) == len(census.CENSUS_OUTCOMES)


def test_an_empty_census_has_zero_totals_and_no_groups() -> None:
    document = _census(()).to_dict()

    assert list(document["totals"].values()) == [0] * len(census.CENSUS_OUTCOMES)
    assert document["cases"] == []
    assert document["signatures"] == []


# ---------------------------------------------------------------------------
# Signature normalization


def test_failure_signature_keeps_the_first_line_without_ansi_or_controls() -> None:
    result = CaseResult.failed(
        "case",
        message="\x1b[31mValueError: boom\x1b[0m\there\r\nsecond line\nthird line",
    )

    assert census.failure_signature(result) == "ValueError: boomhere"


def test_failure_signature_strips_remaining_control_characters() -> None:
    result = CaseResult.failed("case", message="bad\x00\x07message")

    assert census.failure_signature(result) == "badmessage"


def test_failure_signature_is_truncated_to_the_limit() -> None:
    exact = "x" * census.SIGNATURE_LIMIT
    longer = "y" * (census.SIGNATURE_LIMIT + 50)

    assert census.failure_signature(CaseResult.failed("a", message=exact)) == exact
    assert census.failure_signature(CaseResult.failed("b", message=longer)) == "y" * 200


def test_failure_signature_handles_an_empty_message() -> None:
    assert census.failure_signature(CaseResult.failed("case")) == ""


def test_signatures_are_normalized_per_outcome() -> None:
    assert census.failure_signature(CaseResult.timeout("t", detail="budget")) == "timeout"
    assert (
        census.failure_signature(CaseResult.expected_error("XTDE0420", actual="XTDE0040", name="w"))
        == "expected XTDE0420 got XTDE0040"
    )
    assert census.failure_signature(CaseResult.adapter_gap("g", detail="packages")) == "packages"
    assert census.failure_signature(CaseResult.passed("p")) == ""
    assert census.failure_signature(CaseResult.not_applicable("n")) == ""
    assert (
        census.failure_signature(CaseResult.unsupported_feature("u", feature="maps"))
        == "unsupported feature: maps"
    )
    assert (
        census.failure_signature(CaseResult.corpus_dispute("d", detail="unsatisfiable"))
        == "unsatisfiable"
    )


# ---------------------------------------------------------------------------
# Aggregation


def test_signature_groups_sort_by_count_then_signature() -> None:
    results = (
        CaseResult.failed("mike", message="sig-c"),
        CaseResult.failed("alpha", message="sig-a"),
        CaseResult.failed("bravo", message="sig-b"),
        CaseResult.failed("delta", message="sig-a"),
        CaseResult.failed("echo", message="sig-b"),
        CaseResult.failed("charlie", message="sig-a"),
        CaseResult.passed("foxtrot"),
    )

    document = _census(results).to_dict()

    assert [(group["signature"], group["count"]) for group in document["signatures"]] == [
        ("sig-a", 3),
        ("sig-b", 2),
        ("", 1),
        ("sig-c", 1),
    ]
    assert document["signatures"][0]["samples"] == ["alpha", "charlie", "delta"]
    assert document["signatures"][0]["outcomes"] == {"fail": 3}
    assert document["signatures"][2]["outcomes"] == {"pass": 1}


def test_samples_are_sorted_and_capped_at_three() -> None:
    results = (
        CaseResult.failed("zeta", message="same"),
        CaseResult.failed("alpha", message="same"),
        CaseResult.failed("mike", message="same"),
        CaseResult.failed("bravo", message="same"),
    )

    group = _census(results).to_dict()["signatures"][0]

    assert group["count"] == 4
    assert group["samples"] == ["alpha", "bravo", "mike"]


def test_one_signature_group_can_mix_outcomes() -> None:
    results = (
        CaseResult.failed("f", message="same text"),
        CaseResult.adapter_gap("g", detail="same text"),
        CaseResult.failed("h", message="same text"),
    )

    group = _census(results).to_dict()["signatures"][0]

    assert group["signature"] == "same text"
    assert group["count"] == 3
    assert group["outcomes"] == {"fail": 2, "adapter-gap": 1}
    assert group["samples"] == ["f", "g", "h"]


# ---------------------------------------------------------------------------
# Document determinism


def test_case_records_sort_by_name_and_carry_their_message() -> None:
    document = _census(_mixed_results()).to_dict()

    assert [case["name"] for case in document["cases"]] == list("abcdefgh")
    assert document["cases"][1] == {
        "name": "b",
        "outcome": "fail",
        "signature": "ValueError: boom",
        "message": "ValueError: boom",
    }


def test_census_is_byte_stable_and_free_of_clock_or_path_values() -> None:
    first = census.render_json(_census(_mixed_results()))
    second = census.render_json(_census(_mixed_results()))

    assert first == second
    assert first.endswith("\n")
    assert json.loads(first) == _census(_mixed_results()).to_dict()
    assert re.search(r"\d{4}-\d{2}-\d{2}", first) is None
    assert re.search(r"\b\d{2}:\d{2}:\d{2}\b", first) is None
    assert str(REPO_ROOT) not in first


def test_messages_drop_the_checkout_prefix_and_keep_corpus_relative_text(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    message = f"PyXSDError: {root}/tests/xslt/corpus/x.xml: undefined entity"
    results = (CaseResult.failed("case", message=message),)

    without = census.build_census(
        results, profile="xslt10", corpus="c", timeout_seconds=5.0, root=root
    )
    kept = census.build_census(results, profile="xslt10", corpus="c", timeout_seconds=5.0)

    expected = "PyXSDError: tests/xslt/corpus/x.xml: undefined entity"
    assert without.to_dict()["cases"][0]["message"] == expected
    assert without.to_dict()["cases"][0]["signature"] == expected
    assert kept.to_dict()["cases"][0]["message"] == "PyXSDError: <path> undefined entity"
    assert str(root) not in census.render_json(without)


@pytest.mark.parametrize(
    "path",
    [
        "/Users/someone/repo/x.xml",
        "/home/someone/x.xml",
        "/opt/external/file.xml",
        "/tmp/x.xml",
        "/var/lib/x.xml",
        "/private/var/x.xml",
        "file:///opt/external/file.xml",
    ],
)
def test_absolute_filesystem_paths_become_a_placeholder(path: str) -> None:
    message = f"boom at {path}: detail"
    observed = census.build_census(
        (CaseResult.failed("case", message=message),),
        profile="xslt10",
        corpus="c",
        timeout_seconds=5.0,
    )

    case = observed.to_dict()["cases"][0]
    assert case["message"] == "boom at <path> detail"
    assert case["signature"] == "boom at <path> detail"
    rendered = census.render_json(observed)
    assert path not in rendered
    assert rendered == census.render_json(observed)


def test_xpath_assertions_are_not_treated_as_filesystem_paths() -> None:
    messages = (
        "assert expression \"/out='2'\" is false",
        "assert expression '/out' is false",
        "assert expression '/out/P/t1 = \"Intro Section.\"' is false",
    )

    for message in messages:
        case = _census((CaseResult.failed("case", message=message),)).to_dict()["cases"][0]
        assert case["message"] == message
        assert case["signature"] == message


def test_whole_second_budgets_render_as_integers_and_fractions_remain() -> None:
    assert _census((), timeout=5.0).to_dict()["timeout_seconds"] == 5
    assert _census((), timeout=7.5).to_dict()["timeout_seconds"] == 7.5


def test_census_rejects_nameless_and_duplicate_results() -> None:
    with pytest.raises(ValueError, match="named case results"):
        census.build_census(
            (CaseResult.passed(),), profile="xslt30", corpus="c", timeout_seconds=5.0
        )
    with pytest.raises(ValueError, match="duplicate case result"):
        census.build_census(
            (CaseResult.passed("a"), CaseResult.passed("a")),
            profile="xslt30",
            corpus="c",
            timeout_seconds=5.0,
        )


def test_write_census_creates_parents_and_is_stable(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "census.json"
    observed = _census(_mixed_results())

    census.write_census(target, observed)

    assert target.read_text(encoding="utf-8") == census.render_json(observed)


# ---------------------------------------------------------------------------
# CLI


@pytest.mark.parametrize("flag", ["--check", "--enforce", "--write-baseline"])
def test_census_refuses_every_baseline_mode(
    flag: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "census.json"
    argv = ["--profile", "xslt30", "--census", str(target), flag]
    if flag == "--write-baseline":
        argv.append(str(tmp_path / "baseline.toml"))

    assert report_xslt.run(argv, repo_root=tmp_path) == 2
    error = capsys.readouterr().err
    assert "--census cannot be combined with" in error
    assert flag in error
    assert not target.exists()


def test_census_executes_the_profile_and_writes_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_repo(tmp_path)
    target = tmp_path / "out" / "census.json"
    monkeypatch.setattr(report_xslt, "pinned_corpus", lambda repo_root: "pinned-commit")

    assert report_xslt.run(["--profile", "xslt30", "--census", str(target)], repo_root=root) == 0

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["profile"] == "xslt30"
    assert document["corpus"] == "pinned-commit"
    assert document["timeout_seconds"] == report_xslt.DEFAULT_TIMEOUT
    assert [case["name"] for case in document["cases"]] == ["gate/case-001"]
    assert document["cases"][0]["outcome"] == "fail"
    assert document["totals"]["fail"] == 1
    assert "wrote 1 census records" in capsys.readouterr().err
    # A census never writes a baseline.
    assert not (root / "tests" / "xslt" / "baselines" / "xslt30.toml").exists()


def test_census_falls_back_to_the_catalog_corpus_and_keeps_a_fractional_budget(
    tmp_path: Path,
) -> None:
    root = _fixture_repo(tmp_path)
    target = tmp_path / "census.json"

    assert (
        report_xslt.run(
            ["--profile", "xslt30", "--timeout", "7.5", "--census", str(target)], repo_root=root
        )
        == 0
    )

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["corpus"] == _catalog(root).corpus_revision
    assert document["timeout_seconds"] == 7.5


def test_check_mode_never_executes_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fixture_repo(tmp_path)
    _write_baseline(root)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("--check executed the corpus")

    monkeypatch.setattr(report_xslt, "run_parallel", forbidden)

    assert report_xslt.run(["--profile", "xslt30", "--check"], repo_root=root) == 0
