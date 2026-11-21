"""Residual dispositions for checked-in XSLT conformance cases.

Dispositions explain *why* a non-passing case is understood without ever
changing its outcome.  The production loader always validates against the
checked-in baselines, and an entry whose cases start passing remains valid
without describing an active residual.  The census keeps every other
non-passing case visibly unclassified rather than inventing a rationale for it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from tests.xslt.baseline import BASELINE_DIR, Baseline, load_baseline, write_baseline
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

#: A case recorded in the checked-in xslt10 baseline.
REAL_CASE = "bug/bug-0901"

VALID_TOML = """\
[[dispositions]]
id = "gap-1"
title = "One understood residual"
category = "implementation-gap"
profiles = ["xslt10"]
cases = ["bug/bug-0901"]
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


def baselines_for(*entries: Disposition) -> dict[str, Baseline]:
    """Synthetic baselines recording every case the entries name."""
    recorded: dict[str, dict[str, str]] = {}
    for entry in entries:
        for profile in entry.profiles:
            recorded.setdefault(profile, {}).update((case_id, "fail") for case_id in entry.cases)
    return {profile: make_baseline(cases) for profile, cases in recorded.items()}


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
            cases=(REAL_CASE,),
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
    body = VALID_TOML.replace('cases = ["bug/bug-0901"]', 'cases = "bug/bug-0901"')
    path = write_toml(tmp_path, body)

    with pytest.raises(DispositionError, match="entry 1: cases must be an array of strings"):
        load_dispositions(path)


def test_load_rejects_non_string_list_member(tmp_path: Path) -> None:
    body = VALID_TOML.replace('cases = ["bug/bug-0901"]', "cases = [1]")
    path = write_toml(tmp_path, body)

    with pytest.raises(DispositionError, match="entry 1: cases must be an array of strings"):
        load_dispositions(path)


def test_load_rejects_unknown_field(tmp_path: Path) -> None:
    body = VALID_TOML.replace(
        'cases = ["bug/bug-0901"]',
        'cases = ["bug/bug-0901"]\ncase = ["bug/bug-0901"]',
    )
    path = write_toml(tmp_path, body)

    with pytest.raises(DispositionError, match="entry 1: unknown field 'case'"):
        load_dispositions(path)


def test_load_rejects_a_case_absent_from_the_checked_in_baseline(tmp_path: Path) -> None:
    # The regression that matters: the default loader consults the checked-in
    # xslt10 baseline, so a typo cannot sit in the file unnoticed.
    body = VALID_TOML.replace('"bug/bug-0901"', '"bug/bug-0001"')
    path = write_toml(tmp_path, body)

    with pytest.raises(
        DispositionError,
        match="unknown case 'bug/bug-0001' in the xslt10 baseline",
    ):
        load_dispositions(path)


def test_load_reads_only_the_profiles_the_entries_name(tmp_path: Path) -> None:
    # A malformed xslt20 baseline must not disturb an xslt10-only entry.
    write_baseline(tmp_path / "xslt10.toml", make_baseline({REAL_CASE: "fail"}))
    (tmp_path / "xslt20.toml").write_text("[cases\n", encoding="utf-8")
    path = write_toml(tmp_path, VALID_TOML)

    (entry,) = load_dispositions(path, baseline_dir=tmp_path)

    assert entry.cases == (REAL_CASE,)


def test_load_rejects_a_named_profile_without_a_baseline_file(tmp_path: Path) -> None:
    path = write_toml(tmp_path, VALID_TOML)

    with pytest.raises(DispositionError, match="cannot read the xslt10 baseline"):
        load_dispositions(path, baseline_dir=tmp_path)


def test_load_rejects_a_malformed_baseline(tmp_path: Path) -> None:
    (tmp_path / "xslt10.toml").write_text("[cases\na = 'pass'\n", encoding="utf-8")
    path = write_toml(tmp_path, VALID_TOML)

    with pytest.raises(DispositionError, match="cannot read the xslt10 baseline"):
        load_dispositions(path, baseline_dir=tmp_path)


def test_load_reports_an_unknown_profile_without_reading_a_baseline(tmp_path: Path) -> None:
    body = VALID_TOML.replace('profiles = ["xslt10"]', 'profiles = ["xslt99"]')
    path = write_toml(tmp_path, body)

    # The directory holds no baselines at all: the unknown profile must be a
    # validation problem, not a read failure.
    with pytest.raises(DispositionError, match="unknown profile 'xslt99'"):
        load_dispositions(path, baseline_dir=tmp_path)


def test_load_always_runs_structural_validation(tmp_path: Path) -> None:
    body = VALID_TOML.replace('reason = "Documented gap."', 'reason = "  "')
    path = write_toml(tmp_path, body)

    with pytest.raises(DispositionError, match="gap-1: reason is blank"):
        load_dispositions(path)


# ------------------------------------------------------------- validation


def test_validate_accepts_a_current_failure() -> None:
    entry = make_entry()

    assert validate_dispositions((entry,), baselines_for(entry)) is None


def test_validate_does_not_rewrite_baseline_results() -> None:
    entry = make_entry()
    baselines = baselines_for(entry)
    before = dict(baselines["xslt10"].cases)

    validate_dispositions((entry,), baselines)

    assert baselines["xslt10"].cases == before


def test_validate_rejects_duplicate_tracking_ids() -> None:
    entries = (make_entry(), make_entry(id="gap-1", cases=("bug/bug-0002",)))

    with pytest.raises(DispositionError, match="gap-1: duplicate tracking id"):
        validate_dispositions(entries, baselines_for(*entries))


def test_validate_rejects_case_claimed_twice_in_one_profile() -> None:
    entries = (make_entry(), make_entry(id="gap-2"))

    with pytest.raises(DispositionError, match="already claimed by 'gap-1'"):
        validate_dispositions(entries, baselines_for(*entries))


def test_validate_allows_one_case_claimed_in_two_profiles() -> None:
    entries = (
        make_entry(profiles=("xslt10",)),
        make_entry(id="gap-2", profiles=("xslt20",)),
    )

    assert validate_dispositions(entries, baselines_for(*entries)) is None


def test_validate_rejects_duplicate_case_within_one_entry() -> None:
    entry = make_entry(cases=("bug/bug-0001", "bug/bug-0001"))

    with pytest.raises(DispositionError, match=r"gap-1: duplicate case 'bug/bug-0001'"):
        validate_dispositions((entry,), baselines_for(entry))


def test_validate_rejects_unknown_case_id() -> None:
    entry = make_entry(cases=("bug/bug-0001", "bug/bug-0009"))
    baselines = {"xslt10": make_baseline({"bug/bug-0001": "fail"})}

    with pytest.raises(
        DispositionError,
        match="unknown case 'bug/bug-0009' in the xslt10 baseline",
    ):
        validate_dispositions((entry,), baselines)


def test_validate_rejects_unknown_profile_name() -> None:
    entry = make_entry(profiles=("xslt99",))

    with pytest.raises(DispositionError, match="unknown profile 'xslt99'"):
        validate_dispositions((entry,), {})


def test_validate_rejects_profile_without_supplied_baseline() -> None:
    with pytest.raises(DispositionError, match="no baseline for profile 'xslt10'"):
        validate_dispositions((make_entry(),), {})


def test_validate_rejects_blank_case_id() -> None:
    entry = make_entry(cases=("  ",))

    with pytest.raises(DispositionError, match="gap-1: case id is blank"):
        validate_dispositions((entry,), baselines_for(entry))


def test_validate_rejects_invalid_category() -> None:
    entry = make_entry(category="not-in-vocabulary")

    with pytest.raises(DispositionError, match="unknown category 'not-in-vocabulary'"):
        validate_dispositions((entry,), baselines_for(entry))


def test_validate_rejects_blank_reason() -> None:
    entry = make_entry(reason="   ")

    with pytest.raises(DispositionError, match="gap-1: reason is blank"):
        validate_dispositions((entry,), baselines_for(entry))


def test_validate_rejects_blank_title() -> None:
    entry = make_entry(title="")

    with pytest.raises(DispositionError, match="gap-1: title is blank"):
        validate_dispositions((entry,), baselines_for(entry))


def test_validate_rejects_blank_id() -> None:
    entry = make_entry(id="")

    with pytest.raises(DispositionError, match="tracking id is blank"):
        validate_dispositions((entry,), baselines_for(entry))


def test_validate_rejects_empty_profiles() -> None:
    entry = make_entry(profiles=())

    with pytest.raises(DispositionError, match="gap-1: no profiles listed"):
        validate_dispositions((entry,), baselines_for(entry))


def test_validate_rejects_empty_cases() -> None:
    entry = make_entry(cases=())

    with pytest.raises(DispositionError, match="gap-1: no cases listed"):
        validate_dispositions((entry,), baselines_for(entry))


def test_validate_reports_every_problem_not_just_the_first() -> None:
    entries = (
        make_entry(id="gap-1", category="nope"),
        make_entry(id="gap-2", reason=""),
    )

    with pytest.raises(DispositionError) as excinfo:
        validate_dispositions(entries, baselines_for(*entries))

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


def test_unclassified_accepts_a_baseline_or_a_mapping() -> None:
    entry = make_entry()
    baseline = make_baseline({"bug/bug-0001": "fail", "bug/bug-0002": "pass"})

    assert unclassified(baseline, (entry,), "xslt10") == ()
    assert unclassified(baseline.as_mapping(), (entry,), "xslt10") == ()


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


def test_unclassified_does_not_modify_its_inputs() -> None:
    entry = make_entry()
    baseline = make_baseline({"bug/bug-0001": "fail", "bug/bug-0002": "fail"})
    outcomes = baseline.as_mapping()
    entries = (entry,)

    unclassified(baseline, entries, "xslt10")
    unclassified(outcomes, entries, "xslt10")

    assert baseline.cases == {"bug/bug-0001": "fail", "bug/bug-0002": "fail"}
    assert outcomes == {"bug/bug-0001": "fail", "bug/bug-0002": "fail"}
    assert entries == (entry,)


def test_active_entries_does_not_modify_its_inputs() -> None:
    entry = make_entry()
    outcomes = {"bug/bug-0001": "fail"}
    entries = (entry,)

    active_entries("xslt10", outcomes, entries)

    assert outcomes == {"bug/bug-0001": "fail"}
    assert entries == (entry,)


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


def test_checked_in_dispositions_declare_the_remote_text_non_goal() -> None:
    entries = load_dispositions(DISPOSITIONS)

    (entry,) = [item for item in entries if item.id == "remote-text-resources-without-fixtures"]
    assert entry.category == "declared-non-goal"
    assert entry.profiles == ("xslt30", "xslt20")
    assert entry.cases == (
        "unparsed-text/unparsed-text-2002",
        "unparsed-text/unparsed-text-2003",
    )
    assert "no local file" in entry.reason
    for profile in entry.profiles:
        baseline = load_baseline(profile)
        for case_id in entry.cases:
            assert case_id in baseline.cases
        assert not set(entry.cases) & set(unclassified(baseline, entries, profile))


def test_every_namespace_family_residual_is_adjudicated() -> None:
    """Every non-passing namespace-family case names an explicit disposition.

    A new failure in either family must be fixed or adjudicated with a public
    reason; it may not sit silently in the unclassified census.
    """
    entries = load_dispositions(DISPOSITIONS)
    families = ("namespace/", "namespace-alias/")
    for profile in PROFILES:
        baseline = load_baseline(profile)
        remaining = [
            case_id
            for case_id in unclassified(baseline, entries, profile)
            if case_id.startswith(families)
        ]
        assert remaining == [], profile


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
