"""Phase 6 release gate: integration parity and hostile input.

The gate compares shared XSD and RELAX NG registry/model/record workflows
where their contracts actually match, then pushes RELAX NG-specific evidence
through hostile routes: scalar alternatives, equivalent derivations,
recursion, correlated choices, invalid direct construction, stale snapshots,
forged scalar tags, selection incompatibility, unsupported record shapes, and
per-row Arrow/dataset failure policy.

Every negative assertion names the representation or scope limit that caused
the rejection.  A route never fails merely because more than one equivalent
derivation exists, and no check accepts a string, null, arbitrary branch, or
stale report in place of current typed evidence.  Optional dependencies are
imported inside the tests so the module collects and reports skips without
Pydantic or PyArrow installed.
"""

from __future__ import annotations

import copy
import io
import json
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from integration_helpers import compile_schema
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.integrations import IntegrationError
from pyxsd.integrations._values import prepare_document
from pyxsd.integrations.projection import FieldSource, field_source_metadata
from pyxsd.relaxng.contracts import candidate_id
from pyxsd.relaxng.datatypes import DatatypeFactory, DatatypeLibraryRegistration
from pyxsd.relaxng.document import RelaxNGDocument
from pyxsd.relaxng.errors import StaleInterpretationError
from pyxsd.relaxng.records import RelaxNGRecordProjection, iter_relaxng_rows
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.values import AlternativeValue

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
INT = (XSD, "integer")

MATCHED_XSD = """
<xs:element name="record">
  <xs:complexType>
    <xs:sequence>
      <xs:element name="label" type="xs:string" minOccurs="0"/>
      <xs:element name="n" type="xs:long" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:long" use="required"/>
  </xs:complexType>
</xs:element>
<xs:element name="value" type="xs:long"/>
"""

MATCHED_RNG_RECORD = """
<attribute name="id"><data type="long"/></attribute>
<optional><element name="label"><text/></element></optional>
<zeroOrMore><element name="n"><data type="long"/></element></zeroOrMore>
"""


def compile_element(
    body: str,
    *,
    name: str = "doc",
    ns: str | None = None,
    datatype_libraries: dict[str, DatatypeLibraryRegistration] | None = None,
) -> RelaxNGSchema:
    namespace = f' ns="{ns}"' if ns else ""
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="{name}"{namespace} '
            f'datatypeLibrary="{XSD}">{body}</element>'
        ),
        datatype_libraries=datatype_libraries,
    )
    schema.require_valid()
    return schema


def compile_grammar(body: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(io.StringIO(f'<grammar xmlns="{NS}">{body}</grammar>'))
    schema.require_valid()
    return schema


def parse(schema: RelaxNGSchema, xml: str, **kwargs: Any):
    document = schema.parse(io.StringIO(xml), **kwargs)
    document.require_valid()
    return document


def pydantic_models():
    pytest.importorskip("pydantic")
    from pyxsd.integrations.pydantic import models
    from pyxsd.relaxng.pydantic_adapter import RelaxNGModelSet

    return models, RelaxNGModelSet


def arrow_records():
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from pyxsd.integrations.arrow import records

    return pa, pq, records


def assert_representation_limit(error: IntegrationError, *needles: str) -> str:
    text = str(error)
    assert any(needle in text for needle in needles), text
    for forbidden in ("not implemented", "NotImplemented", "missing feature", "Task "):
        assert forbidden not in text, text
    return text


def variant_contains(schema: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Return whether one JSON Schema branch equals *expected*."""
    if all(schema.get(key) == value for key, value in expected.items()):
        return True
    return any(
        isinstance(branch, dict) and variant_contains(branch, expected)
        for branch in schema.get("anyOf", ())
    )


# ---------------------------------------------------------------------------
# 1. Matched XSD and RELAX NG Pydantic contracts
# ---------------------------------------------------------------------------


def test_matched_flat_record_models_share_alias_field_and_default_contract():
    models, RelaxNGModelSet = pydantic_models()
    xsd_schema = compile_schema(MATCHED_XSD)
    rng_schema = compile_element(MATCHED_RNG_RECORD, name="record")

    xsd_registry = models(xsd_schema)
    rng_registry = models(rng_schema)
    assert isinstance(rng_registry, RelaxNGModelSet)

    XRoot = xsd_registry.model_for(element="record")
    RRoot = rng_registry.model_for(element="record")
    assert RRoot is rng_registry.model_for(element="record")
    assert set(XRoot.model_fields) == set(RRoot.model_fields) == {"attr_id", "label", "n"}
    for name in ("attr_id", "label", "n"):
        xsd_field = XRoot.model_fields[name]
        rng_field = RRoot.model_fields[name]
        assert xsd_field.alias == rng_field.alias
        assert xsd_field.is_required() == rng_field.is_required()
        assert xsd_field.default_factory is rng_field.default_factory
        if xsd_field.default_factory is None:
            assert xsd_field.default is rng_field.default
    assert XRoot.model_fields["label"].default is None
    assert XRoot.model_fields["n"].default_factory is list
    assert set(XRoot.model_json_schema()["required"]) == {"@id"}
    assert XRoot.model_json_schema()["required"] == RRoot.model_json_schema()["required"]

    common = {"@id": 7, "label": "x", "n": [1, 2]}
    xsd_model = XRoot.model_validate(common)
    rng_model = RRoot.model_validate(common)
    assert xsd_model.model_dump(by_alias=True) == rng_model.model_dump(by_alias=True) == common
    assert xsd_model.model_dump(mode="json", by_alias=True) == rng_model.model_dump(
        mode="json", by_alias=True
    )

    xsd_document = xsd_schema.parse(
        io.StringIO('<record id="7"><label>x</label><n>1</n><n>2</n></record>')
    )
    rng_document = parse(rng_schema, '<record id="7"><label>x</label><n>1</n><n>2</n></record>')
    xsd_snapshot = xsd_registry.from_document(xsd_document)
    rng_snapshot = rng_registry.from_document(rng_document)
    assert (
        xsd_snapshot.model_dump(by_alias=True) == rng_snapshot.model_dump(by_alias=True) == common
    )

    xsd_schema_json = XRoot.model_json_schema()
    rng_schema_json = RRoot.model_json_schema()
    assert set(xsd_schema_json["properties"]) == set(rng_schema_json["properties"])
    assert xsd_schema_json["properties"]["@id"]["type"] == "integer"
    assert variant_contains(rng_schema_json["properties"]["@id"], {"type": "integer"})
    assert xsd_schema_json["properties"]["n"]["type"] == "array"
    assert rng_schema_json["properties"]["n"]["type"] == "array"
    assert xsd_schema_json["properties"]["n"]["minItems"] == 0
    assert rng_schema_json["properties"]["n"]["minItems"] == 0
    assert xsd_schema_json["additionalProperties"] is False
    assert rng_schema_json["additionalProperties"] is False
    serialized = json.dumps(rng_schema_json)
    assert "Task " not in serialized
    assert "not implemented" not in serialized

    local_definitions = {
        name: definition
        for name, definition in rng_schema_json["$defs"].items()
        if definition.get("x-pyxsd-scope") == "local"
    }
    assert {"RelaxNGModel_label", "RelaxNGModel_n"} <= set(local_definitions)
    for name, definition in local_definitions.items():
        validation = definition["x-pyxsd-validation"]
        assert "local route" in validation, name
        assert "ancestor and sibling constraints are not validated" in validation, name
        assert "Task " not in validation, name
        assert "not implemented" not in validation, name


def test_matched_whole_element_scalar_models_agree_on_common_input_and_documents():
    models, _ = pydantic_models()
    xsd_schema = compile_schema('<xs:element name="value" type="xs:long"/>')
    rng_schema = compile_element('<data type="long"/>', name="value")

    XRoot = models(xsd_schema).model_for(element="value")
    RRoot = models(rng_schema).model_for(element="value")
    assert set(XRoot.model_fields) == set(RRoot.model_fields) == {"root"}
    assert XRoot.model_fields["root"].is_required()
    assert RRoot.model_fields["root"].is_required()

    xsd_model = XRoot.model_validate(42)
    rng_model = RRoot.model_validate(42)
    assert xsd_model.model_dump() == rng_model.model_dump() == 42
    assert xsd_model.model_dump(mode="json") == rng_model.model_dump(mode="json") == 42
    assert xsd_model.model_dump(by_alias=True) == rng_model.model_dump(by_alias=True) == 42

    xsd_snapshot = models(xsd_schema).from_document(
        xsd_schema.parse(io.StringIO("<value>42</value>"))
    )
    rng_snapshot = models(rng_schema).from_document(parse(rng_schema, "<value>42</value>"))
    assert xsd_snapshot.model_dump() == rng_snapshot.model_dump() == 42

    xsd_json = XRoot.model_json_schema()
    rng_json = RRoot.model_json_schema()
    assert xsd_json["type"] == "integer"
    assert variant_contains(rng_json, {"type": "integer"})
    assert rng_json["x-pyxsd-cardinality"] == {"minimum": 1, "maximum": 1}


# ---------------------------------------------------------------------------
# 2. Standalone and native route constraints
# ---------------------------------------------------------------------------


def test_ambiguous_whole_element_scalar_keeps_snapshot_but_rejects_bare_mutations():
    pydantic = pytest.importorskip("pydantic")
    models, _ = pydantic_models()
    schema = compile_element('<choice><data type="integer"/><data type="string"/></choice>')
    model_set = models(schema)
    Root = model_set.model_for(element="doc")

    model = Root.model_validate("12")
    assert isinstance(model.root, AlternativeValue)
    assert model.root.lexical == "12"
    assert {candidate.contract.type_name for candidate in model.root.candidates} == {
        "integer",
        "string",
    }
    created = model_set.to_document(model)
    assert created.is_valid
    assert created.to_string() == "<doc>12</doc>"

    mutated = Root.model_validate("12")
    mutated.root = 12
    with pytest.raises(pydantic.ValidationError, match="ambiguous RELAX NG scalar"):
        model_set.to_document(mutated)
    with pytest.raises(pydantic.ValidationError, match="ambiguous RELAX NG scalar"):
        model_set.to_document(Root.model_construct(root=12))
    with pytest.raises(pydantic.ValidationError, match="scalar-tag-invalid"):
        model_set.to_document(Root.model_construct(root={"#value": {}}))
    with pytest.raises(pydantic.ValidationError):
        Root.model_validate(True)


def test_equivalent_derivations_share_one_class_and_one_candidate_identity():
    models, _ = pydantic_models()
    child_schema = compile_element(
        "<choice><element name='a'><data type='integer'/></element>"
        "<element name='a'><data type='integer'/></element></choice>"
    )
    registry = models(child_schema)
    Root = registry.model_for(element="doc")
    A = registry.model_for(element="doc", path=("a",))
    assert A is registry.model_for(element="doc", path=("a",))
    assert Root.model_fields["a"].annotation is A
    assert Root.model_validate({"a": 12}).a.root == 12

    first_schema = compile_element('<choice><data type="integer"/><data type="integer"/></choice>')
    second_schema = compile_element('<choice><data type="integer"/><data type="integer"/></choice>')
    First = models(first_schema).model_for(element="doc")
    Second = models(second_schema).model_for(element="doc")
    assert First.model_validate("12").root == 12
    assert Second.model_validate("12").root == 12
    first_route = first_schema.projection.routes[first_schema.projection.roots[0]]
    second_route = second_schema.projection.routes[second_schema.projection.roots[0]]
    first_ids = {
        candidate_id(first_schema._grammar_fingerprint, contract)
        for contract in first_route.content.scalar_contracts
    }
    second_ids = {
        candidate_id(second_schema._grammar_fingerprint, contract)
        for contract in second_route.content.scalar_contracts
    }
    assert len(first_ids) == 1
    assert first_ids == second_ids


def test_recursive_grammar_builds_finite_forward_references():
    models, _ = pydantic_models()
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
    assert Node is registry.model_for(element="node")
    assert get_args(Node.model_fields["node"].annotation) == (Node,)
    assert Node.model_fields["node"].default_factory is list
    model = Node.model_validate({"@label": "root", "node": [{"node": []}]})
    assert model.attr_label == "root"
    assert model.node[0].node == []
    assert Node.__relaxng_layout__ == "repeated"
    assert Node.model_json_schema()["$defs"]["RelaxNGModel_node"]["x-pyxsd-layout"] == "repeated"


def test_correlated_choice_is_accepted_only_jointly():
    pydantic = pytest.importorskip("pydantic")
    models, _ = pydantic_models()
    schema = compile_element(
        """
        <choice>
          <group>
            <element name="x"><data type="integer"/></element>
            <element name="y"><data type="integer"/></element>
          </group>
          <group>
            <element name="x"><data type="boolean"/></element>
            <element name="y"><data type="boolean"/></element>
          </group>
        </choice>
        """
    )
    Root = models(schema).model_for(element="doc")
    integer = Root.model_validate({"x": "12", "y": "34"})
    boolean = Root.model_validate({"x": "true", "y": "false"})
    assert [item.root.as_candidate for item in (integer.x, integer.y)]
    assert {candidate.contract.type_name for candidate in integer.x.root.candidates} == {"integer"}
    assert {candidate.contract.type_name for candidate in boolean.x.root.candidates} == {"boolean"}

    with pytest.raises(pydantic.ValidationError, match="invalid-scalar"):
        Root.model_validate({"x": "12", "y": "true"})
    with pytest.raises(pydantic.ValidationError, match="invalid-scalar"):
        Root.model_validate({"x": "true", "y": "12"})
    with pytest.raises(pydantic.ValidationError, match="ambiguous RELAX NG scalar"):
        Root.model_validate({"x": 12, "y": 34})


def test_invalid_direct_construction_is_rejected_by_native_route_validation():
    pydantic = pytest.importorskip("pydantic")
    models, _ = pydantic_models()
    schema = compile_element(
        '<attribute name="n"><data type="integer"/></attribute>'
        '<element name="item"><data type="integer"/></element>'
    )
    model_set = models(schema)
    Root = model_set.model_for(element="doc")

    for data in (
        {},
        {"@n": 1},
        {"@n": 1, "item": 2, "unknown": 3},
        {"@n": "not-an-integer", "item": 2},
        {"@n": 1, "item": None},
        {"@n": True, "item": 2},
    ):
        with pytest.raises(pydantic.ValidationError):
            Root.model_validate(data)
    with pytest.raises(pydantic.ValidationError):
        Root(**{"@n": 1, "item": 2, "extra": 3})
    with pytest.raises(pydantic.ValidationError):
        model_set.to_document(Root.model_construct())


# ---------------------------------------------------------------------------
# 3. Document snapshots, capture independence, freshness, and open names
# ---------------------------------------------------------------------------


def test_document_round_trip_capture_and_snapshot_freshness():
    models, _ = pydantic_models()
    schema = compile_element(
        """
        <attribute name="version"><data type="integer"/></attribute>
        <element name="x">
          <choice><data type="integer"/><data type="string"/></choice>
        </element>
        <zeroOrMore><element name="item"><text/></element></zeroOrMore>
        """
    )
    model_set = models(schema)
    xml = '<doc version="7"><x>12</x><item>a</item><item>b</item></doc>'
    plain = parse(schema, xml)
    captured = parse(schema, xml, capture="patterns")
    assert plain.typed and captured.typed

    plain_model = model_set.from_document(plain)
    captured_model = model_set.from_document(captured)
    plain_ids = tuple(candidate.candidate_id for candidate in plain_model.x.root.candidates)
    captured_ids = tuple(candidate.candidate_id for candidate in captured_model.x.root.candidates)
    assert plain_ids == captured_ids
    assert plain_model.model_dump(by_alias=True) == captured_model.model_dump(by_alias=True)

    created = model_set.to_document(plain_model)
    assert created.is_valid and created.interpretations_current
    parsed_again = model_set.from_document(created)
    assert parsed_again.model_dump(by_alias=True) == plain_model.model_dump(by_alias=True)
    assert (
        tuple(candidate.candidate_id for candidate in parsed_again.x.root.candidates) == plain_ids
    )

    plain.root.set_attribute("version", "9")
    plain.root.children[0].set_text("changed")
    assert plain_model.attr_version == 7
    assert plain_model.x.root.lexical == "12"
    assert [item.root for item in plain_model.item] == ["a", "b"]
    with pytest.raises(StaleInterpretationError, match="stale"):
        model_set.from_document(plain)
    with pytest.raises(StaleInterpretationError):
        prepare_document(schema, plain, False)

    fresh = model_set.from_document(plain, revalidate=True)
    assert fresh.attr_version == 9
    assert fresh.x.root.lexical == "changed"
    assert plain_model.attr_version == 7
    assert plain_model.x.root.lexical == "12"
    assert model_set.to_document(plain_model).to_string() == xml


def test_validation_only_and_invalid_documents_fail_explicitly():
    models, _ = pydantic_models()
    schema = compile_element('<attribute name="n"><data type="integer"/></attribute>')
    model_set = models(schema)

    validation_only = schema.parse(io.StringIO('<doc n="1"/>'), typed=False)
    assert validation_only.is_valid
    with pytest.raises(PyXSDError, match="typed parse"):
        model_set.from_document(validation_only)

    invalid = schema.parse(io.StringIO('<doc n="not-an-integer"/>'))
    assert not invalid.is_valid
    with pytest.raises(ValidationError):
        model_set.from_document(invalid)

    foreign = compile_element('<attribute name="n"><data type="integer"/></attribute>')
    with pytest.raises(IntegrationError, match="another compiled schema"):
        model_set.from_document(parse(foreign, '<doc n="1"/>'))


def test_open_name_start_requires_an_explicit_spelling_for_document_creation():
    models, _ = pydantic_models()
    schema = compile_grammar(
        '<start><element><anyName><except><name ns="urn:no">bad</name></except>'
        "</anyName><text/></element></start>"
    )
    model_set = models(schema)
    Root = model_set.model_for(element="doc")
    model = Root.model_validate("value")

    with pytest.raises(IntegrationError, match="explicit element"):
        model_set.to_document(model)
    with pytest.raises(IntegrationError, match="not admitted"):
        model_set.to_document(model, element="{urn:no}bad")
    created = model_set.to_document(model, element="doc")
    assert created.is_valid
    assert created.to_string() == "<doc>value</doc>"
    expanded = model_set.to_document(model, element="Q{urn:yes}doc")
    assert expanded.is_valid and expanded.root._name_ == "{urn:yes}doc"

    snapshot = model_set.from_document(parse(schema, "<doc>value</doc>"))
    with pytest.raises(IntegrationError, match="explicit element"):
        model_set.to_document(snapshot)


# ---------------------------------------------------------------------------
# 4. Scalar-tag forgery and XSD shape isolation
# ---------------------------------------------------------------------------


def test_forged_candidate_grammar_and_context_tags_are_rejected():
    schema = compile_element('<choice><data type="integer"/><data type="string"/></choice>')
    document = parse(schema, "<doc>12</doc>")
    good = document.to_data(format="pyxsd-json", fidelity="strict")
    assert good["doc"]["#value"]["version"] == 1

    cases = [
        ("grammar", "urn:forged", "scalar-tag-grammar-mismatch"),
        ("candidates", ["forged-id"], "scalar-tag-candidates-mismatch"),
        (
            "candidates",
            sorted(good["doc"]["#value"]["candidates"] + ["forged-id"]),
            "scalar-tag-candidates-mismatch",
        ),
    ]
    for field, value, code in cases:
        forged = copy.deepcopy(good)
        forged["doc"]["#value"][field] = value
        for load in (
            lambda data=forged: schema.from_data(data, format="pyxsd-json"),
            lambda data=forged: schema.from_json(json.dumps(data), format="pyxsd-json"),
        ):
            with pytest.raises(PyXSDError, match=code):
                load()

    for mutate in (
        lambda context: context.__setitem__("extra", "x"),
        lambda context: context.__setitem__("base_uri", 7),
        lambda context: context["namespaces"].__setitem__("xml", "urn:x"),
    ):
        forged = copy.deepcopy(good)
        mutate(forged["doc"]["#value"]["context"])
        for load in (
            lambda data=forged: schema.from_data(data, format="pyxsd-json"),
            lambda data=forged: schema.from_json(json.dumps(data), format="pyxsd-json"),
        ):
            with pytest.raises(PyXSDError, match="scalar-tag-context"):
                load()

    qname_schema = compile_element('<choice><data type="QName"/><data type="string"/></choice>')
    qname_document = parse(qname_schema, '<doc xmlns:p="urn:a">p:x</doc>')
    qname_data = qname_document.to_data(format="pyxsd-json", fidelity="strict")
    forged = copy.deepcopy(qname_data)
    forged["doc"]["#value"]["context"] = {"base_uri": None, "namespaces": {}}
    with pytest.raises(PyXSDError, match="scalar-tag-candidates-mismatch"):
        qname_schema.from_data(forged, format="pyxsd-json")

    imported = schema.from_data(good, format="pyxsd-json")
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == good
    from_json = schema.from_json(json.dumps(good), format="pyxsd-json")
    assert from_json.to_data(format="pyxsd-json", fidelity="strict") == good


def test_xsd_import_rejects_relaxng_tag_shapes():
    schema = compile_element('<choice><data type="integer"/><data type="string"/></choice>')
    tagged = parse(schema, "<doc>12</doc>").to_data(format="pyxsd-json", fidelity="strict")
    assert "#value" in json.dumps(tagged)

    xsd_schema = compile_schema('<xs:element name="doc" type="xs:int"/>')
    with pytest.raises(PyXSDError):
        xsd_schema.from_data(tagged, format="pyxsd-json")
    with pytest.raises(PyXSDError):
        xsd_schema.from_json(json.dumps(tagged), format="pyxsd-json")

    complex_xsd = compile_schema(
        '<xs:element name="doc"><xs:complexType><xs:sequence>'
        '<xs:element name="x" type="xs:int"/></xs:sequence>'
        "</xs:complexType></xs:element>"
    )
    plain = {"doc": [{"x": 1}]}
    assert (
        complex_xsd.from_data(plain, format="pyxsd-json").to_data(
            format="pyxsd-json", fidelity="strict"
        )
        == plain
    )
    with pytest.raises(PyXSDError):
        complex_xsd.from_data({"doc": [{"x": tagged["doc"]["#value"]}]}, format="pyxsd-json")
    assert complex_xsd.parse(io.StringIO("<doc><x>1</x></doc>")).to_dict() == {"x": 1}


# ---------------------------------------------------------------------------
# 5. Arrow rows, fixed schemas, Parquet, and datasets
# ---------------------------------------------------------------------------


def test_relaxng_explicit_columns_cover_scalars_attributes_and_repeated_rows(tmp_path):
    pa, pq, records = arrow_records()
    schema = compile_element(
        """
        <zeroOrMore><element name="item">
          <attribute name="id"><data type="long"/></attribute>
          <optional><element name="label"><data type="string"/></element></optional>
          <element name="value"><data type="int"/></element>
        </element></zeroOrMore>
        """
    )
    projection = records(
        schema,
        element="doc",
        path=("item",),
        columns={
            "id": FieldSource(attribute="id"),
            "label": FieldSource(path=("label",)),
            "value": FieldSource(path=("value",)),
        },
    )
    document = parse(
        schema,
        '<doc><item id="1"><label></label><value>10</value></item>'
        '<item id="2"><value>20</value></item></doc>',
    )
    assert projection.schema.names == ["id", "label", "value"]
    assert projection.schema.field("id").type == pa.int64()
    assert projection.schema.field("label").type == pa.string()
    assert projection.schema.field("value").type == pa.int32()
    assert projection.table(document, selector="item").to_pylist() == [
        {"id": 1, "label": "", "value": 10},
        {"id": 2, "label": None, "value": 20},
    ]
    batches = list(projection.batches(document, selector="item", batch_size=1))
    assert [batch.num_rows for batch in batches] == [1, 1]
    assert all(batch.schema.equals(projection.schema, check_metadata=True) for batch in batches)

    empty = parse(schema, "<doc/>")
    table = projection.table(empty, selector="item")
    assert table.num_rows == 0
    assert table.schema.equals(projection.schema, check_metadata=True)

    destination = tmp_path / "rows.parquet"
    projection.write_parquet(document, destination, selector="item", batch_size=1)
    assert (
        pq.read_table(destination).to_pylist()
        == projection.table(document, selector="item").to_pylist()
    )
    assert pq.read_schema(destination).equals(projection.schema, check_metadata=True)


def test_relaxng_absent_optional_empty_string_and_fixed_homogeneous_lists():
    pa, _, records = arrow_records()
    optional = compile_element(
        '<optional><attribute name="a"><text/></attribute></optional>'
        '<optional><element name="value"><data type="int"/></element></optional>'
    )
    projection = records(
        optional,
        element="doc",
        columns={"a": FieldSource(attribute="a"), "value": FieldSource(path=("value",))},
    )
    assert projection.table(parse(optional, "<doc/>")).to_pylist() == [{"a": None, "value": None}]
    assert projection.table(parse(optional, '<doc a=""><value>7</value></doc>')).to_pylist() == [
        {"a": "", "value": 7}
    ]

    empty_string = compile_element('<choice><data type="string"/><empty/></choice>')
    string_projection = records(empty_string, element="doc", columns={"value": FieldSource()})
    assert string_projection.table(parse(empty_string, "<doc/>")).to_pylist() == [{"value": ""}]
    assert string_projection.table(parse(empty_string, "<doc>hello</doc>")).to_pylist() == [
        {"value": "hello"}
    ]

    fixed_schema = compile_element('<list><data type="int"/><data type="int"/></list>')
    fixed = records(fixed_schema, element="doc", columns={"value": FieldSource()})
    fixed_document = parse(fixed_schema, "<doc>1 2</doc>")
    assert list(iter_relaxng_rows(fixed._plan, fixed_document)) == [{"value": (1, 2)}]
    assert fixed.table(fixed_document).to_pylist() == [{"value": [1, 2]}]
    assert fixed.schema.field("value").type == pa.list_(
        pa.field("item", pa.int32(), nullable=False)
    )

    homogeneous_schema = compile_element('<list><zeroOrMore><data type="int"/></zeroOrMore></list>')
    homogeneous = records(homogeneous_schema, element="doc", columns={"value": FieldSource()})
    assert homogeneous.table(parse(homogeneous_schema, "<doc/>")).to_pylist() == [{"value": []}]
    assert homogeneous.table(parse(homogeneous_schema, "<doc>1 2 3</doc>")).to_pylist() == [
        {"value": [1, 2, 3]}
    ]


def test_matched_xsd_and_relaxng_arrow_names_types_and_rows_agree():
    pa, _, records = arrow_records()
    xsd_schema = compile_schema(
        '<xs:element name="record"><xs:complexType><xs:sequence>'
        '<xs:element name="value" type="xs:string"/>'
        '<xs:element name="n" type="xs:long" minOccurs="0" maxOccurs="unbounded"/>'
        "</xs:sequence></xs:complexType></xs:element>"
    )
    rng_schema = compile_element(
        '<element name="value"><data type="string"/></element>'
        '<zeroOrMore><element name="n"><data type="long"/></element></zeroOrMore>',
        name="record",
    )
    xsd_projection = records(xsd_schema, element="record")
    rng_projection = records(rng_schema, element="record")
    assert (
        set(xsd_projection.schema.names)
        == set(rng_projection.schema.names)
        == {
            "value",
            "n",
        }
    )
    for name in ("value", "n"):
        assert xsd_projection.schema.field(name).type == rng_projection.schema.field(name).type
    assert xsd_projection.schema.field("value").type == pa.string()
    assert xsd_projection.schema.field("n").type == pa.list_(
        pa.field("item", pa.int64(), nullable=False)
    )

    xsd_document = xsd_schema.parse(
        io.StringIO("<record><value>x</value><n>1</n><n>2</n></record>")
    )
    rng_document = parse(rng_schema, "<record><value>x</value><n>1</n><n>2</n></record>")
    assert (
        xsd_projection.table(xsd_document).to_pylist()
        == rng_projection.table(rng_document).to_pylist()
        == [{"value": "x", "n": [1, 2]}]
    )

    xsd_attribute = compile_schema(
        '<xs:element name="record"><xs:complexType><xs:sequence>'
        '<xs:element name="value" type="xs:string"/></xs:sequence>'
        '<xs:attribute name="id" type="xs:long" use="required"/>'
        "</xs:complexType></xs:element>"
    )
    rng_attribute = compile_element(
        '<attribute name="id"><data type="long"/></attribute>'
        '<element name="value"><data type="string"/></element>',
        name="record",
    )
    columns = {"id": FieldSource(attribute="id")}
    xsd_columns = records(xsd_attribute, element="record", columns=columns)
    rng_columns = records(rng_attribute, element="record", columns=columns)
    assert xsd_columns.schema.field("id").type == rng_columns.schema.field("id").type == pa.int64()
    assert not xsd_columns.schema.field("id").nullable
    assert not rng_columns.schema.field("id").nullable
    assert (
        xsd_columns.table(
            xsd_attribute.parse(io.StringIO('<record id="7"><value>x</value></record>'))
        ).to_pylist()
        == rng_columns.table(
            parse(rng_attribute, '<record id="7"><value>x</value></record>')
        ).to_pylist()
        == [{"id": 7}]
    )


def test_matched_unbounded_integer_schema_and_arbitrary_precision_rows_agree(tmp_path):
    pa, pq, records = arrow_records()
    xsd_schema = compile_schema(
        '<xs:element name="record"><xs:complexType><xs:sequence>'
        '<xs:element name="value" type="xs:integer"/>'
        "</xs:sequence></xs:complexType></xs:element>"
    )
    rng_schema = compile_element(
        '<element name="value"><data type="integer"/></element>',
        name="record",
    )
    xsd_projection = records(xsd_schema, element="record")
    rng_projection = records(rng_schema, element="record")
    value = str(2**80)
    assert xsd_projection.schema.field("value").type == pa.string()
    assert rng_projection.schema.field("value").type == pa.string()
    assert (
        "unbounded integer range"
        in rng_projection.schema.field("value").metadata[b"pyxsd:arrow_fallback"].decode()
    )
    xsd_document = xsd_schema.parse(io.StringIO(f"<record><value>{value}</value></record>"))
    rng_document = parse(rng_schema, f"<record><value>{value}</value></record>")
    expected = [{"value": value}]
    assert xsd_projection.table(xsd_document).to_pylist() == expected
    assert rng_projection.table(rng_document).to_pylist() == expected

    columns = {"value": FieldSource(path=("value",))}
    xsd_columns = records(xsd_schema, element="record", columns=columns)
    rng_columns = records(rng_schema, element="record", columns=columns)
    assert xsd_columns.schema.field("value").type == rng_columns.schema.field("value").type
    assert xsd_columns.table(xsd_document).to_pylist() == expected
    assert rng_columns.table(rng_document).to_pylist() == expected

    destination = tmp_path / "unbounded-integer.parquet"
    rng_projection.write_parquet(rng_document, destination)
    assert pq.read_table(destination).to_pylist() == expected
    assert pq.read_schema(destination).field("value").type == pa.string()


def test_candidate_and_datatype_selectors_choose_one_contract():
    _, _, records = arrow_records()
    schema = compile_element('<choice><data type="long"/><data type="string"/></choice>')
    document = parse(schema, "<doc>12</doc>")
    route = schema.projection.routes[schema.projection.roots[0]]
    contracts = {contract.type_name: contract for contract in route.content.scalar_contracts}
    identifier = candidate_id(schema._grammar_fingerprint, contracts["string"])

    integer = records(schema, element="doc", columns={"value": FieldSource(datatype=(XSD, "long"))})
    string = records(schema, element="doc", columns={"value": FieldSource(candidate_id=identifier)})
    assert integer.table(document).to_pylist() == [{"value": 12}]
    assert string.table(document).to_pylist() == [{"value": "12"}]

    with pytest.raises(IntegrationError, match="ambiguous; set datatype/candidate_id"):
        records(schema, element="doc", columns={"value": FieldSource()})
    with pytest.raises(IntegrationError, match="mutually exclusive"):
        FieldSource(datatype=(XSD, "long"), candidate_id=identifier)
    with pytest.raises(IntegrationError, match="unknown"):
        records(
            schema,
            element="doc",
            columns={"value": FieldSource(candidate_id="missing-candidate")},
        )


def test_joint_incompatibility_nondecodable_and_stale_arrow_inputs_fail_closed():
    _, _, records = arrow_records()
    schema = compile_element(
        """
        <choice>
          <group>
            <element name="x"><data type="int"/></element>
            <element name="y"><data type="int"/></element>
          </group>
          <group>
            <element name="x"><data type="string"/></element>
            <element name="y"><data type="string"/></element>
          </group>
        </choice>
        """
    )
    conflicting = records(
        schema,
        element="doc",
        columns={
            "x": FieldSource(path=("x",), datatype=(XSD, "int")),
            "y": FieldSource(path=("y",), datatype=(XSD, "string")),
        },
    )
    document = parse(schema, "<doc><x>12</x><y>34</y></doc>")
    rows = iter_relaxng_rows(conflicting._plan, document)
    with pytest.raises(IntegrationError) as error:
        next(rows)
    message = str(error.value)
    assert "row 1" in message and "conflict" in message

    compatible = records(
        schema,
        element="doc",
        columns={
            "x": FieldSource(path=("x",), datatype=(XSD, "int")),
            "y": FieldSource(path=("y",), datatype=(XSD, "int")),
        },
    )
    assert compatible.table(parse(schema, "<doc><x>12</x><y>34</y></doc>")).to_pylist() == [
        {"x": 12, "y": 34}
    ]

    nondecodable_schema = compile_element(
        '<data type="opaque" datatypeLibrary="urn:nod"/>',
        datatype_libraries={
            "urn:nod": DatatypeLibraryRegistration(UndecodableFactory(), "nod/1"),
        },
    )
    nondecodable = records(nondecodable_schema, element="doc", columns={"value": FieldSource()})
    with pytest.raises(IntegrationError, match="no datatype decoder"):
        nondecodable.table(parse(nondecodable_schema, "<doc>12</doc>"))

    item_schema = compile_element(
        '<zeroOrMore><element name="item">'
        '<attribute name="id"><data type="int"/></attribute>'
        "</element></zeroOrMore>"
    )
    projection = records(
        item_schema,
        element="doc",
        path=("item",),
        columns={"id": FieldSource(attribute="id")},
    )
    stale = parse(item_schema, '<doc><item id="1"/></doc>')
    stale.root.children[0].set_attribute("id", "2")
    with pytest.raises(StaleInterpretationError):
        projection.table(stale, selector="item")
    assert projection.table(stale, selector="item", revalidate=True).to_pylist() == [{"id": 2}]

    invalid = item_schema.parse(io.StringIO('<doc><item id="bad"/></doc>'))
    assert not invalid.is_valid
    with pytest.raises(ValidationError):
        projection.table(invalid, selector="item")


def test_ambiguous_and_nondecodable_arrow_lists_fail_explicitly():
    _, _, records = arrow_records()
    unselected = compile_element(
        '<choice><list><data type="int"/></list><data type="string"/></choice>'
    )
    with pytest.raises(IntegrationError, match="ambiguous; set datatype/candidate_id"):
        records(unselected, element="doc", columns={"value": FieldSource()})

    heterogeneous = compile_element(
        '<list><choice><data type="integer"/><data type="string"/></choice></list>'
    )
    with pytest.raises(IntegrationError) as error:
        records(heterogeneous, element="doc", columns={"value": FieldSource()})
    assert_representation_limit(error.value, "whole-list", "shape")

    nondecodable_list = compile_element(
        '<list><zeroOrMore><data type="opaque" datatypeLibrary="urn:nod"/></zeroOrMore></list>',
        datatype_libraries={
            "urn:nod": DatatypeLibraryRegistration(UndecodableFactory(), "nod/1"),
        },
    )
    projection = records(nondecodable_list, element="doc", columns={"value": FieldSource()})
    with pytest.raises(IntegrationError, match="whole list is not decodable"):
        projection.table(parse(nondecodable_list, "<doc>1</doc>"))


def test_relaxng_dataset_manifest_and_row_failure_policy(tmp_path):
    _, pq, _ = arrow_records()
    from pyxsd.batch import DocumentSource
    from pyxsd.integrations.dataset import DatasetExportError

    schema = compile_element('<choice><data type="integer"/><data type="string"/></choice>')
    projection = RelaxNGRecordProjection(
        schema, element="doc", columns={"value": FieldSource(datatype=INT)}
    )
    source = tmp_path / "bad.xml"
    source.write_text("<doc>abc</doc>")
    sources = [DocumentSource("bad", source)]

    reported = projection.write_dataset(sources, tmp_path / "report", errors="report")
    metadata = json.loads(reported.manifest.read_text())
    assert metadata["schema_format"] == "relaxng"
    assert metadata["schema_version"] == "1.0"
    assert "xsd_version" not in metadata
    assert metadata["status"] == "failed"
    assert metadata["counts"] == {"inputs": 1, "succeeded": 0, "failed": 1, "rows": 0}
    entry = metadata["entries"][0]
    assert entry["status"] == "projection_error"
    assert entry["rows"] == 0 and entry["part"] is None
    assert "did not survive" in entry["failure"]["message"]
    assert list((reported.destination / "parts").iterdir()) == []
    reference = pq.read_schema(reported.destination / "_schema.parquet")
    assert reference.names[0] == "value"
    assert len(reference) == 3

    with pytest.raises(DatasetExportError) as error:
        projection.write_dataset(sources, tmp_path / "raise", errors="raise")
    assert error.value.stage == "projection"
    assert error.value.source_id == "bad"
    assert not (tmp_path / "raise").exists()
    assert not list(tmp_path.glob(".raise.pyxsd-*"))


class UndecodableDatatype:
    """A schema-registered datatype with acceptance but no decoder."""

    library_uri = "urn:nod"
    type_name = "opaque"
    parameters: tuple[object, ...] = ()
    white_space = "preserve"
    semantic_context: tuple[tuple[str, str], ...] = ()

    def allows(self, lexical: str, context: object) -> bool:
        del lexical, context
        return True


class UndecodableFactory(DatatypeFactory):
    def create(self, type_name: str, parameters: object, context: object):
        del type_name, parameters, context
        return UndecodableDatatype()


# ---------------------------------------------------------------------------
# 6. Unsupported shapes and representation limits
# ---------------------------------------------------------------------------


def test_unsupported_routes_name_representation_or_scope_limits():
    models, _ = pydantic_models()
    mixed = compile_element(
        '<mixed><zeroOrMore><element name="em"><text/></element></zeroOrMore></mixed>'
    )
    with pytest.raises(IntegrationError) as error:
        models(mixed).model_for(element="doc")
    assert_representation_limit(error.value, "mixed content", "cannot preserve")

    child_wildcard = compile_element(
        '<element name="known"><text/></element>'
        "<zeroOrMore><element><anyName><except><name>known</name></except></anyName>"
        "<text/></element></zeroOrMore>"
    )
    with pytest.raises(IntegrationError) as error:
        models(child_wildcard).model_for(element="doc")
    assert_representation_limit(error.value, "child wildcard", "arbitrary XML names")

    generic = compile_element(
        '<choice><data type="integer"/><element name="nested"><empty/></element></choice>'
    )
    with pytest.raises(IntegrationError) as error:
        models(generic).model_for(element="doc")
    assert_representation_limit(error.value, "generic", "cannot represent")

    pytest.importorskip("pyarrow")
    recursive = compile_grammar(
        """
        <start><ref name="node"/></start>
        <define name="node"><element name="node">
          <optional><ref name="node"/></optional>
        </element></define>
        """
    )
    with pytest.raises(IntegrationError) as error:
        RelaxNGRecordProjection(recursive, element="node")
    assert_representation_limit(error.value, "recursive", "infinite struct")

    ambiguous = compile_element(
        '<element name="x"><choice><data type="integer"/><text/></choice></element>'
    )
    with pytest.raises(IntegrationError) as error:
        RelaxNGRecordProjection(ambiguous, element="doc")
    assert_representation_limit(error.value, "ambiguous", "set datatype/candidate_id")

    local_schema = compile_element('<element name="item"><text/></element>')
    local_set = models(local_schema)
    local_model = local_set.model_for(element="doc", path=("item",))
    local_document = parse(local_schema, "<doc><item>x</item></doc>")
    item_model = local_set.from_node(local_document.root.children[0], element="doc", path=("item",))
    with pytest.raises(IntegrationError, match="local scope") as error:
        local_set.to_document(item_model)
    assert_representation_limit(error.value, "local scope", "document start")

    start_model = local_set.from_document(local_document)
    local_route = local_schema.projection.routes[local_model.__relaxng_route_id__]
    with pytest.raises(IntegrationError, match="local scope"):
        local_set.to_document(start_model, route=local_route.route_id)

    duplicate = compile_element(
        "<choice><element name='a'><text/></element><element name='a'><text/></element></choice>"
    )
    Duplicate = models(duplicate).model_for(element="doc")
    assert Duplicate.model_validate({"a": "x"}).a.root == "x"
    duplicate_projection = RelaxNGRecordProjection(duplicate, element="doc")
    assert duplicate_projection.table(parse(duplicate, "<doc><a>x</a></doc>")).to_pylist() == [
        {"a": "x"}
    ]


# ---------------------------------------------------------------------------
# 7. Candidate identity and integration metadata
# ---------------------------------------------------------------------------


def test_candidate_ids_are_grammar_scoped_and_value_independent():
    _, _, records = arrow_records()
    base = compile_element(
        '<choice><value type="int">1</value><value type="int">2</value></choice>'
    )
    changed = compile_element(
        '<attribute name="extra"><text/></attribute>'
        '<choice><value type="int">1</value><value type="int">2</value></choice>'
    )
    base_route = base.projection.routes[base.projection.roots[0]]
    changed_route = changed.projection.routes[changed.projection.roots[0]]
    base_ids = {
        candidate_id(base._grammar_fingerprint, contract)
        for contract in base_route.content.scalar_contracts
    }
    changed_ids = {
        candidate_id(changed._grammar_fingerprint, contract)
        for contract in changed_route.content.scalar_contracts
    }
    assert base_ids and changed_ids
    assert base_ids.isdisjoint(changed_ids)

    value_schema = compile_element('<choice><data type="integer"/><data type="string"/></choice>')
    first = parse(value_schema, "<doc>1</doc>")
    second = parse(value_schema, "<doc>2</doc>")
    first_value = first.root.scalar_value()
    second_value = second.root.scalar_value()
    assert isinstance(first_value, AlternativeValue)
    assert isinstance(second_value, AlternativeValue)
    assert tuple(item.candidate_id for item in first_value.candidates) == tuple(
        item.candidate_id for item in second_value.candidates
    )

    contract = next(
        contract
        for contract in value_schema.projection.routes[
            value_schema.projection.roots[0]
        ].content.scalar_contracts
        if contract.type_name == "integer"
    )
    identifier = candidate_id(value_schema._grammar_fingerprint, contract)
    projection = records(
        value_schema, element="doc", columns={"value": FieldSource(candidate_id=identifier)}
    )
    metadata = projection.schema.field("value").metadata
    assert metadata[b"pyxsd:candidate"] == identifier.encode()
    assert json.loads(metadata[b"pyxsd:contract"])["type_name"] == "integer"


def test_arrow_source_metadata_round_trips_selectors_and_xsd_metadata_is_unchanged():
    _, _, records = arrow_records()
    schema = compile_element('<choice><data type="integer"/><data type="string"/></choice>')
    route = schema.projection.routes[schema.projection.roots[0]]
    contracts = {contract.type_name: contract for contract in route.content.scalar_contracts}
    identifier = candidate_id(schema._grammar_fingerprint, contracts["string"])

    for source, expected in (
        (FieldSource(datatype=INT), {"datatype": [XSD, "integer"]}),
        (FieldSource(candidate_id=identifier), {"candidate_id": identifier}),
    ):
        projection = records(schema, element="doc", columns={"value": source})
        raw = projection.schema.field("value").metadata[b"pyxsd:source"]
        data = json.loads(raw)
        assert list(data) == ["scope", "path", "attribute", "levels", *expected]
        assert all(data[key] == value for key, value in expected.items())
        if source.datatype is not None:
            assert "candidate_id" not in data
            assert tuple(data["datatype"]) == source.datatype
        else:
            assert "datatype" not in data
    plain = records(schema, element="doc", columns={"value": FieldSource(datatype=INT)})
    assert field_source_metadata(FieldSource(datatype=INT))["datatype"] == INT
    assert (
        tuple(json.loads(plain.schema.field("value").metadata[b"pyxsd:source"])["datatype"]) == INT
    )

    xsd_schema = compile_schema(
        '<xs:element name="record"><xs:complexType><xs:sequence>'
        '<xs:element name="value" type="xs:int"/></xs:sequence>'
        '<xs:attribute name="id" type="xs:int" use="required"/>'
        "</xs:complexType></xs:element>"
    )
    xsd_projection = records(
        xsd_schema, element="record", columns={"id": FieldSource(attribute="id")}
    )
    assert xsd_projection.schema.metadata == {
        b"pyxsd:projection": b"2",
        b"pyxsd:mode": b"columns",
        b"pyxsd:declaration": b'["record"]',
        b"pyxsd:presence": (
            b"absent/nil scalar columns collapse; attributes include effective defaults"
        ),
        b"pyxsd:exactness": (b"numerical value; temporal strings; no original lexical provenance"),
    }
    field_metadata = xsd_projection.schema.field("id").metadata
    assert json.loads(field_metadata[b"pyxsd:source"]) == {
        "scope": "record",
        "path": [],
        "attribute": "id",
        "levels": 0,
    }
    assert all(b"datatype" not in key and b"candidate_id" not in key for key in field_metadata)
    assert b"pyxsd:format" not in xsd_projection.schema.metadata
    assert b"pyxsd:grammar" not in xsd_projection.schema.metadata


def test_prepare_document_requires_matching_schema_and_typed_relaxng_evidence():
    schema = compile_element("<empty/>")
    document = parse(schema, "<doc/>")
    assert prepare_document(schema, document, False) is document
    fresh = prepare_document(schema, document, True)
    assert isinstance(fresh, RelaxNGDocument)
    assert fresh is not document and fresh.interpretations_current
    other = compile_element("<empty/>")
    with pytest.raises(IntegrationError, match="another compiled schema"):
        prepare_document(other, document, False)
    not_schema = object()
    fake = SimpleNamespace(schema=not_schema)
    with pytest.raises(TypeError, match="compiled XSD or RELAX NG schema"):
        prepare_document(not_schema, fake, False)  # type: ignore[arg-type]
