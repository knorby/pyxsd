"""Explicit Arrow schemas and real Parquet files, independent of Pydantic."""

import io
from decimal import Decimal

import pytest

from integration_helpers import RECORD_SCHEMA, compile_schema, parse

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


def records(schema, element="root", path=()):
    from pyxsd.integrations.arrow import records

    return records(schema, element=element, path=path)


def test_explicit_schema_nested_values_and_parquet_readback(tmp_path):
    schema = compile_schema(RECORD_SCHEMA)
    projection = records(schema)
    doc = parse(
        schema, "<root><count>7</count><item>1</item><flag>true</flag><amount>9999</amount></root>"
    )
    table = projection.table(doc)
    assert table.schema.field("count").type == pa.int32()
    assert table.schema.field("item").type.value_type == pa.int32()
    assert table.schema.field("amount").type.field("$").type == pa.decimal128(6, 2)
    assert table.to_pylist() == [
        {
            "@version": 9,
            "count": 7,
            "item": [1],
            "flag": True,
            "amount": {"@currency": "USD", "$": Decimal("9999.00")},
            "missing": None,
        }
    ]
    path = tmp_path / "result.parquet"
    projection.write_parquet(doc, path, batch_size=1)
    result = pq.read_table(path)
    assert result.equals(table)
    assert result.schema.metadata[b"pyxsd:projection"] == b"1"


def test_local_records_empty_selection_and_batched_writer(tmp_path):
    schema = compile_schema("""<xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="row" minOccurs="0" maxOccurs="unbounded"><xs:complexType><xs:sequence>
    <xs:element name="id" type="xs:int"/><xs:element name="extra" type="xs:string" minOccurs="0"/>
    </xs:sequence></xs:complexType></xs:element></xs:sequence></xs:complexType></xs:element>""")
    projection = records(schema, path=("row",))
    empty = parse(schema, "<root/>")
    table = projection.table(empty, selector="row")
    assert table.num_rows == 0
    assert table.column_names == ["id", "extra"]
    path = tmp_path / "empty.parquet"
    projection.write_parquet(empty, path, selector="row")
    assert pq.read_table(path).schema.equals(table.schema)
    document = parse(
        schema,
        "<root><row><id>1</id></row><row><id>2</id><extra>later</extra></row>"
        "<row><id>3</id></row></root>",
    )
    assert [
        batch.num_rows for batch in projection.batches(document, selector="row", batch_size=2)
    ] == [2, 1]
    projection.write_parquet(document, path, selector="row", batch_size=2)
    assert pq.read_table(path).to_pylist() == [
        {"id": 1, "extra": None},
        {"id": 2, "extra": "later"},
        {"id": 3, "extra": None},
    ]


def test_exact_unbounded_integer_decimal_binary_temporal_and_lists():
    schema = compile_schema("""<xs:simpleType name="L"><xs:list itemType="xs:int"/></xs:simpleType>
    <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="big" type="xs:integer"/><xs:element name="money" type="xs:decimal"/>
    <xs:element name="binary" type="xs:base64Binary"/><xs:element name="date" type="xs:dateTime"/>
    <xs:element name="tokens" type="L" maxOccurs="unbounded"/>
    </xs:sequence></xs:complexType></xs:element>""")
    projection = records(schema)
    doc = parse(
        schema,
        "<root><big>123456789012345678901234567890</big><money>0.00000000001</money>"
        "<binary>AP8=</binary><date>0000-01-01T00:00:00.123456789</date><tokens>1 2</tokens><tokens>3</tokens></root>",
    )
    assert projection.table(doc).to_pylist() == [
        {
            "big": "123456789012345678901234567890",
            "money": "0.00000000001",
            "binary": b"\x00\xff",
            "date": "0000-01-01T00:00:00.123456789",
            "tokens": [[1, 2], [3]],
        }
    ]


def test_nil_complex_attributes_and_nullable_list_items_survive_parquet(tmp_path):
    schema = compile_schema(RECORD_SCHEMA)
    projection = records(schema)
    doc = parse(
        schema,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><count>7</count>'
        '<item xsi:nil="true"/><missing xsi:nil="true" reason="unknown"/></root>',
    )
    path = tmp_path / "nil.parquet"
    projection.write_parquet(doc, path)
    row = pq.read_table(path).to_pylist()[0]
    assert row["missing"] == {"$nil": True, "@reason": "unknown"}
    assert row["item"] == [None]


def test_late_batch_failure_preserves_existing_destination(tmp_path):
    from pyxsd.integrations import IntegrationError

    schema = compile_schema("""<xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="row" type="xs:int" maxOccurs="unbounded"/>
    </xs:sequence></xs:complexType></xs:element>""")
    doc = parse(schema, "<root><row>1</row><row>2</row></root>")
    projection = records(schema, path=("row",))
    doc.root._children_[1]._value_ = ["bad"]
    path = tmp_path / "existing.parquet"
    path.write_bytes(b"existing contents")
    with pytest.raises(IntegrationError, match="row 2"):
        projection.write_parquet(doc, path, selector="row", batch_size=1)
    assert path.read_bytes() == b"existing contents"
    assert list(tmp_path.iterdir()) == [path]


def test_borrowed_sink_remains_open_and_invalid_batch_size_is_rejected():
    from pyxsd.integrations import IntegrationError

    schema = compile_schema(RECORD_SCHEMA)
    doc = parse(schema, "<root><count>7</count></root>")
    projection = records(schema)
    sink = io.BytesIO()
    projection.write_parquet(doc, sink)
    assert not sink.closed
    assert pq.read_table(pa.BufferReader(sink.getvalue())).num_rows == 1
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(IntegrationError, match="batch_size"):
            list(projection.batches(doc, batch_size=invalid))


def test_selection_declaration_mismatch_and_empty_struct_preflight():
    from pyxsd.integrations import IntegrationError

    schema = compile_schema(RECORD_SCHEMA)
    doc = parse(schema, "<root><count>7</count><item>1</item></root>")
    with pytest.raises(IntegrationError, match="declaration"):
        records(schema, path=("count",)).table(doc, selector="item")
    empty = compile_schema('<xs:element name="root"><xs:complexType/></xs:element>')
    with pytest.raises(IntegrationError, match="empty"):
        records(empty)


@pytest.mark.parametrize(
    "restriction, expected, value",
    [
        (
            '<xs:restriction base="xs:integer"><xs:minInclusive value="0"/><xs:maxInclusive value="255"/></xs:restriction>',
            "uint8",
            "255",
        ),
        (
            '<xs:restriction base="xs:integer"><xs:minExclusive value="-129"/><xs:maxExclusive value="128"/></xs:restriction>',
            "int8",
            "-128",
        ),
        (
            '<xs:restriction base="xs:decimal"><xs:totalDigits value="40"/><xs:fractionDigits value="2"/></xs:restriction>',
            "decimal256(42, 2)",
            "9999",
        ),
        (
            '<xs:restriction base="xs:decimal"><xs:totalDigits value="80"/><xs:fractionDigits value="2"/></xs:restriction>',
            "string",
            "9999",
        ),
    ],
)
def test_schema_bound_type_selection_does_not_depend_on_values(restriction, expected, value):
    schema = compile_schema(
        f'<xs:element name="root"><xs:simpleType>{restriction}</xs:simpleType></xs:element>'
    )
    projection = records(schema)
    assert str(projection.schema.field("value").type) == expected
    assert projection.table(parse(schema, f"<root>{value}</root>")).num_rows == 1


def test_float_list_and_qname_primitive_records():
    cases = [
        ("xs:double", "INF", float("inf")),
        ("xs:NMTOKENS", "a b", ["a", "b"]),
        ("xs:QName", "p:kind", "{urn:t}kind"),
    ]
    for name, lexical, expected in cases:
        schema = compile_schema(f'<xs:element name="root" type="{name}"/>')
        doc = parse(schema, f'<root xmlns:p="urn:t">{lexical}</root>')
        assert records(schema).table(doc).to_pylist() == [{"value": expected}]
