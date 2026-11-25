# ruff: noqa: RUF043
"""Derive explicit XML mappings from OpenAPI Schema Object XML metadata."""

import pytest
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from pyxsd import xsd_data_types as xsd
from pyxsd.locations import DataLocation
from pyxsd.namespaces import XSI_NS
from pyxsd.schemaless import MappingRule, XmlMapping, XmlMappingError, from_data, from_json


def test_openapi_xml_object_controls_attributes_names_and_namespaces():
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "xml": {"nodeType": "attribute"}},
            "name": {"type": "string", "xml": {"name": "title", "namespace": "urn:orders"}},
        },
        "xml": {"name": "order", "namespace": "urn:orders", "prefix": "o"},
    }
    xml = from_data({"id": "x", "name": "n"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.root._name_ == "{urn:orders}order"
    assert xml.root.attribute_lexical("id") == "x"
    assert xml.root.children[0]._name_ == "{urn:orders}title"


def test_openapi_formats_change_lexical_values_only_when_typing_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = {
        "xml": {"name": "r"},
        "properties": {
            "text": {"type": "string", "format": "int64", "xml": {"nodeType": "text"}},
            "small": {
                "type": "integer",
                "format": "int32",
                "xml": {"nodeType": "attribute", "name": "n", "namespace": "urn:x", "prefix": "p"},
            },
            "large": {"type": "integer", "format": "int64"},
            "ratios": {
                "type": "array",
                "xml": {"wrapped": True},
                "items": {"type": "number", "format": "float", "xml": {"name": "ratio"}},
            },
            "amount": {"type": "number", "format": "double"},
            "legacy": {"type": "string", "format": "byte"},
            "preferred": {"type": "string", "contentEncoding": "base64"},
        },
    }
    doc = from_json(
        '{"text":"0007","small":1.0,"large":9223372036854775807.0,'
        '"ratios":[16777217],"amount":9007199254740993.00,"legacy":"YQ==","preferred":"Yg=="}'
    )
    # Strict base64 is already canonical. Track the real formatter to
    # distinguish its output from the identical native text.
    formatted = []
    lexical = xsd.Base64Binary.lexical

    def track_base64(value: xsd.Base64Binary) -> str:
        formatted.append(str(value))
        return lexical(value)

    monkeypatch.setattr(xsd.Base64Binary, "lexical", track_base64)
    assert doc.to_xml(XmlMapping.from_openapi(schema)).to_string() == (
        '<r xmlns:p="urn:x" p:n="1.0">0007<large>9223372036854775807.0</large>'
        "<ratios><ratio>16777217</ratio></ratios><amount>9007199254740993.00</amount>"
        "<legacy>YQ==</legacy><preferred>Yg==</preferred></r>"
    )
    assert formatted == []
    assert doc.to_xml(XmlMapping.from_openapi(schema, format_typing=True)).to_string() == (
        '<r xmlns:p="urn:x" p:n="1">7<large>9223372036854775807</large>'
        "<ratios><ratio>16777216.0</ratio></ratios><amount>9007199254740992.0</amount>"
        "<legacy>YQ==</legacy><preferred>Yg==</preferred></r>"
    )
    assert formatted == ["YQ==", "Yg=="]


def test_openapi_assertion_only_rejects_out_of_range_int32() -> None:
    schema = {"xml": {"name": "r"}, "properties": {"value": {"format": "int32"}}}
    mapping = XmlMapping.from_openapi(schema, format_assertion=True)
    assert from_json('{"value":1.0}').to_xml(mapping).to_string() == "<r><value>1.0</value></r>"
    with pytest.raises(XmlMappingError, match="JSON Schema 'format'") as caught:
        from_data({"value": 2147483648}).to_xml(mapping)
    (issue,) = caught.value.report.issues
    location = issue.location
    assert isinstance(location, DataLocation)
    assert (location.pointer, location.schema_pointer) == (
        "/value",
        "/properties/value/format",
    )


def test_openapi_base64_content_encoding_remains_annotation_only() -> None:
    schema = {"type": "string", "contentEncoding": "base64"}
    mapping = XmlMapping.from_openapi(
        schema, root_name="r", format_assertion=True, format_typing=True
    )
    assert from_data("Y Q==").to_xml(mapping).root.text_value() == "Y Q=="


def test_inline_openapi_root_requires_a_name():
    with pytest.raises(XmlMappingError, match="root_name or xml.name"):
        XmlMapping.from_openapi({"type": "object"})


def test_openapi_xml_prefix_controls_the_element_and_attribute_qnames():
    schema = {
        "type": "object",
        "xml": {"name": "r", "namespace": "urn:x", "prefix": "p"},
        "properties": {
            "label": {"type": "string", "xml": {"namespace": "urn:x", "prefix": "q"}},
            "id": {
                "type": "integer",
                "xml": {"nodeType": "attribute", "namespace": "urn:x", "prefix": "z"},
            },
        },
    }
    doc = from_data({"label": "x", "id": 7}).to_xml(XmlMapping.from_openapi(schema))

    assert doc.to_string() == (
        '<p:r xmlns:p="urn:x" xmlns:z="urn:x" z:id="7"><q:label xmlns:q="urn:x">x</q:label></p:r>'
    )
    assert doc.root._name_ == "{urn:x}r"
    assert doc.root.children[0]._name_ == "{urn:x}label"
    assert doc.root.attribute_lexical("{urn:x}id") == "7"
    root = doc.to_xdm().root_element
    (child,) = root.children
    (attribute,) = root.attributes
    assert [(node.name, node.namespace_uri, node.prefix) for node in (root, child, attribute)] == [
        ("{urn:x}r", "urn:x", "p"),
        ("{urn:x}label", "urn:x", "q"),
        ("{urn:x}id", "urn:x", "z"),
    ]


def test_openapi_node_types_cover_element_attribute_text_and_cdata():
    schema = {
        "type": "object",
        "xml": {"name": "record"},
        "properties": {
            "code": {"type": "integer", "xml": {"nodeType": "attribute"}},
            "label": {"type": "string", "xml": {"nodeType": "text"}},
            "element": {"type": "string"},
            "raw": {"type": "string", "xml": {"nodeType": "cdata", "name": "ignored"}},
        },
    }
    xml = from_data({"code": 7, "label": "lead", "element": "body", "raw": "<x/>"}).to_xml(
        XmlMapping.from_openapi(schema)
    )
    assert xml.root.attribute_lexical("code") == "7"
    assert xml.root.text_value() == "lead"
    assert [node._name_ for node in xml.root.children] == ["element"]
    assert xml.root.children[0].text_value() == "body"
    assert xml.root.children[0].tail_value() == "<x/>"


def test_openapi_none_nodes_inline_children_or_drop_scalars():
    schema = {
        "type": "object",
        "xml": {"name": "record"},
        "properties": {
            "hidden": {"type": "string", "xml": {"nodeType": "none", "name": "ignored"}},
            "flat": {
                "type": "object",
                "xml": {"nodeType": "none"},
                "properties": {
                    "id": {"type": "string", "xml": {"nodeType": "attribute"}},
                    "value": {"type": "string"},
                },
            },
        },
    }
    xml = from_data({"hidden": "x", "flat": {"id": "a", "value": "v"}}).to_xml(
        XmlMapping.from_openapi(schema)
    )
    assert xml.to_string() == '<record id="a"><value>v</value></record>'
    (issue,) = xml.report.issues
    assert issue.code == "unmapped-scalar"
    assert issue.details["json_pointer"] == "/hidden"


def test_openapi_unwrapped_arrays_repeat_the_property_name():
    schema = {
        "type": "object",
        "xml": {"name": "document"},
        "properties": {
            "orders": {"type": "array", "items": {"type": "string"}},
            "renamed": {
                "type": "array",
                "xml": {"name": "ignored"},
                "items": {"type": "string"},
            },
            "lines": {
                "type": "array",
                "items": {"type": "string", "xml": {"name": "line"}},
            },
        },
    }
    xml = from_data({"orders": ["1", "2"], "renamed": ["3"], "lines": ["4"]}).to_xml(
        XmlMapping.from_openapi(schema)
    )
    assert [node._name_ for node in xml.root.children] == [
        "orders",
        "orders",
        "renamed",
        "line",
    ]
    assert [node.text_value() for node in xml.root.children] == ["1", "2", "3", "4"]


def test_openapi_explicit_element_node_type_wraps_an_array():
    schema = {
        "type": "object",
        "xml": {"name": "document"},
        "properties": {
            "animals": {
                "type": "array",
                "xml": {"nodeType": "element"},
                "items": {"type": "string"},
            },
            "aliens": {
                "type": "array",
                "xml": {"nodeType": "element", "name": "aliens"},
                "items": {"type": "string", "xml": {"name": "alien"}},
            },
        },
    }
    xml = from_data({"animals": ["dog"], "aliens": ["x"]}).to_xml(XmlMapping.from_openapi(schema))
    animals, aliens = xml.root.children
    assert animals._name_ == "animals"
    assert [node._name_ for node in animals.children] == ["animals"]
    assert aliens._name_ == "aliens"
    assert [node._name_ for node in aliens.children] == ["alien"]


def test_openapi_array_items_and_wrappers_keep_their_own_namespaces():
    schema = {
        "type": "object",
        "xml": {"name": "document"},
        "properties": {
            "wrapped": {
                "type": "array",
                "xml": {"nodeType": "element", "namespace": "urn:wrapped"},
                "items": {
                    "type": "string",
                    "xml": {"name": "row", "namespace": "urn:item", "prefix": "i"},
                },
            },
            "inline": {
                "type": "array",
                "items": {
                    "type": "string",
                    "xml": {"name": "cell", "namespace": "urn:item", "prefix": "i"},
                },
            },
        },
    }
    xml = from_data({"wrapped": ["a"], "inline": ["b"]}).to_xml(XmlMapping.from_openapi(schema))
    wrapped, cell = xml.root.children
    assert wrapped._name_ == "{urn:wrapped}wrapped"
    assert wrapped.children[0]._name_ == "{urn:item}row"
    assert cell._name_ == "{urn:item}cell"


def test_openapi_unconstrained_items_and_additional_properties_use_their_names():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "free": {"type": "array"},
            "rows": {"type": "array", "items": True},
        },
        "additionalProperties": {"type": "string", "xml": {"name": "extra"}},
    }
    xml = from_data({"free": ["a"], "rows": ["b"], "other": "c"}).to_xml(
        XmlMapping.from_openapi(schema)
    )
    assert [node._name_ for node in xml.root.children] == ["free", "rows", "extra"]
    assert [node.text_value() for node in xml.root.children] == ["a", "b", "c"]


def test_openapi_explicit_rule_for_an_unmodeled_pointer_is_kept():
    schema = {"type": "object", "xml": {"name": "r"}}
    mapping = XmlMapping.from_openapi(
        schema, rules={"/later": MappingRule(kind="attribute", name="kept")}
    )
    xml = from_data({"later": "x"}).to_xml(mapping)
    assert xml.root.attribute_lexical("kept") == "x"


def test_openapi_explicit_text_with_attributes_on_array_items():
    schema = {
        "type": "array",
        "xml": {"nodeType": "element", "name": "animals"},
        "items": {
            "type": "object",
            "xml": {"name": "animal"},
            "properties": {
                "kind": {"type": "string", "xml": {"nodeType": "attribute"}},
                "name": {"type": "string", "xml": {"nodeType": "text"}},
            },
        },
    }
    xml = from_data([{"kind": "Cat", "name": "Fluffy"}, {"kind": "Dog", "name": "Fido"}]).to_xml(
        XmlMapping.from_openapi(schema)
    )
    assert xml.to_string() == (
        '<animals><animal kind="Cat">Fluffy</animal><animal kind="Dog">Fido</animal></animals>'
    )


def test_openapi_prefix_items_name_ordered_elements():
    schema = {
        "type": "array",
        "xml": {"nodeType": "element", "name": "OneTwoThree"},
        "prefixItems": [
            {"type": "string", "xml": {"name": "One"}},
            {"type": "integer", "xml": {"name": "Two"}},
        ],
    }
    xml = from_data(["a", 2]).to_xml(XmlMapping.from_openapi(schema))
    assert [node._name_ for node in xml.root.children] == ["One", "Two"]


def test_openapi_unnamed_root_array_items_fail():
    schema = {"type": "array", "items": {"type": "string"}}
    mapping = XmlMapping.from_openapi(schema, root_name="rows")
    with pytest.raises(XmlMappingError, match="xml.name or an explicit"):
        from_data(["a"]).to_xml(mapping)


@pytest.mark.parametrize(
    ("schema", "rules"),
    [
        (
            {
                "type": "array",
                "items": {"type": "string", "xml": {"name": "row"}},
            },
            None,
        ),
        ({"type": "array", "items": {"type": "string"}}, {"/0": MappingRule(name="row")}),
        ({"type": "array", "items": {"type": "string"}}, {"": MappingRule(item_name="row")}),
    ],
)
def test_openapi_root_array_items_accept_a_name(schema, rules):
    mapping = XmlMapping.from_openapi(schema, root_name="rows", rules=rules)
    xml = from_data(["a"]).to_xml(mapping)
    assert [node._name_ for node in xml.root.children] == ["row"]


def test_openapi_object_with_items_uses_object_member_descent():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "items": {"type": "string", "xml": {"name": "ignored"}},
        "additionalProperties": {
            "type": "object",
            "properties": {"id": {"type": "string", "xml": {"nodeType": "attribute"}}},
        },
    }
    xml = from_data({"member": {"id": "x"}}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == '<r><member id="x"/></r>'


def test_openapi_dynamic_ref_defaults_to_none():
    schema = {
        "$id": "https://example.test/orders",
        "type": "object",
        "xml": {"name": "r"},
        "$defs": {
            "node": {
                "$dynamicAnchor": "node",
                "type": "object",
                "properties": {"id": {"type": "string"}},
            }
        },
        "properties": {"child": {"$dynamicRef": "#node"}},
        "additionalProperties": True,
    }
    xml = from_data({"child": {"id": "x"}}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><id>x</id></r>"
    assert not [issue for issue in xml.report.issues if issue.code == "openapi-metadata-unresolved"]


def test_openapi_nullable_array_declaration_defaults_to_none():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "rows": {
                "type": ["array", "null"],
                "items": {"type": "string", "xml": {"name": "row"}},
            }
        },
    }
    xml = from_data({"rows": ["a"]}).to_xml(XmlMapping.from_openapi(schema))
    assert [node._name_ for node in xml.root.children] == ["row"]


def test_openapi_additional_properties_without_xml_name_use_the_instance_key():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "additionalProperties": {"type": "string"},
    }
    xml = from_data({"other": "x"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><other>x</other></r>"


def test_openapi_non_ascii_namespace_name_is_valid():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"value": {"type": "string", "xml": {"namespace": "urn:é"}}},
    }
    xml = from_data({"value": "v"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.root.children[0]._name_ == "{urn:é}value"


def test_openapi_absolute_namespace_must_still_be_a_uri_reference():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"value": {"type": "string", "xml": {"namespace": "urn:bad space"}}},
    }
    with pytest.raises(XmlMappingError, match="non-relative"):
        XmlMapping.from_openapi(schema)


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        ({"attribute": True}, "attribute"),
        ({"attribute": False}, "attribute"),
        ({"wrapped": True}, "wrapped"),
        ({"wrapped": False}, "wrapped"),
    ],
)
def test_openapi_node_type_cannot_be_combined_with_legacy_fields(xml, message):
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"value": {"type": "string", "xml": {"nodeType": "element", **xml}}},
    }
    with pytest.raises(XmlMappingError, match=message) as caught:
        XmlMapping.from_openapi(schema)
    assert caught.value.report.issues[0].details["schema_pointer"].endswith("/xml")


def test_openapi_legacy_attribute_and_wrapped_fields_still_apply():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "id": {"type": "string", "xml": {"attribute": True}},
            "note": {"type": "string", "xml": {"attribute": False}},
            "rows": {"type": "array", "xml": {"wrapped": True}, "items": {"type": "string"}},
            "cols": {"type": "array", "xml": {"wrapped": False}, "items": {"type": "string"}},
        },
    }
    xml = from_data({"id": "1", "note": "n", "rows": ["2"], "cols": ["3"]}).to_xml(
        XmlMapping.from_openapi(schema)
    )
    assert xml.root.attribute_lexical("id") == "1"
    assert (
        xml.to_string() == '<r id="1"><note>n</note><rows><rows>2</rows></rows><cols>3</cols></r>'
    )


def test_openapi_wrapped_applies_only_to_arrays():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"v": {"type": "string", "xml": {"wrapped": True}}},
    }
    with pytest.raises(XmlMappingError, match="array"):
        XmlMapping.from_openapi(schema)


def test_openapi_null_elements_are_nil_and_null_attributes_are_omitted():
    schema = {
        "type": "object",
        "xml": {"name": "product"},
        "properties": {
            "count": {"type": ["number", "null"], "xml": {"nodeType": "attribute"}},
            "description": {"type": ["string", "null"]},
        },
    }
    xml = from_data({"count": None, "description": None}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.root.attributes == {}
    assert [node._name_ for node in xml.root.children] == ["description"]
    assert xml.root.children[0].attribute_lexical(f"{{{XSI_NS}}}nil") == "true"
    (issue,) = xml.report.issues
    assert (issue.code, issue.details["json_pointer"]) == ("null-attribute-omitted", "/count")


def test_openapi_relative_namespace_is_rejected():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"name": {"type": "string", "xml": {"namespace": "orders"}}},
    }
    with pytest.raises(XmlMappingError, match="non-relative") as caught:
        XmlMapping.from_openapi(schema)
    issue = caught.value.report.issues[0]
    assert issue.details["schema_pointer"] == "/properties/name/xml/namespace"


def test_openapi_explicit_rules_override_metadata_fields():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "id": {
                "type": "string",
                "xml": {"nodeType": "attribute", "name": "code", "namespace": "urn:x"},
            }
        },
    }
    mapping = XmlMapping.from_openapi(
        schema, rules={"/id": MappingRule(kind="element", name="identifier")}
    )
    xml = from_data({"id": "x"}).to_xml(mapping)
    assert not xml.root.attributes
    assert xml.root.children[0]._name_ == "{urn:x}identifier"


def test_openapi_explicit_none_kind_drops_metadata_naming():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "flat": {
                "type": "object",
                "xml": {"name": "skip", "namespace": "urn:skip", "prefix": "s"},
                "properties": {"a": {"type": "string"}},
            }
        },
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/flat": MappingRule(kind="none")})
    xml = from_data({"flat": {"a": "v"}}).to_xml(mapping)
    assert xml.to_string() == "<r><a>v</a></r>"


def test_openapi_explicit_text_kind_drops_metadata_naming():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "label": {
                "type": "string",
                "xml": {"name": "renamed", "namespace": "urn:skip", "prefix": "s"},
            }
        },
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/label": MappingRule(kind="text")})
    xml = from_data({"label": "v"}).to_xml(mapping)
    assert xml.to_string() == "<r>v</r>"


def test_openapi_rule_overrides_metadata_name():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"value": {"type": "string", "xml": {"name": "meta"}}},
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/value": MappingRule(name="user")})
    xml = from_data({"value": "v"}).to_xml(mapping)
    assert [node._name_ for node in xml.root.children] == ["user"]


def test_openapi_rule_overrides_metadata_namespace():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"value": {"type": "string", "xml": {"namespace": "urn:meta"}}},
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/value": MappingRule(namespace="urn:user")})
    xml = from_data({"value": "v"}).to_xml(mapping)
    assert xml.root.children[0]._name_ == "{urn:user}value"


def test_openapi_rule_overrides_metadata_prefix():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "value": {"type": "string", "xml": {"namespace": "urn:meta", "prefix": "m"}}
        },
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/value": MappingRule(prefix="p")})
    xml = from_data({"value": "v"}).to_xml(mapping)
    assert xml.root.children[0].namespaces == {"p": "urn:meta"}


def test_openapi_rule_overrides_metadata_array_wrapping():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "rows": {
                "type": "array",
                "xml": {"nodeType": "element"},
                "items": {"type": "string", "xml": {"name": "row"}},
            }
        },
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/rows": MappingRule(array="unwrapped")})
    xml = from_data({"rows": ["a"]}).to_xml(mapping)
    assert xml.to_string() == "<r><row>a</row></r>"


def test_openapi_item_name_overrides_metadata_item_name():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "rows": {
                "type": "array",
                "items": {"type": "string", "xml": {"name": "meta"}},
            }
        },
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/rows": MappingRule(item_name="user")})
    xml = from_data({"rows": ["a"]}).to_xml(mapping)
    assert xml.to_string() == "<r><user>a</user></r>"


def test_openapi_rule_overrides_metadata_null_policy():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"value": {"type": ["string", "null"]}},
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/value": MappingRule(null="omit")})
    xml = from_data({"value": None}).to_xml(mapping)
    assert not xml.root.children
    assert [(issue.code, issue.details["json_pointer"]) for issue in xml.report.issues] == [
        ("null-omitted", "/value")
    ]


def test_openapi_rule_overrides_metadata_type():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"value": {"type": "string"}},
    }
    mapping = XmlMapping.from_openapi(schema, rules={"/value": MappingRule(type="xs:integer")})
    xml = from_data({"value": "0007"}).to_xml(mapping)
    assert xml.root.children[0].text_value() == "7"


def test_openapi_rule_overrides_metadata_order():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    }
    mapping = XmlMapping.from_openapi(schema, rules={"": MappingRule(order=("b", "a"))})
    xml = from_data({"a": 1, "b": 2}).to_xml(mapping)
    assert [node._name_ for node in xml.root.children] == ["b", "a"]


def test_openapi_ref_applies_target_xml_metadata():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "$defs": {
            "child": {
                "type": "object",
                "properties": {"id": {"type": "string", "xml": {"nodeType": "attribute"}}},
            }
        },
        "properties": {"child": {"$ref": "#/$defs/child"}},
    }
    xml = from_data({"child": {"id": "x"}}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == '<r id="x"/>'
    assert not xml.report.issues


def test_openapi_dynamic_ref_applies_target_xml_metadata():
    schema = {
        "$id": "https://example.test/orders",
        "type": "object",
        "xml": {"name": "r"},
        "$defs": {
            "node": {
                "$dynamicAnchor": "node",
                "type": "object",
                "properties": {"id": {"type": "string", "xml": {"nodeType": "attribute"}}},
            }
        },
        "properties": {"child": {"$dynamicRef": "#node"}},
    }
    xml = from_data({"child": {"id": "x"}}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == '<r id="x"/>'
    assert not xml.report.issues


def test_openapi_ref_through_an_array_pointer_applies_target_xml_metadata():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "allOf": [
            {
                "type": "object",
                "properties": {"id": {"type": "string", "xml": {"nodeType": "attribute"}}},
            }
        ],
        "properties": {"child": {"$ref": "#/allOf/0"}},
    }
    xml = from_data({"child": {"id": "x"}}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == '<r id="x"/>'
    assert not xml.report.issues


def test_openapi_ref_to_the_root_uses_its_name_without_spurious_warnings():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"child": {"$ref": "#"}},
    }
    xml = from_data({"child": {}}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><r/></r>"
    assert [issue.code for issue in xml.report.issues] == ["empty-object"]


def test_openapi_pattern_properties_apply_xml_metadata():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "patternProperties": {"^x": {"type": "string", "xml": {"name": "renamed"}}},
    }
    xml = from_data({"xa": "v"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><renamed>v</renamed></r>"
    assert not xml.report.issues


def test_openapi_allof_applies_xml_metadata():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "child": {
                "type": "object",
                "allOf": [
                    {"properties": {"id": {"type": "string", "xml": {"nodeType": "attribute"}}}}
                ],
            }
        },
    }
    xml = from_data({"child": {"id": "x"}}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == '<r><child id="x"/></r>'
    assert not xml.report.issues


def test_openapi_applicator_without_xml_metadata_does_not_warn():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {
            "child": {
                "type": "object",
                "allOf": [True, {"properties": {"id": {"type": "string"}}}],
            }
        },
    }
    xml = from_data({"child": {"id": "x"}}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><child><id>x</id></child></r>"
    assert not xml.report.issues


def test_openapi_dependent_schemas_apply_xml_metadata():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"a": {"type": "integer"}, "id": {"type": "string"}},
        "dependentSchemas": {
            "a": {"properties": {"id": {"type": "string", "xml": {"nodeType": "attribute"}}}}
        },
    }
    xml = from_data({"a": 1, "id": "x"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == '<r id="x"><a>1</a></r>'
    assert not xml.report.issues


def test_openapi_then_branch_applies_xml_metadata():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "properties": {"choose": {"const": "x"}, "id": {"type": "string"}},
        "if": {"properties": {"choose": {"const": "x"}}},
        "then": {"properties": {"id": {"type": "string", "xml": {"nodeType": "attribute"}}}},
    }
    xml = from_data({"choose": "x", "id": "v"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == '<r id="v"><choose>x</choose></r>'
    assert not xml.report.issues


def test_openapi_pattern_properties_without_xml_metadata_do_not_warn():
    schema = {
        "type": "object",
        "xml": {"name": "r"},
        "patternProperties": {
            "^x": {"type": "string", "xml": {"name": "renamed"}},
            "^o": {"type": "string"},
        },
        "additionalProperties": {"type": "string"},
    }
    xml = from_data({"other": "v"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<r><other>v</other></r>"
    assert not xml.report.issues


def test_openapi_root_name_overrides_xml_name_but_not_namespace():
    schema = {"type": "object", "xml": {"name": "doc", "namespace": "urn:d"}}
    xml = from_data({}).to_xml(XmlMapping.from_openapi(schema, root_name="r"))
    assert xml.root._name_ == "{urn:d}r"


def test_openapi_root_name_may_repeat_xml_namespace():
    schema = {"type": "object", "xml": {"name": "doc", "namespace": "urn:d"}}
    xml = from_data({}).to_xml(XmlMapping.from_openapi(schema, root_name="{urn:d}r"))
    assert xml.root._name_ == "{urn:d}r"


def test_openapi_root_name_cannot_disagree_with_xml_namespace():
    schema = {"type": "object", "xml": {"name": "doc", "namespace": "urn:d"}}
    with pytest.raises(XmlMappingError, match="namespace"):
        XmlMapping.from_openapi(schema, root_name="{urn:other}r")


def test_openapi_root_namespace_and_prefix_bind_the_root():
    schema = {"type": "object", "xml": {"name": "order", "namespace": "urn:orders", "prefix": "o"}}
    xml = from_data({}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.root._name_ == "{urn:orders}order"
    assert xml.root.namespaces == {"o": "urn:orders"}


def test_openapi_root_cannot_be_an_attribute():
    with pytest.raises(XmlMappingError, match="attribute"):
        XmlMapping.from_openapi({"type": "object", "xml": {"nodeType": "attribute"}}, root_name="r")


def test_openapi_root_none_still_seeds_the_element_from_xml_name():
    schema = {
        "type": "object",
        "xml": {"nodeType": "none", "name": "ignored"},
        "properties": {"a": {"type": "string"}},
    }
    xml = from_data({"a": "x"}).to_xml(XmlMapping.from_openapi(schema))
    assert xml.to_string() == "<ignored><a>x</a></ignored>"


def test_openapi_boolean_schema_needs_an_explicit_root_name():
    with pytest.raises(XmlMappingError, match="root_name or xml.name"):
        XmlMapping.from_openapi(True)
    xml = from_data({"a": 1}).to_xml(XmlMapping.from_openapi(True, root_name="r"))
    assert xml.to_string() == "<r><a>1</a></r>"


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        ("not-an-object", "object"),
        ({"nodeType": "bogus"}, "nodeType"),
        ({"nodeType": 1}, "nodeType"),
        ({"name": 1}, "name"),
        ({"namespace": 1}, "namespace"),
        ({"prefix": 1}, "prefix"),
        ({"attribute": 1}, "attribute"),
        ({"wrapped": 1}, "wrapped"),
    ],
)
def test_openapi_malformed_xml_objects_are_rejected(xml, message):
    with pytest.raises(XmlMappingError, match=message):
        XmlMapping.from_openapi({"type": "object", "xml": xml}, root_name="r")


@pytest.mark.parametrize(
    ("keyword", "container"),
    [
        ("contains", "schema"),
        ("propertyNames", "schema"),
        ("not", "schema"),
        ("if", "schema"),
        ("then", "schema"),
        ("else", "schema"),
        ("unevaluatedItems", "schema"),
        ("unevaluatedProperties", "schema"),
        ("patternProperties", "map"),
        ("dependentSchemas", "map"),
        ("allOf", "list"),
        ("anyOf", "list"),
        ("oneOf", "list"),
    ],
)
def test_openapi_validates_xml_objects_under_schema_keywords(keyword, container):
    nested = {"xml": "not-an-object"}
    if container == "map":
        value = {"x": nested}
    elif container == "list":
        value = [nested]
    else:
        value = nested
    schema = {"type": "object", "xml": {"name": "r"}, keyword: value}
    with pytest.raises(XmlMappingError, match="xml field must be an object"):
        XmlMapping.from_openapi(schema)


def test_openapi_root_name_must_be_a_string():
    schema = {"type": "object", "xml": {"name": "r"}}
    with pytest.raises(TypeError, match="root_name"):
        XmlMapping.from_openapi(schema, root_name=1)  # type: ignore[arg-type]


def test_openapi_schema_object_must_be_loaded():
    with pytest.raises(TypeError, match="loaded"):
        XmlMapping.from_openapi('{"type":"object","xml":{"name":"r"}}')


def test_openapi_custom_dialect_is_rejected():
    schema = {"type": "object", "xml": {"name": "r"}}
    with pytest.raises(XmlMappingError, match="dialect"):
        XmlMapping.from_openapi(schema, dialect="https://example.test/dialect")


def test_openapi_mapping_still_requires_a_valid_instance():
    schema = {"type": "object", "xml": {"name": "r"}, "properties": {"id": {"type": "string"}}}
    mapping = XmlMapping.from_openapi(schema)
    with pytest.raises(XmlMappingError, match="JSON Schema 'type'"):
        from_data({"id": 1}).to_xml(mapping)


def test_openapi_forwards_reference_registry_to_guidance():
    uri = "https://example.test/child.json"
    registry = Registry().with_resource(uri, DRAFT202012.create_resource({"const": 1}))
    mapping = XmlMapping.from_openapi({"$ref": uri}, root_name="r", reference_registry=registry)
    assert from_data(1).to_xml(mapping).root.text_value() == "1"
    with pytest.raises(XmlMappingError, match="JSON Schema 'const'"):
        from_data(2).to_xml(mapping)
