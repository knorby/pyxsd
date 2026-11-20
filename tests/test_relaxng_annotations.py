"""Optional RELAX NG pattern annotations and independent capture budgets.

Pattern annotations are a separate, optional view over the same accepting
evidence that supports typed scalar access.  They identify surviving authored
patterns and the named grammar definitions those origins belong to, and they
retain scalar alternatives without choosing a branch.  Capture is parse-scoped:
truncation returns sound subsets marked incomplete, never exceptions, and it
never changes typed values or candidate identities.
"""

from __future__ import annotations

import io
from dataclasses import FrozenInstanceError
from xml.etree import ElementTree as ET

import pytest

import pyxsd.relaxng as relaxng
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.relaxng import (
    AlternativeValue,
    NodeAnnotations,
    RelaxNGLimits,
    RelaxNGSchema,
    ScalarAnnotation,
    StaleInterpretationError,
)
from pyxsd.relaxng.annotations import AnnotationTable, build_annotation_table
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    DatatypeParameter,
)
from pyxsd.relaxng.origins import SourceOrigin
from pyxsd.relaxng.tree import RelaxNGElement

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
CUSTOM = "urn:custom"


def data(type_name: str) -> str:
    return f'<data type="{type_name}" datatypeLibrary="{XSD}"/>'


def custom_data(type_name: str) -> str:
    return f'<data type="{type_name}" datatypeLibrary="{CUSTOM}"/>'


def element(name: str, content: str = "<empty/>") -> str:
    return f'<element name="{name}">{content}</element>'


def attribute(name: str, content: str) -> str:
    return f'<attribute name="{name}">{content}</attribute>'


def compile_schema(body: str, **kwargs: object) -> RelaxNGSchema:
    source = f'<grammar xmlns="{NS}"><start>{body}</start></grammar>'
    compiled = RelaxNGSchema.compile(io.StringIO(source), **kwargs)  # type: ignore[arg-type]
    assert not compiled.report.has_errors, [issue.message for issue in compiled.report.issues]
    return compiled


class ExplodingDatatype(Datatype):
    """A declared decoder whose call is observable and always fails."""

    library_uri = CUSTOM
    type_name = "explodes"
    parameters = ()
    supports_decode = True
    calls = 0

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        del lexical, context
        return True

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        del left_context, right_context
        return left == right

    def decode(self, lexical: str, context: DatatypeContext) -> object:
        del lexical, context
        type(self).calls += 1
        raise RuntimeError("decoder exploded")


class CountingDatatype(Datatype):
    """A declared decoder that returns the lexical value and counts calls."""

    library_uri = CUSTOM
    type_name = "counts"
    parameters = ()
    supports_decode = True
    calls = 0

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        del lexical, context
        return True

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        del left_context, right_context
        return left == right

    def decode(self, lexical: str, context: DatatypeContext) -> object:
        del context
        type(self).calls += 1
        return lexical


class _CustomFactory(DatatypeFactory):
    def create(
        self,
        type_name: str,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        del parameters, context
        if type_name == "explodes":
            return ExplodingDatatype()
        return CountingDatatype()


CUSTOM_LIBRARIES = {
    CUSTOM: DatatypeLibraryRegistration(_CustomFactory(), "test-custom/1"),
}


def typed(compiled: RelaxNGSchema, xml: str | ET.Element, **kwargs: object):
    return compiled.parse(xml, typed=True, **kwargs)  # type: ignore[arg-type]


def validation_only(compiled: RelaxNGSchema, xml: str, **kwargs: object):
    return compiled.parse(io.StringIO(xml), typed=False, **kwargs)


def scalar_schema() -> RelaxNGSchema:
    return compile_schema(element("doc", data("integer")))


def ambiguous_schema() -> RelaxNGSchema:
    return compile_schema(element("doc", f"<choice>{data('integer')}{data('string')}</choice>"))


def attribute_schema() -> RelaxNGSchema:
    return compile_schema(
        element(
            "doc",
            attribute("a", data("integer"))
            + attribute("b", f"<choice>{data('integer')}{data('string')}</choice>")
            + f"<optional>{attribute('c', '<text/>')}</optional>"
            + f"<optional>{attribute('flag', '<empty/>')}</optional>"
            + data("integer"),
        )
    )


def list_schema() -> RelaxNGSchema:
    return compile_schema(
        element("doc", f"<list><zeroOrMore>{data('integer')}</zeroOrMore></list>")
    )


def nested_schema() -> RelaxNGSchema:
    return compile_schema(
        element(
            "doc",
            element("item", data("integer")) + element("name", "<text/>"),
        )
    )


def preorder(root: RelaxNGElement) -> list[RelaxNGElement]:
    """Return lexical preorder, the order capture budgets must follow."""
    nodes: list[RelaxNGElement] = []
    stack = [root]
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(reversed(node.children))
    return nodes


def origin_key(origin: SourceOrigin) -> tuple[object, ...]:
    """The documented process-independent ordering key for authored origins."""
    location = origin.location
    if location is None:
        location_key: tuple[object, ...] = (1, "", (), "")
    else:
        location_key = (
            0,
            location.source or "",
            tuple((step.name, step.index) for step in location.path),
            location.attribute or "",
        )
    return (origin.source_id, location_key)


def scalar_ids(value: AlternativeValue) -> tuple[str, ...]:
    assert isinstance(value, AlternativeValue)
    return tuple(candidate.candidate_id for candidate in value.candidates)


def scalar_ids_from(annotation: NodeAnnotations) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(candidate.candidate_id for candidate in item.value.candidates)
        for item in annotation.scalar
    )


def test_node_annotations_and_scalar_annotations_are_public_immutable_values():
    compiled = scalar_schema()
    document = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")

    assert relaxng.NodeAnnotations is NodeAnnotations
    assert relaxng.ScalarAnnotation is ScalarAnnotation
    assert {"NodeAnnotations", "ScalarAnnotation"} <= set(relaxng.__all__)

    annotations = document.annotations
    assert isinstance(annotations, AnnotationTable)
    node = annotations.for_node(document.root)
    assert isinstance(node, NodeAnnotations)
    assert isinstance(node.scalar, tuple)
    assert all(isinstance(item, ScalarAnnotation) for item in node.scalar)
    assert not hasattr(node, "matched_pattern")
    assert not hasattr(node, "matched_type")

    with pytest.raises(FrozenInstanceError):
        node.complete = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        node.scalar[0].name = "changed"  # type: ignore[misc]


def test_text_and_multiple_attributes_are_separate_scalar_sites():
    compiled = attribute_schema()
    document = typed(
        compiled,
        io.StringIO('<doc a="1" b="2" c="x" flag="">12</doc>'),
        capture="patterns",
    )
    annotations = document.annotations.for_node(document.root)

    assert [item.kind for item in annotations.scalar] == [
        "text",
        "attribute",
        "attribute",
        "attribute",
    ]
    text, first, second, third = annotations.scalar
    assert (text.name, text.content_index) == (None, 0)
    assert (first.name, first.content_index) == ("a", None)
    assert (second.name, second.content_index) == ("b", None)
    assert (third.name, third.content_index) == ("c", None)

    assert text.value.candidates[0].decoded == 12
    assert first.value.candidates[0].decoded == 1
    assert {candidate.decoded for candidate in second.value.candidates} == {2, "2"}
    assert third.value.candidates[0].contract.kind == "text"
    assert third.value.candidates[0].decodable is False
    assert third.value.candidates[0].contract.library_uri is None
    assert third.value.candidates[0].contract.type_name is None

    assert document.root.scalar_value() == 12
    assert document.root.attribute_value("a") == 1
    second_typed = document.root.attribute_value("b")
    assert isinstance(second_typed, AlternativeValue)
    assert scalar_ids(second_typed) == scalar_ids(second.value)
    third_typed = document.root.attribute_value("c")
    assert isinstance(third_typed, AlternativeValue)
    assert scalar_ids(third_typed) == scalar_ids(third.value)
    assert annotations.revision_id == document.root.revision.revision_id
    assert annotations.schema_id


@pytest.mark.parametrize("xml", ["<r>head<a/>tail</r>", "<r><a/>tail</r>"])
def test_mixed_annotations_include_leading_and_tail_text_sites(xml):
    compiled = compile_schema(element("r", "<mixed>" + element("a") + "</mixed>"))
    without = typed(compiled, io.StringIO(xml), capture="none")
    captured = typed(compiled, io.StringIO(xml), capture="patterns")
    sites = captured.annotations.for_node(captured.root).scalar
    expected = [(0, "head"), (2, "tail")] if "head" in xml else [(1, "tail")]
    assert [(s.content_index, s.value.lexical) for s in sites] == expected
    assert all(s.kind == "text" and s.name is None for s in sites)
    assert captured.annotations.complete is True
    assert captured.annotations.for_node(captured.root).complete is True
    with pytest.raises(PyXSDError, match="capture='patterns'"):
        _ = without.annotations
    if "head" in xml:
        assert str(without.root.scalar_value()) == str(captured.root.scalar_value()) == "head"
        assert scalar_ids(without.root.scalar_value()) == scalar_ids(sites[0].value)
    else:
        assert without.root.scalar_value() is captured.root.scalar_value() is None
    # Capture-off evidence has the identical alternatives at every text site.
    reference = build_annotation_table(
        without.root,
        without.binding_evidence,
        schema_id=compiled._grammar_fingerprint,
        revision_id=without.root.revision.revision_id,
        definition_origins={},
        max_capture_entries=None,
    ).for_node(without.root)
    assert scalar_ids_from(reference) == scalar_ids_from(
        captured.annotations.for_node(captured.root)
    )
    assert [s.value.lexical for s in reference.scalar] == [s.value.lexical for s in sites]


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_mixed_tail_annotations_obey_partial_and_zero_capture_budgets(budget):
    compiled = compile_schema(element("r", "<mixed>" + element("a") + "</mixed>"))
    xml = "<r>head<a/>tail</r>"
    full = typed(compiled, io.StringIO(xml), capture="patterns")
    limited = typed(
        compiled,
        io.StringIO(xml),
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=budget),
    )
    table = limited.annotations
    assert table.complete is (budget == 2)
    entry = table.for_node(limited.root)
    assert entry.complete is (budget == 2)
    if budget:
        assert [(s.content_index, s.value.lexical) for s in entry.scalar] == [
            (0, "head"),
            (2, "tail"),
        ]
        assert scalar_ids_from(entry) == scalar_ids_from(full.annotations.for_node(full.root))
    else:
        assert entry.patterns == entry.scalar == ()
    child_entry = table.for_node(limited.root.children[0])
    assert child_entry.complete is (budget == 2)
    assert bool(child_entry.patterns) is (budget == 2)
    assert str(limited.root.scalar_value()) == str(full.root.scalar_value()) == "head"
    assert scalar_ids(limited.root.scalar_value()) == scalar_ids(full.root.scalar_value())


def test_annotations_skip_absent_text_unmatched_sites_and_untyped_attributes():
    empty_schema = compile_schema(element("doc", "<empty/>"))
    absent = typed(empty_schema, io.StringIO("<doc/>"), capture="patterns")
    absent_entry = absent.annotations.for_node(absent.root)
    assert absent_entry.patterns
    assert absent_entry.scalar == ()

    whitespace_schema = compile_schema(
        '<element name="doc"><mixed><zeroOrMore>'
        '<element name="item"><empty/></element>'
        "</zeroOrMore></mixed></element>"
    )
    whitespace = typed(
        whitespace_schema,
        io.StringIO("<doc> <item/> </doc>"),
        capture="patterns",
    )
    assert whitespace.root.text_value() == " "
    assert whitespace.root.scalar_value() is None
    assert whitespace.annotations.for_node(whitespace.root).scalar == ()

    empty_content = typed(
        empty_schema,
        io.StringIO("<doc> </doc>"),
        capture="patterns",
    )
    assert empty_content.root.text_value() == " "
    assert empty_content.root.scalar_value() is None
    assert empty_content.annotations.for_node(empty_content.root).scalar == ()

    value_schema = compile_schema(element("doc", data("integer")))
    trusted = ET.Element("doc", {"xmlns:p": "urn:values"})
    trusted.text = "12"
    trusted_document = typed(value_schema, trusted, capture="patterns")
    trusted_entry = trusted_document.annotations.for_node(trusted_document.root)
    assert [item.kind for item in trusted_entry.scalar] == ["text"]
    assert trusted_document.root.attribute_value("xmlns:p") is None


def test_list_scalar_annotations_keep_the_typed_interpretation_and_candidate_id():
    compiled = list_schema()
    document = typed(compiled, io.StringIO("<doc>1 2</doc>"), capture="patterns")

    annotations = document.annotations.for_node(document.root)
    assert len(annotations.scalar) == 1
    annotation = annotations.scalar[0]
    assert annotation.kind == "text"
    candidate = annotation.value.candidates[0]
    assert candidate.contract.kind == "list"
    assert candidate.decoded == [1, 2]
    assert annotation.value.interpretations[0].candidate_id == candidate.candidate_id

    assert document.root.scalar_value() == [1, 2]
    without = typed(compiled, io.StringIO("<doc>1 2</doc>"), capture="none")
    assert without.root.scalar_value() == [1, 2]
    again = typed(compiled, io.StringIO("<doc>1 2</doc>"), capture="patterns")
    assert scalar_ids(again.annotations.for_node(again.root).scalar[0].value) == scalar_ids(
        annotation.value
    )


def test_annotations_match_mixed_list_and_atomic_typed_selection():
    compiled = compile_schema(
        element("doc", f"<choice><list>{data('integer')}</list>{data('string')}</choice>")
    )
    document = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")

    typed_value = document.root.scalar_value()
    annotation = document.annotations.for_node(document.root).scalar[0].value

    assert isinstance(typed_value, AlternativeValue)
    assert scalar_ids(typed_value) == scalar_ids(annotation)
    list_id = next(
        candidate.candidate_id
        for candidate in annotation.candidates
        if candidate.contract.kind == "list"
    )
    assert annotation.as_candidate(list_id) == (12,)
    assert annotation.as_interpretation(list_id).as_python() == (12,)


def test_capture_off_and_on_produce_identical_typed_values_and_candidate_ids():
    compiled = ambiguous_schema()

    without = typed(compiled, io.StringIO("<doc>12</doc>"), capture="none")
    with_capture = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")

    without_value = without.root.scalar_value()
    with_value = with_capture.root.scalar_value()
    assert isinstance(without_value, AlternativeValue)
    assert isinstance(with_value, AlternativeValue)
    assert scalar_ids(without_value) == scalar_ids(with_value)

    annotations = with_capture.annotations.for_node(with_capture.root)
    assert scalar_ids(annotations.scalar[0].value) == scalar_ids(without_value)
    assert annotations.schema_id == with_capture.schema._grammar_fingerprint

    with pytest.raises(PyXSDError, match="capture='patterns'"):
        _ = without.annotations


def test_validation_only_documents_reject_annotation_capture():
    compiled = scalar_schema()
    document = validation_only(compiled, "<doc>12</doc>")

    with pytest.raises(PyXSDError) as excinfo:
        _ = document.annotations
    assert type(excinfo.value) is PyXSDError
    message = str(excinfo.value)
    assert "pattern capture is unavailable" in message
    assert "typed=True, capture='patterns'" in message

    with pytest.raises(ValueError, match="typed=True"):
        compiled.parse(io.StringIO("<doc>12</doc>"), typed=False, capture="patterns")


def test_invalid_typed_documents_expose_no_annotation_view():
    compiled = scalar_schema()
    invalid = typed(compiled, io.StringIO("<doc>words</doc>"), capture="patterns")

    assert invalid.is_valid is False
    assert invalid.binding_evidence is None
    with pytest.raises(ValidationError) as excinfo:
        _ = invalid.annotations
    assert excinfo.value.report is invalid.report

    invalid_without_capture = typed(compiled, io.StringIO("<doc>words</doc>"), capture="none")
    with pytest.raises(ValidationError) as excinfo:
        _ = invalid_without_capture.annotations
    assert excinfo.value.report is invalid_without_capture.report


def test_dirty_and_transform_stale_revisions_reject_annotation_views():
    compiled = scalar_schema()
    document = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")
    annotations = document.annotations
    before = annotations.for_node(document.root)

    document.root.set_text("13")

    assert document.interpretations_current is False
    with pytest.raises(StaleInterpretationError):
        _ = document.annotations
    with pytest.raises(StaleInterpretationError):
        document.root.scalar_value()
    assert before.scalar[0].value.candidates[0].decoded == 12
    assert before.revision_id == annotations.revision_id

    fresh = document.revalidate()

    assert fresh is not document
    assert fresh._capture == "patterns"
    assert fresh.interpretations_current is True
    assert isinstance(fresh.annotations, AnnotationTable)
    assert fresh.annotations is not annotations
    assert fresh.annotations.revision_id != annotations.revision_id
    assert fresh.annotations.for_node(fresh.root).scalar[0].value.candidates[0].decoded == 13
    with pytest.raises(StaleInterpretationError):
        _ = document.annotations


def test_transform_results_are_stale_until_revalidated_with_capture():
    compiled = scalar_schema()
    document = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")

    stale = document.transform(lambda root: root)

    assert stale.interpretations_current is False
    with pytest.raises(StaleInterpretationError):
        _ = stale.annotations

    fresh = stale.revalidate()

    assert fresh.interpretations_current is True
    assert fresh.annotations.for_node(fresh.root).scalar[0].value.candidates[0].decoded == 12


def test_capture_none_stays_stale_before_its_capability_error():
    compiled = scalar_schema()
    document = typed(compiled, io.StringIO("<doc>12</doc>"), capture="none")

    document.root.set_text("13")

    with pytest.raises(StaleInterpretationError):
        _ = document.annotations

    fresh = document.revalidate()

    assert fresh._capture == "none"
    with pytest.raises(PyXSDError, match="capture='patterns'"):
        _ = fresh.annotations


def test_patterns_and_definitions_are_stable_sorted_and_origin_sound():
    compiled = nested_schema()
    first = typed(
        compiled, io.StringIO("<doc><item>1</item><name>x</name></doc>"), capture="patterns"
    )
    second = typed(
        compiled, io.StringIO("<doc><item>1</item><name>x</name></doc>"), capture="patterns"
    )
    recompiled = typed(
        nested_schema(),
        io.StringIO("<doc><item>1</item><name>x</name></doc>"),
        capture="patterns",
    )

    root = first.annotations.for_node(first.root)
    root_again = second.annotations.for_node(second.root)
    root_recompiled = recompiled.annotations.for_node(recompiled.root)

    assert root.patterns == root_again.patterns == root_recompiled.patterns
    assert root.definitions == root_again.definitions == root_recompiled.definitions
    assert root.schema_id == root_again.schema_id == root_recompiled.schema_id
    assert root.patterns == tuple(sorted(set(root.patterns), key=origin_key))
    assert root.definitions == tuple(sorted(set(root.definitions)))
    for name in root.definitions:
        assert name in compiled.grammar.definitions
        assert set(root.patterns) & set(compiled.grammar.definitions[name].origins)

    root_definition = next(
        name
        for name, definition in compiled.grammar.definitions.items()
        if definition.pattern.name_class.contains(None, "doc")
    )
    item = first.root.children[0]
    name_node = first.root.children[1]
    item_definition = next(
        definition_name
        for definition_name, definition in compiled.grammar.definitions.items()
        if definition.pattern.name_class.contains(None, "item")
    )
    name_definition = next(
        definition_name
        for definition_name, definition in compiled.grammar.definitions.items()
        if definition.pattern.name_class.contains(None, "name")
    )
    assert root_definition in root.definitions
    assert item_definition in first.annotations.for_node(item).definitions
    assert name_definition in first.annotations.for_node(name_node).definitions


def test_a_partial_capture_budget_retains_a_sound_preorder_subset():
    compiled = nested_schema()
    document = typed(
        compiled,
        io.StringIO("<doc><item>1</item><name>x</name></doc>"),
        capture="patterns",
    )
    full = document.annotations
    nodes = preorder(document.root)
    annotated = [
        node for node in nodes if full.for_node(node).patterns or full.for_node(node).scalar
    ]
    assert len(annotated) >= 3

    partial = typed(
        compiled,
        io.StringIO("<doc><item>1</item><name>x</name></doc>"),
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=len(annotated) - 1),
    )
    table = partial.annotations
    partial_nodes = preorder(partial.root)

    assert table.complete is False
    for index in range(len(annotated) - 1):
        entry = table.for_node(partial_nodes[index])
        reference = full.for_node(nodes[index])
        assert entry.complete is False
        assert entry.patterns == reference.patterns
        assert entry.definitions == reference.definitions
        assert scalar_ids_from(entry) == scalar_ids_from(reference)
    dropped = table.for_node(partial_nodes[len(annotated) - 1])
    assert dropped.patterns == ()
    assert dropped.definitions == ()
    assert dropped.scalar == ()
    assert dropped.complete is False

    exact = typed(
        compiled,
        io.StringIO("<doc><item>1</item><name>x</name></doc>"),
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=len(annotated)),
    )
    exact_nodes = preorder(exact.root)
    assert exact.annotations.complete is True
    for index in range(len(annotated)):
        entry = exact.annotations.for_node(exact_nodes[index])
        assert entry.complete is True
        assert entry.patterns == full.for_node(nodes[index]).patterns
        assert entry.definitions == full.for_node(nodes[index]).definitions


def test_zero_capture_budget_marks_annotations_incomplete_without_truncating_typed_values():
    compiled = ambiguous_schema()
    without = typed(compiled, io.StringIO("<doc>12</doc>"), capture="none")
    truncated = typed(
        compiled,
        io.StringIO("<doc>12</doc>"),
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=0),
    )

    assert truncated.annotations.complete is False
    annotation = truncated.annotations.for_node(truncated.root)
    assert annotation.patterns == ()
    assert annotation.definitions == ()
    assert annotation.scalar == ()
    assert annotation.complete is False

    assert scalar_ids(truncated.root.scalar_value()) == scalar_ids(without.root.scalar_value())
    assert [candidate.decoded for candidate in truncated.root.scalar_value().candidates] == [
        candidate.decoded for candidate in without.root.scalar_value().candidates
    ]


def test_capture_materialization_is_lazy_and_decoder_failure_surfaces_on_access():
    ExplodingDatatype.calls = 0
    compiled = compile_schema(
        element("doc", custom_data("explodes")),
        datatype_libraries=CUSTOM_LIBRARIES,
    )

    without = typed(compiled, io.StringIO("<doc>boom</doc>"), capture="none")
    with_capture = typed(compiled, io.StringIO("<doc>boom</doc>"), capture="patterns")

    assert ExplodingDatatype.calls == 0
    assert without.is_valid and with_capture.is_valid
    assert with_capture.binding_evidence is not None
    assert with_capture.interpretations_current is True

    with pytest.raises(RuntimeError, match="decoder exploded"):
        _ = with_capture.annotations
    assert ExplodingDatatype.calls == 1

    with pytest.raises(PyXSDError, match="capture='patterns'"):
        _ = without.annotations
    assert ExplodingDatatype.calls == 1


def test_annotation_materialization_is_cached_after_one_build():
    CountingDatatype.calls = 0
    compiled = compile_schema(
        element("doc", custom_data("counts")),
        datatype_libraries=CUSTOM_LIBRARIES,
    )
    document = typed(compiled, io.StringIO("<doc>boom</doc>"), capture="patterns")

    assert CountingDatatype.calls == 0
    first = document.annotations
    assert CountingDatatype.calls == 1
    second = document.annotations
    assert second is first
    assert CountingDatatype.calls == 1
    assert first.for_node(document.root).scalar[0].value.candidates[0].decoded == "boom"
    assert CountingDatatype.calls == 1

    fresh = document.revalidate()
    assert CountingDatatype.calls == 1
    fresh_table = fresh.annotations
    assert fresh_table is not first
    assert CountingDatatype.calls == 2


def test_capture_budgets_never_materialize_dropped_scalar_records():
    ExplodingDatatype.calls = 0
    compiled = compile_schema(
        element(
            "doc",
            element("value", custom_data("explodes")) + element("ok", data("integer")),
        ),
        datatype_libraries=CUSTOM_LIBRARIES,
    )
    xml = "<doc><value>boom</value><ok>7</ok></doc>"

    zero = typed(
        compiled,
        io.StringIO(xml),
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=0),
    )
    zero_table = zero.annotations
    assert zero_table.complete is False
    assert zero_table.for_node(zero.root).patterns == ()
    assert ExplodingDatatype.calls == 0
    assert zero.root.children[1].scalar_value() == 7

    partial = typed(
        compiled,
        io.StringIO(xml),
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=1),
    )
    partial_table = partial.annotations
    assert partial_table.complete is False
    root_entry = partial_table.for_node(partial.root)
    assert root_entry.patterns
    assert root_entry.scalar == ()
    assert root_entry.complete is False
    assert partial_table.for_node(partial.root.children[0]).patterns == ()
    assert ExplodingDatatype.calls == 0
    assert partial.root.children[1].scalar_value() == 7

    retained = typed(
        compiled,
        io.StringIO(xml),
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=2),
    )
    assert ExplodingDatatype.calls == 0
    with pytest.raises(RuntimeError, match="decoder exploded"):
        _ = retained.annotations
    assert ExplodingDatatype.calls == 1


def test_a_retained_list_site_keeps_all_candidates_under_a_partial_budget():
    compiled = compile_schema(
        element(
            "doc",
            element(
                "values",
                f"<choice><list><zeroOrMore>{data('integer')}</zeroOrMore></list>"
                f"{data('string')}</choice>",
            )
            + element("tail", "<empty/>"),
        )
    )
    document = typed(
        compiled,
        io.StringIO("<doc><values>1 2</values><tail/></doc>"),
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=2),
    )

    table = document.annotations
    assert table.complete is False
    values = document.root.children[0]
    tail = document.root.children[1]
    entry = table.for_node(values)
    assert entry.complete is False
    assert len(entry.scalar) == 1
    value = entry.scalar[0].value
    assert len(value.candidates) == 2
    list_id = next(
        candidate.candidate_id
        for candidate in value.candidates
        if candidate.contract.kind == "list"
    )
    assert value.as_candidate(list_id) == [1, 2]
    assert value.as_interpretation(list_id).as_python() == [1, 2]
    assert table.for_node(tail).patterns == ()

    typed_value = values.scalar_value()
    assert isinstance(typed_value, AlternativeValue)
    assert scalar_ids(typed_value) == scalar_ids(value)


def test_annotations_reject_unknown_elements_and_foreign_revisions():
    compiled = scalar_schema()
    document = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")
    other = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")

    with pytest.raises(ValueError):
        document.annotations.for_node(other.root)
    with pytest.raises(ValueError):
        document.annotations.for_node(object())  # type: ignore[arg-type]
    foreign = RelaxNGElement.from_etree(ET.Element("doc"))
    with pytest.raises(ValueError):
        document.annotations.for_node(foreign)


def test_recovered_findings_never_become_accepting_annotation_origins():
    compiled = compile_schema(
        element("doc", element("item", "<text/>") + element("extra", "<empty/>"))
    )
    document = typed(compiled, io.StringIO("<doc><wrong/></doc>"), capture="patterns")

    assert document.is_valid is False
    assert document.binding_evidence is None
    with pytest.raises(ValidationError):
        _ = document.annotations


def test_node_annotations_normalize_and_validate_public_values():
    compiled = scalar_schema()
    document = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")
    text = document.annotations.for_node(document.root).scalar[0]
    attribute_value = text.value

    first = SourceOrigin("b", IssueLocation("b.rng", (PathStep("element", 1),)))
    second = SourceOrigin("a", IssueLocation("a.rng", (PathStep("element", 1),)))
    sourceless = SourceOrigin("c", None)
    attribute_annotation = ScalarAnnotation("attribute", "a", None, attribute_value)

    normalized = NodeAnnotations(
        "schema",
        7,
        (sourceless, first, second, first),
        ("z", "a", "z"),
        (attribute_annotation, text),
        True,
    )

    assert normalized.patterns == (second, first, sourceless)
    assert normalized.definitions == ("a", "z")
    assert [item.kind for item in normalized.scalar] == ["text", "attribute"]

    kwargs = {
        "schema_id": "schema",
        "revision_id": 7,
        "patterns": (),
        "definitions": (),
        "scalar": (),
        "complete": True,
    }
    with pytest.raises(ValueError, match="schema_id"):
        NodeAnnotations(**{**kwargs, "schema_id": ""})
    with pytest.raises(ValueError, match="revision_id"):
        NodeAnnotations(**{**kwargs, "revision_id": True})
    with pytest.raises(TypeError, match="complete"):
        NodeAnnotations(**{**kwargs, "complete": 1})
    with pytest.raises(TypeError, match="patterns"):
        NodeAnnotations(**{**kwargs, "patterns": (object(),)})
    with pytest.raises(TypeError, match="definitions"):
        NodeAnnotations(**{**kwargs, "definitions": (object(),)})
    with pytest.raises(TypeError, match="scalar"):
        NodeAnnotations(**{**kwargs, "scalar": (object(),)})


def test_scalar_annotations_validate_their_site_shape():
    compiled = scalar_schema()
    document = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")
    value = document.annotations.for_node(document.root).scalar[0].value

    text = ScalarAnnotation("text", None, 0, value)
    attribute_annotation = ScalarAnnotation("attribute", "a", None, value)
    assert (text.kind, text.name, text.content_index) == ("text", None, 0)
    assert (attribute_annotation.kind, attribute_annotation.name) == ("attribute", "a")

    with pytest.raises(ValueError, match="name"):
        ScalarAnnotation("text", "a", 0, value)
    with pytest.raises(ValueError, match="content_index"):
        ScalarAnnotation("text", None, None, value)
    with pytest.raises(ValueError, match="name"):
        ScalarAnnotation("attribute", None, None, value)
    with pytest.raises(ValueError, match="content_index"):
        ScalarAnnotation("attribute", "a", 0, value)
    with pytest.raises(ValueError, match="kind"):
        ScalarAnnotation("other", None, None, value)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="value"):
        ScalarAnnotation("text", None, 0, object())  # type: ignore[arg-type]


def test_builder_ignores_nodes_with_no_surviving_annotation_entries():
    compiled = compile_schema(element("doc", "<empty/>"))
    document = typed(compiled, io.StringIO("<doc/>"), capture="none")
    evidence = document.binding_evidence
    assert evidence is not None

    table = build_annotation_table(
        document.root,
        evidence,
        schema_id="schema",
        revision_id=document.root.revision.revision_id,
        definition_origins={},
        max_capture_entries=0,
    )

    assert table.complete is True
    entry = table.for_node(document.root)
    assert entry.patterns == ()
    assert entry.definitions == ()
    assert entry.scalar == ()
    assert entry.complete is True


def test_builder_keeps_scalar_only_records_when_origins_are_absent():
    text_compiled = compile_schema(element("doc", data("integer")))
    text_document = typed(text_compiled, io.StringIO("<doc>12</doc>"), capture="none")
    text_evidence = text_document.binding_evidence
    assert text_evidence is not None
    text_table = build_annotation_table(
        text_document.root,
        text_evidence,
        schema_id="schema",
        revision_id=text_document.root.revision.revision_id,
        definition_origins={},
        max_capture_entries=None,
    )
    text_entry = text_table.for_node(text_document.root)
    assert text_entry.patterns == ()
    assert len(text_entry.scalar) == 1
    assert text_entry.scalar[0].value.as_python() == 12
    assert text_table.complete is True

    attribute_compiled = compile_schema(
        element("doc", attribute("a", data("integer")) + "<empty/>")
    )
    attribute_document = typed(
        attribute_compiled,
        io.StringIO('<doc a="1"/>'),
        capture="none",
    )
    attribute_evidence = attribute_document.binding_evidence
    assert attribute_evidence is not None
    attribute_table = build_annotation_table(
        attribute_document.root,
        attribute_evidence,
        schema_id="schema",
        revision_id=attribute_document.root.revision.revision_id,
        definition_origins={},
        max_capture_entries=None,
    )
    attribute_entry = attribute_table.for_node(attribute_document.root)
    assert attribute_entry.patterns == ()
    assert [item.name for item in attribute_entry.scalar] == ["a"]
    assert attribute_entry.scalar[0].value.as_python() == 1
    assert attribute_table.complete is True

    flag_compiled = compile_schema(
        '<element name="doc"><optional><attribute name="flag"><empty/></attribute>'
        "</optional><empty/></element>"
    )
    flag_document = typed(flag_compiled, io.StringIO('<doc flag=""/>'), capture="none")
    flag_evidence = flag_document.binding_evidence
    assert flag_evidence is not None
    flag_table = build_annotation_table(
        flag_document.root,
        flag_evidence,
        schema_id="schema",
        revision_id=flag_document.root.revision.revision_id,
        definition_origins={},
        max_capture_entries=None,
    )
    flag_entry = flag_table.for_node(flag_document.root)
    assert flag_entry.patterns == ()
    assert flag_entry.scalar == ()
    assert flag_table.complete is True


def test_direct_document_construction_keeps_annotations_parse_scoped():
    compiled = scalar_schema()
    document = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")
    other = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")
    common = {
        "schema": document.schema,
        "root": document.root,
        "source": document.source,
        "report": document.report,
    }

    rebuilt = relaxng.RelaxNGDocument(
        **common,
        typed=True,
        capture="patterns",
        binding_evidence=document.binding_evidence,
        annotations=document.annotations,
    )
    assert rebuilt.annotations.for_node(rebuilt.root).patterns

    with pytest.raises(TypeError, match="annotations"):
        relaxng.RelaxNGDocument(
            **common,
            typed=True,
            capture="patterns",
            binding_evidence=document.binding_evidence,
            annotations=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(PyXSDError, match="annotation"):
        relaxng.RelaxNGDocument(
            **common,
            typed=True,
            capture="patterns",
            binding_evidence=document.binding_evidence,
        )
    with pytest.raises(ValueError, match="capture"):
        relaxng.RelaxNGDocument(
            **common,
            typed=True,
            capture="none",
            binding_evidence=document.binding_evidence,
            annotations=document.annotations,
        )
    with pytest.raises(ValueError, match="annotations"):
        relaxng.RelaxNGDocument(
            **common,
            typed=False,
            annotations=document.annotations,
        )
    invalid = typed(compiled, io.StringIO("<doc>words</doc>"), capture="patterns")
    with pytest.raises(ValueError, match="invalid"):
        relaxng.RelaxNGDocument(
            schema=invalid.schema,
            root=invalid.root,
            source=invalid.source,
            report=invalid.report,
            typed=True,
            capture="patterns",
            annotations=document.annotations,
        )
    with pytest.raises(ValueError, match="revision"):
        relaxng.RelaxNGDocument(
            **common,
            typed=True,
            capture="patterns",
            binding_evidence=document.binding_evidence,
            annotations=other.annotations,
        )
