"""Exact byte spans for element occurrences in original XML source.

The index must return the bytes from an element's opening ``<`` through the
``>`` of its closing tag (or ``/>``), excluding its tail, while preserving
prefixes, quoting, comments, CDATA, references, and original encoding. It
must also make the same declaration-policy decision as the semantic parse
and never retrieve an external resource.
"""

from __future__ import annotations

import contextlib
import io
import socket
from xml.etree import ElementTree as ET
from xml.parsers import expat

import pytest

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import ParseProvenance, PathStep
from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.source_spans import SourceIndex, SourceSpan
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

XML_DECL = b'<?xml version="1.0" encoding="UTF-8"?>\n'
ROOT_OPEN = b'<root xmlns="urn:main" xmlns:p="urn:one" a=\'1\' b="2">\n'
LEAD = b"  <!-- lead -->\n  <?work go?>\n"
ITEM_1 = b'<item id="1">first &amp; &#65;</item>'
CHILD_A = b'<p:child xmlns:p="urn:two">deep<![CDATA[<raw>&]]></p:child>'
CHILD_B = b'<p:child xmlns:p="urn:three"><p:leaf>v</p:leaf></p:child>'
EMPTY_SELF = b"<empty />"
EMPTY_PAIR = b"<empty></empty>"
ITEM_2 = b'<item id="2"> ' + CHILD_A + b"<!--tail-->" + CHILD_B + EMPTY_SELF + b"</item>"
ROOT_BODY = ROOT_OPEN + LEAD + b"  " + ITEM_1 + b"\n  " + ITEM_2 + b"\n  " + EMPTY_PAIR + b"\n"
ROOT_CLOSE = b"</root>"
ROOT_BYTES = ROOT_BODY + ROOT_CLOSE
XML = XML_DECL + ROOT_BYTES + b"\n"

ROOT = "{urn:main}root"
ITEM = "{urn:main}item"
EMPTY = "{urn:main}empty"
LEAF = "{urn:three}leaf"
CHILD_TWO = "{urn:two}child"
CHILD_THREE = "{urn:three}child"

SMALL = b"<r><c><d/></c></r>"


def at(*steps: tuple[str, int]) -> tuple[PathStep, ...]:
    return tuple(PathStep(name, index) for name, index in steps)


ROOT_PATH = at((ROOT, 1))
ITEM_1_PATH = at((ROOT, 1), (ITEM, 1))
ITEM_2_PATH = at((ROOT, 1), (ITEM, 2))
CHILD_TWO_PATH = at((ROOT, 1), (ITEM, 2), (CHILD_TWO, 1))
CHILD_THREE_PATH = at((ROOT, 1), (ITEM, 2), (CHILD_THREE, 1))
LEAF_PATH = at((ROOT, 1), (ITEM, 2), (CHILD_THREE, 1), (LEAF, 1))
EMPTY_SELF_PATH = at((ROOT, 1), (ITEM, 2), (EMPTY, 1))
EMPTY_PAIR_PATH = at((ROOT, 1), (EMPTY, 1))


def utf16(text: str, *, endian: str = "le", bom: bool = True) -> bytes:
    label = "utf-16" if bom else ("utf-16le" if endian == "le" else "utf-16be")
    document = f'<?xml version="1.0" encoding="{label}"?>' + text
    codec = "utf-16-le" if endian == "le" else "utf-16-be"
    payload = document.encode(codec)
    if not bom:
        return payload
    return (b"\xff\xfe" if endian == "le" else b"\xfe\xff") + payload


def semantic_outcome(data: bytes, policy: XmlParserPolicy | None):
    try:
        root = parse_with_namespaces(io.BytesIO(data), NamespaceContext(), policy=policy)
    except ResourcePolicyError as exc:
        return ("denied", str(exc))
    except ET.ParseError as exc:
        return ("malformed", str(exc))
    return ("accepted", root.tag)


def index_outcome(data: bytes, policy: XmlParserPolicy | None):
    try:
        index = SourceIndex("case.xml", data, policy=policy)
    except ResourcePolicyError as exc:
        return ("denied", str(exc))
    except PyXSDError as exc:
        return ("malformed", str(exc))
    return ("accepted", index)


def test_root_span_excludes_the_declaration_and_tail():
    index = SourceIndex("doc.xml", XML)
    span = index.span(ROOT_PATH)
    assert span == SourceSpan(XML.index(ROOT_BYTES), XML.index(ROOT_BYTES) + len(ROOT_BYTES))
    assert index.extract(ROOT_PATH) == ROOT_BYTES
    assert index.extract(ROOT_PATH).startswith(b"<root")
    assert index.extract(ROOT_PATH).endswith(b"</root>")


def test_repeated_sibling_span_is_exact_and_excludes_its_tail():
    index = SourceIndex("doc.xml", XML)
    assert index.extract(ITEM_1_PATH) == ITEM_1
    assert index.extract(ITEM_2_PATH) == ITEM_2
    assert index.extract(ITEM_2_PATH).startswith(b'<item id="2">')
    assert index.extract(ITEM_2_PATH).endswith(b"</item>")
    # Internal spelling is untouched: quotes, references, CDATA, comments.
    assert b"<![CDATA[<raw>&]]>" in index.extract(ITEM_2_PATH)
    assert b"<!--tail-->" in index.extract(ITEM_2_PATH)
    assert b"&amp;" in index.extract(ITEM_1_PATH)
    assert b"&#65;" in index.extract(ITEM_1_PATH)
    assert b"a='1'" in index.extract(ROOT_PATH)


def test_empty_element_spans_include_both_forms_of_markup():
    index = SourceIndex("doc.xml", XML)
    assert index.extract(EMPTY_SELF_PATH) == EMPTY_SELF
    assert index.extract(EMPTY_PAIR_PATH) == EMPTY_PAIR


def test_prefix_rebinding_uses_expanded_names_and_sibling_indices():
    index = SourceIndex("doc.xml", XML)
    assert index.extract(CHILD_TWO_PATH) == CHILD_A
    assert index.extract(CHILD_THREE_PATH) == CHILD_B
    assert index.extract(LEAF_PATH) == b"<p:leaf>v</p:leaf>"


def test_every_provenance_path_has_a_matching_exact_span():
    root = parse_with_namespaces(io.BytesIO(XML), NamespaceContext())
    provenance = ParseProvenance(root, "doc.xml")
    index = SourceIndex("doc.xml", XML)
    seen = 0
    for element in root.iter():
        location = provenance.source_location(element)
        assert location is not None
        assert location.path[-1].name == element.tag
        span = index.span(location.path)
        assert span is not None
        chunk = index.extract(location.path)
        assert chunk is not None
        assert chunk == XML[span.start : span.end]
        assert chunk.startswith(b"<")
        assert chunk.endswith(b">")
        seen += 1
    assert seen == 8


def test_unknown_paths_return_none():
    index = SourceIndex("doc.xml", XML)
    assert index.span(()) is None
    assert index.extract(()) is None
    assert index.span(at((ITEM, 3))) is None
    assert index.extract(at((ITEM, 3))) is None
    assert index.span((PathStep("absent", 1),)) is None
    assert index.extract((PathStep("absent", 1),)) is None


def test_utf8_bom_is_preserved_and_excluded_from_the_root_span():
    bom = b"\xef\xbb\xbf"
    data = bom + XML
    index = SourceIndex("doc.xml", data)
    assert index.extract(ROOT_PATH) == ROOT_BYTES
    assert index.extract(ITEM_2_PATH) == ITEM_2
    assert index.span(ROOT_PATH) == SourceSpan(
        3 + XML.index(ROOT_BYTES), 3 + XML.index(ROOT_BYTES) + len(ROOT_BYTES)
    )
    assert data[: len(XML_DECL) + 3] == bom + XML_DECL


def test_self_closing_root_span():
    assert SourceIndex("a.xml", b"<r/>").extract(at(("r", 1))) == b"<r/>"
    data = b'<?xml version="1.0"?>\n<r />\n'
    index = SourceIndex("b.xml", data)
    assert index.extract(at(("r", 1))) == b"<r />"
    assert index.span(at(("r", 1))) == SourceSpan(data.index(b"<r />"), data.index(b"<r />") + 5)


def test_self_closing_sibling_immediately_before_a_parent_close():
    data = b"<r><x/></r>"
    index = SourceIndex("c.xml", data)
    assert index.extract(at(("r", 1), ("x", 1))) == b"<x/>"
    assert index.extract(at(("r", 1))) == data


@pytest.mark.parametrize("endian", ["le", "be"])
@pytest.mark.parametrize("bom", [True, False])
def test_utf16_spans_are_byte_exact(endian, bom):
    text = '<r xmlns="urn:e"><a>x</a><b/><a>y</a></r>'
    data = utf16(text, endian=endian, bom=bom)
    codec = "utf-16-le" if endian == "le" else "utf-16-be"
    index = SourceIndex("utf16.xml", data)
    root_span = index.span(at(("{urn:e}r", 1)))
    second_a = index.span(at(("{urn:e}r", 1), ("{urn:e}a", 2)))
    empty_b = index.span(at(("{urn:e}r", 1), ("{urn:e}b", 1)))
    assert root_span == SourceSpan(
        data.index(text.encode(codec)), data.index(text.encode(codec)) + len(text.encode(codec))
    )
    assert index.extract(at(("{urn:e}r", 1))) == text.encode(codec)
    assert second_a == SourceSpan(
        data.index("<a>y</a>".encode(codec)),
        data.index("<a>y</a>".encode(codec)) + len("<a>y</a>".encode(codec)),
    )
    assert index.extract(at(("{urn:e}r", 1), ("{urn:e}a", 2))) == "<a>y</a>".encode(codec)
    assert index.extract(at(("{urn:e}r", 1), ("{urn:e}b", 1))) == "<b/>".encode(codec)
    assert empty_b is not None


@pytest.mark.parametrize(
    "text",
    [
        "<r/> ",
        "<r/>\n",
        "<r>x</r> ",
        "<r><x/></r>\n",
    ],
)
def test_utf16_trailing_whitespace_does_not_enter_any_span(text):
    data = utf16(text, endian="le", bom=True)
    index = SourceIndex("utf16-tail.xml", data)
    tag = text.rstrip(" \n")
    assert index.extract(at(("r", 1))) == tag.encode("utf-16-le")


def test_utf16_name_with_marker_bytes_still_finds_the_closing_angle_bracket():
    name = "\u4e3e\u4e00"
    text = f"<{name}>x</{name}> "
    data = utf16(text, endian="le", bom=True)
    index = SourceIndex("utf16-name.xml", data)
    assert index.extract(at((name, 1))) == f"<{name}>x</{name}>".encode("utf-16-le")


@pytest.mark.parametrize(
    "data",
    [b"", b"<r>", b"<r><a></r>", b"<r></r><x/>", b"<r a='1'></r"],
)
def test_malformed_input_raises_pyxsd_error(data):
    with pytest.raises(PyXSDError):
        SourceIndex("broken.xml", data)


def test_malformed_input_after_a_valid_prefix_returns_no_index():
    data = b"<r><a>ok</a><b>broken</r>"
    with pytest.raises(PyXSDError, match=r"broken\.xml"):
        SourceIndex("broken.xml", data)


def test_default_policy_keeps_internal_entities_indexable():
    data = b'<!DOCTYPE r [<!ENTITY v "X">]><r>a&v;b</r>'
    index = SourceIndex("entities.xml", data)
    assert index.extract(at(("r", 1))) == b"<r>a&v;b</r>"


def test_parameter_entity_declared_reference_keeps_spans_exact():
    data = b"<!DOCTYPE r [<!ENTITY % p \"<!ENTITY v 'X'>\"> %p;]><r><a>x</a>&v;<b/></r>"
    index = SourceIndex("parameter-entity.xml", data)
    assert index.extract(at(("r", 1), ("a", 1))) == b"<a>x</a>"
    assert index.extract(at(("r", 1), ("b", 1))) == b"<b/>"
    assert index.extract(at(("r", 1))) == b"<r><a>x</a>&v;<b/></r>"


def test_entity_expanded_elements_have_no_addressable_span():
    data = b'<!DOCTYPE r [<!ENTITY e "<c/>">]><r>&e;</r>'
    index = SourceIndex("entities.xml", data)
    assert index.span(at(("r", 1), ("c", 1))) is None
    assert index.extract(at(("r", 1), ("c", 1))) is None
    assert index.extract(at(("r", 1))) == b"<r>&e;</r>"


def test_closing_tags_allow_whitespace_before_the_angle_bracket():
    data = b"<r><a>x</a ><b>y</b\n></r>"
    index = SourceIndex("spaced-close.xml", data)
    assert index.extract(at(("r", 1), ("a", 1))) == b"<a>x</a >"
    assert index.extract(at(("r", 1), ("b", 1))) == b"<b>y</b\n>"
    assert index.extract(at(("r", 1))) == data


def test_cdata_and_comment_after_a_self_closing_sibling():
    data = b"<r><x/><![CDATA[<y>]]><!-- z --><y/></r>"
    index = SourceIndex("mixed.xml", data)
    assert index.extract(at(("r", 1), ("x", 1))) == b"<x/>"
    assert index.extract(at(("r", 1), ("y", 1))) == b"<y/>"
    assert index.extract(at(("r", 1))) == data


@pytest.mark.parametrize(
    "data",
    [
        b"<r/><!-- after -->",
        b"<r/> <!-- after -->",
        b"<r/><?after work?>",
        b"<r/>\n<?after work?>",
    ],
)
def test_self_closing_root_span_ignores_trailing_markup(data):
    index = SourceIndex("trailing.xml", data)
    assert index.span(at(("r", 1))) == SourceSpan(0, 4)
    assert index.extract(at(("r", 1))) == b"<r/>"


@pytest.mark.parametrize(
    "data",
    [
        b"<r></r>  ",
        b"<r></r>\n<!-- after -->",
        b"<r><x/></r>\n ",
    ],
)
def test_ordinary_root_span_excludes_trailing_whitespace_and_markup(data):
    index = SourceIndex("trailing.xml", data)
    root = at(("r", 1))
    assert index.extract(root) is not None
    assert index.extract(root).endswith(b"</r>")
    assert data[index.span(root).end :] == data[data.index(b"</r>") + len(b"</r>") :]


def test_latin1_source_is_sliced_in_its_own_bytes():
    data = b'<?xml version="1.0" encoding="ISO-8859-1"?><r><a>caf\xe9</a></r>'
    index = SourceIndex("latin1.xml", data)
    assert index.extract(at(("r", 1), ("a", 1))) == b"<a>caf\xe9</a>"
    assert index.extract(at(("r", 1))) == b"<r><a>caf\xe9</a></r>"


def test_crlf_line_endings_keep_offsets_aligned():
    data = b"<r>\r\n  <a>x</a>\r\n  <b />\r\n</r>\r\n"
    index = SourceIndex("crlf.xml", data)
    assert index.extract(at(("r", 1), ("a", 1))) == b"<a>x</a>"
    assert index.extract(at(("r", 1), ("b", 1))) == b"<b />"
    assert index.extract(at(("r", 1))) == b"<r>\r\n  <a>x</a>\r\n  <b />\r\n</r>"


def test_strict_policy_denies_doctype_in_the_index_itself():
    with pytest.raises(ResourcePolicyError, match="DOCTYPE"):
        SourceIndex("strict.xml", b"<!DOCTYPE r><r/>", policy=XmlParserPolicy.strict())


def test_protected_policy_denies_entity_declarations_in_the_index_itself():
    with pytest.raises(ResourcePolicyError, match="entity"):
        SourceIndex(
            "protected.xml",
            b'<!DOCTYPE r [<!ENTITY v "X">]><r/>',
            policy=XmlParserPolicy.protected(),
        )


def test_protected_policy_denies_external_doctypes_in_the_index_itself():
    with pytest.raises(ResourcePolicyError, match="external"):
        SourceIndex(
            "protected.xml",
            b'<!DOCTYPE r SYSTEM "file:///must-not-read"><r/>',
            policy=XmlParserPolicy.protected(),
        )


def test_protected_policy_denies_unparsed_entities_in_the_index_itself():
    data = (
        b'<!DOCTYPE r [<!NOTATION gif SYSTEM "image/gif">'
        b'<!ENTITY logo SYSTEM "logo.gif" NDATA gif>]><r/>'
    )
    with pytest.raises(ResourcePolicyError, match="entity"):
        SourceIndex("protected.xml", data, policy=XmlParserPolicy.protected())


def test_protected_policy_allows_a_plain_doctype_and_attlist_defaults():
    data = b'<!DOCTYPE r [<!ATTLIST r f CDATA "defaulted">]><r/>'
    index = SourceIndex("protected.xml", data, policy=XmlParserPolicy.protected())
    assert index.extract(at(("r", 1))) == b"<r/>"


DECLARATION_DOCS = {
    "plain-doctype": b"<!DOCTYPE r><r/>",
    "attlist-default": b'<!DOCTYPE r [<!ATTLIST r f CDATA "defaulted">]><r/>',
    "internal-entity": b'<!DOCTYPE r [<!ENTITY v "X">]><r>&v;</r>',
    "external-doctype": b'<!DOCTYPE r SYSTEM "file:///must-not-read"><r/>',
    "external-entity": (b'<!DOCTYPE r [<!ENTITY v SYSTEM "file:///must-not-read">]><r>&v;</r>'),
}
POLICIES = [None, XmlParserPolicy.protected(), XmlParserPolicy.strict()]
POLICY_IDS = ["default", "protected", "strict"]


@pytest.mark.parametrize("policy", POLICIES, ids=POLICY_IDS)
@pytest.mark.parametrize("data", DECLARATION_DOCS.values(), ids=list(DECLARATION_DOCS))
def test_index_matches_the_semantic_declaration_decision(data, policy):
    semantic = semantic_outcome(data, policy)
    indexed = index_outcome(data, policy)
    # The declaration decision is the shared contract. Default policy keeps
    # the stdlib's reference behavior qualification: an unresolved external
    # entity is an ElementTree parse error, while Expat skips it without a
    # fetcher; neither parser treats the declaration itself as denied.
    assert (semantic[0] == "denied") == (indexed[0] == "denied")
    if semantic[0] == "denied":
        assert indexed == semantic
    else:
        assert indexed[0] == "accepted"
        if semantic[0] == "accepted":
            assert indexed[1].extract(at((semantic[1], 1))) is not None


@pytest.mark.parametrize("policy", POLICIES, ids=POLICY_IDS)
@pytest.mark.parametrize(
    "data",
    [
        DECLARATION_DOCS["external-doctype"],
        DECLARATION_DOCS["external-entity"],
        (
            b'<!DOCTYPE r [<!NOTATION gif SYSTEM "image/gif">'
            b'<!ENTITY logo SYSTEM "logo.gif" NDATA gif>]><r/>'
        ),
    ],
)
def test_index_never_reads_a_file_or_opens_a_socket(monkeypatch, data, policy):
    def deny(*args, **kwargs):
        raise AssertionError("the index tried to reach an external resource")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", deny)
        patch.setattr(socket, "socket", deny)
        with contextlib.suppress(PyXSDError):
            SourceIndex("external.xml", data, policy=policy)


def test_default_index_installs_no_external_entity_fetcher(monkeypatch):
    created = []
    real_parser_create = expat.ParserCreate

    def record(*args, **kwargs):
        parser = real_parser_create(*args, **kwargs)
        created.append(parser)
        return parser

    monkeypatch.setattr(expat, "ParserCreate", record)
    SourceIndex("plain.xml", b"<r/>")
    (parser,) = created
    assert parser.ExternalEntityRefHandler is None


@pytest.mark.parametrize(
    ("limits", "data"),
    [
        (XmlLimits(max_bytes=len(SMALL)), SMALL),
        (XmlLimits(max_depth=3), SMALL),
        (XmlLimits(max_elements=3), SMALL),
        (XmlLimits(max_bytes=len(SMALL), max_depth=3, max_elements=3), SMALL),
    ],
)
def test_limits_allow_the_exact_boundary(limits, data):
    index = SourceIndex("budget.xml", data, limits=limits)
    assert index.extract(at(("r", 1), ("c", 1), ("d", 1))) == b"<d/>"


@pytest.mark.parametrize(
    ("limits", "name"),
    [
        (XmlLimits(max_bytes=len(SMALL) - 1), "max_bytes"),
        (XmlLimits(max_depth=2), "max_depth"),
        (XmlLimits(max_elements=2), "max_elements"),
    ],
)
def test_limits_reject_one_past_the_boundary(limits, name):
    with pytest.raises(ResourcePolicyError, match=name):
        SourceIndex("budget.xml", SMALL, limits=limits)


def test_byte_budget_counts_supplied_bytes_not_characters():
    data = utf16("<r>\U0001f600</r>", endian="le", bom=True)
    index = SourceIndex("budget.xml", data, limits=XmlLimits(max_bytes=len(data)))
    assert index.extract(at(("r", 1))) == "<r>\U0001f600</r>".encode("utf-16-le")
    assert len(data) > len("<r>\U0001f600</r>")
    with pytest.raises(ResourcePolicyError, match="max_bytes"):
        SourceIndex("budget.xml", data, limits=XmlLimits(max_bytes=len(data) - 1))


def test_limits_and_policies_apply_together():
    data = b"<!DOCTYPE r><r><c/></r>"
    with pytest.raises(ResourcePolicyError, match="max_elements"):
        SourceIndex(
            "budget.xml",
            data,
            policy=XmlParserPolicy.protected(),
            limits=XmlLimits(max_elements=1),
        )
