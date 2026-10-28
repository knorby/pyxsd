"""Schema comparison separates structural impact from observed corpus evidence."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, upload

from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.errors import ToolError

OLD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="Root"/>
<xs:element name="legacy" type="xs:string"/>
<xs:complexType name="Root"><xs:sequence>
<xs:element ref="legacy" minOccurs="0"/>
<xs:element name="item" type="Item" maxOccurs="unbounded"/>
</xs:sequence></xs:complexType>
<xs:complexType name="Item"><xs:sequence>
<xs:element name="amount" type="Amount"/>
</xs:sequence></xs:complexType>
<xs:simpleType name="Amount">
<xs:restriction base="xs:decimal"><xs:totalDigits value="8"/></xs:restriction>
</xs:simpleType>
</xs:schema>"""

NEW = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="Root"/>
<xs:element name="added" type="xs:string"/>
<xs:complexType name="Root"><xs:sequence>
<xs:element name="item" type="Item" maxOccurs="unbounded"/>
<xs:element name="fresh" type="xs:string" minOccurs="0"/>
</xs:sequence></xs:complexType>
<xs:complexType name="Item"><xs:sequence>
<xs:element name="amount" type="Amount"/>
</xs:sequence></xs:complexType>
<xs:simpleType name="Amount">
<xs:restriction base="xs:decimal"><xs:totalDigits value="6"/></xs:restriction>
</xs:simpleType>
</xs:schema>"""

OLD_ANON = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="anon"><xs:complexType><xs:sequence>
<xs:element name="a" type="xs:int"/>
</xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

NEW_ANON = OLD_ANON.replace('type="xs:int"', 'type="xs:string"')

OLD_NS = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
    'targetNamespace="http://old.example" xmlns:o="http://old.example">'
    '<xs:element name="root" type="o:Root"/>'
    '<xs:complexType name="Root"><xs:sequence/></xs:complexType>'
    "</xs:schema>"
)
NEW_NS = OLD_NS.replace("old.example", "new.example")

GOOD = "<root><item><amount>123.45</amount></item></root>"
REGRESSED = "<root><item><amount>12345678</amount></item></root>"
RESOLVED = "<root><item><amount>123.45</amount></item><fresh>x</fresh></root>"
INVALID_BOTH = "<root><item><amount>abc</amount></item></root>"
MALFORMED = "<root><item>"
UNBINDABLE = "<other/>"


def _compare_request(workspace, **overrides):
    from pyxsd.mcp.services.comparison import CompareSchemasRequest

    fields = {
        "workspace_id": workspace,
        "old_schema": SchemaRef(source=FileRef(path="old.xsd")),
        "new_schema": SchemaRef(source=FileRef(path="new.xsd")),
    }
    fields.update(overrides)
    return CompareSchemasRequest(**fields)


def test_structural_diff_added_removed_changed_and_deterministic(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": OLD, "new.xsd": NEW})
            service = ComparisonService(inputs, pool)
            result = await service.compare(_compare_request(workspace))
            structural = result["structural"]
            assert "element:added" in structural["added"]
            assert "element:legacy" in structural["removed"]
            # Local declarations use content-addressed element@<hash> ids and are
            # re-identified when their container changes: conservative reporting.
            assert any(str(item).startswith("element@") for item in structural["added"])
            assert any(str(item).startswith("element@") for item in structural["removed"])
            assert {"type:Root", "type:Amount"} <= set(structural["changed"])
            # Descriptions embed member particles and facets, so containing
            # components are flagged too: conservative potential-impact reporting.
            assert set(structural["changed"]) == {
                "element:root",
                "type:Root",
                "type:Item",
                "type:Amount",
            }
            assert structural["namespace_changed"] is False
            assert structural["totals"] == {"added": 4, "removed": 4, "changed": 4}
            details = {entry["id"]: entry["fields"] for entry in structural["changed_details"]}
            assert "facets" in details["type:Amount"]
            repeat = await service.compare(_compare_request(workspace))
            assert repeat["structural"] == structural

    anyio.run(scenario)


def test_anonymous_declarations_diff_by_content_addressed_identity(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": OLD_ANON, "new.xsd": NEW_ANON})
            service = ComparisonService(inputs, pool)
            result = await service.compare(
                _compare_request(
                    workspace,
                    old_schema=SchemaRef(source=FileRef(path="old.xsd")),
                    new_schema=SchemaRef(source=FileRef(path="new.xsd")),
                ),
            )
            structural = result["structural"]
            assert any(identifier.startswith("type@") for identifier in structural["removed"])
            assert any(identifier.startswith("type@") for identifier in structural["added"])

    anyio.run(scenario)


def test_namespace_change_reports_flag_and_reidentified_components(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": OLD_NS, "new.xsd": NEW_NS})
            service = ComparisonService(inputs, pool)
            result = await service.compare(
                _compare_request(
                    workspace,
                    old_schema=SchemaRef(source=FileRef(path="old.xsd")),
                    new_schema=SchemaRef(source=FileRef(path="new.xsd")),
                ),
            )
            structural = result["structural"]
            assert structural["namespace_changed"] is True
            assert "element:{http://new.example}root" in structural["added"]
            assert "element:{http://old.example}root" in structural["removed"]

    anyio.run(scenario)


def test_corpus_classifies_every_file_and_separates_impact_from_evidence(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "old.xsd": OLD,
                    "new.xsd": NEW,
                    "good.xml": GOOD,
                    "regressed.xml": REGRESSED,
                    "resolved.xml": RESOLVED,
                    "invalid.xml": INVALID_BOTH,
                    "malformed.xml": MALFORMED,
                    "unbindable.xml": UNBINDABLE,
                },
            )
            service = ComparisonService(inputs, pool)
            corpus = tuple(
                FileRef(path=name)
                for name in (
                    "good.xml",
                    "regressed.xml",
                    "resolved.xml",
                    "invalid.xml",
                    "malformed.xml",
                    "unbindable.xml",
                )
            )
            result = await service.compare(_compare_request(workspace, corpus=corpus))
            corpus_result = result["corpus"]
            statuses = {entry["path"]: entry["status"] for entry in corpus_result["files"]}
            assert statuses == {
                "good.xml": "valid",
                "regressed.xml": "regressed",
                "resolved.xml": "resolved",
                "invalid.xml": "invalid",
                "malformed.xml": "malformed",
                "unbindable.xml": "unbindable",
            }
            assert corpus_result["totals"] == {
                "valid": 1,
                "regressed": 1,
                "resolved": 1,
                "invalid": 1,
                "malformed": 1,
                "unbindable": 1,
                "skipped": 0,
            }
            assert corpus_result["regressions"] == ["regressed.xml"]
            assert corpus_result["resolutions"] == ["resolved.xml"]
            conclusions = result["conclusions"]
            assert conclusions["corpus_only"] is True
            assert "No universal compatibility" in conclusions["statement"]
            assert result["structural"]["totals"]["added"] == 4

    anyio.run(scenario)


def test_corpus_cap_is_enforced(tmp_path):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path, Limits(workers=1, corpus_files=2)) as (
            _,
            inputs,
            pool,
            workspace,
        ):
            await upload(
                inputs,
                workspace,
                {
                    "old.xsd": OLD,
                    "new.xsd": NEW,
                    "a.xml": GOOD,
                    "b.xml": GOOD,
                    "c.xml": GOOD,
                },
            )
            service = ComparisonService(inputs, pool)
            with pytest.raises(ToolError, match="quota_exceeded"):
                await service.compare(
                    _compare_request(
                        workspace,
                        corpus=tuple(FileRef(path=name) for name in ("a.xml", "b.xml", "c.xml")),
                    ),
                )

    anyio.run(scenario)


def test_corpus_timeout_skips_remaining_files(tmp_path):
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.services import comparison as comparison_module
    from pyxsd.mcp.worker_protocol import ComparisonOperation
    from pyxsd.mcp.workers import capture_to_wire

    async def scenario():
        async with harness(tmp_path, Limits(workers=1, corpus_file_seconds=1)) as (
            _,
            inputs,
            _pool,
            workspace,
        ):
            await upload(
                inputs,
                workspace,
                {
                    "old.xsd": OLD,
                    "new.xsd": NEW,
                    "good.xml": GOOD,
                },
            )
            async with inputs.capture(workspace) as capture:
                operation = ComparisonOperation(
                    request_id="timeout",
                    capture=capture_to_wire(capture),
                    old_schema=SchemaRef(source=FileRef(path="old.xsd")),
                    new_schema=SchemaRef(source=FileRef(path="new.xsd")),
                    corpus=(FileRef(path="good.xml"),),
                )
                cache = SchemaCache(inputs.limits)
                try:
                    ticks = iter([0.0, 100.0, 100.0, 100.0])
                    result = comparison_module.execute_comparison(
                        operation, cache, "test", clock=lambda: next(ticks)
                    )
                finally:
                    cache.close()
            assert result["corpus"]["files"] == [
                {"path": "good.xml", "status": "skipped", "old": "skipped", "new": "skipped"}
            ]
            assert result["corpus"]["totals"]["skipped"] == 1

    anyio.run(scenario)


def test_comparison_rejects_worker_faults_and_non_object_results(tmp_path, monkeypatch):
    from pyxsd.mcp.contracts import ToolFault
    from pyxsd.mcp.services.comparison import ComparisonService
    from pyxsd.mcp.worker_protocol import WorkerReply

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": OLD, "new.xsd": NEW})
            service = ComparisonService(inputs, pool)

            async def failed(operation, *, deadline):
                return WorkerReply(
                    request_id=operation.request_id,
                    role="clean",
                    status="error",
                    fault=ToolFault(code="worker_died", message="No verdict"),
                )

            monkeypatch.setattr(service.pool, "run", failed)
            with pytest.raises(ToolError, match="worker_died"):
                await service.compare(_compare_request(workspace))

            async def not_an_object(operation, *, deadline):
                return WorkerReply(
                    request_id=operation.request_id, role="clean", status="ok", result=[]
                )

            monkeypatch.setattr(service.pool, "run", not_an_object)
            with pytest.raises(ToolError, match="ipc_protocol"):
                await service.compare(_compare_request(workspace))

    anyio.run(scenario)


def test_compare_tool_works_through_the_sdk_server(tmp_path):
    from tests.mcp.test_server import connected, create_workspace, structured

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
                        {"path": "old.xsd", "text": OLD},
                        {"path": "new.xsd", "text": NEW},
                        {"path": "good.xml", "text": GOOD},
                    ],
                },
            )
            submitted = await client.call_tool(
                "pyxsd_compare_schemas",
                {
                    "workspace_id": workspace,
                    "old_schema": {"source": {"path": "old.xsd"}},
                    "new_schema": {"source": {"path": "new.xsd"}},
                    "corpus": [{"path": "good.xml"}],
                },
            )
            result = structured(submitted)["data"]
            assert "element:added" in result["structural"]["added"]
            assert result["corpus"]["files"][0]["status"] == "valid"
            assert result["conclusions"]["corpus_only"] is True

    anyio.run(scenario)
