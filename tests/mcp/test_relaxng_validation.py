"""RELAX NG validation, batches, and reports through clean-worker operations."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, upload

from pyxsd import __version__
from pyxsd.mcp import cache as cache_module
from pyxsd.mcp import worker_main
from pyxsd.mcp.cache import SchemaCache
from pyxsd.mcp.captures import CaptureFile, InputCapture
from pyxsd.mcp.config import Limits, relaxng_limits_for_mcp
from pyxsd.mcp.contracts import (
    BatchValidationItem,
    FileRef,
    Page,
    SchemaRef,
    ValidateDocumentRequest,
)
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.worker_protocol import BatchParseOperation, ParseOperation
from pyxsd.mcp.workers import capture_to_wire

NS = "http://relaxng.org/ns/structure/1.0"
XSDT = "http://www.w3.org/2001/XMLSchema-datatypes"

RNG_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="root">
      <oneOrMore>
        <element name="item">
          <attribute name="id"><data type="integer" datatypeLibrary="{XSDT}"/></attribute>
          <element name="value"><data type="integer" datatypeLibrary="{XSDT}"/></element>
        </element>
      </oneOrMore>
    </element>
  </start>
</grammar>"""

VALID_DOCUMENT = '<root><item id="1"><value>1</value></item></root>'
INVALID_DOCUMENT = (
    '<root><item id="a"><value>1</value></item><item id="bad"><value>nope</value></item></root>'
)
MALFORMED_DOCUMENT = '<root><item id="1"><value>1</value>'
MISMATCH_DOCUMENT = "<root><wrong/></root>"

XSD_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType><xs:sequence>
      <xs:element name="value" type="xs:int"/>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>"""
XSD_DOCUMENT = "<root><value>nope</value></root>"

#: The complete XSD worker report for ``XSD_SCHEMA``/``XSD_DOCUMENT``. Reports
#: with unspecified completeness must keep this exact JSON shape and order.
XSD_REPORT_WIRE = (
    '{"issues":[{"severity":"error","code":"value","message":"the value of the '
    "'value' element is not valid for its type: Not a valid integer: 'nope'\","
    '"element":"root|complexType","phase":"instance","location":{"source":'
    '"/workspace/document.xml","path":[{"name":"root","index":1},'
    '{"name":"value","index":1}],"attribute":null},"constraint":null,'
    '"details":null,"location_precision":"exact"}],"counts":{"error":1,'
    '"warning":0},"suppressed_count":0}'
)


def relaxng_ref(path="schema.rng"):
    return SchemaRef(source=FileRef(path=path), format="relax-ng")


def xsd_ref(path="schema.xsd"):
    return SchemaRef(source=FileRef(path=path))


def capture_of(root: Path, files: dict[str, str]) -> InputCapture:
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for name in sorted(files):
        data = files[name].encode()
        (root / name).write_bytes(data)
        entries.append(CaptureFile(name, hashlib.sha256(data).hexdigest(), len(data)))
    return InputCapture("workspace", 1, root, tuple(entries))


def parse_operation(
    capture: InputCapture,
    *,
    document: str = "document.xml",
    schema: str | None = "schema.rng",
    offset: int = 0,
    limit: int | None = None,
) -> ParseOperation:
    return ParseOperation(
        request_id="parse",
        capture=capture_to_wire(capture),
        schema_ref=None if schema is None else relaxng_ref(schema),
        document=FileRef(path=document),
        issue_offset=offset,
        issue_limit=limit,
    )


def execute(operation, limits: Limits | None = None):
    cache = SchemaCache(limits or Limits(enforced=True))
    try:
        return worker_main._execute(operation, cache, __version__)
    finally:
        cache.close()


def test_valid_relaxng_validation_reports_boolean_completeness(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "valid.xml": VALID_DOCUMENT},
            )
            envelope = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="valid.xml"),
                    schema_ref=relaxng_ref(),
                )
            )
            assert envelope.data["valid"] is True
            assert envelope.data["binding_available"] is True
            assert envelope.data["report"] is None
            assert envelope.artifacts == []

    anyio.run(scenario)

    capture = capture_of(
        tmp_path / "worker",
        {"schema.rng": RNG_SCHEMA, "document.xml": VALID_DOCUMENT},
    )
    result = execute(parse_operation(capture))
    assert result["report"] == {
        "issues": [],
        "counts": {"error": 0, "warning": 0},
        "suppressed_count": 0,
        "diagnostics_complete": True,
    }
    assert result["page"] == {"returned": 0, "truncated": False, "total": 0}


def test_invalid_relaxng_preserves_recovery_order_and_source_evidence(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "invalid.xml": INVALID_DOCUMENT},
            )
            envelope = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="invalid.xml"),
                    schema_ref=relaxng_ref(),
                )
            )
            assert envelope.data["valid"] is False
            report = envelope.data["report"]
            assert report["counts"] == {"error": 3, "warning": 0}
            assert report["suppressed_count"] == 0
            assert report["diagnostics_complete"] is True
            locations = [
                (issue["location"]["path"][-1], issue["location"]["attribute"])
                for issue in report["issues"]
            ]
            assert locations == [
                ({"name": "item", "index": 1}, "id"),
                ({"name": "item", "index": 2}, "id"),
                ({"name": "value", "index": 1}, None),
            ]
            assert [issue["details"]["actual-lexical"] for issue in report["issues"]] == [
                "a",
                "bad",
                "nope",
            ]
            assert {issue["location"]["source"] for issue in report["issues"]} == {
                "/workspace/invalid.xml"
            }
            assert [issue["phase"] for issue in report["issues"]] == [
                "instance",
                "instance",
                "instance",
            ]
            assert envelope.provenance["source"]["path"] == "invalid.xml"
            assert [artifact.kind for artifact in envelope.artifacts] == ["report"]

    anyio.run(scenario)


def test_malformed_missing_and_grammar_mismatch_are_distinct_outcomes(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.rng": RNG_SCHEMA,
                    "malformed.xml": MALFORMED_DOCUMENT,
                    "mismatch.xml": MISMATCH_DOCUMENT,
                },
            )
            with pytest.raises(ToolError) as malformed:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="malformed.xml"),
                        schema_ref=relaxng_ref(),
                    )
                )
            assert malformed.value.fault.code == "invalid_xml"
            assert malformed.value.fault.details["source"]["path"] == "malformed.xml"

            with pytest.raises(ToolError) as missing:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="absent.xml"),
                        schema_ref=relaxng_ref(),
                    )
                )
            assert missing.value.fault.code == "not_found"
            assert missing.value.fault.details["path"] == "absent.xml"

            mismatch = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="mismatch.xml"),
                    schema_ref=relaxng_ref(),
                )
            )
            assert mismatch.data["valid"] is False
            assert mismatch.data["binding_available"] is True
            assert mismatch.data["report"]["counts"]["error"] >= 1
            assert mismatch.data["report"]["diagnostics_complete"] is True

    anyio.run(scenario)


def test_validation_does_not_build_typed_evidence(tmp_path):
    """MCP validation is evidence-free; these budgets would fail a typed parse."""
    document = (
        '<root><item id="1"><value>1</value></item><item id="2"><value>2</value></item></root>'
    )

    async def scenario():
        limits = Limits(enforced=True, workers=1, relaxng_evidence_states=1)
        async with harness(tmp_path, limits) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "valid.xml": document},
            )
            envelope = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="valid.xml"),
                    schema_ref=relaxng_ref(),
                )
            )
            assert envelope.data["valid"] is True

    anyio.run(scenario)


def test_parse_time_budget_is_not_a_verdict(tmp_path):
    document = (
        '<root><item id="1"><value>1</value></item><item id="2"><value>2</value></item></root>'
    )

    async def scenario():
        limits = Limits(enforced=True, workers=1, relaxng_derivative_states=1)
        async with harness(tmp_path, limits) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "valid.xml": document},
            )
            with pytest.raises(ToolError) as raised:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="valid.xml"),
                        schema_ref=relaxng_ref(),
                    )
                )
            fault = raised.value.fault
            assert fault.code == "limit_exceeded"
            assert fault.code not in {"invalid_xml", "invalid_schema"}
            assert fault.details["stage"] == "validate"
            assert fault.details["budget"] == "max_derivative_states"
            assert fault.details["configured"] == 1
            assert fault.details["observed"] >= 2

    anyio.run(scenario)


def test_unsupported_datatype_is_not_a_verdict(tmp_path):
    schema = f"""<grammar xmlns="{NS}">
      <start>
        <element name="root">
          <data type="custom" datatypeLibrary="urn:example:custom"/>
        </element>
      </start>
    </grammar>"""

    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": schema, "valid.xml": VALID_DOCUMENT},
            )
            with pytest.raises(ToolError) as raised:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="valid.xml"),
                        schema_ref=relaxng_ref(),
                    )
                )
            fault = raised.value.fault
            assert fault.code == "unsupported_capability"
            assert fault.code not in {"invalid_xml", "invalid_schema"}
            assert "urn:example:custom" in fault.message

    anyio.run(scenario)


def test_zero_retention_reports_incomplete_diagnostics(tmp_path, monkeypatch):
    def zero_retention(limits: Limits):
        return replace(relaxng_limits_for_mcp(limits), max_diagnostics=0)

    monkeypatch.setattr(cache_module, "relaxng_limits_for_mcp", zero_retention)
    capture = capture_of(
        tmp_path,
        {"schema.rng": RNG_SCHEMA, "document.xml": INVALID_DOCUMENT},
    )
    result = execute(parse_operation(capture))
    report = result["report"]
    assert report["issues"] == []
    assert report["counts"] == {"error": 3, "warning": 0}
    assert report["suppressed_count"] == 3
    assert report["diagnostics_complete"] is False
    assert result["page"] == {"returned": 0, "truncated": False, "total": 3}


def test_each_parse_returns_an_independent_report(tmp_path):
    capture = capture_of(
        tmp_path,
        {"schema.rng": RNG_SCHEMA, "document.xml": INVALID_DOCUMENT},
    )
    cache = SchemaCache(Limits(enforced=True))
    try:
        first = worker_main._execute(parse_operation(capture), cache, __version__)
        second = worker_main._execute(parse_operation(capture), cache, __version__)
    finally:
        cache.close()
    assert first["report"] is not second["report"]
    assert first["report"]["issues"] is not second["report"]["issues"]
    first["report"]["issues"].clear()
    first["report"]["counts"]["error"] = 0
    assert second["report"]["counts"] == {"error": 3, "warning": 0}
    assert len(second["report"]["issues"]) == 3


def test_mixed_batch_keeps_each_items_report_shape(tmp_path):
    files = {
        "schema.rng": RNG_SCHEMA,
        "schema.xsd": XSD_SCHEMA,
        "rng-invalid.xml": INVALID_DOCUMENT,
        "xsd-invalid.xml": XSD_DOCUMENT,
        "rng-valid.xml": VALID_DOCUMENT,
        "xsd-valid.xml": "<root><value>2</value></root>",
    }
    capture = capture_of(tmp_path, files)
    operation = BatchParseOperation(
        request_id="batch",
        capture=capture_to_wire(capture),
        items=(
            BatchValidationItem(document=FileRef(path="rng-invalid.xml"), schema_ref=relaxng_ref()),
            BatchValidationItem(document=FileRef(path="xsd-invalid.xml"), schema_ref=xsd_ref()),
            BatchValidationItem(document=FileRef(path="rng-valid.xml"), schema_ref=relaxng_ref()),
            BatchValidationItem(document=FileRef(path="xsd-valid.xml"), schema_ref=xsd_ref()),
        ),
    )
    result = execute(operation)
    items = result["items"]
    assert [item["document"]["path"] for item in items] == [
        "rng-invalid.xml",
        "xsd-invalid.xml",
        "rng-valid.xml",
        "xsd-valid.xml",
    ]
    assert [item["valid"] for item in items] == [False, False, True, True]
    assert [item["fault"] for item in items] == [None, None, None, None]
    rng_report, xsd_report = items[0]["report"], items[1]["report"]
    assert rng_report["diagnostics_complete"] is True
    assert "diagnostics_complete" not in xsd_report
    assert rng_report["counts"] == {"error": 3, "warning": 0}
    assert xsd_report["counts"] == {"error": 1, "warning": 0}
    assert [item["report"] for item in items[2:]] == [None, None]
    assert [item["counts"] for item in items[2:]] == [
        {"error": 0, "warning": 0},
        {"error": 0, "warning": 0},
    ]


def test_issue_pages_preserve_counts_and_diagnostics(tmp_path):
    capture = capture_of(
        tmp_path,
        {"schema.rng": RNG_SCHEMA, "document.xml": INVALID_DOCUMENT},
    )
    first = execute(parse_operation(capture, limit=1))
    assert first["page"] == {"returned": 1, "truncated": True, "total": 3}
    assert first["report"]["counts"] == {"error": 3, "warning": 0}
    assert first["report"]["diagnostics_complete"] is True
    assert [issue["details"]["actual-lexical"] for issue in first["report"]["issues"]] == ["a"]

    second = execute(parse_operation(capture, offset=1, limit=1))
    assert second["page"] == {"returned": 1, "truncated": True, "total": 3}
    assert [issue["details"]["actual-lexical"] for issue in second["report"]["issues"]] == ["bad"]

    third = execute(parse_operation(capture, offset=2, limit=1))
    assert third["page"] == {"returned": 1, "truncated": False, "total": 3}
    assert [issue["details"]["actual-lexical"] for issue in third["report"]["issues"]] == ["nope"]

    with pytest.raises(ToolError, match="invalid_argument"):
        execute(
            parse_operation(capture, limit=3),
            Limits(enforced=True, page_items=2),
        )


def test_service_pages_cover_relaxng_issues(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "invalid.xml": INVALID_DOCUMENT},
            )
            first = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="invalid.xml"),
                    schema_ref=relaxng_ref(),
                    page=Page(limit=1),
                )
            )
            assert first.data["valid"] is False
            assert first.page.total == 3
            assert first.page.returned == 1
            assert first.page.truncated is True
            assert first.page.next_cursor is not None
            second = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="invalid.xml"),
                    schema_ref=relaxng_ref(),
                    page=Page(cursor=first.page.next_cursor, limit=1),
                )
            )
            assert second.page.returned == 1
            assert second.data["report"]["counts"] == {"error": 3, "warning": 0}
            assert second.data["report"]["diagnostics_complete"] is True
            with pytest.raises(ToolError, match="invalid_argument"):
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="invalid.xml"),
                        schema_ref=relaxng_ref(),
                        page=Page(limit=51),
                    )
                )

    anyio.run(scenario)


def test_xsd_report_wire_is_unchanged(tmp_path):
    capture = capture_of(
        tmp_path,
        {"schema.xsd": XSD_SCHEMA, "document.xml": XSD_DOCUMENT},
    )
    operation = ParseOperation(
        request_id="xsd",
        capture=capture_to_wire(capture),
        schema_ref=xsd_ref(),
        document=FileRef(path="document.xml"),
    )
    result = execute(operation)
    report = result["report"]
    assert list(report) == ["issues", "counts", "suppressed_count"]
    assert "diagnostics_complete" not in report
    assert json.dumps(report, separators=(",", ":")) == XSD_REPORT_WIRE
