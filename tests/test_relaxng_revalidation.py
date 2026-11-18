"""Typed revalidation, stale transforms, and lexical escape paths.

These tests pin the document lifecycle that surrounds Phase 3 evidence and
Phase 4 projections: revalidation parses an independent clone with the original
parse settings, never updates the old document or its snapshots, and returns
fresh nodes, classes, reports, evidence, and annotations.  A transformed root is
a stale document until revalidation, including the shared-revision boundary for
the original document.  Typed views require freshness; the lexical writer and
``to_string``/``write`` remain available, while data conversion dispatches
through the RELAX NG adapter registry and applies its freshness policy.
"""

from __future__ import annotations

import io
import os

import pytest

from pyxsd.document_transform import DocumentTransform
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.relaxng import (
    AlternativeValue,
    RelaxNGDocument,
    RelaxNGElement,
    RelaxNGLimitError,
    RelaxNGLimits,
    RelaxNGSchema,
    StaleInterpretationError,
)
from pyxsd.relaxng.conversion import RelaxNGUnsupportedAdapterError
from pyxsd.relaxng.xml_writer import clone_relaxng
from pyxsd.xdm.errors import XdmInputError

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"

INTEGER_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <optional>
        <attribute name="n">
          <data type="integer" datatypeLibrary="{XSD}"/>
        </attribute>
      </optional>
      <zeroOrMore>
        <element name="item">
          <data type="integer" datatypeLibrary="{XSD}"/>
        </element>
      </zeroOrMore>
    </element>
  </start>
</grammar>"""

TEXT_ITEM_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <optional>
        <attribute name="n">
          <data type="integer" datatypeLibrary="{XSD}"/>
        </attribute>
      </optional>
      <mixed>
        <zeroOrMore>
          <choice>
            <element name="item"><text/></element>
            <element name="other"><text/></element>
          </choice>
        </zeroOrMore>
      </mixed>
    </element>
  </start>
</grammar>"""

CONTEXT_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <optional>
        <attribute name="kind">
          <data type="QName" datatypeLibrary="{XSD}"/>
        </attribute>
      </optional>
      <optional><attribute name="xml:base"><text/></attribute></optional>
      <mixed>
        <zeroOrMore>
          <element name="item">
            <optional><attribute name="xml:base"><text/></attribute></optional>
            <empty/>
          </element>
        </zeroOrMore>
      </mixed>
    </element>
  </start>
</grammar>"""

ALTERNATIVE_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <choice>
        <group>
          <element name="x">
            <data type="integer" datatypeLibrary="{XSD}"/>
          </element>
          <element name="a"><empty/></element>
        </group>
        <group>
          <element name="x">
            <data type="string" datatypeLibrary="{XSD}"/>
          </element>
          <element name="b"><empty/></element>
        </group>
      </choice>
    </element>
  </start>
</grammar>"""

MOVABLE_ROOT_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <choice>
      <element name="doc">
        <optional><element name="item"><text/></element></optional>
      </element>
      <element name="item"><text/></element>
    </choice>
  </start>
</grammar>"""


def _schema(source: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(io.StringIO(source))
    assert not schema.report.has_errors, schema.report
    return schema


def _typed(xml: str, *, schema: RelaxNGSchema | None = None, **kwargs: object) -> RelaxNGDocument:
    document = (schema or _schema(INTEGER_SCHEMA)).parse(io.StringIO(xml), typed=True, **kwargs)
    assert isinstance(document, RelaxNGDocument)
    return document


def _validation_only(
    xml: str, *, schema: RelaxNGSchema | None = None, **kwargs: object
) -> RelaxNGDocument:
    document = (schema or _schema(INTEGER_SCHEMA)).parse(io.StringIO(xml), typed=False, **kwargs)
    assert isinstance(document, RelaxNGDocument)
    return document


class _RecordingTransform:
    """A whole-document transform used only to prove dispatch order."""

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


def test_typed_revalidation_replaces_every_snapshot_and_keeps_the_plan_class():
    limits = RelaxNGLimits(max_capture_entries=4)
    schema = _schema(INTEGER_SCHEMA)
    document = _typed(
        '<doc n="1"><item>2</item></doc>',
        schema=schema,
        capture="patterns",
        capture_xdm=True,
        limits=limits,
    )
    old_root = document.root
    old_item = document.root.children[0]
    old_report = document.report
    old_evidence = document.binding_evidence
    old_annotations = document.annotations
    old_revision_id = old_root.revision.revision_id
    root_class = type(old_root)
    item_class = type(old_item)

    assert old_root.n == 1
    assert old_item.typed_value == 2

    old_root.set_attribute("n", "3")
    old_item.set_text("4")

    assert document.interpretations_current is False
    with pytest.raises(StaleInterpretationError):
        _ = old_root.n
    with pytest.raises(StaleInterpretationError):
        _ = old_item.typed_value
    with pytest.raises(StaleInterpretationError):
        _ = document.annotations

    fresh = document.revalidate()

    assert fresh is not document
    assert fresh.schema is schema
    assert fresh.root is not old_root
    assert fresh.root.revision.revision_id != old_revision_id
    assert fresh.root.revision is not old_root.revision
    assert fresh.report is not old_report
    assert fresh.binding_evidence is not None
    assert fresh.binding_evidence is not old_evidence
    assert fresh.interpretations_current is True
    assert fresh.typed is True
    assert fresh._capture == "patterns"
    assert fresh._limits is limits
    assert type(fresh.root) is root_class
    assert type(fresh.root.children[0]) is item_class
    assert fresh.root.n == 3
    assert fresh.root.children[0].typed_value == 4
    assert fresh.root.children[0] is not old_item

    fresh_annotations = fresh.annotations
    assert fresh_annotations is not old_annotations
    assert fresh_annotations.revision_id == fresh.root.revision.revision_id
    assert fresh_annotations.revision_id != old_annotations.revision_id
    assert fresh.to_xdm().root_element.name == "doc"

    # The old document, its nodes, and every snapshot stay exactly as parsed.
    assert document.report is old_report
    assert document.binding_evidence is old_evidence
    assert document.interpretations_current is False
    assert old_root.revision.revision_id == old_revision_id
    assert old_root.attribute_lexical("n") == "3"
    assert old_item.text_value() == "4"
    with pytest.raises(StaleInterpretationError):
        _ = document.annotations
    assert document.to_string() == '<doc n="3"><item>4</item></doc>'


def test_typed_revalidation_keeps_candidate_ids_but_replaces_candidates():
    schema = _schema(ALTERNATIVE_SCHEMA)
    document = _typed("<doc><x>12</x><b/></doc>", schema=schema)
    node = document.root.children[0]
    before = node.scalar_value()
    assert isinstance(before, AlternativeValue)
    before_ids = [candidate.candidate_id for candidate in before.candidates]
    assert len(before_ids) == 1
    report = document.report
    evidence = document.binding_evidence

    node.set_text("99")

    with pytest.raises(StaleInterpretationError):
        _ = node.scalar_value()

    fresh = document.revalidate()
    after = fresh.root.children[0].scalar_value()

    assert isinstance(after, AlternativeValue)
    assert after is not before
    assert after.lexical == "99"
    assert before.lexical == "12"
    assert [candidate.candidate_id for candidate in after.candidates] == before_ids
    assert after.candidates[0] is not before.candidates[0]
    assert document.report is report
    assert document.binding_evidence is evidence


def test_validation_only_revalidation_stays_evidence_free_and_keeps_the_xdm_request():
    limits = RelaxNGLimits(max_diagnostics=1)
    schema = _schema(INTEGER_SCHEMA)
    document = _validation_only(
        '<doc n="1"><item>2</item></doc>',
        schema=schema,
        capture_xdm=True,
        limits=limits,
    )
    report = document.report
    root = document.root

    root.set_attribute("n", "3")
    fresh = document.revalidate()

    assert fresh is not document
    assert fresh.typed is False
    assert fresh.binding_evidence is None
    assert fresh._capture == "none"
    assert fresh._limits is limits
    assert fresh.report is not report
    assert fresh.interpretations_current is True
    assert fresh.root is not root
    with pytest.raises(PyXSDError, match="validation-only"):
        _ = fresh.annotations
    with pytest.raises(PyXSDError, match="typed parse"):
        _ = fresh.root.scalar_value()
    assert fresh.to_xdm().root_element.name == "doc"
    assert fresh.to_string() == '<doc n="3"><item>2</item></doc>'


@pytest.mark.parametrize("budget", [None, 0, 1])
def test_revalidation_preserves_capture_and_applies_the_independent_budget(budget):
    limits = None if budget is None else RelaxNGLimits(max_capture_entries=budget)
    schema = _schema(INTEGER_SCHEMA)
    document = _typed(
        '<doc n="1"><item>2</item></doc>',
        schema=schema,
        capture="patterns",
        limits=limits,
    )
    old_annotations = document.annotations
    assert old_annotations.complete is (budget is None)
    old_evidence = document.binding_evidence

    document.root.children[0].set_text("5")
    fresh = document.revalidate()

    assert fresh._capture == "patterns"
    assert fresh._limits is limits
    assert fresh.binding_evidence is not None
    assert fresh.binding_evidence is not old_evidence
    table = fresh.annotations
    assert table is not old_annotations
    assert table.revision_id != old_annotations.revision_id
    assert table.complete is (budget is None)
    assert fresh.root.n == 1
    assert fresh.root.children[0].typed_value == 5
    if budget is not None:
        retained = table.for_node(fresh.root)
        dropped = table.for_node(fresh.root.children[0])
        if budget == 0:
            assert retained.patterns == ()
        else:
            assert retained.patterns
        assert dropped.patterns == ()
    with pytest.raises(StaleInterpretationError):
        _ = document.annotations


def test_invalid_typed_revalidation_returns_a_fresh_generic_document():
    schema = _schema(INTEGER_SCHEMA)
    document = _typed(
        '<doc n="1"><item>2</item></doc>',
        schema=schema,
        capture="patterns",
        capture_xdm=True,
    )
    old_report = document.report
    old_evidence = document.binding_evidence
    old_root = document.root
    old_item = old_root.children[0]

    old_item.set_text("not-an-integer")
    old_root.set_attribute("n", "also-bad")

    fresh = document.revalidate()

    assert fresh is not document
    assert fresh.typed is True
    assert fresh.is_valid is False
    assert fresh.report is not old_report
    assert fresh.report.has_errors
    assert fresh.binding_evidence is None
    assert type(fresh.root) is RelaxNGElement
    assert fresh.interpretations_current is True
    with pytest.raises(ValidationError) as scalar_error:
        fresh.root.scalar_value()
    assert scalar_error.value.report is fresh.report
    with pytest.raises(ValidationError) as attribute_error:
        fresh.root.attribute_value("n")
    assert attribute_error.value.report is fresh.report
    with pytest.raises(ValidationError) as annotation_error:
        _ = fresh.annotations
    assert annotation_error.value.report is fresh.report

    # The old document keeps its own report and evidence; only typed reads are
    # stale because its revision was edited.
    assert document.report is old_report
    assert document.binding_evidence is old_evidence
    assert document.is_valid is True
    assert document.interpretations_current is False
    assert document.to_string() == '<doc n="also-bad"><item>not-an-integer</item></doc>'
    assert document.root.children[0].text_value() == "not-an-integer"
    with pytest.raises(StaleInterpretationError):
        _ = document.root.n
    assert fresh.to_xdm().root_element.name == "doc"


def test_revalidation_preserves_and_enforces_the_instance_limit():
    schema = _schema(
        f"""<grammar xmlns="{NS}">
          <start>
            <element name="doc">
              <zeroOrMore><element name="item"><text/></element></zeroOrMore>
            </element>
          </start>
        </grammar>"""
    )
    limits = RelaxNGLimits(max_scalar_witnesses=1)
    document = _typed("<doc><item>0</item></doc>", schema=schema, limits=limits)
    assert document._limits is limits
    assert document.is_valid

    document.root.append_child(type(document.root)._new("item", document.root.revision, text="1"))

    with pytest.raises(RelaxNGLimitError):
        document.revalidate()

    # The failed revalidation released the shared parse guard.
    document.root.remove_child(document.root.children[1])
    fresh = document.revalidate()
    assert fresh.is_valid
    assert fresh._limits is limits
    assert fresh.root.children[0].typed_value.lexical == "0"
    assert fresh.revalidate().is_valid


def test_revalidation_retains_namespace_qname_and_base_frames_across_repeated_edits(tmp_path):
    schema = _schema(CONTEXT_SCHEMA)
    path = tmp_path / "context.xml"
    path.write_text(
        '<doc xmlns:p="urn:one" kind="p:item" xml:base="first/"><item xml:base="deep/"/></doc>',
        encoding="utf-8",
    )
    document = schema.parse(path, typed=True, capture_xdm=True)
    assert document.root.namespaces is not None
    assert document.root.namespaces["p"] == "urn:one"
    assert document.root.base_uri == os.fspath(tmp_path / "first") + "/"

    document.root.set_namespace_bindings({"q": "urn:two"})
    document.root.set_attribute("kind", "q:item")
    document.root.set_base_uri("second/")
    document.root.children[0].set_base_uri("deeper/")

    fresh = document.revalidate()

    assert fresh.is_valid
    assert fresh.root.namespaces == {"q": "urn:two"}
    assert fresh.root.attribute_lexical("kind") == "q:item"
    assert fresh.root.kind.lexical == "q:item"
    assert fresh.root.base_uri == "second/"
    assert fresh.root.children[0].base_uri == "deeper/"
    assert fresh.to_string() == (
        '<doc xmlns:q="urn:two" kind="q:item" xml:base="first/">'
        '<item xmlns:p="urn:one" xml:base="deep/"/></doc>'
    )

    again = fresh.revalidate()
    assert again.root.namespaces == {"q": "urn:two"}
    assert again.root.kind.lexical == "q:item"
    assert again.root.base_uri == "second/"
    assert again.root.children[0].base_uri == "deeper/"
    assert again.to_string() == fresh.to_string()


def test_repeated_lexical_edits_and_sequence_operations_revalidate_exact_state():
    schema = _schema(TEXT_ITEM_SCHEMA)
    document = _typed('<doc n="1"><item>first</item></doc>', schema=schema)
    item = document.root.children[0]

    item.set_text("second")
    document.root.set_attribute("n", "2")
    document.root.append_child(
        type(document.root)._new("other", document.root.revision, text="tail")
    )
    item.set_name("other")
    item.set_tail(" between ")

    assert document.interpretations_current is False
    fresh = document.revalidate()

    assert fresh.is_valid
    assert fresh.interpretations_current is True
    assert fresh.to_string() == (
        '<doc n="2"><other>second</other> between <other>tail</other></doc>'
    )
    assert fresh.root.n.lexical == "2"
    assert [child.text_value() for child in fresh.root.children] == ["second", "tail"]
    assert [child.tail_value() for child in fresh.root.children] == [" between ", None]

    first, second = fresh.root.children
    first.set_text("third")
    fresh.root.remove_child(second)
    again = fresh.revalidate()

    assert again.is_valid
    assert again.to_string() == '<doc n="2"><other>third</other> between </doc>'
    assert again.root.children[0].typed_value.lexical == "third"
    assert fresh.to_string() == '<doc n="2"><other>third</other> between </doc>'
    assert fresh.interpretations_current is False
    assert document.to_string() == (
        '<doc n="2"><other>second</other> between <other>tail</other></doc>'
    )


def test_revalidation_does_not_carry_a_root_tail():
    document = _typed("<doc><item>1</item></doc>", schema=_schema(TEXT_ITEM_SCHEMA))
    document.root.set_tail("outside")
    document.root.children[0].set_tail(" inside ")

    assert document.to_string() == "<doc><item>1</item> inside </doc>"

    fresh = document.revalidate()

    assert fresh.is_valid
    assert fresh.root.tail_value() is None
    assert fresh.root.children[0].tail_value() == " inside "
    assert fresh.to_string() == "<doc><item>1</item> inside </doc>"


def test_lexical_escape_paths_work_on_dirty_and_stale_documents(tmp_path):
    document = _typed('<doc n="1"><item>2</item></doc>')
    document.root.set_attribute("n", "3")
    expected = '<doc n="3"><item>2</item></doc>'

    path = tmp_path / "dirty.xml"
    document.write(path)
    stream = io.StringIO()
    document.write(stream)

    assert document.to_string() == expected
    assert path.read_text(encoding="utf-8") == expected
    assert stream.getvalue() == expected
    for conversion in (
        lambda: document.to_dict(),
        lambda: document.to_data(),
        lambda: document.to_json(),
    ):
        with pytest.raises(StaleInterpretationError):
            conversion()

    stale = document.transform(lambda root: root)
    assert isinstance(stale, RelaxNGDocument)
    assert stale.to_string() == expected
    assert stale.revalidate().to_string() == expected

    validation = _validation_only('<doc n="1"/>')
    validation.root.set_text("changed")
    assert validation.to_string() == '<doc n="1">changed</doc>'
    for conversion in (
        lambda: validation.to_dict(typed=False),
        lambda: validation.to_data(typed=False),
        lambda: validation.to_json(typed=False),
    ):
        with pytest.raises(RelaxNGUnsupportedAdapterError, match="not implemented"):
            conversion()


def test_root_callable_transform_boundaries_and_non_tree_results():
    schema = _schema(TEXT_ITEM_SCHEMA)
    document = _typed('<doc n="1"><item>x</item></doc>', schema=schema)
    evidence = document.binding_evidence

    stale = document.transform(lambda root: root)

    assert isinstance(stale, RelaxNGDocument)
    assert stale.root is document.root
    assert stale.report is document.report
    assert stale.binding_evidence is evidence
    assert stale.interpretations_current is False
    assert document.interpretations_current is False
    with pytest.raises(StaleInterpretationError):
        _ = document.root.n
    with pytest.raises(StaleInterpretationError):
        _ = stale.root.n
    assert stale.revalidate().interpretations_current is True

    original = _typed('<doc n="1"><item>x</item></doc>', schema=schema)
    cloned = original.transform(lambda root: clone_relaxng(root))

    assert isinstance(cloned, RelaxNGDocument)
    assert cloned.root is not original.root
    assert cloned.binding_evidence is original.binding_evidence
    assert cloned.interpretations_current is False
    assert original.interpretations_current is True
    with pytest.raises(StaleInterpretationError):
        _ = cloned.root.n
    assert original.root.n.lexical == "1"
    assert cloned.revalidate().root.n.lexical == "1"

    raw = _typed("<doc/>", schema=schema)
    assert raw.transform(lambda root: 42) == 42
    assert raw.transform(lambda root: None) is None
    assert raw.transform(lambda root, value: value, "raw") == "raw"
    recording = _RecordingTransform()
    assert isinstance(recording, DocumentTransform)
    assert raw.transform(recording) is raw
    assert recording.seen is raw
    assert raw.transform(_RawTransform()) == "raw"


def test_revalidation_restores_fresh_xdm_for_transformed_roots_with_the_original_request():
    schema = _schema(MOVABLE_ROOT_SCHEMA)
    document = schema.parse(io.StringIO("<doc><item>x</item></doc>"), typed=True, capture_xdm=True)

    cloned = document.transform(lambda root: clone_relaxng(root))
    assert isinstance(cloned, RelaxNGDocument)
    assert cloned.interpretations_current is False
    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        cloned.to_xdm()

    fresh = cloned.revalidate()
    assert fresh.to_xdm().root_element.name == "doc"
    assert fresh.to_xdm().source_name == "<memory>"

    moved = document.transform(lambda root: root.children[0])
    assert isinstance(moved, RelaxNGDocument)
    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        moved.to_xdm()

    moved_fresh = moved.revalidate()
    assert moved_fresh.is_valid
    assert moved_fresh.to_xdm().root_element.name == "item"

    untracked = _schema(MOVABLE_ROOT_SCHEMA).parse(io.StringIO("<doc/>"), typed=True)
    assert untracked._xdm_requested is False
    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        untracked.revalidate().to_xdm()


@pytest.mark.parametrize("source_kind", ["path", "named-stream", "anonymous"])
def test_revalidation_preserves_source_identity_and_locate_addresses(tmp_path, source_kind):
    schema = _schema(
        f"""<grammar xmlns="{NS}">
          <start>
            <element name="doc">
              <zeroOrMore>
                <element name="item">
                  <attribute name="n">
                    <data type="integer" datatypeLibrary="{XSD}"/>
                  </attribute>
                </element>
              </zeroOrMore>
            </element>
          </start>
        </grammar>"""
    )
    xml = '<doc><item n="1"/><item n="bad"/></doc>'
    path = tmp_path / "source.xml"
    if source_kind == "path":
        path.write_text(xml, encoding="utf-8")
        source = path
    else:
        source = io.StringIO(xml)
        if source_kind == "named-stream":
            source.name = str(path)
    expected_source = None if source_kind == "anonymous" else str(path)
    document = schema.parse(source, typed=True, capture_xdm=True)
    document.root.remove_child(document.root.children[0])

    fresh = document.revalidate()

    assert fresh.source is document.source
    assert fresh.root.source_location is not None
    assert fresh.root.source_location.source == expected_source
    assert fresh.root.source_location.path == (PathStep("doc", 1),)
    [issue] = list(fresh.report)
    assert issue.location is not None
    assert issue.location.source == expected_source
    assert issue.location.attribute == "n"
    lookup = fresh.locate(issue.location)
    assert lookup.status == "attribute"
    assert lookup.bound == "bad"
    assert (
        fresh.locate(IssueLocation(expected_source, issue.location.path)).bound
        is (fresh.root.children[0])
    )
    assert fresh.to_xdm().source_name == (expected_source or "<memory>")

    # The old parse's occurrences are not the new parse's occurrence.
    assert document.locate(issue.location).status == "missing"
