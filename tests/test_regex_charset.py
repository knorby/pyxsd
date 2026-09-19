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
import re._parser
import unicodedata

import pytest
from elementpath.regex import translate_pattern

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD
from pyxsd.regex_charset import (
    _class_contents,
    reject_malformed_escapes,
    rewrite_xsd_shorthands,
)

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


# --- elementpath in-class shorthand drift guard ------------------------------
#
# pyxsd's rewrite only handles the *bare* shorthands; elementpath expands the
# in-class forms (``[\w]``, ``[\S]``, ``[\s]``, ``[\d]`` ...) itself, and
# pyxsd relies on that expansion being the XSD set. The tests below parse the
# concrete ``translate_pattern`` output and compare its exact matched codepoint
# ranges against the XSD definition, so an elementpath upgrade that changes the
# expansion fails loudly here instead of silently changing pyxsd's `[\w]`.

_MAX_UNICODE = 0x10FFFF


def _translate(pattern):
    return translate_pattern(
        pattern,
        xsd_version="1.1",
        anchors=False,
        back_references=False,
        lazy_quantifiers=False,
    )


def _translated_class(pattern):
    """The concrete character class (``[`` ... ``]``) of a translated pattern."""
    out = _translate(pattern)
    inner = out[len("^(?:") : -len(")$(?!\\n\\Z)")]
    assert inner.startswith("[") and inner.endswith("]"), inner[:40]
    return inner


def _merge(ranges):
    merged = []
    for low, high in sorted(ranges):
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def _complement(ranges):
    out = []
    nxt = 0
    for low, high in _merge(ranges):
        if low > nxt:
            out.append((nxt, low - 1))
        nxt = high + 1
    if nxt <= _MAX_UNICODE:
        out.append((nxt, _MAX_UNICODE))
    return out


def _matched_ranges(char_class):
    """The exact codepoint ranges a Python character class matches."""
    entries = re._parser.parse(char_class, 0)[0][1]
    negate = any(op is re._parser.NEGATE for op, _ in entries)
    ranges = []
    for op, arg in entries:
        if op is re._parser.NEGATE:
            continue
        if op is re._parser.LITERAL:
            ranges.append((arg, arg))
        elif op is re._parser.RANGE:
            ranges.append(tuple(arg))
        else:  # pragma: no cover - elementpath emits only literals/ranges
            raise AssertionError(f"unexpected class entry {op!r}: {arg!r}")
    return _complement(ranges) if negate else _merge(ranges)


def _unicode_ranges(predicate):
    out = []
    start = None
    last = 0
    for codepoint in range(_MAX_UNICODE + 1):
        if predicate(chr(codepoint)):
            if start is None:
                start = codepoint
            last = codepoint
        elif start is not None:
            out.append((start, last))
            start = None
    if start is not None:
        out.append((start, last))
    return out


def _first_difference(expected, actual):
    for exp, got in zip(expected, actual, strict=False):
        if exp != got:
            return f"expected {exp}, got {got}"
    return f"length {len(actual)} != {len(expected)}" if len(actual) != len(expected) else "none"


def test_elementpath_inclass_space_expansion_is_pinned():
    # `[\s]` expands to exactly the XSD four characters, `[\S]` to its
    # complement; these are small enough to pin verbatim.
    assert _translated_class(r"[\s]") == "[\t\n\r ]"
    assert _translated_class(r"[\S]") == "[^\t\n\r ]"


@pytest.mark.parametrize(("shorthand", "reference"), [("w", "word"), ("W", "nonword")])
def test_elementpath_inclass_word_expansion_matches_xsd(shorthand, reference):
    actual = _matched_ranges(_translated_class(f"[\\{shorthand}]"))
    expected = _matched_ranges("[" + _class_contents(reference) + "]")
    assert actual == expected, (
        f"elementpath changed its in-class [\\{shorthand}] expansion; pyxsd "
        f"leaves the in-class shorthand to elementpath and relies on the XSD "
        f"{reference} set. Re-verify src/pyxsd/regex_charset.py before "
        f"accepting the elementpath upgrade: {_first_difference(expected, actual)}"
    )


def test_elementpath_inclass_digit_expansion_matches_unicode():
    nd = _unicode_ranges(lambda ch: unicodedata.category(ch) == "Nd")
    actual = _matched_ranges(_translated_class(r"[\d]"))
    assert actual == nd, (
        "elementpath changed its in-class [\\d] expansion; pyxsd relies on it "
        f"being the Unicode Nd set: {_first_difference(nd, actual)}"
    )
    actual_complement = _matched_ranges(_translated_class(r"[\D]"))
    expected_complement = _complement(nd)
    assert actual_complement == expected_complement, (
        "elementpath changed its in-class [\\D] expansion; pyxsd relies on it "
        "being the complement of the Unicode Nd set: "
        f"{_first_difference(expected_complement, actual_complement)}"
    )


def test_inclass_drift_guard_rejects_python_word_semantics():
    # If elementpath ever expanded `[\w]` like Python's `\w` (including `_`
    # and excluding marks) the pinned comparison must reject it. This model
    # simulates that drift without touching the installed elementpath.
    python_word = _matched_ranges("[a-zA-Z0-9_]")
    xsd_word = _matched_ranges("[" + _class_contents("word") + "]")
    assert python_word != xsd_word
    assert _first_difference(xsd_word, python_word) != "none"


def test_inclass_word_pattern_uses_xsd_definition():
    body = element("xs:string", r'<xs:pattern value="[\w]"/>')
    assert errors(parse(body, "<r>\u064b</r>")) == []
    assert errors(parse(body, "<r>_</r>"))


def test_inclass_nonspace_pattern_uses_xsd_complement():
    body = element("xs:string", r'<xs:pattern value="[\S]"/>')
    assert errors(parse(body, "<r>\u00a0</r>")) == []
    assert errors(parse(body, "<r>\t</r>"))
