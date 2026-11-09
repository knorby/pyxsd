"""Contract tests for the independent, immutable XDM 3.1 value layer.

The XDM layer is deliberately independent of stylesheet execution: a sequence
is a tuple, the empty sequence is ``()``, a Python string is one ``xs:string``,
and ``None`` is never an XDM value.  Maps, arrays, and function items keep XDM
identity semantics; map keys use XDM key equality, and arrays are indexed from
one.  These tests are corpus-independent and cover every new statement and
branch.
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import pytest
from elementpath import datatypes as ep

from pyxsd import xsd_data_types as xsd
from pyxsd.xdm import (
    XdmArray,
    XdmAtomic,
    XdmFunction,
    XdmFunctionSignature,
    XdmMap,
    XdmSequenceType,
    as_xdm,
)
from pyxsd.xsd_data_types import qname_context

XSD = "http://www.w3.org/2001/XMLSchema"
INTEGER = f"{{{XSD}}}integer"
STRING = f"{{{XSD}}}string"
BOOLEAN = f"{{{XSD}}}boolean"
DOUBLE = f"{{{XSD}}}double"


class ExplodingString(str):
    """A host string whose iteration must never be consulted by ``as_xdm``."""

    def __iter__(self):  # pragma: no cover - failing here is the assertion
        raise AssertionError("a Python string must not be iterated as characters")


class UnhashableAtomicPayload:
    """A hashable-contract violator that otherwise looks like a datatype value."""

    name = "unhashableAtomic"
    __hash__ = None


def _atomic(value: Any) -> XdmAtomic:
    result = as_xdm(value)
    assert len(result) == 1
    item = result[0]
    assert isinstance(item, XdmAtomic)
    return item


# ---------------------------------------------------------------------------
# Sequences and host conversion


def test_sequences_maps_arrays_and_functions():
    assert as_xdm("abc") == (XdmAtomic.string("abc"),)
    assert as_xdm(()) == ()
    with pytest.raises(TypeError):
        as_xdm(None)
    mapping = XdmMap.empty().put(XdmAtomic.string("a"), as_xdm(1))
    assert mapping.get(XdmAtomic.string("a")) == as_xdm(1)
    array = XdmArray((as_xdm(1), as_xdm("two")))
    assert array.get(2) == as_xdm("two")


def test_empty_sequence_is_an_empty_tuple():
    assert as_xdm(()) == ()
    assert as_xdm([]) == ()
    assert as_xdm(iter(())) == ()


def test_none_is_not_an_xdm_value():
    with pytest.raises(TypeError, match="None"):
        as_xdm(None)
    with pytest.raises(TypeError, match="None"):
        as_xdm((1, None))


def test_a_python_string_is_one_string_not_characters():
    atomic = XdmAtomic.string("abc")
    assert as_xdm("abc") == (atomic,)
    assert as_xdm(ExplodingString("abc")) == (atomic,)
    assert len(as_xdm("abc")) == 1


def test_host_primitives_convert_to_typed_atomics():
    assert as_xdm(True) == (XdmAtomic.boolean(True),)
    assert as_xdm(3) == (XdmAtomic.integer(3),)
    assert as_xdm(Decimal("1.50")) == (XdmAtomic.decimal("1.50"),)
    assert as_xdm(1.5) == (XdmAtomic.double(1.5),)
    encoded = as_xdm(b"abc")
    assert len(encoded) == 1
    assert isinstance(encoded[0], XdmAtomic)
    assert encoded[0].type_name == f"{{{XSD}}}base64Binary"
    assert encoded[0].value.to_bytes() == b"abc"


def test_pyxsd_datatypes_retain_xsd_identity():
    int_value = xsd.Int("7")
    token_value = xsd.Token("a")
    boolean_value = xsd.Boolean("true")
    for value, datatype, type_name in [
        (int_value, xsd.Int, f"{{{XSD}}}int"),
        (token_value, xsd.Token, f"{{{XSD}}}token"),
        (boolean_value, xsd.Boolean, BOOLEAN),
    ]:
        atomic = as_xdm(value)[0]
        assert isinstance(atomic, XdmAtomic)
        assert atomic.datatype is datatype
        assert atomic.type_name == type_name
        assert atomic.value is value


def test_elementpath_datatypes_retain_identity_and_value():
    untyped = as_xdm(ep.UntypedAtomic("x"))[0]
    integer = as_xdm(ep.Integer(4))[0]
    uri = as_xdm(ep.AnyURI("urn:x"))[0]
    assert untyped.type_name == f"{{{XSD}}}untypedAtomic"
    assert untyped.datatype is ep.UntypedAtomic
    assert integer.type_name == INTEGER
    assert integer.datatype is ep.Integer
    assert int(integer.value) == 4
    assert uri.type_name == f"{{{XSD}}}anyURI"
    assert str(uri.value) == "urn:x"


def test_python_sequences_flatten_but_strings_stay_atomic():
    assert as_xdm([1, (2, "three")]) == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
        XdmAtomic.string("three"),
    )
    assert as_xdm(range(2)) == (XdmAtomic.integer(0), XdmAtomic.integer(1))


def test_host_mapping_becomes_an_xdm_map_item():
    result = as_xdm({"a": 1, "b": [2, 3]})
    assert len(result) == 1
    mapping = result[0]
    assert isinstance(mapping, XdmMap)
    assert mapping.get(XdmAtomic.string("a")) == as_xdm(1)
    assert mapping.get(XdmAtomic.string("b")) == (XdmAtomic.integer(2), XdmAtomic.integer(3))


def test_mapping_keys_must_convert_to_one_atomic_value():
    with pytest.raises(TypeError, match="atomic"):
        as_xdm({("a", "b"): 1})
    with pytest.raises(TypeError, match="atomic"):
        as_xdm({(): 1})
    with pytest.raises(TypeError, match="atomic"):
        as_xdm({XdmMap.empty(): 1})


def test_as_xdm_rejects_unordered_sets_and_unknown_host_values():
    for value in ({1, 2}, frozenset({1, 2})):
        with pytest.raises(TypeError, match="ordered"):
            as_xdm(value)
    with pytest.raises(TypeError, match="object"):
        as_xdm(object())


def test_as_xdm_rejects_xsd_list_values():
    with pytest.raises(TypeError, match="list"):
        as_xdm(xsd.TypeList(["a"]))
    with pytest.raises(TypeError, match="list"):
        as_xdm(xsd.NMTOKENS("a b"))


def test_as_xdm_is_idempotent_for_items_and_sequences():
    atomic = XdmAtomic.string("a")
    mapping = XdmMap.empty()
    array = XdmArray(())
    function = XdmFunction(lambda args, environment, closure: (), arity=0)
    for item in (atomic, mapping, array, function):
        assert as_xdm(item) == (item,)
    sequence = (atomic, mapping)
    assert as_xdm(sequence) == sequence


# ---------------------------------------------------------------------------
# Atomic values


def test_atomic_factories_and_lexical_forms():
    cases = [
        (XdmAtomic.string("a"), STRING, "a"),
        (XdmAtomic.integer(-2), INTEGER, "-2"),
        (XdmAtomic.decimal("1.50"), f"{{{XSD}}}decimal", "1.50"),
        (XdmAtomic.double(math.inf), DOUBLE, "INF"),
        (XdmAtomic.double(-math.inf), DOUBLE, "-INF"),
        (XdmAtomic.boolean(True), BOOLEAN, "true"),
        (XdmAtomic.boolean(False), BOOLEAN, "false"),
        (XdmAtomic.untyped_atomic("x"), f"{{{XSD}}}untypedAtomic", "x"),
        (XdmAtomic.any_uri("urn:x"), f"{{{XSD}}}anyURI", "urn:x"),
    ]
    for atomic, type_name, lexical in cases:
        assert atomic.type_name == type_name
        assert atomic.lexical == lexical
        assert str(atomic) == lexical


def test_special_float_lexical_forms():
    assert XdmAtomic.double(float("nan")).lexical == "NaN"
    assert XdmAtomic.float(math.inf).lexical == "INF"
    assert XdmAtomic.float(-math.inf).lexical == "-INF"
    # A native bool/float directly attached by a lower-level adapter still
    # serializes with XSD spellings rather than Python spellings.
    assert XdmAtomic(True, BOOLEAN).lexical == "true"
    assert XdmAtomic(False, BOOLEAN).lexical == "false"
    assert XdmAtomic(float("nan"), DOUBLE).lexical == "NaN"
    assert XdmAtomic(float("inf"), DOUBLE).lexical == "INF"
    assert XdmAtomic(float("-inf"), DOUBLE).lexical == "-INF"
    assert XdmAtomic(1.5, DOUBLE).lexical == "1.5"


@pytest.mark.parametrize(
    ("factory", "value"),
    [
        (XdmAtomic.string, 1),
        (XdmAtomic.integer, True),
        (XdmAtomic.integer, "1"),
        (XdmAtomic.decimal, True),
        (XdmAtomic.decimal, object()),
        (XdmAtomic.double, True),
        (XdmAtomic.double, "1"),
        (XdmAtomic.float, True),
        (XdmAtomic.float, "1"),
        (XdmAtomic.boolean, 1),
        (XdmAtomic.untyped_atomic, 1),
        (XdmAtomic.any_uri, 1),
    ],
)
def test_atomic_factory_type_errors(factory, value):
    with pytest.raises(TypeError):
        factory(value)


def test_typed_requires_a_datatype_with_a_type_name():
    token = XdmAtomic.typed(xsd.Token("a"), xsd.Token)
    assert token.type_name == f"{{{XSD}}}token"
    assert token.datatype is xsd.Token

    with pytest.raises(TypeError, match="datatype"):
        XdmAtomic.typed("a", object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="name"):
        XdmAtomic.typed("a", type("Nameless", (), {}))


def test_typed_enforces_the_atomic_value_invariant():
    with pytest.raises(TypeError, match="list"):
        XdmAtomic.typed(xsd.TypeList(["a"]), xsd.TypeList)
    with pytest.raises(TypeError, match="list"):
        XdmAtomic.typed(xsd.NMTOKENS("a b"), xsd.NMTOKENS)
    with pytest.raises(TypeError, match="None"):
        XdmAtomic.typed(None, xsd.String)
    with pytest.raises(TypeError, match="instance"):
        XdmAtomic.typed("a", xsd.String)
    payload = UnhashableAtomicPayload()
    with pytest.raises(TypeError, match="hashable"):
        XdmAtomic.typed(payload, UnhashableAtomicPayload)

    token = XdmAtomic.typed(xsd.Token("a"), xsd.Token)
    assert hash(token) == hash(XdmAtomic.string("a"))
    mapping = XdmMap.empty().put(token, as_xdm(1))
    assert mapping.get(XdmAtomic.string("a")) == as_xdm(1)


def test_atomic_constructor_enforces_the_atomic_value_invariant():
    with pytest.raises(TypeError, match="None"):
        XdmAtomic(None, STRING)
    with pytest.raises(TypeError, match="list"):
        XdmAtomic(xsd.TypeList(["a"]), f"{{{XSD}}}List")
    with pytest.raises(TypeError, match="list"):
        XdmAtomic("a b", f"{{{XSD}}}NMTOKENS")
    with pytest.raises(TypeError, match="hashable"):
        XdmAtomic(UnhashableAtomicPayload(), "{urn:test}unhashable")


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_decimal_factory_rejects_non_finite_values(value):
    with pytest.raises(TypeError, match="finite"):
        XdmAtomic.decimal(value)


def test_atomic_equality_uses_xdm_value_semantics():
    assert XdmAtomic.string("a") == XdmAtomic.string("a")
    assert XdmAtomic.string("a") != XdmAtomic.string("b")
    assert XdmAtomic.string("a") != XdmAtomic.integer(1)
    assert XdmAtomic.integer(1) == XdmAtomic.decimal("1.0")
    assert XdmAtomic.integer(1) == XdmAtomic.double(1.0)
    assert XdmAtomic.boolean(True) != XdmAtomic.integer(1)
    assert XdmAtomic.string("a") == XdmAtomic.typed(xsd.Token("a"), xsd.Token)
    assert XdmAtomic.string("a") == XdmAtomic.any_uri("a")
    assert XdmAtomic.string("a") == XdmAtomic.untyped_atomic("a")
    assert XdmAtomic.integer(1) != "1"
    assert XdmAtomic.double(float("nan")) != XdmAtomic.double(float("nan"))
    assert hash(XdmAtomic.integer(1)) == hash(XdmAtomic.double(1.0))
    assert hash(XdmAtomic.string("a")) == hash(XdmAtomic.any_uri("a"))


def test_atomic_equality_across_temporal_and_binary_families():
    assert XdmAtomic.typed(xsd.Date("2000-01-01"), xsd.Date) == XdmAtomic.typed(
        xsd.Date("2000-01-01"), xsd.Date
    )
    assert XdmAtomic.typed(xsd.Date("2000-01-01"), xsd.Date) != XdmAtomic.typed(
        xsd.DateTime("2000-01-01T00:00:00"), xsd.DateTime
    )
    assert XdmAtomic.typed(xsd.Duration("P1Y"), xsd.Duration) == XdmAtomic.typed(
        xsd.Duration("P1Y"), xsd.Duration
    )
    assert XdmAtomic.typed(xsd.Base64Binary("YWJj"), xsd.Base64Binary) == XdmAtomic.typed(
        xsd.Base64Binary("YWJj"), xsd.Base64Binary
    )
    assert XdmAtomic.typed(xsd.Base64Binary("YWJj"), xsd.Base64Binary) != XdmAtomic.typed(
        xsd.HexBinary("616263"), xsd.HexBinary
    )
    assert XdmAtomic.typed(ep.Date(2000, 1, 1), ep.Date) == XdmAtomic.typed(
        ep.Date(2000, 1, 1), ep.Date
    )
    assert XdmAtomic.typed(ep.Duration(months=12), ep.Duration) == XdmAtomic.typed(
        ep.Duration(months=12), ep.Duration
    )
    assert XdmAtomic.typed(ep.Base64Binary("YWJj"), ep.Base64Binary) == XdmAtomic.typed(
        ep.Base64Binary("YWJj"), ep.Base64Binary
    )


def test_atomic_equality_uses_xsd_value_space_lexical_variants():
    assert _atomic(xsd.Duration("P1Y")) == _atomic(xsd.Duration("P12M"))
    assert _atomic(xsd.YearMonthDuration("P1Y")) == _atomic(xsd.YearMonthDuration("P12M"))
    assert _atomic(xsd.DayTimeDuration("P1D")) == _atomic(xsd.DayTimeDuration("PT24H"))
    assert _atomic(xsd.HexBinary("FF")) == _atomic(xsd.HexBinary("ff"))
    assert _atomic(xsd.Date("2000-01-01Z")) == _atomic(xsd.Date("2000-01-01+00:00"))
    assert _atomic(xsd.DateTime("1999-12-31T19:00:00-05:00")) == _atomic(
        xsd.DateTime("2000-01-01T00:00:00Z")
    )
    assert hash(_atomic(xsd.Duration("P1Y"))) == hash(_atomic(xsd.Duration("P12M")))
    assert hash(_atomic(xsd.HexBinary("FF"))) == hash(_atomic(xsd.HexBinary("ff")))
    assert hash(_atomic(xsd.Date("2000-01-01Z"))) == hash(_atomic(xsd.Date("2000-01-01+00:00")))
    assert _atomic(xsd.Duration("P1M")) != _atomic(xsd.Duration("P30D"))
    assert _atomic(xsd.HexBinary("FF")) != _atomic(xsd.HexBinary("FE"))
    assert _atomic(xsd.Date("2000-01-01")) != _atomic(xsd.Date("2000-01-01Z"))


def test_atomic_numeric_equality_uses_xpath_numeric_promotion():
    assert _atomic(XdmAtomic.decimal("1.1")) == _atomic(XdmAtomic.double(1.1))
    assert _atomic(XdmAtomic.decimal("1.1")).same_key(_atomic(XdmAtomic.double(1.1)))
    assert hash(_atomic(XdmAtomic.decimal("1.1"))) == hash(_atomic(XdmAtomic.double(1.1)))
    assert _atomic(XdmAtomic.decimal("0.1")) != _atomic(XdmAtomic.decimal("0.10000000000000001"))
    assert not _atomic(XdmAtomic.decimal("0.1")).same_key(
        _atomic(XdmAtomic.decimal("0.10000000000000001"))
    )
    assert _atomic(XdmAtomic.integer(10**400)) == _atomic(XdmAtomic.double(float("inf")))
    assert _atomic(XdmAtomic.integer(10**400)).same_key(_atomic(XdmAtomic.double(float("inf"))))
    assert _atomic(XdmAtomic.integer(1)) != _atomic(XdmAtomic.double(float("nan")))
    assert not _atomic(XdmAtomic.integer(1)).same_key(_atomic(XdmAtomic.double(float("nan"))))
    assert not _atomic(XdmAtomic.double(float("nan"))).same_key(_atomic(XdmAtomic.integer(1)))
    assert hash(_atomic(XdmAtomic.double(float("nan")))) == hash(
        _atomic(XdmAtomic.double(float("nan")))
    )


def test_atomic_qname_equality_uses_expanded_names():
    unresolved = as_xdm(xsd.QName("p:a"))[0]
    with qname_context({"p": "urn:x", "q": "urn:x"}):
        first = as_xdm(xsd.QName("p:a"))[0]
        second = as_xdm(xsd.QName("q:a"))[0]
        other = as_xdm(xsd.QName("p:b"))[0]
    assert first == second
    assert hash(first) == hash(second)
    assert first != other
    assert unresolved == as_xdm(xsd.QName("p:a"))[0]
    assert unresolved == XdmAtomic("p:a", f"{{{XSD}}}QName")
    assert XdmAtomic("p:a", f"{{{XSD}}}QName") != XdmAtomic("q:a", f"{{{XSD}}}QName")
    assert as_xdm(ep.QName("urn:x", "p:a"))[0] == as_xdm(ep.QName("urn:x", "q:a"))[0]
    assert as_xdm(ep.QName("urn:x", "p:a"))[0] != as_xdm(ep.QName("urn:y", "p:a"))[0]
    with qname_context({"q": "urn:x"}):
        unbound_prefix = as_xdm(xsd.QName("p:a"))[0]
    assert unbound_prefix == XdmAtomic("p:a", f"{{{XSD}}}QName")
    assert unbound_prefix != XdmAtomic("a", f"{{{XSD}}}QName")


def test_atomic_fallback_equality_for_unknown_simple_types():
    left = XdmAtomic.typed(xsd.AnySimpleType("a"), xsd.AnySimpleType)
    right = XdmAtomic.typed(xsd.AnySimpleType("a"), xsd.AnySimpleType)
    different = XdmAtomic.typed(xsd.AnyType("a"), xsd.AnyType)
    assert left == right
    assert left != different
    assert left != XdmAtomic.string("a")


def test_atomic_same_key_handles_nan_and_cross_family_values():
    assert XdmAtomic.double(float("nan")).same_key(XdmAtomic.double(float("nan")))
    assert XdmAtomic.integer(1).same_key(XdmAtomic.double(1.0))
    assert XdmAtomic.string("a").same_key(XdmAtomic.untyped_atomic("a"))
    assert XdmAtomic.string("a").same_key(XdmAtomic.any_uri("a"))
    assert not XdmAtomic.string("a").same_key(XdmAtomic.integer(1))
    assert not XdmAtomic.integer(1).same_key("1")
    with qname_context({"p": "urn:x", "q": "urn:x"}):
        assert as_xdm(xsd.QName("p:a"))[0].same_key(as_xdm(xsd.QName("q:a"))[0])
    assert XdmAtomic.typed(xsd.Date("2000-01-01"), xsd.Date).same_key(
        XdmAtomic.typed(xsd.Date("2000-01-01"), xsd.Date)
    )
    assert XdmAtomic.typed(xsd.Duration("P1Y"), xsd.Duration).same_key(
        XdmAtomic.typed(xsd.Duration("P1Y"), xsd.Duration)
    )
    assert XdmAtomic.typed(xsd.Base64Binary("YWJj"), xsd.Base64Binary).same_key(
        XdmAtomic.typed(xsd.Base64Binary("YWJj"), xsd.Base64Binary)
    )
    assert XdmAtomic(True, BOOLEAN).same_key(XdmAtomic(True, BOOLEAN))
    assert not XdmAtomic(True, BOOLEAN).same_key(XdmAtomic(False, BOOLEAN))
    assert not XdmAtomic.typed(xsd.AnySimpleType("a"), xsd.AnySimpleType).same_key(
        XdmAtomic.typed(xsd.AnyType("a"), xsd.AnyType)
    )


def test_atomic_same_key_uses_xsd_value_space_lexical_variants():
    assert _atomic(xsd.Duration("P1Y")).same_key(_atomic(xsd.Duration("P12M")))
    assert _atomic(xsd.YearMonthDuration("P1Y")).same_key(_atomic(xsd.YearMonthDuration("P12M")))
    assert _atomic(xsd.DayTimeDuration("P1D")).same_key(_atomic(xsd.DayTimeDuration("PT24H")))
    assert _atomic(xsd.HexBinary("FF")).same_key(_atomic(xsd.HexBinary("ff")))
    assert _atomic(xsd.Date("2000-01-01Z")).same_key(_atomic(xsd.Date("2000-01-01+00:00")))
    assert _atomic(xsd.DateTime("1999-12-31T19:00:00-05:00")).same_key(
        _atomic(xsd.DateTime("2000-01-01T00:00:00Z"))
    )
    assert not _atomic(xsd.Duration("P1M")).same_key(_atomic(xsd.Duration("P30D")))
    assert not _atomic(xsd.Date("2000-01-01")).same_key(_atomic(xsd.Date("2000-01-01Z")))


def test_atomic_nan_detection_for_decimal_and_non_numeric_values():
    nan_decimal = XdmAtomic(Decimal("NaN"), f"{{{XSD}}}decimal")
    assert nan_decimal.same_key(XdmAtomic(Decimal("NaN"), f"{{{XSD}}}decimal"))
    assert nan_decimal != nan_decimal
    assert XdmAtomic.string("a").same_key(XdmAtomic.string("a"))
    assert XdmAtomic.integer(1).same_key(XdmAtomic.integer(1))


def test_atomic_cast_between_lexical_types():
    untyped = XdmAtomic.untyped_atomic("42")
    assert untyped.cast("integer") == XdmAtomic.integer(42)
    assert XdmAtomic.untyped_atomic("1").cast(f"{{{XSD}}}boolean") == XdmAtomic.boolean(True)
    assert untyped.cast("decimal") == XdmAtomic.decimal("42")
    assert untyped.cast("double") == XdmAtomic.double(42.0)
    assert untyped.cast("float") == XdmAtomic.float(42.0)
    assert untyped.cast("anyURI") == XdmAtomic.any_uri("42")
    assert untyped.cast("string") == XdmAtomic.string("42")
    assert XdmAtomic.string("a").cast("untypedAtomic") == XdmAtomic.untyped_atomic("a")
    assert XdmAtomic.double(math.inf).cast("string") == XdmAtomic.string("INF")
    assert XdmAtomic.double(math.inf).cast("untypedAtomic") == XdmAtomic.untyped_atomic("INF")
    assert XdmAtomic.boolean(True).cast("string") == XdmAtomic.string("true")
    integer = XdmAtomic.integer(3)
    assert integer.cast("integer") is integer
    with pytest.raises(TypeError, match="cast"):
        untyped.cast("noSuchType")
    with pytest.raises(TypeError, match="cast"):
        XdmAtomic.string("x").cast("QName")
    with pytest.raises(TypeError, match="cast"):
        XdmAtomic.untyped_atomic("not-an-int").cast("integer")


def test_atomic_values_are_frozen():
    atomic = XdmAtomic.string("a")
    with pytest.raises(FrozenInstanceError):
        atomic.value = "b"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Maps


def test_map_empty_lookup_and_contains():
    mapping = XdmMap.empty()
    assert mapping.size == 0
    assert len(mapping) == 0
    assert mapping.get(XdmAtomic.string("a")) == ()
    assert not mapping.contains(XdmAtomic.string("a"))
    assert mapping.keys() == ()
    assert mapping.values() == ()
    assert mapping.entries == ()
    empty = as_xdm({})
    assert len(empty) == 1
    assert isinstance(empty[0], XdmMap)
    assert empty[0].entries == ()


def test_map_distinguishes_an_empty_value_from_a_missing_key():
    key = XdmAtomic.string("a")
    mapping = XdmMap.empty().put(key, ())
    assert mapping.get(key) == ()
    assert mapping.contains(key)
    assert mapping.get(XdmAtomic.string("b")) == ()
    assert not mapping.contains(XdmAtomic.string("b"))


def test_map_keys_do_not_follow_python_dictionary_collapsing():
    mapping = (
        XdmMap.empty()
        .put(XdmAtomic.integer(1), as_xdm("integer"))
        .put(XdmAtomic.boolean(True), as_xdm("boolean"))
        .put(XdmAtomic.string("1"), as_xdm("string"))
    )
    assert mapping.size == 3
    assert mapping.get(XdmAtomic.integer(1)) == as_xdm("integer")
    assert mapping.get(XdmAtomic.boolean(True)) == as_xdm("boolean")
    assert mapping.get(XdmAtomic.string("1")) == as_xdm("string")


def test_map_put_is_immutable_and_replaces_duplicates_in_place():
    key = XdmAtomic.string("a")
    first = XdmMap.empty().put(key, as_xdm(1))
    second = first.put(XdmAtomic.string("b"), as_xdm(2))
    third = second.put(key, as_xdm(3))
    assert first.get(key) == as_xdm(1)
    assert second.get(key) == as_xdm(1)
    assert third.get(key) == as_xdm(3)
    assert third.keys() == (key, XdmAtomic.string("b"))
    assert third.values() == (as_xdm(3), as_xdm(2))
    assert third.size == 2


def test_map_values_accept_an_item_or_a_sequence_and_keep_strings_whole():
    key = XdmAtomic.string("a")
    mapping = XdmMap.empty().put(key, XdmAtomic.integer(1))
    assert mapping.get(key) == (XdmAtomic.integer(1),)
    mapping = mapping.put(key, "three")
    assert mapping.get(key) == (XdmAtomic.string("three"),)


def test_map_keys_must_be_single_atomic_values():
    mapping = XdmMap.empty()
    for key in ("a", (XdmAtomic.string("a"),), XdmMap.empty()):
        with pytest.raises(TypeError, match="atomic"):
            mapping.put(key, as_xdm(1))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="atomic"):
            mapping.get(key)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="atomic"):
            mapping.contains(key)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="atomic"):
            mapping.remove(key)  # type: ignore[arg-type]


def test_map_remove_present_and_absent():
    key = XdmAtomic.string("a")
    mapping = XdmMap.empty().put(key, as_xdm(1))
    assert mapping.remove(key).entries == ()
    assert mapping.remove(key).remove(key).entries == ()
    assert mapping.entries == ((key, as_xdm(1)),)


def test_map_merge_duplicate_policies():
    first = XdmMap.empty().put(XdmAtomic.string("a"), as_xdm(1))
    second = (
        XdmMap.empty().put(XdmAtomic.string("a"), as_xdm(2)).put(XdmAtomic.string("b"), as_xdm(3))
    )
    use_first = XdmMap.merge(first, second)
    assert use_first.get(XdmAtomic.string("a")) == as_xdm(1)
    assert use_first.get(XdmAtomic.string("b")) == as_xdm(3)
    use_last = XdmMap.merge(first, second, duplicates="use-last")
    assert use_last.get(XdmAtomic.string("a")) == as_xdm(2)
    combine = XdmMap.merge(first, second, duplicates="combine")
    assert combine.get(XdmAtomic.string("a")) == (XdmAtomic.integer(1), XdmAtomic.integer(2))
    use_any = XdmMap.merge(first, second, duplicates="use-any")
    assert use_any.get(XdmAtomic.string("a")) == as_xdm(1)
    with pytest.raises(ValueError, match="duplicate"):
        XdmMap.merge(first, second, duplicates="reject")
    with pytest.raises(ValueError, match="duplicate"):
        XdmMap.merge(first, duplicates="unknown")
    with pytest.raises(TypeError, match="maps"):
        XdmMap.merge(first, {})  # type: ignore[arg-type]
    assert XdmMap.merge().entries == ()


def test_map_key_equality_uses_same_key_semantics():
    mapping = (
        XdmMap.empty()
        .put(XdmAtomic.integer(1), as_xdm("integer"))
        .put(XdmAtomic.double(1.0), as_xdm("double"))
        .put(XdmAtomic.string("a"), as_xdm("string"))
        .put(XdmAtomic.untyped_atomic("a"), as_xdm("untyped"))
    )
    assert mapping.size == 2
    assert mapping.get(XdmAtomic.integer(1)) == as_xdm("double")
    assert mapping.get(XdmAtomic.string("a")) == as_xdm("untyped")
    nan_key = XdmAtomic.double(float("nan"))
    mapping = mapping.put(nan_key, as_xdm("nan")).put(
        XdmAtomic.double(float("nan")), as_xdm("nan-2")
    )
    assert mapping.size == 3
    assert mapping.get(nan_key) == as_xdm("nan-2")


def test_map_key_equality_uses_xsd_value_space_variants():
    duration_key = _atomic(xsd.Duration("P1Y"))
    mapping = (
        XdmMap.empty()
        .put(duration_key, as_xdm("year"))
        .put(_atomic(xsd.Duration("P12M")), as_xdm("months"))
    )
    assert mapping.size == 1
    assert mapping.get(duration_key) == as_xdm("months")

    hex_key = _atomic(xsd.HexBinary("FF"))
    mapping = XdmMap.empty().put(hex_key, as_xdm(1)).put(_atomic(xsd.HexBinary("ff")), as_xdm(2))
    assert mapping.size == 1
    assert mapping.get(hex_key) == as_xdm(2)

    date_key = _atomic(xsd.Date("2000-01-01Z"))
    mapping = (
        XdmMap.empty()
        .put(date_key, as_xdm(1))
        .put(_atomic(xsd.Date("2000-01-01+00:00")), as_xdm(2))
    )
    assert mapping.size == 1
    assert mapping.get(date_key) == as_xdm(2)

    decimal_key = XdmAtomic.decimal("1.1")
    mapping = (
        XdmMap.empty()
        .put(decimal_key, as_xdm("decimal"))
        .put(XdmAtomic.double(1.1), as_xdm("double"))
    )
    assert mapping.size == 1
    assert mapping.get(decimal_key) == as_xdm("double")

    mapping = (
        XdmMap.empty()
        .put(XdmAtomic.decimal("0.1"), as_xdm(1))
        .put(XdmAtomic.decimal("0.10000000000000001"), as_xdm(2))
    )
    assert mapping.size == 2


def test_map_constructor_validation_and_identity_semantics():
    key = XdmAtomic.string("a")
    first = XdmMap([(key, as_xdm(1))])
    second = XdmMap([(key, as_xdm(1))])
    assert first == first
    assert first != second
    with pytest.raises(ValueError, match="duplicate"):
        XdmMap([(key, as_xdm(1)), (XdmAtomic.string("a"), as_xdm(2))])
    with pytest.raises(TypeError, match="atomic"):
        XdmMap([("a", as_xdm(1))])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        XdmMap([("a",)])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="None"):
        XdmMap([(key, None)])


def test_map_is_not_iterable_and_values_are_frozen():
    mapping = XdmMap.empty().put(XdmAtomic.string("a"), as_xdm(1))
    with pytest.raises(TypeError):
        iter(mapping)
    with pytest.raises(FrozenInstanceError):
        mapping._entries = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Arrays


def test_array_members_are_sequences_and_get_is_one_based():
    array = XdmArray((as_xdm(1), as_xdm("two"), XdmAtomic.integer(3), "four"))
    assert array.size == 4
    assert len(array) == 4
    assert array.get(1) == as_xdm(1)
    assert array.get(4) == as_xdm("four")
    assert array.members == (as_xdm(1), as_xdm("two"), (XdmAtomic.integer(3),), as_xdm("four"))


def test_array_bounds_and_index_types():
    array = XdmArray((as_xdm(1),))
    for index in (0, -1, 2):
        with pytest.raises(IndexError, match="FOAY0001"):
            array.get(index)
    for index in (True, 1.0, "1"):
        with pytest.raises(TypeError, match="integer"):
            array.get(index)  # type: ignore[arg-type]


def test_array_rejects_a_string_as_its_member_container():
    for value in ("abc", b"abc", bytearray(b"abc"), memoryview(b"abc")):
        with pytest.raises(TypeError, match="members"):
            XdmArray(value)  # type: ignore[arg-type]


def test_array_identity_and_immutability():
    first = XdmArray(())
    second = XdmArray(())
    assert first == first
    assert first != second
    with pytest.raises(TypeError):
        iter(first)
    with pytest.raises(FrozenInstanceError):
        first._members = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Function items


def _empty(_arguments: Any, _environment: Any, _closure: Any):
    return ()


def test_function_preserves_arity_signature_closure_and_name():
    signature = XdmFunctionSignature(
        (XdmSequenceType("integer", "one"),),
        XdmSequenceType("integer", "one"),
    )
    function = XdmFunction(
        _empty,
        signature=signature,
        closure=("captured",),
        name="local:f",
    )
    assert function.arity == (1, 1)
    assert function.signature is signature
    assert function.closure == ("captured",)
    assert function.name == "local:f"
    assert not callable(function)


def test_function_invoke_passes_arguments_environment_and_closure():
    seen: dict[str, Any] = {}

    def implementation(arguments, environment, closure):
        seen["arguments"] = arguments
        seen["environment"] = environment
        seen["closure"] = closure
        return arguments[0]

    environment = {"focus": XdmAtomic.integer(1)}
    function = XdmFunction(implementation, arity=1, closure=("captured",))
    assert function.invoke([as_xdm(7)], environment) == as_xdm(7)
    assert seen["arguments"] == (as_xdm(7),)
    assert seen["environment"] is environment
    assert seen["closure"] == ("captured",)


def test_function_arity_errors():
    function = XdmFunction(_empty, arity=2)
    with pytest.raises(TypeError, match="arity"):
        function.invoke([as_xdm(1)], None)
    with pytest.raises(TypeError, match="arity"):
        function.invoke([as_xdm(1)] * 3, None)
    variadic = XdmFunction(_empty, arity=(1, None))
    assert variadic.invoke([as_xdm(1)], None) == ()
    assert variadic.invoke([as_xdm(1)] * 5, None) == ()
    bounded = XdmFunction(_empty, arity=(1, 2))
    assert bounded.invoke([as_xdm(1)] * 2, None) == ()
    with pytest.raises(TypeError, match="arity"):
        bounded.invoke([as_xdm(1)] * 3, None)


def test_function_requires_arity_or_signature():
    with pytest.raises(TypeError, match="arity"):
        XdmFunction(_empty)
    signature = XdmFunctionSignature((XdmSequenceType("item()", "zero-or-more"),))
    function = XdmFunction(_empty, signature=signature)
    assert function.arity == (1, 1)


def test_function_cannot_be_invoked_with_a_string_or_non_iterable():
    function = XdmFunction(_empty, arity=0)
    with pytest.raises(TypeError, match="arguments"):
        function.invoke("abc", None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="arguments"):
        function.invoke(1, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "arity",
    [
        True,
        -1,
        (1,),
        (1, 2, 3),
        ("1", 2),
        (-1, 2),
        (1, "2"),
        (2, 1),
    ],
)
def test_function_arity_validation_errors(arity):
    with pytest.raises(ValueError, match="arity"):
        XdmFunction(_empty, arity=arity)  # type: ignore[arg-type]


def test_function_signature_arity_mismatch_is_rejected():
    signature = XdmFunctionSignature((XdmSequenceType("item()", "one"),))
    with pytest.raises(ValueError, match="signature"):
        XdmFunction(_empty, arity=2, signature=signature)


def test_function_signature_validation():
    with pytest.raises(ValueError, match="occurrence"):
        XdmSequenceType("integer", "sometimes")
    with pytest.raises(ValueError, match="type name"):
        XdmSequenceType("", "one")
    with pytest.raises(TypeError, match="parameters"):
        XdmFunctionSignature([object()])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="return"):
        XdmFunctionSignature((), return_type=object())  # type: ignore[arg-type]
    parameters = [XdmSequenceType("integer", "one")]
    signature = XdmFunctionSignature(parameters)  # type: ignore[arg-type]
    assert signature.parameters == (XdmSequenceType("integer", "one"),)


def test_function_signature_coerces_arguments():
    seen: list[tuple[Any, ...]] = []

    def implementation(arguments, environment, closure):
        seen.append(arguments)
        return arguments

    signature = XdmFunctionSignature(
        (
            XdmSequenceType("integer", "one"),
            XdmSequenceType("double", "one"),
            XdmSequenceType("integer", "one"),
            XdmSequenceType("string", "one"),
            XdmSequenceType("double", "one"),
            XdmSequenceType("item()", "one"),
            XdmSequenceType("map(*)", "one"),
            XdmSequenceType("array(*)", "one"),
            XdmSequenceType("function(*)", "one"),
        )
    )
    mapping = XdmMap.empty()
    array = XdmArray(())
    function = XdmFunction(_empty, arity=0)
    result = XdmFunction(implementation, signature=signature).invoke(
        [
            as_xdm(ep.UntypedAtomic("4")),
            as_xdm(1),
            as_xdm(xsd.Int("3")),
            as_xdm(xsd.AnyURI("urn:x")),
            as_xdm(xsd.Float("1.5")),
            as_xdm(mapping),
            as_xdm(mapping),
            as_xdm(array),
            as_xdm(function),
        ],
        None,
    )
    assert isinstance(result[0], XdmAtomic)
    assert result[0].type_name == f"{{{XSD}}}integer"
    assert result[0].datatype is xsd.Integer
    assert isinstance(result[1], XdmAtomic)
    assert result[1].type_name == f"{{{XSD}}}double"
    assert result[1].datatype is xsd.Double
    assert isinstance(result[2], XdmAtomic)
    assert result[2].type_name == f"{{{XSD}}}int"
    assert result[2].datatype is xsd.Int
    assert isinstance(result[3], XdmAtomic)
    assert result[3].type_name == f"{{{XSD}}}string"
    assert result[3].datatype is xsd.String
    assert isinstance(result[4], XdmAtomic)
    assert result[4].type_name == f"{{{XSD}}}double"
    assert result[4].datatype is xsd.Double
    assert result[5] == mapping
    assert result[6] == mapping
    assert result[7] == array
    assert result[8] == function
    assert seen


@pytest.mark.parametrize(
    ("value", "expected_type"),
    [
        (xsd.Int("7"), "long"),
        (xsd.Short("7"), "int"),
        (xsd.Language("en"), "token"),
        (xsd.NCName("a"), "Name"),
        (xsd.DateTimeStamp("2000-01-01T00:00:00Z"), "dateTime"),
        (xsd.YearMonthDuration("P1Y"), "duration"),
    ],
)
def test_function_accepts_xsd_subtype_substitutions(value, expected_type):
    signature = XdmFunctionSignature((XdmSequenceType(expected_type, "one"),))
    function = XdmFunction(lambda arguments, environment, closure: arguments, signature=signature)
    result = function.invoke([as_xdm(value)], None)
    asserted = result[0]
    assert isinstance(asserted, XdmAtomic)
    assert asserted.type_name == f"{{{XSD}}}{type(value).name}"
    assert asserted.datatype is type(value)
    assert asserted.value is value


@pytest.mark.parametrize(
    ("expected_type", "expected_type_name", "expected_datatype"),
    [
        ("long", f"{{{XSD}}}int", None),
        ("decimal", f"{{{XSD}}}decimal", xsd.Decimal),
    ],
)
def test_function_substitution_uses_type_name_when_datatype_is_none(
    expected_type, expected_type_name, expected_datatype
):
    direct = XdmAtomic("1", f"{{{XSD}}}int")
    signature = XdmFunctionSignature((XdmSequenceType(expected_type, "one"),))
    function = XdmFunction(lambda arguments, environment, closure: arguments, signature=signature)
    result = function.invoke([as_xdm(direct)], None)
    asserted = result[0]
    assert isinstance(asserted, XdmAtomic)
    assert asserted.type_name == expected_type_name
    assert asserted.datatype is expected_datatype


@pytest.mark.parametrize(
    ("value", "expected_type", "expected_type_name", "expected_datatype"),
    [
        (XdmAtomic.integer(1), "decimal", f"{{{XSD}}}decimal", xsd.Decimal),
        (XdmAtomic.integer(1), "float", f"{{{XSD}}}float", xsd.Float),
        (XdmAtomic.integer(1), "double", f"{{{XSD}}}double", xsd.Double),
        (XdmAtomic.decimal("1.5"), "float", f"{{{XSD}}}float", xsd.Float),
        (XdmAtomic.decimal("1.5"), "double", f"{{{XSD}}}double", xsd.Double),
        (XdmAtomic.float(1.5), "double", f"{{{XSD}}}double", xsd.Double),
    ],
)
def test_function_numeric_promotion_converts_to_the_required_type(
    value, expected_type, expected_type_name, expected_datatype
):
    signature = XdmFunctionSignature((XdmSequenceType(expected_type, "one"),))
    function = XdmFunction(lambda arguments, environment, closure: arguments, signature=signature)
    result = function.invoke([as_xdm(value)], None)
    asserted = result[0]
    assert isinstance(asserted, XdmAtomic)
    assert asserted.type_name == expected_type_name
    assert asserted.datatype is expected_datatype


@pytest.mark.parametrize(
    ("value", "expected_type"),
    [
        (XdmAtomic.typed(xsd.Date("2000-01-01"), xsd.Date), "string"),
        (XdmAtomic.typed(xsd.QName("p:a"), xsd.QName), "string"),
        (XdmAtomic.typed(xsd.Base64Binary("YWJj"), xsd.Base64Binary), "string"),
        (XdmAtomic.typed(xsd.Duration("P1Y"), xsd.Duration), "string"),
        (XdmAtomic.boolean(True), "integer"),
        (XdmAtomic.double(1.5), "float"),
        (XdmAtomic.typed(xsd.HexBinary("FF"), xsd.HexBinary), "base64Binary"),
        (XdmAtomic.typed(xsd.Int("1"), xsd.Int), "short"),
    ],
)
def test_function_rejects_invalid_atomic_substitutions(value, expected_type):
    signature = XdmFunctionSignature((XdmSequenceType(expected_type, "one"),))
    function = XdmFunction(lambda arguments, environment, closure: arguments, signature=signature)
    with pytest.raises(TypeError, match="coerce"):
        function.invoke([as_xdm(value)], None)


def test_function_signature_occurrence_and_type_errors():
    one = XdmFunctionSignature((XdmSequenceType("integer", "one"),))
    function = XdmFunction(_empty, signature=one)
    with pytest.raises(TypeError, match="one"):
        function.invoke([()], None)
    with pytest.raises(TypeError, match="zero-or-one"):
        XdmFunction(
            _empty, signature=XdmFunctionSignature((XdmSequenceType("integer", "zero-or-one"),))
        ).invoke([(as_xdm(1), as_xdm(2))], None)
    with pytest.raises(TypeError, match="one-or-more"):
        XdmFunction(
            _empty, signature=XdmFunctionSignature((XdmSequenceType("integer", "one-or-more"),))
        ).invoke([()], None)
    with pytest.raises(TypeError, match="coerce"):
        function.invoke([as_xdm("not-an-integer")], None)
    with pytest.raises(TypeError, match="expected xs:integer"):
        function.invoke([as_xdm(XdmMap.empty())], None)
    with pytest.raises(TypeError, match="coerce"):
        XdmFunction(
            _empty, signature=XdmFunctionSignature((XdmSequenceType("noSuchType", "one"),))
        ).invoke([as_xdm(1)], None)
    with pytest.raises(TypeError, match="map"):
        XdmFunction(
            _empty, signature=XdmFunctionSignature((XdmSequenceType("map(*)", "one"),))
        ).invoke([as_xdm(1)], None)
    with pytest.raises(TypeError, match="array"):
        XdmFunction(
            _empty, signature=XdmFunctionSignature((XdmSequenceType("array(*)", "one"),))
        ).invoke([as_xdm(1)], None)
    with pytest.raises(TypeError, match="function"):
        XdmFunction(
            _empty, signature=XdmFunctionSignature((XdmSequenceType("function(*)", "one"),))
        ).invoke([as_xdm(1)], None)
    with pytest.raises(TypeError, match="node"):
        XdmFunction(
            _empty, signature=XdmFunctionSignature((XdmSequenceType("node()", "one"),))
        ).invoke([as_xdm(1)], None)


def test_function_accepts_elementpath_any_uri_for_a_string_parameter():
    signature = XdmFunctionSignature((XdmSequenceType("string", "one"),))
    function = XdmFunction(lambda arguments, environment, closure: arguments, signature=signature)
    result = function.invoke([as_xdm(ep.AnyURI("urn:x"))], None)
    asserted = result[0]
    assert isinstance(asserted, XdmAtomic)
    assert asserted.type_name == f"{{{XSD}}}string"
    assert asserted.datatype is xsd.String
    assert asserted.lexical == "urn:x"


@pytest.mark.parametrize("type_name", ["anyAtomicType", "anySimpleType"])
def test_function_accepts_any_atomic_sequence_type(type_name):
    signature = XdmFunctionSignature((XdmSequenceType(type_name, "one"),))
    function = XdmFunction(lambda arguments, environment, closure: arguments, signature=signature)
    assert function.invoke([as_xdm(1)], None) == as_xdm(1)
    with pytest.raises(TypeError, match="atomic"):
        function.invoke([as_xdm(XdmMap.empty())], None)


def test_function_signature_coerces_and_checks_the_return_value():
    signature = XdmFunctionSignature(
        (XdmSequenceType("item()", "zero-or-more"),),
        XdmSequenceType("integer", "one"),
    )
    function = XdmFunction(
        lambda arguments, environment, closure: as_xdm(ep.UntypedAtomic("5")),
        signature=signature,
    )
    result = function.invoke([()], None)
    asserted = result[0]
    assert isinstance(asserted, XdmAtomic)
    assert asserted.type_name == f"{{{XSD}}}integer"
    assert asserted.datatype is xsd.Integer
    raw_item = XdmFunction(
        lambda arguments, environment, closure: XdmAtomic.integer(6), signature=signature
    )
    raw_result = raw_item.invoke([()], None)[0]
    assert isinstance(raw_result, XdmAtomic)
    assert raw_result.type_name == f"{{{XSD}}}integer"
    assert raw_result.datatype is xsd.Integer
    wrong = XdmFunction(
        lambda arguments, environment, closure: "not-an-integer", signature=signature
    )
    with pytest.raises(TypeError, match="coerce"):
        wrong.invoke([()], None)
    none = XdmFunction(lambda arguments, environment, closure: None, arity=0)
    with pytest.raises(TypeError, match="None"):
        none.invoke([], None)


def test_function_rejects_a_non_string_atomic_for_a_string_return():
    signature = XdmFunctionSignature(
        (XdmSequenceType("item()", "one"),),
        XdmSequenceType("string", "one"),
    )
    date = XdmAtomic.typed(xsd.Date("2000-01-01"), xsd.Date)
    function = XdmFunction(
        lambda arguments, environment, closure: as_xdm(date),
        signature=signature,
    )
    with pytest.raises(TypeError, match="coerce"):
        function.invoke([as_xdm(1)], None)


def test_function_identity_and_immutability():
    first = XdmFunction(_empty, arity=0)
    second = XdmFunction(_empty, arity=0)
    assert first == first
    assert first != second
    with pytest.raises(FrozenInstanceError):
        first.closure = "changed"  # type: ignore[misc]


def test_function_rejects_a_non_callable_implementation():
    with pytest.raises(TypeError, match="callable"):
        XdmFunction(object(), arity=0)  # type: ignore[arg-type]


def test_function_signature_must_be_the_expected_type():
    with pytest.raises(TypeError, match="signature"):
        XdmFunction(_empty, arity=0, signature=object())  # type: ignore[arg-type]
