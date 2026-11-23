import io
from xml.etree import ElementTree as ET

import pytest

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import PathStep, SourceTextSpan
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.schemaless import SchemalessElement, SchemalessParseError, SchemalessRevision, parse_xml
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy


def test_parse_xml_preserves_lexical_content_namespaces_and_locations():
    doc = parse_xml(io.BytesIO(b'<r xmlns="urn:x" a="02138">x<c/>tail</r>'))
    assert doc.root._name_ == "{urn:x}r"
    assert doc.root.attribute_lexical("a") == "02138"
    assert list(doc.root.iter_content()) == ["x", doc.root.children[0], "tail"]
    assert doc.root.source_location.path[0].name == "{urn:x}r"
    assert doc.source_index.span(doc.root.children[0].source_location.path) is not None
    assert isinstance(doc.root.children[0], SchemalessElement)
    assert isinstance(doc.root.revision, SchemalessRevision)
    assert doc.root.children[0].revision is doc.root.revision
    assert doc.report.error_count == 0


def test_malformed_xml_has_location_and_open_stack():
    with pytest.raises(SchemalessParseError) as caught:
        parse_xml(
            io.BytesIO(b"<r><c></r>"),
        )
    assert caught.value.location.source == "<memory>"
    assert caught.value.location.span.line == 1
    assert caught.value.open_elements == ("r", "c")
    assert isinstance(caught.value, PyXSDError)
    assert caught.value.location.span == SourceTextSpan(8, 8, 1, 9, 1, 9)
    (issue,) = caught.value.report.issues
    assert issue.code == "xml-malformed"
    assert issue.location_precision == "exact"
    assert issue.location == caught.value.location
    assert "<memory>:1:9" in str(caught.value)


def test_parse_path_uses_occurrence_indexes_namespace_frames_and_document_uri(tmp_path):
    xml_file = tmp_path / "source.xml"
    xml_file.write_bytes(
        b'<r xmlns:p="urn:x" xml:base="sub/"><p:c/><!--omit--><?p i?>'
        b'<q:c xmlns:q="urn:x" xmlns:p="urn:other" xml:base="../leaf"/>tail</r>'
    )

    doc = parse_xml(xml_file)
    first, second = doc.root.children
    assert doc.source == str(xml_file)
    assert doc.root.base_uri == (tmp_path / "sub").as_uri() + "/"
    assert second.base_uri == (tmp_path / "leaf").as_uri()
    assert first.namespaces == {"p": "urn:x"}
    assert second.namespaces == {"p": "urn:other", "q": "urn:x"}
    path = second.source_location.path
    assert path == (PathStep("r", 1), PathStep("{urn:x}c", 2))
    assert doc.source_index.extract(path) == (
        b'<q:c xmlns:q="urn:x" xmlns:p="urn:other" xml:base="../leaf"/>'
    )
    assert list(doc.root.iter_content()) == [first, second, "tail"]


def test_parse_nonseekable_short_reads_uses_one_source_revision():
    class Stream:
        def __init__(self):
            self._chunks = iter([b"<r>", b"<c/", b">", b"</r>", b""])

        def read(self, size):
            return next(self._chunks)

    doc = parse_xml(Stream())

    assert doc.source_index.content == b"<r><c/></r>"
    assert doc.source_index.extract(doc.root.children[0].source_location.path) == b"<c/>"


def test_parse_text_stream_normalizes_a_decoded_xml_declaration():
    doc = parse_xml(io.StringIO('<?xml version="1.0" encoding="UTF-16"?><r>caf\u00e9</r>'))

    assert doc.root.text_value() == "caf\u00e9"
    assert doc.source_index.content == b'<?xml version="1.0" encoding="UTF-8"?><r>caf\xc3\xa9</r>'


def test_parse_etree_keeps_an_independent_tree_and_has_no_original_source_index():
    source = ET.fromstring(
        "<r>before<!--omit-->after<c a='original'/>tail</r>",
        parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)),
    )
    source.tail = "outside"

    doc = parse_xml(source)
    (child,) = doc.root.children
    source[1].set("a", "changed")
    child.set_tail("new")

    assert doc.source == "<memory>"
    assert doc.source_index is None
    assert doc.root.source_location.path == (PathStep("r", 1),)
    assert doc.root.text_value() == "beforeafter"
    assert doc.root.tail_value() is None
    assert child.attribute_lexical("a") == "original"
    assert source[1].tail == "tail"
    assert source.tail == "outside"
    assert doc._capture_source.data == b'<r>before<!--omit-->after<c a="original" />tail</r>'


@pytest.mark.parametrize(
    "content",
    [
        b'<r xmlns="urn:x">\r\n<c/><c>\xc3\xa9</r>',
        '<r xmlns="urn:x">\r\n<c/><c>\u00e9</r>'.encode("utf-16"),
        '<r xmlns="urn:x">\r\n<c/><c>\u00e9</r>'.encode("utf-16-le"),
        '<r xmlns="urn:x">\r\n<c/><c>\u00e9</r>'.encode("utf-16-be"),
    ],
)
def test_malformed_xml_reports_characters_and_expanded_occurrences(content, tmp_path):
    path = tmp_path / "malformed.xml"
    path.write_bytes(content)

    with pytest.raises(SchemalessParseError) as caught:
        parse_xml(path)

    assert caught.value.open_elements == ("{urn:x}r", "{urn:x}c")
    assert caught.value.location.path == (PathStep("{urn:x}r", 1), PathStep("{urn:x}c", 2))
    assert caught.value.location.source == str(path)
    assert caught.value.location.span == SourceTextSpan(29, 29, 2, 11, 2, 11)


@pytest.mark.parametrize(
    ("content", "span"),
    [
        (b"", SourceTextSpan(0, 0, 1, 1, 1, 1)),
        (b"<r/>x", SourceTextSpan(4, 4, 1, 5, 1, 5)),
        (b"\xef\xbb\xbf<r/>x", SourceTextSpan(4, 4, 1, 5, 1, 5)),
        (b"<r/>\r\n<", SourceTextSpan(6, 6, 2, 1, 2, 1)),
    ],
)
def test_malformed_xml_without_open_elements_has_an_exact_source_position(content, span):
    with pytest.raises(SchemalessParseError) as caught:
        parse_xml(content)

    assert caught.value.open_elements == ()
    assert caught.value.location.path == ()
    assert caught.value.location.span == span


def test_malformed_xml_uses_the_declared_input_encoding():
    with pytest.raises(SchemalessParseError) as caught:
        parse_xml(b'<?xml version="1.0" encoding="iso-8859-1"?><r>\xe9</R>')

    assert caught.value.open_elements == ("r",)
    assert caught.value.location.span == SourceTextSpan(49, 49, 1, 50, 1, 50)


@pytest.mark.parametrize("encoding", [b"unknown-encoding", b"utf-32"])
def test_unsupported_xml_encoding_has_a_diagnostic_at_the_declaration(encoding):
    with pytest.raises(SchemalessParseError) as caught:
        parse_xml(b'<?xml version="1.0" encoding="' + encoding + b'"?><r/>')

    assert caught.value.open_elements == ()
    assert caught.value.location.span == SourceTextSpan(30, 30, 1, 31, 1, 31)
    assert "encoding" in str(caught.value)


@pytest.mark.parametrize(
    ("declaration", "offset"),
    [
        (b'<!DOCTYPE r SYSTEM "file:///not-read">', 41),
        (b'<!DOCTYPE r [<!ENTITY unavailable SYSTEM "file:///not-read">]>', 65),
    ],
)
def test_default_policy_reports_unexpanded_entities_at_the_reference(declaration, offset):
    with pytest.raises(SchemalessParseError) as caught:
        parse_xml(declaration + b"<r>&unavailable;<c></r>")

    assert caught.value.location.span == SourceTextSpan(
        offset, offset, 1, offset + 1, 1, offset + 1
    )
    assert caught.value.open_elements == ("r",)
    assert "undefined entity" in str(caught.value)


def test_default_policy_preserves_expanded_internal_text_and_dtd_defaults():
    doc = parse_xml(
        b'<!DOCTYPE r [<!ATTLIST r a CDATA "02138"><!ENTITY local "ok">]><r>&local;</r>'
    )

    assert doc.root.attribute_lexical("a") == "02138"
    assert doc.root.text_value() == "ok"


def test_strict_policy_rejects_doctype_without_resolving_it(tmp_path):
    dtd = tmp_path / "unavailable.dtd"
    content = f'<!DOCTYPE r SYSTEM "{dtd.as_uri()}"><r>&external;</r>'.encode()

    with pytest.raises(ResourcePolicyError, match="DOCTYPE declarations are forbidden"):
        parse_xml(content, xml_policy=XmlParserPolicy.strict())


def test_strict_policy_still_gives_structured_diagnostics_for_malformed_xml():
    with pytest.raises(SchemalessParseError) as caught:
        parse_xml(b"<r><c></r>", xml_policy=XmlParserPolicy.strict())

    assert caught.value.open_elements == ("r", "c")
    assert caught.value.location.span == SourceTextSpan(8, 8, 1, 9, 1, 9)


def test_xml_limits_are_inclusive():
    limits = XmlLimits(max_depth=2, max_elements=2)

    assert parse_xml(b"<r><a/></r>", xml_limits=limits).root.children[0]._name_ == "a"
    assert parse_xml(b"<r/>", xml_limits=XmlLimits(max_bytes=4)).root._name_ == "r"


@pytest.mark.parametrize(
    ("content", "budget"),
    [
        (b"<r><a><b/></a></r>", "max_depth"),
        (b"<r><a/><b/></r>", "max_elements"),
        (b"<r><a><b></r>", "max_depth"),
    ],
)
def test_xml_limits_fail_before_returning_a_tree_or_a_syntax_issue(content, budget):
    with pytest.raises(ResourcePolicyError, match=rf"{budget}=2"):
        parse_xml(content, xml_limits=XmlLimits(max_depth=2, max_elements=2))


@pytest.mark.parametrize("wrap", [bytes, io.BytesIO])
def test_xml_byte_limit_rejects_oversized_input_even_when_it_is_malformed(wrap):
    with pytest.raises(ResourcePolicyError, match="max_bytes=4"):
        parse_xml(wrap(b"<r/>extra"), xml_limits=XmlLimits(max_bytes=4))


def test_xml_byte_limit_applies_before_parsing_a_malformed_prefix_of_in_memory_bytes():
    with pytest.raises(ResourcePolicyError, match="max_bytes=20000"):
        parse_xml(b"<r/>extra" + b" " * 20000, xml_limits=XmlLimits(max_bytes=20000))


def test_etree_obeys_tree_limits_but_has_no_input_byte_size():
    source = ET.fromstring("<r><c/></r>")

    assert parse_xml(source, xml_limits=XmlLimits(max_bytes=1)).root.children[0]._name_ == "c"
    with pytest.raises(ResourcePolicyError, match="max_depth=1"):
        parse_xml(source, xml_limits=XmlLimits(max_depth=1))
    with pytest.raises(ResourcePolicyError, match="max_elements=1"):
        parse_xml(source, xml_limits=XmlLimits(max_elements=1))


def test_etree_structural_limits_count_only_elements():
    source = ET.Element("r")
    source.extend([ET.Comment("omit"), ET.ProcessingInstruction("p")])

    doc = parse_xml(source, xml_limits=XmlLimits(max_depth=1, max_elements=1))

    assert doc.root.children == ()
