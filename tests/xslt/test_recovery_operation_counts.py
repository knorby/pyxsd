"""Deterministic operation-count regression for the midpoint-recovery stage.

R1 recorded before-images for two operations that the recovery work could
silently make more expensive: candidate-key comparisons inside
``xsl:for-each-group`` and whole-tree ``PatternIndex`` work for
``strategy="forward"``.  Both fixtures and count seams below duplicate R1's
ignored evidence script exactly, so the assertion also runs in a clean
checkout where ``docs/superpowers`` is absent.
"""

from __future__ import annotations

import io
from typing import Any

from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.patterns import compile_pattern
from pyxsd.xslt.runtime import iteration
from pyxsd.xslt.runtime.templates import PatternIndex, TemplateRule

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

#: Number of distinct grouping keys, and therefore population items.
GROUP_SIZE = 200

#: R1 before-image for candidate-key comparisons (0 + 1 + ... + 199).
GROUPING_COMPARISONS = 19_900

#: Fixture shape for the forward-pattern measurement.
PATTERN_DEPTH = 10
PATTERN_WIDTH = 10

#: R1 before-image for whole-tree equivalent-expression evaluations.
FORWARD_EVALUATIONS = 279


def _grouping_input() -> bytes:
    items = "".join(f'<n k="k{index}"/>' for index in range(GROUP_SIZE))
    return f"<root>{items}</root>".encode()


def _grouping_stylesheet() -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:template match="/">'
        '<xsl:for-each-group select="root/n" group-by="@k">'
        '<g key="{current-grouping-key()}">'
        "<xsl:value-of select=\"string-length(string-join(current-group()/@k, ''))\"/>"
        "</g>"
        "</xsl:for-each-group>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    ).encode()


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
    """Grouping and forward-pattern work match R1's recorded before-images."""
    calls = 0
    original = iteration.deep_equal_sequences

    def counted(left: Any, right: Any, collation: Any) -> bool:
        nonlocal calls
        calls += 1
        return original(left, right, collation)

    iteration.deep_equal_sequences = counted
    try:
        compiled = XsltProcessor().compile(io.BytesIO(_grouping_stylesheet()))
        result = compiled.transform(
            source=io.BytesIO(_grouping_input()),
            delivery="document",
        )
    finally:
        iteration.deep_equal_sequences = original

    groups = list(result.principal_entry.tree.children)
    assert len(groups) == GROUP_SIZE
    assert calls == GROUPING_COMPARISONS

    context = StaticContext(version="3.0")
    rules = (
        TemplateRule(compile_pattern("root//b", context), declaration_order=0),
        TemplateRule(compile_pattern("a/b[@id='3-3']", context), declaration_order=1),
        TemplateRule(compile_pattern("root/a[@name='a9']/b", context), declaration_order=2),
    )
    index = PatternIndex(rules, strategy="forward")
    environment = ExpressionEnvironment()
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
