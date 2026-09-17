"""Direct unit tests for ``pyxsd.identity`` internals.

The schema-level identity tests exercise the happy paths; these pin the
bound-node accessors and the keyref/field skip branches that the corpus
does not reach (unsupported selectors and fields, missing descriptors,
raw attribute fallback, simple-content value extraction), plus the
schema-phase declaration legality of key/unique/keyref and their
selector/field children.
"""

import io
from types import SimpleNamespace

import pytest

from pyxsd.binding import ParseModes
from pyxsd.identity import (
    _attributeValue,
    _checkKeyref,
    _formatValues,
    _nameOf,
    _nodeValue,
    check_identity_constraints,
)
from pyxsd.parser import PyXSD
from pyxsd.schema_base import SchemaBase
from pyxsd.validation import IssueSeverity, ValidationReport
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


# --- Schema-phase declaration legality (key/unique/keyref) ------------

ROOT_OPEN = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="root"><xs:complexType><xs:sequence>'
    '<xs:element name="c" maxOccurs="10"/>'
    "</xs:sequence></xs:complexType>"
)
ROOT_CLOSE = "</xs:element></xs:schema>"


@pytest.fixture
def schema_report(tmp_path, monkeypatch):
    """Parses a schema without an instance and returns its schema-phase errors.

    Mirrors the ``parse_schema`` fixture of ``tests/test_schema_legality.py``.
    A constraint fragment is embedded in a minimal root element; a full
    ``<xs:schema>`` document is used verbatim.
    """
    monkeypatch.setattr(PyXSD, "parseXML", lambda self: None)
    schema_path = tmp_path / "schema.xsd"

    def _parse(constraint: str):
        if constraint.lstrip().startswith("<xs:schema"):
            schema = constraint
        else:
            schema = ROOT_OPEN + constraint + ROOT_CLOSE
        schema_path.write_text(schema, encoding="utf-8")
        report = PyXSD(
            io.StringIO("<pyxsd-schema-probe/>"),
            str(schema_path),
            xmlFileOutput=False,
            mode=ParseModes.NAMESPACED,
        ).report
        return [
            issue for issue in report.for_phase("schema") if issue.severity is IssueSeverity.ERROR
        ]

    return _parse


def _codes(errors):
    return {issue.code for issue in errors}


def test_unique_with_a_bogus_attribute_is_a_declaration_attribute(schema_report):
    errors = schema_report(
        '<xs:unique name="u1" bogus="1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
    )
    assert "declaration-attribute" in _codes(errors)


def test_key_without_a_name_is_a_declaration_name(schema_report):
    errors = schema_report('<xs:key><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:key>')
    assert "declaration-name" in _codes(errors)


def test_keyref_without_refer_is_a_declaration_attribute(schema_report):
    errors = schema_report(
        '<xs:keyref name="r1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:keyref>'
    )
    assert "declaration-attribute" in _codes(errors)


def test_selector_rejects_children_other_than_annotation(schema_report):
    errors = schema_report(
        '<xs:unique name="u1"><xs:selector xpath="c"><xs:field xpath="@id"/>'
        '</xs:selector><xs:field xpath="@id"/></xs:unique>'
    )
    assert "declaration-child" in _codes(errors)


def test_field_rejects_attributes_other_than_xpath(schema_report):
    errors = schema_report(
        '<xs:unique name="u1"><xs:selector xpath="c"/>'
        '<xs:field xpath="@id" name="fooID"/></xs:unique>'
    )
    assert "declaration-attribute" in _codes(errors)


def test_two_constraints_may_not_share_a_name(schema_report):
    errors = schema_report(
        '<xs:unique name="u1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
        '<xs:unique name="u1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
    )
    assert "declaration-duplicate" in _codes(errors)


def test_a_second_selector_child_is_a_declaration_child(schema_report):
    errors = schema_report(
        '<xs:unique name="u1"><xs:selector xpath="c"/><xs:selector xpath="."/>'
        '<xs:field xpath="@id"/></xs:unique>'
    )
    assert "declaration-child" in _codes(errors)


def test_constraint_name_must_be_an_ncname(schema_report):
    errors = schema_report(
        '<xs:unique name="a:b"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
    )
    assert "declaration-attribute" in _codes(errors)


def test_selector_rejects_attributes_other_than_xpath(schema_report):
    errors = schema_report(
        '<xs:unique name="u1"><xs:selector xpath="c" name="fooID"/>'
        '<xs:field xpath="@id"/></xs:unique>'
    )
    assert "declaration-attribute" in _codes(errors)


def test_constraint_inside_a_group_is_a_declaration_child(schema_report):
    """The misplacement is reported even though the constraint's parent
    compositor accepts any child (``group`` is never its legal home)."""
    errors = schema_report(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:group name="g"><xs:sequence>'
        '<xs:unique name="u1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
        "</xs:sequence></xs:group>"
        '<xs:element name="root"><xs:complexType><xs:sequence>'
        '<xs:element name="c" maxOccurs="10"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    assert "declaration-child" in _codes(errors)


def test_two_annotation_children_are_a_duplicate(schema_report):
    errors = schema_report(
        '<xs:unique name="u1"><xs:annotation/><xs:annotation/>'
        '<xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
    )
    assert "declaration-duplicate" in _codes(errors)


def test_field_before_selector_is_out_of_order(schema_report):
    errors = schema_report(
        '<xs:unique name="u1"><xs:field xpath="@id"/><xs:selector xpath="c"/></xs:unique>'
    )
    assert "declaration-order" in _codes(errors)


def test_key_without_a_field_is_a_declaration_child(schema_report):
    errors = schema_report('<xs:key name="k1"><xs:selector xpath="c"/></xs:key>')
    assert "declaration-child" in _codes(errors)


def test_unique_without_a_selector_is_a_declaration_child(schema_report):
    errors = schema_report('<xs:unique name="u1"><xs:field xpath="@id"/></xs:unique>')
    assert "declaration-child" in _codes(errors)


def test_selector_with_an_empty_xpath_is_a_declaration_attribute(schema_report):
    errors = schema_report(
        '<xs:unique name="u1"><xs:selector xpath=""/><xs:field xpath="@id"/></xs:unique>'
    )
    assert "declaration-attribute" in _codes(errors)


def test_xpath_default_namespace_is_a_reserved_legal_attribute(schema_report):
    errors = schema_report(
        '<xs:unique name="u1" xpathDefaultNamespace="##local">'
        '<xs:selector xpath="c" xpathDefaultNamespace="##local"/>'
        '<xs:field xpath="@id"/></xs:unique>'
    )
    assert not errors


def test_xsd11_constraint_reference_site_is_legal(schema_report):
    """``<xs:unique ref="..."/>`` borrows the referred constraint's
    name, selector and fields and carries none of them itself."""
    errors = schema_report(
        '<xs:unique name="u1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
        '<xs:unique ref="u1"><xs:annotation><xs:documentation/></xs:annotation></xs:unique>'
        '<xs:key ref="u1"/>'
        '<xs:keyref ref="u1"/>'
    )
    assert not errors


def test_constraint_reference_site_rejects_bogus_attributes(schema_report):
    errors = schema_report(
        '<xs:unique name="u1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
        '<xs:unique ref="u1" bogus="1"/>'
    )
    assert "declaration-attribute" in _codes(errors)
