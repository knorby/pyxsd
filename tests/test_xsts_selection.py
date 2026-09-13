"""Version-profile and expectation-selection semantics."""

from __future__ import annotations

import pytest

from xsts.catalog import Expected
from xsts.selection import (
    XSD10,
    XSD11,
    MetadataError,
    expected_is_valid,
    select_expected,
)


def test_unversioned_test_applies_to_every_profile() -> None:
    assert XSD11.supports(())
    assert XSD10.supports(())


def test_version_tokens_are_a_disjunction() -> None:
    assert XSD11.supports(("1.1",))
    assert XSD11.supports(("1.0", "1.1"))
    assert not XSD11.supports(("1.0",))
    assert XSD10.supports(("1.0",))
    assert not XSD10.supports(("full-xpath-in-CTA",))


def test_version_tagged_expectation_overrides_unversioned_default() -> None:
    expectations = (
        Expected(validity="valid"),
        Expected(validity="invalid", version=("1.0",)),
    )
    assert select_expected(expectations, XSD11).validity == "valid"
    assert select_expected(expectations, XSD10).validity == "invalid"


def test_exclusive_version_tagged_expectations() -> None:
    expectations = (
        Expected(validity="valid", version=("1.1",)),
        Expected(validity="invalid", version=("1.0",)),
    )
    assert select_expected(expectations, XSD11).validity == "valid"
    assert select_expected(expectations, XSD10).validity == "invalid"


def test_expectation_gated_to_another_dimension_means_not_applicable() -> None:
    expectations = (Expected(validity="valid", version=("1.1",)),)
    assert select_expected(expectations, XSD10) is None


def test_unicode_dimension_is_not_applicable_to_an_xsd_profile() -> None:
    expectations = (
        Expected(validity="valid", version=("Unicode_4.0.0",)),
        Expected(validity="invalid", version=("Unicode_6.0.0",)),
    )
    assert select_expected(expectations, XSD11) is None


def test_multiple_matching_expectations_is_a_metadata_error() -> None:
    expectations = (
        Expected(validity="valid", version=("1.1",)),
        Expected(validity="invalid", version=("1.1",)),
    )
    with pytest.raises(MetadataError):
        select_expected(expectations, XSD11)


def test_multiple_unversioned_expectations_is_a_metadata_error() -> None:
    expectations = (Expected(validity="valid"), Expected(validity="invalid"))
    with pytest.raises(MetadataError):
        select_expected(expectations, XSD11)


def test_no_expectations_is_a_metadata_error() -> None:
    with pytest.raises(MetadataError):
        select_expected((), XSD11)


def test_expected_is_valid_mapping() -> None:
    assert expected_is_valid("valid") is True
    assert expected_is_valid("invalid") is False
    assert expected_is_valid("invalid-latent") is False
    assert expected_is_valid("notKnown") is None
    assert expected_is_valid("indeterminate") is None
    assert expected_is_valid("implementation-defined") is None
