"""Extract schema-typed environmental observations; requires pyxsd[arrow]."""

import argparse
from pathlib import Path

import pyarrow.parquet as pq

import pyxsd
from pyxsd.integrations.arrow import RecordProjection, records

DATA = Path(__file__).resolve().parent


def prepare() -> tuple[pyxsd.Schema, RecordProjection]:
    schema = pyxsd.compile(DATA / "observations.xsd", mode=pyxsd.ParseModes.NAMESPACED)
    # Select the local record declaration BEFORE selecting instance occurrences.
    projection = records(schema, element="observations", path=("observation",))
    return schema, projection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="Parquet output path")
    args = parser.parse_args()
    schema, projection = prepare()
    print("Schema-derived observation columns (no row-based type inference)")
    print(projection.schema)
    document = schema.parse(DATA / "observations.xml")
    document.require_valid()
    table = projection.table(document, selector="observation", revalidate=True)
    print(f"Selected {table.num_rows} rows")
    for row in table.to_pylist():
        print(row)
    print(
        "Batch row counts:",
        [b.num_rows for b in projection.batches(document, selector="observation", batch_size=2)],
    )
    # This writer processes batches directly; it does not use `table` above.
    projection.write_parquet(document, args.destination, selector="observation", batch_size=2)
    restored = pq.read_table(args.destination)
    if not restored.equals(table):
        raise RuntimeError("Parquet readback differs from the schema-typed table")
    print(f"Parquet readback: {restored.num_rows} rows; values and schema preserved")
    empty = projection.table(document, selector="observation[@station='absent']")
    print(
        f"Empty selection: {empty.num_rows} rows; same schema: {empty.schema.equals(table.schema)}"
    )


if __name__ == "__main__":
    main()
