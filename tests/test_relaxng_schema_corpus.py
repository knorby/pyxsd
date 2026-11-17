"""Schema-corpus acceptance, outcome classification, and release reporting.

The synthetic tests always run.  The pinned corpus gate is opt-in with
``PYXSD_RUN_RELAXNG=1`` because it compiles every schema assertion; set
``PYXSD_RELAXNG_TIMEOUT`` to change the per-case wall-clock budget.  Only a
matching compiler verdict satisfies a case: an unsupported datatype library,
a limit, a timeout, or a crash is an adapter gap, never a correct rejection.
"""

from __future__ import annotations

import json
import runpy
import signal
import sys
import threading
import time
from pathlib import Path

import pytest
from tests import report_relaxng
from tests.relaxng import driver as driver_module
from tests.relaxng.baseline import (
    Baseline,
    BaselineError,
    diff,
)
from tests.relaxng.baseline import load as load_baseline
from tests.relaxng.baseline import save as save_baseline
from tests.relaxng.catalog import SPECTEST_FILE
from tests.relaxng.dispositions import (
    Disposition,
    DispositionError,
    load_dispositions,
    release_violations,
)
from tests.relaxng.driver import CaseFailure, CaseResult, PyXSDDriver
from tests.relaxng.model import (
    PROFILE_COMPACT_SYNTAX,
    PROFILE_CORE,
    PROFILE_DTD_COMPATIBILITY,
    PROFILE_XSD_DATATYPE,
    CorpusCase,
    Outcome,
    corpus_enabled,
)

from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng.datatypes import UnsupportedDatatypeError
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.schema import RelaxNGSchema

NS = "http://relaxng.org/ns/structure/1.0"
CORPUS_AVAILABLE = all(
    (report_relaxng.CORPUS / name).is_file() for name in ("spectest.xml", "xsdtest.xml")
)
CORRECT = f'<element xmlns="{NS}" name="doc"><empty/></element>'
INCORRECT = f'<element xmlns="{NS}"><empty/></element>'
UNKNOWN_LIBRARY = (
    f'<element xmlns="{NS}" name="doc">'
    '<data type="thing" datatypeLibrary="urn:unregistered"/>'
    "</element>"
)


def write_case(
    tmp_path: Path,
    case_id: str,
    kind: str,
    text: str = CORRECT,
    profile: str = PROFILE_CORE,
) -> CorpusCase:
    path = tmp_path / f"{case_id}.rng"
    path.write_text(text, encoding="utf-8")
    return CorpusCase(
        id=case_id,
        kind=kind,  # type: ignore[arg-type]
        schema=path,
        instance=None,
        profile=profile,
    )


def write_instance_case(
    tmp_path: Path,
    case_id: str,
    kind: str,
    text: str = CORRECT,
    profile: str = PROFILE_CORE,
) -> CorpusCase:
    schema = tmp_path / f"{case_id}.rng"
    schema.write_text(text, encoding="utf-8")
    instance = tmp_path / f"{case_id}.xml"
    instance.write_text("<doc/>", encoding="utf-8")
    return CorpusCase(
        id=case_id,
        kind=kind,  # type: ignore[arg-type]
        schema=schema,
        instance=instance,
        profile=profile,
    )


def make_result(
    case: CorpusCase,
    outcome: Outcome,
    failure: CaseFailure,
    *,
    detail: str | None = None,
) -> CaseResult:
    expected = case.kind == "correct-schema" if case.kind.endswith("schema") else None
    actual = expected if outcome is Outcome.PASS else None
    if outcome is Outcome.FAIL and expected is not None:
        actual = not expected
    return CaseResult(
        case=case,
        outcome=outcome,
        failure=failure,
        expected_valid=expected,
        actual_valid=actual,
        detail=detail,
    )


def disposition(
    entry_id: str,
    category: str,
    cases: tuple[str, ...],
    *,
    feature: str | None = None,
    links: tuple[str, ...] = (),
) -> Disposition:
    return Disposition(
        id=entry_id,
        title=entry_id,
        category=category,
        cases=cases,
        rationale="because the catalog says so",
        feature=feature,
        links=links,
    )


def compile_text(tmp_path: Path, text: str) -> RelaxNGSchema:
    path = tmp_path / "schema.rng"
    path.write_text(text, encoding="utf-8")
    return RelaxNGSchema.compile(path)


def codes(schema: RelaxNGSchema) -> set[str]:
    return {issue.code for issue in schema.report.issues}


# ---------------------------------------------------------------------------
# Driver classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "text"),
    [("correct-schema", CORRECT), ("incorrect-schema", INCORRECT)],
)
def test_driver_matches_correct_and_incorrect_schema_verdicts(tmp_path, kind, text):
    case = write_case(tmp_path, kind, kind, text)

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.PASS
    assert result.failure is CaseFailure.NONE
    assert result.expected_valid is (kind == "correct-schema")
    assert result.actual_valid is result.expected_valid
    assert result.detail is None
    assert result.satisfies_expectation


@pytest.mark.parametrize(
    ("kind", "text", "detail"),
    [
        ("correct-schema", INCORRECT, "expected a correct schema"),
        ("incorrect-schema", CORRECT, "expected an incorrect schema"),
    ],
)
def test_driver_reports_a_wrong_verdict(tmp_path, kind, text, detail):
    case = write_case(tmp_path, kind, kind, text)

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.FAIL
    assert result.failure is CaseFailure.WRONG_VERDICT
    assert result.actual_valid is (not result.expected_valid)
    assert detail in (result.detail or "")
    assert not result.satisfies_expectation


def test_driver_classifies_a_compiler_limit_as_a_gap(tmp_path):
    case = write_case(tmp_path, "limit", "correct-schema")

    result = PyXSDDriver(
        timeout=None,
        limits=RelaxNGLimits(max_pattern_nodes=0),
    ).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.LIMIT
    assert (result.detail or "").startswith("limit:")
    assert result.actual_valid is None
    assert not result.satisfies_expectation


def test_driver_classifies_an_unknown_datatype_as_a_gap(tmp_path):
    case = write_case(tmp_path, "gap", "incorrect-schema", UNKNOWN_LIBRARY)

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.ADAPTER_GAP
    assert "UnsupportedDatatypeError" in (result.detail or "")
    # A rejected incorrect schema must not be credited from a capability gap.
    assert not result.satisfies_expectation


def test_driver_classifies_an_unexpected_exception_as_a_crash(tmp_path):
    case = write_case(tmp_path, "crash", "correct-schema")

    def explode(path, *, limits=None):
        del path, limits
        raise RuntimeError("boom")

    result = PyXSDDriver(timeout=None, compiler=explode).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.CRASH
    assert result.detail == "RuntimeError: boom"
    assert not result.satisfies_expectation


def test_driver_classifies_another_pyxsd_error_as_an_adapter_gap(tmp_path):
    case = write_case(tmp_path, "refusal", "correct-schema")

    def refuse(path, *, limits=None):
        del path, limits
        raise PyXSDError("unsupported capability")

    result = PyXSDDriver(timeout=None, compiler=refuse).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.ADAPTER_GAP
    assert result.detail == "PyXSDError: unsupported capability"


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="no SIGALRM on this platform")
def test_driver_classifies_a_timeout_as_a_gap(tmp_path):
    case = write_case(tmp_path, "timeout", "correct-schema")

    def slow(path, *, limits=None):
        time.sleep(0.2)
        return RelaxNGSchema.compile(path, limits=limits)

    result = PyXSDDriver(timeout=0.01, compiler=slow).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.TIMEOUT
    assert (result.detail or "").startswith("timeout:")
    assert not result.satisfies_expectation


def test_time_limit_runs_without_an_alarm_facility(tmp_path, monkeypatch):
    case = write_case(tmp_path, "no-alarm", "correct-schema")

    def slow(path, *, limits=None):
        time.sleep(0.02)
        return RelaxNGSchema.compile(path, limits=limits)

    monkeypatch.setattr(driver_module, "_alarm_available", lambda: False)
    result = PyXSDDriver(timeout=0.001, compiler=slow).run(case)

    assert result.outcome is Outcome.PASS


def test_time_limit_is_skipped_off_the_main_thread(tmp_path):
    case = write_case(tmp_path, "worker-thread", "correct-schema")
    results: list[Outcome] = []
    failures: list[BaseException] = []

    def slow(path, *, limits=None):
        time.sleep(0.02)
        return RelaxNGSchema.compile(path, limits=limits)

    def work() -> None:
        try:
            results.append(PyXSDDriver(timeout=0.001, compiler=slow).run(case).outcome)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(timeout=10)

    assert failures == []
    assert results == [Outcome.PASS]


def test_driver_validates_instance_assertions(tmp_path):
    case = write_instance_case(tmp_path, "instance", "valid-instance")

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.PASS
    assert result.failure is CaseFailure.NONE
    assert result.phase == "instance"
    assert result.expected_valid is True
    assert result.actual_valid is True
    assert result.satisfies_expectation


# ---------------------------------------------------------------------------
# Baseline updates and dispositions
# ---------------------------------------------------------------------------


def test_baseline_update_cannot_remove_or_downgrade_a_pass(tmp_path):
    path = tmp_path / "baseline.toml"
    save_baseline(
        path,
        {"keep": "pass", "drop": "pass", "fail": "fail"},
        {"profile": "core"},
    )
    previous = load_baseline(path)

    violations = report_relaxng.baseline_update_violations(
        previous,
        {"keep": "pass", "drop": "fail", "fail": "pass", "new": "fail"},
    )
    assert [(item.case_id, item.current) for item in violations] == [("drop", "fail")]

    missing = report_relaxng.baseline_update_violations(previous, {"keep": "pass"})
    assert [(item.case_id, item.current) for item in missing] == [("drop", None)]
    # Only a previous pass is protected; removing a failing entry is allowed.
    assert "fail" not in {item.case_id for item in violations}

    with pytest.raises(BaselineError, match="refusing to remove"):
        report_relaxng.save_baseline_checked(
            path,
            {"keep": "pass", "drop": "fail", "fail": "pass"},
        )
    assert load_baseline(path).results == {
        "keep": "pass",
        "drop": "pass",
        "fail": "fail",
    }

    report_relaxng.save_baseline_checked(
        path,
        {"keep": "pass", "drop": "pass", "fail": "pass"},
        {"profile": "core"},
    )
    assert load_baseline(path).results == {
        "keep": "pass",
        "drop": "pass",
        "fail": "pass",
    }

    fresh = tmp_path / "fresh.toml"
    report_relaxng.save_baseline_checked(fresh, {"new": "adapter-gap"})
    assert load_baseline(fresh).results == {"new": "adapter-gap"}


def test_every_case_has_exactly_one_disposition(tmp_path):
    core = write_case(tmp_path, "core", "correct-schema")
    xsd = write_case(tmp_path, "xsd", "correct-schema", profile=PROFILE_XSD_DATATYPE)
    compact = write_case(tmp_path, "compact", "correct-schema", profile=PROFILE_COMPACT_SYNTAX)
    dtd = write_case(tmp_path, "dtd", "incorrect-schema", profile=PROFILE_DTD_COMPATIBILITY)
    dispositions = (
        disposition(
            "compact-note",
            PROFILE_COMPACT_SYNTAX,
            (compact.id,),
            feature=PROFILE_COMPACT_SYNTAX,
        ),
        disposition(
            "dtd-note",
            PROFILE_DTD_COMPATIBILITY,
            (dtd.id,),
            feature=PROFILE_DTD_COMPATIBILITY,
        ),
        disposition("core-note", PROFILE_CORE, (core.id,)),
    )

    assignments = report_relaxng.case_dispositions((core, xsd, compact, dtd), dispositions)
    assert assignments == {
        "core": PROFILE_CORE,
        "xsd": PROFILE_XSD_DATATYPE,
        "compact": PROFILE_COMPACT_SYNTAX,
        "dtd": PROFILE_DTD_COMPATIBILITY,
    }

    with pytest.raises(DispositionError, match="no feature disposition"):
        report_relaxng.case_dispositions((core, compact), ())


def test_excluded_dependencies_are_sorted_and_carry_their_feature(tmp_path):
    compact = write_case(tmp_path, "compact", "correct-schema", profile=PROFILE_COMPACT_SYNTAX)
    dtd = write_case(tmp_path, "dtd", "incorrect-schema", profile=PROFILE_DTD_COMPATIBILITY)
    dispositions = (
        disposition(
            "z-compact",
            PROFILE_COMPACT_SYNTAX,
            (compact.id,),
            feature=PROFILE_COMPACT_SYNTAX,
            links=("https://relaxng.org/compact.html",),
        ),
        disposition(
            "a-dtd",
            PROFILE_DTD_COMPATIBILITY,
            (dtd.id,),
            feature=PROFILE_DTD_COMPATIBILITY,
        ),
    )

    dependencies = report_relaxng.excluded_dependencies((compact, dtd), dispositions)

    assert [dependency.entry_id for dependency in dependencies] == ["a-dtd", "z-compact"]
    assert dependencies[1].feature == PROFILE_COMPACT_SYNTAX
    assert dependencies[1].links == ("https://relaxng.org/compact.html",)
    assert report_relaxng.excluded_dependencies((dtd,), (dispositions[1],))[0].cases == (dtd.id,)


@pytest.mark.skipif(
    not CORPUS_AVAILABLE,
    reason="the pinned RELAX NG catalogs are checkout-only",
)
def test_checked_in_dispositions_cover_every_schema_case_without_exclusions():
    cases = report_relaxng.schema_cases(report_relaxng.load_cases())
    dispositions = load_dispositions(report_relaxng.DISPOSITIONS)

    assignments = report_relaxng.case_dispositions(cases, dispositions)

    assert set(assignments) == {case.id for case in cases}
    assert len(assignments) == len(cases)
    assert report_relaxng.excluded_dependencies(cases, dispositions) == ()


# ---------------------------------------------------------------------------
# Deterministic report
# ---------------------------------------------------------------------------


def report_fixture(tmp_path):
    passed = write_case(tmp_path, "passed", "correct-schema")
    wrong = write_case(tmp_path, "wrong", "incorrect-schema")
    gap = write_case(tmp_path, "gap", "incorrect-schema")
    compact = write_case(tmp_path, "compact", "correct-schema", profile=PROFILE_COMPACT_SYNTAX)
    results = (
        make_result(passed, Outcome.PASS, CaseFailure.NONE),
        make_result(
            wrong,
            Outcome.FAIL,
            CaseFailure.WRONG_VERDICT,
            detail="expected an incorrect schema, observed no schema errors",
        ),
        make_result(
            gap,
            Outcome.ADAPTER_GAP,
            CaseFailure.ADAPTER_GAP,
            detail="UnsupportedDatatypeError: no such library",
        ),
        make_result(
            compact,
            Outcome.ADAPTER_GAP,
            CaseFailure.ADAPTER_GAP,
            detail="compact syntax is outside the profile",
        ),
    )
    baseline = Baseline(
        results={
            passed.id: "pass",
            wrong.id: "fail",
            gap.id: "adapter-gap",
        }
    )
    dispositions = (
        disposition(
            "compact-note",
            PROFILE_COMPACT_SYNTAX,
            (compact.id,),
            feature=PROFILE_COMPACT_SYNTAX,
            links=("https://relaxng.org/compact.html",),
        ),
    )
    return (passed, wrong, gap, compact), results, baseline, dispositions


def test_report_lists_every_unresolved_failure_and_exclusion(tmp_path):
    cases, results, baseline, dispositions = report_fixture(tmp_path)

    text = report_relaxng.render_report(cases, results, baseline, dispositions, deferred=3)

    assert text == report_relaxng.render_report(cases, results, baseline, dispositions, deferred=3)
    assert "scope: 3 required schema assertions" in text
    assert "excluded schema assertions: 1" in text
    assert "deferred instance assertions: 3" in text
    assert "dispositions: 4 schema cases assigned exactly one" in text
    assert "unresolved in-scope failures (2):" in text
    assert f"  {cases[1].id} [incorrect-schema] fail (wrong-verdict)" in text
    assert f"  {cases[2].id} [incorrect-schema] adapter-gap (adapter-gap)" in text
    assert "compact-note [compact-syntax] compact-note" in text
    assert "https://relaxng.org/compact.html" in text
    assert "baseline: matches" in text


def test_report_shows_baseline_changes_without_hiding_failures(tmp_path):
    cases, results, _, dispositions = report_fixture(tmp_path)
    baseline = Baseline(results={cases[0].id: "pass"})

    text = report_relaxng.render_report(cases, results, baseline, dispositions)

    assert "baseline changes (2):" in text
    assert "[new] gap: missing -> adapter-gap" in text
    assert "[new] wrong: missing -> fail" in text
    assert "unresolved in-scope failures (2):" in text


def test_json_report_has_the_same_per_case_evidence(tmp_path):
    cases, results, baseline, dispositions = report_fixture(tmp_path)

    payload = json.loads(
        report_relaxng.render_json(cases, results, baseline, dispositions, deferred=7)
    )

    assert payload["scope"] == {
        "core": 3,
        "deferred_instance_assertions": 7,
        "excluded_schema_cases": 1,
        "required_schema_cases": 3,
        "xsd_datatype": 0,
    }
    assert set(payload["results"]) == {case.id for case in cases if case.is_required}
    assert payload["results"][cases[1].id]["failure"] == "wrong-verdict"
    assert [failure["case"] for failure in payload["unresolved"]] == [
        cases[2].id,
        cases[1].id,
    ]
    assert payload["excluded"][0]["feature"] == PROFILE_COMPACT_SYNTAX
    assert payload["baseline_changes"] == []


def test_report_omits_absent_failure_detail_and_empty_links(tmp_path):
    failed = write_case(tmp_path, "failed", "incorrect-schema")
    dtd = write_case(tmp_path, "dtd", "incorrect-schema", profile=PROFILE_DTD_COMPATIBILITY)
    results = (make_result(failed, Outcome.ADAPTER_GAP, CaseFailure.ADAPTER_GAP),)
    dispositions = (
        disposition(
            "dtd-note",
            PROFILE_DTD_COMPATIBILITY,
            (dtd.id,),
            feature=PROFILE_DTD_COMPATIBILITY,
        ),
    )

    text = report_relaxng.render_report((failed, dtd), results, Baseline(), dispositions)

    assert "failed [incorrect-schema] adapter-gap (adapter-gap)\n" in text
    assert "links:" not in text
    assert "baseline changes (1):" in text


# ---------------------------------------------------------------------------
# Report command line
# ---------------------------------------------------------------------------

_TWO_CASE_CATALOG = f"""\
<testSuite>
  <testCase>
    <correct><element xmlns="{NS}" name="doc"><empty/></element></correct>
    <valid><doc/></valid>
  </testCase>
  <testCase>
    <incorrect><element xmlns="{NS}"><empty/></element></incorrect>
  </testCase>
</testSuite>
"""

_ONE_UNEXPECTED_CORRECT_CATALOG = f"""\
<testSuite>
  <testCase>
    <correct><element xmlns="{NS}"><empty/></element></correct>
  </testCase>
  <testCase>
    <incorrect><element xmlns="{NS}"><empty/></element></incorrect>
  </testCase>
</testSuite>
"""


def test_report_cli_writes_enforces_and_refuses_regressions(tmp_path, capsys):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / SPECTEST_FILE).write_text(_TWO_CASE_CATALOG, encoding="utf-8")
    (root / "xsdtest.xml").write_text("<xsdtest/>", encoding="utf-8")
    dispositions = tmp_path / "dispositions.toml"
    dispositions.write_text("", encoding="utf-8")
    baseline = tmp_path / "baseline.toml"
    common = [
        "--root",
        str(root),
        "--dispositions",
        str(dispositions),
        "--baseline",
        str(baseline),
    ]

    assert report_relaxng.main([*common, "--list-only"]) == 0
    assert "2 schema cases would run" in capsys.readouterr().out

    assert report_relaxng.main([*common, "--write-baseline", str(baseline), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"]["required_schema_cases"] == 2
    assert {entry["outcome"] for entry in payload["results"].values()} == {"pass"}

    assert report_relaxng.main([*common, "--enforce"]) == 0
    assert "unresolved in-scope failures: none" in capsys.readouterr().out

    # Make the first case a correct schema that the compiler rejects.  The
    # observation flips from pass to fail, which the baseline must refuse.
    (root / SPECTEST_FILE).write_text(_ONE_UNEXPECTED_CORRECT_CATALOG, encoding="utf-8")
    assert report_relaxng.main([*common, "--enforce"]) == 1
    report = capsys.readouterr().out
    assert "unresolved in-scope failures (1):" in report
    assert "spectest-0001 [correct-schema] fail (wrong-verdict)" in report

    assert report_relaxng.main([*common, "--write-baseline", str(baseline)]) == 2
    assert "refusing to remove" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        report_relaxng.main([*common, "--timeout", "0"])


def test_report_cli_rejects_bad_dispositions_and_baselines(tmp_path, capsys):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / SPECTEST_FILE).write_text(_TWO_CASE_CATALOG, encoding="utf-8")
    (root / "xsdtest.xml").write_text("<xsdtest/>", encoding="utf-8")
    dispositions = tmp_path / "dispositions.toml"
    dispositions.write_text("[[dispositions]]\nid = 'x'\n", encoding="utf-8")
    baseline = tmp_path / "baseline.toml"

    assert (
        report_relaxng.main(
            [
                "--root",
                str(root),
                "--dispositions",
                str(dispositions),
                "--baseline",
                str(baseline),
            ]
        )
        == 2
    )
    assert "must be a string" in capsys.readouterr().err

    dispositions.write_text("", encoding="utf-8")
    baseline.write_text("not toml", encoding="utf-8")
    assert (
        report_relaxng.main(
            [
                "--root",
                str(root),
                "--dispositions",
                str(dispositions),
                "--baseline",
                str(baseline),
            ]
        )
        == 2
    )
    assert "malformed baseline" in capsys.readouterr().err


def test_report_script_entry_point_runs_as_a_script(tmp_path, monkeypatch, capsys):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / SPECTEST_FILE).write_text(_TWO_CASE_CATALOG, encoding="utf-8")
    (root / "xsdtest.xml").write_text("<xsdtest/>", encoding="utf-8")
    dispositions = tmp_path / "dispositions.toml"
    dispositions.write_text("", encoding="utf-8")
    baseline = tmp_path / "baseline.toml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report_relaxng.py",
            "--root",
            str(root),
            "--dispositions",
            str(dispositions),
            "--baseline",
            str(baseline),
            "--list-only",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(
            str(Path(report_relaxng.__file__)),
            run_name="__main__",
        )

    assert excinfo.value.code == 0
    assert "2 schema cases would run" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Compiler defects exposed by the pinned corpus
# ---------------------------------------------------------------------------


def test_duplicate_except_clauses_are_schema_errors(tmp_path):
    schema = compile_text(
        tmp_path,
        f"""<element xmlns="{NS}">
          <anyName>
            <except><name>a</name></except>
            <except><name>b</name></except>
          </anyName>
          <empty/>
        </element>""",
    )
    assert "relaxng-duplicate-except" in codes(schema)

    accepted = compile_text(
        tmp_path,
        f"""<element xmlns="{NS}">
          <anyName><except><name>a</name></except></anyName>
          <empty/>
        </element>""",
    )
    assert not accepted.report.has_errors


@pytest.mark.parametrize(
    ("library", "valid"),
    [
        ("", True),
        ("foobar:xyzzy", True),
        ("http:ok", True),
        ("foo:bar", True),
        ("http://www.example.com/%Aa", True),
        ("urn:example:lib", True),
        ("foo_bar:xyzzy", False),
        ("foo:", False),
        ("http://www.example.com/%", False),
        ("http://www.example.com/%xx", False),
        ("xyzzy", False),
        ("xyzzy#foo:bar", False),
        ("xyzzy?foo:bar", False),
        ("xyzzy/foo:bar", False),
        ("http://www.example.com#xyzzy", False),
        ("http://www.example.com#", False),
        # RELAX NG section 4.3 applies XLink 5.4 escaping first, so a space
        # or non-ASCII character is a legal escaped identity.
        ("http://www.example.com/a b", True),
        ("http://www.example.com/\u00e9", True),
    ],
)
def test_datatype_library_uris_follow_rfc2396(tmp_path, library, valid):
    schema = compile_text(
        tmp_path,
        f'<element xmlns="{NS}" name="doc" datatypeLibrary="{library}"><empty/></element>',
    )
    present = "relaxng-invalid-datatype-library" in codes(schema)
    assert present is (not valid)


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("doc", True),
        ("\u0e14\u0e35", True),
        ("a.b-c_d", True),
        ("x:y", True),
        ("", False),
        ("\u0e35", False),
        ("x:", False),
        (":x", False),
        ("x:y:z", False),
        ("x y", False),
    ],
)
def test_names_are_xml_names_and_ncnames(tmp_path, name, valid):
    schema = compile_text(
        tmp_path,
        f'<element xmlns="{NS}" xmlns:x="urn:example:x" name="{name}"><empty/></element>',
    )
    assert ("relaxng-invalid-name" in codes(schema)) is (not valid)


def test_reference_and_definition_names_are_ncnames(tmp_path):
    schema = compile_text(
        tmp_path,
        f"""<grammar xmlns="{NS}" xmlns:x="urn:example:x">
          <start><ref name="x:y"/></start>
          <define name="x:y"><element name="doc"><empty/></element></define>
        </grammar>""",
    )
    assert "relaxng-invalid-name" in codes(schema)
    assert schema.report.has_errors


def test_name_and_value_elements_reject_element_children(tmp_path):
    name_schema = compile_text(
        tmp_path,
        f"""<element xmlns="{NS}">
          <name>doc<eg:comment xmlns:eg="urn:example:comment"/></name>
          <empty/>
        </element>""",
    )
    assert "relaxng-invalid-name-content" in codes(name_schema)

    value_schema = compile_text(
        tmp_path,
        f"""<element xmlns="{NS}" name="doc">
          <value>text<eg:comment xmlns:eg="urn:example:comment"/></value>
        </element>""",
    )
    assert "relaxng-invalid-value-content" in codes(value_schema)

    annotated_data = compile_text(
        tmp_path,
        f"""<element xmlns="{NS}" name="doc">
          <data type="string"><eg:comment xmlns:eg="urn:example:comment"/></data>
        </element>""",
    )
    assert not annotated_data.report.has_errors


@pytest.mark.parametrize(
    "body",
    [
        '<data type="decimal"/>',
        '<value type="tok">x</value>',
    ],
)
def test_unknown_builtin_datatypes_are_schema_errors(tmp_path, body):
    schema = compile_text(
        tmp_path,
        f'<element xmlns="{NS}" name="doc">{body}</element>',
    )
    assert "relaxng-unknown-datatype" in codes(schema)


@pytest.mark.parametrize(
    "body",
    [
        '<data type="thing" datatypeLibrary="urn:unregistered"/>',
        '<value type="thing" datatypeLibrary="urn:unregistered">x</value>',
    ],
)
def test_unregistered_datatype_libraries_remain_capability_gaps(tmp_path, body):
    path = tmp_path / "schema.rng"
    path.write_text(
        f'<element xmlns="{NS}" name="doc">{body}</element>',
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedDatatypeError, match="urn:unregistered"):
        RelaxNGSchema.compile(path)


def test_environment_timeout_must_be_positive(monkeypatch, capsys):
    monkeypatch.setenv("PYXSD_RELAXNG_TIMEOUT", "0")

    with pytest.raises(ValueError, match="positive"):
        report_relaxng.timeout_from_environment()
    with pytest.raises(SystemExit):
        report_relaxng.main(["--list-only"])
    assert "--timeout must be positive" in capsys.readouterr().err

    monkeypatch.setenv("PYXSD_RELAXNG_TIMEOUT", "12.5")
    assert report_relaxng.timeout_from_environment() == 12.5


# ---------------------------------------------------------------------------
# The pinned corpus gate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not CORPUS_AVAILABLE or not corpus_enabled(),
    reason="set PYXSD_RUN_RELAXNG=1 and check out the pinned catalogs",
)
def test_pinned_schema_corpus_matches_the_checked_in_baseline():
    timeout = report_relaxng.timeout_from_environment()
    all_cases = report_relaxng.load_cases()
    cases = report_relaxng.schema_cases(all_cases)
    required = report_relaxng.required_schema_cases(cases)
    dispositions = load_dispositions(report_relaxng.DISPOSITIONS)
    baseline = load_baseline(report_relaxng.BASELINE)

    results = report_relaxng.run_cases(required, timeout=timeout)
    outcomes = report_relaxng.results_to_keys(results)
    failures = report_relaxng.unresolved_failures(results)

    assert not failures, "\n".join(
        f"{failure.case.id} [{failure.case.kind}] {failure.outcome.value}"
        f" ({failure.failure.value}): {failure.detail or ''}"
        for failure in failures
    )
    assert set(outcomes) == {case.id for case in required}
    assert set(baseline.results) == {case.id for case in required}
    assert set(baseline.results.values()) == {"pass"}
    assert diff(baseline, outcomes).changes == ()
    assert (
        release_violations(
            {case.id: case for case in cases},
            baseline,
            dispositions,
        )
        == ()
    )

    report = report_relaxng.render_report(
        cases,
        results,
        baseline,
        dispositions,
        deferred=len(report_relaxng.deferred_instance_cases(all_cases)),
    )
    assert "unresolved in-scope failures: none" in report
    assert "excluded-feature dependencies: none" in report
