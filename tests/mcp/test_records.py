"""Record projections stay bounded and worker-local, with exact evidence."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("pyarrow")

import anyio
from tests.mcp.test_exploration import harness, upload
from tests.mcp.test_server import connected, create_workspace, structured

from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.errors import ToolError

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root"><xs:complexType><xs:sequence>
<xs:element name="item" maxOccurs="unbounded"><xs:complexType><xs:sequence>
<xs:element name="amount" type="xs:decimal"/>
<xs:element name="payload" type="xs:base64Binary"/>
</xs:sequence><xs:attribute name="id" type="xs:string" use="required"/>
</xs:complexType></xs:element></xs:sequence>
<xs:attribute name="batch" type="xs:string"/>
</xs:complexType></xs:element></xs:schema>"""


def xml(count=25):
    return (
        '<root batch="one">'
        + "".join(
            f'<item id="{i}"><amount>1.2300</amount><payload>AP8=</payload></item>'
            for i in range(count)
        )
        + "</root>"
    )


def test_preview_caps_rows_and_reports_exact_total_and_values(tmp_path):
    from pyxsd.mcp.services.records import PreviewRecordsRequest, RecordService

    async def scenario():
        async with harness(tmp_path) as (_, snapshots, pool, principal, workspace):
            await upload(snapshots, workspace, {"schema.xsd": XSD, "input.xml": xml()})
            service = RecordService(snapshots, pool)
            request = PreviewRecordsRequest(
                workspace_id=workspace,
                schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                document=FileRef(path="input.xml"),
                element="root",
                path=("item",),
                selector="./item",
            )
            result = await service.preview_records(principal, request)
            assert len(result.data["rows"]) == 20
            assert result.data["row_count"] == 25
            assert result.data["count_complete"] is True
            assert result.data["truncated"] is True
            assert result.data["rows"][0]["amount"] == "1.2300"
            assert result.data["rows"][0]["payload"] == {"encoding": "base64", "data": "AP8="}
            assert len(result.provenance["schema_fingerprint"]) == 64
            selected = await service.preview_records(
                principal, request.model_copy(update={"selector": "./item[@id='24']"})
            )
            assert selected.data["row_count"] == 1
            assert selected.data["rows"][0]["@id"] == "24"

    anyio.run(scenario)


def test_invalid_document_is_not_silently_exported(tmp_path):
    from pyxsd.mcp.services.records import PreviewRecordsRequest, RecordService

    async def scenario():
        async with harness(tmp_path) as (_, snapshots, pool, principal, workspace):
            await upload(
                snapshots,
                workspace,
                {
                    "schema.xsd": XSD,
                    "input.xml": xml(1).replace("1.2300", "bad"),
                },
            )
            with pytest.raises(ToolError, match="invalid_document"):
                await RecordService(snapshots, pool).preview_records(
                    principal,
                    PreviewRecordsRequest(
                        workspace_id=workspace,
                        schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                        document=FileRef(path="input.xml"),
                        element="root",
                        path=("item",),
                        selector="./item",
                    ),
                )

    anyio.run(scenario)


def test_contextual_columns_and_incomplete_counts(tmp_path):
    from pyxsd.mcp.services.records import PreviewRecordsRequest, RecordService
    from pyxsd.mcp.worker_protocol import FieldSourceSpec

    async def scenario():
        async with harness(tmp_path) as (_, snapshots, pool, principal, workspace):
            await upload(snapshots, workspace, {"schema.xsd": XSD, "input.xml": xml(3)})
            service = RecordService(snapshots, pool)
            request = PreviewRecordsRequest(
                workspace_id=workspace,
                schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                document=FileRef(path="input.xml"),
                element="root",
                path=("item",),
                selector="./item",
                columns={
                    "id": FieldSourceSpec(attribute="id"),
                    "batch": FieldSourceSpec(scope="root", attribute="batch"),
                },
            )
            result = await service.preview_records(principal, request)
            assert result.data["rows"] == [
                {"id": "0", "batch": "one"},
                {"id": "1", "batch": "one"},
                {"id": "2", "batch": "one"},
            ]
            fast = await service.preview_records(
                principal, request.model_copy(update={"exact_count": False})
            )
            assert fast.data["count_complete"] is True
            assert fast.data["row_count"] == 3

    anyio.run(scenario)


def test_arrow_availability_modes(tmp_path, monkeypatch):
    import builtins

    import anyio

    from pyxsd.mcp.config import Settings
    from pyxsd.mcp.errors import ToolError
    from pyxsd.mcp.server import Services
    from pyxsd.mcp.tools import capability_data

    original = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError("pyarrow shared library is incompatible with this interpreter")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(ToolError, match="arrow_unavailable"):
        Services(Settings(state_dir=tmp_path / "broken", arrow="enabled"))
    monkeypatch.setattr(builtins, "__import__", original)

    def absent(name, *args, **kwargs):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ModuleNotFoundError("No module named 'pyarrow'", name="pyarrow")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", absent)
    auto = Services(Settings(state_dir=tmp_path / "absent", arrow="auto"))
    data = capability_data(auto)
    assert data["adapters"]["arrow"]["mode"] == "auto"
    assert data["adapters"]["arrow"]["available"] is False
    disabled = Services(Settings(state_dir=tmp_path / "off", arrow="disabled"))
    assert capability_data(disabled)["adapters"]["arrow"]["available"] is False
    with pytest.raises(ToolError, match="arrow_required"):
        Services(Settings(state_dir=tmp_path / "required", arrow="enabled"))
    anyio.run(auto.aclose)
    anyio.run(disabled.aclose)


def _export_request(workspace, **overrides):
    from pyxsd.mcp.services.records import ExportRecordsRequest

    fields = {
        "workspace_id": workspace,
        "schema_ref": SchemaRef(source=FileRef(path="schema.xsd")),
        "documents": (FileRef(path="input.xml"),),
        "element": "root",
        "path": ("item",),
        "selector": "./item",
        "idempotency_key": "export",
    }
    fields.update(overrides)
    return ExportRecordsRequest(**fields)


async def _terminal(service, principal, ref):
    with anyio.fail_after(20):
        while True:
            result = await service.jobs.status(principal, ref.id)
            if result.state in {"succeeded", "failed", "cancelled"}:
                return result
            await anyio.sleep(0.01)


def test_export_records_job_produces_independent_parquet_artifact(tmp_path):
    import hashlib
    import io

    import pyarrow.parquet as parquet

    from pyxsd.mcp.services.records import RecordService

    async def scenario():
        async with harness(tmp_path) as (_, snapshots, pool, principal, workspace):
            await upload(snapshots, workspace, {"schema.xsd": XSD, "input.xml": xml()})
            from pyxsd.mcp.artifacts import ArtifactStore

            artifacts = ArtifactStore(snapshots.workspaces)
            service = RecordService(snapshots, pool, artifacts=artifacts)
            ref = await service.export_records(
                principal, _export_request(workspace, idempotency_key="parquet-1")
            )
            result = await _terminal(service, principal, ref)
            assert result.state == "succeeded", result.fault
            payload = result.result
            assert payload["format"] == "parquet"
            assert payload["row_count"] == 25
            assert payload["source_documents"] == ["input.xml"]
            assert len(payload["schema_fingerprint"]) == 64
            entry = next(item for item in payload["artifacts"] if item["path"] == "export.parquet")
            assert len(entry["digest"]) == 64
            assert entry["size"] > 0
            async with service.artifacts.lease(principal, entry["id"]) as reader:
                data = await reader.read(offset=0, length=entry["size"])
            assert hashlib.sha256(data).hexdigest() == entry["digest"]
            table = parquet.read_table(io.BytesIO(data))
            assert table.num_rows == 25
            assert table.column("amount")[0].as_py() == "1.2300"
            assert table.column("@id")[0].as_py() == "0"

    anyio.run(scenario)


def test_export_invalid_document_fails_without_artifacts(tmp_path):
    from pyxsd.mcp.services.records import RecordService

    async def scenario():
        async with harness(tmp_path) as (_, snapshots, pool, principal, workspace):
            await upload(
                snapshots,
                workspace,
                {
                    "schema.xsd": XSD,
                    "input.xml": xml(1).replace("1.2300", "bad"),
                },
            )
            from pyxsd.mcp.artifacts import ArtifactStore

            artifacts = ArtifactStore(snapshots.workspaces)
            service = RecordService(snapshots, pool, artifacts=artifacts)
            ref = await service.export_records(
                principal, _export_request(workspace, idempotency_key="bad")
            )
            result = await _terminal(service, principal, ref)
            assert result.state == "failed"
            assert result.fault.code == "invalid_document"
            assert result.result is None

    anyio.run(scenario)


def test_export_output_quota_refusal_leaves_no_artifact(tmp_path):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.services.records import RecordService

    async def scenario():
        async with harness(tmp_path, Limits(workers=1, output_bytes=512)) as (
            _,
            snapshots,
            pool,
            principal,
            workspace,
        ):
            await upload(snapshots, workspace, {"schema.xsd": XSD, "input.xml": xml()})
            from pyxsd.mcp.artifacts import ArtifactStore

            artifacts = ArtifactStore(snapshots.workspaces)
            service = RecordService(snapshots, pool, artifacts=artifacts)
            ref = await service.export_records(
                principal, _export_request(workspace, idempotency_key="quota")
            )
            result = await _terminal(service, principal, ref)
            assert result.state == "failed"
            assert result.fault.code == "output_limit"
            assert result.result is None

    anyio.run(scenario)


def test_export_dataset_multiple_documents_manifest_and_corpus_quota(tmp_path):
    import hashlib

    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.services.records import RecordService

    async def scenario():
        async with harness(tmp_path, Limits(workers=1, corpus_files=1)) as (
            _,
            snapshots,
            pool,
            principal,
            workspace,
        ):
            await upload(
                snapshots,
                workspace,
                {
                    "schema.xsd": XSD,
                    "a.xml": xml(2),
                    "b.xml": xml(3),
                },
            )
            from pyxsd.mcp.artifacts import ArtifactStore

            artifacts = ArtifactStore(snapshots.workspaces)
            service = RecordService(snapshots, pool, artifacts=artifacts)
            request = _export_request(
                workspace,
                documents=(FileRef(path="a.xml"), FileRef(path="b.xml")),
                format="dataset",
                idempotency_key="dataset",
            )
            with pytest.raises(ToolError, match="quota_exceeded"):
                await service.export_records(principal, request)

    anyio.run(scenario)

    async def scenario_two():
        async with harness(tmp_path / "two") as (_, snapshots, pool, principal, workspace):
            await upload(
                snapshots,
                workspace,
                {
                    "schema.xsd": XSD,
                    "a.xml": xml(2),
                    "b.xml": xml(3),
                },
            )
            from pyxsd.mcp.artifacts import ArtifactStore

            artifacts = ArtifactStore(snapshots.workspaces)
            service = RecordService(snapshots, pool, artifacts=artifacts)
            ref = await service.export_records(
                principal,
                _export_request(
                    workspace,
                    documents=(FileRef(path="a.xml"), FileRef(path="b.xml")),
                    format="dataset",
                    idempotency_key="dataset",
                ),
            )
            result = await _terminal(service, principal, ref)
            assert result.state == "succeeded", result.fault
            payload = result.result
            assert payload["format"] == "dataset"
            assert payload["row_count"] == 5
            assert payload["source_documents"] == ["a.xml", "b.xml"]
            assert len(payload["artifacts"]) >= 2
            assert all(item["path"].startswith("export-dataset/") for item in payload["artifacts"])
            assert {item["path"] for item in payload["manifest"]} == {
                item["path"] for item in payload["artifacts"]
            }
            entry = payload["artifacts"][0]
            async with service.artifacts.lease(principal, entry["id"]) as reader:
                data = await reader.read(offset=0, length=entry["size"])
            assert hashlib.sha256(data).hexdigest() == entry["digest"]

    anyio.run(scenario_two)


def test_arrow_gate_blocks_record_services(tmp_path):
    from pyxsd.mcp.services.records import PreviewRecordsRequest, RecordService

    async def scenario():
        async with harness(tmp_path) as (_, snapshots, pool, principal, workspace):
            await upload(snapshots, workspace, {"schema.xsd": XSD, "input.xml": xml(1)})
            service = RecordService(snapshots, pool, arrow=False)
            with pytest.raises(ToolError, match="arrow_required"):
                await service.preview_records(
                    principal,
                    PreviewRecordsRequest(
                        workspace_id=workspace,
                        schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                        document=FileRef(path="input.xml"),
                        element="root",
                        path=("item",),
                        selector="./item",
                    ),
                )
            with pytest.raises(ToolError, match="arrow_required"):
                await service.export_records(
                    principal, _export_request(workspace, idempotency_key="gated")
                )

    anyio.run(scenario)


def test_arrow_tools_work_through_the_sdk_server(tmp_path):
    import base64
    import hashlib

    async def scenario():
        async with connected(
            tmp_path, enabled_groups={"exploration", "workspaces", "execution"}
        ) as (client, _services):
            workspace = await create_workspace(client)
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {"path": "schema.xsd", "text": XSD},
                        {"path": "input.xml", "text": xml(3)},
                    ],
                    "expected_revision": None,
                },
            )
            preview = await client.call_tool(
                "pyxsd_preview_records",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "document": {"path": "input.xml"},
                    "element": "root",
                    "path": ["item"],
                    "selector": "./item",
                },
            )
            assert preview.is_error is False
            data = structured(preview)
            assert data["data"]["row_count"] == 3
            assert data["data"]["rows"][0]["amount"] == "1.2300"
            assert "projected" in data["warnings"][0]

            submitted = await client.call_tool(
                "pyxsd_export_records",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "documents": [{"path": "input.xml"}],
                    "element": "root",
                    "path": ["item"],
                    "selector": "./item",
                    "idempotency_key": "sdk-export",
                },
            )
            assert submitted.is_error is False
            job_id = structured(submitted)["data"]["id"]
            with anyio.fail_after(20):
                while True:
                    status = await client.call_tool("pyxsd_job_status", {"job_id": job_id})
                    payload = structured(status)["data"]
                    if payload["state"] == "succeeded":
                        break
                    assert payload["state"] != "failed", payload
                    await anyio.sleep(0.01)
            assert payload["result"] is not None
            export = payload["result"]
            assert export["row_count"] == 3
            entry = next(item for item in export["artifacts"] if item["path"] == "export.parquet")
            downloaded = await client.call_tool(
                "pyxsd_read_artifact",
                {"artifact_id": entry["id"], "offset": 0, "length": entry["size"]},
            )
            parquet_bytes = base64.b64decode(structured(downloaded)["data"]["data"])
            assert parquet_bytes[:4] == b"PAR1"
            assert hashlib.sha256(parquet_bytes).hexdigest() == entry["digest"]

    anyio.run(scenario)


def test_fast_count_overload_reports_incomplete_count(tmp_path):
    from pyxsd.mcp.services.records import PreviewRecordsRequest, RecordService

    async def scenario():
        async with harness(tmp_path) as (_, snapshots, pool, principal, workspace):
            await upload(snapshots, workspace, {"schema.xsd": XSD, "input.xml": xml()})
            service = RecordService(snapshots, pool)
            result = await service.preview_records(
                principal,
                PreviewRecordsRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                    document=FileRef(path="input.xml"),
                    element="root",
                    path=("item",),
                    selector="./item",
                    exact_count=False,
                ),
            )
            assert result.data["count_complete"] is False
            assert result.data["row_count"] == 25

    anyio.run(scenario)


_DEFAULT_DOCUMENT = FileRef(path="input.xml")


def _worker_operation(snapshot, element="root", documents=(_DEFAULT_DOCUMENT,)):
    from pyxsd.mcp.worker_protocol import ExportOperation, RecordsOperation
    from pyxsd.mcp.workers import snapshot_to_wire

    schema = SchemaRef(source=FileRef(path="schema.xsd"))
    records_op = RecordsOperation(
        request_id="records",
        owner_id="owner",
        snapshot=snapshot_to_wire(snapshot),
        schema_ref=schema,
        document=documents[0],
        element=element,
        path=("item",),
    )
    export_op = ExportOperation(
        request_id="export",
        owner_id="owner",
        snapshot=snapshot_to_wire(snapshot),
        schema_ref=schema,
        documents=documents,
        element=element,
        path=("item",),
        output_directory=str(snapshot.root),
    )
    return records_op, export_op


def test_worker_record_and_export_typed_rejections(tmp_path):
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.services.records import execute_export, execute_records

    async def scenario():
        async with harness(tmp_path) as (_, snapshots, _pool, _principal, workspace):
            snapshot = await upload(snapshots, workspace, {"schema.xsd": XSD, "input.xml": xml(1)})
            records_op, export_op = _worker_operation(snapshot, element="missing")
            cache = SchemaCache(snapshots.limits)
            try:
                with pytest.raises(ToolError, match="unsupported_projection"):
                    execute_records(records_op, cache, "test")
                with pytest.raises(ToolError, match="unsupported_projection"):
                    execute_export(export_op, cache, "test")
                from pyxsd.mcp.worker_protocol import ExportOperation
                from pyxsd.mcp.workers import snapshot_to_wire

                no_documents = ExportOperation(
                    request_id="empty",
                    owner_id="owner",
                    snapshot=snapshot_to_wire(snapshot),
                    schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                    documents=(),
                    element="root",
                    path=("item",),
                )
                with pytest.raises(ToolError, match="invalid_argument"):
                    execute_export(no_documents, cache, "test")
                two_documents = no_documents.model_copy(
                    update={"documents": (FileRef(path="input.xml"),) * 2}
                )
                with pytest.raises(ToolError, match="invalid_argument"):
                    execute_export(two_documents, cache, "test")
            finally:
                cache.close()

    anyio.run(scenario)


def test_export_request_validation_and_revision_consistency(tmp_path):
    from pyxsd.mcp.artifacts import ArtifactStore
    from pyxsd.mcp.services.records import RecordService

    async def scenario():
        async with harness(tmp_path) as (_, snapshots, pool, principal, workspace):
            await upload(snapshots, workspace, {"schema.xsd": XSD, "input.xml": xml(1)})
            bare = RecordService(snapshots, pool)
            try:
                with pytest.raises(ToolError, match="invalid_argument"):
                    await bare.export_records(principal, _export_request(workspace))
            finally:
                await bare.jobs.aclose()
            service = RecordService(snapshots, pool, artifacts=ArtifactStore(snapshots.workspaces))
            try:
                with pytest.raises(ToolError, match="invalid_argument"):
                    await service.export_records(
                        principal, _export_request(workspace, documents=())
                    )
                with pytest.raises(ToolError, match="stale_reference"):
                    await service.export_records(
                        principal,
                        _export_request(
                            workspace,
                            documents=(FileRef(path="input.xml", revision="one"),),
                            schema_ref=SchemaRef(source=FileRef(path="schema.xsd", revision="two")),
                        ),
                    )
            finally:
                await service.jobs.aclose()

    anyio.run(scenario)


def _fake_pool(monkeypatch, service, write):
    from pyxsd.mcp.worker_protocol import WorkerReply

    async def run(operation, *, deadline):
        root = __import__("pathlib").Path(operation.output_directory)
        write(root)
        return WorkerReply(
            request_id=operation.request_id,
            role="clean",
            status="ok",
            result={"format": operation.format, "row_count": 0},
        )

    monkeypatch.setattr(service.pool, "run", run)


def test_arrow_preview_tool_without_execution_group(tmp_path):
    from pyxsd.mcp.config import Settings
    from pyxsd.mcp.server import Services, create_server

    async def scenario():
        settings = Settings(state_dir=tmp_path / "state")
        async with Services(settings) as services:
            server = create_server(settings, services)
            tools = await server.list_tools()
            names = {tool.name for tool in tools}
            assert "pyxsd_preview_records" in names
            assert "pyxsd_export_records" not in names
            services.arrow_available = False
            without_arrow = create_server(settings, services)
            names = {tool.name for tool in await without_arrow.list_tools()}
            assert "pyxsd_preview_records" not in names
            assert "pyxsd_export_records" not in names

    anyio.run(scenario)


def test_capture_rejects_unsafe_and_overweight_dataset_output(tmp_path, monkeypatch):
    import os

    from pyxsd.mcp.artifacts import ArtifactStore
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.services.records import RecordService

    async def scenario():
        async with harness(tmp_path, Limits(workers=1, output_bytes=512)) as (
            _,
            snapshots,
            pool,
            _principal,
            workspace,
        ):
            snapshot = await upload(snapshots, workspace, {"schema.xsd": XSD, "input.xml": xml(1)})
            service = RecordService(snapshots, pool, artifacts=ArtifactStore(snapshots.workspaces))
            records_op, export_op = _worker_operation(snapshot)
            with pytest.raises(ToolError, match="invalid_argument"):
                await service.run(records_op, deadline=1.0)
            export_op = export_op.model_copy(update={"format": "dataset"})

            def write_symlink(root):
                base = root / "export-dataset"
                base.mkdir()
                os.symlink("/etc/passwd", base / "link.parquet")

            _fake_pool(monkeypatch, service, write_symlink)
            with pytest.raises(ToolError, match="unsafe_candidate"):
                await service.run(export_op, deadline=1.0)

            def write_oversize(root):
                base = root / "export-dataset"
                base.mkdir()
                (base / "part.parquet").write_bytes(b"x" * 1024)

            _fake_pool(monkeypatch, service, write_oversize)
            with pytest.raises(ToolError, match="output_limit"):
                await service.run(export_op, deadline=1.0)

            def write_growing(root):
                base = root / "export-dataset"
                base.mkdir()
                (base / "part.parquet").write_bytes(b"x" * 8)

            _fake_pool(monkeypatch, service, write_growing)

            class GrowingStream:
                def __init__(self, fd):
                    self.fd = fd

                def read(self, limit):
                    return b"x" * (limit + 1)

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

            import pyxsd.mcp.services.records as records_module

            monkeypatch.setattr(records_module.os, "fdopen", lambda fd, *a, **k: GrowingStream(fd))
            with pytest.raises(ToolError, match="output_limit"):
                await service.run(export_op, deadline=1.0)
            await service.jobs.aclose()

    anyio.run(scenario)
