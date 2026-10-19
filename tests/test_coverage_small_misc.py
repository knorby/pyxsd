"""Task 9: coverage for the small utility and writer modules.

The schema-driven tests pin user-visible contracts (a facet violation on
the report, the exported dict shape, the written output); the unit tests
exercise pure helpers and the fail-loud AST guards with small stub inputs,
mirroring ``tests/test_misc_coverage.py``.

The guards in ``xpath_subset``/``xpath_assertions`` exist to reject any AST
shape the current elementpath release does not produce (an upgrade that
reshapes the tree must fail loudly, never mis-parse), so they are driven
here with synthetic AST nodes whose class names match the guard's tables.
"""

from __future__ import annotations

import decimal
import io
import xml.etree.ElementTree as ET
from io import StringIO
from types import SimpleNamespace

import pytest

from pyxsd.binding import ParseModes
from pyxsd.dict_export import bound_to_dict
from pyxsd.facets import decimal_digits
from pyxsd.regex_charset import _complement, _scan_ranges, reject_malformed_escapes
from pyxsd.schema import Schema
from pyxsd.transforms.displayer import Displayer
from pyxsd.upa import _distinguishable
from pyxsd.validation import ValidationReport
from pyxsd.version_gates import check_xsd10_vocabulary
from pyxsd.versioning import apply_conditional_inclusion
from pyxsd.wildcards import WildcardSpec, _namespace_constraint
from pyxsd.writers.xml_tree_writer import XmlTreeWriter, _display_attribute
from pyxsd.xpath_assertions import _CTAChecker
from pyxsd.xpath_subset import (
    XPathError,
    _alternatives,
    _attribute_name,
    _step,
    _walk,
)
from pyxsd.xsi import invalid_xsi_nil_value

XSD_NS = "http://www.w3.org/2001/XMLSchema"
VC_NS = "http://www.w3.org/2007/XMLSchema-versioning"

SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
{body}
</xs:schema>"""


def _parse(body, xml, mode=ParseModes.NAMESPACED):
    return (
        Schema.compile(io.StringIO(SCHEMA.format(body=body)), mode=mode)
        .parse(io.StringIO(xml))
        .report
    )


# ---------------------------------------------------------------------------
# dict_export
# ---------------------------------------------------------------------------


def test_dict_export_primitive_root_uses_the_text_key():
    node = SimpleNamespace(_name_="r", _value_="plain", _children_=[], _attribs_={})
    assert bound_to_dict(node) == {"$": "plain"}


def test_dict_export_nilled_leaf_is_none():
    node = SimpleNamespace(_name_="r", _value_="v", _nil_=True, _children_=[], _attribs_={})
    assert bound_to_dict(node) == {"$": None}


def test_dict_export_missing_text_is_none_typed_and_empty_lexical():
    node = SimpleNamespace(_name_="r", _value_=None, _children_=[], _attribs_={})
    assert bound_to_dict(node, typed=True) == {"$": None}
    assert bound_to_dict(node, typed=False) == {"$": ""}


def test_dict_export_element_only_content_keeps_its_text():
    leaf = SimpleNamespace(_name_="a", _value_=["x"], _children_=[], _attribs_={})
    root = SimpleNamespace(_name_="r", _value_=["lead"], _children_=[leaf], _attribs_={})
    assert bound_to_dict(root) == {"a": "x", "$": "lead"}


# ---------------------------------------------------------------------------
# facets
# ---------------------------------------------------------------------------


def test_decimal_digits_collapses_leading_zeros():
    assert decimal_digits(decimal.Decimal("0.5")) == (1, 1)
    # Leading zeros a caller writes are not stored by Decimal, so the
    # pop loop in decimal_digits never finds a multi-digit zero prefix.
    assert decimal_digits("000.50") == (1, 1)
    assert decimal_digits(decimal.Decimal("100")) == (3, 0)


def test_length_facet_above_the_base_maximum_is_reported():
    body = (
        '<xs:simpleType name="A"><xs:restriction base="xs:string">'
        '<xs:maxLength value="3"/></xs:restriction></xs:simpleType>'
        '<xs:element name="r"><xs:simpleType><xs:restriction base="A">'
        '<xs:length value="5"/></xs:restriction></xs:simpleType></xs:element>'
    )
    report = _parse(body, "<r>abc</r>")
    assert any(
        "greater than the base type's maximum" in issue.message and issue.code == "facet"
        for issue in report.errors
    )


def test_length_facet_below_the_base_minimum_is_reported():
    body = (
        '<xs:simpleType name="A"><xs:restriction base="xs:string">'
        '<xs:minLength value="3"/></xs:restriction></xs:simpleType>'
        '<xs:element name="r"><xs:simpleType><xs:restriction base="A">'
        '<xs:length value="1"/></xs:restriction></xs:simpleType></xs:element>'
    )
    report = _parse(body, "<r>abc</r>")
    assert any(
        "less than the base type's minimum" in issue.message and issue.code == "facet"
        for issue in report.errors
    )


# ---------------------------------------------------------------------------
# xpath_api
# ---------------------------------------------------------------------------


def test_project_skips_a_child_that_cannot_be_named():
    from pyxsd.xpath_api import project

    nameless = SimpleNamespace(_name_=None, _children_=[], _attribs_={}, _value_=None)
    root = SimpleNamespace(_name_="r", _children_=[nameless], _attribs_={}, _value_="t")
    et_root, mapping = project(root)
    assert et_root.tag == "r"
    assert et_root.text == "t"
    assert list(et_root) == []
    assert mapping[et_root] is root


def test_project_requires_a_namable_root():
    from pyxsd.xpath_api import project

    node = SimpleNamespace(_name_=None, _children_=[], _attribs_={}, _value_=None)
    with pytest.raises(ValueError, match="element node"):
        project(node)


# ---------------------------------------------------------------------------
# xpath_subset: fail-loud AST guards (elementpath drift)
# ---------------------------------------------------------------------------


class _VerticalLineOperator(list):
    """Stand-in for elementpath's union AST node (matched by class name)."""


class _AttributeSymbol(list):
    """Stand-in for elementpath's attribute-axis AST node."""


class _ChildSymbol(list):
    """Stand-in for elementpath's child-axis AST node."""


def test_union_rejects_a_non_binary_node():
    with pytest.raises(XPathError, match="malformed union"):
        _alternatives(_VerticalLineOperator([object()]), None)


def test_union_nested_in_a_path_is_rejected():
    with pytest.raises(XPathError, match="top level"):
        _walk(_VerticalLineOperator([]), [], leading=True, default_ns=None)


def test_attribute_step_requires_a_single_name_test():
    with pytest.raises(XPathError, match="outside the identity-constraint"):
        _step(_AttributeSymbol([object(), object()]), [], None)


def test_child_step_requires_a_single_name_test():
    with pytest.raises(XPathError, match="outside the identity-constraint"):
        _step(_ChildSymbol([]), [], None)


def test_attribute_name_rejects_a_non_name_test():
    with pytest.raises(XPathError, match="not a valid attribute step"):
        _attribute_name(_ChildSymbol([]))


# ---------------------------------------------------------------------------
# xpath_assertions: CTA fail-loud AST guards (elementpath drift)
# ---------------------------------------------------------------------------


class _NotFunction(list):
    """Stand-in for elementpath's ``not()`` function token."""


class _AndOperator(list):
    """Stand-in for elementpath's binary ``and`` operator token."""


def test_cta_rejects_a_malformed_not_call():
    with pytest.raises(XPathError, match=r"malformed not\(\)"):
        _CTAChecker()._visit(_NotFunction([]))


def test_cta_rejects_a_malformed_boolean_operator():
    with pytest.raises(XPathError, match="malformed _AndOperator"):
        _CTAChecker()._visit(_AndOperator([object()]))


def test_cta_rejects_a_malformed_cast():
    with pytest.raises(XPathError, match="malformed cast"):
        _CTAChecker()._check_cast(_AndOperator([object()]))


def test_cta_rejects_a_malformed_constructor_call():
    with pytest.raises(XPathError, match="exactly one"):
        _CTAChecker()._check_constructor(_AndOperator([]))


def test_cta_rejects_a_non_qname_cast_target():
    with pytest.raises(XPathError, match="cast target"):
        _CTAChecker()._check_type_name(_AndOperator([]))


def test_declared_types_skip_an_attribute_that_has_no_name():
    """A nameless attribute is left untyped; a named one is stamped."""
    from pyxsd.xpath_assertions import _typed_node_tree

    marker = object()
    node = ET.Element("a", {"b": "1", None: "x"})
    tree = _typed_node_tree(node, {"b": marker}, None, None)

    stamped = {attribute.name: attribute.xsd_type for attribute in tree.attributes}
    assert stamped["b"] is marker
    assert stamped[None] is None


# ---------------------------------------------------------------------------
# regex_charset
# ---------------------------------------------------------------------------


def test_scan_ranges_keeps_a_run_open_at_the_end():
    assert _scan_ranges(lambda codepoint: True) == [(0, 0x10FFFF)]


def test_complement_of_the_full_range_is_empty():
    assert _complement([(0, 0x10FFFF)]) == []


def test_unterminated_block_escape_is_tolerated():
    assert reject_malformed_escapes(r"\p{abc") is None
    assert reject_malformed_escapes(r"\P{Is") is None


# ---------------------------------------------------------------------------
# version_gates
# ---------------------------------------------------------------------------


def test_gate_skips_comments_foreign_elements_and_non_string_attribute_names():
    root = ET.Element(f"{{{XSD_NS}}}schema")
    root.append(ET.Comment("a note"))
    ET.SubElement(root, "{urn:other}assert")
    flagged = ET.SubElement(root, f"{{{XSD_NS}}}assert")
    flagged.attrib[("not", "a", "string")] = "value"

    report = ValidationReport()
    check_xsd10_vocabulary(root, report)

    assert [issue.element for issue in report.errors] == ["assert"]
    assert all(issue.code == "xsd11-construct" for issue in report.errors)


# ---------------------------------------------------------------------------
# writers/xml_tree_writer
# ---------------------------------------------------------------------------


def test_display_attribute_leaves_a_foreign_clark_name_alone():
    assert _display_attribute("{urn:x}a", None) == "{urn:x}a"


def test_display_attribute_uses_the_implicit_xml_prefix():
    key = "{http://www.w3.org/XML/1998/namespace}space"
    assert _display_attribute(key, None) == "xml:space"


def test_unqualified_root_declares_xsi_for_an_xsi_attribute():
    output = StringIO()
    element = SimpleNamespace(
        _name_="r", _attribs_={"xsi:nil": "true"}, _children_=[], _value_=None
    )
    XmlTreeWriter.passTagToTagWriter(element, 0, output, None, None)

    assert 'xmlns:xsi = "http://www.w3.org/2001/XMLSchema-instance"' in output.getvalue()
    assert 'xsi:nil   = "true"' in output.getvalue()


def test_single_comment_child_closes_the_element_without_an_end_tag():
    comment = SimpleNamespace(_name_="_comment_", _attribs_={}, _children_=[], _value_="note")
    root = SimpleNamespace(_name_="r", _attribs_={}, _children_=[comment], _value_=None)
    output = StringIO()
    XmlTreeWriter(root, output)

    written = output.getvalue()
    assert "<!--note-->" in written
    assert "</r>" not in written


# ---------------------------------------------------------------------------
# transforms/displayer
# ---------------------------------------------------------------------------


def _display_tree():
    return SimpleNamespace(_name_="root", _attribs_={}, _children_=[], _value_="hi")


class _TreeDisplayer(Displayer):
    """A concrete ``Displayer`` over a synthetic tree."""

    def __init__(self, root):
        super().__init__(root)


class _FailingDisplayer(Displayer):
    """A ``Displayer`` whose serialization always fails."""

    def __init__(self, root):
        super().__init__(root)
        self.attempted: io.IOBase | None = None

    def writeTree(self, file):
        self.attempted = file
        raise RuntimeError("serialization failed")


def test_make_temp_file_of_tree_writes_the_tree_and_rewinds():
    stream = _TreeDisplayer(_display_tree()).makeTempFileOfTree()
    try:
        text = stream.read()
    finally:
        stream.close()

    assert text.startswith("<!--File created by PyXSD")
    assert "<root>hi</root>" in text


def test_make_temp_file_of_tree_closes_the_stream_when_writing_fails():
    displayer = _FailingDisplayer(_display_tree())
    with pytest.raises(RuntimeError, match="serialization failed"):
        displayer.makeTempFileOfTree()

    assert displayer.attempted is not None
    assert displayer.attempted.closed


# ---------------------------------------------------------------------------
# versioning
# ---------------------------------------------------------------------------


def test_ignored_schema_root_keeps_only_the_version_attributes():
    from pyxsd.namespaces import NamespaceContext, clark

    root = ET.Element(f"{{{XSD_NS}}}schema")
    root.set(clark(VC_NS, "minVersion"), "2.0")
    root.set("elementFormDefault", "qualified")
    root.set("targetNamespace", "urn:t")
    ET.SubElement(root, f"{{{XSD_NS}}}element")

    apply_conditional_inclusion(root, NamespaceContext(), ValidationReport())

    assert root.attrib == {
        clark(VC_NS, "minVersion"): "2.0",
        "targetNamespace": "urn:t",
    }
    assert list(root) == []


# ---------------------------------------------------------------------------
# upa / wildcards / alternatives
# ---------------------------------------------------------------------------


def test_distinguishable_when_one_path_is_contained_in_the_other():
    from pyxsd.content_model import Particle

    leaf = Particle("element", 1, 1, [], name="a")
    other = Particle("element", 1, 1, [], name="b")
    # Every node of path1 appears (by identity) in path2, so the shared
    # ancestor is the single-element path1 itself.
    assert _distinguishable([leaf], [leaf, other]) is False


def test_not_namespace_ignores_a_reserved_keyword_token():
    spec = WildcardSpec(not_namespace=frozenset({"##other"}))
    assert _namespace_constraint(spec, None) == ("not", frozenset())


def test_misplaced_alternative_is_reported_as_a_declaration_child():
    body = (
        '<xs:complexType name="t"><xs:alternative test="true()" type="xs:string"/></xs:complexType>'
    )
    report = _parse(body, "<t/>")
    assert any(issue.code == "declaration-child" for issue in report.errors)


def test_alternative_registers_only_when_its_container_has_a_slot():
    """An alternative under a container without an ``alternatives`` list.

    The real parser only ever builds alternatives under an element
    declaration (which allocates the list); this pins the tolerant
    registration path for any other container, mirroring the detached
    stubs used for ``Notation`` in ``test_misc_coverage``.
    """
    import pyxsd.schema  # noqa: F401  (first import loads the ER stack safely)
    from pyxsd.alternatives import AlternativeER

    class _DetachedParent:
        def findLayerNum(self):
            return 1

        def getSchema(self):
            return None

        def getNamespace(self):
            return None

        def getContainingType(self):
            return None

    element = ET.fromstring(
        '<xs:alternative xmlns:xs="http://www.w3.org/2001/XMLSchema"'
        ' test="true()" type="xs:string"/>'
    )
    parent = _DetachedParent()
    alternative = AlternativeER(element, parent)

    assert not hasattr(parent, "alternatives")
    assert alternative.test == "true()"
    assert alternative.type_name == "xs:string"


# ---------------------------------------------------------------------------
# xsi
# ---------------------------------------------------------------------------


def test_invalid_xsi_nil_value_of_none_is_valid():
    assert invalid_xsi_nil_value(None) is None
    assert invalid_xsi_nil_value("maybe") == "maybe"
    assert invalid_xsi_nil_value(" TRUE ") is None
