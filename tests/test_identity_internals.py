"""Direct unit tests for ``pyxsd.identity`` internals.

The schema-level identity tests exercise the happy paths; these pin the
bound-node accessors and the keyref/field skip branches that the corpus
does not reach (unsupported selectors and fields, missing descriptors,
raw attribute fallback, simple-content value extraction).
"""

from types import SimpleNamespace

from pyxsd.identity import (
    _attributeValue,
    _checkKeyref,
    _formatValues,
    _nameOf,
    _nodeValue,
    check_identity_constraints,
)
from pyxsd.schema_base import SchemaBase
from pyxsd.validation import ValidationReport
from pyxsd.xsd_data_types import XsdDataType


class _Constraint:
    def __init__(self, name, refer, selector, fields):
        self.constraintName = name
        self.refer = refer
        self.selector = selector
        self.fieldPaths = fields


def _node(name, children=(), **attributes):
    node = SimpleNamespace(
        _name_=name,
        _children_=list(children),
        _descriptor_=None,
        _nil_=False,
        _attribs_=attributes,
    )
    return node


def test_check_identity_constraints_without_a_root_is_a_noop():
    assert check_identity_constraints(None, ValidationReport()) is None


def test_walk_ignores_identity_constraint_kinds_it_does_not_know():
    class _Declaration:
        identities = (object(),)

    root = _node("root")
    root._descriptor_ = _Declaration()
    report = ValidationReport()
    check_identity_constraints(root, report)
    assert not report.issues


def test_keyref_with_an_unsupported_selector_stops_after_the_warning():
    child = _node("item")
    root = _node("root", [child])
    constraint = _Constraint("ref", "k", "item[x]", ["a"])
    report = ValidationReport()
    _checkKeyref(constraint, root, ({"k": set()},), report)
    assert any("predicate" in issue.message for issue in report.issues)


def test_keyref_with_an_unsupported_field_stops_after_the_warning():
    attribute = _node("a")
    item = _node("item", [attribute])
    root = _node("root", [item])
    constraint = _Constraint("ref", "k", "item", ["a[x]"])
    report = ValidationReport()
    _checkKeyref(constraint, root, ({"k": set()},), report)
    assert any("predicate" in issue.message for issue in report.issues)


def test_keyref_with_a_missing_field_value_is_simply_absent():
    item = _node("item")
    root = _node("root", [item])
    constraint = _Constraint("ref", "k", "item", ["a"])
    report = ValidationReport()
    _checkKeyref(constraint, root, ({"k": set()},), report)
    assert not report.issues


def test_name_of_a_node_without_a_descriptor_uses_its_class_name():
    node = SimpleNamespace(_name_=None, _descriptor_=None)
    assert _nameOf(node) == "SimpleNamespace"


def test_name_of_a_bare_data_type_value():
    node = object.__new__(XsdDataType)
    assert _nameOf(node) == "?"


def test_attribute_value_falls_back_to_the_raw_attribute_table():
    node = SimpleNamespace(_attribs_={"identifier": "abc"})
    assert _attributeValue(node, "identifier") == "abc"


def test_attribute_value_of_a_missing_attribute_is_none():
    node = SimpleNamespace(_attribs_={})
    assert _attributeValue(node, "identifier") is None


def test_node_value_of_a_simple_content_element():
    node = object.__new__(SchemaBase)
    node._nil_ = False
    node._value_ = ["text"]
    assert _nodeValue(node) == "text"


def test_node_value_of_an_empty_simple_content_element_is_none():
    node = object.__new__(SchemaBase)
    node._nil_ = False
    node._value_ = []
    assert _nodeValue(node) is None


def test_format_values_of_a_composite_key_is_parenthesized():
    assert _formatValues(("a", "b")) == "('a', 'b')"
