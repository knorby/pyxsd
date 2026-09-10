"""Exception hierarchy for pyxsd.

Fatal problems (a missing schema, an unopenable file, a schema with no
root elements) raise :class:`PyXSDError`. Recoverable conditions that a
caller may want to filter with :func:`warnings.filterwarnings` are
emitted as :class:`PyXSDWarning`.
"""


class PyXSDError(Exception):
    """Raised when parsing cannot proceed."""


class PyXSDWarning(UserWarning):
    """Warning category for recoverable, non-fatal conditions."""
