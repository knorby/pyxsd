"""Local-only MCP operations return direct results instead of durable jobs."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio

from pydantic import ValidationError
from pyxsd import __version__
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, InlineFile, SchemaRef
from pyxsd.mcp.errors import ToolError

OLD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="Root"/>
<xs:complexType name="Root"><xs:sequence>
<xs:element name="item" type="xs:int"/>
</xs:sequence></xs:complexType>
</xs:schema>"""

NEW = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="Root"/>
<xs:complexType name="Root"><xs:sequence>
<xs:element name="item" type="xs:int"/>
<xs:element name="note" type="xs:string" minOccurs="0"/>
</xs:sequence></xs:complexType>
</xs:schema>"""

DOC = "<root><item>1</item></root>"
REPAIR = "def transform(document, context):\n    return document\n"


def test_batch_parse_reuses_schema_lease_per_unique_schema(tmp_path):
    """One batch operation leases each unique schema once, in process order."""
    import hashlib

    from pyxsd.mcp import worker_main
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.captures import CaptureFile, InputCapture
    from pyxsd.mcp.contracts import BatchValidationItem
    from pyxsd.mcp.worker_protocol import BatchParseOperation
    from pyxsd.mcp.workers import capture_to_wire

    schema_a = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="root-a"><xs:complexType><xs:sequence>'
        '<xs:element name="value" type="xs:int"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    schema_b = schema_a.replace("root-a", "root-b")
    files = {
        "schema-a.xsd": schema_a.encode(),
        "schema-b.xsd": schema_b.encode(),
        "a1.xml": b"<root-a><value>1</value></root-a>",
        "a2.xml": b"<root-a><value>2</value></root-a>",
        "b1.xml": b"<root-b><value>3</value></root-b>",
    }
    manifest = []
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
        manifest.append(CaptureFile(name, hashlib.sha256(content).hexdigest(), len(content)))
    capture = InputCapture("workspace", 1, tmp_path, tuple(manifest))
    operation = BatchParseOperation(
        request_id="batch",
        capture=capture_to_wire(capture),
        items=(
            BatchValidationItem(
                document=FileRef(path="a1.xml"),
                schema_ref=SchemaRef(source=FileRef(path="schema-a.xsd")),
            ),
            BatchValidationItem(
                document=FileRef(path="b1.xml"),
                schema_ref=SchemaRef(source=FileRef(path="schema-b.xsd")),
            ),
            BatchValidationItem(
                document=FileRef(path="a2.xml"),
                schema_ref=SchemaRef(source=FileRef(path="schema-a.xsd")),
            ),
        ),
    )
    cache = SchemaCache(Limits())
    result = worker_main._execute(operation, cache, __version__)
    paths = [item["document"]["path"] for item in result["items"]]
    assert paths == ["a1.xml", "b1.xml", "a2.xml"]
    assert all(item["valid"] is True for item in result["items"])
    info = cache.info()
    # One lease per unique schema inside the batch: two compiles, no re-lease.
    assert (info.misses, info.hits) == (2, 0)
    worker_main._execute(operation, cache, __version__)
    info = cache.info()
    # A second batch reuses the compiled schemas: no new compiles, both leased.
    assert (info.misses, info.hits) == (2, 2)
    cache.close()


def test_batch_parse_rejects_items_over_worker_budget(tmp_path):
    import hashlib

    from pyxsd.mcp import worker_main
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.captures import CaptureFile, InputCapture
    from pyxsd.mcp.contracts import BatchValidationItem
    from pyxsd.mcp.worker_protocol import BatchParseOperation
    from pyxsd.mcp.workers import capture_to_wire

    (tmp_path / "doc.xml").write_bytes(b"<root-a/>")
    content = (tmp_path / "doc.xml").read_bytes()
    capture = InputCapture(
        "workspace",
        1,
        tmp_path,
        (CaptureFile("doc.xml", hashlib.sha256(content).hexdigest(), len(content)),),
    )
    operation = BatchParseOperation(
        request_id="batch",
        capture=capture_to_wire(capture),
        items=tuple(BatchValidationItem(document=FileRef(path="doc.xml")) for _ in range(3)),
    )
    cache = SchemaCache(Limits(corpus_files=2, corpus_max_files=2))
    with pytest.raises(ToolError) as raised:
        worker_main._execute(operation, cache, __version__)
    assert raised.value.fault.code == "invalid_argument"
    cache.close()


@pytest.mark.parametrize("hinted", [False, True])
def test_batch_manifested_document_disappeared(tmp_path, hinted):
    import hashlib

    from pyxsd.mcp import worker_main
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.captures import CaptureFile, InputCapture
    from pyxsd.mcp.contracts import BatchValidationItem
    from pyxsd.mcp.worker_protocol import BatchParseOperation
    from pyxsd.mcp.workers import capture_to_wire

    content = b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="root" type="xs:string"/></xs:schema>'
    (tmp_path / "a.xsd").write_bytes(content)
    # Manifested but absent instance bytes simulate a post-capture disappearance.
    capture = InputCapture(
        "workspace",
        1,
        tmp_path,
        (
            CaptureFile("a.xsd", hashlib.sha256(content).hexdigest(), len(content)),
            CaptureFile("gone.xml", hashlib.sha256(b"<root/>").hexdigest(), 7),
        ),
    )
    operation = BatchParseOperation(
        request_id="vanished",
        capture=capture_to_wire(capture),
        items=(
            BatchValidationItem(
                document=FileRef(path="gone.xml"),
                schema_ref=None if hinted else SchemaRef(source=FileRef(path="a.xsd")),
            ),
        ),
    )
    cache = SchemaCache(Limits())
    try:
        result = worker_main._execute(operation, cache, __version__)
    finally:
        cache.close()
    assert result["items"][0]["fault"]["code"] == "not_found"


def test_sdk_exposes_direct_results_and_no_job_tools(tmp_path):
    from tests.mcp.test_server import connected

    async def scenario():
        async with connected(
            tmp_path,
            enabled_groups={"exploration", "workspaces", "execution"},
            execution_backend="trusted-local",
        ) as (client, _services):
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "pyxsd_job_status" not in names
            assert "pyxsd_cancel_job" not in names

            created = await client.call_tool("pyxsd_manage_workspace", {"action": "create"})
            workspace = created.structured_content["data"]["id"]
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {"path": "old.xsd", "text": OLD},
                        {"path": "new.xsd", "text": NEW},
                        {"path": "doc.xml", "text": DOC},
                    ],
                },
            )
            compared = await client.call_tool(
                "pyxsd_compare_schemas",
                {
                    "workspace_id": workspace,
                    "old_schema": {"source": {"path": "old.xsd"}},
                    "new_schema": {"source": {"path": "new.xsd"}},
                    "corpus": [{"path": "doc.xml"}],
                },
            )
            comparison = compared.structured_content["data"]
            assert comparison["structural"]["totals"]["added"] >= 1
            assert comparison["corpus"]["files"][0]["status"] == "valid"
            assert comparison["conclusions"]["corpus_only"] is True
            assert "state" not in comparison
            assert "id" not in comparison

            previewed = await client.call_tool(
                "pyxsd_preview_transform",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "source_schema": {"source": {"path": "old.xsd"}},
                    "transform": {"kind": "python", "source": REPAIR},
                },
            )
            record = previewed.structured_content["data"]
            assert previewed.is_error is False, previewed
            assert "valid" in record, record
            assert record["candidate"]["digest"]
            assert "state" not in record
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "pyxsd_commit_transform" not in names

    anyio.run(scenario)


def test_direct_requests_have_no_job_only_idempotency_field():
    from pyxsd.mcp.previews import PreviewTransformRequest
    from pyxsd.mcp.services.comparison import CompareSchemasRequest
    from pyxsd.mcp.services.records import ExportRecordsRequest

    for model, fields in (
        (CompareSchemasRequest, {"workspace_id": "w"}),
        (ExportRecordsRequest, {"workspace_id": "w", "element": "root"}),
        (
            PreviewTransformRequest,
            {
                "workspace_id": "w",
                "document": FileRef(path="doc.xml"),
                "source_schema": SchemaRef(source=FileRef(path="schema.xsd")),
                "transform": {"kind": "python", "source": REPAIR},
            },
        ),
    ):
        with pytest.raises(ValidationError):
            model(idempotency_key="not-a-job", **fields)


def test_artifacts_survive_session_restart_but_workspaces_do_not(tmp_path):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.inputs import SessionInputs
    from pyxsd.mcp.output_store import OutputStore
    from pyxsd.mcp.storage import Storage

    async def scenario():
        storage = Storage(tmp_path / "state")
        try:
            outputs = OutputStore(storage, Limits())
            inputs = SessionInputs(storage.scratch, Limits())
            workspace = await inputs.create()
            await inputs.put_files(workspace.id, [InlineFile(path="doc.xml", text=DOC)])
            kept = await outputs.put_bytes(
                b"evidence", name="report.json", media_type="application/json", kind="report"
            )
            outputs.close()
        finally:
            storage.close()

        restarted = Storage(tmp_path / "state")
        try:
            outputs = OutputStore(restarted, Limits())
            inputs = SessionInputs(restarted.scratch, Limits())
            assert await inputs.list() == []
            with pytest.raises(ToolError, match="not_found"):
                await inputs.put_files(workspace.id, [])
            assert await outputs.read(kept.id, offset=0, length=64) == b"evidence"
            outputs.close()
        finally:
            restarted.close()

    anyio.run(scenario)


def test_batch_worker_item_fault_for_manifest_missing_document(tmp_path):
    """Defensive worker branch: a document absent from the manifest faults one item."""
    import hashlib

    from pyxsd.mcp import worker_main
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.captures import CaptureFile, InputCapture
    from pyxsd.mcp.contracts import BatchValidationItem
    from pyxsd.mcp.worker_protocol import BatchParseOperation
    from pyxsd.mcp.workers import capture_to_wire

    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="root-a"><xs:complexType><xs:sequence>'
        '<xs:element name="value" type="xs:int"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    good = b"<root-a><value>1</value></root-a>"
    files = {"schema-a.xsd": schema.encode(), "good-a.xml": good}
    manifest = []
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
        manifest.append(CaptureFile(name, hashlib.sha256(content).hexdigest(), len(content)))
    capture = InputCapture("workspace", 1, tmp_path, tuple(manifest))
    operation = BatchParseOperation(
        request_id="batch",
        capture=capture_to_wire(capture),
        items=(
            BatchValidationItem(
                document=FileRef(path="good-a.xml"),
                schema_ref=SchemaRef(source=FileRef(path="schema-a.xsd")),
            ),
            BatchValidationItem(
                document=FileRef(path="missing.xml"),
                schema_ref=SchemaRef(source=FileRef(path="schema-a.xsd")),
            ),
        ),
    )
    cache = SchemaCache(Limits())
    result = worker_main._execute(operation, cache, __version__)
    assert result["items"][0]["valid"] is True
    assert result["items"][1]["valid"] is None
    assert result["items"][1]["fault"]["code"] == "not_found"
    cache.close()
