"""Import precedence shared by XSLT compiler and runtime callers.

Import precedence is a tuple of integer steps.  A shorter prefix is higher
than a longer prefix that starts with it, because an included module keeps its
parent's precedence while an imported module appends a step.  Distinct steps
at the first difference follow declaration order: the later import has the
larger step and therefore higher precedence.
"""

from __future__ import annotations

__all__ = ["compare_precedence"]


def compare_precedence(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """Compare import precedence: a shorter prefix is higher; later imports win."""
    if left == right:
        return 0
    limit = min(len(left), len(right))
    if left[:limit] == right[:limit]:
        return 1 if len(left) < len(right) else -1
    return 1 if left > right else -1
