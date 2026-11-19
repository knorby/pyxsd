"""RELAX NG operational faults stay distinct from XML and schema verdicts."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, upload

from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import (
    BatchValidationItem,
    FileRef,
    InspectSchemaRequest,
    SchemaRef,
    ValidateDocumentsRequest,
)
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.services.exploration import ValidateDocumentRequest

NS = "http://relaxng.org/ns/structure/1.0"

VALID_SCHEMA = f"""<grammar xmlns="{NS}">
  <start><element name="root"><text/></element></start>
</grammar>"""
VALID_DOCUMENT = "<root>hello</root>"

XSD_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root-a" type="xs:int"/>
</xs:schema>"""
XSD_DOCUMENT = "<root-a>7</root-a>"

UNSUPPORTED_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="root">
      <data type="custom" datatypeLibrary="urn:example:custom"/>
    </element>
  </start>
</grammar>"""


def relaxng_ref(path="schema.rng"):
    return SchemaRef(source=FileRef(path=path), format="relax-ng")


def xsd_ref(path="schema.xsd"):
    return SchemaRef(source=FileRef(path=path))


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


def test_batch_limit_fault_does_not_contaminate_other_item(tmp_path):
    limits = Limits(enforced=True, workers=1, relaxng_pattern_nodes=1)

    async def scenario():
        async with harness(tmp_path, limits) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.xsd": XSD_SCHEMA,
                    "schema.rng": VALID_SCHEMA,
                    "xsd.xml": XSD_DOCUMENT,
                    "rng.xml": VALID_DOCUMENT,
                },
            )
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(
                        BatchValidationItem(
                            document=FileRef(path="rng.xml"), schema_ref=relaxng_ref()
                        ),
                        BatchValidationItem(document=FileRef(path="xsd.xml"), schema_ref=xsd_ref()),
                    ),
                )
            )
            rng_result, xsd_result = envelope.data["results"]
            fault = rng_result["fault"]
            assert fault["code"] == "limit_exceeded"
            assert fault["code"] not in {"invalid_xml", "invalid_schema"}
            assert fault["details"]["stage"] == "compile"
            assert fault["details"]["budget"] == "max_pattern_nodes"
            assert fault["details"]["configured"] == 1
            assert fault["details"]["observed"] >= 2
            assert xsd_result["fault"] is None
            assert xsd_result["valid"] is True
            assert envelope.data["summary"] == {
                "total": 2,
                "valid": 1,
                "invalid": 0,
                "failed": 1,
            }

    anyio.run(scenario)


def test_batch_unsupported_library_does_not_contaminate_other_item(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.xsd": XSD_SCHEMA,
                    "schema.rng": UNSUPPORTED_SCHEMA,
                    "xsd.xml": XSD_DOCUMENT,
                    "rng.xml": VALID_DOCUMENT,
                },
            )
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(
                        BatchValidationItem(
                            document=FileRef(path="rng.xml"), schema_ref=relaxng_ref()
                        ),
                        BatchValidationItem(document=FileRef(path="xsd.xml"), schema_ref=xsd_ref()),
                    ),
                )
            )
            rng_result, xsd_result = envelope.data["results"]
            fault = rng_result["fault"]
            assert fault["code"] == "unsupported_capability"
            assert fault["code"] not in {"invalid_xml", "invalid_schema"}
            assert "urn:example:custom" in fault["message"]
            assert xsd_result["fault"] is None
            assert xsd_result["valid"] is True
            assert envelope.data["summary"] == {
                "total": 2,
                "valid": 1,
                "invalid": 0,
                "failed": 1,
            }

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
