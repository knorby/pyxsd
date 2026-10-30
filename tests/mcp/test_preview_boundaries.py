"""Preview boundary contracts: serialization, hostile workers, safe IPC."""

import hashlib
import json
import pickle
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_previews import harness, preview

from pyxsd.mcp.contracts import FileRef, InlineFile, SchemaRef
from pyxsd.mcp.errors import ToolError

FORGED_DIGEST = "0" * 64

NOOP = "def transform(document, context):\n    return document\n"

NONDETERMINISTIC = (
    "import os\n"
    "\n"
    "def transform(document, context):\n"
    "    from io import StringIO\n"
    "    return document.schema.parse(\n"
    "        StringIO(f'<root><item>{os.urandom(4).hex()}</item></root>')\n"
    "    )\n"
)

FORGED_HASH = (
    "import json as _json\n"
    "import pyxsd.mcp.execution_main as _em\n"
    "\n"
    "_forged = {\n"
    f'    "digest": "{FORGED_DIGEST}",\n'
    '    "checkpoints": [],\n'
    "}\n"
    "_frame = (\n"
    "    _json.dumps(\n"
    '        {"request_id": "execution", "role": "execution", "status": "ok", "result": _forged},\n'
    "        sort_keys=True,\n"
    "    )\n"
    '    + "\\n"\n'
    ").encode()\n"
    "_em.encode_frame = lambda message, max_bytes=0: _frame\n"
    "\n"
    "def transform(document, context):\n"
    "    from io import StringIO\n"
    '    return document.schema.parse(StringIO("<root><item>7</item></root>"))\n'
)

SMUGGLED_FRAME = (
    "import sys\n"
    "\n"
    "def transform(document, context):\n"
    "    sys.stdout.buffer.write(\n"
    '        b\'{"request_id":"execution","role":"execution","status":"ok","result":null}\\n\'\n'
    "    )\n"
    "    sys.stdout.buffer.flush()\n"
    "    return document\n"
)


def test_noop_transform_diff_includes_serialization_only_changes(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            await service.inputs.put_files(
                workspace,
                [InlineFile(path="valid.xml", text="<root><item>42</item></root>")],
            )
            request = _request(workspace, "valid.xml", NOOP)
            record = await service.preview(workspace, request)
            assert record.valid is True
            candidate = await service.outputs.read(record.candidate.id, offset=0, length=8000)
            assert candidate != b"<root><item>42</item></root>"
            assert record.candidate.digest == hashlib.sha256(candidate).hexdigest()
            diff = await service.outputs.read(record.diff.id, offset=0, length=24000)
            assert diff, "serialization-only changes must appear in the byte diff"
            assert any("serialization" in warning.lower() for warning in record.warnings)
            assert any("comment" in warning.lower() for warning in record.warnings)

    anyio.run(scenario)


def test_nondeterministic_transforms_retain_independent_candidates(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            one = await preview(service, workspace, NONDETERMINISTIC)
            two = await preview(service, workspace, NONDETERMINISTIC)
            assert one.candidate.digest != two.candidate.digest
            for record in (one, two):
                content = await service.outputs.read(record.candidate.id, offset=0, length=8000)
                assert record.candidate.digest == hashlib.sha256(content).hexdigest()

    anyio.run(scenario)


def test_forged_worker_hash_claim_is_ignored(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            record = await preview(service, workspace, FORGED_HASH)
            content = await service.outputs.read(record.candidate.id, offset=0, length=8000)
            assert b"<item>7</item>" in content
            assert record.candidate.digest == hashlib.sha256(content).hexdigest()
            assert record.candidate.digest != FORGED_DIGEST
            assert FORGED_DIGEST not in json.dumps(record.model_dump(mode="json"))

    anyio.run(scenario)


def test_smuggled_stdout_frame_is_not_a_second_result(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, workspace):
            with pytest.raises(ToolError, match="ipc_protocol"):
                await preview(service, workspace, SMUGGLED_FRAME)

    anyio.run(scenario)


def _request(workspace, document, source):
    from pyxsd.mcp.previews import PreviewTransformRequest

    return PreviewTransformRequest(
        workspace_id=workspace,
        document=FileRef(path=document),
        source_schema=SchemaRef(source=FileRef(path="schema.xsd")),
        transform={"kind": "python", "source": source},
    )


def test_ipc_decode_refuses_executable_deserialization(tmp_path):
    import pyxsd.mcp.worker_protocol as protocol
    from pyxsd.mcp.worker_protocol import WorkerReply, decode_frame

    sentinel = Path(tmp_path) / "pwned"

    class Exploit:
        def __reduce__(self):
            return (Path.write_text, (sentinel, "pwned"))

    payload = pickle.dumps(Exploit())
    with pytest.raises(ToolError, match="ipc_"):
        decode_frame(
            payload,
            WorkerReply,
            max_bytes=65536,
            expected={"request_id": "execution", "role": "execution"},
        )
    assert not sentinel.exists()

    forged = json.dumps(
        {"request_id": "execution", "role": "execution", "status": "ok", "result": None}
    ).encode()
    with pytest.raises(ToolError, match="ipc_protocol"):
        decode_frame(
            forged + forged + b"\n",
            WorkerReply,
            max_bytes=65536,
            expected={"request_id": "execution", "role": "execution"},
        )
    assert protocol is not None
