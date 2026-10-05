"""Summarize observation records with DuckDB; requires pyxsd[arrow] and duckdb."""

import argparse
import sys
from pathlib import Path

import duckdb
import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common

DATA = Path(__file__).resolve().parent
STATIONS = DATA / "stations.json"

SUMMARY_SQL = """
SELECT analyte, unit,
       COUNT(*) AS readings,
       COUNT(reading) AS populated,
       SUM(reading) AS total
FROM {source}
GROUP BY analyte, unit
ORDER BY analyte, unit
"""


def _summary_rows(rows) -> list[dict[str, object]]:
    return [
        {
            "analyte": analyte,
            "unit": unit,
            "readings": readings,
            "populated": populated,
            "total": total,
        }
        for analyte, unit, readings, populated, total in rows
    ]


def summarize_table(table: pa.Table, stations: list[dict[str, str]]) -> list[dict[str, object]]:
    """Aggregate readings by analyte and unit from a registered Arrow table."""
    common.assert_consumer_profile(table)
    with duckdb.connect() as con:
        con.register("observations", table)
        rows = con.execute(SUMMARY_SQL.format(source="observations")).fetchall()
    return _summary_rows(rows)


def summarize_parquet(path: Path, stations: list[dict[str, str]]) -> list[dict[str, object]]:
    """Aggregate the same findings from an existing Parquet file."""
    with duckdb.connect() as con:
        rows = con.execute(
            SUMMARY_SQL.format(source="read_parquet($path)"), {"path": str(path)}
        ).fetchall()
    return _summary_rows(rows)


def with_regions(table: pa.Table, stations: list[dict[str, str]]) -> list[dict[str, object]]:
    """Left join observations to the region catalog; unmatched rows stay."""
    catalog = pa.table(
        {
            "station": pa.array([entry["station"] for entry in stations]),
            "region": pa.array([entry["region"] for entry in stations]),
        }
    )
    with duckdb.connect() as con:
        con.register("observations", table)
        con.register("stations", catalog)
        rows = con.execute(
            """
            SELECT o.sequence, o.station, COALESCE(s.region, 'unknown') AS region
            FROM observations AS o
            LEFT JOIN stations AS s ON o.station = s.station
            ORDER BY o.sequence
            """
        ).fetchall()
    return [
        {"sequence": sequence, "station": station, "region": region}
        for sequence, station, region in rows
    ]


def passing_sequences(table: pa.Table) -> list[int]:
    """Sequences whose quality flag is true."""
    with duckdb.connect() as con:
        con.register("observations", table)
        rows = con.execute(
            "SELECT sequence FROM observations WHERE quality ORDER BY sequence"
        ).fetchall()
    return [sequence for (sequence,) in rows]


def child_row_counts(nested_table: pa.Table) -> dict[str, int]:
    """Count expanded tag and replicate children; absent lists yield no rows."""
    with duckdb.connect() as con:
        con.register("observations", nested_table)
        tags = con.execute(
            "SELECT COUNT(*) FROM (SELECT UNNEST(tag) FROM observations)"
        ).fetchone()[0]
        replicates = con.execute(
            "SELECT COUNT(*) FROM (SELECT UNNEST(replicates) FROM observations "
            "WHERE replicates IS NOT NULL)"
        ).fetchone()[0]
    return {"tags": tags, "replicates": replicates}


def reading_units(nested_table: pa.Table) -> list[dict[str, object]]:
    """Show the nil flag and retained unit attribute of each nested reading."""
    with duckdb.connect() as con:
        con.register("observations", nested_table)
        rows = con.execute(
            """
            SELECT sequence, reading['@unit'] AS unit, reading['$nil'] AS is_nil
            FROM observations
            ORDER BY sequence
            """
        ).fetchall()
    return [
        {"sequence": sequence, "unit": unit, "is_nil": is_nil} for sequence, unit, is_nil in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--write-parquet", type=Path, help="export the contextual table, then summarize XML"
    )
    source.add_argument(
        "--parquet", type=Path, help="summarize an existing Parquet file; no XML is read"
    )
    args = parser.parse_args()
    stations = common.load_stations(STATIONS)
    if args.parquet is not None:
        summary = summarize_parquet(args.parquet, stations)
    else:
        _, document, nested, contextual = common.prepare_observations()
        table = contextual.table(document, selector="observation", revalidate=True)
        if args.write_parquet is not None:
            contextual.write_parquet(
                document, args.write_parquet, selector="observation", batch_size=2
            )
        summary = summarize_table(table, stations)
        nested_table = nested.table(document, selector="observation", revalidate=True)
        print("--- nested struct and list demonstration (XML path only) ---")
        print(
            "Reading units:",
            [(row["sequence"], row["unit"]) for row in reading_units(nested_table)],
        )
        print("Child row counts:", child_row_counts(nested_table))
        print(
            "Station regions:",
            [(row["sequence"], row["region"]) for row in with_regions(table, stations)],
        )
        print("Passing quality sequences:", passing_sequences(table))
        print("--- summary ---")
    for row in summary:
        print(
            f"{row['analyte']} [{row['unit']}] readings={row['readings']} "
            f"populated={row['populated']} total={row['total']}"
        )


if __name__ == "__main__":
    main()
