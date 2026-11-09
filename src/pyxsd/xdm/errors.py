"""Errors raised when a document cannot supply a coherent XDM view.

The XDM layer keeps source-complete node trees separate from the bound
instance tree.  A view is only meaningful while both describe the same
revision, so this error is raised when capture was never requested or the
bound tree has changed since capture.
"""

from __future__ import annotations

from pyxsd.exceptions import PyXSDError

__all__ = ["XdmInputError"]


class XdmInputError(PyXSDError):
    """A coherent XDM view is unavailable for this document.

    ``Schema.parse(..., capture_xdm=True)`` is the opt-in capture path.
    Documents parsed without it, and documents whose bound tree was edited
    after capture, refuse to expose an XDM view instead of returning one
    that silently disagrees with the source XML.
    """
