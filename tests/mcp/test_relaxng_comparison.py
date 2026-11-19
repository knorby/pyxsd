"""Language-neutral corpus comparison and explicit operational unknowns."""

from typing import cast

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp import test_comparison as xsd_corpus
from tests.mcp.test_exploration import harness, upload

from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.mcp.cache import SchemaCache
from pyxsd.mcp.captures import InputPolicyError
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, SchemaRef, ToolFault
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.services.comparison import (
    CompareSchemasRequest,
    ComparisonService,
    _classify,
    execute_comparison,
)
from pyxsd.mcp.worker_protocol import ComparisonOperation, WorkerReply
from pyxsd.mcp.workers import capture_to_wire
from pyxsd.relaxng.datatypes import DatatypeRegistrationError, UnsupportedDatatypeError
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.validation import ValidationReport
from pyxsd.xml_policy import XmlTreeLimitError

NS = "http://relaxng.org/ns/structure/1.0"

RNG_OLD = f"""<grammar xmlns="{NS}">
  <start>
    <element name="root">
      <element name="item">
        <choice>
          <data type="int" datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes">
            <param name="maxInclusive">5</param>
          </data>
          <value>legacy</value>
        </choice>
      </element>
    </element>
  </start>
</grammar>"""

RNG_NEW = f"""<grammar xmlns="{NS}">
  <start>
    <element name="root">
      <element name="item">
        <choice>
          <data type="int" datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes">
            <param name="maxInclusive">5</param>
          </data>
          <value>special</value>
        </choice>
      </element>
    </element>
  </start>
</grammar>"""

XSD_FIXTURE_NEW = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
 <xs:element name="root"><xs:complexType><xs:sequence>
   <xs:element name="item"><xs:simpleType><xs:union>
     <xs:simpleType><xs:restriction base="xs:int">
       <xs:maxInclusive value="5"/>
     </xs:restriction></xs:simpleType>
     <xs:simpleType><xs:restriction base="xs:string">
       <xs:enumeration value="special"/>
     </xs:restriction></xs:simpleType>
   </xs:union></xs:simpleType></xs:element>
 </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

XSD_FIXTURE_OLD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
 <xs:element name="root"><xs:complexType><xs:sequence>
   <xs:element name="item"><xs:simpleType><xs:union>
     <xs:simpleType><xs:restriction base="xs:int">
       <xs:maxInclusive value="5"/>
     </xs:restriction></xs:simpleType>
     <xs:simpleType><xs:restriction base="xs:string">
       <xs:enumeration value="legacy"/>
     </xs:restriction></xs:simpleType>
   </xs:union></xs:simpleType></xs:element>
 </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

VALID = "<root><item>3</item></root>"
REGRESSED = "<root><item>legacy</item></root>"
RESOLVED = "<root><item>special</item></root>"
INVALID_BOTH = "<root><item>nope</item></root>"
MALFORMED = "<root><item>"
UNBINDABLE = "<other/>"

RNG = '<element xmlns="http://relaxng.org/ns/structure/1.0" name="root"><empty/></element>'

UNSUPPORTED_RNG = f"""<grammar xmlns="{NS}">
  <start><element name="root">
    <data type="custom" datatypeLibrary="urn:example:custom"/>
  </element></start>
</grammar>"""

MALFORMED_SCHEMA = "<grammar"

UNDEFINED_REF_SCHEMA = f"""<grammar xmlns="{NS}">
  <start><element name="root"><ref name="missing"/></element></start>
</grammar>"""

PAIRS = [
    ("relax-ng", "relax-ng"),
    ("xsd", "relax-ng"),
    ("relax-ng", "xsd"),
]


def _schema_text(format: str, *, new: bool) -> str:
    if format == "relax-ng":
        return RNG_NEW if new else RNG_OLD
    return XSD_FIXTURE_NEW if new else XSD_FIXTURE_OLD


def _reference(path: str, format: str) -> SchemaRef:
    return SchemaRef(source=FileRef(path=path), format=format)


def _request(
    workspace: str, old_format: str, new_format: str, **overrides
) -> CompareSchemasRequest:
    fields = {
        "workspace_id": workspace,
        "old_schema": _reference("old.schema", old_format),
        "new_schema": _reference("new.schema", new_format),
    }
    fields.update(overrides)
    return CompareSchemasRequest(**fields)


def _schema_files(old_format: str, new_format: str, **extra: str) -> dict[str, str]:
    return {
        "old.schema": _schema_text(old_format, new=False),
        "new.schema": _schema_text(new_format, new=True),
        **extra,
    }


def _corpus(*paths: str) -> tuple[FileRef, ...]:
    return tuple(FileRef(path=path) for path in paths)


@pytest.mark.parametrize(("old_format", "new_format"), PAIRS)
def test_all_four_classifications_across_languages(tmp_path, old_format, new_format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                _schema_files(
                    old_format,
                    new_format,
                    **{
                        "valid.xml": VALID,
                        "regressed.xml": REGRESSED,
                        "resolved.xml": RESOLVED,
                        "invalid.xml": INVALID_BOTH,
                        "malformed.xml": MALFORMED,
                    },
                ),
            )
            result = await ComparisonService(inputs, pool).compare(
                _request(
                    workspace,
                    old_format,
                    new_format,
                    corpus=_corpus(
                        "valid.xml",
                        "regressed.xml",
                        "resolved.xml",
                        "invalid.xml",
                        "malformed.xml",
                    ),
                )
            )
            corpus = result["corpus"]
            assert corpus["status"] == "completed"
            statuses = {entry["path"]: entry["status"] for entry in corpus["files"]}
            assert statuses == {
                "valid.xml": "valid",
                "regressed.xml": "regressed",
                "resolved.xml": "resolved",
                "invalid.xml": "invalid",
                "malformed.xml": "malformed",
            }
            assert corpus["totals"] == {
                "valid": 1,
                "regressed": 1,
                "resolved": 1,
                "invalid": 1,
                "malformed": 1,
                "unbindable": 0,
                "skipped": 0,
                "unknown": 0,
            }
            assert corpus["regressions"] == ["regressed.xml"]
            assert corpus["resolutions"] == ["resolved.xml"]
            assert corpus["unknowns"] == []
            by_path = {entry["path"]: entry for entry in corpus["files"]}
            assert by_path["regressed.xml"]["evidence"][0]["code"]
            assert by_path["regressed.xml"]["evidence"][0]["message"]
            assert by_path["resolved.xml"]["evidence"][0]["code"]
            assert "evidence" not in by_path["valid.xml"]
            assert "evidence" not in by_path["malformed.xml"]
            assert result["conclusions"]["corpus_only"] is True
            if old_format == new_format:
                assert result["structural"]["totals"]["changed"] > 0
            else:
                assert result["structural"]["status"] == "not_comparable"

    anyio.run(scenario)


@pytest.mark.parametrize("format", ["relax-ng", "xsd"])
def test_identical_reference_uses_one_lease_and_still_classifies(tmp_path, format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, _, workspace):
            schema = RNG if format == "relax-ng" else xsd_corpus.IDENTICAL
            document = "<root/>" if format == "relax-ng" else '<root><item id="x"/></root>'
            await upload(inputs, workspace, {"schema.xml": schema, "doc.xml": document})
            ref = _reference("schema.xml", format)
            async with inputs.capture(workspace) as capture:
                cache = SchemaCache(inputs.limits)
                try:
                    result = execute_comparison(
                        ComparisonOperation(
                            request_id="comparison",
                            capture=capture_to_wire(capture),
                            old_schema=ref,
                            new_schema=ref,
                            corpus=_corpus("doc.xml"),
                            corpus_requested=True,
                        ),
                        cache,
                        "test",
                    )
                    assert cache.info().active == 0
                    assert cache.info().misses == 1
                finally:
                    cache.close()
            assert result["structural"]["totals"] == {"added": 0, "removed": 0, "changed": 0}
            (entry,) = result["corpus"]["files"]
            assert entry == {"path": "doc.xml", "status": "valid", "old": "valid", "new": "valid"}
            assert result["corpus"]["totals"]["valid"] == 1
            assert result["corpus"]["unknowns"] == []

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("old_format", "new_format"),
    [
        ("relax-ng", "relax-ng"),
        ("xsd", "relax-ng"),
        ("relax-ng", "xsd"),
        ("xsd", "xsd"),
    ],
)
def test_malformed_and_unbindable_semantics_survive(tmp_path, old_format, new_format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                _schema_files(
                    old_format,
                    new_format,
                    **{"malformed.xml": MALFORMED, "unbindable.xml": UNBINDABLE},
                ),
            )
            result = await ComparisonService(inputs, pool).compare(
                _request(
                    workspace,
                    old_format,
                    new_format,
                    corpus=_corpus("malformed.xml", "unbindable.xml"),
                )
            )
            by_path = {entry["path"]: entry for entry in result["corpus"]["files"]}
            assert by_path["malformed.xml"]["status"] == "malformed"
            assert by_path["malformed.xml"]["old"] == "malformed"
            assert by_path["malformed.xml"]["new"] == "malformed"
            unbindable = by_path["unbindable.xml"]
            if old_format == new_format == "xsd":
                assert unbindable["status"] == "unbindable"
                assert unbindable["old"] == "unbindable"
                assert unbindable["new"] == "unbindable"
            elif old_format == new_format == "relax-ng":
                assert unbindable["status"] == "invalid"
                assert unbindable["old"] == "invalid"
                assert unbindable["new"] == "invalid"
            else:
                assert unbindable["status"] == "invalid"
                assert "unbindable" in {unbindable["old"], unbindable["new"]}
                assert "invalid" in {unbindable["old"], unbindable["new"]}
            assert result["corpus"]["totals"]["malformed"] == 1
            assert result["corpus"]["totals"]["unknown"] == 0

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("old_format", "new_format"),
    [("relax-ng", "relax-ng"), ("xsd", "relax-ng"), ("relax-ng", "xsd")],
)
def test_empty_glob_corpus_makes_no_compatibility_claim(tmp_path, old_format, new_format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, _schema_files(old_format, new_format))
            result = await ComparisonService(inputs, pool).compare(
                _request(
                    workspace,
                    old_format,
                    new_format,
                    corpus_glob="missing/**/*.xml",
                )
            )
            assert result["corpus_selection"] == {"glob": "missing/**/*.xml", "matched": 0}
            corpus = result["corpus"]
            assert corpus["status"] == "empty"
            assert corpus["files"] == []
            assert corpus["totals"] == {
                "valid": 0,
                "regressed": 0,
                "resolved": 0,
                "invalid": 0,
                "malformed": 0,
                "unbindable": 0,
                "skipped": 0,
                "unknown": 0,
            }
            assert corpus["regressions"] == []
            assert corpus["resolutions"] == []
            assert corpus["unknowns"] == []
            assert "no compatibility claim" in corpus["statement"]
            assert result["conclusions"]["corpus_only"] is True

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("old_format", "new_format"),
    [("relax-ng", "relax-ng"), ("xsd", "relax-ng")],
)
def test_explicit_and_glob_corpus_selection(tmp_path, old_format, new_format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                _schema_files(
                    old_format,
                    new_format,
                    **{"corpus/a.xml": VALID, "corpus/nested/b.xml": REGRESSED},
                ),
            )
            service = ComparisonService(inputs, pool)
            explicit = await service.compare(
                _request(
                    workspace,
                    old_format,
                    new_format,
                    corpus=_corpus("corpus/a.xml", "corpus/nested/b.xml"),
                )
            )
            assert explicit["corpus_selection"] is None
            assert [entry["status"] for entry in explicit["corpus"]["files"]] == [
                "valid",
                "regressed",
            ]
            globbed = await service.compare(
                _request(
                    workspace,
                    old_format,
                    new_format,
                    corpus_glob="corpus/**/*.xml",
                )
            )
            assert globbed["corpus_selection"] == {
                "glob": "corpus/**/*.xml",
                "matched": 2,
            }
            assert [entry["path"] for entry in globbed["corpus"]["files"]] == [
                "corpus/a.xml",
                "corpus/nested/b.xml",
            ]
            assert [entry["status"] for entry in globbed["corpus"]["files"]] == [
                "valid",
                "regressed",
            ]

    anyio.run(scenario)


@pytest.mark.parametrize(("old_format", "new_format"), [*PAIRS, ("xsd", "xsd")])
def test_no_corpus_returns_structural_evidence_only(tmp_path, old_format, new_format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, _schema_files(old_format, new_format))
            result = await ComparisonService(inputs, pool).compare(
                _request(workspace, old_format, new_format)
            )
            assert result["corpus_selection"] is None
            assert "corpus" not in result
            assert result["conclusions"]["corpus_only"] is False
            assert result["conclusions"]["structural_only"] is True
            assert "No corpus documents were selected" in result["conclusions"]["statement"]
            if old_format == new_format:
                assert result["structural"]["totals"]["changed"] > 0
            else:
                assert result["structural"]["status"] == "not_comparable"

    anyio.run(scenario)


@pytest.mark.parametrize(("old_format", "new_format"), [("relax-ng", "xsd"), ("xsd", "relax-ng")])
def test_mixed_languages_keep_structural_limit_and_completed_corpus(
    tmp_path, old_format, new_format
):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                _schema_files(
                    old_format,
                    new_format,
                    **{"valid.xml": VALID, "regressed.xml": REGRESSED},
                ),
            )
            result = await ComparisonService(inputs, pool).compare(
                _request(
                    workspace,
                    old_format,
                    new_format,
                    corpus=_corpus("valid.xml", "regressed.xml"),
                )
            )
            structural = result["structural"]
            assert structural["status"] == "not_comparable"
            assert structural["old_format"] == old_format
            assert structural["new_format"] == new_format
            assert "same language" in structural["statement"]
            assert "added" not in structural
            corpus = result["corpus"]
            assert corpus["status"] == "completed"
            assert {entry["path"]: entry["status"] for entry in corpus["files"]} == {
                "valid.xml": "valid",
                "regressed.xml": "regressed",
            }
            assert corpus["regressions"] == ["regressed.xml"]

    anyio.run(scenario)


def test_relaxng_instance_limit_is_unknown_not_a_verdict(tmp_path):
    limits = Limits(enforced=True, workers=1, relaxng_derivative_states=1)

    async def scenario():
        async with harness(tmp_path, limits) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                _schema_files(
                    "relax-ng",
                    "relax-ng",
                    **{"valid.xml": VALID, "regressed.xml": REGRESSED},
                ),
            )
            result = await ComparisonService(inputs, pool).compare(
                _request(
                    workspace,
                    "relax-ng",
                    "relax-ng",
                    corpus=_corpus("valid.xml", "regressed.xml"),
                )
            )
            corpus = result["corpus"]
            assert corpus["status"] == "completed"
            assert [entry["status"] for entry in corpus["files"]] == ["unknown", "unknown"]
            assert corpus["totals"] == {
                "valid": 0,
                "regressed": 0,
                "resolved": 0,
                "invalid": 0,
                "malformed": 0,
                "unbindable": 0,
                "skipped": 0,
                "unknown": 2,
            }
            assert corpus["regressions"] == []
            assert corpus["resolutions"] == []
            assert corpus["unknowns"] == ["valid.xml", "regressed.xml"]
            assert "without a verdict" in corpus["statement"]
            for entry in corpus["files"]:
                assert entry["old"] == "unknown"
                assert entry["new"] == "unknown"
                for side in ("old", "new"):
                    fault = entry["faults"][side]
                    assert fault["code"] == "limit_exceeded"
                    assert fault["details"]["stage"] == "validate"
                    assert fault["details"]["budget"] == "max_derivative_states"
                    assert fault["details"]["configured"] == 1
                    assert fault["details"]["observed"] >= 2

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (UNSUPPORTED_RNG, "unsupported_capability"),
        (MALFORMED_SCHEMA, "invalid_schema"),
        (UNDEFINED_REF_SCHEMA, "invalid_schema"),
    ],
)
def test_relaxng_schema_failures_are_typed_faults(tmp_path, schema, expected):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "broken.rng": schema,
                    "good.rng": RNG_OLD,
                    "doc.xml": VALID,
                },
            )
            with pytest.raises(ToolError) as raised:
                await ComparisonService(inputs, pool).compare(
                    _request(
                        workspace,
                        "relax-ng",
                        "relax-ng",
                        old_schema=_reference("broken.rng", "relax-ng"),
                        new_schema=_reference("good.rng", "relax-ng"),
                        corpus=_corpus("doc.xml"),
                    )
                )
            fault = raised.value.fault
            assert fault.code == expected
            assert fault.code not in {"invalid_xml", "invalid_data"}

    anyio.run(scenario)


def test_relaxng_compile_limit_is_a_typed_fault_not_a_corpus_verdict(tmp_path):
    limits = Limits(enforced=True, workers=1, relaxng_pattern_nodes=1)

    async def scenario():
        async with harness(tmp_path, limits) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                _schema_files(
                    "relax-ng",
                    "relax-ng",
                    **{"valid.xml": VALID},
                ),
            )
            with pytest.raises(ToolError) as raised:
                await ComparisonService(inputs, pool).compare(
                    _request(
                        workspace,
                        "relax-ng",
                        "relax-ng",
                        corpus=_corpus("valid.xml"),
                    )
                )
            fault = raised.value.fault
            assert fault.code == "limit_exceeded"
            assert fault.code not in {"invalid_xml", "invalid_schema", "invalid_data"}
            assert fault.details["stage"] == "compile"
            assert fault.details["budget"] == "max_pattern_nodes"

    anyio.run(scenario)


def test_corpus_time_budget_skips_remaining_relaxng_files(tmp_path):
    async def scenario():
        async with harness(tmp_path, Limits(enforced=True, workers=1, corpus_file_seconds=1)) as (
            _,
            inputs,
            _,
            workspace,
        ):
            await upload(
                inputs,
                workspace,
                _schema_files(
                    "relax-ng",
                    "relax-ng",
                    **{"valid.xml": VALID, "regressed.xml": REGRESSED},
                ),
            )
            async with inputs.capture(workspace) as capture:
                operation = ComparisonOperation(
                    request_id="timeout",
                    capture=capture_to_wire(capture),
                    old_schema=_reference("old.schema", "relax-ng"),
                    new_schema=_reference("new.schema", "relax-ng"),
                    corpus=_corpus("valid.xml", "regressed.xml"),
                    corpus_requested=True,
                )
                cache = SchemaCache(inputs.limits)
                try:
                    ticks = iter([0.0, 0.0, 100.0])
                    result = execute_comparison(operation, cache, "test", clock=lambda: next(ticks))
                finally:
                    cache.close()
            assert result["corpus"]["files"] == [
                {"path": "valid.xml", "status": "valid", "old": "valid", "new": "valid"},
                {
                    "path": "regressed.xml",
                    "status": "skipped",
                    "old": "skipped",
                    "new": "skipped",
                },
            ]
            assert result["corpus"]["totals"]["skipped"] == 1
            assert result["corpus"]["totals"]["unknown"] == 0

    anyio.run(scenario)


@pytest.mark.parametrize(
    "fault",
    [
        ToolFault(code="worker_died", message="Worker died without a verdict"),
        ToolFault(code="deadline_exceeded", message="Comparison budget exhausted"),
    ],
)
def test_worker_crash_and_time_budget_never_become_corpus_verdicts(tmp_path, fault):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                _schema_files(
                    "relax-ng",
                    "relax-ng",
                    **{"valid.xml": VALID},
                ),
            )
            service = ComparisonService(inputs, pool)

            async def failed(operation, *, deadline):
                return WorkerReply(
                    request_id=operation.request_id,
                    role="clean",
                    status="error",
                    fault=fault,
                )

            service.pool.run = failed
            with pytest.raises(ToolError) as raised:
                await service.compare(
                    _request(
                        workspace,
                        "relax-ng",
                        "relax-ng",
                        corpus=_corpus("valid.xml"),
                    )
                )
            assert raised.value.fault.code == fault.code

    anyio.run(scenario)


class _RaisingSchema:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def parse(self, path, **kwargs):
        raise self.error


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            RelaxNGLimitError("validate", "max_derivative_states", 1, 2),
            "unknown",
            "limit_exceeded",
        ),
        (UnsupportedDatatypeError("no such datatype"), "unknown", "unsupported_capability"),
        (DatatypeRegistrationError("bad registration"), "unknown", "unsupported_capability"),
        (ValidationError("invalid schema", ValidationReport()), "unknown", "invalid_schema"),
        (XmlTreeLimitError("too deep"), "unknown", "limit_exceeded"),
        (InputPolicyError("xml_limit", "too large"), "unknown", "limit_exceeded"),
        (InputPolicyError("source_changed", "changed"), "unknown", "source_changed"),
        (PyXSDError("unexpected operation"), "unknown", "unknown"),
    ],
)
def test_classify_never_decides_an_operational_outcome(error, status, code):
    classification = _classify(_RaisingSchema(error), "doc.xml", message_bytes=5)
    assert classification.status == status
    assert classification.document is None
    assert classification.fault is not None
    assert classification.fault["code"] == code
    assert len(cast(str, classification.fault["message"])) == 5


def test_classify_source_failures_are_malformed_and_unbounded_messages():
    malformed = PyXSDError("the xml file is not well-formed XML")
    malformed.__cause__ = OSError("bad read")
    classification = _classify(_RaisingSchema(malformed), "doc.xml")
    assert classification.status == "malformed"
    assert classification.fault is None

    unknown = _classify(_RaisingSchema(PyXSDError("unexpected operation")), "doc.xml")
    assert unknown.status == "unknown"
    assert unknown.fault is not None
    assert unknown.fault["message"] == "unexpected operation"
    assert unknown.fault["details"] == {}


def test_xsd_comparison_retains_existing_corpus_contract(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "old.xsd": xsd_corpus.OLD,
                    "new.xsd": xsd_corpus.NEW,
                    "good.xml": xsd_corpus.GOOD,
                    "regressed.xml": xsd_corpus.REGRESSED,
                    "resolved.xml": xsd_corpus.RESOLVED,
                    "invalid.xml": xsd_corpus.INVALID_BOTH,
                    "malformed.xml": xsd_corpus.MALFORMED,
                    "unbindable.xml": xsd_corpus.UNBINDABLE,
                },
            )
            result = await ComparisonService(inputs, pool).compare(
                _request(
                    workspace,
                    "xsd",
                    "xsd",
                    old_schema=SchemaRef(source=FileRef(path="old.xsd")),
                    new_schema=SchemaRef(source=FileRef(path="new.xsd")),
                    corpus=_corpus(
                        "good.xml",
                        "regressed.xml",
                        "resolved.xml",
                        "invalid.xml",
                        "malformed.xml",
                        "unbindable.xml",
                    ),
                )
            )
            assert {entry["path"]: entry["status"] for entry in result["corpus"]["files"]} == {
                "good.xml": "valid",
                "regressed.xml": "regressed",
                "resolved.xml": "resolved",
                "invalid.xml": "invalid",
                "malformed.xml": "malformed",
                "unbindable.xml": "unbindable",
            }
            assert result["corpus"]["totals"] == {
                "valid": 1,
                "regressed": 1,
                "resolved": 1,
                "invalid": 1,
                "malformed": 1,
                "unbindable": 1,
                "skipped": 0,
                "unknown": 0,
            }
            assert result["structural"]["totals"]["added"] == 2
            regressed = next(
                entry for entry in result["corpus"]["files"] if entry["path"] == "regressed.xml"
            )
            assert regressed["evidence"][0]["code"]
            assert regressed["evidence"][0]["location"] is not None

    anyio.run(scenario)
