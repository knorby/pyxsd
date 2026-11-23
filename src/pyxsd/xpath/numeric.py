"""F&O numeric function and operator rules for the XPath layer.

elementpath rounds through Python's ``round()`` (ties to even) for both
``fn:round`` and ``fn:round-half-to-even``, and computes ``mod`` with Python's
``%`` (whose remainder takes the divisor's sign).  F&O 3.1 requires ties toward
positive infinity for ``fn:round``, the dividend's sign for ``mod``, and exact
decimal arithmetic for both.  The overrides live on one parser instance and
keep the substrate's type promotion and error handling.
"""

from __future__ import annotations

import math
import struct
from decimal import ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from elementpath.datatypes import Float, NumericProxy, UntypedAtomic
from elementpath.xpath_nodes import XPathNode

__all__ = ["install_numeric_functions"]


#: A decimal context wide enough for the exact value and rounding position.
def _numeric_context(value: Decimal, precision: int) -> Any:
    digits = len(value.as_tuple().digits)
    return max(50, digits + abs(precision) + 20)


def _quantum(precision: int) -> Decimal:
    return Decimal(1).scaleb(-precision)


def _round_decimal_value(value: Decimal, precision: int, *, even: bool) -> Decimal:
    """Round a Decimal to *precision* fractional digits, exactly."""
    with localcontext() as context:
        context.prec = _numeric_context(value, precision)
        context.Emax = max(999999, abs(value.as_tuple().exponent) + abs(precision) + 20)
        context.Emin = -context.Emax
        if even:
            return value.quantize(_quantum(precision), rounding=ROUND_HALF_EVEN)
        scaled = value.scaleb(precision)
        rounded = (scaled + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR)
        return rounded.scaleb(-precision)


def _round_numeric(value: Any, precision: int, *, even: bool) -> Any:
    """Round one F&O numeric value, preserving its type."""
    if isinstance(value, bool):
        raise TypeError("xs:boolean is not numeric")
    if isinstance(value, int):
        if precision >= 0:
            return value
        return int(_round_decimal_value(Decimal(value), precision, even=even))
    if isinstance(value, Decimal):
        rounded = _round_decimal_value(value, precision, even=even)
        if rounded == 0 and value.is_signed():
            return Decimal("-0")
        return rounded
    if isinstance(value, float):
        if value == 0:
            return value
        single = _is_single_precision(value)
        exact = Decimal(_binary32(float(value)) if single else float(value))
        rounded = _round_decimal_value(exact, precision, even=even)
        if rounded == 0 and math.copysign(1.0, value) < 0:
            return Float(-0.0) if single else -0.0
        return Float(float(rounded)) if single else float(rounded)
    raise TypeError(f"{type(value).__name__} is not numeric")


def _is_single_precision(value: Any) -> bool:
    """Whether a numeric payload belongs to the binary32 value space."""
    return getattr(type(value), "name", None) == "float"


def _is_double_precision(value: Any) -> bool:
    """Whether a numeric payload belongs to the binary64 value space."""
    return isinstance(value, float) and not _is_single_precision(value)


def _binary32(value: float) -> float:
    """*value* rounded to the nearest IEEE binary32 value."""
    try:
        return struct.unpack(">f", struct.pack(">f", value))[0]
    except (OverflowError, struct.error):  # pragma: no cover - finite range checked first
        return math.inf if value > 0 else -math.inf


def _numeric_mod(left: Any, right: Any, *, legacy: bool = False) -> Any:
    """F&O 3.1 §4.2.6 ``op:numeric-mod``.

    XPath 1.0 has only doubles, so a legacy evaluation of two integer-looking
    operands yields ``-0.0`` where the 2.0 value space has an integer zero.
    """
    if legacy and not isinstance(left, float) and not isinstance(right, float):
        return _float_mod(float(left), float(right))
    if isinstance(left, float) or isinstance(right, float):
        result = _float_mod(left, right)
        if _is_double_precision(left) or _is_double_precision(right):
            return result
        if _is_single_precision(left) or _is_single_precision(right):
            return Float(result)
        return result
    if right == 0:
        raise ZeroDivisionError("mod by zero")
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        dividend = Decimal(left)
        divisor = Decimal(right)
        precision = len(dividend.as_tuple().digits) + len(divisor.as_tuple().digits) + 20
        with localcontext() as context:
            context.prec = max(50, precision)
            context.Emax = max(999999, abs(dividend.as_tuple().exponent) + 20)
            context.Emin = -context.Emax
            quotient = (dividend / divisor).to_integral_value(rounding=ROUND_DOWN)
            return dividend - divisor * quotient
    remainder = abs(int(left)) % abs(int(right))
    return -remainder if left < 0 else remainder


def _float_mod(left: Any, right: Any) -> float:
    """F&O float/double ``mod``: the dividend's sign, with its special values."""
    if math.isnan(left) or math.isnan(right):
        return math.nan
    if math.isinf(left) or right == 0:
        return math.nan
    if math.isinf(right):
        return left
    return math.fmod(left, right)


def install_numeric_functions(parser: Any, *, include_round: bool = True) -> None:
    """Install the F&O ``round``, ``round-half-to-even``, and ``mod`` rules.

    An XPath 1.0 parser keeps its own ``round`` (already ties toward positive
    infinity) and only needs the truncating remainder rule for ``mod``.
    """

    if include_round:
        _install_round_functions(parser)
    mod_base = parser.symbol_table["mod"]

    def evaluate_mod(self: Any, context: Any = None) -> Any:
        left, right = self.get_operands(context, cls=NumericProxy)
        if left is None:
            return []
        try:
            return _numeric_mod(left, right, legacy=self.parser.version == "1.0")
        except ZeroDivisionError:
            raise self.error("FOAR0001") from None

    parser._register(
        "mod",
        "_PyxsdModOperator",
        bases=(mod_base,),
        label=mod_base.label,
        bp=getattr(mod_base, "bp", 45),
        evaluate=evaluate_mod,
    )


def _install_round_functions(parser: Any) -> None:
    round_base = parser.symbol_table["round"]

    def evaluate_round(self: Any, context: Any = None) -> Any:
        item = _optional_numeric_argument(self, context)
        if item is _ABSENT_NUMERIC:
            return []
        if isinstance(item, float) and (math.isnan(item) or math.isinf(item)):
            return item
        precision = self.get_argument(context, index=1, default=0, cls=int)
        return _round_numeric(item, precision, even=False)

    parser._register(
        "round",
        "_PyxsdRoundFunction",
        bases=(round_base,),
        label=round_base.label,
        nargs=getattr(round_base, "nargs", (1, 2)),
        sequence_types=getattr(round_base, "sequence_types", ()),
        evaluate=evaluate_round,
    )

    half_even_base = parser.symbol_table["round-half-to-even"]

    def evaluate_half_to_even(self: Any, context: Any = None) -> Any:
        item = _optional_numeric_argument(self, context)
        if item is _ABSENT_NUMERIC:
            return []
        if isinstance(item, float) and (math.isnan(item) or math.isinf(item)):
            return item
        precision = self.get_argument(context, index=1, default=0, cls=int)
        return _round_numeric(item, precision, even=True)

    parser._register(
        "round-half-to-even",
        "_PyxsdRoundHalfToEvenFunction",
        bases=(half_even_base,),
        label=half_even_base.label,
        nargs=getattr(half_even_base, "nargs", (1, 2)),
        sequence_types=getattr(half_even_base, "sequence_types", ()),
        evaluate=evaluate_half_to_even,
    )


class _AbsentNumeric:
    """Sentinel distinguishing an empty numeric argument from ``None``."""


_ABSENT_NUMERIC = _AbsentNumeric()


def _optional_numeric_argument(token: Any, context: Any) -> Any:
    """The single required numeric argument, or the absent sentinel."""
    item = token.get_argument(context)
    if item is None:
        return _ABSENT_NUMERIC
    if isinstance(item, XPathNode):
        item = token.number_value(item)
    elif isinstance(item, UntypedAtomic):
        # The XPath 3.1 §3.1.5.2 function conversion rules cast an
        # xs:untypedAtomic argument to the parameter's numeric type.  The
        # union type resolves to xs:double, which is number_value's contract.
        item = token.number_value(item)
    if isinstance(item, bool) or not isinstance(item, (int, float, Decimal)):
        raise token.error("XPTY0004", f"invalid argument type {type(item)!r}")
    return item
