"""Atomic typed assignment and joint compatibility for RELAX NG projections.

Every assignment prepares its lexical text and datatype context before it
touches the authoritative tree.  A failure therefore leaves names, attributes,
children, and revision freshness byte-for-byte unchanged.  Alternative
snapshots contribute lexical text and context only, never old validation
authority; a successful edit marks the whole revision stale until revalidation.
"""

from __future__ import annotations

import io
from dataclasses import replace
from xml.etree import ElementTree as ET

import pytest

import pyxsd.relaxng as relaxng
from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng import AlternativeValue, StaleInterpretationError
from pyxsd.relaxng.binding import bind_projection
from pyxsd.relaxng.contracts import candidate_id, scalar_contract
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    UnsupportedDatatypeOperation,
    XsdDatatypeFactory,
)
from pyxsd.relaxng.evidence import CandidateSelection, build_binding_evidence
from pyxsd.relaxng.patterns import (
    Choice,
    Data,
    Empty,
    List,
    NotAllowed,
    OneOrMore,
    Ref,
)
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.values import (
    _combine_list_tokens,
    _list_token_plan,
    encode_scalar,
    list_shape,
)

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
OPAQUE = "urn:pyxsd:test:opaque"


def data(type_name: str, library: str = XSD) -> str:
    return f'<data type="{type_name}" datatypeLibrary="{library}"/>'


def element(name: str, content: str = "<empty/>") -> str:
    return f'<element name="{name}">{content}</element>'


def compile_body(body: str, **kwargs: object) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="doc">{body}</element>'), **kwargs
    )
    schema.require_valid()
    return schema


def typed(schema: RelaxNGSchema, xml: str, **kwargs: object):
    document = schema.parse(io.StringIO(xml), typed=True, **kwargs)  # type: ignore[arg-type]
    assert document.is_valid
    assert document.binding_evidence is not None
    return document


def serialized(document) -> str:
    return document.to_string()


def root_route(schema: RelaxNGSchema):
    return schema.projection.routes[schema.projection.roots[0]]


def scalar_site(graph, node):
    return next(site for site in graph.occurrences if site.element is node and site.kind == "text")


def selection_for(schema, graph, node, type_name: str) -> CandidateSelection:
    return selection_at(schema, graph, scalar_site(graph, node), type_name)


def selection_at(schema, graph, occurrence, type_name: str) -> CandidateSelection:
    witness = next(
        witness
        for witness in graph.scalar_witnesses_at(occurrence)
        if scalar_contract(witness.pattern).type_name == type_name
    )
    return CandidateSelection(
        occurrence,
        candidate_id(schema._grammar_fingerprint, scalar_contract(witness.pattern)),
    )


def attribute_site(graph, node, expanded_name: str):
    return next(
        site
        for site in graph.occurrences
        if site.element is node and site.kind == "attribute" and site.name == expanded_name
    )


def root_list_pattern(schema, document) -> List:
    graph = document.binding_evidence
    assert graph is not None
    site = scalar_site(graph, document.root)
    return next(pattern for pattern in graph._schema_patterns[site] if isinstance(pattern, List))


class OpaqueDatatype(Datatype):
    """A validation-only datatype with no encoder capability."""

    library_uri = OPAQUE
    type_name = "opaque"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right


class OpaqueFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return OpaqueDatatype()


class NonStringEncoderDatatype(Datatype):
    library_uri = OPAQUE
    type_name = "nonstring"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del value, context
        return 1  # type: ignore[return-value]


def test_unique_text_descriptor_assignment_encodes_before_serialization():
    schema = compile_body('<attribute name="a"><text/></attribute><text/>')
    document = typed(schema, '<doc a="old">text</doc>')
    root = document.root

    root.assign(a="new", typed_value="replacement")

    assert serialized(document) == '<doc a="new">replacement</doc>'
    assert document.interpretations_current is False
    assert root.revision.dirty is True
    with pytest.raises(StaleInterpretationError):
        _ = root.typed_value
    with pytest.raises(StaleInterpretationError):
        _ = root.a


def test_unique_atomic_scalar_and_attribute_assignment_encodes_values():
    schema = compile_body(
        '<attribute name="n">' + data("integer") + "</attribute>" + data("integer")
    )
    document = typed(schema, '<doc n="1">2</doc>')
    root = document.root

    root.assign(n=5, typed_value=7)

    assert serialized(document) == '<doc n="5">7</doc>'
    assert root.revision.dirty is True


def test_optional_attribute_and_absent_scalar_can_be_created_by_assignment():
    schema = compile_body(
        '<optional><attribute name="n">'
        + data("integer")
        + "</attribute></optional><optional>"
        + data("integer")
        + "</optional>"
    )
    document = typed(schema, "<doc/>")
    root = document.root

    assert root.n is None
    assert root.typed_value is None
    root.assign(n=4, typed_value=9)

    assert serialized(document) == '<doc n="4">9</doc>'


def test_text_assignment_requires_a_string_and_never_stringifies_objects():
    schema = compile_body("<text/>")
    document = typed(schema, "<doc>old</doc>")
    root = document.root
    before = serialized(document)

    for value in (1, True, object(), b"bytes"):
        with pytest.raises(TypeError):
            root.typed_value = value
        assert serialized(document) == before
        assert root.revision.dirty is False

    root.typed_value = "new"
    assert serialized(document) == "<doc>new</doc>"


@pytest.mark.parametrize("type_name", ["integer", "decimal", "float", "double"])
def test_bool_is_rejected_as_a_numeric_datatype_value(type_name):
    schema = compile_body(f'<attribute name="n">{data(type_name)}</attribute><empty/>')
    document = typed(schema, '<doc n="1"/>')
    root = document.root
    before = serialized(document)

    with pytest.raises(TypeError, match="expected"):
        root.n = True
    assert serialized(document) == before
    assert document.interpretations_current is True

    contract = root_route(schema).attributes["n"].scalar_contracts[0]
    with pytest.raises(TypeError, match="expected"):
        encode_scalar(contract, True, DatatypeContext(None, {}))


def test_encode_scalar_uses_explicit_contracts_without_str_fallback():
    schema = compile_body("<text/>")
    typed(schema, "<doc>value</doc>")
    contract = root_route(schema).content.scalar_contracts[0]
    context = DatatypeContext(None, {})

    assert contract.kind == "text"
    assert encode_scalar(contract, "value", context) == "value"
    for value in (1, True, object()):
        with pytest.raises(TypeError):
            encode_scalar(contract, value, context)


def test_missing_atomic_encoder_is_a_specific_capability_error_and_atomic():
    schema = compile_body(
        f'<attribute name="value">{data("opaque", OPAQUE)}</attribute><empty/>',
        datatype_libraries={
            OPAQUE: DatatypeLibraryRegistration(OpaqueFactory(), "opaque/1"),
        },
    )
    document = typed(schema, '<doc value="original"/>')
    root = document.root
    before = serialized(document)

    with pytest.raises(UnsupportedDatatypeOperation, match="encoder"):
        root.value = "replacement"

    assert serialized(document) == before
    assert root.revision.dirty is False
    contract = root_route(schema).attributes["value"].scalar_contracts[0]
    with pytest.raises(UnsupportedDatatypeOperation, match="encoder"):
        encode_scalar(contract, "replacement", DatatypeContext(None, {}))


def test_ambiguous_bare_values_are_rejected_but_snapshots_are_lexical_edits():
    schema = compile_body(f"<choice>{data('integer')}{data('string')}</choice>")
    document = typed(schema, "<doc>12</doc>")
    root = document.root
    snapshot = root.typed_value
    assert isinstance(snapshot, AlternativeValue)
    before = serialized(document)

    for value in (12, "12", 12.0, object()):
        with pytest.raises(relaxng.AmbiguousValueError):
            root.typed_value = value
        assert serialized(document) == before
        assert document.interpretations_current is True

    root.assign(typed_value=snapshot)

    assert serialized(document) == before
    assert document.interpretations_current is False
    assert snapshot.as_type("xsd:integer") == 12
    with pytest.raises(StaleInterpretationError):
        _ = root.typed_value


def test_alternative_snapshot_grammar_and_context_must_match_the_document():
    schema = compile_body(
        '<attribute name="q"><choice>'
        + data("QName")
        + data("string")
        + "</choice></attribute><empty/>"
    )
    document = typed(schema, '<doc xmlns:p="urn:p" q="p:value"/>')
    root = document.root
    snapshot = root.q
    assert isinstance(snapshot, AlternativeValue)
    before = serialized(document)

    wrong_grammar = replace(snapshot, grammar_fingerprint="urn:other-grammar")
    wrong_namespace = replace(snapshot, context=DatatypeContext(None, {"p": "urn:different"}))
    wrong_base = replace(
        snapshot,
        context=DatatypeContext("https://example.test/base", dict(snapshot.context.namespaces)),
    )
    foreign_revision = replace(snapshot, revision=snapshot.revision + 100)

    for candidate in (wrong_grammar, wrong_namespace, wrong_base):
        with pytest.raises(ValueError, match=r"grammar|context|namespace|base"):
            root.q = candidate
        assert serialized(document) == before
        assert document.interpretations_current is True

    # A prior revision is an allowed lexical source: its text and context are
    # reusable, but its candidates never become fresh validation evidence.
    root.q = foreign_revision
    assert serialized(document) == before
    assert document.interpretations_current is False
    with pytest.raises(StaleInterpretationError):
        _ = root.q


def test_unique_homogeneous_list_assignment_uses_schema_item_contracts():
    schema = compile_body("<list><oneOrMore>" + data("integer") + "</oneOrMore></list>")
    document = typed(schema, "<doc>1 2</doc>")
    root = document.root
    assert root.typed_value == [1, 2]

    root.typed_value = [3, 4]

    assert serialized(document) == "<doc>3 4</doc>"
    with pytest.raises(StaleInterpretationError):
        _ = root.typed_value


def test_unique_fixed_tuple_assignment_requires_its_declared_shape():
    schema = compile_body("<list><group>" + data("integer") + data("string") + "</group></list>")
    document = typed(schema, "<doc>1 one</doc>")
    root = document.root
    assert root.typed_value == (1, "one")

    for value in ([2, "two"], ("two", 2), (2,), "2 two", object()):
        with pytest.raises((TypeError, ValueError)):
            root.typed_value = value
        assert serialized(document) == "<doc>1 one</doc>"
        assert document.interpretations_current is True

    root.typed_value = (2, "two")
    assert serialized(document) == "<doc>2 two</doc>"
    assert document.interpretations_current is False


def test_repeated_equal_item_patterns_share_one_compiled_plan_entry():
    schema = compile_body("<list><group>" + data("integer") + data("integer") + "</group></list>")
    document = typed(schema, "<doc>1 2</doc>")

    document.root.typed_value = (3, 4)

    assert serialized(document) == "<doc>3 4</doc>"


def test_homogeneous_list_shape_rejects_tuple_and_arbitrary_objects():
    schema = compile_body("<list><oneOrMore>" + data("integer") + "</oneOrMore></list>")
    document = typed(schema, "<doc>1 2</doc>")
    root = document.root
    before = serialized(document)

    for value in ((3, 4), "3 4", b"3 4", object()):
        with pytest.raises((TypeError, ValueError)):
            root.typed_value = value
        assert serialized(document) == before
        assert document.interpretations_current is True

    root.typed_value = [3, 4, 5]
    assert serialized(document) == "<doc>3 4 5</doc>"


def test_ambiguous_list_item_contracts_report_the_structural_gap():
    schema = compile_body(
        "<list><oneOrMore><choice>"
        + data("integer")
        + data("string")
        + "</choice></oneOrMore></list>"
    )
    document = typed(schema, "<doc>1 2</doc>")
    root = document.root
    assert isinstance(root.typed_value, AlternativeValue)
    before = serialized(document)

    with pytest.raises(UnsupportedDatatypeOperation, match=r"token|structural"):
        root.typed_value = [1, 2]

    assert serialized(document) == before
    assert document.interpretations_current is True
    contract = root_route(schema).content.scalar_contracts[0]
    with pytest.raises(UnsupportedDatatypeOperation, match=r"token|structural"):
        encode_scalar(contract, [1, 2], DatatypeContext(None, {}))


def test_all_encodings_are_prepared_before_any_mutation():
    schema = compile_body(
        '<attribute name="n">' + data("integer") + "</attribute>" + data("integer")
    )
    document = typed(schema, '<doc n="1">2</doc>')
    root = document.root
    before = serialized(document)

    with pytest.raises(TypeError, match="expected"):
        root.assign(n=5, typed_value=True)

    assert serialized(document) == before
    assert document.interpretations_current is True
    assert root.n == 1
    assert root.typed_value == 2

    with pytest.raises(TypeError, match="expected"):
        root.assign(n=5, typed_value=object())

    assert serialized(document) == before
    assert root.revision.dirty is False


def test_unknown_and_child_structural_assignments_are_explicit():
    schema = compile_body(
        element("item", data("integer")) + '<attribute name="a"><text/></attribute>'
    )
    document = typed(schema, '<doc a="x"><item>1</item></doc>')
    root = document.root

    with pytest.raises(TypeError, match="field"):
        root.assign(missing=1)
    with pytest.raises(TypeError, match="field"):
        root.assign(nope=1)

    root.item = 4
    assert serialized(document) == '<doc a="x"><item>4</item></doc>'


def test_child_list_descriptor_assignment_encodes_tokens_before_mutation():
    schema = compile_body(
        element("items", "<list><oneOrMore>" + data("integer") + "</oneOrMore></list>")
    )
    document = typed(schema, "<doc><items>1 2</items></doc>")
    root = document.root

    root.items = [3, 4]

    assert serialized(document) == "<doc><items>3 4</items></doc>"


def test_descriptor_suffix_and_typed_value_collision_path_assigns_both_positions():
    schema = compile_body(
        '<attribute name="typed_value"><text/></attribute>'
        '<attribute name="typed_value_"><text/></attribute>' + data("integer")
    )
    document = typed(schema, '<doc typed_value="a" typed_value_="b">12</doc>')
    root = document.root
    assert type(root)._relaxng_scalar_field_ == "typed_value__"

    root.assign(typed_value="A", typed_value__=13)

    assert serialized(document) == '<doc typed_value="A" typed_value_="b">13</doc>'
    with pytest.raises(StaleInterpretationError):
        _ = root.typed_value__


def test_descriptor_writes_share_the_transaction_path():
    schema = compile_body('<attribute name="a"><text/></attribute>' + data("integer"))
    document = typed(schema, '<doc a="old">1</doc>')
    root = document.root

    root.a = "new"
    root.typed_value = 2

    assert serialized(document) == '<doc a="new">2</doc>'
    assert document.interpretations_current is False


def test_lexical_writes_remain_available_after_typed_assignment():
    schema = compile_body(
        '<attribute name="a">' + data("integer") + "</attribute>" + data("integer")
    )
    document = typed(schema, '<doc a="1">2</doc>')
    root = document.root

    root.typed_value = 3
    root.set_text("lexical")
    root.set_attribute("a", "spelled-lexically")

    assert serialized(document) == '<doc a="spelled-lexically">lexical</doc>'
    assert root.text_value() == "lexical"
    assert root.attribute_lexical("a") == "spelled-lexically"


def correlated_document():
    choice = (
        "<choice><group>"
        + element("x", data("integer"))
        + element("y", data("integer"))
        + "</group><group>"
        + element("x", data("string"))
        + element("y", data("string"))
        + "</group></choice>"
    )
    schema = compile_body(choice)
    document = typed(schema, "<doc><x>12</x><y>34</y></doc>")
    return schema, document


def test_jointly_incompatible_selections_fail_before_any_mutation():
    schema, document = correlated_document()
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    x, y = root.children
    xi = selection_for(schema, graph, x, "integer")
    ys = selection_for(schema, graph, y, "string")
    before = serialized(document)

    snapshot_x = x.typed_value
    snapshot_y = y.typed_value
    with pytest.raises(ValueError, match=r"joint|compatible"):
        root.assign(x=snapshot_x, y=snapshot_y, selections=(xi, ys))

    assert serialized(document) == before
    assert document.interpretations_current is True


def test_selections_must_belong_to_an_assigned_current_field():
    schema, document = correlated_document()
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    x, y = root.children
    yi = selection_for(schema, graph, y, "integer")
    snapshot_x = x.typed_value
    before = serialized(document)

    with pytest.raises(ValueError, match=r"assigned field|field occurrence"):
        root.assign(x=snapshot_x, selections=(yi,))
    assert serialized(document) == before

    other_schema, other_document = correlated_document()
    other_graph = other_document.binding_evidence
    assert other_graph is not None
    other_yi = selection_for(other_schema, other_graph, other_document.root.children[1], "integer")
    with pytest.raises(ValueError, match=r"current|assigned field|field occurrence"):
        root.assign(x=snapshot_x, selections=(other_yi,))
    assert serialized(document) == before


def test_valid_joint_selections_commit_snapshots_from_an_old_revision():
    schema, document = correlated_document()
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    x, y = root.children
    xi = selection_for(schema, graph, x, "integer")
    yi = selection_for(schema, graph, y, "integer")

    other_document = typed(schema, "<doc><x>99</x><y>88</y></doc>")
    snapshot_x = other_document.root.children[0].typed_value
    snapshot_y = other_document.root.children[1].typed_value

    root.assign(x=snapshot_x, y=snapshot_y, selections=(xi, yi))

    assert serialized(document) == "<doc><x>99</x><y>88</y></doc>"
    assert document.interpretations_current is False
    with pytest.raises(StaleInterpretationError):
        _ = x.typed_value
    with pytest.raises(StaleInterpretationError):
        _ = y.typed_value


def test_a_selection_may_target_a_token_of_an_assigned_list_field():
    schema = compile_body(
        "<list><oneOrMore><choice>"
        + data("integer")
        + data("string")
        + "</choice></oneOrMore></list>"
    )
    document = typed(schema, "<doc>1 2</doc>")
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    token = next(
        site for site in graph.occurrences if site.kind == "token" and site.token_index == 0
    )
    selected = selection_at(schema, graph, token, "integer")
    snapshot = root.typed_value
    assert isinstance(snapshot, AlternativeValue)

    root.assign(typed_value=snapshot, selections=(selected,))

    assert serialized(document) == "<doc>1 2</doc>"
    assert document.interpretations_current is False


def test_selections_after_a_mutation_are_stale():
    schema, document = correlated_document()
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    x, _ = root.children
    xi = selection_for(schema, graph, x, "integer")
    snapshot_x = x.typed_value

    root.set_text(" ")
    with pytest.raises(StaleInterpretationError):
        root.assign(x=snapshot_x, selections=(xi,))


def test_typed_assignment_marks_annotations_stale_when_captured():
    schema = compile_body("<text/>")
    document = typed(schema, "<doc>old</doc>", capture="patterns")
    root = document.root

    root.typed_value = "new"

    with pytest.raises(StaleInterpretationError):
        _ = document.annotations
    assert serialized(document) == "<doc>new</doc>"


def test_builtin_atomic_contract_uses_its_own_encoder_without_evidence():
    schema = compile_body('<attribute name="v">' + data("token", "") + "</attribute><empty/>")
    typed(schema, '<doc v="a"/>')
    contract = root_route(schema).attributes["v"].scalar_contracts[0]
    context = DatatypeContext(None, {})

    assert encode_scalar(contract, " a\tb ", context) == "a b"
    assert encode_scalar(contract, "plain", context) == "plain"


def test_encode_scalar_rejects_wrong_contract_and_context_types():
    schema = compile_body("<text/>")
    contract = root_route(schema).content.scalar_contracts[0]
    context = DatatypeContext(None, {})

    with pytest.raises(TypeError, match="ScalarContract"):
        encode_scalar(object(), "x", context)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DatatypeContext"):
        encode_scalar(contract, "x", object())  # type: ignore[arg-type]


def test_a_datatype_encoder_must_return_lexical_text():
    schema = compile_body(
        f'<attribute name="v">{data("nonstring", OPAQUE)}</attribute><empty/>',
        datatype_libraries={
            OPAQUE: DatatypeLibraryRegistration(NonStringEncoderDatatypeFactory(), "opaque/2"),
        },
    )
    document = typed(schema, '<doc v="original"/>')
    root = document.root
    before = serialized(document)

    with pytest.raises(TypeError, match="lexical string"):
        root.v = "replacement"

    assert serialized(document) == before
    assert document.interpretations_current is True


class NonStringEncoderDatatypeFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return NonStringEncoderDatatype()


def test_optional_absent_list_reuses_its_compiled_token_pattern():
    schema = compile_body(
        "<optional><list><oneOrMore>" + data("integer") + "</oneOrMore></list></optional>"
    )
    document = typed(schema, "<doc/>")
    root = document.root
    assert root.typed_value is None

    root.typed_value = [1]

    assert serialized(document) == "<doc>1</doc>"


def test_bare_value_without_any_scalar_contract_is_rejected():
    schema = compile_body('<attribute name="empty"><empty/></attribute><empty/>')
    document = typed(schema, '<doc empty=""/>')
    root = document.root

    with pytest.raises(ValueError, match="no schema scalar contract"):
        root.empty = "value"

    assert serialized(document) == '<doc empty=""/>'
    assert document.interpretations_current is True


def test_repeatable_and_absent_child_assignments_are_explicit_boundaries():
    repeated = compile_body("<zeroOrMore>" + element("many", data("integer")) + "</zeroOrMore>")
    repeated_document = typed(repeated, "<doc/>")
    with pytest.raises(TypeError, match=r"repeatable|Task 4"):
        repeated_document.root.many = [1]

    optional = compile_body("<optional>" + element("single", data("integer")) + "</optional>")
    optional_document = typed(optional, "<doc/>")
    with pytest.raises(ValueError, match=r"exactly one existing|Task 4"):
        optional_document.root.single = 1


def test_selections_allow_an_absent_assigned_attribute_next_to_a_selected_one():
    schema = compile_body(
        '<attribute name="a">'
        + data("integer")
        + '</attribute><optional><attribute name="b">'
        + data("integer")
        + "</attribute></optional><empty/>"
    )
    document = typed(schema, '<doc a="1"/>')
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    selected = selection_at(schema, graph, attribute_site(graph, root, "a"), "integer")

    root.assign(a=2, b=3, selections=(selected,))

    assert serialized(document) == '<doc a="2" b="3"/>'


def test_empty_assignment_is_a_noop_and_selection_arguments_are_validated():
    schema = compile_body("<text/>")
    document = typed(schema, "<doc>value</doc>")
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    selected = selection_for(schema, graph, root, None)  # text witness has no type name

    assert root.assign() is None
    assert root.assign(selections=()) is None
    assert document.interpretations_current is True
    with pytest.raises(ValueError, match="at least one assigned field"):
        root.assign(selections=(selected,))
    with pytest.raises(TypeError, match="iterable"):
        root.assign(typed_value="next", selections=selected)
    with pytest.raises(TypeError, match="CandidateSelection"):
        root.assign(typed_value="next", selections=(object(),))
    assert serialized(document) == "<doc>value</doc>"


def test_generic_and_untyped_projected_elements_reject_typed_assignment():
    schema = compile_body("<text/>")
    validation = schema.parse(io.StringIO("<doc>value</doc>"), typed=False)
    with pytest.raises(PyXSDError, match="projected"):
        validation.root.assign(typed_value="next")

    root = RelaxNGElement.from_etree(ET.fromstring("<doc>value</doc>"))
    evidence = build_binding_evidence(
        root,
        schema.grammar.start,
        datatype_context=DatatypeContext(None, {}),
        limits=None,
        projection_fingerprint=schema.projection.grammar_fingerprint,
    )
    bind_projection(root, schema.projection, evidence)
    with pytest.raises(PyXSDError, match="valid typed parse"):
        root.assign(typed_value="next")


def test_optional_absent_custom_attribute_reports_no_encoder():
    schema = compile_body(
        '<optional><attribute name="v">'
        + data("opaque", OPAQUE)
        + "</attribute></optional><empty/>",
        datatype_libraries={
            OPAQUE: DatatypeLibraryRegistration(OpaqueFactory(), "opaque/1"),
        },
    )
    document = typed(schema, "<doc/>")
    root = document.root

    with pytest.raises(UnsupportedDatatypeOperation, match="encoder"):
        root.v = "value"

    assert serialized(document) == "<doc/>"


def test_empty_and_zero_or_more_lists_use_schema_static_empty_plans():
    empty_schema = compile_body("<list><empty/></list>")
    empty_document = typed(empty_schema, "<doc></doc>")
    empty_document.root.typed_value = ()
    assert serialized(empty_document) == "<doc></doc>"

    optional_schema = compile_body("<list><zeroOrMore>" + data("integer") + "</zeroOrMore></list>")
    optional_document = typed(optional_schema, "<doc>1 2</doc>")
    assert optional_document.root.typed_value == [1, 2]
    optional_document.root.typed_value = [3]
    assert serialized(optional_document) == "<doc>3</doc>"


def test_named_definition_list_items_use_their_compiled_ref_target():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<grammar xmlns="{NS}"><start>'
            f'<element name="doc" datatypeLibrary="{XSD}">'
            '<list><oneOrMore><ref name="item"/></oneOrMore></list>'
            "</element></start>"
            f'<define name="item">{data("integer")}</define>'
            "</grammar>"
        )
    )
    schema.require_valid()
    document = typed(schema, "<doc>1 2</doc>")

    document.root.typed_value = [3, 4]

    assert serialized(document) == "<doc>3 4</doc>"


@pytest.mark.parametrize(
    "body, xml",
    [
        (
            "<list><oneOrMore><choice>"
            + data("integer")
            + data("string")
            + "</choice></oneOrMore></list>",
            "<doc>1 2</doc>",
        ),
        (
            "<list><group><oneOrMore><choice>"
            + data("integer")
            + data("string")
            + "</choice></oneOrMore>"
            + data("integer")
            + "</group></list>",
            "<doc>1 2</doc>",
        ),
        (
            "<list><group><oneOrMore>"
            + data("integer")
            + "</oneOrMore>"
            + data("integer")
            + "</group></list>",
            "<doc>1 2</doc>",
        ),
        ("<list><choice>" + data("integer") + data("string") + "</choice></list>", "<doc>1</doc>"),
        (
            "<list><choice><oneOrMore><choice>"
            + data("integer")
            + data("string")
            + "</choice></oneOrMore>"
            + data("integer")
            + "</choice></list>",
            "<doc>1 2</doc>",
        ),
    ],
    ids=["one-or-more", "group-none", "group-list", "choice", "choice-none"],
)
def test_non_plain_list_token_plans_fail_closed(body, xml):
    schema = compile_body(body)
    document = typed(schema, xml)

    pattern = root_list_pattern(schema, document)

    assert list_shape(pattern) == "interpretation"
    assert _list_token_plan(pattern) is None


def test_unexpected_list_content_has_no_token_plan():
    assert _combine_list_tokens(NotAllowed(), {}) is None


def test_raw_list_token_plans_cover_references_and_mirrored_empty_choices():
    datatype = XsdDatatypeFactory().create("integer", (), DatatypeContext(None, {}))
    item = Data(datatype)

    reference = Ref()
    reference.bind(item)
    reference_plan = _list_token_plan(List(reference))

    assert reference_plan is not None
    assert reference_plan.kind == "fixed"
    assert reference_plan.patterns == (item,)

    mirrored = List(Choice(OneOrMore(item), Empty()))
    mirrored_plan = _list_token_plan(mirrored)

    assert list_shape(mirrored) == "list"
    assert mirrored_plan is not None
    assert mirrored_plan.kind == "list"
    assert mirrored_plan.patterns == (item,)
