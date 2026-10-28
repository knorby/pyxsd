"""Bounded page positions bound to one deterministic result identity."""

import base64
import binascii
import hashlib
import json
import re

from .errors import ToolError

_VERSION = 1


def _identity(result_id: str) -> str:
    if not isinstance(result_id, str) or not 1 <= len(result_id) <= 256:
        raise ToolError("invalid_argument", "Invalid page result identity")
    return hashlib.sha256(result_id.encode("utf-8")).hexdigest()


class PageTokens:
    """Encode a nonnegative position for one result without a signing key.

    The identity binding detects stale or foreign cursors; it is not authentication.
    Inputs and result identity already come from the coordinator, never a cursor.
    """

    @staticmethod
    def encode_page(result_id: str, position: int) -> str:
        if type(position) is not int or not 0 <= position < 2**63:
            raise ToolError("invalid_argument", "Invalid page position")
        payload = json.dumps(
            {"v": _VERSION, "p": position, "r": _identity(result_id)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

    @staticmethod
    def decode_page(result_id: str, cursor: str) -> int:
        identity = _identity(result_id)
        if not 1 <= len(cursor) <= 512 or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None:
            raise ToolError("invalid_cursor", "Page cursor is invalid")
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            data = json.loads(raw.decode("ascii"))
            if (
                not isinstance(data, dict)
                or set(data) != {"v", "p", "r"}
                or type(data["v"]) is not int
                or data["v"] != _VERSION
                or type(data["p"]) is not int
                or not 0 <= data["p"] < 2**63
                or data["r"] != identity
            ):
                raise ValueError("payload")
            return int(data["p"])
        except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
            raise ToolError("invalid_cursor", "Page cursor is invalid") from exc
