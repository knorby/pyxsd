"""Deep RELAX NG instance trees validate with an iterative walk.

Integrated event parses, recursive element documents, and recovery projection
used to recurse through the pattern graph once per authored level.  These beams
of depth complete, and a bounded parse agrees with the unlimited one.  The
shared deep-shape builders live in ``relaxng_limits_support``.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from tests.relaxng_limits_support import (
    DEEP_DEPTH,
    DEEP_DOCUMENT_DEPTH,
    INTEGRATED_EVENT_DEPTH,
    NS,
    compile_schema,
    deep_first_operand,
    deep_text_schema,
    nested_instance,
    parse,
    recursive_schema,
)

from pyxsd.relaxng import RelaxNGLimits, RelaxNGSchema


def deep_attribute_schema(depth: int = DEEP_DEPTH) -> RelaxNGSchema:
    inner = '<attribute name="a0"><text/></attribute>'
    for index in range(1, depth):
        inner = f'<group>{inner}<attribute name="a{index}"><text/></attribute></group>'
    source = f'<grammar xmlns="{NS}"><start><element name="r">{inner}</element></start></grammar>'
    return compile_schema(source)


def test_deep_event_parse_is_iterative() -> None:
    schema, instance = deep_first_operand("group", INTEGRATED_EVENT_DEPTH)

    document = parse(schema, instance)

    assert document.is_valid
    assert len(document.root.children) == INTEGRATED_EVENT_DEPTH + 1


def test_deep_recovery_projection_is_iterative() -> None:
    schema = deep_attribute_schema()

    document = parse(schema, "<r/>")

    assert document.is_valid is False
    assert document.report.has_errors
    assert document.root._name_ == "r"


def test_deep_recursive_element_documents_are_iterative() -> None:
    schema = recursive_schema()

    document = parse(schema, nested_instance("n", DEEP_DOCUMENT_DEPTH))

    assert document.is_valid
    depth = 0
    node = document.root
    while node.children:
        node = node.children[0]
        depth += 1
    assert depth == DEEP_DOCUMENT_DEPTH - 1


def test_deep_patterns_are_not_mistaken_for_invalid_documents() -> None:
    schema = deep_text_schema()
    instance = "<r><unexpected/></r>"

    unlimited = parse(schema, instance)
    bounded = parse(
        schema,
        instance,
        limits=RelaxNGLimits(max_derivative_states=10_000, max_memo_entries=10_000),
    )

    assert unlimited.is_valid is False
    assert bounded.is_valid is False
    assert [issue.code for issue in unlimited.report.issues] == [
        issue.code for issue in bounded.report.issues
    ]
    assert bounded.root.to_etree().tag == "r"


def test_deep_events_match_regular_validation_for_a_plain_tree() -> None:
    schema = recursive_schema()
    instance = ET.fromstring("<n><n/></n>")

    document = schema.parse(instance, typed=False)

    assert document.is_valid
    assert document.root._name_ == "n"
