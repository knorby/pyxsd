"""Exact scalar assertions, never datatype choices or resource permissions."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from pyxsd import relaxng
from pyxsd.data_conversion.values import scalar_to_json
from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import XML_NS
from pyxsd.relaxng import json_values
from pyxsd.relaxng.datatypes import DatatypeContext
from pyxsd.relaxng.json_values import (
    ScalarTagClaim,
    ScalarTagLocation,
    parse_scalar_tag,
    scalar_tag,
    scalar_to_relaxng_json,
    verify_tag_claims,
)
from pyxsd.relaxng.values import alternative_value_from_witnesses

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
CHOICE = '<choice><data type="integer"/><data type="string"/></choice>'
POINTER = "/r/#content/0"


def parse(content=CHOICE, xml="<r>12</r>", *, typed=True):
    schema = relaxng.compile(
        io.StringIO(f'<element xmlns="{NS}" name="r" datatypeLibrary="{XSD}">{content}</element>')
    )
    assert not schema.report.has_errors, schema.report.issues
    return schema.parse(io.StringIO(xml), typed=typed)


def wire():
    return {
        "#value": {
            "version": 1,
            "grammar": "grammar",
            "lexical": "12",
            "context": {"base_uri": None, "namespaces": {}},
            "candidates": ["a", "b"],
        }
    }


def claim_for(node, *, location=None, attribute=None, pointer=POINTER):
    value = node.scalar_value() if attribute is None else node.attribute_value(attribute)
    return replace(parse_scalar_tag(scalar_tag(value), pointer), location=location)


def assert_conversion(code, action, pointer=POINTER):
    with pytest.raises(PyXSDError) as caught:
        action()
    assert str(caught.value).startswith(f"{code} at {pointer!r}: ")


def test_exact_tag_has_only_wire_identity_and_copied_context():
    document = parse()
    value = document.root.scalar_value()
    tag = scalar_tag(value)
    expected = {
        "#value": {
            "version": 1,
            "grammar": document.schema._grammar_fingerprint,
            "lexical": "12",
            "context": {"base_uri": None, "namespaces": {}},
            "candidates": sorted(candidate.candidate_id for candidate in value.candidates),
        }
    }
    assert tag == expected
    assert scalar_tag(value) == tag
    encoded = json.dumps(tag, allow_nan=False)
    for forbidden in ("decoded", "proof", "revision", "source", "location", "0x", "type_name"):
        assert forbidden not in encoded
    tag["#value"]["context"]["namespaces"]["p"] = "urn:changed"
    assert scalar_tag(value) == expected
    parsed = parse_scalar_tag(expected, POINTER)
    assert parsed == ScalarTagClaim(
        POINTER,
        value.grammar_fingerprint,
        "12",
        DatatypeContext(None, {}),
        tuple(expected["#value"]["candidates"]),
    )
    assert parsed.location is None


def test_decoded_python_objects_are_never_serialized_or_represented():
    class PrivateValue:
        def __repr__(self):
            pytest.fail("decoded object's repr escaped")

    value = parse().root.scalar_value()
    candidates = tuple(replace(candidate, decoded=PrivateValue()) for candidate in value.candidates)
    private = replace(value, candidates=candidates)
    assert json.dumps(scalar_tag(private)) == json.dumps(scalar_tag(value))


@pytest.mark.parametrize("pointer", ["", "/@a", "/r/$", "/r/#content/0", "/a~1b/~0"])
def test_pointer_is_preserved_without_guessing_any_tree_position(pointer):
    claim = parse_scalar_tag(wire(), pointer)
    assert claim.pointer == pointer
    assert claim.location is None
    assert_conversion(
        "scalar-tag-invalid", lambda: parse_scalar_tag({"r": wire()}, pointer), pointer
    )


@pytest.mark.parametrize("format", ["pyxsd", "pyxsd-json"])
def test_typed_native_and_profile_tag_ambiguity(format):
    value = parse().root.scalar_value()
    assert scalar_to_relaxng_json(value, format=format) == scalar_tag(value)


@pytest.mark.parametrize("format", ["pyxsd", "pyxsd-json", "parker", "badgerfish", "jsonml"])
def test_lexical_modes_and_shared_unique_json_safety(format):
    value = parse().root.scalar_value()
    if format not in ("pyxsd", "pyxsd-json"):
        assert scalar_to_relaxng_json(value, format=format) == "12"
    assert scalar_to_relaxng_json(value, format=format, typed=False) == "12"
    for unique in (None, True, 12, Decimal("1.234567890123456789"), float("inf"), float("nan")):
        assert scalar_to_relaxng_json(unique, format=format) == scalar_to_json(
            unique, format=format
        )
    assert scalar_to_relaxng_json(" 12 ", format=format, typed=False) == " 12 "
    assert scalar_to_relaxng_json((12, Decimal("2.0")), format=format) == [12, "2.0"]
    assert scalar_to_relaxng_json([12, [13]], format=format) == [12, [13]]
    assert_conversion(
        "scalar-type-invalid",
        lambda: scalar_to_relaxng_json(12, format=format, typed=False),
        pointer="",
    )
    assert_conversion(
        "scalar-type-invalid",
        lambda: scalar_to_relaxng_json(object(), format=format),
        pointer="",
    )


def test_invalid_format_is_validated_even_before_wrapper_dispatch():
    with pytest.raises(ValueError, match="unknown data format: 'bogus'"):
        scalar_to_relaxng_json(parse().root.scalar_value(), format="bogus")


def test_nondecodable_text_and_original_list_separators_are_not_decoded(monkeypatch):
    value = parse("<text/>", "<r>  12 </r>").root.scalar_value()
    assert not value.candidates[0].decodable
    assert parse_scalar_tag(scalar_tag(value), POINTER).lexical == "  12 "
    assert scalar_to_relaxng_json(value, format="badgerfish") == "  12 "
    document = parse(f"<list>{CHOICE}{CHOICE}</list>", "<r> 12\t 34\n</r>")
    value = document.root.scalar_value()
    interpretation = value.interpretations[0]
    assert interpretation.lexical == " 12\t 34\n"
    monkeypatch.setattr(type(interpretation), "as_python", lambda self: pytest.fail("decoded"))
    tag = scalar_tag(interpretation)
    assert tag["#value"]["candidates"] == [interpretation.candidate_id]
    assert tag["#value"]["lexical"] == interpretation.lexical
    assert scalar_to_relaxng_json(interpretation, format="pyxsd-json") == tag
    assert scalar_to_relaxng_json(interpretation, format="parker") == interpretation.lexical
    assert (
        scalar_to_relaxng_json(interpretation, format="pyxsd", typed=False)
        == interpretation.lexical
    )
    verify_tag_claims([parse_scalar_tag(tag, POINTER)], document)


@pytest.mark.parametrize("format", ["pyxsd", "pyxsd-json"])
@pytest.mark.parametrize(
    ("pattern", "candidate_count"),
    [("<text/>", 1), ('<choice><text/><data type="string"/></choice>', 2)],
)
def test_empty_lexical_alternatives_round_trip_and_verify(format, pattern, candidate_count):
    document = parse(f'<attribute name="a">{pattern}</attribute>', '<r a=""/>')
    assert document.is_valid
    value = document.root.attribute_value("a")
    assert isinstance(value, relaxng.AlternativeValue)
    assert value.lexical == ""
    assert len(value.candidates) == candidate_count
    tag = scalar_to_relaxng_json(value, format=format)
    assert tag["#value"]["lexical"] == ""
    assert tag["#value"]["candidates"] == sorted(item.candidate_id for item in value.candidates)
    assert all(tag["#value"]["candidates"])
    claim = parse_scalar_tag(json.loads(json.dumps(tag)), "/@a")
    assert claim.lexical == ""
    verify_tag_claims([claim], document)
    verify_tag_claims([replace(claim, location=ScalarTagLocation((), attribute="a"))], document)


@pytest.mark.parametrize("format", ["pyxsd", "pyxsd-json"])
def test_empty_permitted_whole_list_and_interpretation_round_trip_and_verify(format):
    document = parse(
        f'<attribute name="a"><list><zeroOrMore>{CHOICE}</zeroOrMore></list></attribute>',
        '<r a=""/>',
    )
    assert document.is_valid
    value = document.root.attribute_value("a")
    assert isinstance(value, relaxng.AlternativeValue)
    assert value.lexical == ""
    assert len(value.candidates) == len(value.interpretations) == 1
    interpretation = value.interpretations[0]
    assert interpretation.lexical == ""
    assert interpretation.token_spans == interpretation.token_candidates == ()
    assert interpretation.candidate_id
    for scalar in (value, interpretation):
        tag = scalar_tag(scalar)
        assert scalar_to_relaxng_json(scalar, format=format) == tag
        assert tag["#value"]["lexical"] == ""
        assert tag["#value"]["candidates"] == [interpretation.candidate_id]
        claim = parse_scalar_tag(json.loads(json.dumps(tag)), "/@a")
        assert claim.lexical == ""
        assert claim.candidates == (interpretation.candidate_id,)
        verify_tag_claims([claim], document)
        verify_tag_claims([replace(claim, location=ScalarTagLocation((), attribute="a"))], document)


def test_context_default_empty_and_implicit_xml_are_preserved_without_leaking():
    value = parse().root.scalar_value()
    for namespaces in ({}, {"": ""}, {"": "urn:default", "p": "urn:p", "xml": XML_NS}):
        context = DatatypeContext("https://example.test/a/", namespaces)
        tag = scalar_tag(replace(value, context=context))
        assert tag["#value"]["context"] == {
            "base_uri": context.base_uri,
            "namespaces": {key: uri for key, uri in namespaces.items() if key != "xml"},
        }
        parsed = parse_scalar_tag(tag, POINTER)
        assert dict(parsed.context.namespaces) == tag["#value"]["context"]["namespaces"]
    assert_conversion(
        "scalar-tag-context",
        lambda: scalar_tag(replace(value, context=DatatypeContext(None, {"xml": "urn:wrong"}))),
        pointer="",
    )


@pytest.mark.parametrize("value", [None, [], 1, {}, {"#value": {}, "extra": 1}, {"r": wire()}])
def test_outer_shape_rejected_at_supplied_pointer(value):
    assert_conversion("scalar-tag-invalid", lambda: parse_scalar_tag(value, POINTER))


@pytest.mark.parametrize("body", [None, [], "x", {}, {**wire()["#value"], "extra": 1}])
def test_body_shape_rejected_at_supplied_pointer(body):
    assert_conversion("scalar-tag-invalid", lambda: parse_scalar_tag({"#value": body}, POINTER))


@pytest.mark.parametrize("key", ["version", "grammar", "lexical", "context", "candidates"])
def test_each_body_key_is_required(key):
    tag = wire()
    del tag["#value"][key]
    assert_conversion("scalar-tag-invalid", lambda: parse_scalar_tag(tag, POINTER))


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 0, 2, None])
def test_version_is_exact_integer_one(version):
    tag = wire()
    tag["#value"]["version"] = version
    assert_conversion("scalar-tag-version", lambda: parse_scalar_tag(tag, POINTER))


@pytest.mark.parametrize("key", ["grammar", "lexical"])
@pytest.mark.parametrize("bad", [None, 12, [], True])
def test_grammar_and_lexical_must_be_strings(key, bad):
    tag = wire()
    tag["#value"][key] = bad
    assert_conversion(f"scalar-tag-{key}", lambda: parse_scalar_tag(tag, POINTER))


def test_grammar_must_still_be_nonempty():
    tag = wire()
    tag["#value"]["grammar"] = ""
    assert_conversion("scalar-tag-grammar", lambda: parse_scalar_tag(tag, POINTER))


@pytest.mark.parametrize(
    "context",
    [
        None,
        [],
        {},
        {"base_uri": None},
        {"namespaces": {}},
        {"base_uri": None, "namespaces": {}, "extra": 1},
        {"base_uri": 1, "namespaces": {}},
        {"base_uri": None, "namespaces": []},
        {"base_uri": None, "namespaces": {1: "urn:p"}},
        {"base_uri": None, "namespaces": {"p": None}},
        {"base_uri": None, "namespaces": {"xml": XML_NS}},
        {"base_uri": None, "namespaces": {"xml": "urn:wrong"}},
    ],
)
def test_malformed_context_is_rejected(context):
    tag = wire()
    tag["#value"]["context"] = context
    assert_conversion("scalar-tag-context", lambda: parse_scalar_tag(tag, POINTER))


@pytest.mark.parametrize(
    "candidates", [None, "a", (), [], [""], [1], [True], ["a", "a"], ["b", "a"], ["a", None]]
)
def test_candidate_ids_are_nonempty_sorted_unique_string_array(candidates):
    tag = wire()
    tag["#value"]["candidates"] = candidates
    assert_conversion("scalar-tag-candidates", lambda: parse_scalar_tag(tag, POINTER))


def test_parse_copies_wire_data_and_claims_are_immutable():
    tag = wire()
    parsed = parse_scalar_tag(tag, POINTER)
    original = deepcopy(tag)
    tag["#value"]["candidates"].append("c")
    tag["#value"]["context"]["namespaces"]["p"] = "urn:p"
    assert parsed == parse_scalar_tag(original, POINTER)
    with pytest.raises(FrozenInstanceError):
        parsed.grammar = "other"
    with pytest.raises(TypeError):
        parsed.context.namespaces["p"] = "urn:p"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"element_path": []},
        {"element_path": (-1,)},
        {"element_path": (True,)},
        {"element_path": ("0",)},
        {"content_index": -1},
        {"content_index": True},
        {"content_index": 1.0},
        {"attribute": "", "content_index": None},
        {"attribute": 1, "content_index": None},
        {"attribute": "p:a", "content_index": None},
        {"attribute": "{urn:x", "content_index": None},
        {"attribute": "{urn:x}", "content_index": None},
        {"attribute": "bad name", "content_index": None},
        {"attribute": "a", "content_index": 0},
        {"content_index": None},
    ],
)
def test_location_validation(kwargs):
    options = {"element_path": (), "attribute": None, "content_index": 0, **kwargs}
    with pytest.raises(ValueError):
        ScalarTagLocation(**options)


def test_location_is_immutable_nonwire_metadata_in_equality_not_repr():
    unbound = parse_scalar_tag(wire(), POINTER)
    location = ScalarTagLocation((1, 0), attribute="{urn:x}a")
    bound = replace(unbound, location=location)
    assert bound != unbound
    assert repr(bound) == repr(unbound)
    with pytest.raises(FrozenInstanceError):
        location.attribute = "other"
    assert_conversion("scalar-tag-location", lambda: replace(unbound, location="bad"))
    assert_conversion("scalar-tag-context", lambda: replace(unbound, context={}))


def test_bound_claims_use_exact_element_attribute_and_content_occurrences():
    content = (
        f'<attribute name="a" ns="urn:x">{CHOICE}</attribute><element name="x">{CHOICE}</element>'
    )
    document = parse(content, '<r xmlns:p="urn:x" p:a="12"><x>12</x></r>')
    claims = [
        claim_for(
            document.root,
            attribute="{urn:x}a",
            location=ScalarTagLocation((), attribute="{urn:x}a"),
        ),
        claim_for(document.root.children[0], location=ScalarTagLocation((0,), content_index=0)),
    ]
    verify_tag_claims(claims, document)
    for location in (
        ScalarTagLocation((1,), content_index=0),
        ScalarTagLocation((), content_index=0),
        ScalarTagLocation((), attribute="a"),
        ScalarTagLocation((0,), content_index=1),
    ):
        assert_conversion(
            "scalar-tag-location-mismatch",
            lambda location=location: verify_tag_claims(
                [replace(claims[0], location=location)], document
            ),
        )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("grammar", "other", "scalar-tag-grammar-mismatch"),
        ("lexical", "13", "scalar-tag-lexical-mismatch"),
        ("context", DatatypeContext("https://wrong.test/", {}), "scalar-tag-context-mismatch"),
        ("context", DatatypeContext(None, {"p": "urn:wrong"}), "scalar-tag-context-mismatch"),
        ("candidates", ("unknown",), "scalar-tag-candidates-mismatch"),
    ],
)
def test_bound_claim_recomputes_every_assertion(field, value, code):
    document = parse()
    claim = claim_for(document.root, location=ScalarTagLocation((), content_index=0))
    assert_conversion(code, lambda: verify_tag_claims([replace(claim, **{field: value})], document))


def test_missing_extra_and_stale_candidate_ids_are_not_filters():
    document = parse()
    claim = claim_for(document.root, location=ScalarTagLocation((), content_index=0))
    for candidates in (claim.candidates[:1], tuple(sorted((*claim.candidates, "unknown")))):
        assert_conversion(
            "scalar-tag-candidates-mismatch",
            lambda candidates=candidates: verify_tag_claims(
                [replace(claim, candidates=candidates)], document
            ),
        )
    other = parse("<choice><text/><data type='string'/></choice>")
    stale = claim_for(other.root)
    assert_conversion(
        "scalar-tag-candidates-mismatch",
        lambda: verify_tag_claims([replace(claim, candidates=stale.candidates)], document),
    )
    assert len(document.root.scalar_value().candidates) == 2


def test_unbound_claim_requires_exactly_one_matching_occurrence():
    document = parse()
    claim = claim_for(document.root)
    verify_tag_claims([claim], document)
    assert_conversion(
        "scalar-tag-occurrence-mismatch",
        lambda: verify_tag_claims([replace(claim, lexical="13")], document),
    )
    repeated = parse(
        f'<oneOrMore><element name="x">{CHOICE}</element></oneOrMore>',
        "<r><x>12</x><x>12</x></r>",
    )
    claims = [
        claim_for(child, location=ScalarTagLocation((i,), content_index=0), pointer=f"/x/{i}")
        for i, child in enumerate(repeated.root.children)
    ]
    verify_tag_claims(claims, repeated)
    assert_conversion(
        "scalar-tag-occurrence-ambiguous",
        lambda: verify_tag_claims([replace(claims[0], location=None)], repeated),
        pointer="/x/0",
    )
    assert_conversion(
        "scalar-tag-location-duplicate",
        lambda: verify_tag_claims([claims[0], replace(claims[0], pointer="/x/1")], repeated),
        pointer="/x/1",
    )


def test_equal_lexical_at_different_sites_keeps_distinct_complete_candidates():
    document = parse(
        f'<element name="x">{CHOICE}</element><element name="x"><text/></element>',
        "<r><x>12</x><x>12</x></r>",
    )
    claims = [
        claim_for(child, location=ScalarTagLocation((i,), content_index=0))
        for i, child in enumerate(document.root.children)
    ]
    assert claims[0].lexical == claims[1].lexical
    assert claims[0].candidates != claims[1].candidates
    verify_tag_claims(claims, document)
    verify_tag_claims([replace(claim, location=None) for claim in claims], document)
    assert_conversion(
        "scalar-tag-candidates-mismatch",
        lambda: verify_tag_claims([replace(claims[0], location=claims[1].location)], document),
    )


def test_tag_assertions_preserve_correlated_complete_sets_without_selecting():
    def branch(name):
        return "".join(
            f'<element name="{tag}"><data type="{name}"/></element>' for tag in ("x", "y")
        )

    document = parse(
        f"<choice><group>{branch('integer')}</group><group>{branch('string')}</group></choice>",
        "<r><x>12</x><y>34</y></r>",
    )
    claims = [
        claim_for(child, location=ScalarTagLocation((i,), content_index=0))
        for i, child in enumerate(document.root.children)
    ]
    graph = document.binding_evidence
    before = graph.retained_edges
    verify_tag_claims(claims, document)
    assert all(len(claim.candidates) == 2 for claim in claims)
    assert graph.retained_edges is before
    assert_conversion(
        "scalar-tag-candidates-mismatch",
        lambda: verify_tag_claims(
            [replace(claims[0], candidates=claims[0].candidates[:1]), claims[1]], document
        ),
    )


def test_ordered_mixed_content_claims_have_distinct_character_indexes():
    document = parse(
        '<mixed><element name="x"><empty/></element></mixed>',
        "<r>before<x/>after</r>",
    )
    graph = document.binding_evidence
    sites = [site for site in graph.occurrences if site.kind == "text"]
    claims = []
    for site in sites:
        witnesses = graph.scalar_witnesses_at(site)
        if not witnesses:
            continue
        value = alternative_value_from_witnesses(
            witnesses,
            grammar_fingerprint=document.schema._grammar_fingerprint,
            revision=document.root.revision.revision_id,
        )
        claims.append(
            replace(
                parse_scalar_tag(scalar_tag(value), POINTER),
                location=ScalarTagLocation((), content_index=site.content_index),
            )
        )
    assert len(claims) == 2
    assert {claim.lexical for claim in claims} == {"before", "after"}
    assert len({claim.location for claim in claims}) == 2
    verify_tag_claims(claims, document)


def test_context_comparison_ignores_only_standard_implicit_xml():
    document = parse(xml=f'<r xmlns:xml="{XML_NS}">12</r>')
    claim = claim_for(document.root, location=ScalarTagLocation((), content_index=0))
    assert dict(document.root.scalar_value().context.namespaces) == {"xml": XML_NS}
    verify_tag_claims([claim], document)


def test_invalid_document_skips_authoritative_verification():
    document = parse('<data type="integer"/>', "<r>not-integer</r>")
    assert not document.is_valid
    verify_tag_claims([parse_scalar_tag(wire(), POINTER)], document)


def test_validation_only_stale_and_missing_evidence_raise_clear_errors(monkeypatch):
    claim = parse_scalar_tag(wire(), POINTER)
    assert_conversion(
        "scalar-tag-evidence-required",
        lambda: verify_tag_claims([claim], parse(typed=False)),
    )
    document = parse()
    document.root.set_text("13")
    assert_conversion("scalar-tag-evidence-stale", lambda: verify_tag_claims([claim], document))
    document = parse()
    transformed = document.transform(lambda root: root)
    assert_conversion("scalar-tag-evidence-stale", lambda: verify_tag_claims([claim], transformed))
    document = parse()
    monkeypatch.setattr(document, "_binding_evidence", None)
    assert_conversion("scalar-tag-evidence-required", lambda: verify_tag_claims([claim], document))
    verify_tag_claims([], parse())


def test_verification_never_invokes_decoders(monkeypatch):
    document = parse()
    claim = claim_for(document.root)
    for witness in document.binding_evidence.scalar_witnesses_at(
        next(site for site in document.binding_evidence.occurrences if site.kind == "text")
    ):
        monkeypatch.setattr(type(witness.datatype), "decode", lambda *a: pytest.fail("decoded"))
    verify_tag_claims([claim], document)


def test_hash_seed_independent_output():
    script = f"""
import io, json
from pyxsd import relaxng
from pyxsd.relaxng.json_values import scalar_tag
s = relaxng.compile(io.StringIO({f'<element xmlns="{NS}" name="r" datatypeLibrary="{XSD}">{CHOICE}</element>'!r}))
d = s.parse(io.StringIO('<r xmlns:z="urn:z" xmlns:a="urn:a">12</r>'), typed=True)
print(json.dumps(scalar_tag(d.root.scalar_value()), separators=(',', ':')))
"""
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", script], env={**os.environ, "PYTHONHASHSEED": seed}, text=True
        )
        for seed in ("1", "72")
    ]
    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0])["#value"]["candidates"]


def test_raw_to_dict_retains_wrapper_without_calling_json_helper(monkeypatch):
    from pyxsd.relaxng import conversion

    document = parse()
    value = document.root.scalar_value()

    class RawAdapter:
        requires_current_typed_evidence = True

        def export(self, doc, *, mode, **kwargs):
            assert doc is document
            assert mode == "raw"
            return value

    monkeypatch.setitem(conversion._ADAPTERS, "pyxsd", RawAdapter())
    monkeypatch.setattr(
        json_values, "scalar_to_relaxng_json", lambda *a, **k: pytest.fail("tagged")
    )
    assert document.to_dict() is value
