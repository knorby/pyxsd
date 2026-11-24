"""Explicit JSON-to-XML rule resolution, diagnostics, and lexical output."""

from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest

from pyxsd._lexical import clone_lexical, serialize_lexical
from pyxsd.locations import DataLocation
from pyxsd.schemaless import MappingRule, ScalarAdapter, XmlMapping, XmlMappingError, from_data
from pyxsd.xdm.annotations import UNTYPED, UNTYPED_ATOMIC

XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
XML_NS = "http://www.w3.org/XML/1998/namespace"


def test_compact_and_explicit_rules_control_node_kinds():
    mapping = XmlMapping.compact(
        "order",
        rules={
            "/note": MappingRule(kind="element"),
            "/lines": MappingRule(array="wrapped", item_name="line"),
        },
    )
    xml = from_data({"id": 7, "note": "x", "lines": [1]}).to_xml(mapping)
    assert xml.root.attribute_lexical("id") == "7"
    assert xml.root.children[0]._name_ == "note"
    assert xml.root.children[1].children[0]._name_ == "line"


def test_null_attribute_warning_retains_pointer():
    mapping = XmlMapping("r", rules={"/x": MappingRule(kind="attribute")})
    doc = from_data({"x": None}).to_xml(mapping)
    assert "x" not in doc.root.attributes
    issue = doc.report.issues[0]
    assert issue.code == "null-attribute-omitted"
    assert issue.details["json_pointer"] == "/x"


def test_compact_defaults_apply_only_to_scalar_object_properties():
    mapping = XmlMapping.compact("r", rules={"/obj/a": MappingRule(name="renamed")})
    native = {"obj": {"a": False}, "list": [True, {"b": 2}], "nil": None}
    xml = from_data(native).to_xml(mapping)
    assert mapping.default == "compact"
    assert (
        xml.to_string()
        == '<r><obj renamed="false"/><list><item>true</item><item b="2"/></list></r>'
    )
    assert xml.report.issues[0].details == {"json_pointer": "/nil", "xml_path": "/r[1]/@nil"}


def test_none_inlines_children_with_order_and_preserves_their_original_pointers():
    rules = {
        "": MappingRule(order=("missing", "flat", "right")),
        "/flat": MappingRule(kind="none", order=("marker", "last")),
        "/flat/marker": MappingRule(kind="attribute"),
        "/flat/first": MappingRule(name="last"),
        "/other": MappingRule(kind="none", item_name="row"),
        "/other/1": MappingRule(name="second"),
    }
    native = {"left": 1, "right": 2, "flat": {"first": 3, "last": 4, "marker": 5}, "other": [6, 7]}
    doc = from_data(native).to_xml(XmlMapping("r", rules=rules))
    assert doc.to_string() == (
        '<r marker="5"><last>4</last><last>3</last><right>2</right>'
        "<left>1</left><row>6</row><second>7</second></r>"
    )


def test_none_on_scalar_omits_it_with_a_loss_warning():
    doc = from_data({"a/b": 1}).to_xml(XmlMapping("r", rules={"/a~1b": MappingRule(kind="none")}))
    assert doc.to_string() == "<r/>"
    issue = doc.report.issues[0]
    assert issue.code == "unmapped-scalar"
    assert issue.location == DataLocation("<memory>", "/a~1b")
    assert issue.details == {"json_pointer": "/a~1b", "xml_path": "/r[1]"}


def test_order_is_copied_and_unlisted_properties_keep_input_order():
    order = ["c", "b"]
    rules = {"": MappingRule(order=order)}
    order.reverse()
    doc = from_data({"a": 1, "b": 2, "c": 3, "d": 4}).to_xml(XmlMapping("r", rules=rules))
    assert [child._name_ for child in doc.root.children] == ["c", "b", "a", "d"]


def test_text_rules_allow_attributes_and_element_separated_mixed_content():
    rules = {
        "/lead": MappingRule(kind="text"),
        "/a": MappingRule(kind="attribute"),
        "/last": MappingRule(kind="text"),
    }
    doc = from_data({"lead": "pre", "a": 42, "x": 1, "last": "post"}).to_xml(
        XmlMapping("r", rules=rules)
    )
    assert doc.root.text_value() == "pre"
    assert doc.to_string() == '<r a="42">pre<x>1</x>post</r>'


def test_adjacent_text_across_an_attribute_and_an_inlined_object_is_ambiguous():
    rules = {
        "/a": MappingRule(kind="text"),
        "/attr": MappingRule(kind="attribute"),
        "/inline": MappingRule(kind="none"),
        "/inline/b": MappingRule(kind="text"),
    }
    with pytest.raises(XmlMappingError) as caught:
        from_data({"a": "first", "attr": 1, "inline": {"b": "second"}}).to_xml(
            XmlMapping("r", rules=rules)
        )
    issue = caught.value.report.issues[0]
    assert issue.code == "ambiguous-text"
    assert issue.location == DataLocation("<memory>", "/inline/b")
    assert issue.details == {"json_pointer": "/inline/b", "xml_path": "/r[1]/text()"}


def test_null_rules_apply_only_to_nulls_and_record_losses():
    rules = {
        "/gone": MappingRule(null="omit"),
        "/empty": MappingRule(null="empty"),
        "/nil": MappingRule(null="nil"),
        "/still": MappingRule(null="omit"),
        "/empty_attr": MappingRule(kind="attribute", null="empty"),
        "/gone_attr": MappingRule(kind="attribute", null="omit"),
        "/empty_text": MappingRule(kind="text", null="empty"),
        "/gone_text": MappingRule(kind="text", null="omit"),
        "/default_text": MappingRule(kind="text"),
    }
    native = {pointer[1:]: None for pointer in rules}
    native["still"] = True
    doc = from_data(native).to_xml(XmlMapping("r", rules=rules))
    root = ET.fromstring(doc.to_string())
    assert [(child.tag, child.text) for child in root] == [
        ("empty", None),
        ("nil", None),
        ("still", "true"),
    ]
    assert doc.root.children[0].text_value() == ""
    assert root[1].get(XSI_NIL) == "true"
    assert root.attrib == {"empty_attr": ""}
    assert [(issue.code, issue.details["xml_path"]) for issue in doc.report.issues] == [
        ("null-omitted", "/r[1]/gone[1]"),
        ("null-emptied", "/r[1]/empty[1]"),
        ("null-emptied", "/r[1]/@empty_attr"),
        ("null-attribute-omitted", "/r[1]/@gone_attr"),
        ("null-emptied", "/r[1]/text()"),
        ("null-omitted", "/r[1]/text()"),
        ("null-omitted", "/r[1]/text()"),
    ]


def test_root_null_may_be_empty_but_cannot_be_omitted():
    doc = from_data(None).to_xml(XmlMapping("r", rules={"": MappingRule(null="empty")}))
    assert doc.to_string() == "<r></r>"
    assert doc.report.issues[0].details == {"json_pointer": "", "xml_path": "/r[1]"}
    with pytest.raises(XmlMappingError, match="root element cannot be omitted"):
        from_data(None).to_xml(XmlMapping("r", rules={"": MappingRule(null="omit")}))


@pytest.mark.parametrize("first", ["", None])
def test_an_empty_text_rule_does_not_disambiguate_adjacent_properties(first):
    rules = {
        "/x": MappingRule(kind="text", null="empty"),
        "/y": MappingRule(kind="text"),
    }
    with pytest.raises(XmlMappingError) as caught:
        from_data({"x": first, "y": "value"}).to_xml(XmlMapping("r", rules=rules))
    assert caught.value.report.issues[0].details == {
        "json_pointer": "/y",
        "xml_path": "/r[1]/text()",
    }


def test_array_policies_distinguish_wrappers_and_unwrapped_empty_occurrences():
    rules = {
        "/wrapped": MappingRule(name="wrapper", item_name="unused"),
        "/spread": MappingRule(array="unwrapped", item_name="row"),
        "/spread/0": MappingRule(name="first"),
        "/spread/1": MappingRule(null="omit"),
        "/empty": MappingRule(array="unwrapped"),
        "/same": MappingRule(array="unwrapped"),
        "/flat": MappingRule(kind="none"),
    }
    native = {"wrapped": [], "spread": [1, None, 3], "empty": [], "same": [4, 5], "flat": {}}
    doc = from_data(native).to_xml(XmlMapping("r", rules=rules))
    assert (
        doc.to_string()
        == "<r><wrapper/><first>1</first><row>3</row><same>4</same><same>5</same></r>"
    )
    assert [(issue.code, issue.details) for issue in doc.report.issues] == [
        (
            "empty-array",
            {"json_pointer": "/wrapped", "xml_path": "/r[1]/wrapper[1]", "json_kind": "array"},
        ),
        ("null-omitted", {"json_pointer": "/spread/1", "xml_path": "/r[1]/row[1]"}),
        ("empty-array", {"json_pointer": "/empty", "xml_path": "/r[1]", "json_kind": "array"}),
        ("empty-object", {"json_pointer": "/flat", "xml_path": "/r[1]", "json_kind": "object"}),
    ]


def test_element_positions_count_only_emitted_elements_even_when_inlined():
    doc = from_data({"a": [], "inline": {"b": None, "c": {}}}).to_xml(
        XmlMapping(
            "r",
            rules={
                "/a": MappingRule(name="x"),
                "/inline": MappingRule(kind="none"),
                "/inline/b": MappingRule(name="x", null="omit"),
                "/inline/c": MappingRule(name="{}x"),
            },
        )
    )
    assert doc.to_string() == "<r><x/><x/></r>"
    assert [issue.details["xml_path"] for issue in doc.report.issues] == [
        "/r[1]/x[1]",
        "/r[1]/x[2]",
        "/r[1]/x[2]",
        "/r[1]/x[2]",
    ]
    assert doc.report.issues[2].code == "repeated-element-name"
    assert doc.report.issues[2].details["json_pointer"] == "/inline/c"


def test_nullable_container_policies_leave_null_handling_to_the_null_rule():
    rules = {
        "/rows": MappingRule(array="wrapped", item_name="row", null="nil"),
        "/obj": MappingRule(order=("a",), null="omit"),
    }
    doc = from_data({"rows": None, "obj": None}).to_xml(XmlMapping("r", rules=rules))
    root = ET.fromstring(doc.to_string())
    assert len(root) == 1 and root[0].tag == "rows" and root[0].get(XSI_NIL) == "true"
    assert doc.report.issues[0].details["json_pointer"] == "/obj"


@pytest.mark.parametrize("order", ["ab", 1, ["a", 2]])
def test_order_must_be_a_sequence_of_property_names(order):
    with pytest.raises(TypeError, match="order must be a sequence of strings"):
        MappingRule(order=order)


@pytest.mark.parametrize("field", ["namespace", "prefix", "item_name"])
def test_rule_name_components_must_be_strings(field):
    with pytest.raises(TypeError, match=f"{field} must be a string"):
        MappingRule(**{field: 5})


def test_namespaces_use_scoped_lexical_writer_bindings():
    rules = {
        "": MappingRule(namespace="urn:orders", prefix=""),
        "/id": MappingRule(kind="attribute", name="{urn:id}key", namespace="urn:id", prefix="i"),
        "/group": MappingRule(namespace="urn:group", prefix="i"),
        "/group/language": MappingRule(kind="attribute", name=f"{{{XML_NS}}}lang", prefix="xml"),
        "/group/plain/child": MappingRule(name="{urn:group}leaf", prefix="i"),
        "/array": MappingRule(array="unwrapped", namespace="urn:lines", prefix="l", name="line"),
        "/array/1": MappingRule(name="{urn:other}second", prefix="o"),
    }
    native = {"id": "x", "group": {"language": "en", "plain": {"child": 1}}, "array": [2, 3]}
    doc = from_data(native).to_xml(XmlMapping("r", rules=rules))
    assert doc.to_string() == (
        '<r xmlns="urn:orders" xmlns:i="urn:id" i:key="x">'
        '<i:group xmlns:i="urn:group" xml:lang="en">'
        '<plain xmlns=""><i:leaf>1</i:leaf></plain></i:group>'
        '<l:line xmlns:l="urn:lines">2</l:line><o:second xmlns:o="urn:other">3</o:second></r>'
    )
    assert doc.root.children[0].children[0].namespaces[""] == ""


def test_explicit_child_prefix_wins_over_the_inherited_default_namespace():
    rules = {
        "": MappingRule(namespace="urn:x", prefix=""),
        "/child": MappingRule(namespace="urn:x", prefix="p"),
    }
    doc = from_data({"child": 1}).to_xml(XmlMapping("r", rules=rules))

    assert doc.to_string() == '<r xmlns="urn:x"><p:child xmlns:p="urn:x">1</p:child></r>'
    assert doc.source is None and doc.source_index is None
    assert doc.root._name_ == "{urn:x}r"
    assert doc.root.children[0]._name_ == "{urn:x}child"
    root = doc.to_xdm().root_element
    (child,) = root.children
    assert (root.name, root.namespace_uri, root.prefix) == ("{urn:x}r", "urn:x", "")
    assert (child.name, child.namespace_uri, child.prefix) == ("{urn:x}child", "urn:x", "p")
    assert child.annotation.type_name == UNTYPED


def test_nested_prefix_wins_locally_without_changing_unrequested_alias_selection():
    rules = {
        "/a": MappingRule(namespace="urn:x", prefix="p"),
        "/a/b": MappingRule(namespace="urn:x", prefix="q"),
        "/a/b/c": MappingRule(namespace="urn:x"),
    }
    doc = from_data({"a": {"b": {"c": 1}}}).to_xml(XmlMapping("r", rules=rules))
    expected = '<r><p:a xmlns:p="urn:x"><q:b xmlns:q="urn:x"><p:c>1</p:c></q:b></p:a></r>'

    assert doc.to_string() == expected
    assert serialize_lexical(clone_lexical(doc.root)) == expected
    (a,) = doc.root.children
    (b,) = a.children
    (c,) = b.children
    assert [node._name_ for node in (a, b, c)] == ["{urn:x}a", "{urn:x}b", "{urn:x}c"]
    (xdm_a,) = doc.to_xdm().root_element.children
    (xdm_b,) = xdm_a.children
    (xdm_c,) = xdm_b.children
    assert [(node.name, node.namespace_uri, node.prefix) for node in (xdm_a, xdm_b, xdm_c)] == [
        ("{urn:x}a", "urn:x", "p"),
        ("{urn:x}b", "urn:x", "q"),
        ("{urn:x}c", "urn:x", "p"),
    ]


def test_element_and_attribute_with_the_same_expanded_name_can_use_different_prefixes():
    rules = {
        "": MappingRule(namespace="urn:x", prefix="p"),
        "/id": MappingRule(kind="attribute", name="{urn:x}r", prefix="q"),
    }
    doc = from_data({"id": 7}).to_xml(XmlMapping("r", rules=rules))
    doc.root.set_base_uri("https://example.test/")
    doc.root.set_namespace_bindings({"a": "urn:x", "p": "urn:x", "q": "urn:x"})

    assert doc.to_string() == '<p:r xmlns:a="urn:x" xmlns:p="urn:x" xmlns:q="urn:x" q:r="7"/>'
    root = doc.revalidate().to_xdm().root_element
    (attribute,) = root.attributes
    assert (root.name, root.namespace_uri, root.prefix) == ("{urn:x}r", "urn:x", "p")
    assert (attribute.name, attribute.namespace_uri, attribute.prefix) == (
        "{urn:x}r",
        "urn:x",
        "q",
    )
    assert attribute.annotation.type_name == UNTYPED_ATOMIC


def test_rebound_prefix_does_not_change_the_expanded_name_after_a_namespace_edit():
    rules = {"": MappingRule(namespace="urn:x", prefix="p")}
    doc = from_data({}).to_xml(XmlMapping("r", rules=rules))
    doc.root.set_namespace_bindings({"a": "urn:x", "p": "urn:y"})

    assert doc.to_string() == '<a:r xmlns:a="urn:x" xmlns:p="urn:y"/>'
    root = doc.revalidate().to_xdm().root_element
    assert (root.name, root.namespace_uri, root.prefix) == ("{urn:x}r", "urn:x", "a")


def test_nil_still_serializes_when_the_conventional_prefix_is_in_use():
    doc = from_data(None).to_xml(
        XmlMapping("r", rules={"": MappingRule(namespace="urn:r", prefix="xsi")})
    )
    assert doc.to_string().startswith('<xsi:r xmlns:xsi="urn:r"')
    assert ET.fromstring(doc.to_string()).get(XSI_NIL) == "true"


def test_unwrapped_namespaces_apply_to_local_item_names_but_not_clark_item_names():
    rules = {
        "/local": MappingRule(array="unwrapped", namespace="urn:x", prefix="p", item_name="row"),
        "/expanded": MappingRule(
            array="unwrapped", namespace="urn:x", prefix="p", item_name="{urn:y}row"
        ),
    }
    doc = from_data({"local": [1], "expanded": [2]}).to_xml(XmlMapping("r", rules=rules))
    assert doc.root.children[0]._name_ == "{urn:x}row"
    assert doc.root.children[1]._name_ == "{urn:y}row"
    assert doc.to_string() == (
        '<r><p:row xmlns:p="urn:x">1</p:row><ns0:row xmlns:ns0="urn:y">2</ns0:row></r>'
    )


@pytest.mark.parametrize(
    ("rule", "value", "code", "path"),
    [
        ({"kind": "wrong"}, 1, "invalid-mapping-rule", "/r[1]/x[1]"),
        ({"array": "wrong"}, [], "invalid-mapping-rule", "/r[1]/x[1]"),
        ({"null": "wrong"}, None, "invalid-mapping-rule", "/r[1]/x[1]"),
        ({"kind": "attribute"}, {}, "invalid-mapping-rule", "/r[1]/@x"),
        ({"kind": "text"}, [], "invalid-mapping-rule", "/r[1]/text()"),
        ({"kind": "attribute", "null": "nil"}, None, "invalid-mapping-rule", "/r[1]/@x"),
        ({"kind": "text", "name": "s"}, 1, "invalid-mapping-rule", "/r[1]/text()"),
        ({"kind": "none", "namespace": "urn:n"}, {}, "invalid-mapping-rule", "/r[1]"),
        ({"kind": "none", "array": "wrapped"}, [], "invalid-mapping-rule", "/r[1]"),
        ({"kind": "none", "type": "xs:integer"}, 1, "invalid-mapping-rule", "/r[1]"),
        ({"kind": "none", "null": "empty"}, None, "invalid-mapping-rule", "/r[1]"),
        ({"kind": "attribute", "array": "unwrapped"}, 1, "invalid-mapping-rule", "/r[1]/@x"),
        ({"array": "wrapped"}, {}, "invalid-mapping-rule", "/r[1]/x[1]"),
        ({"item_name": "item"}, 1, "invalid-mapping-rule", "/r[1]/x[1]"),
        ({"item_name": "not valid"}, [], "invalid-xml-name", "/r[1]/x[1]/not valid[1]"),
        ({"type": "xs:integer"}, [], "invalid-mapping-rule", "/r[1]/x[1]"),
        ({"order": ("a",)}, 1, "invalid-mapping-rule", "/r[1]/x[1]"),
        ({"order": ("a", "a")}, {}, "invalid-mapping-rule", "/r[1]/x[1]"),
        ({"namespace": "urn: x"}, 1, "invalid-xml-name", "/r[1]/{urn: x}x[1]"),
        (
            {"name": "{urn:a}x", "namespace": "urn:b"},
            1,
            "invalid-mapping-rule",
            "/r[1]/{urn:a}x[1]",
        ),
        ({"prefix": "p"}, 1, "invalid-mapping-rule", "/r[1]/x[1]"),
        ({"namespace": "urn:x", "prefix": "p:q"}, 1, "invalid-mapping-rule", "/r[1]/{urn:x}x[1]"),
        ({"namespace": "urn:x", "prefix": "xmlns"}, 1, "invalid-mapping-rule", "/r[1]/{urn:x}x[1]"),
        ({"namespace": "urn:x", "prefix": "xml"}, 1, "invalid-mapping-rule", "/r[1]/{urn:x}x[1]"),
        (
            {"namespace": XML_NS, "prefix": "p"},
            1,
            "invalid-mapping-rule",
            f"/r[1]/{{{XML_NS}}}x[1]",
        ),
        (
            {"kind": "attribute", "namespace": "urn:x", "prefix": ""},
            1,
            "invalid-mapping-rule",
            "/r[1]/@{urn:x}x",
        ),
        ({"kind": "attribute", "name": "{}xmlns"}, 1, "invalid-xml-name", "/r[1]/@xmlns"),
        ({"namespace": "urn:x"}, 1, "invalid-xml-name", "/r[1]/{urn:y}x[1]"),
    ],
)
def test_invalid_rules_have_both_occurrence_addresses(rule, value, code, path):
    key = "{urn:y}x" if path == "/r[1]/{urn:y}x[1]" else "x"
    with pytest.raises(XmlMappingError) as caught:
        from_data({key: value}).to_xml(XmlMapping("r", rules={f"/{key}": MappingRule(**rule)}))
    issue = caught.value.report.issues[0]
    assert issue.code == code
    assert issue.location == DataLocation("<memory>", f"/{key}")
    assert issue.details == {"json_pointer": f"/{key}", "xml_path": path}


@pytest.mark.parametrize(
    ("rule", "value"),
    [
        ({"kind": "none"}, {}),
        ({"kind": "attribute"}, 1),
        ({"kind": "text"}, 1),
        ({"array": "unwrapped"}, []),
    ],
)
def test_incompatible_root_rules_are_rejected(rule, value):
    with pytest.raises(XmlMappingError) as caught:
        from_data(value).to_xml(XmlMapping("r", rules={"": MappingRule(**rule)}))
    assert caught.value.report.issues[0].details == {"json_pointer": "", "xml_path": "/r[1]"}


def test_duplicate_attributes_do_not_overwrite_an_earlier_value():
    rules = {"/a": MappingRule(kind="attribute"), "/b": MappingRule(kind="attribute", name="{}a")}
    with pytest.raises(XmlMappingError) as caught:
        from_data({"a": 1, "b": 2}).to_xml(XmlMapping("r", rules=rules))
    issue = caught.value.report.issues[0]
    assert issue.code == "duplicate-attribute"
    assert issue.details == {"json_pointer": "/b", "xml_path": "/r[1]/@a"}


def test_conflicting_prefixes_on_one_element_do_not_overwrite_a_binding():
    rules = {
        "": MappingRule(namespace="urn:r", prefix="p"),
        "/a": MappingRule(kind="attribute", namespace="urn:a", prefix="p"),
    }
    with pytest.raises(XmlMappingError) as caught:
        from_data({"a": 1}).to_xml(XmlMapping("r", rules=rules))
    issue = caught.value.report.issues[0]
    assert issue.code == "invalid-mapping-rule"
    assert issue.details == {"json_pointer": "/a", "xml_path": "/{urn:r}r[1]/@{urn:a}a"}


def test_rule_types_format_through_the_existing_scalar_adapters_only():
    rules = {
        "/number": MappingRule(kind="attribute", type="xs:integer"),
        "/price": MappingRule(type="xs:decimal"),
        "/value": MappingRule(kind="text", type=ScalarAdapter(int, lambda value: f"[{value}]")),
    }
    source = from_data({"number": "0007", "price": Decimal("1.2E+8"), "value": "04"})
    source.pin_type("/number", "xs:boolean")
    doc = source.to_xml(XmlMapping("r", rules=rules))
    assert doc.to_string() == '<r number="7"><price>120000000</price>[4]</r>'
    assert dict(doc.root.revision.types.pins) == {}
    assert source.value_at("/number") == "0007"
    assert doc.root.attribute_value("number") == "7"


@pytest.mark.parametrize(
    ("rule", "value", "code", "path"),
    [
        ({"type": "xs:does-not-exist"}, "x", "invalid-mapping-type", "/r[1]/x[1]"),
        ({"type": "xs:integer"}, "not a number", "invalid-mapping-type", "/r[1]/x[1]"),
        ({"type": ScalarAdapter(str, int)}, "no", "invalid-mapping-type", "/r[1]/x[1]"),
        ({"type": ScalarAdapter(str, lambda value: 7)}, "x", "invalid-mapping-type", "/r[1]/x[1]"),
        ({"kind": "attribute"}, "\x01", "invalid-xml-text", "/r[1]/@x"),
        ({"kind": "text"}, "\x00", "invalid-xml-text", "/r[1]/text()"),
        (
            {"type": ScalarAdapter(str, lambda value: "clean")},
            "\x00",
            "invalid-xml-text",
            "/r[1]/x[1]",
        ),
        (
            {"type": ScalarAdapter(str, lambda value: "\x00")},
            "clean",
            "invalid-xml-text",
            "/r[1]/x[1]",
        ),
    ],
)
def test_invalid_type_formatting_or_xml_text_reports_the_exact_occurrence(rule, value, code, path):
    with pytest.raises(XmlMappingError) as caught:
        from_data({"x": value}).to_xml(XmlMapping("r", rules={"/x": MappingRule(**rule)}))
    issue = caught.value.report.issues[0]
    assert issue.code == code
    assert (issue.details["json_pointer"], issue.details["xml_path"]) == ("/x", path)
