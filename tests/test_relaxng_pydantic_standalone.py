"""Standalone RELAX NG route validation and deterministic lexical reconstruction.

Independently supplied model data is translated once into a lexical element
tree using the retained compiled layout.  Native RELAX NG validation of that
tree, against every merged element assignment, is the authority on joint
validity; Pydantic field checks alone never certify a route.
"""

from __future__ import annotations

import io
from copy import deepcopy
from dataclasses import replace

import pytest

pydantic = pytest.importorskip("pydantic")
from integration_helpers import RECORD_SCHEMA, compile_schema  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from pyxsd.integrations import IntegrationError  # noqa: E402
from pyxsd.integrations.pydantic import ModelSet, models  # noqa: E402
from pyxsd.relaxng.contracts import candidate_id  # noqa: E402
from pyxsd.relaxng.datatypes import DatatypeContext  # noqa: E402
from pyxsd.relaxng.json_values import scalar_tag  # noqa: E402
from pyxsd.relaxng.names import AnyName, ExceptName, Name, NameChoice, NsName  # noqa: E402
from pyxsd.relaxng.patterns import Element, NotAllowed  # noqa: E402
from pyxsd.relaxng.projection import (  # noqa: E402
    ContentContract,
    RouteContract,
    _route_id,
)
from pyxsd.relaxng.pydantic_adapter import (  # noqa: E402
    RelaxNGModelSet,
    _encode_scalar_value,
    _merge_contexts,
    _route_witness_name,
    _value_contexts,
    route_to_lexical,
    validate_route_data,
)
from pyxsd.relaxng.pydantic_values import (  # noqa: E402
    STANDALONE_REVISION,
    context_from_mapping,
)
from pyxsd.relaxng.schema import RelaxNGSchema  # noqa: E402
from pyxsd.relaxng.values import AlternativeValue, Candidate  # noqa: E402
from pyxsd.xsd_data_types import QName, qname_context  # noqa: E402

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


class PlainModel(pydantic.BaseModel):
    value: int = 0


def compile_element(
    body: str,
    *,
    name: str = "doc",
    ns: str | None = None,
    datatypes: bool = True,
) -> RelaxNGSchema:
    namespace = f' ns="{ns}"' if ns else ""
    library = f' datatypeLibrary="{XSD}"' if datatypes else ""
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="{name}"{namespace}{library}>{body}</element>')
    )
    schema.require_valid()
    return schema


def compile_grammar(body: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(io.StringIO(f'<grammar xmlns="{NS}">{body}</grammar>'))
    schema.require_valid()
    return schema


def registry_and_route(body: str, *, name: str = "doc") -> tuple[RelaxNGModelSet, RouteContract]:
    schema = compile_element(body, name=name)
    registry = models(schema)
    route = schema.projection.routes[schema.projection.roots[0]]
    return registry, route


def outline(root) -> list[tuple[str, str | None]]:
    return [(child.tag, child.text) for child in root]


def message(error: BaseException) -> str:
    if isinstance(error, pydantic.ValidationError):
        return error.errors()[0]["msg"]
    return str(error)


def test_route_patterns_are_the_exact_registered_element_assignments():
    schema = compile_grammar(
        "<start><choice>"
        '<element name="doc"><element name="a"><text/></element></element>'
        '<element name="doc"><optional><attribute name="v"><text/></attribute>'
        '</optional><element name="a"><text/></element></element>'
        "</choice></start>"
    )
    plan = schema.projection
    route = plan.routes[plan.roots[0]]
    assert isinstance(route.patterns, tuple)
    assert len(route.patterns) == 2
    assert len(route.occurrence.assignment_ids) == 2
    assert all(isinstance(pattern, Element) for pattern in route.patterns)
    assert len({id(pattern) for pattern in route.patterns}) == 2
    assert [pattern.name_class for pattern in route.patterns] == [
        route.assignments[ordinal].name_class for ordinal in route.occurrence.assignment_ids
    ]
    # The immutable field participates in equality but never in route identity.
    assert route.route_id == _route_id(route.occurrence)
    assert replace(route) == route
    assert replace(route, patterns=()) != route
    assert _route_id(replace(route, patterns=()).occurrence) == route.route_id
    assert plan.grammar_fingerprint.startswith("pyxsd.relaxng.projection-grammar/1:")


def test_standalone_record_required_optional_repeat_and_direct_construction():
    registry, route = registry_and_route(
        """
        <attribute name="version"><text/></attribute>
        <optional><attribute name="lang"><text/></attribute></optional>
        <element name="single"><text/></element>
        <optional><element name="maybe"><text/></element></optional>
        <zeroOrMore><element name="many"><text/></element></zeroOrMore>
        <oneOrMore><element name="need"><text/></element></oneOrMore>
        """
    )
    Root = registry.model_for(element="doc")
    assert Root is registry.model_for(element="doc")
    data = {
        "@version": "7",
        "single": "a",
        "many": ["m1", "m2"],
        "need": ["x"],
    }
    model = Root.model_validate(data)
    assert model.attr_version == "7"
    assert model.attr_lang is None
    assert model.single.root == "a"
    assert model.maybe is None
    assert [item.root for item in model.many] == ["m1", "m2"]
    assert [item.root for item in model.need] == ["x"]
    direct = Root(**{"@version": "7", "single": "b", "need": ["y"]})
    assert direct.single.root == "b"
    assert model.model_dump(by_alias=True)["single"] == "a"
    assert "valid" not in model.model_dump()

    tree = route_to_lexical(route, model)
    assert tree.root.tag == "doc"
    assert tree.root.attrib == {"version": "7"}
    assert outline(tree.root) == [
        ("single", "a"),
        ("many", "m1"),
        ("many", "m2"),
        ("need", "x"),
    ]

    for invalid in (
        {"single": "a", "need": ["x"]},
        {"@version": "7", "need": ["x"]},
        {"@version": "7", "single": "a"},
        {"@version": "7", "single": "a", "need": []},
        {"@version": "7", "single": "a", "need": ["x"], "unknown": 1},
        {"@version": "7", "single": "a", "need": "x"},
        {"@version": "7", "single": None, "need": ["x"]},
    ):
        with pytest.raises(pydantic.ValidationError):
            Root.model_validate(invalid)


def test_model_validate_json_and_invalid_scalar_coercions():
    registry, route = registry_and_route('<attribute name="n"><data type="integer"/></attribute>')
    Root = registry.model_for(element="doc")
    assert Root.model_validate_json('{"@n": 7}').attr_n == 7
    assert Root.model_validate_json('{"@n": "7"}').attr_n == 7
    assert Root.model_validate({"@n": "7"}).attr_n == 7
    for invalid in (True, 1.5, "not-an-integer", None):
        with pytest.raises(pydantic.ValidationError):
            Root.model_validate({"@n": invalid})
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate_json('{"@n": true}')
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate_json('{"@n": "not-an-integer"}')

    model = Root.model_validate({"@n": 7})
    assert route_to_lexical(route, model).root.attrib == {"n": "7"}
    with pytest.raises(ValueError, match="not a valid scalar value") as error:
        validate_route_data(route, {"@n": "not-an-integer"})
    assert "RELAX NG route" in str(error.value)
    assert "/doc/@n" in str(error.value)
    assert "invalid-scalar" in str(error.value)


def test_exclusive_choices_constants_and_supported_facets():
    registry, route = registry_and_route(
        """
        <choice>
          <group>
            <attribute name="kind"><value>a</value></attribute>
            <element name="a"><text/></element>
          </group>
          <group>
            <attribute name="kind"><value>b</value></attribute>
            <element name="b"><text/></element>
          </group>
        </choice>
        """
    )
    Root = registry.model_for(element="doc")
    a = Root.model_validate({"@kind": "a", "a": "x"})
    b = Root.model_validate({"@kind": "b", "b": "y"})
    assert a.a.root == "x" and a.b is None
    assert b.b.root == "y" and b.a is None
    assert outline(route_to_lexical(route, a).root) == [("a", "x")]
    assert outline(route_to_lexical(route, b).root) == [("b", "y")]

    for invalid in (
        {"@kind": "a", "b": "y"},
        {"@kind": "b", "a": "x"},
        {"@kind": "c", "a": "x"},
        {"@kind": "a", "a": "x", "b": "y"},
    ):
        with pytest.raises(pydantic.ValidationError):
            Root.model_validate(invalid)

    restricted, restricted_route = registry_and_route(
        """
        <attribute name="kind"><value>fixed</value></attribute>
        <element name="n">
          <data type="integer"><param name="minInclusive">1</param></data>
        </element>
        """
    )
    Fixed = restricted.model_for(element="doc")
    value = Fixed.model_validate({"@kind": "fixed", "n": 2})
    assert outline(route_to_lexical(restricted_route, value).root) == [("n", "2")]
    for invalid in (
        {"@kind": "other", "n": 2},
        {"@kind": "fixed", "n": 0},
        {"@kind": "fixed", "n": -1},
    ):
        with pytest.raises(pydantic.ValidationError):
            Fixed.model_validate(invalid)


def test_individually_legal_jointly_invalid_data_is_rejected():
    schema = compile_element(
        '<choice><element name="a"><text/></element><element name="b"><text/></element></choice>'
    )
    registry = models(schema)
    Root = registry.model_for(element="doc")
    A = registry.model_for(element="doc", path=("a",))
    B = registry.model_for(element="doc", path=("b",))
    route = schema.projection.routes[schema.projection.roots[0]]
    assert Root.model_validate({"a": "x"}).a.root == "x"
    assert Root.model_validate({"b": "y"}).b.root == "y"
    # Each projection is legal by itself, but no single native assignment
    # admits both populated children.
    with pytest.raises(pydantic.ValidationError) as error:
        Root.model_validate({"a": "x", "b": "y"})
    assert "RELAX NG route" in message(error.value)
    with pytest.raises(ValueError, match="different compiled alternatives"):
        validate_route_data(
            route,
            {"a": A.model_validate("x"), "b": B.model_validate("y")},
        )


def test_namespaced_aliases_unknown_fields_and_model_metadata_stability():
    schema = compile_grammar(
        "<start>"
        '<element name="doc" ns="urn:d">'
        '<element name="revision" ns="urn:d"><text/></element>'
        '<optional><element name="a-b" ns="urn:d"><text/></element></optional>'
        "</element>"
        "</start>"
    )
    registry = models(schema)
    Root = registry.model_for(element="{urn:d}doc")
    route = schema.projection.routes[schema.projection.roots[0]]
    data = {"{urn:d}revision": "r", "{urn:d}a-b": "v"}
    before_schema = deepcopy(Root.model_json_schema())
    model = Root.model_validate(data)
    assert model.revision.root == "r"
    assert model.a_b.root == "v"
    assert Root.model_validate(model) == model
    assert Root.model_json_schema() == before_schema
    assert Root.__relaxng_route__ is route
    assert Root.__relaxng_route_id__ == route.route_id
    assert Root.__relaxng_layout__ == "ordered"
    assert Root.__relaxng_scope__ == "start"
    assert Root.__relaxng_element__ == "{urn:d}doc"
    assert Root.__relaxng_input_order__ == "unordered"
    assert set(Root.model_fields) == {"revision", "a_b"}
    assert set(model.model_dump()) == {"{urn:d}revision", "{urn:d}a-b"}
    assert "x-pyxsd-route" in Root.model_json_schema()

    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({**data, "unknown": "x"})
    with pytest.raises(ValueError, match="unknown field"):
        validate_route_data(
            route,
            {"{urn:d}revision": model.revision, "{urn:d}a-b": model.a_b, "unknown": "x"},
        )

    tree = route_to_lexical(route, model)
    assert tree.root.tag == "{urn:d}doc"
    assert outline(tree.root) == [("{urn:d}revision", "r"), ("{urn:d}a-b", "v")]


def test_ambiguous_wrappers_and_list_assignments():
    registry, route = registry_and_route(
        """
        <attribute name="v">
          <choice><data type="integer"/><data type="string"/></choice>
        </attribute>
        <attribute name="tags">
          <list><zeroOrMore><data type="integer"/></zeroOrMore></list>
        </attribute>
        <attribute name="pair">
          <list><data type="integer"/><data type="string"/></list>
        </attribute>
        """
    )
    Root = registry.model_for(element="doc")
    model = Root.model_validate({"@v": "12", "@tags": [1, 2], "@pair": (3, "x")})
    assert isinstance(model.attr_v, AlternativeValue)
    assert model.attr_v.lexical == "12"
    assert model.attr_tags == [1, 2]
    assert model.attr_pair == (3, "x")
    tree = route_to_lexical(route, model)
    assert tree.root.attrib == {"v": "12", "tags": "1 2", "pair": "3 x"}

    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({"@v": 12, "@tags": [1], "@pair": (3, "x")})
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({"@v": "12", "@tags": [1], "@pair": ["3", "x"]})
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({"@v": "12", "@tags": [1], "@pair": (3, "x", "extra")})
    with pytest.raises(ValueError, match="ambiguous scalar position"):
        validate_route_data(route, {"@v": 12, "@tags": [1], "@pair": (3, "x")})


def test_qname_context_is_scoped_and_caller_mappings_are_not_mutated():
    registry, route = registry_and_route('<attribute name="ref"><data type="QName"/></attribute>')
    Root = registry.model_for(element="doc")
    context = {
        "relaxng": {"namespaces": {"p": "urn:x"}, "base_uri": "urn:base"},
        "caller": {"unchanged": True},
    }
    original = deepcopy(context)
    model = Root.model_validate({"@ref": "p:x"}, context=context)
    assert model.attr_ref.to_expanded_name() == "{urn:x}x"
    assert context == original

    tree = route_to_lexical(route, model, context=context)
    assert tree.root.attrib == {"ref": "p:x"}
    root_context = tree.contexts[id(tree.root)]
    assert root_context == DatatypeContext("urn:base", {"p": "urn:x"})
    assert context == original

    # The decoded QName retains its resolved prefix, so reconstruction can
    # recover the lexical spelling even without a repeated caller context.
    without_context = route_to_lexical(route, model)
    assert without_context.root.attrib == {"ref": "p:x"}
    assert "p" in without_context.contexts[id(without_context.root)].namespaces
    assert without_context.contexts[id(without_context.root)].base_uri is None

    data = {"@ref": "p:x"}
    original_data = deepcopy(data)
    assert validate_route_data(route, data, context=context) == data
    assert data == original_data
    with pytest.raises(ValueError, match="invalid-scalar"):
        validate_route_data(route, data)
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate(data)
    with pytest.raises(ValueError, match="validation context"):
        validate_route_data(route, data, context={"relaxng": {"unexpected": True}})


def test_route_to_lexical_order_repeats_empty_absence_and_determinism():
    registry, route = registry_and_route(
        """
        <element name="b"><text/></element>
        <element name="a"><text/></element>
        <zeroOrMore><element name="many"><text/></element></zeroOrMore>
        <optional><element name="maybe"><text/></element></optional>
        <optional><attribute name="opt"><text/></attribute></optional>
        """
    )
    Root = registry.model_for(element="doc")
    data = {
        "a": "A",
        "b": "B",
        "many": ["m1", "m2"],
        "maybe": "",
        "@opt": "",
    }
    original = deepcopy(data)
    model = Root.model_validate(data)
    assert data == original
    first = route_to_lexical(route, model)
    second = route_to_lexical(route, model)
    assert outline(first.root) == [
        ("b", "B"),
        ("a", "A"),
        ("many", "m1"),
        ("many", "m2"),
        ("maybe", ""),
    ]
    assert first.root.attrib == {"opt": ""}
    assert outline(second.root) == outline(first.root)
    assert second.root.attrib == first.root.attrib
    assert list(first.contexts.values()) == list(second.contexts.values())

    absent = Root.model_validate({"a": "A", "b": "B"})
    absent_tree = route_to_lexical(route, absent)
    assert outline(absent_tree.root) == [("b", "B"), ("a", "A")]
    assert absent_tree.root.attrib == {}
    assert "maybe" not in [name for name, _ in outline(absent_tree.root)]


def test_repeated_group_pairs_indices_and_repeated_choice_emits_every_item():
    registry, route = registry_and_route(
        "<oneOrMore><group>"
        '<element name="a"><text/></element>'
        '<element name="b"><text/></element>'
        "</group></oneOrMore>"
    )
    Root = registry.model_for(element="doc")
    model = Root.model_validate({"a": ["a1", "a2"], "b": ["b1", "b2"]})
    assert outline(route_to_lexical(route, model).root) == [
        ("a", "a1"),
        ("b", "b1"),
        ("a", "a2"),
        ("b", "b2"),
    ]

    choice_registry, choice_route = registry_and_route(
        "<oneOrMore><choice>"
        '<element name="a"><text/></element>'
        '<element name="b"><text/></element>'
        "</choice></oneOrMore>"
    )
    ChoiceRoot = choice_registry.model_for(element="doc")
    choice_model = ChoiceRoot.model_validate({"a": ["a1", "a2"], "b": ["b1"]})
    assert outline(route_to_lexical(choice_route, choice_model).root) == [
        ("a", "a1"),
        ("a", "a2"),
        ("b", "b1"),
    ]


def test_scalar_root_reconstruction_and_mapping_input_copy():
    registry, route = registry_and_route('<data type="integer"/>')
    Root = registry.model_for(element="doc")
    model = Root.model_validate(7)
    tree = route_to_lexical(route, model)
    assert tree.root.tag == "doc"
    assert tree.root.text == "7"
    assert list(tree.root) == []
    data = {"$": "7"}
    original = dict(data)
    assert validate_route_data(route, data) == data
    assert data == original
    assert route_to_lexical(route, data).root.text == "7"

    text_registry, text_route = registry_and_route("<text/>")
    TextRoot = text_registry.model_for(element="doc")
    empty = TextRoot.model_validate(None)
    assert route_to_lexical(text_route, empty).root.text is None
    assert route_to_lexical(text_route, TextRoot.model_validate("")).root.text == ""


def test_open_name_validation_uses_a_witness_but_no_lexical_global_root():
    schema = compile_grammar("<start><element><anyName/><empty/></element></start>")
    AnyRoot = models(schema).model_for(element="anything")
    route = schema.projection.routes[schema.projection.roots[0]]
    assert route.expanded_name is None
    assert AnyRoot.model_validate({}).model_dump() == {}
    assert validate_route_data(route, {}) == {}
    with pytest.raises(IntegrationError, match="open name class"):
        route_to_lexical(route, AnyRoot.model_validate({}))

    ns_schema = compile_grammar('<start><element><nsName ns="urn:any"/><empty/></element></start>')
    NsRoot = models(ns_schema).model_for(element="{urn:any}foo")
    ns_route = ns_schema.projection.routes[ns_schema.projection.roots[0]]
    assert NsRoot.model_validate({}).model_dump() == {}
    assert validate_route_data(ns_route, {}) == {}
    with pytest.raises(IntegrationError, match="open name class"):
        route_to_lexical(ns_route, NsRoot.model_validate({}))


def test_multiple_patterns_accept_any_same_name_assignment():
    schema = compile_grammar(
        "<start><choice>"
        '<element name="doc"><element name="a"><text/></element></element>'
        '<element name="doc"><optional><attribute name="v"><text/></attribute>'
        '</optional><element name="a"><text/></element></element>'
        "</choice></start>"
    )
    plan = schema.projection
    route = plan.routes[plan.roots[0]]
    Root = models(schema).model_for(element="doc")
    assert len(route.patterns) == 2
    plain = Root.model_validate({"a": "plain"})
    tagged = Root.model_validate({"a": "tagged", "@v": "value"})
    assert route_to_lexical(route, plain).root.attrib == {}
    tagged_tree = route_to_lexical(route, tagged)
    assert tagged_tree.root.attrib == {"v": "value"}
    assert outline(tagged_tree.root) == [("a", "tagged")]
    # The first assignment has no v attribute; acceptance therefore came from
    # the second retained pattern.
    assert validate_route_data(route, tagged) == {
        "a": tagged.a,
        "@v": tagged.attr_v,
    }


def test_local_route_validates_only_its_own_content_scope():
    schema = compile_element(
        '<element name="leaf"><element name="inner"><text/></element></element>'
        '<element name="sibling"><text/></element>'
    )
    registry = models(schema)
    Doc = registry.model_for(element="doc")
    Leaf = registry.model_for(element="doc", path=("leaf",))
    Inner = registry.model_for(element="doc", path=("leaf", "inner"))
    Sibling = registry.model_for(element="doc", path=("sibling",))
    route = schema.projection.routes[Leaf.__relaxng_route_id__]
    assert Leaf.__relaxng_scope__ == "local"
    assert all(pattern.name_class.contains(None, "leaf") for pattern in route.patterns)
    inner = Inner.model_validate("ok")
    leaf = Leaf.model_validate({"inner": inner})
    assert leaf.inner.root == "ok"
    assert validate_route_data(route, leaf)["inner"].root == "ok"
    tree = route_to_lexical(route, leaf)
    assert tree.root.tag == "leaf"
    assert outline(tree.root) == [("inner", "ok")]
    # Only the parent route knows about the unrepresented required sibling.
    with pytest.raises(pydantic.ValidationError):
        Doc.model_validate({"leaf": leaf})
    assert (
        Doc.model_validate({"leaf": leaf, "sibling": Sibling.model_validate("x")}).sibling.root
        == "x"
    )


def test_direct_route_validation_reports_native_findings_and_unsupported_shapes():
    registry, route = registry_and_route('<attribute name="n"><data type="integer"/></attribute>')
    Root = registry.model_for(element="doc")
    assert validate_route_data(route, {"@n": "7"}) == {"@n": "7"}
    with pytest.raises(ValueError) as error:
        validate_route_data(route, {"@n": "bad"})
    text = str(error.value)
    assert "RELAX NG route doc" in text
    assert "/doc/@n" in text
    assert "invalid-scalar" in text
    assert "xsd:integer" in text
    with pytest.raises(ValueError, match="unknown field"):
        validate_route_data(route, {"@n": "7", "extra": "x"})
    with pytest.raises(ValueError, match="data must be a mapping"):
        validate_route_data(route, "not-a-mapping")  # type: ignore[arg-type]
    child_registry, child_route = registry_and_route('<element name="child"><text/></element>')
    Child = child_registry.model_for(element="doc", path=("child",))
    with pytest.raises(IntegrationError, match="nested child data"):
        validate_route_data(child_route, {"child": {"$": "x"}})
    assert validate_route_data(child_route, {"child": Child.model_validate("x")})
    other_registry, _other_route = registry_and_route('<attribute name="other"><text/></attribute>')
    Other = other_registry.model_for(element="doc")
    with pytest.raises(IntegrationError, match="does not match requested route"):
        route_to_lexical(route, Other.model_validate({"@other": "x"}))
    with pytest.raises(TypeError):
        validate_route_data(  # type: ignore[arg-type]
            object(), {"@n": "7"}
        )
    with pytest.raises(TypeError, match="RouteContract"):
        route_to_lexical(  # type: ignore[arg-type]
            "not-a-route", Root.model_validate({"@n": 7})
        )
    with pytest.raises(ValueError, match="model data must be a mapping"):
        route_to_lexical(route, "not-a-model")  # type: ignore[arg-type]


def test_reconstruction_never_permutes_alternatives_to_find_an_accepting_branch():
    schema = compile_element(
        "<choice>"
        '<group><optional><element name="a"><text/></element></optional>'
        '<optional><element name="b"><text/></element></optional></group>'
        '<group><optional><element name="b"><text/></element></optional>'
        '<optional><element name="c"><text/></element></optional></group>'
        "</choice>"
    )
    registry = models(schema)
    Root = registry.model_for(element="doc")
    route = schema.projection.routes[schema.projection.roots[0]]
    model = Root.model_validate({"b": "B", "c": "C"})
    assert outline(route_to_lexical(route, model).root) == [("b", "B"), ("c", "C")]
    with pytest.raises(pydantic.ValidationError, match="different compiled alternatives"):
        Root.model_validate({"a": "A", "c": "C"})


def test_wrapper_and_tag_contexts_are_scoped_to_their_element_subtree():
    schema = compile_element(
        """
        <element name="ref">
          <attribute name="q">
            <choice><data type="QName"/><data type="string"/></choice>
          </attribute>
        </element>
        <element name="plain"><text/></element>
        """
    )
    registry = models(schema)
    Root = registry.model_for(element="doc")
    Ref = registry.model_for(element="doc", path=("ref",))
    Plain = registry.model_for(element="doc", path=("plain",))
    ref_route = Ref.__relaxng_route__
    contract = next(iter(ref_route.attributes.values())).scalar_contracts[0]
    snapshot = AlternativeValue(
        "p:x",
        (
            Candidate(
                candidate_id(schema._grammar_fingerprint, contract),
                contract,
                None,
                False,
            ),
        ),
        DatatypeContext(None, {"p": "urn:x"}),
        schema._grammar_fingerprint,
        STANDALONE_REVISION,
    )
    q_field = next(iter(Ref.model_fields))
    ref_model = Ref.model_construct(**{q_field: snapshot})
    plain_model = Plain.model_validate("plain")
    parent = Root.model_construct(ref=ref_model, plain=plain_model)
    route = schema.projection.routes[schema.projection.roots[0]]
    tree = route_to_lexical(route, parent)
    assert tree.root.attrib == {}
    assert [(child.tag, child.attrib) for child in tree.root] == [
        ("ref", {"q": "p:x"}),
        ("plain", {}),
    ]
    root_context = tree.contexts[id(tree.root)]
    ref_context = tree.contexts[id(tree.root[0])]
    plain_context = tree.contexts[id(tree.root[1])]
    assert root_context == DatatypeContext(None, {})
    assert ref_context == DatatypeContext(None, {"p": "urn:x"})
    assert plain_context == DatatypeContext(None, {})

    tag_parent = Root.model_construct(
        ref=Ref.model_construct(**{q_field: scalar_tag(snapshot)}),
        plain=plain_model,
    )
    tag_tree = route_to_lexical(route, tag_parent)
    assert tag_tree.root[0].attrib == {"q": "p:x"}
    assert tag_tree.contexts[id(tag_tree.root[0])] == DatatypeContext(None, {"p": "urn:x"})
    assert tag_tree.contexts[id(tag_tree.root)] == DatatypeContext(None, {})
    assert validate_route_data(route, tag_parent) == {
        "ref": tag_parent.ref,
        "plain": tag_parent.plain,
    }


def test_xsd_models_remain_unchanged():
    schema = compile_schema(RECORD_SCHEMA)
    registry = models(schema)
    assert isinstance(registry, ModelSet)
    Root = registry.model_for(element="root")
    assert not hasattr(Root, "__relaxng_route__")
    model = Root.model_validate({"@version": 9, "count": 1, "item": [2], "flag": True})
    assert model.count == 1
    assert "x-pyxsd-route" not in Root.model_json_schema()


def test_model_class_entry_point_accepts_raw_independent_data():
    registry, route = registry_and_route(
        """
        <attribute name="n"><data type="integer"/></attribute>
        <element name="child"><text/></element>
        """
    )
    Root = registry.model_for(element="doc")
    raw = {"@n": "7", "child": "value"}
    before = deepcopy(raw)
    normalized = validate_route_data(Root, raw)
    assert raw == before
    assert normalized["@n"] == 7
    assert normalized["child"].root == "value"
    assert Root.model_validate(raw).attr_n == 7

    with pytest.raises(IntegrationError, match="not a generated RELAX NG route model"):
        validate_route_data(BaseModel, {})
    with pytest.raises(IntegrationError, match="not a generated RELAX NG route model"):
        route_to_lexical(route, PlainModel())


def test_optional_duplicate_child_does_not_emit_an_absent_leaf():
    registry, route = registry_and_route(
        '<element name="a"><text/></element>'
        '<optional><element name="a"><text/></element></optional>'
    )
    Root = registry.model_for(element="doc")
    model = Root.model_validate({"a": ["only"]})
    assert outline(route_to_lexical(route, model).root) == [("a", "only")]


def test_repeated_child_mapping_requires_a_list_and_names_every_state():
    _registry, route = registry_and_route(
        '<zeroOrMore><element name="many"><text/></element></zeroOrMore>'
    )
    with pytest.raises(ValueError, match="a repeated child requires a list"):
        validate_route_data(route, {"many": "not-a-list"})


def test_duplicate_child_leaves_do_not_repeat_absent_values():
    registry, route = registry_and_route(
        '<element name="a"><text/></element><element name="a"><text/></element>'
    )
    A = registry.model_for(element="doc", path=("a",))
    with pytest.raises(ValueError, match="required content is missing"):
        validate_route_data(route, {"a": [A.model_validate("only")]})


def test_witness_names_cover_exact_choices_exclusions_and_empty_classes():
    _registry, route = registry_and_route("<empty/>")
    named = replace(
        route,
        name_class=NameChoice((Name(None, "left"), Name("urn:x", "right"))),
    )
    assert _route_witness_name(named) == "left"
    excluded = replace(
        route,
        name_class=ExceptName(AnyName(), Name(None, "pyxsd-route")),
    )
    assert _route_witness_name(excluded) == "pyxsd-route_"
    open_choice = replace(
        route,
        name_class=NameChoice((Name(None, "mentioned"), NsName("urn:any"))),
    )
    assert _route_witness_name(open_choice) == "mentioned"
    ns_only = replace(route, name_class=NsName("urn:any"))
    assert _route_witness_name(ns_only) == "{urn:any}pyxsd-route"
    empty = replace(route, name_class=ExceptName(AnyName(), AnyName()))
    with pytest.raises(IntegrationError, match="no deterministic witness"):
        _route_witness_name(empty)


def test_value_context_collection_and_conflict_rejection():
    with qname_context({"p": ""}):
        unresolved_namespace = QName("p:x")
    assert _value_contexts(unresolved_namespace) == ()
    with qname_context({"p": "urn:x"}):
        prefixed = QName("p:x")
    assert _value_contexts(prefixed) == (DatatypeContext(None, {"p": "urn:x"}),)
    with qname_context({"": "urn:d"}):
        defaulted = QName("x")
    assert _value_contexts(defaulted) == (DatatypeContext(None, {"": "urn:d"}),)
    assert _value_contexts(QName("x")) == ()
    assert _value_contexts({}) == ()
    cyclic: list[object] = []
    cyclic.append(cyclic)
    assert _value_contexts(cyclic) == ()
    with pytest.raises(ValueError):
        _value_contexts({"#value": {}})

    with pytest.raises(ValueError, match="conflicting base URIs"):
        _merge_contexts(
            DatatypeContext("urn:a", {}),
            (DatatypeContext("urn:b", {}),),
            path=("doc",),
        )
    with pytest.raises(ValueError, match="conflicting namespace bindings"):
        _merge_contexts(
            DatatypeContext(None, {"p": "urn:a"}),
            (DatatypeContext(None, {"p": "urn:b"}),),
            path=("doc",),
        )
    assert _merge_contexts(
        DatatypeContext("urn:a", {"p": "urn:x"}),
        (
            DatatypeContext("urn:a", {"p": "urn:x"}),
            DatatypeContext("urn:a", {"p": "urn:x"}),
        ),
        path=("doc",),
    ) == DatatypeContext("urn:a", {"p": "urn:x"})
    assert _merge_contexts(
        DatatypeContext(None, {}),
        (DatatypeContext("urn:b", {}),),
        path=("doc",),
    ) == DatatypeContext("urn:b", {})
    assert context_from_mapping(DatatypeContext("urn:a", {"p": "urn:x"})) == DatatypeContext(
        "urn:a", {"p": "urn:x"}
    )


def test_scalar_encoder_configuration_errors_and_text_native_path():
    _registry, route = registry_and_route('<data type="integer"/>')
    contract = route.content.scalar_contracts[0]
    context = DatatypeContext(None, {})
    with pytest.raises(ValueError, match="route path doc/@v"):
        _encode_scalar_value(
            {"#value": {}},
            (contract,),
            context,
            {},
            path=("doc", "@v"),
            normalized=True,
        )
    with pytest.raises(IntegrationError, match="no compiled encoding pattern"):
        _encode_scalar_value(
            7,
            (contract,),
            context,
            {},
            path=("doc", "$"),
            normalized=True,
        )
    patterns = {contract.pattern_identity: route.patterns[0].pattern}
    with pytest.raises(ValueError, match="expected an integer"):
        _encode_scalar_value(
            object(),
            (contract,),
            context,
            patterns,
            path=("doc", "$"),
            normalized=True,
        )
    with pytest.raises(ValueError) as error:
        validate_route_data(route, {"$": "bad"})
    assert "/doc/text()" in str(error.value)
    assert "invalid-scalar" in str(error.value)


def test_unrepresentable_extra_fields_and_unknown_contracts_are_configuration_errors():
    registry, route = registry_and_route('<attribute name="n"><text/></attribute>')
    Root = registry.model_for(element="doc")
    model = Root.model_validate({"@n": "x"})
    empty_content = replace(
        route,
        content=replace(route.content, model=ContentContract("empty")),
    )
    with pytest.raises(ValueError, match="do not belong to one compiled alternative"):
        route_to_lexical(empty_content, model)
    bogus = replace(
        route,
        content=replace(route.content, model=ContentContract("made-up")),
    )
    with pytest.raises(IntegrationError, match="unknown content contract"):
        route_to_lexical(bogus, model)
    no_patterns = replace(route, patterns=())
    with pytest.raises(IntegrationError, match="retains no compiled element patterns"):
        validate_route_data(no_patterns, {"@n": "x"})
    rejected = replace(route, patterns=(NotAllowed(), NotAllowed()))
    with pytest.raises(ValueError, match="not allowed"):
        validate_route_data(rejected, {"@n": "x"})


def test_child_route_identity_mismatch_is_a_configuration_error():
    parent_schema = compile_element('<element name="child"><text/></element>')
    parent_route = parent_schema.projection.routes[parent_schema.projection.roots[0]]
    assert models(parent_schema).model_for(element="doc")
    other_schema = compile_element(
        '<element name="child"><text/><attribute name="extra"><text/></attribute></element>'
    )
    Other = models(other_schema).model_for(element="doc", path=("child",))
    other_child = Other.model_validate({"$": "x", "@extra": "value"})
    with pytest.raises(IntegrationError, match="does not match field route"):
        validate_route_data(parent_route, {"child": other_child})
