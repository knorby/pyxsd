"""Shared fixtures and helpers for the RELAX NG instance-limit tests.

Budgets, adversarial derivative depth, and parse concurrency share these named
schemas and deep-shape builders.  Test modules import the names they use
explicitly; nothing here is a test.
"""

from __future__ import annotations

import io

import pyxsd.relaxng.validator as validator
from pyxsd.relaxng import RelaxNGDocument, RelaxNGLimits, RelaxNGSchema
from pyxsd.relaxng.datatypes import DatatypeContext
from pyxsd.relaxng.patterns import Element, PatternPool, Ref

NS = "http://relaxng.org/ns/structure/1.0"

SIMPLE_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="record">
      <optional><element name="count"><text/></element></optional>
    </element>
  </start>
</grammar>"""

INVALID_XML = "<record><other/></record>"
VALID_XML = "<record/>"

#: Deep enough that the former recursive derivative path exceeded Python's
#: default recursion limit, but small enough to compile in a normal test run.
DEEP_DEPTH = 1200
#: An integrated event parse keeps one event per authored child; a moderate
#: depth pins the full traversal without quadratic test cost.
INTEGRATED_EVENT_DEPTH = 80
#: Text derivatives nest through their first operand, so a depth just past the
#: old recursion failure proves the iterative path at low test cost.
DEEP_TEXT_DEPTH = 500
#: A direct deep event derivative pins the iterative treatment past Python's
#: recursion limit without replaying that depth for every child occurrence.
ADVERSARIAL_DEPTH = 1200
#: A recursive element tree exercises the iterative document walk, not the
#: pattern derivative depth.
DEEP_DOCUMENT_DEPTH = 1500


def compile_schema(source: str = SIMPLE_SCHEMA, **options) -> RelaxNGSchema:
    return RelaxNGSchema.compile(io.StringIO(source), **options)


def parse(schema: RelaxNGSchema, xml: str, **options) -> RelaxNGDocument:
    return schema.parse(io.StringIO(xml), typed=False, **options)


def deep_text_schema(depth: int = DEEP_TEXT_DEPTH) -> RelaxNGSchema:
    inner = "<text/>"
    for _ in range(depth):
        inner = f"<group>{inner}<text/></group>"
    source = f'<grammar xmlns="{NS}"><start><element name="r">{inner}</element></start></grammar>'
    return compile_schema(source)


def deep_first_operand(tag: str, depth: int = DEEP_DEPTH) -> tuple[RelaxNGSchema, str]:
    inner = '<element name="a0"><empty/></element>'
    for index in range(1, depth + 1):
        inner = f'<{tag}>{inner}<element name="a{index}"><empty/></element></{tag}>'
    source = f'<grammar xmlns="{NS}"><start><element name="r">{inner}</element></start></grammar>'
    children = "".join(f"<a{index}/>" for index in range(depth + 1))
    return compile_schema(source), f"<r>{children}</r>"


def root_content(schema: RelaxNGSchema):
    node = schema.grammar.start
    while isinstance(node, Ref):
        node = node.target
    assert isinstance(node, Element)
    return node.pattern


def derivation_context(limits: RelaxNGLimits | None = None) -> validator.DerivationContext:
    return validator.DerivationContext(DatatypeContext(None, {}), PatternPool(), limits)


def recursive_schema() -> RelaxNGSchema:
    source = f"""<grammar xmlns="{NS}">
      <start><ref name="node"/></start>
      <define name="node">
        <element name="n">
          <zeroOrMore><ref name="node"/></zeroOrMore>
        </element>
      </define>
    </grammar>"""
    return compile_schema(source)


def nested_instance(name: str, depth: int) -> str:
    return f"<{name}>" * depth + f"</{name}>" * depth
