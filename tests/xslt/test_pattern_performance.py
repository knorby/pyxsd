"""Operation-count evidence for indexed pattern dispatch.

These tests never use elapsed time.  They count equivalent-expression
evaluations, ancestor steps, candidate filters, predicate evaluations, and
cache hits so an accidental whole-tree scan fails deterministically.
"""

from __future__ import annotations

import pytest

from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.values import XdmAtomic
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.patterns import PatternMatcher, compile_pattern
from pyxsd.xslt.runtime.templates import PatternIndex, TemplateRule


def _context() -> StaticContext:
    return StaticContext(version="3.0")


def _rule(pattern: str, order: int) -> TemplateRule:
    return TemplateRule(compile_pattern(pattern, _context()), declaration_order=order)


def _element_children(node: XdmNode) -> tuple[XdmNode, ...]:
    return tuple(child for child in node.children if child.kind is NodeKind.ELEMENT)


def test_common_dispatch_avoids_equivalent_expression_scans() -> None:
    document = XdmDocument.parse(b"<r><a><b><c/></b></a><target/><other/></r>")
    a = _element_children(document.root_element)[0]
    target = _element_children(document.root_element)[1]

    rules = [_rule(f"name{index}", index + 1) for index in range(200)]
    rules.append(_rule("a/b/c", len(rules) + 1))
    rules.append(_rule("target", 0))
    index = PatternIndex(rules)
    selected = index.select(target, environment=ExpressionEnvironment())

    assert selected is not None
    assert selected.pattern.source == "target"
    assert index.statistics.equivalent_expression_evaluations == 0
    assert index.statistics.candidate_filters >= 200
    assert index.statistics.candidate_tests == 1
    assert index.statistics.ancestor_steps == 0
    assert a is not None


def test_name_filters_precede_ancestor_walks() -> None:
    document = XdmDocument.parse(b"<r><a><b><c><d><e><f/></e></d></c></b></a></r>")
    a = _element_children(document.root_element)[0]
    leaf = a
    for _ in range(5):
        leaf = _element_children(leaf)[0]

    rules = [
        _rule("a/b/c/d/e/f", 0),
        _rule("wrong/one/two/three", 2),
        _rule("other//deep", 3),
    ]
    index = PatternIndex(rules)
    selected = index.select(leaf, environment=ExpressionEnvironment())

    assert selected is not None
    assert selected.pattern.source == "a/b/c/d/e/f"
    statistics = index.statistics
    assert statistics.equivalent_expression_evaluations == 0
    assert statistics.candidate_filters >= 1
    assert statistics.candidate_tests == 1
    assert statistics.ancestor_steps <= 6


def test_descendant_walk_is_bounded_by_tree_depth() -> None:
    depth = 30
    xml = "<root>" + "".join(f"<n{level}>" for level in range(depth))
    xml += "</" + "></".join(f"n{level}" for level in reversed(range(depth))) + "></root>"
    document = XdmDocument.parse(xml.encode("utf-8"))
    leaf = document.root_element
    for _ in range(depth):
        leaf = _element_children(leaf)[0]

    index = PatternIndex([_rule("root//leaf", 0), _rule("other//leaf", 1)])
    # The pattern names do not match; replace with the real one.
    real = PatternIndex([_rule(f"n0//n{depth - 1}", 0), _rule("other//leaf", 1)])
    selected = real.select(leaf, environment=ExpressionEnvironment())

    assert selected is not None
    assert selected.pattern.source == f"n0//n{depth - 1}"
    assert real.statistics.equivalent_expression_evaluations == 0
    assert real.statistics.ancestor_steps <= depth * 2 + 2
    assert index.statistics.equivalent_expression_evaluations == 0


def test_sibling_positional_focus_counts_only_predicate_work() -> None:
    document = XdmDocument.parse(b"<r><item/><item/><item/></r>")
    first, second, third = _element_children(document.root_element)
    index = PatternIndex([_rule("item[1]", 0)])
    environment = ExpressionEnvironment()

    assert index.select(first, environment=environment) is not None
    assert index.select(second, environment=environment) is None
    assert index.select(third, environment=environment) is None
    assert index.statistics.equivalent_expression_evaluations == 0
    assert index.statistics.predicate_evaluations == 9
    assert index.statistics.ancestor_steps == 0


def test_complex_pattern_memoizes_candidate_and_root_work() -> None:
    document = XdmDocument.parse(b"<r><a><b><c/><d/></b></a></r>")
    a = _element_children(document.root_element)[0]
    b = _element_children(a)[0]
    d = _element_children(b)[1]
    root = document.root_element

    index = PatternIndex([_rule("a//b/(c | d)", 0)])
    environment = ExpressionEnvironment()
    assert index.select(d, environment=environment) is not None
    assert index.select(d, environment=environment) is not None
    assert index.select(root, environment=environment) is None
    statistics = index.statistics
    assert statistics.equivalent_expression_evaluations == 1
    assert statistics.cache_hits >= 1
    assert statistics.cache_misses == 1


def test_forward_strategy_counts_whole_tree_work_explicitly() -> None:
    document = XdmDocument.parse(b"<r><a><b/></a><b/></r>")
    target = _element_children(_element_children(document.root_element)[0])[0]
    pattern = compile_pattern("a/b", _context())
    matcher = PatternMatcher(pattern, strategy="forward")
    environment = ExpressionEnvironment()

    assert matcher.matches(target, environment)
    assert matcher.statistics.equivalent_expression_evaluations == 1
    assert matcher.matches(target, environment)
    assert matcher.statistics.cache_hits == 1


def test_unknown_strategy_is_rejected() -> None:
    pattern = compile_pattern("a", _context())
    with pytest.raises(ValueError):
        PatternMatcher(pattern, strategy="magic")


def test_predicate_pattern_dispatch_does_not_scan_the_tree() -> None:
    item = XdmAtomic.integer(1)
    index = PatternIndex([_rule(".[. instance of xs:integer]", 0), _rule("target", 1)])
    assert index.select(item, environment=ExpressionEnvironment()) is not None
    assert index.statistics.equivalent_expression_evaluations == 0
    assert index.statistics.candidate_tests == 1
