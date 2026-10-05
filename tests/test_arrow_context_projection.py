"""Fixed Arrow scalar-column schemas and contextual output guarantees."""

import io
import json
from decimal import Decimal

import pytest

from integration_helpers import RECORD_SCHEMA, compile_schema, parse
from pyxsd import ValidationError
from pyxsd.integrations import IntegrationError
from pyxsd.integrations.projection import FieldSource as F
from test_projection_plan import ORDER_XML, ORDERS, order_columns

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
adapter = pytest.importorskip("pyxsd.integrations.arrow")
RecordProjection, records = adapter.RecordProjection, adapter.records


def orders_projection(schema):
    return records(schema, element="orders", path=("order", "line"), columns=order_columns())


def test_columns_schema_before_xml():
    schema = compile_schema(ORDERS)
    projection = orders_projection(schema)
    assert projection.schema.names == ["number", "account", "sku", "quantity"]
    assert projection.schema.types == [pa.string(), pa.string(), pa.string(), pa.int32()]
    assert not any(field.nullable for field in projection.schema)
    assert RecordProjection(
        schema, element="orders", path=("order", "line"), columns=order_columns()
    ).schema.equals(projection.schema, check_metadata=True)
    with pytest.raises(IntegrationError, match="nonempty"):
        records(schema, element="orders", columns={})


def test_nil_primitive_root_keeps_typed_null_column():
    schema = compile_schema('<xs:element name="r" type="xs:int" nillable="true"/>')
    projection = records(schema, element="r", columns={"value": F()})
    doc = parse(schema, '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>')
    result = projection.table(doc)
    assert result.to_pylist() == [{"value": None}]
    assert result.schema.field("value").type == pa.int32()
    assert result.schema.field("value").nullable


def test_column_order_and_source_metadata():
    schema = compile_schema(ORDERS)
    projection = records(
        schema,
        element="orders",
        path=("order", "line"),
        columns={
            "quantity!": F(path=("quantity",)),
            "order #": F(scope="ancestor", levels=1, attribute="number"),
        },
    )
    assert projection.schema.names == ["quantity!", "order #"]
    assert projection.schema.metadata[b"pyxsd:projection"] == b"2"
    assert projection.schema.metadata[b"pyxsd:mode"] == b"columns"
    metadata = projection.schema.field("order #").metadata
    assert json.loads(metadata[b"pyxsd:source"]) == {
        "scope": "ancestor",
        "levels": 1,
        "path": [],
        "attribute": "number",
    }
    assert json.loads(metadata[b"pyxsd:declaration"]) == ["orders", "order", "@number"]
    assert json.loads(metadata[b"pyxsd:row"]) == ["orders", "order", "line"]
    assert b"pyxsd:facets" in metadata


def test_empty_and_nonempty_schema_equal():
    schema = compile_schema(ORDERS)
    projection = orders_projection(schema)
    empty = projection.table(parse(schema, "<orders/>"), selector="order/line")
    populated = projection.table(parse(schema, ORDER_XML), selector="order/line")
    assert empty.num_rows == 0
    assert empty.schema.equals(populated.schema, check_metadata=True)
    assert populated.to_pylist() == [
        {"number": "A", "account": "C1", "sku": "X", "quantity": 2},
        {"number": "A", "account": "C1", "sku": "Y", "quantity": 3},
        {"number": "B", "account": "C2", "sku": "Z", "quantity": 4},
    ]


def test_context_batches():
    schema = compile_schema(ORDERS)
    projection = orders_projection(schema)
    doc = parse(schema, ORDER_XML)
    assert [b.num_rows for b in projection.batches(doc, selector="order/line", batch_size=2)] == [
        2,
        1,
    ]
    for size in [0, -1, True, 1.5, "2"]:
        with pytest.raises(IntegrationError, match="batch_size"):
            list(projection.batches(doc, batch_size=size))


def test_columns_none_preserves_legacy_schema():
    schema = compile_schema(RECORD_SCHEMA)
    legacy = records(schema, element="root")
    assert records(schema, element="root", columns=None).schema.equals(
        legacy.schema, check_metadata=True
    )
    assert legacy.schema.metadata[b"pyxsd:projection"] == b"1"


def test_exact_scalar_policies_and_nil_simple_content():
    schema = compile_schema("""<xs:simpleType name="Money"><xs:restriction base="xs:decimal">
    <xs:totalDigits value="4"/><xs:fractionDigits value="2"/></xs:restriction></xs:simpleType>
    <xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="price" nillable="true"><xs:complexType><xs:simpleContent><xs:extension base="Money">
    <xs:attribute name="currency" use="required"/></xs:extension></xs:simpleContent></xs:complexType></xs:element>
    <xs:element name="big" type="xs:integer"/><xs:element name="flag" type="xs:boolean"/>
    <xs:element name="time" type="xs:dateTime"/><xs:element name="q" type="xs:QName"/>
    <xs:element name="data" type="xs:hexBinary"/>
    </xs:sequence></xs:complexType></xs:element>""")
    columns = {
        "price": F(path=("price",)),
        "currency": F(path=("price",), attribute="currency"),
        **{n: F(path=(n,)) for n in ["big", "flag", "time", "q", "data"]},
    }
    projection = records(schema, element="r", columns=columns)
    assert projection.schema.field("price").type == pa.decimal128(6, 2)
    assert projection.schema.field("price").nullable
    assert not projection.schema.field("currency").nullable
    xml = '<r xmlns:p="urn:q"><price currency="USD">9999</price><big>123456789012345678901234567890</big><flag>true</flag><time>12026-01-01T00:00:00Z</time><q>p:x</q><data>CAFE</data></r>'
    result = projection.table(parse(schema, xml)).to_pylist()[0]
    assert result == {
        "price": Decimal("9999"),
        "currency": "USD",
        "big": "123456789012345678901234567890",
        "flag": True,
        "time": "12026-01-01T00:00:00Z",
        "q": "{urn:q}x",
        "data": b"\xca\xfe",
    }
    nil = xml.replace(
        '<price currency="USD">9999</price>',
        '<price xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true" currency="EUR"/>',
    )
    result = projection.table(parse(schema, nil)).to_pylist()[0]
    assert result["price"] is None and result["currency"] == "EUR"


def test_optional_path_nullability():
    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="optional" minOccurs="0"><xs:complexType><xs:sequence>
    <xs:element name="required" type="xs:int"/></xs:sequence></xs:complexType></xs:element>
    </xs:sequence></xs:complexType></xs:element>""")
    projection = records(schema, element="r", columns={"v": F(path=("optional", "required"))})
    assert projection.schema.field("v").nullable
    assert projection.table(parse(schema, "<r/>")).to_pylist() == [{"v": None}]


def test_revalidation_uses_returned_document_once(monkeypatch, tmp_path):
    schema = compile_schema(ORDERS)
    original = parse(schema, ORDER_XML)
    fresh = parse(schema, ORDER_XML.replace('number="A"', 'number="FRESH"'))
    calls = []
    from pyxsd.document import Document

    def revalidate(document):
        calls.append(document)
        return fresh

    monkeypatch.setattr(Document, "revalidate", revalidate)
    projection = orders_projection(schema)
    for operation in ["table", "batches", "write_parquet"]:
        calls.clear()
        if operation == "table":
            assert (
                projection.table(original, selector="order/line", revalidate=True).to_pylist()[0][
                    "number"
                ]
                == "FRESH"
            )
        elif operation == "batches":
            assert (
                next(
                    projection.batches(original, selector="order/line", revalidate=True)
                ).to_pylist()[0]["number"]
                == "FRESH"
            )
        else:
            target = tmp_path / "fresh.parquet"
            projection.write_parquet(original, target, selector="order/line", revalidate=True)
            assert pq.read_table(target).to_pylist()[0]["number"] == "FRESH"
        assert calls == [original]


def test_historical_errors_require_revalidation_and_foreign_schema_rejected():
    schema = compile_schema(ORDERS)
    doc = schema.parse(io.StringIO(ORDER_XML.replace(' number="A"', "")))
    assert doc.report.has_errors
    doc.root._children_[0]._attribs_["number"] = "A"
    projection = orders_projection(schema)
    with pytest.raises(ValidationError):
        projection.table(doc, selector="order/line")
    assert projection.table(doc, selector="order/line", revalidate=True).num_rows == 3
    with pytest.raises(IntegrationError, match="another compiled schema"):
        projection.table(parse(compile_schema(ORDERS), ORDER_XML), selector="order/line")


def test_context_parquet_readback_and_typed_empty(tmp_path):
    schema = compile_schema(ORDERS)
    projection = orders_projection(schema)
    for xml, count in [(ORDER_XML, 3), ("<orders/>", 0)]:
        target = tmp_path / "rows.parquet"
        doc = parse(schema, xml)
        projection.write_parquet(doc, target, selector="order/line", batch_size=1)
        result = pq.read_table(target)
        assert result.num_rows == count
        assert result.equals(projection.table(doc, selector="order/line"), check_metadata=True)


def test_preparation_failure_preserves_destination(tmp_path):
    target = tmp_path / "existing.parquet"
    target.write_bytes(b"original")
    with pytest.raises(IntegrationError):
        projection = records(compile_schema(ORDERS), element="orders", columns={"struct": F()})
        projection.write_parquet(None, target)
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]


def test_late_column_failure_preserves_destination_and_cause(tmp_path):
    schema = compile_schema(ORDERS)
    doc = parse(schema, ORDER_XML)
    doc.root._children_[1]._children_[1]._children_[0]._value_ = ["bad"]
    target = tmp_path / "existing.parquet"
    target.write_bytes(b"original")
    with pytest.raises(IntegrationError, match=r"row 3 column 'quantity'.*quantity") as error:
        orders_projection(schema).write_parquet(doc, target, selector="order/line", batch_size=1)
    assert error.value.__cause__ is not None
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]


def test_borrowed_context_sink_stays_open_on_success_and_failure():
    schema = compile_schema(ORDERS)
    projection = orders_projection(schema)
    doc = parse(schema, ORDER_XML)
    sink = io.BytesIO()
    projection.write_parquet(doc, sink, selector="order/line", batch_size=1)
    assert not sink.closed
    assert pq.read_table(io.BytesIO(sink.getvalue())).num_rows == 3
    doc.root._children_[1]._children_[1]._children_[0]._value_ = ["bad"]
    sink = io.BytesIO()
    with pytest.raises(IntegrationError, match="row 3") as error:
        projection.write_parquet(doc, sink, selector="order/line", batch_size=1)
    assert not sink.closed and sink.getvalue()
    assert error.value.__cause__ is not None


def test_context_writer_does_not_materialize_rows_or_table(monkeypatch, tmp_path):
    schema = compile_schema(ORDERS)
    projection = orders_projection(schema)
    rows_method, batch_method = projection._rows, projection._batch
    state = {"yielded": 0, "batched": 0}

    def streamed(*args):
        for row in rows_method(*args):
            assert state["yielded"] == state["batched"]
            state["yielded"] += 1
            yield row

    def batch(values, last):
        state["batched"] += len(values)
        return batch_method(values, last)

    def no_table(*args, **kwargs):
        raise AssertionError("writer materialized a table")

    monkeypatch.setattr(projection, "_rows", streamed)
    monkeypatch.setattr(projection, "_batch", batch)
    monkeypatch.setattr(projection, "table", no_table)
    target = tmp_path / "streamed.parquet"
    projection.write_parquet(parse(schema, ORDER_XML), target, selector="order/line", batch_size=1)
    assert state == {"yielded": 3, "batched": 3}
    assert pq.read_table(target).num_rows == 3
