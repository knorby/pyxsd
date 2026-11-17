"""Datatype protocols, the built-in library, and the XSD 1.0 adapter.

RELAX NG delegates lexical acceptance and value equality to datatype
libraries.  The built-in library has only ``string`` and ``token``; the XSD
library adapts pyxsd's built-in datatype classes with XSD 1.0 semantics.  A
compiled datatype keeps the schema-side context beside its immutable
parameters so a ``value`` pattern can compare it against an instance-side
context later.
"""

from __future__ import annotations

import decimal
import re
from dataclasses import FrozenInstanceError

import pytest

from pyxsd.relaxng.datatypes import (
    BUILTIN_FINGERPRINT,
    BUILTIN_LIBRARY,
    XSD_FINGERPRINT,
    XSD_LIBRARY,
    BuiltinDatatypeFactory,
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    DatatypeLibraryRegistry,
    DatatypeParameter,
    DatatypeParameterError,
    DatatypeRegistrationError,
    UnsupportedDatatypeError,
    UnsupportedDatatypeOperation,
    XsdDatatypeFactory,
    resolve_datatype_selector,
)
from pyxsd.relaxng.patterns import Data, Text, Value, choice
from pyxsd.xsd_data_types import AnyURI, QName, qname_context

BUILTIN = BuiltinDatatypeFactory()
XSD = XsdDatatypeFactory()
EMPTY = DatatypeContext(None, {})

XSD_10_TYPES = (
    "ENTITIES",
    "ENTITY",
    "ID",
    "IDREF",
    "IDREFS",
    "NCName",
    "NMTOKEN",
    "NMTOKENS",
    "NOTATION",
    "Name",
    "QName",
    "anySimpleType",
    "anyURI",
    "base64Binary",
    "boolean",
    "byte",
    "date",
    "dateTime",
    "decimal",
    "double",
    "duration",
    "float",
    "gDay",
    "gMonth",
    "gMonthDay",
    "gYear",
    "gYearMonth",
    "hexBinary",
    "int",
    "integer",
    "language",
    "long",
    "negativeInteger",
    "nonNegativeInteger",
    "nonPositiveInteger",
    "normalizedString",
    "positiveInteger",
    "short",
    "string",
    "time",
    "token",
    "unsignedByte",
    "unsignedInt",
    "unsignedLong",
    "unsignedShort",
)


def parameter(name: str, value: str) -> DatatypeParameter:
    """A terse constructor for a datatype parameter."""
    return DatatypeParameter(name, value)


def xsd(type_name: str, *parameters: tuple[str, str]) -> Datatype:
    """Create an XSD datatype with string pairs as parameters."""
    return XSD.create(
        type_name,
        tuple(parameter(name, value) for name, value in parameters),
        EMPTY,
    )


class OpaqueDatatype(Datatype):
    """A minimal custom datatype with validation but no codecs."""

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        return lexical == "ok"

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        return left == right


class OpaqueFactory(DatatypeFactory):
    """A custom library factory returning :class:`OpaqueDatatype`."""

    def create(
        self,
        type_name: str,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        del type_name, parameters, context
        return OpaqueDatatype()


class TestDatatypeContext:
    def test_context_is_frozen(self):
        context = DatatypeContext("urn:base", {"p": "urn:p"})
        with pytest.raises(FrozenInstanceError):
            context.base_uri = "urn:other"  # type: ignore[misc]

    def test_namespaces_are_an_immutable_mapping(self):
        context = DatatypeContext(None, {"p": "urn:p"})
        assert context.namespaces == {"p": "urn:p"}
        assert dict(context.namespaces) == {"p": "urn:p"}
        with pytest.raises(TypeError):
            context.namespaces["q"] = "urn:q"  # type: ignore[index]

    def test_mapping_equality_ignores_insertion_order(self):
        first = DatatypeContext(None, {"a": "1", "b": "2"})
        second = DatatypeContext(None, {"b": "2", "a": "1"})
        assert first == second
        assert hash(first) == hash(second)
        assert repr(first) == repr(second)

    def test_namespace_mapping_does_not_equal_an_unrelated_object(self):
        context = DatatypeContext(None, {"a": "1"})
        assert context.namespaces != object()
        assert object() != context.namespaces

    def test_namespace_mapping_is_copied_from_the_input(self):
        source = {"p": "urn:p"}
        context = DatatypeContext(None, source)
        source["q"] = "urn:q"
        assert dict(context.namespaces) == {"p": "urn:p"}

    def test_context_equality_includes_the_base_uri(self):
        assert DatatypeContext("urn:a", {}) != DatatypeContext("urn:b", {})
        assert DatatypeContext(None, {}) != DatatypeContext("", {})


class TestDatatypeParameter:
    def test_parameter_is_frozen_and_hashable(self):
        first = DatatypeParameter("length", "2")
        second = DatatypeParameter("length", "2")
        assert first == second
        assert hash(first) == hash(second)
        with pytest.raises(FrozenInstanceError):
            first.value = "3"  # type: ignore[misc]

    def test_parameter_rejects_an_empty_or_non_string_name(self):
        with pytest.raises(ValueError):
            DatatypeParameter("", "2")
        with pytest.raises(ValueError):
            DatatypeParameter(1, "2")  # type: ignore[arg-type]

    def test_parameter_rejects_a_non_string_value(self):
        with pytest.raises(ValueError):
            DatatypeParameter("length", 2)  # type: ignore[arg-type]


class TestBuiltinLibrary:
    def test_string_preserves_characters(self):
        datatype = BUILTIN.create("string", (), EMPTY)
        lexical = " leading\tand\u00a0non-breaking "
        assert datatype.allows(lexical, EMPTY)
        assert datatype.decode(lexical, EMPTY) == lexical
        assert datatype.encode(lexical, EMPTY) == lexical
        assert not datatype.equal("a\tb", EMPTY, "a b", EMPTY)

    def test_token_equality_normalizes_only_xml_whitespace(self):
        datatype = BUILTIN.create("token", (), EMPTY)
        assert datatype.allows(" anything\tgoes ", EMPTY)
        assert datatype.equal(" a\tb\n", EMPTY, "a b", EMPTY)
        assert not datatype.equal("a\u00a0b", EMPTY, "a b", EMPTY)
        assert datatype.decode(" a\tb ", EMPTY) == "a b"
        assert datatype.encode(" a\tb ", EMPTY) == "a b"

    def test_builtin_datatypes_ignore_their_context(self):
        first = BUILTIN.create("string", (), EMPTY)
        second = BUILTIN.create("string", (), DatatypeContext("urn:b", {"p": "urn:p"}))
        assert first == second
        assert hash(first) == hash(second)
        assert repr(first) == repr(second)
        assert first.context.base_uri is None
        assert second.context.base_uri == "urn:b"

    def test_builtin_library_rejects_parameters(self):
        with pytest.raises(DatatypeParameterError):
            BUILTIN.create("string", (parameter("length", "1"),), EMPTY)

    def test_builtin_library_rejects_unknown_types(self):
        with pytest.raises(UnsupportedDatatypeError):
            BUILTIN.create("integer", (), EMPTY)

    def test_builtin_datatype_repr_is_deterministic(self):
        first = BUILTIN.create("token", (), EMPTY)
        second = BUILTIN.create("token", (), EMPTY)
        assert repr(first) == repr(second)
        assert "0x" not in repr(first)

    def test_datatype_is_frozen(self):
        datatype = BUILTIN.create("string", (), EMPTY)
        with pytest.raises(FrozenInstanceError):
            datatype.type_name = "token"  # type: ignore[misc]

    def test_factory_repr_is_deterministic(self):
        assert repr(BUILTIN) == "BuiltinDatatypeFactory()"
        assert repr(XSD) == "XsdDatatypeFactory()"


class TestSelectorsAndRegistry:
    def test_string_selector_names_the_builtin_library(self):
        assert resolve_datatype_selector("string") == (BUILTIN_LIBRARY, "string")

    def test_xsd_shorthand_uses_the_xsd_library(self):
        assert resolve_datatype_selector("xsd:integer") == (XSD_LIBRARY, "integer")

    def test_tuple_selector_is_the_library_and_type_name(self):
        selector = (XSD_LIBRARY, "integer")
        assert resolve_datatype_selector(selector) == selector

    def test_shorthand_and_tuple_selectors_create_equal_datatypes(self):
        registry = DatatypeLibraryRegistry()
        shorthand = registry.create("xsd:integer", (), EMPTY)
        explicit = registry.create((XSD_LIBRARY, "integer"), (), EMPTY)
        assert shorthand == explicit

    def test_bad_selectors_are_rejected(self):
        with pytest.raises(UnsupportedDatatypeError):
            resolve_datatype_selector("xsd:")
        with pytest.raises(UnsupportedDatatypeError):
            resolve_datatype_selector("other:integer")
        with pytest.raises(TypeError):
            resolve_datatype_selector(3)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            resolve_datatype_selector(("urn:library",))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            resolve_datatype_selector((XSD_LIBRARY, 3))  # type: ignore[arg-type]

    def test_registry_has_fixed_builtin_identities(self):
        registry = DatatypeLibraryRegistry()
        assert registry.library_uris() == (BUILTIN_LIBRARY, XSD_LIBRARY)
        assert registry.registration(BUILTIN_LIBRARY).fingerprint == BUILTIN_FINGERPRINT
        assert registry.registration(XSD_LIBRARY).fingerprint == XSD_FINGERPRINT
        assert XSD_FINGERPRINT == "pyxsd.relaxng.xsd-1.0/3"
        assert isinstance(registry.registration(BUILTIN_LIBRARY).factory, BuiltinDatatypeFactory)
        assert isinstance(registry.registration(XSD_LIBRARY).factory, XsdDatatypeFactory)

    def test_registry_resolves_selectors(self):
        registry = DatatypeLibraryRegistry()
        assert registry.resolve("token") == (BUILTIN_LIBRARY, "token")
        assert registry.resolve("xsd:date") == (XSD_LIBRARY, "date")
        assert registry.resolve((XSD_LIBRARY, "date")) == (XSD_LIBRARY, "date")

    def test_unknown_library_and_type_are_unsupported(self):
        registry = DatatypeLibraryRegistry()
        with pytest.raises(UnsupportedDatatypeError):
            registry.registration("urn:unknown")
        with pytest.raises(UnsupportedDatatypeError):
            registry.create(("urn:unknown", "thing"), (), EMPTY)
        with pytest.raises(UnsupportedDatatypeError):
            registry.create("xsd:unknown", (), EMPTY)

    def test_custom_registration_is_used(self):
        registry = DatatypeLibraryRegistry()
        registration = DatatypeLibraryRegistration(OpaqueFactory(), "opaque/1")
        registry.register("urn:opaque", registration)
        datatype = registry.create(("urn:opaque", "thing"), (), EMPTY)
        assert datatype.allows("ok", EMPTY)
        assert not datatype.allows("no", EMPTY)
        assert datatype.equal("a", EMPTY, "a", EMPTY)
        with pytest.raises(UnsupportedDatatypeOperation):
            datatype.decode("ok", EMPTY)
        with pytest.raises(UnsupportedDatatypeOperation):
            datatype.encode("ok", EMPTY)
        assert registry.library_uris() == (BUILTIN_LIBRARY, XSD_LIBRARY, "urn:opaque")
        assert registry.registration("urn:opaque") == registration

    def test_custom_datatype_without_codecs_reports_absent_operations(self):
        datatype = OpaqueDatatype()
        with pytest.raises(UnsupportedDatatypeOperation):
            datatype.decode("ok", EMPTY)
        with pytest.raises(UnsupportedDatatypeOperation):
            datatype.encode("ok", EMPTY)

    def test_a_custom_datatype_may_support_only_one_codec(self):
        class Decoder(OpaqueDatatype):
            def decode(self, lexical: str, context: DatatypeContext) -> str:
                del context
                return lexical

        datatype = Decoder()
        assert datatype.decode("ok", EMPTY) == "ok"
        with pytest.raises(UnsupportedDatatypeOperation):
            datatype.encode("ok", EMPTY)

    def test_blank_fingerprints_are_rejected(self):
        for fingerprint in ("", "   "):
            registry = DatatypeLibraryRegistry()
            registration = DatatypeLibraryRegistration(OpaqueFactory(), fingerprint)
            with pytest.raises(DatatypeRegistrationError):
                registry.register("urn:custom", registration)
            assert "urn:custom" not in registry.library_uris()

    def test_process_dependent_fingerprints_are_rejected(self):
        for fingerprint in (repr(object()), "<factory at 0xdeadbeef>"):
            registry = DatatypeLibraryRegistry()
            registration = DatatypeLibraryRegistration(OpaqueFactory(), fingerprint)
            with pytest.raises(DatatypeRegistrationError):
                registry.register("urn:custom", registration)

    def test_non_string_fingerprints_are_rejected(self):
        registry = DatatypeLibraryRegistry()
        registration = DatatypeLibraryRegistration(OpaqueFactory(), 1)  # type: ignore[arg-type]
        with pytest.raises(DatatypeRegistrationError):
            registry.register("urn:custom", registration)

    def test_registration_requires_a_fingerprint(self):
        with pytest.raises(TypeError):
            DatatypeLibraryRegistration(OpaqueFactory())  # type: ignore[call-arg]

    def test_registration_rejects_duplicates_and_builtin_overrides(self):
        registry = DatatypeLibraryRegistry()
        registry.register("urn:custom", DatatypeLibraryRegistration(OpaqueFactory(), "cfg/1"))
        with pytest.raises(DatatypeRegistrationError):
            registry.register("urn:custom", DatatypeLibraryRegistration(OpaqueFactory(), "cfg/2"))
        with pytest.raises(DatatypeRegistrationError):
            registry.register(BUILTIN_LIBRARY, DatatypeLibraryRegistration(OpaqueFactory(), "cfg"))
        with pytest.raises(DatatypeRegistrationError):
            registry.register(XSD_LIBRARY, DatatypeLibraryRegistration(OpaqueFactory(), "cfg"))

    def test_registration_requires_a_factory(self):
        registry = DatatypeLibraryRegistry()
        registration = DatatypeLibraryRegistration(object(), "cfg/1")  # type: ignore[arg-type]
        with pytest.raises(DatatypeRegistrationError):
            registry.register("urn:custom", registration)

    def test_registration_requires_a_registration_object(self):
        registry = DatatypeLibraryRegistry()
        with pytest.raises(TypeError):
            registry.register("urn:custom", OpaqueFactory())  # type: ignore[arg-type]

    def test_registration_requires_a_string_uri(self):
        registry = DatatypeLibraryRegistry()
        registration = DatatypeLibraryRegistration(OpaqueFactory(), "cfg/1")
        with pytest.raises(TypeError):
            registry.register(3, registration)  # type: ignore[arg-type]


class TestXsdDecoding:
    def test_integers_decode_to_python_ints_and_encode_explicitly(self):
        datatype = xsd("integer")
        assert datatype.allows(" 42 ", EMPTY)
        assert not datatype.allows("42.0", EMPTY)
        assert datatype.decode("+042", EMPTY) == 42
        assert datatype.encode(42, EMPTY) == "42"
        with pytest.raises(TypeError):
            datatype.encode(True, EMPTY)
        with pytest.raises(TypeError):
            datatype.encode("42", EMPTY)

    def test_dates_decode_to_typed_lexical_values(self):
        datatype = xsd("date")
        decoded = datatype.decode("2000-02-29Z", EMPTY)
        assert decoded == "2000-02-29Z"
        assert decoded.lexical() == "2000-02-29Z"
        assert datatype.encode(decoded, EMPTY) == "2000-02-29Z"
        with pytest.raises(TypeError):
            datatype.decode("2001-02-29", EMPTY)
        with pytest.raises(TypeError):
            datatype.encode("not-a-date", EMPTY)

    def test_booleans_decode_and_encode_explicitly(self):
        datatype = xsd("boolean")
        decoded = datatype.decode("1", EMPTY)
        assert decoded is True
        assert isinstance(decoded, bool)
        assert datatype.decode("false", EMPTY) is False
        assert datatype.encode(True, EMPTY) == "true"
        assert datatype.encode(0, EMPTY) == "false"
        assert datatype.encode(datatype.decode("false", EMPTY), EMPTY) == "false"
        with pytest.raises(TypeError):
            datatype.encode("true", EMPTY)
        with pytest.raises(TypeError):
            datatype.encode(2, EMPTY)

    def test_binary_types_decode_to_bytes_and_encode_canonically(self):
        hexadecimal = xsd("hexBinary")
        assert hexadecimal.decode("00ff", EMPTY) == b"\x00\xff"
        assert hexadecimal.encode(b"\x00\xff", EMPTY) == "00FF"
        assert hexadecimal.encode(bytearray(b"\x00\xff"), EMPTY) == "00FF"
        with pytest.raises(TypeError):
            hexadecimal.encode("00ff", EMPTY)

        base64 = xsd("base64Binary")
        assert base64.decode("A P8=", EMPTY) == b"\x00\xff"
        assert base64.encode(b"\x00\xff", EMPTY) == "AP8="
        with pytest.raises(TypeError):
            base64.encode("AP8=", EMPTY)

    def test_xsd_white_space_facets_follow_the_builtin_types(self):
        any_simple = xsd("anySimpleType")
        assert any_simple.decode(" a\tb ", EMPTY) == " a\tb "
        assert any_simple.encode(" a\tb ", EMPTY) == " a\tb "
        assert not any_simple.equal("a\tb", EMPTY, "a b", EMPTY)

        normalized = xsd("normalizedString")
        assert normalized.equal("a\tb", EMPTY, "a b", EMPTY)
        assert normalized.decode("a\nb", EMPTY) == "a b"

        token = xsd("token")
        assert token.equal(" a\tb ", EMPTY, "a b", EMPTY)

    def test_decimal_and_float_encoding(self):
        decimal_type = xsd("decimal")
        assert decimal_type.decode("1.50", EMPTY) == decimal_type.decode("1.5", EMPTY)
        assert decimal_type.encode(1, EMPTY) == "1"
        assert decimal_type.encode(decimal.Decimal("1.5"), EMPTY) == "1.5"
        with pytest.raises(TypeError):
            decimal_type.encode(1.5, EMPTY)
        with pytest.raises(TypeError):
            decimal_type.encode(True, EMPTY)
        with pytest.raises(TypeError):
            decimal_type.encode("1.5", EMPTY)

        float_type = xsd("double")
        assert float_type.decode("INF", EMPTY) == float("inf")
        assert float_type.encode(float("inf"), EMPTY) == "INF"
        assert float_type.encode(1, EMPTY) == "1.0"
        with pytest.raises(TypeError):
            float_type.encode(False, EMPTY)
        with pytest.raises(TypeError):
            float_type.encode("1.0", EMPTY)

    def test_text_encoding_requires_a_string(self):
        with pytest.raises(TypeError):
            xsd("string").encode(3, EMPTY)

    def test_decode_enforces_the_parameterized_value_space(self):
        bounded = xsd("integer", ("minInclusive", "10"))
        assert bounded.allows("10", EMPTY)
        assert not bounded.allows("9", EMPTY)
        with pytest.raises(TypeError):
            bounded.decode("9", EMPTY)

    def test_encode_rejects_a_value_outside_the_parameterized_value_space(self):
        bounded = xsd("integer", ("minInclusive", "10"))
        assert bounded.encode(10, EMPTY) == "10"
        with pytest.raises(TypeError):
            bounded.encode(9, EMPTY)


class TestXsd10AnyUri:
    def test_xsd_1_0_rejects_values_only_xsd_1_1_accepts(self):
        datatype = xsd("anyURI")
        invalid = (
            "a%zz",
            "a#b#c",
            "http://[::1",
            "1:2",
            "a[b]",
            "http://exa%zz",
            "a?b%zz",
            "a#b%zz",
            "http://[v1.x]",
            "http://[::1]:x",
            "http://[::1%25eth0]",
            "http://",
            "//",
            "http:///path",
            "///path",
            "a://",
        )
        for lexical in invalid:
            assert not datatype.allows(lexical, EMPTY)
        # The shared class is XSD 1.1: it accepts every XML character sequence.
        for lexical in invalid[:6]:
            assert AnyURI(lexical) == lexical

    def test_xsd_1_0_accepts_escaped_and_unescaped_forms(self):
        datatype = xsd("anyURI")
        for lexical in (
            "a b",
            "a\tb",
            "a\\b",
            "a|b",
            "\u00e9",
            "http://example.com/\u00e9dition",
            "http://host",
            "//host",
            "http://[::1]/x",
            "http://[::1]:8080",
            "http://[::1]:",
            "http://user@[::1]",
            "http://[::ffff:192.168.1.1]",
            "?q",
            "#frag",
            "",
            "a/b:c",
            "./a:b",
            "a:b",
        ):
            assert datatype.allows(lexical, EMPTY)

    def test_anyuri_rejects_non_xml_characters(self):
        datatype = xsd("anyURI")
        assert not datatype.allows("a\x00b", EMPTY)
        assert not datatype.allows("a\ud800b", EMPTY)

    def test_anyuri_decoding_and_encoding_validate_the_lexical(self):
        datatype = xsd("anyURI")
        assert datatype.decode("a b", EMPTY) == "a b"
        assert datatype.encode("a b", EMPTY) == "a b"
        with pytest.raises(TypeError):
            datatype.decode("a%zz", EMPTY)
        with pytest.raises(TypeError):
            datatype.encode("a%zz", EMPTY)

    def test_anyuri_equality_is_lexical_identity(self):
        datatype = xsd("anyURI")
        assert datatype.equal("a b", EMPTY, "a b", EMPTY)
        assert not datatype.equal("a b", EMPTY, "a%20b", EMPTY)
        assert not datatype.equal("a%zz", EMPTY, "a%zz", EMPTY)

    def test_anyuri_facets_see_the_lexical_form(self):
        sized = xsd("anyURI", ("length", "3"))
        assert sized.allows("a b", EMPTY)
        assert not sized.allows("a b c", EMPTY)
        patterned = xsd("anyURI", ("pattern", "a b"))
        assert patterned.allows("a b", EMPTY)
        assert not patterned.allows("a%20b", EMPTY)


class TestXsdEquality:
    @pytest.mark.parametrize("precision", (28, 50))
    @pytest.mark.parametrize(
        ("type_name", "prefix"),
        (("dateTime", "2000-01-01T00:00:00."), ("time", "01:00:00.")),
    )
    def test_temporal_equality_is_independent_of_decimal_context(
        self, precision, type_name, prefix
    ):
        with decimal.localcontext() as context:
            context.prec = precision
            datatype = xsd(type_name)
            left = prefix + "0" * 25 + "1Z"
            right = prefix + "0" * 25 + "2Z"
            assert datatype.allows(left, EMPTY)
            assert not datatype.equal(left, EMPTY, right, EMPTY)
            assert datatype.equal(left, EMPTY, left[:-1] + "0Z", EMPTY)

    def test_numeric_and_binary_spellings_compare_by_value(self):
        assert xsd("integer").equal("1", EMPTY, "01", EMPTY)
        assert xsd("decimal").equal("1.0", EMPTY, "1.00", EMPTY)
        assert xsd("hexBinary").equal("FF", EMPTY, "ff", EMPTY)
        assert xsd("base64Binary").equal("AP8=", EMPTY, "A P8=", EMPTY)

    def test_temporal_equality_uses_the_xsd_value_space(self):
        zoned = xsd("dateTime")
        assert zoned.equal("2000-01-01T00:00:00Z", EMPTY, "2000-01-01T01:00:00+01:00", EMPTY)
        assert not zoned.equal("2000-01-01T00:00:00", EMPTY, "2000-01-01T00:00:00Z", EMPTY)

    def test_temporal_equality_keeps_fractional_seconds_exact(self):
        date_time = xsd("dateTime")
        assert not date_time.equal(
            "2000-01-01T00:00:00.0000001Z",
            EMPTY,
            "2000-01-01T00:00:00.0000002Z",
            EMPTY,
        )
        assert date_time.equal(
            "2000-01-01T00:00:00.0000001Z",
            EMPTY,
            "2000-01-01T00:00:00.00000010Z",
            EMPTY,
        )

        time = xsd("time")
        assert not time.equal(
            "00:00:00.0000001Z",
            EMPTY,
            "00:00:00.0000002Z",
            EMPTY,
        )
        assert time.equal(
            "00:00:00.0000001Z",
            EMPTY,
            "00:00:00.00000010Z",
            EMPTY,
        )

    def test_duration_equality_is_xsd_1_0_identity(self):
        duration = xsd("duration")
        assert duration.equal("P1Y", EMPTY, "P1Y", EMPTY)
        assert not duration.equal("P1Y", EMPTY, "P12M", EMPTY)
        assert not duration.equal("P1D", EMPTY, "PT24H", EMPTY)
        assert duration.equal("PT1M", EMPTY, "PT01M", EMPTY)
        assert duration.equal("PT0.5S", EMPTY, "PT0.50S", EMPTY)
        assert duration.equal("-PT0S", EMPTY, "PT0S", EMPTY)

    def test_equality_ignores_parameterized_facets(self):
        bounded = xsd("integer", ("minInclusive", "10"))
        assert bounded.equal("1", EMPTY, "01", EMPTY)

    def test_invalid_lexical_forms_never_compare_equal(self):
        assert not xsd("date").equal("2001-02-29", EMPTY, "2001-02-29", EMPTY)
        assert not xsd("integer").equal("x", EMPTY, "x", EMPTY)


class TestXsd10TemporalNumbering:
    @pytest.mark.parametrize("sign", ("", "-"))
    @pytest.mark.parametrize(
        ("type_name", "suffix"),
        (
            ("gYear", "Z"),
            ("gYearMonth", "-02Z"),
            ("date", "-02-28Z"),
            ("dateTime", "-02-28T00:00:00Z"),
        ),
    )
    def test_giant_years_keep_acceptance_identity_and_codecs(self, sign, type_name, suffix):
        year = sign + "1" + "0" * 4300
        other_year = sign + "1" + "0" * 4299 + "1"
        lexical, other = year + suffix, other_year + suffix
        datatype = xsd(type_name)
        assert datatype.allows(lexical, EMPTY)
        assert datatype.equal(lexical, EMPTY, lexical, EMPTY)
        assert not datatype.equal(lexical, EMPTY, other, EMPTY)
        decoded = datatype.decode(lexical, EMPTY)
        assert decoded.lexical() == lexical
        assert datatype.encode(decoded, EMPTY) == lexical
        bounded = xsd(type_name, ("minExclusive", lexical))
        assert bounded.allows(other, EMPTY) is (not sign)
        assert not bounded.allows(lexical, EMPTY)

    @pytest.mark.parametrize("type_name", ("date", "dateTime"))
    def test_giant_year_calendar_validity_uses_the_full_year(self, type_name):
        year = "1" + "0" * 4300
        suffix = "T00:00:00Z" if type_name == "dateTime" else "Z"
        datatype = xsd(type_name)
        assert datatype.allows(year + "-02-29" + suffix, EMPTY)
        assert not datatype.allows("-" + year + "-02-29" + suffix, EMPTY)
        assert datatype.allows("-" + year[:-1] + "1-02-29" + suffix, EMPTY)
        assert not datatype.allows(year[:-1] + "1-02-29" + suffix, EMPTY)
        assert not datatype.allows(year + "-04-31" + suffix, EMPTY)
        assert not datatype.allows("0" + year + "-01-01" + suffix, EMPTY)

    @pytest.mark.parametrize("precision", (28, 50))
    @pytest.mark.parametrize("zoned_bound", (False, True))
    def test_exact_mixed_timezone_boundary(self, precision, zoned_bound):
        tiny = "0" * 25 + "1"
        bound = "2000-01-02T00:00:00"
        suffix = "" if zoned_bound else "Z"
        with decimal.localcontext() as context:
            context.prec = precision
            maximum = xsd("dateTime", ("maxExclusive", bound + ("Z" if zoned_bound else "")))
            minimum = xsd("dateTime", ("minExclusive", bound + ("Z" if zoned_bound else "")))
            assert maximum.allows("2000-01-01T09:59:59." + "9" * 26 + suffix, EMPTY)
            assert not maximum.allows("2000-01-01T10:00:00" + suffix, EMPTY)
            assert not maximum.allows("2000-01-01T10:00:00." + tiny + suffix, EMPTY)
            assert minimum.allows("2000-01-02T14:00:00." + tiny + suffix, EMPTY)
            assert not minimum.allows("2000-01-02T14:00:00" + suffix, EMPTY)
            assert not minimum.allows("2000-01-02T13:59:59." + "9" * 26 + suffix, EMPTY)

    def test_1_bce_is_a_leap_year(self):
        datatype = xsd("date")
        assert datatype.allows("-0001-02-29", EMPTY)
        assert datatype.allows("-0005-02-29", EMPTY)
        assert datatype.allows("-0401-02-29", EMPTY)
        assert not datatype.allows("-0002-02-29", EMPTY)
        assert not datatype.allows("-0004-02-29", EMPTY)
        assert not datatype.allows("-0101-02-29", EMPTY)

    def test_bce_leap_day_applies_to_datetime(self):
        datatype = xsd("dateTime")
        assert datatype.allows("-0001-02-29T00:00:00", EMPTY)
        assert datatype.allows("-0005-02-29T00:00:00", EMPTY)
        assert not datatype.allows("-0004-02-29T00:00:00", EMPTY)

    def test_year_bearing_types_accept_negative_lexical_years(self):
        assert xsd("gYear").allows("-0001", EMPTY)
        assert xsd("gYear").allows("-0001Z", EMPTY)
        assert xsd("gYearMonth").allows("-0001-02", EMPTY)
        assert not xsd("gYear").allows("0000", EMPTY)
        assert not xsd("gYearMonth").allows("-0000-02", EMPTY)

    def test_malformed_or_year_zero_temporal_lexicals_are_rejected(self):
        datatype = xsd("date")
        for lexical in ("0000-01-01", "-0000-01-01", "00000-01-01", "-00000-01-01"):
            assert not datatype.allows(lexical, EMPTY)
        assert not datatype.allows("not-a-date", EMPTY)

    def test_bce_equality_uses_the_xsd_1_0_year_timeline(self):
        date = xsd("date")
        assert date.equal("-0001-12-31-14:00", EMPTY, "0001-01-01+10:00", EMPTY)
        assert not date.equal("-0001-12-30-14:00", EMPTY, "0001-01-01+10:00", EMPTY)

        date_time = xsd("dateTime")
        assert date_time.equal(
            "-0001-12-31T00:00:00-14:00",
            EMPTY,
            "0001-01-01T04:00:00+14:00",
            EMPTY,
        )
        assert not date_time.equal(
            "-0001-12-31T00:00:00-14:00",
            EMPTY,
            "0001-01-01T05:00:00+14:00",
            EMPTY,
        )

    def test_g_year_and_g_year_month_equality(self):
        year = xsd("gYear")
        assert year.equal("-0001", EMPTY, "-0001", EMPTY)
        assert not year.equal("-0001", EMPTY, "0001", EMPTY)

        year_month = xsd("gYearMonth")
        assert year_month.equal("-0001-02", EMPTY, "-0001-02", EMPTY)
        assert not year_month.equal("-0001-02", EMPTY, "0001-02", EMPTY)

    def test_bce_values_decode_and_encode_with_the_authored_lexical(self):
        assert xsd("date").decode("-0001-02-29", EMPTY).lexical() == "-0001-02-29"
        assert (
            xsd("dateTime").decode("-0001-02-29T00:00:00", EMPTY).lexical()
            == "-0001-02-29T00:00:00"
        )
        assert xsd("gYear").decode("-0001", EMPTY).lexical() == "-0001"
        assert xsd("gYearMonth").decode("-0001-02", EMPTY).lexical() == "-0001-02"
        assert xsd("date").encode("-0001-02-29", EMPTY) == "-0001-02-29"
        with pytest.raises(TypeError):
            xsd("date").decode("-0004-02-29", EMPTY)

    def test_temporal_bounds_use_the_xsd_1_0_year_numbering(self):
        inclusive = xsd("date", ("minInclusive", "-0001-02-28"))
        assert inclusive.allows("-0001-02-28", EMPTY)
        assert inclusive.allows("-0001-03-01", EMPTY)
        assert not inclusive.allows("-0001-02-27", EMPTY)

        exclusive = xsd("date", ("minExclusive", "-0001-02-28"))
        assert not exclusive.allows("-0001-02-28", EMPTY)
        assert exclusive.allows("-0001-03-01", EMPTY)

        maximum_inclusive = xsd("date", ("maxInclusive", "-0001-03-01"))
        assert maximum_inclusive.allows("-0001-03-01", EMPTY)
        assert not maximum_inclusive.allows("-0001-03-02", EMPTY)

        maximum_exclusive = xsd("date", ("maxExclusive", "-0001-03-01"))
        assert not maximum_exclusive.allows("-0001-03-01", EMPTY)
        assert maximum_exclusive.allows("-0001-02-28", EMPTY)

    def test_temporal_bounds_cross_the_no_year_zero_boundary(self):
        inclusive = xsd("date", ("maxInclusive", "-0001-12-31-14:00"))
        assert inclusive.allows("0001-01-01+10:00", EMPTY)
        exclusive = xsd("date", ("maxExclusive", "-0001-12-31-14:00"))
        assert not exclusive.allows("0001-01-01+10:00", EMPTY)

        bounded = xsd(
            "dateTime",
            ("minInclusive", "-0001-02-29T00:00:00"),
            ("maxInclusive", "0001-01-01T00:00:00"),
        )
        assert bounded.allows("-0001-02-29T00:00:00", EMPTY)
        assert bounded.allows("0001-01-01T00:00:00", EMPTY)
        assert not bounded.allows("-0002-01-01T00:00:00", EMPTY)
        assert not bounded.allows("0001-01-02T00:00:00", EMPTY)

    def test_temporal_bound_literals_use_xsd_1_0_numbering(self):
        assert xsd("date", ("minInclusive", "-0001-02-29")).allows("-0001-02-29", EMPTY)
        assert xsd("date", ("minInclusive", "-0001-02-29")).allows("0001-01-01", EMPTY)
        with pytest.raises(DatatypeParameterError):
            xsd("date", ("minInclusive", "-0004-02-29"))
        with pytest.raises(DatatypeParameterError):
            xsd("date", ("minInclusive", "0000-01-01"))

    def test_temporal_bound_conflicts_are_parameter_errors(self):
        for parameters in (
            (("minInclusive", "-0001-01-01"), ("minExclusive", "-0001-01-01")),
            (("maxInclusive", "-0001-01-01"), ("maxExclusive", "-0001-01-01")),
            (("minInclusive", "0001-01-01"), ("maxInclusive", "-0001-01-01")),
            (("minInclusive", "-0001-01-01"), ("maxExclusive", "-0001-01-01")),
        ):
            with pytest.raises(DatatypeParameterError):
                xsd("date", *parameters)

    def test_temporal_pattern_facets_see_the_lexical_form(self):
        datatype = xsd("date", ("pattern", "-0001-.*"))
        assert datatype.allows("-0001-01-01", EMPTY)
        assert not datatype.allows("0001-01-01", EMPTY)

    def test_mixed_timezone_temporal_bounds_are_incomparable(self):
        cases = (
            ("date", "2000-01-20", "2000-01-20Z"),
            ("dateTime", "2000-01-20T12:00:00", "2000-01-20T12:00:00Z"),
            ("gYear", "2000", "2000Z"),
            ("gYearMonth", "2000-01", "2000-01Z"),
            ("gMonth", "--01", "--01Z"),
            ("gMonthDay", "--01-01", "--01-01Z"),
            ("gDay", "---01", "---01Z"),
            ("time", "12:00:00", "12:00:00Z"),
        )
        for type_name, unzoned, zoned in cases:
            for facet in ("minInclusive", "minExclusive", "maxInclusive", "maxExclusive"):
                assert not xsd(type_name, (facet, zoned)).allows(unzoned, EMPTY)
                assert not xsd(type_name, (facet, unzoned)).allows(zoned, EMPTY)

    def test_mixed_timezone_temporal_bounds_can_be_determinate(self):
        assert xsd("dateTime", ("maxInclusive", "2000-01-02T00:00:00Z")).allows(
            "2000-01-01T00:00:00", EMPTY
        )
        assert xsd("dateTime", ("minInclusive", "2000-01-01T00:00:00Z")).allows(
            "2000-01-03T00:00:00", EMPTY
        )
        assert not xsd("dateTime", ("maxInclusive", "2000-01-01T00:00:00Z")).allows(
            "2000-01-02T00:00:00", EMPTY
        )
        assert xsd("date", ("maxInclusive", "2000-01-02Z")).allows("2000-01-01", EMPTY)
        assert xsd("time", ("maxInclusive", "23:00:00Z")).allows("01:00:00", EMPTY)
        assert xsd("dateTime", ("maxInclusive", "2000-01-02T00:00:00")).allows(
            "2000-01-01T00:00:00Z", EMPTY
        )
        assert xsd("dateTime", ("minInclusive", "2000-01-01T00:00:00")).allows(
            "2000-01-03T00:00:00Z", EMPTY
        )

    def test_midnight_24_temporal_equality(self):
        assert xsd("dateTime").equal("2000-01-01T24:00:00Z", EMPTY, "2000-01-02T00:00:00Z", EMPTY)
        assert xsd("time").equal("24:00:00Z", EMPTY, "00:00:00Z", EMPTY)

    def test_invalid_non_year_temporal_bound_is_rejected(self):
        with pytest.raises(DatatypeParameterError):
            xsd("time", ("minInclusive", "not-a-time"))

    def test_mixed_timezone_temporal_bound_pairs(self):
        for type_name, unzoned, zoned in (
            ("dateTime", "2000-01-01T00:00:00", "2000-01-01T00:00:00Z"),
            ("date", "2000-01-01", "2000-01-01Z"),
            ("time", "12:00:00", "12:00:00Z"),
        ):
            with pytest.raises(DatatypeParameterError):
                xsd(
                    type_name,
                    ("minInclusive", unzoned),
                    ("maxInclusive", zoned),
                )
            with pytest.raises(DatatypeParameterError):
                xsd(
                    type_name,
                    ("minInclusive", zoned),
                    ("maxInclusive", unzoned),
                )

        comparable = xsd(
            "dateTime",
            ("minInclusive", "2000-01-01T00:00:00"),
            ("maxInclusive", "2000-01-03T00:00:00Z"),
        )
        assert comparable.allows("2000-01-02T00:00:00", EMPTY)

    def test_fractional_temporal_bounds_are_exact(self):
        bounded = xsd(
            "dateTime",
            ("maxInclusive", "2000-01-01T00:00:00.0000001Z"),
        )
        assert bounded.allows("2000-01-01T00:00:00.0000001Z", EMPTY)
        assert not bounded.allows("2000-01-01T00:00:00.0000002Z", EMPTY)


class TestXsd10DurationOrder:
    def test_giant_seconds_and_fractions_are_exact(self):
        seconds = "1" + "0" * 4300
        fraction = "0" * 4300 + "1"
        lexical = "PT" + seconds + "." + fraction + "S"
        other = "PT" + seconds + "." + fraction[:-1] + "2S"
        datatype = xsd("duration")
        assert datatype.allows(lexical, EMPTY)
        assert datatype.equal(lexical, EMPTY, lexical, EMPTY)
        assert not datatype.equal(lexical, EMPTY, other, EMPTY)
        assert xsd("duration", ("minExclusive", lexical)).allows(other, EMPTY)

    @pytest.mark.parametrize("precision", (28, 50))
    @pytest.mark.parametrize("sign", ("", "-"))
    def test_fractional_duration_neighbors_are_exact(self, precision, sign):
        lower = sign + "PT1." + "0" * 25 + "1S"
        upper = sign + "PT1." + "0" * 25 + "2S"
        if sign:
            lower, upper = upper, lower
        with decimal.localcontext() as context:
            context.prec = precision
            bounded = xsd("duration", ("minExclusive", lower))
            assert bounded.allows(upper, EMPTY)
            assert not bounded.allows(lower, EMPTY)
            assert not bounded.equal(lower, EMPTY, upper, EMPTY)
            assert bounded.equal(lower, EMPTY, lower[:-1] + "0S", EMPTY)
            assert xsd("duration", ("maxExclusive", upper)).allows(lower, EMPTY)
            assert not xsd("duration", ("maxExclusive", lower)).allows(upper, EMPTY)

    @pytest.mark.parametrize("precision", (28, 50))
    def test_exact_fraction_does_not_collapse_duration_coordinates(self, precision):
        fraction = "." + "0" * 25 + "1S"
        with decimal.localcontext() as context:
            context.prec = precision
            datatype = xsd("duration")
            assert not datatype.equal("PT60" + fraction, EMPTY, "PT1M0" + fraction, EMPTY)
            for facet in ("minInclusive", "minExclusive", "maxInclusive", "maxExclusive"):
                assert not xsd("duration", (facet, "PT60" + fraction)).allows(
                    "PT1M0" + fraction, EMPTY
                )
                assert not xsd("duration", (facet, "P30DT0" + fraction)).allows(
                    "P1MT0" + fraction, EMPTY
                )

    def test_comparable_durations_follow_the_four_reference_dates(self):
        assert xsd("duration", ("minInclusive", "P364D")).allows("P1Y", EMPTY)
        assert xsd("duration", ("minExclusive", "P364D")).allows("P1Y", EMPTY)
        assert not xsd("duration", ("maxInclusive", "P364D")).allows("P1Y", EMPTY)
        assert xsd("duration", ("maxExclusive", "P1Y")).allows("P364D", EMPTY)

        # The shared helper puts P1M above P32D; XSD 1.0 puts it below.
        assert not xsd("duration", ("minInclusive", "P32D")).allows("P1M", EMPTY)
        assert xsd("duration", ("maxInclusive", "P32D")).allows("P1M", EMPTY)
        assert xsd("duration", ("maxExclusive", "P32D")).allows("P1M", EMPTY)

        assert xsd("duration", ("maxInclusive", "P154D")).allows("P5M", EMPTY)
        assert not xsd("duration", ("minInclusive", "P154D")).allows("P5M", EMPTY)
        assert xsd("duration", ("maxInclusive", "P367D")).allows("P1Y", EMPTY)
        assert not xsd("duration", ("minInclusive", "P367D")).allows("P1Y", EMPTY)
        assert xsd("duration", ("minInclusive", "P1000000Y")).allows("P1000001Y", EMPTY)
        assert not xsd("duration", ("minInclusive", "P1000000Y")).allows("P999999Y", EMPTY)

    def test_negative_durations_follow_the_same_partial_order(self):
        assert xsd("duration", ("maxInclusive", "-P364D")).allows("-P1Y", EMPTY)
        assert not xsd("duration", ("minInclusive", "-P364D")).allows("-P1Y", EMPTY)
        assert xsd("duration", ("maxInclusive", "PT0S")).allows("-PT0.5S", EMPTY)
        assert not xsd("duration", ("minInclusive", "PT0S")).allows("-PT0.5S", EMPTY)
        assert xsd("duration", ("maxInclusive", "-PT30M")).allows("-PT1H", EMPTY)
        assert xsd("duration", ("minInclusive", "PT1S")).allows("PT1.5S", EMPTY)
        assert not xsd("duration", ("minExclusive", "PT1.5S")).allows("PT1S", EMPTY)
        assert xsd("duration", ("maxExclusive", "PT1.5S")).allows("PT1S", EMPTY)

    def test_incomparable_durations_reject_every_bound(self):
        for value, bound in (("P1M", "P30D"), ("P1Y", "P365D"), ("P5M", "P150D")):
            for facet in ("minInclusive", "minExclusive", "maxInclusive", "maxExclusive"):
                assert not xsd("duration", (facet, bound)).allows(value, EMPTY)

    def test_distinct_but_order_indistinguishable_durations(self):
        # These pairs add identically to all four references but are distinct
        # XSD 1.0 values, so no bound may accept them as equal.
        for value, bound in (
            ("P1Y", "P12M"),
            ("P12M", "P1Y"),
            ("PT90S", "PT1M30S"),
            ("PT1M", "PT60S"),
            ("PT24H", "P1D"),
        ):
            for facet in ("minInclusive", "minExclusive", "maxInclusive", "maxExclusive"):
                assert not xsd("duration", (facet, bound)).allows(value, EMPTY)

    def test_identical_duration_bounds_are_inclusive(self):
        for facet in ("minInclusive", "maxInclusive"):
            assert xsd("duration", (facet, "P1Y")).allows("P1Y", EMPTY)
            assert xsd("duration", (facet, "P1Y")).allows("P1Y0M", EMPTY)
        for facet in ("minExclusive", "maxExclusive"):
            assert not xsd("duration", (facet, "P1Y")).allows("P1Y", EMPTY)
        assert xsd("duration", ("minInclusive", "PT0S")).allows("-PT0S", EMPTY)
        assert xsd("duration", ("maxInclusive", "-PT0S")).allows("PT0S", EMPTY)

    def test_duration_bound_conflicts_are_parameter_errors(self):
        for parameters in (
            (("minInclusive", "P1Y"), ("maxInclusive", "P364D")),
            (("minInclusive", "P1M"), ("maxInclusive", "P30D")),
            (("minInclusive", "P12M"), ("maxInclusive", "P1Y")),
            (("minInclusive", "P1Y"), ("maxExclusive", "P1Y")),
            (("minInclusive", "P1Y"), ("minExclusive", "P1Y")),
            (("maxInclusive", "P1Y"), ("maxExclusive", "P1Y")),
            (("minInclusive", "not-a-duration"),),
        ):
            with pytest.raises(DatatypeParameterError):
                xsd("duration", *parameters)

    def test_valid_duration_bound_ranges(self):
        singleton = xsd("duration", ("minInclusive", "P1Y"), ("maxInclusive", "P1Y"))
        assert singleton.allows("P1Y", EMPTY)
        assert not singleton.allows("P12M", EMPTY)

        ranged = xsd("duration", ("minInclusive", "P1D"), ("maxInclusive", "P1Y"))
        assert ranged.allows("P1D", EMPTY)
        assert ranged.allows("P1M", EMPTY)
        assert not ranged.allows("P2Y", EMPTY)

        carried = xsd("duration", ("minInclusive", "P1Y"))
        assert carried.allows("P13M", EMPTY)

    def test_duration_bounds_combine_with_pattern_facets(self):
        datatype = xsd("duration", ("pattern", "P.*"), ("minInclusive", "P1D"))
        assert datatype.allows("P1D", EMPTY)
        assert not datatype.allows("P0D", EMPTY)
        assert not datatype.allows("-P1D", EMPTY)

    def test_duration_value_equality_is_not_the_partial_order(self):
        datatype = xsd("duration")
        assert not datatype.equal("P1Y", EMPTY, "P12M", EMPTY)
        assert datatype.equal("-PT0S", EMPTY, "PT0S", EMPTY)

    def test_huge_duration_equality_returns_bool(self):
        datatype = xsd("duration")
        huge = "P" + "9" * 10000 + "Y"
        other = "P" + "9" * 9999 + "8Y"
        assert datatype.allows(huge, EMPTY)
        assert datatype.equal(huge, EMPTY, huge, EMPTY) is True
        assert datatype.equal(huge, EMPTY, other, EMPTY) is False


class TestXsdValuePatterns:
    def test_repeated_patterns_intersect(self):
        datatype = xsd("string", ("pattern", "[a-z]+"), ("pattern", ".{3}"))
        assert datatype.allows("abc", EMPTY)
        assert not datatype.allows("ab", EMPTY)
        assert not datatype.allows("abcd", EMPTY)
        assert not datatype.allows("ABC", EMPTY)
        assert xsd("string", ("pattern", "[a-z]+"), ("pattern", "[a-z]+")).allows("abc", EMPTY)

    def test_repeated_patterns_are_order_independent(self):
        first = xsd("string", ("pattern", "[a-z]+"), ("pattern", ".{3}"))
        second = xsd("string", ("pattern", ".{3}"), ("pattern", "[a-z]+"))
        assert first == second

    def test_pattern_datatypes_stay_equal_across_regex_cache_eviction(self):
        first = xsd("string", ("pattern", "[a-z]+"))
        re.purge()
        second = xsd("string", ("pattern", "[a-z]+"))
        assert first == second
        assert hash(first) == hash(second)
        assert repr(first) == repr(second)

    def test_patterns_see_the_whitespace_processed_lexical_form(self):
        datatype = xsd("token", ("pattern", "a b"))
        assert datatype.allows(" a\tb ", EMPTY)
        assert not datatype.allows("ab", EMPTY)

    def test_length_and_digit_facets_are_enforced(self):
        sized = xsd("string", ("length", "2"))
        assert sized.allows("ab", EMPTY)
        assert not sized.allows("abc", EMPTY)

        digits = xsd("decimal", ("totalDigits", "2"))
        assert digits.allows("12", EMPTY)
        assert not digits.allows("123", EMPTY)

        fraction = xsd("decimal", ("fractionDigits", "1"))
        assert fraction.allows("1.2", EMPTY)
        assert not fraction.allows("1.23", EMPTY)

    def test_inclusive_bounds_are_enforced(self):
        datatype = xsd("integer", ("minInclusive", "2"), ("maxInclusive", "4"))
        assert not datatype.allows("1", EMPTY)
        assert datatype.allows("2", EMPTY)
        assert datatype.allows("4", EMPTY)
        assert not datatype.allows("5", EMPTY)

    def test_encoding_uses_the_parameterized_value_space(self):
        datatype = xsd("string", ("pattern", "[a-z]+"))
        assert datatype.encode("abc", EMPTY) == "abc"
        with pytest.raises(TypeError):
            datatype.encode("ABC", EMPTY)


class TestXsdParameters:
    def test_duplicate_non_pattern_parameters_are_rejected(self):
        with pytest.raises(DatatypeParameterError):
            xsd("string", ("length", "1"), ("length", "2"))

    def test_white_space_is_rejected(self):
        with pytest.raises(DatatypeParameterError, match="whiteSpace"):
            xsd("string", ("whiteSpace", "collapse"))

    def test_enumeration_is_rejected(self):
        with pytest.raises(DatatypeParameterError, match="enumeration"):
            xsd("string", ("enumeration", "a"))

    def test_unknown_parameters_are_rejected(self):
        with pytest.raises(DatatypeParameterError):
            xsd("string", ("facet", "value"))

    def test_inapplicable_parameters_are_rejected(self):
        with pytest.raises(DatatypeParameterError):
            xsd("integer", ("length", "2"))

    def test_bounds_are_only_accepted_for_ordered_types(self):
        assert xsd("date", ("minInclusive", "2000-01-01")).allows("2000-01-02", EMPTY)
        with pytest.raises(DatatypeParameterError):
            xsd("boolean", ("minInclusive", "true"))
        with pytest.raises(DatatypeParameterError):
            xsd("string", ("maxInclusive", "z"))
        with pytest.raises(DatatypeParameterError):
            xsd("hexBinary", ("minInclusive", "00"))

    def test_illegal_facet_values_are_rejected(self):
        with pytest.raises(DatatypeParameterError):
            xsd("integer", ("minInclusive", "not-an-integer"))
        with pytest.raises(DatatypeParameterError):
            xsd("string", ("length", "-1"))

    def test_conflicting_facets_are_rejected(self):
        with pytest.raises(DatatypeParameterError):
            xsd("integer", ("minInclusive", "10"), ("maxInclusive", "1"))

    def test_illegal_patterns_are_rejected(self):
        with pytest.raises(DatatypeParameterError):
            xsd("string", ("pattern", "["))

    def test_parameter_instances_are_required(self):
        with pytest.raises(TypeError):
            XSD.create("string", (("length", "2"),), EMPTY)  # type: ignore[arg-type]


class TestXsdInventory:
    def test_every_xsd_1_0_builtin_type_is_available(self):
        assert len(XSD_10_TYPES) == 45
        for type_name in XSD_10_TYPES:
            datatype = xsd(type_name)
            assert isinstance(datatype, Datatype)
            assert datatype.type_name == type_name
            assert datatype.library_uri == XSD_LIBRARY

    def test_xsd_1_1_only_types_are_rejected_explicitly(self):
        for type_name in (
            "anyAtomicType",
            "dateTimeStamp",
            "dayTimeDuration",
            "precisionDecimal",
            "yearMonthDuration",
        ):
            with pytest.raises(UnsupportedDatatypeError, match=r"XSD 1\.1"):
                xsd(type_name)

    def test_non_simple_types_are_rejected(self):
        for type_name in ("anyType", "List", "noSuchType"):
            with pytest.raises(UnsupportedDatatypeError):
                xsd(type_name)

    def test_xsd_1_0_year_zero_is_rejected(self):
        assert not xsd("date").allows("0000-01-01", EMPTY)
        assert not xsd("dateTime").allows("0000-01-01T00:00:00", EMPTY)
        assert not xsd("gYear").allows("0000Z", EMPTY)
        assert not xsd("gYearMonth").allows("-0000-01", EMPTY)
        assert xsd("date").allows("-0001-01-01", EMPTY)
        assert xsd("dateTime").allows("0001-01-01T00:00:00", EMPTY)

    def test_year_zero_invalidity_applies_to_equality(self):
        datatype = xsd("date")
        assert not datatype.equal("0000-01-01", EMPTY, "0000-01-01", EMPTY)


class TestXsdQNames:
    SCHEMA = DatatypeContext("urn:schema", {"p": "urn:one", "": "urn:schema-default"})
    INSTANCE = DatatypeContext("urn:instance", {"q": "urn:one", "": "urn:instance-default"})

    def test_qname_resolution_uses_each_side_context(self):
        datatype = XSD.create("QName", (), self.SCHEMA)
        assert datatype.allows("p:item", self.SCHEMA)
        assert not datatype.allows("p:item", EMPTY)
        assert datatype.equal("p:item", self.SCHEMA, "q:item", self.INSTANCE)
        assert not datatype.equal("p:item", self.SCHEMA, "q:item", EMPTY)
        assert not datatype.equal("p:item", self.SCHEMA, "r:item", self.SCHEMA)
        assert not datatype.equal(
            "p:item",
            DatatypeContext(None, {"p": "urn:one"}),
            "p:item",
            DatatypeContext(None, {"p": "urn:two"}),
        )

    def test_default_namespace_applies_to_unprefixed_qnames(self):
        datatype = XSD.create("QName", (), self.SCHEMA)
        assert datatype.allows("item", self.SCHEMA)
        assert datatype.equal("item", self.SCHEMA, "item", self.SCHEMA)
        assert not datatype.equal("item", self.SCHEMA, "item", self.INSTANCE)

    def test_qname_decoding_requires_a_bound_prefix(self):
        datatype = XSD.create("QName", (), self.SCHEMA)
        decoded = datatype.decode("p:item", self.SCHEMA)
        assert isinstance(decoded, QName)
        assert decoded == "p:item"
        assert datatype.encode(decoded, self.SCHEMA) == "p:item"
        assert datatype.encode("p:item", self.SCHEMA) == "p:item"
        with pytest.raises(TypeError):
            datatype.decode("p:item", EMPTY)

    def test_qname_encoding_never_invents_a_prefix(self):
        datatype = XSD.create("QName", (), self.SCHEMA)
        with pytest.raises(TypeError):
            datatype.encode("p:item", EMPTY)
        assert datatype.encode("p:item", DatatypeContext(None, {"p": "urn:elsewhere"})) == "p:item"
        decoded = datatype.decode("p:item", self.SCHEMA)
        with pytest.raises(ValueError):
            datatype.encode(decoded, DatatypeContext(None, {"p": "urn:elsewhere"}))
        with pytest.raises(TypeError):
            datatype.encode(3, self.SCHEMA)

    def test_unresolved_qname_values_are_reencoded_with_the_supplied_context(self):
        datatype = XSD.create("QName", (), self.SCHEMA)
        assert datatype.encode(QName("p:item"), self.SCHEMA) == "p:item"

    def test_resolved_qname_without_a_namespace_cannot_be_encoded(self):
        datatype = XSD.create("QName", (), self.SCHEMA)
        with qname_context({}):
            value = QName("p:item")
        with pytest.raises(ValueError):
            datatype.encode(value, self.SCHEMA)

    def test_default_namespace_mismatch_prevents_encoding(self):
        datatype = XSD.create("QName", (), self.SCHEMA)
        decoded = datatype.decode("item", self.SCHEMA)
        assert datatype.encode(decoded, self.SCHEMA) == "item"
        with pytest.raises(ValueError):
            datatype.encode(decoded, self.INSTANCE)

    def test_xml_prefix_is_implicitly_bound(self):
        datatype = XSD.create("QName", (), self.SCHEMA)
        assert datatype.allows("xml:lang", EMPTY)
        assert datatype.equal(
            "xml:lang",
            EMPTY,
            "xml:lang",
            DatatypeContext(None, {"xml": "http://www.w3.org/XML/1998/namespace"}),
        )

    def test_notation_uses_qname_value_semantics(self):
        datatype = XSD.create("NOTATION", (), self.SCHEMA)
        assert datatype.equal("p:item", self.SCHEMA, "q:item", self.INSTANCE)
        assert isinstance(datatype.decode("p:item", self.SCHEMA), QName)

    def test_qname_datatype_identity_includes_its_context(self):
        first = XSD.create("QName", (), self.SCHEMA)
        second = XSD.create("QName", (), self.INSTANCE)
        assert first != second
        assert first == XSD.create("QName", (), self.SCHEMA)
        assert xsd("integer") == XSD.create("integer", (), self.SCHEMA)
        assert "p" in repr(first)


class TestPatternPayloads:
    def test_datatypes_are_hashable_pattern_payloads(self):
        datatype = xsd("integer")
        data = Data(datatype)
        value = Value(datatype, "12")
        assert hash(data) == hash(Data(xsd("integer")))
        assert value.datatype == datatype
        assert value.lexical == "12"

    def test_datatypes_do_not_destabilize_choice_order(self):
        first_datatype = XSD.create("integer", (), DatatypeContext(None, {"p": "urn:one"}))
        second_datatype = XSD.create("integer", (), DatatypeContext(None, {"q": "urn:two"}))
        assert first_datatype == second_datatype
        assert repr(first_datatype) == repr(second_datatype)

        first = choice(Data(first_datatype), Text())
        second = choice(Text(), Data(second_datatype))
        assert first == second
        assert repr(first) == repr(second)

    def test_value_pattern_retains_the_schema_side_context(self):
        schema = DatatypeContext("urn:schema", {"p": "urn:one"})
        datatype = XSD.create("QName", (), schema)
        pattern = Value(datatype, "p:item")
        assert pattern.datatype.context == schema
