"""Direct unit tests for the pure constraint algebra in ``pyxsd.wildcards``.

These pin the private helpers that the public predicates build on, so the
size-adjusted combination paths (XSD 1.1 intersection/union spellings) are
exercised independently of the schema corpus.
"""

from pyxsd.wildcards import (
    NAMESPACE_ANY,
    NAMESPACE_LOCAL,
    NAMESPACE_TARGET,
    WildcardSpec,
    _combine_namespace,
    _exclusion_overlap,
    _namespace_constraint,
    _process_contents_name,
    _token_excluded,
    invalid_not_namespace,
    invalid_not_qname,
    replace_wildcard,
    wildcard_specs_overlap,
)


def test_invalid_not_namespace_absent_is_not_reported():
    assert invalid_not_namespace(None) is None


def test_invalid_not_qname_absent_is_not_reported():
    assert invalid_not_qname(None) is None


def test_invalid_not_qname_reserved_keyword_is_rejected():
    assert invalid_not_qname("##bogus") == "##bogus"


def test_invalid_not_qname_clark_token_is_accepted():
    assert invalid_not_qname("{urn:x}a") is None


def test_replace_wildcard_without_a_spec_list_is_a_noop():
    class _TypeWithoutSpecs:
        pass

    containing = _TypeWithoutSpecs()
    replace_wildcard(containing, WildcardSpec(), WildcardSpec(namespace="##local"))
    assert not getattr(containing, "wildcardElementSpecs", None)


def test_exclusion_overlap_with_an_unbounded_side():
    first = WildcardSpec(namespace=NAMESPACE_ANY)
    second = WildcardSpec(not_namespace=frozenset({"urn:x"}))
    assert _exclusion_overlap(first, second, None)


def test_exclusion_overlap_two_complements_always_overlap():
    first = WildcardSpec(not_namespace=frozenset({"urn:x"}))
    second = WildcardSpec(not_namespace=frozenset({"urn:y"}))
    assert _exclusion_overlap(first, second, None)


def test_overlap_two_other_wildcards():
    assert wildcard_specs_overlap(
        WildcardSpec(namespace="##other"), WildcardSpec(namespace="##other")
    )


def test_overlap_other_versus_local_is_disjoint():
    assert not wildcard_specs_overlap(
        WildcardSpec(namespace="##other"), WildcardSpec(namespace=NAMESPACE_LOCAL)
    )


def test_overlap_two_local_wildcards():
    assert wildcard_specs_overlap(
        WildcardSpec(namespace=NAMESPACE_LOCAL), WildcardSpec(namespace=NAMESPACE_LOCAL)
    )


def test_overlap_two_target_wildcards():
    assert wildcard_specs_overlap(
        WildcardSpec(namespace=NAMESPACE_TARGET), WildcardSpec(namespace=NAMESPACE_TARGET)
    )


def test_overlap_target_keyword_against_a_uri_set_containing_the_target():
    assert wildcard_specs_overlap(
        WildcardSpec(namespace="urn:t"),
        WildcardSpec(namespace=NAMESPACE_TARGET),
        "urn:t",
    )


def test_namespace_constraint_not_namespace_target_token():
    spec = WildcardSpec(not_namespace=frozenset({NAMESPACE_TARGET}))
    assert _namespace_constraint(spec, "urn:t") == ("not", frozenset({"urn:t"}))


def test_namespace_constraint_not_namespace_literal_and_local_tokens():
    spec = WildcardSpec(not_namespace=frozenset({"urn:x", NAMESPACE_LOCAL}))
    kind, members = _namespace_constraint(spec, None)
    assert kind == "not"
    assert members == frozenset({"urn:x", None})


def test_namespace_constraint_empty_namespace_admits_nothing():
    assert _namespace_constraint(WildcardSpec(namespace=""), None) == ("enum", frozenset())


def test_token_excluded_malformed_token_is_never_excluded():
    assert _token_excluded("##bogus", frozenset({"{urn:x}a"})) is False


def test_combine_namespace_uses_the_argument_target_when_no_spec_records_one():
    base = WildcardSpec(namespace="##other", target_namespace="urn:elsewhere")
    own = WildcardSpec(namespace="##other", target_namespace="urn:elsewhere")
    _, target, _ = _combine_namespace("not", frozenset({"urn:t", "urn:x"}), base, own, "urn:t")
    assert target == "urn:t"


def test_combine_namespace_any_when_nothing_present():
    base = WildcardSpec(namespace="##other", target_namespace="urn:elsewhere")
    own = WildcardSpec(namespace="##other", target_namespace="urn:elsewhere")
    assert _combine_namespace("not", frozenset(), base, own, "urn:t") == (
        NAMESPACE_ANY,
        "urn:t",
        frozenset(),
    )


def test_process_contents_name_unknown_severity_reads_strict():
    assert _process_contents_name(999) == "strict"
