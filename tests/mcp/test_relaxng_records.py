"""RELAX NG record selectors survive wire frames, rows, and retained artifacts."""

import base64
import io
import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("pyarrow")
pytest.importorskip("pyarrow.parquet")

import anyio
import pyarrow as pa
import pyarrow.parquet as pq
from tests.mcp.test_exploration import harness, upload
from tests.mcp.test_relaxng_models import XSD, element

from pydantic import ValidationError
from pyxsd.integrations.projection import FieldSource, field_source_metadata
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.services.records import (
    ExportRecordsRequest,
    PreviewRecordsRequest,
    RecordService,
    _record_columns,
)
from pyxsd.mcp.worker_protocol import (
    CaptureMessage,
    FieldSourceSpec,
    RecordsOperation,
    decode_operation,
    encode_frame,
)
from pyxsd.relaxng import RelaxNGSchema
from pyxsd.relaxng.contracts import candidate_id

INT = (XSD, "int")
STRING = (XSD, "string")
AMBIGUOUS = element('<choice><data type="int"/><data type="string"/></choice>')


def request(workspace, **kwargs):
    return dict(
        workspace_id=workspace,
        schema_ref=SchemaRef(source=FileRef(path="schema.rng"), format="relax-ng"),
        element="doc",
        **kwargs,
    )


def test_selector_python_and_ipc_round_trips_and_legacy_defaults():
    for selector in ({}, {"datatype": INT}, {"candidate_id": "candidate"}):
        operation = RecordsOperation(
            request_id="selectors",
            capture=CaptureMessage(root="/unused", files=()),
            schema_ref=SchemaRef(source=FileRef(path="schema.rng"), format="relax-ng"),
            document=FileRef(path="input.xml"),
            element="doc",
            columns={"value": FieldSourceSpec(**selector)},
        )
        decoded = decode_operation(
            encode_frame(operation, max_bytes=None), max_bytes=None, role="clean"
        )
        assert decoded == operation
        source = _record_columns(decoded)["value"]
        assert source == FieldSource(**selector)
        assert field_source_metadata(source) == {
            "scope": "record",
            "path": (),
            "attribute": None,
            "levels": 0,
            **selector,
        }
    with pytest.raises(ValidationError, match="mutually exclusive"):
        FieldSourceSpec(datatype=INT, candidate_id="candidate")


@pytest.mark.parametrize("selector", ["datatype", "candidate_id"])
@pytest.mark.parametrize("format", ["parquet", "dataset"])
def test_selectors_preview_export_arrow_metadata_and_manifest(tmp_path, selector, format):
    schema = RelaxNGSchema.compile(io.StringIO(AMBIGUOUS))
    route = schema.projection.routes[schema.projection.roots[0]]
    contract = next(c for c in route.content.scalar_contracts if c.type_name == "int")
    selection = (
        {"datatype": INT}
        if selector == "datatype"
        else {"candidate_id": candidate_id(schema._grammar_fingerprint, contract)}
    )

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": AMBIGUOUS, "input.xml": "<doc>12</doc>"})
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            common = request(workspace, columns={"value": FieldSourceSpec(**selection)})
            result = await service.preview_records(
                PreviewRecordsRequest(**common, document=FileRef(path="input.xml"))
            )
            assert result.data["rows"] == [{"value": 12}]
            assert result.data["whole_xml_validity"] is False
            assert result.data["route_scope"] == "start"
            result = await service.export_records(
                ExportRecordsRequest(
                    **common, documents=(FileRef(path="input.xml"),), format=format
                )
            )
            assert result["row_count"] == 1
            assert result["whole_xml_validity"] is False
            artifacts = result["artifacts"]
            tables = []
            for artifact in artifacts:
                raw = await inputs.outputs.read(artifact["id"], offset=0, length=artifact["size"])
                if artifact["path"].endswith(".parquet"):
                    table = pq.read_table(pa.BufferReader(raw))
                    source = json.loads(table.schema.field("value").metadata[b"pyxsd:source"])
                    assert source[selector] == (
                        list(INT) if selector == "datatype" else selection[selector]
                    )
                    assert ("candidate_id" if selector == "datatype" else "datatype") not in source
                    tables.append(table)
                else:
                    manifest = json.loads(raw)
                    assert manifest["schema_format"] == "relaxng"
                    field = next(
                        f for f in manifest["projection"]["fields"] if f["name"] == "value"
                    )
                    metadata = {
                        base64.b64decode(m["key_base64"]): base64.b64decode(m["value_base64"])
                        for m in field["metadata"]
                    }
                    assert json.loads(metadata[b"pyxsd:source"])[selector] == (
                        list(INT) if selector == "datatype" else selection[selector]
                    )
            assert any(t.num_rows == 1 and t.column("value").to_pylist() == [12] for t in tables)

    anyio.run(scenario)


@pytest.mark.parametrize(
    "selection", [{}, {"datatype": (XSD, "boolean")}, {"candidate_id": "unknown"}]
)
def test_missing_and_unknown_selectors_are_projection_faults(tmp_path, selection):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": AMBIGUOUS, "input.xml": "<doc>12</doc>"})
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            with pytest.raises(ToolError) as error:
                await service.preview_records(
                    PreviewRecordsRequest(
                        **request(workspace, columns={"value": FieldSourceSpec(**selection)}),
                        document=FileRef(path="input.xml"),
                    )
                )
            assert error.value.fault.code == "unsupported_projection"
            assert (
                "selector" in error.value.fault.message or "candidate" in error.value.fault.message
            )

    anyio.run(scenario)


@pytest.mark.parametrize("action", ["preview", "parquet", "dataset"])
def test_per_row_candidate_failure_is_not_null_string_or_invalid_document(tmp_path, action):
    async def scenario():
        async with harness(tmp_path, Limits(enforced=True, preview_rows=1)) as (
            _,
            inputs,
            pool,
            workspace,
        ):
            await upload(
                inputs,
                workspace,
                {
                    "schema.rng": element(
                        '<oneOrMore><element name="row"><choice><data type="int"/>'
                        '<data type="string"/></choice></element></oneOrMore>'
                    ),
                    "input.xml": "<doc><row>12</row><row>text</row></doc>",
                },
            )
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            common = request(
                workspace,
                path=("row",),
                selector="row",
                columns={"value": FieldSourceSpec(datatype=INT)},
            )
            with pytest.raises(ToolError) as error:
                if action == "preview":
                    await service.preview_records(
                        PreviewRecordsRequest(**common, document=FileRef(path="input.xml"))
                    )
                else:
                    await service.export_records(
                        ExportRecordsRequest(
                            **common, documents=(FileRef(path="input.xml"),), format=action
                        )
                    )
            assert error.value.fault.code == "unsupported_projection"
            assert "row 2" in error.value.fault.message
            assert "did not survive" in error.value.fault.message
            assert not list(service.staging.iterdir())

    anyio.run(scenario)


def test_local_route_lists_and_source_root_columns(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.rng": element(
                        '<attribute name="version"><data type="int"/></attribute>'
                        '<oneOrMore><element name="row"><list><oneOrMore><data type="int"/>'
                        "</oneOrMore></list></element></oneOrMore>"
                    ),
                    "input.xml": '<doc version="3"><row>1 2</row><row>4</row></doc>',
                },
            )
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            common = request(
                workspace,
                path=("row",),
                columns={
                    "values": FieldSourceSpec(),
                    "version": FieldSourceSpec(scope="root", attribute="version"),
                },
            )
            preview = PreviewRecordsRequest(**common, document=FileRef(path="input.xml"))
            with pytest.raises(ToolError, match="explicit selector") as error:
                await service.preview_records(preview)
            assert error.value.fault.code == "unsupported_projection"
            selected = await service.preview_records(preview.model_copy(update={"selector": "row"}))
            assert selected.data["rows"] == [
                {"values": [1, 2], "version": 3},
                {"values": [4], "version": 3},
            ]
            assert selected.data["route_scope"] == "local"
            assert selected.data["route_element"] == "row"
            assert selected.data["whole_xml_validity"] is False
            with pytest.raises(ToolError) as wrong_root:
                await service.preview_records(
                    PreviewRecordsRequest(
                        **request(
                            workspace,
                            columns={"version": FieldSourceSpec(attribute="version")},
                            selector="row",
                        ),
                        document=FileRef(path="input.xml"),
                    )
                )
            assert wrong_root.value.fault.code == "unsupported_projection"
            assert "actual ancestor path doc/row" in wrong_root.value.fault.message

    anyio.run(scenario)


def test_jointly_incompatible_columns_fail_without_a_partial_row(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.rng": element(
                        '<choice><group><element name="x"><data type="int"/></element>'
                        '<element name="y"><data type="int"/></element></group>'
                        '<group><element name="x"><data type="string"/></element>'
                        '<element name="y"><data type="string"/></element></group></choice>'
                    ),
                    "input.xml": "<doc><x>12</x><y>34</y></doc>",
                },
            )
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            with pytest.raises(ToolError) as error:
                await service.preview_records(
                    PreviewRecordsRequest(
                        **request(
                            workspace,
                            columns={
                                "x": FieldSourceSpec(path=("x",), datatype=INT),
                                "y": FieldSourceSpec(path=("y",), datatype=STRING),
                            },
                        ),
                        document=FileRef(path="input.xml"),
                    )
                )
            assert error.value.fault.code == "unsupported_projection"
            assert "row 1" in error.value.fault.message
            assert "conflict" in error.value.fault.message

    anyio.run(scenario)


def test_open_name_scope_and_start_route_id_limits_are_explicit(tmp_path):
    from tests.mcp.test_relaxng_models import NS

    text = f'<element xmlns="{NS}"><anyName/><text/></element>'
    schema = RelaxNGSchema.compile(io.StringIO(text))
    route_id = schema.projection.roots[0]

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs, workspace, {"schema.rng": text, "input.xml": "<anything>x</anything>"}
            )
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            common = request(workspace, columns={"value": FieldSourceSpec()})
            result = await service.preview_records(
                PreviewRecordsRequest(**common, document=FileRef(path="input.xml")).model_copy(
                    update={"element": "anything"}
                )
            )
            assert result.data["rows"] == [{"value": "x"}]
            assert result.data["route_scope"] == "start"
            assert result.data["route_element"] is None
            assert result.data["whole_xml_validity"] is False
            with pytest.raises(ToolError) as error:
                await service.preview_records(
                    PreviewRecordsRequest(**common, document=FileRef(path="input.xml")).model_copy(
                        update={"element": route_id}
                    )
                )
            assert error.value.fault.code == "unsupported_projection"
            assert error.value.fault.details["scope"] == "start"

    anyio.run(scenario)


def test_xsd_selector_defaults_preserve_exact_arrow_source_bytes(tmp_path):
    from tests.mcp.test_records import XSD as SCHEMA
    from tests.mcp.test_records import xml

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": SCHEMA, "input.xml": xml(1)})
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            result = await service.export_records(
                ExportRecordsRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                    documents=(FileRef(path="input.xml"),),
                    element="root",
                    path=("item",),
                    selector="item",
                    columns={"id": FieldSourceSpec(attribute="id")},
                )
            )
            assert "whole_xml_validity" not in result
            artifact = result["artifacts"][0]
            raw = await inputs.outputs.read(artifact["id"], offset=0, length=artifact["size"])
            table = pq.read_table(pa.BufferReader(raw))
            assert table.to_pylist() == [{"id": "0"}]
            assert table.schema.metadata[b"pyxsd:projection"] == b"2"
            assert table.schema.field("id").metadata[b"pyxsd:source"] == (
                b'{"scope": "record", "path": [], "attribute": "id", "levels": 0}'
            )

    anyio.run(scenario)


@pytest.mark.parametrize("format", ["preview", "parquet", "dataset"])
@pytest.mark.parametrize("state", ["stale", "foreign"])
def test_worker_rejects_stale_and_foreign_evidence(tmp_path, monkeypatch, state, format):
    from tests.mcp.test_cache import request as compile_request

    from pyxsd import __version__
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.services.records import execute_export, execute_records
    from pyxsd.mcp.worker_protocol import ExportOperation
    from pyxsd.mcp.workers import capture_to_wire

    req = compile_request(tmp_path, files={"root.xsd": AMBIGUOUS, "input.xml": "<doc>12</doc>"})
    req = req.__class__(
        capture=req.capture,
        schema_ref=SchemaRef(source=FileRef(path="root.xsd"), format="relax-ng"),
        hints=(),
        processor_version=__version__,
    )
    cache = SchemaCache(Limits())
    parse = RelaxNGSchema.parse
    other = RelaxNGSchema.compile(io.StringIO(AMBIGUOUS))

    def changed(schema, *args, **kwargs):
        document = parse(other if state == "foreign" else schema, io.StringIO("<doc>12</doc>"))
        if state == "stale":
            document.root.set_text("13")
        return document

    monkeypatch.setattr(RelaxNGSchema, "parse", changed)
    common = {
        "request_id": "evidence",
        "capture": capture_to_wire(req.capture),
        "schema_ref": req.schema_ref,
        "element": "doc",
        "columns": {"value": FieldSourceSpec(datatype=INT)},
    }
    try:
        with pytest.raises(ToolError) as error:
            if format == "preview":
                execute_records(
                    RecordsOperation(**common, document=FileRef(path="input.xml")),
                    cache,
                    __version__,
                )
            else:
                execute_export(
                    ExportOperation(
                        **common,
                        documents=(FileRef(path="input.xml"),),
                        output_directory=str(tmp_path),
                        format=format,
                    ),
                    cache,
                    __version__,
                )
        assert error.value.fault.code == "unsupported_projection"
        assert (
            "stale" if state == "stale" else "another compiled schema"
        ) in error.value.fault.message
        assert not (tmp_path / "export.parquet").exists()
        assert not (tmp_path / "export-dataset").exists()
    finally:
        cache.close()


def test_mcp_selector_arrays_validate_and_exclusive_selectors_raise_value_error(tmp_path):
    from tests.mcp.test_server import connected, create_workspace, structured

    async def scenario():
        async with connected(tmp_path) as (client, _):
            workspace = await create_workspace(client)
            capabilities = structured(await client.call_tool("pyxsd_capabilities", {}))["data"]
            assert (
                capabilities["schema_languages"]["relax-ng"]["mcp"]["projection"]["records"] is True
            )
            result = await client.call_tool(
                "pyxsd_preview_records",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.rng"}, "format": "relax-ng"},
                    "document": {"path": "input.xml"},
                    "element": "doc",
                    "columns": {"value": {"datatype": list(INT), "candidate_id": "candidate"}},
                },
            )
            assert result.is_error
            assert "mutually exclusive" in result.content[0].text
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {
                            "path": "schema.rng",
                            "text": element(
                                '<element name="value"><choice><data type="int"/>'
                                '<data type="string"/></choice></element>'
                            ),
                        },
                        {"path": "input.xml", "text": "<doc><value>12</value></doc>"},
                    ],
                },
            )
            result = await client.call_tool(
                "pyxsd_preview_records",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.rng"}, "format": "relax-ng"},
                    "document": {"path": "input.xml"},
                    "element": "doc",
                    "columns": {"value": {"path": ["value"], "datatype": list(INT)}},
                },
            )
            assert not result.is_error
            assert structured(result)["data"]["rows"] == [{"value": 12}]

    anyio.run(scenario)


def test_source_root_records_and_exports_use_current_captured_documents(tmp_path):
    from tests.mcp.test_source_root import harness as source_harness
    from tests.mcp.test_source_root import relaxng_ref

    async def scenario():
        async with source_harness(tmp_path) as (exploration, inputs, pool, workspace):
            service = RecordService(inputs, pool, outputs=exploration.outputs)
            request = PreviewRecordsRequest(
                workspace_id=workspace,
                schema_ref=relaxng_ref(),
                document=FileRef(path="data/relaxng.xml"),
                element="note",
                columns={"value": FieldSourceSpec()},
            )
            result = await service.preview_records(request)
            assert result.data["rows"] == [{"value": "hello"}]
            assert result.data["route_scope"] == "start"
            assert result.data["whole_xml_validity"] is False
            exported = await service.export_records(
                ExportRecordsRequest(
                    **request.model_dump(exclude={"document", "exact_count"}),
                    documents=(request.document,),
                )
            )
            assert exported["row_count"] == 1
            assert exported["source_documents"] == ["data/relaxng.xml"]
            (tmp_path / "tree" / "data" / "relaxng.xml").write_text("<wrong/>", encoding="utf-8")
            with pytest.raises(ToolError) as invalid:
                await service.preview_records(request)
            assert invalid.value.fault.code == "invalid_document"

    anyio.run(scenario)


@pytest.mark.parametrize("action", ["preview", "parquet", "dataset"])
def test_malformed_record_input_is_an_xml_fault_not_a_schema_fault(tmp_path, action):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": AMBIGUOUS, "input.xml": "<doc>"})
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            common = request(workspace, columns={"value": FieldSourceSpec(datatype=INT)})
            with pytest.raises(ToolError) as error:
                if action == "preview":
                    await service.preview_records(
                        PreviewRecordsRequest(**common, document=FileRef(path="input.xml"))
                    )
                else:
                    await service.export_records(
                        ExportRecordsRequest(
                            **common, documents=(FileRef(path="input.xml"),), format=action
                        )
                    )
            assert error.value.fault.code == "invalid_xml"

    anyio.run(scenario)


def test_record_parse_budget_fault_retains_operational_details(tmp_path):
    async def scenario():
        async with harness(tmp_path, Limits(enforced=True, relaxng_scalar_witnesses=1)) as (
            _,
            inputs,
            pool,
            workspace,
        ):
            await upload(inputs, workspace, {"schema.rng": AMBIGUOUS, "input.xml": "<doc>12</doc>"})
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            with pytest.raises(ToolError) as error:
                await service.preview_records(
                    PreviewRecordsRequest(
                        **request(workspace, columns={"value": FieldSourceSpec(datatype=INT)}),
                        document=FileRef(path="input.xml"),
                    )
                )
            assert error.value.fault.code == "limit_exceeded"
            assert error.value.fault.details["budget"] == "max_scalar_witnesses"
            assert error.value.fault.details["configured"] == 1
            assert error.value.fault.details["observed"] > 1

    anyio.run(scenario)
