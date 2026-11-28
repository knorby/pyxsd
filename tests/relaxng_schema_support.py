"""Fixtures shared by the RELAX NG compilation contract and its deep shapes.

``NS``, the shared valid schema, the compile helper, and the reference-edge
walker are used by more than one test module.  The contract's own policy
doubles and graph rebuilding stay with the tests that use them.
"""

from __future__ import annotations

import io

from pyxsd.relaxng.patterns import (
    Ref,
)
from pyxsd.relaxng.schema import RelaxNGSchema

NS = "http://relaxng.org/ns/structure/1.0"

VALID = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <attribute name="id"><text/></attribute>
      <optional><element name="item"><text/></element></optional>
    </element>
  </start>
</grammar>"""


def compile_schema(
    source: object = None,
    **options: object,
) -> RelaxNGSchema:
    """Compile the shared valid schema unless another source is supplied."""
    return RelaxNGSchema.compile(
        io.StringIO(VALID) if source is None else source,  # type: ignore[arg-type]
        **options,  # type: ignore[arg-type]
    )


def deref(pattern: object) -> object:
    """Follow bound element reference edges to their target."""
    while isinstance(pattern, Ref):
        pattern = pattern.target
    return pattern
