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
import re
import time
import tomllib
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
import yaml
from tests import report_xslt
from tests.xslt import baseline as baseline_module
from tests.xslt import report
from tests.xslt.catalog import load_catalog
from tests.xslt.model import Collection, Output, Package, Resource, Schema
from tests.xslt.outcomes import CaseResult, Outcome
from tests.xslt.selection import select_cases
from tests.xslt.suite import (
    SUITE_PROFILES,
    UnsupportedCase,
    case_budget,
    execute_case,
    run_case,
    run_parallel,
)

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


def test_check_rejects_profile_corpus_and_case_set_drift(tmp_path: Path) -> None:
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

    baseline_module.write_baseline(path, replace(recorded, cases={"gate/case-001": "bogus"}))
    assert _run(["--profile", "xslt30", "--check"], root) == 1


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
    root = _fixture_repo(tmp_path)
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
            ["--profile", "xslt30", "--filter", "case", "--write-baseline", str(target)],
            root,
        )
        == 2
    )
    assert _run(["--profile", "xslt30", "--enforce", "--write-baseline", str(target)], root) == 2


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
    source = case.environment.sources[0]
    shapes = {
        "resources": replace(
            case,
            environment=replace(
                case.environment,
                resources=(Resource(role="resource", file="r.txt"),),
            ),
        ),
        "collections": replace(
            case,
            environment=replace(
                case.environment,
                collections=(Collection(uri="urn:collection"),),
            ),
        ),
        "packages": replace(
            case,
            invocation=replace(
                case.invocation,
                packages=(Package(role="principal", path=PurePosixPath("pkg.xsl")),),
            ),
        ),
        "source schema": replace(
            case,
            environment=replace(
                case.environment,
                schemas=(Schema(role="source-reference"),),
            ),
        ),
        "schema-validated source": replace(
            case,
            environment=replace(
                case.environment,
                sources=(replace(source, validation="strict"),),
            ),
        ),
        "result-variable output": replace(
            case,
            invocation=replace(
                case.invocation,
                output=replace(
                    case.invocation.output or Output(),
                    result_var="result",
                ),
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
