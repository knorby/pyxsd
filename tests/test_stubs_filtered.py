"""Filtered views select the semantic inventory, never mutate class tables."""

import pytest

from pyxsd.stubs import emit_stub
from test_introspection import compile_schema


@pytest.mark.parametrize("compact", [False, True])
def test_filtering_retains_selected_type_without_mutating_schema(compact):
    schema = compile_schema()
    original_classes = dict(schema.classes)
    original = emit_stub(schema, compact=compact)
    filtered = emit_stub(schema, compact=compact, component_ids=("type:{urn:demo}Small",), depth=0)
    assert "class Small(" in filtered and "class Node(" not in filtered
    assert schema.classes == original_classes
    assert emit_stub(schema, compact=compact) == original


def test_root_selection_traverses_type_dependencies_and_limits_root_aliases():
    schema = compile_schema()
    filtered = emit_stub(schema, component_ids=("element:{urn:demo}root",), depth=1)
    assert "class Node(" in filtered
    assert "class Derived(" not in filtered
    assert "root: type[Node]" in filtered
    assert "alternate:" not in filtered and "other:" not in filtered


def test_explicit_empty_selection_and_invalid_filters():
    schema = compile_schema()
    assert "class " not in emit_stub(schema, component_ids=())
    with pytest.raises(KeyError):
        emit_stub(schema, component_ids=("type:missing",))
    with pytest.raises(ValueError, match="depth"):
        emit_stub(schema, component_ids=("type:{urn:demo}Node",), depth=-1)


def test_schema_stub_exposes_the_same_filter_contract():
    schema = compile_schema()
    selected = ("type:{urn:demo}Small",)
    assert schema.stub(component_ids=selected, depth=0) == emit_stub(
        schema, component_ids=selected, depth=0
    )


def test_duplicate_root_selection_uses_the_requested_declaration():
    from pyxsd.introspection import describe_schema

    schema = compile_schema(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="root" type="xs:string"/>'
        '<xs:element name="root" type="xs:int"/></xs:schema>'
    )
    elements = {
        item.location.path[-1].index: item
        for item in describe_schema(schema).components
        if item.kind == "element"
    }
    first = emit_stub(schema, component_ids=(elements[1].id,), depth=0)
    second = emit_stub(schema, component_ids=(elements[2].id,), depth=0)
    assert "root: type[String]" in first
    assert "root: type[Int]" not in first
    assert "root: type[Int]" in second
    assert "root: type[String]" not in second
