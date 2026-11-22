"""Contracts of the schema-neutral tree and RELAX NG compatibility layer."""

from xml.etree import ElementTree as ET

import pytest

from pyxsd._lexical import LexicalElement, clone_lexical, serialize_lexical
from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.nodes import XMLNode
from pyxsd.relaxng.origins import SourceOrigin
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.xml_writer import clone_relaxng, serialize_relaxng


def test_lexical_tree_is_not_a_relaxng_node() -> None:
    root = LexicalElement.from_etree(ET.fromstring("<r a='1'>x<c/>y</r>"))
    assert isinstance(root, LexicalElement)
    assert not isinstance(root, RelaxNGElement)
    assert list(root.iter_content()) == ["x", root.children[0], "y"]
    assert serialize_lexical(root) == '<r a="1">x<c/>y</r>'
    assert isinstance(root, XMLNode)
    assert type(root.children[0]) is LexicalElement


def test_relaxng_node_remains_a_lexical_node() -> None:
    root = RelaxNGElement.from_etree(ET.fromstring("<r><c/></r>"))
    assert isinstance(root, LexicalElement)
    assert type(clone_relaxng(root)) is RelaxNGElement


def test_lexical_clone_has_independent_storage_and_keeps_occurrence_context() -> None:
    root = LexicalElement.from_etree(
        ET.fromstring('<r xml:base="sub/"><c>first</c>tail<c/></r>'),
        base_uri="https://example.test/root/",
        source=IssueLocation("doc.xml", ()),
    )
    first, second = root.children
    second.set_namespace_bindings({"p": "urn:p"})
    root.append_child(root.remove_child(first))

    clone = clone_lexical(root)
    assert type(clone) is LexicalElement
    assert clone.revision.root is clone
    assert clone.revision.revision_id != root.revision.revision_id
    assert clone.revision.dirty
    assert clone.children[0].parent is clone
    assert clone.children[0].base_uri == "https://example.test/root/sub/"
    assert clone.children[0].namespaces == {"p": "urn:p"}
    assert clone.children[0].source_location == IssueLocation(
        "doc.xml", (PathStep("r", 1), PathStep("c", 2))
    )
    assert serialize_lexical(clone) == (
        '<r xml:base="sub/"><c xmlns:p="urn:p"/><c>first</c>tail</r>'
    )
    clone.children[1].set_text("copy")
    assert first.text_value() == "first"


def test_generic_factories_reject_nonlexical_inputs() -> None:
    with pytest.raises(TypeError, match=r"from_etree\(\) or clone_lexical\(\)"):
        LexicalElement()
    with pytest.raises(TypeError, match=r"element must be an xml\.etree\.ElementTree\.Element"):
        LexicalElement.from_etree(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="root must be a LexicalElement"):
        serialize_lexical(ET.Element("r"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="root must be a LexicalElement"):
        clone_lexical(ET.Element("r"))  # type: ignore[arg-type]


def test_generic_tree_takes_locations_and_relaxng_accepts_origins() -> None:
    element = ET.Element("r")
    origin = SourceOrigin("doc.xml", IssueLocation("doc.xml", ()))
    with pytest.raises(TypeError, match="source must be an IssueLocation or None"):
        LexicalElement.from_etree(element, source=origin)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="source must be an IssueLocation, SourceOrigin, or None"):
        RelaxNGElement.from_etree(element, source="doc.xml")  # type: ignore[arg-type]
    assert RelaxNGElement.from_etree(element, source=origin).source_location == IssueLocation(
        "doc.xml", (PathStep("r", 1),)
    )
    with pytest.raises(TypeError, match=r"element must be an xml\.etree\.ElementTree\.Element"):
        RelaxNGElement.from_etree(object(), source=origin)  # type: ignore[arg-type]


def test_lexical_conversion_omits_captured_comments_and_pis_without_losing_tails() -> None:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    element = ET.fromstring("<r>a<!-- before -->b<c/>c<?p value?>d<!-- last --></r>", parser)

    root = LexicalElement.from_etree(element)
    assert list(root.iter_content()) == ["ab", root.children[0], "cd"]
    assert serialize_lexical(root) == "<r>ab<c/>cd</r>"
    with pytest.raises(TypeError, match="element must have an element name"):
        LexicalElement.from_etree(ET.Comment("not an element"))


def test_relaxng_entry_points_reject_generic_lexical_nodes() -> None:
    root = RelaxNGElement.from_etree(ET.fromstring("<r><c/></r>"))
    generic = LexicalElement.from_etree(ET.Element("c"))

    with pytest.raises(TypeError, match="root must be a RelaxNGElement"):
        serialize_relaxng(generic)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="root must be a RelaxNGElement"):
        clone_relaxng(generic)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="child must be a RelaxNGElement"):
        root.append_child(generic)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="child must be a RelaxNGElement"):
        root.insert_child(0, generic)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="child must be a RelaxNGElement"):
        root.replace_child(0, generic)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="cannot be interpreted as an integer"):
        root.remove_child(generic)  # type: ignore[arg-type]

    assert not root.revision.dirty
    assert serialize_relaxng(root) == "<r><c/></r>"


def test_generic_edits_reject_nonlexical_children() -> None:
    root = LexicalElement.from_etree(ET.Element("r"))
    with pytest.raises(TypeError, match="child must be a LexicalElement"):
        root.append_child(ET.Element("c"))  # type: ignore[arg-type]
    assert not root.revision.dirty
    assert not root.children


def test_schema_neutral_typed_access_uses_only_the_revisions_snapshot() -> None:
    class IntegerSnapshot:
        def scalar_value(self, node: LexicalElement) -> int:
            return int(node.text_value() or "0")

        def attribute_value(self, node: LexicalElement, expanded_name: str) -> int:
            return int(node.attribute_lexical(expanded_name) or "0")

    root = LexicalElement.from_etree(ET.fromstring('<r a="03">02</r>'))
    with pytest.raises(PyXSDError, match="requires a typed interpretation"):
        root.scalar_value()
    with pytest.raises(PyXSDError, match="requires a typed interpretation"):
        root.attribute_value("a")

    root.revision._attach_typed_snapshot(IntegerSnapshot())
    assert root.scalar_value() == 2
    assert root.attribute_value("a") == 3
    assert serialize_lexical(root) == '<r a="03">02</r>'

    with pytest.raises(PyXSDError, match="requires a typed interpretation"):
        clone_lexical(root).scalar_value()
