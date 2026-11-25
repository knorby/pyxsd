"""Check applied JSON Schema guidance without conflating it with validation."""

from decimal import Decimal

import pytest
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from pyxsd import xsd_data_types as xsd
from pyxsd.remote import RemoteResourcePolicy
from pyxsd.schemaless import TypePolicy, from_data, from_json
from pyxsd.schemaless.json_schema import SchemaGuidance
from pyxsd.schemaless.json_xdm import json_to_xdm
from pyxsd.xdm import XdmArray, XdmAtomic


def test_integer_assertion_guides_xdm_without_changing_native_json():
    doc = from_json("1.0", json_schema={"type": "integer"})
    assert isinstance(doc.to_data(), Decimal)
    assert doc.to_xdm()[0].type_name == "{http://www.w3.org/2001/XMLSchema}integer"


def test_format_is_a_candidate_not_an_automatic_xdm_date():
    doc = from_json('"2026-11-22"', json_schema={"type": "string", "format": "date"})
    assert doc.to_xdm()[0].type_name.endswith("string")
    assert "xs:date" in doc.profile().at("").candidate_names


def test_invalid_instance_uses_only_native_or_explicit_typing():
    doc = from_json('"1"', json_schema={"type": "integer"})
    assert doc.to_xdm()[0].type_name.endswith("string")
    doc.pin_type("", "xs:integer")
    assert doc.to_xdm()[0].type_name.endswith("integer")


def test_typed_subtree_and_profiles_use_the_same_escaped_pointers_and_precedence():
    schema = {
        "properties": {
            "a/~": {
                "prefixItems": [
                    {"type": "integer"},
                    {"type": "string"},
                    {"type": "boolean"},
                ],
            }
        }
    }
    doc = from_json(
        '{"a/~":[1.0,"2",true,"3","as-is"]}', json_schema=schema, types=TypePolicy.infer()
    )
    typed = doc.value_at("/a~1~0", typed=True)
    assert typed == [1, "2", True, 3, "as-is"]
    assert isinstance(typed[0], int)
    assert doc.to_xdm()[0].get(XdmAtomic.string("a/~"))[0].get(1) == (XdmAtomic.integer(1),)
    before = doc.profile()
    assert before.at("/a~1~0/0").interpretation == "schema-guided"
    assert before.at("/a~1~0/0").native_kinds == ("decimal",)
    assert before.at("/a~1~0/1").interpretation == "schema-guided"
    assert before.at("/a~1~0/2").interpretation == "schema-guided"
    assert before.at("/a~1~0/3").interpretation == "inferred"
    assert before.at("/a~1~0/4").interpretation == "native"
    assert before.at("/a~1~0", "array-items").interpretations == (
        "schema-guided",
        "inferred",
        "native",
    )
    assert not doc.pin_type("/a~1~0/0", "xs:decimal").has_errors
    assert isinstance(doc.value_at("/a~1~0/0", typed=True), Decimal)
    assert doc.profile().at("/a~1~0/0").interpretation == "pinned"
    assert before.at("/a~1~0/0").interpretation == "schema-guided"
    assert doc.to_json() == '{"a/~": [1.0, "2", true, "3", "as-is"]}'
    assert doc.is_valid


def test_invalid_document_disables_inference_and_guidance_even_at_locally_valid_leaves():
    schema = {"required": ["missing"], "properties": {"a": {"type": "integer"}}}
    doc = from_json('{"a":1.0,"b":"2"}', json_schema=schema, types=TypePolicy.infer())
    assert not doc.is_valid
    assert doc.report.error_count == 1
    assert isinstance(doc.value_at("/a", typed=True), Decimal)
    assert doc.to_xdm()[0].get(XdmAtomic.string("b")) == (XdmAtomic.string("2"),)
    assert doc.profile().at("/a").interpretation == "native"
    assert doc.profile().at("/b").interpretation == "native"
    doc.pin_type("/b", "xs:integer")
    assert doc.value_at("/b", typed=True) == 2
    assert doc.report.error_count == 1


@pytest.mark.parametrize(
    ("schema", "value", "expected"),
    [
        ({"type": "integer"}, 1e20, XdmAtomic.integer(100000000000000000000)),
        ({"type": "integer"}, Decimal("-0E+1000000000"), XdmAtomic.integer(0)),
        ({"type": "integer"}, Decimal("1e1000000000"), XdmAtomic.decimal(Decimal("1e1000000000"))),
        ({"type": "number"}, Decimal("1e-1000000000"), XdmAtomic.decimal(Decimal("1e-1000000000"))),
        ({"type": "number"}, 1, XdmAtomic.decimal(1)),
        pytest.param(
            {"type": "number"}, 10**4400, XdmAtomic.decimal(10**4400), id="large-host-int"
        ),
        ({"type": "number"}, 1e-25, XdmAtomic.decimal(Decimal("1e-25"))),
        ({"type": ["integer", "null"]}, Decimal("1.0"), XdmAtomic.integer(1)),
        ({"type": ["integer", "number"]}, Decimal("1.0"), XdmAtomic.decimal(Decimal("1.0"))),
    ],
)
def test_numeric_assertions_preserve_value_without_reinterpreting_the_native_store(
    schema, value, expected
):
    doc = from_data(value, json_schema=schema)
    assert doc.is_valid
    assert type(doc.to_data()) is type(value)
    assert doc.to_xdm() == (expected,)
    assert doc.to_xdm()[0].type_name == expected.type_name


def test_reference_occurrences_use_target_pointers_and_keep_sibling_hints():
    uri = "https://example.test/types/number.json"
    registry = Registry().with_resource(
        uri,
        DRAFT202012.create_resource({"$defs": {"a/~": {"$anchor": "whole", "type": "integer"}}}),
    )
    schema = {
        "$id": "https://example.test/root.json",
        "properties": {"v": {"$id": "types/local.json", "$ref": "number.json#whole", "title": "v"}},
    }
    guidance = SchemaGuidance.compile(schema, reference_registry=registry)
    applied = guidance.occurrences_at("/v", {"v": Decimal("1.0")})
    assert {(item.instance_pointer, item.schema_pointer) for item in applied} == {
        ("/v", "/properties/v"),
        ("/v", "/$defs/a~1~0"),
    }
    assert {item.schema.get("type") for item in applied} == {None, "integer"}
    doc = from_data({"v": Decimal("1.0")}, json_schema=schema, reference_registry=registry)
    assert isinstance(doc.value_at("/v", typed=True), int)
    target = next(item.schema for item in applied if item.schema_pointer == "/$defs/a~1~0")
    target["type"] = "string"
    assert guidance.validate({"v": Decimal("1.0")}).error_count == 0


def test_document_guidance_reuses_policy_authorized_resources_without_rereading(tmp_path):
    child = tmp_path / "child.json"
    child.write_text('{"$defs":{"n":{"type":"integer"}}}', encoding="utf-8")
    schema = {"$ref": "child.json#/$defs/n"}
    doc = from_json(
        "1.0",
        json_schema=schema,
        base_uri=str(tmp_path / "root.json"),
        resource_policy=RemoteResourcePolicy(),
    )
    child.write_text('{"$defs":{"n":{"type":"string"}}}', encoding="utf-8")
    assert doc.is_valid
    assert doc.value_at("", typed=True) == 1
    assert doc.to_xdm() == (XdmAtomic.integer(1),)


@pytest.mark.parametrize(
    ("keyword", "branches", "paths", "guided"),
    [
        ("allOf", [{"type": "integer"}, {"minimum": 0}], ["/allOf/0", "/allOf/1"], True),
        ("allOf", [{"type": "integer"}, {"type": "number"}], ["/allOf/0", "/allOf/1"], False),
        ("anyOf", [{"type": "integer"}, {"type": "integer"}], ["/anyOf/0", "/anyOf/1"], True),
        ("anyOf", [{"type": "integer"}, {"type": "number"}], ["/anyOf/0", "/anyOf/1"], False),
        ("anyOf", [{"type": "string"}, {"type": "integer"}], ["/anyOf/1"], True),
        ("anyOf", [{"type": "integer"}, {"type": "string"}], ["/anyOf/0"], True),
        ("oneOf", [{"type": "integer"}, {"type": "string"}], ["/oneOf/0"], True),
        ("oneOf", [{"type": "string"}, {"type": "integer"}], ["/oneOf/1"], True),
    ],
)
def test_only_successful_applicator_branches_contribute_and_conflicts_decline(
    keyword, branches, paths, guided
):
    schema = {keyword: branches}
    guidance = SchemaGuidance.compile(schema)
    assert [item.schema_pointer for item in guidance.occurrences_at("", Decimal("1.0"))] == [
        "",
        *paths,
    ]
    doc = from_json("1.0", json_schema=schema)
    assert doc.is_valid
    assert doc.profile().at("").interpretation == ("schema-guided" if guided else "native")
    assert doc.to_xdm()[0].type_name.endswith("integer" if guided else "decimal")


def test_anyof_with_unverifiable_alternative_does_not_change_validation_or_guess():
    schema = {"anyOf": [{"type": "integer"}, {"$ref": "https://example.test/missing"}]}
    doc = from_json("1.0", json_schema=schema)
    assert doc.is_valid
    assert doc.to_xdm() == (XdmAtomic.decimal(Decimal("1.0")),)
    assert doc.report.error_count == 0
    assert doc.profile().at("").interpretation == "native"


@pytest.mark.parametrize(
    ("kind", "selected", "xdm"),
    [
        ("a", "/then/properties/n", XdmAtomic.integer(1)),
        ("b", "/else/properties/n", XdmAtomic.decimal(1)),
    ],
)
def test_conditionals_use_the_validators_numeric_assertions_and_the_parent_value(
    kind, selected, xdm
):
    schema = {
        "if": {"properties": {"kind": {"const": "a"}, "n": {"multipleOf": 0.1}}},
        "then": {"properties": {"n": {"type": "integer"}}},
        "else": {"properties": {"n": {"type": "number"}}},
    }
    value = {"kind": kind, "n": Decimal("1.0")}
    assert [
        item.schema_pointer for item in SchemaGuidance.compile(schema).occurrences_at("/n", value)
    ] == [
        selected,
    ]
    assert from_data(value, json_schema=schema).to_xdm()[0].get(XdmAtomic.string("n")) == (xdm,)


@pytest.mark.parametrize("schema", [{"if": {"type": "integer"}, "else": False}, {"then": False}])
def test_condition_assertions_and_unselected_or_orphaned_branches_are_not_hints(schema):
    doc = from_json("1.0", json_schema=schema)
    assert doc.is_valid
    assert doc.to_xdm() == (XdmAtomic.decimal(Decimal("1.0")),)


def test_properties_and_patterns_both_apply_but_additional_properties_are_local():
    schema = {
        "properties": {"a": {"type": "integer"}},
        "patternProperties": {"^a$": {"type": "number"}, "^p": {"type": "integer"}},
        "additionalProperties": {"type": "integer"},
        "allOf": [{"properties": {"z": {"type": "number"}}}],
    }
    value = {key: Decimal("1.0") for key in ("a", "pattern", "extra", "z")}
    guidance = SchemaGuidance.compile(schema)
    assert {
        key: {item.schema_pointer for item in guidance.occurrences_at(f"/{key}", value)}
        for key in value
    } == {
        "a": {"/properties/a", "/patternProperties/^a$"},
        "pattern": {"/patternProperties/^p"},
        "extra": {"/additionalProperties"},
        "z": {"/allOf/0/properties/z", "/additionalProperties"},
    }
    doc = from_data(value, json_schema=schema)
    assert isinstance(doc.value_at("/a", typed=True), Decimal)
    assert isinstance(doc.value_at("/pattern", typed=True), int)
    assert isinstance(doc.value_at("/extra", typed=True), int)
    assert isinstance(doc.value_at("/z", typed=True), Decimal)


def test_prefix_items_do_not_receive_items_hints():
    schema = {"prefixItems": [True, {"type": "number"}], "items": {"type": "integer"}}
    value = [Decimal("1.0")] * 3
    guidance = SchemaGuidance.compile(schema)
    (first,) = guidance.occurrences_at("/0", value)
    assert first.schema is True
    assert first.schema_pointer == "/prefixItems/0"
    assert [item.schema_pointer for item in guidance.occurrences_at("/1", value)] == [
        "/prefixItems/1"
    ]
    assert [item.schema_pointer for item in guidance.occurrences_at("/2", value)] == ["/items"]
    doc = from_data(value, json_schema=schema)
    assert [doc.profile().at(f"/{i}").interpretation for i in range(3)] == [
        "native",
        "schema-guided",
        "schema-guided",
    ]
    assert doc.to_xdm()[0].get(3) == (XdmAtomic.integer(1),)


def test_boolean_reference_keeps_exact_target_path():
    guidance = SchemaGuidance.compile({"$defs": {"a/~": True}, "$ref": "#/$defs/a~1~0"})
    root, target = guidance.occurrences_at("", 1)
    assert root.schema_pointer == ""
    assert target.schema is True
    assert target.schema_pointer == "/$defs/a~1~0"


def test_reference_into_a_dynamic_anchor_does_not_claim_static_guidance():
    schema = {"$ref": "#n", "$defs": {"n": {"$dynamicAnchor": "n", "type": "integer"}}}
    doc = from_json("1.0", json_schema=schema)
    assert doc.is_valid
    assert doc.profile().at("").interpretation == "native"


def test_unresolved_reference_in_a_short_circuited_branch_is_not_retrieved(tmp_path):
    child = tmp_path / "child.json"
    child.write_text('{"type":"number"}', encoding="utf-8")
    schema = {"anyOf": [{"type": "integer"}, {"$ref": "child.json"}]}
    policy = RemoteResourcePolicy()
    doc = from_json(
        "1.0", json_schema=schema, base_uri=str(tmp_path / "root.json"), resource_policy=policy
    )
    child.write_text('{"type":"integer"}', encoding="utf-8")
    assert doc.is_valid
    assert doc.profile().at("").interpretation == "native"


def test_additional_properties_requires_the_evaluator_to_have_applied_it():
    # The evaluator joins pattern expressions for its additional-property check.
    # In that combined regex, backreference 2 becomes (a), not (b).
    schema = {
        "patternProperties": {"(x)": True, r"^(a)(b)\2$": True},
        "additionalProperties": {"type": "integer"},
    }
    doc = from_json('{"aba":1.0}', json_schema=schema)
    assert doc.is_valid
    assert doc.profile().at("/aba").interpretation == "native"
    assert isinstance(doc.value_at("/aba", typed=True), Decimal)


def test_recursive_static_reference_follows_each_instance_pointer_independently():
    schema = {
        "properties": {"n": {"type": "integer"}, "child": {"$ref": "#"}},
    }
    value = {"n": Decimal("1.0"), "child": {"n": Decimal("2.0")}}
    guidance = SchemaGuidance.compile(schema)
    assert [item.schema_pointer for item in guidance.occurrences_at("/child/n", value)] == [
        "/properties/n"
    ]
    assert isinstance(from_data(value, json_schema=schema).value_at("/child/n", typed=True), int)


def test_dependent_schemas_only_guide_when_the_trigger_property_is_present():
    schema = {"dependentSchemas": {"trigger": {"properties": {"n": {"type": "integer"}}}}}
    without = from_json('{"n":1.0}', json_schema=schema)
    with_trigger = from_json('{"trigger":null,"n":1.0}', json_schema=schema)
    assert without.profile().at("/n").interpretation == "native"
    assert with_trigger.profile().at("/n").interpretation == "schema-guided"


def test_scalar_hook_sees_only_finite_native_decimals():
    def converter(pointer, value):
        pytest.fail("A non-finite Decimal must not reach the converter")

    with pytest.raises(TypeError, match="finite"):
        json_to_xdm(Decimal("Infinity"), converter=converter)


def test_recursion_in_a_short_circuited_alternative_only_declines_guidance():
    doc = from_json("1.0", json_schema={"anyOf": [{"type": "integer"}, {"$ref": "#"}]})
    assert doc.is_valid
    assert doc.profile().at("").interpretation == "native"


def test_evaluator_error_in_a_short_circuited_alternative_only_declines_guidance():
    schema = {
        "anyOf": [
            {"properties": {"x": {"type": "integer"}}},
            {"patternProperties": {"(?i)a": True, "(?i)b": True}, "additionalProperties": True},
        ]
    }
    doc = from_json('{"x":1.0}', json_schema=schema)
    assert doc.is_valid
    assert doc.profile().at("/x").interpretation == "native"
    assert isinstance(doc.value_at("/x", typed=True), Decimal)


def test_anyof_can_compare_an_alternative_already_authorized_during_validation(tmp_path):
    (tmp_path / "child.json").write_text('{"type":"number"}', encoding="utf-8")
    schema = {
        "properties": {
            "a": {"$ref": "child.json"},
            "b": {"anyOf": [{"type": "integer"}, {"$ref": "child.json"}]},
        }
    }
    doc = from_json(
        '{"a":1.0,"b":2.0}',
        json_schema=schema,
        base_uri=str(tmp_path / "root.json"),
        resource_policy=RemoteResourcePolicy(),
    )
    assert doc.is_valid
    assert doc.profile().at("/a").interpretation == "schema-guided"
    assert doc.profile().at("/b").interpretation == "native"


def test_occurrences_require_a_valid_whole_instance_and_a_present_pointer():
    guidance = SchemaGuidance.compile({"required": ["a", "b"], "properties": {"a": True}})
    assert guidance.occurrences_at("/a", {"a": 1}) == ()
    with pytest.raises(ValueError, match="present member"):
        guidance.occurrences_at("/c", {"a": 1, "b": 2})
    with pytest.raises(ValueError, match="leading zero"):
        SchemaGuidance.compile(True).occurrences_at("/00", [1])
    assert SchemaGuidance.compile(True).occurrences_at("/0", [1]) == ()


@pytest.mark.parametrize(
    ("schema", "valid", "invalid", "pointer", "safe_pointer"),
    [
        (
            {
                "properties": {"known": {"type": "integer"}},
                "unevaluatedProperties": {"type": "integer"},
            },
            '{"known":1.0,"extra":1.0}',
            '{"known":1.0,"extra":"bad"}',
            "/extra",
            "/known",
        ),
        (
            {"prefixItems": [{"type": "integer"}], "unevaluatedItems": {"type": "integer"}},
            "[1.0,1.0]",
            '[1.0,"bad"]',
            "/1",
            "/0",
        ),
        (
            {
                "$defs": {"n": {"$dynamicAnchor": "n", "type": "integer"}},
                "properties": {"known": {"type": "integer"}, "extra": {"$dynamicRef": "#n"}},
            },
            '{"known":1.0,"extra":1.0}',
            '{"known":1.0,"extra":"bad"}',
            "/extra",
            "/known",
        ),
    ],
)
def test_dynamic_and_unevaluated_rules_validate_without_providing_hints(
    schema, valid, invalid, pointer, safe_pointer
):
    doc = from_json(valid, json_schema=schema)
    assert doc.is_valid
    assert doc.profile().at(pointer).interpretation == "native"
    assert isinstance(doc.value_at(pointer, typed=True), Decimal)
    assert isinstance(doc.value_at(safe_pointer, typed=True), int)
    assert not from_json(invalid, json_schema=schema).is_valid


@pytest.mark.parametrize("format_assertion", [False, True])
def test_applied_formats_add_profile_evidence_but_do_not_cast(format_assertion):
    schema = {
        "prefixItems": [{"type": "string", "format": "uri"}, {"format": "date"}],
        "items": {"contentEncoding": "base64", "format": "unmapped-extension"},
    }
    doc = from_json(
        '["https://example.test/","2026-11-22","YQ==","!"]',
        json_schema=schema,
        format_assertion=format_assertion,
    )
    profile = doc.profile()
    assert doc.is_valid
    assert profile.at("/0").candidate("xs:anyURI").passed == 1
    assert profile.at("/1").candidate("xs:date").from_schema_annotation
    assert profile.at("/2").candidate("xs:base64Binary").passed == 1
    failure = profile.at("", "array-items").candidate("xs:base64Binary")
    assert (failure.passed, failure.failed, failure.first_failure.pointer) == (1, 3, "/0")
    assert profile.at("/3").candidate("xs:base64Binary").first_failure.pointer == "/3"
    assert profile.at("/1").interpretation == "native"
    assert doc.to_xdm()[0].get(1) == (XdmAtomic.string("https://example.test/"),)
    assert doc.to_xdm()[0].get(2) == (XdmAtomic.string("2026-11-22"),)
    assert doc.to_xdm()[0].get(3) == (XdmAtomic.string("YQ=="),)


def test_annotation_hints_only_come_from_applied_branches_and_string_occurrences():
    schema = {
        "anyOf": [
            {"type": "string", "format": "date-time"},
            {"type": "string", "format": "time"},
            {"type": "integer", "format": "uri"},
        ],
        "contentEncoding": "unknown",
    }
    text = from_data("12:34:56", json_schema=schema)
    row = text.profile().at("")
    assert row.candidate("xs:time").from_schema_annotation
    assert row.candidate("xs:dateTime").from_schema_annotation
    assert "xs:anyURI" not in row.candidate_names
    assert text.to_xdm() == (XdmAtomic.string("12:34:56"),)

    numeric = from_data(1, json_schema={"type": "integer", "format": "uri"})
    assert "xs:anyURI" not in numeric.profile().at("").candidate_names
    invalid = from_data("not integer", json_schema={"type": "integer", "format": "uri"})
    assert "xs:anyURI" not in invalid.profile().at("").candidate_names


@pytest.mark.parametrize(
    ("first", "second", "expected_type", "xdm_type"),
    [
        ("int32", "int32", xsd.Int, xsd.Int),
        ("int32", "int64", Decimal, xsd.Decimal),
        ("int32", "unused-format", xsd.Int, xsd.Int),
    ],
)
def test_specific_formats_refine_primitive_guidance_only_when_compatible(
    first, second, expected_type, xdm_type
):
    doc = from_data(
        1.0,
        json_schema={"type": "number", "allOf": [True, {"format": first}, {"format": second}]},
        format_typing=True,
    )
    assert type(doc.value_at("", typed=True)) is expected_type
    atomic = doc.to_xdm()[0]
    assert isinstance(atomic, XdmAtomic) and atomic.datatype is xdm_type
    assert doc.profile().at("").interpretation == "schema-guided"
    assert not doc.pin_type("", "xs:double").has_errors
    pinned = doc.to_xdm()[0]
    assert isinstance(pinned, XdmAtomic) and pinned.datatype is xsd.Double
    assert doc.profile().at("").interpretation == "pinned"


def test_only_applied_accepted_specific_formats_take_part_in_resolution():
    schema = {
        "allOf": [
            {"anyOf": [{"format": "double", "maximum": 1}, {"format": "int64"}]},
            {"format": "float"},
        ]
    }
    # The double branch fails validation, and the int64 format overflows.
    doc = from_data(2**63, json_schema=schema, format_typing=True)
    assert doc.is_valid
    assert type(doc.value_at("", typed=True)) is xsd.Float
    # Both applicable formats accept 2, so neither can refine the native value.
    ambiguous = from_data(2.0, json_schema=schema, format_typing=True)
    assert ambiguous.is_valid
    assert type(ambiguous.value_at("", typed=True)) is float
    assert ambiguous.profile().at("").interpretation == "native"


def test_byte_and_content_encoding_coalesce_as_one_specific_type():
    doc = from_data(
        "YQ==",
        json_schema={"type": "string", "format": "byte", "contentEncoding": "base64"},
        format_typing=True,
    )
    assert type(doc.value_at("", typed=True)) is xsd.Base64Binary


def test_invalid_asserted_format_suppresses_guidance_for_the_whole_instance():
    schema = {
        "prefixItems": [{"type": "number", "format": "int32"}, {"type": "string", "format": "uuid"}]
    }
    uuid = "123E4567-E89B-12D3-A456-426614174000"
    native = [2**31, uuid]
    doc = from_data(native, json_schema=schema, format_typing=True)
    assert doc.is_valid
    assert type(doc.value_at("/0", typed=True)) is Decimal
    assert doc.profile().at("/1").interpretation == "schema-guided"

    strict = from_data(native, json_schema=schema, format_assertion=True, format_typing=True)
    assert not strict.is_valid
    assert type(strict.value_at("/0", typed=True)) is int
    assert type(strict.value_at("/1", typed=True)) is str
    array = strict.to_xdm()[0]
    assert isinstance(array, XdmArray) and array.get(2) == (XdmAtomic.string(uuid),)
    assert strict.profile().at("/1").interpretation == "native"


@pytest.mark.parametrize(
    ("format_name", "value", "candidate"),
    [
        ("date", "2024-02-29", "xs:date"),
        ("date-time", "2024-02-29T12:34:56Z", "xs:dateTime"),
        ("time", "12:34:56Z", "xs:time"),
    ],
)
def test_rfc3339_annotations_do_not_cast_even_with_format_typing(format_name, value, candidate):
    doc = from_data(
        value, json_schema={"format": format_name}, format_assertion=True, format_typing=True
    )
    assert doc.is_valid
    assert type(doc.value_at("", typed=True)) is str
    assert doc.to_xdm() == (XdmAtomic.string(value),)
    assert doc.profile().at("").candidate(candidate).from_schema_annotation
