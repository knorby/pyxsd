"""Residual dispositions and cross-profile expectation relations.

Dispositions explain *why* a checked-in failure is understood without ever
changing its outcome. Validation reads only ``pyxsd:`` baseline keys, and an
entry whose case starts passing remains valid without describing an active
residual. The census keeps every other current failure visibly unclassified
rather than inventing a rationale for it.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from xsts.baseline import Baseline, load
from xsts.dispositions import (
    INVALID_IN_1_0_VALID_IN_1_1,
    NOT_COMPARABLE,
    SAME_INVALID,
    SAME_VALID,
    VALID_IN_1_0_INVALID_IN_1_1,
    Disposition,
    DispositionError,
    expectation_relation,
    load_dispositions,
    unclassified_failures,
    validate_dispositions,
)
from xsts.runner import Case, build_cases, corpus_available, load_default_catalog
from xsts.selection import XSD10, XSD11

DISPOSITIONS = Path(__file__).resolve().parent / "xsts" / "dispositions.toml"
BASELINE_10 = Path(__file__).resolve().parent / "xsts" / "baseline-xsd10.toml"
BASELINE_11 = Path(__file__).resolve().parent / "xsts" / "baseline-xsd11.toml"


def make_case(test_id: str, *, kind: str = "instance", expected: str | None = "invalid") -> Case:
    """A minimal case with the given expectation for profile-agnostic checks."""
    set_name, group_name, _ = test_id.split("/", 2)
    return Case(
        test_id=test_id,
        set_name=set_name,
        contributor="Saxonica",
        group_name=group_name,
        kind=kind,
        applicable=True,
        schema_documents=(),
        corpus_root=PurePosixPath("."),
        expected_validity=expected,
    )


def make_baseline(fails: dict[str, str], extra: dict[str, str] | None = None) -> Baseline:
    """A baseline whose ``pyxsd:`` keys come from *fails* (id -> kind)."""
    results = {f"pyxsd:{test_id}:{kind}": "fail" for test_id, kind in fails.items()}
    results.update(extra or {})
    return Baseline(results=results)


def make_entry(**overrides) -> Disposition:
    fields = {
        "id": "gap-1",
        "title": "One understood failure",
        "category": "implementation-gap",
        "profiles": ("xsd11",),
        "cases": ("A/g1/t1",),
        "rationale": "Documented gap with a tracker.",
    }
    fields.update(overrides)
    return Disposition(**fields)


def make_world(
    fails: dict[str, str] | None = None,
    extra_results: dict[str, str] | None = None,
    cases: dict[str, Case] | None = None,
):
    fails = fails or {"A/g1/t1": "instance"}
    cases = cases or {"A/g1/t1": make_case("A/g1/t1")}
    baselines = {"xsd11": make_baseline(fails, extra_results)}
    return baselines, {"xsd11": cases}


VALID_TOML = """\
[[dispositions]]
id = "gap-1"
title = "One understood failure"
category = "implementation-gap"
profiles = ["xsd11"]
cases = ["A/g1/t1"]
links = ["https://example.org/spec#section"]
rationale = "Documented gap."
"""


# ---------------------------------------------------------------- loading


def test_load_reads_entries_with_normalized_tuples(tmp_path: Path) -> None:
    path = tmp_path / "dispositions.toml"
    path.write_text(VALID_TOML, encoding="utf-8")

    entries = load_dispositions(path)

    assert entries == (
        Disposition(
            id="gap-1",
            title="One understood failure",
            category="implementation-gap",
            profiles=("xsd11",),
            cases=("A/g1/t1",),
            rationale="Documented gap.",
            links=("https://example.org/spec#section",),
        ),
    )


def test_load_defaults_links_to_empty(tmp_path: Path) -> None:
    body = VALID_TOML.replace('links = ["https://example.org/spec#section"]\n', "")
    path = tmp_path / "dispositions.toml"
    path.write_text(body, encoding="utf-8")

    (entry,) = load_dispositions(path)

    assert entry.links == ()


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(DispositionError, match="cannot read"):
        load_dispositions(tmp_path / "absent.toml")


def test_load_malformed_toml_raises(tmp_path: Path) -> None:
    path = tmp_path / "dispositions.toml"
    path.write_text("[dispositions\n", encoding="utf-8")

    with pytest.raises(DispositionError, match="malformed TOML"):
        load_dispositions(path)


def test_load_without_dispositions_array_raises(tmp_path: Path) -> None:
    path = tmp_path / "dispositions.toml"
    path.write_text('[meta]\nprofile = "xsd11"\n', encoding="utf-8")

    with pytest.raises(DispositionError, match=r"\[\[dispositions\]\] array of tables"):
        load_dispositions(path)


def test_load_rejects_non_table_entry(tmp_path: Path) -> None:
    path = tmp_path / "dispositions.toml"
    path.write_text("dispositions = [1]\n", encoding="utf-8")

    with pytest.raises(DispositionError, match="entry 1 is not a table"):
        load_dispositions(path)


def test_load_rejects_non_string_scalar(tmp_path: Path) -> None:
    body = VALID_TOML.replace('id = "gap-1"', "id = 1")
    path = tmp_path / "dispositions.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(DispositionError, match="entry 1: id must be a string"):
        load_dispositions(path)


def test_load_rejects_non_array_list_field(tmp_path: Path) -> None:
    body = VALID_TOML.replace('cases = ["A/g1/t1"]', 'cases = "A/g1/t1"')
    path = tmp_path / "dispositions.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(DispositionError, match="entry 1: cases must be an array of strings"):
        load_dispositions(path)


def test_load_rejects_non_string_list_member(tmp_path: Path) -> None:
    body = VALID_TOML.replace('cases = ["A/g1/t1"]', "cases = [1]")
    path = tmp_path / "dispositions.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(DispositionError, match="entry 1: cases must be an array of strings"):
        load_dispositions(path)


def test_load_rejects_unknown_field(tmp_path: Path) -> None:
    body = VALID_TOML.replace('cases = ["A/g1/t1"]', 'cases = ["A/g1/t1"]\ncase = ["A/g1/t1"]')
    path = tmp_path / "dispositions.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(DispositionError, match="entry 1: unknown field 'case'"):
        load_dispositions(path)


# ------------------------------------------------------------- validation


def test_validate_accepts_a_current_failure() -> None:
    baselines, cases_by_profile = make_world()

    assert validate_dispositions((make_entry(),), baselines, cases_by_profile) is None


def test_validate_does_not_rewrite_baseline_results() -> None:
    baselines, cases_by_profile = make_world()
    before = dict(baselines["xsd11"].results)

    validate_dispositions((make_entry(),), baselines, cases_by_profile)

    assert baselines["xsd11"].results == before


def test_validate_reads_only_pyxsd_keys() -> None:
    # An xmlschema failure for the same case must neither satisfy nor
    # disturb validation of the pyxsd outcome.
    baselines, cases_by_profile = make_world(
        extra_results={
            "xmlschema:A/g1/t1:instance": "fail",
            "pyxsd:A/g1/t1:instance": "fail",
        }
    )

    assert validate_dispositions((make_entry(),), baselines, cases_by_profile) is None


def test_validate_rejects_duplicate_tracking_ids() -> None:
    baselines, cases_by_profile = make_world()
    entries = (make_entry(), make_entry(id="gap-1", cases=("A/g1/t1",)))

    with pytest.raises(DispositionError, match="gap-1: duplicate tracking id"):
        validate_dispositions(entries, baselines, cases_by_profile)


def test_validate_rejects_case_claimed_twice_in_one_profile() -> None:
    baselines, cases_by_profile = make_world()
    entries = (make_entry(), make_entry(id="gap-2", cases=("A/g1/t1",)))

    with pytest.raises(DispositionError, match="already claimed by 'gap-1'"):
        validate_dispositions(entries, baselines, cases_by_profile)


def test_validate_rejects_duplicate_case_within_one_entry() -> None:
    baselines, cases_by_profile = make_world()
    entry = make_entry(cases=("A/g1/t1", "A/g1/t1"))

    with pytest.raises(DispositionError, match="gap-1: duplicate case 'A/g1/t1'"):
        validate_dispositions((entry,), baselines, cases_by_profile)


def test_validate_rejects_unknown_case_id() -> None:
    baselines, cases_by_profile = make_world()
    entry = make_entry(cases=("A/g1/t1", "A/g9/absent"))

    with pytest.raises(DispositionError, match="unknown case 'A/g9/absent' in profile 'xsd11'"):
        validate_dispositions((entry,), baselines, cases_by_profile)


def test_validate_rejects_unknown_profile_name() -> None:
    baselines, cases_by_profile = make_world()
    entry = make_entry(profiles=("xsd12",))

    with pytest.raises(DispositionError, match="unknown profile 'xsd12'"):
        validate_dispositions((entry,), baselines, cases_by_profile)


def test_validate_rejects_profile_without_baseline() -> None:
    _, cases_by_profile = make_world()
    baselines: dict[str, Baseline] = {}

    with pytest.raises(DispositionError, match="no baseline for profile 'xsd11'"):
        validate_dispositions((make_entry(),), baselines, cases_by_profile)


def test_validate_rejects_profile_without_cases() -> None:
    baselines, _ = make_world()

    with pytest.raises(DispositionError, match="no cases for profile 'xsd11'"):
        validate_dispositions((make_entry(),), baselines, {})


def test_validate_rejects_case_missing_from_baseline() -> None:
    cases = {"A/g1/t1": make_case("A/g1/t1")}
    baselines = {"xsd11": Baseline(results={})}

    with pytest.raises(
        DispositionError,
        match=r"pyxsd:A/g1/t1:instance is not recorded in the xsd11 baseline",
    ):
        validate_dispositions((make_entry(),), baselines, {"xsd11": cases})


def test_validate_allows_a_case_that_no_longer_fails() -> None:
    baselines, cases_by_profile = make_world(
        fails={}, extra_results={"pyxsd:A/g1/t1:instance": "pass"}
    )

    assert validate_dispositions((make_entry(),), baselines, cases_by_profile) is None


def test_validate_rejects_invalid_category() -> None:
    baselines, cases_by_profile = make_world()
    entry = make_entry(category="not-in-vocabulary")

    with pytest.raises(DispositionError, match="unknown category 'not-in-vocabulary'"):
        validate_dispositions((entry,), baselines, cases_by_profile)


def test_validate_rejects_blank_rationale() -> None:
    baselines, cases_by_profile = make_world()
    entry = make_entry(rationale="   ")

    with pytest.raises(DispositionError, match="gap-1: rationale is blank"):
        validate_dispositions((entry,), baselines, cases_by_profile)


def test_validate_rejects_blank_title() -> None:
    baselines, cases_by_profile = make_world()
    entry = make_entry(title="")

    with pytest.raises(DispositionError, match="gap-1: title is blank"):
        validate_dispositions((entry,), baselines, cases_by_profile)


def test_validate_rejects_blank_id() -> None:
    baselines, cases_by_profile = make_world()
    entry = make_entry(id="")

    with pytest.raises(DispositionError, match="tracking id is blank"):
        validate_dispositions((entry,), baselines, cases_by_profile)


def test_validate_rejects_empty_profiles() -> None:
    baselines, cases_by_profile = make_world()
    entry = make_entry(profiles=())

    with pytest.raises(DispositionError, match="gap-1: no profiles listed"):
        validate_dispositions((entry,), baselines, cases_by_profile)


def test_validate_rejects_empty_cases() -> None:
    baselines, cases_by_profile = make_world()
    entry = make_entry(cases=())

    with pytest.raises(DispositionError, match="gap-1: no cases listed"):
        validate_dispositions((entry,), baselines, cases_by_profile)


def test_validate_reports_every_problem_not_just_the_first() -> None:
    baselines, cases_by_profile = make_world()
    entries = (
        make_entry(id="gap-1", category="nope"),
        make_entry(id="gap-2", rationale=""),
    )

    with pytest.raises(DispositionError) as excinfo:
        validate_dispositions(entries, baselines, cases_by_profile)

    message = str(excinfo.value)
    assert "gap-1" in message and "unknown category 'nope'" in message
    assert "gap-2" in message and "rationale is blank" in message


# ----------------------------------------------------------------- census


def test_unclassified_failures_lists_only_unclaimed_failures() -> None:
    baselines, _ = make_world(
        fails={"A/g1/t1": "instance", "B/g2/t2": "schema", "C/g3/t3": "instance"},
        extra_results={
            "pyxsd:D/g4/t4:instance": "pass",
            "pyxsd:E/g5/t5:instance": "adapter-gap",
            "xmlschema:B/g2/t2:schema": "fail",
        },
    )

    census = unclassified_failures((make_entry(),), baselines)

    assert census == {"xsd11": ("B/g2/t2", "C/g3/t3")}


def test_unclassified_failures_separates_profiles() -> None:
    entry = make_entry(profiles=("xsd10", "xsd11"))
    baselines = {
        "xsd10": make_baseline({"A/g1/t1": "instance"}),
        "xsd11": make_baseline({"A/g1/t1": "instance", "F/g6/t6": "instance"}),
    }

    census = unclassified_failures((entry,), baselines)

    assert census == {"xsd10": (), "xsd11": ("F/g6/t6",)}


# ------------------------------------------------------------- relations


def test_expectation_relation_values() -> None:
    assert (
        expectation_relation(
            make_case("A/g/t", expected="valid"), make_case("A/g/t", expected="valid")
        )
        == SAME_VALID
    )
    assert (
        expectation_relation(
            make_case("A/g/t", expected="invalid"), make_case("A/g/t", expected="invalid")
        )
        == SAME_INVALID
    )
    assert (
        expectation_relation(
            make_case("A/g/t", expected="invalid"), make_case("A/g/t", expected="valid")
        )
        == INVALID_IN_1_0_VALID_IN_1_1
    )
    assert (
        expectation_relation(
            make_case("A/g/t", expected="valid"), make_case("A/g/t", expected="invalid")
        )
        == VALID_IN_1_0_INVALID_IN_1_1
    )


def test_expectation_relation_not_comparable_for_non_boolean_expectations() -> None:
    one_sided = expectation_relation(
        make_case("A/g/t", expected="valid"), make_case("A/g/t", expected="notKnown")
    )
    other_sided = expectation_relation(
        make_case("A/g/t", expected="indeterminate"), make_case("A/g/t", expected="valid")
    )

    assert one_sided == NOT_COMPARABLE
    assert other_sided == NOT_COMPARABLE


# ------------------------------------------------------------ real data

ASTRAL_REGEX_CASES = tuple(
    f"MS-Regex2006-07-15/{group}/{group}.i"
    for group in (
        "reJ11",
        "reJ13",
        "reJ19",
        "reJ21",
        "reJ23",
        "reJ25",
        "reJ29",
        "reJ31",
        "reJ33",
        "reJ35",
        "reJ61",
        "reJ69",
        "reJ75",
        "reJ77",
        "reL98",
        "reL99",
        "reM98",
        "reN99",
        "reS21",
        "reS42",
        "reT63",
        "reT84",
    )
)
DTD_ENTITY_CASES = (
    "Id/id017/id017.n01.xml",
    "Id/id018/id018.n01.xml",
    "Id/id018/id018.n02.xml",
    "Id/id019/id019.n01.xml",
    "Id/id019/id019.n02.xml",
    "Id/id020/id020.n01.xml",
    "Id/id020/id020.n02.xml",
    "Id/id021/id021.n01.xml",
    "Id/id021/id021.n02.xml",
)
XML11_LEXER_CASES = (
    "XmlVersions/xv001/xv001.v01.xml",
    "XmlVersions/xv003/xv003.v01.xml",
    "XmlVersions/xv004/xv004.v01.xml",
    "XmlVersions/xv006/xv006.v01.xml",
    "XmlVersions/xv008/xv008.v01.xml",
)
TYPE_ONLY_ROOT_CASES = (
    "CType/targetns00101m/targetNS00101m1_p",
    "SType/st_name00401m/ST_name00401m1_p",
    "SType/st_targetns00101m/ST_targetNS00101m1_p",
    "SType/st_targetns00101m/ST_targetNS00101m2_p",
    "SType/st_targetns00201m/ST_targetNS00201m1_p",
    "MS-Additional2006-07-15/addB187/addB187.v",
)
REGEX_VERSION_SWITCH_CASES = tuple(
    f"MS-Regex2006-07-15/{group}/{group}"
    for group in (
        "reF20",
        "reF21",
        "reF22",
        "reF23",
        "reG26",
        "reG27",
        "reG28",
        "reG29",
        "reG30",
        "reG31",
        "reG32",
        "reG33",
        "reH19",
        "reH20",
        "reH21",
        "reK88",
    )
)

pytestmark_real = pytest.mark.skipif(
    not corpus_available(),
    reason="xsdtests corpus not checked out (git submodule update --init tests/xsts/corpus)",
)


@pytest.fixture(scope="module")
def real_baselines() -> dict[str, Baseline]:
    return {"xsd10": load(BASELINE_10), "xsd11": load(BASELINE_11)}


@pytest.fixture(scope="module")
def real_cases_by_profile() -> dict[str, dict[str, Case]]:
    catalog = load_default_catalog()
    return {
        profile.name: {case.test_id: case for case in build_cases(catalog, profile)}
        for profile in (XSD10, XSD11)
    }


@pytestmark_real
def test_checked_in_dispositions_validate_against_real_baselines(
    real_baselines: dict[str, Baseline],
    real_cases_by_profile: dict[str, dict[str, Case]],
) -> None:
    entries = load_dispositions(DISPOSITIONS)

    assert validate_dispositions(entries, real_baselines, real_cases_by_profile) is None


@pytestmark_real
def test_checked_in_seeds_match_the_adjudicated_case_sets() -> None:
    entries = load_dispositions(DISPOSITIONS)
    by_id = {entry.id: entry for entry in entries}

    seeds = {
        "xsts-astral-regex-queried": ("queried-corpus", ("xsd11",), ASTRAL_REGEX_CASES),
        "xsts-dtd-unparsed-entities": (
            "implementation-gap",
            ("xsd11",),
            DTD_ENTITY_CASES,
        ),
        "xsts-xml11-lexer-floor": ("platform-limit", ("xsd11",), XML11_LEXER_CASES),
        "xsts-type-only-root-xsi-type": (
            "implementation-gap",
            ("xsd10", "xsd11"),
            TYPE_ONLY_ROOT_CASES,
        ),
        "xsd10-regex-version-switch": (
            "version-switch-candidate",
            ("xsd10",),
            REGEX_VERSION_SWITCH_CASES,
        ),
    }
    for seed_id, (category, profiles, cases) in seeds.items():
        entry = by_id[seed_id]
        assert entry.category == category
        assert entry.profiles == profiles
        assert sorted(entry.cases) == sorted(cases)


@pytestmark_real
def test_unclassified_census_keeps_other_failures_visible(
    real_baselines: dict[str, Baseline],
    real_cases_by_profile: dict[str, dict[str, Case]],
) -> None:
    entries = load_dispositions(DISPOSITIONS)
    validate_dispositions(entries, real_baselines, real_cases_by_profile)

    census = unclassified_failures(entries, real_baselines)

    # Derive the census from current outcomes, not a pinned failure total.
    for profile, baseline in real_baselines.items():
        claimed = {
            case_id for entry in entries if profile in entry.profiles for case_id in entry.cases
        }
        failing = {
            key.removeprefix("pyxsd:").rsplit(":", 1)[0]
            for key, outcome in baseline.results.items()
            if key.startswith("pyxsd:") and outcome == "fail"
        }
        assert census[profile] == tuple(sorted(failing - claimed))


@pytestmark_real
def test_expectation_relations_do_not_change_raw_baseline_outcomes(
    real_baselines: dict[str, Baseline],
    real_cases_by_profile: dict[str, dict[str, Case]],
) -> None:
    baseline10 = real_baselines["xsd10"].results
    baseline11 = real_baselines["xsd11"].results
    before10 = dict(baseline10)
    before11 = dict(baseline11)
    relations = set()

    for case10 in real_cases_by_profile["xsd10"].values():
        case11 = real_cases_by_profile["xsd11"][case10.test_id]
        relation = expectation_relation(case10, case11)
        relations.add(relation)
        if relation == INVALID_IN_1_0_VALID_IN_1_1:
            assert case10.expected_valid is False
            assert case11.expected_valid is True

    assert relations <= {
        INVALID_IN_1_0_VALID_IN_1_1,
        NOT_COMPARABLE,
        SAME_INVALID,
        SAME_VALID,
        VALID_IN_1_0_INVALID_IN_1_1,
    }
    assert baseline10 == before10
    assert baseline11 == before11
