"""F&O numeric function and operator rules for the XPath layer.

``fn:round`` rounds ties toward positive infinity, ``fn:round-half-to-even``
rounds ties to the even neighbour, and ``op:numeric-mod`` takes the dividend's
sign; all three must do exact arithmetic on the operand's own value space,
including the binary32 value of an ``xs:float`` and decimals wider than the
default decimal context.  These tests pin the overrides installed on each
XPath parser instance.
"""

from __future__ import annotations

import io
from functools import partial

import pytest
from tests.xslt.support import stylesheet as _stylesheet
from tests.xslt.support import transform as _transform

from pyxsd.xslt.errors import XsltError

stylesheet = partial(_stylesheet)
transform = partial(_transform, delivery="document")


def text(expression: str) -> str:
    body = f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
    return transform(stylesheet(body), initial_template="main").principal_tree.string_value


def error_code(expression: str) -> str | None:
    body = f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
    with pytest.raises(XsltError) as caught:
        transform(stylesheet(body), initial_template="main")
    return caught.value.code


def test_round_with_precision_rounds_ties_toward_positive_infinity() -> None:
    assert text("round(8450, -2)") == "8500"
    assert text("round(8490, -2)") == "8500"
    assert text("round(-8450, -2)") == "-8400"
    assert text("round(2.5)") == "3"
    assert text("round(-2.5)") == "-2"


def test_round_uses_the_binary32_value_of_an_float() -> None:
    # 13.65 as binary32 is 13.6499996..., so the nearest tenth is 13.6.
    assert text("round(xs:float(1.365e1), 1)") == "13.6"
    assert text("round(xs:float(-1.365e1), 1)") == "-13.6"
    assert text("round(xs:float(8496.787e2), -2)") == "849700"


def test_round_half_to_even_uses_the_operand_value_space() -> None:
    assert text("round-half-to-even(2.5)") == "2"
    assert text("round-half-to-even(3.5)") == "4"
    # Binary32 250.025 is 250.02499..., which is nearer 250.02 than 250.03.
    assert text("round-half-to-even(xs:float(250.0250e0), 2)") == "250.02"
    assert text("round-half-to-even(xs:float(150.0150e0), 2)") == "150.01"


def test_round_half_to_even_keeps_every_digit_of_a_wide_decimal() -> None:
    value = "1234567896123456789612345678961234567896.1234567896"
    assert text(f"round-half-to-even({value}, 4)") == (
        "1234567896123456789612345678961234567896.1235"
    )
    assert text(f"round-half-to-even({value}, -1)") == ("1234567896123456789612345678961234567900")


def test_mod_takes_the_dividend_sign_and_keeps_exact_decimals() -> None:
    assert text("1 div (2e0 mod -2)") == "INF"
    assert text("1 div (-2e0 mod 2)") == "-INF"
    assert text("5.2 mod -2.0") == "1.2"
    assert text("-5.2 mod 2.0") == "-1.2"
    assert text("10000000000000000000000000000000000 mod 0.1") == "0"
    assert text("19999999999999999999999999999999999 mod 0.333333333333333333333333333333") == (
        "0.000000000000000000000000019999"
    )


def test_mod_zero_divisor_and_infinities() -> None:
    assert error_code("1 mod 0") == "FOAR0001"
    assert text("1 mod 0e0") == "NaN"
    assert text("1e0 mod 2e0") == "1"
    assert text("2e0 mod xs:double('INF')") == "2"


def test_round_and_mod_argument_edges() -> None:
    assert text("round(())") == ""
    assert text("round(xs:double('NaN'))") == "NaN"
    assert text("round-half-to-even(xs:double('INF'))") == "INF"
    assert text("round-half-to-even(())") == ""
    assert text("round(2.5e0)") == "3"
    assert text("round(8450, 2)") == "8450"
    assert text("round(0e0)") == "0"
    assert text("round(-0.4e0)") == "-0"
    assert error_code("round('x')") == "XPTY0004"
    assert error_code("round(true())") == "XPTY0004"

    assert text("() mod 2") == ""
    assert text("1 mod ()") == ""
    assert text("xs:double('NaN') mod 1") == "NaN"
    assert text("-5 mod 2") == "-1"
    assert text("5 mod -2") == "1"
    assert text("-5.2 mod -2.0") == "-1.2"
    assert text("xs:float(5e0) mod xs:float(2e0)") == "1"
    assert text("(xs:float(5e0) mod xs:float(2e0)) instance of xs:float") == "true"


def test_round_atomizes_a_node_argument() -> None:
    body = '<xsl:template name="main"><xsl:value-of select="round(/doc/n)"/></xsl:template>'
    result = transform(
        stylesheet(body),
        b"<doc><n>2.6</n></doc>",
        initial_template="main",
    )
    assert result.principal_tree.string_value == "3"


def test_native_1_0_mod_takes_the_dividend_sign() -> None:
    from pyxsd.xslt import XsltProcessor

    body = (
        '<xsl:template name="main">'
        '<xsl:value-of select="-5 mod 2"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="5 mod -2"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="1 div (2 mod -2)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="1 div (-2 mod 2)"/>'
        "</xsl:template>"
    )
    compiled = XsltProcessor(language="1.0", xpath="1.0").compile(
        io.BytesIO(stylesheet(body, version="1.0"))
    )
    result = compiled.transform(initial_template="main", delivery="document")
    assert result.principal_tree.string_value == "-1|1|Infinity|-Infinity"


def test_numeric_helpers_reject_non_numeric_payloads() -> None:
    from decimal import Decimal

    from pyxsd.xpath.numeric import _numeric_mod, _round_numeric

    with pytest.raises(TypeError):
        _round_numeric(True, 0, even=False)
    with pytest.raises(TypeError):
        _round_numeric("1", 0, even=True)
    assert _round_numeric(Decimal("-0.25"), 0, even=False) == Decimal("-0")
    assert _numeric_mod(3, 2) == 1
    with pytest.raises(ZeroDivisionError):
        _numeric_mod(Decimal(1), Decimal(0))
