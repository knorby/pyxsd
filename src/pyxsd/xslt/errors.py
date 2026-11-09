"""Errors raised by advisory XSLT stylesheet inspection."""

from __future__ import annotations

from pyxsd.exceptions import PyXSDError


class XsltInspectionError(PyXSDError):
    """Advisory stylesheet inspection could not read or bind its inputs.

    This is an input/resource failure, not an XSLT static error. Schema
    legality findings and instance binding findings are reported on
    :class:`~pyxsd.xslt.inspection.StylesheetInspection.report` instead.
    """
