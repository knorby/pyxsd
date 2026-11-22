"""Pinned compact-syntax catalog, verdicts, and translation parity.

The upstream ``compacttest.xml`` manifest is a compact-to-XML translation
catalog: each ``<correct>`` case is compact syntax the upstream reader accepts,
and each ``<incorrect>`` case is syntax the upstream reader rejects.  Some
accepted fragments translate to RELAX NG patterns that cannot stand as a whole
schema (a top-level value, for example), so the compact profile's schema
verdict is parser acceptance, not schema validity.  Compiled parity is asserted
separately for translation pairs.

The pinned execution gate is opt-in with ``PYXSD_RUN_RELAXNG=1``; synthetic
catalog, classification, and parity tests always run.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from tests import report_relaxng
from tests.relaxng.baseline import load as load_baseline
from tests.relaxng.catalog import (
    CASE_DIRECTORY,
    COMPACTTEST_FILE,
    CatalogError,
    load_compact_syntax_catalog,
)
from tests.relaxng.driver import CaseFailure, PyXSDDriver
from tests.relaxng.model import (
    PROFILE_COMPACT_SYNTAX,
    PROFILE_CORE,
    REQUIRED_PROFILES,
    CorpusCase,
    Outcome,
    corpus_enabled,
)

from pyxsd.relaxng import RelaxNGSchema
from pyxsd.relaxng import syntax as full_syntax
from pyxsd.relaxng.comparison import compare_relaxng_structure
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.readers import read_schema_syntax
from pyxsd.relaxng.resources import ResourceLoader

CORPUS = Path(__file__).resolve().parent / "relaxng" / "corpus"
CORPUS_AVAILABLE = (CORPUS / COMPACTTEST_FILE).is_file()

COMPACTTEST_CASES = 87
COMPACTTEST_CORRECT = 56
COMPACTTEST_INCORRECT = 31

_XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


SYNTHETIC_COMPACTTEST = """\
<testSuite>
  <testCase>
    <compact>
      <correct>element foo { empty }</correct>
    </compact>
    <xml>
      <correct>
        <element xmlns="http://relaxng.org/ns/structure/1.0" name="foo"><empty/></element>
      </correct>
    </xml>
  </testCase>
  <testCase>
    <compact>
      <incorrect>element foo {</incorrect>
    </compact>
  </testCase>
  <testCase>
    <compact>
      <resource name="x.rnc">foo = empty</resource>
      <correct>start = external "x.rnc"</correct>
    </compact>
    <xml>
      <resource name="x.rng">
        <ref xmlns="http://relaxng.org/ns/structure/1.0" name="foo"/>
      </resource>
      <correct>
        <grammar xmlns="http://relaxng.org/ns/structure/1.0">
          <start><externalRef href="x.rng"/></start>
        </grammar>
      </correct>
    </xml>
  </testCase>
  <bug>
    <compact><incorrect>element x:* - y:z { empty }</incorrect></compact>
  </bug>
</testSuite>
"""


@pytest.fixture
def synthetic_compacttest(tmp_path: Path) -> Path:
    write(tmp_path / COMPACTTEST_FILE, SYNTHETIC_COMPACTTEST)
    return tmp_path


# ---------------------------------------------------------------------------
# Catalog loading and materialization
# ---------------------------------------------------------------------------


def test_synthetic_compact_catalog_has_stable_ids_kinds_and_profile(
    synthetic_compacttest: Path,
) -> None:
    cases = load_compact_syntax_catalog(synthetic_compacttest)

    assert [item.id for item in cases] == [
        "compacttest-0001",
        "compacttest-0002",
        "compacttest-0003",
    ]
    assert [item.kind for item in cases] == [
        "correct-schema",
        "incorrect-schema",
        "correct-schema",
    ]
    assert {item.profile for item in cases} == {PROFILE_COMPACT_SYNTAX}
    assert {PROFILE_COMPACT_SYNTAX} <= REQUIRED_PROFILES


def test_synthetic_compact_catalog_writes_paired_documents_and_resources(
    synthetic_compacttest: Path,
) -> None:
    cases = {item.id: item for item in load_compact_syntax_catalog(synthetic_compacttest)}
    root = synthetic_compacttest / CASE_DIRECTORY / "compacttest"

    first = cases["compacttest-0001"]
    assert first.schema == root / "0001" / "c.rnc"
    assert first.schema.read_text(encoding="utf-8") == "element foo { empty }"
    pair = first.schema.with_suffix(".rng")
    assert pair.is_file()
    assert "http://relaxng.org/ns/structure/1.0" in pair.read_text(encoding="utf-8")

    second = cases["compacttest-0002"]
    assert second.schema == root / "0002" / "i.rnc"
    assert not (root / "0002" / "i.rng").exists()

    third = cases["compacttest-0003"]
    assert (root / "0003" / "x.rnc").read_text(encoding="utf-8") == "foo = empty"
    assert (root / "0003" / "x.rng").is_file()
    assert third.schema.read_text(encoding="utf-8") == 'start = external "x.rnc"'


def test_synthetic_compact_catalog_round_trips_deterministically(
    synthetic_compacttest: Path,
) -> None:
    first = load_compact_syntax_catalog(synthetic_compacttest)
    snapshot = {
        path: path.read_bytes()
        for path in (synthetic_compacttest / CASE_DIRECTORY).rglob("*")
        if path.is_file()
    }
    first[0].schema.write_text("stale", encoding="utf-8")
    second = load_compact_syntax_catalog(synthetic_compacttest)
    assert first == second
    assert snapshot == {
        path: path.read_bytes()
        for path in (synthetic_compacttest / CASE_DIRECTORY).rglob("*")
        if path.is_file()
    }


def test_compact_catalog_skips_the_upstream_bug_wrapper(synthetic_compacttest: Path) -> None:
    cases = load_compact_syntax_catalog(synthetic_compacttest)
    assert all("bug" not in item.id for item in cases)
    assert len(cases) == 3


def test_compact_catalog_requires_the_pinned_file(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match=COMPACTTEST_FILE):
        load_compact_syntax_catalog(tmp_path)


def test_compact_catalog_reports_malformed_xml(tmp_path: Path) -> None:
    write(tmp_path / COMPACTTEST_FILE, "<testSuite><testCase>")
    with pytest.raises(CatalogError, match="parse"):
        load_compact_syntax_catalog(tmp_path)


def test_compact_catalog_rejects_a_foreign_root(tmp_path: Path) -> None:
    write(tmp_path / COMPACTTEST_FILE, "<compacttest/>")
    with pytest.raises(CatalogError, match="testSuite"):
        load_compact_syntax_catalog(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "<testCase/>",
        "<testCase><compact/></testCase>",
        "<testCase><compact><correct>x</correct><incorrect>y</incorrect></compact></testCase>",
        "<testCase><compact><correct>x</correct><correct>y</correct></compact></testCase>",
        "<testCase><compact><correct><element/></correct></compact></testCase>",
        "<testCase><compact><unknown>x</unknown></compact></testCase>",
        "<testCase><unknown/></testCase>",
        "<testCase><compact>x</compact><compact>y</compact></testCase>",
        "<testCase><compact><correct>x</correct></compact><xml>y</xml></testCase>",
        "<testCase><compact><correct>x</correct></compact>"
        "<xml><correct>x</correct></xml><xml><correct>y</correct></xml></testCase>",
        "<testCase><compact><correct>x</correct></compact><xml><correct/></xml></testCase>",
        "<testCase><compact><correct>x</correct></compact>"
        "<xml><incorrect>y</incorrect></xml></testCase>",
        "<testCase><compact><correct>x</correct></compact>"
        "<xml><correct>x</correct><unknown/></xml></testCase>",
        "<unknown/>",
    ],
)
def test_compact_catalog_rejects_malformed_cases(tmp_path: Path, body: str) -> None:
    write(tmp_path / COMPACTTEST_FILE, f"<testSuite>{body}</testSuite>")
    with pytest.raises(CatalogError):
        load_compact_syntax_catalog(tmp_path)


def test_compact_catalog_materializes_an_incorrect_xml_pair(tmp_path: Path) -> None:
    body = (
        "<testCase><compact><incorrect>element foo {</incorrect></compact>"
        "<xml><incorrect><element xmlns='http://relaxng.org/ns/structure/1.0'>"
        "</element></incorrect></xml></testCase>"
    )
    write(tmp_path / COMPACTTEST_FILE, f"<testSuite>{body}</testSuite>")
    cases = load_compact_syntax_catalog(tmp_path)
    root = tmp_path / CASE_DIRECTORY / "compacttest" / "0001"
    assert [item.kind for item in cases] == ["incorrect-schema"]
    assert (root / "i.rnc").is_file()
    assert (root / "i.rng").is_file()


def test_compact_catalog_rejects_an_unsafe_resource_path(tmp_path: Path) -> None:
    body = "<testCase><compact><resource name='../x'>x</resource><correct>x</correct></compact></testCase>"
    write(tmp_path / COMPACTTEST_FILE, f"<testSuite>{body}</testSuite>")
    with pytest.raises(CatalogError, match=r"resource|path"):
        load_compact_syntax_catalog(tmp_path)
    assert not (tmp_path.parent / "x").exists()


def test_compact_catalog_rejects_duplicate_resource_paths(tmp_path: Path) -> None:
    body = (
        "<testCase><compact>"
        "<resource name='x.rnc'>a</resource><resource name='x.rnc'>b</resource>"
        "<correct>x</correct></compact></testCase>"
    )
    write(tmp_path / COMPACTTEST_FILE, f"<testSuite>{body}</testSuite>")
    with pytest.raises(CatalogError, match="duplicate"):
        load_compact_syntax_catalog(tmp_path)


@pytest.mark.skipif(not CORPUS_AVAILABLE, reason="the pinned corpus is checkout-only")
def test_pinned_compact_catalog_counts_and_profiles() -> None:
    cases = load_compact_syntax_catalog(CORPUS)
    assert len(cases) == COMPACTTEST_CASES
    assert sum(item.kind == "correct-schema" for item in cases) == COMPACTTEST_CORRECT
    assert sum(item.kind == "incorrect-schema" for item in cases) == COMPACTTEST_INCORRECT
    assert {item.profile for item in cases} == {PROFILE_COMPACT_SYNTAX}
    assert len({item.id for item in cases}) == len(cases)
    for item in cases:
        assert item.schema.is_file()
        assert item.schema.suffix == ".rnc"
        assert item.instance is None
        pair = item.schema.with_suffix(".rng")
        assert pair.is_file() is (item.kind == "correct-schema")
        assert all(ord(char) < 128 for char in str(item.schema))


@pytest.mark.skipif(not CORPUS_AVAILABLE, reason="the pinned corpus is checkout-only")
def test_pinned_compact_catalog_materializes_the_translation_resources() -> None:
    cases = load_compact_syntax_catalog(CORPUS)
    root = CORPUS / CASE_DIRECTORY / "compacttest"
    assert (root / "0006" / "x.rnc").is_file()
    assert (root / "0006" / "x.rng").is_file()
    # The compact-only include resource has no XML twin in the upstream pair.
    assert (root / "0075" / "x.rnc").is_file()
    assert not (root / "0075" / "x.rng").exists()
    assert len(cases) == COMPACTTEST_CASES


@pytest.mark.skipif(not CORPUS_AVAILABLE, reason="the pinned corpus is checkout-only")
def test_pinned_compact_catalog_is_covered_by_checksums_and_provenance() -> None:
    sums = (CORPUS / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    expected = {}
    for line in sums:
        digest, name = line.split("  ", 1)
        expected[name] = digest
    assert COMPACTTEST_FILE in expected
    assert (
        hashlib.sha256((CORPUS / COMPACTTEST_FILE).read_bytes()).hexdigest()
        == expected[COMPACTTEST_FILE]
    )
    provenance = (CORPUS / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "mod/rng-schema/test/compacttest.xml" in provenance


# ---------------------------------------------------------------------------
# Compact syntax verdicts
# ---------------------------------------------------------------------------


def compact_case(
    tmp_path: Path,
    case_id: str,
    kind: str,
    source: str,
) -> CorpusCase:
    schema = write(tmp_path / f"{case_id}.rnc", source)
    instance = None
    if kind.endswith("instance"):
        instance = write(tmp_path / f"{case_id}.xml", "<doc/>")
    return CorpusCase(
        id=case_id,
        kind=kind,  # type: ignore[arg-type]
        schema=schema,
        instance=instance,
        profile=PROFILE_COMPACT_SYNTAX,
    )


def test_compact_syntax_rejection_passes_an_incorrect_schema(tmp_path: Path) -> None:
    case = compact_case(tmp_path, "reject", "incorrect-schema", "element foo {")

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.PASS
    assert result.failure is CaseFailure.NONE
    assert result.expected_valid is False
    assert result.actual_valid is False
    assert result.satisfies_expectation


def test_compact_syntax_rejection_is_a_gap_for_a_correct_schema(tmp_path: Path) -> None:
    case = compact_case(tmp_path, "accept", "correct-schema", "element foo {")

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.ADAPTER_GAP
    assert result.expected_valid is True
    assert result.actual_valid is None
    assert not result.satisfies_expectation


def test_compact_syntax_rejection_is_a_schema_gap_for_an_instance(tmp_path: Path) -> None:
    case = compact_case(tmp_path, "instance", "invalid-instance", "element foo {")

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.SCHEMA_REJECTED
    assert result.phase == "schema"
    assert result.actual_valid is None


def test_accepted_compact_syntax_is_the_compact_profile_verdict(tmp_path: Path) -> None:
    # A top-level value is accepted compact syntax but not a valid schema.
    case = compact_case(tmp_path, "syntax-only", "correct-schema", '"value"')

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.PASS
    assert result.failure is CaseFailure.NONE
    assert result.actual_valid is True


def test_compact_schema_errors_reject_an_incorrect_case(tmp_path: Path) -> None:
    case = compact_case(tmp_path, "semantic", "incorrect-schema", "start = empty")

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.PASS
    assert result.failure is CaseFailure.NONE
    assert result.actual_valid is False


def test_an_accepted_wrong_compact_correct_case_still_fails_the_profile(
    tmp_path: Path,
) -> None:
    case = compact_case(tmp_path, "wrong", "incorrect-schema", "element foo { empty }")

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.FAIL
    assert result.failure is CaseFailure.WRONG_VERDICT
    assert result.actual_valid is True


@pytest.mark.skipif(
    not CORPUS_AVAILABLE or not corpus_enabled(),
    reason="set PYXSD_RUN_RELAXNG=1 and check out the pinned catalogs",
)
def test_pinned_compact_cases_settle_to_the_compact_profile_verdict() -> None:
    driver = PyXSDDriver(timeout=report_relaxng.timeout_from_environment())
    results = [driver.run(case) for case in load_compact_syntax_catalog(CORPUS)]
    assert len(results) == COMPACTTEST_CASES
    assert {result.outcome for result in results} == {Outcome.PASS}


# ---------------------------------------------------------------------------
# Normalized full-syntax parity
# ---------------------------------------------------------------------------


def _splice(children: tuple[full_syntax.FullNode, ...]):
    for child in children:
        if isinstance(child, full_syntax.FullDiv):
            yield from _splice(child.children)
        else:
            yield child


def _flatten(kind: str, parts: list[object]) -> object:
    pending = list(parts)
    out: list[object] = []
    while pending:
        part = pending.pop(0)
        if isinstance(part, list) and len(part) == 2 and part[0] == kind:
            pending[:0] = part[1]
        else:
            out.append(part)
    return out[0] if len(out) == 1 else [kind, out]


def _sequence(children: tuple[full_syntax.FullNode, ...]):
    parts = [_full_syntax(child) for child in _splice(children)]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else _flatten("group", parts)


def _unary(kind: str, node) -> list[object]:
    return [kind, _sequence(node.children)]


def _library(type_name: str | None, library: str) -> str:
    if type_name in {"string", "token"} and library in {"", _XSD}:
        return ""
    return library


def _qualified(type_name: str | None) -> bool:
    return type_name in {"QName", "NOTATION"}


def _namespaces(items: tuple[tuple[str, str], ...]) -> list[list[str]]:
    return sorted([prefix, uri] for prefix, uri in items if prefix != "xml")


def _value_lexical(lexical: str) -> str:
    return lexical.replace("\r\n", "\n").replace("\r", "\n")


def _full_syntax(node) -> object:
    if isinstance(node, full_syntax.FullGrammar):
        return ["grammar", _scope(node.scope)]
    if isinstance(node, full_syntax.GrammarScope):
        return _scope(node)
    if isinstance(node, full_syntax.FullStart):
        return ["start", node.combine, _sequence(node.children)]
    if isinstance(node, full_syntax.FullDefine):
        return ["define", node.name, node.combine, _sequence(node.children)]
    if isinstance(
        node,
        (full_syntax.FullEmpty, full_syntax.FullNotAllowed, full_syntax.FullText),
    ):
        return [type(node).__name__]
    if isinstance(node, full_syntax.FullGroup):
        return _flatten("group", [_full_syntax(child) for child in _splice(node.children)])
    if isinstance(node, full_syntax.FullInterleave):
        return _flatten("interleave", [_full_syntax(child) for child in _splice(node.children)])
    if isinstance(node, full_syntax.FullChoice):
        return _flatten("choice", [_full_syntax(child) for child in _splice(node.children)])
    if isinstance(node, full_syntax.FullOptional):
        return _unary("optional", node)
    if isinstance(node, full_syntax.FullZeroOrMore):
        return _unary("zeroOrMore", node)
    if isinstance(node, full_syntax.FullOneOrMore):
        return _unary("oneOrMore", node)
    if isinstance(node, full_syntax.FullList):
        return _unary("list", node)
    if isinstance(node, full_syntax.FullMixed):
        return _unary("mixed", node)
    if isinstance(node, full_syntax.FullDiv):
        return ["div", [_full_syntax(child) for child in node.children]]
    if isinstance(node, full_syntax.FullUnknown):
        return ["unknown", node.tag, node.text]
    if isinstance(node, full_syntax.FullElement):
        return ["element", _full_syntax(node.name_class), _sequence(node.children)]
    if isinstance(node, full_syntax.FullAttribute):
        content = _sequence(node.children)
        return ["attribute", _full_syntax(node.name_class), content or ["FullText"]]
    if isinstance(node, full_syntax.FullParam):
        return ["param", node.name, node.value]
    if isinstance(node, full_syntax.FullExcept):
        parts = [_full_syntax(child) for child in _splice(node.children)]
        content = None if not parts else parts[0] if len(parts) == 1 else _flatten("choice", parts)
        return ["except", content]
    if isinstance(node, full_syntax.FullValue):
        type_name = node.type_name or "token"
        qualified = _qualified(type_name)
        return [
            "value",
            _library(type_name, node.datatype_library),
            type_name,
            _value_lexical(node.lexical or ""),
            (node.namespace_uri or "") if qualified else "",
            _namespaces(node.namespaces) if qualified else [],
        ]
    if isinstance(node, full_syntax.FullData):
        return [
            "data",
            _library(node.type_name, node.datatype_library),
            node.type_name,
        ]
    if isinstance(node, full_syntax.FullName):
        return ["name", node.uri, node.local]
    if isinstance(node, full_syntax.FullAnyName):
        return [
            "anyName",
            _full_syntax(node.except_class) if node.except_class is not None else None,
        ]
    if isinstance(node, full_syntax.FullNsName):
        return [
            "nsName",
            node.uri,
            _full_syntax(node.except_class) if node.except_class is not None else None,
        ]
    if isinstance(node, (full_syntax.FullNameChoice, full_syntax.FullNameExcept)):
        return _flatten("nameChoice", [_full_syntax(item) for item in node.alternatives])
    if isinstance(node, full_syntax.ReferencePlaceholder):
        return [
            "ref",
            node.kind,
            node.name,
            node.ns_attribute,
            [_full_syntax(child) for child in node.children],
        ]
    raise TypeError(f"unexpected full-syntax node {type(node).__name__}")


def _scope(scope: full_syntax.GrammarScope) -> list[object]:
    return ["scope", [_full_syntax(child) for child in scope.children]]


def _read_full_syntax(path: Path, syntax: str):
    loader = ResourceLoader(None)
    resource = loader.load(path, syntax=syntax)  # type: ignore[arg-type]
    budget = BudgetCounter("compile", RelaxNGLimits())
    return read_schema_syntax(resource, budget=budget)


def _pairs() -> tuple[CorpusCase, ...]:
    return tuple(
        case for case in load_compact_syntax_catalog(CORPUS) if case.kind == "correct-schema"
    )


@pytest.mark.skipif(not CORPUS_AVAILABLE, reason="the pinned corpus is checkout-only")
def test_pinned_translation_pairs_have_normalized_full_syntax_parity() -> None:
    pairs = _pairs()
    assert len(pairs) == COMPACTTEST_CORRECT
    for case in pairs:
        compact = _read_full_syntax(case.schema, "compact")
        xml = _read_full_syntax(case.schema.with_suffix(".rng"), "xml")
        assert _full_syntax(compact.start) == _full_syntax(xml.start), case.id
        assert [finding.code for finding in compact.findings] == [
            finding.code for finding in xml.findings
        ], case.id


def _normalize_compact_source(text: str) -> str:
    """Apply XML 1.0 line-end normalization to compact newline escapes."""
    return (
        text.replace("\\x{D}\\x{A}", "\\x{A}")
        .replace("\\x{d}\\x{a}", "\\x{A}")
        .replace("\\x{D}", "\\x{A}")
        .replace("\\x{d}", "\\x{A}")
    )


def _compile_compact_for_parity(case: CorpusCase, tmp_path: Path) -> RelaxNGSchema:
    directory = tmp_path / case.id
    shutil.copytree(case.schema.parent, directory)
    schema = directory / case.schema.name
    schema.write_text(
        _normalize_compact_source(schema.read_text(encoding="utf-8")), encoding="utf-8"
    )
    return RelaxNGSchema.compile(schema, syntax="compact")


def _error_codes(schema: RelaxNGSchema) -> set[str]:
    return {issue.code for issue in schema.report.issues}


@pytest.mark.skipif(not CORPUS_AVAILABLE, reason="the pinned corpus is checkout-only")
def test_pinned_translation_pairs_have_compiled_structure_parity(tmp_path: Path) -> None:
    compared = 0
    invalid_pairs = 0
    for case in _pairs():
        compact = _compile_compact_for_parity(case, tmp_path)
        xml = RelaxNGSchema.compile(case.schema.with_suffix(".rng"), syntax="xml")
        compact_errors = _error_codes(compact)
        xml_errors = _error_codes(xml)
        if compact_errors or xml_errors:
            invalid_pairs += 1
            # The XML twin has no include resource for one upstream pair.  Its
            # resource-read finding is dependency evidence, not a semantic
            # difference between the two authored schemas.
            allowed = {"relaxng-resource-read"}
            assert compact_errors - allowed == xml_errors - allowed, case.id
            continue
        comparison = compare_relaxng_structure(compact, xml)
        for category, changes in comparison["categories"].items():
            if category == "dependencies":
                continue
            assert not changes["added"], (case.id, category, changes["added"])
            assert not changes["removed"], (case.id, category, changes["removed"])
            assert not changes["changed"], (case.id, category, changes["changed"])
        compared += 1
    assert compared + invalid_pairs == COMPACTTEST_CORRECT
    assert compared > 0
    assert invalid_pairs > 0


def test_compact_and_xml_pairs_have_identical_instance_verdicts(tmp_path: Path) -> None:
    compact_source = write(tmp_path / "note.rnc", "element note { text }")
    xml_source = write(
        tmp_path / "note.rng",
        '<element xmlns="http://relaxng.org/ns/structure/1.0" name="note"><text/></element>',
    )
    compact = RelaxNGSchema.compile(compact_source, syntax="compact")
    xml = RelaxNGSchema.compile(xml_source, syntax="xml")
    compact.require_valid()
    xml.require_valid()

    valid = write(tmp_path / "valid.xml", "<note>x</note>")
    invalid = write(tmp_path / "invalid.xml", "<note><extra/></note>")

    assert compact.parse(valid, typed=False).report.has_errors is False
    assert xml.parse(valid, typed=False).report.has_errors is False
    assert compact.parse(invalid, typed=False).report.has_errors is True
    assert xml.parse(invalid, typed=False).report.has_errors is True


# ---------------------------------------------------------------------------
# Checked-in evidence and the pinned gate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not CORPUS_AVAILABLE, reason="the pinned corpus is checkout-only")
def test_compact_profile_counts_are_in_the_checked_in_baseline() -> None:
    compact = load_compact_syntax_catalog(CORPUS)
    baseline = load_baseline(report_relaxng.BASELINE)
    assert len(compact) == COMPACTTEST_CASES
    assert {case.id for case in compact} <= set(baseline.results)
    assert {baseline.results[case.id] for case in compact} == {"pass"}
    assert baseline.meta["schema_cases"] == 710
    assert baseline.meta["profile"] == "core+xsd-datatype+compact-syntax"


@pytest.mark.skipif(not CORPUS_AVAILABLE, reason="the pinned corpus is checkout-only")
def test_compact_cases_are_release_required_and_not_excluded() -> None:
    cases = load_compact_syntax_catalog(CORPUS)
    assert {case.profile for case in cases} == {PROFILE_COMPACT_SYNTAX}
    assert all(case.is_required and not case.is_excluded for case in cases)
    assert cases[0].profile in REQUIRED_PROFILES
    assert cases[0].profile != PROFILE_CORE


@pytest.mark.skipif(
    not CORPUS_AVAILABLE or not corpus_enabled(),
    reason="set PYXSD_RUN_RELAXNG=1 and check out the pinned catalogs",
)
def test_pinned_compact_corpus_matches_the_checked_in_baseline() -> None:
    timeout = report_relaxng.timeout_from_environment()
    cases = load_compact_syntax_catalog(CORPUS)
    baseline = load_baseline(report_relaxng.BASELINE)
    results = report_relaxng.run_cases(cases, timeout=timeout)
    outcomes = report_relaxng.results_to_keys(results)
    failures = report_relaxng.unresolved_failures(results)

    assert not failures, "\n".join(
        f"{failure.case.id} [{failure.case.kind}] {failure.outcome.value}"
        f" ({failure.failure.value}): {failure.detail or ''}"
        for failure in failures
    )
    assert set(outcomes) == {case.id for case in cases}
    assert {case.id: baseline.results.get(case.id) for case in cases} == outcomes
