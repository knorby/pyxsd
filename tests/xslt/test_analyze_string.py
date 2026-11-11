"""Normative ``xsl:analyze-string`` behavior and regex-group state.

Every stylesheet is inline and every assertion is behavioral through the public
processor.  The cases pin the XPath 3.0 regular-expression grammar (not Python
``re``), zero-length global matching, nested capture state, flags, and the
instruction's static content model.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError, XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"


def stylesheet(body: str, *, version: str = "3.0") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="{version}">{body}</xsl:stylesheet>'
    ).encode()


def compiled(source: bytes, **options: Any):
    return XsltProcessor(**options).compile(io.BytesIO(source))


def transform(
    source: bytes,
    input_source: bytes | None = b"<doc/>",
    *,
    delivery: str = "raw",
    **options: Any,
):
    stylesheet_ = compiled(source)
    if input_source is None:
        return stylesheet_.transform(delivery=delivery, **options)
    return stylesheet_.transform(source=input_source, delivery=delivery, **options)


def values(call: Any) -> list[str]:
    return [item.lexical for item in call().principal]


def error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def analyze(body: str, *, regex: str, select: str = "'.'", flags: str | None = None) -> bytes:
    flag_attribute = "" if flags is None else f' flags="{flags}"'
    return stylesheet(
        f'<xsl:template name="main">'
        f'<xsl:analyze-string select="{select}" regex="{regex}"{flag_attribute}>'
        f"{body}"
        f"</xsl:analyze-string>"
        f"</xsl:template>"
    )


# ---------------------------------------------------------------------------
# Matching, non-matching, and captured groups


def test_matching_and_nonmatching_branches_report_groups() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="'ab-12 cd-34'" regex="([a-z]+)-([0-9]+)">
            <xsl:matching-substring>
              <xsl:sequence select="concat('[', regex-group(0), '|', regex-group(1), '|', regex-group(2), ']')"/>
            </xsl:matching-substring>
            <xsl:non-matching-substring>
              <xsl:sequence select="concat('{', ., '}')"/>
            </xsl:non-matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == [
        "[ab-12|ab|12]",
        "{ }",
        "[cd-34|cd|34]",
    ]


def test_only_matching_branch_is_allowed() -> None:
    source = analyze(
        "<xsl:matching-substring><xsl:sequence select=\"concat('M:', .)\"/></xsl:matching-substring>",
        regex="\\d+",
        select="'a1b22'",
    )
    assert values(lambda: transform(source, initial_template="main")) == ["M:1", "M:22"]


def test_only_nonmatching_branch_is_allowed() -> None:
    source = analyze(
        "<xsl:non-matching-substring><xsl:sequence select=\"concat('N:', .)\"/></xsl:non-matching-substring>",
        regex="\\d+",
        select="'a1b22'",
    )
    assert values(lambda: transform(source, initial_template="main")) == ["N:a", "N:b"]


def test_nonmatching_branch_has_no_captured_groups() -> None:
    source = analyze(
        """
        <xsl:matching-substring>
          <xsl:sequence select="concat('M:', regex-group(1))"/>
        </xsl:matching-substring>
        <xsl:non-matching-substring>
          <xsl:sequence select="concat('N:', regex-group(1))"/>
        </xsl:non-matching-substring>
        """,
        regex="(a)",
        select="'ab'",
    )
    assert values(lambda: transform(source, initial_template="main")) == ["M:a", "N:"]


def test_regex_group_out_of_range_and_negative_return_empty_strings() -> None:
    source = analyze(
        """
        <xsl:matching-substring>
          <xsl:sequence select="concat(regex-group(0), '|', regex-group(1), '|', regex-group(2), '|', regex-group(-1))"/>
        </xsl:matching-substring>
        """,
        regex="(a)",
        select="'a'",
    )
    assert values(lambda: transform(source, initial_template="main")) == ["a|a||"]


# ---------------------------------------------------------------------------
# Zero-length and global matching


def test_zero_length_matches_partition_the_whole_input() -> None:
    source = analyze(
        """
        <xsl:matching-substring>
          <xsl:sequence select="concat('M(', ., ')')"/>
        </xsl:matching-substring>
        <xsl:non-matching-substring>
          <xsl:sequence select="concat('N(', ., ')')"/>
        </xsl:non-matching-substring>
        """,
        regex="a*",
        select="'ba'",
    )
    assert values(lambda: transform(source, initial_template="main")) == [
        "M()",
        "N(b)",
        "M(a)",
        "M()",
    ]


def test_zero_length_match_at_end_does_not_loop() -> None:
    source = analyze(
        "<xsl:matching-substring><xsl:sequence select=\"concat('M(', ., ')')\"/></xsl:matching-substring>",
        regex="^",
        select="'abc'",
    )
    assert values(lambda: transform(source, initial_template="main")) == ["M()"]


def test_empty_select_is_a_zero_length_string() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="()" regex="^$">
            <xsl:matching-substring>
              <xsl:sequence select="'empty'"/>
            </xsl:matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == ["empty"]


def test_multiline_anchors_follow_xpath_newline_rules() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="'a&#10;'" regex="^" flags="m">
            <xsl:matching-substring><xsl:sequence select="'^'"/></xsl:matching-substring>
          </xsl:analyze-string>
          <xsl:analyze-string select="'a&#10;'" regex="$" flags="m">
            <xsl:matching-substring><xsl:sequence select="'$'"/></xsl:matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == ["^", "$", "$"]


# ---------------------------------------------------------------------------
# XPath regex grammar versus Python re


def test_xpath_word_and_space_classes_differ_from_python_re() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="'+'" regex="\\w">
            <xsl:matching-substring><xsl:sequence select="'word'"/></xsl:matching-substring>
            <xsl:non-matching-substring><xsl:sequence select="'not-word'"/></xsl:non-matching-substring>
          </xsl:analyze-string>
          <xsl:analyze-string select="'&#160;'" regex="\\s">
            <xsl:matching-substring><xsl:sequence select="'space'"/></xsl:matching-substring>
            <xsl:non-matching-substring><xsl:sequence select="'not-space'"/></xsl:non-matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == ["word", "not-space"]


def test_character_class_subtraction_and_unicode_categories() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="'bcdf ae'" regex="[a-z-[aeiou]]+">
            <xsl:matching-substring><xsl:sequence select="concat('C:', .)"/></xsl:matching-substring>
          </xsl:analyze-string>
          <xsl:analyze-string select="'ABC def'" regex="\\p{{Lu}}+">
            <xsl:matching-substring><xsl:sequence select="concat('U:', .)"/></xsl:matching-substring>
          </xsl:analyze-string>
          <xsl:analyze-string select="'name'" regex="\\i\\c*">
            <xsl:matching-substring><xsl:sequence select="concat('N:', .)"/></xsl:matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == [
        "C:bcdf",
        "U:ABC",
        "N:name",
    ]


def test_dot_excludes_carriage_return_and_newline_without_dot_all() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="'a&#13;b'" regex="a.b">
            <xsl:matching-substring><xsl:sequence select="'plain'"/></xsl:matching-substring>
            <xsl:non-matching-substring><xsl:sequence select="'plain-no'"/></xsl:non-matching-substring>
          </xsl:analyze-string>
          <xsl:analyze-string select="'a&#13;b'" regex="a.b" flags="s">
            <xsl:matching-substring><xsl:sequence select="'dot-all'"/></xsl:matching-substring>
            <xsl:non-matching-substring><xsl:sequence select="'dot-all-no'"/></xsl:non-matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == ["plain-no", "dot-all"]


def test_flags_i_m_s_x_and_q() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="'ABC'" regex="abc" flags="i">
            <xsl:matching-substring><xsl:sequence select="'i'"/></xsl:matching-substring>
          </xsl:analyze-string>
          <xsl:analyze-string select="'a&#10;b'" regex="^b" flags="m">
            <xsl:matching-substring><xsl:sequence select="'m'"/></xsl:matching-substring>
          </xsl:analyze-string>
          <xsl:analyze-string select="'a b'" regex="a b" flags="x">
            <xsl:matching-substring><xsl:sequence select="'x-outside'"/></xsl:matching-substring>
            <xsl:non-matching-substring><xsl:sequence select="'x-no'"/></xsl:non-matching-substring>
          </xsl:analyze-string>
          <xsl:analyze-string select="'a b'" regex="[a b]" flags="x">
            <xsl:matching-substring><xsl:sequence select="concat('x-class:', .)"/></xsl:matching-substring>
          </xsl:analyze-string>
          <xsl:analyze-string select="'.*'" regex=".*" flags="q">
            <xsl:matching-substring><xsl:sequence select="'q'"/></xsl:matching-substring>
            <xsl:non-matching-substring><xsl:sequence select="'q-no'"/></xsl:non-matching-substring>
          </xsl:analyze-string>
          <xsl:analyze-string select="'B. OBAMA'" regex="b. obama" flags="iq">
            <xsl:matching-substring><xsl:sequence select="'iq'"/></xsl:matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == [
        "i",
        "m",
        "x-no",
        "x-class:a",
        "x-class: ",
        "x-class:b",
        "q",
        "iq",
    ]


def test_regex_attribute_is_an_attribute_value_template() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="'123'" regex=".{{1,2}}">
            <xsl:matching-substring>
              <xsl:sequence select="concat('M:', .)"/>
            </xsl:matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == ["M:12", "M:3"]


def test_invalid_flags_and_regex_raise_instruction_error_codes() -> None:
    bad_flags = analyze(
        "<xsl:matching-substring/>",
        regex="a",
        select="'a'",
        flags="z",
    )
    assert error_code(lambda: transform(bad_flags, initial_template="main")) == "XTDE1145"

    for regex in ("[", "(a", "(?=a)", "\\q", "a{{2,1}}"):
        source = analyze("<xsl:matching-substring/>", regex=regex, select="'a'")
        assert (
            error_code(lambda source=source: transform(source, initial_template="main"))
            == "XTDE1140"
        )


# ---------------------------------------------------------------------------
# Nested state and calls


def test_nested_analyze_string_restores_outer_captures() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="'ou'" regex="(o)(u)">
            <xsl:matching-substring>
              <xsl:sequence select="concat('outer:', regex-group(1), regex-group(2))"/>
              <xsl:analyze-string select="'x1'" regex="([a-z])([0-9])">
                <xsl:matching-substring>
                  <xsl:sequence select="concat('inner:', regex-group(1), regex-group(2))"/>
                </xsl:matching-substring>
              </xsl:analyze-string>
              <xsl:sequence select="concat('restored:', regex-group(1), regex-group(2))"/>
            </xsl:matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == [
        "outer:ou",
        "inner:x1",
        "restored:ou",
    ]


def test_stylesheet_function_clears_captures_but_template_call_preserves_them() -> None:
    source = stylesheet(
        """
        <xsl:function name="my:group" as="xs:string">
          <xsl:sequence select="regex-group(1)"/>
        </xsl:function>
        <xsl:template name="capture">
          <xsl:sequence select="regex-group(1)"/>
        </xsl:template>
        <xsl:template name="main">
          <xsl:analyze-string select="'ab'" regex="(a)(b)">
            <xsl:matching-substring>
              <xsl:sequence select="concat('fn=', my:group())"/>
              <xsl:call-template name="capture"/>
            </xsl:matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == ["fn=", "a"]


def test_branch_context_position_and_size_cover_all_substrings() -> None:
    source = analyze(
        """
        <xsl:matching-substring>
          <xsl:sequence select="concat('M', position(), '/', last(), ':', .)"/>
        </xsl:matching-substring>
        <xsl:non-matching-substring>
          <xsl:sequence select="concat('N', position(), '/', last(), ':', .)"/>
        </xsl:non-matching-substring>
        """,
        regex="\\d",
        select="'a1b2'",
    )
    assert values(lambda: transform(source, initial_template="main")) == [
        "N1/4:a",
        "M2/4:1",
        "N3/4:b",
        "M4/4:2",
    ]


def test_branch_text_value_templates_follow_effective_expand_text() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="'a'" regex="(a)">
            <xsl:matching-substring expand-text="yes">[{regex-group(1)}]</xsl:matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert (
        transform(source, initial_template="main", delivery="document").principal_tree.string_value
        == "[a]"
    )


# ---------------------------------------------------------------------------
# Static content model


def test_analyze_string_requires_a_branch() -> None:
    source = stylesheet(
        '<xsl:template name="main"><xsl:analyze-string select="\'a\'" regex="a"/></xsl:template>'
    )
    assert error_code(lambda: compiled(source)) == "XTSE1130"


def test_analyze_string_requires_select_and_regex() -> None:
    for body in (
        '<xsl:analyze-string regex="a"><xsl:matching-substring/></xsl:analyze-string>',
        "<xsl:analyze-string select=\"'a'\"><xsl:matching-substring/></xsl:analyze-string>",
    ):
        source = stylesheet(f'<xsl:template name="main">{body}</xsl:template>')
        assert error_code(lambda source=source: compiled(source)) == "XTSE0010"


def test_analyze_string_content_model_is_enforced() -> None:
    cases = (
        "<xsl:non-matching-substring/><xsl:matching-substring/>",
        "<xsl:matching-substring/><xsl:matching-substring/>",
        "<xsl:non-matching-substring/><xsl:non-matching-substring/>",
        "text<xsl:matching-substring/>",
    )
    for content in cases:
        source = stylesheet(
            f'<xsl:template name="main"><xsl:analyze-string select="\'a\'" regex="a">'
            f"{content}</xsl:analyze-string></xsl:template>"
        )
        assert error_code(lambda source=source: compiled(source)) == "XTSE0010"


def test_analyze_string_select_converts_atomically_and_rejects_sequences() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="'1'" regex="1">
            <xsl:matching-substring><xsl:sequence select="'one'"/></xsl:matching-substring>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert values(lambda: transform(source, initial_template="main")) == ["one"]

    bad = stylesheet(
        """
        <xsl:template name="main">
          <xsl:analyze-string select="(1, 2)" regex="1">
            <xsl:matching-substring/>
          </xsl:analyze-string>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(bad, initial_template="main")) == "XPTY0004"


# ---------------------------------------------------------------------------
# Public regex API and grammar edge cases


def test_regex_module_rejects_invalid_argument_types() -> None:
    from pyxsd.xslt.runtime.text import (
        AnalyzedSubstring,
        analyze_string,
        compile_xpath_regex,
    )

    with pytest.raises(TypeError):
        compile_xpath_regex(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compile_xpath_regex("a", 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        analyze_string("a", "b")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        analyze_string(compile_xpath_regex("a"), 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AnalyzedSubstring("yes", "x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AnalyzedSubstring(True, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AnalyzedSubstring(True, "x", ("a", 1))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "pattern",
    (
        "]",
        "\\",
        "[z-a]",
        "[[]",
        "[a\\",
        "[a-[b]",
        "\\p",
        "\\p{}",
        "\\p{NoSuch}",
        "\\p{IsNoSuch}",
        "\\1",
        "\\0",
        "(a)\\2",
        "(a\\1)",
        "*a",
        "{2}",
        "a{",
        "a{2,1}",
        "a)",
    ),
)
def test_xpath_regex_rejects_python_only_or_malformed_constructs(pattern: str) -> None:
    from pyxsd.xslt.runtime.text import XPathRegexError, compile_xpath_regex

    with pytest.raises(XPathRegexError) as caught:
        compile_xpath_regex(pattern)
    assert caught.value.code == "FORX0002"


def test_xpath_regex_accepts_normative_extensions() -> None:
    from pyxsd.xslt.runtime.text import analyze_string, compile_xpath_regex

    def matches(pattern: str, text: str) -> list[str]:
        return [
            piece.value
            for piece in analyze_string(compile_xpath_regex(pattern), text)
            if piece.matched
        ]

    assert matches("a|b", "ab") == ["a", "b"]
    assert matches("a*?", "aa") == ["", "", ""]
    assert matches("a{1,2}?", "aa") == ["a", "a"]
    assert matches("(?:a)", "aa") == ["a", "a"]
    assert matches("^*", "a") == ["", ""]
    assert matches("$+", "aa") == [""]
    assert matches("[^a]", "ab") == ["b"]
    assert matches("\\p{IsLu}", "AB") == ["A", "B"]
    assert matches("\\P{Lu}", "ab") == ["a", "b"]
    assert matches("(a)\\1", "aa") == ["aa"]
    assert matches("\\S", "a b") == ["a", "b"]
    assert matches("\\W", "!!") == ["!", "!"]
    assert matches("[a-[b]]", "ac") == ["a"]
    assert matches("\\n", "a\nb") == ["\n"]
    assert matches("[\\]]", "]") == ["]"]


def test_analyze_string_rejects_foreign_and_unknown_children() -> None:
    for content in ("<foreign/>", '<xsl:if test="true()"/>'):
        source = stylesheet(
            f'<xsl:template name="main"><xsl:analyze-string select="\'a\'" regex="a">'
            f"{content}</xsl:analyze-string></xsl:template>"
        )
        assert error_code(lambda source=source: compiled(source)) == "XTSE0010"


def test_analyze_string_fallback_must_follow_branches() -> None:
    before = stylesheet(
        '<xsl:template name="main"><xsl:analyze-string select="\'a\'" regex="a">'
        "<xsl:fallback/><xsl:matching-substring/>"
        "</xsl:analyze-string></xsl:template>"
    )
    assert error_code(lambda: compiled(before)) == "XTSE0010"

    after = stylesheet(
        '<xsl:template name="main"><xsl:analyze-string select="\'a\'" regex="a">'
        "<xsl:matching-substring><xsl:sequence select=\"'ok'\"/></xsl:matching-substring>"
        "<xsl:fallback/>"
        "</xsl:analyze-string></xsl:template>"
    )
    assert values(lambda: transform(after, initial_template="main")) == ["ok"]


def test_branch_instruction_outside_analyze_string_is_static_error() -> None:
    source = stylesheet('<xsl:template name="main"><xsl:matching-substring/></xsl:template>')
    assert error_code(lambda: compiled(source)) == "XTSE0010"


def test_analyze_string_select_rejects_non_string_atomics_and_function_items() -> None:
    for expression in ("1", "map{}", "function() { 'a' }"):
        source = stylesheet(
            f'<xsl:template name="main"><xsl:analyze-string select="{expression}" regex="a">'
            "<xsl:matching-substring/>"
            "</xsl:analyze-string></xsl:template>"
        )
        assert (
            error_code(lambda source=source: transform(source, initial_template="main"))
            == "XPTY0004"
        )


def test_analyze_string_select_accepts_a_node_string_value() -> None:
    source = stylesheet(
        '<xsl:template name="main"><xsl:analyze-string select="/doc/n" regex="2">'
        "<xsl:matching-substring><xsl:sequence select=\"'node'\"/></xsl:matching-substring>"
        "</xsl:analyze-string></xsl:template>"
    )
    assert values(lambda: transform(source, b"<doc><n>2</n></doc>", initial_template="main")) == [
        "node"
    ]


def test_compile_analyze_string_ignores_whitespace_text() -> None:
    from pyxsd.xslt.compiler.context import StaticContext
    from pyxsd.xslt.compiler.instructions import Attribute, Instruction, TextNode
    from pyxsd.xslt.processor import _compile_analyze_string

    instruction = Instruction(
        name=f"{{{XSLT_NS}}}analyze-string",
        attributes=(
            Attribute(name="select", value="'a'"),
            Attribute(name="regex", value="a"),
        ),
        children=(TextNode(" "),),
    )
    with pytest.raises(XsltStaticError) as caught:
        _compile_analyze_string(instruction, StaticContext(), None)  # type: ignore[arg-type]
    assert caught.value.code == "XTSE1130"
