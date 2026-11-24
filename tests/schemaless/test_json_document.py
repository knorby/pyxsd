"""Behavior of the native JSON document: ownership, pointers, and serialization."""

import io
import json
from collections import UserDict
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext
from types import MappingProxyType

import pytest

from pyxsd.document import Document
from pyxsd.exceptions import ValidationError
from pyxsd.schemaless import JsonDocument, XmlMapping, from_data, from_json
from pyxsd.schemaless.errors import JsonLimitError, JsonParseError
from pyxsd.schemaless.json_values import JsonLimits
from pyxsd.xdm import XdmArray, XdmAtomic, XdmMap


class _Named(io.StringIO):
    """A readable text stream with a caller-supplied ``name`` attribute."""

    def __init__(self, text: str, name: object) -> None:
        super().__init__(text)
        self.name = name


def test_json_pointer_escaping_and_array_indices():
    doc = from_data({"a/b": {"~key": [10, 20]}, "": "root-property"})
    assert doc.value_at("/a~1b/~0key/1") == 20
    assert doc.value_at("/") == "root-property"
    assert from_data([1]).value_at("") == [1]
    with pytest.raises(ValueError, match="leading zero"):
        doc.value_at("/a~1b/~0key/01")


def test_document_data_is_copied_and_native_json_round_trips():
    source = {"x": [Decimal("1.20")]}
    doc = from_data(source)
    source["x"][0] = Decimal("9")
    assert doc.to_json() == '{"x": [1.20]}'
    returned = doc.to_data()
    returned["x"].append(2)
    assert doc.to_data() == {"x": [Decimal("1.20")]}


def test_root_null_and_empty_containers_are_full_documents():
    null = from_json("null")
    assert null.to_data() is None
    assert null.to_json() == "null"
    assert null.value_at("") is None
    assert null.is_valid

    for text in ("{}", "[]", '{"a": {}, "b": []}'):
        doc = from_json(text)
        assert doc.to_json() == text
        assert doc.is_valid


def test_native_documents_are_not_xml_documents():
    doc = from_data({"x": 1})
    assert isinstance(doc, JsonDocument)
    assert not isinstance(doc, Document)


def test_value_at_returns_independent_containers():
    original = {"a": {"b": [1, {"c": 2}]}, "s": "x", "i": 3, "d": Decimal("1.5")}
    doc = from_data(original)

    member = doc.value_at("/a")
    member["b"].append(3)
    member["b"][1]["c"] = 9
    root = doc.value_at("")
    root["a"]["b"].clear()

    assert doc.value_at("/s") == "x"
    assert doc.to_data() == original
    assert doc.value_at("/a") == {"b": [1, {"c": 2}]}


def test_pointer_reads_delegate_rfc_6901_rejections():
    doc = from_data({"a": [1], "s": "x", "~": 4})
    with pytest.raises(ValueError, match="present member"):
        doc.value_at("/missing")
    with pytest.raises(ValueError, match="scalar value"):
        doc.value_at("/s/x")
    with pytest.raises(ValueError, match="append position"):
        doc.value_at("/a/-")
    with pytest.raises(ValueError, match="out-of-range"):
        doc.value_at("/a/1")
    with pytest.raises(ValueError, match="leading zero"):
        doc.value_at("/a/01")
    with pytest.raises(ValueError, match="invalid escape"):
        doc.value_at("/a~2")
    with pytest.raises(TypeError, match="must be a string"):
        doc.value_at(1)  # type: ignore[arg-type]


def test_report_and_validity_follow_document_conventions():
    doc = from_data([1, 2])
    assert doc.report.issues == []
    assert doc.report.has_errors is False
    assert doc.is_valid
    doc.require_valid()

    doc.report.add_error("boom", code="test")
    assert not doc.is_valid
    with pytest.raises(ValidationError, match="the document is not valid") as caught:
        doc.require_valid()
    assert caught.value.report is doc.report


def test_report_and_validity_are_read_only_properties():
    doc = from_data(1)
    with pytest.raises(AttributeError):
        doc.report = doc.report  # type: ignore[misc]
    with pytest.raises(AttributeError):
        doc.is_valid = False  # type: ignore[misc]


def test_json_sources_name_diagnostics_and_the_document(tmp_path):
    path = tmp_path / "input.json"
    path.write_text('{"x": }', encoding="utf-8")
    with path.open("rb") as stream, pytest.raises(JsonParseError) as caught:
        from_json(stream)
    assert str(path) in str(caught.value)
    assert caught.value.line == 1
    assert caught.value.column is not None
    assert caught.value.position is not None

    path.write_text('{"x": 1}', encoding="utf-8")
    with path.open("rb") as stream:
        doc = from_json(stream)
    assert doc.source == str(path)
    assert doc.to_data() == {"x": 1}


def test_anonymous_json_sources_are_named_memory():
    doc = from_json(b'{"x": 1}')
    assert doc.source == "<memory>"
    with pytest.raises(JsonParseError, match=r"^<memory>: "):
        from_json('{"x": }')


def test_non_string_stream_names_fall_back_to_memory():
    assert from_json(_Named("1", 7)).source == "<memory>"
    assert from_json(_Named("1", "named.json")).source == "named.json"


def test_documents_forward_native_limits():
    with pytest.raises(JsonLimitError, match="max_depth"):
        from_json("[[1]]", limits=JsonLimits(max_depth=1))
    with pytest.raises(JsonLimitError, match="max_values"):
        from_data([1, 2], limits=JsonLimits(max_values=2))
    assert from_data([1], limits=JsonLimits(max_values=2)).to_data() == [1]


def test_ambiguous_json_text_is_rejected_by_the_document():
    with pytest.raises(JsonParseError, match=r"^<memory>: .*duplicate key 'x'"):
        from_json('{"x": 1, "x": 2}')


def test_to_json_preserves_native_numbers_and_indentation():
    doc = from_json('{"i": 42, "d": 0.1000000000000000000000001}')
    assert doc.to_data() == {"i": 42, "d": Decimal("0.1000000000000000000000001")}
    assert doc.to_json() == '{"i": 42, "d": 0.1000000000000000000000001}'
    assert doc.to_json(indent=2) == ('{\n  "i": 42,\n  "d": 0.1000000000000000000000001\n}')


def test_direct_construction_copies_host_storage():
    source = {"x": [1]}
    doc = JsonDocument(source)
    source["x"].append(2)
    assert doc.to_data() == {"x": [1]}
    assert doc.source == "<memory>"


@pytest.mark.parametrize("sign", [1, -1])
def test_huge_integer_tokens_round_trip_without_the_host_digit_limit(sign):
    token = ("-" if sign < 0 else "") + "1" + "0" * 4999 + "7"
    doc = from_json(token)
    number = doc.value_at("")
    assert type(number) is int
    assert number == sign * (10**5000 + 7)
    assert doc.to_json() == token


def test_huge_host_integer_keeps_its_value_across_document_views():
    number = 10**5000 + 7
    token = "1" + "0" * 4999 + "7"
    doc = from_data({"n": number})
    assert doc.value_at("/n") == number
    assert doc.to_json() == '{"n": ' + token + "}"
    assert not doc.pin_type("/n", "xs:integer").has_errors
    assert doc.value_at("", typed=True) == {"n": number}
    assert doc.to_xdm()[0].get(XdmAtomic.string("n")) == (XdmAtomic.integer(number),)
    row = doc.profile().at("/n")
    assert row.samples == (token,)
    assert row.candidate("xs:integer").passed == 1
    assert doc.to_xml(XmlMapping.elements("root")).root.children[0].text_value() == token


@pytest.mark.parametrize("trap", [True, False])
def test_number_outside_native_decimal_range_reports_a_parse_error(trap):
    with localcontext() as context, pytest.raises(JsonParseError, match=r"^<memory>: .*number"):
        context.traps[InvalidOperation] = trap
        from_json("1e" + "9" * 24)


class _CustomMapping(Mapping):
    def __init__(self, backing):
        self.backing = backing

    def __iter__(self):
        return iter(self.backing)

    def __len__(self):
        return len(self.backing)

    def __getitem__(self, key):
        return self.backing[key]


@pytest.mark.parametrize("wrap", [MappingProxyType, UserDict, _CustomMapping])
def test_document_copies_general_mappings_and_nested_shared_mappings(wrap):
    backing = {"number": 1}
    child = wrap(backing)
    doc = from_data(wrap({"a": child, "b": [child]}))
    backing["number"] = 2
    data = doc.to_data()
    assert type(data) is dict
    assert type(data["a"]) is type(data["b"][0]) is dict
    assert data == {"a": {"number": 1}, "b": [{"number": 1}]}
    assert data["a"] is not data["b"][0]


def test_mapping_cycles_use_the_documented_error():
    backing = {}
    mapping = MappingProxyType(backing)
    backing["recur"] = UserDict({"next": [mapping]})
    with pytest.raises(JsonParseError, match="cyclic container"):
        from_data(mapping)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (UserDict({"nested": MappingProxyType({1: 2})}), "key must be a string"),
        (_CustomMapping({"unsupported": object()}), "unsupported native JSON value"),
    ],
)
def test_invalid_mapping_members_use_the_documented_error(data, message):
    with pytest.raises(JsonParseError, match=message):
        from_data(data)


@pytest.mark.parametrize(
    ("limits", "message"),
    [(JsonLimits(max_depth=2), "max_depth"), (JsonLimits(max_values=2), "max_values")],
)
def test_mapping_values_count_toward_structural_budgets(limits, message):
    data = MappingProxyType({"nested": _CustomMapping({"number": 1})})
    with pytest.raises(JsonLimitError, match=message):
        from_data(data, limits=limits)


@pytest.fixture(params=["array", "object", "mixed"])
def deep_document(request):
    shapes = tuple(
        request.param == "array" or (request.param == "mixed" and index % 2 == 0)
        for index in range(1100)
    )
    value = 7
    for is_array in reversed(shapes):
        value = [value] if is_array else {"a/~": value}
    return from_data(value), shapes


def test_deep_document_serialization_does_not_recurse(deep_document):
    doc, shapes = deep_document
    opening = "".join("[" if is_array else '{"a/~": ' for is_array in shapes)
    closing = "".join("]" if is_array else "}" for is_array in reversed(shapes))
    assert doc.to_json() == opening + "7" + closing


def test_deep_document_xdm_conversion_does_not_recurse(deep_document):
    doc, shapes = deep_document
    pointer = "".join("/0" if is_array else "/a~1~0" for is_array in shapes)
    assert not doc.pin_type(pointer, "xs:string").has_errors
    items = doc.to_xdm()
    for is_array in shapes:
        [item] = items
        if is_array:
            assert isinstance(item, XdmArray) and item.size == 1
            items = item.get(1)
        else:
            assert isinstance(item, XdmMap) and item.size == 1
            items = item.get(XdmAtomic.string("a/~"))
    assert items == (XdmAtomic.string("7"),)


def test_deep_document_typed_view_does_not_recurse(deep_document):
    doc, shapes = deep_document
    pointer = "".join("/0" if is_array else "/a~1~0" for is_array in shapes)
    assert not doc.pin_type(pointer, "xs:string").has_errors
    # Convert a selected subtree; its original pointer still governs the pin.
    first = "/0" if shapes[0] else "/a~1~0"
    view = doc.value_at(first, typed=True)
    for is_array in shapes[1:]:
        view = view[0] if is_array else view["a/~"]
    assert view == "7"
    assert doc.value_at(pointer) == 7


def test_json_whitespace_indent_stays_utf8_and_parseable():
    indent = " \t\r\n"
    result = from_data([1, 2]).to_json(indent=indent)
    assert result == "[\n" + indent + "1,\n" + indent + "2\n]"
    assert json.loads(result.encode("utf-8")) == [1, 2]


@pytest.mark.parametrize(
    "indent", ["x", "\ud800", "\v"], ids=["letter", "surrogate", "vertical-tab"]
)
def test_invalid_json_indent_is_rejected(indent):
    with pytest.raises(JsonParseError, match=r"indent.*JSON whitespace"):
        from_data([1]).to_json(indent=indent)
