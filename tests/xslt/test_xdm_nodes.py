"""Contract tests for grounded XDM nodes and source provenance.

The node layer parses the original XML itself rather than relying on the
lossy public XPath projection: comments, processing instructions, namespace
bindings, base/document URIs, identity, document order, annotations, and
source locations all survive.  These tests are corpus-independent and cover
every new statement and branch in ``pyxsd.xdm.nodes``.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.source_spans import SourceIndex, SourceSpan
from pyxsd.xdm import XdmAtomic, as_xdm
from pyxsd.xdm.nodes import NodeKind, XdmAnnotations, XdmDocument, XdmNode

XSD_NS = "http://www.w3.org/2001/XMLSchema"
XML_NS = "http://www.w3.org/XML/1998/namespace"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
UNTYPED = f"{{{XSD_NS}}}untyped"
UNTYPED_ATOMIC = f"{{{XSD_NS}}}untypedAtomic"
STRING = f"{{{XSD_NS}}}string"


def _write(tmp_path: Path, content: bytes, name: str = "doc.xml") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _parse_indexed(
    tmp_path: Path, content: bytes, name: str = "doc.xml", **kwargs: Any
) -> tuple[Path, SourceIndex, XdmDocument]:
    path = _write(tmp_path, content, name)
    index = SourceIndex(str(path), content)
    return path, index, XdmDocument.parse(path, source_index=index, **kwargs)


# ---------------------------------------------------------------------------
# Node kinds, identity, provenance


def test_all_node_kinds_identity_and_provenance(tmp_path: Path) -> None:
    content = (
        b'<?xml version="1.0"?>\n'
        b"<!--before-->\n"
        b"<?top data?>\n"
        b'<root xmlns="urn:test" attr="A">'
        b"<child>value</child>"
        b"<!--inside-->"
        b"<?inner payload?>"
        b"<tail>t</tail>"
        b"</root>\n"
        b"<!--after-->"
    )
    path, index, doc = _parse_indexed(tmp_path, content)

    assert [child.kind for child in doc.children] == [
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
        NodeKind.ELEMENT,
        NodeKind.COMMENT,
    ]
    assert {child.kind for child in doc.children} >= {
        NodeKind.ELEMENT,
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
    }
    assert doc.kind is NodeKind.DOCUMENT
    assert isinstance(doc, XdmNode)
    assert doc.root_element is doc.root_element
    assert doc.root_element is doc.children[2]
    assert doc.source_index is index

    child = doc.root_element.children[0]
    expected_path = (
        PathStep("{urn:test}root", 1),
        PathStep("{urn:test}child", 1),
    )
    assert child.source_location() == IssueLocation(str(path), expected_path)
    expected_span = SourceSpan(
        content.index(b"<child>"),
        content.index(b"</child>") + len(b"</child>"),
    )
    assert child.source_span() == expected_span
    assert index.extract(expected_path) == b"<child>value</child>"

    assert doc.children[0].string_value == "before"
    assert doc.children[1].target == "top"
    assert doc.children[1].string_value == "data"
    assert doc.children[1].name == "top"
    assert doc.children[3].string_value == "after"
    assert doc.root_element.children[1].string_value == "inside"
    assert doc.root_element.children[2].target == "inner"
    assert doc.root_element.children[2].string_value == "payload"

    assert doc.root_element.name == "{urn:test}root"
    assert doc.root_element.local_name == "root"
    assert doc.root_element.namespace_uri == "urn:test"
    assert doc.root_element.prefix == ""
    assert as_xdm(doc.root_element) == (doc.root_element,)
    assert doc.root_element.node_id != child.node_id


def test_comments_text_attributes_and_processing_instructions(tmp_path: Path) -> None:
    content = b'<r a="1" xmlns:p="urn:p" p:b="2"><p:e p:c="3">text</p:e></r>'
    _, _, doc = _parse_indexed(tmp_path, content)
    root = doc.root_element

    assert root.name == "r"
    assert [attribute.name for attribute in root.attributes] == ["a", "{urn:p}b"]
    assert [attribute.string_value for attribute in root.attributes] == ["1", "2"]
    assert root.attribute("a") is root.attributes[0]
    assert root.attribute("missing") is None
    assert all(attribute.parent is root for attribute in root.attributes)
    assert all(attribute.children == () for attribute in root.attributes)
    assert all(attribute.descendants == () for attribute in root.attributes)

    child = root.children[0]
    assert child.name == "{urn:p}e"
    assert child.prefix == "p"
    assert child.local_name == "e"
    assert child.namespace_uri == "urn:p"
    assert child.string_value == "text"
    assert child.children[0].kind is NodeKind.TEXT
    assert child.children[0].parent is child
    assert child.children[0].string_value == "text"

    in_scope = root.in_scope_namespaces
    assert in_scope["p"] == "urn:p"
    assert in_scope["xml"] == XML_NS
    assert [node.prefix for node in root.namespace_nodes] == ["p", "xml"]
    assert root.namespace_node("p") is root.namespace_nodes[0]
    assert root.namespace_node("missing") is None
    namespace = root.namespace_node("p")
    assert namespace is not None
    assert namespace.kind is NodeKind.NAMESPACE
    assert namespace.parent is root
    assert namespace.name == "p"
    assert namespace.local_name == "p"
    assert namespace.prefix == "p"
    assert namespace.namespace_uri == "urn:p"
    assert namespace.string_value == "urn:p"
    assert namespace.children == ()
    assert namespace.attributes == ()
    assert namespace.namespace_nodes == ()

    assert root.children[0].annotations == XdmAnnotations(UNTYPED)
    assert root.attributes[0].annotations == XdmAnnotations(UNTYPED_ATOMIC)
    assert child.children[0].annotations == XdmAnnotations(UNTYPED_ATOMIC)
    assert namespace.annotations == XdmAnnotations(STRING)


def test_namespace_declarations_and_prefix_rebinding(tmp_path: Path) -> None:
    content = (
        b'<r xmlns:a="urn:one" xmlns="urn:def" xmlns:z="urn:z">'
        b'<a:c xmlns:a="urn:two" xmlns:b="urn:b"><a:d/></a:c>'
        b'<u xmlns=""><v/></u>'
        b"</r>"
    )
    _, _, doc = _parse_indexed(tmp_path, content)
    root = doc.root_element
    assert root.name == "{urn:def}r"

    assert dict(root.in_scope_namespaces) == {
        "": "urn:def",
        "a": "urn:one",
        "z": "urn:z",
        "xml": XML_NS,
    }
    assert dict(root.namespace_declarations) == {
        "": "urn:def",
        "a": "urn:one",
        "z": "urn:z",
    }

    child = root.children[0]
    assert child.name == "{urn:two}c"
    assert dict(child.in_scope_namespaces) == {
        "": "urn:def",
        "a": "urn:two",
        "b": "urn:b",
        "z": "urn:z",
        "xml": XML_NS,
    }
    assert dict(child.namespace_declarations) == {"a": "urn:two", "b": "urn:b"}
    assert {node.prefix: node.namespace_uri for node in child.namespace_nodes} == {
        "": "urn:def",
        "a": "urn:two",
        "b": "urn:b",
        "z": "urn:z",
        "xml": XML_NS,
    }
    assert child.namespace_node("a") is not None
    assert child.namespace_node("a").namespace_uri == "urn:two"  # type: ignore[union-attr]
    assert root.namespace_node("a") is not None
    assert root.namespace_node("a").namespace_uri == "urn:one"  # type: ignore[union-attr]
    assert child.children[0].name == "{urn:two}d"

    default_namespace = root.namespace_node("")
    assert default_namespace is not None
    assert default_namespace.name == ""
    assert default_namespace.local_name == ""
    assert default_namespace.namespace_uri == "urn:def"

    undeclared = root.children[1]
    assert undeclared.name == "u"
    assert undeclared.in_scope_namespaces.get("") is None
    assert undeclared.namespace_node("") is None
    assert undeclared.namespace_declarations == (("", None),)
    assert undeclared.children[0].name == "v"
    assert undeclared.children[0].namespace_declarations == ()


# ---------------------------------------------------------------------------
# Annotations, typed values, string values


def test_annotations_and_typed_values(tmp_path: Path) -> None:
    content = (
        b'<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        b' xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:p="urn:p">'
        b"<u>x</u>"
        b'<n xsi:nil="true"/>'
        b'<f xsi:nil="false">v</f>'
        b'<g xsi:nil="maybe"/>'
        b'<i xsi:type="xs:integer">42</i>'
        b'<s xsi:type="xs:string">v</s>'
        b'<q xsi:type="xs:QName">a</q>'
        b'<c xsi:type="p:Custom">z</c>'
        b'<d xmlns="urn:d" xsi:type="T2"/>'
        b'<e xmlns="" xsi:type="T3"/>'
        b'<m xsi:type="missing:T4"/>'
        b"<!--comment-->"
        b"<?pi data?>"
        b"</r>"
    )
    _, _, doc = _parse_indexed(tmp_path, content)
    root = doc.root_element
    children = {child.name: child for child in root.children if child.kind is NodeKind.ELEMENT}

    assert doc.annotations == XdmAnnotations(UNTYPED)
    assert root.annotations == XdmAnnotations(UNTYPED)
    assert not root.nilled
    assert root.type_name == UNTYPED

    untyped = children["u"]
    assert untyped.annotations == XdmAnnotations(UNTYPED)
    assert untyped.typed_value == (XdmAtomic.untyped_atomic("x"),)
    assert untyped.children[0].typed_value == (XdmAtomic.untyped_atomic("x"),)

    nilled = children["n"]
    assert nilled.annotations.nilled
    assert nilled.nilled
    assert nilled.typed_value == ()

    not_nilled = children["f"]
    assert not not_nilled.nilled
    assert not_nilled.typed_value == (XdmAtomic.untyped_atomic("v"),)

    invalid_nil = children["g"]
    assert not invalid_nil.nilled
    assert invalid_nil.typed_value == (XdmAtomic.untyped_atomic(""),)

    integer = children["i"]
    assert integer.annotations == XdmAnnotations(f"{{{XSD_NS}}}integer")
    assert integer.typed_value == (XdmAtomic.integer(42),)

    string = children["s"]
    assert string.annotations == XdmAnnotations(STRING)
    assert string.typed_value == (XdmAtomic.string("v"),)

    qname = children["q"]
    assert qname.annotations == XdmAnnotations(f"{{{XSD_NS}}}QName")
    assert qname.typed_value == (XdmAtomic.untyped_atomic("a"),)

    custom = children["c"]
    assert custom.annotations == XdmAnnotations("{urn:p}Custom")
    assert custom.typed_value == (XdmAtomic.untyped_atomic("z"),)

    defaulted = children["{urn:d}d"]
    assert defaulted.annotations == XdmAnnotations("{urn:d}T2")
    assert defaulted.typed_value == (XdmAtomic.untyped_atomic(""),)

    no_default = children["e"]
    assert no_default.annotations == XdmAnnotations("T3")
    assert no_default.typed_value == (XdmAtomic.untyped_atomic(""),)

    unbound = children["m"]
    assert unbound.annotations == XdmAnnotations("missing:T4")
    assert unbound.typed_value == (XdmAtomic.untyped_atomic(""),)

    attribute = root.attribute("xmlns:xs")
    assert attribute is None  # namespace declarations are never attributes
    typed_attribute = nilled.attribute(f"{{{XSI_NS}}}nil")
    assert typed_attribute is not None
    assert typed_attribute.annotations == XdmAnnotations(UNTYPED_ATOMIC)
    assert typed_attribute.typed_value == (XdmAtomic.untyped_atomic("true"),)

    comment = next(child for child in root.children if child.kind is NodeKind.COMMENT)
    instruction = next(
        child for child in root.children if child.kind is NodeKind.PROCESSING_INSTRUCTION
    )
    namespace = root.namespace_node("p")
    assert namespace is not None
    assert comment.annotations == XdmAnnotations(STRING)
    assert comment.typed_value == (XdmAtomic.string("comment"),)
    assert instruction.annotations == XdmAnnotations(STRING)
    assert instruction.typed_value == (XdmAtomic.string("data"),)
    assert namespace.annotations == XdmAnnotations(STRING)
    assert namespace.typed_value == (XdmAtomic.string("urn:p"),)


def test_string_values_merge_character_data(tmp_path: Path) -> None:
    content = b"<r>a<!--c-->b<![CDATA[c]]><x>d</x>e</r>"
    _, _, doc = _parse_indexed(tmp_path, content)
    root = doc.root_element

    assert [child.kind for child in root.children] == [
        NodeKind.TEXT,
        NodeKind.COMMENT,
        NodeKind.TEXT,
        NodeKind.ELEMENT,
        NodeKind.TEXT,
    ]
    assert [child.string_value for child in root.children] == ["a", "c", "bc", "d", "e"]
    assert root.string_value == "abcde"
    assert doc.string_value == "abcde"
    assert doc.typed_value == (XdmAtomic.untyped_atomic("abcde"),)
    assert root.children[2].string_value == "bc"

    entity_path, _, entity_doc = _parse_indexed(tmp_path, b"<r>a&amp;b</r>", name="entity.xml")
    assert entity_path.exists()
    entity_root = entity_doc.root_element
    assert len(entity_root.children) == 1
    assert entity_root.children[0].kind is NodeKind.TEXT
    assert entity_root.string_value == "a&b"

    _, _, empty_doc = _parse_indexed(tmp_path, b"<r/>", name="empty.xml")
    assert empty_doc.root_element.string_value == ""
    assert empty_doc.root_element.children == ()
    assert empty_doc.root_element.typed_value == (XdmAtomic.untyped_atomic(""),)


# ---------------------------------------------------------------------------
# Base and document URIs


def test_base_and_document_uris(tmp_path: Path) -> None:
    content = (
        b'<r xml:base="http://example.com/a/">t'
        b'<c xml:base="b"/><d xml:base=""/>'
        b'<e xml:base="http://other/x"/></r>'
    )
    _, _, doc = _parse_indexed(tmp_path, content, document_uri="http://example.com/root.xml")
    root = doc.root_element
    assert doc.document_uri == "http://example.com/root.xml"
    assert doc.base_uri == "http://example.com/root.xml"
    assert root.base_uri == "http://example.com/a/"
    assert root.children[0].base_uri == "http://example.com/a/"
    assert root.children[1].base_uri == "http://example.com/a/b"
    assert root.children[2].base_uri == "http://example.com/a/"
    assert root.children[3].base_uri == "http://other/x"
    assert root.attribute(f"{{{XML_NS}}}base") is root.attributes[0]
    assert root.attributes[0].base_uri == "http://example.com/a/"
    assert root.namespace_nodes[0].base_uri == "http://example.com/a/"
    assert root.children[0].document_uri == "http://example.com/root.xml"

    _, _, explicit_base = _parse_indexed(
        tmp_path,
        content,
        name="explicit.xml",
        document_uri="http://example.com/root.xml",
        base_uri="http://base/",
    )
    assert explicit_base.base_uri == "http://base/"
    assert explicit_base.document_uri == "http://example.com/root.xml"

    path = _write(tmp_path, b"<r/>", "path.xml")
    path_doc = XdmDocument.parse(path)
    assert path_doc.document_uri == path.resolve().as_uri()

    no_base = XdmDocument.parse(b"<r><c/></r>")
    assert no_base.base_uri is None
    assert no_base.root_element.base_uri is None
    assert no_base.root_element.children[0].base_uri is None

    relative = XdmDocument.parse(b'<r xml:base="rel"><c/></r>')
    assert relative.root_element.base_uri == "rel"
    assert relative.root_element.children[0].base_uri == "rel"


# ---------------------------------------------------------------------------
# Navigation and document order


def test_navigation_identity_and_document_order(tmp_path: Path) -> None:
    content = b'<r z="1"><a/><a/><b>t<c/></b></r>'
    path, index, doc = _parse_indexed(tmp_path, content)
    root = doc.root_element
    first, second, wrapper = root.children
    text = wrapper.children[0]
    child = wrapper.children[1]

    identified = (doc, root, first, second, wrapper, child)
    assert doc.node_id == 0
    assert len({node.node_id for node in identified}) == len(identified)
    assert doc.children[0] is root
    assert root.children[0] is first
    assert root.children[0] is root.children[0]

    assert first.following_sibling is second
    assert second.preceding_sibling is first
    assert first.next_sibling is second
    assert second.previous_sibling is first
    assert wrapper.following_sibling is None
    assert first.preceding_sibling is None
    assert first.next_sibling is second
    assert second.previous_sibling is first
    assert root.following_sibling is None
    assert root.preceding_sibling is None
    assert root.attributes[0].following_sibling is None
    assert root.attributes[0].preceding_sibling is None

    assert first.ancestors == (root, doc)
    assert child.ancestors == (wrapper, root, doc)
    assert first.descendants == ()
    assert wrapper.descendants == (text, child)
    assert root.descendants == (first, second, wrapper, text, child)
    assert root.root is doc
    assert child.root is doc
    assert root.document is doc
    assert child.document is doc
    assert doc.root is doc
    assert doc.document is doc

    assert root.document_order < root.namespace_nodes[0].document_order
    assert root.namespace_nodes[0].document_order < root.attributes[0].document_order
    assert root.attributes[0].document_order < first.document_order
    assert first.document_order < second.document_order
    assert second.document_order < wrapper.document_order
    assert wrapper.document_order < text.document_order
    assert text.document_order < child.document_order
    assert doc.document_order == 0

    assert first.compare_document_order(first) == 0
    assert first.compare_document_order(second) < 0
    assert second.compare_document_order(first) > 0
    assert first.is_before(second)
    assert second.is_after(first)
    assert not first.is_before(first)
    assert not first.is_after(first)
    assert not first.is_before(object())
    assert not first.is_after(object())
    assert first.compare_document_order(object()) is None  # type: ignore[arg-type]

    other_path = _write(tmp_path, b"<r/>", "other.xml")
    other = XdmDocument.parse(other_path)
    assert first.compare_document_order(other.root_element) is None
    assert not first.is_before(other.root_element)
    assert not first.is_after(other.root_element)

    assert first.source_location() == IssueLocation(str(path), (PathStep("r", 1), PathStep("a", 1)))
    assert second.source_location() == IssueLocation(
        str(path), (PathStep("r", 1), PathStep("a", 2))
    )
    assert index.extract((PathStep("r", 1), PathStep("a", 2))) == b"<a/>"
    assert first.source_span() == SourceSpan(
        content.index(b"<a/>"),
        content.index(b"<a/>") + len(b"<a/>"),
    )

    attribute = root.attributes[0]
    assert attribute.source_location() == IssueLocation(str(path), (PathStep("r", 1),), "z")
    assert attribute.source_span() is None
    assert text.source_location() is None
    assert root.namespace_nodes[0].source_location() is None
    assert first.source_span() is not None


def test_parentless_document_node(tmp_path: Path) -> None:
    _, _, doc = _parse_indexed(tmp_path, b"<r><c/></r>")
    assert doc.parent is None
    assert doc.root is doc
    assert doc.document is doc
    assert doc.following_sibling is None
    assert doc.preceding_sibling is None
    assert doc.ancestors == ()
    assert doc.descendants == (doc.root_element, doc.root_element.children[0])
    assert doc.compare_document_order(doc) == 0
    assert not doc.is_before(doc)
    assert not doc.is_after(doc)
    assert doc.source_location() is None
    assert doc.source_span() is None
    assert doc.attributes == ()
    assert doc.namespace_nodes == ()
    assert doc.in_scope_namespaces == {}
    assert doc.namespace_declarations == ()
    assert doc.name is None
    assert doc.local_name is None
    assert doc.prefix == ""
    assert doc.namespace_uri is None
    assert doc.target is None


def test_source_locations_without_an_explicit_index(tmp_path: Path) -> None:
    content = b"<r><a/><a/></r>"
    path = _write(tmp_path, content)
    doc = XdmDocument.parse(path)
    index = doc.source_index
    assert index is not None
    second = doc.root_element.children[1]
    expected_path = (PathStep("r", 1), PathStep("a", 2))
    assert second.source_location() == IssueLocation(str(path), expected_path)
    assert second.source_span() == index.span(expected_path)
    assert second.source_span() == SourceSpan(
        content.index(b"<a/>", content.index(b"<a/>") + 1),
        content.index(b"<a/>", content.index(b"<a/>") + 1) + 4,
    )


# ---------------------------------------------------------------------------
# Parsing source forms and errors


def test_parse_source_forms(tmp_path: Path) -> None:
    content = b'<r a="1"><c/></r>'
    path = _write(tmp_path, content)

    documents = [
        XdmDocument.parse(path),
        XdmDocument.parse(str(path)),
        XdmDocument.parse(content),
        XdmDocument.parse(bytearray(content)),
        XdmDocument.parse(memoryview(content)),
        XdmDocument.parse(io.BytesIO(content)),
        XdmDocument.parse(io.StringIO(content.decode("utf-8"))),
    ]
    for document in documents:
        assert document.root_element.name == "r"
        assert document.root_element.source_span() is not None
    for document in documents[:2]:
        assert document.document_uri == path.resolve().as_uri()
    for document in documents[2:]:
        assert document.document_uri is None
    assert documents[2].root_element.source_location() == IssueLocation(
        "<memory>", (PathStep("r", 1),)
    )

    with path.open("rb") as stream:
        binary_document = XdmDocument.parse(stream)
    with path.open("r", encoding="utf-8") as stream:
        text_document = XdmDocument.parse(stream)
    assert binary_document.document_uri == path.resolve().as_uri()
    assert text_document.document_uri == path.resolve().as_uri()
    assert text_document.root_element.name == "r"

    indexed = XdmDocument.parse(content, source_index=SourceIndex("given.xml", content))
    assert indexed.root_element.source_location() == IssueLocation("given.xml", (PathStep("r", 1),))

    named = XdmDocument.parse(content, source_name="named.xml")
    assert named.root_element.source_location() == IssueLocation("named.xml", (PathStep("r", 1),))

    marker = object()
    with_pool = XdmDocument.parse(content, schema_pool=marker)
    assert with_pool.schema_pool is marker

    overridden = XdmDocument.parse(
        content, document_uri="http://example.com/doc.xml", base_uri="http://base/"
    )
    assert overridden.document_uri == "http://example.com/doc.xml"
    assert overridden.base_uri == "http://base/"

    class NamedReader:
        name = "<stdin>"

        def read(self) -> str:
            return "<r/>"

    assert XdmDocument.parse(NamedReader()).document_uri is None

    class BadReader:
        name = 1

        def read(self) -> int:
            return 1

    with pytest.raises(TypeError):
        XdmDocument.parse(BadReader())
    with pytest.raises(TypeError):
        XdmDocument.parse(42)  # type: ignore[arg-type]


def test_malformed_xml_raises_pyxsd_error(tmp_path: Path) -> None:
    path = _write(tmp_path, b"<r>", "broken.xml")
    with pytest.raises(PyXSDError, match="malformed XML"):
        XdmDocument.parse(path)

    valid_index = SourceIndex("valid.xml", b"<r/>")
    with pytest.raises(PyXSDError, match="malformed XML"):
        XdmDocument.parse(b"<r>", source_index=valid_index)
