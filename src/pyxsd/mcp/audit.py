"""Small structured audit sink with pseudonymous owners and no payload fields."""

import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable

from pydantic import JsonValue

logger = logging.getLogger("pyxsd.mcp.audit")


class AuditLogger:
    def __init__(self, *, metrics: Callable[[dict[str, JsonValue]], None] | None = None) -> None:
        self._key = secrets.token_bytes(32)
        self._metrics = metrics

    def _owner(self, owner_id: str) -> str:
        return hmac.digest(self._key, owner_id.encode("utf-8"), hashlib.sha256).hex()[:24]

    def emit(
        self,
        *,
        operation: str,
        owner_id: str,
        status: str,
        duration: float,
        workspace_id: str | None = None,
        revision: str | None = None,
        scope: str | None = None,
        cache: str | None = None,
        job_id: str | None = None,
        input_digest: str | None = None,
        candidate_digest: str | None = None,
    ) -> None:
        event: dict[str, JsonValue] = {
            "timestamp": time.time(),
            "operation": operation,
            "owner": self._owner(owner_id),
            "status": status,
            "duration_ms": max(0, round(duration * 1000)),
        }
        for key, value in (
            ("workspace", workspace_id),
            ("revision", revision),
            ("scope", scope),
            ("cache", cache),
            ("job", job_id),
            ("input_digest", input_digest),
            ("candidate_digest", candidate_digest),
        ):
            if value is not None:
                event[key] = value
        logger.info(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        if self._metrics is not None:
            try:
                self._metrics(dict(event))
            except Exception:
                logger.warning("MCP metrics callback failed")
