"""Compact-source decoding, escape expansion, and original-source spans.

These tests pin the five lexical stages of the compact syntax specification's
Appendix A.2 up to the hand-off to tokenization: encoding selection, BOM
stripping, newline markers, escape interpretation, and the mapping from the
expanded character sequence back to exact original-source addresses.
"""

from __future__ import annotations

import dataclasses

import pytest

from pyxsd.locations import SourceTextSpan
from pyxsd.relaxng.compact.source import NEWLINE, CompactSource, NewlineMarker
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError


def decode(content: bytes) -> CompactSource:
    return CompactSource.from_bytes(content, source_id="schema.rnc")


def expanded(source: CompactSource) -> str:
    return "".join(
        "\n" if isinstance(character, NewlineMarker) else character
        for character in source.characters
    )


def test_utf8_source_keeps_decoded_text_and_plain_characters() -> None:
    source = decode(b"element foo { text }")

    assert source.source_id == "schema.rnc"
    assert source.text == "element foo { text }"
    assert expanded(source) == "element foo { text }"
    assert source.span(0, 7) == SourceTextSpan(0, 7, 1, 1, 1, 8)


def test_utf16_little_endian_bom_selects_little_endian() -> None:
    source = decode(b"\xff\xfe" + "element".encode("utf-16-le"))

    assert source.text == "element"
    assert expanded(source) == "element"
    assert source.span(0, 7) == SourceTextSpan(0, 7, 1, 1, 1, 8)


def test_utf16_big_endian_bom_selects_big_endian() -> None:
    source = decode(b"\xfe\xff" + "élément".encode("utf-16-be"))

    assert source.text == "élément"
    assert expanded(source) == "élément"
    assert source.span(0, 1) == SourceTextSpan(0, 1, 1, 1, 1, 2)


def test_utf8_bom_is_stripped_and_offsets_start_at_the_content() -> None:
    source = decode(b"\xef\xbb\xbfelement")

    assert source.text == "element"
    assert source.span(0, 1) == SourceTextSpan(0, 1, 1, 1, 1, 2)


def test_only_one_leading_bom_is_stripped() -> None:
    source = decode(b"\xef\xbb\xbf\xef\xbb\xbfx")

    assert source.text == "\ufeffx"
    assert expanded(source) == "\ufeffx"


def test_a_bom_without_content_is_an_empty_source() -> None:
    source = decode(b"\xef\xbb\xbf")

    assert source.text == ""
    assert source.characters == ()
    assert source.span(0, 0) == SourceTextSpan(0, 0, 1, 1, 1, 1)


@pytest.mark.parametrize(
    ("content", "label"),
    [
        (b"element \xff", "UTF-8"),
        (b"\xff\xfe" + b"a", "UTF-16LE"),
        (b"\xfe\xff" + b"\x00", "UTF-16BE"),
        (b"\xff\xfe\x00\xd8", "UTF-16LE"),
    ],
)
def test_invalid_byte_sequences_are_rejected_without_replacement(
    content: bytes, label: str
) -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        decode(content)

    error = caught.value
    assert str(error).startswith(f"the compact source is not valid {label}")
    assert error.location is None
    assert "\ufffd" not in str(error)


def test_newline_forms_become_one_distinct_marker() -> None:
    source = decode(b"a\r\nb\rc\nd")

    assert [isinstance(character, NewlineMarker) for character in source.characters] == [
        False,
        True,
        False,
        True,
        False,
        True,
        False,
    ]
    assert expanded(source) == "a\nb\nc\nd"
    assert source.characters[1] is NEWLINE
    assert source.span(1, 2) == SourceTextSpan(1, 3, 1, 2, 2, 1)
    assert source.span(2, 3) == SourceTextSpan(3, 4, 2, 1, 2, 2)
    assert source.span(4, 5) == SourceTextSpan(5, 6, 3, 1, 3, 2)


def test_newline_marker_has_a_stable_repr() -> None:
    assert repr(NEWLINE) == "NEWLINE"


@pytest.mark.parametrize("content", [b"\\x{66}", b"\\xx{66}"])
def test_one_or_more_x_prefixes_are_accepted_in_an_escape(content: bytes) -> None:
    source = decode(content)

    assert source.text == content.decode()
    assert source.characters == ("f",)
    text = content.decode()
    assert source.span(0, 1) == SourceTextSpan(0, len(text), 1, 1, 1, len(text) + 1)


def test_escaped_characters_keep_their_escape_sequence_in_the_source_map() -> None:
    source = decode(b"a\\x{62}c")

    assert source.characters == ("a", "b", "c")
    assert source.span(1, 2) == SourceTextSpan(1, 7, 1, 2, 1, 8)
    assert source.span(1, 3) == SourceTextSpan(1, 8, 1, 2, 1, 9)


def test_escaped_newline_characters_are_not_newline_markers() -> None:
    source = decode(b"\\x{A}")

    assert source.characters == ("\n",)
    assert source.characters[0] is not NEWLINE
    assert not isinstance(source.characters[0], NewlineMarker)
    assert source.span(0, 1) == SourceTextSpan(0, 5, 1, 1, 1, 6)


def test_escaped_carriage_return_is_an_ordinary_character() -> None:
    source = decode(b"\\x{D}")

    assert source.characters == ("\r",)
    assert not isinstance(source.characters[0], NewlineMarker)


def test_escape_scanning_continues_after_the_replacement() -> None:
    source = decode(b"\\x{5C}x{5C}")

    assert source.characters == ("\\", "x", "{", "5", "C", "}")


def test_a_backslash_before_an_escape_is_not_an_odd_even_rule() -> None:
    source = decode(b"\\\\x{41}")

    assert source.characters == ("\\", "A")


@pytest.mark.parametrize("content", [b"\\x", b"\\{", b"\\xq", b"\\"])
def test_a_backslash_without_an_escape_open_is_an_ordinary_character(content: bytes) -> None:
    source = decode(content)

    assert source.characters == tuple(content.decode())


@pytest.mark.parametrize("content", [b"\\x{9}", b"\\x{20}", b"\\x{10FFFF}"])
def test_legal_xml_characters_are_accepted_in_escapes(content: bytes) -> None:
    source = decode(content)

    assert source.characters == (chr(int(content[3:-1], 16)),)


@pytest.mark.parametrize(
    ("content", "message", "end"),
    [
        (b"\\x{}", "needs at least one hexadecimal digit", 4),
        (b"\\x{41", "missing its closing '}'", 5),
        (b"\\x{4g}", "only hexadecimal digits", 5),
        (b"\\x{0}", "does not denote a legal XML character", 5),
        (b"\\x{D800}", "does not denote a legal XML character", 8),
        (b"\\x{FFFF}", "does not denote a legal XML character", 8),
    ],
)
def test_escape_failures_report_the_exact_original_span(
    content: bytes, message: str, end: int
) -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        decode(content)

    error = caught.value
    assert message in str(error)
    assert error.location is not None
    assert error.location.source == "schema.rnc"
    assert error.location.span == SourceTextSpan(0, end, 1, 1, 1, end + 1)


def test_a_huge_hex_number_is_rejected_as_an_xml_character_without_overflow() -> None:
    content = b"\\x{1" + b"0" * 5000 + b"}"

    with pytest.raises(RelaxNGCompactSyntaxError) as caught:
        decode(content)

    assert "does not denote a legal XML character" in str(caught.value)


def test_line_positions_follow_original_newlines() -> None:
    source = decode(b"one\r\ntwo\nthree")

    assert source.span(4, 5) == SourceTextSpan(5, 6, 2, 1, 2, 2)
    assert source.span(7, 8) == SourceTextSpan(8, 9, 2, 4, 3, 1)


def test_an_empty_span_at_the_end_addresses_the_end_of_the_source() -> None:
    source = decode(b"ab")

    assert source.span(0, 0) == SourceTextSpan(0, 0, 1, 1, 1, 1)
    assert source.span(2, 2) == SourceTextSpan(2, 2, 1, 3, 1, 3)


def test_span_requests_outside_the_expanded_range_are_rejected() -> None:
    source = decode(b"ab")

    with pytest.raises(ValueError):
        source.span(-1, 1)
    with pytest.raises(ValueError):
        source.span(2, 1)
    with pytest.raises(ValueError):
        source.span(0, 3)


def test_compact_source_is_immutable() -> None:
    source = decode(b"a")

    with pytest.raises(dataclasses.FrozenInstanceError):
        source.text = "b"  # type: ignore[misc]
