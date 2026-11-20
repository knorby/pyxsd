"""Lexical fidelity tests for the RELAX NG document tree and writer.

The lexical tree is the single authoritative representation of an instance
document.  These tests pin expanded names, lexical attributes and character
data, ordered mixed content, namespace and base-URI frames, source links, and
the mutation contract.  The schema-neutral writer must round-trip character
content without adding XSD/XSI behavior.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET

import pytest

from pyxsd import XMLNode
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import XML_NS, NamespaceContext, parse_with_namespaces
from pyxsd.relaxng.origins import SourceOrigin
from pyxsd.relaxng.tree import LexicalRevision, NodeContext, RelaxNGElement
from pyxsd.relaxng.xml_writer import clone_relaxng, serialize_relaxng
from pyxsd.schema_base import SchemaBase


def _from_xml(
    xml: str,
    *,
    base_uri: str | None = None,
    source: IssueLocation | SourceOrigin | None = None,
) -> tuple[RelaxNGElement, NamespaceContext]:
    context = NamespaceContext()
    element = parse_with_namespaces(io.StringIO(xml), context)
    tree = RelaxNGElement.from_etree(
        element,
        namespaces=context,
        base_uri=base_uri,
        source=source,
    )
    return tree, context


def _shape(element: ET.Element) -> tuple[object, ...]:
    return (
        element.tag,
        dict(element.attrib),
        element.text,
        element.tail,
        [_shape(child) for child in element],
    )


class TestLexicalShape:
    def test_expanded_names_attributes_and_repeated_interleaved_children(self):
        tree, _ = _from_xml('<r xmlns:p="urn:p" p:id="1" plain="v"><a/><b><a/></b><a>x</a></r>')

        assert isinstance(tree, XMLNode)
        assert not isinstance(tree, SchemaBase)
        assert tree._name_ == "r"
        assert tree._attribs_ == {"{urn:p}id": "1", "plain": "v"}
        assert [child._name_ for child in tree.children] == ["a", "b", "a"]
        assert isinstance(tree.children, tuple)
        assert tree.children[1].children[0]._name_ == "a"
        assert dict(tree.attributes) == {"{urn:p}id": "1", "plain": "v"}
        with pytest.raises(TypeError):
            tree.attributes["extra"] = "no"  # type: ignore[index]

    def test_default_namespace_expands_element_names(self):
        tree, _ = _from_xml('<root xmlns="urn:d"><child/></root>')

        assert tree._name_ == "{urn:d}root"
        assert tree.children[0]._name_ == "{urn:d}child"

    def test_text_and_tails_keep_ordered_mixed_content(self):
        tree, _ = _from_xml("<r>a<b>1</b> tail <b/>end<c> </c></r>")
        first, second, third = tree.children

        assert tree.text_value() == "a"
        assert tree._value_ == ["a"]
        assert first.tail_value() == " tail "
        assert second.tail_value() == "end"
        assert third.tail_value() is None
        assert list(tree.iter_content()) == ["a", first, " tail ", second, "end", third]
        assert first.text_value() == "1"
        assert third.text_value() == " "

    def test_multiline_text_remains_one_lexical_value(self):
        tree, _ = _from_xml("<r>line one\nline two</r>")

        assert tree.text_value() == "line one\nline two"
        assert tree._value_ == ["line one\nline two"]

    def test_element_only_marker_is_false_for_leading_text(self):
        tree, _ = _from_xml("<r>a<c/></r>")

        assert tree._elementOnly_ is False

    def test_element_only_marker_is_false_for_any_child_tail(self):
        tree, _ = _from_xml("<r><c/> tail <c/>end</r>")

        assert tree._elementOnly_ is False

    def test_element_only_marker_is_true_without_text_or_tails(self):
        tree, _ = _from_xml("<r><c/><c/></r>")
        empty, _ = _from_xml("<r/>")

        assert tree._elementOnly_ is True
        assert empty._elementOnly_ is True

    def test_element_only_marker_tracks_text_and_tail_edits(self):
        tree, _ = _from_xml("<r><c/></r>")
        child = tree.children[0]

        assert tree._elementOnly_ is True
        tree.set_text("")
        assert tree._elementOnly_ is False
        tree.clear_text()
        assert tree._elementOnly_ is True
        child.set_tail("")
        assert tree._elementOnly_ is False
        child.clear_tail()
        assert tree._elementOnly_ is True

    def test_iter_content_omits_absent_text(self):
        tree, _ = _from_xml("<r><c/></r>")
        child = tree.children[0]

        assert list(tree.iter_content()) == [child]

    def test_direct_construction_is_rejected(self):
        with pytest.raises(TypeError, match="from_etree"):
            RelaxNGElement()

    def test_empty_text_is_distinct_from_absent_text(self):
        element = ET.Element("r")
        empty = ET.SubElement(element, "empty")
        empty.text = ""
        ET.SubElement(element, "absent")
        present = ET.SubElement(element, "present")
        present.text = " "

        tree = RelaxNGElement.from_etree(element)

        assert tree.children[0].text_value() == ""
        assert tree.children[0]._value_ == [""]
        assert tree.children[1].text_value() is None
        assert tree.children[1]._value_ is None
        assert serialize_relaxng(tree) == "<r><empty></empty><absent/><present> </present></r>"

    def test_attribute_lexical_distinguishes_empty_and_absent(self):
        tree, _ = _from_xml('<r xmlns:p="urn:p" empty="" p:x="v&amp;&#9;"/>')

        assert tree.attribute_lexical("{urn:p}x") == "v&\t"
        assert tree.attribute_lexical("empty") == ""
        assert tree.attribute_lexical("missing") is None

    def test_namespace_and_base_uri_frames_are_recorded_per_element(self):
        tree, _ = _from_xml(
            '<r xmlns="urn:d" xmlns:p="urn:p" xml:base="https://example.test/base/">'
            '<c xml:base="sub/"><g xmlns:q="urn:q"/></c></r>',
            base_uri="https://example.test/doc.xml",
        )
        child = tree.children[0]
        grandchild = child.children[0]

        assert tree.namespaces == {"": "urn:d", "p": "urn:p"}
        assert child.namespaces == {"": "urn:d", "p": "urn:p"}
        assert grandchild.namespaces == {"": "urn:d", "p": "urn:p", "q": "urn:q"}
        assert tree.base_uri == "https://example.test/base/"
        assert child.base_uri == "https://example.test/base/sub/"
        assert grandchild.base_uri == "https://example.test/base/sub/"
        assert tree.revision.base_uri_for(grandchild) == "https://example.test/base/sub/"
        assert tree.revision.namespaces_for(grandchild) == grandchild.namespaces

    def test_base_uri_handles_absolute_empty_and_missing_xml_base(self):
        tree, _ = _from_xml(
            '<r xml:base=""/>',
            base_uri="https://example.test/doc.xml",
        )
        assert tree.base_uri == "https://example.test/doc.xml"

        tree, _ = _from_xml('<r xml:base="https://other.test/root/"/>')
        assert tree.base_uri == "https://other.test/root/"

        tree, _ = _from_xml("<r/>")
        assert tree.base_uri is None

    def test_source_links_follow_expanded_names_and_sibling_indices(self):
        root_location = IssueLocation("doc.xml", (PathStep("r", 1),))
        tree, _ = _from_xml("<r><a/><a/><b/></r>", source=root_location)
        first, second, third = tree.children

        assert tree.source_location == root_location
        assert first.source_location == IssueLocation(
            "doc.xml", (PathStep("r", 1), PathStep("a", 1))
        )
        assert second.source_location == IssueLocation(
            "doc.xml", (PathStep("r", 1), PathStep("a", 2))
        )
        assert third.source_location == IssueLocation(
            "doc.xml", (PathStep("r", 1), PathStep("b", 1))
        )
        assert tree.revision.source_location_for(second) == second.source_location

    def test_empty_source_path_is_completed_for_the_root(self):
        tree, _ = _from_xml("<r/>", source=IssueLocation("doc.xml", ()))

        assert tree.source_location == IssueLocation("doc.xml", (PathStep("r", 1),))

    def test_source_origin_is_accepted_as_a_source_link(self):
        origin = SourceOrigin("doc.xml", IssueLocation("doc.xml", (PathStep("r", 1),)))
        tree, _ = _from_xml("<r/>", source=origin)
        assert tree.source_location == origin.location

        tree, _ = _from_xml("<r/>", source=SourceOrigin("doc.xml", None))
        assert tree.source_location is None

    def test_absent_source_records_no_location(self):
        tree, _ = _from_xml("<r><c/></r>")

        assert tree.source_location is None
        assert tree.children[0].source_location is None

    def test_invalid_source_type_is_rejected(self):
        element = ET.Element("r")
        with pytest.raises(TypeError):
            RelaxNGElement.from_etree(element, source="doc.xml")  # type: ignore[arg-type]

    def test_invalid_element_type_is_rejected(self):
        with pytest.raises(TypeError):
            RelaxNGElement.from_etree("not an element")  # type: ignore[arg-type]

    def test_to_etree_round_trips_expanded_names_attributes_and_mixed_text(self):
        tree, _ = _from_xml('<r a="1">x<b>y</b>z</r>')

        assert _shape(tree.to_etree()) == _shape(ET.fromstring('<r a="1">x<b>y</b>z</r>'))

    def test_to_etree_preserves_an_existing_root_tail(self):
        element = ET.Element("r")
        element.text = "x"
        element.tail = "outside"
        tree = RelaxNGElement.from_etree(element)

        assert tree.tail_value() == "outside"
        assert tree.to_etree().tail == "outside"
        assert serialize_relaxng(tree) == "<r>x</r>"


class TestSchemaNeutralWriter:
    def test_serialization_round_trips_mixed_characters_without_loss(self):
        element = ET.Element("r", {"a": 'line1\nline2\tend"quote\r'})
        element.text = "text & < > \" '\r\n\t末尾"
        child = ET.SubElement(element, "c", {"p": "v"})
        child.text = "child\rtext"
        child.tail = "tail & <\r"
        tree = RelaxNGElement.from_etree(element)

        output = serialize_relaxng(tree)

        assert _shape(ET.fromstring(output)) == _shape(element)
        assert "xsi" not in output

    def test_namespace_frames_survive_for_prefixed_values(self):
        tree, _ = _from_xml(
            '<r xmlns:p="urn:a" p:q="p:item">p:item<c xmlns:p="urn:b" p:q="p:item">p:item</c></r>'
        )

        output = serialize_relaxng(tree)
        context = NamespaceContext()
        reparsed = parse_with_namespaces(io.StringIO(output), context)
        child = reparsed[0]

        assert context.bindings_for(reparsed)["p"] == "urn:a"
        assert context.bindings_for(child)["p"] == "urn:b"
        assert context.resolve(reparsed, reparsed.get("{urn:a}q")) == "{urn:a}item"
        assert context.resolve(child, child.get("{urn:b}q")) == "{urn:b}item"
        assert context.resolve(child, child.text) == "{urn:b}item"

    def test_no_xsi_attribute_is_added_or_interpreted(self):
        tree, _ = _from_xml(
            '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>'
        )

        output = serialize_relaxng(tree)

        assert output.count("xsi:nil") == 1
        assert "xsi:type" not in output
        assert output.count("xmlns:xsi") == 1
        assert serialize_relaxng(_from_xml("<r><a/></r>")[0]) == "<r><a/></r>"

    def test_default_namespace_is_undeclared_for_a_no_namespace_child(self):
        tree, _ = _from_xml('<root xmlns="urn:d"><child xmlns="">x</child></root>')

        assert serialize_relaxng(tree) == '<root xmlns="urn:d"><child xmlns="">x</child></root>'

    def test_serializing_a_subtree_emits_its_recorded_lexical_context(self):
        tree, _ = _from_xml('<r xmlns:p="urn:p"><c p:a="1">p:x</c></r>')

        output = serialize_relaxng(tree.children[0])
        context = NamespaceContext()
        reparsed = parse_with_namespaces(io.StringIO(output), context)

        assert output.startswith("<c")
        assert 'xmlns:p="urn:p"' in output
        assert context.bindings_for(reparsed)["p"] == "urn:p"
        assert reparsed.get("{urn:p}a") == "1"
        assert context.resolve(reparsed, reparsed.text) == "{urn:p}x"

    def test_generated_prefixes_are_deterministic_and_avoid_collisions(self):
        element = ET.Element("{urn:x}root", {"{urn:a}k": "v"})
        ET.SubElement(element, "plain")
        tree = RelaxNGElement.from_etree(element)

        output = serialize_relaxng(tree)

        assert tree.namespaces is None
        assert output.startswith("<ns0:root")
        assert 'xmlns:ns0="urn:x"' in output
        assert 'xmlns:ns1="urn:a"' in output
        assert 'ns1:k="v"' in output
        assert _shape(ET.fromstring(output)) == _shape(element)

        context = NamespaceContext()
        colliding = ET.Element("{urn:x}root")
        context.record(colliding, {"ns0": "urn:other"})
        colliding_tree = RelaxNGElement.from_etree(colliding, namespaces=context)

        colliding_output = serialize_relaxng(colliding_tree)

        assert colliding_output.startswith("<ns1:root")
        assert 'xmlns:ns0="urn:other"' in colliding_output
        assert 'xmlns:ns1="urn:x"' in colliding_output

    def test_xml_namespace_uses_the_implicit_prefix(self):
        element = ET.Element("root", {f"{{{XML_NS}}}lang": "en"})
        tree = RelaxNGElement.from_etree(element)

        output = serialize_relaxng(tree)

        assert output == '<root xml:lang="en"/>'
        assert "xmlns:xml" not in output

    def test_element_in_the_xml_namespace_uses_the_implicit_prefix(self):
        tree = RelaxNGElement.from_etree(ET.Element(f"{{{XML_NS}}}root"))

        assert serialize_relaxng(tree) == "<xml:root/>"

    def test_recorded_xml_prefix_mapping_is_not_declared(self):
        element = ET.Element("root")
        context = NamespaceContext()
        context.record(element, {"xml": XML_NS})

        output = serialize_relaxng(RelaxNGElement.from_etree(element, namespaces=context))

        assert output == "<root/>"

    def test_an_invalid_xml_prefix_mapping_is_rejected(self):
        element = ET.Element("root")
        context = NamespaceContext()
        context.record(element, {"xml": "urn:not-xml"})

        with pytest.raises(ValueError, match="xml prefix"):
            serialize_relaxng(RelaxNGElement.from_etree(element, namespaces=context))

    def test_empty_element_with_attributes_is_self_closing(self):
        tree, _ = _from_xml('<r a="1"/>')

        assert serialize_relaxng(tree) == '<r a="1"/>'

    def test_explicit_empty_default_binding_adds_no_declaration(self):
        element = ET.Element("root")
        context = NamespaceContext()
        context.record(element, {"": ""})
        tree = RelaxNGElement.from_etree(element, namespaces=context)

        assert serialize_relaxng(tree) == "<root/>"

    def test_a_no_namespace_element_undeclares_an_inherited_default(self):
        root = ET.Element("{urn:d}root")
        ET.SubElement(root, "child")
        context = NamespaceContext()
        frame = {"": "urn:d"}
        context.record(root, frame)
        context.record(root[0], frame)
        tree = RelaxNGElement.from_etree(root, namespaces=context)

        assert serialize_relaxng(tree) == '<root xmlns="urn:d"><child xmlns=""/></root>'

    def test_a_plain_prefixed_name_is_preserved_with_its_frame(self):
        element = ET.Element("p:root")
        context = NamespaceContext()
        context.record(element, {"p": "urn:p"})
        tree = RelaxNGElement.from_etree(element, namespaces=context)

        output = serialize_relaxng(tree)

        assert output == '<p:root xmlns:p="urn:p"/>'
        assert ET.fromstring(output).tag == "{urn:p}root"

    def test_empty_clark_names_serialize_as_no_namespace(self):
        tree = RelaxNGElement.from_etree(ET.Element("{}root", {"{}k": "v"}))

        assert serialize_relaxng(tree) == '<root k="v"/>'

    def test_writer_and_cloner_reject_non_tree_values(self):
        with pytest.raises(TypeError, match="RelaxNGElement"):
            serialize_relaxng(object())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="RelaxNGElement"):
            clone_relaxng(object())  # type: ignore[arg-type]


class TestCloning:
    def test_clone_is_independent_and_keeps_lexical_context(self):
        root_location = IssueLocation("doc.xml", (PathStep("r", 1),))
        tree, _ = _from_xml(
            '<r xmlns:p="urn:p" xml:base="https://example.test/base/"><c p:a="1">x</c><c/>tail</r>',
            base_uri="https://example.test/doc.xml",
            source=root_location,
        )

        clone = clone_relaxng(tree)

        assert clone is not tree
        assert clone.revision is not tree.revision
        assert clone.revision.revision_id != tree.revision.revision_id
        assert clone.revision.root is clone
        assert tree.revision.root is tree
        assert _shape(clone.to_etree()) == _shape(tree.to_etree())
        assert clone.namespaces == tree.namespaces
        assert clone.children[0].base_uri == tree.children[0].base_uri
        assert clone.children[0].source_location == tree.children[0].source_location
        assert not clone.revision.dirty
        assert not tree.revision.dirty

        clone.children[0].set_text("changed")
        clone.children[0].set_attribute("{urn:p}a", "2")

        assert clone.revision.dirty
        assert not tree.revision.dirty
        assert tree.children[0].text_value() == "x"
        assert tree.children[0].attribute_lexical("{urn:p}a") == "1"

        tree.children[1].set_text("original edit")

        assert clone.children[1].text_value() is None

    def test_clone_without_recorded_namespaces_keeps_no_frame(self):
        tree = RelaxNGElement.from_etree(ET.Element("r"))

        clone = clone_relaxng(tree)

        assert clone.namespaces is None
        assert clone.revision.context_for(clone) == NodeContext()

    def test_clone_copies_the_dirty_state(self):
        tree, _ = _from_xml("<r>x</r>")
        tree.set_text("edited")

        clone = clone_relaxng(tree)

        assert tree.revision.dirty
        assert clone.revision.dirty

    def test_deep_trees_are_built_cloned_written_and_converted_iteratively(self):
        depth = 1500
        element = ET.Element("n")
        node = element
        for _ in range(depth):
            node = ET.SubElement(node, "n")
        node.text = "leaf"

        tree = RelaxNGElement.from_etree(element)
        clone = clone_relaxng(tree)
        output = serialize_relaxng(clone)
        rebuilt = clone.to_etree()

        assert output.count("<n>") == depth + 1
        assert output.endswith("leaf</n>" + "</n>" * depth)
        count = 0
        cursor = rebuilt
        while len(cursor):
            count += 1
            cursor = cursor[0]
        assert count == depth


class TestRevisionAndMutation:
    def test_revision_identities_are_unique_and_dirty_starts_clean(self):
        first = LexicalRevision()
        second = LexicalRevision()

        assert isinstance(first.revision_id, int)
        assert first.revision_id != second.revision_id
        assert first.dirty is False
        assert second.dirty is False

        first.mark_dirty()
        first.mark_dirty()

        assert first.dirty is True
        assert second.dirty is False

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda node: node.set_text("changed"),
            lambda node: node.clear_text(),
            lambda node: node.set_attribute("b", "2"),
            lambda node: node.delete_attribute("a"),
            lambda node: node.set_name("renamed"),
            lambda node: node.set_tail("after"),
            lambda node: node.clear_tail(),
            lambda node: node.set_namespace_bindings({"p": "urn:p"}),
            lambda node: node.set_base_uri("https://example.test/"),
        ],
    )
    def test_every_mutation_dirties_the_owning_revision(self, mutation):
        tree, _ = _from_xml('<r a="1"><c/>tail</r>')

        assert tree.revision.dirty is False
        mutation(tree)
        assert tree.revision.dirty is True

    def test_child_mutations_dirty_the_shared_revision(self):
        tree, _ = _from_xml("<r><a/><b/></r>")

        tree.children[0].set_text("x")
        assert tree.revision.dirty

    def test_mutations_update_the_authoritative_stores(self):
        tree, _ = _from_xml('<r a="1">x<c/>tail</r>')
        child = tree.children[0]

        tree.set_text("y")
        tree.set_attribute("b", "2")
        tree.delete_attribute("a")
        tree.set_name("renamed")
        child.set_tail("changed")
        tree.set_namespace_bindings({"p": "urn:p"})
        tree.set_base_uri("https://example.test/")

        assert tree._name_ == "renamed"
        assert tree.text_value() == "y"
        assert tree._value_ == ["y"]
        assert tree._attribs_ == {"b": "2"}
        assert tree.attribute_lexical("a") is None
        assert child.tail_value() == "changed"
        assert tree.namespaces == {"p": "urn:p"}
        assert tree.base_uri == "https://example.test/"
        assert serialize_relaxng(tree) == '<renamed xmlns:p="urn:p" b="2">y<c/>changed</renamed>'
        assert tree.to_etree().text == "y"
        assert tree.to_etree().get("b") == "2"

    def test_empty_and_absent_text_are_editable(self):
        tree, _ = _from_xml("<r/>")

        tree.set_text("")
        assert tree.text_value() == ""
        assert tree._value_ == [""]
        assert serialize_relaxng(tree) == "<r></r>"

        tree.clear_text()
        assert tree.text_value() is None
        assert tree._value_ is None
        assert serialize_relaxng(tree) == "<r/>"

    def test_text_edit_requires_a_string(self):
        tree, _ = _from_xml("<r/>")

        with pytest.raises(TypeError):
            tree.set_text(1)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            tree.set_tail(1)  # type: ignore[arg-type]

        assert tree.text_value() is None
        assert tree.tail_value() is None
        assert not tree.revision.dirty

    def test_name_and_attribute_validation_is_atomic(self):
        tree, _ = _from_xml('<r a="1"/>')

        with pytest.raises(TypeError):
            tree.set_name(1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            tree.set_name("")
        with pytest.raises(TypeError):
            tree.set_attribute(1, "x")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            tree.set_attribute("", "x")
        with pytest.raises(TypeError):
            tree.set_attribute("b", 1)  # type: ignore[arg-type]
        with pytest.raises(KeyError):
            tree.delete_attribute("missing")

        assert tree._name_ == "r"
        assert tree._attribs_ == {"a": "1"}
        assert not tree.revision.dirty

    def test_context_edits_validate_their_inputs(self):
        tree, _ = _from_xml("<r/>")

        with pytest.raises(TypeError):
            tree.set_namespace_bindings("p=urn:p")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            tree.set_base_uri(3)  # type: ignore[arg-type]

        assert tree.namespaces == {}
        assert tree.base_uri is None
        assert not tree.revision.dirty

    def test_node_context_is_immutable_and_copies_its_mapping(self):
        frame = {"p": "urn:p"}
        context = NodeContext(frame, "https://example.test/", None)
        frame["p"] = "urn:changed"

        assert context.namespaces == {"p": "urn:p"}
        assert context.base_uri == "https://example.test/"
        assert context.source is None
        with pytest.raises(TypeError):
            context.namespaces["x"] = "urn:x"  # type: ignore[index]

        empty = NodeContext()

        assert empty.namespaces is None
        assert empty.base_uri is None
        assert empty.source is None


class TestChildGraphMutations:
    def test_failed_child_edits_leave_the_single_graph_unchanged(self):
        tree, _ = _from_xml("<r><a/><b/></r>")
        first, second = tree.children
        before = tree.children
        other, _ = _from_xml("<other/>")

        with pytest.raises(TypeError):
            tree.append_child("child")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            tree.append_child(first)
        with pytest.raises(ValueError):
            tree.append_child(tree)
        with pytest.raises(ValueError):
            tree.insert_child(0, first)
        with pytest.raises(ValueError):
            tree.append_child(other)

        assert tree.children == before
        assert first.parent is tree
        assert second.parent is tree
        assert other.parent is None
        assert not tree.revision.dirty
        assert not other.revision.dirty

        with pytest.raises(IndexError):
            tree.remove_child(9)
        with pytest.raises(ValueError):
            tree.remove_child(other)
        with pytest.raises(IndexError):
            tree.replace_child(9, tree.children[0])
        with pytest.raises(ValueError):
            tree.replace_child(0, second)

        assert tree.children == before
        assert not tree.revision.dirty

    def test_successful_child_edits_keep_parent_links_and_order(self):
        tree, _ = _from_xml("<r><a/><b/></r>")
        first, second = tree.children

        removed = tree.remove_child(0)

        assert removed is first
        assert removed.parent is None
        assert tree.children == (second,)
        assert tree.revision.dirty

        tree.insert_child(0, removed)

        assert tree.children == (first, second)
        assert first.parent is tree

        detached_second = tree.remove_child(second)

        assert detached_second is second
        assert tree.children == (first,)
        assert second.parent is None

        replaced = tree.replace_child(0, second)

        assert replaced is first
        assert tree.children == (second,)
        assert first.parent is None
        assert second.parent is tree

        same = tree.replace_child(0, second)

        assert same is second
        assert tree.children == (second,)

        removed_last = tree.remove_child(-1)

        assert removed_last is second
        assert tree.children == ()
        assert second.parent is None

        tree.append_child(first)
        tree.append_child(second)

        assert tree.children == (first, second)
        assert first.parent is tree
        assert second.parent is tree

        tree.clear_children()

        assert tree.children == ()
        assert first.parent is None
        assert second.parent is None
