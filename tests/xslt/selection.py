"""Applicability selection from the catalog's declared metadata.

The pinned catalog declares applicability through ``spec``, ``feature`` and
``unicode-version`` dependencies and through the ``xsd-version`` of source
schemas.  Selection consults those four declarations.  It never inspects
stylesheet syntax, source documents, filenames, or test-set names, so a case
that merely uses streamable constructs remains applicable to the non-streaming
profile.

A case that needs an optional feature the profile deliberately excludes (for
example streaming) is not applicable.  A case that needs a capability nobody
declared (yet) is unsupported, which is a different, honest outcome.

A ``unicode-version`` dependency is honoured only when the profile declares a
Unicode version: a profile that claims no version cannot exclude a
version-pinned case.  XSD 1.1 Part 2 §G.4.2.2 and F&O 3.1 §1.1 make the tables
an implementation choose ·implementation-defined·, so grading one version
against a case that pins another has no conformance meaning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tests.xslt.model import Catalog, Dependency, TestCase
from tests.xslt.outcomes import Outcome

#: ``XSLT30`` and ``XSLT30+`` style version tokens.
_SPEC_TOKEN = re.compile(r"^XSLT(?P<major>\d)(?P<minor>\d)(?P<forward>\+)?$")

#: Versioned feature names such as ``XPath_3.1`` and ``XSD_1.1``.
_VERSION_FEATURE = re.compile(r"^(?P<axis>XPath|XSD)_(?P<major>\d+)\.(?P<minor>\d+)$")

#: A dotted major.minor version.
_VERSION = re.compile(r"^\d+\.\d+$")

#: A dotted version with at least two components: ``9.0``, ``14.0.0``.
_UNICODE_VERSION = re.compile(r"^\d+(?:\.\d+)+$")


def _version(value: str) -> tuple[int, int]:
    major, minor = value.split(".")
    return int(major), int(minor)


@dataclass(frozen=True)
class FeatureProfile:
    """A processor profile described by its declared capabilities.

    ``language``, ``xpath_version`` and ``xsd_version`` are the versions the
    profile implements; ``unicode_version`` is the version of the Unicode
    character tables it grades against, or ``""`` when it makes no claim.
    ``features`` names the optional capabilities it provides; :meth:`supports`
    derives ``XPath_3.1`` and ``XSD_1.1`` from the version fields instead of
    duplicating them.  ``declined_features`` names capabilities deliberately
    outside the profile, which keeps them distinct from capabilities that are
    merely not implemented yet.  Both collections are normalised to immutable
    frozensets.
    """

    name: str
    language: str
    xpath_version: str
    xsd_version: str
    features: frozenset[str]
    declined_features: frozenset[str] = frozenset()
    unicode_version: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", frozenset(self.features))
        object.__setattr__(self, "declined_features", frozenset(self.declined_features))
        for dimension, value in (
            ("language", self.language),
            ("xpath_version", self.xpath_version),
            ("xsd_version", self.xsd_version),
        ):
            if not _VERSION.fullmatch(value):
                raise ValueError(f"{dimension} must be a dotted version, got {value!r}")
        # An empty `unicode_version` is the profile making no version claim.
        if self.unicode_version and not _UNICODE_VERSION.fullmatch(self.unicode_version):
            raise ValueError(
                f"unicode_version must be a dotted version, got {self.unicode_version!r}"
            )
        overlap = self.features & self.declined_features
        if overlap:
            names = ", ".join(sorted(overlap))
            raise ValueError(f"features cannot be both supported and declined: {names}")

    def supports(self, feature: str) -> bool:
        """Whether the profile provides one declared catalog feature."""
        match = _VERSION_FEATURE.fullmatch(feature)
        if match is None:
            return feature in self.features
        version = self.xpath_version if match["axis"] == "XPath" else self.xsd_version
        return _version(version) >= (int(match["major"]), int(match["minor"]))

    def declines(self, feature: str) -> bool:
        """Whether the profile names one feature as deliberately out of scope."""
        return feature in self.declined_features


def _spec_token_matches(token: str, language: str) -> bool:
    match = _SPEC_TOKEN.fullmatch(token)
    if match is None:
        return False
    required = (int(match["major"]), int(match["minor"]))
    actual = _version(language)
    if match["forward"]:
        return actual >= required
    return actual == required


def _spec_dependency_satisfied(dependency: Dependency, language: str) -> bool:
    value = dependency.value or ""
    matched = any(_spec_token_matches(token, language) for token in value.split())
    if dependency.satisfied is False:
        return not matched
    return matched


def _specs_satisfied(case: TestCase, profile: FeatureProfile) -> bool:
    return all(
        _spec_dependency_satisfied(dependency, profile.language)
        for dependency in case.dependencies
        if dependency.kind == "spec"
    )


def _unicode_version_matches(declared: str, engine: str) -> bool:
    """Whether *declared* names the same Unicode version as *engine*.

    Only the first two components are compared: the catalog's ``value`` is
    ``major.minor`` (``9.0``) while an engine reports ``major.minor.patch``
    (``14.0.0``).  Table versions do not differ in their patch release.
    """
    return _version(".".join(declared.split(".")[:2])) == _version(".".join(engine.split(".")[:2]))


def _unicode_version_satisfied(dependency: Dependency, engine: str) -> bool:
    """Whether one ``unicode-version`` dependency holds for *engine*.

    A dependency that names no value, or a profile that declares no version,
    cannot constrain the case, so both match.  A malformed value — the catalog
    schema wants exactly ``major.minor`` — is likewise not comparable, and a
    value that cannot be compared must not exclude a case.
    ``satisfied="false"`` inverts the comparison, as it does for every other
    dependency kind.
    """
    declared = dependency.value or ""
    if not declared or not engine:
        return True
    if _VERSION.fullmatch(declared) is None:
        return True
    matched = _unicode_version_matches(declared, engine)
    if dependency.satisfied is False:
        return not matched
    return matched


def _unicode_versions_satisfied(case: TestCase, profile: FeatureProfile) -> bool:
    return all(
        _unicode_version_satisfied(dependency, profile.unicode_version)
        for dependency in case.dependencies
        if dependency.kind == "unicode-version"
    )


def inapplicability(case: TestCase, profile: FeatureProfile) -> Outcome | None:
    """Why *case* is outside *profile*, or ``None`` when it is applicable.

    Returns :attr:`Outcome.NOT_APPLICABLE` for a language, capability or Unicode
    version the profile deliberately excludes, and
    :attr:`Outcome.UNSUPPORTED_FEATURE` for a declared capability the profile
    does not implement.  The Unicode-version gate runs last, so a version-pinned
    case that also needs a feature or schema the profile lacks still reports
    ``UNSUPPORTED_FEATURE``.
    """
    if not _specs_satisfied(case, profile):
        return Outcome.NOT_APPLICABLE
    if any(profile.supports(feature) for feature in case.feature_exclusions):
        return Outcome.NOT_APPLICABLE
    required = case.features
    if any(profile.declines(feature) for feature in required):
        return Outcome.NOT_APPLICABLE
    if any(not profile.supports(feature) for feature in required):
        return Outcome.UNSUPPORTED_FEATURE
    for schema in case.environment.schemas:
        if _version(profile.xsd_version) < _version(schema.xsd_version):
            return Outcome.UNSUPPORTED_FEATURE
    if not _unicode_versions_satisfied(case, profile):
        return Outcome.NOT_APPLICABLE
    return None


def select_cases(catalog: Catalog, profile: FeatureProfile) -> tuple[TestCase, ...]:
    """Every case applicable to *profile*, in catalog order."""
    return tuple(case for case in catalog.cases.values() if inapplicability(case, profile) is None)


#: Capabilities the XSLT 3.0 target profile claims in addition to the versions.
#:
#: The pinned catalog gates package, map and array behavior through ``XSLT30+``
#: and ``XPath_3.1`` rather than dedicated feature names.  ``packages``,
#: ``maps`` and ``arrays`` are kept in the vocabulary so a catalog revision or
#: a narrower profile can state those capabilities directly.
_XSLT30_FEATURES = frozenset(
    {
        "schema_aware",
        "serialization",
        "higher_order_functions",
        "dynamic_evaluation",
        "backwards_compatibility",
        "namespace_axis",
        "dtd",
        "disabling_output_escaping",
        "built_in_derived_types",
        "HTML4",
        "HTML5",
        "packages",
        "maps",
        "arrays",
    }
)

#: XSLT 3.0 target profile: schema-aware XPath 3.1 with no streaming claim.
XSLT30_NON_STREAMING = FeatureProfile(
    name="xslt30",
    language="3.0",
    xpath_version="3.1",
    xsd_version="1.1",
    features=_XSLT30_FEATURES,
    declined_features=frozenset({"streaming", "streaming-fallback", "xquery_invocation"}),
    description=(
        "XSLT 3.0 with XPath 3.1, XDM 3.1 and XSD 1.1; streaming is outside the declared profile"
    ),
)
