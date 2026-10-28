"""Exploration services compose session captures, clean workers and bounded pages."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio

from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import (
    ExplainIssueRequest,
    FileRef,
    FindComponentsRequest,
    InlineFile,
    InspectSchemaRequest,
    Page,
    QueryDocumentRequest,
    SchemaRef,
    ValidateDocumentRequest,
)
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.worker_protocol import WorkerReply

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="Item"><xs:sequence><xs:element name="value" type="xs:int"/></xs:sequence><xs:attribute name="id" type="xs:ID" use="required"/></xs:complexType>
  <xs:element name="root"><xs:complexType><xs:sequence><xs:element name="item" type="Item" minOccurs="0" maxOccurs="2"/></xs:sequence></xs:complexType></xs:element>
</xs:schema>"""
VALID = '<root><item id="a"><value>1</value></item></root>'
INVALID = '<root><item id="a"><value>1</value></item><item><value>bad</value></item><item id="c"><value>3</value></item></root>'


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
    inputs.outputs = outputs  # test-only handle for services that retain outputs
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
    return await inputs.put_files(
        workspace_id, [InlineFile(path=name, text=text) for name, text in files.items()]
    )


def schema_ref(path="schema.xsd"):
    return SchemaRef(source=FileRef(path=path))


def test_schema_overview_compact_constraints_and_graph(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            overview = await service.inspect_schema(
                InspectSchemaRequest(workspace_id=workspace, schema_ref=schema_ref())
            )
            assert overview.data["target_namespace"] is None
            assert overview.data["roots"] == ["element:root"]
            assert overview.data["counts"]["type"] == 2
            assert "components" not in overview.data
            compact = await service.inspect_schema(
                InspectSchemaRequest(
                    workspace_id=workspace,
                    schema_ref=schema_ref(),
                    component_ids=("type:Item",),
                    view="compact",
                    depth=0,
                ),
            )
            assert "class Item" in compact.data["stub"]
            full = await service.inspect_schema(
                InspectSchemaRequest(
                    workspace_id=workspace,
                    schema_ref=schema_ref(),
                    component_ids=("type:Item",),
                    view="full",
                    depth=0,
                ),
            )
            assert "class Item" in full.data["stub"]
            constraints = await service.inspect_schema(
                InspectSchemaRequest(
                    workspace_id=workspace,
                    schema_ref=schema_ref(),
                    component_ids=("type:Item",),
                    view="constraints",
                ),
            )
            assert constraints.data["components"][0]["id"] == "type:Item"
            found = await service.find_schema_components(
                FindComponentsRequest(
                    workspace_id=workspace, schema_ref=schema_ref(), text="item", kind="type"
                ),
            )
            assert [item["id"] for item in found.data["components"]] == ["type:Item"]
            named = await service.find_schema_components(
                FindComponentsRequest(
                    workspace_id=workspace, schema_ref=schema_ref(), kind="type", name="Item"
                ),
            )
            assert [item["id"] for item in named.data["components"]] == ["type:Item"]
            related = await service.find_schema_components(
                FindComponentsRequest(
                    workspace_id=workspace,
                    schema_ref=schema_ref(),
                    component_id="element:root",
                    direction="uses",
                    depth=2,
                ),
            )
            assert "type:Item" in {item["id"] for item in related.data["components"]}
            used_by = await service.find_schema_components(
                FindComponentsRequest(
                    workspace_id=workspace,
                    schema_ref=schema_ref(),
                    component_id="type:Item",
                    direction="used_by",
                    depth=2,
                ),
            )
            assert "element:root" in {item["id"] for item in used_by.data["components"]}
            with pytest.raises(ToolError, match="not_found"):
                await service.inspect_schema(
                    InspectSchemaRequest(
                        workspace_id=workspace,
                        schema_ref=schema_ref(),
                        component_ids=("type:Missing",),
                    ),
                )
            with pytest.raises(ToolError, match="not_found"):
                await service.find_schema_components(
                    FindComponentsRequest(
                        workspace_id=workspace,
                        schema_ref=schema_ref(),
                        component_id="type:Missing",
                    ),
                )

    anyio.run(scenario)


def test_validation_pages_preserve_counts_suppression_and_provenance(tmp_path):
    async def scenario():
        limits = Limits(workers=1, collected_issues=2)
        async with harness(tmp_path, limits) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "document.xml": INVALID})
            request = ValidateDocumentRequest(
                workspace_id=workspace,
                document=FileRef(path="document.xml"),
                schema_ref=schema_ref(),
                page=Page(limit=1),
            )
            first = await service.validate_document(request)
            assert first.data["valid"] is False
            assert first.data["binding_available"] is True
            assert first.data["report"]["counts"] == {"error": 4, "warning": 0}
            assert len(first.data["report"]["issues"]) == 1
            assert first.data["report"]["suppressed_count"] == 2
            assert first.page.total == 4 and first.page.truncated is True
            assert first.provenance["source"]["path"] == "document.xml"
            assert len(first.provenance["schema_fingerprint"]) == 64
            second = await service.validate_document(
                request.model_copy(update={"page": Page(cursor=first.page.next_cursor, limit=1)}),
            )
            assert second.page.returned == 1
            assert second.page.next_cursor is None and second.page.truncated is False
            assert second.data["report"]["counts"] == first.data["report"]["counts"]
            assert second.data["report"]["suppressed_count"] == 2
            with pytest.raises(ToolError, match="invalid_cursor"):
                await service.validate_document(
                    request.model_copy(
                        update={
                            "schema_ref": None,
                            "page": Page(cursor=first.page.next_cursor, limit=1),
                        }
                    ),
                )

    anyio.run(scenario)


def test_valid_malformed_unbindable_and_missing_paths(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.xsd": XSD,
                    "valid.xml": VALID,
                    "bad.xml": "<root>",
                    "unknown.xml": "<unknown/>",
                },
            )
            valid = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="valid.xml"),
                    schema_ref=schema_ref(),
                ),
            )
            assert valid.data["valid"] is True
            with pytest.raises(ToolError, match="invalid_xml") as malformed:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="bad.xml"),
                        schema_ref=schema_ref(),
                    ),
                )
            assert malformed.value.fault.details["source"]["path"] == "bad.xml"
            assert len(malformed.value.fault.details["schema_fingerprint"]) == 64
            with pytest.raises(ToolError, match="unbindable"):
                await service.query_document(
                    QueryDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="unknown.xml"),
                        schema_ref=schema_ref(),
                        xpath="/root",
                    ),
                )
            with pytest.raises(ToolError, match="invalid_xml"):
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="missing.xml"),
                        schema_ref=schema_ref(),
                    ),
                )

    anyio.run(scenario)


def test_query_exact_occurrence_subtree_external_functions_and_prefix_errors(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "document.xml": INVALID})
            result = await service.query_document(
                QueryDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="document.xml"),
                    schema_ref=schema_ref(),
                    xpath="/root/item[2]",
                    result="subtree",
                ),
            )
            item = result.data["results"][0]
            assert item["location"]["path"][-1]["index"] == 2
            assert "<value>bad</value>" in item["subtree"]
            summary = await service.query_document(
                QueryDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="document.xml"),
                    schema_ref=schema_ref(),
                    xpath="/root/item[1]",
                ),
            )
            assert summary.data["results"][0]["location"]["path"][-1]["index"] == 1
            scalar = await service.query_document(
                QueryDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="document.xml"),
                    schema_ref=schema_ref(),
                    xpath="/root/item/value/text()",
                ),
            )
            assert scalar.data["results"] == ["1", "bad", "3"]
            for xpath in ("doc('https://invalid.example/x')", "/p:root"):
                with pytest.raises(ToolError, match="invalid_query"):
                    await service.query_document(
                        QueryDocumentRequest(
                            workspace_id=workspace,
                            document=FileRef(path="document.xml"),
                            schema_ref=schema_ref(),
                            xpath=xpath,
                        ),
                    )

    anyio.run(scenario)


def test_hinted_schema_and_service_limit_and_revision_rejections(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            hinted = VALID.replace(
                "<root>",
                '<root xsi:noNamespaceSchemaLocation="schema.xsd" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
            )
            await upload(inputs, workspace, {"schema.xsd": XSD, "hinted.xml": hinted})
            result = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace, document=FileRef(path="hinted.xml")
                ),
            )
            assert result.data["valid"] is True
            assert result.provenance["options"] == {"hinted": True}
            with pytest.raises(ToolError, match="invalid_argument"):
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="hinted.xml"),
                        page=Page(limit=51),
                    ),
                )
            with pytest.raises(ToolError, match="not_found"):
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id="missing", document=FileRef(path="hinted.xml")
                    ),
                )
            empty = await service.inputs.create()
            with pytest.raises(ToolError, match="invalid_xml"):
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=empty.id, document=FileRef(path="hinted.xml")
                    ),
                )

    anyio.run(scenario)


def test_large_query_result_exceeds_http_frame_without_truncating_value(tmp_path):
    async def scenario():
        text_schema = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="root" type="xs:string"/></xs:schema>'
        text = "x" * (3 * 2**20)
        async with harness(tmp_path, Limits(workers=1, inline_bytes=4 * 2**20)) as (
            service,
            inputs,
            _pool,
            workspace,
        ):
            await upload(
                inputs, workspace, {"text.xsd": text_schema, "text.xml": f"<root>{text}</root>"}
            )
            result = await service.query_document(
                QueryDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="text.xml"),
                    schema_ref=schema_ref("text.xsd"),
                    xpath="string(/root)",
                ),
            )
            assert result.data["results"] == text

    anyio.run(scenario)


def test_envelope_and_report_shape_validation(tmp_path):
    from pyxsd.mcp.services.exploration import _valid_report

    assert not _valid_report([])
    assert not _valid_report({"counts": []})
    assert not _valid_report({"counts": {"error": 1}})
    assert _valid_report({"counts": {"error": 0}})

    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            with pytest.raises(ToolError, match="ipc_protocol"):
                service._envelope(
                    WorkerReply(request_id="bad", role="clean", status="ok", result=[]),
                    provenance={},
                    result_id="0" * 64,
                )

    anyio.run(scenario)


def test_explain_known_unknown_and_scoped_issue(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _inputs, _pool, _workspace):
            known = await service.explain_issue(ExplainIssueRequest(code="missing-attribute"))
            assert known.data["known"] is True
            assert "required" in known.data["summary"]
            unknown = await service.explain_issue(ExplainIssueRequest(code="future-code"))
            assert unknown.data == {
                "code": "future-code",
                "known": False,
                "summary": "Unknown issue code",
                "actions": [],
                "uncertainty": "No curated explanation is available for this code.",
            }
            report = {
                "issues": [
                    {"code": "value", "constraint": {"kind": "enumeration", "actual": "bad"}}
                ]
            }
            made = await service.outputs.put_bytes(
                json.dumps(report).encode(),
                name="report.json",
                media_type="application/json",
                kind="report",
            )
            from pyxsd.mcp.contracts import ArtifactRef

            ref = ArtifactRef(
                id=made.id,
                path=str(made.path),
                media_type=made.media_type,
                size=made.size,
                digest=made.digest,
                created_at=made.created_at,
                kind=made.kind,
            )
            scoped = await service.explain_issue(
                ExplainIssueRequest(code="value", report=ref, issue_index=0),
            )
            assert scoped.data["issue"]["constraint"]["actual"] == "bad"
            with pytest.raises(ToolError, match="invalid_argument"):
                await service.explain_issue(ExplainIssueRequest(code="value", report=ref))
            bad = await service.outputs.put_bytes(
                b"not-json", name="bad.json", media_type="application/json", kind="report"
            )
            malformed = ArtifactRef(
                id=bad.id,
                path=str(bad.path),
                media_type=bad.media_type,
                size=bad.size,
                digest=bad.digest,
                created_at=bad.created_at,
                kind=bad.kind,
            )
            with pytest.raises(ToolError, match="not_found"):
                await service.explain_issue(
                    ExplainIssueRequest(code="value", report=malformed, issue_index=0)
                )

    anyio.run(scenario)


def test_validation_without_an_output_store_keeps_the_report_inline(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "valid.xml": VALID})
            service.outputs = None
            result = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="valid.xml"),
                    schema_ref=schema_ref(),
                )
            )
            assert result.data["valid"] is True
            assert result.artifacts == []

    anyio.run(scenario)
