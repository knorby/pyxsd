"""Longest-match tokenization for the RELAX NG compact syntax.

The lexer is the last of the specification's lexical stages.  These tests pin
every token class, the longest-match boundaries between them, literal
delimiter forms, documentation grouping and continuation, comments, quoted
identifiers, XML NCName ranges, and the shared token budget.
"""

from __future__ import annotations

import dataclasses

import pytest

from pyxsd.locations import SourceTextSpan
from pyxsd.relaxng.compact.lexer import KEYWORDS, Token, TokenKind, lex_compact
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError, RelaxNGLimitError
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits

SOURCE_ID = "schema.rnc"


def lex(
    source: str | bytes,
    *,
    limits: RelaxNGLimits | None = None,
    counter: BudgetCounter | None = None,
) -> tuple[Token, ...]:
    content = source.encode("utf-8") if isinstance(source, str) else source
    budget = counter if counter is not None else BudgetCounter("compile", limits)
    return lex_compact(content, source_id=SOURCE_ID, budget=budget)


def pairs(tokens: tuple[Token, ...]) -> list[tuple[TokenKind, str]]:
    return [(token.kind, token.value) for token in tokens]


def span_of(source: str | bytes, index: int = 0) -> SourceTextSpan:
    return lex(source)[index].span


def test_minimal_schema_tokens_keep_kinds_values_and_spans() -> None:
    source = "element foo { text }"
    tokens = lex(source)

    assert pairs(tokens) == [
        (TokenKind.KEYWORD, "element"),
        (TokenKind.IDENTIFIER, "foo"),
        (TokenKind.SYMBOL, "{"),
        (TokenKind.KEYWORD, "text"),
        (TokenKind.SYMBOL, "}"),
        (TokenKind.EOF, ""),
    ]
    assert tokens[0].span == SourceTextSpan(0, 7, 1, 1, 1, 8)
    assert tokens[1].span == SourceTextSpan(8, 11, 1, 9, 1, 12)
    assert tokens[-1].span == SourceTextSpan(
        len(source), len(source), 1, len(source) + 1, 1, len(source) + 1
    )


def test_empty_source_produces_only_an_eof_token() -> None:
    tokens = lex("")

    assert pairs(tokens) == [(TokenKind.EOF, "")]
    assert tokens[0].span == SourceTextSpan(0, 0, 1, 1, 1, 1)


def test_the_lexer_emits_every_token_kind() -> None:
    tokens = lex('## doc\nelement foo { "x" } \\foo a:b a:* | &')

    assert {token.kind for token in tokens} == set(TokenKind)


@pytest.mark.parametrize(
    "symbol", ["{", "}", "(", ")", "[", "]", ",", "=", "?", "*", "+", "-", "~", "|", "&"]
)
def test_each_single_character_symbol_is_one_token(symbol: str) -> None:
    assert pairs(lex(symbol)) == [(TokenKind.SYMBOL, symbol), (TokenKind.EOF, "")]


@pytest.mark.parametrize("symbol", [">>", "|=", "&="])
def test_each_two_character_symbol_is_one_token(symbol: str) -> None:
    assert pairs(lex(symbol)) == [(TokenKind.SYMBOL, symbol), (TokenKind.EOF, "")]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("|=", [(TokenKind.SYMBOL, "|=")]),
        ("| =", [(TokenKind.SYMBOL, "|"), (TokenKind.SYMBOL, "=")]),
        ("&=", [(TokenKind.SYMBOL, "&=")]),
        ("& =", [(TokenKind.SYMBOL, "&"), (TokenKind.SYMBOL, "=")]),
    ],
)
def test_operator_longest_match_boundaries(
    source: str, expected: list[tuple[TokenKind, str]]
) -> None:
    assert pairs(lex(source)) == [*expected, (TokenKind.EOF, "")]


def test_tilde_is_a_parser_visible_symbol_between_literal_segments() -> None:
    assert pairs(lex('"a" ~ "b"')) == [
        (TokenKind.LITERAL_SEGMENT, "a"),
        (TokenKind.SYMBOL, "~"),
        (TokenKind.LITERAL_SEGMENT, "b"),
        (TokenKind.EOF, ""),
    ]


def test_literal_segment_forms_keep_their_content() -> None:
    assert pairs(lex("\"a\" 'b' \"\"\"c\"d\"\"\" '''e'f'''")) == [
        (TokenKind.LITERAL_SEGMENT, "a"),
        (TokenKind.LITERAL_SEGMENT, "b"),
        (TokenKind.LITERAL_SEGMENT, 'c"d'),
        (TokenKind.LITERAL_SEGMENT, "e'f"),
        (TokenKind.EOF, ""),
    ]


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ('"""a"b"""', 'a"b'),
        ('"""a""b"""', 'a""b'),
        ("'''a'b'''", "a'b"),
        ("'''a''b'''", "a''b"),
    ],
)
def test_triple_delimited_literals_allow_one_or_two_quotes(source: str, value: str) -> None:
    assert pairs(lex(source)) == [(TokenKind.LITERAL_SEGMENT, value), (TokenKind.EOF, "")]


@pytest.mark.parametrize("delimiter", ['"""', "'''"])
def test_triple_delimited_literals_accept_raw_newlines(delimiter: str) -> None:
    source = f"{delimiter}a\nb{delimiter}"
    tokens = lex(source)

    assert pairs(tokens) == [(TokenKind.LITERAL_SEGMENT, "a\nb"), (TokenKind.EOF, "")]
    assert tokens[0].span.end_line == 2
    assert tokens[0].span.end_column == 5


def test_empty_literals_use_the_shortest_delimiters() -> None:
    assert pairs(lex('""')) == [(TokenKind.LITERAL_SEGMENT, ""), (TokenKind.EOF, "")]
    assert pairs(lex("''")) == [(TokenKind.LITERAL_SEGMENT, ""), (TokenKind.EOF, "")]
    assert pairs(lex('""""')) == [
        (TokenKind.LITERAL_SEGMENT, ""),
        (TokenKind.LITERAL_SEGMENT, ""),
        (TokenKind.EOF, ""),
    ]
    assert pairs(lex('""""""')) == [(TokenKind.LITERAL_SEGMENT, ""), (TokenKind.EOF, "")]
    assert span_of('""""""').end == 6


def test_escaped_newline_inside_a_single_line_literal_is_content() -> None:
    tokens = lex(b'"\\x{A}"')

    assert pairs(tokens) == [(TokenKind.LITERAL_SEGMENT, "\n"), (TokenKind.EOF, "")]
    assert tokens[0].span == SourceTextSpan(0, 7, 1, 1, 1, 8)


def test_escapes_inside_literals_keep_their_original_span() -> None:
    tokens = lex(b'"\\x{66}\\x{6f}\\x{6f}"')

    assert pairs(tokens) == [(TokenKind.LITERAL_SEGMENT, "foo"), (TokenKind.EOF, "")]
    assert tokens[0].span == SourceTextSpan(0, 20, 1, 1, 1, 21)


def test_raw_newline_inside_a_single_line_literal_is_rejected() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex('"a\nb"')

    error = caught.value
    assert "literal segment is not terminated" in str(error)
    assert error.location is not None
    assert error.location.source == SOURCE_ID
    assert error.location.span == SourceTextSpan(0, 3, 1, 1, 2, 1)


def test_an_illegal_xml_character_cannot_be_literal_content() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex(b'"a\x00b"')

    assert caught.value.location is not None
    assert caught.value.location.span == SourceTextSpan(0, 3, 1, 1, 1, 4)


@pytest.mark.parametrize(
    ("source", "start"), [('"abc', 0), ("'abc", 0), ('"""abc', 2), ("'''abc", 2)]
)
def test_unterminated_literal_segments_are_rejected(source: str, start: int) -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex(source)

    span = caught.value.location.span
    assert span is not None
    assert span.start == start
    assert span.end == len(source)


def test_a_triple_delimiter_without_a_terminator_falls_back_to_an_empty_literal() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex('"""')

    span = caught.value.location.span
    assert span is not None
    assert (span.start, span.end) == (2, 3)


def test_quoted_identifier_drops_the_backslash() -> None:
    tokens = lex("\\element")

    assert pairs(tokens) == [(TokenKind.QUOTED_IDENTIFIER, "element"), (TokenKind.EOF, "")]
    assert tokens[0].span == SourceTextSpan(0, 8, 1, 1, 1, 9)


def test_quoted_identifier_uses_ncname_ranges() -> None:
    assert pairs(lex("\\élément"))[0] == (TokenKind.QUOTED_IDENTIFIER, "élément")


def test_identifier_and_keyword_are_distinguished_by_the_ncname_value() -> None:
    assert pairs(lex("element elementx")) == [
        (TokenKind.KEYWORD, "element"),
        (TokenKind.IDENTIFIER, "elementx"),
        (TokenKind.EOF, ""),
    ]


@pytest.mark.parametrize("keyword", sorted(KEYWORDS))
def test_every_keyword_tokenizes_as_a_keyword(keyword: str) -> None:
    assert pairs(lex(keyword)) == [(TokenKind.KEYWORD, keyword), (TokenKind.EOF, "")]


@pytest.mark.parametrize("name", ["élément", "漢字", "e\u0301", "a\u00b7b", "_x", "a-b", "a.b"])
def test_xml_ncname_ranges_are_honoured(name: str) -> None:
    assert pairs(lex(name)) == [(TokenKind.IDENTIFIER, name), (TokenKind.EOF, "")]


@pytest.mark.parametrize("source", ["\u0301e", "\u00b7", "1a"])
def test_characters_that_cannot_start_an_ncname_are_rejected(source: str) -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex(source)

    span = caught.value.location.span
    assert span is not None
    assert (span.start, span.end) == (0, 1)


def test_a_hyphen_between_ncname_characters_stays_in_the_identifier() -> None:
    assert pairs(lex("a-b")) == [(TokenKind.IDENTIFIER, "a-b"), (TokenKind.EOF, "")]
    assert pairs(lex("a - b")) == [
        (TokenKind.IDENTIFIER, "a"),
        (TokenKind.SYMBOL, "-"),
        (TokenKind.IDENTIFIER, "b"),
        (TokenKind.EOF, ""),
    ]


def test_cname_and_ns_name_are_single_tokens() -> None:
    tokens = lex("a:b a:*")

    assert pairs(tokens) == [
        (TokenKind.CNAME, "a:b"),
        (TokenKind.NS_NAME, "a"),
        (TokenKind.EOF, ""),
    ]
    assert tokens[0].span == SourceTextSpan(0, 3, 1, 1, 1, 4)
    assert tokens[1].span == SourceTextSpan(4, 7, 1, 5, 1, 8)


def test_cname_and_ns_name_win_over_their_identifier_prefix() -> None:
    assert pairs(lex("text:foo"))[0] == (TokenKind.CNAME, "text:foo")
    assert pairs(lex("start:*"))[0] == (TokenKind.NS_NAME, "start")


def test_escaped_characters_contribute_to_a_single_cname() -> None:
    assert pairs(lex(b"\\x{66}:x")) == [(TokenKind.CNAME, "f:x"), (TokenKind.EOF, "")]
    assert span_of(b"\\x{66}:x").start == 0
    assert span_of(b"\\x{66}:x").end == 8


def test_a_colon_without_a_local_name_is_not_part_of_a_token() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex("a:")

    span = caught.value.location.span
    assert span is not None
    assert (span.start, span.end) == (1, 2)


def test_a_second_colon_starts_no_token() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex("a:b:c")

    span = caught.value.location.span
    assert span is not None
    assert (span.start, span.end) == (3, 4)


def test_a_leading_colon_starts_no_token() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex(":a")

    span = caught.value.location.span
    assert span is not None
    assert (span.start, span.end) == (0, 1)


def test_an_identifier_span_covers_its_escape_sequences() -> None:
    tokens = lex(b"\\x{66}oo")

    assert pairs(tokens) == [(TokenKind.IDENTIFIER, "foo"), (TokenKind.EOF, "")]
    assert tokens[0].span == SourceTextSpan(0, 8, 1, 1, 1, 9)


def test_hash_comment_is_a_discarded_separator() -> None:
    assert pairs(lex("# comment\nelement")) == [
        (TokenKind.KEYWORD, "element"),
        (TokenKind.EOF, ""),
    ]
    assert pairs(lex("#")) == [(TokenKind.EOF, "")]
    assert pairs(lex("#\nx")) == [(TokenKind.IDENTIFIER, "x"), (TokenKind.EOF, "")]


def test_a_comment_may_contain_hash_after_its_first_character() -> None:
    assert pairs(lex("# a # b\nx")) == [(TokenKind.IDENTIFIER, "x"), (TokenKind.EOF, "")]


def test_double_hash_is_a_documentation_token() -> None:
    tokens = lex("## documentation\nelement")

    assert pairs(tokens) == [
        (TokenKind.DOCUMENTATION, "documentation"),
        (TokenKind.KEYWORD, "element"),
        (TokenKind.EOF, ""),
    ]
    assert tokens[0].span == SourceTextSpan(0, 16, 1, 1, 1, 17)


def test_empty_and_hash_prefixed_documentation_lines() -> None:
    assert pairs(lex("##")) == [(TokenKind.DOCUMENTATION, ""), (TokenKind.EOF, "")]
    assert pairs(lex("####")) == [(TokenKind.DOCUMENTATION, ""), (TokenKind.EOF, "")]
    assert pairs(lex("### foo")) == [(TokenKind.DOCUMENTATION, "foo"), (TokenKind.EOF, "")]
    assert pairs(lex("##  two")) == [(TokenKind.DOCUMENTATION, " two"), (TokenKind.EOF, "")]


def test_adjacent_documentation_lines_merge_into_one_token() -> None:
    source = "## first\n## second"
    tokens = lex(source)

    assert pairs(tokens) == [(TokenKind.DOCUMENTATION, "first\nsecond"), (TokenKind.EOF, "")]
    assert tokens[0].span == SourceTextSpan(0, len(source), 1, 1, 2, 10)


def test_documentation_content_need_not_start_with_a_space() -> None:
    assert pairs(lex("##foo bar")) == [
        (TokenKind.DOCUMENTATION, "foo bar"),
        (TokenKind.EOF, ""),
    ]


def test_a_triple_literal_with_an_illegal_character_falls_back() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex(b'"""a\x00b"""')

    span = caught.value.location.span
    assert span is not None
    assert (span.start, span.end) == (2, 5)


def test_an_escaped_newline_ends_documentation_line_content() -> None:
    assert pairs(lex(b"##\\x{A}foo")) == [
        (TokenKind.DOCUMENTATION, ""),
        (TokenKind.IDENTIFIER, "foo"),
        (TokenKind.EOF, ""),
    ]


def test_documentation_continuation_accepts_indentation() -> None:
    assert pairs(lex("## first\n   ## second")) == [
        (TokenKind.DOCUMENTATION, "first\nsecond"),
        (TokenKind.EOF, ""),
    ]


def test_a_blank_line_ends_documentation_continuation() -> None:
    assert pairs(lex("## first\n\n## second")) == [
        (TokenKind.DOCUMENTATION, "first"),
        (TokenKind.DOCUMENTATION, "second"),
        (TokenKind.EOF, ""),
    ]
    assert pairs(lex("## first\n   \n## second")) == [
        (TokenKind.DOCUMENTATION, "first"),
        (TokenKind.DOCUMENTATION, "second"),
        (TokenKind.EOF, ""),
    ]


def test_documentation_only_continues_on_a_documentation_line() -> None:
    assert pairs(lex("## first\nelement")) == [
        (TokenKind.DOCUMENTATION, "first"),
        (TokenKind.KEYWORD, "element"),
        (TokenKind.EOF, ""),
    ]
    assert pairs(lex("## first\n  # comment\n## second")) == [
        (TokenKind.DOCUMENTATION, "first"),
        (TokenKind.DOCUMENTATION, "second"),
        (TokenKind.EOF, ""),
    ]


def test_an_escaped_newline_continues_documentation() -> None:
    assert pairs(lex(b"## first\\x{A}## second")) == [
        (TokenKind.DOCUMENTATION, "first\nsecond"),
        (TokenKind.EOF, ""),
    ]


def test_a_crlf_continuation_contributes_one_newline() -> None:
    assert pairs(lex("## first\r\n## second")) == [
        (TokenKind.DOCUMENTATION, "first\nsecond"),
        (TokenKind.EOF, ""),
    ]


def test_documentation_lines_without_content_join_with_a_newline() -> None:
    assert pairs(lex("##\n##")) == [(TokenKind.DOCUMENTATION, "\n"), (TokenKind.EOF, "")]


def test_a_documentation_token_stops_before_an_illegal_character() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex(b"## ok\x00")

    span = caught.value.location.span
    assert span is not None
    assert (span.start, span.end) == (5, 6)


def test_an_escaped_carriage_return_is_not_a_separator() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex(b"a\\x{D}b")

    span = caught.value.location.span
    assert span is not None
    assert (span.start, span.end) == (1, 6)


@pytest.mark.parametrize("separator", [b"\\x{A}", b"\\x{9}"])
def test_escaped_xml_whitespace_is_a_separator(separator: bytes) -> None:
    assert pairs(lex(b"a" + separator + b"b")) == [
        (TokenKind.IDENTIFIER, "a"),
        (TokenKind.IDENTIFIER, "b"),
        (TokenKind.EOF, ""),
    ]


def test_a_comment_line_stops_before_an_illegal_character() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex(b"# ok\x00")

    span = caught.value.location.span
    assert span is not None
    assert (span.start, span.end) == (4, 5)


@pytest.mark.parametrize("source", ["%", ">", ":", ";", "@", "\\", "\\1"])
def test_unmatched_characters_are_lexical_errors(source: str) -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex(source)

    error = caught.value
    assert "no token matches this character" in str(error)
    assert error.found == repr(source[0])
    assert error.location is not None
    assert error.location.span == SourceTextSpan(0, 1, 1, 1, 1, 2)


def test_an_illegal_character_between_tokens_reports_its_span() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        lex(b"a\x00b")

    error = caught.value
    assert "no token matches this character" in str(error)
    assert error.found == repr("\x00")
    assert error.location is not None
    assert error.location.span == SourceTextSpan(1, 2, 1, 2, 1, 3)


def test_token_budget_counts_non_eof_tokens_exactly() -> None:
    assert len(lex("a b", limits=RelaxNGLimits(max_compact_tokens=2))) == 3
    assert lex("", limits=RelaxNGLimits(max_compact_tokens=0))[0].kind is TokenKind.EOF

    with pytest.raises(RelaxNGLimitError) as caught:
        lex("a b", limits=RelaxNGLimits(max_compact_tokens=1))

    error = caught.value
    assert error.budget == "max_compact_tokens"
    assert error.configured == 1
    assert error.observed == 2


def test_token_budget_is_cumulative_across_sources() -> None:
    counter = BudgetCounter("compile", RelaxNGLimits(max_compact_tokens=2))

    assert len(lex("a", counter=counter)) == 2
    assert len(lex("b", counter=counter)) == 2

    with pytest.raises(RelaxNGLimitError) as caught:
        lex("c", counter=counter)

    assert caught.value.observed == 3


def test_token_is_immutable() -> None:
    token = lex("a")[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        token.value = "b"  # type: ignore[misc]
