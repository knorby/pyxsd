"""Opt-in coherent XDM capture for ordinary schema-bound documents.

``Schema.parse(..., capture_xdm=True)`` retains a source-complete XDM tree,
the occurrence annotations from the same binding run, and an immutable
snapshot of the bound tree.  ``Document.to_xdm()`` exposes that view only
while the bound tree still matches the snapshot: any direct container or
attribute edit, child insertion/removal/reordering, text/tail edit, or
object replacement is rejected rather than annotated with stale XML.

The tests are corpus-independent and pin the source forms Task 9 could not
digest: non-seekable streams and pre-parsed ``Element`` trees.
"""

from __future__ import annotations

import hashlib
import io
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import parse_with_namespaces
from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.schema import Schema
from pyxsd.xdm.annotations import occurrence_capture_active
from pyxsd.xdm.errors import XdmInputError
from pyxsd.xdm.nodes import NodeKind, XdmDocument
from pyxsd.xml_policy import XmlLimits

XSD_NS = "http://www.w3.org/2001/XMLSchema"

SCHEMA_SOURCE = f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
  <xs:element name="catalog">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence><xs:element name="name" type="xs:string"/></xs:sequence>
            <xs:attribute name="status" type="xs:string" use="required"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
      <xs:attribute name="status" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

INPUT = (
    '<catalog status="ok">'
    '<item status="ok"><name>one</name></item>'
    '<item status="new"><name>two</name></item>'
    "</catalog>"
)

OTHER_INPUT = '<catalog status="other"><item status="other"><name>different</name></item></catalog>'


def _schema() -> Schema:
    schema = Schema.compile(io.StringIO(SCHEMA_SOURCE))
    assert not schema.report.has_errors, schema.report
    return schema


def _capture(xml: str = INPUT) -> Document:
    document = _schema().parse(io.StringIO(xml), capture_xdm=True)
    assert document.is_valid, document.report
    return document


def _texts(node: Any) -> list[str]:
    return [
        descendant.string_value
        for descendant in node.descendants
        if descendant.kind is NodeKind.TEXT and descendant.string_value.strip()
    ]


def test_capture_exposes_occurrence_annotations():
    document = _capture()

    xdm = document.to_xdm()

    assert isinstance(xdm, XdmDocument)
    assert xdm.root_element.name == "catalog"
    assert xdm.root_element.annotation.actual_type is type(document.root)
    assert xdm.root_element.annotation.validity == "valid"
    items = [
        child
        for child in xdm.root_element.children
        if child.kind is NodeKind.ELEMENT and child.name == "item"
    ]
    assert len(items) == 2
    assert items[0].annotation.actual_type is type(document.root._children_[0])
    assert items[1].annotation.actual_type is type(document.root._children_[1])
    assert items[0].annotation.actual_type is items[1].annotation.actual_type
    assert _texts(xdm.root_element) == ["one", "two"]


def test_capture_coherence():
    document = _capture()
    assert document.to_xdm().root_element.annotation.actual_type is type(document.root)

    document.root._attribs_["status"] = "changed"
    with pytest.raises(XdmInputError, match="changed since XDM capture"):
        document.to_xdm()


def test_uncaptured_document_fails_clearly():
    document = _schema().parse(io.StringIO(INPUT))
    assert document._xdm_capture is None

    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        document.to_xdm()


def test_uncaptured_element_document_fails_clearly():
    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        _schema().parse(ET.fromstring(INPUT)).to_xdm()


def test_capture_is_stable_when_the_bound_tree_is_unchanged():
    document = _capture()

    first = document.to_xdm()
    second = document.to_xdm()

    assert first is second
    assert not occurrence_capture_active()


def _replace_first_child(document: Document) -> None:
    other = _capture(OTHER_INPUT)
    document.root._children_[0] = other.root._children_[0]


def _insert_new_child(document: Document) -> None:
    first = document.root._children_[0]
    document.root._children_.append(type(first)())


def _edit_existing_value_list(document: Document) -> None:
    name = document.root._children_[0]._children_[0]
    assert name._value_ == ["one"]
    name._value_.append("changed")


MUTATIONS = {
    "attribute-value": lambda document: document.root._attribs_.__setitem__("status", "changed"),
    "attribute-add": lambda document: document.root._attribs_.__setitem__("extra", "x"),
    "attribute-remove": lambda document: document.root._attribs_.pop("status"),
    "attribute-container-replace": lambda document: setattr(
        document.root, "_attribs_", {"status": "changed"}
    ),
    "attribute-container-not-mapping": lambda document: setattr(document.root, "_attribs_", []),
    "child-container-not-walkable": lambda document: setattr(document.root, "_children_", 1),
    "child-cycle": lambda document: document.root._children_.append(document.root),
    "child-insert": _insert_new_child,
    "child-remove": lambda document: document.root._children_.pop(),
    "child-reorder": lambda document: document.root._children_.reverse(),
    "child-replace-object": _replace_first_child,
    "value-edit": lambda document: setattr(document.root._children_[0], "_value_", ["changed"]),
    "value-list-edit": _edit_existing_value_list,
    "tail-edit": lambda document: setattr(document.root._children_[0], "_tail_", "changed"),
    "name-edit": lambda document: setattr(document.root._children_[0], "_name_", "other"),
    "root-replace-object": lambda document: setattr(document, "root", _capture(OTHER_INPUT).root),
}


@pytest.mark.parametrize("mutation", MUTATIONS.values(), ids=MUTATIONS.keys())
def test_every_bound_tree_mutation_rejects_the_stale_view(mutation):
    document = _capture()
    document.to_xdm()

    mutation(document)

    with pytest.raises(XdmInputError, match="changed since XDM capture"):
        document.to_xdm()


def test_restoring_the_captured_shape_makes_the_view_coherent_again():
    document = _capture()
    original_attributes = dict(document.root._attribs_)
    original_children = list(document.root._children_)

    document.root._attribs_["status"] = "changed"
    document.root._children_.reverse()
    with pytest.raises(XdmInputError, match="changed since XDM capture"):
        document.to_xdm()

    document.root._attribs_.clear()
    document.root._attribs_.update(original_attributes)
    document.root._children_[:] = original_children
    assert document.to_xdm().root_element.name == "catalog"


def test_value_list_edit_mutates_an_existing_list():
    document = _capture()
    name = document.root._children_[0]._children_[0]
    assert name._value_ == ["one"]

    name._value_.append("changed")
    with pytest.raises(XdmInputError, match="changed since XDM capture"):
        document.to_xdm()


def test_tail_edit_detected_when_the_original_tail_was_absent():
    document = _capture()
    assert getattr(document.root._children_[0], "_tail_", None) is None

    document.root._children_[0]._tail_ = "\n"
    with pytest.raises(XdmInputError, match="changed since XDM capture"):
        document.to_xdm()


def test_caller_built_document_has_no_capture():
    parsed = _capture()
    manual = Document(
        schema=parsed.schema,
        root=parsed.root,
        source=parsed.source,
        report=parsed.report,
    )

    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        manual.to_xdm()


def test_transformed_document_has_no_capture():
    parsed = _capture()

    transformed = parsed.transform(lambda root: root)

    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        transformed.to_xdm()


def test_capture_state_is_per_document_across_schema_reuse():
    schema = _schema()
    first = schema.parse(io.StringIO(INPUT), capture_xdm=True)
    second = schema.parse(io.StringIO(OTHER_INPUT), capture_xdm=True)
    plain = schema.parse(io.StringIO(INPUT))

    assert _texts(first.to_xdm().root_element)[0] == "one"
    assert _texts(second.to_xdm().root_element)[0] == "different"
    assert first.to_xdm() is first.to_xdm()
    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        plain.to_xdm()
    assert _texts(first.to_xdm().root_element)[0] == "one"


def test_unknown_root_still_captures_the_source_document():
    document = _schema().parse(io.StringIO("<unknown/>"), capture_xdm=True)

    xdm = document.to_xdm()

    assert xdm.root_element.name == "unknown"
    document.root = object()
    with pytest.raises(XdmInputError, match="changed since XDM capture"):
        document.to_xdm()


def test_path_capture_is_source_complete(tmp_path: Path):
    content = (
        b'<catalog status="ok"><!--lead--><?work go?>'
        b'<item status="ok"><name>one</name></item></catalog>'
    )
    path = tmp_path / "doc.xml"
    path.write_bytes(content)

    document = _schema().parse(path, capture_xdm=True)
    xdm = document.to_xdm()

    assert xdm.source_index.digest == hashlib.sha256(content).hexdigest()
    assert xdm.document_uri == path.resolve().as_uri()
    assert xdm.root_element.source_span() is not None
    assert [child.kind for child in xdm.root_element.children] == [
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
        NodeKind.ELEMENT,
    ]
    assert xdm.root_element.annotation.actual_type is type(document.root)


def test_capture_reports_an_unreadable_path(tmp_path: Path):
    missing = tmp_path / "missing.xml"

    with pytest.raises(PyXSDError, match="could not be read"):
        _schema().parse(missing, capture_xdm=True)


def test_text_stream_capture_digests_the_utf8_text():
    xml = '<catalog status="ok"><!--lead--><item status="ok"><name>one</name></item></catalog>'
    document = _schema().parse(io.StringIO(xml), capture_xdm=True)

    xdm = document.to_xdm()

    assert xdm.source_index.digest == hashlib.sha256(xml.encode("utf-8")).hexdigest()
    assert xdm.root_element.children[0].kind is NodeKind.COMMENT
    assert xdm.root_element.annotation.actual_type is type(document.root)


class _NonSeekable:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, size: int = -1) -> bytes:
        del size
        data, self._data = self._data, b""
        return data

    def seekable(self) -> bool:
        return False


class _Chunked:
    """A non-seekable reader that returns one stored chunk per read call."""

    def __init__(self, chunks: list[bytes] | list[str]) -> None:
        self._chunks = list(chunks)
        self._empty: bytes | str = chunks[0][:0]
        self.reads = 0

    def read(self, size: int = -1) -> bytes | str:
        del size
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else self._empty

    def seekable(self) -> bool:
        return False


def test_chunked_non_seekable_stream_reads_to_eof_for_capture():
    chunks = [
        b'<catalog status="ok">',
        b'<item status="ok">',
        b"<name>one</name>",
        b"</item>",
        b"<!--tail-->",
        b"<?work done?>",
        b"</catalog>",
    ]
    content = b"".join(chunks)
    stream = _Chunked(chunks)

    document = _schema().parse(stream, capture_xdm=True)
    xdm = document.to_xdm()

    assert stream.reads > len(chunks) - 1  # EOF was probed, not assumed
    assert xdm.source_index.digest == hashlib.sha256(content).hexdigest()
    assert [child.kind for child in xdm.root_element.children] == [
        NodeKind.ELEMENT,
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
    ]
    assert _texts(xdm.root_element) == ["one"]
    assert xdm.root_element.annotation.actual_type is type(document.root)


def test_chunked_non_seekable_text_stream_reads_to_eof():
    chunks = [
        '<catalog status="ok">',
        '<item status="ok">',
        "<name>one</name>",
        "</item>",
        "<!--tail-->",
        "</catalog>",
    ]
    content = "".join(chunks).encode("utf-8")
    stream = _Chunked(chunks)

    document = _schema().parse(stream, capture_xdm=True)
    xdm = document.to_xdm()

    assert xdm.source_index.digest == hashlib.sha256(content).hexdigest()
    assert xdm.root_element.children[-1].kind is NodeKind.COMMENT


def test_xdm_document_parse_reads_chunked_stream_to_eof():
    chunks = [
        b'<catalog status="ok">',
        b'<item status="ok"><name>one</name></item>',
        b"<!--tail-->",
        b"</catalog>",
    ]
    content = b"".join(chunks)

    xdm = XdmDocument.parse(_Chunked(chunks))

    assert xdm.source_index.digest == hashlib.sha256(content).hexdigest()
    assert xdm.root_element.children[-1].kind is NodeKind.COMMENT


@pytest.mark.parametrize("chunks", [[b"<r", "<x/>"], ["<r", b"<x/>"]])
def test_stream_rejects_mixed_chunk_types(chunks):
    with pytest.raises(TypeError, match="mixed"):
        XdmDocument.parse(_Chunked(chunks))


class _GuardedStream:
    """A non-seekable stream that records how many source bytes were read."""

    def __init__(self, content: bytes) -> None:
        self._content = content
        self.position = 0
        self.consumed = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._content) - self.position
        chunk = self._content[self.position : self.position + size]
        self.position += len(chunk)
        self.consumed += len(chunk)
        return chunk

    def seekable(self) -> bool:
        return False


def test_capture_read_is_capped_by_max_bytes():
    stream = _GuardedStream(b"<catalog/>" + b" " * 64)

    with pytest.raises(ResourcePolicyError, match="max_bytes=8"):
        _schema().parse(stream, xml_limits=XmlLimits(max_bytes=8), capture_xdm=True)

    assert stream.consumed == 9


def _unicode_schema() -> Schema:
    source = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="urn:café"
        xmlns:u="urn:café" elementFormDefault="qualified">
      <xs:element name="café" type="xs:string"/>
    </xs:schema>"""
    schema = Schema.compile(io.StringIO(source))
    assert not schema.report.has_errors, schema.report
    return schema


def test_text_stream_encoding_declaration_is_rewritten_and_coherent():
    text = '<?xml version="1.0" encoding="ISO-8859-1"?><café xmlns="urn:café">hé</café>'
    normalized = text.replace('encoding="ISO-8859-1"', 'encoding="UTF-8"').encode("utf-8")

    document = _unicode_schema().parse(io.StringIO(text), capture_xdm=True)
    xdm = document.to_xdm()

    assert xdm.source_index.digest == hashlib.sha256(normalized).hexdigest()
    assert xdm.root_element.name == "{urn:café}café"
    assert xdm.root_element.source_span() is not None
    assert xdm.root_element.annotation.actual_type is type(document.root)


def test_text_stream_utf16_declaration_is_rewritten_and_coherent():
    text = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<catalog status="ok"><item status="ok"><name>one</name></item></catalog>'
    )
    normalized = text.replace('encoding="UTF-16"', 'encoding="UTF-8"').encode("utf-8")

    document = _schema().parse(io.StringIO(text), capture_xdm=True)
    xdm = document.to_xdm()

    assert xdm.source_index.digest == hashlib.sha256(normalized).hexdigest()
    assert xdm.root_element.name == "catalog"
    assert xdm.root_element.source_span() is not None
    assert xdm.root_element.annotation.actual_type is type(document.root)


def test_text_stream_bom_is_removed_before_digest():
    text = "\ufeff" + '<catalog status="ok"><item status="ok"><name>one</name></item></catalog>'

    document = _schema().parse(io.StringIO(text), capture_xdm=True)
    xdm = document.to_xdm()

    assert xdm.source_index.digest == hashlib.sha256(text[1:].encode("utf-8")).hexdigest()
    assert xdm.root_element.name == "catalog"
    assert xdm.root_element.source_span() is not None


def test_non_seekable_stream_capture_is_digested_and_coherent():
    content = INPUT.encode("utf-8")
    document = _schema().parse(_NonSeekable(content), capture_xdm=True)

    xdm = document.to_xdm()
    assert xdm.source_index.digest == hashlib.sha256(content).hexdigest()
    assert xdm.root_element.annotation.actual_type is type(document.root)

    document.root._attribs_["status"] = "changed"
    with pytest.raises(XdmInputError, match="changed since XDM capture"):
        document.to_xdm()


def test_preparsed_element_capture_digests_a_serialization():
    element = ET.fromstring(INPUT)

    document = _schema().parse(element, capture_xdm=True)
    xdm = document.to_xdm()

    assert len(xdm.source_index.digest) == 64
    assert xdm.root_element.name == "catalog"
    assert xdm.root_element.annotation.actual_type is type(document.root)

    document.root._children_.pop()
    with pytest.raises(XdmInputError, match="changed since XDM capture"):
        document.to_xdm()


def test_preparsed_element_tail_is_outside_the_captured_document():
    element = ET.fromstring(INPUT)
    element.tail = "outside"

    document = _schema().parse(element, capture_xdm=True)

    assert document.to_xdm().root_element.name == "catalog"
    assert element.tail == "outside"


def test_element_capture_rejects_unsupported_reader_data():
    class OddReader:
        def read(self) -> int:
            return 1

    with pytest.raises(TypeError):
        _schema().parse(OddReader(), capture_xdm=True)  # type: ignore[arg-type]


class _MemoryPolicy:
    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        del base, purpose
        return ResourceData(source, Path(source).read_bytes())

    def parse(self, data: ResourceData, context: Any) -> ET.Element:
        return parse_with_namespaces(io.BytesIO(data.content), context)


def test_resource_policy_capture_uses_admitted_bytes():
    policy = _MemoryPolicy()
    schema = Schema.compile(io.StringIO(SCHEMA_SOURCE), resource_policy=policy)
    assert not schema.report.has_errors, schema.report

    document = schema.parse(io.StringIO(INPUT), capture_xdm=True)
    xdm = document.to_xdm()

    assert xdm.root_element.name == "catalog"
    assert xdm.root_element.annotation.actual_type is type(document.root)
    assert len(xdm.source_index.digest) == 64


def test_uncaptured_resource_policy_document_fails_clearly():
    policy = _MemoryPolicy()
    schema = Schema.compile(io.StringIO(SCHEMA_SOURCE), resource_policy=policy)
    assert not schema.report.has_errors, schema.report

    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        schema.parse(io.StringIO(INPUT)).to_xdm()
