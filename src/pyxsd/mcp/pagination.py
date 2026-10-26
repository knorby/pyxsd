"""Authenticated page positions without server-side cursor accumulation."""

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable

from .errors import ToolError
from .workspaces import Principal


class CursorCodec:
    """Bind a bounded position to its caller, revision, query and expiry.

    Clients must treat cursors as opaque. Keyed identity bindings avoid placing
    owner names or revision strings in the token; this is authentication, not
    general-purpose encryption. The default key lasts for this codec's lifetime,
    so restarting a coordinator invalidates its outstanding cursors. Embedders
    may supply a private 32-byte key when they need continuity across restarts.
    A cursor never replaces authorization checks on the underlying resource.
    """

    def __init__(
        self,
        key: bytes | None = None,
        *,
        ttl: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if key is not None and (not isinstance(key, bytes) or len(key) != 32):
            raise ValueError("Cursor key must contain exactly 32 bytes")
        if type(ttl) is not int or ttl <= 0:
            raise ValueError("Cursor TTL must be a positive integer")
        self._key = secrets.token_bytes(32) if key is None else key
        self._ttl, self._clock = ttl, clock

    def _bindings(self, principal: Principal, revision: str, query_digest: str) -> list[str]:
        if not 1 <= len(revision) <= 128 or re.fullmatch(r"[0-9a-f]{64}", query_digest) is None:
            raise ToolError("invalid_argument", "Invalid page query identity")
        return [
            hmac.digest(self._key, json.dumps([kind, value]).encode(), hashlib.sha256).hex()
            for kind, value in (
                ("owner", principal.owner_id),
                ("revision", revision),
                ("query", query_digest),
            )
        ]

    def encode_page(
        self, principal: Principal, revision: str, query_digest: str, position: int
    ) -> str:
        """Return an expiring cursor for a nonnegative signed-64-bit position."""
        if type(position) is not int or not 0 <= position < 2**63:
            raise ToolError("invalid_argument", "Invalid page position")
        payload = json.dumps(
            [
                1,
                int(self._clock()) + self._ttl,
                position,
                *self._bindings(principal, revision, query_digest),
            ],
            separators=(",", ":"),
        ).encode()
        signature = hmac.digest(self._key, payload, hashlib.sha256)
        return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")

    def decode_page(
        self, principal: Principal, revision: str, query_digest: str, cursor: str
    ) -> int:
        """Verify all bindings before returning the next position.

        Unknown, expired, malformed and foreign tokens share one public error.
        The fixed input bound applies before base64 or JSON decoding.
        """
        bindings = self._bindings(principal, revision, query_digest)
        if not 1 <= len(cursor) <= 512 or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None:
            raise ToolError("invalid_cursor", "Page cursor is invalid or expired")
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.digest(self._key, payload, hashlib.sha256), signature):
                raise ValueError("signature")
            data = json.loads(payload)
            if (
                not isinstance(data, list)
                or len(data) != 6
                or type(data[0]) is not int
                or data[0] != 1
                or type(data[1]) is not int
                or self._clock() >= data[1]
                or type(data[2]) is not int
                or not 0 <= data[2] < 2**63
                or data[3:] != bindings
            ):
                raise ValueError("payload")
            return int(data[2])
        except (ValueError, binascii.Error) as exc:
            raise ToolError("invalid_cursor", "Page cursor is invalid or expired") from exc
