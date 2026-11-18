"""RELAX NG model registries: finite route models without instance XML."""

from __future__ import annotations

import io
from typing import get_args

import pytest

pydantic = pytest.importorskip("pydantic")
from integration_helpers import RECORD_SCHEMA, compile_schema  # noqa: E402
from pydantic import BaseModel, RootModel  # noqa: E402
from pyxsd.exceptions import ValidationError  # noqa: E402
from pyxsd.integrations import IntegrationError  # noqa: E402
from pyxsd.integrations.pydantic import ModelSet, models  # noqa: E402
from pyxsd.relaxng.pydantic_adapter import RelaxNGModelSet  # noqa: E402
from pyxsd.relaxng.schema import RelaxNGSchema  # noqa: E402

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


def compile_element(body: str, *, name: str = "doc", ns: str | None = None) -> RelaxNGSchema:
    namespace = f' ns="{ns}"' if ns else ""
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="{name}"{namespace}>{body}</element>')
    )
    schema.require_valid()
    return schema


def compile_grammar(body: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(io.StringIO(f'<grammar xmlns="{NS}">{body}</grammar>'))
    schema.require_valid()
    return schema


def root_route(schema: RelaxNGSchema):
    return schema.projection.routes[schema.projection.roots[0]]


def model_schema(model) -> dict:
    """Return the model's own JSON Schema, following Pydantic's recursive wrapper."""
    schema = model.model_json_schema()
    reference = schema.get("$ref")
    if reference is None:
        return schema
    return schema["$defs"][reference.rsplit("/", 1)[-1]]


def test_models_before_instance_xml_and_pure_scalar_placeholder():
    schema = compile_element("<text/>")
    registry = models(schema)
    assert isinstance(registry, RelaxNGModelSet)
    Root = registry.model_for(element="doc")
    assert issubclass(Root, RootModel)
    assert Root is registry.model_for(element="doc")
    # Task 1 deliberately uses an Any placeholder; it does not claim scalar validation.
    assert Root.model_validate("text").root == "text"
    assert Root.model_validate(42).root == 42
    route = root_route(schema)
    description = Root.model_json_schema()
    assert description["x-pyxsd-route"] == route.route_id
    assert description["x-pyxsd-scope"] == "start"
    assert description["x-pyxsd-element"] == "doc"
    assert description["x-pyxsd-contracts"] == [
        {
            "kind": "text",
            "library_uri": None,
            "type_name": None,
            "parameters": [],
            "restrictions": [],
            "pattern_identity": route.content.scalar_contracts[0].pattern_identity,
        }
    ]
    assert route.route_id not in Root.__name__


def test_record_fields_defaults_cardinality_and_strict_input():
    schema = compile_element(
        """
        <attribute name="version"><text/></attribute>
        <optional><attribute name="class"><text/></attribute></optional>
        <element name="single"><text/></element>
        <optional><element name="maybe"><text/></element></optional>
        <zeroOrMore><element name="many"><text/></element></zeroOrMore>
        <oneOrMore><element name="need"><text/></element></oneOrMore>
        """
    )
    registry = models(schema)
    Root = registry.model_for(element="doc")
    assert issubclass(Root, BaseModel) and not issubclass(Root, RootModel)
    assert Root.model_config["extra"] == "forbid"
    assert Root.model_config["strict"] is True
    assert Root.model_config["validate_by_name"] is True

    data = {"@version": "7", "single": "a", "need": ["x"]}
    model = Root.model_validate(data)
    assert model.attr_version == "7"
    assert model.attr_class is None
    assert model.single.root == "a"
    assert model.maybe is None
    assert model.many == []
    assert [item.root for item in model.need] == ["x"]
    assert (
        Root.model_validate({"attr_version": "7", "single": "a", "need": ["x"]}).attr_version == "7"
    )

    for invalid in (
        {"single": "a", "need": ["x"]},
        {"@version": "7", "single": "a", "need": []},
        {"@version": "7", "single": "a", "need": ["x"], "unknown": 1},
        {"@version": "7", "single": "a", "need": "x"},
    ):
        with pytest.raises(pydantic.ValidationError):
            Root.model_validate(invalid)

    description = Root.model_json_schema()
    properties = description["properties"]
    assert description["x-pyxsd-scope"] == "start"
    assert properties["@version"]["x-pyxsd-cardinality"] == {"minimum": 1, "maximum": 1}
    assert properties["@version"]["x-pyxsd-contracts"][0]["kind"] == "text"
    assert properties["@class"]["default"] is None
    assert properties["@class"]["x-pyxsd-cardinality"] == {"minimum": 0, "maximum": 1}
    assert properties["single"]["x-pyxsd-cardinality"] == {"minimum": 1, "maximum": 1}
    assert properties["maybe"]["default"] is None
    assert properties["many"]["minItems"] == 0
    assert properties["need"]["minItems"] == 1
    assert properties["many"]["x-pyxsd-cardinality"] == {"minimum": 0, "maximum": None}
    assert properties["many"]["x-pyxsd-child-route"]
    assert set(description["required"]) == {"@version", "single", "need"}
    assert Root.model_fields["many"].default_factory is list
    assert Root.model_fields["need"].is_required()
    assert Root.model_fields["maybe"].is_required() is False
    assert Root.model_fields["maybe"].default is None


def test_scalar_attributes_share_a_record_and_missing_scalar_is_required():
    schema = compile_element('<text/><attribute name="lang"><text/></attribute>')
    Root = models(schema).model_for(element="doc")
    assert issubclass(Root, BaseModel) and not issubclass(Root, RootModel)
    model = Root.model_validate({"$": "hello", "@lang": "en"})
    assert model.xml_value == "hello"
    assert model.attr_lang == "en"
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({"@lang": "en"})
    description = Root.model_json_schema()
    assert description["properties"]["$"]["x-pyxsd-contracts"][0]["kind"] == "text"
    assert "$" in description["required"]


def test_empty_route_is_a_strict_model_with_no_fields():
    Root = models(compile_element("<empty/>")).model_for(element="doc")
    assert issubclass(Root, BaseModel) and not issubclass(Root, RootModel)
    assert Root.model_fields == {}
    assert Root.model_validate({}).model_dump() == {}
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({"anything": 1})


def test_alias_and_field_name_collisions_are_deterministic():
    schema = compile_element(
        """
        <attribute name="a"><text/></attribute>
        <attribute name="a" ns="urn:other"><text/></attribute>
        <element name="a"><empty/></element>
        <element name="a" ns="urn:other"><empty/></element>
        <element name="a-b"><empty/></element>
        <element name="a_x2d_b"><empty/></element>
        <element name="class"><empty/></element>
        <element name="model_dump"><empty/></element>
        """
    )
    Root = models(schema).model_for(element="doc")
    data = {
        "@a": "1",
        "@{urn:other}a": "2",
        "a": {},
        "{urn:other}a": {},
        "a-b": {},
        "a_x2d_b": {},
        "class": {},
        "model_dump": {},
    }
    model = Root.model_validate(data)
    assert model.attr_a == "1"
    assert model.attr_a_2 == "2"
    assert Root.model_fields["field_class"].alias == "class"
    assert Root.model_fields["field_model_dump"].alias == "model_dump"
    assert Root.model_fields["a"].alias == "a"
    assert Root.model_fields["a_2"].alias == "{urn:other}a"
    assert Root.model_fields["a_b"].alias == "a-b"
    assert Root.model_fields["a_x2d_b"].alias == "a_x2d_b"
    assert model.model_dump(by_alias=True) == data
    aliases = [field.alias for field in Root.model_fields.values()]
    assert len(set(aliases)) == len(aliases)


def test_route_resolution_accepts_local_clark_and_eqname():
    schema = compile_element(
        '<element name="a"><text/></element><element name="a" ns="urn:other"><text/></element>',
        ns="urn:test",
    )
    registry = models(schema)
    Local = registry.model_for(element="doc")
    Clark = registry.model_for(element="{urn:test}doc")
    EqName = registry.model_for(element="Q{urn:test}doc")
    assert Local is Clark is EqName
    A = registry.model_for(element="doc", path=("{urn:test}a",))
    Other = registry.model_for(element="doc", path=("{urn:other}a",))
    OtherEq = registry.model_for(element="doc", path=("Q{urn:other}a",))
    assert Other is OtherEq
    assert A is not Other
    assert issubclass(A, RootModel) and issubclass(Other, RootModel)
    assert Local.model_fields["a"].annotation is Other
    assert Local.model_fields["a_2"].annotation is A
    assert A is registry.model_for(element="{urn:test}doc", path=("{urn:test}a",))


def test_route_resolution_reports_unknown_and_malformed_names():
    schema = compile_element(
        '<element name="a"><text/></element><element name="a" ns="urn:other"><text/></element>'
    )
    registry = models(schema)
    with pytest.raises(IntegrationError, match=r"\('doc', 'missing'\)"):
        registry.model_for(element="doc", path=("missing",))
    with pytest.raises(IntegrationError, match="not admitted"):
        registry.model_for(element="doc", path=("missing",))
    with pytest.raises(IntegrationError, match="no admitted start route"):
        registry.model_for(element="{urn:other}doc")
    for element in ("", "{urn:test", "p:doc", "doc}", 4):
        with pytest.raises(IntegrationError):
            registry.model_for(element=element)  # type: ignore[arg-type]
    for path in (["a"], ("",)):
        with pytest.raises(IntegrationError):
            registry.model_for(element="doc", path=path)  # type: ignore[arg-type]


def test_ambiguous_local_start_name_requires_namespace():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<grammar xmlns="{NS}"><start><choice>'
            '<element name="doc" ns="urn:a"><empty/></element>'
            '<element name="doc" ns="urn:b"><empty/></element>'
            "</choice></start></grammar>"
        )
    )
    schema.require_valid()
    registry = models(schema)
    with pytest.raises(IntegrationError, match="ambiguous"):
        registry.model_for(element="doc")
    assert registry.model_for(element="{urn:a}doc") is not registry.model_for(element="{urn:b}doc")


def test_unique_child_wildcard_resolves_a_local_route_without_parent_claims():
    schema = compile_element(
        "<zeroOrMore><element><anyName/>"
        '<attribute name="open"><text/></attribute></element></zeroOrMore>'
    )
    registry = models(schema)
    with pytest.raises(IntegrationError, match="child wildcard"):
        registry.model_for(element="doc")
    Child = registry.model_for(element="doc", path=("{urn:any}anything",))
    model = Child.model_validate({"@open": "value"})
    assert model.attr_open == "value"
    description = Child.model_json_schema()
    assert description["x-pyxsd-scope"] == "local"
    assert description["x-pyxsd-element"] is None
    assert "ancestor" in description["x-pyxsd-validation"]


def test_recursive_route_uses_finite_forward_references():
    schema = compile_grammar(
        """
        <start><ref name="node"/></start>
        <define name="node"><element name="node">
          <optional><attribute name="label"><text/></attribute></optional>
          <zeroOrMore><ref name="node"/></zeroOrMore>
        </element></define>
        """
    )
    registry = models(schema)
    Node = registry.model_for(element="node")
    assert Node is registry.model_for(element="node", path=("node", "node"))
    annotation = Node.model_fields["node"].annotation
    assert get_args(annotation) == (Node,)
    assert Node.model_fields["node"].default_factory is list
    model = Node.model_validate({"@label": "root", "node": [{"node": []}]})
    assert model.attr_label == "root"
    assert model.node[0].node == []
    assert model_schema(Node)["x-pyxsd-scope"] == "start"
    assert len(schema.projection.routes) == 1


def test_multiple_paths_reuse_finite_recursive_classes():
    schema = compile_grammar(
        """
        <start><element name="doc">
          <optional><element name="a"><ref name="node"/></element></optional>
          <zeroOrMore><element name="b"><ref name="node"/></element></zeroOrMore>
        </element></start>
        <define name="node"><element name="node">
          <zeroOrMore><choice>
            <element name="next"><ref name="node"/></element>
            <element name="leaf"><text/></element>
          </choice></zeroOrMore>
        </element></define>
        """
    )
    registry = models(schema)
    Doc = registry.model_for(element="doc")
    A = registry.model_for(element="doc", path=("a",))
    B = registry.model_for(element="doc", path=("b",))
    Node = registry.model_for(element="doc", path=("a", "node"))
    assert A is not B
    assert Node is registry.model_for(element="doc", path=("b", "node"))
    Next = registry.model_for(element="doc", path=("a", "node", "next"))
    assert Doc.model_fields["a"].annotation is A
    assert get_args(Doc.model_fields["b"].annotation) == (B,)
    assert A.model_fields["node"].annotation is Node
    assert B.model_fields["node"].annotation is Node
    assert get_args(Node.model_fields["next"].annotation) == (Next,)
    assert Next.model_fields["node"].annotation is Node
    data = {
        "a": {"node": {"next": [{"node": {"leaf": ["x"]}}]}},
        "b": [{"node": {"leaf": ["y"]}}],
    }
    model = Doc.model_validate(data)
    assert model.a.node.next[0].node.leaf[0].root == "x"
    assert model.b[0].node.leaf[0].root == "y"


def test_generic_route_reports_lost_information():
    schema = compile_element(
        '<element name="bad"><choice>'
        f'<data type="integer" datatypeLibrary="{XSD}"/>'
        '<element name="nested"><empty/></element></choice></element>'
        '<element name="good"><text/></element>'
    )
    registry = models(schema)
    with pytest.raises(IntegrationError, match=r"bad.*generic"):
        registry.model_for(element="doc", path=("bad",))
    with pytest.raises(IntegrationError, match="whole-element scalar"):
        registry.model_for(element="doc")
    Good = registry.model_for(element="doc", path=("good",))
    assert Good.model_json_schema()["x-pyxsd-scope"] == "local"
    assert Good.model_validate("ok").root == "ok"


def test_mixed_route_reports_lost_text_child_order():
    schema = compile_element(
        '<mixed><zeroOrMore><element name="em"><text/></element></zeroOrMore></mixed>'
    )
    with pytest.raises(IntegrationError, match="mixed content"):
        models(schema).model_for(element="doc")


def test_wildcard_roles_report_lost_arbitrary_names():
    child_wildcard = compile_element(
        '<element name="known"><text/></element>'
        "<zeroOrMore><element><anyName><except><name>known</name></except></anyName>"
        "<text/></element></zeroOrMore>"
    )
    with pytest.raises(IntegrationError, match="child wildcard"):
        models(child_wildcard).model_for(element="doc")

    attribute_wildcard = compile_element(
        "<zeroOrMore><attribute><anyName/></attribute></zeroOrMore>"
    )
    with pytest.raises(IntegrationError, match="attribute wildcard"):
        models(attribute_wildcard).model_for(element="doc")
    # An attribute wildcard is not a child view when resolving a path step.
    with pytest.raises(IntegrationError, match="not admitted"):
        models(attribute_wildcard).model_for(element="doc", path=("anything",))

    both = compile_element(
        "<zeroOrMore><attribute><anyName/></attribute></zeroOrMore>"
        "<zeroOrMore><element><anyName/><empty/></element></zeroOrMore>"
    )
    with pytest.raises(IntegrationError, match="attribute and child wildcard"):
        models(both).model_for(element="doc")


def test_start_and_local_scope_metadata_never_claims_ancestor_validation():
    schema = compile_element('<element name="item"><text/></element>')
    registry = models(schema)
    Root = registry.model_for(element="doc")
    Item = registry.model_for(element="doc", path=("item",))
    route = root_route(schema)
    item_route = schema.projection.routes[route.children["item"].child_route_id]
    root_description = Root.model_json_schema()
    item_description = Item.model_json_schema()
    assert root_description["x-pyxsd-scope"] == "start"
    assert root_description["x-pyxsd-route"] == route.route_id
    assert item_description["x-pyxsd-scope"] == "local"
    assert item_description["x-pyxsd-route"] == item_route.route_id
    assert "ancestor" in item_description["x-pyxsd-validation"]
    assert "sibling" in item_description["x-pyxsd-validation"]
    assert "start route" in root_description["x-pyxsd-validation"]


def test_equivalent_derivations_share_one_class():
    schema = compile_element(
        "<choice><element name='a'><text/></element><element name='a'><text/></element></choice>"
    )
    registry = models(schema)
    Root = registry.model_for(element="doc")
    A = registry.model_for(element="doc", path=("a",))
    assert A is registry.model_for(element="doc", path=("a",))
    assert Root.model_fields["a"].annotation is A
    assert len(schema.projection.routes) == 2


def test_json_schema_and_class_names_are_deterministic_across_registries():
    schema = compile_element(
        '<attribute name="a"><text/></attribute><element name="item"><text/></element>'
    )
    first = models(schema)
    second = models(schema)
    Root = first.model_for(element="doc")
    SameShape = second.model_for(element="doc")
    assert Root is not SameShape
    assert Root.__name__ == SameShape.__name__ == "RelaxNGModel_doc"
    assert Root.model_json_schema() == SameShape.model_json_schema()
    assert Root.model_json_schema() == Root.model_json_schema()
    assert all("pyxsd.relaxng.route/1:" not in cls.__name__ for cls in first._models.values())


def test_registry_requires_a_valid_relaxng_schema_and_exposes_only_models():
    invalid = RelaxNGSchema.compile(
        io.StringIO(f'<grammar xmlns="{NS}"><start><ref name="missing"/></start></grammar>')
    )
    with pytest.raises(ValidationError):
        models(invalid)
    with pytest.raises(TypeError, match="RelaxNGSchema"):
        RelaxNGModelSet(object())  # type: ignore[arg-type]
    assert not hasattr(RelaxNGModelSet, "from_document")
    assert not hasattr(RelaxNGModelSet, "from_node")
    assert not hasattr(RelaxNGModelSet, "to_document")


def test_xsd_models_dispatch_is_unchanged():
    schema = compile_schema(RECORD_SCHEMA)
    first = models(schema)
    second = models(schema)
    assert isinstance(first, ModelSet)
    assert isinstance(second, ModelSet)
    assert first is not second
    Root = first.model_for(element="root")
    assert Root is first.model_for(element="root")
    assert Root.model_json_schema()["properties"]["count"]["type"] == "integer"
    assert first.model_for(element="root") is not second.model_for(element="root")
    with pytest.raises(TypeError, match="compiled XSD or RELAX NG schema"):
        models(object())  # type: ignore[arg-type]
