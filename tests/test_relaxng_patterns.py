"""Smart constructors, interning, and nullability for the RELAX NG pattern IR.

The pattern graph is immutable and interned by semantic identity.  Origins
live beside the semantic key, never inside it, and nullability terminates on
the legal recursive element graphs produced by reference resolution.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass

import pytest

from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
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
    choice,
    group,
    interleave,
    nullable,
    one_or_more,
)

ORIGIN_A = SourceOrigin("a.rng", None)
ORIGIN_B = SourceOrigin("b.rng", None)


@dataclass(frozen=True)
class StubDatatype:
    """A stand-in for the compiled datatype contract of a later stage."""

    name: str


STRING = StubDatatype("string")
INTEGER = StubDatatype("integer")


class TestConstructors:
    def test_every_constructor_is_a_pattern(self):
        patterns = [
            Empty(),
            NotAllowed(),
            Text(),
            Choice(Text(), Empty()),
            Group(Text(), Empty()),
            Interleave(Text(), Empty()),
            OneOrMore(Text()),
            Element(Name(None, "item"), Empty()),
            Attribute(Name(None, "attr"), Text()),
            List(Text()),
            Data(STRING),
            DataExcept(STRING, Text()),
            Value(STRING, "12"),
        ]
        assert all(isinstance(pattern, Pattern) for pattern in patterns)

    def test_pattern_nodes_are_immutable(self):
        element = Element(Name(None, "item"), Empty())
        with pytest.raises(FrozenInstanceError):
            element.pattern = Text()

    def test_value_retains_its_schema_lexical_value(self):
        value = Value(STRING, "12")
        assert value.datatype is STRING
        assert value.lexical == "12"

    def test_data_except_retains_the_excluded_pattern(self):
        excluded = Value(STRING, "x")
        pattern = DataExcept(STRING, excluded)
        assert pattern.datatype is STRING
        assert pattern.excluded is excluded


class TestSmartConstructors:
    def test_choice_drops_not_allowed_on_either_side(self):
        pattern = Text()
        assert choice(NotAllowed(), pattern) is pattern
        assert choice(pattern, NotAllowed()) is pattern

    def test_choice_of_only_not_allowed_is_not_allowed(self):
        assert choice(NotAllowed(), NotAllowed()) == NotAllowed()

    def test_choice_deduplicates_identical_leaves(self):
        pattern = Text()
        assert choice(pattern, pattern) is pattern
        assert choice(Empty(), Empty()) == Empty()

    def test_group_identities(self):
        pattern = Text()
        assert group(NotAllowed(), pattern) == NotAllowed()
        assert group(pattern, NotAllowed()) == NotAllowed()
        assert group(Empty(), pattern) is pattern
        assert group(pattern, Empty()) is pattern
        assert group(pattern, pattern) == Group(pattern, pattern)

    def test_interleave_identities(self):
        pattern = Text()
        assert interleave(NotAllowed(), pattern) == NotAllowed()
        assert interleave(pattern, NotAllowed()) == NotAllowed()
        assert interleave(Empty(), pattern) is pattern
        assert interleave(pattern, Empty()) is pattern
        assert interleave(pattern, pattern) == Interleave(pattern, pattern)

    def test_one_or_more_identities(self):
        assert one_or_more(NotAllowed()) == NotAllowed()
        assert one_or_more(Empty()) == Empty()
        pattern = Text()
        assert one_or_more(pattern) == OneOrMore(pattern)


class TestDeterministicChoiceOrder:
    def test_choice_order_is_independent_of_argument_order(self):
        assert choice(Text(), Empty()) == choice(Empty(), Text())

    def test_choice_leaves_are_ordered_canonically(self):
        result = choice(Text(), Empty())
        assert result == Choice(Empty(), Text())
        assert result.first == Empty()
        assert result.second == Text()

    def test_nested_choices_are_flattened(self):
        first, second, third = Empty(), Text(), Group(Text(), Empty())
        assert choice(choice(first, second), third) == choice(first, choice(second, third))

    def test_duplicate_leaves_are_removed_across_nesting(self):
        assert choice(choice(Text(), Empty()), Text()) == choice(Empty(), Text())

    def test_a_large_flat_choice_is_flattened_without_recursion(self):
        raw: Pattern = Text()
        for index in range(1500):
            raw = Choice(raw, Empty() if index % 2 else NotAllowed())
        result = choice(raw, Text())
        assert result == choice(Empty(), Text())
        assert nullable(result) is True


class TestInterning:
    @pytest.mark.parametrize(
        "build",
        [
            lambda: Interleave(Text(), Text()),
            lambda: OneOrMore(Text()),
            lambda: Element(Name(None, "item"), Text()),
            lambda: Attribute(Name(None, "attr"), Text()),
            lambda: List(Text()),
            lambda: Data(STRING),
            lambda: DataExcept(STRING, Text()),
            lambda: Value(STRING, "12"),
        ],
    )
    def test_every_node_shape_interns_by_semantics(self, build):
        pool = PatternPool()
        first = pool.intern(build())
        assert pool.intern(build()) is first

    def test_equal_structures_return_the_same_node(self):
        pool = PatternPool()
        first = pool.intern(Element(Name(None, "item"), Text()))
        second = pool.intern(Element(Name("", "item"), Text()))
        assert first is second

    def test_different_structures_return_different_nodes(self):
        pool = PatternPool()
        assert pool.intern(Text()) is not pool.intern(Empty())

    def test_interning_a_tree_interns_its_children(self):
        pool = PatternPool()
        parent = pool.intern(Group(Text(), Text()))
        assert isinstance(parent, Group)
        assert parent.first is pool.intern(Text())
        assert parent.second is parent.first
        assert len(pool) == 2

    def test_interning_applies_the_smart_identities(self):
        pool = PatternPool()
        content = pool.intern(Text())
        assert pool.intern(Group(Empty(), Text())) is content
        assert pool.intern(Choice(Text(), Text())) is content

    def test_len_counts_distinct_semantic_nodes(self):
        pool = PatternPool()
        pool.intern(Element(Name(None, "a"), Text()))
        assert len(pool) == 2
        pool.intern(Element(Name(None, "b"), Text()))
        assert len(pool) == 3
        pool.intern(Element(Name(None, "a"), Text()))
        assert len(pool) == 3


class TestOriginTables:
    def test_origins_are_unioned_beside_the_semantic_node(self):
        pool = PatternPool()
        first = pool.intern(Element(Name(None, "item"), Text()), OriginTable((ORIGIN_A,)))
        second = pool.intern(
            Element(Name("", "item"), Text()),
            OriginTable((ORIGIN_B,)),
        )
        assert first is second
        assert pool.origins(first) == OriginTable((ORIGIN_A, ORIGIN_B))

    def test_unrecorded_origins_are_empty(self):
        pool = PatternPool()
        assert pool.origins(pool.intern(Text())) == OriginTable()
        assert pool.origins(Group(Text(), Text())) == OriginTable()

    def test_interning_without_origins_keeps_existing_origins(self):
        pool = PatternPool()
        node = pool.intern(Text(), OriginTable((ORIGIN_A,)))
        assert pool.intern(Text()) is node
        assert pool.origins(node) == OriginTable((ORIGIN_A,))


class TestPatternBudget:
    def test_distinct_semantic_nodes_consume_the_pattern_budget(self):
        counter = BudgetCounter("compile", RelaxNGLimits(max_pattern_nodes=2))
        pool = PatternPool(counter)
        with pytest.raises(RelaxNGLimitError) as excinfo:
            pool.intern(Element(Name(None, "a"), Element(Name(None, "b"), Text())))
        assert excinfo.value.budget == "max_pattern_nodes"
        assert excinfo.value.configured == 2
        assert excinfo.value.observed == 3
        assert len(pool) == 2

    def test_cached_nodes_do_not_consume_the_budget_again(self):
        pool = PatternPool(BudgetCounter("compile", RelaxNGLimits(max_pattern_nodes=2)))
        pool.intern(Text())
        assert pool.intern(Text()) == Text()
        pool.intern(Empty())
        with pytest.raises(RelaxNGLimitError):
            pool.intern(Element(Name(None, "a"), Empty()))

    def test_a_pool_without_limits_is_unlimited(self):
        pool = PatternPool()
        for index in range(50):
            pool.intern(Element(Name(None, f"item{index}"), Text()))
        assert len(pool) == 51


NULLABLE_CASES = [
    (Empty(), True),
    (NotAllowed(), False),
    (Text(), True),
    (Element(Name(None, "item"), Empty()), False),
    (Attribute(Name(None, "attr"), Text()), False),
    (List(Empty()), False),
    (List(Text()), False),
    (Data(STRING), False),
    (DataExcept(STRING, Text()), False),
    (Value(STRING, "12"), False),
]


@pytest.mark.parametrize(("pattern", "expected"), NULLABLE_CASES)
def test_leaf_nullability(pattern, expected):
    assert nullable(pattern) is expected


class TestCompositeNullability:
    def test_choice_is_nullable_when_either_side_is(self):
        element = Element(Name(None, "item"), Empty())
        assert nullable(Choice(Empty(), Text()))
        assert nullable(Choice(NotAllowed(), Empty()))
        assert not nullable(Choice(NotAllowed(), element))
        assert not nullable(Choice(element, NotAllowed()))

    def test_group_requires_both_sides(self):
        assert nullable(Group(Empty(), Text()))
        assert not nullable(Group(Empty(), NotAllowed()))
        assert not nullable(Group(NotAllowed(), Empty()))

    def test_interleave_requires_both_sides(self):
        assert nullable(Interleave(Text(), Empty()))
        assert not nullable(Interleave(Empty(), NotAllowed()))
        assert not nullable(Interleave(NotAllowed(), Empty()))

    def test_one_or_more_follows_its_operand(self):
        assert nullable(OneOrMore(Empty()))
        assert nullable(OneOrMore(Text()))
        assert not nullable(OneOrMore(Element(Name(None, "item"), Empty())))


class TestRecursiveGraphs:
    def test_ref_to_element_to_ref_terminates(self):
        ref = Ref()
        element = Element(Name(None, "node"), ref)
        ref.bind(element)
        assert ref.target is element
        assert nullable(element) is False
        assert nullable(ref) is False

    def test_a_recursive_choice_can_be_nullable(self):
        ref = Ref()
        node = Choice(ref, Text())
        ref.bind(node)
        assert nullable(node) is True

    def test_a_reference_alias_of_a_nullable_cycle_is_nullable(self):
        # The fixed point must be computed for every node in the cycle, not
        # only for the node the first evaluation started from.
        ref = Ref()
        node = Choice(ref, Text())
        ref.bind(node)
        root = Group(node, ref)
        assert nullable(root) is True

    def test_a_pure_reference_cycle_is_not_nullable(self):
        ref = Ref()
        node = Choice(ref, ref)
        ref.bind(node)
        assert nullable(node) is False

    def test_a_recursive_group_is_not_nullable(self):
        ref = Ref()
        node = Group(ref, Text())
        ref.bind(node)
        assert nullable(node) is False

    def test_mutual_references_terminate(self):
        first = Ref()
        second = Ref()
        first.bind(second)
        second.bind(first)
        assert nullable(first) is False

    def test_a_reference_binds_once(self):
        ref = Ref()
        ref.bind(Empty())
        with pytest.raises(ValueError, match="already bound"):
            ref.bind(Text())
        assert ref.target == Empty()

    def test_an_unbound_reference_is_rejected(self):
        with pytest.raises(ValueError, match="unbound"):
            _ = Ref().target
        with pytest.raises(ValueError, match="unbound"):
            nullable(Ref())

    def test_reference_identity_is_object_identity(self):
        first = Ref()
        second = Ref()
        assert first == first
        assert first != second
        assert len({first, second}) == 2
        assert "Ref" in repr(first)

    def test_references_are_patterns(self):
        assert isinstance(Ref(), Pattern)


class TestDatatypePatterns:
    def test_equal_datatypes_intern_together(self):
        pool = PatternPool()
        first = pool.intern(Data(StubDatatype("string")))
        second = pool.intern(Data(StubDatatype("string")))
        assert first is second

    def test_different_datatypes_stay_distinct(self):
        pool = PatternPool()
        assert pool.intern(Data(STRING)) is not pool.intern(Data(INTEGER))

    def test_value_lexical_values_stay_distinct(self):
        pool = PatternPool()
        assert Value(STRING, "12") == Value(StubDatatype("string"), "12")
        assert pool.intern(Value(STRING, "12")) is not pool.intern(Value(STRING, "13"))


class TestUnknownPatterns:
    class Unknown(Pattern):
        pass

    def test_nullability_rejects_an_unknown_pattern(self):
        with pytest.raises(TypeError):
            nullable(self.Unknown())

    def test_interning_rejects_an_unknown_pattern(self):
        with pytest.raises(TypeError):
            PatternPool().intern(self.Unknown())
