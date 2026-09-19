"""Unit tests for the XSD regular-expression shorthand rewrite layer.

XSD 1.1 Appendix G defines ``\\w``, ``\\W``, ``\\s`` and ``\\S`` differently
from Python's ``re`` module:

* ``\\w`` is every character *except* the punctuation, separator and other
  categories, so marks (``Mn``/``Mc``/``Me``), symbols (``Sm``/``Sc``/``Sk``/
  ``So``) and astral letters are word characters while ``_`` (``Pc``) is not.
* ``\\s`` is exactly ``[#x20\\t\\n\\r]``.

elementpath passes the shorthands straight through to Python, so pyxsd
rewrites the *translated* pattern before handing it to ``re``.  These tests
pin the rewrite for the BMP and the astral planes, both positively and via
the complement, and guard against elementpath output drift.
"""

import io
import re

import pytest
from elementpath.regex import translate_pattern

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD
from pyxsd.regex_charset import reject_malformed_escapes, rewrite_xsd_shorthands

SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
{body}
</xs:schema>"""

ELEMENT = '<xs:element name="r">{simple}</xs:element>'
SIMPLE = '<xs:simpleType><xs:restriction base="{base}">{facets}</xs:restriction></xs:simpleType>'


def parse(body, xml, mode=ParseModes.NAMESPACED):
    parser = PyXSD(
        io.StringIO(xml),
        io.StringIO(SCHEMA.format(body=body)),
        xmlFileOutput=False,
        transformOutputName=None,
        mode=mode,
    )
    return parser.report


def errors(report):
    return [(issue.code, issue.message) for issue in report.errors]


def element(base, facets):
    return ELEMENT.format(simple=SIMPLE.format(base=base, facets=facets))


def fullmatch(pattern, text):
    """Whether the rewritten pattern matches the whole of *text*."""
    return re.fullmatch(rewrite_xsd_shorthands(pattern), text) is not None


# --- \\w / \\W ---------------------------------------------------------------


def test_word_includes_combining_mark_bmp():
    assert fullmatch(r"\w", "\u064b")


def test_word_includes_astral_mark():
    assert fullmatch(r"\w", "\U0001d1ad")


def test_word_includes_symbol():
    assert fullmatch(r"\w", "\u2044")
    assert fullmatch(r"\w", "\u309b")


def test_word_includes_astral_letter():
    assert fullmatch(r"\w", "\U0001d7a8")


def test_word_excludes_underscore():
    assert not fullmatch(r"\w", "_")


def test_word_rejects_whitespace_control():
    assert not fullmatch(r"\w", " ")
    assert not fullmatch(r"\w", "\n")


def test_nonword_is_the_complement():
    assert fullmatch(r"\W", "_")
    assert fullmatch(r"\W", " ")
    assert not fullmatch(r"\W", "\u064b")
    assert not fullmatch(r"\W", "\U0001d7a8")


def test_word_complement_inside_class():
    assert fullmatch(r"[\W]", "_")
    assert not fullmatch(r"[\W]", "\u064b")


def test_word_splice_inside_class():
    assert fullmatch(r"[\w]", "\u064b")
    assert not fullmatch(r"[\w]", "_")


# --- \\s / \\S ---------------------------------------------------------------


def test_space_matches_only_the_xsd_four():
    for text in (" ", "\t", "\n", "\r"):
        assert fullmatch(r"\s", text)


def test_space_rejects_other_whitespace():
    for text in ("\u00a0", "\u000b", "\u000c", "\u2028", "\u2003"):
        assert not fullmatch(r"\s", text)


def test_nonspace_complement():
    assert fullmatch(r"\S", "\u00a0")
    assert fullmatch(r"\S", "\u000b")
    assert not fullmatch(r"\S", " ")


def test_space_inside_class_splice():
    assert fullmatch(r"[\s]", "\n")
    assert not fullmatch(r"[\s]", "\u00a0")


def test_nonspace_inside_class_splice():
    assert fullmatch(r"[\S]", "\u00a0")
    assert not fullmatch(r"[\S]", " ")


# --- escape handling ---------------------------------------------------------


def test_escaped_backslash_is_not_rewritten():
    assert re.fullmatch(rewrite_xsd_shorthands(r"\\w"), "\\w")


def test_category_escape_is_left_alone():
    assert rewrite_xsd_shorthands(r"\p{Lu}") == r"\p{Lu}"


def test_empty_block_escape_is_rejected():
    with pytest.raises(ValueError):
        reject_malformed_escapes(r"\p{Is}")


def test_complement_empty_block_escape_is_rejected():
    with pytest.raises(ValueError):
        reject_malformed_escapes(r"\P{Is}")


def test_named_block_escape_is_allowed():
    reject_malformed_escapes(r"\p{IsGreek}")
    reject_malformed_escapes(r"\p{Is-}")
    reject_malformed_escapes(r"\p{Lu}")


# --- end to end through the parser ------------------------------------------


def test_pattern_word_facet_uses_xsd_definition():
    body = element("xs:string", r'<xs:pattern value="\w"/>')
    assert errors(parse(body, "<r>\u064b</r>")) == []
    assert errors(parse(body, "<r>_</r>"))


def test_pattern_space_facet_uses_xsd_definition():
    body = element("xs:string", r'<xs:pattern value="\s"/>')
    assert errors(parse(body, "<r>\t</r>")) == []
    assert errors(parse(body, "<r>\u00a0</r>"))


def test_pattern_empty_block_escape_is_schema_error():
    report = parse(element("xs:string", r'<xs:pattern value="\p{Is}"/>'), "<r>x</r>")
    assert "facet" in [code for code, _ in errors(report)]


# --- elementpath drift guard -------------------------------------------------


def test_elementpath_still_passes_shorthands_through():
    # The pyxsd rewrite runs on the translated output, so it depends on
    # elementpath leaving the shorthands untouched. If elementpath starts
    # expanding them itself, the rewrite would double-handle (or miss) them.
    out = translate_pattern(
        r"\w\W\s\S",
        xsd_version="1.1",
        anchors=False,
        back_references=False,
        lazy_quantifiers=False,
    )
    for token in (r"\w", r"\W", r"\s", r"\S"):
        assert token in out


def test_elementpath_category_escape_shape():
    # The rewrite deliberately does not touch expanded \\p{...} classes; this
    # pins the shape it assumes (a bracketed class), so a format change breaks
    # loudly rather than silently leaving a bad pattern.
    out = translate_pattern(
        r"[\p{Lu}]",
        xsd_version="1.1",
        anchors=False,
        back_references=False,
        lazy_quantifiers=False,
    )
    assert out.startswith("^(?:[")
    assert "A-Z" in out
