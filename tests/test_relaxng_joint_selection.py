"""Joint selections must participate in the same accepting derivation."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace

import pytest

import pyxsd.relaxng as relaxng
from pyxsd.relaxng.contracts import candidate_id, scalar_contract
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
)
from pyxsd.relaxng.evidence import (
    EvidenceEdge,
    EvidenceNode,
    EvidenceOccurrence,
    ScalarWitness,
    build_binding_evidence,
)
from pyxsd.relaxng.origins import OriginTable

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


def scalar_or_correlated_list():
    schema = relaxng.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="r" datatypeLibrary="{XSD}">'
            '<choice><data type="string"/><list><choice>'
            '<group><data type="integer"/><data type="integer"/></group>'
            '<group><data type="string"/><data type="string"/></group>'
            "</choice></list></choice></element>"
        )
    )
    assert not schema.report.has_errors
    document = schema.parse(io.StringIO("<r>12 34</r>"), typed=True)
    assert document.is_valid
    return schema, document


def selection_for(schema, graph, occurrence, type_name):
    selection = getattr(relaxng, "CandidateSelection", None)
    assert selection is not None, "CandidateSelection is not implemented"
    witness = next(
        witness
        for witness in graph.scalar_witnesses_at(occurrence)
        if scalar_contract(witness.pattern).type_name == type_name
    )
    return selection(
        occurrence,
        candidate_id(schema._grammar_fingerprint, scalar_contract(witness.pattern)),
    )


def test_atomic_selection_cannot_coexist_with_a_list_token():
    schema, document = scalar_or_correlated_list()
    graph = document.binding_evidence
    assert graph is not None
    scalar = next(site for site in graph.occurrences if site.kind == "text")
    token = next(site for site in graph.occurrences if site.kind == "token")
    value = document.root.scalar_value()
    assert isinstance(value, relaxng.AlternativeValue)
    assert {candidate.contract.kind for candidate in value.candidates} == {"atomic", "list"}

    atomic = selection_for(schema, graph, scalar, "string")
    integer_token = selection_for(schema, graph, token, "integer")
    assert graph.compatible((atomic,))
    assert graph.compatible((integer_token,))
    assert not graph.compatible((atomic, integer_token))


def test_forbidden_token_pair_cannot_escape_through_an_atomic_branch():
    schema, document = scalar_or_correlated_list()
    graph = document.binding_evidence
    assert graph is not None
    tokens = sorted(
        (site for site in graph.occurrences if site.kind == "token"),
        key=lambda site: site.token_index,
    )
    first = selection_for(schema, graph, tokens[0], "integer")
    second = selection_for(schema, graph, tokens[1], "string")
    assert graph.compatible((first,))
    assert graph.compatible((second,))
    assert not graph.compatible((first, second))
    whole = selection_for(
        schema, graph, next(site for site in graph.occurrences if site.kind == "text"), "list"
    )
    assert graph.compatible((whole,))
    assert graph.compatible((whole, first))
    assert not graph.compatible((whole, first, second))


def parse(content, xml="<r>12</r>", **kwargs):
    schema = relaxng.compile(
        io.StringIO(f'<element xmlns="{NS}" name="r" datatypeLibrary="{XSD}">{content}</element>'),
        **kwargs,
    )
    assert not schema.report.has_errors, schema.report.issues
    document = schema.parse(io.StringIO(xml), typed=True)
    assert document.is_valid
    assert document.binding_evidence is not None
    return schema, document, document.binding_evidence


def data(name):
    return f'<data type="{name}"/>'


def element(name, content):
    return f'<element name="{name}">{content}</element>'


def scalar_site(graph, node):
    return next(site for site in graph.occurrences if site.element is node and site.kind == "text")


def test_minimal_token_requires_list_and_whole_list_alone_leaves_tokens_free():
    schema, document, graph = parse(
        f"<choice>{data('string')}<list>{data('integer')}</list></choice>"
    )
    parent = scalar_site(graph, document.root)
    token = next(site for site in graph.occurrences if site.kind == "token")
    atomic = selection_for(schema, graph, parent, "string")
    whole = selection_for(schema, graph, parent, "list")
    item = selection_for(schema, graph, token, "integer")
    assert graph.compatible((whole,))
    assert graph.compatible((whole, item))
    assert graph.compatible((item,))
    assert not graph.compatible((atomic, item))


@pytest.mark.parametrize("compositor", ["group", "interleave"])
def test_scalar_fields_are_correlated_across_late_branch(compositor):
    def branch(name):
        return f"<{compositor}>{element('x', data(name))}{element('y', data(name))}</{compositor}>"

    schema, document, graph = parse(
        f"<choice>{branch('integer')}{branch('string')}</choice>",
        "<r><x>12</x><y>34</y></r>",
    )
    x, y = document.root.children
    assert len(x.scalar_value().candidates) == len(y.scalar_value().candidates) == 2
    xi = selection_for(schema, graph, scalar_site(graph, x), "integer")
    xs = selection_for(schema, graph, scalar_site(graph, x), "string")
    yi = selection_for(schema, graph, scalar_site(graph, y), "integer")
    ys = selection_for(schema, graph, scalar_site(graph, y), "string")
    assert graph.compatible((xi,)) and graph.compatible((ys,))
    assert graph.compatible((xi, yi)) and graph.compatible((xs, ys))
    assert not graph.compatible((xi, ys))
    assert not graph.compatible((xs, yi))


def test_independent_fields_allow_mixed_selections():
    choice = f"<choice>{data('integer')}{data('string')}</choice>"
    schema, document, graph = parse(
        element("x", choice) + element("y", choice),
        "<r><x>12</x><y>34</y></r>",
    )
    x, y = document.root.children
    assert graph.compatible(
        selection_for(schema, graph, scalar_site(graph, node), name)
        for node, name in ((x, "integer"), (y, "string"))
    )


def test_list_scalar_correlations_and_two_parent_scopes():
    def branch(name):
        return (
            "<group>"
            + element("x", f"<list>{data(name)}</list>")
            + element("y", data(name))
            + "</group>"
        )

    schema, document, graph = parse(
        f"<choice>{branch('integer')}{branch('string')}</choice>"
        + element("z", f"<choice>{data('string')}<list>{data('integer')}</list></choice>"),
        "<r><x>12</x><y>34</y><z>56</z></r>",
    )
    x, y, z = document.root.children
    before = repr(graph)
    xsite, ysite, zsite = (scalar_site(graph, node) for node in (x, y, z))
    xtoken = next(site for site in graph.occurrences if site.parent is xsite)
    ztoken = next(site for site in graph.occurrences if site.parent is zsite)
    xi = selection_for(schema, graph, xtoken, "integer")
    zs = selection_for(schema, graph, zsite, "string")
    zi = selection_for(schema, graph, ztoken, "integer")
    assert xi.candidate_id == zi.candidate_id
    yi = selection_for(schema, graph, ysite, "integer")
    ys = selection_for(schema, graph, ysite, "string")
    assert graph.compatible((xi, yi, zs))
    assert graph.compatible((xi, yi, zi))
    assert not graph.compatible((xi, ys))
    assert not graph.compatible((xi, yi, zs, zi))
    xvalue = x.scalar_value()
    integer_list = next(
        interpretation
        for interpretation in xvalue.interpretations
        if interpretation.token_candidates[0][0].contract.type_name == "integer"
    )
    whole = relaxng.CandidateSelection(xsite, integer_list.candidate_id)
    assert graph.compatible((whole,))
    assert graph.compatible((whole, xi, yi))
    assert not graph.compatible((whole, ys))
    assert not graph.compatible((whole, selection_for(schema, graph, xtoken, "string")))
    assert repr(graph) == before


def test_repeated_same_name_occurrences_do_not_collapse_equal_ids():
    def branch(first, second):
        return f"<group>{element('x', data(first))}{element('x', data(second))}</group>"

    schema, document, graph = parse(
        f"<choice>{branch('integer', 'string')}{branch('string', 'integer')}</choice>",
        "<r><x>12</x><x>12</x></r>",
    )
    first, second = (scalar_site(graph, child) for child in document.root.children)
    a = selection_for(schema, graph, first, "integer")
    b = selection_for(schema, graph, second, "integer")
    assert a.candidate_id == b.candidate_id
    assert a != b
    assert graph.compatible((a,))
    assert graph.compatible((b,))
    assert not graph.compatible((a, b))


def test_static_but_pruned_candidate_is_not_selectable():
    schema, document, graph = parse(
        f"<choice><group>{element('x', data('integer'))}{element('a', '<empty/>')}</group>"
        f"<group>{element('x', data('string'))}{element('b', '<empty/>')}</group></choice>",
        "<r><x>12</x><b/></r>",
    )
    site = scalar_site(graph, document.root.children[0])
    integer = next(c for c in graph.schema_contracts_at(site) if c.type_name == "integer")
    choice = relaxng.CandidateSelection(site, candidate_id(schema._grammar_fingerprint, integer))
    assert not graph.compatible((choice,))
    # A locally viable but disconnected fact still cannot satisfy a query.
    # Reuse the compiled, later-pruned pattern at this same lexical occurrence.
    pattern = next(
        p for p in graph._schema_patterns[site] if scalar_contract(p).type_name == "integer"
    )
    witness = ScalarWitness(site, pattern, "12", DatatypeContext(None, {}))
    node = EvidenceNode(site, -1, OriginTable())
    detached = replace(
        graph,
        retained_nodes=(*graph.retained_nodes, node),
        retained_edges=(*graph.retained_edges, EvidenceEdge("and", (node,), (), (witness,))),
    )
    assert detached.scalar_witnesses_at(site) != graph.scalar_witnesses_at(site)
    assert not detached.compatible((choice,))


def test_duplicate_unknown_empty_queries_and_immutable_selections():
    schema, document, graph = parse(f"<choice>{data('integer')}{data('string')}</choice>")
    site = scalar_site(graph, document.root)
    a = selection_for(schema, graph, site, "integer")
    b = selection_for(schema, graph, site, "string")
    duplicate = relaxng.CandidateSelection(site, a.candidate_id)
    assert a == duplicate and hash(a) == hash(duplicate)
    assert len({a, duplicate}) == 1
    with pytest.raises(FrozenInstanceError):
        a.candidate_id = "changed"
    assert graph.compatible(())
    assert not replace(graph, accepting_roots=()).compatible(())
    assert not replace(graph, retained_edges=()).compatible(())
    assert graph.compatible(iter((a, duplicate)))
    assert not graph.compatible((a, b))
    assert not graph.compatible((relaxng.CandidateSelection(site, "unknown"),))
    assert not graph.compatible(
        (relaxng.CandidateSelection(EvidenceOccurrence(document.root, "text"), a.candidate_id),)
    )


@pytest.mark.parametrize("occurrence", [None, "text", 1])
def test_selection_rejects_invalid_occurrence_types(occurrence):
    with pytest.raises(TypeError, match="occurrence"):
        relaxng.CandidateSelection(occurrence, "candidate")


@pytest.mark.parametrize("identifier", [None, 1, False])
def test_selection_rejects_invalid_identifier_types(identifier):
    _, document, graph = parse(data("integer"))
    with pytest.raises(TypeError, match="candidate"):
        relaxng.CandidateSelection(scalar_site(graph, document.root), identifier)


def test_selection_rejects_empty_identifier():
    _, document, graph = parse(data("integer"))
    with pytest.raises(ValueError, match="nonempty"):
        relaxng.CandidateSelection(scalar_site(graph, document.root), "")


@pytest.mark.parametrize("selections", [None, 1, ["candidate"], [None], {"id": "candidate"}])
def test_query_rejects_invalid_selection_types(selections):
    _, _, graph = parse(data("integer"))
    with pytest.raises(TypeError):
        graph.compatible(selections)


def test_invalid_selection_after_conflict_still_raises():
    schema, document, graph = parse(f"<choice>{data('integer')}{data('string')}</choice>")
    site = scalar_site(graph, document.root)
    a = selection_for(schema, graph, site, "integer")
    b = selection_for(schema, graph, site, "string")
    with pytest.raises(TypeError):
        graph.compatible((a, b, object()))


def test_graph_without_fingerprint_preserves_marginal_views():
    schema, document, graph = parse(data("integer"))
    anonymous = build_binding_evidence(
        document.root,
        schema.grammar.start,
        datatype_context=DatatypeContext(None, {}),
        limits=None,
    )
    site = scalar_site(anonymous, document.root)
    assert anonymous.scalar_witnesses_at(site)
    assert anonymous.schema_contracts_at(site)
    with pytest.raises(ValueError, match="fingerprint"):
        anonymous.compatible(())
    assert replace(graph, _grammar_fingerprint=None) == graph
    assert hash(replace(graph, _grammar_fingerprint=None)) == hash(graph)
    assert schema._grammar_fingerprint not in repr(graph)


@pytest.mark.parametrize("fingerprint, error", [("", ValueError), (1, TypeError)])
def test_invalid_graph_fingerprint(fingerprint, error):
    _, _, graph = parse(data("integer"))
    with pytest.raises(error, match="fingerprint"):
        replace(graph, _grammar_fingerprint=fingerprint)


@pytest.mark.parametrize("parent_kind", ["missing", "structural"])
def test_token_without_whole_list_parent_is_incompatible(parent_kind):
    schema, _, graph = parse(f"<list>{data('integer')}</list>")
    original = next(site for site in graph.occurrences if site.kind == "token")
    parent = None if parent_kind == "missing" else graph.occurrences[0]
    token = replace(original, parent=parent)
    # Malformed internal graph boundary: derive every witness and edge from a
    # real typed parse, replacing only the token's enclosing-parent identity.
    edges = tuple(
        replace(
            edge,
            witnesses=tuple(
                replace(w, occurrence=token) if w.occurrence is original else w
                for w in edge.witnesses
            ),
        )
        for edge in graph.retained_edges
    )
    malformed = replace(
        graph,
        retained_edges=edges,
        occurrences=tuple(token if site is original else site for site in graph.occurrences),
    )
    assert not malformed.compatible((selection_for(schema, malformed, token, "integer"),))


class ThrowingDatatype(Datatype):
    library_uri = "urn:throwing"
    type_name = "opaque"
    parameters = ()
    supports_decode = True
    calls = 0

    def allows(self, lexical, context):
        return True

    def equal(self, left, left_context, right, right_context):
        return left == right

    def decode(self, lexical, context):
        type(self).calls += 1
        raise RuntimeError("decoder must not run during compatibility")


class ThrowingFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        return ThrowingDatatype()


def test_declared_throwing_decoder_is_never_called():
    ThrowingDatatype.calls = 0
    custom = '<data type="opaque" datatypeLibrary="urn:throwing"/>'
    schema, document, graph = parse(
        element("x", custom) + element("y", f"<list>{custom}</list>"),
        "<r><x>12</x><y>34</y></r>",
        datatype_libraries={
            "urn:throwing": DatatypeLibraryRegistration(ThrowingFactory(), "throwing/1"),
        },
    )
    sites = [
        site
        for site in graph.occurrences
        if graph.scalar_witnesses_at(site) and site.kind in ("text", "token")
    ]
    choices = [
        selection_for(schema, graph, site, "opaque") for site in sites if site.kind == "token"
    ]
    choices.append(
        selection_for(schema, graph, scalar_site(graph, document.root.children[0]), "opaque")
    )
    assert graph.compatible(choices)
    assert ThrowingDatatype.calls == 0


def test_many_independent_fields_are_packed_and_queries_leave_graph_unchanged():
    count = 32
    choice = f"<choice>{data('integer')}{data('string')}</choice>"
    content = "".join(element(f"f{i}", choice) for i in range(count))
    xml = "<r>" + "".join(f"<f{i}>12</f{i}>" for i in range(count)) + "</r>"
    schema, document, graph = parse(content, xml)
    before = (graph.accepting_roots, graph.retained_nodes, graph.retained_edges, graph.occurrences)
    selections = [
        selection_for(schema, graph, scalar_site(graph, node), "integer" if i % 2 else "string")
        for i, node in enumerate(document.root.children)
    ]
    assert len(graph.retained_nodes) < 500 * count
    for _ in range(5):
        assert graph.compatible(iter(selections))
        assert not graph.compatible((*selections, replace(selections[0], candidate_id="unknown")))
        assert graph.compatible(())
    assert before == (
        graph.accepting_roots,
        graph.retained_nodes,
        graph.retained_edges,
        graph.occurrences,
    )
    other_schema, other_document, other_graph = parse(content, xml)
    rebuilt = [
        selection_for(
            other_schema,
            other_graph,
            scalar_site(other_graph, node),
            "integer" if i % 2 else "string",
        )
        for i, node in enumerate(other_document.root.children)
    ]
    assert [s.candidate_id for s in rebuilt] == [s.candidate_id for s in selections]
    assert other_graph.compatible(rebuilt)


def test_many_token_assignments_stay_packed_and_capture_does_not_change_query():
    count = 32
    schema, document, graph = parse(
        f"<list><oneOrMore><choice>{data('integer')}{data('string')}</choice></oneOrMore></list>",
        "<r>" + " ".join(["12"] * count) + "</r>",
    )
    tokens = sorted(
        (site for site in graph.occurrences if site.kind == "token"),
        key=lambda site: site.token_index,
    )
    selections = [
        selection_for(schema, graph, site, "integer" if i % 2 else "string")
        for i, site in enumerate(tokens)
    ]
    assert len(tokens) == count
    assert len(graph.retained_nodes) < 100 * count
    assert graph.compatible(selections)
    captured = schema.parse(io.StringIO(document.to_string()), typed=True, capture="patterns")
    other = captured.binding_evidence
    other_tokens = sorted(
        (site for site in other.occurrences if site.kind == "token"),
        key=lambda site: site.token_index,
    )
    rebuilt = [
        selection_for(schema, other, site, "integer" if i % 2 else "string")
        for i, site in enumerate(other_tokens)
    ]
    assert [item.candidate_id for item in rebuilt] == [item.candidate_id for item in selections]
    assert other.compatible(rebuilt)


def test_candidate_ids_and_joint_results_are_stable_across_hash_seeds():
    script = """
import json, runpy
h = runpy.run_path('tests/test_relaxng_joint_selection.py')
s, d = h['scalar_or_correlated_list']()
g = d.binding_evidence
tokens = sorted((o for o in g.occurrences if o.kind == 'token'), key=lambda o: o.token_index)
a = h['selection_for'](s, g, tokens[0], 'integer')
b = h['selection_for'](s, g, tokens[1], 'string')
print(json.dumps(([a.candidate_id, b.candidate_id], [g.compatible([a]), g.compatible([a,b])])))
"""
    outputs = [
        json.loads(
            subprocess.check_output(
                [sys.executable, "-c", script],
                env={**os.environ, "PYTHONHASHSEED": seed},
                text=True,
            )
        )
        for seed in ("1", "37")
    ]
    assert outputs[0] == outputs[1]
    assert outputs[0][1] == [True, False]


@pytest.mark.parametrize(
    "xcontent, xcandidate",
    [
        ('<data type="string"/>', "string"),
        ('<value type="string"></value>', "string"),
        ("<list><empty/></list>", "list"),
    ],
    ids=["empty-string", "empty-value", "empty-list"],
)
@pytest.mark.parametrize("optional", [False, True], ids=["required-x", "optional-x"])
def test_selected_empty_scalar_cannot_escape_through_weak_text_skip(xcontent, xcandidate, optional):
    absent_scalar = element("x", "<empty/>")
    if optional:
        absent_scalar = f"<optional>{absent_scalar}</optional>"
    schema, document, graph = parse(
        f"<choice><group>{element('x', xcontent)}{element('y', data('integer'))}</group>"
        f"<group>{absent_scalar}{element('y', data('string'))}</group></choice>",
        "<r><x/><y>12</y></r>",
    )
    x, y = document.root.children
    selected_x = selection_for(schema, graph, scalar_site(graph, x), xcandidate)
    selected_y = selection_for(schema, graph, scalar_site(graph, y), "string")
    assert graph.compatible((selected_x,))
    assert graph.compatible((selected_y,))
    assert not graph.compatible((selected_x, selected_y))
    assert graph.compatible(
        (selected_x, selection_for(schema, graph, scalar_site(graph, y), "integer"))
    )
    assert graph.compatible(())
    # The no-witness branch is required when x has no scalar selection.
    assert graph.compatible((selected_y,))
    if optional:
        omitted = schema.parse(io.StringIO("<r><y>12</y></r>"), typed=True)
        assert omitted.is_valid
        assert not omitted.binding_evidence.compatible((selected_x,))


@pytest.mark.parametrize(
    "xcontent, xcandidate",
    [
        ('<data type="string"/>', "string"),
        ("<list><empty/></list>", "list"),
    ],
    ids=["empty-atomic", "empty-list"],
)
def test_selected_empty_attribute_cannot_escape_through_nullable_shortcut(xcontent, xcandidate):
    schema, document, graph = parse(
        f'<choice><group><attribute name="x">{xcontent}</attribute>'
        f"{element('y', data('integer'))}</group>"
        '<group><optional><attribute name="x"><empty/></attribute></optional>'
        f"{element('y', data('string'))}</group></choice>",
        '<r x=""><y>12</y></r>',
    )
    xsite = next(site for site in graph.occurrences if site.kind == "attribute")
    selected_x = selection_for(schema, graph, xsite, xcandidate)
    selected_y = selection_for(
        schema, graph, scalar_site(graph, document.root.children[0]), "string"
    )
    assert graph.compatible((selected_x,))
    assert graph.compatible((selected_y,))
    assert not graph.compatible((selected_x, selected_y))
    assert graph.compatible(())
    # An occurrence from another parse cannot be selected when that optional
    # attribute is absent, even if its contract is schema-possible.
    omitted = schema.parse(io.StringIO("<r><y>12</y></r>"), typed=True)
    assert omitted.is_valid
    assert not omitted.binding_evidence.compatible((selected_x,))


@pytest.mark.parametrize(
    "xcontent, xcandidate",
    [
        ('<data type="string"/>', "string"),
        ("<list><empty/></list>", "list"),
    ],
)
def test_empty_scalar_participation_composes_with_token_parent_requirement(xcontent, xcandidate):
    schema, document, graph = parse(
        f"<choice><group>{element('x', xcontent)}"
        f"{element('y', '<list>' + data('integer') + '</list>')}</group>"
        f"<group>{element('x', '<empty/>')}"
        f"{element('y', '<list>' + data('string') + '</list>')}</group></choice>",
        "<r><x/><y>12</y></r>",
    )
    selected_x = selection_for(
        schema, graph, scalar_site(graph, document.root.children[0]), xcandidate
    )
    token = next(site for site in graph.occurrences if site.kind == "token")
    integer_token = selection_for(schema, graph, token, "integer")
    string_token = selection_for(schema, graph, token, "string")
    assert graph.compatible((selected_x,))
    assert graph.compatible((integer_token,))
    assert graph.compatible((string_token,))
    assert graph.compatible((selected_x, integer_token))
    assert not graph.compatible((selected_x, string_token))


def test_unselected_nullable_attribute_alternative_remains_a_valid_local_fact():
    schema, _, graph = parse(
        '<attribute name="x"><choice><empty/><data type="string"/></choice></attribute>',
        '<r x=""/>',
    )
    site = next(site for site in graph.occurrences if site.kind == "attribute")
    selected = selection_for(schema, graph, site, "string")
    assert graph.compatible(())
    assert graph.compatible((selected,))
    witnessless = replace(
        graph, retained_edges=tuple(edge for edge in graph.retained_edges if not edge.witnesses)
    )
    assert not witnessless.scalar_witnesses_at(site)
    assert witnessless.compatible(())
    assert not witnessless.compatible((selected,))
