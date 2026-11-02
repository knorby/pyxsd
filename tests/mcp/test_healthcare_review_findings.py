"""Protocol regressions from the healthcare diagnostic trial."""

import asyncio

import pytest
from tests.mcp.test_exploration import XSD, harness, schema_ref, upload

from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, InlineFile, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.safe_io import admit_inline
from pyxsd.mcp.services.exploration import (
    ExplainIssueRequest,
    InspectSchemaRequest,
    ValidateDocumentRequest,
)


@pytest.mark.parametrize("encoding", ["ASCII", "US-ASCII", "ascii"])
def test_ascii_declaration_upload_preserves_original_bytes(encoding):
    text = f'<?xml version="1.0" encoding="{encoding}"?><root/>'
    result = admit_inline([InlineFile(path="schema.xsd", text=text)], Limits())
    assert result[0].content == text.encode("ascii")


@pytest.mark.parametrize("text", ["<root>caf\u00e9</root>", "\ufeff<root/>"])
def test_ascii_declaration_with_non_ascii_bytes_is_still_rejected(text):
    # A BOM before the declaration is not ASCII either.
    xml = f'<?xml version="1.0" encoding="ASCII"?>{text}'
    with pytest.raises(ToolError, match="invalid_encoding"):
        admit_inline([InlineFile(path="doc.xml", text=xml)], Limits())


def test_compose_cycle_explanation_addresses_the_schema_not_the_instance(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _inputs, _pool, _workspace):
            result = await service.explain_issue(ExplainIssueRequest(code="compose-cycle"))
            assert result.data["known"] is True
            assert "cycle" in result.data["summary"]
            assert result.data["actions"]
            assert "instance" in result.data["uncertainty"]

    asyncio.run(scenario())


def test_valid_document_with_schema_warning_does_not_expose_a_phantom_page(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            cyclic = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:include schemaLocation="schema.xsd"/><xs:element name="root" type="xs:string"/></xs:schema>'
            await upload(inputs, workspace, {"schema.xsd": cyclic, "doc.xml": "<root/>"})
            result = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    schema_ref=schema_ref(),
                    document=FileRef(path="doc.xml"),
                )
            )
            assert result.data["valid"] is True
            assert result.data["report"] is None
            assert result.artifacts == []
            assert result.page is None

    asyncio.run(scenario())


@pytest.mark.parametrize("missing,role", [("schema.xsd", "schema"), ("doc.xml", "document")])
def test_missing_input_names_its_role(tmp_path, missing, role):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    p: v
                    for p, v in {"schema.xsd": XSD, "doc.xml": "<root/>"}.items()
                    if p != missing
                },
            )
            with pytest.raises(ToolError) as error:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        schema_ref=schema_ref(),
                        document=FileRef(path="doc.xml"),
                    )
                )
            assert error.value.fault.code == "not_found"
            assert error.value.fault.details["role"] == role
            assert error.value.fault.details["path"] == missing

    asyncio.run(scenario())


def test_artifact_page_limit_reports_the_configured_bound(tmp_path):
    async def scenario():
        async with harness(tmp_path, Limits(workers=1, page_items=3)) as (
            _service,
            inputs,
            _pool,
            _workspace,
        ):
            with pytest.raises(ToolError) as error:
                await inputs.outputs.list(limit=4)
            assert "3" in error.value.fault.message
            assert error.value.fault.details == {"limit": 4, "maximum": 3}

    asyncio.run(scenario())


def test_fingerprint_is_conservative_capture_identity_not_generation(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            request = InspectSchemaRequest(workspace_id=workspace, schema_ref=schema_ref())
            first = await service.inspect_schema(request)
            await upload(inputs, workspace, {"notes.txt": "not part of the schema"})
            second = await service.inspect_schema(request)
            await upload(inputs, workspace, {"notes.txt": "not part of the schema"})
            third = await service.inspect_schema(request)
            assert first.provenance["schema_fingerprint"] != second.provenance["schema_fingerprint"]
            assert second.provenance["schema_fingerprint"] == third.provenance["schema_fingerprint"]

    asyncio.run(scenario())


@pytest.mark.parametrize("xml", ["<root>\n<item></root>", "<root>\n", "<root><!--"])
def test_malformed_xml_keeps_parser_diagnosis_and_position(tmp_path, xml):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": XSD, "broken.xml": xml})
            with pytest.raises(ToolError) as error:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                        document=FileRef(path="broken.xml"),
                    )
                )
            fault = error.value.fault
            assert fault.code == "invalid_xml"
            assert "invalid_path" not in fault.message
            assert "line" in fault.message and "column" in fault.message
            assert fault.details["line"] >= 1
            assert fault.details["column"] >= 0
            assert await inputs.outputs.list() == ([], None)

    asyncio.run(scenario())
