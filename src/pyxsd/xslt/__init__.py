"""Optional, advisory XSLT stylesheet inspection.

This package does not implement the normative XSLT compiler.  It exposes a
non-normative pyxsd binding of a stylesheet module against the packaged W3C
stylesheet schemas; see :mod:`pyxsd.xslt.inspection`.
"""

from pyxsd.xslt.errors import XsltInspectionError
from pyxsd.xslt.inspection import (
    InspectionDiagnostic,
    StylesheetInspection,
    inspect_stylesheet,
)

__all__ = [
    "InspectionDiagnostic",
    "StylesheetInspection",
    "XsltInspectionError",
    "inspect_stylesheet",
]
