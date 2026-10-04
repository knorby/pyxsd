"""Write scalar order-line rows with parent context; requires only pyxsd[arrow]."""

import argparse
from pathlib import Path

import pyarrow.parquet as pq

import pyxsd
from pyxsd.integrations.arrow import RecordProjection, records
from pyxsd.integrations.projection import FieldSource

# Reuse the XML/XSD dataset, not the Pydantic demo or its adapter.
DATA = Path(__file__).resolve().parents[1] / "pydantic"


def prepare() -> tuple[pyxsd.Schema, RecordProjection]:
    schema = pyxsd.compile(DATA / "orders.xsd", mode=pyxsd.ParseModes.NAMESPACED)
    projection = records(
        schema,
        element="orders",
        path=("order", "line"),
        columns={
            "order_number": FieldSource(scope="ancestor", levels=1, attribute="number"),
            "customer_account": FieldSource(
                scope="ancestor", levels=1, path=("customer",), attribute="account"
            ),
            "sku": FieldSource(attribute="sku"),
            "quantity": FieldSource(path=("quantity",)),
            "unit_price": FieldSource(path=("price",)),
            "currency": FieldSource(path=("price",), attribute="currency"),
        },
    )
    return schema, projection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="Parquet output path")
    args = parser.parse_args()
    schema, projection = prepare()
    print("Schema-derived scalar columns:")
    print(projection.schema)
    document = schema.parse(DATA / "orders.xml")
    projection.write_parquet(
        document, args.destination, selector="order/line", batch_size=2, revalidate=True
    )
    restored = pq.read_table(args.destination)
    expected = projection.table(document, selector="order/line", revalidate=True)
    if not restored.equals(expected, check_metadata=True):
        raise RuntimeError("Parquet readback differs from the contextual projection")
    print(f"{restored.num_rows} order-line rows; Parquet values and types verified")
    for row in restored.to_pylist():
        print(row)


if __name__ == "__main__":
    main()
