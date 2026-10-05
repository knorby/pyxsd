"""Export explicit observation XML files to a new manifested Parquet dataset."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from pyxsd import ParseModes, Schema
from pyxsd.batch import DocumentSource
from pyxsd.integrations.arrow import RecordProjection, records
from pyxsd.integrations.projection import FieldSource as F

DATA = Path(__file__).resolve().parents[1] / "arrow"


def prepare_projection() -> RecordProjection:
    """Prepare once, before reading inputs; this recipe uses the observation fixture."""
    schema = Schema.compile(DATA / "observations.xsd", mode=ParseModes.NAMESPACED)
    return records(
        schema,
        element="observations",
        path=("observation",),
        columns={
            "station": F(attribute="station"),
            "sequence": F(path=("sequence",)),
            "accession": F(path=("accession",)),
            "collected": F(path=("collected",)),
            "analyte": F(path=("analyte",)),
            "reading": F(path=("reading",)),
            "unit": F(path=("reading",), attribute="unit"),
            "quality": F(path=("quality",)),
        },
    )


def read_dataset(destination: Path) -> pa.Table:
    """Materialize a trusted local manifest's successful parts, ordered by provenance.

    No directory scan: unrelated files and the schema reference are not rows.
    This consumer demonstration materializes all output, unlike the writer.
    """
    metadata = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    if metadata["format_version"] != 1:
        raise ValueError("unsupported dataset manifest version")
    parts = [
        str(destination / entry["part"])
        for entry in metadata["entries"]
        if entry["status"] in ("written", "empty") and entry["part"] is not None
    ]
    table = pq.read_table(parts if parts else str(destination / metadata["schema_file"]))
    return table.sort_by([("__pyxsd_source_id", "ascending"), ("__pyxsd_row_index", "ascending")])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_root", type=Path, help="root for relative source IDs")
    parser.add_argument("destination", type=Path, help="new dataset; parent must exist")
    parser.add_argument(
        "files", nargs="*", type=Path, help="explicit observation files relative to input_root"
    )
    parser.add_argument("--errors", choices=("raise", "report"), default="report")
    parser.add_argument("--batch-size", type=int, default=10000)
    args = parser.parse_args(argv)
    try:
        root = args.input_root.resolve()
        sources = []
        for filename in args.files:
            path = (root / filename).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"file must be within the input root: {filename}")
            sources.append(DocumentSource(path.relative_to(root).as_posix(), path))
        sources.sort(key=lambda source: source.id)
        result = prepare_projection().write_dataset(
            sources,
            args.destination,
            selector="observation",
            batch_size=args.batch_size,
            errors=args.errors,
        )
        print(
            f"{result.status}: inputs={result.inputs} succeeded={result.succeeded} failed={result.failed} rows={result.rows}"
        )
        print(f"manifest: {result.manifest}")
        return 0 if result.status == "complete" else 1
    except Exception as exc:
        print(f"fatal dataset export: {exc}", file=sys.stderr)
        # CLI-only catch: retain the chained cause for diagnosis of internal/I/O faults.
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
