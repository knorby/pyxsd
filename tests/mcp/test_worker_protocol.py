"""The process boundary accepts bounded JSON, never Python object transport."""

import importlib
from typing import Literal

import pytest

pytest.importorskip("pydantic")

from pyxsd.mcp.contracts import ToolFault, WireModel
from pyxsd.mcp.errors import ToolError


class Command(WireModel):
    role: Literal["clean"]
    owner: str
    request: str
    count: int


def test_round_trip_checks_role_identity_and_exact_byte_budget():
    worker_protocol = importlib.import_module("pyxsd.mcp.worker_protocol")
    command = Command(role="clean", owner="owner", request="request", count=1)
    frame = worker_protocol.encode_frame(command, max_bytes=256)
    assert frame.endswith(b"\n")
    assert (
        worker_protocol.decode_frame(
            frame, Command, max_bytes=len(frame), expected={"owner": "owner", "request": "request"}
        )
        == command
    )
    with pytest.raises(ToolError, match="ipc_limit"):
        worker_protocol.encode_frame(command, max_bytes=len(frame) - 1)
    with pytest.raises(ToolError, match="ipc_limit"):
        worker_protocol.decode_frame(frame, Command, max_bytes=len(frame) - 1, expected={})
    for expected in ({"owner": "other"}, {"request": "other"}, {"missing": "value"}):
        with pytest.raises(ToolError, match="ipc_protocol"):
            worker_protocol.decode_frame(frame, Command, max_bytes=256, expected=expected)
    for extra_line in (frame + b"\n", b"\n" + frame):
        with pytest.raises(ToolError, match="ipc_protocol"):
            worker_protocol.decode_frame(extra_line, Command, max_bytes=256, expected={})


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b"{}",
        b"{}\n{}\n",
        b"not json\n",
        b"\xff\n",
        b"[]\n",
        b'{"role":"clean","role":"clean"}\n',
        b'{"value":NaN}\n',
        b'{"value":Infinity}\n',
        b'{"role":"execution","owner":"o","request":"r","count":1}\n',
        b'{"role":"clean","owner":"o","request":"r","count":true}\n',
        b'{"role":"clean","owner":"o","request":"r","count":1,"source":"evil"}\n',
        b"[" * 2000 + b"]" * 2000 + b"\n",
    ],
)
def test_malformed_coercible_and_unknown_commands_fail_without_payload_disclosure(frame):
    worker_protocol = importlib.import_module("pyxsd.mcp.worker_protocol")
    with pytest.raises(ToolError, match="ipc_protocol") as error:
        worker_protocol.decode_frame(frame, Command, max_bytes=8192, expected={})
    assert "evil" not in str(error.value)


def test_utf8_count_and_fault_are_lossless():
    worker_protocol = importlib.import_module("pyxsd.mcp.worker_protocol")
    fault = ToolFault(code="invalid_xml", message="\u00e9" * 20)
    frame = worker_protocol.encode_frame(fault, max_bytes=256)
    assert (
        worker_protocol.decode_frame(frame, ToolFault, max_bytes=len(frame), expected={}) == fault
    )
    assert b"\xc3\xa9" in frame


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_invalid_budgets_are_rejected(budget):
    worker_protocol = importlib.import_module("pyxsd.mcp.worker_protocol")
    with pytest.raises(ToolError, match="invalid_argument"):
        worker_protocol.encode_frame(ToolFault(code="x", message="x"), max_bytes=budget)
    with pytest.raises(ToolError, match="invalid_argument"):
        worker_protocol.decode_frame(b"{}\n", Command, max_bytes=budget, expected={})


def test_non_models_and_unencodable_values_are_not_transported():
    worker_protocol = importlib.import_module("pyxsd.mcp.worker_protocol")
    with pytest.raises(ToolError, match="ipc_protocol"):
        worker_protocol.encode_frame(object(), max_bytes=256)
    for value in ("\ud800", float("nan")):
        fault = ToolFault(code="x", message="x")
        fault.details["value"] = value
        with pytest.raises(ToolError, match="ipc_protocol"):
            worker_protocol.encode_frame(fault, max_bytes=256)
