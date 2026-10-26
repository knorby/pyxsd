"""Dependency-free, exact JSON-safe numeric diagnostic encoding."""

import math
from decimal import Decimal


def encode_number(value: int | float | Decimal) -> int | float | str:
    """Preserve numeric evidence without nonstandard JSON or lossy stringification."""
    if isinstance(value, int):
        return int(value) if abs(value) <= 2**53 - 1 else str(Decimal(value))
    if isinstance(value, Decimal):
        return str(value)
    if math.isfinite(value):
        return float(value)
    if math.isnan(value):
        return "NaN"
    return "INF" if value > 0 else "-INF"
