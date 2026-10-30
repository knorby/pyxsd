"""Production tools capture from --source-root when the workspace is empty."""

import base64
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
    InspectSchemaRequest,
    QueryDocumentRequest,
    SchemaRef,
    ValidateDocumentRequest,
    ValidateDocumentsRequest,
)
from pyxsd.mcp.errors import ToolError

MAIN = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:t="urn:t"
            targetNamespace="urn:t" elementFormDefault="qualified">
  <xs:include schemaLocation="inc.xsd"/>
  <xs:element name="main">
    <xs:complexType><xs:sequence><xs:element ref="t:inc"/></xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>"""
INC = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:t"
            elementFormDefault="qualified">
  <xs:element name="inc">
    <xs:complexType><xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>"""
OTHER = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root-b">
    <xs:complexType><xs:sequence><xs:element name="value" type="xs:int"/></xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>"""
A = '<main xmlns="urn:t"><inc><a>x</a></inc></main>'
B = '<main xmlns="urn:t"><inc><a>y</a></inc></main>'
DOC_B = "<root-b><value>1</value></root-b>"


def make_tree(root: Path) -> None:
    (root / "schema").mkdir()
    (root / "data").mkdir()
    (root / "schema" / "main.xsd").write_text(MAIN)
    (root / "schema" / "inc.xsd").write_text(INC)
    (root / "other.xsd").write_text(OTHER)
    (root / "data" / "a.xml").write_text(A)
    (root / "data" / "b.xml").write_text(B)
    (root / "o.xml").write_text(DOC_B)


@asynccontextmanager
async def harness(tmp_path: Path, *, source: bool = True):
    from pyxsd.mcp.inputs import SessionInputs
    from pyxsd.mcp.output_store import OutputStore
    from pyxsd.mcp.services.exploration import ExplorationService
    from pyxsd.mcp.storage import Storage
    from pyxsd.mcp.workers import WorkerPool

    source_root = tmp_path / "tree"
    source_root.mkdir()
    make_tree(source_root)
    limits = Limits(workers=1)
    storage = Storage(tmp_path / "state", persist=False)
    outputs = OutputStore(storage, limits)
    pool = WorkerPool(limits)
    inputs = SessionInputs(
        storage.scratch, limits, source_root=source_root if source else None, discovery=pool
    )
    workspace = await inputs.create()
    service = ExplorationService(inputs, pool, outputs=outputs)
    try:
        yield service, inputs, pool, workspace.id
    finally:
        await pool.aclose()
        outputs.close()
        storage.close()


def main_ref():
    return SchemaRef(source=FileRef(path="schema/main.xsd"))


def other_ref():
    return SchemaRef(source=FileRef(path="other.xsd"))


def test_source_root_batch_shared_schema_and_repeated_document(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _inputs, _pool, workspace):
            result = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=tuple(
                        BatchValidationItem(document=FileRef(path=path), schema_ref=main_ref())
                        for path in ("data/a.xml", "data/b.xml", "data/a.xml")
                    ),
                )
            )
            assert result.data["summary"] == {"total": 3, "valid": 3, "invalid": 0, "failed": 0}

    anyio.run(scenario)


def test_source_root_validate_without_upload(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _inputs, _pool, workspace):
            envelope = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="data/a.xml"),
                    schema_ref=main_ref(),
                )
            )
            assert envelope.data["valid"] is True
            assert envelope.provenance["workspace_id"] == workspace
            assert envelope.provenance["generation"] == 0

    anyio.run(scenario)


def test_source_root_inspect_and_query(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _inputs, _pool, workspace):
            overview = await service.inspect_schema(
                InspectSchemaRequest(workspace_id=workspace, schema_ref=main_ref())
            )
            assert overview.data["target_namespace"] == "urn:t"
            queried = await service.query_document(
                QueryDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="data/a.xml"),
                    schema_ref=main_ref(),
                    xpath="/t:main/t:inc/t:a",
                    namespaces={"t": "urn:t"},
                )
            )
            assert queried.data["results"]

    anyio.run(scenario)


def test_source_root_subtree_is_exact_utf16_source_bytes(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _inputs, _pool, workspace):
            body = '<t:main xmlns:t="urn:t"><t:inc><t:a>caf\u00e9</t:a></t:inc></t:main>'
            content = ('<?xml version="1.0" encoding="UTF-16"?>\n' + body + "\n").encode("utf-16")
            encoded_body = body.encode("utf-16")[2:]
            (tmp_path / "tree" / "data" / "utf16.xml").write_bytes(content)
            result = await service.query_document(
                QueryDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="data/utf16.xml"),
                    schema_ref=main_ref(),
                    xpath="/t:main",
                    namespaces={"t": "urn:t"},
                    result="subtree",
                )
            )
            item = result.data["results"][0]
            decoded = base64.b64decode(item["data"])
            start = content.index(encoded_body)
            assert decoded == encoded_body
            assert item["byte_length"] == len(encoded_body)
            assert item["span"] == {"start": start, "end": start + len(encoded_body)}
            assert item["source_faithful"] is True

    anyio.run(scenario)


def test_source_root_imported_schema_discovery(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            seen = {}

            original = inputs.capture_source

            @asynccontextmanager
            async def spy(workspace_id, refs):
                async with original(workspace_id, refs) as capture:
                    seen["files"] = [item.path for item in capture.files]
                    seen["workspace_id"] = capture.workspace_id
                    yield capture

            inputs.capture_source = spy
            envelope = await service.validate_document(
                ValidateDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="data/a.xml"),
                    schema_ref=main_ref(),
                )
            )
            assert envelope.data["valid"] is True
            assert "schema/inc.xsd" in seen["files"]
            assert seen["workspace_id"] == workspace

    anyio.run(scenario)


def test_source_root_batch_mixed_schemas(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _inputs, _pool, workspace):
            envelope = await service.validate_documents(
                ValidateDocumentsRequest(
                    workspace_id=workspace,
                    documents=(
                        BatchValidationItem(
                            document=FileRef(path="data/a.xml"), schema_ref=main_ref()
                        ),
                        BatchValidationItem(document=FileRef(path="o.xml"), schema_ref=other_ref()),
                    ),
                )
            )
            assert [result["valid"] for result in envelope.data["results"]] == [True, True]

    anyio.run(scenario)


def test_uploaded_inputs_not_merged_with_source(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await inputs.put_files(workspace, [InlineFile(path="data/a.xml", text=A)])
            with pytest.raises(ToolError) as raised:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="data/b.xml"),
                        schema_ref=main_ref(),
                    )
                )
            assert raised.value.fault.code == "not_found"

    anyio.run(scenario)


def test_empty_workspace_no_source_root(tmp_path):
    async def scenario():
        async with harness(tmp_path, source=False) as (service, _inputs, _pool, workspace):
            with pytest.raises(ToolError) as raised:
                await service.validate_document(
                    ValidateDocumentRequest(
                        workspace_id=workspace,
                        document=FileRef(path="data/a.xml"),
                        schema_ref=main_ref(),
                    )
                )
            assert raised.value.fault.code == "not_found"

    anyio.run(scenario)
