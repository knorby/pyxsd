"""Disposable execution returns bytes, never live objects or trusted verdicts."""

import hashlib
import os
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_workers import make_snapshot

from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.errors import ToolError


def execute(tmp_path, source, *, limits=None, timeout=10):
    from pyxsd.mcp.execution import TrustedLocalExecution
    from pyxsd.mcp.transforms import normalize_transform

    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir(exist_ok=True)
    snapshot = make_snapshot(snapshot_root)
    limits = limits or Limits()
    runtime = TrustedLocalExecution(tmp_path / "output", limits)

    async def scenario():
        return await runtime.run(
            snapshot,
            SchemaRef(source=FileRef(path="schema.xsd")),
            FileRef(path="document.xml"),
            normalize_transform(
                source if isinstance(source, dict) else {"kind": "python", "source": source}, limits
            ),
            registry={},
            deadline=time.monotonic() + timeout,
        )

    return anyio.run(scenario)


@pytest.mark.parametrize("returned", ["document", "document.root", "None"])
def test_python_document_root_and_in_place_returns(tmp_path, returned):
    result = execute(tmp_path, f"def transform(document, context):\n    return {returned}\n")
    assert b"<root" in result.content and b">3<" in result.content
    assert result.process_returncode == 0
    assert not list((tmp_path / "output").iterdir())


@pytest.mark.parametrize(
    "source,code",
    [
        ("def transform(document, context):\n    return {'value': 1}", "invalid_transform_result"),
        ("def transform(document, context):\n    document.root = None", "unbindable"),
        ("not python !", "transform_failed"),
        ("value = 1", "invalid_transform"),
        ("def transform(document, context):\n    raise RuntimeError('secret')", "transform_failed"),
    ],
)
def test_invalid_code_results_and_absent_root_are_typed(tmp_path, source, code):
    with pytest.raises(ToolError, match=code) as raised:
        execute(tmp_path, source)
    assert "secret" not in raised.value.fault.message


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_worker_cannot_supply_nonregular_or_linked_candidate(tmp_path, kind):
    source = f"""import os
def transform(document, context):
    target = context.output_directory + '/candidate.xml'
    if {kind!r} == 'symlink':
        os.symlink('/etc/passwd', target)
    elif {kind!r} == 'hardlink':
        other = context.output_directory + '/other'
        open(other, 'wb').write(b'<root/>')
        os.link(other, target)
    elif {kind!r} == 'directory':
        os.mkdir(target)
    else:
        os.mkfifo(target)
    os._exit(0)
"""
    with pytest.raises(ToolError, match="unsafe_candidate"):
        execute(tmp_path, source)


def test_deadline_terminates_disposable_process(tmp_path):
    with pytest.raises(ToolError, match="deadline_exceeded"):
        execute(tmp_path, "def transform(document, context):\n    while True: pass", timeout=0.5)
    assert not list((tmp_path / "output").iterdir())


def test_stdout_flood_is_not_a_transform_result(tmp_path):
    with pytest.raises(ToolError, match="ipc_limit"):
        execute(
            tmp_path,
            "def transform(document, context):\n    print('x' * 20000)",
            limits=Limits(request_bytes=8192),
        )


def test_oversized_candidate_refused_even_if_worker_bypasses_serializer(tmp_path):
    source = """import os
def transform(document, context):
    open(context.output_directory + '/candidate.xml', 'wb').write(b'x' * 2000)
    os._exit(0)
"""
    with pytest.raises(ToolError, match="output_limit"):
        execute(tmp_path, source, limits=Limits(output_bytes=1024))


def test_pipeline_uses_explicit_definitions_and_returns_advisory_checkpoints(tmp_path):
    source = """from io import StringIO
def transform(document, context, value, *, extra):
    return document.schema.parse(StringIO('<root><item>' + str(value + extra) + '</item></root>'))
"""
    result = execute(
        tmp_path,
        {
            "kind": "pipeline",
            "steps": [
                {"name": "rewrite", "args": [7], "kwargs": {"extra": 2}, "checkpoint": True},
                {"name": "rewrite", "args": [8], "kwargs": {"extra": 1}},
            ],
            "definitions": {"rewrite": source},
        },
    )
    assert b">9<" in result.content
    assert result.checkpoints["checkpoints"][0]["counts"] == {"error": 0, "warning": 0}
    assert len(result.checkpoint_candidates) == 1
    assert b">9<" in result.checkpoint_candidates[0]


def test_serializer_output_budget_is_enforced(tmp_path):
    with pytest.raises(ToolError, match="output_limit"):
        execute(
            tmp_path,
            "def transform(document, context):\n    return document",
            limits=Limits(output_bytes=8),
        )


def test_checkpoint_output_budget_is_enforced(tmp_path):
    with pytest.raises(ToolError, match="output_limit"):
        execute(
            tmp_path,
            {
                "kind": "pipeline",
                "steps": [{"name": "keep", "checkpoint": True}],
                "definitions": {"keep": "def transform(document, context): pass"},
            },
            limits=Limits(output_bytes=8),
        )


def test_malformed_or_missing_worker_reply_is_not_a_verdict(tmp_path):
    with pytest.raises(ToolError, match="ipc_protocol"):
        execute(tmp_path, "def transform(document, context):\n    print('fake verdict')")
    source = """import os
def transform(document, context):
    open(context.output_directory + '/candidate.xml', 'wb').write(b'<root/>')
    os._exit(0)
"""
    with pytest.raises(ToolError, match="ipc_protocol"):
        execute(tmp_path, source)


def test_candidate_growth_between_stat_and_read_still_obeys_budget(tmp_path, monkeypatch):
    from pyxsd.mcp.execution import capture_candidate

    (tmp_path / "candidate.xml").write_bytes(b"x" * 100)
    actual = os.fstat
    monkeypatch.setattr(
        os, "fstat", lambda fd: SimpleNamespace(st_mode=actual(fd).st_mode, st_nlink=1, st_size=0)
    )
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ToolError, match="output_limit"):
            capture_candidate(directory, 10)
    finally:
        os.close(directory)


def test_unbound_input_is_rejected_before_loading_user_code(tmp_path):
    from pyxsd.mcp.captures import CaptureFile
    from pyxsd.mcp.execution import ExecutionRequest
    from pyxsd.mcp.execution_main import execute as run
    from pyxsd.mcp.transforms import PythonTransformSpec
    from pyxsd.mcp.workers import capture_to_wire

    snapshot = make_snapshot(tmp_path)
    data = b"<unknown/>"
    (tmp_path / "document.xml").write_bytes(data)
    snapshot = replace(
        snapshot,
        files=tuple(
            CaptureFile(item.path, hashlib.sha256(data).hexdigest(), len(data))
            if item.path == "document.xml"
            else item
            for item in snapshot.files
        ),
    )
    request = ExecutionRequest(
        request_id="execution",
        capture=capture_to_wire(snapshot),
        schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
        document=FileRef(path="document.xml"),
        transform=PythonTransformSpec(source="raise RuntimeError('must not run')"),
        sources={},
        output_directory=str(tmp_path),
        limits=Limits(),
    )
    with pytest.raises(ToolError, match="unbindable"):
        run(request)
