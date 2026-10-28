"""Page cursors bind one nonnegative position to a result identity."""

import base64
import json

import pytest

pytest.importorskip("pydantic")

from pyxsd.mcp import pagination
from pyxsd.mcp.errors import ToolError

QUERY = "a" * 64


def test_cursor_roundtrip_and_result_binding():
    tokens = pagination.PageTokens()
    token = tokens.encode_page(QUERY, 50)
    assert tokens.decode_page(QUERY, token) == 50
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert QUERY.encode() not in raw
    with pytest.raises(ToolError, match="invalid_cursor"):
        tokens.decode_page("b" * 64, token)


@pytest.mark.parametrize("token", ["", "!", "a", "a" * 513, "YWJj", "YWJj=", "YWJj\n"])
def test_malformed_cursors_are_typed_errors(token):
    with pytest.raises(ToolError, match="invalid_cursor"):
        pagination.PageTokens().decode_page(QUERY, token)


def test_altered_identity_is_rejected():
    token = pagination.PageTokens().encode_page(QUERY, 0)
    altered = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(ToolError, match="invalid_cursor"):
        pagination.PageTokens().decode_page(QUERY, altered)


@pytest.mark.parametrize("position", [-1, True, 1.5, 2**63])
def test_invalid_positions_never_produce_cursors(position):
    with pytest.raises(ToolError, match="invalid_argument"):
        pagination.PageTokens().encode_page(QUERY, position)


@pytest.mark.parametrize("result_id", ["", "x" * 257, 1])
def test_result_identity_is_bounded(result_id):
    with pytest.raises(ToolError, match="invalid_argument"):
        pagination.PageTokens().encode_page(result_id, 0)


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b"null",
        b"[]",
        b'{"v":2,"p":1,"r":"x"}',
        b'{"v":1,"p":true,"r":"x"}',
        b'{"v":1,"p":-1,"r":"x"}',
        b'{"v":1,"p":1}',
        b'{"v":1,"p":1,"r":"x","extra":0}',
    ],
)
def test_incompatible_payloads_fail_closed(payload):
    token = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    with pytest.raises(ToolError, match="invalid_cursor"):
        pagination.PageTokens().decode_page(QUERY, token)


def test_maximum_position_roundtrips():
    token = pagination.PageTokens().encode_page(QUERY, 2**63 - 1)
    assert pagination.PageTokens().decode_page(QUERY, token) == 2**63 - 1
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert json.loads(raw)["p"] == 2**63 - 1
