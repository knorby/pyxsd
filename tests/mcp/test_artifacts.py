"""Persistent outputs are listed, read and deleted without a workspace."""

import base64

import pytest

pytest.importorskip("mcp")
pytest.importorskip("pydantic")
pytest.importorskip("anyio")

import anyio
from tests.mcp.test_server import connected, create_workspace, structured, upload

from pyxsd.mcp.errors import ToolError


def test_artifact_tools_list_read_delete_and_paginate(tmp_path):
    async def scenario():
        async with connected(tmp_path) as (client, services):
            first = await services.outputs.put_bytes(
                b"alpha", name="alpha.txt", media_type="text/plain", kind="result"
            )
            second = await services.outputs.put_bytes(
                b"beta", name="beta.txt", media_type="text/plain", kind="result"
            )
            listed = structured(await client.call_tool("pyxsd_list_artifacts", {"limit": 1}))
            assert [item["id"] for item in listed["data"]["artifacts"]] == [first.id]
            cursor = listed["page"]["next_cursor"]
            assert cursor
            assert "next_cursor" not in listed["data"]
            rest = structured(
                await client.call_tool("pyxsd_list_artifacts", {"limit": 1, "cursor": cursor})
            )
            assert [item["id"] for item in rest["data"]["artifacts"]] == [second.id]
            assert rest["page"]["next_cursor"] is None
            with pytest.raises(ToolError, match="invalid_cursor"):
                from pyxsd.mcp.tools import _decode_after

                _decode_after("")
            with pytest.raises(ToolError, match="invalid_cursor"):
                from pyxsd.mcp.tools import _decode_after

                _decode_after("!")
            with pytest.raises(ToolError, match="invalid_cursor"):
                from pyxsd.mcp.tools import _decode_after

                _decode_after("e30")

            read = structured(
                await client.call_tool(
                    "pyxsd_read_artifact", {"artifact_id": first.id, "length": 5}
                )
            )
            assert base64.b64decode(read["data"]["data"]) == b"alpha"
            assert read["data"]["path"].endswith("alpha.txt")
            removed = structured(
                await client.call_tool("pyxsd_delete_artifact", {"artifact_id": first.id})
            )
            assert removed["data"]["deleted"] == first.id
            again = structured(
                await client.call_tool("pyxsd_delete_artifact", {"artifact_id": first.id})
            )
            assert again["data"]["deleted"] == first.id
            failed = await client.call_tool("pyxsd_read_artifact", {"artifact_id": first.id})
            assert failed.is_error is True

    anyio.run(scenario)


def test_artifact_read_verifies_content_once(tmp_path, monkeypatch):
    async def scenario():
        async with connected(tmp_path) as (client, services):
            reference = await services.outputs.put_bytes(
                b"alpha", name="alpha.txt", media_type="text/plain", kind="result"
            )
            calls = 0
            original = services.outputs._verified

            def counted(row, descriptor, *, offset, length):
                nonlocal calls
                calls += 1
                return original(row, descriptor, offset=offset, length=length)

            monkeypatch.setattr(services.outputs, "_verified", counted)
            response = structured(
                await client.call_tool(
                    "pyxsd_read_artifact", {"artifact_id": reference.id, "length": 5}
                )
            )
            assert base64.b64decode(response["data"]["data"]) == b"alpha"
            assert calls == 1

    anyio.run(scenario)


def test_artifact_validation_errors_are_structured(tmp_path):
    async def scenario():
        async with connected(tmp_path) as (client, _services):
            workspace = await create_workspace(client)
            await upload(client, workspace)
            invalid_cursor = await client.call_tool(
                "pyxsd_list_artifacts", {"limit": 50, "cursor": "not base64!"}
            )
            assert invalid_cursor.is_error is True
            assert structured(invalid_cursor)["data"]["fault"]["code"] == "invalid_cursor"
            invalid_limit = await client.call_tool("pyxsd_list_artifacts", {"limit": 51})
            assert invalid_limit.is_error is True
            assert structured(invalid_limit)["data"]["fault"]["code"] == "invalid_argument"

    anyio.run(scenario)


def test_read_artifact_never_spills_and_fails_with_response_limit(tmp_path):
    from pyxsd.mcp.config import Limits

    async def scenario():
        async with connected(tmp_path, limits=Limits(result_bytes=4 * 2**10)) as (
            client,
            services,
        ):
            reference = await services.outputs.put_bytes(
                b"g" * 8192, name="big.bin", media_type="application/octet-stream", kind="result"
            )
            # A range that fits the budget returns bytes without spilling.
            fitted = structured(
                await client.call_tool(
                    "pyxsd_read_artifact", {"artifact_id": reference.id, "length": 640}
                )
            )
            assert base64.b64decode(fitted["data"]["data"]) == b"g" * 640
            before, _ = await services.outputs.list()
            assert len(before) == 1
            # An oversized range faults instead of spilling into another artifact.
            rejected = structured(
                await client.call_tool(
                    "pyxsd_read_artifact", {"artifact_id": reference.id, "length": 8192}
                )
            )
            assert rejected["status"] == "error"
            assert rejected["data"]["fault"]["code"] == "response_limit"
            after, _ = await services.outputs.list()
            assert [ref.id for ref in after] == [reference.id]

    anyio.run(scenario)


def test_artifact_range_length_reports_clamping(tmp_path):
    async def scenario():
        async with connected(tmp_path) as (client, services):
            reference = await services.outputs.put_bytes(
                b"r" * 8192, name="range.bin", media_type="application/octet-stream", kind="result"
            )
            result = structured(
                await client.call_tool(
                    "pyxsd_read_artifact",
                    {"artifact_id": reference.id, "offset": 0, "length": 8193},
                )
            )
            assert result["data"]["length"] == 8192
            assert result["data"]["size"] == 8192
            assert base64.b64decode(result["data"]["data"]) == b"r" * 8192

    anyio.run(scenario)


def test_spilled_list_keeps_cursor_in_envelope_page(tmp_path):
    from pyxsd.mcp.config import Limits

    async def scenario():
        async with connected(tmp_path, limits=Limits(result_bytes=2 * 2**10)) as (
            client,
            services,
        ):
            refs = [
                await services.outputs.put_bytes(
                    str(index).encode(),
                    name=f"{index}.txt",
                    media_type="text/plain",
                    kind="result",
                )
                for index in range(6)
            ]
            # Five entries exceed the budget even with short Linux temp paths.
            first = structured(await client.call_tool("pyxsd_list_artifacts", {"limit": 5}))
            assert "summary" in first["data"]
            assert "next_cursor" not in first["data"]
            cursor = first["page"]["next_cursor"]
            assert cursor
            assert first["page"]["returned"] == 5
            assert first["page"]["truncated"] is True
            second = structured(
                await client.call_tool("pyxsd_list_artifacts", {"limit": 2, "cursor": cursor})
            )
            # The spill result itself is also a retained artifact and follows
            # the original third entry in the second page.
            assert second["data"]["artifacts"][0]["id"] == refs[5].id
            assert len(second["data"]["artifacts"]) == 2
            assert second["page"]["next_cursor"] is None
            assert "next_cursor" not in second["data"]

    anyio.run(scenario)
