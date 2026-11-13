"""XSLT 3.0 compilation, invocation, and optional advisory inspection.

The normative processor, compiled stylesheet, and result views are the public
entry points.  :mod:`pyxsd.xslt.inspection` remains a separate, non-normative
binding of the packaged XSLT schemas; it never decides acceptance or compiles
the stylesheet.
"""

from pyxsd.xslt.errors import XsltDynamicError, XsltInspectionError
from pyxsd.xslt.extensions import (
    ExtensionSignature,
    XsltExtensions,
    no_args,
    xs_integer_to_integer,
)
from pyxsd.xslt.inspection import (
    InspectionDiagnostic,
    StylesheetInspection,
    inspect_stylesheet,
)
from pyxsd.xslt.policy import OutputPolicy
from pyxsd.xslt.processor import (
    CompiledStylesheet,
    XsltInvocationError,
    XsltProcessor,
)
from pyxsd.xslt.python_rules import PythonRuleContext, PythonRules
from pyxsd.xslt.results import (
    Message,
    ResultEntry,
    ResultShapeError,
    TransformationResult,
)
from pyxsd.xslt.runtime.frames import ExecutionLimits

__all__ = [
    "CompiledStylesheet",
    "ExecutionLimits",
    "ExtensionSignature",
    "InspectionDiagnostic",
    "Message",
    "OutputPolicy",
    "PythonRuleContext",
    "PythonRules",
    "ResultEntry",
    "ResultShapeError",
    "StylesheetInspection",
    "TransformationResult",
    "XsltDynamicError",
    "XsltExtensions",
    "XsltInspectionError",
    "XsltInvocationError",
    "XsltProcessor",
    "inspect_stylesheet",
    "no_args",
    "xs_integer_to_integer",
]
