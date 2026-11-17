"""Instance-corpus acceptance, outcome boundaries, and the Phase 2 gate.

The synthetic tests always run.  The pinned corpus gate is opt-in with
``PYXSD_RUN_RELAXNG=1``.  An instance assertion has a verdict only when its
schema compiled and its instance was well-formed XML: a rejected schema,
malformed XML, timeout, crash, limit, or unsupported capability is an
adapter gap, never an expected-invalid instance pass.
"""

from __future__ import annotations

import io
import json
import signal
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from tests import report_relaxng
from tests.relaxng.baseline import Baseline, BaselineError, diff_instances
from tests.relaxng.baseline import load as load_baseline
from tests.relaxng.baseline import save as save_baseline
from tests.relaxng.dispositions import (
    Disposition,
    load_dispositions,
    release_violations,
)
from tests.relaxng.driver import CaseFailure, PyXSDDriver
from tests.relaxng.model import (
    PROFILE_COMPACT_SYNTAX,
    PROFILE_CORE,
    PROFILE_XSD_DATATYPE,
    CorpusCase,
    Outcome,
    corpus_enabled,
)

from pyxsd.locations import PathStep
from pyxsd.namespaces import parse_with_namespaces
from pyxsd.relaxng.datatypes import DatatypeContext, XsdDatatypeFactory
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.patterns import Data, DataExcept, Empty, Group, Ref
from pyxsd.relaxng.schema import (
    RelaxNGSchema,
    _grammar_uses_unparsed_entity_types,
)
from pyxsd.resource_policy import ResourceData

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
CORPUS_AVAILABLE = all(
    (report_relaxng.CORPUS / name).is_file() for name in ("spectest.xml", "xsdtest.xml")
)

EMPTY_SCHEMA = f'<element xmlns="{NS}" name="doc"><empty/></element>'


def write_instance_case(
    tmp_path: Path,
    case_id: str,
    kind: str,
    *,
    schema_text: str = EMPTY_SCHEMA,
    instance_text: str = "<doc/>",
    profile: str = PROFILE_CORE,
) -> CorpusCase:
    schema = tmp_path / f"{case_id.replace('/', '-')}.rng"
    schema.write_text(schema_text, encoding="utf-8")
    instance = tmp_path / f"{case_id.replace('/', '-')}.xml"
    instance.write_text(instance_text, encoding="utf-8")
    return CorpusCase(
        id=case_id,
        kind=kind,  # type: ignore[arg-type]
        schema=schema,
        instance=instance,
        profile=profile,
    )


def data_schema(type_name: str) -> str:
    return (
        f'<element xmlns="{NS}" name="doc" datatypeLibrary="{XSD}">'
        f'<data type="{type_name}"/></element>'
    )


# ---------------------------------------------------------------------------
# Driver outcome boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "instance_text", "expected"),
    [
        ("valid-instance", "<doc/>", True),
        ("invalid-instance", "<doc><extra/></doc>", False),
    ],
)
def test_driver_matches_valid_and_invalid_instance_verdicts(
    tmp_path: Path,
    kind: str,
    instance_text: str,
    expected: bool,
) -> None:
    case = write_instance_case(tmp_path, kind, kind, instance_text=instance_text)

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.PASS
    assert result.failure is CaseFailure.NONE
    assert result.phase == "instance"
    assert result.expected_valid is expected
    assert result.actual_valid is expected
    assert result.detail is None
    assert result.satisfies_expectation


@pytest.mark.parametrize(
    ("kind", "instance_text", "detail"),
    [
        ("valid-instance", "<doc><extra/></doc>", "expected a valid instance"),
        ("invalid-instance", "<doc/>", "expected an invalid instance"),
    ],
)
def test_driver_reports_a_wrong_instance_verdict(
    tmp_path: Path,
    kind: str,
    instance_text: str,
    detail: str,
) -> None:
    case = write_instance_case(tmp_path, kind, kind, instance_text=instance_text)

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.FAIL
    assert result.failure is CaseFailure.WRONG_VERDICT
    assert result.phase == "instance"
    assert result.actual_valid is (not result.expected_valid)
    assert detail in (result.detail or "")
    assert not result.satisfies_expectation


def test_driver_classifies_invalid_xml_as_an_instance_phase_gap(tmp_path: Path) -> None:
    case = write_instance_case(
        tmp_path,
        "malformed",
        "invalid-instance",
        instance_text="<doc>",
    )

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.INVALID_XML
    assert result.phase == "instance"
    assert result.expected_valid is False
    assert result.actual_valid is None
    assert "PyXSDError" in (result.detail or "")
    assert not result.satisfies_expectation


def test_driver_classifies_an_incorrect_schema_as_a_schema_phase_gap(
    tmp_path: Path,
) -> None:
    case = write_instance_case(
        tmp_path,
        "bad-schema",
        "invalid-instance",
        schema_text=f'<element xmlns="{NS}"><empty/></element>',
    )

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.SCHEMA_REJECTED
    assert result.phase == "schema"
    assert result.expected_valid is False
    assert result.actual_valid is None
    assert "relaxng-" in (result.detail or "")
    # A schema that did not compile cannot satisfy an expected-invalid instance.
    assert not result.satisfies_expectation


def test_driver_omits_the_instance_parse_when_the_schema_did_not_compile(
    tmp_path: Path,
) -> None:
    case = write_instance_case(
        tmp_path,
        "guard-omission",
        "valid-instance",
        schema_text=f'<element xmlns="{NS}"><empty/></element>',
    )
    parsed: list[Path] = []

    def parser(schema, path, limits):
        del schema, limits
        parsed.append(path)
        raise AssertionError("the instance parse must be omitted")

    result = PyXSDDriver(timeout=None, parser=parser).run(case)

    assert result.failure is CaseFailure.SCHEMA_REJECTED
    assert result.phase == "schema"
    assert parsed == []


def test_driver_classifies_a_schema_limit_as_a_schema_phase_gap(tmp_path: Path) -> None:
    case = write_instance_case(tmp_path, "schema-limit", "valid-instance")

    result = PyXSDDriver(
        timeout=None,
        limits=RelaxNGLimits(max_pattern_nodes=0),
    ).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.LIMIT
    assert result.phase == "schema"
    assert (result.detail or "").startswith("limit:")
    assert result.actual_valid is None


def test_driver_classifies_an_instance_limit_as_an_instance_phase_gap(
    tmp_path: Path,
) -> None:
    case = write_instance_case(
        tmp_path,
        "instance-limit",
        "invalid-instance",
        schema_text=f'<element xmlns="{NS}" name="doc"><text/></element>',
        instance_text="<doc>text</doc>",
    )

    result = PyXSDDriver(
        timeout=None,
        limits=RelaxNGLimits(max_derivative_states=0),
    ).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.LIMIT
    assert result.phase == "instance"
    assert (result.detail or "").startswith("limit:")
    assert result.actual_valid is None
    assert not result.satisfies_expectation


def test_driver_classifies_an_unsupported_schema_as_a_schema_phase_gap(
    tmp_path: Path,
) -> None:
    case = write_instance_case(
        tmp_path,
        "unsupported",
        "valid-instance",
        schema_text=(
            f'<element xmlns="{NS}" name="doc">'
            '<data type="thing" datatypeLibrary="urn:unregistered"/>'
            "</element>"
        ),
    )

    result = PyXSDDriver(timeout=None).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.ADAPTER_GAP
    assert result.phase == "schema"
    assert "UnsupportedDatatypeError" in (result.detail or "")
    assert not result.satisfies_expectation


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="no SIGALRM on this platform")
def test_driver_classifies_a_timeout_as_an_instance_phase_gap(tmp_path: Path) -> None:
    case = write_instance_case(tmp_path, "timeout", "valid-instance")

    def slow_parser(schema, path, limits):
        del schema, path, limits
        time.sleep(0.2)
        raise AssertionError("unreachable")

    result = PyXSDDriver(timeout=0.01, parser=slow_parser).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.TIMEOUT
    assert result.phase == "instance"
    assert (result.detail or "").startswith("timeout:")
    assert not result.satisfies_expectation


def test_driver_classifies_an_instance_crash_as_an_instance_phase_gap(
    tmp_path: Path,
) -> None:
    case = write_instance_case(tmp_path, "crash", "valid-instance")

    def explode(schema, path, limits):
        del schema, path, limits
        raise RuntimeError("boom")

    result = PyXSDDriver(timeout=None, parser=explode).run(case)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.failure is CaseFailure.CRASH
    assert result.phase == "instance"
    assert result.detail == "RuntimeError: boom"
    assert not result.satisfies_expectation


# ---------------------------------------------------------------------------
# Focused datatype defects exposed by the pinned instance corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("type_name", ["float", "double"])
def test_float_types_reject_the_xsd_10_plus_inf_spelling(
    tmp_path: Path,
    type_name: str,
) -> None:
    schema = tmp_path / f"{type_name}.rng"
    schema.write_text(data_schema(type_name), encoding="utf-8")
    compiled = RelaxNGSchema.compile(schema)
    compiled.require_valid()

    accepted = tmp_path / "accepted.xml"
    accepted.write_text("<doc>INF</doc>", encoding="utf-8")
    assert not compiled.parse(accepted, typed=False).report.has_errors

    for spelling in ("-INF", "NaN"):
        accepted = tmp_path / f"accepted-{spelling}.xml"
        accepted.write_text(f"<doc>{spelling}</doc>", encoding="utf-8")
        assert not compiled.parse(accepted, typed=False).report.has_errors

    rejected = tmp_path / "rejected.xml"
    rejected.write_text("<doc>+INF</doc>", encoding="utf-8")
    assert compiled.parse(rejected, typed=False).report.has_errors


@pytest.mark.parametrize("type_name", ["ENTITY", "ENTITIES"])
def test_entity_types_require_declared_unparsed_entities(
    tmp_path: Path,
    type_name: str,
) -> None:
    schema = tmp_path / f"{type_name}.rng"
    schema.write_text(data_schema(type_name), encoding="utf-8")
    compiled = RelaxNGSchema.compile(schema)
    compiled.require_valid()

    undeclared = tmp_path / "undeclared.xml"
    undeclared.write_text("<doc>foo</doc>", encoding="utf-8")
    assert compiled.parse(undeclared, typed=False).report.has_errors

    declarations = (
        "<!DOCTYPE doc [\n"
        "<!ENTITY foo SYSTEM 'whatever' NDATA jpeg>\n"
        + ("<!ENTITY bar SYSTEM 'whatever' NDATA jpeg>\n" if type_name == "ENTITIES" else "")
        + "]>\n"
    )
    value = "foo" if type_name == "ENTITY" else " foo bar "
    declared = tmp_path / "declared.xml"
    declared.write_text(f"{declarations}<doc>{value}</doc>", encoding="utf-8")
    assert not compiled.parse(declared, typed=False).report.has_errors


def _entity_schema(tmp_path: Path) -> RelaxNGSchema:
    schema = tmp_path / "entity.rng"
    schema.write_text(data_schema("ENTITY"), encoding="utf-8")
    compiled = RelaxNGSchema.compile(schema)
    compiled.require_valid()
    return compiled


def test_entity_type_rejects_a_declared_parsed_entity(tmp_path: Path) -> None:
    compiled = _entity_schema(tmp_path)
    instance = tmp_path / "parsed-entity.xml"
    instance.write_text(
        '<!DOCTYPE doc [<!ENTITY foo "bar">]>\n<doc>foo</doc>',
        encoding="utf-8",
    )

    # A general parsed entity is not an unparsed entity value for xs:ENTITY.
    assert compiled.parse(instance, typed=False).report.has_errors


def test_external_dtd_keeps_the_verdict_and_emits_one_located_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _entity_schema(tmp_path)
    external = tmp_path / "external.dtd"
    external.write_text("<!ENTITY foo SYSTEM 'whatever' NDATA jpeg>\n", encoding="utf-8")
    instance = tmp_path / "external.xml"
    instance.write_text(
        f'<!DOCTYPE doc SYSTEM "{external.as_posix()}"><doc>foo</doc>',
        encoding="utf-8",
    )

    opened: list[str] = []
    original_open = open

    def tracking_open(file, *args, **kwargs):
        opened.append(str(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking_open)
    document = compiled.parse(instance, typed=False)

    assert not document.report.has_errors
    assert document.is_valid
    assert document.report.diagnostics_complete is True
    payload = document.report.to_dict()
    assert payload["diagnostics_complete"] is True
    assert payload["counts"] == {"error": 0, "warning": 1}
    assert payload["suppressed_count"] == 0
    assert [issue["code"] for issue in payload["issues"]] == ["dtd-external-skipped"]
    advisories = [
        issue for issue in document.report.warnings if issue.code == "dtd-external-skipped"
    ]
    assert len(advisories) == 1
    assert advisories[0].location is not None
    assert advisories[0].location.source == str(instance)
    assert advisories[0].location.path == (PathStep("doc", 1),)
    assert "external DTD declarations were not retrieved" in advisories[0].message
    # The external subset declares foo; the advisory and lexical fallback prove
    # neither declaration collection nor the instance parser retrieved it.
    assert str(external) not in opened


def test_suppressed_external_dtd_advisory_marks_the_report_incomplete(
    tmp_path: Path,
) -> None:
    compiled = _entity_schema(tmp_path)
    instance = tmp_path / "external.xml"
    instance.write_text(
        '<!DOCTYPE doc SYSTEM "missing.dtd"><doc>foo</doc>',
        encoding="utf-8",
    )

    document = compiled.parse(
        instance,
        typed=False,
        limits=RelaxNGLimits(max_diagnostics=0),
    )

    assert not document.report.has_errors
    assert document.report.warning_count == 1
    assert document.report.issues == []
    assert document.report.suppressed_count == 1
    assert document.report.diagnostics_complete is False
    payload = document.report.to_dict()
    assert payload["diagnostics_complete"] is False
    assert payload["counts"] == {"error": 0, "warning": 1}
    assert payload["suppressed_count"] == 1
    assert payload["issues"] == []


def test_nonzero_stream_position_does_not_use_a_different_prolog(tmp_path: Path) -> None:
    compiled = _entity_schema(tmp_path)
    text = '<!DOCTYPE doc SYSTEM "missing.dtd">\n<doc>bar</doc>'
    stream = io.StringIO(text)
    stream.seek(text.index("<doc>"))

    document = compiled.parse(stream, typed=False)

    assert not document.report.has_errors
    assert [issue for issue in document.report.issues if issue.code == "dtd-external-skipped"] == []


def test_entity_declarations_are_read_from_a_seekable_stream(tmp_path: Path) -> None:
    compiled = _entity_schema(tmp_path)
    declarations = "<!DOCTYPE doc [<!ENTITY foo SYSTEM 'whatever' NDATA jpeg>]>\n"

    declared = io.StringIO(f"{declarations}<doc>foo</doc>")
    assert not compiled.parse(declared, typed=False).report.has_errors

    undeclared = io.StringIO("<doc>foo</doc>")
    assert compiled.parse(undeclared, typed=False).report.has_errors


def test_entity_declarations_fall_back_for_non_replayable_sources(tmp_path: Path) -> None:
    compiled = _entity_schema(tmp_path)

    class _Unseekable:
        def __init__(self, text: str) -> None:
            self._stream = io.StringIO(text)

        def read(self, size: int = -1) -> str:
            return self._stream.read(size)

        def seekable(self) -> bool:
            return False

    class _NoSeekable:
        def __init__(self, text: str) -> None:
            self._stream = io.StringIO(text)

        def read(self, size: int = -1) -> str:
            return self._stream.read(size)

    class _BrokenSeekable(_Unseekable):
        def seekable(self) -> bool:
            return True

        def tell(self) -> int:
            raise OSError("source vanished")

    # No re-readable evidence means lexical-only entity validation: the
    # undeclared name is accepted rather than guessed as declared.
    assert not compiled.parse(_Unseekable("<doc>foo</doc>"), typed=False).report.has_errors
    assert not compiled.parse(_NoSeekable("<doc>foo</doc>"), typed=False).report.has_errors
    assert not compiled.parse(_BrokenSeekable("<doc>foo</doc>"), typed=False).report.has_errors
    assert not compiled.parse(ET.fromstring("<doc>foo</doc>"), typed=False).report.has_errors


def test_entity_declarations_stay_lexical_only_under_a_resource_policy(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "entity.rng"
    schema_path.write_text(data_schema("ENTITY"), encoding="utf-8")
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text("<doc>foo</doc>", encoding="utf-8")

    class _Policy:
        def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
            del base, purpose
            return ResourceData(source, Path(source).read_bytes())

        def parse(self, data: ResourceData, context: object) -> ET.Element:
            return parse_with_namespaces(io.BytesIO(data.content), context)  # type: ignore[arg-type]

    compiled = RelaxNGSchema.compile(schema_path, resource_policy=_Policy())
    compiled.require_valid()

    # A trusted policy parse is a caller boundary; ENTITY keeps its lexical
    # behavior because no core-admitted declaration evidence exists.
    assert not compiled.parse(instance_path, typed=False).report.has_errors


def test_entity_type_detection_covers_real_nested_patterns() -> None:
    factory = XsdDatatypeFactory()
    context = DatatypeContext(None, {})
    entity = factory.create("ENTITY", (), context)
    string = factory.create("string", (), context)

    assert _grammar_uses_unparsed_entity_types(DataExcept(entity, Empty()))

    recursive = Ref()
    recursive.bind(Group(recursive, Data(entity)))
    assert _grammar_uses_unparsed_entity_types(recursive)

    string_ref = Ref()
    string_ref.bind(Group(string_ref, Data(string)))
    assert not _grammar_uses_unparsed_entity_types(string_ref)


# ---------------------------------------------------------------------------
# Instance baseline, dispositions, and full-report evidence
# ---------------------------------------------------------------------------


def test_instance_baseline_update_cannot_remove_a_pass(tmp_path: Path) -> None:
    path = tmp_path / "baseline.toml"
    save_baseline(path, {"schema": "pass"}, instances={"instance": "pass"})

    with pytest.raises(BaselineError, match="refusing to remove"):
        report_relaxng.save_baseline_checked(
            path,
            {"schema": "pass"},
            instances={"instance": "fail"},
        )

    assert load_baseline(path).instances == {"instance": "pass"}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[instances]\n"x" = "skipped"\n', "unknown outcome"),
        ("instances = 1\n", "must be tables"),
    ],
)
def test_baseline_load_rejects_malformed_instance_sections(
    tmp_path: Path,
    text: str,
    message: str,
) -> None:
    path = tmp_path / "baseline.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(BaselineError, match=message):
        load_baseline(path)


def test_release_violations_read_instance_baselines(tmp_path: Path) -> None:
    case = write_instance_case(tmp_path, "instance", "invalid-instance")
    cases = {case.id: case}

    assert release_violations(cases, Baseline(instances={case.id: "pass"}), ()) == ()
    failed = release_violations(cases, Baseline(instances={case.id: "fail"}), ())
    assert [violation.outcome for violation in failed] == ["fail"]
    # An older schema-result carrier still works for an instance case.
    assert release_violations(cases, Baseline(results={case.id: "pass"}), ()) == ()
    missing = release_violations(cases, Baseline(), ())
    assert [violation.outcome for violation in missing] == [None]

    unknown = release_violations(
        cases,
        Baseline(instances={case.id: "pass", "stale": "pass"}),
        (),
    )
    assert [violation.case_id for violation in unknown] == ["stale"]


def test_full_report_shows_instance_failures_and_baseline_changes(
    tmp_path: Path,
) -> None:
    good = write_instance_case(tmp_path, "good", "valid-instance")
    failing = write_instance_case(
        tmp_path,
        "failing",
        "valid-instance",
        instance_text="<doc><extra/></doc>",
    )
    missing = write_instance_case(tmp_path, "missing", "invalid-instance")
    result = PyXSDDriver(timeout=None).run(failing)
    baseline = Baseline(instances={failing.id: "pass"})

    text = report_relaxng.render_report(
        (good, failing, missing),
        (),
        baseline,
        (),
        instance_results=(result,),
        instance_baseline=baseline,
    )

    assert "instance outcomes: fail 1, missing 2" in text
    assert "unresolved instance failures (1):" in text
    assert f"{failing.id} [valid-instance] fail (wrong-verdict, instance)" in text
    assert "instance baseline changes (1):" in text
    assert f"[regression] {failing.id}: pass -> fail" in text

    payload = json.loads(
        report_relaxng.render_json(
            (good, failing, missing),
            (),
            baseline,
            (),
            instance_results=(result,),
            instance_baseline=baseline,
        )
    )
    assert payload["scope"]["required_instance_assertions"] == 3
    assert [failure["case"] for failure in payload["instance_unresolved"]] == [failing.id]
    assert payload["instance_baseline_changes"] == [
        {
            "case": failing.id,
            "kind": "regression",
            "previous": "pass",
            "current": "fail",
        }
    ]


def test_full_report_lists_excluded_instance_assertions(tmp_path: Path) -> None:
    compact = write_instance_case(
        tmp_path,
        "compact",
        "valid-instance",
        profile=PROFILE_COMPACT_SYNTAX,
    )
    dispositions = (
        Disposition(
            id="compact-note",
            title="compact syntax",
            category=PROFILE_COMPACT_SYNTAX,
            cases=(compact.id,),
            rationale="depends on compact syntax",
            feature=PROFILE_COMPACT_SYNTAX,
        ),
    )

    text = report_relaxng.render_report(
        (compact,),
        (),
        Baseline(),
        dispositions,
        instance_results=(),
        instance_baseline=Baseline(),
    )

    assert "instance scope: 0 required instance assertions" in text
    assert "excluded instance assertions: 1" in text
    assert "compact-note [compact-syntax] compact syntax" in text
    assert "instance baseline: matches" in text


# ---------------------------------------------------------------------------
# Checked-in instance baseline and dispositions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not CORPUS_AVAILABLE,
    reason="the pinned RELAX NG catalogs are checkout-only",
)
def test_checked_in_dispositions_cover_every_instance_case_without_exclusions() -> None:
    cases = report_relaxng.instance_cases(report_relaxng.load_cases())
    dispositions = load_dispositions(report_relaxng.DISPOSITIONS)

    assignments = report_relaxng.case_dispositions(cases, dispositions)

    assert set(assignments) == {case.id for case in cases}
    assert len(assignments) == len(cases)
    assert report_relaxng.excluded_dependencies(cases, dispositions) == ()


@pytest.mark.skipif(
    not CORPUS_AVAILABLE,
    reason="the pinned RELAX NG catalogs are checkout-only",
)
def test_checked_in_instance_baseline_records_every_required_pass() -> None:
    cases = report_relaxng.required_instance_cases(report_relaxng.load_cases())
    baseline = load_baseline(report_relaxng.BASELINE)

    assert len(cases) == 1621
    assert set(baseline.instances) == {case.id for case in cases}
    assert set(baseline.instances.values()) == {"pass"}
    assert baseline.meta.get("instance_cases") == 1621


# ---------------------------------------------------------------------------
# The pinned instance corpus gate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not CORPUS_AVAILABLE or not corpus_enabled(),
    reason="set PYXSD_RUN_RELAXNG=1 and check out the pinned catalogs",
)
def test_pinned_instance_corpus_matches_the_checked_in_baseline() -> None:
    timeout = report_relaxng.timeout_from_environment()
    all_cases = report_relaxng.load_cases()
    instance_cases = report_relaxng.required_instance_cases(all_cases)
    schema_cases = report_relaxng.required_schema_cases(all_cases)
    dispositions = load_dispositions(report_relaxng.DISPOSITIONS)
    baseline = load_baseline(report_relaxng.BASELINE)

    instance_results = report_relaxng.run_cases(instance_cases, timeout=timeout)
    schema_results = report_relaxng.run_cases(schema_cases, timeout=timeout)
    outcomes = report_relaxng.results_to_keys(instance_results)
    failures = report_relaxng.unresolved_failures(instance_results)

    assert not failures, "\n".join(
        f"{failure.case.id} [{failure.case.kind}] {failure.outcome.value}"
        f" ({failure.failure.value}, {failure.phase}): {failure.detail or ''}"
        for failure in failures
    )
    assert set(outcomes) == {case.id for case in instance_cases}
    assert diff_instances(baseline, outcomes).changes == ()
    assert (
        release_violations(
            {case.id: case for case in all_cases},
            baseline,
            dispositions,
        )
        == ()
    )

    report = report_relaxng.render_report(
        all_cases,
        schema_results,
        baseline,
        dispositions,
        instance_results=instance_results,
        instance_baseline=baseline,
    )
    assert "instance scope: 1621 required instance assertions" in report
    assert "unresolved in-scope failures: none" in report
    assert "unresolved instance failures: none" in report
    assert "instance baseline: matches" in report
    assert "excluded-feature dependencies: none" in report

    payload = json.loads(
        report_relaxng.render_json(
            all_cases,
            schema_results,
            baseline,
            dispositions,
            instance_results=instance_results,
            instance_baseline=baseline,
        )
    )
    assert payload["scope"]["required_instance_assertions"] == 1621
    assert payload["scope"]["instance_core"] == 538
    assert payload["scope"]["instance_xsd_datatype"] == 1083
    assert payload["instance_unresolved"] == []
    assert payload["instance_baseline_changes"] == []


@pytest.mark.skipif(
    not CORPUS_AVAILABLE,
    reason="the pinned RELAX NG catalogs are checkout-only",
)
def test_instance_corpus_counts_cover_both_catalogs() -> None:
    cases = report_relaxng.instance_cases(report_relaxng.load_cases())
    spectest = [case for case in cases if case.id.startswith("spectest-")]
    xsdtest = [case for case in cases if case.id.startswith("xsdtest-")]

    assert len(cases) == 1621
    assert len(spectest) == 580
    assert len(xsdtest) == 1041
    assert sum(case.profile == PROFILE_CORE for case in cases) == 538
    assert sum(case.profile == PROFILE_XSD_DATATYPE for case in cases) == 1083
    assert sum(case.kind == "valid-instance" for case in cases) == 593
    assert sum(case.kind == "invalid-instance" for case in cases) == 1028
    assert sum(case.kind == "valid-instance" for case in spectest) == 289
    assert sum(case.kind == "invalid-instance" for case in spectest) == 291
    assert sum(case.kind == "valid-instance" for case in xsdtest) == 304
    assert sum(case.kind == "invalid-instance" for case in xsdtest) == 737
