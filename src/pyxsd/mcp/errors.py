"""Typed service faults; adapters decide their transport representation."""

from pydantic import JsonValue

from .contracts import ToolFault


class ToolError(Exception):
    """An expected operational failure, not an XML validation verdict."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        self.fault = ToolFault(
            code=code,
            message=message,
            retryable=retryable,
            details={} if details is None else details,
        )
        super().__init__(f"{code}: {message}")
