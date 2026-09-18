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
from pyxsd.xpath_subset import XPathError, parse_xpath_subset
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
    name, selector and fields and carries none of them itself; each
    ref site must name a constraint of its own category (§3.11.3.5)."""
    errors = schema_report(
        '<xs:unique name="u1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
        '<xs:key name="k1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:key>'
        '<xs:keyref name="r1" refer="k1"><xs:selector xpath="c"/>'
        '<xs:field xpath="@id"/></xs:keyref>'
        '<xs:unique ref="u1"><xs:annotation><xs:documentation/></xs:annotation></xs:unique>'
        '<xs:key ref="k1"/>'
        '<xs:keyref ref="r1"/>'
    )
    assert not errors


def test_constraint_reference_site_rejects_bogus_attributes(schema_report):
    errors = schema_report(
        '<xs:unique name="u1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
        '<xs:unique ref="u1" bogus="1"/>'
    )
    assert "declaration-attribute" in _codes(errors)


# --- XPath subset: elementpath parse → whitelist → translate ------------


def test_xpath_union_split():
    parsed = parse_xpath_subset("a | @b", {}, None, None)
    assert len(parsed.alternatives) == 2
    assert parsed.alternatives == (
        (False, (("element", "a"),)),
        (False, (("attribute", "b"),)),
    )


def test_xpath_default_namespace_target():
    """``xpathDefaultNamespace`` resolution: an unprefixed element step
    names the given default namespace in Clark form."""
    parsed = parse_xpath_subset("c", {}, "urn:tns", "urn:tns")
    assert parsed.alternatives == ((False, (("element", "{urn:tns}c"),)),)


def test_xpath_default_namespace_absent_resolves_to_no_namespace():
    parsed = parse_xpath_subset("c", {}, None, "urn:tns")
    assert parsed.alternatives == ((False, (("element", "c"),)),)


def test_xpath_prefixed_names_ignore_the_default_namespace():
    parsed = parse_xpath_subset("x:c/@x:id", {"x": "urn:x"}, "urn:default", None)
    assert parsed.alternatives == (
        (
            False,
            (
                ("element", "{urn:x}c"),
                ("attribute", "{urn:x}id"),
            ),
        ),
    )


def test_xpath_unprefixed_attribute_step_stays_in_no_namespace():
    parsed = parse_xpath_subset("@id", {}, "urn:tns", "urn:tns")
    assert parsed.alternatives == ((False, (("attribute", "id"),)),)


#: Every admitted path shape with its exact translation. An elementpath
#: release that reshapes its AST fails this table loudly in CI instead
#: of silently mis-parsing (the unrecognized-node guard rejects, this
#: table catches the admitted-shape drift).
COMPAT_SHAPES = [
    ("a", {}, None, ((False, (("element", "a"),)),)),
    ("x:a", {"x": "urn:x"}, None, ((False, (("element", "{urn:x}a"),)),)),
    ("*", {}, None, ((False, (("element", "*"),)),)),
    ("x:*", {"x": "urn:x"}, None, ((False, (("element", "{urn:x}*"),)),)),
    (".", {}, None, ((False, (("self",),)),)),
    ("./a", {}, None, ((False, (("self",), ("element", "a"))),)),
    ("a/.", {}, None, ((False, (("element", "a"), ("self",))),)),
    ("a/b", {}, None, ((False, (("element", "a"), ("element", "b"))),)),
    ("a/b/.", {}, None, ((False, (("element", "a"), ("element", "b"), ("self",))),)),
    ("./a/b", {}, None, ((False, (("self",), ("element", "a"), ("element", "b"))),)),
    ("@b", {}, None, ((False, (("attribute", "b"),)),)),
    ("@x:b", {"x": "urn:x"}, None, ((False, (("attribute", "{urn:x}b"),)),)),
    ("@*", {}, None, ((False, (("attribute", "*"),)),)),
    ("@ *", {}, None, ((False, (("attribute", "*"),)),)),
    ("@x:*", {"x": "urn:x"}, None, ((False, (("attribute", "{urn:x}*"),)),)),
    ("child::a", {}, None, ((False, (("element", "a"),)),)),
    ("child::*", {}, None, ((False, (("element", "*"),)),)),
    ("child::x:a", {"x": "urn:x"}, None, ((False, (("element", "{urn:x}a"),)),)),
    ("child::x:*", {"x": "urn:x"}, None, ((False, (("element", "{urn:x}*"),)),)),
    ("attribute::a", {}, None, ((False, (("attribute", "a"),)),)),
    ("attribute::*", {}, None, ((False, (("attribute", "*"),)),)),
    ("attribute::x:a", {"x": "urn:x"}, None, ((False, (("attribute", "{urn:x}a"),)),)),
    ("attribute::x:*", {"x": "urn:x"}, None, ((False, (("attribute", "{urn:x}*"),)),)),
    (".//a", {}, None, ((True, (("element", "a"),)),)),
    (".//*", {}, None, ((True, (("element", "*"),)),)),
    (".//x:a", {"x": "urn:x"}, None, ((True, (("element", "{urn:x}a"),)),)),
    (".//a/b", {}, None, ((True, (("element", "a"), ("element", "b"))),)),
    (".//a/@b", {}, None, ((True, (("element", "a"), ("attribute", "b"))),)),
    (".//./a", {}, None, ((True, (("self",), ("element", "a"))),)),
    (".//.", {}, None, ((True, (("self",),)),)),
    (".//@*", {}, None, ((True, (("attribute", "*"),)),)),
    (
        "a | @b",
        {},
        None,
        ((False, (("element", "a"),)), (False, (("attribute", "b"),))),
    ),
    (
        "a|b|c",
        {},
        None,
        (
            (False, (("element", "a"),)),
            (False, (("element", "b"),)),
            (False, (("element", "c"),)),
        ),
    ),
    (
        ".//x:a | @y:b",
        {"x": "urn:x", "y": "urn:y"},
        None,
        ((True, (("element", "{urn:x}a"),)), (False, (("attribute", "{urn:y}b"),))),
    ),
]


@pytest.mark.parametrize(("xpath", "namespaces", "default_ns", "expected"), COMPAT_SHAPES)
def test_xpath_elementpath_compat_shapes(xpath, namespaces, default_ns, expected):
    parsed = parse_xpath_subset(xpath, namespaces, default_ns, None)
    assert parsed.alternatives == expected


#: Expressions outside the subset: corpus-illegal shapes (XSTS idI*/idJ*)
#: and constructs the whitelist rejects outright.
REJECTED_PATHS = [
    "",
    "   ",
    "/a",
    "/",
    "//",
    "//a",
    ".//",
    "a/",
    "a//b",
    ".//a//b",
    "a[b]",
    "a[1]",
    "self::*",
    "self::node()",
    "descendant::*",
    "descendant-or-self::*",
    "document('')",
    "a/@b/c",
    "@a/.",
    "child::",
    "attribute::",
    "child: :a",
    ".///@*",
    "x:a",
    "(a|b)/c",
    "a .//b",
    "@id |",
    "text()",
]


@pytest.mark.parametrize("xpath", REJECTED_PATHS)
def test_xpath_outside_the_subset_raises_xpath_error(xpath):
    with pytest.raises(XPathError):
        parse_xpath_subset(xpath, {}, None, None)


def test_xpath_invalid_mid_descendant_selector(schema_report):
    errors = schema_report(
        '<xs:key name="k"><xs:selector xpath="a//b"/><xs:field xpath="@id"/></xs:key>'
    )
    assert "xpath-invalid" in _codes(errors)


def test_xpath_invalid_absolute_selector(schema_report):
    errors = schema_report(
        '<xs:key name="k"><xs:selector xpath="/a"/><xs:field xpath="@id"/></xs:key>'
    )
    assert "xpath-invalid" in _codes(errors)


def test_xpath_invalid_predicate_field(schema_report):
    errors = schema_report(
        '<xs:key name="k"><xs:selector xpath="a"/><xs:field xpath="a[b]"/></xs:key>'
    )
    assert "xpath-invalid" in _codes(errors)


def test_xpath_invalid_self_axis_selector(schema_report):
    errors = schema_report(
        '<xs:key name="k"><xs:selector xpath="self::*"/><xs:field xpath="@id"/></xs:key>'
    )
    assert "xpath-invalid" in _codes(errors)


def test_xpath_invalid_unbound_prefix_field(schema_report):
    errors = schema_report(
        '<xs:key name="k"><xs:selector xpath="a"/><xs:field xpath="imp:iid"/></xs:key>'
    )
    assert "xpath-invalid" in _codes(errors)


def test_xpath_invalid_function_field(schema_report):
    errors = schema_report(
        '<xs:key name="k"><xs:selector xpath="a"/><xs:field xpath="document(\'\')"/></xs:key>'
    )
    assert "xpath-invalid" in _codes(errors)


def test_xpath_default_namespace_target_namespace_is_threaded(schema_report):
    """``xpathDefaultNamespace="##targetNamespace"`` resolves the
    unprefixed selector against the schema's target namespace."""
    errors = schema_report(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:root" xmlns="urn:root">'
        '<xs:element name="root"><xs:complexType><xs:sequence>'
        '<xs:element name="c" maxOccurs="10"/>'
        "</xs:sequence></xs:complexType>"
        '<xs:unique name="u1" xpathDefaultNamespace="##targetNamespace">'
        '<xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
        "</xs:element></xs:schema>"
    )
    assert not [issue for issue in errors if issue.code == "xpath-invalid"]


def test_xpath_default_namespace_schema_level_is_inherited(schema_report):
    errors = schema_report(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xpathDefaultNamespace="##local">'
        '<xs:element name="root"><xs:complexType><xs:sequence>'
        '<xs:element name="c" maxOccurs="10"/>'
        "</xs:sequence></xs:complexType>"
        '<xs:unique name="u1"><xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
        "</xs:element></xs:schema>"
    )
    assert not [issue for issue in errors if issue.code == "xpath-invalid"]


def test_xpath_default_namespace_bogus_keyword_is_xpath_invalid(schema_report):
    errors = schema_report(
        '<xs:unique name="u1" xpathDefaultNamespace="##bogus">'
        '<xs:selector xpath="c"/><xs:field xpath="@id"/></xs:unique>'
    )
    assert "xpath-invalid" in _codes(errors)
