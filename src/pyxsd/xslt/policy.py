"""Application authorization for XSLT output destinations.

Authorization alone never writes a result. Results remain in memory until an
application explicitly requests a separate output operation.
"""

from typing import Protocol


class OutputPolicy(Protocol):
    """Authorize one absolute output URI before its content is constructed."""

    def authorize(self, uri: str, *, principal: bool) -> None:
        """Raise a resource policy error when the destination is not allowed."""
        ...
