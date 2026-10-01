"""Optional schema-derived adapters; importing this package needs no extras."""

from pyxsd.exceptions import PyXSDError

__all__ = ["IntegrationError"]


class IntegrationError(PyXSDError):
    """An unsupported shape or incompatible value in an integration projection."""
