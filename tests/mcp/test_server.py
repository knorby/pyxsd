"""The SDK adapter exposes strict tools, valid envelopes, resources and prompts."""

import base64
import json
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("mcp")
pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from mcp.shared.memory import create_client_server_memory_streams
from tests.mcp.test_exploration import INVALID, VALID, XSD, _chunks
from tests.mcp.test_workers import parse_operation

from mcp import ClientSession
from pyxsd.mcp.config import Settings


@asynccontextmanager
async def connected(tmp_path, **settings_kwargs):
    from pyxsd.mcp.server import Services, create_server

    settings = Settings(state_dir=tmp_path / "state", **settings_kwargs)
    services = Services(settings)
    server = create_server(settings, services)
    try:
        async with (
            create_client_server_memory_streams() as (client_streams, server_streams),
            ClientSession(*client_streams) as client,
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
            )
            await client.initialize()
            try:
                yield client, services
            finally:
                tasks.cancel_scope.cancel()
    finally:
        await services.aclose()


def structured(result):
    assert result.structured_content is not None
    text = next(item for item in result.content if item.type == "text")
    assert json.loads(text.text) == result.structured_content
    return result.structured_content


async def create_workspace(client):
    result = await client.call_tool(
        "pyxsd_manage_workspace", {"action": "create", "idempotency_key": "one"}
    )
    assert result.is_error is False
    return structured(result)["data"]["id"]


async def upload(client, workspace, *, valid=True):
    result = await client.call_tool(
        "pyxsd_put_files",
        {
            "workspace_id": workspace,
            "files": [
                {"path": "schema.xsd", "text": XSD},
                {"path": "document.xml", "text": VALID if valid else INVALID},
            ],
            "expected_revision": None,
        },
    )
    assert result.is_error is False
    return structured(result)["data"]["revision"]


def test_catalog_schema_tools_resources_and_prompts(tmp_path):
    async def scenario():
        async with connected(tmp_path) as (client, services):
            capabilities_result = await client.call_tool("pyxsd_capabilities", {})
            assert structured(capabilities_result)["data"]["version"]
            assert str(tmp_path) not in json.dumps(structured(capabilities_result))

            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {
                "pyxsd_capabilities",
                "pyxsd_inspect_schema",
                "pyxsd_find_schema_components",
                "pyxsd_validate_document",
                "pyxsd_query_document",
                "pyxsd_explain_issue",
                "pyxsd_manage_workspace",
                "pyxsd_put_files",
                "pyxsd_read_artifact",
            } <= names
            for tool in tools.tools:
                assert tool.input_schema["additionalProperties"] is False
                assert tool.output_schema["additionalProperties"] is False
            validate = next(tool for tool in tools.tools if tool.name == "pyxsd_validate_document")
            assert validate.annotations.read_only_hint is True
            assert validate.annotations.open_world_hint is False

            unknown = await client.call_tool("pyxsd_capabilities", {"unexpected": 1})
            assert unknown.is_error is True

            workspace = await create_workspace(client)
            revision = await upload(client, workspace)
            inspected = await client.call_tool(
                "pyxsd_inspect_schema",
                {"workspace_id": workspace, "schema_ref": {"source": {"path": "schema.xsd"}}},
            )
            assert structured(inspected)["data"]["roots"] == ["element:root"]
            found = await client.call_tool(
                "pyxsd_find_schema_components",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "kind": "type",
                    "text": "item",
                },
            )
            assert structured(found)["data"]["components"][0]["id"] == "type:Item"
            explained = await client.call_tool("pyxsd_explain_issue", {"code": "value"})
            assert structured(explained)["data"]["known"] is True
            validated = await client.call_tool(
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "document.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            assert structured(validated)["data"]["valid"] is True
            queried = await client.call_tool(
                "pyxsd_query_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "document.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "xpath": "/root/item",
                },
            )
            assert structured(queried)["data"]["results"][0]["location"]["path"][-1]["index"] == 1

            invalid_call = await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [{"path": "bad.xml", "text": "<root>"}],
                    "expected_revision": "missing",
                },
            )
            assert invalid_call.is_error is True
            error = structured(invalid_call)
            assert error["status"] == "error"
            assert error["data"]["fault"]["code"] == "stale_reference"

            published = await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [{"path": "bad.xml", "text": "<root>"}],
                    "expected_revision": revision,
                },
            )
            assert published.is_error is False
            diagnosis = await client.call_tool(
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "bad.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            assert diagnosis.is_error is True
            assert structured(diagnosis)["data"]["fault"]["code"] == "invalid_xml"

            resources = await client.list_resources()
            assert "pyxsd://issue-codes" in {str(item.uri) for item in resources.resources}
            codes = await client.read_resource("pyxsd://issue-codes")
            assert "missing-attribute" in codes.contents[0].text
            capabilities = await client.read_resource("pyxsd://capabilities")
            assert str(tmp_path) not in capabilities.contents[0].text

            ref = await services.artifacts.put(
                services.principal,
                workspace,
                _chunks(b"abc"),
                media_type="application/octet-stream",
                limit=3,
                ttl=60,
            )
            artifact = await client.read_resource(f"pyxsd://artifacts/{ref.id}")
            assert base64.b64decode(artifact.contents[0].blob) == b"abc"
            downloaded = await client.call_tool(
                "pyxsd_read_artifact", {"artifact_id": ref.id, "offset": 1, "length": 2}
            )
            assert base64.b64decode(structured(downloaded)["data"]["data"]) == b"bc"
            missing = await client.call_tool("pyxsd_read_artifact", {"artifact_id": "missing"})
            assert missing.is_error is True
            assert structured(missing)["data"]["fault"]["code"] == "not_found"
            prompts = await client.list_prompts()
            assert "pyxsd_diagnose_xml" in {item.name for item in prompts.prompts}
            prompt = await client.get_prompt(
                "pyxsd_diagnose_xml",
                {"schema_ref": "schema.xsd", "document_ref": "document.xml"},
            )
            assert "schema.xsd" in prompt.messages[0].content.text
            for name, arguments in (
                ("pyxsd_author_pipeline", {"goal": "normalize units"}),
                ("pyxsd_evaluate_migration", {"old_schema": "old.xsd", "new_schema": "new.xsd"}),
            ):
                guided = await client.get_prompt(name, arguments)
                assert guided.messages[0].content.text
            listed = await client.call_tool("pyxsd_manage_workspace", {"action": "list"})
            assert listed.is_error is False
            invalid_release = await client.call_tool(
                "pyxsd_manage_workspace", {"action": "release"}
            )
            assert invalid_release.is_error is True
            released = await client.call_tool(
                "pyxsd_manage_workspace", {"action": "release", "workspace_id": workspace}
            )
            assert structured(released)["data"]["released"] == workspace

    anyio.run(scenario)


def test_large_results_spill_and_download_as_exact_json(tmp_path):
    text = "x" * 40_000
    schema = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="root" type="xs:string"/></xs:schema>'

    async def scenario():
        async with connected(tmp_path) as (client, _services):
            workspace = await create_workspace(client)
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {"path": "text.xsd", "text": schema},
                        {"path": "text.xml", "text": f"<root>{text}</root>"},
                    ],
                    "expected_revision": None,
                },
            )
            result = await client.call_tool(
                "pyxsd_query_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "text.xml"},
                    "schema_ref": {"source": {"path": "text.xsd"}},
                    "xpath": "string(/root)",
                },
            )
            summary = structured(result)
            assert summary["data"]["bytes"] > 40_000
            ref = summary["artifacts"][0]
            chunks = []
            for offset in range(0, ref["size"], 8 * 1024):
                downloaded = await client.call_tool(
                    "pyxsd_read_artifact",
                    {"artifact_id": ref["id"], "offset": offset},
                )
                assert downloaded.is_error is False
                chunks.append(base64.b64decode(structured(downloaded)["data"]["data"]))
            original = json.loads(b"".join(chunks))
            assert original["data"]["results"] == text

    anyio.run(scenario)


def test_services_configuration_source_grants_and_shutdown(tmp_path, monkeypatch):
    from pyxsd.mcp import server as server_module
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.errors import ToolError
    from pyxsd.mcp.workspaces import Principal

    http_services = server_module.Services(
        Settings(state_dir=tmp_path / "http", transport="streamable-http")
    )
    with pytest.raises(ToolError, match="unsupported_transport"):
        server_module.run(http_services.settings)
    anyio.run(http_services.aclose)
    with pytest.raises(ToolError, match="invalid_argument"):
        server_module.Services(Settings(state_dir=tmp_path / "groups", enabled_groups={"unknown"}))
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(state_dir=tmp_path / "execute", execution_backend="isolated")
    with pytest.raises(ToolError, match="execution_unavailable"):
        server_module.Services(
            Settings(
                state_dir=tmp_path / "untrusted-http",
                execution_backend="trusted-local",
                transport="streamable-http",
            )
        )

    async def scenario():
        settings = Settings(
            state_dir=tmp_path / "state",
            source_roots={"stdio": tmp_path},
            limits=Limits(result_bytes=1),
        )
        async with server_module.Services(settings) as services:
            assert services.snapshots._source_root(services.principal, "workspace") == tmp_path
            with pytest.raises(ToolError, match="capture_unavailable"):
                services.snapshots._source_root(Principal("other", frozenset()), "workspace")
            with pytest.raises(ToolError, match="invalid_argument"):
                server_module.create_server(Settings(state_dir=tmp_path / "other"), services)
        assert services.state._closed

        small = Settings(state_dir=tmp_path / "small", limits=Limits(result_bytes=1))
        services = server_module.Services(small)
        client_server = server_module.create_server(small, services)

        class FakeServer:
            async def run_stdio_async(self):
                return None

        monkeypatch.setattr(server_module, "Services", lambda _settings: services)
        monkeypatch.setattr(server_module, "create_server", lambda *_args: FakeServer())
        await server_module._run(small)
        assert services.state._closed

        called = {}
        monkeypatch.setattr(
            server_module.anyio,
            "run",
            lambda function, value: called.update(function=function, value=value),
        )
        server_module.run(small)
        assert called == {"function": server_module._run, "value": small}
        assert client_server is not None

    anyio.run(scenario)


def test_response_limit_is_a_structured_fault(tmp_path):
    from pyxsd.mcp.config import Limits

    async def scenario():
        async with connected(tmp_path, limits=Limits(result_bytes=1)) as (client, _services):
            workspace = await create_workspace(client)
            result = await client.call_tool(
                "pyxsd_put_files",
                {"workspace_id": workspace, "files": [], "expected_revision": None},
            )
            assert result.is_error is True
            assert structured(result)["data"]["fault"]["code"] == "response_limit"

    anyio.run(scenario)


def test_job_tools_return_durable_results_and_errors(tmp_path):
    from pyxsd.mcp import workers as worker_module

    async def scenario():
        async with connected(tmp_path, enabled_groups={"execution", "workspaces"}) as (
            client,
            services,
        ):
            workspace = await create_workspace(client)
            await upload(client, workspace)
            snapshot = await services.snapshots.get(services.principal, workspace)
            ref = await services.jobs.submit(
                services.principal,
                workspace,
                parse_operation(worker_module, snapshot),
                idempotency_key="sdk-job",
            )
            with anyio.fail_after(5):
                while True:
                    result = await client.call_tool("pyxsd_job_status", {"job_id": ref.id})
                    data = structured(result)["data"]
                    if data["state"] == "succeeded":
                        break
                    await anyio.sleep(0.01)
            assert data["result"]["root_present"] is True
            repeated = await client.call_tool("pyxsd_cancel_job", {"job_id": ref.id})
            assert structured(repeated)["data"]["state"] == "succeeded"
            missing = await client.call_tool("pyxsd_job_status", {"job_id": "missing"})
            assert missing.is_error is True
            assert structured(missing)["data"]["fault"]["code"] == "not_found"
            missing_cancel = await client.call_tool("pyxsd_cancel_job", {"job_id": "missing"})
            assert missing_cancel.is_error is True
            assert structured(missing_cancel)["data"]["fault"]["code"] == "not_found"

    anyio.run(scenario)


def test_tool_groups_are_startup_configuration(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups={"exploration"}) as (client, _services):
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "pyxsd_inspect_schema" in names
            assert "pyxsd_manage_workspace" not in names
            assert "pyxsd_put_files" not in names
            assert "pyxsd_read_artifact" not in names

        async with connected(tmp_path, enabled_groups={"workspaces"}) as (client, _services):
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "pyxsd_manage_workspace" in names
            assert "pyxsd_inspect_schema" not in names

    anyio.run(scenario)
