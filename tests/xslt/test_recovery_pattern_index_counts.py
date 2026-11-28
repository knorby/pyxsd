"""Deterministic forward-pattern index counts for the midpoint-recovery stage.

R1 recorded a before-image for whole-tree ``PatternIndex`` work under
``strategy="forward"``, an operation the recovery work could silently make more
expensive.  The fixture and count seam below duplicate R1's ignored evidence
script exactly, so the assertion also runs in a clean checkout where
``docs/superpowers`` is absent.
"""

from __future__ import annotations

from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xpath.engine import XPathParserCache
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.patterns import compile_pattern
from pyxsd.xslt.runtime.templates import PatternIndex, TemplateRule

#: Fixture shape for the forward-pattern measurement.
PATTERN_DEPTH = 10
PATTERN_WIDTH = 10

#: R1 before-image for whole-tree equivalent-expression evaluations.
FORWARD_EVALUATIONS = 279


def _pattern_input() -> XdmDocument:
    levels: list[str] = []
    for depth in range(PATTERN_DEPTH):
        leaves = "".join(f'<b id="{depth}-{width}"/>' for width in range(PATTERN_WIDTH))
        levels.append(f'<a name="a{depth}">{leaves}</a>')
    return XdmDocument.parse(f"<root>{''.join(levels)}</root>".encode())


def _element_children(node: XdmNode) -> tuple[XdmNode, ...]:
    return tuple(child for child in node.children if child.kind is NodeKind.ELEMENT)


def _all_elements(document: XdmDocument) -> tuple[XdmNode, ...]:
    pending = [document.root_element]
    elements: list[XdmNode] = []
    while pending:
        node = pending.pop()
        elements.append(node)
        pending.extend(reversed(_element_children(node)))
    return tuple(elements)


def test_recovery_operation_counts() -> None:
    """Forward-pattern work matches R1's recorded before-image."""
    context = StaticContext(version="3.0")
    rules = (
        TemplateRule(compile_pattern("root//b", context), declaration_order=0),
        TemplateRule(compile_pattern("a/b[@id='3-3']", context), declaration_order=1),
        TemplateRule(compile_pattern("root/a[@name='a9']/b", context), declaration_order=2),
    )
    index = PatternIndex(rules, strategy="forward")
    environment = ExpressionEnvironment(parser_cache=XPathParserCache())
    elements = _all_elements(_pattern_input())
    matches = sum(1 for element in elements if index.select(element, environment=environment))
    statistics = index.statistics

    assert len(elements) == 111
    assert len(rules) == 3
    assert matches == 100
    assert statistics.equivalent_expression_evaluations == FORWARD_EVALUATIONS
    assert statistics.candidate_tests == FORWARD_EVALUATIONS
    assert statistics.candidate_filters == 33
    assert statistics.predicate_evaluations == 10_000
    assert statistics.ancestor_steps == 0
    assert statistics.cache_hits == 0
    assert statistics.cache_misses == FORWARD_EVALUATIONS
    assert statistics.errors_ignored == 0
