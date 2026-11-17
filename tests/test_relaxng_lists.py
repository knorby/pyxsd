"""RELAX NG list interpretations and correlated token assignments.

A list interpretation is built only from accepting evidence.  Token candidate
views are marginal possibilities; selecting one datatype per token must be
checked against the retained list-derivation constraint graph, never by
independent membership in those marginal views.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import Any, cast
from xml.etree import ElementTree as ET

import pytest

from pyxsd.namespaces import NamespaceContext
from pyxsd.relaxng.contracts import (
    candidate_id,
    list_items_decodable,
    list_shape,
    pattern_identity,
    scalar_contract,
)
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    XSD_LIBRARY,
    BuiltinDatatypeFactory,
    Datatype,
    DatatypeContext,
    UnsupportedDatatypeOperation,
    XsdDatatypeFactory,
)
from pyxsd.relaxng.errors import AmbiguousValueError
from pyxsd.relaxng.evidence import (
    ScalarWitness,
    TokenConstraintGraph,
    build_binding_evidence,
)
from pyxsd.relaxng.names import Name
from pyxsd.relaxng.patterns import (
    Choice,
    Data,
    DataExcept,
    Element,
    Empty,
    Group,
    Interleave,
    List,
    OneOrMore,
    Ref,
    Text,
    Value,
)
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.validator import TokenSpan, validate_element
from pyxsd.relaxng.values import (
    AlternativeValue,
    Candidate,
    alternative_value_from_witnesses,
    list_interpretation,
    list_value,
)

CX = DatatypeContext(None, {})
BUILTIN = BuiltinDatatypeFactory()
XSD = XsdDatatypeFactory()
STRING = BUILTIN.create("string", (), CX)
TOKEN = BUILTIN.create("token", (), CX)
INTEGER = XSD.create("integer", (), CX)
BOOLEAN = XSD.create("boolean", (), CX)
QNAME = XSD.create("QName", (), CX)


def element(name, content=None):
    return Element(Name(None, name), Empty() if content is None else content)


def tree(xml, *, namespaces=None):
    etree = ET.fromstring(xml)
    if namespaces is None:
        return RelaxNGElement.from_etree(etree)
    context = NamespaceContext()
    context.record(etree, namespaces)
    return RelaxNGElement.from_etree(etree, namespaces=context)


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


def build(xml, content, *, namespaces=None, datatype_context=CX):
    root = tree(xml, namespaces=namespaces)
    start = element("r", content)
    assert validate_element(root, start).valid
    graph = build_binding_evidence(root, start, datatype_context=datatype_context, limits=None)
    return graph, start, site(graph, root)


def list_witnesses(graph, occurrence):
    return tuple(
        witness
        for witness in graph.scalar_witnesses_at(occurrence)
        if isinstance(witness.pattern, List)
    )


def only_list_witness(graph, occurrence):
    (witness,) = list_witnesses(graph, occurrence)
    return witness


def interpret(graph, occurrence, *, grammar="grammar", revision=1):
    return list_interpretation(
        only_list_witness(graph, occurrence),
        graph,
        grammar_fingerprint=grammar,
        revision=revision,
    )


def value_at(graph, occurrence, contracts, *, grammar="grammar", revision=1):
    return list_value(
        list_witnesses(graph, occurrence),
        graph,
        schema_contracts=contracts,
        grammar_fingerprint=grammar,
        revision=revision,
    )


def fixed_list_contract(content):
    if isinstance(content, List):
        return scalar_contract(content)
    return scalar_contract(List(content))


class NonDecodingDatatype(Datatype):
    """A named custom datatype that validates but provides no decoder."""

    library_uri = "urn:custom"
    type_name = "opaque"
    parameters = ()

    def allows(self, lexical, context):
        return lexical == "ok"

    def equal(self, left, left_context, right, right_context):
        return left == right


class RaisingDecodeDatatype(NonDecodingDatatype):
    """Overrides decode only to report that no decoder exists.

    It deliberately does not declare decoder support: an override alone is not
    a capability claim.
    """

    type_name = "raising"

    def decode(self, lexical, context):
        raise UnsupportedDatatypeOperation(f"{self.type_name} has no decoder")


class DeclaredDecodingDatatype(NonDecodingDatatype):
    """A custom datatype that explicitly declares and provides a decoder."""

    type_name = "decoded"
    supports_decode = True

    def decode(self, lexical, context):
        return lexical.upper()


class UndeclaredDecodingDatatype(DeclaredDecodingDatatype):
    """Successfully decodes but does not declare the capability."""

    type_name = "undeclared"
    supports_decode = False


class UndeclaredCapabilityDatatype(DeclaredDecodingDatatype):
    """Same identity metadata as the declared datatype, without the claim."""

    supports_decode = False


ZERO_OR_MORE_IDS = ("empty-first", "empty-last", "ref-wrapped")


def zero_or_more_contents(item):
    wrapped = Ref()
    wrapped.bind(Choice(Empty(), OneOrMore(item)))
    return (
        List(Choice(Empty(), OneOrMore(item))),
        List(Choice(OneOrMore(item), Empty())),
        List(wrapped),
    )


class TestListShape:
    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (Empty(), "tuple"),
            (Data(INTEGER), "tuple"),
            (Group(Data(INTEGER), Data(TOKEN)), "tuple"),
            (Group(Data(INTEGER), Group(Data(TOKEN), Data(INTEGER))), "tuple"),
            (OneOrMore(Data(INTEGER)), "list"),
            (OneOrMore(Value(INTEGER, "1")), "list"),
            (Choice(Empty(), OneOrMore(Data(INTEGER))), "list"),
            (Choice(OneOrMore(Data(INTEGER)), Empty()), "list"),
            (Choice(Empty(), OneOrMore(Data(TOKEN))), "list"),
            (Choice(Data(INTEGER), Data(STRING)), "interpretation"),
            (Choice(Empty(), Data(INTEGER)), "interpretation"),
            (Choice(Empty(), OneOrMore(Choice(Data(INTEGER), Data(STRING)))), "interpretation"),
            (Choice(Empty(), OneOrMore(Group(Data(INTEGER), Data(TOKEN)))), "interpretation"),
            (OneOrMore(Choice(Data(INTEGER), Data(STRING))), "interpretation"),
            (OneOrMore(Group(Data(INTEGER), Data(TOKEN))), "interpretation"),
            (Group(OneOrMore(Data(INTEGER)), Data(TOKEN)), "interpretation"),
            (Group(Choice(Empty(), Data(INTEGER)), Data(TOKEN)), "interpretation"),
            (Interleave(Data(INTEGER), Data(TOKEN)), "interpretation"),
            (Text(), "interpretation"),
        ],
    )
    def test_shape_classification_is_schema_static(self, content, expected):
        assert list_shape(List(content)) == expected

    def test_references_are_followed_to_their_compiled_target(self):
        integer = Ref()
        integer.bind(Data(INTEGER))
        repeated = Ref()
        repeated.bind(OneOrMore(Data(INTEGER)))

        assert list_shape(List(integer)) == "tuple"
        assert list_shape(List(repeated)) == "list"

    def test_reference_cycles_are_not_plain_list_shapes(self):
        recursive = Ref()
        recursive.bind(Choice(Data(INTEGER), recursive))

        assert list_shape(List(OneOrMore(recursive))) == "interpretation"

    def test_shared_subpatterns_are_analysed_once(self):
        shared = Data(INTEGER)

        assert list_shape(List(Group(shared, shared))) == "tuple"
        nested = List(
            Group(
                Group(shared, Data(TOKEN)),
                Group(shared, Value(INTEGER, "7")),
            )
        )
        assert list_shape(nested) == "tuple"

    def test_plain_item_decoder_eligibility_is_schema_static(self):
        assert list_items_decodable(List(Data(INTEGER)))
        assert list_items_decodable(List(Choice(Empty(), OneOrMore(Data(INTEGER)))))
        assert list_items_decodable(List(Empty()))
        assert not list_items_decodable(List(Data(NonDecodingDatatype())))
        assert not list_items_decodable(
            List(Choice(Empty(), OneOrMore(Data(NonDecodingDatatype()))))
        )
        assert not list_items_decodable(List(Choice(Data(INTEGER), Data(STRING))))
        with pytest.raises(TypeError, match="list"):
            list_items_decodable(Data(INTEGER))

    def test_decoder_capability_declarations(self):
        assert Datatype.supports_decode is False
        assert type(INTEGER).supports_decode is True

    def test_decoder_capability_is_not_semantic_identity(self):
        declared = Data(DeclaredDecodingDatatype())
        undeclared = Data(UndeclaredCapabilityDatatype())

        assert pattern_identity(declared) == pattern_identity(undeclared)
        assert scalar_contract(declared) == scalar_contract(undeclared)
        assert candidate_id("grammar", scalar_contract(declared)) == candidate_id(
            "grammar", scalar_contract(undeclared)
        )
        assert list_items_decodable(List(OneOrMore(declared)))
        assert not list_items_decodable(List(OneOrMore(undeclared)))

    def test_only_list_patterns_have_a_list_shape(self):
        with pytest.raises(TypeError, match="list"):
            list_shape(Data(INTEGER))


class TestFixedHeterogeneousLists:
    def build_fixed(self):
        return build(
            "<r>12 abc</r>",
            List(Group(Data(INTEGER), Data(TOKEN))),
        )

    def test_fixed_items_expose_a_typed_tuple(self):
        graph, start, occurrence = self.build_fixed()
        witness = only_list_witness(graph, occurrence)
        interpretation = list_interpretation(
            witness, graph, grammar_fingerprint="grammar", revision=3
        )

        assert interpretation.lexical == "12 abc"
        assert interpretation.token_spans == (
            TokenSpan("12", 0, 2),
            TokenSpan("abc", 3, 6),
        )
        assert interpretation.context == CX
        assert interpretation.grammar_fingerprint == "grammar"
        assert interpretation.revision == 3
        assert interpretation.shape == "tuple"
        assert interpretation.contract.kind == "list"
        assert interpretation.candidate_id == candidate_id("grammar", interpretation.contract)
        assert [len(candidates) for candidates in interpretation.token_candidates] == [1, 1]
        assert interpretation.token_candidates[0][0].decoded == 12
        assert interpretation.token_candidates[1][0].decoded == "abc"
        assert interpretation.is_plain
        assert interpretation.as_python() == (12, "abc")
        assert type(interpretation.as_python()) is tuple
        assert interpretation.as_types(("xsd:integer", "token")) == (12, "abc")
        assert interpretation.as_types(((XSD_LIBRARY, "integer"), (BUILTIN_LIBRARY, "token"))) == (
            12,
            "abc",
        )
        assert str(interpretation) == "12 abc"

        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts, revision=3)
        assert value == (12, "abc")
        assert type(value) is tuple

    def test_token_positions_come_from_the_accepting_graph(self):
        graph, _, occurrence = self.build_fixed()
        interpretation = interpret(graph, occurrence)

        assert [
            (branch.span.text, branch.span.start, branch.span.end)
            for branch in interpretation.constraints.token_occurrences
        ] == [("12", 0, 2), ("abc", 3, 6)]
        assert [(span.text, span.start, span.end) for span in interpretation.token_spans] == [
            ("12", 0, 2),
            ("abc", 3, 6),
        ]

    def test_references_and_exceptions_follow_compiled_legality(self):
        integer = Ref()
        integer.bind(Data(INTEGER))
        content = List(Group(integer, Data(TOKEN)))
        graph, _, occurrence = build("<r>12 abc</r>", content)

        interpretation = interpret(graph, occurrence)
        assert interpretation.shape == "tuple"
        assert interpretation.as_python() == (12, "abc")

        excluded = List(
            Group(
                DataExcept(INTEGER, Value(INTEGER, "0")),
                Data(TOKEN),
            )
        )
        graph, _, occurrence = build("<r>12 abc</r>", excluded)
        interpretation = interpret(graph, occurrence)
        (candidate,) = interpretation.token_candidates[0]
        assert candidate.contract.kind == "atomic"
        assert interpretation.as_python() == (12, "abc")

    def test_xml_whitespace_only_splits_tokens_and_offsets_are_retained(self):
        graph, _, occurrence = build(
            "<r>alpha  beta</r>",
            List(Group(Data(STRING), Data(STRING))),
        )
        interpretation = interpret(graph, occurrence)

        assert interpretation.token_spans == (
            TokenSpan("alpha", 0, 5),
            TokenSpan("beta", 7, 11),
        )
        assert interpretation.as_python() == ("alpha", "beta")

        graph, _, occurrence = build("<r>a\u00a0b</r>", List(Data(STRING)))
        interpretation = interpret(graph, occurrence)
        assert interpretation.token_spans == (TokenSpan("a\u00a0b", 0, 3),)
        assert interpretation.as_python() == ("a\u00a0b",)


class TestHomogeneousLists:
    def test_repeated_items_expose_a_typed_list(self):
        graph, start, occurrence = build(
            "<r>1  2 3</r>",
            List(OneOrMore(Data(INTEGER))),
        )
        interpretation = interpret(graph, occurrence)

        assert interpretation.shape == "list"
        assert interpretation.is_plain
        assert interpretation.token_spans == (
            TokenSpan("1", 0, 1),
            TokenSpan("2", 3, 4),
            TokenSpan("3", 5, 6),
        )
        assert interpretation.as_python() == [1, 2, 3]
        assert type(interpretation.as_python()) is list
        assert interpretation.as_types(("xsd:integer",) * 3) == (1, 2, 3)

        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert value == [1, 2, 3]
        assert type(value) is list


class TestOptionalAndRepeatingHeterogeneousLists:
    @pytest.mark.parametrize(
        ("xml", "content", "expected", "selectors"),
        [
            (
                "<r>1 2 abc</r>",
                List(Group(OneOrMore(Data(INTEGER)), Data(TOKEN))),
                (1, 2, "abc"),
                ("xsd:integer", "xsd:integer", "token"),
            ),
            (
                "<r>1 abc</r>",
                List(Group(Choice(Empty(), Data(INTEGER)), Data(TOKEN))),
                (1, "abc"),
                ("xsd:integer", "token"),
            ),
            (
                "<r>abc</r>",
                List(Group(Choice(Empty(), Data(INTEGER)), Data(TOKEN))),
                ("abc",),
                ("token",),
            ),
        ],
    )
    def test_nonplain_lists_expose_an_interpretation(self, xml, content, expected, selectors):
        graph, start, occurrence = build(xml, content)
        interpretation = interpret(graph, occurrence)

        assert interpretation.shape == "interpretation"
        assert not interpretation.is_plain
        assert interpretation.as_python() == expected
        assert type(interpretation.as_python()) is tuple
        assert interpretation.as_types(selectors) == expected

        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert isinstance(value, AlternativeValue)
        assert value.as_python() == expected
        (candidate,) = value.candidates
        assert candidate.decoded == expected
        assert value.as_interpretation(candidate.candidate_id) == interpretation

    def test_optional_heterogeneous_wrapper_selection_is_schema_static(self):
        content = List(Group(Choice(Empty(), Data(INTEGER)), Data(TOKEN)))
        contracts = (fixed_list_contract(content),)

        first_graph, _, first_occurrence = build("<r>abc</r>", content)
        second_graph, _, second_occurrence = build("<r>1 abc</r>", content)
        first = value_at(first_graph, first_occurrence, contracts)
        second = value_at(second_graph, second_occurrence, contracts)

        assert isinstance(first, AlternativeValue)
        assert isinstance(second, AlternativeValue)
        assert first.as_python() == ("abc",)
        assert second.as_python() == (1, "abc")


class TestEmptyLists:
    def test_fixed_empty_list_is_an_empty_tuple_without_zero_candidate_failure(self):
        graph, start, occurrence = build("<r/>", List(Empty()))
        interpretation = interpret(graph, occurrence)

        assert interpretation.lexical == ""
        assert interpretation.token_spans == ()
        assert interpretation.token_candidates == ()
        assert interpretation.shape == "tuple"
        assert interpretation.is_plain
        assert interpretation.as_python() == ()
        assert interpretation.as_types(()) == ()

        contracts = (scalar_contract(start.pattern),)
        assert value_at(graph, occurrence, contracts) == ()

    def test_zero_or_more_homogeneous_empty_list_is_a_plain_empty_list(self):
        content = List(Choice(Empty(), OneOrMore(Data(INTEGER))))
        graph, start, occurrence = build("<r/>", content)
        interpretation = interpret(graph, occurrence)

        assert interpretation.token_spans == ()
        assert interpretation.token_candidates == ()
        assert interpretation.shape == "list"
        assert interpretation.is_plain
        assert interpretation.as_python() == []
        assert interpretation.as_types(()) == ()

        contracts = (scalar_contract(start.pattern),)
        assert value_at(graph, occurrence, contracts) == []

    def test_zero_or_more_homogeneous_nonempty_list_is_a_plain_list(self):
        content = List(Choice(Empty(), OneOrMore(Data(INTEGER))))
        graph, start, occurrence = build("<r>1 2</r>", content)
        interpretation = interpret(graph, occurrence)

        assert interpretation.shape == "list"
        assert interpretation.is_plain
        assert interpretation.as_python() == [1, 2]
        assert interpretation.as_types(("xsd:integer", "xsd:integer")) == (1, 2)

        contracts = (scalar_contract(start.pattern),)
        assert value_at(graph, occurrence, contracts) == [1, 2]

    @pytest.mark.parametrize("xml", ["<r/>", "<r>1 2</r>"])
    def test_zero_or_more_form_behind_a_reference_is_list_shaped(self, xml):
        reference = Ref()
        reference.bind(Choice(Empty(), OneOrMore(Data(INTEGER))))
        content = List(reference)
        graph, start, occurrence = build(xml, content)
        interpretation = interpret(graph, occurrence)

        expected = [] if xml == "<r/>" else [1, 2]
        assert interpretation.shape == "list"
        assert interpretation.is_plain
        assert interpretation.as_python() == expected

        contracts = (scalar_contract(start.pattern),)
        assert value_at(graph, occurrence, contracts) == expected

    @pytest.mark.parametrize("xml", ["<r/>", "<r>1 2</r>"])
    def test_zero_or_more_branches_behind_references_are_list_shaped(self, xml):
        empty = Ref()
        empty.bind(Empty())
        repeated = Ref()
        repeated.bind(OneOrMore(Data(INTEGER)))
        content = List(Choice(empty, repeated))
        graph, start, occurrence = build(xml, content)
        interpretation = interpret(graph, occurrence)

        expected = [] if xml == "<r/>" else [1, 2]
        assert interpretation.shape == "list"
        assert interpretation.is_plain
        assert interpretation.as_python() == expected

        contracts = (scalar_contract(start.pattern),)
        assert value_at(graph, occurrence, contracts) == expected

    @pytest.mark.parametrize("xml", ["<r/>", "<r>ok</r>"])
    def test_zero_or_more_nondecodable_items_never_unwrap(self, xml):
        content = List(Choice(Empty(), OneOrMore(Data(NonDecodingDatatype()))))
        graph, start, occurrence = build(xml, content)
        interpretation = interpret(graph, occurrence)

        assert interpretation.shape == "list"
        assert not interpretation.is_plain
        with pytest.raises(UnsupportedDatatypeOperation):
            interpretation.as_python()

        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert isinstance(value, AlternativeValue)
        (candidate,) = value.candidates
        assert candidate.decodable is False
        assert candidate.decoded is None
        with pytest.raises(UnsupportedDatatypeOperation):
            value.as_python()
        assert value.as_interpretation(candidate.candidate_id) == interpretation


class TestZeroOrMoreDecoderCapability:
    @pytest.mark.parametrize(
        "content",
        zero_or_more_contents(Data(RaisingDecodeDatatype())),
        ids=ZERO_OR_MORE_IDS,
    )
    @pytest.mark.parametrize("xml", ["<r/>", "<r>ok</r>"])
    def test_overridden_raising_decode_without_declaration_stays_wrapped(self, xml, content):
        graph, start, occurrence = build(xml, content)
        interpretation = interpret(graph, occurrence)

        assert interpretation.shape == "list"
        assert not interpretation.is_plain
        with pytest.raises(UnsupportedDatatypeOperation):
            interpretation.as_python()

        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert isinstance(value, AlternativeValue)
        (candidate,) = value.candidates
        assert candidate.decodable is False
        assert candidate.decoded is None
        with pytest.raises(UnsupportedDatatypeOperation):
            value.as_python()

        if xml == "<r/>":
            assert interpretation.token_candidates == ()
            assert value.candidates[0].decoded is None
        else:
            assert len(interpretation.token_candidates[0]) == 1

    @pytest.mark.parametrize(
        "content",
        zero_or_more_contents(Data(DeclaredDecodingDatatype())),
        ids=ZERO_OR_MORE_IDS,
    )
    @pytest.mark.parametrize(
        ("xml", "expected"),
        [("<r/>", []), ("<r>ok</r>", ["OK"])],
    )
    def test_declared_decoders_are_plain(self, xml, expected, content):
        graph, start, occurrence = build(xml, content)
        interpretation = interpret(graph, occurrence)

        assert interpretation.shape == "list"
        assert interpretation.is_plain
        assert interpretation.as_python() == expected
        selectors = () if not expected else (("urn:custom", "decoded"),)
        assert interpretation.as_types(selectors) == tuple(expected)

        contracts = (scalar_contract(start.pattern),)
        assert value_at(graph, occurrence, contracts) == expected

    @pytest.mark.parametrize(
        "content",
        zero_or_more_contents(Data(UndeclaredDecodingDatatype())),
        ids=ZERO_OR_MORE_IDS,
    )
    @pytest.mark.parametrize("xml", ["<r/>", "<r>ok</r>"])
    def test_successful_decode_without_declaration_stays_wrapped(self, xml, content):
        graph, start, occurrence = build(xml, content)
        interpretation = interpret(graph, occurrence)

        assert interpretation.shape == "list"
        assert not interpretation.is_plain
        with pytest.raises(UnsupportedDatatypeOperation):
            interpretation.as_python()

        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert isinstance(value, AlternativeValue)
        (candidate,) = value.candidates
        assert candidate.decodable is False
        assert candidate.decoded is None

    def test_builtin_homogeneous_zero_or_more_stays_plain(self):
        content = List(Choice(OneOrMore(Data(INTEGER)), Empty()))
        graph, start, occurrence = build("<r>1 2</r>", content)
        interpretation = interpret(graph, occurrence)

        assert interpretation.is_plain
        assert interpretation.as_python() == [1, 2]
        contracts = (scalar_contract(start.pattern),)
        assert value_at(graph, occurrence, contracts) == [1, 2]

    def test_builtin_nondecoding_fixed_item_stays_wrapped(self):
        graph, start, occurrence = build("<r>ok</r>", List(Data(NonDecodingDatatype())))
        interpretation = interpret(graph, occurrence)

        assert interpretation.shape == "tuple"
        assert not interpretation.is_plain
        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert isinstance(value, AlternativeValue)
        assert value.candidates[0].decoded is None


class TestAmbiguousTokenAssignments:
    def test_ambiguous_assignments_survive_without_preference(self):
        graph, start, occurrence = build("<r>12</r>", List(Choice(Data(INTEGER), Data(STRING))))
        interpretation = interpret(graph, occurrence)

        (candidates,) = interpretation.token_candidates
        assert len(candidates) == 2
        assert {candidate.decoded for candidate in candidates} == {12, "12"}
        assert len({candidate.contract for candidate in candidates}) == 2
        assert [candidate.candidate_id for candidate in candidates] == sorted(
            candidate.candidate_id for candidate in candidates
        )
        assert not interpretation.is_plain
        assert interpretation.shape == "interpretation"
        assert interpretation.as_types(("xsd:integer",)) == (12,)
        assert interpretation.as_types(("string",)) == ("12",)
        with pytest.raises(AmbiguousValueError, match="token interpretation"):
            interpretation.as_python()

        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert isinstance(value, AlternativeValue)
        (candidate,) = value.candidates
        assert candidate.decodable is False
        with pytest.raises(AmbiguousValueError, match="token interpretation"):
            value.as_python()
        with pytest.raises(AmbiguousValueError, match="token interpretation"):
            value.as_candidate(candidate.candidate_id)
        assert value.as_interpretation(candidate.candidate_id) == interpretation

    def test_a_unique_instance_does_not_change_the_schema_static_wrapper(self):
        content = List(Choice(Data(INTEGER), Data(STRING)))
        contracts = (fixed_list_contract(content),)

        ambiguous_graph, _, ambiguous_occurrence = build("<r>12</r>", content)
        unique_graph, _, unique_occurrence = build("<r>abc</r>", content)
        ambiguous = value_at(ambiguous_graph, ambiguous_occurrence, contracts)
        unique = value_at(unique_graph, unique_occurrence, contracts)

        assert isinstance(ambiguous, AlternativeValue)
        assert isinstance(unique, AlternativeValue)
        assert unique.as_python() == ("abc",)
        with pytest.raises(AmbiguousValueError):
            ambiguous.as_python()


class TestCorrelatedCrossProducts:
    content = List(
        Choice(
            Group(Data(INTEGER), Data(INTEGER)),
            Group(Data(STRING), Data(STRING)),
        )
    )

    def build(self):
        return build("<r>12 34</r>", self.content)

    def test_each_token_retains_both_marginal_candidates(self):
        graph, _, occurrence = self.build()
        interpretation = interpret(graph, occurrence)

        assert [len(group) for group in interpretation.token_candidates] == [2, 2]
        assert {candidate.decoded for candidate in interpretation.token_candidates[0]} == {
            12,
            "12",
        }
        assert {candidate.decoded for candidate in interpretation.token_candidates[1]} == {
            34,
            "34",
        }

    def test_accepted_correlated_assignments_succeed(self):
        graph, _, occurrence = self.build()
        interpretation = interpret(graph, occurrence)

        assert interpretation.as_types(("xsd:integer", "xsd:integer")) == (12, 34)
        assert interpretation.as_types(("string", "string")) == ("12", "34")

    def test_grammar_forbidden_cross_products_are_rejected(self):
        graph, _, occurrence = self.build()
        interpretation = interpret(graph, occurrence)

        with pytest.raises(ValueError, match="complete list derivation"):
            interpretation.as_types(("xsd:integer", "string"))
        with pytest.raises(ValueError, match="complete list derivation"):
            interpretation.as_types(("string", "xsd:integer"))
        with pytest.raises(AmbiguousValueError):
            interpretation.as_python()

    def test_ids_are_deterministic_across_rebuilds(self):
        first_graph, _, first_occurrence = self.build()
        second_graph, _, second_occurrence = self.build()
        first = interpret(first_graph, first_occurrence)
        second = interpret(second_graph, second_occurrence)

        assert first == second
        assert first.candidate_id == second.candidate_id
        assert [
            [candidate.candidate_id for candidate in group] for group in first.token_candidates
        ] == [[candidate.candidate_id for candidate in group] for group in second.token_candidates]


class TestAsTypesRequiresAWholeAssignment:
    def test_one_selector_is_required_per_token(self):
        graph, _, occurrence = build("<r>12 abc</r>", List(Group(Data(INTEGER), Data(TOKEN))))
        interpretation = interpret(graph, occurrence)

        with pytest.raises(ValueError, match="one selector per token"):
            interpretation.as_types(("xsd:integer",))
        with pytest.raises(ValueError, match="one selector per token"):
            interpretation.as_types(("xsd:integer", "token", "token"))
        with pytest.raises(ValueError, match="one selector per token"):
            interpretation.as_types(("xsd:integer",))

    def test_a_selector_must_match_a_surviving_atomic_candidate(self):
        graph, _, occurrence = build("<r>12 abc</r>", List(Group(Data(INTEGER), Data(TOKEN))))
        interpretation = interpret(graph, occurrence)

        with pytest.raises(ValueError, match="no surviving token candidate"):
            interpretation.as_types(("xsd:boolean", "token"))
        with pytest.raises(ValueError, match="no surviving token candidate"):
            interpretation.as_types(("xsd:integer", "xsd:integer"))

    def test_several_contracts_with_one_datatype_are_ambiguous(self):
        graph, _, occurrence = build("<r>12</r>", List(Choice(Value(INTEGER, "12"), Data(INTEGER))))
        interpretation = interpret(graph, occurrence)

        assert len(interpretation.token_candidates[0]) == 2
        with pytest.raises(AmbiguousValueError, match="cannot distinguish"):
            interpretation.as_types(("xsd:integer",))

    def test_text_tokens_have_no_datatype_selector(self):
        graph, start, occurrence = build("<r>one two</r>", List(Text()))
        interpretation = interpret(graph, occurrence)

        assert [len(group) for group in interpretation.token_candidates] == [1, 1]
        assert all(
            not candidate.decodable
            for group in interpretation.token_candidates
            for candidate in group
        )
        with pytest.raises(ValueError, match="no surviving token candidate"):
            interpretation.as_types(("string", "string"))
        with pytest.raises(UnsupportedDatatypeOperation):
            interpretation.as_python()

        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert isinstance(value, AlternativeValue)
        with pytest.raises(UnsupportedDatatypeOperation):
            value.as_python()

    def test_tokens_without_decoders_are_reported_explicitly(self):
        graph, start, occurrence = build("<r>ok</r>", List(Data(NonDecodingDatatype())))
        interpretation = interpret(graph, occurrence)
        (candidate,) = interpretation.token_candidates[0]

        assert candidate.contract.library_uri == "urn:custom"
        assert candidate.decodable is False
        assert interpretation.shape == "tuple"
        assert not interpretation.is_plain
        with pytest.raises(UnsupportedDatatypeOperation):
            interpretation.as_types((("urn:custom", "opaque"),))

        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert isinstance(value, AlternativeValue)
        (whole_candidate,) = value.candidates
        assert value.as_interpretation(whole_candidate.candidate_id) == interpretation
        with pytest.raises(UnsupportedDatatypeOperation):
            value.as_python()

    def test_empty_assignment_requires_no_selector(self):
        graph, _, occurrence = build("<r/>", List(Empty()))
        interpretation = interpret(graph, occurrence)

        assert interpretation.as_types(()) == ()
        with pytest.raises(ValueError, match="one selector per token"):
            interpretation.as_types(("string",))


class TestSchemaStaticWrapperSelection:
    def test_one_plain_contract_is_ordinary_typed_data(self):
        graph, start, occurrence = build("<r>1 2 3</r>", List(OneOrMore(Data(INTEGER))))
        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert value == [1, 2, 3]
        assert not isinstance(value, AlternativeValue)

    def test_two_schema_contracts_stay_wrapped_with_one_survivor(self):
        start = element("r", Choice(List(Data(INTEGER)), List(Data(BOOLEAN))))
        contracts = tuple(
            scalar_contract(pattern) for pattern in (start.pattern.first, start.pattern.second)
        )
        integer_root = tree("<r>12</r>")
        assert validate_element(integer_root, start).valid
        integer_graph = build_binding_evidence(
            integer_root, start, datatype_context=CX, limits=None
        )
        integer_value = value_at(integer_graph, site(integer_graph, integer_root), contracts)

        boolean_root = tree("<r>true</r>")
        assert validate_element(boolean_root, start).valid
        boolean_graph = build_binding_evidence(
            boolean_root, start, datatype_context=CX, limits=None
        )
        boolean_value = value_at(boolean_graph, site(boolean_graph, boolean_root), contracts)

        assert isinstance(integer_value, AlternativeValue)
        assert isinstance(boolean_value, AlternativeValue)
        assert integer_value.as_python() == (12,)
        assert boolean_value.as_python() == (True,)
        assert len(integer_value.candidates) == 1
        assert len(boolean_value.candidates) == 1
        assert integer_value.candidates[0].contract != boolean_value.candidates[0].contract

    def test_a_mixed_scalar_route_keeps_a_list_only_survivor_wrapped(self):
        content = Choice(Data(INTEGER), List(Data(TOKEN)))
        start = element("r", content)
        contracts = tuple(scalar_contract(pattern) for pattern in (content.first, content.second))
        root = tree("<r>abc</r>")
        assert validate_element(root, start).valid
        graph = build_binding_evidence(root, start, datatype_context=CX, limits=None)
        occurrence = site(graph, root)
        value = value_at(graph, occurrence, contracts)

        assert isinstance(value, AlternativeValue)
        assert len(value.candidates) == 1
        assert value.candidates[0].contract.kind == "list"
        assert value.as_python() == ("abc",)
        (interpretation,) = value.interpretations
        assert interpretation.as_types(("token",)) == ("abc",)

        list_only = element("r", List(Data(TOKEN)))
        list_graph = build_binding_evidence(root, list_only, datatype_context=CX, limits=None)
        list_value_contract = (scalar_contract(list_only.pattern),)
        list_only_value = value_at(list_graph, site(list_graph, root), list_value_contract)
        assert list_only_value == ("abc",)
        assert not isinstance(list_only_value, AlternativeValue)

    def test_equal_python_values_from_distinct_list_interpretations_stay_distinct(self):
        content = Choice(List(Data(QNAME)), List(Data(STRING)))
        graph, _, occurrence = build("<r>p:x</r>", content, namespaces={"p": "urn:p"})
        contracts = tuple(scalar_contract(pattern) for pattern in (content.first, content.second))
        value = value_at(graph, occurrence, contracts)

        assert isinstance(value, AlternativeValue)
        assert len(value.candidates) == 2
        assert len({candidate.candidate_id for candidate in value.candidates}) == 2
        assert len({candidate.contract for candidate in value.candidates}) == 2
        assert value.candidates[0].decoded == value.candidates[1].decoded == ("p:x",)
        with pytest.raises(AmbiguousValueError):
            value.as_python()

        assert len(value.interpretations) == 2
        decoded = {}
        for interpretation in value.interpretations:
            token_contract = interpretation.token_candidates[0][0].contract
            decoded[token_contract.library_uri] = interpretation.as_types(
                ("xsd:QName",) if token_contract.library_uri == XSD_LIBRARY else ("string",)
            )
        assert decoded == {XSD_LIBRARY: ("p:x",), BUILTIN_LIBRARY: ("p:x",)}
        for candidate in value.candidates:
            assert value.as_candidate(candidate.candidate_id) == ("p:x",)
            assert value.as_interpretation(candidate.candidate_id).candidate_id == (
                candidate.candidate_id
            )

    def test_ambiguous_whole_list_contract_is_wrapped(self):
        graph, start, occurrence = build("<r>12</r>", List(Choice(Data(INTEGER), Data(STRING))))
        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)

        assert isinstance(value, AlternativeValue)
        assert len(value.candidates) == 1
        assert value.candidates[0].decodable is False
        assert len(value.interpretations) == 1

    def test_a_nonplain_list_contract_is_always_wrapped(self):
        content = List(Group(Choice(Empty(), Data(INTEGER)), Data(TOKEN)))
        contracts = (fixed_list_contract(content),)

        first_graph, _, first_occurrence = build("<r>abc</r>", content)
        second_graph, _, second_occurrence = build("<r>1 abc</r>", content)
        assert isinstance(value_at(first_graph, first_occurrence, contracts), AlternativeValue)
        assert isinstance(value_at(second_graph, second_occurrence, contracts), AlternativeValue)


class TestLargeRepeatedChoiceLists:
    def test_assignments_are_not_enumerated(self):
        count = 60
        xml = "<r>" + " ".join(str(index) for index in range(1, count + 1)) + "</r>"
        content = List(OneOrMore(Choice(Data(INTEGER), Data(STRING))))
        graph, start, occurrence = build(xml, content)
        interpretation = interpret(graph, occurrence)

        assert len(interpretation.token_candidates) == count
        assert [len(group) for group in interpretation.token_candidates] == [2] * count
        assert len(interpretation.constraints.token_occurrences) == count
        assert len(interpretation.constraints.edges) <= 14 * count
        assert len(interpretation.constraints.nodes) <= 16 * count
        with pytest.raises(AmbiguousValueError):
            interpretation.as_python()

        contracts = (scalar_contract(start.pattern),)
        value = value_at(graph, occurrence, contracts)
        assert isinstance(value, AlternativeValue)
        assert len(value.candidates) == 1
        assert value.candidates[0].decodable is False


class TestListInterpretationConstruction:
    def setup_method(self):
        graph, _, occurrence = build("<r>12 abc</r>", List(Group(Data(INTEGER), Data(TOKEN))))
        self.graph = graph
        self.occurrence = occurrence
        self.interpretation = interpret(graph, occurrence)

    def test_values_are_immutable_and_unhashable(self):
        with pytest.raises(FrozenInstanceError):
            self.interpretation.lexical = "13"
        with pytest.raises(TypeError, match="unhashable"):
            hash(self.interpretation)

    def test_context_is_copied_into_the_snapshot(self):
        context = DatatypeContext("urn:base", {"p": "urn:p"})
        cloned = replace(self.interpretation, context=context)
        assert cloned.context == context
        assert cloned.context is not context
        assert dict(cloned.context.namespaces) == {"p": "urn:p"}

    def test_repr_names_identifiers_not_decoded_objects(self):
        assert repr(self.interpretation) == (
            "ListInterpretation(lexical='12 abc', "
            f"candidate_id={self.interpretation.candidate_id!r}, "
            "shape='tuple', "
            "token_candidate_ids="
            f"{tuple(tuple(c.candidate_id for c in group) for group in self.interpretation.token_candidates)!r}, "
            "revision=1)"
        )

    @pytest.mark.parametrize(
        ("field", "value", "error", "match"),
        [
            ("lexical", 12, TypeError, "lexical"),
            ("token_spans", ("12",), TypeError, "TokenSpan"),
            ("context", object(), TypeError, "context"),
            ("grammar_fingerprint", "", ValueError, "grammar"),
            ("revision", True, ValueError, "revision"),
            ("contract", scalar_contract(Data(INTEGER)), ValueError, "list"),
            ("contract", object(), TypeError, "contract"),
            ("candidate_id", "", ValueError, "candidate ID"),
            ("shape", "unknown", ValueError, "shape"),
            ("constraints", object(), TypeError, "constraint"),
            ("_static_decodable", "yes", TypeError, "decoder eligibility"),
            ("token_candidates", ("12",), TypeError, "candidates"),
            ("token_candidates", ((), ()), ValueError, "candidate"),
            (
                "token_candidates",
                (
                    (
                        Candidate("a", scalar_contract(List(Data(INTEGER))), (1, "a"), True),
                        Candidate("a", scalar_contract(List(Data(INTEGER))), (1, "a"), True),
                    ),
                    (Candidate("c", scalar_contract(List(Data(INTEGER))), (2,), True),),
                ),
                ValueError,
                "IDs",
            ),
            (
                "token_candidates",
                (
                    (
                        Candidate("a", scalar_contract(List(Data(INTEGER))), (1, "a"), True),
                        Candidate("b", scalar_contract(List(Data(INTEGER))), (1, "a"), True),
                    ),
                    (Candidate("c", scalar_contract(List(Data(INTEGER))), (2,), True),),
                ),
                ValueError,
                "contracts",
            ),
        ],
    )
    def test_invalid_fields_are_rejected(self, field, value, error, match):
        with pytest.raises(error, match=match):
            replace(self.interpretation, **{field: value})

    def test_mismatched_token_candidate_count_is_rejected(self):
        with pytest.raises(ValueError, match="one candidate"):
            replace(self.interpretation, token_candidates=())

    def test_a_non_candidate_group_member_is_rejected(self):
        with pytest.raises(TypeError, match="candidates"):
            replace(
                self.interpretation,
                token_candidates=(
                    (object(),),
                    self.interpretation.token_candidates[1],
                ),
            )

    def test_a_missing_witness_mapping_rejects_every_assignment(self):
        stripped = replace(self.interpretation, _witness_candidates={})
        with pytest.raises(ValueError, match="complete list derivation"):
            stripped.as_types(("xsd:integer", "token"))

    def test_interpretation_equality_ignores_proof_object_identity(self):
        graph, _, occurrence = build("<r>12 abc</r>", List(Group(Data(INTEGER), Data(TOKEN))))
        other = interpret(graph, occurrence)
        assert other == self.interpretation
        assert other is not self.interpretation
        assert self.interpretation != "12 abc"
        assert self.interpretation != object()


class TestAlternativeValueInterpretations:
    def setup_method(self):
        graph, start, occurrence = build("<r>12</r>", List(Choice(Data(INTEGER), Data(STRING))))
        witness = only_list_witness(graph, occurrence)
        self.graph = graph
        self.start = start
        self.interpretation = list_interpretation(
            witness, graph, grammar_fingerprint="grammar", revision=1
        )
        self.candidates = (
            Candidate(
                self.interpretation.candidate_id,
                self.interpretation.contract,
                None,
                False,
            ),
        )

    def wrapper(self, **kwargs):
        arguments = {
            "lexical": "12",
            "candidates": self.candidates,
            "context": CX,
            "grammar_fingerprint": "grammar",
            "revision": 1,
            "interpretations": (self.interpretation,),
        }
        arguments.update(kwargs)
        return AlternativeValue(**cast(Any, arguments))

    def test_valid_interpretations_are_sorted_and_copy_backed(self):
        value = self.wrapper()
        assert value.interpretations == (self.interpretation,)
        assert value.as_interpretation(self.interpretation.candidate_id) is (self.interpretation)

    @pytest.mark.parametrize(
        ("kwargs", "error", "match"),
        [
            ({"interpretations": (object(),)}, TypeError, "ListInterpretation"),
            (
                {"interpretations": ("interp", "interp")},
                TypeError,
                "ListInterpretation",
            ),
            ({"lexical": "13"}, ValueError, "lexical"),
            ({"grammar_fingerprint": "other"}, ValueError, "grammar"),
            ({"revision": 2}, ValueError, "revision"),
            ({"context": DatatypeContext("urn:b", {})}, ValueError, "context"),
        ],
    )
    def test_mismatched_interpretations_are_rejected(self, kwargs, error, match):
        with pytest.raises(error, match=match):
            self.wrapper(**kwargs)

    def test_candidate_must_match_the_interpretation(self):
        with pytest.raises(ValueError, match="candidate"):
            self.wrapper(
                candidates=(
                    Candidate(
                        "other",
                        self.interpretation.contract,
                        None,
                        False,
                    ),
                )
            )
        with pytest.raises(ValueError, match="contract"):
            self.wrapper(
                candidates=(
                    Candidate(
                        self.interpretation.candidate_id,
                        scalar_contract(Data(INTEGER)),
                        None,
                        False,
                    ),
                )
            )

    def test_duplicate_interpretation_ids_are_rejected(self):
        second = replace(self.interpretation)
        with pytest.raises(ValueError, match="unique"):
            self.wrapper(interpretations=(self.interpretation, second))

    def test_unknown_and_non_list_interpretations_are_reported(self):
        graph, _, occurrence = build("<r>12</r>", Data(INTEGER))
        (atomic_witness,) = graph.scalar_witnesses_at(occurrence)
        atomic = alternative_value_from_witnesses(
            (atomic_witness,),
            grammar_fingerprint="grammar",
            revision=1,
        )
        with pytest.raises(ValueError, match="no candidate"):
            self.wrapper().as_interpretation("missing")
        with pytest.raises(ValueError, match="not a list"):
            atomic.as_interpretation(atomic.candidates[0].candidate_id)

    def test_interpretations_require_matching_candidates(self):
        value = self.wrapper()
        other = self.interpretation
        assert value.as_interpretation(other.candidate_id) == other


class TestTokenConstraintGraph:
    def setup_method(self):
        graph, _, occurrence = build("<r>12</r>", List(Choice(Data(INTEGER), Data(STRING))))
        self.graph = graph
        self.occurrence = occurrence
        witness = only_list_witness(graph, occurrence)
        self.witness = witness
        self.constraints = graph.token_constraints(witness)

    def test_roots_are_required(self):
        with pytest.raises(ValueError, match="roots"):
            TokenConstraintGraph(
                self.constraints.site,
                (),
                self.constraints.nodes,
                self.constraints.edges,
                self.constraints.token_occurrences,
            )

    def test_matches_must_be_callable(self):
        with pytest.raises(TypeError, match="callable"):
            self.constraints.accepts(None)

    def test_only_matching_witnesses_survive(self):
        assert self.constraints.accepts(
            lambda witness: (
                isinstance(witness.pattern, List)
                or getattr(getattr(witness.pattern, "datatype", None), "type_name", None)
                == "integer"
            )
        )
        assert not self.constraints.accepts(
            lambda witness: (
                isinstance(witness.pattern, List)
                or getattr(getattr(witness.pattern, "datatype", None), "type_name", None)
                == "boolean"
            )
        )

    def test_an_unretained_witness_has_no_constraint_graph(self):
        absent = ScalarWitness(
            self.occurrence,
            List(Data(INTEGER)),
            "99",
            CX,
            (),
        )
        with pytest.raises(ValueError, match="retained"):
            self.graph.token_constraints(absent)

    def test_wrong_evidence_types_are_explicit(self):
        with pytest.raises(TypeError, match="ScalarWitness"):
            self.graph.token_constraints(object())
        with pytest.raises(TypeError, match="list"):
            self.graph.token_constraints(
                ScalarWitness(
                    self.occurrence,
                    Data(INTEGER),
                    "12",
                    CX,
                    (),
                )
            )


class TestListValueValidation:
    def setup_method(self):
        self.graph, self.start, self.occurrence = build(
            "<r>12 abc</r>", List(Group(Data(INTEGER), Data(TOKEN)))
        )
        self.witness = only_list_witness(self.graph, self.occurrence)
        self.contract = scalar_contract(self.start.pattern)

    def test_witnesses_must_be_whole_list_witnesses(self):
        with pytest.raises(TypeError, match="ScalarWitness"):
            list_value(
                (object(),),  # type: ignore[arg-type]
                self.graph,
                schema_contracts=(self.contract,),
                grammar_fingerprint="grammar",
                revision=1,
            )

    def test_at_least_one_witness_is_required(self):
        with pytest.raises(ValueError, match="witness"):
            list_value(
                (),
                self.graph,
                schema_contracts=(self.contract,),
                grammar_fingerprint="grammar",
                revision=1,
            )

    def test_schema_contracts_must_include_a_whole_list_contract(self):
        with pytest.raises(ValueError, match="contract"):
            list_value(
                (self.witness,),
                self.graph,
                schema_contracts=(),
                grammar_fingerprint="grammar",
                revision=1,
            )
        with pytest.raises(TypeError, match="ScalarContract"):
            list_value(
                (self.witness,),
                self.graph,
                schema_contracts=(object(),),  # type: ignore[arg-type]
                grammar_fingerprint="grammar",
                revision=1,
            )
        with pytest.raises(ValueError, match="whole-list"):
            list_value(
                (self.witness,),
                self.graph,
                schema_contracts=(scalar_contract(Data(INTEGER)),),
                grammar_fingerprint="grammar",
                revision=1,
            )

    def test_mixed_scalar_schema_contracts_are_accepted(self):
        value = list_value(
            (self.witness,),
            self.graph,
            schema_contracts=(
                self.contract,
                scalar_contract(Data(INTEGER)),
            ),
            grammar_fingerprint="grammar",
            revision=1,
        )
        assert isinstance(value, AlternativeValue)
        assert value.candidates[0].contract.kind == "list"

    def test_surviving_witnesses_must_belong_to_the_schema_contracts(self):
        with pytest.raises(ValueError, match="schema"):
            list_value(
                (self.witness,),
                self.graph,
                schema_contracts=(fixed_list_contract(Group(Data(INTEGER), Data(INTEGER))),),
                grammar_fingerprint="grammar",
                revision=1,
            )

    def test_non_list_witnesses_are_rejected(self):
        graph, _, occurrence = build("<r>12</r>", Data(INTEGER))
        (witness,) = graph.scalar_witnesses_at(occurrence)
        with pytest.raises(ValueError, match="list"):
            list_value(
                (witness,),
                graph,
                schema_contracts=(self.contract,),
                grammar_fingerprint="grammar",
                revision=1,
            )

    def test_graph_must_be_an_evidence_graph(self):
        with pytest.raises(TypeError, match="EvidenceGraph"):
            list_interpretation(
                self.witness,
                object(),  # type: ignore[arg-type]
                grammar_fingerprint="grammar",
                revision=1,
            )

    def test_list_interpretation_requires_a_witness(self):
        with pytest.raises(TypeError, match="ScalarWitness"):
            list_interpretation(
                object(),  # type: ignore[arg-type]
                self.graph,
                grammar_fingerprint="grammar",
                revision=1,
            )

    def test_list_value_requires_an_evidence_graph(self):
        with pytest.raises(TypeError, match="EvidenceGraph"):
            list_value(
                (self.witness,),
                object(),  # type: ignore[arg-type]
                schema_contracts=(self.contract,),
                grammar_fingerprint="grammar",
                revision=1,
            )

    def test_list_interpretation_requires_a_list_witness(self):
        graph, _, occurrence = build("<r>12</r>", Data(INTEGER))
        (witness,) = graph.scalar_witnesses_at(occurrence)
        with pytest.raises(TypeError, match="list"):
            list_interpretation(witness, graph, grammar_fingerprint="grammar", revision=1)

    def test_alternative_values_accept_only_list_interpretations(self):
        with pytest.raises(TypeError, match="ListInterpretation"):
            alternative_value_from_witnesses(
                (self.witness,),
                grammar_fingerprint="grammar",
                revision=1,
                list_interpretations=(object(),),  # type: ignore[arg-type]
            )
