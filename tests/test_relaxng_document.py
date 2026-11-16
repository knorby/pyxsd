"""Generic document integration for validation-only RELAX NG documents.

These tests pin the shared-document behavior RELAX NG inherits and extends:
pre-order traversal, XPath and ElementTree queries mapped back to the
authoritative lexical nodes, mixed text/tail visibility, character-preserving
writers, an unannotated XDM view, stale root-callable transform documents,
whole-document transform dispatch, and revalidation from an independent clone
that keeps namespace and base-URI frames without refreshing old evidence.
"""

from __future__ import annotations

import io
import os
from xml.etree import ElementTree as ET

import pytest

from pyxsd.document_transform import DocumentTransform
from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.relaxng import (
    RelaxNGDocument,
    RelaxNGElement,
    RelaxNGLimits,
    RelaxNGSchema,
)
from pyxsd.relaxng.document import _restore_lexical_frames
from pyxsd.relaxng.xml_writer import clone_relaxng
from pyxsd.xdm.annotations import UNTYPED, UNTYPED_ATOMIC
from pyxsd.xdm.errors import XdmInputError

NS = "http://relaxng.org/ns/structure/1.0"
XSD_DATATYPES = "http://www.w3.org/2001/XMLSchema-datatypes"

SIMPLE_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <optional><element name="item"><text/></element></optional>
    </element>
  </start>
</grammar>"""

MIXED_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <mixed>
        <zeroOrMore><element name="item"><text/></element></zeroOrMore>
      </mixed>
    </element>
  </start>
</grammar>"""

CONTEXT_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <optional>
        <attribute name="kind">
          <data type="QName" datatypeLibrary="{XSD_DATATYPES}"/>
        </attribute>
      </optional>
      <mixed>
        <zeroOrMore><element name="item"><empty/></element></zeroOrMore>
      </mixed>
    </element>
  </start>
</grammar>"""

INTEGER_ATTRIBUTE_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <attribute name="n">
        <data type="integer" datatypeLibrary="{XSD_DATATYPES}"/>
      </attribute>
      <empty/>
    </element>
  </start>
</grammar>"""

UNCONSTRAINED_ATTRIBUTES_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <zeroOrMore><attribute><anyName/></attribute></zeroOrMore>
      <mixed>
        <zeroOrMore><element name="item"><empty/></element></zeroOrMore>
      </mixed>
    </element>
  </start>
</grammar>"""


def _schema(source: str = SIMPLE_SCHEMA) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(io.StringIO(source))
    assert not schema.report.has_errors, schema.report
    return schema


def _document(xml: str, *, schema: RelaxNGSchema | None = None) -> RelaxNGDocument:
    parsed = (schema or _schema()).parse(io.StringIO(xml), typed=False)
    assert isinstance(parsed, RelaxNGDocument)
    return parsed


class _RecordingTransform:
    """A whole-document transform that never needs an XSD document shape."""

    def __init__(self) -> None:
        self.seen: object | None = None

    def transform_document(self, document: object) -> object:
        self.seen = document
        return document


class _RawTransform:
    """A whole-document transform whose result is not a document."""

    def transform_document(self, document: object) -> object:
        del document
        return "raw"


def test_walk_yields_the_original_nodes_pre_order():
    document = _document("<doc>a<item>x</item>b<item>y</item></doc>", schema=_schema(MIXED_SCHEMA))
    first, second = document.root.children

    walked = list(document.walk())

    assert walked == [document.root, first, second]
    assert walked[0] is document.root
    assert walked[1] is first
    assert walked[2] is second


def test_xpath_and_elementtree_queries_map_to_the_original_nodes():
    document = _document("<doc>a<item>x</item>b<item>y</item></doc>", schema=_schema(MIXED_SCHEMA))
    first, second = document.root.children

    selected = document.xpath("//item")
    assert selected == [first, second]
    assert selected[0] is first
    assert selected[1] is second
    assert document.find("item") is first
    found_all = document.findall("item")
    assert found_all == [first, second]
    assert document.xpath("count(//item)") == 2


def test_mixed_text_and_tails_remain_queryable():
    document = _document("<doc>a<item>x</item>b<item>y</item></doc>", schema=_schema(MIXED_SCHEMA))

    assert document.xpath("//text()") == ["a", "x", "b", "y"]
    assert document.xpath("//item[1]/following-sibling::text()") == ["b"]
    assert document.xpath("string(/doc)") == "axby"

    document.root.children[0].set_text("changed")
    document.root.children[0].set_tail(" tail ")
    assert document.xpath("//text()") == ["a", "changed", " tail ", "y"]


def test_writers_preserve_character_values_and_escaped_markup(tmp_path):
    document = _document(
        "<doc>caf\u00e9<item>\u4f60\u597d</item> tail &amp; &lt;</doc>",
        schema=_schema(MIXED_SCHEMA),
    )
    expected = "<doc>caf\u00e9<item>\u4f60\u597d</item> tail &amp; &lt;</doc>"

    assert document.to_string() == expected

    path = tmp_path / "mixed.xml"
    document.write(path)
    assert path.read_text(encoding="utf-8") == expected

    document.root.children[0].set_text("na\u00efve & <")
    rewritten = "<doc>caf\u00e9<item>na\u00efve &amp; &lt;</item> tail &amp; &lt;</doc>"
    assert document.to_string() == rewritten


def test_xdm_is_an_unannotated_xml_tree():
    xml = (
        '<doc xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xsi:nil="true" xsi:type="xs:string"><item/></doc>'
    )
    schema = _schema(UNCONSTRAINED_ATTRIBUTES_SCHEMA)
    document = schema.parse(io.StringIO(xml), typed=False, capture_xdm=True)

    xdm = document.to_xdm()

    assert document.is_valid
    assert xdm.root_element.name == "doc"
    assert xdm.root_element.type_name == UNTYPED
    assert xdm.root_element.annotation.actual_type == UNTYPED
    assert xdm.root_element.annotation.validity == "not-validated"
    assert xdm.root_element.annotation.governing_declaration is None
    assert {attribute.type_name for attribute in xdm.root_element.attributes} == {UNTYPED_ATOMIC}
    assert xdm.root_element.children[0].type_name == UNTYPED
    assert {attribute.name for attribute in xdm.root_element.attributes} == {
        "{http://www.w3.org/2001/XMLSchema-instance}nil",
        "{http://www.w3.org/2001/XMLSchema-instance}type",
    }


def test_root_callable_transform_returns_a_stale_relaxng_document():
    document = _document("<doc><item>x</item></doc>", schema=_schema(SIMPLE_SCHEMA))

    stale = document.transform(lambda root: root)

    assert isinstance(stale, RelaxNGDocument)
    assert stale is not document
    assert stale.root is document.root
    assert stale.report is document.report
    assert stale.typed is document.typed is False
    assert stale.interpretations_current is False
    assert document.interpretations_current is True
    assert stale.to_string() == document.to_string()

    moved = document.transform(lambda root: root.children[0])

    assert isinstance(moved, RelaxNGDocument)
    assert moved.root is document.root.children[0]
    assert moved.report is document.report
    assert moved.interpretations_current is False

    refreshed = stale.revalidate()
    assert refreshed.interpretations_current is True


def test_unchanged_root_transform_keeps_a_coherent_xdm_capture():
    document = _schema().parse(
        io.StringIO("<doc><item>x</item></doc>"), typed=False, capture_xdm=True
    )
    assert document.is_valid

    stale = document.transform(lambda root: root)

    assert isinstance(stale, RelaxNGDocument)
    assert stale.interpretations_current is False
    assert stale.to_xdm() is document.to_xdm()
    assert stale.to_xdm().root_element.name == "doc"


def test_moved_root_transform_has_no_stale_xdm_capture():
    document = _schema().parse(
        io.StringIO("<doc><item>x</item></doc>"), typed=False, capture_xdm=True
    )

    moved = document.transform(lambda root: root.children[0])

    assert isinstance(moved, RelaxNGDocument)
    assert moved.interpretations_current is False
    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        moved.to_xdm()


def test_in_place_edit_returning_the_root_rejects_the_stale_xdm_capture():
    document = _schema().parse(
        io.StringIO("<doc><item>x</item></doc>"), typed=False, capture_xdm=True
    )

    def edit(root: RelaxNGElement) -> RelaxNGElement:
        root.set_text("changed")
        return root

    edited = document.transform(edit)

    assert isinstance(edited, RelaxNGDocument)
    assert edited.interpretations_current is False
    with pytest.raises(XdmInputError, match="changed since XDM capture"):
        edited.to_xdm()


def test_transform_non_tree_results_pass_through():
    document = _document("<doc/>")

    assert document.transform(lambda root: 42) == 42
    assert document.transform(lambda root: None) is None
    assert document.transform(lambda root, value: value, "raw") == "raw"
    assert document.transform(lambda root, flag=False: flag, flag=True) is True


def test_document_transform_dispatches_before_the_root_callable_path():
    document = _document("<doc/>")
    transform = _RecordingTransform()

    assert isinstance(transform, DocumentTransform)
    result = document.transform(transform)

    assert result is document
    assert transform.seen is document
    assert document.transform(_RawTransform()) == "raw"


def test_revalidation_returns_a_fresh_revision_without_refreshing_old_evidence():
    limits = RelaxNGLimits(max_diagnostics=5)
    schema = _schema(SIMPLE_SCHEMA)
    document = schema.parse(
        io.StringIO("<doc><item>x</item></doc>"),
        typed=False,
        limits=limits,
    )
    old_report = document.report
    old_root = document.root
    old_child = old_root.children[0]
    old_revision_id = old_root.revision.revision_id

    old_child.set_name("other")

    assert document.interpretations_current is False
    fresh = document.revalidate()

    assert fresh is not document
    assert fresh.root is not old_root
    assert fresh.root.revision.revision_id != old_revision_id
    assert fresh.report is not old_report
    assert fresh.typed is False
    assert fresh._capture == "none"
    assert fresh._limits is limits
    assert fresh.interpretations_current is True
    assert fresh.root._name_ == "doc"
    assert [child._name_ for child in fresh.root.children] == ["other"]
    assert not fresh.is_valid
    assert old_report is document.report
    assert document.is_valid
    assert not old_report.has_errors
    assert old_child._name_ == "other"
    assert document.interpretations_current is False


def test_revalidation_preserves_namespace_and_base_uri_frames(tmp_path):
    schema = _schema(CONTEXT_SCHEMA)
    path = tmp_path / "context.xml"
    path.write_text(
        '<doc xmlns:p="urn:values" kind="p:item" xml:base="sub/"><item xml:base="deeper/"/></doc>',
        encoding="utf-8",
    )
    document = schema.parse(path, typed=False, capture_xdm=True)
    original_root_base = document.root.base_uri
    original_item_base = document.root.children[0].base_uri

    assert document.is_valid
    assert document.root.namespaces is not None
    assert document.root.namespaces["p"] == "urn:values"
    assert original_root_base == os.fspath(tmp_path / "sub") + "/"
    assert original_item_base == os.fspath(tmp_path / "sub" / "deeper") + "/"

    fresh = document.revalidate()

    assert fresh.is_valid
    assert fresh.root is not document.root
    assert fresh.root.namespaces is not None
    assert fresh.root.namespaces["p"] == "urn:values"
    assert fresh.root.base_uri == original_root_base
    assert fresh.root.children[0].base_uri == original_item_base
    assert fresh.to_xdm().root_element.name == "doc"
    assert fresh.root.attribute_lexical("kind") == "p:item"


def test_revalidation_does_not_capture_xdm_unless_the_original_did():
    document = _document("<doc/>")

    fresh = document.revalidate()

    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        fresh.to_xdm()


def test_locate_resolves_parse_specific_source_occurrences(tmp_path):
    schema = _schema(INTEGER_ATTRIBUTE_SCHEMA)
    first_path = tmp_path / "first.xml"
    second_path = tmp_path / "second.xml"
    first_path.write_text('<doc n="bad"/>', encoding="utf-8")
    second_path.write_text('<doc n="bad"/>', encoding="utf-8")

    first = schema.parse(first_path, typed=False)
    second = schema.parse(second_path, typed=False)
    [issue] = list(first.report)
    assert issue.location is not None
    assert issue.location.attribute == "n"

    lookup = first.locate(issue.location)

    assert lookup.location is issue.location
    assert lookup.status == "attribute"
    assert lookup.bound == "bad"

    element_location = IssueLocation(issue.location.source, (PathStep("doc", 1),))
    element = first.locate(element_location)
    assert element.status == "bound"
    assert element.bound is first.root

    absent = first.locate(IssueLocation(issue.location.source, (PathStep("doc", 1),), "absent"))
    assert absent.status == "missing"
    assert absent.bound is None

    # The same address in another parse is not this revision's occurrence.
    assert second.locate(issue.location).status == "missing"


def test_locate_returns_parse_time_attribute_values_after_edit():
    schema = _schema(INTEGER_ATTRIBUTE_SCHEMA)
    document = schema.parse(io.StringIO('<doc n="bad"/>'), typed=False)
    [issue] = list(document.report)
    assert issue.location is not None
    assert issue.location.source is None
    assert issue.location.attribute == "n"
    assert document.locate(issue.location).bound == "bad"

    document.root.set_attribute("n", "7")

    assert document.root.attribute_lexical("n") == "7"
    lookup = document.locate(issue.location)
    assert lookup.status == "attribute"
    assert lookup.bound == "bad"

    document.root.delete_attribute("n")
    assert document.locate(issue.location).status == "attribute"
    assert document.locate(issue.location).bound == "bad"


def test_revalidation_records_a_fresh_attribute_snapshot(tmp_path):
    schema = _schema(INTEGER_ATTRIBUTE_SCHEMA)
    path = tmp_path / "document.xml"
    path.write_text('<doc n="bad"/>', encoding="utf-8")
    document = schema.parse(path, typed=False)
    [issue] = list(document.report)
    assert issue.location is not None

    document.root.set_attribute("n", "8x")
    assert document.locate(issue.location).bound == "bad"

    fresh = document.revalidate()
    [fresh_issue] = list(fresh.report)
    assert fresh_issue.location is not None
    assert fresh_issue.location.attribute == "n"
    assert fresh.locate(fresh_issue.location).bound == "8x"
    assert document.locate(issue.location).bound == "bad"


def test_locate_on_a_source_less_tree_reports_missing():
    schema = _schema()
    parsed = schema.parse(io.StringIO("<doc/>"), typed=False)
    root = RelaxNGElement.from_etree(ET.fromstring("<doc/>"))

    document = RelaxNGDocument(
        schema=schema,
        root=root,
        source=None,
        report=parsed.report,
        typed=False,
    )

    lookup = document.locate(IssueLocation(None, (PathStep("doc", 1),)))

    assert lookup.status == "missing"
    assert lookup.bound is None


class _ProjectedElement(RelaxNGElement):
    """Stand-in for a generated projection class."""


def test_frame_restoration_rejects_a_changed_tree_shape():
    source = RelaxNGElement.from_etree(ET.fromstring("<doc><item/></doc>"))
    target = RelaxNGElement.from_etree(ET.fromstring("<doc/>"))

    with pytest.raises(PyXSDError, match="lexical tree shape"):
        _restore_lexical_frames(source, target)


def test_clone_preserves_the_concrete_relaxng_element_class():
    tree = _ProjectedElement.from_etree(ET.fromstring("<doc><item><leaf/></item></doc>"))

    cloned = clone_relaxng(tree)

    assert type(cloned) is _ProjectedElement
    assert cloned is not tree
    assert cloned.revision is not tree.revision
    assert [type(child) for child in cloned.children] == [_ProjectedElement]
    assert [type(child) for child in cloned.children[0].children] == [_ProjectedElement]
    assert cloned.to_etree().find("item/leaf") is not None
