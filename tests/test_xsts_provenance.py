"""Run provenance recorded in XSTS JSON reports and written baselines."""

from __future__ import annotations

import json
import platform
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import report_xsts
from xsts import baseline, provenance
from xsts.outcomes import Outcome
from xsts.runner import Case, CaseResult

SOURCE_REVISION = "a" * 40
CORPUS_REVISION = "b" * 40
FIXED_NOW = datetime(2026, 10, 31, 18, 22, 33, 500000, tzinfo=UTC)

#: The canonical full-run arguments every collect_provenance test starts from.
FULL_RUN = {
    "profile": "xsd11",
    "suite_file": "suite.xml",
    "case_count": 41735,
    "complete": True,
    "engines": ["pyxsd", "xmlschema"],
    "timeout": 180.0,
    "jobs": 8,
    "command": "python tests/report_xsts.py --profile xsd11",
    "now": FIXED_NOW,
}

FULL_PROVENANCE = {
    "profile": "xsd11",
    "suite_file": "suite.xml",
    "case_count": 41735,
    "complete": True,
    "engines": ["pyxsd", "xmlschema"],
    "timeout_seconds": 180.0,
    "jobs": 8,
    "command": "python tests/report_xsts.py --profile xsd11",
    "source_revision": SOURCE_REVISION,
    "corpus_revision": CORPUS_REVISION,
    "python_version": "3.13.2",
    "platform": "Darwin-24.4.0-arm64",
    "engine_versions": ["elementpath==5.4.0", "pyxsd==1.2.3", "xmlschema==4.7.0"],
    "run_completed_at": "2026-10-31T18:22:33.500000+00:00",
}


def _pin_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every environment probe provenance reads with fixed values."""
    monkeypatch.setattr(
        provenance,
        "_git_revision",
        lambda ref: {"HEAD": SOURCE_REVISION, "HEAD:tests/xsts/corpus": CORPUS_REVISION}[ref],
    )
    monkeypatch.setattr(
        provenance,
        "_distribution_version",
        lambda name: {"pyxsd": "1.2.3", "elementpath": "5.4.0", "xmlschema": "4.7.0"}[name],
    )
    monkeypatch.setattr(provenance, "_python_version", lambda: "3.13.2")
    monkeypatch.setattr(provenance, "_platform_summary", lambda: "Darwin-24.4.0-arm64")
    monkeypatch.setattr(
        provenance, "_current_time", lambda: datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    )


def _collect(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> dict[str, object]:
    _pin_environment(monkeypatch)
    return provenance.collect_provenance(**{**FULL_RUN, **overrides})


# --- collect_provenance -----------------------------------------------------------


def test_collect_provenance_records_the_full_run(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = _collect(monkeypatch)

    assert meta == FULL_PROVENANCE
    # Serialization order is fixed so reports and baselines are diff-stable.
    assert list(meta) == [
        "profile",
        "suite_file",
        "case_count",
        "complete",
        "engines",
        "timeout_seconds",
        "jobs",
        "command",
        "source_revision",
        "corpus_revision",
        "python_version",
        "platform",
        "engine_versions",
        "run_completed_at",
    ]


def test_collect_provenance_records_partial_runs_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = _collect(
        monkeypatch,
        profile="xsd10",
        case_count=12,
        complete=False,
        engines=["pyxsd"],
        timeout=60.0,
        jobs=1,
        command="python tests/report_xsts.py --profile xsd10 --limit 12",
    )

    assert meta["complete"] is False
    assert meta["profile"] == "xsd10"
    assert meta["case_count"] == 12
    assert meta["engines"] == ["pyxsd"]
    assert meta["timeout_seconds"] == 60.0


def test_engine_versions_include_xmlschema_only_when_selected_and_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = _collect(monkeypatch)
    assert installed["engine_versions"] == [
        "elementpath==5.4.0",
        "pyxsd==1.2.3",
        "xmlschema==4.7.0",
    ]

    unselected = _collect(monkeypatch, engines=["pyxsd"])
    assert unselected["engine_versions"] == ["elementpath==5.4.0", "pyxsd==1.2.3"]

    # Selected but not installed: the optional engine is omitted entirely,
    # while the two hard dependencies stay recorded.
    monkeypatch.setattr(
        provenance,
        "_distribution_version",
        lambda name: {"pyxsd": "1.2.3", "elementpath": "5.4.0", "xmlschema": None}[name],
    )
    missing = provenance.collect_provenance(**{**FULL_RUN, "engines": ["pyxsd", "xmlschema"]})
    assert missing["engine_versions"] == ["elementpath==5.4.0", "pyxsd==1.2.3"]


def test_a_supplied_now_never_consults_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_environment(monkeypatch)

    def forbidden() -> datetime:
        raise AssertionError("the clock must not be read when now is supplied")

    monkeypatch.setattr(provenance, "_current_time", forbidden)

    meta = provenance.collect_provenance(**FULL_RUN)

    assert meta["run_completed_at"] == "2026-10-31T18:22:33.500000+00:00"


def test_without_now_the_timestamp_comes_from_the_clock_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = _collect(monkeypatch, now=None)

    assert meta["run_completed_at"] == "2026-10-31T12:00:00+00:00"


def test_run_completed_at_normalizes_to_iso8601_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    offset = timezone(timedelta(hours=2))

    aware = _collect(monkeypatch, now=datetime(2026, 10, 31, 20, 0, 0, tzinfo=offset))
    naive = _collect(monkeypatch, now=datetime(2026, 10, 31, 18, 0, 0))

    assert aware["run_completed_at"] == "2026-10-31T18:00:00+00:00"
    # A naive datetime is interpreted as UTC rather than local time.
    assert naive["run_completed_at"] == "2026-10-31T18:00:00+00:00"


# --- environment helpers ----------------------------------------------------------


def test_git_revision_resolves_refs_from_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=f"{SOURCE_REVISION}\n", stderr="")

    monkeypatch.setattr(provenance.subprocess, "run", fake_run)

    assert provenance._git_revision("HEAD") == SOURCE_REVISION
    command, kwargs = calls[0]
    assert command == ["git", "rev-parse", "HEAD"]
    assert kwargs["cwd"] == provenance.REPO_ROOT
    assert kwargs["check"] is True


def test_git_revision_degrades_when_git_cannot_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(128, command)

    monkeypatch.setattr(provenance.subprocess, "run", failing)
    assert provenance._git_revision("HEAD") == "unknown"

    def absent(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git is not installed")

    monkeypatch.setattr(provenance.subprocess, "run", absent)
    assert provenance._git_revision("HEAD:tests/xsts/corpus") == "unknown"


def test_distribution_version_reports_missing_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provenance.importlib.metadata, "version", lambda name: "9.9.9")
    assert provenance._distribution_version("pyxsd") == "9.9.9"

    def missing(name: str) -> str:
        raise provenance.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(provenance.importlib.metadata, "version", missing)
    assert provenance._distribution_version("xmlschema") is None


def test_interpreter_helpers_report_the_running_environment() -> None:
    # These two have no seam by design: they publish what the interpreter says.
    assert provenance._python_version() == platform.python_version()
    assert provenance._platform_summary() == platform.platform()


# --- CLI plumbing -----------------------------------------------------------------


def test_reproducible_command_rebuilds_the_raw_argument_list() -> None:
    assert report_xsts.reproducible_command([]) == "python tests/report_xsts.py"
    assert (
        report_xsts.reproducible_command(["--profile", "xsd11", "--limit", "5"])
        == "python tests/report_xsts.py --profile xsd11 --limit 5"
    )


def test_reproducible_command_quotes_arguments_that_need_it() -> None:
    command = report_xsts.reproducible_command(["--filter", "particles Z"])

    assert command == "python tests/report_xsts.py --filter 'particles Z'"


def _case(index: int) -> Case:
    return Case(
        test_id=f"set{index}/group/case{index}",
        set_name=f"set{index}",
        contributor="W3C",
        group_name="group",
        kind="instance",
        applicable=True,
        schema_documents=(),
        corpus_root=PurePosixPath("/corpus"),
    )


def _pass(case: Case, engine: str) -> CaseResult:
    return CaseResult(
        test_id=case.test_id,
        set_name=case.set_name,
        contributor=case.contributor,
        group_name=case.group_name,
        kind=case.kind,
        engine=engine,
        outcome=Outcome.PASS,
        expected="valid",
        actual=True,
    )


class _FakeRunner:
    """Both engines pass every case; oracle rows only when an oracle was built."""

    def __init__(self, *, profile: object, driver: object, oracle: object, workdir: Path) -> None:
        self.oracle = oracle

    def run_case(self, case: Case) -> list[CaseResult]:
        results = [_pass(case, "pyxsd")]
        if self.oracle is not None:
            results.append(_pass(case, "xmlschema"))
        return results


def _patch_runner(monkeypatch: pytest.MonkeyPatch, cases: list[Case]) -> None:
    monkeypatch.setattr(report_xsts, "corpus_available", lambda: True)
    monkeypatch.setattr(report_xsts, "load_default_catalog", lambda: object())
    monkeypatch.setattr(report_xsts, "build_cases", lambda catalog, profile: list(cases))
    monkeypatch.setattr(report_xsts, "configure_logging", lambda: None)
    monkeypatch.setattr(report_xsts, "PyXSDDriver", lambda **kwargs: SimpleNamespace(name="pyxsd"))
    monkeypatch.setattr(
        report_xsts, "XmlSchemaDriver", lambda *args, **kwargs: SimpleNamespace(name="xmlschema")
    )
    monkeypatch.setattr(report_xsts, "Runner", _FakeRunner)


def test_main_publishes_one_provenance_block_to_json_and_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pin_environment(monkeypatch)
    _patch_runner(monkeypatch, [_case(index) for index in (1, 2, 3)])
    path = tmp_path / "baseline.toml"
    raw = [
        "--profile",
        "xsd11",
        "--timeout",
        "180",
        "--jobs",
        "1",
        "--write-baseline",
        str(path),
        "--json",
    ]

    code = report_xsts.main(raw)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    expected = dict(FULL_PROVENANCE)
    expected.update(
        case_count=3,
        jobs=1,
        command=(
            f"python tests/report_xsts.py --profile xsd11 --timeout 180 --jobs 1 "
            f"--write-baseline {path} --json"
        ),
        run_completed_at="2026-10-31T12:00:00+00:00",
    )
    assert payload["provenance"] == expected
    # The baseline records the same metadata, and every value survives TOML.
    assert baseline.load(path).meta == payload["provenance"]
    assert {key.split(":")[0] for key in baseline.load(path).results} == {"pyxsd", "xmlschema"}


def test_main_marks_a_limited_run_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pin_environment(monkeypatch)
    _patch_runner(monkeypatch, [_case(index) for index in (1, 2, 3)])
    path = tmp_path / "baseline.toml"

    code = report_xsts.main(
        [
            "--profile",
            "xsd11",
            "--limit",
            "2",
            "--jobs",
            "1",
            "--write-baseline",
            str(path),
            "--json",
        ]
    )

    assert code == 0
    recorded = json.loads(capsys.readouterr().out)["provenance"]
    assert recorded["complete"] is False
    assert recorded["case_count"] == 2
    assert "--limit 2" in recorded["command"]
    assert baseline.load(path).meta["complete"] is False


def test_main_marks_a_filtered_run_partial(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pin_environment(monkeypatch)
    _patch_runner(monkeypatch, [_case(index) for index in (1, 2, 3)])

    code = report_xsts.main(["--profile", "xsd11", "--filter", "case3", "--jobs", "1", "--json"])

    assert code == 0
    recorded = json.loads(capsys.readouterr().out)["provenance"]
    assert recorded["complete"] is False
    assert recorded["case_count"] == 1
    # The run is still a both-engine run; only its scope was narrowed.
    assert recorded["engines"] == ["pyxsd", "xmlschema"]


def test_main_engine_selection_narrows_recorded_engines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pin_environment(monkeypatch)
    _patch_runner(monkeypatch, [_case(1)])

    code = report_xsts.main(["--profile", "xsd11", "--engine", "pyxsd", "--jobs", "1", "--json"])

    assert code == 0
    recorded = json.loads(capsys.readouterr().out)["provenance"]
    assert recorded["complete"] is True
    assert recorded["engines"] == ["pyxsd"]
    assert recorded["engine_versions"] == ["elementpath==5.4.0", "pyxsd==1.2.3"]


def test_main_oracle_only_run_still_records_the_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pin_environment(monkeypatch)
    _patch_runner(monkeypatch, [_case(1)])

    code = report_xsts.main(
        ["--profile", "xsd11", "--engine", "xmlschema", "--jobs", "1", "--json"]
    )

    assert code == 0
    recorded = json.loads(capsys.readouterr().out)["provenance"]
    assert recorded["engines"] == ["xmlschema"]
    # The hard dependencies stay recorded even when only the oracle was run.
    assert recorded["engine_versions"] == [
        "elementpath==5.4.0",
        "pyxsd==1.2.3",
        "xmlschema==4.7.0",
    ]
