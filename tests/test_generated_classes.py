"""Tests for the classes generated from a schema and their instances."""

import pytest

from conftest import run_parser
from pyxsd.element_representatives.attribute import Attribute
from pyxsd.element_representatives.element import Element
from pyxsd.xsd_data_types import Base64Binary, Double, Integer, PositiveInteger


def find_child(instance, name):
    """Return the first child with the given tag name, or None.

    Absent optional elements can appear as ``None`` placeholders in
    ``_children_``; those are skipped.
    """
    for child in instance._children_:
        if child is not None and child._name_ == name:
            return child
    return None


def child_by_name(instance, name):
    """Return the first child with the given tag name; must exist."""
    found = find_child(instance, name)
    assert found is not None, f"no child named {name!r} in {instance._name_!r}"
    return found


class TestGeneratedSchemaClasses:
    def test_schema_class_exists(self):
        parser = run_parser("inventory")
        classes = parser.getClasses()
        assert "schema" in classes
        assert "itemType" in classes
        assert "colorType" in classes

    def test_generated_class_element_bookkeeping(self):
        parser = run_parser("inventory")
        item_cls = parser.getClasses()["itemType"]
        assert item_cls._elementNames_ == ["name", "quantity", "color", "note"]
        assert item_cls._attributeNames_ == ["id"]
        # Element descriptors are stored on the class under their names.
        assert isinstance(item_cls.__dict__["name"], Element)
        assert isinstance(item_cls.__dict__["id"], Attribute)

    def test_generated_class_carries_schema_metadata(self):
        parser = run_parser("inventory")
        item_cls = parser.getClasses()["itemType"]
        # NB: `item_cls.name` is NOT the class name here -- itemType's
        # schema declares an element literally named "name", and element
        # descriptors are stored on the class under their element names,
        # shadowing the metadata string. Tracked for the architecture
        # rework.
        assert item_cls.__name__ == "itemType"
        assert item_cls.pyXSD is parser


class TestInstanceTree:
    def test_root_instance_structure(self):
        parser = run_parser("inventory")
        root = parser.parseXML()
        assert root._name_ == "inventory"
        assert len(root._children_) == 2
        assert all(child._name_ == "item" for child in root._children_)

    def test_typed_values(self):
        parser = run_parser("inventory")
        root = parser.parseXML()
        first_item = root._children_[0]
        name = child_by_name(first_item, "name")
        quantity = child_by_name(first_item, "quantity")
        assert name == "wrench"
        assert isinstance(name, str)
        assert quantity == 12
        assert isinstance(quantity, Integer)

    def test_attributes_and_optional_elements(self):
        parser = run_parser("inventory")
        root = parser.parseXML()
        first, second = root._children_
        assert first._attribs_["id"] == "a1"
        # `note` is optional: absent from the first item, present in the
        # second. An absent optional child has no entry in _children_.
        assert find_child(first, "note") is None
        assert child_by_name(second, "note") == "handle broken"

    def test_primitive_type_lattice(self):
        parser = run_parser("primitives")
        root = parser.parseXML()
        assert child_by_name(root, "count") == 42
        assert isinstance(child_by_name(root, "count"), Integer)
        assert child_by_name(root, "total") == -7
        assert child_by_name(root, "weight") == 1.5
        assert isinstance(child_by_name(root, "weight"), Double)
        assert child_by_name(root, "rank") == 3
        assert isinstance(child_by_name(root, "rank"), PositiveInteger)
        blob = child_by_name(root, "blob")
        assert blob == "aGVsbG8gd29ybGQ="
        assert isinstance(blob, Base64Binary)

    def test_boolean_attribute_stays_lexical(self):
        """Attribute values keep their lexical form in ``_attribs_``.

        The attribute descriptors can convert values (see the Boolean
        handling in ``Attribute.__set__``), but attributes parsed from
        a document are stored lexically. Typed attribute coercion is
        tracked with the value-semantics work.
        """
        parser = run_parser("primitives")
        root = parser.parseXML()
        assert root._attribs_["active"] == "true"
        assert root._attribs_["serial"] == "S-001"

    def test_multiline_typed_value_keeps_text(self):
        parser = run_parser("primitives")
        root = parser.parseXML()
        notes = child_by_name(root, "notes")
        # Typed primitive values store the stripped text as a single
        # item; the writer re-flows it across lines on output.
        assert notes._value_ == ["first line of notes\nsecond line of notes\nthird line of notes"]

    def test_choice_picks_branch(self):
        parser = run_parser("choice")
        root = parser.parseXML()
        square = child_by_name(root, "square")
        assert child_by_name(square, "side") == 2.5

    def test_nested_and_named_types(self):
        parser = run_parser("nested")
        root = parser.parseXML()
        home = child_by_name(root, "home")
        assert child_by_name(home, "street") == "123 Main St"
        emergency = child_by_name(root, "emergency")
        assert child_by_name(emergency, "phone") == "555-0100"
        assert child_by_name(emergency, "relation") == "neighbor"


@pytest.mark.xfail(
    reason="element-level boolean lexical values ('true'/'false') are not "
    "converted before Boolean construction; tracked for the type lattice work",
    strict=True,
)
def test_boolean_element_lexical_value(tmp_path):
    """Documents a known gap: booleans as element text crash the parse."""
    schema = """<?xml version="1.0"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="flag" type="xs:boolean"/>
    </xs:schema>
    """
    instance = (
        '<flag xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="schema.xsd">true</flag>'
    )
    (tmp_path / "schema.xsd").write_text(schema)
    (tmp_path / "instance.xml").write_text(instance)

    from pyxsd.parser import PyXSD

    parser = PyXSD(
        str(tmp_path / "instance.xml"),
        str(tmp_path / "schema.xsd"),
        xmlFileOutput=False,
        transformOutputName=None,
    )
    root = parser.parseXML()
    assert root == 1
