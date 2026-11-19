"""Applicability selection from declared catalog dependencies.

Selection is metadata-only.  These tests build cases with controlled
dependencies and prove that applicability comes from ``spec``, ``feature`` and
``unicode-version`` declarations (plus declared schema versions), never from
filenames, source documents, streamable syntax, or an observed verdict.
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
from tests.xslt.runner import unsatisfiable_dependencies
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
    unicode_versions: Iterable[str] = (),
    excluded_unicode_versions: Iterable[str] = (),
    numbering: Iterable[tuple[str, str | None]] = (),
    source_document: bool = False,
    schema_versions: Iterable[str] = (),
) -> CatalogCase:
    """Build a catalog case whose applicability metadata is fully controlled."""
    dependencies = [Dependency("spec", spec) for spec in specs]
    dependencies.extend(Dependency("spec", spec, False) for spec in excluded_specs)
    dependencies.extend(Dependency("feature", feature) for feature in features)
    dependencies.extend(Dependency("feature", feature, False) for feature in exclusions)
    dependencies.extend(Dependency("unicode-version", version) for version in unicode_versions)
    dependencies.extend(
        Dependency("unicode-version", version, False) for version in excluded_unicode_versions
    )
    dependencies.extend(
        Dependency("combinations_for_numbering", value, range=bounds) for value, bounds in numbering
    )
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


def test_unicode_version_dependency_is_not_applicable_to_another_version() -> None:
    pinned = case("pinned", unicode_versions=("9.0",))
    modern = replace(XSLT30_NON_STREAMING, unicode_version="14.0")

    assert inapplicability(pinned, modern) is Outcome.NOT_APPLICABLE
    assert select_cases(catalog(pinned), modern) == ()


def test_a_case_whose_unicode_version_matches_stays_applicable() -> None:
    pinned = case("pinned", unicode_versions=("14.0",))
    modern = replace(XSLT30_NON_STREAMING, unicode_version="14.0")

    assert inapplicability(pinned, modern) is None
    assert select_cases(catalog(pinned), modern) == (pinned,)


def test_unicode_version_dependency_without_a_value_is_applicable() -> None:
    unconstrained = replace(
        case("unconstrained"),
        dependencies=(Dependency("unicode-version", None),),
    )
    modern = replace(XSLT30_NON_STREAMING, unicode_version="14.0")

    assert inapplicability(unconstrained, modern) is None


def test_unicode_version_dependency_with_a_malformed_value_is_applicable() -> None:
    """A value the profile cannot compare must not constrain the case."""
    modern = replace(XSLT30_NON_STREAMING, unicode_version="14.0")

    for value in ("9", "nine", "9.0.1"):
        malformed = case(f"malformed-{value}", unicode_versions=(value,))
        assert inapplicability(malformed, modern) is None, value


def test_negative_unicode_version_dependency_inverts_the_match() -> None:
    not_nine = case("not-nine", excluded_unicode_versions=("9.0",))
    nine = replace(XSLT30_NON_STREAMING, unicode_version="9.0")
    modern = replace(XSLT30_NON_STREAMING, unicode_version="14.0")

    assert inapplicability(not_nine, modern) is None
    assert inapplicability(not_nine, nine) is Outcome.NOT_APPLICABLE


def test_unicode_version_dependency_is_not_evaluated_without_a_profile_version() -> None:
    pinned = case("pinned", unicode_versions=("9.0",))

    assert XSLT30_NON_STREAMING.unicode_version == ""
    assert inapplicability(pinned, XSLT30_NON_STREAMING) is None
    assert select_cases(catalog(pinned), XSLT30_NON_STREAMING) == (pinned,)


def test_unicode_version_gate_never_hides_an_unsupported_feature() -> None:
    pinned = case("pinned", features={"XML_1.1"}, unicode_versions=("9.0",))
    modern = replace(XSLT30_NON_STREAMING, unicode_version="14.0")

    assert inapplicability(pinned, modern) is Outcome.UNSUPPORTED_FEATURE


def test_unicode_version_gate_never_hides_an_unsupported_schema_version() -> None:
    pinned = case("pinned", schema_versions={"1.2"}, unicode_versions=("9.0",))
    modern = replace(XSLT30_NON_STREAMING, unicode_version="14.0")

    assert inapplicability(pinned, modern) is Outcome.UNSUPPORTED_FEATURE


def test_unsatisfiable_specs_still_dispute_with_a_unicode_version_dependency() -> None:
    disputed = case("disputed", specs=("XSLT30+", "XSLT20"))
    pinned = case("disputed", specs=("XSLT30+", "XSLT20"), unicode_versions=("9.0",))

    assert unsatisfiable_dependencies(disputed) == unsatisfiable_dependencies(pinned)
    assert unsatisfiable_dependencies(pinned) == (
        "unsatisfiable inherited spec dependencies: XSLT30+, XSLT20"
    )


def test_clearing_the_profile_unicode_version_restores_only_the_version_gated_cases() -> None:
    gated = case("gated", unicode_versions=("9.0",))
    also_unsupported = case("also-unsupported", features={"XML_1.1"}, unicode_versions=("9.0",))
    modern = replace(XSLT30_NON_STREAMING, unicode_version="14.0")

    assert select_cases(catalog(gated, also_unsupported), modern) == ()
    assert select_cases(catalog(gated, also_unsupported), XSLT30_NON_STREAMING) == (gated,)


def test_version_features_are_derived_from_profile_versions() -> None:
    assert XSLT30_NON_STREAMING.supports("XPath_3.0")
    assert XSLT30_NON_STREAMING.supports("XPath_3.1")
    assert XSLT30_NON_STREAMING.supports("XSD_1.0")
    assert XSLT30_NON_STREAMING.supports("XSD_1.1")
    assert not replace(XSLT30_NON_STREAMING, xpath_version="2.0").supports("XPath_3.1")
    assert not replace(XSLT30_NON_STREAMING, xsd_version="1.0").supports("XSD_1.1")


def numbering_profile(sequence: str = "CIRCLED DIGIT ONE") -> FeatureProfile:
    """A profile declaring one verified numbering sequence range."""
    return replace(XSLT30_NON_STREAMING, numbering_sequences={sequence: (1, 20)})


def test_a_numbering_combination_without_a_declaration_is_not_applicable() -> None:
    """A profile that declares no optional numbering sequence runs none of them."""
    bare = FeatureProfile(
        name="bare",
        language="3.0",
        xpath_version="3.1",
        xsd_version="1.1",
        features=frozenset(),
    )
    gated = case("gated", numbering=(("CIRCLED DIGIT ONE", "1 to 10"),))

    assert bare.numbering_sequences == {}
    assert inapplicability(gated, bare) is Outcome.NOT_APPLICABLE
    assert select_cases(catalog(gated), bare) == ()


def test_the_shipped_profile_leaves_an_unimplemented_sequence_out() -> None:
    """Greek numbering is not implemented, so its unbounded request is excluded."""
    greek = case("greek", numbering=(("GREEK SMALL LETTER ALPHA (ALPHABETIC)", None),))

    assert inapplicability(greek, XSLT30_NON_STREAMING) is Outcome.NOT_APPLICABLE


def test_a_declared_numbering_range_admits_contained_requests() -> None:
    profile = numbering_profile()

    for requested in ("1 to 9", "1 to 10", "1 to 20"):
        candidate = case(f"admitted-{requested}", numbering=(("CIRCLED DIGIT ONE", requested),))
        assert inapplicability(candidate, profile) is None, requested
        assert select_cases(catalog(candidate), profile) == (candidate,)


@pytest.mark.parametrize("requested", ["0 to 9", "0 to 20", "1 to 50", "0 to 1000"])
def test_a_declared_numbering_range_rejects_wider_requests(requested: str) -> None:
    profile = numbering_profile()

    outside = case(f"outside-{requested}", numbering=(("CIRCLED DIGIT ONE", requested),))

    assert inapplicability(outside, profile) is Outcome.NOT_APPLICABLE
    assert select_cases(catalog(outside), profile) == ()


@pytest.mark.parametrize("requested", [None, "unbounded", "not a range"])
def test_an_absent_or_unbounded_numbering_request_is_never_covered(
    requested: str | None,
) -> None:
    """Absent means unbounded, and no bounded declaration covers unbounded."""
    profile = numbering_profile()

    unbounded = case("unbounded", numbering=(("CIRCLED DIGIT ONE", requested),))

    assert inapplicability(unbounded, profile) is Outcome.NOT_APPLICABLE


def test_a_declared_sequence_covers_only_its_own_name() -> None:
    profile = numbering_profile()

    other = case("other", numbering=(("PARENTHESIZED DIGIT ONE", "1 to 9"),))

    assert inapplicability(other, profile) is Outcome.NOT_APPLICABLE


def test_every_numbering_dependency_must_be_covered() -> None:
    profile = numbering_profile()

    both = case(
        "both",
        numbering=(("CIRCLED DIGIT ONE", "1 to 9"), ("PARENTHESIZED DIGIT ONE", "1 to 9")),
    )

    assert inapplicability(both, profile) is Outcome.NOT_APPLICABLE


def test_a_negative_numbering_dependency_inverts_the_support_test() -> None:
    profile = numbering_profile()
    supported = replace(
        case("supported"),
        dependencies=(
            Dependency("combinations_for_numbering", "CIRCLED DIGIT ONE", False, "1 to 9"),
        ),
    )
    unsupported = replace(
        case("unsupported"),
        dependencies=(
            Dependency("combinations_for_numbering", "PARENTHESIZED DIGIT ONE", False, "1 to 9"),
        ),
    )

    assert inapplicability(supported, profile) is Outcome.NOT_APPLICABLE
    assert inapplicability(unsupported, profile) is None
    assert select_cases(catalog(unsupported), profile) == (unsupported,)


def test_a_numbering_dependency_without_a_sequence_name_is_not_applicable() -> None:
    nameless = replace(
        case("nameless"),
        dependencies=(Dependency("combinations_for_numbering", None, None, "1 to 9"),),
    )

    assert inapplicability(nameless, numbering_profile()) is Outcome.NOT_APPLICABLE


def test_numbering_sequence_declarations_are_validated() -> None:
    def profile(declaration: dict[str, tuple[int, int]]) -> FeatureProfile:
        return replace(XSLT30_NON_STREAMING, numbering_sequences=declaration)

    with pytest.raises(ValueError, match="empty"):
        profile({"": (1, 9)})
    with pytest.raises(ValueError, match="needs two bounds"):
        profile({"CIRCLED DIGIT ONE": 20})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="CIRCLED"):
        profile({"CIRCLED DIGIT ONE": (9, 1)})
    with pytest.raises(ValueError, match="CIRCLED"):
        profile({"CIRCLED DIGIT ONE": (1, 20.0)})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="CIRCLED"):
        profile({"CIRCLED DIGIT ONE": (1, True)})  # type: ignore[dict-item]


def test_numbering_sequence_declarations_are_immutable_and_copied() -> None:
    declaration = {"CIRCLED DIGIT ONE": (1, 20)}

    profile = replace(XSLT30_NON_STREAMING, numbering_sequences=declaration)
    declaration["CIRCLED DIGIT ONE"] = (0, 9)

    assert profile.numbering_sequences == {"CIRCLED DIGIT ONE": (1, 20)}
    with pytest.raises(TypeError):
        profile.numbering_sequences["DIGIT ONE COMMA"] = (1, 9)  # type: ignore[index]


def test_declared_numbering_sequences_are_exercised_by_catalog_cases() -> None:
    """Every declared range is verified by a case whose request it contains."""
    from tests.xslt.catalog import load_catalog

    pinned = load_catalog(Path(__file__).parent / "corpus" / "catalog.xml")
    requests: dict[str, set[str | None]] = {}
    for item in pinned.cases.values():
        for dependency in item.dependencies:
            if dependency.kind == "combinations_for_numbering" and dependency.value:
                requests.setdefault(dependency.value, set()).add(dependency.range)

    declared = XSLT30_NON_STREAMING.numbering_sequences
    assert set(declared) <= set(requests)
    for sequence, (low, high) in declared.items():
        verified = []
        for value in requests[sequence]:
            if value is None or " to " not in value:
                continue
            start, end = (int(part) for part in value.split(" to "))
            if low <= start and high >= end:
                verified.append(value)
        assert verified, sequence


def test_select_cases_preserves_catalog_order() -> None:
    first = case("first")
    second = case("second")

    assert [item.name for item in select_cases(catalog(first, second), XSLT30_NON_STREAMING)] == [
        "first",
        "second",
    ]


@pytest.mark.parametrize("field", ["language", "xpath_version", "xsd_version", "unicode_version"])
def test_profile_versions_must_be_dotted(field: str) -> None:
    arguments = {
        "name": "bad",
        "language": "3.0",
        "xpath_version": "3.1",
        "xsd_version": "1.1",
        "unicode_version": "14.0",
        "features": frozenset(),
    }
    arguments[field] = "three"

    with pytest.raises(ValueError, match=field):
        FeatureProfile(**arguments)


def test_a_profile_may_declare_no_unicode_version() -> None:
    """The empty string means "this profile makes no Unicode-version claim"."""
    profile = FeatureProfile(
        name="unclaimed",
        language="3.0",
        xpath_version="3.1",
        xsd_version="1.1",
        features=frozenset(),
    )

    assert profile.unicode_version == ""
    with pytest.raises(ValueError, match="unicode_version"):
        FeatureProfile(
            name="one-component",
            language="3.0",
            xpath_version="3.1",
            xsd_version="1.1",
            unicode_version="14",
            features=frozenset(),
        )


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
