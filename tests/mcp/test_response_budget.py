"""Budget both MCP result representations, not just the JSON data field."""

import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")

import anyio

from pyxsd.mcp import responses
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import ToolEnvelope
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.output_store import OutputStore
from pyxsd.mcp.storage import Storage


def _store(tmp_path, limits=None):
    storage = Storage(tmp_path / "state", persist=False)
    return storage, OutputStore(storage, limits or Limits())


@pytest.mark.parametrize(
    "envelope",
    [
        ToolEnvelope(data="x" * 25000),
        ToolEnvelope(data="x" * 13000),
        ToolEnvelope(data={"stub": "unique-body-marker" * 1500}),
        ToolEnvelope(warnings=["é" * 14000]),
    ],
)
def test_large_results_spill_once_as_valid_json(tmp_path, envelope):
    async def scenario():
        storage, store = _store(tmp_path)
        try:
            result = await responses.render_result(envelope, store)
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            assert len(encoded) <= store.limits.result_bytes
            structured = result["structuredContent"]
            assert json.loads(result["content"][0]["text"]) == structured
            ref = structured["artifacts"][0]
            parts = []
            for offset in range(0, ref["size"], 24576):
                parts.append(await store.read(ref["id"], offset=offset, length=24576))
            assert json.loads(b"".join(parts)) == envelope.model_dump(mode="json")
            assert "unique-body-marker" not in result["content"][0]["text"]
        finally:
            store.close()
            storage.close()

    anyio.run(scenario)


def test_small_result_does_not_create_artifacts(tmp_path):
    async def scenario():
        storage, store = _store(tmp_path)
        try:
            envelope = ToolEnvelope(data={"hello": "world"})
            result = await responses.render_result(envelope, store)
            assert result["structuredContent"] == envelope.model_dump(mode="json")
            assert await store.list() == ([], None)
        finally:
            store.close()
            storage.close()

    anyio.run(scenario)


@pytest.mark.parametrize(
    "limits,envelope,code",
    [
        (Limits(result_bytes=1), ToolEnvelope(data="large"), "response_limit"),
        (Limits(output_bytes=1), ToolEnvelope(data="x" * 25000), "quota_exceeded"),
        (Limits(), ToolEnvelope(data="\ud800"), "unsupported_value"),
    ],
)
def test_unrepresentable_results_fail_before_retaining_bytes(tmp_path, limits, envelope, code):
    async def scenario():
        storage, store = _store(tmp_path, limits)
        try:
            with pytest.raises(ToolError, match=code):
                await responses.render_result(envelope, store)
            assert await store.list() == ([], None)
        finally:
            store.close()
            storage.close()

    anyio.run(scenario)
