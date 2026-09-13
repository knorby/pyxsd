"""Unit and property tests for the XSD primitive data types."""

import base64
import math

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
        assert Integer.name == "integer"

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
        assert PositiveInteger.name == "positiveInteger"


class TestNonNegativeInteger:
    def test_valid_zero(self):
        assert NonNegativeInteger("0") == 0

    def test_negative_is_invalid(self):
        with pytest.raises(TypeError):
            NonNegativeInteger("-1")

    def test_name_attribute(self):
        assert NonNegativeInteger.name == "nonNegativeInteger"


class TestNegativeInteger:
    def test_valid(self):
        assert NegativeInteger("-1") == -1

    def test_zero_is_invalid(self):
        with pytest.raises(TypeError):
            NegativeInteger("0")

    def test_name_attribute(self):
        assert NegativeInteger.name == "negativeInteger"


class TestNonPositiveInteger:
    def test_valid_zero(self):
        assert NonPositiveInteger("0") == 0

    def test_positive_is_invalid(self):
        with pytest.raises(TypeError):
            NonPositiveInteger("1")

    def test_name_attribute(self):
        assert NonPositiveInteger.name == "nonPositiveInteger"


# ---------------------------------------------------------------------------
# Double
# ---------------------------------------------------------------------------


class TestDouble:
    def test_construct_from_string(self):
        assert Double("1.5") == 1.5

    def test_is_a_float(self):
        assert isinstance(Double("0.1"), float)

    def test_name_attribute(self):
        assert Double.name == "double"


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
        assert Boolean.name == "boolean"


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
        assert String.name == "string"
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
        assert Base64Binary.name == "base64Binary"


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


# ---------------------------------------------------------------------------
# Full lattice: valid and invalid lexical samples for every built-in type
# ---------------------------------------------------------------------------

from pyxsd.xsd_data_types import (  # noqa: E402
    ENTITIES,
    ENTITY,
    IDREFS,
    NMTOKEN,
    NMTOKENS,
    AnySimpleType,
    AnyType,
    AnyURI,
    Byte,
    Date,
    DateTime,
    Decimal,
    Duration,
    Float,
    GDay,
    GMonth,
    GMonthDay,
    GYear,
    GYearMonth,
    HexBinary,
    Int,
    Language,
    Long,
    Name,
    NCName,
    NormalizedString,
    QName,
    Short,
    Time,
    Token,
    UnsignedByte,
    UnsignedInt,
    UnsignedLong,
    UnsignedShort,
    xsd_value_key,
)

LATTICE = [
    # (class, [valid], [invalid])
    (AnySimpleType, ["anything", "", "  spaced  "], []),
    (AnyType, ["<any><content/>", "text"], []),
    (
        AnyURI,
        [
            "https://example.com/a?b=c",
            "relative/path",
            "",
            "urn:x:1",
            # XSD anyURI allows spaces (mapped through escaping).
            "a b",
        ],
        [],
    ),
    (Base64Binary, ["aGVsbG8=", "", "aGVs bG8="], ["not*base64!", "a"]),
    (Boolean, ["true", "false", "1", "0"], ["True", "FALSE", "2", "yes"]),
    (Byte, ["127", "-128", "0"], ["128", "-129"]),
    (
        Date,
        ["2006-08-30", "-0001-01-01", "2006-08-30Z", "2006-08-30+05:00"],
        ["2006-13-01", "2006-08-32", "06-08-30", "2006-8-30"],
    ),
    (
        DateTime,
        [
            "2006-08-30T14:30:00",
            "2006-08-30T14:30:00.123456",
            "2006-08-30T14:30:00Z",
            "2006-08-30T23:59:59",
            # End-of-day is legal when minutes and seconds are zero.
            "2006-08-30T24:00:00",
        ],
        ["2006-08-30 14:30:00", "2006-08-30T14:30", "2006-08-30T14:30:60"],
    ),
    (Decimal, ["19.95", "-0.5", "+3", ".5", "3.", "0"], ["1e5", "abc", "1.5.5", "-"]),
    (
        Double,
        ["1.5", "-0.0", "1e30", "1e+30", "INF", "-INF", "NaN"],
        ["1_0", "foo", "1.5.5", "++1"],
    ),
    (
        Duration,
        ["P1Y2M3DT10H30M", "P1D", "-P2D", "PT0.5S", "P0Y", "P1M1D"],
        ["P", "1Y", "PT", "P1S", "X1D"],
    ),
    (ENTITY, ["e1", "_x"], ["1x", "a:b"]),
    # The built-in list types require at least one item; empty is invalid.
    (
        ENTITIES,
        ["e1 e2", "e1"],
        [
            "e1 1x",
            "",
        ],
    ),
    (Float, ["1.5", "INF", "NaN"], ["foo"]),
    (GDay, ["---31", "---01Z", "---15+05:00"], ["---32", "--31", "31"]),
    (GMonth, ["--08", "--01Z", "--12"], ["--13", "--8", "---08"]),
    (GMonthDay, ["--08-30", "--12-31Z"], ["--13-01", "--08-32"]),
    (GYear, ["2006", "-0044", "12006Z"], ["06", "x", "2006-08"]),
    (GYearMonth, ["2006-08", "-0044-01Z"], ["2006-13", "2006-8"]),
    (HexBinary, ["00FF10", "", "0F"], ["0FG", "0FF"]),
    (ID, ["a1", "_x", "S-001"], ["1x", "a b", "a:b"]),
    (IDREF, ["r1"], ["1x"]),
    (
        IDREFS,
        ["a b c", "a"],
        [
            "a 1!",
            "a b!",
            "",
        ],
    ),
    (Int, ["2147483647", "-2147483648", "0"], ["2147483648", "-2147483649"]),
    (Integer, ["42", "-7", "0", "+9", " 5 "], ["1_000", "3.5", "abc"]),
    (Language, ["en", "en-US", "x-1"], ["toolonglanguage", "-en", "en_US"]),
    (
        Long,
        ["9223372036854775807", "-9223372036854775808"],
        ["9223372036854775808", "-9223372036854775809"],
    ),
    (Name, ["a", "a:b", "_x1", ":a:b:"], ["1a", "a b"]),
    (NCName, ["a", "_x1", "S-001"], ["1x", "a:b"]),
    (NMTOKEN, ["a", "1a", "a:b", "-"], ["", "a b"]),
    (
        NMTOKENS,
        ["a 1a b:c", "a"],
        [
            "a b!",
            "a,b",
            "",
        ],
    ),
    (NegativeInteger, ["-1", "-99999"], ["0", "5"]),
    (NonNegativeInteger, ["0", "5"], ["-1"]),
    (NonPositiveInteger, ["0", "-5"], ["1"]),
    (
        NormalizedString,
        [
            "hello world",
            "",
            # These are folded to spaces, not rejected.
            "a\nb",
            "a\tb",
            "a\rb",
        ],
        [],
    ),
    (PositiveInteger, ["1", "99999"], ["0", "-3"]),
    (QName, ["xs:string", "local", "_a:b9"], [":x", "a:", "1:b"]),
    (Short, ["32767", "-32768"], ["32768", "-32769"]),
    (String, ["anything", ""], []),
    (
        Time,
        [
            "14:30:00",
            "23:59:59.999",
            "00:00:00Z",
            "09:15:00-08:00",
            "24:00:00",
        ],
        ["14:30", "14:30:61"],
    ),
    (Token, ["hello", "  padded  "], []),
    (UnsignedByte, ["0", "255"], ["256", "-1"]),
    (UnsignedInt, ["0", "4294967295"], ["4294967296", "-1"]),
    (UnsignedLong, ["0", "18446744073709551615"], ["18446744073709551616", "-1"]),
    (UnsignedShort, ["0", "65535"], ["65536", "-1"]),
]


def _lattice_id(v):
    """Param id helper that understands ``pytest.param`` wrappers."""
    if hasattr(v, "values"):
        v = v.values[0]
    if isinstance(v, str):
        return v
    return getattr(v, "__name__", repr(v))


def _expand(cls, values):
    """Preserve ``pytest.param`` marks when flattening the lattice."""
    rows = []
    for value in values:
        if hasattr(value, "values"):  # pytest.param wrapper
            rows.append(pytest.param(cls, value.values[0], marks=value.marks, id=value.id))
        else:
            rows.append((cls, value))
    return rows


@pytest.mark.parametrize(
    "cls,value",
    [row for cls, valid, _ in LATTICE for row in _expand(cls, valid)],
    ids=_lattice_id,
)
def test_lattice_valid(cls, value):
    instance = cls(value)
    assert isinstance(instance, cls)


@pytest.mark.parametrize(
    "cls,value",
    [row for cls, _, invalid in LATTICE for row in _expand(cls, invalid)],
    ids=_lattice_id,
)
def test_lattice_invalid(cls, value):
    with pytest.raises(TypeError):
        cls(value)


class TestWhitespaceCollapse:
    """Token-derived types collapse whitespace before validating."""

    def test_integer_collapses(self):
        value = Integer("\n   12\n   ")
        assert value == 12

    def test_date_collapses(self):
        assert Date("  2006-08-30  ") == "2006-08-30"

    def test_double_collapses(self):
        assert Double("  1.5 ") == 1.5

    def test_boolean_collapses(self):
        assert Boolean(" true ") == 1

    def test_base64_strips_all_whitespace(self):
        assert Base64Binary("aGVs\n  bG8=") == "aGVsbG8="

    def test_string_preserves(self):
        assert String("  padded  ") == "  padded  "

    def test_normalized_string_preserves_spaces(self):
        assert NormalizedString("  padded  ") == "  padded  "


class TestXsdLexicalCorrectness:
    """Regression tests for the corrected built-in lexical rules (R13)."""

    def test_normalized_string_folds_xml_whitespace(self):
        assert NormalizedString("a\tb\nc\rd") == "a b c d"

    def test_normalized_string_keeps_nbsp(self):
        assert NormalizedString("a\u00a0b") == "a\u00a0b"

    def test_token_collapses(self):
        assert Token("  a   b  ") == "a b"

    def test_nbsp_is_not_xsd_whitespace(self):
        with pytest.raises(TypeError):
            Integer("\u00a01\u00a0")

    def test_float_is_binary32(self):
        assert Float("16777217") == 16777216.0
        assert math.isinf(Float("1e39"))
        assert Float("1e-50") == 0.0

    def test_float_accepts_plus_inf(self):
        # XSD 1.1 adds an explicit ``+INF`` spelling alongside ``INF``;
        # both denote the same value.
        assert math.isinf(Float("+INF"))
        assert Float("+INF") == Float("INF")
        # The unadorned spelling remains legal.
        assert math.isinf(Double("INF"))

    def test_base64_rejects_nonzero_pad_bits(self):
        with pytest.raises(TypeError):
            Base64Binary("AB==")
        with pytest.raises(TypeError):
            Base64Binary("AAB=")
        assert Base64Binary("AA==") == "AA=="

    def test_base64_rejects_nbsp(self):
        with pytest.raises(TypeError):
            Base64Binary("AA\u00a0==")

    def test_list_types_require_one_item(self):
        for cls in (IDREFS, ENTITIES, NMTOKENS):
            with pytest.raises(TypeError):
                cls("")
            with pytest.raises(TypeError):
                cls(" \t ")

    def test_lists_do_not_split_on_nbsp(self):
        with pytest.raises(TypeError):
            NMTOKENS("a\u00a0b")

    def test_xml_name_ranges(self):
        assert NCName("a\u0301") == "a\u0301"
        assert NCName("a\u00b7b") == "a\u00b7b"
        with pytest.raises(TypeError):
            NCName("\u00b2x")
        with pytest.raises(TypeError):
            NCName("1x")

    def test_temporal_bounds(self):
        assert Time("24:00:00") == "24:00:00"
        assert DateTime("2006-08-30T24:00:00") == "2006-08-30T24:00:00"
        with pytest.raises(TypeError):
            Date("2006-08-30+99:99")
        # XSD 1.1 allows the year zero (1 BCE) and its negative spelling.
        assert GYear("0000") == "0000"
        assert GYear("-0000") == "-0000"
        with pytest.raises(TypeError):
            GYear("02006")
        with pytest.raises(TypeError):
            GYear("\u0662\u0660\u0660\u0666")
        assert GYear("-0044") == "-0044"

    def test_anyuri_allows_spaces(self):
        assert AnyURI("a b") == "a b"
        assert AnyURI("  a b  ") == "a b"

    def test_duration_collapses_whitespace(self):
        assert Duration(" P1D ") == "P1D"

    def test_value_key_normalises_equivalent_spellings(self):
        assert xsd_value_key(HexBinary("FF")) == xsd_value_key(HexBinary("ff"))
        assert xsd_value_key(NMTOKENS("a  b")) == xsd_value_key(NMTOKENS("a b"))
        assert xsd_value_key(Date("2006-08-30+00:00")) == xsd_value_key(Date("2006-08-30Z"))
        assert xsd_value_key(DateTime("1999-12-31T19:00:00-05:00")) == xsd_value_key(
            DateTime("2000-01-01T00:00:00Z")
        )
        assert xsd_value_key(Date("2006-08-30")) != xsd_value_key(Date("2006-08-31"))


class TestListTypes:
    def test_tokens_property(self):
        value = IDREFS("r1 r2 r3")
        assert value.tokens == ["r1", "r2", "r3"]

    def test_nmtokens_tokens(self):
        assert NMTOKENS("a 1a b:c").tokens == ["a", "1a", "b:c"]


class TestBuiltinNameTable:
    def test_table_covers_every_exported_concrete_type(self):
        from pyxsd import xsd_data_types
        from pyxsd.element_representatives.element_representative import _PRIMITIVE_TYPES

        for export in xsd_data_types.__all__:
            klass = getattr(xsd_data_types, export)
            if klass in (XsdDataType, xsd_data_types.TypeList):
                continue
            assert klass.name in _PRIMITIVE_TYPES, klass.__name__
            assert _PRIMITIVE_TYPES[klass.name] is klass

    def test_table_has_45_builtins(self):
        from pyxsd.element_representatives.element_representative import _PRIMITIVE_TYPES

        assert len(_PRIMITIVE_TYPES) == 45
        # Spot-check XSD spellings.
        for xsd_name in (
            "string",
            "integer",
            "int",
            "boolean",
            "decimal",
            "double",
            "float",
            "dateTime",
            "date",
            "time",
            "duration",
            "gYearMonth",
            "gMonthDay",
            "base64Binary",
            "hexBinary",
            "anyURI",
            "QName",
            "NCName",
            "NMTOKENS",
            "unsignedByte",
            "positiveInteger",
            "anySimpleType",
            "anyType",
        ):
            assert xsd_name in _PRIMITIVE_TYPES
