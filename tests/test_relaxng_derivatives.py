"""Text, datatype, and list derivatives for the RELAX NG validator.

The derivative algebra follows James Clark's algorithm.  Text derivatives
depend on the instance lexical value, the instance datatype context, the
pattern (which carries datatype identity and any residual continuation), so
those memo tables are parse-local.  Lists split on the four XML whitespace
characters only and retain exact source offsets.
"""

from __future__ import annotations

import pytest

from pyxsd.relaxng import patterns as pattern_module
from pyxsd.relaxng import validator as validator_module
from pyxsd.relaxng.datatypes import (
    BuiltinDatatypeFactory,
    Datatype,
    DatatypeContext,
    XsdDatatypeFactory,
)
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.names import Name
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
    choice,
    group,
    interleave,
    nullable,
    one_or_more,
)
from pyxsd.relaxng.validator import (
    DerivationContext,
    TokenSpan,
    _After,
    _after,
    list_derivative,
    text_derivative,
    xml_whitespace_tokens,
)

BUILTIN = BuiltinDatatypeFactory()
XSD = XsdDatatypeFactory()
EMPTY_CONTEXT = DatatypeContext(None, {})


def builtin(type_name: str):
    """Compile one built-in library datatype in an empty context."""
    return BUILTIN.create(type_name, (), EMPTY_CONTEXT)


def xsd(type_name: str, context: DatatypeContext = EMPTY_CONTEXT):
    """Compile one parameterless XSD datatype in *context*."""
    return XSD.create(type_name, (), context)


STRING = builtin("string")
TOKEN = builtin("token")
INTEGER = xsd("integer")


def make_context(
    *,
    datatype_context: DatatypeContext = EMPTY_CONTEXT,
    pool: PatternPool | None = None,
    limits: RelaxNGLimits | None = None,
) -> DerivationContext:
    """Build one parse-local derivative context."""
    return DerivationContext(
        datatype_context,
        PatternPool() if pool is None else pool,
        limits,
    )


class RecordingDatatype(Datatype):
    """A custom datatype that records the contexts supplied to ``equal``."""

    def __init__(self, context: DatatypeContext | None = None) -> None:
        if context is not None:
            self.context = context
        self.calls: list[tuple[str, DatatypeContext, str, DatatypeContext]] = []

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        del context
        return lexical != "reject"

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        self.calls.append((left, left_context, right, right_context))
        return left == right


class TestXmlWhitespaceTokens:
    def test_empty_and_whitespace_only_text_has_no_tokens(self):
        assert xml_whitespace_tokens("") == ()
        assert xml_whitespace_tokens(" \t\r\n ") == ()

    def test_tokens_retain_exact_start_and_end_offsets(self):
        assert xml_whitespace_tokens("  a\tb\n\ncd ") == (
            TokenSpan("a", 2, 3),
            TokenSpan("b", 4, 5),
            TokenSpan("cd", 7, 9),
        )
        assert isinstance(xml_whitespace_tokens("a"), tuple)

    def test_non_xml_unicode_whitespace_is_token_content(self):
        lexical = "a\u00a0b c\u2028d"
        assert xml_whitespace_tokens(lexical) == (
            TokenSpan("a\u00a0b", 0, 3),
            TokenSpan("c\u2028d", 4, 7),
        )
        # Python's Unicode-aware whitespace must never split these tokens.
        assert xml_whitespace_tokens("\x0ba\x0c") == (TokenSpan("\x0ba\x0c", 0, 3),)

    def test_lexical_text_must_be_a_string(self):
        with pytest.raises(TypeError, match="string"):
            xml_whitespace_tokens(b"bytes")  # type: ignore[arg-type]


class TestAfterContinuation:
    def test_after_is_private_residual_state(self):
        assert "After" not in pattern_module.__all__
        assert "_After" not in vars(pattern_module)
        assert "_After" not in validator_module.__all__
        assert "_after" not in validator_module.__all__

    def test_after_has_deterministic_value_semantics(self):
        first = _After(Text(), Empty())
        second = _After(Text(), Empty())
        assert first == second
        assert first == first
        assert hash(first) == hash(second)
        assert repr(first) == "After(first=Text(), second=Empty())"
        assert first != _After(Empty(), Empty())
        assert first != Text()
        assert len({first, second, _After(Empty(), Empty())}) == 2

    def test_after_hash_reuses_a_cached_nested_continuation(self):
        inner = _After(Text(), Empty())
        hash(inner)
        outer = _After(Data(STRING), inner)
        assert hash(outer) == hash(_After(Data(STRING), _After(Text(), Empty())))
        assert hash(outer) == hash(outer)
        assert outer == outer

    def test_after_annihilates_not_allowed(self):
        assert _after(NotAllowed(), Text()) == NotAllowed()
        assert _after(Text(), NotAllowed()) == NotAllowed()
        assert _after(Empty(), Empty()) == _After(Empty(), Empty())

    def test_after_derives_its_first_operand(self):
        context = make_context()
        assert text_derivative(_After(Text(), Empty()), "x", context) == _After(Text(), Empty())
        assert text_derivative(_After(Text(), Text()), "x", context) == _After(Text(), Text())
        assert text_derivative(_After(Empty(), Text()), "x", context) == NotAllowed()
        assert text_derivative(_After(Data(INTEGER), Text()), "12", context) == _After(
            Empty(), Text()
        )

    def test_after_continuations_participate_in_memo_identity(self):
        context = make_context()
        first = _After(Text(), Empty())
        same = text_derivative(first, "x", context)
        assert same is text_derivative(first, "x", context)
        assert same is not text_derivative(_After(Text(), Text()), "x", context)


class TestPrimitiveDerivatives:
    def test_empty_and_not_allowed_do_not_match_a_text_node(self):
        context = make_context()
        assert text_derivative(Empty(), "", context) == NotAllowed()
        assert text_derivative(Empty(), "x", context) == NotAllowed()
        assert text_derivative(NotAllowed(), "x", context) == NotAllowed()

    def test_text_keeps_matching_character_data(self):
        context = make_context()
        assert text_derivative(Text(), "", context) == Text()
        assert nullable(text_derivative(Text(), "abc", context)) is True

    def test_element_and_attribute_patterns_do_not_match_text(self):
        context = make_context()
        assert text_derivative(Element(Name(None, "item"), Empty()), "x", context) == NotAllowed()
        assert text_derivative(Attribute(Name(None, "a"), Empty()), "x", context) == NotAllowed()

    def test_unknown_patterns_are_rejected(self):
        class Unknown(Pattern):
            pass

        with pytest.raises(TypeError, match="unknown RELAX NG pattern"):
            text_derivative(Unknown(), "x", make_context())

    def test_reference_edges_are_followed(self):
        reference = Ref()
        reference.bind(Text())
        assert text_derivative(reference, "x", make_context()) == Text()
        with pytest.raises(ValueError, match="unbound"):
            text_derivative(Ref(), "x", make_context())

    def test_lexical_text_must_be_a_string(self):
        with pytest.raises(TypeError, match="string"):
            text_derivative(Text(), b"bytes", make_context())  # type: ignore[arg-type]

    def test_nullable_is_the_phase_one_contract(self):
        assert validator_module.nullable is nullable
        assert nullable(Empty()) is True
        assert nullable(Text()) is True
        assert nullable(NotAllowed()) is False


class TestWeakMatchBoundaries:
    def test_text_derivative_is_strict_about_the_empty_text_node(self):
        context = make_context()
        assert nullable(Empty()) is True
        assert text_derivative(Empty(), "", context) == NotAllowed()
        assert nullable(text_derivative(Empty(), "", context)) is False
        assert text_derivative(Text(), "", context) == Text()
        assert nullable(text_derivative(Text(), "", context)) is True

    def test_whitespace_only_text_is_a_list_of_zero_tokens(self):
        context = make_context()
        assert text_derivative(List(Empty()), "", context) == Empty()
        assert text_derivative(List(Empty()), " \t\r\n", context) == Empty()
        assert text_derivative(List(Empty()), "x", context) == NotAllowed()
        assert text_derivative(List(Data(INTEGER)), "  ", context) == NotAllowed()

    def test_value_lexical_boundaries_follow_the_datatype(self):
        context = make_context()
        assert text_derivative(Value(STRING, "a"), "a", context) == Empty()
        assert text_derivative(Value(STRING, "a"), "a ", context) == NotAllowed()
        assert text_derivative(Value(STRING, ""), "", context) == Empty()
        assert text_derivative(Value(STRING, ""), " ", context) == NotAllowed()
        assert text_derivative(Value(TOKEN, "a b"), " a\tb ", context) == Empty()
        assert text_derivative(Value(TOKEN, "a b"), "a\u00a0b", context) == NotAllowed()


class TestChoiceDerivative:
    def test_both_branches_survive_independently(self):
        pattern = Choice(
            Group(Value(INTEGER, "1"), Text()),
            Group(Value(STRING, "1"), Empty()),
        )
        context = make_context()
        residual = text_derivative(pattern, "1", context)
        assert residual == choice(Text(), Empty())
        assert nullable(residual) is True
        assert text_derivative(pattern, "2", context) == NotAllowed()

    def test_choice_that_reduces_to_one_branch(self):
        context = make_context()
        assert text_derivative(Choice(Text(), Empty()), "x", context) == Text()
        assert text_derivative(Choice(Text(), Empty()), "", context) == Text()


class TestGroupDerivative:
    def test_non_nullable_first_operand_requires_the_second(self):
        pattern = Group(Data(INTEGER), Data(INTEGER))
        context = make_context()
        residual = text_derivative(pattern, "12", context)
        assert residual == Data(INTEGER)
        assert nullable(residual) is False

    def test_nullable_first_operand_can_leave_only_the_second(self):
        context = make_context()
        assert text_derivative(Group(Empty(), Data(INTEGER)), "12", context) == Empty()
        assert text_derivative(Group(Empty(), Data(INTEGER)), "x", context) == NotAllowed()

    def test_nullable_first_operand_keeps_both_residuals(self):
        pattern = Group(Text(), Data(INTEGER))
        context = make_context()
        residual = text_derivative(pattern, "12", context)
        assert residual == choice(group(Text(), Data(INTEGER)), Empty())
        assert nullable(residual) is True


class TestInterleaveDerivative:
    def test_both_operands_may_still_need_content(self):
        pattern = Interleave(Text(), Data(STRING))
        context = make_context()
        residual = text_derivative(pattern, "x", context)
        assert residual == choice(interleave(Text(), Data(STRING)), Text())
        assert nullable(residual) is True

    def test_a_nullable_operand_allows_the_other_side_to_accept(self):
        nullable_first = choice(Data(INTEGER), Empty())
        pattern = Interleave(nullable_first, Data(STRING))
        context = make_context()
        residual = text_derivative(pattern, "x", context)
        assert residual == nullable_first
        assert nullable(residual) is True

    def test_both_operands_can_derive(self):
        nullable_first = choice(Data(INTEGER), Empty())
        pattern = Interleave(nullable_first, Text())
        context = make_context()
        residual = text_derivative(pattern, "1", context)
        assert residual == choice(Text(), interleave(nullable_first, Text()))
        assert nullable(residual) is True

    def test_interleave_rejects_when_neither_side_matches(self):
        context = make_context()
        assert (
            text_derivative(Interleave(Data(INTEGER), Data(INTEGER)), "x", context) == NotAllowed()
        )


class TestOneOrMoreDerivative:
    def test_one_or_more_derives_as_group_with_optional_repetition(self):
        pattern = OneOrMore(Data(INTEGER))
        context = make_context()
        residual = text_derivative(pattern, "1", context)
        assert residual == choice(OneOrMore(Data(INTEGER)), Empty())
        assert nullable(residual) is True
        assert text_derivative(pattern, "x", context) == NotAllowed()

    def test_one_or_more_text_remains_repeatable(self):
        pattern = OneOrMore(Text())
        context = make_context()
        residual = text_derivative(pattern, "x", context)
        assert residual == group(Text(), choice(one_or_more(Text()), Empty()))
        assert nullable(residual) is True


class TestDataDerivative:
    def test_data_accepts_and_rejects_lexical_values(self):
        context = make_context()
        assert text_derivative(Data(INTEGER), "12", context) == Empty()
        assert text_derivative(Data(INTEGER), "12.0", context) == NotAllowed()
        assert text_derivative(Data(INTEGER), " 12 ", context) == Empty()
        assert text_derivative(Data(INTEGER), "\u00a012", context) == NotAllowed()

    def test_data_resolves_context_sensitive_lexical_values(self):
        schema_context = DatatypeContext("urn:schema", {"p": "urn:one"})
        qname = xsd("QName", schema_context)
        one = make_context(datatype_context=DatatypeContext("urn:one", {"q": "urn:one"}))
        two = make_context(datatype_context=DatatypeContext("urn:two", {"r": "urn:two"}))
        assert text_derivative(Data(qname), "q:item", one) == Empty()
        assert text_derivative(Data(qname), "q:item", two) == NotAllowed()


class TestDataExceptDerivative:
    def test_exclusion_uses_value_equality_not_lexical_identity(self):
        pattern = DataExcept(INTEGER, Value(INTEGER, "05"))
        context = make_context()
        assert text_derivative(pattern, "5", context) == NotAllowed()
        assert text_derivative(pattern, "6", context) == Empty()

    def test_exclusion_uses_the_full_excluded_pattern_semantics(self):
        context = make_context()
        assert text_derivative(DataExcept(INTEGER, Data(INTEGER)), "5", context) == NotAllowed()
        assert text_derivative(DataExcept(INTEGER, Text()), "5", context) == NotAllowed()
        assert (
            text_derivative(
                DataExcept(INTEGER, Group(Value(INTEGER, "5"), Value(INTEGER, "6"))),
                "5",
                context,
            )
            == Empty()
        )

    def test_exclusion_can_be_a_list_pattern(self):
        pattern = DataExcept(STRING, List(Value(STRING, "a")))
        context = make_context()
        assert text_derivative(pattern, "a", context) == NotAllowed()
        assert text_derivative(pattern, "a b", context) == Empty()

    def test_disallowed_values_never_reach_the_excluded_pattern(self):
        datatype = RecordingDatatype()
        pattern = DataExcept(datatype, Text())
        assert text_derivative(pattern, "reject", make_context()) == NotAllowed()
        assert datatype.calls == []

    def test_exclusion_resolves_qnames_in_both_contexts(self):
        schema_context = DatatypeContext("urn:schema", {"p": "urn:one"})
        qname = xsd("QName", schema_context)
        pattern = DataExcept(qname, Value(qname, "p:item"))
        one = make_context(datatype_context=DatatypeContext(None, {"q": "urn:one"}))
        two = make_context(datatype_context=DatatypeContext(None, {"q": "urn:two"}))
        assert text_derivative(pattern, "q:item", one) == NotAllowed()
        assert text_derivative(pattern, "q:item", two) == Empty()


class TestValueDerivative:
    def test_equality_uses_the_schema_side_and_instance_side_contexts(self):
        schema_context = DatatypeContext("urn:schema", {"p": "urn:one"})
        qname = xsd("QName", schema_context)
        pattern = Value(qname, "p:item")
        one = make_context(datatype_context=DatatypeContext(None, {"q": "urn:one"}))
        two = make_context(datatype_context=DatatypeContext(None, {"q": "urn:two"}))
        assert text_derivative(pattern, "q:item", one) == Empty()
        assert text_derivative(pattern, "q:item", two) == NotAllowed()

    def test_value_uses_datatype_value_identity(self):
        context = make_context()
        assert text_derivative(Value(INTEGER, "05"), "5", context) == Empty()
        assert text_derivative(Value(INTEGER, "05"), "6", context) == NotAllowed()

    def test_schema_side_context_comes_from_the_compiled_datatype(self):
        schema_context = DatatypeContext("urn:schema", {"p": "urn:one"})
        instance_context = DatatypeContext("urn:instance", {"q": "urn:two"})
        datatype = RecordingDatatype(schema_context)
        context = make_context(datatype_context=instance_context)
        assert text_derivative(Value(datatype, "a"), "a", context) == Empty()
        assert datatype.calls == [("a", schema_context, "a", instance_context)]
        assert text_derivative(Value(datatype, "a"), "b", context) == NotAllowed()
        assert datatype.calls[-1] == ("a", schema_context, "b", instance_context)

    def test_a_datatype_without_context_is_interpreted_in_an_empty_schema_context(self):
        instance_context = DatatypeContext("urn:instance", {"q": "urn:two"})
        datatype = RecordingDatatype()
        context = make_context(datatype_context=instance_context)
        assert text_derivative(Value(datatype, "a"), "a", context) == Empty()
        assert datatype.calls == [("a", EMPTY_CONTEXT, "a", instance_context)]

    def test_a_non_context_attribute_is_not_mistaken_for_a_datatype_context(self):
        datatype = RecordingDatatype()
        datatype.context = "not a context"  # type: ignore[assignment]
        context = make_context()
        assert text_derivative(Value(datatype, "a"), "a", context) == Empty()
        assert datatype.calls == [("a", EMPTY_CONTEXT, "a", EMPTY_CONTEXT)]


class TestListDerivatives:
    def test_list_derivative_folds_tokens_in_order(self):
        pattern = Group(Data(INTEGER), Data(INTEGER))
        context = make_context()
        assert list_derivative(pattern, xml_whitespace_tokens(" 1\t2 "), context) == Empty()
        assert list_derivative(pattern, xml_whitespace_tokens("1"), context) == Data(INTEGER)
        assert list_derivative(pattern, xml_whitespace_tokens("1 x"), context) == NotAllowed()

    def test_empty_token_sequence_returns_the_original_pattern_without_work(self):
        pattern = Group(Data(INTEGER), Data(INTEGER))
        context = make_context(limits=RelaxNGLimits(max_derivative_states=0, max_memo_entries=0))
        assert list_derivative(pattern, (), context) is pattern

    def test_list_derivative_memoizes_semantic_token_text(self):
        pattern = Value(STRING, "1")
        context = make_context()
        first = list_derivative(pattern, (TokenSpan("1", 0, 1),), context)
        second = list_derivative(pattern, (TokenSpan("1", 40, 41),), context)
        assert first is second
        assert first == Empty()
        assert list_derivative(pattern, (TokenSpan("2", 0, 1),), context) == NotAllowed()

    def test_list_derivative_requires_token_spans(self):
        with pytest.raises(TypeError, match="TokenSpan"):
            list_derivative(Data(INTEGER), ["1"], make_context())  # type: ignore[list-item]

    def test_list_derivative_requires_string_token_text(self):
        with pytest.raises(TypeError, match="string"):
            list_derivative(Data(INTEGER), (TokenSpan(1, 0, 1),), make_context())  # type: ignore[arg-type]

    def test_text_derivative_of_a_list_uses_zero_or_more_tokens(self):
        context = make_context()
        assert text_derivative(List(Data(INTEGER)), "1", context) == Empty()
        assert text_derivative(List(Data(INTEGER)), "1 2", context) == NotAllowed()
        assert text_derivative(List(Data(INTEGER)), "", context) == NotAllowed()

    def test_list_content_can_be_arbitrary_patterns(self):
        pattern = List(Group(Value(STRING, "x"), OneOrMore(Data(INTEGER))))
        context = make_context()
        assert text_derivative(pattern, "x 1 2", context) == Empty()
        assert text_derivative(pattern, "x", context) == NotAllowed()
        assert text_derivative(pattern, "x y", context) == NotAllowed()
        assert text_derivative(pattern, "y 1", context) == NotAllowed()

    def test_list_content_can_choose_between_branches(self):
        pattern = List(
            Choice(
                Group(Value(STRING, "x"), Data(INTEGER)),
                Group(Data(INTEGER), Value(STRING, "x")),
            )
        )
        context = make_context()
        assert text_derivative(pattern, "x 3", context) == Empty()
        assert text_derivative(pattern, "3 x", context) == Empty()
        assert text_derivative(pattern, "3 y", context) == NotAllowed()

    def test_list_tokenization_uses_only_xml_whitespace(self):
        pattern = List(Group(Value(STRING, "a\u00a0b"), Value(STRING, "c")))
        context = make_context()
        assert text_derivative(pattern, "a\u00a0b c", context) == Empty()
        assert text_derivative(pattern, "a b c", context) == NotAllowed()

    def test_list_matches_a_value_only_within_one_token(self):
        context = make_context()
        assert text_derivative(List(Value(STRING, "a b")), "a b", context) == NotAllowed()
        assert text_derivative(List(Value(STRING, "a")), "a", context) == Empty()


class TestRecursiveCycles:
    @staticmethod
    def recursive_text_grammar() -> Ref:
        reference = Ref()
        reference.bind(one_or_more(choice(Text(), Element(Name(None, "node"), reference))))
        return reference

    def test_derivatives_terminate_on_legal_recursive_cycles(self):
        pattern = self.recursive_text_grammar()
        context = make_context()
        assert nullable(pattern) is True
        assert nullable(text_derivative(pattern, "text", context)) is True
        assert nullable(list_derivative(pattern, xml_whitespace_tokens("one two three"), context))

    def test_recursive_derivatives_are_deterministic_across_contexts(self):
        pattern = self.recursive_text_grammar()
        first = list_derivative(pattern, xml_whitespace_tokens("one two"), make_context())
        second = list_derivative(pattern, xml_whitespace_tokens("one two"), make_context())
        assert first == second
        assert repr(first) == repr(second)


class TestMemoization:
    def test_memo_hits_are_canonical_and_do_not_consume_again(self):
        context = make_context(limits=RelaxNGLimits(max_derivative_states=1, max_memo_entries=2))
        first = text_derivative(Text(), "a", context)
        same = text_derivative(Text(), "a", context)
        other_lexical = text_derivative(Text(), "b", context)
        assert first is same
        assert other_lexical is first
        with pytest.raises(RelaxNGLimitError) as excinfo:
            text_derivative(Text(), "c", context)
        assert excinfo.value.budget == "max_memo_entries"

    def test_memo_keys_include_the_datatype_context(self):
        qname = xsd("QName", DatatypeContext("urn:schema", {"p": "urn:one"}))
        context = make_context(datatype_context=DatatypeContext("urn:instance", {"q": "urn:one"}))
        pattern = Data(qname)
        assert text_derivative(pattern, "q:item", context) == Empty()
        context.datatype_context = DatatypeContext("urn:instance", {"r": "urn:two"})
        assert text_derivative(pattern, "q:item", context) == NotAllowed()

    def test_memo_keys_include_the_instance_base_uri(self):
        class BaseAwareDatatype(Datatype):
            def allows(self, lexical: str, context: DatatypeContext) -> bool:
                del lexical
                return context.base_uri == "urn:allowed"

            def equal(
                self,
                left: str,
                left_context: DatatypeContext,
                right: str,
                right_context: DatatypeContext,
            ) -> bool:
                del left, left_context, right, right_context
                return False

        context = make_context(datatype_context=DatatypeContext("urn:allowed", {}))
        pattern = Data(BaseAwareDatatype())
        assert text_derivative(pattern, "value", context) == Empty()
        context.datatype_context = DatatypeContext("urn:other", {})
        assert text_derivative(pattern, "value", context) == NotAllowed()

    def test_list_memo_keys_include_the_datatype_context(self):
        qname = xsd("QName", DatatypeContext("urn:schema", {"p": "urn:one"}))
        context = make_context(datatype_context=DatatypeContext("urn:instance", {"q": "urn:one"}))
        pattern = List(Data(qname))
        assert text_derivative(pattern, "q:item", context) == Empty()
        context.datatype_context = DatatypeContext("urn:instance", {"r": "urn:two"})
        assert text_derivative(pattern, "q:item", context) == NotAllowed()

    def test_memo_keys_include_the_pattern_and_datatype_identity(self):
        context = make_context()
        assert text_derivative(Data(STRING), "x", context) == Empty()
        assert text_derivative(Data(INTEGER), "x", context) == NotAllowed()
        assert text_derivative(Text(), "x", context) == Text()

    def test_derivatives_do_not_enter_the_schema_side_pattern_pool(self):
        pool = PatternPool()
        context = make_context(pool=pool)
        pattern = List(Data(INTEGER))
        assert text_derivative(pattern, "1", context) == Empty()
        assert list_derivative(pattern, xml_whitespace_tokens("2"), context) == Empty()
        assert len(pool) == 0

    def test_list_memo_entries_are_counted_separately(self):
        context = make_context(limits=RelaxNGLimits(max_memo_entries=3))
        pattern = Data(INTEGER)
        assert list_derivative(pattern, xml_whitespace_tokens("1"), context) == Empty()
        with pytest.raises(RelaxNGLimitError) as excinfo:
            list_derivative(pattern, xml_whitespace_tokens("2"), context)
        assert excinfo.value.budget == "max_memo_entries"


class TestBudgets:
    def test_derivative_state_budget_is_typed_and_not_a_verdict(self):
        context = make_context(limits=RelaxNGLimits(max_derivative_states=0))
        with pytest.raises(RelaxNGLimitError) as excinfo:
            text_derivative(Text(), "a", context)
        error = excinfo.value
        assert (error.stage, error.budget, error.configured, error.observed) == (
            "validate",
            "max_derivative_states",
            0,
            1,
        )

    def test_memo_budget_is_independent_of_the_residual_budget(self):
        context = make_context(limits=RelaxNGLimits(max_derivative_states=5, max_memo_entries=0))
        with pytest.raises(RelaxNGLimitError) as excinfo:
            text_derivative(Text(), "a", context)
        assert (excinfo.value.budget, excinfo.value.observed) == ("max_memo_entries", 1)

    def test_residual_budget_is_independent_of_the_memo_budget(self):
        context = make_context(limits=RelaxNGLimits(max_derivative_states=0, max_memo_entries=5))
        with pytest.raises(RelaxNGLimitError) as excinfo:
            text_derivative(Text(), "a", context)
        assert (excinfo.value.budget, excinfo.value.observed) == (
            "max_derivative_states",
            1,
        )

    def test_distinct_semantic_residuals_are_counted_once(self):
        context = make_context(limits=RelaxNGLimits(max_derivative_states=1))
        assert text_derivative(Text(), "a", context) == Text()
        assert text_derivative(Text(), "b", context) == Text()
        with pytest.raises(RelaxNGLimitError) as excinfo:
            text_derivative(Empty(), "a", context)
        assert (excinfo.value.budget, excinfo.value.observed) == (
            "max_derivative_states",
            2,
        )

    def test_recursive_branch_work_is_visible_to_the_budget(self):
        context = make_context(limits=RelaxNGLimits(max_derivative_states=1))
        with pytest.raises(RelaxNGLimitError):
            text_derivative(Choice(Text(), Empty()), "a", context)

    def test_list_work_uses_the_same_budgets(self):
        context = make_context(limits=RelaxNGLimits(max_derivative_states=0))
        with pytest.raises(RelaxNGLimitError) as excinfo:
            list_derivative(Data(INTEGER), xml_whitespace_tokens("1"), context)
        assert excinfo.value.budget == "max_derivative_states"


class TestDerivationContextContract:
    def test_keeps_its_inputs(self):
        pool = PatternPool()
        limits = RelaxNGLimits(max_memo_entries=3)
        context = DerivationContext(EMPTY_CONTEXT, pool, limits)
        assert context.datatype_context is EMPTY_CONTEXT
        assert context.pool is pool
        assert context.limits is limits

    def test_rejects_a_non_datatype_context(self):
        with pytest.raises(TypeError, match="DatatypeContext"):
            DerivationContext(object(), PatternPool())  # type: ignore[arg-type]

    def test_rejects_a_non_pattern_pool(self):
        with pytest.raises(TypeError, match="PatternPool"):
            DerivationContext(EMPTY_CONTEXT, object())  # type: ignore[arg-type]

    def test_rejects_non_limit_limits(self):
        with pytest.raises(TypeError, match="RelaxNGLimits"):
            DerivationContext(EMPTY_CONTEXT, PatternPool(), object())  # type: ignore[arg-type]
