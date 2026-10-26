"""Opaque page positions bind their caller and immutable query inputs."""

import base64
import hashlib
import hmac
import json

import pytest

pytest.importorskip("pydantic")

from pyxsd.mcp import pagination
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.workspaces import Principal

OWNER = Principal("private-owner@example.invalid", frozenset())
QUERY = "a" * 64
KEY = b"k" * 32


def test_cursor_roundtrip_and_bindings():
    now = [100.0]
    codec = pagination.CursorCodec(KEY, ttl=30, clock=lambda: now[0])
    token = codec.encode_page(OWNER, "revision-one", QUERY, 50)
    assert codec.decode_page(OWNER, "revision-one", QUERY, token) == 50
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert OWNER.owner_id.encode() not in raw
    assert b"revision-one" not in raw
    for owner, revision, query in [
        (Principal("other", frozenset()), "revision-one", QUERY),
        (OWNER, "revision-two", QUERY),
        (OWNER, "revision-one", "b" * 64),
    ]:
        with pytest.raises(ToolError, match="invalid_cursor"):
            codec.decode_page(owner, revision, query, token)
    now[0] = 130.0
    with pytest.raises(ToolError, match="invalid_cursor"):
        codec.decode_page(OWNER, "revision-one", QUERY, token)


@pytest.mark.parametrize("token", ["", "!", "a", "a" * 513, "YWJj", "YWJj=", "YWJj\n"])
def test_malformed_cursors_are_typed_errors(token):
    codec = pagination.CursorCodec(KEY, ttl=30)
    with pytest.raises(ToolError, match="invalid_cursor"):
        codec.decode_page(OWNER, "revision", QUERY, token)


def test_tampering_and_another_signing_key_are_rejected():
    codec = pagination.CursorCodec(KEY, ttl=30)
    token = codec.encode_page(OWNER, "revision", QUERY, 0)
    altered = ("A" if token[0] != "A" else "B") + token[1:]
    for candidate, decoder in [(altered, codec), (token, pagination.CursorCodec(b"x" * 32))]:
        with pytest.raises(ToolError, match="invalid_cursor"):
            decoder.decode_page(OWNER, "revision", QUERY, candidate)


@pytest.mark.parametrize("position", [-1, True, 1.5, 2**63])
def test_invalid_positions_never_produce_cursors(position):
    with pytest.raises(ToolError, match="invalid_argument"):
        pagination.CursorCodec(KEY).encode_page(OWNER, "revision", QUERY, position)


@pytest.mark.parametrize("key,ttl", [(b"short", 30), (KEY, 0), (KEY, True)])
def test_signing_configuration_is_validated(key, ttl):
    with pytest.raises(ValueError):
        pagination.CursorCodec(key, ttl=ttl)


@pytest.mark.parametrize("revision,query", [("", QUERY), ("x" * 129, QUERY), ("r", "not-a-digest")])
def test_query_identity_is_bounded(revision, query):
    with pytest.raises(ToolError, match="invalid_argument"):
        pagination.CursorCodec(KEY).encode_page(OWNER, revision, query, 0)


@pytest.mark.parametrize(
    "payload", [b"{", b"null", b"[]", b"[2,9999999999,0,0,0,0]", b"[1,9999999999,true,0,0,0]"]
)
def test_authenticated_but_invalid_payloads_fail_closed(payload):
    # Simulate incompatible/old issuer data, not an untrusted request with the key.
    signature = hmac.digest(KEY, payload, hashlib.sha256)
    token = base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode()
    with pytest.raises(ToolError, match="invalid_cursor"):
        pagination.CursorCodec(KEY).decode_page(OWNER, "revision", QUERY, token)


def test_default_key_is_private_to_its_codec():
    codec = pagination.CursorCodec()
    token = codec.encode_page(OWNER, "revision", QUERY, 2**63 - 1)
    assert codec.decode_page(OWNER, "revision", QUERY, token) == 2**63 - 1
    with pytest.raises(ToolError, match="invalid_cursor"):
        pagination.CursorCodec().decode_page(OWNER, "revision", QUERY, token)
    # Payload is valid bounded JSON, but contains keyed bindings rather than identities.
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert json.loads(raw[:-32])[2] == 2**63 - 1
