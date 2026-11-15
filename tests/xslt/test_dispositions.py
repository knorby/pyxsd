"""Residual dispositions for checked-in XSLT conformance cases.

Dispositions explain *why* a non-passing case is understood without ever
changing its outcome.  Validation reads a baseline only when one is supplied,
so the contract can be exercised without corpus data, and an entry whose cases
start passing remains valid without describing an active residual.  The census
keeps every other non-passing case visibly unclassified rather than inventing a
rationale for it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from tests.xslt.baseline import BASELINE_DIR, Baseline, load_baseline
from tests.xslt.dispositions import (
    CATEGORIES,
    PROFILES,
    Disposition,
    DispositionError,
    active_entries,
    load_dispositions,
    unclassified,
    validate_dispositions,
)
from tests.xslt.outcomes import Outcome

DISPOSITIONS = Path(__file__).resolve().parent / "dispositions.toml"

VALID_TOML = """\
[[dispositions]]
id = "gap-1"
title = "One understood residual"
category = "implementation-gap"
profiles = ["xslt10"]
cases = ["bug/bug-0001"]
links = ["https://example.org/spec#section"]
reason = "Documented gap."
"""


def make_entry(**overrides: object) -> Disposition:
    """A minimal valid entry for synthetic structural checks."""
    fields: dict[str, object] = {
        "id": "gap-1",
        "title": "One understood residual",
        "category": "implementation-gap",
        "profiles": ("xslt10",),
        "cases": ("bug/bug-0001",),
        "reason": "Documented gap.",
    }
    fields.update(overrides)
    return Disposition(**fields)  # type: ignore[arg-type]


def make_baseline(cases: dict[str, str]) -> Baseline:
    return Baseline(meta={}, cases=cases)


def write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "dispositions.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------- loading


def test_load_reads_entries_with_normalized_tuples(tmp_path: Path) -> None:
    path = write_toml(tmp_path, VALID_TOML)

    assert load_dispositions(path) == (
        Disposition(
            id="gap-1",
            title="One understood residual",
            category="implementation-gap",
            profiles=("xslt10",),
            cases=("bug/bug-0001",),
            reason="Documented gap.",
            links=("https://example.org/spec#section",),
        ),
    )


def test_load_defaults_links_to_empty(tmp_path: Path) -> None:
    body = VALID_TOML.replace('links = ["https://example.org/spec#section"]\n', "")
    path = write_toml(tmp_path, body)

    (entry,) = load_dispositions(path)

    assert entry.links == ()


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(DispositionError, match="cannot read"):
        load_dispositions(tmp_path / "absent.toml")


def test_load_malformed_toml_raises(tmp_path: Path) -> None:
    path = write_toml(tmp_path, "[dispositions\n")

    with pytest.raises(DispositionError, match="malformed TOML"):
        load_dispositions(path)


def test_load_without_dispositions_array_raises(tmp_path: Path) -> None:
    path = write_toml(tmp_path, '[meta]\nprofile = "xslt10"\n')

    with pytest.raises(DispositionError, match=r"\[\[dispositions\]\] array of tables"):
        load_dispositions(path)


def test_load_rejects_non_table_entry(tmp_path: Path) -> None:
    path = write_toml(tmp_path, "dispositions = [1]\n")

    with pytest.raises(DispositionError, match="entry 1 is not a table"):
        load_dispositions(path)


@pytest.mark.parametrize(
    "scalar",
    [
        "id = 1",
        # A TOML date or timestamp parses to a datetime, never to the string
        # field the contract requires.
        "id = 1979-05-27T07:32:00Z",
    ],
)
def test_load_rejects_non_string_scalar(tmp_path: Path, scalar: str) -> None:
    body = VALID_TOML.replace('id = "gap-1"', scalar)
    path = write_toml(tmp_path, body)

    with pytest.raises(DispositionError, match="entry 1: id must be a string"):
        load_dispositions(path)


def test_load_rejects_non_array_list_field(tmp_path: Path) -> None:
    body = VALID_TOML.replace('cases = ["bug/bug-0001"]', 'cases = "bug/bug-0001"')
    path = write_toml(tmp_path, body)

    with pytest.raises(DispositionError, match="entry 1: cases must be an array of strings"):
        load_dispositions(path)


def test_load_rejects_non_string_list_member(tmp_path: Path) -> None:
    body = VALID_TOML.replace('cases = ["bug/bug-0001"]', "cases = [1]")
    path = write_toml(tmp_path, body)

    with pytest.raises(DispositionError, match="entry 1: cases must be an array of strings"):
        load_dispositions(path)


def test_load_rejects_unknown_field(tmp_path: Path) -> None:
    body = VALID_TOML.replace(
        'cases = ["bug/bug-0001"]',
        'cases = ["bug/bug-0001"]\ncase = ["bug/bug-0001"]',
    )
    path = write_toml(tmp_path, body)

    with pytest.raises(DispositionError, match="entry 1: unknown field 'case'"):
        load_dispositions(path)


def test_load_validates_against_a_supplied_baseline(tmp_path: Path) -> None:
    path = write_toml(tmp_path, VALID_TOML)
    baselines = {"xslt10": make_baseline({"bug/bug-0001": "fail"})}

    (entry,) = load_dispositions(path, baselines=baselines)

    assert entry.cases == ("bug/bug-0001",)


def test_load_checks_baseline_membership_only_when_a_baseline_is_supplied(
    tmp_path: Path,
) -> None:
    path = write_toml(tmp_path, VALID_TOML)

    # No baseline: the structural contract alone lets a case through.
    assert load_dispositions(path)[0].cases == ("bug/bug-0001",)
    with pytest.raises(DispositionError, match="unknown case 'bug/bug-0001'"):
        load_dispositions(path, baselines={"xslt10": make_baseline({})})


def test_load_always_runs_structural_validation(tmp_path: Path) -> None:
    body = VALID_TOML.replace('reason = "Documented gap."', 'reason = "  "')
    path = write_toml(tmp_path, body)

    with pytest.raises(DispositionError, match="gap-1: reason is blank"):
        load_dispositions(path)


# ------------------------------------------------------------- validation


def test_validate_accepts_a_current_failure() -> None:
    baselines = {"xslt10": make_baseline({"bug/bug-0001": "fail"})}

    assert validate_dispositions((make_entry(),), baselines) is None


def test_validate_does_not_rewrite_baseline_results() -> None:
    baselines = {"xslt10": make_baseline({"bug/bug-0001": "fail"})}
    before = dict(baselines["xslt10"].cases)

    validate_dispositions((make_entry(),), baselines)

    assert baselines["xslt10"].cases == before


def test_validate_without_baselines_skips_membership_but_keeps_structure() -> None:
    # The case is absent from every real baseline; without one supplied only
    # the structural contract applies.
    assert validate_dispositions((make_entry(),)) is None


def test_validate_rejects_duplicate_tracking_ids() -> None:
    baselines = {"xslt10": make_baseline({"bug/bug-0001": "fail", "bug/bug-0002": "fail"})}
    entries = (make_entry(), make_entry(id="gap-1", cases=("bug/bug-0002",)))

    with pytest.raises(DispositionError, match="gap-1: duplicate tracking id"):
        validate_dispositions(entries, baselines)


def test_validate_rejects_case_claimed_twice_in_one_profile() -> None:
    baselines = {"xslt10": make_baseline({"bug/bug-0001": "fail"})}
    entries = (make_entry(), make_entry(id="gap-2"))

    with pytest.raises(DispositionError, match="already claimed by 'gap-1'"):
        validate_dispositions(entries, baselines)


def test_validate_allows_one_case_claimed_in_two_profiles() -> None:
    baselines = {
        "xslt10": make_baseline({"bug/bug-0001": "fail"}),
        "xslt20": make_baseline({"bug/bug-0001": "fail"}),
    }
    entries = (
        make_entry(profiles=("xslt10",)),
        make_entry(id="gap-2", profiles=("xslt20",)),
    )

    assert validate_dispositions(entries, baselines) is None


def test_validate_rejects_duplicate_case_within_one_entry() -> None:
    baselines = {"xslt10": make_baseline({"bug/bug-0001": "fail"})}
    entry = make_entry(cases=("bug/bug-0001", "bug/bug-0001"))

    with pytest.raises(DispositionError, match=r"gap-1: duplicate case 'bug/bug-0001'"):
        validate_dispositions((entry,), baselines)


def test_validate_rejects_unknown_case_id() -> None:
    baselines = {"xslt10": make_baseline({"bug/bug-0001": "fail"})}
    entry = make_entry(cases=("bug/bug-0001", "bug/bug-0009"))

    with pytest.raises(
        DispositionError,
        match="unknown case 'bug/bug-0009' in the xslt10 baseline",
    ):
        validate_dispositions((entry,), baselines)


def test_validate_rejects_unknown_profile_name() -> None:
    entry = make_entry(profiles=("xslt99",))

    with pytest.raises(DispositionError, match="unknown profile 'xslt99'"):
        validate_dispositions((entry,))


def test_validate_rejects_profile_without_supplied_baseline() -> None:
    with pytest.raises(DispositionError, match="no baseline for profile 'xslt10'"):
        validate_dispositions((make_entry(),), {})


def test_validate_rejects_blank_case_id() -> None:
    entry = make_entry(cases=("  ",))

    with pytest.raises(DispositionError, match="gap-1: case id is blank"):
        validate_dispositions((entry,))


def test_validate_rejects_invalid_category() -> None:
    entry = make_entry(category="not-in-vocabulary")

    with pytest.raises(DispositionError, match="unknown category 'not-in-vocabulary'"):
        validate_dispositions((entry,))


def test_validate_rejects_blank_reason() -> None:
    entry = make_entry(reason="   ")

    with pytest.raises(DispositionError, match="gap-1: reason is blank"):
        validate_dispositions((entry,))


def test_validate_rejects_blank_title() -> None:
    entry = make_entry(title="")

    with pytest.raises(DispositionError, match="gap-1: title is blank"):
        validate_dispositions((entry,))


def test_validate_rejects_blank_id() -> None:
    entry = make_entry(id="")

    with pytest.raises(DispositionError, match="tracking id is blank"):
        validate_dispositions((entry,))


def test_validate_rejects_empty_profiles() -> None:
    entry = make_entry(profiles=())

    with pytest.raises(DispositionError, match="gap-1: no profiles listed"):
        validate_dispositions((entry,))


def test_validate_rejects_empty_cases() -> None:
    entry = make_entry(cases=())

    with pytest.raises(DispositionError, match="gap-1: no cases listed"):
        validate_dispositions((entry,))


def test_validate_reports_every_problem_not_just_the_first() -> None:
    entries = (
        make_entry(id="gap-1", category="nope"),
        make_entry(id="gap-2", reason=""),
    )

    with pytest.raises(DispositionError) as excinfo:
        validate_dispositions(entries)

    message = str(excinfo.value)
    assert "gap-1" in message and "unknown category 'nope'" in message
    assert "gap-2" in message and "reason is blank" in message


# ----------------------------------------------------------------- census


def test_unclassified_lists_only_unclaimed_non_pass_cases() -> None:
    outcomes = {
        "bug/bug-0001": "fail",
        "axes/axes-0002": "wrong-error-code",
        "math/math-0003": Outcome.TIMEOUT,
        "number/number-0004": "pass",
        "document/document-0005": "adapter-gap",
    }

    assert unclassified(outcomes, (make_entry(),), "xslt10") == (
        "axes/axes-0002",
        "document/document-0005",
        "math/math-0003",
    )


def test_unclassified_separates_profiles() -> None:
    entry = make_entry(profiles=("xslt10",))
    outcomes = {"bug/bug-0001": "fail"}

    assert unclassified(outcomes, (entry,), "xslt10") == ()
    assert unclassified(outcomes, (entry,), "xslt20") == ("bug/bug-0001",)


def test_unclassified_never_returns_a_pass() -> None:
    outcomes = {"bug/bug-0001": "pass", "bug/bug-0002": "pass"}

    assert unclassified(outcomes, (make_entry(cases=("bug/bug-0003",)),), "xslt10") == ()


def test_active_entries_keeps_only_entries_with_a_non_passing_case() -> None:
    failing = make_entry(cases=("bug/bug-0001",))
    passing = make_entry(id="gap-2", cases=("bug/bug-0002",))
    outcomes = {"bug/bug-0001": "fail", "bug/bug-0002": "pass"}

    assert active_entries("xslt10", outcomes, (failing, passing)) == (failing,)


def test_active_entries_keeps_a_mixed_entry() -> None:
    entry = make_entry(cases=("bug/bug-0001", "bug/bug-0002"))
    outcomes = {"bug/bug-0001": "pass", "bug/bug-0002": "adapter-gap"}

    assert active_entries("xslt10", outcomes, (entry,)) == (entry,)


def test_active_entries_skips_entries_for_another_profile() -> None:
    entry = make_entry(profiles=("xslt20",))
    outcomes = {"bug/bug-0001": "fail"}

    assert active_entries("xslt10", outcomes, (entry,)) == ()


def test_active_entries_treats_a_missing_case_as_passed() -> None:
    entry = make_entry(cases=("bug/bug-0001",))

    assert active_entries("xslt10", {}, (entry,)) == ()
    assert active_entries("xslt10", {"bug/bug-0001": "timeout"}, (entry,)) == (entry,)


# --------------------------------------------------------------- real data


def test_profile_vocabulary_matches_the_checked_in_baselines() -> None:
    assert set(PROFILES) == {path.stem for path in BASELINE_DIR.glob("*.toml")}


def test_categories_are_the_documented_closed_vocabulary() -> None:
    documented = {
        "declared-non-goal",
        "implementation-gap",
        "adapter-limit",
        "platform-limit",
        "queried-corpus",
        "spec-correct-divergence",
        "performance-budget",
    }

    assert documented == CATEGORIES


def test_checked_in_dispositions_match_the_xslt10_census() -> None:
    baseline = load_baseline("xslt10")
    entries = load_dispositions(DISPOSITIONS, baselines={"xslt10": baseline})

    assert len(entries) == 1
    (entry,) = entries
    assert entry.id == "xslt10-environment-resources"
    assert entry.category == "adapter-limit"
    assert entry.profiles == ("xslt10",)
    assert entry.cases == ("bug/bug-0901", "document/document-2002")
    assert entry.reason == "resources"
    for case_id in entry.cases:
        assert baseline.outcome(case_id) == Outcome.ADAPTER_GAP.value
    outcomes = baseline.as_mapping()
    assert not set(entry.cases) & set(unclassified(outcomes, entries, "xslt10"))
    assert active_entries("xslt10", outcomes, entries) == entries


def test_loading_the_checked_in_file_is_deterministic() -> None:
    assert load_dispositions(DISPOSITIONS) == load_dispositions(DISPOSITIONS)


def test_checked_in_dispositions_are_time_and_path_free() -> None:
    document = tomllib.loads(DISPOSITIONS.read_text(encoding="utf-8"))
    entries = document["dispositions"]

    assert entries
    for entry in entries:
        for value in entry.values():
            for item in value if isinstance(value, list) else [value]:
                assert isinstance(item, str)
                assert not item.startswith("/")
                assert "timestamp" not in item.lower()
