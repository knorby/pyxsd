"""XPath evaluation substrate for the pyxsd transformation engine.

The package contains the diagnostic capability probes, the continuation seam,
and the standalone, schema-aware expression environment.  It does not import
stylesheet execution code: expressions compile and evaluate without a
transformation, initial template, result builder, or mode.
"""

from pyxsd.xpath.engine import (
    CompiledExpression,
    XPathEngine,
    XPathStaticContext,
)
from pyxsd.xpath.environment import ExpressionEnvironment, Focus

__all__ = [
    "CompiledExpression",
    "ExpressionEnvironment",
    "Focus",
    "XPathEngine",
    "XPathStaticContext",
]
