"""Authenticated binary transfer and ownership over a listening HTTP server."""

import pytest

pytest.importorskip("mcp")
pytest.importorskip("pydantic")
pytest.importorskip("jwt")
pytest.importorskip("uvicorn")

from tests.mcp.http_support import MCPClient, request, serve, token
from tests.mcp.test_exploration import VALID, XSD, _chunks
from tests.mcp.test_http_auth import settings, verifier

from pyxsd.mcp.config import Limits, Settings
from pyxsd.mcp.http import _put_files, create_http_app
from pyxsd.mcp.server import Services
from pyxsd.mcp.workspaces import Principal


def test_binary_upload_validation_download_and_quota(tmp_path):
    config = settings(tmp_path)
    services = Services(config)
    app = create_http_app(config, services, verifier())
    with serve(app) as port:
        client = MCPClient(port, token())
        client.initialize()
        workspace = client.create_workspace()
        schema = request(
            port,
            "PUT",
            f"/files/{workspace}/schema.xsd",
            body=XSD.encode(),
            headers={
                "Authorization": f"Bearer {token()}",
                "Content-Type": "application/octet-stream",
            },
        )
        assert schema.status == 201
        revision = schema.json()["revision"]
        document = request(
            port,
            "PUT",
            f"/files/{workspace}/document.xml",
            body=VALID.encode(),
            headers={
                "Authorization": f"Bearer {token()}",
                "Content-Type": "application/octet-stream",
                "X-PyxSD-Expected-Revision": revision,
            },
        )
        assert document.status == 201
        inspected = client.tool(
            "pyxsd_inspect_schema",
            {"workspace_id": workspace, "schema_ref": {"source": {"path": "schema.xsd"}}},
        )
        assert inspected["result"]["structuredContent"]["data"]["roots"] == ["element:root"]
        result = client.tool(
            "pyxsd_validate_document",
            {
                "workspace_id": workspace,
                "document": {"path": "document.xml"},
                "schema_ref": {"source": {"path": "schema.xsd"}},
            },
        )
        assert result["result"]["structuredContent"]["data"]["valid"] is True
        occurrence = client.tool(
            "pyxsd_query_document",
            {
                "workspace_id": workspace,
                "document": {"path": "document.xml"},
                "schema_ref": {"source": {"path": "schema.xsd"}},
                "xpath": "/root/item",
            },
        )
        location = occurrence["result"]["structuredContent"]["data"]["results"][0]["location"]
        assert location["path"][-1]["index"] == 1
        principal = Principal("http:alice", frozenset({"pyxsd:read", "pyxsd:upload"}))
        import anyio

        async def publish():
            return await services.artifacts.put(
                principal,
                workspace,
                _chunks(b"\x00\xffabc"),
                media_type="application/octet-stream",
                limit=5,
                ttl=60,
            )

        ref = anyio.run(publish)
        denied = request(port, "GET", f"/artifacts/{ref.id}")
        assert denied.status == 401
        foreign = request(
            port,
            "GET",
            f"/artifacts/{ref.id}",
            headers={"Authorization": f"Bearer {token(subject='bob')}"},
        )
        assert foreign.status == 404
        downloaded = request(
            port,
            "GET",
            f"/artifacts/{ref.id}",
            headers={"Authorization": f"Bearer {token()}"},
        )
        assert downloaded.status == 200
        assert downloaded.body == b"\x00\xffabc"
        assert downloaded.headers["x-content-sha256"] == ref.digest
        assert downloaded.headers["content-type"] == "application/octet-stream"
        ranged = request(
            port,
            "GET",
            f"/artifacts/{ref.id}?offset=2&length=2",
            headers={"Authorization": f"Bearer {token()}"},
        )
        assert ranged.status == 200 and ranged.body == b"ab"
        for query in ("offset=-1", "length=0", "length=999999", "offset=bad"):
            invalid = request(
                port,
                "GET",
                f"/artifacts/{ref.id}?{query}",
                headers={"Authorization": f"Bearer {token()}"},
            )
            assert invalid.status == 400
        oversized = request(
            port,
            "PUT",
            f"/files/{workspace}/large.xml",
            body=b"x" * (2 * 2**20 + 1),
            headers={
                "Authorization": f"Bearer {token()}",
                "Content-Type": "application/octet-stream",
            },
        )
        assert oversized.status == 413
        traversal = request(
            port,
            "PUT",
            f"/files/{workspace}/%2Fetc%2Fpasswd",
            body=b"bad",
            headers={
                "Authorization": f"Bearer {token()}",
                "Content-Type": "application/octet-stream",
            },
        )
        assert traversal.status in (400, 404)


def test_stream_quota_is_enforced_before_publication(tmp_path):
    import anyio

    async def scenario():
        services = Services(Settings(state_dir=tmp_path / "state", limits=Limits(request_bytes=1)))
        workspace = await services.workspaces.create(services.principal)

        class Request:
            def __init__(self):
                self.path_params = {"workspace": workspace.id, "path": "large.xml"}

            async def stream(self):
                yield b"12"

        response = await _put_files(Request(), services)
        assert response.status_code == 413
        await services.aclose()

    anyio.run(scenario)
