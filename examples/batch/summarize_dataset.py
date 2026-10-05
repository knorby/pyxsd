"""Reuse the analytical observation queries over manifest-selected dataset parts."""

import argparse
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--consumer", choices=("duckdb", "polars"), required=True)
    args = parser.parse_args()
    table = export_dataset.read_dataset(args.destination)
    analytics = Path(__file__).resolve().parents[1] / "analytics"
    sys.path.insert(0, str(analytics))
    consumer = importlib.import_module(f"{args.consumer}_observations")
    stations = consumer.common.load_stations(analytics / "stations.json")
    for row in consumer.summarize_table(table, stations):
        print(
            f"{row['analyte']} [{row['unit']}] readings={row['readings']} populated={row['populated']} total={row['total']}"
        )


if __name__ == "__main__":
    main()
