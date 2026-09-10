"""Tests for the class-generation architecture.

Covers the Phase 4 rework: classes are created with ``types.new_class``,
descriptor bookkeeping happens in ``SchemaBase.__init_subclass__``, and
``Element``/``Attribute`` descriptors bind to their owning class via
``__set_name__``. Generated classes also provide a helpful
``__getattr__`` for unknown names, and element assignment actually
stores the assigned value (the 0.1 descriptor ``__set__`` discarded it).
"""

import logging

import pytest

from conftest import run_parser
from pyxsd.element_representatives.attribute import Attribute
from pyxsd.element_representatives.element import Element
from pyxsd.schema_base import SchemaBase
from pyxsd.xsd_data_types import String


class TestInitSubclass:
    def test_plain_subclass_gets_empty_bookkeeping(self):
        class Plain(SchemaBase):
            pass

        assert Plain._elementNames_ == []
        assert Plain._attributeNames_ == []

    def test_subclass_collects_descriptors_from_class_body(self):
        parser = run_parser("inventory")
        id_attr = parser.getClasses()["itemType"].__dict__["id"]

        class WithAttr(SchemaBase):
            ident = id_attr

        assert WithAttr._elementNames_ == []
        assert WithAttr._attributeNames_ == ["ident"]

    def test_attribute_descriptors_merge_through_mro(self):
        parser = run_parser("inventory")
        id_attr = parser.getClasses()["itemType"].__dict__["id"]

        class WithAttr(SchemaBase):
            ident = id_attr

        class Derived(WithAttr):
            pass

        assert Derived().descAttributeNames() == ["ident"]


class TestSetName:
    def test_generated_descriptors_know_their_owner(self):
        parser = run_parser("inventory")
        item_cls = parser.getClasses()["itemType"]
        assert item_cls.__dict__["name"].owner is item_cls
        assert item_cls.__dict__["id"].owner is item_cls

    def test_mismatched_binding_logs_warning(self, caplog):
        parser = run_parser("inventory")
        id_attr = parser.getClasses()["itemType"].__dict__["id"]

        with caplog.at_level(logging.WARNING, logger="pyxsd.element_representatives.attribute"):

            class Renamed(SchemaBase):
                other = id_attr

        assert any("other" in r.getMessage() and "id" in r.getMessage() for r in caplog.records)


class TestElementDescriptorSemantics:
    def test_set_stores_single_value(self):
        parser = run_parser("inventory")
        item_cls = parser.getClasses()["itemType"]
        item = item_cls()
        item.name = String("wrench")
        assert item.name == "wrench"
        assert isinstance(item.name, String)

    def test_set_appends_unbounded_values(self):
        parser = run_parser("inventory")
        classes = parser.getClasses()
        inventory_cls = classes["inventory|complexType"]
        item_cls = classes["itemType"]
        inventory = inventory_cls()
        first, second = item_cls(), item_cls()
        inventory.item = first
        inventory.item = second
        assert inventory.item == [first, second]

    def test_set_rejects_wrong_type(self):
        parser = run_parser("inventory")
        item_cls = parser.getClasses()["itemType"]
        item = item_cls()
        with pytest.raises(TypeError, match="quantity"):
            item.quantity = String("not an integer")

    def test_class_level_descriptor_access_returns_descriptor(self):
        parser = run_parser("inventory")
        item_cls = parser.getClasses()["itemType"]
        # Accessing the descriptor through the class (rather than an
        # instance) returns the descriptor itself per the descriptor
        # protocol; the 0.1 code raised AttributeError instead.
        assert isinstance(item_cls.name, Element)
        assert isinstance(item_cls.id, Attribute)


class TestHelpfulGetattr:
    def test_unknown_name_error_mentions_class_and_name(self):
        parser = run_parser("inventory")
        item_cls = parser.getClasses()["itemType"]
        with pytest.raises(AttributeError, match=r"itemType.*no attribute 'widget'"):
            _ = item_cls().widget

    def test_error_lists_declared_elements_and_attributes(self):
        parser = run_parser("inventory")
        item_cls = parser.getClasses()["itemType"]
        with pytest.raises(AttributeError) as excinfo:
            _ = item_cls().widget
        message = str(excinfo.value)
        assert "name" in message
        assert "quantity" in message
        assert "'id'" in message

    def test_internal_names_raise_plain_attribute_error(self):
        class Plain(SchemaBase):
            pass

        with pytest.raises(AttributeError, match="_missing_"):
            _ = Plain()._missing_

    def test_getattr_with_default_still_returns_default(self):
        class Plain(SchemaBase):
            pass

        assert getattr(Plain(), "pyXSD", None) is None
