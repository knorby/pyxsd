"""Behavior of strict native JSON values, limits, and RFC 6901 lookup."""

import io
import json
from decimal import Decimal

import pytest

from pyxsd.exceptions import PyXSDError
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.schemaless import json_values as json_values_module
from pyxsd.schemaless.errors import JsonLimitError, JsonParseError
from pyxsd.schemaless.json_values import (
    JsonLimits,
    copy_native_data,
    decode_json,
    encode_json,
    resolve_pointer,
)


def _non_ascii_json() -> str:
    """A two-character JSON string whose UTF-8 encoding is four bytes."""
    return '"' + chr(0xE9) + '"'


def test_json_numbers_never_pass_through_float():
    assert decode_json('{"i": 42, "d": 0.1000000000000000000000001}') == {
        "i": 42,
        "d": Decimal("0.1000000000000000000000001"),
    }
    assert encode_json(decode_json("0.1000000000000000000000001")) == (
        "0.1000000000000000000000001"
    )


@pytest.mark.parametrize("source", ['{"x":1,"x":2}', "NaN", "Infinity", "01"])
def test_ambiguous_or_nonstandard_json_is_rejected(source):
    with pytest.raises(JsonParseError):
        decode_json(source)


@pytest.mark.parametrize("source", ["-Infinity", "[1,]", '{"a": 1} trailing', "", "    "])
def test_other_malformed_json_is_rejected(source):
    with pytest.raises(JsonParseError):
        decode_json(source)


def test_decoded_numbers_use_int_and_decimal_never_float():
    decoded = decode_json("[42, -7, 0, 1.25, 1E+3, 1e-3]")
    assert [type(value) for value in decoded] == [int, int, int, Decimal, Decimal, Decimal]
    assert decoded[4] == Decimal(1000)
    assert decoded[5] == Decimal("0.001")


def test_bool_and_int_are_preserved_distinctly():
    decoded = decode_json("[true, false, 1, 0]")
    assert decoded == [True, False, 1, 0]
    assert [type(value) for value in decoded] == [bool, bool, int, int]

    copied = copy_native_data([True, 1])
    assert [type(value) for value in copied] == [bool, int]
    assert encode_json([True, False, 1]) == "[true, false, 1]"


def test_tuples_are_normalized_to_lists_and_containers_are_copied():
    copied = copy_native_data({"a": (1, (2, 3))})
    assert copied == {"a": [1, [2, 3]]}
    assert type(copied) is dict
    assert type(copied["a"]) is list
    assert type(copied["a"][1]) is list

    source = {"a": [{"b": 1}]}
    detached = copy_native_data(source)
    source["a"][0]["b"] = 9
    assert detached == {"a": [{"b": 1}]}


def test_finite_floats_keep_their_float_type_and_spelling():
    copied = copy_native_data([1.5, float("1e308")])
    assert copied == [1.5, 1e308]
    assert [type(value) for value in copied] == [float, float]
    assert encode_json(0.1) == "0.1"
    assert encode_json(1.0) == "1.0"
    assert type(decode_json("0.1")) is Decimal


def test_scalar_roots_are_copied_unchanged():
    assert copy_native_data("text") == "text"
    assert copy_native_data(Decimal("1.20")) == Decimal("1.20")
    assert copy_native_data(None) is None


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_non_finite_numbers_are_rejected(value):
    with pytest.raises(JsonParseError, match="non-finite"):
        copy_native_data(value)
    with pytest.raises(JsonParseError, match="non-finite"):
        encode_json(value)


@pytest.mark.parametrize("value", [object(), {1, 2}, b"bytes", complex(1, 2)])
def test_unsupported_host_values_are_rejected(value):
    with pytest.raises(JsonParseError, match="unsupported native JSON value"):
        copy_native_data(value)
    with pytest.raises(JsonParseError, match="unsupported native JSON value"):
        encode_json(value)


@pytest.mark.parametrize("value", [{1: "x"}, {True: "x"}, {"a": {2: "x"}}])
def test_non_string_object_keys_are_rejected(value):
    with pytest.raises(JsonParseError, match="key must be a string"):
        copy_native_data(value)
    with pytest.raises(JsonParseError, match="key must be a string"):
        encode_json(value)


def test_malformed_json_reports_line_column_and_position():
    with pytest.raises(JsonParseError) as caught:
        decode_json('{\n  "a": 1,\n  "b": }\n}')
    error = caught.value
    assert (error.line, error.column, error.position) == (3, 8, 19)
    assert str(error) == "Expecting value: line 3 column 8 (char 19)"


def test_duplicate_keys_name_the_repeated_member():
    with pytest.raises(JsonParseError, match="duplicate key 'x'"):
        decode_json('{"a": {"x": 1, "x": 2}}')


def test_non_standard_constants_are_named():
    with pytest.raises(JsonParseError, match="NaN"):
        decode_json("NaN")


class _RecursingDecoder:
    """Stand-in for a host decoder that runs out of recursion."""

    def decode(self, text):
        raise RecursionError("maximum recursion depth exceeded while decoding JSON")


def test_host_parser_recursion_limit_becomes_a_json_limit_error(monkeypatch):
    monkeypatch.setattr(json_values_module, "_DECODER", _RecursingDecoder())
    with pytest.raises(JsonLimitError, match="host parser recursion limit"):
        decode_json("[1]")


def test_utf8_bom_is_accepted_for_every_buffer_and_stream_form():
    source = b"\xef\xbb\xbf[1]"
    assert decode_json("[1]") == [1]
    assert decode_json("\ufeff[1]") == [1]
    assert decode_json(source) == [1]
    assert decode_json(bytearray(source)) == [1]
    assert decode_json(memoryview(source)) == [1]
    assert decode_json(io.BytesIO(source)) == [1]
    assert decode_json(io.StringIO("\ufeff[1]")) == [1]


def test_invalid_utf8_is_a_parse_error():
    with pytest.raises(JsonParseError, match="valid UTF-8") as caught:
        decode_json(b'{"a": "\xff"}')
    assert caught.value.position is not None
    assert caught.value.line is None


def test_unencodable_text_is_a_parse_error():
    with pytest.raises(JsonParseError, match="valid UTF-8"):
        decode_json('"\ud800"')


def test_text_and_bytes_streams_are_read_to_eof():
    assert decode_json(io.StringIO('{"a": 1}')) == {"a": 1}
    assert decode_json(io.BytesIO(b"[1, 2]")) == [1, 2]


class _Chunks:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else b""


class _TextChunks(_Chunks):
    def read(self, size=-1):
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else ""


def test_stream_chunks_are_joined_in_order():
    assert decode_json(_Chunks([b'{"a"', b": 1}"])) == {"a": 1}
    assert decode_json(_TextChunks(['{"a"', ": 1}"])) == {"a": 1}


def test_mixed_stream_chunk_types_are_rejected():
    with pytest.raises(JsonParseError, match="mixes text and bytes"):
        decode_json(_Chunks(['{"a": ', b"1}"]))
    with pytest.raises(JsonParseError, match="mixes text and bytes"):
        decode_json(_Chunks([b'{"a": ', "1}"]))


def test_unsupported_stream_chunk_is_rejected():
    with pytest.raises(JsonParseError, match="unsupported chunk type"):
        decode_json(_Chunks([None]))


def test_byte_budget_is_enforced_while_reading():
    stream = _Chunks([b"1234", b"5678"])
    with pytest.raises(JsonLimitError, match="max_bytes=3"):
        decode_json(stream, limits=JsonLimits(max_bytes=3))
    assert stream.reads == 1


def test_byte_budget_counts_utf8_bytes_for_text_and_includes_a_bom():
    assert decode_json(_non_ascii_json(), limits=JsonLimits(max_bytes=4)) == chr(0xE9)
    with pytest.raises(JsonLimitError, match="max_bytes=2"):
        decode_json(io.StringIO(_non_ascii_json()), limits=JsonLimits(max_bytes=2))

    bom_source = b"\xef\xbb\xbf[1]"
    assert decode_json(bom_source, limits=JsonLimits(max_bytes=6)) == [1]
    with pytest.raises(JsonLimitError, match="max_bytes=5"):
        decode_json(bom_source, limits=JsonLimits(max_bytes=5))


def test_byte_budget_accepts_an_exact_boundary():
    assert decode_json(b"[1]", limits=JsonLimits(max_bytes=3)) == [1]
    assert decode_json('"x"', limits=JsonLimits(max_bytes=3)) == "x"


def test_depth_budget_is_inclusive_for_containers_and_scalars():
    assert decode_json("1", limits=JsonLimits(max_depth=1)) == 1
    assert decode_json("[1]", limits=JsonLimits(max_depth=2)) == [1]
    assert decode_json('{"a": [1]}', limits=JsonLimits(max_depth=3)) == {"a": [1]}
    with pytest.raises(JsonLimitError, match="max_depth=1"):
        decode_json("[1]", limits=JsonLimits(max_depth=1))
    with pytest.raises(JsonLimitError, match="max_depth=2"):
        decode_json('{"a": [1]}', limits=JsonLimits(max_depth=2))


def test_value_budget_counts_every_value_but_not_object_keys():
    assert decode_json("1", limits=JsonLimits(max_values=1)) == 1
    assert decode_json("[1, 2]", limits=JsonLimits(max_values=3)) == [1, 2]
    assert decode_json('{"a": 1}', limits=JsonLimits(max_values=2)) == {"a": 1}
    with pytest.raises(JsonLimitError, match="max_values=2"):
        decode_json("[1, 2]", limits=JsonLimits(max_values=2))
    with pytest.raises(JsonLimitError, match="max_values=2"):
        decode_json('{"a": [1]}', limits=JsonLimits(max_values=2))


def test_copy_enforces_depth_and_value_budgets():
    assert copy_native_data({"a": [1]}, limits=JsonLimits(max_depth=3, max_values=3)) == {"a": [1]}
    with pytest.raises(JsonLimitError, match="max_depth=2"):
        copy_native_data({"a": [1]}, limits=JsonLimits(max_depth=2))
    with pytest.raises(JsonLimitError, match="max_values=2"):
        copy_native_data([1, 2], limits=JsonLimits(max_values=2))


@pytest.mark.parametrize("name", ["max_bytes", "max_depth", "max_values"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_size_limits_must_be_positive_integers(name, value):
    with pytest.raises(ValueError, match=name):
        JsonLimits(**{name: value})


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_diagnostic_limit_must_be_a_non_negative_integer(value):
    with pytest.raises(ValueError, match="max_diagnostics"):
        JsonLimits(max_diagnostics=value)


def test_diagnostic_limit_accepts_zero_and_is_retained():
    limits = JsonLimits(max_diagnostics=0)
    assert limits.max_diagnostics == 0
    assert limits == JsonLimits(max_diagnostics=0)


def test_encode_uses_native_json_structure_and_compact_separators():
    value = {"a": [1, Decimal("1.20"), True, None], "b": {"c": "x"}}
    assert encode_json(value) == '{"a": [1, 1.20, true, null], "b": {"c": "x"}}'
    assert encode_json([]) == "[]"
    assert encode_json({}) == "{}"


def test_encode_indents_containers_and_leaves_empty_containers_compact():
    value = {"a": [1, Decimal("1.20"), True, None], "b": {"c": "x"}}
    assert encode_json(value, indent=2) == (
        '{\n  "a": [\n    1,\n    1.20,\n    true,\n    null\n  ],\n  "b": {\n    "c": "x"\n  }\n}'
    )
    assert encode_json([], indent=2) == "[]"
    assert encode_json({}, indent=4) == "{}"
    assert encode_json({"a": 1}, indent="\t") == '{\n\t"a": 1\n}'
    assert encode_json({"a": 1}, indent=0) == '{\n"a": 1\n}'


def test_encode_escapes_strings_and_keys_as_json_text():
    assert encode_json("\n") == '"\\n"'
    assert encode_json({"a\n": 1}) == '{"a\\n": 1}'
    assert encode_json(_non_ascii_json().strip('"')) == _non_ascii_json()
    assert decode_json(encode_json(_non_ascii_json().strip('"'))) == chr(0xE9)


@pytest.mark.parametrize(
    "spelling",
    [
        "0",
        "-0",
        "1.20",
        "1E+3",
        "1.0E+5",
        "1E-1000",
        "0.1000000000000000000000001",
        "-1.234567890123456789E+400",
    ],
)
def test_decimal_spelling_is_emitted_exactly(spelling):
    assert encode_json(Decimal(spelling)) == spelling


def test_decimal_round_trip_preserves_value_without_float():
    value = decode_json("-1.234567890123456789E+400")
    assert isinstance(value, Decimal)
    assert decode_json(encode_json(value)) == value
    assert json.loads(encode_json(Decimal("1.20"))) == 1.2


def test_decode_rejects_non_source_objects():
    with pytest.raises(TypeError, match="JSON source"):
        decode_json(42)
    with pytest.raises(TypeError, match="JSON source"):
        decode_json(["not", "a", "source"])


def test_json_error_types_have_expected_hierarchy():
    assert issubclass(JsonParseError, PyXSDError)
    assert issubclass(JsonLimitError, ResourcePolicyError)


def test_resolve_pointer_addresses_root_members_and_indices():
    value = {"a/b": {"~key": [10, 20]}, "": "root-property", "a": [1]}
    assert resolve_pointer(value, "") is value
    assert resolve_pointer(value, "/a~1b/~0key/1") == 20
    assert resolve_pointer(value, "/") == "root-property"
    assert resolve_pointer(value, "/a/0") == 1
    assert resolve_pointer([[7]], "/0/0") == 7
    assert resolve_pointer({"01": "literal key"}, "/01") == "literal key"


@pytest.mark.parametrize(
    "pointer",
    ["a", "/a~2b", "/a~", "/missing", "/a/1", "/a/-", "/a/01", "/a/x", "/a/"],
)
def test_resolve_pointer_rejects_invalid_or_absent_tokens(pointer):
    with pytest.raises(ValueError):
        resolve_pointer({"a": [1]}, pointer)


def test_resolve_pointer_rejects_scalar_traversal_and_bad_types():
    with pytest.raises(ValueError, match="scalar"):
        resolve_pointer({"a": 1}, "/a/b")
    with pytest.raises(ValueError, match="leading zero"):
        resolve_pointer([1], "/01")
    with pytest.raises(TypeError, match="must be a string"):
        resolve_pointer({}, 1)
