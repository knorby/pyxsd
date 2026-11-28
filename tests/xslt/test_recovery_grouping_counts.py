"""Deterministic grouping operation counts for the midpoint-recovery stage.

R1 recorded a before-image for candidate-key comparisons inside
``xsl:for-each-group``, an operation the recovery work could silently make more
expensive.  The fixture and count seam below duplicate R1's ignored evidence
script exactly, so the assertion also runs in a clean checkout where
``docs/superpowers`` is absent.
"""

from __future__ import annotations

import io
from typing import Any

from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.runtime import iteration

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

#: Number of distinct grouping keys, and therefore population items.
GROUP_SIZE = 200

#: R1 before-image for candidate-key comparisons (0 + 1 + ... + 199).
GROUPING_COMPARISONS = 19_900


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


def test_recovery_operation_counts() -> None:
    """Grouping work matches R1's recorded before-image."""
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
