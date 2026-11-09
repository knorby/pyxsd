"""Applicability selection from declared catalog dependencies.

Selection is metadata-only.  These tests build cases with controlled
dependencies and prove that applicability comes from ``spec`` and ``feature``
declarations (plus declared schema versions), never from filenames, source
documents, or streamable syntax.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
from tests.xslt.model import (
    Catalog,
    Dependency,
    Environment,
    Invocation,
    Schema,
    Source,
)
from tests.xslt.model import TestCase as CatalogCase
from tests.xslt.outcomes import Outcome
from tests.xslt.selection import (
    XSLT30_NON_STREAMING,
    FeatureProfile,
    inapplicability,
    select_cases,
)


def case(
    name: str = "case-001",
    *,
    specs: Iterable[str] = (),
    excluded_specs: Iterable[str] = (),
    features: Iterable[str] = (),
    exclusions: Iterable[str] = (),
    source_document: bool = False,
    schema_versions: Iterable[str] = (),
) -> CatalogCase:
    """Build a catalog case whose applicability metadata is fully controlled."""
    dependencies = [Dependency("spec", spec) for spec in specs]
    dependencies.extend(Dependency("spec", spec, False) for spec in excluded_specs)
    dependencies.extend(Dependency("feature", feature) for feature in features)
    dependencies.extend(Dependency("feature", feature, False) for feature in exclusions)
    return CatalogCase(
        name=name,
        local_name=name,
        test_set="synthetic",
        description="",
        source=PurePosixPath(f"{name}.xml"),
        environment=Environment(
            sources=(Source(role="source"),) if source_document else (),
            schemas=tuple(Schema(role="schema", xsd_version=v) for v in schema_versions),
        ),
        invocation=Invocation(),
        dependencies=tuple(dependencies),
    )


def catalog(*cases: CatalogCase) -> Catalog:
    return Catalog(
        path=Path("synthetic-catalog.xml"),
        root=Path("."),
        corpus_revision="synthetic",
        cases={item.name: item for item in cases},
    )


def test_streaming_dependency_is_not_syntax_detection() -> None:
    grounded = case("grounded", features={"schema_aware"}, source_document=True)
    streaming = case("streaming", features={"streaming"}, source_document=True)

    selected = select_cases(catalog(grounded, streaming), XSLT30_NON_STREAMING)

    assert [item.name for item in selected] == ["grounded"]


def test_cases_without_dependencies_are_applicable() -> None:
    plain = case("plain", source_document=True)

    assert select_cases(catalog(plain), XSLT30_NON_STREAMING) == (plain,)


def test_negative_feature_requirement_is_applicable_to_a_profile_without_it() -> None:
    non_schema = case("non-schema", exclusions={"schema_aware"}, source_document=True)
    without_schema = replace(XSLT30_NON_STREAMING, features=frozenset())

    assert inapplicability(non_schema, XSLT30_NON_STREAMING) is Outcome.NOT_APPLICABLE
    assert select_cases(catalog(non_schema), without_schema) == (non_schema,)


def test_streaming_requirement_is_not_applicable_to_the_non_streaming_profile() -> None:
    streaming = case("streaming", features={"streaming"})

    assert inapplicability(streaming, XSLT30_NON_STREAMING) is Outcome.NOT_APPLICABLE
    assert select_cases(catalog(streaming), XSLT30_NON_STREAMING) == ()


def test_a_profile_that_claims_streaming_selects_streaming_cases() -> None:
    streaming = case("streaming", features={"streaming"})
    capable = replace(
        XSLT30_NON_STREAMING,
        name="streaming-capable",
        features=XSLT30_NON_STREAMING.features | {"streaming"},
        declined_features=XSLT30_NON_STREAMING.declined_features - {"streaming"},
    )

    assert select_cases(catalog(streaming), capable) == (streaming,)


def test_undeclared_optional_feature_is_unsupported() -> None:
    xml_11 = case("xml-11", features={"XML_1.1"})

    assert inapplicability(xml_11, XSLT30_NON_STREAMING) is Outcome.UNSUPPORTED_FEATURE
    assert select_cases(catalog(xml_11), XSLT30_NON_STREAMING) == ()


def test_a_declined_feature_wins_over_an_unsupported_feature() -> None:
    mixed = case("mixed", features={"streaming", "XML_1.1"})

    assert inapplicability(mixed, XSLT30_NON_STREAMING) is Outcome.NOT_APPLICABLE


CAPABILITY_FEATURES = (
    "packages",
    "maps",
    "arrays",
    "higher_order_functions",
    "dynamic_evaluation",
    "backwards_compatibility",
    "serialization",
    "schema_aware",
    "namespace_axis",
    "dtd",
    "disabling_output_escaping",
)


@pytest.mark.parametrize("feature", CAPABILITY_FEATURES)
def test_each_declared_capability_is_required(feature: str) -> None:
    candidate = case(feature, features={feature})
    reduced = replace(
        XSLT30_NON_STREAMING,
        features=XSLT30_NON_STREAMING.features - {feature},
    )

    assert select_cases(catalog(candidate), XSLT30_NON_STREAMING) == (candidate,)
    assert inapplicability(candidate, reduced) is Outcome.UNSUPPORTED_FEATURE


@pytest.mark.parametrize(
    ("language", "spec", "applicable"),
    [
        ("3.0", "XSLT30", True),
        ("3.0", "XSLT30+", True),
        ("3.0", "XSLT20+", True),
        ("3.0", "XSLT10+", True),
        ("3.0", "XSLT20", False),
        ("3.0", "XSLT10", False),
        ("2.0", "XSLT20", True),
        ("2.0", "XSLT20+", True),
        ("2.0", "XSLT10+", True),
        ("2.0", "XSLT30", False),
        ("2.0", "XSLT30+", False),
        ("1.0", "XSLT10", True),
        ("1.0", "XSLT10+", True),
        ("1.0", "XSLT20+", False),
    ],
)
def test_exact_spec_values_are_respected(language: str, spec: str, applicable: bool) -> None:
    candidate = case(f"spec-{spec}", specs=(spec,))
    profile = replace(XSLT30_NON_STREAMING, name=f"xslt{language}", language=language)

    assert (select_cases(catalog(candidate), profile) == (candidate,)) is applicable
    assert (inapplicability(candidate, profile) is None) is applicable


@pytest.mark.parametrize(
    ("language", "applicable"),
    [("1.0", True), ("2.0", True), ("3.0", False)],
)
def test_multi_token_spec_value_is_a_disjunction(language: str, applicable: bool) -> None:
    candidate = case("either", specs=("XSLT10 XSLT20",))
    profile = replace(XSLT30_NON_STREAMING, name=f"xslt{language}", language=language)

    assert (select_cases(catalog(candidate), profile) == (candidate,)) is applicable


def test_separate_spec_dependencies_are_all_required() -> None:
    candidate = case("conflicting", specs=("XSLT30+", "XSLT20"))

    assert inapplicability(candidate, XSLT30_NON_STREAMING) is Outcome.NOT_APPLICABLE
    xslt20 = replace(XSLT30_NON_STREAMING, name="xslt20", language="2.0")
    assert inapplicability(candidate, xslt20) is Outcome.NOT_APPLICABLE


def test_negative_spec_dependency_is_respected() -> None:
    not_xslt20 = case("not-xslt20", excluded_specs=("XSLT20",))

    assert inapplicability(not_xslt20, XSLT30_NON_STREAMING) is None
    xslt20 = replace(XSLT30_NON_STREAMING, name="xslt20", language="2.0")
    assert inapplicability(not_xslt20, xslt20) is Outcome.NOT_APPLICABLE


def test_unknown_spec_token_is_not_applicable() -> None:
    unknown = case("unknown", specs=("XSLT4",))
    missing = case("missing")
    missing = replace(
        missing,
        dependencies=(Dependency("spec", None),),
    )
    negative = replace(
        missing,
        name="negative-missing",
        dependencies=(Dependency("spec", None, False),),
    )

    assert inapplicability(unknown, XSLT30_NON_STREAMING) is Outcome.NOT_APPLICABLE
    assert inapplicability(missing, XSLT30_NON_STREAMING) is Outcome.NOT_APPLICABLE
    assert inapplicability(negative, XSLT30_NON_STREAMING) is None


def test_xpath_version_gates_xpath_31_cases() -> None:
    xpath_31 = case("xpath-31", features={"XPath_3.1"})

    assert select_cases(catalog(xpath_31), XSLT30_NON_STREAMING) == (xpath_31,)
    older = replace(XSLT30_NON_STREAMING, xpath_version="3.0")
    assert inapplicability(xpath_31, older) is Outcome.UNSUPPORTED_FEATURE


def test_xsd_version_gates_schema_requirements() -> None:
    xsd_11 = case("xsd-11", schema_versions={"1.1"})
    xsd_10 = case("xsd-10", schema_versions={"1.0"})

    assert select_cases(catalog(xsd_11, xsd_10), XSLT30_NON_STREAMING) == (xsd_11, xsd_10)
    older = replace(XSLT30_NON_STREAMING, xsd_version="1.0")
    assert inapplicability(xsd_11, older) is Outcome.UNSUPPORTED_FEATURE
    assert inapplicability(xsd_10, older) is None


def test_version_features_are_derived_from_profile_versions() -> None:
    assert XSLT30_NON_STREAMING.supports("XPath_3.0")
    assert XSLT30_NON_STREAMING.supports("XPath_3.1")
    assert XSLT30_NON_STREAMING.supports("XSD_1.0")
    assert XSLT30_NON_STREAMING.supports("XSD_1.1")
    assert not replace(XSLT30_NON_STREAMING, xpath_version="2.0").supports("XPath_3.1")
    assert not replace(XSLT30_NON_STREAMING, xsd_version="1.0").supports("XSD_1.1")


def test_select_cases_preserves_catalog_order() -> None:
    first = case("first")
    second = case("second")

    assert [item.name for item in select_cases(catalog(first, second), XSLT30_NON_STREAMING)] == [
        "first",
        "second",
    ]


@pytest.mark.parametrize("field", ["language", "xpath_version", "xsd_version"])
def test_profile_versions_must_be_dotted(field: str) -> None:
    arguments = {
        "name": "bad",
        "language": "3.0",
        "xpath_version": "3.1",
        "xsd_version": "1.1",
        "features": frozenset(),
    }
    arguments[field] = "three"

    with pytest.raises(ValueError, match=field):
        FeatureProfile(**arguments)


def test_profile_rejects_supported_and_declined_overlap() -> None:
    with pytest.raises(ValueError, match="dtd"):
        FeatureProfile(
            name="overlap",
            language="3.0",
            xpath_version="3.1",
            xsd_version="1.1",
            features=frozenset({"dtd"}),
            declined_features=frozenset({"dtd"}),
        )


def test_profile_normalises_feature_collections() -> None:
    profile = FeatureProfile(
        name="normalised",
        language="3.0",
        xpath_version="3.1",
        xsd_version="1.1",
        features={"dtd"},
        declined_features={"streaming"},
    )

    assert profile.features == frozenset({"dtd"})
    assert profile.declined_features == frozenset({"streaming"})


def test_native_profile_identity_and_capabilities() -> None:
    assert XSLT30_NON_STREAMING.language == "3.0"
    assert XSLT30_NON_STREAMING.xpath_version == "3.1"
    assert XSLT30_NON_STREAMING.xsd_version == "1.1"
    assert "streaming" in XSLT30_NON_STREAMING.declined_features
    assert "streaming" not in XSLT30_NON_STREAMING.features
    assert "schema_aware" in XSLT30_NON_STREAMING.features
