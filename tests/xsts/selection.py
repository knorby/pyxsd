"""Version profiles and expectation selection for the test suite.

The suite tags tests with whitespace-separated version tokens.  At suite,
set, group and test level the tokens are a disjunction (any match means the
test applies).  On an ``expected`` element the tokens are a conjunction (all
must be satisfied by the processor configuration).  A processor profile is a
set of tokens it claims; the default profiles are the two published XSD
versions.

Where a test carries both an unversioned expectation and version-tagged
expectations, the most specific applicable expectation wins: a version tag
matching the profile overrides the unversioned default.  Exactly one
expectation must result; anything else is a metadata error.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import Expected


class MetadataError(Exception):
    """Raised when a test's expectations cannot be resolved unambiguously."""


@dataclass(frozen=True)
class Profile:
    """A processor configuration described by the version tokens it supports."""

    name: str
    tokens: frozenset[str]
    description: str = ""

    def supports(self, tokens: tuple[str, ...]) -> bool:
        """Whether this profile satisfies a disjunctive version-token list.

        An absent or empty token list places no restriction, so it applies to
        every profile.
        """
        if not tokens:
            return True
        return bool(self.tokens.intersection(tokens))


#: XSD 1.1 processor profile.  Oracle: ``xmlschema.XMLSchema11``.
XSD11 = Profile(
    name="xsd11",
    tokens=frozenset({"1.1"}),
    description="XSD 1.1 Second Edition",
)

#: XSD 1.0 processor profile.  Oracle: ``xmlschema.XMLSchema10``.
XSD10 = Profile(
    name="xsd10",
    tokens=frozenset({"1.0"}),
    description="XSD 1.0 Second Edition",
)

PROFILES: dict[str, Profile] = {XSD11.name: XSD11, XSD10.name: XSD10}


def select_expected(expecteds: tuple[Expected, ...], profile: Profile) -> Expected | None:
    """Choose the one expectation that applies to *profile*.

    Version-tagged expectations matching the profile take precedence over
    unversioned ones.  Returns ``None`` when every expectation is gated to a
    version dimension the profile does not claim — the test is not applicable
    to this profile.  Raises :class:`MetadataError` when there is nothing to
    choose from or the choice is ambiguous.
    """
    if not expecteds:
        raise MetadataError("test has no expected outcome")
    tagged = [
        expected
        for expected in expecteds
        # An expected/@version is a conjunction, so every token it lists must
        # be supported by the profile.
        if expected.version and all(token in profile.tokens for token in expected.version)
    ]
    if len(tagged) == 1:
        return tagged[0]
    if len(tagged) > 1:
        raise MetadataError(
            "multiple version-tagged expectations match profile "
            f"{profile.name!r}: {_describe(tagged)}"
        )
    untagged = [expected for expected in expecteds if not expected.version]
    if len(untagged) == 1:
        return untagged[0]
    if not untagged:
        # All expectations are version-gated to dimensions this profile does
        # not claim; the test does not apply.
        return None
    raise MetadataError(
        f"multiple unversioned expectations for profile {profile.name!r}: {_describe(untagged)}"
    )


def _describe(expecteds: list[Expected]) -> str:
    return ", ".join(
        f"{e.validity!r} (version={' '.join(e.version) or 'any'})" for e in expecteds
    )


#: Expectation values whose outcome a Boolean conformance runner can check.
CHECKABLE_VALIDITY = frozenset({"valid", "invalid", "invalid-latent"})


def expected_is_valid(validity: str) -> bool | None:
    """Translate an expected validity into a Boolean, or ``None`` if uncheckable."""
    if validity in ("valid",):
        return True
    if validity in ("invalid", "invalid-latent"):
        return False
    return None
