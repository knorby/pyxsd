"""Adversarial RELAX NG evidence budgets, recursion, and parse-local cleanup.

Each mandatory budget is observed independently and raises before a typed
document escapes.  ``typed=False`` never computes evidence, so those budgets
cannot change its verdict or report.  The optional capture budget is a
separate, truncatable annotation view that never changes typed values or
candidate identities.  Recursive and interleaved documents must finish
iteratively, and repeated parses must not accumulate instance-specific state
in the compiled schema or retain dropped documents.
"""

from __future__ import annotations

import gc
import io
import weakref
from xml.etree import ElementTree as ET

import pytest

from pyxsd.relaxng import (
    AlternativeValue,
    RelaxNGDocument,
    RelaxNGLimitError,
    RelaxNGLimits,
    RelaxNGSchema,
)
from pyxsd.relaxng.datatypes import (
    BuiltinDatatypeFactory,
    DatatypeContext,
    XsdDatatypeFactory,
)
from pyxsd.relaxng.evidence import (
    EvidenceGraph,
    EvidenceOccurrence,
    build_binding_evidence,
)
from pyxsd.relaxng.names import Name
from pyxsd.relaxng.patterns import Choice, Data, Element, Empty, Group, Pattern
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.validator import validate_element

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"

CX = DatatypeContext(None, {})
INTEGER = XsdDatatypeFactory().create("integer", (), CX)
STRING = BuiltinDatatypeFactory().create("string", (), CX)


def data(type_name: str) -> str:
    return f'<data type="{type_name}" datatypeLibrary="{XSD}"/>'


def element(name: str, content: str = "<empty/>") -> str:
    return f'<element name="{name}">{content}</element>'


def compile_schema(body: str) -> RelaxNGSchema:
    source = f'<grammar xmlns="{NS}"><start>{body}</start></grammar>'
    compiled = RelaxNGSchema.compile(io.StringIO(source))
    assert not compiled.report.has_errors, [issue.message for issue in compiled.report.issues]
    return compiled


def compile_grammar(source: str) -> RelaxNGSchema:
    compiled = RelaxNGSchema.compile(io.StringIO(source))
    assert not compiled.report.has_errors, [issue.message for issue in compiled.report.issues]
    return compiled


def pattern_element(name: str, content: Pattern | None = None) -> Element:
    return Element(Name(None, name), Empty() if content is None else content)


def tree(xml: str) -> RelaxNGElement:
    return RelaxNGElement.from_etree(ET.fromstring(xml))


def evidence(root: RelaxNGElement, start: Pattern) -> EvidenceGraph:
    assert validate_element(root, start).valid
    return build_binding_evidence(
        root,
        start,
        datatype_context=CX,
        limits=None,
    )


def text_site(graph: EvidenceGraph, node: RelaxNGElement) -> EvidenceOccurrence:
    matches = [
        occurrence
        for occurrence in graph.occurrences
        if occurrence.element is node and occurrence.kind == "text"
    ]
    assert len(matches) == 1
    return matches[0]


def summary_count(graph: EvidenceGraph) -> int:
    """Count retained schema-static scalar-pattern records.

    One record is one occurrence/pattern pair, the independent
    ``max_binding_summaries`` unit.  It is deliberately not a proof node, an
    edge, a surviving scalar witness, or a capture record.
    """
    return sum(len(patterns) for patterns in graph._schema_patterns.values())


def recursive_interleaved_schema() -> RelaxNGSchema:
    return compile_grammar(
        f"""<grammar xmlns="{NS}">
          <start><ref name="node"/></start>
          <define name="node">
            <element name="n">
              <interleave>
                <optional>
                  <attribute name="a">
                    <choice>{data("integer")}{data("string")}</choice>
                  </attribute>
                </optional>
                <optional><ref name="node"/></optional>
              </interleave>
            </element>
          </define>
        </grammar>"""
    )


def nested_attributes(depth: int) -> str:
    return "".join(f'<n a="{index}">' for index in range(depth)) + "</n>" * depth


def attribute_candidates(document: RelaxNGDocument) -> tuple[tuple[str, ...], ...]:
    candidates: list[tuple[str, ...]] = []
    node: RelaxNGElement | None = document.root
    while node is not None:
        value = node.attribute_value("a")
        assert isinstance(value, AlternativeValue)
        candidates.append(tuple(candidate.candidate_id for candidate in value.candidates))
        node = node.children[0] if node.children else None
    return tuple(candidates)


def nested_depth(document: RelaxNGDocument) -> int:
    depth = 0
    node = document.root
    while node.children:
        node = node.children[0]
        depth += 1
    return depth


def test_state_and_edge_budgets_have_their_own_inclusive_boundaries() -> None:
    start = pattern_element("r", Choice(Data(INTEGER), Data(STRING)))
    root = tree("<r>word</r>")
    graph = evidence(root, start)

    states = len(graph.retained_nodes)
    edges = len(graph.retained_edges)
    assert states == edges > 0

    for budget, count in (
        ("max_evidence_states", states),
        ("max_evidence_edges", edges),
    ):
        assert build_binding_evidence(
            root,
            start,
            datatype_context=CX,
            limits=RelaxNGLimits(**{budget: count}),
        ).accepting_roots
        with pytest.raises(RelaxNGLimitError) as caught:
            build_binding_evidence(
                root,
                start,
                datatype_context=CX,
                limits=RelaxNGLimits(**{budget: count - 1}),
            )
        assert caught.value.stage == "evidence"
        assert caught.value.budget == budget
        assert caught.value.configured == count - 1
        assert caught.value.observed == count


def test_witness_and_summary_budgets_count_different_units() -> None:
    start = pattern_element("r", Choice(Data(INTEGER), Data(STRING)))
    root = tree("<r>word</r>")
    graph = evidence(root, start)
    occurrence = text_site(graph, root)

    # The integer branch rejects "word" locally, so only one witness survives,
    # while both schema-static scalar bindings remain recorded for wrapper
    # selection.  A summary is therefore not a witness or a proof node.
    assert len(graph.scalar_witnesses_at(occurrence)) == 1
    assert len(graph.schema_contracts_at(occurrence)) == 2
    summaries = summary_count(graph)
    assert summaries == 2

    assert build_binding_evidence(
        root,
        start,
        datatype_context=CX,
        limits=RelaxNGLimits(max_scalar_witnesses=1, max_binding_summaries=2),
    ).accepting_roots
    with pytest.raises(RelaxNGLimitError) as caught:
        build_binding_evidence(
            root,
            start,
            datatype_context=CX,
            # Both other units are exactly within their own budgets, so the
            # failure below can only belong to the witness counter.
            limits=RelaxNGLimits(max_scalar_witnesses=0, max_binding_summaries=2),
        )
    assert caught.value.stage == "evidence"
    assert caught.value.budget == "max_scalar_witnesses"
    assert caught.value.configured == 0
    assert caught.value.observed == 1

    assert build_binding_evidence(
        root,
        start,
        datatype_context=CX,
        limits=RelaxNGLimits(max_scalar_witnesses=1, max_binding_summaries=2),
    ).accepting_roots
    with pytest.raises(RelaxNGLimitError) as caught:
        build_binding_evidence(
            root,
            start,
            datatype_context=CX,
            # The witness count remains within its budget; only the retained
            # summary pair (two records) crosses the summary budget.
            limits=RelaxNGLimits(max_scalar_witnesses=1, max_binding_summaries=1),
        )
    assert caught.value.stage == "evidence"
    assert caught.value.budget == "max_binding_summaries"
    assert caught.value.configured == 1
    assert caught.value.observed == 2


def test_zero_summaries_succeed_at_a_zero_summary_budget() -> None:
    start = pattern_element("r", pattern_element("x"))
    root = tree("<r><x/></r>")
    graph = evidence(root, start)

    assert summary_count(graph) == 0
    bounded = build_binding_evidence(
        root,
        start,
        datatype_context=CX,
        limits=RelaxNGLimits(max_binding_summaries=0),
    )
    assert bounded.accepting_roots


def test_summary_budget_counts_schema_static_records_from_dead_branches() -> None:
    start = pattern_element(
        "r",
        Choice(
            Group(pattern_element("x", Data(INTEGER)), pattern_element("a")),
            Group(pattern_element("x", Data(STRING)), pattern_element("b")),
        ),
    )
    root = tree("<r><x>12</x><b/></r>")
    graph = evidence(root, start)
    occurrence = text_site(graph, root.children[0])

    # Only the string branch is in the accepting proof, but the integer
    # binding remains a schema-static summary for wrapper selection.
    assert len(graph.scalar_witnesses_at(occurrence)) == 1
    assert len(graph.schema_contracts_at(occurrence)) == 2
    assert summary_count(graph) == 2

    with pytest.raises(RelaxNGLimitError) as caught:
        build_binding_evidence(
            root,
            start,
            datatype_context=CX,
            limits=RelaxNGLimits(max_binding_summaries=1),
        )
    assert caught.value.budget == "max_binding_summaries"
    assert caught.value.observed == 2


def test_a_large_budget_succeeds_for_every_mandatory_unit() -> None:
    start = pattern_element("r", Choice(Data(INTEGER), Data(STRING)))
    root = tree("<r>word</r>")

    graph = build_binding_evidence(
        root,
        start,
        datatype_context=CX,
        limits=RelaxNGLimits(
            max_evidence_states=1000,
            max_evidence_edges=1000,
            max_scalar_witnesses=1000,
            max_binding_summaries=1000,
        ),
    )

    assert graph.accepting_roots
    assert summary_count(graph) == 2


@pytest.mark.parametrize(
    "budget",
    [
        "max_evidence_states",
        "max_evidence_edges",
        "max_scalar_witnesses",
        "max_binding_summaries",
    ],
)
def test_mandatory_budgets_never_change_validation_only_results(budget: str) -> None:
    schema = compile_schema(element("r", data("integer")))
    limits = RelaxNGLimits(**{budget: 0})

    with pytest.raises(RelaxNGLimitError) as caught:
        schema.parse(io.StringIO("<r>12</r>"), typed=True, limits=limits)
    assert caught.value.stage == "evidence"
    assert caught.value.budget == budget

    validation_only = schema.parse(io.StringIO("<r>12</r>"), typed=False, limits=limits)
    assert validation_only.is_valid
    assert validation_only.typed is False
    assert validation_only.binding_evidence is None
    assert not validation_only.report.has_errors
    assert validation_only.report.diagnostics_complete is True

    invalid = schema.parse(io.StringIO("<r><x/></r>"), typed=False, limits=limits)
    assert invalid.is_valid is False
    assert invalid.report.has_errors
    assert invalid.report.diagnostics_complete is True
    baseline = schema.parse(io.StringIO("<r><x/></r>"), typed=False)
    assert [
        (issue.code, issue.message, issue.location, issue.phase) for issue in invalid.report.issues
    ] == [
        (issue.code, issue.message, issue.location, issue.phase) for issue in baseline.report.issues
    ]

    invalid_typed = schema.parse(io.StringIO("<r><x/></r>"), typed=True, limits=limits)
    assert invalid_typed.is_valid is False
    assert invalid_typed.report.has_errors
    assert invalid_typed.binding_evidence is None


def test_capture_budget_is_incomplete_without_changing_typed_values() -> None:
    schema = compile_schema(
        element(
            "record",
            "<optional>"
            f'<attribute name="kind"><choice>{data("integer")}{data("string")}</choice></attribute>'
            "</optional>"
            f"<optional>{element('count', data('integer'))}</optional>",
        )
    )
    xml = '<record kind="7"><count>7</count></record>'

    unlimited = schema.parse(io.StringIO(xml), typed=True, capture="patterns")
    unlimited_kind = unlimited.root.attribute_value("kind")
    assert isinstance(unlimited_kind, AlternativeValue)
    assert unlimited.root.children[0].scalar_value() == 7
    assert unlimited.annotations.complete is True

    for budget in (0, 1):
        document = schema.parse(
            io.StringIO(xml),
            typed=True,
            capture="patterns",
            limits=RelaxNGLimits(max_capture_entries=budget),
        )

        assert document.is_valid
        assert document.binding_evidence is not None
        assert len(document.binding_evidence.retained_nodes) == len(
            unlimited.binding_evidence.retained_nodes
        )
        assert summary_count(document.binding_evidence) == summary_count(unlimited.binding_evidence)
        kind = document.root.attribute_value("kind")
        assert isinstance(kind, AlternativeValue)
        assert tuple(candidate.candidate_id for candidate in kind.candidates) == tuple(
            candidate.candidate_id for candidate in unlimited_kind.candidates
        )
        assert document.root.children[0].scalar_value() == 7

        table = document.annotations
        assert table.complete is False
        child = document.root.children[0]
        assert table.for_node(child).scalar == ()
        assert table.for_node(child).complete is False
        if budget == 0:
            assert table.for_node(document.root).scalar == ()
        else:
            (retained,) = table.for_node(document.root).scalar
            assert retained.kind == "attribute"
            assert retained.name == "kind"
            assert retained.value is not None
            assert tuple(
                candidate.candidate_id for candidate in retained.value.candidates
            ) == tuple(candidate.candidate_id for candidate in kind.candidates)

    exact = schema.parse(
        io.StringIO(xml),
        typed=True,
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=2),
    )
    assert exact.is_valid
    assert exact.annotations.complete is True
    assert exact.annotations.for_node(exact.root).scalar
    child = exact.root.children[0]
    (child_scalar,) = exact.annotations.for_node(child).scalar
    assert child_scalar.value is not None
    assert child.scalar_value() == 7


def test_capture_budget_does_not_fail_an_unread_annotation_parse() -> None:
    schema = compile_schema(element("r", data("integer")))

    document = schema.parse(
        io.StringIO("<r>12</r>"),
        typed=True,
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=0),
    )

    assert document.is_valid
    assert document.binding_evidence is not None
    assert document.root.scalar_value() == 12


def test_capture_budget_never_masks_or_replaces_mandatory_evidence() -> None:
    schema = compile_schema(element("r", data("integer")))

    with pytest.raises(RelaxNGLimitError) as caught:
        schema.parse(
            io.StringIO("<r>12</r>"),
            typed=True,
            capture="patterns",
            limits=RelaxNGLimits(max_capture_entries=0, max_evidence_states=0),
        )
    assert caught.value.stage == "evidence"
    assert caught.value.budget == "max_evidence_states"

    document = schema.parse(
        io.StringIO("<r>12</r>"),
        typed=True,
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=0),
    )
    assert document.is_valid
    assert document.root.scalar_value() == 12
    assert document.annotations.complete is False


@pytest.mark.parametrize("capture", ["none", "patterns"])
@pytest.mark.parametrize("depth", [1, 2, 5])
def test_candidate_ids_are_stable_across_depths_and_capture_modes(capture: str, depth: int) -> None:
    schema = recursive_interleaved_schema()
    baseline_document = schema.parse(io.StringIO(nested_attributes(1)), typed=True, capture="none")
    baseline = attribute_candidates(baseline_document)

    document = schema.parse(io.StringIO(nested_attributes(depth)), typed=True, capture=capture)

    assert document.is_valid
    assert document.binding_evidence is not None
    candidates = attribute_candidates(document)
    assert len(candidates) == depth
    assert all(candidate_ids == baseline[0] for candidate_ids in candidates)
    assert len(baseline[0]) == 2

    if capture == "patterns":
        table = document.annotations
        assert table.complete is True
        node = document.root
        while node is not None:
            annotations = table.for_node(node)
            (scalar,) = annotations.scalar
            assert scalar.name == "a"
            assert scalar.value is not None
            assert (
                tuple(candidate.candidate_id for candidate in scalar.value.candidates)
                == baseline[0]
            )
            node = node.children[0] if node.children else None


def test_deep_recursive_interleaved_evidence_is_iterative() -> None:
    schema = recursive_interleaved_schema()
    # Deeper than the default Python recursion limit: document recursion must
    # stay iterative, and a finite budget must trip long before that depth.
    depth = 1200
    xml = nested_attributes(depth)

    document = schema.parse(io.StringIO(xml), typed=True)

    assert document.is_valid
    assert len(attribute_candidates(document)) == depth
    assert nested_depth(document) == depth - 1

    with pytest.raises(RelaxNGLimitError) as caught:
        schema.parse(io.StringIO(xml), typed=True, limits=RelaxNGLimits(max_evidence_states=8))
    assert caught.value.stage == "evidence"
    assert caught.value.budget == "max_evidence_states"
    assert caught.value.configured == 8
    assert caught.value.observed == 9


def test_deep_nested_grammar_evidence_is_iterative() -> None:
    depth = 120
    inner = "<text/>"
    for _ in range(depth):
        inner = f"<group>{inner}<text/></group>"
    schema = compile_schema(element("r", inner))

    document = schema.parse(io.StringIO("<r>text</r>"), typed=True)

    assert document.is_valid
    value = document.root.scalar_value()
    assert str(value) == "text"


def test_interleaved_children_keep_both_typed_values() -> None:
    schema = compile_schema(
        element(
            "r",
            f"<interleave>{element('x', data('integer'))}"
            f"{element('y', data('string'))}</interleave>",
        )
    )

    document = schema.parse(io.StringIO("<r><y>word</y><x>12</x></r>"), typed=True)

    assert document.is_valid
    assert document.root.children[0].scalar_value() == "word"
    assert document.root.children[1].scalar_value() == 12


def test_repeated_parses_release_parse_state_and_leave_schema_caches_alone() -> None:
    schema = recursive_interleaved_schema()
    xml = nested_attributes(3)

    warm = schema.parse(io.StringIO(xml), typed=True, capture="patterns")
    assert warm.annotations.complete is True
    del warm
    gc.collect()

    keys = frozenset(vars(schema))
    identities = {name: id(value) for name, value in vars(schema).items()}
    fingerprint = schema._grammar_fingerprint

    references = []
    for _ in range(4):
        document = schema.parse(io.StringIO(xml), typed=True, capture="patterns")
        table = document.annotations
        assert document.binding_evidence is not None
        references.append(
            (
                weakref.ref(document.binding_evidence),
                weakref.ref(document.root),
                weakref.ref(table),
            )
        )
        del table, document

    gc.collect()

    for group in references:
        assert all(reference() is None for reference in group)
    assert frozenset(vars(schema)) == keys
    assert {name: id(value) for name, value in vars(schema).items()} == identities
    assert schema._grammar_fingerprint == fingerprint
