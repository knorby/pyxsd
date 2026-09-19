"""Targeted tests for the datatype, facet, and derivation machinery.

Complements the schema-driven suites (``test_facets.py`` and
``test_xsd_data_types.py``) with direct exercises of the value-space
keys, the ``FacetConstraints`` machinery, and the ``xsi:type`` derivation
checks, focused on the XSD 1.1 additions (duration subtypes,
``explicitTimezone``, bound restatement/widening) and their edge cases.
"""

import io
from types import SimpleNamespace
from typing import ClassVar

import pytest

from pyxsd import derivation, facets
from pyxsd import xsd_data_types as dt
from pyxsd.parser import PyXSD
from pyxsd.schema_base import SchemaBase

# ---------------------------------------------------------------------------
# Schema parsing helpers (mirrors test_facets.py)
# ---------------------------------------------------------------------------

SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
{body}
</xs:schema>"""


def parse(body, xml):
    parser = PyXSD(
        io.StringIO(xml),
        io.StringIO(SCHEMA.format(body=body)),
        xmlFileOutput=False,
        transformOutputName=None,
    )
    return parser.report


def errors(report):
    return [(issue.code, issue.message) for issue in report.errors]


def codes(body, xml):
    return [code for code, _ in errors(parse(body, xml))]


def element(base, facet_xml):
    return (
        f'<xs:element name="r"><xs:simpleType><xs:restriction base="{base}">'
        f"{facet_xml}</xs:restriction></xs:simpleType></xs:element>"
    )


def facet_source(**overrides):
    """A stand-in for the SimpleType element representative."""
    attrs = {
        "patterns": [],
        "enumerations": [],
        "length": None,
        "minLength": None,
        "maxLength": None,
        "totalDigits": None,
        "fractionDigits": None,
        "whiteSpace": None,
        "explicitTimezone": None,
        "minInclusive": None,
        "maxInclusive": None,
        "minExclusive": None,
        "maxExclusive": None,
        "xsdElement": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


class _IntList(dt.XsdList):
    """The class generated for ``<xs:list itemType="xs:int"/>``."""

    itemType = dt.Integer


# Stand-in generated classes for the derivation checks.
class _Gen(SchemaBase):
    """A generated complex type with no recorded derivation."""


class _Parent(SchemaBase):
    """A generated complex type: its derivation marker was lost, not absent."""

    _contentKind_ = "element"


class _Child(_Parent):
    """A simpleContent-style child of a generated parent."""


# Stand-in classes for the union-member checks.
class _Year:
    """A union member type."""


class _Timeish:
    """A union member type."""


class _BaseUnion:
    _unionMembers = (_Year, _Timeish)


class _SubUnion:
    _unionMembers = (_Year,)


class _NestedUnion:
    """A union member that is itself a union."""

    _unionMembers = (_Year, _Timeish)


class _RestrictionOfNestedUnion(_NestedUnion):
    """A restriction of a nested member union (inherits no own members)."""


class _OddUnion:
    _unionMembers = (_Year, object)


class _DatatypeUnion:
    _unionMembers = (dt.Date, dt.Token)


class _Shaped:
    """A value object that only exposes a ``tokens`` list."""

    tokens: ClassVar[list[str]] = ["a", "b"]


# ---------------------------------------------------------------------------
# derivation: xsi:type overrides of the ur-type
# ---------------------------------------------------------------------------


class TestUrTypeOverride:
    def test_no_block_admits_any_override_of_untyped_element(self):
        assert derivation.is_valid_xsi_type(None, SchemaBase) is None
        assert derivation.is_valid_xsi_type(_Gen, SchemaBase) is None

    def test_block_all_rejects_any_override(self):
        assert derivation.is_valid_xsi_type(_Gen, SchemaBase, "#all") == "blocked"
        assert derivation.is_valid_xsi_type(dt.String, SchemaBase, "#all") == "blocked"

    def test_block_restriction_rejects_chain_to_ur_type(self):
        # particlesIg003: the transitive restriction step to the ur-type counts.
        assert derivation.is_valid_xsi_type(_Gen, SchemaBase, "restriction") == "blocked"

    def test_block_restriction_rejects_simple_override(self):
        # A simple type is reached from the ur-type only through restrictions.
        assert derivation.is_valid_xsi_type(dt.String, SchemaBase, "restriction") == "blocked"

    def test_block_extension_admits_simple_override(self):
        # particlesIg002: a simple type's chain carries no extension step.
        assert derivation.is_valid_xsi_type(dt.String, SchemaBase, "extension") is None
        # Nor does a markerless generated class whose implicit step is a
        # restriction the block does not name.
        assert derivation.is_valid_xsi_type(_Gen, SchemaBase, "extension") is None

    def test_step_to_generated_parent_is_not_implicitly_blocked(self):
        # particlesElemT058: a markerless step onto a generated parent is a
        # lost simpleContent extension, not an implicit restriction.
        assert derivation.is_validly_derived(_Child, _Parent, "restriction") is None

    def test_block_check_of_unrelated_types_finds_no_blocked_step(self):
        # A chain that never reaches the declared type cannot be blocked.
        assert derivation._blocked_step(dt.String, dt.Integer, frozenset({"extension"})) is False

    def test_any_simple_type_rejects_missing_and_ur_type_override(self):
        assert derivation.is_valid_xsi_type(None, dt.AnySimpleType) == "not-derived"
        assert derivation.is_valid_xsi_type(dt.AnyType, dt.AnySimpleType) == "not-derived"

    def test_any_simple_type_rejects_complex_and_non_class_override(self):
        assert derivation.is_valid_xsi_type(_Gen, dt.AnySimpleType) == "not-derived"
        assert derivation.is_valid_xsi_type(123, dt.AnySimpleType) == "not-derived"

    def test_block_all_rejects_even_a_valid_derivation(self):
        assert derivation.is_validly_derived(dt.Token, dt.String, "#all") == "blocked"
        assert derivation.is_validly_derived(dt.Token, dt.String, "extension") is None


class TestUnionMemberDerivation:
    def test_none_arguments_are_never_union_derived(self):
        assert derivation.derived_from_union_member(None, _BaseUnion) is False
        assert derivation.derived_from_union_member(_SubUnion, None) is False

    def test_restriction_of_a_union_member_is_derived_from_the_union(self):
        # saxonSimple012/016: sub-chap restricts dt, a nested union that
        # is a member of chap.
        assert derivation.derived_from_union_member(_RestrictionOfNestedUnion, _BaseUnion) is True

    def test_member_subset_union_is_not_derived_from_the_union(self):
        # saxonSimple011: a union derived by restriction (its members a
        # subset of the base union's) is not substitutable.
        assert derivation.derived_from_union_member(_SubUnion, _BaseUnion) is False

    def test_union_with_a_foreign_member_is_not_derived(self):
        assert derivation.derived_from_union_member(_OddUnion, _BaseUnion) is False

    def test_xsi_type_may_name_a_union_member(self):
        # MS elemT071/072: a union member stands in for the union.
        assert derivation.is_validly_derived(dt.Date, _DatatypeUnion) is None
        assert derivation.is_validly_derived(dt.Token, _DatatypeUnion) is None
        assert derivation.is_validly_derived(dt.GDay, _DatatypeUnion) == "not-derived"


class TestDerivationGuards:
    def test_boolean_integer_exception_guard_tolerates_missing_builtin(self, monkeypatch):
        monkeypatch.setattr(dt, "Boolean", None)
        assert derivation._builtinNotDerived(int, int) is False

    def test_string_primitive_guard_tolerates_missing_string(self, monkeypatch):
        monkeypatch.setattr(dt, "String", None)
        assert derivation._stringPrimitiveNotDerived(int, int) is False

    def test_helpers_tolerate_non_class_arguments(self):
        assert derivation._pythonDerived(int, 3) is False
        assert derivation._pythonDerived(3, int) is False
        assert derivation._stringPrimitiveNotDerived(int, 3) is False


# ---------------------------------------------------------------------------
# xsd_data_types: whitespace modes and non-string construction
# ---------------------------------------------------------------------------


class TestCompatWhitespaceMode:
    def test_replace_folds_unicode_whitespace(self):
        with dt.whitespace_mode("compat"):
            assert dt.NormalizedString("a\u00a0b\nc") == "a b c"

    def test_base64_strips_unicode_whitespace(self):
        with dt.whitespace_mode("compat"):
            assert dt.Base64Binary("aGVs\u00a0bG8=") == "aGVsbG8="
        # In the XSD profile NBSP is an ordinary character, not whitespace.
        with pytest.raises(TypeError):
            dt.Base64Binary("aGVs\u00a0bG8=")


class TestNonStringConstruction:
    def test_decimal_accepts_a_decimal_value(self):
        value = dt.Decimal(dt.Decimal("1.50"))
        assert isinstance(value, dt.Decimal)
        assert value == dt.Decimal("1.5")
        assert str(value) == "1.50"

    def test_double_accepts_a_float_value(self):
        value = dt.Double(1.5)
        assert isinstance(value, dt.Double)
        assert value == 1.5


class TestXsdListConstruction:
    def test_none_builds_the_empty_list(self):
        assert list(_IntList(None)) == []

    def test_sequence_items_convert_through_the_item_type(self):
        items = list(_IntList(["1", "2"]))
        assert items == [1, 2]
        assert all(isinstance(item, dt.Integer) for item in items)

    def test_single_atomic_value_builds_a_one_item_list(self):
        assert list(_IntList(7)) == [7]

    def test_invalid_item_is_rejected(self):
        with pytest.raises(TypeError):
            _IntList(["1", "x"])

    def test_tokens_property_renders_items_as_strings(self):
        assert _IntList(["3", "4"]).tokens == ["3", "4"]


# ---------------------------------------------------------------------------
# xsd_data_types: value-space keys
# ---------------------------------------------------------------------------


class TestValueSpaceKeys:
    def test_fractional_seconds_are_kept(self):
        assert dt._fraction_microseconds(None) == 0
        assert dt._fraction_microseconds("5") == 500000
        assert facets.order_key(dt.Time("00:00:00.5Z")) > facets.order_key(dt.Time("00:00:00Z"))

    def test_unparseable_lexicals_fall_back_to_their_text(self):
        assert dt._datetime_key("yesterday") == ("lex", "yesterday")
        assert dt._date_key("sometime") == ("lex", "sometime")
        assert dt._gyear_key("junk") == ("lex", "junk")
        assert dt._gyearmonth_key("junk") == ("lex", "junk")
        assert dt._gmonth_key("--soon") == ("lex", "--soon")
        assert dt._gmonthday_key("--soon") == ("lex", "--soon")
        assert dt._gday_key("--soon") == ("lex", "--soon")

    def test_end_of_day_spelling_equals_next_midnight(self):
        assert dt._datetime_key("2000-01-01T24:00:00") == dt._datetime_key("2000-01-02T00:00:00")
        assert dt._time_key("24:00:00") == dt._time_key("00:00:00")

    def test_time_key_folds_the_timezone_into_the_instant(self):
        assert dt._time_key("09:15:00-08:00") == dt._time_key("17:15:00Z")
        assert dt._time_key("bogus") == ("lex", "bogus")

    def test_gyear_and_gyearmonth_order_across_eras_and_timezones(self):
        assert dt._gyear_key("2006") > dt._gyear_key("-0044")
        assert dt._gyear_key("2006+01:00") < dt._gyear_key("2006Z")
        assert dt._gyearmonth_key("2006-08") > dt._gyearmonth_key("2006-07")

    def test_duration_key_of_an_unparseable_lexical_is_neutral(self):
        assert dt._duration_key("not-a-duration") == (0, 0, 0.0)
        assert dt._year_month_duration_key("not-a-duration") == 0
        assert dt._day_time_duration_key("not-a-duration") == 0.0

    def test_equality_key_tags_lexical_fallbacks_with_the_kind(self):
        assert dt._temporal_equality_key("gDay", ("lex", "x"), "x") == ("gDay", ("lex", "x"))


class TestXsdValueKey:
    def test_base64_decodes_to_bytes(self):
        assert dt.xsd_value_key(dt.Base64Binary("aGVsbG8=")) == ("base64Binary", b"hello")

    def test_list_items_are_keyed_individually(self):
        assert dt.xsd_value_key(_IntList(["1", "2"])) == (
            "list",
            (("integer", 1), ("integer", 2)),
        )

    def test_python_bool_is_keyed_as_boolean(self):
        assert dt.xsd_value_key(True) == ("boolean", 1)
        assert dt.xsd_value_key(False) == ("boolean", 0)

    def test_float_nan_keeps_a_distinct_key(self):
        assert dt.xsd_value_key(dt.Double("NaN")) == ("float", "NaN")
        assert dt.xsd_value_key(dt.Double("1.5")) == ("float", 1.5)

    def test_temporal_keys_carry_timezone_presence(self):
        assert dt.xsd_value_key(dt.Time("14:30:00Z")) == (
            "time",
            (dt._time_key("14:30:00Z"), True),
        )
        assert dt.xsd_value_key(dt.GYear("2006")) == ("gYear", (dt._gyear_key("2006"), False))
        assert dt.xsd_value_key(dt.GYearMonth("2006-08Z")) == (
            "gYearMonth",
            (dt._gyearmonth_key("2006-08Z"), True),
        )
        assert dt.xsd_value_key(dt.GMonth("--08")) == (
            "gMonth",
            (dt._gmonth_key("--08"), False),
        )


# ---------------------------------------------------------------------------
# facets: order_key and decimal_digits
# ---------------------------------------------------------------------------


class TestOrderKey:
    def test_booleans_order_as_integers(self):
        assert facets.order_key(True) == 1
        assert facets.order_key(False) == 0

    def test_duration_uses_the_partial_months_seconds_key(self):
        assert facets.order_key(dt.Duration("PT24H")) == facets.order_key(dt.Duration("P1D"))
        assert facets.order_key(dt.Duration("P1M")) != facets.order_key(dt.Duration("P30D"))

    def test_time_orders_by_instant(self):
        assert facets.order_key(dt.Time("09:00:00")) < facets.order_key(dt.Time("10:00:00"))
        assert facets.order_key(dt.Time("09:00:00+01:00")) < facets.order_key(
            dt.Time("09:00:00-01:00")
        )

    def test_gyear_and_gyearmonth_order_chronologically(self):
        assert facets.order_key(dt.GYear("2006")) > facets.order_key(dt.GYear("-0044"))
        assert facets.order_key(dt.GYearMonth("2006-08")) > facets.order_key(
            dt.GYearMonth("2006-07")
        )

    def test_unspecialised_values_fall_back_to_their_text(self):
        assert facets.order_key(dt.String("x")) == "x"


class TestDecimalDigits:
    def test_trailing_zeros_contributed_by_an_exponent_count(self):
        assert facets.decimal_digits("100") == (3, 0)
        assert facets.decimal_digits(0) == (1, 0)

    def test_nan_and_infinity_have_a_zero_fraction(self):
        assert facets.decimal_digits("NaN") == (0, 0)
        assert facets.decimal_digits("INF")[1] == 0

    def test_non_numeric_value_rejected(self):
        with pytest.raises(TypeError, match="cannot count digits"):
            facets.decimal_digits("abc")

    def test_nan_satisfies_digit_facets_vacuously(self):
        facets.FacetConstraints(total_digits=2, fraction_digits=2).check(dt.Double("NaN"), "NaN")


# ---------------------------------------------------------------------------
# facets: FacetConstraints.check
# ---------------------------------------------------------------------------


class TestCheckLengths:
    def test_length_counts_built_in_list_items(self):
        facets.FacetConstraints(length=2).check(dt.NMTOKENS("a b"))
        with pytest.raises(TypeError, match="has 1 items, expected 2"):
            facets.FacetConstraints(length=2).check(dt.NMTOKENS("a"))

    def test_length_counts_generic_list_items(self):
        facets.FacetConstraints(length=2).check(_IntList(["1", "2"]))
        with pytest.raises(TypeError, match="has 2 items, expected 3"):
            facets.FacetConstraints(length=3).check(_IntList(["1", "2"]))

    def test_length_uses_a_tokens_attribute_as_fallback(self):
        facets.FacetConstraints(length=2).check(_Shaped())
        with pytest.raises(TypeError, match="has 2 items, expected 3"):
            facets.FacetConstraints(length=3).check(_Shaped())


class TestCheckBounds:
    def test_incomparable_bound_keys_are_reported_not_raised(self):
        constraints = facets.FacetConstraints(min_inclusive=("lex", "x"))
        with pytest.raises(TypeError, match="facet bound comparison failed"):
            constraints.check(dt.Integer("5"))


# ---------------------------------------------------------------------------
# facets: applicability and merging helpers
# ---------------------------------------------------------------------------


class TestApplicabilityHelpers:
    def test_pattern_and_enumeration_apply_to_every_base(self):
        assert facets._facet_applicable("pattern", dt.Integer) is True
        assert facets._facet_applicable("enumeration", dt.Double) is True

    def test_unknown_facet_defaults_to_applicable(self):
        assert facets._facet_applicable("explicitTimezone", dt.String) is True

    def test_digit_facets_exclude_boolean(self):
        assert facets._is_decimal(dt.Boolean) is False
        assert facets._is_decimal(bool) is False
        assert facets._is_integer(dt.Boolean) is False
        assert facets._is_decimal(dt.Integer) is True
        assert facets._is_integer(dt.Integer) is True

    def test_fixed_white_space_by_base_type(self):
        assert facets._fixed_white_space(dt.String) == "preserve"
        assert facets._fixed_white_space(dt.NormalizedString) == "replace"
        assert facets._fixed_white_space(dt.Token) == "collapse"

    def test_builtin_list_bases_fix_min_length(self):
        assert facets._base_min_length(dt.NMTOKENS) == 1
        assert facets._base_min_length(None) is None
        assert facets._base_fixed_bounds(None) == (None, None, None, None)

    def test_bound_literal_factory_selection(self):
        def identity(text):
            return text

        class _Unionish:
            _unionMembers = (_Year,)

        # Union and list bases keep their own factory: bounds do not apply.
        assert facets._bound_literal_factory(_Unionish, identity) is identity
        assert facets._bound_literal_factory(dt.NMTOKENS, identity) is identity
        # Otherwise the literal validates against the primitive value space.
        assert facets._bound_literal_factory(dt.Integer, identity) is dt.Integer
        assert facets._bound_literal_factory(_Shaped, identity) is identity


class TestBoundMerging:
    def test_effective_lower_prefers_the_stricter_inclusive_bound(self):
        # minInclusive 10 dominates the looser minExclusive 5.
        assert facets._effective_lower(10, 5) == (10, False)

    def test_effective_upper_prefers_the_stricter_inclusive_bound(self):
        # maxInclusive 10 dominates the looser maxExclusive 15.
        assert facets._effective_upper(10, 15) == (10, False)

    def test_incomparable_bounds_are_treated_as_exclusive(self):
        assert facets._effective_lower(5, ("lex", "x")) == (("lex", "x"), True)
        assert facets._effective_upper(5, ("lex", "x")) == (("lex", "x"), True)

    def test_tighten_helpers_keep_the_declared_value_when_incomparable(self):
        assert facets._tighten_min("1", 5) == "1"
        assert facets._tighten_max("5", 9) == "5"
        assert facets._tighten_min(3, 5) == 5
        assert facets._tighten_max(9, 5) == 5

    def test_min_optional_takes_the_smaller_value(self):
        assert facets._min_optional(3, 4) == 3
        assert facets._min_optional(None, 4) == 4
        assert facets._min_optional(3, None) == 3


class TestFacetIntegerParsing:
    def test_non_negative_integer_values(self):
        errors = []
        assert facets._facet_non_negative_int(None, "length", errors) is None
        assert errors == []
        assert facets._facet_non_negative_int("2", "length", errors) == 2
        assert facets._facet_non_negative_int("abc", "length", errors) is None
        assert facets._facet_non_negative_int("-1", "length", errors) is None
        assert len(errors) == 2

    def test_positive_integer_values(self):
        errors = []
        assert facets._facet_positive_int("abc", "totalDigits", errors) is None
        assert facets._facet_positive_int("0", "totalDigits", errors) is None
        assert facets._facet_positive_int("2", "totalDigits", errors) == 2
        assert len(errors) == 2


class TestPatternTranslation:
    def test_escaped_backslash_passes_through_doubled(self):
        assert facets._xml11_name_classes("a\\\\b") == "a\\\\b"

    def test_own_fixed_names_without_an_element_tree_is_empty(self):
        assert facets._own_fixed_facet_names(facet_source()) == set()


# ---------------------------------------------------------------------------
# facets: build_constraints edge cases
# ---------------------------------------------------------------------------


def _rejects_four(text):
    """A base whose value space excludes 4 (a restatable boundary)."""
    if text == "4":
        raise ValueError("outside the base value space")
    return dt.Integer(text)


def _rejects_boundary(text):
    """A base whose value space excludes 4 and 5 (the fixed boundary)."""
    if text in ("4", "5"):
        raise ValueError("outside the base value space")
    return dt.Integer(text)


class TestBuildConstraints:
    def test_baseless_type_treats_every_facet_as_applicable(self):
        result = facets.build_constraints(
            facet_source(length="2"), None, None, base_factory=dt.String
        )
        assert result.errors == ()
        assert result.conflicts == ()
        assert result.constraints.length == 2

    def test_heterogeneous_bounds_do_not_crash_the_empty_space_check(self):
        result = facets.build_constraints(
            facet_source(maxInclusive="5"),
            None,
            facets.FacetConstraints(min_inclusive=("lex", "a")),
            base_factory=dt.String,
        )
        assert result.conflicts == ()
        assert result.constraints.min_inclusive == ("lex", "a")
        assert result.constraints.max_inclusive == "5"

    def test_widened_exclusive_bound_outside_the_base_is_an_error(self):
        # d3_4_28si10: parses via the primitive but is not the base's bound.
        result = facets.build_constraints(
            facet_source(maxExclusive="4"),
            dt.Integer,
            facets.FacetConstraints(max_exclusive=5),
            base_factory=_rejects_four,
        )
        assert any(
            "maxExclusive value '4' is not valid for its base type" in message
            for message in result.errors
        )
        assert result.constraints.max_exclusive == 5

    def test_bound_invalid_for_the_primitive_too_is_an_error(self):
        result = facets.build_constraints(
            facet_source(maxExclusive="abc"),
            dt.Integer,
            facets.FacetConstraints(max_exclusive=5),
            base_factory=_rejects_boundary,
        )
        assert any(
            "maxExclusive value 'abc' is not valid for its base type" in message
            for message in result.errors
        )

    def test_exact_restatement_of_the_base_boundary_is_allowed(self):
        # d3_4_28v09: the boundary itself is outside the base value space.
        result = facets.build_constraints(
            facet_source(maxExclusive="5"),
            dt.Integer,
            facets.FacetConstraints(max_exclusive=5),
            base_factory=_rejects_boundary,
        )
        assert result.errors == ()
        assert result.constraints.max_exclusive == 5


# ---------------------------------------------------------------------------
# facets: schema-time legality through real schemas
# ---------------------------------------------------------------------------


class TestSchemaFacetLegality:
    def test_length_and_max_length_conflict(self):
        body = element("xs:string", '<xs:length value="3"/><xs:maxLength value="5"/>')
        assert "facet" in codes(body, "<r>abc</r>")

    def test_length_and_min_length_conflict_on_a_string_base(self):
        body = element("xs:string", '<xs:length value="3"/><xs:minLength value="2"/>')
        assert "facet" in codes(body, "<r>abc</r>")

    def test_length_and_min_length_coexist_on_a_list_base(self):
        body = element("xs:NMTOKENS", '<xs:length value="2"/><xs:minLength value="2"/>')
        assert errors(parse(body, "<r>a b</r>")) == []
        assert codes(body, "<r>a</r>")

    def test_length_and_min_length_coexist_when_the_min_is_inherited(self):
        body = (
            '<xs:simpleType name="Base"><xs:restriction base="xs:string">'
            '<xs:minLength value="2"/></xs:restriction></xs:simpleType>'
            '<xs:simpleType name="Derived"><xs:restriction base="Base">'
            '<xs:length value="2"/><xs:minLength value="2"/>'
            "</xs:restriction></xs:simpleType>"
            '<xs:element name="r" type="Derived"/>'
        )
        assert errors(parse(body, "<r>ab</r>")) == []
        assert codes(body, "<r>a</r>")

    def test_max_length_below_min_length_is_reported(self):
        body = element("xs:string", '<xs:minLength value="5"/><xs:maxLength value="2"/>')
        assert "facet" in codes(body, "<r>abc</r>")

    def test_qname_values_satisfy_the_length_family_vacuously(self):
        body = element("xs:QName", '<xs:length value="3"/>')
        assert errors(parse(body, "<r>xs:string</r>")) == []

    def test_notation_values_satisfy_the_length_family_vacuously(self):
        # MS-DataTypes NOTATION_length001/003, minLength003, maxLength001:
        # the TSTF ruling extends to NOTATION, whose value space is a set
        # of notation names (QNames).
        body = (
            '<xs:simpleType name="buildNotation"><xs:restriction base="xs:NOTATION">'
            '<xs:enumeration value="mpeg"/></xs:restriction></xs:simpleType>'
            '<xs:notation name="mpeg" public="image/mpeg" system="viewer.exe"/>'
            '<xs:element name="r"><xs:simpleType><xs:restriction base="buildNotation">'
            '<xs:length value="1"/></xs:restriction></xs:simpleType></xs:element>'
        )
        assert errors(parse(body, "<r>mpeg</r>")) == []

    def test_whitespace_cannot_be_loosened(self):
        body = element("xs:token", '<xs:whiteSpace value="preserve"/>')
        assert "facet" in codes(body, "<r> a  b </r>")

    def test_illegal_whitespace_value_is_reported(self):
        body = element("xs:string", '<xs:whiteSpace value="sideways"/>')
        assert "facet" in codes(body, "<r>x</r>")

    def test_fraction_digits_exceeding_total_digits_is_reported(self):
        body = element("xs:decimal", '<xs:totalDigits value="2"/><xs:fractionDigits value="3"/>')
        assert "facet" in codes(body, "<r>1.2</r>")

    def test_length_facets_inconsistent_across_restriction_steps(self):
        body = (
            '<xs:simpleType name="Base"><xs:restriction base="xs:string">'
            '<xs:minLength value="5"/></xs:restriction></xs:simpleType>'
            '<xs:simpleType name="Derived"><xs:restriction base="Base">'
            '<xs:maxLength value="2"/></xs:restriction></xs:simpleType>'
            '<xs:element name="r" type="Derived"/>'
        )
        assert "facet" in codes(body, "<r>abc</r>")

    def test_total_digits_merge_takes_the_minimum(self):
        body = (
            '<xs:simpleType name="Base"><xs:restriction base="xs:decimal">'
            '<xs:totalDigits value="4"/></xs:restriction></xs:simpleType>'
            '<xs:simpleType name="Derived"><xs:restriction base="Base">'
            '<xs:totalDigits value="3"/></xs:restriction></xs:simpleType>'
            '<xs:element name="r" type="Derived"/>'
        )
        assert codes(body, "<r>1234.0</r>")
        assert errors(parse(body, "<r>123</r>")) == []

    def test_non_integer_digit_facet_value_is_reported(self):
        body = element("xs:int", '<xs:totalDigits value="1e3"/>')
        assert "facet" in codes(body, "<r>5</r>")
