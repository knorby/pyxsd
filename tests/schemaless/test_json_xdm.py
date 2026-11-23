"""Native JSON values exposed as XDM maps, arrays, atomics, and sequences."""

from decimal import Decimal

import pytest

from pyxsd.schemaless import from_data, from_json
from pyxsd.schemaless.json_xdm import json_to_xdm
from pyxsd.xdm import XdmArray, XdmAtomic, XdmMap

XSD_NS = "http://www.w3.org/2001/XMLSchema"


def test_nested_arrays_and_null_members_stay_nested():
    sequence = from_data({"a": [[1], None], "n": None}).to_xdm()
    root = sequence[0]
    array = root.get(XdmAtomic.string("a"))[0]
    assert isinstance(array, XdmArray)
    assert isinstance(array.get(1)[0], XdmArray)
    assert array.get(2) == ()
    assert root.contains(XdmAtomic.string("n"))
    assert root.get(XdmAtomic.string("n")) == ()


def test_root_json_values_have_xdm_sequence_semantics():
    assert from_json("null").to_xdm() == ()
    assert isinstance(from_json("[1,2]").to_xdm()[0], XdmArray)
    assert from_json('"x"').to_xdm()[0].type_name.endswith("string")


@pytest.mark.parametrize(
    ("value", "type_local"),
    [
        (True, "boolean"),
        (7, "integer"),
        (Decimal("1.20"), "decimal"),
        (1.5, "double"),
        ("text", "string"),
    ],
)
def test_native_scalars_use_their_exact_xdm_types(value, type_local):
    [item] = from_data(value).to_xdm()
    assert isinstance(item, XdmAtomic)
    assert item.type_name == f"{{{XSD_NS}}}{type_local}"


def test_parsed_numbers_keep_their_exact_native_type():
    root = from_json('{"i": 9007199254740993, "d": 0.1}').to_xdm()[0]
    integer = root.get(XdmAtomic.string("i"))[0]
    decimal = root.get(XdmAtomic.string("d"))[0]
    assert integer.type_name == f"{{{XSD_NS}}}integer"
    assert decimal.type_name == f"{{{XSD_NS}}}decimal"
    assert str(decimal.value) == "0.1"


def test_nested_objects_become_nested_maps_in_input_order():
    root = from_data({"outer": {"inner": {"x": 1}}, "z": 2}).to_xdm()[0]
    outer = root.get(XdmAtomic.string("outer"))[0]
    assert isinstance(outer, XdmMap)
    inner = outer.get(XdmAtomic.string("inner"))[0]
    assert inner.get(XdmAtomic.string("x")) == (XdmAtomic.integer(1),)
    assert root.keys() == (XdmAtomic.string("outer"), XdmAtomic.string("z"))


def test_empty_objects_and_arrays_remain_distinct_items():
    root = from_data({"o": {}, "a": []}).to_xdm()[0]
    empty_map = root.get(XdmAtomic.string("o"))[0]
    empty_array = root.get(XdmAtomic.string("a"))[0]
    assert isinstance(empty_map, XdmMap)
    assert empty_map.size == 0
    assert isinstance(empty_array, XdmArray)
    assert empty_array.size == 0


def test_converter_receives_escaped_pointers_and_may_override_a_scalar():
    calls = []

    def converter(pointer, value):
        calls.append((pointer, value))
        if value == 2:
            return XdmAtomic.string("two")
        return None

    root = json_to_xdm({"a/b": [1, None, {"~k": 2}], "n": None}, converter=converter)[0]

    assert calls == [("/a~1b/0", 1), ("/a~1b/2/~0k", 2)]
    array = root.get(XdmAtomic.string("a/b"))[0]
    assert array.get(1) == (XdmAtomic.integer(1),)
    assert array.get(2) == ()
    member_map = array.get(3)[0]
    assert member_map.get(XdmAtomic.string("~k")) == (XdmAtomic.string("two"),)


def test_converter_may_supply_an_empty_sequence():
    root = json_to_xdm({"x": 1}, converter=lambda pointer, value: ())[0]
    assert root.get(XdmAtomic.string("x")) == ()


def test_converter_receives_the_empty_pointer_for_a_root_scalar():
    seen = []

    def converter(pointer, value):
        seen.append((pointer, value))
        return None

    assert json_to_xdm("x", converter=converter) == (XdmAtomic.string("x"),)
    assert seen == [("", "x")]


def test_values_outside_the_native_model_are_rejected():
    with pytest.raises(TypeError, match="not a native JSON scalar"):
        json_to_xdm({1, 2})  # type: ignore[arg-type]
