"""Named, applied, and functional tail chains at 10,000 steps.

The mixed-recursion proof drives the shared named stylesheets at a depth that
used to reach Python's recursion limit.  It lives in its own module so the
scheduling contract and this long probe are separate scheduling units.
"""

from __future__ import annotations

import sys

from tests.xslt.scheduler_support import run_stylesheet


def test_deep_mixed_recursion() -> None:
    recursion_limit = sys.getrecursionlimit()

    assert run_stylesheet("named-tail-10000.xsl") == "done"
    assert run_stylesheet("apply-tail-10000.xsl") == "done"
    assert run_stylesheet("xpath-function-tail-10000.xsl") == "done"
    assert run_stylesheet("xpath-higher-order-roundtrip.xsl") == "83"
    assert sys.getrecursionlimit() == recursion_limit
