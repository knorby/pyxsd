"""Set semantics for RELAX NG expanded-name classes.

RELAX NG name classes denote sets of expanded names.  Membership and the
symmetric overlap check used by the schema correctness passes must honour
namespace wildcards and exclusions rather than comparing literal syntax.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pyxsd.relaxng import names as names_module
from pyxsd.relaxng.names import (
    AnyName,
    ExceptName,
    Name,
    NameChoice,
    NameClass,
    NsName,
)

NS_A = "urn:example:a"
NS_B = "urn:example:b"


class TestName:
    def test_exact_expanded_name_matches(self):
        name = Name(NS_A, "item")
        assert name.contains(NS_A, "item")
        assert not name.contains(NS_A, "other")
        assert not name.contains(NS_B, "item")

    def test_absent_and_empty_namespace_are_the_same_name(self):
        assert Name(None, "item") == Name("", "item")
        assert Name(None, "item").contains("", "item")
        assert Name("", "item").contains(None, "item")

    def test_an_empty_local_name_is_not_a_wildcard(self):
        name = Name(None, "")
        assert name.contains(None, "")
        assert not name.contains(None, "item")
        assert not name.contains(NS_A, "")

    def test_namespaced_and_no_namespace_names_differ(self):
        assert not Name(None, "item").contains(NS_A, "item")
        assert not Name(NS_A, "item").contains(None, "item")

    def test_names_are_immutable(self):
        with pytest.raises(FrozenInstanceError):
            Name(None, "item").local = "other"


class TestWildcards:
    def test_any_name_contains_every_name(self):
        any_name = AnyName()
        for uri, local in [(None, "item"), ("", ""), (NS_A, "x"), (NS_A, "")]:
            assert any_name.contains(uri, local)

    def test_any_name_instances_are_equal(self):
        assert AnyName() == AnyName()

    def test_ns_name_matches_only_its_namespace(self):
        wildcard = NsName(NS_A)
        assert wildcard.contains(NS_A, "anything")
        assert wildcard.contains(NS_A, "")
        assert not wildcard.contains(NS_B, "anything")
        assert not wildcard.contains(None, "anything")

    def test_ns_name_without_a_namespace_matches_the_no_namespace(self):
        wildcard = NsName(None)
        assert wildcard.contains(None, "item")
        assert wildcard.contains("", "item")
        assert not wildcard.contains(NS_A, "item")
        assert NsName("") == NsName(None)


class TestNameChoice:
    def test_choice_is_the_membership_union(self):
        name_class = NameChoice((Name(None, "a"), NsName(NS_A)))
        assert name_class.contains(None, "a")
        assert name_class.contains(NS_A, "b")
        assert not name_class.contains(None, "b")
        assert not name_class.contains(NS_B, "b")

    def test_an_empty_choice_matches_nothing(self):
        empty = NameChoice(())
        assert not empty.contains(None, "a")
        assert not empty.contains(NS_A, "a")

    def test_alternatives_are_an_immutable_tuple(self):
        alternative = Name(None, "a")
        name_class = NameChoice([alternative])
        assert name_class.alternatives == (alternative,)
        with pytest.raises(FrozenInstanceError):
            name_class.alternatives = ()


class TestExceptName:
    def test_any_name_except_a_name(self):
        excluded = ExceptName(AnyName(), Name(NS_A, "a"))
        assert excluded.contains(NS_A, "b")
        assert excluded.contains(NS_B, "a")
        assert excluded.contains(None, "a")
        assert not excluded.contains(NS_A, "a")

    def test_ns_name_except_is_limited_to_its_base_namespace(self):
        excluded = ExceptName(NsName(NS_A), Name(NS_A, "a"))
        assert excluded.contains(NS_A, "b")
        assert not excluded.contains(NS_A, "a")
        assert not excluded.contains(NS_B, "b")
        assert not excluded.contains(None, "a")


OVERLAP_CASES = [
    (Name(None, "a"), Name(None, "a"), True),
    (Name(None, "a"), Name("", "a"), True),
    (Name(None, "a"), Name(None, "b"), False),
    (Name(None, "a"), Name(NS_A, "a"), False),
    (Name(NS_A, ""), Name(NS_A, ""), True),
    (Name(NS_A, ""), Name(NS_A, "a"), False),
    (Name(None, "a"), AnyName(), True),
    (Name(None, "a"), NsName(None), True),
    (Name(NS_A, "a"), NsName(NS_A), True),
    (Name(NS_A, "a"), NsName(NS_B), False),
    (NsName(NS_A), NsName(NS_A), True),
    (NsName(NS_A), NsName(NS_B), False),
    (NsName(None), NsName(None), True),
    (NsName(None), NsName(NS_A), False),
    (AnyName(), AnyName(), True),
    (AnyName(), NameChoice(()), False),
    (Name(None, "a"), NameChoice((Name(None, "b"), Name(NS_A, "a"))), False),
    (Name(None, "a"), NameChoice((Name(None, "a"), Name(NS_A, "a"))), True),
]


@pytest.mark.parametrize(("left", "right", "expected"), OVERLAP_CASES)
def test_overlap_is_symmetric(left, right, expected):
    assert left.overlaps(right) is expected
    assert right.overlaps(left) is expected


class TestOverlapWithExclusions:
    def test_any_name_except_a_name_excludes_only_that_name(self):
        excluded = ExceptName(AnyName(), Name(NS_A, "a"))
        assert not excluded.overlaps(Name(NS_A, "a"))
        assert excluded.overlaps(Name(NS_A, "b"))
        assert excluded.overlaps(Name(NS_B, "a"))
        assert excluded.overlaps(Name(None, "a"))

    def test_any_name_except_a_namespace_excludes_that_namespace(self):
        excluded = ExceptName(AnyName(), NsName(NS_A))
        assert not excluded.overlaps(NsName(NS_A))
        assert not excluded.overlaps(Name(NS_A, "a"))
        assert excluded.overlaps(NsName(NS_B))
        assert excluded.overlaps(Name(None, "a"))
        assert excluded.overlaps(AnyName())

    def test_any_name_except_a_choice_of_namespace_and_name(self):
        excluded = ExceptName(
            AnyName(),
            NameChoice((NsName(NS_A), Name(None, "a"))),
        )
        assert not excluded.overlaps(NsName(NS_A))
        assert not excluded.overlaps(Name(NS_A, "anything"))
        assert not excluded.overlaps(Name(None, "a"))
        assert excluded.overlaps(NsName(NS_B))
        assert excluded.overlaps(Name(NS_B, "a"))
        assert excluded.overlaps(Name(None, "b"))

    def test_ns_name_except_a_name(self):
        excluded = ExceptName(NsName(NS_A), Name(NS_A, "a"))
        assert not excluded.overlaps(Name(NS_A, "a"))
        assert excluded.overlaps(Name(NS_A, "b"))
        assert not excluded.overlaps(Name(NS_B, "b"))
        assert not excluded.overlaps(Name(None, "b"))

    def test_two_exclusions_can_still_overlap(self):
        left = ExceptName(AnyName(), Name(None, "a"))
        right = ExceptName(AnyName(), Name(None, "b"))
        assert left.overlaps(right)
        assert right.overlaps(left)

    def test_a_double_exclusion_is_the_excluded_name(self):
        double = ExceptName(AnyName(), ExceptName(AnyName(), Name(None, "a")))
        assert double.overlaps(Name(None, "a"))
        assert not double.overlaps(Name(None, "b"))
        assert not double.overlaps(Name(NS_A, "a"))
        assert double.overlaps(AnyName())

    def test_an_exclusion_does_not_follow_the_fresh_candidate_names(self):
        # The overlap decision uses representative names.  A schema that
        # mentions the generator's own seeds must still receive a correct
        # answer, which requires the generator to move past them.
        excluded = NameChoice(
            (NsName(names_module._FRESH_NAMESPACE), Name(None, names_module._FRESH_LOCAL))
        )
        outer = ExceptName(AnyName(), excluded)
        assert outer.overlaps(AnyName())
        assert AnyName().overlaps(outer)


class TestAbstractContract:
    def test_the_base_name_class_requires_membership(self):
        with pytest.raises(NotImplementedError):
            NameClass().contains(None, "a")

    def test_an_unknown_name_class_is_rejected(self):
        class Unknown(NameClass):
            def contains(self, uri, local):
                return True

        with pytest.raises(TypeError):
            Unknown().overlaps(Name(None, "a"))
