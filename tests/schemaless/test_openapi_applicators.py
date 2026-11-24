"""Resolve OpenAPI XML annotations at validated JSON occurrences."""

import pytest
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from pyxsd.namespaces import XSI_NS
from pyxsd.schemaless import MappingRule, XmlMapping, XmlMappingError, from_data
from pyxsd.schemaless.errors import JsonSchemaError


# Keep the task brief's three literal fixtures as written.
# fmt: off
def test_prefixitems_none_mapping_controls_xml_order():
    schema = {
        "type": "object", "xml": {"name": "root"},
        "properties": {"ordered": {
            "type": "array", "xml": {"nodeType": "none"},
            "prefixItems": [
                {"type": "string", "xml": {"nodeType": "element", "name": "first"}},
                {"type": "integer", "xml": {"nodeType": "element", "name": "second"}},
            ],
        }},
    }
    mapping = XmlMapping.from_openapi(schema)
    xml = from_data({"ordered": ["a", 2]}).to_xml(mapping)
    assert [node._name_ for node in xml.root.children] == ["first", "second"]
    assert [node.text_value() for node in xml.root.children] == ["a", "2"]


def test_allof_xml_metadata_merges_only_when_compatible():
    schema = {"allOf": [
        {"type": "object", "xml": {"name": "r"}},
        {"type": "object", "properties": {"id": {
            "type": "string", "xml": {"nodeType": "attribute"}
        }}},
    ]}
    xml = from_data({"id": "x"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.root._name_ == "r" and xml.root.attribute_lexical("id") == "x"


def test_successful_anyof_branches_cannot_choose_first_xml_mapping():
    schema = {"anyOf": [
        {"type": "object", "properties": {"id": {
            "type": "string", "xml": {"nodeType": "attribute"}
        }}},
        {"type": "object", "properties": {"id": {
            "type": "string", "xml": {"nodeType": "element"}
        }}},
    ]}
    mapping = XmlMapping.from_openapi(schema, root_name="r")
    with pytest.raises(XmlMappingError, match="ambiguous"):
        from_data({"id": "x"}).to_xml(mapping)
# fmt: on


def test_ref_to_defs_under_ordered_array_uses_target_annotations():
    schema = {
        "xml": {"name": "root"},
        "$defs": {
            "a": {"type": "string", "xml": {"name": "first", "namespace": "urn:x"}},
            "b": {"type": "integer", "xml": {"nodeType": "element", "name": "second"}},
        },
        "properties": {
            "ordered": {
                "type": "array",
                "xml": {"nodeType": "none"},
                "prefixItems": [{"$ref": "#/$defs/a"}, {"$ref": "#/$defs/b"}],
                "items": {"type": "string", "xml": {"name": "last"}},
            }
        },
    }
    xml = from_data({"ordered": ["a", 2, "z"]}).to_xml(XmlMapping.from_openapi(schema))
    assert [node._name_ for node in xml.root.children] == ["{urn:x}first", "second", "last"]
    assert [node.text_value() for node in xml.root.children] == ["a", "2", "z"]
    assert not xml.report.issues


def test_oneof_uses_assertions_not_the_discriminator_mapping():
    schema = {
        "xml": {"name": "r"},
        "discriminator": {"propertyName": "kind", "mapping": {"b": "#/$defs/A"}},
        "$defs": {
            "A": {"properties": {"kind": {"const": "a"}, "value": {"xml": {"name": "alpha"}}}},
            "B": {"properties": {"kind": {"const": "b"}, "value": {"xml": {"name": "beta"}}}},
        },
        "oneOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}],
    }
    mapping = XmlMapping.from_openapi(schema)
    assert from_data({"kind": "b", "value": "v"}).to_xml(mapping).to_string() == (
        "<r><kind>b</kind><beta>v</beta></r>"
    )
    with pytest.raises(XmlMappingError, match="oneOf"):
        from_data({"kind": "c", "value": "v"}).to_xml(mapping)
    with pytest.raises(XmlMappingError, match="oneOf"):
        from_data({"value": "v"}).to_xml(mapping)


@pytest.mark.parametrize(
    "uri",
    [
        "https://spec.openapis.org/oas/3.1/dialect/base",
        "https://spec.openapis.org/oas/3.1/dialect/2024-10-25",
        "https://spec.openapis.org/oas/3.1/dialect/2024-11-10",
        "https://spec.openapis.org/oas/3.2/dialect/2025-09-17",
        "https://spec.openapis.org/oas/3.2/dialect/2026-02-26",
    ],
)
def test_openapi_standard_dialects_use_draft_2020_12_assertions_and_xml_annotations(uri):
    schema = {
        "$schema": uri + "#",
        "xml": {"name": "r"},
        "properties": {"v": {"type": "integer", "xml": {"nodeType": "attribute"}}},
    }
    for argument in (None, uri):
        mapping = XmlMapping.from_openapi(schema, dialect=argument)
        assert from_data({"v": 7}).to_xml(mapping).to_string() == '<r v="7"/>'
        with pytest.raises(XmlMappingError, match="type"):
            from_data({"v": "x"}).to_xml(mapping)
    with pytest.raises(JsonSchemaError, match="Unsupported JSON Schema dialect"):
        XmlMapping.from_json_schema(schema, root_name="r")


def test_openapi_custom_dialect_is_not_silently_treated_as_the_base():
    uri = "https://example.test/custom"
    with pytest.raises(XmlMappingError, match="dialect") as caught:
        XmlMapping.from_openapi({}, root_name="r", dialect=uri)
    assert caught.value.report.issues[0].code == "unsupported-openapi-dialect"
    with pytest.raises(JsonSchemaError, match="Unsupported JSON Schema dialect"):
        XmlMapping.from_openapi({"$schema": uri}, root_name="r")


def test_explicit_draft_2020_12_preserves_openapi_annotations():
    schema = {"xml": {"name": "r"}}
    mapping = XmlMapping.from_openapi(
        schema, dialect="https://json-schema.org/draft/2020-12/schema"
    )
    assert from_data("x").to_xml(mapping).to_string() == "<r>x</r>"


@pytest.mark.parametrize(
    ("left", "right", "field"),
    [
        ({"nodeType": "element"}, {"nodeType": "text"}, "kind"),
        ({"name": "one"}, {"name": "two"}, "name"),
        ({"namespace": "urn:one"}, {"namespace": "urn:two"}, "namespace"),
        (
            {"namespace": "urn:one", "prefix": "p"},
            {"namespace": "urn:one", "prefix": "q"},
            "prefix",
        ),
        ({"name": "{urn:one}v"}, {"namespace": "urn:two"}, "namespace"),
    ],
)
def test_conflicting_array_metadata_reports_the_instance_and_both_schema_locations(
    left, right, field
):
    schema = {
        "xml": {"name": "r"},
        "properties": {
            "v": {
                "type": "array",
                "allOf": [{"xml": left}, {"xml": right}],
            }
        },
    }
    with pytest.raises(XmlMappingError, match="ambiguous") as caught:
        from_data({"v": ["a"]}).to_xml(XmlMapping.from_openapi(schema))
    (issue,) = caught.value.report.issues
    assert issue.code == "ambiguous-openapi-mapping"
    assert issue.location.pointer == "/v"
    assert issue.details["field"] == field
    assert issue.details["schema_pointers"] == ("/properties/v/allOf/0", "/properties/v/allOf/1")


def test_compatible_annotation_fields_combine_across_pattern_and_property():
    schema = {
        "xml": {"name": "r"},
        "properties": {"id": {"xml": {"nodeType": "attribute", "prefix": "p"}}},
        "patternProperties": {
            "^i": {"xml": {"name": "{urn:x}code"}},
            "d$": {"xml": {"name": "code", "namespace": "urn:x"}},
            "^z": {"xml": {"nodeType": "text"}},
        },
    }
    xml = from_data({"id": "a"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == '<r xmlns:p="urn:x" p:code="a"/>'
    assert not xml.report.issues


def test_caller_rule_masks_conflicting_kind_and_name_but_keeps_other_metadata():
    schema = {
        "xml": {"name": "r"},
        "properties": {
            "id": {
                "anyOf": [
                    {"xml": {"nodeType": "attribute", "name": "a", "namespace": "urn:x"}},
                    {"xml": {"nodeType": "element", "name": "b", "prefix": "p"}},
                ]
            }
        },
    }
    mapping = XmlMapping.from_openapi(
        schema, rules={"/id": MappingRule(kind="attribute", name="override")}
    )
    assert from_data({"id": "x"}).to_xml(mapping).to_string() == (
        '<r xmlns:p="urn:x" p:override="x"/>'
    )


def test_required_null_is_present_and_cdata_is_xdm_text():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "allOf": [
            {
                "required": ["maybe", "body"],
                "properties": {
                    "maybe": {"type": ["string", "null"], "xml": {"name": "nullable"}},
                    "body": {"type": "string", "xml": {"nodeType": "cdata"}},
                },
            },
            {"properties": {"body": {"xml": {"nodeType": "text"}}}},
        ],
    }
    mapping = XmlMapping.from_openapi(schema)
    xml = from_data({"maybe": None, "body": "<x/>"}).to_xml(mapping)
    assert xml.root.children[0]._name_ == "nullable"
    assert xml.root.children[0].attribute_lexical(f"{{{XSI_NS}}}nil") == "true"
    child, text = xml.to_xdm().root_element.children
    assert (child.kind, text.kind, text.string_value) == ("element", "text", "<x/>")
    assert "&lt;x/&gt;" in xml.to_string()
    with pytest.raises(XmlMappingError, match="required"):
        from_data({"body": "<x/>"}).to_xml(mapping)


def test_conflicting_referenced_metadata_identifies_each_resource():
    left = "https://example.test/a"
    right = "https://example.test/b"
    registry = Registry().with_resources(
        [
            (left, DRAFT202012.create_resource({"xml": {"name": "a"}})),
            (right, DRAFT202012.create_resource({"xml": {"name": "b"}})),
        ]
    )
    schema = {"properties": {"id": {"allOf": [{"$ref": left}, {"$ref": right}]}}}
    mapping = XmlMapping.from_openapi(schema, root_name="r", reference_registry=registry)
    with pytest.raises(XmlMappingError, match="ambiguous") as caught:
        from_data({"id": "v"}).to_xml(mapping)
    (issue,) = caught.value.report.issues
    assert issue.location.pointer == "/id"
    assert issue.details["schema_pointers"] == ("", "")
    assert issue.details["schema_sources"] == (left, right)


def test_root_name_is_selected_per_instance_and_never_guessed():
    schema = {
        "oneOf": [
            {"type": "integer", "xml": {"name": "number", "namespace": "urn:x", "prefix": "p"}},
            {"type": "string"},
        ],
    }
    mapping = XmlMapping.from_openapi(schema)
    assert mapping.root_name is None
    assert from_data(7).to_xml(mapping).to_string() == '<p:number xmlns:p="urn:x">7</p:number>'
    with pytest.raises(XmlMappingError, match=r"root_name or xml\.name") as caught:
        from_data("v").to_xml(mapping)
    assert caught.value.report.issues[0].location.pointer == ""


def test_root_rule_supplies_a_name_and_overrides_schema_namespace():
    schema = {"xml": {"namespace": "urn:old", "prefix": "p"}}
    mapping = XmlMapping.from_openapi(
        schema, rules={"": MappingRule(name="new", namespace="urn:new")}
    )
    assert from_data("x").to_xml(mapping).to_string() == '<p:new xmlns:p="urn:new">x</p:new>'


def test_dynamic_reference_uses_the_evaluators_dynamic_scope():
    base = "https://example.test/base"
    outer = {
        "$id": "https://example.test/outer",
        "$defs": {"override": {"$dynamicAnchor": "field", "xml": {"name": "overridden"}}},
        "$ref": base,
        "xml": {"name": "r"},
    }
    target = {
        "$id": base,
        "$defs": {"default": {"$dynamicAnchor": "field", "xml": {"name": "default"}}},
        "properties": {"value": {"$dynamicRef": "#field"}},
    }
    registry = Registry().with_resource(base, DRAFT202012.create_resource(target))
    mapping = XmlMapping.from_openapi(outer, reference_registry=registry)
    xml = from_data({"value": "x"}).to_xml(mapping)
    assert xml.to_string() == "<r><overridden>x</overridden></r>"
    assert not xml.report.issues


def test_short_circuited_alternative_cannot_be_retrieved_just_for_xml(tmp_path):
    child = tmp_path / "not-authorized.json"
    child.write_text('{"xml":{"name":"remote"}}', encoding="utf-8")
    schema = {
        "xml": {"name": "r"},
        "properties": {
            "v": {"anyOf": [{"xml": {"name": "first"}}, {"$ref": child.as_uri()}]},
        },
    }
    xml = from_data({"v": "x"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><v>x</v></r>"
    (issue,) = xml.report.issues
    assert issue.code == "openapi-metadata-unresolved"
    assert issue.location.pointer == "/v"
    assert issue.details["schema_pointers"] == ("/properties/v",)


def test_unevaluated_schema_metadata_is_reported_only_for_possible_child_applications():
    schema = {
        "xml": {"name": "r"},
        "properties": {"known": True},
        "unevaluatedProperties": {"xml": {"name": "unresolved"}},
    }
    mapping = XmlMapping.from_openapi(schema)
    known = from_data({"known": "x"}).to_xml(mapping)
    assert known.to_string() == "<r><known>x</known></r>"
    assert not known.report.issues
    unknown = from_data({"extra": "x"}).to_xml(mapping)
    assert unknown.to_string() == "<r><extra>x</extra></r>"
    (issue,) = unknown.report.issues
    assert issue.code == "openapi-metadata-unresolved"
    assert issue.location.pointer == "/extra"
    assert issue.details["schema_pointers"] == ("/unevaluatedProperties",)


def test_ordered_array_cannot_merge_adjacent_text_and_cdata():
    schema = {
        "xml": {"name": "r"},
        "properties": {
            "v": {
                "type": "array",
                "xml": {"nodeType": "none"},
                "prefixItems": [
                    {"xml": {"nodeType": "text"}},
                    {"xml": {"nodeType": "cdata"}},
                ],
            }
        },
    }
    with pytest.raises(XmlMappingError, match="adjacent text") as caught:
        from_data({"v": ["a", "b"]}).to_xml(XmlMapping.from_openapi(schema))
    assert caught.value.report.issues[0].location.pointer == "/v/1"


def test_unevaluated_items_with_no_xml_metadata_do_not_warn():
    schema = {"xml": {"name": "r"}, "prefixItems": [True], "unevaluatedItems": True}
    mapping = XmlMapping.from_openapi(schema, rules={"": MappingRule(item_name="item")})
    xml = from_data(["a", "b"]).to_xml(mapping)
    assert xml.to_string() == "<r><item>a</item><item>b</item></r>"
    assert not xml.report.issues


@pytest.mark.parametrize(
    ("schema", "data", "root_rules"),
    [
        (
            {"allOf": [{"properties": {"covered": True}}]},
            {"covered": "a", "extra": "b"},
            None,
        ),
        (
            {"allOf": [{"prefixItems": [True]}]},
            ["a", "b"],
            {"": MappingRule(item_name="item")},
        ),
    ],
)
def test_unevaluated_metadata_does_not_warn_for_members_evaluated_by_applicators(
    schema, data, root_rules
):
    object_data = isinstance(data, dict)
    keyword = "unevaluatedProperties" if object_data else "unevaluatedItems"
    schema = {"xml": {"name": "r"}, keyword: {"xml": {"name": "unknown"}}, **schema}
    xml = from_data(data).to_xml(XmlMapping.from_openapi(schema, rules=root_rules))
    expected = (
        "<r><covered>a</covered><extra>b</extra></r>"
        if object_data
        else ("<r><item>a</item><item>b</item></r>")
    )
    assert xml.to_string() == expected
    assert [(issue.code, issue.location.pointer) for issue in xml.report.issues] == [
        ("openapi-metadata-unresolved", "/extra" if object_data else "/1")
    ]


def test_referenced_root_attribute_is_rejected_at_the_instance_root():
    schema = {
        "$defs": {"field": {"xml": {"nodeType": "attribute", "name": "attr"}}},
        "$ref": "#/$defs/field",
    }
    with pytest.raises(XmlMappingError, match="root cannot be an attribute") as caught:
        from_data("x").to_xml(XmlMapping.from_openapi(schema))
    (issue,) = caught.value.report.issues
    assert (issue.location.pointer, issue.location.schema_pointer) == ("", "/$defs/field/xml")


def test_deferred_root_name_does_not_allow_an_explicit_non_element_root():
    schema = {"allOf": [{"xml": {"name": "r"}}]}
    mapping = XmlMapping.from_openapi(schema, rules={"": MappingRule(kind="none")})
    with pytest.raises(XmlMappingError, match="root must be a single element"):
        from_data("v").to_xml(mapping)


def test_uninspectable_root_alternative_keeps_direct_root_qname():
    schema = {
        "xml": {"name": "root", "namespace": "urn:x", "prefix": "p"},
        "anyOf": [True, {"$ref": "https://example.test/not-authorized"}],
    }
    xml = from_data("v").to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == '<p:root xmlns:p="urn:x">v</p:root>'
    (issue,) = xml.report.issues
    assert (issue.code, issue.location.pointer) == ("openapi-metadata-unresolved", "")


def test_local_caller_name_keeps_namespace_from_a_clark_annotation():
    schema = {
        "xml": {"name": "r"},
        "properties": {"v": {"xml": {"name": "{urn:x}old", "prefix": "p"}}},
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/v": MappingRule(name="new")})
    assert from_data({"v": "x"}).to_xml(mapping).to_string() == (
        '<r><p:new xmlns:p="urn:x">x</p:new></r>'
    )


@pytest.mark.parametrize("metadata", [{}, {"xml": {"nodeType": "none"}}])
def test_caller_can_wrap_an_openapi_inline_array(metadata):
    schema = {
        "xml": {"name": "r"},
        "properties": {"v": {"type": "array", "items": {"xml": {"name": "entry"}}, **metadata}},
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/v": MappingRule("list", array="wrapped")})
    assert (
        from_data({"v": ["x"]}).to_xml(mapping).to_string()
        == "<r><list><entry>x</entry></list></r>"
    )


def test_caller_wrapping_policy_resolves_a_conflicting_array_kind():
    schema = {
        "xml": {"name": "r"},
        "properties": {
            "v": {
                "type": "array",
                "allOf": [{"xml": {"nodeType": "none"}}, {"xml": {"nodeType": "element"}}],
                "items": {"xml": {"name": "item"}},
            }
        },
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/v": MappingRule(array="wrapped")})
    assert from_data({"v": ["x"]}).to_xml(mapping).to_string() == "<r><v><item>x</item></v></r>"


def test_malformed_xml_on_a_registered_ref_reports_both_locations():
    uri = "https://example.test/schema"
    registry = Registry().with_resource(uri, DRAFT202012.create_resource({"xml": {"name": 1}}))
    schema = {"xml": {"name": "r"}, "properties": {"v": {"$ref": uri}}}
    mapping = XmlMapping.from_openapi(schema, reference_registry=registry)
    with pytest.raises(XmlMappingError, match="XML name must be a string") as caught:
        from_data({"v": "x"}).to_xml(mapping)
    (issue,) = caught.value.report.issues
    assert (issue.location.pointer, issue.location.schema_pointer) == ("/v", "/xml/name")
    assert issue.details["schema_source"] == uri


def test_applied_dialect_is_not_reused_from_an_existing_plain_json_schema_evaluation():
    schema = {
        "$defs": {"target": {"$dynamicAnchor": "target", "xml": {"name": "changed"}}},
        "properties": {"v": {"$dynamicRef": "#target"}},
    }
    doc = from_data({"v": "x"}, json_schema=schema)
    assert doc.to_xml(XmlMapping.from_openapi(schema, root_name="r")).to_string() == (
        "<r><changed>x</changed></r>"
    )


def test_null_array_and_scalar_shapes_cannot_be_settled_by_the_inline_default():
    schema = {
        "xml": {"name": "r"},
        "required": ["v"],
        "properties": {"v": {"anyOf": [{"type": ["array", "null"]}, {"type": ["string", "null"]}]}},
    }
    with pytest.raises(XmlMappingError, match="ambiguous") as caught:
        from_data({"v": None}).to_xml(XmlMapping.from_openapi(schema))
    (issue,) = caught.value.report.issues
    assert issue.location.pointer == "/v"
    assert issue.details["schema_pointers"] == ("/properties/v/anyOf/0", "/properties/v/anyOf/1")


def test_an_inferred_array_item_name_is_not_allowed_to_turn_a_data_key_into_a_clark_name():
    key = "{urn:x}data"
    schema = {"xml": {"name": "r"}, "properties": {key: {"type": "array", "items": True}}}
    mapping = XmlMapping.from_openapi(schema)
    with pytest.raises(XmlMappingError, match="valid local XML name") as caught:
        from_data({key: ["a"]}).to_xml(mapping)
    assert caught.value.report.issues[0].location.pointer == "/{urn:x}data/0"


def test_contains_contributes_xml_metadata_only_to_matching_array_members():
    schema = {
        "xml": {"name": "r"},
        "properties": {
            "v": {
                "type": "array",
                "contains": {"type": "integer", "xml": {"name": "matched"}},
                "items": True,
            }
        },
    }
    xml = from_data({"v": ["a", 2, "b"]}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><v>a</v><matched>2</matched><v>b</v></r>"
    assert not xml.report.issues


def test_caller_clark_item_name_supplies_both_fields_before_metadata_conflict_checks():
    schema = {
        "xml": {"name": "r"},
        "properties": {
            "v": {
                "type": "array",
                "items": {
                    "allOf": [
                        {"xml": {"namespace": "urn:a", "prefix": "p"}},
                        {"xml": {"namespace": "urn:b", "name": "b"}},
                    ]
                },
            }
        },
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/v": MappingRule(item_name="{urn:x}row")})
    assert from_data({"v": ["x"]}).to_xml(mapping).to_string() == (
        '<r><p:row xmlns:p="urn:x">x</p:row></r>'
    )


def test_explicit_legacy_attribute_flags_must_not_disagree():
    schema = {
        "xml": {"name": "r"},
        "properties": {
            "v": {"allOf": [{"xml": {"attribute": True}}, {"xml": {"attribute": False}}]}
        },
    }
    with pytest.raises(XmlMappingError, match="ambiguous") as caught:
        from_data({"v": "x"}).to_xml(XmlMapping.from_openapi(schema))
    (issue,) = caught.value.report.issues
    assert issue.details["field"] == "kind"
    assert issue.details["values"] == ("attribute", "element")


def test_legacy_attribute_false_does_not_wrap_an_array():
    schema = {
        "xml": {"name": "r"},
        "properties": {"v": {"type": "array", "xml": {"attribute": False}, "items": True}},
    }
    xml = from_data({"v": ["x"]}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><v>x</v></r>"


def test_property_oneof_ref_preserves_the_property_element():
    schema = {
        "xml": {"name": "pet"},
        "properties": {"animal": {"oneOf": [{"$ref": "#/$defs/Cat"}]}},
        "$defs": {
            "Cat": {
                "type": "object",
                "properties": {"kind": {"const": "cat"}, "meow": {"type": "string"}},
            }
        },
    }
    xml = from_data({"animal": {"kind": "cat", "meow": "m"}}).to_xml(
        XmlMapping.from_openapi(schema)
    )
    assert xml.to_string() == "<pet><animal><kind>cat</kind><meow>m</meow></animal></pet>"
    assert not xml.report.issues


def test_property_anyof_selects_a_ref_without_flattening_the_property():
    schema = {
        "xml": {"name": "pet"},
        "properties": {"animal": {"anyOf": [{"$ref": "#/$defs/Dog"}, {"$ref": "#/$defs/Cat"}]}},
        "$defs": {
            "Dog": {
                "properties": {
                    "kind": {"const": "dog"},
                    "sound": {"xml": {"name": "bark"}},
                },
                "required": ["kind"],
            },
            "Cat": {
                "properties": {
                    "kind": {"const": "cat"},
                    "sound": {"xml": {"name": "meow"}},
                },
                "required": ["kind"],
            },
        },
    }
    xml = from_data({"animal": {"kind": "cat", "sound": "m"}}).to_xml(
        XmlMapping.from_openapi(schema)
    )
    assert xml.to_string() == "<pet><animal><kind>cat</kind><meow>m</meow></animal></pet>"
    assert not xml.report.issues


def test_property_allof_ref_preserves_the_extending_object_element():
    schema = {
        "xml": {"name": "r"},
        "$defs": {"base": {"type": "object", "properties": {"v": {"type": "integer"}}}},
        "properties": {
            "child": {
                "type": "object",
                "allOf": [{"$ref": "#/$defs/base"}],
                "properties": {"w": {"type": "integer"}},
            }
        },
    }
    xml = from_data({"child": {"v": 1, "w": 2}}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><child><v>1</v><w>2</w></child></r>"
    assert not xml.report.issues


def test_array_ref_under_allof_preserves_the_property_wrapper():
    schema = {
        "xml": {"name": "r"},
        "$defs": {"rows": {"type": "array", "items": {"xml": {"name": "row"}}}},
        "properties": {"wrapper": {"allOf": [{"$ref": "#/$defs/rows"}]}},
    }
    xml = from_data({"wrapper": [1, 2]}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><wrapper><row>1</row><row>2</row></wrapper></r>"
    assert not xml.report.issues


def test_direct_ref_and_array_defaults_still_inline_members():
    schema = {
        "xml": {"name": "r"},
        "$defs": {"base": {"type": "object", "properties": {"v": {"type": "integer"}}}},
        "properties": {
            "child": {"$ref": "#/$defs/base"},
            "rows": {"type": "array", "items": {"xml": {"name": "row"}}},
        },
    }
    xml = from_data({"child": {"v": 1}, "rows": [2]}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><v>1</v><row>2</row></r>"
    assert not xml.report.issues


def test_conflicting_direct_defaults_fail_unless_overridden():
    schema = {
        "xml": {"name": "r"},
        "$defs": {"base": {"type": "object", "properties": {"v": {"type": "integer"}}}},
        "properties": {"child": {"$ref": "#/$defs/base"}},
        "patternProperties": {"^child$": {"type": "object"}},
    }
    doc = from_data({"child": {"v": 1}})
    with pytest.raises(XmlMappingError, match="ambiguous") as caught:
        doc.to_xml(XmlMapping.from_openapi(schema))
    (issue,) = caught.value.report.issues
    assert issue.location.pointer == "/child"
    assert issue.details["field"] == "kind"
    assert issue.details["schema_pointers"] == ("/properties/child", "/patternProperties/^child$")

    mapping = XmlMapping.from_openapi(schema, rules={"/child": MappingRule(kind="element")})
    assert doc.to_xml(mapping).to_string() == "<r><child><v>1</v></child></r>"
    schema["patternProperties"]["^child$"]["xml"] = {"nodeType": "none"}
    assert doc.to_xml(XmlMapping.from_openapi(schema)).to_string() == "<r><v>1</v></r>"
