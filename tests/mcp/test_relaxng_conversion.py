"""Shared MCP conversion for RELAX NG: formats, routes, faults, and profiles."""

import io
import math
import warnings

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, upload
from tests.mcp.test_relaxng_validation import capture_of
from tests.mcp.test_server import connected, structured

from pydantic import ValidationError as PydanticValidationError
from pyxsd import __version__
from pyxsd.mcp import worker_main
from pyxsd.mcp.cache import SchemaCache
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import (
    DocumentJsonSchemaRequest,
    ExportDataRequest,
    FileRef,
    ImportDataRequest,
    SchemaRef,
)
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.services.conversion import ConversionService, _export_data
from pyxsd.mcp.worker_protocol import (
    CaptureMessage,
    DocumentJsonSchemaOperation,
    ExportDataOperation,
    ImportDataOperation,
)
from pyxsd.mcp.workers import capture_to_wire
from pyxsd.relaxng import RelaxNGSchema

NS = "http://relaxng.org/ns/structure/1.0"
XSDT = "http://www.w3.org/2001/XMLSchema-datatypes"
FORMATS = ("pyxsd", "pyxsd-json", "parker", "badgerfish", "jsonml")
NONNATIVE = ("pyxsd-json", "parker", "badgerfish", "jsonml")

TAGGED = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <optional><attribute name="id"><data type="integer" datatypeLibrary="{XSDT}"/></attribute></optional>
      <choice>
        <data type="integer" datatypeLibrary="{XSDT}"/>
        <data type="string" datatypeLibrary="{XSDT}"/>
      </choice>
    </element>
  </start>
</grammar>"""

TAGGED_DOCUMENT = '<doc id="3">12</doc>'

GENERIC = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <zeroOrMore>
        <element><anyName/><data type="string" datatypeLibrary="{XSDT}"/></element>
      </zeroOrMore>
    </element>
  </start>
</grammar>"""

GENERIC_DOCUMENT = "<doc><alpha>v</alpha><beta>w</beta></doc>"

ROOTS = f"""<grammar xmlns="{NS}">
  <start>
    <choice>
      <element name="a"><data type="string" datatypeLibrary="{XSDT}"/></element>
      <element name="b"><data type="string" datatypeLibrary="{XSDT}"/></element>
    </choice>
  </start>
</grammar>"""

INTEGER = f"""<grammar xmlns="{NS}">
  <start><element name="doc"><data type="integer" datatypeLibrary="{XSDT}"/></element></start>
</grammar>"""

TEXT = f"""<grammar xmlns="{NS}">
  <start><element name="doc"><text/></element></start>
</grammar>"""

MALFORMED_SCHEMA = f'<grammar xmlns="{NS}"><start><element name="doc"><text/></element></start>'
INVALID_SCHEMA = f'<grammar xmlns="{NS}"><define name="unused"><text/></define></grammar>'


def relaxng_ref(path="schema.rng") -> SchemaRef:
    return SchemaRef(source=FileRef(path=path), format="relax-ng")


def public_rng(schema_text: str = TAGGED, document_text: str = TAGGED_DOCUMENT):
    schema = RelaxNGSchema.compile(io.StringIO(schema_text))
    assert not schema.report.has_errors
    document = schema.parse(io.StringIO(document_text), typed=True)
    assert not document.report.has_errors
    return schema, document


def without_capture_base(value):
    """Normalize the virtual capture base URI for public-result comparison."""
    if isinstance(value, dict):
        return {
            key: (
                None
                if key == "base_uri" and isinstance(item, str) and item.startswith("/workspace/")
                else without_capture_base(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [without_capture_base(item) for item in value]
    return value


def export_request(workspace: str, **options) -> ExportDataRequest:
    options.setdefault("document", FileRef(path="document.xml"))
    return ExportDataRequest(workspace_id=workspace, schema_ref=relaxng_ref(), **options)


def import_request(workspace: str, data, **options) -> ImportDataRequest:
    return ImportDataRequest(workspace_id=workspace, schema_ref=relaxng_ref(), data=data, **options)


@pytest.mark.parametrize("format", FORMATS)
def test_export_matches_public_relaxng_results_for_every_format(tmp_path, format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                await upload(
                    inputs,
                    workspace,
                    {"schema.rng": TAGGED, "document.xml": TAGGED_DOCUMENT},
                )
                envelope = await ConversionService(inputs, pool).export_data(
                    export_request(workspace, format=format)
                )
            _, document = public_rng()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                expected = document.to_data(format=format)
            assert without_capture_base(envelope.data) == {
                "format": format,
                "fidelity": "best-effort",
                "data": expected,
            }
            if format in ("pyxsd", "pyxsd-json"):
                assert "#value" in str(envelope.data["data"])
            assert envelope.provenance["source"]["path"] == "document.xml"

    anyio.run(scenario)


def test_export_native_options_match_public_relaxng_results(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": TEXT, "document.xml": "<doc>x</doc>"})
            _, document = public_rng(TEXT, "<doc>x</doc>")
            service = ConversionService(inputs, pool)
            for typed, always_list in ((True, True), (False, False), (False, True)):
                envelope = await service.export_data(
                    export_request(workspace, typed=typed, always_list=always_list)
                )
                assert without_capture_base(envelope.data["data"]) == document.to_data(
                    typed=typed, always_list=always_list
                )

    anyio.run(scenario)


@pytest.mark.parametrize("format", NONNATIVE)
def test_relaxng_export_rejects_native_options_on_nonnative_formats(tmp_path, format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": TAGGED, "document.xml": TAGGED_DOCUMENT},
            )
            service = ConversionService(inputs, pool)
            for options, option in (
                ({"typed": False}, "typed"),
                ({"always_list": True}, "always_list"),
            ):
                with pytest.raises(ToolError) as raised:
                    await service.export_data(export_request(workspace, format=format, **options))
                assert raised.value.fault.code == "invalid_argument"
                assert raised.value.fault.message == (
                    f"{option} is a native-only data-export option"
                )

    anyio.run(scenario)


def test_relaxng_invalid_and_stale_exports_are_typed_faults(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs, workspace, {"schema.rng": INTEGER, "document.xml": "<doc>oops</doc>"}
            )
            service = ConversionService(inputs, pool)
            with pytest.raises(ToolError) as invalid:
                await service.export_data(export_request(workspace))
            assert invalid.value.fault.code == "invalid_data"
            assert invalid.value.fault.details["format"] == "pyxsd"
            lexical = await service.export_data(export_request(workspace, typed=False))
            assert lexical.data["data"] == {"$": "oops"}

    anyio.run(scenario)

    schema, document = public_rng(INTEGER, "<doc>12</doc>")
    document.root.set_text("13")
    operation = ExportDataOperation(
        request_id="stale",
        capture=CaptureMessage(root=str(tmp_path), files=()),
        schema_ref=relaxng_ref(),
        document=FileRef(path="document.xml"),
    )
    cache = SchemaCache(Limits(enforced=True))
    try:
        with pytest.raises(ToolError) as stale:
            _export_data(document, operation, cache)
    finally:
        cache.close()
    assert stale.value.fault.code == "invalid_data"
    assert "stale" in stale.value.fault.message

    validation_only = schema.parse(io.StringIO("<doc>12</doc>"), typed=False)
    cache = SchemaCache(Limits(enforced=True))
    try:
        with pytest.raises(ToolError) as untyped:
            _export_data(validation_only, operation, cache)
    finally:
        cache.close()
    assert untyped.value.fault.code == "invalid_data"
    assert "requires a typed document" in untyped.value.fault.message


@pytest.mark.parametrize(
    "schema_text,code", [(MALFORMED_SCHEMA, "invalid_schema"), (INVALID_SCHEMA, "invalid_schema")]
)
def test_relaxng_schema_faults_block_export_and_json_schema(tmp_path, schema_text, code):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": schema_text, "document.xml": TAGGED_DOCUMENT},
            )
            service = ConversionService(inputs, pool)
            with pytest.raises(ToolError) as exported:
                await service.export_data(export_request(workspace))
            assert exported.value.fault.code == code
            with pytest.raises(ToolError) as imported:
                await service.import_data(
                    import_request(workspace, {"doc": "x"}, format="pyxsd-json")
                )
            assert imported.value.fault.code == code
            with pytest.raises(ToolError) as generated:
                await service.document_json_schema(
                    DocumentJsonSchemaRequest(workspace_id=workspace, schema_ref=relaxng_ref())
                )
            assert generated.value.fault.code == code

    anyio.run(scenario)


@pytest.mark.parametrize("format", FORMATS)
def test_import_round_trips_relaxng_tagged_alternatives(tmp_path, format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": TAGGED})
            schema, document = public_rng()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                exported = document.to_data(format=format)
                envelope = await ConversionService(inputs, pool).import_data(
                    import_request(workspace, exported, format=format)
                )
            assert envelope.data["valid"] is True
            assert envelope.data["binding_available"] is True
            assert envelope.data["report"] is None
            assert envelope.data["format"] == format
            rebuilt = schema.from_data(exported, format=format)
            assert rebuilt.report.error_count == 0

    anyio.run(scenario)


def test_import_rejects_forged_relaxng_tag_claims(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": TAGGED})
            _, document = public_rng()
            data = document.to_data(format="pyxsd-json")
            data["doc"]["#content"]["#value"]["version"] = 2
            with pytest.raises(ToolError) as raised:
                await ConversionService(inputs, pool).import_data(
                    import_request(workspace, data, format="pyxsd-json")
                )
            fault = raised.value.fault
            assert fault.code == "invalid_data"
            assert "scalar-tag-version at '/doc/#content'" in fault.message
            assert fault.details["format"] == "pyxsd-json"

    anyio.run(scenario)


def test_import_uses_relaxng_generic_routes(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": GENERIC})
            schema, document = public_rng(GENERIC, GENERIC_DOCUMENT)
            data = document.to_data(format="pyxsd-json")
            envelope = await ConversionService(inputs, pool).import_data(
                import_request(workspace, data, format="pyxsd-json")
            )
            assert envelope.data["valid"] is True
            assert envelope.data["report"] is None
            rebuilt = schema.from_data(data, format="pyxsd-json")
            assert [child._name_ for child in rebuilt.root.children] == ["alpha", "beta"]

    anyio.run(scenario)


def test_relaxng_root_selection_for_root_absorbed_formats(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": ROOTS})
            service = ConversionService(inputs, pool)
            with pytest.raises(ToolError) as ambiguous:
                await service.import_data(import_request(workspace, "x", format="parker"))
            assert ambiguous.value.fault.code == "invalid_data"
            assert "root-ambiguous" in ambiguous.value.fault.message
            selected = await service.import_data(
                import_request(workspace, "x", format="parker", element="b")
            )
            assert selected.data["valid"] is True

    anyio.run(scenario)


def test_relaxng_import_grammar_errors_are_ordinary_reports(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": INTEGER})
            envelope = await ConversionService(inputs, pool).import_data(
                import_request(workspace, "oops", format="parker")
            )
            assert envelope.data["valid"] is False
            assert envelope.data["binding_available"] is True
            report = envelope.data["report"]
            assert report["counts"]["error"] >= 1
            assert report["diagnostics_complete"] is True
            assert report["issues"][0]["phase"] == "instance"

    anyio.run(scenario)


def test_relaxng_import_shape_tag_and_nonfinite_faults(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": TAGGED})
            service = ConversionService(inputs, pool)
            with pytest.raises(ToolError) as shape:
                await service.import_data(
                    import_request(workspace, {"doc": {"#value": 1}}, format="pyxsd-json")
                )
            assert shape.value.fault.code == "invalid_data"
            assert "container-invalid at '/doc/#value'" in shape.value.fault.message

            capture = await inputs.manifest(workspace)
            operation = ImportDataOperation.model_construct(
                request_id="nonfinite",
                capture=capture_to_wire(capture),
                schema_ref=relaxng_ref(),
                data={"doc": math.inf},
                format="pyxsd-json",
                element=None,
                namespaces={},
                fidelity="strict",
                issue_offset=0,
                issue_limit=None,
            )
            cache = SchemaCache(Limits(enforced=True))
            try:
                with pytest.raises(ToolError) as nonfinite:
                    worker_main._execute(operation, cache, __version__)
            finally:
                cache.close()
            assert nonfinite.value.fault.code == "invalid_data"
            assert "json-invalid at '/doc'" in nonfinite.value.fault.message

    anyio.run(scenario)


@pytest.mark.parametrize("format", FORMATS)
def test_document_json_schema_has_relaxng_profile_metadata(tmp_path, format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": TAGGED})
            envelope = await ConversionService(inputs, pool).document_json_schema(
                DocumentJsonSchemaRequest(
                    workspace_id=workspace, schema_ref=relaxng_ref(), format=format
                )
            )
            schema, _ = public_rng()
            assert envelope.data == {
                "format": format,
                "fidelity": "strict",
                "json_schema": schema.json_schema(format=format),
            }
            generated = envelope.data["json_schema"]
            assert generated["x-pyxsd-format"] == format
            assert generated["x-pyxsd-profile"] == "relaxng"
            assert generated["x-pyxsd-profile-version"] == 1
            assert generated["x-pyxsd-validation"]["level"] == "structural"
            assert generated["x-pyxsd-validation"]["unvalidated"]

    anyio.run(scenario)


def test_document_json_schema_selects_relaxng_roots_and_rejects_options(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": ROOTS})
            service = ConversionService(inputs, pool)
            schema, _ = public_rng(ROOTS, "<b>x</b>")
            selected = await service.document_json_schema(
                DocumentJsonSchemaRequest(
                    workspace_id=workspace, schema_ref=relaxng_ref(), element="b"
                )
            )
            assert selected.data["json_schema"] == schema.json_schema(element="b")
            with pytest.raises(ToolError) as absent:
                await service.document_json_schema(
                    DocumentJsonSchemaRequest(
                        workspace_id=workspace,
                        schema_ref=relaxng_ref(),
                        element="absent",
                    )
                )
            assert absent.value.fault.code == "invalid_data"
            assert "root-unresolved" in absent.value.fault.message
            for format, options, option in (
                *((format, {"typed": False}, "typed") for format in NONNATIVE),
                *((format, {"always_list": True}, "always_list") for format in NONNATIVE),
            ):
                with pytest.raises(ToolError) as raised:
                    await service.document_json_schema(
                        DocumentJsonSchemaRequest(
                            workspace_id=workspace,
                            schema_ref=relaxng_ref(),
                            format=format,
                            **options,
                        )
                    )
                assert raised.value.fault.message == (
                    f"{option} is a native-only JSON Schema option"
                )

    anyio.run(scenario)


def test_relaxng_worker_dispatch_export_import_and_json_schema(tmp_path):
    capture = capture_of(
        tmp_path / "worker",
        {"schema.rng": TAGGED, "document.xml": TAGGED_DOCUMENT},
    )
    cache = SchemaCache(Limits(enforced=True))
    try:
        exported = worker_main._execute(
            ExportDataOperation(
                request_id="export",
                capture=capture_to_wire(capture),
                schema_ref=relaxng_ref(),
                document=FileRef(path="document.xml"),
                format="pyxsd-json",
            ),
            cache,
            __version__,
        )
        assert exported["data"]["doc"]["#content"]["#value"]["lexical"] == "12"
        imported = worker_main._execute(
            ImportDataOperation(
                request_id="import",
                capture=capture_to_wire(capture),
                schema_ref=relaxng_ref(),
                data=exported["data"],
                format="pyxsd-json",
            ),
            cache,
            __version__,
        )
        assert imported["root_present"] is True
        assert imported["report"]["counts"]["error"] == 0
        assert imported["page"] == {"returned": 0, "truncated": False, "total": 0}
        generated = worker_main._execute(
            DocumentJsonSchemaOperation(
                request_id="schema",
                capture=capture_to_wire(capture),
                schema_ref=relaxng_ref(),
            ),
            cache,
            __version__,
        )
        assert generated["json_schema"]["x-pyxsd-profile"] == "relaxng"
    finally:
        cache.close()


def test_relaxng_export_spills_to_an_artifact_and_claims_capabilities(tmp_path):
    limits = Limits(enforced=True, result_bytes=4096, output_bytes=8 * 2**20)
    body = "x" * 20000

    async def scenario():
        async with connected(tmp_path, limits=limits) as (client, services):
            from pyxsd.mcp.contracts import InlineFile
            from pyxsd.mcp.tools import capability_data

            workspace = await services.inputs.create()
            await services.inputs.put_files(
                workspace.id,
                [
                    InlineFile(path="schema.rng", text=TEXT),
                    InlineFile(path="document.xml", text=f"<doc>{body}</doc>"),
                ],
            )
            result = structured(
                await client.call_tool(
                    "pyxsd_export_data",
                    {
                        "workspace_id": workspace.id,
                        "schema_ref": {
                            "source": {"path": "schema.rng"},
                            "format": "relax-ng",
                        },
                        "document": {"path": "document.xml"},
                    },
                )
            )
            assert result["data"]["bytes"] > 4096
            artifact = result["artifacts"][0]
            retained = await services.outputs.read(
                artifact["id"], offset=0, length=artifact["size"]
            )
            assert body in retained.decode("utf-8")
            conversion = capability_data(services)["schema_languages"]["relax-ng"]["mcp"][
                "conversion"
            ]
            assert conversion["tools"] is True
            assert conversion["formats"] == list(FORMATS)
            assert conversion["document_json_schema"] is True

    anyio.run(scenario)


def test_relaxng_wire_operation_rejects_unsupported_native_options(tmp_path):
    for operation_type, message in (
        (ExportDataOperation, "typed is a native-only data-export option"),
        (
            DocumentJsonSchemaOperation,
            "typed is a native-only JSON Schema option",
        ),
    ):
        extra = (
            {"document": FileRef(path="document.xml")}
            if operation_type is ExportDataOperation
            else {}
        )
        with pytest.raises(PydanticValidationError) as raised:
            operation_type(
                request_id="conversion",
                capture=CaptureMessage(root=str(tmp_path), files=()),
                schema_ref=relaxng_ref(),
                format="parker",
                typed=False,
                **extra,
            )
        assert message in str(raised.value)
