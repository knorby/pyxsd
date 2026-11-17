"""Accepting event proofs, not local datatype matches or origin unions."""

from xml.etree import ElementTree as ET

import pytest

from pyxsd.relaxng.datatypes import (
    BuiltinDatatypeFactory,
    DatatypeContext,
    XsdDatatypeFactory,
)
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.names import Name
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Data,
    DataExcept,
    Element,
    Empty,
    Group,
    Interleave,
    List,
    NotAllowed,
    OneOrMore,
    Pattern,
    PatternPool,
    Ref,
    Text,
    Value,
)
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.validator import validate_element

CX = DatatypeContext(None, {})
STRING = BuiltinDatatypeFactory().create("string", (), CX)
INTEGER = XsdDatatypeFactory().create("integer", (), CX)


def element(name, content=None):
    return Element(Name(None, name), Empty() if content is None else content)


def tree(xml):
    return RelaxNGElement.from_etree(ET.fromstring(xml))


def evidence(root, start, **kwargs):
    # Import here so red tests fail at the missing production entry.
    from pyxsd.relaxng.evidence import build_binding_evidence

    assert validate_element(root, start).valid
    return build_binding_evidence(root, start, datatype_context=CX, limits=None, **kwargs)


def site(graph, node, *, kind="text", token_index=None):
    matches = [
        occurrence
        for occurrence in graph.occurrences
        if occurrence.element is node
        and occurrence.kind == kind
        and occurrence.token_index == token_index
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize("interned", [False, True])
def test_late_failure_removes_integer_witness_even_with_interned_residuals(interned):
    pool = PatternPool()
    rejected = OriginTable(frozenset([SourceOrigin("integer-branch", None)]))
    accepted = OriginTable(frozenset([SourceOrigin("string-branch", None)]))
    integer = pool.intern(Data(INTEGER), rejected)
    string = pool.intern(Data(STRING), accepted)
    start = element(
        "r",
        Choice(
            Group(element("x", integer), element("a")),
            Group(element("x", string), element("b")),
        ),
    )
    if interned:
        start = pool.intern(start)
    root = tree("<r><x>12</x><b/></r>")
    graph = evidence(root, start, origins=pool.origins)
    witnesses = graph.scalar_witnesses_at(site(graph, root.children[0]))
    assert [w.datatype for w in witnesses] == [STRING]
    assert witnesses[0].lexical == "12"
    assert graph.origins_at(site(graph, root.children[0])) == accepted
    assert graph.accepting_roots
    assert all(root in graph.retained_nodes for root in graph.accepting_roots)


def test_identical_semantic_sites_expose_origin_union_not_branch_selection():
    pool = PatternPool()
    origins = OriginTable(frozenset([SourceOrigin("first", None), SourceOrigin("second", None)]))
    first = pool.intern(Data(STRING), OriginTable(frozenset([SourceOrigin("first", None)])))
    second = pool.intern(Data(STRING), OriginTable(frozenset([SourceOrigin("second", None)])))
    assert first is second
    start = pool.intern(
        element(
            "r",
            Choice(
                Group(element("x", first), element("a")), Group(element("x", second), element("b"))
            ),
        )
    )
    root = tree("<r><x>12</x><b/></r>")
    graph = evidence(root, start, origins=pool.origins)
    occurrence = site(graph, root.children[0])
    assert graph.origins_at(occurrence) == origins
    assert [w.pattern for w in graph.scalar_witnesses_at(occurrence)] == [first]


def test_distinct_occurrences_do_not_collapse_when_residuals_are_equal():
    scalar = Data(STRING)
    start = element("r", Group(element("x", scalar), element("x", scalar)))
    root = tree("<r><x>12</x><x>12</x></r>")
    graph = evidence(root, start)
    left, right = (site(graph, child) for child in root.children)
    assert left is not right
    (a,) = graph.scalar_witnesses_at(left)
    (b,) = graph.scalar_witnesses_at(right)
    assert a.occurrence is left and b.occurrence is right
    assert a is not b
    assert graph.origins_at(left) == OriginTable()


def test_interleave_requires_both_scalar_witnesses():
    start = element("r", Interleave(element("x", Data(INTEGER)), element("y", Data(STRING))))
    root = tree("<r><y>words</y><x>12</x></r>")
    graph = evidence(root, start)
    assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, root.children[0]))] == [
        STRING
    ]
    assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, root.children[1]))] == [
        INTEGER
    ]
    assert any(edge.kind == "and" and len(edge.successors) > 1 for edge in graph.retained_edges)


def test_valid_ambiguity_keeps_every_semantic_witness_in_deterministic_order():
    root = tree("<r>12</r>")
    start = element("r", Choice(Data(INTEGER), Data(STRING)))
    graph = evidence(root, start)
    occurrence = site(graph, root)
    witnesses = graph.scalar_witnesses_at(occurrence)
    # Witness-free close retains the validator's canonical semantic choice order.
    assert [w.datatype for w in witnesses] == [STRING, INTEGER]
    assert witnesses == graph.scalar_witnesses_at(occurrence)
    assert any(edge.kind == "or" for edge in graph.retained_edges)


def test_list_tokens_keep_spans_context_and_correlated_paths():
    root = tree("<r> 12\tword </r>")
    start = element("r", List(Group(Data(INTEGER), Data(STRING))))
    graph = evidence(root, start)
    (outer,) = graph.scalar_witnesses_at(site(graph, root))
    assert [(s.text, s.start, s.end) for s in outer.token_spans] == [("12", 1, 3), ("word", 4, 8)]
    for index, datatype in enumerate((INTEGER, STRING)):
        (witness,) = graph.scalar_witnesses_at(site(graph, root, kind="token", token_index=index))
        assert witness.datatype == datatype
        assert witness.context == CX


@pytest.mark.parametrize(
    "budget", ["max_evidence_states", "max_evidence_edges", "max_scalar_witnesses"]
)
def test_mandatory_evidence_budgets_raise_instead_of_truncating(budget):
    from pyxsd.relaxng.evidence import build_binding_evidence
    from pyxsd.relaxng.limits import RelaxNGLimits

    with pytest.raises(RelaxNGLimitError) as error:
        build_binding_evidence(
            tree("<r>12</r>"),
            element("r", Data(INTEGER)),
            datatype_context=CX,
            limits=RelaxNGLimits(**{budget: 0}),
        )
    assert error.value.stage == "evidence"
    assert error.value.budget == budget
    assert error.value.observed == 1


def test_invalid_input_is_an_internal_precondition_error_not_empty_evidence():
    from pyxsd.relaxng.evidence import build_binding_evidence

    with pytest.raises(RuntimeError, match="complete accepting derivation"):
        build_binding_evidence(
            tree("<r>bad</r>"),
            element("r", Data(INTEGER)),
            datatype_context=CX,
            limits=None,
        )


@pytest.mark.parametrize("in_list", [False, True])
def test_data_except_cannot_borrow_an_excluded_result_when_its_base_rejects(in_list):
    excluded = Data(INTEGER)
    content = Choice(Choice(excluded, Data(STRING)), DataExcept(INTEGER, excluded))
    start = element("r", List(content) if in_list else content)
    root = tree("<r>bad</r>")
    graph = evidence(root, start)
    occurrence = site(graph, root, kind="token", token_index=0) if in_list else site(graph, root)
    assert [w.datatype for w in graph.scalar_witnesses_at(occurrence)] == [STRING]


@pytest.mark.parametrize(
    ("content", "lexical", "expected"),
    [
        (Text(), "words", [None]),
        (Text(), "", [None]),
        (Empty(), "", []),
        (List(Empty()), " \t ", [None]),
        (DataExcept(INTEGER, Value(INTEGER, "0")), "12", [INTEGER]),
        (Choice(DataExcept(INTEGER, Data(INTEGER)), Data(STRING)), "12", [STRING]),
        (Choice(Value(INTEGER, "0"), Data(STRING)), "12", [STRING]),
        (Choice(Data(INTEGER), Value(STRING, "bad")), "bad", [STRING]),
        (Group(Choice(Empty(), Data(INTEGER)), Data(STRING)), "12", [STRING]),
        (Group(Text(), Data(INTEGER)), "12", [INTEGER]),
        (Interleave(Data(INTEGER), Text()), "12", [INTEGER]),
        (OneOrMore(Data(INTEGER)), "12", [INTEGER]),
        (Choice(Group(Data(INTEGER), NotAllowed()), Data(STRING)), "12", [STRING]),
    ],
)
def test_scalar_rules_keep_only_complete_accepting_contracts(content, lexical, expected):
    root = tree("<r/>")
    root.set_text(lexical)
    graph = evidence(root, element("r", content))
    assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, root))] == expected


@pytest.mark.parametrize(
    "content",
    [
        Group(Attribute(Name(None, "a"), Data(INTEGER)), Attribute(Name(None, "b"), Data(STRING))),
        Interleave(
            Attribute(Name(None, "b"), Data(STRING)), Attribute(Name(None, "a"), Data(INTEGER))
        ),
        OneOrMore(
            Choice(
                Attribute(Name(None, "a"), Data(INTEGER)),
                Attribute(Name(None, "b"), Data(STRING)),
            )
        ),
    ],
)
def test_unordered_attributes_require_correlated_value_proofs(content):
    root = tree('<r b="word" a="12"/>')
    graph = evidence(root, element("r", content))
    witnesses = [
        (occurrence.name, tuple(w.datatype for w in graph.scalar_witnesses_at(occurrence)))
        for occurrence in graph.occurrences
        if occurrence.kind == "attribute"
    ]
    assert witnesses == [("a", (INTEGER,)), ("b", (STRING,))]


def test_weak_attribute_acceptance_does_not_fabricate_a_scalar_contract():
    root = tree('<r a=" \t "/>')
    graph = evidence(root, element("r", Attribute(Name(None, "a"), Empty())))
    assert graph.scalar_witnesses_at(site(graph, root, kind="attribute")) == ()


@pytest.mark.parametrize(
    "content",
    [
        Group(Empty(), element("x", Data(INTEGER))),
        OneOrMore(element("x", Data(INTEGER))),
        Interleave(Text(), element("x", Data(INTEGER))),
    ],
)
def test_open_operator_continuations(content):
    root = tree("<r><x>12</x></r>")
    graph = evidence(root, element("r", content))
    assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, root.children[0]))] == [
        INTEGER
    ]


def test_nullable_text_prefix_can_be_skipped_when_opening_a_child():
    root = tree("<r><x>12</x></r>")
    graph = evidence(root, element("r", Group(Text(), element("x", Data(INTEGER)))))
    assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, root.children[0]))] == [
        INTEGER
    ]


def test_recursive_element_references_keep_repeated_scalar_sites_separate():
    ref = Ref()
    ref.bind(element("r", Choice(Data(INTEGER), ref)))
    root = tree("<r><r><r>12</r></r></r>")
    graph = evidence(root, ref)
    leaf = root.children[0].children[0]
    assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, leaf))] == [INTEGER]


def test_each_scalar_uses_its_own_namespace_and_base_context():
    qname = XsdDatatypeFactory().create("QName", (), CX)
    root = tree('<r a="p:r"><x>p:x</x></r>')
    child = root.children[0]
    root.set_namespace_bindings({"p": "urn:root"})
    root.set_base_uri("https://root.example/")
    child.set_namespace_bindings({"p": "urn:child"})
    child.set_base_uri("https://child.example/")
    graph = evidence(
        root,
        element("r", Group(Attribute(Name(None, "a"), Data(qname)), element("x", Data(qname)))),
    )
    (attribute,) = graph.scalar_witnesses_at(site(graph, root, kind="attribute"))
    (scalar,) = graph.scalar_witnesses_at(site(graph, child))
    assert attribute.context == DatatypeContext("https://root.example/", {"p": "urn:root"})
    assert scalar.context == DatatypeContext("https://child.example/", {"p": "urn:child"})
    child.set_namespace_bindings({"p": "urn:changed"})
    assert scalar.context.namespaces["p"] == "urn:child"


def test_pruning_rejects_dead_conjunctions_and_ungrounded_cycles():
    from pyxsd.relaxng.evidence import EvidenceEdge, EvidenceNode, EvidenceOccurrence, _prune

    occurrence = EvidenceOccurrence(tree("<r/>"), "text")
    good, dead, conjunction, alternative, disconnected, cycle = [
        EvidenceNode(occurrence, index, OriginTable()) for index in range(6)
    ]
    edges = [
        EvidenceEdge("and", (good,), ()),
        EvidenceEdge("or", (dead,), ()),
        EvidenceEdge("and", (conjunction,), (good, dead)),
        EvidenceEdge("or", (alternative,), (conjunction, good, dead, cycle)),
        EvidenceEdge("and", (disconnected,), ()),
        EvidenceEdge("and", (cycle,), (cycle,)),
        # A second viable proof cannot cause repeated parent processing.
        EvidenceEdge("and", (good,), (disconnected,)),
        EvidenceEdge("and", (good,), (dead,)),
        EvidenceEdge("or", (good,), ()),
    ]
    nodes, retained = _prune(
        [good, dead, conjunction, alternative, disconnected, cycle],
        edges,
        (alternative, dead, conjunction, cycle),
    )
    assert set(nodes) == {good, alternative, disconnected}
    assert retained[1].successors == (good,)
    assert all(edge.predecessors[0] not in (dead, conjunction, cycle) for edge in retained)


def compatible(graph, witnesses):
    """Evaluate this small proof under whole-assignment constraints, not marginals."""
    from pyxsd.relaxng.evidence import EvidenceEdge, EvidenceNode

    selected = {w.occurrence: w for w in witnesses}
    outgoing: dict[EvidenceNode, list[EvidenceEdge]] = {}
    for edge in graph.retained_edges:
        outgoing.setdefault(edge.predecessors[0], []).append(edge)
    memo = {}

    def viable(node):
        if node not in memo:
            memo[node] = any(
                all(selected.get(w.occurrence, w) == w for w in edge.witnesses)
                and (
                    all(viable(child) for child in edge.successors)
                    if edge.kind == "and"
                    else any(viable(child) for child in edge.successors)
                )
                for edge in outgoing[node]
            )
        return memo[node]

    return any(viable(root) for root in graph.accepting_roots)


@pytest.mark.parametrize("in_list", [False, True])
def test_proof_preserves_correlations_not_just_marginal_witness_sets(in_list):
    content: Pattern
    if in_list:
        content = List(
            Choice(Group(Data(INTEGER), Data(STRING)), Group(Data(STRING), Data(INTEGER)))
        )
        root = tree("<r>12 12</r>")
    else:
        content = Choice(
            Group(element("x", Data(INTEGER)), element("y", Data(STRING))),
            Group(element("x", Data(STRING)), element("y", Data(INTEGER))),
        )
        root = tree("<r><x>12</x><y>12</y></r>")
    graph = evidence(root, element("r", content))
    occurrences = (
        [site(graph, root, kind="token", token_index=i) for i in range(2)]
        if in_list
        else [site(graph, child) for child in root.children]
    )
    left, right = [
        {w.datatype: w for w in graph.scalar_witnesses_at(occurrence)} for occurrence in occurrences
    ]
    assert set(left) == set(right) == {INTEGER, STRING}
    assert compatible(graph, [left[INTEGER], right[STRING]])
    assert compatible(graph, [left[STRING], right[INTEGER]])
    assert not compatible(graph, [left[INTEGER], right[INTEGER]])
    assert not compatible(graph, [left[STRING], right[STRING]])


def test_ambiguous_list_does_not_enumerate_exponentially_many_assignments():
    content: Pattern = Choice(Data(INTEGER), Data(STRING))
    for _ in range(25):
        content = Group(content, Choice(Data(INTEGER), Data(STRING)))
    graph = evidence(tree("<r>" + " ".join(["12"] * 26) + "</r>"), element("r", List(content)))
    assert len(graph.retained_nodes) < 10000  # 2**26 accepting token assignments.
    assert len([o for o in graph.occurrences if o.kind == "token"]) == 26


def test_nonaccepting_attribute_and_list_prefixes_do_not_supply_witnesses():
    root = tree('<r a="12">12</r>')
    attribute = Attribute(Name(None, "a"), Choice(Group(Data(INTEGER), Data(STRING)), Data(STRING)))
    content = Choice(List(Group(Data(INTEGER), Data(STRING))), List(Data(STRING)))
    graph = evidence(root, element("r", Group(attribute, content)))
    assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, root, kind="attribute"))] == [
        STRING
    ]
    assert [
        w.datatype
        for w in graph.scalar_witnesses_at(site(graph, root, kind="token", token_index=0))
    ] == [STRING]


def test_list_reference_and_repeated_tokens_share_no_occurrence_identity():
    ref = Ref()
    ref.bind(Data(INTEGER))
    root = tree("<r>12 12</r>")
    graph = evidence(root, element("r", List(OneOrMore(ref))))
    first = site(graph, root, kind="token", token_index=0)
    second = site(graph, root, kind="token", token_index=1)
    assert first is not second
    assert [w.datatype for w in graph.scalar_witnesses_at(first)] == [INTEGER]
    assert [w.datatype for w in graph.scalar_witnesses_at(second)] == [INTEGER]


def test_local_proof_engine_rejects_an_ungrounded_dependency_cycle():
    from pyxsd.relaxng.evidence import EvidenceOccurrence, _Builder

    ref = Ref()
    ref.bind(ref)
    with pytest.raises(RuntimeError, match="no event boundary"):
        _Builder(None, None).derive(
            ref, EvidenceOccurrence(tree("<r/>"), "text"), "text", "", "12", CX
        )


def test_missing_complete_proof_is_an_internal_error_not_an_empty_graph(monkeypatch):
    from pyxsd.relaxng.evidence import _Builder, build_binding_evidence

    root = tree("<r>12</r>")
    start = element("r", Data(INTEGER))
    assert validate_element(root, start).valid
    # Inject a lost transition into the proof engine, not the boolean verdict.
    monkeypatch.setattr(_Builder, "sequence", lambda *args: {})
    with pytest.raises(RuntimeError, match="complete accepting derivation"):
        build_binding_evidence(
            root,
            start,
            datatype_context=CX,
            limits=None,
        )


def test_evidence_limits_are_inclusive_and_independently_counted():
    from pyxsd.relaxng.evidence import build_binding_evidence
    from pyxsd.relaxng.limits import RelaxNGLimits

    root = tree("<r/>")
    start = element("r")
    graph = evidence(root, start)
    for budget, count in [
        ("max_evidence_states", len(graph.retained_nodes)),
        ("max_evidence_edges", len(graph.retained_edges)),
    ]:
        assert build_binding_evidence(
            root, start, datatype_context=CX, limits=RelaxNGLimits(**{budget: count})
        ).accepting_roots
        with pytest.raises(RelaxNGLimitError) as error:
            build_binding_evidence(
                root, start, datatype_context=CX, limits=RelaxNGLimits(**{budget: count - 1})
            )
        assert error.value.observed == count
    assert build_binding_evidence(
        tree("<r>12</r>"),
        element("r", Data(INTEGER)),
        datatype_context=CX,
        limits=RelaxNGLimits(max_scalar_witnesses=1, max_capture_entries=0),
    ).accepting_roots


def test_root_fallback_context_is_never_inherited_by_contextless_children():
    from pyxsd.relaxng.evidence import build_binding_evidence

    root = tree('<r a="root"><x>child</x></r>')
    fallback = DatatypeContext("https://fallback.example/", {"p": "urn:fallback"})
    start = element(
        "r", Group(Attribute(Name(None, "a"), Data(STRING)), element("x", Data(STRING)))
    )
    with pytest.raises(ValueError, match=r"fallback.*Phase 2"):
        build_binding_evidence(root, start, datatype_context=fallback, limits=None)
    root.set_namespace_bindings(fallback.namespaces)
    root.set_base_uri(fallback.base_uri)
    graph = build_binding_evidence(
        root,
        start,
        datatype_context=DatatypeContext("https://ignored.example/", {"p": "urn:ignored"}),
        limits=None,
    )
    (attribute,) = graph.scalar_witnesses_at(site(graph, root, kind="attribute"))
    (child,) = graph.scalar_witnesses_at(site(graph, root.children[0]))
    assert attribute.context == fallback
    assert child.context == CX


def test_pruned_local_witnesses_still_consume_mandatory_work_budget():
    from pyxsd.relaxng.evidence import build_binding_evidence
    from pyxsd.relaxng.limits import RelaxNGLimits

    root = tree("<r><x>12</x><b/></r>")
    start = element(
        "r",
        Choice(
            Group(element("x", Data(INTEGER)), element("a")),
            Group(element("x", Data(STRING)), element("b")),
        ),
    )
    assert validate_element(root, start).valid
    with pytest.raises(RelaxNGLimitError) as error:
        build_binding_evidence(
            root, start, datatype_context=CX, limits=RelaxNGLimits(max_scalar_witnesses=1)
        )
    assert error.value.budget == "max_scalar_witnesses"
    assert error.value.observed == 2


@pytest.mark.parametrize("count", [4, 6, 8])
def test_independent_child_choices_stay_factored_through_start_tag_close(count):
    from pyxsd.relaxng.evidence import build_binding_evidence
    from pyxsd.relaxng.limits import RelaxNGLimits

    factor = Choice(element("x", Data(INTEGER)), element("x", Data(STRING)))
    content: Pattern = factor
    for _ in range(count - 1):
        content = Group(content, factor)
    root = tree("<r>" + "<x>12</x>" * count + "</r>")
    graph = build_binding_evidence(
        root,
        element("r", content),
        datatype_context=CX,
        limits=RelaxNGLimits(max_evidence_states=100 * count, max_evidence_edges=100 * count),
    )
    assert len(graph.retained_nodes) < 100 * count
    for child in root.children:
        assert {witness.datatype for witness in graph.scalar_witnesses_at(site(graph, child))} == {
            INTEGER,
            STRING,
        }


def test_late_failure_in_one_factor_does_not_select_unrelated_child_choices():
    from pyxsd.relaxng.evidence import build_binding_evidence
    from pyxsd.relaxng.limits import RelaxNGLimits

    content: Pattern = Choice(
        Group(element("x", Data(INTEGER)), element("a")),
        Group(element("x", Data(STRING)), element("b")),
    )
    for _ in range(7):
        content = Group(content, Choice(element("x", Data(INTEGER)), element("x", Data(STRING))))
    root = tree("<r><x>12</x><b/>" + "<x>12</x>" * 7 + "</r>")
    graph = build_binding_evidence(
        root,
        element("r", content),
        datatype_context=CX,
        limits=RelaxNGLimits(max_evidence_states=1500, max_evidence_edges=1500),
    )
    assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, root.children[0]))] == [
        STRING
    ]
    for child in root.children[2:]:
        assert {witness.datatype for witness in graph.scalar_witnesses_at(site(graph, child))} == {
            INTEGER,
            STRING,
        }


def test_witness_free_close_does_not_select_one_branch_for_all_repetitions():
    start = element(
        "r",
        OneOrMore(Choice(element("x", Value(INTEGER, "12")), element("x", Value(STRING, "bad")))),
    )
    root = tree("<r><x>12</x><x>bad</x></r>")
    graph = evidence(root, start)
    for child, datatype in zip(root.children, (INTEGER, STRING), strict=True):
        assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, child))] == [datatype]


def test_qname_fallback_cannot_manufacture_a_witness_rejected_by_phase_two():
    from pyxsd.relaxng.evidence import build_binding_evidence

    qname = XsdDatatypeFactory().create("QName", (), CX)
    root = tree("<r>p:x</r>")
    start = element("r", Choice(Data(qname), Data(STRING)))
    graph = evidence(root, start)
    assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, root))] == [STRING]
    with pytest.raises(ValueError, match=r"fallback.*Phase 2"):
        build_binding_evidence(
            root,
            start,
            datatype_context=DatatypeContext(None, {"p": "urn:fallback"}),
            limits=None,
        )


@pytest.mark.parametrize(
    "fallback",
    [
        DatatypeContext("https://fallback.example/", {}),
        DatatypeContext(None, {"p": "urn:fallback"}),
    ],
)
def test_absent_root_context_rejects_each_nonempty_fallback_component(fallback):
    from pyxsd.relaxng.evidence import build_binding_evidence

    with pytest.raises(ValueError, match=r"fallback.*Phase 2"):
        build_binding_evidence(
            tree("<r>12</r>"),
            element("r", Data(STRING)),
            datatype_context=fallback,
            limits=None,
        )


def test_known_empty_root_namespace_frame_does_not_take_a_nonempty_fallback():
    from pyxsd.relaxng.evidence import build_binding_evidence

    qname = XsdDatatypeFactory().create("QName", (), CX)
    root = tree("<r>p:x</r>")
    root.set_namespace_bindings({})
    graph = build_binding_evidence(
        root,
        element("r", Choice(Data(qname), Data(STRING))),
        datatype_context=DatatypeContext(None, {"p": "urn:ignored"}),
        limits=None,
    )
    (witness,) = graph.scalar_witnesses_at(site(graph, root))
    assert witness.datatype == STRING
    assert witness.context == CX


def test_witness_free_close_filters_a_branch_with_an_unmet_attribute():
    root = tree("<r>12</r>")
    start = Choice(
        element("r", Group(Attribute(Name(None, "a"), Text()), Data(INTEGER))),
        element("r", Data(STRING)),
    )
    graph = evidence(root, start)
    assert [w.datatype for w in graph.scalar_witnesses_at(site(graph, root))] == [STRING]


def test_dead_list_continuation_cannot_leak_a_locally_successful_integer():
    root = tree("<r>12</r>")
    start = element("r", List(Choice(Group(Data(INTEGER), NotAllowed()), Data(STRING))))
    graph = evidence(root, start)
    assert [
        w.datatype
        for w in graph.scalar_witnesses_at(site(graph, root, kind="token", token_index=0))
    ] == [STRING]
