"""RELAX NG model registries: finite route models without instance XML."""

from __future__ import annotations

import io
import subprocess
import sys
from typing import get_args

import pytest

pydantic = pytest.importorskip("pydantic")
from integration_helpers import RECORD_SCHEMA, compile_schema  # noqa: E402
from pydantic import BaseModel, RootModel  # noqa: E402
from pyxsd.exceptions import ValidationError  # noqa: E402
from pyxsd.integrations import IntegrationError  # noqa: E402
from pyxsd.integrations.pydantic import ModelSet, models  # noqa: E402
from pyxsd.relaxng.projection import ContentContract  # noqa: E402
from pyxsd.relaxng.pydantic_adapter import (  # noqa: E402
    RelaxNGModelSet,
    _child_content,
    _content_cannot_match,
    _content_may_be_empty,
    _flat_interleave,
    _route_layout,
)
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


def test_models_before_instance_xml_and_scalar_validation():
    schema = compile_element("<text/>")
    registry = models(schema)
    assert isinstance(registry, RelaxNGModelSet)
    Root = registry.model_for(element="doc")
    assert issubclass(Root, RootModel)
    assert Root is registry.model_for(element="doc")
    # A unique text contract returns its lexical string and rejects other shapes.
    assert Root.model_validate("text").root == "text"
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate(42)
    # Text content is nullable: None is real empty content, never an invented "".
    assert Root.model_validate(None).root is None
    route = root_route(schema)
    description = Root.model_json_schema()
    assert description["x-pyxsd-route"] == route.route_id
    assert description["x-pyxsd-scope"] == "start"
    assert description["x-pyxsd-element"] == "doc"
    assert description["x-pyxsd-layout"] == "scalar"
    assert description["x-pyxsd-cardinality"] == {"minimum": 0, "maximum": 1}
    assert Root.__relaxng_layout__ == "scalar"
    assert Root.__relaxng_route_id__ == route.route_id
    assert Root.__relaxng_route__ is route
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
        {"@version": "7", "single": "a", "need": ["x", None]},
    ):
        with pytest.raises(pydantic.ValidationError):
            Root.model_validate(invalid)
    assert Root.model_validate(model) == model
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate("not a record")

    description = Root.model_json_schema()
    properties = description["properties"]
    assert description["x-pyxsd-scope"] == "start"
    assert properties["@version"]["x-pyxsd-cardinality"] == {"minimum": 1, "maximum": 1}
    assert properties["@version"]["x-pyxsd-contracts"][0]["kind"] == "text"
    assert "default" not in properties["@class"]
    assert properties["@class"]["x-pyxsd-cardinality"] == {"minimum": 0, "maximum": 1}
    assert properties["single"]["x-pyxsd-cardinality"] == {"minimum": 1, "maximum": 1}
    assert "default" not in properties["maybe"]
    assert properties["many"]["minItems"] == 0
    assert properties["need"]["minItems"] == 1
    assert properties["many"]["x-pyxsd-cardinality"] == {"minimum": 0, "maximum": None}
    assert properties["many"]["x-pyxsd-child-route"]
    assert set(description["required"]) == {"@version", "single", "need"}
    assert Root.model_fields["many"].default_factory is list
    assert Root.model_fields["need"].is_required()
    assert Root.model_fields["maybe"].is_required() is False
    assert Root.model_fields["maybe"].default is None
    # A record child rejects explicit None even though its default is None.
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({"@version": "7", "single": "a", "maybe": None, "need": ["x"]})


def test_child_names_cannot_shadow_the_presence_validator():
    schema = compile_element(
        '<element name="relaxng_children"><text/></element>'
        '<element name="relaxng-children"><text/></element>'
        '<element name="sibling"><text/></element>'
        '<zeroOrMore><element name="many"><text/></element></zeroOrMore>'
    )
    Root = models(schema).model_for(element="doc")
    assert "relaxng_children" in Root.model_fields
    assert "relaxng_children_2" in Root.model_fields
    assert Root.model_fields["relaxng_children"].alias == "relaxng_children"
    assert Root.model_fields["relaxng_children_2"].alias == "relaxng-children"
    assert "__pyxsd_relaxng_child_presence__" not in Root.model_fields

    by_alias = Root.model_validate(
        {
            "relaxng_children": "plain",
            "relaxng-children": "dashed",
            "sibling": "s",
            "many": ["m"],
        }
    )
    assert by_alias.relaxng_children.root == "plain"
    assert by_alias.relaxng_children_2.root == "dashed"
    assert by_alias.many[0].root == "m"
    by_name = Root.model_validate(
        {
            "relaxng_children": "plain",
            "relaxng_children_2": "dashed",
            "sibling": "s",
        }
    )
    assert by_name.relaxng_children_2.root == "dashed"

    invalid_values = (
        {"relaxng_children": None, "relaxng-children": "dashed", "sibling": "s"},
        {"relaxng_children": "plain", "relaxng-children": None, "sibling": "s"},
        {"relaxng_children": "plain", "relaxng_children_2": None, "sibling": "s"},
        {"relaxng_children": "plain", "relaxng-children": "dashed", "sibling": None},
        {
            "relaxng_children": "plain",
            "relaxng-children": "dashed",
            "sibling": "s",
            "many": [None],
        },
    )
    for data in invalid_values:
        with pytest.raises(pydantic.ValidationError):
            Root.model_validate(data)


def test_namespaced_alias_and_field_name_are_both_checked_for_null():
    schema = compile_element(
        '<element name="item" ns="urn:x"><text/></element><element name="sibling"><text/></element>'
    )
    Root = models(schema).model_for(element="doc")
    assert Root.model_fields["item"].alias == "{urn:x}item"
    assert Root.model_validate({"{urn:x}item": "value", "sibling": "s"}).item.root == "value"
    assert Root.model_validate({"item": "value", "sibling": "s"}).item.root == "value"
    for data in (
        {"{urn:x}item": None, "sibling": "s"},
        {"item": None, "sibling": "s"},
    ):
        with pytest.raises(pydantic.ValidationError):
            Root.model_validate(data)


def test_scalar_presence_tracks_content_emptiness():
    optional = models(
        compile_element(
            f'<optional><data type="integer" datatypeLibrary="{XSD}"/></optional>'
            '<optional><attribute name="lang"><text/></attribute></optional>'
        )
    ).model_for(element="doc")
    assert optional.__relaxng_layout__ == "scalar"
    assert optional.model_validate({}).xml_value is None
    empty = optional.model_validate({"@lang": "en"})
    assert empty.xml_value is None
    assert empty.attr_lang == "en"
    assert optional.model_validate({"@lang": "en", "$": "7"}).xml_value == 7
    assert optional.model_validate({"@lang": "en", "$": None}).xml_value is None
    optional_schema = optional.model_json_schema()
    assert "$" not in optional_schema.get("required", [])
    assert optional_schema["properties"]["$"]["x-pyxsd-cardinality"] == {
        "minimum": 0,
        "maximum": 1,
    }
    assert "default" not in optional_schema["properties"]["$"]
    assert optional.model_fields["xml_value"].is_required() is False

    text = models(compile_element("<text/>")).model_for(element="doc")
    assert text.__relaxng_layout__ == "scalar"
    assert text.model_validate(None).root is None
    assert text.model_json_schema()["x-pyxsd-cardinality"] == {
        "minimum": 0,
        "maximum": 1,
    }
    text_attribute = models(
        compile_element('<text/><attribute name="lang"><text/></attribute>')
    ).model_for(element="doc")
    assert text_attribute.model_validate({"@lang": "en"}).xml_value is None

    required = models(compile_element(f'<data type="integer" datatypeLibrary="{XSD}"/>')).model_for(
        element="doc"
    )
    assert required.__relaxng_layout__ == "scalar"
    with pytest.raises(pydantic.ValidationError):
        required.model_validate(None)
    assert required.model_validate("7").root == 7
    assert required.model_json_schema()["x-pyxsd-cardinality"] == {
        "minimum": 1,
        "maximum": 1,
    }

    required_attribute = models(
        compile_element(
            f'<data type="integer" datatypeLibrary="{XSD}"/>'
            '<attribute name="lang"><text/></attribute>'
        )
    ).model_for(element="doc")
    with pytest.raises(pydantic.ValidationError):
        required_attribute.model_validate({"@lang": "en"})
    with pytest.raises(pydantic.ValidationError):
        required_attribute.model_validate({"@lang": "en", "$": None})
    required_schema = required_attribute.model_json_schema()
    assert "$" in required_schema["required"]
    assert required_schema["properties"]["$"]["x-pyxsd-cardinality"] == {
        "minimum": 1,
        "maximum": 1,
    }


@pytest.mark.parametrize(
    ("body", "layout"),
    [
        (
            '<element name="a"><text/></element><element name="b"><text/></element>',
            "ordered",
        ),
        (
            '<zeroOrMore><element name="a"><text/></element></zeroOrMore>'
            '<oneOrMore><element name="b"><text/></element></oneOrMore>',
            "ordered",
        ),
        (
            '<interleave><zeroOrMore><element name="a"><text/></element></zeroOrMore>'
            '<element name="b"><text/></element></interleave>',
            "interleaved",
        ),
        (
            '<interleave><element name="a"><text/></element>'
            '<element name="b"><text/></element></interleave>',
            "record",
        ),
        (
            '<interleave><optional><element name="a"><text/></element></optional>'
            '<optional><element name="b"><text/></element></optional></interleave>',
            "record",
        ),
        (
            '<interleave><group><element name="a"><text/></element>'
            '<element name="b"><text/></element></group>'
            '<element name="c"><text/></element></interleave>',
            "interleaved",
        ),
        ('<interleave><element name="a"><text/></element></interleave>', "ordered"),
        (
            '<choice><element name="a"><text/></element>'
            '<element name="b"><text/></element></choice>',
            "choice",
        ),
        ('<zeroOrMore><element name="a"><text/></element></zeroOrMore>', "repeated"),
    ],
)
def test_route_layout_metadata_matches_the_compiled_route(body, layout):
    schema = compile_element(body)
    route = root_route(schema)
    Root = models(schema).model_for(element="doc")
    assert Root.__relaxng_route__ is route
    assert Root.__relaxng_route_id__ == route.route_id
    assert Root.__relaxng_layout__ == layout
    assert Root.__relaxng_scope__ == "start"
    assert Root.__relaxng_start__ is True
    assert Root.__relaxng_element__ == "doc"
    assert Root.__relaxng_input_order__ == "unordered"
    description = Root.model_json_schema()
    assert description["x-pyxsd-layout"] == layout
    assert description["x-pyxsd-route"] == route.route_id
    assert description["x-pyxsd-input-order"] == "unordered"


def test_choice_route_is_accepted_at_class_time_with_its_retained_constraint():
    schema = compile_element(
        '<choice><element name="a"><text/></element><element name="b"><text/></element></choice>'
    )
    Root = models(schema).model_for(element="doc")
    assert Root.__relaxng_layout__ == "choice"
    assert Root.__relaxng_route__.constraints
    assert Root.__relaxng_route__.constraints[0].kind == "choice"
    assert Root.model_validate({"a": "x"}).a.root == "x"
    assert Root.model_validate({"b": "y"}).b.root == "y"
    # Task 1 keeps the unordered class permissive; Task 3 uses the retained
    # route constraint to reject this jointly impossible populated combination.
    both = Root.model_validate({"a": "x", "b": "y"})
    assert both.a.root == "x"
    assert both.b.root == "y"


def test_child_resolved_before_parent_reuses_the_cached_child_class():
    schema = compile_element('<element name="item"><text/></element>')
    registry = models(schema)
    Item = registry.model_for(element="doc", path=("item",))
    Root = registry.model_for(element="doc")
    assert Root.model_fields["item"].annotation is Item
    assert Item.__relaxng_scope__ == "local"
    assert Item.__relaxng_start__ is False
    assert Root.model_validate({"item": "value"}).item.root == "value"


def test_repeated_child_minimum_above_one_preserves_bounds():
    schema = compile_element(
        '<element name="item"><text/></element>'
        '<element name="item"><text/></element>'
        '<optional><element name="item"><text/></element></optional>'
    )
    Root = models(schema).model_for(element="doc")
    field = Root.model_fields["item"]
    assert field.json_schema_extra["x-pyxsd-cardinality"] == {
        "minimum": 2,
        "maximum": 3,
    }
    description = Root.model_json_schema()
    assert description["properties"]["item"]["minItems"] == 2
    assert description["properties"]["item"]["maxItems"] == 3
    assert len(Root.model_validate({"item": ["a", "b"]}).item) == 2
    assert len(Root.model_validate({"item": ["a", "b", "c"]}).item) == 3
    for values in (["a"], ["a", "b", "c", "d"]):
        with pytest.raises(pydantic.ValidationError):
            Root.model_validate({"item": values})


def test_wildcard_starts_resolve_by_their_admitted_names():
    any_name = compile_grammar("<start><element><anyName/><empty/></element></start>")
    AnyRoot = models(any_name).model_for(element="foo")
    assert AnyRoot.__relaxng_element__ is None
    assert AnyRoot.__relaxng_layout__ == "empty"
    assert AnyRoot.__relaxng_scope__ == "start"
    assert AnyRoot.model_validate({}).model_dump() == {}

    ns_name = compile_grammar('<start><element><nsName ns="urn:any"/><empty/></element></start>')
    registry = models(ns_name)
    with pytest.raises(IntegrationError, match="no admitted start route"):
        registry.model_for(element="foo")
    NsRoot = registry.model_for(element="{urn:any}foo")
    assert NsRoot is registry.model_for(element="Q{urn:any}foo")
    assert NsRoot.__relaxng_element__ is None
    assert NsRoot.__relaxng_layout__ == "empty"


def test_layout_and_empty_helpers_cover_condition_arms():
    mixed = compile_element(
        '<mixed><zeroOrMore><element name="em"><text/></element></zeroOrMore></mixed>'
    )
    assert _route_layout(root_route(mixed)) == "generic"

    generic_loss = compile_element(
        '<element name="bad"><choice>'
        f'<data type="integer" datatypeLibrary="{XSD}"/>'
        '<element name="nested"><empty/></element></choice></element>'
        '<element name="good"><text/></element>'
    )
    bad = generic_loss.projection.routes[root_route(generic_loss).children["bad"].child_route_id]
    assert bad.generic_reason is not None
    assert _route_layout(bad) == "generic"

    child_wildcard = compile_element(
        "<zeroOrMore><element><anyName/><empty/></element></zeroOrMore>"
    )
    assert _route_layout(root_route(child_wildcard)) == "generic"

    attribute_wildcard = compile_element(
        "<zeroOrMore><attribute><anyName/></attribute></zeroOrMore>"
        '<element name="visible"><text/></element>'
    )
    assert _route_layout(root_route(attribute_wildcard)) == "generic"

    repeated_group = compile_element(
        '<oneOrMore><element name="a"><text/></element>'
        '<element name="b"><text/></element></oneOrMore>'
    )
    assert _route_layout(root_route(repeated_group)) == "repeated"

    not_allowed = compile_element("<notAllowed/>")
    assert _route_layout(root_route(not_allowed)) == "generic"
    assert _content_may_be_empty(root_route(not_allowed).content.model) is False
    assert _content_cannot_match(root_route(not_allowed).content.model) is True

    empty = ContentContract("empty")
    child = ContentContract("child")
    shared_group = ContentContract("group", (empty, empty))
    assert _content_may_be_empty(shared_group) is True
    assert _child_content(shared_group) is None
    assert _content_may_be_empty(ContentContract("unknown-kind")) is False
    for kind in ("attribute", "child", "element"):
        assert _content_may_be_empty(ContentContract(kind)) is True
    assert (
        _content_may_be_empty(ContentContract("group", (empty, ContentContract("notAllowed"))))
        is False
    )
    assert _content_may_be_empty(ContentContract("interleave", (empty, empty))) is True
    assert (
        _content_may_be_empty(ContentContract("choice", (empty, ContentContract("notAllowed"))))
        is True
    )
    assert (
        _content_may_be_empty(ContentContract("choice", (ContentContract("notAllowed"),))) is False
    )
    assert _content_may_be_empty(ContentContract("repeat", (empty,))) is True
    assert (
        _content_may_be_empty(ContentContract("repeat", (ContentContract("notAllowed"),))) is False
    )
    assert _child_content(ContentContract("attribute")) is None
    assert _child_content(ContentContract("group", (child,))).kind == "child"
    assert _child_content(ContentContract("group", (child, child))).kind == "group"

    not_allowed_contract = ContentContract("notAllowed")
    repeat_child = ContentContract("repeat", (child,))
    group_child = ContentContract("group", (child, child))
    assert _flat_interleave(child) is True
    assert _flat_interleave(empty) is True
    assert _flat_interleave(ContentContract("choice", (empty, child))) is True
    assert _flat_interleave(ContentContract("choice", (group_child, child))) is False
    assert _flat_interleave(group_child) is False
    assert _flat_interleave(ContentContract("interleave", (child, repeat_child))) is False
    assert (
        _flat_interleave(
            ContentContract("interleave", (child, ContentContract("interleave", (child, child))))
        )
        is True
    )
    for kind in ("empty", "attribute", "child", "element", "scalar"):
        assert _content_cannot_match(ContentContract(kind)) is False
    assert _content_cannot_match(not_allowed_contract) is True
    assert _content_cannot_match(ContentContract("group", (empty, not_allowed_contract))) is True
    assert (
        _content_cannot_match(ContentContract("interleave", (empty, not_allowed_contract))) is True
    )
    assert _content_cannot_match(ContentContract("choice", (not_allowed_contract, empty))) is False
    assert (
        _content_cannot_match(
            ContentContract("choice", (not_allowed_contract, not_allowed_contract))
        )
        is True
    )
    assert _content_cannot_match(ContentContract("repeat", (not_allowed_contract,))) is True
    assert _content_cannot_match(ContentContract("repeat", (empty,))) is False
    assert _content_cannot_match(ContentContract("unknown-kind")) is False


def test_not_allowed_route_is_rejected_instead_of_building_an_empty_model():
    schema = compile_element("<notAllowed/>")
    with pytest.raises(IntegrationError, match="notAllowed") as error:
        models(schema).model_for(element="doc")
    assert root_route(schema).route_id in str(error.value)


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
    for element in (
        "",
        "{urn:test",
        "{urn:test}",
        "{urn:test}a{b",
        "{urn:test}a}b",
        "p:doc",
        "doc}",
        4,
    ):
        with pytest.raises(IntegrationError):
            registry.model_for(element=element)  # type: ignore[arg-type]
    for path in (["a"], ("",), (4,)):
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


def test_route_metadata_is_attached_at_model_level():
    scalar_schema = compile_element(f'<data type="integer" datatypeLibrary="{XSD}"/>')
    Scalar = models(scalar_schema).model_for(element="doc")
    scalar_route = root_route(scalar_schema)
    scalar_description = Scalar.model_json_schema()
    assert scalar_description["x-pyxsd-route"] == scalar_route.route_id
    assert scalar_description["x-pyxsd-scope"] == "start"
    assert scalar_description["x-pyxsd-element"] == "doc"
    assert scalar_description["x-pyxsd-layout"] == "scalar"
    assert scalar_description["x-pyxsd-cardinality"] == {"minimum": 1, "maximum": 1}
    assert scalar_description["x-pyxsd-input-order"] == "unordered"
    assert "start route" in scalar_description["x-pyxsd-validation"]

    record_schema = compile_element('<element name="child"><text/></element>')
    Record = models(record_schema).model_for(element="doc")
    record_description = Record.model_json_schema()
    assert record_description["x-pyxsd-route"] == root_route(record_schema).route_id
    assert record_description["x-pyxsd-scope"] == "start"
    assert record_description["x-pyxsd-element"] == "doc"
    assert record_description["x-pyxsd-layout"] == Record.__relaxng_layout__
    child_definition = next(iter(record_description["$defs"].values()))
    assert child_definition["x-pyxsd-route"]
    assert child_definition["x-pyxsd-scope"] == "local"
    assert child_definition["x-pyxsd-element"] == "child"

    recursive_schema = compile_grammar(
        """
        <start><ref name="node"/></start>
        <define name="node"><element name="node">
          <optional><attribute name="label"><text/></attribute></optional>
          <zeroOrMore><ref name="node"/></zeroOrMore>
        </element></define>
        """
    )
    Node = models(recursive_schema).model_for(element="node")
    recursive_description = model_schema(Node)
    assert recursive_description["x-pyxsd-route"] == root_route(recursive_schema).route_id
    assert recursive_description["x-pyxsd-scope"] == "start"
    assert recursive_description["x-pyxsd-element"] == "node"
    assert recursive_description["x-pyxsd-layout"] == Node.__relaxng_layout__


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
    assert Node.__relaxng_layout__ == "repeated"
    assert Node.__relaxng_route_id__ == root_route(schema).route_id
    assert Node.__relaxng_route__ is root_route(schema)
    assert model_schema(Node)["x-pyxsd-scope"] == "start"
    assert model_schema(Node)["x-pyxsd-layout"] == "repeated"
    assert len(schema.projection.routes) == 1


def test_recursive_schema_metadata_is_deterministic_and_not_a_field():
    schema = compile_grammar(
        """
        <start><ref name="node"/></start>
        <define name="node"><element name="node">
          <optional><attribute name="label"><text/></attribute></optional>
          <zeroOrMore><ref name="node"/></zeroOrMore>
        </element></define>
        """
    )
    first = models(schema).model_for(element="node")
    second = models(schema).model_for(element="node")
    assert first is not second
    assert first.__relaxng_route_id__ == second.__relaxng_route_id__
    assert first.__relaxng_layout__ == second.__relaxng_layout__ == "repeated"
    assert first.model_json_schema() == second.model_json_schema()
    description = model_schema(first)
    assert description["x-pyxsd-layout"] == "repeated"
    assert description["x-pyxsd-route"] == root_route(schema).route_id
    assert all(not name.startswith("__relaxng") for name in first.model_fields)
    assert not any(key.startswith("__relaxng") for key in description.get("properties", {}))


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
    assert A.__relaxng_layout__ == "ordered"
    assert B.__relaxng_layout__ == "ordered"
    assert Node is registry.model_for(element="doc", path=("b", "node"))
    assert Node.__relaxng_layout__ == "choice"
    Next = registry.model_for(element="doc", path=("a", "node", "next"))
    assert Next.__relaxng_layout__ == "ordered"
    assert Node.__relaxng_route_id__ != Next.__relaxng_route_id__
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
    bad = schema.projection.routes[root_route(schema).children["bad"].child_route_id]
    with pytest.raises(IntegrationError, match=r"bad.*generic") as error:
        registry.model_for(element="doc", path=("bad",))
    assert bad.route_id in str(error.value)
    with pytest.raises(IntegrationError, match="whole-element scalar") as error:
        registry.model_for(element="doc")
    assert bad.route_id in str(error.value)
    Good = registry.model_for(element="doc", path=("good",))
    assert Good.model_json_schema()["x-pyxsd-scope"] == "local"
    assert Good.model_validate("ok").root == "ok"


def test_mixed_route_reports_lost_text_child_order():
    schema = compile_element(
        '<mixed><zeroOrMore><element name="em"><text/></element></zeroOrMore></mixed>'
    )
    with pytest.raises(IntegrationError, match="mixed content") as error:
        models(schema).model_for(element="doc")
    assert root_route(schema).route_id in str(error.value)


def test_wildcard_roles_report_lost_arbitrary_names():
    child_wildcard = compile_element(
        '<element name="known"><text/></element>'
        "<zeroOrMore><element><anyName><except><name>known</name></except></anyName>"
        "<text/></element></zeroOrMore>"
    )
    with pytest.raises(IntegrationError, match="child wildcard") as error:
        models(child_wildcard).model_for(element="doc")
    assert root_route(child_wildcard).route_id in str(error.value)

    attribute_wildcard = compile_element(
        "<zeroOrMore><attribute><anyName/></attribute></zeroOrMore>"
    )
    with pytest.raises(IntegrationError, match="attribute wildcard") as error:
        models(attribute_wildcard).model_for(element="doc")
    assert root_route(attribute_wildcard).route_id in str(error.value)
    # An attribute wildcard is not a child view when resolving a path step.
    with pytest.raises(IntegrationError, match="not admitted"):
        models(attribute_wildcard).model_for(element="doc", path=("anything",))

    both = compile_element(
        "<zeroOrMore><attribute><anyName/></attribute></zeroOrMore>"
        "<zeroOrMore><element><anyName/><empty/></element></zeroOrMore>"
    )
    with pytest.raises(IntegrationError, match="attribute and child wildcard") as error:
        models(both).model_for(element="doc")
    assert root_route(both).route_id in str(error.value)


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
    assert Root.__relaxng_scope__ == "start"
    assert Item.__relaxng_scope__ == "local"
    assert Item.__relaxng_start__ is False
    assert Item.__relaxng_route_id__ == item_route.route_id
    assert Item.__relaxng_layout__ == "scalar"


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


def test_clean_field_schema_removes_only_the_internal_none_default():
    from pyxsd.integrations.pydantic import clean_field_schema

    real_default = {"type": "integer", "default": 0}
    clean_field_schema(real_default)
    assert real_default == {"type": "integer", "default": 0}

    nullable_placeholder = {"type": "string", "default": None}
    clean_field_schema(nullable_placeholder)
    assert nullable_placeholder == {"type": "string"}


def test_xsd_models_path_does_not_import_the_relaxng_adapter():
    script = """
import sys
from io import StringIO
import pyxsd
from pyxsd.integrations.pydantic import ModelSet, models

schema = pyxsd.compile(
    StringIO(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="r" type="xs:int"/></xs:schema>'
    )
)
registry = models(schema)
assert isinstance(registry, ModelSet)
assert registry.model_for(element="r").model_validate(7).root == 7
assert "pyxsd.relaxng.pydantic_adapter" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


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
