"""Summarize observation records with Polars; requires pyxsd[arrow] and polars."""

import argparse
import sys
from pathlib import Path

import polars as pl
import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common

DATA = Path(__file__).resolve().parent
STATIONS = DATA / "stations.json"


def _summarize(frame: pl.DataFrame | pl.LazyFrame) -> list[dict[str, object]]:
    result = (
        frame.group_by("analyte", "unit")
        .agg(
            pl.len().alias("readings"),
            pl.col("reading").count().alias("populated"),
            pl.col("reading").sum().alias("total"),
        )
        .sort("analyte", "unit")
    )
    if isinstance(result, pl.LazyFrame):
        result = result.collect()
    return [
        {
            "analyte": row["analyte"],
            "unit": row["unit"],
            "readings": int(row["readings"]),
            "populated": int(row["populated"]),
            "total": row["total"],
        }
        for row in result.iter_rows(named=True)
    ]


def summarize_table(table: pa.Table, stations: list[dict[str, str]]) -> list[dict[str, object]]:
    """Aggregate readings by analyte and unit from an imported Arrow table."""
    common.assert_consumer_profile(table)
    return _summarize(pl.from_arrow(table))


def summarize_parquet(path: Path, stations: list[dict[str, str]]) -> list[dict[str, object]]:
    """Aggregate the same findings from a lazy Parquet scan."""
    return _summarize(pl.scan_parquet(path))


def with_regions(table: pa.Table, stations: list[dict[str, str]]) -> list[dict[str, object]]:
    """Left join observations to the region catalog; unmatched rows stay."""
    catalog = pl.DataFrame(
        {
            "station": [entry["station"] for entry in stations],
            "region": [entry["region"] for entry in stations],
        }
    )
    joined = (
        pl.from_arrow(table)
        .select("sequence", "station")
        .join(catalog, on="station", how="left")
        .with_columns(pl.col("region").fill_null("unknown"))
        .sort("sequence")
    )
    return [
        {"sequence": row["sequence"], "station": row["station"], "region": row["region"]}
        for row in joined.iter_rows(named=True)
    ]


def passing_sequences(table: pa.Table) -> list[int]:
    """Sequences whose quality flag is true."""
    frame = pl.from_arrow(table).filter(pl.col("quality")).sort("sequence")
    return frame.get_column("sequence").to_list()


def _child_rows(frame: pl.DataFrame, column: str) -> int:
    # Empty and null lists both normalize to zero child rows.
    return frame.select(pl.col(column).explode(empty_as_null=False).drop_nulls()).height


def child_row_counts(nested_table: pa.Table) -> dict[str, int]:
    """Count expanded tag and replicate children; absent lists yield no rows."""
    frame = pl.from_arrow(nested_table)
    return {
        "tags": _child_rows(frame, "tag"),
        "replicates": _child_rows(frame, "replicates"),
    }


def reading_units(nested_table: pa.Table) -> list[dict[str, object]]:
    """Show the nil flag and retained unit attribute of each nested reading."""
    rows = (
        pl.from_arrow(nested_table)
        .select(
            pl.col("sequence"),
            pl.col("reading").struct.field("@unit").alias("unit"),
            pl.col("reading").struct.field("$nil").alias("is_nil"),
        )
        .sort("sequence")
        .iter_rows(named=True)
    )
    return [
        {"sequence": row["sequence"], "unit": row["unit"], "is_nil": row["is_nil"]} for row in rows
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
