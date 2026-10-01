"""Generated models validate the projected contract, not generic exported dicts."""

from decimal import Decimal

import pytest

from integration_helpers import RECORD_SCHEMA, compile_schema, parse

pydantic = pytest.importorskip("pydantic")


def models(schema):
    from pyxsd.integrations.pydantic import models

    return models(schema)


def test_generate_without_xml_and_validate_independent_input():
    schema = compile_schema(RECORD_SCHEMA)
    registry = models(schema)
    Root = registry.model_for(element="root")
    model = Root.model_validate({"count": 7, "flag": True, "amount": {"$": "9999"}})
    assert model.count == 7
    assert model.item == []
    assert model.flag is True
    assert model.amount.xml_value == Decimal("9999")
    assert model.amount.attr_currency == "USD"
    assert Root is registry.model_for(element="root")
    assert Root.model_json_schema()["properties"]["count"]["type"] == "integer"
    assert "item" not in Root.model_json_schema()["required"]
    assert "flag" not in model.model_fields_set or model.flag is True


@pytest.mark.parametrize(
    "data",
    [
        {"count": "7"},
        {"count": True},
        {"count": 7.0},
        {"count": 7, "flag": None},
        {"count": 7, "item": None},
        {"count": 7, "item": (1,)},
        {"count": 7, "unexpected": 4},
        {"count": 7, "@version": None},
        {"count": 7, "amount": {"$": 1.25}},
        {"count": 7, "amount": {"$": "99999"}},
    ],
)
def test_strict_input_nullability_and_unknown_fields(data):
    Root = models(compile_schema(RECORD_SCHEMA)).model_for(element="root")
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate(data)


def test_snapshots_preserve_explicit_presence_and_effective_defaults():
    schema = compile_schema(RECORD_SCHEMA)
    registry = models(schema)
    doc = parse(schema, "<root><count>7</count><amount>1.00</amount></root>")
    model = registry.from_document(doc)
    assert model.attr_version == 9
    assert model.amount.attr_currency == "USD"
    assert model.model_dump(exclude_unset=True, by_alias=True) == {
        "count": 7,
        "amount": {"$": Decimal("1.00")},
    }
    assert model.item == []
    doc.root.count = 42
    assert registry.from_document(doc).count == 42
    model.count = 100
    assert doc.root.count == 42


def test_required_nillable_and_complex_nil_shells():
    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="required" type="xs:int" nillable="true"/>
    <xs:element name="shell" nillable="true"><xs:complexType><xs:sequence>
      <xs:element name="content" type="xs:int"/></xs:sequence>
      <xs:attribute name="why" type="xs:string" use="required"/>
    </xs:complexType></xs:element></xs:sequence></xs:complexType></xs:element>""")
    registry = models(schema)
    Root = registry.model_for(element="r")
    data = {"required": None, "shell": {"$nil": True, "@why": "unknown"}}
    assert Root.model_validate(data).shell.xml_nil is True
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({"shell": data["shell"]})
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({"required": None, "shell": {"$nil": True, "@why": "x", "content": 1}})
    doc = parse(
        schema,
        '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<required xsi:nil="true"/><shell xsi:nil="true" why="unknown"/></r>',
    )
    assert registry.from_document(doc).model_dump(exclude_unset=True, by_alias=True) == data


def test_choice_and_optional_composite_group_validation():
    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:choice><xs:element name="a" type="xs:int"/><xs:element name="b" type="xs:int"/></xs:choice>
    <xs:sequence minOccurs="0"><xs:element name="x" type="xs:int"/><xs:element name="y" type="xs:int"/></xs:sequence>
    </xs:sequence></xs:complexType></xs:element>""")
    Root = models(schema).model_for(element="r")
    assert Root.model_validate({"a": 1}).a == 1
    assert Root.model_validate({"b": 1, "x": 2, "y": 3}).y == 3
    for data in ({}, {"a": 1, "b": 2}, {"a": 1, "x": 2}):
        with pytest.raises(pydantic.ValidationError):
            Root.model_validate(data)


def test_primitive_root_local_model_and_safe_reserved_aliases():
    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="model_validate" type="xs:int"/><xs:element name="has-dash" type="xs:string"/>
    </xs:sequence></xs:complexType></xs:element>""")
    registry = models(schema)
    Root = registry.model_for(element="r")
    data = {"model_validate": 4, "has-dash": "works"}
    assert Root.model_validate(data).model_dump(by_alias=True) == data
    Primitive = registry.model_for(element="r", path=("model_validate",))
    assert Primitive.model_validate(4).root == 4
    node = parse(
        schema, "<r><model_validate>4</model_validate><has-dash>works</has-dash></r>"
    ).root._children_[0]
    assert registry.from_node(node, element="r", path=("model_validate",)).root == 4


def test_namespaced_aliases_are_expanded_and_collision_free():
    schema = compile_schema(
        """<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="x" type="xs:int"/><xs:element name="x" form="unqualified" type="xs:int"/>
    </xs:sequence></xs:complexType></xs:element>""",
        namespace="urn:test",
    )
    Root = models(schema).model_for(element="{urn:test}r")
    data = {"{urn:test}x": 1, "x": 2}
    assert Root.model_validate(data).model_dump(by_alias=True) == data


def test_numeric_lexical_pattern_is_rejected_before_model_generation():
    from pyxsd.integrations import IntegrationError

    schema = compile_schema("""<xs:element name="r"><xs:simpleType><xs:restriction base="xs:int">
    <xs:pattern value="0[0-9]+"/></xs:restriction></xs:simpleType></xs:element>""")
    with pytest.raises(IntegrationError, match="lexical"):
        models(schema).model_for(element="r")


def test_binary_lists_and_temporal_lexical_values():
    schema = compile_schema("""<xs:simpleType name="L"><xs:list itemType="xs:int"/></xs:simpleType>
    <xs:element name="r"><xs:complexType><xs:sequence><xs:element name="binary" type="xs:hexBinary"/>
    <xs:element name="values" type="L"/><xs:element name="when" type="xs:dateTime"/>
    </xs:sequence></xs:complexType></xs:element>""")
    registry = models(schema)
    doc = parse(
        schema,
        "<r><binary>00FF</binary><values>1 2</values><when>0000-01-01T00:00:00.123456789</when></r>",
    )
    model = registry.from_document(doc)
    assert model.binary == b"\x00\xff"
    assert model.values == [1, 2]
    assert model.when == "0000-01-01T00:00:00.123456789"


def test_revalidation_does_not_use_repaired_stale_report():
    schema = compile_schema(RECORD_SCHEMA)
    doc = schema.parse(__import__("io").StringIO("<root><count>bad</count></root>"))
    assert doc.report.has_errors
    registry = models(schema)
    import pyxsd

    with pytest.raises(pyxsd.ValidationError):
        registry.from_document(doc)


def test_fixed_child_values_and_required_repeated_json_schema():
    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="fixed" type="xs:int" fixed="2"/>
    <xs:element name="many" type="xs:int" minOccurs="2" maxOccurs="3"/>
    </xs:sequence></xs:complexType></xs:element>""")
    Root = models(schema).model_for(element="r")
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate({"fixed": 3, "many": [1, 2]})
    assert Root.model_validate({"fixed": 2, "many": [1, 2]}).fixed == 2
    description = Root.model_json_schema()
    assert "many" in description["required"]
    assert description["properties"]["many"]["minItems"] == 2
    assert description["properties"]["many"]["maxItems"] == 3
