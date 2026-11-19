"""Current-evidence RELAX NG rows and fixed Arrow batches.

Rows are materialized only from the accepting evidence of a current valid
typed document.  A selection is never inferred from a Python value, and a row
is yielded only after every selected contract is jointly compatible with the
document's accepting derivation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from pyxsd.exceptions import PyXSDError, ValidationError  # noqa: E402
from pyxsd.integrations import IntegrationError  # noqa: E402
from pyxsd.integrations.projection import FieldSource  # noqa: E402
from pyxsd.relaxng.contracts import ScalarContract, candidate_id  # noqa: E402
from pyxsd.relaxng.datatypes import (  # noqa: E402
    DatatypeFactory,
    DatatypeLibraryRegistration,
)
from pyxsd.relaxng.errors import StaleInterpretationError  # noqa: E402
from pyxsd.relaxng.evidence import EvidenceGraph  # noqa: E402
from pyxsd.relaxng.names import Name  # noqa: E402
from pyxsd.relaxng.records import (  # noqa: E402
    RelaxNGRecordProjection,
    _context,
    _convert_value,
    _node_matches_route,
    _node_route_id,
    _read_selected_scalar,
    iter_relaxng_rows,
)
from pyxsd.relaxng.schema import RelaxNGSchema  # noqa: E402
from pyxsd.xsd_data_types import QName  # noqa: E402

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
INT = (XSD, "int")
STRING = (XSD, "string")


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


def typed(schema: RelaxNGSchema, xml: str | object):
    if isinstance(xml, str):
        xml = io.StringIO(xml)
    document = schema.parse(xml, typed=True)
    document.require_valid()
    return document


def rows(projection: RelaxNGRecordProjection, document, **kwargs):
    return list(iter_relaxng_rows(projection._plan, document, **kwargs))


def projection(
    schema: RelaxNGSchema,
    *,
    element: str = "doc",
    path: tuple[str, ...] = (),
    columns: dict[str, FieldSource],
) -> RelaxNGRecordProjection:
    return RelaxNGRecordProjection(schema, element=element, path=path, columns=columns)


def test_unique_scalar_attributes_and_child_columns_are_typed():
    schema = compile_element(
        """
        <attribute name="version"><data type="int"/></attribute>
        <attribute name="flag"><data type="boolean"/></attribute>
        <attribute name="amount">
          <data type="decimal">
            <param name="totalDigits">5</param>
            <param name="fractionDigits">2</param>
          </data>
        </attribute>
        <attribute name="when"><data type="date"/></attribute>
        <attribute name="ref"><data type="QName"/></attribute>
        <element name="name"><text/></element>
        <element name="count"><data type="int"/></element>
        """
    )
    record = projection(
        schema,
        columns={
            "version": FieldSource(attribute="version"),
            "flag": FieldSource(attribute="flag"),
            "amount": FieldSource(attribute="amount"),
            "when": FieldSource(attribute="when"),
            "ref": FieldSource(attribute="ref"),
            "name": FieldSource(path=("name",)),
            "count": FieldSource(path=("count",)),
        },
    )
    document = typed(
        schema,
        '<doc xmlns:p="urn:p" version="2" flag="true" amount="12.34" '
        'when="2024-01-02" ref="p:item"><name>Box</name><count>3</count></doc>',
    )
    assert rows(record, document) == [
        {
            "version": 2,
            "flag": True,
            "amount": Decimal("12.34"),
            "when": "2024-01-02",
            "ref": "{urn:p}item",
            "name": "Box",
            "count": 3,
        }
    ]
    table = record.table(document)
    assert table.schema.field("amount").type == pa.decimal128(5, 2)
    assert table.schema.field("when").type == pa.string()
    assert table.to_pylist() == rows(record, document)


def test_root_scalar_column_preserves_empty_string_against_absent_none():
    schema = compile_element('<choice><data type="string"/><empty/></choice>')
    record = projection(schema, columns={"value": FieldSource()})
    assert rows(record, typed(schema, "<doc>hello</doc>")) == [{"value": "hello"}]
    assert rows(record, typed(schema, "<doc/>")) == [{"value": None}]
    from xml.etree import ElementTree as ET

    element = ET.Element("doc")
    element.text = ""
    assert rows(record, typed(schema, element)) == [{"value": ""}]


def test_optional_attribute_path_and_text_yield_none_but_empty_is_a_value():
    schema = compile_element(
        """
        <optional><attribute name="opt"><text/></attribute></optional>
        <optional><element name="container"><element name="value"><data type="int"/></element></element></optional>
        <element name="name"><text/></element>
        """
    )
    record = projection(
        schema,
        columns={
            "opt": FieldSource(attribute="opt"),
            "value": FieldSource(path=("container", "value")),
            "name": FieldSource(path=("name",)),
        },
    )
    assert rows(record, typed(schema, "<doc><name/></doc>")) == [
        {"opt": None, "value": None, "name": None}
    ]
    assert rows(record, typed(schema, '<doc opt=""><name>x</name></doc>')) == [
        {"opt": "", "value": None, "name": "x"}
    ]
    assert rows(
        record,
        typed(
            schema,
            "<doc><container><value>3</value></container><name>x</name></doc>",
        ),
    ) == [{"opt": None, "value": 3, "name": "x"}]


def test_ambiguous_integer_string_selects_datatype_and_candidate_id():
    schema = compile_element('<choice><data type="int"/><data type="string"/></choice>')
    document = typed(schema, "<doc>12</doc>")
    integer = projection(schema, columns={"value": FieldSource(datatype=INT)})
    string = projection(schema, columns={"value": FieldSource(datatype=STRING)})
    assert rows(integer, document) == [{"value": 12}]
    assert type(rows(integer, document)[0]["value"]) is not str
    assert rows(string, document) == [{"value": "12"}]

    route = schema.projection.routes[schema.projection.roots[0]]
    contracts = {contract.type_name: contract for contract in route.content.scalar_contracts}
    for type_name, expected in (("int", 12), ("string", "12")):
        identifier = candidate_id(schema._grammar_fingerprint, contracts[type_name])
        selected = projection(schema, columns={"value": FieldSource(candidate_id=identifier)})
        assert rows(selected, document) == [{"value": expected}]


@dataclass(frozen=True)
class UndecodableDatatype:
    library_uri: str = "urn:nod"
    type_name: str = "opaque"
    parameters: tuple[object, ...] = ()
    white_space: str = "preserve"
    semantic_context: tuple[tuple[str, str], ...] = ()

    def allows(self, lexical: str, context: object) -> bool:
        del lexical, context
        return True

    def equal(self, left: str, right: str, context: object) -> bool:
        del context
        return left == right


class UndecodableFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return UndecodableDatatype()


def test_nondecodable_selected_candidate_is_a_column_error():
    schema = compile_element(
        '<data type="opaque" datatypeLibrary="urn:nod"/>',
        datatype_libraries={
            "urn:nod": DatatypeLibraryRegistration(UndecodableFactory(), "nod/1"),
        },
    )
    record = projection(schema, columns={"value": FieldSource()})
    document = typed(schema, "<doc>12</doc>")
    with pytest.raises(IntegrationError) as error:
        rows(record, document)
    message = str(error.value)
    assert "row 1" in message
    assert "column 'value'" in message
    assert "FieldSource" in message
    assert "declaration doc" in message
    assert "no datatype decoder" in message


def test_unknown_and_pruned_candidates_are_column_errors():
    schema = compile_element(
        '<choice><value type="int">1</value><value type="int">2</value></choice>'
    )
    route = schema.projection.routes[schema.projection.roots[0]]
    missing = next(
        contract
        for contract in route.content.scalar_contracts
        if getattr(schema.projection.encoding_patterns[contract.pattern_identity], "lexical", None)
        == "2"
    )
    identifier = candidate_id(schema._grammar_fingerprint, missing)
    record = projection(schema, columns={"value": FieldSource(candidate_id=identifier)})
    document = typed(schema, "<doc>1</doc>")
    with pytest.raises(IntegrationError) as error:
        rows(record, document)
    message = str(error.value)
    assert "row 1" in message
    assert "column 'value'" in message
    assert "did not survive" in message
    assert "declaration doc" in message


def test_fixed_homogeneous_and_empty_lists_keep_their_declared_shapes():
    fixed_schema = compile_element(
        '<list><group><data type="int"/><data type="int"/></group></list>'
    )
    fixed = projection(fixed_schema, columns={"value": FieldSource()})
    fixed_rows = rows(fixed, typed(fixed_schema, "<doc>1 2</doc>"))
    assert fixed_rows == [{"value": (1, 2)}]
    assert type(fixed_rows[0]["value"]) is tuple
    assert fixed.table(typed(fixed_schema, "<doc>1 2</doc>")).to_pylist() == [{"value": [1, 2]}]

    list_schema = compile_element('<list><zeroOrMore><data type="int"/></zeroOrMore></list>')
    homogeneous = projection(list_schema, columns={"value": FieldSource()})
    assert rows(homogeneous, typed(list_schema, "<doc>1 2 3</doc>")) == [{"value": [1, 2, 3]}]
    assert rows(homogeneous, typed(list_schema, "<doc/>")) == [{"value": []}]
    assert homogeneous.table(typed(list_schema, "<doc/>")).to_pylist() == [{"value": []}]


def correlated_schema(*, repeated: bool) -> RelaxNGSchema:
    choice = """<choice>
        <group>
          <element name="x"><data type="int"/></element>
          <element name="y"><data type="int"/></element>
        </group>
        <group>
          <element name="x"><data type="string"/></element>
          <element name="y"><data type="string"/></element>
        </group>
      </choice>"""
    if not repeated:
        return compile_element(choice)
    return compile_element(f'<zeroOrMore><element name="row">{choice}</element></zeroOrMore>')


def test_jointly_incompatible_selected_columns_raise_before_yield():
    schema = correlated_schema(repeated=False)
    document = typed(schema, "<doc><x>12</x><y>34</y></doc>")
    conflicting = projection(
        schema,
        columns={
            "x": FieldSource(path=("x",), datatype=INT),
            "y": FieldSource(path=("y",), datatype=STRING),
        },
    )
    with pytest.raises(IntegrationError) as error:
        rows(conflicting, document)
    message = str(error.value)
    assert "row 1" in message
    assert "x" in message and "y" in message
    assert "conflict" in message

    compatible = projection(
        schema,
        columns={
            "x": FieldSource(path=("x",), datatype=INT),
            "y": FieldSource(path=("y",), datatype=INT),
        },
    )
    assert rows(compatible, document) == [{"x": 12, "y": 34}]


def test_failing_row_yields_no_partial_or_lexical_fallback():
    schema = correlated_schema(repeated=True)
    document = typed(
        schema,
        "<doc><row><x>1</x><y>2</y></row><row><x>a</x><y>b</y></row></doc>",
    )
    record = projection(
        schema,
        path=("row",),
        columns={
            "x": FieldSource(path=("x",), datatype=INT),
            "y": FieldSource(path=("y",), datatype=INT),
        },
    )
    stream = iter_relaxng_rows(record._plan, document, selector="row")
    assert next(stream) == {"x": 1, "y": 2}
    with pytest.raises(IntegrationError) as error:
        next(stream)
    message = str(error.value)
    assert "row 2" in message
    assert "column 'x'" in message
    assert "did not survive" in message
    assert "'a'" not in message
    with pytest.raises(StopIteration):
        next(stream)


def namespaced_rows_schema() -> RelaxNGSchema:
    return compile_grammar(
        f"""
        <start>
          <element name="orders" ns="urn:o" datatypeLibrary="{XSD}">
            <attribute name="version"><data type="int"/></attribute>
            <zeroOrMore>
              <element name="order" ns="urn:o">
                <attribute name="number"><data type="int"/></attribute>
                <element name="line" ns="urn:o">
                  <attribute name="sku"><text/></attribute>
                  <element name="quantity"><data type="int"/></element>
                </element>
              </element>
            </zeroOrMore>
          </element>
        </start>
        """
    )


def namespaced_rows_document(schema: RelaxNGSchema):
    return typed(
        schema,
        """
        <t:orders xmlns:t="urn:o" version="7">
          <t:order number="1"><t:line sku="A"><t:quantity>2</t:quantity></t:line></t:order>
          <t:order number="2"><t:line sku="B"><t:quantity>3</t:quantity></t:line></t:order>
        </t:orders>
        """,
    )


def test_namespaced_repeated_rows_and_root_ancestor_sources():
    schema = namespaced_rows_schema()
    document = namespaced_rows_document(schema)
    record = projection(
        schema,
        element="{urn:o}orders",
        path=("{urn:o}order", "{urn:o}line"),
        columns={
            "sku": FieldSource(attribute="sku"),
            "quantity": FieldSource(path=("{urn:o}quantity",)),
            "number": FieldSource(scope="ancestor", levels=1, attribute="number"),
            "version": FieldSource(scope="root", attribute="version"),
        },
    )
    assert rows(
        record,
        document,
        selector="t:order/t:line",
        namespaces={"t": "urn:o"},
    ) == [
        {"sku": "A", "quantity": 2, "number": 1, "version": 7},
        {"sku": "B", "quantity": 3, "number": 2, "version": 7},
    ]
    with pytest.raises(IntegrationError, match="selector"):
        rows(record, document)


def test_recursive_route_ancestor_cannot_climb_above_the_document_root():
    schema = compile_grammar(
        f"""
        <start>
          <element name="order" datatypeLibrary="{XSD}">
            <attribute name="id"><data type="int"/></attribute>
            <zeroOrMore><ref name="order"/></zeroOrMore>
          </element>
        </start>
        <define name="order">
          <element name="order" datatypeLibrary="{XSD}">
            <attribute name="id"><data type="int"/></attribute>
            <zeroOrMore><ref name="order"/></zeroOrMore>
          </element>
        </define>
        """
    )
    record = projection(
        schema,
        element="order",
        path=("order",),
        columns={
            "id": FieldSource(attribute="id"),
            "parent": FieldSource(scope="ancestor", levels=1, attribute="id"),
        },
    )
    document = typed(schema, '<order id="1"><order id="2"/></order>')
    with pytest.raises(IntegrationError) as error:
        rows(record, document, selector=".")
    assert "row 1" in str(error.value)
    assert "above" in str(error.value)
    assert rows(record, document, selector="order") == [{"id": 2, "parent": 1}]


def test_generic_wildcard_routes_match_by_name_class():
    schema = compile_grammar(
        "<start><element><anyName/><optional><element><anyName/><text/>"
        "</element></optional></element></start>"
    )
    record = projection(schema, columns={"value": FieldSource(path=("anything",))})
    document = typed(schema, "<doc><anything>x</anything></doc>")
    assert rows(record, document) == [{"value": "x"}]

    wildcard_row = projection(
        schema,
        path=("anything",),
        columns={"value": FieldSource()},
    )
    assert rows(wildcard_row, document, selector="anything") == [{"value": "x"}]


def test_mixed_selector_routes_fail_instead_of_reinterpreting_nodes():
    schema = compile_element(
        """
        <element name="orders">
          <zeroOrMore><element name="order">
            <element name="a"><text/></element>
          </element></zeroOrMore>
        </element>
        <element name="archive">
          <zeroOrMore><element name="order">
            <element name="b"><text/></element>
          </element></zeroOrMore>
        </element>
        """
    )
    record = projection(
        schema,
        path=("orders", "order"),
        columns={"a": FieldSource(path=("a",))},
    )
    document = typed(
        schema,
        "<doc><orders><order><a>A</a></order></orders>"
        "<archive><order><b>B</b></order></archive></doc>",
    )
    stream = iter_relaxng_rows(record._plan, document, selector=".//order")
    assert next(stream) == {"a": "A"}
    with pytest.raises(IntegrationError) as error:
        next(stream)
    message = str(error.value)
    assert "row 2" in message
    assert "does not match" in message


def test_wrong_selector_route_and_root_mismatch_fail():
    schema = compile_element(
        """
        <element name="order"><element name="value"><data type="int"/></element></element>
        <element name="header"><element name="code"><text/></element></element>
        """
    )
    record = projection(
        schema,
        path=("order",),
        columns={"value": FieldSource(path=("value",))},
    )
    document = typed(
        schema, "<doc><order><value>1</value></order><header><code>x</code></header></doc>"
    )
    with pytest.raises(IntegrationError, match="does not match"):
        rows(record, document, selector="header")
    root_plan = projection(
        schema,
        path=(),
        columns={"value": FieldSource(path=("order", "value"))},
    )
    with pytest.raises(IntegrationError, match="does not match"):
        rows(root_plan, document, selector="order")


def test_default_projection_primitive_record_and_repeated_fields():
    primitive_schema = compile_element('<data type="int"/>')
    primitive = RelaxNGRecordProjection(primitive_schema, element="doc")
    assert primitive.table(typed(primitive_schema, "<doc>12</doc>")).to_pylist() == [{"value": 12}]

    schema = compile_element(
        """
        <attribute name="version"><data type="int"/></attribute>
        <element name="count"><data type="int"/></element>
        <optional><element name="flag"><data type="boolean"/></element></optional>
        <zeroOrMore><element name="item"><data type="int"/></element></zeroOrMore>
        <element name="amount">
          <attribute name="currency"><text/></attribute>
          <data type="decimal">
            <param name="totalDigits">30</param>
            <param name="fractionDigits">0</param>
          </data>
        </element>
        <zeroOrMore>
          <element name="group">
            <element name="value"><data type="int"/></element>
          </element>
        </zeroOrMore>
        """
    )
    record = RelaxNGRecordProjection(schema, element="doc")
    document = typed(
        schema,
        '<doc version="1"><count>2</count><item>3</item><item>4</item>'
        '<amount currency="USD">12</amount>'
        "<group><value>5</value></group><group><value>6</value></group></doc>",
    )
    assert record.table(document).to_pylist() == [
        {
            "version": 1,
            "amount": {"currency": "USD", "$": Decimal("12")},
            "count": 2,
            "flag": None,
            "item": [3, 4],
            "group": [{"value": 5}, {"value": 6}],
        }
    ]


def test_default_projection_enforces_joint_compatibility(monkeypatch):
    schema = compile_element(
        """
        <element name="x"><data type="int"/></element>
        <element name="y"><data type="int"/></element>
        """
    )
    record = RelaxNGRecordProjection(schema, element="doc")
    document = typed(schema, "<doc><x>1</x><y>2</y></doc>")
    seen: list[tuple[object, ...]] = []
    original = EvidenceGraph.compatible

    def reject(self, selections):
        items = tuple(selections)
        seen.append(items)
        return False

    monkeypatch.setattr(EvidenceGraph, "compatible", reject)
    with pytest.raises(IntegrationError, match="conflict"):
        list(iter_relaxng_rows(record._plan, document))
    assert len(seen[0]) == 2
    monkeypatch.setattr(EvidenceGraph, "compatible", original)
    assert record.table(document).to_pylist() == [{"x": 1, "y": 2}]


def test_default_projection_selector_route_mismatch():
    schema = compile_element(
        """
        <element name="order"><element name="value"><data type="int"/></element></element>
        <element name="header"><element name="code"><text/></element></element>
        """
    )
    record = RelaxNGRecordProjection(schema, element="doc", path=("order",))
    document = typed(
        schema, "<doc><order><value>1</value></order><header><code>x</code></header></doc>"
    )
    with pytest.raises(IntegrationError, match="does not match"):
        list(iter_relaxng_rows(record._plan, document, selector="header"))
    with pytest.raises(IntegrationError, match="selector"):
        list(iter_relaxng_rows(record._plan, document))


def test_stale_invalid_validation_only_and_foreign_documents_fail_before_rows():
    schema = compile_element('<data type="int"/>')
    record = projection(schema, columns={"value": FieldSource()})
    current = typed(schema, "<doc>12</doc>")
    current.root.set_text("13")
    with pytest.raises(StaleInterpretationError):
        rows(record, current)

    invalid = schema.parse(io.StringIO("<doc>bad</doc>"), typed=True)
    with pytest.raises(ValidationError):
        rows(record, invalid)

    validation_only = schema.parse(io.StringIO("<doc>12</doc>"), typed=False)
    with pytest.raises(PyXSDError, match="typed"):
        rows(record, validation_only)

    other = compile_element('<data type="int"/>')
    with pytest.raises(IntegrationError, match="another compiled schema"):
        rows(record, typed(other, "<doc>12</doc>"))


def test_batches_keep_identical_schema_and_empty_tables_are_typed():
    schema = compile_element(
        '<zeroOrMore><element name="item"><attribute name="id"><data type="int"/></attribute>'
        "</element></zeroOrMore>"
    )
    record = projection(schema, path=("item",), columns={"id": FieldSource(attribute="id")})
    document = typed(schema, '<doc><item id="1"/><item id="2"/><item id="3"/></doc>')
    batches = list(record.batches(document, selector="item", batch_size=2))
    assert [batch.num_rows for batch in batches] == [2, 1]
    assert all(batch.schema.equals(record.schema, check_metadata=True) for batch in batches)
    assert record.table(document, selector="item").to_pylist() == [
        {"id": 1},
        {"id": 2},
        {"id": 3},
    ]
    empty = record.table(document, selector="missing")
    assert empty.num_rows == 0
    assert empty.schema.equals(record.schema, check_metadata=True)
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(IntegrationError, match="batch_size"):
            list(record.batches(document, selector="item", batch_size=invalid))


def test_parquet_round_trip_borrowed_sink_and_atomic_replacement(tmp_path):
    schema = compile_element(
        '<zeroOrMore><element name="item"><attribute name="id"><data type="int"/></attribute>'
        "</element></zeroOrMore>"
    )
    record = projection(schema, path=("item",), columns={"id": FieldSource(attribute="id")})
    document = typed(schema, '<doc><item id="1"/><item id="2"/></doc>')
    table = record.table(document, selector="item")

    destination = tmp_path / "rows.parquet"
    destination.write_bytes(b"old")
    record.write_parquet(document, destination, selector="item", batch_size=1)
    assert pq.read_table(destination).equals(table)

    sink = io.BytesIO()
    record.write_parquet(document, sink, selector="item")
    assert not sink.closed
    assert pq.read_table(pa.BufferReader(sink.getvalue())).to_pylist() == table.to_pylist()

    stale = typed(schema, '<doc><item id="3"/></doc>')
    stale.root.children[0].set_attribute("id", "4")
    before = destination.read_bytes()
    with pytest.raises(StaleInterpretationError):
        record.write_parquet(stale, destination, selector="item")
    assert destination.read_bytes() == before

    with pytest.raises(IntegrationError, match="Task 7"):
        record.write_dataset([], tmp_path / "dataset")


def test_float_binary_and_unbounded_decimal_arrow_values(tmp_path):
    schema = compile_element(
        """
        <attribute name="ratio"><data type="double"/></attribute>
        <attribute name="blob"><data type="base64Binary"/></attribute>
        <attribute name="money"><data type="decimal"/></attribute>
        """
    )
    record = projection(
        schema,
        columns={
            "ratio": FieldSource(attribute="ratio"),
            "blob": FieldSource(attribute="blob"),
            "money": FieldSource(attribute="money"),
        },
    )
    document = typed(schema, '<doc ratio="1.5" blob="AQI=" money="12.5"/>')
    assert rows(record, document) == [{"ratio": 1.5, "blob": b"\x01\x02", "money": "12.5"}]
    table = record.table(document)
    assert table.schema.field("ratio").type == pa.float64()
    assert table.schema.field("blob").type == pa.binary()
    assert table.schema.field("money").type == pa.string()
    destination = tmp_path / "fallbacks.parquet"
    with pytest.raises(IntegrationError, match="batch_size"):
        record.write_parquet(document, destination, batch_size=0)
    assert not destination.exists()


def atomic_contract(type_name: str) -> ScalarContract:
    return ScalarContract("atomic", XSD, type_name, (), (), f"pyxsd.test.pattern/{type_name}")


def text_contract() -> ScalarContract:
    return ScalarContract("text", None, None, (), (), "pyxsd.test.pattern/text")


class LexicalValue:
    def lexical(self) -> str:
        return "lexical"


@pytest.mark.parametrize(
    ("contract", "value", "arrow_shape", "expected"),
    [
        (text_contract(), "text", pa.string(), "text"),
        (atomic_contract("string"), "text", pa.string(), "text"),
        (atomic_contract("boolean"), True, pa.bool_(), True),
        (atomic_contract("int"), 3, pa.int64(), 3),
        (atomic_contract("double"), 1.5, pa.float64(), 1.5),
        (atomic_contract("decimal"), Decimal("1.5"), pa.decimal128(5, 1), Decimal("1.5")),
        (atomic_contract("decimal"), Decimal("1.5"), pa.string(), "1.5"),
        (atomic_contract("base64Binary"), b"ab", pa.binary(), b"ab"),
        (atomic_contract("date"), "2024-01-02", pa.string(), "2024-01-02"),
        (atomic_contract("custom"), LexicalValue(), pa.string(), "lexical"),
    ],
)
def test_arrow_scalar_conversion_accepts_explicit_value_kinds(
    contract, value, arrow_shape, expected
):
    assert _convert_value(contract, value, arrow_shape, "field") == expected


@pytest.mark.parametrize(
    ("contract", "value", "arrow_shape"),
    [
        (text_contract(), 1, pa.string()),
        (atomic_contract("QName"), "p:item", pa.string()),
        (atomic_contract("QName"), QName("p:item"), pa.string()),
        (atomic_contract("date"), object(), pa.string()),
        (atomic_contract("boolean"), True, pa.string()),
        (atomic_contract("int"), 1, pa.string()),
        (atomic_contract("double"), 1.5, pa.string()),
        (atomic_contract("decimal"), Decimal("1.5"), pa.int64()),
        (atomic_contract("base64Binary"), b"ab", pa.string()),
        (atomic_contract("string"), "text", pa.int64()),
        (atomic_contract("custom"), object(), pa.string()),
    ],
)
def test_arrow_scalar_conversion_rejects_missing_contract_context(contract, value, arrow_shape):
    with pytest.raises(IntegrationError, match="field"):
        _convert_value(contract, value, arrow_shape, "field")


def test_generated_and_generic_node_route_matching():
    class Generated:
        _relaxng_route_id_ = "route-a"
        _name_ = "doc"

    class Alternate:
        __relaxng_route_id__ = "route-b"
        _name_ = "doc"

    class Bare:
        _name_ = "doc"

    generic = SimpleNamespace(
        route_id="route-generic",
        generic_reason="opaque scalar contract",
        expanded_name="doc",
        name_class=Name(None, "doc"),
    )
    stable = SimpleNamespace(
        route_id="route-a",
        generic_reason=None,
        expanded_name="doc",
        name_class=Name(None, "doc"),
    )
    other = SimpleNamespace(
        route_id="route-other",
        generic_reason="opaque scalar contract",
        expanded_name="other",
        name_class=Name(None, "other"),
    )
    assert _node_route_id(Generated()) == "route-a"
    assert _node_route_id(Alternate()) == "route-b"
    assert _node_route_id(Bare()) is None
    assert _node_matches_route(Generated(), stable)
    assert not _node_matches_route(Alternate(), stable)
    assert not _node_matches_route(Bare(), stable)
    assert not _node_matches_route(Bare(), other)
    assert _node_matches_route(Bare(), generic)


def test_ambiguous_list_selection_decodes_the_selected_whole_list():
    schema = compile_element(
        '<choice><list><data type="int"/></list><data type="string"/></choice>'
    )
    route = schema.projection.routes[schema.projection.roots[0]]
    list_contract = next(
        contract for contract in route.content.scalar_contracts if contract.kind == "list"
    )
    identifier = candidate_id(schema._grammar_fingerprint, list_contract)
    record = projection(schema, columns={"value": FieldSource(candidate_id=identifier)})
    assert rows(record, typed(schema, "<doc>12</doc>")) == [{"value": (12,)}]


def test_nondecodable_whole_list_is_a_column_error():
    schema = compile_element(
        '<list><zeroOrMore><data type="opaque" datatypeLibrary="urn:nod"/></zeroOrMore></list>',
        datatype_libraries={
            "urn:nod": DatatypeLibraryRegistration(UndecodableFactory(), "nod/1"),
        },
    )
    record = projection(schema, columns={"value": FieldSource()})
    with pytest.raises(IntegrationError) as error:
        rows(record, typed(schema, "<doc>1</doc>"))
    assert "whole list is not decodable" in str(error.value)


def test_unique_contract_mismatch_on_a_forged_context_is_rejected():
    schema = compile_element('<data type="int"/>')
    record = projection(schema, columns={"value": FieldSource()})
    document = typed(schema, "<doc>12</doc>")
    context = _context(record._plan, document)
    forged_contract = atomic_contract("string")
    with pytest.raises(IntegrationError, match="differs from the selected contract"):
        _read_selected_scalar(
            context,
            document.root,
            context.text_sites[document.root],
            forged_contract,
            pa.string(),
            attribute=None,
            label="field",
        )


def test_required_absent_scalar_content_is_a_row_error():
    schema = compile_element('<data type="string"/>')
    document = typed(schema, "<doc/>")
    explicit = projection(schema, columns={"value": FieldSource()})
    with pytest.raises(IntegrationError, match="required scalar content is absent"):
        rows(explicit, document)
    default = RelaxNGRecordProjection(schema, element="doc")
    with pytest.raises(IntegrationError, match="required scalar content is absent"):
        list(iter_relaxng_rows(default._plan, document))


def test_shared_route_under_another_ancestor_cannot_climb_the_compiled_chain():
    schema = compile_grammar(
        f"""
        <start>
          <element name="doc" datatypeLibrary="{XSD}">
            <attribute name="version"><data type="int"/></attribute>
            <element name="orders">
              <attribute name="id"><data type="int"/></attribute>
              <zeroOrMore><ref name="order"/></zeroOrMore>
            </element>
            <element name="archive">
              <attribute name="id"><data type="int"/></attribute>
              <zeroOrMore><ref name="order"/></zeroOrMore>
            </element>
          </element>
        </start>
        <define name="order">
          <element name="order" datatypeLibrary="{XSD}">
            <attribute name="number"><data type="int"/></attribute>
          </element>
        </define>
        """
    )
    record = projection(
        schema,
        path=("orders", "order"),
        columns={
            "number": FieldSource(attribute="number"),
            "parent_id": FieldSource(scope="ancestor", levels=1, attribute="id"),
            "version": FieldSource(scope="root", attribute="version"),
        },
    )
    document = typed(
        schema,
        '<doc version="1"><orders id="10"><order number="1"/></orders>'
        '<archive id="20"><order number="2"/></archive></doc>',
    )
    stream = iter_relaxng_rows(record._plan, document, selector=".//order")
    assert next(stream) == {"number": 1, "parent_id": 10, "version": 1}
    with pytest.raises(IntegrationError) as error:
        next(stream)
    assert "row 2" in str(error.value)
    assert "ancestor node does not match" in str(error.value)


def test_invalid_selectors_and_plans_are_rejected():
    schema = compile_element('<data type="int"/>')
    record = projection(schema, columns={"value": FieldSource()})
    document = typed(schema, "<doc>12</doc>")
    for selector in ("", 1, object()):
        with pytest.raises(IntegrationError, match="selector"):
            list(iter_relaxng_rows(record._plan, document, selector=selector))
    with pytest.raises(IntegrationError, match="RelaxNGProjectionPlan"):
        list(iter_relaxng_rows(object(), document))


def test_batch_conversion_failure_names_the_ending_row():
    schema = compile_element(
        '<element name="item"><attribute name="id"><data type="int"/></attribute></element>'
    )
    record = projection(schema, path=("item",), columns={"id": FieldSource(attribute="id")})
    with pytest.raises(IntegrationError, match="ending at row 3"):
        record._batch([{"id": "not-an-int"}], 3)
