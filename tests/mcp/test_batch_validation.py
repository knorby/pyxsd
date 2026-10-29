"""Batch validation: one capture, one worker call, per-document outcomes."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio

from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import (
    BatchValidationItem,
    FileRef,
    InlineFile,
    ValidateDocumentsRequest,
)
from pyxsd.mcp.errors import ToolError

SCHEMA_A = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root-a">
    <xs:complexType><xs:sequence>
      <xs:element name="value" type="xs:int"/>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>"""
SCHEMA_B = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root-b">
    <xs:complexType><xs:sequence>
      <xs:element name="name" type="xs:string"/>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>"""
GOOD_A = "<root-a><value>1</value></root-a>"
BAD_A = "<root-a><value>nope</value></root-a>"
GOOD_B = "<root-b><name>x</name></root-b>"
MALFORMED = "<root-a><value>1</value>"


@asynccontextmanager
async def harness(tmp_path: Path, limits: Limits | None = None):
    from pyxsd.mcp.inputs import SessionInputs
    from pyxsd.mcp.output_store import OutputStore
    from pyxsd.mcp.services.exploration import ExplorationService
    from pyxsd.mcp.storage import Storage
    from pyxsd.mcp.workers import WorkerPool

    limits = limits or Limits(workers=1)
    storage = Storage(tmp_path / "state", persist=False)
    outputs = OutputStore(storage, limits)
    inputs = SessionInputs(storage.scratch, limits)
    pool = WorkerPool(limits)
    workspace = await inputs.create()
    service = ExplorationService(inputs, pool, outputs=outputs)
    try:
        yield service, inputs, pool, workspace.id
    finally:
        await pool.aclose()
        outputs.close()
        storage.close()


async def upload(inputs, workspace_id, files):
    await inputs.put_files(
        workspace_id, [InlineFile(path=name, text=text) for name, text in files.items()]
    )


def item(path, schema):
    return BatchValidationItem(
        document=FileRef(path=path),
        schema_ref=schema,
    )


def ref(path):
    from pyxsd.mcp.contracts import SchemaRef

    return SchemaRef(source=FileRef(path=path))


def spy(service):
    counts = {"capture": 0, "run": 0}
    inputs, pool = service.inputs, service.pool
    original_capture, original_run = inputs.capture, pool.run

    @asynccontextmanager
    async def capture_spy(workspace_id):
        counts["capture"] += 1
        async with original_capture(workspace_id) as captured:
            yield captured

    async def run_spy(operation, *, deadline):
        counts["run"] += 1
        return await original_run(operation, deadline=deadline)

    inputs.capture = capture_spy
    pool.run = run_spy
    return counts


def test_batch_mixed_schemas_one_capture_one_invocation(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema-a.xsd": SCHEMA_A,
                    "schema-b.xsd": SCHEMA_B,
                    "good-a.xml": GOOD_A,
                    "bad-a.xml": BAD_A,
                    "good-b.xml": GOOD_B,
                },
            )
            counts = spy(service)
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(
                        item("good-a.xml", ref("schema-a.xsd")),
                        item("bad-a.xml", ref("schema-a.xsd")),
                        item("good-b.xml", ref("schema-b.xsd")),
                    ),
                )
            )
            assert counts == {"capture": 1, "run": 1}
            results = envelope.data["results"]
            assert [result["valid"] for result in results] == [True, False, True]
            assert envelope.data["summary"] == {
                "total": 3,
                "valid": 2,
                "invalid": 1,
                "failed": 0,
            }
            good, bad, _other = results
            assert good["report"] is None and good["fault"] is None
            assert good["counts"] == {"error": 0, "warning": 0}
            assert good["provenance"]["schema_fingerprint"]
            assert bad["counts"]["error"] >= 1
            assert bad["fault"] is None
            assert bad["report"]["id"] and bad["report"]["kind"] == "report"
            assert bad["provenance"]["schema_fingerprint"]
            artifacts, _ = await service.outputs.list(after=None, limit=10)
            assert [reference.kind for reference in artifacts] == ["report"]

    anyio.run(scenario)


def test_batch_preserves_request_order_with_duplicates(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema-a.xsd": SCHEMA_A, "good-a.xml": GOOD_A})
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(
                        item("good-a.xml", ref("schema-a.xsd")),
                        item("good-a.xml", ref("schema-a.xsd")),
                    ),
                )
            )
            results = envelope.data["results"]
            assert len(results) == 2
            assert [result["document"]["path"] for result in results] == [
                "good-a.xml",
                "good-a.xml",
            ]
            assert all(result["valid"] is True for result in results)
            assert envelope.data["summary"]["valid"] == 2

    anyio.run(scenario)


def test_batch_missing_document_not_found(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema-a.xsd": SCHEMA_A, "good-a.xml": GOOD_A})
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(
                        item("good-a.xml", ref("schema-a.xsd")),
                        item("missing.xml", ref("schema-a.xsd")),
                    ),
                )
            )
            results = envelope.data["results"]
            assert results[0]["valid"] is True
            assert results[1]["fault"]["code"] == "not_found"

    anyio.run(scenario)


def test_batch_compile_fault_continues_and_duplicate_schema_provenance(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "a.xsd": SCHEMA_A,
                    "b.xsd": SCHEMA_B,
                    "bad.xsd": "<not-a-schema/>",
                    "good.xml": GOOD_A,
                },
            )
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(
                        item("good.xml", ref("bad.xsd")),
                        item("good.xml", ref("a.xsd")),
                        item("good.xml", ref("b.xsd")),
                    ),
                )
            )
            bad, good, other = envelope.data["results"]
            assert bad["fault"]["code"] == "invalid_schema"
            assert good["valid"] is True
            assert other["valid"] is False
            assert (
                good["provenance"]["schema_fingerprint"]
                != other["provenance"]["schema_fingerprint"]
            )

    anyio.run(scenario)


def test_hinted_schema_missing_include_fails_closed(tmp_path):
    from pyxsd.mcp.contracts import ValidateDocumentRequest

    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "a.xsd": SCHEMA_A_INCOMPLETE,
                    "hinted.xml": '<root-a xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="a.xsd"><value>1</value></root-a>',
                },
            )
            result = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(item("hinted.xml", None),),
                )
            )
            assert result.data["results"][0]["fault"]["code"] == "schema_incomplete"
            with pytest.raises(ToolError, match="schema_incomplete"):
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="hinted.xml"),
                    )
                )

    anyio.run(scenario)


def test_batch_invalid_without_output_store_stays_invalid(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"a.xsd": SCHEMA_A, "bad.xml": BAD_A})
            service.outputs = None
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(item("bad.xml", ref("a.xsd")),),
                )
            )
            assert envelope.data["summary"]["invalid"] == 1
            assert envelope.data["summary"]["failed"] == 0

    anyio.run(scenario)


def test_batch_malformed_xml_item_fault(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema-a.xsd": SCHEMA_A, "good-a.xml": GOOD_A, "broken.xml": MALFORMED},
            )
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(
                        item("broken.xml", ref("schema-a.xsd")),
                        item("good-a.xml", ref("schema-a.xsd")),
                    ),
                )
            )
            results = envelope.data["results"]
            assert results[0]["valid"] is None
            assert results[0]["fault"]["code"] == "invalid_xml"
            assert results[1]["valid"] is True
            assert envelope.data["summary"] == {
                "total": 2,
                "valid": 1,
                "invalid": 0,
                "failed": 1,
            }

    anyio.run(scenario)


def test_batch_empty_documents_invalid_argument(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _inputs, _pool, workspace):
            with pytest.raises(ToolError) as raised:
                await service.validate_documents(
                    ValidateDocumentsRequest(workspace_id=workspace, documents=())
                )
            assert raised.value.fault.code == "invalid_argument"

    anyio.run(scenario)


@pytest.mark.parametrize(
    "reply_data, expected",
    [
        ([], "ipc_protocol"),
        ({}, "ipc_protocol"),
        ({"items": None}, "ipc_protocol"),
        ("fault", "worker_timeout"),
    ],
)
def test_batch_protocol_faults_surface_as_tool_faults(tmp_path, reply_data, expected):
    from pyxsd.mcp.contracts import ToolFault
    from pyxsd.mcp.worker_protocol import WorkerReply

    async def scenario():
        async with harness(tmp_path) as (service, inputs, pool, workspace):
            await upload(inputs, workspace, {"a.xsd": SCHEMA_A, "a.xml": GOOD_A})

            async def reply(operation, *, deadline):
                if reply_data == "fault":
                    return WorkerReply(
                        request_id=operation.request_id,
                        role="clean",
                        status="error",
                        fault=ToolFault(code=expected, message="worker failed"),
                    )
                return WorkerReply(
                    request_id=operation.request_id, role="clean", status="ok", result=reply_data
                )

            pool.run = reply
            with pytest.raises(ToolError, match=expected):
                await service.validate_documents(
                    ValidateDocumentsRequest(
                        workspace_id=workspace, documents=(item("a.xml", ref("a.xsd")),)
                    )
                )

    anyio.run(scenario)


def test_batch_hints_success_and_malformed_continue(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "a.xsd": SCHEMA_A,
                    "hint.xml": '<root-a xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="a.xsd"><value>1</value></root-a>',
                    "bad.xml": MALFORMED,
                },
            )
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(item("bad.xml", None), item("hint.xml", None)),
                )
            )
            assert envelope.data["results"][0]["fault"]["code"] == "invalid_xml"
            assert envelope.data["results"][1]["valid"] is True

    anyio.run(scenario)


def test_batch_over_corpus_max_files_quota(tmp_path):
    async def scenario():
        limits = Limits(workers=1, corpus_files=2, corpus_max_files=2)
        async with harness(tmp_path, limits) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema-a.xsd": SCHEMA_A, "good-a.xml": GOOD_A})
            with pytest.raises(ToolError) as raised:
                await service.validate_documents(
                    ValidateDocumentsRequest(
                        workspace_id=workspace,
                        documents=tuple(item("good-a.xml", ref("schema-a.xsd")) for _ in range(3)),
                    )
                )
            assert raised.value.fault.code == "quota_exceeded"

    anyio.run(scenario)


def test_valid_documents_write_no_report_artifacts(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema-a.xsd": SCHEMA_A,
                    "schema-b.xsd": SCHEMA_B,
                    "good-a.xml": GOOD_A,
                    "good-b.xml": GOOD_B,
                },
            )
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(
                        item("good-a.xml", ref("schema-a.xsd")),
                        item("good-b.xml", ref("schema-b.xsd")),
                    ),
                )
            )
            assert envelope.data["summary"]["valid"] == 2
            artifacts, _ = await service.outputs.list(after=None, limit=10)
            assert artifacts == []

    anyio.run(scenario)


def test_batch_retains_all_collected_issues_not_only_first_ten(tmp_path):
    import json

    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "a.xsd": SCHEMA_A.replace(
                        'type="xs:int"', 'type="xs:int" maxOccurs="unbounded"'
                    ),
                    "bad.xml": "<root-a>" + "<value>bad</value>" * 20 + "</root-a>",
                },
            )
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(item("bad.xml", ref("a.xsd")),),
                )
            )
            reference = envelope.data["results"][0]["report"]
            data = json.loads(
                await service.outputs.read(reference["id"], offset=0, length=reference["size"])
            )
            assert len(data["issues"]) == 20
            assert data["suppressed_count"] == 0

    anyio.run(scenario)


SCHEMA_A_INCOMPLETE = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:include schemaLocation="missing-inc.xsd"/>
  <xs:element name="root-a">
    <xs:complexType><xs:sequence><xs:element name="value" type="xs:int"/></xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>"""


def test_batch_missing_schema_not_found(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema-a.xsd": SCHEMA_A, "good-a.xml": GOOD_A})
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(item("good-a.xml", ref("schema-z.xsd")),),
                )
            )
            assert envelope.data["results"][0]["fault"]["code"] == "not_found"
            assert "schema-z.xsd" in envelope.data["results"][0]["fault"]["message"]

    anyio.run(scenario)


def test_batch_partial_schema_incomplete(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema-a.xsd": SCHEMA_A_INCOMPLETE,
                    "schema-b.xsd": SCHEMA_B,
                    "good-a.xml": GOOD_A,
                    "good-b.xml": GOOD_B,
                },
            )
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(
                        item("good-a.xml", ref("schema-a.xsd")),
                        item("good-b.xml", ref("schema-b.xsd")),
                    ),
                )
            )
            results = envelope.data["results"]
            assert results[0]["valid"] is None
            assert results[0]["fault"]["code"] == "schema_incomplete"
            assert results[1]["valid"] is True
            assert envelope.data["summary"] == {
                "total": 2,
                "valid": 1,
                "invalid": 0,
                "failed": 1,
            }

    anyio.run(scenario)
