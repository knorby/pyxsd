"""Bounded immutable profiles over XML lexical trees and native JSON values."""

import io
from dataclasses import FrozenInstanceError
from xml.etree import ElementTree as ET

import pytest

from pyxsd._lexical import clone_lexical
from pyxsd.locations import DataLocation, IssueLocation, PathStep
from pyxsd.schemaless import (
    DocumentProfile,
    JsonDocument,
    PathProfile,
    ProfileLimits,
    SchemalessDocument,
    SchemalessElement,
    TypeCandidate,
    TypePolicy,
    from_data,
    from_json,
    parse_xml,
)
from pyxsd.validation import ValidationReport

CANDIDATE_NAMES = (
    "xs:boolean",
    "xs:integer",
    "xs:decimal",
    "xs:double",
    "xs:date",
    "xs:dateTime",
    "xs:time",
)


@pytest.mark.parametrize("format_typing", [False, True])
def test_json_profile_suggests_formats_by_native_kind_before_counting_array_items(format_typing):
    doc = from_json(
        '[true,"1",null,{},2,3,"004",1.25,2.5,"YQ==","Yg==","!"]',
        json_schema={
            "prefixItems": [
                {"format": "int32"},
                {"format": "int32"},
                {"format": "int32"},
                {"format": "int32"},
                {"format": "int32"},
                {"format": "int64"},
                {"format": "int64"},
                {"format": "float"},
                {"format": "double"},
                {"format": "byte"},
                {"contentEncoding": "base64"},
                {"contentEncoding": "base64"},
            ]
        },
        format_typing=format_typing,
    )
    profile = doc.profile()
    for index in range(4):
        assert "xs:int" not in profile.at(f"/{index}").candidate_names
    expected_names = (
        "xs:int",
        "xs:long",
        "xs:long",
        "xs:float",
        "xs:double",
        "xs:base64Binary",
        "xs:base64Binary",
        "xs:base64Binary",
    )
    for index, name in enumerate(expected_names, start=4):
        row = profile.at(f"/{index}")
        candidate = row.candidate(name)
        assert candidate.from_schema_annotation
        assert (candidate.passed, candidate.failed) == ((0, 1) if index == 11 else (1, 0))
        assert row.interpretation == ("schema-guided" if format_typing and index < 11 else "native")

    items = profile.at("", "array-items")
    assert items.candidate_names == (
        *CANDIDATE_NAMES,
        "xs:int",
        "xs:long",
        "xs:float",
        "xs:base64Binary",
    )
    integer = items.candidate("xs:int")
    assert (integer.passed, integer.failed, integer.first_failure) == (
        4,
        6,
        DataLocation(doc.source, "/0"),
    )


def test_xml_profile_groups_occurrences_and_type_failures():
    doc = parse_xml(io.StringIO("<r><x>1</x><x>bad</x><x/></r>"))
    profile = doc.profile()
    row = profile.at("/r/x")
    assert (row.occurrences, row.parents, row.min_occurs, row.max_occurs) == (3, 1, 3, 3)
    integer = row.candidate("xs:integer")
    assert (integer.passed, integer.failed) == (1, 2)
    assert integer.first_failure.path[-1].index == 2


def test_profile_limits_and_snapshots_are_explicit():
    doc = parse_xml(io.StringIO("<r><x>1</x><x>2</x></r>"))
    profile = doc.profile(ProfileLimits(max_samples=1, max_distinct=1, max_value_bytes=8))
    assert profile.at("/r/x").samples == ("1",)
    assert not profile.complete
    doc.root.children[0].set_text("9")
    assert profile.revision_id == doc.root.revision.revision_id
    assert profile.revision_dirty is False and doc.root.revision.dirty is True
    assert profile.at("/r/x").samples == ("1",)  # immutable old evidence


def test_generic_clone_profile_reports_bounded_lexical_evidence():
    doc = parse_xml(b"<r><x>1</x><x>2</x></r>", types=TypePolicy.infer())
    clone = doc.transform(clone_lexical)
    profile = clone.profile(ProfileLimits(max_samples=1, max_distinct=1))

    row = profile.at("/r/x")
    assert row.interpretation == "lexical"
    assert row.occurrences == 2 and row.samples == ("1",)
    assert not profile.complete
    assert profile.revision_id == clone.root.revision.revision_id
    assert profile.revision_id != doc.root.revision.revision_id
    assert doc.profile().at("/r/x").interpretation == "inferred"


def test_profile_models_are_exported_frozen_snapshots():
    profile = parse_xml(io.StringIO("<r/>")).profile()
    assert isinstance(profile, DocumentProfile)
    assert profile.complete and profile.revision_dirty is False
    row = profile.at("/r")
    assert isinstance(row, PathProfile)
    assert isinstance(row.candidate("xs:integer"), TypeCandidate)
    assert isinstance(profile.rows, tuple)
    with pytest.raises(FrozenInstanceError):
        profile.complete = False
    with pytest.raises(FrozenInstanceError):
        row.occurrences = 2
    with pytest.raises(FrozenInstanceError):
        row.candidate("xs:integer").passed = 1


def test_xml_profile_groups_across_parents_and_records_missing_children():
    doc = parse_xml(b"<r><g><n>1</n><n>2</n></g><g><n>3</n></g><g/></r>")
    profile = doc.profile()
    assert (profile.at("/r").occurrences, profile.at("/r").parents) == (1, 1)
    assert (profile.at("/r/g").occurrences, profile.at("/r/g").parents) == (3, 1)
    assert (profile.at("/r/g").min_occurs, profile.at("/r/g").max_occurs) == (3, 3)
    row = profile.at("/r/g/n")
    assert (row.occurrences, row.parents) == (3, 3)
    assert (row.min_occurs, row.max_occurs, row.missing) == (0, 2, 1)
    assert row.samples == ("1", "2", "3")
    assert row.candidate("xs:integer").passed == 3
    assert row.candidate("xs:integer").failed == 0


def test_xml_profile_covers_attributes_empty_text_and_expanded_names():
    doc = parse_xml(b'<p:r xmlns:p="urn:e" p:id=" 1 " plain=""><p:x/><p:x>2</p:x></p:r>')
    profile = doc.profile()
    attribute = profile.at("/{urn:e}r/@{urn:e}id")
    assert (attribute.kind, attribute.occurrences, attribute.parents) == ("attribute", 1, 1)
    assert (attribute.min_occurs, attribute.max_occurs, attribute.missing) == (1, 1, 0)
    assert attribute.samples == (" 1 ",)
    assert attribute.candidate("xs:integer").passed == 1
    plain = profile.at("/{urn:e}r/@plain")
    assert (plain.occurrences, plain.empties) == (1, 1)
    assert plain.samples == ("",)
    children = profile.at("/{urn:e}r/{urn:e}x")
    assert (children.occurrences, children.parents, children.empties) == (2, 1, 1)
    assert (children.min_occurs, children.max_occurs) == (2, 2)
    assert children.samples == ("", "2")
    assert children.candidate("xs:integer").first_failure == IssueLocation(
        doc.source, (PathStep("{urn:e}r", 1), PathStep("{urn:e}x", 1))
    )


def test_xml_profile_counts_optional_attributes_across_occurrences():
    doc = parse_xml(b'<r><x a="1"/><x/><x a="2"/></r>')
    profile = doc.profile()
    row = profile.at("/r/x/@a")
    assert (row.kind, row.occurrences, row.parents) == ("attribute", 2, 3)
    assert (row.min_occurs, row.max_occurs, row.missing) == (0, 1, 1)
    assert row.samples == ("1", "2")
    assert profile.at("/r/x").occurrences == 3


def test_xml_profile_reports_attribute_interpretation_and_failure_locations():
    doc = parse_xml(b'<r a="1" b="bad"><x a="2"/></r>', types=TypePolicy.infer())
    profile = doc.profile()
    inferred = profile.at("/r/@a")
    assert inferred.kind == "attribute"
    assert inferred.interpretations == ("inferred",)
    assert inferred.interpretation == "inferred"
    assert inferred.candidate("xs:integer").passed == 1
    child = profile.at("/r/x/@a")
    assert child.interpretations == ("inferred",)
    assert child.candidate("xs:integer").passed == 1
    failing = profile.at("/r/@b")
    assert failing.interpretation == "lexical"
    integer = failing.candidate("xs:integer")
    assert (integer.passed, integer.failed) == (0, 1)
    assert integer.first_failure == IssueLocation(doc.source, (PathStep("r", 1),), "b")
    assert integer.first_failure.attribute == "b"
    assert not doc.pin_type("/r/@a", "xs:integer").has_errors
    pinned = doc.profile().at("/r/@a")
    assert pinned.interpretations == ("pinned",)
    assert pinned.interpretation == "pinned"
    assert pinned.candidate("xs:integer").passed == 1


def test_xml_profile_records_mixed_content_and_skips_container_text():
    doc = parse_xml(b"<r><p>hi<b>x</b>end</p><m><b>y</b>tail</m><q><c/></q><s>only</s></r>")
    profile = doc.profile()
    leading = profile.at("/r/p")
    assert (leading.occurrences, leading.mixed, leading.empties) == (1, 1, 0)
    assert leading.samples == ()
    assert leading.candidate_names == CANDIDATE_NAMES
    assert leading.candidate("xs:integer").passed == 0
    assert leading.candidate("xs:integer").failed == 0
    trailing = profile.at("/r/m")
    assert (trailing.mixed, trailing.empties) == (1, 0)
    element_only = profile.at("/r/q")
    assert (element_only.mixed, element_only.empties) == (0, 0)
    assert element_only.samples == ()
    text_only = profile.at("/r/s")
    assert (text_only.mixed, text_only.empties, text_only.samples) == (0, 0, ("only",))


def test_xml_profile_reports_policy_provenance_without_changing_reads():
    doc = parse_xml(b"<r><n>1</n><n>2</n><t>true</t><s>001</s></r>", types=TypePolicy.infer())
    before = doc.to_string()
    profile = doc.profile()
    assert profile.at("/r").interpretation == "lexical"
    assert profile.at("/r/n").interpretations == ("inferred",)
    assert profile.at("/r/n").candidate("xs:integer").passed == 2
    assert profile.at("/r/t").interpretation == "inferred"
    assert profile.at("/r/s").interpretation == "lexical"
    assert not doc.pin_type("/r/n[1]", "xs:integer").has_errors
    assert doc.root.children[0].scalar_value() == 1
    pinned = doc.profile()
    assert pinned.at("/r/n").interpretations == ("pinned", "inferred")
    assert pinned.at("/r/n").interpretation == "mixed"
    assert pinned.at("/r/t").interpretation == "inferred"
    assert profile.at("/r/n").interpretation == "inferred"
    assert doc.to_string() == before
    assert parse_xml(b"<r><n>1</n></r>").profile().at("/r/n").interpretation == "lexical"


def test_xml_profile_evaluates_candidates_without_applying_a_failing_pin():
    doc = parse_xml(b"<r><x>bad</x></r>")
    assert doc.pin_type("/r/x", "xs:integer").error_count == 1
    profile = doc.profile()
    row = profile.at("/r/x")
    assert row.interpretation == "pinned"
    assert (row.candidate("xs:integer").passed, row.candidate("xs:integer").failed) == (0, 1)
    assert row.candidate("xs:integer").first_failure.path[-1] == PathStep("x", 1)
    assert doc.root.children[0].text_value() == "bad"


def test_xml_profile_candidate_ranking_is_stable_and_addressable():
    row = parse_xml(b"<r><x>true</x></r>").profile().at("/r/x")
    assert row.candidate_names == CANDIDATE_NAMES
    assert (row.candidate("xs:boolean").passed, row.candidate("xs:boolean").failed) == (1, 0)
    assert row.candidate("xs:integer").failed == 1
    with pytest.raises(KeyError, match="xs:string"):
        row.candidate("xs:string")


def test_profile_path_budget_keeps_admitted_rows_complete_and_marks_the_profile():
    doc = parse_xml(b"<r><g><n/></g><g><n/></g><g/></r>")
    profile = doc.profile(ProfileLimits(max_paths=2))
    assert tuple(row.path for row in profile.rows) == ("/r", "/r/g")
    root = profile.at("/r")
    assert (root.occurrences, root.parents, root.truncated) == (1, 1, False)
    row = profile.at("/r/g")
    assert (row.occurrences, row.parents, row.min_occurs, row.max_occurs) == (3, 1, 3, 3)
    assert row.missing == 0
    assert row.truncated is False
    assert profile.complete is False


def test_profile_path_budget_skips_unadmitted_rows_but_continues_the_walk():
    doc = parse_xml(b'<r><x a="1"><c/></x><x a="2" b="2"><c/></x></r>')
    profile = doc.profile(ProfileLimits(max_paths=4))
    assert tuple(row.path for row in profile.rows) == ("/r", "/r/x", "/r/x/@a", "/r/x/c")
    assert profile.at("/r/x").occurrences == 2
    assert profile.at("/r/x/@a").occurrences == 2
    assert profile.at("/r/x/@a").truncated is False
    child = profile.at("/r/x/c")
    assert (child.occurrences, child.parents) == (2, 2)
    assert (child.min_occurs, child.max_occurs) == (1, 1)
    assert child.truncated is False
    assert not profile.complete


def test_profile_limits_bound_paths_distinct_samples_and_value_bytes():
    doc = parse_xml(b"<r><x>alpha</x><x>beta</x><x>alpha</x></r>")
    paths = doc.profile(ProfileLimits(max_paths=1))
    assert tuple(row.path for row in paths.rows) == ("/r",)
    assert paths.at("/r").occurrences == 1
    assert not paths.complete
    attributed = parse_xml(b'<r a="1"><x>1</x></r>').profile(ProfileLimits(max_paths=1))
    assert tuple(row.path for row in attributed.rows) == ("/r",)
    assert not attributed.complete
    bounded = doc.profile(ProfileLimits(max_distinct=1, max_samples=2, max_value_bytes=3))
    row = bounded.at("/r/x")
    assert row.distinct_values == ("alp",)
    assert row.samples == ("alp", "bet")
    assert (row.occurrences, row.min_occurs, row.max_occurs) == (3, 3, 3)
    assert row.candidate("xs:integer").failed == 3
    assert row.truncated and not bounded.complete
    within = doc.profile(ProfileLimits(max_distinct=2, max_samples=3, max_value_bytes=5))
    assert within.at("/r/x").distinct_values == ("alpha", "beta")
    assert within.at("/r/x").samples == ("alpha", "beta", "alpha")
    assert not within.at("/r/x").truncated and within.complete


def test_profile_value_byte_limit_keeps_valid_utf8():
    doc = parse_xml(b"<r><x>\xc3\xa9</x></r>")
    profile = doc.profile(ProfileLimits(max_value_bytes=1))
    assert profile.at("/r/x").samples == ("",)
    assert profile.at("/r/x").truncated and not profile.complete


def test_profile_limits_validate_their_budgets():
    assert ProfileLimits() == ProfileLimits()
    for field, value in [
        ("max_paths", 0),
        ("max_distinct", True),
        ("max_samples", -1),
        ("max_value_bytes", 0),
    ]:
        with pytest.raises(ValueError, match=field):
            ProfileLimits(**{field: value})
    with pytest.raises(ValueError, match="max_value_bytes"):
        ProfileLimits(max_value_bytes="8")  # type: ignore[arg-type]
    with pytest.raises(FrozenInstanceError):
        ProfileLimits().max_paths = 1


def test_profile_addresses_rows_by_path_and_kind():
    doc = from_json('{"a":[1]}')
    profile = doc.profile()
    assert profile.at("/a") is not profile.at("/a", kind="array-items")
    assert profile.at("/a", kind="value") is profile.at("/a")
    assert profile.at("/a", kind="array-items").occurrences == 1
    with pytest.raises(KeyError, match="absent"):
        profile.at("/absent")
    with pytest.raises(KeyError, match="absent"):
        profile.at("/a", kind="absent")


def test_profile_of_a_dirty_revision_reads_current_lexical_evidence():
    doc = parse_xml(b"<r><x>1</x></r>")
    doc.root.children[0].set_text("2")
    profile = doc.profile()
    assert profile.revision_id == doc.root.revision.revision_id
    assert profile.revision_dirty is True
    assert profile.at("/r/x").samples == ("2",)


def test_profile_failure_locations_use_the_source_occurrence_family():
    root = SchemalessElement.from_etree(ET.fromstring("<r><x/></r>"))
    root.children[0].set_text("")
    doc = SchemalessDocument(root, None, ValidationReport())
    profile = doc.profile()
    row = profile.at("/r/x")
    assert (row.empties, row.samples) == (1, ("",))
    assert row.candidate("xs:integer").first_failure == IssueLocation(
        None, (PathStep("r", 1), PathStep("x", 1))
    )


def test_profile_uses_absolute_paths_for_transformed_and_detached_trees():
    doc = parse_xml(b"<r><g><n>1</n></g></r>")
    child = doc.transform(lambda root: root.children[0])
    profile = child.profile()
    assert tuple(row.path for row in profile.rows) == ("/r/g", "/r/g/n")
    assert profile.revision_id == doc.root.revision.revision_id
    detached = parse_xml(b"<r><n>001</n></r>", types=TypePolicy.infer())
    assert not detached.pin_type("/r/n", "xs:integer").has_errors
    orphan = SchemalessDocument(
        detached.root.remove_child(0), None, ValidationReport(), report_current=False
    )
    assert orphan.profile().at("/n").interpretation == "lexical"


def test_profile_does_not_replace_the_policy_storage_or_xdm_view():
    doc = parse_xml(b"<r><x>1</x></r>", types=TypePolicy.infer())
    xdm = doc.to_xdm()
    policy = doc.root.revision.types
    profile = doc.profile()
    assert profile.complete
    assert doc.root.revision.types is policy
    assert doc.to_xdm() is xdm
    assert not doc.root.revision.dirty
    assert doc.to_string() == "<r><x>1</x></r>"


def test_json_profile_reports_native_kinds_nulls_and_empty_values():
    doc = from_json('{"o":{},"a":[],"s":"text","e":"","b":true,"i":1,"d":1.5,"n":null}')
    profile = doc.profile()
    assert profile.revision_id is None
    assert profile.revision_dirty is False
    assert profile.complete
    root = profile.at("")
    assert (root.kind, root.occurrences, root.parents) == ("value", 1, 1)
    assert (root.min_occurs, root.max_occurs) == (1, 1)
    assert root.native_kinds == ("object",)
    assert root.samples == ()
    assert root.candidate("xs:integer").failed == 0
    values = {row.path: row for row in profile.rows if row.kind == "value"}
    assert {path: row.native_kinds for path, row in values.items()} == {
        "": ("object",),
        "/o": ("object",),
        "/a": ("array",),
        "/s": ("string",),
        "/e": ("string",),
        "/b": ("boolean",),
        "/i": ("integer",),
        "/d": ("decimal",),
        "/n": ("null",),
    }
    assert (values["/o"].empties, values["/a"].empties, values["/e"].empties) == (1, 1, 1)
    assert (values["/n"].nulls, values["/n"].samples, values["/n"].empties) == (1, (), 0)
    assert values["/n"].candidate("xs:integer").passed == 0
    assert values["/i"].candidate("xs:integer").passed == 1
    assert values["/i"].candidate("xs:double").passed == 1
    assert values["/i"].candidate("xs:boolean").passed == 1
    assert values["/s"].candidate("xs:boolean").failed == 1
    assert values["/s"].candidate("xs:date").failed == 1
    assert from_data({"f": 1.25}).profile().at("/f").native_kinds == ("double",)


def test_json_profile_evaluates_native_booleans_as_boolean_candidates():
    profile = from_json('{"t":true,"f":false}').profile()
    truthy = profile.at("/t")
    assert truthy.native_kinds == ("boolean",)
    assert truthy.samples == ("true",)
    assert (truthy.candidate("xs:boolean").passed, truthy.candidate("xs:boolean").failed) == (1, 0)
    assert truthy.candidate("xs:integer").failed == 1
    falsy = profile.at("/f")
    assert falsy.native_kinds == ("boolean",)
    assert falsy.samples == ("false",)
    assert (falsy.candidate("xs:boolean").passed, falsy.candidate("xs:boolean").failed) == (1, 0)
    assert falsy.candidate("xs:date").failed == 1
    items = from_json("[true,false]").profile().at("", kind="array-items")
    assert items.native_kinds == ("boolean",)
    assert items.samples == ("true", "false")
    assert items.candidate("xs:boolean").passed == 2
    assert items.candidate("xs:integer").failed == 2


def test_json_profile_aggregates_array_members_at_the_array_pointer():
    doc = from_json('{"a":[1,"x",null,[],{}],"b":[]}')
    profile = doc.profile()
    items = profile.at("/a", kind="array-items")
    assert (items.kind, items.path, items.occurrences, items.parents) == ("array-items", "/a", 5, 1)
    assert (items.min_occurs, items.max_occurs, items.missing) == (5, 5, 0)
    assert (items.nulls, items.empties) == (1, 2)
    assert items.native_kinds == ("integer", "string", "array", "object", "null")
    assert items.samples == ("1", "x")
    assert items.distinct_values == ("1", "x")
    assert (items.candidate("xs:integer").passed, items.candidate("xs:integer").failed) == (1, 1)
    assert items.candidate("xs:date").first_failure == DataLocation(doc.source, "/a/0")
    assert profile.at("/a").native_kinds == ("array",)
    assert profile.at("/a/2").native_kinds == ("null",)
    assert profile.at("/a/2").nulls == 1
    assert profile.at("/a/3", kind="array-items").occurrences == 0
    empty = profile.at("/b", kind="array-items")
    assert (empty.occurrences, empty.missing, empty.interpretation) == (0, 1, "native")
    assert (empty.native_kinds, empty.samples, empty.distinct_values) == ((), (), ())


def test_json_profile_reports_policy_provenance_and_snapshots():
    doc = from_json('{"d":"2026-11-22","n":1}', types=TypePolicy.infer())
    policy = doc.types
    profile = doc.profile()
    assert doc.types is policy
    assert profile.at("").interpretation == "native"
    assert profile.at("/d").interpretation == "inferred"
    assert profile.at("/n").interpretation == "native"
    assert profile.at("/d").candidate("xs:date").passed == 1
    assert from_json('"42"', types=TypePolicy.infer()).profile().at("").interpretation == "inferred"
    declined = from_json('{"z":"abc"}', types=TypePolicy.infer()).profile()
    assert declined.at("/z").interpretation == "native"
    assert not doc.pin_type("/d", "xs:date").has_errors
    pinned = doc.profile()
    assert pinned.at("/d").interpretation == "pinned"
    assert profile.at("/d").interpretation == "inferred"
    assert doc.types is not policy
    assert pinned.revision_id is None and pinned.revision_dirty is False
    assert doc.to_data() == {"d": "2026-11-22", "n": 1}

    class Guided(JsonDocument):
        """A stand-in for the later schema-guidance layer."""

        def _asserted_adapter(self, pointer, value):
            return TypePolicy.resolve("xs:boolean") if pointer == "/x" else None

    guided = Guided({"x": "true"}).profile()
    assert guided.at("/x").interpretation == "schema-guided"
    assert guided.at("/x").candidate_names == CANDIDATE_NAMES


def test_json_profile_uses_escaped_pointers_and_bounded_paths():
    doc = from_json('{"a/b":{"~key":"v"}}')
    profile = doc.profile()
    assert tuple(row.path for row in profile.rows) == ("", "/a~1b", "/a~1b/~0key")
    assert profile.at("/a~1b").native_kinds == ("object",)
    assert profile.at("/a~1b/~0key").samples == ("v",)
    limited = doc.profile(ProfileLimits(max_paths=2))
    assert tuple(row.path for row in limited.rows) == ("", "/a~1b")
    assert limited.at("/a~1b").occurrences == 1
    assert not limited.complete
    array_limited = from_json("[1,2]").profile(ProfileLimits(max_paths=1))
    assert tuple(row.path for row in array_limited.rows) == ("",)
    assert not array_limited.complete


def test_json_profile_limits_bound_distinct_samples_and_value_bytes():
    doc = from_json('["abcd","abce","abcd"]')
    bounded = doc.profile(ProfileLimits(max_distinct=1, max_samples=2))
    items = bounded.at("", kind="array-items")
    assert items.distinct_values == ("abcd",)
    assert items.samples == ("abcd", "abce")
    assert items.truncated and not bounded.complete
    within = doc.profile(ProfileLimits(max_distinct=2, max_samples=3))
    assert within.at("", kind="array-items").distinct_values == ("abcd", "abce")
    assert within.at("", kind="array-items").samples == ("abcd", "abce", "abcd")
    assert within.complete and not within.at("", kind="array-items").truncated
    bytes_bound = from_json('"\u00e9"').profile(ProfileLimits(max_value_bytes=1))
    assert bytes_bound.at("").samples == ("",)
    assert bytes_bound.at("").truncated and not bytes_bound.complete
