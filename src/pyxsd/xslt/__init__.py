"""XSLT 3.0 compilation, invocation, and optional advisory inspection.

The normative processor, compiled stylesheet, and result views are the public
entry points.  :mod:`pyxsd.xslt.inspection` remains a separate, non-normative
binding of the packaged XSLT schemas; it never decides acceptance or compiles
the stylesheet.
"""

from pyxsd.xslt.errors import XsltInspectionError
from pyxsd.xslt.inspection import (
    InspectionDiagnostic,
    StylesheetInspection,
    inspect_stylesheet,
)
from pyxsd.xslt.processor import (
    CompiledStylesheet,
    XsltInvocationError,
    XsltProcessor,
)
from pyxsd.xslt.results import (
    Message,
    ResultEntry,
    ResultShapeError,
    TransformationResult,
)

__all__ = [
    "CompiledStylesheet",
    "InspectionDiagnostic",
    "Message",
    "ResultEntry",
    "ResultShapeError",
    "StylesheetInspection",
    "TransformationResult",
    "XsltInspectionError",
    "XsltInvocationError",
    "XsltProcessor",
    "inspect_stylesheet",
]
