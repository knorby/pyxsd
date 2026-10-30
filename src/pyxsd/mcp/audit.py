"""Small structured audit sink with no payload fields or user identity."""

import json
import logging
import time
from collections.abc import Callable

from pydantic import JsonValue

logger = logging.getLogger("pyxsd.mcp.audit")


class AuditLogger:
    def __init__(self, *, metrics: Callable[[dict[str, JsonValue]], None] | None = None) -> None:
        self._metrics = metrics

    def emit(
        self,
        *,
        operation: str,
        status: str,
        duration: float,
        workspace_id: str | None = None,
        generation: int | None = None,
        cache: str | None = None,
        input_digest: str | None = None,
        candidate_digest: str | None = None,
    ) -> None:
        event: dict[str, JsonValue] = {
            "timestamp": time.time(),
            "operation": operation,
            "status": status,
            "duration_ms": max(0, round(duration * 1000)),
        }
        for key, value in (
            ("workspace", workspace_id),
            ("generation", generation),
            ("cache", cache),
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
