"""Typed full-corpus equivalence for the pinned RELAX NG instance assertions.

Every release-required instance in the pinned catalogs is parsed twice through
the same compiled schema: once validation-only and once typed.  The typed
document is returned to :class:`PyXSDDriver`, so the catalog expectation still
settles the outcome exactly as it does in the validation-only gate.  Reports
must agree on the verdict, counts, retained diagnostics, severities, codes,
phases, messages, and locations.  Valid typed documents carry binding evidence;
invalid typed documents stay generic and expose no evidence.

The pinned run is opt-in with ``PYXSD_RUN_RELAXNG=1`` and the checkout-only
catalogs.  Synthetic tests always exercise the same equivalence parser.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests import report_relaxng
from tests.relaxng.baseline import diff_instances
from tests.relaxng.baseline import load as load_baseline
from tests.relaxng.dispositions import load_dispositions, release_violations
from tests.relaxng.driver import CaseFailure, PyXSDDriver
from tests.relaxng.model import (
    PROFILE_CORE,
    PROFILE_XSD_DATATYPE,
    CaseKind,
    CorpusCase,
    Outcome,
    corpus_enabled,
)

from pyxsd.exceptions import ValidationError
from pyxsd.relaxng.document import RelaxNGDocument
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.validation import ValidationReport

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
CORPUS_AVAILABLE = all(
    (report_relaxng.CORPUS / name).is_file() for name in ("spectest.xml", "xsdtest.xml")
)
EXPECTED_INSTANCE_ASSERTIONS = 1621

INTEGER_SCHEMA = (
    f'<element xmlns="{NS}" name="doc" datatypeLibrary="{XSD}"><data type="integer"/></element>'
)


def report_signature(
    report: ValidationReport,
) -> tuple[object, ...]:
    """Return every report field the typed and validation-only parses must share."""
    return (
        report.has_errors,
        report.error_count,
        report.warning_count,
        report.suppressed_count,
        report.diagnostics_complete,
        tuple(
            (
                issue.severity,
                issue.code,
                issue.phase,
                issue.location,
                issue.location_precision,
                issue.message,
            )
            for issue in report.issues
        ),
    )


def typed_equivalence_parser(
    schema: RelaxNGSchema,
    instance: Path,
    limits: RelaxNGLimits | None,
) -> RelaxNGDocument:
    """Parse both modes, require an identical report, and return the typed document."""
    validation_only = schema.parse(instance, typed=False, limits=limits)
    typed_document = schema.parse(instance, typed=True, limits=limits)

    assert typed_document.typed is True
    assert report_signature(validation_only.report) == report_signature(typed_document.report), (
        f"{instance}: typed and validation-only reports disagree"
    )
    if typed_document.report.has_errors:
        assert typed_document.binding_evidence is None
    else:
        assert typed_document.binding_evidence is not None
        assert typed_document.interpretations_current
    return typed_document


def write_instance_case(
    tmp_path: Path,
    *,
    kind: CaseKind,
    instance_text: str,
    schema_text: str = INTEGER_SCHEMA,
) -> CorpusCase:
    schema = tmp_path / "schema.rng"
    schema.write_text(schema_text, encoding="utf-8")
    instance = tmp_path / "instance.xml"
    instance.write_text(instance_text, encoding="utf-8")
    return CorpusCase(
        id=f"typed-{kind}",
        kind=kind,
        schema=schema,
        instance=instance,
        profile=PROFILE_CORE,
    )


# ---------------------------------------------------------------------------
# Synthetic equivalence-parser boundaries
# ---------------------------------------------------------------------------


def test_typed_equivalence_parser_keeps_clean_valid_roots_typed(tmp_path: Path) -> None:
    case = write_instance_case(tmp_path, kind="valid-instance", instance_text="<doc>12</doc>")
    result = PyXSDDriver(timeout=None, parser=typed_equivalence_parser).run(case)

    assert result.outcome is Outcome.PASS
    assert result.failure is CaseFailure.NONE
    assert result.phase == "instance"
    assert result.actual_valid is True

    compiled = RelaxNGSchema.compile(case.schema)
    compiled.require_valid()
    assert case.instance is not None
    document = typed_equivalence_parser(compiled, case.instance, None)
    assert document.typed is True
    assert document.is_valid
    assert document.binding_evidence is not None
    assert document.interpretations_current
    assert document.root.scalar_value() == 12


def test_typed_equivalence_parser_keeps_invalid_documents_generic(tmp_path: Path) -> None:
    case = write_instance_case(tmp_path, kind="invalid-instance", instance_text="<doc>words</doc>")
    result = PyXSDDriver(timeout=None, parser=typed_equivalence_parser).run(case)

    assert result.outcome is Outcome.PASS
    assert result.failure is CaseFailure.NONE
    assert result.phase == "instance"
    assert result.actual_valid is False

    compiled = RelaxNGSchema.compile(case.schema)
    compiled.require_valid()
    assert case.instance is not None
    document = typed_equivalence_parser(compiled, case.instance, None)
    assert document.typed is True
    assert not document.is_valid
    assert document.binding_evidence is None
    with pytest.raises(ValidationError):
        document.root.scalar_value()


# ---------------------------------------------------------------------------
# The pinned typed corpus gate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not CORPUS_AVAILABLE or not corpus_enabled(),
    reason="set PYXSD_RUN_RELAXNG=1 and check out the pinned catalogs",
)
def test_pinned_instance_corpus_is_typed_equivalent_to_validation_only() -> None:
    timeout = report_relaxng.timeout_from_environment()
    all_cases = report_relaxng.load_cases()
    instance_cases = report_relaxng.required_instance_cases(all_cases)
    dispositions = load_dispositions(report_relaxng.DISPOSITIONS)
    baseline = load_baseline(report_relaxng.BASELINE)

    driver = PyXSDDriver(timeout=timeout, parser=typed_equivalence_parser)
    results = tuple(driver.run(case) for case in instance_cases)

    failures = [result for result in results if not result.satisfies_expectation]
    assert not failures, "\n".join(
        f"{failure.case.id} [{failure.case.kind}] {failure.outcome.value}"
        f" ({failure.failure.value}, {failure.phase}): {failure.detail or ''}"
        for failure in failures
    )
    assert len(results) == EXPECTED_INSTANCE_ASSERTIONS
    assert {result.outcome for result in results} == {Outcome.PASS}
    assert {result.failure for result in results} == {CaseFailure.NONE}
    assert {result.phase for result in results} == {"instance"}
    assert all(result.actual_valid == result.expected_valid for result in results)
    for forbidden in (
        CaseFailure.SCHEMA_REJECTED,
        CaseFailure.ADAPTER_GAP,
        CaseFailure.CRASH,
        CaseFailure.LIMIT,
        CaseFailure.TIMEOUT,
    ):
        assert all(result.failure is not forbidden for result in results)

    spectest = [case for case in instance_cases if case.id.startswith("spectest-")]
    xsdtest = [case for case in instance_cases if case.id.startswith("xsdtest-")]
    assert len(spectest) == 580
    assert len(xsdtest) == 1041
    assert sum(case.profile == PROFILE_CORE for case in instance_cases) == 538
    assert sum(case.profile == PROFILE_XSD_DATATYPE for case in instance_cases) == 1083
    assert sum(case.kind == "valid-instance" for case in instance_cases) == 593
    assert sum(case.kind == "invalid-instance" for case in instance_cases) == 1028
    assert sum(case.kind == "valid-instance" for case in spectest) == 289
    assert sum(case.kind == "invalid-instance" for case in spectest) == 291
    assert sum(case.kind == "valid-instance" for case in xsdtest) == 304
    assert sum(case.kind == "invalid-instance" for case in xsdtest) == 737

    outcomes = report_relaxng.results_to_keys(results)
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
        (),
        baseline,
        dispositions,
        instance_results=results,
        instance_baseline=baseline,
    )
    assert "instance scope: 1621 required instance assertions" in report
    assert "unresolved instance failures: none" in report
    assert "instance baseline: matches" in report
