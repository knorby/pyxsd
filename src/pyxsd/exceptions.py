"""Exception hierarchy for pyxsd.

Fatal problems (a missing schema, an unopenable file, a schema with no
root elements) raise :class:`PyXSDError`. Recoverable conditions that a
caller may want to filter with :func:`warnings.filterwarnings` are
emitted as :class:`PyXSDWarning`. The fatal hierarchy is defined here in
full — ``ValidationError``, ``NamespaceError`` and ``XPathError`` share
the :class:`PyXSDError` root — with ``pyxsd.namespaces`` and
``pyxsd.xpath_subset`` re-exporting the latter two for compatibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyxsd.validation import ValidationReport


class PyXSDError(Exception):
    """Raised when parsing cannot proceed."""


class ValidationError(PyXSDError):
    """Raised by ``require_valid()`` when a report contains errors.

    - ``report``: the :class:`~pyxsd.validation.ValidationReport` whose
      errors caused the raise; inspect it instead of parsing the message.
    """

    def __init__(self, message: str, report: ValidationReport) -> None:
        super().__init__(message)
        self.report = report


class NamespaceError(PyXSDError):
    """A QName used a prefix that is not bound in its scope."""


class XPathError(PyXSDError):
    """The expression is outside the identity-constraint XPath subset."""


class PyXSDWarning(UserWarning):
    """Warning category for recoverable, non-fatal conditions."""
