import io
from xml.etree import ElementTree as ET

import pytest

from pyxsd._lexical import clone_lexical
from pyxsd.nodes import XMLNode
from pyxsd.schemaless import NodeMapping, SchemalessElement, TypePolicy, parse_xml


def test_mixin_identity_precedence_and_document_isolation():
    class BaseView:
        def label(self):
            return "base"

    class LaterView:
        def label(self):
            return "later"

    mapping = NodeMapping()
    mapping.register(BaseView, path="/r/x")
    mapping.register(LaterView, predicate=lambda node: node._name_ == "x")
    doc = parse_xml(io.StringIO("<r><x/><x/></r>"), node_mapping=mapping)
    assert type(doc.root.children[0]) is type(doc.root.children[1])
    assert isinstance(doc.root.children[0], SchemalessElement)
    assert doc.root.children[0].label() == "later"
    assert type(parse_xml(io.StringIO("<r><x/></r>")).root.children[0]) is SchemalessElement


def test_mixin_method_overrides_node_base_method():
    class TextView:
        __slots__ = ()

        def text_value(self):
            return f"view: {super().text_value()}"

    mapping = NodeMapping()
    mapping.register(TextView, path="/r")
    root = parse_xml(b"<r>original</r>", node_mapping=mapping).root

    assert root.text_value() == "view: original"
    assert SchemalessElement.text_value(root) == "original"


def test_indexed_path_selects_one_occurrence():
    class Marker:
        pass

    mapping = NodeMapping()
    mapping.register(Marker, path="/r/x[2]")
    doc = parse_xml(io.StringIO("<r><x/><x/></r>"), node_mapping=mapping)
    assert not isinstance(doc.root.children[0], Marker)
    assert isinstance(doc.root.children[1], Marker)


def test_decorator_selects_clark_names_and_counts_only_same_name_siblings():
    mapping = NodeMapping()

    @mapping.register(path="/{https://example.org/r}r/{urn:p}x[2]")
    class Marker:
        __slots__ = ()

    doc = parse_xml(
        b'<r xmlns="https://example.org/r" xmlns:p="urn:p"><p:x/><x/><p:x/></r>',
        node_mapping=mapping,
    )
    first, other_namespace, second = doc.root.children
    assert not isinstance(first, Marker)
    assert not isinstance(other_namespace, Marker)
    assert isinstance(second, Marker)
    assert type(second).__slots__ == ()
    assert not hasattr(second, "__dict__")


def test_duplicate_mixin_uses_its_latest_matching_registration():
    class First:
        def label(self):
            return "first"

    class Second:
        def label(self):
            return "second"

    mapping = NodeMapping()
    mapping.register(First, path="/r/x")
    mapping.register(Second, path="/r/x")
    mapping.register(First, path="/r/x[2]")
    first, second = parse_xml(b"<r><x/><x/></r>", node_mapping=mapping).root.children
    assert first.label() == "second"
    assert second.label() == "first"
    assert sum(base is First for base in type(second).__mro__) == 1


def test_predicates_see_only_lexical_nodes_before_any_classes_are_applied():
    class Marker:
        pass

    def lexical_marker(node):
        assert type(node) is SchemalessElement
        assert all(type(child) is SchemalessElement for child in node.children)
        if node.parent is not None:
            assert type(node.parent) is SchemalessElement
        return node.attribute_lexical("id") == "001" and node.scalar_value() == "002"

    mapping = NodeMapping()
    mapping.register(Marker, predicate=lexical_marker)
    types = TypePolicy.lexical().with_pin("/r/@id", "xs:integer")
    doc = parse_xml(b'<r id="001">002<x/>tail</r>', node_mapping=mapping, types=types)
    assert isinstance(doc.root, Marker)
    assert isinstance(doc.root, XMLNode)
    assert not isinstance(doc.root.children[0], Marker)
    assert doc.root.children[0].parent is doc.root
    assert doc.root.source_location.path[0].name == "r"
    assert doc.root.revision.root is doc.root
    assert doc.root.attribute_value("id") == 1
    assert doc.to_string() == '<r id="001">002<x/>tail</r>'
    assert doc.to_xdm().root_element.string_value == "002tail"
    assert doc.report_current


@pytest.mark.parametrize(
    "selectors",
    [{}, {"path": "/r", "predicate": lambda node: True}, {"path": 5}, {"predicate": "yes"}],
)
def test_invalid_selector_arguments_raise_at_registration(selectors):
    with pytest.raises(TypeError):
        NodeMapping().register(object, **selectors)


@pytest.mark.parametrize("path", ["/", "r/x", "/r//x", "/r/x[0]", "/r/@x", "/r/p:x"])
def test_invalid_element_paths_raise_at_registration(path):
    with pytest.raises(ValueError):
        NodeMapping().register(object, path=path)


def test_invalid_mixins_raise_at_registration():
    class Stateful:
        __slots__ = ("state",)

    class Unhashable:
        __hash__ = None

    mapping = NodeMapping()
    for invalid in (object(), lambda: None, SchemalessElement, Stateful, int, Unhashable):
        with pytest.raises(TypeError, match="mixin"):
            mapping.register(invalid, path="/r")
    decorate = mapping.register(path="/r")
    with pytest.raises(TypeError, match="mixin"):
        decorate("not a class")


def test_invalid_mapping_is_rejected_before_reading_source():
    source = io.StringIO("<r/>")
    with pytest.raises(TypeError, match="NodeMapping"):
        parse_xml(source, node_mapping=object())
    assert source.tell() == 0


def test_conflicting_base_order_fails_before_projecting_nodes():
    class Parent:
        pass

    class Child(Parent):
        pass

    mapping = NodeMapping()
    mapping.register(Child, path="/r")
    mapping.register(Parent, path="/r")
    with pytest.raises(TypeError, match="mixin"):
        parse_xml(b"<r/>", node_mapping=mapping)


def test_mapping_snapshots_and_class_cache_are_local_to_the_rule_set():
    class Marker:
        pass

    class Later:
        pass

    mapping = NodeMapping()
    mapping.register(Marker, path="/r")
    doc = parse_xml(ET.fromstring("<r/>"), node_mapping=mapping)
    assert isinstance(doc.root, Marker)
    assert type(parse_xml(b"<r/>", node_mapping=mapping).root) is type(doc.root)
    separate = NodeMapping()
    separate.register(Marker, path="/r")
    assert type(parse_xml(b"<r/>", node_mapping=separate).root) is not type(doc.root)
    mapping.register(Later, path="/r")
    assert isinstance(parse_xml(b"<r/>", node_mapping=mapping).root, Later)
    assert not isinstance(doc.root, Later)
    assert type(doc.revalidate().root) is type(doc.root)
    assert type(parse_xml(b"<r/>", node_mapping=NodeMapping()).root) is SchemalessElement


def test_clone_and_revalidation_retain_mapping():
    class Marker:
        def lexical_number(self):
            return self.text_value()

    mapping = NodeMapping()
    mapping.register(Marker, path="/r/x")
    types = TypePolicy.lexical().with_pin("/r/x", "xs:integer")
    doc = parse_xml(b"<r><x>001</x></r>", node_mapping=mapping, types=types)
    clone = doc.transform(clone_lexical)
    (child,) = doc.root.children
    (cloned_child,) = clone.root.children
    assert cloned_child is not child
    assert type(cloned_child) is type(child)
    assert cloned_child.lexical_number() == "001"
    assert cloned_child.parent is clone.root
    refreshed = clone.revalidate().root.children[0]
    assert type(refreshed) is type(child)
    # The shared lexical clone deliberately drops the source typed snapshot.
    assert refreshed.scalar_value() == "001"
    assert doc.revalidate().root.children[0].scalar_value() == 1


def test_revalidation_selects_the_current_occurrence_after_reordering():
    class Marker:
        pass

    mapping = NodeMapping()
    mapping.register(Marker, path="/r/x[2]")
    doc = parse_xml(b"<r><x>one</x><x>two</x></r>", node_mapping=mapping)
    first, second = doc.root.children

    def reorder(root):
        root.insert_child(0, root.remove_child(1))
        return root

    wrapped = doc.transform(reorder)
    assert wrapped.root.children == (second, first)
    assert not wrapped.report_current
    refreshed_first, refreshed_second = wrapped.revalidate().root.children
    assert refreshed_first.text_value() == "two"
    assert not isinstance(refreshed_first, Marker)
    assert refreshed_second.text_value() == "one"
    assert type(refreshed_second) is type(second)


def test_revalidation_uses_subtree_root_not_historical_path():
    class InParent:
        pass

    class AtRoot:
        __slots__ = ()

    mapping = NodeMapping()
    mapping.register(InParent, path="/r/x")
    mapping.register(AtRoot, path="/x")
    doc = parse_xml(b"<r><x/></r>", node_mapping=mapping)
    child = doc.root.children[0]
    attached = doc.transform(lambda root: root.children[0])
    assert attached.root is child
    assert isinstance(attached.root, InParent)
    detached = doc.transform(lambda root: root.remove_child(0))
    assert detached.root is child and detached.root.parent is None
    for wrapper in (attached, detached):
        rebound = wrapper.revalidate()
        assert isinstance(rebound.root, AtRoot)
        assert not isinstance(rebound.root, InParent)
        assert rebound.root.source_location.path[0].name == "x"
