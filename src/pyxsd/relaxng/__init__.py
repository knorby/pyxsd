"""Native RELAX NG 1.0 support.

The package exposes its public contracts as each stage lands. It does not
re-export RELAX NG names from :mod:`pyxsd`, and it never routes RELAX NG work
through the XSD compiler.
"""

from pyxsd.relaxng.annotations import NodeAnnotations, ScalarAnnotation
from pyxsd.relaxng.document import RelaxNGDocument
from pyxsd.relaxng.errors import (
    AmbiguousValueError,
    RelaxNGCompactSyntaxError,
    RelaxNGLimitError,
    StaleInterpretationError,
)
from pyxsd.relaxng.evidence import CandidateSelection
from pyxsd.relaxng.introspection import (
    RelaxNGComponentDescription,
    RelaxNGSchemaDescription,
    describe_component,
    describe_schema,
    find_components,
    related_components,
)
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.schema import RelaxNGSchema, compile
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.values import AlternativeValue, ListInterpretation

__all__ = [
    "AlternativeValue",
    "AmbiguousValueError",
    "CandidateSelection",
    "ListInterpretation",
    "NodeAnnotations",
    "RelaxNGCompactSyntaxError",
    "RelaxNGComponentDescription",
    "RelaxNGDocument",
    "RelaxNGElement",
    "RelaxNGLimitError",
    "RelaxNGLimits",
    "RelaxNGSchema",
    "RelaxNGSchemaDescription",
    "ScalarAnnotation",
    "StaleInterpretationError",
    "compile",
    "describe_component",
    "describe_schema",
    "find_components",
    "related_components",
]
