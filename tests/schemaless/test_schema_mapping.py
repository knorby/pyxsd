"""Project native JSON with occurrence-sensitive JSON Schema guidance."""

from io import StringIO
from pathlib import Path
from sys import get_int_max_str_digits

import pytest
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from pyxsd.locations import DataLocation
from pyxsd.namespaces import XSI_NS
from pyxsd.remote import RemoteResourcePolicy
from pyxsd.schemaless import MappingRule, XmlMapping, from_data, from_json
from pyxsd.schemaless.errors import JsonSchemaError, XmlMappingError


# fmt: off
def test_schema_properties_and_items_guide_default_projection():
    schema = {
        "type": "object",
        "properties": {"lines": {"type": "array", "items": {"type": "integer"}}},
        "required": ["lines"],
    }
    doc = from_json('{"lines":[1,2]}', json_schema=schema)
    xml = doc.to_xml(XmlMapping.from_json_schema(schema, root_name="order"))
    assert [n.text_value() for n in xml.root.children[0].children] == ["1", "2"]


def test_anyof_with_identical_structure_shares_projection_guidance():
    schema = {"anyOf": [
        {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        {"type": "object", "properties": {"id": {"type": "number"}}, "required": ["id"]},
    ]}
    doc = from_json('{"id":1}', json_schema=schema)
    xml = doc.to_xml(XmlMapping.from_json_schema(schema, root_name="r"))
    assert xml.root.children[0]._name_ == "id"
    assert xml.root.children[0].text_value() == "1"
# fmt: on


def test_reference_guidance_combines_with_allof_and_preserves_native_order_and_spelling():
    schema = {
        "$defs": {"rows": {"type": "array", "items": {"type": "integer"}}},
        "allOf": [
            {"properties": {"id": {"type": "string"}}},
            {"properties": {"rows": {"$ref": "#/$defs/rows"}}},
            {"properties": {"rows": {"type": ["array", "null"]}}},
        ],
    }
    doc = from_json('{"rows":[1.0,2],"id":"ok","extra":true}')
    xml = doc.to_xml(XmlMapping.from_json_schema(schema, root_name="r"))
    assert [node._name_ for node in xml.root.children] == ["rows", "id", "extra"]
    assert [node._name_ for node in xml.root.children[0].children] == ["item", "item"]
    assert [node.text_value() for node in xml.root.children[0].children] == ["1.0", "2"]
    assert xml.root.children[1].text_value() == "ok"
    assert xml.root.children[2].text_value() == "true"


@pytest.mark.parametrize("keyword", ["anyOf", "allOf"])
def test_conflicting_nullable_shapes_report_both_schema_locations(keyword):
    schema = {
        "$defs": {"list": {"type": ["array", "null"]}},
        "properties": {
            "value": {keyword: [{"$ref": "#/$defs/list"}, {"type": ["object", "null"]}]}
        },
    }
    doc = from_json('{"value":null}', json_schema=schema)
    assert doc.is_valid
    mapping = XmlMapping.from_json_schema(schema, root_name="r")
    with pytest.raises(XmlMappingError, match="ambiguous") as caught:
        doc.to_xml(mapping)
    (issue,) = caught.value.report.issues
    assert issue.location.pointer == "/value"
    assert issue.details["xml_path"] == "/r[1]/value[1]"
    assert issue.details["schema_pointers"] == ("/$defs/list", f"/properties/value/{keyword}/1")
    assert issue.details["schema_shapes"] == (("array",), ("object",))


def test_conflicting_ref_targets_distinguish_equal_pointers_in_distinct_resources():
    left_uri = "https://example.test/list.json"
    right_uri = "https://example.test/object.json"
    registry = Registry().with_resources(
        [
            (left_uri, DRAFT202012.create_resource({"type": ["array", "null"]})),
            (right_uri, DRAFT202012.create_resource({"type": ["object", "null"]})),
        ]
    )
    mapping = XmlMapping.from_json_schema(
        {"anyOf": [{"$ref": left_uri}, {"$ref": right_uri}]},
        root_name="r",
        reference_registry=registry,
    )
    with pytest.raises(XmlMappingError, match="ambiguous") as caught:
        from_json("null").to_xml(mapping)
    (issue,) = caught.value.report.issues
    assert issue.details["schema_pointers"] == ("", "")
    assert issue.details["schema_sources"] == (left_uri, right_uri)


def test_explicit_pointer_rule_wins_even_over_conflicting_guidance():
    schema = {
        "properties": {
            "value": {"anyOf": [{"type": ["array", "null"]}, {"type": ["object", "null"]}]}
        }
    }
    mapping = XmlMapping.from_json_schema(
        schema,
        root_name="r",
        rules={"/value": MappingRule(kind="attribute", name="out", null="empty")},
    )
    xml = from_json('{"value":null}').to_xml(mapping)
    assert not xml.root.children
    assert xml.root.attribute_lexical("out") == ""


def test_root_occurrence_can_be_ambiguous_or_overridden():
    schema = {"anyOf": [{"type": ["array", "null"]}, {"type": ["object", "null"]}]}
    doc = from_json("null")
    with pytest.raises(XmlMappingError, match="ambiguous") as caught:
        doc.to_xml(XmlMapping.from_json_schema(schema, root_name="r"))
    assert caught.value.report.issues[0].details["xml_path"] == "/r[1]"
    xml = doc.to_xml(
        XmlMapping.from_json_schema(schema, root_name="r", rules={"": MappingRule("v")})
    )
    assert xml.root._name_ == "v"
    assert xml.root.attribute_lexical(f"{{{XSI_NS}}}nil") == "true"


def test_overlapping_nullable_shapes_and_nonstructural_hints_are_compatible():
    schema = {
        "allOf": [
            True,
            {"type": "null"},
            {"type": ["array", "object", "null"]},
            {"type": ["array", "number", "null"]},
        ]
    }
    xml = from_json("null").to_xml(XmlMapping.from_json_schema(schema, root_name="r"))
    assert xml.root.attribute_lexical(f"{{{XSI_NS}}}nil") == "true"


@pytest.mark.parametrize("applicator", ["oneOf", "conditional"])
def test_only_the_instance_selected_branch_contributes_shapes(applicator):
    test = {"properties": {"choose": {"const": "list"}}, "required": ["choose"]}
    list_branch = {"properties": {"value": {"type": ["array", "null"]}}}
    object_branch = {"properties": {"value": {"type": ["object", "null"]}}}
    schema = {"properties": {"value": {"type": ["array", "null"]}}}
    if applicator == "conditional":
        schema.update({"if": test, "then": list_branch, "else": object_branch})
    else:
        schema["oneOf"] = [
            {"allOf": [test, list_branch]},
            {"allOf": [{"not": test}, object_branch]},
        ]
    mapping = XmlMapping.from_json_schema(schema, root_name="r")
    doc = from_json('{"choose":"list","value":null}')
    xml = doc.to_xml(mapping)
    assert xml.root.children[1].attribute_lexical(f"{{{XSI_NS}}}nil") == "true"
    with pytest.raises(XmlMappingError, match="ambiguous"):
        from_json('{"choose":"object","value":null}').to_xml(mapping)


def test_prefix_items_keep_tuple_order_and_do_not_receive_tail_items_hints():
    schema = {
        "type": "array",
        "prefixItems": [{"type": ["object", "null"]}, {"type": "string"}],
        "items": {"type": ["array", "null"]},
        "allOf": [{"prefixItems": [{"type": ["object", "null"]}]}],
    }
    mapping = XmlMapping.from_json_schema(schema, root_name="tuple")
    xml = from_json('[null,"second",[1]]').to_xml(mapping)
    assert xml.root.children[0].attribute_lexical(f"{{{XSI_NS}}}nil") == "true"
    assert xml.root.children[1].text_value() == "second"
    assert xml.root.children[2].children[0].text_value() == "1"


def test_required_nullable_property_is_present_but_optional_property_is_not_invented():
    schema = {
        "type": "object",
        "required": ["present"],
        "properties": {"present": {"type": "null"}, "absent": {"default": 1}},
    }
    xml = from_json('{"present":null}').to_xml(XmlMapping.from_json_schema(schema, root_name="r"))
    (node,) = xml.root.children
    assert node._name_ == "present"
    assert node.attribute_lexical(f"{{{XSI_NS}}}nil") == "true"
    with pytest.raises(XmlMappingError, match="JSON Schema 'required'"):
        from_json("{}").to_xml(XmlMapping.from_json_schema(schema, root_name="r"))


@pytest.mark.parametrize("prevalidated", [False, True])
def test_invalid_instance_reports_schema_failure_only_for_schema_dependent_mapping(prevalidated):
    schema = {"properties": {"value": {"type": "integer"}}}
    source = StringIO('{"value":"bad"}')
    source.name = "source.json"
    doc = from_json(source, json_schema=schema if prevalidated else None)
    before = doc.report
    assert doc.to_xml(XmlMapping.elements("r")).root.children[0].text_value() == "bad"
    mapping = XmlMapping.from_json_schema(
        schema, root_name="r", rules={"/value": MappingRule("out")}
    )
    with pytest.raises(XmlMappingError, match="JSON Schema 'type'") as caught:
        doc.to_xml(mapping)
    (issue,) = caught.value.report.issues
    assert (
        issue.code,
        issue.location.source,
        issue.location.pointer,
        issue.location.schema_pointer,
    ) == (
        "json-schema-invalid",
        "source.json",
        "/value",
        "/properties/value/type",
    )
    assert doc.report is before
    assert doc.is_valid is not prevalidated


def test_valid_evaluation_is_reused_with_its_authorized_reference_snapshot(tmp_path):
    child = tmp_path / "child.json"
    child.write_text('{"type":"integer"}', encoding="utf-8")
    schema = {"$ref": "child.json"}
    base_uri = str(tmp_path / "root.json")
    policy = RemoteResourcePolicy()
    doc = from_json("1", json_schema=schema, base_uri=base_uri, resource_policy=policy)
    mapping = XmlMapping.from_json_schema(
        '{"$ref":"child.json"}', root_name="r", base_uri=base_uri, resource_policy=policy
    )
    child.write_text('{"type":"string"}', encoding="utf-8")
    assert doc.to_xml(mapping).root.text_value() == "1"
    with pytest.raises(XmlMappingError, match="JSON Schema 'type'"):
        from_json("1").to_xml(mapping)


def test_applied_schema_with_a_different_base_does_not_bypass_validation():
    schema = {"$ref": "child.json"}
    registry = Registry().with_resources(
        [
            ("https://example.test/one/child.json", DRAFT202012.create_resource(True)),
            ("https://example.test/two/child.json", DRAFT202012.create_resource(False)),
        ]
    )
    doc = from_json(
        '""',
        json_schema=schema,
        base_uri="https://example.test/one/root.json",
        reference_registry=registry,
    )
    mapping = XmlMapping.from_json_schema(
        schema,
        root_name="r",
        base_uri="https://example.test/two/root.json",
        reference_registry=registry,
    )
    assert doc.is_valid
    with pytest.raises(XmlMappingError, match="boolean schema"):
        doc.to_xml(mapping)


def test_applied_schema_with_a_different_registry_does_not_bypass_validation():
    uri = "https://example.test/child.json"
    schema = {"$ref": uri}
    prior = Registry().with_resource(uri, DRAFT202012.create_resource({"const": 1}))
    target = prior.with_resource(uri, DRAFT202012.create_resource({"const": True}))
    doc = from_json("1", json_schema=schema, reference_registry=prior)
    assert doc.is_valid
    mapping = XmlMapping.from_json_schema(schema, root_name="r", reference_registry=target)
    with pytest.raises(XmlMappingError, match="JSON Schema 'const'"):
        doc.to_xml(mapping)


def test_unregistered_reference_does_not_inherit_a_documents_registry():
    uri = "https://example.test/child.json"
    schema = {"$ref": uri}
    registry = Registry().with_resource(uri, DRAFT202012.create_resource(True))
    doc = from_json("1", json_schema=schema, reference_registry=registry)
    with pytest.raises(JsonSchemaError, match="Cannot resolve JSON Schema reference"):
        doc.to_xml(XmlMapping.from_json_schema(schema, root_name="r"))


def test_applied_schema_with_a_different_policy_does_not_authorize_a_reference(tmp_path):
    (tmp_path / "child.json").write_text("true", encoding="utf-8")
    schema = {"$ref": "child.json"}
    base_uri = str(tmp_path / "root.json")
    doc = from_json(
        "1", json_schema=schema, base_uri=base_uri, resource_policy=RemoteResourcePolicy()
    )
    mapping = XmlMapping.from_json_schema(schema, root_name="r", base_uri=base_uri)
    with pytest.raises(JsonSchemaError, match="Cannot resolve JSON Schema reference"):
        doc.to_xml(mapping)


def test_applied_schema_with_different_assertions_does_not_bypass_validation():
    doc = from_json("1", json_schema={"const": 1})
    mapping = XmlMapping.from_json_schema({"const": True}, root_name="r")
    with pytest.raises(XmlMappingError, match="JSON Schema 'const'"):
        doc.to_xml(mapping)


def test_equivalent_large_numeric_constraints_reuse_validation_without_serialization(tmp_path):
    (tmp_path / "child.json").write_text("true", encoding="utf-8")
    schema = {"$ref": "child.json", "maximum": 10 ** (get_int_max_str_digits() or 4400)}
    base_uri = str(tmp_path / "root.json")
    policy = RemoteResourcePolicy()
    doc = from_data(1, json_schema=schema, base_uri=base_uri, resource_policy=policy)
    mapping = XmlMapping.from_json_schema(
        schema, root_name="r", base_uri=base_uri, resource_policy=policy
    )
    (tmp_path / "child.json").unlink()
    assert doc.to_xml(mapping).root.text_value() == "1"


def test_different_numeric_representations_do_not_share_a_guidance_snapshot(tmp_path):
    child = tmp_path / "child.json"
    child.write_text("true", encoding="utf-8")
    base_uri = str(tmp_path / "root.json")
    policy = RemoteResourcePolicy()
    doc = from_json(
        "1",
        json_schema={"$ref": "child.json", "maximum": 1},
        base_uri=base_uri,
        resource_policy=policy,
    )
    mapping = XmlMapping.from_json_schema(
        '{"$ref":"child.json","maximum":1.0}',
        root_name="r",
        base_uri=base_uri,
        resource_policy=policy,
    )
    child.write_text("false", encoding="utf-8")
    with pytest.raises(XmlMappingError, match="boolean schema"):
        doc.to_xml(mapping)


@pytest.mark.parametrize(
    ("prior", "target"),
    [
        ({"const": {}}, {"const": {"extra": None}}),
        ({"const": [1]}, {"const": [1, 2]}),
        ({"const": [1]}, {"const": [2]}),
    ],
)
def test_structurally_distinct_schema_snapshots_do_not_share_validation(prior, target):
    doc = from_data(prior["const"], json_schema=prior)
    with pytest.raises(XmlMappingError, match="JSON Schema 'const'"):
        doc.to_xml(XmlMapping.from_json_schema(target, root_name="r"))


def test_equivalent_schema_arrays_reuse_validation(tmp_path):
    child = tmp_path / "child.json"
    child.write_text("true", encoding="utf-8")
    schema = {"$ref": "child.json", "type": ["array", "null"], "prefixItems": [{"const": 2}]}
    base_uri = str(tmp_path / "root.json")
    policy = RemoteResourcePolicy()
    doc = from_json("[2]", json_schema=schema, base_uri=base_uri, resource_policy=policy)
    mapping = XmlMapping.from_json_schema(
        schema, root_name="r", base_uri=base_uri, resource_policy=policy
    )
    child.unlink()
    assert doc.to_xml(mapping).root.children[0].text_value() == "2"


def test_reordered_pattern_properties_can_change_the_validation_result():
    # The evaluator joins these expressions for its additionalProperties check:
    # in the first order \2 refers to (a), in the second order it refers to (b).
    prior = {"patternProperties": {"(x)": True, r"^(a)(b)\2$": True}, "additionalProperties": False}
    target = {
        "patternProperties": {r"^(a)(b)\2$": True, "(x)": True},
        "additionalProperties": False,
    }
    doc = from_json('{"aba":1}', json_schema=prior)
    assert doc.is_valid
    with pytest.raises(XmlMappingError, match="JSON Schema 'additionalProperties'"):
        doc.to_xml(XmlMapping.from_json_schema(target, root_name="r"))


def test_applied_format_assertions_cannot_change_the_mappings_successful_branches():
    schema = {
        "anyOf": [
            {
                "properties": {
                    "value": {"type": ["array", "null"]},
                    "date": {"format": "date"},
                }
            },
            {"properties": {"value": {"type": ["object", "null"]}}},
        ]
    }
    doc = from_json('{"value":null,"date":"not a date"}', json_schema=schema, format_assertion=True)
    assert doc.is_valid
    with pytest.raises(XmlMappingError, match="ambiguous"):
        doc.to_xml(XmlMapping.from_json_schema(schema, root_name="r"))


def test_mappings_compile_and_snapshot_the_schema_before_projection():
    schema = {"type": "integer"}
    mapping = XmlMapping.from_json_schema(schema, root_name="r")
    schema["type"] = "string"
    assert from_json("2").to_xml(mapping).root.text_value() == "2"
    with pytest.raises(JsonSchemaError, match="Invalid JSON Schema"):
        XmlMapping.from_json_schema({"type": "bogus"}, root_name="r")


def test_boolean_schema_falls_back_to_element_first_defaults():
    mapping = XmlMapping.from_json_schema(True, root_name="r")
    xml = from_json('{"value":1}').to_xml(mapping)
    assert xml.root.children[0]._name_ == "value"
    assert xml.root.children[0].text_value() == "1"


def test_json_schema_format_assertion_is_independent_of_format_typing() -> None:
    schema = {"properties": {"value": {"type": "integer", "format": "int32"}}}
    doc = from_json('{"value":2147483648}', json_schema=schema)
    assert doc.is_valid
    mapping = XmlMapping.from_json_schema(schema, root_name="r", format_typing=True)
    assert doc.to_xml(mapping).to_string() == "<r><value>2147483648</value></r>"

    mapping = XmlMapping.from_json_schema(schema, root_name="r", format_assertion=True)
    with pytest.raises(XmlMappingError, match="JSON Schema 'format'") as caught:
        doc.to_xml(mapping)
    (issue,) = caught.value.report.issues
    location = issue.location
    assert isinstance(location, DataLocation)
    assert (issue.code, location.pointer, location.schema_pointer) == (
        "json-schema-invalid",
        "/value",
        "/properties/value/format",
    )


def test_explicit_mapping_type_wins_over_the_applied_format() -> None:
    schema = {"properties": {"amount": {"type": "number", "format": "float"}}}
    doc = from_json('{"amount":16777217}')
    mapping = XmlMapping.from_json_schema(
        schema, root_name="r", format_typing=True, rules={"/amount": MappingRule(type="xs:double")}
    )
    assert doc.to_xml(mapping).to_string() == "<r><amount>16777217.0</amount></r>"


@pytest.mark.parametrize(
    ("alternatives", "expected"),
    [
        ([True, {"format": "double", "maximum": 0}], "9007199254740992.0"),
        ([True, {"format": "double"}], "9007199254740993.00"),
    ],
    ids=["one-compatible-format", "conflicting-successful-formats"],
)
def test_projection_resolves_formats_from_successful_occurrence_schemas(
    alternatives: list[bool | dict[str, object]], expected: str
) -> None:
    schema = {
        "prefixItems": [{"anyOf": [{"type": "number", "format": "float"}, *alternatives]}],
        "items": True,
    }
    mapping = XmlMapping.from_json_schema(schema, root_name="r", format_typing=True)
    xml = from_json("[9007199254740993.00,1.0]").to_xml(mapping)
    assert [node.text_value() for node in xml.root.children] == [expected, "1.0"]


@pytest.mark.parametrize("mapping_typing", [True, False])
def test_evaluation_is_reused_when_only_format_typing_differs(
    tmp_path: Path, mapping_typing: bool
) -> None:
    child = tmp_path / "child.json"
    child.write_text('{"type":"integer","format":"int32"}', encoding="utf-8")
    schema = {"$ref": "child.json"}
    base_uri = str(tmp_path / "root.json")
    policy = RemoteResourcePolicy()
    doc = from_json(
        "1.0",
        json_schema=schema,
        base_uri=base_uri,
        resource_policy=policy,
        format_typing=not mapping_typing,
    )
    assert doc.is_valid
    mapping = XmlMapping.from_json_schema(
        schema,
        root_name="r",
        base_uri=base_uri,
        resource_policy=policy,
        format_typing=mapping_typing,
    )
    child.write_text("false", encoding="utf-8")
    expected = "1" if mapping_typing else "1.0"
    assert doc.to_xml(mapping).root.text_value() == expected
    with pytest.raises(XmlMappingError, match="boolean schema"):
        from_json("1.0").to_xml(mapping)
