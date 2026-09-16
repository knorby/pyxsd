"""Direct unit tests for the path analysis internals of ``pyxsd.upa``.

The public sweep is covered through compiled models in ``test_upa.py``;
these pin the internal branches (empty compositors, blocked substitution
chains, missing descriptors, path separation) that the corpus does not
reach on its own.
"""

from pyxsd.content_model import Particle
from pyxsd.upa import (
    _distinguishable,
    _emptiable,
    _head_chain,
    _index_of,
    _overlap,
    _substitution_overlap,
    upa_violations,
)


def _elt(name):
    return Particle("element", 1, 1, [], name=name)


def _group(kind, *children, min_occurs=1, max_occurs=1):
    return Particle(kind, min_occurs, max_occurs, list(children))


def test_upa_violations_without_a_model_is_deterministic():
    assert upa_violations(None) == []


def test_emptiable_compositor_without_children_can_match_nothing():
    assert _emptiable(_group("choice", min_occurs=1, max_occurs=1))


def test_head_chain_of_a_missing_declaration_is_empty():
    assert _head_chain(None, lambda declaration: None) == set()


def test_substitution_overlap_without_descriptors_is_disjoint():
    assert not _substitution_overlap(_elt("a"), _elt("b"), lambda declaration: None)


def test_overlap_of_two_specless_wildcards_is_disjoint():
    spec_less = Particle("any", 1, 1, [])
    assert not _overlap(spec_less, spec_less, None)


def test_index_of_a_missing_child():
    assert _index_of([_elt("a")], _elt("b")) == -1


def test_distinguishable_when_the_roots_themselves_differ():
    root1 = _group("choice", _elt("a"))
    root2 = _group("choice", _elt("b"))
    assert _distinguishable([root1, _elt("a")], [root2, _elt("b")])


def test_distinguishable_when_the_shared_parent_cannot_repeat():
    root = _group("choice", _elt("a"), _elt("b"), max_occurs=0)
    assert _distinguishable([root, _elt("a")], [root, _elt("b")])


def test_distinguishable_reports_a_non_univocal_choice():
    leaf = _elt("x")
    branch = _group("choice", leaf, _group("choice", min_occurs=0, max_occurs=1))
    other = _group("choice", _elt("y"))
    root = _group("sequence", branch, other)
    assert not _distinguishable([root, branch, leaf], [root, other, _elt("y")])
