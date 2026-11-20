"""Shared MCP data conversion: XSD parity, wire contracts, and budgets."""

import io
import json
import math
from itertools import product

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, upload
from tests.mcp.test_relaxng_validation import capture_of
from tests.mcp.test_server import connected, create_workspace, structured

from pydantic import ValidationError as PydanticValidationError
from pyxsd import Schema, __version__
from pyxsd.mcp import worker_main
from pyxsd.mcp.cache import SchemaCache
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import (
    DocumentJsonSchemaRequest,
    ExportDataRequest,
    FileRef,
    ImportDataRequest,
    Page,
    SchemaRef,
)
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.services.conversion import ConversionService, execute_import_data
from pyxsd.mcp.worker_protocol import (
    CaptureMessage,
    DocumentJsonSchemaOperation,
    ExportDataOperation,
    ImportDataOperation,
    decode_operation,
    encode_frame,
)
from pyxsd.mcp.workers import capture_to_wire

FORMATS = ("pyxsd", "pyxsd-json", "parker", "badgerfish", "jsonml")
NONNATIVE = ("pyxsd-json", "parker", "badgerfish", "jsonml")
FIDELITIES = ("strict", "best-effort")

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="label" type="xs:string"/>
        <xs:element name="count" type="xs:int"/>
      </xs:sequence>
      <xs:attribute name="id" type="xs:int"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

DOCUMENT = '<root id="7"><label>x</label><count>2</count></root>'
INVALID_DOCUMENT = '<root id="x"><label>x</label><count>bad</count></root>'

SEQUENCE_XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="a" type="xs:int"/>
    <xs:element name="b" type="xs:int"/>
    <xs:element name="c" type="xs:int"/>
    <xs:element name="d" type="xs:int"/>
    <xs:element name="e" type="xs:int"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

MALFORMED_XSD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="root" type="xs:string"/>'
)

INVALID_XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:missingType"/>
</xs:schema>"""


def xsd_ref(path="schema.xsd") -> SchemaRef:
    return SchemaRef(source=FileRef(path=path))


def public_pair(schema_text: str = XSD, document_text: str = DOCUMENT):
    from pyxsd.binding import ParseModes

    schema = Schema.compile(
        io.StringIO(schema_text), mode=ParseModes.LAX.replace(namespaces="strict")
    )
    return schema, schema.parse(io.StringIO(document_text))


def export_request(workspace: str, **options) -> ExportDataRequest:
    return ExportDataRequest(
        workspace_id=workspace,
        schema_ref=xsd_ref(),
        document=FileRef(path="document.xml"),
        **options,
    )


def import_request(workspace: str, data, **options) -> ImportDataRequest:
    return ImportDataRequest(
        workspace_id=workspace,
        schema_ref=xsd_ref(),
        data=data,
        **options,
    )


@pytest.mark.parametrize(
    ("format", "fidelity"),
    [
        (format, fidelity)
        for format in FORMATS
        for fidelity in FIDELITIES
        if (format, fidelity) != ("parker", "strict")
    ],
)
def test_export_matches_public_xsd_results_for_every_format(tmp_path, format, fidelity):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "document.xml": DOCUMENT})
            envelope = await ConversionService(inputs, pool).export_data(
                export_request(workspace, format=format, fidelity=fidelity)
            )
            _, document = public_pair()
            assert envelope.data == {
                "format": format,
                "fidelity": fidelity,
                "data": document.to_data(format=format, fidelity=fidelity),
            }
            assert envelope.artifacts == []
            assert envelope.page is None
            assert envelope.provenance["source"]["path"] == "document.xml"
            assert envelope.provenance["source"]["digest"]
            assert len(envelope.provenance["schema_fingerprint"]) == 64

    anyio.run(scenario)


def test_export_strict_loss_is_a_typed_conversion_fault(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "document.xml": DOCUMENT})
            with pytest.raises(ToolError) as raised:
                await ConversionService(inputs, pool).export_data(
                    export_request(workspace, format="parker", fidelity="strict")
                )
            fault = raised.value.fault
            assert fault.code == "invalid_data"
            assert fault.details["format"] == "parker"
            assert "attributes-lost at ''" in fault.message

    anyio.run(scenario)


def test_export_native_options_match_public_results(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "document.xml": DOCUMENT})
            _, document = public_pair()
            service = ConversionService(inputs, pool)
            for typed, always_list in product((True, False), (True, False)):
                envelope = await service.export_data(
                    export_request(workspace, typed=typed, always_list=always_list)
                )
                assert envelope.data["data"] == document.to_data(
                    typed=typed, always_list=always_list
                )
            plain = await service.export_data(export_request(workspace))
            lexical = await service.export_data(export_request(workspace, typed=False))
            assert plain.data["data"]["@id"] == 7
            assert lexical.data["data"]["@id"] == "7"

    anyio.run(scenario)


def test_export_preserves_an_invalid_documents_public_result(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "document.xml": INVALID_DOCUMENT})
            envelope = await ConversionService(inputs, pool).export_data(
                export_request(workspace, format="pyxsd")
            )
            _, document = public_pair(document_text=INVALID_DOCUMENT)
            assert document.report.error_count
            assert envelope.data["data"] == document.to_data(format="pyxsd")

    anyio.run(scenario)


def test_export_rejects_native_options_on_nonnative_formats(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "document.xml": DOCUMENT})
            service = ConversionService(inputs, pool)
            for format, options in (
                *((format, {"typed": False}) for format in NONNATIVE),
                *((format, {"always_list": True}) for format in NONNATIVE),
            ):
                with pytest.raises(ToolError) as raised:
                    await service.export_data(export_request(workspace, format=format, **options))
                option = next(iter(options))
                assert raised.value.fault.code == "invalid_argument"
                assert raised.value.fault.message == (
                    f"{option} is a native-only data-export option"
                )

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"typed": False}, "typed is a native-only data-export option"),
        ({"always_list": True}, "always_list is a native-only data-export option"),
    ],
)
@pytest.mark.parametrize("format", NONNATIVE)
def test_worker_export_operation_enforces_the_native_only_rule(tmp_path, format, options, message):
    with pytest.raises(PydanticValidationError) as raised:
        ExportDataOperation(
            request_id="export",
            capture=CaptureMessage(root=str(tmp_path), files=()),
            schema_ref=xsd_ref(),
            document=FileRef(path="document.xml"),
            format=format,
            **options,
        )
    assert message in str(raised.value)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"typed": False}, "typed is a native-only JSON Schema option"),
        ({"always_list": True}, "always_list is a native-only JSON Schema option"),
    ],
)
@pytest.mark.parametrize("format", NONNATIVE)
def test_worker_json_schema_operation_enforces_the_native_only_rule(
    tmp_path, format, options, message
):
    with pytest.raises(PydanticValidationError) as raised:
        DocumentJsonSchemaOperation(
            request_id="schema",
            capture=CaptureMessage(root=str(tmp_path), files=()),
            schema_ref=xsd_ref(),
            format=format,
            **options,
        )
    assert message in str(raised.value)


def test_wire_requests_reject_import_native_fields_and_unknown_operation_formats(tmp_path):
    with pytest.raises(PydanticValidationError):
        ImportDataRequest(
            workspace_id="w",
            schema_ref=xsd_ref(),
            data={"label": "x"},
            typed=False,
        )
    with pytest.raises(PydanticValidationError):
        ExportDataOperation(
            request_id="export",
            capture=CaptureMessage(root=str(tmp_path), files=()),
            schema_ref=xsd_ref(),
            document=FileRef(path="document.xml"),
            format="nope",
        )


def test_invalid_format_and_fidelity_match_public_messages(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "document.xml": DOCUMENT})
            service = ConversionService(inputs, pool)
            for options, message in (
                ({"format": "nope"}, "unknown data format: 'nope'"),
                ({"fidelity": "lossy"}, "unknown fidelity mode: 'lossy'"),
            ):
                calls = (
                    service.export_data(export_request(workspace, **options)),
                    service.import_data(import_request(workspace, {"label": "x"}, **options)),
                    service.document_json_schema(
                        DocumentJsonSchemaRequest(
                            workspace_id=workspace, schema_ref=xsd_ref(), **options
                        )
                    ),
                )
                for call in calls:
                    with pytest.raises(ToolError) as raised:
                        await call
                    assert raised.value.fault.code == "invalid_argument"
                    assert raised.value.fault.message == message

    anyio.run(scenario)


@pytest.mark.parametrize("format", FORMATS)
def test_import_round_trips_public_xsd_exports(tmp_path, format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "document.xml": DOCUMENT})
            schema, document = public_pair()
            exported = document.to_data(format=format)
            envelope = await ConversionService(inputs, pool).import_data(
                import_request(workspace, exported, format=format)
            )
            assert envelope.data["valid"] is True
            assert envelope.data["binding_available"] is True
            assert "root_present" not in envelope.data
            assert envelope.data["report"] is None
            assert envelope.data["format"] == format
            assert envelope.data["fidelity"] == "strict"
            assert envelope.page is None
            assert "document" not in envelope.data
            rebuilt = schema.from_data(exported, format=format)
            assert rebuilt.is_valid

    anyio.run(scenario)


def test_import_grammar_errors_are_an_ordinary_report(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            envelope = await ConversionService(inputs, pool).import_data(
                import_request(workspace, {"label": "x", "count": "bad"}, format="pyxsd")
            )
            assert envelope.data["valid"] is False
            assert envelope.data["binding_available"] is True
            report = envelope.data["report"]
            assert report["counts"]["error"] >= 1
            assert report["issues"]
            assert {issue["phase"] for issue in report["issues"]} == {"instance"}
            schema, _ = public_pair()
            public_report = schema.from_data({"label": "x", "count": "bad"}, format="pyxsd").report
            assert report["counts"] == public_report.to_dict()["counts"]

    anyio.run(scenario)


def test_import_accepts_an_explicit_xsd_root(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            service = ConversionService(inputs, pool)
            for element in ("root", "{}root"):
                envelope = await service.import_data(
                    import_request(
                        workspace,
                        {"@id": 9, "label": "x", "count": 2},
                        format="pyxsd",
                        element=element,
                    )
                )
                assert envelope.data["valid"] is True

    anyio.run(scenario)


@pytest.mark.parametrize("format", NONNATIVE)
def test_import_shape_and_tag_errors_are_typed_conversion_faults(tmp_path, format):
    bad_data = {
        "pyxsd-json": {"root": 5},
        "parker": {"label": {"unstoppable": "x"}},
        "badgerfish": {"root": 5},
        "jsonml": {"root": {}},
    }[format]
    expected_pointer = {
        "pyxsd-json": "'/root'",
        "parker": "'/label/unstoppable'",
        "badgerfish": "'/root'",
        "jsonml": "''",
    }[format]

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            with pytest.raises(ToolError) as raised:
                await ConversionService(inputs, pool).import_data(
                    import_request(workspace, bad_data, format=format)
                )
            fault = raised.value.fault
            assert fault.code == "invalid_data"
            assert fault.details["format"] == format
            assert expected_pointer in fault.message
            assert "at " in fault.message

    anyio.run(scenario)


def test_import_nonfinite_numbers_preserve_public_pointers(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            with pytest.raises(PydanticValidationError):
                import_request(workspace, {"label": "x", "count": math.inf})
            capture = await inputs.manifest(workspace)
            operation = ImportDataOperation.model_construct(
                request_id="nonfinite",
                capture=capture_to_wire(capture),
                schema_ref=xsd_ref(),
                data={"root": {"label": "x", "count": math.inf}},
                format="pyxsd-json",
                element=None,
                namespaces={},
                fidelity="strict",
                issue_offset=0,
                issue_limit=None,
            )
            cache = SchemaCache(Limits(enforced=True))
            try:
                with pytest.raises(ToolError) as raised:
                    execute_import_data(operation, cache, __version__)
            finally:
                cache.close()
            assert raised.value.fault.code == "invalid_data"
            assert "json-invalid at '/root/count'" in raised.value.fault.message

    anyio.run(scenario)


@pytest.mark.parametrize("format", FORMATS)
def test_document_json_schema_matches_public_xsd_results(tmp_path, format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            envelope = await ConversionService(inputs, pool).document_json_schema(
                DocumentJsonSchemaRequest(
                    workspace_id=workspace, schema_ref=xsd_ref(), format=format
                )
            )
            schema, _ = public_pair()
            assert envelope.data == {
                "format": format,
                "fidelity": "strict",
                "json_schema": schema.json_schema(format=format),
            }
            generated = envelope.data["json_schema"]
            assert generated["x-pyxsd-format"] == format
            assert generated["x-pyxsd-validation"]["level"] == "structural"
            assert "x-pyxsd-profile" not in generated

    anyio.run(scenario)


def test_document_json_schema_selects_a_root_and_native_options(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            schema, _ = public_pair()
            service = ConversionService(inputs, pool)
            selected = await service.document_json_schema(
                DocumentJsonSchemaRequest(
                    workspace_id=workspace,
                    schema_ref=xsd_ref(),
                    format="pyxsd",
                    element="root",
                    fidelity="best-effort",
                    typed=False,
                    always_list=True,
                )
            )
            assert selected.data["json_schema"] == schema.json_schema(
                format="pyxsd",
                element="root",
                fidelity="best-effort",
                typed=False,
                always_list=True,
            )

    anyio.run(scenario)


def test_document_json_schema_rejects_native_options_on_nonnative_formats(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            service = ConversionService(inputs, pool)
            for format, options in (
                *((format, {"typed": False}) for format in NONNATIVE),
                *((format, {"always_list": True}) for format in NONNATIVE),
            ):
                with pytest.raises(ToolError) as raised:
                    await service.document_json_schema(
                        DocumentJsonSchemaRequest(
                            workspace_id=workspace,
                            schema_ref=xsd_ref(),
                            format=format,
                            **options,
                        )
                    )
                option = next(iter(options))
                assert raised.value.fault.code == "invalid_argument"
                assert raised.value.fault.message == (
                    f"{option} is a native-only JSON Schema option"
                )

    anyio.run(scenario)


def test_export_distinguishes_missing_malformed_and_invalid_schema(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.xsd": XSD,
                    "document.xml": DOCUMENT,
                    "malformed.xml": "<root><label>x</label>",
                    "bad.xsd": INVALID_XSD,
                },
            )
            service = ConversionService(inputs, pool)
            with pytest.raises(ToolError) as missing:
                await service.export_data(
                    ExportDataRequest(
                        workspace_id=workspace,
                        schema_ref=xsd_ref(),
                        document=FileRef(path="absent.xml"),
                    )
                )
            assert missing.value.fault.code == "not_found"
            with pytest.raises(ToolError) as malformed:
                await service.export_data(
                    ExportDataRequest(
                        workspace_id=workspace,
                        schema_ref=xsd_ref(),
                        document=FileRef(path="malformed.xml"),
                    )
                )
            assert malformed.value.fault.code == "invalid_xml"
            assert "line" in malformed.value.fault.details
            with pytest.raises(ToolError) as invalid:
                await service.export_data(
                    ExportDataRequest(
                        workspace_id=workspace,
                        schema_ref=xsd_ref("bad.xsd"),
                        document=FileRef(path="document.xml"),
                    )
                )
            assert invalid.value.fault.code == "invalid_schema"

    anyio.run(scenario)


def test_import_pages_issues_under_the_operator_budget(tmp_path):
    async def scenario():
        limits = Limits(enforced=True, page_items=2)
        async with harness(tmp_path, limits) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": SEQUENCE_XSD})
            service = ConversionService(inputs, pool)
            data = dict.fromkeys("abcde", "bad")
            first = await service.import_data(
                import_request(workspace, data, format="pyxsd", page=Page(limit=2))
            )
            assert first.data["valid"] is False
            assert first.page is not None
            assert first.page.returned == 2
            assert first.page.total == 5
            assert first.page.truncated is True
            assert len(first.data["report"]["issues"]) == 2
            collected = list(first.data["report"]["issues"])
            cursor = first.page.next_cursor
            assert cursor
            while cursor is not None:
                page = await service.import_data(
                    import_request(
                        workspace,
                        data,
                        format="pyxsd",
                        page=Page(cursor=cursor, limit=2),
                    )
                )
                assert page.page is not None
                collected.extend(page.data["report"]["issues"])
                cursor = page.page.next_cursor
            assert len(collected) == 5
            with pytest.raises(ToolError) as oversized:
                await service.import_data(
                    import_request(workspace, data, format="pyxsd", page=Page(limit=3))
                )
            assert oversized.value.fault.code == "invalid_argument"

    anyio.run(scenario)


def test_conversion_tools_are_read_only_and_budgeted(tmp_path):
    limits = Limits(enforced=True, result_bytes=32 * 1024, page_items=50)

    async def scenario():
        async with connected(tmp_path, limits=limits) as (client, services):
            from pyxsd.mcp.tools import capability_data

            names = {tool.name for tool in (await client.list_tools()).tools}
            for name in (
                "pyxsd_export_data",
                "pyxsd_import_data",
                "pyxsd_document_json_schema",
            ):
                assert name in names
            workspace = await create_workspace(client)
            uploaded = await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {"path": "schema.xsd", "text": XSD},
                        {"path": "document.xml", "text": DOCUMENT},
                    ],
                },
            )
            assert uploaded.is_error is False
            exported = structured(
                await client.call_tool(
                    "pyxsd_export_data",
                    {
                        "workspace_id": workspace,
                        "schema_ref": {"source": {"path": "schema.xsd"}},
                        "document": {"path": "document.xml"},
                        "format": "pyxsd-json",
                    },
                )
            )
            assert exported["data"]["data"] == {"root": [{"@id": 7}, {"label": "x"}, {"count": 2}]}
            assert exported["artifacts"] == []
            imported = structured(
                await client.call_tool(
                    "pyxsd_import_data",
                    {
                        "workspace_id": workspace,
                        "schema_ref": {"source": {"path": "schema.xsd"}},
                        "data": {"root": [{"@id": 7}, {"label": "x"}, {"count": 2}]},
                    },
                )
            )
            assert imported["data"]["valid"] is True
            generated = structured(
                await client.call_tool(
                    "pyxsd_document_json_schema",
                    {
                        "workspace_id": workspace,
                        "schema_ref": {"source": {"path": "schema.xsd"}},
                    },
                )
            )
            assert generated["data"]["json_schema"]["x-pyxsd-format"] == "pyxsd-json"
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in (
                "pyxsd_export_data",
                "pyxsd_import_data",
                "pyxsd_document_json_schema",
            ):
                assert tools[name].annotations.read_only_hint is True
            capabilities = capability_data(services)
            assert capabilities["schema_languages"]["xsd"]["mcp"]["conversion"] == {
                "tools": True,
                "export_data": True,
                "import_data": True,
                "document_json_schema": True,
                "formats": list(FORMATS),
                "fidelities": list(FIDELITIES),
            }
            listed = await services.outputs.list()
            assert listed == ([], None)

    anyio.run(scenario)


def test_oversized_export_spills_to_a_readable_artifact(tmp_path):
    rows = "".join(f"<item>{index}</item>" for index in range(2000))
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="root"><xs:complexType><xs:sequence>'
        '<xs:element name="item" type="xs:int" maxOccurs="unbounded"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    limits = Limits(enforced=True, result_bytes=4096, output_bytes=8 * 2**20)

    async def scenario():
        async with connected(tmp_path, limits=limits) as (client, services):
            workspace = await create_workspace(client)
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {"path": "schema.xsd", "text": schema},
                        {"path": "document.xml", "text": f"<root>{rows}</root>"},
                    ],
                },
            )
            result = structured(
                await client.call_tool(
                    "pyxsd_export_data",
                    {
                        "workspace_id": workspace,
                        "schema_ref": {"source": {"path": "schema.xsd"}},
                        "document": {"path": "document.xml"},
                        "format": "pyxsd-json",
                    },
                )
            )
            assert result["data"]["summary"]
            assert result["data"]["bytes"] > 4096
            artifact = result["artifacts"][0]
            assert artifact["kind"] == "result"
            body = await services.outputs.read(artifact["id"], offset=0, length=artifact["size"])
            stored = json.loads(body)
            assert stored["data"]["data"]["root"] == list(range(2000))

    anyio.run(scenario)


def test_budget_too_small_for_an_artifact_reference_faults(tmp_path):
    limits = Limits(enforced=True, result_bytes=1)

    async def scenario():
        async with connected(tmp_path, limits=limits) as (client, services):
            workspace = await services.inputs.create()
            from pyxsd.mcp.contracts import InlineFile

            await services.inputs.put_files(
                workspace.id,
                [
                    InlineFile(path="schema.xsd", text=XSD),
                    InlineFile(path="document.xml", text=DOCUMENT),
                ],
            )
            result = await client.call_tool(
                "pyxsd_export_data",
                {
                    "workspace_id": workspace.id,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "document": {"path": "document.xml"},
                },
            )
            assert result.is_error is True
            assert structured(result)["data"]["fault"]["code"] == "response_limit"
            assert await services.outputs.list() == ([], None)

    anyio.run(scenario)


def test_conversion_tools_are_absent_without_the_exploration_group(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups={"workspaces"}) as (client, _services):
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert (
                not {
                    "pyxsd_export_data",
                    "pyxsd_import_data",
                    "pyxsd_document_json_schema",
                }
                & names
            )

    anyio.run(scenario)


def test_export_worker_reports_missing_document_from_capture(tmp_path):
    capture = capture_of(tmp_path / "worker", {"schema.xsd": XSD})
    cache = SchemaCache(Limits(enforced=True))
    try:
        with pytest.raises(ToolError) as raised:
            worker_main._execute(
                ExportDataOperation(
                    request_id="export",
                    capture=capture_to_wire(capture),
                    schema_ref=xsd_ref(),
                    document=FileRef(path="absent.xml"),
                ),
                cache,
                __version__,
            )
    finally:
        cache.close()
    assert raised.value.fault.code == "not_found"
    assert raised.value.fault.details == {"role": "document", "path": "absent.xml"}


def test_conversion_operations_round_trip_through_bounded_frames(tmp_path):
    capture = CaptureMessage(root=str(tmp_path), files=())
    schema_ref = xsd_ref()
    operations = (
        ExportDataOperation(
            request_id="export",
            capture=capture,
            schema_ref=schema_ref,
            document=FileRef(path="document.xml"),
        ),
        ImportDataOperation(
            request_id="import",
            capture=capture,
            schema_ref=schema_ref,
            data={"label": "x"},
        ),
        DocumentJsonSchemaOperation(
            request_id="schema",
            capture=capture,
            schema_ref=schema_ref,
        ),
    )
    for operation in operations:
        frame = encode_frame(operation, max_bytes=16 * 1024)
        assert decode_operation(frame, max_bytes=16 * 1024, role="clean") == operation
