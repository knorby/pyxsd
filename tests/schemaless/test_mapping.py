"""Explicit element-first projection of native JSON into XML."""

from decimal import Decimal

import pytest

from pyxsd.schemaless import (
    MappingRule,
    NodeMapping,
    SchemalessDocument,
    SchemalessElement,
    XmlMapping,
    XmlMappingError,
    from_data,
    from_json,
    text,
)
from pyxsd.validation import ValidationReport

XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def test_element_first_projection_preserves_native_values_as_lexical_xml():
    doc = from_json('{"id":"02138","n":1.20,"ok":true,"items":[1,2],"nil":null}')
    xml = doc.to_xml(XmlMapping.elements("order"))
    id_node, number, boolean, items, nil = xml.root.children
    assert id_node.text_value() == "02138"
    assert number.text_value() == "1.20" and boolean.text_value() == "true"
    assert [item.text_value() for item in items.children] == ["1", "2"]
    assert nil.attribute_lexical("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true"


def test_invalid_xml_names_are_not_sanitized():
    with pytest.raises(XmlMappingError) as caught:
        from_data({"bad name": 1}).to_xml(XmlMapping.elements("root"))
    assert caught.value.report.issues[0].details["json_pointer"] == "/bad name"


def test_root_scalars_arrays_and_null_project_without_an_object_wrapper():
    mapping = XmlMapping.elements("root")

    assert from_data("hi").to_xml(mapping).root.text_value() == "hi"
    assert from_data(7).to_xml(mapping).root.text_value() == "7"
    assert from_data(True).to_xml(mapping).root.text_value() == "true"
    assert from_data(Decimal("1.20")).to_xml(mapping).root.text_value() == "1.20"

    items = from_data([1, {"a": 2}, None]).to_xml(mapping).root
    assert [child._name_ for child in items.children] == ["item", "item", "item"]
    assert items.children[0].text_value() == "1"
    assert items.children[1].children[0]._name_ == "a"
    assert items.children[1].children[0].text_value() == "2"
    assert items.children[2].attribute_lexical(XSI_NIL) == "true"
    assert from_data(None).to_xml(mapping).root.attribute_lexical(XSI_NIL) == "true"


def test_projection_preserves_member_order_and_nests_containers():
    value = {"z": {"b": 1, "a": [{"c": 2}]}, "a": None, "m": 3}
    xml = from_data(value).to_xml(XmlMapping.elements("root"))

    assert [child._name_ for child in xml.root.children] == ["z", "a", "m"]
    nested = xml.root.children[0]
    assert [child._name_ for child in nested.children] == ["b", "a"]
    wrapper = nested.children[1]
    assert [child._name_ for child in wrapper.children] == ["item"]
    assert [child._name_ for child in wrapper.children[0].children] == ["c"]
    assert wrapper.children[0].children[0].text_value() == "2"


@pytest.mark.parametrize(
    ("value", "code", "kind"), [({}, "empty-object", "object"), ([], "empty-array", "array")]
)
def test_empty_containers_warn_with_both_addresses(value, code, kind):
    xml = from_data(value).to_xml(XmlMapping.elements("root"))

    assert xml.root.children == ()
    assert xml.is_valid
    assert (xml.report.error_count, xml.report.warning_count) == (0, 1)
    issue = xml.report.issues[0]
    assert issue.code == code
    assert issue.details == {"json_pointer": "", "xml_path": "/root[1]", "json_kind": kind}


def test_nested_empty_container_warning_names_its_pointer_and_xml_path():
    xml = from_data({"a": {"b": []}}).to_xml(XmlMapping.elements("root"))

    issue = xml.report.issues[0]
    assert issue.code == "empty-array"
    assert issue.details == {
        "json_pointer": "/a/b",
        "xml_path": "/root[1]/a[1]/b[1]",
        "json_kind": "array",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1.20"), "1.20"),
        (Decimal("1E+2"), "1E+2"),
        (1.5, "1.5"),
        (1e300, "1e+300"),
        (-0.0, "-0.0"),
        (0, "0"),
        (False, "false"),
    ],
)
def test_number_and_boolean_text_is_json_compatible(value, expected):
    node = from_data({"n": value}).to_xml(XmlMapping.elements("root")).root.children[0]
    assert node.text_value() == expected


def test_strings_are_escaped_text_not_json_quoted():
    xml = from_data({"s": 'a<b&c>"d"', "empty": ""}).to_xml(XmlMapping.elements("root"))

    assert xml.root.children[0].text_value() == 'a<b&c>"d"'
    assert xml.root.children[1].text_value() == ""
    assert '<s>a&lt;b&amp;c&gt;"d"</s>' in xml.to_string()
    assert "<empty></empty>" in xml.to_string()


@pytest.mark.parametrize(
    ("text", "character"),
    [
        ("a\x00b", "U+0000"),
        ("a\x01b", "U+0001"),
        ("a\ufffeb", "U+FFFE"),
        ("a\ud800b", "U+D800"),
    ],
)
def test_xml_illegal_string_characters_are_fatal(text, character):
    with pytest.raises(XmlMappingError) as caught:
        from_data({"s": text}).to_xml(XmlMapping.elements("root"))

    issue = caught.value.report.issues[0]
    assert issue.code == "invalid-xml-text"
    assert issue.details == {
        "json_pointer": "/s",
        "xml_path": "/root[1]/s[1]",
        "character": character,
    }


def test_illegal_root_string_names_both_addresses():
    with pytest.raises(XmlMappingError) as caught:
        from_data("\x00").to_xml(XmlMapping.elements("root"))

    assert caught.value.report.issues[0].details == {
        "json_pointer": "",
        "xml_path": "/root[1]",
        "character": "U+0000",
    }


def test_valid_xml_characters_survive_text_escaping():
    value = "tab\tline\nreturn\rnext café \ufffd"
    xml = from_data({"s": value}).to_xml(XmlMapping.elements("root"))

    assert xml.root.children[0].text_value() == value
    assert "tab\tline\nreturn&#13;next café \ufffd" in xml.to_string()
    assert xml.to_xdm().root_element.local_name == "root"


def test_null_uses_the_xsi_prefix_and_is_declared_only_when_used():
    with_nil = from_data({"nil": None}).to_xml(XmlMapping.elements("order"))
    text = with_nil.to_string()
    assert text.startswith('<order xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">')
    assert '<nil xsi:nil="true"/>' in text
    assert "xmlns:xsi" not in from_data({"a": 1}).to_xml(XmlMapping.elements("order")).to_string()


def test_explicit_rules_rename_elements_by_pointer():
    mapping = XmlMapping.elements(
        "root",
        rules={
            "": MappingRule(name="top"),
            "/a": MappingRule(name="alpha"),
            "/items": MappingRule(name="list"),
            "/items/0": MappingRule(name="first"),
            "/absent": MappingRule(name="never"),
        },
    )
    xml = from_data({"a": 1, "items": [2, 3]}).to_xml(mapping)

    assert xml.root._name_ == "top"
    assert [child._name_ for child in xml.root.children] == ["alpha", "list"]
    assert [child._name_ for child in xml.root.children[1].children] == ["first", "item"]


def test_rules_address_keys_through_escaped_pointers():
    mapping = XmlMapping.elements(
        "root",
        rules={"/a~1b": MappingRule(name="ab"), "/~0x": MappingRule(name="tx")},
    )
    xml = from_data({"a/b": 1, "~x": 2}).to_xml(mapping)

    assert [child._name_ for child in xml.root.children] == ["ab", "tx"]


def test_invalid_name_diagnostics_escape_the_json_pointer():
    with pytest.raises(XmlMappingError) as caught:
        from_data({"~x": 1}).to_xml(XmlMapping.elements("root"))

    issue = caught.value.report.issues[0]
    assert issue.code == "invalid-xml-name"
    assert issue.details == {"json_pointer": "/~0x", "xml_path": "/root[1]/~x[1]"}


@pytest.mark.parametrize(
    "name",
    ["bad name", "", "p:x", "{urn:x}p:y", "{urn:missing", 'a b=""'],
)
def test_invalid_names_are_rejected_without_sanitization(name):
    mapping = XmlMapping.elements("root", rules={"/a": MappingRule(name=name)})
    with pytest.raises(XmlMappingError) as caught:
        from_data({"a": 1}).to_xml(mapping)

    issue = caught.value.report.issues[0]
    assert issue.code == "invalid-xml-name"
    assert issue.details["json_pointer"] == "/a"


def test_empty_object_key_and_unencodable_name_are_invalid_names():
    with pytest.raises(XmlMappingError) as caught:
        from_data({"": 1}).to_xml(XmlMapping.elements("root"))
    assert caught.value.report.issues[0].details == {
        "json_pointer": "/",
        "xml_path": "/root[1]/[1]",
    }

    with pytest.raises(XmlMappingError) as caught:
        from_data({"\ud800": 1}).to_xml(XmlMapping.elements("root"))
    assert caught.value.report.issues[0].details["json_pointer"] == "/\ud800"


def test_rule_without_a_name_keeps_the_default_element_name():
    mapping = XmlMapping.elements(
        "root",
        rules={"": MappingRule(), "/items": MappingRule(), "/items/1": MappingRule()},
    )
    xml = from_data({"items": [1, 2]}).to_xml(mapping)

    assert xml.root._name_ == "root"
    assert xml.root.children[0]._name_ == "items"
    assert [child._name_ for child in xml.root.children[0].children] == ["item", "item"]


def test_empty_node_mapping_leaves_the_projection_unprojected():
    xml = from_data({"a": 1}).to_xml(XmlMapping.elements("root"), node_mapping=NodeMapping())

    assert type(xml.root) is SchemalessElement
    assert xml.root.children[0].text_value() == "1"


def test_expanded_names_project_with_a_generated_prefix():
    mapping = XmlMapping.elements(
        "{urn:example}order", rules={"/id": MappingRule(name="{urn:x}id")}
    )
    xml = from_data({"id": 1}).to_xml(mapping)

    assert xml.root._name_ == "{urn:example}order"
    assert xml.root.children[0]._name_ == "{urn:x}id"
    assert 'xmlns:ns0="urn:example"' in xml.to_string()
    assert 'xmlns:ns1="urn:x"' in xml.to_string()


XMLNS_NS = "http://www.w3.org/2000/xmlns/"


@pytest.mark.parametrize(
    "name",
    [
        f"{{{XMLNS_NS}}}r",
        "{urn: x}r",
        "{urn:\x00x}r",
        "{urn:\ufffex}r",
        "{urn:foo[bar}r",
    ],
)
def test_invalid_expanded_namespace_names_are_rejected_eagerly(name):
    with pytest.raises(XmlMappingError) as caught:
        XmlMapping.elements(name)

    issue = caught.value.report.issues[0]
    assert issue.code == "invalid-xml-name"
    assert issue.details == {"json_pointer": "", "xml_path": f"/{name}[1]"}


@pytest.mark.parametrize(
    "name",
    [f"{{{XMLNS_NS}}}a", "{urn: x}a", "{urn:\x00x}a", "{urn:foo[bar}a"],
)
def test_rule_namespace_names_are_validated_when_projected(name):
    mapping = XmlMapping.elements("root", rules={"/a": MappingRule(name=name)})
    with pytest.raises(XmlMappingError) as caught:
        from_data({"a": 1}).to_xml(mapping)

    issue = caught.value.report.issues[0]
    assert issue.code == "invalid-xml-name"
    assert issue.details == {"json_pointer": "/a", "xml_path": f"/root[1]/{name}[1]"}


def test_invalid_namespace_name_on_an_absent_rule_stays_lazy():
    mapping = XmlMapping.elements("root", rules={"/absent": MappingRule(name="{urn: x}a")})
    xml = from_data({"a": 1}).to_xml(mapping)

    assert xml.root.children[0]._name_ == "a"


def test_valid_expanded_names_allow_iri_namespace_names():
    xml = from_data({"id": 1}).to_xml(XmlMapping.elements("{urn:café}order"))

    assert xml.root._name_ == "{urn:café}order"
    assert 'xmlns:ns0="urn:café"' in xml.to_string()


@pytest.mark.parametrize("key", ["{urn:x}id", "{urn:x}bad name"])
def test_object_keys_are_local_names_only(key):
    with pytest.raises(XmlMappingError) as caught:
        from_data({key: 1}).to_xml(XmlMapping.elements("root"))

    issue = caught.value.report.issues[0]
    assert issue.code == "invalid-xml-name"
    assert issue.details == {
        "json_pointer": f"/{key}",
        "xml_path": f"/root[1]/{key}[1]",
    }


def test_invalid_root_and_rule_names_raise_mapping_errors():
    with pytest.raises(XmlMappingError) as caught:
        XmlMapping.elements("bad name")
    issue = caught.value.report.issues[0]
    assert issue.code == "invalid-xml-name"
    assert issue.details == {"json_pointer": "", "xml_path": "/bad name[1]"}

    mapping = XmlMapping.elements("root", rules={"/a": MappingRule(name="bad name")})
    with pytest.raises(XmlMappingError) as caught:
        from_data({"a": 1}).to_xml(mapping)
    issue = caught.value.report.issues[0]
    assert issue.code == "invalid-xml-name"
    assert issue.details == {"json_pointer": "/a", "xml_path": "/root[1]/bad name[1]"}

    with pytest.raises(XmlMappingError) as caught:
        from_data({}).to_xml(XmlMapping.elements("root", rules={"": MappingRule(name="bad name")}))
    assert caught.value.report.issues[0].details["json_pointer"] == ""


@pytest.mark.parametrize("pointer", ["a", "/a~2", "/a~"])
def test_invalid_rule_pointers_are_rejected_at_construction(pointer):
    with pytest.raises(XmlMappingError) as caught:
        XmlMapping("root", rules={pointer: MappingRule()})

    issue = caught.value.report.issues[0]
    assert issue.code == "invalid-json-pointer"
    assert issue.details["json_pointer"] == pointer


def test_mapping_argument_types_and_defaults_are_explicit():
    mapping = XmlMapping.elements("root")
    assert (mapping.root_name, mapping.default, dict(mapping.rules)) == ("root", "elements", {})
    with pytest.raises(AttributeError):
        mapping.root_name = "other"

    with pytest.raises(TypeError, match="root_name must be a string"):
        XmlMapping(1)
    with pytest.raises(TypeError, match="mapping rule pointers must be strings"):
        XmlMapping("root", rules={1: MappingRule()})
    with pytest.raises(TypeError, match="mapping rule values must be MappingRule"):
        XmlMapping("root", rules={"": "rename"})
    with pytest.raises(TypeError, match="rules must be a mapping"):
        XmlMapping("root", rules=[])
    with pytest.raises(TypeError, match="mapping rule name must be a string"):
        MappingRule(name=1)
    with pytest.raises(ValueError, match="only 'elements' is available"):
        XmlMapping("root", default="compact")


def test_mapping_error_exposes_its_report_and_has_a_fallback_message():
    report = ValidationReport()
    error = XmlMappingError(report)

    assert error.report is report
    assert str(error) == "the JSON value cannot be projected into XML"


def test_optional_node_mapping_projects_classes_and_descriptor_pins():
    class ItemView:
        __slots__ = ()

        number = text(type="xs:integer")

        def label(self):
            return "item"

    nodes = NodeMapping()
    nodes.register(ItemView, path="/root/items/item")
    xml = from_data({"items": [1, 2]}).to_xml(XmlMapping.elements("root"), node_mapping=nodes)

    wrapper = xml.root.children[0]
    assert all(isinstance(item, ItemView) for item in wrapper.children)
    assert type(wrapper.children[0]) is type(wrapper.children[1])
    assert wrapper.children[0].number == 1
    assert wrapper.children[0].scalar_value() == 1
    assert wrapper.children[0].label() == "item"


def test_node_and_mapping_arguments_are_checked_before_projecting():
    mapping = XmlMapping.elements("root")
    with pytest.raises(TypeError, match="mapping must be an XmlMapping"):
        from_data(1).to_xml("root")
    with pytest.raises(TypeError, match="node_mapping must be a NodeMapping"):
        from_data(1).to_xml(mapping, node_mapping=object())


def test_projection_is_a_fresh_document_without_source_evidence():
    source = from_json('{"a":1}')
    xml = source.to_xml(XmlMapping.elements("root"))

    assert isinstance(xml, SchemalessDocument)
    assert xml.schema is None and xml.source is None and xml.source_index is None
    assert xml.report_current and not xml.root.revision.dirty
    assert xml.report.diagnostics_complete is True
    assert xml.to_string() == "<root><a>1</a></root>"
    assert xml.to_xdm().root_element.local_name == "root"
    assert source.to_json() == '{"a": 1}'


def test_json_pointer_pins_do_not_flow_into_projected_xml():
    source = from_json('{"n":1.20}')
    source.pin_type("/n", "xs:integer")

    xml = source.to_xml(XmlMapping.elements("root"))

    assert dict(xml.root.revision.types.pins) == {}
    assert xml.root.children[0].text_value() == "1.20"
    assert xml.root.children[0].scalar_value() == "1.20"


def test_projection_is_deterministic_across_documents():
    value = {"b": [1, 2], "a": {"d": None, "c": True}}
    mapping = XmlMapping.elements("root")

    assert (
        from_data(value).to_xml(mapping).to_string() == from_data(value).to_xml(mapping).to_string()
    )
