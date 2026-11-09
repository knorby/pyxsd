"""Execute catalog cases and classify them honestly.

Selection happens before execution.  The executor only returns results or
raises an explicit execution error; :func:`tests.xslt.assertions.assert_case`
decides whether the result passed.  Inherited dependency combinations that no
single language version can satisfy are surfaced as ``corpus-dispute`` instead
of being silently dropped as not applicable.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from tests.xslt.assertions import ResultSnapshot, assert_case
from tests.xslt.model import Dependency, TestCase
from tests.xslt.outcomes import CaseResult, Outcome
from tests.xslt.selection import FeatureProfile, inapplicability

#: ``XSLT10``, ``XSLT20``, ``XSLT30+`` style dependency tokens.
_SPEC_TOKEN = re.compile(r"^XSLT(?P<major>\d)(?P<minor>\d)(?P<forward>\+)?$")


class CaseExecutionError(Exception):
    """An executor-reported XSLT error with a catalog-visible code."""

    def __init__(self, code: str, message: str = "") -> None:
        value = code.strip()
        if not value:
            raise ValueError("an execution error code is required")
        self.code = value
        self.message = message
        super().__init__(message or value)


def _token_matches(token: str, version: tuple[int, int]) -> bool:
    match = _SPEC_TOKEN.fullmatch(token)
    if match is None:
        return False
    required = (int(match["major"]), int(match["minor"]))
    if match["forward"]:
        return version >= required
    return version == required


def _spec_candidates(dependencies: Iterable[Dependency]) -> set[tuple[int, int]]:
    candidates: set[tuple[int, int]] = set()
    for dependency in dependencies:
        for token in (dependency.value or "").split():
            match = _SPEC_TOKEN.fullmatch(token)
            if match is not None:
                candidates.add((int(match["major"]), int(match["minor"])))
    if candidates:
        candidates.add(
            (max(major for major, _ in candidates), max(minor for _, minor in candidates) + 1)
        )
        candidates.add((max(major for major, _ in candidates) + 1, 0))
    return candidates


def _specs_satisfiable(dependencies: tuple[Dependency, ...]) -> bool:
    specs = tuple(dependency for dependency in dependencies if dependency.kind == "spec")
    candidates = _spec_candidates(specs)
    if not candidates:
        return True

    def dependency_satisfied(dependency: Dependency, version: tuple[int, int]) -> bool:
        tokens = [
            token
            for token in (dependency.value or "").split()
            if _SPEC_TOKEN.fullmatch(token) is not None
        ]
        if not tokens:
            return True
        matched = any(_token_matches(token, version) for token in tokens)
        if dependency.satisfied is False:
            return not matched
        return matched

    return any(
        all(dependency_satisfied(dependency, version) for dependency in specs)
        for version in candidates
    )


def unsatisfiable_dependencies(case: TestCase) -> str | None:
    """A diagnostic for a demonstrably unsatisfiable dependency combination.

    Separate ``spec`` dependencies are conjunctive, following the catalog
    schema and the upstream runner.  Tokens inside one dependency are
    alternatives.  A case that requires, for example, both exact ``XSLT20``
    and ``XSLT30+`` can never run under any profile; reporting it as merely
    not applicable would hide a corpus-data conflict.
    """
    specs = tuple(dependency for dependency in case.dependencies if dependency.kind == "spec")
    if specs and not _specs_satisfiable(case.dependencies):
        values = ", ".join(
            value for dependency in specs for value in (dependency.value or "").split()
        )
        return f"unsatisfiable inherited spec dependencies: {values}"

    positive = {
        dependency.value
        for dependency in case.dependencies
        if dependency.kind == "feature"
        and dependency.value is not None
        and dependency.satisfied is not False
    }
    negative = {
        dependency.value
        for dependency in case.dependencies
        if dependency.kind == "feature"
        and dependency.value is not None
        and dependency.satisfied is False
    }
    contradiction = positive & negative
    if contradiction:
        names = ", ".join(sorted(contradiction))
        return f"contradictory feature dependencies: {names}"
    return None


def _inapplicable_detail(case: TestCase, profile: FeatureProfile) -> str:
    unknown = sorted(feature for feature in case.features if not profile.supports(feature))
    if unknown:
        return unknown[0]
    for schema in case.environment.schemas:
        if tuple(int(part) for part in profile.xsd_version.split(".")) < tuple(
            int(part) for part in schema.xsd_version.split(".")
        ):
            return f"XSD version {schema.xsd_version}"
    return "optional feature"


def run_cases(
    cases: Iterable[TestCase],
    executor: Callable[[TestCase], ResultSnapshot],
    profile: FeatureProfile,
) -> tuple[CaseResult, ...]:
    """Execute every applicable case and return one explicit outcome each."""
    results: list[CaseResult] = []
    for case in cases:
        dispute = unsatisfiable_dependencies(case)
        if dispute is not None:
            results.append(CaseResult.corpus_dispute(case.name, detail=dispute))
            continue

        reason = inapplicability(case, profile)
        if reason is Outcome.NOT_APPLICABLE:
            results.append(
                CaseResult.not_applicable(case.name, reason="outside the declared feature profile")
            )
            continue
        if reason is Outcome.UNSUPPORTED_FEATURE:
            results.append(
                CaseResult.unsupported_feature(
                    case.name, feature=_inapplicable_detail(case, profile)
                )
            )
            continue

        try:
            snapshot = executor(case)
        except CaseExecutionError as exc:
            snapshot = ResultSnapshot(error_code=exc.code, error_message=exc.message)
        except TimeoutError as exc:
            results.append(CaseResult.timeout(case.name, detail=str(exc)))
            continue
        results.append(assert_case(case, snapshot))
    return tuple(results)
