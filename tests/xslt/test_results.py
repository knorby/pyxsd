"""Public result views, shapes, and in-memory result trees.

The result layer keeps the raw XDM sequence separate from any delivered tree
and never serializes or reparses it.  These tests exercise the independent
result objects and the sequence-normalization boundary directly.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from pyxsd.exceptions import PyXSDError
from pyxsd.xdm import XdmArray, XdmAtomic, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.results import (
    Message,
    ResultEntry,
    ResultShapeError,
    TransformationResult,
    _append_complex_content,
    _build_result_tree,
    _NodeFactory,
)

OUT_URI = "http://example.com/result.xml"
SECONDARY_URI = "http://example.com/secondary.xml"
RAW_MAP = XdmMap.from_mapping({"k": 1})


def _error_code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


def _factory() -> _NodeFactory:
    return _NodeFactory()


def _document(xml: bytes) -> XdmDocument:
    return XdmDocument.parse(xml)


def _element_children(node: XdmNode) -> tuple[XdmNode, ...]:
    return tuple(child for child in node.children if child.kind is NodeKind.ELEMENT)


def test_result_entry_normalizes_and_freezes_views() -> None:
    entry = ResultEntry(
        [XdmAtomic.integer(1)],
        output_uri=OUT_URI,
        serialization={"method": "xml"},
        messages=(Message("hello"),),
    )

    assert entry.raw == (XdmAtomic.integer(1),)
    assert isinstance(entry.serialization, MappingProxyType)
    assert entry.serialization["method"] == "xml"
    assert entry.serialized is None
    assert entry.messages[0].text == "hello"
    with pytest.raises(FrozenInstanceError):
        entry.output_uri = SECONDARY_URI  # type: ignore[misc]

    with pytest.raises(ValueError, match="absolute URI"):
        ResultEntry((), output_uri="relative/result.xml")
    with pytest.raises(TypeError, match="XDM items"):
        ResultEntry((1,))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="XdmDocument"):
        ResultEntry((), tree=_document(b"<x/>").root_element)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="serialized"):
        ResultEntry((), serialized=b"<x/>")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="serialization parameters"):
        ResultEntry((), serialization={"method": 1})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="messages"):
        ResultEntry((), messages=("not a message",))  # type: ignore[arg-type]


def test_transformation_result_exposes_principal_and_secondary() -> None:
    principal = ResultEntry(
        (XdmAtomic.integer(1), XdmAtomic.integer(2)),
        output_uri=OUT_URI,
        serialization={"method": "xml"},
    )
    secondary_entry = ResultEntry(
        (RAW_MAP,),
        output_uri=SECONDARY_URI,
        serialization={"method": "adaptive"},
    )
    result = TransformationResult(
        principal,
        secondary={SECONDARY_URI: secondary_entry},
        messages=(Message("note"),),
    )

    assert result.principal == (XdmAtomic.integer(1), XdmAtomic.integer(2))
    assert result.secondary[SECONDARY_URI].raw == (RAW_MAP,)
    assert isinstance(result.secondary, MappingProxyType)
    assert result.messages[0].text == "note"
    with pytest.raises(ResultShapeError, match="not a document tree"):
        _ = result.principal_tree
    with pytest.raises(FrozenInstanceError):
        result.messages = ()  # type: ignore[misc]

    document = _document(b"<result/>")
    shaped = TransformationResult(ResultEntry((document,), output_uri=OUT_URI))
    assert shaped.principal_tree is document
    with pytest.raises(ResultShapeError, match="not a document tree"):
        _ = TransformationResult(ResultEntry(())).principal_tree
    with pytest.raises(ResultShapeError, match="not a document tree"):
        _ = TransformationResult(ResultEntry((document, document))).principal_tree


def test_transformation_result_rejects_bad_secondary_entries() -> None:
    principal = ResultEntry(())
    entry = ResultEntry((), output_uri=SECONDARY_URI)

    with pytest.raises(ValueError, match="absolute URI"):
        TransformationResult(principal, secondary={"relative.xml": entry})
    with pytest.raises(TypeError, match="ResultEntry"):
        TransformationResult(principal, secondary={SECONDARY_URI: object()})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="output_uri"):
        TransformationResult(principal, secondary={OUT_URI: entry})
    with pytest.raises(TypeError, match="messages"):
        TransformationResult(principal, messages=("bad",))  # type: ignore[arg-type]


def test_build_result_tree_normalizes_atomics_documents_and_text() -> None:
    source = _document(b"<root>a<child/>b</root>")
    sequence = (
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
        source,
        XdmAtomic.string("tail"),
        XdmAtomic.string(""),
    )

    document = _build_result_tree(sequence, output_uri=OUT_URI)

    assert document.kind is NodeKind.DOCUMENT
    assert document.document_uri == OUT_URI
    assert document.base_uri == OUT_URI
    assert root_string(document) == "1 2" + "a" + "b" + "tail "
    kinds = [child.kind for child in document.children]
    assert kinds == [NodeKind.TEXT, NodeKind.ELEMENT, NodeKind.TEXT]
    assert document.root_element.name == "root"
    assert document.document_order >= 0
    assert document.children[0].document_order >= 0


def root_string(document: XdmDocument) -> str:
    return "".join(child.string_value for child in document.children)


def test_build_result_tree_handles_empty_and_adjacent_text() -> None:
    empty = _build_result_tree((), output_uri=None)
    assert empty.children == ()
    assert empty.document_uri is None

    first = _document(b"<one>a</one>")
    second = _document(b"<two>b</two>")
    document = _build_result_tree((first, second))
    assert [child.local_name for child in _element_children(document)] == ["one", "two"]

    text = _build_result_tree(
        (XdmAtomic.string("a"), XdmAtomic.string("b")),
        output_uri=OUT_URI,
    )
    assert len(text.children) == 1
    assert text.children[0].string_value == "a b"


def test_build_result_tree_rejects_function_items_and_bad_nodes() -> None:
    with pytest.raises(XsltError) as error:
        _build_result_tree((XdmMap.from_mapping({"k": 1}),))
    assert _error_code(error.value) == "XTDE0450"

    flattened = _build_result_tree((XdmArray([1]),))
    assert flattened.children[0].string_value == "1"

    attribute = _document(b"<x a='1'/>").root_element.attributes[0]
    with pytest.raises(XsltError) as error:
        _build_result_tree((attribute,))
    assert _error_code(error.value) == "XTDE0420"

    namespace = _document(b"<x xmlns:p='urn:p'/>").root_element.namespace_nodes[0]
    with pytest.raises(XsltError) as error:
        _build_result_tree((namespace,))
    assert _error_code(error.value) == "XTDE0420"


def test_element_content_requires_attributes_first() -> None:
    factory = _factory()
    element = factory.element("{urn:test}item")
    text = factory.text("value")
    attribute = _document(b"<x a='1'/>").root_element.attributes[0]

    with pytest.raises(XsltError) as error:
        _append_complex_content(factory, element, (text, attribute), allow_attributes=True)
    assert _error_code(error.value) == "XTDE0410"

    other = factory.element("{urn:test}other")
    _append_complex_content(factory, other, (attribute,), allow_attributes=True)
    assert other.attributes[0].string_value == "1"


def test_result_shape_error_is_a_pyxsd_error() -> None:
    error = ResultShapeError("no tree")

    assert isinstance(error, PyXSDError)
    assert "no tree" in str(error)


def test_message_is_immutable() -> None:
    message = Message("text", code="XTMM9000")

    assert message.terminate is False
    with pytest.raises(FrozenInstanceError):
        message.text = "changed"  # type: ignore[misc]


def test_message_validation_branches() -> None:
    with pytest.raises(TypeError, match="message text"):
        Message(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="message code"):
        Message("x", code=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="message uri"):
        Message("x", uri=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="terminate"):
        Message("x", terminate="no")  # type: ignore[arg-type]


def test_result_entry_rejects_empty_uri_and_bad_parameter_mapping() -> None:
    with pytest.raises(ValueError, match="non-empty absolute URI"):
        ResultEntry((), output_uri="")
    with pytest.raises(TypeError, match="serialization parameters"):
        ResultEntry((), serialization=[])  # type: ignore[arg-type]


def test_transformation_result_rejects_bad_container_types() -> None:
    with pytest.raises(TypeError, match="principal_entry"):
        TransformationResult(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="secondary results"):
        TransformationResult(ResultEntry(()), secondary=[])  # type: ignore[arg-type]


def test_namespace_prefixes_and_namespace_nodes_are_preserved() -> None:
    source = _document(b"<p:root xmlns:p='urn:p' a='1'><p:child/></p:root>")
    document = _build_result_tree((source.root_element,))
    root = document.root_element

    assert root.name == "{urn:p}root"
    assert root.prefix == "p"
    assert any(node.prefix == "p" for node in root.namespace_nodes)
    assert root.attributes[0].string_value == "1"
    assert _element_children(root)[0].name == "{urn:p}child"

    factory = _factory()
    namespaced = factory.element(
        "{urn:default}element",
        namespaces={"": "urn:default"},
        attributes=(("{urn:default}attr", "v"),),
    )
    assert namespaced.prefix == ""
    assert namespaced.attributes[0].prefix == "ns1"
    assert {node.prefix: node.string_value for node in namespaced.namespace_nodes}[
        "ns1"
    ] == "urn:default"

    factory = _factory()
    prefixed = factory.element(
        "{urn:p}other",
        namespaces={"p": "urn:p"},
    )
    assert prefixed.prefix == "p"


def test_comment_pi_and_namespace_content_branches() -> None:
    source = _document(b"<root><!--c--><?pi?><child xmlns:p='urn:p'/></root>")
    comment = source.root_element.children[0]
    instruction = source.root_element.children[1]
    namespace = source.root_element.children[2].namespace_nodes[0]
    document = _build_result_tree((source.root_element,))
    root = document.root_element

    assert [child.kind for child in root.children] == [
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
        NodeKind.ELEMENT,
    ]

    factory = _factory()
    element = factory.element("root")
    _append_complex_content(
        factory,
        element,
        (namespace, comment, instruction),
        allow_attributes=True,
    )
    p_namespace = element.namespace_node("p")
    assert p_namespace is not None
    assert p_namespace.prefix == "p"
    assert element.children[0].kind is NodeKind.COMMENT
    assert element.children[1].kind is NodeKind.PROCESSING_INSTRUCTION


def test_namespaced_element_without_a_bound_prefix_gets_a_default_binding() -> None:
    factory = _factory()
    element = factory.element("{urn:unbound}name")

    assert element.prefix == ""
    assert element.in_scope_namespaces[""] == "urn:unbound"
    assert any(
        node.prefix == "" and node.string_value == "urn:unbound" for node in element.namespace_nodes
    )


def test_constructed_and_cloned_annotations_are_preserved() -> None:
    from pyxsd.xdm.nodes import XdmAnnotation

    factory = _factory()
    element = factory.element("{urn:x}item", attributes=(("a", "1"),))
    text = factory.text("value")

    assert element.annotation.type_name == "{http://www.w3.org/2001/XMLSchema}untyped"
    assert element.attributes[0].annotation.type_name == (
        "{http://www.w3.org/2001/XMLSchema}untypedAtomic"
    )
    assert text.annotation.type_name == ("{http://www.w3.org/2001/XMLSchema}untypedAtomic")

    source = _document(b"<typed/>").root_element
    source._apply_occurrence_annotation(XdmAnnotation("urn:type", False))
    clone = _build_result_tree((source,)).root_element
    assert clone.annotation is source.annotation


def test_namespace_prefix_generation_and_collision() -> None:
    from pyxsd.xslt.results import _prefix_for

    assert _prefix_for("urn:missing", {}) == ""

    factory = _factory()
    collided = factory.element(
        "{urn:new}element",
        namespaces={"": "urn:other", "ns1": "urn:other-two"},
    )
    assert collided.prefix == "ns2"
    assert collided.in_scope_namespaces["ns2"] == "urn:new"

    factory = _factory()
    occupied_default = factory.element(
        "{urn:new}element",
        namespaces={"": "urn:occupied"},
    )
    assert occupied_default.prefix == "ns1"
    assert occupied_default.in_scope_namespaces["ns1"] == "urn:new"
