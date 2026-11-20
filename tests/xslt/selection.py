"""Applicability selection from the catalog's declared metadata.

The pinned catalog declares applicability through ``spec``, ``feature`` and
``unicode-version`` dependencies, through the ``xsd-version`` of source
schemas, and through ``combinations_for_numbering``.  Selection consults those
five declarations.  It never inspects stylesheet syntax, source documents,
filenames, or test-set names, so a case that merely uses streamable constructs
remains applicable to the non-streaming profile.

A case that needs an optional feature the profile deliberately excludes (for
example streaming) is not applicable.  A case that needs a capability nobody
declared (yet) is unsupported, which is a different, honest outcome.

A ``unicode-version`` dependency is honoured only when the profile declares a
Unicode version: a profile that claims no version cannot exclude a
version-pinned case.  XSD 1.1 Part 2 §G.4.2.2 and F&O 3.1 §1.1 make the tables
an implementation choose ·implementation-defined·, so grading one version
against a case that pins another has no conformance meaning.

A ``combinations_for_numbering`` dependency is a capability declaration, not a
version: XSLT 3.0 §12.4 makes a numbering sequence beyond its listed tokens
·implementation-defined·, and the catalog's ``range`` says how much of one
sequence a case asks the processor to support.  A profile declares the
sequences it implements and the inclusive range each one covers; a case is
applicable only when every such dependency names a declared sequence whose
range contains the requested one.  An absent or ``unbounded`` request is
unbounded, which no bounded declaration covers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

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

#: The inclusive bounds a ``combinations_for_numbering/@range`` states.
_NUMBERING_RANGE = re.compile(r"^(?P<low>\d+)\s+to\s+(?P<high>\d+)$")


def _version(value: str) -> tuple[int, int]:
    major, minor = value.split(".")
    return int(major), int(minor)


def _numbering_sequences(
    declaration: Mapping[str, tuple[int, int]],
) -> Mapping[str, tuple[int, int]]:
    """Validate one numbering-sequence declaration and freeze it.

    A name must be non-empty and a range must be two integers with
    ``low <= high``; a declaration that cannot describe a capability would
    silently admit or exclude cases, so it is rejected here.  ``bool`` is an
    ``int`` subclass but never a bound.
    """
    validated: dict[str, tuple[int, int]] = {}
    for name, bounds in declaration.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"a numbering sequence name is empty: {name!r}")
        try:
            low, high = bounds
        except (TypeError, ValueError):
            raise ValueError(
                f"the numbering sequence {name!r} needs two bounds, got {bounds!r}"
            ) from None
        for bound in (low, high):
            if isinstance(bound, bool) or not isinstance(bound, int):
                raise ValueError(
                    f"the numbering sequence {name!r} has a non-integer bound {bound!r}"
                )
        if low > high:
            raise ValueError(f"the numbering sequence {name!r} has an empty range {low} to {high}")
        validated[name] = (low, high)
    return MappingProxyType(validated)


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

    ``numbering_sequences`` declares the optional XSLT 3.0 §12.4 numbering
    sequences the profile implements, each mapped to the inclusive range of
    values it supports.  It is empty by default — a profile claims no optional
    sequence it has not verified — and :func:`inapplicability` excludes a case
    whose ``combinations_for_numbering`` request the declaration does not
    contain.
    """

    name: str
    language: str
    xpath_version: str
    xsd_version: str
    features: frozenset[str]
    declined_features: frozenset[str] = frozenset()
    unicode_version: str = ""
    description: str = ""
    numbering_sequences: Mapping[str, tuple[int, int]] = field(default_factory=dict)

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
        object.__setattr__(
            self, "numbering_sequences", _numbering_sequences(self.numbering_sequences)
        )

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


def _numbering_bounds(value: str | None) -> tuple[int, int] | None:
    """The inclusive bounds a numbering dependency's ``@range`` requests.

    ``None`` means unbounded: the attribute is absent, says ``unbounded``, or
    cannot be read.  The catalog schema enumerates the readable forms, so an
    unreadable one is a catalog defect; claiming an unknown window would be
    worse than leaving the case unnumbered, and both leave the case outside the
    profile.
    """
    if value is None:
        return None
    match = _NUMBERING_RANGE.fullmatch(value.strip())
    if match is None:
        return None
    return int(match["low"]), int(match["high"])


def _numbering_satisfied(case: TestCase, profile: FeatureProfile) -> bool:
    """Whether every declared numbering capability *case* asks for is present."""
    return all(
        _numbering_dependency_satisfied(dependency, profile)
        for dependency in case.dependencies
        if dependency.kind == "combinations_for_numbering"
    )


def _numbering_dependency_satisfied(dependency: Dependency, profile: FeatureProfile) -> bool:
    """Whether one ``combinations_for_numbering`` dependency holds.

    The dependency names a numbering sequence in ``value`` and, in ``range``, the
    values a case asks the processor to number.  It holds only when the profile
    declares that sequence with a range containing the request; a
    ``satisfied="false"`` dependency inverts the test, as every other kind does.
    """
    required = _numbering_bounds(dependency.range)
    declared = profile.numbering_sequences.get(dependency.value or "")
    supported = (
        required is not None
        and declared is not None
        and declared[0] <= required[0]
        and declared[1] >= required[1]
    )
    if dependency.satisfied is False:
        return not supported
    return supported


def inapplicability(case: TestCase, profile: FeatureProfile) -> Outcome | None:
    """Why *case* is outside *profile*, or ``None`` when it is applicable.

    Returns :attr:`Outcome.NOT_APPLICABLE` for a language, capability, numbering
    sequence or Unicode version the profile deliberately excludes, and
    :attr:`Outcome.UNSUPPORTED_FEATURE` for a declared capability the profile
    does not implement.  The Unicode-version gate runs last, so a version-pinned
    case that also needs a feature, schema or numbering sequence the profile
    lacks still reports ``UNSUPPORTED_FEATURE``.
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
    if not _numbering_satisfied(case, profile):
        return Outcome.NOT_APPLICABLE
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

#: Optional XSLT 3.0 section 12.4 numbering sequences this engine implements.
#:
#: A single character whose numeric value is 1 in the ``No``/``Nl`` categories
#: names a contiguous codepoint run, and each entry is the inclusive range that
#: run covers: value *n* is the *n - 1* codepoints above the token.  A run is
#: bounded by its own codepoints, so a shorter entry is the whole sequence, not
#: a partial claim.  Only sequences a catalog case exercises are declared, and
#: each entry cites the cases whose ``combinations_for_numbering`` request it
#: contains; a request for value 0 (or an unbounded request) stays outside every
#: declaration, so those cases are not applicable rather than failures.
NUMBERING_SEQUENCES: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        # number-5003/5004 (1 to 9), 5007/5008 (1 to 10), 5011/5012 (1 to 20).
        "CIRCLED DIGIT ONE": (1, 20),
        # number-5017/5018 (1 to 9), 5019/5020 (1 to 10), 5021/5022 and 0820 (1 to 20).
        "PARENTHESIZED DIGIT ONE": (1, 20),
        # number-5023 to 5026 (1 to 9), 5029/5030 (1 to 10), 5033/5034 and 0821 (1 to 20).
        "DIGIT ONE FULL STOP": (1, 20),
        # number-5047/5048 (1 to 9), 5049/5050 (1 to 10).
        "DOUBLE CIRCLED DIGIT ONE": (1, 10),
        # number-5037/5038 (1 to 9), 5041/5042 (1 to 10).
        "DINGBAT NEGATIVE CIRCLED DIGIT ONE": (1, 10),
        # number-5053/5054 (1 to 9), 5057/5058 (1 to 10).
        "DINGBAT CIRCLED SANS-SERIF DIGIT ONE": (1, 10),
        # number-5059 to 5062 (1 to 9), 5065/5066 (1 to 10).
        "DINGBAT NEGATIVE CIRCLED SANS-SERIF DIGIT ONE": (1, 10),
        # number-5067/5068 (1 to 9), 5069/5070 (1 to 10).
        "PARENTHESIZED IDEOGRAPH ONE": (1, 10),
        # number-5071/5072 (1 to 9), 5073/5074 (1 to 10).
        "CIRCLED IDEOGRAPH ONE": (1, 10),
        # number-5075/5076 (1 to 9), 5077/5078 (1 to 10).
        "AEGEAN NUMBER ONE": (1, 10),
        # number-5079/5080 (1 to 9), 5081/5082 (1 to 10).
        "COPTIC EPACT DIGIT ONE": (1, 10),
        # number-5083/5084 (1 to 9), 5085/5086 (1 to 10).
        "RUMI DIGIT ONE": (1, 10),
        # number-5087/5088 (1 to 9), 5089/5090 (1 to 10).
        "BRAHMI NUMBER ONE": (1, 10),
        # number-5091/5092 (1 to 9), 5093/5094 (1 to 10).
        "SINHALA ARCHAIC DIGIT ONE": (1, 10),
        # number-5095/5096 (1 to 9).
        "COUNTING ROD UNIT DIGIT ONE": (1, 10),
        # number-5097/5098 (1 to 9).
        "MENDE KIKAKUI DIGIT ONE": (1, 9),
        # number-5099 to 5102 (0 to 9 and 1 to 9); DIGIT ZERO COMMA precedes it.
        "DIGIT ONE COMMA": (0, 9),
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
    numbering_sequences=NUMBERING_SEQUENCES,
)
