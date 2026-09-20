"""Tests for the classes generated from a schema and their instances."""

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
        doc = run_parser("inventory")
        classes = doc.schema.classes
        assert "schema" in classes
        assert "itemType" in classes
        assert "colorType" in classes

    def test_generated_class_element_bookkeeping(self):
        doc = run_parser("inventory")
        item_cls = doc.schema.classes["itemType"]
        assert item_cls._elementNames_ == ["name", "quantity", "color", "note"]
        assert item_cls._attributeNames_ == ["id"]
        # Element descriptors are stored on the class under their names.
        assert isinstance(item_cls.__dict__["name"], Element)
        assert isinstance(item_cls.__dict__["id"], Attribute)

    def test_generated_class_carries_schema_metadata(self):
        doc = run_parser("inventory")
        item_cls = doc.schema.classes["itemType"]
        # NB: `item_cls.name` is NOT the class name here -- itemType's
        # schema declares an element literally named "name", and element
        # descriptors are stored on the class under their element names,
        # shadowing the metadata string. Tracked for the architecture
        # rework.
        assert item_cls.__name__ == "itemType"
        assert item_cls.schema is doc.schema


class TestInstanceTree:
    def test_root_instance_structure(self):
        doc = run_parser("inventory")
        root = doc.root
        assert root._name_ == "inventory"
        assert len(root._children_) == 2
        assert all(child._name_ == "item" for child in root._children_)

    def test_typed_values(self):
        doc = run_parser("inventory")
        root = doc.root
        first_item = root._children_[0]
        name = child_by_name(first_item, "name")
        quantity = child_by_name(first_item, "quantity")
        assert name == "wrench"
        assert isinstance(name, str)
        assert quantity == 12
        assert isinstance(quantity, Integer)

    def test_attributes_and_optional_elements(self):
        doc = run_parser("inventory")
        root = doc.root
        first, second = root._children_
        assert first._attribs_["id"] == "a1"
        # `note` is optional: absent from the first item, present in the
        # second. An absent optional child has no entry in _children_.
        assert find_child(first, "note") is None
        assert child_by_name(second, "note") == "handle broken"

    def test_primitive_type_lattice(self):
        doc = run_parser("primitives")
        root = doc.root
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
        doc = run_parser("primitives")
        root = doc.root
        assert root._attribs_["active"] == "true"
        assert root._attribs_["serial"] == "S-001"

    def test_multiline_typed_value_keeps_text(self):
        doc = run_parser("primitives")
        root = doc.root
        notes = child_by_name(root, "notes")
        # Typed primitive values store the stripped text as a single
        # item; the writer re-flows it across lines on output.
        assert notes._value_ == ["first line of notes\nsecond line of notes\nthird line of notes"]

    def test_choice_picks_branch(self):
        doc = run_parser("choice")
        root = doc.root
        square = child_by_name(root, "square")
        assert child_by_name(square, "side") == 2.5

    def test_nested_and_named_types(self):
        doc = run_parser("nested")
        root = doc.root
        home = child_by_name(root, "home")
        assert child_by_name(home, "street") == "123 Main St"
        emergency = child_by_name(root, "emergency")
        assert child_by_name(emergency, "phone") == "555-0100"
        assert child_by_name(emergency, "relation") == "neighbor"


def test_boolean_element_lexical_value(tmp_path):
    """Element-level booleans accept XSD lexical forms since the type
    lattice work (0.1 crashed on 'true'/'false')."""
    schema = """<?xml version="1.0"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="settings">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="flag" type="xs:boolean"/>
          </xs:sequence>
        </xs:complexType>
      </xs:element>
    </xs:schema>
    """
    instance = (
        '<settings xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="schema.xsd"><flag>true</flag></settings>'
    )
    (tmp_path / "schema.xsd").write_text(schema)
    (tmp_path / "instance.xml").write_text(instance)

    from pyxsd.schema import Schema

    schema = Schema.compile(str(tmp_path / "schema.xsd"))
    doc = schema.parse(str(tmp_path / "instance.xml"))
    root = doc.root
    flag = child_by_name(root, "flag")
    assert flag == 1
    assert str(flag) == "true"
    assert repr(flag) == "True"
