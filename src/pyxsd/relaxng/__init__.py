"""Native RELAX NG 1.0 support.

The package exposes its public contracts as each stage lands. It does not
re-export RELAX NG names from :mod:`pyxsd`, and it never routes RELAX NG work
through the XSD compiler.
"""

from pyxsd.relaxng.document import RelaxNGDocument
from pyxsd.relaxng.errors import (
    AmbiguousValueError,
    RelaxNGLimitError,
    StaleInterpretationError,
)
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.schema import RelaxNGSchema, compile
from pyxsd.relaxng.tree import RelaxNGElement

__all__ = [
    "AmbiguousValueError",
    "RelaxNGDocument",
    "RelaxNGElement",
    "RelaxNGLimitError",
    "RelaxNGLimits",
    "RelaxNGSchema",
    "StaleInterpretationError",
    "compile",
]
