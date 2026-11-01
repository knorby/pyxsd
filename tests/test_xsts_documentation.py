"""Byte-deterministic XSTS documentation fragments.

The generator reads the checked-in baselines, the pinned main ``suite.xml``
catalogue, and the reviewed dispositions, then renders MyST.  These tests
pin the numbers a miniature fixture produces so the tables cannot silently
change shape, and they prove that publication rejects partial or malformed
baseline metadata instead of presenting it as a full run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import report_xsts_docs
from xsts.dispositions import DispositionError
from xsts.documentation import (
    PublicationError,
    build_headline_fragment,
    build_results_fragment,
    format_agreement,
)

REMOVE = object()

SUITE_XML = """\
<?xml version="1.0"?>
<testSuite xmlns="http://www.w3.org/XML/2004/xml-schema-test-suite/"
           xmlns:xlink="http://www.w3.org/1999/xlink"
           name="Miniature" releaseDate="2026-10-31" version="1.0 1.1">
  <testSetRef xlink:href="mini.testSet"/>
</testSuite>
"""

TEST_SET = """\
<?xml version="1.0"?>
<testSet xmlns="http://www.w3.org/XML/2004/xml-schema-test-suite/"
         xmlns:xlink="http://www.w3.org/1999/xlink"
         name="Mini" contributor="Fixture">
  <testGroup name="g1">
    <schemaTest name="s1">
      <schemaDocument xlink:href="g1/s1.xsd"/>
      <expected validity="valid"/>
    </schemaTest>
    <instanceTest name="i1">
      <instanceDocument xlink:href="g1/i1.xml"/>
      <expected version="1.0" validity="invalid"/>
      <expected version="1.1" validity="valid"/>
    </instanceTest>
  </testGroup>
  <testGroup name="g2">
    <schemaTest name="s2">
      <schemaDocument xlink:href="g2/s2.xsd"/>
      <expected validity="valid"/>
    </schemaTest>
    <instanceTest name="i2">
      <instanceDocument xlink:href="g2/i2.xml"/>
      <expected validity="valid"/>
    </instanceTest>
  </testGroup>
  <testGroup name="g3">
    <schemaTest name="s3">
      <schemaDocument xlink:href="g3/s3.xsd"/>
      <expected version="1.0" validity="valid"/>
      <expected version="1.1" validity="invalid"/>
    </schemaTest>
    <instanceTest name="i3">
      <instanceDocument xlink:href="g3/i3.xml"/>
      <expected validity="invalid"/>
    </instanceTest>
  </testGroup>
  <testGroup name="g4">
    <schemaTest name="s4">
      <schemaDocument xlink:href="g4/s4.xsd"/>
      <expected validity="invalid"/>
    </schemaTest>
    <instanceTest name="i4">
      <instanceDocument xlink:href="g4/i4.xml"/>
      <expected validity="invalid"/>
    </instanceTest>
  </testGroup>
  <testGroup name="g5">
    <instanceTest name="i5">
      <instanceDocument xlink:href="g5/i5.xml"/>
      <expected version="1.0" validity="notKnown"/>
      <expected version="1.1" validity="valid"/>
    </instanceTest>
  </testGroup>
</testSet>
"""

LEGACY_META = {"profile": "xsd10", "case_count": 9}

FULL_META = {
    "profile": "xsd11",
    "suite_file": "suite.xml",
    "case_count": 9,
    "complete": True,
    "engines": ["pyxsd", "xmlschema"],
    "timeout_seconds": 180.0,
    "jobs": 8,
    "command": "python tests/report_xsts.py --profile xsd11 --engine both",
    "source_revision": "a" * 40,
    "corpus_revision": "b" * 40,
    "python_version": "3.13.2",
    "platform": "Darwin-24.4.0-arm64",
    "engine_versions": ["elementpath==5.4.0", "pyxsd==1.2.3", "xmlschema==4.7.0"],
    "run_completed_at": "2026-10-31T18:22:33.500000+00:00",
}

XSD10_RESULTS = {
    "pyxsd:Mini/g1/s1:schema": "pass",
    "pyxsd:Mini/g1/i1:instance": "fail",
    "pyxsd:Mini/g2/s2:schema": "fail",
    "pyxsd:Mini/g2/i2:instance": "pass",
    "pyxsd:Mini/g3/s3:schema": "fail",
    "pyxsd:Mini/g3/i3:instance": "not-applicable",
    "pyxsd:Mini/g4/s4:schema": "fail",
    "pyxsd:Mini/g4/i4:instance": "pass",
    "pyxsd:Mini/g5/i5:instance": "not-checkable",
}

XSD11_RESULTS = {
    "pyxsd:Mini/g1/s1:schema": "pass",
    "pyxsd:Mini/g1/i1:instance": "pass",
    "pyxsd:Mini/g2/s2:schema": "fail",
    "pyxsd:Mini/g2/i2:instance": "pass",
    "pyxsd:Mini/g3/s3:schema": "adapter-gap",
    "pyxsd:Mini/g3/i3:instance": "pass",
    "pyxsd:Mini/g4/s4:schema": "fail",
    "pyxsd:Mini/g4/i4:instance": "pass",
    "pyxsd:Mini/g5/i5:instance": "fail",
    "xmlschema:Mini/g1/i1:instance": "pass",
    "xmlschema:Mini/g2/s2:schema": "fail",
    "xmlschema:Mini/g4/s4:schema": "fail",
}

DISPOSITION_BLOCKS = (
    """\
[[dispositions]]
id = "fixture-gap"
title = "A reviewed fixture gap"
category = "implementation-gap"
profiles = ["xsd10"]
cases = ["Mini/g1/i1", "Mini/g2/s2"]
rationale = "Both cases are understood fixture failures."
""",
    """\
[[dispositions]]
id = "fixture-second"
title = "A second reviewed fixture gap"
category = "version-switch-candidate"
profiles = ["xsd11"]
cases = ["Mini/g2/s2"]
rationale = "A reviewed version-switch candidate."
""",
)


def _write_baseline(
    path: Path,
    meta: dict[str, object],
    results: dict[str, str],
    *,
    reverse: bool = False,
) -> None:
    """Write TOML directly so insertion order is under the test's control."""
    lines = ["[meta]"]
    for key, value in meta.items():
        lines.append(f"{key} = {json.dumps(value)}")
    lines.extend(["", "[results]"])
    items = list(results.items())
    if reverse:
        items.reverse()
    for key, value in items:
        lines.append(f"{json.dumps(key)} = {json.dumps(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_repo(
    root: Path,
    *,
    xsd10_meta: dict[str, object] | None = None,
    xsd10_results: dict[str, str] | None = None,
    xsd11_meta: dict[str, object] | None = None,
    xsd11_results: dict[str, str] | None = None,
    dispositions: str | None = None,
    reverse: bool = False,
) -> Path:
    """A miniature repository with the layout the generator expects."""
    corpus = root / "tests" / "xsts" / "corpus"
    corpus.mkdir(parents=True)
    (corpus / "suite.xml").write_text(SUITE_XML, encoding="utf-8")
    (corpus / "mini.testSet").write_text(TEST_SET, encoding="utf-8")
    _write_baseline(
        root / "tests" / "xsts" / "baseline-xsd10.toml",
        dict(LEGACY_META if xsd10_meta is None else xsd10_meta),
        dict(XSD10_RESULTS if xsd10_results is None else xsd10_results),
        reverse=reverse,
    )
    _write_baseline(
        root / "tests" / "xsts" / "baseline-xsd11.toml",
        dict(FULL_META if xsd11_meta is None else xsd11_meta),
        dict(XSD11_RESULTS if xsd11_results is None else xsd11_results),
        reverse=reverse,
    )
    blocks = list(DISPOSITION_BLOCKS)
    if reverse:
        blocks.reverse()
    body = dispositions if dispositions is not None else "\n\n".join(blocks)
    (root / "tests" / "xsts" / "dispositions.toml").write_text(body, encoding="utf-8")
    return root


def _lines(fragment: str) -> list[str]:
    return fragment.splitlines()


def _flat(fragment: str) -> str:
    return " ".join(fragment.split())


# ------------------------------------------------------------- agreement format


def test_format_agreement_uses_the_raw_checked_ratio() -> None:
    assert format_agreement(3, 7) == "42.8571%"
    assert format_agreement(39438, 39438) == "100.0000%"


def test_format_agreement_declines_to_invent_a_rate_without_checked_cases() -> None:
    assert format_agreement(0, 0) == "n/a"


# ------------------------------------------------------------------- outcomes


def test_results_report_exact_outcome_counts_and_denominators(tmp_path: Path) -> None:
    fragment = build_results_fragment(make_repo(tmp_path))
    lines = _lines(fragment)

    for line in [
        "| Outcome | XSD 1.1 pyxsd | XSD 1.1 xmlschema | XSD 1.0 pyxsd |",
        "| pass | 5 | 1 | 3 |",
        "| fail | 3 | 2 | 4 |",
        "| not-applicable | 0 | 0 | 1 |",
        "| not-checkable | 0 | 0 | 1 |",
        "| adapter-gap | 1 | 0 | 0 |",
        "| metadata-error | 0 | 0 | 0 |",
        "| timeout | 0 | 0 | 0 |",
        "| error | 0 | 0 | 0 |",
        "| **Total** | 9 | 3 | 9 |",
        "| **Checked (pass + fail)** | 8 | 3 | 7 |",
        "| **Agreement on checked** | 62.5000% | 33.3333% | 42.8571% |",
    ]:
        assert line in lines


def test_results_split_schema_and_instance_outcomes(tmp_path: Path) -> None:
    fragment = build_results_fragment(make_repo(tmp_path))
    lines = _lines(fragment)

    for line in [
        "| XSD 1.1 | pyxsd | schema | 4 | 1 | 2 | 3 | 33.3333% |",
        "| XSD 1.1 | pyxsd | instance | 5 | 4 | 1 | 5 | 80.0000% |",
        "| XSD 1.1 | xmlschema | schema | 2 | 0 | 2 | 2 | 0.0000% |",
        "| XSD 1.1 | xmlschema | instance | 1 | 1 | 0 | 1 | 100.0000% |",
        "| XSD 1.0 | pyxsd | schema | 4 | 1 | 3 | 4 | 25.0000% |",
        "| XSD 1.0 | pyxsd | instance | 5 | 2 | 1 | 3 | 66.6667% |",
    ]:
        assert line in lines


def test_results_count_false_accepts_and_rejects_by_kind(tmp_path: Path) -> None:
    fragment = build_results_fragment(make_repo(tmp_path))
    lines = _lines(fragment)

    for line in [
        "| XSD 1.1 | pyxsd | schema | 1 | 1 | 0 | 2 |",
        "| XSD 1.1 | pyxsd | instance | 0 | 1 | 0 | 1 |",
        "| XSD 1.1 | pyxsd | **Total** | 1 | 2 | 0 | 3 |",
        "| XSD 1.1 | xmlschema | schema | 1 | 1 | 0 | 2 |",
        "| XSD 1.1 | xmlschema | instance | 0 | 0 | 0 | 0 |",
        "| XSD 1.1 | xmlschema | **Total** | 1 | 1 | 0 | 2 |",
        "| XSD 1.0 | pyxsd | schema | 1 | 2 | 0 | 3 |",
        "| XSD 1.0 | pyxsd | instance | 1 | 0 | 0 | 1 |",
        "| XSD 1.0 | pyxsd | **Total** | 2 | 2 | 0 | 4 |",
    ]:
        assert line in lines


def test_results_account_for_combined_profile_applicability(tmp_path: Path) -> None:
    fragment = build_results_fragment(make_repo(tmp_path))
    lines = _lines(fragment)

    for line in [
        "| Checked in both profiles | 6 |",
        "| Checked only in XSD 1.1 | 2 |",
        "| Checked only in XSD 1.0 | 1 |",
        "| Checked in at least one profile | 9 |",
        "| Checked in neither profile | 0 |",
        "| Total main-suite cases | 9 |",
    ]:
        assert line in lines


def test_results_label_every_cross_profile_expectation_relation(tmp_path: Path) -> None:
    fragment = build_results_fragment(make_repo(tmp_path))
    lines = _lines(fragment)

    for line in [
        "| Relation | Pass in 1.1 | Fail in 1.1 | Not checked | Total |",
        "| invalid-in-1.0-valid-in-1.1 | 1 | 0 | 0 | 1 |",
        "| same-valid | 0 | 1 | 0 | 1 |",
        "| valid-in-1.0-invalid-in-1.1 | 0 | 0 | 1 | 1 |",
        "| same-invalid | 0 | 1 | 0 | 1 |",
        "| **Total** | **1** | **2** | **1** | **4** |",
    ]:
        assert line in lines
    assert (
        "Of the 4 checked XSD 1.0 failures, 1 pass under XSD 1.1, 2 fail, "
        "and 1 are not checked." in _flat(fragment)
    )


# ------------------------------------------------------------------ residuals


def test_results_group_only_intentional_version_split_failures(tmp_path: Path) -> None:
    fragment = build_results_fragment(make_repo(tmp_path))
    lines = _lines(fragment)

    start = lines.index("### Intentional version-split failures")
    end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("## "))
    rows = [line for line in lines[start:end] if line.startswith("| Mini/")]

    assert rows == ["| Mini/g1/i1 | instance | fixture-gap |"]


def test_results_report_disposition_counts_and_every_remaining_failure(
    tmp_path: Path,
) -> None:
    fragment = build_results_fragment(make_repo(tmp_path))
    lines = _lines(fragment)

    for line in [
        "| XSD 1.1 | 3 | 1 | 2 |",
        "| XSD 1.0 | 4 | 2 | 2 |",
        "| fixture-gap | implementation-gap | XSD 1.0 | 2 | A reviewed fixture gap |",
        "| fixture-second | version-switch-candidate | XSD 1.1 | 1 | "
        "A second reviewed fixture gap |",
        "| XSD 1.0 | Mini/g1/i1 | instance | fail | invalid-in-1.0-valid-in-1.1 | fixture-gap |",
        "| XSD 1.0 | Mini/g2/s2 | schema | fail | same-valid | fixture-gap |",
        "| XSD 1.0 | Mini/g3/s3 | schema | fail | valid-in-1.0-invalid-in-1.1 | unclassified |",
        "| XSD 1.0 | Mini/g4/s4 | schema | fail | same-invalid | unclassified |",
        "| XSD 1.1 | Mini/g2/s2 | schema | fail | same-valid | fixture-second |",
        "| XSD 1.1 | Mini/g4/s4 | schema | fail | same-invalid | unclassified |",
        "| XSD 1.1 | Mini/g5/i5 | instance | fail | not-comparable | unclassified |",
    ]:
        assert line in lines


def test_results_keep_residual_rows_in_a_stable_order(tmp_path: Path) -> None:
    fragment = build_results_fragment(make_repo(tmp_path))
    rows = [line for line in _lines(fragment) if line.startswith("| XSD 1.") and "| fail |" in line]

    assert rows == [
        "| XSD 1.1 | Mini/g2/s2 | schema | fail | same-valid | fixture-second |",
        "| XSD 1.1 | Mini/g4/s4 | schema | fail | same-invalid | unclassified |",
        "| XSD 1.1 | Mini/g5/i5 | instance | fail | not-comparable | unclassified |",
        "| XSD 1.0 | Mini/g1/i1 | instance | fail | invalid-in-1.0-valid-in-1.1 | fixture-gap |",
        "| XSD 1.0 | Mini/g2/s2 | schema | fail | same-valid | fixture-gap |",
        "| XSD 1.0 | Mini/g3/s3 | schema | fail | valid-in-1.0-invalid-in-1.1 | unclassified |",
        "| XSD 1.0 | Mini/g4/s4 | schema | fail | same-invalid | unclassified |",
    ]


# ----------------------------------------------------------------- provenance


def test_results_read_full_run_provenance_from_baseline_metadata(tmp_path: Path) -> None:
    fragment = build_results_fragment(make_repo(tmp_path))
    lines = _lines(fragment)

    for line in [
        "| suite_file | suite.xml |",
        "| case_count | 9 |",
        "| complete | true |",
        "| engines | pyxsd, xmlschema |",
        "| timeout_seconds | 180.0 |",
        "| jobs | 8 |",
        "| source_revision | " + "a" * 40 + " |",
        "| corpus_revision | " + "b" * 40 + " |",
        "| python_version | 3.13.2 |",
        "| platform | Darwin-24.4.0-arm64 |",
        "| engine_versions | elementpath==5.4.0, pyxsd==1.2.3, xmlschema==4.7.0 |",
        "| run_completed_at | 2026-10-31T18:22:33.500000+00:00 |",
    ]:
        assert line in lines
    assert "2026-10-31T18:22:33.500000+00:00" in fragment


def test_results_mark_legacy_metadata_instead_of_crashing(tmp_path: Path) -> None:
    fragment = build_results_fragment(make_repo(tmp_path))
    lines = _lines(fragment)

    heading = lines.index("### XSD 1.0")
    assert lines[heading + 1] == ""
    assert lines[heading + 2] == "full-run provenance not recorded"
    assert "### XSD 1.1" in lines


def test_legacy_metadata_without_a_case_count_still_renders(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, xsd10_meta={"profile": "xsd10"})

    fragment = build_results_fragment(repo)

    assert "full-run provenance not recorded" in _lines(fragment)


def test_fragments_are_byte_identical_and_ignore_mapping_and_file_order(
    tmp_path: Path,
) -> None:
    forward = make_repo(tmp_path / "forward")
    reverse = make_repo(tmp_path / "reverse", reverse=True)

    first = build_results_fragment(forward)

    assert first == build_results_fragment(forward)
    assert first == build_results_fragment(reverse)
    assert build_headline_fragment(forward) == build_headline_fragment(reverse)


# --------------------------------------------------------------- publication


def test_partial_baseline_is_rejected_for_publication(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, xsd11_meta=FULL_META | {"complete": False})

    with pytest.raises(PublicationError, match="partial baselines cannot be published"):
        build_results_fragment(repo)


@pytest.mark.parametrize("builder", [build_results_fragment, build_headline_fragment])
def test_partial_baseline_is_rejected_by_both_builders(tmp_path: Path, builder) -> None:
    repo = make_repo(tmp_path, xsd11_meta=FULL_META | {"complete": False})

    with pytest.raises(PublicationError, match="partial"):
        builder(repo)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"complete": "yes"}, "field 'complete' must be a boolean"),
        ({"case_count": 8}, "case_count 8 does not match 9"),
        ({"case_count": True}, "case_count must be an integer"),
        ({"profile": "xsd10"}, "expected 'xsd11'"),
        ({"suite_file": "extra-suite.xml"}, "only the main suite.xml may be published"),
        ({"engines": "pyxsd"}, "engines must be a list of strings"),
        ({"engines": ["pyxsd"]}, "does not match recorded engines"),
        ({"source_revision": REMOVE}, "provenance is incomplete"),
    ],
)
def test_malformed_baseline_metadata_is_rejected(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    meta = dict(FULL_META)
    for key, value in change.items():
        if value is REMOVE:
            del meta[key]
        else:
            meta[key] = value
    repo = make_repo(tmp_path, xsd11_meta=meta)

    with pytest.raises(PublicationError, match=message):
        build_results_fragment(repo)


def test_baseline_without_pyxsd_results_is_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, xsd10_results={"xmlschema:Mini/g1/i1:instance": "pass"})

    with pytest.raises(PublicationError, match="records no pyxsd results"):
        build_results_fragment(repo)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("broken", "pass", "is malformed"),
        ("pyxsd:Mini/g9/absent:instance", "pass", "not in the main suite"),
        ("pyxsd:Mini/g1/s1:instance", "pass", "records kind 'instance'"),
        ("pyxsd:Mini/g1/s1:schema", "mystery", "unknown outcome"),
    ],
)
def test_unusable_baseline_entries_are_rejected(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    results = dict(XSD10_RESULTS)
    results[key] = value
    repo = make_repo(tmp_path, xsd10_results=results)

    with pytest.raises(PublicationError, match=message):
        build_results_fragment(repo)


# ------------------------------------------------------------------- headline


def test_headline_reports_only_profile_rates_and_exact_denominators(
    tmp_path: Path,
) -> None:
    fragment = build_headline_fragment(make_repo(tmp_path))
    lines = _lines(fragment)
    rows = [line for line in lines if line.startswith("| XSD ")]

    assert rows == [
        "| XSD 1.1 | 5 | 8 | 62.5000% |",
        "| XSD 1.0 | 3 | 7 | 42.8571% |",
    ]
    assert "[XSTS methodology and residuals](xsts.md)" in fragment
    assert "`extra-suite.xml` is excluded" in fragment
    assert "xmlschema" not in fragment
    assert ":orphan:" in lines


def test_fragments_carry_an_orphan_marker_and_a_source_command_comment(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)

    for fragment in (build_results_fragment(repo), build_headline_fragment(repo)):
        lines = _lines(fragment)
        assert lines[0] == ":orphan:"
        assert "<!-- generated by: uv run python tests/report_xsts_docs.py -->" in lines


# ------------------------------------------------------------------------ CLI


def test_cli_writes_both_fragments_and_check_accepts_them(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    assert report_xsts_docs.run([], repo_root=repo) == 0

    results_path = repo / "docs" / "generated" / "xsts-results.md"
    headline_path = repo / "docs" / "generated" / "xsts-headline.md"
    assert results_path.read_text(encoding="utf-8") == build_results_fragment(repo)
    assert headline_path.read_text(encoding="utf-8") == build_headline_fragment(repo)

    assert report_xsts_docs.run(["--check"], repo_root=repo) == 0


def test_cli_check_reports_a_missing_fragment_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    assert report_xsts_docs.run([], repo_root=repo) == 0
    results_path = repo / "docs" / "generated" / "xsts-results.md"
    results_path.unlink()

    assert report_xsts_docs.run(["--check"], repo_root=repo) == 1

    assert "missing: docs/generated/xsts-results.md" in capsys.readouterr().err
    assert not results_path.exists()


def test_cli_check_reports_changed_content_without_rewriting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    assert report_xsts_docs.run([], repo_root=repo) == 0
    results_path = repo / "docs" / "generated" / "xsts-results.md"
    results_path.write_text("stale\n", encoding="utf-8")

    assert report_xsts_docs.run(["--check"], repo_root=repo) == 1

    assert "changed: docs/generated/xsts-results.md" in capsys.readouterr().err
    assert results_path.read_text(encoding="utf-8") == "stale\n"


def test_cli_reports_a_publication_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path, xsd11_meta=FULL_META | {"complete": False})

    assert report_xsts_docs.run([], repo_root=repo) == 2

    assert "partial baselines cannot be published" in capsys.readouterr().err
    assert not (repo / "docs" / "generated").exists()


def test_cli_reports_an_unreadable_corpus(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    (repo / "tests" / "xsts" / "corpus" / "suite.xml").unlink()

    assert report_xsts_docs.run([], repo_root=repo) == 2

    assert "cannot read suite" in capsys.readouterr().err


def test_main_delegates_to_run_with_the_configured_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    assert report_xsts_docs.run([], repo_root=repo) == 0
    monkeypatch.setattr(report_xsts_docs, "REPO_ROOT", repo)

    assert report_xsts_docs.main(["--check"]) == 0
    assert report_xsts_docs.main(["--check"], repo_root=repo) == 0


# ---------------------------------------------------------- real-data contract


def test_checked_in_baselines_publish_the_design_denominators() -> None:
    repo = Path(__file__).resolve().parents[1]
    if not (repo / "tests" / "xsts" / "corpus" / "suite.xml").is_file():
        pytest.skip("the xsdtests corpus submodule is not checked out")

    fragment = build_results_fragment(repo)
    flat = _flat(fragment)

    assert "| Checked in at least one profile | 41,626 |" in flat
    assert "| Checked in neither profile | 109 |" in flat
    assert "| invalid-in-1.0-valid-in-1.1 | 71 | 0 | 0 | 71 |" in flat
    assert "| same-valid | 6 | 16 | 4 | 26 |" in flat
    assert "| valid-in-1.0-invalid-in-1.1 | 5 | 0 | 0 | 5 |" in flat
    assert "full-run provenance not recorded" in flat

    section = fragment.split("### Intentional version-split failures\n", 1)[1]
    split_rows = [
        line
        for line in section.split("\n## ", 1)[0].splitlines()
        if line.startswith("| ") and line != "| Case | Kind | Disposition |"
    ]
    assert len(split_rows) == 71
    assert sum("| xsd10-regex-version-switch |" in row for row in split_rows) == 16
    assert "This view lists 71 checked XSD 1.0 failures" in flat


def test_checked_in_baselines_reject_a_stale_disposition(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "tests" / "xsts" / "dispositions.toml").write_text(
        DISPOSITION_BLOCKS[0].replace('"Mini/g2/s2"', '"Mini/g1/s1"'),
        encoding="utf-8",
    )

    with pytest.raises(DispositionError, match="no longer a failure"):
        build_results_fragment(repo)
