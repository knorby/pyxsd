"""Contract for the independent XSLT 3.0, 2.0, and 1.0 conformance gates.

The checks are structural: per-profile baselines and their corpus, profile,
and timeout-budget identity, deterministic report shape, strict
regression/improvement handling, the native processor identity behind each
profile, and the workflow's corpus caching and coverage posture.  They never
assert a current failure total.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PurePosixPath
from threading import Barrier
from unittest.mock import patch

import pytest
import yaml
from tests import report_xslt
from tests.xslt import baseline as baseline_module
from tests.xslt import census as census_module
from tests.xslt import environment as environment_module
from tests.xslt import report
from tests.xslt import suite as suite_module
from tests.xslt.catalog import load_catalog
from tests.xslt.model import (
    Collection,
    Environment,
    Invocation,
    Resource,
    Schema,
    Stylesheet,
)
from tests.xslt.model import TestCase as CatalogCase
from tests.xslt.outcomes import CaseResult, Outcome
from tests.xslt.runner import CaseExecutionError
from tests.xslt.selection import select_cases
from tests.xslt.suite import (
    ENGINE_UNICODE_VERSION,
    SUITE_PROFILES,
    SuiteProfile,
    UnsupportedCase,
    _parallel_start_method,
    _run_payload,
    case_budget,
    execute_case,
    run_case,
    run_parallel,
    with_unicode_version,
)
from tests.xslt.support import stylesheet

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "tests" / "xslt" / "corpus" / "catalog.xml"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "xslt.yml"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
COVERAGE_FLOOR = 99


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _steps(job: dict) -> list[dict]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _coverage_floor(config: dict) -> int:
    """The configured coverage floor, read as a number: a minimum, not a pin."""
    floor = config["tool"]["coverage"]["report"]["fail_under"]
    assert isinstance(floor, int)
    return floor


def _dependency_names(requirements: list[str]) -> set[str]:
    """Distribution names, normalized, from requirement strings."""
    return {
        re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].lower() for requirement in requirements
    }


CI_XSLT_JOBS = tuple(sorted(_workflow()["jobs"]))
PINNED_COMMIT = report_xslt.pinned_corpus(REPO_ROOT)

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
{environments}
{cases}
</test-set>
"""

TWO_FAMILY_CATALOG = """\
<?xml version="1.0" encoding="UTF-8"?>
<catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog">
  <test-set name="gate" file="gate/_gate-test-set.xml"/>
  <test-set name="other" file="other/_other-test-set.xml"/>
</catalog>
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

GATE_CASE = """\
  <test-case name="{name}">
    <environment ref="gate-env"/>
    <test><stylesheet file="{name}.xsl"/></test>
    <result><assert-xml><![CDATA[<out>right</out>]]></assert-xml></result>
  </test-case>
"""

SHEET = """\
<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:template match="/"><out>wrong</out></xsl:template>
</xsl:stylesheet>
"""

RIGHT_SHEET = SHEET.replace("wrong", "right")


@pytest.fixture(autouse=True)
def _no_ambient_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every fixture's budget hermetic: the test sets it, never the shell."""
    monkeypatch.delenv("PYXSD_XSLT_TIMEOUT", raising=False)


def _fixture_repo(tmp_path: Path, names: tuple[str, ...] = ("case-001",)) -> Path:
    """A synthetic repo with one test set, its stylesheets, and no baseline."""
    root = tmp_path / "repo"
    corpus = root / "tests" / "xslt" / "corpus"
    gate = corpus / "gate"
    gate.mkdir(parents=True)
    (corpus / "catalog.xml").write_text(GATE_CATALOG, encoding="utf-8")
    (gate / "_gate-test-set.xml").write_text(
        GATE_TEST_SET.format(
            environments="",
            cases="".join(GATE_CASE.format(name=name) for name in names),
        ),
        encoding="utf-8",
    )
    for name in names:
        (gate / f"{name}.xsl").write_text(SHEET, encoding="utf-8")
    (root / "tests" / "xslt" / "baselines").mkdir(parents=True)
    return root


def _two_family_repo(tmp_path: Path) -> Path:
    """A synthetic repo with ``gate/`` (two cases) and ``other/`` (one case)."""
    root = _fixture_repo(tmp_path, names=("case-001", "case-002"))
    corpus = root / "tests" / "xslt" / "corpus"
    (corpus / "catalog.xml").write_text(TWO_FAMILY_CATALOG, encoding="utf-8")
    other = corpus / "other"
    other.mkdir()
    (other / "_other-test-set.xml").write_text(OTHER_TEST_SET, encoding="utf-8")
    (other / "other-001.xsl").write_text(SHEET, encoding="utf-8")
    return root


def _baseline_path(root: Path, profile: str = "xslt30") -> Path:
    return root / "tests" / "xslt" / "baselines" / f"{profile}.toml"


def _write_baseline(root: Path, profile: str, cases: dict[str, str], **meta: object) -> Path:
    path = _baseline_path(root, profile)
    catalog = load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")
    recorded = baseline_module.Baseline(
        meta={
            "schema": baseline_module.BASELINE_SCHEMA,
            "corpus": catalog.corpus_revision,
            "profile": profile,
            "timeout_seconds": report_xslt.DEFAULT_TIMEOUT,
            **meta,
        },
        cases=cases,
    )
    baseline_module.write_baseline(path, recorded)
    return path


def _run(argv: list[str], root: Path) -> int:
    return report_xslt.run(argv, repo_root=root)


def _run_census(root: Path, target: Path, argv: tuple[str, ...] = ()) -> dict:
    """Write a census by argv and return the parsed document."""
    assert _run(["--profile", "xslt30", "--census", str(target), *argv], root) == 0
    return json.loads(target.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Profile and baseline identity


def test_profiles_have_independent_baselines_and_reports() -> None:
    assert baseline_module.load_baseline("xslt30").meta["corpus"] == PINNED_COMMIT
    assert baseline_module.load_baseline("xslt20").meta["profile"] == "xslt20"
    assert baseline_module.load_baseline("xslt10").meta["profile"] == "xslt10"
    assert set(CI_XSLT_JOBS) == {"xslt30", "xslt20", "xslt10"}


def test_profile_runs_use_native_processor_identities() -> None:
    assert set(SUITE_PROFILES) == {"xslt30", "xslt20", "xslt10"}
    expected = {
        "xslt30": ("3.0", "3.1"),
        "xslt20": ("2.0", "2.0"),
        "xslt10": ("1.0", "1.0"),
    }
    for name, (language, xpath) in expected.items():
        profile = SUITE_PROFILES[name]
        assert profile.processor.language == language
        assert profile.processor.xpath == xpath
        assert profile.feature_profile.language == language
        assert profile.feature_profile.xpath_version == xpath
        assert profile.name == name
    assert SUITE_PROFILES["xslt20"].feature_profile.supports("schema_aware")
    assert not SUITE_PROFILES["xslt10"].feature_profile.supports("schema_aware")
    assert SUITE_PROFILES["xslt20"].feature_profile.declines("streaming")
    assert SUITE_PROFILES["xslt10"].feature_profile.declines("higher_order_functions")


def test_xslt10_profile_is_not_schema_aware() -> None:
    """The 1.0 identity is constructed without schema awareness."""
    assert SUITE_PROFILES["xslt10"].processor.schema_aware is False


def test_every_profile_constructs_the_schema_awareness_it_declares() -> None:
    constructed = {name: profile.processor.schema_aware for name, profile in SUITE_PROFILES.items()}
    declared = {
        name: profile.feature_profile.supports("schema_aware")
        for name, profile in SUITE_PROFILES.items()
    }
    assert constructed == declared


def test_baselines_record_their_own_corpus_and_never_share_verdicts() -> None:
    profiles = ("xslt30", "xslt20", "xslt10")
    loaded = {name: baseline_module.load_baseline(name) for name in profiles}
    assert {recorded.meta["profile"] for recorded in loaded.values()} == set(profiles)
    assert {recorded.meta["corpus"] for recorded in loaded.values()} == {PINNED_COMMIT}
    paths = {_baseline_path(REPO_ROOT, name) for name in profiles}
    assert len(paths) == 3
    assert len({frozenset(recorded.cases) for recorded in loaded.values()}) == 3


@pytest.mark.skipif(not CORPUS.is_file(), reason="xslt30-test corpus is not checked out")
def test_real_catalog_smoke_covers_every_baseline_case() -> None:
    catalog = load_catalog(CORPUS)
    assert not catalog.metadata_errors
    for name, profile in SUITE_PROFILES.items():
        declared = {case.name for case in select_cases(catalog, profile.feature_profile)}
        recorded = set(baseline_module.load_baseline(name).cases)
        assert recorded == declared


# ---------------------------------------------------------------------------
# Report shape and reproducibility


def test_report_shape_is_deterministic_and_clock_free() -> None:
    generated = report.build_report(
        (
            CaseResult.passed("a"),
            CaseResult.failed("b", message="mismatch"),
            CaseResult.adapter_gap("c", detail="packages"),
        ),
        corpus=PINNED_COMMIT,
        profile="xslt30",
    )
    document = generated.to_dict()
    assert set(document) == {"schema", "corpus", "profile", "summary", "cases"}
    assert document["corpus"] == PINNED_COMMIT
    assert document["profile"] == "xslt30"
    assert list(document["summary"]) == sorted(document["summary"])
    assert set(document["cases"]) == {"a", "b", "c"}
    assert set(document["cases"]["a"]) == {"outcome", "coverage", "message"}

    rendered = report.render_json(generated)
    assert rendered == report.render_json(generated)
    assert re.search(r"\d{4}-\d{2}-\d{2}", rendered) is None
    assert re.search(r"\b\d{2}:\d{2}:\d{2}\b", rendered) is None
    assert str(REPO_ROOT) not in rendered


def test_outcome_only_reports_keep_the_baseline_limited_to_verdicts() -> None:
    recorded = report.outcomes_report(
        {"a": "pass", "b": "wrong-error-code"},
        corpus=PINNED_COMMIT,
        profile="xslt20",
    )
    document = recorded.to_dict()
    assert document["cases"]["a"] == {"outcome": "pass", "coverage": [], "message": ""}
    assert document["cases"]["b"]["outcome"] == "wrong-error-code"
    assert "expected_error_code" not in document["cases"]["b"]
    with pytest.raises(ValueError, match="unknown XSLT outcome"):
        report.outcomes_report(
            {"a": "not-an-outcome"},
            corpus=PINNED_COMMIT,
            profile="xslt20",
        )


@pytest.mark.skipif(not CORPUS.is_file(), reason="xslt30-test corpus is not checked out")
@pytest.mark.parametrize("profile", sorted(SUITE_PROFILES))
def test_checked_in_baseline_is_reproducible(
    profile: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert report_xslt.run(["--profile", profile, "--check"]) == 0
    first = capsys.readouterr()
    assert report_xslt.run(["--profile", profile, "--check"]) == 0
    second = capsys.readouterr()
    assert first.err == second.err
    # ``--check`` renders in memory; pin the checked-in bytes too, so a
    # hand-edited or drifted file fails even when its parsed content re-renders.
    baseline_path = _baseline_path(REPO_ROOT, profile)
    assert (
        baseline_module.render_baseline(baseline_module.load_baseline(profile)).encode()
        == baseline_path.read_bytes()
    )


def test_check_reads_neither_the_clock_nor_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fixture_repo(tmp_path)
    _write_baseline(root, "xslt30", {"gate/case-001": "fail"})

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("report generation read the clock")

    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(time, "monotonic", forbidden)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert _run(["--profile", "xslt30", "--check", "--json"], root) == 0
    assert output.getvalue() == report.render_json(
        report.build_report(
            (CaseResult(Outcome.FAIL, name="gate/case-001"),),
            corpus=PINNED_COMMIT,
            profile="xslt30",
        )
    )


def test_check_rejects_profile_corpus_and_case_set_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_repo(tmp_path)
    path = _write_baseline(root, "xslt30", {"gate/case-001": "fail"})
    assert _run(["--profile", "xslt30", "--check"], root) == 0

    recorded = baseline_module.load_baseline(path)
    baseline_module.write_baseline(
        path, replace(recorded, meta={**recorded.meta, "profile": "xslt20"})
    )
    assert _run(["--profile", "xslt30", "--check"], root) == 1

    baseline_module.write_baseline(
        path, replace(recorded, meta={**recorded.meta, "corpus": "deadbeef"})
    )
    assert _run(["--profile", "xslt30", "--check"], root) == 1

    baseline_module.write_baseline(path, replace(recorded, cases={}))
    assert _run(["--profile", "xslt30", "--check"], root) == 1

    baseline_module.write_baseline(
        path,
        replace(recorded, cases={**recorded.cases, "gate/not-declared": "pass"}),
    )
    assert _run(["--profile", "xslt30", "--check"], root) == 1

    # A verdict outside the Outcome vocabulary is a refusal to read the
    # baseline, like a missing [meta] table: exit 2, not report drift.
    baseline_module.write_baseline(path, replace(recorded, cases={"gate/case-001": "bogus"}))
    assert _run(["--profile", "xslt30", "--check"], root) == 2
    assert "not a verdict" in capsys.readouterr().err


def test_check_rejects_a_timeout_budget_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_repo(tmp_path)
    _write_baseline(root, "xslt30", {"gate/case-001": "fail"}, timeout_seconds=5)

    assert _run(["--profile", "xslt30", "--check", "--timeout", "10"], root) == 1
    error = capsys.readouterr().err
    assert "5" in error and "10" in error
    assert "regenerate" in error


def test_check_accepts_a_budget_that_matches_the_baseline(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    _write_baseline(root, "xslt30", {"gate/case-001": "fail"}, timeout_seconds=10)

    assert _run(["--profile", "xslt30", "--check", "--timeout", "10"], root) == 0


def test_check_takes_the_budget_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_repo(tmp_path)
    _write_baseline(root, "xslt30", {"gate/case-001": "fail"}, timeout_seconds=5)
    monkeypatch.setenv("PYXSD_XSLT_TIMEOUT", "10")

    assert _run(["--profile", "xslt30", "--check"], root) == 1
    error = capsys.readouterr().err
    assert "5" in error and "10" in error


def test_check_rejects_a_baseline_that_records_no_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_repo(tmp_path)
    path = _write_baseline(root, "xslt30", {"gate/case-001": "fail"})
    recorded = baseline_module.load_baseline(path)
    baseline_module.write_baseline(
        path,
        replace(
            recorded,
            meta={key: value for key, value in recorded.meta.items() if key != "timeout_seconds"},
        ),
    )

    assert _run(["--profile", "xslt30", "--check"], root) == 1
    assert "timeout budget" in capsys.readouterr().err


def test_check_reports_a_missing_baseline_and_runs_need_the_catalog(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    assert _run(["--profile", "xslt30", "--check"], root) == 2
    assert _run(["--profile", "xslt30", "--list-only"], root) == 0
    assert _run(["--profile", "xslt30", "--check", "--enforce"], root) == 2

    missing = tmp_path / "missing"
    assert _run(["--profile", "xslt30"], missing) == 2


def test_write_baseline_refuses_an_incomplete_slice(tmp_path: Path) -> None:
    root = _two_family_repo(tmp_path)
    target = _baseline_path(root)
    assert (
        _run(
            ["--profile", "xslt30", "--limit", "1", "--write-baseline", str(target)],
            root,
        )
        == 2
    )
    assert (
        _run(
            ["--profile", "xslt30", "--filter", "gate/", "--write-baseline", str(target)],
            root,
        )
        == 2
    )
    assert _run(["--profile", "xslt30", "--enforce", "--write-baseline", str(target)], root) == 2
    assert not target.exists()


def test_write_baseline_is_byte_stable_after_an_independent_read(tmp_path: Path) -> None:
    """A written baseline is a function of its own bytes, not of the run."""
    root = _fixture_repo(tmp_path)
    target = _baseline_path(root)

    assert _run(["--profile", "xslt30", "--write-baseline", str(target)], root) == 0
    written = target.read_bytes()
    document = tomllib.loads(written.decode("utf-8"))
    independent = baseline_module.Baseline(meta=document["meta"], cases=document["cases"])

    assert baseline_module.render_baseline(independent).encode("utf-8") == written
    assert (
        baseline_module.render_baseline(baseline_module.load_baseline(target)).encode("utf-8")
        == written
    )


def test_write_baseline_records_the_environment_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fixture_repo(tmp_path)
    target = _baseline_path(root)

    monkeypatch.setenv("PYXSD_XSLT_TIMEOUT", "7.5")
    assert _run(["--profile", "xslt30", "--write-baseline", str(target)], root) == 0
    assert baseline_module.load_baseline(target).meta["timeout_seconds"] == 7.5

    monkeypatch.setenv("PYXSD_XSLT_TIMEOUT", "3")
    assert (
        _run(
            ["--profile", "xslt30", "--timeout", "9", "--write-baseline", str(target)],
            root,
        )
        == 0
    )
    assert baseline_module.load_baseline(target).meta["timeout_seconds"] == 9


def test_check_names_corpus_identity_when_the_baseline_has_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_repo(tmp_path)
    path = _write_baseline(root, "xslt30", {"gate/case-001": "fail"})
    recorded = baseline_module.load_baseline(path)
    baseline_module.write_baseline(
        path,
        replace(
            recorded,
            meta={key: value for key, value in recorded.meta.items() if key != "corpus"},
        ),
    )

    assert _run(["--profile", "xslt30", "--check"], root) == 1
    assert "corpus identity" in capsys.readouterr().err


def test_check_labels_a_baseline_outside_the_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_repo(tmp_path)
    catalog = load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")
    external = tmp_path / "elsewhere" / "xslt30.toml"
    baseline_module.write_baseline(
        external,
        baseline_module.Baseline(
            meta={
                "schema": baseline_module.BASELINE_SCHEMA,
                "corpus": catalog.corpus_revision,
                "profile": "xslt30",
                "timeout_seconds": report_xslt.DEFAULT_TIMEOUT,
            },
            cases={"gate/case-001": "fail"},
        ),
    )

    assert _run(["--profile", "xslt30", "--check", "--baseline", str(external)], root) == 0
    assert f"reproducible from {external}" in capsys.readouterr().err


def test_run_corpus_emits_json_for_a_synthetic_case(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_repo(tmp_path)

    assert _run(["--profile", "xslt30", "--json"], root) == 0
    document = json.loads(capsys.readouterr().out)
    assert set(document["cases"]) == {"gate/case-001"}
    assert document["cases"]["gate/case-001"]["outcome"] == "fail"
    assert set(document["cases"]["gate/case-001"]) == {"outcome", "coverage", "message"}


# ---------------------------------------------------------------------------
# Strict gate and honest execution


def test_gate_names_regressions_and_improvements_separately() -> None:
    changes = baseline_module.diff_baseline(
        {"kept": "pass", "worse": "pass", "better": "fail", "moved": "timeout"},
        {"kept": "pass", "worse": "fail", "better": "pass", "moved": "fail"},
    )
    assert [(change.kind, change.key) for change in changes] == [
        ("improvement", "better"),
        ("changed", "moved"),
        ("regression", "worse"),
    ]

    hard, drift = baseline_module.gate_changes(
        {"back": "timeout", "passes-out": "pass", "stays-out": "fail", "worse": "pass"},
        {"back": "pass", "passes-out": "timeout", "stays-out": "timeout", "worse": "fail"},
    )
    assert [change.key for change in hard] == ["back", "passes-out", "worse"]
    assert [change.key for change in drift] == ["stays-out"]


def test_enforce_is_strict_and_current_failures_are_not_counted(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    target = _baseline_path(root)

    assert _run(["--profile", "xslt30", "--write-baseline", str(target)], root) == 0
    recorded = baseline_module.load_baseline(target)
    assert recorded.cases == {"gate/case-001": "fail"}
    # A matching failing verdict passes: the gate compares named outcomes, it
    # never asserts a failure total.
    assert _run(["--profile", "xslt30", "--enforce", "--baseline", str(target)], root) == 0

    (root / "tests" / "xslt" / "corpus" / "gate" / "case-001.xsl").write_text(
        RIGHT_SHEET, encoding="utf-8"
    )
    assert _run(["--profile", "xslt30", "--enforce", "--baseline", str(target)], root) == 2

    (root / "tests" / "xslt" / "corpus" / "gate" / "case-001.xsl").write_text(
        SHEET, encoding="utf-8"
    )
    assert _run(["--profile", "xslt30", "--enforce", "--baseline", str(target)], root) == 0


def test_enforce_refuses_a_baseline_recorded_under_another_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The executing gate treats the budget as identity, not as verdict drift."""
    root = _fixture_repo(tmp_path)
    # Recorded as a timeout at 5 s; the same case fails at 10 s, which
    # ``gate_changes`` alone would call ``fail``↔``timeout`` harness drift.
    _write_baseline(root, "xslt30", {"gate/case-001": "timeout"}, timeout_seconds=5)

    assert _run(["--profile", "xslt30", "--enforce", "--timeout", "10"], root) == 2
    error = capsys.readouterr().err
    assert "5" in error and "10" in error
    assert "regenerate the baseline deliberately" in error
    assert "[harness]" not in error


def test_executor_reports_unsupported_environment_shapes_as_adapter_gaps(
    tmp_path: Path,
) -> None:
    root = _fixture_repo(tmp_path)
    catalog = load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")
    case = catalog.cases["gate/case-001"]
    profile = SUITE_PROFILES["xslt30"]
    shapes = {
        "resource without a file or URI": replace(
            case,
            environment=replace(
                case.environment,
                resources=(Resource(role="resource"),),
            ),
        ),
        "named collection without sources": replace(
            case,
            environment=replace(
                case.environment,
                collections=(Collection(uri="urn:collection"),),
            ),
        ),
        "0 principal stylesheets": replace(
            case,
            invocation=replace(case.invocation, stylesheets=()),
        ),
    }
    for reason, mutated in shapes.items():
        with pytest.raises(UnsupportedCase, match=re.escape(reason)):
            execute_case(mutated, profile)


def test_run_case_classifies_errors_without_aborting_the_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fixture_repo(tmp_path)
    catalog = load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")
    case = catalog.cases["gate/case-001"]
    profile = SUITE_PROFILES["xslt30"]

    def explode(item: object, selected: object) -> object:
        raise ValueError("boom")

    monkeypatch.setattr("tests.xslt.suite.execute_case", explode)
    result = run_case(case, profile)
    assert result.outcome is Outcome.FAIL
    assert result.message == "ValueError: boom"


def _hat_case(root: Path, schema_name: str) -> CatalogCase:
    return CatalogCase(
        name=f"cache/{schema_name}",
        local_name=schema_name,
        test_set="cache",
        description="",
        source=PurePosixPath("cache"),
        environment=Environment(
            schemas=(Schema(role="stylesheet-import", path=PurePosixPath(schema_name)),),
        ),
        invocation=Invocation(
            stylesheets=(Stylesheet(role="principal", path=PurePosixPath("sheet.xsl")),),
        ),
        document_root=root,
    )


HAT_NS = "urn:hats"

_HAT_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:hats" xmlns:t="urn:hats" elementFormDefault="qualified">
  <xs:simpleType name="hatsize">
    <xs:restriction base="xs:integer">
      <xs:minInclusive value="4"/><xs:maxInclusive value="{maximum}"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:element name="head" type="t:hatsize"/>
</xs:schema>"""


def test_a_shared_stylesheet_never_shares_one_compiled_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compiled artifact is keyed by the case's schema environment too.

    Both cases share one stylesheet path and differ only in the schema they
    declare: the constructor it calls caps its argument at 10 in one and 20 in
    the other.  A shared artifact would decide one case with the other's
    schema, so the cap is the difference between a pass and a dynamic error.
    """
    monkeypatch.setattr(environment_module, "_COMPILED_SCHEMAS", {})
    monkeypatch.setattr(suite_module, "_COMPILED", {})
    (tmp_path / "narrow.xsd").write_text(_HAT_SCHEMA.format(maximum=10), encoding="utf-8")
    (tmp_path / "wide.xsd").write_text(_HAT_SCHEMA.format(maximum=20), encoding="utf-8")
    (tmp_path / "sheet.xsl").write_bytes(
        stylesheet(
            '<xsl:param name="p" select="11"/>'
            '<xsl:template name="xsl:initial-template"><out>'
            '<xsl:value-of select="hat:hatsize($p)"/></out></xsl:template>',
            namespaces={"hat": HAT_NS},
            attributes=' exclude-result-prefixes="hat"',
            declare_xs=False,
        )
    )

    wide = _hat_case(tmp_path, "wide.xsd")
    narrow = _hat_case(tmp_path, "narrow.xsd")

    assert execute_case(wide, SUITE_PROFILES["xslt30"]).xml == "<out>11</out>"
    with pytest.raises(CaseExecutionError, match="FORG0001"):
        execute_case(narrow, SUITE_PROFILES["xslt30"])

    assert len(suite_module._COMPILED) == 2
    assert {key[3] for key in suite_module._COMPILED} == {
        environment_module.environment_fingerprint(wide),
        environment_module.environment_fingerprint(narrow),
    }


def test_case_budget_interrupts_a_long_case() -> None:
    with pytest.raises(TimeoutError, match="budget"), case_budget(0.05):
        time.sleep(1)
    with case_budget(None):
        pass


def test_parallel_execution_preserves_catalog_order(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path, names=("case-001", "case-002", "case-003"))
    for name in ("case-001", "case-002", "case-003"):
        (root / "tests" / "xslt" / "corpus" / "gate" / f"{name}.xsl").write_text(
            RIGHT_SHEET, encoding="utf-8"
        )
    catalog = load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")
    cases = tuple(catalog.cases.values())
    results = run_parallel(cases, SUITE_PROFILES["xslt30"], timeout=10, jobs=2)
    assert [result.name for result in results] == [case.name for case in cases]
    assert {result.name: result.outcome for result in results} == {
        "gate/case-001": Outcome.PASS,
        "gate/case-002": Outcome.PASS,
        "gate/case-003": Outcome.PASS,
    }


def _deadline_case(case, profile):
    if case.local_name == "case-001":
        start = Barrier(2)

        def backtrack():
            try:
                Path(os.environ["PYXSD_TEST_XSLT_WORKER_PID"]).write_text(
                    str(os.getpid()), encoding="ascii"
                )
                start.wait()
                # The C matcher holds the GIL here. Its signal checks cannot
                # dispatch the Python alarm handler from this non-main thread.
                re.fullmatch(r"(a+)+", "a" * 80 + "!")
                raise AssertionError("the backtracking probe completed")
            except BaseException:
                start.abort()
                raise

        with ThreadPoolExecutor(max_workers=1) as executor:
            searching = executor.submit(backtrack)
            start.wait()
            return searching.result()
    return execute_case(case, profile)


def _deadline_worker(payload):
    with patch.object(suite_module, "execute_case", _deadline_case):
        return _run_payload(payload)


@pytest.mark.parametrize(("jobs", "count"), [(1, 2), (2, 1), (2, 2)])
def test_parallel_deadline_reaps_an_uninterruptible_worker(tmp_path, jobs, count):
    names = ("case-001", "case-002")[:count]
    root = _fixture_repo(tmp_path, names=names)
    for name in names:
        (root / "tests" / "xslt" / "corpus" / "gate" / f"{name}.xsl").write_text(
            RIGHT_SHEET, encoding="utf-8"
        )
    pid_path = tmp_path / "worker.pid"
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import json
import multiprocessing
import os
import sys
from tests.xslt import suite
from tests.xslt.catalog import load_catalog
from tests.xslt.test_ci_contract import _deadline_case, _deadline_worker

suite.execute_case = _deadline_case
suite._run_payload = _deadline_worker
cases = tuple(load_catalog(sys.argv[1]).cases.values())
results = suite.run_parallel(cases, suite.SUITE_PROFILES["xslt30"], timeout=1, jobs=int(sys.argv[2]))
print(json.dumps([
    os.getpid(),
    [proc.pid for proc in multiprocessing.active_children()],
    [[result.name, result.outcome.value, result.message] for result in results],
]))
""",
            str(root / "tests" / "xslt" / "corpus" / "catalog.xml"),
            str(jobs),
        ],
        env={**os.environ, "PYXSD_TEST_XSLT_WORKER_PID": str(pid_path)},
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    ) as run:
        try:
            stdout, stderr = run.communicate(timeout=22)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(run.pid, signal.SIGKILL)
            else:
                run.kill()
            run.communicate()
            raise
    assert run.returncode == 0, stderr
    parent, children, results = json.loads(stdout)
    assert int(pid_path.read_text(encoding="ascii")) != parent
    assert children == []
    expected = [["gate/case-001", "timeout", "the case exceeded its 1 s budget"]]
    if count == 2:
        expected.append(["gate/case-002", "pass", ""])
    assert results == expected


def test_parallel_workers_avoid_fork_in_xdist_or_multithreaded_parents() -> None:
    assert _parallel_start_method(in_xdist_worker=True, active_threads=1) == "spawn"
    assert _parallel_start_method(in_xdist_worker=False, active_threads=2) == "spawn"


def test_serial_workers_keep_the_fast_safe_start_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        suite_module.multiprocessing, "get_all_start_methods", lambda: ["fork", "spawn"]
    )
    assert _parallel_start_method(in_xdist_worker=False, active_threads=1) == "fork"
    monkeypatch.setattr(suite_module.multiprocessing, "get_all_start_methods", lambda: ["spawn"])
    assert _parallel_start_method(in_xdist_worker=False, active_threads=1) == "spawn"


def test_catalog_models_are_not_collected_as_test_classes() -> None:
    assert CatalogCase.__test__ is False


def test_pytest_configuration_does_not_require_xdist() -> None:
    addopts = _pyproject()["tool"]["pytest"]["ini_options"].get("addopts", "")
    assert "-n" not in addopts
    assert "--dist" not in addopts


def test_worker_payload_carries_the_profile_unicode_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker gates exactly as the parent did, even for a cleared profile."""
    root = _fixture_repo(tmp_path)
    catalog = load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")
    case = catalog.cases["gate/case-001"]
    seen: list[str] = []

    def record(item: object, profile: SuiteProfile, **kwargs: object) -> CaseResult:
        seen.append(profile.feature_profile.unicode_version)
        return CaseResult.passed(case.name)

    monkeypatch.setattr("tests.xslt.suite.run_case", record)

    passed = (case.name, CaseResult.passed(case.name))
    assert _run_payload(("xslt30", "", case, None)) == passed
    assert seen == [""]
    assert _run_payload(("xslt30", "9.0", case, None)) == passed
    assert seen == ["", "9.0"]
    assert _run_payload(("xslt30", ENGINE_UNICODE_VERSION, case, None)) == passed
    assert seen == ["", "9.0", ENGINE_UNICODE_VERSION]

    # An unchanged version reuses the checked-in profile; another version keeps
    # the same processor identity and moves only the applicability claim.
    assert (
        with_unicode_version(SUITE_PROFILES["xslt30"], ENGINE_UNICODE_VERSION)
        is SUITE_PROFILES["xslt30"]
    )
    cleared = with_unicode_version(SUITE_PROFILES["xslt30"], "")
    assert cleared.feature_profile.unicode_version == ""
    assert cleared.processor is SUITE_PROFILES["xslt30"].processor


# ---------------------------------------------------------------------------
# Workflow and coverage posture


def test_workflow_runs_each_profile_with_the_pinned_corpus() -> None:
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True, {}))
    assert "pull_request" in triggers
    assert workflow["permissions"]["contents"] == "read"
    jobs = workflow["jobs"]
    assert set(jobs) == {"xslt30", "xslt20", "xslt10"}
    groups = set()
    for name, job in jobs.items():
        assert job["strategy"]["matrix"]["python-version"] == [
            "3.11",
            "3.12",
            "3.13",
            "3.14",
            "3.15",
        ]
        assert job["timeout-minutes"] >= 20
        steps = _steps(job)
        assert any(step.get("uses", "").startswith("actions/checkout@") for step in steps)
        assert any("git rev-parse HEAD:tests/xslt/corpus" in step.get("run", "") for step in steps)
        assert any(
            step.get("uses", "").startswith("actions/cache@")
            and "tests/xslt/corpus" in step.get("with", {}).get("path", "")
            and ".git/modules/tests/xslt/corpus" in step.get("with", {}).get("path", "")
            for step in steps
        )
        assert any(
            "git submodule update --init --depth 1 tests/xslt/corpus" in step.get("run", "")
            for step in steps
        )
        gates = [step for step in steps if "pytest tests/test_xslt_suite.py" in step.get("run", "")]
        assert len(gates) == 1
        assert gates[0]["env"]["PYXSD_RUN_XSLT"] == "1"
        assert gates[0]["env"]["PYXSD_XSLT_PROFILE"] == name
        assert gates[0]["env"]["PYXSD_XSLT_TIMEOUT"]
        assert job.get("concurrency", {}).get("group")
        groups.add(job["concurrency"]["group"])
    assert len(groups) == 3


def test_standalone_corpus_job_never_weakens_unit_coverage() -> None:
    config = _pyproject()
    assert _coverage_floor(config) >= COVERAGE_FLOOR

    ci = yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))
    unit_runs = [
        step.get("run", "") for step in _steps(ci["jobs"]["test"]) if "--cov" in step.get("run", "")
    ]
    assert any("--cov --cov-branch" in run for run in unit_runs)
    base_run = next(run for run in unit_runs if "--cov --cov-branch" in run)
    assert "-n 4" in base_run
    assert "--dist loadfile" in base_run

    for job in _workflow()["jobs"].values():
        for step in _steps(job):
            run = step.get("run", "")
            if "--cov" in run:
                assert "--cov-fail-under=0" in run


def test_the_coverage_pin_may_rise_but_never_fall() -> None:
    """The floor is a minimum; raising it over time must keep this contract."""
    for floor, meets in ((98, False), (99, True), (100, True)):
        document = {"tool": {"coverage": {"report": {"fail_under": floor}}}}
        assert (_coverage_floor(document) >= COVERAGE_FLOOR) is meets


def test_the_contract_declares_the_yaml_parser_it_imports() -> None:
    """PyYAML is imported here directly, so the dev group must promise it."""
    declared = _dependency_names(_pyproject()["dependency-groups"]["dev"])
    assert "pyyaml" in declared


def test_conformance_workflow_reacts_to_dependency_lock_changes() -> None:
    """A dependency-only change still exercises the conformance gate."""
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True, {}))
    assert "uv.lock" in triggers["pull_request"]["paths"]


# ---------------------------------------------------------------------------
# Development loop: family-filtered slices and the honesty rules


def test_filter_tokens_match_family_prefixes_and_exact_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_family_repo(tmp_path)
    for argv, expected in (
        # A trailing "/" selects the family; anything else is an exact id.
        (["--filter", "gate/"], 2),
        (["--filter", "gate/case-002"], 1),
        (["--filter", "gate/case-001,other/other-001"], 2),
        # Tokens are stripped and de-duplicated, order preserved.
        (["--filter", "gate/, gate/"], 2),
        (["--filter", " gate/case-001 , other/other-001 "], 2),
    ):
        assert _run(["--profile", "xslt30", "--list-only", *argv], root) == 0
        assert f"{expected} cases would run under profile xslt30" in capsys.readouterr().out

    # A prefix that is not a token (no trailing "/") is an exact id.
    assert _run(["--profile", "xslt30", "--list-only", "--filter", "gate/case-00"], root) == 2
    assert "gate/case-00" in capsys.readouterr().err

    document = _run_census(root, tmp_path / "census.json", ("--filter", "gate/,gate/case-001"))
    assert document["selection"]["filters"] == ["gate/", "gate/case-001"]
    assert [case["name"] for case in document["cases"]] == ["gate/case-001", "gate/case-002"]

    collapsed = _run_census(root, tmp_path / "collapsed.json", ("--filter", "gate/, gate/"))
    assert collapsed["selection"]["filters"] == ["gate/"]


def test_check_and_write_baseline_refuse_a_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_family_repo(tmp_path)
    _write_baseline(
        root,
        "xslt30",
        {"gate/case-001": "fail", "gate/case-002": "fail", "other/other-001": "fail"},
    )
    # The baseline is valid and complete here, so pre-change ``--check`` exits 0
    # and the only reason these arms exit 2 is the narrowing refusal.
    target = _baseline_path(root)
    assert _run(["--profile", "xslt30", "--check"], root) == 0
    capsys.readouterr()

    assert _run(["--profile", "xslt30", "--check", "--filter", "gate/case-001"], root) == 2
    error = capsys.readouterr().err
    assert "--check" in error and "--filter" in error and "cannot be narrowed" in error

    assert _run(["--profile", "xslt30", "--check", "--limit", "1"], root) == 2
    error = capsys.readouterr().err
    assert "--check" in error and "--limit" in error and "cannot be narrowed" in error

    never = tmp_path / "never" / "xslt30.toml"
    assert (
        _run(["--profile", "xslt30", "--write-baseline", str(never), "--filter", "gate/"], root)
        == 2
    )
    error = capsys.readouterr().err
    assert "--write-baseline" in error and "--filter" in error
    assert not never.exists()
    assert target.exists()

    census = tmp_path / "census.json"
    assert _run(["--profile", "xslt30", "--census", str(census), "--limit", "0"], root) == 2
    assert "--limit must be at least 1, got 0" in capsys.readouterr().err
    assert not census.exists()

    # A plain run has no mode to compare or report a slice.
    assert _run(["--profile", "xslt30", "--filter", "gate/"], root) == 2
    error = capsys.readouterr().err
    assert "--filter" in error and "--census" in error


def test_an_empty_filter_token_is_refused_before_any_catalog_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "bare-repo"
    root.mkdir()
    target = tmp_path / "census.json"

    assert _run(["--profile", "xslt30", "--census", str(target), "--filter", ""], root) == 2
    error = capsys.readouterr().err
    assert "token '' is empty" in error
    # The catalog error would say "cannot read XSLT catalog"; argument parsing
    # must refuse before the corpus is consulted at all.
    assert "catalog" not in error
    assert not target.exists()


def test_flag_help_and_docstring_name_every_refused_host(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        report_xslt.parse_args(["--help"])
    help_output = capsys.readouterr().out

    def description(flag: str) -> str:
        start = help_output.index(f"\n  {flag}")
        end = help_output.find("\n  --", start + 1)
        return " ".join(help_output[start : end if end != -1 else len(help_output)].split())

    def names(segment: str, host: str) -> bool:
        """True when *host* appears; argparse may wrap a long flag mid-hyphen."""
        return host.replace(" ", "") in "".join(segment.split())

    filter_help = description("--filter")
    for host in ("--check", "--write-baseline", "plain run"):
        assert names(filter_help, host)
    skip_help = description("--skip-timeouts")
    for host in ("--check", "--enforce", "--write-baseline", "--list-only", "plain run"):
        assert names(skip_help, host)

    docstring = " ".join(report_xslt.__doc__.split())
    assert "a plain run has no slice to compare or report" in docstring
    assert "a plain run has no census to report it" in docstring


def test_filtered_enforce_announces_the_slice_and_compares_only_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_family_repo(tmp_path)
    target = _write_baseline(root, "xslt30", {"gate/case-001": "fail"})

    # The whole-baseline gate still names both out-of-slice additions.
    assert _run(["--profile", "xslt30", "--enforce"], root) == 2
    error = capsys.readouterr().err
    assert "[added] gate/case-002" in error
    assert "[added] other/other-001" in error

    assert _run(["--profile", "xslt30", "--enforce", "--filter", "gate/case-001"], root) == 0
    assert "enforcing 1 of 1 cases" in capsys.readouterr().err

    sheet = root / "tests" / "xslt" / "corpus" / "gate" / "case-001.xsl"
    sheet.write_text(RIGHT_SHEET, encoding="utf-8")
    assert _run(["--profile", "xslt30", "--enforce", "--filter", "gate/case-001"], root) == 2
    assert "[improvement] gate/case-001" in capsys.readouterr().err

    # An out-of-slice removal disappears: it was never enforced.
    _write_baseline(root, "xslt30", {"gate/case-001": "fail", "other/other-001": "pass"})
    sheet.write_text(SHEET, encoding="utf-8")
    assert _run(["--profile", "xslt30", "--enforce", "--filter", "gate/case-001"], root) == 0
    error = capsys.readouterr().err
    assert "enforcing 1 of 2 cases" in error
    assert "other/other-001" not in error

    # A slice member absent from the baseline is added, not ignored.
    _write_baseline(root, "xslt30", {"other/other-001": "pass"})
    assert _run(["--profile", "xslt30", "--enforce", "--filter", "gate/case-001"], root) == 2
    assert "[added] gate/case-001" in capsys.readouterr().err

    # A limited enforce is the same kind of slice.
    _write_baseline(root, "xslt30", {"gate/case-001": "fail"})
    assert _run(["--profile", "xslt30", "--enforce", "--limit", "1"], root) == 0
    assert "enforcing 1 of 1 cases" in capsys.readouterr().err

    # The profile/corpus/budget preflight survives the slice.
    recorded = baseline_module.load_baseline(target)
    baseline_module.write_baseline(
        target, replace(recorded, meta={**recorded.meta, "corpus": "deadbeef"})
    )
    assert _run(["--profile", "xslt30", "--enforce", "--filter", "gate/case-001"], root) == 2
    assert "deadbeef" in capsys.readouterr().err


def test_skip_timeouts_is_accepted_only_for_census(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_family_repo(tmp_path)
    _write_baseline(root, "xslt30", {"gate/case-001": "timeout"})

    document = _run_census(root, tmp_path / "census.json", ("--skip-timeouts",))
    assert document["skipped"]["cases"] == ["gate/case-001"]
    assert document["selection"]["skip_timeouts"] is True
    assert [case["name"] for case in document["cases"]] == [
        "gate/case-002",
        "other/other-001",
    ]

    for argv, host in (
        (["--enforce"], "--enforce"),
        (["--check"], "--check"),
        (["--write-baseline", str(tmp_path / "baseline.toml")], "--write-baseline"),
        (["--list-only"], "--list-only"),
        ([], "plain run"),
    ):
        assert _run(["--profile", "xslt30", *argv, "--skip-timeouts"], root) == 2
        error = capsys.readouterr().err
        assert "--skip-timeouts" in error
        assert host in error
        assert "only valid with --census" in error
    assert not (tmp_path / "baseline.toml").exists()

    # A missing baseline is a refusal to read, before any case runs.
    missing_census = tmp_path / "missing.json"
    argv = [
        "--profile",
        "xslt30",
        "--census",
        str(missing_census),
        "--skip-timeouts",
        "--baseline",
        str(tmp_path / "absent.toml"),
    ]
    assert _run(argv, root) == 2
    assert "error:" in capsys.readouterr().err
    assert not missing_census.exists()


@pytest.mark.parametrize("dimension", ["profile", "corpus", "budget", "missing-budget"])
def test_skip_timeouts_refuses_a_baseline_of_another_identity(
    dimension: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_family_repo(tmp_path)
    path = _write_baseline(root, "xslt30", {"gate/case-001": "timeout"})
    recorded = baseline_module.load_baseline(path)
    if dimension == "profile":
        meta = {**recorded.meta, "profile": "xslt20"}
        expected = "profile"
    elif dimension == "corpus":
        meta = {**recorded.meta, "corpus": "deadbeef"}
        expected = "corpus"
    elif dimension == "budget":
        meta = {**recorded.meta, "timeout_seconds": 10}
        expected = "timeout budget"
    else:
        meta = {key: value for key, value in recorded.meta.items() if key != "timeout_seconds"}
        expected = "timeout budget"
    baseline_module.write_baseline(path, replace(recorded, meta=meta))

    target = tmp_path / "census.json"
    assert _run(["--profile", "xslt30", "--census", str(target), "--skip-timeouts"], root) == 2
    assert expected in capsys.readouterr().err
    assert not target.exists()


def test_a_partial_census_describes_its_selection(tmp_path: Path) -> None:
    root = _two_family_repo(tmp_path)

    filtered = _run_census(root, tmp_path / "filtered.json", ("--filter", "gate/case-002"))
    assert filtered["selection"] == {
        "filters": ["gate/case-002"],
        "limit": None,
        "skip_timeouts": False,
        "available": 3,
        "matched": 1,
        "selected": 1,
    }

    limited = _run_census(root, tmp_path / "limited.json", ("--limit", "1"))
    assert limited["selection"]["limit"] == 1
    assert limited["selection"]["filters"] == []
    assert (limited["selection"]["matched"], limited["selection"]["selected"]) == (3, 1)
    assert [case["name"] for case in limited["cases"]] == ["gate/case-001"]

    both = _run_census(root, tmp_path / "both.json", ("--filter", "gate/", "--limit", "1"))
    assert both["selection"] == {
        "filters": ["gate/"],
        "limit": 1,
        "skip_timeouts": False,
        "available": 3,
        "matched": 2,
        "selected": 1,
    }

    full = _run_census(root, tmp_path / "full.json")
    assert "selection" not in full
    assert "skipped" not in full

    # The metadata cannot lie about how many rows were measured.
    with pytest.raises(ValueError, match="measured"):
        census_module.build_census(
            (CaseResult.passed("a"),),
            profile="xslt30",
            corpus="pinned",
            timeout_seconds=5.0,
            selection=census_module.Selection(available=1, matched=1, selected=2),
        )


def test_milestone_runs_are_unfiltered_on_all_three_profiles(tmp_path: Path) -> None:
    rules = report_xslt.HONESTY_RULES
    assert len(rules) == 2
    assert all(rule.strip() for rule in rules)
    milestone, engine_touching = rules
    for profile in ("xslt30", "xslt20", "xslt10"):
        assert profile in milestone
    assert "--filter" in milestone
    assert "--skip-timeouts" in milestone
    assert "--skip-timeouts" in engine_touching
    docstring = " ".join(report_xslt.__doc__.split())
    assert "never --filter and never --skip-timeouts" in docstring

    # The mechanical half: the milestone instruments refuse narrowing.
    root = _two_family_repo(tmp_path)
    assert _run(["--profile", "xslt30", "--check", "--filter", "gate/"], root) == 2
    assert _run(["--profile", "xslt30", "--enforce", "--skip-timeouts"], root) == 2

    commands = [step.get("run", "") for job in _workflow()["jobs"].values() for step in _steps(job)]
    checks = [command for command in commands if "report_xslt.py" in command]
    for profile in ("xslt30", "xslt20", "xslt10"):
        assert any(f"--profile {profile} --check" in command for command in checks)
    for command in checks:
        assert "--filter" not in command
        assert "--skip-timeouts" not in command
        assert "--limit" not in command
