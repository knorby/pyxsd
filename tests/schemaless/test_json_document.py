"""Behavior of the native JSON document: ownership, pointers, and serialization."""

import io
from decimal import Decimal

import pytest

from pyxsd.document import Document
from pyxsd.exceptions import ValidationError
from pyxsd.schemaless import JsonDocument, from_data, from_json
from pyxsd.schemaless.errors import JsonLimitError, JsonParseError
from pyxsd.schemaless.json_values import JsonLimits


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
