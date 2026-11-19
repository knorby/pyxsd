"""RELAX NG document/model snapshots and document creation.

Conversion reads only current typed binding evidence.  A model is an
independent snapshot: it retains no lexical node, revision, or caller mapping,
and later source edits cannot change its values, wrappers, or candidate IDs.
Document creation revalidates the model, builds one compiled-order lexical
tree under an explicit admitted start name, and delegates validity to the
ordinary guarded parse instead of treating route validation as a document
certificate.
"""

from __future__ import annotations

import io
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

pydantic = pytest.importorskip("pydantic")
import pyxsd.relaxng.pydantic_adapter as adapter  # noqa: E402
from integration_helpers import RECORD_SCHEMA, compile_schema  # noqa: E402
from integration_helpers import parse as parse_xsd  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from pyxsd.exceptions import PyXSDError, ValidationError  # noqa: E402
from pyxsd.integrations import IntegrationError  # noqa: E402
from pyxsd.integrations._values import prepare_document  # noqa: E402
from pyxsd.integrations.pydantic import ModelSet, models  # noqa: E402
from pyxsd.relaxng.document import RelaxNGDocument  # noqa: E402
from pyxsd.relaxng.errors import StaleInterpretationError  # noqa: E402
from pyxsd.relaxng.projection import ProjectionPlan  # noqa: E402
from pyxsd.relaxng.pydantic_adapter import (  # noqa: E402
    RelaxNGModelSet,
    _project_record,
    route_to_lexical,
)
from pyxsd.relaxng.schema import RelaxNGSchema  # noqa: E402
from pyxsd.relaxng.values import AlternativeValue  # noqa: E402
from pyxsd.xsd_data_types import QName, qname_context  # noqa: E402

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


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


def parse(schema: RelaxNGSchema, xml: str, **kwargs: object) -> RelaxNGDocument:
    return schema.parse(io.StringIO(xml), **kwargs)


def registry(schema: RelaxNGSchema) -> RelaxNGModelSet:
    result = models(schema)
    assert isinstance(result, RelaxNGModelSet)
    return result


def test_document_model_round_trip_preserves_values_and_compiled_order():
    schema = compile_element(
        """
        <attribute name="version"><data type="integer"/></attribute>
        <optional><attribute name="lang"><text/></attribute></optional>
        <element name="single"><data type="integer"/></element>
        <optional><element name="maybe"><text/></element></optional>
        <zeroOrMore><element name="many"><text/></element></zeroOrMore>
        """
    )
    model_set = registry(schema)
    document = parse(
        schema,
        '<doc version="7"><single>3</single><many>a</many><many>b</many></doc>',
    )
    model = model_set.from_document(document)
    assert model.attr_version == 7
    assert model.attr_lang is None
    assert model.single.root == 3
    assert model.maybe is None
    assert [item.root for item in model.many] == ["a", "b"]
    assert model.model_dump(by_alias=True) == {
        "@version": 7,
        "single": 3,
        "many": ["a", "b"],
    }

    again = model_set.to_document(model)
    assert isinstance(again, RelaxNGDocument)
    assert again.schema is schema
    assert again.typed is True
    assert again.interpretations_current is True
    assert again.is_valid
    assert again.to_string() == (
        '<doc version="7"><single>3</single><many>a</many><many>b</many></doc>'
    )
    snapshot = model_set.from_document(again)
    assert snapshot.model_dump(by_alias=True) == model.model_dump(by_alias=True)


def test_capture_on_and_off_agree_and_candidate_ids_survive_round_trip():
    schema = compile_element('<choice><data type="integer"/><data type="string"/></choice>')
    model_set = registry(schema)
    xml = "<doc>12</doc>"
    plain = parse(schema, xml)
    captured = parse(schema, xml, capture="patterns")
    assert plain.typed and captured.typed
    plain_model = model_set.from_document(plain)
    captured_model = model_set.from_document(captured)
    assert isinstance(plain_model.root, AlternativeValue)
    assert isinstance(captured_model.root, AlternativeValue)
    plain_ids = tuple(item.candidate_id for item in plain_model.root.candidates)
    captured_ids = tuple(item.candidate_id for item in captured_model.root.candidates)
    assert plain_ids == captured_ids
    assert plain_ids == tuple(item.candidate_id for item in plain.root.scalar_value().candidates)
    assert plain_model.model_dump() == captured_model.model_dump()
    assert str(plain_model.root) == "12"

    list_schema = compile_element(
        '<list><choice><data type="integer"/><data type="string"/></choice></list>'
    )
    list_set = registry(list_schema)
    list_document = parse(list_schema, "<doc>12</doc>")
    list_model = list_set.from_document(list_document)
    document_value = list_document.root.scalar_value()
    assert isinstance(list_model.root, AlternativeValue)
    assert tuple(item.candidate_id for item in list_model.root.candidates) == tuple(
        item.candidate_id for item in document_value.candidates
    )
    assert list_set.to_document(list_model).to_string() == "<doc>12</doc>"


def test_model_snapshot_is_independent_of_source_mutation():
    schema = compile_element(
        """
        <attribute name="version"><data type="integer"/></attribute>
        <element name="x">
          <choice><data type="integer"/><data type="string"/></choice>
        </element>
        <zeroOrMore><element name="item"><text/></element></zeroOrMore>
        """
    )
    model_set = registry(schema)
    document = parse(
        schema,
        '<doc version="7"><x>12</x><item>one</item><item>two</item></doc>',
    )
    model = model_set.from_document(document)
    before_dump = model.model_dump(by_alias=True)
    before_json = model.model_dump_json()
    before_ids = tuple(item.candidate_id for item in model.x.root.candidates)

    document.root.set_attribute("version", "99")
    document.root.children[0].set_text("99")
    document.root.children[1].set_text("changed")
    document.root.remove_child(2)

    assert model.model_dump(by_alias=True) == before_dump
    assert model.model_dump_json() == before_json
    assert tuple(item.candidate_id for item in model.x.root.candidates) == before_ids
    assert model.x.root.candidates == model.x.root.candidates


def test_revalidate_controls_stale_evidence_and_never_reuses_candidates():
    schema = compile_element('<attribute name="n"><data type="integer"/></attribute>')
    model_set = registry(schema)
    document = parse(schema, '<doc n="1"/>')
    model = model_set.from_document(document)
    assert model.attr_n == 1

    document.root.set_attribute("n", "5")
    with pytest.raises(StaleInterpretationError, match="stale"):
        model_set.from_document(document)
    with pytest.raises(StaleInterpretationError, match="stale"):
        model_set.from_node(document.root, element="doc")

    fresh = model_set.from_document(document, revalidate=True)
    assert fresh.attr_n == 5
    assert model.attr_n == 1
    with pytest.raises(StaleInterpretationError):
        model_set.from_document(document)


def test_validation_only_invalid_foreign_and_unbound_documents_are_rejected():
    schema = compile_element('<attribute name="n"><data type="integer"/></attribute>')
    model_set = registry(schema)
    validation_only = parse(schema, '<doc n="1"/>', typed=False)
    with pytest.raises(PyXSDError, match="typed parse"):
        model_set.from_document(validation_only)
    with pytest.raises(PyXSDError, match="typed scalar access"):
        model_set.from_node(validation_only.root, element="doc")

    invalid = parse(schema, '<doc n="not-an-integer"/>')
    assert not invalid.is_valid
    with pytest.raises(ValidationError):
        model_set.from_document(invalid)
    with pytest.raises(ValidationError):
        model_set.from_node(invalid.root, element="doc")

    foreign_schema = compile_element('<attribute name="n"><data type="integer"/></attribute>')
    with pytest.raises(IntegrationError, match="another compiled schema"):
        model_set.from_document(parse(foreign_schema, '<doc n="1"/>'))

    unbound = parse(schema, '<doc n="1"/>')
    unbound.root = None  # type: ignore[assignment]
    with pytest.raises(IntegrationError, match="no bound root"):
        prepare_document(schema, unbound, False)


def test_prepare_document_rejects_wrong_document_and_schema_kinds():
    schema = compile_element("<text/>")
    fake_document = SimpleNamespace(schema=schema)
    with pytest.raises(IntegrationError, match="not a RELAX NG document"):
        prepare_document(schema, fake_document, False)  # type: ignore[arg-type]

    fake_schema = SimpleNamespace(require_valid=lambda: None)
    with pytest.raises(TypeError, match="compiled XSD or RELAX NG schema"):
        prepare_document(  # type: ignore[arg-type]
            fake_schema,
            SimpleNamespace(schema=fake_schema),
            False,
        )


def test_from_node_start_and_local_snapshots_use_their_own_scope():
    schema = compile_element(
        '<element name="item"><attribute name="v"><data type="integer"/></attribute></element>'
    )
    model_set = registry(schema)
    document = parse(schema, '<doc><item v="3"/></doc>')
    start = model_set.from_node(document.root, element="doc")
    assert start.item.attr_v == 3
    local = model_set.from_node(document.root.children[0], element="doc", path=("item",))
    assert local.attr_v == 3
    assert isinstance(local, BaseModel)

    with pytest.raises(IntegrationError, match="belongs to route"):
        model_set.from_node(document.root, element="doc", path=("item",))
    with pytest.raises(IntegrationError, match="not admitted"):
        model_set.from_node(document.root, element="doc", path=("other",))


def test_from_node_generic_name_fallback_is_defensive_for_unrouted_nodes(monkeypatch):
    """Typed parses assign route classes; this pins the fail-closed fallback."""
    schema = compile_element(
        '<attribute name="v"><text/></attribute>'
        '<element name="item"><attribute name="v"><text/></attribute><text/></element>'
    )
    model_set = registry(schema)
    document = parse(schema, '<doc v="x"><item v="y">hi</item></doc>')
    monkeypatch.setattr(adapter, "_node_route_id", lambda node: None)
    model = model_set.from_node(document.root, element="doc")
    assert model.attr_v == "x"
    assert model.item.attr_v == "y"
    with pytest.raises(IntegrationError, match="not admitted"):
        model_set.from_node(document.root, element="doc", path=("item",))


def test_from_node_requires_a_relaxng_node():
    schema = compile_element("<text/>")
    model_set = registry(schema)
    with pytest.raises(IntegrationError, match="RELAX NG lexical node"):
        model_set.from_node(object(), element="doc")  # type: ignore[arg-type]


def test_from_document_generic_route_and_unknown_start_names_are_precise():
    generic = compile_element(
        '<mixed><zeroOrMore><element name="item"><text/></element></zeroOrMore></mixed>'
    )
    generic_set = registry(generic)
    document = parse(generic, "<doc><item>x</item></doc>")
    with pytest.raises(IntegrationError, match="mixed content"):
        generic_set.from_document(document)

    lost_schema = compile_element(
        '<choice><data type="integer"/><element name="nested"><empty/></element></choice>'
    )
    lost_set = registry(lost_schema)
    lost_document = parse(lost_schema, "<doc>12</doc>")
    assert not hasattr(type(lost_document.root), "_relaxng_route_id_")
    with pytest.raises(IntegrationError, match=r"doc.*generic"):
        lost_set.from_document(lost_document)

    plain = compile_element("<text/>")
    plain_set = registry(plain)
    plain_document = parse(plain, "<doc>x</doc>")
    with pytest.raises(IntegrationError, match="no admitted start route"):
        plain_set.from_node(plain_document.root, element="other")


def test_empty_absent_and_explicit_empty_lexical_values_are_distinct():
    text_schema = compile_element("<text/>")
    text_set = registry(text_schema)
    absent_text = text_set.from_document(parse(text_schema, "<doc/>"))
    explicit_text = ET.Element("doc")
    explicit_text.text = ""
    present_text = text_set.from_document(text_schema.parse(explicit_text))
    assert absent_text.root is None
    assert present_text.root == ""
    assert text_set.to_document(absent_text).to_string() == "<doc/>"
    assert text_set.to_document(present_text).to_string() == "<doc></doc>"

    string_schema = compile_element('<data type="string"/>')
    string_set = registry(string_schema)
    empty_string = string_set.from_document(parse(string_schema, "<doc/>"))
    assert empty_string.root == ""
    assert string_set.to_document(empty_string).to_string() == "<doc></doc>"

    optional_schema = compile_element('<optional><data type="integer"/></optional>')
    optional_set = registry(optional_schema)
    absent_optional = optional_set.from_document(parse(optional_schema, "<doc/>"))
    assert absent_optional.root is None
    assert optional_set.to_document(absent_optional).is_valid

    empty_schema = compile_element("<empty/>")
    empty_set = registry(empty_schema)
    empty_model = empty_set.from_document(parse(empty_schema, "<doc/>"))
    assert empty_model.model_dump() == {}
    assert empty_set.to_document(empty_model).to_string() == "<doc/>"


def test_record_optional_scalar_empty_attribute_and_absent_repeat():
    optional_schema = compile_element(
        '<attribute name="n"><text/></attribute><optional><data type="integer"/></optional>'
    )
    optional_set = registry(optional_schema)
    optional = optional_set.from_document(parse(optional_schema, '<doc n="x"/>'))
    assert optional.model_dump(by_alias=True) == {"@n": "x", "$": None}
    created = optional_set.to_document(optional)
    assert created.is_valid
    assert created.to_string() == '<doc n="x"/>'

    empty_attribute_schema = compile_element('<attribute name="x"><empty/></attribute><text/>')
    empty_attribute_set = registry(empty_attribute_schema)
    empty_attribute = empty_attribute_set.from_document(
        parse(empty_attribute_schema, '<doc x="">value</doc>')
    )
    assert empty_attribute.attr_x == ""
    assert empty_attribute.model_dump(by_alias=True)["$"] == "value"
    assert empty_attribute_set.to_document(empty_attribute).is_valid

    repeated_schema = compile_element(
        '<attribute name="n"><text/></attribute>'
        '<zeroOrMore><element name="item"><text/></element></zeroOrMore>'
    )
    repeated_set = registry(repeated_schema)
    repeated = repeated_set.from_document(parse(repeated_schema, '<doc n="x"/>'))
    assert repeated.item == []
    assert repeated_set.to_document(repeated).to_string() == '<doc n="x"/>'


def test_projection_missing_typed_evidence_branches_are_defensive(monkeypatch):
    """Valid documents carry evidence; these unreachable branches fail closed."""
    scalar_schema = compile_element('<data type="integer"/>')
    scalar_set = registry(scalar_schema)
    scalar_document = parse(scalar_schema, "<doc>12</doc>")
    monkeypatch.setattr(adapter, "_typed_snapshot_at", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="path doc/\\$"):
        scalar_set.from_document(scalar_document)

    attribute_schema = compile_element('<attribute name="n"><text/></attribute>')
    attribute_set = registry(attribute_schema)
    attribute_document = parse(attribute_schema, '<doc n="x"/>')
    with pytest.raises(ValueError, match="path doc/@n"):
        attribute_set.from_document(attribute_document)


def test_projection_unreachable_child_states_fail_closed(monkeypatch):
    """A valid typed document cannot reach these fabricated child states."""
    required_schema = compile_element('<element name="item"><text/></element>')
    required_set = registry(required_schema)
    required_route = required_schema.projection.routes[required_schema.projection.roots[0]]
    required_plan = required_schema.projection

    empty_document = parse(compile_element("<text/>"), "<doc/>")
    with pytest.raises(ValueError, match="required child is absent"):
        _project_record(
            required_plan,
            required_route,
            empty_document.root,
            ("doc",),
            model_for=required_set._model,
        )

    many_schema = compile_element('<zeroOrMore><element name="item"><text/></element></zeroOrMore>')
    many_document = parse(many_schema, "<doc><item>a</item><item>b</item></doc>")
    monkeypatch.setattr(adapter, "_project_node", lambda *args, **kwargs: object())
    with pytest.raises(ValueError, match="singular child occurs 2 times"):
        _project_record(
            required_plan,
            required_route,
            many_document.root,
            ("doc",),
            model_for=required_set._model,
        )
    monkeypatch.undo()

    other_schema = compile_element('<element name="other"><text/></element>')
    other_document = parse(other_schema, "<doc><other>x</other></doc>")
    with pytest.raises(ValueError, match="child 'other' is not admitted"):
        _project_record(
            required_plan,
            required_route,
            other_document.root,
            ("doc",),
            model_for=required_set._model,
        )

    no_child_field = replace(
        required_route,
        children={
            name: replace(field, child_route_id=None)
            for name, field in required_route.children.items()
        },
    )
    with pytest.raises(IntegrationError, match="no route identity"):
        _project_record(
            required_plan,
            no_child_field,
            many_document.root,
            ("doc",),
            model_for=required_set._model,
        )


def test_child_base_uri_shadows_the_inherited_document_base():
    schema = compile_element(
        '<element name="b"><optional><attribute name="xml:base"><text/></attribute>'
        '</optional><choice><data type="integer"/><data type="string"/></choice></element>'
    )
    model_set = registry(schema)
    document = parse(schema, '<doc><b xml:base="urn:child">12</b></doc>')
    model = model_set.from_document(document)
    created = model_set.to_document(
        model,
        context={"relaxng": {"base_uri": "urn:root"}},
    )
    assert created.is_valid
    assert created.root.base_uri == "urn:root"
    assert created.root.children[0].base_uri == "urn:child"


def test_repeated_choice_and_interleave_round_trip():
    schema = compile_grammar(
        f'<start><element name="doc"><interleave>'
        f'<zeroOrMore><element name="item"><data type="integer" '
        f'datatypeLibrary="{XSD}"/></element></zeroOrMore>'
        f'<optional><element name="flag"><text/></element></optional>'
        f"</interleave></element></start>"
    )
    model_set = registry(schema)
    document = parse(schema, "<doc><item>1</item><flag>x</flag><item>2</item></doc>")
    model = model_set.from_document(document)
    assert [item.root for item in model.item] == [1, 2]
    assert model.flag.root == "x"
    again = model_set.to_document(model)
    assert again.is_valid
    assert again.to_string() == "<doc><item>1</item><item>2</item><flag>x</flag></doc>"
    assert model_set.from_document(again).model_dump(by_alias=True) == model.model_dump(
        by_alias=True
    )

    choice_schema = compile_grammar(
        "<start><choice>"
        '<element name="a"><text/></element>'
        '<element name="b"><text/></element>'
        "</choice></start>"
    )
    choice_set = registry(choice_schema)
    choice_model = choice_set.from_document(parse(choice_schema, "<b>value</b>"))
    assert choice_model.root == "value"
    assert choice_set.to_document(choice_model).to_string() == "<b>value</b>"
    assert choice_set.to_document(choice_model, route="b").is_valid


def test_qname_and_base_context_are_scoped_per_field_without_sibling_leak():
    schema = compile_element(
        """
        <attribute name="ref"><data type="QName"/></attribute>
        <element name="a"><data type="QName"/></element>
        <element name="b"><data type="QName"/></element>
        """
    )
    model_set = registry(schema)
    document = parse(
        schema,
        '<doc xmlns:p="urn:a" ref="p:top">'
        '<a xmlns:p="urn:a">p:x</a>'
        '<b xmlns:p="urn:b">p:y</b>'
        "</doc>",
    )
    model = model_set.from_document(document)
    assert model.attr_ref.to_expanded_name() == "{urn:a}top"
    assert model.a.root.to_expanded_name() == "{urn:a}x"
    assert model.b.root.to_expanded_name() == "{urn:b}y"

    context = {"relaxng": {"namespaces": {"p": "urn:a"}, "base_uri": "urn:base"}}
    original = deepcopy(context)
    again = model_set.to_document(model, context=context)
    assert again.is_valid
    assert context == original
    assert again.root.base_uri == "urn:base"
    assert again.to_string() == (
        '<doc xmlns:p="urn:a" ref="p:top"><a>p:x</a><b xmlns:p="urn:b">p:y</b></doc>'
    )
    round_tripped = model_set.from_document(again)
    assert round_tripped.attr_ref.to_expanded_name() == "{urn:a}top"
    assert round_tripped.a.root.to_expanded_name() == "{urn:a}x"
    assert round_tripped.b.root.to_expanded_name() == "{urn:b}y"


def test_missing_standalone_qname_context_is_still_a_conversion_error():
    schema = compile_element('<attribute name="ref"><data type="QName"/></attribute>')
    model_set = registry(schema)
    document = parse(schema, '<doc xmlns:p="urn:x" ref="p:x"/>')
    model = model_set.from_document(document)
    assert model.attr_ref.to_expanded_name() == "{urn:x}x"
    Root = model_set.model_for(element="doc")
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({"@ref": "p:x"})
    constructed = Root.model_construct(attr_ref="p:x")
    with pytest.raises(pydantic.ValidationError, match="does not satisfy"):
        model_set.to_document(constructed)


def test_default_and_explicit_start_route_selection():
    schema = compile_element("<text/>")
    model_set = registry(schema)
    Root = model_set.model_for(element="doc")
    model = Root.model_validate("value")
    start = schema.projection.roots[0]
    route = schema.projection.routes[start]
    for kwargs in ({}, {"route": start}, {"route": route.expanded_name}):
        document = model_set.to_document(model, **kwargs)
        assert document.is_valid
        assert document.to_string() == "<doc>value</doc>"

    with pytest.raises(IntegrationError, match="no such route"):
        model_set.to_document(model, route="not-a-route")
    with pytest.raises(IntegrationError, match="route ID or an exact expanded"):
        model_set.to_document(model, route=object())  # type: ignore[arg-type]


def test_to_document_route_is_a_check_of_the_model_own_route():
    schema = compile_grammar(
        "<start><choice>"
        '<element name="records"><attribute name="v"><text/></attribute><text/></element>'
        '<element name="scalar"><data type="integer" '
        f'datatypeLibrary="{XSD}"/></element>'
        "</choice></start>"
    )
    model_set = registry(schema)
    records = model_set.model_for(element="records").model_validate({"@v": "x", "$": "value"})
    scalar = model_set.model_for(element="scalar").model_validate(12)
    records_route = schema.projection.routes[records.__relaxng_route_id__]
    scalar_route = schema.projection.routes[scalar.__relaxng_route_id__]

    for selector in (records_route.route_id, records_route.expanded_name):
        assert model_set.to_document(records, route=selector).to_string() == (
            '<records v="x">value</records>'
        )
    for selector in (scalar_route.route_id, scalar_route.expanded_name):
        assert model_set.to_document(scalar, route=selector).to_string() == ("<scalar>12</scalar>")

    with pytest.raises(IntegrationError, match="can only become a document") as error:
        model_set.to_document(records, route=scalar_route.expanded_name)
    message = str(error.value)
    assert records_route.route_id in message
    assert scalar_route.route_id in message
    with pytest.raises(IntegrationError, match="can only become a document"):
        model_set.to_document(scalar, route=records_route.expanded_name)
    with pytest.raises(TypeError, match="Pydantic model"):
        model_set.to_document({"$": "value"})  # type: ignore[arg-type]

    def ambiguous(self: ProjectionPlan, selector: str):
        del self, selector
        raise ValueError("ambiguous RELAX NG projection route; use a route ID")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ProjectionPlan, "resolve_route", ambiguous)
        with pytest.raises(IntegrationError, match="ambiguous"):
            model_set.to_document(records, route="records")


def test_to_document_canonicalizes_equal_routes_and_rejects_foreign_models():
    schema = compile_element("<text/>")
    model_set = registry(schema)
    equal_schema = compile_element("<text/>")
    equal_model = registry(equal_schema).model_for(element="doc").model_validate("value")
    assert model_set.to_document(equal_model).to_string() == "<doc>value</doc>"

    foreign_schema = compile_element('<data type="integer"/>')
    foreign_model = registry(foreign_schema).model_for(element="doc").model_validate(7)
    with pytest.raises(IntegrationError, match="another compiled schema"):
        model_set.to_document(foreign_model)

    nested_schema = compile_element(
        '<attribute name="v"><text/></attribute>'
        '<element name="item"><choice><data type="integer"/><data type="string"/></choice>'
        "</element>"
    )
    nested_set = registry(nested_schema)
    equal_nested_schema = compile_element(
        '<attribute name="v"><text/></attribute>'
        '<element name="item"><choice><data type="integer"/><data type="string"/></choice>'
        "</element>"
    )
    equal_nested_model = (
        registry(equal_nested_schema)
        .model_for(element="doc")
        .model_validate({"@v": "x", "item": "12"})
    )
    created = nested_set.to_document(equal_nested_model)
    assert created.is_valid
    assert created.to_string() == '<doc v="x"><item>12</item></doc>'
    canonical = nested_set.from_document(created)
    assert type(canonical.item) is nested_set.model_for(element="doc", path=("item",))
    assert canonical.model_dump(by_alias=True) == equal_nested_model.model_dump(by_alias=True)


def test_to_document_preserves_qname_list_contexts_and_rejects_edges():
    list_schema = compile_element('<list><oneOrMore><data type="QName"/></oneOrMore></list>')
    list_set = registry(list_schema)
    list_document = parse(list_schema, '<doc xmlns:p="urn:x">p:a p:b</doc>')
    list_model = list_set.from_document(list_document)
    created = list_set.to_document(list_model)
    assert created.is_valid
    assert created.to_string() == '<doc xmlns:p="urn:x">p:a p:b</doc>'

    Root = list_set.model_for(element="doc")
    with qname_context({"p": "urn:a"}):
        first = QName("p:a")
    with qname_context({"p": "urn:b"}):
        second = QName("p:b")
    with pytest.raises(ValueError, match="QName values declare conflicting"):
        list_set.to_document(Root.model_construct(root=(first, second)))

    with qname_context({}):
        unbound = QName("p:x")
        plain = QName("plain")
    assert list_set.to_document(Root.model_construct(root=(plain,))).is_valid
    with pytest.raises(pydantic.ValidationError):
        list_set.to_document(Root.model_construct(root=(unbound,)))
    with pytest.raises(pydantic.ValidationError):
        list_set.to_document(Root.model_construct(root=QName("p:x")))

    restricted_schema = compile_element('<data type="QName"><param name="pattern">a</param></data>')
    restricted_set = registry(restricted_schema)
    restricted = restricted_set.model_for(element="doc")
    with qname_context({"": "urn:x"}):
        rejected = QName("b")
    with pytest.raises(pydantic.ValidationError):
        restricted_set.to_document(restricted.model_construct(root=rejected))


def test_local_route_to_document_is_rejected_with_local_scope():
    schema = compile_element('<element name="item"><text/></element>')
    model_set = registry(schema)
    local = model_set.model_for(element="doc", path=("item",))
    document = parse(schema, "<doc><item>x</item></doc>")
    local_model = model_set.from_node(document.root.children[0], element="doc", path=("item",))
    assert local_model.root == "x"
    with pytest.raises(IntegrationError, match="local scope"):
        model_set.to_document(local_model)
    local_route = schema.projection.routes[local.__relaxng_route_id__]
    for selector in (None, local_route.route_id, schema.projection.roots[0]):
        kwargs = {} if selector is None else {"route": selector}
        with pytest.raises(IntegrationError, match="local scope"):
            model_set.to_document(local_model, **kwargs)
    start_model = model_set.from_document(document)
    with pytest.raises(IntegrationError, match="local scope"):
        model_set.to_document(start_model, route=local_route.route_id)


def test_fixed_and_open_start_element_spellings_are_explicit():
    fixed = compile_element("<text/>", ns="urn:fixed")
    fixed_set = registry(fixed)
    fixed_model = fixed_set.model_for(element="{urn:fixed}doc").model_validate("x")
    assert fixed_set.to_document(fixed_model, element="{urn:fixed}doc").is_valid
    assert fixed_set.to_document(fixed_model, element="Q{urn:fixed}doc").is_valid
    for wrong in ("doc", "{urn:other}doc", "other"):
        with pytest.raises(IntegrationError, match="does not match"):
            fixed_set.to_document(fixed_model, element=wrong)

    open_schema = compile_grammar(
        '<start><element><anyName><except><name ns="urn:no">bad</name></except>'
        "</anyName><text/></element></start>"
    )
    open_set = registry(open_schema)
    open_model = open_set.model_for(element="doc").model_validate("value")
    with pytest.raises(IntegrationError, match="explicit element"):
        open_set.to_document(open_model)
    with pytest.raises(IntegrationError, match="not admitted"):
        open_set.to_document(open_model, element="{urn:no}bad")
    with pytest.raises(IntegrationError, match="prefix-form EQName"):
        open_set.to_document(open_model, element="p:doc")
    assert open_set.to_document(open_model, element="doc").to_string() == "<doc>value</doc>"
    expanded = open_set.to_document(open_model, element="Q{urn:yes}doc")
    assert expanded.is_valid
    assert expanded.root._name_ == "{urn:yes}doc"

    absent = open_set.from_document(parse(open_schema, "<doc>value</doc>"))
    with pytest.raises(IntegrationError, match="explicit element"):
        open_set.to_document(absent)


def test_to_document_revalidates_mutated_and_constructed_records():
    schema = compile_element(
        '<attribute name="n"><data type="integer"/></attribute><data type="integer"/>'
    )
    model_set = registry(schema)
    Root = model_set.model_for(element="doc")
    value_field = next(
        name for name, info in Root.model_fields.items() if (info.alias or name) == "$"
    )
    ordinary = Root.model_validate({"@n": 7, "$": 12})
    assert model_set.to_document(ordinary).is_valid

    mutated = Root.model_validate({"@n": 7, "$": 12})
    mutated.attr_n = None
    with pytest.raises(pydantic.ValidationError, match="scalar content is required"):
        model_set.to_document(mutated)
    with pytest.raises(pydantic.ValidationError, match="scalar content is required"):
        model_set.to_document(Root.model_construct(attr_n=None, **{value_field: 12}))
    with pytest.raises(pydantic.ValidationError):
        model_set.to_document(Root.model_construct())
    with pytest.raises(pydantic.ValidationError, match="scalar content is required"):
        model_set.to_document(Root.model_construct(attr_n=7, **{value_field: None}))
    with pytest.raises(pydantic.ValidationError):
        model_set.to_document(Root.model_construct(attr_n=7, **{value_field: "not-an-integer"}))
    with pytest.raises(pydantic.ValidationError, match="scalar-tag-invalid"):
        model_set.to_document(Root.model_construct(attr_n=7, **{value_field: {"#value": {}}}))


def test_to_document_rejects_constructed_none_child_content():
    schema = compile_element('<element name="item"><text/></element>')
    model_set = registry(schema)
    Root = model_set.model_for(element="doc")
    with pytest.raises(pydantic.ValidationError, match="omitted, not None"):
        model_set.to_document(Root.model_construct(item=None))


def test_to_document_revalidates_ambiguous_root_scalars():
    schema = compile_element('<choice><data type="integer"/><data type="string"/></choice>')
    model_set = registry(schema)
    Root = model_set.model_for(element="doc")
    valid = Root.model_validate("12")
    assert isinstance(valid.root, AlternativeValue)
    created = model_set.to_document(valid)
    assert created.is_valid
    assert created.to_string() == "<doc>12</doc>"
    round_tripped = model_set.from_document(created)
    assert round_tripped.model_dump() == valid.model_dump()
    assert tuple(item.candidate_id for item in round_tripped.root.candidates) == tuple(
        item.candidate_id for item in valid.root.candidates
    )

    mutated = Root.model_validate("12")
    mutated.root = 12
    with pytest.raises(pydantic.ValidationError, match="ambiguous RELAX NG scalar"):
        model_set.to_document(mutated)
    with pytest.raises(pydantic.ValidationError, match="ambiguous RELAX NG scalar"):
        model_set.to_document(Root.model_construct(root=12))

    wrapper = valid.root
    incomplete = AlternativeValue(
        wrapper.lexical,
        (wrapper.candidates[0],),
        wrapper.context,
        wrapper.grammar_fingerprint,
        wrapper.revision,
    )
    with pytest.raises(pydantic.ValidationError, match="candidate set"):
        model_set.to_document(Root.model_construct(root=incomplete))
    with pytest.raises(pydantic.ValidationError, match="scalar-tag-invalid"):
        model_set.to_document(Root.model_construct(root={"#value": {}}))


def test_to_document_revalidates_ambiguous_attribute_scalars():
    schema = compile_element(
        '<attribute name="v"><choice><data type="integer"/>'
        '<data type="string"/></choice></attribute>'
    )
    model_set = registry(schema)
    Root = model_set.model_for(element="doc")
    valid = Root.model_validate({"@v": "12"})
    assert isinstance(valid.attr_v, AlternativeValue)
    assert model_set.to_document(valid).is_valid
    mutated = Root.model_validate({"@v": "12"})
    mutated.attr_v = 12
    with pytest.raises(pydantic.ValidationError, match="ambiguous RELAX NG scalar"):
        model_set.to_document(mutated)
    with pytest.raises(pydantic.ValidationError, match="ambiguous RELAX NG scalar"):
        model_set.to_document(Root.model_construct(attr_v=12))


def test_nested_malformed_scalar_tag_is_rejected_before_document_creation():
    schema = compile_element('<element name="item"><data type="integer"/></element>')
    model_set = registry(schema)
    Root = model_set.model_for(element="doc")
    Child = model_set.model_for(element="doc", path=("item",))
    child = Child.model_construct(root={"#value": {}})
    parent = Root.model_construct(item=child)
    with pytest.raises(pydantic.ValidationError, match="scalar-tag-invalid"):
        model_set.to_document(parent)
    route = schema.projection.routes[schema.projection.roots[0]]
    with pytest.raises(ValueError, match="route path doc/item"):
        route_to_lexical(route, parent)


def test_root_context_conflict_is_not_silently_ignored():
    schema = compile_element(
        '<optional><attribute name="xml:base"><text/></attribute></optional>'
        '<choice><data type="integer"/><data type="string"/></choice>'
    )
    model_set = registry(schema)
    document = parse(schema, '<doc xml:base="urn:doc">12</doc>')
    model = model_set.from_document(document)
    same = model_set.to_document(model, context={"relaxng": {"base_uri": "urn:doc"}})
    assert same.is_valid
    assert same.root.base_uri == "urn:doc"
    with pytest.raises(ValueError, match="conflicting base URIs"):
        model_set.to_document(model, context={"relaxng": {"base_uri": "urn:other"}})


def test_to_document_does_not_mutate_model_or_caller_context():
    schema = compile_element(
        '<attribute name="n"><data type="integer"/></attribute><element name="item"><text/></element>'
    )
    model_set = registry(schema)
    document = parse(schema, '<doc n="3"><item>value</item></doc>')
    model = model_set.from_document(document)
    model_dump = model.model_dump(by_alias=True)
    source = document.to_string()
    context = {"relaxng": {"namespaces": {"p": "urn:x"}}, "caller": {"keep": True}}
    original = deepcopy(context)
    created = model_set.to_document(model, context=context)
    assert created.is_valid
    assert model.model_dump(by_alias=True) == model_dump
    assert document.to_string() == source
    assert context == original


def test_document_creation_fallback_is_defensive_and_returns_parse_report(monkeypatch):
    """Admitted start routes normally validate; this pins the report fallback."""
    schema = compile_element("<text/>")
    model_set = registry(schema)
    Root = model_set.model_for(element="doc")
    model = Root.model_validate("value")
    original = adapter._accepted_route_tree

    def force_fallback(*args: object, **kwargs: object):
        if kwargs.get("root_name") is not None:
            raise ValueError("forced named validation failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "_accepted_route_tree", force_fallback)
    created = model_set.to_document(model)
    assert isinstance(created, RelaxNGDocument)
    assert created.is_valid
    assert created.to_string() == "<doc>value</doc>"


def test_xsd_modelset_and_prepare_document_behavior_is_unchanged():
    schema = compile_schema(RECORD_SCHEMA)
    model_set = models(schema)
    assert isinstance(model_set, ModelSet)
    assert not hasattr(model_set, "to_document")
    document = parse_xsd(schema, "<root><count>7</count></root>")
    assert prepare_document(schema, document, False) is document
    fresh = prepare_document(schema, document, True)
    assert fresh is not document
    assert fresh.is_valid
    model = model_set.from_document(document)
    assert model.count == 7
    document.root.count = 8
    assert model_set.from_document(document).count == 8
    assert model_set.from_document(document, revalidate=True).count == 8
    assert model.count == 7
    with pytest.raises(IntegrationError, match="another compiled schema"):
        prepare_document(compile_schema(RECORD_SCHEMA), document, False)


def test_xsd_and_value_paths_keep_optional_pydantic_and_relaxng_imports_lazy():
    script = """
import sys
import pyxsd.integrations._values
assert "pydantic" not in sys.modules
assert "pyxsd.relaxng.document" not in sys.modules
assert "pyxsd.relaxng.schema" not in sys.modules
assert "pyxsd.relaxng.pydantic_adapter" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
