"""Unit and property tests for the XSD primitive data types."""

import base64

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pyxsd.xsd_data_types import (
    ID,
    IDREF,
    Base64Binary,
    Boolean,
    Double,
    Integer,
    NegativeInteger,
    NonNegativeInteger,
    NonPositiveInteger,
    PositiveInteger,
    String,
    TypeList,
    XsdDataType,
)

# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_all_exports_importable():
    import pyxsd.xsd_data_types as module

    for name in module.__all__:
        assert hasattr(module, name)


def test_all_types_subclass_xsd_data_type():
    for cls in (
        Integer,
        PositiveInteger,
        NonNegativeInteger,
        NegativeInteger,
        NonPositiveInteger,
        Double,
        TypeList,
        Boolean,
        String,
        ID,
        IDREF,
        Base64Binary,
    ):
        assert issubclass(cls, XsdDataType)


# ---------------------------------------------------------------------------
# Integer and derived integer types
# ---------------------------------------------------------------------------


class TestInteger:
    def test_construct_from_string(self):
        assert Integer("42") == 42

    def test_construct_from_int(self):
        assert Integer(-7) == -7

    def test_is_an_int(self):
        assert isinstance(Integer("0"), int)

    def test_name_attribute(self):
        assert Integer.name == "Integer"

    def test_arithmetic_demotes_to_plain_int(self):
        result = Integer("5") + 1
        assert result == 6
        assert type(result) is int


class TestPositiveInteger:
    def test_valid(self):
        assert PositiveInteger("1") == 1

    def test_zero_is_invalid(self):
        with pytest.raises(TypeError):
            PositiveInteger("0")

    def test_negative_is_invalid(self):
        with pytest.raises(TypeError):
            PositiveInteger(-3)

    def test_name_attribute(self):
        assert PositiveInteger.name == "PositiveInteger"


class TestNonNegativeInteger:
    def test_valid_zero(self):
        assert NonNegativeInteger("0") == 0

    def test_negative_is_invalid(self):
        with pytest.raises(TypeError):
            NonNegativeInteger("-1")

    def test_name_attribute(self):
        assert NonNegativeInteger.name == "NonNegativeInteger"


class TestNegativeInteger:
    def test_valid(self):
        assert NegativeInteger("-1") == -1

    def test_zero_is_invalid(self):
        with pytest.raises(TypeError):
            NegativeInteger("0")

    def test_name_attribute(self):
        assert NegativeInteger.name == "NegativeInteger"


class TestNonPositiveInteger:
    def test_valid_zero(self):
        assert NonPositiveInteger("0") == 0

    def test_positive_is_invalid(self):
        with pytest.raises(TypeError):
            NonPositiveInteger("1")

    def test_name_attribute(self):
        assert NonPositiveInteger.name == "NonPositiveInteger"


# ---------------------------------------------------------------------------
# Double
# ---------------------------------------------------------------------------


class TestDouble:
    def test_construct_from_string(self):
        assert Double("1.5") == 1.5

    def test_is_a_float(self):
        assert isinstance(Double("0.1"), float)

    def test_name_attribute(self):
        assert Double.name == "Double"


# ---------------------------------------------------------------------------
# Boolean
# ---------------------------------------------------------------------------


class TestBoolean:
    def test_true_values(self):
        assert Boolean(1) == 1
        assert Boolean("1") == 1

    def test_false_values(self):
        assert Boolean(0) == 0
        assert Boolean("0") == 0

    def test_out_of_range_is_invalid(self):
        with pytest.raises(TypeError):
            Boolean(2)

    def test_negative_is_invalid(self):
        with pytest.raises(TypeError):
            Boolean(-1)

    def test_lexical_form(self):
        assert str(Boolean(1)) == "true"
        assert str(Boolean(0)) == "false"

    def test_python_repr(self):
        assert repr(Boolean(1)) == "True"
        assert repr(Boolean(0)) == "False"

    def test_val_attribute(self):
        assert Boolean(1).val == 1
        assert Boolean(0).val == 0

    def test_name_attribute(self):
        assert Boolean.name == "Boolean"


# ---------------------------------------------------------------------------
# String and derived string types
# ---------------------------------------------------------------------------


class TestStringTypes:
    def test_string_behaves_like_str(self):
        value = String("hello")
        assert value == "hello"
        assert isinstance(value, str)
        assert value.upper() == "HELLO"

    def test_names(self):
        assert String.name == "String"
        assert ID.name == "ID"
        assert IDREF.name == "IDREF"


# ---------------------------------------------------------------------------
# Base64Binary
# ---------------------------------------------------------------------------


class TestBase64Binary:
    def test_valid_value(self):
        value = Base64Binary("aGVsbG8=")
        assert value == "aGVsbG8="
        assert base64.b64decode(str(value)) == b"hello"

    def test_empty_value_is_valid(self):
        assert Base64Binary("") == ""

    def test_invalid_characters(self):
        with pytest.raises(TypeError):
            Base64Binary("not*base64!")

    def test_bad_padding(self):
        with pytest.raises(TypeError):
            Base64Binary("a")

    def test_name_attribute(self):
        assert Base64Binary.name == "Base64Binary"


# ---------------------------------------------------------------------------
# TypeList
# ---------------------------------------------------------------------------


class TestTypeList:
    def test_behaves_like_list(self):
        value = TypeList(["a", "b"])
        assert value == ["a", "b"]
        assert isinstance(value, list)
        value.append("c")
        assert value == ["a", "b", "c"]

    def test_name_attribute(self):
        assert TypeList.name == "List"


# ---------------------------------------------------------------------------
# Property tests: lexical round-trips
# ---------------------------------------------------------------------------


@given(st.integers(min_value=1, max_value=10**18))
def test_positive_integer_lexical_round_trip(value):
    assert int(PositiveInteger(str(value))) == value


@given(st.integers(min_value=0, max_value=10**18))
def test_non_negative_integer_lexical_round_trip(value):
    assert int(NonNegativeInteger(str(value))) == value


@given(st.integers(min_value=-(10**18), max_value=-1))
def test_negative_integer_lexical_round_trip(value):
    assert int(NegativeInteger(str(value))) == value


@given(st.integers(min_value=-(10**18), max_value=0))
def test_non_positive_integer_lexical_round_trip(value):
    assert int(NonPositiveInteger(str(value))) == value


@given(st.booleans())
def test_boolean_lexical_round_trip(value):
    numeric = int(value)
    parsed = Boolean(numeric)
    assert (str(parsed) == "true") is value
    assert (repr(parsed) == "True") is value


@given(st.binary(max_size=64))
def test_base64_binary_lexical_round_trip(value):
    encoded = base64.b64encode(value).decode("ascii")
    parsed = Base64Binary(encoded)
    assert base64.b64decode(str(parsed)) == value


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_double_lexical_round_trip(value):
    assert float(Double(str(value))) == value


@given(st.text(max_size=64))
def test_string_lexical_round_trip(value):
    assert str(String(value)) == value
