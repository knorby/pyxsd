"""RELAX NG operational faults stay distinct from XML and schema verdicts."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, upload

from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, InspectSchemaRequest, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.services.exploration import ValidateDocumentRequest

NS = "http://relaxng.org/ns/structure/1.0"

VALID_SCHEMA = f"""<grammar xmlns="{NS}">
  <start><element name="root"><text/></element></start>
</grammar>"""
VALID_DOCUMENT = "<root>hello</root>"

UNSUPPORTED_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="root">
      <data type="custom" datatypeLibrary="urn:example:custom"/>
    </element>
  </start>
</grammar>"""


def relaxng_ref(path="schema.rng"):
    return SchemaRef(source=FileRef(path=path), format="relax-ng")


def test_validate_document_accepts_a_relaxng_upload(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": VALID_SCHEMA, "document.xml": VALID_DOCUMENT},
            )
            envelope = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="document.xml"),
                    schema_ref=relaxng_ref(),
                )
            )
            assert envelope.data["valid"] is True

    anyio.run(scenario)


def test_relaxng_budget_exhaustion_is_not_a_verdict(tmp_path):
    limits = Limits(enforced=True, workers=1, relaxng_pattern_nodes=1)

    async def scenario():
        async with harness(tmp_path, limits) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": VALID_SCHEMA, "document.xml": VALID_DOCUMENT},
            )
            with pytest.raises(ToolError) as raised:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="document.xml"),
                        schema_ref=relaxng_ref(),
                    )
                )
            fault = raised.value.fault
            assert fault.code == "limit_exceeded"
            assert fault.code not in {"invalid_xml", "invalid_schema"}
            assert fault.details["stage"] == "compile"
            assert fault.details["budget"] == "max_pattern_nodes"
            assert fault.details["configured"] == 1
            assert fault.details["observed"] >= 2

    anyio.run(scenario)


def test_unsupported_relaxng_library_is_not_a_verdict(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": UNSUPPORTED_SCHEMA, "document.xml": VALID_DOCUMENT},
            )
            with pytest.raises(ToolError) as raised:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="document.xml"),
                        schema_ref=relaxng_ref(),
                    )
                )
            fault = raised.value.fault
            assert fault.code == "unsupported_capability"
            assert fault.code not in {"invalid_xml", "invalid_schema"}
            assert "urn:example:custom" in fault.message

    anyio.run(scenario)


def test_xsd_only_inspection_rejects_relaxng_explicitly(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.rng": VALID_SCHEMA})
            with pytest.raises(ToolError) as raised:
                await service.inspect_schema(
                    InspectSchemaRequest(workspace_id=workspace, schema_ref=relaxng_ref())
                )
            assert raised.value.fault.code == "unsupported_capability"

    anyio.run(scenario)
