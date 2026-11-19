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
  <test-set name="pinned" file="pinned/_pinned-test-set.xml"/>
</catalog>
"""

GATE_ONLY_CATALOG = """\
<?xml version="1.0" encoding="UTF-8"?>
<catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog">
  <test-set name="gate" file="gate/_gate-test-set.xml"/>
</catalog>
"""

GATE_OTHER_CATALOG = """\
<?xml version="1.0" encoding="UTF-8"?>
<catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog">
  <test-set name="gate" file="gate/_gate-test-set.xml"/>
  <test-set name="other" file="other/_other-test-set.xml"/>
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

GATE_TWO_CASE_TEST_SET = """\
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
  <test-case name="case-002">
    <environment ref="gate-env"/>
    <test><stylesheet file="case-002.xsl"/></test>
    <result><assert-xml><![CDATA[<out>right</out>]]></assert-xml></result>
  </test-case>
</test-set>
"""

OTHER_TEST_SET = """\
<?xml version="1.0" encoding="UTF-8"?>
<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog" name="other">
  <environment name="other-env">
    <source role="."><content><![CDATA[<doc/>]]></content></source>
  </environment>
  <test-case name="other-001">
    <environment ref="other-env"/>
    <test><stylesheet file="other-001.xsl"/></test>
    <result><assert-xml><![CDATA[<out>right</out>]]></assert-xml></result>
  </test-case>
</test-set>
"""

PINNED_TEST_SET = """\
<?xml version="1.0" encoding="UTF-8"?>
<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog" name="pinned">
  <dependencies>
    <spec value="XSLT20+"/>
    <unicode-version value="{version}" satisfied="true"/>
  </dependencies>
  <environment name="pinned-env">
    <source role="."><content><![CDATA[<doc/>]]></content></source>
  </environment>
  <test-case name="pinned-001">
    <environment ref="pinned-env"/>
    <test><stylesheet file="pinned-001.xsl"/></test>
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


def _fixture_repo(tmp_path: Path, *, pinned_version: str | None = None) -> Path:
    """A synthetic repo with the gate case, plus a version-pinned one if asked.

    The pinned test set declares a ``unicode-version`` dependency the xslt30
    profile's tables do not satisfy, so its case is gated out of the profile
    but still measurable through ``--include-gated``.
    """
    root = tmp_path / "repo"
    corpus = root / "tests" / "xslt" / "corpus"
    gate = corpus / "gate"
    gate.mkdir(parents=True)
    catalog = GATE_ONLY_CATALOG
    (corpus / "catalog.xml").write_text(catalog, encoding="utf-8")
    (gate / "_gate-test-set.xml").write_text(GATE_TEST_SET, encoding="utf-8")
    (gate / "case-001.xsl").write_text(SHEET, encoding="utf-8")
    if pinned_version is not None:
        pinned = corpus / "pinned"
        pinned.mkdir()
        (corpus / "catalog.xml").write_text(GATE_CATALOG, encoding="utf-8")
        (pinned / "_pinned-test-set.xml").write_text(
            PINNED_TEST_SET.format(version=pinned_version), encoding="utf-8"
        )
        (pinned / "pinned-001.xsl").write_text(SHEET, encoding="utf-8")
    (root / "tests" / "xslt" / "baselines").mkdir(parents=True)
    return root


def _three_case_repo(tmp_path: Path) -> Path:
    """A synthetic repo with two ``gate/`` cases and one ``other/`` case."""
    root = tmp_path / "repo"
    corpus = root / "tests" / "xslt" / "corpus"
    gate = corpus / "gate"
    gate.mkdir(parents=True)
    (corpus / "catalog.xml").write_text(GATE_OTHER_CATALOG, encoding="utf-8")
    (gate / "_gate-test-set.xml").write_text(GATE_TWO_CASE_TEST_SET, encoding="utf-8")
    for name in ("case-001", "case-002"):
        (gate / f"{name}.xsl").write_text(SHEET, encoding="utf-8")
    other = corpus / "other"
    other.mkdir()
    (other / "_other-test-set.xml").write_text(OTHER_TEST_SET, encoding="utf-8")
    (other / "other-001.xsl").write_text(SHEET, encoding="utf-8")
    (root / "tests" / "xslt" / "baselines").mkdir(parents=True)
    return root


def _catalog(root: Path):
    return load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")


def _write_baseline(root: Path, cases: dict[str, str] | None = None) -> Path:
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
            cases={"gate/case-001": "fail"} if cases is None else cases,
        ),
    )
    return path


def _run_census(root: Path, target: Path, argv: tuple[str, ...] = ()) -> dict:
    """Write a census by argv and return the parsed document."""
    argv = ("--profile", "xslt30", "--census", str(target), *argv)
    assert report_xslt.run(list(argv), repo_root=root) == 0
    return json.loads(target.read_text(encoding="utf-8"))


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


def test_error_detail_splits_one_unexpected_error_group_into_families() -> None:
    results = (
        CaseResult.failed("a", message="unexpected error: XTDE0650: circularity"),
        CaseResult.failed("b", message="unexpected error: XTDE0650: circularity"),
        CaseResult.failed("c", message="unexpected error: XTDE0040: no such template"),
    )

    document = _census(results).to_dict()

    assert [(group["signature"], group["count"]) for group in document["signatures"]] == [
        ("unexpected error: XTDE0650: circularity", 2),
        ("unexpected error: XTDE0040: no such template", 1),
    ]


def test_a_multiline_mismatch_signs_on_its_path_and_keeps_detail_sanitized() -> None:
    message = (
        "actual XML differs from the expected XML: text differs at /out/a\n"
        "expected: /Users/someone/expected.xml\n"
        "actual: <out><a>2</a></out>"
    )
    observed = _census((CaseResult.failed("case", message=message),))

    case = observed.to_dict()["cases"][0]
    assert case["signature"] == "actual XML differs from the expected XML: text differs at /out/a"
    assert case["message"] == (
        "actual XML differs from the expected XML: text differs at /out/a\n"
        "expected: <path>\n"
        "actual: <out><a>2</a></out>"
    )
    assert "/Users/" not in census.render_json(observed)


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


def test_gated_results_form_their_own_labelled_section() -> None:
    observed = census.build_census(
        (CaseResult.passed("kept"),),
        profile="xslt30",
        corpus="pinned",
        timeout_seconds=5.0,
        gated=(
            CaseResult.timeout("gated-b", detail="the case exceeded its 5 s budget"),
            CaseResult.passed("gated-a"),
        ),
    ).to_dict()

    # The profile section describes only its own cases.
    assert observed["totals"]["pass"] == 1
    assert sum(observed["totals"].values()) == 1
    assert [case["name"] for case in observed["cases"]] == ["kept"]
    assert all(case["name"] != "gated-a" for case in observed["cases"])

    gated = observed["gated"]
    assert set(gated) == {"note", "totals", "cases"}
    assert gated["note"] == census.GATED_NOTE
    assert list(gated["totals"]) == [outcome.value for outcome in census.CENSUS_OUTCOMES]
    assert gated["totals"]["pass"] == 1
    assert gated["totals"]["timeout"] == 1
    assert [case["name"] for case in gated["cases"]] == ["gated-a", "gated-b"]
    assert gated["cases"][1]["signature"] == "timeout"
    assert "signatures" not in gated


def test_an_empty_gated_tuple_leaves_the_document_unchanged() -> None:
    plain = census.render_json(_census(_mixed_results()))
    empty = census.render_json(
        census.build_census(
            _mixed_results(),
            profile="xslt30",
            corpus="pinned",
            timeout_seconds=5.0,
            gated=(),
        )
    )

    assert plain == empty
    assert "gated" not in json.loads(empty)


def test_census_rejects_a_case_result_in_both_sections() -> None:
    with pytest.raises(ValueError, match="both sections"):
        census.build_census(
            (CaseResult.passed("a"),),
            profile="xslt30",
            corpus="c",
            timeout_seconds=5.0,
            gated=(CaseResult.failed("a"),),
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


def test_progress_reports_a_multiple_of_250_and_the_last_case(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = report_xslt.progress_reporter("gated cases")

    report(1, 3)
    assert capsys.readouterr().err == ""
    report(250, 1000)
    assert "...250/1000 gated cases" in capsys.readouterr().err
    report(1000, 1000)
    assert "...1000/1000 gated cases" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--check", "--enforce", "--write-baseline", None])
def test_include_gated_requires_census(
    flag: str | None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gated measurement must never become or be compared with a baseline."""
    argv = ["--profile", "xslt30", "--include-gated"]
    if flag is not None:
        argv.append(flag)
    if flag == "--write-baseline":
        argv.append(str(tmp_path / "baseline.toml"))

    assert report_xslt.run(argv, repo_root=tmp_path) == 2
    error = capsys.readouterr().err
    assert "--include-gated requires --census" in error
    assert not (tmp_path / "baseline.toml").exists()


def test_include_gated_keeps_gated_outcomes_out_of_totals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_repo(tmp_path, pinned_version="9.0")
    target = tmp_path / "census.json"

    argv = ["--profile", "xslt30", "--census", str(target), "--include-gated"]
    assert report_xslt.run(argv, repo_root=root) == 0

    document = json.loads(target.read_text(encoding="utf-8"))
    # The pinned case is gated out of the profile, so only the plain case counts.
    assert [case["name"] for case in document["cases"]] == ["gate/case-001"]
    assert document["totals"]["fail"] == 1
    assert document["totals"]["pass"] == 0
    # It is still measured, in its own labelled section, at the same budget.
    gated = document["gated"]
    assert gated["note"] == census.GATED_NOTE
    assert [case["name"] for case in gated["cases"]] == ["pinned/pinned-001"]
    assert gated["cases"][0]["outcome"] == "fail"
    assert gated["totals"]["fail"] == 1
    assert document["timeout_seconds"] == report_xslt.DEFAULT_TIMEOUT
    assert "wrote 2 census records" in capsys.readouterr().err


def test_gated_section_is_absent_without_the_flag(tmp_path: Path) -> None:
    """A census without ``--include-gated`` keeps its exact bytes."""
    root = _fixture_repo(tmp_path, pinned_version="9.0")
    plain = tmp_path / "plain.json"
    flagged = tmp_path / "flagged.json"

    assert report_xslt.run(["--profile", "xslt30", "--census", str(plain)], repo_root=root) == 0
    assert "gated" not in json.loads(plain.read_text(encoding="utf-8"))

    # The same repo with no gated cases renders identically with the flag.
    empty = _fixture_repo(tmp_path / "empty")
    assert (
        report_xslt.run(
            ["--profile", "xslt30", "--census", str(flagged), "--include-gated"], repo_root=empty
        )
        == 0
    )
    assert plain.read_bytes() == flagged.read_bytes()
    assert "gated" not in json.loads(flagged.read_text(encoding="utf-8"))


def test_a_filtered_gated_run_measures_only_the_matching_family(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path, pinned_version="9.0")
    target = tmp_path / "census.json"

    argv = [
        "--profile",
        "xslt30",
        "--filter",
        "pinned/",
        "--census",
        str(target),
        "--include-gated",
    ]
    assert report_xslt.run(argv, repo_root=root) == 0

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["cases"] == []
    assert [case["name"] for case in document["gated"]["cases"]] == ["pinned/pinned-001"]

    # A limited slice applies to both sections, so a bounded gated refresh never
    # silently widens into the whole family.
    limited = tmp_path / "limited.json"
    assert (
        report_xslt.run(
            [
                "--profile",
                "xslt30",
                "--limit",
                "1",
                "--census",
                str(limited),
                "--include-gated",
            ],
            repo_root=root,
        )
        == 0
    )
    bounded = json.loads(limited.read_text(encoding="utf-8"))
    assert [case["name"] for case in bounded["cases"]] == ["gate/case-001"]
    assert [case["name"] for case in bounded["gated"]["cases"]] == ["pinned/pinned-001"]


# ---------------------------------------------------------------------------
# Filter universe and the skipped section


def test_a_zero_match_filter_token_exits_two_naming_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_repo(tmp_path, pinned_version="9.0")

    argv = ["--profile", "xslt30", "--list-only", "--filter", "gate/case-999"]
    assert report_xslt.run(argv, repo_root=root) == 2
    assert "gate/case-999" in capsys.readouterr().err

    argv = ["--profile", "xslt30", "--list-only", "--filter", "gate/,,gate/case-001"]
    assert report_xslt.run(argv, repo_root=root) == 2
    assert "token '' is empty" in capsys.readouterr().err

    # The universe is the profile's selection plus, with --include-gated, the
    # gated candidates: a gated-only family is not zero-match there.
    target = tmp_path / "census.json"
    document = _run_census(root, target, ("--include-gated", "--filter", "pinned/"))
    assert document["cases"] == []
    assert [case["name"] for case in document["gated"]["cases"]] == ["pinned/pinned-001"]
    assert document["selection"]["matched"] == 0
    assert document["selection"]["available"] == 1

    refused = tmp_path / "refused.json"
    argv = ["--profile", "xslt30", "--census", str(refused), "--filter", "pinned/"]
    assert report_xslt.run(argv, repo_root=root) == 2
    assert "pinned/" in capsys.readouterr().err
    assert not refused.exists()


def test_skip_set_is_the_matching_baselines_timeout_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _three_case_repo(tmp_path)
    _write_baseline(
        root,
        {"gate/case-001": "timeout", "gate/case-002": "fail", "other/other-001": "pass"},
    )

    document = _run_census(root, tmp_path / "census.json", ("--skip-timeouts",))
    assert document["skipped"]["cases"] == ["gate/case-001"]
    assert document["selection"] == {
        "filters": [],
        "limit": None,
        "skip_timeouts": True,
        "available": 3,
        "matched": 3,
        "selected": 2,
    }
    assert sum(document["totals"].values()) == 2
    assert [case["name"] for case in document["cases"]] == [
        "gate/case-002",
        "other/other-001",
    ]
    assert "wrote 2 census records" in capsys.readouterr().err

    # The skip set follows the baseline; it is never a hardcoded family list.
    _write_baseline(
        root,
        {"gate/case-001": "fail", "gate/case-002": "timeout", "other/other-001": "pass"},
    )
    derived = _run_census(root, tmp_path / "derived.json", ("--skip-timeouts",))
    assert derived["skipped"]["cases"] == ["gate/case-002"]
    assert [case["name"] for case in derived["cases"]] == [
        "gate/case-001",
        "other/other-001",
    ]

    # A gap or a failure is never skipped; an empty skip set is legal and the
    # run is still marked.
    _write_baseline(
        root,
        {"gate/case-001": "adapter-gap", "gate/case-002": "fail", "other/other-001": "pass"},
    )
    unskipped = _run_census(root, tmp_path / "unskipped.json", ("--skip-timeouts",))
    assert "skipped" not in unskipped
    assert unskipped["selection"]["skip_timeouts"] is True
    assert unskipped["selection"]["selected"] == 3

    _write_baseline(
        root,
        {"gate/case-001": "timeout", "gate/case-002": "fail", "other/other-001": "pass"},
    )

    # A recorded timeout outside the filter slice was never in scope.
    _write_baseline(
        root,
        {"gate/case-001": "fail", "gate/case-002": "timeout", "other/other-001": "pass"},
    )
    outside = _run_census(
        root, tmp_path / "outside.json", ("--filter", "gate/case-001", "--skip-timeouts")
    )
    assert "skipped" not in outside
    assert outside["selection"]["selected"] == 1

    _write_baseline(
        root,
        {"gate/case-001": "timeout", "gate/case-002": "fail", "other/other-001": "pass"},
    )
    sliced = _run_census(root, tmp_path / "sliced.json", ("--filter", "gate/", "--skip-timeouts"))
    assert sliced["skipped"]["cases"] == ["gate/case-001"]
    assert (sliced["selection"]["matched"], sliced["selection"]["selected"]) == (2, 1)

    # filter -> skip -> limit: the limit bounds the cases that survive skipping.
    ordered = _run_census(
        root,
        tmp_path / "ordered.json",
        ("--filter", "gate/", "--skip-timeouts", "--limit", "1"),
    )
    assert [case["name"] for case in ordered["cases"]] == ["gate/case-002"]
    assert (ordered["selection"]["matched"], ordered["selection"]["selected"]) == (2, 1)


def test_skipped_rows_stay_outside_the_verdict_vocabulary(tmp_path: Path) -> None:
    root = _three_case_repo(tmp_path)
    _write_baseline(
        root,
        {"gate/case-001": "timeout", "gate/case-002": "fail", "other/other-001": "pass"},
    )
    document = _run_census(root, tmp_path / "census.json", ("--skip-timeouts",))

    skipped = document["skipped"]
    assert set(skipped) == {"note", "count", "source", "cases"}
    assert skipped["note"] == census.SKIPPED_NOTE
    assert skipped["count"] == 1
    assert set(skipped["source"]) == {"profile", "corpus", "timeout_seconds"}
    assert skipped["cases"] == ["gate/case-001"]
    assert "outcome" not in json.dumps(skipped)
    assert "signature" not in json.dumps(skipped)

    names = {case["name"] for case in document["cases"]}
    assert names.isdisjoint(skipped["cases"])
    samples = {sample for group in document["signatures"] for sample in group["samples"]}
    assert samples.isdisjoint(skipped["cases"])
    assert sum(document["totals"].values()) == len(document["cases"])
    assert len(document["cases"]) == document["selection"]["selected"]
    assert document["selection"]["selected"] + skipped["count"] == document["selection"]["matched"]

    selection = census.Selection(available=3, matched=3, selected=2)
    with pytest.raises(ValueError, match="both sections"):
        census.build_census(
            (CaseResult.passed("a"), CaseResult.failed("b")),
            profile="xslt30",
            corpus="c",
            timeout_seconds=5.0,
            selection=selection,
            skipped=census.Skipped(source={"profile": "xslt30"}, names=("b",)),
        )
    with pytest.raises(ValueError, match="selection"):
        census.build_census(
            (CaseResult.passed("a"),),
            profile="xslt30",
            corpus="c",
            timeout_seconds=5.0,
            skipped=census.Skipped(source={"profile": "xslt30"}, names=("b",)),
        )
    with pytest.raises(ValueError, match="duplicate skipped case"):
        census.build_census(
            (CaseResult.passed("a"),),
            profile="xslt30",
            corpus="c",
            timeout_seconds=5.0,
            selection=census.Selection(available=3, matched=3, selected=1),
            skipped=census.Skipped(source={}, names=("b", "b")),
        )
    with pytest.raises(ValueError, match="requires a name"):
        census.build_census(
            (CaseResult.passed("a"),),
            profile="xslt30",
            corpus="c",
            timeout_seconds=5.0,
            selection=census.Selection(available=2, matched=2, selected=1),
            skipped=census.Skipped(source={}, names=("",)),
        )
    with pytest.raises(ValueError, match="fewer than"):
        census.build_census(
            (CaseResult.passed("a"),),
            profile="xslt30",
            corpus="c",
            timeout_seconds=5.0,
            selection=census.Selection(available=2, matched=0, selected=1),
        )
    with pytest.raises(ValueError, match="more than"):
        census.build_census(
            (CaseResult.passed("a"),),
            profile="xslt30",
            corpus="c",
            timeout_seconds=5.0,
            selection=census.Selection(available=1, matched=2, selected=1),
        )


def test_a_full_census_is_byte_identical_to_the_unfiltered_render() -> None:
    results = _mixed_results()
    names = tuple(result.name for result in results)
    plain = census.build_census(results, profile="xslt30", corpus="pinned", timeout_seconds=5.0)
    full_coverage = census.build_census(
        results,
        profile="xslt30",
        corpus="pinned",
        timeout_seconds=5.0,
        selection=census.Selection(
            filters=names,
            available=len(names),
            matched=len(names),
            selected=len(names),
        ),
    )

    filtered = json.loads(census.render_json(full_coverage))
    assert list(filtered) == [
        "profile",
        "corpus",
        "timeout_seconds",
        "selection",
        "totals",
        "cases",
        "signatures",
    ]
    filtered.pop("selection")
    assert json.dumps(filtered, indent=2, ensure_ascii=False) + "\n" == census.render_json(plain)
    assert "selection" not in json.loads(census.render_json(plain))
    assert "skipped" not in json.loads(census.render_json(plain))


def test_an_engine_touching_change_runs_affected_families_unskipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rule = report_xslt.HONESTY_RULES[1]
    for phrase in ("src/", "tests/xslt/suite.py", "tests/xslt/environment.py", "--skip-timeouts"):
        assert phrase in rule
    # The module docstring references both honesty rules, not only rule 0.
    docstring = " ".join(report_xslt.__doc__.split())
    for phrase in ("tests/xslt/suite.py", "tests/xslt/environment.py", "without --skip-timeouts"):
        assert phrase in docstring

    root = _three_case_repo(tmp_path)
    _write_baseline(
        root,
        {"gate/case-001": "timeout", "gate/case-002": "fail", "other/other-001": "pass"},
    )
    for host in ("--enforce", "--check", "--list-only"):
        assert (
            report_xslt.run(["--profile", "xslt30", host, "--skip-timeouts"], repo_root=root) == 2
        )
        assert "--skip-timeouts" in capsys.readouterr().err
    target = tmp_path / "baseline.toml"
    argv = ["--profile", "xslt30", "--write-baseline", str(target), "--skip-timeouts"]
    assert report_xslt.run(argv, repo_root=root) == 2
    assert "--skip-timeouts" in capsys.readouterr().err
    assert not target.exists()

    document = _run_census(root, tmp_path / "census.json", ("--skip-timeouts",))
    assert document["selection"]["skip_timeouts"] is True
    assert document["skipped"]["cases"] == ["gate/case-001"]
