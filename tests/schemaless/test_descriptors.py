"""Lightweight descriptors over the ordinary typed lexical accessors."""

import io

import pytest

from pyxsd._lexical import clone_lexical
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.schemaless import (
    NodeMapping,
    ScalarTypeError,
    SchemalessElement,
    TypePolicy,
    attribute,
    child,
    children,
    parse_xml,
    text,
)


class OrderView:
    id = attribute("id", type="xs:integer")
    note = text(default="")
    first_line = child("line")
    lines = children("line")


def order_mapping():
    mapping = NodeMapping()
    mapping.register(OrderView, path="/order")
    return mapping


def test_descriptors_read_and_write_through_node_policy():
    doc = parse_xml(
        io.StringIO('<order id="1"><line/>text<line/></order>'), node_mapping=order_mapping()
    )
    order = doc.root
    assert order.id == 1 and order.first_line is order.lines[0]
    assert [n._name_ for n in order.lines] == ["line", "line"]
    order.id = 2
    assert order.attribute_lexical("id") == "2"
    assert doc.report_current is False


def test_child_and_mixed_content_ambiguity_are_not_hidden():
    class ManyView:
        value = child("x")

    mapping = NodeMapping()
    mapping.register(ManyView, path="/r")
    doc = parse_xml(io.StringIO("<r><x/><x/></r>"), node_mapping=mapping)
    with pytest.raises(ValueError, match="multiple"):
        doc.root.value  # noqa: B018

    class MixedView:
        body = text()

    mixed_mapping = NodeMapping()
    mixed_mapping.register(MixedView, path="/r")
    mixed = parse_xml(io.StringIO("<r>head<x/>tail</r>"), node_mapping=mixed_mapping)
    assert mixed.root.body == "head"
    assert list(mixed.root.iter_content()) == ["head", mixed.root.children[0], "tail"]


def test_singular_child_is_still_ambiguous_without_a_list_view_for_that_name():
    class MismatchedView:
        value = child("x")
        others = children("y")

    mapping = NodeMapping()
    mapping.register(MismatchedView, path="/r")
    doc = parse_xml(b"<r><x/><x/><y/></r>", node_mapping=mapping)
    with pytest.raises(ValueError, match="multiple"):
        doc.root.value  # noqa: B018
    assert doc.root.others[0]._name_ == "y"


def test_paired_child_leniency_is_local_to_the_declaring_class():
    class SingleView:
        value = child("x")

    class ListView:
        all_x = children("x")

    mapping = NodeMapping()
    mapping.register(SingleView, path="/r")
    mapping.register(ListView, path="/r")
    doc = parse_xml(b"<r><x/><x/></r>", node_mapping=mapping)
    with pytest.raises(ValueError, match="multiple"):
        doc.root.value  # noqa: B018
    assert len(doc.root.all_x) == 2

    class PairedView:
        value = child("x")
        all_x = children("x")

    paired = NodeMapping()
    paired.register(PairedView, path="/r")
    both = parse_xml(b"<r><x/><x/></r>", node_mapping=paired)
    assert both.root.value is both.root.all_x[0]


def test_cross_class_or_shadowed_list_descriptors_do_not_suppress_ambiguity():
    class Base:
        value = child("x")
        all_x = children("x")

    class Shadowed(Base):
        all_x = text()

    shadowed = NodeMapping()
    shadowed.register(Shadowed, path="/r")
    doc = parse_xml(b"<r>head<x/><x/></r>", node_mapping=shadowed)
    with pytest.raises(ValueError, match="multiple"):
        doc.root.value  # noqa: B018
    assert doc.root.all_x == "head"

    class ChildOnly:
        value = child("x")

    class ListOnly(ChildOnly):
        all_x = children("x")

    crossed = NodeMapping()
    crossed.register(ListOnly, path="/r")
    crossed_doc = parse_xml(b"<r><x/><x/></r>", node_mapping=crossed)
    with pytest.raises(ValueError, match="multiple"):
        crossed_doc.root.value  # noqa: B018

    class ShadowedByMethod(Base):
        def all_x(self):
            return self.children

    method_mapping = NodeMapping()
    method_mapping.register(ShadowedByMethod, path="/r")
    method_doc = parse_xml(b"<r><x/><x/></r>", node_mapping=method_mapping)
    with pytest.raises(ValueError, match="multiple"):
        method_doc.root.value  # noqa: B018
    assert len(method_doc.root.all_x()) == 2


def test_cross_mixin_non_descriptor_shadow_keeps_child_strict():
    class Paired:
        value = child("x")
        all_x = children("x")

    class Shadow:
        all_x = 5

    mapping = NodeMapping()
    mapping.register(Paired, path="/r")
    mapping.register(Shadow, path="/r")
    doc = parse_xml(b"<r><x/><x/></r>", node_mapping=mapping)
    assert doc.root.all_x == 5
    with pytest.raises(ValueError, match="multiple"):
        doc.root.value  # noqa: B018


def test_cross_mixin_method_shadow_keeps_child_strict():
    class Paired:
        value = child("x")
        all_x = children("x")

    class Shadow:
        def all_x(self):
            return self.children

    mapping = NodeMapping()
    mapping.register(Paired, path="/r")
    mapping.register(Shadow, path="/r")
    doc = parse_xml(b"<r><x/><x/></r>", node_mapping=mapping)
    assert len(doc.root.all_x()) == 2
    with pytest.raises(ValueError, match="multiple"):
        doc.root.value  # noqa: B018


def test_sibling_inheritance_shadow_keeps_child_strict():
    class Paired:
        value = child("x")
        all_x = children("x")

    class Left(Paired):
        pass

    class Shadow(Paired):
        all_x = 5

    mapping = NodeMapping()
    mapping.register(Left, path="/r")
    mapping.register(Shadow, path="/r")
    doc = parse_xml(b"<r><x/><x/></r>", node_mapping=mapping)
    assert doc.root.all_x == 5
    with pytest.raises(ValueError, match="multiple"):
        doc.root.value  # noqa: B018

    class Replaced(Paired):
        all_x = attribute("other")

    replaced = NodeMapping()
    replaced.register(Left, path="/r")
    replaced.register(Replaced, path="/r")
    replaced_doc = parse_xml(b'<r other="9"><x/><x/></r>', node_mapping=replaced)
    assert replaced_doc.root.all_x == "9"
    with pytest.raises(ValueError, match="multiple"):
        replaced_doc.root.value  # noqa: B018


def test_missing_attribute_text_and_child_defaults():
    class DefaultsView:
        id = attribute("id", type="xs:integer")
        note = text(default="")
        optional = text()
        count = text(default=0)
        first_line = child("line")
        lines = children("line")

    mapping = NodeMapping()
    mapping.register(DefaultsView, path="/order")
    empty = parse_xml(b"<order/>", node_mapping=mapping).root
    assert empty.id is None
    assert empty.note == ""
    assert empty.optional is None
    assert empty.count == 0
    assert empty.first_line is None
    assert empty.lines == ()

    with_lines = parse_xml(b"<order><line/></order>", node_mapping=mapping).root
    assert with_lines.first_line is with_lines.lines[0]
    assert with_lines.note == ""


def test_descriptor_writes_preserve_unrelated_character_data():
    doc = parse_xml(
        io.StringIO('<order id="1">note<line/>tail<line/>after</order>'),
        node_mapping=order_mapping(),
    )
    order = doc.root
    order.note = "changed"
    order.id = 2
    first, second = order.children
    assert doc.to_string() == '<order id="2">changed<line/>tail<line/>after</order>'
    assert list(order.iter_content()) == ["changed", first, "tail", second, "after"]
    assert first.tail_value() == "tail"
    assert second.tail_value() == "after"

    bare = parse_xml(b'<order id="1"><line/>tail</order>', node_mapping=order_mapping())
    assert bare.root.note == ""
    bare.root.note = "added"
    assert bare.to_string() == '<order id="1">added<line/>tail</order>'
    assert bare.root.children[0].tail_value() == "tail"


def test_child_and_children_follow_current_document_order():
    doc = parse_xml(b"<order><line/><line/></order>", node_mapping=order_mapping())
    order = doc.root
    first, second = order.lines
    assert order.lines == (first, second)
    assert order.lines is not order.lines
    assert order.first_line is first
    order.remove_child(first)
    order.append_child(first)
    assert order.children == (second, first)
    assert order.lines == (second, first)
    assert order.first_line is second

    # Every child view selects one exact expanded name.
    extends = parse_xml(b"<order><line/><lines/></order>", node_mapping=order_mapping())
    assert len(extends.root.lines) == 1
    assert extends.root.first_line._name_ == "line"


def test_descriptor_type_pins_are_effective_and_replaceable():
    doc = parse_xml(b'<order id="1"/>', node_mapping=order_mapping())
    assert doc.root.id == 1
    assert doc.root.attribute_value("id") == 1
    report = doc.pin_type("/order/@id", "xs:string")
    assert not report.has_errors
    assert doc.root.id == "1"
    assert doc.root.attribute_value("id") == "1"
    assert doc.root.attribute_lexical("id") == "1"
    assert doc.report_current is True
    doc.root.id = "002"
    assert doc.root.attribute_lexical("id") == "002"


def test_construction_policy_merges_beneath_descriptor_pins():
    lexical = parse_xml(
        b'<order id="1"/>', node_mapping=order_mapping(), types=TypePolicy.lexical()
    )
    assert lexical.root.id == 1
    replaced = parse_xml(
        b'<order id="1"/>',
        node_mapping=order_mapping(),
        types=TypePolicy.lexical().with_pin("/order/@id", "xs:string"),
    )
    assert replaced.root.id == "1"


def test_text_descriptor_pin_uses_the_registered_occurrence_path():
    class ItemView:
        value = text(type="xs:integer")

    mapping = NodeMapping()
    mapping.register(ItemView, path="/r/x[2]")
    doc = parse_xml(b"<r><x>01</x><x>02</x></r>", node_mapping=mapping)
    first, second = doc.root.children
    assert isinstance(first, SchemalessElement)
    assert not isinstance(first, ItemView)
    assert second.value == 2
    second.value = 3
    assert second.text_value() == "3"
    assert doc.to_string() == "<r><x>01</x><x>3</x></r>"


def test_text_descriptor_pin_can_be_replaced_after_construction():
    class View:
        value = text(type="xs:integer", default=0)

    mapping = NodeMapping()
    mapping.register(View, path="/r")
    empty = parse_xml(b"<r/>", node_mapping=mapping)
    assert empty.root.value == 0
    doc = parse_xml(b"<r>12</r>", node_mapping=mapping)
    assert doc.root.value == 12
    assert not doc.pin_type("/r", "xs:string").has_errors
    assert doc.root.value == "12"
    assert doc.root.text_value() == "12"


def test_namespaced_attribute_descriptor_uses_the_expanded_name():
    class NamespacedView:
        ident = attribute("{urn:test}id", type="xs:integer")

    mapping = NodeMapping()
    mapping.register(NamespacedView, path="/r")
    doc = parse_xml(b'<r xmlns:p="urn:test" p:id="7"/>', node_mapping=mapping)
    assert doc.root.ident == 7
    doc.root.ident = 8
    assert doc.root.attribute_lexical("{urn:test}id") == "8"


def test_descriptor_write_uses_the_effective_pin_for_an_untyped_field():
    class View:
        id = attribute("id")

    mapping = NodeMapping()
    mapping.register(View, path="/r")
    types = TypePolicy.lexical().with_pin("/r/@id", "xs:integer")
    doc = parse_xml(b'<r id="1"/>', node_mapping=mapping, types=types)
    assert doc.root.id == 1
    doc.root.id = 2
    assert doc.root.attribute_lexical("id") == "2"
    with pytest.raises(TypeError):
        doc.root.id = "not a number"
    assert doc.root.attribute_lexical("id") == "2"


def test_invalid_descriptor_writes_do_not_change_the_tree():
    class View:
        note = text()
        first = child("x")
        all_x = children("x")

    assert View.note is View.note
    mapping = NodeMapping()
    mapping.register(View, path="/r")
    doc = parse_xml(b"<r>text<x/>tail</r>", node_mapping=mapping)
    with pytest.raises(TypeError):
        doc.root.note = 3
    assert doc.root.text_value() == "text"
    assert doc.report_current is True
    with pytest.raises(AttributeError):
        doc.root.first = doc.root.children[0]
    with pytest.raises(AttributeError):
        doc.root.all_x = ()
    assert doc.root.first is doc.root.children[0]


def test_descriptor_writes_on_a_generic_clone_stay_lexical():
    doc = parse_xml(b"<order>note</order>", node_mapping=order_mapping())
    clone = doc.transform(clone_lexical)
    clone.root.note = "fresh"
    assert clone.root.text_value() == "fresh"
    assert clone.root.revision.dirty


def test_conflicting_same_name_descriptors_are_mapping_errors():
    class First:
        value = attribute("a")

    class Second:
        value = attribute("b")

    mapping = NodeMapping()
    mapping.register(First, path="/r")
    mapping.register(Second, path="/r")
    with pytest.raises(TypeError, match="descriptor"):
        parse_xml(b"<r/>", node_mapping=mapping)


def test_shared_and_overriding_descriptor_declarations_combine():
    class Shared:
        id = attribute("id")

    class Left(Shared):
        pass

    class Right(Shared):
        pass

    shared = NodeMapping()
    shared.register(Left, path="/r")
    shared.register(Right, path="/r")
    assert parse_xml(b'<r id="1"/>', node_mapping=shared).root.id == "1"

    class Base:
        id = attribute("id")

    class Derived(Base):
        id = attribute("id", type="xs:integer")

    derived = NodeMapping()
    derived.register(Base, path="/r")
    derived.register(Derived, path="/r")
    assert parse_xml(b'<r id="1"/>', node_mapping=derived).root.id == 1


def test_typed_descriptors_require_a_path_selector():
    class Typed:
        id = attribute("id", type="xs:integer")

    class Untyped:
        note = text()

    with pytest.raises(TypeError, match="path"):
        NodeMapping().register(Typed, predicate=lambda node: True)

    mapping = NodeMapping()
    mapping.register(Untyped, predicate=lambda node: True)
    assert parse_xml(b"<r>note</r>", node_mapping=mapping).root.note == "note"


def test_typed_descriptor_pin_path_must_be_a_scalar_path():
    class BadView:
        value = attribute("bad/name", type="xs:integer")

    with pytest.raises(ValueError, match="path"):
        NodeMapping().register(BadView, path="/r")


def test_untyped_descriptors_follow_inferred_policy_for_reads_and_writes():
    class View:
        id = attribute("id")
        body = text()

    mapping = NodeMapping()
    mapping.register(View, path="/r")
    lexical = parse_xml(b'<r id="012">12</r>', node_mapping=mapping, types=TypePolicy.infer())
    assert lexical.root.id == "012"
    assert lexical.root.body == 12
    lexical.root.body = 13
    assert lexical.root.text_value() == "13"
    lexical.root.id = "007"
    assert lexical.root.attribute_lexical("id") == "007"

    numeric = parse_xml(b'<r id="1">12</r>', node_mapping=mapping, types=TypePolicy.infer())
    assert numeric.root.id == 1
    assert numeric.root.body == 12
    numeric.root.id = 2
    numeric.root.body = 13
    assert numeric.to_string() == '<r id="2">13</r>'


def test_descriptor_pin_survives_an_overridden_parent_property():
    class View:
        @property
        def parent(self):
            return None

        value = text(type="xs:integer")

    mapping = NodeMapping()
    mapping.register(View, path="/r/x")
    doc = parse_xml(b"<r><x>1</x></r>", node_mapping=mapping)
    child = doc.root.children[0]
    assert child.parent is None
    assert child.value == 1
    child.value = 2
    assert child.text_value() == "2"


def test_failing_descriptor_pin_reports_the_exact_occurrence():
    class View:
        value = text(type="xs:integer")

    mapping = NodeMapping()
    mapping.register(View, path="/r/x")
    doc = parse_xml(b"<r><x>bad</x></r>", node_mapping=mapping)
    child = doc.root.children[0]
    with pytest.raises(ScalarTypeError) as failure:
        child.value  # noqa: B018
    location = IssueLocation(doc.root.source_location.source, (PathStep("r", 1), PathStep("x", 1)))
    assert failure.value.location == location
    report = doc.test_type("/r/x", "xs:integer")
    assert report.issues[0].location == location
    assert report.issues[0].location_precision == "exact"
    assert child.text_value() == "bad"


def test_sibling_subclass_same_name_descriptors_conflict():
    class Shared:
        pass

    class Left(Shared):
        value = attribute("a")

    class Right(Shared):
        value = attribute("b")

    mapping = NodeMapping()
    mapping.register(Left, path="/r")
    mapping.register(Right, path="/r")
    with pytest.raises(TypeError, match="descriptor"):
        parse_xml(b'<r a="1" b="2"/>', node_mapping=mapping)


def test_identical_configs_on_unrelated_mixins_are_still_conflicts():
    class First:
        value = attribute("a")

    class Second:
        value = attribute("a")

    mapping = NodeMapping()
    mapping.register(First, path="/r")
    mapping.register(Second, path="/r")
    with pytest.raises(TypeError, match="descriptor"):
        parse_xml(b'<r a="1"/>', node_mapping=mapping)


def test_reverse_base_registration_order_fails_with_a_clear_error():
    class Base:
        value = attribute("value", type="xs:integer")

    class Derived(Base):
        value = attribute("value", type="xs:string")

    mapping = NodeMapping()
    mapping.register(Derived, path="/r")
    mapping.register(Base, path="/r")
    with pytest.raises(TypeError, match="register a base class before its subclasses") as failure:
        parse_xml(b'<r value="1"/>', node_mapping=mapping)
    assert "Cannot create a consistent method resolution order" not in str(failure.value)


def test_sibling_inherited_override_combines_in_generated_mro_order():
    class Shared:
        value = attribute("value")

    class Left(Shared):
        value = attribute("value", type="xs:integer")

    class Right(Shared):
        pass

    mapping = NodeMapping()
    mapping.register(Left, path="/r")
    mapping.register(Right, path="/r")
    doc = parse_xml(b'<r value="1"/>', node_mapping=mapping)
    assert doc.root.value == 1


def test_writing_an_absent_attribute_installs_the_typed_lexical_value():
    doc = parse_xml(b"<order/>", node_mapping=order_mapping())
    assert doc.root.id is None
    doc.root.id = 5
    assert doc.root.attribute_lexical("id") == "5"
    assert doc.root.id == 5


def test_detached_subtree_descriptors_are_lexical():
    class View:
        value = text(type="xs:integer")

    mapping = NodeMapping()
    mapping.register(View, path="/r/x")
    doc = parse_xml(b"<r><x>1</x></r>", node_mapping=mapping)
    detached = doc.root.remove_child(0)
    assert detached.value == "1"
    with pytest.raises(TypeError):
        detached.value = 2
    detached.value = "2"
    assert detached.text_value() == "2"


def test_generic_clone_descriptors_follow_the_clones_own_policy():
    doc = parse_xml(
        b'<order id="001">12</order>', node_mapping=order_mapping(), types=TypePolicy.infer()
    )
    clone = doc.transform(clone_lexical)
    assert (doc.root.id, doc.root.note) == (1, 12)
    assert (clone.root.id, clone.root.note) == ("001", "12")

    clone.root.id = "002"
    clone.root.note = "fresh"
    assert (clone.root.attribute_lexical("id"), clone.root.text_value()) == ("002", "fresh")
    assert (clone.root.id, clone.root.note) == ("002", "fresh")

    assert not clone.pin_type("/order/@id", "xs:integer").has_errors
    assert clone.root.id == 2
    clone.root.id = 3
    assert clone.to_string() == '<order id="3">fresh</order>'
    assert doc.to_string() == '<order id="001">12</order>'


def test_descriptor_pins_are_isolated_per_document():
    mapping = order_mapping()
    first = parse_xml(b'<order id="1"/>', node_mapping=mapping)
    second = parse_xml(b'<order id="1"/>', node_mapping=mapping)
    assert type(first.root) is type(second.root)
    first.pin_type("/order/@id", "xs:string")
    assert first.root.id == "1"
    assert second.root.id == 1
    second.root.id = 2
    assert second.root.attribute_lexical("id") == "2"
