"""Only library-built imports may bypass policy parsing, not policy budgets."""

import io
from xml.etree import ElementTree as ET

import pytest

from pyxsd import Schema
from pyxsd.namespaces import parse_with_namespaces
from pyxsd.relaxng import RelaxNGSchema
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.xml_policy import XmlLimits, XmlTreeLimitError, enforce_tree_limits

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:anyType"/>
</xs:schema>"""
RNG = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start><ref name="node"/></start>
  <define name="node"><element><anyName/><zeroOrMore><choice>
    <attribute><anyName/></attribute><text/><ref name="node"/>
  </choice></zeroOrMore></element></define>
</grammar>"""


class TreePolicy:
    def __init__(self, limits):
        self.tree_limits = limits

    def read(self, source, *, base, purpose):
        raise ResourcePolicyError("instance reads denied")

    def parse(self, data, context):
        return parse_with_namespaces(io.BytesIO(data.content), context)


@pytest.fixture(params=[(Schema, XSD), (RelaxNGSchema, RNG)], ids=["xsd", "rng"])
def policy_schema(request):
    schema_type, source = request.param

    def compile(limits):
        schema = schema_type.compile(io.StringIO(source), resource_policy=TreePolicy(limits))
        assert not schema.report.has_errors
        return schema

    return compile


@pytest.mark.parametrize(
    "limits,data,message",
    [
        (XmlLimits(max_elements=8), ["root", *[["child"] for _ in range(10)]], "max_elements=8"),
        (XmlLimits(max_depth=2), ["root", ["child", ["leaf"]]], "max_depth=2"),
    ],
)
def test_import_enforces_policy_tree_limits(policy_schema, limits, data, message):
    schema = policy_schema(limits)
    with pytest.raises(ResourcePolicyError, match=f"the XML input exceeds {message}"):
        schema.from_data(data, format="jsonml")


@pytest.mark.parametrize(
    "limits,data",
    [
        (XmlLimits(max_depth=1, max_elements=1, max_bytes=1), ["root"]),
        (XmlLimits(max_depth=2, max_elements=3), ["root", ["a"], ["b"]]),
        (XmlLimits(), ["root", ["a", ["b"]]]),
    ],
)
def test_import_tree_limits_are_inclusive_and_ignore_byte_budget(policy_schema, limits, data):
    schema = policy_schema(limits)
    schema.from_data(data, format="jsonml").require_valid()


def test_import_does_not_read_or_reparse_inert_resource_syntax(policy_schema, monkeypatch):
    schema = policy_schema(XmlLimits(max_depth=2, max_elements=2))
    text = '<!DOCTYPE root SYSTEM "file:///not-admitted">&external;'
    data = [
        "root",
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": "urn:example file:///not-admitted.xsd",
            "xsi:noNamespaceSchemaLocation": "https://not-admitted.example/schema",
            "xml:base": "file:///not-admitted/",
        },
        text,
    ]

    def forbidden(*args, **kwargs):
        pytest.fail("library-built imports must not open or reparse resources")

    policy = schema.resource_policy if isinstance(schema, Schema) else schema._resource_policy
    monkeypatch.setattr(policy, "read", forbidden)
    monkeypatch.setattr(policy, "parse", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    document = schema.from_data(data, format="jsonml")
    document.require_valid()
    assert text in str(document.to_data(format="jsonml"))


def test_policy_still_rejects_caller_elements_and_instance_reads(policy_schema):
    schema = policy_schema(XmlLimits(max_elements=8))
    with pytest.raises(ResourcePolicyError, match="unverified Element"):
        schema.parse(ET.Element("root"))
    with pytest.raises(ResourcePolicyError, match="instance reads denied"):
        schema.parse("file:///not-admitted.xml")


def test_tree_limit_check_is_iterative_and_does_not_modify_the_tree():
    root = ET.Element("root")
    node = root
    for _ in range(1500):
        node = ET.SubElement(node, "child")
    enforce_tree_limits(root, XmlLimits(max_depth=1501, max_elements=1501))
    assert len(list(root.iter())) == 1501
    with pytest.raises(ResourcePolicyError, match="max_depth=1500"):
        enforce_tree_limits(root, XmlLimits(max_depth=1500))


@pytest.mark.parametrize("make_node", [ET.Comment, ET.ProcessingInstruction])
def test_shared_tree_limits_can_count_only_start_tags(make_node):
    root = ET.Element("root")
    node = make_node("note")
    root.append(node)
    limits = XmlLimits(max_depth=1, max_elements=1)

    # Schema imports retain the default: both nodes count, and depth wins
    # when the same node breaches both limits.
    with pytest.raises(XmlTreeLimitError, match=r"^the XML input exceeds max_depth=1$"):
        enforce_tree_limits(root, limits)
    with pytest.raises(XmlTreeLimitError, match=r"^the XML input exceeds max_elements=1$"):
        enforce_tree_limits(root, XmlLimits(max_elements=1))

    enforce_tree_limits(root, limits, count_non_element_nodes=False)
    assert list(root) == [node]

    ET.SubElement(root, "child")
    with pytest.raises(XmlTreeLimitError, match=r"^the XML input exceeds max_depth=1$"):
        enforce_tree_limits(root, limits, count_non_element_nodes=False)


def test_schema_imports_keep_counting_non_element_nodes(policy_schema, monkeypatch):
    from pyxsd.data_conversion import import_data
    from pyxsd.relaxng import json_profile

    schema = policy_schema(XmlLimits(max_elements=1))
    if isinstance(schema, Schema):
        convert = import_data.data_to_element

        def append_comment(*args, **kwargs):
            root = convert(*args, **kwargs)
            root.append(ET.Comment("note"))
            return root

        monkeypatch.setattr(import_data, "data_to_element", append_comment)
    else:
        convert_rng = json_profile.data_to_relaxng_tree

        def append_comment_rng(*args, **kwargs):
            tree = convert_rng(*args, **kwargs)
            tree.root.append(ET.Comment("note"))
            return tree

        monkeypatch.setattr(json_profile, "data_to_relaxng_tree", append_comment_rng)

    with pytest.raises(XmlTreeLimitError, match=r"^the XML input exceeds max_elements=1$"):
        schema.from_data(["root"], format="jsonml")


def test_policy_import_preserves_relaxng_scalar_contexts(monkeypatch):
    source = """<element xmlns="http://relaxng.org/ns/structure/1.0"
      name="root" datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes">
      <choice><data type="QName"/><data type="string"/></choice>
    </element>"""
    original = RelaxNGSchema.compile(io.StringIO(source)).parse(
        io.StringIO('<root xmlns:p="urn:actual">p:value</root>'), typed=True
    )
    data = original.to_data(format="pyxsd-json")
    policy = TreePolicy(XmlLimits(max_elements=1, max_depth=1))
    schema = RelaxNGSchema.compile(io.StringIO(source), resource_policy=policy)

    def forbidden(*args, **kwargs):
        pytest.fail("imported scalar contexts must not be reparsed")

    monkeypatch.setattr(policy, "parse", forbidden)
    document = schema.from_data(data, format="pyxsd-json", namespaces={"p": "urn:wrong"})
    document.require_valid()
    assert document.to_data(format="pyxsd-json") == data
