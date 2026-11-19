"""``xsl:number``, decimal formats, and ``fn:format-number`` behavior.

The tests exercise the public processor only.  They pin level/count/from
numbering, every normative format-token family, re-basing and grouping, the
decimal-format static constraints, and the number-picture grammar.
"""

from __future__ import annotations

import io
from functools import partial
from typing import Any

import pytest
from tests.xslt.support import stylesheet as _stylesheet

from pyxsd.xdm import XdmAtomic
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError, XsltError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"

stylesheet = partial(_stylesheet, namespaces={"my": MY_NS})

SOURCE = b"""<doc>
  <chapter><title>A</title><section><title>B</title><p>x</p></section></chapter>
  <chapter><title>C</title></chapter>
</doc>"""


def compiled(source: bytes, **options: Any):
    return XsltProcessor(**options).compile(io.BytesIO(source))


def transform(
    source: bytes,
    input_source: bytes | None = SOURCE,
    *,
    delivery: str = "document",
    **options: Any,
):
    stylesheet_ = compiled(source)
    if input_source is None:
        return stylesheet_.transform(delivery=delivery, **options)
    return stylesheet_.transform(source=input_source, delivery=delivery, **options)


def values(call: Any) -> list[str]:
    return [item.lexical for item in call().principal]


def text(call: Any) -> str:
    return call().principal_tree.string_value


def error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def number(
    *,
    value: str | None = None,
    select: str | None = None,
    level: str | None = None,
    count: str | None = None,
    from_: str | None = None,
    format_: str | None = None,
    lang: str | None = None,
    ordinal: str | None = None,
    letter_value: str | None = None,
    grouping_separator: str | None = None,
    grouping_size: str | None = None,
    start_at: str | None = None,
    match: str = "/",
) -> bytes:
    attributes = []
    for name, item in (
        ("value", value),
        ("select", select),
        ("level", level),
        ("count", count),
        ("from", from_),
        ("format", format_),
        ("lang", lang),
        ("ordinal", ordinal),
        ("letter-value", letter_value),
        ("grouping-separator", grouping_separator),
        ("grouping-size", grouping_size),
        ("start-at", start_at),
    ):
        if item is not None:
            attributes.append(f'{name}="{item}"')
    attribute_text = (" " + " ".join(attributes)) if attributes else ""
    return stylesheet(
        f'<xsl:template name="main" match="{match}"><xsl:number{attribute_text}/></xsl:template>'
    )


# ---------------------------------------------------------------------------
# Levels, count, from, select, and value


def test_value_attribute_formats_a_supplied_place_marker() -> None:
    assert text(lambda: transform(number(value="3"), initial_template="main")) == "3"


def test_value_sequence_uses_multiple_format_tokens() -> None:
    source = number(value="(5, 13, 7)", format_="A-001(i)")
    assert text(lambda: transform(source, initial_template="main")) == "E-013(vii)"


def test_value_zero_and_large_values() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number value="0"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="123456789" format="1"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="1000000" format="0000000"/>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, None, initial_template="main")) == ("0|123456789|1000000")


def test_value_rebases_with_start_at() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number value="(1, 1, 1, 1)" format="1.1.1.1" start-at="3 0 0"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="1" start-at="100"/>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, None, initial_template="main")) == "3.0.0.0|100"


def test_value_negative_or_non_integer_is_a_dynamic_error() -> None:
    for value in ("-1", "'x'", "xs:double('NaN')"):
        source = number(value=value)
        assert (
            error_code(lambda source=source: transform(source, initial_template="main"))
            == "XTDE0980"
        )


def test_value_rounds_to_the_nearest_integer_towards_positive_infinity() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number value="(1.5, 1.4, -0.4)"/>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, None, initial_template="main")) == "2.1.0"


def test_value_with_count_is_a_static_error() -> None:
    source = number(value="1", count="chapter")
    assert error_code(lambda: compiled(source)) == "XTSE0975"


def test_single_level_counts_matching_siblings() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//chapter"><xsl:number/></xsl:for-each>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, SOURCE, initial_template="main")) == "12"


def test_single_level_default_count_uses_same_name_and_kind() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//section/title"><xsl:number/></xsl:for-each>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, SOURCE, initial_template="main")) == "1"


def test_single_level_with_from_returns_empty_outside_the_subtree() -> None:
    source = stylesheet(
        """
        <xsl:template match="doc">
          <xsl:number from="chapter"/>
        </xsl:template>
        """
    )
    # The document node is outside every chapter subtree, so no place marker.
    assert text(lambda: transform(source)) == ""


def test_multiple_level_counts_each_matching_ancestor() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//section/title">
            <xsl:number level="multiple" count="chapter|section" format="1.1"/>
          </xsl:for-each>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, SOURCE, initial_template="main")) == "1.1"


def test_any_level_counts_preceding_nodes_and_honours_from() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//chapter[2]">
            <xsl:number level="any" count="*"/>
            <xsl:text>|</xsl:text>
            <xsl:number level="any" count="*" from="section"/>
          </xsl:for-each>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, SOURCE, initial_template="main")) == "7|4"


def test_select_attribute_chooses_the_numbered_node() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number select="//chapter[2]"/>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, SOURCE, initial_template="main")) == "2"


def test_number_without_context_node_or_with_non_node_select_is_a_type_error() -> None:
    no_context = stylesheet('<xsl:template name="main"><xsl:number/></xsl:template>')
    assert error_code(lambda: transform(no_context, None, initial_template="main")) == "XTTE0990"

    non_node = stylesheet('<xsl:template name="main"><xsl:number select="1"/></xsl:template>')
    assert error_code(lambda: transform(non_node, None, initial_template="main")) == "XTTE1000"


def test_invalid_level_is_a_static_error() -> None:
    source = number(level="deep")
    assert error_code(lambda: compiled(source)) == "XTSE0020"


def test_number_must_have_empty_content() -> None:
    source = stylesheet(
        '<xsl:template name="main"><xsl:number value="1">text</xsl:number></xsl:template>'
    )
    assert error_code(lambda: compiled(source)) == "XTSE0010"


# ---------------------------------------------------------------------------
# Numbering tokens


@pytest.mark.parametrize(
    ("format_", "numbers", "expected"),
    [
        ("1", ("1", "2", "10", "11"), ("1", "2", "10", "11")),
        ("01", ("1", "9", "10", "100"), ("01", "09", "10", "100")),
        ("0001", ("1", "23", "4567"), ("0001", "0023", "4567")),
        ("A", ("1", "2", "26", "27", "52", "703"), ("A", "B", "Z", "AA", "AZ", "AAA")),
        ("a", ("1", "2", "26", "27"), ("a", "b", "z", "aa")),
        ("i", ("1", "4", "9", "49"), ("i", "iv", "ix", "xlix")),
        ("I", ("1", "4", "9"), ("I", "IV", "IX")),
        ("w", ("1", "2", "21", "100"), ("one", "two", "twenty-one", "one hundred")),
        ("W", ("1", "2", "21"), ("ONE", "TWO", "TWENTY-ONE")),
        ("Ww", ("1", "2", "21"), ("One", "Two", "Twenty-One")),
    ],
)
def test_numbering_format_tokens(
    format_: str, numbers: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    expressions = "".join(f'<xsl:number value="{value}" format="{format_}"/>' for value in numbers)
    source = stylesheet(f'<xsl:template name="main">{expressions}</xsl:template>')
    assert text(lambda: transform(source, None, initial_template="main")) == "".join(expected)


def test_roman_falls_back_to_decimal_beyond_the_supported_range() -> None:
    source = number(value="5000", format_="I")
    assert text(lambda: transform(source, None, initial_template="main")) == "5000"


def test_multiple_format_tokens_and_literal_separators() -> None:
    source = number(value="(1, 2, 3)", format_="A-1-(i)")
    assert text(lambda: transform(source, None, initial_template="main")) == "A-2-(iii)"


def test_a_format_without_a_format_token_is_used_as_prefix_and_suffix() -> None:
    source = number(value="3", format_="()")
    assert text(lambda: transform(source, None, initial_template="main")) == "()3()"


def test_a_format_with_one_separator_between_adjacent_format_tokens() -> None:
    source = number(value="(1, 2)", format_="1 1")
    assert text(lambda: transform(source, None, initial_template="main")) == "1 2"


def test_unicode_decimal_digit_family_is_preserved() -> None:
    source = number(value="(1, 2, 12)", format_="&#x0660;&#x0661;")
    assert (
        text(lambda: transform(source, None, initial_template="main"))
        == "\u0660\u0661.\u0660\u0662.\u0661\u0662"
    )


def test_ordinal_decimal_and_word_forms() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number value="(1, 2, 3, 4, 11, 21)" format="1" ordinal="yes"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="(1, 2, 3, 21)" format="w" ordinal="yes"/>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, None, initial_template="main")) == (
        "1st.2nd.3rd.4th.11th.21st|first.second.third.twenty-first"
    )


def test_ordinal_no_and_zero_values_are_cardinal() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number value="2" format="1" ordinal="no"/>
          <xsl:number value="2" format="1" ordinal="0"/>
          <xsl:number value="2" format="1" ordinal="false"/>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, None, initial_template="main")) == "222"


def test_language_selection_and_unsupported_language_fallback() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number value="2" format="w" lang="en"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="2" format="w" lang="en-GB"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="2" format="w" lang="zz"/>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, None, initial_template="main")) == "two|two|two"


def test_letter_value_selects_alphabetic_or_traditional_sequences() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number value="(1, 2, 27)" format="a" letter-value="alphabetic"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="4" format="a" letter-value="traditional"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="4" format="i" letter-value="alphabetic"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="4" format="A" letter-value="traditional"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="4" format="I" letter-value="alphabetic"/>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, None, initial_template="main")) == "a.b.aa|iv|l|IV|L"

    invalid = stylesheet(
        '<xsl:template name="main"><xsl:number value="2" format="a" '
        'letter-value="unknown"/></xsl:template>'
    )
    assert error_code(lambda: transform(invalid, None, initial_template="main")) == "XTDE0030"


def test_grouping_separator_and_size_apply_to_decimal_tokens() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number value="1234567" grouping-separator="," grouping-size="3"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="1234567" grouping-separator="." grouping-size="2"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="1234567" grouping-separator=","/>
          <xsl:text>|</xsl:text>
          <xsl:number value="1234567" grouping-size="3"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="1234567" grouping-separator="," grouping-size="0"/>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, None, initial_template="main")) == (
        "1,234,567|1.23.45.67|1234567|1234567|1234567"
    )


def test_numbering_attribute_value_templates_are_evaluated() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number value="2" format="{concat('(', '1', ')')}"/>
        </xsl:template>
        """
    )
    assert text(lambda: transform(source, None, initial_template="main")) == "(2)"


# ---------------------------------------------------------------------------
# Decimal formats and fn:format-number


def format_number(
    *,
    expression: str,
    decimal_formats: str = "",
) -> bytes:
    return stylesheet(
        f'{decimal_formats}<xsl:template name="main">'
        f'<xsl:sequence select="{expression}"/>'
        f"</xsl:template>"
    )


def test_format_number_examples_from_the_specification() -> None:
    source = format_number(
        expression=(
            "format-number(12345.6, '#,###.00'), "
            "format-number(12345678.9, '9,999.99'), "
            "format-number(123.9, '9999'), "
            "format-number(0.14, '01%'), "
            "format-number(-6, '000')"
        )
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "12,345.60",
        "12,345,678.90",
        "0124",
        "14%",
        "-006",
    ]


def test_format_number_negative_subpicture_and_custom_minus_sign() -> None:
    source = format_number(
        expression="format-number(-1234.5, '#,##0.00;(#,##0.00)')",
        decimal_formats='<xsl:decimal-format name="my:minus" minus-sign="&#x2212;"/>',
    )
    assert values(lambda: transform(source, None, initial_template="main")) == ["(1,234.50)"]

    minus = format_number(
        expression="format-number(-6, '0', 'my:minus')",
        decimal_formats='<xsl:decimal-format name="my:minus" minus-sign="&#x2212;"/>',
    )
    assert values(lambda: transform(minus, None, initial_template="main")) == ["\u22126"]


def test_format_number_nan_infinity_and_empty_value() -> None:
    source = format_number(
        expression=(
            "format-number(xs:double('NaN'), '0.0'), "
            "format-number(xs:double('INF'), '0.0'), "
            "format-number(xs:double('-INF'), '0.0'), "
            "format-number((), '0.0')"
        ),
        decimal_formats='<xsl:decimal-format infinity="∞" NaN="n/a"/>',
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "n/a",
        "∞",
        "-∞",
        "n/a",
    ]


def test_named_decimal_format_symbols_and_defaults() -> None:
    source = format_number(
        expression="format-number(1234.5, '#.##0,00', 'my:dec')",
        decimal_formats=(
            '<xsl:decimal-format name="my:dec" decimal-separator="," grouping-separator="."/>'
        ),
    )
    assert values(lambda: transform(source, None, initial_template="main")) == ["1.234,50"]


def test_unnamed_decimal_format_symbols_apply_by_default() -> None:
    source = format_number(
        expression="format-number(1234.5, '#.##0,00')",
        decimal_formats='<xsl:decimal-format decimal-separator="," grouping-separator="."/>',
    )
    assert values(lambda: transform(source, None, initial_template="main")) == ["1.234,50"]


def test_pattern_separator_percent_per_mille_and_zero_digit() -> None:
    source = format_number(
        expression=(
            "format-number(-1, '0!0', 'my:bang'), "
            "format-number(0.4857, '###.###%'), "
            "format-number(0.4857, '###.###‰'), "
            "format-number(123, '\u0660\u0660\u0660', 'my:arabic')"
        ),
        decimal_formats=(
            '<xsl:decimal-format name="my:bang" pattern-separator="!"/>'
            '<xsl:decimal-format name="my:arabic" zero-digit="&#x0660;"/>'
        ),
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "1",
        "48.57%",
        "485.7‰",
        "١٢٣",
    ]


def test_format_number_minimum_sizes_grouping_and_decimal_removal() -> None:
    source = format_number(
        expression=(
            "format-number(0.5, '000.0'), "
            "format-number(1, '0.00'), "
            "format-number(1, '0.'), "
            "format-number(1234567.891, '#,##0.0#'), "
            "format-number(1.234567, '0.00#,##')"
        )
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "000.5",
        "1.00",
        "1",
        "1,234,567.89",
        "1.234,57",
    ]


def test_format_number_rounds_half_to_even() -> None:
    source = format_number(
        expression=(
            "format-number(2.5, '0'), "
            "format-number(3.5, '0'), "
            "format-number(2.345, '0.00'), "
            "format-number(2.355, '0.00')"
        )
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "2",
        "4",
        "2.34",
        "2.36",
    ]


def test_format_number_negative_zero_uses_the_negative_subpicture() -> None:
    source = format_number(expression="format-number(-0.0e0, '0.0;(0.0)')")
    assert values(lambda: transform(source, None, initial_template="main")) == ["(0.0)"]


def test_format_number_accepts_eqname_decimal_format_names() -> None:
    source = format_number(
        expression="format-number(1234.5, '#.##0,00', 'Q{urn:my}dec')",
        decimal_formats=(
            '<xsl:decimal-format name="my:dec" decimal-separator="," grouping-separator="."/>'
        ),
    )
    assert values(lambda: transform(source, None, initial_template="main")) == ["1.234,50"]


def test_format_number_unknown_decimal_format_name_is_fodf1280() -> None:
    for name in ("'my:missing'", "'1bad'", "'Q{urn:my'", "'my:dec'"):
        source = format_number(
            expression=f"format-number(1, '0', {name})",
            decimal_formats='<xsl:decimal-format name="my:other"/>',
        )
        assert error_code(
            lambda source=source: transform(source, None, initial_template="main")
        ) == ("FODF1280")


def test_format_number_invalid_pictures_are_fodf1310() -> None:
    for picture in (
        "0;0;0",
        "0,.0",
        "0%‰",
        "0a0",
        "0#",
        "0.#0",
        "abc",
        "0,",
    ):
        source = format_number(expression=f"format-number(1, '{picture}')")
        assert error_code(
            lambda source=source: transform(source, None, initial_template="main")
        ) == ("FODF1310")


def test_format_number_rejects_non_numeric_values() -> None:
    source = format_number(expression="format-number('1', '0')")
    assert error_code(lambda: transform(source, None, initial_template="main")) == "XPTY0004"


def test_decimal_format_single_character_attributes_are_static_errors() -> None:
    for attribute in (
        'decimal-separator=".."',
        'grouping-separator=""',
        'percent="%%"',
        'per-mille="‰‰"',
        'digit="##"',
        'pattern-separator=";;"',
        'minus-sign="--"',
    ):
        source = stylesheet(f"<xsl:decimal-format {attribute}/>")
        assert error_code(lambda source=source: compiled(source)) == "XTSE0020"


def test_decimal_format_zero_digit_must_be_zero() -> None:
    for value in ("1", "a"):
        source = stylesheet(f'<xsl:decimal-format zero-digit="{value}"/>')
        assert error_code(lambda source=source: compiled(source)) == "XTSE1295"


def test_decimal_format_symbols_must_be_distinct() -> None:
    source = stylesheet('<xsl:decimal-format decimal-separator="," grouping-separator=","/>')
    assert error_code(lambda: compiled(source)) == "XTSE1300"


def test_decimal_format_conflicting_values_at_same_precedence_are_static_errors() -> None:
    source = stylesheet(
        '<xsl:decimal-format name="my:dec" decimal-separator=","/>'
        '<xsl:decimal-format name="my:dec" decimal-separator="."/>'
    )
    assert error_code(lambda: compiled(source)) == "XTSE1290"


def test_format_number_value_types_and_empty_sequence() -> None:
    source = format_number(
        expression=(
            "format-number(xs:decimal('12.5'), '0.0'), "
            "format-number(xs:integer(7), '0'), "
            "format-number(xs:double(1.25), '0.00')"
        )
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "12.5",
        "7",
        "1.25",
    ]


# ---------------------------------------------------------------------------
# Public numbering API and boundary branches


def test_numbering_module_rejects_invalid_argument_types() -> None:
    from pyxsd.xdm.nodes import NodeKind, XdmDocument
    from pyxsd.xslt.runtime.numbering import (
        DecimalFormat,
        NumberingCache,
        format_number,
        format_place_marker,
        place_marker,
    )

    document = XdmDocument.parse(b"<doc><chapter><p/></chapter></doc>")
    chapter = document.root_element.children[0]
    paragraph = chapter.children[0]
    with pytest.raises(TypeError):
        place_marker("x", "single", lambda node: True, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        place_marker(paragraph, "invalid", lambda node: True, None)
    with pytest.raises(ValueError):
        NumberingCache().counter("key", "invalid", lambda node: True, None)
    with pytest.raises(ValueError):
        format_place_marker([1.5], "1")  # type: ignore[list-item]
    with pytest.raises(XsltDynamicError) as caught:
        format_number(1, 1, None, {None: DecimalFormat()}, {})  # type: ignore[arg-type]
    assert caught.value.code == "XPTY0004"

    attribute = document.root_element.children[0].attributes
    assert attribute == ()
    assert NodeKind.ELEMENT is chapter.kind
    with pytest.raises(XsltDynamicError) as caught:
        format_number(object(), "0", None, {None: DecimalFormat()}, {})
    assert caught.value.code == "XPTY0004"


def test_place_marker_from_and_root_boundaries() -> None:
    from pyxsd.xdm.nodes import NodeKind, XdmDocument
    from pyxsd.xslt.runtime.numbering import place_marker

    document = XdmDocument.parse(b"<doc><chapter><p/></chapter></doc>")
    chapter = document.root_element.children[0]
    paragraph = chapter.children[0]
    assert (
        place_marker(
            paragraph,
            "single",
            lambda node: node is chapter,
            lambda node: node is paragraph,
        )
        == ()
    )
    assert (
        place_marker(
            paragraph,
            "multiple",
            lambda node: node is chapter,
            lambda node: False,
        )
        == ()
    )
    assert (
        place_marker(
            paragraph,
            "any",
            lambda node: node.kind is NodeKind.ELEMENT,
            lambda node: False,
        )
        == ()
    )
    assert place_marker(document, "single", lambda node: True, None) == (1,)
    attributed = XdmDocument.parse(b'<doc n="1"/>')
    attribute = attributed.root_element.attributes[0]
    assert place_marker(attribute, "single", lambda node: node is attribute, None) == (1,)


def test_place_marker_any_can_select_an_attribute() -> None:
    from pyxsd.xdm.nodes import NodeKind, XdmDocument
    from pyxsd.xslt.runtime.numbering import place_marker

    document = XdmDocument.parse(b'<doc><chapter n="1"/><chapter n="2"/></doc>')
    attribute = document.root_element.children[1].attributes[0]
    marker = place_marker(
        attribute,
        "any",
        lambda node: node.kind is NodeKind.ELEMENT,
        None,
    )
    assert marker == (3,)


def test_number_formatting_boundary_tokens() -> None:
    from pyxsd.xslt.runtime.numbering import format_place_marker

    assert format_place_marker([], "1") == ""
    assert (
        format_place_marker([1234567], "1", grouping_separator=",", grouping_size="abc")
        == "1234567"
    )
    assert format_place_marker([-5], "1") == "-5"
    assert format_place_marker([0], "A") == "0"
    assert format_place_marker([10**21], "w") == "1000000000000000000000"
    assert format_place_marker([2], "a1") == "2"
    assert format_place_marker([2], "21") == "2"
    assert format_place_marker([0], "w") == "zero"
    assert format_place_marker([0], "w", ordinal=True) == "zeroth"
    assert format_place_marker([20], "w") == "twenty"
    assert format_place_marker([1000], "w") == "one thousand"
    assert format_place_marker([1000000], "w") == "one million"
    assert format_place_marker([2], "") == "2"


def test_decimal_format_ignores_unknown_attributes_and_exposes_defaults() -> None:
    from pyxsd.xslt.runtime.numbering import DecimalFormat

    decimal_format = DecimalFormat.from_attributes({"unknown": "x"})
    assert decimal_format.decimal_separator == "."
    assert decimal_format.zero_digit == "0"
    assert decimal_format.digits == "0123456789"


def test_format_number_untyped_atomic_and_named_formats() -> None:
    from pyxsd.xslt.runtime.numbering import DecimalFormat, format_number

    assert (
        format_number(
            XdmAtomic.untyped_atomic("1.5"),
            "0.0",
            None,
            {None: DecimalFormat()},
            {},
        )
        == "1.5"
    )
    with pytest.raises(XsltDynamicError) as caught:
        format_number(
            XdmAtomic.untyped_atomic("not-a-number"),
            "0.0",
            None,
            {None: DecimalFormat()},
            {},
        )
    assert caught.value.code == "XPTY0004"
    assert (
        format_number(
            1.5,
            "0,0",
            "dec",
            {"dec": DecimalFormat(decimal_separator=",")},
            {},
        )
        == "1,5"
    )
    with pytest.raises(XsltDynamicError) as missing:
        format_number(1, "0", "Q{urn:my}1bad", {None: DecimalFormat()}, {})
    assert missing.value.code == "FODF1280"
    with pytest.raises(XsltDynamicError) as unbound:
        format_number(1, "0", "zz:dec", {None: DecimalFormat()}, {})
    assert unbound.value.code == "FODF1280"
    with pytest.raises(XsltDynamicError) as bad_local:
        format_number(1, "0", "1bad:dec", {None: DecimalFormat()}, {})
    assert bad_local.value.code == "FODF1280"


def test_format_number_picture_boundaries() -> None:
    from pyxsd.xslt.runtime.numbering import DecimalFormat, format_number

    formats = {None: DecimalFormat()}
    assert format_number(5, "#", None, formats, {}) == "5"
    assert format_number(1234567, "#,##,##0", None, formats, {}) == "12,34,567"
    for picture in ("0.0.0", "0%‰", "0%%", "."):
        with pytest.raises(XsltDynamicError) as caught:
            format_number(1, picture, None, formats, {})
        assert caught.value.code == "FODF1310"

    arabic = {"{urn:my}arabic": DecimalFormat.from_attributes({"zero-digit": "\u0660"})}
    with pytest.raises(XsltDynamicError) as caught:
        format_number(1, "\u06600", "my:arabic", arabic, {"my": "urn:my"})
    assert caught.value.code == "FODF1310"


def test_number_value_atomizes_nodes_and_rejects_function_items() -> None:
    node_value = stylesheet('<xsl:template name="main"><xsl:number value="//n"/></xsl:template>')
    assert (
        text(lambda: transform(node_value, b"<doc><n>2</n></doc>", initial_template="main")) == "2"
    )

    function_value = stylesheet(
        '<xsl:template name="main"><xsl:number value="function() { 1 }"/></xsl:template>'
    )
    assert (
        error_code(lambda: transform(function_value, None, initial_template="main")) == "FOTY0013"
    )


def test_number_value_conversion_boundaries() -> None:
    for expression in ("xs:date('2020-01-01')", "xs:untypedAtomic('INF')"):
        source = stylesheet(
            f'<xsl:template name="main"><xsl:number value="{expression}"/></xsl:template>'
        )
        assert (
            error_code(lambda source=source: transform(source, None, initial_template="main"))
            == "XTDE0980"
        )

    float_value = stylesheet(
        '<xsl:template name="main"><xsl:number value="xs:double(1.5)"/></xsl:template>'
    )
    assert text(lambda: transform(float_value, None, initial_template="main")) == "2"


def test_number_start_at_rejects_non_integer_tokens() -> None:
    source = stylesheet(
        '<xsl:template name="main"><xsl:number value="1" start-at="bad"/></xsl:template>'
    )
    assert error_code(lambda: transform(source, None, initial_template="main")) == "XTDE0030"


def test_format_number_dynamic_call_and_multiple_values() -> None:
    dynamic = stylesheet(
        '<xsl:template name="main"><xsl:sequence select="format-number#2(1, \'0\')"/></xsl:template>'
    )
    assert values(lambda: transform(dynamic, None, initial_template="main")) == ["1"]

    multiple = stylesheet(
        '<xsl:template name="main"><xsl:sequence select="format-number((1, 2), \'0\')"/></xsl:template>'
    )
    assert error_code(lambda: transform(multiple, None, initial_template="main")) == "XPTY0004"


def test_context_bridge_validates_decimal_format_values() -> None:
    from pyxsd.xslt.runtime.functions import XsltContextBridge
    from pyxsd.xslt.runtime.numbering import DecimalFormat
    from pyxsd.xslt.runtime.transformation import Transformation

    with pytest.raises(TypeError):
        XsltContextBridge(Transformation(), decimal_formats=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        XsltContextBridge(Transformation(), decimal_formats={1: DecimalFormat()})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        XsltContextBridge(Transformation(), decimal_formats={None: "bad"})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# Review fix round 1 regressions


def test_format_number_preserves_more_than_28_significant_digits() -> None:
    source = format_number(
        expression=(
            "format-number(99999999999999999999999999999, '0'), "
            "format-number(12222222222222222222222222222, '0'), "
            "format-number(xs:decimal('1222222222222222222222222222.5'), '0.0')"
        )
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "99999999999999999999999999999",
        "12222222222222222222222222222",
        "1222222222222222222222222222.5",
    ]


def test_format_number_atomizes_nodes_and_attributes() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="format-number(/doc/price, '0.00')"/>
          <xsl:sequence select="format-number(/doc/n/@x, '0.00')"/>
          <xsl:for-each select="/doc/n">
            <xsl:sequence select="format-number(@x, '#,##0.00')"/>
          </xsl:for-each>
        </xsl:template>
        """
    )
    assert values(
        lambda: transform(
            source,
            b'<doc><price>12.5</price><n x="12.5"/></doc>',
            initial_template="main",
        )
    ) == ["12.50", "12.50", "12.50"]


def test_format_number_large_doubles_do_not_raise_decimal_errors() -> None:
    source = format_number(
        expression=(
            "format-number(xs:double('1e50'), '0.00'), "
            "format-number(xs:double('1e308'), '0'), "
            "format-number(xs:double('1e308'), '0%')"
        )
    )
    results = values(lambda: transform(source, None, initial_template="main"))
    assert results[0].startswith("1") and results[0].endswith(".00")
    assert results[1].startswith("1") and results[1].isdigit() and len(results[1]) == 309
    assert results[2].startswith("1") and results[2].endswith("%") and len(results[2]) == 312


def test_place_marker_uses_dot_after_a_prefix() -> None:
    from pyxsd.xslt.runtime.numbering import format_place_marker

    assert format_place_marker([1, 2, 3], "(1)") == "(1.2.3)"
    assert format_place_marker([1, 2, 3], "(A)") == "(A.B.C)"
    assert format_place_marker([1, 2, 3], "(w)") == "(one.two.three)"

    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//section">
            <xsl:number level="multiple" count="chapter|section" format="(1)"/>
          </xsl:for-each>
        </xsl:template>
        """
    )
    source_xml = b"<doc><chapter><section/></chapter><chapter><section/></chapter></doc>"
    assert text(lambda: transform(source, source_xml, initial_template="main")) == "(1.1)(2.1)"


def test_exponent_pictures_and_exponent_separator() -> None:
    source = format_number(
        expression=(
            "format-number(12.34, '0.000e00'), "
            "format-number(1234.5678, '00.000E0', 'fortran'), "
            "format-number(0.234, '0.0E0', 'fortran'), "
            "format-number(0.234, '#.00E0', 'fortran'), "
            "format-number(0.234, '.00E0', 'fortran')"
        ),
        decimal_formats='<xsl:decimal-format name="fortran" exponent-separator="E"/>',
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "1.234e01",
        "12.346E2",
        "2.3E-1",
        "0.23E0",
        ".23E0",
    ]


def test_exponent_separator_must_be_one_character() -> None:
    source = stylesheet('<xsl:decimal-format exponent-separator="EE"/>')
    assert error_code(lambda: compiled(source)) == "XTSE0020"


def test_format_number_removes_a_leading_zero_without_a_mandatory_integer_digit() -> None:
    source = format_number(
        expression=(
            "format-number(0.5, '.00'), "
            "format-number(0.5, '#.0'), "
            "format-number(0.234, '.00E0', 'fortran')"
        ),
        decimal_formats='<xsl:decimal-format name="fortran" exponent-separator="E"/>',
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        ".50",
        ".5",
        ".23E0",
    ]


def test_exponent_and_letter_value_boundary_branches() -> None:
    from pyxsd.xslt.runtime.numbering import DecimalFormat, format_number, format_place_marker

    formats = {None: DecimalFormat()}
    assert format_number(0, "0.0e0", None, formats, {}) == "0.0e0"
    assert format_number(1, "e0", None, formats, {}) == "e1"
    assert format_number(1, "0e ", None, formats, {}) == "1e "
    assert format_number(0.123, "#.e9", None, formats, {}) == "0.1e0"
    assert format_number(1, "0e0ms", None, formats, {}) == "1e0ms"
    for picture in ("0e0e0", "0e0%", "0e#"):
        with pytest.raises(XsltDynamicError) as caught:
            format_number(1, picture, None, formats, {})
        assert caught.value.code == "FODF1310"
    assert format_place_marker([0], "a", letter_value="traditional") == "0"
    assert format_place_marker([0], "i", letter_value="alphabetic") == "0"


# ---------------------------------------------------------------------------
# Review fix round 2 regressions


def test_large_percent_and_per_mille_scaling_is_exact() -> None:
    source = format_number(
        expression=(
            "format-number(99999999999999999999999999999, '0%'), "
            "format-number(99999999999999999999999999999, '0‰')"
        )
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "9999999999999999999999999999900%",
        "99999999999999999999999999999000‰",
    ]


def test_exponent_mantissa_preserves_more_than_28_significant_digits() -> None:
    source = format_number(
        expression=(
            "format-number(99999999999999999999999999999, "
            "'0.0000000000000000000000000000e00'), "
            "format-number(12345678901234567890123456789, "
            "'0.00000000000000000000000000000e00')"
        )
    )
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "9.9999999999999999999999999999e28",
        "1.23456789012345678901234567890e28",
    ]


def test_empty_place_marker_still_validates_letter_value() -> None:
    source = stylesheet(
        '<xsl:template name="main"><xsl:number value="()" format="a" '
        'letter-value="unknown"/></xsl:template>'
    )
    assert error_code(lambda: transform(source, None, initial_template="main")) == "XTDE0030"


def test_exponent_picture_pads_mandatory_fractional_digits() -> None:
    source = format_number(
        expression=(
            "format-number(99999999999999999999999999999, '0.00000000000000000000000000000e00')"
        )
    )
    # The 29-zero fractional picture requires 29 fractional digits; the exact
    # mantissa has 28, so F&O step 9 adds one mandatory trailing zero.
    assert values(lambda: transform(source, None, initial_template="main")) == [
        "9.99999999999999999999999999990e28"
    ]


# ---------------------------------------------------------------------------
# Decimal tokens beyond a trailing digit one, and Unicode numbering sequences


def test_an_all_digit_token_is_decimal_with_its_own_zero_digit() -> None:
    from pyxsd.xslt.runtime.numbering import format_place_marker

    # A token whose characters are all decimal digits names a decimal family:
    # the zero digit is the token's digit of value zero and the width is the
    # token length, so the zero itself carries grouping (XSLT 3.0 section 12.3).
    assert format_place_marker([0], "0") == "0"
    assert format_place_marker([5], "00") == "05"
    assert format_place_marker([1234], "0", grouping_separator=",", grouping_size="3") == "1,234"
    assert format_place_marker([0], "\u0660") == "\u0660"
    assert format_place_marker([5], "\u0660\u0661") == "\u0660\u0665"
    # A lone digit one of a family still derives its zero from its value.
    assert format_place_marker([5], "\u0661") == "\u0665"
    assert format_place_marker([9], "1") == "9"
    # A token with no zero digit of its own is not a decimal family token.
    assert format_place_marker([5], "21") == "5"


def test_multiple_decimal_families_keep_grouping_and_zero_tokens() -> None:
    from pyxsd.xslt.runtime.numbering import format_place_marker

    # The shape of catalog case number-0111: an ASCII one, an ASCII zero, and an
    # Arabic-Indic zero, all formatting the same number in their own family.
    assert (
        format_place_marker(
            [1234, 1234, 1234],
            "[1(0)(\u0660)]",
            grouping_separator=":",
            grouping_size="3",
        )
        == "[1:234(1:234)(\u0661:\u0662\u0663\u0664)]"
    )


def test_a_single_numeric_token_formats_as_its_contiguous_codepoint_run() -> None:
    from pyxsd.xslt.runtime.numbering import format_place_marker

    # XSLT 3.0 section 12.3: any other format token names a numbering sequence
    # in which the token represents one.  CIRCLED DIGIT ONE starts a run of
    # twenty characters, so values one to twenty are the run's codepoints.
    assert format_place_marker([1], "\u2460") == "\u2460"
    assert format_place_marker([2], "\u2460") == "\u2461"
    assert format_place_marker([20], "\u2460") == "\u2473"
    # Beyond the run the processor uses the format token "1".
    assert format_place_marker([21], "\u2460") == "21"
    assert format_place_marker([0], "\u2460") == "0"


def test_a_numbering_sequence_zero_needs_an_adjacent_zero_glyph() -> None:
    from pyxsd.xslt.runtime.numbering import format_place_marker

    # SUBSCRIPT ONE is preceded by SUBSCRIPT ZERO, so zero is in the run.
    assert format_place_marker([0], "\u2081") == "\u2080"
    assert format_place_marker([9], "\u2081") == "\u2089"
    assert format_place_marker([10], "\u2081") == "10"
    # CIRCLED DIGIT ONE is preceded by an unassigned codepoint, so it is not.
    assert format_place_marker([0], "\u2460") == "0"


def test_a_non_contiguous_or_non_unit_token_is_not_a_sequence() -> None:
    from pyxsd.xslt.runtime.numbering import format_place_marker

    # OLD ITALIC NUMERAL ONE is a run of one: the next codepoint is FIVE.
    assert format_place_marker([1], "\U00010320") == "\U00010320"
    assert format_place_marker([2], "\U00010320") == "2"
    # TIBETAN DIGIT HALF ONE has numeric value one half, not one.
    assert format_place_marker([1], "\u0f2a") == "1"
    # Only a single character can start a sequence.
    assert format_place_marker([1], "\u2460\u2460") == "1"
    # An ideograph with numeric value one is a letter, not a number, so the
    # contiguous rule does not claim it.
    assert format_place_marker([1], "\u4e00") == "1"
    # A value far past the codespace still has a decimal fallback.
    assert format_place_marker([10**21], "\u2460") == "1000000000000000000000"


def test_every_declared_numbering_range_is_what_the_engine_formats() -> None:
    """The profile's numbering declaration is exactly the measured behaviour.

    XSLT 3.0 section 12.3 lets an implementation choose an implementation-defined
    range for a numbering sequence, and requires the format token ``1`` outside
    it.  The declared range must therefore be the run the formatter really
    produces: a sequence character inside the range, the decimal fallback one
    value past either end.
    """
    import unicodedata

    from tests.xslt.selection import XSLT30_NON_STREAMING

    from pyxsd.xslt.runtime.numbering import format_place_marker

    for sequence, (low, high) in XSLT30_NON_STREAMING.numbering_sequences.items():
        token = unicodedata.lookup(sequence)
        for value in (low, high):
            expected = chr(ord(token) + value - 1)
            assert format_place_marker([value], token) == expected, (sequence, value)
        assert format_place_marker([high + 1], token) == str(high + 1), sequence
        if low > 1:
            assert format_place_marker([low - 1], token) == str(low - 1), sequence


def test_number_uses_a_contiguous_sequence_through_the_instruction() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:number value="(1, 2, 21)" format="&#x2460;"/>
          <xsl:text>|</xsl:text>
          <xsl:number value="(1, 3)" format="&#x2474;"/>
        </xsl:template>
        """
    )
    # U+2460 CIRCLED DIGIT ONE runs to twenty; U+2474 PARENTHESIZED DIGIT ONE
    # runs to twenty as well, and the values inside each run are its codepoints.
    assert text(lambda: transform(source, None, initial_template="main")) == (
        "\u2460.\u2461.21|\u2474.\u2476"
    )


# ---------------------------------------------------------------------------
# Counting once per parent and the per-instruction counting state


def test_number_any_from_resets_at_the_nearest_preceding_match() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//p">
            <xsl:number level="any" count="p" from="chapter|section"/>
          </xsl:for-each>
        </xsl:template>
        """
    )
    input_source = b"""<doc><chapter>
      <section><p/><p/></section>
      <section><p/></section>
    </chapter></doc>"""
    # Both sections reset the count; only the nearest preceding match wins.
    assert text(lambda: transform(source, input_source, initial_template="main")) == "121"


def test_number_any_counts_ancestors_and_preceding_nodes() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//f"><xsl:number level="any" count="*"/></xsl:for-each>
        </xsl:template>
        """
    )
    input_source = b'<doc a="1"><a><b/></a><c><d><e><f n="2"/></e></d></c></doc>'
    # The union includes ancestors and preceding subtrees; attributes and
    # namespace nodes are never counted.
    assert text(lambda: transform(source, input_source, initial_template="main")) == "7"


def test_number_count_pattern_with_a_predicate_is_honoured() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//p">
            <xsl:number level="single" count="section[@level='2']"/>
            <xsl:text>/</xsl:text>
            <xsl:number level="multiple" count="section[@level='2']"/>
            <xsl:text>|</xsl:text>
          </xsl:for-each>
        </xsl:template>
        """
    )
    input_source = b"""<doc>
      <section level="1"/>
      <section level="2"><p/></section>
      <section level="2"><p/></section>
    </doc>"""
    # The per-parent array is built from predicate results, not from a
    # name/kind shortcut that would count the level="1" section.
    assert text(lambda: transform(source, input_source, initial_template="main")) == "1/1|2/2|"


def test_number_any_across_markers_out_of_document_order() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//p">
            <xsl:number level="any" count="p" from="chapter"/><xsl:text>,</xsl:text>
          </xsl:for-each>
          <xsl:text>|</xsl:text>
          <xsl:for-each select="//p">
            <xsl:sort select="@n" data-type="number" order="descending"/>
            <xsl:number level="any" count="p" from="chapter"/><xsl:text>,</xsl:text>
          </xsl:for-each>
        </xsl:template>
        """
    )
    input_source = b"""<doc>
      <chapter><p n="1"/><p n="2"/><p n="3"/></chapter>
      <chapter><p n="4"/><p n="5"/></chapter>
    </doc>"""
    # The reverse visit must read the same markers from the recorded state.
    assert (
        text(lambda: transform(source, input_source, initial_template="main"))
        == "1,2,3,1,2,|2,1,3,2,1,"
    )


def test_number_single_attribute_selected_node_counts_no_preceding_siblings() -> None:
    source = stylesheet(
        """
        <xsl:template name="main" match="/">
          <xsl:number select="(//@n)[1]" count="*|@*"/>
        </xsl:template>
        """
    )
    input_source = b'<doc n="0"><item n="1"/><item n="2"/></doc>'
    # XSLT 3.0 section 12.3: an attribute node has no preceding siblings.  The
    # pre-change sibling scan returned 3 because the attribute is not one of
    # its parent's children, so the loop never stopped.
    assert text(lambda: transform(source, input_source)) == "1"


def test_number_counts_once_per_parent_for_a_wide_sibling_list() -> None:
    notes = "".join(f'<note id="{index}"/>' for index in range(1, 61))
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//note">
            <xsl:number level="any" count="note" from="chapter"/><xsl:text> </xsl:text>
          </xsl:for-each>
          <xsl:text>|</xsl:text>
          <xsl:for-each select="//note">
            <xsl:number level="single" count="note"/><xsl:text> </xsl:text>
          </xsl:for-each>
        </xsl:template>
        """
    )
    input_source = f"<doc><chapter>{notes}</chapter></doc>".encode()
    expected = " ".join(str(index) for index in range(1, 61))
    assert (
        text(lambda: transform(source, input_source, initial_template="main"))
        == f"{expected} |{expected} "
    )


def test_number_any_in_an_element_rooted_temporary_tree_uses_the_counter_path() -> None:
    source = stylesheet(
        """
        <xsl:template name="main" match="/">
          <xsl:variable name="t" as="element()"><a><b/><b/><b/><c/></a></xsl:variable>
          <xsl:number level="any" select="$t/c" count="a|b|c"/>
        </xsl:template>
        """
    )
    # The variable holds a delivered element-rooted temporary tree.  Delivery
    # finalizes the tree, so its nodes carry distinct document positions and
    # the marker runs on the incremental counter path instead of the reference
    # fallback.  XSLT 3.0 section 12.3 makes 5 the normative answer.
    assert text(lambda: transform(source, None, initial_template="main")) == "5"


def test_number_any_in_an_unfinalized_tree_keeps_the_reference_fallback() -> None:
    from pyxsd.xslt.runtime.builder import _NodeFactory
    from pyxsd.xslt.runtime.numbering import NumberingCounter, place_marker

    # A tree that was built but never finalized keeps its pre-cache behavior:
    # every node reports document_order -1, so the marker takes the reference
    # walk instead of asserting on the incremental one.
    factory = _NodeFactory()
    root = factory.element("a")
    for name in ("b", "b", "b", "c"):
        factory._append(root, factory.element(name))
    assert root.document.document_order == -1
    selected = root.children[-1]

    def count(node: Any) -> bool:
        return node.name in {"a", "b", "c"}

    counter = NumberingCounter("any", count, None)
    assert place_marker(selected, "any", count, None, counter=counter) == (1,)
    assert place_marker(selected, "any", count, None) == (1,)


def test_number_implied_count_follows_each_selected_nodes_name() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="//*">
            <xsl:number level="single"/><xsl:text>|</xsl:text>
          </xsl:for-each>
        </xsl:template>
        """
    )
    input_source = b"<doc><a/><b/><a/><b/></doc>"
    # The implied count is the selected node's own kind and name, so markers
    # for two names may not share one per-parent array.
    assert text(lambda: transform(source, input_source, initial_template="main")) == "1|1|1|2|2|"


def test_number_cache_does_not_leak_between_transformations() -> None:
    source = stylesheet(
        """
        <xsl:template match="/">
          <out><xsl:apply-templates select="//p"/></out>
        </xsl:template>
        <xsl:template match="p">
          <xsl:number level="any" count="p" from="chapter"/><xsl:text>,</xsl:text>
        </xsl:template>
        """
    )
    first_input = b"<doc><chapter><p/><p/></chapter></doc>"
    other_input = b"<doc><chapter><p/><p/><p/></chapter><chapter><p/></chapter></doc>"
    stylesheet_ = compiled(source)
    first = stylesheet_.transform(source=first_input).principal_tree.string_value
    other = stylesheet_.transform(source=other_input).principal_tree.string_value
    again = stylesheet_.transform(source=first_input).principal_tree.string_value
    assert first == "1,2,"
    assert other == "1,2,3,1,"
    assert again == first


def test_number_count_pattern_with_a_variable_is_not_cached() -> None:
    source = stylesheet(
        """
        <xsl:template name="main" match="/">
          <xsl:for-each select="//item">
            <xsl:variable name="wanted" select="if (position() le 2) then 'a' else 'b'"/>
            <xsl:number level="single" count="*[@role = $wanted]"/><xsl:text>|</xsl:text>
          </xsl:for-each>
          <xsl:for-each select="//item">
            <xsl:variable name="wanted" select="if (position() le 2) then 'a' else 'b'"/>
            <xsl:number level="any" count="item" from="*[@role = $wanted]"/><xsl:text>|</xsl:text>
          </xsl:for-each>
        </xsl:template>
        """
    )
    input_source = b'<doc><item role="a"/><item role="a"/><item role="b"/><item role="b"/></doc>'
    # A variable-bearing count or from pattern keeps the per-marker path: the
    # first marker's locals must never answer a later marker.
    assert (
        text(lambda: transform(source, input_source, initial_template="main")) == "1|2|1|2|1|1|1|1|"
    )


def test_numbering_counter_shares_state_within_one_transformation() -> None:
    from pyxsd.xdm.nodes import NodeKind, XdmDocument
    from pyxsd.xslt.runtime.numbering import NumberingCache, place_marker

    document = XdmDocument.parse(b'<doc><chapter n="1"><a/><a/></chapter></doc>')
    chapter = document.root_element.children[0]
    first, _ = chapter.children
    cache = NumberingCache()
    counter = cache.counter("any", "any", lambda node: node.kind is NodeKind.ELEMENT, None)
    assert cache.counter("any", "any", lambda node: False, None) is counter
    assert counter.place_marker(first) == (3,)
    # A marker that arrives earlier in document order reads the recorded state.
    assert counter.place_marker(chapter) == (2,)
    attribute = chapter.attributes[0]
    assert place_marker(
        attribute,
        "any",
        lambda node: node.kind is NodeKind.ELEMENT,
        None,
        counter=counter,
    ) == (2,)
    namespace = chapter.namespace_nodes[0]
    assert place_marker(
        namespace,
        "any",
        lambda node: node.kind is NodeKind.ELEMENT,
        None,
        counter=counter,
    ) == (2,)
    attribute_count = cache.counter(
        "attribute-count",
        "any",
        lambda node: node.kind is NodeKind.ATTRIBUTE,
        None,
    )
    assert place_marker(
        attribute,
        "any",
        lambda node: node.kind is NodeKind.ATTRIBUTE,
        None,
        counter=attribute_count,
    ) == (1,)
    attribute_from = cache.counter(
        "attribute-from",
        "any",
        lambda node: node.kind is NodeKind.ELEMENT,
        lambda node: node is attribute,
    )
    assert (
        place_marker(
            attribute,
            "any",
            lambda node: node.kind is NodeKind.ELEMENT,
            lambda node: node is attribute,
            counter=attribute_from,
        )
        == ()
    )
    single = cache.counter("single", "single", lambda node: node.kind is NodeKind.DOCUMENT, None)
    assert single.place_marker(document) == (1,)


def test_numbering_counter_rejects_a_mismatched_level() -> None:
    from pyxsd.xdm.nodes import NodeKind, XdmDocument
    from pyxsd.xslt.runtime.numbering import NumberingCache, place_marker

    document = XdmDocument.parse(b"<doc><a/></doc>")
    counter = NumberingCache().counter(
        "single", "single", lambda node: node.kind is NodeKind.ELEMENT, None
    )
    # Internal invariant: a counter carries one instruction's level and
    # predicates, so numbering another level through it is a programming
    # error rather than a silently wrong marker.
    with pytest.raises(AssertionError):
        place_marker(
            document.root_element.children[0],
            "any",
            lambda node: node.kind is NodeKind.ELEMENT,
            None,
            counter=counter,
        )
