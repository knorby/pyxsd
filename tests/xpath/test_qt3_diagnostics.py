"""Tests for the pinned, catalog-driven QT3 XPath diagnostics.

Synthetic catalogs pin selection and assertion behavior so the ordinary test
suite never depends on the QT3 checkout.  The real-corpus test runs only when
the pinned submodule is present and reports substrate facts separately from
every XSLT baseline.
"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import elementpath
import pytest
import tests.xpath.qt3 as qt3
from tests.xpath.qt3 import (
    QT3_STATUSES,
    QT3_TEST_SETS,
    Qt3CaseResult,
    Qt3CorpusError,
    Qt3Report,
    load_catalog,
    render_json,
    render_text,
    run_qt3_diagnostics,
)

NAMESPACE = qt3.CATALOG_NS
REAL_CORPUS = Path(__file__).resolve().parents[1] / "xpath" / "corpus"


def _case(
    name: str,
    test: str,
    result: str,
    *,
    dependency: str = "",
    environment: str = "",
) -> str:
    return (
        f'<test-case name="{name}">{dependency}{environment}'
        f"<test>{escape(test)}</test><result>{result}</result></test-case>"
    )


def write_corpus(
    root: Path,
    cases: list[str],
    *,
    catalog_environments: str = "",
    test_set_environments: str = "",
    test_set_dependency: str = '<dependency type="spec" value="XP31+"/>',
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "catalog.xml").write_text(
        f"""<catalog xmlns="{NAMESPACE}" test-suite="synthetic" version="3.1">
        <environment name="empty"/>
        {catalog_environments}
        <test-set name="probe-set" file="probe-set.xml"/>
        </catalog>""",
        encoding="utf-8",
    )
    (root / "probe-set.xml").write_text(
        f"""<test-set xmlns="{NAMESPACE}" name="probe-set">
        {test_set_dependency}
        {test_set_environments}
        {"".join(cases)}
        </test-set>""",
        encoding="utf-8",
    )
    return root


def statuses(report: Qt3Report) -> dict[str, str]:
    return {case.name: case.status for case in report.cases}


def run_synthetic(root: Path, version: str = "3.1") -> Qt3Report:
    return run_qt3_diagnostics(version, corpus=root, test_sets=("probe-set",))


def messages(report: Qt3Report) -> dict[str, str]:
    return {case.name: case.message for case in report.cases}


def test_synthetic_catalog_reports_every_status(tmp_path: Path) -> None:
    root = write_corpus(
        tmp_path / "corpus",
        [
            _case("pass", "1 + 2", "<assert-eq>3</assert-eq>"),
            _case("defect", "1 + 2", "<assert-eq>4</assert-eq>"),
            _case("error-pass", "xs:integer('x')", '<error code="FORG0001"/>'),
            _case("wrong-error", "xs:integer('x')", '<error code="XPTY0004"/>'),
            _case("unexpected-error", "xs:integer('x')", "<assert-eq>1</assert-eq>"),
            _case("missing-error", "1 + 2", '<error code="FOAR0002"/>'),
            _case("context-sensitive", ".", "<assert-eq>1</assert-eq>"),
            _case("context-error", ".", '<error code="XPDY0002"/>'),
            _case(
                "elementpath-context-limited", "let $x := 1 return $x", "<assert-eq>1</assert-eq>"
            ),
            _case(
                "version",
                "1 + 2",
                "<assert-eq>3</assert-eq>",
                dependency='<dependency type="spec" value="XP30"/>',
            ),
            _case(
                "language",
                "1 + 2",
                "<assert-eq>3</assert-eq>",
                dependency='<dependency type="spec" value="XQ31"/>',
            ),
            _case(
                "feature",
                "1 + 2",
                "<assert-eq>3</assert-eq>",
                dependency='<dependency type="feature" value="staticTyping"/>',
            ),
            _case("unsupported", "1", "<assert-xml>&lt;a/&gt;</assert-xml>"),
            _case(
                "source",
                "1",
                "<assert-eq>1</assert-eq>",
                environment='<environment ref="works"/>',
            ),
            _case(
                "missing-environment",
                "1",
                "<assert-eq>1</assert-eq>",
                environment='<environment ref="absent"/>',
            ),
            _case(
                "context-item",
                "1",
                "<assert-eq>1</assert-eq>",
                environment='<environment ref="context"/>',
            ),
        ],
        catalog_environments=(
            '<environment name="works"><source role="." file="works.xml"/></environment>'
            '<environment name="context"><context-item select="1"/></environment>'
        ),
    )

    report = run_synthetic(root)
    by_name = statuses(report)

    assert by_name["probe-set/pass"] == "pass"
    assert by_name["probe-set/defect"] == "defect"
    assert by_name["probe-set/error-pass"] == "pass"
    assert by_name["probe-set/wrong-error"] == "defect"
    assert by_name["probe-set/unexpected-error"] == "defect"
    assert "FORG0001" in messages(report)["probe-set/unexpected-error"]
    assert by_name["probe-set/missing-error"] == "defect"
    assert by_name["probe-set/context-sensitive"] == "adapter-requirement"
    assert "absent context item" in messages(report)["probe-set/context-sensitive"]
    assert by_name["probe-set/context-error"] == "pass"
    assert by_name["probe-set/elementpath-context-limited"] == "adapter-requirement"
    assert "absent context item" in messages(report)["probe-set/elementpath-context-limited"]
    assert by_name["probe-set/version"] == "untested"
    assert by_name["probe-set/language"] == "untested"
    assert by_name["probe-set/feature"] == "untested"
    assert by_name["probe-set/unsupported"] == "adapter-requirement"
    assert by_name["probe-set/source"] == "adapter-requirement"
    assert by_name["probe-set/missing-environment"] == "adapter-requirement"
    assert by_name["probe-set/context-item"] == "adapter-requirement"
    assert report.summary()["pass"] == 3
    assert sum(report.summary().values()) == len(report.cases)
    assert report.parser == "XPath31Parser"
    assert report.parser_version == "3.1"
    assert report.evaluator == "elementpath"
    assert report.evaluator_version == elementpath.__version__
    assert report.corpus_commit == ""
    assert "XPTY0004" in messages(report)["probe-set/wrong-error"]
    assert "FOAR0002" in messages(report)["probe-set/missing-error"]


def test_spec_dependencies_select_versions_and_last_one_wins(tmp_path: Path) -> None:
    root = write_corpus(
        tmp_path / "corpus",
        [
            _case("default", "1", "<assert-eq>1</assert-eq>"),
            _case(
                "xp20",
                "1",
                "<assert-eq>1</assert-eq>",
                dependency='<dependency type="spec" value="XP20"/>',
            ),
            _case(
                "xp20plus",
                "1",
                "<assert-eq>1</assert-eq>",
                dependency='<dependency type="spec" value="XP20+"/>',
            ),
            _case(
                "xp30",
                "1",
                "<assert-eq>1</assert-eq>",
                dependency='<dependency type="spec" value="XP30"/>',
            ),
            _case(
                "xquery",
                "1",
                "<assert-eq>1</assert-eq>",
                dependency='<dependency type="spec" value="XQ31"/>',
            ),
            _case(
                "xt",
                "1",
                "<assert-eq>1</assert-eq>",
                dependency='<dependency type="spec" value="XT30"/>',
            ),
            _case(
                "invalid-version",
                "1",
                "<assert-eq>1</assert-eq>",
                dependency='<dependency type="spec" value="XPAB"/>',
            ),
            _case(
                "other-language",
                "1",
                "<assert-eq>1</assert-eq>",
                dependency='<dependency type="spec" value="FT30"/>',
            ),
        ],
    )

    version_20 = statuses(run_synthetic(root, "2.0"))
    version_30 = statuses(run_synthetic(root, "3.0"))
    version_31 = statuses(run_synthetic(root, "3.1"))

    assert version_20 == {
        "probe-set/default": "untested",
        "probe-set/xp20": "pass",
        "probe-set/xp20plus": "pass",
        "probe-set/xp30": "untested",
        "probe-set/xquery": "untested",
        "probe-set/xt": "untested",
        "probe-set/invalid-version": "untested",
        "probe-set/other-language": "untested",
    }
    assert version_30["probe-set/xp20"] == "untested"
    assert version_30["probe-set/xp20plus"] == "pass"
    assert version_30["probe-set/xp30"] == "pass"
    assert version_30["probe-set/default"] == "untested"
    assert version_31["probe-set/default"] == "pass"
    assert version_31["probe-set/xp30"] == "untested"
    assert version_31["probe-set/xquery"] == "untested"
    assert version_31["probe-set/xt"] == "untested"
    assert version_31["probe-set/invalid-version"] == "untested"
    assert version_31["probe-set/other-language"] == "pass"


def test_catalog_and_test_set_environments_merge_namespaces(tmp_path: Path) -> None:
    root = write_corpus(
        tmp_path / "corpus",
        [
            _case(
                "map",
                "map:size(map{'a': 1})",
                "<assert-eq>1</assert-eq>",
                environment='<environment ref="map-env"/>',
            )
        ],
        catalog_environments=(
            '<environment name="map-env"><namespace prefix="map" uri="urn:wrong"/></environment>'
        ),
        test_set_environments=(
            '<environment name="map-env">'
            '<namespace prefix="map" uri="http://www.w3.org/2005/xpath-functions/map"/>'
            "</environment>"
        ),
    )

    report = run_synthetic(root)

    assert statuses(report)["probe-set/map"] == "pass"


def test_assertion_vocabulary_is_evaluated(tmp_path: Path) -> None:
    cases = [
        _case("true", "true()", "<assert-true/>"),
        _case("false", "false()", "<assert-false/>"),
        _case("empty", "()", "<assert-empty/>"),
        _case("count", "(1, 2, 3)", "<assert-count>3</assert-count>"),
        _case("type", "1", "<assert-type>xs:integer</assert-type>"),
        _case("eq", "2", "<assert-eq>2.0</assert-eq>"),
        _case("deep-eq", "(1, 2)", "<assert-deep-eq>(1, 2)</assert-deep-eq>"),
        _case("string", "(1, 2)", "<assert-string-value>1 2</assert-string-value>"),
        _case("assert", "3", "<assert>$result = 3</assert>"),
        _case(
            "all-of",
            "true()",
            "<all-of><assert-true/><assert-eq>true()</assert-eq></all-of>",
        ),
        _case("any-of", "2", "<any-of><assert-eq>1</assert-eq><assert-eq>2</assert-eq></any-of>"),
        _case("not", "2", "<not><assert-eq>1</assert-eq></not>"),
        _case(
            "error-any-of",
            "1 + 2",
            '<any-of><assert-eq>3</assert-eq><error code="XPTY0004"/></any-of>',
        ),
        _case(
            "error-any-of-raised",
            "xs:integer('x')",
            '<any-of><assert-eq>3</assert-eq><error code="FORG0001"/></any-of>',
        ),
        _case("error-wildcard", "xs:integer('x')", '<error code="*"/>'),
    ]
    root = write_corpus(tmp_path / "corpus", cases)

    report = run_synthetic(root)

    assert statuses(report) == {
        f"probe-set/{name}": "pass"
        for name in (
            "true",
            "false",
            "empty",
            "count",
            "type",
            "eq",
            "deep-eq",
            "string",
            "assert",
            "all-of",
            "any-of",
            "not",
            "error-any-of",
            "error-any-of-raised",
            "error-wildcard",
        )
    }


def test_assertion_evaluation_failure_is_a_defect(tmp_path: Path) -> None:
    root = write_corpus(
        tmp_path / "corpus",
        [_case("bad-type", "1", "<assert-type>not a sequence type</assert-type>")],
    )

    report = run_synthetic(root)

    assert statuses(report)["probe-set/bad-type"] == "defect"
    assert "assertion" in messages(report)["probe-set/bad-type"]


def test_failed_and_unsupported_nested_assertions_are_explicit(tmp_path: Path) -> None:
    root = write_corpus(
        tmp_path / "corpus",
        [
            _case(
                "failing-all-of",
                "1",
                "<all-of><assert-eq>1</assert-eq><assert-eq>2</assert-eq></all-of>",
            ),
            _case(
                "nested-error",
                "1 + 2",
                '<all-of><assert-eq>4</assert-eq><error code="FOAR0002"/></all-of>',
            ),
            _case(
                "unsupported-any-of",
                "2",
                "<any-of><assert-eq>1</assert-eq><assert-xml>&lt;a/&gt;</assert-xml></any-of>",
            ),
            _case(
                "unsupported-not",
                "2",
                "<not><assert-xml>&lt;a/&gt;</assert-xml></not>",
            ),
            _case("unknown", "1", "<assert-foo>1</assert-foo>"),
        ],
    )

    report = run_synthetic(root)

    assert statuses(report)["probe-set/failing-all-of"] == "defect"
    assert statuses(report)["probe-set/nested-error"] == "defect"
    assert "FOAR0002" in messages(report)["probe-set/nested-error"]
    assert statuses(report)["probe-set/unsupported-any-of"] == "adapter-requirement"
    assert "assert-xml" in messages(report)["probe-set/unsupported-any-of"]
    assert statuses(report)["probe-set/unsupported-not"] == "adapter-requirement"
    assert "assert-xml" in messages(report)["probe-set/unsupported-not"]
    assert statuses(report)["probe-set/unknown"] == "adapter-requirement"


def test_unclassified_dependency_is_untested(tmp_path: Path) -> None:
    root = write_corpus(
        tmp_path / "corpus",
        [_case("no-spec", "1", "<assert-eq>1</assert-eq>")],
        test_set_dependency="",
    )

    report = run_synthetic(root)

    assert statuses(report)["probe-set/no-spec"] == "untested"
    assert "spec dependency" in messages(report)["probe-set/no-spec"]


def test_namespace_declarations_and_incomplete_environment_entries(tmp_path: Path) -> None:
    root = write_corpus(
        tmp_path / "corpus",
        [
            _case(
                "qualified",
                "map:size(map{'a': 1})",
                "<assert-eq>1</assert-eq>",
                environment='<environment ref="partial"/>',
            )
        ],
        test_set_environments=(
            '<environment name="partial">'
            '<namespace prefix="" uri="urn:ignored"/>'
            '<namespace prefix="unused" uri=""/>'
            "</environment>"
        ),
    )
    (root / "probe-set.xml").write_text(
        f"""<test-set xmlns="{NAMESPACE}" xmlns:map="http://www.w3.org/2005/xpath-functions/map"
          map:marker="1" name="probe-set">
        <dependency type="spec" value="XP31+"/>
        <environment name="partial">
          <namespace prefix="" uri="urn:ignored"/>
          <namespace prefix="unused" uri=""/>
        </environment>
        <test-case name="qualified">
          <environment ref="partial"/>
          <test>map:size(map{{'a': 1}})</test>
          <result><assert-eq>1</assert-eq></result>
        </test-case>
        </test-set>""",
        encoding="utf-8",
    )

    report = run_synthetic(root)

    assert statuses(report)["probe-set/qualified"] == "pass"


def test_corpus_revision_handles_git_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        qt3.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("git missing")),
    )
    assert qt3._corpus_revision(tmp_path) == ""

    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(qt3.subprocess, "run", lambda *args, **kwargs: Result())
    assert qt3._corpus_revision(tmp_path) == ""


def test_empty_test_set_reference_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(Qt3CorpusError, match="requires a file"):
        qt3._resolve_reference(tmp_path, tmp_path, "")
    catalog = tmp_path / "catalog.xml"
    catalog.write_text(
        f'<catalog xmlns="{NAMESPACE}"><test-set name="x" file=""/></catalog>',
        encoding="utf-8",
    )
    with pytest.raises(Qt3CorpusError, match="invalid QT3 test-set"):
        load_catalog(catalog)


def test_error_labels_fall_back_to_the_exception_type() -> None:
    assert qt3._error_label(RuntimeError("boom")) == "RuntimeError"


def test_context_item_analysis_is_static_and_variable_aware() -> None:
    from elementpath.xpath3 import XPath31Parser

    parser = XPath31Parser()
    assert not qt3._accepts_absent_context_error(None)
    for expression in (
        ".",
        "a",
        "./a",
        ".//a",
        "child::a",
        "count(a)",
        "position()",
        "last()",
        "/out",
        "root()",
        "[a]",
    ):
        assert qt3._uses_context_item(parser.parse(expression)), expression
    for expression in (
        "1 + 2",
        "[]",
        "$result/a",
        "$result//a",
        "$result/a[1]",
        "count($result/a)",
        "1 instance of xs:integer",
        "xs:integer('1')",
        "$result instance of array(*)",
        "$result instance of map(*)",
        "let $x := 1 return $x",
    ):
        assert not qt3._uses_context_item(parser.parse(expression)), expression


def test_assertion_that_requires_a_context_item_is_an_adapter_requirement(
    tmp_path: Path,
) -> None:
    root = write_corpus(
        tmp_path / "corpus",
        [_case("assert-context", "1", "<assert>. = 1</assert>")],
    )

    report = run_synthetic(root)

    assert statuses(report)["probe-set/assert-context"] == "adapter-requirement"
    assert "assertion requires" in messages(report)["probe-set/assert-context"]


def test_missing_result_is_an_adapter_requirement(tmp_path: Path) -> None:
    root = write_corpus(tmp_path / "corpus", [])
    (root / "probe-set.xml").write_text(
        f'<test-set xmlns="{NAMESPACE}" name="probe-set">'
        '<dependency type="spec" value="XP31+"/>'
        '<test-case name="no-result"><test>1</test></test-case>'
        "</test-set>",
        encoding="utf-8",
    )

    report = run_synthetic(root)

    assert statuses(report)["probe-set/no-result"] == "adapter-requirement"


def test_empty_result_is_an_adapter_requirement(tmp_path: Path) -> None:
    root = write_corpus(tmp_path / "corpus", [])
    (root / "probe-set.xml").write_text(
        f'<test-set xmlns="{NAMESPACE}" name="probe-set">'
        '<dependency type="spec" value="XP31+"/>'
        '<test-case name="empty-result"><test>1</test><result/></test-case>'
        "</test-set>",
        encoding="utf-8",
    )

    report = run_synthetic(root)

    assert statuses(report)["probe-set/empty-result"] == "adapter-requirement"


def test_missing_and_malformed_catalogs_report_the_checkout_command(tmp_path: Path) -> None:
    with pytest.raises(Qt3CorpusError, match="git submodule update"):
        load_catalog(tmp_path / "absent")

    malformed = tmp_path / "malformed.xml"
    malformed.write_text("<catalog>", encoding="utf-8")
    with pytest.raises(Qt3CorpusError, match="not well formed"):
        load_catalog(malformed)

    wrong_root = tmp_path / "wrong.xml"
    wrong_root.write_text("<not-catalog/>", encoding="utf-8")
    with pytest.raises(Qt3CorpusError, match="not a QT3 catalog"):
        load_catalog(wrong_root)


def test_catalog_structure_errors_are_explicit(tmp_path: Path) -> None:
    missing_file = tmp_path / "missing-file.xml"
    missing_file.write_text(
        f'<catalog xmlns="{NAMESPACE}"><test-set name="x"/></catalog>', encoding="utf-8"
    )
    with pytest.raises(Qt3CorpusError, match="invalid QT3 test-set"):
        load_catalog(missing_file)

    duplicate = tmp_path / "duplicate.xml"
    duplicate.write_text(
        f'<catalog xmlns="{NAMESPACE}">'
        '<test-set name="x" file="a.xml"/><test-set name="x" file="b.xml"/>'
        "</catalog>",
        encoding="utf-8",
    )
    with pytest.raises(Qt3CorpusError, match="duplicate"):
        load_catalog(duplicate)

    absolute = tmp_path / "absolute.xml"
    absolute.write_text(
        f'<catalog xmlns="{NAMESPACE}"><test-set name="x" file="/etc/passwd"/></catalog>',
        encoding="utf-8",
    )
    with pytest.raises(Qt3CorpusError, match="must be relative"):
        load_catalog(absolute)

    escaping = tmp_path / "escaping.xml"
    escaping.write_text(
        f'<catalog xmlns="{NAMESPACE}"><test-set name="x" file="../outside.xml"/></catalog>',
        encoding="utf-8",
    )
    with pytest.raises(Qt3CorpusError, match="escapes the corpus"):
        load_catalog(escaping)


def test_missing_test_set_file_is_explicit(tmp_path: Path) -> None:
    root = write_corpus(tmp_path / "corpus", [])
    (root / "probe-set.xml").unlink()

    with pytest.raises(Qt3CorpusError, match="probe-set"):
        run_synthetic(root)


def test_unknown_test_set_and_invalid_version_are_rejected(tmp_path: Path) -> None:
    root = write_corpus(tmp_path / "corpus", [])

    with pytest.raises(Qt3CorpusError, match="does not declare"):
        run_qt3_diagnostics("3.1", corpus=root, test_sets=("missing-set",))
    with pytest.raises(ValueError, match="at least one"):
        run_qt3_diagnostics("3.1", corpus=root, test_sets=())
    with pytest.raises(ValueError, match="XPath version"):
        run_qt3_diagnostics("1.0", corpus=root)


def test_report_validation_and_rendering(tmp_path: Path) -> None:
    root = write_corpus(
        tmp_path / "corpus",
        [
            _case("pass", "1", "<assert-eq>1</assert-eq>"),
            _case("defect", "1", "<assert-eq>2</assert-eq>"),
        ],
    )
    report = run_synthetic(root)

    document = render_json([report])
    parsed = json.loads(document)
    assert parsed["schema"] == 1
    assert parsed["suite"] == "qt3-diagnostics"
    assert parsed["reports"][0]["suite"] == "qt3"
    assert parsed["reports"][0]["summary"] == {"defect": 1, "pass": 1}
    assert parsed["reports"][0]["cases"]["probe-set/pass"]["status"] == "pass"

    text = render_text([report])
    assert "QT3 XPath 3.1" in text
    assert "probe-set/defect: defect" in text
    assert "probe-set/pass: pass" not in text

    with pytest.raises(ValueError, match="status"):
        Qt3CaseResult("case", "set", "maybe")
    with pytest.raises(ValueError, match="case name"):
        Qt3CaseResult("", "set", "pass")
    with pytest.raises(ValueError, match="unique"):
        Qt3Report("3.1", "P", "3.1", "elementpath", "5.1.4", "", (report.cases[0], report.cases[0]))
    assert set(report.summary()) <= QT3_STATUSES


def test_qt3_harness_does_not_import_the_xslt_harness() -> None:
    source = Path(qt3.__file__).read_text(encoding="utf-8")

    assert "tests.xslt" not in source
    assert "baseline" not in source.replace("baselines", "")


def test_main_and_module_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = write_corpus(
        tmp_path / "corpus",
        [_case("pass", "1", "<assert-eq>1</assert-eq>")],
    )

    assert (
        qt3.main(
            ["--versions", "3.0", "3.1", "--corpus", str(root), "--test-set", "probe-set", "--json"]
        )
        == 0
    )
    parsed = json.loads(capsys.readouterr().out)
    assert [report["xpath_version"] for report in parsed["reports"]] == ["3.0", "3.1"]

    assert qt3.main(["--versions", "3.1", "--corpus", str(root), "--test-set", "probe-set"]) == 0
    assert "QT3 XPath 3.1" in capsys.readouterr().out

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tests.xpath.qt3",
            "--versions",
            "3.1",
            "--corpus",
            str(root),
            "--test-set",
            "probe-set",
            "--json",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(Path(qt3.__file__)), run_name="__main__")
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["suite"] == "qt3-diagnostics"


@pytest.fixture(scope="module")
def real_reports() -> dict[str, Qt3Report]:
    if not (REAL_CORPUS / "catalog.xml").is_file():
        pytest.skip(f"QT3 corpus absent; run {qt3.CORPUS_COMMAND!r}")
    return {
        version: run_qt3_diagnostics(version, corpus=REAL_CORPUS)
        for version in ("2.0", "3.0", "3.1")
    }


def test_real_corpus_is_pinned_and_reported_separately(
    real_reports: dict[str, Qt3Report],
) -> None:
    for version, report in real_reports.items():
        assert report.xpath_version == version
        assert report.corpus_commit == "201a6e466940cdfc727f4babfedcde5332b9f578"
        assert report.evaluator_version == elementpath.__version__
        assert report.to_dict()["suite"] == "qt3"
        assert set(report.summary()) <= QT3_STATUSES
        assert sum(report.summary().values()) == len(report.cases)
        assert report.summary().get("pass", 0) > 0
        assert report.summary().get("untested", 0) > 0
        if version == "3.1":
            assert report.summary().get("adapter-requirement", 0) > 0
            assert report.summary().get("defect", 0) > 0


def test_real_corpus_pins_observed_substrate_defects(
    real_reports: dict[str, Qt3Report],
) -> None:
    latest = statuses(real_reports["3.1"])

    assert latest["prod-InlineFunctionExpr/inline-fn-005"] == "pass"
    assert latest["map-size/map-size-012"] == "defect"
    assert latest["fn-apply/fn-apply-13"] == "defect"
    assert "fn-function-lookup/fn-function-lookup-701" in latest
    assert real_reports["3.1"].summary()["defect"] >= 3


def test_qt3_test_sets_do_not_overlap_the_xslt_catalog() -> None:
    assert "prod-MapConstructor" in QT3_TEST_SETS
    assert not any(name.startswith("xslt") for name in QT3_TEST_SETS)
